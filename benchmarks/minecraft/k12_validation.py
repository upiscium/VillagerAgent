"""Fail-closed validation of the parent-owned K12 trace."""
from __future__ import annotations
import json

from dataclasses import MISSING, dataclass, field, fields
from enum import Enum
from typing import Any, Iterable, Mapping

from benchmarks.common.eac.canonical import canonical_sha256
from benchmarks.common.eac.canonical import thaw_json
from .k12_parent_events import ParentEvent
from .k12_trace import TraceRecord, TRACE_SCHEMA, GENESIS_DIGEST, message_digest
from .k12_evidence import EvidenceSnapshot
from .k12_authority_adapter import AuthorityRejectionV1
from .eac_runtime import RUNTIME_ID
from .k12_identity import REQUEST_CONTENT_SCHEMA
from .k12_fixture import STRATUM_ACTIONS
from .k12_request import request_content_digest
from .k12_worker_protocol import parse_recovery_proposal


class Disposition(str, Enum):
    RECOVERED = "RECOVERED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    REPEATED_ORIGINAL_REQUEST = "REPEATED_ORIGINAL_REQUEST"
    RECOVERY_SAFETY_FAILURE = "RECOVERY_SAFETY_FAILURE"
    UNRECOVERABLE = "UNRECOVERABLE"
    STOPPED_AFTER_REJECTION = "STOPPED_AFTER_REJECTION"
    A_TERMINAL = "A_TERMINAL"
    INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE"
    TRACE_INVALID = "TRACE_INVALID"
    RESET_INVALID = "RESET_INVALID"


TERMINAL_DISPOSITIONS = tuple(d.value for d in Disposition)
_COMMON = ("reset_attested", "cell_started", "prepared_request_frozen", "invalidation_ingested")
_SUFFIX = ("objective_oracle_evaluated", "process_finalized", "cell_terminal")
_ID_KEYS = ("request_id", "candidate_id", "attempt_id", "permit_id", "effect_id", "evidence_root_id")


class K12ValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True, init=False)
class K12CellResult:
    cell_id: str
    triplet_id: str
    disposition: Disposition
    arm: str
    valid: bool
    reasons: tuple[str, ...]
    utility: tuple[int, ...] = ()
    protocol_digest: str = ""
    trace_digest: str = ""
    reset_generation: int = 0
    budgets: tuple[tuple[str, int], ...] = ()
    objective_completed: bool = False
    campaign_id: str = ""
    cohort_id: str = ""
    campaign_seed: str = ""
    evidence_snapshot_digest: str = ""
    artifact_id: str = ""
    artifact_version: int = 0
    fixture_manifest_digest: str = ""
    randomization_manifest_digest: str = ""
    validation_contract_identity: str = ""
    validation_contract_digest: str = ""
    schedule_identity: str = ""
    schedule_count: int = 0
    source_events: tuple[ParentEvent | TraceRecord, ...] = field(default=(), init=False, repr=False, compare=False)
    source_evidence: EvidenceSnapshot | None = field(default=None, init=False, repr=False, compare=False)
    source_protocol_digest: str = field(default="", init=False, repr=False, compare=False)
    source_campaign_id: str = field(default="", init=False, repr=False, compare=False)
    source_cohort_id: str = field(default="", init=False, repr=False, compare=False)
    source_campaign_seed: str = field(default="", init=False, repr=False, compare=False)

    @classmethod
    def _mint(cls, cell_id: str, triplet_id: str, disposition: Disposition, arm: str,
              valid: bool, reasons: tuple[str, ...], utility: tuple[int, ...] = (),
              **values: Any) -> "K12CellResult":
        result = object.__new__(cls)
        supplied = {"cell_id": cell_id, "triplet_id": triplet_id,
                    "disposition": disposition, "arm": arm, "valid": valid,
                    "reasons": reasons, "utility": utility, **values}
        for descriptor in fields(cls):
            if descriptor.name.startswith("source_"):
                value = None if descriptor.name == "source_evidence" else () if descriptor.name == "source_events" else ""
            elif descriptor.name in supplied:
                value = supplied[descriptor.name]
            elif descriptor.default is not MISSING:
                value = descriptor.default
            else:
                raise K12ValidationError(f"missing cell result field: {descriptor.name}")
            object.__setattr__(result, descriptor.name, value)
        return result

    def _replace(self, **changes: Any) -> "K12CellResult":
        unknown = set(changes) - {descriptor.name for descriptor in fields(self)}
        if unknown:
            raise K12ValidationError("unknown cell result fields")
        result = object.__new__(type(self))
        for descriptor in fields(self):
            object.__setattr__(result, descriptor.name,
                               changes.get(descriptor.name, getattr(self, descriptor.name)))
        return result

    @property
    def authenticated(self) -> bool:
        return bool(self.source_events and self.source_evidence is not None)

    @property
    def identity(self) -> str:
        self.require_public_authentication()
        return canonical_sha256({"schema": "minecraft-k12-recovery-cell-result/1", "cell_id": self.cell_id,
            "triplet_id": self.triplet_id, "disposition": self.disposition.value, "arm": self.arm,
            "valid": self.valid, "reasons": list(self.reasons), "utility": list(self.utility),
            "protocol_digest": self.protocol_digest, "trace_digest": self.trace_digest,
            "reset_generation": self.reset_generation,
            "budgets": [[name, value] for name, value in self.budgets],
            "objective_completed": self.objective_completed,
            "campaign_id": self.campaign_id, "cohort_id": self.cohort_id,
            "campaign_seed": self.campaign_seed,
            "evidence_snapshot_digest": self.evidence_snapshot_digest,
            "artifact_id": self.artifact_id, "artifact_version": self.artifact_version,
            "fixture_manifest_digest": self.fixture_manifest_digest,
            "randomization_manifest_digest": self.randomization_manifest_digest,
            "validation_contract_identity": self.validation_contract_identity,
            "validation_contract_digest": self.validation_contract_digest,
            "schedule_identity": self.schedule_identity, "schedule_count": self.schedule_count})

    def require_public_authentication(self) -> None:
        from . import k12_protocol
        from .k12_identity import VALIDATION_CONTRACT_IDENTITY

        plain_digest = lambda value: (isinstance(value, str) and len(value) == 64
                                      and all(char in "0123456789abcdef" for char in value))
        prefixed_digest = lambda value: (isinstance(value, str) and value.startswith("sha256:")
                                         and plain_digest(value[7:]))
        if (self.artifact_id != "minecraft-k12-recovery-cell-result"
                or self.artifact_version != 1 or not plain_digest(self.protocol_digest)
                or not isinstance(self.campaign_seed, str) or not self.campaign_seed
                or not prefixed_digest(self.campaign_id) or not prefixed_digest(self.cohort_id)
                or not prefixed_digest(self.trace_digest) or not prefixed_digest(self.evidence_snapshot_digest)
                or not plain_digest(self.fixture_manifest_digest)
                or not plain_digest(self.randomization_manifest_digest)
                or self.validation_contract_identity != "minecraft-k12-recovery-validation-contract/1"
                or not plain_digest(self.validation_contract_digest)
                 or not prefixed_digest(self.schedule_identity) or self.schedule_count != 90):
            raise K12ValidationError("cell result public authentication is incomplete")
        schedule = tuple(cell.cell_id for cell in k12_protocol.build_k12_cells())
        identity_input = {"seed": self.campaign_seed, "protocol_digest": self.protocol_digest,
                          "cells": list(schedule)}
        contract = json.loads(k12_protocol.VALIDATION_CONTRACT_PATH.read_text(encoding="utf-8"))
        expected = (
            k12_protocol.load_k12_protocol()["validated_protocol_digest"],
            k12_protocol.load_fixture_manifest()["detached_artifact_sha256"],
            k12_protocol.load_randomization_manifest()["detached_artifact_sha256"],
            VALIDATION_CONTRACT_IDENTITY,
            k12_protocol.detached_digest(contract),
            canonical_sha256({"schedule": list(schedule)}),
        )
        actual = (self.protocol_digest, self.fixture_manifest_digest,
                  self.randomization_manifest_digest, self.validation_contract_identity,
                  self.validation_contract_digest, self.schedule_identity)
        if (actual != expected or self.schedule_count != len(schedule)
                or self.campaign_id != canonical_sha256(identity_input)
                or self.cohort_id != canonical_sha256({"campaign": identity_input})):
            raise K12ValidationError("cell result frozen authentication does not derive")
        scheduled = next((item for item in k12_protocol.build_k12_cells()
                          if item.cell_id == self.cell_id), None)
        if (scheduled is None or scheduled.triplet_id != self.triplet_id or scheduled.arm != self.arm
                or self.source_protocol_digest != self.protocol_digest
                or self.source_campaign_id != self.campaign_id
                or self.source_cohort_id != self.cohort_id
                or self.source_campaign_seed != self.campaign_seed
                or self.source_evidence is None or not self.source_events
                or self.trace_digest != self.source_events[-1].trace_digest
                or self.evidence_snapshot_digest != self.source_evidence.digest):
            raise K12ValidationError("cell result source authentication is inconsistent")
        replayed = validate_cell(
            self.source_events, cell_id=self.cell_id, triplet_id=self.triplet_id,
            expected_arm=self.arm, expected_reset_generation=self.reset_generation,
            evidence_snapshot=self.source_evidence,
            expected_protocol_digest=self.protocol_digest,
            expected_campaign_id=self.campaign_id, expected_cohort_id=self.cohort_id,
            expected_campaign_seed=self.campaign_seed)
        public_fields = tuple(descriptor.name for descriptor in fields(self)
                              if not descriptor.name.startswith("source_"))
        if any(getattr(replayed, name) != getattr(self, name) for name in public_fields):
            raise K12ValidationError("cell result does not replay from source authority")

    def as_dict(self) -> dict[str, Any]:
        self.require_public_authentication()
        return {
            "schema_version": "minecraft-k12-recovery-cell-result/1",
            "protocol_digest": self.protocol_digest,
            "cell_id": self.cell_id, "triplet_id": self.triplet_id, "arm": self.arm,
            "terminal_disposition": self.disposition.value, "trace_digest": self.trace_digest,
            "reset_generation": self.reset_generation,
            "budgets": dict(self.budgets),
            "validation": {"valid": self.valid, "reasons": list(self.reasons),
                           "finite_analysis_only": True},
            "utility": list(self.utility), "objective_completed": self.objective_completed,
            "campaign_id": self.campaign_id, "cohort_id": self.cohort_id,
            "campaign_seed": self.campaign_seed,
            "evidence_snapshot_digest": self.evidence_snapshot_digest,
            "artifact_id": self.artifact_id, "artifact_version": self.artifact_version,
            "fixture_manifest_digest": self.fixture_manifest_digest,
            "randomization_manifest_digest": self.randomization_manifest_digest,
            "validation_contract_identity": self.validation_contract_identity,
            "validation_contract_digest": self.validation_contract_digest,
            "schedule_identity": self.schedule_identity, "schedule_count": self.schedule_count,
        }


def _payload(event: ParentEvent | TraceRecord) -> Mapping[str, Any]:
    value = thaw_json(event.payload)
    if not isinstance(value, Mapping):
        raise K12ValidationError("event payload must be an object")
    return value


def _invalid(cell_id: str, triplet_id: str, arm: str, reason: str) -> K12CellResult:
    return K12CellResult._mint(cell_id, triplet_id, Disposition.TRACE_INVALID, arm, False, (reason,))


def _paired(kinds: tuple[str, ...], start: str, end: str) -> bool:
    """Require every lifecycle admission to have exactly one matching terminal."""
    lifecycle = tuple(kind for kind in kinds if kind in {start, end})
    return lifecycle == (start, end) * (len(lifecycle) // 2)


def _operation_fsm(items: tuple[ParentEvent | TraceRecord, ...]) -> bool:
    pairs = {
        "model_call_admitted": ("model", "start"), "model_call_terminal": ("model", "terminal"),
        "observation_started": ("observation", "start"), "observation_terminal": ("observation", "terminal"),
        "effect_entered": ("effect", "start"), "effect_terminal": ("effect", "terminal"),
        "recovery_step": ("recovery_step", "start"),
        "recovery_step_terminal": ("recovery_step", "terminal"),
        "evidence_ingested": ("evidence", "atomic"),
        "recovery_proposed": ("proposal", "start"),
        "proposal_validated": ("proposal", "terminal"),
    }
    active: tuple[str, str] | None = None
    seen: set[str] = set()
    worker_terminal_seen = False
    for event in items:
        if worker_terminal_seen and event.event not in {
                "objective_oracle_evaluated", "process_finalized", "cell_terminal"}:
            return False
        if event.event == "worker_terminal_candidate":
            worker_terminal_seen = True
        operation = pairs.get(event.event)
        if operation is None:
            if active is not None and event.event in {"recovery_proposed", "worker_terminal_candidate",
                                                       "process_finalized", "cell_terminal"}:
                return False
            continue
        kind, state = operation
        payload = _payload(event)
        identifier = (event.message_digest if event.event == "recovery_proposed"
                      else payload.get("proposal_message_digest") if event.event == "proposal_validated"
                      else payload.get("operation_id"))
        if not isinstance(identifier, str) or not identifier:
            return False
        if state == "start":
            if active is not None or identifier in seen:
                return False
            active = (kind, identifier); seen.add(identifier)
        elif state == "terminal":
            if active != (kind, identifier):
                return False
            active = None
        elif active is not None or identifier in seen:
            return False
        else:
            seen.add(identifier)
    return active is None


def _parent_counters(finalized: Mapping[str, Any], kinds: tuple[str, ...]) -> bool:
    expected = {
        "budget_steps": kinds.count("recovery_step"),
        "budget_model_calls": kinds.count("model_call_admitted"),
        "budget_evidence_calls": (kinds.count("rejection_observation_emitted")
                                   + kinds.count("observation_started")),
        "budget_effect_attempts": kinds.count("effect_entered"),
    }
    return all(type(finalized.get(key)) is int and finalized.get(key) == value
               for key, value in expected.items())


def _validate_cell(raw_events: Iterable[ParentEvent | TraceRecord], *, cell_id: str = "",
                  triplet_id: str = "", expected_arm: str | None = None,
                  expected_budget: int | None = None, expected_reset_generation: int | None = None,
                  evidence_snapshot: EvidenceSnapshot | None = None,
                  expected_protocol_digest: str | None = None,
                  expected_campaign_id: str | None = None,
                  expected_cohort_id: str | None = None) -> K12CellResult:
    items = tuple(raw_events)
    if not items or any(not isinstance(e, (ParentEvent, TraceRecord)) for e in items):
        raise K12ValidationError("authenticated ParentEvent/TraceRecord stream required")
    first = items[0]
    cell_id = cell_id or first.cell_id
    triplet_id = triplet_id or first.triplet_id
    arm = expected_arm or first.arm
    if not cell_id or not triplet_id or arm not in {"A", "R", "S"}:
        return _invalid(cell_id, triplet_id, arm, "incomplete cell identity")
    if any((e.schema != TRACE_SCHEMA or e.cell_id != cell_id or e.triplet_id != triplet_id or e.arm != arm)
           for e in items):
        return _invalid(cell_id, triplet_id, arm, "event cell/triplet/arm identity mismatch")
    if (not first.worker_id or any(event.source != "minecraft-k12-parent"
                                   or event.worker_id != first.worker_id for event in items)):
        return _invalid(cell_id, triplet_id, arm, "parent provenance or worker identity mismatch")
    previous = GENESIS_DIGEST
    for index, event in enumerate(items):
        digest = event.trace_digest if isinstance(event, ParentEvent) else event.digest
        valid_digest = digest == canonical_sha256(thaw_json(event.unsigned()))
        if event.sequence != index or event.previous_digest != previous or not valid_digest:
            return _invalid(cell_id, triplet_id, arm, "trace order or digest is invalid")
        previous = digest
    if evidence_snapshot is not None:
        if (not isinstance(evidence_snapshot, EvidenceSnapshot) or not evidence_snapshot.verify()
                or any(record.binding.cell != cell_id or record.binding.triplet != triplet_id
                       or record.binding.arm != arm for record in evidence_snapshot.records)):
            return _invalid(cell_id, triplet_id, arm, "evidence snapshot binding is invalid")
        snapshot_binding = evidence_snapshot.records[0].binding if evidence_snapshot.records else None
        if (snapshot_binding is None
                or (expected_protocol_digest is not None
                    and snapshot_binding.protocol != expected_protocol_digest)
                or (expected_campaign_id is not None
                    and snapshot_binding.campaign != expected_campaign_id)
                or (expected_cohort_id is not None
                    and snapshot_binding.cohort != expected_cohort_id)):
            return _invalid(cell_id, triplet_id, arm, "evidence campaign binding is invalid")
    parent_authority = {"current_inadmissible_confirmed", "authority_rejected",
                        "proposal_validated", "new_request_prepared", "permit_issued",
                        "effect_decision", "effect_entered", "effect_terminal"}
    for event in items:
        if arm != "A" and event.event in parent_authority and event.message_digest != message_digest({
                "parent_event": event.event, "cell_id": event.cell_id,
                "triplet_id": event.triplet_id, "arm": event.arm,
                "payload": event.payload}):
            return _invalid(cell_id, triplet_id, arm, "worker-authored authority event")
    kinds = tuple(e.event for e in items)
    if evidence_snapshot is not None:
        rejection_records = tuple(record for record in evidence_snapshot.records
                                  if record.kind == "authority_rejection")
        if ((arm in {"R", "S"} and len(rejection_records) != 1)
                or (arm == "A" and rejection_records)):
            return _invalid(cell_id, triplet_id, arm,
                            "authority rejection evidence cardinality is invalid")
        if len(tuple(record for record in evidence_snapshot.records
                     if record.kind == "finalization")) != 1:
            return _invalid(cell_id, triplet_id, arm,
                            "finalization evidence cardinality is invalid")
    process_events = [event for event in items if event.event == "process_finalized"]
    raw_finalized = _payload(process_events[0]) if len(process_events) == 1 else {}
    finalized: Mapping[str, Any] = raw_finalized
    if evidence_snapshot is not None and raw_finalized.get("finalization_evidence_id") is not None:
        try:
            finalization_record = evidence_snapshot.require(
                "finalization", raw_finalized.get("finalization_evidence_id"))
            finalized = thaw_json(finalization_record.payload)
        except (KeyError, TypeError, ValueError):
            return _invalid(cell_id, triplet_id, arm, "finalization evidence is unresolved")
        if raw_finalized.get("evidence_snapshot_digest") != evidence_snapshot.digest:
            return _invalid(cell_id, triplet_id, arm, "finalization snapshot binding is invalid")
    if len(process_events) == 1 and kinds[-1:] == ("cell_terminal",):
        failure = finalized
        if failure.get("reset_invalid") is True:
            return K12CellResult._mint(cell_id, triplet_id, Disposition.RESET_INVALID, arm, False,
                                 (str(failure.get("reason", "reset authority failure")),))
        if failure.get("runtime_failure") is True or failure.get("containment_failure") is True:
            if (not _operation_fsm(items)
                    or any(_payload(event).get("outcome") == "unknown" for event in items
                           if event.event == "effect_terminal")):
                return _invalid(cell_id, triplet_id, arm,
                                "infrastructure failure has unresolved operation lifecycle")
            try:
                failed_reset = evidence_snapshot.require("reset", failure.get("reset_evidence_id"))
                failed_containment = evidence_snapshot.require(
                    "containment", failure.get("containment_evidence_id"))
                failed_containment_payload = thaw_json(failed_containment.payload)
            except (AttributeError, KeyError, TypeError, ValueError):
                return _invalid(cell_id, triplet_id, arm,
                                "infrastructure failure evidence is unresolved")
            if (raw_finalized.get("evidence_snapshot_digest") != evidence_snapshot.digest
                    or failed_containment_payload.get("reset_evidence_id") != failed_reset.id
                    or (failure.get("containment_failure") is True
                        and failed_containment_payload.get("containment_failure") is not True)):
                return _invalid(cell_id, triplet_id, arm,
                                "infrastructure failure evidence binding is invalid")
            return K12CellResult._mint(cell_id, triplet_id, Disposition.INFRASTRUCTURE_FAILURE, arm, False,
                                 (str(failure.get("reason", "infrastructure failure")),))
        if failure.get("budget_exhausted") is True:
            reset = _payload(items[0])
            try:
                budget_reset = evidence_snapshot.require("reset", failure.get("reset_evidence_id"))
                budget_containment = evidence_snapshot.require(
                    "containment", failure.get("containment_evidence_id"))
            except (AttributeError, KeyError, TypeError, ValueError):
                return _invalid(cell_id, triplet_id, arm, "invalid parent budget evidence")
            budget_reset_payload = thaw_json(budget_reset.payload)
            containment_payload = thaw_json(budget_containment.payload)
            raw = {
                "budget_steps": kinds.count("recovery_step"),
                "budget_model_calls": kinds.count("model_call_admitted"),
                "budget_evidence_calls": (kinds.count("rejection_observation_emitted")
                                          + kinds.count("observation_started")),
                "budget_effect_attempts": kinds.count("effect_entered"),
            }
            at_cap = (failure.get("budget_steps") == 4
                      or failure.get("budget_model_calls") == 2
                      or failure.get("budget_evidence_calls") == 3
                      or failure.get("budget_effect_attempts") == 2
                      or failure.get("budget_deadline_reached") is True)
            pre_budget = kinds[1:kinds.index("budget_reached")] if "budget_reached" in kinds else ()
            r_prefix = ("cell_started", "prepared_request_frozen", "invalidation_ingested",
                        "current_inadmissible_confirmed", "authority_rejected",
                        "rejection_observation_emitted", "recovery_started")
            activity = pre_budget[len(r_prefix):] if pre_budget[:len(r_prefix)] == r_prefix else ()
            normal_prefix = pre_budget[:3] == _COMMON[1:]
            r_activity_valid = (
                 set(activity) <= {"recovery_step", "model_call_admitted",
                                   "model_call_terminal", "observation_started",
                                   "observation_terminal", "evidence_ingested", "recovery_step_terminal",
                                   "recovery_proposed", "proposal_validated",
                                   "new_request_prepared"}
                and _paired(pre_budget, "model_call_admitted", "model_call_terminal")
                and _paired(pre_budget, "observation_started", "observation_terminal")
                and _paired(pre_budget, "effect_entered", "effect_terminal")
            )
            deadline_terminal = failure.get("budget_deadline_reached") is True
            arm_valid = (arm in {"A", "R", "S"} and (
                (deadline_terminal and (not pre_budget or normal_prefix))
                or (normal_prefix and arm == "R"
                    and pre_budget[:len(r_prefix)] == r_prefix
                    and bool(activity) and r_activity_valid)
            ))
            valid = (reset.get("status") == "verified" and kinds.count("budget_reached") == 1
                     and kinds[-2:] == ("process_finalized", "cell_terminal")
                     and all(failure.get(key) == value for key, value in raw.items()
                             if key != "budget_effect_attempts")
                     and type(failure.get("budget_effect_attempts")) is int
                     and raw["budget_effect_attempts"] <= failure["budget_effect_attempts"] <= 2
                     and at_cap and arm_valid and _operation_fsm(items)
                      and raw_finalized.get("evidence_snapshot_digest") == evidence_snapshot.digest
                     and budget_reset.binding.generation == reset.get("generation")
                     and budget_reset.binding.reset_token == reset.get("token_id")
                     and budget_reset.binding.attestation == reset.get("attestation_digest")
                     and budget_reset_payload.get("generation") == reset.get("generation")
                     and budget_reset_payload.get("token_id") == reset.get("token_id")
                     and budget_reset_payload.get("attestation_digest") == reset.get("attestation_digest")
                     and budget_reset_payload.get("initial_state_digest") == reset.get("initial_state_digest")
                     and containment_payload.get("cgroup_empty") is True
                     and containment_payload.get("blocked_next_launch") is False
                     and containment_payload.get("reset_evidence_id") == budget_reset.id)
            return K12CellResult._mint(
                cell_id, triplet_id,
                Disposition.BUDGET_EXHAUSTED if valid else Disposition.TRACE_INVALID,
                arm, valid, () if valid else ("invalid parent budget finalization",),
            )
    utility = (items[-1].received_monotonic_ns - items[0].received_monotonic_ns,
               kinds.count("model_call_admitted"),
               kinds.count("rejection_observation_emitted") + kinds.count("observation_started"),
               kinds.count("effect_entered"))
    if evidence_snapshot is None:
        return _invalid(cell_id, triplet_id, arm, "immutable parent evidence snapshot is required")
    binding = evidence_snapshot.records[0].binding if evidence_snapshot.records else None
    if (binding is None
            or (expected_protocol_digest is not None and binding.protocol != expected_protocol_digest)
            or (expected_campaign_id is not None and binding.campaign != expected_campaign_id)
            or (expected_cohort_id is not None and binding.cohort != expected_cohort_id)):
        return _invalid(cell_id, triplet_id, arm, "evidence campaign binding is invalid")
    counts = {kind: kinds.count(kind) for kind in
              ("budget_reached", "worker_terminal_candidate", "objective_oracle_evaluated",
               "process_finalized", "cell_terminal")}
    if (counts["worker_terminal_candidate"] != 1
            or counts["objective_oracle_evaluated"] != 1
            or counts["process_finalized"] != 1
            or counts["cell_terminal"] != 1
            or counts["budget_reached"] > 1):
        return _invalid(cell_id, triplet_id, arm, "terminal event cardinality is invalid")
    if not (_paired(kinds, "model_call_admitted", "model_call_terminal")
            and _paired(kinds, "observation_started", "observation_terminal")
            and _paired(kinds, "effect_entered", "effect_terminal")
            and _operation_fsm(items)):
        return _invalid(cell_id, triplet_id, arm, "lifecycle admission is not terminally paired")
    entered_effects = [_payload(e).get("operation_id", _payload(e).get("effect_id"))
                       for e in items if e.event == "effect_entered"]
    terminal_effects = [_payload(e).get("operation_id", _payload(e).get("effect_id"))
                        for e in items if e.event == "effect_terminal"]
    if (any(not isinstance(operation_id, str) or not operation_id for operation_id in entered_effects)
            or entered_effects != terminal_effects or len(set(entered_effects)) != len(entered_effects)
            or any(_payload(e).get("outcome") == "unknown"
                   for e in items if e.event == "effect_terminal")):
        return _invalid(cell_id, triplet_id, arm, "effect entered without an effect identity")
    if not _parent_counters(finalized, kinds):
        return _invalid(cell_id, triplet_id, arm, "parent budget counters do not reconcile")
    reset_data = _payload(items[0])
    supplied_generations = [
        _payload(event).get("generation") for event in items
        if isinstance(event, ParentEvent) and "generation" in _payload(event)
    ]
    if any(type(generation) is not int for generation in supplied_generations):
        return _invalid(cell_id, triplet_id, arm, "invalid reset generation")
    if supplied_generations and any(generation != supplied_generations[0] for generation in supplied_generations):
        return _invalid(cell_id, triplet_id, arm, "reset generation mismatch")
    if expected_reset_generation is not None and (
            not supplied_generations or supplied_generations[0] != expected_reset_generation):
        return _invalid(cell_id, triplet_id, arm, "reset generation mismatch")
    if "budget_reached" in kinds:
        budget_index = kinds.index("budget_reached")
        pre_budget = kinds[len(_COMMON):budget_index]
        if arm == "A":
            arm_valid = pre_budget == ("advisory_would_block", "effect_decision",
                                       "effect_entered", "effect_terminal")
        elif arm == "R":
            r_prefix_budget = ("current_inadmissible_confirmed", "authority_rejected",
                               "rejection_observation_emitted", "recovery_started")
            activity = pre_budget
            activity = activity[len(r_prefix_budget):] if activity[:len(r_prefix_budget)] == r_prefix_budget else ()
            arm_valid = (pre_budget[:len(r_prefix_budget)] == r_prefix_budget and bool(activity)
                         and set(activity) <= {"recovery_step", "model_call_admitted", "model_call_terminal",
                                               "observation_started", "observation_terminal", "evidence_ingested"})
        else:
            arm_valid = False
        valid_budget = (
            reset_data.get("status") == "verified" and kinds[:len(_COMMON)] == _COMMON
            and arm_valid and budget_index + 1 < len(kinds)
            and _operation_fsm(items)
            and kinds[budget_index + 1] == "worker_terminal_candidate"
            and kinds[budget_index + 2:] == _SUFFIX
            and finalized.get("containment") == "verified"
            and utility[1] <= 2 and utility[2] <= 3 and utility[3] <= 2
            and (finalized.get("budget_steps") == 4
                 or finalized.get("budget_model_calls") == 2
                 or finalized.get("budget_evidence_calls") == 3
                 or finalized.get("budget_effect_attempts") == 2
                 or finalized.get("budget_deadline_reached") is True)
        )
        return K12CellResult._mint(
            cell_id, triplet_id,
            Disposition.BUDGET_EXHAUSTED if valid_budget else Disposition.TRACE_INVALID,
            arm, valid_budget, () if valid_budget else ("invalid budget terminal",), utility,
        )
    middle = kinds[len(_COMMON):-len(_SUFFIX)]
    expected_middle = {
        "A": ("advisory_would_block", "effect_decision", "effect_entered",
              "effect_terminal", "worker_terminal_candidate"),
        "S": ("current_inadmissible_confirmed", "authority_rejected",
              "worker_terminal_candidate"),
    }.get(arm)
    r_prefix = ("current_inadmissible_confirmed", "authority_rejected",
                "rejection_observation_emitted", "recovery_started")
    r_tail = ("recovery_proposed", "proposal_validated", "new_request_prepared", "permit_issued", "effect_decision",
              "effect_entered", "effect_terminal", "worker_terminal_candidate")
    repeated_tail = ("recovery_proposed", "proposal_validated", "worker_terminal_candidate")
    r_activity = middle[len(r_prefix):-len(r_tail)] if arm == "R" and len(middle) >= len(r_prefix) + len(r_tail) else ()
    activity_allowed = {"recovery_step", "recovery_step_terminal", "model_call_admitted", "model_call_terminal",
                        "observation_started", "observation_terminal", "evidence_ingested"}
    r_valid = (arm == "R" and middle[:len(r_prefix)] == r_prefix
               and middle[-len(r_tail):] == r_tail and bool(r_activity)
               and set(r_activity) <= activity_allowed
               and 1 <= r_activity.count("recovery_step") <= 4
               and r_activity.count("model_call_admitted") == r_activity.count("model_call_terminal") <= 2
               and r_activity.count("observation_started") == r_activity.count("observation_terminal") <= 3
               and r_activity.count("evidence_ingested") <= r_activity.count("observation_started")
               and 1 + r_activity.count("observation_started") <= 3)
    repeated_activity = (middle[len(r_prefix):-len(repeated_tail)]
                         if arm == "R" and len(middle) >= len(r_prefix) + len(repeated_tail)
                         else ())
    repeated_valid = (arm == "R" and middle[:len(r_prefix)] == r_prefix
                      and middle[-len(repeated_tail):] == repeated_tail
                      and bool(repeated_activity) and set(repeated_activity) <= activity_allowed
                      and 1 <= repeated_activity.count("recovery_step") <= 4
                      and repeated_activity.count("model_call_admitted") == repeated_activity.count("model_call_terminal") <= 2
                      and repeated_activity.count("observation_started") == repeated_activity.count("observation_terminal")
                      and repeated_activity.count("evidence_ingested") <= repeated_activity.count("observation_started")
                      and 1 + repeated_activity.count("observation_started") <= 3)
    if (kinds[:len(_COMMON)] != _COMMON or kinds[-len(_SUFFIX):] != _SUFFIX
            or (arm == "R" and not (r_valid or repeated_valid))
            or (arm != "R" and middle != expected_middle)):
        return _invalid(cell_id, triplet_id, arm, "exact event enum/order is invalid")
    candidate = items[kinds.index("worker_terminal_candidate")]
    oracle_events = [e for e in items if e.event == "objective_oracle_evaluated"]
    if len(oracle_events) != 1:
        return _invalid(cell_id, triplet_id, arm, "parent oracle event is missing or duplicated")
    oracle_data = _payload(oracle_events[0])
    final_payload = _payload(items[-1])
    data = _payload(candidate)
    reset_data = _payload(items[0])
    try:
        reset_record = evidence_snapshot.require("reset", finalized.get("reset_evidence_id"))
        oracle_record = evidence_snapshot.require("oracle", finalized.get("oracle_evidence_id"))
        containment_record = evidence_snapshot.require(
            "containment", finalized.get("containment_evidence_id"))
        effect_record = (evidence_snapshot.require("backend_effect", oracle_data["effect_evidence_id"])
                         if oracle_data.get("effect_evidence_id") is not None else None)
    except (KeyError, TypeError, ValueError):
        return _invalid(cell_id, triplet_id, arm, "parent evidence reference is unresolved")
    reset_evidence = thaw_json(reset_record.payload)
    oracle_evidence = thaw_json(oracle_record.payload)
    containment_evidence = thaw_json(containment_record.payload)
    effect_evidence = thaw_json(effect_record.payload) if effect_record is not None else None
    model_ids = finalized.get("model_call_evidence_ids")
    if not isinstance(model_ids, (list, tuple)) or len(model_ids) != kinds.count("model_call_terminal"):
        return _invalid(cell_id, triplet_id, arm, "model-call evidence accounting mismatch")
    model_events = [event for event in items if event.event in {
        "model_call_admitted", "model_call_terminal"}]
    try:
        model_records = [thaw_json(evidence_snapshot.require("model_call", item).payload)
                         for item in model_ids]
    except (KeyError, TypeError, ValueError):
        return _invalid(cell_id, triplet_id, arm, "model-call evidence reference is unresolved")
    for record in model_records:
        operation_id = record.get("operation_id")
        admitted = next((event for event in model_events
                         if event.event == "model_call_admitted"
                         and _payload(event).get("operation_id") == operation_id), None)
        terminal_model = next((event for event in model_events
                               if event.event == "model_call_terminal"
                               and _payload(event).get("operation_id") == operation_id), None)
        if (admitted is None or terminal_model is None
                or record.get("start_message_digest") != admitted.message_digest
                or record.get("terminal_message_digest") != terminal_model.message_digest):
            return _invalid(cell_id, triplet_id, arm, "model-call evidence binding is inconsistent")
    coordinates = cell_id.split("-")
    prepared_original = _payload(items[kinds.index("prepared_request_frozen")])
    registry_original_request = (thaw_json(rejection_records[0].payload).get("request_digest")
                                 if arm in {"R", "S"} and len(rejection_records) == 1 else None)
    expected_oracle_request = (finalized.get("recovery_request_id")
                               or registry_original_request
                               or prepared_original.get("request_id"))
    if (raw_finalized.get("evidence_snapshot_digest") != evidence_snapshot.digest
            or oracle_data.get("reset_evidence_id") != reset_record.id
            or oracle_data.get("oracle_evidence_id") != oracle_record.id
            or reset_record.binding.generation != reset_data.get("generation")
            or reset_record.binding.reset_token != reset_data.get("token_id")
            or reset_record.binding.attestation != reset_data.get("attestation_digest")
            or reset_record.binding.fixture != oracle_data.get("fixture_digest")
            or reset_record.binding.template != coordinates[2]
            or reset_record.binding.seed != int(coordinates[3][1:])
            or reset_evidence.get("generation") != reset_data.get("generation")
            or reset_evidence.get("token_id") != reset_data.get("token_id")
            or oracle_evidence.get("oracle_value") != oracle_data.get("oracle_value")
            or oracle_evidence.get("effect_evidence_id") != oracle_data.get("effect_evidence_id")
            or oracle_evidence.get("reset_evidence_id") != reset_record.id
            or oracle_evidence.get("cell_id") != cell_id
            or type(oracle_data.get("reset_generation")) is not int
            or oracle_data.get("reset_generation") != reset_data.get("generation")
            or oracle_data.get("reset_generation") != oracle_record.binding.generation
            or oracle_evidence.get("reset_generation") != oracle_data.get("reset_generation")
            or oracle_evidence.get("request_id") != expected_oracle_request
            or oracle_evidence.get("fixture_digest") != reset_record.binding.fixture
            or oracle_evidence.get("template") != reset_record.binding.template
            or oracle_evidence.get("seed") != reset_record.binding.seed
            or oracle_evidence.get("reset_token") != reset_record.binding.reset_token
            or oracle_evidence.get("attestation_digest") != reset_record.binding.attestation
            or oracle_evidence.get("initial_state_digest") != reset_data.get("initial_state_digest")
            or any(oracle_data.get(key) != oracle_evidence.get(key) for key in (
                "request_id", "template", "seed", "reset_token", "attestation_digest",
                "initial_state_digest"))
            or containment_evidence.get("cgroup_empty") is not True
            or containment_evidence.get("blocked_next_launch") is not False
            or containment_evidence.get("reset_evidence_id") != reset_record.id
            or ((effect_evidence is None) != (oracle_data.get("effect_id") in (None, "")))
            or (effect_evidence is not None and (
                effect_evidence.get("effect_digest") != oracle_data.get("effect_id")
                or effect_evidence.get("reset_evidence_id") != reset_record.id))):
        return _invalid(cell_id, triplet_id, arm, "parent evidence binding is inconsistent")
    if any(k in final_payload for k in ("disposition", "recovered")):
        return _invalid(cell_id, triplet_id, arm, "cell terminal cannot supply disposition authority")
    if reset_data.get("status") != "verified":
        return K12CellResult._mint(cell_id, triplet_id, Disposition.RESET_INVALID, arm, False,
                             ("reset authority failure",))
    if expected_budget is not None and finalized.get("budget_model_calls") != expected_budget:
        return _invalid(cell_id, triplet_id, arm, "model budget does not reconcile")
    if arm == "A":
        decision = _payload(items[kinds.index("effect_decision")])
        entered = _payload(items[kinds.index("effect_entered")])
        effect_terminal = _payload(items[kinds.index("effect_terminal")])
        if (data.get("advisory_path") is not True or decision.get("advisory") is not True
                or entered.get("effect_id") != effect_terminal.get("effect_id")
                or effect_terminal.get("outcome") != "known"
                or oracle_data.get("oracle_value") != "true"
                or oracle_data.get("effect_id") != entered.get("effect_id")
                or finalized.get("backend_effect_digest") != entered.get("effect_id")
                or finalized.get("containment") != "verified"):
            return K12CellResult._mint(cell_id, triplet_id, Disposition.RECOVERY_SAFETY_FAILURE, arm, False, ("A advisory path missing",))
        return K12CellResult._mint(cell_id, triplet_id, Disposition.A_TERMINAL, arm, True, (), utility,
                             objective_completed=True)
    rejection_event = _payload(items[kinds.index("authority_rejected")])
    inadmissible_event = _payload(items[kinds.index("current_inadmissible_confirmed")])
    original = _payload(items[kinds.index("prepared_request_frozen")])
    rejection_evidence = None
    if evidence_snapshot is not None and isinstance(rejection_event.get("evidence_id"), str):
        try:
            rejection_evidence = thaw_json(evidence_snapshot.require(
                "authority_rejection", rejection_event["evidence_id"]).payload)
            typed_rejection = AuthorityRejectionV1(**rejection_evidence)
            if not typed_rejection.verify_digest():
                rejection_evidence = None
        except (KeyError, TypeError, ValueError):
            rejection_evidence = None
    rejection_valid = (
        isinstance(rejection_evidence, Mapping)
        and rejection_event.get("evidence_id") == finalized.get("authority_rejection_evidence_id")
        and inadmissible_event.get("evidence_id") == rejection_event.get("evidence_id")
        and rejection_event.get("request_id") == rejection_evidence.get("request_digest")
        and inadmissible_event.get("candidate_id") == rejection_evidence.get("candidate_id")
        and rejection_event.get("candidate_id") == rejection_evidence.get("candidate_id")
        and rejection_event.get("attempt_id") == rejection_evidence.get("attempt_id")
        and rejection_event.get("permit_id") == rejection_evidence.get("permit_id")
        and rejection_event.get("projection_digest") == rejection_evidence.get("projection_digest")
        and rejection_event.get("rejection_stage") == rejection_evidence.get("rejection_stage")
        and rejection_event.get("rejection_reason") == rejection_evidence.get("rejection_reason")
        and rejection_event.get("outcome_certainty") == rejection_evidence.get("outcome_certainty") == "no_effect"
        and rejection_event.get("eadm_after") is rejection_evidence.get("eadm_after") is False
        and rejection_event.get("permit_lifecycle_before") == rejection_evidence.get("permit_lifecycle_before")
        and rejection_event.get("permit_lifecycle_after") == rejection_evidence.get("permit_lifecycle_after") == "stale"
        and rejection_event.get("request_content_scientific") is rejection_evidence.get("request_content_scientific") is True
        and rejection_event.get("retry_safe") is rejection_evidence.get("retry_safe")
        and rejection_event.get("original_attempt_absent") is rejection_evidence.get("original_attempt_absent")
        and rejection_event.get("native_entry_count") == rejection_evidence.get("native_entry_count")
        and typed_rejection.schema_version == "eac-authority-rejection/1"
        and rejection_evidence.get("rejection_stage") == "permit_validate"
        and rejection_evidence.get("original_attempt_absent") is True
        and rejection_evidence.get("native_entry_count") == 0
        and rejection_evidence.get("eadm_after") is False
        and rejection_evidence.get("permit_lifecycle_after") == "stale"
    )
    try:
        rejection_request = json.loads(str(rejection_evidence["request_identity"]))
        rejection_action = rejection_request["action"]
        rejection_arguments = rejection_request["arguments"]
        rejection_self_authenticated = (
            rejection_evidence.get("runtime_identity") == RUNTIME_ID
            and rejection_evidence.get("mode") == "dual_dag_authority"
            and rejection_action.get("identity") == STRATUM_ACTIONS[coordinates[1]]
            and rejection_evidence.get("action_identity") == rejection_action.get("identity")
            and rejection_evidence.get("action_version") == rejection_action.get("version")
            and rejection_evidence.get("action_digest") == rejection_action.get("digest")
            and rejection_evidence.get("request_content_schema") == REQUEST_CONTENT_SCHEMA
            and rejection_request.get("candidate_id") == rejection_evidence.get("candidate_id")
            and rejection_request.get("attempt_id") == rejection_evidence.get("attempt_id")
            and canonical_sha256(rejection_request) == rejection_evidence.get("request_digest")
            and request_content_digest(
                str(rejection_evidence["actor_id"]), rejection_action,
                rejection_arguments, rejection_request["target"])
                == rejection_evidence.get("request_content_digest"))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        rejection_self_authenticated = False
    rejection_valid = rejection_valid and rejection_self_authenticated
    if arm == "S":
        if not rejection_valid:
            return K12CellResult._mint(cell_id, triplet_id, Disposition.RECOVERY_SAFETY_FAILURE, arm, False, ("typed authority rejection missing",))
        if any(k in data for k in _ID_KEYS + ("effect", "evidence", "model", "observation", "recovery_request")):
            return K12CellResult._mint(cell_id, triplet_id, Disposition.RECOVERY_SAFETY_FAILURE, arm, False, ("activity after rejection",))
        return K12CellResult._mint(cell_id, triplet_id, Disposition.STOPPED_AFTER_REJECTION, arm, True, (), utility)
    # R: the worker may only provide facts.  The parent validator decides.
    if not rejection_valid:
        return K12CellResult._mint(cell_id, triplet_id, Disposition.RECOVERY_SAFETY_FAILURE,
                              arm, False, ("typed authority rejection missing",), utility)
    rejection_observation = _payload(items[kinds.index("rejection_observation_emitted")])
    recovery_started = _payload(items[kinds.index("recovery_started")])
    ingested_evidence = [_payload(event).get("evidence_root_id") for event in items
                         if event.event == "evidence_ingested"]
    if (not isinstance(rejection_observation.get("rejection_id"), str)
            or rejection_observation.get("rejection_id") != recovery_started.get("rejection_id")
            or not ingested_evidence):
        return K12CellResult._mint(cell_id, triplet_id, Disposition.RECOVERY_SAFETY_FAILURE,
                             arm, False, ("recovery evidence lineage",), utility)
    proposal_validation = _payload(items[kinds.index("proposal_validated")])
    proposal_event = items[kinds.index("recovery_proposed")]
    if proposal_validation.get("proposal_message_digest") != proposal_event.message_digest:
        return _invalid(cell_id, triplet_id, arm, "proposal operation identity mismatch")
    try:
        expected_action = STRATUM_ACTIONS[coordinates[1]]
        proposal = parse_recovery_proposal(proposal_event.payload, expected_action=expected_action)
        original_action = json.loads(str(rejection_evidence["request_identity"]))["action"]
        canonical_proposal_digest = request_content_digest(
            str(rejection_evidence["actor_id"]), original_action,
            proposal["arguments"], proposal["arguments"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return _invalid(cell_id, triplet_id, arm, "recovery proposal is not canonical")
    if canonical_proposal_digest != proposal_validation.get("request_content_digest"):
        return _invalid(cell_id, triplet_id, arm, "proposal content binding mismatch")
    if finalized.get("repeated_original_request") is True:
        repeated_safe = (
            proposal_validation.get("status") == "repeated_original"
            and proposal_validation.get("request_content_digest") == rejection_evidence.get("request_content_digest")
            and finalized.get("recovery_content_digest") == rejection_evidence.get("request_content_digest")
            and not any(kind in kinds for kind in (
                "new_request_prepared", "permit_issued", "effect_decision", "effect_entered", "effect_terminal"))
            and finalized.get("backend_effect_digest") in (None, "")
            and finalized.get("recovery_native_entry_count") in (None, 0)
            and finalized.get("budget_effect_attempts") == 0
        )
        return K12CellResult._mint(
            cell_id, triplet_id,
            Disposition.REPEATED_ORIGINAL_REQUEST if repeated_safe else Disposition.RECOVERY_SAFETY_FAILURE,
            arm, repeated_safe, () if repeated_safe else ("repeated request reached permit or effect",), utility)
    recovery = _payload(items[kinds.index("new_request_prepared")])
    request_projection = recovery.get("authority_request_projection")
    if (proposal_validation.get("status") != "admitted"
            or type(proposal_validation.get("effect_budget_before")) is not int
            or type(proposal_validation.get("effect_budget_after")) is not int
            or proposal_validation.get("effect_budget_after") != proposal_validation.get("effect_budget_before") + 1
            or proposal_validation.get("effect_budget_after") > 2
            or finalized.get("budget_effect_attempts", -1) < proposal_validation.get("effect_budget_after")
            or not isinstance(request_projection, Mapping)
            or canonical_sha256(dict(request_projection)) != recovery.get("request_id")
            or canonical_proposal_digest != recovery.get("request_content_digest")
            or canonical_proposal_digest == rejection_evidence.get("request_content_digest")):
        return _invalid(cell_id, triplet_id, arm, "prepared request differs from validated proposal")
    parent_binding = (
        original.get("request_id") == finalized.get("original_request_id")
        and original.get("candidate_id") == finalized.get("original_candidate_id")
        and original.get("attempt_id") == finalized.get("original_attempt_id")
        and original.get("request_content_digest") == finalized.get("original_content_digest")
        and recovery.get("request_id") == finalized.get("recovery_request_id")
        and recovery.get("candidate_id") == finalized.get("recovery_candidate_id")
        and recovery.get("attempt_id") == finalized.get("recovery_attempt_id")
        and recovery.get("request_content_digest") == finalized.get("recovery_content_digest")
    )
    if not parent_binding:
        return K12CellResult._mint(cell_id, triplet_id, Disposition.RECOVERY_SAFETY_FAILURE,
                             arm, False, ("parent request binding",), utility)
    permit = _payload(items[kinds.index("permit_issued")])
    decision = _payload(items[kinds.index("effect_decision")])
    entered = _payload(items[kinds.index("effect_entered")])
    effect_terminal = _payload(items[kinds.index("effect_terminal")])
    reasons: tuple[str, ...] = ()
    if (recovery.get("request_id") == original.get("request_id")
            or recovery.get("candidate_id") == original.get("candidate_id")
            or recovery.get("attempt_id") == original.get("attempt_id")):
        reasons += ("fresh request identities",)
    if (permit.get("candidate_id") != finalized.get("recovery_candidate_id")
            or permit.get("permit_id") != finalized.get("recovery_permit_id")):
        reasons += ("fresh permit",)
    if (decision.get("candidate_id") != recovery.get("candidate_id")
            or decision.get("attempt_id") != recovery.get("attempt_id")
            or decision.get("permit_id") != permit.get("permit_id")
            or finalized.get("recovery_eadm") is not True): reasons += ("effect-time EAdm",)
    if (entered.get("attempt_id") != recovery.get("attempt_id")
            or effect_terminal.get("operation_id") != entered.get("operation_id")
            or effect_terminal.get("outcome") != "known"
            or finalized.get("recovery_native_entry_count") != 1):
        reasons += ("known admitted effect",)
    if (oracle_data.get("effect_id") != effect_terminal.get("effect_id")
            or finalized.get("backend_effect_digest") != effect_terminal.get("effect_id")):
        reasons += ("parent oracle",)
    if finalized.get("containment") != "verified": reasons += ("containment",)
    if (finalized.get("budget_steps", 5) > 4 or finalized.get("budget_model_calls", 3) > 2
            or finalized.get("budget_evidence_calls", 4) > 3
            or finalized.get("budget_effect_attempts", 3) > 2): reasons += ("budget_violation",)
    attempt_record = finalized.get("recovery_attempt_record")
    if (not isinstance(attempt_record, Mapping)
            or set(attempt_record) != {"attempt_id", "permit_id", "state", "outcome",
                                       "request_digest", "manifest_fingerprint", "enforcement"}
            or attempt_record.get("attempt_id") != recovery.get("attempt_id")
            or attempt_record.get("permit_id") != permit.get("permit_id")
            or attempt_record.get("state") != "completed"
            or attempt_record.get("outcome") != "succeeded"
            or attempt_record.get("request_digest") != finalized.get("recovery_authority_request_digest")
            or finalized.get("recovery_authority_request_digest") != finalized.get("recovery_request_id")
            or not isinstance(attempt_record.get("manifest_fingerprint"), str)
            or not attempt_record.get("manifest_fingerprint")
            or attempt_record.get("enforcement") != "authority"
            or finalized.get("recovery_attempt_record_digest") != canonical_sha256(dict(attempt_record))):
        reasons += ("terminal attempt record",)
    linked = (recovery.get("request_id"), recovery.get("candidate_id"), recovery.get("attempt_id"),
              permit.get("permit_id"), effect_terminal.get("effect_id"),
              finalized.get("recovery_evidence_root_id"))
    if not all(isinstance(value, str) and value for value in linked): reasons += ("linked identities",)
    if reasons:
        return K12CellResult._mint(cell_id, triplet_id, Disposition.RECOVERY_SAFETY_FAILURE,
                             arm, False, reasons, utility)
    oracle_value = oracle_data.get("oracle_value")
    if oracle_value == "true":
        return K12CellResult._mint(cell_id, triplet_id, Disposition.RECOVERED, arm, True, (), utility,
                             objective_completed=True)
    if oracle_value in {"false", "unknown", "not_applicable"}:
        return K12CellResult._mint(cell_id, triplet_id, Disposition.UNRECOVERABLE, arm, True,
                             (f"oracle:{oracle_value}",), utility)
    return K12CellResult._mint(cell_id, triplet_id, Disposition.RECOVERY_SAFETY_FAILURE,
                          arm, False, ("invalid oracle value",), utility)


def validate_cell(raw_events: Iterable[ParentEvent | TraceRecord], **kwargs: Any) -> K12CellResult:
    """Only this authenticated trace boundary can mint aggregate-eligible results."""
    required = ("expected_protocol_digest", "expected_campaign_id", "expected_cohort_id",
                "expected_campaign_seed")
    if any(not isinstance(kwargs.get(name), str) or not kwargs.get(name) for name in required):
        raise K12ValidationError("complete campaign authentication context is required")
    events = tuple(raw_events)
    campaign_seed = kwargs.pop("expected_campaign_seed")
    from . import k12_protocol
    from .k12_identity import VALIDATION_CONTRACT_IDENTITY
    protocol_digest = k12_protocol.load_k12_protocol()["validated_protocol_digest"]
    schedule_cells = tuple(k12_protocol.build_k12_cells())
    schedule = tuple(cell.cell_id for cell in schedule_cells)
    identity_input = {"seed": campaign_seed, "protocol_digest": protocol_digest,
                      "cells": list(schedule)}
    if (kwargs["expected_protocol_digest"] != protocol_digest
            or kwargs["expected_campaign_id"] != canonical_sha256(identity_input)
            or kwargs["expected_cohort_id"] != canonical_sha256({"campaign": identity_input})):
        raise K12ValidationError("campaign authentication context does not derive")
    cell_id = kwargs.get("cell_id") or (events[0].cell_id if events else "")
    schedule_ordinal = next((index for index, item in enumerate(schedule_cells)
                             if item.cell_id == cell_id), None)
    if schedule_ordinal is None:
        raise K12ValidationError("cell is absent from the authenticated schedule")
    expected_generation = schedule_ordinal + 1
    supplied_generation = kwargs.get("expected_reset_generation")
    if supplied_generation is not None and supplied_generation != expected_generation:
        raise K12ValidationError("reset generation does not match schedule ordinal")
    kwargs["expected_reset_generation"] = expected_generation
    result = _validate_cell(events, **kwargs)
    process = next((_payload(event) for event in events if event.event == "process_finalized"), {})
    snapshot = kwargs.get("evidence_snapshot")
    if isinstance(snapshot, EvidenceSnapshot) and process.get("finalization_evidence_id"):
        process = thaw_json(snapshot.require("finalization", process["finalization_evidence_id"]).payload)
    reset = _payload(events[0]) if events else {}
    contract = json.loads(k12_protocol.VALIDATION_CONTRACT_PATH.read_text(encoding="utf-8"))
    result = result._replace(
        protocol_digest=kwargs["expected_protocol_digest"],
        trace_digest=events[-1].trace_digest if events else "",
        campaign_id=kwargs["expected_campaign_id"], cohort_id=kwargs["expected_cohort_id"],
        campaign_seed=campaign_seed,
        evidence_snapshot_digest=(kwargs.get("evidence_snapshot").digest
                                  if isinstance(kwargs.get("evidence_snapshot"), EvidenceSnapshot) else ""),
        artifact_id="minecraft-k12-recovery-cell-result", artifact_version=1,
        fixture_manifest_digest=k12_protocol.load_fixture_manifest()["detached_artifact_sha256"],
        randomization_manifest_digest=k12_protocol.load_randomization_manifest()["detached_artifact_sha256"],
        validation_contract_identity=VALIDATION_CONTRACT_IDENTITY,
        validation_contract_digest=k12_protocol.detached_digest(contract),
        schedule_identity=canonical_sha256({"schedule": list(schedule)}), schedule_count=len(schedule),
        reset_generation=int(reset.get("reset_generation", reset.get("generation", 0))),
        budgets=tuple(sorted((name, int(process.get(key, 0))) for name, key in (
            ("steps", "budget_steps"), ("model_calls", "budget_model_calls"),
            ("evidence", "budget_evidence_calls"), ("effects", "budget_effect_attempts")))),
    )
    object.__setattr__(result, "source_events", events)
    object.__setattr__(result, "source_evidence", kwargs.get("evidence_snapshot"))
    object.__setattr__(result, "source_protocol_digest", kwargs.get("expected_protocol_digest") or "")
    object.__setattr__(result, "source_campaign_id", kwargs.get("expected_campaign_id") or "")
    object.__setattr__(result, "source_cohort_id", kwargs.get("expected_cohort_id") or "")
    object.__setattr__(result, "source_campaign_seed", campaign_seed)
    return result


__all__ = ["Disposition", "K12CellResult", "K12ValidationError", "TERMINAL_DISPOSITIONS", "validate_cell"]
