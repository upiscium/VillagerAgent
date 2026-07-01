import copy

import pytest

from benchmarks.craft.config import InvalidConfigError, condition_from_config, load_config, validate_config
from benchmarks.craft.craft_env_adapter import _evidence_summary_enabled


def test_config_rejects_target_structure_exposure():
    config = load_config("configs/craft/villageragent_qwen.yaml")
    config = copy.deepcopy(config)
    config["villageragent"]["expose_target_structure"] = True
    with pytest.raises(InvalidConfigError):
        validate_config(config)


def test_baseline_and_villageragent_use_same_seed_structure_turns():
    baseline = load_config("configs/craft/official_baseline.yaml")
    villager = load_config("configs/craft/eval_qwen_ollama.yaml")
    assert baseline["run"]["seed"] == villager["run"]["seed"]
    assert baseline["run"]["structures"] == villager["run"]["structures"]
    assert baseline["run"]["turns"] == villager["run"]["turns"]
    assert condition_from_config(baseline) == "official_baseline"
    assert condition_from_config(villager) == "villageragent_directors"


def test_condition_override_preserves_comparison_axes():
    config = load_config(
        "configs/craft/villageragent_qwen.yaml",
        overrides={"condition": "official_baseline", "structures": [0], "turns": 2, "seed": 7},
    )
    assert condition_from_config(config) == "official_baseline"
    assert config["run"]["structures"] == [0]
    assert config["run"]["turns"] == 2
    assert config["run"]["seed"] == 7


def test_nested_overrides_update_craft_and_dual_dag_sections():
    config = load_config(
        "configs/craft/eval_gemma4_12b_ollama_dual_dag.yaml",
        overrides={
            "craft": {"oracle_n": 5},
            "dual_dag": {"gated_clarification": {"enabled": False}},
        },
    )

    assert config["craft"]["oracle_n"] == 5
    assert config["dual_dag"]["runtime_decision_support"]["enabled"] is True
    assert config["dual_dag"]["gated_clarification"]["enabled"] is False


def test_qwen_ollama_config_uses_native_provider_without_openai_key():
    config = load_config("configs/craft/villageragent_qwen_ollama.yaml")
    assert config["models"]["director"]["provider"] == "ollama_native"
    assert config["models"]["builder"]["provider"] == "ollama_native"
    assert config["models"]["director"]["think"] is False


def test_batch_qwen_ollama_config_uses_three_structure_eval_axis():
    config = load_config("configs/craft/eval_qwen_ollama.yaml")
    assert config["run"]["structures"] == [0, 1, 2]
    assert config["run"]["turns"] == 5
    assert config["models"]["director"]["provider"] == "ollama_native"
    assert config["models"]["builder"]["provider"] == "ollama_native"


def test_single_director_qwen_ollama_config_uses_native_provider():
    config = load_config("configs/craft/single_director_qwen_ollama.yaml")
    assert condition_from_config(config) == "single_director_ablation"
    assert config["run"]["structures"] == [0, 1, 2]
    assert config["run"]["turns"] == 5
    assert config["models"]["director"]["provider"] == "ollama_native"
    assert config["models"]["builder"]["provider"] == "ollama_native"


def test_official_baseline_matches_qwen_batch_eval_axis():
    baseline = load_config("configs/craft/official_baseline.yaml")
    full_baseline = load_config("configs/craft/official_baseline_full.yaml")
    batch = load_config("configs/craft/eval_qwen_ollama.yaml")
    for config in (baseline, full_baseline):
        assert config["run"]["seed"] == batch["run"]["seed"]
        assert config["run"]["structures"] == batch["run"]["structures"]
        assert config["run"]["turns"] == batch["run"]["turns"]
        assert condition_from_config(config) == "official_baseline"
    assert full_baseline["craft"]["official_runner"] == "external_cli"


def test_ollama_model_comparison_configs_share_eval_axis():
    expected_models = {
        "configs/craft/eval_qwen_ollama.yaml": "qwen3.5:9b",
        "configs/craft/eval_qwen35_4b_ollama.yaml": "qwen3.5:4b",
        "configs/craft/eval_qwen36_27b_ollama.yaml": "qwen3.6:27b",
        "configs/craft/eval_gemma4_26b_ollama.yaml": "gemma4:26b",
        "configs/craft/eval_gemma4_e4b_ollama.yaml": "gemma4:e4b",
    }
    for config_path, model in expected_models.items():
        config = load_config(config_path)
        assert condition_from_config(config) == "villageragent_directors"
        assert config["run"]["structures"] == [0, 1, 2]
        assert config["run"]["turns"] == 5
        assert config["models"]["director"]["provider"] == "ollama_native"
        assert config["models"]["builder"]["provider"] == "ollama_native"
        assert config["models"]["director"]["model"] == model
        assert config["models"]["builder"]["model"] == model
        assert config["models"]["director"]["think"] is False
        assert config["models"]["builder"]["think"] is False


def test_dual_dag_qwen_configs_share_eval_axis():
    baseline = load_config("configs/craft/eval_qwen_ollama.yaml")
    for config_path in (
        "configs/craft/eval_qwen_ollama_dual_dag.yaml",
        "configs/craft/single_director_qwen_ollama_dual_dag.yaml",
        "configs/craft/eval_qwen_ollama_dual_dag_adaptive.yaml",
    ):
        config = load_config(config_path)
        assert config["run"]["structures"] == baseline["run"]["structures"]
        assert config["run"]["turns"] == baseline["run"]["turns"]
        assert config["dual_dag"]["enabled"] is True
        assert config["dual_dag"]["runtime_decision_support"]["enabled"] is True
        assert config["dual_dag"]["gated_clarification"]["enabled"] is True
        assert config["dual_dag"]["gated_clarification"]["min_action_confidence"] == 0.55
        assert config["dual_dag"]["gated_clarification"]["clarification_cost"] == 0.4
        assert config["models"]["director"]["provider"] == "ollama_native"
        assert config["models"]["builder"]["provider"] == "ollama_native"


def test_adaptive_dual_dag_config_keeps_static_gate_config_explicit():
    config = load_config("configs/craft/eval_qwen_ollama_dual_dag_adaptive.yaml")
    gate = config["dual_dag"]["gated_clarification"]
    assert gate["adaptive_thresholds"]["enabled"] is True
    assert gate["min_action_confidence"] == 0.55
    assert gate["clarification_cost"] == 0.4
    assert gate["clarify_on_required_evidence"] is True


def test_gemma4_ablation_configs_keep_villageragent_parity_and_flags():
    baseline = load_config("configs/craft/eval_gemma4_12b_ollama.yaml")
    metadata_only = load_config("configs/craft/eval_gemma4_12b_ollama_dual_dag_metadata_only.yaml")
    current_evidence = load_config("configs/craft/eval_gemma4_12b_ollama_dual_dag_current_evidence.yaml")
    retrieval = load_config("configs/craft/eval_gemma4_12b_ollama_dual_dag_retrieval.yaml")
    gating_no_coordination = load_config(
        "configs/craft/eval_gemma4_12b_ollama_dual_dag_gating_no_coordination.yaml"
    )
    clarify_only = load_config("configs/craft/eval_gemma4_12b_ollama_dual_dag_clarify_only.yaml")
    full_dual_dag = load_config("configs/craft/eval_gemma4_12b_ollama_dual_dag.yaml")
    clarify_fix = load_config("configs/craft/eval_gemma4_12b_ollama_dual_dag_clarify_throughput_fix.yaml")
    voi = load_config("configs/craft/eval_gemma4_12b_ollama_dual_dag_value_of_information.yaml")
    repeated_zero_fix = load_config(
        "configs/craft/eval_gemma4_12b_ollama_dual_dag_value_of_information_repeated_zero_fix.yaml"
    )

    for config in (
        metadata_only,
        current_evidence,
        retrieval,
        gating_no_coordination,
        clarify_only,
        full_dual_dag,
        clarify_fix,
        voi,
        repeated_zero_fix,
    ):
        assert config["craft"] == baseline["craft"]
        assert config["villageragent"] == baseline["villageragent"]
        assert config["models"] == baseline["models"]

    for config in (metadata_only, current_evidence, retrieval):
        assert config["dual_dag"]["gated_clarification"]["enabled"] is False

    assert _evidence_summary_enabled(metadata_only) is False
    assert _evidence_summary_enabled(current_evidence) is True
    assert current_evidence["dual_dag"]["runtime_decision_support"]["historical_retrieval"]["enabled"] is False
    assert retrieval["dual_dag"]["runtime_decision_support"]["historical_retrieval"]["enabled"] is True
    assert gating_no_coordination["dual_dag"]["gated_clarification"]["enabled"] is True
    assert gating_no_coordination["dual_dag"]["gated_clarification"]["coordination_actions"]["enabled"] is False
    assert clarify_only["dual_dag"]["gated_clarification"]["enabled"] is True
    assert clarify_only["dual_dag"]["gated_clarification"]["coordination_actions"]["enabled"] is True
    assert full_dual_dag["dual_dag"]["gated_clarification"]["enabled"] is True
    assert clarify_fix["dual_dag"]["gated_clarification"]["suppress_executable_low_confidence"] is True
    assert voi["dual_dag"]["gated_clarification"]["policy"] == "value_of_information"
    assert repeated_zero_fix["dual_dag"]["gated_clarification"]["policy"] == "value_of_information"
    assert repeated_zero_fix["dual_dag"]["action_selection"]["suppress_repeated_zero_progress"]["enabled"] is True
