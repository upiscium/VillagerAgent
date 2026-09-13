"""Strict validation and the typed final launch gate for K12 live."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from benchmarks.common.eac.canonical import canonical_sha256
from .k12_live_qualification import QualificationAggregate, ProbeAggregate
from .k12_live_containment import LiveObservation, validate_final_observation
from .k12_runtime_profile import detached_digest, strict_json_load

LIVE_CONTAINMENT_PROBE_IDENTITY = "minecraft-k12-live-containment-probe/1"
CONTAINMENT_PROBES = ("P1", "P2", "P3", "P4")
LIVE_FINAL_WRAPPER_IDENTITY = "minecraft-eac-k12-live-controlled-recovery/2"
STOP_POLICY_IDENTITY = "minecraft-k12-live-stop-policy/1"

# Deliberately exhaustive: a caller cannot continue by inventing a new/default state.
def _stop(cell: str, capability: str, campaign_stop: bool,
          quarantine: bool, consequence: str) -> dict[str, Any]:
    return {"cell_disposition": cell, "capability": capability,
            "campaign_stop": campaign_stop, "target_quarantine": quarantine,
            "result_consequence": consequence}


STOP_POLICY_CONSEQUENCES = {
    "expected_stale_rejection": _stop("STOPPED_AFTER_REJECTION", "REVOKED", False, False, "valid_cell"),
    "known_objective_true": _stop("scientific_success", "REVOKED", False, False, "valid_cell"),
    "known_objective_false": _stop("scientific_non_success", "REVOKED", False, False, "qualification_fails"),
    "proposal_invalid": _stop("scientific_terminal", "REVOKED", False, False, "qualification_fails"),
    "reset_or_readback_failure": _stop("infrastructure_terminal", "POISONED", True, True, "disqualifier"),
    "provider_admission_failure": _stop("infrastructure_terminal", "REVOKED", True, False, "disqualifier"),
    "provider_termination_unknown": _stop("infrastructure_terminal", "POISONED", True, True, "disqualifier"),
    "authority_mismatch": _stop("integrity_terminal", "POISONED", True, True, "disqualifier"),
    "worker_active_operation": _stop("infrastructure_terminal", "POISONED", True, True, "disqualifier"),
    "native_effect_unknown": _stop("infrastructure_terminal", "POISONED", True, True, "disqualifier"),
    "oracle_unknown": _stop("infrastructure_terminal", "POISONED", True, True, "disqualifier"),
    "evidence_tamper": _stop("integrity_terminal", "POISONED", True, True, "disqualifier"),
    "containment_failure": _stop("infrastructure_terminal", "POISONED", True, True, "disqualifier"),
    "target_lock_loss": _stop("infrastructure_terminal", "POISONED", True, True, "disqualifier"),
    "identity_mismatch": _stop("prelaunch_infrastructure_stop", "REVOKED", True, False, "disqualifier"),
}

def validate_live_containment(observation: LiveObservation,
                              expected_cgroup: str) -> tuple[bool, tuple[str, ...]]:
    if not isinstance(observation, LiveObservation) or not validate_final_observation(observation):
        return False, ("untyped_observation",)
    reasons: tuple[str, ...] = ()
    if observation.main_pid != 0 or observation.main_start != 0: reasons += ("unit_pid_not_cleared",)
    if observation.active_state != "inactive": reasons += ("unit_not_inactive",)
    if observation.cgroup_procs: reasons += ("cgroup_procs_not_empty",)
    if observation.events_populated != 0: reasons += ("events_populated",)
    if observation.control_group != expected_cgroup: reasons += ("cgroup_changed",)
    if observation.descendants: reasons += ("descendants_present",)
    return not reasons, reasons

def validate_containment_probe_artifact(artifact: Mapping[str, Any]) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    if artifact.get("identity") != LIVE_CONTAINMENT_PROBE_IDENTITY: reasons.append("containment_probe_identity_mismatch")
    if artifact.get("probes") != list(CONTAINMENT_PROBES): reasons.append("containment_probe_set_mismatch")
    if artifact.get("passed") is not True: reasons.append("containment_probe_failed")
    if artifact.get("scope") not in (None, "campaign"): reasons.append("containment_probe_scope_mismatch")
    return not reasons, tuple(reasons)

def load_detached_artifact(path: str | Path) -> dict[str, Any]:
    value = strict_json_load(Path(path), "K12 live detached artifact")
    if not isinstance(value, dict) or value.get("detached_artifact_sha256") != detached_digest(value):
        raise ValueError("detached artifact digest mismatch")
    return value

def load_k12_live_stop_policy(path: str | Path | None = None) -> dict[str, Any]:
    path = path or Path(__file__).with_name("k12_live_stop_policy_v1.json")
    value = load_detached_artifact(path)
    if (value.get("artifact_id") != "minecraft-k12-live-stop-policy"
            or value.get("identity") != STOP_POLICY_IDENTITY
            or value.get("retry") != "forbidden"
            or value.get("resume") != "forbidden"
            or value.get("replacement") != "forbidden"
            or value.get("consequences") != STOP_POLICY_CONSEQUENCES):
        raise ValueError("stop policy is not exhaustive")
    return value

def load_k12_live_containment_probe(path: str | Path | None = None) -> dict[str, Any]:
    path = path or Path(__file__).parents[2] / "configs/minecraft/k12-live-containment-probe-v1.json"
    value = load_detached_artifact(path)
    if (value.get("identity") != LIVE_CONTAINMENT_PROBE_IDENTITY
            or value.get("probes") != list(CONTAINMENT_PROBES)
            or value.get("scope") != "campaign"
            or value.get("executor") != "injected_only"
            or value.get("network") is not False
            or value.get("rcon") is not False):
        raise ValueError("containment probe manifest mismatch")
    return value

@dataclass(frozen=True, slots=True)
class LiveFinalWrapper:
    schedule_count: int
    profile_digest: str
    campaign_id: str
    identity: str = LIVE_FINAL_WRAPPER_IDENTITY
    digest: str = field(init=False)
    def __post_init__(self):
        if self.identity != LIVE_FINAL_WRAPPER_IDENTITY: raise ValueError("wrong final wrapper")
        object.__setattr__(self, "digest", canonical_sha256({"identity": self.identity,
            "schedule_count": self.schedule_count, "profile_digest": self.profile_digest,
            "campaign_id": self.campaign_id}))

@dataclass(frozen=True, slots=True)
class FinalGateInput:
    wrapper: LiveFinalWrapper
    profile_digest: str
    campaign_id: str
    qualification: QualificationAggregate
    probes: ProbeAggregate
    manifests_clean: bool
    schedule_count: int = 90
    retry: bool = False
    resumed: bool = False
    replacement: bool = False
    authenticated_digest: str = field(init=False)
    def __post_init__(self):
        object.__setattr__(self, "authenticated_digest", canonical_sha256({"wrapper": self.wrapper.digest,
            "profile_digest": self.profile_digest, "campaign_id": self.campaign_id,
            "qualification": self.qualification.identity, "probes": self.probes.identity,
            "manifests_clean": self.manifests_clean, "schedule_count": self.schedule_count,
            "retry": self.retry, "resumed": self.resumed, "replacement": self.replacement}))

def final_launch_gate(value: FinalGateInput) -> bool:
    if not isinstance(value, FinalGateInput): return False
    expected = canonical_sha256({"wrapper": value.wrapper.digest, "profile_digest": value.profile_digest,
        "campaign_id": value.campaign_id, "qualification": value.qualification.identity,
        "probes": value.probes.identity, "manifests_clean": value.manifests_clean,
        "schedule_count": value.schedule_count, "retry": value.retry,
        "resumed": value.resumed, "replacement": value.replacement})
    return (value.authenticated_digest == expected and value.wrapper.identity == LIVE_FINAL_WRAPPER_IDENTITY
            and value.wrapper.digest == LiveFinalWrapper(value.schedule_count, value.profile_digest, value.campaign_id).digest
            and value.schedule_count == 90 and value.profile_digest == value.wrapper.profile_digest
            and value.campaign_id == value.wrapper.campaign_id and value.qualification.qualifies()
            and value.qualification.profile_digest == value.profile_digest
            and value.probes.passed and value.probes.probes == CONTAINMENT_PROBES
            and value.probes.profile_digest == value.profile_digest
            and all(result.trace.execution_provenance == "live_qualified"
                    for result in value.qualification.results)
            and value.probes.execution_provenance == "live_qualified"
            and len({value.campaign_id, value.qualification.campaign_id,
                     value.probes.campaign_id}) == 3
            and value.manifests_clean and not value.retry and not value.resumed and not value.replacement)

def validate_pid_tree(*, main_pid: int, main_start: int, observed_identity: tuple[int, int],
                      session_leader: int, descendants: Any) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    if (type(main_pid) is not int or main_pid <= 0 or type(main_start) is not int or main_start < 0
            or observed_identity != (main_pid, main_start)): reasons.append("pid_start_identity_changed")
    if type(session_leader) is not int or session_leader != main_pid: reasons.append("setsid_leader_mismatch")
    if not (isinstance(descendants, (list, tuple)) and all(type(pid) is int and pid > 0 for pid in descendants)):
        reasons.append("invalid_descendants")
    return not reasons, tuple(reasons)
