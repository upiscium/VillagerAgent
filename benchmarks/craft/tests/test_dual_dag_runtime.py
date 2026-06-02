import json

from benchmarks.craft.craft_protocol import CraftPrivateView, CraftPublicState
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
