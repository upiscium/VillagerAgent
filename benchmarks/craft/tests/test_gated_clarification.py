from benchmarks.craft.craft_env_adapter import _apply_clarification_gate
from benchmarks.craft.dual_dag.gating import should_clarify


def test_gate_does_not_fire_when_dual_dag_disabled():
    should_gate, metadata = should_clarify(
        candidate_metadata={"chosen_confidence": 0.1, "claim_conflict_count": 3},
        config={"dual_dag": {"enabled": False, "gated_clarification": {"enabled": True}}},
    )

    assert should_gate is False
    assert metadata["enabled"] is False
    assert metadata["reason"] == "none"


def test_gate_fires_on_low_confidence():
    should_gate, metadata = should_clarify(
        candidate_metadata={"chosen_confidence": 0.4, "claim_conflict_count": 0},
        config={"dual_dag": {"enabled": True, "gated_clarification": {"enabled": True}}},
    )

    assert should_gate is True
    assert metadata["reason"] == "low_action_confidence"
    assert metadata["risk_score"] == 0.6


def test_gate_fires_on_conflict_count():
    should_gate, metadata = should_clarify(
        candidate_metadata={"chosen_confidence": 0.9, "claim_conflict_count": 1},
        config={"dual_dag": {"enabled": True, "gated_clarification": {"enabled": True}}},
    )

    assert should_gate is True
    assert metadata["reason"] == "claim_conflict"


def test_gate_fires_on_large_block_without_span():
    should_gate, metadata = should_clarify(
        candidate_metadata={
            "chosen_candidate_id": "action:1:0",
            "chosen_confidence": 0.9,
            "claim_conflict_count": 0,
            "candidates": [{
                "node_id": "action:1:0",
                "action": {"action": "place", "block": "bl", "position": "(0,0)", "layer": 0},
            }],
        },
        config={"dual_dag": {"enabled": True, "gated_clarification": {"enabled": True}}},
    )

    assert should_gate is True
    assert metadata["reason"] == "large_block_span_uncertainty"


def test_clarify_action_preserves_candidate_metadata_without_hidden_state():
    candidate_metadata = {"chosen_confidence": 0.4, "claim_conflict_count": 0}
    action = _apply_clarification_gate(
        {
            "action": "place",
            "block": "ys",
            "position": "(0,0)",
            "layer": 0,
            "_action_candidate_metadata": candidate_metadata,
        },
        {"dual_dag": {"enabled": True, "gated_clarification": {"enabled": True}}},
    )

    assert action["action"] == "clarify"
    assert action["_action_candidate_metadata"] is candidate_metadata
    assert action["_gated_clarification"]["reason"] == "low_action_confidence"
    serialized = str(action).lower()
    assert "target_structure" not in serialized
    assert "oracle" not in serialized


def test_clarify_action_not_applied_when_gate_disabled():
    action = {"action": "place", "_action_candidate_metadata": {"chosen_confidence": 0.1}}

    assert _apply_clarification_gate(action, {}) is action
