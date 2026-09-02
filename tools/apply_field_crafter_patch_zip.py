#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


class PatchZipError(RuntimeError):
    pass


def _validate_member(info: zipfile.ZipInfo) -> None:
    name = info.filename.replace("\\", "/")
    posix = PurePosixPath(name)

    if posix.is_absolute():
        raise PatchZipError(f"Archive contains an absolute path: {info.filename!r}")
    if any(part in {"", ".", ".."} for part in posix.parts):
        if ".." in posix.parts:
            raise PatchZipError(f"Archive contains path traversal: {info.filename!r}")

    # Reject Unix symlink entries. Field Crafter patch bundles do not need them.
    mode = (info.external_attr >> 16) & 0o170000
    if mode == 0o120000:
        raise PatchZipError(f"Archive contains a symbolic link: {info.filename!r}")


def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    destination = destination.resolve()
    for info in archive.infolist():
        _validate_member(info)
        target = (destination / info.filename).resolve()
        try:
            target.relative_to(destination)
        except ValueError as exc:
            raise PatchZipError(
                f"Archive member escapes extraction directory: {info.filename!r}"
            ) from exc

    archive.extractall(destination)


def _find_patcher(extracted: Path) -> Path:
    # Future Field Crafter patch ZIPs should contain exactly one top-level-ish
    # apply_*.py patcher. Ignore source payloads and cache/vendor directories.
    candidates: list[Path] = []
    for path in extracted.rglob("apply_*.py"):
        relative = path.relative_to(extracted)
        lowered = {part.casefold() for part in relative.parts}
        if {"src", "__pycache__", "patch_backups"} & lowered:
            continue
        candidates.append(path)

    candidates.sort(key=lambda p: (len(p.relative_to(extracted).parts), str(p)))

    if not candidates:
        raise PatchZipError(
            "No apply_*.py patcher was found in the ZIP. "
            "This does not look like a supported Field Crafter incremental patch."
        )
    if len(candidates) != 1:
        rendered = "\n".join(f"  {p.relative_to(extracted)}" for p in candidates)
        raise PatchZipError(
            "Expected exactly one Field Crafter patcher, but found:\n" + rendered
        )
    return candidates[0]


def _validate_root(root: Path) -> Path:
    root = root.resolve()
    if not (root / "src" / "hc_recipe_db").is_dir():
        raise PatchZipError(
            f"{root} does not look like a Field Crafter source root "
            "(src/hc_recipe_db is missing)."
        )
    return root


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Safely unpack a Field Crafter incremental patch ZIP to a temporary "
            "directory, run its patcher, and remove the temporary files afterward."
        )
    )
    parser.add_argument("patch_zip", help="Path to the Field Crafter patch ZIP")
    parser.add_argument(
        "--root",
        default=".",
        help="Field Crafter source root to patch (default: current directory)",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep the temporary extraction directory for debugging",
    )
    args = parser.parse_args()

    temp_root: Path | None = None
    try:
        patch_zip = Path(args.patch_zip).expanduser().resolve()
        if not patch_zip.is_file():
            raise PatchZipError(f"Patch ZIP does not exist: {patch_zip}")
        root = _validate_root(Path(args.root))

        if args.keep_temp:
            temp_root = Path(
                tempfile.mkdtemp(prefix="field_crafter_patch_")
            ).resolve()
            cleanup = False
        else:
            temp_root = Path(
                tempfile.mkdtemp(prefix="field_crafter_patch_")
            ).resolve()
            cleanup = True

        print(f"Patch ZIP: {patch_zip}")
        print(f"Target:    {root}")
        print(f"Temporary: {temp_root}")

        try:
            with zipfile.ZipFile(patch_zip, "r") as archive:
                bad = archive.testzip()
                if bad is not None:
                    raise PatchZipError(
                        f"ZIP CRC validation failed for archive member: {bad}"
                    )
                _safe_extract(archive, temp_root)

            patcher = _find_patcher(temp_root)
            print(f"Patcher:   {patcher.relative_to(temp_root)}")
            print()
            print("Running patch...")
            print("-" * 72)

            completed = subprocess.run(
                [sys.executable, str(patcher), "--root", str(root)],
                cwd=str(root),
            )

            print("-" * 72)
            if completed.returncode != 0:
                raise PatchZipError(
                    f"Patch process exited with code {completed.returncode}."
                )

            print("PASS: patch completed successfully.")
            return 0
        finally:
            if cleanup and temp_root.exists():
                shutil.rmtree(temp_root, ignore_errors=True)
                print(f"Cleaned temporary extraction: {temp_root}")
            elif not cleanup:
                print(f"Temporary extraction retained: {temp_root}")

    except (PatchZipError, zipfile.BadZipFile) as exc:
        print(f"PATCH ZIP FAILED: {exc}", file=sys.stderr)
        if temp_root is not None and args.keep_temp:
            print(f"Temporary extraction retained: {temp_root}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Patch cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
