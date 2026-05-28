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
    villager = load_config("configs/craft/villageragent_qwen.yaml")
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
