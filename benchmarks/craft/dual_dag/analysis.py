import argparse
import csv
import json
from collections import Counter
from pathlib import Path

from benchmarks.craft.config import repo_root
from benchmarks.craft.hidden_state_keys import hidden_state_key_labels


TURN_FIELDNAMES = [
    "run_name",
    "condition",
    "structure_id",
    "turn_index",
    "action",
    "chosen_candidate_id",
    "chosen_confidence",
    "claim_support_count",
    "claim_conflict_count",
    "claim_required_evidence_count",
    "gated_clarification",
    "gate_reasons",
    "builder_fallback",
    "move_executed",
    "progress",
]

HIDDEN_STATE_KEYS = hidden_state_key_labels()


class DualDAGAnalysisError(ValueError):
    """Raised when normalized Dual-DAG artifacts cannot be analyzed."""


def analyze_run(run_name: str, *, result_root: Path) -> dict:
    run_dir = result_root / run_name
    normalized_dir = run_dir / "normalized"
    summary = _read_json(normalized_dir / "summary.json")
    dag_summary = _read_json(normalized_dir / "dual_dag_summary.json")
    nodes = _read_jsonl(normalized_dir / "dual_dag_nodes.jsonl")
    edges = _read_jsonl(normalized_dir / "dual_dag_edges.jsonl")
    turns = _read_jsonl(normalized_dir / "turns.jsonl")

    node_type_counts = Counter(_node_kind(node) for node in nodes)
    edge_type_counts = Counter(edge.get("edge_type", "") for edge in edges)
    nodes_by_id = {node.get("node_id"): node for node in nodes if node.get("node_id")}
    epistemic_edge_type_counts = Counter(
        edge.get("edge_type", "")
        for edge in edges
        if _edge_graph_type(edge, nodes_by_id) == "epistemic"
    )
    action_edge_type_counts = Counter(
        edge.get("edge_type", "")
        for edge in edges
        if _edge_graph_type(edge, nodes_by_id) == "action"
    )
    claims_by_id = {
        node.get("node_id"): node
        for node in nodes
        if node.get("node_type") == "reported_claim"
    }
    director_claim_counts = Counter(
        (node.get("content") or {}).get("director_id", "unknown")
        for node in claims_by_id.values()
    )
    director_support_counts: Counter[str] = Counter()
    director_conflict_counts: Counter[str] = Counter()
    director_required_evidence_counts: Counter[str] = Counter()
    action_support_counts: Counter[str] = Counter()
    action_conflict_counts: Counter[str] = Counter()
    action_required_evidence_counts: Counter[str] = Counter()
    for edge in edges:
        if _edge_graph_type(edge, nodes_by_id) != "action":
            continue
        claim = claims_by_id.get(edge.get("source_id"), {})
        director_id = (claim.get("content") or {}).get("director_id", "unknown")
        target_id = edge.get("target_id", "")
        if edge.get("edge_type") == "supports":
            director_support_counts[director_id] += 1
            action_support_counts[target_id] += 1
        if edge.get("edge_type") == "conflicts_with":
            director_conflict_counts[director_id] += 1
            action_conflict_counts[target_id] += 1
    for node in nodes:
        required_evidence = node.get("required_evidence", [])
        if not required_evidence:
            continue
        action_id = node.get("node_id", "")
        action_required_evidence_counts[action_id] += len(required_evidence)
        for claim_id in required_evidence:
            claim = claims_by_id.get(claim_id, {})
            director_id = (claim.get("content") or {}).get("director_id", "unknown")
            director_required_evidence_counts[director_id] += 1

    turn_rows = [_turn_row(run_name, summary, turn) for turn in turns]
    confidences = [
        row["chosen_confidence"]
        for row in turn_rows
        if row["chosen_confidence"] != ""
    ]
    failure_modes = _failure_mode_summary(turn_rows)
    return {
        "run_name": summary.get("run_name", run_name),
        "condition": summary.get("condition", ""),
        "node_count": dag_summary.get("node_count", len(nodes)),
        "edge_count": dag_summary.get("edge_count", len(edges)),
        "artifact_health": _artifact_health(normalized_dir, turns, dag_summary),
        "failure_modes": failure_modes,
        "node_type_counts": dict(node_type_counts),
        "edge_type_counts": dict(edge_type_counts),
        "epistemic_edge_type_counts": dict(epistemic_edge_type_counts),
        "action_edge_type_counts": dict(action_edge_type_counts),
        "director_claim_counts": dict(director_claim_counts),
        "director_support_counts": dict(director_support_counts),
        "director_conflict_counts": dict(director_conflict_counts),
        "director_required_evidence_counts": dict(director_required_evidence_counts),
        "supported_action_count": sum(1 for count in action_support_counts.values() if count > 0),
        "conflicted_action_count": sum(1 for count in action_conflict_counts.values() if count > 0),
        "required_evidence_action_count": sum(1 for count in action_required_evidence_counts.values() if count > 0),
        "mean_action_confidence": sum(confidences) / len(confidences) if confidences else 0.0,
        "gated_clarification_count": failure_modes["gated_clarification"]["count"],
        "builder_fallback_count": failure_modes["fallback"]["count"],
        "turns": turn_rows,
    }


def analyze_runs(run_names: list[str], *, result_root: Path) -> dict:
    if not run_names:
        raise DualDAGAnalysisError("At least one CRAFT run is required.")
    runs = [analyze_run(run_name, result_root=result_root) for run_name in run_names]
    return {"runs": runs}


def write_json_analysis(analysis: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(analysis, f, ensure_ascii=False, indent=2)


def write_turn_csv(analysis: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TURN_FIELDNAMES)
        writer.writeheader()
        for row in _turn_rows_for_csv(analysis):
            writer.writerow({field: row.get(field, "") for field in TURN_FIELDNAMES})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze normalized CRAFT Dual-DAG artifacts.")
    parser.add_argument("--run", default=None)
    parser.add_argument("--runs", nargs="+", default=None)
    parser.add_argument("--result-root", default="result/craft")
    parser.add_argument("--output", default=None)
    parser.add_argument("--turn-csv-output", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_names = args.runs or ([args.run] if args.run else [])
    if not run_names:
        raise DualDAGAnalysisError("Specify --run or --runs.")
    root = repo_root()
    result_root = _resolve_path(root, args.result_root)
    analysis = analyze_runs(run_names, result_root=result_root) if len(run_names) > 1 else analyze_run(run_names[0], result_root=result_root)
    output = _resolve_path(
        root,
        args.output or _default_output_path(run_names),
    )
    write_json_analysis(analysis, output)
    if args.turn_csv_output:
        write_turn_csv(analysis, _resolve_path(root, args.turn_csv_output))
    print(f"Wrote CRAFT Dual-DAG analysis: {output}")


def _turn_row(run_name: str, summary: dict, turn: dict) -> dict:
    action = turn.get("builder_action") or {}
    metadata = action.get("_action_candidate_metadata", {}) or {}
    gate = action.get("_gated_clarification", {}) or {}
    return {
        "run_name": summary.get("run_name", run_name),
        "condition": summary.get("condition", ""),
        "structure_id": turn.get("structure_id"),
        "turn_index": turn.get("turn_index"),
        "action": action.get("action", ""),
        "chosen_candidate_id": metadata.get("chosen_candidate_id", ""),
        "chosen_confidence": metadata.get("chosen_confidence", ""),
        "claim_support_count": metadata.get("claim_support_count", 0),
        "claim_conflict_count": metadata.get("claim_conflict_count", 0),
        "claim_required_evidence_count": metadata.get("claim_required_evidence_count", 0),
        "gated_clarification": bool(gate),
        "gate_reasons": ",".join(gate.get("reasons", [])),
        "builder_fallback": bool(action.get("_builder_fallback")),
        "move_executed": bool(turn.get("move_executed", False)),
        "progress": _progress_value(turn.get("progress")),
    }


def _turn_rows_for_csv(analysis: dict) -> list[dict]:
    if "runs" in analysis:
        return [row for run in analysis["runs"] for row in run.get("turns", [])]
    return list(analysis.get("turns", []))


def _default_output_path(run_names: list[str]) -> str:
    if len(run_names) == 1:
        return f"result/craft/{run_names[0]}/normalized/dual_dag_analysis.json"
    return "result/craft/dual_dag_analysis.json"


def _progress_value(progress) -> float | str:
    if isinstance(progress, dict):
        for key in ("progress", "current", "overall_progress"):
            value = progress.get(key)
            if value is not None:
                return value
        metrics = progress.get("metrics")
        if isinstance(metrics, dict) and metrics.get("overall_progress") is not None:
            return metrics["overall_progress"]
    if isinstance(progress, (int, float)):
        return progress
    return ""


def _failure_mode_summary(turn_rows: list[dict]) -> dict:
    categories = {
        "fallback": lambda row: bool(row["builder_fallback"]),
        "low_confidence": _is_low_confidence_turn,
        "conflict": lambda row: int(row["claim_conflict_count"] or 0) > 0,
        "required_evidence": lambda row: int(row["claim_required_evidence_count"] or 0) > 0,
        "clarification": lambda row: row["action"] == "clarify",
        "gated_clarification": lambda row: bool(row["gated_clarification"]),
    }
    total = len(turn_rows)
    summary = {}
    for name, predicate in categories.items():
        matching_rows = [row for row in turn_rows if predicate(row)]
        summary[name] = {
            "count": len(matching_rows),
            "rate": len(matching_rows) / total if total else 0.0,
            "turns": [
                {
                    "structure_id": row["structure_id"],
                    "turn_index": row["turn_index"],
                    "gate_reasons": row["gate_reasons"],
                }
                for row in matching_rows
            ],
        }
    return summary


def _is_low_confidence_turn(row: dict) -> bool:
    if "low_action_confidence" in str(row.get("gate_reasons", "")).split(","):
        return True
    confidence = row.get("chosen_confidence")
    if confidence == "":
        return False
    try:
        return float(confidence) < 0.5
    except (TypeError, ValueError):
        return False


def _artifact_health(normalized_dir: Path, turns: list[dict], dag_summary: dict) -> dict:
    artifact_files = [
        "turns.jsonl",
        "dual_dag_summary.json",
        "dual_dag_nodes.jsonl",
        "dual_dag_edges.jsonl",
        "leakage_report.json",
    ]
    missing_files = [name for name in artifact_files if not (normalized_dir / name).exists()]
    leakage_report = _read_json(normalized_dir / "leakage_report.json")
    leakage_checks = leakage_report.get("checks", [])
    leakage_passed = all(check.get("passed", True) for check in leakage_checks)
    hidden_key_hits = _hidden_key_hits(normalized_dir, artifact_files)
    node_count = dag_summary.get("node_count", 0)
    edge_count = dag_summary.get("edge_count", 0)
    action_metadata_turn_count = sum(
        1
        for turn in turns
        if (turn.get("builder_action") or {}).get("_action_candidate_metadata")
    )
    checks = {
        "required_files_present": not missing_files,
        "non_negative_dual_dag_counts": node_count >= 0 and edge_count >= 0,
        "action_candidate_metadata_present": action_metadata_turn_count > 0 if turns else False,
        "leakage_report_passed": leakage_passed,
        "hidden_state_keys_absent": not hidden_key_hits,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "missing_files": missing_files,
        "node_count": node_count,
        "edge_count": edge_count,
        "action_candidate_metadata_turn_count": action_metadata_turn_count,
        "hidden_state_key_hits": hidden_key_hits,
    }


def _hidden_key_hits(normalized_dir: Path, artifact_files: list[str]) -> list[dict]:
    hits = []
    for name in artifact_files:
        path = normalized_dir / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for key in HIDDEN_STATE_KEYS:
            if key in text:
                hits.append({"file": path.name, "key": key})
    return hits


def _node_kind(node: dict) -> str:
    return node.get("node_type") or node.get("action_type") or "unknown"


def _edge_graph_type(edge: dict, nodes_by_id: dict[str, dict]) -> str:
    graph_type = edge.get("graph_type")
    if graph_type in {"epistemic", "action"}:
        return graph_type
    source = nodes_by_id.get(edge.get("source_id"), {})
    target = nodes_by_id.get(edge.get("target_id"), {})
    source_kind = _node_kind(source)
    target_kind = _node_kind(target)
    if source_kind in {"observed_fact", "public_fact", "reported_claim", "hypothesis"}:
        if target_kind in {"observed_fact", "public_fact", "reported_claim", "hypothesis"}:
            return "epistemic"
    return "action"


def _read_json(path: Path) -> dict:
    if not path.exists():
        raise DualDAGAnalysisError(f"Missing CRAFT Dual-DAG artifact: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        raise DualDAGAnalysisError(f"Missing CRAFT Dual-DAG artifact: {path}")
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _resolve_path(root: Path, path: str) -> Path:
    resolved = Path(path)
    if resolved.is_absolute():
        return resolved
    return root / resolved


if __name__ == "__main__":
    main()
