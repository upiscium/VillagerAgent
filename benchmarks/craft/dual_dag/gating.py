DEFAULT_GATE_CONFIG = {
    "policy": "current",
    "min_action_confidence": 0.55,
    "max_conflict_count": 0,
    "clarify_on_required_evidence": False,
    "clarify_on_large_block_span_uncertainty": True,
    "suppress_executable_low_confidence": False,
    "clarification_cost": 0.4,
    "mistake_cost_weight": 1.0,
    "value_of_information": {
        "resolution_probability": 0.55,
        "required_evidence_resolution_bonus": 0.1,
        "conflict_resolution_bonus": 0.1,
        "mean_candidate_value": 0.05,
        "clarification_failure_risk": 0.05,
        "min_value": 0.0,
    },
    "adaptive_thresholds": {
        "enabled": False,
        "min_confidence_floor": 0.35,
        "min_confidence_ceiling": 0.8,
        "support_relief": 0.05,
        "conflict_pressure": 0.1,
        "required_evidence_pressure": 0.05,
        "clarification_cost_floor": 0.2,
        "clarification_cost_conflict_discount": 0.05,
        "clarification_cost_required_evidence_discount": 0.03,
    },
}


def should_clarify(*, candidate_metadata: dict, config: dict) -> tuple[bool, dict]:
    dual_dag = config.get("dual_dag", {})
    gate_config = dual_dag.get("gated_clarification", {})
    enabled = bool(dual_dag.get("enabled", False) and gate_config.get("enabled", False))
    thresholds = _gate_thresholds(gate_config)
    policy = str(thresholds.get("policy", "current") or "current")
    chosen_confidence = float(candidate_metadata.get("chosen_confidence", 0.0) or 0.0)
    conflict_count = int(candidate_metadata.get("claim_conflict_count", 0) or 0)
    required_evidence_count = int(candidate_metadata.get("claim_required_evidence_count", 0) or 0)
    support_count = int(candidate_metadata.get("claim_support_count", 0) or 0)
    thresholds = _apply_adaptive_thresholds(
        thresholds=thresholds,
        support_count=support_count,
        conflict_count=conflict_count,
        required_evidence_count=required_evidence_count,
    )
    risk_score = (1.0 - chosen_confidence) * float(thresholds["mistake_cost_weight"])
    risk_exceeds_cost = risk_score > float(thresholds["clarification_cost"])
    reasons = []
    if enabled and chosen_confidence < float(thresholds["min_action_confidence"]):
        reasons.append("low_action_confidence")
    if enabled and conflict_count > int(thresholds["max_conflict_count"]) and risk_exceeds_cost:
        reasons.append("claim_conflict")
    if (
        enabled
        and thresholds.get("clarify_on_required_evidence", False)
        and required_evidence_count > 0
        and risk_exceeds_cost
    ):
        reasons.append("required_evidence")
    if enabled and thresholds.get("clarify_on_large_block_span_uncertainty", True):
        if _large_block_span_unresolved(candidate_metadata):
            reasons.append("large_block_span_uncertainty")
    if enabled and thresholds.get("suppress_executable_low_confidence", False):
        reasons = _suppress_executable_low_confidence(reasons, candidate_metadata)
    voi = _value_of_information_components(
        candidate_metadata=candidate_metadata,
        thresholds=thresholds,
        chosen_confidence=chosen_confidence,
        conflict_count=conflict_count,
        required_evidence_count=required_evidence_count,
    )
    if policy == "disabled":
        reasons = []
    elif policy == "value_of_information" and reasons:
        if voi["value_of_clarification"] <= float(thresholds["value_of_information"].get("min_value", 0.0)):
            reasons = []
    return bool(reasons), {
        "enabled": enabled,
        "should_clarify": bool(reasons),
        "policy": policy,
        "reason": reasons[0] if reasons else "none",
        "reasons": reasons,
        "chosen_confidence": chosen_confidence,
        "claim_support_count": support_count,
        "claim_conflict_count": conflict_count,
        "claim_required_evidence_count": required_evidence_count,
        "risk_score": risk_score,
        "risk_exceeds_clarification_cost": risk_exceeds_cost,
        "value_of_clarification": voi["value_of_clarification"],
        "voi_components": voi,
        "thresholds": thresholds,
    }


def _apply_adaptive_thresholds(
    *,
    thresholds: dict,
    support_count: int,
    conflict_count: int,
    required_evidence_count: int,
) -> dict:
    adaptive = thresholds.get("adaptive_thresholds", {}) or {}
    if not adaptive.get("enabled", False):
        return thresholds

    adjusted = dict(thresholds)
    base_min_confidence = float(thresholds["min_action_confidence"])
    min_confidence = base_min_confidence
    min_confidence -= support_count * float(adaptive.get("support_relief", 0.05))
    min_confidence += conflict_count * float(adaptive.get("conflict_pressure", 0.1))
    min_confidence += required_evidence_count * float(adaptive.get("required_evidence_pressure", 0.05))
    adjusted["min_action_confidence"] = _clamp(
        min_confidence,
        float(adaptive.get("min_confidence_floor", 0.35)),
        float(adaptive.get("min_confidence_ceiling", 0.8)),
    )

    clarification_cost = float(thresholds["clarification_cost"])
    clarification_cost -= conflict_count * float(adaptive.get("clarification_cost_conflict_discount", 0.05))
    clarification_cost -= required_evidence_count * float(
        adaptive.get("clarification_cost_required_evidence_discount", 0.03)
    )
    adjusted["clarification_cost"] = max(
        float(adaptive.get("clarification_cost_floor", 0.2)),
        clarification_cost,
    )
    adjusted["adaptive_thresholds"] = {**adaptive, "applied": True}
    return adjusted


def _gate_thresholds(gate_config: dict) -> dict:
    thresholds = {**DEFAULT_GATE_CONFIG, **gate_config}
    thresholds["adaptive_thresholds"] = {
        **DEFAULT_GATE_CONFIG["adaptive_thresholds"],
        **(gate_config.get("adaptive_thresholds", {}) or {}),
    }
    thresholds["value_of_information"] = {
        **DEFAULT_GATE_CONFIG["value_of_information"],
        **(gate_config.get("value_of_information", {}) or {}),
    }
    return thresholds


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _large_block_span_unresolved(candidate_metadata: dict) -> bool:
    chosen = _chosen_candidate(candidate_metadata)
    if not chosen:
        return False
    action = chosen.get("action", {})
    block = action.get("block")
    return isinstance(block, str) and block.endswith("l") and not action.get("span_to")


def _suppress_executable_low_confidence(reasons: list[str], candidate_metadata: dict) -> list[str]:
    if reasons != ["low_action_confidence"]:
        return reasons
    chosen = _chosen_candidate(candidate_metadata)
    if chosen and chosen.get("state") == "executable":
        return []
    return reasons


def _value_of_information_components(
    *,
    candidate_metadata: dict,
    thresholds: dict,
    chosen_confidence: float,
    conflict_count: int,
    required_evidence_count: int,
) -> dict:
    voi_config = thresholds.get("value_of_information", {}) or {}
    p_wrong = 1.0 - _clamp(chosen_confidence, 0.0, 1.0)
    p_resolution = _clamp(
        float(voi_config.get("resolution_probability", 0.55))
        + required_evidence_count * float(voi_config.get("required_evidence_resolution_bonus", 0.1))
        + conflict_count * float(voi_config.get("conflict_resolution_bonus", 0.1)),
        0.0,
        1.0,
    )
    mistake_cost = float(thresholds.get("mistake_cost_weight", 1.0))
    mean_candidate_value = float(voi_config.get("mean_candidate_value", 0.05))
    expected_avoided_mistake_cost = p_wrong * p_resolution * mistake_cost
    expected_future_unlock_value = p_resolution * required_evidence_count * mean_candidate_value
    turn_opportunity_cost = _turn_opportunity_cost(candidate_metadata, mean_candidate_value)
    clarification_cost = float(thresholds.get("clarification_cost", 0.4))
    clarification_failure_risk = float(voi_config.get("clarification_failure_risk", 0.05))
    value = (
        expected_avoided_mistake_cost
        + expected_future_unlock_value
        - turn_opportunity_cost
        - clarification_cost
        - clarification_failure_risk
    )
    return {
        "p_wrong": p_wrong,
        "p_resolution": p_resolution,
        "expected_avoided_mistake_cost": expected_avoided_mistake_cost,
        "expected_future_unlock_value": expected_future_unlock_value,
        "turn_opportunity_cost": turn_opportunity_cost,
        "clarification_cost": clarification_cost,
        "clarification_failure_risk": clarification_failure_risk,
        "value_of_clarification": value,
    }


def _turn_opportunity_cost(candidate_metadata: dict, mean_candidate_value: float) -> float:
    chosen = _chosen_candidate(candidate_metadata)
    if not chosen or chosen.get("state") != "executable":
        return 0.0
    confidence = _clamp(float(chosen.get("confidence", candidate_metadata.get("chosen_confidence", 0.0)) or 0.0), 0.0, 1.0)
    return confidence * mean_candidate_value


def _chosen_candidate(candidate_metadata: dict) -> dict | None:
    chosen_id = candidate_metadata.get("chosen_candidate_id")
    candidates = candidate_metadata.get("candidates", [])
    return next(
        (candidate for candidate in candidates if candidate.get("node_id") == chosen_id),
        None,
    )
