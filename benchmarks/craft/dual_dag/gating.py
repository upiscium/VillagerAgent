DEFAULT_GATE_CONFIG = {
    "min_action_confidence": 0.65,
    "max_conflict_count": 0,
    "clarify_on_large_block_span_uncertainty": True,
    "clarification_cost": 0.15,
    "mistake_cost_weight": 1.0,
}


def should_clarify(*, candidate_metadata: dict, config: dict) -> tuple[bool, dict]:
    dual_dag = config.get("dual_dag", {})
    gate_config = dual_dag.get("gated_clarification", {})
    enabled = bool(dual_dag.get("enabled", False) and gate_config.get("enabled", False))
    thresholds = {**DEFAULT_GATE_CONFIG, **gate_config}
    chosen_confidence = float(candidate_metadata.get("chosen_confidence", 0.0) or 0.0)
    conflict_count = int(candidate_metadata.get("claim_conflict_count", 0) or 0)
    risk_score = (1.0 - chosen_confidence) * float(thresholds["mistake_cost_weight"])
    reasons = []
    if enabled and chosen_confidence < float(thresholds["min_action_confidence"]):
        reasons.append("low_action_confidence")
    if enabled and conflict_count > int(thresholds["max_conflict_count"]):
        reasons.append("claim_conflict")
    if enabled and thresholds.get("clarify_on_large_block_span_uncertainty", True):
        if _large_block_span_unresolved(candidate_metadata):
            reasons.append("large_block_span_uncertainty")
    return bool(reasons), {
        "enabled": enabled,
        "should_clarify": bool(reasons),
        "reason": reasons[0] if reasons else "none",
        "reasons": reasons,
        "chosen_confidence": chosen_confidence,
        "claim_conflict_count": conflict_count,
        "risk_score": risk_score,
        "risk_exceeds_clarification_cost": risk_score > float(thresholds["clarification_cost"]),
        "thresholds": thresholds,
    }


def _large_block_span_unresolved(candidate_metadata: dict) -> bool:
    chosen_id = candidate_metadata.get("chosen_candidate_id")
    candidates = candidate_metadata.get("candidates", [])
    chosen = next(
        (candidate for candidate in candidates if candidate.get("node_id") == chosen_id),
        None,
    )
    if not chosen:
        return False
    action = chosen.get("action", {})
    block = action.get("block")
    return isinstance(block, str) and block.endswith("l") and not action.get("span_to")
