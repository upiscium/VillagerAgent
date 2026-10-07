"""Pure scheduler, ledger, and trace-boundary tests; no Minecraft runtime is used."""
import threading
import copy
from dataclasses import dataclass
from itertools import count

import pytest

from benchmarks.common.eac import ActionRef, ExactRequest
from benchmarks.common.eac.canonical import canonical_bytes as common_canonical_bytes
from benchmarks.minecraft.eac_runtime import MinecraftPreparedAction
from benchmarks.minecraft.k11_hold_protocol import (
    CADENCE_NS, DEADLINE_NS, FixedPassiveScheduler, K11CommitReceipt,
    K11FatalSensorIngest, K11RejectedBeforeMutation, K11SensorAdmissionGate,
    MAX_PAYLOAD_BYTES,
    POLICY, REQUEST_KEYS, REQUEST_SCHEMA, SensorBinding, SensorResponse,
    TRACE_SCHEMA, snapshot_canonical_bytes,
)
from benchmarks.minecraft.k11_hold_trace import validate_hold_trace, validate_request_payload


class FakeClock:
    def __init__(self, value=0):
        self._value = value
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self._value

    def set(self, value):
        with self._lock:
            assert value >= self._value
            self._value = value


def bindings():
    return (
        SensorBinding("Alice", "sensor-A", "a" * 64, "b" * 64, "c" * 64,
                      "geometry-A", "d" * 64),
        SensorBinding("Bob", "sensor-B", "e" * 64, "f" * 64, "1" * 64,
                      "geometry-B", "2" * 64),
    )


def scheduler(clock, transport, ingest=lambda *args: None, *, duration=4 * CADENCE_NS):
    nonces = count()
    return FixedPassiveScheduler(
        run_id="run-fixed", window_id="window-fixed", bindings=bindings(),
        window_duration_ns=duration, transport=transport, ingest=ingest,
        clock_ns=clock, t0_ns=0,
        nonce_factory=lambda: f"{next(nonces):032x}",
    )


def success(receive, *, payload=b"passive", bridge_ns=None, identity=None):
    receive(SensorResponse(payload, bridge_monotonic_ns=bridge_ns,
                           request_identity=identity))


def commit_cell(index=0, *, actor="Alice"):
    tag = actor[0]
    return {
        "cell_index": index, "coordinate": [index, 64, -2],
        "root_id": f"root-{tag}-{index}",
        "provenance_id": f"provenance-{tag}-{index}",
        "polarity": True, "supersedes": [], "ingest_sequence": index + 1,
    }


def fatal_receipt(*, cells=(), phase="runtime_commit", actor="Alice"):
    failing = len(cells)
    return {
        "status": "partial_commit_fatal" if cells else "sensor_ingest_fatal",
        "actor_id": actor, "tick_index": 0, "capture_seq": 17,
        "committed_cells": list(cells), "failing_cell_index": failing,
        "failing_coordinate": [failing, 64, -2], "phase": phase,
        "orphan_provenance_id": None, "orphan_provenance": False,
    }


def raw_observations():
    return [
        ({"cell_index": index, "coordinate": [index, 64, -2],
          "state": "known_non_air", "block_name": "stone", "registry_id": 1}
         if index == 0 else
         {"cell_index": index, "coordinate": [index, 64, -2],
          "state": "unknown", "unknown_reason": "unloaded_target"})
        for index in range(75)
    ]


def test_snapshot_canonical_domain_and_request_allowlist():
    request = {
        "schema": REQUEST_SCHEMA, "run_id": "run", "window_id": "window",
        "actor_id": "Alice", "tick_index": 0, "nonce": "0" * 32,
        "sensor_id": "sensor-A", "sensor_digest": "a" * 64,
        "profile_digest": "b" * 64, "ingestion_digest": "c" * 64,
        "geometry_id": "geometry-A", "geometry_digest": "d" * 64,
    }
    assert validate_request_payload(request)
    assert snapshot_canonical_bytes(request) == common_canonical_bytes(request)
    with pytest.raises(ValueError, match="byte bound"):
        snapshot_canonical_bytes({"payload": "x" * MAX_PAYLOAD_BYTES})
    recursive = []
    recursive.append(recursive)
    with pytest.raises(ValueError, match="recursive"):
        snapshot_canonical_bytes(recursive)
    assert not validate_request_payload({**request, "target": "forbidden"})


def test_two_actor_absolute_cadence_and_missed_ticks_are_never_replayed():
    clock = FakeClock()
    sent = []

    def transport(request, receive):
        sent.append((request["actor_id"], request["tick_index"]))
        success(receive)

    instance = scheduler(clock, transport, duration=3 * CADENCE_NS)
    instance.poll()
    clock.set(1_600_000_000)
    instance.poll()
    assert sent == [("Alice", 0), ("Bob", 0)]
    assert instance.counters["missed"] == 2
    clock.set(2 * CADENCE_NS)
    instance.poll()
    assert sent == [("Alice", 0), ("Bob", 0), ("Alice", 2), ("Bob", 2)]
    trace = instance.trace_artifact()
    assert trace["schema"] == TRACE_SCHEMA and trace["policy"] == POLICY
    assert validate_hold_trace(trace)["valid"] is True


@pytest.mark.parametrize("observations", [None, []])
def test_empty_receipt_without_observations_is_not_a_commit_or_semantic_noop(observations):
    clock = FakeClock()
    receipt = K11CommitReceipt((), [], "Alice", 0, 17, observations=observations)
    instance = scheduler(clock, lambda request, receive: success(receive),
                         ingest=lambda actor, *args: receipt if actor == "Alice" else None,
                         duration=CADENCE_NS)
    instance.poll()
    trace = instance.trace_artifact()
    qc = next(row for row in trace["events"] if row["event"] == "qc"
              and row.get("actor_id") == "Alice" and row.get("status") == "accepted")
    assert qc.get("evidence_status") == "unknown_no_evidence"
    assert "commit_marker" not in qc and "committed_cells" not in qc
    assert validate_hold_trace(trace)["valid"] is True


def test_semantic_noop_and_unknown_reason_are_distinct_and_never_make_a_root():
    clock = FakeClock()
    noop = {
        "cell_index": 0, "coordinate": [0, 64, -2], "state": "known_non_air",
        "disposition": "semantic_noop", "block_name": "stone", "registry_id": 1,
    }
    unknown = {
        "cell_index": 1, "coordinate": [1, 64, -2], "state": "unknown",
        "disposition": "unknown", "unknown_reason": "unloaded_target",
    }
    receipt = K11CommitReceipt((), [], "Alice", 0, 17, observations=[noop, unknown])
    instance = scheduler(clock, lambda request, receive: success(receive),
                         ingest=lambda actor, *args: receipt if actor == "Alice" else None,
                         duration=CADENCE_NS)
    instance.poll()
    trace = instance.trace_artifact()
    qc = next(row for row in trace["events"] if row["event"] == "qc"
              and row.get("actor_id") == "Alice" and row.get("status") == "accepted")
    assert qc["observations"] == [noop, unknown]
    assert qc["semantic_noop_count"] == 1 and qc["transition_count"] == 0
    assert qc["unknown_count"] == 1 and qc["evidence_status"] == "semantic_noop"
    assert "commit_marker" not in qc and "committed_cells" not in qc
    assert validate_hold_trace(trace)["valid"] is True

    bad = instance.trace_artifact()
    target = next(row for row in bad["events"] if row.get("actor_id") == "Alice"
                 and row.get("event") == "qc" and row.get("status") == "accepted")
    target["observations"][1]["unknown_reason"] = "Alice-secret-identity"
    assert validate_hold_trace(bad)["valid"] is False


def test_all_unknown_receipt_is_unknown_only_with_actual_counts():
    observations = [
        {"cell_index": index, "coordinate": [index, 64, -2], "state": "unknown",
         "disposition": "unknown", "unknown_reason": "unloaded_path"}
        for index in range(75)
    ]
    receipt = K11CommitReceipt((), [], "Alice", 0, 8, observations=observations)
    clock = FakeClock()
    instance = scheduler(clock, lambda request, receive: success(receive),
                         ingest=lambda actor, *args: receipt if actor == "Alice" else None,
                         duration=CADENCE_NS)
    instance.poll()
    trace = instance.trace_artifact()
    qc = next(row for row in trace["events"] if row.get("actor_id") == "Alice"
              and row.get("event") == "qc" and row.get("status") == "accepted")
    assert qc["raw_cell_count"] == 75 and qc["unknown_count"] == 75
    assert qc["semantic_noop_count"] == qc["transition_count"] == 0
    assert qc["evidence_status"] == "unknown_only"
    assert "commit_marker" not in qc
    assert validate_hold_trace(trace)["valid"] is True


@pytest.mark.parametrize("result", [None, (), ("unverified-root",), object()])
def test_generic_successful_callback_without_receipt_never_claims_a_commit(result):
    clock = FakeClock()
    instance = scheduler(clock, lambda request, receive: success(receive),
                         ingest=lambda actor, *args: result if actor == "Alice" else None,
                         duration=CADENCE_NS)
    instance.poll()
    trace = instance.trace_artifact()
    accepted = [row for row in trace["events"] if row.get("event") == "qc"
                and row.get("actor_id") == "Alice" and row.get("status") == "accepted"]
    assert len(accepted) == 1
    assert accepted[0]["evidence_status"] == "unknown_no_evidence"
    assert "observations" not in accepted[0] and "commit_marker" not in accepted[0]
    assert "committed_cells" not in accepted[0] and "capture_seq" not in accepted[0]
    assert trace["fatal_receipt"] is None and trace["scientifically_eligible"] is True
    assert validate_hold_trace(trace)["valid"] is True


def test_typed_rejection_is_the_only_ordinary_zero_mutation_rejection():
    clock = FakeClock()

    def ingest(actor, *_args):
        if actor == "Alice":
            raise K11RejectedBeforeMutation("proven-before-mutation")

    instance = scheduler(clock, lambda request, receive: success(receive), ingest, duration=CADENCE_NS)
    instance.poll()
    trace = instance.trace_artifact()
    rejection = next(row for row in trace["events"] if row.get("event") == "qc"
                     and row.get("actor_id") == "Alice")
    assert rejection["status"] == "rejected_before_mutation"
    assert rejection["mutation_status"] == "zero"
    assert trace["fatal_receipt"] is None and trace["scientifically_eligible"] is True
    assert validate_hold_trace(trace)["valid"] is True


@dataclass
class _OpaqueExactRequestIdentity:
    marker: str


def test_sensor_progress_preserves_exact_request_identity_without_action_side_effects():
    clock = FakeClock()
    request = ExactRequest(
        "run:MineBlock:candidate", "run:MineBlock:attempt",
        ActionRef("MineBlock", 1, "a" * 64),
        (("x", 1), ("y", 64), ("z", -2)),
        target={"x": 1, "y": 64, "z": -2},
    )
    prepared = MinecraftPreparedAction(
        "MineBlock", request, object(), arguments=request.arguments,
    )
    saved = (
        prepared, prepared.request, prepared.request.identity_bytes(),
        prepared.request.candidate_id, prepared.request.attempt_id,
        prepared.request.action, prepared.request.arguments, prepared.request.target,
    )
    identity = prepared
    progressed = []

    def transport(sensor_request, receive):
        progressed.append((sensor_request["actor_id"], sensor_request["tick_index"]))
        success(receive, identity=identity)

    def ingest(_actor, _request, _payload, received_identity):
        assert received_identity is identity

    instance = scheduler(clock, transport, ingest=ingest, duration=2 * CADENCE_NS)
    instance.poll()
    clock.set(CADENCE_NS)
    instance.poll()
    assert progressed == [(actor, tick) for tick in (0, 1) for actor in ("Alice", "Bob")]
    assert prepared is saved[0] and prepared.request is saved[1]
    assert (
        prepared.request.identity_bytes(), prepared.request.candidate_id,
        prepared.request.attempt_id, prepared.request.action,
        prepared.request.arguments, prepared.request.target,
    ) == saved[2:]


class _LockProbe:
    def __init__(self):
        self._guard = threading.Lock()
        self._held = False

    @property
    def held(self):
        with self._guard:
            return self._held

    def __enter__(self):
        with self._guard:
            assert not self._held
            self._held = True
        return self

    def __exit__(self, *_exc):
        with self._guard:
            self._held = False


def test_blocked_callback_does_not_retain_admission_or_runtime_locks_or_starve_peer():
    clock = FakeClock()
    runtime_lock, authority_lock = _LockProbe(), _LockProbe()
    callbacks = {}
    request_objects = {}
    identities = {actor: _OpaqueExactRequestIdentity(actor) for actor in ("Alice", "Bob")}
    ingest_entered = threading.Event()
    release_ingest = threading.Event()
    progress = []

    def transport(request, receive):
        actor, tick = request["actor_id"], request["tick_index"]
        request_objects[(actor, tick)] = request
        if actor == "Alice" and tick == 0:
            callbacks[(actor, tick)] = receive
            return
        progress.append((actor, tick))
        success(receive, identity=identities[actor])

    def ingest(actor, request, _payload, identity):
        assert request is request_objects[(actor, request["tick_index"])]
        assert identity is identities[actor]
        if actor == "Alice" and request["tick_index"] == 0:
            assert not runtime_lock.held and not authority_lock.held
            ingest_entered.set()
            assert release_ingest.wait(timeout=2)
            with runtime_lock, authority_lock:
                pass

    instance = scheduler(clock, transport, ingest=ingest, duration=3 * CADENCE_NS)
    instance.poll()
    alice_thread = threading.Thread(
        target=lambda: success(callbacks[("Alice", 0)], identity=identities["Alice"]),
    )
    alice_thread.start()
    assert ingest_entered.wait(timeout=2)
    assert not runtime_lock.held and not authority_lock.held
    assert instance._admission_gate.lock.acquire(blocking=False)
    instance._admission_gate.lock.release()

    clock.set(CADENCE_NS)
    instance.poll()
    assert ("Bob", 1) in progress
    assert instance.admission_enabled("Bob") is True
    assert instance.pending_count == 1
    release_ingest.set()
    alice_thread.join(timeout=2)
    assert not alice_thread.is_alive()
    assert validate_hold_trace(instance.trace_artifact())["valid"] is True


def test_generic_mutate_then_raise_is_unknown_fatal_without_invented_inventory():
    clock = FakeClock()
    later_callbacks = []
    mutated = []

    def transport(request, receive):
        later_callbacks.append((request["actor_id"], request["tick_index"]))
        success(receive)

    def ingest(actor, *_args):
        if actor == "Alice":
            mutated.append("mutation-may-have-happened")
            raise RuntimeError("exception text is not retained")

    instance = scheduler(clock, transport, ingest, duration=3 * CADENCE_NS)
    instance.poll()
    trace = instance.trace_artifact()
    assert mutated == ["mutation-may-have-happened"]
    assert later_callbacks == [("Alice", 0)]  # Bob was pending but never dispatched.
    assert trace["fatal_status"] == "sensor_ingest_unverified_fatal"
    assert trace["fatal_receipt"] is None
    assert trace["fatal_reason"] == "sensor_ingest_unverified_fatal"
    assert trace["scientifically_eligible"] is False and trace["censored"] is True
    assert trace["unverified_ingest_failure"]["mutation_status"] == "unknown"
    fatal = next(row for row in trace["events"] if row.get("status") == "sensor_ingest_unverified_fatal")
    assert fatal["mutation_status"] == "unknown"
    assert not ({"capture_seq", "committed_cells", "fatal_receipt", "tick_mutations"} & set(fatal))
    assert validate_hold_trace(trace)["valid"] is True
    clock.set(3 * CADENCE_NS)
    instance.poll()
    after_close = instance.trace_artifact()
    assert after_close["window_closed"] is True
    fatal_index = fatal["sequence"]
    assert not any(row["sequence"] > fatal_index and row["event"] in {"due", "request"}
                   for row in after_close["events"])
    assert validate_hold_trace(after_close)["valid"] is True


def test_committed_receipt_marker_requires_and_carries_nonempty_inventory():
    clock = FakeClock()
    cell = commit_cell()
    observation = {
        "cell_index": 0, "coordinate": cell["coordinate"], "state": "known_non_air",
        "disposition": "committed", "block_name": "stone", "registry_id": 1,
        "root_id": cell["root_id"],
    }
    receipt = K11CommitReceipt((cell["root_id"],), [cell], "Alice", 0, 17,
                               observations=[observation])
    instance = scheduler(clock, lambda request, receive: success(receive),
                         ingest=lambda actor, *args: receipt if actor == "Alice" else None,
                         duration=CADENCE_NS)
    instance.poll()
    trace = instance.trace_artifact()
    marker = next(row for row in trace["events"] if row.get("commit_marker") is True)
    assert marker["committed_cells"] == [cell]
    assert marker["eac_ingest_sequences"] == [1]
    assert validate_hold_trace(trace)["valid"] is True


def test_partial_commit_fatal_preserves_only_real_inventory_and_stops_both_actors():
    clock = FakeClock()
    cell = commit_cell()
    failure = fatal_receipt(cells=[cell])
    sent = []

    def transport(request, receive):
        sent.append((request["actor_id"], request["tick_index"]))
        success(receive)

    def ingest(actor, *_args):
        if actor == "Alice":
            raise K11FatalSensorIngest(failure)
        return None

    instance = scheduler(clock, transport, ingest, duration=3 * CADENCE_NS)
    instance.poll()
    trace = instance.trace_artifact()
    assert sent == [("Alice", 0)]
    assert instance.admission_enabled("Alice") is False
    assert instance.admission_enabled("Bob") is False
    assert trace["fatal_status"] == "partial_commit_fatal"
    assert trace["fatal_receipt"]["committed_cells"] == [cell]
    assert trace["scientifically_eligible"] is False and trace["censored"] is True
    assert validate_hold_trace(trace)["valid"] is True


@pytest.mark.parametrize("bad_status", [[], {}])
def test_malformed_ingest_status_is_a_structured_trace_rejection(bad_status):
    instance = scheduler(FakeClock(), lambda request, receive: success(receive), duration=CADENCE_NS)
    instance.poll()
    artifact = instance.trace_artifact()
    next(event for event in artifact["events"] if event["event"] == "ingest")["status"] = bad_status
    assert validate_hold_trace(artifact)["valid"] is False


def test_secondary_completed_observation_after_unverified_fatal_preserves_inventory():
    clock = FakeClock()
    callbacks = {}
    entered = threading.Event()
    release = threading.Event()
    cell = commit_cell(actor="Bob")
    observation = {"cell_index": 0, "coordinate": cell["coordinate"],
                   "state": "known_non_air", "disposition": "committed",
                   "block_name": "stone", "registry_id": 1, "root_id": cell["root_id"]}
    receipt = K11CommitReceipt((cell["root_id"],), [cell], "Bob", 0, 17,
                               observations=[observation])

    def ingest(actor, *_args):
        if actor == "Alice":
            raise RuntimeError("synthetic callback whose mutation state is unknown")
        entered.set()
        assert release.wait(timeout=5)
        return receipt

    instance = scheduler(clock, lambda request, receive: callbacks.setdefault(request["actor_id"], receive),
                         ingest, duration=2 * CADENCE_NS)
    instance.poll()
    worker = threading.Thread(target=lambda: success(callbacks["Bob"]))
    worker.start()
    try:
        assert entered.wait(timeout=5)
        success(callbacks["Alice"])
        assert instance.trace_artifact()["fatal_status"] == "sensor_ingest_unverified_fatal"
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    artifact = instance.trace_artifact()
    secondary = next(event for event in artifact["events"]
                     if event.get("reason") == "receipt_after_unverified_fatal")
    assert secondary["secondary_committed_cells"] == [cell]
    assert secondary["observations"] == [observation]
    assert not artifact["scientifically_eligible"] and artifact["censored"]
    assert validate_hold_trace(artifact)["valid"] is True
    for field, bad in (("provenance_id", []), ("ingest_sequence", "invalid")):
        malformed = copy.deepcopy(artifact)
        row = next(event for event in malformed["events"]
                   if event.get("reason") == "receipt_after_unverified_fatal")
        row["secondary_committed_cells"][0][field] = bad
        assert validate_hold_trace(malformed)["valid"] is False
    malformed = copy.deepcopy(artifact)
    row = next(event for event in malformed["events"]
               if event.get("reason") == "receipt_after_unverified_fatal")
    row["secondary_eac_ingest_sequences"] = ["invalid"]
    assert validate_hold_trace(malformed)["valid"] is False


@pytest.mark.parametrize("kind", [[], {}])
@pytest.mark.parametrize("unverified", [False, True])
def test_malformed_event_kind_after_either_fatal_returns_structured_rejection(kind, unverified):
    clock = FakeClock()

    def ingest(actor, *_args):
        if actor == "Alice":
            if unverified:
                raise RuntimeError("synthetic unknown-mutation failure")
            raise K11FatalSensorIngest(fatal_receipt())

    instance = scheduler(clock, lambda request, receive: success(receive), ingest,
                         duration=CADENCE_NS)
    instance.poll()
    clock.set(CADENCE_NS)
    instance.poll()
    artifact = instance.trace_artifact()
    assert validate_hold_trace(artifact)["valid"] is True
    assert artifact["events"][-1]["event"] == "window_closed"
    artifact["events"][-1]["event"] = kind
    assert validate_hold_trace(artifact)["valid"] is False


def test_fatal_raw_account_is_exactly_75_and_gate_latches_a_detached_first_receipt():
    clock = FakeClock()
    cell = commit_cell()
    receipt = fatal_receipt(cells=[cell])
    receipt.update({"raw_observations": raw_observations(), "raw_cell_count": 75})
    gate = K11SensorAdmissionGate()
    assert gate.publish_fatal(receipt) is True
    detached = gate.fatal_receipt
    detached["raw_observations"][0]["block_name"] = "tampered"
    other = fatal_receipt(cells=[])
    assert gate.publish_fatal(other) is False
    assert gate.fatal_receipt["raw_observations"][0]["block_name"] == "stone"

    instance = scheduler(
        clock, lambda request, receive: success(receive),
        lambda actor, *args: (_ for _ in ()).throw(K11FatalSensorIngest(receipt))
        if actor == "Alice" else None,
        duration=CADENCE_NS,
    )
    instance.poll()
    trace = instance.trace_artifact()
    fatal = next(row for row in trace["events"] if row.get("status") == "partial_commit_fatal")
    assert fatal["raw_cell_count"] == 75 and len(fatal["raw_observations"]) == 75
    assert "disposition" not in fatal["raw_observations"][0]
    assert "root_id" not in fatal["raw_observations"][0]
    assert validate_hold_trace(trace)["valid"] is True

    tampered = instance.trace_artifact()
    fatal = next(row for row in tampered["events"] if row.get("status") == "partial_commit_fatal")
    fatal["raw_observations"][4]["disposition"] = "committed"
    assert validate_hold_trace(tampered)["valid"] is False
    tampered_count = instance.trace_artifact()
    fatal = next(row for row in tampered_count["events"] if row.get("status") == "partial_commit_fatal")
    fatal["raw_cell_count"] = 74
    assert validate_hold_trace(tampered_count)["valid"] is False

    capacity = {
        **fatal_receipt(phase="capacity_exhausted"),
        "failing_cell_index": None, "failing_coordinate": None,
        "raw_observations": raw_observations(), "raw_cell_count": 75,
    }
    capacity_fatal = K11FatalSensorIngest(capacity).fatal_receipt
    assert capacity_fatal["committed_cells"] == []
    assert capacity_fatal["raw_cell_count"] == len(capacity_fatal["raw_observations"]) == 75


def test_fatal_capacity_root_inventory_and_malformed_nested_trace_fail_closed():
    clock = FakeClock()
    capacity = {
        "status": "sensor_ingest_fatal", "actor_id": "Alice", "tick_index": 0,
        "capture_seq": 17, "committed_cells": [], "failing_cell_index": None,
        "failing_coordinate": None, "phase": "capacity_exhausted",
        "orphan_provenance_id": None, "orphan_provenance": False,
    }
    instance = scheduler(
        clock, lambda request, receive: success(receive),
        lambda actor, *args: (_ for _ in ()).throw(K11FatalSensorIngest(capacity))
        if actor == "Alice" else None,
        duration=3 * CADENCE_NS,
    )
    instance.poll()
    trace = instance.trace_artifact()
    assert trace["fatal_reason"] == "capacity_exhausted"
    fatal = next(row for row in trace["events"] if row.get("status") == "sensor_ingest_fatal")
    assert fatal["committed_cells"] == [] and fatal["tick_mutations"] == 0
    assert validate_hold_trace(trace)["valid"] is True

    malformed = instance.trace_artifact()
    malformed["fatal_receipt"]["phase"] = []  # membership must not raise TypeError
    fatal_event = next(row for row in malformed["events"] if row.get("fatal_status") == "sensor_ingest_fatal")
    fatal_event["fatal_receipt"] = malformed["fatal_receipt"]
    assert validate_hold_trace(malformed)["valid"] is False

    nested = instance.trace_artifact()
    nested["events"][0]["details"] = {"nested": [{"request_hmac": "do-not-retain"}]}
    assert validate_hold_trace(nested)["valid"] is False


def test_malformed_inventory_index_does_not_raise_and_trace_counts_are_exact():
    clock = FakeClock()
    cell = commit_cell()
    observation = {
        "cell_index": 0, "coordinate": cell["coordinate"], "state": "known_non_air",
        "disposition": "committed", "block_name": "stone", "registry_id": 1,
    }
    receipt = K11CommitReceipt((cell["root_id"],), [cell], "Alice", 0, 17,
                               observations=[observation])
    instance = scheduler(clock, lambda request, receive: success(receive),
                         lambda actor, *args: receipt if actor == "Alice" else None,
                         duration=CADENCE_NS)
    instance.poll()
    malformed = instance.trace_artifact()
    marker = next(row for row in malformed["events"] if row.get("commit_marker") is True)
    marker["committed_cells"][0]["cell_index"] = []
    assert validate_hold_trace(malformed)["valid"] is False
    clean = instance.trace_artifact()
    qc = next(row for row in clean["events"] if row.get("observations"))
    qc["transition_count"] += 1
    assert validate_hold_trace(clean)["valid"] is False


def test_gate_published_fatal_stops_next_admission_before_delayed_notifier():
    clock = FakeClock()
    callbacks = {}
    sent = []
    published = threading.Event()
    release_notifier = threading.Event()
    receipt = fatal_receipt()

    class DelayedNotifierAdapter:
        def bind_admission_gate(self, gate):
            self.gate = gate

        def bind_fatal_notifier(self, notifier):
            self.notifier = notifier

        def ingest(self, actor, *_args):
            if actor == "Alice":
                with self.gate.lock:
                    assert self.gate.publish_fatal(receipt) is True
                published.set()
                assert release_notifier.wait(timeout=2)
                # Notification runs only after the gate critical section.
                self.notifier(receipt)
                raise K11FatalSensorIngest(receipt)

    adapter = DelayedNotifierAdapter()

    def transport(request, receive):
        sent.append((request["actor_id"], request["tick_index"]))
        callbacks[request["actor_id"]] = receive

    instance = scheduler(clock, transport, adapter.ingest, duration=3 * CADENCE_NS)
    instance.poll()
    assert sent == [("Alice", 0), ("Bob", 0)]
    callback_thread = threading.Thread(
        target=lambda: success(callbacks["Alice"], payload=b"alice"),
    )
    callback_thread.start()
    assert published.wait(timeout=2)

    # The notifier is deliberately stalled after gate publication. Poll and
    # trace synchronize gate -> scheduler and must already fail closed.
    clock.set(CADENCE_NS)
    instance.poll()
    artifact = instance.trace_artifact()
    assert artifact["fatal_status"] == "sensor_ingest_fatal"
    assert instance.admission_enabled("Alice") is False
    assert instance.admission_enabled("Bob") is False
    assert sent == [("Alice", 0), ("Bob", 0)]
    fatal_sequence = next(row["sequence"] for row in artifact["events"]
                          if row.get("status") == "sensor_ingest_fatal")
    assert not any(row["sequence"] > fatal_sequence
                   and (row["event"] in {"due", "request"}
                        or (row["event"] == "ingest" and row.get("status") == "admitted"))
                   for row in artifact["events"])

    release_notifier.set()
    callback_thread.join(timeout=2)
    assert not callback_thread.is_alive()
    final_trace = instance.trace_artifact()
    assert sum(row.get("status") == "sensor_ingest_fatal" for row in final_trace["events"]) == 1
    assert validate_hold_trace(final_trace)["valid"] is True


def test_concurrent_duplicate_and_secondary_fatal_receipts_keep_first_inventory():
    clock = FakeClock()
    callbacks = {}
    sent = []
    alice_published = threading.Event()
    bob_entered = threading.Event()
    release_alice = threading.Event()
    release_bob = threading.Event()
    primary = fatal_receipt(actor="Alice")
    secondary = fatal_receipt(cells=[commit_cell(actor="Bob")], actor="Bob")

    class RacingAdapter:
        def bind_admission_gate(self, gate):
            self.gate = gate

        def bind_fatal_notifier(self, notifier):
            self.notifier = notifier

        def ingest(self, actor, *_args):
            if actor == "Alice":
                with self.gate.lock:
                    self.gate.publish_fatal(primary)
                alice_published.set()
                assert release_alice.wait(timeout=2)
                self.alice_notification = self.notifier(primary)
                raise K11FatalSensorIngest(primary)
            bob_entered.set()
            assert release_bob.wait(timeout=2)
            with self.gate.lock:
                self.bob_publish_result = self.gate.publish_fatal(secondary)
            self.bob_notification = self.notifier(secondary)
            raise K11FatalSensorIngest(secondary)

    adapter = RacingAdapter()

    def transport(request, receive):
        sent.append((request["actor_id"], request["tick_index"]))
        callbacks[request["actor_id"]] = receive

    instance = scheduler(clock, transport, adapter.ingest, duration=3 * CADENCE_NS)
    instance.poll()
    bob_thread = threading.Thread(target=lambda: success(callbacks["Bob"], payload=b"bob"))
    alice_thread = threading.Thread(target=lambda: success(callbacks["Alice"], payload=b"alice"))
    bob_thread.start()
    assert bob_entered.wait(timeout=2)
    alice_thread.start()
    try:
        assert alice_published.wait(timeout=2)
        first_trace = instance.trace_artifact()
        assert first_trace["fatal_receipt"]["actor_id"] == "Alice"
        assert instance.admission_enabled("Alice") is False
        assert instance.admission_enabled("Bob") is False
        clock.set(CADENCE_NS)
        instance.poll()
        assert sent == [("Alice", 0), ("Bob", 0)]

        release_bob.set()
        bob_thread.join(timeout=2)
        assert not bob_thread.is_alive()
        assert adapter.bob_publish_result is False
        assert adapter.bob_notification is False
    finally:
        release_bob.set()
        release_alice.set()
        bob_thread.join(timeout=2)
        alice_thread.join(timeout=2)

    assert not alice_thread.is_alive()
    assert adapter.alice_notification is False
    clock.set(3 * CADENCE_NS)
    instance.poll()
    trace = instance.trace_artifact()
    fatal_events = [row for row in trace["events"] if row.get("status") in {
        "partial_commit_fatal", "sensor_ingest_fatal",
    }]
    assert len(fatal_events) == 1
    assert trace["fatal_receipt"]["actor_id"] == "Alice"
    secondary_event = next(row for row in trace["events"] if "secondary_fatal_receipt" in row)
    assert secondary_event["status"] == "diagnostic_only"
    assert secondary_event["secondary_fatal_receipt"]["actor_id"] == "Bob"
    assert secondary_event["committed_cells"] == secondary["committed_cells"]
    assert not any(row.get("actor_id") == "Bob" and row.get("status") == "accepted"
                   for row in trace["events"])
    assert validate_hold_trace(trace)["valid"] is True


@pytest.mark.parametrize("cutoff_ns", [DEADLINE_NS, CADENCE_NS])
def test_trusted_fatal_receipt_takes_precedence_at_deadline_and_window_close(cutoff_ns):
    clock = FakeClock()
    scheduler_holder = {}
    sent = []
    cell = commit_cell()
    receipt = fatal_receipt(cells=[cell])

    def transport(request, receive):
        sent.append((request["actor_id"], request["tick_index"]))
        success(receive, bridge_ns=10**18)

    def ingest(actor, *_args):
        if actor == "Alice":
            clock.set(cutoff_ns)
            scheduler_holder["scheduler"].poll()
            raise K11FatalSensorIngest(receipt)

    instance = scheduler(clock, transport, ingest, duration=CADENCE_NS)
    scheduler_holder["scheduler"] = instance
    instance.poll()
    assert sent == [("Alice", 0)]
    assert instance.admission_enabled("Alice") is False
    assert instance.admission_enabled("Bob") is False
    trace = instance.trace_artifact()
    fatal = next(row for row in trace["events"] if row.get("status") == "partial_commit_fatal")
    assert fatal["committed_cells"] == [cell]
    assert fatal["controller_monotonic_ns"] == cutoff_ns
    assert trace["scientifically_eligible"] is False and trace["censored"] is True
    if cutoff_ns == CADENCE_NS:
        assert trace["window_closed"] is True
        assert fatal["sequence"] > next(row["sequence"] for row in trace["events"]
                                         if row.get("event") == "window_closed")
    assert validate_hold_trace(trace)["valid"] is True


def test_fatal_window_close_does_not_add_post_fatal_due_ticks():
    clock = FakeClock()
    receipt = fatal_receipt(cells=[commit_cell()])
    instance = scheduler(
        clock, lambda request, receive: success(receive),
        lambda actor, *_args: (_ for _ in ()).throw(K11FatalSensorIngest(receipt))
        if actor == "Alice" else None,
        duration=2 * CADENCE_NS,
    )
    instance.poll()
    before_close = instance.trace_artifact()
    fatal_seq = next(row["sequence"] for row in before_close["events"]
                     if row.get("status") == "partial_commit_fatal")
    clock.set(2 * CADENCE_NS)
    instance.poll()
    final = instance.trace_artifact()
    assert final["window_closed"] is True
    assert final["fatal_receipt"] == before_close["fatal_receipt"]
    assert not any(row["event"] == "due" and row["sequence"] > fatal_seq
                   for row in final["events"])
    assert validate_hold_trace(final)["valid"] is True
