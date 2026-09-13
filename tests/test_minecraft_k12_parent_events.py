import json
from benchmarks.minecraft.k12_parent_events import ParentEventLog
from benchmarks.minecraft.k12_worker_protocol import WORKER_SCHEMA

def msg(event): return json.dumps({"schema": WORKER_SCHEMA, "worker_id": "w", "cell_id": "c",
                                   "triplet_id": "t", "arm": "A", "event": event, "payload": {}})
def test_parent_assigns_chain_and_joins_by_digest():
    log = ParentEventLog("w", clock_ns=lambda: 7)
    event = log.receive(msg("cell_started"))
    assert event.sequence == 0 and event.received_monotonic_ns == 7
    assert log.by_digest(event.message_digest) == event
    assert log.verify()
