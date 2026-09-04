#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import tempfile
import zipfile
from pathlib import Path

from hc_recipe_db.application_updates import (
    ApplicationUpdateError,
    _canonical_manifest_bytes,
    _is_newer_version,
    _safe_extract_python_release,
    _version_at_least,
    check_for_application_update,
    verify_application_update_manifest,
)
from hc_recipe_db.memory_update_crypto import public_key_from_seed, sign


def _signed_manifest(seed: bytes, *, version: str = "9.9.9") -> dict:
    doc = {
        "schema_version": 1,
        "channel": "test",
        "version": version,
        "minimum_updater_version": "1.16.1",
        "release_url": f"https://github.com/macskull/Field-Crafter/releases/tag/v{version}",
        "summary": "Updater regression fixture",
        "artifacts": {
            "exe": {
                "url": f"https://github.com/macskull/Field-Crafter/releases/download/v{version}/Field_Crafter_{version}.exe",
                "sha256": "1" * 64,
                "bytes": 123456,
            },
            "python": {
                "url": f"https://github.com/macskull/Field-Crafter/releases/download/v{version}/Field_Crafter_{version}_Python.zip",
                "sha256": "2" * 64,
                "bytes": 234567,
            },
        },
    }
    doc["signature"] = base64.b64encode(sign(seed, _canonical_manifest_bytes(doc))).decode("ascii")
    return doc


def test_manifest_signature() -> None:
    seed = bytes(range(32))
    public = public_key_from_seed(seed)
    config = {
        "schema_version": 1,
        "channel": "test",
        "manifest_url": "https://raw.githubusercontent.com/macskull/Field-Crafter/main/updates/manifest.json",
        "public_key_ed25519": base64.b64encode(public).decode("ascii"),
    }
    manifest = _signed_manifest(seed)
    verify_application_update_manifest(manifest, config=config)

    tampered = json.loads(json.dumps(manifest))
    tampered["version"] = "10.0.0"
    try:
        verify_application_update_manifest(tampered, config=config)
    except ApplicationUpdateError as exc:
        assert "signature" in str(exc).casefold()
    else:
        raise AssertionError("Tampered application-update manifest was accepted")


def test_version_comparison() -> None:
    assert _is_newer_version("1.16.1", "1.16")
    assert _is_newer_version("1.17", "1.16.9")
    assert not _is_newer_version("1.16.1", "1.16.1")
    assert _version_at_least("1.16.1", "1.16.1")
    assert _version_at_least("1.17", "1.16.1")
    assert not _version_at_least("1.16", "1.16.1")


def test_python_archive_validation() -> None:
    with tempfile.TemporaryDirectory(prefix="field_crafter_app_update_test_") as temp:
        root = Path(temp)
        source = root / "Field_Crafter_9.9.9_Python"
        (source / "src" / "hc_recipe_db").mkdir(parents=True)
        (source / "data").mkdir()
        (source / "wheelhouse").mkdir()
        (source / "Field Crafter.pyw").write_text("# launcher\n", encoding="utf-8")
        (source / "field_crafter_entry.py").write_text("# entry\n", encoding="utf-8")
        (source / ".field_crafter_release").write_text("release\n", encoding="utf-8")
        (source / "src" / "hc_recipe_db" / "version.py").write_text(
            'RELEASE_VERSION = "9.9.9"\n', encoding="utf-8"
        )
        (source / "data" / "application_update_config.json").write_text("{}\n", encoding="utf-8")
        (source / "wheelhouse" / "fixture.whl").write_bytes(b"fixture")
        archive = root / "release.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in source.rglob("*"):
                if path.is_file():
                    zf.write(path, path.relative_to(source.parent).as_posix())
        extracted = _safe_extract_python_release(
            archive,
            root / "extract",
            version="9.9.9",
            max_extract_bytes=1024 * 1024,
        )
        assert extracted.name == "Field_Crafter_9.9.9_Python"
        assert (extracted / "Field Crafter.pyw").is_file()

        evil = root / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("../escape.txt", "nope")
        try:
            _safe_extract_python_release(
                evil,
                root / "evil_extract",
                version="9.9.9",
                max_extract_bytes=1024 * 1024,
            )
        except ApplicationUpdateError:
            pass
        else:
            raise AssertionError("Unsafe Python update ZIP was accepted")


def test_development_checkout_does_not_self_replace() -> None:
    result = check_for_application_update()
    # This test runs from the maintainer source tree, where self-update must never
    # overwrite development files.
    assert result.distribution == "development"
    assert not result.update_available


def main() -> int:
    test_manifest_signature()
    test_version_comparison()
    test_python_archive_validation()
    test_development_checkout_does_not_self_replace()
    print("PASS: Field Crafter application-update unit tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
