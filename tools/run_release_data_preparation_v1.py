#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# FIELD_CRAFTER_RELEASE_DATA_PREPARATION_V1
# FIELD_CRAFTER_RELEASE_DATA_PREPARATION_V1_1
# FIELD_CRAFTER_RELEASE_DATA_PREPARATION_V1_2
# FIELD_CRAFTER_RELEASE_DATA_PREPARATION_V1_3

MUTABLE_RELEASE_FILES = (
    "data/homecoming_recipes.sqlite",
    "data/memory_recipe_aliases.json",
    "data/validation_report.txt",
    "data/validation_report.json",
    "data/release_data_summary.json",
    "data/release_database_info.json",
    "data/last_release_prepare_failure.txt",
)

IMMUTABLE_MEMORY_FILES = (
    "data/memory_profiles.json",
    "data/memory_update_config.json",
)

REQUIREMENT_FILES = (
    "requirements.txt",
    "requirements-ocr.txt",
    "requirements-build.txt",
)


def find_root(start: Path) -> Path:
    start = start.resolve()
    for root in [start] + list(start.parents):
        if (
            (root / "src" / "hc_recipe_db" / "version.py").is_file()
            and (root / "prepare_release.py").is_file()
            and (root / "validate_release_data.py").is_file()
        ):
            return root
    raise RuntimeError(
        "Could not find the Field Crafter 1.16 source root."
    )


def diagnostic_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) / "FieldCrafter" if base else Path.home() / ".field_crafter"
    out = root / "diagnostics"
    out.mkdir(parents=True, exist_ok=True)
    return out


def sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def release_version(root: Path) -> str:
    text = (root / "src" / "hc_recipe_db" / "version.py").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r'(?m)^RELEASE_VERSION\s*=\s*"([^"]+)"\s*$',
        text,
    )
    if not match:
        raise RuntimeError("Could not read RELEASE_VERSION.")
    return match.group(1)


def run(
    name: str,
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )
    return {
        "name": name,
        "passed": completed.returncode == 0,
        "returncode": completed.returncode,
        "command": command,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }



def run_streaming(
    name: str,
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Stream long-running child output live while retaining it for JSON."""
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    captured: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        captured.append(line)
        print(line, end="", flush=True)

    returncode = process.wait()
    output = "".join(captured)
    return {
        "name": name,
        "passed": returncode == 0,
        "returncode": returncode,
        "command": command,
        "stdout": output,
        "stderr": "",
    }


def fail_if(step: dict[str, Any]) -> None:
    if not step.get("passed"):
        detail = (
            step.get("stderr")
            or step.get("stdout")
            or "no output"
        )
        raise RuntimeError(
            f"{step.get('name')} failed with exit code "
            f"{step.get('returncode')}: {detail[-4000:]}"
        )


def venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def pip_is_healthy(python_path: Path, *, cwd: Path) -> tuple[bool, str]:
    if not python_path.is_file():
        return False, "release Python executable is missing"
    try:
        completed = subprocess.run(
            [str(python_path), "-m", "pip", "--version"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:
        return False, str(exc)
    detail = (completed.stdout or completed.stderr or "").strip()
    return completed.returncode == 0, detail


def recreate_release_venv(
    root: Path,
    release_venv: Path,
    *,
    steps: list[dict[str, Any]],
) -> Path:
    if release_venv.exists():
        shutil.rmtree(release_venv)

    create_step = run(
        "Create clean .release_venv",
        [
            sys.executable,
            "-m",
            "venv",
            str(release_venv),
        ],
        cwd=root,
    )
    steps.append(create_step)
    fail_if(create_step)

    python_path = venv_python(release_venv)
    healthy, detail = pip_is_healthy(
        python_path,
        cwd=root,
    )
    if not healthy:
        raise RuntimeError(
            "Fresh .release_venv still has an unusable pip installation: "
            + detail
        )
    return python_path


def cleanup_stale_rollback_files(root: Path) -> list[str]:
    removed: list[str] = []
    for rel in MUTABLE_RELEASE_FILES:
        target = root / rel
        temp = target.with_name(target.name + ".rollback")
        if not temp.exists():
            continue

        # A stale rollback file generated by this tool is safe to remove only
        # when the live target exists and the bytes are identical. Otherwise,
        # retain it for manual inspection rather than guessing.
        if target.is_file() and sha256(target) == sha256(temp):
            temp.unlink()
            removed.append(str(temp))
            continue

        raise RuntimeError(
            "A stale rollback file differs from the live release file and "
            "requires manual inspection before continuing: "
            f"{temp}"
        )
    return removed


def snapshot_files(root: Path) -> dict[str, str | None]:
    return {
        rel: sha256(root / rel)
        for rel in (
            *MUTABLE_RELEASE_FILES,
            *IMMUTABLE_MEMORY_FILES,
        )
    }


def backup_release_state(root: Path, backup: Path) -> dict[str, bool]:
    manifest: dict[str, bool] = {}
    for rel in MUTABLE_RELEASE_FILES:
        source = root / rel
        manifest[rel] = source.exists()
        if source.exists():
            dest = backup / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)

    exports = root / "exports"
    manifest["exports/"] = exports.exists()
    if exports.exists():
        shutil.copytree(exports, backup / "exports")

    return manifest


def restore_release_state(
    root: Path,
    backup: Path,
    manifest: dict[str, bool],
) -> None:
    for rel in MUTABLE_RELEASE_FILES:
        target = root / rel
        if manifest.get(rel):
            source = backup / rel
            target.parent.mkdir(parents=True, exist_ok=True)

            # Avoid touching an unchanged file at all. This matters on Windows,
            # where the SQLite file may legitimately be open by another process.
            if target.is_file() and sha256(target) == sha256(source):
                continue

            temp = target.with_name(target.name + ".rollback")
            try:
                shutil.copy2(source, temp)
                os.replace(temp, target)
            finally:
                if temp.exists():
                    try:
                        temp.unlink()
                    except Exception:
                        pass
        else:
            target.unlink(missing_ok=True)

    exports = root / "exports"
    if exports.exists():
        shutil.rmtree(exports)
    if manifest.get("exports/"):
        shutil.copytree(backup / "exports", exports)


def db_counts(path: Path) -> dict[str, int] | None:
    if not path.is_file():
        return None
    conn = sqlite3.connect(path)
    try:
        return {
            "recipes": int(
                conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
            ),
            "salvage": int(
                conn.execute("SELECT COUNT(*) FROM salvage").fetchone()[0]
            ),
            "set_families": int(
                conn.execute(
                    "SELECT COUNT(DISTINCT set_name) "
                    "FROM recipes WHERE set_name IS NOT NULL"
                ).fetchone()[0]
            ),
        }
    finally:
        conn.close()


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def tail(text: str, lines: int = 120) -> str:
    values = text.splitlines()
    return "\n".join(values[-lines:])


def git_status(root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=20,
        )
        if completed.returncode == 0:
            return completed.stdout.strip()
    except Exception:
        pass
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the real Field Crafter 1.16 live release-data refresh, "
            "complete memory-map rebuild, and independent redistribution "
            "validation with rollback on failure."
        )
    )
    parser.add_argument("--root", default=".")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d_%H%M%S")
    result_path = (
        diagnostic_dir()
        / f"field_crafter_release_data_preparation_v1_3_{stamp}.json"
    )

    steps: list[dict[str, Any]] = []
    root: Path | None = None
    backup_root: Path | None = None
    backup_manifest: dict[str, bool] | None = None
    before: dict[str, str | None] = {}
    restored = False

    result: dict[str, Any] = {
        "passed": False,
        "test_version": "1.3",
        "generated_at_utc": now.isoformat(),
        "steps": [],
    }

    try:
        root = find_root(Path(args.root))
        version = release_version(root)
        if version != "1.16":
            raise RuntimeError(
                f"Expected RELEASE_VERSION 1.16, found {version!r}."
            )

        result["release_version"] = version
        result["source_root"] = str(root)
        result["git_status_before"] = git_status(root)

        for rel in (
            "prepare_release.py",
            "validate_release_data.py",
            "tools/validate_release_packaging_v1.py",
            *REQUIREMENT_FILES,
            *IMMUTABLE_MEMORY_FILES,
        ):
            if not (root / rel).is_file():
                raise RuntimeError(f"Required release file is missing: {rel}")

        # Static packaging gate first.
        static_step = run(
            "Static release packaging preflight",
            [
                sys.executable,
                str(root / "tools" / "validate_release_packaging_v1.py"),
                "--root",
                str(root),
            ],
            cwd=root,
        )
        steps.append(static_step)
        fail_if(static_step)

        before = snapshot_files(root)
        result["hashes_before"] = before
        result["database_counts_before"] = db_counts(
            root / "data" / "homecoming_recipes.sqlite"
        )

        # Use the same persistent release venv that build_release.ps1 will use.
        release_venv = root / ".release_venv"
        release_python = venv_python(release_venv)

        stale_rollbacks_removed = cleanup_stale_rollback_files(root)
        result["stale_rollback_files_removed"] = stale_rollbacks_removed

        pip_healthy, pip_health_detail = pip_is_healthy(
            release_python,
            cwd=root,
        )
        result["release_venv_initial_pip_health"] = {
            "healthy": pip_healthy,
            "detail": pip_health_detail,
        }

        if not pip_healthy:
            release_python = recreate_release_venv(
                root,
                release_venv,
                steps=steps,
            )
            result["release_venv_recreated"] = True
        else:
            result["release_venv_recreated"] = False

        pip_step = run(
            "Update release-environment pip",
            [
                str(release_python),
                "-m",
                "pip",
                "install",
                "--upgrade",
                "pip",
            ],
            cwd=root,
        )
        steps.append(pip_step)
        if not pip_step.get("passed"):
            # A pip installation can be superficially healthy yet fail on the
            # first install command. Rebuild the venv once, then retry.
            release_python = recreate_release_venv(
                root,
                release_venv,
                steps=steps,
            )
            result["release_venv_recreated"] = True
            pip_step = run(
                "Update release-environment pip after venv rebuild",
                [
                    str(release_python),
                    "-m",
                    "pip",
                    "install",
                    "--upgrade",
                    "pip",
                ],
                cwd=root,
            )
            steps.append(pip_step)
        fail_if(pip_step)

        for rel, label in (
            ("requirements.txt", "Install runtime requirements"),
            ("requirements-ocr.txt", "Install OCR requirements"),
            ("requirements-build.txt", "Install build requirements"),
        ):
            install_step = run(
                label,
                [
                    str(release_python),
                    "-m",
                    "pip",
                    "install",
                    "-r",
                    str(root / rel),
                ],
                cwd=root,
            )
            steps.append(install_step)
            fail_if(install_step)

        env = dict(os.environ)
        env["PYTHONPATH"] = str(root / "src")

        # Arm rollback only now. Environment/requirements setup above does not
        # mutate factory release data, so failures there require no data rollback.
        backup_root = Path(
            tempfile.mkdtemp(
                prefix="field_crafter_release_data_rollback_",
                dir=str(diagnostic_dir()),
            )
        )
        backup_manifest = backup_release_state(
            root,
            backup_root,
        )
        result["release_data_rollback_armed"] = True

        print(
            "\nStarting live release-data preparation. "
            "Progress from prepare_release.py follows:",
            flush=True,
        )
        prepare_step = run_streaming(
            "Live release-data preparation",
            [
                str(release_python),
                str(root / "prepare_release.py"),
            ],
            cwd=root,
            env=env,
        )
        steps.append(prepare_step)
        fail_if(prepare_step)

        print(
            "\nStarting independent release-data validation:",
            flush=True,
        )
        validate_step = run_streaming(
            "Independent release-data validation",
            [
                str(release_python),
                str(root / "validate_release_data.py"),
            ],
            cwd=root,
            env=env,
        )
        steps.append(validate_step)
        fail_if(validate_step)

        after = snapshot_files(root)
        result["hashes_after"] = after

        immutable_changes = [
            rel
            for rel in IMMUTABLE_MEMORY_FILES
            if before.get(rel) != after.get(rel)
        ]
        if immutable_changes:
            raise RuntimeError(
                "Bundled memory definition/config files changed during "
                "release-data preparation: "
                + ", ".join(immutable_changes)
            )

        summary = load_json(
            root / "data" / "release_data_summary.json"
        )
        info = load_json(
            root / "data" / "release_database_info.json"
        )
        if not summary or not info:
            raise RuntimeError(
                "Release metadata was not generated after preparation."
            )

        if not summary.get("release_data_ready"):
            raise RuntimeError(
                "release_data_summary.json does not mark release data ready."
            )
        if not summary.get("redistribution_ready"):
            raise RuntimeError(
                "release_data_summary.json does not mark redistribution ready."
            )
        if not info.get("redistribution_ready"):
            raise RuntimeError(
                "release_database_info.json does not mark redistribution ready."
            )

        memory_hashes = {
            rel.split("/")[-1]: after.get(rel)
            for rel in IMMUTABLE_MEMORY_FILES
        }
        summary_hashes = (
            summary.get("sha256")
            if isinstance(summary.get("sha256"), dict)
            else {}
        )
        for name, digest in memory_hashes.items():
            if summary_hashes.get(name) != digest:
                raise RuntimeError(
                    f"Release summary hash for {name} does not match "
                    "the bundled file."
                )

        result.update(
            {
                "passed": True,
                "database_counts_after": db_counts(
                    root / "data" / "homecoming_recipes.sqlite"
                ),
                "release_data_summary": summary,
                "release_database_info": info,
                "memory_definition_files_unchanged": True,
                "memory_profile_pack_version": summary.get(
                    "memory_profile_pack_version"
                ),
                "memory_profile_count": summary.get(
                    "memory_profile_count"
                ),
                "memory_update_channel": summary.get(
                    "memory_update_channel"
                ),
                "memory_channel_publication_pending": True,
                "git_status_after": git_status(root),
                "rollback_performed": False,
            }
        )

        # Successful operation no longer needs its rollback snapshot.
        if backup_root.exists():
            shutil.rmtree(backup_root)
            backup_root = None

    except Exception as exc:
        result["error"] = str(exc)

        if root is not None and backup_root is not None and backup_manifest is not None:
            try:
                restore_release_state(
                    root,
                    backup_root,
                    backup_manifest,
                )
                restored = True
                result["rollback_performed"] = True
                result["rollback_succeeded"] = True
            except Exception as rollback_exc:
                result["rollback_performed"] = True
                result["rollback_succeeded"] = False
                result["rollback_error"] = str(rollback_exc)
        else:
            result["rollback_performed"] = False
            result["rollback_succeeded"] = None

        if root is not None:
            result["hashes_after_failure"] = snapshot_files(root)
            failure_log = (
                root / "data" / "last_release_prepare_failure.txt"
            )
            if failure_log.is_file():
                try:
                    result["prepare_failure_log_tail"] = tail(
                        failure_log.read_text(
                            encoding="utf-8",
                            errors="replace",
                        ),
                        160,
                    )
                except Exception:
                    pass

    finally:
        result["steps"] = [
            {
                "name": step.get("name"),
                "passed": step.get("passed"),
                "returncode": step.get("returncode"),
                "command": step.get("command"),
                "stdout_tail": tail(step.get("stdout") or "", 160),
                "stderr_tail": tail(step.get("stderr") or "", 120),
            }
            for step in steps
        ]

        if backup_root is not None and backup_root.exists():
            try:
                shutil.rmtree(backup_root)
            except Exception:
                pass

        result_path.write_text(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    print(
        f"{'PASS' if result.get('passed') else 'FAIL'}: "
        f"release-data preparation result written to {result_path}"
    )
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
