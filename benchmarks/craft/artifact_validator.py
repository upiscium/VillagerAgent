import argparse
import csv
import json
from pathlib import Path

from benchmarks.craft.config import repo_root
from benchmarks.craft.dual_dag.schema import DUAL_DAG_SCHEMA_VERSION
from benchmarks.craft.hidden_state_keys import hidden_state_key_labels


REQUIRED_NORMALIZED_ARTIFACTS = [
    "summary.json",
    "metrics.csv",
    "turns.jsonl",
    "dual_dag_summary.json",
    "dual_dag_nodes.jsonl",
    "dual_dag_edges.jsonl",
    "leakage_report.json",
]


class ArtifactValidationError(ValueError):
    """Raised when CRAFT experiment artifacts fail validation."""


def validate_runs(run_names: list[str], *, result_root: Path) -> dict:
    if not run_names:
        raise ArtifactValidationError("At least one CRAFT run is required.")
    runs = [_validate_run(run_name, result_root=result_root) for run_name in run_names]
    return {
        "passed": all(run["passed"] for run in runs),
        "schema_version": DUAL_DAG_SCHEMA_VERSION,
        "runs": runs,
    }


def write_validation_json(report: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


def write_validation_csv(report: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "run_name",
        "passed",
        "missing_artifacts",
        "summary_present",
        "metrics_present",
        "schema_version_present",
        "schema_version_matches",
        "leakage_report_passed",
        "hidden_state_keys_absent",
        "hidden_state_key_hits",
    ]
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for run in report.get("runs", []):
            checks = run.get("checks", {})
            writer.writerow({
                "run_name": run.get("run_name", ""),
                "passed": run.get("passed", False),
                "missing_artifacts": ",".join(run.get("missing_artifacts", [])),
                "summary_present": checks.get("summary_present", False),
                "metrics_present": checks.get("metrics_present", False),
                "schema_version_present": checks.get("schema_version_present", False),
                "schema_version_matches": checks.get("schema_version_matches", False),
                "leakage_report_passed": checks.get("leakage_report_passed", False),
                "hidden_state_keys_absent": checks.get("hidden_state_keys_absent", False),
                "hidden_state_key_hits": json.dumps(run.get("hidden_state_key_hits", []), sort_keys=True),
            })


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate normalized CRAFT experiment artifacts.")
    parser.add_argument("--runs", nargs="+", required=True)
    parser.add_argument("--result-root", default="result/craft")
    parser.add_argument("--output", default=None)
    parser.add_argument("--csv-output", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = repo_root()
    result_root = _resolve_path(root, args.result_root)
    report = validate_runs(args.runs, result_root=result_root)
    if args.output:
        write_validation_json(report, _resolve_path(root, args.output))
    if args.csv_output:
        write_validation_csv(report, _resolve_path(root, args.csv_output))
    print(f"Validated {len(report['runs'])} CRAFT run artifact set(s): passed={report['passed']}")
    if not report["passed"]:
        raise SystemExit(1)


def _validate_run(run_name: str, *, result_root: Path) -> dict:
    normalized_dir = result_root / run_name / "normalized"
    missing_artifacts = [
        name for name in REQUIRED_NORMALIZED_ARTIFACTS
        if not (normalized_dir / name).exists()
    ]
    summary = _read_json_if_exists(normalized_dir / "summary.json")
    dag_summary = _read_json_if_exists(normalized_dir / "dual_dag_summary.json")
    leakage_report = _read_json_if_exists(normalized_dir / "leakage_report.json")
    hidden_key_hits = _hidden_key_hits(normalized_dir)
    checks = {
        "required_artifacts_present": not missing_artifacts,
        "summary_present": bool(summary),
        "metrics_present": (normalized_dir / "metrics.csv").exists(),
        "schema_version_present": bool(dag_summary.get("schema_version")),
        "schema_version_matches": dag_summary.get("schema_version") == DUAL_DAG_SCHEMA_VERSION,
        "leakage_report_passed": all(
            check.get("passed", True)
            for check in leakage_report.get("checks", [])
        ) if leakage_report else False,
        "hidden_state_keys_absent": not hidden_key_hits,
    }
    return {
        "run_name": run_name,
        "passed": all(checks.values()),
        "normalized_dir": str(normalized_dir),
        "checks": checks,
        "missing_artifacts": missing_artifacts,
        "schema_version": dag_summary.get("schema_version", ""),
        "hidden_state_key_hits": hidden_key_hits,
    }


def _read_json_if_exists(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _hidden_key_hits(normalized_dir: Path) -> list[dict]:
    hits = []
    for artifact in REQUIRED_NORMALIZED_ARTIFACTS:
        path = normalized_dir / artifact
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for key in hidden_state_key_labels():
            if key in text:
                hits.append({"file": path.name, "key": key})
    return hits


def _resolve_path(root: Path, path: str) -> Path:
    resolved = Path(path)
    if resolved.is_absolute():
        return resolved
    return root / resolved


if __name__ == "__main__":
    main()
