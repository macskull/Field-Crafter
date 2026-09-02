#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
from pathlib import Path


# FIELD_CRAFTER_MEMORY_STRUCTURAL_RECOVERY_V7_TEST

ROOT_DRIFT_LOCATORS = ("identity", "server", "roster")
INVENTORY_DRIFT_BYTES = 0x100
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

from field_crafter_test_output import print_result_path, write_test_json


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
            "Signature has too few fixed bytes for the v7 live self-test."
        )
    index = fixed[0]
    tokens[index] = f"{int(tokens[index], 16) ^ 0x01:02X}"
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
    return paths


def _fingerprint(root: Path) -> dict[str, str | None]:
    return {
        str(path): _sha256_if_file(path)
        for path in _persistent_memory_files(root)
    }


class _SingleProfileManager:
    def __init__(self, profile) -> None:
        self._profile = profile

    def candidates(self):
        return [self._profile]


def _drift_profile(
    original,
    *,
    root_drift: bool,
    inventory_drift: bool,
    structural_drift: bool,
):
    from hc_recipe_db.memory_profiles import MemoryProfile, as_int

    data = copy.deepcopy(original.data)

    if root_drift:
        for locator_name in ROOT_DRIFT_LOCATORS:
            locator = data["locators"][locator_name]
            locator["pattern"] = _mutate_one_fixed_byte(
                str(locator["pattern"])
            )

    if structural_drift:
        roster = data["structures"]["roster"]
        roster["name_offset"] = (
            as_int(roster["name_offset"])
            + STRUCTURAL_DRIFT["roster_name"]
        )
        roster["entity_pointer_offset"] = (
            as_int(roster["entity_pointer_offset"])
            + STRUCTURAL_DRIFT["roster_entity_pointer"]
        )

        entity = data["structures"]["entity"]
        entity["name_offset"] = (
            as_int(entity["name_offset"])
            + STRUCTURAL_DRIFT["entity_name"]
        )
        entity["character_pointer_offset"] = (
            as_int(entity["character_pointer_offset"])
            + STRUCTURAL_DRIFT["entity_character_pointer"]
        )

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

    if inventory_drift:
        character = data["structures"]["character"]
        for kind in ("recipes", "salvage"):
            cfg = character[kind]
            collection = as_int(cfg["collection_offset"])
            capacity_delta = as_int(cfg["capacity_offset"]) - collection
            count_delta = as_int(cfg["count_offset"]) - collection
            moved = collection + INVENTORY_DRIFT_BYTES
            cfg["collection_offset"] = moved
            cfg["capacity_offset"] = moved + capacity_delta
            cfg["count_offset"] = moved + count_delta

    tags = []
    if root_drift:
        tags.append("root")
    if inventory_drift:
        tags.append("inventory")
    if structural_drift:
        tags.append("structural")

    return MemoryProfile(
        profile_id=original.profile_id,
        profile_version=original.profile_version,
        priority=original.priority,
        source=f"{original.source}+v7-self-test-{'-'.join(tags)}",
        data=data,
    )


def _run_scenario(
    *,
    root: Path,
    pid: int,
    original,
    baseline_snapshot,
    name: str,
    root_drift: bool,
    inventory_drift: bool,
    structural_drift: bool,
) -> dict:
    from hc_recipe_db.game_memory import (
        GameInventoryReader,
        GameMemoryError,
        ProcessMemory,
        _resolve_memory_context,
    )
    from hc_recipe_db.memory_root_recovery import (
        MemoryRootRecoveryError,
        recover_root_context_for_profile,
    )

    db_path = root / "data" / "homecoming_recipes.sqlite"
    alias_path = root / "data" / "memory_recipe_aliases.json"

    drifted = _drift_profile(
        original,
        root_drift=root_drift,
        inventory_drift=inventory_drift,
        structural_drift=structural_drift,
    )
    manager = _SingleProfileManager(drifted)

    exact_root_passed = False
    exact_root_detail = ""
    v5_root_passed = False
    v5_root_detail = ""
    no_recovery_inventory_passed = False
    no_recovery_inventory_detail = ""

    with ProcessMemory(pid) as mem:
        try:
            exact_context = _resolve_memory_context(
                mem,
                manager,
                require_server=True,
            )
            exact_root_passed = True
        except GameMemoryError as exc:
            exact_context = None
            exact_root_detail = str(exc)

        try:
            v5 = recover_root_context_for_profile(
                mem,
                drifted,
                require_server=True,
            )
            v5_root_passed = True
            v5_root_detail = (
                "recovered " + ", ".join(v5.recovered_locators)
            )
        except MemoryRootRecoveryError as exc:
            v5_root_detail = str(exc)

        if exact_context is not None:
            direct = GameInventoryReader(
                db_path,
                alias_path=alias_path if alias_path.exists() else None,
                profile_manager=manager,
                allow_session_recovery=False,
                allow_root_recovery=False,
                allow_structural_recovery=False,
            )
            try:
                direct._read_inventory_with_profile(
                    mem,
                    exact_context,
                    drifted,
                )
                no_recovery_inventory_passed = True
            except GameMemoryError as exc:
                no_recovery_inventory_detail = str(exc)

    reader = GameInventoryReader(
        db_path,
        alias_path=alias_path if alias_path.exists() else None,
        profile_manager=manager,
        allow_session_recovery=True,
        allow_root_recovery=True,
        allow_structural_recovery=True,
    )
    snapshot = reader.read(pid)

    if not snapshot.memory_structural_recovery_applied:
        raise RuntimeError(
            f"{name}: production reader returned without v7 structural recovery."
        )
    if int(snapshot.memory_structural_recovery_samples) != 3:
        raise RuntimeError(
            f"{name}: structural recovery did not report three validation samples."
        )
    if snapshot.recipe_total != baseline_snapshot.recipe_total:
        raise RuntimeError(
            f"{name}: recipe total {snapshot.recipe_total} differs from exact "
            f"baseline {baseline_snapshot.recipe_total}."
        )
    if snapshot.salvage_total != baseline_snapshot.salvage_total:
        raise RuntimeError(
            f"{name}: salvage total {snapshot.salvage_total} differs from exact "
            f"baseline {baseline_snapshot.salvage_total}."
        )
    if snapshot.recipe_capacity != baseline_snapshot.recipe_capacity:
        raise RuntimeError(
            f"{name}: recipe capacity differs from exact baseline."
        )
    if snapshot.salvage_capacity != baseline_snapshot.salvage_capacity:
        raise RuntimeError(
            f"{name}: salvage capacity differs from exact baseline."
        )

    required_fields = {
        "Entry quantity",
        "Entry Definition pointer",
        "Definition internal-name pointer",
        "recipe level",
    }
    if structural_drift:
        required_fields.update(
            {
                "roster name",
                "roster Entity pointer",
                "Entity name",
                "Entity Character pointer",
                "Character vitals",
            }
        )
    if inventory_drift:
        required_fields.update(
            {
                "recipe header",
                "salvage header",
            }
        )

    actual_fields = set(snapshot.memory_structural_recovery_fields)
    missing = sorted(required_fields - actual_fields)
    if missing:
        raise RuntimeError(
            f"{name}: structural recovery did not report expected recovered "
            f"field groups: {', '.join(missing)}"
        )

    if root_drift:
        if set(snapshot.memory_root_recovery_locators) != set(
            ROOT_DRIFT_LOCATORS
        ):
            raise RuntimeError(
                f"{name}: root locator metadata did not include identity/server/roster."
            )
        if int(snapshot.memory_root_recovery_samples) != 3:
            raise RuntimeError(
                f"{name}: root locator metadata did not report three samples."
            )

    return {
        "name": name,
        "intentional_drift": {
            "root": root_drift,
            "inventory_headers": inventory_drift,
            "deeper_structure": structural_drift,
        },
        "pre_recovery_proofs": {
            "exact_root_passed": exact_root_passed,
            "exact_root_detail": exact_root_detail,
            "v5_full_root_recovery_passed": v5_root_passed,
            "v5_full_root_recovery_detail": v5_root_detail,
            "direct_no_recovery_inventory_passed": (
                no_recovery_inventory_passed
            ),
            "direct_no_recovery_inventory_detail": (
                no_recovery_inventory_detail
            ),
        },
        "production_result": {
            "structural_recovery_applied": (
                snapshot.memory_structural_recovery_applied
            ),
            "structural_recovery_fields": list(
                snapshot.memory_structural_recovery_fields
            ),
            "structural_recovery_samples": (
                snapshot.memory_structural_recovery_samples
            ),
            "root_recovery_applied": snapshot.memory_root_recovery_applied,
            "root_recovery_locators": list(
                snapshot.memory_root_recovery_locators
            ),
            "root_recovery_samples": snapshot.memory_root_recovery_samples,
            "recipe_total": snapshot.recipe_total,
            "recipe_capacity": snapshot.recipe_capacity,
            "salvage_total": snapshot.salvage_total,
            "salvage_capacity": snapshot.salvage_capacity,
        },
        "matches_exact_baseline": {
            "recipe_total": (
                snapshot.recipe_total == baseline_snapshot.recipe_total
            ),
            "recipe_capacity": (
                snapshot.recipe_capacity == baseline_snapshot.recipe_capacity
            ),
            "salvage_total": (
                snapshot.salvage_total == baseline_snapshot.salvage_total
            ),
            "salvage_capacity": (
                snapshot.salvage_capacity == baseline_snapshot.salvage_capacity
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Live end-to-end self-test for Field Crafter v7 structural "
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

    pid_for_failure = args.pid

    try:
        root = _find_root(Path(args.root))
        sys.path.insert(0, str(root / "src"))

        from hc_recipe_db.game_memory import (
            GameInventoryReader,
            ProcessMemory,
            _resolve_memory_context,
            list_city_of_heroes_processes,
        )
        from hc_recipe_db.memory_profiles import MemoryProfileManager
        from hc_recipe_db.memory_structural_recovery import (
            structural_recovery_policy_summary,
        )

        processes = list_city_of_heroes_processes()
        if args.pid is None:
            if not processes:
                raise RuntimeError("No running cityofheroes.exe clients were found.")
            if len(processes) != 1:
                raise RuntimeError(
                    "Multiple clients are running. Re-run with --pid."
                )
            pid = int(processes[0].pid)
        else:
            pid = int(args.pid)
        pid_for_failure = pid

        db_path = root / "data" / "homecoming_recipes.sqlite"
        alias_path = root / "data" / "memory_recipe_aliases.json"
        if not db_path.exists():
            raise RuntimeError(f"Database not found: {db_path}")

        before = _fingerprint(root)

        manager = MemoryProfileManager()
        with ProcessMemory(pid) as mem:
            exact_context = _resolve_memory_context(
                mem,
                manager,
                require_server=True,
            )
            original = exact_context.profile

        baseline_reader = GameInventoryReader(
            db_path,
            alias_path=alias_path if alias_path.exists() else None,
            profile_manager=manager,
            allow_session_recovery=False,
            allow_root_recovery=False,
            allow_structural_recovery=False,
        )
        baseline = baseline_reader.read(pid)

        scenarios = [
            _run_scenario(
                root=root,
                pid=pid,
                original=original,
                baseline_snapshot=baseline,
                name="entry_layout_only",
                root_drift=False,
                inventory_drift=False,
                structural_drift=True,
            ),
            _run_scenario(
                root=root,
                pid=pid,
                original=original,
                baseline_snapshot=baseline,
                name="full_root_inventory_structural",
                root_drift=True,
                inventory_drift=True,
                structural_drift=True,
            ),
        ]

        after = _fingerprint(root)
        if before != after:
            changed = [
                path
                for path in before
                if before.get(path) != after.get(path)
            ]
            raise RuntimeError(
                "v7 live self-test changed persistent memory-definition/update "
                "files: " + ", ".join(changed)
            )

        full = next(
            item
            for item in scenarios
            if item["name"] == "full_root_inventory_structural"
        )
        if full["pre_recovery_proofs"]["exact_root_passed"]:
            raise RuntimeError(
                "Full combined scenario unexpectedly passed exact root resolution."
            )
        if full["pre_recovery_proofs"]["v5_full_root_recovery_passed"]:
            raise RuntimeError(
                "Full combined scenario unexpectedly passed v5 full root recovery; "
                "v7 would not be proving deeper structural fallback."
            )

        entry_only = next(
            item
            for item in scenarios
            if item["name"] == "entry_layout_only"
        )
        if not entry_only["pre_recovery_proofs"]["exact_root_passed"]:
            raise RuntimeError(
                "Entry-only scenario unexpectedly broke exact root semantics."
            )

        policy = structural_recovery_policy_summary()
        if (
            policy.get("persistent") is not False
            or policy.get("signed_candidate_validation_may_use_recovery")
            is not False
            or int(policy.get("sample_count") or 0) != 3
        ):
            raise RuntimeError(
                f"Unexpected v7 structural recovery policy: {policy!r}"
            )

        result = {
            "passed": True,
            "test_version": "7",
            "profile_id": original.profile_id,
            "profile_version": original.profile_version,
            "character_name": exact_context.character_name,
            "server": exact_context.server,
            "baseline": {
                "recipe_total": baseline.recipe_total,
                "recipe_capacity": baseline.recipe_capacity,
                "salvage_total": baseline.salvage_total,
                "salvage_capacity": baseline.salvage_capacity,
            },
            "scenarios": scenarios,
            "persistent_files_unchanged": True,
            "persistent_changes": False,
            "policy": policy,
        }

        output_path = write_test_json(
            "structural_recovery_v7",
            pid,
            result,
        )
        print_result_path(output_path, passed=True)
        return 0

    except Exception as exc:
        failure = {
            "passed": False,
            "test_version": "7",
            "error": str(exc),
            "persistent_changes": False,
        }
        try:
            output_path = write_test_json(
                "structural_recovery_v7",
                pid_for_failure,
                failure,
            )
            print_result_path(output_path, passed=False)
        except Exception:
            print(f"SELF-TEST FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
