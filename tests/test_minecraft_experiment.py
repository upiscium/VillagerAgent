import json

from benchmarks.minecraft.experiment import run_minecraft_experiment


def test_minecraft_experiment_dry_run_writes_expected_artifacts(tmp_path):
    config_path = tmp_path / "minecraft_config.json"
    config_path.write_text(
        json.dumps({
            "task_type": "meta",
            "task_idx": 0,
            "agent_num": 1,
            "task_goal": "Find the village bell",
            "host": "127.0.0.1",
            "port": 25565,
            "task_name": "issue110_dry_run",
            "api_key": "secret",
        }),
        encoding="utf-8",
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="issue110",
        enable_dual_dag_task_selection=True,
    )

    output_dir = tmp_path / "result" / "issue110"
    assert summary["mode"] == "dry_run"
    assert summary["output_dir"] == str(output_dir)
    assert summary["dual_dag_task_selection_enabled"] is True
    assert summary["mutates_runtime"] is False
    assert summary["artifact_summary"]["task_node_count"] == 1
    assert summary["recommended_task_id"].startswith("minecraft:task:")
    assert (output_dir / "action_log.json").exists()
    assert (output_dir / "task_graph_snapshot.json").exists()
    assert (output_dir / "dual_dag_artifact.json").exists()
    assert (output_dir / "decision_support.json").exists()
    assert (output_dir / "summary.json").exists()

    launch_config = json.loads((output_dir / "launch_config.json").read_text(encoding="utf-8"))
    assert "api_key" not in launch_config


def test_minecraft_experiment_accepts_config_lists(tmp_path):
    config_path = tmp_path / "minecraft_config.json"
    config_path.write_text(
        json.dumps([
            {
                "task_type": "meta",
                "task_idx": 0,
                "agent_num": 1,
                "task_goal": "First task",
                "host": "127.0.0.1",
                "port": 25565,
                "task_name": "first",
            },
            {
                "task_type": "meta",
                "task_idx": 1,
                "agent_num": 2,
                "task_goal": "Second task",
                "host": "127.0.0.1",
                "port": 25565,
                "task_name": "second",
            },
        ]),
        encoding="utf-8",
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        config_index=1,
    )

    assert summary["run_name"] == "second"
    assert summary["task_idx"] == 1
    graph_snapshot = json.loads(
        (tmp_path / "result" / "second" / "task_graph_snapshot.json").read_text(encoding="utf-8")
    )
    assert graph_snapshot["mutates_runtime"] is False
    assert graph_snapshot["tasks"][0]["description"] == "Second task"
