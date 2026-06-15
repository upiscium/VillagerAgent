import csv
import json
from pathlib import Path


def normalize_results(*, config: dict, condition: str, raw_result: dict, output_dir: Path) -> None:
    normalized_dir = output_dir / "normalized"
    normalized_dir.mkdir(parents=True, exist_ok=True)

    turns = raw_result.get("turns", [])
    games = raw_result.get("games") or [raw_result]
    final_progress_values = [game.get("final_progress", 0.0) for game in games]
    mean_final_progress = (
        sum(final_progress_values) / len(final_progress_values)
        if final_progress_values else 0.0
    )
    completion_rate = (
        sum(1 for game in games if game.get("completed", False)) / len(games)
        if games else 0.0
    )
    builder_fallback_count = sum(
        1 for turn in turns if (turn.get("builder_action") or {}).get("_builder_fallback")
    )
    active_directors = _active_directors(config, condition)
    epistemic_counts = _epistemic_counts(turns)
    action_candidate_metrics = _action_candidate_metrics(turns)
    clarification_metrics = _clarification_metrics(turns)
    dual_dag_metrics = _dual_dag_metrics(games)
    hypothesis_count = max(
        epistemic_counts["hypothesis_count"],
        dual_dag_metrics.pop("hypothesis_count", 0),
    )
    summary = {
        "benchmark": "CRAFT",
        "condition": condition,
        "run_name": config["run"]["name"],
        "seed": config["run"].get("seed"),
        "structures": config["run"].get("structures"),
        "turns": config["run"].get("turns"),
        "num_games": len(games),
        "mean_final_progress": mean_final_progress,
        "completion_rate": completion_rate,
        "models": {
            "director": config["models"]["director"]["model"],
            "builder": config["models"]["builder"]["model"],
        },
        "providers": {
            "director": config["models"]["director"].get("provider", ""),
            "builder": config["models"]["builder"].get("provider", ""),
        },
        "runtime": {
            "active_directors": active_directors,
            "active_director_count": len(active_directors),
            "builder_fallback_count": builder_fallback_count,
            "builder_fallback_rate": builder_fallback_count / len(turns) if turns else 0.0,
            "baseline_type": _baseline_type(condition, raw_result),
            **epistemic_counts,
            "hypothesis_count": hypothesis_count,
            **action_candidate_metrics,
            **clarification_metrics,
            **dual_dag_metrics,
        },
        "villageragent": {
            "enabled": config.get("villageragent", {}).get("enabled", False),
            "use_task_decomposer": config.get("villageragent", {}).get("use_task_decomposer", False),
            "use_agent_controller": config.get("villageragent", {}).get("use_agent_controller", False),
            "use_state_manager": config.get("villageragent", {}).get("use_state_manager", False),
        },
        "partial_information": {
            "target_structure_exposed": config.get("villageragent", {}).get("expose_target_structure", False),
            "oracle_moves_exposed": config.get("villageragent", {}).get("expose_oracle_moves", False),
            "private_views_shared_raw": config.get("villageragent", {}).get("expose_private_views_to_global_state", False),
        },
    }
    with (normalized_dir / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    with (normalized_dir / "turns.jsonl").open("w", encoding="utf-8") as f:
        for turn in turns:
            f.write(json.dumps(turn, ensure_ascii=False) + "\n")

    metrics_path = normalized_dir / "metrics.csv"
    fieldnames = [
        "run_name",
        "condition",
        "structure_id",
        "seed",
        "turns",
        "completed",
        "final_progress",
        "completion_rate",
        "director_model",
        "builder_model",
        "director_provider",
        "builder_provider",
        "active_directors",
        "active_director_count",
        "builder_fallback_count",
        "builder_fallback_rate",
        "observed_fact_count",
        "reported_claim_count",
        "hypothesis_count",
        "mean_action_confidence",
        "claim_support_count",
        "claim_conflict_count",
        "claim_required_evidence_count",
        "candidate_count",
        "clarification_count",
        "gated_clarification_count",
        "gated_clarification_rate",
        "clarification_resolution_count",
        "clarification_resolution_rate",
        "mean_clarification_quality_score",
        "mean_post_clarification_progress_delta",
        "mean_risk_score",
        "low_confidence_gate_count",
        "conflict_gate_count",
        "dual_dag_node_count",
        "dual_dag_edge_count",
        "baseline_type",
        "use_task_decomposer",
        "use_agent_controller",
        "use_state_manager",
        "leakage_passed",
    ]
    with metrics_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for game in games:
            game_epistemic_counts = _epistemic_counts(game.get("turns", []))
            game_action_candidate_metrics = _action_candidate_metrics(game.get("turns", []))
            game_clarification_metrics = _clarification_metrics(game.get("turns", []))
            game_dual_dag_metrics = _dual_dag_metrics([game])
            game_hypothesis_count = max(
                game_epistemic_counts["hypothesis_count"],
                game_dual_dag_metrics.pop("hypothesis_count", 0),
            )
            writer.writerow({
                "run_name": config["run"]["name"],
                "condition": condition,
                "structure_id": game.get("structure_id"),
                "seed": config["run"].get("seed"),
                "turns": config["run"].get("turns"),
                "completed": game.get("completed", False),
                "final_progress": game.get("final_progress", 0.0),
                "completion_rate": 1.0 if game.get("completed", False) else 0.0,
                "director_model": config["models"]["director"]["model"],
                "builder_model": config["models"]["builder"]["model"],
                "director_provider": config["models"]["director"].get("provider", ""),
                "builder_provider": config["models"]["builder"].get("provider", ""),
                "active_directors": ",".join(active_directors),
                "active_director_count": len(active_directors),
                "builder_fallback_count": sum(
                    1 for turn in game.get("turns", [])
                    if (turn.get("builder_action") or {}).get("_builder_fallback")
                ),
                "builder_fallback_rate": _fallback_rate(game.get("turns", [])),
                "observed_fact_count": game_epistemic_counts["observed_fact_count"],
                "reported_claim_count": game_epistemic_counts["reported_claim_count"],
                "hypothesis_count": game_hypothesis_count,
                "mean_action_confidence": game_action_candidate_metrics["mean_action_confidence"],
                "claim_support_count": game_action_candidate_metrics["claim_support_count"],
                "claim_conflict_count": game_action_candidate_metrics["claim_conflict_count"],
                "claim_required_evidence_count": game_action_candidate_metrics["claim_required_evidence_count"],
                "candidate_count": game_action_candidate_metrics["candidate_count"],
                "clarification_count": game_clarification_metrics["clarification_count"],
                "gated_clarification_count": game_clarification_metrics["gated_clarification_count"],
                "gated_clarification_rate": game_clarification_metrics["gated_clarification_rate"],
                "clarification_resolution_count": game_clarification_metrics["clarification_resolution_count"],
                "clarification_resolution_rate": game_clarification_metrics["clarification_resolution_rate"],
                "mean_clarification_quality_score": game_clarification_metrics["mean_clarification_quality_score"],
                "mean_post_clarification_progress_delta": game_clarification_metrics["mean_post_clarification_progress_delta"],
                "mean_risk_score": game_clarification_metrics["mean_risk_score"],
                "low_confidence_gate_count": game_clarification_metrics["low_confidence_gate_count"],
                "conflict_gate_count": game_clarification_metrics["conflict_gate_count"],
                "dual_dag_node_count": game_dual_dag_metrics["dual_dag_node_count"],
                "dual_dag_edge_count": game_dual_dag_metrics["dual_dag_edge_count"],
                "baseline_type": _baseline_type(condition, raw_result),
                "use_task_decomposer": config.get("villageragent", {}).get("use_task_decomposer", False),
                "use_agent_controller": config.get("villageragent", {}).get("use_agent_controller", False),
                "use_state_manager": config.get("villageragent", {}).get("use_state_manager", False),
                "leakage_passed": game.get("leakage_passed", raw_result.get("leakage_passed", True)),
            })

    leakage_report = raw_result.get("leakage_report", {"checks": []})
    with (normalized_dir / "leakage_report.json").open("w", encoding="utf-8") as f:
        json.dump(leakage_report, f, ensure_ascii=False, indent=2)
    _write_dual_dag_artifacts(normalized_dir=normalized_dir, games=games)


def _active_directors(config: dict, condition: str) -> list[str]:
    if condition == "single_director_ablation":
        return ["D1"]
    villageragent = config.get("villageragent", {})
    if villageragent.get("enabled", False):
        return list(
            villageragent.get("active_director_ids")
            or villageragent.get("director_ids", ["D1", "D2", "D3"])
        )
    return []


def _baseline_type(condition: str, raw_result: dict | None = None) -> str:
    if condition == "official_baseline":
        runner = (raw_result or {}).get("official_craft_runner", {})
        if runner.get("mode") == "external_cli":
            return "full_official_runner"
        return "comparable_artifact"
    return ""


def _fallback_rate(turns: list[dict]) -> float:
    if not turns:
        return 0.0
    fallback_count = sum(
        1 for turn in turns if (turn.get("builder_action") or {}).get("_builder_fallback")
    )
    return fallback_count / len(turns)


def _epistemic_counts(turns: list[dict]) -> dict:
    observed_fact_count = 0
    hypothesis_count = 0
    reported_claim_count = 0
    for turn in turns:
        reported_claim_count += len(turn.get("epistemic_claims", {}))
        for metadata in turn.get("director_metadata", {}).values():
            epistemic = metadata.get("epistemic", {}) if isinstance(metadata, dict) else {}
            observed_fact_count += len(epistemic.get("observed_facts", []))
            hypothesis_count += len(epistemic.get("hypotheses", []))
    return {
        "observed_fact_count": observed_fact_count,
        "reported_claim_count": reported_claim_count,
        "hypothesis_count": hypothesis_count,
    }


def _action_candidate_metrics(turns: list[dict]) -> dict:
    confidences = []
    claim_support_count = 0
    claim_conflict_count = 0
    claim_required_evidence_count = 0
    candidate_count = 0
    for turn in turns:
        metadata = (turn.get("builder_action") or {}).get("_action_candidate_metadata", {})
        if not metadata:
            continue
        candidate_count += int(metadata.get("candidate_count", 0) or 0)
        claim_support_count += int(metadata.get("claim_support_count", 0) or 0)
        claim_conflict_count += int(metadata.get("claim_conflict_count", 0) or 0)
        claim_required_evidence_count += int(metadata.get("claim_required_evidence_count", 0) or 0)
        confidence = metadata.get("chosen_confidence")
        if confidence is not None:
            confidences.append(float(confidence))
    return {
        "mean_action_confidence": sum(confidences) / len(confidences) if confidences else 0.0,
        "claim_support_count": claim_support_count,
        "claim_conflict_count": claim_conflict_count,
        "claim_required_evidence_count": claim_required_evidence_count,
        "candidate_count": candidate_count,
    }


def _clarification_metrics(turns: list[dict]) -> dict:
    clarification_count = 0
    gated_clarification_count = 0
    clarification_resolution_count = 0
    low_confidence_gate_count = 0
    conflict_gate_count = 0
    risk_scores = []
    quality_scores = []
    progress_deltas = []
    for index, turn in enumerate(turns):
        action = turn.get("builder_action") or {}
        if action.get("action") == "clarify":
            clarification_count += 1
            quality_scores.append(_clarification_quality_score(action))
            resolution = _clarification_resolution(turns, index)
            if resolution["resolved"]:
                clarification_resolution_count += 1
            if resolution["progress_delta"] is not None:
                progress_deltas.append(resolution["progress_delta"])
        gate = action.get("_gated_clarification")
        if not gate:
            continue
        gated_clarification_count += 1
        reasons = gate.get("reasons", [])
        if "low_action_confidence" in reasons:
            low_confidence_gate_count += 1
        if "claim_conflict" in reasons:
            conflict_gate_count += 1
        risk_score = gate.get("risk_score")
        if risk_score is not None:
            risk_scores.append(float(risk_score))
    return {
        "clarification_count": clarification_count,
        "gated_clarification_count": gated_clarification_count,
        "gated_clarification_rate": gated_clarification_count / len(turns) if turns else 0.0,
        "clarification_resolution_count": clarification_resolution_count,
        "clarification_resolution_rate": (
            clarification_resolution_count / clarification_count
            if clarification_count else 0.0
        ),
        "mean_clarification_quality_score": (
            sum(quality_scores) / len(quality_scores)
            if quality_scores else 0.0
        ),
        "mean_post_clarification_progress_delta": (
            sum(progress_deltas) / len(progress_deltas)
            if progress_deltas else 0.0
        ),
        "mean_risk_score": sum(risk_scores) / len(risk_scores) if risk_scores else 0.0,
        "low_confidence_gate_count": low_confidence_gate_count,
        "conflict_gate_count": conflict_gate_count,
    }


def _clarification_quality_score(action: dict) -> float:
    score = 0.0
    clarification = str(action.get("clarification", "")).lower()
    gate = action.get("_gated_clarification") or {}
    metadata = action.get("_action_candidate_metadata") or {}
    reasons = set(gate.get("reasons", []) or [])
    if clarification and any(token in clarification for token in ("clarify", "please", "?")):
        score += 0.3
    if reasons - {"none"}:
        score += 0.3
    if reasons & {"claim_conflict", "required_evidence", "large_block_span_uncertainty"}:
        score += 0.2
    if metadata.get("public_evidence_summary") or int(metadata.get("claim_required_evidence_count", 0) or 0) > 0:
        score += 0.2
    return min(score, 1.0)


def _clarification_resolution(turns: list[dict], index: int) -> dict:
    clarify_turn = turns[index]
    clarify_action = clarify_turn.get("builder_action") or {}
    next_action_turn = _next_non_clarify_turn(turns, index + 1)
    if next_action_turn is None:
        return {"resolved": False, "progress_delta": None}
    before_metadata = _action_metadata(clarify_action)
    after_action = next_action_turn.get("builder_action") or {}
    after_metadata = _action_metadata(after_action)
    progress_delta = _progress_value(next_action_turn.get("progress")) - _progress_value(clarify_turn.get("progress"))
    confidence_improved = _metadata_float(after_metadata, "chosen_confidence") > _metadata_float(
        before_metadata,
        "chosen_confidence",
    )
    conflict_reduced = _metadata_int(after_metadata, "claim_conflict_count") < _metadata_int(
        before_metadata,
        "claim_conflict_count",
    )
    evidence_reduced = _metadata_int(after_metadata, "claim_required_evidence_count") < _metadata_int(
        before_metadata,
        "claim_required_evidence_count",
    )
    return {
        "resolved": progress_delta > 0.0 or confidence_improved or conflict_reduced or evidence_reduced,
        "progress_delta": progress_delta,
    }


def _next_non_clarify_turn(turns: list[dict], start: int) -> dict | None:
    for turn in turns[start:]:
        action = turn.get("builder_action") or {}
        if action.get("action") not in {None, "clarify"}:
            return turn
    return None


def _action_metadata(action: dict) -> dict:
    metadata = action.get("_action_candidate_metadata") or {}
    gate = action.get("_gated_clarification") or {}
    return {**gate, **metadata}


def _metadata_float(metadata: dict, key: str) -> float:
    try:
        return float(metadata.get(key, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _metadata_int(metadata: dict, key: str) -> int:
    try:
        return int(float(metadata.get(key, 0) or 0))
    except (TypeError, ValueError):
        return 0


def _progress_value(progress) -> float:
    if isinstance(progress, dict):
        if "current" in progress:
            return _metadata_float(progress, "current")
        if "overall_progress" in progress:
            return _metadata_float(progress, "overall_progress")
        metrics = progress.get("metrics")
        if isinstance(metrics, dict):
            return _metadata_float(metrics, "overall_progress")
    return 0.0


def _dual_dag_metrics(games: list[dict]) -> dict:
    node_count = 0
    edge_count = 0
    hypothesis_count = 0
    for game in games:
        dual_dag = game.get("dual_dag", {})
        epistemic_nodes = list(dual_dag.get("epistemic_nodes", []))
        node_count += len(epistemic_nodes)
        node_count += len(dual_dag.get("action_nodes", []))
        edge_count += len(dual_dag.get("epistemic_edges", []))
        edge_count += len(dual_dag.get("action_edges", []))
        hypothesis_count += sum(1 for node in epistemic_nodes if node.get("node_type") == "hypothesis")
    return {
        "dual_dag_node_count": node_count,
        "dual_dag_edge_count": edge_count,
        "hypothesis_count": hypothesis_count,
    }


def _write_dual_dag_artifacts(*, normalized_dir: Path, games: list[dict]) -> None:
    summary = {
        "game_count": len(games),
        "node_count": 0,
        "edge_count": 0,
        "games": [],
    }
    nodes_path = normalized_dir / "dual_dag_nodes.jsonl"
    edges_path = normalized_dir / "dual_dag_edges.jsonl"
    with nodes_path.open("w", encoding="utf-8") as nodes_file, edges_path.open("w", encoding="utf-8") as edges_file:
        for game in games:
            dual_dag = game.get("dual_dag", {})
            nodes = list(dual_dag.get("epistemic_nodes", [])) + list(dual_dag.get("action_nodes", []))
            epistemic_edges = [
                {"graph_type": "epistemic", **edge}
                for edge in dual_dag.get("epistemic_edges", [])
            ]
            action_edges = [
                {"graph_type": "action", **edge}
                for edge in dual_dag.get("action_edges", [])
            ]
            edges = epistemic_edges + action_edges
            summary["node_count"] += len(nodes)
            summary["edge_count"] += len(edges)
            summary["games"].append({
                "structure_id": game.get("structure_id"),
                "summary": dual_dag.get("summary", {}),
            })
            for node in nodes:
                nodes_file.write(json.dumps({"structure_id": game.get("structure_id"), **node}, ensure_ascii=False) + "\n")
            for edge in edges:
                edges_file.write(json.dumps({"structure_id": game.get("structure_id"), **edge}, ensure_ascii=False) + "\n")
    with (normalized_dir / "dual_dag_summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
