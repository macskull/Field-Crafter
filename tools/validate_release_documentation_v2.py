#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

VALIDATION_VERSION = "2"
EXPECTED_VERSION = "1.16.1"


def find_root(start: Path) -> Path:
    start = start.resolve()
    for root in [start] + list(start.parents):
        if (root / "src" / "hc_recipe_db" / "version.py").is_file():
            return root
    raise RuntimeError("Could not find Field Crafter source root.")


def release_version(root: Path) -> str:
    text = (root / "src" / "hc_recipe_db" / "version.py").read_text(encoding="utf-8")
    match = re.search(r'(?m)^RELEASE_VERSION\s*=\s*"([^"]+)"\s*$', text)
    if not match:
        raise RuntimeError("Could not read RELEASE_VERSION.")
    return match.group(1)


def require(checks: list[dict], name: str, passed: bool, detail: str) -> None:
    checks.append({"name": name, "passed": bool(passed), "detail": detail})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args()

    try:
        root = find_root(Path(args.root))
        version = release_version(root)
        if version != EXPECTED_VERSION:
            raise RuntimeError(f"Expected RELEASE_VERSION {EXPECTED_VERSION}, found {version!r}.")

        paths = {
            "public_readme": root / "README.md",
            "python_readme": root / "README.txt",
            "data_readme": root / "data" / "README.txt",
            "release_notes": root / f"RELEASE_NOTES_{version}.txt",
            "github_release": root / f"GITHUB_RELEASE_{version}.md",
            "github_release_metadata": root / f"GITHUB_RELEASE_{version}.json",
            "app_update_config": root / "data" / "application_update_config.json",
            "spec": root / f"field_crafter_{version.replace('.', '_')}.spec",
            "build": root / "build_release.ps1",
            "salvage_test": root / "tools" / "test_invention_salvage_classification_v1.py",
            "updater_test": root / "tools" / "test_application_updates_v1.py",
        }
        for path in paths.values():
            if not path.is_file():
                raise RuntimeError(f"Missing required release file: {path}")

        content = {
            name: path.read_text(encoding="utf-8-sig")
            for name, path in paths.items()
            if path.suffix.lower() in {".md", ".txt", ".json", ".spec", ".ps1", ".py"}
        }
        checks: list[dict] = []

        public = content["public_readme"]
        require(
            checks,
            "readme_current_downloads",
            f"releases/download/v{version}/Field_Crafter_{version}.exe" in public
            and f"releases/download/v{version}/Field_Crafter_{version}_Python.zip" in public,
            "README.md must link to the prepared current-version release assets.",
        )
        require(
            checks,
            "readme_current_release_page",
            f"releases/tag/v{version}" in public
            and f"Field Crafter {version}" in public,
            "README.md must identify and link the current release.",
        )
        require(
            checks,
            "readme_offline_python",
            "64-bit Python 3.13" in public
            and "offline dependency wheelhouse" in public
            and "first launch does not require a PyPI connection" in public,
            "README.md must retain the offline prepared-Python behavior.",
        )
        require(
            checks,
            "readme_memory_updates",
            "## Game-Memory Definition Updates" in public
            and "**Check for memory updates**" in public,
            "README.md must document signed memory-definition updates.",
        )
        require(
            checks,
            "readme_application_updates",
            "## Application Updates" in public
            and "**Check for app updates**" in public
            and "signed" in public.lower(),
            "README.md must document the signed full-application updater.",
        )
        require(
            checks,
            "readme_diagnostics",
            "%LOCALAPPDATA%\\FieldCrafter\\diagnostics\\" in public,
            "README.md must retain the game-memory diagnostic location.",
        )

        python_readme = content["python_readme"]
        require(
            checks,
            "python_readme_current",
            f"Field Crafter {version} - Python Distribution" in python_readme
            and "offline dependency wheelhouse" in python_readme
            and "Check for memory updates" in python_readme
            and "Check for app updates" in python_readme,
            "Prepared Python README must describe the current release and both signed update channels.",
        )

        data_readme = content["data_readme"]
        for required_name in (
            "homecoming_recipes.sqlite",
            "memory_recipe_aliases.json",
            "memory_profiles.json",
            "memory_update_config.json",
            "application_update_config.json",
        ):
            require(
                checks,
                f"data_readme_{required_name}",
                required_name in data_readme,
                f"data/README.txt must document {required_name}.",
            )
        require(
            checks,
            "data_readme_current_version",
            f"Field Crafter {version}" in data_readme,
            "data/README.txt must identify the current release version.",
        )

        app_config = json.loads(content["app_update_config"])
        require(
            checks,
            "app_update_config_public_only",
            bool(app_config.get("manifest_url"))
            and bool(app_config.get("public_key_ed25519"))
            and "seed_base64" not in app_config
            and "private_key" not in app_config,
            "Application update config must contain only public verification material, never the private signing seed.",
        )

        notes = content["release_notes"]
        require(
            checks,
            "release_notes_current",
            notes.startswith(f"FIELD CRAFTER {version} RELEASE NOTES"),
            "Release notes must be explicitly versioned for the current release.",
        )
        require(
            checks,
            "release_notes_hotfix_topics",
            "INVENTION SALVAGE" in notes
            and "APPLICATION UPDATES" in notes
            and "S_*" in notes,
            "1.16.1 release notes must cover the salvage hotfix and full-application updater.",
        )
        note_bullets = [line.strip() for line in notes.splitlines() if line.lstrip().startswith("- ")]
        require(
            checks,
            "release_notes_change_bullets",
            bool(note_bullets)
            and all(line.startswith(("- Added ", "- Updated ", "- Fixed ")) for line in note_bullets),
            "Release-note bullets must describe explicit additions, updates, or fixes.",
        )

        release_meta = json.loads(content["github_release_metadata"])
        require(
            checks,
            "github_release_metadata_current",
            release_meta.get("tag_name") == f"v{version}"
            and release_meta.get("name") == f"Field Crafter {version} - Public Test Hotfix"
            and release_meta.get("prerelease") is True
            and release_meta.get("notes_file") == f"GITHUB_RELEASE_{version}.md",
            "GitHub release metadata must target the current public-test hotfix.",
        )
        require(
            checks,
            "github_release_metadata_assets",
            release_meta.get("assets") == [
                f"Field_Crafter_{version}.exe",
                f"Field_Crafter_{version}_Python.zip",
                "SHA256SUMS.txt",
                "RELEASE_MANIFEST.json",
            ],
            "GitHub release metadata must name the exact current-version artifacts.",
        )

        release_body = content["github_release"]
        require(
            checks,
            "github_release_current",
            release_body.startswith(f"# Field Crafter {version} - Public Test Hotfix")
            and f"Field_Crafter_{version}.exe" in release_body
            and f"Field_Crafter_{version}_Python.zip" in release_body,
            "Prepared GitHub release body must identify the current hotfix and assets.",
        )
        require(
            checks,
            "github_release_hotfix_topics",
            "invention salvage" in release_body.lower()
            and "application update" in release_body.lower(),
            "Prepared GitHub release body must explain both major 1.16.1 changes.",
        )

        spec = content["spec"]
        require(
            checks,
            "pyinstaller_spec_current",
            f'name="Field_Crafter_{version}"' in spec
            and '"application_update_config.json"' in spec
            and '"hc_recipe_db.application_updates"' in spec
            and '"hc_recipe_db.application_update_helper"' in spec,
            "The current PyInstaller spec must include the application updater runtime files/imports.",
        )

        build = content["build"]
        require(
            checks,
            "build_uses_documentation_v2",
            "tools\\validate_release_documentation_v2.py" in build,
            "build_release.ps1 must use the 1.16.1-aware documentation validator.",
        )
        require(
            checks,
            "build_runs_salvage_regression",
            "tools\\test_invention_salvage_classification_v1.py" in build,
            "Release packaging must run the invention-salvage regression suite.",
        )
        require(
            checks,
            "build_runs_updater_regression",
            "tools\\test_application_updates_v1.py" in build,
            "Release packaging must run the application-updater regression suite.",
        )
        require(
            checks,
            "build_keeps_app_update_config",
            '"application_update_config.json"' in build,
            "Prepared Python packaging must retain application_update_config.json.",
        )

        failed = [check for check in checks if not check["passed"]]
        result = {
            "passed": not failed,
            "validation_version": VALIDATION_VERSION,
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
