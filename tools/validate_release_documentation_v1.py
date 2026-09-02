#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


# FIELD_CRAFTER_RELEASE_DOCUMENTATION_VALIDATION_V1


def find_root(start: Path) -> Path:
    start = start.resolve()
    for root in [start] + list(start.parents):
        if (root / "src" / "hc_recipe_db" / "version.py").is_file():
            return root
    raise RuntimeError("Could not find Field Crafter source root.")


def release_version(root: Path) -> str:
    text = (root / "src" / "hc_recipe_db" / "version.py").read_text(encoding="utf-8")
    m = re.search(r'(?m)^RELEASE_VERSION\s*=\s*"([^"]+)"\s*$', text)
    if not m:
        raise RuntimeError("Could not read RELEASE_VERSION.")
    return m.group(1)


def require(checks: list[dict], name: str, passed: bool, detail: str) -> None:
    checks.append({"name": name, "passed": bool(passed), "detail": detail})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args()

    try:
        root = find_root(Path(args.root))
        version = release_version(root)
        if version != "1.16":
            raise RuntimeError(f"Expected RELEASE_VERSION 1.16, found {version!r}.")

        paths = {
            "public_readme": root / "README.md",
            "python_readme": root / "README.txt",
            "data_readme": root / "data" / "README.txt",
            "release_notes": root / "RELEASE_NOTES_1.16.txt",
            "github_release": root / "GITHUB_RELEASE_1.16.md",
            "github_release_metadata": root / "GITHUB_RELEASE_1.16.json",
            "python_launcher": root / "Field Crafter.pyw",
            "python_setup": root / "setup_ocr.ps1",
            "powershell_launcher": root / "launch_gui.ps1",
        }
        for name, path in paths.items():
            if not path.is_file():
                raise RuntimeError(f"Missing release documentation file: {path}")

        content = {
            name: path.read_text(encoding="utf-8-sig")
            for name, path in paths.items()
        }
        checks: list[dict] = []

        all_docs = "\n".join(content[name] for name in ("public_readme", "python_readme", "data_readme", "release_notes", "github_release"))
        require(
            checks,
            "no_1_15_release_references",
            "1.15" not in all_docs and "/v1.15" not in all_docs,
            "Release-facing documentation must not retain 1.15 version links/text.",
        )

        public = content["public_readme"]
        require(
            checks,
            "readme_downloads_1_16",
            "releases/download/v1.16/Field_Crafter_1.16.exe" in public
            and "releases/download/v1.16/Field_Crafter_1.16_Python.zip" in public,
            "README.md must link to the prepared v1.16 release assets.",
        )
        require(
            checks,
            "readme_release_page_1_16",
            "releases/tag/v1.16" in public
            and "**Field Crafter 1.16 - Public Test Release**" in public,
            "README.md must point to and identify the v1.16 GitHub release.",
        )
        require(
            checks,
            "readme_python_offline_first_launch",
            "64-bit Python 3.13" in public
            and "offline dependency wheelhouse" in public
            and "first launch does not require a PyPI connection" in public,
            "README.md must describe the bundled offline Python wheelhouse.",
        )
        require(
            checks,
            "readme_memory_update_channel",
            "## Game-Memory Definition Updates" in public
            and "**Check for memory updates**" in public
            and "%LOCALAPPDATA%\\FieldCrafter\\diagnostics\\" in public,
            "README.md must document signed memory updates and diagnostics.",
        )
        require(
            checks,
            "readme_separates_database_and_memory_updates",
            "## Crafting Database Updates" in public
            and "Crafting-database maintenance is separate" in public,
            "README.md must distinguish crafting data updates from memory-definition updates.",
        )

        python_readme = content["python_readme"]
        require(
            checks,
            "python_readme_v1_16_and_offline",
            "Field Crafter 1.16 - Python Distribution" in python_readme
            and "64-bit Python 3.13" in python_readme
            and "offline dependency wheelhouse" in python_readme
            and "Check for memory updates" in python_readme,
            "Prepared Python README must describe the v1.16 offline and memory-update behavior.",
        )

        data = content["data_readme"]
        require(
            checks,
            "data_readme_memory_runtime_files",
            "memory_profiles.json" in data
            and "memory_update_config.json" in data
            and "Field Crafter 1.16" in data,
            "data/README.txt must describe all four hash-bound release-data files.",
        )

        python_launcher = content["python_launcher"]
        python_setup = content["python_setup"]
        powershell_launcher = content["powershell_launcher"]

        require(
            checks,
            "python_launcher_requires_3_13_and_offline_wheelhouse",
            "REQUIRED_PYTHON = (3, 13)" in python_launcher
            and "--no-index" in python_launcher
            and "--find-links" in python_launcher
            and 'root.title(f"Field Crafter {_display_version()} - First launch")' in python_launcher,
            "Field Crafter.pyw must enforce Python 3.13, use the offline wheelhouse, and show the release-data version.",
        )
        require(
            checks,
            "setup_ocr_is_offline_and_python_3_13",
            "py -3.13" in python_setup
            and "--no-index" in python_setup
            and "--find-links" in python_setup
            and "pip install --upgrade pip" not in python_setup,
            "setup_ocr.ps1 must use Python 3.13 and only the bundled wheelhouse.",
        )
        require(
            checks,
            "launch_gui_validates_private_runtime",
            "sys.version_info[:2] == (3, 13)" in powershell_launcher
            and "setup_ocr.ps1" in powershell_launcher,
            "launch_gui.ps1 must repair/validate the prepared Python 3.13 runtime before launch.",
        )

        notes = content["release_notes"]
        require(
            checks,
            "release_notes_v1_16",
            notes.startswith("FIELD CRAFTER 1.16 RELEASE NOTES"),
            "Release notes must be explicitly versioned 1.16.",
        )
        note_bullets = [
            line.strip()
            for line in notes.splitlines()
            if line.lstrip().startswith("- ")
        ]
        allowed_note_prefixes = ("- Added ", "- Updated ", "- Expanded ")
        require(
            checks,
            "release_notes_only_added_or_changed_items",
            bool(note_bullets)
            and all(line.startswith(allowed_note_prefixes) for line in note_bullets),
            "Every 1.16 release-note bullet must describe an addition/update/expansion.",
        )

        release_meta = json.loads(content["github_release_metadata"])
        require(
            checks,
            "github_release_metadata_current_version",
            release_meta.get("tag_name") == "v1.16"
            and release_meta.get("name") == "Field Crafter 1.16 - Public Test Release"
            and release_meta.get("prerelease") is True
            and release_meta.get("notes_file") == "GITHUB_RELEASE_1.16.md",
            "GitHub release metadata must target tag/title v1.16 and use the reviewed v1.16 notes.",
        )
        require(
            checks,
            "github_release_metadata_assets_current_version",
            release_meta.get("assets") == [
                "Field_Crafter_1.16.exe",
                "Field_Crafter_1.16_Python.zip",
                "SHA256SUMS.txt",
                "RELEASE_MANIFEST.json",
            ],
            "GitHub release metadata must name the v1.16 artifacts.",
        )

        release_body = content["github_release"]
        require(
            checks,
            "github_release_title_1_16",
            release_body.startswith("# Field Crafter 1.16 - Public Test Release"),
            "Prepared GitHub release body must identify version 1.16.",
        )
        require(
            checks,
            "github_release_assets_1_16",
            "Field_Crafter_1.16.exe" in release_body
            and "Field_Crafter_1.16_Python.zip" in release_body,
            "Prepared GitHub release body must name the v1.16 assets.",
        )

        # Only enforce positive wording in the changelog section, not in ordinary
        # install/verification instructions.
        whats_new = release_body.split("## What's new in 1.16", 1)
        if len(whats_new) == 2:
            changelog = whats_new[1].split("## Typical usage", 1)[0]
            change_bullets = [
                line.strip()
                for line in changelog.splitlines()
                if line.lstrip().startswith("- ")
            ]
        else:
            change_bullets = []
        require(
            checks,
            "github_release_changelog_only_added_or_changed",
            bool(change_bullets)
            and all(
                line.startswith(("- Added ", "- Updated ", "- Expanded "))
                for line in change_bullets
            ),
            "GitHub 'What's new' bullets must describe only additions/updates/expansions.",
        )

        failed = [c for c in checks if not c["passed"]]
        result = {
            "passed": not failed,
            "validation_version": "1",
            "release_version": version,
            "checks": checks,
        }

        print(json.dumps(result, indent=2))
        return 0 if not failed else 1

    except Exception as exc:
        print(f"Documentation validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
