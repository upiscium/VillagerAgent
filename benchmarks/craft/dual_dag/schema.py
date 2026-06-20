DUAL_DAG_SCHEMA_VERSION = "1.0.0"

DUAL_DAG_NODE_TYPES = [
    "observed_fact",
    "public_fact",
    "reported_claim",
    "hypothesis",
    "resolved_fact",
    "suggested_constraint",
    "place_block",
    "remove_block",
    "clarify",
    "wait_for_evidence",
    "minecraft_task",
    "minecraft_action",
    "minecraft_observation",
    "minecraft_claim",
]

DUAL_DAG_EDGE_TYPES = [
    "supports",
    "conflicts_with",
    "derived_from",
    "requires_confirmation_from",
    "resolved_by",
    "requires_clarification",
    "waits_for_evidence",
    "coordinates_action",
    "clarification_response",
    "unlocks_action",
    "blocks_action",
    "executes_action",
    "precedes_task",
    "task_invokes_action",
    "produces_observation",
    "reports_claim",
]

DUAL_DAG_HYPOTHESIS_STATUSES = ["open", "supported", "conflicted", "resolved", "invalidated"]
DUAL_DAG_ACTION_STATES = ["candidate", "executable", "waiting_for_evidence", "blocked", "invalidated", "executed"]


def dual_dag_schema_registry() -> dict:
    return {
        "schema_version": DUAL_DAG_SCHEMA_VERSION,
        "node_types": DUAL_DAG_NODE_TYPES,
        "edge_types": DUAL_DAG_EDGE_TYPES,
        "lifecycle_fields": {
            "hypothesis_statuses": DUAL_DAG_HYPOTHESIS_STATUSES,
            "action_candidate_states": DUAL_DAG_ACTION_STATES,
            "coordination_action_types": ["clarify", "wait_for_evidence"],
        },
        "compatibility": {
            "semver": True,
            "patch": "Compatible additions or documentation-only clarifications.",
            "minor": "Backward-compatible node, edge, or metadata fields may be added.",
            "major": "Existing node, edge, or lifecycle field semantics may change.",
        },
    }
