#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hc_recipe_db import memory_diagnostics as md
from hc_recipe_db.privacy import sanitize_structure, sanitize_user_paths


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    original = dict(os.environ)
    try:
        os.environ["USERPROFILE"] = r"C:\\Users\\Alice Example"
        os.environ["LOCALAPPDATA"] = r"C:\\Users\\Alice Example\\AppData\\Local"
        os.environ["APPDATA"] = r"C:\\Users\\Alice Example\\AppData\\Roaming"
        os.environ["TEMP"] = r"C:\\Users\\Alice Example\\AppData\\Local\\Temp"
        os.environ["TMP"] = os.environ["TEMP"]

        sample = r"failed at C:\Users\Alice Example\AppData\Local\FieldCrafter\diagnostics\x.zip"
        clean = sanitize_user_paths(sample)
        check("Alice Example" not in clean, "current profile username leaked")
        check(
            "%LOCALAPPDATA%" in clean
            or "%USERPROFILE%" in clean
            or r"C:\Users\<USER>" in clean,
            f"path structure was not preserved: {clean!r}",
        )

        copied = r"trace C:\Users\Someone Else\Desktop\field_crafter.py"
        copied_clean = sanitize_user_paths(copied)
        check("Someone Else" not in copied_clean, "foreign Windows profile username leaked")
        check(r"C:\Users\<USER>\Desktop" in copied_clean, "foreign profile placeholder missing")

        structured = sanitize_structure({"error": copied, "nested": [sample]})
        check("Someone Else" not in json.dumps(structured), "recursive structure sanitizer leaked username")
        check("Alice Example" not in json.dumps(structured), "recursive structure sanitizer leaked current username")

        old_collect = md.collect_memory_diagnostic
        try:
            md.collect_memory_diagnostic = lambda process, failure_detail=None: {
                "schema_version": md.DIAGNOSTIC_SCHEMA_VERSION,
                "failure_detail": failure_detail or copied,
                "profile_manager_warnings": [sample],
                "character_name": "Farmy Boi A",
            }
            with tempfile.TemporaryDirectory() as td:
                out = md.create_memory_diagnostic_zip(1234, failure_detail=copied, output_dir=td)
                with zipfile.ZipFile(out, "r") as archive:
                    jtext = archive.read("memory_diagnostic.json").decode("utf-8")
                    atext = archive.read("memory_diagnostic_analysis.txt").decode("utf-8")
                combined = jtext + "\n" + atext
                check("Someone Else" not in combined, "diagnostic ZIP leaked foreign profile username")
                check("Alice Example" not in combined, "diagnostic ZIP leaked current profile username")
                # Character identity is operational game context, not a Windows user-profile username.
                check("Farmy Boi A" in jtext, "character name should not be removed by path privacy sanitizer")
        finally:
            md.collect_memory_diagnostic = old_collect
    finally:
        os.environ.clear()
        os.environ.update(original)

    print("PASS: Field Crafter UI/diagnostic user-profile path redaction")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
