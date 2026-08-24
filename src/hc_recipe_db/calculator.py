from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .normalize import canonical_key


class CalculationError(ValueError):
    """Raised when calculator input is ambiguous or internally inconsistent."""


@dataclass(slots=True)
class RecipeSelection:
    recipe: str
    quantity: int = 1
    level: int | None = None
    option_index: int | None = None


@dataclass(slots=True)
class SalvageCapacity:
    used: int
    capacity: int
    source: str = "manual"
    raw_text: str | None = None

    def __post_init__(self) -> None:
        if self.used < 0:
            raise CalculationError("Salvage capacity 'used' cannot be negative")
        if self.capacity <= 0:
            raise CalculationError("Salvage capacity must be greater than zero")
        if self.used > self.capacity:
            raise CalculationError(
                f"Salvage inventory reports {self.used}/{self.capacity}; used cannot exceed capacity"
            )


_CAPACITY_PATTERNS = (
    re.compile(r"(?<!\d)(\d{1,5})\s*/\s*(\d{1,5})(?!\d)"),
    re.compile(r"(?<!\d)(\d{1,5})\s+(?:of|OF|Of)\s+(\d{1,5})(?!\d)"),
)


def parse_capacity_text(text: str, *, source: str = "screenshot_ocr") -> SalvageCapacity:
    """Extract a salvage used/capacity pair from OCR text such as ``169 / 172``.

    The OCR/image layer is deliberately separate from the calculator. It only needs
    to pass the recognized text through this function. If more than one plausible
    fraction appears, the largest capacity is preferred because CoH screenshots may
    contain unrelated fractions elsewhere in the captured region.
    """
    text = text or ""
    candidates: list[tuple[int, int]] = []
    for pattern in _CAPACITY_PATTERNS:
        for m in pattern.finditer(text):
            used, capacity = int(m.group(1)), int(m.group(2))
            if capacity > 0 and used <= capacity:
                candidates.append((used, capacity))
    if not candidates:
        raise CalculationError(f"Could not find a plausible salvage capacity in OCR text: {text!r}")
    used, capacity = max(candidates, key=lambda x: (x[1], x[0]))
    return SalvageCapacity(used=used, capacity=capacity, source=source, raw_text=text)


def capacity_from_payload(value: Any) -> SalvageCapacity | None:
    if value is None:
        return None
    if isinstance(value, str):
        return parse_capacity_text(value)
    if not isinstance(value, dict):
        raise CalculationError("salvage_capacity must be an object, OCR text string, or null")
    source = str(value.get("source") or ("screenshot_ocr" if "text" in value else "manual"))
    if "text" in value:
        return parse_capacity_text(str(value["text"]), source=source)
    if "used" not in value or "capacity" not in value:
        raise CalculationError("salvage_capacity requires both 'used' and 'capacity', or a 'text' field")
    return SalvageCapacity(
        used=int(value["used"]),
        capacity=int(value["capacity"]),
        source=source,
        raw_text=value.get("raw_text"),
    )


_RARITY_ORDER = {"common": 0, "uncommon": 1, "rare": 2}


class CraftingCalculator:
    """Read-only calculator over a Stage 1 Homecoming recipe SQLite database."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise CalculationError(f"Recipe database does not exist: {self.db_path}")
        uri = f"file:{self.db_path.resolve().as_posix()}?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True)
        self.conn.row_factory = sqlite3.Row
        required_tables = {"recipes", "recipe_levels", "craft_options", "craft_requirements", "salvage"}
        present = {
            row[0]
            for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = sorted(required_tables - present)
        if missing:
            self.conn.close()
            raise CalculationError(f"Database is missing calculator tables: {', '.join(missing)}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "CraftingCalculator":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _resolve_recipe(self, value: str) -> sqlite3.Row:
        # Prefer an exact visible name before normalized aliases.
        exact = self.conn.execute(
            "SELECT * FROM recipes WHERE name=? COLLATE NOCASE ORDER BY recipe_type, name", (value,)
        ).fetchall()
        if len(exact) == 1:
            return exact[0]
        key = canonical_key(value)
        rows = self.conn.execute(
            """
            SELECT DISTINCT r.*
            FROM recipe_aliases a JOIN recipes r ON r.id=a.recipe_id
            WHERE a.alias_key=?
            ORDER BY r.recipe_type,r.name
            """,
            (key,),
        ).fetchall()
        if not rows:
            # Canonical keys in the recipes table are also useful when an older DB
            # was built without a full alias table.
            rows = self.conn.execute(
                "SELECT * FROM recipes WHERE canonical_key=? ORDER BY recipe_type,name", (key,)
            ).fetchall()
        if not rows:
            raise CalculationError(f"Unknown recipe: {value!r}")
        if len(rows) > 1:
            choices = ", ".join(f"{r['name']} [{r['recipe_type']}]" for r in rows[:8])
            raise CalculationError(f"Ambiguous recipe {value!r}; matches: {choices}")
        return rows[0]

    def _resolve_salvage(self, value: str) -> sqlite3.Row:
        exact = self.conn.execute("SELECT * FROM salvage WHERE name=? COLLATE NOCASE", (value,)).fetchall()
        if len(exact) == 1:
            return exact[0]
        key = canonical_key(value)
        row = self.conn.execute(
            """
            SELECT s.* FROM salvage_aliases a JOIN salvage s ON s.id=a.salvage_id
            WHERE a.alias_key=?
            """,
            (key,),
        ).fetchone()
        if row is None:
            row = self.conn.execute("SELECT * FROM salvage WHERE canonical_key=?", (key,)).fetchone()
        if row is None:
            raise CalculationError(f"Unknown standard invention salvage: {value!r}")
        return row

    def _normalize_inventory(self, inventory: dict[str, Any] | None) -> tuple[dict[int, int], dict[int, sqlite3.Row]]:
        quantities: dict[int, int] = Counter()
        metadata: dict[int, sqlite3.Row] = {}
        for name, raw_qty in (inventory or {}).items():
            try:
                qty = int(raw_qty)
            except (TypeError, ValueError) as exc:
                raise CalculationError(f"Inventory quantity for {name!r} is not an integer: {raw_qty!r}") from exc
            if qty < 0:
                raise CalculationError(f"Inventory quantity for {name!r} cannot be negative")
            if qty == 0:
                continue
            row = self._resolve_salvage(name)
            sid = int(row["id"])
            quantities[sid] += qty
            metadata[sid] = row
        return dict(quantities), metadata

    def _selection_detail(self, selection: RecipeSelection) -> dict[str, Any]:
        if selection.quantity <= 0:
            raise CalculationError(f"Recipe quantity must be positive for {selection.recipe!r}")
        recipe = self._resolve_recipe(selection.recipe)
        levels = [
            int(r["level"])
            for r in self.conn.execute(
                "SELECT level FROM recipe_levels WHERE recipe_id=? ORDER BY level", (recipe["id"],)
            )
        ]
        if not levels:
            raise CalculationError(f"Recipe has no craftable levels: {recipe['name']}")
        level = selection.level
        if level is None:
            if len(levels) == 1:
                level = levels[0]
            else:
                raise CalculationError(
                    f"Recipe {recipe['name']!r} exists at multiple levels ({levels[0]}-{levels[-1]}); specify 'level'"
                )
        if level not in levels:
            raise CalculationError(
                f"Recipe {recipe['name']!r} is not craftable at level {level}; available levels: "
                f"{levels[0]}-{levels[-1]}"
            )
        rl = self.conn.execute(
            "SELECT id FROM recipe_levels WHERE recipe_id=? AND level=?", (recipe["id"], level)
        ).fetchone()
        options = self.conn.execute(
            "SELECT * FROM craft_options WHERE recipe_level_id=? ORDER BY option_index", (rl["id"],)
        ).fetchall()
        if not options:
            raise CalculationError(f"Recipe {recipe['name']!r} L{level} has no crafting option")
        if selection.option_index is None:
            if len(options) != 1:
                descriptions = "; ".join(
                    f"option {o['option_index']} (cost {o['crafting_cost']})" for o in options
                )
                raise CalculationError(
                    f"Recipe {recipe['name']!r} L{level} has multiple crafting options; specify 'option_index'. "
                    f"Available: {descriptions}"
                )
            option = options[0]
        else:
            matches = [o for o in options if int(o["option_index"]) == int(selection.option_index)]
            if not matches:
                allowed = ", ".join(str(o["option_index"]) for o in options)
                raise CalculationError(
                    f"Invalid option_index {selection.option_index} for {recipe['name']!r} L{level}; available: {allowed}"
                )
            option = matches[0]

        reqs = [
            dict(r)
            for r in self.conn.execute(
                """
                SELECT s.id salvage_id,s.name salvage,s.rarity,s.level_tier,s.origin,cr.quantity
                FROM craft_requirements cr JOIN salvage s ON s.id=cr.salvage_id
                WHERE cr.craft_option_id=? ORDER BY s.name
                """,
                (option["id"],),
            )
        ]
        return {
            "recipe_id": int(recipe["id"]),
            "recipe": recipe["name"],
            "recipe_type": recipe["recipe_type"],
            "recipe_rarity": recipe["recipe_rarity"],
            "level": int(level),
            "quantity": int(selection.quantity),
            "option_index": int(option["option_index"]),
            "crafting_cost_each": option["crafting_cost"],
            "crafting_cost_total": None
            if option["crafting_cost"] is None
            else int(option["crafting_cost"]) * int(selection.quantity),
            "requirements_each": [
                {k: v for k, v in req.items() if k != "salvage_id"} for req in reqs
            ],
            "_requirements": reqs,
        }

    def calculate(
        self,
        selections: Iterable[RecipeSelection],
        *,
        inventory: dict[str, Any] | None = None,
        salvage_capacity: SalvageCapacity | None = None,
        allowed_disposal_rarities: Iterable[str] = ("common",),
    ) -> dict[str, Any]:
        selections = list(selections)
        if not selections:
            raise CalculationError("At least one recipe must be selected")
        allowed = [str(x).lower() for x in allowed_disposal_rarities]
        invalid = [x for x in allowed if x not in _RARITY_ORDER]
        if invalid:
            raise CalculationError(f"Unknown disposal rarity: {', '.join(invalid)}")
        # Preserve user order but remove duplicates in the policy.
        allowed = list(dict.fromkeys(allowed))

        inv, inv_meta = self._normalize_inventory(inventory)
        details = [self._selection_detail(s) for s in selections]
        required: Counter[int] = Counter()
        salvage_meta: dict[int, dict[str, Any]] = {}
        total_cost = 0
        cost_complete = True
        for detail in details:
            if detail["crafting_cost_total"] is None:
                cost_complete = False
            else:
                total_cost += int(detail["crafting_cost_total"])
            for req in detail.pop("_requirements"):
                sid = int(req["salvage_id"])
                required[sid] += int(req["quantity"]) * int(detail["quantity"])
                salvage_meta[sid] = req
        for sid, row in inv_meta.items():
            salvage_meta.setdefault(
                sid,
                {
                    "salvage_id": sid,
                    "salvage": row["name"],
                    "rarity": row["rarity"],
                    "level_tier": row["level_tier"],
                    "origin": row["origin"],
                    "quantity": 0,
                },
            )

        comparison: list[dict[str, Any]] = []
        for sid in sorted(set(required) | set(inv), key=lambda x: salvage_meta[x]["salvage"]):
            meta = salvage_meta[sid]
            need = int(required.get(sid, 0))
            have = int(inv.get(sid, 0))
            buy = max(need - have, 0)
            surplus = max(have - need, 0)
            comparison.append(
                {
                    "salvage": meta["salvage"],
                    "rarity": meta["rarity"],
                    "level_tier": meta["level_tier"],
                    "origin": meta["origin"],
                    "required": need,
                    "have": have,
                    "buy": buy,
                    "surplus": surplus,
                    "disposal_allowed": meta["rarity"] in allowed,
                }
            )

        shopping = [dict(x) for x in comparison if x["buy"] > 0]
        safe_surplus = [dict(x) for x in comparison if x["surplus"] > 0]
        safe_to_dispose = [dict(x) for x in safe_surplus if x["disposal_allowed"]]
        buy_total = sum(x["buy"] for x in shopping)
        total_required = sum(required.values())
        covered = sum(min(int(required.get(sid, 0)), int(inv.get(sid, 0))) for sid in required)
        tracked_inventory_total = sum(inv.values())

        capacity_result: dict[str, Any] | None = None
        if salvage_capacity is not None:
            if salvage_capacity.used < tracked_inventory_total:
                raise CalculationError(
                    f"Capacity reports {salvage_capacity.used} salvage used, but the tracked inventory totals "
                    f"{tracked_inventory_total}. Correct the screenshot/manual capacity or inventory quantities."
                )
            free_before = salvage_capacity.capacity - salvage_capacity.used
            room_needed = max(buy_total - free_before, 0)

            # Prefer lower rarity, then salvage that is completely unused by the selected crafts,
            # then large surplus stacks. This usually frees room while touching the fewest useful stacks.
            candidates = sorted(
                safe_to_dispose,
                key=lambda x: (
                    _RARITY_ORDER[x["rarity"]],
                    0 if x["required"] == 0 else 1,
                    -x["surplus"],
                    x["salvage"],
                ),
            )
            remaining = room_needed
            recommendations: list[dict[str, Any]] = []
            for item in candidates:
                if remaining <= 0:
                    break
                qty = min(int(item["surplus"]), remaining)
                if qty:
                    recommendations.append(
                        {
                            "salvage": item["salvage"],
                            "rarity": item["rarity"],
                            "quantity": qty,
                            "have": item["have"],
                            "required": item["required"],
                            "remaining_after_disposal": item["have"] - qty,
                        }
                    )
                    remaining -= qty
            disposed = room_needed - remaining
            free_after = free_before + disposed
            capacity_result = {
                "source": salvage_capacity.source,
                "raw_text": salvage_capacity.raw_text,
                "used": salvage_capacity.used,
                "capacity": salvage_capacity.capacity,
                "free_before": free_before,
                "tracked_inventory_total": tracked_inventory_total,
                "untracked_inventory_count": salvage_capacity.used - tracked_inventory_total,
                "items_to_buy": buy_total,
                "room_needed_to_buy_all_before_crafting": room_needed,
                "recommended_disposals": recommendations,
                "recommended_disposal_total": disposed,
                "free_after_recommended_disposals": free_after,
                "remaining_room_shortfall": remaining,
                "can_buy_all_before_crafting": remaining == 0,
                "projected_used_after_disposal_and_buy": salvage_capacity.used - disposed + buy_total,
                "strategy": "buy_all_before_crafting",
            }

        return {
            "summary": {
                "recipe_lines": len(details),
                "craft_count": sum(int(x["quantity"]) for x in details),
                "crafting_cost_total": total_cost if cost_complete else None,
                "salvage_required_total": total_required,
                "salvage_required_types": len(required),
                "salvage_covered_by_inventory": covered,
                "shopping_total": buy_total,
                "shopping_types": len(shopping),
                "tracked_inventory_total": tracked_inventory_total,
                "allowed_disposal_rarities": allowed,
            },
            "selected_recipes": details,
            "comparison": comparison,
            "shopping_list": shopping,
            "safe_surplus": safe_surplus,
            "safe_to_dispose": safe_to_dispose,
            "space_plan": capacity_result,
        }


def selections_from_payload(payload: dict[str, Any]) -> list[RecipeSelection]:
    raw = payload.get("recipes")
    if not isinstance(raw, list) or not raw:
        raise CalculationError("Input JSON must contain a non-empty 'recipes' array")
    out: list[RecipeSelection] = []
    for idx, item in enumerate(raw):
        if isinstance(item, str):
            out.append(RecipeSelection(recipe=item))
            continue
        if not isinstance(item, dict) or not item.get("recipe"):
            raise CalculationError(f"recipes[{idx}] must contain a 'recipe' name")
        out.append(
            RecipeSelection(
                recipe=str(item["recipe"]),
                quantity=int(item.get("quantity", 1)),
                level=None if item.get("level") is None else int(item["level"]),
                option_index=None if item.get("option_index") is None else int(item["option_index"]),
            )
        )
    return out


def calculate_payload(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    inventory = payload.get("inventory") or {}
    if not isinstance(inventory, dict):
        raise CalculationError("'inventory' must be an object mapping salvage names to quantities")
    policy = payload.get("disposal_policy") or {}
    if not isinstance(policy, dict):
        raise CalculationError("'disposal_policy' must be an object")
    allowed = policy.get("allowed_rarities", ["common"])
    if isinstance(allowed, str):
        allowed = [allowed]
    capacity = capacity_from_payload(payload.get("salvage_capacity"))
    with CraftingCalculator(db_path) as calculator:
        return calculator.calculate(
            selections_from_payload(payload),
            inventory=inventory,
            salvage_capacity=capacity,
            allowed_disposal_rarities=allowed,
        )


def _table_lines(rows: list[dict[str, Any]], quantity_field: str, *, max_columns: int = 3) -> list[str]:
    if not rows:
        return ["  (none)"]
    entries = [f"{int(r[quantity_field]):>3}  {r['salvage']}" for r in rows]
    width = max(len(x) for x in entries) + 3
    lines: list[str] = []
    for i in range(0, len(entries), max_columns):
        lines.append("".join(x.ljust(width) for x in entries[i : i + max_columns]).rstrip())
    return lines


def format_text_result(result: dict[str, Any]) -> str:
    s = result["summary"]
    lines = [
        "Homecoming Crafting Shopping List",
        "=================================",
        f"Crafts: {s['craft_count']} ({s['recipe_lines']} recipe lines)",
        f"Salvage required: {s['salvage_required_total']} across {s['salvage_required_types']} types",
        f"Already covered: {s['salvage_covered_by_inventory']}",
        f"Need to buy: {s['shopping_total']} across {s['shopping_types']} types",
    ]
    if s["crafting_cost_total"] is not None:
        lines.append(f"Crafting cost: {s['crafting_cost_total']:,} influence")
    lines += ["", "BUY", "---"]
    lines += _table_lines(result["shopping_list"], "buy")
    lines += ["", "SAFE SURPLUS (all rarities)", "---------------------------"]
    lines += _table_lines(result["safe_surplus"], "surplus")
    lines += ["", "SAFE TO SELL/DELETE UNDER CURRENT POLICY", "----------------------------------------"]
    lines += _table_lines(result["safe_to_dispose"], "surplus")

    space = result.get("space_plan")
    if space is not None:
        lines += [
            "",
            "SPACE PLAN",
            "----------",
            f"Capacity: {space['used']}/{space['capacity']} ({space['free_before']} free; source={space['source']})",
            f"Need room for {space['items_to_buy']} purchases: {space['room_needed_to_buy_all_before_crafting']} additional spaces",
            "Recommended disposals:",
        ]
        lines += _table_lines(space["recommended_disposals"], "quantity")
        if space["remaining_room_shortfall"]:
            lines.append(
                f"Still short {space['remaining_room_shortfall']} spaces after all allowed safe surplus; "
                "craft in batches or explicitly permit additional rarities."
            )
        else:
            lines.append("Enough room to buy the complete shopping list before crafting.")
    return "\n".join(lines) + "\n"


def load_payload(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalculationError(f"Could not read calculator input {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CalculationError("Calculator input JSON must contain one top-level object")
    return value
