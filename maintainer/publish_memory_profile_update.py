from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hc_recipe_db.memory_profiles import load_profile_pack
from hc_recipe_db.memory_update_crypto import public_key_from_seed, sign
from hc_recipe_db.version import RELEASE_VERSION


DEFAULT_RAW_BASE = "https://raw.githubusercontent.com/macskull/Field-Crafter/main/memory"


def canonical_manifest_bytes(value: dict) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def load_private_seed(path: Path) -> bytes:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("format") != "field-crafter-ed25519-seed-v1":
        raise RuntimeError("Unrecognized Field Crafter signing-key file.")
    seed = base64.b64decode(value["seed_base64"], validate=True)
    if len(seed) != 32:
        raise RuntimeError("Signing seed is not 32 bytes.")
    expected_public = base64.b64decode(value["public_key_base64"], validate=True)
    actual_public = public_key_from_seed(seed)
    if actual_public != expected_public:
        raise RuntimeError("Signing key's stored public key does not match its private seed.")
    return seed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a signed Field Crafter memory-definition GitHub update."
    )
    parser.add_argument("--private-key", required=True)
    parser.add_argument("--pack", default=str(ROOT / "data" / "memory_profiles.json"))
    parser.add_argument("--output-dir", default=str(ROOT / "memory_publish"))
    parser.add_argument("--min-field-crafter-version", default=RELEASE_VERSION)
    parser.add_argument("--channel", default="public-test")
    parser.add_argument("--raw-base-url", default=DEFAULT_RAW_BASE)
    args = parser.parse_args()

    try:
        pack = Path(args.pack).resolve()
        out = Path(args.output_dir).resolve()
        seed = load_private_seed(Path(args.private_key).resolve())
        pack_version, profiles = load_profile_pack(pack, source="publish")
        if not profiles:
            raise RuntimeError("Memory profile pack contains no profiles.")

        out.mkdir(parents=True, exist_ok=True)
        filename = f"field-crafter-memory-definitions-{pack_version}.json"
        published_pack = out / filename
        shutil.copy2(pack, published_pack)
        pack_sha = hashlib.sha256(published_pack.read_bytes()).hexdigest()

        unsigned = {
            "schema_version": 1,
            "channel": args.channel,
            "pack_version": pack_version,
            "min_field_crafter_version": args.min_field_crafter_version,
            "pack_url": args.raw_base_url.rstrip("/") + "/" + filename,
            "pack_sha256": pack_sha,
        }
        signature = sign(seed, canonical_manifest_bytes(unsigned))
        manifest = dict(unsigned)
        manifest["signature"] = base64.b64encode(signature).decode("ascii")
        manifest_path = out / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        print("PASS: signed memory update prepared")
        print(f"Pack: {published_pack}")
        print(f"Manifest: {manifest_path}")
        print(f"Pack SHA-256: {pack_sha}")
        print("Commit both files under the repository's memory/ directory.")
        return 0
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
