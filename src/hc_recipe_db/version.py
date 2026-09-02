"""Field Crafter application/release version identifiers."""

from __future__ import annotations

import sys
from pathlib import Path


DEV_VERSION = "1.16-dev"
RELEASE_VERSION = "1.16"

_RELEASE_MARKER = ".field_crafter_release"


def _is_release_runtime() -> bool:
    # PyInstaller release build.
    if getattr(sys, "frozen", False):
        return True

    # Prepared Python distribution.
    try:
        root = Path(__file__).resolve().parents[2]
        if (root / _RELEASE_MARKER).is_file():
            return True
    except (OSError, IndexError):
        pass

    return False


APP_VERSION = RELEASE_VERSION if _is_release_runtime() else DEV_VERSION
