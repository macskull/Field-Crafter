from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from .memory_profiles import (
    MemoryProfileError,
    bundled_memory_profile_path,
    default_user_memory_profile_path,
    load_profile_pack,
)
from .memory_update_crypto import verify as ed25519_verify
from .version import APP_VERSION


FIELD_CRAFTER_VERSION = APP_VERSION
UPDATE_SCHEMA_VERSION = 1
_ALLOWED_DOWNLOAD_HOSTS = {
    "raw.githubusercontent.com",
    "github.com",
    "objects.githubusercontent.com",
    "githubusercontent.com",
}


class MemoryProfileUpdateError(RuntimeError):
    pass


@dataclass(slots=True)
class MemoryProfileStatus:
    active_source: str
    active_pack_version: str
    bundled_pack_version: str
    downloaded_pack_version: str | None
    previous_pack_version: str | None
    rollback_available: bool
    current_path: str
    previous_path: str
    warning: str = ""


@dataclass(slots=True)
class MemoryProfileUpdateCandidate:
    current_pack_version: str
    pack_version: str
    min_field_crafter_version: str
    pack_path: Path
    work_dir: Path
    sha256: str
    manifest: dict[str, Any]


@dataclass(slots=True)
class MemoryProfileUpdateCheck:
    update_available: bool
    current_pack_version: str
    latest_pack_version: str
    message: str
    candidate: MemoryProfileUpdateCandidate | None = None


def _resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parents[2]


def bundled_update_config_path() -> Path:
    return _resource_root() / "data" / "memory_update_config.json"


def _memory_dir() -> Path:
    return default_user_memory_profile_path().parent


def previous_memory_profile_path() -> Path:
    return _memory_dir() / "memory_profiles.previous.json"


def update_state_path() -> Path:
    return _memory_dir() / "memory_update_state.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _version_tuple(value: str) -> tuple[int, ...]:
    out: list[int] = []
    for part in str(value or "").strip().split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        if not digits:
            break
        out.append(int(digits))
    return tuple(out)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonical_manifest_bytes(manifest_without_signature: dict[str, Any]) -> bytes:
    return json.dumps(
        manifest_without_signature,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def load_update_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path) if path else bundled_update_config_path()
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise MemoryProfileUpdateError(
            f"Could not read memory update configuration: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise MemoryProfileUpdateError("Memory update configuration is not a JSON object.")
    if int(value.get("schema_version") or 0) != UPDATE_SCHEMA_VERSION:
        raise MemoryProfileUpdateError(
            f"Unsupported memory update configuration schema: {value.get('schema_version')!r}"
        )
    url = str(value.get("manifest_url") or "").strip()
    if not url:
        raise MemoryProfileUpdateError("Memory update manifest URL is not configured.")
    _require_https_github_url(url, "manifest_url")
    try:
        public_key = base64.b64decode(str(value.get("public_key_ed25519") or ""), validate=True)
    except Exception as exc:
        raise MemoryProfileUpdateError("Memory update public key is not valid base64.") from exc
    if len(public_key) != 32:
        raise MemoryProfileUpdateError("Memory update public key must be 32 bytes.")
    return value


def _require_https_github_url(url: str, field: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        raise MemoryProfileUpdateError(f"{field} must use HTTPS.")
    host = (parsed.hostname or "").casefold()
    if host not in _ALLOWED_DOWNLOAD_HOSTS:
        raise MemoryProfileUpdateError(
            f"{field} host {host!r} is not an allowed GitHub download host."
        )


def verify_manifest_document(
    manifest: dict[str, Any],
    *,
    public_key_b64: str,
) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise MemoryProfileUpdateError("Memory update manifest is not a JSON object.")
    signature_text = manifest.get("signature")
    if not isinstance(signature_text, str) or not signature_text:
        raise MemoryProfileUpdateError("Memory update manifest is unsigned.")
    unsigned = dict(manifest)
    unsigned.pop("signature", None)

    required = {
        "schema_version",
        "channel",
        "pack_version",
        "min_field_crafter_version",
        "pack_url",
        "pack_sha256",
    }
    missing = sorted(required - set(unsigned))
    if missing:
        raise MemoryProfileUpdateError(
            "Memory update manifest is missing: " + ", ".join(missing)
        )
    if int(unsigned.get("schema_version") or 0) != UPDATE_SCHEMA_VERSION:
        raise MemoryProfileUpdateError(
            f"Unsupported memory update manifest schema: {unsigned.get('schema_version')!r}"
        )
    _require_https_github_url(str(unsigned["pack_url"]), "pack_url")
    digest = str(unsigned["pack_sha256"]).lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise MemoryProfileUpdateError("Memory update pack SHA-256 is invalid.")

    try:
        public_key = base64.b64decode(public_key_b64, validate=True)
        signature = base64.b64decode(signature_text, validate=True)
    except Exception as exc:
        raise MemoryProfileUpdateError("Memory update signature encoding is invalid.") from exc
    if len(public_key) != 32 or len(signature) != 64:
        raise MemoryProfileUpdateError("Memory update signature/key length is invalid.")
    if not ed25519_verify(
        public_key,
        _canonical_manifest_bytes(unsigned),
        signature,
    ):
        raise MemoryProfileUpdateError(
            "Memory update manifest signature verification failed."
        )
    return unsigned


def _read_pack_version(path: Path, source: str) -> str:
    version, _profiles = load_profile_pack(path, source=source)
    return version


def memory_profile_status() -> MemoryProfileStatus:
    bundled = bundled_memory_profile_path()
    current = default_user_memory_profile_path()
    previous = previous_memory_profile_path()
    warning = ""

    try:
        bundled_version = _read_pack_version(bundled, "bundled")
    except Exception as exc:
        bundled_version = "invalid"
        warning = str(exc)

    downloaded_version: str | None = None
    if current.exists():
        try:
            downloaded_version = _read_pack_version(current, "user")
        except Exception as exc:
            warning = (warning + "; " if warning else "") + str(exc)

    previous_version: str | None = None
    if previous.exists():
        try:
            previous_version = _read_pack_version(previous, "previous")
        except Exception as exc:
            warning = (warning + "; " if warning else "") + str(exc)

    if downloaded_version:
        active_source = "downloaded"
        active_version = downloaded_version
    else:
        active_source = "bundled"
        active_version = bundled_version

    return MemoryProfileStatus(
        active_source=active_source,
        active_pack_version=active_version,
        bundled_pack_version=bundled_version,
        downloaded_pack_version=downloaded_version,
        previous_pack_version=previous_version,
        rollback_available=bool(current.exists() or previous.exists()),
        current_path=str(current),
        previous_path=str(previous),
        warning=warning,
    )


def _download_bytes(
    url: str,
    *,
    max_bytes: int,
    timeout: float = 30.0,
) -> bytes:
    _require_https_github_url(url, "download URL")
    try:
        with requests.get(
            url,
            timeout=timeout,
            stream=True,
            headers={"User-Agent": f"FieldCrafter/{FIELD_CRAFTER_VERSION} memory-profile-updater"},
        ) as response:
            response.raise_for_status()
            _require_https_github_url(str(response.url), "redirected download URL")
            length = response.headers.get("Content-Length")
            if length:
                try:
                    if int(length) > max_bytes:
                        raise MemoryProfileUpdateError(
                            f"Memory update download is unexpectedly large ({int(length):,} bytes)."
                        )
                except ValueError:
                    pass
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise MemoryProfileUpdateError(
                        f"Memory update download exceeded the {max_bytes:,}-byte safety limit."
                    )
                chunks.append(chunk)
            return b"".join(chunks)
    except MemoryProfileUpdateError:
        raise
    except Exception as exc:
        raise MemoryProfileUpdateError(f"Memory update download failed: {exc}") from exc


def check_for_memory_profile_update() -> MemoryProfileUpdateCheck:
    config = load_update_config()
    status = memory_profile_status()
    max_bytes = int(config.get("max_download_bytes") or 2 * 1024 * 1024)
    if max_bytes <= 0 or max_bytes > 16 * 1024 * 1024:
        raise MemoryProfileUpdateError("Memory update maximum download size is invalid.")

    manifest_bytes = _download_bytes(
        str(config["manifest_url"]),
        max_bytes=min(max_bytes, 512 * 1024),
    )
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except Exception as exc:
        raise MemoryProfileUpdateError(
            f"Memory update manifest is not valid UTF-8 JSON: {exc}"
        ) from exc
    unsigned = verify_manifest_document(
        manifest,
        public_key_b64=str(config["public_key_ed25519"]),
    )
    if str(unsigned.get("channel")) != str(config.get("channel")):
        raise MemoryProfileUpdateError(
            f"Memory update channel mismatch: manifest={unsigned.get('channel')!r}, "
            f"client={config.get('channel')!r}."
        )

    latest = str(unsigned["pack_version"])
    current = status.active_pack_version
    minimum = str(unsigned["min_field_crafter_version"])
    if _version_tuple(minimum) > _version_tuple(FIELD_CRAFTER_VERSION):
        return MemoryProfileUpdateCheck(
            update_available=False,
            current_pack_version=current,
            latest_pack_version=latest,
            message=(
                f"Memory definitions {latest} require Field Crafter {minimum} or newer. "
                f"This installation is {FIELD_CRAFTER_VERSION}."
            ),
        )
    if _version_tuple(latest) <= _version_tuple(current):
        return MemoryProfileUpdateCheck(
            update_available=False,
            current_pack_version=current,
            latest_pack_version=latest,
            message=f"Memory definitions are up to date ({current}).",
        )

    pack_bytes = _download_bytes(str(unsigned["pack_url"]), max_bytes=max_bytes)
    actual_hash = _sha256_bytes(pack_bytes)
    expected_hash = str(unsigned["pack_sha256"]).lower()
    if actual_hash != expected_hash:
        raise MemoryProfileUpdateError(
            "Downloaded memory definition pack failed SHA-256 verification."
        )

    root = _memory_dir() / "candidates"
    root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"{latest.replace('.', '_')}_", dir=root))
    candidate_path = work / "memory_profiles.json"
    candidate_path.write_bytes(pack_bytes)

    try:
        pack_version, profiles = load_profile_pack(candidate_path, source="candidate")
    except MemoryProfileError:
        shutil.rmtree(work, ignore_errors=True)
        raise
    if pack_version != latest:
        shutil.rmtree(work, ignore_errors=True)
        raise MemoryProfileUpdateError(
            f"Manifest pack version {latest!r} does not match downloaded pack {pack_version!r}."
        )
    if not profiles:
        shutil.rmtree(work, ignore_errors=True)
        raise MemoryProfileUpdateError("Downloaded memory definition pack contains no profiles.")

    return MemoryProfileUpdateCheck(
        update_available=True,
        current_pack_version=current,
        latest_pack_version=latest,
        message=f"Memory definitions {latest} are available.",
        candidate=MemoryProfileUpdateCandidate(
            current_pack_version=current,
            pack_version=latest,
            min_field_crafter_version=minimum,
            pack_path=candidate_path,
            work_dir=work,
            sha256=actual_hash,
            manifest=manifest,
        ),
    )


def reject_memory_profile_update(candidate: MemoryProfileUpdateCandidate | None) -> None:
    if candidate is None:
        return
    shutil.rmtree(candidate.work_dir, ignore_errors=True)


def _write_state(value: dict[str, Any]) -> None:
    path = update_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.new")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def install_memory_profile_update(
    candidate: MemoryProfileUpdateCandidate,
    *,
    db_path: str | Path,
    pid: int,
    alias_path: str | Path | None = None,
) -> dict[str, Any]:
    # Import lazily to keep profile downloading portable on non-Windows systems.
    from .game_memory import validate_memory_profile_pack_live

    if not candidate.pack_path.exists():
        raise MemoryProfileUpdateError("Downloaded memory update candidate no longer exists.")

    live = validate_memory_profile_pack_live(
        db_path,
        int(pid),
        candidate.pack_path,
        alias_path=alias_path,
    )

    current = default_user_memory_profile_path()
    previous = previous_memory_profile_path()
    current.parent.mkdir(parents=True, exist_ok=True)
    previous.unlink(missing_ok=True)
    if current.exists():
        shutil.copy2(current, previous)

    staged = current.with_suffix(".json.new")
    shutil.copy2(candidate.pack_path, staged)
    os.replace(staged, current)

    _write_state({
        "schema_version": 1,
        "installed_at_utc": _utc_now(),
        "installed_pack_version": candidate.pack_version,
        "previous_pack_version": candidate.current_pack_version,
        "pack_sha256": candidate.sha256,
        "manifest_url": load_update_config()["manifest_url"],
        "live_validation": live,
    })
    reject_memory_profile_update(candidate)
    return live


def rollback_memory_profile_update(
    *,
    db_path: str | Path,
    pid: int,
    alias_path: str | Path | None = None,
) -> dict[str, Any]:
    """Toggle between current downloaded definitions and the one-level predecessor.

    If no downloaded predecessor existed, rolling back removes the downloaded pack
    and returns to the bundled definitions. The removed pack becomes `previous`, so
    another explicit rollback action can restore it.
    """
    from .game_memory import validate_memory_profile_pack_live

    current = default_user_memory_profile_path()
    previous = previous_memory_profile_path()
    current.parent.mkdir(parents=True, exist_ok=True)

    if not current.exists() and not previous.exists():
        raise MemoryProfileUpdateError("No downloaded memory definition state is available to roll back.")

    live: dict[str, Any] = {}
    action: str

    if current.exists() and previous.exists():
        # Validate the target before changing the active file.
        live = validate_memory_profile_pack_live(
            db_path, int(pid), previous, alias_path=alias_path
        )
        swap = current.with_suffix(".swap")
        os.replace(current, swap)
        try:
            os.replace(previous, current)
            os.replace(swap, previous)
        except Exception:
            if swap.exists() and not current.exists():
                os.replace(swap, current)
            raise
        action = "swapped_to_previous"
    elif current.exists():
        # The predecessor was the bundled profile pack.
        os.replace(current, previous)
        action = "returned_to_bundled"
    else:
        # Re-enable a previously rolled-back downloaded pack only after it validates.
        live = validate_memory_profile_pack_live(
            db_path, int(pid), previous, alias_path=alias_path
        )
        os.replace(previous, current)
        action = "restored_downloaded"

    status = memory_profile_status()
    _write_state({
        "schema_version": 1,
        "rolled_back_at_utc": _utc_now(),
        "action": action,
        "active_source": status.active_source,
        "active_pack_version": status.active_pack_version,
        "live_validation": live,
    })
    return {
        "action": action,
        "active_source": status.active_source,
        "active_pack_version": status.active_pack_version,
        "live_validation": live,
    }
