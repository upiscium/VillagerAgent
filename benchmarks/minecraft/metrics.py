from benchmarks.craft.dual_dag.schema import DUAL_DAG_SCHEMA_VERSION
from env.minecraft_dual_dag import sanitize_public_value


def build_minecraft_metrics(
    *,
    summary: dict,
    action_log: dict,
    task_graph_snapshot: dict,
    decision_support: dict,
) -> dict:
    tasks = task_graph_snapshot.get("tasks", []) if isinstance(task_graph_snapshot, dict) else []
    actions = _flatten_action_log(action_log)
    task_count = len(tasks)
    completed_tasks = [task for task in tasks if task.get("status") == "success"]
    failed_tasks = [task for task in tasks if task.get("status") == "failure"]
    failed_actions = [action for action in actions if _action_status(action) is False]
    retry_replan_actions = [action for action in actions if _is_retry_or_replan_action(action)]
    recommended_task_id = summary.get("recommended_task_id", "")
    selected_task_id = summary.get("selected_task_id", "")
    recommendation_adopted = bool(recommended_task_id and recommended_task_id == selected_task_id)

    metrics = {
        "schema_version": DUAL_DAG_SCHEMA_VERSION,
        "run_name": summary.get("run_name", ""),
        "mode": summary.get("mode", ""),
        "task_completion_rate": _safe_rate(len(completed_tasks), task_count),
        "task_count": task_count,
        "completed_task_count": len(completed_tasks),
        "failed_task_count": len(failed_tasks),
        "action_count": len(actions),
        "failed_action_count": len(failed_actions),
        "retry_replan_count": len(retry_replan_actions),
        "time_to_completion": _time_to_completion(actions),
        "recommendation_adopted_count": 1 if recommendation_adopted else 0,
        "recommendation_helped_count": 0,
        "recommendation_hurt_count": 0,
        "recommended_task_id": recommended_task_id,
        "selected_task_id": selected_task_id,
        "progress": summary.get("progress"),
        "error": summary.get("error"),
        "mutates_runtime": False,
    }
    return sanitize_public_value(metrics)


def _flatten_action_log(action_log: dict) -> list[dict]:
    if not isinstance(action_log, dict):
        return []
    actions = []
    for agent_name, entries in action_log.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                row = dict(entry)
                row["agent"] = agent_name
                actions.append(row)
    return actions


def _action_status(action: dict):
    result = action.get("result")
    if isinstance(result, dict):
        return result.get("status")
    return None


def _is_retry_or_replan_action(action: dict) -> bool:
    text = " ".join(str(action.get(key, "")) for key in ("action", "feedback", "final_answer"))
    result = action.get("result")
    if isinstance(result, dict):
        text = f"{text} {result.get('message', '')}"
    lowered = text.lower()
    return "retry" in lowered or "replan" in lowered


def _time_to_completion(actions: list[dict]):
    durations = [action.get("duration") for action in actions]
    numeric_durations = [duration for duration in durations if isinstance(duration, int | float)]
    if numeric_durations:
        return sum(numeric_durations)
    return None


def _safe_rate(numerator: int, denominator: int):
    if denominator == 0:
        return None
    return numerator / denominator
