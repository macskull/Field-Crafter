from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass(slots=True)
class Check:
    severity: str  # PASS/WARN/ERROR
    name: str
    detail: str


def _requirements(conn: sqlite3.Connection, recipe: str, level: int, option: int = 0) -> tuple[int | None, dict[str, int]] | None:
    row = conn.execute("""
        SELECT co.id,co.crafting_cost FROM recipes r
        JOIN recipe_levels rl ON rl.recipe_id=r.id
        JOIN craft_options co ON co.recipe_level_id=rl.id
        WHERE r.name=? AND rl.level=? AND co.option_index=?
    """, (recipe, level, option)).fetchone()
    if not row:
        return None
    reqs = {
        x[0]: x[1] for x in conn.execute("""
            SELECT s.name,cr.quantity FROM craft_requirements cr
            JOIN salvage s ON s.id=cr.salvage_id WHERE cr.craft_option_id=?
        """, (row[0],))
    }
    return row[1], reqs


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def validate_database(db_path: str | Path) -> list[Check]:
    conn = sqlite3.connect(db_path)
    checks: list[Check] = []

    salvage_count = conn.execute("SELECT COUNT(*) FROM salvage").fetchone()[0]
    if salvage_count == 108:
        checks.append(Check("PASS", "Invention salvage count", "108 records (matches Homecoming Wiki's stated total)."))
    else:
        checks.append(Check("ERROR", "Invention salvage count", f"Expected 108, found {salvage_count}."))

    combos = conn.execute("SELECT rarity,level_tier,origin,COUNT(*) FROM salvage GROUP BY rarity,level_tier,origin").fetchall()
    bad = [r for r in combos if r[3] != 6]
    if len(combos) == 18 and not bad:
        checks.append(Check("PASS", "Salvage taxonomy", "All 18 rarity/tier/origin combinations contain exactly 6 salvage items."))
    else:
        checks.append(Check("ERROR", "Salvage taxonomy", f"Unexpected combination counts: {combos}"))

    salvage_mismatch = int(_meta(conn, "salvage_category_mismatch_count") or 0)
    salvage_missing = int(_meta(conn, "salvage_category_missing_count") or 0)
    checks.append(Check(
        "ERROR" if salvage_mismatch else "PASS",
        "Salvage individual-page taxonomy cross-check",
        f"Tier-table vs individual-page category mismatches: {salvage_mismatch}.",
    ))
    checks.append(Check(
        "WARN" if salvage_missing else "PASS",
        "Salvage category metadata completeness",
        f"Individual salvage pages missing one or more taxonomy categories: {salvage_missing}.",
    ))

    recipes = conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
    if recipes:
        checks.append(Check("PASS", "Recipe records present", f"{recipes} recipes extracted."))
    else:
        checks.append(Check("ERROR", "Recipe records present", "No recipes were extracted."))

    type_counts = dict(conn.execute("SELECT recipe_type,COUNT(*) FROM recipes GROUP BY recipe_type").fetchall())
    checks.append(Check("PASS", "Recipe type coverage", ", ".join(f"{k}={v}" for k, v in sorted(type_counts.items())) or "none"))

    common_seed_raw = _meta(conn, "common_io_recipe_type_count")
    if common_seed_raw is not None:
        expected = int(common_seed_raw)
        actual = type_counts.get("common_io", 0)
        checks.append(Check(
            "PASS" if actual == expected and expected > 0 else "ERROR",
            "Common IO canonical coverage",
            f"Canonical Common Invention Recipes parsed={expected}; final common IO recipes={actual}.",
        ))

    temp_expected_raw = _meta(conn, "temporary_power_candidate_count")
    if temp_expected_raw is not None:
        expected = int(temp_expected_raw)
        titles: list[str] = []
        raw_discrepancies = _meta(conn, "discovery_discrepancies_json")
        if raw_discrepancies:
            try:
                titles = list(json.loads(raw_discrepancies).get("temporary_power_index_titles", []))
            except Exception:
                titles = []

        parsed = 0
        no_recipe = 0
        missing_page = 0
        bad: list[str] = []
        for title in titles:
            row = conn.execute(
                "SELECT status,detail FROM discovery_audit WHERE page_title=? ORDER BY id DESC LIMIT 1",
                (title,),
            ).fetchone()
            if not row:
                bad.append(f"{title}: no audit disposition")
                continue
            status = row[0]
            if status == "parsed":
                parsed += 1
            elif status == "skipped_no_recipe_table":
                no_recipe += 1
            elif status == "skipped_missing_page":
                missing_page += 1
            else:
                bad.append(f"{title}: {status}")

        # Older temporary-power index pages contain a few stale/non-recipe
        # references. Completeness means every link has a known disposition,
        # not that every linked power still has an invention recipe page.
        actual_total = type_counts.get("temporary_power", 0)
        extras = max(0, actual_total - parsed)
        accounted = parsed + no_recipe + missing_page
        detail = (
            f"Index links={expected}; accounted={accounted}: parsed recipes={parsed}, "
            f"current pages without recipe tables={no_recipe}, missing/stale pages={missing_page}; "
            f"additional temporary-power recipes recovered from category cross-checks={extras}."
        )
        if bad:
            detail += " Unresolved dispositions: " + "; ".join(bad[:8]) + "."
        checks.append(Check(
            "PASS" if not bad and accounted == expected else "ERROR",
            "Temporary-power index disposition",
            detail,
        ))

    costume_seed_raw = _meta(conn, "costume_index_recipe_count")
    if costume_seed_raw is not None:
        seed = int(costume_seed_raw)
        actual = type_counts.get("costume", 0)
        checks.append(Check(
            "PASS" if actual == seed and seed > 0 else "ERROR",
            "Costume recipe coverage",
            f"Canonical index parsed={seed}; final costume recipes={actual}.",
        ))

    vanity_seed_raw = _meta(conn, "vanity_pet_invention_salvage_recipe_count")
    if vanity_seed_raw is not None:
        expected = int(vanity_seed_raw)
        actual = type_counts.get("vanity_pet", 0)
        checks.append(Check(
            "PASS" if actual == expected and expected > 0 else "WARN",
            "Vanity-pet invention-salvage coverage",
            f"Supplemental source parsed={expected}; final vanity-pet recipes={actual}.",
        ))

    empty_levels = conn.execute("""
        SELECT COUNT(*) FROM recipe_levels rl
        LEFT JOIN craft_options co ON co.recipe_level_id=rl.id
        WHERE co.id IS NULL
    """).fetchone()[0]
    checks.append(Check("PASS" if not empty_levels else "ERROR", "Craft options", f"Recipe levels without craft options: {empty_levels}."))

    empty_reqs = conn.execute("""
        SELECT COUNT(*) FROM craft_options co
        LEFT JOIN craft_requirements cr ON cr.craft_option_id=co.id
        WHERE cr.id IS NULL
    """).fetchone()[0]
    empty_breakdown = conn.execute("""
        SELECT r.recipe_type,COUNT(*)
        FROM craft_options co
        JOIN recipe_levels rl ON rl.id=co.recipe_level_id
        JOIN recipes r ON r.id=rl.recipe_id
        LEFT JOIN craft_requirements cr ON cr.craft_option_id=co.id
        WHERE cr.id IS NULL GROUP BY r.recipe_type ORDER BY r.recipe_type
    """).fetchall()
    empty_samples = conn.execute("""
        SELECT r.name,rl.level,co.option_index
        FROM craft_options co
        JOIN recipe_levels rl ON rl.id=co.recipe_level_id
        JOIN recipes r ON r.id=rl.recipe_id
        LEFT JOIN craft_requirements cr ON cr.craft_option_id=co.id
        WHERE cr.id IS NULL ORDER BY r.recipe_type,r.name,rl.level,co.option_index LIMIT 12
    """).fetchall()
    empty_detail = f"Craft options with no salvage requirements: {empty_reqs}."
    if empty_reqs:
        empty_detail += " By type: " + ", ".join(f"{t}={n}" for t,n in empty_breakdown) + "."
        empty_detail += " Sample: " + "; ".join(f"{n} L{lv} opt{o}" for n,lv,o in empty_samples) + "."
    checks.append(Check("PASS" if not empty_reqs else "ERROR", "Salvage requirements", empty_detail))

    null_costs = conn.execute("SELECT COUNT(*) FROM craft_options WHERE crafting_cost IS NULL").fetchone()[0]
    checks.append(Check("PASS" if not null_costs else "WARN", "Crafting costs", f"Craft options with no parsed crafting cost: {null_costs}."))

    bad_levels = conn.execute("SELECT COUNT(*) FROM recipe_levels WHERE level < 0 OR level > 50").fetchone()[0]
    checks.append(Check("PASS" if not bad_levels else "ERROR", "Recipe level bounds",
                        f"Levels outside 0-50: {bad_levels}. Level 0 is reserved for explicitly level-independent recipes."))

    dupes = conn.execute("SELECT canonical_key,recipe_type,COUNT(*) n FROM recipes GROUP BY canonical_key,recipe_type HAVING n>1").fetchall()
    dupe_groups: list[str] = []
    for key, recipe_type, _ in dupes:
        names = [x[0] for x in conn.execute(
            "SELECT name FROM recipes WHERE canonical_key=? AND recipe_type=? ORDER BY name", (key, recipe_type)
        )]
        dupe_groups.append(f"{recipe_type}: " + " | ".join(names))
    dupe_detail = f"Duplicate groups: {len(dupes)}."
    if dupe_groups:
        dupe_detail += " " + "; ".join(dupe_groups[:8])
    checks.append(Check("PASS" if not dupes else "ERROR", "Duplicate canonical recipes", dupe_detail))

    alias_missing = conn.execute("""
        SELECT COUNT(*) FROM recipes r
        LEFT JOIN recipe_aliases a ON a.recipe_id=r.id AND a.alias_key=r.canonical_key
        WHERE a.recipe_id IS NULL
    """).fetchone()[0]
    checks.append(Check("PASS" if not alias_missing else "ERROR", "Canonical recipe aliases", f"Recipes missing canonical OCR/search alias: {alias_missing}."))

    missing_revisions = conn.execute("""
        SELECT COUNT(*) FROM source_pages sp
        JOIN recipes r ON r.source_page_id=sp.id
        WHERE sp.revision_id IS NULL
    """).fetchone()[0]
    checks.append(Check("PASS" if not missing_revisions else "WARN", "Source revision provenance", f"Recipe source references without revision ID: {missing_revisions}."))

    unresolved_rows = conn.execute(
        "SELECT name,notes_json FROM recipes WHERE notes_json LIKE '%UNRESOLVED_SALVAGE:%' ORDER BY name"
    ).fetchall()
    unresolved_salvage = len(unresolved_rows)
    unresolved_detail = (
        f"Recipes with one or more ingredient links that did not resolve to the 108-item invention-salvage catalog: {unresolved_salvage}."
    )
    if unresolved_rows:
        snippets = []
        for name, notes_json in unresolved_rows[:12]:
            try:
                notes = json.loads(notes_json)
            except Exception:
                notes = [notes_json]
            unresolved = [x for x in notes if "UNRESOLVED_SALVAGE:" in x]
            snippets.append(f"{name} -> {', '.join(unresolved)}")
        unresolved_detail += " " + "; ".join(snippets)
    checks.append(Check(
        "ERROR" if unresolved_salvage else "PASS",
        "Unresolved salvage names",
        unresolved_detail,
    ))

    note_rows = conn.execute("SELECT name,notes_json FROM recipes WHERE notes_json != '[]' ORDER BY name").fetchall()
    actionable_notes: list[str] = []
    informational_notes = 0
    informational_prefixes = (
        "Applied wiki Recipe Note salvage-range override:",
        "Documented salvage ranges ",
        "Inline vanity-pet recipe parsed from source heading:",
    )
    for name, notes_json in note_rows:
        try:
            notes = json.loads(notes_json)
        except Exception:
            notes = [notes_json]
        for note in notes:
            if any(str(note).startswith(prefix) for prefix in informational_prefixes):
                informational_notes += 1
            else:
                actionable_notes.append(f"{name}: {note}")
    note_detail = (
        f"Recipes carrying notes={len(note_rows)}; informational notes={informational_notes}; "
        f"actionable parser notes={len(actionable_notes)}."
    )
    if actionable_notes:
        note_detail += " Sample: " + "; ".join(actionable_notes[:8])
    checks.append(Check(
        "WARN" if actionable_notes else "PASS",
        "Extraction notes",
        note_detail,
    ))

    # Spot checks grounded in known wiki pages. Missing records are warnings so the
    # report remains useful if a page is renamed, while wrong present data is an error.
    spotchecks = [
        ("Shield Wall: Defense/Endurance", 50, 0, 490400,
         {"Silver":1,"Spirit Thorn":1,"Gold":1,"Bleeding Stone":1,"Diamond":1}),
        ("Analyze Weakness: Defense Debuff", 50, 0, 490400,
         {"Ceramic Armor Plate":1,"Regenerating Flesh":1,"Pneumatic Piston":1,"Rikti Alloy":1}),
        # The wiki Recipe Note explicitly corrects this set's displayed boundary:
        # upper-band salvage begins at level 40, not 41.
        ("Exploit Weakness: Damage/Endurance", 40, 0, 104800,
         {"Ceramic Armor Plate":1,"Fortune":1,"Unquenchable Flame":1}),
        ("Invention: Damage", 50, 0, 464400,
         {"Demonic Threat Report":1,"Kinetic Weapon":1}),
        ("Costume Piece: Bat Wings", 1, 0, 5000,
         {"Demonic Threat Report":1,"Spirit Thorn":1,"Regenerating Flesh":1}),
        ("Pet: Hamidon Bud", 0, 0, 2500000, {"Hamidon Goo":20}),
    ]
    for name, level, option, cost, expected in spotchecks:
        got = _requirements(conn, name, level, option)
        if got is None:
            checks.append(Check("WARN", f"Spot check: {name}", "Recipe/level not found."))
        elif got == (cost, expected):
            checks.append(Check("PASS", f"Spot check: {name}", f"Level {level} cost and salvage match expected snapshot."))
        else:
            checks.append(Check("ERROR", f"Spot check: {name}", f"Expected {(cost, expected)}, got {got}."))

    jet_expected = [
        (2500, {"Simple Chemical":1,"Spiritual Essence":1,"Chemical Formula":1,"Unearthed Relic":1}),
        (10000, {"Inert Gas":1,"Alchemical Silver":1,"Scientific Law":1,"Ensorcelled Weapon":1}),
        (40000, {"Hydraulic Piston":1,"Nevermelting Ice":1,"Chaos Theorem":1,"Unquenchable Flame":1}),
    ]
    jet = [_requirements(conn, "Temporary Power: Jet Pack", 1, i) for i in range(3)]
    if all(x is not None for x in jet):
        checks.append(Check(
            "PASS" if jet == jet_expected else "ERROR",
            "Spot check: Temporary Power: Jet Pack",
            "Three alternative cost/salvage combinations match expected snapshot."
            if jet == jet_expected else f"Expected {jet_expected}, got {jet}.",
        ))
    else:
        checks.append(Check("WARN", "Spot check: Temporary Power: Jet Pack", "Three alternatives were not all found."))

    enhancement_only_raw = _meta(conn, "known_non_recipe_enhancement_page_count")
    if enhancement_only_raw is not None:
        enhancement_only = int(enhancement_only_raw)
        checks.append(Check(
            "PASS",
            "Enhancement-only families excluded",
            f"Known ATO/Winter/Event enhancement pages excluded from recipe crawling: {enhancement_only}.",
        ))

    reverse_failures_raw = _meta(conn, "salvage_reverse_failure_count")
    if reverse_failures_raw is not None:
        reverse_failures = int(reverse_failures_raw)
        checks.append(Check(
            "WARN" if reverse_failures else "PASS",
            "Salvage reverse-index crawl",
            f"Salvage pages that could not be used for the independent reverse-index cross-check: {reverse_failures}.",
        ))

    skipped = conn.execute("SELECT COUNT(*) FROM discovery_audit WHERE status='skipped_no_recipe_table'").fetchone()[0]
    missing_pages = conn.execute("SELECT COUNT(*) FROM discovery_audit WHERE status='skipped_missing_page'").fetchone()[0]
    # A page positively indexed as a set recipe should never be silently skipped.
    # Reverse-index-only pages without a colon are typically Empowerment/base
    # crafting uses rather than inventory recipes and are retained as audit info.
    critical_skips = conn.execute("""
        SELECT page_title,discovered_from FROM discovery_audit
        WHERE status='skipped_no_recipe_table'
          AND (discovered_from='Recipe Drop Pools'
               OR (discovered_from='Salvage Recipes Used In' AND page_title LIKE '%:%'))
        ORDER BY page_title
    """).fetchall()
    skip_detail = f"Pages without a recognized salvage-consuming recipe table: {skipped}; stale/missing page references: {missing_pages}."
    if critical_skips:
        skip_detail += " Unexpected positively-evidenced skips: " + "; ".join(f"{p} ({src})" for p,src in critical_skips[:12])
    checks.append(Check(
        "ERROR" if critical_skips else "PASS",
        "Discovery page dispositions",
        skip_detail,
    ))

    failed = conn.execute("SELECT COUNT(*) FROM discovery_audit WHERE status='error'").fetchone()[0]
    error_rows = conn.execute(
        "SELECT page_title,detail FROM discovery_audit WHERE status='error' ORDER BY page_title LIMIT 12"
    ).fetchall()
    error_detail = f"Pages with extraction errors: {failed}."
    if error_rows:
        error_detail += " " + "; ".join(f"{p}: {d}" for p,d in error_rows)
    checks.append(Check("ERROR" if failed else "PASS", "Extraction errors", error_detail))

    warnings = conn.execute("SELECT COUNT(*) FROM discovery_audit WHERE status='warning'").fetchone()[0]
    warning_rows = conn.execute(
        "SELECT page_title,detail FROM discovery_audit WHERE status='warning' ORDER BY page_title LIMIT 12"
    ).fetchall()
    info_count = conn.execute("SELECT COUNT(*) FROM discovery_audit WHERE status='info'").fetchone()[0]
    warning_detail = f"Actionable cross-check warnings: {warnings}; informational cross-source differences: {info_count}."
    if warning_rows:
        warning_detail += " " + "; ".join(f"{p or '[global]'}: {d}" for p,d in warning_rows)
    checks.append(Check("WARN" if warnings else "PASS", "Discovery cross-check health", warning_detail))

    conn.close()
    return checks


def write_validation_report(db_path: str | Path, txt_path: str | Path, json_path: str | Path | None = None) -> list[Check]:
    checks = validate_database(db_path)
    lines = ["Homecoming Recipe Database Validation Report", "=" * 43, ""]
    for c in checks:
        lines.append(f"{c.severity:<5} {c.name}: {c.detail}")
    errors = sum(c.severity == "ERROR" for c in checks)
    warnings = sum(c.severity == "WARN" for c in checks)
    passes = sum(c.severity == "PASS" for c in checks)
    lines.extend(["", f"Summary: {passes} PASS, {warnings} WARN, {errors} ERROR"])
    Path(txt_path).parent.mkdir(parents=True, exist_ok=True)
    Path(txt_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    if json_path:
        Path(json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(json_path).write_text(json.dumps([asdict(c) for c in checks], indent=2), encoding="utf-8")
    return checks
