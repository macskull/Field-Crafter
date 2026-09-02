#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path


# FIELD_CRAFTER_MEMORY_STRUCTURAL_DIAGNOSTICS_V6_TEST

DRIFT = {
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


def _find_root(start: Path) -> Path:
    start = start.resolve()
    for root in [start] + list(start.parents):
        if (root / "src" / "hc_recipe_db" / "game_memory.py").exists():
            return root
    raise RuntimeError(
        "Could not find Field_Crafter_1.16_Build_Source_DEV. "
        "Run from the source root or pass --root."
    )


def _has_delta(items, key: str, expected_delta: int) -> bool:
    for item in items or []:
        try:
            if int(item.get(key)) == int(expected_delta):
                return True
        except Exception:
            continue
    return False


def _pair_has_deltas(
    items,
    *,
    definition_delta: int,
    name_delta: int,
) -> bool:
    for item in items or []:
        try:
            if (
                int(item.get("definition_delta_from_expected"))
                == int(definition_delta)
                and int(item.get("name_delta_from_expected"))
                == int(name_delta)
            ):
                return True
        except Exception:
            continue
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Live, non-persistent self-test for Field Crafter v6 bounded "
            "structural-drift diagnostics."
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
        from hc_recipe_db.memory_diagnostics import collect_memory_diagnostic
        from hc_recipe_db.memory_profiles import (
            MemoryProfile,
            MemoryProfileManager,
            as_int,
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
            data = copy.deepcopy(original.data)

            roster = data["structures"]["roster"]
            roster["name_offset"] = (
                as_int(roster["name_offset"]) + DRIFT["roster_name"]
            )
            roster["entity_pointer_offset"] = (
                as_int(roster["entity_pointer_offset"])
                + DRIFT["roster_entity_pointer"]
            )

            entity = data["structures"]["entity"]
            entity["name_offset"] = (
                as_int(entity["name_offset"]) + DRIFT["entity_name"]
            )
            entity["character_pointer_offset"] = (
                as_int(entity["character_pointer_offset"])
                + DRIFT["entity_character_pointer"]
            )

            character = data["structures"]["character"]
            for key in (
                "current_hp_offset",
                "current_end_offset",
                "max_hp_offset",
                "max_end_offset",
            ):
                character[key] = (
                    as_int(character[key]) + DRIFT["character_vitals"]
                )

            entries = data["structures"]["entries"]
            entries["definition_pointer_offset"] = (
                as_int(entries["definition_pointer_offset"])
                + DRIFT["entry_definition_pointer"]
            )
            entries["quantity_offset"] = (
                as_int(entries["quantity_offset"])
                + DRIFT["entry_quantity"]
            )
            entries["internal_name_pointer_offset"] = (
                as_int(entries["internal_name_pointer_offset"])
                + DRIFT["entry_internal_name_pointer"]
            )
            entries["recipe_level_offset"] = (
                as_int(entries["recipe_level_offset"])
                + DRIFT["entry_recipe_level"]
            )

            drifted = MemoryProfile(
                profile_id=original.profile_id,
                profile_version=original.profile_version,
                priority=original.priority,
                source=f"{original.source}+structural-self-test-drift",
                data=data,
            )

            evidence = collect_structural_drift_evidence(
                mem,
                drifted,
                trusted_identity_names={exact.character_name},
                roster_observation=None,
            )

        if evidence.get("status") != "collected":
            raise RuntimeError(
                f"Structural evidence was not collected: {evidence.get('reason')}"
            )

        record = evidence.get("roster_record") or {}
        roster_name_scan = record.get("name_offset_scan") or {}
        entity_scan = record.get("entity_pointer_offset_scan") or {}

        found = {
            "roster_name_offset": _has_delta(
                roster_name_scan.get("exact_identity_matches"),
                "delta_from_expected",
                -DRIFT["roster_name"],
            ),
            "roster_entity_pointer_offset": _has_delta(
                entity_scan.get("candidates"),
                "delta_from_expected",
                -DRIFT["roster_entity_pointer"],
            ),
            "entity_name_offset": False,
            "entity_character_pointer_offset": False,
            "character_vitals_common_shift": False,
            "recipe_definition_and_name_offsets": False,
            "salvage_definition_and_name_offsets": False,
            "recipe_quantity_offset": False,
            "salvage_quantity_offset": False,
            "recipe_level_offset": False,
        }

        entity_candidate = None
        for candidate in entity_scan.get("candidates") or []:
            try:
                if int(candidate.get("delta_from_expected")) == -DRIFT["roster_entity_pointer"]:
                    entity_candidate = candidate
                    break
            except Exception:
                continue

        if entity_candidate:
            entity_name_scan = (
                entity_candidate.get("entity_name_offset_scan") or {}
            )
            found["entity_name_offset"] = _has_delta(
                entity_name_scan.get("exact_identity_matches"),
                "delta_from_expected",
                -DRIFT["entity_name"],
            )

            char_scan = (
                entity_candidate.get("character_pointer_offset_scan") or {}
            )
            char_candidate = None
            for candidate in char_scan.get("candidates") or []:
                try:
                    if int(candidate.get("delta_from_expected")) == -DRIFT["entity_character_pointer"]:
                        char_candidate = candidate
                        found["entity_character_pointer_offset"] = True
                        break
                except Exception:
                    continue

            if char_candidate:
                vitals_candidates = (
                    char_candidate.get("vitals_common_shift") or {}
                ).get("candidates") or []
                found["character_vitals_common_shift"] = any(
                    int(item.get("shift")) == -DRIFT["character_vitals"]
                    for item in vitals_candidates
                    if item.get("shift") is not None
                )

        entries_evidence = evidence.get("entries") or {}
        for kind, prefix in (("recipes", "recipe"), ("salvage", "salvage")):
            block = entries_evidence.get(kind) or {}
            pair = block.get("definition_and_name_offset_scan") or {}
            found[f"{prefix}_definition_and_name_offsets"] = _pair_has_deltas(
                pair.get("candidates"),
                definition_delta=-DRIFT["entry_definition_pointer"],
                name_delta=-DRIFT["entry_internal_name_pointer"],
            )

            quantity = block.get("quantity_offset_scan") or {}
            found[f"{prefix}_quantity_offset"] = _has_delta(
                quantity.get("exact_header_reproduction_candidates"),
                "delta_from_expected",
                -DRIFT["entry_quantity"],
            )

        recipe_level = (
            entries_evidence.get("recipes", {})
            .get("recipe_level_offset_scan", {})
        )
        found["recipe_level_offset"] = _has_delta(
            recipe_level.get("candidates"),
            "delta_from_expected",
            -DRIFT["entry_recipe_level"],
        )

        missing = [name for name, value in found.items() if not value]
        if missing:
            raise RuntimeError(
                "The bounded structural diagnostics did not retain the known-good "
                "field(s) after intentional in-memory drift: "
                + ", ".join(missing)
            )

        # Separately prove that the normal diagnostic ZIP payload path has been
        # upgraded to schema 3 and includes structural observations.
        report = collect_memory_diagnostic(pid)
        if int(report.get("schema_version") or 0) != 3:
            raise RuntimeError(
                f"Expected diagnostic schema 3, got {report.get('schema_version')!r}."
            )
        profiles = report.get("profiles") or []
        if not profiles:
            raise RuntimeError("Schema-3 diagnostic returned no profile observations.")
        integrated_structural = (
            (profiles[0].get("observations") or {}).get("structural_drift")
            or {}
        )
        if not integrated_structural:
            raise RuntimeError(
                "Schema-3 diagnostic did not contain structural_drift observations."
            )

        policy = structural_diagnostic_policy_summary()
        if (
            policy.get("diagnostic_only") is not True
            or policy.get("auto_adopted") is not False
            or policy.get("persistent") is not False
        ):
            raise RuntimeError(
                f"Unexpected structural diagnostic policy: {policy!r}"
            )

        result = {
            "passed": True,
            "profile_id": original.profile_id,
            "profile_version": original.profile_version,
            "character_name": exact.character_name,
            "server": exact.server,
            "intentional_in_memory_structural_drift": DRIFT,
            "known_good_fields_retained_as_candidates": found,
            "diagnostic_schema": report.get("schema_version"),
            "schema3_structural_status": integrated_structural.get("status"),
            "schema3_structural_summary": integrated_structural.get("summary"),
            "auto_adopted": False,
            "persistent_changes": False,
            "policy": policy,
        }
        print(json.dumps(result, indent=2))
        return 0

    except Exception as exc:
        print(f"SELF-TEST FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
