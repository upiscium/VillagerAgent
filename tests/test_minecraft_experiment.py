import hashlib
import json
import os
import shlex
import signal
import sys
import time
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest
import yaml

from benchmarks.common.run_artifacts import (
    COMPLETION_MARKER_FILE,
    RunDirectoryExistsError,
    validate_run_attempt,
)
from benchmarks.minecraft.experiment import (
    MinecraftExecuteTimeoutError,
    MinecraftRuntimeChildError,
    _execute_real_runtime,
    _execute_real_runtime_bounded,
    _read_completed_runtime_result,
    _runtime_llm_config,
    _sanitize_retained_meta_judger_diagnostics,
    _public_bridge_cleanup,
    _public_runtime_process,
    _task_graph_from_config,
    _terminate_runtime_process,
    _cleanup_exited_runtime_process_group,
    _validate_child_status,
    _validate_runtime_cleanup,
    run_minecraft_experiment,
    task_graph_from_runtime_task_dag_snapshot,
    validate_experiment_artifact_admission,
    validate_minecraft_config,
)
from benchmarks.minecraft.metrics import build_minecraft_metrics
from benchmarks.minecraft.run_lock import (
    MinecraftTargetLock,
    MinecraftTargetLockReleaseOutcome,
    MinecraftTargetLockReleaseStatus,
    MinecraftTargetPredecessorHistoryStatus,
    clear_minecraft_target_quarantine,
    read_minecraft_target_predecessor_status,
)
from benchmarks.minecraft.events import (
    ATTEMPT_TERMINAL_EVENT_TYPES,
    finalize_attempt_events,
)
from benchmarks.common.report import summarize_inputs
from pipeline.runtime_events import (
    InMemoryRuntimeEventRecorder,
    JsonlRuntimeEventRecorder,
    read_runtime_events,
)
from env.runtime_execution import RuntimeAssetSpec, RuntimeExecution


def test_runtime_llm_config_preserves_fixed_model_and_endpoint_without_credential_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "ambient-secret-must-not-be-used")
    config = _runtime_llm_config({
        "api_model": "gemma4:12b", "api_base": "http://10.255.255.5:11434",
        "api_key_env": None,
    })

    assert config["api_model"] == "gemma4:12b"
    assert config["api_base"] == "http://10.255.255.5:11434/v1"
    assert config["api_key"] == "ollama"


def test_runtime_llm_config_rejects_partial_fixed_model_fields_without_key_env():
    with pytest.raises(RuntimeError, match="requires api_model and api_base"):
        _runtime_llm_config({"api_key_env": None, "api_model": "gemma4:12b"})


@pytest.fixture(autouse=True)
def _isolate_minecraft_lock_root(tmp_path, monkeypatch):
    monkeypatch.setenv("VILLAGER_MINECRAFT_LOCK_ROOT", str(tmp_path / "target-locks"))


@pytest.mark.parametrize("dependency_key", ["required_subtasks", "required subtasks"])
def test_minecraft_config_preserves_explicit_empty_dependencies_as_parallel(dependency_key):
    config = {
        "agent_num": 2,
        "smoke_tasks": [
            {"id": "a", "description": "A", dependency_key: []},
            {"id": "b", "description": "B", dependency_key: []},
        ],
    }

    tasks, graph, store = _task_graph_from_config(config)

    assert all(task._pre_idxs_explicit for task in tasks)
    assert graph.edge == []
    assert store.to_task_graph_projection().edge == []


def test_runtime_worker_reimports_modules_from_execution_root(tmp_path, monkeypatch):
    execution_root = tmp_path / "execution"
    execution_root.mkdir()
    (execution_root / "config.py").write_text("VALUE = 'approved'\n", encoding="utf-8")
    benchmark_package = execution_root / "benchmarks" / "minecraft"
    benchmark_package.mkdir(parents=True)
    (execution_root / "benchmarks" / "__init__.py").write_text("", encoding="utf-8")
    (benchmark_package / "__init__.py").write_text("", encoding="utf-8")
    (benchmark_package / "runtime_marker_486.py").write_text(
        "VALUE = 'approved-benchmark'\n", encoding="utf-8"
    )
    (execution_root / "start_with_config.py").write_text(
        "import config\n"
        "import os\n"
        "from benchmarks.minecraft import runtime_marker_486\n"
        "def run(*args, **kwargs):\n"
        "    return {\n"
        "        'attempt_id': kwargs['attempt_id'],\n"
        "        'task_name': args[11],\n"
        "        'module_value': config.VALUE,\n"
        "        'benchmark_value': runtime_marker_486.VALUE,\n"
        "        'cwd': os.getcwd(),\n"
        "        'pythonpath': os.environ.get('PYTHONPATH'),\n"
        "    }\n",
        encoding="utf-8",
    )
    execution = RuntimeExecution.resolve(
        execution_root,
        [
            RuntimeAssetSpec("start_with_config", "start_with_config.py"),
            RuntimeAssetSpec("config", "config.py"),
            RuntimeAssetSpec("benchmarks", "benchmarks/__init__.py"),
            RuntimeAssetSpec("minecraft", "benchmarks/minecraft/__init__.py"),
            RuntimeAssetSpec("benchmark_marker", "benchmarks/minecraft/runtime_marker_486.py"),
        ],
    )
    identity = {
        "category": "explicit_root",
        "manifest_sha256": execution.manifest_sha256,
    }
    monkeypatch.setitem(sys.modules, "config", SimpleNamespace(VALUE="hostile"))
    monkeypatch.setitem(
        sys.modules,
        "benchmarks.minecraft.runtime_marker_486",
        SimpleNamespace(VALUE="hostile-benchmark"),
    )
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._resolve_runtime_execution",
        lambda _root: (execution, identity),
    )
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._runtime_llm_config",
        lambda _config: {"api_model": "model", "api_base": "http://model"},
    )

    result = _execute_real_runtime_bounded(
        {
            "task_type": "construction",
            "task_idx": 0,
            "agent_num": 1,
            "task_goal": "goal",
            "host": "127.0.0.1",
            "port": 25565,
            "task_name": "runtime-root-test",
        },
        dual_dag_config={},
        timeout_seconds=5,
        runtime_result_path=tmp_path / "runtime" / "result.json",
        runtime_root=tmp_path / "runtime",
        attempt_id="attempt-runtime-root",
        runtime_execution=execution,
        runtime_execution_identity=identity,
    )

    assert result["module_value"] == "approved"
    assert result["benchmark_value"] == "approved-benchmark"
    assert result["cwd"] == str(execution_root)
    assert result["pythonpath"] == str(execution_root)


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
        command_text=[
            "python",
            "-m",
            "benchmarks.minecraft.experiment",
            "--config",
            str(config_path),
            "--output-root",
            str(tmp_path / "result"),
        ],
    )

    output_dir = tmp_path / "result" / "issue110"
    assert summary["mode"] == "dry_run"
    assert summary["output_dir"] == str(output_dir)
    assert summary["dual_dag_runtime_enabled"] is True
    assert summary["dual_dag_task_selection_enabled"] is True
    assert summary["runtime_task_store"] == "runtime_task_dag"
    assert summary["source_of_truth"] == "runtime_task_dag"
    assert summary["task_state_source"] == "config_fixture"
    assert summary["runtime_selection_policy"] == "dual-dag"
    assert summary["runtime_selected_task_ids"] == []
    assert summary["posthoc_ranked_task_order"] == summary["ranked_task_order"]
    assert summary["mutates_environment"] is False
    assert summary["artifact_generation_mutates_runtime"] is False
    assert summary["task_selection_mutates_order"] is True
    assert summary["task_order_changed"] is False
    assert summary["mutates_runtime"] is False
    assert summary["runtime_target_lock_admission"] == "not_applicable"
    assert summary["runtime_target_lock_metadata_valid"] is None
    assert summary["runtime_target_safe_to_reuse"] is True
    assert summary["artifact_summary"]["task_node_count"] == 1
    assert summary["recommended_task_id"].startswith("minecraft:task:")
    assert (output_dir / "action_log.json").exists()
    assert (output_dir / "task_graph_snapshot.json").exists()
    assert (output_dir / "runtime_dual_dag_snapshot.json").exists()
    assert (output_dir / "dual_dag_artifact.json").exists()
    assert (output_dir / "decision_support.json").exists()
    assert (output_dir / "metrics.json").exists()
    assert (output_dir / "summary.json").exists()
    assert (output_dir / "command.txt").exists()
    assert (output_dir / "config.resolved.json").exists()
    assert (output_dir / "provenance.json").exists()

    launch_config = json.loads((output_dir / "launch_config.json").read_text(encoding="utf-8"))
    assert "api_key" not in launch_config
    provenance = json.loads((output_dir / "provenance.json").read_text(encoding="utf-8"))
    persisted_summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert provenance["benchmark"] == "minecraft"
    assert provenance["schema_version"] == "2.0.0"
    assert provenance["lifecycle"]["status"] == "success"
    assert provenance["repository"]["dirty"] in {True, False}
    assert "secret" not in json.dumps(provenance)
    assert persisted_summary["output_dir"] == "."
    assert str(tmp_path) not in json.dumps(provenance)
    assert str(tmp_path) not in (output_dir / "command.txt").read_text(encoding="utf-8")
    assert not any(
        isinstance(value, str) and Path(value).is_absolute()
        for value in _nested_values(provenance)
    )
    runtime_snapshot = json.loads((output_dir / "runtime_dual_dag_snapshot.json").read_text(encoding="utf-8"))
    artifact = json.loads((output_dir / "dual_dag_artifact.json").read_text(encoding="utf-8"))
    decision_support = json.loads((output_dir / "decision_support.json").read_text(encoding="utf-8"))
    assert runtime_snapshot["source_of_truth"] == "runtime_task_dag"
    assert runtime_snapshot["snapshot_source"] == "config_fixture"
    assert runtime_snapshot["nodes"][0]["node_type"] == "runtime_task"
    assert artifact["task_state_source"] == "config_fixture"
    assert decision_support["task_state_source"] == "config_fixture"
    _assert_bundle_has_no_absolute_paths(output_dir)


def _nested_values(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from _nested_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nested_values(child)
    else:
        yield value


def _assert_bundle_has_no_absolute_paths(root: Path):
    values = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix == ".json":
            values.extend(_nested_values(json.loads(path.read_text(encoding="utf-8"))))
        elif path.suffix == ".jsonl":
            for line in path.read_text(encoding="utf-8").splitlines():
                values.extend(_nested_values(json.loads(line)))
        elif path.suffix in {".yaml", ".yml"}:
            values.extend(_nested_values(yaml.safe_load(path.read_text(encoding="utf-8"))))
        elif path.name == "command.txt":
            values.extend(shlex.split(path.read_text(encoding="utf-8")))

    absolute = [
        value
        for value in values
        if isinstance(value, str)
        and (Path(value).is_absolute() or PureWindowsPath(value).is_absolute())
    ]
    assert absolute == []


def test_minecraft_experiment_sanitizes_run_names(tmp_path):
    config_path = tmp_path / "minecraft_config.json"
    config_path.write_text(
        json.dumps({
            "task_type": "meta",
            "task_idx": 0,
            "agent_num": 1,
            "task_goal": "Find the village bell",
            "host": "127.0.0.1",
            "port": 25565,
            "task_name": "ignored",
        }),
        encoding="utf-8",
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="minecraft enabled local seed0 issue/109 smoke",
        command_text="python -m benchmarks.minecraft.experiment --config minecraft_config.json",
    )

    assert summary["run_name"] == "minecraft_enabled_local_seed0_issue_109_smoke"
    assert (tmp_path / "result" / summary["run_name"] / "command.txt").exists()


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


def test_minecraft_experiment_rejects_missing_required_fields(tmp_path):
    config_path = tmp_path / "minecraft_config.json"
    config_path.write_text(json.dumps({"task_type": "meta"}), encoding="utf-8")

    with pytest.raises(ValueError, match=r"missing required field\(s\): task_idx, agent_num, task_goal, host, port, task_name"):
        run_minecraft_experiment(config_path=config_path, output_root=tmp_path / "result")


def test_minecraft_experiment_rejects_config_index_out_of_range(tmp_path):
    config_path = tmp_path / "minecraft_config.json"
    config_path.write_text(json.dumps([_minecraft_config("first")]), encoding="utf-8")

    with pytest.raises(ValueError, match="Minecraft config index out of range: 1"):
        run_minecraft_experiment(config_path=config_path, output_root=tmp_path / "result", config_index=1)


def test_minecraft_experiment_rejects_negative_agent_count(tmp_path):
    config_path = tmp_path / "minecraft_config.json"
    config = _minecraft_config("bad_agents")
    config["agent_num"] = -1
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ValueError, match="config.agent_num must be non-negative"):
        run_minecraft_experiment(config_path=config_path, output_root=tmp_path / "result")


def test_minecraft_experiment_rejects_invalid_smoke_fixture_shape(tmp_path):
    config_path = tmp_path / "minecraft_config.json"
    config = _minecraft_config("bad_smoke")
    config["smoke_tasks"] = [{"id": "missing_description"}]
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ValueError, match=r"config.smoke_tasks\[0\] missing required field: description"):
        run_minecraft_experiment(config_path=config_path, output_root=tmp_path / "result")


def test_minecraft_execute_rejects_zero_agents():
    config = _minecraft_config("zero_agents")
    config["agent_num"] = 0

    with pytest.raises(ValueError, match="agent_num must be positive"):
        validate_minecraft_config(config, execute=True)


@pytest.mark.parametrize("value", [True, 1.5, "1"])
def test_minecraft_config_rejects_non_integer_agent_count(value):
    config = _minecraft_config("invalid_agents")
    config["agent_num"] = value

    with pytest.raises(ValueError, match="agent_num must be an integer"):
        validate_minecraft_config(config)


@pytest.mark.parametrize(
    ("task", "message"),
    [
        ({"description": "A", "candidate_agents": []}, "candidate_agents must be non-empty"),
        ({"description": "A", "assigned_agents": []}, "assigned_agents must be non-empty"),
        ({"description": "A", "candidate_agents": ["Alice"], "number": 2}, "must not exceed"),
        ({"description": "A", "assigned_agents": ["Alice", "Bob"], "number": 1}, "must match"),
        ({"description": "A", "candidate_agents": ["Alice"], "number": True}, "must be an integer"),
    ],
)
def test_minecraft_config_rejects_invalid_smoke_assignment(task, message):
    config = _minecraft_config("invalid_smoke")
    config["smoke_tasks"] = [task]

    with pytest.raises(ValueError, match=message):
        validate_minecraft_config(config)


def test_minecraft_meta_execute_requires_non_empty_task_scenario(tmp_path):
    config_path = _write_minecraft_config(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.pop("task_scenario")
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="config.task_scenario must be a non-empty string for meta execute mode",
    ):
        run_minecraft_experiment(
            config_path=config_path,
            output_root=tmp_path / "result",
            execute=True,
            execute_timeout_seconds=30,
        )
    assert not (tmp_path / "result").exists()


@pytest.mark.parametrize("document_file", [0, False, [], {}])
def test_minecraft_config_rejects_non_string_document_file(document_file):
    config = _minecraft_config("invalid_document")
    config["document_file"] = document_file

    with pytest.raises(ValueError, match="document_file must be a string or null"):
        validate_minecraft_config(config)


@pytest.mark.parametrize("timeout", [None, 0, -1, float("nan"), float("inf"), True])
def test_minecraft_execute_requires_positive_finite_timeout(tmp_path, timeout):
    config_path = _write_minecraft_config(tmp_path)
    output_root = tmp_path / "result"

    with pytest.raises(ValueError, match="positive finite timeout"):
        run_minecraft_experiment(
            config_path=config_path,
            output_root=output_root,
            execute=True,
            execute_timeout_seconds=timeout,
        )

    assert not output_root.exists()


def test_minecraft_meta_runtime_forwards_top_level_task_scenario(tmp_path, monkeypatch):
    captured = {}
    config = _minecraft_config("meta_forwarding")
    config["task_scenario"] = "move"
    config["evaluation_arg"] = {"target": [1, 2, 3]}
    config["position_convention"] = "entity_feet"

    monkeypatch.setattr(
        "model.ollama_config.make_ollama_llm_config",
        lambda: {
            "api_model": "model",
            "api_base": "http://example.test/v1",
            "api_key_list": [],
        },
    )

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"score": {}}

    monkeypatch.setattr("start_with_config.run", fake_run)

    result = _execute_real_runtime(
        config,
        dual_dag_config={"task_selection_policy": "dual-dag"},
        runtime_result_path=tmp_path / "runtime_result.json",
    )

    assert result == {"score": {}}
    assert captured["args"][14] == config["evaluation_arg"]
    assert captured["kwargs"]["task_scenario"] == "move"
    assert captured["kwargs"]["emit_controller_terminal_event"] is False
    assert captured["kwargs"]["position_convention"] == "entity_feet"


def test_eac_nonjudged_fixture_wires_premanifest_and_revision_without_execution(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "docs/eac/minecraft_eac_nonjudged_fixture_v1.json").read_text())
    captured = {}
    monkeypatch.setattr(
        "model.ollama_config.make_ollama_llm_config",
        lambda **kwargs: {"api_model": kwargs.get("api_model", "model"),
                          "api_base": kwargs.get("api_base", "http://example.test/v1"),
                          "api_key_list": []},
    )
    monkeypatch.setattr("start_with_config.run", lambda *args, **kwargs: captured.update(kwargs) or {})

    _execute_real_runtime(
        config,
        dual_dag_config={"eac_mode": "dual_dag_authority"},
        runtime_result_path=tmp_path / "runtime_result.json",
    )

    eac = captured["minecraft_dual_dag_config"]
    assert eac["eac_premanifest"] == "docs/eac/minecraft_eac_premanifest_v1.json"
    assert len(eac["eac_execution_revision"]) == 40
    assert eac["judged_execution"] is False and eac["production"] is False


@pytest.mark.parametrize("field", ["judged_execution", "production"])
def test_eac_execute_rejects_judged_or_production_launch(field, tmp_path):
    config = _minecraft_config("eac_rejected")
    config.update({
        "eac_premanifest": "docs/eac/minecraft_eac_premanifest_v1.json",
        "eac_execution_revision": "0" * 40,
        "judged_execution": False,
        "production": False,
    })
    config[field] = True
    with pytest.raises(ValueError, match="non-judged, non-production"):
        _execute_real_runtime(
            config,
            dual_dag_config={"eac_mode": "dual_dag_authority"},
            runtime_result_path=tmp_path / "runtime_result.json",
        )


def test_minecraft_experiment_emits_one_terminal_event_when_finalization_raises(tmp_path, monkeypatch):
    config_path = tmp_path / "minecraft_config.json"
    config_path.write_text(json.dumps(_minecraft_config("terminal_once")), encoding="utf-8")
    sink = InMemoryRuntimeEventRecorder("terminal_once")
    finalize_calls = 0
    real_finalize = __import__(
        "benchmarks.minecraft.experiment",
        fromlist=["finalize_run_directory"],
    ).finalize_run_directory

    def fail_first_finalization(*args, **kwargs):
        nonlocal finalize_calls
        finalize_calls += 1
        if finalize_calls == 1:
            raise RuntimeError("finalization failed")
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(
        "benchmarks.minecraft.experiment.finalize_run_directory",
        fail_first_finalization,
    )

    with pytest.raises(RuntimeError, match="finalization failed"):
        run_minecraft_experiment(
            config_path=config_path,
            output_root=tmp_path / "result",
            event_sink=sink,
        )

    terminal_events = [
        event["event_type"]
        for event in sink.events
        if event["event_type"] in {"run_completed", "run_failed", "run_timed_out"}
    ]
    assert terminal_events == ["run_failed"]
    run_dir = tmp_path / "result" / "terminal_once"
    events = _read_public_events(run_dir)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    provenance = json.loads((run_dir / "provenance.json").read_text(encoding="utf-8"))
    assert events[-1]["event_type"] == "run_failed"
    assert events[-1]["payload"]["error"] == "finalization failed"
    assert summary["terminal_event_type"] == "run_failed"
    assert summary["error"] == "finalization failed"
    assert provenance["lifecycle"]["status"] == "failure"


def test_minecraft_manifest_captures_final_event_artifacts(tmp_path, monkeypatch):
    config_path = tmp_path / "minecraft_config.json"
    config_path.write_text(json.dumps(_minecraft_config("manifest_terminal")), encoding="utf-8")
    build_calls = 0

    def build_events(**_kwargs):
        nonlocal build_calls
        build_calls += 1
        return SimpleNamespace(
            events=[],
            warnings=[],
        )

    monkeypatch.setattr("benchmarks.minecraft.experiment.build_normalized_events", build_events)

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        event_sink=JsonlRuntimeEventRecorder(tmp_path / "runtime_events.jsonl", run_id="test"),
    )

    output_dir = tmp_path / "result" / "manifest_terminal"
    validate_run_attempt(output_dir, attempt_id=summary["attempt_id"])
    events = [json.loads(line) for line in (output_dir / "events.jsonl").read_text().splitlines()]
    assert build_calls == 1
    assert [event["event_type"] for event in events] == ["run_started", "run_completed"]


def test_required_artifact_admission_accepts_complete_judged_run():
    summary, snapshot, action_log = _artifact_admission_fixture()
    normalized_events = _admitted_event_fixture()

    admission = validate_experiment_artifact_admission(
        summary=summary,
        runtime_snapshot=snapshot,
        action_log=action_log,
        required_artifacts={
            "score",
            "runtime_task_dag",
            "action_log",
            "events",
            "bridge_cleanup",
            "child_protocol",
        },
        normalized_events=normalized_events,
        expected_terminal_event_type="run_completed",
        expected_run_id="run",
        expected_attempt_id="attempt-a",
    )

    assert admission["passed"] is True
    assert admission["missing"] == []
    assert admission["invalid"] == []


@pytest.mark.parametrize(
    ("mutation", "problem"),
    [
        (lambda summary, _snapshot, _log: summary.update(runtime_collection_errors=[{"error": "bad"}]), "runtime_collection_errors"),
        (lambda _summary, _snapshot, log: log.update(Alice=[]), "action_log"),
        (lambda summary, _snapshot, _log: summary.update(bridge_cleanup={}), "bridge_cleanup"),
        (lambda summary, _snapshot, _log: summary["bridge_cleanup"].update(cleanup_complete=False), "bridge_cleanup"),
        (lambda summary, _snapshot, _log: summary.update(runtime_target_safe_to_reuse=False), "runtime_target"),
        (lambda summary, _snapshot, _log: summary["child_protocol"].update(result_written=False), "child_protocol"),
        (lambda summary, _snapshot, _log: summary.update(events_available=False), "events"),
        (lambda summary, _snapshot, _log: summary.update(event_artifact_error="RuntimeError"), "events"),
    ],
)
def test_required_artifact_admission_rejects_missing_or_invalid_evidence(mutation, problem):
    summary, snapshot, action_log = _artifact_admission_fixture()
    mutation(summary, snapshot, action_log)

    admission = validate_experiment_artifact_admission(
        summary=summary,
        runtime_snapshot=snapshot,
        action_log=action_log,
        required_artifacts={
            "score",
            "runtime_task_dag",
            "action_log",
            "events",
            "bridge_cleanup",
            "child_protocol",
        },
    )

    assert admission["passed"] is False
    assert problem in admission["missing"] + admission["invalid"]


@pytest.mark.parametrize("events_required", [False, True])
def test_event_generation_failure_respects_required_policy(tmp_path, monkeypatch, events_required):
    config_path = tmp_path / "minecraft_config.json"
    config = _minecraft_config(f"events_{events_required}")
    if events_required:
        config["required_artifacts"] = ["events"]
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment.build_normalized_events",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("event conversion failed")),
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
    )

    run_dir = Path(summary["output_dir"])
    assert summary["event_artifact_error"] == "RuntimeError"
    assert summary["artifact_admission"]["passed"] is (not events_required)
    assert summary["error_type"] == ("RequiredArtifactError" if events_required else "")
    assert (run_dir / COMPLETION_MARKER_FILE).exists() is (not events_required)
    manifest = json.loads((run_dir / "artifact_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == ("failed" if events_required else "completed")


def test_invalid_event_lifecycle_rejects_required_event_artifact(tmp_path, monkeypatch):
    config_path = tmp_path / "minecraft_config.json"
    config = _minecraft_config("invalid_lifecycle")
    config["required_artifacts"] = ["events"]
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment.finalize_attempt_events",
        lambda *_args, **_kwargs: SimpleNamespace(
            events=({"event_type": "run_started"},),
            warnings=(),
        ),
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
    )

    run_dir = Path(summary["output_dir"])
    assert summary["events_available"] is False
    assert summary["event_artifact_error"] == "EventLifecycleConsistencyError"
    assert summary["error_type"] == "RequiredArtifactError"
    assert summary["artifact_admission"]["passed"] is False
    assert not (run_dir / "events.jsonl").exists()
    assert not (run_dir / COMPLETION_MARKER_FILE).exists()


@pytest.mark.parametrize("events_required", [False, True])
def test_atomic_event_write_failure_respects_required_policy(
    tmp_path,
    monkeypatch,
    events_required,
):
    config_path = tmp_path / "minecraft_config.json"
    config = _minecraft_config(f"event_write_{events_required}")
    if events_required:
        config["required_artifacts"] = ["events"]
    config_path.write_text(json.dumps(config), encoding="utf-8")
    real_replace = os.replace

    def fail_event_replace(source, destination):
        if Path(destination).name == "events.jsonl":
            raise OSError("event replace failed")
        return real_replace(source, destination)

    monkeypatch.setattr("benchmarks.minecraft.experiment.os.replace", fail_event_replace)

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
    )

    run_dir = Path(summary["output_dir"])
    assert summary["events_available"] is False
    assert summary["event_artifact_error"] == "OSError"
    assert summary["error_type"] == ("RequiredArtifactError" if events_required else "")
    assert not (run_dir / "events.jsonl").exists()
    assert not (run_dir / "events.jsonl.tmp").exists()
    assert (run_dir / COMPLETION_MARKER_FILE).exists() is (not events_required)


def test_canonical_events_do_not_depend_on_external_event_sink(tmp_path):
    class BrokenSink:
        def emit(self, *_args, **_kwargs):
            raise OSError("notification unavailable")

    config_path = tmp_path / "minecraft_config.json"
    config_path.write_text(json.dumps(_minecraft_config("broken_sink")), encoding="utf-8")

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        event_sink=BrokenSink(),
    )

    events = _read_public_events(Path(summary["output_dir"]))
    assert summary["events_available"] is True
    assert events[0]["event_type"] == "run_started"
    assert events[-1]["event_type"] == "run_completed"


def test_events_only_policy_writes_failed_terminal_when_runtime_guard_fails(tmp_path):
    config_path = tmp_path / "minecraft_config.json"
    config = _minecraft_config("events_only_guard")
    config["required_artifacts"] = ["events"]
    config_path.write_text(json.dumps(config), encoding="utf-8")

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
    )

    run_dir = Path(summary["output_dir"])
    events = _read_public_events(run_dir)
    provenance = json.loads((run_dir / "provenance.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "artifact_manifest.json").read_text(encoding="utf-8"))
    assert summary["error_type"] == "RequiredArtifactError"
    assert summary["artifact_admission"]["passed"] is False
    assert summary["terminal_event_type"] == "run_failed"
    assert summary["event_lifecycle_valid"] is True
    assert events[0]["event_type"] == "run_started"
    assert events[-1]["event_type"] == "run_failed"
    assert events[-1]["payload"]["error_type"] == "RequiredArtifactError"
    assert sum(
        event["event_type"] in ATTEMPT_TERMINAL_EVENT_TYPES
        for event in events
    ) == 1
    assert provenance["lifecycle"]["status"] == "failure"
    assert manifest["status"] == "failed"
    assert not (run_dir / COMPLETION_MARKER_FILE).exists()


def test_late_admission_failure_rewrites_completed_terminal_to_failed(
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "minecraft_config.json"
    config_path.write_text(json.dumps(_minecraft_config("late_failure")), encoding="utf-8")
    real_validate = validate_experiment_artifact_admission
    calls = 0

    def fail_final_admission(**kwargs):
        nonlocal calls
        calls += 1
        admission = real_validate(**kwargs)
        if calls == 2:
            return {
                **admission,
                "passed": False,
                "invalid": [*admission["invalid"], "late_failure"],
            }
        return admission

    monkeypatch.setattr(
        "benchmarks.minecraft.experiment.validate_experiment_artifact_admission",
        fail_final_admission,
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
    )

    run_dir = Path(summary["output_dir"])
    events = _read_public_events(run_dir)
    provenance = json.loads((run_dir / "provenance.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "artifact_manifest.json").read_text(encoding="utf-8"))
    assert summary["error_type"] == "RequiredArtifactError"
    assert summary["terminal_event_type"] == "run_failed"
    assert events[-1]["event_type"] == "run_failed"
    assert sum(
        event["event_type"] in ATTEMPT_TERMINAL_EVENT_TYPES
        for event in events
    ) == 1
    assert provenance["lifecycle"]["status"] == "failure"
    assert manifest["status"] == "failed"
    assert not (run_dir / COMPLETION_MARKER_FILE).exists()


def test_dry_run_does_not_import_previous_external_sink_events(tmp_path):
    external_journal = tmp_path / "external.jsonl"
    sink = JsonlRuntimeEventRecorder(
        external_journal,
        run_id="previous-run",
        durable=False,
    )
    sink.emit(
        "task_status_changed",
        entity_id="previous-task",
        source="previous-controller",
        payload={"status": "success"},
    )
    config_path = tmp_path / "minecraft_config.json"
    config_path.write_text(json.dumps(_minecraft_config("current-run")), encoding="utf-8")

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="current-run",
        event_sink=sink,
    )

    public_events = _read_public_events(Path(summary["output_dir"]))
    assert all(event.get("entity_id") != "previous-task" for event in public_events)
    assert all(event.get("source") != "previous-controller" for event in public_events)
    assert public_events[0]["event_type"] == "run_started"
    assert public_events[-1]["event_type"] == "run_completed"
    external_events = read_runtime_events(external_journal).events
    assert any(event["event_type"] == "run_started" for event in external_events)


def test_reused_external_sink_does_not_cross_contaminate_public_runs(tmp_path):
    external_journal = tmp_path / "external.jsonl"
    sink = JsonlRuntimeEventRecorder(
        external_journal,
        run_id="notifications",
        durable=False,
    )
    sink.emit(
        "task_status_changed",
        entity_id="external-only-task",
        source="external-controller",
        payload={"run": "before-both"},
    )
    config_path = tmp_path / "minecraft_config.json"
    config_path.write_text(json.dumps(_minecraft_config("reused_sink")), encoding="utf-8")

    first = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="first-run",
        event_sink=sink,
    )
    second = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="second-run",
        event_sink=sink,
    )

    first_events = _read_public_events(Path(first["output_dir"]))
    second_events = _read_public_events(Path(second["output_dir"]))
    assert all(event["run_id"] == "first-run" for event in first_events)
    assert all(event["run_id"] == "second-run" for event in second_events)
    assert all(event.get("entity_id") != "external-only-task" for event in first_events)
    assert all(event.get("entity_id") != "external-only-task" for event in second_events)
    assert first_events[-1]["event_type"] == "run_completed"
    assert second_events[-1]["event_type"] == "run_completed"


def test_minecraft_non_meta_execute_does_not_require_task_scenario(tmp_path, monkeypatch):
    config_path = tmp_path / "minecraft_config.json"
    config = _minecraft_config("construction_execute")
    config["task_type"] = "construction"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime",
        lambda *args, **kwargs: {
            "bridge_cleanup": {"cleanup_complete": True, "processes": {}},
        },
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        execute=True,
        execute_timeout_seconds=30,
    )

    assert summary["error"] is None


def test_non_meta_execute_requires_safe_target_independent_of_artifact_policy(
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "minecraft_config.json"
    config = _minecraft_config("construction_unsafe_cleanup")
    config.update({
        "task_type": "construction",
        "required_artifacts": [],
    })
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime_bounded",
        lambda *_args, **_kwargs: {
            "action_log": {},
            "runtime_process": _safe_runtime_process_metadata(),
            "bridge_cleanup": {},
        },
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="construction_unsafe_cleanup",
        execute=True,
        execute_timeout_seconds=30,
    )

    run_dir = Path(summary["output_dir"])
    assert summary["error_type"] == "MinecraftTargetCleanupError"
    assert summary["runtime_target_safe_to_reuse"] is False
    assert summary["runtime_target_quarantined"] is True
    assert summary["terminal_event_type"] == "run_failed"
    assert _read_public_events(run_dir)[-1]["event_type"] == "run_failed"
    assert json.loads((run_dir / "artifact_manifest.json").read_text(encoding="utf-8"))["status"] == "failed"
    assert not (run_dir / COMPLETION_MARKER_FILE).exists()


def test_minecraft_experiment_records_task_selection_policy_ablation(tmp_path):
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
        run_name="original",
        task_selection_policy="original",
    )
    enabled = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="dual_dag",
        task_selection_policy="dual-dag",
    )

    assert disabled["dual_dag_runtime_enabled"] is True
    assert disabled["dual_dag_task_selection_enabled"] is False
    assert disabled["task_selection_policy"] == "original"
    assert disabled["source_of_truth"] == "runtime_task_dag"
    assert disabled["ranked_task_order"][0]["description"] == "Open locked door"
    assert enabled["dual_dag_task_selection_enabled"] is True
    assert enabled["task_selection_policy"] == "dual-dag"
    assert enabled["ranked_task_order"][0]["description"] == "Find chest"
    assert disabled["task_order"] == disabled["ranked_task_order"]
    assert enabled["task_order"] != enabled["ranked_task_order"]
    assert disabled["task_selection_mutates_order"] is False
    assert disabled["task_order_changed"] is False
    assert enabled["task_selection_mutates_order"] is True
    assert enabled["task_order_changed"] is True
    assert enabled["recommended_description"] == "Find chest"


def test_minecraft_experiment_rejects_unknown_task_selection_policy(tmp_path):
    config_path = _write_minecraft_config(tmp_path)

    with pytest.raises(ValueError, match="Unsupported task_selection_policy"):
        run_minecraft_experiment(
            config_path=config_path,
            output_root=tmp_path / "result",
            task_selection_policy="bad-policy",
        )

    run_dir = tmp_path / "result" / "bounded_execute"
    provenance = json.loads((run_dir / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["lifecycle"]["status"] == "failure"
    assert provenance["effective_settings"]["task_selection_policy"] == "bad-policy"


def test_minecraft_metrics_extracts_representative_counts_without_secrets():
    metrics = build_minecraft_metrics(
        summary={
            "run_name": "metrics",
            "mode": "dry_run",
            "recommended_task_id": "minecraft:task:find_chest",
            "selected_task_id": "minecraft:task:find_chest",
            "progress": 0.5,
            "mutates_environment": False,
            "artifact_generation_mutates_runtime": False,
            "task_selection_mutates_order": True,
            "task_order_changed": True,
            "api_key": "secret",
        },
        action_log={
            "Alice": [{
                "action": "openContainer",
                "duration": 2.0,
                "kwargs": {"api_key": "secret"},
                "result": {"status": False, "message": "retry after locked door"},
            }],
            "Bob": [{
                "action": "talkTo",
                "duration": 3.0,
                "result": {"status": True},
            }],
        },
        task_graph_snapshot={
            "tasks": [
                {"description": "Open locked door", "status": "failure"},
                {"description": "Find chest", "status": "success"},
            ]
        },
        decision_support={"mutates_runtime": False},
    )

    assert metrics["schema_version"] == "1.0.0"
    assert metrics["task_completion_rate"] == 0.5
    assert metrics["action_count"] == 2
    assert metrics["failed_action_count"] == 1
    assert metrics["retry_replan_count"] == 1
    assert metrics["time_to_completion"] == 5.0
    assert metrics["recommendation_adopted_count"] == 1
    assert metrics["mutates_environment"] is False
    assert metrics["artifact_generation_mutates_runtime"] is False
    assert metrics["task_selection_mutates_order"] is True
    assert metrics["task_order_changed"] is True
    assert "api_key" not in json.dumps(metrics)


def test_minecraft_execute_preserves_artifacts_on_runtime_error(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)

    def fail_runtime(*args, **kwargs):
        raise RuntimeError("server unavailable")

    monkeypatch.setattr("benchmarks.minecraft.experiment._execute_real_runtime", fail_runtime)

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="execute_error",
        execute=True,
        execute_timeout_seconds=30,
    )

    output_dir = tmp_path / "result" / "execute_error"
    assert summary["mode"] == "execute"
    assert summary["execute_real_environment"] is True
    assert summary["mutates_environment"] is True
    assert summary["artifact_generation_mutates_runtime"] is False
    assert summary["execute_timeout_seconds"] == 30
    assert summary["error"] == "server unavailable"
    assert summary["error_type"] == "RuntimeError"
    assert summary["timed_out"] is False
    assert summary["runtime_process_isolated"] is True
    assert summary["runtime_process_exit_code"] != 0
    assert summary["runtime_task_name"].startswith("bounded_execute_")
    assert (output_dir / "summary.json").exists()
    assert (output_dir / "metrics.json").exists()
    assert (output_dir / "judged_iteration_trace.json").exists()
    terminal_diagnostics = json.loads(
        (output_dir / "judged_terminal_diagnostics.json").read_text(encoding="utf-8")
    )
    assert terminal_diagnostics["score_status"] is None
    assert terminal_diagnostics["root_cause_category"] == "runtime_internal_error"
    manifest = validate_run_attempt(
        output_dir,
        attempt_id=summary["attempt_id"],
        require_completed=False,
    )
    assert manifest["status"] == "failed"
    assert "judged_iteration_trace.json" in {
        artifact["path"] for artifact in manifest["artifacts"]
    }
    assert "judged_terminal_diagnostics.json" in {
        artifact["path"] for artifact in manifest["artifacts"]
    }
    assert not (output_dir / COMPLETION_MARKER_FILE).exists()
    _assert_bundle_has_no_absolute_paths(output_dir)
    metrics = json.loads((output_dir / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["error"] == "server unavailable"
    assert metrics["mutates_environment"] is True


def test_minecraft_execute_uses_real_runtime_snapshot(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)

    def runtime_result(*args, **kwargs):
        return _runtime_result_snapshot(status="success")

    monkeypatch.setattr("benchmarks.minecraft.experiment._execute_real_runtime", runtime_result)

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="execute_real_snapshot",
        execute=True,
        execute_timeout_seconds=30,
    )

    output_dir = tmp_path / "result" / "execute_real_snapshot"
    runtime_snapshot = json.loads((output_dir / "runtime_dual_dag_snapshot.json").read_text(encoding="utf-8"))
    assert summary["snapshot_source"] == "real_runtime"
    assert runtime_snapshot["snapshot_source"] == "real_runtime"
    assert runtime_snapshot["nodes"][0]["lifecycle"]["status"] == "success"
    assert summary["runtime_process_isolated"] is True
    assert summary["runtime_process_exit_code"] == 0
    assert summary["runtime_process_terminated"] is False
    assert summary["runtime_process_killed"] is False
    assert summary["runtime_process_alive_after_kill"] is False
    assert summary["runtime_process_group_alive_after_kill"] is False
    assert summary["runtime_target_lock_admission"] == "granted"


def test_runtime_task_snapshot_adapter_restores_tasks_edges_and_lifecycle_metadata():
    snapshot = _runtime_result_snapshot(status="running")["runtime_task_dag_snapshot"]
    snapshot["nodes"].append({
        "node_id": "runtime:task:second",
        "node_type": "runtime_task",
        "content": {
            "description": "Second runtime task",
            "metadata": {"kind": "follow_up"},
            "milestones": ["done"],
            "reflect": {"ok": True},
        },
        "lifecycle": {
            "status": "unknown",
            "candidate_agents": ["Bob"],
            "active_agents": [],
            "last_assigned_agents": [],
            "required_agent_count": 1,
        },
        "derived": {"dependency_ready": False, "blocked_by_tasks": ["runtime:task:mock"]},
        "provenance": {"source": "test_replan"},
    })
    snapshot["edges"] = [{
        "source_id": "runtime:task:mock",
        "target_id": "runtime:task:second",
        "edge_type": "precedes_task",
        "metadata": {},
    }]

    tasks, graph = task_graph_from_runtime_task_dag_snapshot(snapshot)

    assert [(task.id, task.description, task.status) for task in tasks] == [
        ("mock", "Runtime task", "running"),
        ("second", "Second runtime task", "unknown"),
    ]
    assert tasks[0].candidate_list == ["Alice"]
    assert tasks[0]._agent == ["Alice"]
    assert tasks[0].number == 1
    assert tasks[0].content["runtime_snapshot"]["last_assigned_agents"] == ["Alice"]
    assert tasks[1].content["runtime_snapshot"]["provenance"] == {"source": "test_replan"}
    assert [(start.id, end.id) for start, end in graph.edge] == [("mock", "second")]


def test_minecraft_execute_builds_task_artifacts_from_real_runtime_state(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)

    def successful_runtime(launch_config, *args, **kwargs):
        result = _runtime_result_snapshot(status="success")
        result.update({
            "score": {
                "attempt_id": kwargs["attempt_id"],
                "task_name": launch_config["task_name"],
                "status": "success",
                "score": 100,
            },
            "controller": {"shutdown_complete": True, "active_assignments": {}},
            "bridge_cleanup": {"cleanup_complete": True, "processes": {}},
            "collection_errors": [],
        })
        return result

    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime",
        successful_runtime,
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="runtime_artifact_source",
        execute=True,
            execute_timeout_seconds=30,
    )

    output_dir = tmp_path / "result" / "runtime_artifact_source"
    artifact = json.loads((output_dir / "dual_dag_artifact.json").read_text(encoding="utf-8"))
    decision_support = json.loads((output_dir / "decision_support.json").read_text(encoding="utf-8"))
    task_nodes = [node for node in artifact["nodes"] if node["node_type"] == "minecraft_task"]
    assert artifact["task_state_source"] == "real_runtime"
    assert [node["content"]["description"] for node in task_nodes] == ["Runtime task"]
    assert task_nodes[0]["content"]["status"] == "success"
    assert task_nodes[0]["content"]["last_assigned_agents"] == ["Alice"]
    assert task_nodes[0]["content"]["metadata"]["runtime_snapshot"]["last_assigned_agents"] == ["Alice"]
    assert decision_support["task_state_source"] == "real_runtime"
    assert [candidate["description"] for candidate in decision_support["candidates"]] == ["Runtime task"]
    assert summary["task_state_source"] == "real_runtime"
    assert summary["task_order"][0]["description"] == "Runtime task"
    assert summary["runtime_selection_policy"] == "dual-dag"
    assert summary["runtime_selected_task_ids"] == []
    assert summary["posthoc_ranked_task_order"][0]["description"] == "Runtime task"
    assert summary["selected_task_id"] == ""
    assert any(edge["edge_type"] == "task_invokes_action" for edge in artifact["edges"])
    metrics = json.loads((output_dir / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["task_completion_rate"] == 1.0
    events = _read_public_events(output_dir)
    assert events[0]["event_type"] == "run_started"
    assert events[-1]["event_type"] == "run_completed"
    assert events[-1]["payload"]["attempt_id"] == summary["attempt_id"]
    assert summary["terminal_event_type"] == "run_completed"
    assert summary["event_lifecycle_valid"] is True


def test_minecraft_execute_uses_recorded_runtime_selection_history(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)
    runtime_result = _runtime_result_snapshot(status="success")
    runtime_result["runtime_selected_task_ids"] = ["runtime:task:mock"]
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime",
        lambda *args, **kwargs: runtime_result,
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="runtime_selection_history",
        execute=True,
            execute_timeout_seconds=30,
    )

    assert summary["runtime_selected_task_ids"] == ["runtime:task:mock"]
    assert summary["selected_task_id"] == "runtime:task:mock"
    assert summary["selected_description"] == "Runtime task"


def test_minecraft_execute_failure_uses_partial_runtime_snapshot(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)

    def fail_with_partial(*args, **kwargs):
        runtime_result_path = Path(kwargs["runtime_result_path"])
        runtime_result_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_result_path.write_text(json.dumps(_runtime_result_snapshot(status="running")), encoding="utf-8")
        raise RuntimeError("server unavailable")

    monkeypatch.setattr("benchmarks.minecraft.experiment._execute_real_runtime", fail_with_partial)

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="execute_partial_error",
        execute=True,
            execute_timeout_seconds=30,
    )

    output_dir = tmp_path / "result" / "execute_partial_error"
    runtime_snapshot = json.loads((output_dir / "runtime_dual_dag_snapshot.json").read_text(encoding="utf-8"))
    assert summary["error_type"] == "RuntimeError"
    assert summary["snapshot_source"] == "real_runtime"
    assert runtime_snapshot["nodes"][0]["lifecycle"]["status"] == "running"
    events = _read_public_events(output_dir)
    assert events[-1]["event_type"] == "run_failed"
    assert events[-1]["payload"]["error"] == "server unavailable"
    assert events[-1]["payload"]["error_type"] == "RuntimeError"
    provenance = json.loads((output_dir / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["lifecycle"]["status"] == "failure"
    assert json.loads((output_dir / "artifact_manifest.json").read_text())["status"] == "failed"


def test_minecraft_execute_timeout_preserves_artifacts(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)

    def slow_runtime(*args, **kwargs):
        time.sleep(1)

    monkeypatch.setattr("benchmarks.minecraft.experiment._execute_real_runtime", slow_runtime)

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="execute_timeout",
        execute=True,
        execute_timeout_seconds=0.01,
    )

    output_dir = tmp_path / "result" / "execute_timeout"
    assert summary["error_type"] == "timeout"
    assert summary["timed_out"] is True
    assert summary["server_lock_acquired"] is True
    assert summary["server_lock_released"] is True
    assert summary["server_lock_release_outcome"]["status"] == "verified_released"
    assert summary["server_lock_release_outcome"]["verified_released"] is True
    assert (output_dir / "server_lock_release.json").exists()
    assert summary["runtime_process_isolated"] is True
    assert summary["runtime_process_terminated"] is True
    assert summary["runtime_process_alive_after_kill"] is False
    assert summary["runtime_process_group_alive_after_kill"] is False
    assert summary["child_protocol"].get("status") != "completed"
    assert "timed out after 0.01 seconds" in summary["error"]
    assert (output_dir / "action_log.json").exists()
    persisted = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert persisted["timed_out"] is True
    provenance = json.loads((output_dir / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["lifecycle"]["status"] == "timeout"
    assert provenance["environment_unverifiable"] is True
    common_rows = summarize_inputs([output_dir])
    assert common_rows[0]["error_type"] == "timeout"
    events = _read_public_events(output_dir)
    assert events[-1]["event_type"] == "run_timed_out"
    assert events[-1]["payload"]["attempt_id"] == summary["attempt_id"]
    assert summary["terminal_event_type"] == "run_timed_out"


@pytest.mark.parametrize(
    "release_status",
    [
        MinecraftTargetLockReleaseStatus.NOT_ACQUIRED,
        MinecraftTargetLockReleaseStatus.FAILED,
        MinecraftTargetLockReleaseStatus.UNCERTAIN,
    ],
)
def test_nonverified_target_lock_release_denies_reuse_and_fails_attempt(
    tmp_path,
    monkeypatch,
    release_status,
):
    config_path = _write_minecraft_config(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.update({"task_type": "construction", "required_artifacts": []})
    config_path.write_text(json.dumps(config), encoding="utf-8")
    _install_target_lock_release_outcome(
        monkeypatch,
        status=release_status,
        error=f"private lock path: {tmp_path / 'secret-lock'}",
    )
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime_bounded",
        lambda *_args, **_kwargs: {
            "action_log": {},
            "runtime_process": _safe_runtime_process_metadata(),
            "bridge_cleanup": {"cleanup_complete": True, "processes": {}},
        },
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="nonverified_release",
        execute=True,
        execute_timeout_seconds=30,
    )

    run_dir = Path(summary["output_dir"])
    release = summary["server_lock_release_outcome"]
    assert release["status"] == release_status.value
    assert release["verified_released"] is False
    assert release["error_type"] == "OSError"
    assert str(tmp_path) not in json.dumps(release)
    assert json.loads((run_dir / "server_lock_release.json").read_text()) == {
        **release,
        "attempt_id": summary["attempt_id"],
    }
    persisted_summary = json.loads((run_dir / "summary.json").read_text())
    assert persisted_summary["server_lock_release_outcome"] == release
    assert summary["server_lock_released"] is False
    assert summary["runtime_target_safe_to_reuse"] is False
    assert summary["error_type"] == "MinecraftTargetLockReleaseError"
    assert summary["terminal_event_type"] == "run_failed"
    assert json.loads((run_dir / "artifact_manifest.json").read_text())["status"] == "failed"
    assert not (run_dir / COMPLETION_MARKER_FILE).exists()


@pytest.mark.parametrize(
    ("runtime_failure", "expected_error_type", "expected_timed_out"),
    [
        ("runtime", "RuntimeError", False),
        ("timeout", "timeout", True),
    ],
)
def test_target_lock_release_failure_does_not_replace_primary_runtime_failure(
    tmp_path,
    monkeypatch,
    runtime_failure,
    expected_error_type,
    expected_timed_out,
):
    config_path = _write_minecraft_config(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.update({"task_type": "construction", "required_artifacts": []})
    config_path.write_text(json.dumps(config), encoding="utf-8")
    _install_target_lock_release_outcome(
        monkeypatch,
        status=MinecraftTargetLockReleaseStatus.UNCERTAIN,
        error=f"private lock path: {tmp_path / 'secret-lock'}",
    )

    def fail_runtime(*_args, **_kwargs):
        if runtime_failure == "timeout":
            raise MinecraftExecuteTimeoutError("primary runtime timeout")
        raise RuntimeError("primary runtime failure")

    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime_bounded",
        fail_runtime,
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name=f"release_after_{runtime_failure}",
        execute=True,
        execute_timeout_seconds=30,
    )

    run_dir = Path(summary["output_dir"])
    expected_error = (
        "primary runtime timeout"
        if expected_timed_out
        else "primary runtime failure"
    )
    expected_terminal = "run_timed_out" if expected_timed_out else "run_failed"
    release = summary["server_lock_release_outcome"]
    assert summary["error"] == expected_error
    assert summary["error_type"] == expected_error_type
    assert summary["timed_out"] is expected_timed_out
    assert summary["terminal_event_type"] == expected_terminal
    assert release["status"] == "uncertain"
    assert release["verified_released"] is False
    assert release["error_type"] == "OSError"
    assert str(tmp_path) not in json.dumps(release)
    assert json.loads((run_dir / "server_lock_release.json").read_text()) == {
        **release,
        "attempt_id": summary["attempt_id"],
    }
    terminal_event = _read_public_events(run_dir)[-1]
    assert terminal_event["event_type"] == expected_terminal
    assert terminal_event["payload"]["error"] == expected_error
    provenance = json.loads((run_dir / "provenance.json").read_text())
    assert provenance["lifecycle"]["status"] == (
        "timeout" if expected_timed_out else "failure"
    )


def test_stale_lock_owner_remains_diagnostic_for_generic_experiment(
    tmp_path,
    monkeypatch,
):
    config_path = _write_minecraft_config(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.update({"task_type": "construction", "required_artifacts": []})
    config_path.write_text(json.dumps(config), encoding="utf-8")
    stale_lock = MinecraftTargetLock(
        lock_root=tmp_path / "target-locks",
        host="127.0.0.1",
        port=25565,
        world_id="",
        attempt_id="previous-attempt",
    )
    stale_lock.lock_root.mkdir(parents=True)
    stale_lock.path.write_text(json.dumps({
        "schema_version": 2,
        "status": "acquired",
        "attempt_id": "previous-attempt",
        "pid": 99999999,
        "host": "127.0.0.1",
        "port": 25565,
        "world_id": "",
        "lock_key": stale_lock.key,
        "acquired_at": 1.0,
        "stale_owner_detected": False,
    }), encoding="utf-8")
    monkeypatch.setattr(
        "benchmarks.minecraft.run_lock._pid_exists",
        lambda _pid: False,
    )
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime_bounded",
        lambda *_args, **_kwargs: {
            "action_log": {},
            "runtime_process": _safe_runtime_process_metadata(),
            "bridge_cleanup": {"cleanup_complete": True, "processes": {}},
        },
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="stale_owner_diagnostic",
        execute=True,
        execute_timeout_seconds=30,
    )

    assert summary["runtime_target_lock_admission"] == "granted"
    assert summary["server_lock_stale_owner_detected"] is True
    assert summary["server_lock_released"] is True
    assert summary["server_lock_release_outcome"]["verified_released"] is True
    assert summary["runtime_target_safe_to_reuse"] is True
    assert summary["error"] is None


def test_generic_experiment_cycles_preserve_abandoned_predecessor_history(
    tmp_path,
    monkeypatch,
):
    config_path = _write_minecraft_config(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.update({"task_type": "construction", "required_artifacts": []})
    config_path.write_text(json.dumps(config), encoding="utf-8")

    lock_root = tmp_path / "target-locks"
    abandoned_attempt_id = "abandoned-predecessor-A"
    abandoned_lock = MinecraftTargetLock(
        lock_root=lock_root,
        host=config["host"],
        port=config["port"],
        world_id="",
        attempt_id=abandoned_attempt_id,
    ).acquire()
    abandoned_lock._stream.close()
    abandoned_lock._history_io.close()

    runtime_calls = []

    def mocked_runtime(launch_config, **kwargs):
        runtime_calls.append((launch_config, kwargs))
        runtime_result = _runtime_result_snapshot(status="success")
        runtime_result["score"] = {}
        runtime_result["runtime_process"] = _safe_runtime_process_metadata(
            exit_code=1 if len(runtime_calls) == 2 else 0
        )
        if len(runtime_calls) == 2:
            runtime_result_path = Path(kwargs["runtime_result_path"])
            runtime_result_path.parent.mkdir(parents=True, exist_ok=True)
            runtime_result_path.write_text(json.dumps(runtime_result), encoding="utf-8")
            raise MinecraftRuntimeChildError(
                "mock runtime context failed after cleanup",
                error_type="RuntimeError",
                process_metadata=_safe_runtime_process_metadata(exit_code=1),
                child_protocol={"status": "failed"},
            )
        return runtime_result

    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime_bounded",
        mocked_runtime,
    )

    completed = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="generic_after_abandoned_predecessor",
        execute=True,
        execute_timeout_seconds=30,
    )
    first_history = read_minecraft_target_predecessor_status(
        lock_root=lock_root,
        host=config["host"],
        port=config["port"],
    )

    failed = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="generic_failure_after_abandoned_predecessor",
        execute=True,
        execute_timeout_seconds=30,
    )
    final_history = read_minecraft_target_predecessor_status(
        lock_root=lock_root,
        host=config["host"],
        port=config["port"],
    )

    assert len(runtime_calls) == 2
    assert all(call[0]["host"] == config["host"] for call in runtime_calls)
    assert all(call[0]["port"] == config["port"] for call in runtime_calls)
    assert completed["runtime_target_lock_admission"] == "granted"
    assert completed["error"] is None
    assert completed["server_lock_released"] is True
    assert completed["server_lock_release_outcome"]["status"] == "verified_released"
    assert completed["runtime_target_safe_to_reuse"] is True
    assert failed["runtime_target_lock_admission"] == "granted"
    assert failed["error_type"] == "RuntimeError"
    assert failed["server_lock_released"] is True
    assert failed["server_lock_release_outcome"]["status"] == "verified_released"
    assert failed["runtime_target_safe_to_reuse"] is True

    assert first_history.status is MinecraftTargetPredecessorHistoryStatus.UNRESOLVED
    assert first_history.active_owner is False
    assert first_history.first["attempt_id"] == abandoned_attempt_id
    assert final_history.status is MinecraftTargetPredecessorHistoryStatus.UNRESOLVED
    assert final_history.active_owner is False
    assert final_history.first == first_history.first
    assert final_history.first["attempt_id"] == abandoned_attempt_id


def _install_target_lock_release_outcome(monkeypatch, *, status, error):
    real_release = MinecraftTargetLock.release

    def release_with_outcome(lock):
        real_release(lock)
        outcome = MinecraftTargetLockReleaseOutcome(
            status=status,
            error_type="OSError",
            error=error,
            uncertainty_persisted=status is MinecraftTargetLockReleaseStatus.UNCERTAIN,
        )
        lock.last_release_outcome = outcome
        return outcome

    monkeypatch.setattr(MinecraftTargetLock, "release", release_with_outcome)


def test_non_event_admission_failure_writes_canonical_failed_events(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)
    runtime_result = _runtime_result_snapshot(status="success")
    runtime_result["score"] = {}
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime",
        lambda *args, **kwargs: runtime_result,
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="missing_score",
        execute=True,
        execute_timeout_seconds=30,
    )

    run_dir = Path(summary["output_dir"])
    events = _read_public_events(run_dir)
    assert summary["artifact_admission"]["passed"] is False
    assert summary["terminal_event_type"] == "run_failed"
    assert summary["event_lifecycle_valid"] is True
    assert events[-1]["event_type"] == "run_failed"
    assert not (run_dir / COMPLETION_MARKER_FILE).exists()


def test_minecraft_execute_timeout_preserves_partial_runtime_dag(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)

    def persist_partial_then_hang(*_args, **kwargs):
        runtime_result_path = Path(kwargs["runtime_result_path"])
        runtime_result_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_result_path.write_text(
            json.dumps(_runtime_result_snapshot(status="running")),
            encoding="utf-8",
        )
        time.sleep(1)

    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime",
        persist_partial_then_hang,
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="partial_timeout",
        execute=True,
        execute_timeout_seconds=0.25,
    )

    snapshot = json.loads(
        (tmp_path / "result" / "partial_timeout" / "runtime_dual_dag_snapshot.json").read_text(
            encoding="utf-8"
        )
    )
    assert summary["timed_out"] is True
    assert summary["score_available"] is False
    assert summary["server_lock_released"] is True
    assert summary["runtime_process_group_alive_after_kill"] is False
    assert snapshot["nodes"][0]["lifecycle"]["status"] == "running"


def test_minecraft_meta_execute_persists_run_local_load_diagnostics(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)

    def runtime_with_diagnostics(launch_config, **kwargs):
        diagnostics_path = Path(kwargs["runtime_result_path"]).parent / "meta_judger_diagnostics.json"
        diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_root = Path(kwargs["runtime_root"])
        assert runtime_root.is_absolute()
        diagnostics_path.write_text(json.dumps({
            "command": ["python", "env/meta_judger.py", "--runtime-root", str(runtime_root)],
            "stdout_path": str(runtime_root / "meta_judger.stdout.log"),
            "stderr_path": str(tmp_path / "external-runtime" / "meta_judger.stderr.log"),
            "windows_path": r"C:\Users\researcher\meta_judger.log",
            "unc_path": r"\\server\share\meta_judger.log",
            "api_key": "retained-secret-value-12345",
            "load_status_history": [{"status": "loading"}, {"status": "loaded"}],
            "exit_code": None,
            "timeout_reason": None,
        }), encoding="utf-8")
        history_path = runtime_root / "result" / launch_config["task_name"] / "Alice_history.json"
        history_path.parent.mkdir(parents=True)
        history_path.write_text(json.dumps([{
            "action_list": [{
                "action": {"tool": "navigateTo", "tool_input": {"x": 5, "y": -60, "z": 5}},
                "feedback": {"status": True},
            }],
            "final_answer": "arrived",
        }]), encoding="utf-8")
        result = _runtime_result_snapshot(status="success")
        result.update({
            "score": {
                "attempt_id": kwargs["attempt_id"],
                "task_name": launch_config["task_name"],
                "status": "success",
                "score": 1,
                "iteration": {
                    "available": True,
                    "owner": "external_meta_judger",
                    "source": "Alice_history.json outer episode count",
                    "source_available": True,
                    "limit": 1,
                    "limit_available": True,
                    "used": None,
                    "usage_available": False,
                    "usage_unavailable_reason": {
                        "code": "history_not_observable_at_terminal_evaluation",
                        "message": "Alice_history.json was not observable at terminal evaluation",
                    },
                    "terminal_observations": 0,
                    "terminal_observations_available": True,
                },
            },
            "bridge_cleanup": {"cleanup_complete": True, "processes": {}},
            "controller": {"shutdown_complete": True, "active_assignments": {}},
            "collection_errors": [],
            "agent_iteration_limit": 12,
            "agent_iteration_limit_source": "VillagerBench.step max_turn",
        })
        return result

    monkeypatch.setattr("benchmarks.minecraft.experiment._execute_real_runtime", runtime_with_diagnostics)

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="meta_diagnostics",
        execute=True,
        execute_timeout_seconds=30,
    )

    output_dir = tmp_path / "result" / "meta_diagnostics"
    assert summary["load_status"] == "loaded"
    assert summary["meta_judger_diagnostics_available"] is True
    assert summary["score_available"] is True
    assert summary["artifact_admission"]["passed"] is True
    assert summary["judged_iteration_trace_available"] is True
    assert summary["judged_terminal_diagnostics_available"] is True
    terminal_diagnostics = json.loads(
        (output_dir / "judged_terminal_diagnostics.json").read_text(encoding="utf-8")
    )
    assert terminal_diagnostics["score_status"] == "success"
    assert terminal_diagnostics["schema_version"] == 2
    assert terminal_diagnostics["root_cause_category"] is None
    iteration_trace = json.loads(
        (output_dir / "judged_iteration_trace.json").read_text(encoding="utf-8")
    )
    assert iteration_trace["schema_version"] == 1
    assert iteration_trace["outer_episode_count"] == 1
    assert iteration_trace["agent_iteration"]["limit"] == 12
    assert terminal_diagnostics["agent_iteration"] == iteration_trace["agent_iteration"]
    assert terminal_diagnostics["agent_iteration"]["used"] == 1
    assert terminal_diagnostics["judger_iteration"]["used"] is None
    assert terminal_diagnostics["judger_iteration"]["usage_available"] is False
    assert terminal_diagnostics["judger_iteration"]["usage_unavailable_reason"]["code"] == (
        "history_not_observable_at_terminal_evaluation"
    )
    diagnostics = json.loads((output_dir / "meta_judger_diagnostics.json").read_text(encoding="utf-8"))
    retained_diagnostics_path = (
        output_dir / summary["runtime_root"] / "meta_judger_diagnostics.json"
    )
    retained_diagnostics = json.loads(retained_diagnostics_path.read_text(encoding="utf-8"))
    assert diagnostics["load_status_history"][-1]["status"] == "loaded"
    assert str(tmp_path) not in json.dumps(diagnostics)
    assert diagnostics == retained_diagnostics
    assert diagnostics["stdout_path"].startswith(".runtime/attempts/")
    assert diagnostics["stderr_path"] == "<external>"
    assert diagnostics["windows_path"] == "<external>"
    assert diagnostics["unc_path"] == "<external>"
    assert diagnostics["api_key"] == "[REDACTED]"
    runtime_root_index = diagnostics["command"].index("--runtime-root") + 1
    assert diagnostics["command"][runtime_root_index].startswith(".runtime/attempts/")
    validate_run_attempt(
        output_dir,
        attempt_id=summary["attempt_id"],
        require_completed=True,
    )
    assert (output_dir / COMPLETION_MARKER_FILE).is_file()
    _assert_bundle_has_no_absolute_paths(output_dir)
    assert "retained-secret-value-12345" not in "".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in output_dir.rglob("*")
        if path.is_file()
    )


def test_minecraft_failed_judger_preserves_measured_iteration_usage(
    tmp_path,
    monkeypatch,
):
    config_path = _write_minecraft_config(tmp_path)

    def failed_judger_result(launch_config, **kwargs):
        result = _runtime_result_snapshot(status="failure")
        result.update({
            "score": {
                "attempt_id": kwargs["attempt_id"],
                "task_name": launch_config["task_name"],
                "status": "failure",
                "progress": 0,
                "end_reason": "max iteration out",
                "iteration": {
                    "owner": "external_meta_judger",
                    "source": "Alice_history.json outer episode count",
                    "limit": 1,
                    "used": 1,
                    "terminal_observations": 3,
                },
            },
            "bridge_cleanup": {"cleanup_complete": True, "processes": {}},
            "controller": {"shutdown_complete": True, "active_assignments": {}},
            "collection_errors": [],
        })
        return result

    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime",
        failed_judger_result,
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="failed_judger_iteration",
        execute=True,
        execute_timeout_seconds=30,
    )

    output_dir = tmp_path / "result" / "failed_judger_iteration"
    terminal = json.loads(
        (output_dir / "judged_terminal_diagnostics.json").read_text(encoding="utf-8")
    )
    assert summary["artifact_admission"]["passed"] is False
    assert terminal["judger_iteration"]["usage_available"] is True
    assert terminal["judger_iteration"]["used"] == 1
    assert terminal["judger_iteration"]["usage_unavailable_reason"] is None
    manifest = validate_run_attempt(
        output_dir,
        attempt_id=summary["attempt_id"],
        require_completed=False,
    )
    assert manifest["status"] == "failed"
    assert not (output_dir / COMPLETION_MARKER_FILE).exists()
    _assert_bundle_has_no_absolute_paths(output_dir)


def test_minecraft_failure_sanitizes_retained_diagnostics_before_manifest(
    tmp_path,
    monkeypatch,
):
    config_path = _write_minecraft_config(tmp_path)

    def runtime_with_diagnostics(_launch_config, **kwargs):
        runtime_root = Path(kwargs["runtime_root"])
        diagnostics_path = runtime_root / "meta_judger_diagnostics.json"
        diagnostics_path.write_text(json.dumps({
            "command": ["python", "env/meta_judger.py", "--runtime-root", str(runtime_root)],
            "stdout_path": str(runtime_root / "meta_judger.stdout.log"),
            "stderr_path": str(tmp_path / "external-runtime" / "meta_judger.stderr.log"),
            "windows_path": r"C:\Users\researcher\meta_judger.log",
            "unc_path": r"\\server\share\meta_judger.log",
        }), encoding="utf-8")
        return _runtime_result_snapshot(status="success")

    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime",
        runtime_with_diagnostics,
    )
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._runtime_task_dag_snapshot",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("snapshot failed")),
    )

    with pytest.raises(RuntimeError, match="snapshot failed"):
        run_minecraft_experiment(
            config_path=config_path,
            output_root=tmp_path / "result",
            run_name="failed_diagnostics",
            execute=True,
            execute_timeout_seconds=30,
        )

    output_dir = tmp_path / "result" / "failed_diagnostics"
    retained_paths = list(output_dir.glob(".runtime/attempts/*/meta_judger_diagnostics.json"))
    assert len(retained_paths) == 1
    retained_path = retained_paths[0]
    retained = json.loads(retained_path.read_text(encoding="utf-8"))
    assert retained["stdout_path"].startswith(".runtime/attempts/")
    assert retained["stderr_path"] == "<external>"
    assert retained["windows_path"] == "<external>"
    assert retained["unc_path"] == "<external>"

    attempt = json.loads((output_dir / "attempt.json").read_text(encoding="utf-8"))
    manifest = validate_run_attempt(
        output_dir,
        attempt_id=attempt["attempt_id"],
        require_completed=False,
    )
    relative_path = str(retained_path.relative_to(output_dir))
    retained_entry = next(
        artifact for artifact in manifest["artifacts"] if artifact["path"] == relative_path
    )
    assert retained_entry["sha256"] == hashlib.sha256(retained_path.read_bytes()).hexdigest()
    assert manifest["status"] == "failed"
    assert not (output_dir / COMPLETION_MARKER_FILE).exists()
    _assert_bundle_has_no_absolute_paths(output_dir)


@pytest.mark.parametrize(
    ("contents", "error_type"),
    [
        ("{not-json", "JSONDecodeError"),
        ("[]", "TypeError"),
    ],
)
def test_retained_diagnostics_invalid_payload_is_replaced(
    tmp_path,
    contents,
    error_type,
):
    output_dir = tmp_path / "bundle"
    diagnostics_path = output_dir / ".runtime" / "meta_judger_diagnostics.json"
    diagnostics_path.parent.mkdir(parents=True)
    diagnostics_path.write_text(contents, encoding="utf-8")

    diagnostics = _sanitize_retained_meta_judger_diagnostics(
        diagnostics_path,
        output_dir=output_dir,
        secret_values=(),
    )

    assert diagnostics == {
        "schema_version": 1,
        "status": "invalid",
        "error": "retained meta judger diagnostics could not be published",
        "error_type": error_type,
    }
    assert json.loads(diagnostics_path.read_text(encoding="utf-8")) == diagnostics


def test_minecraft_execute_timeout_stops_child_activity(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)
    late_marker = tmp_path / "late_child_activity.txt"

    def slow_then_write(*args, **kwargs):
        time.sleep(0.2)
        late_marker.write_text("still running", encoding="utf-8")
        return {}

    monkeypatch.setattr("benchmarks.minecraft.experiment._execute_real_runtime", slow_then_write)

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="child_stops",
        execute=True,
        execute_timeout_seconds=0.01,
    )
    time.sleep(0.3)

    assert summary["timed_out"] is True
    assert summary["runtime_process_terminated"] is True
    assert not late_marker.exists()


def test_runtime_process_termination_uses_kill_fallback():
    process = _StubbornProcess()

    metadata = _terminate_runtime_process(process, grace_seconds=0.01)

    assert process.calls == ["terminate", ("join", 0.01), "kill", ("join", 0.01)]
    assert metadata == {
        "exit_code": None,
        "terminated": True,
        "killed": True,
        "process_alive_after_kill": True,
        "process_group_alive_after_kill": False,
    }


@pytest.mark.parametrize(
    "unsafe_field",
    ["process_alive_after_kill", "process_group_alive_after_kill"],
)
def test_runtime_cleanup_validator_prioritizes_cleanup_failure(unsafe_field):
    metadata = {
        "process_alive_after_kill": False,
        "process_group_alive_after_kill": False,
    }
    metadata[unsafe_field] = True
    primary_error = {
        "error_type": "MinecraftExecuteTimeoutError",
        "message": "execute timed out",
    }

    with pytest.raises(MinecraftRuntimeChildError) as raised:
        _validate_runtime_cleanup(
            metadata,
            context="after execute timeout",
            timed_out=True,
            primary_error=primary_error,
        )

    assert raised.value.error_type == "ProcessGroupCleanupError"
    assert raised.value.timed_out is True
    assert raised.value.primary_error == primary_error


def test_cleanup_failure_summary_keeps_timeout_and_marks_target_unsafe(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)
    primary_error = {
        "error_type": "MinecraftExecuteTimeoutError",
        "message": "execute timed out",
    }

    def fail_cleanup(*_args, **_kwargs):
        raise MinecraftRuntimeChildError(
            "Minecraft runtime cleanup failed after execute timeout",
            error_type="ProcessGroupCleanupError",
            process_metadata={
                "exit_code": None,
                "terminated": True,
                "killed": True,
                "process_alive_after_kill": False,
                "process_group_alive_after_kill": True,
            },
            timed_out=True,
            primary_error=primary_error,
        )

    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime_bounded",
        fail_cleanup,
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="unsafe_cleanup",
        execute=True,
        execute_timeout_seconds=30,
    )

    assert summary["error_type"] == "ProcessGroupCleanupError"
    assert summary["timed_out"] is True
    assert summary["runtime_primary_error"] == primary_error
    assert summary["runtime_target_safe_to_reuse"] is False
    assert summary["runtime_target_quarantined"] is True
    assert summary["runtime_target_quarantine"]["status"] == "created"
    assert "runtime_process_group_alive_after_kill" in summary["runtime_target_quarantine"]["reasons"]
    assert summary["score_available"] is False
    assert summary["child_protocol"].get("status") != "completed"


@pytest.mark.parametrize(
    ("bridge_cleanup", "expected_reason"),
    [
        (
            {"cleanup_complete": True, "processes": []},
            "bridge_process_metadata_invalid",
        ),
        (
            {"cleanup_complete": True, "processes": {"Alice": "malformed"}},
            "bridge_process_metadata_invalid:Alice",
        ),
        (
            {"cleanup_complete": "true", "processes": {}},
            "bridge_cleanup_metadata_invalid",
        ),
        (
            {
                "cleanup_complete": True,
                "processes": {"Alice": {"alive_after_kill": 0}},
            },
            "bridge_process_metadata_invalid:Alice",
        ),
    ],
)
def test_malformed_raw_bridge_cleanup_quarantines_target(
    tmp_path,
    monkeypatch,
    bridge_cleanup,
    expected_reason,
):
    config_path = _write_minecraft_config(tmp_path)
    calls = []

    def malformed_runtime(*_args, **_kwargs):
        calls.append("runtime")
        return {
            "action_log": {},
            "runtime_process": _safe_runtime_process_metadata(),
            "bridge_cleanup": bridge_cleanup,
        }

    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime_bounded",
        malformed_runtime,
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="malformed_cleanup",
        execute=True,
        execute_timeout_seconds=30,
    )

    run_dir = Path(summary["output_dir"])
    assert summary["runtime_target_safe_to_reuse"] is False
    assert summary["runtime_target_quarantined"] is True
    assert expected_reason in summary["runtime_target_quarantine"]["reasons"]
    if bridge_cleanup.get("cleanup_complete") == "true":
        assert summary["bridge_cleanup"]["cleanup_complete"] is None
    if expected_reason == "bridge_process_metadata_invalid:Alice":
        assert (
            summary["bridge_cleanup"]["processes"]["Alice"]["alive_after_kill"]
            is None
        )
    assert summary["terminal_event_type"] == "run_failed"
    assert _read_public_events(run_dir)[-1]["event_type"] == "run_failed"
    assert json.loads((run_dir / "artifact_manifest.json").read_text(encoding="utf-8"))["status"] == "failed"
    assert not (run_dir / COMPLETION_MARKER_FILE).exists()
    if expected_reason == "bridge_process_metadata_invalid":
        blocked = run_minecraft_experiment(
            config_path=config_path,
            output_root=tmp_path / "result",
            run_name="blocked_after_malformed_cleanup",
            execute=True,
            execute_timeout_seconds=30,
        )
        assert calls == ["runtime"]
        assert blocked["error_type"] == "MinecraftTargetQuarantinedError"
        assert blocked["runtime_started"] is False


@pytest.mark.parametrize(
    "runtime_process",
    [
        [],
        {
            "process_alive_after_kill": "false",
            "process_group_alive_after_kill": False,
        },
    ],
)
def test_malformed_runtime_process_metadata_quarantines_without_breaking_finalization(
    tmp_path,
    monkeypatch,
    runtime_process,
):
    config_path = _write_minecraft_config(tmp_path)
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime_bounded",
        lambda *_args, **_kwargs: {
            "action_log": {},
            "runtime_process": runtime_process,
            "bridge_cleanup": {"cleanup_complete": True, "processes": {}},
        },
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="malformed_runtime_process",
        execute=True,
        execute_timeout_seconds=30,
    )

    run_dir = Path(summary["output_dir"])
    assert summary["runtime_target_safe_to_reuse"] is False
    assert summary["runtime_target_quarantined"] is True
    assert "runtime_process_metadata_invalid" in summary["runtime_target_quarantine"]["reasons"]
    assert summary["terminal_event_type"] == "run_failed"
    assert json.loads((run_dir / "artifact_manifest.json").read_text(encoding="utf-8"))["status"] == "failed"
    assert not (run_dir / COMPLETION_MARKER_FILE).exists()


def test_missing_runtime_process_cleanup_field_is_publicly_unknown(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime_bounded",
        lambda *_args, **_kwargs: {
            "action_log": {},
            "runtime_process": {"process_alive_after_kill": False},
            "bridge_cleanup": {"cleanup_complete": True, "processes": {}},
        },
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="missing_process_group_cleanup",
        execute=True,
        execute_timeout_seconds=30,
    )

    assert summary["runtime_process_alive_after_kill"] is False
    assert summary["runtime_process_group_alive_after_kill"] is None
    assert summary["runtime_target_safe_to_reuse"] is False
    assert summary["runtime_target_quarantined"] is True
    assert (
        "runtime_process_metadata_invalid"
        in summary["runtime_target_quarantine"]["reasons"]
    )


def test_quarantine_blocks_next_command_until_explicit_clear(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)
    calls = []

    def unsafe_then_safe(*_args, **_kwargs):
        calls.append("runtime")
        return {
            "action_log": {},
            "runtime_process": _safe_runtime_process_metadata(),
            "bridge_cleanup": {"cleanup_complete": len(calls) > 1, "processes": {}},
        }

    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime_bounded",
        unsafe_then_safe,
    )

    first = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="creates_quarantine",
        execute=True,
        execute_timeout_seconds=30,
    )
    blocked = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="blocked_by_quarantine",
        execute=True,
        execute_timeout_seconds=30,
    )

    assert calls == ["runtime"]
    assert first["runtime_target_quarantined"] is True
    assert first["runtime_target_quarantine"]["reasons"] == ["bridge_cleanup_incomplete"]
    assert blocked["error_type"] == "MinecraftTargetQuarantinedError"
    assert blocked["runtime_target_lock_admission"] == "quarantined"
    assert blocked["runtime_started"] is False
    assert blocked["server_lock_quarantine_detected"] is True
    assert blocked["runtime_target_quarantine"]["status"] == "preexisting"
    assert blocked["terminal_event_type"] == "run_failed"
    assert _read_public_events(Path(blocked["output_dir"]))[-1]["event_type"] == "run_failed"
    assert "pid" not in json.dumps(blocked["runtime_target_quarantine"]).lower()
    assert "path" not in json.dumps(blocked["runtime_target_quarantine"]).lower()
    blocked_dir = Path(blocked["output_dir"])
    assert json.loads((blocked_dir / "artifact_manifest.json").read_text(encoding="utf-8"))["status"] == "failed"
    assert not (blocked_dir / COMPLETION_MARKER_FILE).exists()

    clear_minecraft_target_quarantine(
        lock_root=tmp_path / "target-locks",
        host="127.0.0.1",
        port=25565,
        reason="operator verified all processes stopped",
        acknowledge_target_safe=True,
    )
    resumed = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="after_clear",
        execute=True,
        execute_timeout_seconds=30,
    )

    assert calls == ["runtime", "runtime"]
    assert resumed["runtime_started"] is True
    assert resumed["runtime_target_quarantined"] is False


def test_busy_target_fails_closed_and_runs_after_owner_release(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)
    owner = MinecraftTargetLock(
        lock_root=tmp_path / "target-locks",
        host="127.0.0.1",
        port=25565,
        world_id="",
        attempt_id="attempt-owner",
    ).acquire()
    calls = []

    def safe_runtime(*_args, **_kwargs):
        calls.append("runtime")
        return {
            "action_log": {},
            "runtime_process": _safe_runtime_process_metadata(),
            "bridge_cleanup": {"cleanup_complete": True, "processes": {}},
        }

    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime_bounded",
        safe_runtime,
    )
    try:
        summary = run_minecraft_experiment(
            config_path=config_path,
            output_root=tmp_path / "result",
            run_name="busy_target",
            execute=True,
            execute_timeout_seconds=30,
        )
    finally:
        owner.release()

    run_dir = Path(summary["output_dir"])
    owner_summary = summary["runtime_target_lock_owner"]
    assert calls == []
    assert summary["error_type"] == "MinecraftTargetLockBusyError"
    assert summary["runtime_started"] is False
    assert summary["runtime_target_lock_admission"] == "busy"
    assert summary["runtime_target_lock_unavailable"] is True
    assert summary["runtime_target_lock_unavailable_reason"] == "busy"
    assert summary["runtime_target_lock_metadata_valid"] is None
    assert summary["runtime_target_safe_to_reuse"] is False
    assert summary["runtime_target_quarantined"] is False
    assert owner_summary == {"status": "acquired", "attempt_id": "attempt-owner"}
    assert "pid" not in json.dumps(owner_summary).lower()
    assert "path" not in json.dumps(owner_summary).lower()
    assert summary["terminal_event_type"] == "run_failed"
    assert json.loads((run_dir / "provenance.json").read_text(encoding="utf-8"))["lifecycle"]["status"] == "failure"
    assert json.loads((run_dir / "artifact_manifest.json").read_text(encoding="utf-8"))["status"] == "failed"
    assert not (run_dir / COMPLETION_MARKER_FILE).exists()

    resumed = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="after_owner_release",
        execute=True,
        execute_timeout_seconds=30,
    )
    assert calls == ["runtime"]
    assert resumed["runtime_started"] is True
    assert resumed["runtime_target_lock_admission"] == "granted"
    assert resumed["runtime_target_quarantined"] is False


def test_lock_io_error_is_unavailable_without_starting_runtime(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)
    calls = []
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime_bounded",
        lambda *_args, **_kwargs: calls.append("runtime"),
    )
    monkeypatch.setattr(
        "benchmarks.minecraft.run_lock.fcntl.flock",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("flock failed")),
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="lock_unavailable",
        execute=True,
        execute_timeout_seconds=30,
    )

    assert calls == []
    assert summary["error_type"] == "MinecraftTargetLockUnavailableError"
    assert summary["runtime_started"] is False
    assert summary["runtime_target_lock_admission"] == "unavailable"
    assert summary["runtime_target_lock_unavailable"] is True
    assert summary["runtime_target_lock_unavailable_reason"] == "io_error"
    assert summary["runtime_target_lock_metadata_valid"] is None
    assert summary["runtime_target_safe_to_reuse"] is False
    assert summary["runtime_target_quarantined"] is False


def test_unknown_lock_admission_error_fails_closed(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)
    calls = []
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment.MinecraftTargetLock.acquire",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("unknown lock failure")),
    )
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime_bounded",
        lambda *_args, **_kwargs: calls.append("runtime"),
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="unknown_lock_failure",
        execute=True,
        execute_timeout_seconds=30,
    )

    assert calls == []
    assert summary["error_type"] == "RuntimeError"
    assert summary["runtime_started"] is False
    assert summary["runtime_target_lock_admission"] == "unavailable"
    assert summary["runtime_target_lock_unavailable"] is True
    assert summary["runtime_target_lock_unavailable_reason"] == "unknown_error"
    assert summary["runtime_target_lock_metadata_valid"] is None
    assert summary["runtime_target_safe_to_reuse"] is False
    assert summary["runtime_target_quarantined"] is False


def test_generic_runtime_error_quarantines_unverified_cleanup(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)
    calls = []

    def fail_runtime(*_args, **_kwargs):
        calls.append("runtime")
        raise RuntimeError("runtime failed")

    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime_bounded",
        fail_runtime,
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="generic_runtime_error",
        execute=True,
        execute_timeout_seconds=30,
    )
    blocked = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="blocked_after_runtime_error",
        execute=True,
        execute_timeout_seconds=30,
    )

    run_dir = Path(summary["output_dir"])
    reasons = summary["runtime_target_quarantine"]["reasons"]
    assert calls == ["runtime"]
    assert summary["error_type"] == "RuntimeError"
    assert summary["runtime_started"] is True
    assert summary["runtime_target_safe_to_reuse"] is False
    assert summary["runtime_target_quarantined"] is True
    assert summary["runtime_process_alive_after_kill"] is None
    assert summary["runtime_process_group_alive_after_kill"] is None
    assert summary["runtime_process_terminated"] is None
    assert summary["runtime_process_killed"] is None
    assert "runtime_process_metadata_invalid" in reasons
    assert "bridge_cleanup_missing" in reasons
    assert summary["terminal_event_type"] == "run_failed"
    assert json.loads((run_dir / "artifact_manifest.json").read_text(encoding="utf-8"))["status"] == "failed"
    assert not (run_dir / COMPLETION_MARKER_FILE).exists()
    assert blocked["error_type"] == "MinecraftTargetQuarantinedError"
    assert blocked["runtime_started"] is False


def test_invalid_lock_metadata_is_not_reported_as_safe(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)
    lock = MinecraftTargetLock(
        lock_root=tmp_path / "target-locks",
        host="127.0.0.1",
        port=25565,
        world_id="",
        attempt_id="corrupt-owner",
    )
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_text("{", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime_bounded",
        lambda *_args, **_kwargs: calls.append("runtime"),
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="invalid_lock_metadata",
        execute=True,
        execute_timeout_seconds=30,
    )

    run_dir = Path(summary["output_dir"])
    provenance = json.loads((run_dir / "provenance.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "artifact_manifest.json").read_text(encoding="utf-8"))
    assert calls == []
    assert summary["error_type"] == "MinecraftTargetLockMetadataError"
    assert summary["runtime_target_lock_admission"] == "metadata_invalid"
    assert summary["runtime_started"] is False
    assert summary["runtime_target_lock_metadata_valid"] is False
    assert summary["runtime_target_safe_to_reuse"] is False
    assert summary["runtime_target_quarantined"] is False
    assert summary["terminal_event_type"] == "run_failed"
    assert provenance["lifecycle"]["status"] == "failure"
    assert manifest["status"] == "failed"
    assert not (run_dir / COMPLETION_MARKER_FILE).exists()


def test_invalid_utf8_lock_metadata_is_not_reported_as_safe(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)
    lock = MinecraftTargetLock(
        lock_root=tmp_path / "target-locks",
        host="127.0.0.1",
        port=25565,
        world_id="",
        attempt_id="corrupt-owner",
    )
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_bytes(b"\xff\xfeinvalid-lock-metadata")
    calls = []
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime_bounded",
        lambda *_args, **_kwargs: calls.append("runtime"),
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="invalid_utf8_lock_metadata",
        execute=True,
        execute_timeout_seconds=30,
    )

    run_dir = Path(summary["output_dir"])
    assert calls == []
    assert summary["error_type"] == "MinecraftTargetLockMetadataError"
    assert summary["runtime_started"] is False
    assert summary["runtime_target_lock_admission"] == "metadata_invalid"
    assert summary["runtime_target_lock_metadata_valid"] is False
    assert summary["runtime_target_safe_to_reuse"] is False
    assert summary["runtime_target_quarantined"] is False
    assert summary["terminal_event_type"] == "run_failed"
    assert json.loads((run_dir / "artifact_manifest.json").read_text(encoding="utf-8"))["status"] == "failed"
    assert not (run_dir / COMPLETION_MARKER_FILE).exists()


def test_public_bridge_cleanup_omits_process_ids():
    result = _public_bridge_cleanup({
        "cleanup_complete": True,
        "processes": {
            "Alice": {
                "pid": 1234,
                "terminated": True,
                "killed": False,
                "alive_after_kill": False,
            }
        },
    })

    assert result == {
        "cleanup_complete": True,
        "processes": {
            "Alice": {
                "terminated": True,
                "killed": False,
                "alive_after_kill": False,
            }
        },
    }


@pytest.mark.parametrize(
    ("raw_value", "expected_public"),
    [
        ("true", None),
        (1, None),
        (0, None),
        (True, True),
        (False, False),
    ],
)
def test_public_bridge_cleanup_preserves_only_real_booleans(raw_value, expected_public):
    result = _public_bridge_cleanup({
        "cleanup_complete": raw_value,
        "processes": {},
    })

    assert result["cleanup_complete"] is expected_public


def test_public_bridge_cleanup_preserves_invalid_process_booleans_as_unknown():
    result = _public_bridge_cleanup({
        "cleanup_complete": True,
        "processes": {
            "Alice": {
                "alive_after_kill": 0,
                "terminated": "true",
                "killed": False,
            },
        },
    })

    assert result == {
        "cleanup_complete": True,
        "processes": {
            "Alice": {
                "terminated": None,
                "killed": False,
                "alive_after_kill": None,
            },
        },
    }


@pytest.mark.parametrize(
    ("raw_value", "expected_public"),
    [
        ("false", None),
        (0, None),
        (1, None),
        (None, None),
        (False, False),
        (True, True),
    ],
)
def test_public_runtime_process_preserves_only_real_booleans(raw_value, expected_public):
    result = _public_runtime_process({
        "process_alive_after_kill": raw_value,
        "process_group_alive_after_kill": raw_value,
        "terminated": raw_value,
        "killed": raw_value,
    })

    assert result["process_alive_after_kill"] is expected_public
    assert result["process_group_alive_after_kill"] is expected_public
    assert result["terminated"] is expected_public
    assert result["killed"] is expected_public


@pytest.mark.parametrize("value", [None, [], "invalid"])
def test_public_runtime_process_reports_non_objects_as_unknown(value):
    result = _public_runtime_process(value)

    assert result["terminated"] is None
    assert result["killed"] is None
    assert result["process_alive_after_kill"] is None
    assert result["process_group_alive_after_kill"] is None


def test_runtime_process_termination_targets_isolated_process_group(monkeypatch):
    class GroupProcess:
        pid = 321
        exitcode = None
        alive = True

        def terminate(self):
            raise AssertionError("isolated process must use killpg")

        def kill(self):
            raise AssertionError("isolated process must use killpg")

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return self.alive

    process = GroupProcess()
    signals = []
    monkeypatch.setattr("benchmarks.minecraft.experiment.os.getpgid", lambda pid: pid)

    def kill_group(process_group_id, sent_signal):
        if sent_signal == 0:
            raise ProcessLookupError()
        signals.append((process_group_id, sent_signal))
        process.alive = False
        process.exitcode = -sent_signal

    monkeypatch.setattr("benchmarks.minecraft.experiment.os.killpg", kill_group)

    metadata = _terminate_runtime_process(process, grace_seconds=0.01)

    assert signals == [(321, signal.SIGTERM)]
    assert metadata["terminated"] is True
    assert metadata["killed"] is False


def test_runtime_process_group_kills_descendants_after_leader_exits(monkeypatch):
    class GroupProcess:
        pid = 654
        exitcode = None
        alive = True

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return self.alive

    process = GroupProcess()
    group_alive = True
    signals = []
    monkeypatch.setattr("benchmarks.minecraft.experiment.os.getpgid", lambda pid: pid)

    def kill_group(process_group_id, sent_signal):
        nonlocal group_alive
        if sent_signal == 0:
            if not group_alive:
                raise ProcessLookupError()
            return
        signals.append((process_group_id, sent_signal))
        if sent_signal == signal.SIGTERM:
            process.alive = False
            process.exitcode = -signal.SIGTERM
        elif sent_signal == signal.SIGKILL:
            group_alive = False

    monkeypatch.setattr("benchmarks.minecraft.experiment.os.killpg", kill_group)

    metadata = _terminate_runtime_process(process, grace_seconds=0.01)

    assert signals == [(654, signal.SIGTERM), (654, signal.SIGKILL)]
    assert metadata["terminated"] is True
    assert metadata["killed"] is True


def test_runtime_process_group_cleanup_tolerates_group_exit_race(monkeypatch):
    class ExitedProcess:
        pid = 777
        exitcode = 0

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return False

    monkeypatch.setattr("benchmarks.minecraft.experiment.os.getpgid", lambda pid: pid)
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment.os.killpg",
        lambda *args: (_ for _ in ()).throw(ProcessLookupError()),
    )

    metadata = _terminate_runtime_process(ExitedProcess(), grace_seconds=0.01)

    assert metadata["terminated"] is True
    assert metadata["killed"] is False


def test_runtime_process_group_cleanup_waits_for_delayed_exit(monkeypatch):
    class ExitedLeader:
        pid = 779
        exitcode = 0

        @staticmethod
        def join(timeout=None):
            return None

        @staticmethod
        def is_alive():
            return False

    checks = 0

    def kill_group(_process_group_id, sent_signal):
        nonlocal checks
        if sent_signal == 0:
            checks += 1
            if checks >= 3:
                raise ProcessLookupError()

    monkeypatch.setattr("benchmarks.minecraft.experiment.os.killpg", kill_group)

    metadata = _terminate_runtime_process(
        ExitedLeader(),
        grace_seconds=0.1,
        process_group_id=ExitedLeader.pid,
    )

    assert metadata["process_group_alive_after_kill"] is False
    assert checks >= 3


def test_cleaned_descendants_do_not_turn_successful_child_into_failure(monkeypatch):
    process = SimpleNamespace(exitcode=0)
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._process_group_exists",
        lambda _group_id: True,
    )
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._terminate_runtime_process",
        lambda *_args, **_kwargs: {
            "exit_code": 0,
            "terminated": True,
            "killed": True,
            "process_alive_after_kill": False,
            "process_group_alive_after_kill": False,
        },
    )

    metadata = _cleanup_exited_runtime_process_group(
        process,
        process_group_id=123,
    )

    assert metadata["terminated"] is True
    assert metadata["process_group_alive_after_kill"] is False


def test_runtime_process_group_cleanup_falls_back_to_direct_termination(monkeypatch):
    process = _StubbornProcess()
    process.pid = 778
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment.os.killpg",
        lambda *args: (_ for _ in ()).throw(ProcessLookupError()),
    )

    metadata = _terminate_runtime_process(
        process,
        grace_seconds=0.01,
        process_group_id=process.pid,
    )

    assert process.calls == ["terminate", ("join", 0.01), "kill", ("join", 0.01)]
    assert metadata["killed"] is True


def test_minecraft_execute_timeout_uses_partial_runtime_snapshot(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)

    def slow_runtime_with_partial(*args, **kwargs):
        runtime_result_path = Path(kwargs["runtime_result_path"])
        runtime_result_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_result_path.write_text(json.dumps(_runtime_result_snapshot(status="running")), encoding="utf-8")
        time.sleep(1)

    monkeypatch.setattr("benchmarks.minecraft.experiment._execute_real_runtime", slow_runtime_with_partial)

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="execute_timeout_partial",
        execute=True,
        execute_timeout_seconds=0.25,
    )

    output_dir = tmp_path / "result" / "execute_timeout_partial"
    runtime_snapshot = json.loads((output_dir / "runtime_dual_dag_snapshot.json").read_text(encoding="utf-8"))
    assert summary["timed_out"] is True
    assert summary["snapshot_source"] == "real_runtime"
    assert runtime_snapshot["nodes"][0]["lifecycle"]["status"] == "running"


def test_minecraft_execute_timeout_survives_contextmanager_generator_error(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)

    def contextmanager_swallowed_timeout(*args, **kwargs):
        try:
            time.sleep(1)
        except MinecraftExecuteTimeoutError as exc:
            raise RuntimeError("generator didn't yield") from exc

    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime",
        contextmanager_swallowed_timeout,
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="execute_contextmanager_timeout",
        execute=True,
        execute_timeout_seconds=0.01,
    )

    assert summary["error_type"] == "timeout"
    assert summary["timed_out"] is True
    assert "timed out after 0.01 seconds" in summary["error"]


def test_minecraft_execute_uses_unique_per_run_result_paths_and_cleans_up(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)

    def runtime_result(*args, **kwargs):
        runtime_result_path = Path(kwargs["runtime_result_path"])
        (Path(kwargs["runtime_root"]) / "child_runtime_path.txt").write_text(
            str(runtime_result_path),
            encoding="utf-8",
        )
        return _runtime_result_snapshot(status="success")

    monkeypatch.setattr("benchmarks.minecraft.experiment._execute_real_runtime", runtime_result)

    first = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="run_a",
        execute=True,
            execute_timeout_seconds=30,
    )
    second = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="run_b",
        execute=True,
            execute_timeout_seconds=30,
    )

    expected_paths = [
        Path(first["output_dir"]) / first["runtime_result_path"],
        Path(second["output_dir"]) / second["runtime_result_path"],
    ]
    assert [
        (Path(first["output_dir"]) / first["runtime_root"] / "child_runtime_path.txt").read_text(encoding="utf-8"),
        (Path(second["output_dir"]) / second["runtime_root"] / "child_runtime_path.txt").read_text(encoding="utf-8"),
    ] == [str(path) for path in expected_paths]
    assert first["runtime_result_retained"] is False
    assert second["runtime_result_retained"] is False
    assert first["runtime_root"] != second["runtime_root"]
    assert first["score_path"] != second["score_path"]
    assert first["load_status_path"] != second["load_status_path"]
    assert first["server_lock_acquired"] is True
    assert first["server_lock_released"] is True
    assert all(not path.exists() for path in expected_paths)
    assert (tmp_path / "result" / "run_a" / "runtime_dual_dag_snapshot.json").exists()
    assert (tmp_path / "result" / "run_b" / "runtime_dual_dag_snapshot.json").exists()


def test_minecraft_execute_ignores_stale_global_runtime_result(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_path = _write_minecraft_config(tmp_path)
    stale_path = tmp_path / ".cache" / "minecraft_runtime_result.json"
    stale_path.parent.mkdir(parents=True)
    stale_path.write_text(json.dumps(_runtime_result_snapshot(status="running")), encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "action_log.json").write_text(
        json.dumps({"Alice": [{"action": "staleAction", "result": {"status": True}}]}),
        encoding="utf-8",
    )
    (data_dir / "score.json").write_text(json.dumps({"progress": 1.0}), encoding="utf-8")
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime",
        lambda *args, **kwargs: {},
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="ignore_stale",
        execute=True,
            execute_timeout_seconds=30,
    )

    assert summary["snapshot_source"] == "config_fixture"
    assert summary["progress"] is None
    assert summary["action_log_available"] is False
    action_log = json.loads(
        (tmp_path / "result" / "ignore_stale" / "action_log.json").read_text(encoding="utf-8")
    )
    assert "Alice" not in action_log
    assert stale_path.exists()


def test_minecraft_execute_rejects_score_owned_by_another_attempt(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)

    def mismatched_score(*_args, **kwargs):
        return {
            "score": {
                "attempt_id": "another-attempt",
                "task_name": "another-task",
                "score": 100,
            },
            "action_log": {},
            "bridge_cleanup": {"cleanup_complete": True, "processes": {}},
        }

    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime",
        mismatched_score,
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="mismatched_score",
        execute=True,
            execute_timeout_seconds=30,
    )

    assert summary["score_available"] is False
    assert summary["score_ownership_verified"] is False
    assert summary["error_type"] == "ScoreOwnershipError"
    assert "another-attempt" in summary["error"]


@pytest.mark.parametrize("missing_field", ["attempt_id", "task_name"])
def test_minecraft_execute_rejects_score_with_missing_ownership(
    tmp_path, monkeypatch, missing_field
):
    config_path = _write_minecraft_config(tmp_path)

    def missing_identity(launch_config, **kwargs):
        score = {
            "attempt_id": kwargs["attempt_id"],
            "task_name": launch_config["task_name"],
            "status": "success",
            "score": 100,
        }
        score.pop(missing_field)
        return {
            "score": score,
            "action_log": {},
            "bridge_cleanup": {"cleanup_complete": True, "processes": {}},
        }

    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime",
        missing_identity,
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name=f"missing_{missing_field}",
        execute=True,
            execute_timeout_seconds=30,
    )

    assert summary["score_available"] is False
    assert summary["score_ownership_verified"] is False
    assert summary["error_type"] == "ScoreOwnershipError"
    assert f"missing: {missing_field}" in summary["error"]


def test_minecraft_execute_ignores_partial_tmp_result(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)

    def write_partial_tmp(*args, **kwargs):
        runtime_result_path = Path(kwargs["runtime_result_path"])
        runtime_result_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_result_path.with_suffix(".json.tmp").write_text(
            json.dumps(_runtime_result_snapshot(status="running")),
            encoding="utf-8",
        )
        raise RuntimeError("interrupted write")

    monkeypatch.setattr("benchmarks.minecraft.experiment._execute_real_runtime", write_partial_tmp)

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="partial_tmp",
        execute=True,
            execute_timeout_seconds=30,
    )

    assert summary["snapshot_source"] == "config_fixture"
    assert summary["error_type"] == "RuntimeError"
    runtime_root = Path(summary["output_dir"]) / summary["runtime_root"]
    assert runtime_root.exists()
    assert not (runtime_root / "runtime_result.json").exists()


def test_minecraft_execute_can_retain_internal_result_explicitly(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)

    def persist_result(*args, **kwargs):
        from start_with_config import _write_runtime_result

        _write_runtime_result(
            str(kwargs["runtime_result_path"]),
            _runtime_result_snapshot(status="success"),
        )
        return {}

    monkeypatch.setattr("benchmarks.minecraft.experiment._execute_real_runtime", persist_result)

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="retained",
        execute=True,
            execute_timeout_seconds=30,
        retain_runtime_result=True,
    )

    runtime_result_path = Path(summary["output_dir"]) / summary["runtime_result_path"]
    assert summary["runtime_result_retained"] is True
    assert runtime_result_path.exists()
    assert not runtime_result_path.with_suffix(".json.tmp").exists()
    assert json.loads(runtime_result_path.read_text(encoding="utf-8"))["runtime_task_dag_snapshot"]


def test_minecraft_run_rejects_reuse_and_allows_explicit_overwrite(tmp_path):
    config_path = _write_minecraft_config(tmp_path)
    first = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="rerun",
    )

    with pytest.raises(RunDirectoryExistsError, match="not empty"):
        run_minecraft_experiment(
            config_path=config_path,
            output_root=tmp_path / "result",
            run_name="rerun",
        )

    second = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="rerun",
        overwrite=True,
    )
    run_dir = tmp_path / "result" / "rerun"

    assert second["attempt_id"] != first["attempt_id"]
    assert (run_dir / COMPLETION_MARKER_FILE).read_text(encoding="utf-8").strip() == second["attempt_id"]
    assert json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))["attempt_id"] == second["attempt_id"]
    assert json.loads((run_dir / "artifact_manifest.json").read_text(encoding="utf-8"))["status"] == "completed"


def test_minecraft_failed_run_has_no_completion_marker(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("runtime failed")),
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="failed_bundle",
        execute=True,
            execute_timeout_seconds=30,
    )
    run_dir = tmp_path / "result" / "failed_bundle"
    manifest = json.loads((run_dir / "artifact_manifest.json").read_text(encoding="utf-8"))

    assert summary["error_type"] == "RuntimeError"
    assert manifest["attempt_id"] == summary["attempt_id"]
    assert manifest["status"] == "failed"
    assert not (run_dir / COMPLETION_MARKER_FILE).exists()


def test_minecraft_runtime_error_redacts_secret_literals(tmp_path, monkeypatch):
    secret = "tiny"
    runtime_secret = "runtime-only-secret"
    config_path = _write_minecraft_config(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["api_key"] = secret
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(
        "model.ollama_config.make_ollama_llm_config",
        lambda: {
            "api_key": runtime_secret,
            "api_key_list": [runtime_secret],
            "api_model": "model",
            "api_base": "http://example.test/v1",
        },
    )
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment._execute_real_runtime",
        lambda *args, **kwargs: (
            _ for _ in ()
        ).throw(RuntimeError(f"rejected {secret} and {runtime_secret}")),
    )

    summary = run_minecraft_experiment(
        config_path=config_path,
        output_root=tmp_path / "result",
        run_name="secret_error",
        execute=True,
            execute_timeout_seconds=30,
        retain_runtime_result=True,
    )
    run_dir = tmp_path / "result" / "secret_error"

    assert summary["error"] == "rejected [REDACTED] and [REDACTED]"
    for artifact in run_dir.rglob("*"):
        if artifact.is_file():
            artifact_text = artifact.read_text(encoding="utf-8", errors="ignore")
            assert secret not in artifact_text
            assert runtime_secret not in artifact_text


def test_minecraft_runtime_config_error_finalizes_failed_attempt(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)
    monkeypatch.setattr(
        "model.ollama_config.make_ollama_llm_config",
        lambda: (_ for _ in ()).throw(RuntimeError("config failed")),
    )

    with pytest.raises(RuntimeError, match="config failed"):
        run_minecraft_experiment(
            config_path=config_path,
            output_root=tmp_path / "result",
            run_name="config_failure",
            execute=True,
            execute_timeout_seconds=30,
        )

    run_dir = tmp_path / "result" / "config_failure"
    manifest = json.loads((run_dir / "artifact_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert not (run_dir / COMPLETION_MARKER_FILE).exists()
    row = summarize_inputs([run_dir])[0]
    assert row["status"] == "failed"
    assert row["success_rate"] is None
    assert row["task_completion_rate"] is None


def test_minecraft_unexpected_artifact_error_finalizes_failed_attempt(tmp_path, monkeypatch):
    config_path = _write_minecraft_config(tmp_path)
    monkeypatch.setattr(
        "benchmarks.minecraft.experiment.build_minecraft_dual_dag_artifact",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("artifact failed")),
    )

    with pytest.raises(RuntimeError, match="artifact failed"):
        run_minecraft_experiment(
            config_path=config_path,
            output_root=tmp_path / "result",
            run_name="unexpected_failure",
        )
    run_dir = tmp_path / "result" / "unexpected_failure"
    manifest = json.loads((run_dir / "artifact_manifest.json").read_text(encoding="utf-8"))

    assert manifest["status"] == "failed"
    assert not (run_dir / COMPLETION_MARKER_FILE).exists()


def test_bounded_runtime_terminates_child_when_join_is_interrupted(tmp_path, monkeypatch):
    class FakeQueue:
        closed = False

        def close(self):
            self.closed = True

    class FakeProcess:
        exitcode = None
        alive = False
        terminated = False

        def start(self):
            self.alive = True

        def join(self, timeout=None):
            if not self.terminated:
                raise KeyboardInterrupt()

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.terminated = True
            self.alive = False
            self.exitcode = -15

        def kill(self):
            raise AssertionError("terminated process must not be killed")

    process = FakeProcess()
    status_queue = FakeQueue()

    class FakeContext:
        def Queue(self):
            return status_queue

        def Process(self, **kwargs):
            return process

    monkeypatch.setattr(
        "benchmarks.minecraft.experiment.multiprocessing.get_context",
        lambda: FakeContext(),
    )

    with pytest.raises(KeyboardInterrupt):
        _execute_real_runtime_bounded(
            {},
            dual_dag_config={},
            timeout_seconds=10,
            runtime_result_path=tmp_path / "runtime_result.json",
        )

    assert process.terminated is True
    assert process.is_alive() is False
    assert status_queue.closed is True


@pytest.mark.parametrize(
    "child_status",
    [
        None,
        {},
        {"schema_version": 1, "status": "running"},
    ],
)
def test_child_protocol_rejects_missing_or_nonterminal_status(child_status):
    with pytest.raises(MinecraftRuntimeChildError) as raised:
        _validate_child_status(
            child_status,
            expected_attempt_id="attempt-a",
            expected_task_name="runtime-task-a",
            process_metadata={"exit_code": 0},
        )

    assert raised.value.error_type == "ChildProtocolError"
    assert raised.value.child_protocol["status_received"] is (child_status is not None)


def test_child_protocol_accepts_error_status_without_failure_checkpoint():
    protocol = _validate_child_status(
        {
            "schema_version": 1,
            "status": "error",
            "attempt_id": "attempt-a",
            "task_name": "runtime-task-a",
            "error": "primary failure",
            "error_type": "RuntimeError",
            "result_written": False,
        },
        expected_attempt_id="attempt-a",
        expected_task_name="runtime-task-a",
        process_metadata={"exit_code": 1},
    )

    assert protocol["status"] == "error"
    assert protocol["result_valid"] is False


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (None, "without a runtime result"),
        ([], "must be an object"),
        ({"attempt_id": "stale", "task_name": "runtime-task-a", "error": None}, "attempt mismatch"),
        ({"attempt_id": "attempt-a", "task_name": "stale", "error": None}, "task mismatch"),
        ({"attempt_id": "attempt-a", "task_name": "runtime-task-a", "error": "runtime failed"}, "runtime failed"),
    ],
)
def test_completed_child_requires_valid_owned_error_free_result(tmp_path, payload, message):
    result_path = tmp_path / "runtime_result.json"
    if payload is not None:
        result_path.write_text(json.dumps(payload), encoding="utf-8")
    protocol = {
        "schema_version": 1,
        "status_received": True,
        "status": "completed",
        "exit_code": 0,
        "result_valid": False,
    }

    with pytest.raises(MinecraftRuntimeChildError, match=message):
        _read_completed_runtime_result(
            result_path,
            expected_attempt_id="attempt-a",
            expected_task_name="runtime-task-a",
            process_metadata={"exit_code": 0},
            child_protocol=protocol,
        )


def _write_minecraft_config(tmp_path):
    config_path = tmp_path / "minecraft_config.json"
    config_path.write_text(
        json.dumps({
            "task_type": "meta",
            "task_idx": 0,
            "agent_num": 1,
            "task_goal": "Find the village bell",
            "host": "127.0.0.1",
            "port": 25565,
            "task_name": "bounded_execute",
            "task_scenario": "move",
        }),
        encoding="utf-8",
    )
    return config_path


class _StubbornProcess:
    def __init__(self):
        self.calls = []
        self.exitcode = None
        self._alive = True

    def terminate(self):
        self.calls.append("terminate")

    def join(self, timeout=None):
        self.calls.append(("join", timeout))
        if timeout is None:
            self._alive = False
            self.exitcode = -9

    def is_alive(self):
        return self._alive

    def kill(self):
        self.calls.append("kill")


def _runtime_result_snapshot(status="success"):
    return {
        "score": {"progress": 1.0 if status == "success" else 0.5},
        "action_log": {"Alice": [{"action": "mock", "result": {"status": True}}]},
        "runtime_task_dag_snapshot": {
            "schema_version": "1.0.0",
            "runtime": "runtime_task_dag_store",
            "source_of_truth": "runtime_task_dag",
            "summary": {"task_node_count": 1, "task_edge_count": 0, "terminal_state": status},
            "nodes": [{
                "node_id": "runtime:task:mock",
                "node_type": "runtime_task",
                "content": {"description": "Runtime task", "metadata": {}, "milestones": [], "reflect": None},
                "lifecycle": {
                    "status": status,
                    "candidate_agents": ["Alice"],
                    "active_agents": ["Alice"] if status == "running" else [],
                    "last_assigned_agents": ["Alice"],
                    "required_agent_count": 1,
                },
                "derived": {"dependency_ready": True, "blocked_by_tasks": []},
                "provenance": {"source": "test"},
            }],
            "edges": [],
            "schema": {},
        },
        "task_graph_snapshot": {
            "mutates_runtime": False,
            "tasks": [{"description": "Runtime task", "status": status}],
            "edges": [],
        },
        "error": None,
        "bridge_cleanup": {"cleanup_complete": True, "processes": {}},
    }


def _safe_runtime_process_metadata(*, exit_code=0):
    return {
        "exit_code": exit_code,
        "terminated": False,
        "killed": False,
        "process_alive_after_kill": False,
        "process_group_alive_after_kill": False,
    }


def _artifact_admission_fixture():
    summary = {
        "final_score": {"status": "success", "score": 100},
        "score_available": True,
        "score_ownership_verified": True,
        "action_log_available": True,
        "events_available": True,
        "event_artifact_error": None,
        "bridge_cleanup": {
            "cleanup_complete": True,
            "processes": {"Alice": {"alive_after_kill": False}},
        },
        "child_protocol": {
            "status": "completed",
            "result_written": True,
            "result_valid": True,
        },
        "runtime_collection_errors": [],
        "controller_shutdown_complete": True,
        "controller_active_assignments": {},
        "runtime_target_safe_to_reuse": True,
    }
    snapshot = {
        "summary": {"terminal_state": "success"},
        "nodes": [{
            "lifecycle": {"status": "success", "active_agents": []},
        }],
    }
    return summary, snapshot, {"Alice": [{"action": "navigateTo"}]}


def _read_public_events(run_dir: Path):
    return [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def _admitted_event_fixture():
    return finalize_attempt_events(
        (),
        run_id="run",
        attempt_id="attempt-a",
        mode="execute",
        started_at="start",
        finished_at="finish",
        terminal_event_type="run_completed",
        error=None,
        error_type=None,
    ).events


def _minecraft_config(task_name):
    return {
        "task_type": "meta",
        "task_idx": 0,
        "agent_num": 1,
        "task_goal": task_name,
        "host": "127.0.0.1",
        "port": 25565,
        "task_name": task_name,
    }
