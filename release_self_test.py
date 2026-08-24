from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from hc_recipe_db.calculator import CraftingCalculator, RecipeSelection, parse_capacity_text
from hc_recipe_db.game_memory import MemoryNameResolver
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
