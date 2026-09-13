"""The exact, authenticated fifteen-cell K12Q qualification."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .k12_live_artifacts import DomainIdentity, LiveArtifact, freeze, thaw
from .k12_runtime_profile import (LIVE_QUALIFICATION_IDENTITY, LIVE_SCHEDULE_IDENTITY,
                                    detached_digest, load_k12_live_runtime_profile,
                                    load_k12_live_qualification_manifest, strict_json_load)
from benchmarks.common.eac.canonical import canonical_argument, canonical_sha256

PROBES = ("P1", "P2", "P3", "P4")
STRATA = ("S1", "S2", "S3", "S4", "S5")
QUALIFICATION_PATH = Path(__file__).with_name("k12_live_qualification_v1.json")
_QUALIFICATION_TOKEN = object()

def qualification_ids() -> tuple[str, ...]:
    from .k12_live_fixture import qualification_ids as fixture_ids
    return tuple(fixture_ids())

def qualification_cells() -> tuple[dict[str, Any], ...]:
    return tuple({"cell_id": i, "stratum": i.split("-")[1], "arm": i.rsplit("-", 1)[1],
                  "status": "not_started"} for i in qualification_ids())

def qualification_artifact() -> LiveArtifact:
    profile = load_k12_live_runtime_profile()
    return LiveArtifact("qualification", tuple(qualification_cells()),
                        DomainIdentity(profile["detached_artifact_sha256"],
                                       LIVE_QUALIFICATION_IDENTITY))

def qualification_matrix() -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(c["cell_id"] for c in qualification_cells() if c["stratum"] == s)
                 for s in STRATA)

@dataclass(frozen=True, slots=True, init=False)
class QualificationTrace:
    cell_id: str; events: Any; profile_digest: str; campaign_id: str
    reset_identity: str = ""; evidence_digest: str = ""
    execution_provenance: str = field(init=False, default="mock_only")
    identity: str = field(init=False)
    def __init__(self,cell_id,events,profile_digest,campaign_id,reset_identity,evidence_digest,token=None):
        if token is not _QUALIFICATION_TOKEN: raise TypeError("qualification traces are coordinator-minted")
        for name,value in (("cell_id",cell_id),("events",events),("profile_digest",profile_digest),("campaign_id",campaign_id),("reset_identity",reset_identity),("evidence_digest",evidence_digest)): object.__setattr__(self,name,value)
        object.__setattr__(self,"execution_provenance","mock_only")
        self.__post_init__()
    def __post_init__(self) -> None:
        if (self.cell_id not in qualification_ids()
                or not all(isinstance(value, str) and value for value in
                           (self.profile_digest, self.campaign_id,
                            self.reset_identity, self.evidence_digest))):
            raise ValueError("complete qualification trace binding is required")
        object.__setattr__(self, "events", canonical_argument(self.events))
        object.__setattr__(self, "identity", canonical_sha256({"artifact": "minecraft-k12-live-qualification-trace/1", "cell_id": self.cell_id,
            "events": thaw(self.events), "profile": self.profile_digest,
            "campaign": self.campaign_id, "reset": self.reset_identity,
            "evidence": self.evidence_digest}))

@dataclass(frozen=True, slots=True, init=False)
class QualificationCellResult:
    cell_id: str; passed: bool; fresh_root: bool; retry: bool; trace: QualificationTrace
    resumed: bool = False; replacement: bool = False
    identity: str = field(init=False)
    def __init__(self,cell_id,passed,fresh_root,retry,trace,resumed=False,replacement=False,token=None):
        if token is not _QUALIFICATION_TOKEN: raise TypeError("qualification results are coordinator-minted")
        for name,value in (("cell_id",cell_id),("passed",passed),("fresh_root",fresh_root),("retry",retry),("trace",trace),("resumed",resumed),("replacement",replacement)): object.__setattr__(self,name,value)
        self.__post_init__()
    def __post_init__(self) -> None:
        if self.trace.cell_id != self.cell_id:
            raise ValueError("qualification result/trace cell mismatch")
        if self.retry or self.resumed or self.replacement or not self.fresh_root: object.__setattr__(self, "passed", False)
        object.__setattr__(self, "identity", canonical_sha256({"artifact": "minecraft-k12-live-qualification-cell-result/1", "cell_id": self.cell_id,
            "passed": self.passed, "fresh_root": self.fresh_root, "retry": self.retry,
            "resumed": self.resumed, "replacement": self.replacement, "trace": self.trace.identity}))

@dataclass(frozen=True, slots=True, init=False)
class ProbeAggregate:
    probes: tuple[str, ...]; passed: bool; profile_digest: str; campaign_id: str; evidence_digest: str
    execution_provenance: str = field(init=False, default="mock_only")
    identity: str = field(init=False)
    def __init__(self,probes,passed,profile_digest,campaign_id,evidence_digest,token=None):
        if token is not _QUALIFICATION_TOKEN: raise TypeError("probe aggregates are coordinator-minted")
        for name,value in (("probes",tuple(probes)),("passed",passed),("profile_digest",profile_digest),("campaign_id",campaign_id),("evidence_digest",evidence_digest)): object.__setattr__(self,name,value)
        object.__setattr__(self,"execution_provenance","mock_only")
        self.__post_init__()
    def __post_init__(self):
        if (self.probes != PROBES or not all(isinstance(value, str) and value for value in
                (self.profile_digest, self.campaign_id, self.evidence_digest))):
            raise ValueError("P1-P4 aggregate is exhaustive and bound")
        object.__setattr__(self, "identity", canonical_sha256({"artifact": "minecraft-k12-live-containment-probe/1", "probes": list(self.probes), "passed": self.passed,
            "profile": self.profile_digest, "campaign": self.campaign_id, "evidence": self.evidence_digest}))

@dataclass(frozen=True, slots=True, init=False)
class QualificationAggregate:
    results: tuple[QualificationCellResult, ...]; profile_digest: str; campaign_id: str
    identity: str = field(init=False)
    def __init__(self,results,profile_digest,campaign_id,token=None):
        if token is not _QUALIFICATION_TOKEN: raise TypeError("qualification aggregates are coordinator-minted")
        object.__setattr__(self,"results",tuple(results)); object.__setattr__(self,"profile_digest",profile_digest); object.__setattr__(self,"campaign_id",campaign_id); self.__post_init__()
    def __post_init__(self):
        object.__setattr__(self, "results", tuple(self.results))
        object.__setattr__(self, "identity", canonical_sha256({"artifact": "minecraft-k12-live-qualification-aggregate/1", "results": [r.identity for r in self.results],
            "profile": self.profile_digest, "campaign": self.campaign_id}))
    def qualifies(self) -> bool:
        return (len(self.results) == 15 and tuple(r.cell_id for r in self.results) == qualification_ids()
                and len({r.identity for r in self.results}) == 15
                and len({r.trace.identity for r in self.results}) == 15
                and all(r.passed and r.fresh_root and not r.retry and not r.resumed and not r.replacement
                        and r.trace.profile_digest == self.profile_digest and r.trace.campaign_id == self.campaign_id
                        and r.trace.execution_provenance == "mock_only"
                        for r in self.results))
    @property
    def schedule_identity(self) -> str: return LIVE_SCHEDULE_IDENTITY
    @property
    def qualification_identity(self) -> str: return LIVE_QUALIFICATION_IDENTITY

@dataclass(frozen=True, slots=True)
class MockCellQualificationEvidence:
    cell_id: str; events: Any; profile_digest: str; campaign_id: str
    reset_identity: str; evidence_digest: str; fresh_root: bool
    reset_passed: bool; capability_state: str; provider_terminal: str
    oracle_value: str; containment_clean: bool; evidence_valid: bool
    retry: bool = False; resumed: bool = False; replacement: bool = False

def qualify_mock_cell(value: MockCellQualificationEvidence) -> QualificationCellResult:
    if not isinstance(value,MockCellQualificationEvidence): raise TypeError("typed mock cell evidence required")
    arm=value.cell_id.rsplit("-",1)[-1]
    expected_oracle="not_applicable" if arm=="S" else "true"
    passed=(value.cell_id in qualification_ids() and value.fresh_root and value.reset_passed
            and value.capability_state=="REVOKED" and value.provider_terminal=="success"
            and value.oracle_value==expected_oracle and value.containment_clean and value.evidence_valid
            and not value.retry and not value.resumed and not value.replacement)
    trace=QualificationTrace(value.cell_id,value.events,value.profile_digest,value.campaign_id,
        value.reset_identity,value.evidence_digest,_QUALIFICATION_TOKEN)
    return QualificationCellResult(value.cell_id,passed,value.fresh_root,value.retry,trace,
        value.resumed,value.replacement,_QUALIFICATION_TOKEN)

def qualify_mock_campaign(values: tuple[MockCellQualificationEvidence,...]) -> QualificationAggregate:
    if type(values) is not tuple or tuple(v.cell_id for v in values)!=qualification_ids():
        raise ValueError("exact qualification evidence order required")
    results=tuple(qualify_mock_cell(value) for value in values)
    profiles={value.profile_digest for value in values}; campaigns={value.campaign_id for value in values}
    if len(profiles)!=1 or len(campaigns)!=1: raise ValueError("qualification evidence binding mismatch")
    return QualificationAggregate(results,profiles.pop(),campaigns.pop(),_QUALIFICATION_TOKEN)

@dataclass(frozen=True, slots=True)
class MockProbeEvidence:
    probe: str; passed: bool; profile_digest: str; campaign_id: str; evidence_digest: str

def qualify_mock_probes(values: tuple[MockProbeEvidence,...]) -> ProbeAggregate:
    if type(values) is not tuple or tuple(value.probe for value in values)!=PROBES:
        raise ValueError("exact P1-P4 evidence required")
    profiles={value.profile_digest for value in values}; campaigns={value.campaign_id for value in values}
    if len(profiles)!=1 or len(campaigns)!=1 or any(not value.evidence_digest for value in values):
        raise ValueError("probe evidence binding mismatch")
    evidence=canonical_sha256({"probe_evidence":[value.evidence_digest for value in values]})
    return ProbeAggregate(PROBES,all(value.passed for value in values),profiles.pop(),campaigns.pop(),evidence,_QUALIFICATION_TOKEN)

def load_k12_live_qualification(path: str | Path = QUALIFICATION_PATH) -> dict[str, Any]:
    value = strict_json_load(Path(path), "K12 live qualification")
    if not isinstance(value, dict) or value.get("detached_artifact_sha256") != detached_digest(value):
        raise ValueError("qualification detached digest mismatch")
    if value.get("schedule") != list(qualification_ids()) or value.get("cell_count") != 15:
        raise ValueError("qualification schedule mismatch")
    return value

final_gate_accepts = lambda artifact: False

__all__=["PROBES","STRATA","MockCellQualificationEvidence","MockProbeEvidence",
    "QualificationTrace","QualificationCellResult","QualificationAggregate","ProbeAggregate",
    "qualification_ids","qualification_cells","qualification_artifact","qualification_matrix",
    "qualify_mock_cell","qualify_mock_campaign","qualify_mock_probes",
    "load_k12_live_qualification","final_gate_accepts"]
