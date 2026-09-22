from __future__ import annotations

"""Share-safe redaction helpers for Field Crafter UI text and diagnostics.

Field Crafter diagnostics are frequently shared as screenshots or ZIPs. Redact
machine-local user-profile prefixes while retaining useful path structure.
This mirrors the privacy hardening used by Auto-leveler.
"""

import os
import re
from pathlib import Path
from typing import Any


def _variants(value: str) -> set[str]:
    text = str(value or "").strip()
    if not text:
        return set()
    vals = {text, text.rstrip("\\/"), text.replace("\\", "/"), text.replace("/", "\\")}
    return {item for item in vals if item}


def _known_prefixes() -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    env_tokens = (
        ("LOCALAPPDATA", "%LOCALAPPDATA%"),
        ("APPDATA", "%APPDATA%"),
        ("TEMP", "%TEMP%"),
        ("TMP", "%TEMP%"),
        ("USERPROFILE", "%USERPROFILE%"),
        ("HOME", "%USERPROFILE%"),
    )
    for env_name, token in env_tokens:
        value = os.environ.get(env_name, "")
        for variant in _variants(value):
            pairs.append((variant, token))
    try:
        for variant in _variants(str(Path.home())):
            pairs.append((variant, "%USERPROFILE%"))
    except Exception:
        pass

    dedup: dict[str, tuple[str, str]] = {}
    for root, token in sorted(pairs, key=lambda item: len(item[0]), reverse=True):
        dedup.setdefault(root.casefold(), (root, token))
    return list(dedup.values())


def sanitize_user_paths(value: Any) -> str:
    """Redact local profile roots and Windows profile-folder usernames."""
    text = str(value if value is not None else "")
    for root, token in _known_prefixes():
        text = re.sub(re.escape(root), token, text, flags=re.IGNORECASE)

    # Defense in depth for persisted tracebacks or text copied from another
    # Windows machine. Keep the drive/Users path shape, redact only the username.
    text = re.sub(
        r"(?i)\b([A-Z]:[\\/]+Users[\\/]+)([^\\/\r\n\"']+)",
        r"\1<USER>",
        text,
    )
    return text


def sanitize_structure(value: Any) -> Any:
    """Recursively sanitize strings in JSON-like diagnostic structures."""
    if isinstance(value, str):
        return sanitize_user_paths(value)
    if isinstance(value, dict):
        return {key: sanitize_structure(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_structure(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_structure(item) for item in value)
    return value
