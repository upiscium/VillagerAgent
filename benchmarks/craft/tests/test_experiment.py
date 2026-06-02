import pytest
import yaml

from benchmarks.craft.config import repo_root
from benchmarks.craft.experiment import ExperimentConfigError, load_experiment, run_experiment


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

    assert run_experiment(str(manifest_path), dry_run=True) == []
    assert (tmp_path / "results" / "craft_experiment_dry_run" / "config.resolved.yaml").exists()
