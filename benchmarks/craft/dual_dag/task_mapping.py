from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class TaskDAGNodeProjection:
    node_id: str
    task_type: str
    description: str
    status: str
    confidence: float
    content: dict
    craft_source_id: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TaskDAGEdgeProjection:
    source_id: str
    target_id: str
    edge_type: str
    craft_relation: str

    def to_dict(self) -> dict:
        return asdict(self)


def action_candidate_to_task_node(candidate: dict) -> TaskDAGNodeProjection:
    action = candidate.get("action", {}) if isinstance(candidate.get("action"), dict) else {}
    node_id = candidate.get("node_id", "")
    return TaskDAGNodeProjection(
        node_id=f"task:{node_id}",
        task_type=_task_type(candidate),
        description=_task_description(action, candidate.get("action_type", "")),
        status=_task_status(candidate),
        confidence=float(candidate.get("confidence", 0.0) or 0.0),
        content={
            "action": _public_action(action),
            "craft": {
                "source_node_id": node_id,
                "action_type": candidate.get("action_type", ""),
                "physically_verified": bool(
                    (candidate.get("metadata", {}) or {}).get("physically_verified", False)
                ),
            },
        },
        craft_source_id=node_id,
    )


def action_candidates_to_task_dag(candidates: list[dict]) -> dict:
    nodes = [action_candidate_to_task_node(candidate).to_dict() for candidate in candidates]
    edges = []
    for candidate in candidates:
        task_id = f"task:{candidate.get('node_id', '')}"
        for claim_id in candidate.get("supported_by", []) or []:
            edges.append(TaskDAGEdgeProjection(
                source_id=f"evidence:{claim_id}",
                target_id=task_id,
                edge_type="supports_task",
                craft_relation="supported_by",
            ).to_dict())
        for claim_id in candidate.get("conflicts_with", []) or []:
            edges.append(TaskDAGEdgeProjection(
                source_id=f"evidence:{claim_id}",
                target_id=task_id,
                edge_type="blocks_task",
                craft_relation="conflicts_with",
            ).to_dict())
        for claim_id in candidate.get("required_evidence", []) or []:
            edges.append(TaskDAGEdgeProjection(
                source_id=task_id,
                target_id=f"evidence:{claim_id}",
                edge_type="requires_evidence",
                craft_relation="required_evidence",
            ).to_dict())
    return {"nodes": nodes, "edges": edges}


def task_mapping_tradeoffs() -> dict:
    return {
        "mapping": {
            "action_candidate": "TaskDAGNodeProjection",
            "supported_by": "evidence -> task supports_task edge",
            "conflicts_with": "evidence -> task blocks_task edge",
            "required_evidence": "task -> evidence requires_evidence edge",
        },
        "boundaries": [
            "CRAFT action candidates remain CRAFT-owned runtime objects.",
            "Task DAG projections are read-only interoperability artifacts, not a replacement for CRAFT scoring.",
            "Hidden CRAFT state and private views are not projected into Task DAG content.",
        ],
    }


def _task_type(candidate: dict) -> str:
    action_type = candidate.get("action_type", "")
    if action_type in {"place_block", "remove_block"}:
        return "craft_action"
    if action_type == "clarify":
        return "craft_clarification"
    return "craft_evidence_wait"


def _task_status(candidate: dict) -> str:
    if candidate.get("conflicts_with"):
        return "blocked"
    if candidate.get("required_evidence"):
        return "waiting_for_evidence"
    if candidate.get("state") == "chosen":
        return "running"
    return "candidate"


def _task_description(action: dict, action_type: str) -> str:
    if action_type == "place_block":
        return "Place {block} at {position} layer {layer}".format(
            block=action.get("block", "block"),
            position=action.get("position", "unknown"),
            layer=action.get("layer", "unknown"),
        )
    if action_type == "remove_block":
        return "Remove block at {position} layer {layer}".format(
            position=action.get("position", "unknown"),
            layer=action.get("layer", "unknown"),
        )
    if action_type == "clarify":
        return "Clarify missing public evidence before acting"
    return "Wait for additional public evidence"


def _public_action(action: dict) -> dict:
    return {key: value for key, value in action.items() if not str(key).startswith("_")}
