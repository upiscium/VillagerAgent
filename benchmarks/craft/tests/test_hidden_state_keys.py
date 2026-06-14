from benchmarks.craft.dual_dag import analysis
from benchmarks.craft.dual_dag.evidence_prompt import HIDDEN_STATE_KEYS
from benchmarks.craft.dual_dag.serialization import FORBIDDEN_SERIALIZED_KEYS
from benchmarks.craft.hidden_state_keys import (
    BASE_HIDDEN_STATE_KEYS,
    OFFICIAL_RUNNER_HIDDEN_STATE_KEYS,
    hidden_state_key_labels,
    official_runner_hidden_state_key_labels,
)
from benchmarks.craft.villager.state_manager_adapter import FORBIDDEN_STATE_KEYS


def test_hidden_state_keys_are_shared_by_prompt_and_analysis_consumers():
    assert HIDDEN_STATE_KEYS is BASE_HIDDEN_STATE_KEYS
    assert FORBIDDEN_SERIALIZED_KEYS is BASE_HIDDEN_STATE_KEYS
    assert FORBIDDEN_STATE_KEYS is BASE_HIDDEN_STATE_KEYS
    assert analysis.HIDDEN_STATE_KEYS == hidden_state_key_labels()


def test_official_runner_hidden_state_keys_extend_base_keys():
    assert BASE_HIDDEN_STATE_KEYS < OFFICIAL_RUNNER_HIDDEN_STATE_KEYS
    assert "target_spans" in OFFICIAL_RUNNER_HIDDEN_STATE_KEYS
    assert "internal_thinking" in OFFICIAL_RUNNER_HIDDEN_STATE_KEYS
    assert official_runner_hidden_state_key_labels() == sorted(OFFICIAL_RUNNER_HIDDEN_STATE_KEYS)
