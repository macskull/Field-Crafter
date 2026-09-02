from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from typing import Any

from .memory_profiles import MemoryProfile, as_int


# FIELD_CRAFTER_MEMORY_SESSION_RECOVERY_V4
# FIELD_CRAFTER_MEMORY_STALE_EMPTY_GUARD_V5_2
#
# Deliberately conservative and ephemeral:
# - root identity/server/roster/Entity/Character resolution is NOT recovered here
# - only Character-local recipe/salvage header offsets can move
# - the search stays within +/-0x200 bytes of the signed profile
# - a candidate must be populated, quantity-valid, namespace-compatible, and strong
# - recovered offsets must repeat across three fresh memory samples
# - nothing is written to the signed/bundled/downloaded profile pack
# - signed candidate-profile validation disables this layer entirely

RECOVERY_SAMPLE_COUNT = 3
RECOVERY_SAMPLE_DELAY_SECONDS = 0.06
MIN_STRONG_SCORE = 15.0
MIN_NAMESPACE_MATCHES = 2
MIN_RUNNER_UP_MARGIN = 3.0


class MemorySessionRecoveryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RecoveredInventoryKind:
    kind: str
    original_collection_offset: int
    recovered_collection_offset: int
    capacity_delta: int
    count_delta: int
    sample_count: int
    minimum_score: float
    minimum_namespace_matches: int


@dataclass(frozen=True, slots=True)
class MemorySessionRecoveryResult:
    profile: MemoryProfile
    recovered: tuple[RecoveredInventoryKind, ...]

    @property
    def applied(self) -> bool:
        return bool(self.recovered)

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(item.kind for item in self.recovered)

    @property
    def sample_count(self) -> int:
        return min((item.sample_count for item in self.recovered), default=0)

    @property
    def summary(self) -> str:
        names = [item.kind for item in self.recovered]
        if not names:
            return ""
        if len(names) == 1:
            return names[0]
        if len(names) == 2:
            return f"{names[0]} and {names[1]}"
        return ", ".join(names[:-1]) + f", and {names[-1]}"


def recovery_policy_summary() -> dict[str, Any]:
    return {
        "sample_count": RECOVERY_SAMPLE_COUNT,
        "sample_delay_seconds": RECOVERY_SAMPLE_DELAY_SECONDS,
        "min_strong_score": MIN_STRONG_SCORE,
        "min_namespace_matches": MIN_NAMESPACE_MATCHES,
        "min_runner_up_margin": MIN_RUNNER_UP_MARGIN,
        "search_window_bytes_each_direction": 0x200,
        "persistent": False,
        "root_locator_recovery": False,
        "signed_candidate_validation_may_use_recovery": False,
        "stale_empty_guard": True,
        "stale_empty_requires_positive_moved_collection_proof": True,
    }


def _winner_is_acceptable(scan: dict[str, Any]) -> tuple[bool, str]:
    winner = scan.get("winner") or {}
    best = winner.get("best")
    if not isinstance(best, dict):
        return False, "no strong candidate"

    if best.get("classification") != "strong":
        return False, f"best candidate is {best.get('classification')!r}, not strong"
    if not best.get("recovery_valid"):
        return False, "best candidate did not pass recovery validation"
    if best.get("type_compatible") is not True:
        return False, "best candidate did not pass recipe/salvage namespace validation"

    try:
        score = float(best.get("score"))
    except (TypeError, ValueError):
        return False, "best candidate has no usable score"
    if score < MIN_STRONG_SCORE:
        return False, f"best score {score:.3f} is below {MIN_STRONG_SCORE:.3f}"

    namespace_matches = int(best.get("namespace_matches") or 0)
    if namespace_matches < MIN_NAMESPACE_MATCHES:
        return False, (
            f"best candidate has only {namespace_matches} typed namespace match(es); "
            f"{MIN_NAMESPACE_MATCHES} are required"
        )

    if not winner.get("clear_winner"):
        return False, "the strong-candidate winner is ambiguous"

    runner = winner.get("runner_up")
    margin = winner.get("score_margin")
    if runner is not None:
        try:
            numeric_margin = float(margin)
        except (TypeError, ValueError):
            return False, "runner-up exists but score margin is unavailable"
        if numeric_margin < MIN_RUNNER_UP_MARGIN:
            return False, (
                f"winner margin {numeric_margin:.3f} is below "
                f"{MIN_RUNNER_UP_MARGIN:.3f}"
            )
    return True, ""


def _scan_one_kind(
    mem,
    character: int,
    profile: MemoryProfile,
    *,
    kind: str,
) -> tuple[int, int, int, float, int]:
    # Import lazily. memory_diagnostics imports game_memory, while game_memory calls
    # this module only after its own module initialization is complete.
    from .memory_diagnostics import (
        _probe_inventory_header,
        _scan_header_candidates,
    )

    character_cfg = profile.structure("character")
    config = character_cfg[kind]
    expected = as_int(config["collection_offset"])
    capacity_delta = as_int(config["capacity_offset"]) - expected
    count_delta = as_int(config["count_offset"]) - expected
    probe_kind = "recipe" if kind == "recipes" else "salvage"

    expected_probe = _probe_inventory_header(
        mem,
        character,
        profile,
        kind=probe_kind,
        collection_offset=expected,
        capacity_delta=capacity_delta,
        count_delta=count_delta,
    )
    if expected_probe.get("recovery_valid"):
        # The signed/current definition is already strongly type-valid. This kind
        # does not need offset recovery, even if the caller's full inventory read
        # failed for some other reason.
        return expected, capacity_delta, count_delta, 0.0, 0

    offsets: list[int] = []
    scores: list[float] = []
    namespace_matches: list[int] = []

    for sample_index in range(RECOVERY_SAMPLE_COUNT):
        scan = _scan_header_candidates(
            mem,
            character,
            profile,
            kind=probe_kind,
            expected_offset=expected,
            capacity_delta=capacity_delta,
            count_delta=count_delta,
        )
        acceptable, reason = _winner_is_acceptable(scan)
        if not acceptable:
            raise MemorySessionRecoveryError(
                f"{kind} recovery sample {sample_index + 1}/{RECOVERY_SAMPLE_COUNT} "
                f"was rejected: {reason}."
            )
        best = (scan.get("winner") or {}).get("best") or {}
        raw_offset = best.get("offset")
        try:
            offset = int(str(raw_offset), 0)
        except Exception as exc:
            raise MemorySessionRecoveryError(
                f"{kind} recovery returned an invalid offset {raw_offset!r}."
            ) from exc

        offsets.append(offset)
        scores.append(float(best["score"]))
        namespace_matches.append(int(best.get("namespace_matches") or 0))

        if sample_index + 1 < RECOVERY_SAMPLE_COUNT:
            time.sleep(RECOVERY_SAMPLE_DELAY_SECONDS)

    if len(set(offsets)) != 1:
        rendered = ", ".join(f"0x{x:X}" for x in offsets)
        raise MemorySessionRecoveryError(
            f"{kind} recovery was unstable across samples ({rendered})."
        )

    recovered = offsets[0]
    if abs(recovered - expected) > 0x200:
        # _scan_header_candidates is already bounded, but retain the invariant here
        # so a future diagnostics refactor cannot silently widen automatic recovery.
        raise MemorySessionRecoveryError(
            f"{kind} candidate exceeded the automatic recovery window."
        )

    return (
        recovered,
        capacity_delta,
        count_delta,
        min(scores),
        min(namespace_matches),
    )


def recover_inventory_profile(
    mem,
    character: int,
    profile: MemoryProfile,
) -> MemorySessionRecoveryResult:
    """Derive an in-memory-only profile for Character-local inventory drift.

    This does not modify the MemoryProfileManager, LocalAppData, bundled data, GitHub
    update state, or any on-disk JSON.
    """
    recovered_items: list[RecoveredInventoryKind] = []
    chosen: dict[str, tuple[int, int, int]] = {}

    for kind in ("recipes", "salvage"):
        cfg = profile.structure("character")[kind]
        original = as_int(cfg["collection_offset"])
        recovered, capacity_delta, count_delta, min_score, min_matches = _scan_one_kind(
            mem,
            character,
            profile,
            kind=kind,
        )
        chosen[kind] = (recovered, capacity_delta, count_delta)
        if recovered != original:
            recovered_items.append(
                RecoveredInventoryKind(
                    kind=kind,
                    original_collection_offset=original,
                    recovered_collection_offset=recovered,
                    capacity_delta=capacity_delta,
                    count_delta=count_delta,
                    sample_count=RECOVERY_SAMPLE_COUNT,
                    minimum_score=min_score,
                    minimum_namespace_matches=min_matches,
                )
            )

    if not recovered_items:
        raise MemorySessionRecoveryError(
            "No Character-local inventory offset change was proven. "
            "The failure is likely outside the bounded recipe/salvage header recovery scope."
        )

    # A recipe and salvage collection must not collapse to the same header.
    if chosen["recipes"][0] == chosen["salvage"][0]:
        raise MemorySessionRecoveryError(
            "Recipe and salvage recovery resolved to the same Character offset."
        )

    data = copy.deepcopy(profile.data)
    structures = data.setdefault("structures", {})
    character_cfg = structures.setdefault("character", {})

    for kind, (collection, capacity_delta, count_delta) in chosen.items():
        kind_cfg = character_cfg.setdefault(kind, {})
        kind_cfg["collection_offset"] = collection
        kind_cfg["capacity_offset"] = collection + capacity_delta
        kind_cfg["count_offset"] = collection + count_delta

    data["_session_recovery"] = {
        "applied": True,
        "persistent": False,
        "recovered_kinds": [item.kind for item in recovered_items],
        "sample_count": RECOVERY_SAMPLE_COUNT,
        "policy": recovery_policy_summary(),
    }

    derived = MemoryProfile(
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        priority=profile.priority,
        source=f"{profile.source}+session-recovery",
        data=data,
    )
    return MemorySessionRecoveryResult(
        profile=derived,
        recovered=tuple(recovered_items),
    )

def recover_stale_empty_inventory_profile(
    mem,
    character: int,
    profile: MemoryProfile,
    *,
    zero_kinds: tuple[str, ...],
) -> MemorySessionRecoveryResult | None:
    """Recover a moved populated collection hidden behind a stale all-zero header.

    A zero total is legitimate by itself. This guard only acts when the bounded,
    type-aware scan positively finds a strong moved collection. If there is no
    strong candidate, None is returned and the original zero inventory remains
    accepted. If strong evidence exists but is ambiguous/unsafe, fail closed.
    """
    from .memory_diagnostics import _scan_header_candidates

    requested = tuple(
        kind for kind in zero_kinds if kind in {"recipes", "salvage"}
    )
    if not requested:
        return None

    proven_kinds: list[str] = []
    for kind in requested:
        cfg = profile.structure("character")[kind]
        expected = as_int(cfg["collection_offset"])
        capacity_delta = as_int(cfg["capacity_offset"]) - expected
        count_delta = as_int(cfg["count_offset"]) - expected
        probe_kind = "recipe" if kind == "recipes" else "salvage"

        scan = _scan_header_candidates(
            mem,
            character,
            profile,
            kind=probe_kind,
            expected_offset=expected,
            capacity_delta=capacity_delta,
            count_delta=count_delta,
        )
        strong = list(scan.get("strong_candidates") or [])
        if not strong:
            # No positive proof of a moved populated collection. Treat the current
            # zero header as a legitimate empty inventory.
            continue

        acceptable, reason = _winner_is_acceptable(scan)
        if not acceptable:
            raise MemorySessionRecoveryError(
                f"{kind} is empty at the signed/current offset, but the bounded "
                f"scan found strong moved-collection evidence that was unsafe to "
                f"adopt: {reason}."
            )
        proven_kinds.append(kind)

    if not proven_kinds:
        return None

    recovered_items: list[RecoveredInventoryKind] = []
    chosen: dict[str, tuple[int, int, int]] = {}

    for kind in ("recipes", "salvage"):
        cfg = profile.structure("character")[kind]
        original = as_int(cfg["collection_offset"])
        capacity_delta = as_int(cfg["capacity_offset"]) - original
        count_delta = as_int(cfg["count_offset"]) - original

        if kind in proven_kinds:
            (
                recovered,
                capacity_delta,
                count_delta,
                min_score,
                min_matches,
            ) = _scan_one_kind(
                mem,
                character,
                profile,
                kind=kind,
            )
        else:
            recovered = original
            min_score = 0.0
            min_matches = 0

        chosen[kind] = (recovered, capacity_delta, count_delta)
        if recovered != original:
            recovered_items.append(
                RecoveredInventoryKind(
                    kind=kind,
                    original_collection_offset=original,
                    recovered_collection_offset=recovered,
                    capacity_delta=capacity_delta,
                    count_delta=count_delta,
                    sample_count=RECOVERY_SAMPLE_COUNT,
                    minimum_score=min_score,
                    minimum_namespace_matches=min_matches,
                )
            )

    if not recovered_items:
        # Defensive: a strong moved candidate was proven above, so reaching this
        # state would mean the multi-sample gate contradicted the first scan.
        raise MemorySessionRecoveryError(
            "A stale-empty moved collection was initially proven, but the "
            "three-sample recovery gate did not confirm an offset change."
        )

    if chosen["recipes"][0] == chosen["salvage"][0]:
        raise MemorySessionRecoveryError(
            "Recipe and salvage stale-empty recovery resolved to the same "
            "Character offset."
        )

    data = copy.deepcopy(profile.data)
    character_cfg = data.setdefault("structures", {}).setdefault(
        "character", {}
    )
    for kind, (collection, capacity_delta, count_delta) in chosen.items():
        kind_cfg = character_cfg.setdefault(kind, {})
        kind_cfg["collection_offset"] = collection
        kind_cfg["capacity_offset"] = collection + capacity_delta
        kind_cfg["count_offset"] = collection + count_delta

    data["_session_recovery"] = {
        "applied": True,
        "persistent": False,
        "trigger": "stale_empty_guard",
        "recovered_kinds": [item.kind for item in recovered_items],
        "sample_count": RECOVERY_SAMPLE_COUNT,
        "policy": recovery_policy_summary(),
    }

    derived = MemoryProfile(
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        priority=profile.priority,
        source=f"{profile.source}+session-recovery",
        data=data,
    )
    return MemorySessionRecoveryResult(
        profile=derived,
        recovered=tuple(recovered_items),
    )

