import argparse
import csv
import json
from pathlib import Path

import yaml

from benchmarks.craft.config import repo_root


REPORT_FIELDS = [
    "run_name",
    "condition",
    "seed",
    "structures",
    "num_games",
    "turns",
    "mean_final_progress",
    "completion_rate",
    "director_model",
    "builder_model",
    "director_provider",
    "builder_provider",
    "active_directors",
    "active_director_count",
    "builder_fallback_count",
    "builder_fallback_rate",
    "observed_fact_count",
    "reported_claim_count",
    "hypothesis_count",
    "mean_action_confidence",
    "claim_support_count",
    "claim_conflict_count",
    "candidate_count",
    "clarification_count",
    "gated_clarification_count",
    "gated_clarification_rate",
    "mean_risk_score",
    "low_confidence_gate_count",
    "conflict_gate_count",
    "baseline_type",
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
    resolved_config = _load_resolved_config(run_dir / "config.resolved.yaml")
    metrics_rows = _read_metrics(metrics_path)
    leakage_passed = all(_as_bool(row.get("leakage_passed", True)) for row in metrics_rows)
    condition = summary.get("condition", "")
    runtime = summary.get("runtime", {})
    active_directors = runtime.get("active_directors") or _active_directors_from_config(
        resolved_config,
        condition,
    )
    return {
        "run_name": summary.get("run_name", run_name),
        "condition": condition,
        "seed": summary.get("seed", ""),
        "structures": ",".join(str(item) for item in summary.get("structures", []) or []),
        "num_games": summary.get("num_games", len(metrics_rows)),
        "turns": summary.get("turns", ""),
        "mean_final_progress": summary.get("mean_final_progress", 0.0),
        "completion_rate": summary.get("completion_rate", 0.0),
        "director_model": summary.get("models", {}).get("director", ""),
        "builder_model": summary.get("models", {}).get("builder", ""),
        "director_provider": summary.get("providers", {}).get("director", "")
        or resolved_config.get("models", {}).get("director", {}).get("provider", ""),
        "builder_provider": summary.get("providers", {}).get("builder", "")
        or resolved_config.get("models", {}).get("builder", {}).get("provider", ""),
        "active_directors": ",".join(active_directors),
        "active_director_count": runtime.get("active_director_count", len(active_directors)),
        "builder_fallback_count": runtime.get(
            "builder_fallback_count",
            _builder_fallback_count(run_dir / "normalized" / "turns.jsonl"),
        ),
        "builder_fallback_rate": runtime.get(
            "builder_fallback_rate",
            _builder_fallback_rate(run_dir / "normalized" / "turns.jsonl"),
        ),
        "observed_fact_count": runtime.get(
            "observed_fact_count",
            _sum_metric_rows(metrics_rows, "observed_fact_count"),
        ),
        "reported_claim_count": runtime.get(
            "reported_claim_count",
            _sum_metric_rows(metrics_rows, "reported_claim_count"),
        ),
        "hypothesis_count": runtime.get(
            "hypothesis_count",
            _sum_metric_rows(metrics_rows, "hypothesis_count"),
        ),
        "mean_action_confidence": runtime.get(
            "mean_action_confidence",
            _mean_metric_rows(metrics_rows, "mean_action_confidence"),
        ),
        "claim_support_count": runtime.get(
            "claim_support_count",
            _sum_metric_rows(metrics_rows, "claim_support_count"),
        ),
        "claim_conflict_count": runtime.get(
            "claim_conflict_count",
            _sum_metric_rows(metrics_rows, "claim_conflict_count"),
        ),
        "candidate_count": runtime.get(
            "candidate_count",
            _sum_metric_rows(metrics_rows, "candidate_count"),
        ),
        "clarification_count": runtime.get(
            "clarification_count",
            _sum_metric_rows(metrics_rows, "clarification_count"),
        ),
        "gated_clarification_count": runtime.get(
            "gated_clarification_count",
            _sum_metric_rows(metrics_rows, "gated_clarification_count"),
        ),
        "gated_clarification_rate": runtime.get(
            "gated_clarification_rate",
            _mean_metric_rows(metrics_rows, "gated_clarification_rate"),
        ),
        "mean_risk_score": runtime.get(
            "mean_risk_score",
            _mean_metric_rows(metrics_rows, "mean_risk_score"),
        ),
        "low_confidence_gate_count": runtime.get(
            "low_confidence_gate_count",
            _sum_metric_rows(metrics_rows, "low_confidence_gate_count"),
        ),
        "conflict_gate_count": runtime.get(
            "conflict_gate_count",
            _sum_metric_rows(metrics_rows, "conflict_gate_count"),
        ),
        "baseline_type": runtime.get("baseline_type", _baseline_type(condition)),
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


def _sum_metric_rows(rows: list[dict], field: str) -> int:
    total = 0
    for row in rows:
        try:
            total += int(float(row.get(field, 0) or 0))
        except ValueError:
            continue
    return total


def _mean_metric_rows(rows: list[dict], field: str) -> float:
    values = []
    for row in rows:
        try:
            values.append(float(row.get(field, 0.0) or 0.0))
        except ValueError:
            continue
    return sum(values) / len(values) if values else 0.0


def _load_resolved_config(config_path: Path) -> dict:
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _active_directors_from_config(config: dict, condition: str) -> list[str]:
    if condition == "single_director_ablation":
        return ["D1"]
    villageragent = config.get("villageragent", {})
    if villageragent.get("enabled", False):
        return list(
            villageragent.get("active_director_ids")
            or villageragent.get("director_ids", ["D1", "D2", "D3"])
        )
    return []


def _baseline_type(condition: str) -> str:
    if condition == "official_baseline":
        return "comparable_artifact"
    return ""


def _builder_fallback_count(turns_path: Path) -> int:
    if not turns_path.exists():
        return 0
    count = 0
    with turns_path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            turn = json.loads(line)
            if (turn.get("builder_action") or {}).get("_builder_fallback"):
                count += 1
    return count


def _builder_fallback_rate(turns_path: Path) -> float:
    if not turns_path.exists():
        return 0.0
    total = 0
    fallback = 0
    with turns_path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            total += 1
            turn = json.loads(line)
            if (turn.get("builder_action") or {}).get("_builder_fallback"):
                fallback += 1
    return fallback / total if total else 0.0


if __name__ == "__main__":
    main()
