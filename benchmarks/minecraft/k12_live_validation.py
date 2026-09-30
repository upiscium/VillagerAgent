"""Strict K12 live validation and parent-owned final admission.

The final path is deliberately separate from the fifteen-cell qualification.
``FinalCampaignAdmission`` is a typed, one-shot capability derived from an
active ``ActiveFinalAuthority``.  It authenticates the exact ninety-cell
schedule and the final-phase manifest, while ``FinalCellAuthority`` gives the
runner one non-replayable capability for each scheduled cell.

The final cell order is launch, post-dispatch completion, then containment;
completed cells retain only a containment-validation receipt and cannot be
reused as launch authority.

All observations are injected values.  This module has no subprocess,
network, RCON, Minecraft, provider, systemd, or filesystem execution path.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
import re
from threading import RLock
from pathlib import Path
from typing import Any, ClassVar
import weakref

from benchmarks.common.eac.canonical import canonical_sha256

from .k12_execution_provenance import (
    ActiveFinalAuthority,
    AuthorityBinding,
    FINAL_AUTHORITY,
    FinalExecutionAuthority,
    INJECTED_FAKE_ORIGIN,
    INJECTED_TEST_ORIGIN,
    LIVE_FINAL_NAMESPACE,
    PROFILE_V2,
    ProvenanceError,
    QualificationSemanticAttestation,
    RUNTIME_VERIFIED_ORIGIN,
    authority_owns_profile,
)
from .k12_live_containment import LiveObservation, validate_final_observation
from .k12_qualification_semantics import PROBES as QUALIFICATION_PROBES
from .k12_protocol import build_k12_cells
from .k12_runtime_profile import detached_digest, strict_json_load


# Deliberately exhaustive: a caller cannot continue by inventing a new/default
# state after an A/R/S or containment stop.
LIVE_CONTAINMENT_PROBE_IDENTITY = "minecraft-k12-live-containment-probe/1"
CONTAINMENT_PROBES = QUALIFICATION_PROBES
LIVE_FINAL_WRAPPER_IDENTITY = "minecraft-eac-k12-live-controlled-recovery/2"
STOP_POLICY_IDENTITY = "minecraft-k12-live-stop-policy/1"

FINAL_PHASE = "final"
QUALIFICATION_PHASE = "qualification"
FINAL_CELL_COUNT = 90
FINAL_SCHEDULE_IDENTITY = "minecraft-k12-live-final-schedule/1"
FINAL_FIXTURE_MANIFEST_IDENTITY = "minecraft-k12-recovery-fixture-manifest/1"
FINAL_RANDOMIZATION_MANIFEST_IDENTITY = "minecraft-k12-recovery-randomization-manifest/1"
FINAL_MANIFEST_IDENTITY = FINAL_RANDOMIZATION_MANIFEST_IDENTITY
# These detached roots are sealed repository inputs.  A caller may construct
# an injected-test manifest with synthetic digests, but a ``runtime_verified``
# manifest must be the exact loaded pair rather than a look-alike schedule.
FINAL_FIXTURE_MANIFEST_DIGEST = "a321af43ed18c6ef3dae4e5d1f64d8cffdd36c4472f6b9510bc60e5c0a588d1f"
FINAL_RANDOMIZATION_MANIFEST_DIGEST = "116e568b5c5f87ac957bf8ceea25ce9443c2370012a6ec2f5f180e10602c3693"
FINAL_PHASE_MANIFEST_IDENTITY = "minecraft-k12-live-final-phase-manifest/1"
_QUALIFICATION_MANIFEST_IDENTITIES = frozenset(
    {
        "minecraft-eac-k12-live-runtime-qualification/1",
        "minecraft-eac-k12-live-runtime-qualification/2",
        "minecraft-k12-live-runtime-qualification-schedule/1",
    }
)
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_CANONICAL_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FINAL_ADMISSION_TOKEN = object()
_FINAL_CELL_TOKEN = object()
_FINAL_ADMISSION_LOCK = RLock()
_FINAL_ADMISSION_KEYS: set[str] = set()
_FINAL_ADMISSIONS: weakref.WeakValueDictionary[
    str, "FinalCampaignAdmission"
] = weakref.WeakValueDictionary()
_FINAL_ADMISSION_SNAPSHOTS: dict[str, tuple[str, str]] = {}
_FINAL_ADMISSION_STATE_DIGESTS: dict[str, str] = {}
_FINAL_EVIDENCE_ORIGINS = frozenset(
    {"test_only", RUNTIME_VERIFIED_ORIGIN, INJECTED_FAKE_ORIGIN}
)
INJECTED_FAKE_EVIDENCE_ORIGIN = INJECTED_FAKE_ORIGIN
LIVE_EVIDENCE_ORIGIN = RUNTIME_VERIFIED_ORIGIN


def _raw_digest(value: Any) -> bool:
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def _digest(value: Any) -> bool:
    return _raw_digest(value) or (
        isinstance(value, str) and _CANONICAL_DIGEST.fullmatch(value) is not None
    )


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is required")
    return value


def _authority_evidence_origin(authority: ActiveFinalAuthority) -> str:
    """Map an active final authority origin to its evidence origin."""

    if not isinstance(authority, ActiveFinalAuthority):
        raise TypeError("active parent final authority required")
    origin = authority.origin
    if origin == RUNTIME_VERIFIED_ORIGIN:
        if not authority.runtime_admissible:
            raise ProvenanceError("authority_origin_mismatch")
        return RUNTIME_VERIFIED_ORIGIN
    if origin == INJECTED_FAKE_ORIGIN:
        owner = authority.owner
        if (
            authority.runtime_admissible
            or getattr(owner, "origin", None) != INJECTED_TEST_ORIGIN
            or not getattr(owner, "is_injected_test_controller", False)
        ):
            raise ProvenanceError("authority_origin_mismatch")
        return INJECTED_FAKE_ORIGIN
    raise ProvenanceError("authority_origin_mismatch")


def final_cell_ids() -> tuple[str, ...]:
    """Return the frozen ninety-cell final schedule in execution order."""

    result = tuple(cell.cell_id for cell in build_k12_cells())
    if len(result) != FINAL_CELL_COUNT or len(set(result)) != FINAL_CELL_COUNT:
        raise ValueError("final schedule is not exactly ninety unique cells")
    return result


FINAL_SCHEDULE = final_cell_ids()
FINAL_SCHEDULE_DIGEST = canonical_sha256(list(FINAL_SCHEDULE))


def final_schedule() -> tuple[str, ...]:
    return FINAL_SCHEDULE


def _stop(
    cell: str,
    capability: str,
    campaign_stop: bool,
    quarantine: bool,
    consequence: str,
) -> dict[str, Any]:
    return {
        "cell_disposition": cell,
        "capability": capability,
        "campaign_stop": campaign_stop,
        "target_quarantine": quarantine,
        "result_consequence": consequence,
    }


STOP_POLICY_CONSEQUENCES = {
    "expected_stale_rejection": _stop(
        "STOPPED_AFTER_REJECTION", "REVOKED", False, False, "valid_cell"
    ),
    "known_objective_true": _stop("scientific_success", "REVOKED", False, False, "valid_cell"),
    "known_objective_false": _stop(
        "scientific_non_success", "REVOKED", False, False, "qualification_fails"
    ),
    "proposal_invalid": _stop(
        "scientific_terminal", "REVOKED", False, False, "qualification_fails"
    ),
    "reset_or_readback_failure": _stop(
        "infrastructure_terminal", "POISONED", True, True, "disqualifier"
    ),
    "provider_admission_failure": _stop(
        "infrastructure_terminal", "REVOKED", True, False, "disqualifier"
    ),
    "provider_termination_unknown": _stop(
        "infrastructure_terminal", "POISONED", True, True, "disqualifier"
    ),
    "authority_mismatch": _stop("integrity_terminal", "POISONED", True, True, "disqualifier"),
    "worker_active_operation": _stop(
        "infrastructure_terminal", "POISONED", True, True, "disqualifier"
    ),
    "native_effect_unknown": _stop(
        "infrastructure_terminal", "POISONED", True, True, "disqualifier"
    ),
    "oracle_unknown": _stop("infrastructure_terminal", "POISONED", True, True, "disqualifier"),
    "evidence_tamper": _stop("integrity_terminal", "POISONED", True, True, "disqualifier"),
    "containment_failure": _stop(
        "infrastructure_terminal", "POISONED", True, True, "disqualifier"
    ),
    "target_lock_loss": _stop("infrastructure_terminal", "POISONED", True, True, "disqualifier"),
    "identity_mismatch": _stop(
        "prelaunch_infrastructure_stop", "REVOKED", True, False, "disqualifier"
    ),
}


def validate_live_containment(
    observation: LiveObservation, expected_cgroup: str, *, cell_authority: Any = None
) -> tuple[bool, tuple[str, ...]]:
    if not isinstance(observation, LiveObservation) or not validate_final_observation(
        observation, cell_authority,
    ):
        return False, ("untyped_observation",)
    reasons: tuple[str, ...] = ()
    if observation.main_pid != 0 or observation.main_start != 0:
        reasons += ("unit_pid_not_cleared",)
    if observation.active_state != "inactive":
        reasons += ("unit_not_inactive",)
    if observation.cgroup_procs:
        reasons += ("cgroup_procs_not_empty",)
    if observation.events_populated != 0:
        reasons += ("events_populated",)
    if observation.control_group != expected_cgroup:
        reasons += ("cgroup_changed",)
    if observation.descendants:
        reasons += ("descendants_present",)
    return not reasons, reasons


def validate_containment_probe_artifact(
    artifact: Mapping[str, Any],
) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    if not isinstance(artifact, Mapping):
        return False, ("untyped_containment_probe",)
    if artifact.get("identity") != LIVE_CONTAINMENT_PROBE_IDENTITY:
        reasons.append("containment_probe_identity_mismatch")
    if artifact.get("probes") != list(CONTAINMENT_PROBES):
        reasons.append("containment_probe_set_mismatch")
    if artifact.get("passed") is not True:
        reasons.append("containment_probe_failed")
    if artifact.get("scope") not in (None, "campaign"):
        reasons.append("containment_probe_scope_mismatch")
    if "execution_provenance" in artifact and artifact.get("execution_provenance") != "qualification_probe":
        reasons.append("containment_probe_namespace_mismatch")
    if "evidence_origin" in artifact and artifact.get("evidence_origin") not in {
        "test_only",
        RUNTIME_VERIFIED_ORIGIN,
        INJECTED_FAKE_ORIGIN,
    }:
        reasons.append("containment_probe_origin_mismatch")
    return not reasons, tuple(reasons)


def load_detached_artifact(path: str | Path) -> dict[str, Any]:
    value = strict_json_load(Path(path), "K12 live detached artifact")
    if not isinstance(value, dict) or value.get("detached_artifact_sha256") != detached_digest(value):
        raise ValueError("detached artifact digest mismatch")
    return value


def load_k12_live_stop_policy(path: str | Path | None = None) -> dict[str, Any]:
    path = path or Path(__file__).with_name("k12_live_stop_policy_v1.json")
    value = load_detached_artifact(path)
    if (
        value.get("artifact_id") != "minecraft-k12-live-stop-policy"
        or value.get("identity") != STOP_POLICY_IDENTITY
        or value.get("retry") != "forbidden"
        or value.get("resume") != "forbidden"
        or value.get("replacement") != "forbidden"
        or value.get("consequences") != STOP_POLICY_CONSEQUENCES
    ):
        raise ValueError("stop policy is not exhaustive")
    return value


def load_k12_live_containment_probe(path: str | Path | None = None) -> dict[str, Any]:
    path = path or Path(__file__).parents[2] / "configs/minecraft/k12-live-containment-probe-v1.json"
    value = load_detached_artifact(path)
    if (
        value.get("identity") != LIVE_CONTAINMENT_PROBE_IDENTITY
        or value.get("probes") != list(CONTAINMENT_PROBES)
        or value.get("scope") != "campaign"
        or value.get("executor") != "injected_only"
        or value.get("network") is not False
        or value.get("rcon") is not False
    ):
        raise ValueError("containment probe manifest mismatch")
    return value


def _final_manifest_identity_payload(manifest: Any) -> dict[str, Any]:
    """Return every field that authenticates a final campaign manifest."""
    return {
        "artifact": FINAL_PHASE_MANIFEST_IDENTITY,
        "phase": manifest.phase,
        "manifest_identity": manifest.manifest_identity,
        "manifest_digest": manifest.manifest_digest,
        "schedule": list(manifest.schedule),
        "common_closure_digest": manifest.common_closure_digest,
        "fixture_manifest_digest": manifest.fixture_manifest_digest,
        "randomization_manifest_digest": manifest.randomization_manifest_digest,
        "evidence_origin": manifest.evidence_origin,
    }


def _final_manifest_closure_digest(manifest: Any) -> str:
    return canonical_sha256(
        {
            "phase": manifest.phase,
            "manifest_identity": manifest.manifest_identity,
            "manifest_digest": manifest.manifest_digest,
            "schedule_digest": FINAL_SCHEDULE_DIGEST,
            "fixture_manifest_digest": manifest.fixture_manifest_digest,
            "randomization_manifest_digest": manifest.randomization_manifest_digest,
        }
    )


@dataclass(frozen=True, slots=True)
class FinalCampaignManifest:
    """Authenticated final-phase manifest identity and exact schedule."""

    phase: str
    manifest_identity: str
    manifest_digest: str
    schedule: tuple[str, ...]
    common_closure_digest: str = ""
    fixture_manifest_digest: str = ""
    randomization_manifest_digest: str = ""
    evidence_origin: str = INJECTED_FAKE_EVIDENCE_ORIGIN
    identity: str = field(init=False)

    def __post_init__(self) -> None:
        schedule = tuple(self.schedule)
        object.__setattr__(self, "schedule", schedule)
        if (
            self.phase != FINAL_PHASE
            or not isinstance(self.manifest_identity, str)
            or not self.manifest_identity
            or self.manifest_identity in _QUALIFICATION_MANIFEST_IDENTITIES
            or not _digest(self.manifest_digest)
            or schedule != FINAL_SCHEDULE
            or self.evidence_origin not in _FINAL_EVIDENCE_ORIGINS
            or "qualification" in self.manifest_identity.lower()
            or (self.fixture_manifest_digest and not _raw_digest(self.fixture_manifest_digest))
            or (
                self.randomization_manifest_digest
                and not _raw_digest(self.randomization_manifest_digest)
            )
        ):
            raise ValueError("final phase manifest is not exact or is qualification-scoped")
        if self.evidence_origin == RUNTIME_VERIFIED_ORIGIN and (
            self.manifest_identity != FINAL_RANDOMIZATION_MANIFEST_IDENTITY
            or self.manifest_digest != FINAL_RANDOMIZATION_MANIFEST_DIGEST
            or self.fixture_manifest_digest != FINAL_FIXTURE_MANIFEST_DIGEST
            or self.randomization_manifest_digest != FINAL_RANDOMIZATION_MANIFEST_DIGEST
        ):
            raise ValueError("live final manifest is not the sealed repository manifest pair")
        closure = self.common_closure_digest
        expected_closure = _final_manifest_closure_digest(self)
        if not closure:
            closure = expected_closure
            object.__setattr__(self, "common_closure_digest", closure)
        elif not _digest(closure) or closure != expected_closure:
            raise ValueError("final manifest closure digest is invalid")
        object.__setattr__(
            self,
            "identity",
            canonical_sha256(_final_manifest_identity_payload(self)),
        )

    def validate_integrity(self) -> bool:
        """Verify that the current object still matches its sealed identity."""
        try:
            if type(self.schedule) is not tuple:
                return False
            schedule = self.schedule
            if (
                self.phase != FINAL_PHASE
                or not isinstance(self.manifest_identity, str)
                or not self.manifest_identity
                or self.manifest_identity in _QUALIFICATION_MANIFEST_IDENTITIES
                or not _digest(self.manifest_digest)
                or schedule != FINAL_SCHEDULE
                or not _digest(self.common_closure_digest)
                or self.common_closure_digest != _final_manifest_closure_digest(self)
                or self.evidence_origin not in _FINAL_EVIDENCE_ORIGINS
                or "qualification" in self.manifest_identity.lower()
                or (self.fixture_manifest_digest and not _raw_digest(self.fixture_manifest_digest))
                or (
                    self.randomization_manifest_digest
                    and not _raw_digest(self.randomization_manifest_digest)
                )
            ):
                return False
            if self.evidence_origin == RUNTIME_VERIFIED_ORIGIN and (
                self.manifest_identity != FINAL_RANDOMIZATION_MANIFEST_IDENTITY
                or self.manifest_digest != FINAL_RANDOMIZATION_MANIFEST_DIGEST
                or self.fixture_manifest_digest != FINAL_FIXTURE_MANIFEST_DIGEST
                or self.randomization_manifest_digest != FINAL_RANDOMIZATION_MANIFEST_DIGEST
            ):
                return False
            return self.identity == canonical_sha256(
                _final_manifest_identity_payload(self)
            )
        except (AttributeError, TypeError, ValueError):
            return False

    @property
    def schedule_identity(self) -> str:
        return FINAL_SCHEDULE_IDENTITY

    @property
    def cell_count(self) -> int:
        return FINAL_CELL_COUNT

    @property
    def cell_ids(self) -> tuple[str, ...]:
        return self.schedule

    @property
    def runtime_admissible(self) -> bool:
        return self.evidence_origin == RUNTIME_VERIFIED_ORIGIN

    @classmethod
    def from_runtime_manifests(
        cls, *, evidence_origin: str = RUNTIME_VERIFIED_ORIGIN
    ) -> "FinalCampaignManifest":
        # This is an explicit loader boundary; importing this module never
        # reads the manifests or performs external I/O.
        from .k12_protocol import load_fixture_manifest, load_randomization_manifest

        fixture = load_fixture_manifest()
        randomization = load_randomization_manifest()
        schedule = tuple(randomization["ordered_schedule"])
        if schedule != FINAL_SCHEDULE:
            raise ValueError("final randomization manifest schedule mismatch")
        return cls(
            FINAL_PHASE,
            randomization["schema_version"],
            randomization["detached_artifact_sha256"],
            schedule,
            fixture_manifest_digest=fixture["detached_artifact_sha256"],
            randomization_manifest_digest=randomization["detached_artifact_sha256"],
            evidence_origin=evidence_origin,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FinalCampaignManifest":
        if not isinstance(value, Mapping):
            raise TypeError("typed final campaign manifest required")
        schedule = value.get("schedule", value.get("ordered_schedule", ()))
        return cls(
            value.get("phase", FINAL_PHASE),
            value.get("manifest_identity", value.get("schema_version", "")),
            value.get("manifest_digest", value.get("detached_artifact_sha256", "")),
            tuple(schedule),
            value.get("common_closure_digest", value.get("closure_digest", "")),
            value.get("fixture_manifest_digest", ""),
            value.get("randomization_manifest_digest", ""),
            value.get("evidence_origin", INJECTED_FAKE_EVIDENCE_ORIGIN),
        )


FinalPhaseManifest = FinalCampaignManifest
FinalManifestObservation = FinalCampaignManifest


def load_final_campaign_manifest(
    *, evidence_origin: str = RUNTIME_VERIFIED_ORIGIN
) -> FinalCampaignManifest:
    return FinalCampaignManifest.from_runtime_manifests(evidence_origin=evidence_origin)


def make_final_campaign_manifest(**values: Any) -> FinalCampaignManifest:
    return FinalCampaignManifest(**values)


final_manifest = load_final_campaign_manifest


def final_common_closure(
    authority: ActiveFinalAuthority, manifest: FinalCampaignManifest
) -> str:
    """Return the closure all final cells must repeat byte-for-byte."""

    _validate_active_final_authority(authority)
    expected_origin = _authority_evidence_origin(authority)
    if not isinstance(manifest, FinalCampaignManifest) or manifest.evidence_origin != expected_origin:
        raise ProvenanceError("final_prerequisite_mismatch")
    body = authority.authority.receipt()
    body.pop("detached_artifact_sha256", None)
    required = (
        "qualification_authority_digest",
        "qualification_semantic_projection_digest",
        "qualification_probe_projection_digest",
        "qualification_terminal_receipt_census_digest",
        "qualification_terminal_event_digest",
        "qualification_terminal_ledger_digest",
        "source_aggregate",
        "profile_digest",
        "contract_set_digest",
        "capsule_digest",
    )
    if any(name not in body for name in required) or "checkout" not in body:
        raise ProvenanceError("final_prerequisite_mismatch")
    checkout = body["checkout"]
    if not isinstance(checkout, Mapping) or "head_commit" not in checkout or "head_tree" not in checkout:
        raise ProvenanceError("final_prerequisite_mismatch")
    return canonical_sha256(
        {
            "authority": authority.identity,
            "authority_binding": authority.binding.canonical(),
            "qualification_authority_digest": body["qualification_authority_digest"],
            "qualification_semantic_projection_digest":
                body["qualification_semantic_projection_digest"],
            "qualification_probe_projection_digest":
                body["qualification_probe_projection_digest"],
            "qualification_terminal_receipt_census_digest":
                body["qualification_terminal_receipt_census_digest"],
            "qualification_terminal_event_digest":
                body["qualification_terminal_event_digest"],
            "qualification_terminal_ledger_digest": body["qualification_terminal_ledger_digest"],
            "checkout": dict(checkout),
            "source_aggregate": body["source_aggregate"],
            "profile_digest": body["profile_digest"],
            "contract_set_digest": body["contract_set_digest"],
            "capsule_digest": body["capsule_digest"],
            "manifest": {
                "identity": manifest.manifest_identity,
                "digest": manifest.manifest_digest,
                "schedule_digest": FINAL_SCHEDULE_DIGEST,
                "closure": manifest.common_closure_digest,
            },
        }
    )


def _final_cell_evidence_identity_payload(value: Any) -> dict[str, Any]:
    return {
        "artifact": "minecraft-k12-live-final-cell-evidence/1",
        "cell_id": value.cell_id,
        "profile_digest": value.profile_digest,
        "campaign_id": value.campaign_id,
        "phase": value.phase,
        "manifest_identity": value.manifest_identity,
        "manifest_digest": value.manifest_digest,
        "common_closure_digest": value.common_closure_digest,
        "evidence_digest": value.evidence_digest,
        "authority_binding": (
            value.authority_binding.canonical() if value.authority_binding else None
        ),
        "terminal_verified": value.terminal_verified,
        "result": value.result,
        "execution_provenance": value.execution_provenance,
        "evidence_origin": value.evidence_origin,
        "fresh_root": value.fresh_root,
        "retry": value.retry,
        "resumed": value.resumed,
        "replacement": value.replacement,
    }


@dataclass(frozen=True, slots=True)
class FinalCellEvidence:
    """One injected, terminal final-cell observation."""

    cell_id: str
    profile_digest: str
    campaign_id: str
    phase: str
    manifest_identity: str
    manifest_digest: str
    common_closure_digest: str
    evidence_digest: str
    authority_binding: AuthorityBinding | None = None
    terminal_verified: bool = False
    result: str = "passed"
    execution_provenance: str = "live_final"
    evidence_origin: str = INJECTED_FAKE_EVIDENCE_ORIGIN
    fresh_root: bool = False
    retry: bool = False
    resumed: bool = False
    replacement: bool = False
    identity: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.cell_id, str)
            or not self.cell_id
            or not isinstance(self.profile_digest, str)
            or not self.profile_digest
            or not isinstance(self.campaign_id, str)
            or not self.campaign_id
            or self.phase != FINAL_PHASE
            or not isinstance(self.manifest_identity, str)
            or not self.manifest_identity
            or not _digest(self.manifest_digest)
            or not _digest(self.common_closure_digest)
            or not isinstance(self.evidence_digest, str)
            or not self.evidence_digest
            or self.terminal_verified is not True
            or self.result != "passed"
            or self.execution_provenance != "live_final"
            or self.evidence_origin not in _FINAL_EVIDENCE_ORIGINS
            or self.fresh_root is not True
        ):
            raise ValueError("complete final cell evidence is required")
        object.__setattr__(
            self,
            "identity",
            canonical_sha256(_final_cell_evidence_identity_payload(self)),
        )

    def validate_integrity(self) -> bool:
        """Verify that mutable object fields still match the evidence identity."""
        try:
            if (
                not isinstance(self.cell_id, str)
                or not self.cell_id
                or not isinstance(self.profile_digest, str)
                or not self.profile_digest
                or not isinstance(self.campaign_id, str)
                or not self.campaign_id
                or self.phase != FINAL_PHASE
                or not isinstance(self.manifest_identity, str)
                or not self.manifest_identity
                or not _digest(self.manifest_digest)
                or not _digest(self.common_closure_digest)
                or not isinstance(self.evidence_digest, str)
                or not self.evidence_digest
                or (self.authority_binding is not None
                    and not isinstance(self.authority_binding, AuthorityBinding))
                or self.terminal_verified is not True
                or self.result != "passed"
                or self.execution_provenance != "live_final"
                or self.evidence_origin not in _FINAL_EVIDENCE_ORIGINS
                or self.fresh_root is not True
            ):
                return False
            return self.identity == canonical_sha256(
                _final_cell_evidence_identity_payload(self)
            )
        except (AttributeError, TypeError, ValueError):
            return False

    @property
    def closure_digest(self) -> str:
        return self.common_closure_digest

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FinalCellEvidence":
        if not isinstance(value, Mapping):
            raise TypeError("typed final cell evidence required")
        return cls(
            value.get("cell_id", ""),
            value.get("profile_digest", ""),
            value.get("campaign_id", ""),
            value.get("phase", FINAL_PHASE),
            value.get("manifest_identity", ""),
            value.get("manifest_digest", ""),
            value.get("common_closure_digest", value.get("closure_digest", "")),
            value.get("evidence_digest", ""),
            value.get("authority_binding"),
            value.get("terminal_verified", False),
            value.get("result", "passed"),
            value.get("execution_provenance", "live_final"),
            value.get("evidence_origin", INJECTED_FAKE_EVIDENCE_ORIGIN),
            value.get("fresh_root", False),
            value.get("retry", False),
            value.get("resumed", False),
            value.get("replacement", False),
        )


FinalCellObservation = FinalCellEvidence


def make_final_cell_evidence(**values: Any) -> FinalCellEvidence:
    return FinalCellEvidence(**values)


def _validate_active_final_authority(authority: ActiveFinalAuthority) -> None:
    if not isinstance(authority, ActiveFinalAuthority):
        raise TypeError("active parent final authority required")
    try:
        owner = authority.owner
        owns_active = getattr(owner, "owns_active_authority", None)
        owned = callable(owns_active) and owns_active(authority) is True
    except (AttributeError, TypeError, ValueError, ProvenanceError):
        owned = False
    if not owned:
        raise ProvenanceError("authority_replay")
    if not authority.current_at():
        raise ProvenanceError("authority_replay")
    expected_origin = _authority_evidence_origin(authority)
    if authority.lifecycle != "active" or authority.binding.lifecycle != "active":
        raise ProvenanceError("authority_replay")
    if (
        authority.binding.authority_type != FINAL_AUTHORITY
        or authority.binding.provenance != LIVE_FINAL_NAMESPACE
        or authority.binding.namespace != LIVE_FINAL_NAMESPACE
        or authority.binding.origin != authority.origin
    ):
        raise ProvenanceError("authority_namespace_mismatch")
    if authority.body.get("evidence_origin") != authority.origin:
        raise ProvenanceError("authority_origin_mismatch")
    if authority.body.get("profile_digest") != authority.profile_digest or not _raw_digest(
        authority.profile_digest
    ):
        raise ProvenanceError("profile_mismatch")
    if (
        not authority.owns(authority.binding)
        or authority.binding.evidence_origin != expected_origin
        or not authority_owns_profile(
            authority,
            authority.binding,
            profile_id=PROFILE_V2,
            profile_digest=authority.profile_digest,
        )
    ):
        raise ProvenanceError("authority_replay")


def _authority_terminalization_guard(authority: ActiveFinalAuthority):
    owner = getattr(authority, "owner", None)
    guard = getattr(owner, "_terminalization_guard", None)
    return guard() if callable(guard) else nullcontext()


def _authority_semantic_guard(authority: ActiveFinalAuthority):
    owner = getattr(authority, "owner", None)
    guard = getattr(owner, "_semantic_guard", None)
    return guard() if callable(guard) else nullcontext()


def _final_admission_payload(admission: Any) -> dict[str, Any]:
    """Return the parent-side immutable identity inputs for an admission."""
    manifest = admission.manifest
    return {
        "artifact": "minecraft-k12-live-final-campaign-admission/1",
        "authority": admission.authority.identity,
        "authority_binding": admission.authority_binding.canonical(),
        "campaign_id": admission.campaign_id,
        "manifest": {
            "identity": manifest.identity,
            "phase": manifest.phase,
            "manifest_identity": manifest.manifest_identity,
            "manifest_digest": manifest.manifest_digest,
            "schedule": list(manifest.schedule),
            "common_closure_digest": manifest.common_closure_digest,
            "fixture_manifest_digest": manifest.fixture_manifest_digest,
            "randomization_manifest_digest": manifest.randomization_manifest_digest,
            "evidence_origin": manifest.evidence_origin,
        },
        "schedule": list(admission.schedule),
        "common_closure_digest": admission.common_closure_digest,
        "profile_digest": admission.profile_digest,
        "qualification_semantic_projection_digest":
            admission.qualification_semantic_projection_digest,
        "qualification_probe_projection_digest":
            admission.qualification_probe_projection_digest,
        "qualification_terminal_receipt_census_digest":
            admission.qualification_terminal_receipt_census_digest,
        "qualification_terminal_event_digest":
            admission.qualification_terminal_event_digest,
        "qualification_terminal_ledger_digest":
            admission.qualification_terminal_ledger_digest,
        "evidence_origin": admission.evidence_origin,
    }


@dataclass(slots=True)
class _FinalAdmissionState:
    observations: dict[str, FinalCellEvidence] = field(default_factory=dict)
    issued: set[str] = field(default_factory=set)
    cell_authorities: dict[str, "FinalCellAuthority"] = field(default_factory=dict)
    launched: set[str] = field(default_factory=set)
    consumed: set[str] = field(default_factory=set)
    launch_permits: dict[str, "FinalLaunchPermit"] = field(default_factory=dict)
    lock: RLock = field(default_factory=RLock)


def _final_admission_state_digest(admission: Any) -> str:
    """Digest mutable transition state separately from static admission data."""
    state = admission._state
    with state.lock:
        observations = []
        for cell_id, value in state.observations.items():
            admission._validate_cell_observation(value, cell_id)
            observations.append([cell_id, value.identity])
        return canonical_sha256(
            {
                "artifact": "minecraft-k12-live-final-admission-state/1",
                "observations": observations,
                "issued": sorted(state.issued),
                "cell_authorities": [
                    [cell_id, getattr(value, "identity", None)]
                    for cell_id, value in state.cell_authorities.items()
                ],
                "launched": sorted(state.launched),
                "consumed": sorted(state.consumed),
                "launch_permits": [
                    [cell_id, getattr(value, "identity", None)]
                    for cell_id, value in state.launch_permits.items()
                ],
            }
        )


def _record_final_admission_state(admission: Any) -> None:
    """Record a parent-side transition digest after a legitimate mutation."""
    digest = _final_admission_state_digest(admission)
    authority_identity = admission.authority.identity
    with _FINAL_ADMISSION_LOCK:
        if _FINAL_ADMISSIONS.get(authority_identity) is admission:
            _FINAL_ADMISSION_STATE_DIGESTS[authority_identity] = digest


@dataclass(frozen=True, init=False)
class FinalCampaignAdmission:
    """One final campaign admission owned by one active final authority."""

    # ``WeakValueDictionary`` needs ``__weakref__``; declaring the slots
    # manually keeps the admission weak-referenceable without reintroducing a
    # writable ``__dict__`` on this frozen capability.
    __slots__ = (
        "authority", "authority_binding", "campaign_id", "manifest", "schedule",
        "common_closure_digest", "profile_digest",
        "qualification_semantic_projection_digest",
        "qualification_probe_projection_digest",
        "qualification_terminal_receipt_census_digest",
        "qualification_terminal_event_digest", "qualification_terminal_ledger_digest",
        "evidence_origin", "identity", "_state", "__weakref__",
    )

    authority: ActiveFinalAuthority
    authority_binding: AuthorityBinding
    campaign_id: str
    manifest: FinalCampaignManifest
    schedule: tuple[str, ...]
    common_closure_digest: str
    profile_digest: str
    qualification_semantic_projection_digest: str
    qualification_probe_projection_digest: str
    qualification_terminal_receipt_census_digest: str
    qualification_terminal_event_digest: str
    qualification_terminal_ledger_digest: str
    evidence_origin: str
    identity: str
    _state: ClassVar[_FinalAdmissionState]

    @classmethod
    def from_authority(cls, authority: ActiveFinalAuthority, *args: Any, **kwargs: Any) -> "FinalCampaignAdmission":
        return admit_final_campaign(authority, *args, **kwargs)

    admit = from_authority

    def __init__(
        self,
        authority: ActiveFinalAuthority,
        campaign_id: str,
        manifest: FinalCampaignManifest,
        common_closure_digest: str,
        qualification_semantic_projection_digest: str,
        qualification_probe_projection_digest: str,
        qualification_terminal_receipt_census_digest: str,
        qualification_terminal_event_digest: str,
        qualification_terminal_ledger_digest: str,
        evidence_origin: str,
        token: object = None,
    ) -> None:
        if token is not _FINAL_ADMISSION_TOKEN:
            raise TypeError("final campaign admissions are parent-minted")
        _validate_active_final_authority(authority)
        expected_origin = _authority_evidence_origin(authority)
        if not isinstance(manifest, FinalCampaignManifest):
            raise TypeError("typed final phase manifest required")
        if not manifest.validate_integrity():
            raise ProvenanceError("final_manifest_integrity_mismatch")
        if not isinstance(campaign_id, str) or not campaign_id:
            raise ValueError("final campaign identity is required")
        expected_closure = final_common_closure(authority, manifest)
        if common_closure_digest != expected_closure:
            raise ProvenanceError("final_prerequisite_mismatch")
        if (
            not _digest(qualification_semantic_projection_digest)
            or not _digest(qualification_probe_projection_digest)
            or not _digest(qualification_terminal_receipt_census_digest)
            or not _digest(qualification_terminal_event_digest)
            or not _digest(qualification_terminal_ledger_digest)
            or evidence_origin not in _FINAL_EVIDENCE_ORIGINS
            or evidence_origin != expected_origin
            or manifest.evidence_origin != expected_origin
        ):
            raise ProvenanceError("final_prerequisite_mismatch")
        state = _FinalAdmissionState()
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "authority_binding", authority.binding)
        object.__setattr__(self, "campaign_id", campaign_id)
        object.__setattr__(self, "manifest", manifest)
        object.__setattr__(self, "schedule", FINAL_SCHEDULE)
        object.__setattr__(self, "common_closure_digest", common_closure_digest)
        object.__setattr__(self, "profile_digest", authority.profile_digest)
        object.__setattr__(self, "qualification_semantic_projection_digest", qualification_semantic_projection_digest)
        object.__setattr__(self, "qualification_probe_projection_digest", qualification_probe_projection_digest)
        object.__setattr__(self, "qualification_terminal_receipt_census_digest", qualification_terminal_receipt_census_digest)
        object.__setattr__(self, "qualification_terminal_event_digest", qualification_terminal_event_digest)
        object.__setattr__(self, "qualification_terminal_ledger_digest", qualification_terminal_ledger_digest)
        object.__setattr__(self, "evidence_origin", evidence_origin)
        object.__setattr__(self, "_state", state)
        object.__setattr__(self, "identity", canonical_sha256(
            _final_admission_payload(self)
        ))

    def _validate_cell_observation(self, value: FinalCellEvidence, cell_id: str) -> None:
        if not isinstance(value, FinalCellEvidence):
            raise TypeError("typed final cell evidence required")
        if not value.validate_integrity():
            raise ProvenanceError("final_evidence_integrity_mismatch")
        if (
            value.cell_id != cell_id
            or value.cell_id not in FINAL_SCHEDULE
            or value.profile_digest != self.profile_digest
            or value.campaign_id != self.campaign_id
            or value.phase != FINAL_PHASE
            or value.manifest_identity != self.manifest.manifest_identity
            or value.manifest_digest != self.manifest.manifest_digest
            or value.common_closure_digest != self.common_closure_digest
            or value.authority_binding != self.authority_binding
            or value.terminal_verified is not True
            or value.result != "passed"
            or value.execution_provenance != "live_final"
            or value.evidence_origin != self.evidence_origin
            or value.fresh_root is not True
            or value.retry
            or value.resumed
            or value.replacement
        ):
            raise ProvenanceError("final_prerequisite_mismatch")

    @property
    def cell_count(self) -> int:
        return FINAL_CELL_COUNT

    @property
    def cell_ids(self) -> tuple[str, ...]:
        return self.schedule

    @property
    def phase(self) -> str:
        return FINAL_PHASE

    @property
    def binding(self) -> AuthorityBinding:
        return self.authority_binding

    @property
    def authority_identity(self) -> str:
        return self.authority.identity

    @property
    def origin(self) -> str:
        return self.evidence_origin

    @property
    def runtime_admissible(self) -> bool:
        try:
            _validate_active_final_authority(self.authority)
        except (TypeError, ValueError, ProvenanceError):
            return False
        return (
            self.is_parent_owned()
            and
            self.evidence_origin == RUNTIME_VERIFIED_ORIGIN
            and self.manifest.evidence_origin == RUNTIME_VERIFIED_ORIGIN
            and self.authority.runtime_admissible
        )

    def is_parent_owned(self) -> bool:
        try:
            authority_identity = self.authority.identity
        except (AttributeError, TypeError, ValueError):
            return False
        with _FINAL_ADMISSION_LOCK:
            snapshot = _FINAL_ADMISSION_SNAPSHOTS.get(authority_identity)
            state_digest = _FINAL_ADMISSION_STATE_DIGESTS.get(authority_identity)
            registered = _FINAL_ADMISSIONS.get(authority_identity) is self
        if not registered:
            return False
        try:
            if type(self.schedule) is not tuple or self.schedule != FINAL_SCHEDULE:
                return False
            if not self.manifest.validate_integrity():
                return False
            expected = (
                self.identity,
                canonical_sha256(_final_admission_payload(self)),
            )
            current_state_digest = _final_admission_state_digest(self)
        except (AttributeError, TypeError, ValueError, ProvenanceError):
            return False
        return snapshot == expected and state_digest == current_state_digest

    def owns_cell_authority(self, authority: Any) -> bool:
        if type(authority) is not FinalCellAuthority:
            return False
        with self._state.lock:
            return (
                self.is_parent_owned()
                and authority.admission is self
                and self._state.cell_authorities.get(authority.cell_id) is authority
                and authority.cell_id in FINAL_SCHEDULE
                and authority.ordinal == FINAL_SCHEDULE.index(authority.cell_id)
                and authority.authority_binding == self.authority_binding
                and authority.identity == canonical_sha256(
                    {
                        "artifact": "minecraft-k12-live-final-cell-authority/1",
                        "admission": self.identity,
                        "cell_id": authority.cell_id,
                        "ordinal": authority.ordinal,
                        "authority_binding": self.authority_binding.canonical(),
                    }
                )
            )

    @property
    def complete(self) -> bool:
        return len(self._state.consumed) == FINAL_CELL_COUNT

    @property
    def issued_cells(self) -> tuple[str, ...]:
        with self._state.lock:
            return tuple(cell_id for cell_id in FINAL_SCHEDULE if cell_id in self._state.issued)

    @property
    def consumed_cells(self) -> tuple[str, ...]:
        with self._state.lock:
            return tuple(cell_id for cell_id in FINAL_SCHEDULE if cell_id in self._state.consumed)

    @property
    def launched_cells(self) -> tuple[str, ...]:
        with self._state.lock:
            return tuple(cell_id for cell_id in FINAL_SCHEDULE if cell_id in self._state.launched)

    @property
    def observations(self) -> tuple[FinalCellEvidence, ...]:
        return tuple(self._state.observations[cell_id] for cell_id in FINAL_SCHEDULE if cell_id in self._state.observations)

    def matches_qualification(self, attestation: QualificationSemanticAttestation) -> bool:
        return (
            isinstance(attestation, QualificationSemanticAttestation)
            and self.authority.owner.owns_qualification_semantic_attestation(attestation)
            and attestation.semantic_projection_digest
                == self.qualification_semantic_projection_digest
            and attestation.probe_terminal_digest
                == self.qualification_probe_projection_digest
            and attestation.terminal_receipt_digest
                == self.qualification_terminal_receipt_census_digest
            and attestation.terminal_event_digest
                == self.qualification_terminal_event_digest
            and attestation.terminal_ledger_digest == self.qualification_terminal_ledger_digest
            and attestation.profile_digest == self.profile_digest
            and self.authority.body.get("qualification_semantic_attestation_digest")
                == attestation.identity
            and self.authority.body.get("qualification_authority_digest")
                == attestation.qualification_authority_digest
            and attestation.evidence_origin == self.evidence_origin
        )

    def validate(self) -> bool:
        return self.runtime_admissible

    @contextmanager
    def _target_commit_guard(self):
        """Keep the authority-bound target lease across each state commit."""
        lease = getattr(self.authority, "target_lease", None)
        owner = getattr(self.authority, "owner", None)
        if lease is None or owner is None:
            raise ProvenanceError("target_lock_loss")
        owned_by = getattr(lease, "owned_by", None)
        commit_guard = getattr(lease, "commit_guard", None)
        revalidate = getattr(lease, "revalidate", None)
        if not callable(owned_by) or not callable(commit_guard) or not callable(revalidate):
            raise ProvenanceError("target_lock_loss")
        with commit_guard():
            if not owned_by(owner):
                raise ProvenanceError("target_lock_loss")
            revalidate()
            yield

    def issue_cell(self, cell_id: str) -> "FinalCellAuthority":
        with _authority_terminalization_guard(self.authority):
            with self._target_commit_guard():
                _validate_active_final_authority(self.authority)
                if not self.is_parent_owned():
                    raise ProvenanceError("authority_replay")
                if cell_id not in FINAL_SCHEDULE:
                    raise ValueError("cell is outside the authenticated final schedule")
                with self._state.lock:
                    if cell_id in self._state.issued:
                        raise ProvenanceError("authority_replay")
                    next_ordinal = len(self._state.issued)
                    if (
                        next_ordinal >= FINAL_CELL_COUNT
                        or FINAL_SCHEDULE[next_ordinal] != cell_id
                    ):
                        raise ProvenanceError("final_schedule_order")
                    preloaded = self._state.observations.get(cell_id)
                    if preloaded is not None:
                        self._validate_cell_observation(preloaded, cell_id)
                    self._state.issued.add(cell_id)
                    ordinal = FINAL_SCHEDULE.index(cell_id)
                    authority = FinalCellAuthority(
                        self,
                        cell_id,
                        ordinal,
                        _FINAL_CELL_TOKEN,
                    )
                    self._state.cell_authorities[cell_id] = authority
                    _record_final_admission_state(self)
                    return authority

    cell_authority = issue_cell
    authorize_cell = issue_cell
    for_cell = issue_cell

    def issue_all(self) -> tuple["FinalCellAuthority", ...]:
        return tuple(self.issue_cell(cell_id) for cell_id in FINAL_SCHEDULE)

    def _consume_for_launch(self, authority: "FinalCellAuthority") -> "FinalLaunchPermit":
        with _authority_terminalization_guard(self.authority):
            with self._target_commit_guard():
                _validate_active_final_authority(self.authority)
                if (
                    not self.is_parent_owned()
                    or not isinstance(authority, FinalCellAuthority)
                    or authority.admission is not self
                ):
                    raise ProvenanceError("authority_namespace_mismatch")
                with self._state.lock:
                    _validate_active_final_authority(self.authority)
                    if authority.cell_id not in self._state.issued:
                        raise ProvenanceError("authority_replay")
                    if not self.owns_cell_authority(authority):
                        raise ProvenanceError("authority_namespace_mismatch")
                    if authority.cell_id in self._state.launched or authority.cell_id in self._state.consumed:
                        raise ProvenanceError("authority_replay")
                    next_ordinal = len(self._state.launched)
                    if (
                        next_ordinal >= FINAL_CELL_COUNT
                        or authority.ordinal != next_ordinal
                        or FINAL_SCHEDULE[next_ordinal] != authority.cell_id
                    ):
                        raise ProvenanceError("final_schedule_order")
                    self._state.launched.add(authority.cell_id)
                    permit = FinalLaunchPermit(authority, _FINAL_LAUNCH_TOKEN)
                    self._state.launch_permits[authority.cell_id] = permit
                    _record_final_admission_state(self)
                    return permit

    def _complete_cell(
        self, authority: "FinalCellAuthority", terminal_evidence: FinalCellEvidence
    ) -> FinalCellEvidence:
        with _authority_terminalization_guard(self.authority):
            with self._target_commit_guard():
                _validate_active_final_authority(self.authority)
                if (
                    not self.is_parent_owned()
                    or not isinstance(authority, FinalCellAuthority)
                    or authority.admission is not self
                ):
                    raise ProvenanceError("authority_namespace_mismatch")
                with self._state.lock:
                    _validate_active_final_authority(self.authority)
                    if authority.cell_id not in self._state.launched:
                        raise ProvenanceError("final_cell_launch_required")
                    if not self.owns_cell_authority(authority):
                        raise ProvenanceError("authority_namespace_mismatch")
                    if authority.cell_id not in self._state.launch_permits:
                        raise ProvenanceError("authority_replay")
                    if authority.cell_id in self._state.consumed:
                        raise ProvenanceError("authority_replay")
                    next_ordinal = len(self._state.consumed)
                    if (
                        next_ordinal >= FINAL_CELL_COUNT
                        or authority.ordinal != next_ordinal
                        or FINAL_SCHEDULE[next_ordinal] != authority.cell_id
                    ):
                        raise ProvenanceError("final_schedule_order")
                    if not isinstance(terminal_evidence, FinalCellEvidence):
                        raise TypeError("typed terminal final-cell evidence is required")
                    self._validate_cell_observation(terminal_evidence, authority.cell_id)
                    self._state.consumed.add(authority.cell_id)
                    self._state.observations[authority.cell_id] = terminal_evidence
                    _record_final_admission_state(self)
                    return terminal_evidence

    def consume_for_launch(self, authority: "FinalCellAuthority") -> "FinalLaunchPermit":
        return self._consume_for_launch(authority)

    def complete(
        self, authority: "FinalCellAuthority", terminal_evidence: FinalCellEvidence
    ) -> FinalCellEvidence:
        return self._complete_cell(authority, terminal_evidence)

    def consume(
        self, authority: "FinalCellAuthority", evidence: FinalCellEvidence | None = None
    ) -> "FinalLaunchPermit":
        """Compatibility spelling for the prelaunch boundary.

        Terminal evidence is intentionally rejected here; completion belongs
        to :meth:`complete` after the external launch has returned.
        """

        if evidence is not None:
            raise ProvenanceError("terminal evidence is post-launch")
        return self._consume_for_launch(authority)


@dataclass(frozen=True, slots=True, init=False)
class FinalCellAuthority:
    """One non-replayable final-cell capability owned by an admission."""

    admission: FinalCampaignAdmission
    cell_id: str
    ordinal: int
    authority_binding: AuthorityBinding
    identity: str

    @classmethod
    def from_admission(
        cls, admission: FinalCampaignAdmission, cell_id: str
    ) -> "FinalCellAuthority":
        if not isinstance(admission, FinalCampaignAdmission):
            raise TypeError("typed final campaign admission required")
        return admission.issue_cell(cell_id)

    def __init__(
        self,
        admission: FinalCampaignAdmission,
        cell_id: str,
        ordinal: int,
        token: object = None,
    ) -> None:
        if token is not _FINAL_CELL_TOKEN or not isinstance(admission, FinalCampaignAdmission):
            raise TypeError("final cell authorities are admission-minted")
        if cell_id not in FINAL_SCHEDULE or ordinal != FINAL_SCHEDULE.index(cell_id):
            raise ValueError("final cell is outside the authenticated schedule")
        object.__setattr__(self, "admission", admission)
        object.__setattr__(self, "cell_id", cell_id)
        object.__setattr__(self, "ordinal", ordinal)
        object.__setattr__(self, "authority_binding", admission.authority_binding)
        object.__setattr__(
            self,
            "identity",
            canonical_sha256(
                {
                    "artifact": "minecraft-k12-live-final-cell-authority/1",
                    "admission": admission.identity,
                    "cell_id": cell_id,
                    "ordinal": ordinal,
                    "authority_binding": admission.authority_binding.canonical(),
                }
            ),
        )

    @property
    def authority(self) -> ActiveFinalAuthority:
        return self.admission.authority

    @property
    def binding(self) -> AuthorityBinding:
        return self.authority_binding

    @property
    def profile_digest(self) -> str:
        return self.admission.profile_digest

    @property
    def campaign_id(self) -> str:
        return self.admission.campaign_id

    @property
    def origin(self) -> str:
        return self.admission.evidence_origin

    @property
    def evidence_origin(self) -> str:
        return self.admission.evidence_origin

    @property
    def phase(self) -> str:
        return FINAL_PHASE

    @property
    def manifest(self) -> FinalCampaignManifest:
        return self.admission.manifest

    @property
    def common_closure_digest(self) -> str:
        return self.admission.common_closure_digest

    @property
    def closure_digest(self) -> str:
        return self.common_closure_digest

    @property
    def consumed(self) -> bool:
        return self.cell_id in self.admission._state.consumed

    @property
    def runtime_admissible(self) -> bool:
        return self.admission.runtime_admissible

    @property
    def launch_consumed(self) -> bool:
        with self.admission._state.lock:
            return self.cell_id in self.admission._state.launched

    @property
    def launch_permit(self) -> "FinalLaunchPermit | None":
        with self.admission._state.lock:
            return self.admission._state.launch_permits.get(self.cell_id)

    @property
    def launched(self) -> bool:
        return self.launch_consumed

    def consume_for_launch(self) -> "FinalLaunchPermit":
        """Atomically consume this cell's prelaunch admission."""

        return self.admission._consume_for_launch(self)

    def complete(self, terminal_evidence: FinalCellEvidence) -> FinalCellEvidence:
        """Validate and terminalize evidence after a successful launch."""

        return self.admission._complete_cell(self, terminal_evidence)

    def require_for_containment(
        self,
        terminal_evidence: FinalCellEvidence | None = None,
        *,
        launch_permit: "FinalLaunchPermit | None" = None,
    ) -> "FinalLaunchPermit":
        """Authenticate the completed post-dispatch state for containment."""

        return _require_completed_final_cell_containment(
            self,
            launch_permit=launch_permit,
            terminal_evidence=terminal_evidence,
        )

    def validate_for_containment(
        self,
        terminal_evidence: FinalCellEvidence | None = None,
        *,
        launch_permit: "FinalLaunchPermit | None" = None,
    ) -> bool:
        """Return whether this cell is complete and containment-admissible."""

        try:
            self.require_for_containment(
                terminal_evidence,
                launch_permit=launch_permit,
            )
        except Exception:
            return False
        return True

    validate_completed_containment = validate_for_containment
    validate_containment = validate_for_containment
    require_containment = require_for_containment

    def consume(self, evidence: FinalCellEvidence | None = None) -> "FinalLaunchPermit":
        """Compatibility spelling for :meth:`consume_for_launch`."""

        if evidence is not None:
            raise ProvenanceError("terminal evidence is post-launch")
        return self.consume_for_launch()

    consume_observation = consume
    consume_evidence = consume


_FINAL_LAUNCH_TOKEN = object()


def _final_launch_permit_identity(authority: "FinalCellAuthority") -> str:
    return canonical_sha256(
        {
            "artifact": "minecraft-k12-live-final-launch-permit/1",
            "admission": authority.admission.identity,
            "cell_authority": authority.identity,
            "cell_id": authority.cell_id,
            "ordinal": authority.ordinal,
            "authority_binding": authority.binding.canonical(),
            "evidence_origin": authority.evidence_origin,
        }
    )


@dataclass(frozen=True, slots=True, init=False)
class FinalLaunchPermit:
    """Parent-minted proof that one final cell crossed the launch boundary."""

    cell_authority: FinalCellAuthority = field(repr=False, compare=False)
    admission_identity: str
    cell_id: str
    ordinal: int
    authority_binding: AuthorityBinding
    evidence_origin: str
    identity: str

    def __init__(self, authority: FinalCellAuthority, token: object = None) -> None:
        if token is not _FINAL_LAUNCH_TOKEN or not isinstance(authority, FinalCellAuthority):
            raise TypeError("final launch permits are parent-minted")
        object.__setattr__(self, "cell_authority", authority)
        object.__setattr__(self, "admission_identity", authority.admission.identity)
        object.__setattr__(self, "cell_id", authority.cell_id)
        object.__setattr__(self, "ordinal", authority.ordinal)
        object.__setattr__(self, "authority_binding", authority.binding)
        object.__setattr__(self, "evidence_origin", authority.evidence_origin)
        object.__setattr__(self, "identity", _final_launch_permit_identity(authority))

    @property
    def admission(self) -> FinalCampaignAdmission:
        return self.cell_authority.admission

    @property
    def authority(self) -> ActiveFinalAuthority:
        return self.cell_authority.authority

    @property
    def cell_authority_identity(self) -> str:
        return self.cell_authority.identity

    @property
    def authority_identity(self) -> str:
        return self.cell_authority.authority.identity

    @property
    def binding(self) -> AuthorityBinding:
        return self.authority_binding

    @property
    def permit(self) -> "FinalLaunchPermit":
        return self

    @property
    def receipt(self) -> "FinalLaunchPermit":
        return self

    @property
    def active(self) -> bool:
        return self.cell_authority.launch_consumed and not self.cell_authority.consumed

    def require_for_containment(
        self,
        terminal_evidence: FinalCellEvidence | None = None,
        *,
        cell_authority: FinalCellAuthority | None = None,
    ) -> "FinalLaunchPermit":
        """Authenticate this immutable launch permit after completion."""

        if cell_authority is not None and cell_authority is not self.cell_authority:
            raise ProvenanceError("authority_namespace_mismatch")
        return _require_completed_final_cell_containment(
            self.cell_authority,
            launch_permit=self,
            terminal_evidence=terminal_evidence,
        )

    def validate_for_containment(
        self,
        terminal_evidence: FinalCellEvidence | None = None,
        *,
        cell_authority: FinalCellAuthority | None = None,
    ) -> bool:
        """Return whether this permit authenticates completed containment."""

        try:
            self.require_for_containment(
                terminal_evidence,
                cell_authority=cell_authority,
            )
        except Exception:
            return False
        return True

    validate_completed_containment = validate_for_containment
    validate_containment = validate_for_containment
    require_containment = require_for_containment


def _require_current_final_parent(authority: "FinalCellAuthority") -> None:
    parent = authority.authority
    _validate_active_final_authority(parent)
    from .k12_guarded_backend import authority_binding_is_current

    if not authority_binding_is_current(
        parent,
        authority.binding,
        profile_digest=authority.profile_digest,
        namespace=LIVE_FINAL_NAMESPACE,
        allow_injected=authority.evidence_origin == INJECTED_FAKE_ORIGIN,
    ):
        raise ProvenanceError("final_cell_containment_authority_stale")


def _require_completed_final_cell_containment(
    authority: "FinalCellAuthority",
    *,
    launch_permit: "FinalLaunchPermit | None" = None,
    terminal_evidence: FinalCellEvidence | None = None,
) -> "FinalLaunchPermit":
    """Validate the only state in which final containment may begin."""

    if not isinstance(authority, FinalCellAuthority):
        raise TypeError("typed final cell authority required")
    admission = authority.admission
    if not isinstance(admission, FinalCampaignAdmission):
        raise ProvenanceError("authority_namespace_mismatch")
    if authority.binding != admission.binding:
        raise ProvenanceError("authority_namespace_mismatch")
    if not admission.is_parent_owned():
        raise ProvenanceError("authority_namespace_mismatch")
    with admission._state.lock:
        if admission._state.cell_authorities.get(authority.cell_id) is not authority:
            raise ProvenanceError("authority_namespace_mismatch")
        stored_permit = admission._state.launch_permits.get(authority.cell_id)
        if not isinstance(stored_permit, FinalLaunchPermit):
            raise ProvenanceError("final_cell_launch_required")
        selected_permit = stored_permit if launch_permit is None else launch_permit
        if selected_permit is not stored_permit:
            raise ProvenanceError("authority_namespace_mismatch")
        if (
            selected_permit.cell_authority is not authority
            or selected_permit.admission_identity != admission.identity
            or selected_permit.cell_id != authority.cell_id
            or selected_permit.ordinal != authority.ordinal
            or selected_permit.authority_binding != authority.binding
            or selected_permit.evidence_origin != authority.evidence_origin
            or selected_permit.identity != _final_launch_permit_identity(authority)
        ):
            raise ProvenanceError("authority_namespace_mismatch")
        if (
            authority.cell_id not in admission._state.launched
            or authority.cell_id not in admission._state.consumed
        ):
            raise ProvenanceError("final_cell_completion_required")
        completed_evidence = admission._state.observations.get(authority.cell_id)
        if not isinstance(completed_evidence, FinalCellEvidence):
            raise ProvenanceError("final_cell_completion_required")
        admission._validate_cell_observation(completed_evidence, authority.cell_id)
        if terminal_evidence is not None and terminal_evidence is not completed_evidence:
            raise ProvenanceError("final_cell_evidence_mismatch")
    if not admission.is_parent_owned():
        raise ProvenanceError("authority_namespace_mismatch")
    _require_current_final_parent(authority)
    return selected_permit


FinalLaunchReceipt = FinalLaunchPermit
FinalCellLaunchPermit = FinalLaunchPermit
FinalCellLaunchReceipt = FinalLaunchPermit


FinalCellCapability = FinalCellAuthority
FinalAdmission = FinalCampaignAdmission
FinalAdmissionScope = FinalCampaignAdmission
FinalCellScope = FinalCellAuthority


def admit_final_campaign(
    authority: ActiveFinalAuthority,
    qualification: QualificationSemanticAttestation | None = None,
    *,
    manifest: FinalCampaignManifest | Mapping[str, Any] | None = None,
    manifest_identity: str = "",
    manifest_digest: str = "",
    manifest_phase: str = FINAL_PHASE,
    campaign_id: str = "",
    schedule: Sequence[str] | None = None,
    common_closure_digest: str = "",
    evidence_origin: str | None = None,
) -> FinalCampaignAdmission:
    """Mint one final campaign admission from an active final authority.

    The parent-owned semantic attestation is the only qualification input at
    this boundary. The final campaign identity must also be
    distinct from both qualification campaign identities; omitting it can
    never authorize a launch.
    """

    _validate_active_final_authority(authority)
    expected_origin = _authority_evidence_origin(authority)
    if manifest is None and manifest_identity and manifest_digest:
        manifest = FinalCampaignManifest(
            manifest_phase,
            manifest_identity,
            manifest_digest,
            FINAL_SCHEDULE,
            evidence_origin=evidence_origin or expected_origin,
        )
    if manifest is None:
        raise TypeError("typed final phase manifest required")
    if schedule is not None and tuple(schedule) != FINAL_SCHEDULE:
        raise ValueError("final campaign schedule mismatch")
    if isinstance(manifest, Mapping):
        manifest = FinalCampaignManifest.from_mapping(manifest)
    if not isinstance(manifest, FinalCampaignManifest):
        raise TypeError("typed final phase manifest required")
    body = authority.body
    qualification_digest = body.get("qualification_semantic_projection_digest", "")
    probe_digest = body.get("qualification_probe_projection_digest", "")
    receipt_digest = body.get("qualification_terminal_receipt_census_digest", "")
    terminal_event_digest = body.get("qualification_terminal_event_digest", "")
    terminal_digest = body.get("qualification_terminal_ledger_digest", "")
    if isinstance(qualification, QualificationSemanticAttestation):
        attestation = qualification
        if not authority.owner.owns_qualification_semantic_attestation(attestation):
            raise ProvenanceError("final_prerequisite_mismatch")
    else:
        raise TypeError("parent-owned qualification semantic attestation required")
    if (
        attestation.semantic_projection_digest != qualification_digest
        or attestation.probe_terminal_digest != probe_digest
        or attestation.terminal_receipt_digest != receipt_digest
        or attestation.terminal_event_digest != terminal_event_digest
        or attestation.terminal_ledger_digest != terminal_digest
        or attestation.profile_digest != authority.profile_digest
        or attestation.qualification_authority_digest
            != body.get("qualification_authority_digest")
        or attestation.identity != body.get("qualification_semantic_attestation_digest")
        or attestation.evidence_origin != expected_origin
        or manifest.evidence_origin != expected_origin
    ):
        raise ProvenanceError("final_prerequisite_mismatch")
    if not all(_digest(value) for value in (
        qualification_digest, probe_digest, receipt_digest,
        terminal_event_digest, terminal_digest,
    )):
        raise ProvenanceError("final_prerequisite_mismatch")
    if not campaign_id:
        raise ValueError("final campaign identity is required")
    if campaign_id in {attestation.cell_campaign_id, attestation.probe_campaign_id}:
        raise ProvenanceError("final_prerequisite_mismatch")
    observed_origin = manifest.evidence_origin
    if evidence_origin is None:
        evidence_origin = observed_origin
    if evidence_origin not in _FINAL_EVIDENCE_ORIGINS:
        raise ValueError("final evidence origin is invalid")
    if evidence_origin != expected_origin:
        raise ProvenanceError("final_prerequisite_mismatch")
    closure = final_common_closure(authority, manifest)
    if common_closure_digest and common_closure_digest != closure:
        raise ProvenanceError("final_prerequisite_mismatch")
    with _FINAL_ADMISSION_LOCK:
        if authority.identity in _FINAL_ADMISSION_KEYS:
            raise ProvenanceError("authority_replay")
        admission = FinalCampaignAdmission(
            authority,
            campaign_id,
            manifest,
            closure,
            qualification_digest,
            probe_digest,
            receipt_digest,
            terminal_event_digest,
            terminal_digest,
            evidence_origin,
            _FINAL_ADMISSION_TOKEN,
        )
        _FINAL_ADMISSION_KEYS.add(authority.identity)
        _FINAL_ADMISSIONS[authority.identity] = admission
        _FINAL_ADMISSION_SNAPSHOTS[authority.identity] = (
            admission.identity,
            canonical_sha256(_final_admission_payload(admission)),
        )
        _FINAL_ADMISSION_STATE_DIGESTS[authority.identity] = (
            _final_admission_state_digest(admission)
        )
    return admission


build_final_campaign_admission = admit_final_campaign
final_campaign_admission = admit_final_campaign
build_final_admission = admit_final_campaign


def issue_final_cell_authority(
    admission: FinalCampaignAdmission, cell_id: str
) -> FinalCellAuthority:
    if not isinstance(admission, FinalCampaignAdmission):
        raise TypeError("typed final campaign admission required")
    return admission.issue_cell(cell_id)


final_cell_authority = issue_final_cell_authority
issue_final_cell = issue_final_cell_authority


def consume_final_cell_authority(authority: FinalCellAuthority) -> FinalLaunchPermit:
    if not isinstance(authority, FinalCellAuthority):
        raise TypeError("typed final cell authority required")
    return authority.consume_for_launch()


consume_final_cell = consume_final_cell_authority


def complete_final_cell_authority(
    authority: FinalCellAuthority, terminal_evidence: FinalCellEvidence
) -> FinalCellEvidence:
    if not isinstance(authority, FinalCellAuthority):
        raise TypeError("typed final cell authority required")
    return authority.complete(terminal_evidence)


complete_final_cell = complete_final_cell_authority


@dataclass(frozen=True, slots=True, init=False)
class LiveFinalWrapper:
    schedule_count: int
    profile_digest: str
    campaign_id: str
    identity: str = LIVE_FINAL_WRAPPER_IDENTITY
    authority_binding: AuthorityBinding | None = None
    digest: str = field(init=False)

    def __init__(
        self,
        schedule_count: int,
        profile_digest: str,
        campaign_id: str,
        identity: str | AuthorityBinding = LIVE_FINAL_WRAPPER_IDENTITY,
        authority_binding: AuthorityBinding | None = None,
    ) -> None:
        # `/2` callers historically used the fourth positional value for the
        # wrapper identity; the authority-owned API uses it for the binding.
        # Accept both spellings without weakening the identity check.
        if isinstance(identity, AuthorityBinding) and authority_binding is None:
            authority_binding = identity
            identity = LIVE_FINAL_WRAPPER_IDENTITY
        object.__setattr__(self, "schedule_count", schedule_count)
        object.__setattr__(self, "profile_digest", profile_digest)
        object.__setattr__(self, "campaign_id", campaign_id)
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "authority_binding", authority_binding)
        self.__post_init__()

    def __post_init__(self) -> None:
        if self.identity != LIVE_FINAL_WRAPPER_IDENTITY:
            raise ValueError("wrong final wrapper")
        binding = self.authority_binding or AuthorityBinding.mock_only()
        if not isinstance(binding, AuthorityBinding):
            raise ValueError("final authority binding required")
        object.__setattr__(self, "authority_binding", binding)
        object.__setattr__(
            self,
            "digest",
            canonical_sha256(
                {
                    "identity": self.identity,
                    "schedule_count": self.schedule_count,
                    "profile_digest": self.profile_digest,
                    "campaign_id": self.campaign_id,
                    "authority_binding": binding.canonical(),
                }
            ),
        )


def _object_identity(value: Any) -> Any:
    return getattr(value, "identity", None)


@dataclass(frozen=True, slots=True)
class FinalGateInput:
    wrapper: LiveFinalWrapper
    profile_digest: str
    campaign_id: str
    qualification_attestation: QualificationSemanticAttestation
    manifests_clean: bool
    schedule_count: int = FINAL_CELL_COUNT
    retry: bool = False
    resumed: bool = False
    replacement: bool = False
    authority_binding: AuthorityBinding | None = None
    execution_authority: ActiveFinalAuthority | None = None
    observed_at: int | None = None
    admission: FinalCampaignAdmission | None = None
    cell_authorities: tuple[FinalCellAuthority, ...] = ()
    authenticated_digest: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "authenticated_digest",
            canonical_sha256(
                {
                    "wrapper": getattr(self.wrapper, "digest", None),
                    "profile_digest": self.profile_digest,
                    "campaign_id": self.campaign_id,
                    "qualification_attestation":
                        _object_identity(self.qualification_attestation),
                    "manifests_clean": self.manifests_clean,
                    "schedule_count": self.schedule_count,
                    "retry": self.retry,
                    "resumed": self.resumed,
                    "replacement": self.replacement,
                    "authority_binding": (
                        self.authority_binding.canonical() if self.authority_binding else None
                    ),
                    "execution_authority": _object_identity(self.execution_authority),
                    "observed_at": self.observed_at,
                    "admission": _object_identity(self.admission),
                    "cell_authorities": [
                        _object_identity(value) for value in self.cell_authorities
                    ],
                }
            ),
        )


def _final_execution(value: FinalGateInput) -> FinalExecutionAuthority | None:
    authority = value.execution_authority
    if isinstance(authority, ActiveFinalAuthority):
        return authority.authority
    return authority if isinstance(authority, FinalExecutionAuthority) else None


def _final_launch_gate_unlocked(value: FinalGateInput) -> bool:
    """Accept only a complete live qualification/final authority closure."""

    if not isinstance(value, FinalGateInput):
        return False
    expected = canonical_sha256(
        {
            "wrapper": getattr(value.wrapper, "digest", None),
            "profile_digest": value.profile_digest,
            "campaign_id": value.campaign_id,
            "qualification_attestation":
                _object_identity(value.qualification_attestation),
            "manifests_clean": value.manifests_clean,
            "schedule_count": value.schedule_count,
            "retry": value.retry,
            "resumed": value.resumed,
            "replacement": value.replacement,
            "authority_binding": (
                value.authority_binding.canonical() if value.authority_binding else None
            ),
            "execution_authority": _object_identity(value.execution_authority),
            "observed_at": value.observed_at,
            "admission": _object_identity(value.admission),
            "cell_authorities": [_object_identity(item) for item in value.cell_authorities],
        }
    )
    if value.authenticated_digest != expected:
        return False
    if not isinstance(
        value.qualification_attestation, QualificationSemanticAttestation,
    ):
        return False
    if not isinstance(value.admission, FinalCampaignAdmission):
        return False
    authority = _final_execution(value)
    binding = value.authority_binding
    if authority is None or not isinstance(binding, AuthorityBinding):
        return False
    active = value.admission.authority
    if (
        not isinstance(value.execution_authority, ActiveFinalAuthority)
        or value.execution_authority is not active
        or not value.admission.is_parent_owned()
        or not value.admission.runtime_admissible
        or not value.admission.matches_qualification(value.qualification_attestation)
        or authority.identity != active.identity
        or binding != active.binding
         or not authority.owns(binding)
         or binding.authority_type != FINAL_AUTHORITY
         or binding.provenance != LIVE_FINAL_NAMESPACE
         or binding.namespace != LIVE_FINAL_NAMESPACE
         or binding.origin != RUNTIME_VERIFIED_ORIGIN
        or binding.lifecycle != "active"
        or value.wrapper.authority_binding != binding
    ):
        return False
    if type(value.observed_at) is not int:
        return False
    attestation = value.qualification_attestation
    if not (
        authority.body["issued_at"] <= value.observed_at <= authority.body["expires_at"]
    ):
        return False
    if (
        authority.body.get("profile_digest") != value.profile_digest
        or authority.body.get("qualification_semantic_projection_digest")
            != attestation.semantic_projection_digest
        or authority.body.get("qualification_probe_projection_digest")
            != attestation.probe_terminal_digest
        or authority.body.get("qualification_terminal_receipt_census_digest")
            != attestation.terminal_receipt_digest
        or authority.body.get("qualification_terminal_event_digest")
            != attestation.terminal_event_digest
        or authority.body.get("qualification_semantic_attestation_digest")
            != attestation.identity
        or value.wrapper.identity != LIVE_FINAL_WRAPPER_IDENTITY
         or value.wrapper.digest
         != LiveFinalWrapper(
             value.schedule_count,
             value.profile_digest,
             value.campaign_id,
             authority_binding=binding,
         ).digest
         or value.schedule_count != FINAL_CELL_COUNT
         or value.profile_digest != value.wrapper.profile_digest
         or value.campaign_id != value.wrapper.campaign_id
         or value.admission.campaign_id != value.campaign_id
         or attestation.profile_digest != value.profile_digest
         or attestation.evidence_origin != LIVE_EVIDENCE_ORIGIN
        or len(
            {
                value.campaign_id,
                attestation.cell_campaign_id,
                attestation.probe_campaign_id,
            }
        )
        != 3
        or value.manifests_clean is not True
        or value.retry
        or value.resumed
        or value.replacement
    ):
        return False
    if value.cell_authorities:
        if type(value.cell_authorities) is not tuple or len(value.cell_authorities) != FINAL_CELL_COUNT:
            return False
        if tuple(item.cell_id for item in value.cell_authorities) != FINAL_SCHEDULE:
            return False
        if any(
            not isinstance(item, FinalCellAuthority)
            or item.admission is not value.admission
            or not value.admission.owns_cell_authority(item)
            or item.authority_binding != binding
            for item in value.cell_authorities
        ):
            return False
    return True


def final_launch_gate(value: FinalGateInput) -> bool:
    if not isinstance(value, FinalGateInput):
        return False
    authority = value.execution_authority
    if isinstance(authority, ActiveFinalAuthority):
        # Qualification publication acquires semantic -> terminalization;
        # final admission validation must use the same order.
        with _authority_semantic_guard(authority):
            with _authority_terminalization_guard(authority):
                return _final_launch_gate_unlocked(value)
    return _final_launch_gate_unlocked(value)


final_gate_accepts = final_launch_gate


def validate_pid_tree(
    *,
    main_pid: int,
    main_start: int,
    observed_identity: tuple[int, int],
    session_leader: int,
    descendants: Any,
) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    if (
        type(main_pid) is not int
        or main_pid <= 0
        or type(main_start) is not int
        or main_start < 0
        or observed_identity != (main_pid, main_start)
    ):
        reasons.append("pid_start_identity_changed")
    if type(session_leader) is not int or session_leader != main_pid:
        reasons.append("setsid_leader_mismatch")
    if not (
        isinstance(descendants, (list, tuple))
        and all(type(pid) is int and pid > 0 for pid in descendants)
    ):
        reasons.append("invalid_descendants")
    return not reasons, tuple(reasons)


__all__ = [
    "LIVE_CONTAINMENT_PROBE_IDENTITY",
    "CONTAINMENT_PROBES",
    "LIVE_FINAL_WRAPPER_IDENTITY",
    "STOP_POLICY_IDENTITY",
    "FINAL_PHASE",
    "QUALIFICATION_PHASE",
    "RUNTIME_VERIFIED_ORIGIN",
    "INJECTED_FAKE_ORIGIN",
    "INJECTED_TEST_ORIGIN",
    "FINAL_CELL_COUNT",
    "FINAL_SCHEDULE",
    "FINAL_SCHEDULE_DIGEST",
    "FINAL_SCHEDULE_IDENTITY",
    "FINAL_FIXTURE_MANIFEST_IDENTITY",
    "FINAL_RANDOMIZATION_MANIFEST_IDENTITY",
    "FINAL_FIXTURE_MANIFEST_DIGEST",
    "FINAL_RANDOMIZATION_MANIFEST_DIGEST",
    "FINAL_MANIFEST_IDENTITY",
    "FINAL_PHASE_MANIFEST_IDENTITY",
    "FINAL_AUTHORITY",
    "AuthorityBinding",
    "FinalCampaignManifest",
    "FinalPhaseManifest",
    "FinalManifestObservation",
    "FinalCellEvidence",
    "FinalCellObservation",
    "FinalCampaignAdmission",
    "FinalAdmission",
    "FinalAdmissionScope",
    "FinalCellAuthority",
    "FinalCellCapability",
    "FinalCellScope",
    "FinalLaunchPermit",
    "FinalLaunchReceipt",
    "FinalCellLaunchPermit",
    "FinalCellLaunchReceipt",
    "final_cell_ids",
    "final_schedule",
    "final_common_closure",
    "load_final_campaign_manifest",
    "make_final_campaign_manifest",
    "final_manifest",
    "make_final_cell_evidence",
    "admit_final_campaign",
    "build_final_campaign_admission",
    "final_campaign_admission",
    "build_final_admission",
    "issue_final_cell_authority",
    "final_cell_authority",
    "issue_final_cell",
    "consume_final_cell_authority",
    "consume_final_cell",
    "complete_final_cell_authority",
    "complete_final_cell",
    "validate_live_containment",
    "validate_containment_probe_artifact",
    "load_detached_artifact",
    "load_k12_live_stop_policy",
    "load_k12_live_containment_probe",
    "STOP_POLICY_CONSEQUENCES",
    "LiveFinalWrapper",
    "FinalGateInput",
    "final_launch_gate",
    "final_gate_accepts",
    "validate_pid_tree",
]
