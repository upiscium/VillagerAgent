from copy import deepcopy
import hashlib
import json

from benchmarks.common.eac import Proposition, PropositionKey
from benchmarks.minecraft.eac_runtime import MinecraftEACError, MinecraftEACRuntime
from benchmarks.minecraft.k11_analysis import (
    analyze_prospective_trace, analyze_trace, replay_admissibility, validate_p0_analysis,
)
from benchmarks.minecraft.k11_instrumentation import instrument_runtime
from benchmarks.minecraft.k11_trace import (
    K11TraceRecorder, K11TraceScope, PROSPECTIVE_TRACE_SCHEMA_VERSION,
    canonical_trace_bytes, use_scope,
)


def _mine(**kwargs):
    return {"status": True, "message": "ok"}


def _runtime(run_id: str, *, env_precheck=True):
    runtime = MinecraftEACRuntime(
        mode="dual_dag_advisory",
        run_id=run_id,
        env_prechecks={"MineBlock": lambda unused: env_precheck},
        audit_path=None,
    )
    trace = K11TraceRecorder(run_id)
    instrument_runtime(runtime, trace)
    return runtime, trace


def _prepare(runtime):
    return runtime.prepare_tool(
        "MineBlock",
        _mine,
        (),
        {
            "player_name": "Alice",
            "x": 1,
            "y": 2,
            "z": 3,
            "emotion": [],
            "murmur": "",
        },
    )


def _prepare_at(runtime, x, y, z):
    return runtime.prepare_tool(
        "MineBlock", _mine, (),
        {"player_name": "Alice", "x": x, "y": y, "z": z, "emotion": [], "murmur": ""},
    )


def _complete_zero_evidence_artifact(run_id="k11-analysis-zero-evidence"):
    runtime, trace = _runtime(run_id, env_precheck=False)
    scope = K11TraceScope(
        trace.run_id, task_id="task-1", actor_id="Alice",
        agent_step_id="step-1", tool_call_id="tool-1",
    )
    with use_scope(scope):
        trace.record("k11.agent_step_started", source="test")
        trace.record("k11.model_call_started", source="test", payload={"model_call_id": "model-1"})
        trace.record("k11.model_call_completed", source="test", payload={"model_call_id": "model-1"})
        trace.record("k11.tool_call_entered", source="test")
        try:
            runtime.execute_prepared(_prepare(runtime))
        except MinecraftEACError:
            trace.record("k11.tool_call_exited", source="test", payload={"outcome": "raised"})
        trace.record("k11.agent_step_completed", source="test")
    artifact = trace.artifact()
    opened_ns = min(event["monotonic_ns"] for event in artifact["events"]) - 1
    closed_ns = max(event["monotonic_ns"] for event in artifact["events"]) + 1
    horizon_seconds = 3600
    common = {
        "run_id": artifact["run_id"], "task_id": None, "actor_id": None,
        "agent_step_id": None, "tool_call_id": None, "source": "test", "thread_id": 1,
    }
    artifact["events"] = [
        {
            **common, "event_id": artifact["run_id"] + ":window-open",
            "event_type": "k11.observation_window_opened", "monotonic_ns": opened_ns,
            "payload": {"configured_horizon_seconds": horizon_seconds,
                        "horizon_monotonic_ns": opened_ns + horizon_seconds * 1_000_000_000},
        },
        *artifact["events"],
        {
            **common, "event_id": artifact["run_id"] + ":window-close",
            "event_type": "k11.observation_window_closed", "monotonic_ns": closed_ns,
            "payload": {"reason": "natural_runtime_terminal",
                        "configured_horizon_seconds": horizon_seconds,
                        "window_close_monotonic_ns": closed_ns, "shutdown_requested": False},
        },
    ]
    for seq, event in enumerate(artifact["events"], 1):
        event["seq"] = seq
    return artifact


def _window_after_first_prepare(artifact, *, reason="fixed_observation_horizon"):
    artifact = deepcopy(artifact)
    original = artifact["events"]
    pivot = next(
        index for index, event in enumerate(original)
        if event["event_type"] == "k11.eac_action_prepared"
    )
    opened_ns = 1_000
    horizon_ns = opened_ns + 1_000_000_000
    close_ns = horizon_ns if reason == "fixed_observation_horizon" else 10_000
    opened = {
        "seq": 1,
        "event_id": artifact["run_id"] + ":window-open",
        "event_type": "k11.observation_window_opened",
        "source": "test",
        "payload": {
            "configured_horizon_seconds": 1,
            "horizon_monotonic_ns": horizon_ns,
        },
        "monotonic_ns": opened_ns,
        "thread_id": 1,
        "run_id": artifact["run_id"],
        "task_id": None,
        "actor_id": None,
        "agent_step_id": None,
        "tool_call_id": None,
    }
    closed = {
        **opened,
        "event_id": artifact["run_id"] + ":window-close",
        "event_type": "k11.observation_window_closed",
        "payload": {
            "reason": reason,
            "configured_horizon_seconds": 1,
            "window_close_monotonic_ns": close_ns,
            "shutdown_requested": reason == "fixed_observation_horizon",
        },
        "monotonic_ns": close_ns,
    }
    rebuilt = [opened]
    for index, event in enumerate(original):
        event["monotonic_ns"] = (
            opened_ns + index + 1 if index <= pivot else close_ns + index + 1
        )
        rebuilt.append(event)
        if index == pivot:
            rebuilt.append(closed)
    for seq, event in enumerate(rebuilt, 1):
        event["seq"] = seq
    artifact["events"] = rebuilt
    return artifact


def test_k11_offline_replay_matches_positive_prepare_state() -> None:
    runtime, trace = _runtime("k11-replay-positive")
    scope = K11TraceScope(
        trace.run_id,
        task_id="task-1",
        actor_id="Alice",
        agent_step_id="step-1",
        tool_call_id="tool-1",
    )
    with use_scope(scope):
        runtime.ingest_target_observation(
            "Alice", "MineBlock", {"x": 1, "y": 2, "z": 3}
        )
        prepared = _prepare(runtime)

    artifact = trace.artifact()
    prepared_event = next(
        event for event in artifact["events"]
        if event["event_type"] == "k11.eac_action_prepared"
    )
    result = replay_admissibility(
        artifact,
        prepared_event,
        cutoff_seq=prepared_event["seq"],
        replay_label="positive",
    )

    assert result["admissible"] is True
    assert result["dependency_ids"]
    assert prepared.request.action.digest == prepared_event["payload"]["exact_request"]["action"]["digest"]


def test_k11_offline_analysis_recognizes_controlled_relevant_invalidation_fixture() -> None:
    """Development fixture only; this is not a natural K11 prevalence observation."""
    runtime, trace = _runtime("k11-replay-invalidated")
    scope = K11TraceScope(
        trace.run_id,
        task_id="task-1",
        actor_id="Alice",
        agent_step_id="step-1",
        tool_call_id="tool-1",
    )
    with use_scope(scope):
        runtime.ingest_target_observation(
            "Alice", "MineBlock", {"x": 1, "y": 2, "z": 3}
        )
        prepared = _prepare(runtime)
        negative = Proposition(
            PropositionKey("minecraft", "target_block_present", (1, 2, 3), "current"),
            polarity=False,
        )
        runtime._ingest_current_fluent(
            "Alice",
            negative,
            source="minecraft-visible-observation",
        )
        runtime.execute_prepared(prepared)

    analysis = analyze_trace(trace.artifact())

    assert analysis["prevalence_inference_allowed"] is False
    assert analysis["trace_validation"]["valid"] is True
    assert analysis["denominators"] == {
        "D1": 1,
        "D2": 1,
        "D3": 1,
        "D4": 1,
        "D5": 1,
        "D6": 1,
    }
    assert analysis["taxonomy"] == {
        "N0": 0,
        "N1": 0,
        "N2": 1,
        "N3": 0,
        "N4": 0,
    }
    action = analysis["actions"][0]
    assert action["EAdm_prepare"] is True
    assert action["EAdm_disposition"] is False
    assert action["native_effect_entered"] is True
    assert action["prepare_to_decision_ns"] > 0


def test_k11_offline_analysis_recognizes_controlled_positive_replacement_as_n1() -> None:
    """Development classifier fixture only; never a natural prevalence observation."""
    runtime, trace = _runtime("k11-replay-replacement")
    scope = K11TraceScope(
        trace.run_id, task_id="task-1", actor_id="Alice",
        agent_step_id="step-1", tool_call_id="tool-1",
    )
    successor_scope = K11TraceScope(
        trace.run_id, task_id="task-1", actor_id="Alice",
        agent_step_id="step-1", tool_call_id="tool-2",
    )
    with use_scope(scope):
        runtime.ingest_target_observation("Alice", "MineBlock", {"x": 1, "y": 2, "z": 3})
        original = _prepare_at(runtime, 1, 2, 3)
        runtime._ingest_current_fluent(
            "Alice",
            Proposition(
                PropositionKey("minecraft", "target_block_present", (1, 2, 3), "current"),
                polarity=False,
            ),
            source="minecraft-visible-observation",
        )
        trace.record(
            "k11.tool_call_exited",
            source="controlled-development-fixture",
            payload={"outcome": "returned"},
        )
    with use_scope(successor_scope):
        runtime.ingest_target_observation("Alice", "MineBlock", {"x": 4, "y": 5, "z": 6})
        successor = _prepare_at(runtime, 4, 5, 6)
        runtime.execute_prepared(successor)
    with use_scope(K11TraceScope(
        trace.run_id, task_id="task-1", actor_id="Alice", agent_step_id="step-1",
    )):
        trace.record(
            "k11.agent_step_completed",
            source="controlled-development-fixture",
            payload={"outcome": "returned"},
        )

    analysis = analyze_trace(trace.artifact())

    assert analysis["trace_validation"]["valid"] is True
    assert analysis["taxonomy"]["N1"] == 1
    original_row = next(row for row in analysis["actions"] if row["candidate_id"] == original.request.candidate_id)
    assert original_row["D1"] is True
    assert original_row["D2"] is True
    assert original_row["D3"] is True
    assert original_row["D4"] is True
    assert original_row["D5"] is True
    assert original_row["D6"] is False
    assert original_row["EAdm_prepare"] is True
    assert original_row["EAdm_disposition"] is False
    assert original_row["taxonomy"] == "N1"
    assert original_row["disposition_kind"] == "replacement"


def test_k11_offline_analysis_keeps_ambiguous_disappearance_unresolved() -> None:
    runtime, trace = _runtime("k11-replay-unresolved")
    runtime.ingest_target_observation("Alice", "MineBlock", {"x": 1, "y": 2, "z": 3})
    _prepare(runtime)

    analysis = analyze_trace(trace.artifact())

    assert analysis["taxonomy"]["N1"] == 0
    assert analysis["actions"][0]["qc_state"] == "disposition_unresolved"


def test_k11_offline_analysis_accepts_zero_evidence_baseline() -> None:
    artifact = _complete_zero_evidence_artifact()

    analysis = analyze_trace(artifact)

    assert analysis["trace_validation"]["valid"] is True
    assert analysis["p0_trace_validation"]["valid"] is True
    assert analysis["p0_trace_validation"]["counts"]["evidence_ingestions"] == 0
    assert analysis["denominators"]["D2"] == 0
    assert analysis["taxonomy"]["N2"] == 0
    assert analysis["taxonomy"] == {"N0": 0, "N1": 0, "N2": 0, "N3": 0, "N4": 0}
    assert analysis["actions"][0]["qc_state"] == "prepared_inadmissible_baseline"
    assert validate_p0_analysis(analysis, artifact)["valid"] is True


def test_k11_fixed_window_right_censors_prepare_without_in_window_disposition() -> None:
    runtime, trace = _runtime("k11-window-censored")
    scope = K11TraceScope(
        trace.run_id, task_id="task-1", actor_id="Alice",
        agent_step_id="step-1", tool_call_id="tool-1",
    )
    with use_scope(scope):
        trace.record("k11.agent_step_started", source="test")
        trace.record("k11.model_call_started", source="test", payload={"model_call_id": "model-1"})
        trace.record("k11.model_call_completed", source="test", payload={"model_call_id": "model-1"})
        trace.record("k11.tool_call_entered", source="test")
        runtime.ingest_target_observation("Alice", "MineBlock", {"x": 1, "y": 2, "z": 3})
        runtime.execute_prepared(_prepare(runtime))
        trace.record("k11.tool_call_exited", source="test")
        trace.record("k11.agent_step_completed", source="test")
    artifact = _window_after_first_prepare(trace.artifact())

    analysis = analyze_trace(artifact)

    assert analysis["trace_validation"]["valid"] is True
    assert analysis["denominators"] == {
        "D1": 1, "D2": 0, "D3": 0, "D4": 0, "D5": 0, "D6": 0,
    }
    assert analysis["prepared_inside_window"] == 1
    assert analysis["complete_dispositions_inside_window"] == 0
    assert analysis["window_censored_preparations"] == 1
    assert analysis["censoring_fraction"] == 1.0
    assert analysis["actions"][0]["qc_state"] == "observation_window_censored"
    assert validate_p0_analysis(analysis, artifact)["valid"] is True


def test_k11_natural_close_does_not_relabel_missing_disposition_as_censored() -> None:
    runtime, trace = _runtime("k11-window-natural-unresolved")
    runtime.ingest_target_observation("Alice", "MineBlock", {"x": 1, "y": 2, "z": 3})
    _prepare(runtime)
    artifact = _window_after_first_prepare(
        trace.artifact(), reason="natural_runtime_terminal",
    )

    analysis = analyze_trace(artifact)

    assert analysis["window_censored_preparations"] == 0
    assert analysis["actions"][0]["qc_state"] == "disposition_unresolved"


def test_k11_observation_window_uses_half_open_end_boundary() -> None:
    runtime, trace = _runtime("k11-window-half-open")
    runtime.ingest_target_observation("Alice", "MineBlock", {"x": 1, "y": 2, "z": 3})
    runtime.execute_prepared(_prepare(runtime))
    artifact = _window_after_first_prepare(trace.artifact())
    close_ns = next(
        event["monotonic_ns"] for event in artifact["events"]
        if event["event_type"] == "k11.observation_window_closed"
    )
    prepared = next(
        event for event in artifact["events"]
        if event["event_type"] == "k11.eac_action_prepared"
    )
    prepared["monotonic_ns"] = close_ns

    analysis = analyze_trace(artifact)

    assert analysis["prepared_inside_window"] == 0
    assert analysis["denominators"]["D1"] == 0


def test_k11_p0_analysis_rejects_trace_failure_even_without_analysis_error() -> None:
    runtime, trace = _runtime("k11-analysis-gate")
    runtime.ingest_target_observation("Alice", "MineBlock", {"x": 1, "y": 2, "z": 3})
    runtime.execute_prepared(_prepare(runtime))
    analysis = analyze_trace(trace.artifact())
    analysis["p0_trace_validation"] = {"valid": False, "errors": ["missing lifecycle"]}
    result = validate_p0_analysis(analysis)
    assert result["valid"] is False
    assert any("trace validation" in error for error in result["errors"])


def test_k11_p0_analysis_accepts_inadmissible_baseline_after_complete_replay() -> None:
    analysis = {
        "artifact_id": "minecraft-k11-trace-analysis-draft",
        "prevalence_inference_allowed": False,
        "p0_trace_validation": {"valid": True},
        "denominators": {"D1": 1, "D2": 0, "D3": 0, "D4": 0, "D5": 0, "D6": 0},
        "actions": [{
            "tool_name": "MineBlock", "D1": True,
            "EAdm_prepare": False, "EAdm_disposition": False,
            "qc_state": "prepared_inadmissible_baseline",
        }],
    }
    assert validate_p0_analysis(analysis)["valid"] is True


def test_k11_p0_analysis_rejects_non_boolean_or_missing_replay_results() -> None:
    analysis = {
        "artifact_id": "minecraft-k11-trace-analysis-draft",
        "prevalence_inference_allowed": False,
        "p0_trace_validation": {"valid": True},
        "denominators": {"D1": 1, "D2": 0, "D3": 0, "D4": 0, "D5": 0, "D6": 0},
        "actions": [{
            "tool_name": "MineBlock", "D1": True,
            "EAdm_prepare": True, "EAdm_disposition": None,
        }],
    }
    result = validate_p0_analysis(analysis)
    assert result["valid"] is False
    assert any("replay" in error for error in result["errors"])


def test_k11_p0_analysis_rejects_top_level_analysis_error() -> None:
    analysis = {
        "artifact_id": "minecraft-k11-trace-analysis-draft",
        "prevalence_inference_allowed": False,
        "analysis_error": "replay crashed",
        "p0_trace_validation": {"valid": True},
        "denominators": {"D1": 1, "D2": 0, "D3": 0, "D4": 0, "D5": 0, "D6": 0},
        "actions": [{
            "tool_name": "MineBlock", "D1": True,
            "EAdm_prepare": True, "EAdm_disposition": True,
        }],
    }
    result = validate_p0_analysis(analysis)
    assert result["valid"] is False
    assert any("top-level analysis error" in error for error in result["errors"])


def test_k11_p0_analysis_rejects_inconsistent_higher_denominator() -> None:
    analysis = {
        "artifact_id": "minecraft-k11-trace-analysis-draft",
        "prevalence_inference_allowed": False,
        "p0_trace_validation": {"valid": True},
        "denominators": {"D1": 1, "D2": 1, "D3": 0, "D4": 0, "D5": 0, "D6": 0},
        "actions": [{
            "tool_name": "MineBlock", "D1": True, "D2": False,
            "EAdm_prepare": False, "EAdm_disposition": False,
            "qc_state": "prepared_inadmissible_baseline",
        }],
    }
    result = validate_p0_analysis(analysis)
    assert result["valid"] is False
    assert any("D2 denominator" in error for error in result["errors"])


def test_k11_p0_analysis_rejects_dropped_primary_trace_candidate(monkeypatch) -> None:
    monkeypatch.setattr(
        "benchmarks.minecraft.k11_analysis.validate_p0_trace",
        lambda unused: {"valid": True},
    )
    trace = {"events": [{
        "event_type": "k11.eac_action_prepared",
        "payload": {"exact_request": {
            "candidate_id": candidate,
            "action": {"identity": "MineBlock"},
        }},
    } for candidate in ("candidate-1", "candidate-2")]}
    analysis = {
        "artifact_id": "minecraft-k11-trace-analysis-draft",
        "prevalence_inference_allowed": False,
        "denominators": {"D1": 1, "D2": 0, "D3": 0, "D4": 0, "D5": 0, "D6": 0},
        "actions": [{
            "candidate_id": "candidate-1", "tool_name": "MineBlock", "D1": True,
            "EAdm_prepare": False, "EAdm_disposition": False,
            "qc_state": "prepared_inadmissible_baseline",
        }],
    }
    result = validate_p0_analysis(analysis, trace)
    assert result["valid"] is False
    assert any("every primary trace candidate" in error for error in result["errors"])


def test_k11_p0_analysis_rejects_malformed_validation_structures() -> None:
    result = validate_p0_analysis({
        "artifact_id": "minecraft-k11-trace-analysis-draft",
        "prevalence_inference_allowed": False,
        "p0_trace_validation": [],
        "denominators": [],
        "actions": [],
    })
    assert result["valid"] is False


def _prospective(artifact):
    value = deepcopy(artifact)
    value["schema_version"] = "minecraft-k11-trace/3"
    events = value["events"]
    start = min(event["monotonic_ns"] for event in events)
    end = max(event["monotonic_ns"] for event in events) + 1
    in_window = [event for event in events if start <= event["monotonic_ns"] < end]
    value["measurement_cut"] = {
        "schema_version": "minecraft-k11-measurement-cut/1",
        "boundary": "[open,close)",
        "window_open_monotonic_ns": start, "window_close_monotonic_ns": end,
        "close_reason": "natural_runtime_terminal", "close_sequence": max(event["seq"] for event in events),
        "event_prefix_high_water_sequence": max(event["seq"] for event in events),
        "in_window_event_count": len(in_window),
        "in_window_event_digest": "sha256:" + hashlib.sha256(canonical_trace_bytes(in_window)).hexdigest(),
        "snapshot_valid": True, "snapshot_errors": [],
        "censoring_inventory": {"items": [], "retention": {"capacity": 256}},
    }
    return value


def test_k11_prospective_identity_and_v1_classification_compatibility() -> None:
    runtime, trace = _runtime("k11-prospective-compatibility")
    runtime.ingest_target_observation("Alice", "MineBlock", {"x": 1, "y": 2, "z": 3})
    runtime.execute_prepared(_prepare(runtime))
    legacy = analyze_trace(trace.artifact())
    prospective = analyze_prospective_trace(_prospective(trace.artifact()))
    assert prospective["artifact_id"] == "minecraft-k11-prospective-analysis"
    assert prospective["artifact_version"] == 1
    assert prospective["taxonomy"] == legacy["taxonomy"]
    assert prospective["denominators"] == legacy["denominators"]


def test_k11_recorder_generated_prospective_analysis_is_eligible() -> None:
    run_id = "k11-prospective-recorder-analysis"
    trace = K11TraceRecorder(
        run_id, schema_version=PROSPECTIVE_TRACE_SCHEMA_VERSION,
        measurement_identity={
            "run_id": run_id, "manifest_digest": "a" * 64,
            "execution_revision": "b" * 40,
            "runtime_digest": "sha256:" + "c" * 64,
            "premanifest_identity": "d" * 64,
            "validation_contract": "minecraft-k11-p0-validation-contract/2",
            "trace_schema": PROSPECTIVE_TRACE_SCHEMA_VERSION,
        },
    )
    runtime = MinecraftEACRuntime(
        mode="dual_dag_advisory", run_id=run_id,
        env_prechecks={"MineBlock": lambda unused: True}, audit_path=None,
    )
    instrument_runtime(runtime, trace)
    opened = 1
    horizon_seconds = 10_000_000
    trace.record(
        "k11.observation_window_opened", source="test", monotonic_ns=opened,
        payload={
            "configured_horizon_seconds": horizon_seconds,
            "horizon_monotonic_ns": opened + horizon_seconds * 1_000_000_000,
        },
    )
    scope = K11TraceScope(
        run_id, task_id="task-1", actor_id="Alice",
        agent_step_id="step-1", tool_call_id="tool-1",
    )
    with use_scope(scope):
        trace.record("k11.agent_step_started", source="test")
        trace.record("k11.model_call_started", source="test", payload={"model_call_id": "model-1"})
        trace.record("k11.model_call_completed", source="test", payload={"model_call_id": "model-1"})
        trace.record("k11.tool_call_entered", source="test")
        runtime.ingest_target_observation("Alice", "MineBlock", {"x": 1, "y": 2, "z": 3})
        runtime.execute_prepared(_prepare(runtime))
        trace.record("k11.tool_call_exited", source="test")
        trace.record("k11.agent_step_completed", source="test")
    closed = max(event["monotonic_ns"] for event in trace.events) + 1
    trace.record_and_cut(
        "k11.observation_window_closed", source="test", monotonic_ns=closed,
        reason="natural_runtime_terminal", window_open_monotonic_ns=opened,
        window_close_monotonic_ns=closed,
        payload={
            "reason": "natural_runtime_terminal",
            "configured_horizon_seconds": horizon_seconds,
            "window_close_monotonic_ns": closed, "shutdown_requested": False,
        },
        active_executions={
            "items": [], "retention": {
                "capacity": 128, "retained": 0,
                "truncated": False, "dropped_count": 0,
            },
        },
    )
    artifact = trace.artifact()
    analysis = analyze_trace(artifact)
    assert analysis["measurement_analysis_eligible"] is True
    assert validate_p0_analysis(analysis, artifact)["valid"] is True


def test_k11_prospective_cut_digest_mismatch_fails_closed() -> None:
    runtime, trace = _runtime("k11-prospective-cut-mismatch")
    runtime.ingest_target_observation("Alice", "MineBlock", {"x": 1, "y": 2, "z": 3})
    runtime.execute_prepared(_prepare(runtime))
    artifact = _prospective(trace.artifact())
    artifact["events"][0]["payload"] = {"tampered": True}
    try:
        analyze_trace(artifact)
    except ValueError as exc:
        assert "digest" in str(exc)
    else:
        raise AssertionError("measurement cut mismatch was accepted")


def test_k11_prospective_post_close_events_are_ignored_by_the_bound_cut() -> None:
    runtime, trace = _runtime("k11-prospective-post-close")
    runtime.ingest_target_observation("Alice", "MineBlock", {"x": 1, "y": 2, "z": 3})
    runtime.execute_prepared(_prepare(runtime))
    artifact = _prospective(trace.artifact())
    baseline = analyze_trace(artifact)
    post_close = deepcopy(artifact["events"][-1])
    post_close.update({"seq": artifact["measurement_cut"]["event_prefix_high_water_sequence"] + 1,
                       "event_id": "post-close-evidence",
                       "event_type": "k11.eac_evidence_ingested",
                       "monotonic_ns": artifact["measurement_cut"]["window_close_monotonic_ns"] + 1})
    artifact["events"].append(post_close)
    assert analyze_trace(artifact)["taxonomy"] == baseline["taxonomy"]


def test_k11_prospective_no_primary_and_natural_unresolved_are_ineligible() -> None:
    runtime, trace = _runtime("k11-prospective-unresolved")
    runtime.ingest_target_observation("Alice", "MineBlock", {"x": 1, "y": 2, "z": 3})
    _prepare(runtime)
    result = analyze_prospective_trace(_prospective(trace.artifact()))
    assert result["prospective_eligible"] is False
    assert result["actions"][0]["qc_state"] == "disposition_unresolved"

    empty = _prospective(trace.artifact())
    empty["events"] = [e for e in empty["events"] if e["event_type"] != "k11.eac_action_prepared"]
    high_water = max(e["seq"] for e in empty["events"])
    empty["measurement_cut"]["close_sequence"] = high_water
    empty["measurement_cut"]["event_prefix_high_water_sequence"] = high_water
    in_window = [e for e in empty["events"] if empty["measurement_cut"]["window_open_monotonic_ns"] <= e["monotonic_ns"] < empty["measurement_cut"]["window_close_monotonic_ns"]]
    empty["measurement_cut"]["in_window_event_count"] = len(in_window)
    empty["measurement_cut"]["in_window_event_digest"] = "sha256:" + hashlib.sha256(canonical_trace_bytes(in_window)).hexdigest()
    assert analyze_prospective_trace(empty)["prospective_eligible"] is False
