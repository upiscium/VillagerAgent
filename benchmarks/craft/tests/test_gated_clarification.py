import pytest

from benchmarks.craft.craft_env_adapter import (
    _apply_clarification_gate,
    _prepare_runtime_action_candidates,
    _prioritize_supported_candidates,
    _suppress_repeated_zero_progress_candidates,
    _runtime_decision_support,
)
from benchmarks.craft.dual_dag.action_candidates import action_candidates_from_moves
from benchmarks.craft.dual_dag.gating import should_clarify
from benchmarks.craft.dual_dag.runtime import DualDAGRuntime


def test_gate_does_not_fire_when_dual_dag_disabled():
    should_gate, metadata = should_clarify(
        candidate_metadata={"chosen_confidence": 0.1, "claim_conflict_count": 3},
        config={"dual_dag": {"enabled": False, "gated_clarification": {"enabled": True}}},
    )

    assert should_gate is False
    assert metadata["enabled"] is False
    assert metadata["reason"] == "none"


def test_repeated_zero_progress_suppression_reorders_candidates_when_enabled():
    moves = [
        {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0},
        {"action": "place", "block": "bs", "position": "(0,1)", "layer": 0},
    ]
    candidates = action_candidates_from_moves(moves=moves, reported_claims={}, turn_index=3)
    result = _suppress_repeated_zero_progress_candidates(
        oracle_moves=moves,
        action_candidates=candidates,
        previous_actions=[
            {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0, "_progress_delta": 0.0},
            {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0, "_progress_delta": None},
        ],
        config={
            "dual_dag": {
                "enabled": True,
                "action_selection": {
                    "suppress_repeated_zero_progress": {
                        "enabled": True,
                        "window_turns": 4,
                        "max_repeats": 2,
                        "treat_missing_progress_as_zero": True,
                    }
                },
            }
        },
    )

    assert result["applied"] is True
    assert result["oracle_moves"][0]["block"] == "bs"
    assert result["action_candidates"][0].action["block"] == "bs"
    assert result["metadata"]["enabled"] is True
    assert result["metadata"]["attempted"] is True
    assert result["metadata"]["applied"] is True
    assert result["metadata"]["detected_signature_count"] == 1
    assert result["metadata"]["suppressed_candidate_ids"] == ["action:3:0"]


def test_repeated_zero_progress_suppression_is_disabled_by_default():
    moves = [
        {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0},
        {"action": "place", "block": "bs", "position": "(0,1)", "layer": 0},
    ]
    candidates = action_candidates_from_moves(moves=moves, reported_claims={}, turn_index=3)

    result = _suppress_repeated_zero_progress_candidates(
        oracle_moves=moves,
        action_candidates=candidates,
        previous_actions=[
            {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0, "_progress_delta": 0.0},
            {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0, "_progress_delta": 0.0},
        ],
        config={"dual_dag": {"enabled": True}},
    )

    assert result["applied"] is False
    assert result["oracle_moves"] == moves
    assert result["action_candidates"] == candidates
    assert result["metadata"]["enabled"] is False
    assert result["metadata"]["attempted"] is False


def test_repeated_zero_progress_suppression_preserves_order_when_all_candidates_suppressed():
    moves = [{"action": "place", "block": "ys", "position": "(0,0)", "layer": 0}]
    candidates = action_candidates_from_moves(moves=moves, reported_claims={}, turn_index=3)

    result = _suppress_repeated_zero_progress_candidates(
        oracle_moves=moves,
        action_candidates=candidates,
        previous_actions=[
            {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0, "_progress_delta": 0.0},
            {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0, "_progress_delta": 0.0},
        ],
        config={
            "dual_dag": {
                "enabled": True,
                "action_selection": {"suppress_repeated_zero_progress": {"enabled": True}},
            }
        },
    )

    assert result["applied"] is False
    assert result["oracle_moves"] == moves
    assert result["metadata"]["attempted"] is True
    assert result["metadata"]["all_candidates_suppressed"] is True
    assert result["metadata"]["suppressed_candidate_ids"] == ["action:3:0"]


def test_repeated_zero_progress_suppression_reports_no_match():
    moves = [{"action": "place", "block": "bs", "position": "(0,1)", "layer": 0}]
    candidates = action_candidates_from_moves(moves=moves, reported_claims={}, turn_index=3)

    result = _suppress_repeated_zero_progress_candidates(
        oracle_moves=moves,
        action_candidates=candidates,
        previous_actions=[
            {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0, "_progress_delta": 0.0},
            {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0, "_progress_delta": 0.0},
        ],
        config={
            "dual_dag": {
                "enabled": True,
                "action_selection": {"suppress_repeated_zero_progress": {"enabled": True}},
            }
        },
    )

    assert result["applied"] is False
    assert result["metadata"]["attempted"] is True
    assert result["metadata"]["detected_signature_count"] == 1
    assert result["metadata"]["no_match"] is True
    assert result["metadata"]["suppressed_candidate_ids"] == []


def test_repeated_zero_progress_relaxed_diagnostics_do_not_reorder_candidates():
    moves = [
        {"action": "place", "block": "gs", "position": "(0,0)", "layer": 0},
        {"action": "place", "block": "bs", "position": "(1,0)", "layer": 0},
    ]
    candidates = action_candidates_from_moves(moves=moves, reported_claims={}, turn_index=4)

    result = _suppress_repeated_zero_progress_candidates(
        oracle_moves=moves,
        action_candidates=candidates,
        previous_actions=[
            {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0, "_progress_delta": 0.0},
            {"action": "remove", "block": "ys", "position": "(0,0)", "layer": 0, "_progress_delta": 0.0},
        ],
        config={
            "dual_dag": {
                "enabled": True,
                "action_selection": {
                    "suppress_repeated_zero_progress": {
                        "enabled": True,
                        "relaxed_diagnostics": {"enabled": True},
                    }
                },
            }
        },
    )

    assert result["applied"] is False
    assert result["oracle_moves"] == moves
    assert result["action_candidates"] == candidates
    assert result["metadata"]["detected_signature_count"] == 0
    assert result["metadata"]["relaxed_diagnostics_enabled"] is True
    assert result["metadata"]["relaxed_region_signature_count"] == 1
    assert result["metadata"]["relaxed_inverse_loop_signature_count"] == 1
    assert result["metadata"]["relaxed_current_candidate_match_count"] == 1
    assert result["metadata"]["relaxed_would_suppress_candidate_ids"] == ["action:4:0"]


def test_repeated_zero_progress_relaxed_diagnostics_counts_no_candidate_signatures():
    result = _suppress_repeated_zero_progress_candidates(
        oracle_moves=[],
        action_candidates=[],
        previous_actions=[
            {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0, "_progress_delta": 0.0},
            {"action": "place", "block": "bs", "position": "(0,0)", "layer": 0, "_progress_delta": 0.0},
        ],
        config={
            "dual_dag": {
                "enabled": True,
                "action_selection": {
                    "suppress_repeated_zero_progress": {
                        "enabled": True,
                        "relaxed_diagnostics": {"enabled": True},
                    }
                },
            }
        },
    )

    assert result["applied"] is False
    assert result["metadata"]["no_candidates"] is True
    assert result["metadata"]["relaxed_region_signature_count"] == 1
    assert result["metadata"]["relaxed_no_candidate_signature_count"] == 1


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
        candidate_metadata={"chosen_confidence": 0.35, "claim_conflict_count": 1},
        config={"dual_dag": {"enabled": True, "gated_clarification": {"enabled": True}}},
    )

    assert should_gate is True
    assert "claim_conflict" in metadata["reasons"]


def test_gate_allows_weak_conflict_when_risk_is_below_cost():
    should_gate, metadata = should_clarify(
        candidate_metadata={"chosen_confidence": 0.7, "claim_conflict_count": 1},
        config={"dual_dag": {"enabled": True, "gated_clarification": {"enabled": True}}},
    )

    assert should_gate is False
    assert metadata["risk_exceeds_clarification_cost"] is False


def test_gate_allows_medium_confidence_after_tuning():
    should_gate, metadata = should_clarify(
        candidate_metadata={"chosen_confidence": 0.6, "claim_conflict_count": 0},
        config={"dual_dag": {"enabled": True, "gated_clarification": {"enabled": True}}},
    )

    assert should_gate is False
    assert metadata["thresholds"]["min_action_confidence"] == 0.55
    assert metadata["thresholds"]["adaptive_thresholds"]["enabled"] is False


def test_disabled_policy_suppresses_clarification():
    should_gate, metadata = should_clarify(
        candidate_metadata={"chosen_confidence": 0.1, "claim_conflict_count": 2},
        config={
            "dual_dag": {
                "enabled": True,
                "gated_clarification": {"enabled": True, "policy": "disabled"},
            }
        },
    )

    assert should_gate is False
    assert metadata["policy"] == "disabled"
    assert metadata["reason"] == "none"


def test_value_of_information_policy_suppresses_low_value_clarification():
    should_gate, metadata = should_clarify(
        candidate_metadata={
            "chosen_candidate_id": "action:1:0",
            "chosen_confidence": 0.52,
            "claim_conflict_count": 0,
            "claim_required_evidence_count": 0,
            "candidates": [{"node_id": "action:1:0", "state": "executable", "confidence": 0.52}],
        },
        config={
            "dual_dag": {
                "enabled": True,
                "gated_clarification": {
                    "enabled": True,
                    "policy": "value_of_information",
                    "min_action_confidence": 0.55,
                    "clarification_cost": 0.4,
                },
            }
        },
    )

    assert should_gate is False
    assert metadata["policy"] == "value_of_information"
    assert metadata["value_of_clarification"] < 0
    assert metadata["reason"] == "none"


def test_value_of_information_policy_allows_high_value_clarification():
    should_gate, metadata = should_clarify(
        candidate_metadata={
            "chosen_confidence": 0.1,
            "claim_conflict_count": 1,
            "claim_required_evidence_count": 2,
        },
        config={
            "dual_dag": {
                "enabled": True,
                "gated_clarification": {
                    "enabled": True,
                    "policy": "value_of_information",
                    "min_action_confidence": 0.55,
                    "clarify_on_required_evidence": True,
                    "clarification_cost": 0.1,
                    "value_of_information": {"mean_candidate_value": 0.1, "clarification_failure_risk": 0.0},
                },
            }
        },
    )

    assert should_gate is True
    assert metadata["policy"] == "value_of_information"
    assert metadata["value_of_clarification"] > 0
    assert metadata["voi_components"]["expected_avoided_mistake_cost"] > 0
    assert "low_action_confidence" in metadata["reasons"]


def test_gate_can_suppress_low_confidence_for_executable_candidate():
    should_gate, metadata = should_clarify(
        candidate_metadata={
            "chosen_candidate_id": "action:1:0",
            "chosen_confidence": 0.4,
            "claim_conflict_count": 0,
            "claim_required_evidence_count": 0,
            "candidates": [{"node_id": "action:1:0", "state": "executable", "action": {"action": "place"}}],
        },
        config={
            "dual_dag": {
                "enabled": True,
                "gated_clarification": {
                    "enabled": True,
                    "suppress_executable_low_confidence": True,
                },
            }
        },
    )

    assert should_gate is False
    assert metadata["reason"] == "none"
    assert metadata["thresholds"]["suppress_executable_low_confidence"] is True


def test_gate_keeps_conflict_even_when_executable_low_confidence_is_suppressed():
    should_gate, metadata = should_clarify(
        candidate_metadata={
            "chosen_candidate_id": "action:1:0",
            "chosen_confidence": 0.4,
            "claim_conflict_count": 1,
            "candidates": [{"node_id": "action:1:0", "state": "executable", "action": {"action": "place"}}],
        },
        config={
            "dual_dag": {
                "enabled": True,
                "gated_clarification": {
                    "enabled": True,
                    "suppress_executable_low_confidence": True,
                },
            }
        },
    )

    assert should_gate is True
    assert "claim_conflict" in metadata["reasons"]


def test_adaptive_gate_lowers_confidence_threshold_with_support():
    should_gate, metadata = should_clarify(
        candidate_metadata={
            "chosen_confidence": 0.5,
            "claim_support_count": 2,
            "claim_conflict_count": 0,
        },
        config={
            "dual_dag": {
                "enabled": True,
                "gated_clarification": {
                    "enabled": True,
                    "adaptive_thresholds": {"enabled": True},
                },
            }
        },
    )

    assert should_gate is False
    assert metadata["thresholds"]["min_action_confidence"] == pytest.approx(0.45)
    assert metadata["thresholds"]["adaptive_thresholds"]["applied"] is True


def test_adaptive_gate_raises_threshold_and_lowers_cost_for_uncertainty():
    should_gate, metadata = should_clarify(
        candidate_metadata={
            "chosen_confidence": 0.6,
            "claim_support_count": 0,
            "claim_conflict_count": 1,
            "claim_required_evidence_count": 1,
        },
        config={
            "dual_dag": {
                "enabled": True,
                "gated_clarification": {
                    "enabled": True,
                    "min_action_confidence": 0.55,
                    "clarify_on_required_evidence": True,
                    "adaptive_thresholds": {"enabled": True},
                },
            }
        },
    )

    assert should_gate is True
    assert metadata["thresholds"]["min_action_confidence"] == pytest.approx(0.7)
    assert metadata["thresholds"]["clarification_cost"] == pytest.approx(0.32)
    assert "low_action_confidence" in metadata["reasons"]


def test_gate_ignores_required_evidence_when_configured_off():
    should_gate, metadata = should_clarify(
        candidate_metadata={
            "chosen_confidence": 0.35,
            "claim_conflict_count": 0,
            "claim_required_evidence_count": 1,
        },
        config={"dual_dag": {"enabled": True, "gated_clarification": {"enabled": True}}},
    )

    assert should_gate is True
    assert "required_evidence" not in metadata["reasons"]
    assert metadata["reason"] == "low_action_confidence"


def test_gate_fires_on_required_evidence_when_enabled_and_risky():
    should_gate, metadata = should_clarify(
        candidate_metadata={
            "chosen_confidence": 0.5,
            "claim_conflict_count": 0,
            "claim_required_evidence_count": 1,
        },
        config={
            "dual_dag": {
                "enabled": True,
                "gated_clarification": {
                    "enabled": True,
                    "min_action_confidence": 0.4,
                    "clarify_on_required_evidence": True,
                    "clarification_cost": 0.2,
                },
            }
        },
    )

    assert should_gate is True
    assert metadata["reason"] == "required_evidence"
    assert metadata["claim_required_evidence_count"] == 1


def test_gate_allows_required_evidence_when_risk_is_below_cost():
    should_gate, metadata = should_clarify(
        candidate_metadata={
            "chosen_confidence": 0.8,
            "claim_conflict_count": 0,
            "claim_required_evidence_count": 1,
        },
        config={
            "dual_dag": {
                "enabled": True,
                "gated_clarification": {
                    "enabled": True,
                    "min_action_confidence": 0.4,
                    "clarify_on_required_evidence": True,
                    "clarification_cost": 0.4,
                },
            }
        },
    )

    assert should_gate is False
    assert metadata["risk_exceeds_clarification_cost"] is False


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


def test_gate_records_metadata_without_coordination_action_when_configured():
    action = {
        "action": "place",
        "block": "ys",
        "position": "(0,0)",
        "layer": 0,
        "_action_candidate_metadata": {"chosen_confidence": 0.1, "claim_conflict_count": 0},
    }

    gated = _apply_clarification_gate(
        action,
        {
            "dual_dag": {
                "enabled": True,
                "gated_clarification": {
                    "enabled": True,
                    "coordination_actions": {"enabled": False},
                },
            }
        },
    )

    assert gated["action"] == "place"
    assert gated["_gated_clarification"]["reason"] == "low_action_confidence"
    assert gated["_gated_clarification"]["decision"] == "allow"


def test_gate_suppresses_clarification_when_episode_budget_exhausted():
    action = {
        "action": "place",
        "block": "ys",
        "position": "(0,0)",
        "layer": 0,
        "_action_candidate_metadata": {"chosen_confidence": 0.1, "claim_conflict_count": 0},
    }

    gated = _apply_clarification_gate(
        action,
        {"dual_dag": {"enabled": True, "gated_clarification": {"enabled": True, "max_clarifications_per_episode": 1}}},
        turn_index=2,
        previous_actions=[{"action": "clarify", "_clarification_turn_index": 1}],
    )

    assert gated["action"] == "place"
    assert gated["_gated_clarification"]["decision"] == "allow"
    assert gated["_gated_clarification"]["suppression_reason"] == "clarification_budget_exhausted"


def test_gate_suppresses_clarification_during_cooldown():
    action = {"action": "place", "_action_candidate_metadata": {"chosen_confidence": 0.1}}

    gated = _apply_clarification_gate(
        action,
        {"dual_dag": {"enabled": True, "gated_clarification": {"enabled": True, "clarification_cooldown_turns": 2}}},
        turn_index=3,
        previous_actions=[{"action": "clarify", "_clarification_turn_index": 2}],
    )

    assert gated["action"] == "place"
    assert gated["_gated_clarification"]["suppression_reason"] == "clarification_cooldown"


def test_gate_suppresses_late_clarification_when_remaining_turns_too_low():
    action = {"action": "place", "_action_candidate_metadata": {"chosen_confidence": 0.1}}

    gated = _apply_clarification_gate(
        action,
        {
            "run": {"turns": 5},
            "dual_dag": {
                "enabled": True,
                "gated_clarification": {"enabled": True, "min_remaining_turns_after_clarification": 2},
            },
        },
        turn_index=4,
    )

    assert gated["action"] == "place"
    assert gated["_gated_clarification"]["suppression_reason"] == "late_clarification"


def test_gate_suppresses_duplicate_clarification_key():
    action = {
        "action": "place",
        "block": "ys",
        "position": "(0,0)",
        "layer": 0,
        "_action_candidate_metadata": {
            "chosen_candidate_id": "action:1:0",
            "chosen_confidence": 0.1,
            "candidates": [{
                "node_id": "action:1:0",
                "action": {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0},
            }],
        },
    }
    first = _apply_clarification_gate(
        action,
        {"dual_dag": {"enabled": True, "gated_clarification": {"enabled": True}}},
        turn_index=1,
    )

    second = _apply_clarification_gate(
        action,
        {
            "dual_dag": {
                "enabled": True,
                "gated_clarification": {"enabled": True, "prevent_duplicate_clarifications": True},
            }
        },
        turn_index=2,
        previous_actions=[first],
    )

    assert first["action"] == "clarify"
    assert second["action"] == "place"
    assert second["_gated_clarification"]["suppression_reason"] == "duplicate_clarification"


def test_oracle_aware_rule_suppresses_single_executable_candidate():
    action = {
        "action": "place",
        "block": "ys",
        "position": "(0,0)",
        "layer": 0,
        "_action_candidate_metadata": {
            "chosen_confidence": 0.1,
            "candidates": [{
                "node_id": "action:1:0",
                "state": "executable",
                "confidence": 0.1,
                "action": {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0},
            }],
        },
    }

    gated = _apply_clarification_gate(
        action,
        {
            "craft": {"use_oracle": True},
            "dual_dag": {
                "enabled": True,
                "gated_clarification": {
                    "enabled": True,
                    "oracle_aware_rules": {"enabled": True},
                },
            },
        },
    )

    assert gated["action"] == "place"
    assert gated["_gated_clarification"]["suppression_reason"] == "single_oracle_candidate_executable"


def test_oracle_aware_rule_allows_execution_blocker():
    action = {
        "action": "place",
        "block": "yl",
        "position": "(0,0)",
        "layer": 0,
        "_action_candidate_metadata": {
            "chosen_candidate_id": "action:1:0",
            "chosen_confidence": 0.9,
            "candidates": [{
                "node_id": "action:1:0",
                "state": "executable",
                "confidence": 0.9,
                "action": {"action": "place", "block": "yl", "position": "(0,0)", "layer": 0},
            }],
        },
    }

    gated = _apply_clarification_gate(
        action,
        {
            "craft": {"use_oracle": True},
            "dual_dag": {
                "enabled": True,
                "gated_clarification": {
                    "enabled": True,
                    "oracle_aware_rules": {"enabled": True},
                },
            },
        },
    )

    assert gated["action"] == "clarify"
    assert gated["_gated_clarification"]["reason"] == "large_block_span_uncertainty"


def test_oracle_aware_rule_suppresses_large_candidate_margin():
    action = {
        "action": "place",
        "block": "ys",
        "position": "(0,0)",
        "layer": 0,
        "_action_candidate_metadata": {
            "chosen_confidence": 0.4,
            "candidates": [
                {"node_id": "a", "state": "executable", "confidence": 0.9, "action": {"action": "place"}},
                {"node_id": "b", "state": "executable", "confidence": 0.2, "action": {"action": "place"}},
            ],
        },
    }

    gated = _apply_clarification_gate(
        action,
        {
            "craft": {"use_oracle": True},
            "dual_dag": {
                "enabled": True,
                "gated_clarification": {
                    "enabled": True,
                    "oracle_aware_rules": {"enabled": True, "min_top_candidate_margin": 0.5},
                },
            },
        },
    )

    assert gated["action"] == "place"
    assert gated["_gated_clarification"]["suppression_reason"] == "oracle_candidate_margin_sufficient"


def test_oracle_aware_rule_does_not_change_non_oracle_mode():
    action = {
        "action": "place",
        "block": "ys",
        "position": "(0,0)",
        "layer": 0,
        "_action_candidate_metadata": {
            "chosen_confidence": 0.1,
            "candidates": [{"node_id": "a", "state": "executable", "confidence": 0.1, "action": {"action": "place"}}],
        },
    }

    gated = _apply_clarification_gate(
        action,
        {
            "craft": {"use_oracle": False},
            "dual_dag": {
                "enabled": True,
                "gated_clarification": {
                    "enabled": True,
                    "oracle_aware_rules": {"enabled": True},
                },
            },
        },
    )

    assert gated["action"] == "clarify"


def test_required_evidence_clarification_references_public_claim_only():
    action = _apply_clarification_gate(
        {
            "action": "place",
            "block": "rs",
            "position": "(0,0)",
            "_action_candidate_metadata": {
                "chosen_candidate_id": "action:1:0",
                "chosen_confidence": 0.5,
                "claim_conflict_count": 0,
                "claim_required_evidence_count": 1,
                "public_evidence_summary": [{
                    "candidate_id": "action:1:0",
                    "missing_public_evidence_claims": [{
                        "claim_id": "claim:D3:1",
                        "public_message": "I am unsure whether the bottom left block should be green.",
                    }],
                }],
            },
        },
        {
            "dual_dag": {
                "enabled": True,
                "gated_clarification": {
                    "enabled": True,
                    "min_action_confidence": 0.4,
                    "clarify_on_required_evidence": True,
                    "clarification_cost": 0.2,
                },
            }
        },
    )

    assert action["action"] == "clarify"
    assert "bottom left block should be green" in action["clarification"]
    serialized = str(action).lower()
    assert "target_structure" not in serialized
    assert "oracle" not in serialized


def test_gate_fires_on_conflict_only_when_risk_exceeds_clarification_cost():
    should_gate, metadata = should_clarify(
        candidate_metadata={"chosen_confidence": 0.5, "claim_conflict_count": 1},
        config={
            "dual_dag": {
                "enabled": True,
                "gated_clarification": {
                    "enabled": True,
                    "min_action_confidence": 0.4,
                    "max_conflict_count": 0,
                    "clarification_cost": 0.2,
                },
            }
        },
    )

    assert should_gate is True
    assert metadata["reason"] == "claim_conflict"
    assert metadata["risk_score"] == 0.5


def test_gate_ignores_large_block_span_when_configured_off():
    should_gate, metadata = should_clarify(
        candidate_metadata={
            "chosen_candidate_id": "action:1:0",
            "chosen_confidence": 0.9,
            "claim_conflict_count": 0,
            "candidates": [{
                "node_id": "action:1:0",
                "action": {"action": "place", "block": "yl", "position": "(0,0)", "layer": 0},
            }],
        },
        config={
            "dual_dag": {
                "enabled": True,
                "gated_clarification": {
                    "enabled": True,
                    "clarify_on_large_block_span_uncertainty": False,
                },
            }
        },
    )

    assert should_gate is False
    assert metadata["reason"] == "none"


def test_runtime_decision_support_is_config_gated():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    candidates = [{"node_id": "action:1:0", "action": {"action": "place"}, "confidence": 0.8}]

    assert _runtime_decision_support(
        runtime=runtime,
        candidates=candidates,
        config={"dual_dag": {"enabled": True, "runtime_decision_support": {"enabled": False}}},
        turn_index=1,
    ) == {}

    support = _runtime_decision_support(
        runtime=runtime,
        candidates=candidates,
        config={"dual_dag": {"enabled": True, "runtime_decision_support": {"enabled": True}}},
        turn_index=1,
    )

    assert support["recommended_candidate_id"] == "action:1:0"


def test_runtime_decision_support_updates_candidate_state_and_hypotheses():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    candidate = {
        "node_id": "action:2:0",
        "action_type": "place_block",
        "action": {"action": "place", "block": "rs", "position": "(0,0)", "layer": 1},
        "state": "candidate",
        "confidence": 0.5,
        "supported_by": [],
        "conflicts_with": [],
        "required_evidence": ["claim:D1:1"],
        "metadata": {"physically_verified": False},
    }

    support = _runtime_decision_support(
        runtime=runtime,
        candidates=[candidate],
        config={"dual_dag": {"enabled": True, "runtime_decision_support": {"enabled": True}}},
        turn_index=2,
    )

    assert runtime.action_nodes["action:2:0"]["state"] == "waiting_for_evidence"
    assert support["candidates"][0]["state"] == "waiting_for_evidence"
    assert support["candidates"][0]["unlock"]["reason"] == "required_evidence_unresolved"
    hypothesis = runtime.hypotheses()["hypothesis:required_evidence:claim:D1:1:action:2:0"]
    assert hypothesis["content"]["last_updated_turn"] == 2

    runtime.add_resolved_fact(
        fact_id="claim_D1_1",
        turn_index=3,
        summary="D1 claim resolved publicly",
        evidence_ids=["claim:D1:1"],
        confidence=0.9,
    )
    support = _runtime_decision_support(
        runtime=runtime,
        candidates=[candidate],
        config={"dual_dag": {"enabled": True, "runtime_decision_support": {"enabled": True}}},
        turn_index=3,
    )

    assert runtime.action_nodes["action:2:0"]["state"] == "executable"
    assert support["candidates"][0]["state"] == "executable"
    assert runtime.hypotheses()["hypothesis:required_evidence:claim:D1:1:action:2:0"]["content"]["status"] == "resolved"


def test_prepare_runtime_action_candidates_syncs_unlock_state_to_metadata_candidates():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    action_candidates = action_candidates_from_moves(
        moves=[{"action": "place", "block": "ys", "position": "(0,0)", "layer": 0}],
        reported_claims={},
        turn_index=1,
    )

    support = _prepare_runtime_action_candidates(
        runtime=runtime,
        action_candidates=action_candidates,
        config={"dual_dag": {"enabled": True, "runtime_decision_support": {"enabled": True}}},
        turn_index=1,
    )

    assert action_candidates[0].state == "executable"
    assert action_candidates[0].metadata["unlock"]["reason"] == "physically_verified"
    assert support["candidates"][0]["state"] == "executable"


def test_runtime_decision_support_prioritizes_recommended_oracle_candidate():
    oracle_moves = [
        {"action": "place", "block": "bs", "position": "(0,0)", "layer": 0, "span_to": None},
        {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0, "span_to": None},
    ]
    action_candidates = action_candidates_from_moves(
        moves=oracle_moves,
        reported_claims={},
        turn_index=1,
    )

    prioritized_moves, prioritized_candidates = _prioritize_supported_candidates(
        oracle_moves=oracle_moves,
        action_candidates=action_candidates,
        decision_support={"recommended_candidate_id": "action:1:1"},
    )

    assert prioritized_moves[0]["block"] == "ys"
    assert prioritized_candidates[0].node_id == "action:1:1"
