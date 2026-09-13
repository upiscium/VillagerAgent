"""Deterministic in-process calibration for K11 trace overhead.

This is not a natural/prevalence cohort.  It compares the same EAC operation
with tracing disabled and enabled using a no-network native callable and reports
only instrumentation overhead diagnostics.
"""
from __future__ import annotations

import statistics
import time
from typing import Any

from benchmarks.minecraft.eac_runtime import MinecraftEACRuntime
from benchmarks.minecraft.k11_instrumentation import instrument_runtime
from benchmarks.minecraft.k11_trace import K11TraceRecorder


def _native_mine(*, player_name, x, y, z, emotion=None, murmur=""):
    return {"status": True, "message": "calibration"}


def _runtime(run_id: str) -> MinecraftEACRuntime:
    return MinecraftEACRuntime(
        mode="dual_dag_advisory",
        run_id=run_id,
        env_prechecks={"MineBlock": lambda unused: True},
        audit_path=None,
    )


def _one_series(*, iterations: int, traced: bool) -> dict[str, Any]:
    runtime = _runtime("k11-calibration-traced" if traced else "k11-calibration-baseline")
    trace = K11TraceRecorder(runtime.run_id) if traced else None
    if trace is not None:
        instrument_runtime(runtime, trace)

    prepare_ns: list[int] = []
    execute_ns: list[int] = []
    total_ns: list[int] = []
    for index in range(iterations):
        arguments = {"x": index + 1000, "y": 64, "z": 0}
        runtime.ingest_target_observation("Alice", "MineBlock", arguments)
        start = time.perf_counter_ns()
        prepared = runtime.prepare_tool(
            "MineBlock",
            _native_mine,
            (),
            {
                "player_name": "Alice",
                **arguments,
                "emotion": [],
                "murmur": "",
            },
        )
        prepared_at = time.perf_counter_ns()
        runtime.execute_prepared(prepared)
        completed_at = time.perf_counter_ns()
        prepare_ns.append(prepared_at - start)
        execute_ns.append(completed_at - prepared_at)
        total_ns.append(completed_at - start)

    measured_intervals: list[int] = []
    trace_validation = None
    if trace is not None:
        artifact = trace.artifact()
        prepared_events = {}
        decision_events = {}
        for event in artifact["events"]:
            request = event.get("payload", {}).get("exact_request")
            candidate_id = request.get("candidate_id") if isinstance(request, dict) else None
            if not candidate_id:
                continue
            if event["event_type"] == "k11.eac_action_prepared":
                prepared_events[candidate_id] = event
            elif event["event_type"] == "k11.eac_execution_decision_attempted":
                decision_events[candidate_id] = event
        for candidate_id, prepared_event in prepared_events.items():
            decision = decision_events.get(candidate_id)
            if decision is not None:
                measured_intervals.append(decision["monotonic_ns"] - prepared_event["monotonic_ns"])
        from benchmarks.minecraft.k11_trace import validate_trace
        trace_validation = validate_trace(artifact)

    def summary(values: list[int]) -> dict[str, float | int | None]:
        if not values:
            return {"count": 0, "median_ns": None, "min_ns": None, "max_ns": None}
        return {
            "count": len(values),
            "median_ns": int(statistics.median(values)),
            "min_ns": min(values),
            "max_ns": max(values),
        }

    return {
        "traced": traced,
        "prepare": summary(prepare_ns),
        "execute": summary(execute_ns),
        "total": summary(total_ns),
        "prepare_to_decision_marker": summary(measured_intervals),
        "trace_validation": trace_validation,
    }


def measure_inprocess_overhead(*, iterations: int = 100) -> dict[str, Any]:
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 10:
        raise ValueError("K11 calibration iterations must be an integer >= 10")
    baseline = _one_series(iterations=iterations, traced=False)
    traced = _one_series(iterations=iterations, traced=True)

    def delta(section: str) -> int:
        return int(traced[section]["median_ns"] - baseline[section]["median_ns"])

    return {
        "artifact_id": "minecraft-k11-p0-inprocess-overhead-calibration",
        "artifact_version": 1,
        "study_phase": "K11-P0-instrumentation-validation",
        "prevalence_inference_allowed": False,
        "iterations_per_condition": iterations,
        "network_effects_used": False,
        "audit_path_used": False,
        "baseline": baseline,
        "traced": traced,
        "median_added_ns": {
            "prepare": delta("prepare"),
            "execute": delta("execute"),
            "total": delta("total"),
        },
        "interpretation": "Diagnostic incremental instrumentation cost only; not a natural latency estimate.",
    }
