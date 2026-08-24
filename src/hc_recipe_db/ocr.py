from __future__ import annotations

import csv
import io
import math
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


class OCRError(RuntimeError):
    pass


@dataclass(slots=True)
class OCRSpan:
    text: str
    x: float
    y: float
    w: float
    h: float
    confidence: float = 1.0
    line_id: str | None = None

    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y2(self) -> float:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class OCRResult:
    spans: list[OCRSpan]
    backend: str
    image_width: int
    image_height: int
    scale: float

    @property
    def text(self) -> str:
        return "\n".join(s.text for s in self.spans if s.text.strip())


def _adaptive_scale(width: int, height: int, target_max_dimension: int = 2400) -> float:
    if width <= 0 or height <= 0:
        return 1.0
    longest = max(width, height)
    # The CoH inventory font is very small in cropped screenshots. A stable 4x upscale
    # worked better than chasing a precise target size in regression screenshots.
    if longest <= 800:
        return 4.0
    if longest <= 1200:
        return 2.5
    if longest >= target_max_dimension:
        return 1.0
    return max(1.0, min(2.0, target_max_dimension / longest))


def _prepare_image(image_path: str | Path, *, scale: float | None = None, sharpness: float = 1.7) -> tuple[Path, int, int, float, tempfile.TemporaryDirectory[str]]:
    try:
        from PIL import Image, ImageEnhance
    except ImportError as exc:
        raise OCRError(
            "Screenshot OCR requires Pillow. Run .\\setup_ocr.ps1 (recommended) or install pillow."
        ) from exc

    source = Path(image_path)
    if not source.exists():
        raise OCRError(f"Screenshot does not exist: {source}")
    try:
        image = Image.open(source).convert("RGB")
    except Exception as exc:
        raise OCRError(f"Could not open screenshot {source}: {exc}") from exc
    original_w, original_h = image.size
    scale = float(scale or _adaptive_scale(original_w, original_h))
    if scale != 1.0:
        image = image.resize(
            (max(1, round(original_w * scale)), max(1, round(original_h * scale))),
            Image.Resampling.LANCZOS,
        )
    # A small sharpening pass helps the tiny outlined CoH UI font while preserving
    # arbitrary user colors/backgrounds. No color thresholding is used.
    image = ImageEnhance.Sharpness(image).enhance(float(sharpness))
    td = tempfile.TemporaryDirectory(prefix="hc_ocr_")
    out = Path(td.name) / "prepared.png"
    image.save(out)
    return out, original_w, original_h, scale, td


def _map_back(span: OCRSpan, scale: float) -> OCRSpan:
    if scale == 1.0:
        return span
    return OCRSpan(
        text=span.text,
        x=span.x / scale,
        y=span.y / scale,
        w=span.w / scale,
        h=span.h / scale,
        confidence=span.confidence,
        line_id=span.line_id,
    )


def _map_back_padded(span: OCRSpan, scale: float, pad_scaled: float) -> OCRSpan:
    """Map a span from a padded/upscaled image back to original screenshot coordinates."""
    if scale <= 0:
        scale = 1.0
    return OCRSpan(
        text=span.text,
        x=(span.x - pad_scaled) / scale,
        y=(span.y - pad_scaled) / scale,
        w=span.w / scale,
        h=span.h / scale,
        confidence=span.confidence,
        line_id=span.line_id,
    )


class TesseractBackend:
    name = "tesseract"

    def __init__(self, executable: str | None = None, *, psm: int = 6):
        self.executable = executable or shutil.which("tesseract")
        self.psm = int(psm)
        if not self.executable:
            raise OCRError(
                "Tesseract was requested but tesseract.exe was not found on PATH. "
                "Use the RapidOCR backend via .\\setup_ocr.ps1, or install Tesseract."
            )

    def recognize(self, image_path: str | Path, *, scale: float | None = None) -> OCRResult:
        prepared, width, height, actual_scale, td = _prepare_image(image_path, scale=scale)
        try:
            proc = subprocess.run(
                [self.executable, str(prepared), "stdout", "--psm", str(self.psm), "tsv"],
                capture_output=True,
                text=True,
                check=False,
                encoding="utf-8",
                errors="replace",
            )
            if proc.returncode != 0:
                raise OCRError(f"Tesseract failed for {image_path}: {proc.stderr.strip()}")
            reader = csv.DictReader(io.StringIO(proc.stdout), delimiter="\t")
            spans: list[OCRSpan] = []
            for row in reader:
                text = (row.get("text") or "").strip()
                if not text:
                    continue
                try:
                    conf_raw = float(row.get("conf") or -1)
                    if conf_raw < 0:
                        continue
                    span = OCRSpan(
                        text=text,
                        x=float(row["left"]),
                        y=float(row["top"]),
                        w=float(row["width"]),
                        h=float(row["height"]),
                        confidence=max(0.0, min(1.0, conf_raw / 100.0)),
                        line_id=f"t:{row.get('page_num','')}:{row.get('block_num','')}:{row.get('par_num','')}:{row.get('line_num','')}",
                    )
                except (TypeError, ValueError, KeyError):
                    continue
                spans.append(_map_back(span, actual_scale))

            # Sparse-text mode recovers some rows that Tesseract's block-oriented mode can
            # skip entirely (especially when translucent UI rows overlap a busy game scene).
            # Keep it as a complementary pass and let the recognizer deduplicate by geometry.
            if self.psm != 11:
                sparse_proc = subprocess.run(
                    [self.executable, str(prepared), "stdout", "--psm", "11", "tsv"],
                    capture_output=True, text=True, check=False, encoding="utf-8", errors="replace",
                )
                if sparse_proc.returncode == 0:
                    sreader = csv.DictReader(io.StringIO(sparse_proc.stdout), delimiter="\t")
                    for row in sreader:
                        text = (row.get("text") or "").strip()
                        if not text or not any(ch.isalpha() for ch in text):
                            continue
                        try:
                            conf_raw = float(row.get("conf") or -1)
                            if conf_raw < 0:
                                continue
                            candidate = _map_back(OCRSpan(
                                text=text, x=float(row["left"]), y=float(row["top"]),
                                w=float(row["width"]), h=float(row["height"]),
                                confidence=max(0.0, min(1.0, conf_raw / 100.0)),
                                line_id=f"ts:{row.get('page_num','')}:{row.get('block_num','')}:{row.get('par_num','')}:{row.get('line_num','')}",
                            ), actual_scale)
                        except (TypeError, ValueError, KeyError):
                            continue
                        spans.append(candidate)

            # A second digits-only pass is useful for CoH salvage stack counts: the tiny
            # quantity glyphs can be omitted by normal OCR even when nearby names are read
            # perfectly. Keep only pure integers from this pass; fractions are deliberately
            # excluded so a weak digits-only pass cannot silently invent salvage capacity.
            numeric_prepared, _, _, _, numeric_td = _prepare_image(image_path, scale=actual_scale, sharpness=1.0)
            try:
                numeric_proc = subprocess.run(
                    [self.executable, str(numeric_prepared), "stdout", "--psm", "6",
                     "-c", "tessedit_char_whitelist=0123456789/", "tsv"],
                    capture_output=True, text=True, check=False, encoding="utf-8", errors="replace",
                )
            finally:
                numeric_td.cleanup()
            if numeric_proc.returncode == 0:
                nreader = csv.DictReader(io.StringIO(numeric_proc.stdout), delimiter="\t")
                for row in nreader:
                    text = (row.get("text") or "").strip()
                    if not text.isdigit():
                        continue
                    try:
                        conf_raw = float(row.get("conf") or -1)
                        if conf_raw < 0:
                            continue
                        candidate = _map_back(OCRSpan(
                            text=text, x=float(row["left"]), y=float(row["top"]),
                            w=float(row["width"]), h=float(row["height"]),
                            confidence=max(0.0, min(1.0, conf_raw / 100.0)),
                            line_id=f"tn:{row.get('page_num','')}:{row.get('block_num','')}:{row.get('par_num','')}:{row.get('line_num','')}",
                        ), actual_scale)
                    except (TypeError, ValueError, KeyError):
                        continue
                    duplicate = any(
                        s.text == candidate.text
                        and abs(s.cx - candidate.cx) <= max(2.0, candidate.w)
                        and abs(s.cy - candidate.cy) <= max(2.0, candidate.h)
                        for s in spans
                    )
                    if not duplicate:
                        spans.append(candidate)
            return OCRResult(spans=spans, backend=self.name, image_width=width, image_height=height, scale=actual_scale)
        finally:
            td.cleanup()

    def recognize_single_line(self, image_path: str | Path, *, digits_only: bool = False) -> tuple[str, float]:
        """Recognize one already-cropped text line without running page layout detection."""
        cmd = [self.executable, str(image_path), "stdout", "--psm", "10"]
        if digits_only:
            cmd += ["-c", "tessedit_char_whitelist=0123456789"]
        cmd.append("tsv")
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False,
            encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            return "", 0.0
        reader = csv.DictReader(io.StringIO(proc.stdout), delimiter="\t")
        words: list[tuple[int, str, float]] = []
        for row in reader:
            text = (row.get("text") or "").strip()
            if not text:
                continue
            try:
                conf = float(row.get("conf") or -1)
                left = int(float(row.get("left") or 0))
            except (TypeError, ValueError):
                continue
            if conf < 0:
                continue
            words.append((left, text, max(0.0, min(1.0, conf / 100.0))))
        if not words:
            return "", 0.0
        words.sort(key=lambda x: x[0])
        text = " ".join(x[1] for x in words)
        conf = sum(x[2] for x in words) / len(words)
        return text, conf


class RapidOCRBackend:
    name = "rapidocr"

    def __init__(self):
        try:
            from rapidocr import RapidOCR
        except ImportError as exc:
            raise OCRError(
                "RapidOCR is not installed. Run .\\setup_ocr.ps1 once, then retry the scan."
            ) from exc
        try:
            # Current RapidOCR 3.x accepts config overrides through params. The low
            # minimum side length is useful for tiny game UI text after our adaptive upscale.
            self.engine = RapidOCR(params={
                "Global.text_score": 0.35,
                "Global.return_word_box": True,
                "Global.min_height": 20,
                "Global.max_side_len": 4000,
                "Rec.lang_type": "en",
            })
        except TypeError:
            # Compatibility fallback for older RapidOCR releases.
            self.engine = RapidOCR()

    @staticmethod
    def _bbox_to_rect(box: Any) -> tuple[float, float, float, float] | None:
        try:
            pts = list(box)
            xs = [float(p[0]) for p in pts]
            ys = [float(p[1]) for p in pts]
            if not xs or not ys:
                return None
            return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
        except Exception:
            return None

    def recognize(self, image_path: str | Path, *, scale: float | None = None) -> OCRResult:
        prepared, width, height, actual_scale, td = _prepare_image(image_path, scale=scale)
        try:
            # OCR detectors are noticeably less reliable when a text row touches the edge
            # of a screenshot (a common case when users crop the CoH recipe window tightly).
            # Add neutral margin around the prepared image so the detector sees a complete
            # text region. The margin is removed again when boxes are mapped back.
            try:
                from PIL import Image, ImageOps, ImageStat
                pad_scaled = max(16, round(10 * actual_scale))
                with Image.open(prepared).convert("RGB") as base:
                    # Match the screenshot's own edge tone instead of assuming a dark/light
                    # theme. This gives the detector margin without introducing a synthetic
                    # high-contrast frame on customized UIs.
                    edge_w = min(base.width, max(4, pad_scaled // 2))
                    edge_h = min(base.height, max(4, pad_scaled // 2))
                    samples = [
                        base.crop((0, 0, edge_w, edge_h)),
                        base.crop((base.width - edge_w, 0, base.width, edge_h)),
                        base.crop((0, base.height - edge_h, edge_w, base.height)),
                        base.crop((base.width - edge_w, base.height - edge_h, base.width, base.height)),
                    ]
                    means = [ImageStat.Stat(sample).mean[:3] for sample in samples]
                    fill = tuple(int(round(sum(m[i] for m in means) / len(means))) for i in range(3))
                    padded = ImageOps.expand(base, border=pad_scaled, fill=fill)
                    padded_path = Path(td.name) / "prepared_padded.png"
                    padded.save(padded_path)
            except Exception:
                pad_scaled = 0
                padded_path = prepared
            try:
                result = self.engine(
                    str(padded_path), use_det=True, use_cls=False, use_rec=True,
                    return_word_box=True,
                )
            except TypeError:
                result = self.engine(str(padded_path))
            spans: list[OCRSpan] = []

            # Prefer per-word boxes when the current RapidOCR release provides them.
            word_results = getattr(result, "word_results", None)
            if word_results:
                for line_index, line in enumerate(word_results):
                    if not line:
                        continue
                    for item in line:
                        try:
                            text, score, box = item
                        except Exception:
                            continue
                        rect = self._bbox_to_rect(box)
                        if not text or rect is None:
                            continue
                        raw_span = OCRSpan(str(text), *rect, float(score), line_id=f"r:{line_index}")
                        spans.append(_map_back_padded(raw_span, actual_scale, pad_scaled))

            if not spans:
                boxes = getattr(result, "boxes", None)
                txts = getattr(result, "txts", None)
                scores = getattr(result, "scores", None)
                if boxes is not None and txts is not None:
                    if scores is None:
                        scores = [1.0] * len(txts)
                    for line_index, (box, text, score) in enumerate(zip(boxes, txts, scores)):
                        rect = self._bbox_to_rect(box)
                        if not text or rect is None:
                            continue
                        raw_span = OCRSpan(str(text), *rect, float(score), line_id=f"r:{line_index}")
                        spans.append(_map_back_padded(raw_span, actual_scale, pad_scaled))
            return OCRResult(spans=spans, backend=self.name, image_width=width, image_height=height, scale=actual_scale)
        except Exception as exc:
            if isinstance(exc, OCRError):
                raise
            raise OCRError(f"RapidOCR failed for {image_path}: {exc}") from exc
        finally:
            td.cleanup()

    def recognize_single_line(self, image_path: str | Path, *, digits_only: bool = False) -> tuple[str, float]:
        """Use RapidOCR's recognition model directly on an already-cropped single line.

        RapidOCR's own pipeline supports disabling detection; this is valuable for the tiny
        CoH salvage count glyphs because the recognition model can read an isolated crop even
        when the text detector omitted that glyph from the full screenshot.
        """
        try:
            result = self.engine(
                str(image_path), use_det=False, use_cls=False, use_rec=True,
                return_word_box=False, text_score=0.0,
            )
            txts = getattr(result, "txts", None)
            scores = getattr(result, "scores", None)
            if not txts:
                return "", 0.0
            text = str(txts[0]).strip()
            score = float(scores[0]) if scores else 0.0
            return text, max(0.0, min(1.0, score))
        except Exception:
            return "", 0.0
        finally:
            # __call__ updates the engine's live flags, so restore normal full OCR mode for
            # the next screenshot.
            try:
                self.engine.update_params(use_det=True, use_cls=False, use_rec=True)
            except Exception:
                pass


def create_backend(name: str = "auto"):
    value = (name or "auto").strip().lower()
    if value == "rapidocr":
        return RapidOCRBackend()
    if value == "tesseract":
        return TesseractBackend()
    if value != "auto":
        raise OCRError(f"Unknown OCR backend: {name!r}; expected auto, rapidocr, or tesseract")
    try:
        return RapidOCRBackend()
    except OCRError:
        if shutil.which("tesseract"):
            return TesseractBackend()
        raise OCRError(
            "No OCR backend is available. Run .\\setup_ocr.ps1 to install RapidOCR."
        )


def cluster_visual_lines(spans: Iterable[OCRSpan]) -> list[list[OCRSpan]]:
    """Cluster OCR words/regions into visual text lines without fixed coordinates."""
    items = [s for s in spans if s.text.strip() and s.h > 0]
    if not items:
        return []
    if all(s.line_id is not None for s in items):
        grouped: dict[str, list[OCRSpan]] = {}
        for span in items:
            grouped.setdefault(str(span.line_id), []).append(span)
        lines = list(grouped.values())
        for line in lines:
            line.sort(key=lambda s: s.x)
        lines.sort(key=lambda line: (sum(s.cy for s in line) / len(line), min(s.x for s in line)))
        return lines
    items.sort(key=lambda s: (s.cy, s.x))
    median_h = sorted(s.h for s in items)[len(items) // 2]
    tolerance = max(2.0, median_h * 0.65)
    lines: list[list[OCRSpan]] = []
    centers: list[float] = []
    for span in items:
        best = None
        best_distance = math.inf
        for idx, cy in enumerate(centers):
            distance = abs(span.cy - cy)
            if distance <= tolerance and distance < best_distance:
                best = idx
                best_distance = distance
        if best is None:
            lines.append([span])
            centers.append(span.cy)
        else:
            lines[best].append(span)
            centers[best] = sum(s.cy for s in lines[best]) / len(lines[best])
    ordered = sorted(zip(centers, lines), key=lambda x: x[0])
    out: list[list[OCRSpan]] = []
    for _, line in ordered:
        line.sort(key=lambda s: s.x)
        out.append(line)
    return out
