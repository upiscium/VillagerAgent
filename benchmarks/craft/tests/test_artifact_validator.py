import csv
import json

from benchmarks.craft.artifact_validator import (
    validate_runs,
    write_validation_csv,
    write_validation_json,
)


def test_artifact_validator_accepts_complete_safe_run(tmp_path):
    _write_normalized_run(tmp_path, "safe_run")

    report = validate_runs(["safe_run"], result_root=tmp_path)

    assert report["passed"] is True
    assert report["runs"][0]["checks"]["schema_version_matches"] is True
    assert report["runs"][0]["hidden_state_key_hits"] == []


def test_artifact_validator_reports_hidden_key_hits_and_writes_outputs(tmp_path):
    _write_normalized_run(tmp_path, "leaky_run")
    turns_path = tmp_path / "leaky_run" / "normalized" / "turns.jsonl"
    turns_path.write_text('{"target_structure": "hidden"}\n', encoding="utf-8")
    report = validate_runs(["leaky_run"], result_root=tmp_path)
    json_path = tmp_path / "validation.json"
    csv_path = tmp_path / "validation.csv"

    write_validation_json(report, json_path)
    write_validation_csv(report, csv_path)

    assert report["passed"] is False
    assert report["runs"][0]["checks"]["hidden_state_keys_absent"] is False
    assert report["runs"][0]["hidden_state_key_hits"] == [
        {"file": "turns.jsonl", "key": "target_structure"}
    ]
    assert json.loads(json_path.read_text(encoding="utf-8"))["passed"] is False
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["hidden_state_keys_absent"] == "False"


def _write_normalized_run(tmp_path, run_name):
    normalized = tmp_path / run_name / "normalized"
    normalized.mkdir(parents=True)
    (normalized / "summary.json").write_text(
        json.dumps({"run_name": run_name}),
        encoding="utf-8",
    )
    (normalized / "metrics.csv").write_text("run_name\n" + run_name + "\n", encoding="utf-8")
    (normalized / "turns.jsonl").write_text(
        json.dumps({"builder_action": {"_action_candidate_metadata": {"candidate_count": 1}}}) + "\n",
        encoding="utf-8",
    )
    (normalized / "dual_dag_summary.json").write_text(
        json.dumps({"schema_version": "1.0.0", "node_count": 0, "edge_count": 0}),
        encoding="utf-8",
    )
    (normalized / "dual_dag_nodes.jsonl").write_text("", encoding="utf-8")
    (normalized / "dual_dag_edges.jsonl").write_text("", encoding="utf-8")
    (normalized / "leakage_report.json").write_text(
        json.dumps({"checks": [{"passed": True}]}),
        encoding="utf-8",
    )
