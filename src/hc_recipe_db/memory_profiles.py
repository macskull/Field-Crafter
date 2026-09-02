from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SUPPORTED_SCHEMA_VERSION = 1
ALLOWED_RESOLVERS = {
    "identity_xyz_relative_v1",
    "rip_relative_cstring_v1",
    "roster_builder_v1",
}


class MemoryProfileError(RuntimeError):
    pass


def as_int(value: Any, *, field: str = "value") -> int:
    if isinstance(value, bool):
        raise MemoryProfileError(f"{field} must be an integer, not boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text, 0)
        except ValueError as exc:
            raise MemoryProfileError(f"{field} is not a valid integer: {value!r}") from exc
    raise MemoryProfileError(f"{field} must be an integer or 0x-prefixed string")


def parse_signature(pattern: str) -> tuple[int | None, ...]:
    if not isinstance(pattern, str) or not pattern.strip():
        raise MemoryProfileError("Signature pattern must be a non-empty string")
    out: list[int | None] = []
    for token in pattern.split():
        if token in {"?", "??"}:
            out.append(None)
            continue
        if len(token) != 2:
            raise MemoryProfileError(f"Invalid signature byte token: {token!r}")
        try:
            out.append(int(token, 16))
        except ValueError as exc:
            raise MemoryProfileError(f"Invalid signature byte token: {token!r}") from exc
    if not out:
        raise MemoryProfileError("Signature pattern contains no bytes")
    if all(value is None for value in out):
        raise MemoryProfileError("Signature pattern cannot be entirely wildcarded")
    return tuple(out)


def find_signature_offsets(data: bytes, pattern: str) -> list[int]:
    pat = parse_signature(pattern)
    if len(data) < len(pat):
        return []

    best_start = 0
    best_blob = b""
    index = 0
    while index < len(pat):
        if pat[index] is None:
            index += 1
            continue
        end = index
        values: list[int] = []
        while end < len(pat) and pat[end] is not None:
            values.append(int(pat[end]))
            end += 1
        blob = bytes(values)
        if len(blob) > len(best_blob):
            best_start = index
            best_blob = blob
        index = end

    hits: list[int] = []
    cursor = 0
    while True:
        anchor = data.find(best_blob, cursor)
        if anchor < 0:
            break
        cursor = anchor + 1
        start = anchor - best_start
        if start < 0 or start + len(pat) > len(data):
            continue
        if all(expected is None or data[start + offset] == expected
               for offset, expected in enumerate(pat)):
            hits.append(start)
    return hits


def _resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parents[2]


def bundled_memory_profile_path() -> Path:
    return _resource_root() / "data" / "memory_profiles.json"


def default_user_memory_profile_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) / "FieldCrafter" if base else Path.home() / ".field_crafter"
    return root / "memory" / "memory_profiles.json"


@dataclass(frozen=True, slots=True)
class MemoryProfile:
    profile_id: str
    profile_version: str
    priority: int
    source: str
    data: dict[str, Any]

    def locator(self, name: str) -> dict[str, Any]:
        value = (self.data.get("locators") or {}).get(name)
        if not isinstance(value, dict):
            raise MemoryProfileError(
                f"Profile {self.profile_id!r} has no locator {name!r}"
            )
        return value

    def structure(self, name: str) -> dict[str, Any]:
        value = (self.data.get("structures") or {}).get(name)
        if not isinstance(value, dict):
            raise MemoryProfileError(
                f"Profile {self.profile_id!r} has no structure {name!r}"
            )
        return value

    def validation(self) -> dict[str, Any]:
        value = self.data.get("validation") or {}
        if not isinstance(value, dict):
            raise MemoryProfileError(
                f"Profile {self.profile_id!r} validation block must be an object"
            )
        return value


def _require_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MemoryProfileError(f"{field} must be an object")
    return value


def _require_offset(block: dict[str, Any], key: str, field: str) -> int:
    if key not in block:
        raise MemoryProfileError(f"{field}.{key} is required")
    value = as_int(block[key], field=f"{field}.{key}")
    if value < -0x10000000 or value > 0x10000000:
        raise MemoryProfileError(f"{field}.{key} is outside the allowed range")
    return value


def _validate_locator(name: str, value: Any) -> None:
    block = _require_dict(value, f"locators.{name}")
    resolver = str(block.get("resolver") or "")
    if resolver not in ALLOWED_RESOLVERS:
        raise MemoryProfileError(
            f"locators.{name}.resolver {resolver!r} is not supported"
        )
    parse_signature(str(block.get("pattern") or ""))

    if resolver == "identity_xyz_relative_v1":
        for key in ("disp_offset", "instruction_end", "target_adjust", "string_offset"):
            _require_offset(block, key, f"locators.{name}")
    elif resolver == "rip_relative_cstring_v1":
        for key in ("disp_offset", "instruction_end"):
            _require_offset(block, key, f"locators.{name}")
    elif resolver == "roster_builder_v1":
        for key in (
            "base_disp_offset", "base_instruction_end",
            "count_disp_offset", "count_instruction_end",
        ):
            _require_offset(block, key, f"locators.{name}")


def _validate_profile(raw: Any, *, source: str) -> MemoryProfile:
    profile = _require_dict(raw, "profile")
    profile_id = str(profile.get("id") or "").strip()
    profile_version = str(profile.get("profile_version") or "").strip()
    if not profile_id:
        raise MemoryProfileError("profile.id is required")
    if not profile_version:
        raise MemoryProfileError(f"Profile {profile_id!r} is missing profile_version")

    locators = _require_dict(profile.get("locators"), f"{profile_id}.locators")
    for required in ("identity", "server", "roster"):
        if required not in locators:
            raise MemoryProfileError(f"Profile {profile_id!r} is missing {required!r} locator")
        _validate_locator(required, locators[required])

    structures = _require_dict(profile.get("structures"), f"{profile_id}.structures")
    entity = _require_dict(structures.get("entity"), f"{profile_id}.structures.entity")
    character = _require_dict(structures.get("character"), f"{profile_id}.structures.character")
    entries = _require_dict(structures.get("entries"), f"{profile_id}.structures.entries")
    salvage = _require_dict(character.get("salvage"), f"{profile_id}.structures.character.salvage")
    recipes = _require_dict(character.get("recipes"), f"{profile_id}.structures.character.recipes")
    roster = _require_dict(structures.get("roster"), f"{profile_id}.structures.roster")

    for block, field, keys in (
        (roster, "structures.roster", ("record_stride", "name_offset", "entity_pointer_offset")),
        (entity, "structures.entity", ("name_offset", "character_pointer_offset")),
        (character, "structures.character",
         ("current_hp_offset", "current_end_offset", "max_hp_offset", "max_end_offset")),
        (salvage, "structures.character.salvage",
         ("collection_offset", "capacity_offset", "count_offset")),
        (recipes, "structures.character.recipes",
         ("collection_offset", "capacity_offset", "count_offset")),
        (entries, "structures.entries",
         ("definition_pointer_offset", "quantity_offset", "internal_name_pointer_offset",
          "recipe_level_offset")),
    ):
        for key in keys:
            _require_offset(block, key, field)

    validation = _require_dict(profile.get("validation"), f"{profile_id}.validation")
    for key in (
        "max_roster_count", "max_inventory_capacity", "max_collection_entries",
        "max_internal_string", "max_recipe_level",
    ):
        if key not in validation:
            raise MemoryProfileError(f"{profile_id}.validation.{key} is required")
        value = as_int(validation[key], field=f"{profile_id}.validation.{key}")
        if value <= 0 or value > 100000:
            raise MemoryProfileError(f"{profile_id}.validation.{key} is implausible")

    return MemoryProfile(
        profile_id=profile_id,
        profile_version=profile_version,
        priority=int(profile.get("priority") or 0),
        source=source,
        data=profile,
    )


def load_profile_pack(path: str | Path, *, source: str) -> tuple[str, list[MemoryProfile]]:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise MemoryProfileError(f"Could not read memory profile pack {path}: {exc}") from exc
    pack = _require_dict(raw, "memory profile pack")
    schema = as_int(pack.get("schema_version"), field="schema_version")
    if schema != SUPPORTED_SCHEMA_VERSION:
        raise MemoryProfileError(
            f"Unsupported memory profile schema {schema}; expected {SUPPORTED_SCHEMA_VERSION}"
        )
    pack_version = str(pack.get("pack_version") or "").strip()
    if not pack_version:
        raise MemoryProfileError("memory profile pack is missing pack_version")
    raw_profiles = pack.get("profiles")
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise MemoryProfileError("memory profile pack must contain a non-empty profiles array")
    profiles = [_validate_profile(item, source=source) for item in raw_profiles]
    return pack_version, profiles


class MemoryProfileManager:
    def __init__(
        self,
        *,
        bundled_path: str | Path | None = None,
        user_pack_path: str | Path | None = None,
    ) -> None:
        self.bundled_path = Path(bundled_path) if bundled_path else bundled_memory_profile_path()
        self.user_pack_path = (
            Path(user_pack_path) if user_pack_path is not None
            else default_user_memory_profile_path()
        )
        self.warnings: list[str] = []
        self._profiles: list[MemoryProfile] = []
        self._load()

    def _load(self) -> None:
        if not self.bundled_path.exists():
            raise MemoryProfileError(
                f"Bundled memory profile pack was not found: {self.bundled_path}"
            )

        _pack_version, bundled = load_profile_pack(
            self.bundled_path, source="bundled"
        )
        candidates: list[MemoryProfile] = list(bundled)

        if self.user_pack_path.exists():
            try:
                _user_version, user_profiles = load_profile_pack(
                    self.user_pack_path, source="user"
                )
                candidates = list(user_profiles) + candidates
            except MemoryProfileError as exc:
                self.warnings.append(str(exc))

        source_rank = {"user": 0, "bundled": 1}
        candidates.sort(
            key=lambda p: (
                source_rank.get(p.source, 9),
                -p.priority,
                p.profile_id,
                p.profile_version,
            )
        )
        self._profiles = candidates

    def candidates(self) -> tuple[MemoryProfile, ...]:
        return tuple(self._profiles)
