import csv
import json

import pytest
import yaml

from benchmarks.craft.config import repo_root
from benchmarks.craft.experiment import (
    ExperimentConfigError,
    _experiment_overrides,
    _report_path,
    _structure_override,
    load_experiment,
    run_experiment,
)


def test_load_experiment_manifest():
    manifest = load_experiment("configs/craft/experiments/qwen_batch_v1.yaml")
    experiment = manifest["experiment"]
    assert experiment["name"] == "craft_qwen_batch_v1"
    assert experiment["runs"] == [
        "configs/craft/eval_qwen_ollama.yaml",
        "configs/craft/single_director_qwen_ollama.yaml",
        "configs/craft/official_baseline.yaml",
    ]


def test_load_ollama_model_comparison_manifest():
    manifest = load_experiment("configs/craft/experiments/ollama_model_comparison_v1.yaml")
    experiment = manifest["experiment"]
    assert experiment["name"] == "craft_ollama_model_comparison_v1"
    assert experiment["runs"] == [
        "configs/craft/eval_qwen_ollama.yaml",
        "configs/craft/eval_qwen35_4b_ollama.yaml",
        "configs/craft/eval_qwen36_27b_ollama.yaml",
        "configs/craft/eval_gemma4_26b_ollama.yaml",
        "configs/craft/eval_gemma4_e4b_ollama.yaml",
    ]
    assert experiment["continue_on_error"] is True
    assert experiment["report"]["compact_summary_output"].endswith("summary_ollama_models_v1.csv")


def test_load_qwen_dual_dag_manifest():
    manifest = load_experiment("configs/craft/experiments/qwen_dual_dag_v1.yaml")
    experiment = manifest["experiment"]
    assert experiment["name"] == "craft_qwen_dual_dag_v1"
    assert experiment["runs"] == [
        "configs/craft/eval_qwen_ollama.yaml",
        "configs/craft/eval_qwen_ollama_dual_dag.yaml",
        "configs/craft/single_director_qwen_ollama_dual_dag.yaml",
        "configs/craft/official_baseline.yaml",
    ]


def test_load_experiment_rejects_empty_runs(tmp_path):
    manifest_path = tmp_path / "empty.yaml"
    manifest_path.write_text(yaml.safe_dump({"experiment": {"runs": []}}), encoding="utf-8")
    with pytest.raises(ExperimentConfigError, match="experiment.runs"):
        load_experiment(str(manifest_path))


def test_load_experiment_rejects_non_mapping_overrides(tmp_path):
    manifest_path = tmp_path / "bad_overrides.yaml"
    manifest_path.write_text(
        yaml.safe_dump({"experiment": {"runs": ["config.yaml"], "overrides": ["bad"]}}),
        encoding="utf-8",
    )
    with pytest.raises(ExperimentConfigError, match="experiment.overrides"):
        load_experiment(str(manifest_path))


def test_experiment_cli_overrides_replace_manifest_overrides():
    overrides = _experiment_overrides(
        {"overrides": {"structures": [0, 1], "turns": 5, "run_name_suffix": "_manifest"}},
        {"structures": [2], "turns": 1, "seed": None, "run_name_suffix": "_smoke"},
    )

    assert overrides == {"structures": [2], "turns": 1, "run_name_suffix": "_smoke"}
    assert _structure_override("0,2") == [0, 2]
    assert str(_report_path("result/craft/comparison.csv", overrides)).endswith(
        "result/craft/comparison_smoke.csv"
    )


def test_run_experiment_dry_run_creates_run_output(tmp_path):
    root = repo_root()
    config_path = tmp_path / "official.yaml"
    config_path.write_text(
        yaml.safe_dump({
            "run": {
                "name": "craft_experiment_dry_run",
                "seed": 3,
                "output_dir": str(tmp_path / "results"),
                "structures": [0],
                "turns": 1,
            },
            "craft": {
                "repo_path": str(root / "external/CRAFT"),
                "dataset_path": str(root / "external/CRAFT/data/structures_dataset_20.json"),
                "use_oracle": True,
                "oracle_n": 1,
                "builder_tool_use": False,
            },
            "villageragent": {"enabled": False},
            "models": {
                "director": {
                    "provider": "openai_compatible",
                    "model": "qwen3.5:9b",
                    "base_url": "https://ollama-melchior.arc.upiscium.dev/v1",
                    "api_key": "ollama",
                },
                "builder": {
                    "provider": "openai_compatible",
                    "model": "gpt-4o-mini",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "test",
                },
            },
        }),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "experiment.yaml"
    manifest_path.write_text(
        yaml.safe_dump({
            "experiment": {
                "name": "dry_run",
                "runs": [str(config_path)],
                "report": {"output": str(tmp_path / "comparison.csv")},
            }
        }),
        encoding="utf-8",
    )

    assert run_experiment(
        str(manifest_path),
        dry_run=True,
        overrides={"structures": [0], "turns": 1, "seed": 9, "run_name_suffix": "_smoke"},
    ) == []
    output = tmp_path / "results" / "craft_experiment_dry_run_smoke"
    resolved_config = yaml.safe_load((output / "config.resolved.yaml").read_text())
    command_text = (output / "command.txt").read_text()
    assert resolved_config["run"]["structures"] == [0]
    assert resolved_config["run"]["turns"] == 1
    assert resolved_config["run"]["seed"] == 9
    assert "--run-name-suffix _smoke" in command_text


def test_run_experiment_records_failed_run_and_writes_summaries(tmp_path, monkeypatch):
    root = repo_root()
    config_path = tmp_path / "ollama.yaml"
    config_path.write_text(
        yaml.safe_dump({
            "run": {
                "name": "craft_failed_model",
                "seed": 3,
                "output_dir": str(tmp_path / "results"),
                "structures": [0],
                "turns": 1,
            },
            "craft": {
                "repo_path": str(root / "external/CRAFT"),
                "dataset_path": str(root / "external/CRAFT/data/structures_dataset_20.json"),
                "use_oracle": True,
                "oracle_n": 1,
                "builder_tool_use": False,
            },
            "villageragent": {"enabled": False},
            "models": {
                "director": {
                    "provider": "openai_compatible",
                    "model": "missing-model",
                    "base_url": "https://ollama.invalid/v1",
                    "api_key": "ollama",
                },
                "builder": {
                    "provider": "openai_compatible",
                    "model": "missing-model",
                    "base_url": "https://ollama.invalid/v1",
                    "api_key": "ollama",
                },
            },
        }),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "experiment.yaml"
    manifest_path.write_text(
        yaml.safe_dump({
            "experiment": {
                "name": "failed_run",
                "continue_on_error": True,
                "result_root": str(tmp_path / "results"),
                "runs": [str(config_path)],
                "report": {
                    "output": str(tmp_path / "comparison.csv"),
                    "json_output": str(tmp_path / "comparison.json"),
                    "compact_summary_output": str(tmp_path / "summary.csv"),
                    "compact_summary_json_output": str(tmp_path / "summary.json"),
                },
            }
        }),
        encoding="utf-8",
    )

    def fail_run(*args, **kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr("benchmarks.craft.experiment.run_config", fail_run)

    rows = run_experiment(str(manifest_path))

    assert rows[0]["run_name"] == "craft_failed_model"
    assert rows[0]["status"] == "failed"
    assert rows[0]["error_type"] == "RuntimeError"
    normalized = tmp_path / "results" / "craft_failed_model" / "normalized"
    failure_summary = json.loads((normalized / "summary.json").read_text(encoding="utf-8"))
    assert failure_summary["failure"]["message"] == "model unavailable"
    with (tmp_path / "summary.csv").open("r", encoding="utf-8", newline="") as f:
        summary_rows = list(csv.DictReader(f))
    assert summary_rows[0]["status"] == "failed"
    assert summary_rows[0]["leakage_passed"] == "False"
