from dataclasses import asdict, dataclass
import json
from pathlib import Path


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
