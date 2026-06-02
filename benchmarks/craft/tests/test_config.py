import copy

import pytest

from benchmarks.craft.config import InvalidConfigError, condition_from_config, load_config, validate_config


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
    batch = load_config("configs/craft/eval_qwen_ollama.yaml")
    assert baseline["run"]["seed"] == batch["run"]["seed"]
    assert baseline["run"]["structures"] == batch["run"]["structures"]
    assert baseline["run"]["turns"] == batch["run"]["turns"]


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
    ):
        config = load_config(config_path)
        assert config["run"]["structures"] == baseline["run"]["structures"]
        assert config["run"]["turns"] == baseline["run"]["turns"]
        assert config["dual_dag"]["enabled"] is True
        assert config["dual_dag"]["gated_clarification"]["enabled"] is True
        assert config["dual_dag"]["gated_clarification"]["min_action_confidence"] == 0.55
        assert config["dual_dag"]["gated_clarification"]["clarification_cost"] == 0.4
        assert config["models"]["director"]["provider"] == "ollama_native"
        assert config["models"]["builder"]["provider"] == "ollama_native"
