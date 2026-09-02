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


def _mutate_one_fixed_byte(pattern: str) -> str:
    tokens = pattern.split()
    fixed = [
        index
        for index, token in enumerate(tokens)
        if token not in {"?", "??"}
    ]
    if len(fixed) < 12:
        raise RuntimeError(
            "Signature has too few fixed bytes for the v5 self-test."
        )

    # Prefer a fixed byte near the beginning while leaving other fixed runs intact.
    index = fixed[0]
    original = int(tokens[index], 16)
    tokens[index] = f"{original ^ 0x01:02X}"
    return " ".join(tokens)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Live, non-persistent self-test for Field Crafter v5 root-locator "
            "session recovery."
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
            GameInventoryReader,
            ProcessMemory,
            _resolve_memory_context,
            list_city_of_heroes_processes,
        )
        from hc_recipe_db.memory_profiles import (
            MemoryProfile,
            MemoryProfileManager,
        )
        from hc_recipe_db.memory_root_recovery import (
            recover_root_context_for_profile,
            root_recovery_policy_summary,
        )

        processes = list_city_of_heroes_processes()
        if args.pid is None:
            if not processes:
                raise RuntimeError("No running cityofheroes.exe clients were found.")
            if len(processes) != 1:
                print(
                    "Multiple clients are running. Re-run with --pid:",
                    file=sys.stderr,
                )
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
            # Capture the exact signed-profile answer first. The self-test must
            # recover back to this same object graph.
            exact = _resolve_memory_context(
                mem,
                manager,
                require_server=True,
            )
            original = exact.profile

            data = copy.deepcopy(original.data)
            changed: dict[str, dict[str, str]] = {}
            for locator_name in ("identity", "server", "roster"):
                locator = data["locators"][locator_name]
                before = str(locator["pattern"])
                after = _mutate_one_fixed_byte(before)
                locator["pattern"] = after
                changed[locator_name] = {
                    "original": before,
                    "self_test": after,
                }

            drifted = MemoryProfile(
                profile_id=original.profile_id,
                profile_version=original.profile_version,
                priority=original.priority,
                source=f"{original.source}+self-test-root-drift",
                data=data,
            )

            # Prove the intentionally changed signatures no longer exact-match.
            exact_hits_after_drift: dict[str, int] = {}
            for locator_name in ("identity", "server", "roster"):
                count = len(
                    mem.signature_hits(
                        str(drifted.locator(locator_name)["pattern"])
                    )
                )
                exact_hits_after_drift[locator_name] = count
                if count != 0:
                    raise RuntimeError(
                        f"Self-test did not break the {locator_name} exact signature "
                        f"(found {count} hit(s))."
                    )

            recovered = recover_root_context_for_profile(
                mem,
                drifted,
                require_server=True,
            )

            expected_recovered = {"identity", "server", "roster"}
            if set(recovered.recovered_locators) != expected_recovered:
                raise RuntimeError(
                    "Self-test failed: v5 did not nearest-landmark recover all "
                    "three intentionally drifted root locators."
                )

            context = recovered.context
            comparisons = {
                "character_name": (
                    context.character_name == exact.character_name
                ),
                "server": context.server == exact.server,
                "entity_address": (
                    context.entity_address == exact.entity_address
                ),
                "character_address": (
                    context.character_address == exact.character_address
                ),
            }
            if not all(comparisons.values()):
                raise RuntimeError(
                    f"Recovered root context did not match the exact baseline: "
                    f"{comparisons}"
                )

            # Exercise a full inventory read through the recovered root context.
            # Both v4 inventory recovery and v5 recursive root recovery are disabled:
            # this must work on the recovered root and current signed layout directly.
            reader = GameInventoryReader(
                db_path,
                alias_path=alias_path if alias_path.exists() else None,
                profile_manager=manager,
                allow_session_recovery=False,
                allow_root_recovery=False,
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
                original,
            )

        code_evidence = [
            {
                "locator": item["locator"],
                "source": item["source"],
                "mismatch_count": item["mismatch_count"],
                "fixed_byte_count": item["fixed_byte_count"],
                "match_ratio": item["match_ratio"],
            }
            for item in recovered.code_evidence
        ]

        result = {
            "passed": True,
            "profile_id": original.profile_id,
            "profile_version": original.profile_version,
            "character_name": context.character_name,
            "server": context.server,
            "intentionally_drifted_locators": [
                "identity",
                "server",
                "roster",
            ],
            "exact_hits_after_test_drift": exact_hits_after_drift,
            "recovered_locators": list(recovered.recovered_locators),
            "validation_samples": recovered.sample_count,
            "root_matches_exact_baseline": comparisons,
            "code_evidence": code_evidence,
            "recipe_total": recipe_total,
            "recipe_capacity": recipe_capacity,
            "salvage_total": salvage_total,
            "salvage_capacity": salvage_capacity,
            "persistent_changes": False,
            "policy": root_recovery_policy_summary(),
        }
        print(json.dumps(result, indent=2))
        return 0

    except Exception as exc:
        print(f"SELF-TEST FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
