"""Dependency-neutral, frozen semantics for the K12 qualification census.

This module is intentionally a small semantic boundary.  It does not know how
an authority was created, how a reset was performed, or how a provider was
contacted.  Callers hand it normalized, immutable observations and the module
checks the exact qualification predicate which is currently used by the live
qualification path.

The schedule and predicate are intentionally literal copies of the sealed
qualification contract.  Keeping them here avoids coupling this verifier to
execution provenance, runners, or provider backends.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Iterable

from benchmarks.common.eac.canonical import canonical_argument, canonical_sha256


# The order is the exact order emitted by the sealed fixture contract.
QUALIFICATION_SCHEDULE = (
    "K12Q-S1-T1-N1-A",
    "K12Q-S1-T1-N1-R",
    "K12Q-S1-T1-N1-S",
    "K12Q-S2-T1-N1-R",
    "K12Q-S2-T1-N1-S",
    "K12Q-S2-T1-N1-A",
    "K12Q-S3-T1-N1-S",
    "K12Q-S3-T1-N1-A",
    "K12Q-S3-T1-N1-R",
    "K12Q-S4-T1-N1-A",
    "K12Q-S4-T1-N1-S",
    "K12Q-S4-T1-N1-R",
    "K12Q-S5-T1-N1-R",
    "K12Q-S5-T1-N1-A",
    "K12Q-S5-T1-N1-S",
)
QUALIFICATION_IDS = QUALIFICATION_SCHEDULE
FROZEN_QUALIFICATION_SCHEDULE = QUALIFICATION_SCHEDULE
SCHEDULE = QUALIFICATION_SCHEDULE
CELL_ORDER = QUALIFICATION_SCHEDULE
FROZEN_CELL_ORDER = QUALIFICATION_SCHEDULE

PROBES = ("P1", "P2", "P3", "P4")
QUALIFICATION_PROBES = PROBES
FROZEN_PROBES = PROBES
PROBE_ORDER = PROBES
FROZEN_PROBE_ORDER = PROBES

QUALIFICATION_CELL_COUNT = 15
QUALIFICATION_PROBE_COUNT = 4
QUALIFICATION_SCHEDULE_IDENTITY = "minecraft-k12-live-runtime-qualification-schedule/1"
QUALIFICATION_PROBE_IDENTITY = "minecraft-k12-live-runtime-qualification-probes/1"
SEMANTIC_VERIFIER_IDENTITY = "minecraft-k12-qualification-semantic-verifier/1"

# These are semantic labels, not imports of the corresponding live authority
# implementations.  They are part of the current live predicate's frozen
# vocabulary.
LIVE_QUALIFICATION_PROVENANCE = "live_qualification"
QUALIFICATION_PROBE_PROVENANCE = "qualification_probe"
LIVE_EVIDENCE_ORIGINS = frozenset({"runtime_verified", "injected_fake"})

CELL_SEMANTIC_DOMAIN = "minecraft-k12-qualification-cell/1"
PROBE_SEMANTIC_DOMAIN = "minecraft-k12-qualification-probe/1"
TERMINAL_SEMANTIC_DOMAIN = "minecraft-k12-qualification-terminal/1"
CENSUS_SEMANTIC_DOMAIN = "minecraft-k12-qualification-census/1"
AGGREGATE_SEMANTIC_DOMAIN = "minecraft-k12-qualification-aggregate/1"
PROBE_AGGREGATE_SEMANTIC_DOMAIN = "minecraft-k12-qualification-probe-aggregate/1"
VERDICT_SEMANTIC_DOMAIN = "minecraft-k12-qualification-verdict/1"

QUALIFICATION_SCHEDULE_DIGEST = canonical_sha256(list(QUALIFICATION_SCHEDULE))
PROBE_SCHEDULE_DIGEST = canonical_sha256(list(PROBES))

_CELL_ARMS = {
    cell_id: cell_id.rsplit("-", 1)[1] for cell_id in QUALIFICATION_SCHEDULE
}
_MISSING = object()
_RAW_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CANONICAL_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def qualification_ids() -> tuple[str, ...]:
    """Return the sealed fifteen-cell order without importing live code."""

    return QUALIFICATION_SCHEDULE


def qualification_probes() -> tuple[str, ...]:
    """Return the sealed ordered probe set."""

    return PROBES


def _resolve(value: Any, aliases: dict[str, Any], default: Any, *names: str) -> Any:
    """Resolve a canonical constructor value and its compatibility aliases."""

    found = [(name, aliases.pop(name)) for name in names if name in aliases]
    # ``dataclasses.replace`` supplies every declared field and a caller may
    # use one of the short aliases to replace a field.  Alias values therefore
    # deliberately take precedence over a copied canonical value.
    return found[0][1] if found else (default if value is _MISSING else value)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _raw_digest(value: Any) -> bool:
    return isinstance(value, str) and _RAW_DIGEST.fullmatch(value) is not None


def _canonical_digest(value: Any) -> bool:
    return isinstance(value, str) and _CANONICAL_DIGEST.fullmatch(value) is not None


def _unique(values: Iterable[Any]) -> bool:
    values = tuple(values)
    return all(values[index] != values[prior] for index in range(len(values)) for prior in range(index))


def _identity_of(value: Any) -> str | None:
    identity = getattr(value, "identity", None)
    return identity if isinstance(identity, str) else None


def _component_identity(value: Any) -> str | None:
    """Return a stable component identity even for a malformed record.

    The verifier must be able to return a negative verdict for a typed record
    whose semantic field was altered.  The fallback therefore avoids making
    census construction itself the semantic decision boundary.
    """

    identity = _identity_of(value)
    if identity is not None:
        return identity
    try:
        return canonical_sha256(canonical_argument(value))
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True, init=False)
class NormalizedRejectionBinding:
    """Typed S-arm stale-rejection binding.

    The live oracle's ``RejectionEvidence`` proves these same relationships.
    This record contains only normalized scalar values, so it can be checked
    without importing the live oracle or its authority object.
    """

    arm: str
    cell_id: str
    profile_digest: str
    campaign_id: str
    authority: str
    activation: str
    evidence_origin: str
    reset_token: str
    generation: int
    request_identity: str
    permit_identity: str
    effect_identity: str
    evidence_digest: str
    current_inadmissible: bool
    native_entries: int
    identity: str = field(init=False)

    def __init__(
        self,
        arm: Any = _MISSING,
        cell_id: Any = _MISSING,
        profile_digest: Any = _MISSING,
        campaign_id: Any = _MISSING,
        authority: Any = _MISSING,
        activation: Any = _MISSING,
        evidence_origin: Any = _MISSING,
        reset_token: Any = _MISSING,
        generation: Any = _MISSING,
        request_identity: Any = _MISSING,
        permit_identity: Any = _MISSING,
        effect_identity: Any = _MISSING,
        evidence_digest: Any = _MISSING,
        current_inadmissible: Any = _MISSING,
        native_entries: Any = _MISSING,
        **aliases: Any,
    ) -> None:
        arm = _resolve(arm, aliases, "S", "branch")
        cell_id = _resolve(cell_id, aliases, "", "cell")
        profile_digest = _resolve(profile_digest, aliases, "", "profile")
        campaign_id = _resolve(campaign_id, aliases, "", "campaign")
        authority = _resolve(authority, aliases, "", "authority_id", "authority_digest")
        activation = _resolve(activation, aliases, "", "activation_digest", "activation_id")
        evidence_origin = _resolve(evidence_origin, aliases, "", "origin")
        reset_token = _resolve(reset_token, aliases, "", "reset")
        generation = _resolve(generation, aliases, 0)
        request_identity = _resolve(request_identity, aliases, "", "request")
        permit_identity = _resolve(permit_identity, aliases, "", "permit")
        effect_identity = _resolve(effect_identity, aliases, "", "effect")
        evidence_digest = _resolve(evidence_digest, aliases, "", "evidence")
        current_inadmissible = _resolve(current_inadmissible, aliases, True, "inadmissible")
        native_entries = _resolve(native_entries, aliases, 0, "native_entry_count")
        if aliases:
            raise TypeError(f"unexpected rejection binding fields: {sorted(aliases)}")
        for name, value in (
            ("arm", arm),
            ("cell_id", cell_id),
            ("profile_digest", profile_digest),
            ("campaign_id", campaign_id),
            ("authority", authority),
            ("activation", activation),
            ("evidence_origin", evidence_origin),
            ("reset_token", reset_token),
            ("generation", generation),
            ("request_identity", request_identity),
            ("permit_identity", permit_identity),
            ("effect_identity", effect_identity),
            ("evidence_digest", evidence_digest),
            ("current_inadmissible", current_inadmissible),
            ("native_entries", native_entries),
        ):
            object.__setattr__(self, name, value)
        self.__post_init__()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "identity",
            canonical_sha256(
                {
                    "domain": "minecraft-k12-qualification-rejection-binding/1",
                    "arm": self.arm,
                    "cell_id": self.cell_id,
                    "profile": self.profile_digest,
                    "campaign": self.campaign_id,
                    "authority": self.authority,
                    "activation": self.activation,
                    "origin": self.evidence_origin,
                    "reset_token": self.reset_token,
                    "generation": self.generation,
                    "request": self.request_identity,
                    "permit": self.permit_identity,
                    "effect": self.effect_identity,
                    "evidence": self.evidence_digest,
                    "current_inadmissible": self.current_inadmissible,
                    "native_entries": self.native_entries,
                }
            ),
        )

    @property
    def cell(self) -> str:
        return self.cell_id

    @property
    def profile(self) -> str:
        return self.profile_digest

    @property
    def campaign(self) -> str:
        return self.campaign_id

    @property
    def authority_id(self) -> str:
        return self.authority

    @property
    def activation_digest(self) -> str:
        return self.activation

    @property
    def origin(self) -> str:
        return self.evidence_origin

    @property
    def request(self) -> str:
        return self.request_identity

    @property
    def permit(self) -> str:
        return self.permit_identity

    @property
    def effect(self) -> str:
        return self.effect_identity

    @property
    def native_entry_count(self) -> int:
        return self.native_entries

    @property
    def digest(self) -> str:
        return self.identity

    @property
    def semantic_digest(self) -> str:
        return self.identity


@dataclass(frozen=True, slots=True, init=False)
class NormalizedCell:
    """One immutable normalized live qualification cell observation."""

    cell_id: str
    authority: str
    activation: str
    profile_digest: str
    evidence_origin: str
    campaign_id: str
    reset_identity: str
    evidence_digest: str
    fresh_root: bool
    reset_passed: bool
    capability_state: str
    provider_terminal: str
    oracle_value: str
    containment_clean: bool
    evidence_valid: bool
    terminal_verified: bool
    rejection_verified: bool
    execution_provenance: str
    retry: bool
    resumed: bool
    replacement: bool
    rejection_binding: NormalizedRejectionBinding | None
    reset_token: str | None
    generation: int | None
    request_identity: str | None
    permit_identity: str | None
    effect_identity: str | None
    identity: str = field(init=False)

    def __init__(
        self,
        cell_id: Any,
        authority: Any = _MISSING,
        activation: Any = _MISSING,
        profile_digest: Any = _MISSING,
        evidence_origin: Any = _MISSING,
        campaign_id: Any = _MISSING,
        reset_identity: Any = _MISSING,
        evidence_digest: Any = _MISSING,
        fresh_root: Any = _MISSING,
        reset_passed: Any = _MISSING,
        capability_state: Any = _MISSING,
        provider_terminal: Any = _MISSING,
        oracle_value: Any = _MISSING,
        containment_clean: Any = _MISSING,
        evidence_valid: Any = _MISSING,
        terminal_verified: Any = _MISSING,
        rejection_verified: Any = _MISSING,
        execution_provenance: Any = _MISSING,
        retry: Any = _MISSING,
        resumed: Any = _MISSING,
        replacement: Any = _MISSING,
        rejection_binding: Any = _MISSING,
        reset_token: Any = _MISSING,
        generation: Any = _MISSING,
        request_identity: Any = _MISSING,
        permit_identity: Any = _MISSING,
        effect_identity: Any = _MISSING,
        **aliases: Any,
    ) -> None:
        authority = _resolve(
            authority, aliases, "authority", "authority_id", "authority_digest", "authority_binding"
        )
        activation = _resolve(activation, aliases, "activation", "activation_digest", "activation_id")
        profile_digest = _resolve(profile_digest, aliases, "profile", "profile_id")
        evidence_origin = _resolve(evidence_origin, aliases, "runtime_verified", "origin")
        campaign_id = _resolve(campaign_id, aliases, "cells", "campaign")
        reset_identity = _resolve(reset_identity, aliases, "", "reset", "reset_id")
        evidence_digest = _resolve(evidence_digest, aliases, "", "evidence", "evidence_id")
        fresh_root = _resolve(fresh_root, aliases, False, "fresh")
        reset_passed = _resolve(reset_passed, aliases, False, "reset_valid")
        capability_state = _resolve(capability_state, aliases, "", "capability")
        provider_terminal = _resolve(provider_terminal, aliases, "", "provider")
        oracle_value = _resolve(oracle_value, aliases, "", "oracle")
        containment_clean = _resolve(containment_clean, aliases, False, "clean")
        evidence_valid = _resolve(evidence_valid, aliases, False, "evidence_ok")
        terminal_verified = _resolve(
            terminal_verified, aliases, True, "terminal", "terminal_valid"
        )
        rejection_verified = _resolve(rejection_verified, aliases, True, "rejection_valid")
        execution_provenance = _resolve(
            execution_provenance, aliases, LIVE_QUALIFICATION_PROVENANCE, "provenance"
        )
        retry = _resolve(retry, aliases, False)
        resumed = _resolve(resumed, aliases, False)
        replacement = _resolve(replacement, aliases, False)
        rejection_binding = _resolve(
            rejection_binding, aliases, None, "rejection", "rejection_evidence"
        )
        reset_token = _resolve(reset_token, aliases, None)
        generation = _resolve(generation, aliases, None)
        request_identity = _resolve(request_identity, aliases, None, "request")
        permit_identity = _resolve(permit_identity, aliases, None, "permit")
        effect_identity = _resolve(effect_identity, aliases, None, "effect")
        if aliases:
            raise TypeError(f"unexpected cell fields: {sorted(aliases)}")
        for name, value in (
            ("cell_id", cell_id),
            ("authority", authority),
            ("activation", activation),
            ("profile_digest", profile_digest),
            ("evidence_origin", evidence_origin),
            ("campaign_id", campaign_id),
            ("reset_identity", reset_identity),
            ("evidence_digest", evidence_digest),
            ("fresh_root", fresh_root),
            ("reset_passed", reset_passed),
            ("capability_state", capability_state),
            ("provider_terminal", provider_terminal),
            ("oracle_value", oracle_value),
            ("containment_clean", containment_clean),
            ("evidence_valid", evidence_valid),
            ("terminal_verified", terminal_verified),
            ("rejection_verified", rejection_verified),
            ("execution_provenance", execution_provenance),
            ("retry", retry),
            ("resumed", resumed),
            ("replacement", replacement),
            ("rejection_binding", rejection_binding),
            ("reset_token", reset_token),
            ("generation", generation),
            ("request_identity", request_identity),
            ("permit_identity", permit_identity),
            ("effect_identity", effect_identity),
        ):
            object.__setattr__(self, name, value)
        self.__post_init__()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "identity",
            canonical_sha256(
                {
                    "domain": CELL_SEMANTIC_DOMAIN,
                    "cell_id": self.cell_id,
                    "authority": self.authority,
                    "activation": self.activation,
                    "profile": self.profile_digest,
                    "origin": self.evidence_origin,
                    "campaign": self.campaign_id,
                    "reset": self.reset_identity,
                    "evidence": self.evidence_digest,
                    "fresh_root": self.fresh_root,
                    "reset_passed": self.reset_passed,
                    "capability_state": self.capability_state,
                    "provider_terminal": self.provider_terminal,
                    "oracle_value": self.oracle_value,
                    "containment_clean": self.containment_clean,
                    "evidence_valid": self.evidence_valid,
                    "terminal_verified": self.terminal_verified,
                    "rejection_verified": self.rejection_verified,
                    "execution_provenance": self.execution_provenance,
                    "retry": self.retry,
                    "resumed": self.resumed,
                    "replacement": self.replacement,
                    "rejection_binding": _component_identity(self.rejection_binding),
                    "reset_token": self.reset_token,
                    "generation": self.generation,
                    "request": self.request_identity,
                    "permit": self.permit_identity,
                    "effect": self.effect_identity,
                }
            ),
        )

    @property
    def arm(self) -> str | None:
        return _CELL_ARMS.get(self.cell_id)

    @property
    def authority_id(self) -> str:
        return self.authority

    @property
    def activation_digest(self) -> str:
        return self.activation

    @property
    def profile(self) -> str:
        return self.profile_digest

    @property
    def origin(self) -> str:
        return self.evidence_origin

    @property
    def campaign(self) -> str:
        return self.campaign_id

    @property
    def fresh(self) -> bool:
        return self.fresh_root

    @property
    def reset_valid(self) -> bool:
        return self.reset_passed

    @property
    def terminal(self) -> bool:
        return self.terminal_verified

    @property
    def clean(self) -> bool:
        return self.containment_clean

    @property
    def evidence_ok(self) -> bool:
        return self.evidence_valid

    @property
    def oracle(self) -> str:
        return self.oracle_value

    @property
    def rejection(self) -> NormalizedRejectionBinding | None:
        return self.rejection_binding

    @property
    def digest(self) -> str:
        return self.identity

    @property
    def semantic_digest(self) -> str:
        return self.identity


@dataclass(frozen=True, slots=True, init=False)
class NormalizedProbe:
    """One immutable normalized P1--P4 probe observation."""

    probe: str
    authority: str
    activation: str
    profile_digest: str
    evidence_origin: str
    campaign_id: str
    passed: bool
    terminal_verified: bool
    evidence_digest: str
    execution_provenance: str
    identity: str = field(init=False)

    def __init__(
        self,
        probe: Any,
        authority: Any = _MISSING,
        activation: Any = _MISSING,
        profile_digest: Any = _MISSING,
        evidence_origin: Any = _MISSING,
        campaign_id: Any = _MISSING,
        passed: Any = _MISSING,
        terminal_verified: Any = _MISSING,
        evidence_digest: Any = _MISSING,
        execution_provenance: Any = _MISSING,
        **aliases: Any,
    ) -> None:
        authority = _resolve(
            authority, aliases, "authority", "authority_id", "authority_digest", "authority_binding"
        )
        activation = _resolve(activation, aliases, "activation", "activation_digest", "activation_id")
        profile_digest = _resolve(profile_digest, aliases, "profile", "profile_id")
        evidence_origin = _resolve(evidence_origin, aliases, "runtime_verified", "origin")
        campaign_id = _resolve(campaign_id, aliases, "probes", "campaign")
        passed = _resolve(passed, aliases, False)
        terminal_verified = _resolve(terminal_verified, aliases, True, "terminal", "terminal_valid")
        evidence_digest = _resolve(evidence_digest, aliases, "", "evidence")
        execution_provenance = _resolve(
            execution_provenance, aliases, QUALIFICATION_PROBE_PROVENANCE, "provenance"
        )
        if aliases:
            raise TypeError(f"unexpected probe fields: {sorted(aliases)}")
        for name, value in (
            ("probe", probe),
            ("authority", authority),
            ("activation", activation),
            ("profile_digest", profile_digest),
            ("evidence_origin", evidence_origin),
            ("campaign_id", campaign_id),
            ("passed", passed),
            ("terminal_verified", terminal_verified),
            ("evidence_digest", evidence_digest),
            ("execution_provenance", execution_provenance),
        ):
            object.__setattr__(self, name, value)
        self.__post_init__()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "identity",
            canonical_sha256(
                {
                    "domain": PROBE_SEMANTIC_DOMAIN,
                    "probe": self.probe,
                    "authority": self.authority,
                    "activation": self.activation,
                    "profile": self.profile_digest,
                    "origin": self.evidence_origin,
                    "campaign": self.campaign_id,
                    "passed": self.passed,
                    "terminal_verified": self.terminal_verified,
                    "evidence": self.evidence_digest,
                    "execution_provenance": self.execution_provenance,
                }
            ),
        )

    @property
    def authority_id(self) -> str:
        return self.authority

    @property
    def activation_digest(self) -> str:
        return self.activation

    @property
    def profile(self) -> str:
        return self.profile_digest

    @property
    def origin(self) -> str:
        return self.evidence_origin

    @property
    def campaign(self) -> str:
        return self.campaign_id

    @property
    def terminal(self) -> bool:
        return self.terminal_verified

    @property
    def digest(self) -> str:
        return self.identity

    @property
    def semantic_digest(self) -> str:
        return self.identity


@dataclass(frozen=True, slots=True, init=False)
class NormalizedTerminal:
    """The immutable passed terminal ledger observation."""

    authority: str
    activation: str
    profile_digest: str
    evidence_origin: str
    ledger_digest: str
    aggregate_digest: str
    probe_digest: str
    result: str
    state: str
    verified: bool
    identity: str = field(init=False)

    def __init__(
        self,
        authority: Any = _MISSING,
        activation: Any = _MISSING,
        profile_digest: Any = _MISSING,
        evidence_origin: Any = _MISSING,
        ledger_digest: Any = _MISSING,
        aggregate_digest: Any = _MISSING,
        probe_digest: Any = _MISSING,
        result: Any = _MISSING,
        state: Any = _MISSING,
        verified: Any = _MISSING,
        **aliases: Any,
    ) -> None:
        authority = _resolve(
            authority, aliases, "authority", "authority_id", "authority_digest", "authority_binding"
        )
        activation = _resolve(activation, aliases, "activation", "activation_digest", "activation_id")
        profile_digest = _resolve(profile_digest, aliases, "profile", "profile_id")
        evidence_origin = _resolve(evidence_origin, aliases, "runtime_verified", "origin")
        ledger_digest = _resolve(ledger_digest, aliases, "", "ledger")
        aggregate_digest = _resolve(aggregate_digest, aliases, "", "aggregate")
        probe_digest = _resolve(probe_digest, aliases, "", "probes")
        result = _resolve(result, aliases, "passed")
        state = _resolve(state, aliases, "terminal")
        verified = _resolve(verified, aliases, True, "terminal_verified")
        if aliases:
            raise TypeError(f"unexpected terminal fields: {sorted(aliases)}")
        for name, value in (
            ("authority", authority),
            ("activation", activation),
            ("profile_digest", profile_digest),
            ("evidence_origin", evidence_origin),
            ("ledger_digest", ledger_digest),
            ("aggregate_digest", aggregate_digest),
            ("probe_digest", probe_digest),
            ("result", result),
            ("state", state),
            ("verified", verified),
        ):
            object.__setattr__(self, name, value)
        self.__post_init__()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "identity",
            canonical_sha256(
                {
                    "domain": TERMINAL_SEMANTIC_DOMAIN,
                    "authority": self.authority,
                    "activation": self.activation,
                    "profile": self.profile_digest,
                    "origin": self.evidence_origin,
                    "ledger": self.ledger_digest,
                    "aggregate": self.aggregate_digest,
                    "probe": self.probe_digest,
                    "result": self.result,
                    "state": self.state,
                    "verified": self.verified,
                }
            ),
        )

    @property
    def authority_id(self) -> str:
        return self.authority

    @property
    def activation_digest(self) -> str:
        return self.activation

    @property
    def profile(self) -> str:
        return self.profile_digest

    @property
    def origin(self) -> str:
        return self.evidence_origin

    @property
    def terminal_verified(self) -> bool:
        return self.verified

    @property
    def passed(self) -> bool:
        return self.result == "passed" and self.state == "terminal" and self.verified is True

    @property
    def digest(self) -> str:
        return self.identity

    @property
    def semantic_digest(self) -> str:
        return self.identity


@dataclass(frozen=True, slots=True, init=False)
class QualificationCensus:
    """Frozen cells, probes, and terminal record with canonical digests."""

    cells: tuple[NormalizedCell, ...]
    probes: tuple[NormalizedProbe, ...]
    terminal: NormalizedTerminal | None
    cell_digest: str = field(init=False)
    probe_digest: str = field(init=False)
    aggregate_digest: str = field(init=False)
    identity: str = field(init=False)

    def __init__(
        self,
        cells: Iterable[NormalizedCell],
        probes: Iterable[NormalizedProbe],
        terminal: NormalizedTerminal | None = None,
        **aliases: Any,
    ) -> None:
        terminal = _resolve(terminal, aliases, None, "terminal_evidence", "terminal_record")
        if aliases:
            raise TypeError(f"unexpected census fields: {sorted(aliases)}")
        object.__setattr__(self, "cells", tuple(cells))
        object.__setattr__(self, "probes", tuple(probes))
        object.__setattr__(self, "terminal", terminal)
        self.__post_init__()

    def __post_init__(self) -> None:
        cell_identities = tuple(_component_identity(value) for value in self.cells)
        probe_identities = tuple(_component_identity(value) for value in self.probes)
        object.__setattr__(
            self,
            "cell_digest",
            canonical_sha256(
                {
                    "domain": "minecraft-k12-qualification-cell-census/1",
                    "cells": list(cell_identities),
                }
            ),
        )
        first_cell = self.cells[0] if self.cells and isinstance(self.cells[0], NormalizedCell) else None
        first_probe = self.probes[0] if self.probes and isinstance(self.probes[0], NormalizedProbe) else None
        common = {
            "authority": getattr(first_cell, "authority", None),
            "activation": getattr(first_cell, "activation", None),
            "profile": getattr(first_cell, "profile_digest", None),
            "origin": getattr(first_cell, "evidence_origin", None),
            "cell_campaign": getattr(first_cell, "campaign_id", None),
            "probe_campaign": getattr(first_probe, "campaign_id", None),
        }
        object.__setattr__(
            self,
            "probe_digest",
            canonical_sha256(
                {
                    "domain": PROBE_AGGREGATE_SEMANTIC_DOMAIN,
                    "probes": list(probe_identities),
                    **common,
                }
            ),
        )
        object.__setattr__(
            self,
            "aggregate_digest",
            canonical_sha256(
                {
                    "domain": AGGREGATE_SEMANTIC_DOMAIN,
                    "cells": list(cell_identities),
                    "probes": self.probe_digest,
                    **common,
                    "cell_provenance": LIVE_QUALIFICATION_PROVENANCE,
                }
            ),
        )
        object.__setattr__(
            self,
            "identity",
            canonical_sha256(
                {
                    "domain": CENSUS_SEMANTIC_DOMAIN,
                    "aggregate": self.aggregate_digest,
                    "terminal": _component_identity(self.terminal),
                }
            ),
        )

    @property
    def digest(self) -> str:
        return self.identity

    @property
    def census_digest(self) -> str:
        return self.identity

    @property
    def semantic_digest(self) -> str:
        return self.identity

    @property
    def schedule_identity(self) -> str:
        return QUALIFICATION_SCHEDULE_IDENTITY

    @property
    def cell_count(self) -> int:
        return len(self.cells)

    @property
    def probe_count(self) -> int:
        return len(self.probes)

    @property
    def qualification_digest(self) -> str:
        return self.aggregate_digest

    @property
    def aggregate_identity(self) -> str:
        return self.aggregate_digest

    @property
    def probe_aggregate_digest(self) -> str:
        return self.probe_digest

    def verify(self) -> "QualificationVerdict":
        return verify_qualification(self)


@dataclass(frozen=True, slots=True, init=False)
class QualificationVerdict:
    """Pure verifier output; it contains no authority or runtime capability."""

    passed: bool
    reasons: tuple[str, ...]
    census_identity: str
    aggregate_digest: str
    probe_digest: str
    identity: str = field(init=False)

    def __init__(
        self,
        passed: bool,
        reasons: Iterable[str] = (),
        census_identity: str = "",
        aggregate_digest: str = "",
        probe_digest: str = "",
    ) -> None:
        object.__setattr__(self, "passed", passed)
        object.__setattr__(self, "reasons", tuple(reasons))
        object.__setattr__(self, "census_identity", census_identity)
        object.__setattr__(self, "aggregate_digest", aggregate_digest)
        object.__setattr__(self, "probe_digest", probe_digest)
        self.__post_init__()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "identity",
            canonical_sha256(
                {
                    "domain": VERDICT_SEMANTIC_DOMAIN,
                    "passed": self.passed,
                    "reasons": list(self.reasons),
                    "census": self.census_identity,
                    "aggregate": self.aggregate_digest,
                    "probe": self.probe_digest,
                }
            ),
        )

    def __bool__(self) -> bool:
        return self.passed is True

    @property
    def qualifies(self) -> bool:
        return self.passed is True

    @property
    def accepted(self) -> bool:
        return self.passed is True

    @property
    def valid(self) -> bool:
        return self.passed is True

    @property
    def ok(self) -> bool:
        return self.passed is True

    @property
    def is_valid(self) -> bool:
        return self.passed is True

    @property
    def digest(self) -> str:
        return self.identity

    @property
    def semantic_digest(self) -> str:
        return self.identity


def _rejection_matches(cell: NormalizedCell) -> bool:
    rejection = cell.rejection_binding
    if not isinstance(rejection, NormalizedRejectionBinding):
        return False
    if (
        rejection.arm != "S"
        or rejection.cell_id != cell.cell_id
        or rejection.profile_digest != cell.profile_digest
        or rejection.campaign_id != cell.campaign_id
        or rejection.authority != cell.authority
        or rejection.activation != cell.activation
        or rejection.evidence_origin != cell.evidence_origin
        or type(cell.generation) is not int
        or cell.generation <= 0
        or not _text(rejection.reset_token)
        or type(rejection.generation) is not int
        or rejection.generation <= 0
        or not _text(rejection.request_identity)
        or not _text(rejection.permit_identity)
        or not _text(rejection.effect_identity)
        or rejection.reset_token != cell.reset_identity
        or rejection.generation != cell.generation
        or rejection.request_identity != cell.request_identity
        or rejection.permit_identity != cell.permit_identity
        or rejection.effect_identity != cell.effect_identity
        or rejection.evidence_digest != cell.evidence_digest
        or not _raw_digest(rejection.evidence_digest)
        or rejection.current_inadmissible is not True
        or type(rejection.native_entries) is not int
        or rejection.native_entries != 0
    ):
        return False
    # These optional mirrors correspond to the direct operation fields on the
    # live observation.  If supplied, the typed binding must repeat them.
    return all(
        mirror is None or mirror == expected
        for mirror, expected in (
            (cell.reset_token, rejection.reset_token),
            (cell.generation, rejection.generation),
            (cell.request_identity, rejection.request_identity),
            (cell.permit_identity, rejection.permit_identity),
            (cell.effect_identity, rejection.effect_identity),
        )
    )


def qualification_cell_clauses(
    *, cell_id: str, fresh_root: bool, reset_passed: bool,
    capability_state: str, provider_terminal: str, oracle_value: str,
    containment_clean: bool, evidence_valid: bool, terminal_verified: bool,
    rejection_verified: bool, retry: bool, resumed: bool, replacement: bool,
    rejection_matches: bool,
) -> bool:
    """Evaluate the single frozen per-cell A/R/S boolean clause set."""

    return not qualification_cell_clause_failures(
        cell_id=cell_id, fresh_root=fresh_root, reset_passed=reset_passed,
        capability_state=capability_state, provider_terminal=provider_terminal,
        oracle_value=oracle_value, containment_clean=containment_clean,
        evidence_valid=evidence_valid, terminal_verified=terminal_verified,
        rejection_verified=rejection_verified, retry=retry, resumed=resumed,
        replacement=replacement, rejection_matches=rejection_matches,
    )


def qualification_cell_clause_failures(
    *, cell_id: str, fresh_root: bool, reset_passed: bool,
    capability_state: str, provider_terminal: str, oracle_value: str,
    containment_clean: bool, evidence_valid: bool, terminal_verified: bool,
    rejection_verified: bool, retry: bool, resumed: bool, replacement: bool,
    rejection_matches: bool,
) -> tuple[str, ...]:
    """Return stable reasons from the single frozen per-cell clause set."""

    arm = _CELL_ARMS.get(cell_id)
    if arm is None:
        return ("cell_domain",)
    expected_oracle = "not_applicable" if arm == "S" else "true"
    clauses = (
        (fresh_root is True, "cell_not_fresh"),
        (reset_passed is True, "cell_reset"),
        (capability_state == "REVOKED", "cell_capability"),
        (provider_terminal == "success", "cell_provider_terminal"),
        (oracle_value == expected_oracle, f"oracle_{arm}"),
        (containment_clean is True, "cell_containment"),
        (evidence_valid is True, "cell_evidence"),
        (terminal_verified is True, "cell_terminal"),
        (rejection_verified is True, "cell_rejection"),
        (retry is False, "cell_retry"),
        (resumed is False, "cell_resume"),
        (replacement is False, "cell_replacement"),
        (arm != "S" or rejection_matches, "rejection_binding"),
    )
    return tuple(reason for passed, reason in clauses if not passed)


def normalized_cell_passes(cell: NormalizedCell) -> bool:
    """Return the frozen predicate plus normalized live-domain bindings."""

    if not isinstance(cell, NormalizedCell):
        return False
    return (
        all(_text(value) for value in (
            cell.authority,
            cell.activation,
            cell.profile_digest,
            cell.evidence_origin,
            cell.campaign_id,
            cell.reset_identity,
            cell.evidence_digest,
        ))
        and cell.evidence_origin in LIVE_EVIDENCE_ORIGINS
        and cell.execution_provenance == LIVE_QUALIFICATION_PROVENANCE
        and qualification_cell_clauses(
            cell_id=cell.cell_id,
            fresh_root=cell.fresh_root,
            reset_passed=cell.reset_passed,
            capability_state=cell.capability_state,
            provider_terminal=cell.provider_terminal,
            oracle_value=cell.oracle_value,
            containment_clean=cell.containment_clean,
            evidence_valid=cell.evidence_valid,
            terminal_verified=cell.terminal_verified,
            rejection_verified=cell.rejection_verified,
            retry=cell.retry,
            resumed=cell.resumed,
            replacement=cell.replacement,
            rejection_matches=_rejection_matches(cell),
        )
    )


def _add(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _verdict(census: QualificationCensus | None, reasons: Iterable[str]) -> QualificationVerdict:
    reasons = tuple(reasons)
    if census is None:
        return QualificationVerdict(False, reasons)
    return QualificationVerdict(
        not reasons,
        reasons,
        census.identity,
        census.aggregate_digest,
        census.probe_digest,
    )


def _verify_qualification(
    census: QualificationCensus, *, require_terminal: bool
) -> QualificationVerdict:
    """Verify one frozen census using only normalized semantic values."""

    if not isinstance(census, QualificationCensus):
        return _verdict(None, ("untyped_census",))

    reasons: list[str] = []
    cells = census.cells
    probes = census.probes
    terminal = census.terminal

    if len(cells) != QUALIFICATION_CELL_COUNT:
        _add(reasons, "cell_count")
    if len(probes) != QUALIFICATION_PROBE_COUNT:
        _add(reasons, "probe_count")

    typed_cells = all(isinstance(cell, NormalizedCell) for cell in cells)
    typed_probes = all(isinstance(probe, NormalizedProbe) for probe in probes)
    typed_terminal = isinstance(terminal, NormalizedTerminal)
    if not typed_cells:
        _add(reasons, "cell_type")
    if not typed_probes:
        _add(reasons, "probe_type")
    if require_terminal and not typed_terminal:
        _add(reasons, "terminal_type")

    valid_cells = tuple(cell for cell in cells if isinstance(cell, NormalizedCell))
    valid_probes = tuple(probe for probe in probes if isinstance(probe, NormalizedProbe))

    cell_ids = tuple(cell.cell_id for cell in valid_cells)
    if cell_ids != QUALIFICATION_SCHEDULE:
        _add(reasons, "cell_order")
    if not _unique(cell_ids):
        _add(reasons, "cell_duplicate")
    if not _unique(cell.identity for cell in valid_cells):
        _add(reasons, "cell_identity_duplicate")
    if not _unique(cell.evidence_digest for cell in valid_cells):
        _add(reasons, "cell_evidence_duplicate")
    if not _unique(cell.reset_identity for cell in valid_cells):
        _add(reasons, "cell_reset_duplicate")

    for cell in valid_cells:
        arm = _CELL_ARMS.get(cell.cell_id) if isinstance(cell.cell_id, str) else None
        if arm is None:
            _add(reasons, "cell_domain")
            continue
        if not all(
            _text(value)
            for value in (
                cell.authority,
                cell.activation,
                cell.profile_digest,
                cell.evidence_origin,
                cell.campaign_id,
                cell.reset_identity,
                cell.evidence_digest,
            )
        ):
            _add(reasons, "cell_binding")
        if cell.evidence_origin not in LIVE_EVIDENCE_ORIGINS:
            _add(reasons, "cell_origin")
        if cell.execution_provenance != LIVE_QUALIFICATION_PROVENANCE:
            _add(reasons, "cell_provenance")
        for reason in qualification_cell_clause_failures(
            cell_id=cell.cell_id,
            fresh_root=cell.fresh_root,
            reset_passed=cell.reset_passed,
            capability_state=cell.capability_state,
            provider_terminal=cell.provider_terminal,
            oracle_value=cell.oracle_value,
            containment_clean=cell.containment_clean,
            evidence_valid=cell.evidence_valid,
            terminal_verified=cell.terminal_verified,
            rejection_verified=cell.rejection_verified,
            retry=cell.retry,
            resumed=cell.resumed,
            replacement=cell.replacement,
            rejection_matches=_rejection_matches(cell),
        ):
            _add(reasons, reason)
        if not normalized_cell_passes(cell):
            _add(reasons, "cell_predicate")

    probe_ids = tuple(probe.probe for probe in valid_probes)
    if probe_ids != PROBES:
        _add(reasons, "probe_order")
    if not _unique(probe_ids):
        _add(reasons, "probe_duplicate")
    if not _unique(probe.identity for probe in valid_probes):
        _add(reasons, "probe_identity_duplicate")
    for probe in valid_probes:
        if probe.probe not in PROBES:
            _add(reasons, "probe_domain")
        if not all(
            _text(value)
            for value in (
                probe.authority,
                probe.activation,
                probe.profile_digest,
                probe.evidence_origin,
                probe.campaign_id,
                probe.evidence_digest,
            )
        ):
            _add(reasons, "probe_binding")
        if probe.evidence_origin not in LIVE_EVIDENCE_ORIGINS:
            _add(reasons, "probe_origin")
        if probe.execution_provenance != QUALIFICATION_PROBE_PROVENANCE:
            _add(reasons, "probe_provenance")
        if probe.passed is not True:
            _add(reasons, "probe_failed")
        if probe.terminal_verified is not True:
            _add(reasons, "probe_terminal")

    if valid_cells and valid_probes:
        cell_campaign_values = tuple(cell.campaign_id for cell in valid_cells)
        probe_campaign_values = tuple(probe.campaign_id for probe in valid_probes)
        cell_campaigns = set(cell_campaign_values) if all(_text(value) for value in cell_campaign_values) else set()
        probe_campaigns = set(probe_campaign_values) if all(_text(value) for value in probe_campaign_values) else set()
        if not cell_campaigns or len(cell_campaigns) != 1:
            _add(reasons, "cell_campaign_binding")
        if not probe_campaigns or len(probe_campaigns) != 1:
            _add(reasons, "probe_campaign_binding")
        if len(cell_campaigns) == 1 and len(probe_campaigns) == 1:
            if next(iter(cell_campaigns)) == next(iter(probe_campaigns)):
                _add(reasons, "campaign_not_distinct")

    # All live records repeat the same semantic authority closure.  This is
    # intentionally stricter than merely comparing a profile or a campaign.
    common_records: tuple[Any, ...] = (*valid_cells, *valid_probes)
    if typed_terminal:
        common_records += (terminal,)
    for attribute, reason in (
        ("authority", "common_authority"),
        ("activation", "common_activation"),
        ("profile_digest", "common_profile"),
        ("evidence_origin", "common_origin"),
    ):
        values = tuple(getattr(record, attribute) for record in common_records)
        if not values or not _text(values[0]) or any(value != values[0] for value in values[1:]):
            _add(reasons, reason)

    if typed_terminal:
        if not all(
            _text(value)
            for value in (
                terminal.authority,
                terminal.activation,
                terminal.profile_digest,
                terminal.evidence_origin,
                terminal.ledger_digest,
                terminal.aggregate_digest,
                terminal.probe_digest,
            )
        ):
            _add(reasons, "terminal_binding")
        if terminal.evidence_origin not in LIVE_EVIDENCE_ORIGINS:
            _add(reasons, "terminal_origin")
        if not _canonical_digest(terminal.ledger_digest):
            _add(reasons, "terminal_ledger")
        if terminal.verified is not True:
            _add(reasons, "terminal_unverified")
        if terminal.state != "terminal":
            _add(reasons, "terminal_state")
        if terminal.result != "passed":
            _add(reasons, "terminal_result")
        if terminal.aggregate_digest != census.aggregate_digest:
            _add(reasons, "terminal_aggregate")
        if terminal.probe_digest != census.probe_digest:
            _add(reasons, "terminal_probe")

    return _verdict(census, reasons)


def verify_qualification(census: QualificationCensus) -> QualificationVerdict:
    """Verify a complete frozen census, including its durable terminal."""

    return _verify_qualification(census, require_terminal=True)


def verify_qualification_projection(census: QualificationCensus) -> QualificationVerdict:
    """Verify the parent terminal projection before appending its ledger event."""

    return _verify_qualification(census, require_terminal=False)


def verify_qualification_semantics(census: QualificationCensus) -> QualificationVerdict:
    """Descriptive alias for :func:`verify_qualification`."""

    return verify_qualification(census)


verify_census = verify_qualification
verify_frozen_qualification = verify_qualification
verify_qualification_census = verify_qualification
verify = verify_qualification


def qualifies(census: QualificationCensus) -> bool:
    """Return only the boolean result for callers that do not need reasons."""

    return verify_qualification(census).passed is True


def make_census(
    cells: Iterable[NormalizedCell],
    probes: Iterable[NormalizedProbe],
    terminal: NormalizedTerminal | None = None,
) -> QualificationCensus:
    """Freeze normalized records into a census without performing verification."""

    return QualificationCensus(cells, probes, terminal)


build_census = make_census
FrozenCell = NormalizedCell
FrozenProbe = NormalizedProbe
FrozenTerminal = NormalizedTerminal
FrozenQualificationCell = NormalizedCell
FrozenQualificationProbe = NormalizedProbe
FrozenQualificationTerminal = NormalizedTerminal
CellRecord = NormalizedCell
ProbeRecord = NormalizedProbe
TerminalRecord = NormalizedTerminal
RejectionBinding = NormalizedRejectionBinding
NormalizedRejection = NormalizedRejectionBinding
FrozenRejectionBinding = NormalizedRejectionBinding
FrozenQualificationCensus = QualificationCensus
QualificationSemanticVerdict = QualificationVerdict
CellQualificationRecord = NormalizedCell
ProbeQualificationRecord = NormalizedProbe
TerminalQualificationRecord = NormalizedTerminal
CellAttestation = NormalizedCell
ProbeAttestation = NormalizedProbe
TerminalAttestation = NormalizedTerminal


__all__ = [
    "QUALIFICATION_SCHEDULE",
    "QUALIFICATION_IDS",
    "FROZEN_QUALIFICATION_SCHEDULE",
    "SCHEDULE",
    "CELL_ORDER",
    "FROZEN_CELL_ORDER",
    "PROBES",
    "QUALIFICATION_PROBES",
    "FROZEN_PROBES",
    "PROBE_ORDER",
    "FROZEN_PROBE_ORDER",
    "QUALIFICATION_CELL_COUNT",
    "QUALIFICATION_PROBE_COUNT",
    "QUALIFICATION_SCHEDULE_IDENTITY",
    "QUALIFICATION_PROBE_IDENTITY",
    "SEMANTIC_VERIFIER_IDENTITY",
    "LIVE_QUALIFICATION_PROVENANCE",
    "QUALIFICATION_PROBE_PROVENANCE",
    "LIVE_EVIDENCE_ORIGINS",
    "QUALIFICATION_SCHEDULE_DIGEST",
    "PROBE_SCHEDULE_DIGEST",
    "qualification_ids",
    "qualification_probes",
    "NormalizedRejectionBinding",
    "NormalizedCell",
    "NormalizedProbe",
    "NormalizedTerminal",
    "QualificationCensus",
    "QualificationVerdict",
    "verify_qualification",
    "verify_qualification_projection",
    "normalized_cell_passes",
    "qualification_cell_clauses",
    "qualification_cell_clause_failures",
    "verify_qualification_semantics",
    "verify_census",
    "verify_frozen_qualification",
    "verify_qualification_census",
    "verify",
    "qualifies",
    "make_census",
    "build_census",
    "FrozenCell",
    "FrozenProbe",
    "FrozenTerminal",
    "FrozenQualificationCell",
    "FrozenQualificationProbe",
    "FrozenQualificationTerminal",
    "CellRecord",
    "ProbeRecord",
    "TerminalRecord",
    "RejectionBinding",
    "NormalizedRejection",
    "FrozenRejectionBinding",
    "FrozenQualificationCensus",
    "QualificationSemanticVerdict",
    "CellQualificationRecord",
    "ProbeQualificationRecord",
    "TerminalQualificationRecord",
    "CellAttestation",
    "ProbeAttestation",
    "TerminalAttestation",
]
