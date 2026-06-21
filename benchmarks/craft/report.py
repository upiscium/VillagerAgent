import argparse
import csv
import json
from pathlib import Path

import yaml

from benchmarks.craft.config import repo_root
from benchmarks.craft.result_converter import _clarification_metrics, _progress_action_metrics, _retrieval_metrics


REPORT_FIELDS = [
    "run_name",
    "status",
    "error_type",
    "error_message",
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
    "max_progress",
    "progress_auc",
    "physical_action_count",
    "place_action_count",
    "remove_action_count",
    "clarify_count",
    "wait_count",
    "fallback_count",
    "no_op_count",
    "invalid_action_count",
    "positive_progress_turn_count",
    "zero_progress_turn_count",
    "negative_progress_turn_count",
    "mean_progress_delta_per_turn",
    "mean_progress_delta_per_physical_action",
    "observed_fact_count",
    "reported_claim_count",
    "hypothesis_count",
    "resolved_fact_count",
    "hypothesis_open_count",
    "hypothesis_supported_count",
    "hypothesis_conflicted_count",
    "hypothesis_resolved_count",
    "hypothesis_invalidated_count",
    "action_candidate_candidate_count",
    "action_candidate_executable_count",
    "action_candidate_waiting_for_evidence_count",
    "action_candidate_blocked_count",
    "action_candidate_invalidated_count",
    "action_candidate_executed_count",
    "coordination_action_count",
    "clarify_coordination_action_count",
    "wait_for_evidence_coordination_action_count",
    "mean_action_confidence",
    "claim_support_count",
    "claim_conflict_count",
    "claim_required_evidence_count",
    "candidate_count",
    "clarification_count",
    "unique_clarification_count",
    "repeated_clarification_count",
    "clarification_response_count",
    "clarification_to_unlock_count",
    "clarification_to_unlock_rate",
    "clarification_to_positive_action_count",
    "clarification_to_positive_action_latency",
    "clarification_without_state_change_count",
    "gate_invocation_count",
    "gate_allow_count",
    "gate_block_count",
    "gate_clarify_count",
    "gate_wait_count",
    "gate_reason_counts",
    "retrieved_node_count",
    "retrieved_claim_count",
    "retrieved_action_count",
    "mean_retrieved_node_age",
    "max_retrieved_node_age",
    "retrieved_executed_candidate_count",
    "retrieved_invalidated_candidate_count",
    "retrieved_superseded_node_count",
    "retrieval_used_in_top_action_count",
    "retrieval_changed_top_action_count",
    "gated_clarification_count",
    "gated_clarification_rate",
    "clarification_resolution_count",
    "clarification_resolution_rate",
    "mean_clarification_quality_score",
    "mean_post_clarification_progress_delta",
    "mean_risk_score",
    "low_confidence_gate_count",
    "conflict_gate_count",
    "required_evidence_gate_count",
    "span_uncertainty_gate_count",
    "dual_dag_node_count",
    "dual_dag_edge_count",
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
    status = summary.get("status") or summary.get("runtime", {}).get("status") or "completed"
    failure = summary.get("failure") or summary.get("runtime", {}).get("failure", {}) or {}
    leakage_passed = status == "completed" and all(
        _as_bool(row.get("leakage_passed", True)) for row in metrics_rows
    )
    condition = summary.get("condition", "")
    runtime = summary.get("runtime", {})
    turns_path = run_dir / "normalized" / "turns.jsonl"
    turns = _read_turns(turns_path)
    progress_action_metrics = _progress_action_metrics({
        "turns": turns,
        "final_progress": summary.get("mean_final_progress", 0.0),
    })
    clarification_metrics = _clarification_metrics(turns)
    retrieval_metrics = _retrieval_metrics(turns)
    active_directors = runtime.get("active_directors") or _active_directors_from_config(
        resolved_config,
        condition,
    )
    return {
        "run_name": summary.get("run_name", run_name),
        "status": status,
        "error_type": failure.get("type", ""),
        "error_message": failure.get("message", ""),
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
            _builder_fallback_count(turns_path),
        ),
        "builder_fallback_rate": runtime.get(
            "builder_fallback_rate",
            _builder_fallback_rate(turns_path),
        ),
        "max_progress": runtime.get(
            "max_progress",
            _mean_metric_rows_or_default(metrics_rows, "max_progress", progress_action_metrics["max_progress"]),
        ),
        "progress_auc": runtime.get(
            "progress_auc",
            _mean_metric_rows_or_default(metrics_rows, "progress_auc", progress_action_metrics["progress_auc"]),
        ),
        "physical_action_count": runtime.get(
            "physical_action_count",
            _sum_metric_rows_or_default(metrics_rows, "physical_action_count", progress_action_metrics["physical_action_count"]),
        ),
        "place_action_count": runtime.get(
            "place_action_count",
            _sum_metric_rows_or_default(metrics_rows, "place_action_count", progress_action_metrics["place_action_count"]),
        ),
        "remove_action_count": runtime.get(
            "remove_action_count",
            _sum_metric_rows_or_default(metrics_rows, "remove_action_count", progress_action_metrics["remove_action_count"]),
        ),
        "clarify_count": runtime.get(
            "clarify_count",
            _sum_metric_rows_or_default(metrics_rows, "clarify_count", progress_action_metrics["clarify_count"]),
        ),
        "wait_count": runtime.get(
            "wait_count",
            _sum_metric_rows_or_default(metrics_rows, "wait_count", progress_action_metrics["wait_count"]),
        ),
        "fallback_count": runtime.get(
            "fallback_count",
            _sum_metric_rows_or_default(metrics_rows, "fallback_count", progress_action_metrics["fallback_count"]),
        ),
        "no_op_count": runtime.get(
            "no_op_count",
            _sum_metric_rows_or_default(metrics_rows, "no_op_count", progress_action_metrics["no_op_count"]),
        ),
        "invalid_action_count": runtime.get(
            "invalid_action_count",
            _sum_metric_rows_or_default(metrics_rows, "invalid_action_count", progress_action_metrics["invalid_action_count"]),
        ),
        "positive_progress_turn_count": runtime.get(
            "positive_progress_turn_count",
            _sum_metric_rows_or_default(metrics_rows, "positive_progress_turn_count", progress_action_metrics["positive_progress_turn_count"]),
        ),
        "zero_progress_turn_count": runtime.get(
            "zero_progress_turn_count",
            _sum_metric_rows_or_default(metrics_rows, "zero_progress_turn_count", progress_action_metrics["zero_progress_turn_count"]),
        ),
        "negative_progress_turn_count": runtime.get(
            "negative_progress_turn_count",
            _sum_metric_rows_or_default(metrics_rows, "negative_progress_turn_count", progress_action_metrics["negative_progress_turn_count"]),
        ),
        "mean_progress_delta_per_turn": runtime.get(
            "mean_progress_delta_per_turn",
            _mean_metric_rows_or_default(metrics_rows, "mean_progress_delta_per_turn", progress_action_metrics["mean_progress_delta_per_turn"]),
        ),
        "mean_progress_delta_per_physical_action": runtime.get(
            "mean_progress_delta_per_physical_action",
            _mean_metric_rows_or_default(metrics_rows, "mean_progress_delta_per_physical_action", progress_action_metrics["mean_progress_delta_per_physical_action"]),
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
        "resolved_fact_count": runtime.get(
            "resolved_fact_count",
            _sum_metric_rows(metrics_rows, "resolved_fact_count"),
        ),
        "hypothesis_open_count": runtime.get(
            "hypothesis_open_count",
            _sum_metric_rows(metrics_rows, "hypothesis_open_count"),
        ),
        "hypothesis_supported_count": runtime.get(
            "hypothesis_supported_count",
            _sum_metric_rows(metrics_rows, "hypothesis_supported_count"),
        ),
        "hypothesis_conflicted_count": runtime.get(
            "hypothesis_conflicted_count",
            _sum_metric_rows(metrics_rows, "hypothesis_conflicted_count"),
        ),
        "hypothesis_resolved_count": runtime.get(
            "hypothesis_resolved_count",
            _sum_metric_rows(metrics_rows, "hypothesis_resolved_count"),
        ),
        "hypothesis_invalidated_count": runtime.get(
            "hypothesis_invalidated_count",
            _sum_metric_rows(metrics_rows, "hypothesis_invalidated_count"),
        ),
        "action_candidate_candidate_count": runtime.get(
            "action_candidate_candidate_count",
            _sum_metric_rows(metrics_rows, "action_candidate_candidate_count"),
        ),
        "action_candidate_executable_count": runtime.get(
            "action_candidate_executable_count",
            _sum_metric_rows(metrics_rows, "action_candidate_executable_count"),
        ),
        "action_candidate_waiting_for_evidence_count": runtime.get(
            "action_candidate_waiting_for_evidence_count",
            _sum_metric_rows(metrics_rows, "action_candidate_waiting_for_evidence_count"),
        ),
        "action_candidate_blocked_count": runtime.get(
            "action_candidate_blocked_count",
            _sum_metric_rows(metrics_rows, "action_candidate_blocked_count"),
        ),
        "action_candidate_invalidated_count": runtime.get(
            "action_candidate_invalidated_count",
            _sum_metric_rows(metrics_rows, "action_candidate_invalidated_count"),
        ),
        "action_candidate_executed_count": runtime.get(
            "action_candidate_executed_count",
            _sum_metric_rows(metrics_rows, "action_candidate_executed_count"),
        ),
        "coordination_action_count": runtime.get(
            "coordination_action_count",
            _sum_metric_rows(metrics_rows, "coordination_action_count"),
        ),
        "clarify_coordination_action_count": runtime.get(
            "clarify_coordination_action_count",
            _sum_metric_rows(metrics_rows, "clarify_coordination_action_count"),
        ),
        "wait_for_evidence_coordination_action_count": runtime.get(
            "wait_for_evidence_coordination_action_count",
            _sum_metric_rows(metrics_rows, "wait_for_evidence_coordination_action_count"),
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
        "claim_required_evidence_count": runtime.get(
            "claim_required_evidence_count",
            _sum_metric_rows(metrics_rows, "claim_required_evidence_count"),
        ),
        "candidate_count": runtime.get(
            "candidate_count",
            _sum_metric_rows(metrics_rows, "candidate_count"),
        ),
        "clarification_count": runtime.get(
            "clarification_count",
            _sum_metric_rows_or_default(metrics_rows, "clarification_count", clarification_metrics["clarification_count"]),
        ),
        "unique_clarification_count": runtime.get(
            "unique_clarification_count",
            _sum_metric_rows_or_default(metrics_rows, "unique_clarification_count", clarification_metrics["unique_clarification_count"]),
        ),
        "repeated_clarification_count": runtime.get(
            "repeated_clarification_count",
            _sum_metric_rows_or_default(metrics_rows, "repeated_clarification_count", clarification_metrics["repeated_clarification_count"]),
        ),
        "clarification_response_count": runtime.get(
            "clarification_response_count",
            _sum_metric_rows_or_default(metrics_rows, "clarification_response_count", clarification_metrics["clarification_response_count"]),
        ),
        "clarification_to_unlock_count": runtime.get(
            "clarification_to_unlock_count",
            _sum_metric_rows_or_default(metrics_rows, "clarification_to_unlock_count", clarification_metrics["clarification_to_unlock_count"]),
        ),
        "clarification_to_unlock_rate": runtime.get(
            "clarification_to_unlock_rate",
            _mean_metric_rows_or_default(metrics_rows, "clarification_to_unlock_rate", clarification_metrics["clarification_to_unlock_rate"]),
        ),
        "clarification_to_positive_action_count": runtime.get(
            "clarification_to_positive_action_count",
            _sum_metric_rows_or_default(metrics_rows, "clarification_to_positive_action_count", clarification_metrics["clarification_to_positive_action_count"]),
        ),
        "clarification_to_positive_action_latency": runtime.get(
            "clarification_to_positive_action_latency",
            _mean_metric_rows_or_default(metrics_rows, "clarification_to_positive_action_latency", clarification_metrics["clarification_to_positive_action_latency"]),
        ),
        "clarification_without_state_change_count": runtime.get(
            "clarification_without_state_change_count",
            _sum_metric_rows_or_default(metrics_rows, "clarification_without_state_change_count", clarification_metrics["clarification_without_state_change_count"]),
        ),
        "gate_invocation_count": runtime.get(
            "gate_invocation_count",
            _sum_metric_rows_or_default(metrics_rows, "gate_invocation_count", clarification_metrics["gate_invocation_count"]),
        ),
        "gate_allow_count": runtime.get(
            "gate_allow_count",
            _sum_metric_rows_or_default(metrics_rows, "gate_allow_count", clarification_metrics["gate_allow_count"]),
        ),
        "gate_block_count": runtime.get(
            "gate_block_count",
            _sum_metric_rows_or_default(metrics_rows, "gate_block_count", clarification_metrics["gate_block_count"]),
        ),
        "gate_clarify_count": runtime.get(
            "gate_clarify_count",
            _sum_metric_rows_or_default(metrics_rows, "gate_clarify_count", clarification_metrics["gate_clarify_count"]),
        ),
        "gate_wait_count": runtime.get(
            "gate_wait_count",
            _sum_metric_rows_or_default(metrics_rows, "gate_wait_count", clarification_metrics["gate_wait_count"]),
        ),
        "gate_reason_counts": runtime.get(
            "gate_reason_counts",
            _metric_text_or_default(metrics_rows, "gate_reason_counts", clarification_metrics["gate_reason_counts"]),
        ),
        "retrieved_node_count": runtime.get(
            "retrieved_node_count",
            _sum_metric_rows_or_default(metrics_rows, "retrieved_node_count", retrieval_metrics["retrieved_node_count"]),
        ),
        "retrieved_claim_count": runtime.get(
            "retrieved_claim_count",
            _sum_metric_rows_or_default(metrics_rows, "retrieved_claim_count", retrieval_metrics["retrieved_claim_count"]),
        ),
        "retrieved_action_count": runtime.get(
            "retrieved_action_count",
            _sum_metric_rows_or_default(metrics_rows, "retrieved_action_count", retrieval_metrics["retrieved_action_count"]),
        ),
        "mean_retrieved_node_age": runtime.get(
            "mean_retrieved_node_age",
            _mean_metric_rows_or_default(metrics_rows, "mean_retrieved_node_age", retrieval_metrics["mean_retrieved_node_age"]),
        ),
        "max_retrieved_node_age": runtime.get(
            "max_retrieved_node_age",
            _mean_metric_rows_or_default(metrics_rows, "max_retrieved_node_age", retrieval_metrics["max_retrieved_node_age"]),
        ),
        "retrieved_executed_candidate_count": runtime.get(
            "retrieved_executed_candidate_count",
            _sum_metric_rows_or_default(metrics_rows, "retrieved_executed_candidate_count", retrieval_metrics["retrieved_executed_candidate_count"]),
        ),
        "retrieved_invalidated_candidate_count": runtime.get(
            "retrieved_invalidated_candidate_count",
            _sum_metric_rows_or_default(metrics_rows, "retrieved_invalidated_candidate_count", retrieval_metrics["retrieved_invalidated_candidate_count"]),
        ),
        "retrieved_superseded_node_count": runtime.get(
            "retrieved_superseded_node_count",
            _sum_metric_rows_or_default(metrics_rows, "retrieved_superseded_node_count", retrieval_metrics["retrieved_superseded_node_count"]),
        ),
        "retrieval_used_in_top_action_count": runtime.get(
            "retrieval_used_in_top_action_count",
            _sum_metric_rows_or_default(metrics_rows, "retrieval_used_in_top_action_count", retrieval_metrics["retrieval_used_in_top_action_count"]),
        ),
        "retrieval_changed_top_action_count": runtime.get(
            "retrieval_changed_top_action_count",
            _sum_metric_rows_or_default(metrics_rows, "retrieval_changed_top_action_count", retrieval_metrics["retrieval_changed_top_action_count"]),
        ),
        "gated_clarification_count": runtime.get(
            "gated_clarification_count",
            _sum_metric_rows(metrics_rows, "gated_clarification_count"),
        ),
        "gated_clarification_rate": runtime.get(
            "gated_clarification_rate",
            _mean_metric_rows(metrics_rows, "gated_clarification_rate"),
        ),
        "clarification_resolution_count": runtime.get(
            "clarification_resolution_count",
            _sum_metric_rows(metrics_rows, "clarification_resolution_count"),
        ),
        "clarification_resolution_rate": runtime.get(
            "clarification_resolution_rate",
            _mean_metric_rows(metrics_rows, "clarification_resolution_rate"),
        ),
        "mean_clarification_quality_score": runtime.get(
            "mean_clarification_quality_score",
            _mean_metric_rows(metrics_rows, "mean_clarification_quality_score"),
        ),
        "mean_post_clarification_progress_delta": runtime.get(
            "mean_post_clarification_progress_delta",
            _mean_metric_rows(metrics_rows, "mean_post_clarification_progress_delta"),
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
        "required_evidence_gate_count": runtime.get(
            "required_evidence_gate_count",
            _sum_metric_rows_or_default(metrics_rows, "required_evidence_gate_count", clarification_metrics["required_evidence_gate_count"]),
        ),
        "span_uncertainty_gate_count": runtime.get(
            "span_uncertainty_gate_count",
            _sum_metric_rows_or_default(metrics_rows, "span_uncertainty_gate_count", clarification_metrics["span_uncertainty_gate_count"]),
        ),
        "dual_dag_node_count": runtime.get(
            "dual_dag_node_count",
            _sum_metric_rows(metrics_rows, "dual_dag_node_count"),
        ),
        "dual_dag_edge_count": runtime.get(
            "dual_dag_edge_count",
            _sum_metric_rows(metrics_rows, "dual_dag_edge_count"),
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


def _read_turns(turns_path: Path) -> list[dict]:
    if not turns_path.exists():
        return []
    turns = []
    with turns_path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                turns.append(json.loads(line))
    return turns


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


def _sum_metric_rows_or_default(rows: list[dict], field: str, default: int) -> int:
    if not _metric_field_exists(rows, field):
        return default
    return _sum_metric_rows(rows, field)


def _mean_metric_rows_or_default(rows: list[dict], field: str, default: float) -> float:
    if not _metric_field_exists(rows, field):
        return default
    return _mean_metric_rows(rows, field)


def _metric_field_exists(rows: list[dict], field: str) -> bool:
    return any(field in row for row in rows)


def _merge_reason_counts(rows: list[dict], field: str) -> str:
    merged: dict[str, int] = {}
    for row in rows:
        value = row.get(field)
        if not value:
            continue
        try:
            counts = json.loads(value)
        except json.JSONDecodeError:
            continue
        if not isinstance(counts, dict):
            continue
        for reason, count in counts.items():
            try:
                merged[str(reason)] = merged.get(str(reason), 0) + int(count)
            except (TypeError, ValueError):
                continue
    return json.dumps(merged, sort_keys=True)


def _metric_text_or_default(rows: list[dict], field: str, default: str) -> str:
    if not _metric_field_exists(rows, field):
        return default
    return _merge_reason_counts(rows, field)


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
