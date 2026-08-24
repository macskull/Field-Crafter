from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


@dataclass(slots=True)
class DatabaseInfo:
    built_at_utc: str | None
    effective_date: str | None
    latest_source_revision: str | None
    recipe_count: int
    salvage_count: int
    source_count: int


@dataclass(slots=True)
class UpdateCandidate:
    candidate_db: Path
    work_dir: Path
    build_result: dict[str, Any]
    diff: dict[str, Any]


def _connect_ro(path: str | Path) -> sqlite3.Connection:
    p = Path(path).resolve()
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def database_info(path: str | Path) -> DatabaseInfo:
    conn = _connect_ro(path)
    try:
        meta = {str(r["key"]): str(r["value"]) for r in conn.execute("SELECT key,value FROM metadata")}
        latest = conn.execute("SELECT MAX(revision_timestamp) AS ts FROM source_pages").fetchone()["ts"]
        recipe_count = int(conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0])
        salvage_count = int(conn.execute("SELECT COUNT(*) FROM salvage").fetchone()[0])
        source_count = int(conn.execute("SELECT COUNT(*) FROM source_pages").fetchone()[0])
    finally:
        conn.close()
    built = meta.get("built_at_utc")
    effective = built[:10] if built and len(built) >= 10 else None
    return DatabaseInfo(
        built_at_utc=built,
        effective_date=effective,
        latest_source_revision=str(latest) if latest else None,
        recipe_count=recipe_count,
        salvage_count=salvage_count,
        source_count=source_count,
    )


def _recipe_signatures(path: str | Path) -> dict[tuple[str, str], str]:
    """Return stable hashes over every user-visible/crafting property of each recipe."""
    conn = _connect_ro(path)
    try:
        recipes = conn.execute(
            """SELECT id,name,recipe_type,recipe_rarity,set_name,min_level,max_level
               FROM recipes ORDER BY name COLLATE NOCASE, recipe_type"""
        ).fetchall()
        out: dict[tuple[str, str], str] = {}
        for r in recipes:
            levels = []
            for rl in conn.execute(
                "SELECT id,level FROM recipe_levels WHERE recipe_id=? ORDER BY level", (r["id"],)
            ).fetchall():
                opts = []
                for op in conn.execute(
                    "SELECT id,option_index,crafting_cost FROM craft_options WHERE recipe_level_id=? ORDER BY option_index",
                    (rl["id"],),
                ).fetchall():
                    reqs = [
                        (str(q["name"]), int(q["quantity"]))
                        for q in conn.execute(
                            """SELECT s.name,cr.quantity
                               FROM craft_requirements cr JOIN salvage s ON s.id=cr.salvage_id
                               WHERE cr.craft_option_id=? ORDER BY s.name COLLATE NOCASE""",
                            (op["id"],),
                        ).fetchall()
                    ]
                    opts.append((int(op["option_index"]), op["crafting_cost"], reqs))
                levels.append((int(rl["level"]), opts))
            payload = {
                "name": str(r["name"]),
                "type": str(r["recipe_type"]),
                "rarity": r["recipe_rarity"],
                "set": r["set_name"],
                "min": r["min_level"],
                "max": r["max_level"],
                "levels": levels,
            }
            blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            out[(str(r["name"]), str(r["recipe_type"]))] = hashlib.sha256(blob).hexdigest()
        return out
    finally:
        conn.close()


def _salvage_signatures(path: str | Path) -> dict[str, str]:
    conn = _connect_ro(path)
    try:
        out = {}
        for r in conn.execute(
            "SELECT name,rarity,level_tier,origin,wiki_title,wiki_url FROM salvage ORDER BY name COLLATE NOCASE"
        ).fetchall():
            payload = tuple(r[k] for k in ("name", "rarity", "level_tier", "origin", "wiki_title", "wiki_url"))
            out[str(r["name"])] = hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()
        return out
    finally:
        conn.close()


def compare_databases(current: str | Path, candidate: str | Path) -> dict[str, Any]:
    old_r = _recipe_signatures(current)
    new_r = _recipe_signatures(candidate)
    old_s = _salvage_signatures(current)
    new_s = _salvage_signatures(candidate)

    old_rk, new_rk = set(old_r), set(new_r)
    old_sk, new_sk = set(old_s), set(new_s)
    added_recipes = sorted(new_rk - old_rk, key=lambda x: (x[0].casefold(), x[1]))
    removed_recipes = sorted(old_rk - new_rk, key=lambda x: (x[0].casefold(), x[1]))
    changed_recipes = sorted((k for k in old_rk & new_rk if old_r[k] != new_r[k]), key=lambda x: (x[0].casefold(), x[1]))
    added_salvage = sorted(new_sk - old_sk, key=str.casefold)
    removed_salvage = sorted(old_sk - new_sk, key=str.casefold)
    changed_salvage = sorted((k for k in old_sk & new_sk if old_s[k] != new_s[k]), key=str.casefold)

    old_info = database_info(current)
    new_info = database_info(candidate)
    changed = bool(added_recipes or removed_recipes or changed_recipes or added_salvage or removed_salvage or changed_salvage)
    return {
        "changed": changed,
        "current": asdict(old_info),
        "candidate": asdict(new_info),
        "recipes": {
            "added": [{"name": n, "type": t} for n, t in added_recipes],
            "removed": [{"name": n, "type": t} for n, t in removed_recipes],
            "changed": [{"name": n, "type": t} for n, t in changed_recipes],
        },
        "salvage": {
            "added": added_salvage,
            "removed": removed_salvage,
            "changed": changed_salvage,
        },
    }


def _default_update_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "FieldCrafter" / "updates"
    return Path.home() / ".field_crafter" / "updates"


def build_update_candidate(
    current_db: str | Path,
    *,
    update_root: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> UpdateCandidate:
    """Build a fresh candidate DB. The active DB is never modified here."""
    from .builder import build_database

    progress = progress or (lambda _msg: None)
    current_db = Path(current_db)
    root = Path(update_root) if update_root else _default_update_root()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    work = root / stamp
    work.mkdir(parents=True, exist_ok=True)
    candidate = work / "homecoming_recipes_candidate.sqlite"
    # Keep a persistent Wiki cache across scans. The builder checks current
    # revision IDs in batches and reuses cached parse responses whose revisions
    # are unchanged, so normal future scans only re-download changed/new pages.
    shared_cache = root / "wiki_cache"
    shared_cache.mkdir(parents=True, exist_ok=True)
    result = build_database(
        db_path=candidate,
        cache_dir=shared_cache,
        export_dir=work / "exports",
        report_path=work / "validation_report.txt",
        report_json_path=work / "validation_report.json",
        refresh=True,
        delay_seconds=0.45,
        progress=progress,
        cancel_check=cancel_check,
    )
    errors = int((result.get("validation") or {}).get("error") or 0)
    if errors:
        raise RuntimeError(f"Candidate database failed validation with {errors} error(s). Current database was not changed. Report: {work / 'validation_report.txt'}")
    diff = compare_databases(current_db, candidate)
    (work / "update_diff.json").write_text(json.dumps(diff, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return UpdateCandidate(candidate_db=candidate, work_dir=work, build_result=result, diff=diff)


def accept_update(current_db: str | Path, candidate: UpdateCandidate) -> Path:
    current = Path(current_db)
    if not candidate.candidate_db.exists():
        raise FileNotFoundError(candidate.candidate_db)
    backup_dir = current.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / f"homecoming_recipes_{stamp}.sqlite"
    shutil.copy2(current, backup)
    replacement = current.with_suffix(current.suffix + ".new")
    shutil.copy2(candidate.candidate_db, replacement)
    os.replace(replacement, current)
    return backup


def reject_update(candidate: UpdateCandidate) -> None:
    try:
        shutil.rmtree(candidate.work_dir)
    except OSError:
        pass


def format_update_diff(diff: dict[str, Any], *, max_items: int = 80) -> str:
    if not diff.get("changed"):
        return "No recipe or salvage data changes were found."
    lines: list[str] = []
    r = diff.get("recipes") or {}
    s = diff.get("salvage") or {}
    lines.append(
        "Recipes: "
        f"+{len(r.get('added') or [])} added, "
        f"-{len(r.get('removed') or [])} removed, "
        f"{len(r.get('changed') or [])} changed"
    )
    lines.append(
        "Salvage: "
        f"+{len(s.get('added') or [])} added, "
        f"-{len(s.get('removed') or [])} removed, "
        f"{len(s.get('changed') or [])} changed"
    )
    detail: list[str] = []
    for label, items in (("ADDED RECIPE", r.get("added") or []), ("REMOVED RECIPE", r.get("removed") or []), ("CHANGED RECIPE", r.get("changed") or [])):
        for item in items:
            detail.append(f"{label}: {item['name']} [{item['type']}]")
    for label, items in (("ADDED SALVAGE", s.get("added") or []), ("REMOVED SALVAGE", s.get("removed") or []), ("CHANGED SALVAGE", s.get("changed") or [])):
        for item in items:
            detail.append(f"{label}: {item}")
    if detail:
        lines.append("")
        lines.extend(detail[:max_items])
        if len(detail) > max_items:
            lines.append(f"... {len(detail) - max_items} more change(s) not shown here.")
    return "\n".join(lines)
