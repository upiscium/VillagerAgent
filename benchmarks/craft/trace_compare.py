import argparse
import csv
import json
from pathlib import Path
from typing import Any

from benchmarks.craft.config import repo_root


CSV_FIELDS = [
    "structure_id",
    "baseline_final_progress",
    "variant_final_progress",
    "delta_final_progress",
    "baseline_progress_auc",
    "variant_progress_auc",
    "baseline_fallback_count",
    "variant_fallback_count",
    "baseline_repeated_zero_progress_streak_max",
    "variant_repeated_zero_progress_streak_max",
    "baseline_zero_or_missing_progress_turn_count",
    "variant_zero_or_missing_progress_turn_count",
    "baseline_negative_progress_turn_count",
    "variant_negative_progress_turn_count",
    "baseline_final_action",
    "variant_final_action",
    "baseline_final_progress_delta",
    "variant_final_progress_delta",
    "first_action_divergence_turn",
]


class TraceCompareError(ValueError):
    """Raised when trace comparison inputs are missing or incompatible."""


def compare_runs(baseline_run: str, variant_run: str, *, result_root: Path) -> dict:
    baseline_dir = result_root / baseline_run / "normalized"
    variant_dir = result_root / variant_run / "normalized"
    baseline_metrics = _load_metrics_by_structure(baseline_dir / "metrics.csv")
    variant_metrics = _load_metrics_by_structure(variant_dir / "metrics.csv")
    baseline_turns = _load_turns_by_structure(baseline_dir / "turns.jsonl")
    variant_turns = _load_turns_by_structure(variant_dir / "turns.jsonl")
    structure_ids = sorted(set(baseline_metrics) & set(variant_metrics))
    if not structure_ids:
        raise TraceCompareError("No overlapping structures found for trace comparison.")

    rows = []
    for structure_id in structure_ids:
        baseline_trace = _summarize_structure_turns(baseline_turns.get(structure_id, []))
        variant_trace = _summarize_structure_turns(variant_turns.get(structure_id, []))
        baseline_row = baseline_metrics[structure_id]
        variant_row = variant_metrics[structure_id]
        baseline_final = _float(baseline_row.get("final_progress"))
        variant_final = _float(variant_row.get("final_progress"))
        rows.append({
            "structure_id": structure_id,
            "baseline_final_progress": baseline_final,
            "variant_final_progress": variant_final,
            "delta_final_progress": variant_final - baseline_final,
            "baseline_progress_auc": _float(baseline_row.get("progress_auc")),
            "variant_progress_auc": _float(variant_row.get("progress_auc")),
            "baseline_fallback_count": _float(baseline_row.get("fallback_count")),
            "variant_fallback_count": _float(variant_row.get("fallback_count")),
            "baseline_repeated_zero_progress_streak_max": baseline_trace[
                "repeated_zero_progress_streak_max"
            ],
            "variant_repeated_zero_progress_streak_max": variant_trace[
                "repeated_zero_progress_streak_max"
            ],
            "baseline_zero_or_missing_progress_turn_count": baseline_trace[
                "zero_or_missing_progress_turn_count"
            ],
            "variant_zero_or_missing_progress_turn_count": variant_trace[
                "zero_or_missing_progress_turn_count"
            ],
            "baseline_negative_progress_turn_count": baseline_trace["negative_progress_turn_count"],
            "variant_negative_progress_turn_count": variant_trace["negative_progress_turn_count"],
            "baseline_final_action": baseline_trace["final_action"],
            "variant_final_action": variant_trace["final_action"],
            "baseline_final_progress_delta": baseline_trace["final_progress_delta"],
            "variant_final_progress_delta": variant_trace["final_progress_delta"],
            "first_action_divergence_turn": _first_action_divergence(
                baseline_turns.get(structure_id, []),
                variant_turns.get(structure_id, []),
            ),
        })

    return {
        "baseline_run": baseline_run,
        "variant_run": variant_run,
        "structure_count": len(rows),
        "mean_delta_final_progress": _mean(row["delta_final_progress"] for row in rows),
        "baseline_mean_repeated_zero_progress_streak_max": _mean(
            row["baseline_repeated_zero_progress_streak_max"] for row in rows
        ),
        "variant_mean_repeated_zero_progress_streak_max": _mean(
            row["variant_repeated_zero_progress_streak_max"] for row in rows
        ),
        "baseline_zero_or_missing_progress_turn_count": sum(
            row["baseline_zero_or_missing_progress_turn_count"] for row in rows
        ),
        "variant_zero_or_missing_progress_turn_count": sum(
            row["variant_zero_or_missing_progress_turn_count"] for row in rows
        ),
        "rows": rows,
    }


def write_csv(rows: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})


def write_json(report: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare normalized CRAFT turn traces.")
    parser.add_argument("--baseline-run", required=True)
    parser.add_argument("--variant-run", required=True)
    parser.add_argument("--result-root", default="result/craft")
    parser.add_argument("--output", required=True)
    parser.add_argument("--json-output", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = repo_root()
    result_root = _resolve_path(root, args.result_root)
    report = compare_runs(args.baseline_run, args.variant_run, result_root=result_root)
    write_csv(report["rows"], _resolve_path(root, args.output))
    if args.json_output:
        write_json(report, _resolve_path(root, args.json_output))
    print(f"Wrote CRAFT trace comparison: {_resolve_path(root, args.output)}")


def _load_metrics_by_structure(path: Path) -> dict[int, dict]:
    if not path.exists():
        raise TraceCompareError(f"Missing CRAFT metrics: {path}")
    with path.open("r", encoding="utf-8", newline="") as f:
        return {int(row["structure_id"]): row for row in csv.DictReader(f)}


def _load_turns_by_structure(path: Path) -> dict[int, list[dict]]:
    if not path.exists():
        raise TraceCompareError(f"Missing CRAFT turns: {path}")
    turns: dict[int, list[dict]] = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            turns.setdefault(int(row["structure_id"]), []).append(row)
    for rows in turns.values():
        rows.sort(key=lambda row: int(row.get("turn_index", 0)))
    return turns


def _summarize_structure_turns(turns: list[dict]) -> dict:
    current_signature = None
    current_streak = 0
    max_streak = 0
    zero_or_missing_count = 0
    negative_count = 0
    final_action = ""
    final_delta = None

    for turn in turns:
        signature = _action_signature(turn.get("builder_action") or {})
        delta = _progress_delta(turn)
        final_action = signature
        final_delta = delta
        if delta is None or delta == 0:
            zero_or_missing_count += 1
            if signature == current_signature:
                current_streak += 1
            else:
                current_signature = signature
                current_streak = 1
            max_streak = max(max_streak, current_streak)
        else:
            current_signature = None
            current_streak = 0
        if delta is not None and delta < 0:
            negative_count += 1

    return {
        "repeated_zero_progress_streak_max": max_streak,
        "zero_or_missing_progress_turn_count": zero_or_missing_count,
        "negative_progress_turn_count": negative_count,
        "final_action": final_action,
        "final_progress_delta": final_delta,
    }


def _first_action_divergence(baseline_turns: list[dict], variant_turns: list[dict]) -> int | None:
    by_turn = {int(row.get("turn_index", 0)): row for row in variant_turns}
    for row in baseline_turns:
        turn_index = int(row.get("turn_index", 0))
        other = by_turn.get(turn_index)
        if other is None:
            return turn_index
        if _action_signature(row.get("builder_action") or {}) != _action_signature(
            other.get("builder_action") or {}
        ):
            return turn_index
    return None


def _action_signature(action: dict) -> str:
    action_type = action.get("action") or ""
    block = action.get("block") or ""
    position = _stable_value(action.get("position"))
    layer = action.get("layer")
    span_to = _stable_value(action.get("span_to"))
    return f"{action_type}:{block}:{position}:{layer}:{span_to}"


def _progress_delta(turn: dict) -> float | None:
    progress = turn.get("progress") or {}
    return _float_or_none(progress.get("progress_delta"))


def _stable_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _float(value: Any) -> float:
    result = _float_or_none(value)
    if result is None:
        return 0.0
    return result


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mean(values) -> float:
    items = list(values)
    if not items:
        return 0.0
    return sum(items) / len(items)


def _resolve_path(root: Path, path: str) -> Path:
    output = Path(path)
    if output.is_absolute():
        return output
    return root / output


if __name__ == "__main__":
    main()
