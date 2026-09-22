from __future__ import annotations

from pathlib import Path

from hc_recipe_db.game_memory import (
    GameInventoryReader,
    _ResolvedMemoryContext,
    _resolve_direct_player,
)
from hc_recipe_db.memory_profiles import load_profile_pack

ROOT = Path(__file__).resolve().parents[1]


class FakeMemory:
    def __init__(self):
        self.base = 0x140000000
        self.module_size = 0x02000000
        self._qword = {}
        self._u32 = {}
        self._i32 = {}
        self._f32 = {}
        self._byte = {}
        self._string = {}
        self._sig = {}
        self._blob = {}

    def signature_hits(self, pattern):
        return list(self._sig.get(pattern, []))

    def qword(self, address):
        if address not in self._qword:
            raise RuntimeError(f"unmapped qword 0x{address:X}")
        return self._qword[address]

    def u32(self, address):
        if address not in self._u32:
            raise RuntimeError(f"unmapped u32 0x{address:X}")
        return self._u32[address]

    def i32(self, address):
        if address not in self._i32:
            raise RuntimeError(f"unmapped i32 0x{address:X}")
        return self._i32[address]

    def f32(self, address):
        if address not in self._f32:
            raise RuntimeError(f"unmapped f32 0x{address:X}")
        return self._f32[address]

    def cstring(self, address, _max_len):
        if address not in self._string:
            raise RuntimeError(f"unmapped string 0x{address:X}")
        return self._string[address]

    def read(self, address, size):
        key = (address, size)
        if key in self._blob:
            return self._blob[key]
        if size == 1 and address in self._byte:
            return bytes([self._byte[address]])
        raise RuntimeError(f"unmapped blob 0x{address:X}+{size}")


class Resolver:
    def resolve_recipe(self, internal_name, level):
        return f"Recipe:{internal_name}:{level}", "fixture"

    def resolve_salvage(self, internal_name):
        if internal_name.startswith("S_"):
            return internal_name[2:].replace("_", " ")
        return None


def rip_disp(match, instruction_end, target):
    return int(target - (match + instruction_end))


def seed_direct_player(mem, profile):
    player = profile.locator("player")
    table = profile.locator("entity_table")
    player_match = 0x330000
    table_match = 0x330180
    mem._sig[player["pattern"]] = [player_match]
    mem._sig[table["pattern"]] = [table_match]

    primary_rva = 0xBE5000
    fallback_rva = 0xBE5008
    selector_rva = 0xBE5010
    alternate_rva = 0xBE5018
    player_targets = [
        ("primary_disp_offset", "primary_instruction_end", primary_rva),
        ("fallback_disp_offset", "fallback_instruction_end", fallback_rva),
        ("selector_disp_offset", "selector_instruction_end", selector_rva),
        ("alternate_disp_offset", "alternate_instruction_end", alternate_rva),
        ("alternate2_disp_offset", "alternate2_instruction_end", alternate_rva),
    ]
    for disp_key, end_key, target in player_targets:
        mem._i32[mem.base + player_match + int(player[disp_key], 0)] = rip_disp(
            player_match, int(player[end_key], 0), target
        )

    count_rva = 0x11D0000
    flags_rva = 0x11CE000
    pointer_table_rva = 0x11D0100
    mem._i32[mem.base + table_match + int(table["count_disp_offset"], 0)] = rip_disp(
        table_match, int(table["count_instruction_end"], 0), count_rva
    )
    flags_encoded = flags_rva - int(table["flags_target_adjust"], 0)
    mem._i32[mem.base + table_match + int(table["flags_disp_offset"], 0)] = rip_disp(
        table_match, int(table["flags_instruction_end"], 0), flags_encoded
    )
    table_encoded = pointer_table_rva - int(table["table_target_adjust"], 0)
    mem._i32[mem.base + table_match + int(table["table_disp_offset"], 0)] = rip_disp(
        table_match, int(table["table_instruction_end"], 0), table_encoded
    )

    entity = 0x0000050000100000
    character = 0x0000050000200000
    powersets = 0x0000050000300000
    mem._qword[mem.base + primary_rva] = 0
    mem._qword[mem.base + fallback_rva] = entity
    mem._i32[mem.base + selector_rva] = 0
    mem._qword[mem.base + alternate_rva] = 0x0000050000400000

    entity_cfg = profile.structure("entity")
    char_cfg = profile.structure("character")
    mem._qword[entity + int(entity_cfg["character_pointer_offset"], 0)] = character
    mem._u32[entity + int(entity_cfg["index_offset"], 0)] = 3
    mem._string[entity + int(entity_cfg["name_offset"], 0)] = "Fixture Hero"
    mem._u32[character + int(char_cfg["trained_level_offset"], 0)] = 49
    mem._qword[character + int(char_cfg["powerset_array_offset"], 0)] = powersets
    mem._u32[powersets + int(char_cfg["earray_count_from_data_offset"], 0)] = 45

    mem._i32[mem.base + count_rva] = 8
    rows = [0] * 8
    rows[3] = entity
    mem._blob[(mem.base + pointer_table_rva, 8 * 8)] = b"".join(
        int(x).to_bytes(8, "little") for x in rows
    )
    mem._byte[mem.base + flags_rva + 3] = 1
    return entity, character


def seed_inventory(mem, profile, character):
    # Make only the post-beta +0x10 layout valid. Use totals greater than capacity
    # to prove stack quantities are not incorrectly compared to slot capacity.
    layouts = {x["id"]: x for x in profile.inventory_layouts()}
    chosen = layouts["post_beta_2026_09_plus_0x10"]
    old = layouts["pre_patch_2026_09"]
    for kind in ("recipes", "salvage"):
        ocfg = old[kind]
        mem._qword[character + int(ocfg["collection_offset"], 0)] = 0
        mem._u32[character + int(ocfg["capacity_offset"], 0)] = 0
        mem._u32[character + int(ocfg["count_offset"], 0)] = 0

    arrays = {"recipes": 0x0000050000500000, "salvage": 0x0000050000600000}
    totals = {"recipes": 5, "salvage": 6}
    capacities = {"recipes": 2, "salvage": 2}
    for kind in ("recipes", "salvage"):
        cfg = chosen[kind]
        mem._qword[character + int(cfg["collection_offset"], 0)] = arrays[kind]
        mem._u32[character + int(cfg["capacity_offset"], 0)] = capacities[kind]
        mem._u32[character + int(cfg["count_offset"], 0)] = totals[kind]

    entries = profile.structure("entries")
    def_off = int(entries["definition_pointer_offset"], 0)
    qty_off = int(entries["quantity_offset"], 0)
    name_off = int(entries["internal_name_pointer_offset"], 0)
    level_off = int(entries["recipe_level_offset"], 0)

    def add(kind, index, name, qty, level=0):
        array = arrays[kind]
        entry = 0x0000050000700000 + (0 if kind == "recipes" else 0x10000) + index * 0x100
        definition = entry + 0x40
        name_ptr = entry + 0x80
        mem._qword[array + index * 8] = entry
        mem._qword[entry + def_off] = definition
        mem._u32[entry + qty_off] = qty
        mem._qword[definition + name_off] = name_ptr
        mem._string[name_ptr] = name
        mem._u32[definition + level_off] = level

    add("recipes", 0, "Invention_Accuracy_50", 2, 50)
    add("recipes", 1, "Invention_Damage_50", 3, 50)
    add("salvage", 0, "S_AlchemicalSilver", 4)
    add("salvage", 1, "S_AlienBloodSample", 2)


def main():
    _, profiles = load_profile_pack(ROOT / "data" / "memory_profiles.json", source="test")
    profile = profiles[0]
    mem = FakeMemory()
    entity, character = seed_direct_player(mem, profile)
    name, resolved_entity, resolved_character = _resolve_direct_player(mem, profile)
    assert name == "Fixture Hero"
    assert resolved_entity == entity
    assert resolved_character == character

    # Inventory-specific test uses a lower trained level so the known level-50
    # minimum capacities do not intentionally reject the tiny synthetic fixture.
    char_cfg = profile.structure("character")
    mem._u32[character + int(char_cfg["trained_level_offset"], 0)] = 9
    seed_inventory(mem, profile, character)
    reader = GameInventoryReader.__new__(GameInventoryReader)
    reader.resolver = Resolver()
    context = _ResolvedMemoryContext(profile, "Fixture Hero", "Fixture", entity, character)
    result = reader._read_inventory_with_profile(mem, context, profile)
    recipe_capacity, recipe_total, recipes, salvage_capacity, salvage_total, salvage = result
    assert recipe_capacity == 2 and recipe_total == 5 and sum(x.quantity for x in recipes) == 5
    assert salvage_capacity == 2 and salvage_total == 6 and sum(x.quantity for x in salvage) == 6
    print("PASS: direct-player resolver and multi-layout/dynamic-capacity tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
