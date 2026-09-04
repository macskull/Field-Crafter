from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from hc_recipe_db.game_memory import (
    GameInventoryReader,
    GameMemoryError,
    MemoryNameResolver,
)
from hc_recipe_db import memory_diagnostics, memory_structural_diagnostics
from hc_recipe_db.salvage_semantics import invention_salvage_membership


class FakeProfile:
    def structure(self, name: str):
        if name != "entries":
            raise KeyError(name)
        return {
            "definition_pointer_offset": 0,
            "quantity_offset": 8,
            "internal_name_pointer_offset": 0,
            "recipe_level_offset": 16,
        }

    def validation(self):
        return {
            "max_collection_entries": 64,
            "max_internal_string": 192,
            "max_recipe_level": 53,
        }


class FakeMemory:
    def __init__(self):
        self.qwords: dict[int, int] = {}
        self.u32s: dict[int, int] = {}
        self.strings: dict[int, str] = {}

    def qword(self, address: int) -> int:
        if address not in self.qwords:
            raise RuntimeError(f"unmapped qword 0x{address:X}")
        return self.qwords[address]

    def u32(self, address: int) -> int:
        if address not in self.u32s:
            raise RuntimeError(f"unmapped u32 0x{address:X}")
        return self.u32s[address]

    def cstring(self, address: int, _max_len: int) -> str:
        if address not in self.strings:
            raise RuntimeError(f"unmapped string 0x{address:X}")
        return self.strings[address]


def build_memory(rows: list[tuple[str, int]]):
    mem = FakeMemory()
    array = 0x1000
    entries: list[int] = []
    for index, (name, quantity) in enumerate(rows):
        entry = 0x2000 + index * 0x100
        definition = 0x8000 + index * 0x100
        name_ptr = 0xC000 + index * 0x100
        entries.append(entry)
        mem.qwords[array + index * 8] = entry
        mem.qwords[entry] = definition
        mem.u32s[entry + 8] = quantity
        mem.qwords[definition] = name_ptr
        mem.strings[name_ptr] = name
    mem.qwords[array + len(rows) * 8] = 0
    return mem, array, entries


def make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE recipes(name TEXT, set_name TEXT)")
        conn.execute("CREATE TABLE salvage(name TEXT)")
        conn.executemany(
            "INSERT INTO salvage(name) VALUES (?)",
            [("Legit One",), ("Legit Two",), ("Legit Three",)],
        )
        conn.commit()
    finally:
        conn.close()


def assert_raises_memory_error(func) -> None:
    try:
        func()
    except GameMemoryError:
        return
    raise AssertionError("expected GameMemoryError")


def main() -> int:
    profile = FakeProfile()
    with tempfile.TemporaryDirectory(prefix="field_crafter_salvage_test_") as td:
        db_path = Path(td) / "test.sqlite"
        make_db(db_path)
        resolver = MemoryNameResolver(db_path)
        reader = GameInventoryReader.__new__(GameInventoryReader)
        reader.resolver = resolver

        classify = lambda name: invention_salvage_membership(name, db_path=db_path)
        memory_diagnostics.default_invention_salvage_membership = classify
        memory_structural_diagnostics.default_invention_salvage_membership = classify

        assert classify("S_Legit_One") is True
        assert classify("S_GeneticSample") is False
        assert classify("S_HVSuperPackSalvage") is False

        # Classification regression: unrelated S_* objects encountered before
        # the semantic stopping boundary must be ignored, while canonical
        # invention salvage still reproduces the authoritative header exactly.
        rows = [
            ("S_Legit_One", 100),
            ("S_GeneticSample", 1),
            ("S_HVSuperPackSalvage", 999),
            ("S_Legit_Two", 46),
        ]
        mem, array, entries = build_memory(rows)
        salvage = reader._read_salvage(mem, array, 146, profile)
        assert [(x.canonical_name, x.quantity) for x in salvage] == [
            ("Legit One", 100),
            ("Legit Two", 46),
        ]
        assert sum(x.quantity for x in salvage) == 146
        assert all(x.canonical_name for x in salvage)

        probe = memory_diagnostics._probe_collection(
            mem, array, 146, profile, kind="salvage"
        )
        assert probe["quantity_matches_header"] is True
        assert probe["quantity_sum"] == 146
        ignored = [
            row for row in probe["entries"]
            if row.get("counts_toward_header_total") is False
        ]
        assert {row.get("internal_name") for row in ignored} >= {
            "S_GeneticSample", "S_HVSuperPackSalvage"
        }

        quantity_scan = memory_structural_diagnostics._scan_quantity_offsets(
            mem,
            entries,
            146,
            profile,
            kind="salvage",
            definition_offset=0,
            name_offset=0,
        )
        winner = quantity_scan.get("winner") or {}
        assert quantity_scan["clear_winner"] is True
        assert int(str(winner["offset"]), 0) == 8
        assert winner["quantity_sum"] == 146
        assert winner["ignored_non_invention_entries"] == 2

        # Non-invention quantities are not bounded by the invention header.
        mem2, array2, _ = build_memory([
            ("S_GeneticSample", 500000),
            ("S_Legit_One", 146),
        ])
        salvage2 = reader._read_salvage(mem2, array2, 146, profile)
        assert len(salvage2) == 1 and salvage2[0].quantity == 146

        # A true invention-salvage mismatch must still fail closed.
        mem3, array3, _ = build_memory([
            ("S_Legit_One", 100),
            ("S_GeneticSample", 1),
            ("S_Legit_Two", 45),
        ])
        assert_raises_memory_error(
            lambda: reader._read_salvage(mem3, array3, 146, profile)
        )
        probe3 = memory_diagnostics._probe_collection(
            mem3, array3, 146, profile, kind="salvage"
        )
        assert probe3["quantity_matches_header"] is False
        assert probe3["quantity_sum"] == 145

        # The invention-salvage header is the semantic stopping boundary. The
        # game's pointer array can contain poison/stale slots after the live rows;
        # once canonical invention salvage exactly reproduces the header, the
        # production reader must not dereference later slots.
        mem4, array4, _ = build_memory([
            ("S_GeneticSample", 1),
            ("S_Legit_One", 100),
            ("S_Legit_Two", 46),
        ])
        mem4.qwords[array4 + 3 * 8] = 0xDEDEDEDEDEDEDEDE
        salvage4 = reader._read_salvage(mem4, array4, 146, profile)
        assert sum(x.quantity for x in salvage4) == 146

        probe4 = memory_diagnostics._probe_collection(
            mem4, array4, 146, profile, kind="salvage"
        )
        assert probe4["quantity_matches_header"] is True
        assert probe4["stopped_reason"] == "header_total_reproduced"

    print("PASS: invention-salvage classification regression tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
