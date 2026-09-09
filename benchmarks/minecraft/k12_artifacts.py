"""Finite descriptive artifacts joined to the authenticated K12 schedule."""
from __future__ import annotations
from dataclasses import dataclass, fields
import json
from typing import Any, Iterable
from benchmarks.common.eac.canonical import canonical_sha256, thaw_json
from . import k12_protocol
from .k12_identity import (AGGREGATE_IDENTITY, PROTOCOL_IDENTITY,
                           VALIDATION_CONTRACT_IDENTITY)
from .k12_protocol import build_k12_cells
from .k12_validation import Disposition, K12CellResult, validate_cell

K12_ARTIFACT_SCHEMA = AGGREGATE_IDENTITY
K12_ARTIFACT_ID = "minecraft-k12-recovery-aggregate"
_SHA256 = set("0123456789abcdef")
_SCHEDULE = tuple(cell.cell_id for cell in build_k12_cells())
K12_SCHEDULE_IDENTITY = canonical_sha256({"schedule": list(_SCHEDULE)})


def _authentication() -> tuple[str, str, str, str, str]:
    protocol = k12_protocol.load_k12_protocol()
    contract = json.loads(k12_protocol.VALIDATION_CONTRACT_PATH.read_text(encoding="utf-8"))
    contract_digest = k12_protocol.detached_digest(contract)
    if contract.get("identity") != {
            "protocol": PROTOCOL_IDENTITY,
            "trace": "minecraft-k12-recovery-trace/1",
            "cell_result": "minecraft-k12-recovery-cell-result/1",
    }:
        raise ValueError("K12 validation contract identity mismatch")
    return (protocol["validated_protocol_digest"],
            k12_protocol.load_fixture_manifest()["detached_artifact_sha256"],
            k12_protocol.load_randomization_manifest()["detached_artifact_sha256"],
            VALIDATION_CONTRACT_IDENTITY, contract_digest)


def _is_digest(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and set(value) <= _SHA256)


def _is_prefixed_digest(value: object) -> bool:
    return (isinstance(value, str) and value.startswith("sha256:")
            and _is_digest(value.removeprefix("sha256:")))

@dataclass(frozen=True, slots=True)
class K12ScheduleReport:
    """The finite, canonical schedule status reported by an aggregate."""

    scheduled: int
    attempted: int
    reset_valid: int
    trace_valid: int
    integrity_valid: int
    runtime_failure: int
    budget_exhausted: int
    recovered: int
    comparable_triplet: int
    not_started: int

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 0 for value in self.as_tuple()):
            raise ValueError("K12 schedule report values must be non-negative integers")

    def as_tuple(self) -> tuple[int, ...]:
        return (self.scheduled, self.attempted, self.reset_valid, self.trace_valid,
                self.integrity_valid, self.runtime_failure, self.budget_exhausted,
                self.recovered, self.comparable_triplet, self.not_started)

    def as_dict(self) -> dict[str, int]:
        return {"scheduled": self.scheduled, "attempted": self.attempted,
                "reset_valid": self.reset_valid, "trace_valid": self.trace_valid,
                "integrity_valid": self.integrity_valid,
                "runtime_failure": self.runtime_failure,
                "budget_exhausted": self.budget_exhausted, "recovered": self.recovered,
                "comparable_triplet": self.comparable_triplet,
                "not_started": self.not_started}


@dataclass(frozen=True, slots=True, init=False)
class K12CampaignStop:
    protocol_digest: str
    campaign_id: str
    cohort_id: str
    campaign_seed: str
    cause: str
    reason: str
    trigger_reference: str
    attempted_result_identities: tuple[str, ...]
    remaining_cell_ids: tuple[str, ...]
    triggering_cell_id: str
    identity: str

    @classmethod
    def from_prefix(cls, *args: Any, **kwargs: Any) -> "K12CampaignStop":
        raise TypeError("campaign stops are minted only by parent campaign authority")

    @classmethod
    def _from_parent_prefix(cls, cells: Iterable[K12CellResult], *, protocol_digest: str,
                    campaign_id: str, cohort_id: str, campaign_seed: str,
                    cause: str, reason: str,
                    trigger_reference: str | None = None) -> "K12CampaignStop":
        values = tuple(cells)
        schedule = _SCHEDULE
        if tuple(cell.cell_id for cell in values) != schedule[:len(values)]:
            raise ValueError("campaign stop results are not the canonical attempted prefix")
        if not (0 <= len(values) < len(schedule)) or not isinstance(reason, str) or not reason:
            raise ValueError("campaign stop requires a bounded partial prefix and reason")
        if cause not in {"containment_unknown", "manifest_corruption", "parent_authority_loss"}:
            raise ValueError("campaign stop cause is not frozen")
        identity_input = {"seed": campaign_seed, "protocol_digest": protocol_digest,
                          "cells": list(_SCHEDULE)}
        if (campaign_id != canonical_sha256(identity_input)
                or cohort_id != canonical_sha256({"campaign": identity_input})
                or any(cell.campaign_seed != campaign_seed for cell in values)):
            raise ValueError("campaign stop campaign/cohort identity mismatch")
        if values:
            if cause == "containment_unknown":
                if values[-1].disposition is not Disposition.INFRASTRUCTURE_FAILURE:
                    raise ValueError("attempted campaign stop lacks a containment failure authority")
                try:
                    snapshot = values[-1].source_evidence
                    process = next(thaw_json(event.payload) for event in values[-1].source_events
                                   if event.event == "process_finalized")
                    finalization = thaw_json(snapshot.require(
                        "finalization", process["finalization_evidence_id"]).payload)
                    containment = thaw_json(snapshot.require(
                        "containment", finalization["containment_evidence_id"]).payload)
                except (AttributeError, KeyError, StopIteration, TypeError, ValueError) as exc:
                    raise ValueError("containment stop authority is unresolved") from exc
                if (finalization.get("containment_failure") is not True
                        or containment.get("containment_failure") is not True
                        or not (containment.get("blocked_next_launch") is True
                                or containment.get("cgroup_empty") is False)):
                    raise ValueError("attempted campaign stop lacks authenticated containment failure")
                expected_trigger = values[-1].trace_digest
            else:
                expected_trigger = canonical_sha256({
                    "domain": "minecraft-k12-campaign-stop-authority/1",
                    "protocol_digest": protocol_digest, "campaign_id": campaign_id,
                    "cohort_id": cohort_id, "campaign_seed": campaign_seed,
                    "cause": cause, "reason": reason,
                    "blocking_cell_id": schedule[len(values)],
                    "attempted_result_identities": [cell.identity for cell in values],
                })
                if trigger_reference is not None and trigger_reference != expected_trigger:
                    raise ValueError("campaign stop authority reference does not derive")
        else:
            if cause == "containment_unknown":
                raise ValueError("containment stop requires an attempted failure cell")
            expected_trigger = canonical_sha256({
                "domain": "minecraft-k12-campaign-stop-authority/1",
                "protocol_digest": protocol_digest, "campaign_id": campaign_id,
                "cohort_id": cohort_id, "campaign_seed": campaign_seed,
                "cause": cause, "reason": reason,
                "blocking_cell_id": schedule[0], "attempted_result_identities": [],
            })
            if trigger_reference is not None and trigger_reference != expected_trigger:
                raise ValueError("campaign stop authority reference does not derive")
        remaining = schedule[len(values):]
        body = {
            "schema": "minecraft-k12-campaign-stop/1", "protocol_digest": protocol_digest,
            "campaign_id": campaign_id, "cohort_id": cohort_id, "reason": reason,
            "campaign_seed": campaign_seed, "cause": cause,
            "trigger_reference": expected_trigger,
            "attempted_result_identities": [cell.identity for cell in values],
            "remaining_cell_ids": list(remaining),
            "triggering_cell_id": (values[-1].cell_id if cause == "containment_unknown"
                                    else schedule[len(values)]),
        }
        result = object.__new__(cls)
        minted = (protocol_digest, campaign_id, cohort_id, campaign_seed, cause, reason,
                  expected_trigger, tuple(cell.identity for cell in values), remaining,
                  body["triggering_cell_id"], canonical_sha256(body))
        for descriptor, value in zip(fields(cls), minted):
            object.__setattr__(result, descriptor.name, value)
        return result


@dataclass(frozen=True, slots=True, init=False)
class K12Aggregate:
    schema: str
    artifact_id: str
    artifact_version: int
    cells: tuple[K12CellResult, ...]
    descriptive_counts: tuple[tuple[str, int], ...]
    utility_vectors: tuple[tuple[str, tuple[int, ...]], ...]
    schedule_report: K12ScheduleReport
    protocol_digest: str
    campaign_id: str
    cohort_id: str
    identity: str
    fixture_manifest_digest: str
    randomization_manifest_digest: str
    validation_contract_identity: str
    validation_contract_digest: str
    schedule_identity: str
    schedule_count: int
    finalized_slots: tuple[tuple[str, str, str], ...]
    campaign_disposition: str
    campaign_stop: K12CampaignStop | None
    infrastructure_stop_reason: str

    @classmethod
    def _mint(cls, *values: Any) -> "K12Aggregate":
        if len(values) != len(fields(cls)):
            raise ValueError("complete authenticated K12 aggregate fields required")
        result = object.__new__(cls)
        for descriptor, value in zip(fields(cls), values):
            object.__setattr__(result, descriptor.name, value)
        return result

    @classmethod
    def from_cells(cls, cells: Iterable[K12CellResult], *, artifact_id: str = K12_ARTIFACT_ID,
                   campaign_stop: K12CampaignStop | None = None):
        if artifact_id != K12_ARTIFACT_ID:
            raise ValueError("K12 aggregate artifact identity is frozen")
        values = tuple(cells)
        protocol_digest, fixture_digest, randomization_digest, contract_identity, contract_digest = _authentication()
        if not values and campaign_stop is None:
            raise ValueError("K12 aggregate requires a complete campaign or authenticated stop")
        schedule = {c.cell_id: c for c in build_k12_cells()}
        if len({c.cell_id for c in values}) != len(values):
            raise ValueError("duplicate K12 cell result")
        if tuple(cell.cell_id for cell in values) != _SCHEDULE[:len(values)]:
            raise ValueError("K12 results are not the canonical schedule prefix")
        for ordinal, cell in enumerate(values):
            if type(cell) is not K12CellResult or not cell.authenticated:
                raise ValueError("K12 aggregate requires validator-authenticated cell results")
            try:
                cell.require_public_authentication()
            except Exception as exc:
                raise ValueError("K12 cell public authentication is invalid") from exc
            replayed = validate_cell(
                cell.source_events, cell_id=cell.cell_id, triplet_id=cell.triplet_id,
                expected_arm=cell.arm, expected_reset_generation=cell.reset_generation,
                evidence_snapshot=cell.source_evidence,
                expected_protocol_digest=cell.source_protocol_digest,
                expected_campaign_id=cell.source_campaign_id,
                expected_cohort_id=cell.source_cohort_id,
                expected_campaign_seed=cell.source_campaign_seed,
            )
            if ((replayed.disposition, replayed.arm, replayed.valid, replayed.reasons,
                 replayed.utility, replayed.objective_completed)
                    != (cell.disposition, cell.arm, cell.valid, cell.reasons,
                        cell.utility, cell.objective_completed)
                    or cell.protocol_digest != cell.source_protocol_digest
                    or cell.campaign_id != cell.source_campaign_id
                    or cell.cohort_id != cell.source_cohort_id
                    or cell.trace_digest != cell.source_events[-1].trace_digest
                    or cell.evidence_snapshot_digest != cell.source_evidence.digest):
                raise ValueError("K12 cell result does not match authenticated trace evidence")
            if cell.reset_generation != ordinal + 1:
                raise ValueError("K12 reset generation does not match schedule ordinal")
        identities = {
            "protocol_digest": (values[0].protocol_digest if values else campaign_stop.protocol_digest),
            "campaign_id": (values[0].campaign_id if values else campaign_stop.campaign_id),
            "cohort_id": (values[0].cohort_id if values else campaign_stop.cohort_id),
        }
        campaign_seed = values[0].campaign_seed if values else campaign_stop.campaign_seed
        identity_input = {"seed": campaign_seed, "protocol_digest": identities["protocol_digest"],
                          "cells": list(_SCHEDULE)}
        if (identities["campaign_id"] != canonical_sha256(identity_input)
                or identities["cohort_id"] != canonical_sha256({"campaign": identity_input})
                or any(cell.campaign_seed != campaign_seed for cell in values)):
            raise ValueError("K12 campaign/cohort identity cannot be authenticated")
        for name in ("protocol_digest", "campaign_id", "cohort_id"):
            if len({getattr(cell, name) for cell in values}) > 1:
                raise ValueError(f"mixed K12 {name}")
            validator = _is_digest if name == "protocol_digest" else _is_prefixed_digest
            if not validator(identities[name]) or any(not validator(getattr(cell, name)) for cell in values):
                raise ValueError(f"K12 {name} must be an authenticated identity")
        if campaign_stop is None and len(values) != len(_SCHEDULE):
            raise ValueError("K12 aggregate requires all 90 results unless the campaign stopped")
        if any(not _is_prefixed_digest(cell.trace_digest)
               or not _is_prefixed_digest(cell.evidence_snapshot_digest) for cell in values):
            raise ValueError("K12 cell trace and evidence identities must be authenticated")
        if identities["protocol_digest"] != protocol_digest:
            raise ValueError("K12 protocol identity mismatch")
        if any((cell.artifact_id != "minecraft-k12-recovery-cell-result"
                or cell.artifact_version != 1
                or cell.fixture_manifest_digest != fixture_digest
                or cell.randomization_manifest_digest != randomization_digest
                or cell.validation_contract_identity != contract_identity
                or cell.validation_contract_digest != contract_digest
                or cell.schedule_identity != K12_SCHEDULE_IDENTITY
                or cell.schedule_count != len(_SCHEDULE)) for cell in values):
            raise ValueError("K12 cell frozen artifact authentication mismatch")
        for cell in values:
            expected = schedule.get(cell.cell_id)
            if expected is None or cell.triplet_id != expected.triplet_id or cell.arm != expected.arm:
                raise ValueError("foreign or unauthenticated K12 cell result")
        if campaign_stop is not None:
            expected_stop = K12CampaignStop._from_parent_prefix(
                values, protocol_digest=identities["protocol_digest"],
                campaign_id=identities["campaign_id"], cohort_id=identities["cohort_id"],
                campaign_seed=campaign_seed, cause=campaign_stop.cause,
                reason=campaign_stop.reason,
                trigger_reference=campaign_stop.trigger_reference)
            if campaign_stop != expected_stop:
                raise ValueError("K12 campaign stop identity or prefix mismatch")
        attempted = len(values)
        stop_ref = campaign_stop.identity if campaign_stop is not None else ""
        finalized_slots = tuple(
            (cell_id, "terminal", values[index].identity) if index < attempted
            else (cell_id, "not_started", stop_ref)
            for index, cell_id in enumerate(_SCHEDULE)
        )
        counts: dict[str, int] = {}
        vectors = []
        for cell in values:
            counts[cell.disposition.value] = counts.get(cell.disposition.value, 0) + 1
            vectors.append((cell.cell_id, tuple(cell.utility)))
        groups: dict[str, list[K12CellResult]] = {}
        for cell in values:
            groups.setdefault(cell.triplet_id, []).append(cell)
        equivalence_keys = ("action_identity", "request_content_digest", "rejection_stage",
                            "rejection_reason", "outcome_certainty",
                            "eadm_after", "permit_lifecycle_before", "permit_lifecycle_after",
                            "request_content_scientific", "retry_safe",
                            "original_attempt_absent", "native_entry_count")
        for triplet in groups.values():
            arms = {cell.arm: cell for cell in triplet}
            if {"R", "S"} <= set(arms):
                projections = []
                for arm in ("R", "S"):
                    records = [record for record in arms[arm].source_evidence.records
                               if record.kind == "authority_rejection"]
                    if len(records) != 1 or not records[0].verify():
                        raise ValueError("K12 R/S authority projection is missing or ambiguous")
                    projections.append(thaw_json(records[0].payload))
                if tuple(projections[0].get(key) for key in equivalence_keys) != tuple(
                        projections[1].get(key) for key in equivalence_keys):
                    raise ValueError("K12 R/S equivalence does not derive from registry evidence")
        report = K12ScheduleReport(
            scheduled=len(schedule), attempted=len(values),
            reset_valid=sum(cell.disposition is not Disposition.RESET_INVALID for cell in values),
            trace_valid=sum(cell.disposition is not Disposition.TRACE_INVALID for cell in values),
            integrity_valid=sum(cell.valid for cell in values),
            runtime_failure=sum(cell.disposition is Disposition.INFRASTRUCTURE_FAILURE for cell in values),
            budget_exhausted=sum(cell.disposition is Disposition.BUDGET_EXHAUSTED for cell in values),
            recovered=sum(cell.disposition is Disposition.RECOVERED for cell in values),
            comparable_triplet=sum({cell.arm for cell in triplet} == {"A", "R", "S"}
                                   and len(triplet) == 3 and all(cell.valid for cell in triplet)
            for triplet in groups.values()), not_started=len(_SCHEDULE) - attempted,
        )
        body = {"schema": K12_ARTIFACT_SCHEMA, "artifact_id": K12_ARTIFACT_ID,
                "artifact_version": 1,
                "schedule": list(_SCHEDULE), "schedule_identity": K12_SCHEDULE_IDENTITY,
                "schedule_count": len(_SCHEDULE),
                "cells": [cell.identity for cell in values], "counts": counts,
                "utility_vectors": [[i, list(v)] for i, v in vectors],
                "schedule_report": report.as_dict(),
                "protocol_digest": identities["protocol_digest"],
                "campaign_id": identities["campaign_id"],
                "cohort_id": identities["cohort_id"],
                "finalized_slots": [list(slot) for slot in finalized_slots],
                "campaign_disposition": "INFRASTRUCTURE_STOP" if campaign_stop else "COMPLETED",
                "campaign_stop_identity": stop_ref,
                "infrastructure_stop_reason": campaign_stop.reason if campaign_stop else ""}
        body.update({"fixture_manifest_digest": fixture_digest,
                     "randomization_manifest_digest": randomization_digest,
                     "validation_contract_identity": contract_identity,
                     "validation_contract_digest": contract_digest})
        return cls._mint(K12_ARTIFACT_SCHEMA, K12_ARTIFACT_ID, 1,
                    values, tuple(sorted(counts.items())),
                   tuple(vectors), report, body["protocol_digest"], body["campaign_id"],
                   body["cohort_id"], canonical_sha256(body), fixture_digest,
                   randomization_digest, contract_identity, contract_digest,
                    K12_SCHEDULE_IDENTITY, len(_SCHEDULE), finalized_slots,
                    body["campaign_disposition"], campaign_stop,
                    body["infrastructure_stop_reason"])

    @property
    def scheduled(self) -> int: return self.schedule_report.scheduled
    @property
    def attempted(self) -> int: return self.schedule_report.attempted
    @property
    def reset_valid(self) -> int: return self.schedule_report.reset_valid
    @property
    def trace_valid(self) -> int: return self.schedule_report.trace_valid
    @property
    def integrity_valid(self) -> int: return self.schedule_report.integrity_valid
    @property
    def runtime_failure(self) -> int: return self.schedule_report.runtime_failure
    @property
    def budget_exhausted(self) -> int: return self.schedule_report.budget_exhausted
    @property
    def recovered(self) -> int: return self.schedule_report.recovered
    @property
    def comparable_triplet(self) -> int:
        return self.schedule_report.comparable_triplet

    @property
    def objective_completion(self) -> tuple[tuple[str, int], ...]:
        return tuple((arm, sum(cell.arm == arm and cell.valid and cell.objective_completed
            for cell in self.cells)) for arm in ("A", "R", "S"))

    @property
    def original_invalid_effect_integrity(self) -> int:
        return sum(cell.arm in {"R", "S"} and cell.valid and cell.disposition.value in
                   {"RECOVERED", "UNRECOVERABLE", "BUDGET_EXHAUSTED",
                    "REPEATED_ORIGINAL_REQUEST", "STOPPED_AFTER_REJECTION"}
                   for cell in self.cells)

def aggregate_cells(cells: Iterable[K12CellResult], *, artifact_id: str = K12_ARTIFACT_ID,
                    campaign_stop: K12CampaignStop | None = None) -> K12Aggregate:
    return K12Aggregate.from_cells(cells, artifact_id=artifact_id, campaign_stop=campaign_stop)

__all__ = ["K12Aggregate", "K12CampaignStop", "K12ScheduleReport", "K12_ARTIFACT_ID", "K12_ARTIFACT_SCHEMA",
           "K12_SCHEDULE_IDENTITY", "aggregate_cells"]
