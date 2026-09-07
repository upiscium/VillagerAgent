"""Offline-only K11 trace reconstruction and P0 diagnostic analysis.

This module never mutates the measured runtime.  It replays captured actor-visible
evidence into a fresh advisory runtime and evaluates the original action semantics
there.  P0/P1 outputs from this module are diagnostic only unless bound by a
later frozen K11-E protocol.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from benchmarks.common.eac import Proposition, PropositionKey
from benchmarks.common.eac.authority import _proposition_slot
from benchmarks.minecraft.eac_runtime import CLASSIFICATION_PATH, MinecraftEACRuntime
from benchmarks.minecraft.k11_trace import (
    PRIMARY_EFFECT_ACTIONS,
    TRACE_SCHEMA_VERSION,
    _event_precedes,
    derive_positive_disposition,
    validate_p0_trace,
    validate_trace,
    canonical_trace_bytes,
    event_in_observation_window,
    observation_window_bounds,
)


class K11AnalysisError(ValueError):
    pass


PROSPECTIVE_TRACE_SCHEMA = "minecraft-k11-trace/3"
MEASUREMENT_CUT_SCHEMA = "minecraft-k11-measurement-cut/1"
PROSPECTIVE_ANALYSIS_ID = "minecraft-k11-prospective-analysis"
PROSPECTIVE_ANALYSIS_VERSION = 1


def _validate_measurement_cut(trace: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[int, int, str]]:
    cut = trace.get("measurement_cut")
    if not isinstance(cut, Mapping) or cut.get("schema_version") != MEASUREMENT_CUT_SCHEMA:
        raise K11AnalysisError("prospective trace lacks measurement cut schema binding")
    if cut.get("boundary") != "[open,close)":
        raise K11AnalysisError("prospective measurement cut boundary is invalid")
    start = cut.get("window_open_monotonic_ns")
    end = cut.get("window_close_monotonic_ns")
    reason = cut.get("close_reason")
    if (type(start) is not int or type(end) is not int or end <= start
            or reason not in {"fixed_observation_horizon", "natural_runtime_terminal"}):
        raise K11AnalysisError("prospective measurement cut bounds are invalid")
    events = trace.get("events")
    if not isinstance(events, list):
        raise K11AnalysisError("prospective trace events are malformed")
    close_sequence = cut.get("close_sequence")
    high_water = cut.get("event_prefix_high_water_sequence")
    count = cut.get("in_window_event_count")
    digest = cut.get("in_window_event_digest")
    if (type(close_sequence) is not int or type(high_water) is not int or high_water < 0
            or close_sequence < 0 or close_sequence > high_water):
        raise K11AnalysisError("prospective measurement cut high-water is invalid")
    cut_events = [e for e in events if isinstance(e, Mapping)
                  and isinstance(e.get("monotonic_ns"), int)
                  and start <= e["monotonic_ns"] < end
                  and e.get("seq", 0) <= high_water]
    expected = "sha256:" + hashlib.sha256(canonical_trace_bytes(cut_events)).hexdigest()
    if (type(count) is not int or count != len(cut_events) or not isinstance(digest, str)
            or digest != expected):
        raise K11AnalysisError("prospective measurement cut digest/count/high-water mismatch")
    if (cut.get("snapshot_valid") is not True
            or not isinstance(cut.get("snapshot_errors"), list)
            or cut.get("snapshot_errors")):
        raise K11AnalysisError("prospective measurement cut snapshot is invalid")
    inventory = cut.get("censoring_inventory")
    if (not isinstance(inventory, Mapping) or not isinstance(inventory.get("items"), list)
            or not isinstance(inventory.get("retention"), Mapping)):
        raise K11AnalysisError("prospective censoring inventory is invalid")
    return dict(cut), (start, end, reason)


def load_trace(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") not in {
        TRACE_SCHEMA_VERSION, PROSPECTIVE_TRACE_SCHEMA,
    }:
        raise K11AnalysisError("invalid K11 trace artifact")
    if value.get("schema_version") == PROSPECTIVE_TRACE_SCHEMA:
        _validate_measurement_cut(value)
    return value


def _proposition(value: Mapping[str, Any]) -> Proposition:
    return Proposition(
        PropositionKey(
            str(value["namespace"]),
            str(value["predicate"]),
            tuple(value.get("arguments", ())),
            str(value.get("temporal_scope", "")),
        ),
        polarity=value.get("polarity") is True,
    )


def _runtime(run_id: str) -> MinecraftEACRuntime:
    classification = json.loads(CLASSIFICATION_PATH.read_text(encoding="utf-8"))
    actions = classification.get("actions", [])
    names = [row["action_identity"] for row in actions if isinstance(row, Mapping)]
    pass_checks = {name: (lambda unused: True) for name in names}
    sec_checks = {
        row["action_identity"]: (lambda unused: True)
        for row in actions
        if isinstance(row, Mapping) and row.get("sec_pre")
    }
    return MinecraftEACRuntime(
        mode="dual_dag_advisory",
        run_id=run_id,
        env_prechecks=pass_checks,
        sec_prechecks=sec_checks,
        audit_path=None,
    )


def _evidence_events_before(trace: Mapping[str, Any], cutoff_seq: int) -> list[Mapping[str, Any]]:
    return [
        event for event in trace.get("events", [])
        if event.get("event_type") == "k11.eac_evidence_ingested"
        and isinstance(event.get("seq"), int)
        and event["seq"] < cutoff_seq
    ]


def _replay_evidence(runtime: MinecraftEACRuntime, events: list[Mapping[str, Any]]) -> None:
    for event in sorted(events, key=lambda item: item["seq"]):
        payload = event.get("payload", {})
        actor_id = event.get("actor_id")
        if not isinstance(actor_id, str) or not actor_id:
            raise K11AnalysisError("evidence event lacks actor identity")
        proposition_value = payload.get("proposition")
        if not isinstance(proposition_value, Mapping):
            raise K11AnalysisError("evidence event lacks proposition")
        record_type = payload.get("record_type")
        source = payload.get("source")
        root_id = payload.get("root_id")
        revision = payload.get("revision")
        if not all(isinstance(value, str) and value for value in (record_type, source, root_id)):
            raise K11AnalysisError("evidence event identity is incomplete")
        supersedes_value = payload.get("supersedes", [])
        if not isinstance(supersedes_value, list) or any(not isinstance(item, str) for item in supersedes_value):
            raise K11AnalysisError("evidence supersession identity is malformed")
        runtime.ingest_actor_record(
            actor_id=actor_id,
            proposition=_proposition(proposition_value),
            record_type=record_type,
            source=source,
            visible_to=(actor_id,),
            root_id=root_id,
            revision=revision,
            supersedes=tuple(supersedes_value),
        )


def replay_admissibility(
    trace: Mapping[str, Any],
    prepared_event: Mapping[str, Any],
    *,
    cutoff_seq: int,
    replay_label: str,
) -> dict[str, Any]:
    """Re-evaluate one prepared request using only evidence observed before cutoff."""
    payload = prepared_event.get("payload", {})
    request = payload.get("exact_request")
    actor_id = prepared_event.get("actor_id")
    if not isinstance(request, Mapping) or not isinstance(actor_id, str) or not actor_id:
        actor_scope = payload.get("actor_scope")
        actor_id = actor_scope.get("actor_id") if isinstance(actor_scope, Mapping) else actor_id
    if not isinstance(request, Mapping) or not isinstance(actor_id, str) or not actor_id:
        raise K11AnalysisError("prepared event lacks exact request or actor identity")
    action = request.get("action")
    arguments = request.get("arguments")
    if not isinstance(action, Mapping) or not isinstance(arguments, Mapping):
        raise K11AnalysisError("prepared request binding is malformed")
    tool_name = action.get("identity")
    if not isinstance(tool_name, str) or not tool_name:
        raise K11AnalysisError("prepared request action identity is missing")

    runtime = _runtime(f"offline:{trace.get('run_id')}:{replay_label}")
    _replay_evidence(runtime, _evidence_events_before(trace, cutoff_seq))

    def offline_native(**kwargs):
        return {"status": True}

    kwargs = {"player_name": actor_id, **dict(arguments), "emotion": [], "murmur": ""}
    prepared = runtime.prepare_tool(tool_name, offline_native, (), kwargs)
    if prepared.request.action.digest != action.get("digest"):
        raise K11AnalysisError("offline action semantic binding differs from captured request")
    decision = runtime.authority.evaluate(prepared.request.candidate_id)
    candidate = runtime.authority._candidates[prepared.request.candidate_id]
    manifest = candidate.manifest
    if manifest is None:
        raise K11AnalysisError("offline admissibility evaluation produced no dependency manifest")
    return {
        "admissible": decision.admissible,
        "reasons": list(decision.reasons),
        "recoveries": list(decision.recoveries),
        "manifest_fingerprint": manifest.fingerprint,
        "dependency_ids": [item.dependency_id for item in manifest.expectations],
    }


def _changed_dependency_ids(event: Mapping[str, Any]) -> set[str]:
    payload = event.get("payload", {})
    proposition_value = payload.get("proposition")
    actor_id = event.get("actor_id")
    if not isinstance(proposition_value, Mapping) or not isinstance(actor_id, str):
        return set()
    proposition = _proposition(proposition_value)
    changed = {
        _proposition_slot(proposition, actor_id),
        _proposition_slot(proposition, "*"),
    }
    root_id = payload.get("root_id")
    if isinstance(root_id, str) and root_id:
        changed.add("evidence:" + root_id)
    supersedes = payload.get("supersedes", [])
    if isinstance(supersedes, list):
        changed.update("evidence:" + item for item in supersedes if isinstance(item, str) and item)
    return changed


def _candidate_events(trace: Mapping[str, Any], event_type: str,
                      bounds: tuple[int, int, str] | None = None) -> dict[str, Mapping[str, Any]]:
    result = {}
    for event in trace.get("events", []):
        if event.get("event_type") != event_type:
            continue
        if bounds is not None and not event_in_observation_window(event, bounds):
            continue
        request = event.get("payload", {}).get("exact_request")
        candidate_id = request.get("candidate_id") if isinstance(request, Mapping) else None
        if isinstance(candidate_id, str):
            result[candidate_id] = event
    return result


def analyze_trace(trace: Mapping[str, Any]) -> dict[str, Any]:
    """Build P0 structural diagnostics and draft D1-D6/N0-N4 classifications."""
    if trace.get("schema_version") == PROSPECTIVE_TRACE_SCHEMA:
        return analyze_prospective_trace(trace)
    validation = validate_trace(trace)
    p0_validation = validate_p0_trace(trace)
    bounds = trace.get("_analysis_measurement_bounds", observation_window_bounds(trace))
    def in_population(event: Mapping[str, Any]) -> bool:
        return event_in_observation_window(event, bounds)
    prepared_by_id = _candidate_events(trace, "k11.eac_action_prepared", bounds)
    decision_by_id = _candidate_events(trace, "k11.eac_execution_decision_attempted", bounds)
    native_by_id = _candidate_events(trace, "k11.eac_native_effect_entered", bounds)
    scoped_trace = trace
    if bounds is not None:
        scoped_trace = dict(trace)
        scoped_trace["events"] = [event for event in trace.get("events", [])
                                   if isinstance(event, Mapping) and in_population(event)]
    rows = []

    for candidate_id, prepared in prepared_by_id.items():
        request = prepared.get("payload", {}).get("exact_request", {})
        action = request.get("action", {}) if isinstance(request, Mapping) else {}
        tool_name = action.get("identity") if isinstance(action, Mapping) else None
        if tool_name not in PRIMARY_EFFECT_ACTIONS:
            continue
        row = {
            "candidate_id": candidate_id,
            "tool_name": tool_name,
            "D1": True,
            "D2": False,
            "D3": False,
            "D4": False,
            "D5": False,
            "D6": False,
            "taxonomy": None,
            "qc_state": None,
            "prepared_inside_window": True,
            "complete_dispositions_inside_window": False,
            "window_censored_preparations": 0,
        }
        decision = decision_by_id.get(candidate_id)
        positive_abandonment = derive_positive_disposition(scoped_trace, prepared)
        if decision is not None and positive_abandonment is not None:
            decision_precedes = _event_precedes(decision, positive_abandonment["marker"])
            if decision_precedes is True:
                positive_abandonment = None
            else:
                row["qc_state"] = (
                    "unsupported_path_observed" if decision_precedes is False
                    else "ordering_ambiguous"
                )
                rows.append(row)
                continue
        disposition = decision if decision is not None else (
            positive_abandonment["marker"] if positive_abandonment is not None else None
        )
        if disposition is None:
            if (bounds is not None and bounds[2] == "fixed_observation_horizon"
                    and isinstance(prepared.get("monotonic_ns"), int)
                    and prepared["monotonic_ns"] < bounds[1]):
                row.update({"qc_state": "observation_window_censored", "D1": True,
                            "D2": False, "D3": False, "D4": False, "D5": False, "D6": False,
                            "prepared_inside_window": True, "complete_dispositions_inside_window": False,
                            "window_censored_preparations": 1})
                try:
                    row["EAdm_prepare"] = replay_admissibility(
                        trace, prepared, cutoff_seq=prepared["seq"], replay_label=candidate_id + ":prepare"
                    )["admissible"]
                except Exception:
                    pass
                rows.append(row)
                continue
            row["qc_state"] = "disposition_unresolved"
            rows.append(row)
            continue
        prepare_ns = prepared.get("monotonic_ns")
        disposition_ns = disposition.get("monotonic_ns")
        if (not isinstance(prepare_ns, int) or not isinstance(disposition_ns, int)
                or disposition_ns <= prepare_ns):
            row["qc_state"] = "ordering_ambiguous"
            rows.append(row)
            continue
        row["prepare_to_disposition_ns"] = disposition_ns - prepare_ns
        row["complete_dispositions_inside_window"] = True
        if decision is not None:
            row["prepare_to_decision_ns"] = disposition_ns - prepare_ns

        try:
            eadm_prepare = replay_admissibility(
                trace, prepared, cutoff_seq=prepared["seq"], replay_label=candidate_id + ":prepare"
            )
            eadm_disposition = replay_admissibility(
                trace, prepared, cutoff_seq=disposition["seq"], replay_label=candidate_id + ":disposition"
            )
        except Exception as exc:
            row["qc_state"] = "offline_replay_failed"
            row["offline_replay_error_type"] = type(exc).__name__
            row["offline_replay_error"] = str(exc)
            rows.append(row)
            continue

        row["EAdm_prepare"] = eadm_prepare["admissible"]
        row["EAdm_disposition"] = eadm_disposition["admissible"]
        if eadm_prepare["admissible"] is not True:
            row["qc_state"] = "prepared_inadmissible_baseline"
            rows.append(row)
            continue
        row["D2"] = True

        actor_id = prepared.get("actor_id")
        interval_mutations = [
            event for event in trace.get("events", [])
            if event.get("event_type") == "k11.eac_evidence_ingested"
            and event.get("actor_id") == actor_id
            and prepared["monotonic_ns"] < event.get("monotonic_ns", -1) < disposition["monotonic_ns"]
            and in_population(event)
        ]
        if not interval_mutations:
            row["taxonomy"] = "N0"
            rows.append(row)
            continue
        row["D3"] = True

        dependency_ids = set(eadm_prepare["dependency_ids"])
        relevant = [
            event for event in interval_mutations
            if _changed_dependency_ids(event).intersection(dependency_ids)
        ]
        if not relevant:
            row["taxonomy"] = "N4"
            rows.append(row)
            continue
        row["D4"] = True
        row["relevant_mutation_event_ids"] = [event.get("event_id") for event in relevant]

        if eadm_disposition["admissible"] is True:
            row["taxonomy"] = "N3"
            rows.append(row)
            continue
        row["D5"] = True

        if positive_abandonment:
            row["taxonomy"] = "N1"
            row["disposition_kind"] = positive_abandonment["kind"]
            row["successor_candidate_ids"] = positive_abandonment["successor_candidate_ids"]
        elif decision is not None and (
            prepared.get("payload", {}).get("exact_request_digest")
            == decision.get("payload", {}).get("exact_request_digest")
        ):
            row["D6"] = True
            row["taxonomy"] = "N2"
            row["native_effect_entered"] = candidate_id in native_by_id
        else:
            row["qc_state"] = "disposition_unresolved"
        rows.append(row)

    denominators = {
        name: sum(row.get(name) is True for row in rows)
        for name in ("D1", "D2", "D3", "D4", "D5", "D6")
    }
    prepared_inside = len(rows)
    complete_inside = sum(row.get("complete_dispositions_inside_window") is True for row in rows)
    censored_count = sum(row.get("qc_state") == "observation_window_censored" for row in rows)
    taxonomy = {
        name: sum(row.get("taxonomy") == name for row in rows)
        for name in ("N0", "N1", "N2", "N3", "N4")
    }
    qc = {}
    for row in rows:
        state = row.get("qc_state")
        if state:
            qc[state] = qc.get(state, 0) + 1
    durations = [row["prepare_to_decision_ns"] for row in rows if "prepare_to_decision_ns" in row]
    disposition_durations = [
        row["prepare_to_disposition_ns"] for row in rows if "prepare_to_disposition_ns" in row
    ]
    return {
        "artifact_id": "minecraft-k11-trace-analysis-draft",
        "artifact_version": 1,
        "prevalence_inference_allowed": False,
        "run_id": trace.get("run_id"),
        "trace_validation": validation,
        "p0_trace_validation": p0_validation,
        "denominators": denominators,
        "prepared_inside_window": prepared_inside,
        "complete_dispositions_inside_window": complete_inside,
        "window_censored_preparations": censored_count,
        "censoring_fraction": (censored_count / prepared_inside if prepared_inside else 0.0),
        "taxonomy": taxonomy,
        "qc_states": qc,
        "prepare_to_decision_ns": durations,
        "prepare_to_disposition_ns": disposition_durations,
        "actions": rows,
    }


def analyze_prospective_trace(trace: Mapping[str, Any]) -> dict[str, Any]:
    """Analyze a prospectively cut /3 trace without changing the /2 classifier."""
    cut, bounds = _validate_measurement_cut(trace)
    legacy = dict(trace)
    legacy["schema_version"] = TRACE_SCHEMA_VERSION
    legacy["events"] = list(trace.get("events", []))
    legacy["_analysis_measurement_bounds"] = bounds
    # Bind every population decision to the authenticated cut, never to events
    # appended after a fixed horizon marker.
    result = analyze_trace(legacy)
    prospective_validation = _prospective_validation(trace, cut)
    prospective_p0_validation = _prospective_p0_validation(trace)
    result["artifact_id"] = PROSPECTIVE_ANALYSIS_ID
    result["artifact_version"] = PROSPECTIVE_ANALYSIS_VERSION
    result["analysis_identity"] = {"schema_version": PROSPECTIVE_ANALYSIS_ID + "/1"}
    result["measurement_cut"] = cut
    result["measurement_bounds"] = list(bounds)
    result["trace_validation"] = prospective_validation
    result["p0_trace_validation"] = prospective_p0_validation
    result["prevalence_inference_allowed"] = False
    result["measurement_analysis_eligible"] = (
        prospective_p0_validation.get("valid") is True
        and result["denominators"].get("D1", 0) > 0
        and not any(state in result["qc_states"] for state in
                    ("disposition_unresolved", "ordering_ambiguous", "offline_replay_failed",
                     "unsupported_path_observed"))
    )
    result["prospective_eligible"] = result["measurement_analysis_eligible"]
    return result


def _prospective_validation(trace: Mapping[str, Any], cut: Mapping[str, Any]) -> dict[str, Any]:
    return validate_trace(trace)


def _prospective_p0_validation(trace: Mapping[str, Any]) -> dict[str, Any]:
    return validate_p0_trace(trace)


def validate_p0_analysis(analysis: Mapping[str, Any], trace: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate that offline P0 replay diagnostics are complete and admissible."""
    if analysis.get("artifact_id") == PROSPECTIVE_ANALYSIS_ID:
        errors = []
        if analysis.get("artifact_version") != PROSPECTIVE_ANALYSIS_VERSION:
            errors.append("prospective analysis version is invalid")
        if analysis.get("prevalence_inference_allowed") is not False:
            errors.append("prospective analysis must forbid prevalence inference")
        if trace is not None:
            try:
                cut, _ = _validate_measurement_cut(trace)
                if analysis.get("measurement_cut") != cut:
                    errors.append("prospective analysis measurement cut does not match trace")
            except K11AnalysisError as exc:
                errors.append(str(exc))
            prospective = _prospective_validation(trace, cut if 'cut' in locals() else {})
            if prospective.get("valid") is not True:
                errors.append("prospective trace validation did not pass")
            expected_candidates = {e.get("payload", {}).get("exact_request", {}).get("candidate_id")
                                  for e in trace.get("events", [])
                                  if e.get("event_type") == "k11.eac_action_prepared"
                                  and event_in_observation_window(e, analysis.get("measurement_bounds"))
                                  and e.get("payload", {}).get("exact_request", {}).get("action", {}).get("identity") in PRIMARY_EFFECT_ACTIONS}
            observed_candidates = {r.get("candidate_id") for r in analysis.get("actions", []) if isinstance(r, Mapping)}
            if expected_candidates != observed_candidates:
                errors.append("prospective analysis does not cover primary actions exactly once")
        if analysis.get("measurement_analysis_eligible") is not True:
            errors.append("prospective measurement analysis is not eligible")
        if analysis.get("denominators", {}).get("D1", 0) < 1:
            errors.append("prospective analysis requires a primary action")
        if analysis.get("qc_states", {}).get("disposition_unresolved", 0):
            errors.append("natural unresolved disposition makes prospective analysis invalid")
        denominators = analysis.get("denominators", {})
        actions = [r for r in analysis.get("actions", []) if isinstance(r, Mapping)]
        for name in ("D1", "D2", "D3", "D4", "D5", "D6"):
            if denominators.get(name) != sum(row.get(name) is True for row in actions):
                errors.append(f"prospective {name} denominator is inconsistent")
        censored = [r for r in actions if r.get("qc_state") == "observation_window_censored"]
        if any(r.get("D1") is not True or any(r.get(name) is True for name in ("D2", "D3", "D4", "D5", "D6"))
               or r.get("taxonomy") is not None or "EAdm_disposition" in r
               or type(r.get("EAdm_prepare")) is not bool for r in censored):
            errors.append("prospective censored row invariants are invalid")
        if analysis.get("window_censored_preparations") != len(censored):
            errors.append("prospective censor count is inconsistent")
        prepared_count = analysis.get("prepared_inside_window")
        expected_fraction = len(censored) / prepared_count if prepared_count else 0.0
        if analysis.get("censoring_fraction") != expected_fraction:
            errors.append("prospective censor fraction is inconsistent")
        return {"valid": not errors, "errors": errors,
                "counts": {"primary_actions": analysis.get("denominators", {}).get("D1", 0)}}
    errors: list[str] = []
    embedded_trace_validation = analysis.get("p0_trace_validation", {})
    trace_validation = (
        validate_p0_trace(trace) if trace is not None
        else embedded_trace_validation if isinstance(embedded_trace_validation, Mapping) else {}
    )
    if not isinstance(trace_validation, Mapping):
        trace_validation = {}
        errors.append("P0 trace validation result is malformed")
    if analysis.get("analysis_error") is not None:
        errors.append("P0 analysis contains a top-level analysis error")
    if analysis.get("artifact_id") != "minecraft-k11-trace-analysis-draft":
        errors.append("P0 analysis artifact identity is invalid")
    if analysis.get("prevalence_inference_allowed") is not False:
        errors.append("P0 analysis must explicitly forbid prevalence inference")
    if trace_validation.get("valid") is not True:
        errors.append("P0 trace validation did not pass")
    denominators = analysis.get("denominators", {})
    if not isinstance(denominators, Mapping):
        denominators = {}
        errors.append("P0 analysis denominators are malformed")
    if type(denominators.get("D1")) is not int or denominators["D1"] < 1:
        errors.append("P0 analysis requires at least one D1 primary action")
    actions = analysis.get("actions", [])
    if not isinstance(actions, list):
        actions = []
        errors.append("P0 analysis actions are malformed")
    primary = [row for row in actions if isinstance(row, Mapping) and row.get("tool_name") in PRIMARY_EFFECT_ACTIONS]
    if trace is not None:
        bounds = observation_window_bounds(trace)
        trace_candidates = [
            event.get("payload", {}).get("exact_request", {}).get("candidate_id")
            for event in trace.get("events", [])
            if event.get("event_type") == "k11.eac_action_prepared"
            and event_in_observation_window(event, bounds)
            and event.get("payload", {}).get("exact_request", {}).get("action", {}).get("identity")
            in PRIMARY_EFFECT_ACTIONS
        ]
        analysis_candidates = [row.get("candidate_id") for row in primary]
        if (any(not isinstance(value, str) or not value for value in trace_candidates + analysis_candidates)
                or len(set(trace_candidates)) != len(trace_candidates)
                or len(set(analysis_candidates)) != len(analysis_candidates)
                or set(trace_candidates) != set(analysis_candidates)):
            errors.append("P0 analysis does not cover every primary trace candidate exactly once")
    for name in ("D1", "D2", "D3", "D4", "D5", "D6"):
        expected = sum(row.get(name) is True for row in primary)
        observed = denominators.get(name) if isinstance(denominators, Mapping) else None
        if type(observed) is not int or observed != expected:
            errors.append(f"P0 analysis {name} denominator is inconsistent with primary actions")
    if any(row.get("D1") is not True for row in primary):
        errors.append("P0 analysis contains a primary action outside D1")
    censored = [row for row in primary if row.get("qc_state") == "observation_window_censored"]
    replayed = [
        row for row in primary
        if row not in censored
        if type(row.get("EAdm_prepare")) is bool
        and type(row.get("EAdm_disposition")) is bool
    ]
    non_censored = [row for row in primary if row not in censored]
    if non_censored and not replayed:
        errors.append("P0 analysis lacks a replayed primary action")
    if len(replayed) != len(non_censored):
        errors.append("not all expected primary prepare/disposition replays completed")
    if any(type(row.get("EAdm_prepare")) is not bool for row in censored):
        errors.append("window-censored primary lacks prepare replay")
    forbidden = {"offline_replay_failed", "ordering_ambiguous", "disposition_unresolved"}
    for row in primary:
        if row.get("qc_state") in forbidden:
            errors.append(f"P0 analysis contains instrumentation-related QC state: {row['qc_state']}")
    return {"valid": not errors, "errors": errors, "trace_validation": trace_validation,
            "counts": {"primary_actions": len(primary), "replayed_primary_actions": len(replayed)}}
