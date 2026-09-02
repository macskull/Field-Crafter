from __future__ import annotations

import ctypes
import difflib
import json
import math
import os
import re
import sqlite3
import struct
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .normalize import canonical_key
from .version import APP_VERSION
from .memory_profiles import (
    MemoryProfile, MemoryProfileError, MemoryProfileManager,
    as_int, find_signature_offsets,
)

# FIELD_CRAFTER_MEMORY_PROFILE_REFACTOR_V1
# FIELD_CRAFTER_MEMORY_PROFILE_UPDATES_V2
# FIELD_CRAFTER_MEMORY_DIAGNOSTICS_V3
# FIELD_CRAFTER_MEMORY_DIAGNOSTICS_V3_1
# FIELD_CRAFTER_MEMORY_SESSION_RECOVERY_V4
# FIELD_CRAFTER_MEMORY_ROOT_RECOVERY_V5
# Memory locations/structure offsets are now declarative and profile-driven.
# Fixed RVAs are intentionally not retained here.


PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_INTERNAL_STRING = 192


class GameMemoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class GameProcessInfo:
    pid: int
    executable: str
    window_title: str = ""
    character_name: str = ""
    server: str = ""

    @property
    def label(self) -> str:
        identity = ""
        if self.character_name and self.server:
            identity = f"{self.character_name} - {self.server}"
        elif self.character_name:
            identity = self.character_name
        elif self.server:
            identity = self.server
        elif self.window_title:
            identity = self.window_title
        else:
            identity = "City of Heroes"
        return f"{identity} (PID {self.pid})"


@dataclass
class MemoryRecipe:
    internal_name: str
    level: int
    quantity: int
    canonical_name: str | None
    entry_address: int
    definition_address: int
    mapping_source: str = ""


@dataclass
class MemorySalvage:
    internal_name: str
    quantity: int
    canonical_name: str | None
    entry_address: int
    definition_address: int


@dataclass
class MemoryInventorySnapshot:
    pid: int
    window_title: str
    owner_address: int
    inventory_address: int
    recipe_capacity: int
    recipe_total: int
    recipes: list[MemoryRecipe] = field(default_factory=list)
    salvage_capacity: int = 0
    salvage_total: int = 0
    salvage: list[MemorySalvage] = field(default_factory=list)
    memory_profile_id: str = ""
    memory_profile_version: str = ""
    memory_profile_source: str = ""
    memory_recovery_applied: bool = False
    memory_recovery_kinds: tuple[str, ...] = field(default_factory=tuple)
    memory_recovery_samples: int = 0
    memory_root_recovery_applied: bool = False
    memory_root_recovery_locators: tuple[str, ...] = field(default_factory=tuple)
    memory_root_recovery_samples: int = 0

    @property
    def unresolved_recipe_count(self) -> int:
        return sum(1 for x in self.recipes if not x.canonical_name)

    @property
    def unresolved_salvage_count(self) -> int:
        return sum(1 for x in self.salvage if not x.canonical_name)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", wintypes.WCHAR * 256),
        ("szExePath", wintypes.WCHAR * 260),
    ]


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k.Process32FirstW.restype = wintypes.BOOL
    k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k.Process32NextW.restype = wintypes.BOOL
    k.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
    k.Module32FirstW.restype = wintypes.BOOL
    k.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
    k.Module32NextW.restype = wintypes.BOOL
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.ReadProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    k.ReadProcessMemory.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    return k

def _require_windows() -> None:
    if os.name != "nt":
        raise GameMemoryError("Live game-memory reading is available only on Windows.")


def _window_titles_by_pid() -> dict[int, str]:
    if os.name != "nt":
        return {}
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    titles: dict[int, str] = {}
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if title and int(pid.value) not in titles:
            titles[int(pid.value)] = title
        return True

    proc = EnumWindowsProc(callback)
    user32.EnumWindows(proc, 0)
    return titles


def _valid_identity_string(value: str, *, max_len: int = 48) -> bool:
    if not value or len(value) > max_len or "\ufffd" in value:
        return False
    return all(ch.isprintable() and ch not in "\r\n\t" for ch in value)


def _module_info(pid: int, module_name: str = "cityofheroes.exe") -> tuple[int, int, str]:
    kernel32 = _kernel32()
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snap == INVALID_HANDLE_VALUE:
        raise GameMemoryError(
            f"Could not enumerate modules for PID {pid} (Windows error {ctypes.get_last_error()})."
        )
    try:
        entry = MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Module32FirstW(snap, ctypes.byref(entry))
        while ok:
            if str(entry.szModule).casefold() == module_name.casefold():
                base = ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value or 0
                return int(base), int(entry.modBaseSize), str(entry.szExePath)
            ok = kernel32.Module32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    raise GameMemoryError(f"Could not locate {module_name} module in PID {pid}.")


class ProcessMemory:
    IMAGE_SCN_MEM_EXECUTE = 0x20000000

    def __init__(self, pid: int):
        _require_windows()
        self.pid = int(pid)
        self.kernel32 = _kernel32()
        self.handle = self.kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, self.pid
        )
        if not self.handle:
            raise GameMemoryError(
                f"Could not open cityofheroes.exe PID {pid} for reading "
                f"(Windows error {ctypes.get_last_error()})."
            )
        self.base, self.module_size, self.module_path = _module_info(self.pid)
        self._executable_snapshot: list[tuple[int, bytes]] | None = None
        self._signature_cache: dict[str, list[int]] = {}

    def close(self) -> None:
        if getattr(self, "handle", None):
            self.kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def read(self, address: int, size: int) -> bytes:
        if not address or size <= 0:
            raise GameMemoryError(
                f"Invalid memory read request address=0x{address:X} size={size}."
            )
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t()
        ok = self.kernel32.ReadProcessMemory(
            self.handle, ctypes.c_void_p(address), buf, size, ctypes.byref(got)
        )
        if not ok or got.value != size:
            raise GameMemoryError(
                f"Could not read {size} bytes from PID {self.pid} "
                f"(Windows error {ctypes.get_last_error()})."
            )
        return buf.raw

    def u32(self, address: int) -> int:
        return int.from_bytes(self.read(address, 4), "little", signed=False)

    def i32(self, address: int) -> int:
        return int.from_bytes(self.read(address, 4), "little", signed=True)

    def qword(self, address: int) -> int:
        return int.from_bytes(self.read(address, 8), "little", signed=False)

    def f32(self, address: int) -> float:
        return float(struct.unpack("<f", self.read(address, 4))[0])

    def cstring(self, address: int, max_len: int = MAX_INTERNAL_STRING) -> str:
        raw = self.read(address, max_len)
        raw = raw.split(b"\x00", 1)[0]
        try:
            return raw.decode("ascii", errors="strict")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")

    def _executable_sections(self) -> list[tuple[int, int]]:
        # Parse the in-memory PE headers and scan only executable sections. This
        # avoids thousands of page reads across data/BSS while remaining independent
        # of fixed code RVAs.
        dos = self.read(self.base, min(0x1000, self.module_size))
        if len(dos) < 0x40 or dos[:2] != b"MZ":
            raise GameMemoryError("cityofheroes.exe has an invalid in-memory DOS header.")
        pe_off = int.from_bytes(dos[0x3C:0x40], "little")
        nt = self.read(self.base + pe_off, 24)
        if nt[:4] != b"PE\x00\x00":
            raise GameMemoryError("cityofheroes.exe has an invalid in-memory PE header.")
        section_count = int.from_bytes(nt[6:8], "little")
        optional_size = int.from_bytes(nt[20:22], "little")
        if section_count <= 0 or section_count > 96:
            raise GameMemoryError("cityofheroes.exe reports an implausible PE section count.")
        table = self.read(self.base + pe_off + 24 + optional_size, section_count * 40)
        out: list[tuple[int, int]] = []
        for index in range(section_count):
            row = table[index * 40:(index + 1) * 40]
            virtual_size = int.from_bytes(row[8:12], "little")
            virtual_address = int.from_bytes(row[12:16], "little")
            raw_size = int.from_bytes(row[16:20], "little")
            characteristics = int.from_bytes(row[36:40], "little")
            if not (characteristics & self.IMAGE_SCN_MEM_EXECUTE):
                continue
            size = max(virtual_size, raw_size)
            if virtual_address >= self.module_size:
                continue
            size = min(size, self.module_size - virtual_address)
            if size > 0:
                out.append((virtual_address, size))
        if not out:
            raise GameMemoryError("No executable PE sections were found in cityofheroes.exe.")
        return out

    def executable_snapshot(self) -> list[tuple[int, bytes]]:
        if self._executable_snapshot is not None:
            return self._executable_snapshot
        sections: list[tuple[int, bytes]] = []
        chunk_size = 1024 * 1024
        for section_rva, section_size in self._executable_sections():
            # Code sections should be readable, but use bounded chunks so one failed
            # read cannot discard an entire large section.
            pieces = bytearray(section_size)
            captured = 0
            for offset in range(0, section_size, chunk_size):
                size = min(chunk_size, section_size - offset)
                try:
                    data = self.read(self.base + section_rva + offset, size)
                except GameMemoryError:
                    continue
                pieces[offset:offset + len(data)] = data
                captured += len(data)
            if captured:
                sections.append((section_rva, bytes(pieces)))
        if not sections:
            raise GameMemoryError("Could not snapshot executable cityofheroes.exe sections.")
        self._executable_snapshot = sections
        return sections

    def signature_hits(self, pattern: str) -> list[int]:
        cached = self._signature_cache.get(pattern)
        if cached is not None:
            return list(cached)
        hits: list[int] = []
        for section_rva, data in self.executable_snapshot():
            hits.extend(section_rva + offset for offset in find_signature_offsets(data, pattern))
        hits = sorted(set(hits))
        self._signature_cache[pattern] = hits
        return list(hits)


@dataclass(frozen=True)
class _ResolvedMemoryContext:
    profile: MemoryProfile
    character_name: str
    server: str
    entity_address: int
    character_address: int


def _rip_relative_target_rva(
    mem: ProcessMemory,
    match_rva: int,
    *,
    disp_offset: int,
    instruction_end: int,
    target_adjust: int = 0,
) -> int:
    displacement = mem.i32(mem.base + match_rva + disp_offset)
    return match_rva + instruction_end + displacement + target_adjust


def _resolve_identity(mem: ProcessMemory, profile: MemoryProfile) -> str:
    locator = profile.locator("identity")
    pattern = str(locator["pattern"])
    hits = mem.signature_hits(pattern)
    valid: list[str] = []
    max_string = as_int(locator.get("max_string", 64), field="identity.max_string")
    max_abs = float(locator.get("position_max_abs", 1000000))
    for match_rva in hits:
        try:
            xyz_rva = _rip_relative_target_rva(
                mem,
                match_rva,
                disp_offset=as_int(locator["disp_offset"]),
                instruction_end=as_int(locator["instruction_end"]),
                target_adjust=as_int(locator.get("target_adjust", 0)),
            )
            if not (0 <= xyz_rva <= mem.module_size - 12):
                continue
            coords = [mem.f32(mem.base + xyz_rva + offset) for offset in (0, 4, 8)]
            if not all(math.isfinite(value) and abs(value) <= max_abs for value in coords):
                continue
            name = mem.cstring(
                mem.base + xyz_rva + as_int(locator["string_offset"]),
                max_string,
            ).strip()
            if _valid_identity_string(name, max_len=max_string - 1):
                valid.append(name)
        except Exception:
            continue
    if len(valid) != 1:
        raise GameMemoryError(
            f"Profile {profile.profile_id} identity locator did not resolve to exactly one valid character."
        )
    return valid[0]


def _resolve_server(mem: ProcessMemory, profile: MemoryProfile, *, required: bool) -> str:
    locator = profile.locator("server")
    hits = mem.signature_hits(str(locator["pattern"]))
    valid: list[str] = []
    max_string = as_int(locator.get("max_string", 64), field="server.max_string")
    for match_rva in hits:
        try:
            target_rva = _rip_relative_target_rva(
                mem,
                match_rva,
                disp_offset=as_int(locator["disp_offset"]),
                instruction_end=as_int(locator["instruction_end"]),
            )
            if not (0 <= target_rva < mem.module_size):
                continue
            value = mem.cstring(mem.base + target_rva, max_string).strip()
            if _valid_identity_string(value, max_len=max_string - 1):
                valid.append(value)
        except Exception:
            continue
    unique = list(dict.fromkeys(valid))
    if len(unique) == 1:
        return unique[0]
    if not required and not unique:
        return ""
    raise GameMemoryError(
        f"Profile {profile.profile_id} selected-server locator did not resolve uniquely."
    )


def _character_vitals_plausible(
    mem: ProcessMemory, character: int, profile: MemoryProfile
) -> bool:
    character_cfg = profile.structure("character")
    limits = profile.validation()
    try:
        hp = mem.f32(character + as_int(character_cfg["current_hp_offset"]))
        end = mem.f32(character + as_int(character_cfg["current_end_offset"]))
        max_hp = mem.f32(character + as_int(character_cfg["max_hp_offset"]))
        max_end = mem.f32(character + as_int(character_cfg["max_end_offset"]))
    except Exception:
        return False
    hp_limit = float(limits.get("max_hp", 1000000))
    end_limit = float(limits.get("max_endurance", 100000))
    return (
        all(math.isfinite(value) for value in (hp, end, max_hp, max_end))
        and -10 <= hp <= hp_limit
        and -10 <= end <= end_limit
        and 0 < max_hp <= hp_limit
        and 0 < max_end <= end_limit
        and hp <= max_hp * 2.0 + 100
        and end <= max_end * 2.0 + 100
    )


def _resolve_entity_character(
    mem: ProcessMemory,
    profile: MemoryProfile,
    identity_name: str,
) -> tuple[int, int]:
    locator = profile.locator("roster")
    roster_cfg = profile.structure("roster")
    entity_cfg = profile.structure("entity")
    max_roster = as_int(profile.validation()["max_roster_count"])

    valid: list[tuple[int, int]] = []
    for match_rva in mem.signature_hits(str(locator["pattern"])):
        try:
            roster_base_rva = _rip_relative_target_rva(
                mem,
                match_rva,
                disp_offset=as_int(locator["base_disp_offset"]),
                instruction_end=as_int(locator["base_instruction_end"]),
            )
            roster_count_rva = _rip_relative_target_rva(
                mem,
                match_rva,
                disp_offset=as_int(locator["count_disp_offset"]),
                instruction_end=as_int(locator["count_instruction_end"]),
            )
            if not (
                0 <= roster_base_rva < mem.module_size
                and 0 <= roster_count_rva <= mem.module_size - 4
            ):
                continue
            raw_count = mem.u32(mem.base + roster_count_rva)
            if raw_count > max_roster:
                continue

            record = mem.base + roster_base_rva
            copied_name = mem.cstring(
                record + as_int(roster_cfg["name_offset"]), 64
            ).strip()
            entity = mem.qword(record + as_int(roster_cfg["entity_pointer_offset"]))
            if not entity:
                continue
            entity_name = mem.cstring(
                entity + as_int(entity_cfg["name_offset"]), 64
            ).strip()
            character = mem.qword(
                entity + as_int(entity_cfg["character_pointer_offset"])
            )
            if not character:
                continue
            if not (
                copied_name == identity_name
                and entity_name == identity_name
                and _character_vitals_plausible(mem, character, profile)
            ):
                continue
            valid.append((entity, character))
        except Exception:
            continue

    # Deduplicate in case two signatures/aliases someday point to the same structure.
    valid = list(dict.fromkeys(valid))
    if len(valid) != 1:
        raise GameMemoryError(
            f"Profile {profile.profile_id} roster -> Entity -> Character validation failed."
        )
    return valid[0]


def _resolve_memory_context(
    mem: ProcessMemory,
    manager: MemoryProfileManager,
    *,
    require_server: bool = False,
) -> _ResolvedMemoryContext:
    failures: list[str] = []
    for profile in manager.candidates():
        try:
            character_name = _resolve_identity(mem, profile)
            server = _resolve_server(mem, profile, required=require_server)
            entity, character = _resolve_entity_character(
                mem, profile, character_name
            )
            return _ResolvedMemoryContext(
                profile=profile,
                character_name=character_name,
                server=server,
                entity_address=entity,
                character_address=character,
            )
        except (GameMemoryError, MemoryProfileError) as exc:
            failures.append(f"{profile.profile_id}: {exc}")
    detail = "; ".join(failures[:4]) or "no profiles were available"
    raise GameMemoryError(
        "No memory profile passed semantic validation for this City of Heroes client. "
        f"{detail}"
    )


def _read_process_identity(pid: int) -> tuple[str, str]:
    """Resolve selector identity through the same signature/profile path as inventory."""
    try:
        manager = MemoryProfileManager()
        with ProcessMemory(pid) as mem:
            context = _resolve_memory_context(mem, manager, require_server=False)
            return context.character_name, context.server
    except Exception:
        return "", ""


def list_city_of_heroes_processes() -> list[GameProcessInfo]:
    _require_windows()
    kernel32 = _kernel32()
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        raise GameMemoryError(
            f"Could not enumerate processes (Windows error {ctypes.get_last_error()})."
        )
    titles = _window_titles_by_pid()
    raw: list[tuple[int, str]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            exe = str(entry.szExeFile)
            if exe.casefold() == "cityofheroes.exe":
                raw.append((int(entry.th32ProcessID), exe))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)

    found: list[GameProcessInfo] = []
    for pid, exe in raw:
        character, server = _read_process_identity(pid)
        if not character or not server:
            try:
                from .memory_root_recovery import recover_root_context
                with ProcessMemory(pid) as recovery_mem:
                    selector_recovery = recover_root_context(
                        recovery_mem,
                        MemoryProfileManager(),
                        require_server=True,
                    )
                if not character:
                    character = selector_recovery.context.character_name
                if not server:
                    server = selector_recovery.context.server
            except Exception:
                # Selector labeling remains best-effort. The actual inventory read
                # has its own fail-closed recovery and diagnostic path.
                pass
        found.append(
            GameProcessInfo(
                pid=pid,
                executable=exe,
                window_title=titles.get(pid, ""),
                character_name=character,
                server=server,
            )
        )
    return sorted(found, key=lambda x: ((x.character_name or "~").casefold(), x.pid))

_COMMON_IO_INTERNAL_TO_CANONICAL = {
    "Accuracy": "Invention: Accuracy",
    "Confuse": "Invention: Confuse",
    "Damage": "Invention: Damage",
    "Defense_Buff": "Invention: Defense Buff",
    "Defense_DeBuff": "Invention: Defense DeBuff",
    "Endurance_Discount": "Invention: Endurance Reduction",
    "Fear": "Invention: Fear",
    "Fly": "Invention: Fly",
    "Heal": "Invention: Healing/Absorb",
    "Hold": "Invention: Hold",
    "Immobilize": "Invention: Immobilize",
    "Intangible": "Invention: Intangibility",
    "Interrupt": "Invention: Interrupt",
    "Jump": "Invention: Jump",
    "Knockback": "Invention: Knockback",
    "Range": "Invention: Range",
    "Recharge": "Invention: Recharge Reduction",
    "Recovery": "Invention: Endurance Modification",
    "Res_Damage": "Invention: Resist Damage",
    "Run": "Invention: Run",
    "Sleep": "Invention: Sleep",
    "Snare": "Invention: Snare",
    "Stun": "Invention: Stun",
    "Taunt": "Invention: Taunt",
    "ToHit_Buff": "Invention: To Hit",
    "ToHit_DeBuff": "Invention: To Hit Debuff",
}

# Seed aliases are the IDs directly validated during memory discovery. The resolver is
# intentionally extensible: manually corrected memory IDs are persisted in the user's
# LocalAppData and take precedence over this seed table.
_SEED_RECIPE_ALIASES = {
    "Absolute_Amazement_B": "Absolute Amazement: Stun/Recharge (Superior)",
    "Gladiators_Armor_B": "Gladiator's Armor: Resistance/Recharge",
    "Hecatomb_A": "Hecatomb: Damage (Superior)",
    "Ragnarok_F": "Ragnarok: Chance for Knockdown",
    "Unbreakable_Constraint_C": "Unbreakable Constraint: Hold/Recharge/Accuracy (Superior)",
    # Panacea's legacy AttribFileItem aspects predate the current Healing/Absorb names.
    # These validated A-F aliases prevent an old terminology mismatch from appearing
    # as an unmapped recipe before the user refreshes the full memory recipe map.
    "Panacea_A": "Panacea: Healing/Absorb/Endurance",
    "Panacea_B": "Panacea: Endurance/Recharge",
    "Panacea_C": "Panacea: Healing/Absorb/Recharge",
    "Panacea_D": "Panacea: Healing/Absorb/Endurance/Recharge",
    "Panacea_E": "Panacea: Healing/Absorb",
    "Panacea_F": "Panacea: Chance for +Hit Points/Endurance",
    # Healing sets whose legacy AttribFileItem aspect labels predate the current
    # Healing/Absorb terminology. Keep explicit A-F aliases so live memory reads
    # work even before a future Wiki map refresh.
    "Triage_A": "Triage: Healing/Absorb/Endurance",
    "Triage_B": "Triage: Endurance/Recharge",
    "Triage_C": "Triage: Healing/Absorb/Recharge",
    "Triage_D": "Triage: Healing/Absorb/Endurance/Recharge",
    "Regenerative_Tissue_A": "Regenerative Tissue: Healing/Absorb/Endurance",
    "Regenerative_Tissue_B": "Regenerative Tissue: Endurance/Recharge",
    "Regenerative_Tissue_C": "Regenerative Tissue: Healing/Absorb/Recharge",
    "Regenerative_Tissue_D": "Regenerative Tissue: Healing/Absorb/Endurance/Recharge",
    "Regenerative_Tissue_E": "Regenerative Tissue: Regeneration",
    "Harmonized_Healing_A": "Harmonized Healing: Healing/Absorb/Endurance",
    "Harmonized_Healing_B": "Harmonized Healing: Endurance/Recharge",
    "Harmonized_Healing_C": "Harmonized Healing: Healing/Absorb/Recharge",
    "Harmonized_Healing_D": "Harmonized Healing: Healing/Absorb/Endurance/Recharge",
    "Harmonized_Healing_E": "Harmonized Healing: Healing/Absorb",
    "Harmonized_Healing_F": "Harmonized Healing: Endurance",
    "Miracle_A": "Miracle: Healing/Absorb/Endurance",
    "Miracle_B": "Miracle: Endurance/Recharge",
    "Miracle_C": "Miracle: Healing/Absorb/Recharge",
    "Miracle_D": "Miracle: Healing/Absorb/Endurance/Recharge",
    "Miracle_E": "Miracle: Healing/Absorb",
    "Miracle_F": "Miracle: Recovery",
    "Doctored_Wounds_A": "Doctored Wounds: Healing/Absorb/Endurance",
    "Doctored_Wounds_B": "Doctored Wounds: Endurance/Recharge",
    "Doctored_Wounds_C": "Doctored Wounds: Healing/Absorb/Recharge",
    "Doctored_Wounds_D": "Doctored Wounds: Healing/Absorb/Endurance/Recharge",
    "Doctored_Wounds_E": "Doctored Wounds: Healing/Absorb",
    "Doctored_Wounds_F": "Doctored Wounds: Recharge",
    "Numinas_Convalescence_A": "Numina's Convalescence: Healing/Absorb/Endurance",
    "Numinas_Convalescence_B": "Numina's Convalescence: Endurance/Recharge",
    "Numinas_Convalescence_C": "Numina's Convalescence: Healing/Absorb/Recharge",
    "Numinas_Convalescence_D": "Numina's Convalescence: Healing/Absorb/Endurance/Recharge",
    "Numinas_Convalescence_E": "Numina's Convalescence: Healing/Absorb",
    "Numinas_Convalescence_F": "Numina's Convalescence: Regeneration/Recovery",
    "Preventive_Medicine_A": "Preventive Medicine: Healing/Absorb",
    "Preventive_Medicine_B": "Preventive Medicine: Healing/Absorb/Endurance",
    "Preventive_Medicine_C": "Preventive Medicine: Recharge/Endurance",
    "Preventive_Medicine_D": "Preventive Medicine: Healing/Absorb/Recharge",
    "Preventive_Medicine_E": "Preventive Medicine: Healing/Absorb/Endurance/Recharge",
    "Preventive_Medicine_F": "Preventive Medicine: Absorb Proc",
}

# These sets are always resolved by current set-page A-F ordering when refreshing
# the memory map. The legacy AttribFileItem aspect text is validation-only for them.
_HEALING_ORDINAL_SET_KEYS = {
    canonical_key(name) for name in (
        "Triage", "Regenerative Tissue", "Harmonized Healing", "Miracle",
        "Doctored Wounds", "Numina's Convalescence", "Panacea",
        "Preventive Medicine",
    )
}

_RECIPE_LEVEL_RE = re.compile(r"^(?P<stem>.+)_(?P<level>\d{1,2})$")
_SET_PIECE_RE = re.compile(r"^(?P<set>.+)_(?P<piece>[A-Z])$")


def _bundled_memory_alias_path() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        root = Path(getattr(sys, "_MEIPASS"))
    else:
        root = Path(__file__).resolve().parents[2]
    return root / "data" / "memory_recipe_aliases.json"


class MemoryNameResolver:
    def __init__(self, db_path: str | Path, *, alias_path: str | Path | None = None):
        self.db_path = Path(db_path)
        self.alias_path = Path(alias_path) if alias_path else None
        self.recipe_names: set[str] = set()
        self.recipe_set_by_key: dict[str, str] = {}
        self.recipe_set_key_by_recipe: dict[str, str] = {}
        self.salvage_by_key: dict[str, str] = {}
        self.factory_aliases: dict[str, str] = {}
        self.custom_aliases: dict[str, str] = {}
        self.set_piece_aliases: dict[str, dict[str, str]] = {}
        self.internal_stem_to_set_key: dict[str, str] = {}
        self._load_db()
        self._load_factory_aliases()
        self._load_custom_aliases()
        self._build_set_piece_indexes()

    def _load_db(self) -> None:
        uri = f"file:{self.db_path.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            rows = conn.execute("SELECT name, set_name FROM recipes").fetchall()
            for name, set_name in rows:
                name = str(name)
                self.recipe_names.add(name)
                if set_name:
                    set_key = canonical_key(str(set_name))
                    self.recipe_set_by_key.setdefault(set_key, str(set_name))
                    self.recipe_set_key_by_recipe[name] = set_key
            for (name,) in conn.execute("SELECT name FROM salvage"):
                self.salvage_by_key[canonical_key(str(name))] = str(name)
        finally:
            conn.close()

    def _load_factory_aliases(self) -> None:
        path = _bundled_memory_alias_path()
        if not path.exists():
            return
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                for key, recipe in value.items():
                    if isinstance(key, str) and isinstance(recipe, str) and recipe in self.recipe_names:
                        self.factory_aliases[key] = recipe
        except Exception:
            # A bad optional factory map must never prevent live memory reading.
            pass

    def _load_custom_aliases(self) -> None:
        if not self.alias_path or not self.alias_path.exists():
            return
        try:
            value = json.loads(self.alias_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                for key, recipe in value.items():
                    if isinstance(key, str) and isinstance(recipe, str) and recipe in self.recipe_names:
                        self.custom_aliases[key] = recipe
        except Exception:
            pass

    def _build_set_piece_indexes(self) -> None:
        """Build punctuation-insensitive set/piece indexes from validated aliases.

        The factory map remains the authority for which canonical recipe A-F means.
        This index only makes the set stem tolerant of punctuation, spacing, and
        conservative spelling drift. It never fuzzy-matches enhancement/aspect text.
        """
        combined: dict[str, str] = {}
        combined.update(_SEED_RECIPE_ALIASES)
        combined.update(self.factory_aliases)
        combined.update(self.custom_aliases)
        for alias_base, recipe in combined.items():
            if recipe not in self.recipe_names:
                continue
            sm = _SET_PIECE_RE.match(alias_base)
            if not sm:
                continue
            set_key = self.recipe_set_key_by_recipe.get(recipe)
            if not set_key:
                continue
            piece = sm.group("piece")
            self.set_piece_aliases.setdefault(set_key, {}).setdefault(piece, recipe)
            self.internal_stem_to_set_key.setdefault(canonical_key(sm.group("set")), set_key)

    def _fuzzy_set_match(self, incoming_key: str, piece: str) -> tuple[str | None, float, float]:
        """Return a conservative fuzzy set match, score, and runner-up score."""
        candidates = [
            key for key, pieces in self.set_piece_aliases.items()
            if piece in pieces
        ]
        if not candidates:
            return None, 0.0, 0.0

        scored: list[tuple[float, str]] = []
        for set_key in candidates:
            scores = [difflib.SequenceMatcher(None, incoming_key, set_key).ratio()]
            for stem_key, stem_set_key in self.internal_stem_to_set_key.items():
                if stem_set_key == set_key:
                    scores.append(difflib.SequenceMatcher(None, incoming_key, stem_key).ratio())
            scored.append((max(scores), set_key))
        scored.sort(reverse=True)
        best_score, best_key = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        # Keep this deliberately conservative. The A-F letter remains exact, and
        # only a clearly dominant set-name match is allowed to auto-resolve.
        if best_score < 0.88 or best_score - runner_up < 0.07:
            return None, best_score, runner_up
        return best_key, best_score, runner_up

    def _remember_fuzzy_set_stem(self, incoming_stem: str, set_key: str) -> None:
        if not self.alias_path:
            return
        pieces = self.set_piece_aliases.get(set_key) or {}
        changed = False
        for piece, recipe in pieces.items():
            alias_base = f"{incoming_stem}_{piece}"
            if self.custom_aliases.get(alias_base) != recipe:
                self.custom_aliases[alias_base] = recipe
                changed = True
        if changed:
            self.alias_path.parent.mkdir(parents=True, exist_ok=True)
            self.alias_path.write_text(
                json.dumps(dict(sorted(self.custom_aliases.items())), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self._build_set_piece_indexes()

    @staticmethod
    def base_recipe_internal_name(internal_name: str) -> tuple[str, int | None]:
        m = _RECIPE_LEVEL_RE.match(internal_name.strip())
        if not m:
            return internal_name.strip(), None
        return m.group("stem"), int(m.group("level"))

    def resolve_recipe(self, internal_name: str, level: int | None = None) -> tuple[str | None, str]:
        base, parsed_level = self.base_recipe_internal_name(internal_name)
        _level = level if level is not None else parsed_level
        if internal_name in self.custom_aliases:
            return self.custom_aliases[internal_name], "saved_alias"
        if base in self.custom_aliases:
            return self.custom_aliases[base], "saved_alias"
        if internal_name in self.factory_aliases:
            return self.factory_aliases[internal_name], "bundled_alias"
        if base in self.factory_aliases:
            return self.factory_aliases[base], "bundled_alias"
        if base in _SEED_RECIPE_ALIASES:
            value = _SEED_RECIPE_ALIASES[base]
            if value in self.recipe_names:
                return value, "validated_seed"
        if base.startswith("Invention_"):
            stem = base[len("Invention_"):]
            value = _COMMON_IO_INTERNAL_TO_CANONICAL.get(stem)
            if value in self.recipe_names:
                return value, "common_io"
            # Compatibility with pre-1.13 databases, which stored the old common
            # enhancement label as "Invention: Healing".
            if stem == "Heal" and "Invention: Healing" in self.recipe_names:
                return "Invention: Healing", "common_io_legacy_healing"

        sm = _SET_PIECE_RE.match(base)
        if not sm:
            return None, "unmapped"

        incoming_stem = sm.group("set")
        incoming_key = canonical_key(incoming_stem)
        piece = sm.group("piece")

        # First tolerate punctuation/case/underscore differences. canonical_key()
        # makes names such as Numinas_Convalescence and Numina's Convalescence
        # identical without weakening which A-F piece is being requested.
        set_key = self.internal_stem_to_set_key.get(incoming_key)
        if set_key is None and incoming_key in self.recipe_set_by_key:
            set_key = incoming_key
        if set_key is not None:
            recipe = (self.set_piece_aliases.get(set_key) or {}).get(piece)
            if recipe:
                return recipe, "normalized_set_stem"

        # Final automatic fallback: fuzzy-match only the set stem. The A-F letter
        # remains exact and the canonical piece still comes from the validated map.
        fuzzy_key, best_score, runner_up = self._fuzzy_set_match(incoming_key, piece)
        if fuzzy_key is not None:
            recipe = self.set_piece_aliases[fuzzy_key][piece]
            self._remember_fuzzy_set_stem(incoming_stem, fuzzy_key)
            return recipe, f"fuzzy_set_stem:{best_score:.3f}:{runner_up:.3f}"

        # If we can identify the canonical set but do not have the A-F mapping for
        # this piece, keep it unresolved rather than guessing from aspect text.
        canonical_set = self.recipe_set_by_key.get(incoming_key)
        if canonical_set:
            return None, f"unmapped_set_piece:{canonical_set}:{piece}"
        return None, "unmapped"

    def resolve_salvage(self, internal_name: str) -> str | None:
        base = internal_name[2:] if internal_name.startswith("S_") else internal_name
        return self.salvage_by_key.get(canonical_key(base))

    def remember_recipe_alias(self, internal_name: str, canonical_name: str) -> None:
        if canonical_name not in self.recipe_names:
            raise GameMemoryError(f"Cannot remember unknown recipe name: {canonical_name}")
        base, _ = self.base_recipe_internal_name(internal_name)
        self.custom_aliases[base] = canonical_name
        if not self.alias_path:
            return
        self.alias_path.parent.mkdir(parents=True, exist_ok=True)
        self.alias_path.write_text(
            json.dumps(dict(sorted(self.custom_aliases.items())), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


class GameInventoryReader:
    def __init__(
        self,
        db_path: str | Path,
        *,
        alias_path: str | Path | None = None,
        memory_profile_path: str | Path | None = None,
        profile_manager: MemoryProfileManager | None = None,
        allow_session_recovery: bool = True,
        allow_root_recovery: bool = True,
    ):
        self.db_path = Path(db_path)
        self.alias_path = Path(alias_path) if alias_path else None
        self.resolver = MemoryNameResolver(self.db_path, alias_path=self.alias_path)
        self.profile_manager = profile_manager or MemoryProfileManager(
            user_pack_path=memory_profile_path
        )
        self.allow_session_recovery = bool(allow_session_recovery)
        self.allow_root_recovery = bool(allow_root_recovery)

    @staticmethod
    def _validate_header(
        kind: str,
        capacity: int,
        total: int,
        array: int,
        *,
        max_capacity: int,
    ) -> None:
        if capacity < 0 or capacity > max_capacity:
            raise GameMemoryError(
                f"{kind} capacity is implausible ({capacity}); "
                "the game memory layout may have changed."
            )
        if total < 0 or total > capacity:
            raise GameMemoryError(
                f"{kind} total {total} is inconsistent with capacity {capacity}; "
                "the game memory layout may have changed."
            )
        if total > 0 and not array:
            raise GameMemoryError(
                f"{kind} collection pointer is null while total is {total}."
            )

    def _read_inventory_with_profile(
        self,
        mem: ProcessMemory,
        context: _ResolvedMemoryContext,
        profile: MemoryProfile,
    ) -> tuple[int, int, list[MemoryRecipe], int, int, list[MemorySalvage]]:
        character = context.character_address
        character_cfg = profile.structure("character")
        recipe_cfg = character_cfg["recipes"]
        salvage_cfg = character_cfg["salvage"]
        validation = profile.validation()
        max_capacity = as_int(validation["max_inventory_capacity"])

        recipe_array = mem.qword(
            character + as_int(recipe_cfg["collection_offset"])
        )
        recipe_capacity = mem.u32(
            character + as_int(recipe_cfg["capacity_offset"])
        )
        recipe_total = mem.u32(
            character + as_int(recipe_cfg["count_offset"])
        )
        salvage_array = mem.qword(
            character + as_int(salvage_cfg["collection_offset"])
        )
        salvage_capacity = mem.u32(
            character + as_int(salvage_cfg["capacity_offset"])
        )
        salvage_total = mem.u32(
            character + as_int(salvage_cfg["count_offset"])
        )

        self._validate_header(
            "Recipe inventory",
            recipe_capacity,
            recipe_total,
            recipe_array,
            max_capacity=max_capacity,
        )
        self._validate_header(
            "Salvage inventory",
            salvage_capacity,
            salvage_total,
            salvage_array,
            max_capacity=max_capacity,
        )

        recipes = self._read_recipes(
            mem, recipe_array, recipe_total, profile
        )
        salvage = self._read_salvage(
            mem, salvage_array, salvage_total, profile
        )
        return (
            recipe_capacity,
            recipe_total,
            recipes,
            salvage_capacity,
            salvage_total,
            salvage,
        )

    def read(self, process: GameProcessInfo | int) -> MemoryInventorySnapshot:
        pid = process.pid if isinstance(process, GameProcessInfo) else int(process)
        title = process.window_title if isinstance(process, GameProcessInfo) else ""
        recovery_result = None
        root_recovery_result = None

        with ProcessMemory(pid) as mem:
            try:
                context = _resolve_memory_context(
                    mem, self.profile_manager, require_server=False
                )
            except GameMemoryError as original_root_exc:
                if not self.allow_root_recovery:
                    raise

                from .memory_root_recovery import (
                    MemoryRootRecoveryError,
                    recover_root_context,
                )
                try:
                    root_recovery_result = recover_root_context(
                        mem,
                        self.profile_manager,
                        require_server=False,
                    )
                    context = root_recovery_result.context
                except MemoryRootRecoveryError as recovery_exc:
                    raise GameMemoryError(
                        f"{original_root_exc} Conservative session-only root-locator "
                        f"recovery was attempted but not accepted: {recovery_exc}"
                    ) from original_root_exc

            profile = context.profile

            try:
                inventory = self._read_inventory_with_profile(
                    mem, context, profile
                )
            except GameMemoryError as original_exc:
                if not self.allow_session_recovery:
                    raise

                from .memory_recovery import (
                    MemorySessionRecoveryError,
                    recover_inventory_profile,
                )
                try:
                    recovery_result = recover_inventory_profile(
                        mem,
                        context.character_address,
                        profile,
                    )
                except MemorySessionRecoveryError as recovery_exc:
                    raise GameMemoryError(
                        f"{original_exc} Conservative session-only inventory recovery "
                        f"was attempted but not accepted: {recovery_exc}"
                    ) from original_exc

                profile = recovery_result.profile
                try:
                    inventory = self._read_inventory_with_profile(
                        mem, context, profile
                    )
                except GameMemoryError as retry_exc:
                    raise GameMemoryError(
                        f"{original_exc} A bounded session-recovery candidate passed "
                        f"the multi-sample gate, but the full inventory retry failed: "
                        f"{retry_exc}"
                    ) from original_exc

            (
                recipe_capacity,
                recipe_total,
                recipes,
                salvage_capacity,
                salvage_total,
                salvage,
            ) = inventory

        # Preserve the 1.15 snapshot API. The legacy field names now contain the
        # semantically validated Entity and Character addresses respectively.
        return MemoryInventorySnapshot(
            pid=pid,
            window_title=title,
            owner_address=context.entity_address,
            inventory_address=context.character_address,
            recipe_capacity=recipe_capacity,
            recipe_total=recipe_total,
            recipes=recipes,
            salvage_capacity=salvage_capacity,
            salvage_total=salvage_total,
            salvage=salvage,
            memory_profile_id=profile.profile_id,
            memory_profile_version=profile.profile_version,
            memory_profile_source=profile.source,
            memory_recovery_applied=bool(
                recovery_result and recovery_result.applied
            ),
            memory_recovery_kinds=(
                recovery_result.kinds if recovery_result else ()
            ),
            memory_recovery_samples=(
                recovery_result.sample_count if recovery_result else 0
            ),
            memory_root_recovery_applied=bool(
                root_recovery_result and root_recovery_result.applied
            ),
            memory_root_recovery_locators=(
                root_recovery_result.recovered_locators
                if root_recovery_result else ()
            ),
            memory_root_recovery_samples=(
                root_recovery_result.sample_count
                if root_recovery_result else 0
            ),
        )
    def _read_recipes(
        self,
        mem: ProcessMemory,
        array: int,
        total: int,
        profile: MemoryProfile,
    ) -> list[MemoryRecipe]:
        if total == 0:
            return []

        entry_cfg = profile.structure("entries")
        validation = profile.validation()
        max_entries = as_int(validation["max_collection_entries"])
        max_level = as_int(validation["max_recipe_level"])
        max_string = as_int(validation["max_internal_string"])

        definition_offset = as_int(entry_cfg["definition_pointer_offset"])
        quantity_offset = as_int(entry_cfg["quantity_offset"])
        name_offset = as_int(entry_cfg["internal_name_pointer_offset"])
        level_offset = as_int(entry_cfg["recipe_level_offset"])

        out: list[MemoryRecipe] = []
        quantity_sum = 0
        for index in range(max_entries):
            entry = mem.qword(array + index * 8)
            if not entry:
                # The validated collections are contiguous. Stopping at the first
                # null avoids walking from the pointer array into unrelated memory.
                break
            definition = mem.qword(entry + definition_offset)
            quantity = mem.u32(entry + quantity_offset)
            if (
                not definition
                or quantity <= 0
                or quantity > total
                or quantity_sum + quantity > total
            ):
                raise GameMemoryError(
                    "Recipe collection entry failed structural validation; "
                    "no memory inventory was imported."
                )

            name_ptr = mem.qword(definition + name_offset)
            level = mem.u32(definition + level_offset)
            if not name_ptr or level > max_level:
                raise GameMemoryError(
                    "Recipe definition failed semantic validation; "
                    "no memory inventory was imported."
                )

            internal_name = mem.cstring(name_ptr, max_string).strip()
            if not internal_name or "\ufffd" in internal_name:
                raise GameMemoryError(
                    "Recipe internal name failed semantic validation; "
                    "no memory inventory was imported."
                )

            canonical, source = self.resolver.resolve_recipe(internal_name, level)
            out.append(
                MemoryRecipe(
                    internal_name=internal_name,
                    level=level,
                    quantity=quantity,
                    canonical_name=canonical,
                    entry_address=entry,
                    definition_address=definition,
                    mapping_source=source,
                )
            )
            quantity_sum += quantity
            if quantity_sum == total:
                break

        if quantity_sum != total:
            raise GameMemoryError(
                f"Recipe validation failed: enumerated quantities sum to {quantity_sum}, "
                f"but the header total is {total}. No memory inventory was imported."
            )
        return out

    def _read_salvage(
        self,
        mem: ProcessMemory,
        array: int,
        total: int,
        profile: MemoryProfile,
    ) -> list[MemorySalvage]:
        if total == 0:
            return []

        entry_cfg = profile.structure("entries")
        validation = profile.validation()
        max_entries = as_int(validation["max_collection_entries"])
        max_string = as_int(validation["max_internal_string"])

        definition_offset = as_int(entry_cfg["definition_pointer_offset"])
        quantity_offset = as_int(entry_cfg["quantity_offset"])
        name_offset = as_int(entry_cfg["internal_name_pointer_offset"])

        out: list[MemorySalvage] = []
        quantity_sum = 0
        for index in range(max_entries):
            entry = mem.qword(array + index * 8)
            if not entry:
                break
            definition = mem.qword(entry + definition_offset)
            quantity = mem.u32(entry + quantity_offset)
            if (
                not definition
                or quantity <= 0
                or quantity > total
                or quantity_sum + quantity > total
            ):
                raise GameMemoryError(
                    "Salvage collection entry failed structural validation; "
                    "no memory inventory was imported."
                )

            name_ptr = mem.qword(definition + name_offset)
            if not name_ptr:
                raise GameMemoryError(
                    "Salvage definition has a null internal-name pointer; "
                    "no memory inventory was imported."
                )
            internal_name = mem.cstring(name_ptr, max_string).strip()
            if not internal_name or "\ufffd" in internal_name:
                raise GameMemoryError(
                    "Salvage internal name failed semantic validation; "
                    "no memory inventory was imported."
                )

            canonical = self.resolver.resolve_salvage(internal_name)
            out.append(
                MemorySalvage(
                    internal_name=internal_name,
                    quantity=quantity,
                    canonical_name=canonical,
                    entry_address=entry,
                    definition_address=definition,
                )
            )
            quantity_sum += quantity
            # Salvage collection can contain non-capacity currencies after normal
            # invention salvage. The Character header total is the capacity-using
            # salvage count, so stop as soon as that exact semantic total is reached.
            if quantity_sum == total:
                break

        if quantity_sum != total:
            raise GameMemoryError(
                f"Salvage validation failed: enumerated quantities sum to {quantity_sum}, "
                f"but the header total is {total}. No memory inventory was imported."
            )
        return out

def validate_memory_profile_pack_live(
    db_path: str | Path,
    pid: int,
    profile_path: str | Path,
    *,
    alias_path: str | Path | None = None,
) -> dict[str, Any]:
    # Validate one candidate profile pack against a live client with no bundled fallback.
    candidate_path = Path(profile_path)
    if not candidate_path.exists():
        raise GameMemoryError(f"Memory profile candidate does not exist: {candidate_path}")

    no_override = candidate_path.with_name(".field_crafter_no_profile_override.json")
    manager = MemoryProfileManager(
        bundled_path=candidate_path,
        user_pack_path=no_override,
    )

    with ProcessMemory(int(pid)) as mem:
        context = _resolve_memory_context(mem, manager, require_server=True)

    reader = GameInventoryReader(
        db_path,
        alias_path=alias_path,
        profile_manager=manager,
        allow_session_recovery=False,
        allow_root_recovery=False,
    )
    snapshot = reader.read(int(pid))
    if snapshot.memory_recovery_applied:
        raise GameMemoryError(
            "Candidate memory profile validation attempted to use session recovery."
        )
    if snapshot.memory_root_recovery_applied:
        raise GameMemoryError(
            "Candidate memory profile validation attempted to use root recovery."
        )
    if snapshot.memory_profile_id != context.profile.profile_id:
        raise GameMemoryError(
            "Candidate memory profile changed between semantic validation and inventory read."
        )
    return {
        "profile_id": context.profile.profile_id,
        "profile_version": context.profile.profile_version,
        "character_name": context.character_name,
        "server": context.server,
        "recipe_total": snapshot.recipe_total,
        "recipe_capacity": snapshot.recipe_capacity,
        "salvage_total": snapshot.salvage_total,
        "salvage_capacity": snapshot.salvage_capacity,
        "unresolved_recipes": snapshot.unresolved_recipe_count,
        "unresolved_salvage": snapshot.unresolved_salvage_count,
    }

def review_from_memory_snapshot(snapshot: MemoryInventorySnapshot) -> dict[str, Any]:
    recipe_rows: list[dict[str, Any]] = []
    recipe_meta: list[dict[str, Any]] = []
    for item in snapshot.recipes:
        resolved = bool(item.canonical_name)
        display = item.canonical_name or f"[Unresolved] {item.internal_name}"
        recipe_rows.append({
            "recipe": display,
            "quantity": item.quantity,
            "level": item.level,
            "option_index": None,
            "selected": resolved,
        })
        recipe_meta.append({
            "recipe": display,
            "raw_text": item.internal_name,
            "internal_name": item.internal_name,
            "confidence": 1.0 if resolved else 0.0,
            "needs_review": not resolved,
            "source": "game_memory",
            "mapping_source": item.mapping_source,
            "candidates": [],
        })

    inventory: dict[str, int] = {}
    salvage_meta: list[dict[str, Any]] = []
    unresolved_salvage: list[dict[str, Any]] = []
    for item in snapshot.salvage:
        if item.canonical_name:
            inventory[item.canonical_name] = inventory.get(item.canonical_name, 0) + item.quantity
        else:
            unresolved_salvage.append({"internal_name": item.internal_name, "quantity": item.quantity})
        salvage_meta.append({
            "salvage": item.canonical_name or "",
            "raw_text": item.internal_name,
            "internal_name": item.internal_name,
            "quantity": item.quantity,
            "confidence": 1.0 if item.canonical_name else 0.0,
            "quantity_confidence": 1.0,
            "needs_review": not bool(item.canonical_name),
            "source": "game_memory",
        })

    return {
        "schema_version": 1,
        "confirmed": False,
        "needs_review": bool(snapshot.unresolved_recipe_count or snapshot.unresolved_salvage_count),
        "ocr_backend": "game_memory",
        "input_source": "game_memory",
        "recipes": recipe_rows,
        "inventory": inventory,
        "salvage_capacity": {
            "used": snapshot.salvage_total,
            "capacity": snapshot.salvage_capacity,
            "source": "game_memory",
        },
        "disposal_policy": {"allowed_rarities": ["common"]},
        "recognition": {
            "recipes": recipe_meta,
            "salvage": salvage_meta,
            "unresolved_salvage_quantities": unresolved_salvage,
            "capacity_candidates": [],
            "capacity_needs_review": False,
            "images": [],
            "memory": {
                "pid": snapshot.pid,
                "window_title": snapshot.window_title,
                "recipe_total": snapshot.recipe_total,
                "recipe_capacity": snapshot.recipe_capacity,
                "salvage_total": snapshot.salvage_total,
                "salvage_capacity": snapshot.salvage_capacity,
                "memory_profile_id": snapshot.memory_profile_id,
                "memory_profile_version": snapshot.memory_profile_version,
                "memory_profile_source": snapshot.memory_profile_source,
                "memory_recovery_applied": snapshot.memory_recovery_applied,
                "memory_recovery_kinds": list(snapshot.memory_recovery_kinds),
                "memory_recovery_samples": snapshot.memory_recovery_samples,
                "memory_root_recovery_applied": snapshot.memory_root_recovery_applied,
                "memory_root_recovery_locators": list(snapshot.memory_root_recovery_locators),
                "memory_root_recovery_samples": snapshot.memory_root_recovery_samples,
            },
        },
    }

MEMORY_RECIPE_INDEX_URL = "https://homecoming.wiki/wiki/Recipe_AttribFileItem_ID_Numbers%2C_Names%2C_and_Aspects"
_ATTRIB_LINE_RE = re.compile(r"^\s*\d+\s+([A-Za-z0-9_]+_[A-Z]_\d{1,2})\s+(.+?)\s*$")


def _aspect_key(value: str) -> tuple[str, ...]:
    text = value.replace("_", " ").replace("-Res", "Resistance Debuff")
    text = re.sub(r"\(Superior\)", "", text, flags=re.I)
    replacements = {
        "endurance reduction": "endurance",
        "stun duration": "stun",
        "hold duration": "hold",
        "sleep duration": "sleep",
        "confuse duration": "confuse",
        "immobilization duration": "immobilize",
        "immobilize duration": "immobilize",
        "fear duration": "fear",
        "to hit debuff": "tohit debuff",
        "to hit buff": "tohit",
        "defense debuff": "defense debuff",
        "defense buff": "defense",
        "healing": "heal",
    }
    low = text.lower()
    for src, dst in replacements.items():
        low = low.replace(src, dst)
    # Recipe pages sometimes express the same multi-aspect enhancement in a different
    # order. Compare a normalized token multiset rather than sequence order.
    tokens = re.findall(r"[a-z0-9]+", low)
    return tuple(sorted(tokens))


def _canonical_recipe_aspect(name: str) -> str:
    return name.split(":", 1)[1].strip() if ":" in name else name


def _internal_stem_guess(set_name: str) -> str:
    """Best-effort CoH internal set stem for sets absent from the legacy Attrib index."""
    value = set_name.replace("’", "'").replace("'", "")
    value = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return value


def _set_piece_from_order(base: str, ordered_members: dict[str, list[str]]) -> str | None:
    sm = _SET_PIECE_RE.match(base)
    if not sm:
        return None
    set_key = canonical_key(sm.group("set"))
    members = ordered_members.get(set_key)
    if not members:
        return None
    idx = ord(sm.group("piece")) - ord("A")
    if idx < 0 or idx >= len(members):
        return None
    return members[idx]


def _recipe_label_key(value: str) -> str:
    value = re.sub(r"\s*\(Superior\)\s*$", "", value, flags=re.I)
    return canonical_key(value)


def _extract_ordered_set_members(
    html: str,
    set_name: str,
    canonical_members: list[str],
) -> list[str]:
    """Extract the set's member order from its individual Wiki page.

    The page link labels are treated as the ordering authority. Matching ignores a
    trailing '(Superior)' because some set pages show the base enhancement label while
    Field Crafter stores the Superior recipe name.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise GameMemoryError("BeautifulSoup is required to parse Homecoming set pages.") from exc

    soup = BeautifulSoup(html, "html.parser")
    content = soup.select_one(".mw-parser-output") or soup
    by_key: dict[str, str] = {}
    for recipe in canonical_members:
        by_key.setdefault(_recipe_label_key(recipe), recipe)

    ordered: list[str] = []
    prefix_key = canonical_key(set_name)
    for anchor in content.find_all("a"):
        label = anchor.get_text(" ", strip=True)
        if not label or ":" not in label:
            continue
        # Avoid unrelated links on the page before doing the more expensive key lookup.
        if canonical_key(label.split(":", 1)[0]) != prefix_key:
            continue
        recipe = by_key.get(_recipe_label_key(label))
        if recipe and recipe not in ordered:
            ordered.append(recipe)
            if len(ordered) == len(canonical_members):
                break
    return ordered


def _wiki_title_url(title: str) -> str:
    from urllib.parse import quote
    return "https://homecoming.wiki/wiki/" + quote(title.replace(" ", "_"), safe="_()'/:+")


def _load_recipe_sets(db_path: str | Path) -> tuple[dict[str, str], dict[str, list[str]]]:
    db_path = Path(db_path)
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT set_name, name FROM recipes WHERE set_name IS NOT NULL ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    set_by_key: dict[str, str] = {}
    recipes_by_set: dict[str, list[str]] = {}
    for set_name, recipe_name in rows:
        set_name = str(set_name)
        recipe_name = str(recipe_name)
        set_by_key.setdefault(canonical_key(set_name), set_name)
        recipes_by_set.setdefault(set_name, []).append(recipe_name)
    return set_by_key, recipes_by_set


def validate_memory_recipe_alias_coverage(
    db_path: str | Path,
    alias_path: str | Path,
) -> dict[str, Any]:
    """Validate bundled memory-map coverage against the canonical recipe DB.

    This is intentionally stricter than a set-family count. Every canonical set
    recipe must be represented by at least one A-F internal alias, and every common
    IO mapping hard-coded in the reader must point at a canonical common recipe.
    """
    set_by_key, recipes_by_set = _load_recipe_sets(db_path)
    alias_path = Path(alias_path)
    try:
        raw = json.loads(alias_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "complete": False,
            "error": f"Could not read memory alias map: {exc}",
            "set_count": len(set_by_key),
            "covered_set_count": 0,
            "member_count": sum(len(v) for v in recipes_by_set.values()),
            "covered_member_count": 0,
            "missing_sets": sorted(recipes_by_set),
            "missing_members": [],
            "invalid_aliases": [],
            "common_complete": False,
            "missing_common": [],
        }

    if not isinstance(raw, dict):
        return {
            "complete": False,
            "error": "Memory alias map is not a JSON object.",
            "set_count": len(set_by_key),
            "covered_set_count": 0,
            "member_count": sum(len(v) for v in recipes_by_set.values()),
            "covered_member_count": 0,
            "missing_sets": sorted(recipes_by_set),
            "missing_members": [],
            "invalid_aliases": [],
            "common_complete": False,
            "missing_common": [],
        }

    recipe_to_set_key: dict[str, str] = {}
    expected_by_key: dict[str, set[str]] = {}
    for set_name, members in recipes_by_set.items():
        key = canonical_key(set_name)
        expected_by_key[key] = set(members)
        for recipe in members:
            recipe_to_set_key[recipe] = key

    mapped_by_key: dict[str, set[str]] = {key: set() for key in expected_by_key}
    invalid_aliases: list[str] = []
    seen_piece_targets: dict[tuple[str, str], str] = {}
    for alias, recipe in raw.items():
        if not isinstance(alias, str) or not isinstance(recipe, str):
            invalid_aliases.append(str(alias))
            continue
        sm = _SET_PIECE_RE.match(alias)
        set_key = recipe_to_set_key.get(recipe)
        if not sm or not set_key:
            invalid_aliases.append(alias)
            continue
        piece = sm.group("piece")
        stem_key = canonical_key(sm.group("set"))
        pair = (stem_key, piece)
        prior = seen_piece_targets.get(pair)
        if prior is not None and prior != recipe:
            invalid_aliases.append(alias)
            continue
        seen_piece_targets[pair] = recipe
        mapped_by_key.setdefault(set_key, set()).add(recipe)

    missing_members: list[str] = []
    missing_sets: list[str] = []
    covered_set_count = 0
    covered_member_count = 0
    for set_name, members in recipes_by_set.items():
        key = canonical_key(set_name)
        mapped = mapped_by_key.get(key, set())
        covered_member_count += len(mapped & set(members))
        missing = sorted(set(members) - mapped, key=str.casefold)
        if missing:
            missing_sets.append(set_name)
            missing_members.extend(missing)
        else:
            covered_set_count += 1

    uri = f"file:{Path(db_path).resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        common_db = {
            str(row[0]) for row in conn.execute(
                "SELECT name FROM recipes WHERE recipe_type='common_io'"
            )
        }
    finally:
        conn.close()
    common_mapped = {
        recipe for recipe in _COMMON_IO_INTERNAL_TO_CANONICAL.values()
        if recipe in common_db
    }
    missing_common = sorted(common_db - common_mapped, key=str.casefold)

    complete = not missing_sets and not invalid_aliases and not missing_common
    return {
        "complete": complete,
        "error": "",
        "set_count": len(recipes_by_set),
        "covered_set_count": covered_set_count,
        "member_count": sum(len(v) for v in recipes_by_set.values()),
        "covered_member_count": covered_member_count,
        "missing_sets": sorted(missing_sets, key=str.casefold),
        "missing_members": missing_members,
        "invalid_aliases": sorted(set(invalid_aliases), key=str.casefold),
        "common_complete": not missing_common,
        "common_count": len(common_db),
        "covered_common_count": len(common_db) - len(missing_common),
        "missing_common": missing_common,
    }


def _parse_attrib_set_rows(text: str) -> list[tuple[str, str, str, str]]:
    """Return (full internal id, base id, set display key, aspect) rows from Attrib text."""
    rows: list[tuple[str, str, str, str]] = []
    for raw_line in text.splitlines():
        m = _ATTRIB_LINE_RE.match(raw_line.strip())
        if not m:
            continue
        internal_full, aspect = m.groups()
        base, _level = MemoryNameResolver.base_recipe_internal_name(internal_full)
        sm = _SET_PIECE_RE.match(base)
        if not sm:
            continue
        rows.append((internal_full, base, canonical_key(sm.group("set")), aspect))
    return rows


def build_memory_recipe_aliases_from_attrib_text(
    db_path: str | Path,
    text: str,
    *,
    ordered_members: dict[str, list[str]] | None = None,
) -> dict[str, str]:
    """Map the Wiki internal A-F IDs to canonical Field Crafter recipes.

    Aspect text is used when it matches cleanly. If the legacy Attrib list uses stale
    terminology, the A-F ordinal can be resolved from an individual set page supplied
    through ordered_members.
    """
    set_by_key, recipes_by_set = _load_recipe_sets(db_path)
    ordered_members = ordered_members or {}
    aliases: dict[str, str] = {}

    for _internal_full, base, raw_set_key, aspect in _parse_attrib_set_rows(text):
        set_name = set_by_key.get(raw_set_key)
        if not set_name:
            continue
        # Current set-page ordering is authoritative when available. This avoids
        # stale legacy aspect labels such as Healing vs Healing/Absorb and malformed
        # legacy entries such as Miracle_F.
        ordinal = _set_piece_from_order(base, ordered_members)
        if ordinal:
            aliases[base] = ordinal
            continue
        target_key = _aspect_key(aspect)
        matches = [
            recipe for recipe in recipes_by_set.get(set_name, [])
            if _aspect_key(_canonical_recipe_aspect(recipe)) == target_key
        ]
        if len(matches) == 1:
            aliases[base] = matches[0]
    return aliases


def refresh_memory_recipe_aliases(
    db_path: str | Path,
    alias_path: str | Path,
    *,
    url: str = MEMORY_RECIPE_INDEX_URL,
    progress: Callable[[str], None] | None = None,
    refresh_index: bool = True,
) -> dict[str, Any]:
    """Refresh the internal recipe-ID map without hammering Homecoming Wiki.

    The AttribFileItem index is used for everything it can resolve locally. Only sets
    absent from that index, or sets whose legacy aspect labels cannot be matched to the
    current Field Crafter database, require an individual set-page fetch. Requests go
    through MediaWikiClient so they are cached, paced, maxlag-aware, and retried when
    the Wiki responds with a transient 429/5xx status.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise GameMemoryError(
            "Refreshing the game-memory recipe map requires BeautifulSoup. "
            "Use the normal Field Crafter setup/build with database-update dependencies installed."
        ) from exc

    from .client import MediaWikiClient

    progress = progress or (lambda _message: None)
    alias_path = Path(alias_path)
    cache_dir = alias_path.parent / "wiki_cache" / "memory_recipe_map"
    client = MediaWikiClient(
        cache_dir=cache_dir,
        user_agent=f"FieldCrafter/{APP_VERSION} (personal Homecoming crafting utility)",
        delay_seconds=0.45,
        timeout=45.0,
        max_retries=7,
    )

    progress("Reading the Homecoming recipe internal-ID index...")
    try:
        index_page = client.parse_page(
            "Recipe AttribFileItem ID Numbers, Names, and Aspects",
            refresh=refresh_index,
            fetch_timestamp=False,
        )
    except Exception as exc:
        raise GameMemoryError(
            "Could not read the Homecoming recipe internal-ID index. "
            f"The existing memory recipe map was left unchanged. Details: {exc}"
        ) from exc
    text = BeautifulSoup(index_page.html, "html.parser").get_text("\n")

    set_by_key, recipes_by_set = _load_recipe_sets(db_path)
    all_set_keys = set(set_by_key)
    index_rows = _parse_attrib_set_rows(text)
    index_set_keys = {set_key for _, _, set_key, _ in index_rows if set_key in set_by_key}

    provisional = build_memory_recipe_aliases_from_attrib_text(db_path, text)
    # Seeded aliases are already validated and avoid unnecessary page requests for
    # known stale terminology such as Panacea.
    resolver_for_seeds = MemoryNameResolver(db_path)
    for base, canonical in resolver_for_seeds.factory_aliases.items():
        if canonical in resolver_for_seeds.recipe_names:
            provisional.setdefault(base, canonical)
    for base, canonical in _SEED_RECIPE_ALIASES.items():
        if canonical in resolver_for_seeds.recipe_names:
            provisional.setdefault(base, canonical)

    # Work out which sets are completely covered by local index matching. A set is
    # only considered complete when every canonical member stored in Field Crafter's
    # database has a corresponding internal A-F alias. This prevents a partially
    # matched legacy index (for example, one stale Panacea aspect label) from being
    # reported as complete merely because another piece in the same set matched.
    canonical_to_set_key: dict[str, str] = {}
    canonical_members_by_key: dict[str, set[str]] = {}
    for set_name, members in recipes_by_set.items():
        key = canonical_key(set_name)
        canonical_members_by_key[key] = set(members)
        for recipe in members:
            canonical_to_set_key[recipe] = key

    mapped_members_by_key: dict[str, set[str]] = {key: set() for key in all_set_keys}
    for recipe in provisional.values():
        set_key = canonical_to_set_key.get(recipe)
        if set_key:
            mapped_members_by_key.setdefault(set_key, set()).add(recipe)

    incomplete_from_index = {
        key for key in all_set_keys
        if mapped_members_by_key.get(key, set()) != canonical_members_by_key.get(key, set())
    }

    # Cross-check the complete local recipe DB. This automatically catches both the
    # omissions documented on the old index, future sets added after that note, and
    # older rows whose aspect terminology no longer matches the current set recipe.
    missing_from_index = all_set_keys - index_set_keys
    pages_needed = incomplete_from_index | missing_from_index | (all_set_keys & _HEALING_ORDINAL_SET_KEYS)

    ordered_members: dict[str, list[str]] = {}
    pages_scraped: list[str] = []
    stem_by_set_key: dict[str, str] = {}

    for _internal_full, base, set_key, _aspect in index_rows:
        if set_key not in set_by_key:
            continue
        sm = _SET_PIECE_RE.match(base)
        if sm:
            stem_by_set_key.setdefault(set_key, sm.group("set"))

    total_pages = len(pages_needed)
    for page_number, set_key in enumerate(sorted(pages_needed), start=1):
        display_name = set_by_key[set_key]
        canonical_members = recipes_by_set.get(display_name, [])
        if not canonical_members:
            raise GameMemoryError(f"No canonical recipe members are stored for set {display_name}.")
        progress(f"Updating recipe map - {page_number} of {total_pages} set pages: {display_name}")
        try:
            # Cached pages are reused. A later explicit refresh of the main index will
            # still discover newly added sets without re-downloading every old set page.
            page = client.parse_page(display_name, refresh=False, fetch_timestamp=False)
            members = _extract_ordered_set_members(page.html, display_name, canonical_members)
        except Exception as exc:
            raise GameMemoryError(
                f"Could not read Homecoming set page for {display_name}. "
                "The existing memory recipe map was left unchanged. "
                f"Details: {exc}"
            ) from exc
        if len(members) != len(canonical_members):
            raise GameMemoryError(
                f"Homecoming set page for {display_name} did not yield the complete ordered recipe list "
                f"(found {len(members)} of {len(canonical_members)}). Existing aliases were left unchanged."
            )
        internal_stem = stem_by_set_key.get(set_key) or _internal_stem_guess(display_name)
        ordered_members[canonical_key(internal_stem)] = members
        ordered_members[set_key] = members
        stem_by_set_key[set_key] = internal_stem
        pages_scraped.append(display_name)

    discovered = build_memory_recipe_aliases_from_attrib_text(
        db_path, text, ordered_members=ordered_members
    )
    # Keep bundled and validated seed aliases in the refreshed map as well.
    for base, canonical in resolver_for_seeds.factory_aliases.items():
        if canonical in resolver_for_seeds.recipe_names:
            discovered.setdefault(base, canonical)
    for base, canonical in _SEED_RECIPE_ALIASES.items():
        if canonical in resolver_for_seeds.recipe_names:
            discovered.setdefault(base, canonical)

    supplemental = 0
    supplemental_sets: list[str] = []
    for set_key in sorted(missing_from_index):
        display_name = set_by_key[set_key]
        members = ordered_members.get(set_key)
        stem = stem_by_set_key.get(set_key) or _internal_stem_guess(display_name)
        if not members:
            continue
        supplemental_sets.append(display_name)
        for idx, recipe in enumerate(members):
            discovered[f"{stem}_{chr(ord('A') + idx)}"] = recipe
            supplemental += 1

    final_members_by_key: dict[str, set[str]] = {key: set() for key in all_set_keys}
    for recipe in discovered.values():
        set_key = canonical_to_set_key.get(recipe)
        if set_key:
            final_members_by_key.setdefault(set_key, set()).add(recipe)
    incomplete_set_keys = sorted(
        key for key in all_set_keys
        if final_members_by_key.get(key, set()) != canonical_members_by_key.get(key, set())
    )
    if incomplete_set_keys:
        details: list[str] = []
        for key in incomplete_set_keys[:8]:
            missing_members = sorted(
                canonical_members_by_key[key] - final_members_by_key.get(key, set()),
                key=str.casefold,
            )
            sample = ", ".join(missing_members[:2])
            if len(missing_members) > 2:
                sample += f" (+{len(missing_members) - 2} more)"
            details.append(f"{set_by_key[key]} [{sample}]")
        more = "" if len(incomplete_set_keys) <= 8 else f" (+{len(incomplete_set_keys) - 8} more sets)"
        raise GameMemoryError(
            "Game-memory recipe map coverage is incomplete after cross-checking every recipe member in the local database. "
            f"Incomplete sets: {'; '.join(details)}{more}. Existing aliases were left unchanged."
        )
    covered_set_keys = all_set_keys

    resolver = MemoryNameResolver(db_path, alias_path=alias_path)
    before = len(resolver.custom_aliases)
    resolver.custom_aliases.update(discovered)
    alias_path.parent.mkdir(parents=True, exist_ok=True)
    alias_path.write_text(
        json.dumps(dict(sorted(resolver.custom_aliases.items())), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    progress("Game-memory recipe map update complete.")
    return {
        "source": url,
        "db_set_count": len(all_set_keys),
        "attrib_set_count": len(index_set_keys),
        "mapped_alias_count": len(discovered),
        "supplemental_alias_count": supplemental,
        "sets_missing_from_index": supplemental_sets,
        "set_pages_scraped": pages_scraped,
        "saved_aliases_before": before,
        "saved_aliases_after": len(resolver.custom_aliases),
        "set_coverage": f"{len(covered_set_keys)}/{len(all_set_keys)}",
        "cache_dir": str(cache_dir),
        "note": (
            "Field Crafter cross-checked the legacy Attrib index against every set in the local recipe database. "
            "Stale aspect labels use set-page A-F order, and DB sets missing from the index are filled from "
            "their individual Homecoming Wiki set pages. Wiki requests are cached, paced, and retried."
        ),
    }

