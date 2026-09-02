from __future__ import annotations

import hashlib
import json
import math
import os
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .game_memory import (
    GameProcessInfo,
    ProcessMemory,
    _character_vitals_plausible,
    _rip_relative_target_rva,
    _valid_identity_string,
)
from .memory_profiles import MemoryProfile, MemoryProfileManager, as_int, parse_signature
from .version import APP_VERSION


# FIELD_CRAFTER_MEMORY_STRUCTURAL_DIAGNOSTICS_V6
DIAGNOSTIC_SCHEMA_VERSION = 3
MAX_REPORTED_SIGNATURE_HITS = 32
MAX_REPORTED_LOCATOR_CANDIDATES = 16
MAX_REPORTED_COLLECTION_ENTRIES = 96
RECOVERY_WINDOW = 0x200
RECOVERY_STEP = 0x8
MAX_RECOVERY_RESULTS = 12
MAX_RECOVERY_STRONG_RESULTS = 8
MAX_RECOVERY_AUX_RESULTS = 4

CODE_FINGERPRINT_RADIUS = 32
LANDMARK_MAX_RESULTS = 8
LANDMARK_MAX_ANCHOR_RUNS = 8
LANDMARK_MIN_ANCHOR_RUN = 3
LANDMARK_MAX_ANCHOR_HITS = 2048
LANDMARK_MAX_CANDIDATE_STARTS = 8192
LANDMARK_MAX_MISMATCH_RATIO = 0.30

_INTERNAL_NAME_RE = re.compile(r"^[A-Za-z0-9_.'+\-]{1,191}$")
_RECIPE_COMMON_RE = re.compile(r"^Invention_.+_[0-9]+$")
_RECIPE_SET_RE = re.compile(r"^.+_[A-F]_[0-9]+$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def diagnostic_filename(pid: int, when: datetime | None = None) -> str:
    stamp = (when or _utc_now()).strftime("%Y%m%d_%H%M%S")
    return f"field_crafter_memory_diagnostic_{stamp}_pid{int(pid)}.zip"


def default_diagnostic_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) / "FieldCrafter" if base else Path.home() / ".field_crafter"
    return root / "diagnostics"


def _hex(value: int | None) -> str | None:
    if value is None:
        return None
    try:
        return f"0x{int(value):X}"
    except Exception:
        return None


def _sha256_file(path: str | Path) -> str | None:
    try:
        h = hashlib.sha256()
        with Path(path).open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _safe_call(func, default=None):
    try:
        return func()
    except Exception:
        return default


def _code_context_fingerprint(
    mem: ProcessMemory,
    match_rva: int,
    *,
    radius: int = CODE_FINGERPRINT_RADIUS,
) -> dict[str, Any] | None:
    """Hash a small executable-code window without storing raw code bytes."""
    try:
        for section_rva, data in mem.executable_snapshot():
            relative = int(match_rva) - int(section_rva)
            if not (0 <= relative < len(data)):
                continue
            start = max(0, relative - int(radius))
            end = min(len(data), relative + int(radius))
            blob = data[start:end]
            return {
                "window_start_rva": _hex(section_rva + start),
                "window_length": len(blob),
                "sha256": hashlib.sha256(blob).hexdigest(),
            }
    except Exception:
        return None
    return None


def _fixed_signature_runs(
    pattern: str,
) -> tuple[tuple[int | None, ...], list[tuple[int, bytes]]]:
    parsed = parse_signature(pattern)
    runs: list[tuple[int, bytes]] = []
    index = 0
    while index < len(parsed):
        if parsed[index] is None:
            index += 1
            continue
        start = index
        values: list[int] = []
        while index < len(parsed) and parsed[index] is not None:
            values.append(int(parsed[index]))
            index += 1
        if len(values) >= LANDMARK_MIN_ANCHOR_RUN:
            runs.append((start, bytes(values)))

    # Longer fragments are more selective. Keep distinct offsets so one changed
    # fragment cannot eliminate all landmark recovery opportunities.
    runs.sort(key=lambda item: (-len(item[1]), item[0]))
    return parsed, runs[:LANDMARK_MAX_ANCHOR_RUNS]


def _nearest_masked_landmarks(
    mem: ProcessMemory,
    pattern: str,
) -> dict[str, Any]:
    """Find close masked-signature landmarks when the exact signature has zero hits.

    This is diagnostic-only. Candidates are not executed, resolved, cached, or
    accepted by the memory reader.
    """
    parsed, runs = _fixed_signature_runs(pattern)
    fixed_positions = [
        (index, int(value))
        for index, value in enumerate(parsed)
        if value is not None
    ]
    result: dict[str, Any] = {
        "attempted": True,
        "fixed_byte_count": len(fixed_positions),
        "anchor_run_lengths": [len(blob) for _offset, blob in runs],
        "candidate_count": 0,
        "candidates": [],
    }
    if not fixed_positions or not runs:
        result["reason"] = "insufficient_fixed_signature_fragments"
        return result

    max_mismatches = max(
        1, int(math.ceil(len(fixed_positions) * LANDMARK_MAX_MISMATCH_RATIO))
    )
    candidate_starts: dict[tuple[int, int], int] = {}

    try:
        for section_rva, data in mem.executable_snapshot():
            for run_offset, blob in runs:
                cursor = 0
                hits_for_run = 0
                while hits_for_run < LANDMARK_MAX_ANCHOR_HITS:
                    hit = data.find(blob, cursor)
                    if hit < 0:
                        break
                    cursor = hit + 1
                    hits_for_run += 1
                    start = hit - run_offset
                    if start < 0 or start + len(parsed) > len(data):
                        continue
                    key = (section_rva, start)
                    old = candidate_starts.get(key, 0)
                    candidate_starts[key] = max(old, len(blob))
                    if len(candidate_starts) >= LANDMARK_MAX_CANDIDATE_STARTS:
                        break
                if len(candidate_starts) >= LANDMARK_MAX_CANDIDATE_STARTS:
                    break
            if len(candidate_starts) >= LANDMARK_MAX_CANDIDATE_STARTS:
                break
    except Exception as exc:
        result["reason"] = f"executable_snapshot_failed: {exc}"
        return result

    ranked: list[dict[str, Any]] = []
    section_map = {rva: data for rva, data in mem.executable_snapshot()}
    for (section_rva, start), anchor_length in candidate_starts.items():
        data = section_map.get(section_rva)
        if data is None:
            continue
        mismatches = sum(
            1 for index, expected in fixed_positions
            if data[start + index] != expected
        )
        if mismatches > max_mismatches:
            continue
        fixed_count = len(fixed_positions)
        match_ratio = (fixed_count - mismatches) / fixed_count
        match_rva = section_rva + start
        ranked.append({
            "match_rva": _hex(match_rva),
            "mismatch_count": mismatches,
            "fixed_byte_count": fixed_count,
            "match_ratio": round(match_ratio, 4),
            "anchor_run_length": anchor_length,
            "code_context": _code_context_fingerprint(mem, match_rva),
        })

    ranked.sort(
        key=lambda item: (
            int(item["mismatch_count"]),
            -float(item["match_ratio"]),
            -int(item["anchor_run_length"]),
            str(item["match_rva"]),
        )
    )
    result["candidate_count"] = len(ranked)
    result["candidates"] = ranked[:LANDMARK_MAX_RESULTS]
    if not ranked:
        result["reason"] = "no_close_masked_landmarks_found"
    return result


def _landmark_report_if_needed(
    mem: ProcessMemory,
    pattern: str,
    exact_hits: list[int],
) -> dict[str, Any]:
    if exact_hits:
        return {
            "attempted": False,
            "reason": "exact_signature_hit_available",
            "candidates": [],
        }
    return _nearest_masked_landmarks(mem, pattern)


def _classify_internal_name(internal_name: str, kind: str) -> str:
    """Classify namespace evidence without rejecting unknown/special recipes."""
    name = str(internal_name or "")
    is_salvage = name.startswith("S_")
    is_recipe = bool(
        not is_salvage
        and (
            _RECIPE_COMMON_RE.fullmatch(name)
            or _RECIPE_SET_RE.fullmatch(name)
        )
    )

    if kind == "salvage":
        if is_salvage:
            return "match"
        if is_recipe:
            return "mismatch"
        return "neutral"

    if kind == "recipe":
        if is_recipe:
            return "match"
        if is_salvage:
            return "mismatch"
        return "neutral"

    return "neutral"


def _finalize_collection_namespace(
    result: dict[str, Any],
    *,
    kind: str,
    total: int,
) -> None:
    namespace = result.setdefault(
        "namespace",
        {"matches": 0, "mismatches": 0, "neutral": 0},
    )
    matches = int(namespace.get("matches") or 0)
    mismatches = int(namespace.get("mismatches") or 0)
    neutral = int(namespace.get("neutral") or 0)
    typed = matches + mismatches

    namespace["typed_count"] = typed
    namespace["confidence"] = (
        round(matches / typed, 4) if typed else None
    )

    if total == 0:
        status = "empty_unproven"
        compatible = None
    elif matches > 0 and mismatches == 0:
        status = "type_match"
        compatible = True
    elif mismatches > 0 and matches == 0:
        status = "type_mismatch"
        compatible = False
    elif matches > 0 and mismatches > 0:
        status = "mixed_namespace"
        compatible = False
    else:
        status = "namespace_unproven"
        compatible = False

    namespace["status"] = status
    namespace["type_compatible"] = compatible
    namespace["kind"] = kind
    namespace["neutral_count"] = neutral


def _recovery_classification(probe: dict[str, Any]) -> str:
    if not probe.get("header_plausible"):
        return "rejected_header"
    total = int(probe.get("total") or 0)
    if total == 0:
        return "empty_unproven"
    collection = probe.get("collection") or {}
    if not collection.get("quantity_matches_header"):
        return "rejected_quantity"
    namespace = collection.get("namespace") or {}
    if namespace.get("type_compatible") is True:
        return "strong"
    if namespace.get("status") in {"type_mismatch", "mixed_namespace"}:
        return "type_mismatch"
    return "namespace_unproven"


def _profile_summary(profile: MemoryProfile) -> dict[str, Any]:
    return {
        "profile_id": profile.profile_id,
        "profile_version": profile.profile_version,
        "source": profile.source,
        "priority": profile.priority,
        "locators": profile.data.get("locators") or {},
        "structures": profile.data.get("structures") or {},
        "validation": profile.data.get("validation") or {},
    }


def _probe_identity(mem: ProcessMemory, profile: MemoryProfile) -> dict[str, Any]:
    locator = profile.locator("identity")
    hits = mem.signature_hits(str(locator["pattern"]))
    max_string = as_int(locator.get("max_string", 64), field="identity.max_string")
    max_abs = float(locator.get("position_max_abs", 1000000))
    candidates: list[dict[str, Any]] = []

    for match_rva in hits[:MAX_REPORTED_LOCATOR_CANDIDATES]:
        item: dict[str, Any] = {
            "match_rva": _hex(match_rva),
            "code_context": _code_context_fingerprint(mem, match_rva),
        }
        try:
            xyz_rva = _rip_relative_target_rva(
                mem,
                match_rva,
                disp_offset=as_int(locator["disp_offset"]),
                instruction_end=as_int(locator["instruction_end"]),
                target_adjust=as_int(locator.get("target_adjust", 0)),
            )
            item["xyz_rva"] = _hex(xyz_rva)
            coords = [mem.f32(mem.base + xyz_rva + offset) for offset in (0, 4, 8)]
            item["xyz"] = [
                round(value, 4) if math.isfinite(value) else str(value)
                for value in coords
            ]
            position_plausible = all(
                math.isfinite(value) and abs(value) <= max_abs for value in coords
            )
            item["position_plausible"] = position_plausible
            name = mem.cstring(
                mem.base + xyz_rva + as_int(locator["string_offset"]),
                max_string,
            ).strip()
            item["name"] = name
            item["name_valid"] = _valid_identity_string(name, max_len=max_string - 1)
            item["valid"] = bool(position_plausible and item["name_valid"])
        except Exception as exc:
            item["error"] = str(exc)
            item["valid"] = False
        candidates.append(item)

    return {
        "signature_hit_count": len(hits),
        "signature_hit_rvas": [_hex(x) for x in hits[:MAX_REPORTED_SIGNATURE_HITS]],
        "signature_hits_truncated": len(hits) > MAX_REPORTED_SIGNATURE_HITS,
        "candidates": candidates,
        "valid_candidate_count": sum(1 for item in candidates if item.get("valid")),
        "nearest_landmarks": _landmark_report_if_needed(mem, str(locator["pattern"]), hits),
    }


def _probe_server(mem: ProcessMemory, profile: MemoryProfile) -> dict[str, Any]:
    locator = profile.locator("server")
    hits = mem.signature_hits(str(locator["pattern"]))
    max_string = as_int(locator.get("max_string", 64), field="server.max_string")
    candidates: list[dict[str, Any]] = []

    for match_rva in hits[:MAX_REPORTED_LOCATOR_CANDIDATES]:
        item: dict[str, Any] = {
            "match_rva": _hex(match_rva),
            "code_context": _code_context_fingerprint(mem, match_rva),
        }
        try:
            target_rva = _rip_relative_target_rva(
                mem,
                match_rva,
                disp_offset=as_int(locator["disp_offset"]),
                instruction_end=as_int(locator["instruction_end"]),
            )
            item["target_rva"] = _hex(target_rva)
            value = mem.cstring(mem.base + target_rva, max_string).strip()
            item["value"] = value
            item["valid"] = _valid_identity_string(value, max_len=max_string - 1)
        except Exception as exc:
            item["error"] = str(exc)
            item["valid"] = False
        candidates.append(item)

    return {
        "signature_hit_count": len(hits),
        "signature_hit_rvas": [_hex(x) for x in hits[:MAX_REPORTED_SIGNATURE_HITS]],
        "signature_hits_truncated": len(hits) > MAX_REPORTED_SIGNATURE_HITS,
        "candidates": candidates,
        "valid_candidate_count": sum(1 for item in candidates if item.get("valid")),
        "nearest_landmarks": _landmark_report_if_needed(mem, str(locator["pattern"]), hits),
    }


def _read_vitals(mem: ProcessMemory, character: int, profile: MemoryProfile) -> dict[str, Any]:
    cfg = profile.structure("character")
    values: dict[str, Any] = {}
    for key, field in (
        ("current_hp_offset", "current_hp"),
        ("current_end_offset", "current_end"),
        ("max_hp_offset", "max_hp"),
        ("max_end_offset", "max_end"),
    ):
        try:
            value = mem.f32(character + as_int(cfg[key]))
            values[field] = round(value, 4) if math.isfinite(value) else str(value)
        except Exception as exc:
            values[field] = None
            values.setdefault("errors", []).append(f"{field}: {exc}")
    values["plausible"] = _safe_call(
        lambda: _character_vitals_plausible(mem, character, profile), False
    )
    return values


def _probe_collection(
    mem: ProcessMemory,
    array: int,
    total: int,
    profile: MemoryProfile,
    *,
    kind: str,
) -> dict[str, Any]:
    entry_cfg = profile.structure("entries")
    validation = profile.validation()
    max_entries = min(
        as_int(validation["max_collection_entries"]),
        MAX_REPORTED_COLLECTION_ENTRIES,
    )
    max_string = as_int(validation["max_internal_string"])
    max_level = as_int(validation["max_recipe_level"])

    definition_offset = as_int(entry_cfg["definition_pointer_offset"])
    quantity_offset = as_int(entry_cfg["quantity_offset"])
    name_offset = as_int(entry_cfg["internal_name_pointer_offset"])
    level_offset = as_int(entry_cfg["recipe_level_offset"])

    result: dict[str, Any] = {
        "entries": [],
        "quantity_sum": 0,
        "quantity_matches_header": total == 0,
        "first_null_index": None,
        "stopped_reason": "",
        "valid_entry_count": 0,
        "namespace": {
            "matches": 0,
            "mismatches": 0,
            "neutral": 0,
        },
    }
    if total == 0:
        result["stopped_reason"] = "header_total_zero"
        _finalize_collection_namespace(result, kind=kind, total=total)
        return result
    if not array:
        result["stopped_reason"] = "null_collection_pointer"
        _finalize_collection_namespace(result, kind=kind, total=total)
        return result

    quantity_sum = 0
    for index in range(max_entries):
        row: dict[str, Any] = {"index": index}
        try:
            entry = mem.qword(array + index * 8)
        except Exception as exc:
            row["error"] = f"entry pointer: {exc}"
            result["entries"].append(row)
            result["stopped_reason"] = "entry_pointer_read_failed"
            break

        row["entry"] = _hex(entry)
        if not entry:
            result["entries"].append(row)
            result["first_null_index"] = index
            result["stopped_reason"] = "first_null"
            break

        try:
            definition = mem.qword(entry + definition_offset)
            quantity = mem.u32(entry + quantity_offset)
            row["definition"] = _hex(definition)
            row["quantity"] = quantity
            if not definition:
                row["error"] = "null definition"
                result["entries"].append(row)
                result["stopped_reason"] = "invalid_definition"
                break
            if quantity <= 0 or quantity > max(total, 1):
                row["error"] = "implausible quantity"
                result["entries"].append(row)
                result["stopped_reason"] = "invalid_quantity"
                break

            name_ptr = mem.qword(definition + name_offset)
            row["name_pointer"] = _hex(name_ptr)
            if not name_ptr:
                row["error"] = "null internal-name pointer"
                result["entries"].append(row)
                result["stopped_reason"] = "invalid_name_pointer"
                break

            internal_name = mem.cstring(name_ptr, max_string).strip()
            row["internal_name"] = internal_name
            row["internal_name_plausible"] = bool(
                internal_name and _INTERNAL_NAME_RE.fullmatch(internal_name)
            )
            row["namespace_classification"] = _classify_internal_name(
                internal_name, kind
            )

            if kind == "recipe":
                level = mem.u32(definition + level_offset)
                row["level"] = level
                row["level_plausible"] = 0 <= level <= max_level
            else:
                row["level_plausible"] = True

            row["valid"] = bool(
                row["internal_name_plausible"] and row["level_plausible"]
            )
            if not row["valid"]:
                result["entries"].append(row)
                result["stopped_reason"] = "entry_semantics_invalid"
                break

            classification = row["namespace_classification"]
            if classification == "match":
                result["namespace"]["matches"] += 1
            elif classification == "mismatch":
                result["namespace"]["mismatches"] += 1
            else:
                result["namespace"]["neutral"] += 1

            quantity_sum += quantity
            result["valid_entry_count"] += 1
            result["entries"].append(row)
            if quantity_sum == total:
                result["stopped_reason"] = "header_total_reproduced"
                result["quantity_matches_header"] = True
                break
            if quantity_sum > total:
                result["stopped_reason"] = "quantity_sum_exceeded_header"
                break
        except Exception as exc:
            row["error"] = str(exc)
            result["entries"].append(row)
            result["stopped_reason"] = "entry_decode_failed"
            break
    else:
        result["stopped_reason"] = "diagnostic_entry_limit"

    result["quantity_sum"] = quantity_sum
    _finalize_collection_namespace(result, kind=kind, total=total)
    return result

def _probe_inventory_header(
    mem: ProcessMemory,
    character: int,
    profile: MemoryProfile,
    *,
    kind: str,
    collection_offset: int,
    capacity_delta: int,
    count_delta: int,
) -> dict[str, Any]:
    validation = profile.validation()
    max_capacity = as_int(validation["max_inventory_capacity"])
    result: dict[str, Any] = {
        "kind": kind,
        "character_offset": _hex(collection_offset),
    }

    try:
        header = character + collection_offset
        array = mem.qword(header)
        capacity = mem.u32(header + capacity_delta)
        total = mem.u32(header + count_delta)
        result.update({
            "collection_pointer": _hex(array),
            "capacity": capacity,
            "total": total,
            "header_plausible": bool(
                0 <= capacity <= max_capacity
                and 0 <= total <= capacity
                and (total == 0 or bool(array))
            ),
        })
        if result["header_plausible"]:
            collection = _probe_collection(mem, array, total, profile, kind=kind)
        else:
            collection = {
                "entries": [],
                "quantity_sum": 0,
                "quantity_matches_header": False,
                "first_null_index": None,
                "stopped_reason": "header_implausible",
                "valid_entry_count": 0,
                "namespace": {
                    "matches": 0,
                    "mismatches": 0,
                    "neutral": 0,
                    "typed_count": 0,
                    "confidence": None,
                    "status": "header_implausible",
                    "type_compatible": False,
                    "kind": kind,
                },
            }
        result["collection"] = collection
        result["semantic_valid"] = bool(
            result["header_plausible"] and collection["quantity_matches_header"]
        )
        result["type_compatible"] = (
            (collection.get("namespace") or {}).get("type_compatible")
        )
        result["recovery_valid"] = bool(
            int(total) > 0
            and result["semantic_valid"]
            and int(collection.get("valid_entry_count") or 0) > 0
            and result["type_compatible"] is True
        )
        result["recovery_classification"] = _recovery_classification(result)
    except Exception as exc:
        result["error"] = str(exc)
        result["header_plausible"] = False
        result["semantic_valid"] = False
        result["type_compatible"] = False
        result["recovery_valid"] = False
        result["recovery_classification"] = "read_failed"
    return result


def _header_score(probe: dict[str, Any]) -> float:
    if not probe.get("header_plausible"):
        return 0.0

    total = int(probe.get("total") or 0)
    capacity = int(probe.get("capacity") or 0)
    pointer = probe.get("collection_pointer")
    substantive_header = bool(capacity > 0 or total > 0 or pointer not in {None, "0x0"})

    # Empty headers are legitimate states, but they cannot prove the collection
    # type and must never compete with a populated, quantity-validated candidate.
    if total == 0:
        return 1.25 if substantive_header else 0.25

    collection = probe.get("collection") or {}
    namespace = collection.get("namespace") or {}

    score = 2.0  # plausible populated header
    if int(collection.get("valid_entry_count") or 0) > 0:
        score += 2.0
    if collection.get("quantity_matches_header"):
        score += 4.0
    if collection.get("stopped_reason") == "header_total_reproduced":
        score += 1.0
    if probe.get("semantic_valid"):
        score += 1.0

    if namespace.get("type_compatible") is True:
        score += 5.0
        confidence = namespace.get("confidence")
        if isinstance(confidence, (int, float)):
            score += min(1.0, max(0.0, float(confidence)))
    elif namespace.get("status") in {"type_mismatch", "mixed_namespace"}:
        score -= 3.0

    if probe.get("recovery_valid"):
        score += 1.0
    return round(score, 3)


def _candidate_from_probe(
    offset: int,
    expected_offset: int,
    probe: dict[str, Any],
    score: float,
) -> dict[str, Any]:
    namespace = (probe.get("collection") or {}).get("namespace") or {}
    return {
        "offset": _hex(offset),
        "delta_from_expected": offset - expected_offset,
        "score": score,
        "classification": probe.get("recovery_classification"),
        "capacity": probe.get("capacity"),
        "total": probe.get("total"),
        "semantic_valid": probe.get("semantic_valid", False),
        "recovery_valid": probe.get("recovery_valid", False),
        "type_compatible": probe.get("type_compatible"),
        "namespace_status": namespace.get("status"),
        "namespace_matches": namespace.get("matches", 0),
        "namespace_mismatches": namespace.get("mismatches", 0),
        "namespace_neutral": namespace.get("neutral", 0),
        "namespace_confidence": namespace.get("confidence"),
        "valid_entry_count": (probe.get("collection") or {}).get("valid_entry_count", 0),
        "quantity_sum": (probe.get("collection") or {}).get("quantity_sum", 0),
        "stopped_reason": (probe.get("collection") or {}).get("stopped_reason", ""),
    }


def _recovery_winner_summary(
    strong: list[dict[str, Any]],
) -> dict[str, Any]:
    if not strong:
        return {
            "best": None,
            "runner_up": None,
            "score_margin": None,
            "clear_winner": False,
            "reason": "no_strong_candidates",
        }
    best = strong[0]
    runner = strong[1] if len(strong) > 1 else None
    margin = (
        round(float(best["score"]) - float(runner["score"]), 3)
        if runner is not None else None
    )
    clear = runner is None or (margin is not None and margin >= 3.0)
    return {
        "best": best,
        "runner_up": runner,
        "score_margin": margin,
        "clear_winner": bool(clear),
        "reason": (
            "only_strong_candidate" if runner is None
            else ("score_margin_sufficient" if clear else "score_margin_ambiguous")
        ),
    }


def _scan_header_candidates(
    mem: ProcessMemory,
    character: int,
    profile: MemoryProfile,
    *,
    kind: str,
    expected_offset: int,
    capacity_delta: int,
    count_delta: int,
) -> dict[str, Any]:
    all_candidates: list[dict[str, Any]] = []
    start = max(0, expected_offset - RECOVERY_WINDOW)
    end = expected_offset + RECOVERY_WINDOW

    for offset in range(start, end + 1, RECOVERY_STEP):
        probe = _probe_inventory_header(
            mem,
            character,
            profile,
            kind=kind,
            collection_offset=offset,
            capacity_delta=capacity_delta,
            count_delta=count_delta,
        )
        score = _header_score(probe)
        if score <= 0:
            continue
        all_candidates.append(
            _candidate_from_probe(offset, expected_offset, probe, score)
        )

    all_candidates.sort(
        key=lambda item: (
            -float(item["score"]),
            abs(int(item["delta_from_expected"])),
            str(item["offset"]),
        )
    )
    strong = [
        item for item in all_candidates
        if item.get("classification") == "strong"
    ][:MAX_RECOVERY_STRONG_RESULTS]
    type_mismatch = [
        item for item in all_candidates
        if item.get("classification") == "type_mismatch"
    ][:MAX_RECOVERY_AUX_RESULTS]
    namespace_unproven = [
        item for item in all_candidates
        if item.get("classification") == "namespace_unproven"
    ][:MAX_RECOVERY_AUX_RESULTS]
    empty_unproven = [
        item for item in all_candidates
        if item.get("classification") == "empty_unproven"
    ][:MAX_RECOVERY_AUX_RESULTS]

    # The compact primary list prioritizes candidates that could actually support
    # recovery, then preserves a few useful rejected/unproven observations.
    primary = (
        strong
        + type_mismatch[:2]
        + namespace_unproven[:2]
        + empty_unproven[:2]
    )[:MAX_RECOVERY_RESULTS]

    return {
        "candidates": primary,
        "strong_candidates": strong,
        "type_mismatch_candidates": type_mismatch,
        "namespace_unproven_candidates": namespace_unproven,
        "empty_unproven_candidates": empty_unproven,
        "winner": _recovery_winner_summary(strong),
    }


def _inventory_probe(
    mem: ProcessMemory,
    character: int,
    profile: MemoryProfile,
) -> dict[str, Any]:
    char_cfg = profile.structure("character")
    output: dict[str, Any] = {}

    for kind in ("recipes", "salvage"):
        cfg = char_cfg[kind]
        collection_offset = as_int(cfg["collection_offset"])
        capacity_offset = as_int(cfg["capacity_offset"])
        count_offset = as_int(cfg["count_offset"])
        capacity_delta = capacity_offset - collection_offset
        count_delta = count_offset - collection_offset
        probe_kind = "recipe" if kind == "recipes" else "salvage"

        expected = _probe_inventory_header(
            mem,
            character,
            profile,
            kind=probe_kind,
            collection_offset=collection_offset,
            capacity_delta=capacity_delta,
            count_delta=count_delta,
        )
        recovery = _scan_header_candidates(
            mem,
            character,
            profile,
            kind=probe_kind,
            expected_offset=collection_offset,
            capacity_delta=capacity_delta,
            count_delta=count_delta,
        )
        output[kind] = {
            "expected": expected,
            "bounded_recovery_window": {
                "bytes_each_direction": RECOVERY_WINDOW,
                "step": RECOVERY_STEP,
                "auto_adopted": False,
                **recovery,
            },
        }
    return output

def _probe_roster(
    mem: ProcessMemory,
    profile: MemoryProfile,
    identity_names: set[str],
) -> dict[str, Any]:
    locator = profile.locator("roster")
    roster_cfg = profile.structure("roster")
    entity_cfg = profile.structure("entity")
    max_roster = as_int(profile.validation()["max_roster_count"])
    hits = mem.signature_hits(str(locator["pattern"]))
    candidates: list[dict[str, Any]] = []

    for match_rva in hits[:MAX_REPORTED_LOCATOR_CANDIDATES]:
        item: dict[str, Any] = {
            "match_rva": _hex(match_rva),
            "code_context": _code_context_fingerprint(mem, match_rva),
        }
        try:
            roster_base_rva = _rip_relative_target_rva(
                mem,
                match_rva,
                disp_offset=as_int(locator["base_disp_offset"]),
                instruction_end=as_int(locator["base_instruction_end"]),
            )
            roster_count_rva = _rip_relative_target_rva(
                mem,
                match_rva,
                disp_offset=as_int(locator["count_disp_offset"]),
                instruction_end=as_int(locator["count_instruction_end"]),
            )
            item["roster_base_rva"] = _hex(roster_base_rva)
            item["roster_count_rva"] = _hex(roster_count_rva)
            raw_count = mem.u32(mem.base + roster_count_rva)
            item["raw_roster_count"] = raw_count
            item["roster_count_plausible"] = 0 <= raw_count <= max_roster

            record = mem.base + roster_base_rva
            copied_name = mem.cstring(
                record + as_int(roster_cfg["name_offset"]), 64
            ).strip()
            item["record0_name"] = copied_name
            item["record0_name_valid"] = _valid_identity_string(
                copied_name, max_len=63
            )

            entity = mem.qword(
                record + as_int(roster_cfg["entity_pointer_offset"])
            )
            item["entity_pointer"] = _hex(entity)
            if entity:
                entity_name = mem.cstring(
                    entity + as_int(entity_cfg["name_offset"]), 64
                ).strip()
                item["entity_name"] = entity_name
                item["entity_name_valid"] = _valid_identity_string(
                    entity_name, max_len=63
                )
                character = mem.qword(
                    entity + as_int(entity_cfg["character_pointer_offset"])
                )
                item["character_pointer"] = _hex(character)
                if character:
                    item["vitals"] = _read_vitals(mem, character, profile)
                    item["inventory"] = _inventory_probe(mem, character, profile)
                else:
                    item["vitals"] = {"plausible": False}
            else:
                item["entity_name"] = ""
                item["entity_name_valid"] = False
                item["character_pointer"] = None
                item["vitals"] = {"plausible": False}

            identity_match = bool(
                identity_names
                and copied_name in identity_names
                and item.get("entity_name") in identity_names
            )
            item["identity_agreement"] = identity_match
            item["semantic_valid"] = bool(
                item["roster_count_plausible"]
                and identity_match
                and (item.get("vitals") or {}).get("plausible")
            )
        except Exception as exc:
            item["error"] = str(exc)
            item["semantic_valid"] = False
        candidates.append(item)

    return {
        "signature_hit_count": len(hits),
        "signature_hit_rvas": [_hex(x) for x in hits[:MAX_REPORTED_SIGNATURE_HITS]],
        "signature_hits_truncated": len(hits) > MAX_REPORTED_SIGNATURE_HITS,
        "candidates": candidates,
        "semantic_valid_candidate_count": sum(
            1 for item in candidates if item.get("semantic_valid")
        ),
        "nearest_landmarks": _landmark_report_if_needed(mem, str(locator["pattern"]), hits),
    }


def collect_memory_diagnostic(
    process: GameProcessInfo | int,
    *,
    failure_detail: str | None = None,
) -> dict[str, Any]:
    pid = process.pid if isinstance(process, GameProcessInfo) else int(process)
    window_title = process.window_title if isinstance(process, GameProcessInfo) else ""
    label_name = process.character_name if isinstance(process, GameProcessInfo) else ""
    label_server = process.server if isinstance(process, GameProcessInfo) else ""

    report: dict[str, Any] = {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "generated_at_utc": _utc_now().isoformat(),
        "field_crafter_version": APP_VERSION,
        "pid": pid,
        "selector": {
            "window_title": window_title,
            "character_name": label_name,
            "server": label_server,
        },
        "failure_detail": failure_detail or "",
        "raw_memory_dump_included": False,
        "auto_recovery_applied": False,
        "profiles": [],
    }

    manager = MemoryProfileManager()
    report["profile_manager_warnings"] = list(manager.warnings)

    with ProcessMemory(pid) as mem:
        report["module"] = {
            "name": Path(mem.module_path).name if mem.module_path else "cityofheroes.exe",
            "base": _hex(mem.base),
            "size": mem.module_size,
            "sha256": _sha256_file(mem.module_path) if mem.module_path else None,
        }

        for profile in manager.candidates():
            profile_report = _profile_summary(profile)
            try:
                identity = _probe_identity(mem, profile)
            except Exception as exc:
                identity = {"error": str(exc), "signature_hit_count": 0, "candidates": [], "nearest_landmarks": {"attempted": False, "reason": "probe_failed", "candidates": []}}
            try:
                server = _probe_server(mem, profile)
            except Exception as exc:
                server = {"error": str(exc), "signature_hit_count": 0, "candidates": [], "nearest_landmarks": {"attempted": False, "reason": "probe_failed", "candidates": []}}

            identity_names = {
                str(item.get("name"))
                for item in identity.get("candidates", [])
                if item.get("valid") and item.get("name")
            }
            if (
                label_name
                and _valid_identity_string(str(label_name).strip(), max_len=63)
            ):
                # The v5 process selector can itself have been recovered through
                # conservative root semantics. Keep it only as diagnostic name
                # evidence; structural candidates are never auto-adopted.
                identity_names.add(str(label_name).strip())
            try:
                roster = _probe_roster(mem, profile, identity_names)
            except Exception as exc:
                roster = {"error": str(exc), "signature_hit_count": 0, "candidates": [], "nearest_landmarks": {"attempted": False, "reason": "probe_failed", "candidates": []}}

            try:
                from .memory_structural_diagnostics import (
                    collect_structural_drift_evidence,
                )
                structural = collect_structural_drift_evidence(
                    mem,
                    profile,
                    trusted_identity_names=identity_names,
                    roster_observation=roster,
                )
            except Exception as exc:
                structural = {
                    "diagnostic_only": True,
                    "auto_adopted": False,
                    "persistent_changes": False,
                    "status": "probe_failed",
                    "error": str(exc),
                }

            profile_report["observations"] = {
                "identity": identity,
                "server": server,
                "roster": roster,
                "structural_drift": structural,
            }
            profile_report["summary"] = {
                "identity_valid_candidates": int(identity.get("valid_candidate_count") or 0),
                "server_valid_candidates": int(server.get("valid_candidate_count") or 0),
                "roster_semantic_valid_candidates": int(
                    roster.get("semantic_valid_candidate_count") or 0
                ),
                "structural_drift_status": structural.get("status", ""),
                "structural_clear_evidence": sum(
                    1
                    for value in (structural.get("summary") or {}).values()
                    if value is True
                ),
            }
            report["profiles"].append(profile_report)

    return report


def _analysis_text(report: dict[str, Any]) -> str:
    lines = [
        "Field Crafter Memory Diagnostic",
        "=" * 31,
        "",
        f"Diagnostic schema: {report.get('schema_version')}",
        f"Generated UTC: {report.get('generated_at_utc')}",
        f"Field Crafter: {report.get('field_crafter_version')}",
        f"PID: {report.get('pid')}",
    ]
    module = report.get("module") or {}
    lines.extend([
        f"Client module: {module.get('name')}",
        f"Client SHA-256: {module.get('sha256') or 'unavailable'}",
        f"Module size: {module.get('size')}",
        "",
    ])
    failure = str(report.get("failure_detail") or "").strip()
    if failure:
        lines.extend(["Original memory-read failure:", failure, ""])

    for profile in report.get("profiles") or []:
        lines.append(
            f"Profile {profile.get('profile_id')} "
            f"{profile.get('profile_version')} [{profile.get('source')}]"
        )
        obs = profile.get("observations") or {}
        identity = obs.get("identity") or {}
        server = obs.get("server") or {}
        roster = obs.get("roster") or {}

        for label, block, valid_key in (
            ("Identity", identity, "valid_candidate_count"),
            ("Server", server, "valid_candidate_count"),
            ("Roster", roster, "semantic_valid_candidate_count"),
        ):
            lines.append(
                f"  {label}: signature hits={block.get('signature_hit_count', 0)}, "
                f"valid candidates={block.get(valid_key, 0)}"
            )
            nearest = block.get("nearest_landmarks") or {}
            if int(block.get("signature_hit_count") or 0) == 0 and nearest.get("attempted"):
                candidates = nearest.get("candidates") or []
                if candidates:
                    rendered = ", ".join(
                        f"{item.get('match_rva')} mismatches={item.get('mismatch_count')}/"
                        f"{item.get('fixed_byte_count')} ratio={item.get('match_ratio')}"
                        for item in candidates[:3]
                    )
                    lines.append(f"    nearest masked code landmarks: {rendered}")
                else:
                    lines.append(
                        f"    nearest masked code landmarks: none "
                        f"({nearest.get('reason') or 'no candidate'})"
                    )

        for idx, candidate in enumerate(roster.get("candidates") or []):
            lines.append(
                f"    Roster candidate {idx + 1}: count={candidate.get('raw_roster_count')}, "
                f"record0={candidate.get('record0_name')!r}, "
                f"entity_name={candidate.get('entity_name')!r}, "
                f"identity_agreement={candidate.get('identity_agreement')}, "
                f"vitals_plausible={(candidate.get('vitals') or {}).get('plausible')}"
            )
            inventory = candidate.get("inventory") or {}
            for kind in ("recipes", "salvage"):
                inv = inventory.get(kind) or {}
                expected = inv.get("expected") or {}
                collection = expected.get("collection") or {}
                namespace = collection.get("namespace") or {}
                lines.append(
                    f"      {kind}: expected_offset={expected.get('character_offset')}, "
                    f"capacity={expected.get('capacity')}, total={expected.get('total')}, "
                    f"quantity_sum={collection.get('quantity_sum')}, "
                    f"semantic_valid={expected.get('semantic_valid')}, "
                    f"namespace={namespace.get('status')}"
                )

                recovery = inv.get("bounded_recovery_window") or {}
                winner = recovery.get("winner") or {}
                best = winner.get("best")
                runner = winner.get("runner_up")
                if best:
                    lines.append(
                        f"        recovery winner: {best.get('offset')} "
                        f"score={best.get('score')} class={best.get('classification')}; "
                        f"runner_up={runner.get('offset') if runner else None}; "
                        f"margin={winner.get('score_margin')}; "
                        f"clear_winner={winner.get('clear_winner')}"
                    )
                else:
                    lines.append(
                        f"        recovery winner: none ({winner.get('reason')})"
                    )

                mismatches = recovery.get("type_mismatch_candidates") or []
                if mismatches:
                    rendered = ", ".join(
                        f"{item.get('offset')} score={item.get('score')} "
                        f"namespace={item.get('namespace_status')}"
                        for item in mismatches[:2]
                    )
                    lines.append(
                        f"        type-mismatch candidates retained for diagnosis: {rendered}"
                    )

                empty = recovery.get("empty_unproven_candidates") or []
                if empty:
                    rendered = ", ".join(
                        f"{item.get('offset')} score={item.get('score')}"
                        for item in empty[:2]
                    )
                    lines.append(
                        f"        empty/unproven observations: {rendered}"
                    )
        structural = obs.get("structural_drift") or {}
        lines.append(
            f"  Structural drift diagnostics: status={structural.get('status')}, "
            f"diagnostic_only={structural.get('diagnostic_only')}, "
            f"auto_adopted={structural.get('auto_adopted')}"
        )
        anchor = structural.get("roster_anchor") or {}
        if anchor.get("resolved"):
            lines.append(
                f"    roster code anchor: source={anchor.get('source')}, "
                f"raw_count={anchor.get('raw_roster_count')}"
            )
        else:
            lines.append(
                f"    roster code anchor unavailable: "
                f"{anchor.get('reason') or structural.get('reason') or 'unknown'}"
            )

        record = structural.get("roster_record") or {}
        name_scan = record.get("name_offset_scan") or {}
        name_winner = name_scan.get("winner")
        if name_winner:
            lines.append(
                f"    roster name field candidate: {name_winner.get('offset')} "
                f"delta={name_winner.get('delta_from_expected')}"
            )

        entity_scan = record.get("entity_pointer_offset_scan") or {}
        entity_winner = entity_scan.get("winner")
        if entity_winner:
            lines.append(
                f"    roster Entity* field candidate: {entity_winner.get('offset')} "
                f"delta={entity_winner.get('delta_from_expected')}"
            )
            entity_name = (
                entity_winner.get("entity_name_offset_scan") or {}
            ).get("winner")
            if entity_name:
                lines.append(
                    f"      Entity name field candidate: {entity_name.get('offset')} "
                    f"delta={entity_name.get('delta_from_expected')}"
                )
            character_scan = (
                entity_winner.get("character_pointer_offset_scan") or {}
            )
            character_winner = character_scan.get("winner")
            if character_winner:
                lines.append(
                    f"      Entity Character* field candidate: "
                    f"{character_winner.get('offset')} "
                    f"delta={character_winner.get('delta_from_expected')}"
                )
                vitals = (
                    character_winner.get("vitals_common_shift") or {}
                ).get("winner")
                if vitals:
                    lines.append(
                        f"        Character vitals common shift candidate: "
                        f"{vitals.get('shift'):+d} bytes"
                    )

        entries = structural.get("entries") or {}
        for kind in ("recipes", "salvage"):
            block = entries.get(kind) or {}
            if not block.get("available"):
                continue
            quantity = (block.get("quantity_offset_scan") or {}).get("winner")
            pair = (
                block.get("definition_and_name_offset_scan") or {}
            ).get("winner")
            lines.append(
                f"    {kind} entry layout: "
                f"quantity_offset={quantity.get('offset') if quantity else None}; "
                f"definition_offset={pair.get('definition_pointer_offset') if pair else None}; "
                f"name_pointer_offset={pair.get('internal_name_pointer_offset') if pair else None}"
            )
            if kind == "recipes":
                level = (
                    block.get("recipe_level_offset_scan") or {}
                ).get("winner")
                lines.append(
                    f"      recipe level field candidate: "
                    f"{level.get('offset') if level else None}"
                )

        lines.append("")

    lines.extend([
        "Safety notes:",
        "  No raw process-memory dump is included.",
        "  Successful/near code landmarks include hashes of small code windows, not raw code bytes.",
        "  Bounded recovery candidates are observations only; none were auto-adopted.",
        "  Structural-drift candidates are diagnostic observations only and are never auto-adopted or persisted.",
        "",
    ])
    return "\n".join(lines)

def create_memory_diagnostic_zip(
    process: GameProcessInfo | int,
    *,
    failure_detail: str | None = None,
    output_dir: str | Path | None = None,
) -> Path:
    pid = process.pid if isinstance(process, GameProcessInfo) else int(process)
    directory = Path(output_dir) if output_dir else default_diagnostic_dir()
    directory.mkdir(parents=True, exist_ok=True)
    final_path = directory / diagnostic_filename(pid)
    temp_path = final_path.with_suffix(".zip.new")

    report = collect_memory_diagnostic(process, failure_detail=failure_detail)
    report_json = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    analysis = (_analysis_text(report) + "\n").encode("utf-8")

    with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("memory_diagnostic.json", report_json)
        archive.writestr("memory_diagnostic_analysis.txt", analysis)

    os.replace(temp_path, final_path)
    return final_path
