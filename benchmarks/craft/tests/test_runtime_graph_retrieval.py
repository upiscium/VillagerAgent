from benchmarks.craft.dual_dag.runtime import DualDAGRuntime


def test_retrieve_public_graph_context_returns_prior_public_matches_only():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.add_reported_claim(
        director_id="D1",
        turn_index=1,
        message="The blue block belongs at the bottom left.",
    )
    runtime.add_reported_claim(
        director_id="D1",
        turn_index=2,
        message="The red block belongs at the top right.",
    )
    runtime.epistemic_nodes["private:matching:hidden"] = {
        "node_id": "private:matching:hidden",
        "node_type": "observed_fact",
        "content": {"keywords": ["blue", "bottom", "left"]},
        "provenance": {"visibility": "private", "turn_index": 1},
    }
    runtime.add_public_builder_action(
        turn_index=1,
        action={"action": "place", "block": "bs", "position": "(0,0)", "layer": 0},
    )

    context = runtime.retrieve_public_graph_context(
        turn_index=3,
        action={"action": "place", "block": "bs", "position": "(0,0)", "layer": 0},
    )

    assert context["query"]["location_keywords"] == ["bottom", "left"]
    assert [claim["node_id"] for claim in context["relevant_public_claims"]] == ["claim:D1:1"]
    assert context["relevant_public_claims"][0]["relation"] == "supports"
    assert [action["node_id"] for action in context["relevant_public_actions"]] == [
        "public:builder_action:1:0"
    ]


def test_historical_context_changes_decision_support_when_enabled():
    runtime = DualDAGRuntime(director_ids=["D1"], config={})
    runtime.add_reported_claim(
        director_id="D1",
        turn_index=1,
        message="The blue block belongs at the bottom left.",
    )
    candidates = [
        {
            "node_id": "action:2:0",
            "action": {"action": "place", "block": "bs", "position": "(0,0)", "layer": 0},
            "confidence": 0.5,
            "supported_by": [],
            "conflicts_with": [],
            "required_evidence": [],
        },
        {
            "node_id": "action:2:1",
            "action": {"action": "place", "block": "gs", "position": "(2,2)", "layer": 0},
            "confidence": 0.53,
            "supported_by": [],
            "conflicts_with": [],
            "required_evidence": [],
        },
    ]

    without_history = runtime.current_turn_decision_support(
        turn_index=2,
        candidates=candidates,
        use_historical_graph_context=False,
    )
    with_history = runtime.current_turn_decision_support(
        turn_index=2,
        candidates=candidates,
        use_historical_graph_context=True,
    )

    assert without_history["recommended_candidate_id"] == "action:2:1"
    assert with_history["recommended_candidate_id"] == "action:2:0"
    assert with_history["candidates"][0]["claim_support_count"] == 1
    assert with_history["candidates"][0]["confidence"] == 0.55
    assert "graph_context" not in without_history["candidates"][0]
    assert with_history["candidates"][0]["graph_context"]["relevant_public_claims"]
