import json

import pytest

from benchmarks.craft.leakage_guard import LeakageGuard, PartialInformationLeakageError
from benchmarks.craft.craft_env_adapter import _target_structure_for_guard


class _GameState:
    def __init__(self, current_structure):
        self.current_structure = current_structure


def test_oracle_moves_not_in_prompt():
    guard = LeakageGuard({})
    prompt = [{"role": "user", "content": "Use only my private view."}]
    report = guard.inspect_prompt(
        director_id="D1",
        prompt_messages=prompt,
        forbidden_payloads={"oracle_moves": [{"action": "place", "position": "(0,0)"}]},
    )
    assert report["passed"] is True


def test_target_structure_not_in_prompt():
    guard = LeakageGuard({})
    prompt = [{"role": "user", "content": "target hidden payload"}]
    with pytest.raises(PartialInformationLeakageError):
        guard.inspect_prompt(
            director_id="D1",
            prompt_messages=prompt,
            forbidden_payloads={"target_structure": "target hidden payload"},
        )


def test_saved_builder_prompt_artifact_is_checked_without_leakage(tmp_path):
    artifact_path = tmp_path / "Builder_turn_001.json"
    artifact_path.write_text(
        json.dumps({
            "director_id": "Builder",
            "turn_index": 1,
            "prompt_messages": [
                {"role": "system", "content": "You are the CRAFT Builder."},
                {"role": "user", "content": "Use only public Director claims."},
            ],
        }),
        encoding="utf-8",
    )
    guard = LeakageGuard({})

    report = guard.inspect_prompt_artifact(
        artifact_path=artifact_path,
        forbidden_payloads={
            "target_structure": "hidden target payload",
            "oracle_moves": [{"action": "place", "position": "(0,0)"}],
            "D1_raw_private_view": "hidden raw private view",
        },
    )

    assert report["director_id"] == "Builder"
    assert report["artifact_path"] == str(artifact_path)
    assert report["passed"] is True
    assert guard.reports == [report]


def test_saved_builder_prompt_artifact_reports_hidden_payload(tmp_path):
    artifact_path = tmp_path / "Builder_turn_001.json"
    artifact_path.write_text(
        json.dumps({
            "director_id": "Builder",
            "turn_index": 1,
            "prompt_messages": [
                {"role": "user", "content": "Use hidden raw private view."},
            ],
        }),
        encoding="utf-8",
    )
    guard = LeakageGuard({})

    with pytest.raises(PartialInformationLeakageError):
        guard.inspect_prompt_artifact(
            artifact_path=artifact_path,
            forbidden_payloads={"D1_raw_private_view": "hidden raw private view"},
        )

    assert guard.reports[0]["director_id"] == "Builder"
    assert guard.reports[0]["passed"] is False
    assert guard.reports[0]["violations"][0]["label"] == "D1_raw_private_view"


def test_saved_builder_prompt_artifact_reports_hidden_key(tmp_path):
    artifact_path = tmp_path / "Builder_turn_001.json"
    artifact_path.write_text(
        json.dumps({
            "director_id": "Builder",
            "turn_index": 1,
            "prompt_messages": [
                {"role": "user", "content": "Do not expose target_structure."},
            ],
        }),
        encoding="utf-8",
    )
    guard = LeakageGuard({})

    with pytest.raises(PartialInformationLeakageError):
        guard.inspect_prompt_artifact(
            artifact_path=artifact_path,
            forbidden_payloads={"hidden_key:target_structure": "target_structure"},
        )

    assert guard.reports[0]["violations"][0]["label"] == "hidden_key:target_structure"


def test_target_structure_guard_allows_publicly_completed_structure():
    target = {"(0,0)": ["gs"], "(0,1)": []}
    sample = {"structure": target}

    assert _target_structure_for_guard(sample=sample, game_state=_GameState({"(0,0)": [], "(0,1)": []})) == target
    assert _target_structure_for_guard(sample=sample, game_state=_GameState({"(0,0)": ["gs"], "(0,1)": []})) is None
