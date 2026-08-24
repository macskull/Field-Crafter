from __future__ import annotations

import html
import re
import unicodedata

_SPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^a-z0-9]+")


def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = value.replace("\u00a0", " ")
    value = value.replace("\u2019", "'").replace("\u2018", "'")
    value = value.replace("\u2013", "-").replace("\u2014", "-")
    return _SPACE.sub(" ", value).strip()


def canonical_key(value: str) -> str:
    value = unicodedata.normalize("NFKD", clean_text(value)).encode("ascii", "ignore").decode("ascii")
    value = value.lower()
    return _PUNCT.sub("", value)


def singularize_salvage_hint(value: str) -> str:
    """Conservative aliases for wiki display plurals seen in crafting tables."""
    v = clean_text(value)
    replacements = {
        "Masterwork Weapons": "Masterwork Weapon",
        "Demonic Threat Reports": "Demonic Threat Report",
        "Runes": "Rune",
    }
    return replacements.get(v, v)
