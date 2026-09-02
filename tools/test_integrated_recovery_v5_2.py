#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
from pathlib import Path


# FIELD_CRAFTER_MEMORY_INTEGRATED_RECOVERY_TEST_V5_1
# FIELD_CRAFTER_MEMORY_STALE_EMPTY_GUARD_V5_2_TEST

ROOT_DRIFT_LOCATORS = ("identity", "server", "roster")
INVENTORY_DRIFT_KINDS = ("recipes", "salvage")
INTENTIONAL_INVENTORY_DRIFT_BYTES = 0x100


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
            "Signature has too few fixed bytes for the integrated recovery test."
        )

    # Match the proven v5 self-test: change one fixed byte while retaining enough
    # unchanged fixed runs for conservative nearest-landmark recovery.
    index = fixed[0]
    original = int(tokens[index], 16)
    tokens[index] = f"{original ^ 0x01:02X}"
    return " ".join(tokens)


def _sha256_if_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _persistent_memory_files(root: Path) -> list[Path]:
    paths = [
        root / "data" / "memory_profiles.json",
        root / "data" / "memory_update_config.json",
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        memory_dir = Path(local) / "FieldCrafter" / "memory"
        paths.extend(
            [
                memory_dir / "memory_profiles.json",
                memory_dir / "memory_profiles.previous.json",
                memory_dir / "memory_update_state.json",
            ]
        )
    # Preserve order while removing duplicates.
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _fingerprint_persistent_files(root: Path) -> dict[str, str | None]:
    return {
        str(path): _sha256_if_file(path)
        for path in _persistent_memory_files(root)
    }


class _SingleProfileManager:
    """Minimal in-memory manager accepted by both exact and recovery paths."""

    def __init__(self, profile) -> None:
        self._profile = profile

    def candidates(self):
        return [self._profile]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "End-to-end, non-persistent Field Crafter v5.1 test: intentionally "
            "break root signatures and inventory headers in one in-memory profile, "
            "then require the production reader to compose v5 root recovery followed "
            "by v4 inventory recovery."
        )
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Field Crafter 1.16 DEV root",
    )
    parser.add_argument(
        "--pid",
        type=int,
        help=(
            "cityofheroes.exe PID; omit when exactly one client is running"
        ),
    )
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

        processes = list_city_of_heroes_processes()
        if args.pid is None:
            if not processes:
                raise RuntimeError(
                    "No running cityofheroes.exe clients were found."
                )
            if len(processes) != 1:
                print(
                    "Multiple clients are running. Re-run with --pid:",
                    file=sys.stderr,
                )
                for proc in processes:
                    print(f"  {proc.label}", file=sys.stderr)
                return 2
            pid = int(processes[0].pid)
        else:
            pid = int(args.pid)

        db_path = root / "data" / "homecoming_recipes.sqlite"
        alias_path = root / "data" / "memory_recipe_aliases.json"
        if not db_path.exists():
            raise RuntimeError(f"Database not found: {db_path}")

        persistent_before = _fingerprint_persistent_files(root)

        # Establish the known-good baseline with the real manager/profile first.
        real_manager = MemoryProfileManager()
        with ProcessMemory(pid) as mem:
            exact = _resolve_memory_context(
                mem,
                real_manager,
                require_server=True,
            )
            original = exact.profile

            data = copy.deepcopy(original.data)

            # Break all three root signatures by one fixed byte each.
            exact_hits_after_root_drift: dict[str, int] = {}
            for locator_name in ROOT_DRIFT_LOCATORS:
                locator = data["locators"][locator_name]
                locator["pattern"] = _mutate_one_fixed_byte(
                    str(locator["pattern"])
                )

            # Move both Character-local inventory header groups while preserving
            # their internal collection/capacity/count spacing.
            original_inventory_offsets: dict[str, dict[str, int]] = {}
            drifted_inventory_offsets: dict[str, dict[str, int]] = {}
            character_cfg = data["structures"]["character"]
            for kind in INVENTORY_DRIFT_KINDS:
                cfg = character_cfg[kind]
                collection = as_int(cfg["collection_offset"])
                capacity = as_int(cfg["capacity_offset"])
                count = as_int(cfg["count_offset"])
                capacity_delta = capacity - collection
                count_delta = count - collection

                original_inventory_offsets[kind] = {
                    "collection_offset": collection,
                    "capacity_offset": capacity,
                    "count_offset": count,
                }

                drifted_collection = (
                    collection + INTENTIONAL_INVENTORY_DRIFT_BYTES
                )
                cfg["collection_offset"] = drifted_collection
                cfg["capacity_offset"] = (
                    drifted_collection + capacity_delta
                )
                cfg["count_offset"] = drifted_collection + count_delta

                drifted_inventory_offsets[kind] = {
                    "collection_offset": drifted_collection,
                    "capacity_offset": drifted_collection + capacity_delta,
                    "count_offset": drifted_collection + count_delta,
                }

            drifted = MemoryProfile(
                profile_id=original.profile_id,
                profile_version=original.profile_version,
                priority=original.priority,
                source=f"{original.source}+integrated-self-test-drift",
                data=data,
            )
            drifted_manager = _SingleProfileManager(drifted)

            # Stage 1 proof: all changed root patterns really have zero exact hits.
            for locator_name in ROOT_DRIFT_LOCATORS:
                count = len(
                    mem.signature_hits(
                        str(drifted.locator(locator_name)["pattern"])
                    )
                )
                exact_hits_after_root_drift[locator_name] = count
                if count != 0:
                    raise RuntimeError(
                        f"Integrated self-test did not break the "
                        f"{locator_name} exact signature "
                        f"(found {count} hit(s))."
                    )

            # Stage 2 proof: the ordinary exact root resolver must reject this
            # in-memory profile before v5 is allowed to help.
            exact_root_failure_confirmed = False
            exact_root_failure_detail = ""
            try:
                _resolve_memory_context(
                    mem,
                    drifted_manager,
                    require_server=False,
                )
            except GameMemoryError as exc:
                exact_root_failure_confirmed = True
                exact_root_failure_detail = str(exc)
            if not exact_root_failure_confirmed:
                raise RuntimeError(
                    "Integrated self-test profile unexpectedly passed normal "
                    "root resolution; v5 would not actually be exercised."
                )

            # Stage 3 proof: determine how the stale shifted inventory manifests
            # without recovery. A patch may leave the old header as all-zero memory,
            # which is structurally valid as a genuinely empty inventory. v5.2 exists
            # specifically to distinguish that case from a real empty inventory by
            # finding a strong populated collection nearby.
            direct_reader = GameInventoryReader(
                db_path,
                alias_path=(
                    alias_path if alias_path.exists() else None
                ),
                profile_manager=drifted_manager,
                allow_session_recovery=False,
                allow_root_recovery=False,
            )
            exact_inventory_failure_confirmed = False
            exact_inventory_failure_detail = ""
            stale_empty_inventory_confirmed = False
            stale_empty_inventory_totals = None
            try:
                direct_inventory = direct_reader._read_inventory_with_profile(
                    mem,
                    exact,
                    drifted,
                )
            except GameMemoryError as exc:
                exact_inventory_failure_confirmed = True
                exact_inventory_failure_detail = str(exc)
            else:
                stale_empty_inventory_totals = {
                    "recipe_total": int(direct_inventory[1]),
                    "salvage_total": int(direct_inventory[4]),
                }
                stale_empty_inventory_confirmed = (
                    int(direct_inventory[1]) == 0
                    or int(direct_inventory[4]) == 0
                )

            inventory_drift_effect_confirmed = bool(
                exact_inventory_failure_confirmed
                or stale_empty_inventory_confirmed
            )
            if not inventory_drift_effect_confirmed:
                raise RuntimeError(
                    "Integrated self-test inventory drift neither failed the "
                    "direct read nor produced a stale-empty inventory. The test "
                    "would not prove v4/v5.2 recovery behavior."
                )

        # Now exercise the actual public production path. This opens a fresh
        # ProcessMemory instance internally:
        #
        # exact root -> fail
        # v5 root recovery -> pass
        # exact inventory on drifted profile -> fail
        # v4 inventory recovery -> pass
        # final production snapshot -> return
        reader = GameInventoryReader(
            db_path,
            alias_path=alias_path if alias_path.exists() else None,
            profile_manager=drifted_manager,
            allow_session_recovery=True,
            allow_root_recovery=True,
        )
        snapshot = reader.read(pid)

        if not snapshot.memory_root_recovery_applied:
            raise RuntimeError(
                "Production reader returned without reporting v5 root recovery."
            )
        if set(snapshot.memory_root_recovery_locators) != set(
            ROOT_DRIFT_LOCATORS
        ):
            raise RuntimeError(
                "Production reader did not recover all three intentionally "
                "drifted root locators."
            )
        if int(snapshot.memory_root_recovery_samples) != 3:
            raise RuntimeError(
                "Production reader did not report the expected three-sample "
                "root recovery validation."
            )

        if not snapshot.memory_recovery_applied:
            raise RuntimeError(
                "Production reader returned without reporting v4 inventory recovery."
            )
        if set(snapshot.memory_recovery_kinds) != set(
            INVENTORY_DRIFT_KINDS
        ):
            raise RuntimeError(
                "Production reader did not recover both intentionally drifted "
                "inventory kinds."
            )
        if int(snapshot.memory_recovery_samples) != 3:
            raise RuntimeError(
                "Production reader did not report the expected three-sample "
                "inventory recovery validation."
            )

        # The final root must still be the same live player object as the exact
        # baseline captured before introducing the synthetic drift.
        baseline_match = {
            "character_name": (
                exact.character_name
                == next(
                    (
                        proc.character_name
                        for proc in processes
                        if int(proc.pid) == pid and proc.character_name
                    ),
                    exact.character_name,
                )
            ),
            "entity_address": (
                int(snapshot.owner_address) == int(exact.entity_address)
            ),
            "character_address": (
                int(snapshot.inventory_address)
                == int(exact.character_address)
            ),
        }
        if not all(baseline_match.values()):
            raise RuntimeError(
                "Combined production recovery returned a different root object "
                f"than the exact baseline: {baseline_match}"
            )

        persistent_after = _fingerprint_persistent_files(root)
        persistent_unchanged = (
            persistent_before == persistent_after
        )
        if not persistent_unchanged:
            changed = [
                path
                for path in persistent_before
                if persistent_before.get(path)
                != persistent_after.get(path)
            ]
            raise RuntimeError(
                "Integrated recovery test unexpectedly changed persistent "
                "memory-definition/update files: "
                + ", ".join(changed)
            )

        result = {
            "passed": True,
            "profile_id": original.profile_id,
            "profile_version": original.profile_version,
            "character_name": exact.character_name,
            "server": exact.server,
            "production_path_exercised": "GameInventoryReader.read",
            "exact_root_failure_confirmed": exact_root_failure_confirmed,
            "exact_root_failure_detail": exact_root_failure_detail,
            "exact_inventory_failure_confirmed": (
                exact_inventory_failure_confirmed
            ),
            "exact_inventory_failure_detail": (
                exact_inventory_failure_detail
            ),
            "stale_empty_inventory_confirmed": (
                stale_empty_inventory_confirmed
            ),
            "stale_empty_inventory_totals": (
                stale_empty_inventory_totals
            ),
            "inventory_drift_effect_confirmed": (
                inventory_drift_effect_confirmed
            ),
            "intentionally_drifted_root_locators": list(
                ROOT_DRIFT_LOCATORS
            ),
            "exact_hits_after_root_test_drift": (
                exact_hits_after_root_drift
            ),
            "intentional_inventory_drift_bytes": (
                INTENTIONAL_INVENTORY_DRIFT_BYTES
            ),
            "intentionally_drifted_inventory_kinds": list(
                INVENTORY_DRIFT_KINDS
            ),
            "root_recovery": {
                "applied": snapshot.memory_root_recovery_applied,
                "locators": list(
                    snapshot.memory_root_recovery_locators
                ),
                "validation_samples": (
                    snapshot.memory_root_recovery_samples
                ),
            },
            "inventory_recovery": {
                "applied": snapshot.memory_recovery_applied,
                "kinds": list(snapshot.memory_recovery_kinds),
                "validation_samples": snapshot.memory_recovery_samples,
            },
            "root_matches_exact_baseline": baseline_match,
            "recipe_total": snapshot.recipe_total,
            "recipe_capacity": snapshot.recipe_capacity,
            "salvage_total": snapshot.salvage_total,
            "salvage_capacity": snapshot.salvage_capacity,
            "persistent_files_unchanged": persistent_unchanged,
            "persistent_changes": False,
        }
        print(json.dumps(result, indent=2))
        return 0

    except Exception as exc:
        print(f"SELF-TEST FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
