"""Compatibility entry point for pre-1.15 maintainer workflows.

Field Crafter 1.15 uses prepare_release.py as the canonical release-data command.
"""
from prepare_release import main

if __name__ == "__main__":
    raise SystemExit(main())
