import csv
import json
from pathlib import Path

from benchmarks.craft.dual_dag.schema import DUAL_DAG_SCHEMA_VERSION, dual_dag_schema_registry


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
    retrieval_metrics = _retrieval_metrics(turns)
    progress_action_metrics = _aggregate_progress_action_metrics(games)
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
            **retrieval_metrics,
            **progress_action_metrics,
            **dual_dag_metrics,
        },
        "villageragent": {
            "enabled": config.get("villageragent", {}).get("enabled", False),
            "use_task_decomposer": config.get("villageragent", {}).get("use_task_decomposer", False),
            "use_agent_controller": config.get("villageragent", {}).get("use_agent_controller", False),
            "use_state_manager": config.get("villageragent", {}).get("use_state_manager", False),
        },
        "partial_information": {
            "target_blueprint_exposed": config.get("villageragent", {}).get("expose_target_structure", False),
            "oracle_plan_exposed": config.get("villageragent", {}).get("expose_oracle_moves", False),
            "director_view_payloads_shared": config.get("villageragent", {}).get("expose_private_views_to_global_state", False),
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
        "max_progress",
        "progress_auc",
        "physical_action_count",
        "place_action_count",
        "remove_action_count",
        "clarify_count",
        "wait_count",
        "fallback_count",
        "no_op_count",
        "invalid_action_count",
        "positive_progress_turn_count",
        "zero_progress_turn_count",
        "negative_progress_turn_count",
        "mean_progress_delta_per_turn",
        "mean_progress_delta_per_physical_action",
        "observed_fact_count",
        "reported_claim_count",
        "hypothesis_count",
        "resolved_fact_count",
        "hypothesis_open_count",
        "hypothesis_supported_count",
        "hypothesis_conflicted_count",
        "hypothesis_resolved_count",
        "hypothesis_invalidated_count",
        "action_candidate_candidate_count",
        "action_candidate_executable_count",
        "action_candidate_waiting_for_evidence_count",
        "action_candidate_blocked_count",
        "action_candidate_invalidated_count",
        "action_candidate_executed_count",
        "coordination_action_count",
        "clarify_coordination_action_count",
        "wait_for_evidence_coordination_action_count",
        "mean_action_confidence",
        "claim_support_count",
        "claim_conflict_count",
        "claim_required_evidence_count",
        "candidate_count",
        "clarification_count",
        "unique_clarification_count",
        "repeated_clarification_count",
        "clarification_response_count",
        "clarification_to_unlock_count",
        "clarification_to_unlock_rate",
        "clarification_to_positive_action_count",
        "clarification_to_positive_action_latency",
        "clarification_without_state_change_count",
        "gate_invocation_count",
        "gate_allow_count",
        "gate_block_count",
        "gate_clarify_count",
        "gate_wait_count",
        "gate_reason_counts",
        "retrieved_node_count",
        "retrieved_claim_count",
        "retrieved_action_count",
        "mean_retrieved_node_age",
        "max_retrieved_node_age",
        "retrieved_executed_candidate_count",
        "retrieved_invalidated_candidate_count",
        "retrieved_superseded_node_count",
        "retrieval_used_in_top_action_count",
        "retrieval_changed_top_action_count",
        "gated_clarification_count",
        "gated_clarification_rate",
        "clarification_resolution_count",
        "clarification_resolution_rate",
        "mean_clarification_quality_score",
        "mean_post_clarification_progress_delta",
        "mean_risk_score",
        "low_confidence_gate_count",
        "conflict_gate_count",
        "required_evidence_gate_count",
        "span_uncertainty_gate_count",
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
            game_retrieval_metrics = _retrieval_metrics(game.get("turns", []))
            game_progress_action_metrics = _progress_action_metrics(game)
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
                "max_progress": game_progress_action_metrics["max_progress"],
                "progress_auc": game_progress_action_metrics["progress_auc"],
                "physical_action_count": game_progress_action_metrics["physical_action_count"],
                "place_action_count": game_progress_action_metrics["place_action_count"],
                "remove_action_count": game_progress_action_metrics["remove_action_count"],
                "clarify_count": game_progress_action_metrics["clarify_count"],
                "wait_count": game_progress_action_metrics["wait_count"],
                "fallback_count": game_progress_action_metrics["fallback_count"],
                "no_op_count": game_progress_action_metrics["no_op_count"],
                "invalid_action_count": game_progress_action_metrics["invalid_action_count"],
                "positive_progress_turn_count": game_progress_action_metrics["positive_progress_turn_count"],
                "zero_progress_turn_count": game_progress_action_metrics["zero_progress_turn_count"],
                "negative_progress_turn_count": game_progress_action_metrics["negative_progress_turn_count"],
                "mean_progress_delta_per_turn": game_progress_action_metrics["mean_progress_delta_per_turn"],
                "mean_progress_delta_per_physical_action": game_progress_action_metrics["mean_progress_delta_per_physical_action"],
                "observed_fact_count": game_epistemic_counts["observed_fact_count"],
                "reported_claim_count": game_epistemic_counts["reported_claim_count"],
                "hypothesis_count": game_hypothesis_count,
                "resolved_fact_count": game_dual_dag_metrics["resolved_fact_count"],
                "hypothesis_open_count": game_dual_dag_metrics["hypothesis_open_count"],
                "hypothesis_supported_count": game_dual_dag_metrics["hypothesis_supported_count"],
                "hypothesis_conflicted_count": game_dual_dag_metrics["hypothesis_conflicted_count"],
                "hypothesis_resolved_count": game_dual_dag_metrics["hypothesis_resolved_count"],
                "hypothesis_invalidated_count": game_dual_dag_metrics["hypothesis_invalidated_count"],
                "action_candidate_candidate_count": game_dual_dag_metrics["action_candidate_candidate_count"],
                "action_candidate_executable_count": game_dual_dag_metrics["action_candidate_executable_count"],
                "action_candidate_waiting_for_evidence_count": game_dual_dag_metrics["action_candidate_waiting_for_evidence_count"],
                "action_candidate_blocked_count": game_dual_dag_metrics["action_candidate_blocked_count"],
                "action_candidate_invalidated_count": game_dual_dag_metrics["action_candidate_invalidated_count"],
                "action_candidate_executed_count": game_dual_dag_metrics["action_candidate_executed_count"],
                "coordination_action_count": game_dual_dag_metrics["coordination_action_count"],
                "clarify_coordination_action_count": game_dual_dag_metrics["clarify_coordination_action_count"],
                "wait_for_evidence_coordination_action_count": game_dual_dag_metrics["wait_for_evidence_coordination_action_count"],
                "mean_action_confidence": game_action_candidate_metrics["mean_action_confidence"],
                "claim_support_count": game_action_candidate_metrics["claim_support_count"],
                "claim_conflict_count": game_action_candidate_metrics["claim_conflict_count"],
                "claim_required_evidence_count": game_action_candidate_metrics["claim_required_evidence_count"],
                "candidate_count": game_action_candidate_metrics["candidate_count"],
                "clarification_count": game_clarification_metrics["clarification_count"],
                "unique_clarification_count": game_clarification_metrics["unique_clarification_count"],
                "repeated_clarification_count": game_clarification_metrics["repeated_clarification_count"],
                "clarification_response_count": game_clarification_metrics["clarification_response_count"],
                "clarification_to_unlock_count": game_clarification_metrics["clarification_to_unlock_count"],
                "clarification_to_unlock_rate": game_clarification_metrics["clarification_to_unlock_rate"],
                "clarification_to_positive_action_count": game_clarification_metrics["clarification_to_positive_action_count"],
                "clarification_to_positive_action_latency": game_clarification_metrics["clarification_to_positive_action_latency"],
                "clarification_without_state_change_count": game_clarification_metrics["clarification_without_state_change_count"],
                "gate_invocation_count": game_clarification_metrics["gate_invocation_count"],
                "gate_allow_count": game_clarification_metrics["gate_allow_count"],
                "gate_block_count": game_clarification_metrics["gate_block_count"],
                "gate_clarify_count": game_clarification_metrics["gate_clarify_count"],
                "gate_wait_count": game_clarification_metrics["gate_wait_count"],
                "gate_reason_counts": game_clarification_metrics["gate_reason_counts"],
                "retrieved_node_count": game_retrieval_metrics["retrieved_node_count"],
                "retrieved_claim_count": game_retrieval_metrics["retrieved_claim_count"],
                "retrieved_action_count": game_retrieval_metrics["retrieved_action_count"],
                "mean_retrieved_node_age": game_retrieval_metrics["mean_retrieved_node_age"],
                "max_retrieved_node_age": game_retrieval_metrics["max_retrieved_node_age"],
                "retrieved_executed_candidate_count": game_retrieval_metrics["retrieved_executed_candidate_count"],
                "retrieved_invalidated_candidate_count": game_retrieval_metrics["retrieved_invalidated_candidate_count"],
                "retrieved_superseded_node_count": game_retrieval_metrics["retrieved_superseded_node_count"],
                "retrieval_used_in_top_action_count": game_retrieval_metrics["retrieval_used_in_top_action_count"],
                "retrieval_changed_top_action_count": game_retrieval_metrics["retrieval_changed_top_action_count"],
                "gated_clarification_count": game_clarification_metrics["gated_clarification_count"],
                "gated_clarification_rate": game_clarification_metrics["gated_clarification_rate"],
                "clarification_resolution_count": game_clarification_metrics["clarification_resolution_count"],
                "clarification_resolution_rate": game_clarification_metrics["clarification_resolution_rate"],
                "mean_clarification_quality_score": game_clarification_metrics["mean_clarification_quality_score"],
                "mean_post_clarification_progress_delta": game_clarification_metrics["mean_post_clarification_progress_delta"],
                "mean_risk_score": game_clarification_metrics["mean_risk_score"],
                "low_confidence_gate_count": game_clarification_metrics["low_confidence_gate_count"],
                "conflict_gate_count": game_clarification_metrics["conflict_gate_count"],
                "required_evidence_gate_count": game_clarification_metrics["required_evidence_gate_count"],
                "span_uncertainty_gate_count": game_clarification_metrics["span_uncertainty_gate_count"],
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


def _aggregate_progress_action_metrics(games: list[dict]) -> dict:
    game_metrics = [_progress_action_metrics(game) for game in games]
    if not game_metrics:
        return _empty_progress_action_metrics()
    total_physical_actions = sum(metric["physical_action_count"] for metric in game_metrics)
    total_progress_delta = sum(metric["total_progress_delta"] for metric in game_metrics)
    return {
        "max_progress": _mean([metric["max_progress"] for metric in game_metrics]),
        "progress_auc": _mean([metric["progress_auc"] for metric in game_metrics]),
        "physical_action_count": sum(metric["physical_action_count"] for metric in game_metrics),
        "place_action_count": sum(metric["place_action_count"] for metric in game_metrics),
        "remove_action_count": sum(metric["remove_action_count"] for metric in game_metrics),
        "clarify_count": sum(metric["clarify_count"] for metric in game_metrics),
        "wait_count": sum(metric["wait_count"] for metric in game_metrics),
        "fallback_count": sum(metric["fallback_count"] for metric in game_metrics),
        "no_op_count": sum(metric["no_op_count"] for metric in game_metrics),
        "invalid_action_count": sum(metric["invalid_action_count"] for metric in game_metrics),
        "positive_progress_turn_count": sum(metric["positive_progress_turn_count"] for metric in game_metrics),
        "zero_progress_turn_count": sum(metric["zero_progress_turn_count"] for metric in game_metrics),
        "negative_progress_turn_count": sum(metric["negative_progress_turn_count"] for metric in game_metrics),
        "mean_progress_delta_per_turn": _mean([
            metric["mean_progress_delta_per_turn"] for metric in game_metrics
        ]),
        "mean_progress_delta_per_physical_action": (
            total_progress_delta / total_physical_actions if total_physical_actions else 0.0
        ),
    }


def _progress_action_metrics(game: dict) -> dict:
    turns = game.get("turns", []) or []
    progress_values = [_progress_value(turn.get("progress")) for turn in turns]
    if not progress_values and "final_progress" in game:
        progress_values = [_metadata_float(game, "final_progress")]
    deltas = []
    previous_progress = 0.0
    for progress in progress_values:
        deltas.append(progress - previous_progress)
        previous_progress = progress

    action_counts = _action_throughput_counts(turns)
    total_progress_delta = sum(deltas)
    physical_action_count = action_counts["physical_action_count"]
    return {
        "max_progress": max(progress_values) if progress_values else 0.0,
        "progress_auc": _mean(progress_values),
        **action_counts,
        "positive_progress_turn_count": sum(1 for delta in deltas if delta > 0.0),
        "zero_progress_turn_count": sum(1 for delta in deltas if delta == 0.0),
        "negative_progress_turn_count": sum(1 for delta in deltas if delta < 0.0),
        "mean_progress_delta_per_turn": _mean(deltas),
        "mean_progress_delta_per_physical_action": (
            total_progress_delta / physical_action_count if physical_action_count else 0.0
        ),
        "total_progress_delta": total_progress_delta,
    }


def _action_throughput_counts(turns: list[dict]) -> dict:
    counts = {
        "physical_action_count": 0,
        "place_action_count": 0,
        "remove_action_count": 0,
        "clarify_count": 0,
        "wait_count": 0,
        "fallback_count": 0,
        "no_op_count": 0,
        "invalid_action_count": 0,
    }
    for turn in turns:
        action = turn.get("builder_action") or {}
        action_type = str(action.get("action", "") or "").lower()
        if action.get("_builder_fallback"):
            counts["fallback_count"] += 1
        if action.get("invalid") or action.get("_invalid_action") or action_type == "invalid":
            counts["invalid_action_count"] += 1
        if action_type == "place":
            counts["place_action_count"] += 1
            counts["physical_action_count"] += 1
        elif action_type == "remove":
            counts["remove_action_count"] += 1
            counts["physical_action_count"] += 1
        elif action_type == "clarify":
            counts["clarify_count"] += 1
        elif action_type == "wait_for_evidence":
            counts["wait_count"] += 1
        elif action_type in {"", "noop", "no_op", "none"}:
            counts["no_op_count"] += 1
    return counts


def _empty_progress_action_metrics() -> dict:
    return {
        "max_progress": 0.0,
        "progress_auc": 0.0,
        "physical_action_count": 0,
        "place_action_count": 0,
        "remove_action_count": 0,
        "clarify_count": 0,
        "wait_count": 0,
        "fallback_count": 0,
        "no_op_count": 0,
        "invalid_action_count": 0,
        "positive_progress_turn_count": 0,
        "zero_progress_turn_count": 0,
        "negative_progress_turn_count": 0,
        "mean_progress_delta_per_turn": 0.0,
        "mean_progress_delta_per_physical_action": 0.0,
    }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


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


def _retrieval_metrics(turns: list[dict]) -> dict:
    retrieved_claim_count = 0
    retrieved_action_count = 0
    retrieved_executed_candidate_count = 0
    retrieved_invalidated_candidate_count = 0
    retrieved_superseded_node_count = 0
    retrieval_used_in_top_action_count = 0
    retrieval_changed_top_action_count = 0
    ages = []
    for turn in turns:
        action = turn.get("builder_action") or {}
        metadata = action.get("_action_candidate_metadata") or {}
        turn_index = _turn_index(turn)
        chosen_id = metadata.get("chosen_candidate_id")
        top_uses_retrieval = False
        for candidate in metadata.get("candidates", []) or []:
            context = candidate.get("graph_context") or {}
            claims = context.get("relevant_public_claims", []) or []
            actions = context.get("relevant_public_actions", []) or []
            retrieved_claim_count += len(claims)
            retrieved_action_count += len(actions)
            for node in [*claims, *actions]:
                age = _retrieved_node_age(node, turn_index)
                if age is not None:
                    ages.append(age)
                state = str(node.get("state", "") or "").lower()
                if state == "invalidated":
                    retrieved_invalidated_candidate_count += 1
                if state == "superseded" or node.get("superseded_by"):
                    retrieved_superseded_node_count += 1
            retrieved_executed_candidate_count += sum(
                1 for node in actions
                if str(node.get("state", "executed") or "").lower() == "executed"
            )
            if candidate.get("node_id") == chosen_id and (claims or actions):
                top_uses_retrieval = True
        if top_uses_retrieval:
            retrieval_used_in_top_action_count += 1
        if metadata.get("retrieval_changed_top_action"):
            retrieval_changed_top_action_count += 1
    return {
        "retrieved_node_count": retrieved_claim_count + retrieved_action_count,
        "retrieved_claim_count": retrieved_claim_count,
        "retrieved_action_count": retrieved_action_count,
        "mean_retrieved_node_age": _mean(ages),
        "max_retrieved_node_age": max(ages) if ages else 0.0,
        "retrieved_executed_candidate_count": retrieved_executed_candidate_count,
        "retrieved_invalidated_candidate_count": retrieved_invalidated_candidate_count,
        "retrieved_superseded_node_count": retrieved_superseded_node_count,
        "retrieval_used_in_top_action_count": retrieval_used_in_top_action_count,
        "retrieval_changed_top_action_count": retrieval_changed_top_action_count,
    }


def _turn_index(turn: dict) -> int | None:
    for key in ("turn_index", "turn_number"):
        value = turn.get(key)
        if isinstance(value, int):
            return value
    return None


def _retrieved_node_age(node: dict, current_turn: int | None) -> int | None:
    source_turn = node.get("turn_index")
    if current_turn is None or not isinstance(source_turn, int):
        return None
    return max(current_turn - source_turn, 0)


def _clarification_metrics(turns: list[dict]) -> dict:
    clarification_count = 0
    clarification_keys = []
    clarification_response_count = 0
    gated_clarification_count = 0
    clarification_resolution_count = 0
    clarification_to_positive_action_count = 0
    positive_action_latencies = []
    gate_invocation_count = 0
    gate_allow_count = 0
    gate_block_count = 0
    gate_clarify_count = 0
    gate_wait_count = 0
    gate_reason_counts = {}
    low_confidence_gate_count = 0
    conflict_gate_count = 0
    required_evidence_gate_count = 0
    span_uncertainty_gate_count = 0
    risk_scores = []
    quality_scores = []
    progress_deltas = []
    for index, turn in enumerate(turns):
        action = turn.get("builder_action") or {}
        clarification_response_count += len(turn.get("director_responses", {}) or {})
        if action.get("action") == "clarify":
            clarification_count += 1
            clarification_keys.append(_clarification_key(action))
            quality_scores.append(_clarification_quality_score(action))
            resolution = _clarification_resolution(turns, index)
            if resolution["resolved"]:
                clarification_resolution_count += 1
            if resolution["progress_delta"] is not None:
                progress_deltas.append(resolution["progress_delta"])
            positive_latency = _clarification_positive_action_latency(turns, index)
            if positive_latency is not None:
                clarification_to_positive_action_count += 1
                positive_action_latencies.append(positive_latency)
        gate = action.get("_gated_clarification")
        if not gate:
            continue
        gate_invocation_count += 1
        gated_clarification_count += 1
        reasons = gate.get("reasons", [])
        if not reasons and gate.get("reason"):
            reasons = [gate["reason"]]
        for reason in reasons:
            gate_reason_counts[reason] = gate_reason_counts.get(reason, 0) + 1
        decision = str(gate.get("decision") or action.get("action") or "").lower()
        if gate.get("reason") == "none" or decision == "allow":
            gate_allow_count += 1
        elif decision == "clarify":
            gate_block_count += 1
            gate_clarify_count += 1
        elif decision == "wait_for_evidence":
            gate_block_count += 1
            gate_wait_count += 1
        if "low_action_confidence" in reasons:
            low_confidence_gate_count += 1
        if "claim_conflict" in reasons:
            conflict_gate_count += 1
        if "required_evidence" in reasons:
            required_evidence_gate_count += 1
        if "large_block_span_uncertainty" in reasons:
            span_uncertainty_gate_count += 1
        risk_score = gate.get("risk_score")
        if risk_score is not None:
            risk_scores.append(float(risk_score))
    unique_clarification_count = len(set(clarification_keys))
    return {
        "clarification_count": clarification_count,
        "unique_clarification_count": unique_clarification_count,
        "repeated_clarification_count": clarification_count - unique_clarification_count,
        "clarification_response_count": clarification_response_count,
        "clarification_to_unlock_count": clarification_resolution_count,
        "clarification_to_unlock_rate": (
            clarification_resolution_count / clarification_count
            if clarification_count else 0.0
        ),
        "clarification_to_positive_action_count": clarification_to_positive_action_count,
        "clarification_to_positive_action_latency": (
            _mean(positive_action_latencies) if positive_action_latencies else 0.0
        ),
        "clarification_without_state_change_count": clarification_count - clarification_resolution_count,
        "gate_invocation_count": gate_invocation_count,
        "gate_allow_count": gate_allow_count,
        "gate_block_count": gate_block_count,
        "gate_clarify_count": gate_clarify_count,
        "gate_wait_count": gate_wait_count,
        "gate_reason_counts": json.dumps(gate_reason_counts, sort_keys=True),
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
        "required_evidence_gate_count": required_evidence_gate_count,
        "span_uncertainty_gate_count": span_uncertainty_gate_count,
    }


def _clarification_key(action: dict) -> str:
    metadata = action.get("_action_candidate_metadata") or {}
    gate = action.get("_gated_clarification") or {}
    candidate = _chosen_candidate(metadata)
    candidate_action = candidate.get("action", {}) if isinstance(candidate, dict) else {}
    reasons = gate.get("reasons", []) or []
    question_type = reasons[0] if reasons else gate.get("reason", "general")
    parts = [
        str(action.get("target_director") or action.get("director_id") or "unknown_director"),
        str(action.get("coordinate") or action.get("position") or candidate_action.get("position") or "unknown_coordinate"),
        str(action.get("layer") or candidate_action.get("layer") or "unknown_layer"),
        str(action.get("block_attribute") or action.get("block") or candidate_action.get("block") or "unknown_block"),
        str(action.get("span") or candidate_action.get("span_to") or "unknown_span"),
        str(question_type),
    ]
    if all(part.startswith("unknown_") for part in parts[:5]):
        parts.append(str(action.get("clarification", "")).strip().lower())
    return "|".join(parts)


def _chosen_candidate(metadata: dict) -> dict:
    chosen_id = metadata.get("chosen_candidate_id")
    for candidate in metadata.get("candidates", []) or []:
        if candidate.get("node_id") == chosen_id:
            return candidate
    return {}


def _clarification_positive_action_latency(turns: list[dict], index: int) -> int | None:
    clarify_progress = _progress_value(turns[index].get("progress"))
    for next_index in range(index + 1, len(turns)):
        action = turns[next_index].get("builder_action") or {}
        if action.get("action") == "clarify":
            continue
        if _progress_value(turns[next_index].get("progress")) > clarify_progress:
            return next_index - index
    return None


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
    resolved_fact_count = 0
    hypothesis_status_counts = {status: 0 for status in _HYPOTHESIS_STATUSES}
    action_state_counts = {state: 0 for state in _ACTION_CANDIDATE_STATES}
    coordination_counts = {action_type: 0 for action_type in _COORDINATION_ACTION_TYPES}
    for game in games:
        dual_dag = game.get("dual_dag", {})
        epistemic_nodes = list(dual_dag.get("epistemic_nodes", []))
        action_nodes = list(dual_dag.get("action_nodes", []))
        node_count += len(epistemic_nodes)
        node_count += len(action_nodes)
        edge_count += len(dual_dag.get("epistemic_edges", []))
        edge_count += len(dual_dag.get("action_edges", []))
        for node in epistemic_nodes:
            node_type = node.get("node_type")
            if node_type == "hypothesis":
                hypothesis_count += 1
                status = (node.get("content") or {}).get("status", "open")
                if status in hypothesis_status_counts:
                    hypothesis_status_counts[status] += 1
            if node_type == "resolved_fact":
                resolved_fact_count += 1
        for node in action_nodes:
            state = node.get("state", "candidate")
            if state in action_state_counts:
                action_state_counts[state] += 1
            action_type = node.get("action_type")
            if action_type in coordination_counts:
                coordination_counts[action_type] += 1
    return {
        "dual_dag_node_count": node_count,
        "dual_dag_edge_count": edge_count,
        "hypothesis_count": hypothesis_count,
        "resolved_fact_count": resolved_fact_count,
        **{f"hypothesis_{status}_count": count for status, count in hypothesis_status_counts.items()},
        **{f"action_candidate_{state}_count": count for state, count in action_state_counts.items()},
        "coordination_action_count": sum(coordination_counts.values()),
        "clarify_coordination_action_count": coordination_counts["clarify"],
        "wait_for_evidence_coordination_action_count": coordination_counts["wait_for_evidence"],
    }


_HYPOTHESIS_STATUSES = ["open", "supported", "conflicted", "resolved", "invalidated"]
_ACTION_CANDIDATE_STATES = ["candidate", "executable", "waiting_for_evidence", "blocked", "invalidated", "executed"]
_COORDINATION_ACTION_TYPES = ["clarify", "wait_for_evidence"]


def _write_dual_dag_artifacts(*, normalized_dir: Path, games: list[dict]) -> None:
    summary = {
        "schema_version": DUAL_DAG_SCHEMA_VERSION,
        "game_count": len(games),
        "node_count": 0,
        "edge_count": 0,
        "schema": dual_dag_schema_registry(),
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
