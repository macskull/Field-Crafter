from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Iterable

from .memory_profiles import MemoryProfile, MemoryProfileManager, as_int, parse_signature


# FIELD_CRAFTER_MEMORY_ROOT_RECOVERY_V5
#
# Root recovery is deliberately narrower than the diagnostics nearest-landmark
# reporting. It may only use a single, uniquely best code landmark and must prove
# the original profile's downstream semantics across three fresh samples.
#
# Nothing recovered here is persisted. Signed candidate-profile validation disables
# this layer completely.

RECOVERY_SAMPLE_COUNT = 3
RECOVERY_SAMPLE_DELAY_SECONDS = 0.06

MAX_FIXED_MISMATCHES = 3
MAX_FIXED_MISMATCH_RATIO = 0.12
MIN_FIXED_BYTE_COUNT = 12
MIN_ANCHOR_RUN = 3
MAX_ANCHOR_RUNS = 8
MAX_ANCHOR_HITS = 2048
MAX_CANDIDATE_STARTS = 8192

MAX_REPORTED_CODE_CANDIDATES = 8


class MemoryRootRecoveryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _CodeCandidate:
    locator: str
    match_rva: int
    source: str
    mismatch_count: int
    fixed_byte_count: int
    anchor_run_length: int

    @property
    def match_ratio(self) -> float:
        if self.fixed_byte_count <= 0:
            return 0.0
        return (
            self.fixed_byte_count - self.mismatch_count
        ) / self.fixed_byte_count


@dataclass(frozen=True, slots=True)
class RootRecoveryContext:
    profile: MemoryProfile
    character_name: str
    server: str
    entity_address: int
    character_address: int


@dataclass(frozen=True, slots=True)
class MemoryRootRecoveryResult:
    context: RootRecoveryContext
    recovered_locators: tuple[str, ...]
    sample_count: int
    code_evidence: tuple[dict[str, Any], ...]

    @property
    def applied(self) -> bool:
        return bool(self.recovered_locators)

    @property
    def summary(self) -> str:
        names = list(self.recovered_locators)
        if not names:
            return ""
        if len(names) == 1:
            return names[0]
        if len(names) == 2:
            return f"{names[0]} and {names[1]}"
        return ", ".join(names[:-1]) + f", and {names[-1]}"


def root_recovery_policy_summary() -> dict[str, Any]:
    return {
        "sample_count": RECOVERY_SAMPLE_COUNT,
        "sample_delay_seconds": RECOVERY_SAMPLE_DELAY_SECONDS,
        "max_fixed_mismatches": MAX_FIXED_MISMATCHES,
        "max_fixed_mismatch_ratio": MAX_FIXED_MISMATCH_RATIO,
        "min_fixed_byte_count": MIN_FIXED_BYTE_COUNT,
        "unique_best_code_candidate_required": True,
        "identity_roster_entity_name_agreement_required": True,
        "vitals_plausibility_required": True,
        "persistent": False,
        "signed_candidate_validation_may_use_recovery": False,
        "inventory_header_recovery": False,
        "profile_precedence_allowed_only_for_identical_root_context": True,
    }


def _fixed_runs(
    pattern: str,
) -> tuple[tuple[int | None, ...], list[tuple[int, bytes]]]:
    parsed = parse_signature(pattern)
    runs: list[tuple[int, bytes]] = []
    index = 0
    while index < len(parsed):
        if parsed[index] is None:
            index += 1
            continue
        start = index
        values: list[int] = []
        while index < len(parsed) and parsed[index] is not None:
            values.append(int(parsed[index]))
            index += 1
        if len(values) >= MIN_ANCHOR_RUN:
            runs.append((start, bytes(values)))

    runs.sort(key=lambda item: (-len(item[1]), item[0]))
    return parsed, runs[:MAX_ANCHOR_RUNS]


def _nearest_code_candidates(
    mem,
    *,
    locator: str,
    pattern: str,
) -> list[_CodeCandidate]:
    parsed, runs = _fixed_runs(pattern)
    fixed_positions = [
        (index, int(value))
        for index, value in enumerate(parsed)
        if value is not None
    ]
    if len(fixed_positions) < MIN_FIXED_BYTE_COUNT:
        raise MemoryRootRecoveryError(
            f"{locator} signature has only {len(fixed_positions)} fixed bytes; "
            f"{MIN_FIXED_BYTE_COUNT} are required for root recovery."
        )
    if not runs:
        raise MemoryRootRecoveryError(
            f"{locator} signature has no usable fixed-byte anchor run."
        )

    max_ratio_mismatches = max(
        1,
        int(math.floor(len(fixed_positions) * MAX_FIXED_MISMATCH_RATIO)),
    )
    max_mismatches = min(MAX_FIXED_MISMATCHES, max_ratio_mismatches)

    candidate_starts: dict[tuple[int, int], int] = {}
    snapshots = list(mem.executable_snapshot())

    for section_rva, data in snapshots:
        for run_offset, blob in runs:
            cursor = 0
            hits = 0
            while hits < MAX_ANCHOR_HITS:
                hit = data.find(blob, cursor)
                if hit < 0:
                    break
                cursor = hit + 1
                hits += 1
                start = hit - run_offset
                if start < 0 or start + len(parsed) > len(data):
                    continue
                key = (int(section_rva), int(start))
                candidate_starts[key] = max(
                    candidate_starts.get(key, 0),
                    len(blob),
                )
                if len(candidate_starts) >= MAX_CANDIDATE_STARTS:
                    break
            if len(candidate_starts) >= MAX_CANDIDATE_STARTS:
                break
        if len(candidate_starts) >= MAX_CANDIDATE_STARTS:
            break

    by_section = {int(rva): data for rva, data in snapshots}
    ranked: list[_CodeCandidate] = []
    for (section_rva, start), anchor_len in candidate_starts.items():
        data = by_section.get(section_rva)
        if data is None:
            continue
        mismatches = sum(
            1
            for index, expected in fixed_positions
            if data[start + index] != expected
        )
        if mismatches > max_mismatches:
            continue
        ranked.append(
            _CodeCandidate(
                locator=locator,
                match_rva=section_rva + start,
                source="nearest_masked_landmark",
                mismatch_count=mismatches,
                fixed_byte_count=len(fixed_positions),
                anchor_run_length=anchor_len,
            )
        )

    ranked.sort(
        key=lambda item: (
            item.mismatch_count,
            -item.anchor_run_length,
            item.match_rva,
        )
    )
    return ranked[:MAX_REPORTED_CODE_CANDIDATES]


def _select_code_candidate(
    mem,
    profile: MemoryProfile,
    locator_name: str,
) -> _CodeCandidate:
    locator = profile.locator(locator_name)
    pattern = str(locator["pattern"])
    exact_hits = list(mem.signature_hits(pattern))

    if exact_hits:
        if len(exact_hits) != 1:
            raise MemoryRootRecoveryError(
                f"{locator_name} still has {len(exact_hits)} exact signature hits; "
                "root recovery will not guess among exact matches."
            )
        parsed = parse_signature(pattern)
        fixed_count = sum(1 for value in parsed if value is not None)
        return _CodeCandidate(
            locator=locator_name,
            match_rva=int(exact_hits[0]),
            source="exact_signature",
            mismatch_count=0,
            fixed_byte_count=fixed_count,
            anchor_run_length=fixed_count,
        )

    nearest = _nearest_code_candidates(
        mem,
        locator=locator_name,
        pattern=pattern,
    )
    if not nearest:
        raise MemoryRootRecoveryError(
            f"{locator_name} has zero exact hits and no sufficiently close "
            "masked code landmark."
        )

    best = nearest[0]
    if len(nearest) > 1 and nearest[1].mismatch_count == best.mismatch_count:
        tied = [
            item
            for item in nearest
            if item.mismatch_count == best.mismatch_count
        ]
        raise MemoryRootRecoveryError(
            f"{locator_name} has {len(tied)} equally good nearest code landmarks "
            f"at {best.mismatch_count} fixed-byte mismatch(es)."
        )

    if best.mismatch_count <= 0:
        raise MemoryRootRecoveryError(
            f"{locator_name} nearest-landmark recovery was invoked even though "
            "the best candidate has zero fixed-byte mismatches."
        )
    return best


def _resolve_identity_semantics(
    mem,
    profile: MemoryProfile,
    candidate: _CodeCandidate,
) -> dict[str, Any]:
    from .game_memory import (
        _rip_relative_target_rva,
        _valid_identity_string,
    )

    locator = profile.locator("identity")
    if str(locator.get("resolver")) != "identity_xyz_relative_v1":
        raise MemoryRootRecoveryError(
            f"Unsupported identity resolver for v5: {locator.get('resolver')!r}"
        )

    xyz_rva = _rip_relative_target_rva(
        mem,
        candidate.match_rva,
        disp_offset=as_int(locator["disp_offset"]),
        instruction_end=as_int(locator["instruction_end"]),
        target_adjust=as_int(locator.get("target_adjust", 0)),
    )
    if not (0 <= xyz_rva < int(mem.module_size)):
        raise MemoryRootRecoveryError(
            "Recovered identity landmark resolved outside cityofheroes.exe."
        )

    coords = [
        mem.f32(mem.base + xyz_rva + delta)
        for delta in (0, 4, 8)
    ]
    max_abs = float(locator.get("position_max_abs", 1000000))
    if not all(
        math.isfinite(value) and abs(value) <= max_abs
        for value in coords
    ):
        raise MemoryRootRecoveryError(
            "Recovered identity landmark produced implausible XYZ coordinates."
        )

    max_string = as_int(locator.get("max_string", 64))
    name = mem.cstring(
        mem.base + xyz_rva + as_int(locator["string_offset"]),
        max_string,
    ).strip()
    if not _valid_identity_string(name, max_len=max_string - 1):
        raise MemoryRootRecoveryError(
            "Recovered identity landmark did not produce a valid character name."
        )

    return {
        "name": name,
        "xyz_rva": xyz_rva,
        "xyz": tuple(float(value) for value in coords),
    }


def _resolve_server_semantics(
    mem,
    profile: MemoryProfile,
    candidate: _CodeCandidate,
) -> dict[str, Any]:
    from .game_memory import (
        _rip_relative_target_rva,
        _valid_identity_string,
    )

    locator = profile.locator("server")
    if str(locator.get("resolver")) != "rip_relative_cstring_v1":
        raise MemoryRootRecoveryError(
            f"Unsupported server resolver for v5: {locator.get('resolver')!r}"
        )

    target_rva = _rip_relative_target_rva(
        mem,
        candidate.match_rva,
        disp_offset=as_int(locator["disp_offset"]),
        instruction_end=as_int(locator["instruction_end"]),
    )
    if not (0 <= target_rva < int(mem.module_size)):
        raise MemoryRootRecoveryError(
            "Recovered server landmark resolved outside cityofheroes.exe."
        )

    max_string = as_int(locator.get("max_string", 64))
    value = mem.cstring(
        mem.base + target_rva,
        max_string,
    ).strip()
    if not _valid_identity_string(value, max_len=max_string - 1):
        raise MemoryRootRecoveryError(
            "Recovered server landmark did not produce a valid server string."
        )
    return {
        "server": value,
        "target_rva": target_rva,
    }


def _resolve_roster_semantics(
    mem,
    profile: MemoryProfile,
    candidate: _CodeCandidate,
    *,
    identity_name: str,
) -> dict[str, Any]:
    from .game_memory import (
        _character_vitals_plausible,
        _rip_relative_target_rva,
        _valid_identity_string,
    )

    locator = profile.locator("roster")
    if str(locator.get("resolver")) != "roster_builder_v1":
        raise MemoryRootRecoveryError(
            f"Unsupported roster resolver for v5: {locator.get('resolver')!r}"
        )

    roster_base_rva = _rip_relative_target_rva(
        mem,
        candidate.match_rva,
        disp_offset=as_int(locator["base_disp_offset"]),
        instruction_end=as_int(locator["base_instruction_end"]),
    )
    roster_count_rva = _rip_relative_target_rva(
        mem,
        candidate.match_rva,
        disp_offset=as_int(locator["count_disp_offset"]),
        instruction_end=as_int(locator["count_instruction_end"]),
    )
    module_size = int(mem.module_size)
    if not (
        0 <= roster_base_rva < module_size
        and 0 <= roster_count_rva < module_size
    ):
        raise MemoryRootRecoveryError(
            "Recovered roster landmark resolved outside cityofheroes.exe."
        )

    validation = profile.validation()
    raw_count = mem.u32(mem.base + roster_count_rva)
    max_roster = as_int(validation["max_roster_count"])
    if raw_count < 1 or raw_count > max_roster:
        raise MemoryRootRecoveryError(
            f"Recovered roster count {raw_count} is outside 1..{max_roster}."
        )

    roster_cfg = profile.structure("roster")
    entity_cfg = profile.structure("entity")
    record0 = mem.base + roster_base_rva

    copied_name = mem.cstring(
        record0 + as_int(roster_cfg["name_offset"]),
        64,
    ).strip()
    if not _valid_identity_string(copied_name, max_len=63):
        raise MemoryRootRecoveryError(
            "Recovered roster record 0 has an invalid copied character name."
        )

    entity = mem.qword(
        record0 + as_int(roster_cfg["entity_pointer_offset"])
    )
    if not entity:
        raise MemoryRootRecoveryError(
            "Recovered roster record 0 has a null Entity pointer."
        )

    entity_name = mem.cstring(
        entity + as_int(entity_cfg["name_offset"]),
        64,
    ).strip()
    if not _valid_identity_string(entity_name, max_len=63):
        raise MemoryRootRecoveryError(
            "Recovered roster Entity has an invalid character name."
        )

    if not (
        identity_name == copied_name == entity_name
    ):
        raise MemoryRootRecoveryError(
            "Recovered identity, roster record 0, and Entity names do not agree."
        )

    character = mem.qword(
        entity + as_int(entity_cfg["character_pointer_offset"])
    )
    if not character:
        raise MemoryRootRecoveryError(
            "Recovered roster Entity has a null Character pointer."
        )
    if not _character_vitals_plausible(
        mem,
        character,
        profile,
    ):
        raise MemoryRootRecoveryError(
            "Recovered Character pointer failed HP/Endurance plausibility."
        )

    return {
        "raw_roster_count": raw_count,
        "roster_base_rva": roster_base_rva,
        "roster_count_rva": roster_count_rva,
        "copied_name": copied_name,
        "entity_name": entity_name,
        "entity_address": entity,
        "character_address": character,
    }


def _code_evidence(candidate: _CodeCandidate) -> dict[str, Any]:
    return {
        "locator": candidate.locator,
        "source": candidate.source,
        "mismatch_count": candidate.mismatch_count,
        "fixed_byte_count": candidate.fixed_byte_count,
        "match_ratio": round(candidate.match_ratio, 4),
        "anchor_run_length": candidate.anchor_run_length,
    }


def recover_root_context_for_profile(
    mem,
    profile: MemoryProfile,
    *,
    require_server: bool,
) -> MemoryRootRecoveryResult:
    identity_code = _select_code_candidate(
        mem, profile, "identity"
    )
    roster_code = _select_code_candidate(
        mem, profile, "roster"
    )

    server_code: _CodeCandidate | None = None
    try:
        server_code = _select_code_candidate(
            mem, profile, "server"
        )
    except MemoryRootRecoveryError:
        if require_server:
            raise

    recovered_locators = tuple(
        candidate.locator
        for candidate in (
            identity_code,
            server_code,
            roster_code,
        )
        if candidate is not None
        and candidate.source != "exact_signature"
    )
    if not recovered_locators:
        raise MemoryRootRecoveryError(
            "No root locator actually required nearest-landmark recovery."
        )

    samples: list[dict[str, Any]] = []
    for sample_index in range(RECOVERY_SAMPLE_COUNT):
        identity = _resolve_identity_semantics(
            mem, profile, identity_code
        )
        server = {"server": "", "target_rva": None}
        if server_code is not None:
            server = _resolve_server_semantics(
                mem, profile, server_code
            )
        roster = _resolve_roster_semantics(
            mem,
            profile,
            roster_code,
            identity_name=str(identity["name"]),
        )
        samples.append({
            "identity": identity,
            "server": server,
            "roster": roster,
        })
        if sample_index + 1 < RECOVERY_SAMPLE_COUNT:
            time.sleep(RECOVERY_SAMPLE_DELAY_SECONDS)

    first = samples[0]
    stable_fields = {
        "character_name": str(first["identity"]["name"]),
        "server": str(first["server"]["server"]),
        "xyz_rva": int(first["identity"]["xyz_rva"]),
        "server_target_rva": first["server"]["target_rva"],
        "roster_base_rva": int(first["roster"]["roster_base_rva"]),
        "roster_count_rva": int(first["roster"]["roster_count_rva"]),
        "entity_address": int(first["roster"]["entity_address"]),
        "character_address": int(first["roster"]["character_address"]),
    }

    for index, sample in enumerate(samples[1:], start=2):
        current = {
            "character_name": str(sample["identity"]["name"]),
            "server": str(sample["server"]["server"]),
            "xyz_rva": int(sample["identity"]["xyz_rva"]),
            "server_target_rva": sample["server"]["target_rva"],
            "roster_base_rva": int(sample["roster"]["roster_base_rva"]),
            "roster_count_rva": int(sample["roster"]["roster_count_rva"]),
            "entity_address": int(sample["roster"]["entity_address"]),
            "character_address": int(sample["roster"]["character_address"]),
        }
        if current != stable_fields:
            raise MemoryRootRecoveryError(
                f"Recovered root context changed during validation sample "
                f"{index}/{RECOVERY_SAMPLE_COUNT}."
            )

    if require_server and not stable_fields["server"]:
        raise MemoryRootRecoveryError(
            "Server identity is required but root recovery produced none."
        )

    evidence = tuple(
        _code_evidence(candidate)
        for candidate in (
            identity_code,
            server_code,
            roster_code,
        )
        if candidate is not None
    )

    return MemoryRootRecoveryResult(
        context=RootRecoveryContext(
            profile=profile,
            character_name=str(stable_fields["character_name"]),
            server=str(stable_fields["server"]),
            entity_address=int(stable_fields["entity_address"]),
            character_address=int(stable_fields["character_address"]),
        ),
        recovered_locators=recovered_locators,
        sample_count=RECOVERY_SAMPLE_COUNT,
        code_evidence=evidence,
    )


def recover_root_context(
    mem,
    profile_manager: MemoryProfileManager,
    *,
    require_server: bool = False,
) -> MemoryRootRecoveryResult:
    errors: list[str] = []
    candidates = list(profile_manager.candidates())
    if not candidates:
        raise MemoryRootRecoveryError(
            "No valid memory profiles are available for root recovery."
        )

    recovered: list[MemoryRootRecoveryResult] = []
    for profile in candidates:
        try:
            recovered.append(
                recover_root_context_for_profile(
                    mem,
                    profile,
                    require_server=require_server,
                )
            )
        except Exception as exc:
            errors.append(
                f"{profile.profile_id} {profile.profile_version}: {exc}"
            )

    if len(recovered) == 1:
        return recovered[0]
    if len(recovered) > 1:
        # Downloaded/user packs intentionally coexist with bundled fallbacks. If
        # multiple candidates independently recover the exact same live root object
        # graph, preserve MemoryProfileManager precedence and let the normal inventory
        # semantic reader prove the chosen profile's remaining structure. Only
        # genuinely different recovered roots are ambiguous.
        keys = {
            (
                item.context.character_name,
                item.context.server,
                item.context.entity_address,
                item.context.character_address,
            )
            for item in recovered
        }
        if len(keys) == 1:
            return recovered[0]

        labels = ", ".join(
            f"{item.context.profile.profile_id} "
            f"{item.context.profile.profile_version}"
            for item in recovered
        )
        raise MemoryRootRecoveryError(
            "More than one memory profile passed root recovery but they resolved "
            f"different live object graphs ({labels}); refusing an ambiguous "
            "session recovery."
        )

    detail = "; ".join(errors[:4])
    raise MemoryRootRecoveryError(
        "No memory profile passed conservative root recovery."
        + (f" Details: {detail}" if detail else "")
    )
