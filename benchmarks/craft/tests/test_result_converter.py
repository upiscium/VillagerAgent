import csv
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


def test_result_converter_counts_dual_dag_hypothesis_nodes(tmp_path):
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
                "epistemic_nodes": [{"node_id": "hypothesis:1", "node_type": "hypothesis"}],
                "action_nodes": [],
                "epistemic_edges": [],
                "action_edges": [],
            },
            "final_progress": 0.0,
            "completed": False,
        },
        output_dir=tmp_path,
    )

    summary = json.loads((tmp_path / "normalized" / "summary.json").read_text())
    metrics_text = (tmp_path / "normalized" / "metrics.csv").read_text()
    assert summary["runtime"]["hypothesis_count"] == 1
    assert ",1," in metrics_text


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
                        "claim_required_evidence_count": 1,
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
    assert summary["runtime"]["claim_required_evidence_count"] == 1
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
                        "reasons": ["low_action_confidence", "claim_conflict", "required_evidence"],
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
    assert summary["runtime"]["gate_invocation_count"] == 1
    assert summary["runtime"]["gate_block_count"] == 1
    assert summary["runtime"]["gate_clarify_count"] == 1
    assert summary["runtime"]["gate_reason_counts"] == '{"claim_conflict": 1, "low_action_confidence": 1, "required_evidence": 1}'
    assert summary["runtime"]["gated_clarification_rate"] == 1.0
    assert summary["runtime"]["clarification_resolution_count"] == 0
    assert summary["runtime"]["clarification_resolution_rate"] == 0.0
    assert summary["runtime"]["mean_clarification_quality_score"] == 0.5
    assert summary["runtime"]["mean_risk_score"] == 0.4
    assert summary["runtime"]["low_confidence_gate_count"] == 1
    assert summary["runtime"]["conflict_gate_count"] == 1
    assert summary["runtime"]["required_evidence_gate_count"] == 1
    assert "gated_clarification_count" in metrics_text


def test_result_converter_tracks_clarification_resolution_and_progress_delta(tmp_path):
    config = {
        "run": {"name": "test", "seed": 3, "structures": [0], "turns": 2},
        "models": {"director": {"model": "d"}, "builder": {"model": "b"}},
        "villageragent": {"enabled": True},
    }
    normalize_results(
        config=config,
        condition="villageragent_directors",
        raw_result={
            "structure_id": 0,
            "turns": [
                {
                    "builder_action": {
                        "action": "clarify",
                        "clarification": "Please clarify the missing color evidence?",
                        "_gated_clarification": {
                            "risk_score": 0.5,
                            "reasons": ["required_evidence"],
                            "chosen_confidence": 0.3,
                            "claim_required_evidence_count": 2,
                        },
                        "_action_candidate_metadata": {
                            "claim_required_evidence_count": 2,
                            "public_evidence_summary": [{"candidate_id": "action:1:0"}],
                        },
                    },
                    "progress": {"overall_progress": 0.1},
                },
                {
                    "builder_action": {
                        "action": "place",
                        "_action_candidate_metadata": {
                            "chosen_confidence": 0.8,
                            "claim_required_evidence_count": 0,
                        },
                    },
                    "progress": {"overall_progress": 0.4},
                },
            ],
            "final_progress": 0.4,
            "completed": False,
        },
        output_dir=tmp_path,
    )

    summary = json.loads((tmp_path / "normalized" / "summary.json").read_text())
    metrics_text = (tmp_path / "normalized" / "metrics.csv").read_text()
    assert summary["runtime"]["clarification_resolution_count"] == 1
    assert summary["runtime"]["clarification_resolution_rate"] == 1.0
    assert summary["runtime"]["mean_clarification_quality_score"] == 1.0
    assert summary["runtime"]["mean_post_clarification_progress_delta"] == 0.30000000000000004
    assert "clarification_resolution_rate" in metrics_text


def test_result_converter_tracks_duplicate_clarifications_and_positive_latency(tmp_path):
    config = {
        "run": {"name": "test", "seed": 3, "structures": [0], "turns": 3},
        "models": {"director": {"model": "d"}, "builder": {"model": "b"}},
        "villageragent": {"enabled": True},
    }
    clarify_action = {
        "action": "clarify",
        "clarification": "Please clarify the missing span?",
        "_gated_clarification": {
            "risk_score": 0.5,
            "reasons": ["large_block_span_uncertainty"],
        },
        "_action_candidate_metadata": {
            "chosen_candidate_id": "action:1:0",
            "chosen_confidence": 0.4,
            "candidates": [{
                "node_id": "action:1:0",
                "action": {"action": "place", "block": "yl", "position": "(0,0)", "layer": 0},
            }],
        },
    }
    normalize_results(
        config=config,
        condition="villageragent_directors",
        raw_result={
            "structure_id": 0,
            "turns": [
                {
                    "builder_action": clarify_action,
                    "director_responses": {"D1": {"public_message": "It spans right."}},
                    "progress": {"overall_progress": 0.1},
                },
                {"builder_action": dict(clarify_action), "progress": {"overall_progress": 0.1}},
                {
                    "builder_action": {
                        "action": "place",
                        "_action_candidate_metadata": {"chosen_confidence": 0.9},
                    },
                    "progress": {"overall_progress": 0.4},
                },
            ],
            "final_progress": 0.4,
            "completed": False,
        },
        output_dir=tmp_path,
    )

    summary = json.loads((tmp_path / "normalized" / "summary.json").read_text())
    assert summary["runtime"]["clarification_count"] == 2
    assert summary["runtime"]["unique_clarification_count"] == 1
    assert summary["runtime"]["repeated_clarification_count"] == 1
    assert summary["runtime"]["clarification_response_count"] == 1
    assert summary["runtime"]["clarification_to_unlock_count"] == 2
    assert summary["runtime"]["clarification_to_unlock_rate"] == 1.0
    assert summary["runtime"]["clarification_to_positive_action_count"] == 2
    assert summary["runtime"]["clarification_to_positive_action_latency"] == 1.5
    assert summary["runtime"]["clarification_without_state_change_count"] == 0
    assert summary["runtime"]["span_uncertainty_gate_count"] == 2


def test_result_converter_tracks_retrieval_metrics(tmp_path):
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
                "turn_index": 5,
                "builder_action": {
                    "action": "place",
                    "_action_candidate_metadata": {
                        "chosen_candidate_id": "action:5:0",
                        "retrieval_changed_top_action": True,
                        "candidates": [{
                            "node_id": "action:5:0",
                            "graph_context": {
                                "relevant_public_claims": [
                                    {"node_id": "claim:D1:1", "turn_index": 2, "relation": "supports"},
                                    {"node_id": "claim:D2:1", "turn_index": 3, "relation": "conflicts_with", "state": "invalidated"},
                                ],
                                "relevant_public_actions": [
                                    {"node_id": "public:builder_action:1:0", "turn_index": 1},
                                    {"node_id": "public:builder_action:4:0", "turn_index": 4, "state": "superseded", "superseded_by": "public:builder_action:5:0"},
                                ],
                            },
                        }],
                    },
                },
                "progress": {"overall_progress": 0.1},
            }],
            "final_progress": 0.1,
            "completed": False,
        },
        output_dir=tmp_path,
    )

    summary = json.loads((tmp_path / "normalized" / "summary.json").read_text())
    assert summary["runtime"]["retrieved_node_count"] == 4
    assert summary["runtime"]["retrieved_claim_count"] == 2
    assert summary["runtime"]["retrieved_action_count"] == 2
    assert summary["runtime"]["mean_retrieved_node_age"] == 2.5
    assert summary["runtime"]["max_retrieved_node_age"] == 4
    assert summary["runtime"]["retrieved_executed_candidate_count"] == 1
    assert summary["runtime"]["retrieved_invalidated_candidate_count"] == 1
    assert summary["runtime"]["retrieved_superseded_node_count"] == 1
    assert summary["runtime"]["retrieval_used_in_top_action_count"] == 1
    assert summary["runtime"]["retrieval_changed_top_action_count"] == 1


def test_result_converter_tracks_progress_and_action_throughput(tmp_path):
    config = {
        "run": {"name": "test", "seed": 3, "structures": [0], "turns": 4},
        "models": {"director": {"model": "d"}, "builder": {"model": "b"}},
        "villageragent": {"enabled": True},
    }
    normalize_results(
        config=config,
        condition="villageragent_directors",
        raw_result={
            "structure_id": 0,
            "turns": [
                {"builder_action": {"action": "place"}, "progress": {"overall_progress": 0.2}},
                {"builder_action": {"action": "clarify"}, "progress": {"overall_progress": 0.2}},
                {"builder_action": {"action": "remove"}, "progress": {"overall_progress": 0.1}},
                {
                    "builder_action": {
                        "action": "wait_for_evidence",
                        "_builder_fallback": "fallback",
                        "_invalid_action": True,
                    },
                    "progress": {"overall_progress": 0.4},
                },
            ],
            "final_progress": 0.4,
            "completed": False,
        },
        output_dir=tmp_path,
    )

    summary = json.loads((tmp_path / "normalized" / "summary.json").read_text())
    with (tmp_path / "normalized" / "metrics.csv").open("r", encoding="utf-8", newline="") as f:
        metrics = next(csv.DictReader(f))
    assert summary["runtime"]["max_progress"] == 0.4
    assert summary["runtime"]["progress_auc"] == 0.225
    assert summary["runtime"]["physical_action_count"] == 2
    assert summary["runtime"]["place_action_count"] == 1
    assert summary["runtime"]["remove_action_count"] == 1
    assert summary["runtime"]["clarify_count"] == 1
    assert summary["runtime"]["wait_count"] == 1
    assert summary["runtime"]["fallback_count"] == 1
    assert summary["runtime"]["invalid_action_count"] == 1
    assert summary["runtime"]["positive_progress_turn_count"] == 2
    assert summary["runtime"]["zero_progress_turn_count"] == 1
    assert summary["runtime"]["negative_progress_turn_count"] == 1
    assert summary["runtime"]["mean_progress_delta_per_turn"] == 0.1
    assert summary["runtime"]["mean_progress_delta_per_physical_action"] == 0.2
    assert metrics["progress_auc"] == "0.225"
    assert metrics["mean_progress_delta_per_physical_action"] == "0.2"


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
                "epistemic_nodes": [
                    {"node_id": "claim:D1:1", "node_type": "reported_claim"},
                    {"node_id": "resolved_fact:1", "node_type": "resolved_fact"},
                    {"node_id": "hypothesis:1", "node_type": "hypothesis", "content": {"status": "resolved"}},
                ],
                "epistemic_edges": [{"source_id": "claim:D1:1", "target_id": "hypothesis:1"}],
                "action_nodes": [
                    {"node_id": "action:1:0", "state": "executed"},
                    {"node_id": "coordination:clarify:1:0", "action_type": "clarify", "state": "candidate"},
                    {
                        "node_id": "coordination:wait_for_evidence:1:0",
                        "action_type": "wait_for_evidence",
                        "state": "waiting_for_evidence",
                    },
                ],
                "action_edges": [
                    {"source_id": "claim:D1:1", "target_id": "action:1:0"},
                    {"source_id": "coordination:clarify:1:0", "target_id": "action:1:0"},
                ],
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
    assert summary["runtime"]["dual_dag_node_count"] == 6
    assert summary["runtime"]["dual_dag_edge_count"] == 3
    assert summary["runtime"]["resolved_fact_count"] == 1
    assert summary["runtime"]["hypothesis_resolved_count"] == 1
    assert summary["runtime"]["action_candidate_executed_count"] == 1
    assert summary["runtime"]["action_candidate_waiting_for_evidence_count"] == 1
    assert summary["runtime"]["coordination_action_count"] == 2
    assert summary["runtime"]["clarify_coordination_action_count"] == 1
    assert summary["runtime"]["wait_for_evidence_coordination_action_count"] == 1
    assert dag_summary["schema_version"] == "1.0.0"
    assert "resolved_fact" in dag_summary["schema"]["node_types"]
    assert "executes_action" in dag_summary["schema"]["edge_types"]
    assert dag_summary["node_count"] == 6
    assert "claim:D1:1" in nodes_text
    assert "coordination:clarify:1:0" in nodes_text
    assert "coordination:wait_for_evidence:1:0" in nodes_text
    assert "action:1:0" in edges_text
    assert '"graph_type": "epistemic"' in edges_text
    assert '"graph_type": "action"' in edges_text
