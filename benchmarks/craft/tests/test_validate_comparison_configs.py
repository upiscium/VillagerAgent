import copy

from benchmarks.craft.config import load_config
from benchmarks.craft.validate_comparison_configs import compare_configs, main


def test_gemma4_v_and_dual_dag_configs_pass_parity_validation():
    report = compare_configs(
        load_config("configs/craft/eval_gemma4_12b_ollama.yaml"),
        load_config("configs/craft/eval_gemma4_12b_ollama_dual_dag.yaml"),
    )

    assert report["passed"] is True
    assert report["disallowed_differences"] == []
    assert {diff["path"] for diff in report["allowed_differences"]} >= {
        "run.name",
        "dual_dag.enabled",
    }


def test_parity_validation_fails_on_oracle_n_mismatch():
    baseline = load_config("configs/craft/eval_gemma4_12b_ollama.yaml")
    treatment = load_config("configs/craft/eval_gemma4_12b_ollama_dual_dag.yaml")
    treatment = copy.deepcopy(treatment)
    treatment["craft"]["oracle_n"] = 5

    report = compare_configs(baseline, treatment)

    assert report["passed"] is False
    assert "craft.oracle_n" in {
        diff["path"] for diff in report["disallowed_differences"]
    }


def test_parity_validation_fails_on_turn_mismatch():
    baseline = load_config("configs/craft/eval_gemma4_12b_ollama.yaml")
    treatment = load_config("configs/craft/eval_gemma4_12b_ollama_dual_dag.yaml")
    treatment = copy.deepcopy(treatment)
    treatment["run"]["turns"] = 20

    report = compare_configs(baseline, treatment)

    assert report["passed"] is False
    assert "run.turns" in {diff["path"] for diff in report["disallowed_differences"]}


def test_parity_validation_cli_writes_json_and_returns_nonzero(tmp_path):
    output = tmp_path / "parity.json"

    assert main([
        "--baseline",
        "configs/craft/eval_gemma4_12b_ollama.yaml",
        "--treatment",
        "configs/craft/official_baseline_gemma4_12b_ollama.yaml",
        "--json-output",
        str(output),
    ]) == 1

    assert "craft.oracle_n" in output.read_text(encoding="utf-8")
