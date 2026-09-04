from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import requests

from .application_update_helper import _tree_sha256
from .memory_update_crypto import verify as ed25519_verify
from .version import APP_VERSION


APPLICATION_UPDATE_SCHEMA_VERSION = 1
CONFIG_SCHEMA_VERSION = 1
ALLOWED_UPDATE_HOSTS = {
    "github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "github-releases.githubusercontent.com",
}
DEFAULT_MANIFEST_MAX_BYTES = 1024 * 1024
DEFAULT_ARTIFACT_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_EXTRACT_MAX_BYTES = 1536 * 1024 * 1024


class ApplicationUpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApplicationUpdateArtifact:
    distribution: str
    url: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class ApplicationUpdateCandidate:
    current_version: str
    version: str
    channel: str
    distribution: str
    artifact: ApplicationUpdateArtifact
    release_url: str
    summary: str
    manifest: dict[str, Any]
    config: dict[str, Any]


@dataclass(frozen=True)
class ApplicationUpdateCheck:
    update_available: bool
    current_version: str
    latest_version: str | None
    distribution: str
    message: str
    release_url: str | None = None
    candidate: ApplicationUpdateCandidate | None = None


@dataclass(frozen=True)
class PreparedApplicationUpdate:
    version: str
    distribution: str
    plan_path: Path
    helper_ready_path: Path
    helper_log_path: Path


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parents[2]


def _user_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    out = Path(base) / "FieldCrafter" if base else Path.home() / ".field_crafter"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _updates_dir() -> Path:
    out = _user_data_dir() / "updates"
    out.mkdir(parents=True, exist_ok=True)
    return out


def bundled_application_update_config_path() -> Path:
    return _resource_root() / "data" / "application_update_config.json"


def application_update_state_path() -> Path:
    return _updates_dir() / "application_update_state.json"


def _version_tuple(value: str) -> tuple[int, ...]:
    text = str(value or "").strip()
    match = re.match(r"^(\d+(?:\.\d+)*)", text)
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def _is_newer_version(candidate: str, current: str) -> bool:
    left = _version_tuple(candidate)
    right = _version_tuple(current)
    if not left or not right:
        raise ApplicationUpdateError(
            f"Could not compare application versions {candidate!r} and {current!r}."
        )
    width = max(len(left), len(right))
    left += (0,) * (width - len(left))
    right += (0,) * (width - len(right))
    return left > right


def _version_at_least(current: str, minimum: str) -> bool:
    if not minimum:
        return True
    current_tuple = _version_tuple(current)
    minimum_tuple = _version_tuple(minimum)
    if not current_tuple or not minimum_tuple:
        return False
    width = max(len(current_tuple), len(minimum_tuple))
    current_tuple += (0,) * (width - len(current_tuple))
    minimum_tuple += (0,) * (width - len(minimum_tuple))
    return current_tuple >= minimum_tuple


def _require_github_https_url(value: Any, field: str) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme.casefold() != "https" or host not in ALLOWED_UPDATE_HOSTS:
        raise ApplicationUpdateError(
            f"{field} must be an HTTPS GitHub URL on an approved host."
        )
    return url


def _decode_public_key(value: Any) -> bytes:
    try:
        public_key = base64.b64decode(str(value), validate=True)
    except Exception as exc:
        raise ApplicationUpdateError("Application-update public key is not valid base64.") from exc
    if len(public_key) != 32:
        raise ApplicationUpdateError("Application-update Ed25519 public key must be 32 bytes.")
    return public_key


def load_application_update_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path) if path is not None else bundled_application_update_config_path()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ApplicationUpdateError(f"Could not read application-update configuration: {exc}") from exc
    if int(config.get("schema_version") or 0) != CONFIG_SCHEMA_VERSION:
        raise ApplicationUpdateError("Unsupported application-update configuration schema.")
    channel = str(config.get("channel") or "").strip()
    if not channel:
        raise ApplicationUpdateError("Application-update configuration is missing its channel.")
    _require_github_https_url(config.get("manifest_url"), "manifest_url")
    _decode_public_key(config.get("public_key_ed25519"))
    return config


def _canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    body = {key: value for key, value in manifest.items() if key != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def verify_application_update_manifest(
    manifest: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if config is None:
        config = load_application_update_config()
    if not isinstance(manifest, dict):
        raise ApplicationUpdateError("Application-update manifest must be a JSON object.")
    if int(manifest.get("schema_version") or 0) != APPLICATION_UPDATE_SCHEMA_VERSION:
        raise ApplicationUpdateError("Unsupported application-update manifest schema.")

    required = (
        "channel",
        "version",
        "minimum_updater_version",
        "release_url",
        "artifacts",
        "signature",
    )
    missing = [name for name in required if manifest.get(name) in (None, "")]
    if missing:
        raise ApplicationUpdateError(
            "Application-update manifest is missing required fields: " + ", ".join(missing)
        )

    channel = str(manifest["channel"]).strip()
    if channel != str(config["channel"]).strip():
        raise ApplicationUpdateError(
            f"Application-update channel mismatch: expected {config['channel']!r}, got {channel!r}."
        )
    version = str(manifest["version"]).strip()
    if not _version_tuple(version):
        raise ApplicationUpdateError(f"Invalid application-update version: {version!r}")
    minimum = str(manifest.get("minimum_updater_version") or "").strip()
    if minimum and not _version_tuple(minimum):
        raise ApplicationUpdateError(f"Invalid minimum_updater_version: {minimum!r}")
    _require_github_https_url(manifest.get("release_url"), "release_url")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ApplicationUpdateError("Application-update manifest artifacts must be an object.")
    for distribution in ("exe", "python"):
        artifact = artifacts.get(distribution)
        if not isinstance(artifact, dict):
            raise ApplicationUpdateError(
                f"Application-update manifest is missing the {distribution!r} artifact."
            )
        _require_github_https_url(artifact.get("url"), f"artifacts.{distribution}.url")
        sha = str(artifact.get("sha256") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ApplicationUpdateError(
                f"artifacts.{distribution}.sha256 is not a valid SHA-256 digest."
            )
        try:
            size = int(artifact.get("bytes"))
        except (TypeError, ValueError) as exc:
            raise ApplicationUpdateError(
                f"artifacts.{distribution}.bytes must be an integer."
            ) from exc
        if size <= 0:
            raise ApplicationUpdateError(
                f"artifacts.{distribution}.bytes must be positive."
            )

    try:
        signature = base64.b64decode(str(manifest["signature"]), validate=True)
    except Exception as exc:
        raise ApplicationUpdateError("Application-update manifest signature is not valid base64.") from exc
    if len(signature) != 64:
        raise ApplicationUpdateError("Application-update Ed25519 signature must be 64 bytes.")
    public_key = _decode_public_key(config.get("public_key_ed25519"))
    if not ed25519_verify(public_key, _canonical_manifest_bytes(manifest), signature):
        raise ApplicationUpdateError("Application-update manifest signature verification failed.")
    return manifest


def _download_manifest(url: str, *, max_bytes: int, timeout: tuple[float, float]) -> bytes:
    response = requests.get(url, stream=True, timeout=timeout, allow_redirects=True)
    try:
        response.raise_for_status()
        _require_github_https_url(response.url, "redirected manifest URL")
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    raise ApplicationUpdateError("Application-update manifest is unexpectedly large.")
            except ValueError:
                pass
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise ApplicationUpdateError("Application-update manifest exceeded its size limit.")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        response.close()


def current_application_distribution() -> str:
    if getattr(sys, "frozen", False):
        return "exe"
    try:
        root = Path(__file__).resolve().parents[2]
        if (root / ".field_crafter_release").is_file():
            return "python"
    except (OSError, IndexError):
        pass
    return "development"


def current_application_target() -> Path | None:
    distribution = current_application_distribution()
    if distribution == "exe":
        return Path(sys.executable).resolve()
    if distribution == "python":
        return Path(__file__).resolve().parents[2]
    return None


def check_for_application_update(
    *,
    config_path: str | Path | None = None,
    timeout: tuple[float, float] = (5.0, 20.0),
) -> ApplicationUpdateCheck:
    distribution = current_application_distribution()
    if distribution == "development":
        return ApplicationUpdateCheck(
            update_available=False,
            current_version=APP_VERSION,
            latest_version=None,
            distribution=distribution,
            message=(
                "Application self-update is disabled in a development source checkout. "
                "Test it from a packaged Field Crafter EXE or prepared Python release."
            ),
        )

    config = load_application_update_config(config_path)
    manifest_url = _require_github_https_url(config["manifest_url"], "manifest_url")
    max_manifest = int(config.get("max_manifest_bytes") or DEFAULT_MANIFEST_MAX_BYTES)
    raw = _download_manifest(manifest_url, max_bytes=max_manifest, timeout=timeout)
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ApplicationUpdateError(f"Application-update manifest is not valid JSON: {exc}") from exc
    verify_application_update_manifest(manifest, config=config)

    latest = str(manifest["version"]).strip()
    release_url = str(manifest["release_url"]).strip()
    if not _is_newer_version(latest, APP_VERSION):
        return ApplicationUpdateCheck(
            update_available=False,
            current_version=APP_VERSION,
            latest_version=latest,
            distribution=distribution,
            message=f"Field Crafter {APP_VERSION} is up to date.",
            release_url=release_url,
        )

    minimum = str(manifest.get("minimum_updater_version") or "").strip()
    if minimum and not _version_at_least(APP_VERSION, minimum):
        return ApplicationUpdateCheck(
            update_available=False,
            current_version=APP_VERSION,
            latest_version=latest,
            distribution=distribution,
            message=(
                f"Field Crafter {latest} is available, but this updater is too old to install it safely. "
                f"Install Field Crafter {minimum} or newer manually first."
            ),
            release_url=release_url,
        )

    artifact_doc = manifest["artifacts"][distribution]
    artifact = ApplicationUpdateArtifact(
        distribution=distribution,
        url=str(artifact_doc["url"]),
        sha256=str(artifact_doc["sha256"]).lower(),
        size_bytes=int(artifact_doc["bytes"]),
    )
    candidate = ApplicationUpdateCandidate(
        current_version=APP_VERSION,
        version=latest,
        channel=str(manifest["channel"]),
        distribution=distribution,
        artifact=artifact,
        release_url=release_url,
        summary=str(manifest.get("summary") or "").strip(),
        manifest=manifest,
        config=config,
    )
    return ApplicationUpdateCheck(
        update_available=True,
        current_version=APP_VERSION,
        latest_version=latest,
        distribution=distribution,
        message=f"Field Crafter {latest} is available.",
        release_url=release_url,
        candidate=candidate,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_artifact_to_path(
    url: str,
    destination: Path,
    *,
    expected_sha256: str,
    expected_size: int,
    max_bytes: int,
    timeout: tuple[float, float],
) -> None:
    _require_github_https_url(url, "artifact URL")
    if expected_size > max_bytes:
        raise ApplicationUpdateError(
            f"Application update is {expected_size:,} bytes, exceeding the configured download limit."
        )
    response = requests.get(url, stream=True, timeout=timeout, allow_redirects=True)
    try:
        response.raise_for_status()
        _require_github_https_url(response.url, "redirected artifact URL")
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                reported = int(content_length)
                if reported > max_bytes:
                    raise ApplicationUpdateError("Application update exceeded its configured size limit.")
            except ValueError:
                pass
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_suffix(destination.suffix + ".part")
        digest = hashlib.sha256()
        total = 0
        try:
            with part.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise ApplicationUpdateError(
                            "Application update exceeded its configured size limit while downloading."
                        )
                    digest.update(chunk)
                    handle.write(chunk)
            if total != expected_size:
                raise ApplicationUpdateError(
                    f"Application update size mismatch: expected {expected_size:,} bytes, got {total:,}."
                )
            actual = digest.hexdigest()
            if actual != expected_sha256:
                raise ApplicationUpdateError(
                    f"Application update SHA-256 mismatch: expected {expected_sha256}, got {actual}."
                )
            os.replace(part, destination)
        finally:
            if part.exists():
                part.unlink(missing_ok=True)
    finally:
        response.close()


def download_application_update(
    candidate: ApplicationUpdateCandidate,
    *,
    timeout: tuple[float, float] = (5.0, 120.0),
) -> Path:
    max_bytes = int(candidate.config.get("max_download_bytes") or DEFAULT_ARTIFACT_MAX_BYTES)
    suffix = ".exe" if candidate.distribution == "exe" else ".zip"
    update_dir = _updates_dir() / candidate.version
    destination = update_dir / f"Field_Crafter_{candidate.version}_{candidate.distribution}{suffix}"
    if destination.is_file():
        if (
            destination.stat().st_size == candidate.artifact.size_bytes
            and _sha256_file(destination) == candidate.artifact.sha256
        ):
            return destination
        destination.unlink(missing_ok=True)
    _download_artifact_to_path(
        candidate.artifact.url,
        destination,
        expected_sha256=candidate.artifact.sha256,
        expected_size=candidate.artifact.size_bytes,
        max_bytes=max_bytes,
        timeout=timeout,
    )
    return destination


def _safe_extract_python_release(
    archive_path: Path,
    destination: Path,
    *,
    version: str,
    max_extract_bytes: int,
) -> Path:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            bad = archive.testzip()
            if bad is not None:
                raise ApplicationUpdateError(f"Python update ZIP CRC validation failed for {bad!r}.")
            total = 0
            top_levels: set[str] = set()
            for info in archive.infolist():
                name = info.filename.replace("\\", "/")
                posix = PurePosixPath(name)
                if posix.is_absolute() or ".." in posix.parts:
                    raise ApplicationUpdateError(f"Python update ZIP contains an unsafe path: {info.filename!r}")
                if not posix.parts:
                    continue
                if any(part in {"", "."} or ":" in part for part in posix.parts):
                    raise ApplicationUpdateError(f"Python update ZIP contains an unsafe path: {info.filename!r}")
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise ApplicationUpdateError(
                        f"Python update ZIP contains an unsupported symbolic link: {info.filename!r}"
                    )
                total += max(0, int(info.file_size))
                if total > max_extract_bytes:
                    raise ApplicationUpdateError("Python update ZIP exceeded its extraction-size limit.")
                top_levels.add(posix.parts[0])
                target = (destination / Path(*posix.parts)).resolve()
                try:
                    target.relative_to(destination.resolve())
                except ValueError as exc:
                    raise ApplicationUpdateError(
                        f"Python update ZIP member escapes the extraction directory: {info.filename!r}"
                    ) from exc
            if len(top_levels) != 1:
                raise ApplicationUpdateError(
                    "Prepared Python update must contain exactly one top-level release folder."
                )
            archive.extractall(destination)
    except zipfile.BadZipFile as exc:
        raise ApplicationUpdateError(f"Python update is not a valid ZIP archive: {exc}") from exc

    root = destination / next(iter(top_levels))
    required = (
        root / "Field Crafter.pyw",
        root / "field_crafter_entry.py",
        root / ".field_crafter_release",
        root / "src" / "hc_recipe_db" / "version.py",
        root / "data" / "application_update_config.json",
    )
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    if missing:
        raise ApplicationUpdateError(
            "Prepared Python update is incomplete: " + ", ".join(missing)
        )
    version_text = (root / "src" / "hc_recipe_db" / "version.py").read_text(encoding="utf-8")
    if f'RELEASE_VERSION = "{version}"' not in version_text:
        raise ApplicationUpdateError(
            f"Prepared Python update does not identify itself as Field Crafter {version}."
        )
    wheelhouse = root / "wheelhouse"
    if not wheelhouse.is_dir() or not any(wheelhouse.glob("*.whl")):
        raise ApplicationUpdateError("Prepared Python update is missing its offline dependency wheelhouse.")
    return root


def _same_path(left: Path, right: Path) -> bool:
    try:
        return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))
    except OSError:
        return False


def _unique_sibling(target: Path, label: str, version: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_version = re.sub(r"[^0-9A-Za-z._-]+", "_", version)
    return target.parent / f".{target.name}.fieldcrafter-{label}-{safe_version}-{os.getpid()}-{stamp}"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _base_python_launcher(target_root: Path) -> Path:
    candidates: list[Path] = []
    raw_base_executable = getattr(sys, "_base_executable", None)
    if raw_base_executable:
        base_exe = Path(raw_base_executable)
        if base_exe.name.casefold() == "python.exe":
            candidates.append(base_exe.with_name("pythonw.exe"))
        candidates.append(base_exe)
    base_prefix = Path(sys.base_prefix)
    candidates.extend([base_prefix / "pythonw.exe", base_prefix / "python.exe"])
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            if not resolved.is_file():
                continue
            try:
                resolved.relative_to(target_root.resolve())
                continue
            except ValueError:
                return resolved
        except OSError:
            continue
    raise ApplicationUpdateError(
        "Could not locate the system Python interpreter outside the Field Crafter release folder."
    )


def _powershell_helper_text() -> str:
    return r'''param([Parameter(Mandatory=$true)][string]$PlanPath)
$ErrorActionPreference = "Stop"
$plan = Get-Content -LiteralPath $PlanPath -Raw | ConvertFrom-Json
$ready = [string]$plan.ready_path
$readyDir = Split-Path -Parent $ready
if ($readyDir) { New-Item -ItemType Directory -Force $readyDir | Out-Null }
[System.IO.File]::WriteAllText($ready, "ready $PID " + [DateTimeOffset]::UtcNow.ToString("o") + "`n", [System.Text.UTF8Encoding]::new($false))
try { Wait-Process -Id ([int]$plan.old_pid) -Timeout ([int]$plan.wait_timeout_sec) -ErrorAction SilentlyContinue } catch { }
$old = Get-Process -Id ([int]$plan.old_pid) -ErrorAction SilentlyContinue
if ($old) { throw "Field Crafter did not exit before the application-update timeout." }
$target = [string]$plan.target_path
$staged = [string]$plan.staged_path
$backup = [string]$plan.backup_path
$statePath = [string]$plan.state_path
$expected = ([string]$plan.expected_sha256).ToLowerInvariant()
if (-not (Test-Path -LiteralPath $staged -PathType Leaf)) { throw "Staged Field Crafter EXE is missing: $staged" }
$actual = (Get-FileHash -LiteralPath $staged -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $expected) { throw "Staged Field Crafter EXE hash mismatch." }
if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw "Current Field Crafter EXE is missing: $target" }
if (Test-Path -LiteralPath $backup) { throw "Refusing to overwrite an existing Field Crafter update backup: $backup" }
$backupCreated = $false
try {
    Move-Item -LiteralPath $target -Destination $backup
    $backupCreated = $true
    Move-Item -LiteralPath $staged -Destination $target
    $installed = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($installed -ne $expected) { throw "Installed Field Crafter EXE hash mismatch." }
    $state = [ordered]@{
        schema_version = 1
        status = "awaiting_startup_finalize"
        mode = "exe"
        from_version = [string]$plan.from_version
        to_version = [string]$plan.to_version
        target_path = $target
        backup_path = $backup
        installed_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
    }
    $stateDir = Split-Path -Parent $statePath
    if ($stateDir) { New-Item -ItemType Directory -Force $stateDir | Out-Null }
    $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $statePath -Encoding UTF8
    Start-Process -FilePath $target -WorkingDirectory (Split-Path -Parent $target)
} catch {
    if ($backupCreated) {
        try {
            if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }
            if (Test-Path -LiteralPath $backup) { Move-Item -LiteralPath $backup -Destination $target }
        } catch { }
        try { if (Test-Path -LiteralPath $target) { Start-Process -FilePath $target -WorkingDirectory (Split-Path -Parent $target) } } catch { }
    }
    throw
}
'''


def _launch_helper_and_wait_ready(
    command: list[str],
    *,
    ready_path: Path,
    log_path: Path,
    cwd: Path,
    timeout_sec: float = 5.0,
) -> None:
    ready_path.unlink(missing_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    creationflags = 0
    if os.name == "nt":
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    with log_path.open("ab") as log_handle:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=log_handle,
            stderr=log_handle,
            creationflags=creationflags,
            close_fds=True,
        )
    deadline = time.monotonic() + max(1.0, timeout_sec)
    while time.monotonic() < deadline:
        if ready_path.is_file():
            return
        code = process.poll()
        if code is not None:
            raise ApplicationUpdateError(
                f"Application-update helper exited before it was ready (exit code {code}). "
                f"See {log_path}."
            )
        time.sleep(0.05)
    try:
        process.terminate()
    except Exception:
        pass
    raise ApplicationUpdateError(
        f"Application-update helper did not become ready. See {log_path}."
    )


def prepare_application_update_install(
    candidate: ApplicationUpdateCandidate,
    downloaded_artifact: str | Path,
) -> PreparedApplicationUpdate:
    if candidate.distribution != current_application_distribution():
        raise ApplicationUpdateError("Application-update distribution changed before installation.")
    target = current_application_target()
    if target is None:
        raise ApplicationUpdateError("Application self-update is unavailable in a development source checkout.")
    artifact = Path(downloaded_artifact).resolve()
    if not artifact.is_file():
        raise ApplicationUpdateError(f"Downloaded application update is missing: {artifact}")
    if artifact.stat().st_size != candidate.artifact.size_bytes:
        raise ApplicationUpdateError("Downloaded application update size changed before installation.")
    if _sha256_file(artifact) != candidate.artifact.sha256:
        raise ApplicationUpdateError("Downloaded application update hash changed before installation.")

    run_dir = _updates_dir() / candidate.version / f"install_{os.getpid()}_{int(time.time())}"
    run_dir.mkdir(parents=True, exist_ok=False)
    plan_path = run_dir / "application_update_plan.json"
    ready_path = run_dir / "helper.ready"
    helper_log = run_dir / "helper.log"
    state_path = application_update_state_path()

    if candidate.distribution == "exe":
        if os.name != "nt":
            raise ApplicationUpdateError("Portable EXE self-update is supported only on Windows.")
        if not target.is_file():
            raise ApplicationUpdateError(f"Current Field Crafter EXE is missing: {target}")
        staged = _unique_sibling(target, "update", candidate.version)
        backup = _unique_sibling(target, "backup", candidate.current_version)
        copying = staged.with_name(staged.name + ".copying")
        try:
            shutil.copy2(artifact, copying)
            if _sha256_file(copying) != candidate.artifact.sha256:
                raise ApplicationUpdateError("Same-volume staged EXE failed SHA-256 verification.")
            os.replace(copying, staged)
        finally:
            copying.unlink(missing_ok=True)
        plan = {
            "schema_version": 1,
            "mode": "exe",
            "old_pid": os.getpid(),
            "wait_timeout_sec": 120,
            "from_version": candidate.current_version,
            "to_version": candidate.version,
            "target_path": str(target),
            "staged_path": str(staged),
            "backup_path": str(backup),
            "expected_sha256": candidate.artifact.sha256,
            "state_path": str(state_path),
            "ready_path": str(ready_path),
        }
        _write_json_atomic(plan_path, plan)
        helper_path = run_dir / "application_update_helper.ps1"
        helper_path.write_text(_powershell_helper_text(), encoding="utf-8-sig")
        _launch_helper_and_wait_ready(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(helper_path),
                "-PlanPath",
                str(plan_path),
            ],
            ready_path=ready_path,
            log_path=helper_log,
            cwd=run_dir,
        )
    elif candidate.distribution == "python":
        if not target.is_dir():
            raise ApplicationUpdateError(f"Current Field Crafter Python folder is missing: {target}")
        extract_dir = run_dir / "extracted"
        extracted_root = _safe_extract_python_release(
            artifact,
            extract_dir,
            version=candidate.version,
            max_extract_bytes=int(candidate.config.get("max_extract_bytes") or DEFAULT_EXTRACT_MAX_BYTES),
        )
        staged = _unique_sibling(target, "update", candidate.version)
        backup = _unique_sibling(target, "backup", candidate.current_version)
        try:
            shutil.copytree(extracted_root, staged)
        except Exception:
            if staged.exists():
                shutil.rmtree(staged, ignore_errors=True)
            raise
        expected_tree = _tree_sha256(staged)
        launch_executable = _base_python_launcher(target)
        plan = {
            "schema_version": 1,
            "mode": "python",
            "old_pid": os.getpid(),
            "wait_timeout_sec": 120,
            "from_version": candidate.current_version,
            "to_version": candidate.version,
            "target_path": str(target),
            "staged_path": str(staged),
            "backup_path": str(backup),
            "expected_tree_sha256": expected_tree,
            "launch_executable": str(launch_executable),
            "state_path": str(state_path),
            "ready_path": str(ready_path),
        }
        _write_json_atomic(plan_path, plan)
        helper_source = Path(__file__).resolve().with_name("application_update_helper.py")
        helper_path = run_dir / "application_update_helper.py"
        shutil.copy2(helper_source, helper_path)
        _launch_helper_and_wait_ready(
            [str(launch_executable), str(helper_path), str(plan_path)],
            ready_path=ready_path,
            log_path=helper_log,
            cwd=run_dir,
        )
    else:
        raise ApplicationUpdateError(f"Unsupported application distribution: {candidate.distribution!r}")

    return PreparedApplicationUpdate(
        version=candidate.version,
        distribution=candidate.distribution,
        plan_path=plan_path,
        helper_ready_path=ready_path,
        helper_log_path=helper_log,
    )


def finalize_pending_application_update() -> str | None:
    state_path = application_update_state_path()
    if not state_path.is_file():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    if state.get("status") != "awaiting_startup_finalize":
        return None
    if str(state.get("to_version") or "") != APP_VERSION:
        return None
    target = current_application_target()
    if target is None:
        return None
    recorded_target = Path(str(state.get("target_path") or ""))
    if not _same_path(target, recorded_target):
        return None
    backup = Path(str(state.get("backup_path") or ""))
    try:
        if backup.exists():
            if backup.resolve().parent != target.resolve().parent or _same_path(backup, target):
                raise ApplicationUpdateError("Refusing to remove an unexpected application-update backup path.")
            if backup.is_dir():
                shutil.rmtree(backup)
            else:
                backup.unlink()
        state["status"] = "installed"
        state["finalized_at_utc"] = _now_utc()
        _write_json_atomic(state_path, state)
        return f"Field Crafter {APP_VERSION} application update finalized successfully."
    except Exception as exc:
        state["status"] = "cleanup_pending"
        state["cleanup_error"] = str(exc)
        state["cleanup_attempted_at_utc"] = _now_utc()
        try:
            _write_json_atomic(state_path, state)
        except Exception:
            pass
        return f"Field Crafter {APP_VERSION} started successfully; old-version backup cleanup is pending: {exc}"


def format_download_size(size_bytes: int) -> str:
    value = float(max(0, int(size_bytes)))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{size_bytes} B"
