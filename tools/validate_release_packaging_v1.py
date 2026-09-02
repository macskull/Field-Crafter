#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


# FIELD_CRAFTER_RELEASE_PACKAGING_PREFLIGHT_V1
# FIELD_CRAFTER_RELEASE_PACKAGING_PREFLIGHT_V1_1
# FIELD_CRAFTER_RELEASE_PACKAGING_PREFLIGHT_V1_2
# FIELD_CRAFTER_RELEASE_PACKAGING_PREFLIGHT_V1_3
# FIELD_CRAFTER_RELEASE_PACKAGING_PREFLIGHT_V1_4


def find_root(start: Path) -> Path:
    start = start.resolve()
    for root in [start] + list(start.parents):
        if (root / "src" / "hc_recipe_db" / "version.py").is_file():
            return root
    raise RuntimeError("Could not find Field Crafter source root.")


def diagnostics_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) / "FieldCrafter" if base else Path.home() / ".field_crafter"
    out = root / "diagnostics"
    out.mkdir(parents=True, exist_ok=True)
    return out


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def require(checks: dict, name: str, condition: bool, detail: str) -> None:
    checks[name] = {"passed": bool(condition), "detail": detail}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args()

    pid = None
    try:
        root = find_root(Path(args.root))
        version_text = read(root / "src" / "hc_recipe_db" / "version.py")
        match = re.search(
            r'(?m)^RELEASE_VERSION\s*=\s*"([^"]+)"\s*$',
            version_text,
        )
        if not match:
            raise RuntimeError("Could not read RELEASE_VERSION.")
        version = match.group(1)
        spec_name = f"field_crafter_{version.replace('.', '_')}.spec"

        paths = {
            "spec": root / spec_name,
            "build": root / "build_release.ps1",
            "launcher": root / "Field Crafter.pyw",
            "prepare": root / "prepare_release.py",
            "validate": root / "validate_release_data.py",
            "make_zip": root / "make_release_zip.py",
            "verify": root / "verify_release_artifacts.ps1",
            "build_requirements": root / "requirements-build.txt",
            "release_notes": root / f"RELEASE_NOTES_{version}.txt",
            "structural_recovery": (
                root / "src" / "hc_recipe_db" / "memory_structural_recovery.py"
            ),
            "release_data_runner": (
                root / "tools" / "run_release_data_preparation_v1.py"
            ),
        }

        missing = [name for name, path in paths.items() if not path.is_file()]
        if missing:
            raise RuntimeError(
                "Missing packaging file(s): " + ", ".join(missing)
            )

        content = {name: read(path) for name, path in paths.items()}
        checks: dict = {}

        require(
            checks,
            "release_version_is_1_16",
            version == "1.16",
            f"RELEASE_VERSION={version}",
        )
        require(
            checks,
            "spec_bundles_memory_profile_pack",
            '"memory_profiles.json"' in content["spec"],
            spec_name,
        )
        require(
            checks,
            "spec_bundles_memory_update_config",
            '"memory_update_config.json"' in content["spec"],
            spec_name,
        )
        require(
            checks,
            "spec_names_1_16_exe",
            f'name="Field_Crafter_{version}"' in content["spec"],
            spec_name,
        )
        require(
            checks,
            "spec_explicit_structural_recovery_hiddenimport",
            '"hc_recipe_db.memory_structural_recovery"'
            in content["spec"],
            spec_name,
        )
        require(
            checks,
            "build_derives_central_version",
            "RELEASE_VERSION" in content["build"]
            and '$Version = "1.15"' not in content["build"],
            "build_release.ps1",
        )
        require(
            checks,
            "build_repairs_broken_release_venv",
            "function Test-ReleasePip" in content["build"]
            and "Existing .release_venv has missing/broken pip" in content["build"]
            and "rebuilding the release environment once" in content["build"],
            "build_release.ps1",
        )
        require(
            checks,
            "build_uses_versioned_spec",
            "$SpecPath" in content["build"]
            and "field_crafter_1_15.spec" not in content["build"],
            "build_release.ps1",
        )
        require(
            checks,
            "python_stage_keeps_memory_profiles",
            '"memory_profiles.json"' in content["build"],
            "build_release.ps1",
        )
        require(
            checks,
            "python_stage_keeps_memory_update_config",
            '"memory_update_config.json"' in content["build"],
            "build_release.ps1",
        )
        require(
            checks,
            "python_build_creates_offline_wheelhouse",
            "pip wheel --wheel-dir" in content["build"]
            and "wheelhouse" in content["build"].lower(),
            "build_release.ps1",
        )
        require(
            checks,
            "python_stage_creates_release_marker",
            '".field_crafter_release"' in content["build"]
            and "$PythonStage" in content["build"]
            and "ReleaseMarker" in content["build"],
            "build_release.ps1",
        )
        require(
            checks,
            "runtime_version_distinguishes_release_from_dev",
            "DEV_VERSION" in version_text
            and 'RELEASE_VERSION = "1.16"' in version_text
            and (
                'getattr(sys, "frozen", False)' in version_text
                or "sys.frozen" in version_text
            )
            and ".field_crafter_release" in version_text
            and "_is_release_runtime" in version_text,
            "src/hc_recipe_db/version.py",
        )
        require(
            checks,
            "runtime_app_version_uses_release_context",
            "APP_VERSION = RELEASE_VERSION if _is_release_runtime() else DEV_VERSION"
            in version_text,
            "src/hc_recipe_db/version.py",
        )
        require(
            checks,
            "launcher_uses_no_index",
            '"--no-index"' in content["launcher"],
            "Field Crafter.pyw",
        )
        require(
            checks,
            "launcher_uses_bundled_wheelhouse",
            '"--find-links"' in content["launcher"]
            and "WHEELHOUSE" in content["launcher"],
            "Field Crafter.pyw",
        )
        require(
            checks,
            "launcher_does_not_upgrade_pip_online",
            '"--upgrade", "pip"' not in content["launcher"],
            "Field Crafter.pyw",
        )

        four_hashes = all(
            f'"{name}"' in content["prepare"]
            and f'"{name}"' in content["validate"]
            for name in (
                "homecoming_recipes.sqlite",
                "memory_recipe_aliases.json",
                "memory_profiles.json",
                "memory_update_config.json",
            )
        )
        require(
            checks,
            "release_metadata_hash_binds_four_runtime_data_files",
            four_hashes,
            "prepare_release.py + validate_release_data.py",
        )
        require(
            checks,
            "prepare_release_checks_windows_replaceability_before_refresh",
            "def _windows_assert_replaceable(" in content["prepare"]
            and "Factory recipe database" in content["prepare"],
            "prepare_release.py",
        )
        require(
            checks,
            "prepare_release_requires_sustained_windows_lock_before_abort",
            "attempts: int = 40" in content["prepare"]
            and "delay_seconds: float = 0.25" in content["prepare"]
            and "remained non-replaceable" in content["prepare"],
            "prepare_release.py",
        )
        require(
            checks,
            "release_data_runner_explicitly_closes_sqlite_count_connection",
            "finally:" in content["release_data_runner"]
            and "conn.close()" in content["release_data_runner"],
            "tools/run_release_data_preparation_v1.py",
        )
        require(
            checks,
            "prepare_release_retries_transient_windows_replace_locks",
            "def _replace_with_retry(" in content["prepare"]
            and "attempts: int = 12" in content["prepare"],
            "prepare_release.py",
        )
        require(
            checks,
            "prepare_release_restores_only_replaced_files",
            "db_replaced = False" in content["prepare"]
            and "aliases_replaced = False" in content["prepare"]
            and "if db_replaced:" in content["prepare"]
            and "if aliases_replaced:" in content["prepare"],
            "prepare_release.py",
        )
        require(
            checks,
            "release_data_runner_streams_live_progress",
            "def run_streaming(" in content["release_data_runner"]
            and "prepare_step = run_streaming(" in content["release_data_runner"],
            "tools/run_release_data_preparation_v1.py",
        )
        require(
            checks,
            "database_validation_uses_severity_field",
            'getattr(c, "severity", "")' in content["validate"]
            and 'getattr(c, "status", "")' not in content["validate"],
            "validate_release_data.py",
        )
        require(
            checks,
            "zip_builder_requires_memory_runtime",
            "memory_structural_recovery.py" in content["make_zip"]
            and "memory_profiles.json" in content["make_zip"]
            and "memory_update_config.json" in content["make_zip"],
            "make_release_zip.py",
        )
        require(
            checks,
            "zip_builder_requires_offline_wheels",
            "wheelhouse/" in content["make_zip"]
            and ".whl" in content["make_zip"],
            "make_release_zip.py",
        )
        require(
            checks,
            "artifact_verifier_derives_version",
            "RELEASE_VERSION" in content["verify"]
            and '$Version = "1.15"' not in content["verify"],
            "verify_release_artifacts.ps1",
        )
        require(
            checks,
            "artifact_verifier_checks_memory_runtime",
            "memory_structural_recovery.py" in content["verify"]
            and "memory_profiles.json" in content["verify"]
            and "memory_update_config.json" in content["verify"],
            "verify_release_artifacts.ps1",
        )
        require(
            checks,
            "artifact_verifier_checks_offline_wheels",
            "wheelhouse/*.whl" in content["verify"],
            "verify_release_artifacts.ps1",
        )
        require(
            checks,
            "build_requirements_include_pyinstaller",
            bool(
                re.search(
                    r"(?mi)^\s*pyinstaller\s*[<>=]",
                    content["build_requirements"],
                )
            ),
            "requirements-build.txt",
        )
        require(
            checks,
            "release_notes_are_versioned_1_16",
            "FIELD CRAFTER 1.16 RELEASE NOTES" in content["release_notes"],
            paths["release_notes"].name,
        )
        require(
            checks,
            "v7_1_structural_recovery_present",
            "FIELD_CRAFTER_MEMORY_STRUCTURAL_RECOVERY_V7_1"
            in content["structural_recovery"],
            "memory_structural_recovery.py",
        )

        passed = all(item["passed"] for item in checks.values())
        result = {
            "passed": passed,
            "preflight_version": "1.4",
            "release_version": version,
            "checks": checks,
        }

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output = (
            diagnostics_dir()
            / f"field_crafter_release_packaging_preflight_{stamp}.json"
        )
        output.write_text(
            json.dumps(result, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            f"{'PASS' if passed else 'FAIL'}: packaging preflight written to "
            f"{output}"
        )
        return 0 if passed else 1

    except Exception as exc:
        result = {
            "passed": False,
            "preflight_version": "1.4",
            "error": str(exc),
        }
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output = (
            diagnostics_dir()
            / f"field_crafter_release_packaging_preflight_{stamp}.json"
        )
        output.write_text(
            json.dumps(result, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"FAIL: packaging preflight written to {output}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
