import argparse
import csv
import json
from pathlib import Path


def build_fallback_monitor_report(run_names: list[str], *, result_root: Path) -> dict:
    runs = [_summarize_run(run_name, result_root=result_root) for run_name in run_names]
    return {
        "runs": runs,
        "by_condition": _group_rows(runs, key="condition_label"),
        "by_condition_and_structure": _group_rows(runs, key="condition_structure"),
    }


def write_fallback_monitor_report(report: dict, output: Path, *, csv_output: Path | None = None) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if csv_output is None:
        return
    csv_output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "run_name",
        "condition_label",
        "status",
        "structure_id",
        "turn_count",
        "builder_fallback_count",
        "builder_fallback_rate",
        "error_type",
        "error_message",
    ]
    with csv_output.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for run in report["runs"]:
            for row in run["structure_rows"]:
                writer.writerow({field: row.get(field, "") for field in fieldnames})


def _summarize_run(run_name: str, *, result_root: Path) -> dict:
    run_dir = result_root / run_name / "normalized"
    summary = _read_json(run_dir / "summary.json", default={})
    status = summary.get("status", "ok")
    failure = summary.get("failure", {}) if isinstance(summary.get("failure"), dict) else {}
    condition_label = _condition_label(run_name)
    if status == "failed":
        rows = [{
            "run_name": run_name,
            "condition_label": condition_label,
            "status": status,
            "structure_id": "",
            "turn_count": 0,
            "builder_fallback_count": 0,
            "builder_fallback_rate": 0.0,
            "error_type": failure.get("type", ""),
            "error_message": failure.get("message", ""),
        }]
    else:
        rows = _structure_rows(run_name, condition_label=condition_label, turns_path=run_dir / "turns.jsonl")
    return {
        "run_name": run_name,
        "condition_label": condition_label,
        "status": status,
        "error_type": failure.get("type", ""),
        "error_message": failure.get("message", ""),
        "structure_rows": rows,
    }


def _structure_rows(run_name: str, *, condition_label: str, turns_path: Path) -> list[dict]:
    by_structure: dict[str, list[dict]] = {}
    for turn in _read_jsonl(turns_path):
        structure_id = str(turn.get("structure_id", ""))
        by_structure.setdefault(structure_id, []).append(turn)
    rows = []
    for structure_id, turns in sorted(by_structure.items()):
        fallback_count = sum(
            1 for turn in turns if (turn.get("builder_action") or {}).get("_builder_fallback")
        )
        rows.append({
            "run_name": run_name,
            "condition_label": condition_label,
            "status": "ok",
            "structure_id": structure_id,
            "turn_count": len(turns),
            "builder_fallback_count": fallback_count,
            "builder_fallback_rate": fallback_count / len(turns) if turns else 0.0,
            "error_type": "",
            "error_message": "",
        })
    return rows


def _group_rows(runs: list[dict], *, key: str) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for run in runs:
        for row in run["structure_rows"]:
            group_key = row["condition_label"]
            if key == "condition_structure":
                group_key = f"{row['condition_label']}|structure={row['structure_id']}"
            groups.setdefault(group_key, []).append(row)
    summary_rows = []
    for group_key, rows in sorted(groups.items()):
        turn_count = sum(int(row.get("turn_count", 0)) for row in rows)
        fallback_count = sum(int(row.get("builder_fallback_count", 0)) for row in rows)
        failed_run_count = sum(1 for row in rows if row.get("status") == "failed")
        summary_rows.append({
            "group": group_key,
            "turn_count": turn_count,
            "builder_fallback_count": fallback_count,
            "builder_fallback_rate": fallback_count / turn_count if turn_count else 0.0,
            "failed_run_count": failed_run_count,
        })
    return summary_rows


def _condition_label(run_name: str) -> str:
    if "dual_dag" in run_name:
        return "dual_dag"
    if "baseline" in run_name:
        return "baseline"
    return "unknown"


def _read_json(path: Path, *, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize CRAFT Builder fallback rates by run and structure.")
    parser.add_argument("--runs", nargs="+", required=True)
    parser.add_argument("--result-root", default="result/craft")
    parser.add_argument("--output", required=True)
    parser.add_argument("--csv-output", default=None)
    args = parser.parse_args(argv)

    report = build_fallback_monitor_report(args.runs, result_root=Path(args.result_root))
    write_fallback_monitor_report(
        report,
        Path(args.output),
        csv_output=Path(args.csv_output) if args.csv_output else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
