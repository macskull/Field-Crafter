from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from hc_recipe_db.calculator import CraftingCalculator, RecipeSelection, parse_capacity_text
from hc_recipe_db.game_memory import MemoryNameResolver
from hc_recipe_db.memory_profiles import MemoryProfileManager
from hc_recipe_db.memory_profile_updates import load_update_config
from hc_recipe_db.memory_diagnostics import diagnostic_filename
from hc_recipe_db.memory_recovery import recovery_policy_summary
from hc_recipe_db.memory_root_recovery import root_recovery_policy_summary
from hc_recipe_db.gui import _auction_search_batches


ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "homecoming_recipes.sqlite"


def main() -> int:
    if not DB.exists():
        raise RuntimeError(f"Missing database: {DB}")

    # Basic database/query/calculator integration.
    with sqlite3.connect(DB) as conn:
        row = conn.execute(
            """
            SELECT r.name, MIN(rl.level)
            FROM recipes r
            JOIN recipe_levels rl ON rl.recipe_id=r.id
            JOIN craft_options co ON co.recipe_level_id=rl.id
            GROUP BY r.id
            ORDER BY r.name COLLATE NOCASE
            LIMIT 1
            """
        ).fetchone()
    if not row:
        raise RuntimeError("No craftable recipe was found for calculator smoke testing.")
    recipe_name, level = str(row[0]), int(row[1])
    with CraftingCalculator(DB) as calc:
        result = calc.calculate([RecipeSelection(recipe=recipe_name, level=level)])
    if result["summary"]["craft_count"] != 1:
        raise RuntimeError("Calculator smoke test returned an unexpected craft count.")

    cap = parse_capacity_text("Salvage 74 / 150")
    if (cap.used, cap.capacity) != (74, 150):
        raise RuntimeError("Capacity parser smoke test failed.")

    # Memory naming must retain common-IO compatibility and validated seed mapping.
    with tempfile.TemporaryDirectory() as tmp:
        resolver = MemoryNameResolver(DB, alias_path=Path(tmp) / "aliases.json")
        common, source = resolver.resolve_recipe("Invention_Heal_50")
        if common not in {"Invention: Healing/Absorb", "Invention: Healing"}:
            raise RuntimeError(f"Common healing IO mapping failed: {common!r} ({source})")
        panacea, source = resolver.resolve_recipe("Panacea_D_50")
        if panacea != "Panacea: Healing/Absorb/Endurance/Recharge":
            raise RuntimeError(f"Panacea A-F mapping smoke test failed: {panacea!r} ({source})")

    manager = MemoryProfileManager(
        user_pack_path=ROOT / ".release_self_test_no_user_memory_profile.json"
    )
    candidates = manager.candidates()
    if not candidates:
        raise RuntimeError("Bundled memory profile pack contains no candidates.")
    config = load_update_config()
    if not config.get("manifest_url") or not config.get("public_key_ed25519"):
        raise RuntimeError("Memory update configuration is incomplete.")

    diagnostic_name = diagnostic_filename(1234)
    if not diagnostic_name.startswith("field_crafter_memory_diagnostic_") or not diagnostic_name.endswith("_pid1234.zip"):
        raise RuntimeError(f"Memory diagnostic filename smoke test failed: {diagnostic_name!r}")

    recovery_policy = recovery_policy_summary()
    if (
        int(recovery_policy.get("sample_count") or 0) < 3
        or recovery_policy.get("persistent") is not False
        or recovery_policy.get("root_locator_recovery") is not False
        or recovery_policy.get("signed_candidate_validation_may_use_recovery") is not False
        or recovery_policy.get("stale_empty_guard") is not True
        or recovery_policy.get("stale_empty_requires_positive_moved_collection_proof") is not True
    ):
        raise RuntimeError(
            f"Memory session-recovery policy smoke test failed: {recovery_policy!r}"
        )

    root_recovery_policy = root_recovery_policy_summary()
    if (
        int(root_recovery_policy.get("sample_count") or 0) < 3
        or root_recovery_policy.get("persistent") is not False
        or root_recovery_policy.get("unique_best_code_candidate_required") is not True
        or root_recovery_policy.get("identity_roster_entity_name_agreement_required") is not True
        or root_recovery_policy.get("signed_candidate_validation_may_use_recovery") is not False
    ):
        raise RuntimeError(
            f"Memory root-recovery policy smoke test failed: {root_recovery_policy!r}"
        )

    batches = _auction_search_batches(
        [
            {"salvage": "Chaos Theorem", "buy": 2, "rarity": "uncommon"},
            {"salvage": "Ruby", "buy": 1, "rarity": "common"},
        ],
        "Name",
        128,
    )
    if not batches or "Chaos Theorem" not in batches[0] or "Ruby" not in batches[0]:
        raise RuntimeError("Shopping-list helper smoke test failed.")

    print("PASS: Field Crafter core runtime smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
