import json
import pytest
from benchmarks.minecraft.k12_worker_protocol import WorkerProtocolError, WorkerStream, WORKER_SCHEMA

def line(event="cell_started", payload=None, arm="A"):
    return json.dumps({"schema": WORKER_SCHEMA, "worker_id": "w", "cell_id": "c",
                       "triplet_id": "t", "arm": arm, "event": event, "payload": payload or {}})

def test_strict_order_and_terminal():
    stream = WorkerStream("w")
    assert stream.accept(line()).event == "cell_started"
    assert stream.accept(line("prepared_request_frozen")).event == "prepared_request_frozen"
    assert stream.accept(line("invalidation_ingested")).event == "invalidation_ingested"
    assert stream.accept(line("worker_terminal_candidate")).terminal
    with pytest.raises(WorkerProtocolError): stream.accept(line("effect_entered"))

@pytest.mark.parametrize("value", [{"schema": WORKER_SCHEMA, "worker_id": "w", "event": "started", "payload": {}, "source": "x"},
                                    {"schema": WORKER_SCHEMA, "worker_id": "other", "event": "started", "payload": {}}])
def test_untrusted_fields_and_identity_rejected(value):
    value = {**value, "cell_id": "c", "triplet_id": "t", "arm": "A"}
    with pytest.raises(WorkerProtocolError): WorkerStream("w").accept(json.dumps(value))


def test_duplicate_keys_and_recursive_authority_rejected():
    raw = '{"schema":"%s","schema":"%s","worker_id":"w","cell_id":"c","triplet_id":"t","arm":"A","event":"cell_started","payload":{}}' % (WORKER_SCHEMA, WORKER_SCHEMA)
    with pytest.raises(WorkerProtocolError): WorkerStream("w").accept(raw)
    with pytest.raises(WorkerProtocolError): WorkerStream("w").accept(line(payload={"nested": [{"oracle": "true"}]}))


@pytest.mark.parametrize("key", ["reset_invalid", "runtime_failure", "containment_failure",
                                  "budget_exhausted", "reset_generation", "generation",
                                  "budget_model_calls", "model_call_count", "effect_totals"])
def test_worker_cannot_claim_failure_generation_or_budget_authority(key):
    with pytest.raises(WorkerProtocolError):
        WorkerStream("w").accept(line(payload={"nested": [{key: True}]}))


def test_recovery_protocol_represents_bounded_repeated_operation_cycles():
    stream = WorkerStream("w")
    for event in ("cell_started", "prepared_request_frozen", "invalidation_ingested",
                  "rejection_observation_emitted", "recovery_started"):
        stream.accept(line(event, arm="R"))
    for index in range(2):
        stream.accept(line("model_call_admitted", {"operation_id": f"m{index}"}, "R"))
        stream.accept(line("model_call_terminal", {"operation_id": f"m{index}"}, "R"))
    for index in range(2):
        stream.accept(line("observation_started", {"operation_id": f"o{index}"}, "R"))
        stream.accept(line("observation_terminal", {"operation_id": f"o{index}"}, "R"))
        stream.accept(line("evidence_ingested", {"operation_id": f"e{index}"}, "R"))
    assert stream.accept(line("recovery_proposed", {"action": "MineBlock",
                                                      "arguments": {"x": 1, "y": 2, "z": 3}}, "R")).event == "recovery_proposed"


def test_operation_terminals_match_their_active_operation():
    stream = WorkerStream("w")
    stream.accept(line())
    with pytest.raises(WorkerProtocolError):
        stream.accept(line("model_call_terminal", {"operation_id": "m1"}))
    stream.accept(line("model_call_admitted", {"operation_id": "m1"}))
    with pytest.raises(WorkerProtocolError):
        stream.accept(line("observation_started", {"operation_id": "o1"}))
    with pytest.raises(WorkerProtocolError):
        stream.accept(line("model_call_terminal", {"operation_id": "other"}))
    stream.accept(line("model_call_terminal", {"operation_id": "m1"}))


def test_operation_ids_cannot_be_reused_or_interleaved():
    stream = WorkerStream("w")
    stream.accept(line())
    stream.accept(line("effect_entered", {"effect_id": "e1", "operation_id": "e1"}))
    with pytest.raises(WorkerProtocolError):
        stream.accept(line("effect_entered", {"effect_id": "e2", "operation_id": "e2"}))
    stream.accept(line("effect_terminal", {"effect_id": "e1", "operation_id": "e1"}))
    with pytest.raises(WorkerProtocolError):
        stream.accept(line("effect_entered", {"effect_id": "e1", "operation_id": "e1"}))


def test_operation_events_require_ids_and_terminal_cannot_follow_worker_terminal():
    stream = WorkerStream("w")
    stream.accept(line())
    with pytest.raises(WorkerProtocolError):
        stream.accept(line("observation_started"))
    stream.accept(line("worker_terminal_candidate"))
    with pytest.raises(WorkerProtocolError):
        stream.accept(line("observation_terminal", {"operation_id": "o1"}))
