import copy
import json
import os
from pathlib import Path
from typing import Any

import yaml


SUPPORTED_PROVIDERS = {"openai", "openai_compatible", "ollama", "ollama_native"}


class InvalidConfigError(ValueError):
    """Raised when a CRAFT integration config violates required constraints."""


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def _resolve_path(value: str, root: Path) -> str:
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return str(path)


def _require_false(config: dict, section: str, key: str) -> None:
    if config.get(section, {}).get(key) is not False:
        raise InvalidConfigError(
            f"CRAFT integration requires {section}.{key}=false."
        )


def _validate_provider(config: dict, model_name: str) -> None:
    provider = config.get("models", {}).get(model_name, {}).get("provider")
    if provider not in SUPPORTED_PROVIDERS:
        raise InvalidConfigError(
            f"models.{model_name}.provider must be one of {sorted(SUPPORTED_PROVIDERS)}."
        )


def _apply_overrides(config: dict, overrides: dict | None) -> dict:
    if not overrides:
        return config
    config = copy.deepcopy(config)
    run = config.setdefault("run", {})
    if overrides.get("structures") is not None:
        run["structures"] = overrides["structures"]
    if overrides.get("turns") is not None:
        run["turns"] = overrides["turns"]
    if overrides.get("seed") is not None:
        run["seed"] = overrides["seed"]
    if overrides.get("run_name_suffix"):
        run["name"] = f"{run.get('name', 'craft_run')}{overrides['run_name_suffix']}"
    if overrides.get("condition") is not None:
        condition = overrides["condition"]
        va = config.setdefault("villageragent", {})
        va["enabled"] = condition == "villageragent_directors"
        if condition == "single_director_ablation":
            va["enabled"] = False
            va["ablation"] = "single_director"
        elif va.get("ablation") == "single_director":
            va.pop("ablation")
    for section in ("craft", "dual_dag", "villageragent", "models", "logging"):
        if isinstance(overrides.get(section), dict):
            _deep_update(config.setdefault(section, {}), overrides[section])
    return config


def _deep_update(target: dict, updates: dict) -> dict:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value
    return target


def load_config(path: str, *, overrides: dict | None = None, require_api_keys: bool = False) -> dict:
    root = repo_root()
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = root / config_path
    if not config_path.exists():
        raise FileNotFoundError(f"CRAFT config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    config = _apply_overrides(_expand_env(config), overrides)
    config.setdefault("_meta", {})["config_path"] = str(config_path)
    config["_meta"]["repo_root"] = str(root)

    craft = config.setdefault("craft", {})
    for key in ("repo_path", "dataset_path"):
        if key in craft:
            craft[key] = _resolve_path(craft[key], root)

    for model_name in ("director", "builder"):
        model_config = config.setdefault("models", {}).setdefault(model_name, {})
        api_key_env = model_config.pop("api_key_env", None)
        if api_key_env:
            api_key = os.environ.get(api_key_env)
            if not api_key and require_api_keys:
                raise InvalidConfigError(
                    f"Environment variable {api_key_env} is not set"
                )
            if api_key:
                model_config["api_key"] = api_key
            else:
                model_config["api_key_env"] = api_key_env

    validate_config(config)
    return config


def validate_config(config: dict) -> None:
    craft = config.get("craft", {})
    run = config.get("run", {})
    villageragent = config.get("villageragent", {})

    repo_path = Path(craft.get("repo_path", ""))
    if not repo_path.exists():
        raise InvalidConfigError(f"craft.repo_path does not exist: {repo_path}")

    dataset_path = Path(craft.get("dataset_path", ""))
    if not dataset_path.exists():
        raise InvalidConfigError(f"craft.dataset_path does not exist: {dataset_path}")

    if run.get("turns", 0) <= 0:
        raise InvalidConfigError("run.turns must be greater than 0.")

    structures = run.get("structures")
    if structures is not None and not (
        isinstance(structures, list) and all(isinstance(i, int) for i in structures)
    ):
        raise InvalidConfigError("run.structures must be list[int] or null.")

    if craft.get("oracle_n", 1) <= 0:
        raise InvalidConfigError("craft.oracle_n must be greater than 0.")

    if villageragent.get("enabled", False):
        if villageragent.get("num_agents") != 3:
            raise InvalidConfigError("villageragent.num_agents must be 3.")
        _require_false(config, "villageragent", "expose_target_structure")
        _require_false(config, "villageragent", "expose_oracle_moves")
        _require_false(config, "villageragent", "expose_private_views_to_global_state")

    _validate_provider(config, "director")
    _validate_provider(config, "builder")


def condition_from_config(config: dict) -> str:
    villageragent = config.get("villageragent", {})
    if villageragent.get("enabled"):
        return "villageragent_directors"
    if villageragent.get("ablation") == "single_director":
        return "single_director_ablation"
    return "official_baseline"


def output_dir_for_config(config: dict) -> Path:
    root = repo_root()
    run = config.get("run", {})
    output_root = Path(run.get("output_dir", "result/craft"))
    if not output_root.is_absolute():
        output_root = root / output_root
    return output_root / run.get("name", "craft_run")


def save_resolved_config(config: dict, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "config.resolved.yaml").open("w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)
    with (output_dir / "config.resolved.json").open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
