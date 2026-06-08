from benchmarks.craft.dual_dag.action_candidates import (
    action_candidates_from_moves,
    build_action_candidate_metadata,
)
from benchmarks.craft.dual_dag.epistemic_extractor import reported_claim_from_message


def test_action_candidate_supports_matching_color_and_location_claim():
    claim = reported_claim_from_message(
        director_id="D1",
        turn_index=1,
        message="Bottom left is yellow small.",
    )

    candidates = action_candidates_from_moves(
        moves=[{"action": "place", "block": "ys", "position": "(0,0)", "layer": 0}],
        reported_claims={claim["node_id"]: claim},
        turn_index=1,
    )

    candidate = candidates[0]
    assert candidate.node_id == "action:1:0"
    assert candidate.action_type == "place_block"
    assert candidate.supported_by == ["claim:D1:1"]
    assert candidate.conflicts_with == []
    assert candidate.required_evidence == []
    assert candidate.confidence == 0.75
    assert candidate.metadata["physically_verified"] is True


def test_action_candidate_records_conflicting_certain_claim():
    claim = reported_claim_from_message(
        director_id="D2",
        turn_index=1,
        message="Bottom left is blue small.",
    )

    candidates = action_candidates_from_moves(
        moves=[{"action": "place", "block": "ys", "position": "(0,0)", "layer": 0}],
        reported_claims={claim["node_id"]: claim},
        turn_index=1,
    )

    candidate = candidates[0]
    assert candidate.supported_by == []
    assert candidate.conflicts_with == ["claim:D2:1"]
    assert candidate.required_evidence == []
    assert candidate.confidence == 0.4


def test_action_candidate_records_uncertain_conflict_as_required_evidence():
    claim = reported_claim_from_message(
        director_id="D3",
        turn_index=1,
        message="Bottom left may be blue small, please confirm.",
    )

    candidates = action_candidates_from_moves(
        moves=[{"action": "place", "block": "ys", "position": "(0,0)", "layer": 0}],
        reported_claims={claim["node_id"]: claim},
        turn_index=1,
    )

    candidate = candidates[0]
    assert candidate.supported_by == []
    assert candidate.conflicts_with == []
    assert candidate.required_evidence == ["claim:D3:1"]
    assert candidate.confidence == 0.6


def test_action_candidate_metadata_selects_matching_chosen_action():
    candidates = action_candidates_from_moves(
        moves=[
            {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0},
            {"action": "remove", "position": "(1,1)", "layer": 0},
        ],
        reported_claims={},
        turn_index=5,
    )

    metadata = build_action_candidate_metadata(
        candidates=candidates,
        chosen_action={"action": "remove", "position": "(1,1)", "layer": 0},
        chosen_by="builder",
    )

    assert metadata["candidate_count"] == 2
    assert metadata["chosen_candidate_id"] == "action:5:1"
    assert metadata["chosen_by"] == "builder"
    assert metadata["chosen_confidence"] == 0.6
    assert len(metadata["candidates"]) == 2
