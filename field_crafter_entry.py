from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path

from hc_recipe_db.gui import launch_gui


def _bundle_root() -> Path:
    # PyInstaller sets __file__ to a path inside the extracted one-file bundle.
    # _MEIPASS remains a safe fallback for older/runtime edge cases.
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent


def _user_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    out = Path(base) / "FieldCrafter" if base else Path.home() / ".field_crafter"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _database_meta(path: Path, key: str) -> str | None:
    try:
        conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        finally:
            conn.close()
        return str(row[0]) if row and row[0] else None
    except Exception:
        return None


def _database_built_at(path: Path) -> str | None:
    return _database_meta(path, "built_at_utc")


def _release_version_tuple(value: str | None) -> tuple[int, ...]:
    if not value:
        return ()
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return ()


def _install_bundled_database(bundled: Path, active: Path) -> None:
    temp = active.with_suffix(".sqlite.new")
    shutil.copy2(bundled, temp)
    os.replace(temp, active)


def _active_database() -> Path:
    bundled = _bundle_root() / "data" / "homecoming_recipes.sqlite"
    if not bundled.exists():
        raise FileNotFoundError(f"Bundled recipe database was not found: {bundled}")
    active = _user_data_dir() / "homecoming_recipes.sqlite"
    if not active.exists():
        _install_bundled_database(bundled, active)
        return active

    # A new Field Crafter release can ship a fresher validated factory database. If
    # it is newer than the user's active copy, install it automatically so upgrading
    # the app never requires an immediate manual database refresh. A user-installed
    # database with an equal or newer build timestamp is preserved.
    bundled_built = _database_built_at(bundled)
    active_built = _database_built_at(active)
    bundled_release = _release_version_tuple(_database_meta(bundled, "field_crafter_release_version"))
    active_release = _release_version_tuple(_database_meta(active, "field_crafter_release_version"))

    should_upgrade = False
    if bundled_built and active_built:
        if bundled_built > active_built:
            should_upgrade = True
        elif bundled_built == active_built and bundled_release > active_release:
            # Allows a release to ship canonicalization/mapping fixes against the
            # same underlying Wiki snapshot without pretending the Wiki build date changed.
            should_upgrade = True
    elif bundled_release > active_release:
        should_upgrade = True

    if should_upgrade:
        backup_dir = active.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        safe_stamp = (active_built or "unknown").replace(":", "").replace("+", "_")
        backup = backup_dir / f"homecoming_recipes_pre_release_upgrade_{safe_stamp}.sqlite"
        if not backup.exists():
            shutil.copy2(active, backup)
        _install_bundled_database(bundled, active)
    return active


if __name__ == "__main__":
    launch_gui(db_path=_active_database())
