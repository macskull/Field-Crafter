#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hc_recipe_db.memory_update_crypto import public_key_from_seed, sign  # noqa: E402


KEY_FORMAT = "field-crafter-ed25519-seed-v1"
SCHEMA_VERSION = 1


def _load_key(path: Path) -> tuple[bytes, bytes]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("format") != KEY_FORMAT:
        raise RuntimeError(f"Unsupported signing-key format: {doc.get('format')!r}")
    try:
        seed = base64.b64decode(str(doc["seed_base64"]), validate=True)
        recorded_public = base64.b64decode(str(doc["public_key_base64"]), validate=True)
    except Exception as exc:
        raise RuntimeError("Signing-key file contains invalid base64 material.") from exc
    if len(seed) != 32 or len(recorded_public) != 32:
        raise RuntimeError("Application-update signing key must contain a 32-byte seed and public key.")
    derived = public_key_from_seed(seed)
    if derived != recorded_public:
        raise RuntimeError("Application-update signing key public key does not match its private seed.")
    return seed, derived


def _canonical_bytes(doc: dict[str, Any]) -> bytes:
    body = {key: value for key, value in doc.items() if key != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_release_manifest(dist: Path) -> dict[str, Any]:
    path = dist / "RELEASE_MANIFEST.json"
    if not path.is_file():
        raise RuntimeError(f"Release manifest was not found: {path}")
    doc = json.loads(path.read_text(encoding="utf-8-sig"))
    version = str(doc.get("field_crafter_version") or "").strip()
    if not version:
        raise RuntimeError("RELEASE_MANIFEST.json is missing field_crafter_version.")
    artifacts = doc.get("artifacts")
    if not isinstance(artifacts, list):
        raise RuntimeError("RELEASE_MANIFEST.json artifacts must be a list.")
    return doc


def _artifact_by_suffix(release: dict[str, Any], suffix: str) -> dict[str, Any]:
    matches = [
        item for item in release.get("artifacts", [])
        if isinstance(item, dict) and str(item.get("file") or "").endswith(suffix)
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one release artifact ending in {suffix!r}; found {len(matches)}.")
    item = matches[0]
    sha = str(item.get("sha256") or "").lower()
    size = int(item.get("bytes") or 0)
    if len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha) or size <= 0:
        raise RuntimeError(f"Release artifact metadata is invalid for {item.get('file')!r}.")
    return item


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create the signed GitHub application-update manifest for a validated Field Crafter release."
    )
    parser.add_argument("--private-key", required=True, help="Path to the private application-update Ed25519 key JSON")
    parser.add_argument("--dist", default=str(ROOT / "dist"), help="Release dist directory containing RELEASE_MANIFEST.json")
    parser.add_argument("--output-dir", default=str(ROOT / "application_update_publish"))
    parser.add_argument("--channel", default="public-test")
    parser.add_argument("--minimum-updater-version", default="1.16.1")
    parser.add_argument("--repository", default="macskull/Field-Crafter")
    parser.add_argument("--summary", default="")
    args = parser.parse_args()

    dist = Path(args.dist).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    seed, public_key = _load_key(Path(args.private_key).expanduser().resolve())
    release = _load_release_manifest(dist)
    version = str(release["field_crafter_version"])
    exe = _artifact_by_suffix(release, ".exe")
    pyzip = _artifact_by_suffix(release, "_Python.zip")

    # Refuse to sign metadata for artifacts that are not actually present with the
    # exact sizes recorded by the already-validated release build.
    for item in (exe, pyzip):
        artifact_path = dist / str(item["file"])
        if not artifact_path.is_file():
            raise RuntimeError(f"Release artifact is missing: {artifact_path}")
        if artifact_path.stat().st_size != int(item["bytes"]):
            raise RuntimeError(f"Release artifact size changed after release validation: {artifact_path.name}")
        actual_sha = _sha256_file(artifact_path)
        if actual_sha != str(item["sha256"]).lower():
            raise RuntimeError(
                f"Release artifact SHA-256 changed after release validation: {artifact_path.name}"
            )

    repo = str(args.repository).strip().strip("/")
    if "/" not in repo:
        raise RuntimeError("--repository must be in owner/name form.")
    tag = f"v{version}"
    release_base = f"https://github.com/{repo}/releases/download/{tag}"
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "channel": str(args.channel),
        "version": version,
        "minimum_updater_version": str(args.minimum_updater_version),
        "release_url": f"https://github.com/{repo}/releases/tag/{tag}",
        "summary": str(args.summary or ""),
        "artifacts": {
            "exe": {
                "url": f"{release_base}/{exe['file']}",
                "sha256": str(exe["sha256"]).lower(),
                "bytes": int(exe["bytes"]),
            },
            "python": {
                "url": f"{release_base}/{pyzip['file']}",
                "sha256": str(pyzip["sha256"]).lower(),
                "bytes": int(pyzip["bytes"]),
            },
        },
    }
    doc["signature"] = base64.b64encode(sign(seed, _canonical_bytes(doc))).decode("ascii")

    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    public_path = output_dir / "application_update_public_key.txt"
    public_path.write_text(base64.b64encode(public_key).decode("ascii") + "\n", encoding="ascii")

    print("PASS: signed application update manifest prepared")
    print(f"Version:  {version}")
    print(f"Manifest: {manifest_path}")
    print(f"EXE:      {exe['file']} ({int(exe['bytes']):,} bytes)")
    print(f"Python:   {pyzip['file']} ({int(pyzip['bytes']):,} bytes)")
    print("Commit manifest.json as updates/manifest.json only after the matching GitHub release assets are published.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
