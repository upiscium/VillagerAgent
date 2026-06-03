from benchmarks.craft.dual_dag.action_candidates import (
    action_candidate_from_parsed_action,
    action_candidates_from_moves,
    build_action_candidate_metadata,
)


def test_oracle_moves_become_deterministic_action_candidates():
    candidates = action_candidates_from_moves(
        moves=[{"action": "place", "block": "ys", "position": "(0,0)", "layer": 0, "span_to": None}],
        reported_claims={},
        turn_index=3,
    )

    assert len(candidates) == 1
    assert candidates[0].node_id == "action:3:0"
    assert candidates[0].action_type == "place_block"
    assert candidates[0].state == "candidate"
    assert candidates[0].metadata["physically_verified"] is True


def test_candidate_supports_matching_claim_keywords():
    claims = {
        "D1": {
            "node_id": "claim:D1:1",
            "content": {"keywords": ["place", "yellow", "small", "bottom", "left"]},
        },
        "D2": {
            "node_id": "claim:D2:1",
            "content": {"keywords": ["blue", "bottom", "left"]},
        },
    }
    candidates = action_candidates_from_moves(
        moves=[{"action": "place", "block": "ys", "position": "(0,0)", "layer": 0, "span_to": None}],
        reported_claims=claims,
        turn_index=1,
    )

    assert candidates[0].supported_by == ["claim:D1:1"]
    assert candidates[0].conflicts_with == ["claim:D2:1"]
    assert 0.0 <= candidates[0].confidence <= 1.0
    assert candidates[0].metadata["location_keywords"] == ["bottom", "left"]


def test_candidate_ignores_unrelated_location_color_conflict():
    claims = {
        "D1": {
            "node_id": "claim:D1:1",
            "content": {"keywords": ["blue", "top", "right"]},
        },
    }
    candidates = action_candidates_from_moves(
        moves=[{"action": "place", "block": "ys", "position": "(0,0)", "layer": 0, "span_to": None}],
        reported_claims=claims,
        turn_index=1,
    )

    assert candidates[0].conflicts_with == []


def test_candidate_ignores_color_only_conflict():
    claims = {
        "D1": {
            "node_id": "claim:D1:1",
            "content": {"keywords": ["blue"]},
        },
    }
    candidates = action_candidates_from_moves(
        moves=[{"action": "place", "block": "ys", "position": "(0,0)", "layer": 0, "span_to": None}],
        reported_claims=claims,
        turn_index=1,
    )

    assert candidates[0].conflicts_with == []


def test_candidate_treats_uncertain_color_mismatch_as_required_evidence():
    claims = {
        "D3": {
            "node_id": "claim:D3:2",
            "content": {
                "keywords": ["green", "bottom", "left"],
                "uncertain": True,
            },
        },
    }
    candidates = action_candidates_from_moves(
        moves=[{"action": "place", "block": "rs", "position": "(0,0)", "layer": 1, "span_to": None}],
        reported_claims=claims,
        turn_index=2,
    )

    assert candidates[0].conflicts_with == []
    assert candidates[0].required_evidence == ["claim:D3:2"]
    assert candidates[0].confidence == 0.6


def test_candidate_support_requires_location_overlap_when_claim_has_location():
    claims = {
        "D1": {
            "node_id": "claim:D1:1",
            "content": {"keywords": ["yellow", "top", "right"]},
        },
    }
    candidates = action_candidates_from_moves(
        moves=[{"action": "place", "block": "ys", "position": "(0,0)", "layer": 0, "span_to": None}],
        reported_claims=claims,
        turn_index=1,
    )

    assert candidates[0].supported_by == []


def test_candidate_does_not_support_generic_action_keywords_only():
    claims = {
        "D1": {
            "node_id": "claim:D1:1",
            "content": {"keywords": ["place", "bottom", "left"]},
        },
    }
    candidates = action_candidates_from_moves(
        moves=[{"action": "place", "block": "ys", "position": "(0,0)", "layer": 0, "span_to": None}],
        reported_claims=claims,
        turn_index=1,
    )

    assert candidates[0].supported_by == []


def test_build_metadata_marks_builder_chosen_candidate():
    action = {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0, "span_to": None}
    candidates = action_candidates_from_moves(
        moves=[action],
        reported_claims={},
        turn_index=1,
    )

    metadata = build_action_candidate_metadata(
        candidates=candidates,
        chosen_action=action,
        chosen_by="builder_response",
    )

    assert metadata["candidate_count"] == 1
    assert metadata["chosen_candidate_id"] == "action:1:0"
    assert metadata["chosen_by"] == "builder_response"
    assert metadata["claim_support_count"] == 0
    assert metadata["claim_conflict_count"] == 0
    assert metadata["claim_required_evidence_count"] == 0


def test_build_metadata_counts_required_evidence_for_chosen_candidate():
    action = {"action": "place", "block": "rs", "position": "(0,0)", "layer": 1, "span_to": None}
    candidates = action_candidates_from_moves(
        moves=[action],
        reported_claims={
            "D3": {
                "node_id": "claim:D3:2",
                "content": {"keywords": ["green", "bottom", "left"], "uncertain": True},
            },
        },
        turn_index=2,
    )

    metadata = build_action_candidate_metadata(
        candidates=candidates,
        chosen_action=action,
        chosen_by="builder_response",
    )

    assert metadata["claim_required_evidence_count"] == 1


def test_build_metadata_marks_oracle_fallback_candidate():
    action = {"action": "place", "block": "bl", "position": "(1,0)", "layer": 0, "span_to": "(2,0)"}
    candidates = action_candidates_from_moves(
        moves=[action],
        reported_claims={},
        turn_index=2,
    )

    metadata = build_action_candidate_metadata(
        candidates=candidates,
        chosen_action=action,
        chosen_by="oracle_fallback",
    )

    assert metadata["chosen_candidate_id"] == "action:2:0"
    assert metadata["chosen_by"] == "oracle_fallback"


def test_parsed_action_candidate_is_not_physically_verified():
    candidate = action_candidate_from_parsed_action(
        action={"action": "remove", "position": "(1,2)", "layer": 0, "span_to": None},
        reported_claims={},
        turn_index=4,
    )

    assert candidate.node_id == "action:4:0"
    assert candidate.action_type == "remove_block"
    assert candidate.metadata["physically_verified"] is False
