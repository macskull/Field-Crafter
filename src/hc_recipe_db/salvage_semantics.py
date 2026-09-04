from __future__ import annotations

import sqlite3
import sys
from functools import lru_cache
from pathlib import Path

from .normalize import canonical_key


# FIELD_CRAFTER_INVENTION_SALVAGE_SEMANTICS_V1


def bundled_database_path() -> Path:
    """Return the recipe/salvage database shipped with the running app."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        root = Path(getattr(sys, "_MEIPASS"))
    else:
        root = Path(__file__).resolve().parents[2]
    return root / "data" / "homecoming_recipes.sqlite"


def internal_salvage_key(internal_name: str) -> str:
    """Normalize an internal salvage id the same way MemoryNameResolver does."""
    value = str(internal_name or "").strip()
    if value.startswith("S_"):
        value = value[2:]
    return canonical_key(value)


@lru_cache(maxsize=8)
def _invention_salvage_keys_for_path(path_text: str) -> frozenset[str]:
    path = Path(path_text)
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return frozenset(
            canonical_key(str(name))
            for (name,) in conn.execute("SELECT name FROM salvage")
            if str(name).strip()
        )
    finally:
        conn.close()


def invention_salvage_keys(db_path: str | Path | None = None) -> frozenset[str]:
    path = Path(db_path) if db_path is not None else bundled_database_path()
    return _invention_salvage_keys_for_path(str(path.resolve()))


def invention_salvage_membership(
    internal_name: str,
    *,
    db_path: str | Path | None = None,
) -> bool:
    """Return True only when the id maps to canonical invention salvage."""
    key = internal_salvage_key(internal_name)
    return bool(key and key in invention_salvage_keys(db_path))


@lru_cache(maxsize=4096)
def default_invention_salvage_membership(internal_name: str) -> bool | None:
    """Best-effort bundled-DB classifier for diagnostics/recovery.

    None means the bundled database could not be read. Callers must fail
    conservatively or fall back to their previous structural-only behavior; they
    must never treat a database failure as proof that an item is non-invention.
    """
    try:
        return invention_salvage_membership(internal_name)
    except Exception:
        return None
