from __future__ import annotations

import copy
import math
import time
from dataclasses import dataclass
from typing import Any

from .memory_profiles import MemoryProfile, MemoryProfileManager, as_int


# FIELD_CRAFTER_MEMORY_STRUCTURAL_RECOVERY_V7
# FIELD_CRAFTER_MEMORY_STRUCTURAL_RECOVERY_V7_1
#
# Conservative session-only structural recovery.
#
# This layer is intentionally downstream of the signed/current profile and the
# narrower v4/v5/v5.2 recovery mechanisms. It may only construct an in-memory
# derived profile after a coherent object/layout hypothesis passes strict semantic
# gates across three fresh samples. Nothing recovered here is persisted.
#
# Signed candidate-profile validation MUST disable this layer.

RECOVERY_SAMPLE_COUNT = 3
RECOVERY_SAMPLE_DELAY_SECONDS = 0.06

MIN_ROSTER_ENTITY_SCORE_MARGIN = 5.0
MIN_ENTITY_CHARACTER_SCORE_MARGIN = 4.0
MIN_ENTRY_PAIR_SCORE_MARGIN = 5.0
MIN_JOINT_HYPOTHESIS_MARGIN = 5.0

ROOT_LOCATORS = ("identity", "server", "roster")


class MemoryStructuralRecoveryError(RuntimeError):
    pass


class MemoryStructuralRecoveryNotNeeded(MemoryStructuralRecoveryError):
    """The signed/current deeper structure already matches the live client."""


@dataclass(frozen=True, slots=True)
class StructuralRecoveryContext:
    profile: MemoryProfile
    character_name: str
    server: str
    entity_address: int
    character_address: int


@dataclass(frozen=True, slots=True)
class MemoryStructuralRecoveryResult:
    context: StructuralRecoveryContext
    profile: MemoryProfile
    recovered_fields: tuple[str, ...]
    recovered_root_locators: tuple[str, ...]
    sample_count: int
    evidence_summary: dict[str, Any]

    @property
    def applied(self) -> bool:
        return bool(self.recovered_fields)

    @property
    def summary(self) -> str:
        if not self.recovered_fields:
            return ""
        if len(self.recovered_fields) == 1:
            return self.recovered_fields[0]
        if len(self.recovered_fields) == 2:
            return " and ".join(self.recovered_fields)
        return ", ".join(self.recovered_fields[:-1]) + (
            f", and {self.recovered_fields[-1]}"
        )


def structural_recovery_policy_summary() -> dict[str, Any]:
    return {
        "sample_count": RECOVERY_SAMPLE_COUNT,
        "sample_delay_seconds": RECOVERY_SAMPLE_DELAY_SECONDS,
        "minimum_roster_entity_score_margin": MIN_ROSTER_ENTITY_SCORE_MARGIN,
        "minimum_entity_character_score_margin": MIN_ENTITY_CHARACTER_SCORE_MARGIN,
        "minimum_entry_pair_score_margin": MIN_ENTRY_PAIR_SCORE_MARGIN,
        "minimum_joint_hypothesis_margin": MIN_JOINT_HYPOTHESIS_MARGIN,
        "identity_name_exact_match_required": True,
        "roster_name_exact_match_required": True,
        "entity_name_exact_match_required": True,
        "vitals_common_shift_required": True,
        "inventory_header_total_reproduction_required": True,
        "recipe_namespace_proof_required": True,
        "salvage_namespace_proof_required": True,
        "recipe_level_suffix_proof_required": True,
        "three_sample_exact_layout_stability_required": True,
        "full_production_inventory_retry_required": True,
        "persistent": False,
        "signed_candidate_validation_may_use_recovery": False,
        "profile_precedence_allowed_only_for_identical_recovered_layout": True,
    }


def _int_hex(value: Any) -> int:
    if isinstance(value, int):
        return int(value)
    return int(str(value), 0)


def _candidate_margin(
    candidates: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    winner: dict[str, Any],
    score_key: str = "score",
) -> float | None:
    winner_score = float(winner.get(score_key) or 0.0)
    others = [
        float(item.get(score_key) or 0.0)
        for item in candidates
        if item is not winner
        and item != winner
    ]
    if not others:
        return None
    return winner_score - max(others)


def _require_pointer_margin(
    label: str,
    scan: dict[str, Any],
    *,
    minimum_margin: float,
) -> float | None:
    if not scan.get("clear_winner"):
        raise MemoryStructuralRecoveryError(
            f"{label} does not have a clear structural winner."
        )
    winner = scan.get("winner") or {}
    if not winner:
        raise MemoryStructuralRecoveryError(
            f"{label} clear-winner flag has no winner object."
        )
    margin = _candidate_margin(
        list(scan.get("candidates") or []),
        winner=winner,
    )
    if margin is not None and margin < float(minimum_margin):
        raise MemoryStructuralRecoveryError(
            f"{label} winner margin {margin:.3f} is below "
            f"{float(minimum_margin):.3f}."
        )
    return margin


def _resolve_roster_code_anchor(
    mem,
    profile: MemoryProfile,
    roster_code,
) -> dict[str, Any]:
    from .game_memory import _rip_relative_target_rva

    locator = profile.locator("roster")
    roster_base_rva = _rip_relative_target_rva(
        mem,
        roster_code.match_rva,
        disp_offset=as_int(locator["base_disp_offset"]),
        instruction_end=as_int(locator["base_instruction_end"]),
    )
    roster_count_rva = _rip_relative_target_rva(
        mem,
        roster_code.match_rva,
        disp_offset=as_int(locator["count_disp_offset"]),
        instruction_end=as_int(locator["count_instruction_end"]),
    )

    module_size = int(mem.module_size)
    if not (
        0 <= roster_base_rva < module_size
        and 0 <= roster_count_rva < module_size
    ):
        raise MemoryStructuralRecoveryError(
            "Recovered roster code anchor resolved outside cityofheroes.exe."
        )

    raw_count = int(mem.u32(mem.base + roster_count_rva))
    max_roster = as_int(profile.validation()["max_roster_count"])
    if not (1 <= raw_count <= max_roster):
        raise MemoryStructuralRecoveryError(
            f"Recovered roster count {raw_count} is outside 1..{max_roster}."
        )

    return {
        "roster_base_rva": roster_base_rva,
        "roster_count_rva": roster_count_rva,
        "raw_roster_count": raw_count,
    }


def _exact_name_winner(
    label: str,
    scan: dict[str, Any],
    *,
    trusted_name: str,
) -> dict[str, Any]:
    if not scan.get("clear_winner"):
        raise MemoryStructuralRecoveryError(
            f"{label} does not have exactly one trusted-name match."
        )
    winner = scan.get("winner") or {}
    if str(winner.get("value") or "") != trusted_name:
        raise MemoryStructuralRecoveryError(
            f"{label} winner does not equal the trusted identity name."
        )
    return winner


def _empty_expected_header_is_safe(
    mem,
    character: int,
    profile: MemoryProfile,
    *,
    kind: str,
) -> bool:
    from .memory_structural_diagnostics import (
        _joint_raw_header_candidates,
        _raw_inventory_header,
    )

    cfg = profile.structure("character")[kind]
    expected = as_int(cfg["collection_offset"])
    header = _raw_inventory_header(
        mem,
        character,
        profile,
        kind=kind,
        collection_offset=expected,
    )
    if not header.get("plausible") or int(header.get("total") or 0) != 0:
        return False

    # If there is any populated raw header nearby, a missing semantic winner is
    # unsafe: it could be a moved populated inventory that we failed to prove.
    populated = _joint_raw_header_candidates(
        mem,
        character,
        profile,
        kind=kind,
    )
    return not populated


def _entry_layout_for_kind(
    mem,
    character: int,
    profile: MemoryProfile,
    *,
    kind: str,
    block: dict[str, Any],
) -> dict[str, Any]:
    if not block.get("available"):
        if _empty_expected_header_is_safe(
            mem,
            character,
            profile,
            kind=kind,
        ):
            return {
                "available": False,
                "legitimate_empty_at_expected_header": True,
            }
        raise MemoryStructuralRecoveryError(
            f"{kind} entry layout has no clear semantic anchor, but a safe "
            "legitimate-empty interpretation could not be proven."
        )

    if not block.get("header_anchor_semantic_clear"):
        raise MemoryStructuralRecoveryError(
            f"{kind} inventory header is not a clear semantic anchor."
        )

    header = block.get("header") or {}
    anchor_source = str(block.get("header_anchor_source") or "")
    if anchor_source == "joint_header_entry_hypothesis":
        margin = header.get("joint_hypothesis_score_margin")
        runner = header.get("joint_hypothesis_runner_up")
        if runner is not None:
            if margin is None or float(margin) < MIN_JOINT_HYPOTHESIS_MARGIN:
                raise MemoryStructuralRecoveryError(
                    f"{kind} joint header+entry hypothesis margin is below "
                    f"{MIN_JOINT_HYPOTHESIS_MARGIN:.3f}."
                )
    elif anchor_source != "type_aware_strong_winner":
        raise MemoryStructuralRecoveryError(
            f"{kind} inventory header used unsupported semantic anchor "
            f"{anchor_source!r}."
        )

    quantity_scan = block.get("quantity_offset_scan") or {}
    if not quantity_scan.get("clear_winner"):
        raise MemoryStructuralRecoveryError(
            f"{kind} Entry quantity offset is not a clear winner."
        )
    quantity = quantity_scan.get("winner") or {}

    pair_scan = block.get("definition_and_name_offset_scan") or {}
    if not pair_scan.get("clear_winner"):
        raise MemoryStructuralRecoveryError(
            f"{kind} Definition/internal-name offsets are not a clear winner."
        )
    pair = pair_scan.get("winner") or {}
    pair_margin = _candidate_margin(
        list(pair_scan.get("candidates") or []),
        winner=pair,
    )
    if (
        pair_margin is not None
        and pair_margin < MIN_ENTRY_PAIR_SCORE_MARGIN
    ):
        raise MemoryStructuralRecoveryError(
            f"{kind} Definition/internal-name winner margin "
            f"{pair_margin:.3f} is below "
            f"{MIN_ENTRY_PAIR_SCORE_MARGIN:.3f}."
        )
    if int(pair.get("namespace_matches") or 0) < 2:
        raise MemoryStructuralRecoveryError(
            f"{kind} Definition/internal-name winner has fewer than two "
            "type-compatible namespace matches."
        )
    if int(pair.get("namespace_mismatches") or 0) != 0:
        raise MemoryStructuralRecoveryError(
            f"{kind} Definition/internal-name winner has namespace mismatches."
        )

    result = {
        "available": True,
        "header_offset": _int_hex(header["offset"]),
        "header_anchor_source": anchor_source,
        "quantity_offset": _int_hex(quantity["offset"]),
        "definition_pointer_offset": _int_hex(
            pair["definition_pointer_offset"]
        ),
        "internal_name_pointer_offset": _int_hex(
            pair["internal_name_pointer_offset"]
        ),
        "namespace_matches": int(pair.get("namespace_matches") or 0),
        "pair_score": float(pair.get("score") or 0.0),
    }

    if kind == "recipes":
        level_scan = block.get("recipe_level_offset_scan") or {}
        if not level_scan.get("clear_winner"):
            raise MemoryStructuralRecoveryError(
                "Recipe level offset is not a clear winner."
            )
        level = level_scan.get("winner") or {}
        if float(level.get("confidence") or 0.0) < 0.75:
            raise MemoryStructuralRecoveryError(
                "Recipe level winner does not meet the 0.75 suffix agreement gate."
            )
        if int(level.get("matched_level_suffixes") or 0) < 2:
            raise MemoryStructuralRecoveryError(
                "Recipe level winner has fewer than two suffix matches."
            )
        result["recipe_level_offset"] = _int_hex(level["offset"])
        result["recipe_level_confidence"] = float(
            level.get("confidence") or 0.0
        )

    return result


def _extract_structural_sample(
    mem,
    profile: MemoryProfile,
    *,
    identity: dict[str, Any],
    server: dict[str, Any],
    roster_anchor: dict[str, Any],
) -> dict[str, Any]:
    from .memory_structural_diagnostics import (
        collect_structural_drift_evidence,
    )

    trusted_name = str(identity["name"])
    roster_observation = {
        "candidates": [{
            "match_rva": None,
            "roster_base_rva": hex(int(roster_anchor["roster_base_rva"])),
            "roster_count_rva": hex(int(roster_anchor["roster_count_rva"])),
        }]
    }

    evidence = collect_structural_drift_evidence(
        mem,
        profile,
        trusted_identity_names={trusted_name},
        roster_observation=roster_observation,
    )
    if evidence.get("status") != "collected":
        raise MemoryStructuralRecoveryError(
            "Structural evidence could not be collected: "
            f"{evidence.get('reason') or evidence.get('status')}"
        )

    record = evidence.get("roster_record") or {}
    roster_name = _exact_name_winner(
        "Roster record name",
        record.get("name_offset_scan") or {},
        trusted_name=trusted_name,
    )

    entity_scan = record.get("entity_pointer_offset_scan") or {}
    entity_margin = _require_pointer_margin(
        "Roster Entity pointer",
        entity_scan,
        minimum_margin=MIN_ROSTER_ENTITY_SCORE_MARGIN,
    )
    entity_winner = entity_scan.get("winner") or {}
    entity = _int_hex(entity_winner["entity_pointer"])

    entity_name = _exact_name_winner(
        "Entity name",
        entity_winner.get("entity_name_offset_scan") or {},
        trusted_name=trusted_name,
    )

    character_scan = (
        entity_winner.get("character_pointer_offset_scan") or {}
    )
    character_margin = _require_pointer_margin(
        "Entity Character pointer",
        character_scan,
        minimum_margin=MIN_ENTITY_CHARACTER_SCORE_MARGIN,
    )
    character_winner = character_scan.get("winner") or {}
    character = _int_hex(character_winner["character_pointer"])

    vitals_scan = character_winner.get("vitals_common_shift") or {}
    if not vitals_scan.get("clear_winner"):
        raise MemoryStructuralRecoveryError(
            "Character vitals do not have one clear common-shift winner."
        )
    vitals = vitals_scan.get("winner") or {}
    vitals_shift = int(vitals["shift"])

    entries = evidence.get("entries") or {}
    recipe_layout = _entry_layout_for_kind(
        mem,
        character,
        profile,
        kind="recipes",
        block=entries.get("recipes") or {},
    )
    salvage_layout = _entry_layout_for_kind(
        mem,
        character,
        profile,
        kind="salvage",
        block=entries.get("salvage") or {},
    )

    available = [
        item
        for item in (recipe_layout, salvage_layout)
        if item.get("available")
    ]

    shared: dict[str, int] = {}
    for key in (
        "quantity_offset",
        "definition_pointer_offset",
        "internal_name_pointer_offset",
    ):
        values = {
            int(item[key])
            for item in available
            if key in item
        }
        if len(values) > 1:
            raise MemoryStructuralRecoveryError(
                f"Recipe and salvage disagree on shared Entry field {key}."
            )
        if values:
            shared[key] = values.pop()

    if not available:
        # This is still useful for object-graph/vitals recovery when both
        # inventories are legitimately empty. Shared entry fields remain signed.
        entry_evidence_available = False
    else:
        entry_evidence_available = True
        required_shared = {
            "quantity_offset",
            "definition_pointer_offset",
            "internal_name_pointer_offset",
        }
        if not required_shared.issubset(shared):
            raise MemoryStructuralRecoveryError(
                "Available inventory evidence did not prove all shared Entry fields."
            )

    recipe_level_offset = None
    if recipe_layout.get("available"):
        recipe_level_offset = int(recipe_layout["recipe_level_offset"])

    return {
        "identity_name": trusted_name,
        "server": str(server.get("server") or ""),
        "identity_xyz_rva": int(identity["xyz_rva"]),
        "server_target_rva": server.get("target_rva"),
        "roster_base_rva": int(roster_anchor["roster_base_rva"]),
        "roster_count_rva": int(roster_anchor["roster_count_rva"]),
        "raw_roster_count": int(roster_anchor["raw_roster_count"]),
        "roster_name_offset": _int_hex(roster_name["offset"]),
        "roster_entity_pointer_offset": _int_hex(
            entity_winner["offset"]
        ),
        "entity_address": entity,
        "entity_name_offset": _int_hex(entity_name["offset"]),
        "entity_character_pointer_offset": _int_hex(
            character_winner["offset"]
        ),
        "character_address": character,
        "vitals_common_shift": vitals_shift,
        "recipe_layout": recipe_layout,
        "salvage_layout": salvage_layout,
        "shared_entry_layout": shared,
        "recipe_level_offset": recipe_level_offset,
        "entry_evidence_available": entry_evidence_available,
        "margins": {
            "roster_entity_pointer": entity_margin,
            "entity_character_pointer": character_margin,
            "recipe_joint_hypothesis": (
                (recipe_layout.get("available")
                 and (entries.get("recipes") or {}).get("header", {})
                 .get("joint_hypothesis_score_margin"))
                or None
            ),
            "salvage_joint_hypothesis": (
                (salvage_layout.get("available")
                 and (entries.get("salvage") or {}).get("header", {})
                 .get("joint_hypothesis_score_margin"))
                or None
            ),
        },
    }


def _apply_sample_to_profile(
    profile: MemoryProfile,
    sample: dict[str, Any],
) -> tuple[MemoryProfile, tuple[str, ...]]:
    data = copy.deepcopy(profile.data)
    structures = data["structures"]

    recovered: list[str] = []

    def set_if_changed(
        obj: dict[str, Any],
        key: str,
        value: int,
        label: str,
    ) -> None:
        old = as_int(obj[key])
        if old != int(value):
            obj[key] = int(value)
            recovered.append(label)

    roster = structures["roster"]
    set_if_changed(
        roster,
        "name_offset",
        int(sample["roster_name_offset"]),
        "roster name",
    )
    set_if_changed(
        roster,
        "entity_pointer_offset",
        int(sample["roster_entity_pointer_offset"]),
        "roster Entity pointer",
    )

    entity = structures["entity"]
    set_if_changed(
        entity,
        "name_offset",
        int(sample["entity_name_offset"]),
        "Entity name",
    )
    set_if_changed(
        entity,
        "character_pointer_offset",
        int(sample["entity_character_pointer_offset"]),
        "Entity Character pointer",
    )

    character = structures["character"]
    vitals_shift = int(sample["vitals_common_shift"])
    if vitals_shift:
        for key in (
            "current_hp_offset",
            "current_end_offset",
            "max_hp_offset",
            "max_end_offset",
        ):
            character[key] = as_int(character[key]) + vitals_shift
        recovered.append("Character vitals")

    for kind, sample_key, label in (
        ("recipes", "recipe_layout", "recipe header"),
        ("salvage", "salvage_layout", "salvage header"),
    ):
        layout = sample[sample_key]
        if not layout.get("available"):
            continue
        cfg = character[kind]
        old_collection = as_int(cfg["collection_offset"])
        capacity_delta = as_int(cfg["capacity_offset"]) - old_collection
        count_delta = as_int(cfg["count_offset"]) - old_collection
        new_collection = int(layout["header_offset"])
        if new_collection != old_collection:
            cfg["collection_offset"] = new_collection
            cfg["capacity_offset"] = new_collection + capacity_delta
            cfg["count_offset"] = new_collection + count_delta
            recovered.append(label)

    shared = sample["shared_entry_layout"]
    entries = structures["entries"]
    if shared:
        set_if_changed(
            entries,
            "quantity_offset",
            int(shared["quantity_offset"]),
            "Entry quantity",
        )
        set_if_changed(
            entries,
            "definition_pointer_offset",
            int(shared["definition_pointer_offset"]),
            "Entry Definition pointer",
        )
        set_if_changed(
            entries,
            "internal_name_pointer_offset",
            int(shared["internal_name_pointer_offset"]),
            "Definition internal-name pointer",
        )

    if sample.get("recipe_level_offset") is not None:
        set_if_changed(
            entries,
            "recipe_level_offset",
            int(sample["recipe_level_offset"]),
            "recipe level",
        )

    # Keep stable ordering and remove duplicate labels if multiple related fields
    # collapsed into one human-facing recovery category.
    unique: list[str] = []
    for item in recovered:
        if item not in unique:
            unique.append(item)

    data["_structural_session_recovery"] = {
        "applied": bool(unique),
        "persistent": False,
        "recovered_fields": unique,
        "sample_count": RECOVERY_SAMPLE_COUNT,
        "policy": structural_recovery_policy_summary(),
    }

    derived = MemoryProfile(
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        priority=profile.priority,
        source=f"{profile.source}+structural-session-recovery",
        data=data,
    )
    return derived, tuple(unique)


def _sample_signature(sample: dict[str, Any]) -> tuple[Any, ...]:
    def layout_signature(layout: dict[str, Any]) -> tuple[Any, ...]:
        if not layout.get("available"):
            return (
                False,
                bool(layout.get("legitimate_empty_at_expected_header")),
            )
        return (
            True,
            int(layout["header_offset"]),
            str(layout["header_anchor_source"]),
            int(layout["quantity_offset"]),
            int(layout["definition_pointer_offset"]),
            int(layout["internal_name_pointer_offset"]),
            int(layout.get("recipe_level_offset") or -1),
        )

    return (
        str(sample["identity_name"]),
        str(sample["server"]),
        int(sample["identity_xyz_rva"]),
        sample["server_target_rva"],
        int(sample["roster_base_rva"]),
        int(sample["roster_count_rva"]),
        int(sample["roster_name_offset"]),
        int(sample["roster_entity_pointer_offset"]),
        int(sample["entity_address"]),
        int(sample["entity_name_offset"]),
        int(sample["entity_character_pointer_offset"]),
        int(sample["character_address"]),
        int(sample["vitals_common_shift"]),
        layout_signature(sample["recipe_layout"]),
        layout_signature(sample["salvage_layout"]),
        tuple(sorted(sample["shared_entry_layout"].items())),
        sample.get("recipe_level_offset"),
    )


def recover_structural_context_for_profile(
    mem,
    profile: MemoryProfile,
    *,
    require_server: bool = False,
) -> MemoryStructuralRecoveryResult:
    from .memory_root_recovery import (
        MemoryRootRecoveryError,
        _resolve_identity_semantics,
        _resolve_server_semantics,
        _select_code_candidate,
    )

    try:
        identity_code = _select_code_candidate(
            mem,
            profile,
            "identity",
        )
        roster_code = _select_code_candidate(
            mem,
            profile,
            "roster",
        )
    except MemoryRootRecoveryError as exc:
        raise MemoryStructuralRecoveryError(str(exc)) from exc

    server_code = None
    try:
        server_code = _select_code_candidate(
            mem,
            profile,
            "server",
        )
    except MemoryRootRecoveryError as exc:
        if require_server:
            raise MemoryStructuralRecoveryError(str(exc)) from exc

    recovered_root_locators = tuple(
        candidate.locator
        for candidate in (
            identity_code,
            server_code,
            roster_code,
        )
        if candidate is not None
        and candidate.source != "exact_signature"
    )

    samples: list[dict[str, Any]] = []
    for sample_index in range(RECOVERY_SAMPLE_COUNT):
        try:
            identity = _resolve_identity_semantics(
                mem,
                profile,
                identity_code,
            )
            server = {
                "server": "",
                "target_rva": None,
            }
            if server_code is not None:
                server = _resolve_server_semantics(
                    mem,
                    profile,
                    server_code,
                )
        except MemoryRootRecoveryError as exc:
            raise MemoryStructuralRecoveryError(str(exc)) from exc

        roster_anchor = _resolve_roster_code_anchor(
            mem,
            profile,
            roster_code,
        )

        sample = _extract_structural_sample(
            mem,
            profile,
            identity=identity,
            server=server,
            roster_anchor=roster_anchor,
        )
        samples.append(sample)

        if sample_index + 1 < RECOVERY_SAMPLE_COUNT:
            time.sleep(RECOVERY_SAMPLE_DELAY_SECONDS)

    first_signature = _sample_signature(samples[0])
    for index, sample in enumerate(samples[1:], start=2):
        if _sample_signature(sample) != first_signature:
            raise MemoryStructuralRecoveryError(
                "Recovered structural layout changed during validation sample "
                f"{index}/{RECOVERY_SAMPLE_COUNT}."
            )

    first = samples[0]
    if require_server and not str(first["server"]):
        raise MemoryStructuralRecoveryError(
            "Server identity is required but structural recovery produced none."
        )

    derived, recovered_fields = _apply_sample_to_profile(
        profile,
        first,
    )

    if not recovered_fields:
        raise MemoryStructuralRecoveryNotNeeded(
            "The live structural layout matches the signed/current profile."
        )

    summary = {
        "recovered_fields": list(recovered_fields),
        "recovered_root_locators": list(recovered_root_locators),
        "margins": first["margins"],
        "entry_evidence_available": bool(
            first["entry_evidence_available"]
        ),
        "recipe_anchor_source": (
            first["recipe_layout"].get("header_anchor_source")
            if first["recipe_layout"].get("available") else None
        ),
        "salvage_anchor_source": (
            first["salvage_layout"].get("header_anchor_source")
            if first["salvage_layout"].get("available") else None
        ),
    }

    return MemoryStructuralRecoveryResult(
        context=StructuralRecoveryContext(
            profile=derived,
            character_name=str(first["identity_name"]),
            server=str(first["server"]),
            entity_address=int(first["entity_address"]),
            character_address=int(first["character_address"]),
        ),
        profile=derived,
        recovered_fields=recovered_fields,
        recovered_root_locators=recovered_root_locators,
        sample_count=RECOVERY_SAMPLE_COUNT,
        evidence_summary=summary,
    )


def _result_identity_key(
    result: MemoryStructuralRecoveryResult,
) -> tuple[Any, ...]:
    profile = result.profile
    structures = profile.data["structures"]
    roster = structures["roster"]
    entity = structures["entity"]
    character = structures["character"]
    entries = structures["entries"]

    return (
        result.context.character_name,
        result.context.server,
        int(result.context.entity_address),
        int(result.context.character_address),
        as_int(roster["name_offset"]),
        as_int(roster["entity_pointer_offset"]),
        as_int(entity["name_offset"]),
        as_int(entity["character_pointer_offset"]),
        as_int(character["current_hp_offset"]),
        as_int(character["current_end_offset"]),
        as_int(character["max_hp_offset"]),
        as_int(character["max_end_offset"]),
        as_int(character["recipes"]["collection_offset"]),
        as_int(character["recipes"]["capacity_offset"]),
        as_int(character["recipes"]["count_offset"]),
        as_int(character["salvage"]["collection_offset"]),
        as_int(character["salvage"]["capacity_offset"]),
        as_int(character["salvage"]["count_offset"]),
        as_int(entries["definition_pointer_offset"]),
        as_int(entries["quantity_offset"]),
        as_int(entries["internal_name_pointer_offset"]),
        as_int(entries["recipe_level_offset"]),
    )


def recover_structural_context(
    mem,
    profile_manager: MemoryProfileManager,
    *,
    require_server: bool = False,
) -> MemoryStructuralRecoveryResult:
    accepted: list[MemoryStructuralRecoveryResult] = []
    errors: list[str] = []
    not_needed = 0

    for profile in profile_manager.candidates():
        try:
            result = recover_structural_context_for_profile(
                mem,
                profile,
                require_server=require_server,
            )
            accepted.append(result)
        except MemoryStructuralRecoveryNotNeeded:
            not_needed += 1
        except MemoryStructuralRecoveryError as exc:
            errors.append(
                f"{profile.profile_id}: {exc}"
            )

    if not accepted:
        if not_needed:
            raise MemoryStructuralRecoveryNotNeeded(
                "No structural field required session recovery."
            )
        detail = "; ".join(errors[:4])
        raise MemoryStructuralRecoveryError(
            "No memory profile passed conservative structural recovery"
            + (f": {detail}" if detail else ".")
        )

    first_key = _result_identity_key(accepted[0])
    conflicting = [
        result
        for result in accepted[1:]
        if _result_identity_key(result) != first_key
    ]
    if conflicting:
        raise MemoryStructuralRecoveryError(
            "Multiple memory profiles recovered different live structural layouts."
        )

    # Preserve MemoryProfileManager precedence when user/downloaded and bundled
    # profiles independently converge on the identical live object/layout graph.
    return accepted[0]
