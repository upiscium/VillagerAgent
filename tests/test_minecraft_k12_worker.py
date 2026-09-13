import json

import pytest

from benchmarks.minecraft.k12_protocol import build_k12_cells
from benchmarks.minecraft.k12_worker import K12Worker, K12WorkerError
from benchmarks.minecraft.k12_worker_protocol import WORKER_SCHEMA


def test_worker_receives_one_immutable_cell_and_preserves_arm_sequence():
    worker = K12Worker(build_k12_cells()[0], worker_id="worker-1")
    assert worker.manifest.arm_permutation == ("A", "R", "S")
    with pytest.raises((AttributeError, TypeError)):
        worker.manifest.arm = "R"
    messages = [json.loads(line) for line in worker.run()]
    assert [item["event"] for item in messages] == [
        "cell_started", "prepared_request_frozen", "invalidation_ingested",
        "advisory_would_block", "effect_decision", "effect_entered",
        "effect_terminal", "worker_terminal_candidate",
    ]
    assert messages[0]["payload"]["arm_sequence"] == ["A", "R", "S"]


@pytest.mark.parametrize("key", ["source", "sequence", "message_digest", "oracle", "recovered", "containment",
                                  "reset_invalid", "runtime_failure", "containment_failure",
                                  "budget_exhausted", "reset_generation", "generation",
                                  "budget_model_calls", "model_call_count", "effect_totals"])
def test_worker_cannot_author_parent_fields(key):
    worker = K12Worker(build_k12_cells()[0], worker_id="worker-1")
    with pytest.raises(K12WorkerError):
        worker.message("progress", {key: "forbidden"})


def test_worker_authority_keys_are_rejected_recursively():
    worker = K12Worker(build_k12_cells()[0], worker_id="worker-1")
    with pytest.raises(K12WorkerError):
        worker.message("cell_started", {"nested": [{"budget_exhausted": True}]})


def test_worker_messages_have_only_untrusted_protocol_fields():
    line = json.loads(K12Worker(build_k12_cells()[0], worker_id="w").message("cell_started"))
    assert set(line) == {"schema", "worker_id", "cell_id", "triplet_id", "arm", "event", "payload"}
    assert line["schema"] == WORKER_SCHEMA


def test_default_worker_operations_have_stable_start_terminal_ids():
    messages = [json.loads(line) for line in K12Worker(build_k12_cells()[1], worker_id="w").run()]
    entered = next(item for item in messages if item["event"] == "observation_started")
    terminal = next(item for item in messages if item["event"] == "observation_terminal")
    assert entered["payload"]["operation_id"] == terminal["payload"]["operation_id"]
    assert "recovery_proposed" in {item["event"] for item in messages}
    assert not {"new_request_prepared", "permit_issued", "effect_decision",
                "effect_entered", "effect_terminal"}.intersection(
                    item["event"] for item in messages)

    recovery = [item for item in messages if item["event"] == "recovery_step"]
    assert recovery and all(item["payload"].get("operation_id") for item in recovery)
