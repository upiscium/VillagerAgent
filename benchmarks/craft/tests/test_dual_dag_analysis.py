import csv
import json

import pytest

from benchmarks.craft.dual_dag.analysis import (
    DualDAGAnalysisError,
    analyze_run,
    analyze_runs,
    write_json_analysis,
    write_turn_csv,
)


def test_dual_dag_analysis_summarizes_claim_edges_and_turns(tmp_path):
    _write_normalized_run(tmp_path, "craft_dual_dag")

    analysis = analyze_run("craft_dual_dag", result_root=tmp_path)

    assert analysis["node_count"] == 6
    assert analysis["edge_count"] == 4
    assert analysis["node_type_counts"] == {
        "reported_claim": 2,
        "hypothesis": 1,
        "resolved_fact": 1,
        "place_block": 1,
        "wait_for_evidence": 1,
    }
    assert analysis["edge_type_counts"] == {"supports": 1, "conflicts_with": 2, "derived_from": 1}
    assert analysis["epistemic_edge_type_counts"] == {"derived_from": 1, "conflicts_with": 1}
    assert analysis["action_edge_type_counts"] == {"supports": 1, "conflicts_with": 1}
    assert analysis["director_claim_counts"] == {"D1": 1, "D2": 1}
    assert analysis["director_support_counts"] == {"D1": 1}
    assert analysis["director_conflict_counts"] == {"D2": 1}
    assert analysis["director_required_evidence_counts"] == {"D2": 1}
    assert analysis["supported_action_count"] == 1
    assert analysis["conflicted_action_count"] == 1
    assert analysis["required_evidence_action_count"] == 1
    assert analysis["resolved_fact_count"] == 1
    assert analysis["hypothesis_status_counts"] == {"conflicted": 1}
    assert analysis["action_state_counts"] == {"invalidated": 1, "waiting_for_evidence": 1}
    assert analysis["coordination_action_counts"] == {"wait_for_evidence": 1}
    assert analysis["gated_clarification_count"] == 1
    assert analysis["builder_fallback_count"] == 1
    assert analysis["artifact_health"]["passed"] is True
    assert analysis["artifact_health"]["action_candidate_metadata_turn_count"] == 1
    assert analysis["failure_modes"]["fallback"]["count"] == 1
    assert analysis["failure_modes"]["conflict"]["count"] == 1
    assert analysis["failure_modes"]["required_evidence"]["count"] == 1
    assert analysis["failure_modes"]["clarification"]["count"] == 1
    assert analysis["failure_modes"]["gated_clarification"]["count"] == 1
    assert analysis["turns"][0]["gate_reasons"] == "claim_conflict"


def test_dual_dag_analysis_writes_json_and_turn_csv(tmp_path):
    _write_normalized_run(tmp_path, "craft_dual_dag")
    analysis = analyze_run("craft_dual_dag", result_root=tmp_path)
    json_path = tmp_path / "analysis.json"
    csv_path = tmp_path / "turns.csv"

    write_json_analysis(analysis, json_path)
    write_turn_csv(analysis, csv_path)

    assert json.loads(json_path.read_text(encoding="utf-8"))["run_name"] == "craft_dual_dag"
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["chosen_candidate_id"] == "action:1:0"
    assert rows[0]["claim_required_evidence_count"] == "1"
    assert rows[0]["gated_clarification"] == "True"


def test_dual_dag_analysis_writes_multi_run_turn_csv(tmp_path):
    _write_normalized_run(tmp_path, "run_a")
    _write_normalized_run(tmp_path, "run_b")
    analysis = analyze_runs(["run_a", "run_b"], result_root=tmp_path)
    csv_path = tmp_path / "turns.csv"

    write_turn_csv(analysis, csv_path)

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [row["run_name"] for row in rows] == ["run_a", "run_b"]


def test_dual_dag_analysis_rejects_missing_artifacts(tmp_path):
    with pytest.raises(DualDAGAnalysisError, match="Missing CRAFT Dual-DAG artifact"):
        analyze_run("missing", result_root=tmp_path)


def test_dual_dag_analysis_reports_hidden_state_key_hits(tmp_path):
    _write_normalized_run(tmp_path, "leaky_run")
    turns_path = tmp_path / "leaky_run" / "normalized" / "turns.jsonl"
    _write_jsonl(
        turns_path,
        [
            {
                "builder_action": {
                    "action": "place",
                    "_action_candidate_metadata": {"chosen_confidence": 0.9},
                },
                "debug": {"target_structure": "should not be serialized"},
            },
        ],
    )

    analysis = analyze_run("leaky_run", result_root=tmp_path)

    assert analysis["artifact_health"]["passed"] is False
    assert analysis["artifact_health"]["checks"]["hidden_state_keys_absent"] is False
    assert {hit["key"] for hit in analysis["artifact_health"]["hidden_state_key_hits"]} == {
        "target_structure",
    }


def _write_normalized_run(tmp_path, run_name):
    normalized = tmp_path / run_name / "normalized"
    normalized.mkdir(parents=True)
    (normalized / "summary.json").write_text(
        json.dumps({"run_name": run_name, "condition": "villageragent_directors"}),
        encoding="utf-8",
    )
    (normalized / "dual_dag_summary.json").write_text(
        json.dumps({"node_count": 6, "edge_count": 4}),
        encoding="utf-8",
    )
    _write_jsonl(
        normalized / "dual_dag_nodes.jsonl",
        [
            {
                "node_id": "claim:D1:1",
                "node_type": "reported_claim",
                "content": {"director_id": "D1"},
            },
            {
                "node_id": "claim:D2:1",
                "node_type": "reported_claim",
                "content": {"director_id": "D2"},
            },
            {
                "node_id": "hypothesis:conflicting_evidence:claim:D2:1:action:1:0",
                "node_type": "hypothesis",
                "content": {"source_claim_ids": ["claim:D2:1"], "status": "conflicted"},
            },
            {"node_id": "resolved_fact:1", "node_type": "resolved_fact"},
            {
                "node_id": "action:1:0",
                "node_type": "place_block",
                "state": "invalidated",
                "required_evidence": ["claim:D2:1"],
            },
            {
                "node_id": "coordination:wait_for_evidence:1:0",
                "action_type": "wait_for_evidence",
                "state": "waiting_for_evidence",
            },
        ],
    )
    _write_jsonl(
        normalized / "dual_dag_edges.jsonl",
        [
            {"source_id": "claim:D1:1", "target_id": "action:1:0", "edge_type": "supports", "graph_type": "action"},
            {"source_id": "claim:D2:1", "target_id": "action:1:0", "edge_type": "conflicts_with", "graph_type": "action"},
            {
                "source_id": "claim:D2:1",
                "target_id": "hypothesis:conflicting_evidence:claim:D2:1:action:1:0",
                "edge_type": "derived_from",
                "graph_type": "epistemic",
            },
            {
                "source_id": "hypothesis:conflicting_evidence:claim:D2:1:action:1:0",
                "target_id": "claim:D2:1",
                "edge_type": "conflicts_with",
                "graph_type": "epistemic",
            },
        ],
    )
    _write_jsonl(
        normalized / "turns.jsonl",
        [
            {
                "structure_id": 0,
                "turn_index": 1,
                "builder_action": {
                    "action": "clarify",
                    "_builder_fallback": "oracle_first_candidate_after_non_candidate_response",
                    "_gated_clarification": {"reasons": ["claim_conflict"]},
                    "_action_candidate_metadata": {
                        "chosen_candidate_id": "action:1:0",
                        "chosen_confidence": 0.5,
                        "claim_support_count": 1,
                        "claim_conflict_count": 1,
                        "claim_required_evidence_count": 1,
                    },
                },
                "move_executed": False,
                "progress": {"progress": 0.25},
            },
        ],
    )
    (normalized / "leakage_report.json").write_text(
        json.dumps({"checks": [{"passed": True, "violations": []}]}),
        encoding="utf-8",
    )


def _write_jsonl(path, rows):
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
