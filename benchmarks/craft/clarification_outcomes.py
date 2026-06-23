BENEFICIAL = "beneficial"
NEUTRAL = "neutral"
HARMFUL = "harmful"
FAILED = "failed"


def classify_clarification_outcome(trace_row: dict) -> dict:
    reasons = []
    if trace_row.get("response_received") is False:
        return _result(FAILED, ["response_missing"])
    if trace_row.get("response_parse_success") is False:
        return _result(FAILED, ["response_parse_failed"])
    if trace_row.get("duplicate_clarification") is True:
        return _result(FAILED, ["duplicate_question"])
    if trace_row.get("next_physical_action_turn") is None:
        return _result(FAILED, ["no_followup_physical_action"])

    unlocked = _non_empty(trace_row.get("newly_unlocked_candidate_ids"))
    invalidated = _non_empty(trace_row.get("newly_invalidated_candidate_ids"))
    resolved_hypotheses = _non_empty(trace_row.get("resolved_hypothesis_ids"))
    resolved_evidence = _non_empty(trace_row.get("resolved_required_evidence_ids"))
    top_changed = bool(trace_row.get("top_action_changed", False))
    progress_delta = _float_or_none(trace_row.get("next_physical_action_progress_delta"))

    if unlocked:
        reasons.append("candidate_unlocked")
    if invalidated:
        reasons.append("candidate_invalidated")
    if resolved_hypotheses:
        reasons.append("hypothesis_resolved")
    if resolved_evidence:
        reasons.append("required_evidence_resolved")
    if top_changed and progress_delta is not None and progress_delta > 0:
        reasons.append("top_action_changed_with_positive_progress")
    if reasons:
        return _result(BENEFICIAL, reasons)

    same_action = bool(
        trace_row.get("same_top_action_after_clarification")
        or trace_row.get("next_action_was_original_top_candidate")
    )
    executable_before = "executable" in set(trace_row.get("candidate_states_before") or [])
    if executable_before and same_action and (progress_delta is None or progress_delta <= 0):
        return _result(HARMFUL, ["executable_candidate_delayed_without_progress"])
    if executable_before and progress_delta is not None and progress_delta <= 0:
        return _result(HARMFUL, ["executable_candidate_delayed_without_progress"])
    if same_action:
        return _result(NEUTRAL, ["same_action_after_clarification"])
    if _confidence_only_changed(trace_row):
        return _result(NEUTRAL, ["confidence_only_changed"])
    return _result(NEUTRAL, ["no_decisive_outcome"])


def _result(outcome: str, reasons: list[str]) -> dict:
    return {"outcome": outcome, "outcome_reasons": reasons}


def _non_empty(value) -> bool:
    return isinstance(value, list) and len(value) > 0


def _float_or_none(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _confidence_only_changed(trace_row: dict) -> bool:
    before = _float_or_none(trace_row.get("action_confidence_before"))
    after = _float_or_none(trace_row.get("action_confidence_after"))
    if before is None or after is None or before == after:
        return False
    return not any(
        _non_empty(trace_row.get(key))
        for key in (
            "newly_unlocked_candidate_ids",
            "newly_invalidated_candidate_ids",
            "resolved_hypothesis_ids",
            "resolved_required_evidence_ids",
        )
    ) and not bool(trace_row.get("top_action_changed", False))
