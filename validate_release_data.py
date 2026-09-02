from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from hc_recipe_db.game_memory import validate_memory_recipe_alias_coverage
from hc_recipe_db.memory_profiles import load_profile_pack
from hc_recipe_db.memory_profile_updates import load_update_config
from hc_recipe_db.validation import validate_database
from hc_recipe_db.version import RELEASE_VERSION

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DB = DATA / "homecoming_recipes.sqlite"
ALIASES = DATA / "memory_recipe_aliases.json"
MEMORY_PROFILES = DATA / "memory_profiles.json"
MEMORY_UPDATE_CONFIG = DATA / "memory_update_config.json"
SUMMARY = DATA / "release_data_summary.json"
INFO = DATA / "release_database_info.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_json(path: Path, problems: list[str]) -> dict:
    if not path.exists():
        problems.append(f"Missing release metadata file: {path.name}")
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        problems.append(f"Could not parse {path.name}: {exc}")
        return {}
    if not isinstance(value, dict):
        problems.append(f"{path.name} is not a JSON object")
        return {}
    return value


def main() -> int:
    problems: list[str] = []

    if not DB.exists():
        problems.append(f"Missing factory database: {DB}")
    if not ALIASES.exists():
        problems.append(f"Missing factory memory map: {ALIASES}")
    if not MEMORY_PROFILES.exists():
        problems.append(f"Missing bundled memory definitions: {MEMORY_PROFILES}")
    if not MEMORY_UPDATE_CONFIG.exists():
        problems.append(f"Missing memory update configuration: {MEMORY_UPDATE_CONFIG}")
    if not problems:
        try:
            pack_version, profiles = load_profile_pack(MEMORY_PROFILES, source="release_validation")
            if not pack_version or not profiles:
                problems.append("Bundled memory definition pack is empty.")
            load_update_config(MEMORY_UPDATE_CONFIG)
        except Exception as exc:
            problems.append(f"Memory definition/update validation failed: {exc}")
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}")
        return 1

    with sqlite3.connect(DB) as conn:
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        meta = dict(conn.execute("SELECT key, value FROM metadata"))
        actual_counts = {
            "recipes": int(conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]),
            "salvage": int(conn.execute("SELECT COUNT(*) FROM salvage").fetchone()[0]),
            "set_families": int(conn.execute("SELECT COUNT(DISTINCT set_name) FROM recipes WHERE set_name IS NOT NULL").fetchone()[0]),
        }
    if quick != "ok":
        problems.append(f"SQLite quick_check returned {quick!r}")

    checks = validate_database(DB)
    errors = [c for c in checks if str(getattr(c, "status", "")).upper() == "ERROR"]
    warnings = [c for c in checks if str(getattr(c, "status", "")).upper() == "WARN"]
    if errors:
        problems.append(f"Database validation reported {len(errors)} ERROR check(s)")

    coverage = validate_memory_recipe_alias_coverage(DB, ALIASES)
    if not coverage.get("complete"):
        missing_sets = coverage.get("missing_sets") or []
        invalid = coverage.get("invalid_aliases") or []
        sample = ", ".join(missing_sets[:12])
        if len(missing_sets) > 12:
            sample += f" (+{len(missing_sets) - 12} more)"
        problems.append(
            "Memory map incomplete: "
            f"sets {coverage.get('covered_set_count')}/{coverage.get('set_count')}, "
            f"members {coverage.get('covered_member_count')}/{coverage.get('member_count')}, "
            f"common IOs {coverage.get('covered_common_count')}/{coverage.get('common_count')}, "
            f"invalid aliases {len(invalid)}. Missing sets: {sample or 'none'}"
        )

    if meta.get("field_crafter_release_version") != RELEASE_VERSION:
        problems.append(
            f"Factory DB release metadata is {meta.get('field_crafter_release_version')!r}; expected {RELEASE_VERSION!r}."
        )
    if meta.get("field_crafter_release_validation") != "validated for redistribution":
        problems.append(
            "Factory DB is not stamped 'validated for redistribution'. Run prepare_release.py before packaging."
        )

    summary = _load_json(SUMMARY, problems)
    info = _load_json(INFO, problems)

    if summary.get("release_version") != RELEASE_VERSION:
        problems.append(f"release_data_summary.json release_version is not {RELEASE_VERSION}")
    if not summary.get("release_data_ready"):
        problems.append("release_data_summary.json does not mark release data ready")
    if not summary.get("redistribution_ready"):
        problems.append("release_data_summary.json does not mark this package redistribution ready")

    expected_hashes = summary.get("sha256") if isinstance(summary.get("sha256"), dict) else {}
    actual_db_hash = _sha256(DB)
    actual_alias_hash = _sha256(ALIASES)
    if expected_hashes.get("homecoming_recipes.sqlite") != actual_db_hash:
        problems.append("Factory database SHA-256 does not match release_data_summary.json")
    if expected_hashes.get("memory_recipe_aliases.json") != actual_alias_hash:
        problems.append("Memory-map SHA-256 does not match release_data_summary.json")

    summary_counts = summary.get("database_counts") if isinstance(summary.get("database_counts"), dict) else {}
    for key, actual in actual_counts.items():
        if summary_counts.get(key) is not None and int(summary_counts.get(key)) != actual:
            problems.append(f"Database count mismatch for {key}: metadata={summary_counts.get(key)} actual={actual}")

    if info.get("field_crafter_version") != RELEASE_VERSION:
        problems.append(f"release_database_info.json field_crafter_version is not {RELEASE_VERSION}")
    if info.get("validation_status") != "passed" or not info.get("redistribution_ready"):
        problems.append("release_database_info.json does not report a passed redistribution-ready validation")
    info_hashes = info.get("sha256") if isinstance(info.get("sha256"), dict) else {}
    if info_hashes and info_hashes != {"homecoming_recipes.sqlite": actual_db_hash, "memory_recipe_aliases.json": actual_alias_hash}:
        problems.append("release_database_info.json SHA-256 values do not match installed factory data")

    print(f"Field Crafter {RELEASE_VERSION} release-data validation")
    print("=" * 48)
    print(f"Database: {actual_counts['recipes']:,} recipes, {actual_counts['salvage']:,} salvage, {actual_counts['set_families']:,} set families")
    print(f"Database validation: {len(checks)} checks, {len(warnings)} warnings, {len(errors)} errors")
    print(
        "Memory map coverage: "
        f"{coverage.get('covered_set_count')}/{coverage.get('set_count')} sets, "
        f"{coverage.get('covered_member_count')}/{coverage.get('member_count')} set members, "
        f"{coverage.get('covered_common_count')}/{coverage.get('common_count')} common IOs"
    )
    print(f"Database SHA-256: {actual_db_hash}")
    print(f"Memory-map SHA-256: {actual_alias_hash}")

    if problems:
        print("\nRELEASE DATA NOT READY FOR REDISTRIBUTION")
        for problem in problems:
            print(f"- {problem}")
        return 1

    print(f"\nPASS: Field Crafter {RELEASE_VERSION} factory data is complete, validated, and redistribution ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
