from __future__ import annotations

import math
import re
from typing import Any

from .memory_profiles import MemoryProfile, as_int


# FIELD_CRAFTER_MEMORY_STRUCTURAL_DIAGNOSTICS_V6
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


def _raw_header_for_entry_scan(
    mem,
    character: int,
    profile: MemoryProfile,
    *,
    kind: str,
) -> dict[str, Any] | None:
    scan = _scan_raw_inventory_headers(
        mem,
        character,
        profile,
        kind=kind,
    )
    populated = scan.get("populated_candidates") or []
    return populated[0] if populated else None


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
        pointers.append(pointer)
    return pointers


def _scan_quantity_offsets(
    mem,
    entries: list[int],
    total: int,
    profile: MemoryProfile,
) -> dict[str, Any]:
    expected = as_int(profile.structure("entries")["quantity_offset"])
    candidates: list[dict[str, Any]] = []

    for offset in _offset_candidates(expected, ENTRY_FIELD_WINDOW, 4):
        quantity_sum = 0
        used = 0
        plausible = True
        values: list[int] = []
        for entry in entries:
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
            if quantity_sum >= total:
                break
        exact = bool(plausible and quantity_sum == total and used > 0)
        if exact:
            candidates.append({
                "offset": _hex(offset),
                "delta_from_expected": offset - expected,
                "quantity_sum": quantity_sum,
                "entries_used": used,
                "sample_values": values[:12],
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

    quantity = _scan_quantity_offsets(
        mem,
        entries,
        total,
        profile,
    )
    pair = _scan_definition_name_pairs(
        mem,
        entries,
        profile,
        kind=("recipe" if kind == "recipes" else "salvage"),
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
