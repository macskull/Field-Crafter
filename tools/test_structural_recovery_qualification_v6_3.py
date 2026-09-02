#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

from field_crafter_test_output import print_result_path, write_test_json


# FIELD_CRAFTER_MEMORY_STRUCTURAL_QUALIFICATION_V6_1
# FIELD_CRAFTER_MEMORY_STRUCTURAL_QUALIFICATION_V6_2
# FIELD_CRAFTER_MEMORY_STRUCTURAL_QUALIFICATION_V6_3

SAMPLE_COUNT = 3
QUALIFICATION_VERSION = "6.3"
SAMPLE_DELAY_SECONDS = 0.06

STRUCTURAL_DRIFT = {
    "roster_name": 0x40,
    "roster_entity_pointer": 0x40,
    "entity_name": 0x80,
    "entity_character_pointer": 0x40,
    "character_vitals": 0x40,
    "entry_definition_pointer": 0x10,
    "entry_quantity": 0x10,
    "entry_internal_name_pointer": 0x10,
    "entry_recipe_level": 0x10,
}
INVENTORY_DRIFT_BYTES = 0x100
ROOT_DRIFT_LOCATORS = ("identity", "server", "roster")


def _find_root(start: Path) -> Path:
    start = start.resolve()
    for root in [start] + list(start.parents):
        if (root / "src" / "hc_recipe_db" / "game_memory.py").exists():
            return root
    raise RuntimeError(
        "Could not find Field_Crafter_1.16_Build_Source_DEV. "
        "Run from the source root or pass --root."
    )


def _mutate_one_fixed_byte(pattern: str) -> str:
    tokens = pattern.split()
    fixed = [
        index
        for index, token in enumerate(tokens)
        if token not in {"?", "??"}
    ]
    if len(fixed) < 12:
        raise RuntimeError(
            "Signature has too few fixed bytes for the v6.1 qualification harness."
        )
    index = fixed[0]
    tokens[index] = f"{int(tokens[index], 16) ^ 0x01:02X}"
    return " ".join(tokens)


def _make_scenario_profile(
    original,
    *,
    root_drift: bool = False,
    inventory_drift: bool = False,
    roster_drift: bool = False,
    entity_drift: bool = False,
    vitals_drift: bool = False,
    entry_drift: bool = False,
):
    from hc_recipe_db.memory_profiles import MemoryProfile, as_int

    data = copy.deepcopy(original.data)
    expected: dict[str, Any] = {
        "roster_name_delta": 0,
        "roster_entity_pointer_delta": 0,
        "entity_name_delta": 0,
        "entity_character_pointer_delta": 0,
        "vitals_shift": 0,
        "recipe_definition_delta": 0,
        "salvage_definition_delta": 0,
        "recipe_name_pointer_delta": 0,
        "salvage_name_pointer_delta": 0,
        "recipe_quantity_delta": 0,
        "salvage_quantity_delta": 0,
        "recipe_level_delta": 0,
        "recipe_header_delta": 0,
        "salvage_header_delta": 0,
    }

    if root_drift:
        for locator_name in ROOT_DRIFT_LOCATORS:
            locator = data["locators"][locator_name]
            locator["pattern"] = _mutate_one_fixed_byte(
                str(locator["pattern"])
            )

    if roster_drift:
        roster = data["structures"]["roster"]
        roster["name_offset"] = (
            as_int(roster["name_offset"])
            + STRUCTURAL_DRIFT["roster_name"]
        )
        roster["entity_pointer_offset"] = (
            as_int(roster["entity_pointer_offset"])
            + STRUCTURAL_DRIFT["roster_entity_pointer"]
        )
        expected["roster_name_delta"] = -STRUCTURAL_DRIFT["roster_name"]
        expected["roster_entity_pointer_delta"] = (
            -STRUCTURAL_DRIFT["roster_entity_pointer"]
        )

    if entity_drift:
        entity = data["structures"]["entity"]
        entity["name_offset"] = (
            as_int(entity["name_offset"])
            + STRUCTURAL_DRIFT["entity_name"]
        )
        entity["character_pointer_offset"] = (
            as_int(entity["character_pointer_offset"])
            + STRUCTURAL_DRIFT["entity_character_pointer"]
        )
        expected["entity_name_delta"] = -STRUCTURAL_DRIFT["entity_name"]
        expected["entity_character_pointer_delta"] = (
            -STRUCTURAL_DRIFT["entity_character_pointer"]
        )

    if vitals_drift:
        character = data["structures"]["character"]
        for key in (
            "current_hp_offset",
            "current_end_offset",
            "max_hp_offset",
            "max_end_offset",
        ):
            character[key] = (
                as_int(character[key])
                + STRUCTURAL_DRIFT["character_vitals"]
            )
        expected["vitals_shift"] = -STRUCTURAL_DRIFT["character_vitals"]

    if entry_drift:
        entries = data["structures"]["entries"]
        entries["definition_pointer_offset"] = (
            as_int(entries["definition_pointer_offset"])
            + STRUCTURAL_DRIFT["entry_definition_pointer"]
        )
        entries["quantity_offset"] = (
            as_int(entries["quantity_offset"])
            + STRUCTURAL_DRIFT["entry_quantity"]
        )
        entries["internal_name_pointer_offset"] = (
            as_int(entries["internal_name_pointer_offset"])
            + STRUCTURAL_DRIFT["entry_internal_name_pointer"]
        )
        entries["recipe_level_offset"] = (
            as_int(entries["recipe_level_offset"])
            + STRUCTURAL_DRIFT["entry_recipe_level"]
        )
        expected["recipe_definition_delta"] = (
            -STRUCTURAL_DRIFT["entry_definition_pointer"]
        )
        expected["salvage_definition_delta"] = (
            -STRUCTURAL_DRIFT["entry_definition_pointer"]
        )
        expected["recipe_name_pointer_delta"] = (
            -STRUCTURAL_DRIFT["entry_internal_name_pointer"]
        )
        expected["salvage_name_pointer_delta"] = (
            -STRUCTURAL_DRIFT["entry_internal_name_pointer"]
        )
        expected["recipe_quantity_delta"] = (
            -STRUCTURAL_DRIFT["entry_quantity"]
        )
        expected["salvage_quantity_delta"] = (
            -STRUCTURAL_DRIFT["entry_quantity"]
        )
        expected["recipe_level_delta"] = (
            -STRUCTURAL_DRIFT["entry_recipe_level"]
        )

    if inventory_drift:
        character = data["structures"]["character"]
        for kind, key in (
            ("recipes", "recipe_header_delta"),
            ("salvage", "salvage_header_delta"),
        ):
            cfg = character[kind]
            collection = as_int(cfg["collection_offset"])
            capacity_delta = as_int(cfg["capacity_offset"]) - collection
            count_delta = as_int(cfg["count_offset"]) - collection
            shifted = collection + INVENTORY_DRIFT_BYTES
            cfg["collection_offset"] = shifted
            cfg["capacity_offset"] = shifted + capacity_delta
            cfg["count_offset"] = shifted + count_delta
            expected[key] = -INVENTORY_DRIFT_BYTES

    tags = []
    for label, enabled in (
        ("root", root_drift),
        ("inventory", inventory_drift),
        ("roster", roster_drift),
        ("entity", entity_drift),
        ("vitals", vitals_drift),
        ("entries", entry_drift),
    ):
        if enabled:
            tags.append(label)

    profile = MemoryProfile(
        profile_id=original.profile_id,
        profile_version=original.profile_version,
        priority=original.priority,
        source=(
            f"{original.source}+qualification-"
            + ("-".join(tags) if tags else "baseline")
        ),
        data=data,
    )
    return profile, expected


def _score_candidate(item: dict[str, Any], mode: str) -> float:
    if mode == "score":
        return float(item.get("score") or 0.0)
    if mode == "confidence":
        return (
            float(item.get("confidence") or 0.0) * 1000.0
            + float(item.get("matched_level_suffixes") or 0.0)
        )
    # Exact semantic candidates (trusted-name equality or exact quantity
    # reproduction) have equal semantic strength. Uniqueness is therefore the
    # discriminant rather than an invented score.
    return 1.0


def _candidate_key(item: dict[str, Any], fields: tuple[str, ...]) -> tuple[Any, ...]:
    return tuple(item.get(field) for field in fields)


def _qualify_candidates(
    candidates: list[dict[str, Any]],
    *,
    expected_key: tuple[Any, ...],
    key_fields: tuple[str, ...],
    score_mode: str,
    clear_winner: bool,
    winner: dict[str, Any] | None,
) -> dict[str, Any]:
    public_candidates = list(candidates or [])
    scored = [
        (_score_candidate(item, score_mode), item)
        for item in public_candidates
    ]
    scored.sort(
        key=lambda pair: (
            -pair[0],
            str(_candidate_key(pair[1], key_fields)),
        )
    )

    expected_item = next(
        (
            item
            for item in public_candidates
            if _candidate_key(item, key_fields) == expected_key
        ),
        None,
    )
    best_score = scored[0][0] if scored else None
    runner_score = scored[1][0] if len(scored) > 1 else None
    margin = (
        best_score - runner_score
        if best_score is not None and runner_score is not None
        else None
    )
    expected_score = (
        _score_candidate(expected_item, score_mode)
        if expected_item is not None
        else None
    )
    expected_is_best = bool(
        expected_item is not None
        and best_score is not None
        and math.isclose(expected_score, best_score)
    )
    winner_key = (
        _candidate_key(winner, key_fields)
        if winner else None
    )
    expected_is_clear_winner = bool(
        clear_winner and winner_key == expected_key
    )

    return {
        "found": expected_item is not None,
        "candidate_count": len(public_candidates),
        "unique_candidate": len(public_candidates) == 1,
        "expected_is_best_scoring": expected_is_best,
        "clear_winner": bool(clear_winner),
        "expected_is_clear_winner": expected_is_clear_winner,
        "expected_score": expected_score,
        "best_score": best_score,
        "runner_up_score": runner_score,
        "margin_vs_runner_up": margin,
        "best_candidate_key": (
            list(_candidate_key(scored[0][1], key_fields))
            if scored else None
        ),
        "expected_key": list(expected_key),
    }


def _find_entity_candidate(
    evidence: dict[str, Any],
    expected_delta: int,
) -> dict[str, Any] | None:
    entity_scan = (
        evidence.get("roster_record", {})
        .get("entity_pointer_offset_scan", {})
    )
    for item in entity_scan.get("candidates") or []:
        try:
            if int(item.get("delta_from_expected")) == expected_delta:
                return item
        except Exception:
            continue
    return None


def _find_character_candidate(
    entity_candidate: dict[str, Any] | None,
    expected_delta: int,
) -> dict[str, Any] | None:
    if not entity_candidate:
        return None
    scan = entity_candidate.get("character_pointer_offset_scan") or {}
    for item in scan.get("candidates") or []:
        try:
            if int(item.get("delta_from_expected")) == expected_delta:
                return item
        except Exception:
            continue
    return None


def _extract_sample_metrics(
    evidence: dict[str, Any],
    expected: dict[str, Any],
) -> dict[str, Any]:
    record = evidence.get("roster_record") or {}
    name_scan = record.get("name_offset_scan") or {}
    entity_scan = record.get("entity_pointer_offset_scan") or {}

    entity_candidate = _find_entity_candidate(
        evidence,
        int(expected["roster_entity_pointer_delta"]),
    )
    entity_name_scan = (
        entity_candidate.get("entity_name_offset_scan") or {}
        if entity_candidate else {}
    )
    char_scan = (
        entity_candidate.get("character_pointer_offset_scan") or {}
        if entity_candidate else {}
    )
    char_candidate = _find_character_candidate(
        entity_candidate,
        int(expected["entity_character_pointer_delta"]),
    )
    vitals_scan = (
        char_candidate.get("vitals_common_shift") or {}
        if char_candidate else {}
    )

    entries = evidence.get("entries") or {}
    recipe = entries.get("recipes") or {}
    salvage = entries.get("salvage") or {}

    metrics: dict[str, Any] = {}

    metrics["roster_name"] = _qualify_candidates(
        name_scan.get("exact_identity_matches") or [],
        expected_key=(int(expected["roster_name_delta"]),),
        key_fields=("delta_from_expected",),
        score_mode="exact",
        clear_winner=bool(name_scan.get("clear_winner")),
        winner=name_scan.get("winner"),
    )
    metrics["roster_entity_pointer"] = _qualify_candidates(
        entity_scan.get("candidates") or [],
        expected_key=(int(expected["roster_entity_pointer_delta"]),),
        key_fields=("delta_from_expected",),
        score_mode="score",
        clear_winner=bool(entity_scan.get("clear_winner")),
        winner=entity_scan.get("winner"),
    )
    metrics["entity_name"] = _qualify_candidates(
        entity_name_scan.get("exact_identity_matches") or [],
        expected_key=(int(expected["entity_name_delta"]),),
        key_fields=("delta_from_expected",),
        score_mode="exact",
        clear_winner=bool(entity_name_scan.get("clear_winner")),
        winner=entity_name_scan.get("winner"),
    )
    metrics["entity_character_pointer"] = _qualify_candidates(
        char_scan.get("candidates") or [],
        expected_key=(int(expected["entity_character_pointer_delta"]),),
        key_fields=("delta_from_expected",),
        score_mode="score",
        clear_winner=bool(char_scan.get("clear_winner")),
        winner=char_scan.get("winner"),
    )
    metrics["character_vitals_common_shift"] = _qualify_candidates(
        vitals_scan.get("candidates") or [],
        expected_key=(int(expected["vitals_shift"]),),
        key_fields=("shift",),
        score_mode="score",
        clear_winner=bool(vitals_scan.get("clear_winner")),
        winner=vitals_scan.get("winner"),
    )

    for kind, block, prefix in (
        ("recipes", recipe, "recipe"),
        ("salvage", salvage, "salvage"),
    ):
        header = block.get("header") or {}
        header_expected = int(expected[f"{prefix}_header_delta"])
        header_found = (
            bool(block.get("available"))
            and int(header.get("delta_from_expected") or 0)
            == header_expected
        )
        header_clear = bool(
            block.get("header_anchor_semantic_clear")
        )
        anchor_source = block.get("header_anchor_source")
        if anchor_source == "type_aware_strong_winner":
            header_score = header.get("type_aware_score")
            header_margin = header.get("type_aware_score_margin")
        elif anchor_source == "joint_header_entry_hypothesis":
            header_score = header.get("joint_hypothesis_score")
            header_margin = header.get("joint_hypothesis_score_margin")
        else:
            header_score = None
            header_margin = None

        metrics[f"{prefix}_header"] = {
            "found": header_found,
            "candidate_count": 1 if header_clear else 0,
            "unique_candidate": header_clear,
            "expected_is_best_scoring": header_found,
            "clear_winner": header_clear,
            "expected_is_clear_winner": bool(header_found and header_clear),
            "expected_score": header_score,
            "best_score": header_score,
            "runner_up_score": None,
            "margin_vs_runner_up": header_margin,
            "best_candidate_key": (
                [int(header.get("delta_from_expected") or 0)]
                if block.get("available") else None
            ),
            "expected_key": [header_expected],
            "anchor_source": anchor_source,
            "used_joint_hypothesis": bool(
                block.get("header_anchor_joint_hypothesis")
            ),
        }

        quantity = block.get("quantity_offset_scan") or {}
        metrics[f"{prefix}_quantity"] = _qualify_candidates(
            quantity.get("exact_header_reproduction_candidates") or [],
            expected_key=(int(expected[f"{prefix}_quantity_delta"]),),
            key_fields=("delta_from_expected",),
            score_mode="exact",
            clear_winner=bool(quantity.get("clear_winner")),
            winner=quantity.get("winner"),
        )

        pair = block.get("definition_and_name_offset_scan") or {}
        metrics[f"{prefix}_definition_name_pair"] = _qualify_candidates(
            pair.get("candidates") or [],
            expected_key=(
                int(expected[f"{prefix}_definition_delta"]),
                int(expected[f"{prefix}_name_pointer_delta"]),
            ),
            key_fields=(
                "definition_delta_from_expected",
                "name_delta_from_expected",
            ),
            score_mode="score",
            clear_winner=bool(pair.get("clear_winner")),
            winner=pair.get("winner"),
        )

    recipe_level = recipe.get("recipe_level_offset_scan") or {}
    metrics["recipe_level"] = _qualify_candidates(
        recipe_level.get("candidates") or [],
        expected_key=(int(expected["recipe_level_delta"]),),
        key_fields=("delta_from_expected",),
        score_mode="confidence",
        clear_winner=bool(recipe_level.get("clear_winner")),
        winner=recipe_level.get("winner"),
    )

    return metrics


def _aggregate_signal(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if not samples:
        return {
            "found_all_samples": False,
            "best_all_samples": False,
            "clear_winner_all_samples": False,
            "expected_clear_winner_all_samples": False,
            "stable_best_candidate": False,
            "minimum_margin_vs_runner_up": None,
            "candidate_counts": [],
            "qualification": "not_observed",
        }

    best_keys = [
        tuple(item["best_candidate_key"])
        if item.get("best_candidate_key") is not None else None
        for item in samples
    ]
    margins = [
        float(item["margin_vs_runner_up"])
        for item in samples
        if item.get("margin_vs_runner_up") is not None
    ]

    found_all = all(bool(item.get("found")) for item in samples)
    best_all = all(
        bool(item.get("expected_is_best_scoring"))
        for item in samples
    )
    clear_all = all(bool(item.get("clear_winner")) for item in samples)
    expected_clear_all = all(
        bool(item.get("expected_is_clear_winner"))
        for item in samples
    )
    stable_best = (
        all(key is not None for key in best_keys)
        and len(set(best_keys)) == 1
    )

    if (
        found_all
        and best_all
        and expected_clear_all
        and stable_best
    ):
        qualification = "strong"
    elif found_all and best_all and stable_best:
        qualification = "best_but_not_uniquely_proven"
    elif found_all:
        qualification = "retained_but_not_consistently_best"
    else:
        qualification = "known_good_candidate_lost"

    return {
        "found_all_samples": found_all,
        "best_all_samples": best_all,
        "clear_winner_all_samples": clear_all,
        "expected_clear_winner_all_samples": expected_clear_all,
        "stable_best_candidate": stable_best,
        "minimum_margin_vs_runner_up": min(margins) if margins else None,
        "candidate_counts": [
            int(item.get("candidate_count") or 0)
            for item in samples
        ],
        "qualification": qualification,
    }


def _aggregate_scenario(
    samples: list[dict[str, Any]],
) -> dict[str, Any]:
    signal_names = sorted(
        {
            name
            for sample in samples
            for name in sample.keys()
        }
    )
    signals = {
        name: _aggregate_signal(
            [sample.get(name, {}) for sample in samples]
        )
        for name in signal_names
    }
    qualifications = [
        item["qualification"] for item in signals.values()
    ]
    return {
        "signals": signals,
        "all_known_good_fields_retained": all(
            item["found_all_samples"] for item in signals.values()
        ),
        "all_known_good_fields_best": all(
            item["best_all_samples"] for item in signals.values()
        ),
        "all_scored_fields_stable": all(
            item["stable_best_candidate"] for item in signals.values()
        ),
        "strong_signal_count": sum(
            1 for value in qualifications if value == "strong"
        ),
        "non_strong_signals": [
            name
            for name, item in signals.items()
            if item["qualification"] != "strong"
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Field Crafter v6.1 live structural-recovery qualification harness. "
            "No candidates are auto-adopted or persisted."
        )
    )
    parser.add_argument("--root", default=".", help="Field Crafter 1.16 DEV root")
    parser.add_argument(
        "--pid",
        type=int,
        help="cityofheroes.exe PID; omit when exactly one client is running",
    )
    args = parser.parse_args()

    try:
        root = _find_root(Path(args.root))
        sys.path.insert(0, str(root / "src"))

        from hc_recipe_db.game_memory import (
            ProcessMemory,
            _resolve_memory_context,
            list_city_of_heroes_processes,
        )
        from hc_recipe_db.memory_profiles import MemoryProfileManager
        from hc_recipe_db.memory_root_recovery import (
            _resolve_identity_semantics,
            _resolve_server_semantics,
            _select_code_candidate,
        )
        from hc_recipe_db.memory_structural_diagnostics import (
            collect_structural_drift_evidence,
            structural_diagnostic_policy_summary,
        )

        processes = list_city_of_heroes_processes()
        if args.pid is None:
            if not processes:
                raise RuntimeError("No running cityofheroes.exe clients were found.")
            if len(processes) != 1:
                print("Multiple clients are running. Re-run with --pid:", file=sys.stderr)
                for proc in processes:
                    print(f"  {proc.label}", file=sys.stderr)
                return 2
            pid = int(processes[0].pid)
        else:
            pid = int(args.pid)

        manager = MemoryProfileManager()
        with ProcessMemory(pid) as mem:
            exact = _resolve_memory_context(
                mem,
                manager,
                require_server=True,
            )
            original = exact.profile

        all_structural = {
            "roster_drift": True,
            "entity_drift": True,
            "vitals_drift": True,
            "entry_drift": True,
        }
        scenarios = [
            ("A_baseline", {}),
            ("B1_roster_only", {"roster_drift": True}),
            ("B2_entity_only", {"entity_drift": True}),
            ("B3_vitals_only", {"vitals_drift": True}),
            ("B4_entries_only", {"entry_drift": True}),
            ("C_all_structural", dict(all_structural)),
            (
                "D_root_plus_structural",
                {"root_drift": True, **all_structural},
            ),
            (
                "E_inventory_plus_structural",
                {"inventory_drift": True, **all_structural},
            ),
            (
                "F_root_inventory_structural",
                {
                    "root_drift": True,
                    "inventory_drift": True,
                    **all_structural,
                },
            ),
        ]

        scenario_results: dict[str, Any] = {}
        for scenario_name, flags in scenarios:
            profile, expected = _make_scenario_profile(
                original,
                **flags,
            )
            root_qualification = None
            trusted_name = exact.character_name

            # When root signatures and deeper structures drift together, the
            # existing v5 *full* root recovery is expected to fail closed because
            # its downstream roster/Entity/Character proof uses the old structural
            # offsets. For qualification we therefore recover only the independently
            # provable root pieces: identity code+name semantics, selected-server
            # code+string semantics, and the roster code anchor. v6 then uses the
            # recovered identity name to search for the moved structure fields.
            if flags.get("root_drift"):
                with ProcessMemory(pid) as mem:
                    identity_code = _select_code_candidate(
                        mem, profile, "identity"
                    )
                    server_code = _select_code_candidate(
                        mem, profile, "server"
                    )
                    roster_code = _select_code_candidate(
                        mem, profile, "roster"
                    )
                    identity_semantics = _resolve_identity_semantics(
                        mem, profile, identity_code
                    )
                    server_semantics = _resolve_server_semantics(
                        mem, profile, server_code
                    )

                code_items = (
                    identity_code,
                    server_code,
                    roster_code,
                )
                root_qualification = {
                    "recovered_locators": [
                        item.locator
                        for item in code_items
                        if item.source == "nearest_masked_landmark"
                    ],
                    "all_three_nearest_landmarks": all(
                        item.source == "nearest_masked_landmark"
                        for item in code_items
                    ),
                    "mismatch_counts": {
                        item.locator: item.mismatch_count
                        for item in code_items
                    },
                    "identity_name_matches_baseline": (
                        str(identity_semantics["name"])
                        == exact.character_name
                    ),
                    "server_matches_baseline": (
                        str(server_semantics["server"]) == exact.server
                    ),
                    "roster_code_anchor_recovered": (
                        roster_code.source == "nearest_masked_landmark"
                    ),
                    "deeper_root_structure_auto_recovery_attempted": False,
                }
                if not all(
                    [
                        root_qualification["all_three_nearest_landmarks"],
                        root_qualification["identity_name_matches_baseline"],
                        root_qualification["server_matches_baseline"],
                        root_qualification["roster_code_anchor_recovered"],
                    ]
                ):
                    raise RuntimeError(
                        f"{scenario_name}: independent root code/name "
                        "qualification did not match the exact baseline."
                    )
                trusted_name = str(identity_semantics["name"])

            sample_metrics: list[dict[str, Any]] = []
            structural_statuses: list[str] = []
            structural_summaries: list[dict[str, Any]] = []

            for sample_index in range(SAMPLE_COUNT):
                with ProcessMemory(pid) as mem:
                    evidence = collect_structural_drift_evidence(
                        mem,
                        profile,
                        trusted_identity_names={trusted_name},
                        roster_observation=None,
                    )
                structural_statuses.append(
                    str(evidence.get("status") or "")
                )
                structural_summaries.append(
                    dict(evidence.get("summary") or {})
                )
                if evidence.get("status") != "collected":
                    raise RuntimeError(
                        f"{scenario_name}: structural observation sample "
                        f"{sample_index + 1}/{SAMPLE_COUNT} was "
                        f"{evidence.get('status')}: {evidence.get('reason')}"
                    )

                sample_metrics.append(
                    _extract_sample_metrics(evidence, expected)
                )
                if sample_index + 1 < SAMPLE_COUNT:
                    time.sleep(SAMPLE_DELAY_SECONDS)

            aggregate = _aggregate_scenario(sample_metrics)
            scenario_results[scenario_name] = {
                "flags": flags,
                "expected_candidate_deltas": expected,
                "root_qualification": root_qualification,
                "structural_statuses": structural_statuses,
                "schema3_clear_winner_summaries": structural_summaries,
                "sample_count": SAMPLE_COUNT,
                **aggregate,
            }

        signal_names = sorted(
            {
                signal
                for scenario in scenario_results.values()
                for signal in scenario["signals"]
            }
        )
        cross_scenario: dict[str, Any] = {}
        for signal in signal_names:
            per_scenario = {
                name: scenario["signals"].get(signal)
                for name, scenario in scenario_results.items()
                if signal in scenario["signals"]
            }
            cross_scenario[signal] = {
                "qualified_strong_in": [
                    name
                    for name, item in per_scenario.items()
                    if item["qualification"] == "strong"
                ],
                "not_strong_in": [
                    name
                    for name, item in per_scenario.items()
                    if item["qualification"] != "strong"
                ],
                "retained_in_all_scenarios": all(
                    item["found_all_samples"]
                    for item in per_scenario.values()
                ),
                "best_in_all_scenarios": all(
                    item["best_all_samples"]
                    for item in per_scenario.values()
                ),
                "stable_best_in_all_scenarios": all(
                    item["stable_best_candidate"]
                    for item in per_scenario.values()
                ),
                "minimum_observed_margin_vs_runner_up": min(
                    [
                        float(item["minimum_margin_vs_runner_up"])
                        for item in per_scenario.values()
                        if item["minimum_margin_vs_runner_up"] is not None
                    ],
                    default=None,
                ),
            }

        policy = structural_diagnostic_policy_summary()
        safety_ok = bool(
            policy.get("diagnostic_only") is True
            and policy.get("auto_adopted") is False
            and policy.get("persistent") is False
        )
        if not safety_ok:
            raise RuntimeError(
                f"Unexpected v6 structural diagnostic policy: {policy!r}"
            )

        result = {
            "passed": True,
            "qualification_harness_version": QUALIFICATION_VERSION,
            "profile_id": original.profile_id,
            "profile_version": original.profile_version,
            "character_name": exact.character_name,
            "server": exact.server,
            "scenario_count": len(scenarios),
            "sample_count_per_scenario": SAMPLE_COUNT,
            "sample_delay_seconds": SAMPLE_DELAY_SECONDS,
            "scenarios": scenario_results,
            "cross_scenario_signal_qualification": cross_scenario,
            "all_known_good_fields_retained_everywhere": all(
                scenario["all_known_good_fields_retained"]
                for scenario in scenario_results.values()
            ),
            "all_known_good_fields_best_everywhere": all(
                scenario["all_known_good_fields_best"]
                for scenario in scenario_results.values()
            ),
            "all_best_candidates_stable_everywhere": all(
                scenario["all_scored_fields_stable"]
                for scenario in scenario_results.values()
            ),
            "joint_entry_hypothesis_expected_when_entry_layout_drifts": True,
            "joint_entry_hypothesis_strong_in_entry_drift_scenarios": all(
                scenario_results[name]["signals"]["recipe_header"]["qualification"] == "strong"
                and scenario_results[name]["signals"]["salvage_header"]["qualification"] == "strong"
                for name in (
                    "B4_entries_only",
                    "C_all_structural",
                    "D_root_plus_structural",
                    "E_inventory_plus_structural",
                    "F_root_inventory_structural",
                )
            ),
            "all_entry_layout_signals_strong_in_entry_drift_scenarios": all(
                scenario_results[name]["signals"][signal]["qualification"] == "strong"
                for name in (
                    "B4_entries_only",
                    "C_all_structural",
                    "D_root_plus_structural",
                    "E_inventory_plus_structural",
                    "F_root_inventory_structural",
                )
                for signal in (
                    "recipe_header",
                    "recipe_definition_name_pair",
                    "recipe_quantity",
                    "recipe_level",
                    "salvage_header",
                    "salvage_definition_name_pair",
                    "salvage_quantity",
                )
            ),
            "auto_adopted": False,
            "persistent_changes": False,
            "policy": policy,
        }
        output_path = write_test_json(
            "structural_qualification_v6_3",
            pid,
            result,
        )
        print_result_path(output_path, passed=True)
        return 0

    except Exception as exc:
        failure = {
            "passed": False,
            "qualification_harness_version": QUALIFICATION_VERSION,
            "error": str(exc),
            "auto_adopted": False,
            "persistent_changes": False,
        }
        try:
            output_path = write_test_json(
                "structural_qualification_v6_3",
                args.pid,
                failure,
            )
            print_result_path(output_path, passed=False)
        except Exception:
            print(f"QUALIFICATION HARNESS FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
