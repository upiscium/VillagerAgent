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


def test_minecraft_experiment_records_enabled_task_reordering(tmp_path):
    config_path = tmp_path / "minecraft_config.json"
    config_path.write_text(
        json.dumps({
            "task_type": "meta",
            "task_idx": 0,
            "agent_num": 2,
            "task_goal": "Smoke compare task selection",
            "host": "127.0.0.1",
            "port": 25565,
            "task_name": "issue107",
            "smoke_tasks": [
                {
                    "id": "open_locked_door",
                    "description": "Open locked door",
                    "candidate_agents": ["Alice"],
                    "assigned_agents": ["Alice"],
                },
                {
                    "id": "find_chest",
                    "description": "Find chest",
                    "candidate_agents": ["Bob"],
                },
            ],
            "smoke_action_log": {
                "Alice": [{
                    "action": "openContainer",
                    "kwargs": {"player_name": "Alice", "item_name": "door"},
                    "result": {"status": False, "message": "door is locked"},
                }],
                "Bob": [{
                    "action": "talkTo",
                    "kwargs": {
                        "player_name": "Bob",
                        "entity_name": "Alice",
                        "message": "The chest is north of the door.",
                    },
                    "result": {"status": True},
                }],
            },
        }),
        encoding="utf-8",
    )

    disabled = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="disabled",
    )
    enabled = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="enabled",
        enable_dual_dag_task_selection=True,
    )

    assert disabled["task_order"] == disabled["ranked_task_order"]
    assert enabled["ranked_task_order"][0]["description"] == "Find chest"
    assert enabled["task_order"] != enabled["ranked_task_order"]
    assert enabled["recommended_description"] == "Find chest"
