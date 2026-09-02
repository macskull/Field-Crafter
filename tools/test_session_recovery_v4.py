#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path


def _find_root(start: Path) -> Path:
    start = start.resolve()
    for root in [start] + list(start.parents):
        if (root / "src" / "hc_recipe_db" / "game_memory.py").exists():
            return root
    raise RuntimeError(
        "Could not find Field_Crafter_1.16_Build_Source_DEV. "
        "Run from the source root or pass --root."
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Live, non-persistent self-test for Field Crafter v4 bounded "
            "Character-local inventory recovery."
        )
    )
    parser.add_argument("--root", default=".", help="Field Crafter 1.16 DEV root")
    parser.add_argument("--pid", type=int, help="cityofheroes.exe PID; omit when exactly one client is running")
    args = parser.parse_args()

    try:
        root = _find_root(Path(args.root))
        sys.path.insert(0, str(root / "src"))

        from hc_recipe_db.game_memory import (
            GameInventoryReader,
            GameMemoryError,
            ProcessMemory,
            _resolve_memory_context,
            list_city_of_heroes_processes,
        )
        from hc_recipe_db.memory_profiles import (
            MemoryProfile,
            MemoryProfileManager,
            as_int,
        )
        from hc_recipe_db.memory_recovery import (
            recover_inventory_profile,
            recovery_policy_summary,
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
            pid = processes[0].pid
        else:
            pid = int(args.pid)

        db_path = root / "data" / "homecoming_recipes.sqlite"
        alias_path = root / "data" / "memory_recipe_aliases.json"
        if not db_path.exists():
            raise RuntimeError(f"Database not found: {db_path}")

        manager = MemoryProfileManager()
        with ProcessMemory(pid) as mem:
            # This exact root chain must work first. v4 does not recover root locators.
            context = _resolve_memory_context(mem, manager, require_server=True)
            original = context.profile

            # Deliberately make a bad *in-memory clone*. Nothing on disk is touched.
            data = copy.deepcopy(original.data)
            character_cfg = data["structures"]["character"]
            for kind in ("recipes", "salvage"):
                cfg = character_cfg[kind]
                collection = as_int(cfg["collection_offset"])
                capacity_delta = as_int(cfg["capacity_offset"]) - collection
                count_delta = as_int(cfg["count_offset"]) - collection
                drifted = collection + 0x100
                cfg["collection_offset"] = drifted
                cfg["capacity_offset"] = drifted + capacity_delta
                cfg["count_offset"] = drifted + count_delta

            intentionally_bad = MemoryProfile(
                profile_id=original.profile_id,
                profile_version=original.profile_version,
                priority=original.priority,
                source=f"{original.source}+self-test-drifted",
                data=data,
            )

            recovered = recover_inventory_profile(
                mem,
                context.character_address,
                intentionally_bad,
            )
            if set(recovered.kinds) != {"recipes", "salvage"}:
                raise RuntimeError(
                    "Self-test failed: recovery did not independently recover "
                    "both recipes and salvage."
                )

            # Prove recovery returned the exact signed-profile layout without
            # printing those offsets in normal app UI.
            original_character = original.structure("character")
            recovered_character = recovered.profile.structure("character")
            for kind in ("recipes", "salvage"):
                for key in ("collection_offset", "capacity_offset", "count_offset"):
                    expected = as_int(original_character[kind][key])
                    actual = as_int(recovered_character[kind][key])
                    if actual != expected:
                        raise RuntimeError(
                            f"Self-test failed: recovered {kind} {key} did not "
                            "match the current signed profile."
                        )

            # Full read through the recovered profile. Disable recursive recovery:
            # this must pass on the recovered offsets themselves.
            reader = GameInventoryReader(
                db_path,
                alias_path=alias_path if alias_path.exists() else None,
                profile_manager=manager,
                allow_session_recovery=False,
            )
            (
                recipe_capacity,
                recipe_total,
                _recipes,
                salvage_capacity,
                salvage_total,
                _salvage,
            ) = reader._read_inventory_with_profile(
                mem,
                context,
                recovered.profile,
            )

        result = {
            "passed": True,
            "profile_id": original.profile_id,
            "profile_version": original.profile_version,
            "character_name": context.character_name,
            "server": context.server,
            "intentional_in_memory_drift_bytes": 0x100,
            "recovered_kinds": list(recovered.kinds),
            "validation_samples": recovered.sample_count,
            "recipe_total": recipe_total,
            "recipe_capacity": recipe_capacity,
            "salvage_total": salvage_total,
            "salvage_capacity": salvage_capacity,
            "persistent_changes": False,
            "policy": recovery_policy_summary(),
        }
        print(json.dumps(result, indent=2))
        return 0

    except Exception as exc:
        print(f"SELF-TEST FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
