"""Authenticated qualification aggregates for the guarded K12 live path.

There are deliberately two kinds of objects in this module:

* ``QualificationAggregate`` and the ``qualify_mock_*`` helpers are the old
  deterministic test fixtures.  They are useful for exercising the A/R/S
  validators, but they are permanently ``mock_only`` and cannot be promoted.
* ``LiveQualificationAggregate`` is minted only at an active parent-owned
  qualification authority boundary.  It consumes the exact fifteen-cell
  schedule, a separate ordered P1--P4 probe set, and a terminal ledger event.

The live constructors accept observations supplied by a runner.  They never
execute a command, contact a provider, or manufacture an execution authority.
The latter is intentionally left to :mod:`k12_execution_provenance` and its
``ParentExecutionAuthority`` paths.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
import re
from threading import RLock
from typing import Any

from benchmarks.common.eac.canonical import canonical_argument, canonical_sha256
from .k12_authority_contracts import (
    _CONTRACT_MINT_TOKEN,
    QualificationAggregateOwnershipReceipt,
    QualificationEvidenceContract,
    QualificationTerminalEvidenceContract,
    install_qualification_evidence_contract,
)

from .k12_execution_provenance import (
    ActiveQualificationAuthority,
    AuthorityBinding,
    INJECTED_FAKE_ORIGIN,
    INJECTED_TEST_ORIGIN,
    LIVE_QUALIFICATION_NAMESPACE,
    QUALIFICATION_PROBE_NAMESPACE,
    PROFILE_V2,
    ProvenanceError,
    QualificationCoordinateExecutionReceipt,
    QualificationSemanticAttestation,
    QUALIFICATION_AUTHORITY,
    RUNTIME_VERIFIED_ORIGIN,
    authority_owns_profile,
)
from .k12_qualification_semantics import (
    NormalizedCell,
    NormalizedProbe,
    NormalizedRejectionBinding,
    QualificationCensus,
    PROBES as FROZEN_QUALIFICATION_PROBES,
    QUALIFICATION_SCHEDULE,
    normalized_cell_passes,
    qualification_cell_clauses,
    verify_qualification_projection,
)
from .k12_live_artifacts import DomainIdentity, LiveArtifact, thaw
from .k12_live_oracle import RejectionEvidence
from .k12_runtime_profile import (
    LIVE_QUALIFICATION_IDENTITY,
    LIVE_SCHEDULE_IDENTITY,
    detached_digest,
    load_k12_live_runtime_profile,
    strict_json_load,
)


PROBES = FROZEN_QUALIFICATION_PROBES
STRATA = ("S1", "S2", "S3", "S4", "S5")
QUALIFICATION_PHASE = "qualification"
LIVE_QUALIFICATION_PROVENANCE = "live_qualification"
QUALIFICATION_PROBE_PROVENANCE = "qualification_probe"
TEST_EVIDENCE_ORIGIN = "test_only"
# Operational provenance and evidence origin are deliberately distinct.  The
# compatibility name remains exported, but now denotes the runtime origin,
# not the operational ``live_qualification`` namespace.
LIVE_EVIDENCE_ORIGIN = RUNTIME_VERIFIED_ORIGIN
INJECTED_FAKE_EVIDENCE_ORIGIN = INJECTED_FAKE_ORIGIN
_LIVE_EVIDENCE_ORIGINS = frozenset({RUNTIME_VERIFIED_ORIGIN, INJECTED_FAKE_ORIGIN})
QUALIFICATION_PATH = Path(__file__).with_name("k12_live_qualification_v1.json")

_QUALIFICATION_TOKEN = object()
_LIVE_AGGREGATE_LOCK = RLock()
_LIVE_AGGREGATES: set[str] = set()
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_CANONICAL_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _raw_digest(value: Any) -> bool:
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def _canonical_digest(value: Any) -> bool:
    return isinstance(value, str) and _CANONICAL_DIGEST.fullmatch(value) is not None


def _identity(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is required")
    return value


def qualification_ids() -> tuple[str, ...]:
    """Return the authenticated, ordered fifteen-cell qualification census."""

    return QUALIFICATION_SCHEDULE


def qualification_cells() -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "cell_id": cell_id,
            "stratum": cell_id.split("-")[1],
            "arm": cell_id.rsplit("-", 1)[1],
            "status": "not_started",
        }
        for cell_id in qualification_ids()
    )


def qualification_artifact() -> LiveArtifact:
    """Return the historical generic artifact, never a live authority."""

    profile = load_k12_live_runtime_profile()
    return LiveArtifact(
        "qualification",
        tuple(qualification_cells()),
        DomainIdentity(profile["detached_artifact_sha256"], LIVE_QUALIFICATION_IDENTITY),
    )


def qualification_matrix() -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(cell["cell_id"] for cell in qualification_cells() if cell["stratum"] == stratum)
        for stratum in STRATA
    )


def qualification_schedule_digest() -> str:
    return canonical_sha256(list(qualification_ids()))


def _mock_binding() -> AuthorityBinding:
    return AuthorityBinding.mock_only()


def _authority_evidence_origin(authority: ActiveQualificationAuthority) -> str:
    """Return the only evidence origin an active authority may consume."""

    if not isinstance(authority, ActiveQualificationAuthority):
        raise TypeError("active parent qualification authority required")
    origin = authority.origin
    if origin == RUNTIME_VERIFIED_ORIGIN:
        if not authority.runtime_admissible:
            raise ProvenanceError("authority_origin_mismatch")
        return RUNTIME_VERIFIED_ORIGIN
    if origin == INJECTED_TEST_ORIGIN:
        owner = authority.owner
        if (
            authority.runtime_admissible
            or getattr(owner, "origin", None) != INJECTED_TEST_ORIGIN
            or not getattr(owner, "is_injected_test_controller", False)
        ):
            raise ProvenanceError("authority_origin_mismatch")
        return INJECTED_FAKE_ORIGIN
    raise ProvenanceError("authority_origin_mismatch")


def _default_evidence_origin(binding: AuthorityBinding) -> str:
    if binding.provenance == "mock_only":
        return TEST_EVIDENCE_ORIGIN
    if binding.origin == RUNTIME_VERIFIED_ORIGIN:
        return RUNTIME_VERIFIED_ORIGIN
    if binding.origin == INJECTED_TEST_ORIGIN:
        return INJECTED_FAKE_ORIGIN
    raise ProvenanceError("authority_origin_mismatch")


@dataclass(frozen=True, slots=True, init=False)
class QualificationTrace:
    cell_id: str
    events: Any
    profile_digest: str
    campaign_id: str
    reset_identity: str = ""
    evidence_digest: str = ""
    execution_provenance: str = field(init=False, default="mock_only")
    evidence_origin: str = field(init=False, default=TEST_EVIDENCE_ORIGIN)
    authority_binding: AuthorityBinding = field(default_factory=_mock_binding)
    reset_token: str | None = None
    generation: int | None = None
    request_identity: str | None = None
    permit_identity: str | None = None
    effect_identity: str | None = None
    rejection_identity: str | None = None
    identity: str = field(init=False)

    def __init__(
        self,
        cell_id: str,
        events: Any,
        profile_digest: str,
        campaign_id: str,
        reset_identity: str,
        evidence_digest: str,
        token: object = None,
        authority_binding: AuthorityBinding | None = None,
        evidence_origin: str | None = None,
        reset_token: str | None = None,
        generation: int | None = None,
        request_identity: str | None = None,
        permit_identity: str | None = None,
        effect_identity: str | None = None,
        rejection_identity: str | None = None,
    ) -> None:
        if token is not _QUALIFICATION_TOKEN:
            raise TypeError("qualification traces are coordinator-minted")
        binding = authority_binding or _mock_binding()
        for name, value in (
            ("cell_id", cell_id),
            ("events", events),
            ("profile_digest", profile_digest),
            ("campaign_id", campaign_id),
            ("reset_identity", reset_identity),
            ("evidence_digest", evidence_digest),
        ):
            object.__setattr__(self, name, value)
        object.__setattr__(self, "authority_binding", binding)
        for name, value in (
            ("reset_token", reset_token),
            ("generation", generation),
            ("request_identity", request_identity),
            ("permit_identity", permit_identity),
            ("effect_identity", effect_identity),
            ("rejection_identity", rejection_identity),
        ):
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "execution_provenance",
            binding.provenance if isinstance(binding, AuthorityBinding) else "mock_only",
        )
        object.__setattr__(
            self,
            "evidence_origin",
            evidence_origin
            or _default_evidence_origin(binding),
        )
        self.__post_init__()

    def __post_init__(self) -> None:
        if (
            self.cell_id not in qualification_ids()
            or not isinstance(self.authority_binding, AuthorityBinding)
            or not all(
                isinstance(value, str) and value
                for value in (
                    self.profile_digest,
                    self.campaign_id,
                    self.reset_identity,
                    self.evidence_digest,
                )
            )
            or self.evidence_origin not in {TEST_EVIDENCE_ORIGIN, *_LIVE_EVIDENCE_ORIGINS}
        ):
            raise ValueError("complete qualification trace binding is required")
        if (
            self.reset_token is not None
            and (not isinstance(self.reset_token, str) or not self.reset_token)
        ) or (
            self.generation is not None
            and (type(self.generation) is not int or self.generation <= 0)
        ) or any(
            value is not None and (not isinstance(value, str) or not value)
            for value in (
                self.request_identity,
                self.permit_identity,
                self.effect_identity,
                self.rejection_identity,
            )
        ):
            raise ValueError("qualification trace operation binding is invalid")
        object.__setattr__(self, "events", canonical_argument(self.events))
        identity_body = {
            "artifact": "minecraft-k12-live-qualification-trace/1",
            "cell_id": self.cell_id,
            "events": thaw(self.events),
            "profile": self.profile_digest,
            "campaign": self.campaign_id,
            "reset": self.reset_identity,
            "evidence": self.evidence_digest,
        }
        if self.authority_binding.provenance != "mock_only":
            identity_body.update(
                {
                    "artifact": "minecraft-k12-live-qualification-trace/2",
                    "authority_binding": self.authority_binding.canonical(),
                    "execution_provenance": self.execution_provenance,
                    "evidence_origin": self.evidence_origin,
                }
            )
            operation_binding = {
                "reset_token": self.reset_token,
                "generation": self.generation,
                "request_identity": self.request_identity,
                "permit_identity": self.permit_identity,
                "effect_identity": self.effect_identity,
                "rejection_identity": self.rejection_identity,
            }
            if any(value is not None for value in operation_binding.values()):
                identity_body["operation_binding"] = operation_binding
        object.__setattr__(
            self,
            "identity",
            canonical_sha256(identity_body),
        )


@dataclass(frozen=True, slots=True, init=False)
class QualificationCellResult:
    cell_id: str
    passed: bool
    fresh_root: bool
    retry: bool
    trace: QualificationTrace
    resumed: bool = False
    replacement: bool = False
    identity: str = field(init=False)

    def __init__(
        self,
        cell_id: str,
        passed: bool,
        fresh_root: bool,
        retry: bool,
        trace: QualificationTrace,
        resumed: bool = False,
        replacement: bool = False,
        token: object = None,
    ) -> None:
        if token is not _QUALIFICATION_TOKEN:
            raise TypeError("qualification results are coordinator-minted")
        for name, value in (
            ("cell_id", cell_id),
            ("passed", passed),
            ("fresh_root", fresh_root),
            ("retry", retry),
            ("trace", trace),
            ("resumed", resumed),
            ("replacement", replacement),
        ):
            object.__setattr__(self, name, value)
        self.__post_init__()

    def __post_init__(self) -> None:
        if not isinstance(self.trace, QualificationTrace) or self.trace.cell_id != self.cell_id:
            raise ValueError("qualification result/trace cell mismatch")
        if self.retry or self.resumed or self.replacement or not self.fresh_root:
            object.__setattr__(self, "passed", False)
        object.__setattr__(
            self,
            "identity",
            canonical_sha256(
                {
                    "artifact": "minecraft-k12-live-qualification-cell-result/1",
                    "cell_id": self.cell_id,
                    "passed": self.passed,
                    "fresh_root": self.fresh_root,
                    "retry": self.retry,
                    "resumed": self.resumed,
                    "replacement": self.replacement,
                    "trace": self.trace.identity,
                }
            ),
        )


@dataclass(frozen=True, slots=True, init=False)
class ProbeAggregate:
    """The ordered P1--P4 aggregate, mock or authority-bound."""

    probes: tuple[str, ...]
    passed: bool
    profile_digest: str
    campaign_id: str
    evidence_digest: str
    execution_provenance: str = field(init=False, default="mock_only")
    evidence_origin: str = field(init=False, default=TEST_EVIDENCE_ORIGIN)
    authority_binding: AuthorityBinding = field(default_factory=_mock_binding)
    identity: str = field(init=False)

    def __init__(
        self,
        probes: Sequence[str],
        passed: bool,
        profile_digest: str,
        campaign_id: str,
        evidence_digest: str,
        token: object = None,
        authority_binding: AuthorityBinding | None = None,
        evidence_origin: str | None = None,
    ) -> None:
        if token is not _QUALIFICATION_TOKEN:
            raise TypeError("probe aggregates are coordinator-minted")
        binding = authority_binding or _mock_binding()
        for name, value in (
            ("probes", tuple(probes)),
            ("passed", passed),
            ("profile_digest", profile_digest),
            ("campaign_id", campaign_id),
            ("evidence_digest", evidence_digest),
        ):
            object.__setattr__(self, name, value)
        object.__setattr__(self, "authority_binding", binding)
        object.__setattr__(
            self,
            "execution_provenance",
            binding.provenance if isinstance(binding, AuthorityBinding) else "mock_only",
        )
        object.__setattr__(
            self,
            "evidence_origin",
            evidence_origin
            or _default_evidence_origin(binding),
        )
        self.__post_init__()

    def __post_init__(self) -> None:
        if (
            self.probes != PROBES
            or not isinstance(self.authority_binding, AuthorityBinding)
            or not all(
                isinstance(value, str) and value
                for value in (self.profile_digest, self.campaign_id, self.evidence_digest)
            )
            or self.evidence_origin not in {TEST_EVIDENCE_ORIGIN, *_LIVE_EVIDENCE_ORIGINS}
        ):
            raise ValueError("P1-P4 aggregate is exhaustive and bound")
        identity_body = {
            "artifact": "minecraft-k12-live-containment-probe/1",
            "probes": list(self.probes),
            "passed": self.passed,
            "profile": self.profile_digest,
            "campaign": self.campaign_id,
            "evidence": self.evidence_digest,
        }
        if self.authority_binding.provenance != "mock_only":
            identity_body.update(
                {
                    "artifact": "minecraft-k12-live-containment-probe/2",
                    "authority_binding": self.authority_binding.canonical(),
                    "execution_provenance": self.execution_provenance,
                    "evidence_origin": self.evidence_origin,
                }
            )
        object.__setattr__(self, "identity", canonical_sha256(identity_body))

    def qualifies(self) -> bool:
        return self.passed is True and self.probes == PROBES


@dataclass(frozen=True, slots=True, init=False)
class QualificationAggregate:
    """Historical mock aggregate; it is intentionally not a live aggregate."""

    results: tuple[QualificationCellResult, ...]
    profile_digest: str
    campaign_id: str
    identity: str = field(init=False)
    authority_binding: AuthorityBinding = field(default_factory=_mock_binding)
    execution_provenance: str = field(init=False, default="mock_only")
    evidence_origin: str = field(init=False, default=TEST_EVIDENCE_ORIGIN)

    def __init__(
        self,
        results: Sequence[QualificationCellResult],
        profile_digest: str,
        campaign_id: str,
        token: object = None,
        authority_binding: AuthorityBinding | None = None,
    ) -> None:
        if token is not _QUALIFICATION_TOKEN:
            raise TypeError("qualification aggregates are coordinator-minted")
        object.__setattr__(self, "results", tuple(results))
        object.__setattr__(self, "profile_digest", profile_digest)
        object.__setattr__(self, "campaign_id", campaign_id)
        object.__setattr__(self, "authority_binding", authority_binding or _mock_binding())
        object.__setattr__(self, "execution_provenance", "mock_only")
        object.__setattr__(self, "evidence_origin", TEST_EVIDENCE_ORIGIN)
        self.__post_init__()

    def __post_init__(self) -> None:
        if not isinstance(self.authority_binding, AuthorityBinding) or any(
            not isinstance(result, QualificationCellResult)
            or result.trace.authority_binding != self.authority_binding
            for result in self.results
        ):
            raise ValueError("qualification authority binding mismatch")
        identity_body = {
            "artifact": "minecraft-k12-live-qualification-aggregate/1",
            "results": [result.identity for result in self.results],
            "profile": self.profile_digest,
            "campaign": self.campaign_id,
        }
        if self.authority_binding.provenance != "mock_only":
            identity_body["authority_binding"] = self.authority_binding.canonical()
        object.__setattr__(self, "identity", canonical_sha256(identity_body))

    def qualifies(self) -> bool:
        return (
            len(self.results) == 15
            and tuple(result.cell_id for result in self.results) == qualification_ids()
            and len({result.identity for result in self.results}) == 15
            and len({result.trace.identity for result in self.results}) == 15
            and all(
                result.passed
                and result.fresh_root
                and not result.retry
                and not result.resumed
                and not result.replacement
                and result.trace.profile_digest == self.profile_digest
                and result.trace.campaign_id == self.campaign_id
                and result.trace.execution_provenance == "mock_only"
                and self.authority_binding.provenance == "mock_only"
                for result in self.results
            )
        )

    @property
    def schedule_identity(self) -> str:
        return LIVE_SCHEDULE_IDENTITY

    @property
    def qualification_identity(self) -> str:
        return LIVE_QUALIFICATION_IDENTITY


@dataclass(frozen=True, slots=True)
class MockCellQualificationEvidence:
    cell_id: str
    events: Any
    profile_digest: str
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
    retry: bool = False
    resumed: bool = False
    replacement: bool = False


def _mock_cell_passes(value: MockCellQualificationEvidence) -> bool:
    return qualification_cell_clauses(
        cell_id=value.cell_id,
        fresh_root=value.fresh_root,
        reset_passed=value.reset_passed,
        capability_state=value.capability_state,
        provider_terminal=value.provider_terminal,
        oracle_value=value.oracle_value,
        containment_clean=value.containment_clean,
        evidence_valid=value.evidence_valid,
        terminal_verified=True,
        rejection_verified=True,
        retry=value.retry,
        resumed=value.resumed,
        replacement=value.replacement,
        # Mock evidence predates typed stale-rejection receipts and is barred
        # from every live/final authority path by its provenance.
        rejection_matches=True,
    )


def qualify_mock_cell(value: MockCellQualificationEvidence) -> QualificationCellResult:
    if not isinstance(value, MockCellQualificationEvidence):
        raise TypeError("typed mock cell evidence required")
    trace = QualificationTrace(
        value.cell_id,
        value.events,
        value.profile_digest,
        value.campaign_id,
        value.reset_identity,
        value.evidence_digest,
        _QUALIFICATION_TOKEN,
        _mock_binding(),
        TEST_EVIDENCE_ORIGIN,
    )
    return QualificationCellResult(
        value.cell_id,
        _mock_cell_passes(value),
        value.fresh_root,
        value.retry,
        trace,
        value.resumed,
        value.replacement,
        _QUALIFICATION_TOKEN,
    )


def qualify_mock_campaign(
    values: tuple[MockCellQualificationEvidence, ...],
) -> QualificationAggregate:
    if type(values) is not tuple or any(
        not isinstance(value, MockCellQualificationEvidence) for value in values
    ) or tuple(value.cell_id for value in values) != qualification_ids():
        raise ValueError("exact qualification evidence order required")
    results = tuple(qualify_mock_cell(value) for value in values)
    profiles = {value.profile_digest for value in values}
    campaigns = {value.campaign_id for value in values}
    if len(profiles) != 1 or len(campaigns) != 1:
        raise ValueError("qualification evidence binding mismatch")
    return QualificationAggregate(
        results,
        profiles.pop(),
        campaigns.pop(),
        _QUALIFICATION_TOKEN,
        _mock_binding(),
    )


@dataclass(frozen=True, slots=True)
class MockProbeEvidence:
    probe: str
    passed: bool
    profile_digest: str
    campaign_id: str
    evidence_digest: str


def qualify_mock_probes(values: tuple[MockProbeEvidence, ...]) -> ProbeAggregate:
    if type(values) is not tuple or any(
        not isinstance(value, MockProbeEvidence) for value in values
    ) or tuple(value.probe for value in values) != PROBES:
        raise ValueError("exact P1-P4 evidence required")
    profiles = {value.profile_digest for value in values}
    campaigns = {value.campaign_id for value in values}
    if len(profiles) != 1 or len(campaigns) != 1 or any(
        not value.evidence_digest for value in values
    ):
        raise ValueError("probe evidence binding mismatch")
    evidence = canonical_sha256(
        {"probe_evidence": [value.evidence_digest for value in values]}
    )
    return ProbeAggregate(
        PROBES,
        all(value.passed for value in values),
        profiles.pop(),
        campaigns.pop(),
        evidence,
        _QUALIFICATION_TOKEN,
        _mock_binding(),
        TEST_EVIDENCE_ORIGIN,
    )


# ---------------------------------------------------------------------------
# Authority-owned live qualification


@dataclass(frozen=True, slots=True)
class LiveQualificationCellEvidence:
    """Injected terminal observation for one live qualification cell.

    ``evidence_origin`` is ``injected_fake`` by default.  That makes fake
    observations useful for controller-owned validator tests while ensuring
    they can never satisfy a runtime final-admission protocol.  A real runner
    must explicitly provide ``runtime_verified`` after authenticating its
    evidence.
    """

    cell_id: str
    events: Any
    profile_digest: str
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
    terminal_verified: bool = True
    authority_binding: AuthorityBinding | None = None
    evidence_origin: str = INJECTED_FAKE_EVIDENCE_ORIGIN
    execution_provenance: str = LIVE_QUALIFICATION_PROVENANCE
    rejection_verified: bool = True
    retry: bool = False
    resumed: bool = False
    replacement: bool = False
    rejection_evidence: RejectionEvidence | None = None
    # These mirrors may be supplied directly or through the event mapping, but
    # S-arm evidence must expose all of them before it can qualify.  Keeping
    # the fields optional preserves the observation shape for A/R cells.
    reset_token: str | None = None
    generation: int | None = None
    request_identity: str | None = None
    permit_identity: str | None = None
    effect_identity: str | None = None
    terminal_capability: Any = field(default=None, repr=False, compare=False)
    execution_receipt: QualificationCoordinateExecutionReceipt | None = field(
        default=None, repr=False, compare=False,
    )


LiveCellQualificationEvidence = LiveQualificationCellEvidence
LiveQualificationCellObservation = LiveQualificationCellEvidence
LiveCellEvidence = LiveQualificationCellEvidence


@dataclass(frozen=True, slots=True)
class LiveQualificationProbeEvidence:
    probe: str
    passed: bool
    profile_digest: str
    campaign_id: str
    evidence_digest: str
    terminal_verified: bool = True
    authority_binding: AuthorityBinding | None = None
    evidence_origin: str = INJECTED_FAKE_EVIDENCE_ORIGIN
    execution_provenance: str = QUALIFICATION_PROBE_PROVENANCE
    terminal_capability: Any = field(default=None, repr=False, compare=False)
    execution_receipt: QualificationCoordinateExecutionReceipt | None = field(
        default=None, repr=False, compare=False,
    )


LiveProbeEvidence = LiveQualificationProbeEvidence
LiveQualificationProbeObservation = LiveQualificationProbeEvidence
LiveProbeObservation = LiveQualificationProbeEvidence


@dataclass(frozen=True, slots=True)
class QualificationTerminalEvidence(QualificationTerminalEvidenceContract):
    """The parent ledger's terminal, passed qualification event."""

    ledger_digest: str
    aggregate_digest: str
    probe_digest: str
    authority_binding: AuthorityBinding
    result: str = "passed"
    state: str = "terminal"
    verified: bool = True
    evidence_origin: str = INJECTED_FAKE_EVIDENCE_ORIGIN
    identity: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            not _canonical_digest(self.ledger_digest)
            or not _canonical_digest(self.aggregate_digest)
            or not _canonical_digest(self.probe_digest)
            or not isinstance(self.authority_binding, AuthorityBinding)
            or self.result != "passed"
            or self.state != "terminal"
            or self.verified is not True
            or self.evidence_origin not in {TEST_EVIDENCE_ORIGIN, *_LIVE_EVIDENCE_ORIGINS}
        ):
            raise ValueError("verified qualification terminal evidence is required")
        object.__setattr__(
            self,
            "identity",
            canonical_sha256(
                {
                    "artifact": "minecraft-k12-live-qualification-terminal/1",
                    "ledger": self.ledger_digest,
                    "aggregate": self.aggregate_digest,
                    "probe": self.probe_digest,
                    "authority_binding": self.authority_binding.canonical(),
                    "result": self.result,
                    "state": self.state,
                    "verified": self.verified,
                    "evidence_origin": self.evidence_origin,
                }
            ),
        )


LiveQualificationTerminalEvidence = QualificationTerminalEvidence
LiveTerminalEvidence = QualificationTerminalEvidence


def _active_qualification_binding(
    authority: ActiveQualificationAuthority,
) -> AuthorityBinding:
    if not isinstance(authority, ActiveQualificationAuthority):
        raise TypeError("active parent qualification authority required")
    expected_origin = _authority_evidence_origin(authority)
    if authority.lifecycle != "active" or authority.authority_binding.lifecycle != "active":
        raise ProvenanceError("authority_replay")
    if (
        authority.authority_binding.provenance != LIVE_QUALIFICATION_PROVENANCE
        or authority.authority_binding.namespace != LIVE_QUALIFICATION_NAMESPACE
        or authority.authority_binding.origin != authority.origin
    ):
        raise ProvenanceError("authority_origin_mismatch")
    if authority.body.get("evidence_origin") != authority.origin:
        raise ProvenanceError("authority_origin_mismatch")
    if authority.body.get("profile", {}).get("identity") != PROFILE_V2:
        raise ProvenanceError("profile_mismatch")
    if not _raw_digest(authority.profile_digest):
        raise ProvenanceError("profile_mismatch")
    owns_profile = authority_owns_profile(
        authority,
        authority.binding,
        profile_id=PROFILE_V2,
        profile_digest=authority.profile_digest,
    )
    if not owns_profile:
        try:
            owns_profile = authority.owner.owns_qualification_semantic_attestation(
                authority.owner.qualification_semantic_attestation(authority),
                authority=authority,
            )
        except (AttributeError, TypeError, ValueError, ProvenanceError):
            owns_profile = False
    if not owns_profile:
        raise ProvenanceError("authority_replay")
    if not authority.owns(authority.binding):
        raise ProvenanceError("authority_namespace_mismatch")
    return authority.binding


def _probe_binding(authority: ActiveQualificationAuthority) -> AuthorityBinding:
    binding = authority.authority.binding(QUALIFICATION_PROBE_NAMESPACE)
    if (
        not authority.owns(binding)
        or binding.provenance != QUALIFICATION_PROBE_PROVENANCE
        or binding.namespace != QUALIFICATION_PROBE_NAMESPACE
        or binding.origin != authority.origin
    ):
        raise ProvenanceError("authority_namespace_mismatch")
    return binding


def _authority_ledger(authority: ActiveQualificationAuthority) -> Any:
    ledger = getattr(getattr(authority, "authority", None), "ledger", None)
    if ledger is None:
        raise ProvenanceError("authority_replay")
    return ledger


def _event_expectations(value: LiveQualificationCellEvidence, *names: str) -> tuple[Any, ...]:
    """Return operation identities copied into the cell event, when present."""

    found: list[Any] = []
    events = value.events
    if isinstance(events, Mapping):
        for name in names:
            if name in events and events[name] is not None:
                found.append(events[name])
    return tuple(found)


def _event_expectation(
    value: LiveQualificationCellEvidence, explicit: Any, *names: str
) -> Any:
    if explicit is not None:
        return explicit
    found = _event_expectations(value, *names)
    return found[0] if found else None


def _rejection_binding_matches(
    value: LiveQualificationCellEvidence, rejection: RejectionEvidence
) -> bool:
    binding = rejection.binding
    if (
        not isinstance(binding.reset_token, str)
        or not binding.reset_token
        or type(binding.generation) is not int
        or binding.generation <= 0
        or not all(
            isinstance(identity, str) and identity
            for identity in (binding.request, binding.permit, binding.effect)
        )
    ):
        return False
    expected_reset_tokens = [value.reset_identity]
    if value.reset_token is not None:
        expected_reset_tokens.append(value.reset_token)
    expected_reset_tokens.extend(_event_expectations(value, "reset_token", "reset_identity"))
    if any(binding.reset_token != expected for expected in expected_reset_tokens):
        return False
    expected_values = (
        (binding.generation, value.generation, ("generation",)),
        (binding.request, value.request_identity, ("request_identity", "request", "request_id")),
        (binding.permit, value.permit_identity, ("permit_identity", "permit", "permit_id")),
        (binding.effect, value.effect_identity, ("effect_identity", "effect", "effect_id")),
    )
    for binding_value, explicit, names in expected_values:
        expectations = (() if explicit is None else (explicit,)) + _event_expectations(
            value, *names
        )
        if not expectations:
            return False
        if any(binding_value != expected for expected in expectations):
            return False
    return True


def _live_cell_passes(
    authority: ActiveQualificationAuthority,
    value: LiveQualificationCellEvidence,
    trace: QualificationTrace,
) -> bool:
    if value.cell_id.endswith("-S"):
        rejection = value.rejection_evidence
        if not (
            isinstance(rejection, RejectionEvidence)
            and rejection.binding.arm == "S"
            and rejection.binding.cell == value.cell_id
            and rejection.binding.profile == value.profile_digest
            and rejection.binding.campaign == value.campaign_id
            and value.authority_binding is not None
            and rejection.authority_binding == value.authority_binding
            and _rejection_binding_matches(value, rejection)
            and rejection.evidence_digest == value.evidence_digest
            and rejection.evidence_origin == value.evidence_origin
        ):
            return False
    return normalized_cell_passes(
        _normalized_live_cell_from_trace(authority, value, trace)
    )


def qualify_live_cell(
    authority: ActiveQualificationAuthority,
    value: LiveQualificationCellEvidence,
) -> QualificationCellResult:
    """Normalize one observation for diagnostics; never mint a terminal receipt."""

    binding = _active_qualification_binding(authority)
    expected_origin = _authority_evidence_origin(authority)
    if not isinstance(value, LiveQualificationCellEvidence):
        raise TypeError("typed live qualification cell evidence required")
    if value.evidence_origin != expected_origin:
        raise ProvenanceError("authority_origin_mismatch")
    if value.authority_binding is not None and value.authority_binding != binding:
        raise ProvenanceError("authority_namespace_mismatch")
    if value.profile_digest != authority.profile_digest:
        raise ProvenanceError("profile_mismatch")
    arm = value.cell_id.rsplit("-", 1)[-1]
    reset_token = value.reset_token
    generation = value.generation
    request_identity = value.request_identity
    permit_identity = value.permit_identity
    effect_identity = value.effect_identity
    if arm == "S":
        reset_token = _event_expectation(value, reset_token, "reset_token", "reset_identity")
        if reset_token is None:
            reset_token = value.reset_identity
        generation = _event_expectation(value, generation, "generation")
        request_identity = _event_expectation(
            value, request_identity, "request_identity", "request", "request_id"
        )
        permit_identity = _event_expectation(
            value, permit_identity, "permit_identity", "permit", "permit_id"
        )
        effect_identity = _event_expectation(
            value, effect_identity, "effect_identity", "effect", "effect_id"
        )
    trace = QualificationTrace(
        value.cell_id,
        value.events,
        value.profile_digest,
        value.campaign_id,
        value.reset_identity,
        value.evidence_digest,
        _QUALIFICATION_TOKEN,
        binding,
        value.evidence_origin,
        reset_token,
        generation,
        request_identity,
        permit_identity,
        effect_identity,
        value.rejection_evidence.identity
        if isinstance(value.rejection_evidence, RejectionEvidence)
        else None,
    )
    return QualificationCellResult(
        value.cell_id,
        _live_cell_passes(authority, value, trace),
        value.fresh_root,
        value.retry,
        trace,
        value.resumed,
        value.replacement,
        _QUALIFICATION_TOKEN,
    )


def qualify_live_probes(
    authority: ActiveQualificationAuthority,
    values: tuple[LiveQualificationProbeEvidence, ...],
) -> ProbeAggregate:
    """Build a diagnostic P1--P4 aggregate; never mint a terminal receipt."""

    try:
        binding = _probe_binding(authority)
    except ProvenanceError:
        try:
            attestation = authority.owner.qualification_semantic_attestation(authority)
            binding = authority.owner.qualification_probe_binding(authority)
        except (AttributeError, TypeError, ValueError, ProvenanceError):
            attestation = None
            binding = None
        if (not isinstance(binding, AuthorityBinding)
                or binding.namespace != QUALIFICATION_PROBE_NAMESPACE
                or binding.authority_digest != authority.identity
                or not authority.owner.owns_qualification_semantic_attestation(
                    attestation, authority=authority,
                )):
            raise ProvenanceError("authority_namespace_mismatch")
    expected_origin = _authority_evidence_origin(authority)
    if type(values) is not tuple or any(
        not isinstance(value, LiveQualificationProbeEvidence) for value in values
    ) or tuple(value.probe for value in values) != PROBES:
        raise ValueError("exact ordered P1-P4 evidence required")
    profiles = {value.profile_digest for value in values}
    campaigns = {value.campaign_id for value in values}
    origins = {value.evidence_origin for value in values}
    if (
        profiles != {authority.profile_digest}
        or len(campaigns) != 1
        or origins != {expected_origin}
        or any(
            value.authority_binding is not None and value.authority_binding != binding
            for value in values
        )
        or any(
            value.execution_provenance != QUALIFICATION_PROBE_PROVENANCE
            or value.terminal_verified is not True
            or not isinstance(value.evidence_digest, str)
            or not value.evidence_digest
            for value in values
        )
    ):
        raise ValueError("probe evidence binding mismatch")
    evidence = canonical_sha256(
        {"probe_evidence": [value.evidence_digest for value in values]}
    )
    return ProbeAggregate(
        PROBES,
        all(value.passed is True for value in values),
        authority.profile_digest,
        campaigns.pop(),
        evidence,
        _QUALIFICATION_TOKEN,
        binding,
        origins.pop(),
    )


def _coerce_terminal(
    value: QualificationTerminalEvidence | Mapping[str, Any],
    *,
    binding: AuthorityBinding,
    aggregate_digest: str,
    probe_digest: str,
    evidence_origin: str,
) -> QualificationTerminalEvidence:
    if isinstance(value, QualificationTerminalEvidence):
        terminal = value
    elif isinstance(value, Mapping):
        try:
            terminal = QualificationTerminalEvidence(
                ledger_digest=value["ledger_digest"],
                aggregate_digest=value.get("aggregate_digest", aggregate_digest),
                probe_digest=value.get("probe_digest", probe_digest),
                authority_binding=value.get("authority_binding", binding),
                result=value.get("result", "passed"),
                state=value.get("state", "terminal"),
                verified=value.get("verified", False),
                evidence_origin=value.get("evidence_origin", evidence_origin),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("typed terminal evidence is incomplete") from exc
    else:
        raise TypeError("typed qualification terminal evidence required")
    if (
        terminal.authority_binding != binding
        or terminal.aggregate_digest != aggregate_digest
        or terminal.probe_digest != probe_digest
        or terminal.evidence_origin != evidence_origin
    ):
        raise ProvenanceError("first_consume_mismatch")
    return terminal


def _terminalize_failed_qualification(
    authority: ActiveQualificationAuthority,
    ledger: Any,
    *,
    aggregate_digest: str,
    probe_digest: str,
    evidence_origin: str,
    reason: str,
) -> str:
    """Durably close a non-passing qualification without a passed event."""

    if ledger is not authority.authority.ledger:
        raise ProvenanceError("authority_replay")
    if getattr(ledger, "namespace", None) != "qualification":
        raise ProvenanceError("authority_replay")
    if getattr(ledger, "reservation_id", None) != authority.reservation_id:
        raise ProvenanceError("authority_replay")
    if not getattr(ledger, "verify_chain", lambda: False)():
        raise ProvenanceError("ledger_corrupt")
    if ledger.state != "active":
        raise ProvenanceError("first_consume_mismatch")
    payload = {
        "result": "failed",
        "phase": QUALIFICATION_PHASE,
        "authority_digest": authority.identity,
        "qualification_aggregate_digest": aggregate_digest,
        "probe_aggregate_digest": probe_digest,
        "evidence_origin": evidence_origin,
        "terminal_verified": False,
        "failure_reason": reason,
    }
    try:
        event_digest = authority.owner.ledger_terminal(ledger, payload)
    except Exception as exc:
        raise ProvenanceError("authority_replay") from exc
    if ledger.state != "terminal" or not ledger.events:
        raise ProvenanceError("first_consume_mismatch")
    event = ledger.events[-1]
    if event.state != "terminal" or dict(event.payload) != payload:
        raise ProvenanceError("first_consume_mismatch")
    if event.digest != event_digest:
        raise ProvenanceError("first_consume_mismatch")
    return event_digest


def _validate_live_aggregate_components(
    results: tuple[QualificationCellResult, ...],
    probes: ProbeAggregate,
    profile_digest: str,
    campaign_id: str,
    authority_binding: AuthorityBinding,
    authority: ActiveQualificationAuthority,
    evidence_origin: str,
    *,
    require_probe_passed: bool,
) -> None:
    """Validate every positive-aggregate invariant before terminalization."""

    if not isinstance(authority, ActiveQualificationAuthority):
        raise ValueError("live qualification aggregate is incomplete")
    expected_origin = _authority_evidence_origin(authority)
    if (
        not isinstance(authority_binding, AuthorityBinding)
        or authority_binding.provenance != LIVE_QUALIFICATION_PROVENANCE
        or authority_binding.authority_type != QUALIFICATION_AUTHORITY
        or authority_binding.namespace != LIVE_QUALIFICATION_NAMESPACE
        or authority_binding.lifecycle != "active"
        or authority_binding.origin != authority.origin
        or evidence_origin != expected_origin
        or evidence_origin not in _LIVE_EVIDENCE_ORIGINS
        or not authority.owns(authority_binding)
    ):
        raise ProvenanceError("authority_namespace_mismatch")
    if not isinstance(probes, ProbeAggregate):
        raise ValueError("live qualification aggregate is incomplete")
    if (
        probes.execution_provenance != QUALIFICATION_PROBE_PROVENANCE
        or not isinstance(probes.authority_binding, AuthorityBinding)
        or probes.authority_binding.authority_type != QUALIFICATION_AUTHORITY
        or probes.authority_binding.namespace != QUALIFICATION_PROBE_NAMESPACE
        or probes.authority_binding.lifecycle != "active"
        or probes.authority_binding.origin != authority.origin
        or not authority.owns(probes.authority_binding)
        or probes.authority_binding.authority_digest != authority_binding.authority_digest
        or probes.probes != PROBES
        or probes.profile_digest != profile_digest
        or probes.evidence_origin != evidence_origin
        or probes.campaign_id == campaign_id
        or (require_probe_passed and probes.passed is not True)
    ):
        raise ValueError("live qualification aggregate is incomplete")
    if (
        not isinstance(profile_digest, str)
        or not profile_digest
        or not isinstance(campaign_id, str)
        or not campaign_id
        or len(results) != 15
        or any(not isinstance(result, QualificationCellResult) for result in results)
        or tuple(result.cell_id for result in results) != qualification_ids()
        or len({result.identity for result in results}) != 15
        or len({result.trace.identity for result in results}) != 15
    ):
        raise ValueError("live qualification aggregate is incomplete")
    if any(
        not isinstance(result, QualificationCellResult)
        or not result.passed
        or not result.fresh_root
        or result.retry
        or result.resumed
        or result.replacement
        or result.trace.authority_binding != authority_binding
        or result.trace.execution_provenance != LIVE_QUALIFICATION_PROVENANCE
        or result.trace.evidence_origin != evidence_origin
        or result.trace.profile_digest != profile_digest
        or result.trace.campaign_id != campaign_id
        or (
            result.cell_id.rsplit("-", 1)[-1] == "S"
            and (
                not result.trace.reset_token
                or type(result.trace.generation) is not int
                or result.trace.generation <= 0
                or not result.trace.request_identity
                or not result.trace.permit_identity
                or not result.trace.effect_identity
                or not result.trace.rejection_identity
            )
        )
        for result in results
    ):
        raise ValueError("live qualification aggregate is incomplete")


@dataclass(frozen=True, slots=True, init=False)
class LiveQualificationAggregate(QualificationEvidenceContract):
    """Positive aggregate minted from an active qualification authority."""

    results: tuple[QualificationCellResult, ...]
    probes: ProbeAggregate
    profile_digest: str
    campaign_id: str
    authority_binding: AuthorityBinding
    terminal: QualificationTerminalEvidence
    authority: ActiveQualificationAuthority = field(repr=False, compare=False)
    semantic_attestation: QualificationSemanticAttestation = field(
        repr=False, compare=False,
    )
    execution_provenance: str = field(init=False, default=LIVE_QUALIFICATION_PROVENANCE)
    evidence_origin: str = field(init=False)
    identity: str = field(init=False)
    _ownership_marker: object = field(repr=False, compare=False)

    def __init__(
        self,
        results: tuple[QualificationCellResult, ...],
        probes: ProbeAggregate,
        profile_digest: str,
        campaign_id: str,
        authority_binding: AuthorityBinding,
        terminal: QualificationTerminalEvidence,
        authority: ActiveQualificationAuthority,
        semantic_attestation: QualificationSemanticAttestation,
        token: object = None,
    ) -> None:
        del results, probes, profile_digest, campaign_id, authority_binding
        del terminal, authority, semantic_attestation, token
        raise TypeError("live qualification aggregates are factory-minted")

    def __post_init__(self) -> None:
        _validate_live_aggregate_components(
            self.results,
            self.probes,
            self.profile_digest,
            self.campaign_id,
            self.authority_binding,
            self.authority,
            self.evidence_origin,
            require_probe_passed=True,
        )
        if (
            not isinstance(self.terminal, QualificationTerminalEvidence)
            or self.terminal.aggregate_digest == ""
            or self.terminal.probe_digest != self.probes.identity
            or self.terminal.authority_binding != self.authority_binding
            or self.terminal.evidence_origin != self.evidence_origin
            or not isinstance(self.semantic_attestation, QualificationSemanticAttestation)
            or not self.authority.owner.owns_qualification_semantic_attestation(
                self.semantic_attestation, authority=self.authority,
            )
        ):
            raise ValueError("live qualification aggregate is incomplete")
        object.__setattr__(
            self,
            "identity",
            canonical_sha256(
                {
                    "artifact": "minecraft-k12-live-qualification-aggregate/3",
                    "results": [result.identity for result in self.results],
                    "probes": self.probes.identity,
                    "profile": self.profile_digest,
                    "campaign": self.campaign_id,
                    "authority_binding": self.authority_binding.canonical(),
                    "execution_provenance": self.execution_provenance,
                    "evidence_origin": self.evidence_origin,
                }
            ),
        )
        if self.terminal.aggregate_digest != self.identity:
            raise ValueError("qualification terminal does not bind aggregate")

    def qualifies(self) -> bool:
        # Scientific qualification is decided only by the parent registry's
        # neutral semantic verifier.  The remaining checks merely ensure this
        # optional presentation artifact has not drifted from its own fields.
        return (
            self.authority.owner.owns_qualification_semantic_attestation(
                self.semantic_attestation, authority=self.authority,
            )
            and len(self.results) == len(QUALIFICATION_SCHEDULE)
            and tuple(result.cell_id for result in self.results) == qualification_ids()
            and len({result.identity for result in self.results}) == 15
            and len({result.trace.identity for result in self.results}) == 15
            and all(
                result.passed
                and result.fresh_root
                and not result.retry
                and not result.resumed
                and not result.replacement
                and result.trace.profile_digest == self.profile_digest
                and result.trace.campaign_id == self.campaign_id
                and result.trace.authority_binding == self.authority_binding
                and result.trace.execution_provenance == LIVE_QUALIFICATION_PROVENANCE
                and result.trace.evidence_origin == self.evidence_origin
                for result in self.results
            )
            and self.probes.passed
            and self.probes.probes == PROBES
            and self.probes.profile_digest == self.profile_digest
            and self.probes.execution_provenance == QUALIFICATION_PROBE_PROVENANCE
            and self.probes.evidence_origin == self.evidence_origin
            and self.terminal.verified is True
            and self.terminal.state == "terminal"
            and self.terminal.result == "passed"
            and self.terminal.aggregate_digest == self.identity
            and self.terminal.probe_digest == self.probes.identity
            and self.terminal.evidence_origin == self.evidence_origin
        )

    @property
    def origin(self) -> str:
        return self.evidence_origin

    @property
    def runtime_admissible(self) -> bool:
        return (
            self.evidence_origin == RUNTIME_VERIFIED_ORIGIN
            and self.authority.runtime_admissible
        )

    @property
    def controller(self) -> Any:
        return self.authority.owner

    @property
    def passed(self) -> bool:
        return self.qualifies()

    @property
    def schedule_identity(self) -> str:
        return LIVE_SCHEDULE_IDENTITY

    @property
    def qualification_identity(self) -> str:
        return LIVE_QUALIFICATION_IDENTITY

    @property
    def qualification_aggregate_digest(self) -> str:
        return self.identity

    @property
    def aggregate_digest(self) -> str:
        return self.identity

    @property
    def probe_aggregate_digest(self) -> str:
        return self.probes.identity

    @property
    def qualification_terminal_ledger_digest(self) -> str:
        return self.terminal.ledger_digest

    @property
    def terminal_ledger_digest(self) -> str:
        return self.terminal.ledger_digest

    @property
    def terminal_evidence(self) -> QualificationTerminalEvidence:
        return self.terminal

    @property
    def qualification_terminal_evidence(self) -> QualificationTerminalEvidence:
        return self.terminal

    @property
    def qualification_semantic_attestation(self) -> QualificationSemanticAttestation:
        return self.semantic_attestation

    @property
    def qualification_semantic_attestation_digest(self) -> str:
        return self.semantic_attestation.identity

    @property
    def qualification_binding(self) -> AuthorityBinding:
        return self.authority_binding

    @property
    def authority_identity(self) -> str:
        return self.authority.identity

    @property
    def ownership_receipt(self) -> "QualificationAggregateOwnershipReceipt":
        """Return the parent-minted capability proving aggregate ownership."""

        return QualificationEvidenceContract.ownership_receipt.fget(self)

    aggregate_ownership_receipt = ownership_receipt
    final_prerequisite_receipt = ownership_receipt

    def authenticate_for_final_prerequisites(
        self, authority: Any = None, controller: Any = None
    ) -> "QualificationAggregateOwnershipReceipt":
        """Authenticate this concrete aggregate at a final-prerequisite boundary."""

        receipt = self.ownership_receipt
        resolved_controller = self.controller if controller is None else controller
        if (not receipt.authenticates(
                self, authority=authority, controller=resolved_controller)
                or resolved_controller is not self.controller
                or not bool(getattr(
                resolved_controller,
                "owns_qualification_semantic_attestation",
                lambda _attestation, **_kwargs: False,
            )(self.semantic_attestation, authority=self.authority))):
            raise ProvenanceError("final_prerequisite_mismatch")
        return receipt

    verify_aggregate_ownership = authenticate_for_final_prerequisites

    @property
    def probe_campaign_id(self) -> str:
        return self.probes.campaign_id

    @classmethod
    def from_authority(
        cls,
        authority: ActiveQualificationAuthority,
        cells: tuple[LiveQualificationCellEvidence, ...],
        probes: tuple[LiveQualificationProbeEvidence, ...],
        *,
        terminal_evidence: QualificationTerminalEvidence | Mapping[str, Any] | None = None,
        ledger: Any = None,
    ) -> "LiveQualificationAggregate":
        return aggregate_live_qualification(
            authority,
            cells,
            probes,
            terminal_evidence=terminal_evidence,
            ledger=ledger,
        )

    mint = from_authority


def _normalized_live_cell(
    authority: ActiveQualificationAuthority,
    evidence: LiveQualificationCellEvidence,
    result: QualificationCellResult,
) -> NormalizedCell:
    return _normalized_live_cell_from_trace(authority, evidence, result.trace)


def _normalized_live_cell_from_trace(
    authority: ActiveQualificationAuthority,
    evidence: LiveQualificationCellEvidence,
    trace: QualificationTrace,
) -> NormalizedCell:
    rejection = evidence.rejection_evidence
    normalized_rejection = None
    if isinstance(rejection, RejectionEvidence):
        normalized_rejection = NormalizedRejectionBinding(
            arm=rejection.binding.arm,
            cell_id=rejection.binding.cell,
            profile_digest=rejection.binding.profile,
            campaign_id=rejection.binding.campaign,
            authority=authority.identity,
            activation=authority.activation_digest,
            evidence_origin=rejection.evidence_origin,
            reset_token=rejection.binding.reset_token,
            generation=rejection.binding.generation,
            request_identity=rejection.binding.request,
            permit_identity=rejection.binding.permit,
            effect_identity=rejection.binding.effect,
            evidence_digest=rejection.evidence_digest,
            current_inadmissible=rejection.current_inadmissible,
            native_entries=rejection.native_entries,
        )
    return NormalizedCell(
        cell_id=evidence.cell_id,
        authority=authority.identity,
        activation=authority.activation_digest,
        profile_digest=evidence.profile_digest,
        evidence_origin=evidence.evidence_origin,
        campaign_id=evidence.campaign_id,
        reset_identity=evidence.reset_identity,
        evidence_digest=evidence.evidence_digest,
        fresh_root=evidence.fresh_root,
        reset_passed=evidence.reset_passed,
        capability_state=evidence.capability_state,
        provider_terminal=evidence.provider_terminal,
        oracle_value=evidence.oracle_value,
        containment_clean=evidence.containment_clean,
        evidence_valid=evidence.evidence_valid,
        terminal_verified=evidence.terminal_verified,
        rejection_verified=evidence.rejection_verified,
        execution_provenance=evidence.execution_provenance,
        retry=evidence.retry,
        resumed=evidence.resumed,
        replacement=evidence.replacement,
        rejection_binding=normalized_rejection,
        reset_token=trace.reset_token,
        generation=trace.generation,
        request_identity=trace.request_identity,
        permit_identity=trace.permit_identity,
        effect_identity=trace.effect_identity,
    )


def _normalized_live_probe(
    authority: ActiveQualificationAuthority,
    evidence: LiveQualificationProbeEvidence,
) -> NormalizedProbe:
    return NormalizedProbe(
        probe=evidence.probe,
        authority=authority.identity,
        activation=authority.activation_digest,
        profile_digest=evidence.profile_digest,
        evidence_origin=evidence.evidence_origin,
        campaign_id=evidence.campaign_id,
        passed=evidence.passed,
        terminal_verified=evidence.terminal_verified,
        evidence_digest=evidence.evidence_digest,
        execution_provenance=evidence.execution_provenance,
    )


def _presentation_cell_from_parent(
    authority: ActiveQualificationAuthority,
    record: NormalizedCell,
) -> QualificationCellResult:
    """Project one attested parent record into a descriptive result."""

    binding = _active_qualification_binding(authority)
    rejection = record.rejection_binding
    trace = QualificationTrace(
        record.cell_id,
        {"parent_record_identity": record.identity},
        record.profile_digest,
        record.campaign_id,
        record.reset_identity,
        record.evidence_digest,
        _QUALIFICATION_TOKEN,
        binding,
        record.evidence_origin,
        record.reset_token,
        record.generation,
        record.request_identity,
        record.permit_identity,
        record.effect_identity,
        None if rejection is None else rejection.identity,
    )
    return QualificationCellResult(
        record.cell_id,
        normalized_cell_passes(record),
        record.fresh_root,
        record.retry,
        trace,
        record.resumed,
        record.replacement,
        _QUALIFICATION_TOKEN,
    )


def _presentation_probes_from_parent(
    authority: ActiveQualificationAuthority,
    records: tuple[NormalizedProbe, ...],
) -> ProbeAggregate:
    """Project the parent probe census into a descriptive aggregate."""

    if tuple(record.probe for record in records) != PROBES:
        raise ProvenanceError("final_prerequisite_mismatch")
    binding = authority.owner.qualification_probe_binding(authority)
    origins = {record.evidence_origin for record in records}
    profiles = {record.profile_digest for record in records}
    campaigns = {record.campaign_id for record in records}
    if len(origins) != 1 or len(profiles) != 1 or len(campaigns) != 1:
        raise ProvenanceError("final_prerequisite_mismatch")
    return ProbeAggregate(
        PROBES,
        all(record.passed is True and record.terminal_verified is True for record in records),
        profiles.pop(),
        campaigns.pop(),
        canonical_sha256({"probe_evidence": [record.evidence_digest for record in records]}),
        _QUALIFICATION_TOKEN,
        binding,
        origins.pop(),
    )


def issue_live_qualification_capabilities(
    authority: ActiveQualificationAuthority,
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """Issue the parent-owned one-shot coordinate set before terminal work."""

    return authority.owner._issue_qualification_coordinate_capabilities(authority)


def publish_live_qualification_terminals(
    authority: ActiveQualificationAuthority,
    cells: tuple[LiveQualificationCellEvidence, ...],
    probes: tuple[LiveQualificationProbeEvidence, ...],
    *,
    ledger: Any = None,
) -> QualificationSemanticAttestation:
    """Consume coordinate capabilities and finalize the parent semantic census."""

    selected_ledger = _authority_ledger(authority) if ledger is None else ledger
    if (selected_ledger is not authority.authority.ledger
            or not authority.owner.owns_ledger(selected_ledger)
            or getattr(selected_ledger, "namespace", None) != "qualification"
            or getattr(selected_ledger, "reservation_id", None) != authority.reservation_id
            or getattr(selected_ledger, "state", None) != "active"):
        raise ProvenanceError("authority_replay")
    if (type(cells) is not tuple
            or tuple(value.cell_id for value in cells) != qualification_ids()
            or type(probes) is not tuple
            or tuple(value.probe for value in probes) != PROBES):
        raise ValueError("exact qualification terminal domains are required")
    failure_reason: str | None = None

    def terminalize_failure(census: QualificationCensus, verdict: Any) -> None:
        nonlocal failure_reason
        failure_reason = (
            "qualification_probe_failed"
            if any(probe.passed is not True for probe in census.probes)
            else "qualification_cell_failed"
        )
        return _terminalize_failed_qualification(
            authority,
            selected_ledger,
            aggregate_digest=verdict.aggregate_digest,
            probe_digest=verdict.probe_digest,
            evidence_origin=(
                RUNTIME_VERIFIED_ORIGIN
                if authority.origin == RUNTIME_VERIFIED_ORIGIN
                else INJECTED_FAKE_EVIDENCE_ORIGIN
            ),
            reason=failure_reason,
        )

    def terminalize_success(
        census: QualificationCensus, verdict: Any, receipt_digest: str,
        terminal_receipts: tuple[Any, ...], validated_batch: tuple[Any, ...],
    ) -> None:
        try:
            authority.owner._ledger_terminal_qualification_batch(
                authority,
                selected_ledger,
                census,
                verdict,
                receipt_digest,
                terminal_receipts,
                _validated_batch=validated_batch,
            )
        except Exception as exc:
            raise ProvenanceError("authority_replay") from exc

    verdict = authority.owner._publish_qualification_terminal_batch(
        authority,
        tuple(
            (evidence.terminal_capability, evidence.execution_receipt)
            for evidence in (*cells, *probes)
        ),
        on_failure=terminalize_failure,
        on_success=terminalize_success,
    )
    if verdict.passed is not True:
        raise ValueError(
            "qualification probes failed"
            if failure_reason == "qualification_probe_failed"
            else "qualification cell evidence does not qualify"
        )
    return authority.owner.finalize_qualification_semantics(authority)


def aggregate_live_qualification(
    authority: ActiveQualificationAuthority,
    cells: tuple[LiveQualificationCellEvidence, ...],
    probes: tuple[LiveQualificationProbeEvidence, ...] | ProbeAggregate,
    *,
    terminal_evidence: QualificationTerminalEvidence | Mapping[str, Any] | None = None,
    ledger: Any = None,
) -> LiveQualificationAggregate:
    """Build a presentation aggregate from a parent-owned attestation.

    The factory is intentionally strict about tuple order and terminal ledger
    state. Caller observations and probe aggregates supply only presentation
    shape/binding checks; semantic fields are projected from the parent-attested
    census. This report does not authorize final execution. ``injected_fake``
    remains controller-owned and is never runtime-admissible.
    """

    binding = _active_qualification_binding(authority)
    if type(cells) is not tuple or any(
        not isinstance(value, LiveQualificationCellEvidence) for value in cells
    ) or tuple(value.cell_id for value in cells) != qualification_ids():
        raise ValueError("exact ordered fifteen-cell qualification evidence required")
    if any(value.profile_digest != authority.profile_digest for value in cells):
        raise ProvenanceError("profile_mismatch")
    if len({value.evidence_digest for value in cells}) != len(cells):
        raise ValueError("duplicate qualification evidence digest")
    if len({value.reset_identity for value in cells}) != len(cells):
        raise ValueError("duplicate qualification reset identity")
    origins = {value.evidence_origin for value in cells}
    expected_origin = _authority_evidence_origin(authority)
    if origins != {expected_origin}:
        raise ValueError("mixed qualification evidence origin")
    campaign_ids = {value.campaign_id for value in cells}
    if len(campaign_ids) != 1:
        raise ValueError("qualification campaign binding mismatch")
    probe_campaign_ids: set[str]
    if isinstance(probes, ProbeAggregate):
        probe_binding = authority.owner.qualification_probe_binding(authority)
        if (
            probes.probes != PROBES
            or probes.profile_digest != authority.profile_digest
            or probes.authority_binding != probe_binding
            or probes.evidence_origin != expected_origin
        ):
            raise ProvenanceError("final_prerequisite_mismatch")
        probe_campaign_ids = {probes.campaign_id}
    elif type(probes) is tuple and all(
        isinstance(value, LiveQualificationProbeEvidence) for value in probes
    ) and tuple(value.probe for value in probes) == PROBES:
        probe_campaign_ids = {value.campaign_id for value in probes}
    else:
        raise TypeError("parent terminal registry requires ordered P1-P4 observations")
    try:
        semantic_attestation = authority.owner.qualification_semantic_attestation(
            authority,
        )
    except ProvenanceError as exc:
        # Aggregation is a presentation layer only.  It may never be the
        # first path that turns caller-authored evidence into a terminal event.
        raise ProvenanceError("final_prerequisite_mismatch") from exc
    cell_campaign_id = semantic_attestation.cell_campaign_id
    if campaign_ids != {cell_campaign_id}:
        raise ProvenanceError("final_prerequisite_mismatch")
    if probe_campaign_ids != {semantic_attestation.probe_campaign_id}:
        raise ProvenanceError("final_prerequisite_mismatch")
    parent_census = authority.owner.qualification_semantic_census(authority)
    if (
        type(parent_census.cells) is not tuple
        or type(parent_census.probes) is not tuple
        or tuple(value.cell_id for value in parent_census.cells) != qualification_ids()
        or tuple(value.probe for value in parent_census.probes) != PROBES
    ):
        raise ProvenanceError("final_prerequisite_mismatch")
    results = tuple(
        _presentation_cell_from_parent(authority, value)
        for value in parent_census.cells
    )
    probe_aggregate = _presentation_probes_from_parent(
        authority, parent_census.probes,
    )
    if probe_aggregate.evidence_origin != expected_origin:
        raise ValueError("mixed qualification/probe evidence origin")
    if probe_aggregate.campaign_id == cell_campaign_id:
        raise ValueError("qualification and probe campaigns must be distinct")
    aggregate_origin = expected_origin
    probe_digest = probe_aggregate.identity
    _validate_live_aggregate_components(
        results,
        probe_aggregate,
        authority.profile_digest,
        cell_campaign_id,
        binding,
        authority,
        aggregate_origin,
        require_probe_passed=False,
    )
    # Compute the aggregate identity once, without a terminal object.  The
    # terminal event records this identity; including the event in the
    # aggregate would create an unresolvable digest cycle.
    aggregate_identity = canonical_sha256(
        {
            "artifact": "minecraft-k12-live-qualification-aggregate/3",
            "results": [result.identity for result in results],
            "probes": probe_digest,
            "profile": authority.profile_digest,
            "campaign": cell_campaign_id,
            "authority_binding": binding.canonical(),
            "execution_provenance": LIVE_QUALIFICATION_PROVENANCE,
            "evidence_origin": aggregate_origin,
        }
    )
    selected_ledger = _authority_ledger(authority) if ledger is None else ledger
    if probe_aggregate.passed is not True:
        _terminalize_failed_qualification(
            authority,
            selected_ledger,
            aggregate_digest=aggregate_identity,
            probe_digest=probe_digest,
            evidence_origin=aggregate_origin,
            reason="qualification_probe_failed",
        )
        raise ValueError("qualification probes failed")
    presentation_census = parent_census
    presentation_verdict = verify_qualification_projection(presentation_census)
    if (
        not presentation_verdict.passed
        or presentation_verdict.aggregate_digest
            != semantic_attestation.semantic_projection_digest
        or presentation_verdict.probe_digest
            != semantic_attestation.probe_terminal_digest
            or cell_campaign_id != semantic_attestation.cell_campaign_id
            or probe_aggregate.campaign_id != semantic_attestation.probe_campaign_id
    ):
        raise ProvenanceError("final_prerequisite_mismatch")
    aggregate = object.__new__(LiveQualificationAggregate)
    for name, value in (
        ("results", results),
        ("probes", probe_aggregate),
        ("profile_digest", authority.profile_digest),
        ("campaign_id", cell_campaign_id),
        ("authority_binding", binding),
        ("authority", authority),
        ("execution_provenance", LIVE_QUALIFICATION_PROVENANCE),
        ("evidence_origin", aggregate_origin),
        ("identity", aggregate_identity),
        ("_ownership_marker", object()),
    ):
        object.__setattr__(aggregate, name, value)
    try:
        if (selected_ledger is not authority.authority.ledger
                or getattr(selected_ledger, "state", None) != "terminal"
                or getattr(selected_ledger, "head_digest", None)
                    != semantic_attestation.terminal_ledger_digest):
            raise ProvenanceError("first_consume_mismatch")
        terminal = QualificationTerminalEvidence(
            ledger_digest=selected_ledger.head_digest,
            aggregate_digest=aggregate_identity,
            probe_digest=probe_digest,
            authority_binding=binding,
            result="passed",
            state="terminal",
            verified=True,
            evidence_origin=aggregate_origin,
        )
        if terminal_evidence is not None:
            supplied_terminal = _coerce_terminal(
                terminal_evidence,
                binding=binding,
                aggregate_digest=aggregate_identity,
                probe_digest=probe_digest,
                evidence_origin=aggregate_origin,
            )
            if supplied_terminal.ledger_digest != terminal.ledger_digest:
                raise ProvenanceError("first_consume_mismatch")
        object.__setattr__(aggregate, "terminal", terminal)
        object.__setattr__(aggregate, "semantic_attestation", semantic_attestation)
        aggregate.__post_init__()
        install_qualification_evidence_contract(
            aggregate,
            aggregate_identity=aggregate.identity,
            probe_aggregate_digest=aggregate.probes.identity,
            qualification_terminal_ledger_digest=aggregate.terminal.ledger_digest,
            profile_digest=aggregate.profile_digest,
            authority_binding=aggregate.authority_binding,
            authority_binding_canonical=aggregate.authority_binding.canonical(),
            evidence_origin=aggregate.evidence_origin,
            execution_provenance=aggregate.execution_provenance,
            passed=aggregate.qualifies(),
            terminal_evidence=aggregate.terminal,
            authority=aggregate.authority,
            controller=aggregate.controller,
            aggregate_marker=aggregate._ownership_marker,
            token=_CONTRACT_MINT_TOKEN,
        )
        if aggregate.identity != aggregate_identity or not aggregate.qualifies():
            raise ValueError("live qualification aggregate failed terminal verification")
    except Exception:
        raise
    with _LIVE_AGGREGATE_LOCK:
        if authority.identity in _LIVE_AGGREGATES:
            raise ProvenanceError("authority_replay")
        _LIVE_AGGREGATES.add(authority.identity)
    return aggregate


build_live_qualification_aggregate = aggregate_live_qualification
qualify_live_campaign = aggregate_live_qualification
live_qualification_aggregate = aggregate_live_qualification
build_live_qualification = aggregate_live_qualification
LiveQualification = LiveQualificationAggregate
QualificationProbeAggregate = ProbeAggregate


def load_k12_live_qualification(path: str | Path = QUALIFICATION_PATH) -> dict[str, Any]:
    value = strict_json_load(Path(path), "K12 live qualification")
    if not isinstance(value, dict) or value.get("detached_artifact_sha256") != detached_digest(value):
        raise ValueError("qualification detached digest mismatch")
    if value.get("schedule") != list(qualification_ids()) or value.get("cell_count") != 15:
        raise ValueError("qualification schedule mismatch")
    return value


def final_gate_accepts(artifact: Any) -> bool:
    """Generic artifacts are never accepted by the final gate."""

    return False


__all__ = [
    "PROBES",
    "STRATA",
    "QUALIFICATION_PHASE",
    "RUNTIME_VERIFIED_ORIGIN",
    "INJECTED_FAKE_ORIGIN",
    "INJECTED_TEST_ORIGIN",
    "INJECTED_FAKE_EVIDENCE_ORIGIN",
    "LIVE_EVIDENCE_ORIGIN",
    "TEST_EVIDENCE_ORIGIN",
    "MockCellQualificationEvidence",
    "MockProbeEvidence",
    "LiveQualificationCellEvidence",
    "LiveCellQualificationEvidence",
    "LiveQualificationCellObservation",
    "LiveCellEvidence",
    "LiveQualificationProbeEvidence",
    "LiveProbeEvidence",
    "LiveQualificationProbeObservation",
    "LiveProbeObservation",
    "QualificationTerminalEvidence",
    "LiveQualificationTerminalEvidence",
    "LiveTerminalEvidence",
    "QualificationTrace",
    "QualificationCellResult",
    "QualificationAggregate",
    "QualificationAggregateOwnershipReceipt",
    "LiveQualificationAggregate",
    "LiveQualification",
    "ProbeAggregate",
    "QualificationProbeAggregate",
    "qualification_ids",
    "qualification_cells",
    "qualification_artifact",
    "qualification_matrix",
    "qualification_schedule_digest",
    "qualify_mock_cell",
    "qualify_mock_campaign",
    "qualify_mock_probes",
    "qualify_live_cell",
    "qualify_live_probes",
    "issue_live_qualification_capabilities",
    "publish_live_qualification_terminals",
    "aggregate_live_qualification",
    "build_live_qualification_aggregate",
    "qualify_live_campaign",
    "live_qualification_aggregate",
    "build_live_qualification",
    "load_k12_live_qualification",
    "final_gate_accepts",
]
