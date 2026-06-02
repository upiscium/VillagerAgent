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

    assert analysis["node_count"] == 3
    assert analysis["edge_count"] == 2
    assert analysis["node_type_counts"] == {"reported_claim": 2, "place_block": 1}
    assert analysis["edge_type_counts"] == {"supports": 1, "conflicts_with": 1}
    assert analysis["director_claim_counts"] == {"D1": 1, "D2": 1}
    assert analysis["director_support_counts"] == {"D1": 1}
    assert analysis["director_conflict_counts"] == {"D2": 1}
    assert analysis["supported_action_count"] == 1
    assert analysis["conflicted_action_count"] == 1
    assert analysis["gated_clarification_count"] == 1
    assert analysis["builder_fallback_count"] == 1
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


def _write_normalized_run(tmp_path, run_name):
    normalized = tmp_path / run_name / "normalized"
    normalized.mkdir(parents=True)
    (normalized / "summary.json").write_text(
        json.dumps({"run_name": run_name, "condition": "villageragent_directors"}),
        encoding="utf-8",
    )
    (normalized / "dual_dag_summary.json").write_text(
        json.dumps({"node_count": 3, "edge_count": 2}),
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
            {"node_id": "action:1:0", "node_type": "place_block"},
        ],
    )
    _write_jsonl(
        normalized / "dual_dag_edges.jsonl",
        [
            {"source_id": "claim:D1:1", "target_id": "action:1:0", "edge_type": "supports"},
            {"source_id": "claim:D2:1", "target_id": "action:1:0", "edge_type": "conflicts_with"},
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
                    },
                },
                "move_executed": False,
                "progress": {"progress": 0.25},
            },
        ],
    )


def _write_jsonl(path, rows):
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
