"""Synthetic future-cost gates only; these tests do not qualify live services."""
import json
import threading
import time

from benchmarks.minecraft.k11_hold_protocol import (
    CADENCE_NS, DEADLINE_NS, FixedPassiveScheduler, MAX_PAYLOAD_BYTES,
    SensorBinding, SensorResponse,
)
from benchmarks.minecraft.k11_hold_trace import validate_hold_trace
from benchmarks.minecraft.k11_hold_evidence import K11HoldEvidenceAdapter
from test_minecraft_k11_hold_evidence import (
    BRIDGE_A, BRIDGE_B, GEOMETRY_ID, SECRETS, SENSOR_ID, _adapter,
    _known_cell, _response_bytes, runtime_factory,
)


class FakeClock:
    def __init__(self):
        self._value = 0
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self._value

    def set(self, value):
        with self._lock:
            assert value >= self._value
            self._value = value

    def advance(self, delta):
        with self._lock:
            self._value += delta
            return self._value


def _bindings():
    return (
        SensorBinding("Alice", "sensor-A", "a" * 64, "b" * 64, "c" * 64,
                      "geometry-A", "d" * 64),
        SensorBinding("Bob", "sensor-B", "e" * 64, "f" * 64, "1" * 64,
                      "geometry-B", "2" * 64),
    )


def _nearest_rank_p95(values):
    ordered = sorted(values)
    return ordered[(95 * len(ordered) + 99) // 100 - 1]


def test_deterministic_4096_request_cost_census_has_bounded_queues_and_trace():
    """Pure fake-clock census: 2 actors x 8 independent labels x 256 slots."""
    labels = (0, 1, 10, 100, 1_000, 5_000, 30_000, 120_000)
    accepted = []
    scheduled = trace_events = byte_total = 0
    max_payload_bytes = max_request_bytes = max_in_flight = 0
    cpu_start_ns = time.process_time_ns()
    wall_start_ns = time.perf_counter_ns()
    for label in labels:
        clock = FakeClock()
        nonce_values = iter(f"{label * 512 + index:032x}" for index in range(512))

        def transport(request, receive):
            nonlocal max_payload_bytes, max_request_bytes, byte_total
            actor_offset = 0 if request["actor_id"] == "Alice" else 1
            latency_ns = 10_000_000 + ((request["tick_index"] * 31 + actor_offset) % 9) * 10_000_000
            body = b"s" * 256
            request_bytes = len(json.dumps(dict(request), sort_keys=True).encode("utf-8"))
            assert len(body) <= MAX_PAYLOAD_BYTES and request_bytes <= MAX_PAYLOAD_BYTES
            max_payload_bytes = max(max_payload_bytes, len(body))
            max_request_bytes = max(max_request_bytes, request_bytes)
            byte_total += len(body) + request_bytes
            clock.advance(latency_ns)
            receive(SensorResponse(body))

        scheduler = FixedPassiveScheduler(
            run_id="cost-census", window_id="window-256", bindings=_bindings(),
            window_duration_ns=256 * CADENCE_NS, transport=transport,
            ingest=lambda actor, request, payload, identity: None,
            clock_ns=clock, t0_ns=0,
            nonce_factory=lambda: next(nonce_values),
        )
        assert scheduler.tick_count == 256
        for tick in range(256):
            clock.set(tick * CADENCE_NS)
            scheduler.poll()
            max_in_flight = max(max_in_flight, scheduler.pending_count)
        clock.set(256 * CADENCE_NS)
        scheduler.poll()
        artifact = scheduler.trace_artifact()
        per_label = [row["controller_monotonic_ns"] - row["due_monotonic_ns"]
                     for row in artifact["events"]
                     if row["event"] == "qc" and row["status"] == "accepted"]
        assert len(per_label) == 512
        accepted.extend(per_label)
        scheduled += scheduler.tick_count * 2
        trace_events += len(artifact["events"])
        assert scheduler.counters["missed"] == scheduler.counters["overlap_skips"] == 0
        assert scheduler.counters["timeouts"] == 0
        assert scheduler.counters["completed"] == scheduler.counters["ingested"] == 512
        assert scheduler.pending_count == scheduler.queue_depth == 0
        assert scheduler.counters["queue_capacity"] == scheduler.counters["queue_depth_max"] == 0
        assert artifact["fatal_receipt"] is None and artifact["fatal_status"] is None
        assert artifact["scientifically_eligible"] is True and artifact["censored"] is False
        assert artifact["trace_retention"] == {
            "capacity": 9 * 512 + 2, "retained": 7 * 512 + 2,
            "truncated": False, "dropped_count": 0,
        }
        assert validate_hold_trace(artifact)["valid"] is True
    cpu_ns = time.process_time_ns() - cpu_start_ns
    wall_ns = time.perf_counter_ns() - wall_start_ns
    assert scheduled == len(accepted) == 4_096
    assert all(latency < DEADLINE_NS for latency in accepted)
    assert _nearest_rank_p95(accepted) <= 250_000_000
    assert trace_events == 7 * 4_096 + 2 * len(labels)
    assert max_payload_bytes == 256 and max_request_bytes <= MAX_PAYLOAD_BYTES
    assert max_in_flight <= 2
    print({"mock_only": True, "labels_ms": labels, "scheduled": scheduled,
           "verified_ingest_p95_ns": _nearest_rank_p95(accepted),
           "worst_verified_ingest_ns": max(accepted), "bytes_transferred": byte_total,
           "max_payload_bytes": max_payload_bytes, "max_request_bytes": max_request_bytes,
           "max_in_flight": max_in_flight, "queue_depth": 0,
           "cpu_ns": cpu_ns, "wall_ns": wall_ns})


def test_stateful_4096_mock_workload_uses_v2_adapter_and_scheduler(runtime_factory, caplog):
    """Stateful mock evidence path; elapsed work is never a live performance claim."""
    labels_ms = (0, 1, 10, 100, 1_000, 5_000, 30_000, 120_000)
    actors = ("Alice", "Bob")
    known_cell = _known_cell({"x": 0, "y": 0, "z": 0}, name="stone", registry_id=1)
    total_raw = total_known = total_transitions = total_noops = total_unknown = 0
    all_qc_ns = []
    callback_wall_ns = []
    callback_cpu_ns = []
    roots_by_label = []
    provenance_by_label = []
    max_payload = max_request = max_in_flight = max_queue = 0
    caplog.clear()
    for label_index, _delta_label_ms in enumerate(labels_ms):
        clock = FakeClock()
        runtime = runtime_factory(run_id=f"k11-stateful-cost-label-{label_index}", source_version=2)
        assert runtime.source_version == 2
        adapter = _adapter(runtime, clock, window=f"window-cost-{label_index}")
        assert isinstance(adapter, K11HoldEvidenceAdapter)
        actor_bindings = tuple(SensorBinding(
            actor, SENSOR_ID, adapter.sensor_digest, adapter.profile_digest,
            adapter.ingestion_digest, GEOMETRY_ID, adapter.geometry_digest,
        ) for actor in actors)
        nonce_values = iter(f"{label_index * 512 + index:032x}" for index in range(512))
        metrics = {"callback_wall": [], "callback_cpu": [], "max_in_flight": 0,
                   "max_payload": 0, "max_request": 0}
        scheduler = None

        def transport(request, receive):
            actor, tick = request["actor_id"], request["tick_index"]
            due_ns = tick * CADENCE_NS
            adapter.register_pending(actor, tick, request["nonce"], due_ns + DEADLINE_NS,
                                     scheduler.window_close_ns)
            metrics["max_in_flight"] = max(metrics["max_in_flight"], scheduler.pending_count)
            bridge_id = BRIDGE_A if actor == "Alice" else BRIDGE_B
            payload, response = _response_bytes(adapter, request, actor=actor, bridge_id=bridge_id,
                                                capture_seq=tick + 1, known=[known_cell])
            request_size = len(json.dumps(dict(request), sort_keys=True).encode("utf-8"))
            known_cells = [cell for cell in response["cells"] if cell["state"] != "unknown"]
            assert response["actor_id"] == actor and response["capture_seq"] == tick + 1
            assert len(known_cells) == 1
            assert known_cells[0]["coverage"]["eye_loaded"]["block_name"] == "air"
            assert known_cells[0]["coverage"]["target_loaded"]["block_name"] == "stone"
            assert known_cells[0]["coverage"]["path"] == []
            assert len(payload) <= MAX_PAYLOAD_BYTES and request_size <= MAX_PAYLOAD_BYTES
            metrics["max_payload"] = max(metrics["max_payload"], len(payload))
            metrics["max_request"] = max(metrics["max_request"], request_size)
            offset = 0 if actor == "Alice" else 1
            clock.advance(10_000_000 + ((tick * 31 + offset) % 9) * 10_000_000)
            wall = time.perf_counter_ns()
            cpu = time.process_time_ns()
            receive(SensorResponse(payload))
            metrics["callback_wall"].append(time.perf_counter_ns() - wall)
            metrics["callback_cpu"].append(time.process_time_ns() - cpu)

        scheduler = FixedPassiveScheduler(
            run_id=runtime.run_id, window_id=adapter.window_id, bindings=actor_bindings,
            window_duration_ns=256 * CADENCE_NS, transport=transport,
            ingest=adapter.ingest_callback, clock_ns=clock, t0_ns=0,
            nonce_factory=lambda: next(nonce_values),
        )
        peak_roots = peak_provenance = 0
        for tick in range(256):
            clock.set(tick * CADENCE_NS)
            scheduler.poll()
            semantic_roots = [root for root in runtime.authority._roots.values()
                              if root.proposition.key.namespace == "minecraft"
                              and root.proposition.key.predicate == "target_block_present"]
            assert len(semantic_roots) == len(runtime.authority._roots)
            peak_roots = max(peak_roots, len(semantic_roots))
            peak_provenance = max(peak_provenance, len(runtime.authority._provenance))
            max_queue = max(max_queue, scheduler.queue_depth)
        clock.set(256 * CADENCE_NS)
        scheduler.poll()
        artifact = scheduler.trace_artifact()
        assert validate_hold_trace(artifact)["valid"] is True
        assert artifact["fatal_receipt"] is None and artifact["fatal_status"] is None
        assert artifact["scientifically_eligible"] is True and artifact["censored"] is False
        counters = scheduler.counters
        assert counters["missed"] == counters["overlap_skips"] == counters["timeouts"] == 0
        assert counters["late_responses"] == counters["rejected"] == 0
        assert counters["completed"] == counters["ingested"] == 512
        assert counters["queue_capacity"] == counters["queue_depth_max"] == 0
        assert scheduler.pending_count == scheduler.queue_depth == 0
        assert metrics["max_in_flight"] <= 2 and scheduler.trace_capacity == 9 * 512 + 2
        assert artifact["trace_retention"]["truncated"] is False

        ledger = adapter.observation_ledger
        assert len(ledger) == 512
        assert [(row["tick_index"], row["actor_id"]) for row in ledger] == [
            (tick, actor) for tick in range(256) for actor in actors
        ]
        label_raw = label_known = label_transitions = label_noops = label_unknown = 0
        for capture in ledger:
            assert capture["capture_seq"] == capture["tick_index"] + 1
            assert capture["raw_cell_count"] == 75
            observations = capture["observations"]
            known = [item for item in observations if item["state"] != "unknown"]
            unknown = [item for item in observations if item["state"] == "unknown"]
            assert len(known) == 1 and len(unknown) == 74
            item = known[0]
            assert item["coordinate"] == [0, 0, 0] and item["block_name"] == "stone"
            transition = capture["tick_index"] == 0
            assert item["disposition"] == ("committed" if transition else "semantic_noop")
            assert capture["transition_count"] == int(transition)
            assert capture["semantic_noop_count"] == int(not transition)
            assert capture["unknown_count"] == 74
            label_raw += capture["raw_cell_count"]
            label_known += len(known)
            label_transitions += capture["transition_count"]
            label_noops += capture["semantic_noop_count"]
            label_unknown += capture["unknown_count"]
        assert label_raw == 512 * 75 and label_known == 512
        assert label_transitions == 2 and label_noops == 510 and label_unknown == 512 * 74
        assert label_known == label_transitions + label_noops
        assert peak_roots == peak_provenance == 2
        assert len(runtime.authority._roots) == len(runtime.authority._provenance) == 2
        assert runtime._sequence == len(runtime._k11_current_roots) == 2
        assert len(adapter.commit_ledger) == 2
        assert set(runtime.authority._roots) == {row["root_id"] for row in adapter.commit_ledger}
        assert adapter.bridge_ids == {"Alice": BRIDGE_A, "Bob": BRIDGE_B}
        roots_by_label.append(peak_roots)
        provenance_by_label.append(peak_provenance)
        qc_values = [row["controller_monotonic_ns"] - row["due_monotonic_ns"]
                     for row in artifact["events"]
                     if row["event"] == "qc" and row["status"] == "accepted"]
        assert len(qc_values) == 512 and all(value < DEADLINE_NS for value in qc_values)
        assert _nearest_rank_p95(qc_values) <= 250_000_000
        all_qc_ns.extend(qc_values)
        callback_wall_ns.extend(metrics["callback_wall"])
        callback_cpu_ns.extend(metrics["callback_cpu"])
        max_in_flight = max(max_in_flight, metrics["max_in_flight"])
        max_payload = max(max_payload, metrics["max_payload"])
        max_request = max(max_request, metrics["max_request"])
        total_raw += label_raw
        total_known += label_known
        total_transitions += label_transitions
        total_noops += label_noops
        total_unknown += label_unknown
        trace_text = json.dumps(artifact, sort_keys=True)
        diagnostics_text = json.dumps(adapter.qc_diagnostics, sort_keys=True)
        assert all(secret.decode("utf-8") not in trace_text for secret in SECRETS.values())
        assert all(secret.decode("utf-8") not in diagnostics_text for secret in SECRETS.values())

    p95 = _nearest_rank_p95(all_qc_ns)
    assert len(all_qc_ns) == 4_096 and all(value < DEADLINE_NS for value in all_qc_ns)
    assert p95 <= 250_000_000
    assert roots_by_label == provenance_by_label == [2] * len(labels_ms)
    assert total_raw == 8 * 512 * 75 and total_known == 4_096
    assert total_transitions == 16 and total_noops == 4_080
    assert total_unknown == 8 * 512 * 74
    assert max_payload <= MAX_PAYLOAD_BYTES and max_request <= MAX_PAYLOAD_BYTES
    assert max_in_flight <= 2 and max_queue == 0
    assert all(secret.decode("utf-8") not in caplog.text for secret in SECRETS.values())
    assert "request_hmac" not in caplog.text and "hmac_sha256" not in caplog.text
    assert "nonce" not in caplog.text
    print(json.dumps({
        "mock_only": True, "labels_ms": labels_ms, "requests": len(all_qc_ns),
        "raw_cells": total_raw, "known_cells": total_known,
        "transitions": total_transitions, "semantic_noops": total_noops,
        "unknown_cells": total_unknown, "peak_semantic_roots_per_label": max(roots_by_label),
        "peak_provenance_per_label": max(provenance_by_label),
        "fake_due_to_accepted_qc_p95_ns": p95,
        "fake_due_to_accepted_qc_worst_ns": max(all_qc_ns),
        "fake_p95_bound_ns": 250_000_000,
        "mock_callback_wall_p95_ns_diagnostic": _nearest_rank_p95(callback_wall_ns),
        "mock_callback_cpu_p95_ns_diagnostic": _nearest_rank_p95(callback_cpu_ns),
        "actual_callback_cost_scope": "mock_processing_only_not_live_bridge_or_server",
        "max_payload_bytes": max_payload, "max_in_flight": max_in_flight,
        "max_queue_depth": max_queue, "missed": 0, "overlap_skips": 0,
        "trace_valid": True, "secret_log_leaks": False,
    }, sort_keys=True, separators=(",", ":")))


def test_response_boundary_is_strict_and_bridge_clock_is_not_used_for_cost():
    clock = FakeClock()
    ingested = []
    callbacks = {}

    def transport(request, receive):
        if request["actor_id"] == "Alice":
            callbacks["alice"] = receive
        else:
            clock.set(DEADLINE_NS - 1)
            callbacks["alice"](SensorResponse(b"before-deadline", bridge_monotonic_ns=10**18))
            clock.set(DEADLINE_NS)
            receive(SensorResponse(b"at-deadline", bridge_monotonic_ns=10**18))

    scheduler = FixedPassiveScheduler(
        run_id="deadline-boundary", window_id="one-second", bindings=_bindings(),
        window_duration_ns=2 * CADENCE_NS, transport=transport,
        ingest=lambda actor, *args: ingested.append(actor), clock_ns=clock, t0_ns=0,
    )
    scheduler.poll()
    assert ingested == ["Alice"]
    assert scheduler.counters["late_responses"] == 1
    assert scheduler.counters["completed"] == 1
    artifact = scheduler.trace_artifact()
    assert any(row.get("bridge_monotonic_ns_diagnostic") == 10**18 for row in artifact["events"])
    assert validate_hold_trace(artifact)["valid"] is True
