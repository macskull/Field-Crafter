from __future__ import annotations

import json
import math
import re
import sqlite3
import tempfile
import unicodedata
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from .calculator import CalculationError, calculate_payload
from .normalize import clean_text
from .ocr import OCRError, OCRResult, OCRSpan, cluster_visual_lines, create_backend


_WORD = re.compile(r"[a-z0-9]+")
_LEVEL = re.compile(r"\bleve[l1i|]?\s*[:#]?\s*([0-9sSoOlI|$]{1,2})\b", re.I)
_LEADING_QTY = re.compile(r"^\s*([0-9]{1,3})\s+(.*)$")
_FRACTION = re.compile(r"(?<!\d)(\d{1,4})\s*/\s*(\d{1,4})(?!\d)")

_TOKEN_EXPANSIONS = {
    "acc": "accuracy", "accur": "accuracy", "ace": "accuracy", "acs": "accuracy",
    "dam": "damage", "dmg": "damage",
    "def": "defense",
    "end": "endurance", "endrdx": "endurance", "ena": "endurance", "bnd": "endurance", "lind": "endurance",
    "rech": "recharge", "rch": "recharge",
    "res": "resistance",
    "immob": "immobilize",
    "kb": "knockback",
    "tohit": "tohit",
    "hit": "hit",
    "dur": "duration",
}

_DROP_TOKENS = {"recipe", "level", "reduction"}


def _ascii(value: str) -> str:
    return unicodedata.normalize("NFKD", clean_text(value)).encode("ascii", "ignore").decode("ascii")


def _fix_ocr_token(token: str) -> str:
    t = token.lower()
    # Frequent OCR confusions that are low-risk inside alphabetic recipe/salvage names.
    if len(t) > 2:
        t = t.replace("0", "o")
    return _TOKEN_EXPANSIONS.get(t, t)


def _tokens(value: str) -> list[str]:
    text = _ascii(value).lower()
    text = re.sub(r"\[?\s*level\s*[0-9a-z|]+\s*\]?", " ", text, flags=re.I)
    text = text.replace("(recipe)", " ")
    raw = [_fix_ocr_token(x) for x in _WORD.findall(text)]
    out: list[str] = []
    for token in raw:
        if token in _DROP_TOKENS or token.isdigit():
            continue
        if token == "duration" and out and out[-1] in {"hold", "sleep", "stun", "confuse", "immobilize"}:
            continue
        out.append(token)
    return out


def _norm(value: str) -> str:
    return " ".join(_tokens(value))


def _multiset_f1(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    ca, cb = Counter(a), Counter(b)
    common = sum((ca & cb).values())
    return (2.0 * common) / (sum(ca.values()) + sum(cb.values()))


def _soft_token_f1(a: list[str], b: list[str]) -> float:
    """Greedy fuzzy token F1 for OCR misspellings such as Contagions/Contagious."""
    if not a or not b:
        return 0.0
    remaining = list(b)
    matched = 0.0
    for token in a:
        best_i = None
        best = 0.0
        for i, other in enumerate(remaining):
            score = 1.0 if token == other else SequenceMatcher(None, token, other).ratio()
            if score > best:
                best = score; best_i = i
        if best_i is not None and best >= 0.68:
            matched += best
            remaining.pop(best_i)
    return (2.0 * matched) / (len(a) + len(b))


def _name_score(observed: str, canonical: str) -> float:
    a, b = _tokens(observed), _tokens(canonical)
    if not a or not b:
        return 0.0
    seq = SequenceMatcher(None, " ".join(a), " ".join(b)).ratio()
    f1 = max(_multiset_f1(a, b), _soft_token_f1(a, b))
    score = 0.38 * seq + 0.62 * f1

    # Recipe set names are unusually strong identifiers. When both strings retain a
    # colon, score the set prefix independently so OCR damage in one enhancement piece
    # does not erase a clearly recognized set name.
    if ":" in observed and ":" in canonical:
        op, os = observed.split(":", 1)
        cp, cs = canonical.split(":", 1)
        prefix = SequenceMatcher(None, _norm(op), _norm(cp)).ratio()
        piece_a, piece_b = _tokens(os), _tokens(cs)
        piece_seq = SequenceMatcher(None, " ".join(piece_a), " ".join(piece_b)).ratio() if piece_a and piece_b else 0.0
        piece_f1 = max(_multiset_f1(piece_a, piece_b), _soft_token_f1(piece_a, piece_b))
        structured = 0.40 * prefix + 0.60 * (0.35 * piece_seq + 0.65 * piece_f1)
        score = max(score, structured)
        # The game UI and Wiki do not always present multi-aspect enhancement tokens
        # in the same order (for example End/Def vs Defense/Endurance). Once the set
        # prefix is clearly the same, an exact token multiset is effectively an exact
        # semantic match and should not be penalized by sequence order.
        if prefix >= 0.90 and Counter(piece_a) == Counter(piece_b) and piece_a:
            score = max(score, 0.995)

    # Superior is semantically important; do not allow a normal/superior pair to look identical.
    if ("superior" in a) != ("superior" in b):
        score *= 0.72
    return max(0.0, min(1.0, score))


def _rect_for(spans: list[OCRSpan]) -> tuple[float, float, float, float]:
    x1 = min(s.x for s in spans); y1 = min(s.y for s in spans)
    x2 = max(s.x2 for s in spans); y2 = max(s.y2 for s in spans)
    return x1, y1, x2 - x1, y2 - y1


def _line_text(line: list[OCRSpan]) -> str:
    return " ".join(s.text for s in line)


def _line_conf(line: list[OCRSpan]) -> float:
    if not line:
        return 0.0
    return sum(s.confidence for s in line) / len(line)


def _spans_text(spans: list[OCRSpan]) -> str:
    visual = cluster_visual_lines(spans)
    return " ".join(_line_text(line) for line in visual)


def _parse_int_token(text: str) -> int | None:
    t = text.strip()
    if t.isdigit():
        return int(t)
    # Single-character OCR confusions are safe to treat as 1 only in quantity context.
    if t in {"l", "I", "|", "!", "ι", "Ι", "ı"}:
        return 1
    return None


def _parse_quantity_text(text: str) -> int | None:
    """Parse a tiny OCR'd stack count, allowing common digit/letter confusions."""
    if not text:
        return None
    stripped = text.strip()
    if stripped in {"ι", "Ι", "ı"}:
        return 1
    cleaned = re.sub(r"[^0-9A-Za-z|!]", "", stripped)
    if not cleaned or len(cleaned) > 4:
        return None
    trans = str.maketrans({
        "I": "1", "i": "1", "l": "1", "L": "1", "|": "1", "!": "1", "ι": "1", "Ι": "1", "ı": "1",
        "O": "0", "o": "0",
        "S": "5", "s": "5",
    })
    normalized = cleaned.translate(trans)
    if not normalized.isdigit():
        return None
    try:
        value = int(normalized)
    except ValueError:
        return None
    return value if 1 <= value <= 999 else None


def _parse_level(text: str) -> int | None:
    m = _LEVEL.search(text)
    if not m:
        return None
    raw = m.group(1).replace("S", "5").replace("s", "5").replace("$", "5").replace("O", "0").replace("o", "0")
    raw = raw.replace("l", "1").replace("I", "1").replace("|", "1")
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 0 <= value <= 50 else None


def _offset_ocr_result(ocr: OCRResult, x_offset: float, y_offset: float, *, image_width: int, image_height: int) -> OCRResult:
    """Translate crop-local OCR boxes back into screenshot coordinates."""
    spans = [
        OCRSpan(
            text=s.text,
            x=s.x + x_offset,
            y=s.y + y_offset,
            w=s.w,
            h=s.h,
            confidence=s.confidence,
            line_id=s.line_id,
        )
        for s in ocr.spans
    ]
    return OCRResult(spans=spans, backend=ocr.backend, image_width=image_width, image_height=image_height, scale=ocr.scale)


def _ocr_crop_global(engine: Any, image_path: str | Path, box: tuple[float, float, float, float], *, scale: float | None = None) -> OCRResult:
    """OCR a screenshot crop and return boxes in the original screenshot coordinate space."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise OCRError("Targeted screenshot retry requires Pillow. Run .\\setup_ocr.ps1.") from exc

    source = Path(image_path)
    with Image.open(source).convert("RGB") as image:
        width, height = image.size
        x0, y0, x1, y1 = box
        left = max(0, min(width - 1, int(math.floor(x0))))
        top = max(0, min(height - 1, int(math.floor(y0))))
        right = max(left + 1, min(width, int(math.ceil(x1))))
        bottom = max(top + 1, min(height, int(math.ceil(y1))))
        crop = image.crop((left, top, right, bottom))
        with tempfile.TemporaryDirectory(prefix="hc_retry_") as td:
            path = Path(td) / "crop.png"
            crop.save(path)
            local = engine.recognize(path, scale=scale)
    return _offset_ocr_result(local, left, top, image_width=width, image_height=height)


def _merge_recipe_rows(primary: list[dict[str, Any]], extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge alternate OCR reads of the same physical recipe row, preferring stronger reads."""
    items = list(primary) + list(extra)
    items.sort(key=lambda r: (r["source_image"], r["bbox"][1], r["bbox"][0], -r["confidence"]))
    merged: list[dict[str, Any]] = []
    for item in items:
        cy = item["bbox"][1] + item["bbox"][3] / 2.0
        conflict = None
        for i in range(len(merged) - 1, -1, -1):
            prev = merged[i]
            if prev["source_image"] != item["source_image"]:
                break
            pcy = prev["bbox"][1] + prev["bbox"][3] / 2.0
            if abs(cy - pcy) <= 12.0:
                conflict = i
                break
            if pcy < cy - 14.0:
                break
        if conflict is None:
            merged.append(item)
            continue
        prev = merged[conflict]
        # Strong full-row recovery should replace a fragment. Confidence is primary;
        # raw-text length breaks close ties because it usually means more of the row was read.
        rank_item = (float(item.get("confidence", 0)), len(item.get("raw_text") or ""))
        rank_prev = (float(prev.get("confidence", 0)), len(prev.get("raw_text") or ""))
        if rank_item > rank_prev:
            merged[conflict] = item
    merged.sort(key=lambda r: (r["source_image"], r["bbox"][1], r["bbox"][0]))
    return merged


def _recipe_retry_regions(ocr: OCRResult, found: list[dict[str, Any]]) -> list[tuple[float, float, float, float]]:
    """Find row-sized areas worth a higher-resolution OCR retry without assuming fixed UI coordinates."""
    width, height = float(ocr.image_width), float(ocr.image_height)
    regions: list[tuple[float, float, float, float]] = []
    lines = cluster_visual_lines(ocr.spans)

    # Retry OCR fragments that structurally look like recipe rows but either were not
    # recognized or produced a low-confidence match.
    found_centers = [r["bbox"][1] + r["bbox"][3] / 2.0 for r in found]
    for line in lines:
        text = _line_text(line).lower()
        if not any(marker in text for marker in ("recipe", "level", "superior")):
            continue
        x, y, w, h = _rect_for(line)
        cy = y + h / 2.0
        covering = [r for r in found if abs((r["bbox"][1] + r["bbox"][3] / 2.0) - cy) <= 9.0]
        if not covering:
            pad_y = max(8.0, h * 0.9)
            regions.append((0.0, y - pad_y, width, y + h + pad_y))

    # A tightly cropped screenshot can cause the OCR detector to skip the first row.
    # Infer likely missing edge/gap rows from the spacing of successfully recognized rows.
    if found_centers:
        centers = sorted(found_centers)
        diffs = [b - a for a, b in zip(centers, centers[1:]) if 10.0 <= b - a <= 80.0]
        spacing = sorted(diffs)[len(diffs) // 2] if diffs else 28.0
        if centers[0] > max(17.0, spacing * 0.72):
            regions.append((0.0, 0.0, width, min(height, centers[0] + spacing * 0.55)))
        for a, b in zip(centers, centers[1:]):
            if b - a > spacing * 1.62:
                regions.append((0.0, max(0.0, a + spacing * 0.35), width, min(height, b - spacing * 0.35)))

    # Always retry explicitly low-confidence rows with more horizontal context.
    for item in found:
        if not item.get("needs_review"):
            continue
        _, y, _, h = item["bbox"]
        regions.append((0.0, y - max(8.0, h * 0.8), width, y + h + max(8.0, h * 0.8)))

    # Deduplicate strongly overlapping y-bands.
    normalized: list[tuple[float, float, float, float]] = []
    for x0, y0, x1, y1 in sorted(regions, key=lambda b: (b[1], b[3])):
        y0, y1 = max(0.0, y0), min(height, y1)
        if y1 - y0 < 8:
            continue
        if normalized:
            px0, py0, px1, py1 = normalized[-1]
            overlap = max(0.0, min(py1, y1) - max(py0, y0))
            smaller = min(py1 - py0, y1 - y0)
            if smaller > 0 and overlap / smaller >= 0.65:
                normalized[-1] = (0.0, min(py0, y0), width, max(py1, y1))
                continue
        normalized.append((0.0, y0, width, y1))
    return normalized[:8]


def _cluster_axis(values: list[float], tolerance: float) -> list[float]:
    if not values:
        return []
    groups: list[list[float]] = []
    for value in sorted(values):
        if not groups or value - (sum(groups[-1]) / len(groups[-1])) > tolerance:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [sum(group) / len(group) for group in groups]


def _salvage_grid_geometry(items: list[dict[str, Any]]) -> tuple[list[float], list[float], float, float]:
    """Infer row/column centers and spacing from recognized salvage labels."""
    if not items:
        return [], [], 90.0, 90.0
    heights = [max(1.0, float(item["bbox"][3])) for item in items]
    median_h = sorted(heights)[len(heights) // 2]
    xs = [float(item["bbox"][0]) + float(item["bbox"][2]) / 2.0 for item in items]
    ys = [float(item["bbox"][1]) + float(item["bbox"][3]) / 2.0 for item in items]
    row_centers = _cluster_axis(ys, max(18.0, median_h * 2.8))
    col_centers = _cluster_axis(xs, max(20.0, median_h * 3.2))
    row_gaps = [b - a for a, b in zip(row_centers, row_centers[1:]) if b - a > median_h * 3.0]
    col_gaps = [b - a for a, b in zip(col_centers, col_centers[1:]) if b - a > median_h * 3.0]
    row_spacing = sorted(row_gaps)[len(row_gaps) // 2] if row_gaps else max(70.0, median_h * 8.5)
    col_spacing = sorted(col_gaps)[len(col_gaps) // 2] if col_gaps else max(70.0, median_h * 8.5)
    return row_centers, col_centers, row_spacing, col_spacing


def _salvage_quantity_crop(
    item: dict[str, Any],
    *,
    image_width: int,
    image_height: int,
    row_centers: list[float] | None = None,
    col_centers: list[float] | None = None,
    row_spacing: float | None = None,
    col_spacing: float | None = None,
) -> tuple[float, float, float, float]:
    """Return a tight, scale-independent search region where a stack number should appear.

    The inventory window itself can be moved/resized/recolored, but inside each salvage tile
    the count is drawn just above and left of the icon, while the label is centered below it.
    Use the detected label height as the scale reference so this survives UI scaling without
    depending on absolute screenshot coordinates or color.
    """
    x, y, w, h = [float(v) for v in item["bbox"]]
    label_cx = x + w / 2.0
    label_cy = y + h / 2.0
    row_spacing = float(row_spacing or max(70.0, h * 8.5))
    col_spacing = float(col_spacing or max(70.0, h * 8.5))
    row_center = min(row_centers or [label_cy], key=lambda v: abs(v - label_cy))
    col_center = min(col_centers or [label_cx], key=lambda v: abs(v - label_cx))
    qty_cx = col_center - col_spacing * 0.27
    qty_cy = row_center - row_spacing * 0.56
    half_w = max(10.0, col_spacing * 0.13)
    half_h = max(6.0, row_spacing * 0.09)
    left, right = qty_cx - half_w, qty_cx + half_w
    top, bottom = qty_cy - half_h, qty_cy + half_h
    return (
        max(0.0, left), max(0.0, top), min(float(image_width), right), min(float(image_height), bottom)
    )


def _targeted_quantities_batch(
    engine: Any,
    image: str | Path,
    items: list[dict[str, Any]],
    *,
    base_scale: float,
    image_width: int,
    image_height: int,
) -> dict[int, tuple[int, float, list[dict[str, Any]]]]:
    """Recover many tiny stack counts with one OCR pass over a crop contact sheet.

    Running a full OCR inference per salvage item is far too slow. Instead, crop the
    number/icon area for every unresolved label, enlarge those crops, place them in a
    neutral contact sheet, and OCR the sheet once. Tile boundaries map numbers back to
    their salvage entries without relying on the original inventory grid coordinates.
    """
    if not items:
        return {}
    try:
        from PIL import Image, ImageStat
    except ImportError as exc:
        raise OCRError("Targeted screenshot retry requires Pillow. Run .\\setup_ocr.ps1.") from exc

    source = Path(image)
    with Image.open(source).convert("RGB") as base:
        row_centers, col_centers, row_spacing, col_spacing = _salvage_grid_geometry(items)
        crops: list[tuple[int, Any]] = []
        for index, item in enumerate(items):
            x0, y0, x1, y1 = _salvage_quantity_crop(
                item,
                image_width=image_width,
                image_height=image_height,
                row_centers=row_centers,
                col_centers=col_centers,
                row_spacing=row_spacing,
                col_spacing=col_spacing,
            )
            left = max(0, min(base.width - 1, int(math.floor(x0))))
            top = max(0, min(base.height - 1, int(math.floor(y0))))
            right = max(left + 1, min(base.width, int(math.ceil(x1))))
            bottom = max(top + 1, min(base.height, int(math.ceil(y1))))
            crop = base.crop((left, top, right, bottom))
            # Pre-enlarge before sheet assembly. The crop contains almost nothing except the
            # count itself, so aggressive enlargement is cheap and helps OCR detectors that
            # otherwise ignore a 5-7 px standalone glyph.
            crop = crop.resize((max(1, crop.width * 4), max(1, crop.height * 4)), Image.Resampling.LANCZOS)
            crops.append((index, crop))

        pad = 18
        tile_w = max(c.width for _, c in crops) + pad * 2
        tile_h = max(c.height for _, c in crops) + pad * 2
        cols = min(4, max(1, math.ceil(math.sqrt(len(crops)))))
        rows = math.ceil(len(crops) / cols)

        # Use the screenshot's average edge tone instead of assuming a light/dark UI.
        edge = base.crop((0, 0, min(base.width, 12), min(base.height, 12)))
        mean = ImageStat.Stat(edge).mean[:3]
        fill = tuple(max(0, min(255, int(round(v)))) for v in mean)
        sheet = Image.new("RGB", (cols * tile_w, rows * tile_h), fill)
        tile_for_index: dict[int, tuple[int, int]] = {}
        for ordinal, (index, crop) in enumerate(crops):
            row, col = divmod(ordinal, cols)
            ox, oy = col * tile_w + pad, row * tile_h + pad
            sheet.paste(crop, (ox, oy))
            tile_for_index[index] = (row, col)

        with tempfile.TemporaryDirectory(prefix="hc_qty_sheet_") as td:
            path = Path(td) / "quantity_sheet.png"
            sheet.save(path)
            # The crops were already enlarged 4x; a moderate second upscale is enough and
            # keeps the contact sheet below OCR engine size limits.
            retry_scale = max(2.0, min(3.0, base_scale * 0.75))
            result = engine.recognize(path, scale=retry_scale)

    per_tile: dict[tuple[int, int], list[OCRSpan]] = {}
    for span in result.spans:
        qty = _parse_int_token(span.text)
        if qty is None or qty <= 0 or qty > 999:
            continue
        col = int(max(0.0, span.cx) // tile_w)
        row = int(max(0.0, span.cy) // tile_h)
        if 0 <= col < cols and 0 <= row < rows:
            per_tile.setdefault((row, col), []).append(span)

    recovered: dict[int, tuple[int, float, list[dict[str, Any]]]] = {}
    for index, tile in tile_for_index.items():
        spans = per_tile.get(tile, [])
        if not spans:
            continue
        # Within a deliberately tight number/icon crop there should normally be one integer.
        # Prefer OCR confidence, then smaller glyph area (capacity/header-like text is larger).
        spans.sort(key=lambda s: (-s.confidence, s.w * s.h, s.cy, s.cx))
        best = spans[0]
        qty = _parse_int_token(best.text)
        if qty is None:
            continue
        debug = [
            {
                "quantity": _parse_int_token(s.text),
                "confidence": round(s.confidence, 4),
                "bbox": [round(s.x, 1), round(s.y, 1), round(s.w, 1), round(s.h, 1)],
            }
            for s in spans[:5]
        ]
        recovered[index] = (qty, float(best.confidence), debug)
    return recovered


def _targeted_quantities_direct(
    engine: Any,
    image: str | Path,
    items: list[dict[str, Any]],
    *,
    image_width: int,
    image_height: int,
    geometry_items: list[dict[str, Any]] | None = None,
) -> dict[int, tuple[int, float, list[dict[str, Any]], bool, int]]:
    """Read unresolved stack counts from tight per-item crops with recognition-only OCR.

    Returns index -> (quantity, confidence, debug candidates, disagreement_flag, support_count).
    """
    if not items or not hasattr(engine, "recognize_single_line"):
        return {}
    try:
        from PIL import Image, ImageEnhance, ImageOps
    except ImportError as exc:
        raise OCRError("Targeted screenshot retry requires Pillow. Run .\\setup_ocr.ps1.") from exc

    source = Path(image)
    with Image.open(source).convert("RGB") as base:
        row_centers, col_centers, row_spacing, col_spacing = _salvage_grid_geometry(geometry_items or items)
        results: dict[int, tuple[int, float, list[dict[str, Any]], bool, int]] = {}
        with tempfile.TemporaryDirectory(prefix="hc_qty_direct_") as td:
            root = Path(td)
            for index, item in enumerate(items):
                x0, y0, x1, y1 = _salvage_quantity_crop(
                    item,
                    image_width=image_width,
                    image_height=image_height,
                    row_centers=row_centers,
                    col_centers=col_centers,
                    row_spacing=row_spacing,
                    col_spacing=col_spacing,
                )
                left = max(0, min(base.width - 1, int(math.floor(x0))))
                top = max(0, min(base.height - 1, int(math.floor(y0))))
                right = max(left + 1, min(base.width, int(math.ceil(x1))))
                bottom = max(top + 1, min(base.height, int(math.ceil(y1))))
                crop = base.crop((left, top, right, bottom))
                # Normalize every crop to a generously sized single-line image. This bypasses
                # the full-page text detector and feeds the recognizer the exact area where the
                # count glyph is expected.
                target_w = max(180, crop.width * 8)
                target_h = max(120, crop.height * 8)
                original = crop.resize((target_w, target_h), Image.Resampling.LANCZOS)
                original = ImageEnhance.Sharpness(original).enhance(1.8)
                gray = ImageOps.autocontrast(ImageOps.grayscale(original)).convert("RGB")
                inverted = ImageOps.invert(ImageOps.grayscale(original)).convert("RGB")
                variants = [("original", original), ("gray", gray), ("inverted", inverted)]

                observations: list[tuple[int, float, str, str]] = []
                debug: list[dict[str, Any]] = []
                for variant_name, variant in variants:
                    path = root / f"{index}_{variant_name}.png"
                    variant.save(path)
                    text, confidence = engine.recognize_single_line(path, digits_only=True)
                    qty = _parse_quantity_text(text)
                    debug.append({
                        "variant": variant_name,
                        "raw_text": text,
                        "quantity": qty,
                        "confidence": round(float(confidence), 4),
                    })
                    if qty is not None:
                        observations.append((qty, float(confidence), variant_name, text))
                if not observations:
                    continue

                grouped: dict[int, list[float]] = {}
                for qty, conf, _, _ in observations:
                    grouped.setdefault(qty, []).append(conf)
                ranked = sorted(
                    grouped.items(),
                    key=lambda kv: (-len(kv[1]), -max(kv[1]), -sum(kv[1]) / len(kv[1]), kv[0]),
                )
                chosen, confs = ranked[0]
                disagreement = len(grouped) > 1
                confidence = max(confs)
                # Two agreeing preprocessing variants are much stronger evidence than one.
                if len(confs) >= 2:
                    confidence = min(1.0, 0.08 + max(confs))
                results[index] = (int(chosen), float(confidence), debug, disagreement, len(confs))
    return results


@dataclass(slots=True)
class VocabEntry:
    name: str
    min_level: int | None = None
    max_level: int | None = None
    recipe_type: str | None = None
    rarity: str | None = None


class ScreenshotRecognizer:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise OCRError(f"Recipe database does not exist: {self.db_path}")
        uri = f"file:{self.db_path.resolve().as_posix()}?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True)
        self.conn.row_factory = sqlite3.Row
        self.recipes = [
            VocabEntry(r["name"], int(r["min_level"]), int(r["max_level"]), r["recipe_type"], r["recipe_rarity"])
            for r in self.conn.execute("SELECT name,min_level,max_level,recipe_type,recipe_rarity FROM recipes ORDER BY name")
        ]
        self.salvage = [
            VocabEntry(r["name"], rarity=r["rarity"])
            for r in self.conn.execute("SELECT name,rarity FROM salvage ORDER BY name")
        ]
        self.recipe_levels: dict[str, list[int]] = {
            r["name"]: [
                int(x[0]) for x in self.conn.execute(
                    "SELECT rl.level FROM recipe_levels rl JOIN recipes r ON r.id=rl.recipe_id WHERE r.name=? ORDER BY rl.level",
                    (r["name"],),
                )
            ]
            for r in self.conn.execute("SELECT name FROM recipes")
        }

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ScreenshotRecognizer":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @staticmethod
    def _top_matches(text: str, entries: list[VocabEntry], limit: int = 3) -> list[tuple[VocabEntry, float]]:
        scored = [(entry, _name_score(text, entry.name)) for entry in entries]
        scored.sort(key=lambda x: (-x[1], x[0].name))
        return scored[:limit]

    def recognize_recipes(self, ocr: OCRResult, image: str | Path) -> list[dict[str, Any]]:
        lines = cluster_visual_lines(ocr.spans)
        if not lines:
            return []
        median_h = sorted(max(s.h for s in line) for line in lines)[len(lines) // 2]
        # Merge obvious wrapped continuation lines such as a trailing [Level 50].
        merged: list[list[OCRSpan]] = []
        for line in lines:
            text = _line_text(line)
            if merged:
                prev = merged[-1]
                ptext = _line_text(prev)
                gap = min(s.y for s in line) - max(s.y2 for s in prev)
                current_is_continuation_shape = ":" not in text and "recipe" not in text.lower()
                continuation = (
                    gap <= max(4.0, median_h * 1.5)
                    and current_is_continuation_shape
                    and (
                        ("recipe" in ptext.lower() and _parse_level(ptext) is None and _parse_level(text) is not None)
                        or _parse_level(text) is not None
                    )
                )
                if continuation:
                    merged[-1] = prev + line
                    continue
            merged.append(line)

        results: list[dict[str, Any]] = []
        for line in merged:
            raw = _spans_text(line)
            # Header/tab text cannot be a recipe. A colon is an excellent structural clue,
            # but allow Common IO-like text or very strong fuzzy matches too.
            stripped = raw
            quantity = None
            qmatch = _LEADING_QTY.match(stripped)
            if qmatch:
                parsed_q = int(qmatch.group(1))
                if parsed_q > 0:
                    quantity = parsed_q
                stripped = qmatch.group(2)
            elif line:
                # Quantity may be a separate OCR word at the far left.
                first = min(line, key=lambda s: s.x)
                q = _parse_int_token(first.text)
                if q is not None:
                    if q > 0:
                        quantity = q
                    stripped = raw.replace(first.text, "", 1).strip()
            matches = self._top_matches(stripped, self.recipes, 3)
            if not matches:
                continue
            best, score = matches[0]
            if score < 0.48:
                continue
            if ":" not in raw and "recipe" not in raw.lower() and score < 0.78:
                continue
            level = _parse_level(raw)
            levels = self.recipe_levels.get(best.name, [])
            level_source = "ocr"
            if level not in levels:
                if len(levels) == 1:
                    level = levels[0]
                    level_source = "database_fixed_level"
                else:
                    level_source = "unresolved"
                    level = None
            q_source = "ocr"
            if quantity is None:
                quantity = 1
                q_source = "default_needs_review"
            margin = score - (matches[1][1] if len(matches) > 1 else 0.0)
            ocr_conf = _line_conf(line)
            confidence = max(0.0, min(1.0, 0.72 * score + 0.18 * max(0.0, min(1.0, margin * 3.0)) + 0.10 * ocr_conf))
            needs_review = confidence < 0.82 or q_source != "ocr" or level is None
            selected = not needs_review
            x, y, w, h = _rect_for(line)
            results.append({
                "selected": selected,
                "recipe": best.name,
                "quantity": int(quantity),
                "level": level,
                "option_index": None,
                "confidence": round(confidence, 4),
                "needs_review": bool(needs_review),
                "quantity_source": q_source,
                "level_source": level_source,
                "raw_text": raw,
                "source_image": str(image),
                "bbox": [round(x, 1), round(y, 1), round(w, 1), round(h, 1)],
                "candidates": [
                    {"recipe": e.name, "score": round(sc, 4)} for e, sc in matches
                ],
            })
        # Keep screen order and drop accidental duplicate OCR lines occupying essentially the same row.
        results.sort(key=lambda r: (r["source_image"], r["bbox"][1], r["bbox"][0]))
        dedup: list[dict[str, Any]] = []
        for item in results:
            icy = item["bbox"][1] + item["bbox"][3] / 2.0
            conflict_index = None
            for i in range(len(dedup) - 1, -1, -1):
                prev = dedup[i]
                if prev["source_image"] != item["source_image"]:
                    break
                pcy = prev["bbox"][1] + prev["bbox"][3] / 2.0
                # Multiple OCR passes may recognize the same UI row differently. The CoH
                # recipe rows in our regression images are ~28 px apart, so an 8 px center
                # tolerance safely merges alternate reads without merging adjacent rows.
                if abs(icy - pcy) <= 8.0:
                    conflict_index = i
                    break
                if pcy < icy - 12.0:
                    break
            if conflict_index is not None:
                prev = dedup[conflict_index]
                if item["confidence"] > prev["confidence"]:
                    dedup[conflict_index] = item
                continue
            dedup.append(item)
        dedup.sort(key=lambda r: (r["source_image"], r["bbox"][1], r["bbox"][0]))
        return dedup

    def recognize_salvage(self, ocr: OCRResult, image: str | Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        lines = cluster_visual_lines(ocr.spans)
        numeric_spans: list[OCRSpan] = []
        for span in ocr.spans:
            if _parse_int_token(span.text) is not None:
                numeric_spans.append(span)

        # Generate sliding word phrases within each visual line. This works whether the OCR
        # backend returns words or short line regions, and does not depend on a fixed grid.
        candidates: list[dict[str, Any]] = []
        for line in lines:
            words = [s for s in sorted(line, key=lambda s: s.x) if any(ch.isalpha() for ch in s.text)]
            if not words:
                continue
            for start in range(len(words)):
                for size in range(1, min(5, len(words) - start) + 1):
                    chunk = words[start:start + size]
                    # Do not bridge huge horizontal gaps between unrelated grid cells.
                    if len(chunk) > 1:
                        gaps = [chunk[i + 1].x - chunk[i].x2 for i in range(len(chunk) - 1)]
                        typical_h = max(1.0, sum(s.h for s in chunk) / len(chunk))
                        if max(gaps) > typical_h * 3.0:
                            break
                    phrase = " ".join(s.text for s in chunk)
                    match = self._top_matches(phrase, self.salvage, 2)
                    if not match:
                        continue
                    best, score = match[0]
                    if score < 0.66:
                        continue
                    x, y, w, h = _rect_for(chunk)
                    margin = score - (match[1][1] if len(match) > 1 else 0.0)
                    candidates.append({
                        "salvage": best.name,
                        "rarity": best.rarity,
                        "name_score": score,
                        "margin": margin,
                        "name_ocr_conf": _line_conf(chunk),
                        "raw_text": phrase,
                        "bbox": (x, y, w, h),
                        "source_image": str(image),
                    })

        # Non-maximum suppression: prefer the strongest phrase for each physical name label,
        # while allowing the same salvage to appear in separate screenshots.
        candidates.sort(key=lambda c: (-c["name_score"], -c["margin"], -c["bbox"][2]))
        chosen: list[dict[str, Any]] = []
        for c in candidates:
            cx = c["bbox"][0] + c["bbox"][2] / 2
            cy = c["bbox"][1] + c["bbox"][3] / 2
            conflict = False
            for p in chosen:
                px = p["bbox"][0] + p["bbox"][2] / 2
                py = p["bbox"][1] + p["bbox"][3] / 2
                same_region = abs(cx - px) < max(c["bbox"][2], p["bbox"][2]) * 0.55 and abs(cy - py) < max(c["bbox"][3], p["bbox"][3]) * 1.4
                same_salvage_near = c["salvage"] == p["salvage"] and abs(cy - py) < max(c["bbox"][3], p["bbox"][3]) * 2
                if same_region or same_salvage_near:
                    conflict = True
                    break
            if not conflict:
                chosen.append(c)

        results: list[dict[str, Any]] = []
        for c in chosen:
            x, y, w, h = c["bbox"]
            # Quantity sits near/above its name, but exact icon/layout geometry can vary.
            # Use relative distances based on the detected name's own height.
            plausible: list[tuple[float, OCRSpan, int]] = []
            for n in numeric_spans:
                qty = _parse_int_token(n.text)
                if qty is None or qty < 0 or qty > 999:
                    continue
                vertical = y - n.cy
                if vertical < -h * 0.5 or vertical > max(90.0, h * 8.5):
                    continue
                horizontal = abs(n.cx - (x + w * 0.25))
                if horizontal > max(w * 0.8, h * 5.0):
                    continue
                # Favor a number directly above/within the label column.
                distance = vertical * 0.8 + horizontal
                plausible.append((distance, n, qty))
            plausible.sort(key=lambda t: t[0])
            qty = plausible[0][2] if plausible else None
            qty_conf = plausible[0][1].confidence if plausible else 0.0
            quantity_source = "ocr" if plausible else "unresolved"
            confidence = max(0.0, min(1.0, 0.70 * c["name_score"] + 0.15 * max(0.0, min(1.0, c["margin"] * 4)) + 0.10 * c["name_ocr_conf"] + 0.05 * qty_conf))
            # A partial salvage label can still be unambiguous against the finite 108-item
            # vocabulary (e.g. "Conspiracy" -> Magical Conspiracy). Treat a large top-match
            # margin as strong identity evidence instead of forcing review solely because the
            # raw fuzzy score is below 0.80. Quantity confidence is handled independently.
            unique_name = bool(c["name_score"] >= 0.68 and c["margin"] >= 0.35)
            name_needs_review = bool(confidence < 0.80 and not unique_name)
            results.append({
                "salvage": c["salvage"],
                "rarity": c["rarity"],
                "quantity": qty,
                "confidence": round(confidence, 4),
                "name_score": round(float(c["name_score"]), 4),
                "name_margin": round(float(c["margin"]), 4),
                "needs_review": bool(qty is None or name_needs_review),
                "quantity_source": quantity_source,
                "raw_text": c["raw_text"],
                "source_image": c["source_image"],
                "bbox": [round(v, 1) for v in c["bbox"]],
            })
        results.sort(key=lambda r: (r["source_image"], r["bbox"][1], r["bbox"][0]))

        # Capacity candidates come from OCR words/lines containing fractions. Preserve all
        # candidates instead of silently trusting one potentially misread digit.
        capacity_candidates: list[dict[str, Any]] = []
        for line in lines:
            text = _line_text(line)
            for m in _FRACTION.finditer(text):
                used, cap = int(m.group(1)), int(m.group(2))
                if cap <= 0 or used > cap:
                    continue
                conf = _line_conf(line)
                capacity_candidates.append({
                    "used": used,
                    "capacity": cap,
                    "confidence": round(conf, 4),
                    "raw_text": text,
                    "source_image": str(image),
                })
        return results, capacity_candidates


def _aggregate_salvage(items: list[dict[str, Any]]) -> tuple[dict[str, int], list[dict[str, Any]]]:
    totals: Counter[str] = Counter()
    unresolved: list[dict[str, Any]] = []
    for item in items:
        qty = item.get("quantity")
        if qty is None:
            unresolved.append(item)
            continue
        totals[item["salvage"]] += int(qty)
    return dict(sorted(totals.items())), unresolved


def scan_screenshots(
    db_path: str | Path,
    *,
    recipe_images: Iterable[str | Path] = (),
    salvage_images: Iterable[str | Path] = (),
    backend: str = "auto",
    scale: float | None = None,
    progress_callback=None,
) -> dict[str, Any]:
    recipe_images = list(recipe_images)
    salvage_images = list(salvage_images)
    engine = create_backend(backend)
    all_recipes: list[dict[str, Any]] = []
    all_salvage: list[dict[str, Any]] = []
    capacity_candidates: list[dict[str, Any]] = []
    image_debug: list[dict[str, Any]] = []
    total_images = len(recipe_images) + len(salvage_images)
    current_index = 0
    with ScreenshotRecognizer(db_path) as recognizer:
        for path in recipe_images:
            current_index += 1
            if progress_callback is not None:
                progress_callback(current_index, total_images, "recipe", str(path))
            ocr = engine.recognize(path, scale=scale)
            found = recognizer.recognize_recipes(ocr, path)
            retry_regions = _recipe_retry_regions(ocr, found)
            retry_rows: list[dict[str, Any]] = []
            for region in retry_regions[:4]:
                try:
                    retry_ocr = _ocr_crop_global(
                        engine, path, region,
                        scale=max(5.5, min(7.0, ocr.scale * 1.5)),
                    )
                except OCRError:
                    continue
                retry_rows.extend(recognizer.recognize_recipes(retry_ocr, path))
            if retry_rows:
                found = _merge_recipe_rows(found, retry_rows)
            all_recipes.extend(found)
            image_debug.append({
                "image": str(path), "kind": "recipe", "backend": ocr.backend,
                "ocr_spans": len(ocr.spans), "recognized": len(found), "scale": round(ocr.scale, 3),
                "retry_regions": len(retry_regions), "retry_candidates": len(retry_rows),
            })
        for path in salvage_images:
            current_index += 1
            if progress_callback is not None:
                progress_callback(current_index, total_images, "salvage", str(path))
            ocr = engine.recognize(path, scale=scale)
            found, caps = recognizer.recognize_salvage(ocr, path)
            unresolved_items = [item for item in found if item.get("quantity") is None]
            quantity_retries = len(unresolved_items)
            quantity_recovered = 0
            if unresolved_items:
                try:
                    recovered = _targeted_quantities_direct(
                        engine, path, unresolved_items,
                        image_width=ocr.image_width,
                        image_height=ocr.image_height,
                        geometry_items=found,
                    )
                except OCRError:
                    recovered = {}
                for index, item in enumerate(unresolved_items):
                    hit = recovered.get(index)
                    if hit is None:
                        continue
                    qty, qty_conf, qty_debug, disagreement, support_count = hit
                    item["quantity_retry_candidates"] = qty_debug
                    item["quantity_confidence"] = round(qty_conf, 4)
                    item["quantity_disagreement"] = bool(disagreement)
                    item["quantity_support_count"] = int(support_count)
                    # Do not silently promote a weak one-off classification into inventory.
                    # Two independent preprocessing variants that agree are strong evidence;
                    # a single read must be exceptionally confident before it is even prefilled.
                    accepted = (support_count >= 2 and not disagreement) or (
                        support_count == 1 and not disagreement and qty_conf >= 0.92
                    )
                    if not accepted:
                        continue
                    item["quantity"] = int(qty)
                    item["quantity_source"] = "targeted_recognition"
                    unique_name = bool(
                        item.get("name_score", 0.0) >= 0.68
                        and item.get("name_margin", 0.0) >= 0.35
                    )
                    name_needs_review = bool(item.get("confidence", 0.0) < 0.80 and not unique_name)
                    item["needs_review"] = bool(
                        name_needs_review
                        or qty_conf < 0.65
                        or disagreement
                        or support_count < 2
                    )
                    quantity_recovered += 1
            all_salvage.extend(found)
            capacity_candidates.extend(caps)
            image_debug.append({
                "image": str(path), "kind": "salvage", "backend": ocr.backend,
                "ocr_spans": len(ocr.spans), "recognized": len(found),
                "capacity_candidates": len(caps), "scale": round(ocr.scale, 3),
                "quantity_retries": quantity_retries, "quantity_recovered": quantity_recovered,
            })

    inventory, unresolved_salvage = _aggregate_salvage(all_salvage)
    # Accept capacity automatically only when all highest-confidence candidates agree.
    capacity = None
    capacity_needs_review = True
    if capacity_candidates:
        capacity_candidates.sort(key=lambda c: (-c["confidence"], -c["capacity"], -c["used"]))
        best_conf = capacity_candidates[0]["confidence"]
        top = [c for c in capacity_candidates if c["confidence"] >= best_conf - 0.08]
        pairs = {(c["used"], c["capacity"]) for c in top}
        if len(pairs) == 1 and best_conf >= 0.90:
            used, cap = next(iter(pairs))
            capacity = {"used": used, "capacity": cap, "source": "screenshot_ocr"}
            capacity_needs_review = False

    recipes_payload = [
        {
            "recipe": r["recipe"], "quantity": r["quantity"], "level": r["level"],
            "option_index": r["option_index"], "selected": r["selected"],
        }
        for r in all_recipes
    ]
    needs_review = (
        any(r["needs_review"] for r in all_recipes)
        or any(s["needs_review"] for s in all_salvage)
        or capacity_needs_review
    )
    return {
        "schema_version": 1,
        "confirmed": False,
        "needs_review": needs_review,
        "ocr_backend": getattr(engine, "name", backend),
        "recipes": recipes_payload,
        "inventory": inventory,
        "salvage_capacity": capacity,
        "disposal_policy": {"allowed_rarities": ["common"]},
        "recognition": {
            "recipes": all_recipes,
            "salvage": all_salvage,
            "unresolved_salvage_quantities": unresolved_salvage,
            "capacity_candidates": capacity_candidates,
            "capacity_needs_review": capacity_needs_review,
            "images": image_debug,
        },
    }


def calculator_payload_from_review(review: dict[str, Any], *, require_confirmed: bool = True) -> dict[str, Any]:
    if require_confirmed and not review.get("confirmed"):
        raise CalculationError(
            "Recognition review is not confirmed. Inspect/edit the scan JSON, set \"confirmed\": true, then calculate."
        )
    recipes = []
    for item in review.get("recipes") or []:
        if not isinstance(item, dict) or not item.get("selected", True):
            continue
        recipes.append({k: item.get(k) for k in ("recipe", "quantity", "level", "option_index") if item.get(k) is not None})
    if not recipes:
        raise CalculationError("No selected recipes remain in the recognition review")
    inventory = review.get("inventory") or {}
    # Manual user edits to the top-level normalized inventory/capacity intentionally take
    # precedence over raw recognition metadata.
    return {
        "recipes": recipes,
        "inventory": inventory,
        "salvage_capacity": review.get("salvage_capacity"),
        "disposal_policy": review.get("disposal_policy") or {"allowed_rarities": ["common"]},
    }


def calculate_review(db_path: str | Path, review: dict[str, Any], *, require_confirmed: bool = True) -> dict[str, Any]:
    return calculate_payload(db_path, calculator_payload_from_review(review, require_confirmed=require_confirmed))


def load_review(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalculationError(f"Could not read recognition review {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CalculationError("Recognition review JSON must contain one top-level object")
    return value
