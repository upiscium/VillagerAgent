import csv
import json

import pytest

from benchmarks.craft.experiment_summary import (
    ExperimentSummaryError,
    build_experiment_summary,
    write_summary_csv,
    write_summary_json,
)


def test_experiment_summary_combines_runtime_and_analysis_metrics(tmp_path):
    _write_run(tmp_path, "craft_dual_dag", leakage_values=["True"])

    rows = build_experiment_summary(["craft_dual_dag"], result_root=tmp_path)

    assert rows[0]["run_name"] == "craft_dual_dag"
    assert rows[0]["mean_final_progress"] == 0.25
    assert rows[0]["claim_required_evidence_count"] == 3
    assert rows[0]["supported_action_count"] == 2
    assert rows[0]["required_evidence_action_count"] == 1
    assert rows[0]["leakage_passed"] is True


def test_experiment_summary_accepts_aggregate_analysis_input(tmp_path):
    _write_run(tmp_path, "craft_dual_dag", leakage_values=["True"], write_run_analysis=False)
    analysis_path = tmp_path / "dual_dag_analysis.json"
    analysis_path.write_text(
        json.dumps({
            "runs": [{
                "run_name": "craft_dual_dag",
                "supported_action_count": 4,
                "conflicted_action_count": 0,
                "required_evidence_action_count": 2,
            }],
        }),
        encoding="utf-8",
    )

    rows = build_experiment_summary(
        ["craft_dual_dag"],
        result_root=tmp_path,
        analysis_input=analysis_path,
    )

    assert rows[0]["supported_action_count"] == 4
    assert rows[0]["required_evidence_action_count"] == 2


def test_experiment_summary_writes_csv_and_json(tmp_path):
    _write_run(tmp_path, "craft_dual_dag", leakage_values=["True"])
    rows = build_experiment_summary(["craft_dual_dag"], result_root=tmp_path)
    csv_path = tmp_path / "summary.csv"
    json_path = tmp_path / "summary.json"

    write_summary_csv(rows, csv_path)
    write_summary_json(rows, json_path)

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        csv_rows = list(csv.DictReader(f))
    assert csv_rows[0]["claim_required_evidence_count"] == "3"
    assert json.loads(json_path.read_text(encoding="utf-8"))["runs"][0]["run_name"] == "craft_dual_dag"


def test_experiment_summary_rejects_missing_summary(tmp_path):
    with pytest.raises(ExperimentSummaryError, match="Missing CRAFT summary input"):
        build_experiment_summary(["missing"], result_root=tmp_path)


def test_experiment_summary_marks_leakage_failure(tmp_path):
    _write_run(tmp_path, "craft_bad", leakage_values=["True", "False"])

    rows = build_experiment_summary(["craft_bad"], result_root=tmp_path)

    assert rows[0]["leakage_passed"] is False


def _write_run(tmp_path, run_name, *, leakage_values, write_run_analysis=True):
    normalized = tmp_path / run_name / "normalized"
    normalized.mkdir(parents=True)
    summary = {
        "run_name": run_name,
        "condition": "villageragent_directors",
        "mean_final_progress": 0.25,
        "completion_rate": 0.0,
        "runtime": {
            "builder_fallback_rate": 0.2,
            "gated_clarification_rate": 0.1,
            "mean_action_confidence": 0.8,
            "claim_support_count": 5,
            "claim_conflict_count": 1,
            "claim_required_evidence_count": 3,
            "dual_dag_node_count": 42,
            "dual_dag_edge_count": 6,
        },
    }
    analysis = {
        "supported_action_count": 2,
        "conflicted_action_count": 1,
        "required_evidence_action_count": 1,
    }
    (normalized / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    if write_run_analysis:
        (normalized / "dual_dag_analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
    with (normalized / "metrics.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["leakage_passed"])
        writer.writeheader()
        for value in leakage_values:
            writer.writerow({"leakage_passed": value})
