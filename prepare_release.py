from __future__ import annotations

import gc
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from hc_recipe_db.builder import build_database
from hc_recipe_db.game_memory import refresh_memory_recipe_aliases, validate_memory_recipe_alias_coverage
from hc_recipe_db.memory_profiles import load_profile_pack
from hc_recipe_db.memory_profile_updates import load_update_config
from hc_recipe_db.application_updates import load_application_update_config
from hc_recipe_db.validation import validate_database
from hc_recipe_db.version import RELEASE_VERSION

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
FACTORY_DB = DATA_DIR / "homecoming_recipes.sqlite"
FACTORY_ALIASES = DATA_DIR / "memory_recipe_aliases.json"
MEMORY_PROFILES = DATA_DIR / "memory_profiles.json"
MEMORY_UPDATE_CONFIG = DATA_DIR / "memory_update_config.json"
APPLICATION_UPDATE_CONFIG = DATA_DIR / "application_update_config.json"
CACHE_DIR = ROOT / "cache" / "release_data"
EXPORT_DIR = ROOT / "exports"
REPORT_TXT = DATA_DIR / "validation_report.txt"
REPORT_JSON = DATA_DIR / "validation_report.json"
SUMMARY = DATA_DIR / "release_data_summary.json"
INFO = DATA_DIR / "release_database_info.json"
FAILURE_LOG = DATA_DIR / "last_release_prepare_failure.txt"


def _progress(message: str) -> None:
    print(message, flush=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _db_meta(path: Path, key: str) -> str | None:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None
    finally:
        conn.close()


def _set_db_meta(path: Path, key: str, value: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO metadata(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def _copy_exports(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for child in list(target.iterdir()):
        if child.is_file() or child.is_symlink():
            child.unlink()
        else:
            shutil.rmtree(child)
    if not source.exists():
        return
    for child in source.iterdir():
        dest = target / child.name
        if child.is_dir():
            shutil.copytree(child, dest)
        else:
            shutil.copy2(child, dest)


def _cleanup_temp_tree(path: Path | None, *, attempts: int = 8) -> bool:
    """Best-effort cleanup that never masks the real release error on Windows.

    SQLite, Windows Defender, or an indexing process can briefly retain a handle to
    a freshly-written database.  Release preparation must not turn that transient
    cleanup race into a false build failure, and—more importantly—must not hide an
    earlier database/wiki exception behind WinError 32.
    """
    if path is None or not path.exists():
        return True
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            last_error = exc
            gc.collect()
            if attempt + 1 < attempts:
                time.sleep(min(0.15 * (attempt + 1), 0.75))
    if last_error:
        _progress(
            f"Warning: temporary release workspace could not be removed immediately: {path} ({last_error})"
        )
    return False



def _windows_assert_replaceable(
    path: Path,
    *,
    label: str,
    attempts: int = 40,
    delay_seconds: float = 0.25,
) -> None:
    """Fail early only after a sustained Windows replacement lock."""
    if os.name != "nt" or not path.exists():
        return

    import ctypes
    from ctypes import wintypes

    DELETE = 0x00010000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE

    last_error: int | None = None
    for attempt in range(attempts):
        # Make any local Python/SQLite finalizers run before probing Windows.
        gc.collect()

        handle = create_file(
            str(path),
            DELETE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle != INVALID_HANDLE_VALUE:
            kernel32.CloseHandle(handle)
            if attempt:
                _progress(
                    f"{label} replacement lock cleared after "
                    f"{attempt + 1} checks."
                )
            return

        error = ctypes.get_last_error()
        last_error = error
        if error not in {5, 32, 33}:
            raise OSError(
                error,
                f"Could not test replaceability of {path}",
            )

        if attempt + 1 < attempts:
            time.sleep(delay_seconds)

    raise RuntimeError(
        f"{label} remained non-replaceable for approximately "
        f"{attempts * delay_seconds:.1f} seconds "
        f"(Windows error {last_error}). Close any running Field Crafter "
        "instance, SQLite/DB browser, antivirus scan, sync/indexing tool, or "
        "other program using this source-tree file and retry: "
        f"{path}"
    )


def _replace_with_retry(
    source: Path,
    target: Path,
    *,
    label: str,
    attempts: int = 12,
) -> None:
    """Replace a release file, tolerating short-lived Defender/indexer locks."""
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            last_error = exc
            winerror = getattr(exc, "winerror", None)
            if os.name != "nt" or winerror not in {5, 32, 33}:
                raise
            gc.collect()
            if attempt + 1 < attempts:
                time.sleep(min(0.20 * (attempt + 1), 1.25))

    assert last_error is not None
    raise RuntimeError(
        f"{label} could not replace {target.name} after {attempts} attempts. "
        "A Windows process is still holding the file open. Close Field Crafter, "
        "SQLite/DB tools, or anything else using the source-tree data directory "
        f"and retry. Last error: {last_error}"
    ) from last_error


def _safe_remove_swap_artifact(
    artifact: Path,
    *,
    live_target: Path | None = None,
) -> bool:
    if not artifact.exists():
        return False
    if live_target is not None and live_target.is_file():
        try:
            if _sha256(artifact) != _sha256(live_target):
                raise RuntimeError(
                    "Stale release-swap artifact differs from the live file and "
                    f"requires manual inspection: {artifact}"
                )
        except OSError:
            raise
    artifact.unlink()
    return True



def _strict_validate(db_path: Path, alias_path: Path) -> tuple[list, dict]:
    conn = sqlite3.connect(db_path)
    try:
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        conn.close()
    if quick != "ok":
        raise RuntimeError(f"SQLite quick_check failed: {quick}")

    checks = validate_database(db_path)
    errors = [c for c in checks if str(getattr(c, "severity", "")).upper() == "ERROR"]
    if errors:
        raise RuntimeError(f"Recipe database validation reported {len(errors)} ERROR check(s).")

    coverage = validate_memory_recipe_alias_coverage(db_path, alias_path)
    if not coverage.get("complete"):
        missing_sets = coverage.get("missing_sets") or []
        invalid_aliases = coverage.get("invalid_aliases") or []
        sample = ", ".join(missing_sets[:10])
        if len(missing_sets) > 10:
            sample += f" (+{len(missing_sets) - 10} more)"
        raise RuntimeError(
            "Game-memory recipe-map coverage is incomplete. "
            f"Sets {coverage.get('covered_set_count')}/{coverage.get('set_count')}; "
            f"members {coverage.get('covered_member_count')}/{coverage.get('member_count')}; "
            f"common IOs {coverage.get('covered_common_count')}/{coverage.get('common_count')}; "
            f"invalid aliases {len(invalid_aliases)}. Missing sets: {sample or 'none'}."
        )
    return checks, coverage


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    FAILURE_LOG.unlink(missing_ok=True)

    _progress(f"Preparing Field Crafter {RELEASE_VERSION} release data...")
    try:
        if not MEMORY_PROFILES.exists():
            raise RuntimeError(
                f"Bundled memory definitions are missing: {MEMORY_PROFILES}"
            )
        if not MEMORY_UPDATE_CONFIG.exists():
            raise RuntimeError(
                f"Bundled memory update configuration is missing: {MEMORY_UPDATE_CONFIG}"
            )
        if not APPLICATION_UPDATE_CONFIG.exists():
            raise RuntimeError(
                f"Bundled application update configuration is missing: {APPLICATION_UPDATE_CONFIG}"
            )
        memory_pack_version, memory_profiles = load_profile_pack(
            MEMORY_PROFILES,
            source="release_preparation",
        )
        if not memory_pack_version or not memory_profiles:
            raise RuntimeError("Bundled memory definition pack is empty.")
        memory_update_config = load_update_config(MEMORY_UPDATE_CONFIG)
        if not memory_update_config.get("manifest_url"):
            raise RuntimeError("Memory update configuration has no manifest URL.")
        application_update_config = load_application_update_config(APPLICATION_UPDATE_CONFIG)
        if not application_update_config.get("manifest_url"):
            raise RuntimeError("Application update configuration has no manifest URL.")

        # Do this before the expensive Wiki refresh. If a running Field Crafter
        # instance or DB tool has the factory DB open without delete sharing,
        # Windows cannot atomically replace it later.
        _windows_assert_replaceable(
            FACTORY_DB,
            label="Factory recipe database",
        )
        _windows_assert_replaceable(
            FACTORY_ALIASES,
            label="Factory memory recipe map",
        )

        # Clean only byte-identical stale internal restore files from a prior
        # interrupted preparation attempt.
        _safe_remove_swap_artifact(
            DATA_DIR / ".homecoming_recipes.restore.sqlite",
            live_target=FACTORY_DB,
        )
        _safe_remove_swap_artifact(
            DATA_DIR / ".memory_recipe_aliases.restore.json",
            live_target=FACTORY_ALIASES,
        )

        temp_root = Path(tempfile.mkdtemp(prefix="field_crafter_release_"))
        release_candidate_completed = False
        try:
            temp_db = temp_root / "homecoming_recipes.sqlite"
            temp_report = temp_root / "validation_report.txt"
            temp_report_json = temp_root / "validation_report.json"
            temp_exports = temp_root / "exports"
            temp_aliases = temp_root / "memory_recipe_aliases.json"

            _progress("1/5 Refreshing the complete Homecoming recipe/salvage database...")
            result = build_database(
                db_path=temp_db,
                cache_dir=CACHE_DIR / "recipe_database",
                export_dir=temp_exports,
                report_path=temp_report,
                report_json_path=temp_report_json,
                refresh=True,
                delay_seconds=0.45,
                progress=_progress,
            )
            build_errors = int((result.get("validation") or {}).get("error", 0))
            if build_errors:
                raise RuntimeError(
                    f"Refusing release preparation: refreshed database has {build_errors} validation error(s)."
                )

            _set_db_meta(temp_db, "field_crafter_release_version", RELEASE_VERSION)
            _set_db_meta(temp_db, "field_crafter_release_validation", "pending memory-map validation")

            _progress("2/5 Rebuilding the complete game-memory recipe map...")
            map_result = refresh_memory_recipe_aliases(
                temp_db,
                temp_aliases,
                progress=_progress,
                refresh_index=True,
            )

            _progress("3/5 Running strict database and memory-map coverage validation...")
            checks, coverage = _strict_validate(temp_db, temp_aliases)
            validated_at = _utc_now()
            _set_db_meta(temp_db, "field_crafter_release_validation", "validated for redistribution")
            _set_db_meta(temp_db, "field_crafter_release_validated_at_utc", validated_at)
            _set_db_meta(
                temp_db,
                "field_crafter_memory_map_coverage",
                f"sets {coverage['covered_set_count']}/{coverage['set_count']}; "
                f"members {coverage['covered_member_count']}/{coverage['member_count']}; "
                f"common {coverage['covered_common_count']}/{coverage['common_count']}",
            )

            # Validate again after metadata stamping so the exact database to be copied is checked.
            checks, coverage = _strict_validate(temp_db, temp_aliases)
            map_result["validated_coverage"] = coverage

            _progress("4/5 Installing validated factory data atomically...")
            if FACTORY_DB.exists():
                backup_dir = CACHE_DIR / "factory_backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                stamp = (_db_meta(FACTORY_DB, "built_at_utc") or "unknown").replace(":", "-")
                backup = backup_dir / f"homecoming_recipes_{stamp}.sqlite"
                if not backup.exists():
                    shutil.copy2(FACTORY_DB, backup)

            # Copy both candidate files to the destination filesystem first, then
            # replace the live factory pair. Keep temporary rollback copies until
            # the exact installed pair passes strict validation.
            with tempfile.TemporaryDirectory(prefix="field_crafter_factory_swap_", dir=DATA_DIR) as swap_name:
                swap_dir = Path(swap_name)
                staged_db = DATA_DIR / ".homecoming_recipes.release-new.sqlite"
                staged_aliases = DATA_DIR / ".memory_recipe_aliases.release-new.json"
                old_db = swap_dir / "old_homecoming_recipes.sqlite"
                old_aliases = swap_dir / "old_memory_recipe_aliases.json"
                had_db = FACTORY_DB.exists()
                had_aliases = FACTORY_ALIASES.exists()
                if had_db:
                    shutil.copy2(FACTORY_DB, old_db)
                if had_aliases:
                    shutil.copy2(FACTORY_ALIASES, old_aliases)
                shutil.copy2(temp_db, staged_db)
                shutil.copy2(temp_aliases, staged_aliases)
                db_replaced = False
                aliases_replaced = False
                restore_db = DATA_DIR / ".homecoming_recipes.restore.sqlite"
                restore_aliases = DATA_DIR / ".memory_recipe_aliases.restore.json"
                try:
                    _replace_with_retry(
                        staged_db,
                        FACTORY_DB,
                        label="Factory database install",
                    )
                    db_replaced = True

                    _replace_with_retry(
                        staged_aliases,
                        FACTORY_ALIASES,
                        label="Factory memory-map install",
                    )
                    aliases_replaced = True

                    # Validate the installed files, not just the temporary candidates.
                    checks, coverage = _strict_validate(FACTORY_DB, FACTORY_ALIASES)
                except Exception as install_error:
                    rollback_errors: list[str] = []

                    # Restore only files we actually replaced. If the initial DB
                    # replacement itself failed, the live DB was never changed and
                    # there is nothing to restore. This preserves the real error.
                    if db_replaced:
                        try:
                            if had_db:
                                shutil.copy2(old_db, restore_db)
                                _replace_with_retry(
                                    restore_db,
                                    FACTORY_DB,
                                    label="Factory database rollback",
                                )
                            else:
                                FACTORY_DB.unlink(missing_ok=True)
                        except Exception as exc:
                            rollback_errors.append(
                                f"database rollback: {exc}"
                            )

                    if aliases_replaced:
                        try:
                            if had_aliases:
                                shutil.copy2(old_aliases, restore_aliases)
                                _replace_with_retry(
                                    restore_aliases,
                                    FACTORY_ALIASES,
                                    label="Factory memory-map rollback",
                                )
                            else:
                                FACTORY_ALIASES.unlink(missing_ok=True)
                        except Exception as exc:
                            rollback_errors.append(
                                f"memory-map rollback: {exc}"
                            )

                    if rollback_errors:
                        raise RuntimeError(
                            f"Factory-data install failed: {install_error}. "
                            "Rollback also encountered: "
                            + "; ".join(rollback_errors)
                        ) from install_error
                    raise
                finally:
                    staged_db.unlink(missing_ok=True)
                    staged_aliases.unlink(missing_ok=True)

                    # Successful restore replacements consume these paths. If a
                    # transient error left an identical copy behind, remove it.
                    for artifact, live in (
                        (restore_db, FACTORY_DB),
                        (restore_aliases, FACTORY_ALIASES),
                    ):
                        if artifact.exists():
                            try:
                                if live.is_file() and _sha256(artifact) == _sha256(live):
                                    artifact.unlink()
                            except Exception:
                                pass

            # Reports/exports are installed only after the exact factory data pair
            # has survived post-install validation.
            shutil.copy2(temp_report, REPORT_TXT)
            shutil.copy2(temp_report_json, REPORT_JSON)
            _copy_exports(temp_exports, EXPORT_DIR)
            db_hash = _sha256(FACTORY_DB)
            alias_hash = _sha256(FACTORY_ALIASES)
            memory_profiles_hash = _sha256(MEMORY_PROFILES)
            memory_update_config_hash = _sha256(MEMORY_UPDATE_CONFIG)
            application_update_config_hash = _sha256(APPLICATION_UPDATE_CONFIG)

            summary = {
                "release_version": RELEASE_VERSION,
                "release_data_ready": True,
                "redistribution_ready": True,
                "offline_first_launch_ready": True,
                "validated_at_utc": validated_at,
                "database_built_at_utc": _db_meta(FACTORY_DB, "built_at_utc"),
                "database_effective_date": _db_meta(FACTORY_DB, "effective_date"),
                "database_counts": result.get("counts", {}),
                "validation": result.get("validation", {}),
                "validation_check_count": len(checks),
                "memory_map": map_result,
                "memory_profile_pack_version": memory_pack_version,
                "memory_profile_count": len(memory_profiles),
                "memory_update_channel": memory_update_config.get("channel"),
                "application_update_channel": application_update_config.get("channel"),
                "sha256": {
                    "homecoming_recipes.sqlite": db_hash,
                    "memory_recipe_aliases.json": alias_hash,
                    "memory_profiles.json": memory_profiles_hash,
                    "memory_update_config.json": memory_update_config_hash,
                    "application_update_config.json": application_update_config_hash,
                },
                "note": (
                    "Release data was live-refreshed, rebuilt, and strictly validated. "
                    f"This data snapshot is eligible for redistribution with Field Crafter {RELEASE_VERSION}."
                ),
            }
            SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            info = {
                "field_crafter_version": RELEASE_VERSION,
                "database_updated": _db_meta(FACTORY_DB, "built_at_utc"),
                "database_effective_date": _db_meta(FACTORY_DB, "effective_date"),
                "validation_status": "passed",
                "validated_at_utc": validated_at,
                "redistribution_ready": True,
                "recipe_count": (result.get("counts") or {}).get("recipes"),
                "salvage_count": (result.get("counts") or {}).get("salvage"),
                "set_family_count": (result.get("counts") or {}).get("set_families"),
                "memory_mapping_count": map_result.get("mapped_alias_count"),
                "memory_map_coverage": coverage,
                "sha256": summary["sha256"],
            }
            INFO.write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            _progress("5/5 Verifying release metadata and final hashes...")
            if summary["sha256"]["homecoming_recipes.sqlite"] != _sha256(FACTORY_DB):
                raise RuntimeError("Database hash changed unexpectedly after release metadata generation.")
            if summary["sha256"]["memory_recipe_aliases.json"] != _sha256(FACTORY_ALIASES):
                raise RuntimeError("Memory-map hash changed unexpectedly after release metadata generation.")
            if summary["sha256"]["memory_profiles.json"] != _sha256(MEMORY_PROFILES):
                raise RuntimeError("Bundled memory-definition hash changed unexpectedly after release metadata generation.")
            if summary["sha256"]["memory_update_config.json"] != _sha256(MEMORY_UPDATE_CONFIG):
                raise RuntimeError("Memory-update configuration hash changed unexpectedly after release metadata generation.")
            if summary["sha256"]["application_update_config.json"] != _sha256(APPLICATION_UPDATE_CONFIG):
                raise RuntimeError("Application-update configuration hash changed unexpectedly after release metadata generation.")
            release_candidate_completed = True

        finally:
            # On success, clean up here. On failure, wait until the outer handler
            # has detached the exception traceback; that traceback can otherwise
            # keep a failed builder's SQLite connection alive on Windows.
            if release_candidate_completed:
                _cleanup_temp_tree(temp_root)

        _progress("")
        _progress("RELEASE DATA VALIDATION PASSED")
        _progress(
            f"Memory map: {coverage['covered_set_count']}/{coverage['set_count']} sets, "
            f"{coverage['covered_member_count']}/{coverage['member_count']} set members, "
            f"{coverage['covered_common_count']}/{coverage['common_count']} common IOs"
        )
        _progress("The source tree is now ready for strict release packaging.")
        return 0
    except Exception as exc:
        # Format the traceback before detaching it. This both preserves the actual
        # root cause for the maintainer and releases frames that may still own an
        # SQLite connection so the temporary workspace can be deleted on Windows.
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        message = str(exc)
        try:
            exc.__traceback__ = None
        except Exception:
            pass
        gc.collect()
        temp_root_obj = locals().get("temp_root")
        if isinstance(temp_root_obj, Path):
            _cleanup_temp_tree(temp_root_obj)

        FAILURE_LOG.write_text(
            f"Field Crafter {RELEASE_VERSION} release preparation failed at {_utc_now()}\n\n{detail}",
            encoding="utf-8",
        )
        print("\nRELEASE PREPARATION FAILED", flush=True)
        print(message, flush=True)
        print(f"Full diagnostic traceback: {FAILURE_LOG}", flush=True)
        print("Existing factory database and memory-map files were left unchanged unless all validation gates passed.", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
