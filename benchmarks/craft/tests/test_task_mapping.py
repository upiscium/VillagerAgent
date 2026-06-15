import json

from benchmarks.craft.dual_dag.task_mapping import (
    action_candidate_to_task_node,
    action_candidates_to_task_dag,
    task_mapping_tradeoffs,
)


def test_action_candidate_maps_to_task_node_projection():
    candidate = {
        "node_id": "action:1:0",
        "action_type": "place_block",
        "action": {
            "action": "place",
            "block": "rs",
            "position": "(0,0)",
            "layer": 0,
            "_oracle_moves": ["hidden"],
        },
        "state": "candidate",
        "confidence": 0.75,
        "supported_by": ["claim:D1:1"],
        "conflicts_with": [],
        "required_evidence": [],
        "metadata": {"physically_verified": True},
    }

    node = action_candidate_to_task_node(candidate).to_dict()

    assert node["node_id"] == "task:action:1:0"
    assert node["task_type"] == "craft_action"
    assert node["status"] == "candidate"
    assert node["confidence"] == 0.75
    assert node["content"]["action"] == {
        "action": "place",
        "block": "rs",
        "position": "(0,0)",
        "layer": 0,
    }
    assert node["content"]["craft"]["physically_verified"] is True
    assert "oracle" not in json.dumps(node).lower()


def test_action_candidates_map_evidence_edges_to_task_dag_projection():
    dag = action_candidates_to_task_dag([
        {
            "node_id": "action:1:0",
            "action_type": "place_block",
            "action": {"action": "place", "block": "bs"},
            "confidence": 0.4,
            "supported_by": ["claim:D1:1"],
            "conflicts_with": ["claim:D2:1"],
            "required_evidence": ["claim:D3:1"],
        }
    ])

    assert dag["nodes"][0]["status"] == "blocked"
    assert dag["edges"] == [
        {
            "source_id": "evidence:claim:D1:1",
            "target_id": "task:action:1:0",
            "edge_type": "supports_task",
            "craft_relation": "supported_by",
        },
        {
            "source_id": "evidence:claim:D2:1",
            "target_id": "task:action:1:0",
            "edge_type": "blocks_task",
            "craft_relation": "conflicts_with",
        },
        {
            "source_id": "task:action:1:0",
            "target_id": "evidence:claim:D3:1",
            "edge_type": "requires_evidence",
            "craft_relation": "required_evidence",
        },
    ]


def test_task_mapping_documents_tradeoff_boundaries():
    tradeoffs = task_mapping_tradeoffs()

    assert tradeoffs["mapping"]["action_candidate"] == "TaskDAGNodeProjection"
    assert any("read-only" in boundary for boundary in tradeoffs["boundaries"])
    assert any("Hidden CRAFT state" in boundary for boundary in tradeoffs["boundaries"])
