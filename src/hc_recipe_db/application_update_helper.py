from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PLAN_SCHEMA_VERSION = 1


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    root = root.resolve()
    digest = hashlib.sha256()
    for path in sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: p.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        size = path.stat().st_size
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            pass
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_for_pid_exit(pid: int, timeout_sec: float) -> None:
    deadline = time.monotonic() + max(1.0, float(timeout_sec))
    while time.monotonic() < deadline:
        if not _pid_is_alive(pid):
            return
        time.sleep(0.20)
    raise RuntimeError(f"Field Crafter process {pid} did not exit within {timeout_sec:.0f} seconds.")


def _remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _restore_backup(target: Path, backup: Path) -> None:
    try:
        if target.exists():
            _remove_path(target)
        if backup.exists():
            os.replace(backup, target)
    except Exception:
        pass


def _validate_python_tree(root: Path, expected_version: str) -> None:
    required = (
        root / "Field Crafter.pyw",
        root / "field_crafter_entry.py",
        root / ".field_crafter_release",
        root / "src" / "hc_recipe_db" / "version.py",
        root / "data" / "application_update_config.json",
    )
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError("Staged Python update is incomplete: " + ", ".join(missing))
    version_text = (root / "src" / "hc_recipe_db" / "version.py").read_text(encoding="utf-8")
    marker = f'RELEASE_VERSION = "{expected_version}"'
    if marker not in version_text:
        raise RuntimeError(
            f"Staged Python update does not identify itself as Field Crafter {expected_version}."
        )


def _apply_exe_plan(plan: dict[str, Any]) -> None:
    target = Path(plan["target_path"]).resolve()
    staged = Path(plan["staged_path"]).resolve()
    backup = Path(plan["backup_path"]).resolve()
    expected = str(plan["expected_sha256"]).lower()
    state_path = Path(plan["state_path"]).resolve()

    if not staged.is_file():
        raise RuntimeError(f"Staged application update is missing: {staged}")
    actual = _sha256_file(staged)
    if actual != expected:
        raise RuntimeError(f"Staged EXE hash mismatch: expected {expected}, got {actual}")
    if not target.is_file():
        raise RuntimeError(f"Current Field Crafter EXE is missing: {target}")
    if backup.exists():
        raise RuntimeError(f"Refusing to overwrite an existing update backup: {backup}")

    try:
        os.replace(target, backup)
        os.replace(staged, target)
        actual_target = _sha256_file(target)
        if actual_target != expected:
            raise RuntimeError(
                f"Installed EXE hash mismatch: expected {expected}, got {actual_target}"
            )
    except Exception:
        _restore_backup(target, backup)
        try:
            if target.is_file():
                subprocess.Popen([str(target)], cwd=str(target.parent), close_fds=True)
        except Exception:
            pass
        raise

    state = {
        "schema_version": 1,
        "status": "awaiting_startup_finalize",
        "mode": "exe",
        "from_version": plan.get("from_version"),
        "to_version": plan.get("to_version"),
        "target_path": str(target),
        "backup_path": str(backup),
        "installed_at_utc": _now_utc(),
    }
    _write_json_atomic(state_path, state)

    try:
        subprocess.Popen([str(target)], cwd=str(target.parent), close_fds=True)
    except Exception:
        _restore_backup(target, backup)
        state["status"] = "launch_failed_rolled_back"
        state["failed_at_utc"] = _now_utc()
        _write_json_atomic(state_path, state)
        raise


def _apply_python_plan(plan: dict[str, Any]) -> None:
    target = Path(plan["target_path"]).resolve()
    staged = Path(plan["staged_path"]).resolve()
    backup = Path(plan["backup_path"]).resolve()
    expected_tree = str(plan["expected_tree_sha256"]).lower()
    expected_version = str(plan["to_version"])
    state_path = Path(plan["state_path"]).resolve()
    launch_executable = Path(plan["launch_executable"]).resolve()

    if not target.is_dir():
        raise RuntimeError(f"Current Field Crafter Python folder is missing: {target}")
    if not staged.is_dir():
        raise RuntimeError(f"Staged Python update folder is missing: {staged}")
    if backup.exists():
        raise RuntimeError(f"Refusing to overwrite an existing update backup: {backup}")
    if not launch_executable.is_file():
        raise RuntimeError(f"System Python launcher is unavailable: {launch_executable}")

    _validate_python_tree(staged, expected_version)
    actual_tree = _tree_sha256(staged)
    if actual_tree != expected_tree:
        raise RuntimeError(
            f"Staged Python tree hash mismatch: expected {expected_tree}, got {actual_tree}"
        )

    try:
        os.replace(target, backup)
        os.replace(staged, target)
        _validate_python_tree(target, expected_version)
        actual_target_tree = _tree_sha256(target)
        if actual_target_tree != expected_tree:
            raise RuntimeError(
                f"Installed Python tree hash mismatch: expected {expected_tree}, got {actual_target_tree}"
            )
    except Exception:
        _restore_backup(target, backup)
        try:
            old_launcher = target / "Field Crafter.pyw"
            if target.is_dir() and old_launcher.is_file():
                creationflags = 0
                if os.name == "nt":
                    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
                subprocess.Popen(
                    [str(launch_executable), str(old_launcher)],
                    cwd=str(target),
                    creationflags=creationflags,
                    close_fds=True,
                )
        except Exception:
            pass
        raise

    state = {
        "schema_version": 1,
        "status": "awaiting_startup_finalize",
        "mode": "python",
        "from_version": plan.get("from_version"),
        "to_version": expected_version,
        "target_path": str(target),
        "backup_path": str(backup),
        "installed_at_utc": _now_utc(),
    }
    _write_json_atomic(state_path, state)

    launcher = target / "Field Crafter.pyw"
    try:
        creationflags = 0
        if os.name == "nt":
            creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        subprocess.Popen(
            [str(launch_executable), str(launcher)],
            cwd=str(target),
            creationflags=creationflags,
            close_fds=True,
        )
    except Exception:
        _restore_backup(target, backup)
        state["status"] = "launch_failed_rolled_back"
        state["failed_at_utc"] = _now_utc()
        _write_json_atomic(state_path, state)
        raise


def run_update_plan(plan_path: str | Path) -> int:
    path = Path(plan_path).resolve()
    plan = json.loads(path.read_text(encoding="utf-8"))
    if int(plan.get("schema_version") or 0) != PLAN_SCHEMA_VERSION:
        raise RuntimeError("Unsupported Field Crafter application-update plan schema.")

    ready_path = Path(plan["ready_path"]).resolve()
    ready_path.parent.mkdir(parents=True, exist_ok=True)
    ready_path.write_text(f"ready {os.getpid()} {_now_utc()}\n", encoding="utf-8")

    _wait_for_pid_exit(int(plan["old_pid"]), float(plan.get("wait_timeout_sec") or 120.0))
    mode = str(plan.get("mode") or "")
    if mode == "exe":
        _apply_exe_plan(plan)
    elif mode == "python":
        _apply_python_plan(plan)
    else:
        raise RuntimeError(f"Unsupported Field Crafter application-update mode: {mode!r}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: application_update_helper.py <plan.json>")
    try:
        raise SystemExit(run_update_plan(sys.argv[1]))
    except Exception as exc:
        print(f"Field Crafter application update failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
