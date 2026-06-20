import json

from benchmarks.craft.craft_protocol import CraftPrivateView, CraftPublicState
from benchmarks.craft.dual_dag.evidence_prompt import (
    append_public_evidence_context,
    append_public_evidence_summary,
    build_public_evidence_context,
    build_public_evidence_summary,
    prompt_contains_hidden_state_key,
)
from benchmarks.craft.dual_dag.runtime import DualDAGRuntime
from benchmarks.craft.dual_dag.serialization import sanitize_for_serialization


def test_runtime_creates_deterministic_nodes_and_reset_clears_state():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.update_private_observation(
        director_id="D1",
        turn_index=1,
        private_view=CraftPrivateView(
            director_id="D1",
            view_name="left",
            raw_view="hidden raw view",
            text_view="yellow bottom left",
            structured_view={"row_0": [{"color": "yellow", "size": 1}]},
        ),
    )

    assert "observed:D1:1:row_0:0" in runtime.epistemic_nodes
    runtime.reset()
    assert runtime.snapshot_summary()["epistemic_node_count"] == 0


def test_runtime_keeps_reported_claims_unresolved_by_default():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    claim = runtime.add_reported_claim(
        director_id="D1",
        turn_index=1,
        message="Please place yellow at bottom left.",
    )

    assert claim["node_id"] == "claim:D1:1"
    assert runtime.epistemic_nodes["claim:D1:1"]["node_type"] == "reported_claim"
    assert "resolved_fact" not in {node["node_type"] for node in runtime.epistemic_nodes.values()}


def test_runtime_links_action_candidates_to_supporting_claims():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.add_reported_claim(
        director_id="D1",
        turn_index=1,
        message="Please place yellow at bottom left.",
    )
    candidates = runtime.build_action_candidates(
        turn_index=1,
        oracle_moves=[{"action": "place", "block": "ys", "position": "(0,0)", "layer": 0, "span_to": None}],
    )

    assert candidates[0]["node_id"] == "action:1:0"
    assert runtime.action_edges == [{
        "source_id": "claim:D1:1",
        "target_id": "action:1:0",
        "edge_type": "supports",
        "metadata": {"turn_index": 1},
    }]


def test_runtime_links_observations_to_matching_claims():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.update_private_observation(
        director_id="D1",
        turn_index=1,
        private_view=CraftPrivateView(
            director_id="D1",
            view_name="left",
            raw_view="hidden raw view",
            text_view="yellow bottom left",
            structured_view={"row_0": [{"color": "yellow", "size": 1}]},
        ),
    )

    runtime.add_reported_claim(
        director_id="D1",
        turn_index=1,
        message="Bottom left is yellow small.",
    )

    assert {
        "source_id": "observed:D1:1:row_0:0",
        "target_id": "claim:D1:1",
        "edge_type": "supports",
        "metadata": {"turn_index": 1, "matched_keywords": ["bottom", "left", "small", "yellow"]},
    } in runtime.epistemic_edges


def test_runtime_keeps_all_reported_claims_by_node_id():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.add_reported_claim(
        director_id="D1",
        turn_index=1,
        message="Please place yellow at bottom left.",
    )
    runtime.add_reported_claim(
        director_id="D1",
        turn_index=2,
        message="Please place blue at top right.",
    )

    assert sorted(runtime.reported_claims()) == ["claim:D1:1", "claim:D1:2"]


def test_runtime_adds_public_builder_action_fact():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.update_public_state(
        turn_index=2,
        public_state=CraftPublicState(
            turn_index=2,
            public_messages=[],
            builder_actions=[{"action": "place", "block": "ys", "_hidden": "nope"}],
            visible_constructed_structure={},
            progress_summary=None,
        ),
    )
    runtime.add_public_builder_action(turn_index=2, action={"action": "clarify", "_secret": "nope"})

    assert "public:builder_action:2:0" in runtime.epistemic_nodes
    assert runtime.epistemic_nodes["public:builder_action:2:1"]["content"] == {
        "builder_action": {"action": "clarify"},
    }
    serialized = json.dumps(runtime.serialized_snapshot())
    assert "_secret" not in serialized
    assert "_hidden" not in serialized


def test_serialization_strips_forbidden_hidden_keys():
    serialized = sanitize_for_serialization({
        "target_structure": "hidden",
        "oracle_moves": ["hidden"],
        "content": {"ok": True, "raw_private_view": "hidden"},
    })

    assert serialized == {"content": {"ok": True}}


def test_runtime_reset_clears_all_node_and_edge_counts():
    runtime = DualDAGRuntime(director_ids=["D1", "D2", "D3"], config={})
    runtime.update_private_observation(
        director_id="D1",
        turn_index=1,
        private_view=CraftPrivateView(
            director_id="D1",
            view_name="front",
            raw_view={"hidden_labels": ["secret"]},
            text_view="bottom left yellow small",
            structured_view={"row_0": [{"color": "yellow", "size": 1}]},
        ),
    )
    runtime.add_reported_claim(director_id="D1", turn_index=1, message="Bottom left is yellow small.")

    assert sorted(runtime.epistemic_nodes) == [
        "claim:D1:1",
        "constraint:D1:1:0",
        "observed:D1:1:row_0:0",
    ]
    assert runtime.snapshot_summary()["epistemic_node_count"] == 3

    runtime.reset()

    assert runtime.snapshot_summary() == {
        "epistemic_node_count": 0,
        "action_node_count": 0,
        "epistemic_edge_count": 0,
        "action_edge_count": 0,
        "reported_claim_count": 0,
        "hypothesis_count": 0,
        "resolved_fact_count": 0,
        "action_candidate_count": 0,
    }


def test_runtime_public_builder_action_strips_prefixed_private_metadata():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})

    runtime.add_public_builder_action(
        turn_index=2,
        action={
            "action": "place",
            "block": "ys",
            "position": "(0,0)",
            "layer": 0,
            "_action_candidate_metadata": {"oracle_moves": ["hidden"]},
        },
    )

    assert runtime.epistemic_nodes["public:builder_action:2:0"]["content"]["builder_action"] == {
        "action": "place",
        "block": "ys",
        "position": "(0,0)",
        "layer": 0,
    }


def test_runtime_marks_chosen_action_candidate_executed_after_builder_action():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.add_action_candidates(turn_index=2, candidates=[{
        "node_id": "action:2:0",
        "action": {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0},
        "confidence": 0.9,
        "supported_by": [],
        "conflicts_with": [],
        "required_evidence": [],
        "metadata": {},
    }])

    runtime.add_public_builder_action(
        turn_index=2,
        action={
            "action": "place",
            "block": "ys",
            "position": "(0,0)",
            "layer": 0,
            "_action_candidate_metadata": {
                "chosen_candidate_id": "action:2:0",
                "oracle_moves": ["hidden"],
            },
        },
    )

    candidate = runtime.action_nodes["action:2:0"]
    assert candidate["state"] == "executed"
    assert candidate["metadata"]["execution"] == {
        "state": "executed",
        "turn_index": 2,
        "public_action_id": "public:builder_action:2:0",
        "action": {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0},
    }
    assert {
        "source_id": "public:builder_action:2:0",
        "target_id": "action:2:0",
        "edge_type": "executes_action",
        "metadata": {"turn_index": 2, "state": "executed"},
    } in runtime.action_edges
    assert runtime.update_action_candidate_states(turn_index=3)[0]["state"] == "executed"
    assert not any(edge.get("edge_type") == "blocks_action" for edge in runtime.action_edges)
    assert "oracle_moves" not in json.dumps(runtime.serialized_snapshot())


def test_runtime_marks_matching_action_candidate_executed_without_chosen_id():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.add_action_candidates(turn_index=2, candidates=[{
        "node_id": "action:2:0",
        "action": {"action": "remove", "position": "(1,1)", "layer": 0},
        "confidence": 0.8,
        "supported_by": [],
        "conflicts_with": [],
        "required_evidence": [],
        "metadata": {},
    }])

    runtime.add_public_builder_action(
        turn_index=2,
        action={"action": "remove", "position": "(1,1)", "layer": 0},
    )

    assert runtime.action_nodes["action:2:0"]["state"] == "executed"
    assert runtime.action_edges[-1]["edge_type"] == "executes_action"


def test_runtime_does_not_mark_non_matching_or_unexecuted_builder_action():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.add_action_candidates(turn_index=2, candidates=[{
        "node_id": "action:2:0",
        "action": {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0},
        "confidence": 0.9,
        "supported_by": [],
        "conflicts_with": [],
        "required_evidence": [],
        "metadata": {},
    }])

    runtime.add_public_builder_action(
        turn_index=2,
        action={"action": "place", "block": "bs", "position": "(1,1)", "layer": 0},
    )
    runtime.add_public_builder_action(
        turn_index=3,
        action={
            "action": "place",
            "block": "ys",
            "position": "(0,0)",
            "layer": 0,
            "_action_candidate_metadata": {"chosen_candidate_id": "action:2:0"},
        },
        executed=False,
    )

    assert runtime.action_nodes["action:2:0"].get("state") != "executed"
    assert not any(edge.get("edge_type") == "executes_action" for edge in runtime.action_edges)


def test_serialized_snapshot_removes_forbidden_hidden_keys_recursively():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.epistemic_nodes["bad"] = {
        "node_id": "bad",
        "target_structure": {"secret": True},
        "content": {
            "safe": "value",
            "oracle_moves": [{"hidden": True}],
            "nested": {"raw_private_view": ["hidden"], "visible": 1},
        },
    }

    snapshot = runtime.serialized_snapshot()

    assert snapshot["schema_version"] == "1.0.0"
    assert snapshot["epistemic_nodes"] == [
        {"node_id": "bad", "content": {"safe": "value", "nested": {"visible": 1}}}
    ]
    assert "target_structure" not in str(snapshot)
    assert "oracle_moves" not in str(snapshot)
    assert "raw_private_view" not in str(snapshot)


def test_sanitize_for_serialization_removes_forbidden_keys_recursively():
    assert sanitize_for_serialization({
        "visible": 1,
        "hidden_spans": ["secret"],
        "children": [{"hidden_labels": ["secret"], "visible": 2}],
    }) == {"visible": 1, "children": [{"visible": 2}]}


def test_runtime_public_state_actions_become_public_facts():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    public_state = CraftPublicState(
        turn_index=3,
        public_messages=[],
        builder_actions=[{"action": "remove", "position": "(1,1)", "layer": 0}],
        visible_constructed_structure={},
        progress_summary=None,
    )

    runtime.update_public_state(turn_index=3, public_state=public_state)

    assert runtime.epistemic_nodes["public:builder_action:3:0"]["node_type"] == "public_fact"


def test_runtime_current_turn_decision_support_recommends_public_candidate():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    candidates = runtime.build_action_candidates(
        turn_index=1,
        oracle_moves=[
            {"action": "place", "block": "bs", "position": "(0,0)", "layer": 0, "span_to": None},
            {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0, "span_to": None},
        ],
    )
    candidates[0]["confidence"] = 0.4
    candidates[0]["conflicts_with"] = ["claim:D1:1"]
    candidates[1]["confidence"] = 0.7

    support = runtime.current_turn_decision_support(turn_index=1, candidates=candidates)

    assert support["recommended_candidate_id"] == "action:1:1"
    assert support["has_conflicts"] is True
    assert support["candidates"][0] == {
        "node_id": "action:1:0",
        "action": {"action": "place", "block": "bs", "position": "(0,0)", "layer": 0, "span_to": None},
        "state": "candidate",
        "confidence": 0.4,
        "claim_support_count": 0,
        "claim_conflict_count": 1,
        "claim_required_evidence_count": 0,
    }
    assert "target_structure" not in json.dumps(support)
    assert "oracle_moves" not in json.dumps(support)


def test_runtime_creates_hypothesis_for_uncertain_claim():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})

    runtime.add_reported_claim(
        director_id="D1",
        turn_index=1,
        message="I am unsure whether the bottom left block is blue, please confirm.",
    )

    hypotheses = runtime.hypotheses()
    assert list(hypotheses) == ["hypothesis:unresolved_claim:claim:D1:1"]
    hypothesis = hypotheses["hypothesis:unresolved_claim:claim:D1:1"]
    assert hypothesis["node_type"] == "hypothesis"
    assert hypothesis["content"]["hypothesis_type"] == "unresolved_claim"
    assert hypothesis["content"]["source_claim_ids"] == ["claim:D1:1"]
    assert hypothesis["content"]["status"] == "open"
    assert runtime.snapshot_summary()["hypothesis_count"] == 1
    assert {
        "source_id": "claim:D1:1",
        "target_id": "hypothesis:unresolved_claim:claim:D1:1",
        "edge_type": "derived_from",
        "metadata": {"turn_index": 1, "reason": "uncertain_claim"},
    } in runtime.epistemic_edges


def test_runtime_creates_and_updates_action_hypothesis_for_required_evidence():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.add_reported_claim(
        director_id="D1",
        turn_index=1,
        message="Bottom left may be blue small, please confirm.",
    )
    candidate = {
        "node_id": "action:2:0",
        "action": {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0},
        "confidence": 0.6,
        "supported_by": [],
        "conflicts_with": [],
        "required_evidence": ["claim:D1:1"],
    }

    runtime.add_action_candidates(turn_index=2, candidates=[candidate])
    runtime.add_action_candidates(turn_index=3, candidates=[candidate])

    hypothesis = runtime.hypotheses()["hypothesis:required_evidence:claim:D1:1:action:2:0"]
    assert hypothesis["content"]["hypothesis_type"] == "required_evidence"
    assert hypothesis["content"]["source_claim_ids"] == ["claim:D1:1"]
    assert hypothesis["content"]["action_candidate_ids"] == ["action:2:0"]
    assert hypothesis["content"]["created_turn"] == 2
    assert hypothesis["content"]["last_updated_turn"] == 3
    expected_edges = [
        {
            "source_id": "claim:D1:1",
            "target_id": "hypothesis:unresolved_claim:claim:D1:1",
            "edge_type": "derived_from",
            "metadata": {"turn_index": 1, "reason": "uncertain_claim"},
        },
        {
            "source_id": "claim:D1:1",
            "target_id": "hypothesis:required_evidence:claim:D1:1:action:2:0",
            "edge_type": "requires_confirmation_from",
            "metadata": {"turn_index": 2, "action_candidate_id": "action:2:0", "last_updated_turn": 3},
        },
        {
            "source_id": "hypothesis:required_evidence:claim:D1:1:action:2:0",
            "target_id": "action:2:0",
            "edge_type": "supports",
            "metadata": {"turn_index": 2, "source_claim_id": "claim:D1:1", "last_updated_turn": 3},
        },
    ]
    for edge in expected_edges:
        assert edge in runtime.epistemic_edges


def test_runtime_creates_action_hypothesis_for_conflicting_evidence():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.add_reported_claim(
        director_id="D1",
        turn_index=1,
        message="Bottom left is blue small.",
    )
    candidate = {
        "node_id": "action:2:0",
        "action": {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0},
        "confidence": 0.4,
        "supported_by": [],
        "conflicts_with": ["claim:D1:1"],
        "required_evidence": [],
    }

    runtime.add_action_candidates(turn_index=2, candidates=[candidate])

    hypothesis = runtime.hypotheses()["hypothesis:conflicting_evidence:claim:D1:1:action:2:0"]
    assert hypothesis["content"]["hypothesis_type"] == "conflicting_evidence"
    assert hypothesis["content"]["source_claim_ids"] == ["claim:D1:1"]
    assert hypothesis["content"]["action_candidate_ids"] == ["action:2:0"]
    assert hypothesis["confidence"] == 0.3
    expected_edges = [
        {
            "source_id": "claim:D1:1",
            "target_id": "hypothesis:conflicting_evidence:claim:D1:1:action:2:0",
            "edge_type": "conflicts_with",
            "metadata": {"turn_index": 2, "action_candidate_id": "action:2:0"},
        },
        {
            "source_id": "hypothesis:conflicting_evidence:claim:D1:1:action:2:0",
            "target_id": "action:2:0",
            "edge_type": "conflicts_with",
            "metadata": {"turn_index": 2, "source_claim_id": "claim:D1:1"},
        },
    ]
    for edge in expected_edges:
        assert edge in runtime.epistemic_edges


def test_hypothesis_lifecycle_updates_supported_state_deterministically():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.add_reported_claim(
        director_id="D1",
        turn_index=1,
        message="I am unsure whether the bottom left block is blue, please confirm.",
    )
    hypothesis_id = "hypothesis:unresolved_claim:claim:D1:1"
    runtime.epistemic_edges.append({
        "source_id": "claim:D1:1",
        "target_id": hypothesis_id,
        "edge_type": "supports",
        "metadata": {"turn_index": 2},
    })

    first = runtime.update_hypothesis_lifecycle(turn_index=2)[0]
    first_confidence = first["confidence"]
    second = runtime.update_hypothesis_lifecycle(turn_index=3)[0]

    assert first["content"]["status"] == "supported"
    assert first["content"]["support_count"] == 1
    assert first_confidence == 0.55
    assert second["confidence"] == first_confidence
    assert second["content"]["last_updated_turn"] == 3


def test_hypothesis_lifecycle_marks_conflicted_hypothesis():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.add_reported_claim(
        director_id="D1",
        turn_index=1,
        message="Bottom left is blue small.",
    )
    runtime.add_action_candidates(turn_index=2, candidates=[{
        "node_id": "action:2:0",
        "action": {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0},
        "confidence": 0.4,
        "supported_by": [],
        "conflicts_with": ["claim:D1:1"],
        "required_evidence": [],
    }])

    hypothesis = runtime.update_hypothesis_lifecycle(turn_index=2)[0]

    assert hypothesis["content"]["status"] == "conflicted"
    assert hypothesis["content"]["conflict_count"] == 2
    assert hypothesis["confidence"] == 0.0


def test_hypothesis_lifecycle_marks_resolution_ready_hypothesis_resolved():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.add_reported_claim(
        director_id="D1",
        turn_index=1,
        message="I am unsure whether the top left block is blue, please confirm.",
    )
    runtime.add_resolved_fact(
        fact_id="top_left_blue",
        turn_index=2,
        summary="Top left blue claim has enough public evidence.",
        evidence_ids=["claim:D1:1"],
        confidence=0.8,
        content={"block": "bs"},
    )

    hypothesis = runtime.update_hypothesis_lifecycle(turn_index=2)[0]

    assert hypothesis["content"]["status"] == "resolved"
    assert hypothesis["content"]["resolved_evidence_count"] == 1
    assert hypothesis["content"]["resolution_ready"] is True
    assert hypothesis["confidence"] == 0.6


def test_hypothesis_lifecycle_marks_invalidated_from_action_state_and_preserves_scores():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.add_reported_claim(
        director_id="D1",
        turn_index=1,
        message="Bottom left may be blue small, please confirm.",
    )
    candidate = {
        "node_id": "action:2:0",
        "action": {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0},
        "confidence": 0.4,
        "supported_by": [],
        "conflicts_with": [],
        "required_evidence": ["claim:D1:1"],
    }
    runtime.add_action_candidates(turn_index=2, candidates=[candidate])
    hypothesis = runtime.hypotheses()["hypothesis:required_evidence:claim:D1:1:action:2:0"]
    hypothesis["content"].update({
        "expected_progress_gain": 0.7,
        "mistake_cost": 0.4,
        "clarification_cost": 0.2,
    })
    runtime.action_nodes["action:2:0"]["state"] = "invalidated"

    runtime.update_hypothesis_lifecycle(turn_index=3)
    updated = runtime.hypotheses()["hypothesis:required_evidence:claim:D1:1:action:2:0"]

    assert updated["content"]["status"] == "invalidated"
    assert updated["content"]["expected_progress_gain"] == 0.7
    assert updated["content"]["mistake_cost"] == 0.4
    assert updated["content"]["clarification_cost"] == 0.2


def test_serialized_hypothesis_excludes_hidden_state():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.add_reported_claim(
        director_id="D1",
        turn_index=1,
        message="I am unsure whether the top right block is red.",
    )

    serialized = json.dumps(runtime.serialized_snapshot())

    assert "hypothesis" in serialized
    assert "target_structure" not in serialized
    assert "oracle_moves" not in serialized
    assert "raw_private_view" not in serialized


def test_runtime_adds_resolved_fact_with_resolved_by_edges():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.add_reported_claim(
        director_id="D1",
        turn_index=1,
        message="Bottom left is yellow small.",
    )
    runtime.update_public_state(
        turn_index=2,
        public_state=CraftPublicState(
            turn_index=2,
            public_messages=[],
            builder_actions=[{"action": "place", "block": "ys", "position": "(0,0)", "layer": 0}],
            visible_constructed_structure={},
            progress_summary=None,
        ),
    )

    resolved = runtime.add_resolved_fact(
        fact_id="bottom_left_yellow",
        turn_index=2,
        summary="Bottom left can be treated as yellow small for action decisions.",
        evidence_ids=["claim:D1:1", "public:builder_action:2:0", "claim:D1:1"],
        confidence=0.92,
        content={"position": "(0,0)", "block": "ys"},
    )

    assert resolved == {
        "node_id": "resolved_fact:bottom_left_yellow",
        "node_type": "resolved_fact",
        "content": {
            "summary": "Bottom left can be treated as yellow small for action decisions.",
            "evidence_ids": ["claim:D1:1", "public:builder_action:2:0"],
            "position": "(0,0)",
            "block": "ys",
        },
        "confidence": 0.92,
        "provenance": {
            "source": "dual_dag_runtime",
            "director_id": None,
            "turn_index": 2,
            "visibility": "public",
        },
    }
    assert runtime.resolved_facts() == {"resolved_fact:bottom_left_yellow": resolved}
    assert runtime.snapshot_summary()["resolved_fact_count"] == 1
    assert runtime.epistemic_edges[-2:] == [
        {
            "source_id": "claim:D1:1",
            "target_id": "resolved_fact:bottom_left_yellow",
            "edge_type": "resolved_by",
            "metadata": {"turn_index": 2, "resolved_fact_id": "resolved_fact:bottom_left_yellow"},
        },
        {
            "source_id": "public:builder_action:2:0",
            "target_id": "resolved_fact:bottom_left_yellow",
            "edge_type": "resolved_by",
            "metadata": {"turn_index": 2, "resolved_fact_id": "resolved_fact:bottom_left_yellow"},
        },
    ]


def test_resolved_fact_serialization_excludes_hidden_state():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})

    runtime.add_resolved_fact(
        fact_id="safe_fact",
        turn_index=3,
        summary="Publicly resolved safe fact.",
        evidence_ids=["claim:D1:1"],
        confidence=1.2,
        content={
            "visible": "ok",
            "target_structure": "hidden",
            "oracle_moves": ["hidden"],
            "nested": {"raw_private_view": "hidden", "public": True},
            "_internal": "drop",
        },
    )

    serialized = json.dumps(runtime.serialized_snapshot())

    assert "resolved_fact" in serialized
    assert "visible" in serialized
    assert "target_structure" not in serialized
    assert "oracle_moves" not in serialized
    assert "raw_private_view" not in serialized
    assert "_internal" not in serialized
    assert runtime.resolved_facts()["resolved_fact:safe_fact"]["confidence"] == 1.0


def test_action_candidate_state_unlocks_support_only_candidate():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.add_action_candidates(turn_index=2, candidates=[{
        "node_id": "action:2:0",
        "action": {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0},
        "confidence": 0.45,
        "supported_by": ["claim:D1:1"],
        "conflicts_with": [],
        "required_evidence": [],
        "metadata": {},
    }])

    updated = runtime.update_action_candidate_states(turn_index=2)

    assert updated[0]["state"] == "executable"
    assert updated[0]["metadata"]["unlock"] == {
        "state": "executable",
        "turn_index": 2,
        "reason": "supported_by_public_claims",
        "evidence_ids": ["claim:D1:1"],
    }
    assert runtime.action_edges[-1] == {
        "source_id": "claim:D1:1",
        "target_id": "action:2:0",
        "edge_type": "unlocks_action",
        "metadata": {"turn_index": 2, "state": "executable", "reason": "supported_by_public_claims"},
    }


def test_action_candidate_state_invalidates_conflicting_candidate():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.add_action_candidates(turn_index=2, candidates=[{
        "node_id": "action:2:0",
        "action": {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0},
        "confidence": 0.9,
        "supported_by": ["claim:D1:1"],
        "conflicts_with": ["claim:D2:1"],
        "required_evidence": [],
        "metadata": {"physically_verified": True},
    }])

    updated = runtime.update_action_candidate_states(turn_index=2)

    assert updated[0]["state"] == "invalidated"
    assert updated[0]["metadata"]["unlock"]["reason"] == "conflicting_evidence"
    assert runtime.action_edges[-1]["edge_type"] == "blocks_action"


def test_action_candidate_state_waits_until_required_evidence_is_resolved():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.add_action_candidates(turn_index=2, candidates=[{
        "node_id": "action:2:0",
        "action": {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0},
        "confidence": 0.9,
        "supported_by": [],
        "conflicts_with": [],
        "required_evidence": ["claim:D1:1"],
        "metadata": {"physically_verified": True},
    }])

    unresolved = runtime.update_action_candidate_states(turn_index=2)[0]
    assert unresolved["state"] == "waiting_for_evidence"
    assert unresolved["metadata"]["unlock"]["reason"] == "required_evidence_unresolved"

    runtime.add_resolved_fact(
        fact_id="claim_d1_1_resolved",
        turn_index=3,
        summary="D1 claim is resolved by public evidence.",
        evidence_ids=["claim:D1:1"],
        confidence=0.8,
        content={"block": "ys"},
    )
    resolved = runtime.update_action_candidate_states(turn_index=3)[0]

    assert resolved["state"] == "executable"
    assert resolved["metadata"]["unlock"] == {
        "state": "executable",
        "turn_index": 3,
        "reason": "required_evidence_resolved",
        "evidence_ids": ["claim:D1:1"],
    }


def test_action_candidate_state_unlocks_from_public_board_state_without_hidden_state():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.add_public_builder_action(
        turn_index=1,
        action={
            "action": "place",
            "block": "ys",
            "position": "(0,0)",
            "layer": 0,
            "_oracle_moves": ["hidden"],
        },
    )
    runtime.add_action_candidates(turn_index=2, candidates=[{
        "node_id": "action:2:0",
        "action": {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0},
        "confidence": 0.2,
        "supported_by": [],
        "conflicts_with": [],
        "required_evidence": [],
        "metadata": {},
    }])

    updated = runtime.update_action_candidate_states(turn_index=2)[0]
    serialized = json.dumps(runtime.serialized_snapshot())

    assert updated["state"] == "executable"
    assert updated["metadata"]["unlock"] == {
        "state": "executable",
        "turn_index": 2,
        "reason": "matches_public_board_state",
        "evidence_ids": ["public:builder_action:1:0"],
    }
    assert "oracle_moves" not in serialized


def test_action_candidate_state_blocks_insufficient_public_evidence():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.add_action_candidates(turn_index=2, candidates=[{
        "node_id": "action:2:0",
        "action": {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0},
        "confidence": 0.2,
        "supported_by": [],
        "conflicts_with": [],
        "required_evidence": [],
        "metadata": {},
    }])

    updated = runtime.update_action_candidate_states(turn_index=2)[0]

    assert updated["state"] == "blocked"
    assert updated["metadata"]["unlock"] == {
        "state": "blocked",
        "turn_index": 2,
        "reason": "insufficient_public_evidence",
        "evidence_ids": [],
    }


def test_runtime_persists_clarify_action_candidate_with_dependencies():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})

    clarify = runtime.add_coordination_action_candidate(
        turn_index=3,
        action_type="clarify",
        reason="required_evidence",
        related_candidate_ids=["action:3:0"],
        blocking_claim_ids=["claim:D1:2"],
        required_evidence_ids=["claim:D1:2"],
        question="Please confirm the bottom-left block color.",
        confidence=0.9,
    )

    assert clarify == {
        "node_id": "coordination:clarify:3:0",
        "action_type": "clarify",
        "action": {
            "action": "clarify",
            "reason": "required_evidence",
            "clarification": "Please confirm the bottom-left block color.",
        },
        "state": "candidate",
        "confidence": 0.9,
        "supported_by": [],
        "conflicts_with": ["claim:D1:2"],
        "required_evidence": ["claim:D1:2"],
        "metadata": {
            "coordination_action": True,
            "reason": "required_evidence",
            "related_candidate_ids": ["action:3:0"],
        },
    }
    assert runtime.snapshot()["action_nodes"] == [clarify]
    assert runtime.action_edges == [
        {
            "source_id": "claim:D1:2",
            "target_id": "coordination:clarify:3:0",
            "edge_type": "requires_clarification",
            "metadata": {"turn_index": 3, "reason": "required_evidence"},
        },
        {
            "source_id": "coordination:clarify:3:0",
            "target_id": "claim:D1:2",
            "edge_type": "waits_for_evidence",
            "metadata": {"turn_index": 3, "reason": "required_evidence"},
        },
        {
            "source_id": "coordination:clarify:3:0",
            "target_id": "action:3:0",
            "edge_type": "coordinates_action",
            "metadata": {"turn_index": 3, "reason": "required_evidence"},
        },
    ]


def test_runtime_persists_wait_for_evidence_action_candidate_without_hidden_state():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})

    wait = runtime.add_coordination_action_candidate(
        turn_index=4,
        action_type="wait_for_evidence",
        reason="conflicting_evidence",
        related_candidate_ids=["action:4:0"],
        blocking_claim_ids=["claim:D2:4"],
        required_evidence_ids=["claim:D3:4"],
        question="Please wait for public evidence before acting.",
        confidence=1.2,
    )
    serialized = json.dumps(runtime.serialized_snapshot())

    assert wait["node_id"] == "coordination:wait_for_evidence:4:0"
    assert wait["state"] == "waiting_for_evidence"
    assert wait["confidence"] == 1.0
    assert wait["action"]["action"] == "wait_for_evidence"
    assert "target_structure" not in serialized
    assert "oracle_moves" not in serialized


def test_runtime_links_suggested_constraints_to_reported_claims_without_hidden_state():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})

    claim = runtime.add_reported_claim(
        director_id="D1",
        turn_index=5,
        message="Please place blue large at the top right.",
    )
    serialized = json.dumps(runtime.serialized_snapshot())

    assert claim["content"]["suggested_constraint_ids"] == ["constraint:D1:5:0"]
    assert runtime.suggested_constraints()["constraint:D1:5:0"]["content"]["constraints"] == {
        "blocks": [],
        "colors": ["blue"],
        "sizes": ["large"],
        "locations": ["top", "right"],
    }
    assert {
        "source_id": "claim:D1:5",
        "target_id": "constraint:D1:5:0",
        "edge_type": "derived_from",
        "metadata": {"turn_index": 5, "reason": "suggested_constraint"},
    } in runtime.epistemic_edges
    assert "target_structure" not in serialized
    assert "oracle_moves" not in serialized


def test_clarification_response_resolves_required_evidence_and_updates_candidate():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.add_action_candidates(turn_index=2, candidates=[{
        "node_id": "action:2:0",
        "action": {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0},
        "confidence": 0.4,
        "supported_by": [],
        "conflicts_with": [],
        "required_evidence": ["claim:D1:1"],
        "metadata": {},
    }])
    runtime.add_coordination_action_candidate(
        turn_index=2,
        action_type="clarify",
        reason="required_evidence",
        related_candidate_ids=["action:2:0"],
        required_evidence_ids=["claim:D1:1"],
        question="Please confirm bottom left.",
    )

    result = runtime.ingest_clarification_response(
        clarify_candidate_id="coordination:clarify:2:0",
        director_id="D1",
        turn_index=3,
        message="Bottom left is yellow small.",
    )

    candidate = runtime.action_nodes["action:2:0"]
    assert result["reported_claim"]["node_id"] == "claim:D1:3"
    assert result["resolved_fact"]["node_type"] == "resolved_fact"
    assert result["resolved_fact"]["content"]["evidence_ids"] == ["claim:D1:1", "claim:D1:3"]
    assert candidate["supported_by"] == ["claim:D1:3"]
    assert candidate["state"] == "executable"
    assert candidate["metadata"]["unlock"]["reason"] == "required_evidence_resolved"
    assert runtime.hypotheses()["hypothesis:required_evidence:claim:D1:1:action:2:0"]["content"]["status"] == "resolved"
    assert {
        "source_id": "claim:D1:3",
        "target_id": "coordination:clarify:2:0",
        "edge_type": "clarification_response",
        "metadata": {"turn_index": 3, "director_id": "D1"},
    } in runtime.action_edges


def test_clarification_response_can_introduce_conflict_and_invalidate_candidate():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.add_action_candidates(turn_index=2, candidates=[{
        "node_id": "action:2:0",
        "action": {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0},
        "confidence": 0.7,
        "supported_by": [],
        "conflicts_with": [],
        "required_evidence": [],
        "metadata": {},
    }])
    runtime.add_coordination_action_candidate(
        turn_index=2,
        action_type="clarify",
        reason="low_action_confidence",
        related_candidate_ids=["action:2:0"],
        question="Please confirm bottom left.",
    )

    result = runtime.ingest_clarification_response(
        clarify_candidate_id="coordination:clarify:2:0",
        director_id="D2",
        turn_index=3,
        message="Bottom left is blue small.",
    )

    candidate = runtime.action_nodes["action:2:0"]
    assert result["resolved_fact"] is None
    assert candidate["conflicts_with"] == ["claim:D2:3"]
    assert candidate["state"] == "invalidated"
    assert candidate["metadata"]["unlock"]["reason"] == "conflicting_evidence"


def test_public_evidence_summary_includes_required_public_claim_only():
    claim = {
        "node_id": "claim:D1:1",
        "node_type": "reported_claim",
        "content": {
            "director_id": "D1",
            "message": "I am unsure whether the top left block is blue.",
            "keywords": ["top", "left", "blue"],
            "uncertain": True,
        },
        "provenance": {
            "source": "director_message",
            "director_id": "D1",
            "turn_index": 1,
            "visibility": "public",
        },
    }
    candidate = {
        "node_id": "action:1:0",
        "action": {"action": "place", "block": "rs", "position": "(0,2)", "_private": "drop"},
        "supported_by": [],
        "conflicts_with": [],
        "required_evidence": ["claim:D1:1"],
    }

    summary = build_public_evidence_summary(
        candidates=[candidate],
        reported_claims={"D1": claim},
    )

    assert summary == [{
        "candidate_id": "action:1:0",
        "action": {"action": "place", "block": "rs", "position": "(0,2)"},
        "supporting_public_claims": [],
        "conflicting_public_claims": [],
        "missing_public_evidence_claims": [{
            "claim_id": "claim:D1:1",
            "director_id": "D1",
            "turn_index": 1,
            "public_message": "I am unsure whether the top left block is blue.",
            "keywords": ["top", "left", "blue"],
            "uncertain": True,
        }],
    }]


def test_public_evidence_prompt_omits_hidden_state_keys():
    claim = {
        "node_id": "claim:D2:3",
        "content": {
            "director_id": "D2",
            "message": "Please confirm the bottom right orange block.",
            "keywords": ["bottom", "right", "orange"],
            "uncertain": True,
        },
        "provenance": {"turn_index": 3, "visibility": "public"},
    }
    candidate = {
        "node_id": "action:3:1",
        "action": {"action": "place", "block": "bs", "position": "(2,0)"},
        "supported_by": [],
        "conflicts_with": [],
        "required_evidence": ["claim:D2:3"],
    }

    prompt = append_public_evidence_summary(
        prompt="Base Builder prompt",
        candidates=[candidate],
        reported_claims={"D2": claim},
    )

    assert "PUBLIC EVIDENCE SUMMARY" in prompt
    assert "Please confirm the bottom right orange block." in prompt
    assert not prompt_contains_hidden_state_key(prompt)


def test_public_evidence_prompt_noops_without_relevant_evidence():
    prompt = append_public_evidence_summary(
        prompt="Base Builder prompt",
        candidates=[{"node_id": "action:1:0", "action": {"action": "clarify"}}],
        reported_claims={},
    )

    assert prompt == "Base Builder prompt"


def test_public_evidence_context_supports_no_oracle_prompt_without_hidden_state():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.add_reported_claim(
        director_id="D1",
        turn_index=1,
        message="I am unsure whether the top left block is blue, please confirm.",
    )

    prompt = append_public_evidence_context(
        prompt="Base Builder prompt",
        reported_claims=runtime.reported_claims(),
        hypotheses=runtime.hypotheses(),
    )

    assert "PUBLIC EVIDENCE CONTEXT" in prompt
    assert "No oracle candidates are available" in prompt
    assert "requires_confirmation" in prompt
    assert "hypothesis:unresolved_claim:claim:D1:1" in prompt
    assert "target_structure" not in prompt
    assert "oracle_moves" not in prompt
    assert "raw_private_view" not in prompt
    assert not prompt_contains_hidden_state_key(prompt)


def test_public_evidence_context_uses_public_claims_only():
    public_claim = {
        "node_id": "claim:D1:1",
        "content": {
            "director_id": "D1",
            "message": "Bottom left is yellow.",
            "keywords": ["bottom", "left", "yellow"],
            "uncertain": False,
        },
        "provenance": {"turn_index": 1, "visibility": "public"},
    }
    private_claim = {
        "node_id": "claim:D2:1",
        "content": {
            "director_id": "D2",
            "message": "Hidden target_structure should not appear.",
            "keywords": ["target_structure"],
            "uncertain": True,
        },
        "provenance": {"turn_index": 1, "visibility": "private"},
    }

    context = build_public_evidence_context(
        reported_claims={"public": public_claim, "private": private_claim},
        hypotheses={},
    )

    assert context["public_claims"] == [{
        "claim_id": "claim:D1:1",
        "director_id": "D1",
        "turn_index": 1,
        "public_message": "Bottom left is yellow.",
        "keywords": ["bottom", "left", "yellow"],
        "evidence_status": "reported",
    }]
    assert "target_structure" not in json.dumps(context)
