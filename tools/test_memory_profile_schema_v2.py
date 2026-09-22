from __future__ import annotations

import json
import tempfile
from pathlib import Path

from hc_recipe_db.memory_profiles import MemoryProfileManager, load_profile_pack

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    version, profiles = load_profile_pack(ROOT / "data" / "memory_profiles.json", source="bundled")
    assert version == "2026.09.21.1"
    assert len(profiles) == 1
    profile = profiles[0]
    assert profile.uses_direct_player_resolver()
    assert [x["id"] for x in profile.inventory_layouts()] == [
        "pre_patch_2026_09",
        "post_beta_2026_09_plus_0x10",
    ]

    # Schema-v1 packs remain readable for migration/backward-compatible tooling.
    legacy = json.loads((ROOT / "memory" / "field-crafter-memory-definitions-2026.09.01.1.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="field_crafter_profile_v2_") as td:
        path = Path(td) / "legacy.json"
        path.write_text(json.dumps(legacy), encoding="utf-8")
        legacy_version, legacy_profiles = load_profile_pack(path, source="user")
        assert legacy_version == "2026.09.01.1"
        assert not legacy_profiles[0].uses_direct_player_resolver()

        # When an old downloaded v1 pack coexists with the new bundled v2 pack,
        # the higher-generation direct resolver must be considered first.
        manager = MemoryProfileManager(
            bundled_path=ROOT / "data" / "memory_profiles.json",
            user_pack_path=path,
        )
        candidates = manager.candidates()
        assert candidates[0].uses_direct_player_resolver()
        assert candidates[0].priority > candidates[1].priority

    print("PASS: memory-profile schema-v2 and legacy compatibility tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
