from copy import deepcopy

import pytest

from benchmarks.common.eac import Proposition, PropositionKey
from benchmarks.minecraft.eac_runtime import MinecraftEACError, MinecraftEACRuntime
from benchmarks.minecraft.k11_instrumentation import instrument_runtime
from benchmarks.minecraft.k11_trace import (
    K11TraceRecorder,
    K11TraceScope,
    derive_positive_disposition,
    exact_request_digest,
    use_scope,
    valid_evidence_ingestion,
    validate_p0_trace,
    validate_trace,
    PROSPECTIVE_TRACE_SCHEMA_VERSION,
    TRACE_SCHEMA_VERSION,
    validate_prospective_trace,
)


def test_k11_recorder_selects_legacy_and_prospective_schema_and_retains_after_cut():
    legacy = K11TraceRecorder("legacy")
    legacy.record("k11.agent_step_started", source="test", monotonic_ns=1)
    assert legacy.artifact()["schema_version"] == TRACE_SCHEMA_VERSION

    recorder = K11TraceRecorder("prospective", schema_version=PROSPECTIVE_TRACE_SCHEMA_VERSION)
    recorder.record("k11.observation_window_opened", source="test", monotonic_ns=1,
                    payload={"configured_horizon_seconds": 1, "horizon_monotonic_ns": 1_000_000_001})
    cut = recorder.record_and_cut("k11.observation_window_closed", source="test",
        monotonic_ns=2, reason="natural_runtime_terminal",
        window_open_monotonic_ns=1, window_close_monotonic_ns=2)
    recorder.record("k11.agent_step_completed", source="test", monotonic_ns=3)
    assert cut["schema_version"] == PROSPECTIVE_TRACE_SCHEMA_VERSION
    assert len(recorder.artifact()["events"]) == 3
    assert len(cut["events"]) == 2
    assert validate_prospective_trace(cut)["valid"] is False  # no-primary is strict


def test_k11_prospective_cut_digest_is_immutable_and_rejects_late_preclose_event():
    recorder = K11TraceRecorder("prospective-late", schema_version=PROSPECTIVE_TRACE_SCHEMA_VERSION)
    recorder.record("k11.observation_window_opened", source="test", monotonic_ns=1,
                    payload={"configured_horizon_seconds": 1, "horizon_monotonic_ns": 1_000_000_001})
    recorder.record_and_cut("k11.observation_window_closed", source="test", monotonic_ns=3,
                            reason="natural_runtime_terminal", window_open_monotonic_ns=1,
                            window_close_monotonic_ns=3)
    recorder.record("k11.agent_step_completed", source="test", monotonic_ns=2)
    artifact = recorder.artifact()
    assert len(artifact["events"]) == 3
    validation = validate_prospective_trace(artifact)
    assert any("late pre-close" in error for error in validation["errors"])
    artifact["measurement_cut"]["in_window_event_digest"] = "sha256:" + "0" * 64
    assert validate_prospective_trace(artifact)["valid"] is False


def _mine(*, player_name, x, y, z, emotion=None, murmur=""):
    return {"status": True, "message": f"mined {x},{y},{z}"}


def _runtime(run_id="k11-test", *, env_precheck=True):
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


def _with_natural_window(artifact):
    artifact = deepcopy(artifact)
    events = artifact["events"]
    opened_ns = min(event["monotonic_ns"] for event in events) - 1
    closed_ns = max(event["monotonic_ns"] for event in events) + 1
    horizon_seconds = 3600
    common = {
        "run_id": artifact["run_id"],
        "task_id": None,
        "actor_id": None,
        "agent_step_id": None,
        "tool_call_id": None,
        "source": "test",
        "thread_id": 1,
    }
    opened = {
        **common,
        "event_id": artifact["run_id"] + ":window-open",
        "event_type": "k11.observation_window_opened",
        "monotonic_ns": opened_ns,
        "payload": {
            "configured_horizon_seconds": horizon_seconds,
            "horizon_monotonic_ns": opened_ns + horizon_seconds * 1_000_000_000,
        },
    }
    closed = {
        **common,
        "event_id": artifact["run_id"] + ":window-close",
        "event_type": "k11.observation_window_closed",
        "monotonic_ns": closed_ns,
        "payload": {
            "reason": "natural_runtime_terminal",
            "configured_horizon_seconds": horizon_seconds,
            "window_close_monotonic_ns": closed_ns,
            "shutdown_requested": False,
        },
    }
    artifact["events"] = [opened, *events, closed]
    for seq, event in enumerate(artifact["events"], 1):
        event["seq"] = seq
    return artifact


def _with_fixed_close_after(artifact, event_type):
    artifact = deepcopy(artifact)
    events = [
        event for event in artifact["events"]
        if not event["event_type"].startswith("k11.observation_window_")
    ]
    pivot = next(index for index, event in enumerate(events)
                 if event["event_type"] == event_type)
    opened_ns = 1_000
    closed_ns = opened_ns + 1_000_000_000
    common = {
        "run_id": artifact["run_id"], "task_id": None, "actor_id": None,
        "agent_step_id": None, "tool_call_id": None, "source": "test", "thread_id": 1,
    }
    opened = {
        **common, "event_id": artifact["run_id"] + ":window-open",
        "event_type": "k11.observation_window_opened", "monotonic_ns": opened_ns,
        "payload": {"configured_horizon_seconds": 1, "horizon_monotonic_ns": closed_ns},
    }
    closed = {
        **common, "event_id": artifact["run_id"] + ":window-close",
        "event_type": "k11.observation_window_closed", "monotonic_ns": closed_ns,
        "payload": {
            "reason": "fixed_observation_horizon", "configured_horizon_seconds": 1,
            "window_close_monotonic_ns": closed_ns, "shutdown_requested": True,
        },
    }
    rebuilt = [opened]
    for index, event in enumerate(events):
        event["monotonic_ns"] = (
            opened_ns + index + 1 if index <= pivot else closed_ns + index + 1
        )
        rebuilt.append(event)
        if index == pivot:
            rebuilt.append(closed)
    for seq, event in enumerate(rebuilt, 1):
        event["seq"] = seq
    artifact["events"] = rebuilt
    return artifact


def _complete_p0_artifact(run_id="k11-p0-complete"):
    runtime, trace = _runtime(run_id)
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
    return _with_natural_window(trace.artifact())


def _as_prospective(artifact, *, close_reason=None):
    identity = {
        "run_id": artifact["run_id"],
        "manifest_digest": "a" * 64,
        "execution_revision": "b" * 40,
        "runtime_digest": "sha256:" + "c" * 64,
        "premanifest_identity": "d" * 64,
        "validation_contract": "minecraft-k11-p0-validation-contract/2",
        "trace_schema": PROSPECTIVE_TRACE_SCHEMA_VERSION,
    }
    recorder = K11TraceRecorder(
        artifact["run_id"], schema_version=PROSPECTIVE_TRACE_SCHEMA_VERSION,
        measurement_identity=identity,
    )
    opened_ns = next(
        event["monotonic_ns"] for event in artifact["events"]
        if event["event_type"] == "k11.observation_window_opened"
    )
    for event in artifact["events"]:
        scope = K11TraceScope(
            artifact["run_id"], task_id=event.get("task_id"),
            actor_id=event.get("actor_id"), agent_step_id=event.get("agent_step_id"),
            tool_call_id=event.get("tool_call_id"),
        )
        if event["event_type"] == "k11.observation_window_closed":
            reason = close_reason or event["payload"]["reason"]
            payload = {**event["payload"], "reason": reason}
            prior = [
                row for row in artifact["events"]
                if row["monotonic_ns"] < event["monotonic_ns"]
            ]
            completed_steps = {
                row.get("agent_step_id") for row in prior
                if row["event_type"] == "k11.agent_step_completed"
            }
            active_items = [
                {
                    "execution_id": f"test-execution:{row['agent_step_id']}",
                    "task_id": str(row["task_id"]),
                    "actor_id": row["actor_id"],
                }
                for row in prior
                if row["event_type"] == "k11.agent_step_started"
                and row.get("agent_step_id") not in completed_steps
            ]
            recorder.record_and_cut(
                event["event_type"], source=event["source"], payload=payload,
                scope=scope, monotonic_ns=event["monotonic_ns"], reason=reason,
                window_open_monotonic_ns=opened_ns,
                window_close_monotonic_ns=event["monotonic_ns"],
                active_executions={
                    "items": active_items, "retention": {
                        "capacity": 128, "retained": len(active_items),
                        "truncated": False, "dropped_count": 0,
                    },
                },
            )
        else:
            recorder.record(
                event["event_type"], source=event["source"], payload=event["payload"],
                scope=scope, monotonic_ns=event["monotonic_ns"],
            )
    return recorder.artifact()


def test_k11_prospective_complete_and_fixed_censored_traces_validate():
    complete = _as_prospective(_complete_p0_artifact("prospective-complete"))
    assert validate_trace(complete)["valid"] is True
    assert validate_p0_trace(complete)["valid"] is True

    fixed = _with_fixed_close_after(
        _complete_p0_artifact("prospective-fixed-censor"),
        "k11.eac_action_prepared",
    )
    censored = _as_prospective(fixed)
    assert censored["measurement_cut"]["censoring_inventory"]["items"]
    assert validate_trace(censored)["valid"] is True
    assert validate_p0_trace(censored)["valid"] is True

    decision_before_close = _as_prospective(_with_fixed_close_after(
        _complete_p0_artifact("prospective-post-decision-cleanup"),
        "k11.eac_execution_decision_attempted",
    ))
    assert validate_p0_trace(decision_before_close)["valid"] is True

    native_before_close = _as_prospective(_with_fixed_close_after(
        _complete_p0_artifact("prospective-post-native-cleanup"),
        "k11.eac_native_effect_entered",
    ))
    assert any(
        item["kind"] == "native"
        for item in native_before_close["measurement_cut"]["censoring_inventory"]["items"]
    )
    assert validate_p0_trace(native_before_close)["valid"] is True

    natural_unresolved = _as_prospective(fixed, close_reason="natural_runtime_terminal")
    assert validate_trace(natural_unresolved)["valid"] is False


def test_k11_prospective_cut_rejects_window_and_snapshot_identity_tampering():
    artifact = _as_prospective(_complete_p0_artifact("prospective-tamper"))
    artifact["measurement_cut"]["window_open_monotonic_ns"] += 1
    assert any(
        "bounds do not match" in error
        for error in validate_trace(artifact)["errors"]
    )

    artifact = _as_prospective(_complete_p0_artifact("prospective-snapshot-tamper"))
    artifact["measurement_cut"]["identity"]["manifest_digest"] = "e" * 64
    assert any(
        "snapshot state digest" in error
        for error in validate_trace(artifact)["errors"]
    )


def test_k11_prospective_inventory_overflow_fails_closed():
    artifact = _as_prospective(_complete_p0_artifact("prospective-overflow"))
    retention = artifact["measurement_cut"]["prepared_requests"]["retention"]
    retention["dropped_count"] = 1
    retention["truncated"] = True

    result = validate_trace(artifact)

    assert result["valid"] is False
    assert "measurement cut prepared_requests inventory overflow" in result["errors"]


def test_k11_prospective_validation_rejects_malformed_payload_and_pending_cut_theft():
    artifact = _as_prospective(_complete_p0_artifact("prospective-malformed"))
    artifact["events"][-1]["payload"] = []
    assert validate_trace(artifact)["valid"] is False

    malformed_request = _as_prospective(_complete_p0_artifact("prospective-request"))
    next(
        event for event in malformed_request["events"]
        if event["event_type"] == "k11.eac_native_effect_completed"
    )["payload"]["exact_request"] = []
    assert validate_trace(malformed_request)["valid"] is False

    malformed_candidate = _as_prospective(
        _complete_p0_artifact("prospective-candidate")
    )
    next(
        event for event in malformed_candidate["events"]
        if event["event_type"] == "k11.eac_native_effect_completed"
    )["payload"]["exact_request"]["candidate_id"] = []
    assert validate_trace(malformed_candidate)["valid"] is False

    malformed_snapshot = _as_prospective(_complete_p0_artifact("prospective-snapshot"))
    malformed_snapshot["measurement_cut"]["active_executions"] = []
    assert validate_trace(malformed_snapshot)["valid"] is False

    malformed_snapshot_items = _as_prospective(
        _complete_p0_artifact("prospective-snapshot-items")
    )
    malformed_snapshot_items["measurement_cut"]["active_executions"]["items"] = None
    assert validate_trace(malformed_snapshot_items)["valid"] is False

    malformed_retention = _as_prospective(_complete_p0_artifact("prospective-retention"))
    malformed_retention["measurement_cut"]["prepared_requests"]["retention"][
        "dropped_count"
    ] = "zero"
    assert validate_trace(malformed_retention)["valid"] is False

    malformed_action = _as_prospective(_complete_p0_artifact("prospective-action"))
    next(
        event for event in malformed_action["events"]
        if event["event_type"] == "k11.eac_action_prepared"
    )["payload"]["exact_request"]["action"] = []
    assert validate_trace(malformed_action)["valid"] is False

    malformed_bounds = _as_prospective(_complete_p0_artifact("prospective-bounds"))
    malformed_bounds["measurement_cut"]["window_close_monotonic_ns"] = None
    assert validate_trace(malformed_bounds)["valid"] is False

    recorder = K11TraceRecorder(
        "prospective-pending", schema_version=PROSPECTIVE_TRACE_SCHEMA_VERSION,
    )
    pending = recorder.begin_record_and_cut(
        "k11.observation_window_closed", source="test", payload={},
        monotonic_ns=2, window_open_monotonic_ns=1,
        window_close_monotonic_ns=2,
    )
    with pytest.raises(ValueError, match="owned by another"):
        recorder.measurement_cut(
            window_open_monotonic_ns=1, window_close_monotonic_ns=2,
        )
    recorder.finalize_record_and_cut(pending)


def _complete_zero_evidence_p0_artifact(run_id="k11-p0-zero-evidence"):
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
    return _with_natural_window(trace.artifact())


def _complete_p0_abandonment_artifact(run_id="k11-p0-abandonment"):
    runtime, trace = _runtime(run_id)
    agent_scope = K11TraceScope(
        trace.run_id, task_id="task-1", actor_id="Alice", agent_step_id="step-1",
    )
    first_scope = K11TraceScope(
        trace.run_id, task_id="task-1", actor_id="Alice",
        agent_step_id="step-1", tool_call_id="tool-1",
    )
    second_scope = K11TraceScope(
        trace.run_id, task_id="task-1", actor_id="Alice",
        agent_step_id="step-1", tool_call_id="tool-2",
    )
    with use_scope(agent_scope):
        trace.record("k11.agent_step_started", source="test")
        trace.record("k11.model_call_started", source="test", payload={"model_call_id": "model-1"})
        trace.record("k11.model_call_completed", source="test", payload={"model_call_id": "model-1"})
    with use_scope(first_scope):
        trace.record("k11.tool_call_entered", source="test")
        runtime.ingest_target_observation("Alice", "MineBlock", {"x": 1, "y": 2, "z": 3})
        _prepare_at(runtime, 1, 2, 3)
        runtime._ingest_current_fluent(
            "Alice",
            Proposition(
                PropositionKey("minecraft", "target_block_present", (1, 2, 3), "current"),
                polarity=False,
            ),
            source="minecraft-visible-observation",
        )
        trace.record("k11.tool_call_exited", source="test", payload={"outcome": "returned"})
    with use_scope(second_scope):
        trace.record("k11.tool_call_entered", source="test")
        runtime.ingest_target_observation("Alice", "MineBlock", {"x": 4, "y": 5, "z": 6})
        successor = _prepare_at(runtime, 4, 5, 6)
        runtime.execute_prepared(successor)
        trace.record("k11.tool_call_exited", source="test")
    with use_scope(agent_scope):
        trace.record("k11.agent_step_completed", source="test", payload={"outcome": "returned"})
    return _with_natural_window(trace.artifact())


def test_k11_advisory_prepare_does_not_add_measurement_evaluation() -> None:
    runtime, trace = _runtime("k11-no-extra-eval")
    runtime.ingest_target_observation(
        "Alice", "MineBlock", {"x": 1, "y": 2, "z": 3}
    )

    original_evaluate = runtime.authority.evaluate
    calls = []

    def counted(candidate_id):
        calls.append(candidate_id)
        return original_evaluate(candidate_id)

    runtime.authority.evaluate = counted
    prepared = _prepare(runtime)

    assert calls == []
    runtime.execute_prepared(prepared)
    assert calls

    artifact = trace.artifact()
    assert validate_trace(artifact)["valid"] is True


def test_k11_trace_correlates_exact_request_decision_native_and_terminal() -> None:
    runtime, trace = _runtime("k11-correlate")
    runtime.ingest_target_observation(
        "Alice", "MineBlock", {"x": 1, "y": 2, "z": 3}
    )
    prepared = _prepare(runtime)
    result = runtime.execute_prepared(prepared)

    assert result["status"] is True
    artifact = trace.artifact()
    validation = validate_trace(artifact)
    assert validation["valid"] is True
    assert validation["counts"]["prepared"] == 1
    assert validation["counts"]["execution_decisions"] == 1
    assert validation["counts"]["native_entries"] == 1
    assert validation["counts"]["native_completions"] == 1
    assert validation["counts"]["terminals"] == 1
    assert validation["counts"]["evidence_ingestions"] >= 1

    by_type = {}
    for event in artifact["events"]:
        by_type.setdefault(event["event_type"], []).append(event)

    prepared_event = by_type["k11.eac_action_prepared"][0]
    decision_event = by_type["k11.eac_execution_decision_attempted"][0]
    native_event = by_type["k11.eac_native_effect_entered"][0]
    terminal_event = by_type["k11.eac_action_terminal"][0]

    digests = {
        event["payload"]["exact_request_digest"]
        for event in (prepared_event, decision_event, native_event, terminal_event)
    }
    assert len(digests) == 1
    assert decision_event["monotonic_ns"] > prepared_event["monotonic_ns"]

    request = prepared_event["payload"]["exact_request"]
    assert request["candidate_id"] == prepared.request.candidate_id
    assert request["attempt_id"] == prepared.request.attempt_id
    assert request["action"]["identity"] == "MineBlock"
    assert request["arguments"] == {"x": 1, "y": 2, "z": 3}
    assert request["target"] == {"x": 1, "y": 2, "z": 3}
    assert prepared_event["payload"]["exact_request_digest"] == exact_request_digest(request)


def test_k11_evidence_event_preserves_actor_visible_semantic_identity() -> None:
    runtime, trace = _runtime("k11-evidence")
    runtime.ingest_target_observation(
        "Alice", "MineBlock", {"x": 4, "y": 5, "z": 6}
    )

    evidence = [
        event for event in trace.artifact()["events"]
        if event["event_type"] == "k11.eac_evidence_ingested"
    ]
    assert len(evidence) == 1
    event = evidence[0]
    assert event["actor_id"] == "Alice"
    assert event["payload"]["visible_to"] == ["Alice"]
    assert event["payload"]["record_type"] == "direct_observation"
    assert event["payload"]["proposition"] == {
        "namespace": "minecraft",
        "predicate": "target_block_present",
        "arguments": [4, 5, 6],
        "temporal_scope": "current",
        "polarity": True,
    }


def test_k11_trace_validator_rejects_exact_request_substitution() -> None:
    runtime, trace = _runtime("k11-substitution")
    runtime.ingest_target_observation(
        "Alice", "MineBlock", {"x": 1, "y": 2, "z": 3}
    )
    prepared = _prepare(runtime)
    runtime.execute_prepared(prepared)

    artifact = deepcopy(trace.artifact())
    decision = next(
        event for event in artifact["events"]
        if event["event_type"] == "k11.eac_execution_decision_attempted"
    )
    decision["payload"]["exact_request_digest"] = "sha256:" + "0" * 64

    validation = validate_trace(artifact)
    assert validation["valid"] is False
    assert any("exact request changed" in error for error in validation["errors"])


def test_k11_trace_contains_no_online_natural_classification_labels() -> None:
    runtime, trace = _runtime("k11-no-online-labels")
    runtime.ingest_target_observation(
        "Alice", "MineBlock", {"x": 1, "y": 2, "z": 3}
    )
    runtime.execute_prepared(_prepare(runtime))

    serialized = str(trace.artifact())
    for label in ("N0", "N1", "N2", "N3", "N4", "reconsidered", "invalidated"):
        assert label not in serialized


def test_k11_p0_trace_rejects_high_level_only_artifact() -> None:
    artifact = {"schema_version": "minecraft-k11-trace/2", "run_id": "empty", "events": []}
    assert validate_p0_trace(artifact)["valid"] is False


def test_k11_p0_trace_rejects_primary_digest_mismatch() -> None:
    runtime, trace = _runtime("k11-p0-digest")
    runtime.ingest_target_observation("Alice", "MineBlock", {"x": 1, "y": 2, "z": 3})
    runtime.execute_prepared(_prepare(runtime))
    artifact = deepcopy(trace.artifact())
    terminal = next(event for event in artifact["events"] if event["event_type"] == "k11.eac_action_terminal")
    terminal["payload"]["exact_request_digest"] = "sha256:" + "f" * 64
    assert validate_p0_trace(artifact)["valid"] is False


def test_k11_p0_trace_accepts_complete_correlated_run() -> None:
    validation = validate_p0_trace(_complete_p0_artifact())
    assert validation["valid"] is True
    assert validation["counts"]["prepared"] == 1


def test_k11_p0_trace_accepts_complete_zero_evidence_run() -> None:
    validation = validate_p0_trace(_complete_zero_evidence_p0_artifact())

    assert validation["valid"] is True
    assert validation["counts"]["evidence_ingestions"] == 0
    assert validation["counts"]["prepared"] == 1


def test_k11_p0_trace_rejects_malformed_evidence_identity() -> None:
    artifact = _complete_p0_artifact("k11-p0-malformed-evidence")
    evidence = next(
        event for event in artifact["events"]
        if event["event_type"] == "k11.eac_evidence_ingested"
    )
    evidence["payload"]["visible_to"] = []

    validation = validate_p0_trace(artifact)

    assert validation["valid"] is False
    assert any("evidence ingestion" in error for error in validation["errors"])


def test_k11_non_stream_evidence_retains_replay_identity_without_stream_fields() -> None:
    artifact = _complete_p0_artifact("k11-p0-non-stream-evidence")
    evidence = next(
        event for event in artifact["events"]
        if event["event_type"] == "k11.eac_evidence_ingested"
    )
    for record_type in ("trusted_tool_result", "peer_report"):
        candidate = deepcopy(evidence)
        candidate["payload"]["record_type"] = record_type
        candidate["payload"]["source_stream_id"] = None
        candidate["payload"]["source_stream_revision"] = None
        candidate["payload"]["supersedes"] = []
        assert valid_evidence_ingestion(candidate, run_id=artifact["run_id"]) is True


def test_k11_stream_evidence_requires_matching_integer_revision() -> None:
    artifact = _complete_p0_artifact("k11-p0-stream-revision")
    evidence = next(
        event for event in artifact["events"]
        if event["event_type"] == "k11.eac_evidence_ingested"
        and event["payload"]["record_type"] == "direct_observation"
    )
    for revision, stream_revision in (("1", 1), (1, 2)):
        candidate = deepcopy(evidence)
        candidate["payload"]["revision"] = revision
        candidate["payload"]["source_stream_revision"] = stream_revision
        assert valid_evidence_ingestion(candidate, run_id=artifact["run_id"]) is False


def test_k11_evidence_rejects_noncanonical_proposition_argument() -> None:
    artifact = _complete_p0_artifact("k11-p0-evidence-argument")
    evidence = next(
        event for event in artifact["events"]
        if event["event_type"] == "k11.eac_evidence_ingested"
    )
    evidence["payload"]["proposition"]["arguments"] = [1.5]

    assert valid_evidence_ingestion(evidence, run_id=artifact["run_id"]) is False
    assert validate_p0_trace(artifact)["valid"] is False


def test_k11_p0_trace_rejects_missing_or_mismatched_run_identity() -> None:
    artifact = _complete_p0_artifact("k11-p0-run-identity")
    artifact["run_id"] = ""
    assert validate_p0_trace(artifact)["valid"] is False

    artifact = _complete_p0_artifact("k11-p0-run-identity")
    artifact["events"][1]["run_id"] = "another-run"
    validation = validate_p0_trace(artifact)
    assert validation["valid"] is False
    assert any("run identity" in error for error in validation["errors"])


def test_k11_p0_zero_evidence_run_still_rejects_malformed_lifecycle() -> None:
    artifact = _complete_zero_evidence_p0_artifact("k11-p0-zero-evidence-malformed")
    artifact["events"] = [
        event for event in artifact["events"]
        if event["event_type"] != "k11.tool_call_exited"
    ]

    validation = validate_p0_trace(artifact)

    assert validation["valid"] is False
    assert any("tool lifecycle" in error for error in validation["errors"])


def test_k11_p0_trace_requires_declared_observation_window() -> None:
    artifact = _complete_p0_artifact("k11-p0-window-required")
    artifact["events"] = [
        event for event in artifact["events"]
        if not event["event_type"].startswith("k11.observation_window_")
    ]
    for seq, event in enumerate(artifact["events"], 1):
        event["seq"] = seq

    assert validate_trace(artifact)["valid"] is True
    validation = validate_p0_trace(artifact)
    assert validation["valid"] is False
    assert any("observation window" in error for error in validation["errors"])


def test_k11_trace_rejects_natural_close_at_fixed_horizon() -> None:
    artifact = _complete_p0_artifact("k11-natural-at-horizon")
    opened = artifact["events"][0]
    closed = artifact["events"][-1]
    horizon_ns = opened["payload"]["horizon_monotonic_ns"]
    closed["monotonic_ns"] = horizon_ns
    closed["payload"]["window_close_monotonic_ns"] = horizon_ns

    validation = validate_trace(artifact)

    assert validation["valid"] is False
    assert any("natural observation close" in error for error in validation["errors"])


def test_k11_trace_accepts_disposition_with_cleanup_after_fixed_close() -> None:
    artifact = _with_fixed_close_after(
        _complete_p0_artifact("k11-p0-cross-window-cleanup"),
        "k11.eac_native_effect_entered",
    )

    assert validate_trace(artifact)["valid"] is True
    assert validate_p0_trace(artifact)["valid"] is True


def test_k11_trace_fails_closed_on_malformed_post_window_terminal() -> None:
    artifact = _with_fixed_close_after(
        _complete_p0_artifact("k11-p0-malformed-cross-window"),
        "k11.eac_native_effect_entered",
    )
    terminal = next(
        event for event in artifact["events"]
        if event["event_type"] == "k11.eac_action_terminal"
    )
    terminal["payload"] = None

    validation = validate_p0_trace(artifact)

    assert validation["valid"] is False
    assert any("terminal" in error for error in validation["errors"])


def test_k11_p0_trace_accepts_positive_replacement_disposition() -> None:
    validation = validate_p0_trace(_complete_p0_abandonment_artifact())

    assert validation["valid"] is True
    assert validation["counts"]["prepared"] == 2
    assert validation["counts"]["positive_abandonments"] == 1


def test_k11_p0_trace_rejects_disappearance_without_positive_tool_exit() -> None:
    artifact = _complete_p0_abandonment_artifact("k11-p0-missing-tool-exit")
    artifact["events"] = [
        event for event in artifact["events"]
        if not (event["event_type"] == "k11.tool_call_exited"
                and event.get("tool_call_id") == "tool-1")
    ]

    validation = validate_p0_trace(artifact)

    assert validation["valid"] is False
    assert any("disposition" in error for error in validation["errors"])


def test_k11_p0_trace_rejects_raised_tool_exit_as_positive_disposition() -> None:
    artifact = _complete_p0_abandonment_artifact("k11-p0-raised-tool-exit")
    first_exit = next(
        event for event in artifact["events"]
        if event["event_type"] == "k11.tool_call_exited" and event.get("tool_call_id") == "tool-1"
    )
    first_exit["payload"]["outcome"] = "raised"

    validation = validate_p0_trace(artifact)

    assert validation["valid"] is False
    assert any("disposition" in error for error in validation["errors"])


def test_k11_p0_trace_rejects_delayed_decision_after_positive_replacement() -> None:
    artifact = _complete_p0_abandonment_artifact("k11-p0-delayed-decision")
    original = next(
        event for event in artifact["events"]
        if event["event_type"] == "k11.eac_action_prepared" and event.get("tool_call_id") == "tool-1"
    )
    delayed = next(
        deepcopy(event) for event in artifact["events"]
        if event["event_type"] == "k11.eac_execution_decision_attempted"
    )
    delayed["seq"] = max(event["seq"] for event in artifact["events"]) + 1
    delayed["monotonic_ns"] = max(event["monotonic_ns"] for event in artifact["events"]) + 1
    delayed["event_id"] = "k11-p0-delayed-decision:k11:delayed"
    delayed["actor_id"] = original["actor_id"]
    delayed["task_id"] = original["task_id"]
    delayed["agent_step_id"] = original["agent_step_id"]
    delayed["tool_call_id"] = original["tool_call_id"]
    delayed["payload"]["exact_request"] = deepcopy(original["payload"]["exact_request"])
    delayed["payload"]["exact_request_digest"] = original["payload"]["exact_request_digest"]
    close = next(
        event for event in artifact["events"]
        if event["event_type"] == "k11.observation_window_closed"
    )
    close["monotonic_ns"] += 2
    close["payload"]["window_close_monotonic_ns"] = close["monotonic_ns"]
    delayed["monotonic_ns"] = close["monotonic_ns"] - 1
    artifact["events"].insert(artifact["events"].index(close), delayed)
    for seq, event in enumerate(artifact["events"], 1):
        event["seq"] = seq

    validation = validate_p0_trace(artifact)

    assert validation["valid"] is False
    assert any("decision after positive abandonment" in error for error in validation["errors"])


def test_k11_positive_disposition_does_not_treat_cross_step_successor_as_replacement() -> None:
    artifact = _complete_p0_abandonment_artifact("k11-p0-cross-step-successor")
    original = next(
        event for event in artifact["events"]
        if event["event_type"] == "k11.eac_action_prepared" and event.get("tool_call_id") == "tool-1"
    )
    successor = next(
        event for event in artifact["events"]
        if event["event_type"] == "k11.eac_action_prepared" and event.get("tool_call_id") == "tool-2"
    )
    successor["agent_step_id"] = "step-2"

    disposition = derive_positive_disposition(artifact, original)

    assert disposition is not None
    assert disposition["kind"] == "cancellation"
    assert disposition["successor_candidate_ids"] == []


def test_k11_trace_validator_fails_closed_on_malformed_event_payload() -> None:
    artifact = _complete_p0_artifact("k11-p0-malformed-payload")
    artifact["events"][0]["payload"] = None

    validation = validate_p0_trace(artifact)

    assert validation["valid"] is False
    assert any("payload is malformed" in error for error in validation["errors"])


def test_k11_p0_trace_rejects_cross_actor_lifecycle_pairing() -> None:
    artifact = _complete_p0_artifact("k11-p0-cross-actor")
    completed = next(
        event for event in artifact["events"]
        if event["event_type"] == "k11.agent_step_completed"
    )
    completed["actor_id"] = "Bob"
    validation = validate_p0_trace(artifact)
    assert validation["valid"] is False
    assert any("agent lifecycle" in error for error in validation["errors"])


def test_k11_p0_trace_rejects_additional_dangling_model_call() -> None:
    artifact = _complete_p0_artifact("k11-p0-dangling-model")
    started = next(
        deepcopy(event) for event in artifact["events"]
        if event["event_type"] == "k11.model_call_started"
    )
    started["seq"] = max(event["seq"] for event in artifact["events"]) + 1
    started["event_id"] = "k11-p0-dangling-model:k11:dangling"
    started["payload"]["model_call_id"] = "model-dangling"
    artifact["events"].append(started)

    validation = validate_p0_trace(artifact)
    assert validation["valid"] is False
    assert any("model lifecycle" in error for error in validation["errors"])


def test_k11_p0_trace_rejects_cross_scope_eac_correlation() -> None:
    artifact = _complete_p0_artifact("k11-p0-cross-scope")
    decision = next(
        event for event in artifact["events"]
        if event["event_type"] == "k11.eac_execution_decision_attempted"
    )
    decision["actor_id"] = "Bob"

    validation = validate_p0_trace(artifact)
    assert validation["valid"] is False
    assert any("not correlated" in error for error in validation["errors"])


def test_k11_p0_trace_rejects_orphan_eac_event() -> None:
    artifact = _complete_p0_artifact("k11-p0-orphan")
    orphan = next(
        deepcopy(event) for event in artifact["events"]
        if event["event_type"] == "k11.eac_execution_decision_attempted"
    )
    orphan["seq"] = max(event["seq"] for event in artifact["events"]) + 1
    orphan["event_id"] = "k11-p0-orphan:k11:orphan"
    orphan["payload"]["exact_request"]["candidate_id"] = "orphan-candidate"
    orphan["payload"]["exact_request_digest"] = exact_request_digest(
        orphan["payload"]["exact_request"]
    )
    artifact["events"].append(orphan)

    validation = validate_p0_trace(artifact)
    assert validation["valid"] is False
    assert any("has no preparation" in error for error in validation["errors"])
