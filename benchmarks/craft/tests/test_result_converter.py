import json

from benchmarks.craft.result_converter import normalize_results


def test_result_converter_writes_normalized_files(tmp_path):
    config = {
        "run": {"name": "test", "seed": 3, "structures": [0], "turns": 1},
        "models": {"director": {"model": "d"}, "builder": {"model": "b"}},
        "villageragent": {"enabled": True},
    }
    normalize_results(
        config=config,
        condition="villageragent_directors",
        raw_result={"structure_id": 0, "turns": [], "final_progress": 0.0, "completed": False},
        output_dir=tmp_path,
    )
    assert (tmp_path / "normalized" / "summary.json").exists()
    assert (tmp_path / "normalized" / "turns.jsonl").exists()
    assert (tmp_path / "normalized" / "metrics.csv").exists()
    summary = json.loads((tmp_path / "normalized" / "summary.json").read_text())
    assert summary["benchmark"] == "CRAFT"
