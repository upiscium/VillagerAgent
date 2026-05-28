import json

from benchmarks.craft.config import load_config
from benchmarks.craft.craft_env_adapter import CraftEnvAdapter
from benchmarks.craft.result_converter import normalize_results


def test_official_baseline_generates_comparable_turn_artifacts(tmp_path):
    config = load_config("configs/craft/official_baseline.yaml")
    adapter = CraftEnvAdapter(config, tmp_path)
    raw_result = adapter.run("official_baseline")

    assert raw_result["condition"] == "official_baseline"
    assert raw_result["structure_id"] == config["run"]["structures"][0]
    assert len(raw_result["turns"]) == config["run"]["turns"]
    assert raw_result["official_craft_runner"]["seed"] == config["run"]["seed"]
    assert raw_result["official_craft_runner"]["turns"] == config["run"]["turns"]
    assert (tmp_path / "raw" / "official_baseline.json").exists()

    normalize_results(
        config=config,
        condition="official_baseline",
        raw_result=raw_result,
        output_dir=tmp_path,
    )
    turns = (tmp_path / "normalized" / "turns.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(turns) == config["run"]["turns"]
    assert json.loads(turns[0])["leakage_check"]["passed"] is True
