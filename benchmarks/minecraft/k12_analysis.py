"""Descriptive-only K12 analysis; no inferential claims are made."""
from __future__ import annotations
from dataclasses import dataclass, fields
from typing import Any
from benchmarks.common.eac.canonical import canonical_sha256
from .k12_artifacts import (K12Aggregate, K12ScheduleReport, K12_ARTIFACT_ID,
                             K12_ARTIFACT_SCHEMA,
                             K12_SCHEDULE_IDENTITY, _authentication)
from .k12_identity import ANALYSIS_IDENTITY
from .k12_protocol import build_k12_cells

_FORBIDDEN = ("k8", "k10", "k11", "p_value", "confidence", "significance", "infer", "odds", "effect_size")
K12_ANALYSIS_ID = "minecraft-k12-descriptive-analysis"

@dataclass(frozen=True, slots=True, init=False)
class K12Analysis:
    schema: str
    analysis_id: str
    artifact_version: int
    aggregate_id: str
    paired_triplets: tuple[str, ...]
    excluded_triplets: tuple[str, ...]
    descriptive_counts: tuple[tuple[str, int], ...]
    utility_vectors: tuple[tuple[str, tuple[int, ...]], ...]
    schedule_report: K12ScheduleReport
    protocol_digest: str
    campaign_id: str
    cohort_id: str
    recovery_by_stratum: tuple[tuple[str, int], ...] = ()
    paired_differences: tuple[tuple[str, int, int], ...] = ()
    utility_differences: tuple[tuple[str, tuple[int, ...], tuple[int, ...]], ...] = ()
    identity: str = ""
    fixture_manifest_digest: str = ""
    randomization_manifest_digest: str = ""
    validation_contract_identity: str = ""
    validation_contract_digest: str = ""
    schedule_identity: str = ""
    schedule_count: int = 0

    @classmethod
    def _mint(cls, *values: Any) -> "K12Analysis":
        if len(values) != len(fields(cls)):
            raise ValueError("complete authenticated K12 analysis fields required")
        result = object.__new__(cls)
        for descriptor, value in zip(fields(cls), values):
            object.__setattr__(result, descriptor.name, value)
        return result

def analyze(aggregate: K12Aggregate, *, analysis_id: str = K12_ANALYSIS_ID) -> K12Analysis:
    if (type(aggregate) is not K12Aggregate or analysis_id != K12_ANALYSIS_ID
            or any(x in analysis_id.lower() for x in _FORBIDDEN)):
        raise ValueError("invalid or forbidden K12 analysis identity")
    protocol_digest, fixture_digest, randomization_digest, contract_identity, contract_digest = _authentication()
    expected = K12Aggregate.from_cells(aggregate.cells, campaign_stop=aggregate.campaign_stop)
    if (aggregate != expected or aggregate.artifact_id != K12_ARTIFACT_ID
            or aggregate.artifact_version != 1
            or aggregate.protocol_digest != protocol_digest
            or aggregate.fixture_manifest_digest != fixture_digest
            or aggregate.randomization_manifest_digest != randomization_digest
            or aggregate.validation_contract_identity != contract_identity
            or aggregate.validation_contract_digest != contract_digest
            or aggregate.schedule_identity != K12_SCHEDULE_IDENTITY
            or aggregate.schedule_count != 90):
        raise ValueError("K12 aggregate identity or authentication mismatch")
    scheduled_cells = build_k12_cells()
    schedule = {c.cell_id: c for c in scheduled_cells}
    by_triplet: dict[str, dict[str, object]] = {
        triplet_id: {} for triplet_id in dict.fromkeys(cell.triplet_id for cell in scheduled_cells)
    }
    for cell in aggregate.cells:
        by_triplet.setdefault(cell.triplet_id, {})[cell.arm] = cell
    paired = tuple(sorted(t for t, arms in by_triplet.items()
                          if set(arms) == {"A", "R", "S"}
                          and all(cell.valid for cell in arms.values())))
    excluded = tuple(sorted(t for t in by_triplet if t not in paired))
    differences = []
    utility_differences = []
    for triplet in paired:
        arms = by_triplet[triplet]
        a, r, s = arms["A"], arms["R"], arms["S"]
        differences.append((triplet, int(r.objective_completed) - int(a.objective_completed),
                            int(r.objective_completed) - int(s.objective_completed)))
        utility_differences.append((triplet, tuple(x - y for x, y in zip(r.utility, a.utility)),
                                    tuple(x - y for x, y in zip(r.utility, s.utility))))
    strata: dict[str, int] = {stratum: 0 for stratum in ("S1", "S2", "S3", "S4", "S5")}
    for cell in aggregate.cells:
        if (cell.arm == "R" and cell.valid and cell.disposition.value == "RECOVERED"
                and cell.cell_id in schedule):
            strata[schedule[cell.cell_id].stratum] = strata.get(schedule[cell.cell_id].stratum, 0) + 1
    body = {"schema": ANALYSIS_IDENTITY, "analysis_id": K12_ANALYSIS_ID,
            "artifact_version": 1,
            "aggregate_id": aggregate.artifact_id, "aggregate_identity": aggregate.identity,
            "paired": list(paired), "excluded": list(excluded),
             "counts": [[key, value] for key, value in aggregate.descriptive_counts],
             "utility_vectors": [[cell_id, list(vector)]
                                 for cell_id, vector in aggregate.utility_vectors],
             "schedule_report": aggregate.schedule_report.as_dict(),
             "protocol_digest": aggregate.protocol_digest,
             "campaign_id": aggregate.campaign_id, "cohort_id": aggregate.cohort_id,
             "fixture_manifest_digest": fixture_digest,
             "randomization_manifest_digest": randomization_digest,
             "validation_contract_identity": contract_identity,
             "validation_contract_digest": contract_digest,
             "schedule_identity": K12_SCHEDULE_IDENTITY, "schedule_count": 90,
            "differences": [[triplet, ra, rs] for triplet, ra, rs in differences],
            "utility_differences": [[triplet, list(ra), list(rs)]
                                    for triplet, ra, rs in utility_differences],
            "strata": strata}
    return K12Analysis._mint(ANALYSIS_IDENTITY, K12_ANALYSIS_ID, 1,
                         aggregate.artifact_id, paired, excluded,
                         aggregate.descriptive_counts, aggregate.utility_vectors, aggregate.schedule_report,
                         aggregate.protocol_digest, aggregate.campaign_id, aggregate.cohort_id,
                         tuple(sorted(strata.items())),
                         tuple(differences), tuple(utility_differences), canonical_sha256(body),
                         fixture_digest, randomization_digest, contract_identity, contract_digest,
                         K12_SCHEDULE_IDENTITY, 90)

__all__ = ["K12Analysis", "K12_ANALYSIS_ID", "analyze"]
