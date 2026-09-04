from __future__ import annotations

import math
import re
from typing import Any

from .memory_profiles import MemoryProfile, as_int
from .salvage_semantics import default_invention_salvage_membership


# FIELD_CRAFTER_MEMORY_STRUCTURAL_DIAGNOSTICS_V6
# FIELD_CRAFTER_MEMORY_STRUCTURAL_DIAGNOSTICS_V6_2
# FIELD_CRAFTER_MEMORY_STRUCTURAL_DIAGNOSTICS_V6_3
# FIELD_CRAFTER_INVENTION_SALVAGE_STRUCTURAL_DIAGNOSTICS_V1
# FIELD_CRAFTER_INVENTION_SALVAGE_STRUCTURAL_DIAGNOSTICS_V2
#
# Diagnostic only. Nothing in this module is used by the production reader to
# adopt or persist a memory layout.

ROSTER_NAME_WINDOW = 0x100
ROSTER_POINTER_WINDOW = 0x100
ENTITY_NAME_WINDOW = 0x180
ENTITY_POINTER_WINDOW = 0x100
VITALS_COMMON_SHIFT_WINDOW = 0x100
INVENTORY_HEADER_WINDOW = 0x200
ENTRY_FIELD_WINDOW = 0x20

MAX_ENTITY_POINTER_CANDIDATES = 16
MAX_CHARACTER_POINTER_CANDIDATES = 12
MAX_VITALS_CANDIDATES = 8
MAX_ENTRY_POINTERS = 64
MAX_ENTRY_PAIR_CANDIDATES = 8
MAX_QUANTITY_CANDIDATES = 8
MAX_LEVEL_CANDIDATES = 8
MAX_JOINT_RAW_HEADER_CANDIDATES = 16
MAX_JOINT_HYPOTHESIS_RESULTS = 12
JOINT_HYPOTHESIS_MIN_MARGIN = 5.0

_INTERNAL_NAME_RE = re.compile(r"^[A-Za-z0-9_.'+\-]{1,191}$")
_RECIPE_COMMON_RE = re.compile(r"^Invention_.+_[0-9]+$")
_RECIPE_SET_RE = re.compile(r"^.+_[A-F]_[0-9]+$")
_LEVEL_SUFFIX_RE = re.compile(r"_([0-9]+)$")


def structural_diagnostic_policy_summary() -> dict[str, Any]:
    return {
        "diagnostic_only": True,
        "auto_adopted": False,
        "persistent": False,
        "roster_name_window_bytes_each_direction": ROSTER_NAME_WINDOW,
        "roster_pointer_window_bytes_each_direction": ROSTER_POINTER_WINDOW,
        "entity_name_window_bytes_each_direction": ENTITY_NAME_WINDOW,
        "entity_pointer_window_bytes_each_direction": ENTITY_POINTER_WINDOW,
        "vitals_common_shift_window_bytes_each_direction": VITALS_COMMON_SHIFT_WINDOW,
        "inventory_header_window_bytes_each_direction": INVENTORY_HEADER_WINDOW,
        "entry_field_window_bytes_each_direction": ENTRY_FIELD_WINDOW,
        "entry_layout_auto_recovery": False,
        "entity_character_layout_auto_recovery": False,
        "vitals_layout_auto_recovery": False,
        "entry_anchor_uses_type_aware_inventory_scan": True,
        "entry_anchor_requires_clear_strong_winner": True,
        "entry_anchor_joint_header_entry_hypothesis_fallback": True,
        "joint_hypothesis_minimum_score_margin": JOINT_HYPOTHESIS_MIN_MARGIN,
        "joint_hypothesis_auto_recovery": False,
    }


def _hex(value: int | None) -> str | None:
    if value is None:
        return None
    try:
        return f"0x{int(value):X}"
    except Exception:
        return None


def _valid_identity_string(value: str, *, max_len: int = 63) -> bool:
    value = str(value or "").strip()
    if not value or len(value) > max_len:
        return False
    return all(ch.isprintable() and ch not in "\r\n\t" for ch in value)


def _classify_name(name: str, kind: str) -> str:
    value = str(name or "")
    is_salvage = value.startswith("S_")
    is_recipe = bool(
        not is_salvage
        and (
            _RECIPE_COMMON_RE.fullmatch(value)
            or _RECIPE_SET_RE.fullmatch(value)
        )
    )
    if kind == "salvage":
        return "match" if is_salvage else ("mismatch" if is_recipe else "neutral")
    if kind == "recipe":
        return "match" if is_recipe else ("mismatch" if is_salvage else "neutral")
    return "neutral"


def _offset_candidates(expected: int, window: int, step: int):
    start = max(0, int(expected) - int(window))
    end = int(expected) + int(window)
    offsets = list(range(start, end + 1, step))
    offsets.sort(key=lambda value: (abs(value - expected), value))
    return offsets


def _resolve_roster_anchor(
    mem,
    profile: MemoryProfile,
    roster_observation: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve only the code-derived roster base/count, not structure fields."""
    max_roster = as_int(profile.validation()["max_roster_count"])

    for candidate in (roster_observation or {}).get("candidates") or []:
        try:
            base_text = candidate.get("roster_base_rva")
            count_text = candidate.get("roster_count_rva")
            if base_text is None or count_text is None:
                continue
            base_rva = int(str(base_text), 0)
            count_rva = int(str(count_text), 0)
            raw_count = int(mem.u32(mem.base + count_rva))
            if 1 <= raw_count <= max_roster:
                return {
                    "resolved": True,
                    "source": "diagnostic_roster_signature",
                    "match_rva": candidate.get("match_rva"),
                    "roster_base_rva": _hex(base_rva),
                    "roster_count_rva": _hex(count_rva),
                    "raw_roster_count": raw_count,
                }
        except Exception:
            continue

    # If the exact signature itself drifted, reuse only v5's conservative code
    # landmark selector. Do NOT run its structure semantics here.
    try:
        from .game_memory import _rip_relative_target_rva
        from .memory_root_recovery import _select_code_candidate

        code = _select_code_candidate(mem, profile, "roster")
        locator = profile.locator("roster")
        base_rva = _rip_relative_target_rva(
            mem,
            code.match_rva,
            disp_offset=as_int(locator["base_disp_offset"]),
            instruction_end=as_int(locator["base_instruction_end"]),
        )
        count_rva = _rip_relative_target_rva(
            mem,
            code.match_rva,
            disp_offset=as_int(locator["count_disp_offset"]),
            instruction_end=as_int(locator["count_instruction_end"]),
        )
        raw_count = int(mem.u32(mem.base + count_rva))
        if not (1 <= raw_count <= max_roster):
            raise RuntimeError(
                f"code-derived roster count {raw_count} is outside 1..{max_roster}"
            )
        return {
            "resolved": True,
            "source": code.source,
            "match_rva": _hex(code.match_rva),
            "mismatch_count": code.mismatch_count,
            "roster_base_rva": _hex(base_rva),
            "roster_count_rva": _hex(count_rva),
            "raw_roster_count": raw_count,
        }
    except Exception as exc:
        return {
            "resolved": False,
            "reason": str(exc),
        }


def _scan_exact_name_offsets(
    mem,
    base: int,
    *,
    expected_offset: int,
    window: int,
    trusted_names: set[str],
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    if not trusted_names:
        return {
            "expected_offset": _hex(expected_offset),
            "window_bytes_each_direction": window,
            "exact_identity_matches": [],
            "clear_winner": False,
            "reason": "no_trusted_identity_name",
        }

    for offset in _offset_candidates(expected_offset, window, 1):
        try:
            value = mem.cstring(base + offset, 64).strip()
        except Exception:
            continue
        if value in trusted_names:
            matches.append({
                "offset": _hex(offset),
                "delta_from_expected": offset - expected_offset,
                "value": value,
            })

    return {
        "expected_offset": _hex(expected_offset),
        "window_bytes_each_direction": window,
        "exact_identity_matches": matches[:16],
        "clear_winner": len(matches) == 1,
        "winner": matches[0] if len(matches) == 1 else None,
        "reason": (
            "one_exact_identity_match" if len(matches) == 1
            else ("no_exact_identity_match" if not matches else "multiple_exact_identity_matches")
        ),
    }


def _raw_inventory_header(
    mem,
    character: int,
    profile: MemoryProfile,
    *,
    kind: str,
    collection_offset: int,
) -> dict[str, Any]:
    char_cfg = profile.structure("character")[kind]
    expected = as_int(char_cfg["collection_offset"])
    capacity_delta = as_int(char_cfg["capacity_offset"]) - expected
    count_delta = as_int(char_cfg["count_offset"]) - expected
    max_capacity = as_int(profile.validation()["max_inventory_capacity"])

    try:
        array = int(mem.qword(character + collection_offset))
        capacity = int(mem.u32(character + collection_offset + capacity_delta))
        total = int(mem.u32(character + collection_offset + count_delta))
        plausible = bool(
            0 <= capacity <= max_capacity
            and 0 <= total <= capacity
            and (total == 0 or array != 0)
        )
        return {
            "offset": _hex(collection_offset),
            "delta_from_expected": collection_offset - expected,
            "collection_pointer": _hex(array),
            "capacity": capacity,
            "total": total,
            "plausible": plausible,
            "populated": bool(plausible and total > 0 and array),
        }
    except Exception as exc:
        return {
            "offset": _hex(collection_offset),
            "delta_from_expected": collection_offset - expected,
            "plausible": False,
            "populated": False,
            "error": str(exc),
        }


def _scan_raw_inventory_headers(
    mem,
    character: int,
    profile: MemoryProfile,
    *,
    kind: str,
) -> dict[str, Any]:
    expected = as_int(profile.structure("character")[kind]["collection_offset"])
    populated: list[dict[str, Any]] = []
    empty: list[dict[str, Any]] = []

    for offset in _offset_candidates(expected, INVENTORY_HEADER_WINDOW, 8):
        item = _raw_inventory_header(
            mem,
            character,
            profile,
            kind=kind,
            collection_offset=offset,
        )
        if not item.get("plausible"):
            continue
        if item.get("populated"):
            populated.append(item)
        else:
            empty.append(item)

    populated.sort(
        key=lambda item: (
            abs(int(item["delta_from_expected"])),
            str(item["offset"]),
        )
    )
    empty.sort(
        key=lambda item: (
            abs(int(item["delta_from_expected"])),
            str(item["offset"]),
        )
    )
    return {
        "expected_offset": _hex(expected),
        "window_bytes_each_direction": INVENTORY_HEADER_WINDOW,
        "populated_candidates": populated[:8],
        "empty_candidates": empty[:4],
    }


def _vitals_values_at_shift(
    mem,
    character: int,
    profile: MemoryProfile,
    shift: int,
) -> dict[str, Any]:
    cfg = profile.structure("character")
    offsets = {
        "current_hp": as_int(cfg["current_hp_offset"]) + shift,
        "current_end": as_int(cfg["current_end_offset"]) + shift,
        "max_hp": as_int(cfg["max_hp_offset"]) + shift,
        "max_end": as_int(cfg["max_end_offset"]) + shift,
    }
    values = {
        name: float(mem.f32(character + offset))
        for name, offset in offsets.items()
    }
    validation = profile.validation()
    max_hp_cap = float(validation["max_hp"])
    max_end_cap = float(validation["max_endurance"])

    finite = all(math.isfinite(value) for value in values.values())
    plausible = bool(
        finite
        and 0.0 <= values["current_hp"] <= max_hp_cap
        and 1.0 <= values["max_hp"] <= max_hp_cap
        and 0.0 <= values["current_end"] <= max_end_cap
        and 1.0 <= values["max_end"] <= max_end_cap
        and values["current_hp"] <= values["max_hp"] * 1.5
        and values["current_end"] <= values["max_end"] * 1.5
    )
    if not plausible:
        score = 0.0
    else:
        score = 4.0
        if values["current_hp"] <= values["max_hp"] * 1.05:
            score += 1.0
        if values["current_end"] <= values["max_end"] * 1.05:
            score += 1.0
        if values["max_end"] <= 1000:
            score += 0.5

    return {
        "shift": shift,
        "offsets": {key: _hex(value) for key, value in offsets.items()},
        "values": {key: round(value, 4) for key, value in values.items()},
        "plausible": plausible,
        "score": round(score, 3),
    }


def _scan_vitals_common_shift(
    mem,
    character: int,
    profile: MemoryProfile,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for shift in range(
        -VITALS_COMMON_SHIFT_WINDOW,
        VITALS_COMMON_SHIFT_WINDOW + 1,
        4,
    ):
        try:
            item = _vitals_values_at_shift(mem, character, profile, shift)
        except Exception:
            continue
        if item["plausible"]:
            candidates.append(item)

    candidates.sort(
        key=lambda item: (
            -float(item["score"]),
            abs(int(item["shift"])),
            int(item["shift"]),
        )
    )
    best = candidates[0] if candidates else None
    runner = candidates[1] if len(candidates) > 1 else None
    clear = bool(
        best is not None
        and (
            runner is None
            or float(best["score"]) - float(runner["score"]) >= 1.0
        )
    )
    return {
        "window_bytes_each_direction": VITALS_COMMON_SHIFT_WINDOW,
        "step": 4,
        "candidates": candidates[:MAX_VITALS_CANDIDATES],
        "winner": best if clear else None,
        "clear_winner": clear,
        "reason": (
            "one_or_separated_plausible_shift" if clear
            else ("no_plausible_shift" if not candidates else "plausible_shifts_ambiguous")
        ),
    }


def _character_evidence(
    mem,
    character: int,
    profile: MemoryProfile,
) -> dict[str, Any]:
    vitals = _scan_vitals_common_shift(mem, character, profile)
    recipes = _scan_raw_inventory_headers(
        mem, character, profile, kind="recipes"
    )
    salvage = _scan_raw_inventory_headers(
        mem, character, profile, kind="salvage"
    )

    score = 0
    if vitals.get("winner"):
        score += 5
    if recipes.get("populated_candidates"):
        score += 2
    if salvage.get("populated_candidates"):
        score += 2

    return {
        "character_pointer": _hex(character),
        "score": score,
        "vitals_common_shift": vitals,
        "raw_inventory_headers": {
            "recipes": recipes,
            "salvage": salvage,
        },
    }


def _scan_character_pointer_offsets(
    mem,
    entity: int,
    profile: MemoryProfile,
) -> dict[str, Any]:
    expected = as_int(profile.structure("entity")["character_pointer_offset"])
    raw: list[tuple[int, int]] = []

    for offset in _offset_candidates(expected, ENTITY_POINTER_WINDOW, 8):
        try:
            pointer = int(mem.qword(entity + offset))
        except Exception:
            continue
        if pointer:
            raw.append((offset, pointer))
        if len(raw) >= MAX_CHARACTER_POINTER_CANDIDATES:
            break

    candidates: list[dict[str, Any]] = []
    seen: set[int] = set()
    for offset, pointer in raw:
        if pointer in seen:
            continue
        seen.add(pointer)
        try:
            evidence = _character_evidence(mem, pointer, profile)
        except Exception as exc:
            evidence = {
                "character_pointer": _hex(pointer),
                "score": 0,
                "error": str(exc),
            }
        candidates.append({
            "offset": _hex(offset),
            "delta_from_expected": offset - expected,
            **evidence,
        })

    candidates.sort(
        key=lambda item: (
            -int(item.get("score") or 0),
            abs(int(item["delta_from_expected"])),
            str(item["offset"]),
        )
    )
    best = candidates[0] if candidates else None
    runner = candidates[1] if len(candidates) > 1 else None
    clear = bool(
        best
        and int(best.get("score") or 0) >= 5
        and (
            runner is None
            or int(best.get("score") or 0) > int(runner.get("score") or 0)
        )
    )
    return {
        "expected_offset": _hex(expected),
        "window_bytes_each_direction": ENTITY_POINTER_WINDOW,
        "candidates": candidates[:MAX_CHARACTER_POINTER_CANDIDATES],
        "winner": best if clear else None,
        "clear_winner": clear,
        "reason": (
            "one_best_character_like_pointer" if clear
            else ("no_character_like_pointer" if not candidates else "character_pointer_candidates_ambiguous")
        ),
    }


def _scan_entity_pointer_offsets(
    mem,
    roster_record: int,
    profile: MemoryProfile,
    trusted_names: set[str],
) -> dict[str, Any]:
    expected = as_int(profile.structure("roster")["entity_pointer_offset"])
    raw: list[tuple[int, int]] = []

    for offset in _offset_candidates(expected, ROSTER_POINTER_WINDOW, 8):
        try:
            pointer = int(mem.qword(roster_record + offset))
        except Exception:
            continue
        if pointer:
            raw.append((offset, pointer))
        if len(raw) >= MAX_ENTITY_POINTER_CANDIDATES:
            break

    candidates: list[dict[str, Any]] = []
    seen: set[int] = set()
    entity_name_expected = as_int(profile.structure("entity")["name_offset"])

    for offset, entity in raw:
        if entity in seen:
            continue
        seen.add(entity)
        name_scan = _scan_exact_name_offsets(
            mem,
            entity,
            expected_offset=entity_name_expected,
            window=ENTITY_NAME_WINDOW,
            trusted_names=trusted_names,
        )
        if not name_scan.get("exact_identity_matches"):
            # Exact identity agreement is the principal Entity proof. Keep only a
            # compact rejected observation rather than recursively scanning every
            # qword-like pointer in record 0.
            candidates.append({
                "offset": _hex(offset),
                "delta_from_expected": offset - expected,
                "entity_pointer": _hex(entity),
                "identity_name_matches": 0,
                "score": 0,
            })
            continue

        character_scan = _scan_character_pointer_offsets(
            mem,
            entity,
            profile,
        )
        score = 6
        if name_scan.get("clear_winner"):
            score += 2
        if character_scan.get("winner"):
            score += 5

        candidates.append({
            "offset": _hex(offset),
            "delta_from_expected": offset - expected,
            "entity_pointer": _hex(entity),
            "identity_name_matches": len(
                name_scan.get("exact_identity_matches") or []
            ),
            "entity_name_offset_scan": name_scan,
            "character_pointer_offset_scan": character_scan,
            "score": score,
        })

    candidates.sort(
        key=lambda item: (
            -int(item.get("score") or 0),
            abs(int(item["delta_from_expected"])),
            str(item["offset"]),
        )
    )
    best = candidates[0] if candidates else None
    runner = candidates[1] if len(candidates) > 1 else None
    clear = bool(
        best
        and int(best.get("score") or 0) >= 8
        and (
            runner is None
            or int(best.get("score") or 0) > int(runner.get("score") or 0)
        )
    )
    return {
        "expected_offset": _hex(expected),
        "window_bytes_each_direction": ROSTER_POINTER_WINDOW,
        "candidates": candidates[:MAX_ENTITY_POINTER_CANDIDATES],
        "winner": best if clear else None,
        "clear_winner": clear,
        "reason": (
            "one_best_identity_agreeing_entity" if clear
            else ("no_identity_agreeing_entity" if not candidates else "entity_pointer_candidates_ambiguous")
        ),
    }



def _joint_raw_header_candidates(
    mem,
    character: int,
    profile: MemoryProfile,
    *,
    kind: str,
) -> list[dict[str, Any]]:
    expected = as_int(
        profile.structure("character")[kind]["collection_offset"]
    )
    candidates: list[dict[str, Any]] = []
    for offset in _offset_candidates(
        expected,
        INVENTORY_HEADER_WINDOW,
        8,
    ):
        item = _raw_inventory_header(
            mem,
            character,
            profile,
            kind=kind,
            collection_offset=offset,
        )
        if not item.get("populated"):
            continue
        try:
            array = int(str(item.get("collection_pointer")), 0)
        except Exception:
            continue
        entries = _entry_pointers(mem, array)
        if len(entries) < 2:
            continue
        item = dict(item)
        item["entry_pointer_count"] = len(entries)
        candidates.append(item)

    # The raw stage is intentionally weak. Distance is only a bounded-work
    # ordering signal; semantic evidence below decides the winner.
    candidates.sort(
        key=lambda item: (
            abs(int(item.get("delta_from_expected") or 0)),
            -int(item.get("entry_pointer_count") or 0),
            str(item.get("offset")),
        )
    )
    return candidates[:MAX_JOINT_RAW_HEADER_CANDIDATES]


def _decode_pair_for_joint(
    mem,
    entries: list[int],
    profile: MemoryProfile,
    *,
    kind: str,
    definition_offset: int,
    name_offset: int,
) -> dict[str, Any] | None:
    max_string = as_int(profile.validation()["max_internal_string"])
    definitions: list[int] = []
    names: list[str] = []
    matches = 0
    mismatches = 0
    neutral = 0

    for entry in entries[:12]:
        try:
            definition = int(mem.qword(entry + definition_offset))
            if not definition:
                return None
            name_ptr = int(mem.qword(definition + name_offset))
            if not name_ptr:
                return None
            name = mem.cstring(name_ptr, max_string).strip()
        except Exception:
            return None
        if not name or not _INTERNAL_NAME_RE.fullmatch(name):
            return None

        classification = _classify_name(name, kind)
        if classification == "match":
            matches += 1
        elif classification == "mismatch":
            mismatches += 1
        else:
            neutral += 1

        definitions.append(definition)
        names.append(name)

    decoded = matches + mismatches + neutral
    if decoded < 2:
        return None

    score = matches * 3 - mismatches * 4 + min(neutral, 2)
    if matches >= 2 and mismatches == 0:
        score += 5

    return {
        "definitions": definitions,
        "names": names,
        "decoded_count": decoded,
        "namespace_matches": matches,
        "namespace_mismatches": mismatches,
        "namespace_neutral": neutral,
        "score": score,
    }


def _recipe_level_joint_candidates(
    mem,
    definitions: list[int],
    names: list[str],
    profile: MemoryProfile,
) -> list[dict[str, Any]]:
    expected = as_int(
        profile.structure("entries")["recipe_level_offset"]
    )
    max_level = as_int(profile.validation()["max_recipe_level"])
    known: list[tuple[int, int]] = []

    for definition, name in zip(definitions, names):
        match = _LEVEL_SUFFIX_RE.search(str(name))
        if not match:
            continue
        level = int(match.group(1))
        if 0 <= level <= max_level:
            known.append((definition, level))

    if len(known) < 2:
        return []

    candidates: list[dict[str, Any]] = []
    for offset in _offset_candidates(expected, ENTRY_FIELD_WINDOW, 4):
        sampled = 0
        matched = 0
        for definition, expected_level in known:
            try:
                value = int(mem.u32(definition + offset))
            except Exception:
                break
            sampled += 1
            if value == expected_level:
                matched += 1
        if sampled < 2 or matched < 2:
            continue
        candidates.append({
            "offset": _hex(offset),
            "delta_from_expected": offset - expected,
            "matched_level_suffixes": matched,
            "sampled": sampled,
            "confidence": round(matched / sampled, 4),
        })

    candidates.sort(
        key=lambda item: (
            -float(item["confidence"]),
            -int(item["matched_level_suffixes"]),
            abs(int(item["delta_from_expected"])),
        )
    )
    return candidates[:MAX_LEVEL_CANDIDATES]


def _joint_hypothesis_public(
    item: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not item:
        return None
    return {
        key: value
        for key, value in item.items()
        if not key.startswith("_")
    }


def _joint_header_entry_hypotheses(
    mem,
    character: int,
    profile: MemoryProfile,
    *,
    kind: str,
) -> dict[str, Any]:
    """Jointly score header and entry-layout hypotheses.

    Unlike the v6.2 type-aware header scan, this path never assumes the signed
    Entry/Definition offsets are correct. It first collects weak but plausible
    populated Character headers, then asks whether a complete entry-layout
    hypothesis can independently explain that header.

    Diagnostic only: no candidate from this function is installed or persisted.
    """
    entry_cfg = profile.structure("entries")
    expected_def = as_int(entry_cfg["definition_pointer_offset"])
    expected_qty = as_int(entry_cfg["quantity_offset"])
    expected_name = as_int(entry_cfg["internal_name_pointer_offset"])
    expected_level = as_int(entry_cfg["recipe_level_offset"])

    hypotheses: list[dict[str, Any]] = []

    for header in _joint_raw_header_candidates(
        mem,
        character,
        profile,
        kind=kind,
    ):
        try:
            array = int(str(header["collection_pointer"]), 0)
            total = int(header["total"])
        except Exception:
            continue

        entries = _entry_pointers(mem, array)
        if len(entries) < 2 or total <= 0:
            continue

        quantity_scan = _scan_quantity_offsets(
            mem,
            entries,
            total,
            profile,
            kind=kind,
        )
        quantity_candidates = (
            quantity_scan.get("exact_header_reproduction_candidates") or []
        )
        if not quantity_candidates:
            continue

        pair_candidates: list[dict[str, Any]] = []
        for definition_offset in _offset_candidates(
            expected_def,
            ENTRY_FIELD_WINDOW,
            8,
        ):
            for name_offset in _offset_candidates(
                expected_name,
                ENTRY_FIELD_WINDOW,
                8,
            ):
                decoded = _decode_pair_for_joint(
                    mem,
                    entries,
                    profile,
                    kind=("recipe" if kind == "recipes" else "salvage"),
                    definition_offset=definition_offset,
                    name_offset=name_offset,
                )
                if not decoded:
                    continue
                if (
                    int(decoded["namespace_matches"]) < 2
                    or int(decoded["namespace_mismatches"]) != 0
                ):
                    continue

                pair_candidates.append({
                    "definition_pointer_offset": _hex(definition_offset),
                    "definition_delta_from_expected": (
                        definition_offset - expected_def
                    ),
                    "internal_name_pointer_offset": _hex(name_offset),
                    "name_delta_from_expected": name_offset - expected_name,
                    "decoded_count": decoded["decoded_count"],
                    "namespace_matches": decoded["namespace_matches"],
                    "namespace_mismatches": decoded["namespace_mismatches"],
                    "namespace_neutral": decoded["namespace_neutral"],
                    "pair_score": decoded["score"],
                    "sample_internal_names": decoded["names"][:8],
                    "_definitions": decoded["definitions"],
                    "_names": decoded["names"],
                })

        if not pair_candidates:
            continue

        pair_candidates.sort(
            key=lambda item: (
                -int(item["pair_score"]),
                -int(item["namespace_matches"]),
                abs(int(item["definition_delta_from_expected"])),
                abs(int(item["name_delta_from_expected"])),
            )
        )
        pair_candidates = pair_candidates[:MAX_ENTRY_PAIR_CANDIDATES]

        for quantity in quantity_candidates[:MAX_QUANTITY_CANDIDATES]:
            for pair in pair_candidates:
                level_best = None
                level_candidates: list[dict[str, Any]] = []
                if kind == "recipes":
                    level_candidates = _recipe_level_joint_candidates(
                        mem,
                        list(pair["_definitions"]),
                        list(pair["_names"]),
                        profile,
                    )
                    if not level_candidates:
                        continue
                    level_best = level_candidates[0]
                    if (
                        float(level_best["confidence"]) < 0.75
                        or int(level_best["matched_level_suffixes"]) < 2
                    ):
                        continue

                score = 10.0  # populated plausible header + >=2 entry pointers
                score += 8.0  # quantity offset exactly reproduces header total
                score += float(pair["pair_score"])
                score += min(
                    8.0,
                    float(pair["namespace_matches"]) * 0.75,
                )
                if kind == "recipes" and level_best is not None:
                    score += 6.0 * float(level_best["confidence"])
                    score += min(
                        4.0,
                        float(level_best["matched_level_suffixes"]),
                    )

                hypotheses.append({
                    "header_offset": header["offset"],
                    "header_delta_from_expected": (
                        header["delta_from_expected"]
                    ),
                    "capacity": header["capacity"],
                    "total": header["total"],
                    "entry_pointer_count": len(entries),
                    "quantity_offset": quantity["offset"],
                    "quantity_delta_from_expected": (
                        quantity["delta_from_expected"]
                    ),
                    "quantity_sum": quantity["quantity_sum"],
                    "definition_pointer_offset": (
                        pair["definition_pointer_offset"]
                    ),
                    "definition_delta_from_expected": (
                        pair["definition_delta_from_expected"]
                    ),
                    "internal_name_pointer_offset": (
                        pair["internal_name_pointer_offset"]
                    ),
                    "name_delta_from_expected": (
                        pair["name_delta_from_expected"]
                    ),
                    "namespace_matches": pair["namespace_matches"],
                    "namespace_mismatches": pair["namespace_mismatches"],
                    "namespace_neutral": pair["namespace_neutral"],
                    "sample_internal_names": pair["sample_internal_names"],
                    "recipe_level_offset": (
                        level_best["offset"]
                        if level_best is not None else None
                    ),
                    "recipe_level_delta_from_expected": (
                        level_best["delta_from_expected"]
                        if level_best is not None else None
                    ),
                    "recipe_level_confidence": (
                        level_best["confidence"]
                        if level_best is not None else None
                    ),
                    "recipe_level_matches": (
                        level_best["matched_level_suffixes"]
                        if level_best is not None else None
                    ),
                    "score": round(score, 3),
                    "strong": True,
                })

    hypotheses.sort(
        key=lambda item: (
            -float(item["score"]),
            -int(item["namespace_matches"]),
            abs(int(item["header_delta_from_expected"])),
            abs(int(item["definition_delta_from_expected"])),
            abs(int(item["name_delta_from_expected"])),
            abs(int(item["quantity_delta_from_expected"])),
        )
    )
    hypotheses = hypotheses[:MAX_JOINT_HYPOTHESIS_RESULTS]

    best = hypotheses[0] if hypotheses else None
    runner = hypotheses[1] if len(hypotheses) > 1 else None
    margin = (
        round(float(best["score"]) - float(runner["score"]), 3)
        if best is not None and runner is not None else None
    )
    clear = bool(
        best
        and (
            runner is None
            or (
                margin is not None
                and margin >= JOINT_HYPOTHESIS_MIN_MARGIN
            )
        )
    )

    return {
        "kind": kind,
        "diagnostic_only": True,
        "raw_header_candidate_limit": MAX_JOINT_RAW_HEADER_CANDIDATES,
        "hypotheses": [
            _joint_hypothesis_public(item)
            for item in hypotheses
        ],
        "winner": {
            "best": _joint_hypothesis_public(best) if clear else None,
            "runner_up": _joint_hypothesis_public(runner),
            "score_margin": margin,
            "clear_winner": clear,
            "reason": (
                "only_strong_joint_hypothesis"
                if best is not None and runner is None
                else (
                    "joint_score_margin_sufficient"
                    if clear
                    else (
                        "no_complete_joint_hypothesis"
                        if best is None
                        else "joint_score_margin_ambiguous"
                    )
                )
            ),
        },
    }

def _raw_header_for_entry_scan(
    mem,
    character: int,
    profile: MemoryProfile,
    *,
    kind: str,
) -> dict[str, Any] | None:
    """Choose an entry-layout anchor without circularly trusting entry offsets.

    Fast path: reuse the validated v4/v5.2 type-aware header scan when the
    signed/current entry layout is still good.

    Fallback: if entry-layout drift prevents that scan from proving a header,
    jointly score Character header + quantity + Definition* + internal-name
    pointer (+ recipe level) hypotheses. This removes the circular dependency
    exposed by v6.2 qualification.

    Diagnostic only. Neither path installs or persists a candidate.
    """
    from .memory_diagnostics import _scan_header_candidates

    cfg = profile.structure("character")[kind]
    expected = as_int(cfg["collection_offset"])
    capacity_delta = as_int(cfg["capacity_offset"]) - expected
    count_delta = as_int(cfg["count_offset"]) - expected
    probe_kind = "recipe" if kind == "recipes" else "salvage"

    typed_scan = _scan_header_candidates(
        mem,
        character,
        profile,
        kind=probe_kind,
        expected_offset=expected,
        capacity_delta=capacity_delta,
        count_delta=count_delta,
    )
    winner = typed_scan.get("winner") or {}
    best = winner.get("best") or {}

    if (
        winner.get("clear_winner")
        and best
        and best.get("classification") == "strong"
        and best.get("recovery_valid")
        and best.get("type_compatible")
    ):
        try:
            offset = int(str(best.get("offset")), 0)
        except Exception:
            offset = -1

        if offset >= 0:
            header = _raw_inventory_header(
                mem,
                character,
                profile,
                kind=kind,
                collection_offset=offset,
            )
            if header.get("populated"):
                header["anchor_source"] = "type_aware_strong_winner"
                header["semantic_anchor_clear"] = True
                header["type_aware_score"] = best.get("score")
                header["type_aware_namespace_matches"] = (
                    best.get("namespace_matches")
                )
                header["type_aware_score_margin"] = (
                    winner.get("score_margin")
                )
                header["type_aware_runner_up_present"] = (
                    winner.get("runner_up") is not None
                )
                header["type_aware_clear_winner"] = True
                return header

    joint = _joint_header_entry_hypotheses(
        mem,
        character,
        profile,
        kind=kind,
    )
    joint_winner = (joint.get("winner") or {}).get("best") or {}
    if not (joint.get("winner") or {}).get("clear_winner"):
        return None
    if not joint_winner:
        return None

    try:
        offset = int(str(joint_winner["header_offset"]), 0)
    except Exception:
        return None

    header = _raw_inventory_header(
        mem,
        character,
        profile,
        kind=kind,
        collection_offset=offset,
    )
    if not header.get("populated"):
        return None

    header["anchor_source"] = "joint_header_entry_hypothesis"
    header["semantic_anchor_clear"] = True
    header["joint_hypothesis_score"] = joint_winner.get("score")
    header["joint_hypothesis_score_margin"] = (
        (joint.get("winner") or {}).get("score_margin")
    )
    header["joint_hypothesis_winner"] = joint_winner
    header["joint_hypothesis_runner_up"] = (
        (joint.get("winner") or {}).get("runner_up")
    )
    header["joint_hypothesis_scan"] = joint
    return header

def _entry_pointers(
    mem,
    array: int,
    *,
    limit: int = MAX_ENTRY_POINTERS,
) -> list[int]:
    pointers: list[int] = []
    for index in range(limit):
        try:
            pointer = int(mem.qword(array + index * 8))
        except Exception:
            break
        if not pointer:
            break
        # ReadProcessMemory is only meaningful for canonical user-mode pointers.
        # CoH's collection arrays can contain allocator poison such as
        # 0xDEDEDEDEDEDEDEDE after the live logical entries; treat that as the
        # end of the usable diagnostic prefix instead of feeding it into layout scans.
        if pointer < 0x10000 or pointer > 0x00007FFFFFFFFFFF:
            break
        pointers.append(pointer)
    return pointers


def _salvage_memberships_for_entries(
    mem,
    entries: list[int],
    profile: MemoryProfile,
    *,
    definition_offset: int,
    name_offset: int,
) -> list[bool] | None:
    """Decode salvage ids once so quantity scans can reproduce invention total."""
    max_string = as_int(profile.validation()["max_internal_string"])
    memberships: list[bool] = []
    for entry in entries:
        try:
            definition = int(mem.qword(entry + definition_offset))
            if not definition:
                return None
            name_ptr = int(mem.qword(definition + name_offset))
            if not name_ptr:
                return None
            name = mem.cstring(name_ptr, max_string).strip()
        except Exception:
            return None
        if not name or not _INTERNAL_NAME_RE.fullmatch(name):
            return None
        membership = default_invention_salvage_membership(name)
        if membership is None:
            return None
        memberships.append(bool(membership))
    return memberships


def _scan_quantity_offsets(
    mem,
    entries: list[int],
    total: int,
    profile: MemoryProfile,
    *,
    kind: str | None = None,
    definition_offset: int | None = None,
    name_offset: int | None = None,
) -> dict[str, Any]:
    cfg = profile.structure("entries")
    expected = as_int(cfg["quantity_offset"])
    semantic_memberships: list[bool] | None = None
    normalized_kind = str(kind or "").casefold()
    if normalized_kind in {"salvage", "salvages"}:
        semantic_memberships = _salvage_memberships_for_entries(
            mem,
            entries,
            profile,
            definition_offset=(
                int(definition_offset)
                if definition_offset is not None
                else as_int(cfg["definition_pointer_offset"])
            ),
            name_offset=(
                int(name_offset)
                if name_offset is not None
                else as_int(cfg["internal_name_pointer_offset"])
            ),
        )

    candidates: list[dict[str, Any]] = []
    for offset in _offset_candidates(expected, ENTRY_FIELD_WINDOW, 4):
        quantity_sum = 0
        used = 0
        ignored = 0
        plausible = True
        values: list[int] = []
        for index, entry in enumerate(entries):
            if (
                semantic_memberships is not None
                and not semantic_memberships[index]
            ):
                ignored += 1
                continue
            try:
                value = int(mem.u32(entry + offset))
            except Exception:
                plausible = False
                break
            if value <= 0 or value > max(total, 1):
                plausible = False
                break
            values.append(value)
            quantity_sum += value
            used += 1
            if semantic_memberships is None and quantity_sum >= total:
                # Preserve the pre-hotfix structural-only behavior when no
                # invention-salvage semantic classifier is available.
                break
            if semantic_memberships is not None and quantity_sum > total:
                plausible = False
                break
        exact = bool(plausible and quantity_sum == total and used > 0)
        if exact:
            candidates.append({
                "offset": _hex(offset),
                "delta_from_expected": offset - expected,
                "quantity_sum": quantity_sum,
                "entries_used": used,
                "sample_values": values[:12],
                "semantic_quantity_filter": (
                    "canonical_invention_salvage"
                    if semantic_memberships is not None
                    else "structural_all_entries"
                ),
                "ignored_non_invention_entries": ignored,
            })
    candidates.sort(
        key=lambda item: (
            abs(int(item["delta_from_expected"])),
            str(item["offset"]),
        )
    )
    return {
        "expected_offset": _hex(expected),
        "window_bytes_each_direction": ENTRY_FIELD_WINDOW,
        "step": 4,
        "semantic_quantity_filter_available": semantic_memberships is not None,
        "exact_header_reproduction_candidates": candidates[:MAX_QUANTITY_CANDIDATES],
        "clear_winner": len(candidates) == 1,
        "winner": candidates[0] if len(candidates) == 1 else None,
        "reason": (
            "one_exact_quantity_offset" if len(candidates) == 1
            else ("no_exact_quantity_offset" if not candidates else "quantity_offsets_ambiguous")
        ),
    }

def _scan_definition_name_pairs(
    mem,
    entries: list[int],
    profile: MemoryProfile,
    *,
    kind: str,
) -> dict[str, Any]:
    cfg = profile.structure("entries")
    expected_def = as_int(cfg["definition_pointer_offset"])
    expected_name = as_int(cfg["internal_name_pointer_offset"])
    max_string = as_int(profile.validation()["max_internal_string"])

    candidates: list[dict[str, Any]] = []
    sample_entries = entries[:12]

    for def_offset in _offset_candidates(expected_def, ENTRY_FIELD_WINDOW, 8):
        definitions: list[int] = []
        valid_defs = True
        for entry in sample_entries:
            try:
                definition = int(mem.qword(entry + def_offset))
            except Exception:
                valid_defs = False
                break
            if not definition:
                valid_defs = False
                break
            definitions.append(definition)
        if not valid_defs or not definitions:
            continue

        for name_offset in _offset_candidates(expected_name, ENTRY_FIELD_WINDOW, 8):
            matches = 0
            mismatches = 0
            neutral = 0
            names: list[str] = []
            decoded_definitions: list[int] = []
            for definition in definitions:
                try:
                    name_ptr = int(mem.qword(definition + name_offset))
                    if not name_ptr:
                        break
                    name = mem.cstring(name_ptr, max_string).strip()
                except Exception:
                    break
                if not name or not _INTERNAL_NAME_RE.fullmatch(name):
                    break
                classification = _classify_name(name, kind)
                if classification == "match":
                    matches += 1
                elif classification == "mismatch":
                    mismatches += 1
                else:
                    neutral += 1
                names.append(name)
                decoded_definitions.append(definition)

            decoded = matches + mismatches + neutral
            if decoded < 2:
                continue
            score = matches * 3 - mismatches * 4 + min(neutral, 2)
            if matches >= 2 and mismatches == 0:
                score += 5
            candidates.append({
                "definition_pointer_offset": _hex(def_offset),
                "definition_delta_from_expected": def_offset - expected_def,
                "internal_name_pointer_offset": _hex(name_offset),
                "name_delta_from_expected": name_offset - expected_name,
                "decoded_count": decoded,
                "namespace_matches": matches,
                "namespace_mismatches": mismatches,
                "namespace_neutral": neutral,
                "score": score,
                "sample_internal_names": names[:8],
                "_definitions": decoded_definitions,
                "_names": names,
            })

    candidates.sort(
        key=lambda item: (
            -int(item["score"]),
            -int(item["namespace_matches"]),
            abs(int(item["definition_delta_from_expected"])),
            abs(int(item["name_delta_from_expected"])),
        )
    )

    public: list[dict[str, Any]] = []
    for item in candidates[:MAX_ENTRY_PAIR_CANDIDATES]:
        public.append({
            key: value
            for key, value in item.items()
            if not key.startswith("_")
        })

    best_internal = candidates[0] if candidates else None
    runner = candidates[1] if len(candidates) > 1 else None
    clear = bool(
        best_internal
        and int(best_internal["namespace_matches"]) >= 2
        and int(best_internal["namespace_mismatches"]) == 0
        and (
            runner is None
            or int(best_internal["score"]) - int(runner["score"]) >= 3
        )
    )
    winner_public = (
        {
            key: value
            for key, value in best_internal.items()
            if not key.startswith("_")
        }
        if clear and best_internal else None
    )
    return {
        "expected_definition_pointer_offset": _hex(expected_def),
        "expected_internal_name_pointer_offset": _hex(expected_name),
        "window_bytes_each_direction": ENTRY_FIELD_WINDOW,
        "candidates": public,
        "winner": winner_public,
        "clear_winner": clear,
        "reason": (
            "one_separated_type_compatible_pair" if clear
            else ("no_decodable_pair" if not candidates else "definition_name_pairs_ambiguous")
        ),
        "_winner_internal": best_internal if clear else None,
    }


def _scan_recipe_level_offsets(
    mem,
    pair_scan: dict[str, Any],
    profile: MemoryProfile,
) -> dict[str, Any]:
    expected = as_int(profile.structure("entries")["recipe_level_offset"])
    winner = pair_scan.get("_winner_internal")
    if not winner:
        return {
            "expected_offset": _hex(expected),
            "candidates": [],
            "clear_winner": False,
            "reason": "no_clear_definition_name_pair",
        }

    definitions = list(winner.get("_definitions") or [])
    names = list(winner.get("_names") or [])
    expected_levels: list[tuple[int, int]] = []
    for definition, name in zip(definitions, names):
        match = _LEVEL_SUFFIX_RE.search(str(name))
        if not match:
            continue
        level = int(match.group(1))
        if 0 <= level <= as_int(profile.validation()["max_recipe_level"]):
            expected_levels.append((definition, level))

    if len(expected_levels) < 2:
        return {
            "expected_offset": _hex(expected),
            "candidates": [],
            "clear_winner": False,
            "reason": "too_few_names_with_level_suffix",
        }

    candidates: list[dict[str, Any]] = []
    for offset in _offset_candidates(expected, ENTRY_FIELD_WINDOW, 4):
        matched = 0
        sampled = 0
        for definition, expected_level in expected_levels:
            try:
                value = int(mem.u32(definition + offset))
            except Exception:
                break
            sampled += 1
            if value == expected_level:
                matched += 1
        if sampled >= 2 and matched >= 2:
            candidates.append({
                "offset": _hex(offset),
                "delta_from_expected": offset - expected,
                "matched_level_suffixes": matched,
                "sampled": sampled,
                "confidence": round(matched / sampled, 4),
            })

    candidates.sort(
        key=lambda item: (
            -float(item["confidence"]),
            -int(item["matched_level_suffixes"]),
            abs(int(item["delta_from_expected"])),
        )
    )
    best = candidates[0] if candidates else None
    runner = candidates[1] if len(candidates) > 1 else None
    clear = bool(
        best
        and float(best["confidence"]) >= 0.75
        and int(best["matched_level_suffixes"]) >= 2
        and (
            runner is None
            or float(best["confidence"]) > float(runner["confidence"])
            or int(best["matched_level_suffixes"]) > int(runner["matched_level_suffixes"])
        )
    )
    return {
        "expected_offset": _hex(expected),
        "window_bytes_each_direction": ENTRY_FIELD_WINDOW,
        "step": 4,
        "candidates": candidates[:MAX_LEVEL_CANDIDATES],
        "winner": best if clear else None,
        "clear_winner": clear,
        "reason": (
            "one_best_level_suffix_match" if clear
            else ("no_level_suffix_match" if not candidates else "level_offsets_ambiguous")
        ),
    }


def _scan_entry_layout(
    mem,
    character: int,
    profile: MemoryProfile,
    *,
    kind: str,
) -> dict[str, Any]:
    header = _raw_header_for_entry_scan(
        mem,
        character,
        profile,
        kind=kind,
    )
    if not header:
        return {
            "kind": kind,
            "available": False,
            "reason": "no_populated_raw_inventory_header",
        }
    array_text = header.get("collection_pointer")
    array = int(str(array_text), 0) if array_text else 0
    total = int(header.get("total") or 0)
    entries = _entry_pointers(mem, array)
    if len(entries) < 2:
        return {
            "kind": kind,
            "available": False,
            "header": header,
            "entry_pointer_count": len(entries),
            "reason": "too_few_entry_pointers",
        }

    pair_kind = "recipe" if kind == "recipes" else "salvage"
    pair = _scan_definition_name_pairs(
        mem,
        entries,
        profile,
        kind=pair_kind,
    )
    pair_winner = pair.get("_winner_internal") or {}
    definition_offset = None
    name_offset = None
    if pair_winner:
        try:
            definition_offset = int(
                str(pair_winner["definition_pointer_offset"]), 0
            )
            name_offset = int(
                str(pair_winner["internal_name_pointer_offset"]), 0
            )
        except Exception:
            definition_offset = None
            name_offset = None

    quantity = _scan_quantity_offsets(
        mem,
        entries,
        total,
        profile,
        kind=kind,
        definition_offset=definition_offset,
        name_offset=name_offset,
    )
    level = (
        _scan_recipe_level_offsets(mem, pair, profile)
        if kind == "recipes"
        else None
    )
    pair.pop("_winner_internal", None)
    return {
        "kind": kind,
        "available": True,
        "header": header,
        "header_anchor_source": header.get("anchor_source"),
        "header_anchor_type_aware": (
            header.get("anchor_source") == "type_aware_strong_winner"
        ),
        "header_anchor_joint_hypothesis": (
            header.get("anchor_source") == "joint_header_entry_hypothesis"
        ),
        "header_anchor_semantic_clear": bool(
            header.get("semantic_anchor_clear")
        ),
        "entry_pointer_count": len(entries),
        "quantity_offset_scan": quantity,
        "definition_and_name_offset_scan": pair,
        "recipe_level_offset_scan": level,
    }

def collect_structural_drift_evidence(
    mem,
    profile: MemoryProfile,
    *,
    trusted_identity_names: set[str],
    roster_observation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect bounded deeper-layout evidence without adopting any candidate."""
    trusted_names = {
        str(value).strip()
        for value in trusted_identity_names
        if _valid_identity_string(str(value).strip(), max_len=63)
    }
    result: dict[str, Any] = {
        "diagnostic_only": True,
        "auto_adopted": False,
        "persistent_changes": False,
        "policy": structural_diagnostic_policy_summary(),
        "trusted_identity_names": sorted(trusted_names),
    }

    anchor = _resolve_roster_anchor(mem, profile, roster_observation)
    result["roster_anchor"] = anchor
    if not anchor.get("resolved"):
        result["status"] = "unavailable"
        result["reason"] = "roster_code_anchor_unavailable"
        return result
    if not trusted_names:
        result["status"] = "limited"
        result["reason"] = "trusted_identity_name_unavailable"
        return result

    roster_base_rva = int(str(anchor["roster_base_rva"]), 0)
    record0 = mem.base + roster_base_rva
    roster_cfg = profile.structure("roster")

    name_scan = _scan_exact_name_offsets(
        mem,
        record0,
        expected_offset=as_int(roster_cfg["name_offset"]),
        window=ROSTER_NAME_WINDOW,
        trusted_names=trusted_names,
    )
    entity_scan = _scan_entity_pointer_offsets(
        mem,
        record0,
        profile,
        trusted_names,
    )
    result["roster_record"] = {
        "name_offset_scan": name_scan,
        "entity_pointer_offset_scan": entity_scan,
    }

    selected_character = None
    entity_winner = entity_scan.get("winner")
    if entity_winner:
        char_scan = entity_winner.get("character_pointer_offset_scan") or {}
        char_winner = char_scan.get("winner")
        if char_winner and char_winner.get("character_pointer"):
            selected_character = int(
                str(char_winner["character_pointer"]), 0
            )

    if selected_character:
        result["character"] = {
            "selected_from_entity_scan": _hex(selected_character),
            "evidence": _character_evidence(
                mem,
                selected_character,
                profile,
            ),
        }
        result["entries"] = {
            "recipes": _scan_entry_layout(
                mem,
                selected_character,
                profile,
                kind="recipes",
            ),
            "salvage": _scan_entry_layout(
                mem,
                selected_character,
                profile,
                kind="salvage",
            ),
        }
    else:
        result["character"] = {
            "selected_from_entity_scan": None,
            "reason": "no_clear_character_pointer_candidate",
        }
        result["entries"] = {
            "recipes": {
                "available": False,
                "reason": "character_pointer_unavailable",
            },
            "salvage": {
                "available": False,
                "reason": "character_pointer_unavailable",
            },
        }

    result["summary"] = {
        "roster_name_clear": bool(name_scan.get("clear_winner")),
        "entity_pointer_clear": bool(entity_scan.get("clear_winner")),
        "character_pointer_clear": bool(
            entity_winner
            and (entity_winner.get("character_pointer_offset_scan") or {}).get(
                "clear_winner"
            )
        ),
        "vitals_common_shift_clear": bool(
            selected_character
            and (
                (result.get("character") or {})
                .get("evidence", {})
                .get("vitals_common_shift", {})
                .get("clear_winner")
            )
        ),
        "recipe_entry_definition_name_clear": bool(
            (result.get("entries", {}).get("recipes", {})
             .get("definition_and_name_offset_scan", {})
             .get("clear_winner"))
        ),
        "salvage_entry_definition_name_clear": bool(
            (result.get("entries", {}).get("salvage", {})
             .get("definition_and_name_offset_scan", {})
             .get("clear_winner"))
        ),
        "recipe_quantity_offset_clear": bool(
            (result.get("entries", {}).get("recipes", {})
             .get("quantity_offset_scan", {})
             .get("clear_winner"))
        ),
        "salvage_quantity_offset_clear": bool(
            (result.get("entries", {}).get("salvage", {})
             .get("quantity_offset_scan", {})
             .get("clear_winner"))
        ),
        "recipe_level_offset_clear": bool(
            (result.get("entries", {}).get("recipes", {})
             .get("recipe_level_offset_scan", {})
             .get("clear_winner"))
        ),
    }
    result["status"] = "collected"
    return result
