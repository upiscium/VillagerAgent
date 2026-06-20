from dataclasses import asdict, dataclass
import json
from pathlib import Path

from benchmarks.craft.dual_dag.schema import DUAL_DAG_SCHEMA_VERSION, dual_dag_schema_registry


SENSITIVE_KEYS = {
    "api_key",
    "api_keys",
    "api_key_list",
    "base_url",
    "password",
    "secret",
    "token",
}
SENSITIVE_KEY_MARKERS = ("api_key", "password", "secret", "token")


@dataclass
class MinecraftDAGNode:
    node_id: str
    node_type: str
    content: dict
    provenance: dict
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MinecraftDAGEdge:
    source_id: str
    target_id: str
    edge_type: str
    metadata: dict

    def to_dict(self) -> dict:
        return asdict(self)


def build_minecraft_dual_dag_artifact(
    *,
    action_log: dict | None = None,
    tasks: list | None = None,
    graph=None,
) -> dict:
    """Build a read-only Dual-DAG-style artifact for Minecraft runs.

    This intentionally does not instantiate or mutate the CRAFT runtime. It only
    projects public Minecraft task/action/observation records into a common shape.
    """
    nodes: list[dict] = []
    edges: list[dict] = []

    graph_tasks = list(getattr(graph, "vertex", []) or [])
    all_tasks = _dedupe_tasks([*(tasks or []), *graph_tasks])
    for task in all_tasks:
        nodes.append(_task_node(task).to_dict())

    if graph is not None:
        for start_task, end_task in getattr(graph, "edge", []) or []:
            edges.append(MinecraftDAGEdge(
                source_id=_task_node_id(start_task),
                target_id=_task_node_id(end_task),
                edge_type="precedes_task",
                metadata={"source": "minecraft_task_graph"},
            ).to_dict())

    task_by_agent = _tasks_by_agent(all_tasks)
    for agent_name, entries in (action_log or {}).items():
        if not isinstance(entries, list):
            continue
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            action_node = _action_node(agent_name, index, entry)
            nodes.append(action_node.to_dict())
            for task in task_by_agent.get(agent_name, []):
                edges.append(MinecraftDAGEdge(
                    source_id=_task_node_id(task),
                    target_id=action_node.node_id,
                    edge_type="task_invokes_action",
                    metadata={"source": "agent_assignment"},
                ).to_dict())

            observation = _observation_node(agent_name, index, entry)
            if observation is not None:
                nodes.append(observation.to_dict())
                edges.append(MinecraftDAGEdge(
                    source_id=action_node.node_id,
                    target_id=observation.node_id,
                    edge_type="produces_observation",
                    metadata={"source": "minecraft_tool_result"},
                ).to_dict())

            claim = _claim_node(agent_name, index, entry)
            if claim is not None:
                nodes.append(claim.to_dict())
                edges.append(MinecraftDAGEdge(
                    source_id=action_node.node_id,
                    target_id=claim.node_id,
                    edge_type="reports_claim",
                    metadata={"source": "minecraft_agent_message"},
                ).to_dict())

    return {
        "schema_version": DUAL_DAG_SCHEMA_VERSION,
        "runtime": "minecraft_dual_dag_artifact",
        "summary": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "task_node_count": sum(1 for node in nodes if node["node_type"] == "minecraft_task"),
            "action_node_count": sum(1 for node in nodes if node["node_type"] == "minecraft_action"),
            "observation_node_count": sum(1 for node in nodes if node["node_type"] == "minecraft_observation"),
            "claim_node_count": sum(1 for node in nodes if node["node_type"] == "minecraft_claim"),
        },
        "nodes": nodes,
        "edges": edges,
        "mapping": minecraft_dual_dag_mapping(),
        "schema": dual_dag_schema_registry(),
    }


def build_minecraft_dual_dag_artifact_from_action_log(
    action_log_path: str | Path,
    *,
    tasks: list | None = None,
    graph=None,
) -> dict:
    with Path(action_log_path).open("r", encoding="utf-8") as f:
        action_log = json.load(f)
    return build_minecraft_dual_dag_artifact(
        action_log=action_log,
        tasks=tasks,
        graph=graph,
    )


def build_minecraft_runtime_decision_support(
    artifact: dict,
    *,
    candidate_tasks: list | None = None,
) -> dict:
    """Return dry-run decision support from a Minecraft Dual-DAG artifact.

    This is the lowest-risk runtime hook: callers can read the recommendation
    without mutating Task, Graph, action logs, or Minecraft environment state.
    """
    nodes = list(artifact.get("nodes", []) or [])
    edges = list(artifact.get("edges", []) or [])
    task_rows = _decision_candidate_tasks(candidate_tasks, nodes)
    scored = [
        _score_task_candidate(task, nodes=nodes, edges=edges)
        for task in task_rows
    ]
    recommended = max(scored, key=lambda row: row["score"]) if scored else {}
    return {
        "mode": "dry_run",
        "mutates_runtime": False,
        "recommended_task_id": recommended.get("task_id", ""),
        "recommended_description": recommended.get("description", ""),
        "candidates": scored,
        "context": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "hook": "read_only_minecraft_dual_dag_artifact",
        },
    }


def minecraft_dual_dag_mapping() -> dict:
    return {
        "observations": [
            "Minecraft tool responses and task feedback are public observation nodes.",
            "Environment query results are represented through the same observation shape when present in action logs.",
        ],
        "actions": [
            "Agent tool calls recorded in action_log.json are minecraft_action nodes.",
            "Task graph assignments may link task nodes to action nodes by assigned agent.",
        ],
        "claims": [
            "Agent final answers and talkTo messages are minecraft_claim nodes.",
            "Claims are separate from observations so later analysis can compare reported intent with tool feedback.",
        ],
        "boundaries": [
            "Minecraft artifacts are read-only projections and do not mutate type_define.graph.Graph.",
            "CRAFT DualDAGRuntime remains decoupled from Minecraft artifact capture.",
            "Private, internal, credential, and underscore-prefixed fields are dropped recursively.",
        ],
        "decision_support": [
            "Runtime decision support is a dry-run read of Minecraft Dual-DAG artifacts.",
            "Recommendations do not mutate Task, Graph, action logs, or environment state.",
        ],
    }


def _task_node(task) -> MinecraftDAGNode:
    content = {
        "description": getattr(task, "description", ""),
        "status": getattr(task, "status", "unknown"),
        "milestones": sanitize_public_value(getattr(task, "milestones", [])),
        "reflect": sanitize_public_value(getattr(task, "reflect", None)),
        "candidate_agents": sanitize_public_value(getattr(task, "candidate_list", [])),
        "assigned_agents": sanitize_public_value(getattr(task, "_agent", [])),
        "metadata": sanitize_public_value(getattr(task, "content", {})),
    }
    return MinecraftDAGNode(
        node_id=_task_node_id(task),
        node_type="minecraft_task",
        content=content,
        provenance={"source": "type_define.graph.Task"},
    )


def _action_node(agent_name: str, index: int, entry: dict) -> MinecraftDAGNode:
    kwargs = entry.get("kwargs", {}) if isinstance(entry.get("kwargs"), dict) else {}
    return MinecraftDAGNode(
        node_id=f"minecraft:action:{agent_name}:{index}",
        node_type="minecraft_action",
        content={
            "agent": agent_name,
            "tool": entry.get("action", "unknown"),
            "arguments": sanitize_public_value(kwargs),
            "duration": entry.get("duration"),
        },
        provenance={
            "source": "data/action_log.json",
            "agent": agent_name,
            "start_time": entry.get("start_time"),
            "end_time": entry.get("end_time"),
        },
    )


def _observation_node(agent_name: str, index: int, entry: dict) -> MinecraftDAGNode | None:
    result = entry.get("result")
    feedback = entry.get("feedback")
    if result is None and feedback is None:
        return None
    return MinecraftDAGNode(
        node_id=f"minecraft:observation:{agent_name}:{index}",
        node_type="minecraft_observation",
        content={
            "result": sanitize_public_value(result),
            "feedback": sanitize_public_value(feedback),
        },
        provenance={"source": "minecraft_tool_result", "agent": agent_name},
        confidence=1.0 if _entry_status(entry) is not False else 0.5,
    )


def _claim_node(agent_name: str, index: int, entry: dict) -> MinecraftDAGNode | None:
    content = _claim_content(entry)
    if not content:
        return None
    return MinecraftDAGNode(
        node_id=f"minecraft:claim:{agent_name}:{index}",
        node_type="minecraft_claim",
        content=sanitize_public_value(content),
        provenance={"source": "minecraft_agent_message", "agent": agent_name},
        confidence=0.8,
    )


def sanitize_public_value(value):
    if isinstance(value, dict):
        return {
            key: sanitize_public_value(child)
            for key, child in value.items()
            if _is_public_key(key)
        }
    if isinstance(value, list):
        return [sanitize_public_value(child) for child in value]
    if isinstance(value, tuple):
        return [sanitize_public_value(child) for child in value]
    return value


def _decision_candidate_tasks(candidate_tasks: list | None, nodes: list[dict]) -> list[dict]:
    if candidate_tasks is not None:
        rows = []
        for index, task in enumerate(candidate_tasks):
            if isinstance(task, str):
                rows.append({"task_id": f"candidate:{index}", "description": task})
            elif isinstance(task, dict):
                rows.append({
                    "task_id": str(task.get("task_id") or task.get("node_id") or f"candidate:{index}"),
                    "description": str(task.get("description", "")),
                })
        return rows
    return [
        {
            "task_id": node.get("node_id", ""),
            "description": str((node.get("content", {}) or {}).get("description", "")),
        }
        for node in nodes
        if node.get("node_type") == "minecraft_task"
    ]


def _score_task_candidate(task: dict, *, nodes: list[dict], edges: list[dict]) -> dict:
    task_id = task.get("task_id", "")
    description = task.get("description", "")
    words = _keyword_set(description)
    invoked_action_ids = [
        edge.get("target_id", "")
        for edge in edges
        if edge.get("source_id") == task_id and edge.get("edge_type") == "task_invokes_action"
    ]
    observations = _observations_for_actions(invoked_action_ids, nodes=nodes, edges=edges)
    failed_observations = [node for node in observations if _observation_status(node) is False]
    successful_observations = [node for node in observations if _observation_status(node) is True]
    related_claims = [
        node for node in nodes
        if node.get("node_type") == "minecraft_claim"
        and words
        and words & _keyword_set(str((node.get("content", {}) or {}).get("message", "")))
    ]
    score = 0.0
    score += len(successful_observations)
    score += 0.5 * len(related_claims)
    score -= 2.0 * len(failed_observations)
    if not invoked_action_ids:
        score += 0.1
    return {
        "task_id": task_id,
        "description": description,
        "score": score,
        "recommendation": "retry_or_replan" if failed_observations else "candidate",
        "supporting_claim_ids": [node.get("node_id", "") for node in related_claims],
        "successful_observation_ids": [node.get("node_id", "") for node in successful_observations],
        "failed_observation_ids": [node.get("node_id", "") for node in failed_observations],
    }


def _observations_for_actions(action_ids: list[str], *, nodes: list[dict], edges: list[dict]) -> list[dict]:
    observation_ids = {
        edge.get("target_id", "")
        for edge in edges
        if edge.get("source_id") in action_ids and edge.get("edge_type") == "produces_observation"
    }
    return [node for node in nodes if node.get("node_id") in observation_ids]


def _observation_status(node: dict):
    content = node.get("content", {}) if isinstance(node, dict) else {}
    result = content.get("result") if isinstance(content, dict) else None
    if isinstance(result, dict):
        return result.get("status")
    return None


def _keyword_set(text: str) -> set[str]:
    return {
        word.strip(".,:;!?()[]{}\"'").lower()
        for word in text.split()
        if len(word.strip(".,:;!?()[]{}\"'")) > 2
    }


def _claim_content(entry: dict) -> dict:
    if isinstance(entry.get("final_answer"), str) and entry["final_answer"].strip():
        return {"claim_type": "final_answer", "message": entry["final_answer"]}

    kwargs = entry.get("kwargs", {}) if isinstance(entry.get("kwargs"), dict) else {}
    if entry.get("action") == "talkTo" and isinstance(kwargs.get("message"), str):
        return {
            "claim_type": "agent_message",
            "recipient": kwargs.get("entity_name"),
            "message": kwargs.get("message"),
        }
    return {}


def _entry_status(entry: dict):
    result = entry.get("result")
    if isinstance(result, dict):
        return result.get("status")
    return None


def _task_node_id(task) -> str:
    task_id = getattr(task, "id", None) or getattr(task, "description", "unknown")
    return f"minecraft:task:{task_id}"


def _tasks_by_agent(tasks: list) -> dict[str, list]:
    by_agent: dict[str, list] = {}
    for task in tasks:
        for agent_name in getattr(task, "_agent", []) or []:
            by_agent.setdefault(agent_name, []).append(task)
    return by_agent


def _dedupe_tasks(tasks: list) -> list:
    seen = set()
    deduped = []
    for task in tasks:
        task_id = _task_node_id(task)
        if task_id in seen:
            continue
        seen.add(task_id)
        deduped.append(task)
    return deduped


def _is_public_key(key) -> bool:
    key_text = str(key)
    lower_key = key_text.lower()
    return (
        not key_text.startswith("_")
        and lower_key not in SENSITIVE_KEYS
        and not any(marker in lower_key for marker in SENSITIVE_KEY_MARKERS)
    )
