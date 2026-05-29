import argparse
import csv
import json
from pathlib import Path

from benchmarks.craft.config import repo_root


REPORT_FIELDS = [
    "run_name",
    "condition",
    "num_games",
    "turns",
    "mean_final_progress",
    "completion_rate",
    "director_model",
    "builder_model",
    "use_task_decomposer",
    "use_agent_controller",
    "use_state_manager",
    "leakage_passed",
]


class ReportInputError(ValueError):
    """Raised when a requested CRAFT run cannot be summarized."""


def load_run_summary(run_name: str, *, result_root: Path) -> dict:
    run_dir = result_root / run_name
    summary_path = run_dir / "normalized" / "summary.json"
    metrics_path = run_dir / "normalized" / "metrics.csv"
    if not summary_path.exists():
        raise ReportInputError(f"Missing CRAFT summary: {summary_path}")
    if not metrics_path.exists():
        raise ReportInputError(f"Missing CRAFT metrics: {metrics_path}")

    with summary_path.open("r", encoding="utf-8") as f:
        summary = json.load(f)
    metrics_rows = _read_metrics(metrics_path)
    leakage_passed = all(_as_bool(row.get("leakage_passed", True)) for row in metrics_rows)
    return {
        "run_name": summary.get("run_name", run_name),
        "condition": summary.get("condition", ""),
        "num_games": summary.get("num_games", len(metrics_rows)),
        "turns": summary.get("turns", ""),
        "mean_final_progress": summary.get("mean_final_progress", 0.0),
        "completion_rate": summary.get("completion_rate", 0.0),
        "director_model": summary.get("models", {}).get("director", ""),
        "builder_model": summary.get("models", {}).get("builder", ""),
        "use_task_decomposer": summary.get("villageragent", {}).get("use_task_decomposer", False),
        "use_agent_controller": summary.get("villageragent", {}).get("use_agent_controller", False),
        "use_state_manager": summary.get("villageragent", {}).get("use_state_manager", False),
        "leakage_passed": leakage_passed,
    }


def build_comparison_report(runs: list[str], *, result_root: Path) -> list[dict]:
    if not runs:
        raise ReportInputError("At least one run name is required.")
    return [load_run_summary(run, result_root=result_root) for run in runs]


def write_csv_report(rows: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in REPORT_FIELDS})


def write_json_report(rows: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump({"runs": rows}, f, ensure_ascii=False, indent=2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize normalized CRAFT runs.")
    parser.add_argument("--runs", nargs="+", required=True)
    parser.add_argument("--result-root", default="result/craft")
    parser.add_argument("--output", default="result/craft/comparison_summary.csv")
    parser.add_argument("--json-output", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = repo_root()
    result_root = Path(args.result_root)
    if not result_root.is_absolute():
        result_root = root / result_root
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = root / output_path
    rows = build_comparison_report(args.runs, result_root=result_root)
    write_csv_report(rows, output_path)
    if args.json_output:
        json_output = Path(args.json_output)
        if not json_output.is_absolute():
            json_output = root / json_output
        write_json_report(rows, json_output)
    print(f"Wrote CRAFT comparison report: {output_path}")


def _read_metrics(metrics_path: Path) -> list[dict]:
    with metrics_path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "1", "yes"}
    return bool(value)


if __name__ == "__main__":
    main()
