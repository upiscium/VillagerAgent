import csv
import json
from pathlib import Path


def normalize_results(*, config: dict, condition: str, raw_result: dict, output_dir: Path) -> None:
    normalized_dir = output_dir / "normalized"
    normalized_dir.mkdir(parents=True, exist_ok=True)

    turns = raw_result.get("turns", [])
    final_progress = raw_result.get("final_progress", 0.0)
    summary = {
        "benchmark": "CRAFT",
        "condition": condition,
        "run_name": config["run"]["name"],
        "seed": config["run"].get("seed"),
        "structures": config["run"].get("structures"),
        "turns": config["run"].get("turns"),
        "num_games": len(config["run"].get("structures") or []),
        "mean_final_progress": final_progress,
        "completion_rate": 1.0 if raw_result.get("completed") else 0.0,
        "models": {
            "director": config["models"]["director"]["model"],
            "builder": config["models"]["builder"]["model"],
        },
        "villageragent": {
            "enabled": config.get("villageragent", {}).get("enabled", False),
            "use_task_decomposer": config.get("villageragent", {}).get("use_task_decomposer", False),
            "use_agent_controller": config.get("villageragent", {}).get("use_agent_controller", False),
            "use_state_manager": config.get("villageragent", {}).get("use_state_manager", False),
        },
        "partial_information": {
            "target_structure_exposed": config.get("villageragent", {}).get("expose_target_structure", False),
            "oracle_moves_exposed": config.get("villageragent", {}).get("expose_oracle_moves", False),
            "private_views_shared_raw": config.get("villageragent", {}).get("expose_private_views_to_global_state", False),
        },
    }
    with (normalized_dir / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    with (normalized_dir / "turns.jsonl").open("w", encoding="utf-8") as f:
        for turn in turns:
            f.write(json.dumps(turn, ensure_ascii=False) + "\n")

    metrics_path = normalized_dir / "metrics.csv"
    fieldnames = [
        "run_name",
        "condition",
        "structure_id",
        "seed",
        "turns",
        "completed",
        "final_progress",
        "completion_rate",
        "director_model",
        "builder_model",
        "use_task_decomposer",
        "use_agent_controller",
        "use_state_manager",
        "leakage_passed",
    ]
    with metrics_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({
            "run_name": config["run"]["name"],
            "condition": condition,
            "structure_id": raw_result.get("structure_id"),
            "seed": config["run"].get("seed"),
            "turns": config["run"].get("turns"),
            "completed": raw_result.get("completed", False),
            "final_progress": final_progress,
            "completion_rate": 1.0 if raw_result.get("completed") else 0.0,
            "director_model": config["models"]["director"]["model"],
            "builder_model": config["models"]["builder"]["model"],
            "use_task_decomposer": config.get("villageragent", {}).get("use_task_decomposer", False),
            "use_agent_controller": config.get("villageragent", {}).get("use_agent_controller", False),
            "use_state_manager": config.get("villageragent", {}).get("use_state_manager", False),
            "leakage_passed": raw_result.get("leakage_passed", True),
        })

    leakage_report = raw_result.get("leakage_report", {"checks": []})
    with (normalized_dir / "leakage_report.json").open("w", encoding="utf-8") as f:
        json.dump(leakage_report, f, ensure_ascii=False, indent=2)
