import json

from benchmarks.craft.result_converter import normalize_results


def test_result_converter_writes_normalized_files(tmp_path):
    config = {
        "run": {"name": "test", "seed": 3, "structures": [0], "turns": 1},
        "models": {"director": {"model": "d"}, "builder": {"model": "b"}},
        "villageragent": {"enabled": True},
    }
    normalize_results(
        config=config,
        condition="villageragent_directors",
        raw_result={"structure_id": 0, "turns": [], "final_progress": 0.0, "completed": False},
        output_dir=tmp_path,
    )
    assert (tmp_path / "normalized" / "summary.json").exists()
    assert (tmp_path / "normalized" / "turns.jsonl").exists()
    assert (tmp_path / "normalized" / "metrics.csv").exists()
    summary = json.loads((tmp_path / "normalized" / "summary.json").read_text())
    assert summary["benchmark"] == "CRAFT"
    assert summary["runtime"]["active_directors"] == ["D1", "D2", "D3"]
    assert summary["runtime"]["observed_fact_count"] == 0
    assert summary["runtime"]["reported_claim_count"] == 0


def test_result_converter_records_runtime_metrics(tmp_path):
    config = {
        "run": {"name": "test", "seed": 3, "structures": [0], "turns": 1},
        "models": {
            "director": {"model": "d", "provider": "ollama_native"},
            "builder": {"model": "b", "provider": "ollama_native"},
        },
        "villageragent": {"enabled": False},
    }
    normalize_results(
        config=config,
        condition="single_director_ablation",
        raw_result={
            "structure_id": 0,
            "turns": [{"builder_action": {"_builder_fallback": "fallback"}}],
            "final_progress": 0.0,
            "completed": False,
        },
        output_dir=tmp_path,
    )
    summary = json.loads((tmp_path / "normalized" / "summary.json").read_text())
    assert summary["providers"] == {"director": "ollama_native", "builder": "ollama_native"}
    assert summary["runtime"]["active_directors"] == ["D1"]
    assert summary["runtime"]["builder_fallback_count"] == 1
    assert summary["runtime"]["builder_fallback_rate"] == 1.0
    assert summary["runtime"]["mean_action_confidence"] == 0.0


def test_result_converter_writes_metrics_for_each_game(tmp_path):
    config = {
        "run": {"name": "test", "seed": 3, "structures": [0, 1], "turns": 1},
        "models": {"director": {"model": "d"}, "builder": {"model": "b"}},
        "villageragent": {"enabled": True},
    }
    normalize_results(
        config=config,
        condition="villageragent_directors",
        raw_result={
            "turns": [],
            "games": [
                {"structure_id": 0, "final_progress": 0.25, "completed": False},
                {"structure_id": 1, "final_progress": 0.75, "completed": True},
            ],
        },
        output_dir=tmp_path,
    )
    summary = json.loads((tmp_path / "normalized" / "summary.json").read_text())
    metrics_lines = (tmp_path / "normalized" / "metrics.csv").read_text().splitlines()
    assert summary["num_games"] == 2
    assert summary["mean_final_progress"] == 0.5
    assert summary["completion_rate"] == 0.5
    assert len(metrics_lines) == 3


def test_result_converter_counts_epistemic_metadata(tmp_path):
    config = {
        "run": {"name": "test", "seed": 3, "structures": [0], "turns": 1},
        "models": {"director": {"model": "d"}, "builder": {"model": "b"}},
        "villageragent": {"enabled": True},
    }
    normalize_results(
        config=config,
        condition="villageragent_directors",
        raw_result={
            "structure_id": 0,
            "turns": [{
                "director_metadata": {
                    "D1": {"epistemic": {"observed_facts": [{"node_id": "o1"}], "hypotheses": []}},
                    "D2": {"epistemic": {"observed_facts": [{"node_id": "o2"}], "hypotheses": [{"node_id": "h1"}]}},
                },
                "epistemic_claims": {"D1": {"node_id": "c1"}, "D2": {"node_id": "c2"}},
                "builder_action": {"action": "clarify"},
            }],
            "final_progress": 0.0,
            "completed": False,
        },
        output_dir=tmp_path,
    )
    summary = json.loads((tmp_path / "normalized" / "summary.json").read_text())
    metrics_text = (tmp_path / "normalized" / "metrics.csv").read_text()
    assert summary["runtime"]["observed_fact_count"] == 2
    assert summary["runtime"]["reported_claim_count"] == 2
    assert summary["runtime"]["hypothesis_count"] == 1
    assert "observed_fact_count" in metrics_text


def test_result_converter_counts_action_candidate_metadata(tmp_path):
    config = {
        "run": {"name": "test", "seed": 3, "structures": [0], "turns": 1},
        "models": {"director": {"model": "d"}, "builder": {"model": "b"}},
        "villageragent": {"enabled": True},
    }
    normalize_results(
        config=config,
        condition="villageragent_directors",
        raw_result={
            "structure_id": 0,
            "turns": [{
                "builder_action": {
                    "action": "place",
                    "_action_candidate_metadata": {
                        "candidate_count": 2,
                        "chosen_confidence": 0.75,
                        "claim_support_count": 1,
                        "claim_conflict_count": 1,
                    },
                },
            }],
            "final_progress": 0.0,
            "completed": False,
        },
        output_dir=tmp_path,
    )
    summary = json.loads((tmp_path / "normalized" / "summary.json").read_text())
    metrics_text = (tmp_path / "normalized" / "metrics.csv").read_text()
    assert summary["runtime"]["mean_action_confidence"] == 0.75
    assert summary["runtime"]["claim_support_count"] == 1
    assert summary["runtime"]["claim_conflict_count"] == 1
    assert summary["runtime"]["candidate_count"] == 2
    assert "mean_action_confidence" in metrics_text


def test_result_converter_counts_gated_clarification_metadata(tmp_path):
    config = {
        "run": {"name": "test", "seed": 3, "structures": [0], "turns": 1},
        "models": {"director": {"model": "d"}, "builder": {"model": "b"}},
        "villageragent": {"enabled": True},
    }
    normalize_results(
        config=config,
        condition="villageragent_directors",
        raw_result={
            "structure_id": 0,
            "turns": [{
                "builder_action": {
                    "action": "clarify",
                    "_gated_clarification": {
                        "risk_score": 0.4,
                        "reasons": ["low_action_confidence", "claim_conflict"],
                    },
                },
            }],
            "final_progress": 0.0,
            "completed": False,
        },
        output_dir=tmp_path,
    )
    summary = json.loads((tmp_path / "normalized" / "summary.json").read_text())
    metrics_text = (tmp_path / "normalized" / "metrics.csv").read_text()
    assert summary["runtime"]["clarification_count"] == 1
    assert summary["runtime"]["gated_clarification_count"] == 1
    assert summary["runtime"]["gated_clarification_rate"] == 1.0
    assert summary["runtime"]["mean_risk_score"] == 0.4
    assert summary["runtime"]["low_confidence_gate_count"] == 1
    assert summary["runtime"]["conflict_gate_count"] == 1
    assert "gated_clarification_count" in metrics_text


def test_result_converter_writes_dual_dag_artifacts(tmp_path):
    config = {
        "run": {"name": "test", "seed": 3, "structures": [0], "turns": 1},
        "models": {"director": {"model": "d"}, "builder": {"model": "b"}},
        "villageragent": {"enabled": True},
    }
    normalize_results(
        config=config,
        condition="villageragent_directors",
        raw_result={
            "structure_id": 0,
            "turns": [],
            "dual_dag": {
                "summary": {"epistemic_node_count": 1},
                "epistemic_nodes": [{"node_id": "claim:D1:1", "node_type": "reported_claim"}],
                "epistemic_edges": [],
                "action_nodes": [{"node_id": "action:1:0"}],
                "action_edges": [{"source_id": "claim:D1:1", "target_id": "action:1:0"}],
            },
            "final_progress": 0.0,
            "completed": False,
        },
        output_dir=tmp_path,
    )

    summary = json.loads((tmp_path / "normalized" / "summary.json").read_text())
    dag_summary = json.loads((tmp_path / "normalized" / "dual_dag_summary.json").read_text())
    nodes_text = (tmp_path / "normalized" / "dual_dag_nodes.jsonl").read_text()
    edges_text = (tmp_path / "normalized" / "dual_dag_edges.jsonl").read_text()
    assert summary["runtime"]["dual_dag_node_count"] == 2
    assert summary["runtime"]["dual_dag_edge_count"] == 1
    assert dag_summary["node_count"] == 2
    assert "claim:D1:1" in nodes_text
    assert "action:1:0" in edges_text
