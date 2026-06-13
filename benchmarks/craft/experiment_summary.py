import argparse
import csv
import json
from pathlib import Path

from benchmarks.craft.config import repo_root


SUMMARY_FIELDS = [
    "run_name",
    "status",
    "error_type",
    "error_message",
    "condition",
    "mean_final_progress",
    "completion_rate",
    "builder_fallback_rate",
    "gated_clarification_rate",
    "mean_action_confidence",
    "claim_support_count",
    "claim_conflict_count",
    "claim_required_evidence_count",
    "dual_dag_node_count",
    "dual_dag_edge_count",
    "supported_action_count",
    "conflicted_action_count",
    "required_evidence_action_count",
    "leakage_passed",
]


class ExperimentSummaryError(ValueError):
    """Raised when CRAFT experiment summary inputs are missing or invalid."""


def build_experiment_summary(
    run_names: list[str],
    *,
    result_root: Path,
    analysis_input: Path | None = None,
) -> list[dict]:
    if not run_names:
        raise ExperimentSummaryError("At least one CRAFT run is required.")
    analyses = _analysis_by_run(analysis_input) if analysis_input else {}
    return [
        _summarize_run(run_name, result_root=result_root, analysis=analyses.get(run_name))
        for run_name in run_names
    ]


def write_summary_csv(rows: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in SUMMARY_FIELDS})


def write_summary_json(rows: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump({"runs": rows}, f, ensure_ascii=False, indent=2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write a compact CRAFT experiment summary table.")
    parser.add_argument("--runs", nargs="+", required=True)
    parser.add_argument("--result-root", default="result/craft")
    parser.add_argument("--analysis-input", default=None)
    parser.add_argument("--output", default="result/craft/experiment_summary.csv")
    parser.add_argument("--json-output", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = repo_root()
    result_root = _resolve_path(root, args.result_root)
    analysis_input = _resolve_path(root, args.analysis_input) if args.analysis_input else None
    rows = build_experiment_summary(args.runs, result_root=result_root, analysis_input=analysis_input)
    output = _resolve_path(root, args.output)
    write_summary_csv(rows, output)
    if args.json_output:
        write_summary_json(rows, _resolve_path(root, args.json_output))
    print(f"Wrote CRAFT experiment summary: {output}")


def _summarize_run(run_name: str, *, result_root: Path, analysis: dict | None = None) -> dict:
    normalized_dir = result_root / run_name / "normalized"
    summary = _read_json(normalized_dir / "summary.json")
    runtime = summary.get("runtime", {})
    status = summary.get("status") or runtime.get("status") or "completed"
    failure = summary.get("failure") or runtime.get("failure", {}) or {}
    analysis = analysis or _read_optional_json(normalized_dir / "dual_dag_analysis.json")
    return {
        "run_name": summary.get("run_name", run_name),
        "status": status,
        "error_type": failure.get("type", ""),
        "error_message": failure.get("message", ""),
        "condition": summary.get("condition", ""),
        "mean_final_progress": summary.get("mean_final_progress", 0.0),
        "completion_rate": summary.get("completion_rate", 0.0),
        "builder_fallback_rate": runtime.get("builder_fallback_rate", 0.0),
        "gated_clarification_rate": runtime.get("gated_clarification_rate", 0.0),
        "mean_action_confidence": runtime.get("mean_action_confidence", 0.0),
        "claim_support_count": runtime.get("claim_support_count", 0),
        "claim_conflict_count": runtime.get("claim_conflict_count", 0),
        "claim_required_evidence_count": runtime.get("claim_required_evidence_count", 0),
        "dual_dag_node_count": runtime.get("dual_dag_node_count", 0),
        "dual_dag_edge_count": runtime.get("dual_dag_edge_count", 0),
        "supported_action_count": analysis.get("supported_action_count", 0),
        "conflicted_action_count": analysis.get("conflicted_action_count", 0),
        "required_evidence_action_count": analysis.get("required_evidence_action_count", 0),
        "leakage_passed": status == "completed" and _leakage_passed(normalized_dir),
    }


def _leakage_passed(normalized_dir: Path) -> bool:
    metrics_path = normalized_dir / "metrics.csv"
    if not metrics_path.exists():
        return True
    with metrics_path.open("r", encoding="utf-8", newline="") as f:
        return all(_as_bool(row.get("leakage_passed", True)) for row in csv.DictReader(f))


def _read_json(path: Path) -> dict:
    if not path.exists():
        raise ExperimentSummaryError(f"Missing CRAFT summary input: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _read_optional_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _analysis_by_run(path: Path) -> dict[str, dict]:
    analysis = _read_json(path)
    if "runs" in analysis:
        return {
            run.get("run_name", ""): run
            for run in analysis.get("runs", [])
            if run.get("run_name")
        }
    if analysis.get("run_name"):
        return {analysis["run_name"]: analysis}
    return {}


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "1", "yes"}
    return bool(value)


def _resolve_path(root: Path, path: str) -> Path:
    resolved = Path(path)
    if resolved.is_absolute():
        return resolved
    return root / resolved


if __name__ == "__main__":
    main()
