import json

from env.minecraft_dual_dag import (
    build_minecraft_dual_dag_artifact,
    build_minecraft_dual_dag_artifact_from_action_log,
    build_minecraft_runtime_decision_support,
    minecraft_dual_dag_mapping,
    sanitize_public_value,
)
from type_define.graph import Graph, Task


def test_minecraft_dual_dag_maps_tasks_actions_and_observations():
    task = Task("Collect wood", {"biome": "forest"})
    task._agent = ["Alice"]
    task.status = Task.running
    action_log = {
        "Alice": [{
            "action": "MineBlock",
            "start_time": "2026-06-15 10:00:00",
            "end_time": "2026-06-15 10:00:02",
            "duration": 2.0,
            "kwargs": {"player_name": "Alice", "block_name": "oak_log", "_internal": "drop"},
            "result": {"status": True, "message": "mined oak_log"},
        }]
    }

    artifact = build_minecraft_dual_dag_artifact(action_log=action_log, tasks=[task])

    assert artifact["summary"] == {
        "node_count": 3,
        "edge_count": 2,
        "task_node_count": 1,
        "action_node_count": 1,
        "observation_node_count": 1,
        "claim_node_count": 0,
    }
    assert {node["node_type"] for node in artifact["nodes"]} == {
        "minecraft_task",
        "minecraft_action",
        "minecraft_observation",
    }
    assert artifact["edges"] == [
        {
            "source_id": f"minecraft:task:{task.id}",
            "target_id": "minecraft:action:Alice:0",
            "edge_type": "task_invokes_action",
            "metadata": {"source": "agent_assignment"},
        },
        {
            "source_id": "minecraft:action:Alice:0",
            "target_id": "minecraft:observation:Alice:0",
            "edge_type": "produces_observation",
            "metadata": {"source": "minecraft_tool_result"},
        },
    ]
    assert "_internal" not in json.dumps(artifact)


def test_minecraft_dual_dag_maps_graph_dependencies_and_claims():
    graph = Graph()
    first = Task("Find teammate", {})
    second = Task("Share location", {})
    second._agent = ["Bob"]
    graph.add_node(first)
    graph.add_node(second)
    graph.add_edge(first, second)

    artifact = build_minecraft_dual_dag_artifact(
        graph=graph,
        action_log={
            "Bob": [{
                "action": "talkTo",
                "kwargs": {
                    "player_name": "Bob",
                    "entity_name": "Alice",
                    "message": "The chest is at x=1 y=2 z=3",
                },
                "result": {"status": True},
            }]
        },
    )

    edge_types = [edge["edge_type"] for edge in artifact["edges"]]
    assert "precedes_task" in edge_types
    assert "reports_claim" in edge_types
    claim = next(node for node in artifact["nodes"] if node["node_type"] == "minecraft_claim")
    assert claim["content"] == {
        "claim_type": "agent_message",
        "recipient": "Alice",
        "message": "The chest is at x=1 y=2 z=3",
    }


def test_minecraft_dual_dag_sanitizes_private_and_credential_fields_recursively():
    public = sanitize_public_value({
        "tool": "navigateTo",
        "api_key": "secret",
        "nested": {"_private": "hidden", "x": 1},
        "items": [{"session_token": "hidden", "name": "map"}],
    })

    assert public == {
        "tool": "navigateTo",
        "nested": {"x": 1},
        "items": [{"name": "map"}],
    }


def test_minecraft_dual_dag_can_build_from_action_log_file(tmp_path):
    action_log_path = tmp_path / "action_log.json"
    action_log_path.write_text(
        json.dumps({
            "Alice": [{
                "action": "read",
                "kwargs": {"player_name": "Alice", "item_name": "sign"},
                "result": {"status": True, "message": "Go north"},
            }]
        }),
        encoding="utf-8",
    )

    artifact = build_minecraft_dual_dag_artifact_from_action_log(action_log_path)

    assert artifact["summary"]["action_node_count"] == 1
    assert artifact["summary"]["observation_node_count"] == 1


def test_minecraft_dual_dag_mapping_documents_boundaries():
    mapping = minecraft_dual_dag_mapping()

    assert any("tool responses" in item for item in mapping["observations"])
    assert any("action_log.json" in item for item in mapping["actions"])
    assert any("final answers" in item for item in mapping["claims"])
    assert any("CRAFT DualDAGRuntime remains decoupled" in item for item in mapping["boundaries"])
    assert any("dry-run" in item for item in mapping["decision_support"])


def test_minecraft_runtime_decision_support_uses_artifact_context_without_mutation():
    failed_task = Task("Open locked door", {})
    failed_task._agent = ["Alice"]
    claim_supported_task = Task("Find chest", {})
    graph = Graph()
    graph.add_node(failed_task)
    graph.add_node(claim_supported_task)
    action_log = {
        "Alice": [{
            "action": "openContainer",
            "kwargs": {"player_name": "Alice", "item_name": "door"},
            "result": {"status": False, "message": "door is locked"},
        }],
        "Bob": [{
            "action": "talkTo",
            "kwargs": {
                "player_name": "Bob",
                "entity_name": "Alice",
                "message": "The chest is north of the door.",
            },
            "result": {"status": True},
        }],
    }
    artifact = build_minecraft_dual_dag_artifact(action_log=action_log, graph=graph)
    before_artifact = json.dumps(artifact, sort_keys=True)
    before_statuses = [failed_task.status, claim_supported_task.status]

    support = build_minecraft_runtime_decision_support(
        artifact,
        candidate_tasks=[
            {"node_id": f"minecraft:task:{failed_task.id}", "description": failed_task.description},
            {"node_id": f"minecraft:task:{claim_supported_task.id}", "description": claim_supported_task.description},
        ],
    )

    assert support["mode"] == "dry_run"
    assert support["mutates_runtime"] is False
    assert support["recommended_task_id"] == f"minecraft:task:{claim_supported_task.id}"
    assert support["candidates"][0]["recommendation"] == "retry_or_replan"
    assert support["candidates"][0]["failed_observation_ids"] == ["minecraft:observation:Alice:0"]
    assert support["candidates"][1]["supporting_claim_ids"] == ["minecraft:claim:Bob:0"]
    assert json.dumps(artifact, sort_keys=True) == before_artifact
    assert [failed_task.status, claim_supported_task.status] == before_statuses
