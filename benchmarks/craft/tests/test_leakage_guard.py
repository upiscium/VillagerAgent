import pytest

from benchmarks.craft.leakage_guard import LeakageGuard, PartialInformationLeakageError


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
