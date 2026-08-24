from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path


def build_zip(source_dir: Path, output_zip: Path) -> None:
    source_dir = source_dir.resolve()
    output_zip = output_zip.resolve()
    if not source_dir.is_dir():
        raise RuntimeError(f"Python release staging directory does not exist: {source_dir}")

    root_name = source_dir.name
    if not root_name:
        raise RuntimeError("Could not determine the Python release top-level folder name.")

    output_zip.parent.mkdir(parents=True, exist_ok=True)
    if output_zip.exists():
        output_zip.unlink()

    files = sorted(p for p in source_dir.rglob("*") if p.is_file())
    if not files:
        raise RuntimeError(f"Python release staging directory is empty: {source_dir}")

    with zipfile.ZipFile(
        output_zip,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        allowZip64=True,
    ) as archive:
        for path in files:
            relative = path.relative_to(source_dir)
            archive_name = (Path(root_name) / relative).as_posix()
            archive.write(path, archive_name)

    # Verify the exact structure immediately after creation.  This intentionally
    # requires one enclosing folder so extracting the public ZIP never sprays
    # Field Crafter's files into the user's current directory.
    prefix = f"{root_name}/"
    required = {
        f"{prefix}Field Crafter.pyw",
        f"{prefix}field_crafter_entry.py",
        f"{prefix}README.txt",
        f"{prefix}data/homecoming_recipes.sqlite",
        f"{prefix}data/memory_recipe_aliases.json",
        f"{prefix}data/release_data_summary.json",
        f"{prefix}data/release_database_info.json",
    }
    with zipfile.ZipFile(output_zip, "r") as archive:
        names = {name.replace("\\", "/") for name in archive.namelist()}

    missing = sorted(required - names)
    if missing:
        preview = "\n".join(f"  {name}" for name in sorted(names)[:30])
        raise RuntimeError(
            "Python release ZIP did not preserve the required enclosing folder.\n"
            f"Missing: {', '.join(missing)}\n"
            "First archive entries:\n"
            f"{preview}"
        )

    rootless_runtime = {
        "Field Crafter.pyw",
        "field_crafter_entry.py",
        "README.txt",
    }
    leaked = sorted(rootless_runtime & names)
    if leaked:
        raise RuntimeError(
            "Python release ZIP contains root-level runtime files instead of only "
            f"the enclosing {root_name}/ folder: {', '.join(leaked)}"
        )

    print(f"PASS: Python release ZIP created with enclosing folder {root_name}/")
    print(f"      {output_zip}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create the Field Crafter public Python ZIP with an exact top-level folder.")
    parser.add_argument("--source", required=True, help="Staged Python release folder")
    parser.add_argument("--output", required=True, help="Destination ZIP path")
    args = parser.parse_args()

    try:
        build_zip(Path(args.source), Path(args.output))
    except Exception as exc:
        print(f"Python release ZIP creation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
