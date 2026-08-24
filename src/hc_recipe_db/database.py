from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import Recipe, Salvage
from .normalize import canonical_key

SCHEMA = r"""
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_pages (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    revision_id INTEGER,
    revision_timestamp TEXT,
    parser_kind TEXT NOT NULL,
    discovered_from TEXT,
    raw_cache_path TEXT
);

CREATE TABLE IF NOT EXISTS salvage (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    canonical_key TEXT NOT NULL UNIQUE,
    rarity TEXT NOT NULL CHECK(rarity IN ('common','uncommon','rare')),
    level_tier TEXT NOT NULL CHECK(level_tier IN ('low','mid','high')),
    origin TEXT NOT NULL CHECK(origin IN ('arcane','technological')),
    wiki_title TEXT,
    wiki_url TEXT
);

CREATE TABLE IF NOT EXISTS salvage_aliases (
    alias_key TEXT PRIMARY KEY,
    alias_text TEXT NOT NULL,
    salvage_id INTEGER NOT NULL REFERENCES salvage(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    recipe_type TEXT NOT NULL,
    recipe_rarity TEXT,
    set_name TEXT,
    min_level INTEGER,
    max_level INTEGER,
    source_page_id INTEGER NOT NULL REFERENCES source_pages(id),
    notes_json TEXT NOT NULL DEFAULT '[]',
    categories_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE(name, recipe_type)
);
CREATE INDEX IF NOT EXISTS idx_recipes_key ON recipes(canonical_key);
CREATE INDEX IF NOT EXISTS idx_recipes_type ON recipes(recipe_type);

CREATE TABLE IF NOT EXISTS recipe_aliases (
    alias_key TEXT NOT NULL,
    alias_text TEXT NOT NULL,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    PRIMARY KEY(alias_key, recipe_id)
);
CREATE INDEX IF NOT EXISTS idx_recipe_aliases_key ON recipe_aliases(alias_key);

CREATE TABLE IF NOT EXISTS recipe_levels (
    id INTEGER PRIMARY KEY,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    level INTEGER NOT NULL CHECK(level BETWEEN 0 AND 50),
    UNIQUE(recipe_id, level)
);

CREATE TABLE IF NOT EXISTS craft_options (
    id INTEGER PRIMARY KEY,
    recipe_level_id INTEGER NOT NULL REFERENCES recipe_levels(id) ON DELETE CASCADE,
    option_index INTEGER NOT NULL,
    crafting_cost INTEGER,
    UNIQUE(recipe_level_id, option_index)
);

CREATE TABLE IF NOT EXISTS craft_requirements (
    id INTEGER PRIMARY KEY,
    craft_option_id INTEGER NOT NULL REFERENCES craft_options(id) ON DELETE CASCADE,
    salvage_id INTEGER NOT NULL REFERENCES salvage(id),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    raw_salvage_name TEXT,
    UNIQUE(craft_option_id, salvage_id)
);

CREATE TABLE IF NOT EXISTS discovery_audit (
    id INTEGER PRIMARY KEY,
    page_title TEXT,
    discovered_from TEXT,
    status TEXT NOT NULL,
    detail TEXT
);
"""


class RecipeDatabase:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def reset_data(self) -> None:
        cur = self.conn.cursor()
        for table in (
            "craft_requirements", "craft_options", "recipe_levels", "recipe_aliases", "recipes",
            "salvage_aliases", "salvage", "source_pages", "discovery_audit", "metadata"
        ):
            cur.execute(f"DELETE FROM {table}")
        self.conn.commit()

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def add_source(self, *, title: str, url: str, revision_id: int | None, timestamp: str | None,
                   parser_kind: str, discovered_from: str | None = None, raw_cache_path: str | None = None) -> int:
        self.conn.execute(
            """INSERT INTO source_pages(title,url,revision_id,revision_timestamp,parser_kind,discovered_from,raw_cache_path)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(title) DO UPDATE SET
                 url=excluded.url, revision_id=excluded.revision_id, revision_timestamp=excluded.revision_timestamp,
                 parser_kind=excluded.parser_kind, discovered_from=COALESCE(excluded.discovered_from,source_pages.discovered_from),
                 raw_cache_path=excluded.raw_cache_path""",
            (title, url, revision_id, timestamp, parser_kind, discovered_from, raw_cache_path),
        )
        row = self.conn.execute("SELECT id FROM source_pages WHERE title=?", (title,)).fetchone()
        return int(row[0])

    def add_salvage(self, items: Iterable[Salvage]) -> None:
        cur = self.conn.cursor()
        for item in items:
            cur.execute(
                """INSERT INTO salvage(name,canonical_key,rarity,level_tier,origin,wiki_title,wiki_url)
                   VALUES(?,?,?,?,?,?,?)""",
                (item.name, canonical_key(item.name), item.rarity, item.level_tier, item.origin, item.wiki_title, item.wiki_url),
            )
        # Recognition aliases useful later for OCR and current wiki plurals.
        alias_pairs = {
            "Masterwork Weapons": "Masterwork Weapon",
            "Demonic Threat Reports": "Demonic Threat Report",
            "Runes": "Rune",
            "Scientific Theories": "Scientific Theory",
            "Rubies": "Ruby",
        }
        for alias, target in alias_pairs.items():
            row = cur.execute("SELECT id FROM salvage WHERE name=?", (target,)).fetchone()
            if row:
                cur.execute("INSERT OR REPLACE INTO salvage_aliases(alias_key,alias_text,salvage_id) VALUES(?,?,?)",
                            (canonical_key(alias), alias, int(row[0])))
        # Every canonical name is also an alias, which gives the future OCR layer one lookup table.
        for row in cur.execute("SELECT id,name,canonical_key FROM salvage").fetchall():
            cur.execute("INSERT OR IGNORE INTO salvage_aliases(alias_key,alias_text,salvage_id) VALUES(?,?,?)",
                        (row["canonical_key"], row["name"], row["id"]))
        self.conn.commit()

    def add_recipe(self, recipe: Recipe, source_page_id: int) -> int:
        cur = self.conn.cursor()
        cur.execute(
            """INSERT INTO recipes(name,canonical_key,recipe_type,recipe_rarity,set_name,min_level,max_level,
                                    source_page_id,notes_json,categories_json)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(name,recipe_type) DO UPDATE SET
                 canonical_key=excluded.canonical_key, recipe_rarity=excluded.recipe_rarity,
                 set_name=excluded.set_name, min_level=excluded.min_level, max_level=excluded.max_level,
                 source_page_id=excluded.source_page_id, notes_json=excluded.notes_json,
                 categories_json=excluded.categories_json""",
            (recipe.name, canonical_key(recipe.name), recipe.recipe_type, recipe.recipe_rarity, recipe.set_name,
             recipe.min_level, recipe.max_level, source_page_id, json.dumps(recipe.notes, ensure_ascii=False),
             json.dumps(recipe.categories, ensure_ascii=False)),
        )
        recipe_id = int(cur.execute("SELECT id FROM recipes WHERE name=? AND recipe_type=?",
                                    (recipe.name, recipe.recipe_type)).fetchone()[0])
        cur.execute("DELETE FROM recipe_aliases WHERE recipe_id=?", (recipe_id,))
        cur.execute("INSERT INTO recipe_aliases(alias_key,alias_text,recipe_id) VALUES(?,?,?)",
                    (canonical_key(recipe.name), recipe.name, recipe_id))
        cur.execute("DELETE FROM recipe_levels WHERE recipe_id=?", (recipe_id,))

        for rl in recipe.levels:
            cur.execute("INSERT INTO recipe_levels(recipe_id,level) VALUES(?,?)", (recipe_id, rl.level))
            level_id = int(cur.lastrowid)
            for idx, option in enumerate(rl.options):
                cur.execute("INSERT INTO craft_options(recipe_level_id,option_index,crafting_cost) VALUES(?,?,?)",
                            (level_id, idx, option.crafting_cost))
                option_id = int(cur.lastrowid)
                # Normalize duplicate ingredient mentions before writing.
                # A malformed/legacy wiki template can emit the same salvage
                # more than once; the database models the actual total needed.
                aggregated: dict[str, tuple[str, int, list[str]]] = {}
                for req in option.requirements:
                    key = canonical_key(req.salvage_name)
                    if key not in aggregated:
                        aggregated[key] = (req.salvage_name, req.quantity, [req.raw_name or req.salvage_name])
                    else:
                        name, qty, raws = aggregated[key]
                        raws.append(req.raw_name or req.salvage_name)
                        aggregated[key] = (name, qty + req.quantity, raws)
                for key, (salvage_name, quantity, raws) in aggregated.items():
                    srow = cur.execute("SELECT id FROM salvage WHERE canonical_key=?", (key,)).fetchone()
                    if not srow:
                        raise KeyError(f"Unknown salvage in recipe {recipe.name} L{rl.level}: {salvage_name}")
                    cur.execute(
                        "INSERT INTO craft_requirements(craft_option_id,salvage_id,quantity,raw_salvage_name) VALUES(?,?,?,?)",
                        (option_id, int(srow[0]), quantity, " | ".join(dict.fromkeys(raws))),
                    )
        self.conn.commit()
        return recipe_id

    def audit(self, page_title: str | None, discovered_from: str | None, status: str, detail: str = "") -> None:
        self.conn.execute("INSERT INTO discovery_audit(page_title,discovered_from,status,detail) VALUES(?,?,?,?)",
                          (page_title, discovered_from, status, detail))

    def counts(self) -> dict[str, int]:
        out = {}
        for table in ("salvage", "recipes", "recipe_aliases", "recipe_levels", "craft_options", "craft_requirements", "source_pages"):
            out[table] = int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        return out

    def export(self, export_dir: str | Path) -> None:
        export_dir = Path(export_dir)
        export_dir.mkdir(parents=True, exist_ok=True)

        # Human-readable normalized JSON.
        salvage = [dict(r) for r in self.conn.execute(
            "SELECT name,rarity,level_tier,origin,wiki_title,wiki_url FROM salvage ORDER BY rarity,level_tier,origin,name"
        )]
        (export_dir / "salvage.json").write_text(json.dumps(salvage, ensure_ascii=False, indent=2), encoding="utf-8")

        recipes: list[dict] = []
        for r in self.conn.execute("""
            SELECT r.*,sp.title source_title,sp.url source_url,sp.revision_id source_revision,
                   sp.revision_timestamp source_timestamp
            FROM recipes r JOIN source_pages sp ON sp.id=r.source_page_id
            ORDER BY r.recipe_type,r.name
        """):
            item = {
                "name": r["name"], "type": r["recipe_type"], "rarity": r["recipe_rarity"],
                "set_name": r["set_name"], "min_level": r["min_level"], "max_level": r["max_level"],
                "source": {
                    "title": r["source_title"], "url": r["source_url"],
                    "revision_id": r["source_revision"], "timestamp": r["source_timestamp"],
                },
                "levels": []
            }
            for lv in self.conn.execute("SELECT * FROM recipe_levels WHERE recipe_id=? ORDER BY level", (r["id"],)):
                level = {"level": lv["level"], "options": []}
                for op in self.conn.execute("SELECT * FROM craft_options WHERE recipe_level_id=? ORDER BY option_index", (lv["id"],)):
                    reqs = [dict(x) for x in self.conn.execute(
                        """SELECT s.name AS salvage, cr.quantity FROM craft_requirements cr
                           JOIN salvage s ON s.id=cr.salvage_id WHERE cr.craft_option_id=? ORDER BY s.name""", (op["id"],)
                    )]
                    level["options"].append({"crafting_cost": op["crafting_cost"], "requirements": reqs})
                item["levels"].append(level)
            recipes.append(item)
        (export_dir / "recipes.json").write_text(json.dumps(recipes, ensure_ascii=False, indent=2), encoding="utf-8")

        # CSVs are convenient for spot-checking in any spreadsheet without making them canonical.
        with (export_dir / "salvage.csv").open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["name", "rarity", "level_tier", "origin", "wiki_title", "wiki_url"])
            w.writeheader(); w.writerows(salvage)

        with (export_dir / "requirements.csv").open("w", newline="", encoding="utf-8-sig") as f:
            fields = ["recipe", "recipe_type", "level", "option_index", "crafting_cost", "salvage", "quantity",
                      "source_title", "source_revision"]
            w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
            for row in self.conn.execute("""
                SELECT r.name recipe,r.recipe_type,rl.level,co.option_index,co.crafting_cost,s.name salvage,cr.quantity,
                       sp.title source_title,sp.revision_id source_revision
                FROM craft_requirements cr
                JOIN craft_options co ON co.id=cr.craft_option_id
                JOIN recipe_levels rl ON rl.id=co.recipe_level_id
                JOIN recipes r ON r.id=rl.recipe_id
                JOIN source_pages sp ON sp.id=r.source_page_id
                JOIN salvage s ON s.id=cr.salvage_id
                ORDER BY r.recipe_type,r.name,rl.level,co.option_index,s.name
            """):
                w.writerow(dict(row))


        # Diagnostic exports are intentionally small and upload-friendly.
        # They make a live crawl debuggable without requiring the full SQLite
        # database or raw page cache.
        with (export_dir / "discovery_audit.csv").open("w", newline="", encoding="utf-8-sig") as f:
            fields = ["page_title", "discovered_from", "status", "detail"]
            w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
            for row in self.conn.execute(
                "SELECT page_title,discovered_from,status,detail FROM discovery_audit ORDER BY status,page_title"
            ):
                w.writerow(dict(row))

        with (export_dir / "recipe_notes.csv").open("w", newline="", encoding="utf-8-sig") as f:
            fields = ["recipe", "recipe_type", "notes_json", "source_title", "source_revision"]
            w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
            for row in self.conn.execute("""
                SELECT r.name recipe,r.recipe_type,r.notes_json,sp.title source_title,sp.revision_id source_revision
                FROM recipes r JOIN source_pages sp ON sp.id=r.source_page_id
                WHERE r.notes_json != '[]' ORDER BY r.recipe_type,r.name
            """):
                w.writerow(dict(row))

        with (export_dir / "empty_requirements.csv").open("w", newline="", encoding="utf-8-sig") as f:
            fields = ["recipe", "recipe_type", "level", "option_index", "crafting_cost", "source_title", "source_revision"]
            w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
            for row in self.conn.execute("""
                SELECT r.name recipe,r.recipe_type,rl.level,co.option_index,co.crafting_cost,
                       sp.title source_title,sp.revision_id source_revision
                FROM craft_options co
                JOIN recipe_levels rl ON rl.id=co.recipe_level_id
                JOIN recipes r ON r.id=rl.recipe_id
                JOIN source_pages sp ON sp.id=r.source_page_id
                LEFT JOIN craft_requirements cr ON cr.craft_option_id=co.id
                WHERE cr.id IS NULL
                ORDER BY r.recipe_type,r.name,rl.level,co.option_index
            """):
                w.writerow(dict(row))
