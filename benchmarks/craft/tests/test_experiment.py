import csv
import json

import pytest
import yaml

from benchmarks.craft.config import repo_root
from benchmarks.craft.experiment import (
    ExperimentConfigError,
    _expand_run_specs,
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


def test_load_qwen_robustness_manifest():
    manifest = load_experiment("configs/craft/experiments/qwen_robustness_v1.yaml")
    experiment = manifest["experiment"]
    assert experiment["name"] == "craft_qwen_robustness_v1"
    assert experiment["runs"][0] == {
        "config": "configs/craft/eval_qwen_ollama.yaml",
        "suffix": "_robust",
        "seeds": [1, 3, 5],
        "structures": [0, 1, 2, 3, 4],
    }
    assert experiment["report"]["variance_summary_output"].endswith("variance_qwen_robustness_v1.csv")


def test_load_qwen_adaptive_gating_manifest():
    manifest = load_experiment("configs/craft/experiments/qwen_adaptive_gating_v1.yaml")
    experiment = manifest["experiment"]
    assert experiment["name"] == "craft_qwen_adaptive_gating_v1"
    assert experiment["runs"] == [
        "configs/craft/eval_qwen_ollama_dual_dag.yaml",
        "configs/craft/eval_qwen_ollama_dual_dag_adaptive.yaml",
    ]
    assert experiment["report"]["compact_summary_output"].endswith("summary_qwen_adaptive_gating_v1.csv")


def test_load_gemma4_progress_smoke_manifest():
    manifest = load_experiment("configs/craft/experiments/gemma4_12b_progress_smoke.yaml")
    experiment = manifest["experiment"]
    assert experiment["name"] == "craft_gemma4_12b_progress_smoke"
    assert "Diagnostic smoke" in experiment["description"]
    assert experiment["overrides"]["structures"] == [0, 1, 2, 3, 4]
    assert experiment["overrides"]["turns"] == 5
    assert experiment["report"]["variance_group_by"] == "run_group"
    assert [run["suffix"] for run in experiment["runs"]] == [
        "_official",
        "_baseline",
        "_dual_dag",
    ]


def test_load_gemma4_progress_full_manifest():
    manifest = load_experiment("configs/craft/experiments/gemma4_12b_progress_full.yaml")
    experiment = manifest["experiment"]
    assert experiment["name"] == "craft_gemma4_12b_progress_full"
    assert experiment["overrides"]["structures"] == list(range(20))
    assert experiment["overrides"]["turns"] == 20
    assert experiment["report"]["variance_group_by"] == "run_group"
    for run in experiment["runs"]:
        assert run["seeds"] == [1, 3, 5, 7, 11]
        assert run["structures"] == list(range(20))


def test_load_gemma4_ablation_smoke_manifest_covers_c0_to_c3():
    manifest = load_experiment("configs/craft/experiments/gemma4_12b_dual_dag_ablation_smoke.yaml")
    experiment = manifest["experiment"]
    assert experiment["name"] == "craft_gemma4_12b_dual_dag_ablation_smoke"
    assert experiment["overrides"]["turns"] == 5
    assert experiment["report"]["variance_group_by"] == "run_group"
    assert [run["suffix"] for run in experiment["runs"]] == [
        "_c0_va_baseline",
        "_c1_metadata_only",
        "_c2_current_evidence",
        "_c3_retrieval",
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


def test_load_experiment_rejects_bad_run_spec(tmp_path):
    manifest_path = tmp_path / "bad_run_spec.yaml"
    manifest_path.write_text(
        yaml.safe_dump({"experiment": {"runs": [{"config": "config.yaml", "seeds": ["bad"]}]}}),
        encoding="utf-8",
    )
    with pytest.raises(ExperimentConfigError, match="seeds"):
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


def test_expand_run_specs_adds_seed_suffix_and_structure_override():
    runs = [{
        "config": "configs/craft/eval_qwen_ollama.yaml",
        "suffix": "_robust",
        "seeds": [1, 3],
        "structures": [0, 1, 2, 3, 4],
    }]

    expanded = _expand_run_specs(runs, {"run_name_suffix": "_final", "turns": 5})

    assert expanded == [
        {
            "config": "configs/craft/eval_qwen_ollama.yaml",
            "overrides": {
                "run_name_suffix": "_final_robust_seed1",
                "turns": 5,
                "seed": 1,
                "structures": [0, 1, 2, 3, 4],
            },
        },
        {
            "config": "configs/craft/eval_qwen_ollama.yaml",
            "overrides": {
                "run_name_suffix": "_final_robust_seed3",
                "turns": 5,
                "seed": 3,
                "structures": [0, 1, 2, 3, 4],
            },
        },
    ]


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
                    "base_url": "http://ollama.arc.upiscium.dev/v1",
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
    provenance = json.loads((output / "provenance.json").read_text(encoding="utf-8"))
    assert resolved_config["run"]["structures"] == [0]
    assert resolved_config["run"]["turns"] == 1
    assert resolved_config["run"]["seed"] == 9
    assert "--run-name-suffix _smoke" in command_text
    assert provenance["benchmark"] == "craft"
    assert provenance["schema_version"] == "1.0.0"


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
                    "variance_summary_output": str(tmp_path / "variance.csv"),
                    "variance_summary_json_output": str(tmp_path / "variance.json"),
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
    with (tmp_path / "variance.csv").open("r", encoding="utf-8", newline="") as f:
        variance_rows = list(csv.DictReader(f))
    assert variance_rows[0]["failed_run_count"] == "1"
    assert json.loads((tmp_path / "variance.json").read_text(encoding="utf-8"))["groups"][0][
        "failed_run_count"
    ] == 1
