from benchmarks.craft.clarification_outcomes import classify_clarification_outcome


def test_classifier_marks_beneficial_when_candidate_unlocked():
    result = classify_clarification_outcome({
        "next_physical_action_turn": 2,
        "newly_unlocked_candidate_ids": ["action:2:0"],
    })

    assert result == {"outcome": "beneficial", "outcome_reasons": ["candidate_unlocked"]}


def test_classifier_marks_beneficial_when_top_action_changes_with_progress():
    result = classify_clarification_outcome({
        "next_physical_action_turn": 2,
        "top_action_changed": True,
        "next_physical_action_progress_delta": 0.2,
    })

    assert result["outcome"] == "beneficial"
    assert result["outcome_reasons"] == ["top_action_changed_with_positive_progress"]


def test_classifier_marks_neutral_for_same_action_with_progress():
    result = classify_clarification_outcome({
        "next_physical_action_turn": 2,
        "next_action_was_original_top_candidate": True,
        "candidate_states_before": ["executable"],
        "next_physical_action_progress_delta": 0.1,
    })

    assert result == {"outcome": "neutral", "outcome_reasons": ["same_action_after_clarification"]}


def test_classifier_marks_harmful_for_delayed_executable_without_progress():
    result = classify_clarification_outcome({
        "next_physical_action_turn": 2,
        "next_action_was_original_top_candidate": True,
        "candidate_states_before": ["executable"],
        "next_physical_action_progress_delta": 0.0,
    })

    assert result == {
        "outcome": "harmful",
        "outcome_reasons": ["executable_candidate_delayed_without_progress"],
    }


def test_classifier_marks_failed_without_followup_action():
    result = classify_clarification_outcome({"next_physical_action_turn": None})

    assert result == {"outcome": "failed", "outcome_reasons": ["no_followup_physical_action"]}


def test_classifier_marks_failed_on_parse_failure():
    result = classify_clarification_outcome({
        "next_physical_action_turn": 2,
        "response_parse_success": False,
    })

    assert result == {"outcome": "failed", "outcome_reasons": ["response_parse_failed"]}
