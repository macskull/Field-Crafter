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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# FIELD_CRAFTER_MEMORY_CHANNEL_PUBLICATION_V1


def find_root(start: Path) -> Path:
    start = start.resolve()
    for root in [start] + list(start.parents):
        if (
            (root / "src" / "hc_recipe_db" / "version.py").is_file()
            and (root / "maintainer" / "publish_memory_profile_update.py").is_file()
            and (root / "data" / "memory_profiles.json").is_file()
        ):
            return root
    raise RuntimeError("Could not find the Field Crafter 1.16 source root.")


def diagnostics_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) / "FieldCrafter" if base else Path.home() / ".field_crafter"
    out = root / "diagnostics"
    out.mkdir(parents=True, exist_ok=True)
    return out


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_manifest_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{path.name} is not a JSON object.")
    return value


def release_python(root: Path) -> Path:
    candidate = (
        root / ".release_venv" / "Scripts" / "python.exe"
        if os.name == "nt"
        else root / ".release_venv" / "bin" / "python"
    )
    if candidate.is_file():
        return candidate
    return Path(sys.executable)


def run_publisher(
    root: Path,
    *,
    private_key: Path,
    output_dir: Path,
    channel: str,
    raw_base_url: str,
) -> dict[str, Any]:
    command = [
        str(release_python(root)),
        str(root / "maintainer" / "publish_memory_profile_update.py"),
        "--private-key",
        str(private_key),
        "--pack",
        str(root / "data" / "memory_profiles.json"),
        "--output-dir",
        str(output_dir),
        "--min-field-crafter-version",
        "1.16",
        "--channel",
        channel,
        "--raw-base-url",
        raw_base_url,
    ]
    completed = subprocess.run(
        command,
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    return {
        "passed": completed.returncode == 0,
        "returncode": completed.returncode,
        "command": [
            "<private-key-path>" if item == str(private_key) else item
            for item in command
        ],
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare and independently verify the signed Field Crafter memory "
            "definition files that will be committed to main/memory."
        )
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--private-key", required=True)
    parser.add_argument(
        "--output-dir",
        default="memory_publish",
        help="Staging directory relative to the source root unless absolute.",
    )
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    result_path = (
        diagnostics_dir()
        / f"field_crafter_memory_channel_publication_{stamp}.json"
    )

    result: dict[str, Any] = {
        "passed": False,
        "test_version": "1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    try:
        root = find_root(Path(args.root))
        private_key = Path(args.private_key).expanduser().resolve()
        if not private_key.is_file():
            raise RuntimeError(
                "The external signing-key file does not exist. "
                "Keep it outside the repository and pass its full path."
            )

        output_dir = Path(args.output_dir)
        if not output_dir.is_absolute():
            output_dir = (root / output_dir).resolve()
        else:
            output_dir = output_dir.resolve()

        # Refuse to publish into the source data or maintainer trees.
        for forbidden in (
            (root / "data").resolve(),
            (root / "src").resolve(),
            (root / "maintainer").resolve(),
        ):
            try:
                output_dir.relative_to(forbidden)
            except ValueError:
                pass
            else:
                raise RuntimeError(
                    f"Publication output must not be inside {forbidden}."
                )

        summary = load_json(root / "data" / "release_data_summary.json")
        info = load_json(root / "data" / "release_database_info.json")
        config = load_json(root / "data" / "memory_update_config.json")
        pack = load_json(root / "data" / "memory_profiles.json")

        if summary.get("release_version") != "1.16":
            raise RuntimeError("Release data is not for Field Crafter 1.16.")
        if not summary.get("release_data_ready") or not summary.get("redistribution_ready"):
            raise RuntimeError(
                "Release data is not marked ready/redistribution-ready."
            )
        if not info.get("redistribution_ready"):
            raise RuntimeError(
                "release_database_info.json is not redistribution-ready."
            )

        pack_version = str(pack.get("pack_version") or "")
        if not pack_version:
            raise RuntimeError("memory_profiles.json has no pack_version.")
        if summary.get("memory_profile_pack_version") != pack_version:
            raise RuntimeError(
                "Release metadata memory pack version does not match memory_profiles.json."
            )

        source_pack_hash = sha256(root / "data" / "memory_profiles.json")
        expected_hash = (
            summary.get("sha256", {}).get("memory_profiles.json")
            if isinstance(summary.get("sha256"), dict)
            else None
        )
        if source_pack_hash != expected_hash:
            raise RuntimeError(
                "memory_profiles.json no longer matches the validated release metadata."
            )

        channel = str(config.get("channel") or "")
        manifest_url = str(config.get("manifest_url") or "")
        configured_public_key = str(config.get("public_key_ed25519") or "")
        if not channel or not manifest_url or not configured_public_key:
            raise RuntimeError(
                "memory_update_config.json is missing channel, manifest URL, "
                "or configured public key."
            )

        if not manifest_url.endswith("/manifest.json"):
            raise RuntimeError(
                "Configured manifest URL does not end in /manifest.json."
            )
        raw_base_url = manifest_url[: -len("/manifest.json")]

        # Inspect only the public-key field of the external key file before
        # invoking the publisher. Never copy/log the private seed.
        key_meta = load_json(private_key)
        if key_meta.get("format") != "field-crafter-ed25519-seed-v1":
            raise RuntimeError("Unrecognized Field Crafter signing-key format.")
        key_public = str(key_meta.get("public_key_base64") or "")
        if key_public != configured_public_key:
            raise RuntimeError(
                "The signing key's public key does not match "
                "data/memory_update_config.json. Refusing to publish."
            )

        # Start from a clean staging directory, but only after all metadata/key
        # consistency checks above have passed.
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True)

        publish = run_publisher(
            root,
            private_key=private_key,
            output_dir=output_dir,
            channel=channel,
            raw_base_url=raw_base_url,
        )
        result["publisher"] = {
            "passed": publish["passed"],
            "returncode": publish["returncode"],
            "command": publish["command"],
            "stdout_tail": "\n".join(publish["stdout"].splitlines()[-40:]),
            "stderr_tail": "\n".join(publish["stderr"].splitlines()[-40:]),
        }
        if not publish["passed"]:
            raise RuntimeError(
                "Maintainer publisher failed: "
                + (publish["stderr"] or publish["stdout"] or "no output")[-4000:]
            )

        expected_filename = (
            f"field-crafter-memory-definitions-{pack_version}.json"
        )
        published_pack = output_dir / expected_filename
        manifest_path = output_dir / "manifest.json"

        files = sorted(
            p.name
            for p in output_dir.iterdir()
            if p.is_file()
        )
        if files != sorted([expected_filename, "manifest.json"]):
            raise RuntimeError(
                "Publication directory must contain exactly the signed manifest "
                f"and versioned memory pack. Found: {files}"
            )

        published_pack_hash = sha256(published_pack)
        if published_pack_hash != source_pack_hash:
            raise RuntimeError(
                "Published memory pack differs from the validated bundled pack."
            )

        manifest = load_json(manifest_path)
        signature_b64 = manifest.get("signature")
        if not isinstance(signature_b64, str) or not signature_b64:
            raise RuntimeError("Published manifest has no signature.")

        expected_manifest_fields = {
            "schema_version": 1,
            "channel": channel,
            "pack_version": pack_version,
            "min_field_crafter_version": "1.16",
            "pack_url": raw_base_url.rstrip("/") + "/" + expected_filename,
            "pack_sha256": source_pack_hash,
        }
        unsigned = {
            key: manifest.get(key)
            for key in expected_manifest_fields
        }
        if unsigned != expected_manifest_fields:
            raise RuntimeError(
                "Published manifest fields do not match the validated release/config."
            )

        if set(manifest) != set(expected_manifest_fields) | {"signature"}:
            raise RuntimeError(
                "Published manifest contains unexpected or missing fields."
            )

        # Independently verify the signature with the exact public key embedded
        # in the app update config.
        src = root / "src"
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        from hc_recipe_db.memory_update_crypto import verify

        public_key = base64.b64decode(
            configured_public_key,
            validate=True,
        )
        signature = base64.b64decode(
            signature_b64,
            validate=True,
        )
        if not verify(
            public_key,
            canonical_manifest_bytes(unsigned),
            signature,
        ):
            raise RuntimeError(
                "Independent signature verification against the app's configured "
                "public key failed."
            )

        # Secret-hygiene pass over the publication directory.
        for path in output_dir.rglob("*"):
            if not path.is_file():
                continue
            lowered = path.name.casefold()
            if (
                "signing_key" in lowered
                or "private_key" in lowered
                or "private-key" in lowered
            ):
                raise RuntimeError(
                    f"Secret-looking filename appeared in publication output: {path.name}"
                )
            text = path.read_text(encoding="utf-8", errors="ignore")
            if '"seed_base64"' in text or "BEGIN PRIVATE KEY" in text:
                raise RuntimeError(
                    f"Private signing material appeared in publication output: {path.name}"
                )

        result.update(
            {
                "passed": True,
                "release_version": "1.16",
                "channel": channel,
                "pack_version": pack_version,
                "output_dir": str(output_dir),
                "files": files,
                "pack_sha256": source_pack_hash,
                "manifest_url": manifest_url,
                "pack_url": expected_manifest_fields["pack_url"],
                "signature_verified_against_app_config": True,
                "published_pack_matches_validated_bundled_pack": True,
                "private_key_path_recorded": False,
                "private_key_material_copied": False,
                "next_step": (
                    "Commit the two files from the publication directory under "
                    "main/memory, push main, then verify both raw GitHub URLs and "
                    "Field Crafter's manual memory-update check."
                ),
            }
        )

    except Exception as exc:
        result["error"] = str(exc)

    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"{'PASS' if result.get('passed') else 'FAIL'}: "
        f"memory-channel publication result written to {result_path}"
    )
    if result.get("passed"):
        print(f"Publication directory: {result['output_dir']}")
        print(
            "Only the versioned pack and manifest.json should be committed "
            "under the repository's memory/ directory."
        )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
