import json
import subprocess

from benchmarks.craft.config import load_config
from benchmarks.craft.craft_env_adapter import CraftEnvAdapter
from benchmarks.craft.result_converter import normalize_results


def test_official_baseline_generates_comparable_turn_artifacts(tmp_path):
    config = load_config(
        "configs/craft/official_baseline.yaml",
        overrides={"structures": [0]},
    )
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


def test_official_baseline_runs_all_requested_structures(tmp_path):
    config = load_config(
        "configs/craft/official_baseline.yaml",
        overrides={"structures": [0, 1], "turns": 2},
    )
    adapter = CraftEnvAdapter(config, tmp_path)
    raw_result = adapter.run("official_baseline")

    assert raw_result["structure_ids"] == [0, 1]
    assert len(raw_result["games"]) == 2
    assert len(raw_result["turns"]) == 4
    assert raw_result["official_craft_runner"]["structure_indices"] == [0, 1]


def test_official_baseline_external_cli_normalizes_runner_output(tmp_path, monkeypatch):
    config = load_config(
        "configs/craft/official_baseline_full.yaml",
        overrides={"structures": [0], "turns": 2, "seed": 7},
    )

    def fake_run(command, cwd, check, capture_output, text):
        output_dir = command[command.index("--output") + 1]
        result_dir = tmp_path / "raw" / "official_craft_runner" / "gpt-4o-mini_gpt-4o-mini"
        assert output_dir == str(tmp_path / "raw" / "official_craft_runner")
        assert cwd == config["craft"]["repo_path"]
        assert "--oracle" in command
        assert "--no_tools" in command
        result_dir.mkdir(parents=True)
        (result_dir / "craft_structure_001_7.json").write_text(
            json.dumps({
                "experiment_info": {
                    "structure_index": 0,
                    "max_turns": 2,
                    "run": 7,
                    "models": {"director": "gpt-4o-mini", "builder": "gpt-4o-mini"},
                },
                "games": [{
                    "structure_id": "structure_001",
                    "target_structure": {"hidden": True},
                    "completed": True,
                    "final_progress": 1.0,
                    "turns": [{
                        "turn_number": 1,
                        "director_responses": {
                            "D1": {"public_message": "place red", "internal_thinking": "hidden"},
                        },
                        "oracle_moves": [{"hidden": True}],
                        "move_attempted": {"action": "place", "color": "red"},
                        "move_executed": True,
                        "progress_data": {"overall_progress": 0.5},
                    }],
                }],
            }),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr("benchmarks.craft.craft_env_adapter.subprocess.run", fake_run)

    adapter = CraftEnvAdapter(config, tmp_path)
    raw_result = adapter.run("official_baseline")

    assert raw_result["official_craft_runner"]["mode"] == "external_cli"
    assert raw_result["final_progress"] == 1.0
    assert raw_result["turns"][0]["director_messages"] == {"D1": "place red"}
    assert raw_result["turns"][0]["progress"] == {"overall_progress": 0.5}
    sanitized_runner_output = json.loads(
        (tmp_path / "raw" / "official_craft_runner" / "gpt-4o-mini_gpt-4o-mini" / "craft_structure_001_7.json").read_text(
            encoding="utf-8"
        )
    )
    serialized = json.dumps(sanitized_runner_output)
    assert "target_structure" not in serialized
    assert "oracle_moves" not in serialized
    assert "internal_thinking" not in serialized

    normalize_results(
        config=config,
        condition="official_baseline",
        raw_result=raw_result,
        output_dir=tmp_path,
    )
    summary = json.loads((tmp_path / "normalized" / "summary.json").read_text(encoding="utf-8"))
    assert summary["runtime"]["baseline_type"] == "full_official_runner"
