from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# FIELD_CRAFTER_TEST_JSON_OUTPUT_V6_2


def default_diagnostic_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) / "FieldCrafter" if base else Path.home() / ".field_crafter"
    return root / "diagnostics"


def write_test_json(
    kind: str,
    pid: int | None,
    payload: dict[str, Any],
    *,
    output_dir: str | Path | None = None,
) -> Path:
    directory = Path(output_dir) if output_dir else default_diagnostic_dir()
    directory.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_kind = "".join(
        ch if ch.isalnum() or ch in {"_", "-"} else "_"
        for ch in str(kind).strip()
    ).strip("_") or "memory_test"
    pid_part = f"_pid{int(pid)}" if pid is not None else ""
    path = directory / f"field_crafter_{safe_kind}_{stamp}{pid_part}.json"

    # Ensure the artifact describes where/when it was written without requiring
    # individual test tools to duplicate the convention.
    result = dict(payload)
    result.setdefault("artifact_kind", safe_kind)
    result.setdefault(
        "generated_at_utc",
        datetime.now(timezone.utc).isoformat(),
    )
    result.setdefault("pid", int(pid) if pid is not None else None)

    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, path)
    return path


def print_result_path(path: Path, *, passed: bool) -> None:
    status = "PASS" if passed else "FAIL"
    print(f"{status}: JSON result written to {path}")
