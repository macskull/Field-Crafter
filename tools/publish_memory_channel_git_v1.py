#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# FIELD_CRAFTER_MEMORY_CHANNEL_GIT_PUBLISH_V1
# FIELD_CRAFTER_MEMORY_CHANNEL_GIT_PUBLISH_V1_1
# FIELD_CRAFTER_MEMORY_CHANNEL_GIT_PUBLISH_V1_1_1


def find_root(start: Path) -> Path:
    start = start.resolve()
    for root in [start] + list(start.parents):
        if (
            (root / ".git").exists()
            and (root / "data" / "memory_profiles.json").is_file()
            and (root / "memory_publish" / "manifest.json").is_file()
        ):
            return root
    raise RuntimeError("Could not find the prepared Field Crafter git source root.")


def diagnostics_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) / "FieldCrafter" if base else Path.home() / ".field_crafter"
    out = root / "diagnostics"
    out.mkdir(parents=True, exist_ok=True)
    return out


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{path.name} is not a JSON object.")
    return value


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_manifest_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def run(
    command: list[str],
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        capture_output=True,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {' '.join(command)}\n"
            + (completed.stderr or completed.stdout or "no output")[-4000:]
        )
    return completed


def run_streaming(command: list[str], *, cwd: Path) -> int:
    proc = subprocess.Popen(
        command,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
    return proc.wait()


def git_blob_bytes(worktree: Path, rev_path: str) -> bytes:
    completed = subprocess.run(
        ["git", "show", rev_path],
        cwd=str(worktree),
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Could not read staged/committed Git blob "
            f"{rev_path}: {completed.stderr.decode(errors='replace')[-2000:]}"
        )
    return completed.stdout


def write_memory_attributes(memory_dir: Path) -> Path:
    path = memory_dir / ".gitattributes"
    path.write_text(
        "# Preserve signed memory-update JSON byte-for-byte.\n"
        "*.json -text\n",
        encoding="ascii",
        newline="\n",
    )
    return path


def fetch(url: str, *, attempts: int = 45, delay: float = 2.0) -> bytes:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Field-Crafter-release-verifier/1.16"},
            )
            with urllib.request.urlopen(req, timeout=20) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status} from {url}")
                return response.read()
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(delay)
    raise RuntimeError(
        f"Could not retrieve {url} after {attempts} attempts: {last_error}"
    )


def fetch_until_exact(
    url: str,
    expected: bytes,
    *,
    attempts: int = 60,
    delay: float = 2.0,
) -> bytes:
    last_hash: str | None = None
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Field-Crafter-release-verifier/1.16",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
            with urllib.request.urlopen(req, timeout=20) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status} from {url}")
                value = response.read()
            last_hash = sha256_bytes(value)
            if value == expected:
                if attempt:
                    print(
                        f"Live raw file matched after {attempt + 1} checks: {url}",
                        flush=True,
                    )
                return value
        except Exception as exc:
            last_error = exc

        if attempt + 1 < attempts:
            time.sleep(delay)

    detail = (
        f"last SHA-256 {last_hash}"
        if last_hash is not None
        else f"last error {last_error}"
    )
    raise RuntimeError(
        f"Live raw file did not converge to the expected bytes after "
        f"{attempts * delay:.0f} seconds: {url} ({detail})"
    )


def verify_live_channel(root: Path) -> dict[str, Any]:
    config = load_json(root / "data" / "memory_update_config.json")
    bundled_pack = root / "data" / "memory_profiles.json"
    bundled = bundled_pack.read_bytes()
    bundled_hash = sha256_bytes(bundled)

    manifest_url = str(config.get("manifest_url") or "")
    configured_public_key = str(config.get("public_key_ed25519") or "")
    channel = str(config.get("channel") or "")
    if not manifest_url or not configured_public_key or not channel:
        raise RuntimeError("memory_update_config.json is incomplete.")

    manifest_bytes = fetch(manifest_url)
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if not isinstance(manifest, dict):
        raise RuntimeError("Live manifest is not a JSON object.")

    pack_url = str(manifest.get("pack_url") or "")
    if not pack_url:
        raise RuntimeError("Live manifest has no pack_url.")
    # The branch-based raw URL can briefly serve a previous GitHub CDN object
    # immediately after a push. Poll until the exact bytes become visible rather
    # than treating the first stale-but-successful response as a failed release.
    pack_bytes = fetch_until_exact(
        pack_url,
        bundled,
    )

    if sha256_bytes(pack_bytes) != bundled_hash:
        raise RuntimeError(
            "Live memory pack SHA-256 does not match the bundled validated "
            "memory_profiles.json."
        )

    expected_unsigned = {
        "schema_version": 1,
        "channel": channel,
        "pack_version": load_json(bundled_pack).get("pack_version"),
        "min_field_crafter_version": "1.16",
        "pack_url": pack_url,
        "pack_sha256": bundled_hash,
    }
    unsigned = {key: manifest.get(key) for key in expected_unsigned}
    if unsigned != expected_unsigned:
        raise RuntimeError(
            "Live manifest fields do not match the validated release/config."
        )

    signature_b64 = manifest.get("signature")
    if not isinstance(signature_b64, str) or not signature_b64:
        raise RuntimeError("Live manifest has no signature.")

    src = root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from hc_recipe_db.memory_update_crypto import verify

    public_key = base64.b64decode(configured_public_key, validate=True)
    signature = base64.b64decode(signature_b64, validate=True)
    if not verify(
        public_key,
        canonical_manifest_bytes(unsigned),
        signature,
    ):
        raise RuntimeError(
            "Live manifest signature failed verification against the app public key."
        )

    return {
        "manifest_url": manifest_url,
        "pack_url": pack_url,
        "pack_version": expected_unsigned["pack_version"],
        "pack_sha256": bundled_hash,
        "channel": channel,
        "live_pack_matches_bundled_pack": True,
        "signature_verified_against_app_config": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--push",
        action="store_true",
        help="Actually push the verified memory publication commit to origin/main.",
    )
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    result_path = (
        diagnostics_dir()
        / f"field_crafter_memory_channel_git_publish_{stamp}.json"
    )
    result: dict[str, Any] = {
        "passed": False,
        "test_version": "1.1.1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "push_requested": bool(args.push),
    }

    worktree: Path | None = None

    try:
        root = find_root(Path(args.root))
        publication = root / "memory_publish"
        source_manifest = publication / "manifest.json"

        summary = load_json(root / "data" / "release_data_summary.json")
        pack = load_json(root / "data" / "memory_profiles.json")
        config = load_json(root / "data" / "memory_update_config.json")

        if not summary.get("redistribution_ready"):
            raise RuntimeError("Release data is no longer redistribution-ready.")
        pack_version = str(pack.get("pack_version") or "")
        expected_pack_name = (
            f"field-crafter-memory-definitions-{pack_version}.json"
        )
        source_pack = publication / expected_pack_name

        files = sorted(p.name for p in publication.iterdir() if p.is_file())
        if files != sorted(["manifest.json", expected_pack_name]):
            raise RuntimeError(
                "memory_publish must contain exactly manifest.json and the "
                f"versioned pack; found {files}"
            )

        bundled_hash = sha256(root / "data" / "memory_profiles.json")
        if sha256(source_pack) != bundled_hash:
            raise RuntimeError(
                "Prepared publication pack no longer matches bundled memory_profiles.json."
            )
        expected_release_hash = summary.get("sha256", {}).get("memory_profiles.json")
        if bundled_hash != expected_release_hash:
            raise RuntimeError(
                "Bundled memory_profiles.json no longer matches release metadata."
            )

        prepared_manifest = load_json(source_manifest)
        if prepared_manifest.get("pack_sha256") != bundled_hash:
            raise RuntimeError("Prepared manifest pack hash is stale.")
        if prepared_manifest.get("channel") != config.get("channel"):
            raise RuntimeError("Prepared manifest channel differs from app config.")

        remote = run(
            ["git", "remote", "get-url", "origin"],
            cwd=root,
        ).stdout.strip()
        if "Field-Crafter" not in remote or "macskull" not in remote:
            raise RuntimeError(
                f"Unexpected origin remote; refusing to publish: {remote}"
            )

        print("Fetching origin/main...", flush=True)
        run(["git", "fetch", "origin", "main"], cwd=root)

        worktree = Path(
            tempfile.mkdtemp(
                prefix="field_crafter_memory_main_",
                dir=str(diagnostics_dir()),
            )
        )
        # git worktree requires a non-existing target.
        worktree.rmdir()

        run(
            [
                "git",
                "worktree",
                "add",
                "--detach",
                str(worktree),
                "origin/main",
            ],
            cwd=root,
        )

        memory_dir = worktree / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)

        # Git for Windows commonly has core.autocrlf enabled. Signed update
        # packs are SHA-256 bound to their exact bytes, so text normalization
        # would invalidate an otherwise identical JSON document. Store all
        # memory-channel JSON byte-for-byte.
        attributes_path = write_memory_attributes(memory_dir)

        shutil.copyfile(source_manifest, memory_dir / "manifest.json")
        shutil.copyfile(source_pack, memory_dir / expected_pack_name)

        run(
            [
                "git",
                "add",
                "--",
                "memory/.gitattributes",
                "memory/manifest.json",
                f"memory/{expected_pack_name}",
            ],
            cwd=worktree,
        )

        staged = [
            line.strip()
            for line in run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=worktree,
            ).stdout.splitlines()
            if line.strip()
        ]
        allowed = sorted(
            [
                "memory/.gitattributes",
                "memory/manifest.json",
                f"memory/{expected_pack_name}",
            ]
        )
        if sorted(staged) not in ([], allowed):
            raise RuntimeError(
                f"Unexpected staged paths in publication worktree: {staged}"
            )

        if staged:
            # Verify what Git will actually publish, after all clean filters and
            # line-ending rules. This catches CRLF/LF normalization before push.
            staged_pack = git_blob_bytes(
                worktree,
                f":memory/{expected_pack_name}",
            )
            if staged_pack != source_pack.read_bytes():
                raise RuntimeError(
                    "Git's staged memory pack is not byte-for-byte identical to "
                    "the validated publication pack. Refusing to commit."
                )
            if sha256_bytes(staged_pack) != bundled_hash:
                raise RuntimeError(
                    "Git's staged memory-pack SHA-256 differs from the release hash."
                )

            run(
                [
                    "git",
                    "commit",
                    "-m",
                    f"Publish memory definitions {pack_version}",
                ],
                cwd=worktree,
            )
            commit = run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree,
            ).stdout.strip()

            committed_pack = git_blob_bytes(
                worktree,
                f"HEAD:memory/{expected_pack_name}",
            )
            if committed_pack != source_pack.read_bytes():
                raise RuntimeError(
                    "Committed memory pack is not byte-for-byte identical to "
                    "the validated publication pack."
                )
            publication_changed = True
        else:
            commit = run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree,
            ).stdout.strip()
            publication_changed = False

        result.update(
            {
                "origin": remote,
                "pack_version": pack_version,
                "pack_sha256": bundled_hash,
                "staged_paths": staged,
                "publication_changed": publication_changed,
                "git_byte_preservation_rule": "memory/*.json -text",
                "committed_pack_matches_validated_bytes": True,
                "commit": commit,
            }
        )

        if not args.push:
            result.update(
                {
                    "passed": True,
                    "pushed": False,
                    "live_verified": False,
                    "next_step": (
                        "Re-run this command with --push to publish to origin/main."
                    ),
                }
            )
        else:
            print(
                f"Pushing verified memory publication {commit[:12]} to origin/main...",
                flush=True,
            )
            rc = run_streaming(
                ["git", "push", "origin", "HEAD:main"],
                cwd=worktree,
            )
            if rc != 0:
                raise RuntimeError(
                    f"git push origin HEAD:main failed with exit code {rc}."
                )

            print("Verifying live raw GitHub memory channel...", flush=True)
            live = verify_live_channel(root)
            result.update(
                {
                    "passed": True,
                    "pushed": True,
                    "live_verified": True,
                    "live": live,
                    "next_step": (
                        "Exercise Field Crafter's manual 'Check for memory updates' "
                        "path against this live channel."
                    ),
                }
            )

    except Exception as exc:
        result["error"] = str(exc)

    finally:
        if worktree is not None:
            try:
                # Remove through git first so repository worktree metadata is cleaned.
                root_for_cleanup = locals().get("root")
                if isinstance(root_for_cleanup, Path):
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(worktree)],
                        cwd=str(root_for_cleanup),
                        capture_output=True,
                        text=True,
                    )
            except Exception:
                pass
            if worktree.exists():
                shutil.rmtree(worktree, ignore_errors=True)

        result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    print(
        f"{'PASS' if result.get('passed') else 'FAIL'}: "
        f"memory Git publication result written to {result_path}"
    )
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
