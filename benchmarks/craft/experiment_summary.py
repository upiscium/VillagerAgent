import argparse
import csv
import json
from pathlib import Path

from benchmarks.craft.config import repo_root
from benchmarks.craft.result_converter import (
    _action_selection_metrics,
    _clarification_metrics,
    _progress_action_metrics,
    _retrieval_metrics,
)


SUMMARY_FIELDS = [
    "run_name",
    "run_group",
    "status",
    "error_type",
    "error_message",
    "condition",
    "seed",
    "structures",
    "mean_final_progress",
    "completion_rate",
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
    "gated_clarification_rate",
    "clarification_resolution_rate",
    "mean_clarification_quality_score",
    "mean_post_clarification_progress_delta",
    "mean_action_confidence",
    "required_evidence_gate_count",
    "span_uncertainty_gate_count",
    "claim_support_count",
    "claim_conflict_count",
    "claim_required_evidence_count",
    "dual_dag_node_count",
    "dual_dag_edge_count",
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
    "candidate_created_count",
    "candidate_blocked_count",
    "candidate_executable_count",
    "candidate_executed_count",
    "candidate_invalidated_count",
    "candidate_repeated_after_execution_count",
    "action_selection_suppression_enabled_count",
    "action_selection_suppression_disabled_count",
    "action_selection_suppression_attempt_count",
    "action_selection_repeated_zero_signature_count",
    "action_selection_suppression_no_match_count",
    "action_selection_all_candidates_suppressed_count",
    "action_selection_suppression_applied_count",
    "action_selection_suppressed_candidate_count",
    "action_selection_no_candidate_count",
    "candidate_state_transition_counts",
    "coordination_action_count",
    "clarify_coordination_action_count",
    "wait_for_evidence_coordination_action_count",
    "supported_action_count",
    "conflicted_action_count",
    "required_evidence_action_count",
    "leakage_passed",
]


_ACTION_CANDIDATE_STATES_FOR_CREATED = [
    "candidate",
    "executable",
    "waiting_for_evidence",
    "blocked",
    "invalidated",
    "executed",
]


VARIANCE_FIELDS = [
    "group",
    "run_count",
    "completed_run_count",
    "failed_run_count",
    "seed_count",
    "structures",
    "mean_final_progress_mean",
    "mean_final_progress_stddev",
    "mean_final_progress_min",
    "mean_final_progress_max",
    "completion_rate_mean",
    "completion_rate_stddev",
    "completion_rate_min",
    "completion_rate_max",
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


def build_variance_summary(rows: list[dict], *, group_by: str = "run_group") -> list[dict]:
    if not rows:
        raise ExperimentSummaryError("At least one CRAFT summary row is required.")
    groups: dict[str, list[dict]] = {}
    for row in rows:
        key = str(row.get(group_by) or row.get("condition") or row.get("run_name") or "")
        groups.setdefault(key, []).append(row)
    return [_summarize_variance_group(group, group_rows) for group, group_rows in sorted(groups.items())]


def write_variance_csv(rows: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=VARIANCE_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in VARIANCE_FIELDS})


def write_variance_json(rows: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump({"groups": rows}, f, ensure_ascii=False, indent=2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write a compact CRAFT experiment summary table.")
    parser.add_argument("--runs", nargs="+", required=True)
    parser.add_argument("--result-root", default="result/craft")
    parser.add_argument("--analysis-input", default=None)
    parser.add_argument("--output", default="result/craft/experiment_summary.csv")
    parser.add_argument("--json-output", default=None)
    parser.add_argument("--variance-output", default=None)
    parser.add_argument("--variance-json-output", default=None)
    parser.add_argument("--variance-group-by", default="run_group")
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
    if args.variance_output:
        variance_rows = build_variance_summary(rows, group_by=args.variance_group_by)
        write_variance_csv(variance_rows, _resolve_path(root, args.variance_output))
        if args.variance_json_output:
            write_variance_json(variance_rows, _resolve_path(root, args.variance_json_output))
    print(f"Wrote CRAFT experiment summary: {output}")


def _summarize_run(run_name: str, *, result_root: Path, analysis: dict | None = None) -> dict:
    normalized_dir = result_root / run_name / "normalized"
    summary = _read_json(normalized_dir / "summary.json")
    runtime = summary.get("runtime", {})
    status = summary.get("status") or runtime.get("status") or "completed"
    failure = summary.get("failure") or runtime.get("failure", {}) or {}
    analysis = analysis or _read_optional_json(normalized_dir / "dual_dag_analysis.json")
    turns = _read_turns(normalized_dir / "turns.jsonl")
    progress_action_metrics = _progress_action_metrics({
        "turns": turns,
        "final_progress": summary.get("mean_final_progress", 0.0),
    })
    clarification_metrics = _clarification_metrics(turns)
    retrieval_metrics = _retrieval_metrics(turns)
    action_selection_metrics = _action_selection_metrics(turns)
    return {
        "run_name": summary.get("run_name", run_name),
        "run_group": _run_group(summary.get("run_name", run_name)),
        "status": status,
        "error_type": failure.get("type", ""),
        "error_message": failure.get("message", ""),
        "condition": summary.get("condition", ""),
        "seed": summary.get("seed", ""),
        "structures": ",".join(str(item) for item in summary.get("structures", []) or []),
        "mean_final_progress": summary.get("mean_final_progress", 0.0),
        "completion_rate": summary.get("completion_rate", 0.0),
        "builder_fallback_rate": runtime.get("builder_fallback_rate", 0.0),
        "max_progress": runtime.get("max_progress", progress_action_metrics["max_progress"]),
        "progress_auc": runtime.get("progress_auc", progress_action_metrics["progress_auc"]),
        "physical_action_count": runtime.get("physical_action_count", progress_action_metrics["physical_action_count"]),
        "place_action_count": runtime.get("place_action_count", progress_action_metrics["place_action_count"]),
        "remove_action_count": runtime.get("remove_action_count", progress_action_metrics["remove_action_count"]),
        "clarify_count": runtime.get("clarify_count", progress_action_metrics["clarify_count"]),
        "wait_count": runtime.get("wait_count", progress_action_metrics["wait_count"]),
        "fallback_count": runtime.get("fallback_count", progress_action_metrics["fallback_count"]),
        "no_op_count": runtime.get("no_op_count", progress_action_metrics["no_op_count"]),
        "invalid_action_count": runtime.get("invalid_action_count", progress_action_metrics["invalid_action_count"]),
        "positive_progress_turn_count": runtime.get("positive_progress_turn_count", progress_action_metrics["positive_progress_turn_count"]),
        "zero_progress_turn_count": runtime.get("zero_progress_turn_count", progress_action_metrics["zero_progress_turn_count"]),
        "negative_progress_turn_count": runtime.get("negative_progress_turn_count", progress_action_metrics["negative_progress_turn_count"]),
        "mean_progress_delta_per_turn": runtime.get("mean_progress_delta_per_turn", progress_action_metrics["mean_progress_delta_per_turn"]),
        "mean_progress_delta_per_physical_action": runtime.get("mean_progress_delta_per_physical_action", progress_action_metrics["mean_progress_delta_per_physical_action"]),
        "unique_clarification_count": runtime.get("unique_clarification_count", clarification_metrics["unique_clarification_count"]),
        "repeated_clarification_count": runtime.get("repeated_clarification_count", clarification_metrics["repeated_clarification_count"]),
        "clarification_response_count": runtime.get("clarification_response_count", clarification_metrics["clarification_response_count"]),
        "clarification_to_unlock_count": runtime.get("clarification_to_unlock_count", clarification_metrics["clarification_to_unlock_count"]),
        "clarification_to_unlock_rate": runtime.get("clarification_to_unlock_rate", clarification_metrics["clarification_to_unlock_rate"]),
        "clarification_to_positive_action_count": runtime.get("clarification_to_positive_action_count", clarification_metrics["clarification_to_positive_action_count"]),
        "clarification_to_positive_action_latency": runtime.get("clarification_to_positive_action_latency", clarification_metrics["clarification_to_positive_action_latency"]),
        "clarification_without_state_change_count": runtime.get("clarification_without_state_change_count", clarification_metrics["clarification_without_state_change_count"]),
        "gate_invocation_count": runtime.get("gate_invocation_count", clarification_metrics["gate_invocation_count"]),
        "gate_allow_count": runtime.get("gate_allow_count", clarification_metrics["gate_allow_count"]),
        "gate_block_count": runtime.get("gate_block_count", clarification_metrics["gate_block_count"]),
        "gate_clarify_count": runtime.get("gate_clarify_count", clarification_metrics["gate_clarify_count"]),
        "gate_wait_count": runtime.get("gate_wait_count", clarification_metrics["gate_wait_count"]),
        "gate_reason_counts": runtime.get("gate_reason_counts", clarification_metrics["gate_reason_counts"]),
        "retrieved_node_count": runtime.get("retrieved_node_count", retrieval_metrics["retrieved_node_count"]),
        "retrieved_claim_count": runtime.get("retrieved_claim_count", retrieval_metrics["retrieved_claim_count"]),
        "retrieved_action_count": runtime.get("retrieved_action_count", retrieval_metrics["retrieved_action_count"]),
        "mean_retrieved_node_age": runtime.get("mean_retrieved_node_age", retrieval_metrics["mean_retrieved_node_age"]),
        "max_retrieved_node_age": runtime.get("max_retrieved_node_age", retrieval_metrics["max_retrieved_node_age"]),
        "retrieved_executed_candidate_count": runtime.get("retrieved_executed_candidate_count", retrieval_metrics["retrieved_executed_candidate_count"]),
        "retrieved_invalidated_candidate_count": runtime.get("retrieved_invalidated_candidate_count", retrieval_metrics["retrieved_invalidated_candidate_count"]),
        "retrieved_superseded_node_count": runtime.get("retrieved_superseded_node_count", retrieval_metrics["retrieved_superseded_node_count"]),
        "retrieval_used_in_top_action_count": runtime.get("retrieval_used_in_top_action_count", retrieval_metrics["retrieval_used_in_top_action_count"]),
        "retrieval_changed_top_action_count": runtime.get("retrieval_changed_top_action_count", retrieval_metrics["retrieval_changed_top_action_count"]),
        "gated_clarification_rate": runtime.get("gated_clarification_rate", 0.0),
        "clarification_resolution_rate": runtime.get("clarification_resolution_rate", 0.0),
        "mean_clarification_quality_score": runtime.get("mean_clarification_quality_score", 0.0),
        "mean_post_clarification_progress_delta": runtime.get("mean_post_clarification_progress_delta", 0.0),
        "mean_action_confidence": runtime.get("mean_action_confidence", 0.0),
        "required_evidence_gate_count": runtime.get("required_evidence_gate_count", clarification_metrics["required_evidence_gate_count"]),
        "span_uncertainty_gate_count": runtime.get("span_uncertainty_gate_count", clarification_metrics["span_uncertainty_gate_count"]),
        "claim_support_count": runtime.get("claim_support_count", 0),
        "claim_conflict_count": runtime.get("claim_conflict_count", 0),
        "claim_required_evidence_count": runtime.get("claim_required_evidence_count", 0),
        "dual_dag_node_count": runtime.get("dual_dag_node_count", 0),
        "dual_dag_edge_count": runtime.get("dual_dag_edge_count", 0),
        "resolved_fact_count": runtime.get("resolved_fact_count", analysis.get("resolved_fact_count", 0)),
        "hypothesis_open_count": runtime.get("hypothesis_open_count", analysis.get("hypothesis_status_counts", {}).get("open", 0)),
        "hypothesis_supported_count": runtime.get("hypothesis_supported_count", analysis.get("hypothesis_status_counts", {}).get("supported", 0)),
        "hypothesis_conflicted_count": runtime.get("hypothesis_conflicted_count", analysis.get("hypothesis_status_counts", {}).get("conflicted", 0)),
        "hypothesis_resolved_count": runtime.get("hypothesis_resolved_count", analysis.get("hypothesis_status_counts", {}).get("resolved", 0)),
        "hypothesis_invalidated_count": runtime.get("hypothesis_invalidated_count", analysis.get("hypothesis_status_counts", {}).get("invalidated", 0)),
        "action_candidate_candidate_count": runtime.get("action_candidate_candidate_count", analysis.get("action_state_counts", {}).get("candidate", 0)),
        "action_candidate_executable_count": runtime.get("action_candidate_executable_count", analysis.get("action_state_counts", {}).get("executable", 0)),
        "action_candidate_waiting_for_evidence_count": runtime.get("action_candidate_waiting_for_evidence_count", analysis.get("action_state_counts", {}).get("waiting_for_evidence", 0)),
        "action_candidate_blocked_count": runtime.get("action_candidate_blocked_count", analysis.get("action_state_counts", {}).get("blocked", 0)),
        "action_candidate_invalidated_count": runtime.get("action_candidate_invalidated_count", analysis.get("action_state_counts", {}).get("invalidated", 0)),
        "action_candidate_executed_count": runtime.get("action_candidate_executed_count", analysis.get("action_state_counts", {}).get("executed", 0)),
        "candidate_created_count": runtime.get("candidate_created_count", _candidate_created_fallback(runtime, analysis)),
        "candidate_blocked_count": runtime.get("candidate_blocked_count", runtime.get("action_candidate_blocked_count", 0)),
        "candidate_executable_count": runtime.get("candidate_executable_count", runtime.get("action_candidate_executable_count", 0)),
        "candidate_executed_count": runtime.get("candidate_executed_count", runtime.get("action_candidate_executed_count", 0)),
        "candidate_invalidated_count": runtime.get("candidate_invalidated_count", runtime.get("action_candidate_invalidated_count", 0)),
        "candidate_repeated_after_execution_count": runtime.get("candidate_repeated_after_execution_count", 0),
        "action_selection_suppression_enabled_count": runtime.get("action_selection_suppression_enabled_count", action_selection_metrics["action_selection_suppression_enabled_count"]),
        "action_selection_suppression_disabled_count": runtime.get("action_selection_suppression_disabled_count", action_selection_metrics["action_selection_suppression_disabled_count"]),
        "action_selection_suppression_attempt_count": runtime.get("action_selection_suppression_attempt_count", action_selection_metrics["action_selection_suppression_attempt_count"]),
        "action_selection_repeated_zero_signature_count": runtime.get("action_selection_repeated_zero_signature_count", action_selection_metrics["action_selection_repeated_zero_signature_count"]),
        "action_selection_suppression_no_match_count": runtime.get("action_selection_suppression_no_match_count", action_selection_metrics["action_selection_suppression_no_match_count"]),
        "action_selection_all_candidates_suppressed_count": runtime.get("action_selection_all_candidates_suppressed_count", action_selection_metrics["action_selection_all_candidates_suppressed_count"]),
        "action_selection_suppression_applied_count": runtime.get("action_selection_suppression_applied_count", action_selection_metrics["action_selection_suppression_applied_count"]),
        "action_selection_suppressed_candidate_count": runtime.get("action_selection_suppressed_candidate_count", action_selection_metrics["action_selection_suppressed_candidate_count"]),
        "action_selection_no_candidate_count": runtime.get("action_selection_no_candidate_count", action_selection_metrics["action_selection_no_candidate_count"]),
        "candidate_state_transition_counts": runtime.get("candidate_state_transition_counts", "{}"),
        "coordination_action_count": runtime.get("coordination_action_count", sum(analysis.get("coordination_action_counts", {}).values())),
        "clarify_coordination_action_count": runtime.get("clarify_coordination_action_count", analysis.get("coordination_action_counts", {}).get("clarify", 0)),
        "wait_for_evidence_coordination_action_count": runtime.get("wait_for_evidence_coordination_action_count", analysis.get("coordination_action_counts", {}).get("wait_for_evidence", 0)),
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


def _read_turns(path: Path) -> list[dict]:
    if not path.exists():
        return []
    turns = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                turns.append(json.loads(line))
    return turns


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


def _candidate_created_fallback(runtime: dict, analysis: dict) -> int:
    analysis_states = analysis.get("action_state_counts", {})
    states = _ACTION_CANDIDATE_STATES_FOR_CREATED
    if analysis_states:
        return sum(int(analysis_states.get(state, 0) or 0) for state in states)
    return sum(int(runtime.get(f"action_candidate_{state}_count", 0) or 0) for state in states)


def _summarize_variance_group(group: str, rows: list[dict]) -> dict:
    completed_rows = [row for row in rows if row.get("status", "completed") == "completed"]
    progress = [_as_float(row.get("mean_final_progress", 0.0)) for row in completed_rows]
    completion = [_as_float(row.get("completion_rate", 0.0)) for row in completed_rows]
    seeds = {str(row.get("seed")) for row in rows if row.get("seed") not in (None, "")}
    structures = sorted({
        structure.strip()
        for row in rows
        for structure in str(row.get("structures", "")).split(",")
        if structure.strip()
    }, key=_structure_sort_key)
    return {
        "group": group,
        "run_count": len(rows),
        "completed_run_count": len(completed_rows),
        "failed_run_count": len(rows) - len(completed_rows),
        "seed_count": len(seeds),
        "structures": ",".join(structures),
        "mean_final_progress_mean": _mean(progress),
        "mean_final_progress_stddev": _stddev(progress),
        "mean_final_progress_min": min(progress) if progress else 0.0,
        "mean_final_progress_max": max(progress) if progress else 0.0,
        "completion_rate_mean": _mean(completion),
        "completion_rate_stddev": _stddev(completion),
        "completion_rate_min": min(completion) if completion else 0.0,
        "completion_rate_max": max(completion) if completion else 0.0,
    }


def _as_float(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stddev(values: list[float]) -> float:
    if len(values) <= 1:
        return 0.0
    mean = _mean(values)
    return (sum((value - mean) ** 2 for value in values) / len(values)) ** 0.5


def _structure_sort_key(value: str):
    try:
        return (0, int(value))
    except ValueError:
        return (1, value)


def _run_group(run_name: str) -> str:
    marker = "_seed"
    prefix, marker_found, suffix = run_name.rpartition(marker)
    if marker_found and suffix.isdigit():
        return prefix
    return run_name


def _resolve_path(root: Path, path: str) -> Path:
    resolved = Path(path)
    if resolved.is_absolute():
        return resolved
    return root / resolved


if __name__ == "__main__":
    main()
