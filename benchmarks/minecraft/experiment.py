import argparse
import json
import time
from pathlib import Path

from benchmarks.minecraft.metrics import build_minecraft_metrics
from env.minecraft_dual_dag import (
    build_minecraft_dual_dag_artifact,
    build_minecraft_runtime_decision_support,
    rank_minecraft_runtime_tasks,
    sanitize_public_value,
)
from type_define.graph import Graph, Task


DEFAULT_OUTPUT_ROOT = Path("result/minecraft")


def run_minecraft_experiment(
    *,
    config_path: str | Path,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    run_name: str | None = None,
    config_index: int = 0,
    enable_dual_dag_task_selection: bool = False,
    execute: bool = False,
) -> dict:
    """Run or dry-run a Minecraft experiment and write normalized artifacts.

    Dry-run is the default so CI and local development can validate artifact capture
    without requiring a Minecraft server. ``execute=True`` calls the existing real
    runtime and then captures the same public artifact set from the run outputs.
    """
    launch_config = _load_config(config_path, config_index=config_index)
    selected_run_name = run_name or launch_config.get("task_name") or _default_run_name(config_path)
    output_dir = Path(output_root) / selected_run_name
    output_dir.mkdir(parents=True, exist_ok=True)

    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
    dual_dag_config = _dual_dag_config(enable_dual_dag_task_selection)
    tasks, graph = _task_graph_from_config(launch_config)
    action_log: dict = _fixture_action_log(launch_config)
    score: dict = {}
    error = None

    if execute:
        try:
            _execute_real_runtime(launch_config, dual_dag_config=dual_dag_config)
        except Exception as exc:  # Preserve partial artifacts for failed smoke runs.
            error = str(exc)
        action_log = _read_json(Path("data/action_log.json"), default={})
        score = _read_json(Path("data/score.json"), default={})

    artifact = build_minecraft_dual_dag_artifact(
        action_log=action_log,
        tasks=tasks,
        graph=graph,
    )
    decision_support = build_minecraft_runtime_decision_support(
        artifact,
        candidate_tasks=tasks,
    )
    ranked = rank_minecraft_runtime_tasks(
        tasks,
        graph=graph,
        action_log=action_log,
        config=dual_dag_config,
    )
    ranked_tasks = ranked.get("tasks", tasks)
    task_graph_snapshot = _task_graph_snapshot(graph)
    summary = {
        "run_name": selected_run_name,
        "mode": "execute" if execute else "dry_run",
        "started_at": started_at,
        "output_dir": str(output_dir),
        "task_name": launch_config.get("task_name", ""),
        "task_type": launch_config.get("task_type", ""),
        "task_idx": launch_config.get("task_idx"),
        "dual_dag_task_selection_enabled": enable_dual_dag_task_selection,
        "execute_real_environment": bool(execute),
        "mutates_runtime": False,
        "artifact_summary": artifact.get("summary", {}),
        "recommended_task_id": decision_support.get("recommended_task_id", ""),
        "recommended_description": decision_support.get("recommended_description", ""),
        "task_order": _task_order(tasks),
        "ranked_task_order": _task_order(ranked_tasks),
        "selected_task_id": _task_id(ranked_tasks[0]) if ranked_tasks else "",
        "selected_description": ranked_tasks[0].description if ranked_tasks else "",
        "final_score": sanitize_public_value(score),
        "progress": _progress_from_score(score),
        "error": error,
    }
    metrics = build_minecraft_metrics(
        summary=summary,
        action_log=action_log,
        task_graph_snapshot=task_graph_snapshot,
        decision_support=decision_support,
    )

    _write_json(output_dir / "launch_config.json", sanitize_public_value(launch_config))
    _write_json(output_dir / "action_log.json", sanitize_public_value(action_log))
    _write_json(output_dir / "task_graph_snapshot.json", task_graph_snapshot)
    _write_json(output_dir / "dual_dag_artifact.json", artifact)
    _write_json(output_dir / "decision_support.json", decision_support)
    _write_json(output_dir / "metrics.json", metrics)
    _write_json(output_dir / "summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Minecraft real-environment experiment harness")
    parser.add_argument("--config", required=True, help="Launch config JSON file")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--run-name", default=None)
    parser.add_argument("--config-index", type=int, default=0)
    parser.add_argument("--dual-dag-task-selection", action="store_true")
    parser.add_argument("--execute", action="store_true", help="Run the real Minecraft environment")
    args = parser.parse_args(argv)

    summary = run_minecraft_experiment(
        config_path=args.config,
        output_root=args.output_root,
        run_name=args.run_name,
        config_index=args.config_index,
        enable_dual_dag_task_selection=args.dual_dag_task_selection,
        execute=args.execute,
    )
    print(json.dumps(summary, indent=2))
    return 0 if summary.get("error") is None else 1


def _load_config(config_path: str | Path, *, config_index: int) -> dict:
    config = _read_json(Path(config_path), default=None)
    if isinstance(config, list):
        return dict(config[config_index])
    if isinstance(config, dict):
        return dict(config)
    raise ValueError(f"Unsupported Minecraft config shape: {config_path}")


def _execute_real_runtime(launch_config: dict, *, dual_dag_config: dict) -> None:
    from start_with_config import run
    from model.ollama_config import make_ollama_llm_config

    llm_config = make_ollama_llm_config()
    config = dict(launch_config)
    document = config.get("evaluation_arg", {}) if config.get("task_type") == "meta" else {}
    run(
        llm_config["api_model"],
        llm_config["api_base"],
        config["task_type"],
        config["task_idx"],
        config["agent_num"],
        config.get("dig_needed", False),
        config.get("max_task_num", 0),
        config["task_goal"],
        config.get("document_file", ""),
        config["host"],
        config["port"],
        config["task_name"],
        config.get("role", "same"),
        [llm_config.get("api_key_list", [])],
        document,
        minecraft_dual_dag_config=dual_dag_config,
    )


def _task_graph_from_config(config: dict) -> tuple[list[Task], Graph]:
    task_configs = config.get("smoke_tasks")
    if isinstance(task_configs, list) and task_configs:
        tasks = [_task_from_config(config, task_config) for task_config in task_configs]
    else:
        tasks = [_task_from_config(config, {
            "description": config.get("task_goal", config.get("task_name", "Minecraft task")),
        })]
    graph = Graph()
    for task in tasks:
        graph.add_node(task)
    return tasks, graph


def _task_from_config(config: dict, task_config: dict) -> Task:
    task = Task(task_config.get("description", "Minecraft task"), {
        "task_name": config.get("task_name", ""),
        "task_type": config.get("task_type", ""),
        "task_idx": config.get("task_idx"),
        "smoke_task_id": task_config.get("id", ""),
    })
    if task_config.get("id"):
        task.id = str(task_config["id"])
    agent_num = int(config.get("agent_num", 0) or 0)
    task.candidate_list = task_config.get("candidate_agents") or _agent_names(agent_num)
    task._agent = task_config.get("assigned_agents", [])
    task.number = int(task_config.get("number", max(1, min(agent_num, 1))))
    return task


def _task_graph_snapshot(graph: Graph) -> dict:
    return {
        "mutates_runtime": False,
        "tasks": [sanitize_public_value(task.to_json()) for task in graph.vertex],
        "edges": [
            {"source": start.description, "target": end.description}
            for start, end in graph.edge
        ],
    }


def _dual_dag_config(enabled: bool) -> dict:
    return {"runtime_task_selection": {"enabled": enabled}}


def _fixture_action_log(config: dict) -> dict:
    action_log = config.get("smoke_action_log", {})
    return action_log if isinstance(action_log, dict) else {}


def _task_order(tasks: list[Task]) -> list[dict]:
    return [
        {"task_id": _task_id(task), "description": task.description}
        for task in tasks
    ]


def _task_id(task: Task) -> str:
    return f"minecraft:task:{task.id}"


def _agent_names(agent_num: int) -> list[str]:
    names = ["Alice", "Bob", "Cindy", "David", "Eve", "Frank"]
    return names[:agent_num]


def _progress_from_score(score: dict):
    if not isinstance(score, dict):
        return None
    for key in ("progress", "score", "completion", "success_rate"):
        if key in score:
            return score[key]
    return None


def _default_run_name(config_path: str | Path) -> str:
    return f"minecraft_experiment_{Path(config_path).stem}"


def _read_json(path: Path, *, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, payload) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
