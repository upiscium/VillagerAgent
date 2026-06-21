import csv
import json

import pytest

from benchmarks.craft.experiment_summary import (
    ExperimentSummaryError,
    build_experiment_summary,
    build_variance_summary,
    write_summary_csv,
    write_summary_json,
    write_variance_csv,
    write_variance_json,
)


def test_experiment_summary_combines_runtime_and_analysis_metrics(tmp_path):
    _write_run(tmp_path, "craft_dual_dag", leakage_values=["True"], seed=3, structures=[0, 1])

    rows = build_experiment_summary(["craft_dual_dag"], result_root=tmp_path)

    assert rows[0]["run_name"] == "craft_dual_dag"
    assert rows[0]["run_group"] == "craft_dual_dag"
    assert rows[0]["seed"] == 3
    assert rows[0]["structures"] == "0,1"
    assert rows[0]["mean_final_progress"] == 0.25
    assert rows[0]["progress_auc"] == 0.3
    assert rows[0]["physical_action_count"] == 2
    assert rows[0]["mean_progress_delta_per_physical_action"] == 0.2
    assert rows[0]["gate_invocation_count"] == 1
    assert rows[0]["gate_reason_counts"] == '{"low_action_confidence": 1}'
    assert rows[0]["retrieved_node_count"] == 3
    assert rows[0]["mean_retrieved_node_age"] == 2.0
    assert rows[0]["clarification_to_unlock_rate"] == 0.5
    assert rows[0]["clarification_resolution_rate"] == 0.5
    assert rows[0]["mean_clarification_quality_score"] == 0.75
    assert rows[0]["claim_required_evidence_count"] == 3
    assert rows[0]["resolved_fact_count"] == 2
    assert rows[0]["hypothesis_resolved_count"] == 1
    assert rows[0]["action_candidate_executed_count"] == 1
    assert rows[0]["candidate_created_count"] == 3
    assert rows[0]["candidate_state_transition_counts"] == '{"executes_action:executed": 1}'
    assert rows[0]["coordination_action_count"] == 2
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
    assert csv_rows[0]["action_candidate_executed_count"] == "1"
    assert json.loads(json_path.read_text(encoding="utf-8"))["runs"][0]["run_name"] == "craft_dual_dag"


def test_experiment_summary_rejects_missing_summary(tmp_path):
    with pytest.raises(ExperimentSummaryError, match="Missing CRAFT summary input"):
        build_experiment_summary(["missing"], result_root=tmp_path)


def test_experiment_summary_marks_leakage_failure(tmp_path):
    _write_run(tmp_path, "craft_bad", leakage_values=["True", "False"])

    rows = build_experiment_summary(["craft_bad"], result_root=tmp_path)

    assert rows[0]["leakage_passed"] is False


def test_experiment_summary_includes_failed_run_status(tmp_path):
    _write_failed_run(tmp_path, "craft_failed")

    rows = build_experiment_summary(["craft_failed"], result_root=tmp_path)

    assert rows[0]["status"] == "failed"
    assert rows[0]["error_type"] == "RuntimeError"
    assert rows[0]["error_message"] == "model unavailable"
    assert rows[0]["leakage_passed"] is False


def test_experiment_summary_derives_progress_action_metrics_from_turns_jsonl(tmp_path):
    normalized = tmp_path / "craft_old_artifact" / "normalized"
    normalized.mkdir(parents=True)
    summary = {
        "run_name": "craft_old_artifact",
        "condition": "villageragent_directors",
        "seed": 3,
        "structures": [0],
        "mean_final_progress": 0.4,
        "completion_rate": 0.0,
        "runtime": {},
    }
    (normalized / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    with (normalized / "turns.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps({"builder_action": {"action": "place"}, "progress": {"overall_progress": 0.2}}) + "\n")
        f.write(json.dumps({"builder_action": {"action": "remove"}, "progress": {"overall_progress": 0.4}}) + "\n")
    with (normalized / "metrics.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["leakage_passed"])
        writer.writeheader()
        writer.writerow({"leakage_passed": "True"})

    rows = build_experiment_summary(["craft_old_artifact"], result_root=tmp_path)

    assert rows[0]["progress_auc"] == 0.30000000000000004
    assert rows[0]["physical_action_count"] == 2
    assert rows[0]["mean_progress_delta_per_physical_action"] == 0.2


def test_variance_summary_groups_completed_runs_and_records_failures(tmp_path):
    _write_run(tmp_path, "craft_dual_dag_seed1", leakage_values=["True"], seed=1, progress=0.2)
    _write_run(tmp_path, "craft_dual_dag_seed3", leakage_values=["True"], seed=3, progress=0.4)
    _write_failed_run(tmp_path, "craft_dual_dag_seed5", seed=5)
    rows = build_experiment_summary(
        ["craft_dual_dag_seed1", "craft_dual_dag_seed3", "craft_dual_dag_seed5"],
        result_root=tmp_path,
    )

    variance_rows = build_variance_summary(rows)

    assert variance_rows[0]["group"] == "craft_dual_dag"
    assert variance_rows[0]["run_count"] == 3
    assert variance_rows[0]["completed_run_count"] == 2
    assert variance_rows[0]["failed_run_count"] == 1
    assert variance_rows[0]["seed_count"] == 3
    assert variance_rows[0]["structures"] == "0,1,2"
    assert variance_rows[0]["mean_final_progress_mean"] == 0.30000000000000004
    assert variance_rows[0]["mean_final_progress_stddev"] == 0.1


def test_variance_summary_writes_csv_and_json(tmp_path):
    rows = [{
        "condition": "villageragent_directors",
        "status": "completed",
        "seed": 1,
        "structures": "0,1",
        "mean_final_progress": 0.5,
        "completion_rate": 0.25,
    }]
    variance_rows = build_variance_summary(rows)
    csv_path = tmp_path / "variance.csv"
    json_path = tmp_path / "variance.json"

    write_variance_csv(variance_rows, csv_path)
    write_variance_json(variance_rows, json_path)

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        csv_rows = list(csv.DictReader(f))
    assert csv_rows[0]["group"] == "villageragent_directors"
    assert json.loads(json_path.read_text(encoding="utf-8"))["groups"][0]["seed_count"] == 1


def _write_run(
    tmp_path,
    run_name,
    *,
    leakage_values,
    write_run_analysis=True,
    seed=3,
    structures=None,
    progress=0.25,
):
    structures = structures or [0, 1, 2]
    normalized = tmp_path / run_name / "normalized"
    normalized.mkdir(parents=True)
    summary = {
        "run_name": run_name,
        "condition": "villageragent_directors",
        "seed": seed,
        "structures": structures,
        "mean_final_progress": progress,
        "completion_rate": 0.0,
        "runtime": {
            "builder_fallback_rate": 0.2,
            "max_progress": 0.4,
            "progress_auc": 0.3,
            "physical_action_count": 2,
            "place_action_count": 1,
            "remove_action_count": 1,
            "clarify_count": 1,
            "wait_count": 0,
            "fallback_count": 0,
            "no_op_count": 0,
            "invalid_action_count": 0,
            "positive_progress_turn_count": 2,
            "zero_progress_turn_count": 1,
            "negative_progress_turn_count": 0,
            "mean_progress_delta_per_turn": 0.1,
            "mean_progress_delta_per_physical_action": 0.2,
            "unique_clarification_count": 1,
            "repeated_clarification_count": 0,
            "clarification_response_count": 1,
            "clarification_to_unlock_count": 1,
            "clarification_to_unlock_rate": 0.5,
            "clarification_to_positive_action_count": 1,
            "clarification_to_positive_action_latency": 1.0,
            "clarification_without_state_change_count": 0,
            "gate_invocation_count": 1,
            "gate_allow_count": 0,
            "gate_block_count": 1,
            "gate_clarify_count": 1,
            "gate_wait_count": 0,
            "gate_reason_counts": '{"low_action_confidence": 1}',
            "retrieved_node_count": 3,
            "retrieved_claim_count": 2,
            "retrieved_action_count": 1,
            "mean_retrieved_node_age": 2.0,
            "max_retrieved_node_age": 4,
            "retrieved_executed_candidate_count": 1,
            "retrieved_invalidated_candidate_count": 0,
            "retrieved_superseded_node_count": 0,
            "retrieval_used_in_top_action_count": 1,
            "retrieval_changed_top_action_count": 0,
            "gated_clarification_rate": 0.1,
            "clarification_resolution_rate": 0.5,
            "mean_clarification_quality_score": 0.75,
            "mean_post_clarification_progress_delta": 0.2,
            "mean_action_confidence": 0.8,
            "claim_support_count": 5,
            "claim_conflict_count": 1,
            "claim_required_evidence_count": 3,
            "dual_dag_node_count": 42,
            "dual_dag_edge_count": 6,
            "resolved_fact_count": 2,
            "hypothesis_open_count": 0,
            "hypothesis_supported_count": 1,
            "hypothesis_conflicted_count": 0,
            "hypothesis_resolved_count": 1,
            "hypothesis_invalidated_count": 0,
            "action_candidate_candidate_count": 0,
            "action_candidate_executable_count": 2,
            "action_candidate_waiting_for_evidence_count": 1,
            "action_candidate_blocked_count": 0,
            "action_candidate_invalidated_count": 0,
            "action_candidate_executed_count": 1,
            "candidate_created_count": 3,
            "candidate_blocked_count": 0,
            "candidate_executable_count": 2,
            "candidate_executed_count": 1,
            "candidate_invalidated_count": 0,
            "candidate_repeated_after_execution_count": 1,
            "candidate_state_transition_counts": '{"executes_action:executed": 1}',
            "coordination_action_count": 2,
            "clarify_coordination_action_count": 1,
            "wait_for_evidence_coordination_action_count": 1,
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


def _write_failed_run(tmp_path, run_name, *, seed=3):
    normalized = tmp_path / run_name / "normalized"
    normalized.mkdir(parents=True)
    summary = {
        "run_name": run_name,
        "status": "failed",
        "condition": "villageragent_directors",
        "seed": seed,
        "structures": [0, 1, 2],
        "failure": {"type": "RuntimeError", "message": "model unavailable"},
        "runtime": {"status": "failed"},
    }
    (normalized / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    with (normalized / "metrics.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["leakage_passed"])
        writer.writeheader()
