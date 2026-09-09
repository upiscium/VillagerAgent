import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain.agents import tool
from langchain_core.callbacks.manager import CallbackManager

from env.env import VillagerBench, env_type
from env.judger_artifacts import ScoreOwnershipError, TerminalArtifactWriter
from env.minecraft_client import (
    Agent as MinecraftAgent,
    AgentExecutionCancelledError,
    CancellationCallbackHandler,
    MinecraftActionLogError,
    MinecraftBridgeCleanupError,
    MinecraftToolEffectUnknownError,
    MinecraftToolTimeoutError,
    ToolActionBlockedError,
    _minecraft_request,
    timeit,
)
from env.runtime_paths import RuntimePaths, atomic_write_json, read_json_artifact
from pipeline.controller_tiny import ControllerShutdownError
from env.minecraft_bridge_diagnostics import (
    MOVEMENT_FAILURE_REASON_HEADER,
    MOVEMENT_TERMINAL_HEADER,
    OUTCOME_CERTAINTY_HEADER,
    RETRY_SAFE_HEADER,
)
from env.movement_http_contract import movement_effect_unknown_response
from env.movement_runtime import MovementEffectUnknownError
from pipeline.agent import BaseAgent
from start_with_config import (
    RuntimeDocumentResolution,
    _load_runtime_document,
    _resolve_runtime_document_path,
    _with_runtime_paths,
)


def test_default_runtime_paths_preserve_legacy_layout(tmp_path):
    paths = RuntimePaths.legacy(tmp_path)

    assert paths.meta_setting == tmp_path / ".cache" / "meta_setting.json"
    assert paths.load_status == tmp_path / ".cache" / "load_status.cache"
    assert paths.score == tmp_path / "data" / "score.json"
    assert paths.run_result_dir("run-a") == tmp_path / "result" / "run-a"


def test_isolated_runtime_paths_stay_under_attempt_root(tmp_path):
    paths = RuntimePaths.isolated(tmp_path / "attempt-a")

    assert paths.cache_dir == tmp_path / "attempt-a" / "cache"
    assert paths.score == tmp_path / "attempt-a" / "data" / "score.json"
    assert paths.meta_judger_phase == tmp_path / "attempt-a" / "cache" / "meta_judger_phase.cache"
    assert paths.task_list_log == tmp_path / "attempt-a" / "logs" / "task_list.json"
    assert paths.recipe_hint == tmp_path / "attempt-a" / "data" / "recipe_hint.json"
    assert paths.build_map == tmp_path / "attempt-a" / "data" / "map.json"
    assert paths.map_description == tmp_path / "attempt-a" / "data" / "map_description.json"
    assert paths.openai_log == tmp_path / "attempt-a" / "data" / "openai.logs"
    assert paths.openai_cache == tmp_path / "attempt-a" / "cache" / "openai.cache"
    assert paths.llm_inference == tmp_path / "attempt-a" / "data" / "llm_inference.json"


def test_openai_artifact_paths_preserve_legacy_relative_layout(tmp_path):
    paths = RuntimePaths.legacy(tmp_path)

    assert paths.tokens == tmp_path / "data" / "tokens.json"
    assert paths.openai_log == tmp_path / "data" / "openai.logs"
    assert paths.openai_cache == tmp_path / ".cache" / "openai.cache"
    assert paths.llm_inference == tmp_path / "data" / "llm_inference.json"


def test_runtime_subprocess_environment_supports_direct_bridge_entrypoint(tmp_path):
    repository_root = Path(__file__).resolve().parents[1]
    base = dict(os.environ, PYTHONPATH="/existing/python/path")
    environment = RuntimePaths.isolated(tmp_path / "attempt").subprocess_environment(
        base
    )

    assert environment["PYTHONPATH"] == str(repository_root)
    completed = subprocess.run(
        [sys.executable, "env/minecraft_server_fast.py", "--help"],
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout


def test_runtime_generated_documents_do_not_cross_attempt_roots(tmp_path):
    first = RuntimePaths.isolated(tmp_path / "attempt-a")
    second = RuntimePaths.isolated(tmp_path / "attempt-b")

    atomic_write_json(first.recipe_hint, [{"result": "first"}])
    atomic_write_json(second.recipe_hint, [{"result": "second"}])
    atomic_write_json(first.task_list_log, {"task_list": ["first"]})
    atomic_write_json(second.task_list_log, {"task_list": ["second"]})

    assert read_json_artifact(first.recipe_hint).value == [{"result": "first"}]
    assert read_json_artifact(second.recipe_hint).value == [{"result": "second"}]
    assert read_json_artifact(first.task_list_log).value == {"task_list": ["first"]}
    assert read_json_artifact(second.task_list_log).value == {"task_list": ["second"]}


def test_generated_document_paths_resolve_under_runtime_root(tmp_path):
    paths = RuntimePaths.isolated(tmp_path / "attempt")

    assert _resolve_runtime_document_path("data\\recipe_hint.json", paths) == RuntimeDocumentResolution(
        paths.recipe_hint, "generated"
    )
    assert _resolve_runtime_document_path("data/map_description.json", paths) == RuntimeDocumentResolution(
        paths.map_description, "generated"
    )
    assert _resolve_runtime_document_path("data/recipes.json", paths) == RuntimeDocumentResolution(
        Path("data/recipes.json"), "external"
    )
    assert _resolve_runtime_document_path(None, paths) == RuntimeDocumentResolution(None, "none")
    assert _resolve_runtime_document_path("", paths) == RuntimeDocumentResolution(None, "none")
    assert _resolve_runtime_document_path("   ", paths) == RuntimeDocumentResolution(None, "none")


@pytest.mark.parametrize(
    "value",
    [
        "/opt/custom/recipe_hint.json",
        "./fixtures/recipe_hint.json",
        "custom/recipe_hint.json",
        r"C:\external\recipe_hint.json",
    ],
)
def test_custom_same_basename_document_is_not_substituted(tmp_path, value):
    paths = RuntimePaths.isolated(tmp_path / "attempt")

    assert _resolve_runtime_document_path(value, paths) == RuntimeDocumentResolution(
        Path(value.strip()), "external"
    )


@pytest.mark.parametrize("value", [0, False, [], {}])
def test_runtime_document_resolution_rejects_non_string(value, tmp_path):
    with pytest.raises(ValueError, match="string or null"):
        _resolve_runtime_document_path(value, RuntimePaths.isolated(tmp_path / "attempt"))


def test_runtime_document_loader_distinguishes_generated_and_external_missing(tmp_path):
    generated = RuntimeDocumentResolution(tmp_path / "generated.json", "generated")
    external = RuntimeDocumentResolution(tmp_path / "external.json", "external")

    assert _load_runtime_document(generated) is None
    with pytest.raises(FileNotFoundError, match="does not exist"):
        _load_runtime_document(external)


def test_runtime_document_loader_reads_custom_same_basename_file(tmp_path):
    custom = tmp_path / "custom" / "recipe_hint.json"
    custom.parent.mkdir()
    custom.write_text('[{"custom": true}]', encoding="utf-8")

    assert _load_runtime_document(RuntimeDocumentResolution(custom, "external")) == [
        {"custom": True}
    ]


def test_runtime_document_loader_rejects_directory_and_invalid_json(tmp_path):
    with pytest.raises(ValueError, match="regular file"):
        _load_runtime_document(RuntimeDocumentResolution(tmp_path, "external"))

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        _load_runtime_document(RuntimeDocumentResolution(invalid, "external"))


def test_runtime_document_loader_rejects_symbolic_link(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="symbolic link"):
        _load_runtime_document(RuntimeDocumentResolution(link, "external"))


def test_runtime_write_audit_has_no_known_global_judger_outputs():
    repository_root = Path(__file__).resolve().parents[1]
    sources = [
        repository_root / "pipeline" / "controller_tiny.py",
        repository_root / "pipeline" / "controller.py",
        repository_root / "env" / "meta_judger.py",
        repository_root / "env" / "farm_craft_judger.py",
        repository_root / "env" / "build_judger.py",
        repository_root / "env" / "env.py",
    ]
    forbidden = (
        'open("logs/task_list.json", "w")',
        'open("data/recipe_hint.json", "w")',
        "open('data/blueprint_description_all.json', 'w')",
        'open("data/map.json", \'w\')',
        "open('data/map_description.json', 'w')",
    )

    combined = "\n".join(path.read_text(encoding="utf-8") for path in sources)
    assert all(pattern not in combined for pattern in forbidden)
    assert 'open("data/recipes.json", "r")' in combined


def test_atomic_json_reader_ignores_temporary_file(tmp_path):
    target = tmp_path / "status.json"
    temporary = tmp_path / ".status.json.123.tmp"
    temporary.write_text('{"status": "end"}', encoding="utf-8")

    assert read_json_artifact(target).state == "absent"

    atomic_write_json(target, {"status": "loaded"})
    result = read_json_artifact(target)
    assert result.state == "valid"
    assert result.value == {"status": "loaded"}


def test_runtime_path_environment_is_restored(tmp_path, monkeypatch):
    monkeypatch.setenv("VILLAGER_RUNTIME_ROOT", "/previous")
    monkeypatch.setenv("VILLAGER_RUNTIME_LAYOUT", "legacy")
    paths = RuntimePaths.isolated(tmp_path / "attempt")

    with paths.activated():
        assert os.environ["VILLAGER_RUNTIME_ROOT"] == str(paths.root.resolve())
        assert os.environ["VILLAGER_RUNTIME_LAYOUT"] == "isolated"

    assert os.environ["VILLAGER_RUNTIME_ROOT"] == "/previous"
    assert os.environ["VILLAGER_RUNTIME_LAYOUT"] == "legacy"


def test_runtime_path_wrapper_accepts_positional_runtime_paths(tmp_path):
    paths = RuntimePaths.isolated(tmp_path / "attempt")

    @_with_runtime_paths
    def wrapped(value, runtime_paths=None):
        return value, runtime_paths, os.environ["VILLAGER_RUNTIME_LAYOUT"]

    assert wrapped("value", paths) == ("value", paths, "isolated")


def test_environment_reads_injected_score_and_status_paths(tmp_path):
    paths = RuntimePaths.isolated(tmp_path / "attempt")
    paths.ensure_directories()
    atomic_write_json(paths.score, {"score": 100})
    atomic_write_json(paths.load_status, {"status": "end"})
    environment = object.__new__(VillagerBench)
    environment.env_type = env_type.meta
    environment.runtime_paths = paths
    environment._invalid_status_reads = 0

    assert environment.get_score() == {"score": 100}
    assert environment.is_task_complete() is True


def test_environment_reads_construction_metadata_from_runtime_root(tmp_path):
    paths = RuntimePaths.isolated(tmp_path / "attempt")
    atomic_write_json(paths.map_description, ["isolated map"])
    environment = object.__new__(VillagerBench)
    environment.env_type = env_type.construction
    environment.runtime_paths = paths

    assert environment.get_metadata() == ["isolated map"]


def test_minecraft_interaction_history_uses_injected_runtime_path(tmp_path):
    paths = RuntimePaths.isolated(tmp_path / "attempt")
    agent = object.__new__(MinecraftAgent)
    agent.runtime_paths = paths

    agent._save_interaction_history(
        {"input": "move to target"},
        [{"action": {"tool": "navigateTo"}, "feedback": {"status": True}}],
        "arrived",
    )

    history_files = list(paths.history_dir.iterdir())
    assert len(history_files) == 1
    assert read_json_artifact(history_files[0]).value == {
        "input": "move to target",
        "action_list": [
            {"action": {"tool": "navigateTo"}, "feedback": {"status": True}}
        ],
        "final_answer": "arrived",
    }
    assert not (tmp_path / "data" / "history").exists()


def test_minecraft_url_prefix_uses_injected_runtime_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths = RuntimePaths.isolated(tmp_path / "attempt")
    legacy = RuntimePaths.legacy(tmp_path)
    atomic_write_json(legacy.url_prefix, {"Alice": "http://localhost:9999"})

    try:
        MinecraftAgent("Alice", runtime_paths=paths)
        assert MinecraftAgent.get_url_prefix(paths) == {"Alice": "http://localhost:5000"}
        assert MinecraftAgent.get_agent_url("Alice") == "http://localhost:5000"
    finally:
        MinecraftAgent.kill()

    assert read_json_artifact(paths.url_prefix).value == {"Alice": "http://localhost:5000"}
    assert read_json_artifact(legacy.url_prefix).value == {"Alice": "http://localhost:9999"}


def test_minecraft_url_registries_are_isolated_without_activation(tmp_path):
    first = RuntimePaths.isolated(tmp_path / "attempt-a")
    second = RuntimePaths.isolated(tmp_path / "attempt-b")

    try:
        MinecraftAgent("Alice", local_port=5001, runtime_paths=first)
        MinecraftAgent("Bob", local_port=5002, runtime_paths=second)

        assert MinecraftAgent.get_agent_url("Alice") == "http://localhost:5001"
        assert MinecraftAgent.get_agent_url("Bob") == "http://localhost:5002"
        assert read_json_artifact(first.url_prefix).value == {"Alice": "http://localhost:5001"}
        assert read_json_artifact(second.url_prefix).value == {"Bob": "http://localhost:5002"}
    finally:
        MinecraftAgent.kill()


def test_minecraft_url_registry_read_modify_write_keeps_all_agents(tmp_path):
    paths = RuntimePaths.isolated(tmp_path / "attempt")

    try:
        MinecraftAgent("Alice", local_port=5001, runtime_paths=paths)
        MinecraftAgent("Bob", local_port=5002, runtime_paths=paths)

        assert read_json_artifact(paths.url_prefix).value == {
            "Alice": "http://localhost:5001",
            "Bob": "http://localhost:5002",
        }
    finally:
        MinecraftAgent.kill()


def test_minecraft_ping_uses_injected_registry_without_activation(tmp_path, monkeypatch):
    paths = RuntimePaths.isolated(tmp_path / "attempt")
    requested_urls = []

    class Response:
        @staticmethod
        def json():
            return {"status": True}

    monkeypatch.setattr(
        "env.minecraft_client._minecraft_request",
        lambda _method, url, **_kwargs: requested_urls.append(url) or Response(),
    )
    try:
        MinecraftAgent("Alice", local_port=5010, runtime_paths=paths)
        assert MinecraftAgent.ping("Alice") == {"status": True}
    finally:
        MinecraftAgent.kill()

    assert requested_urls == ["http://localhost:5010/post_ping"]


def test_minecraft_url_registry_rejects_unknown_agent(tmp_path):
    with pytest.raises(RuntimeError, match="No runtime paths registered"):
        MinecraftAgent.get_agent_url("Unknown")


def test_minecraft_kill_clears_runtime_registry_state(tmp_path):
    paths = RuntimePaths.isolated(tmp_path / "attempt")
    process = _FakeBridgeProcess(exit_on_terminate=True)
    MinecraftAgent("Alice", runtime_paths=paths)
    MinecraftAgent.agent_process["Alice"] = process

    MinecraftAgent.kill()

    assert "Alice" not in MinecraftAgent.runtime_paths_by_name
    assert "Alice" not in MinecraftAgent.name2port
    assert "Alice" not in MinecraftAgent.agent_process
    assert MinecraftAgent._action_log_locks == {}


class _FakeBridgeProcess:
    pid = 1234

    def __init__(self, *, exit_on_terminate=False, exit_on_kill=True):
        self.alive = True
        self.exit_on_terminate = exit_on_terminate
        self.exit_on_kill = exit_on_kill
        self.calls = []

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.calls.append("terminate")
        if self.exit_on_terminate:
            self.alive = False

    def kill(self):
        self.calls.append("kill")
        if self.exit_on_kill:
            self.alive = False

    def wait(self, timeout=None):
        self.calls.append(("wait", timeout))
        if self.alive:
            raise subprocess.TimeoutExpired("bridge", timeout)
        return 0


def test_bridge_cleanup_stops_after_terminate():
    process = _FakeBridgeProcess(exit_on_terminate=True)
    MinecraftAgent.agent_process["Alice"] = process

    result = MinecraftAgent.kill(terminate_grace_seconds=0.2, kill_grace_seconds=0.1)

    assert process.calls == ["terminate", ("wait", 0.2)]
    assert result["cleanup_complete"] is True
    assert result["processes"]["Alice"]["killed"] is False


def test_bridge_cleanup_escalates_to_bounded_kill():
    process = _FakeBridgeProcess(exit_on_kill=True)
    MinecraftAgent.agent_process["Alice"] = process

    result = MinecraftAgent.kill(terminate_grace_seconds=0.2, kill_grace_seconds=0.1)

    assert process.calls == [
        "terminate",
        ("wait", 0.2),
        "kill",
        ("wait", 0.1),
    ]
    assert result["cleanup_complete"] is True
    assert result["processes"]["Alice"]["alive_after_kill"] is False
    metadata = result["processes"]["Alice"]
    assert metadata["pid"] == 1234
    assert "process_group_id" in metadata
    assert "session_id" in metadata
    assert metadata["initial_poll"]["returncode"] is None
    assert metadata["initial_poll"]["completed"] is True
    assert metadata["terminate"]["attempted"] is True
    assert metadata["terminate"]["completed"] is True
    assert metadata["terminate_wait"]["budget_seconds"] == 0.2
    assert metadata["terminate_wait"]["timed_out"] is True
    assert metadata["post_terminate_poll"]["returncode"] is None
    assert metadata["kill"]["attempted"] is True
    assert metadata["kill"]["completed"] is True
    assert metadata["kill_wait"]["budget_seconds"] == 0.1
    assert metadata["kill_wait"]["completed"] is True
    assert metadata["kill_wait"]["returncode"] == 0
    assert metadata["final_poll"]["returncode"] == 0
    for stage_name in (
        "initial_poll", "terminate", "terminate_wait", "post_terminate_poll",
        "kill", "kill_wait", "final_poll",
    ):
        stage = metadata[stage_name]
        if stage["attempted"]:
            assert isinstance(stage["elapsed_ns"], int)
            assert stage["elapsed_ns"] >= 0
    assert metadata["exit_code"] == 0
    assert result["process_retention"] == {
        "capacity": 64,
        "retained": 1,
        "truncated": False,
        "dropped_count": 0,
    }


def test_bridge_cleanup_failure_preserves_process_mapping(tmp_path):
    process = _FakeBridgeProcess(exit_on_kill=False)
    MinecraftAgent.agent_process["Alice"] = process
    MinecraftAgent.runtime_paths_by_name["Alice"] = RuntimePaths.isolated(
        tmp_path / "attempt"
    )

    try:
        with pytest.raises(MinecraftBridgeCleanupError) as raised:
            MinecraftAgent.kill(terminate_grace_seconds=0.2, kill_grace_seconds=0.1)

        assert raised.value.cleanup_result["cleanup_complete"] is False
        assert raised.value.cleanup_result["processes"]["Alice"]["alive_after_kill"] is True
        assert MinecraftAgent.agent_process["Alice"] is process
        assert "Alice" in MinecraftAgent.runtime_paths_by_name
        assert MinecraftAgent._bridge_diagnostic_recorders == {}
    finally:
        MinecraftAgent.agent_process.clear()
        MinecraftAgent.runtime_paths_by_name.clear()
        MinecraftAgent.name2port.clear()
        MinecraftAgent.last_bridge_cleanup = None


def test_bridge_cleanup_records_signal_errors_and_all_stage_timings(tmp_path):
    class ErrorProcess(_FakeBridgeProcess):
        def terminate(self):
            self.calls.append("terminate")
            raise OSError("sensitive terminate detail")

        def kill(self):
            self.calls.append("kill")
            raise RuntimeError("sensitive kill detail")

    process = ErrorProcess(exit_on_kill=False)
    MinecraftAgent.agent_process["Alice"] = process
    MinecraftAgent.runtime_paths_by_name["Alice"] = RuntimePaths.isolated(
        tmp_path / "attempt"
    )

    try:
        with pytest.raises(MinecraftBridgeCleanupError) as raised:
            MinecraftAgent.kill(
                terminate_grace_seconds=0.01, kill_grace_seconds=0.01,
            )
        metadata = raised.value.cleanup_result["processes"]["Alice"]
        assert metadata["terminate"]["error_type"] == "OSError"
        assert metadata["terminate"]["error_text"] == "operation_failed"
        assert metadata["terminate_wait"]["timed_out"] is True
        assert metadata["post_terminate_poll"]["returncode"] is None
        assert metadata["kill"]["error_type"] == "RuntimeError"
        assert metadata["kill"]["error_text"] == "operation_failed"
        assert metadata["kill_wait"]["timed_out"] is True
        assert metadata["final_poll"]["returncode"] is None
        for stage_name in (
            "initial_poll", "terminate", "terminate_wait",
            "post_terminate_poll", "kill", "kill_wait", "final_poll",
        ):
            stage = metadata[stage_name]
            assert stage["attempted"] is True
            assert isinstance(stage["started_monotonic_ns"], int)
            assert isinstance(stage["completed_monotonic_ns"], int)
            assert isinstance(stage["elapsed_ns"], int)
        assert "sensitive" not in str(metadata)
        assert MinecraftAgent._bridge_diagnostic_recorders == {}
    finally:
        MinecraftAgent.agent_process.clear()
        MinecraftAgent.runtime_paths_by_name.clear()
        MinecraftAgent.name2port.clear()
        MinecraftAgent.last_bridge_cleanup = None


def test_tool_runtime_snapshot_marks_active_diagnostics_as_not_finalized():
    previous = MinecraftAgent.last_bridge_diagnostics
    MinecraftAgent.last_bridge_diagnostics = None
    try:
        snapshot = MinecraftAgent.tool_runtime_snapshot()
    finally:
        MinecraftAgent.last_bridge_diagnostics = previous

    assert snapshot["snapshot_source"] == "in_memory_only"
    assert snapshot["bridge_diagnostics"] is None
    assert snapshot["bridge_diagnostics_state"] == (
        "not_finalized_active_recorder_snapshot_unavailable"
    )


def test_environment_stop_is_idempotent_and_keeps_cleanup_result(monkeypatch):
    environment = object.__new__(VillagerBench)
    environment.running = True
    environment.bridge_cleanup_result = None
    environment.bridge_cleanup_error = None
    calls = []
    cleanup = {"processes": {}, "cleanup_complete": True}
    monkeypatch.setattr(
        MinecraftAgent,
        "kill",
        lambda: calls.append(True) or cleanup,
    )

    assert environment.stop() is cleanup
    assert environment.stop() is cleanup
    assert calls == [True]


def test_environment_stop_does_not_repeat_cleanup_failure(monkeypatch):
    environment = object.__new__(VillagerBench)
    environment.running = True
    environment.bridge_cleanup_result = None
    environment.bridge_cleanup_error = None
    calls = []
    cleanup = {
        "processes": {"Alice": {"alive_after_kill": True}},
        "cleanup_complete": False,
    }

    def fail_cleanup():
        calls.append(True)
        raise MinecraftBridgeCleanupError("cleanup failed", cleanup_result=cleanup)

    monkeypatch.setattr(MinecraftAgent, "kill", fail_cleanup)

    with pytest.raises(MinecraftBridgeCleanupError):
        environment.stop()
    assert environment.stop() is cleanup
    assert calls == [True]


def test_environment_run_preserves_primary_error_and_attaches_cleanup_failure(tmp_path):
    environment = object.__new__(VillagerBench)
    environment._virtual_debug = True
    environment.logger = logging.getLogger("test-environment-error-chain")
    environment.runtime_paths = RuntimePaths.isolated(tmp_path / "attempt")
    environment.runtime_paths.ensure_directories()
    cleanup_result = {
        "processes": {"Alice": {"alive_after_kill": True}},
        "cleanup_complete": False,
    }
    cleanup_error = MinecraftBridgeCleanupError(
        "cleanup failed", cleanup_result=cleanup_result,
    )
    environment.stop = lambda: (_ for _ in ()).throw(cleanup_error)
    primary_error = RuntimeError("controller failed")

    with pytest.raises(RuntimeError) as raised:
        with environment.run():
            raise primary_error

    assert raised.value is primary_error
    assert raised.value.__cause__ is cleanup_error
    assert raised.value.cleanup_error is cleanup_error
    assert raised.value.cleanup_failure == {
        "error_type": "MinecraftBridgeCleanupError",
        "cleanup_result": cleanup_result,
    }
    assert environment.runtime_cleanup_failure == raised.value.cleanup_failure


def test_controller_error_and_real_bridge_nonexit_cleanup_remain_separate(tmp_path):
    environment = object.__new__(VillagerBench)
    environment._virtual_debug = True
    environment.running = True
    environment.bridge_cleanup_result = None
    environment.bridge_cleanup_error = None
    environment.logger = logging.getLogger("test-controller-bridge-error-chain")
    environment.runtime_paths = RuntimePaths.isolated(tmp_path / "attempt")
    environment.runtime_paths.ensure_directories()
    process = _FakeBridgeProcess(exit_on_kill=False)
    MinecraftAgent.agent_process["Alice"] = process
    MinecraftAgent.runtime_paths_by_name["Alice"] = environment.runtime_paths
    primary_error = ControllerShutdownError("shutdown incomplete")

    try:
        with pytest.raises(ControllerShutdownError) as raised:
            with environment.run():
                raise primary_error

        assert raised.value is primary_error
        assert isinstance(raised.value.__cause__, MinecraftBridgeCleanupError)
        chain = environment.runtime_failure_chain
        assert chain["primary_failure"] == {
            "error_type": "ControllerShutdownError",
        }
        cleanup = chain["cleanup_failure"]["cleanup_result"]
        assert cleanup["cleanup_complete"] is False
        assert cleanup["processes"]["Alice"]["kill_wait"]["timed_out"] is True
        assert cleanup["processes"]["Alice"]["final_poll"]["returncode"] is None
    finally:
        MinecraftAgent.agent_process.clear()
        MinecraftAgent.runtime_paths_by_name.clear()
        MinecraftAgent.name2port.clear()
        MinecraftAgent.last_bridge_cleanup = None


def test_environment_run_records_generic_cleanup_failure_with_primary(tmp_path):
    environment = object.__new__(VillagerBench)
    environment._virtual_debug = True
    environment.logger = logging.getLogger("test-environment-generic-cleanup")
    environment.runtime_paths = RuntimePaths.isolated(tmp_path / "attempt")
    environment.runtime_paths.ensure_directories()
    cleanup_error = OSError("cleanup failed")
    environment.stop = lambda: (_ for _ in ()).throw(cleanup_error)
    primary_error = RuntimeError("controller failed")

    with pytest.raises(RuntimeError) as raised:
        with environment.run():
            raise primary_error

    assert raised.value is primary_error
    assert raised.value.__cause__ is cleanup_error
    assert environment.runtime_failure_chain == {
        "primary_failure": {"error_type": "RuntimeError"},
        "cleanup_failure": {"error_type": "OSError"},
    }


def test_environment_run_records_cleanup_only_generic_failure(tmp_path):
    environment = object.__new__(VillagerBench)
    environment._virtual_debug = True
    environment.logger = logging.getLogger("test-environment-cleanup-only")
    environment.runtime_paths = RuntimePaths.isolated(tmp_path / "attempt")
    environment.runtime_paths.ensure_directories()
    cleanup_error = OSError("cleanup failed")
    environment.stop = lambda: (_ for _ in ()).throw(cleanup_error)

    with pytest.raises(OSError) as raised:
        with environment.run():
            pass

    assert raised.value is cleanup_error
    assert environment.runtime_failure_chain == {
        "primary_failure": None,
        "cleanup_failure": {"error_type": "OSError"},
    }


def test_environment_run_cleans_up_after_base_exception(tmp_path):
    environment = object.__new__(VillagerBench)
    environment._virtual_debug = True
    environment.logger = logging.getLogger("test-environment-base-exception")
    environment.runtime_paths = RuntimePaths.isolated(tmp_path / "attempt")
    environment.runtime_paths.ensure_directories()
    cleanup_calls = []
    environment.stop = lambda: cleanup_calls.append(True)
    interrupt = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt) as raised:
        with environment.run():
            raise interrupt

    assert raised.value is interrupt
    assert cleanup_calls == [True]


def test_environment_run_clears_stale_failure_chain(tmp_path):
    environment = object.__new__(VillagerBench)
    environment._virtual_debug = True
    environment.logger = logging.getLogger("test-environment-failure-reset")
    environment.runtime_paths = RuntimePaths.isolated(tmp_path / "attempt")
    environment.runtime_paths.ensure_directories()
    environment.runtime_failure_chain = {"cleanup_failure": {"stale": True}}
    environment.runtime_cleanup_failure = {"stale": True}
    environment.stop = lambda: None

    with environment.run():
        assert environment.runtime_failure_chain is None
        assert environment.runtime_cleanup_failure is None


def test_action_log_uses_injected_paths_without_activation(tmp_path):
    paths = RuntimePaths.isolated(tmp_path / "attempt")
    legacy = RuntimePaths.legacy(tmp_path)
    atomic_write_json(legacy.action_log, {"legacy": []})
    MinecraftAgent.runtime_paths_by_name["Alice"] = paths

    try:
        MinecraftAgent.append_action_log("Alice", {"action": "placeBlock"})
    finally:
        MinecraftAgent.kill()

    assert read_json_artifact(paths.action_log).value == {
        "Alice": [{"action": "placeBlock"}]
    }
    assert read_json_artifact(legacy.action_log).value == {"legacy": []}


def test_action_logs_are_isolated_between_runtime_roots(tmp_path):
    first = RuntimePaths.isolated(tmp_path / "attempt-a")
    second = RuntimePaths.isolated(tmp_path / "attempt-b")
    MinecraftAgent.runtime_paths_by_name.update({"Alice": first, "Bob": second})

    try:
        MinecraftAgent.append_action_log("Alice", {"action": "first"})
        MinecraftAgent.append_action_log("Bob", {"action": "second"})
    finally:
        MinecraftAgent.kill()

    assert read_json_artifact(first.action_log).value == {
        "Alice": [{"action": "first"}]
    }
    assert read_json_artifact(second.action_log).value == {
        "Bob": [{"action": "second"}]
    }


@pytest.mark.parametrize("same_agent", [False, True])
def test_concurrent_action_log_appends_do_not_lose_entries(tmp_path, same_agent):
    paths = RuntimePaths.isolated(tmp_path / "attempt")
    names = ["Alice", "Alice" if same_agent else "Bob"]
    MinecraftAgent.runtime_paths_by_name.update({name: paths for name in names})
    barrier = threading.Barrier(3)
    errors = []

    def append(name, index):
        try:
            barrier.wait()
            MinecraftAgent.append_action_log(name, {"action": f"action-{index}"})
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=append, args=(name, index))
        for index, name in enumerate(names)
    ]
    try:
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(2)
    finally:
        MinecraftAgent.kill()

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    action_log = read_json_artifact(paths.action_log).value
    assert sum(len(entries) for entries in action_log.values()) == 2
    assert sorted(
        entry["action"] for entries in action_log.values() for entry in entries
    ) == ["action-0", "action-1"]


@pytest.mark.parametrize("content", ["{", "[]"])
def test_action_log_rejects_malformed_existing_artifact(tmp_path, content):
    paths = RuntimePaths.isolated(tmp_path / "attempt")
    paths.ensure_directories()
    paths.action_log.write_text(content, encoding="utf-8")
    MinecraftAgent.runtime_paths_by_name["Alice"] = paths

    try:
        with pytest.raises(MinecraftActionLogError):
            MinecraftAgent.append_action_log("Alice", {"action": "placeBlock"})
    finally:
        MinecraftAgent.kill()


def test_action_log_failure_does_not_repeat_completed_tool(tmp_path, monkeypatch):
    paths = RuntimePaths.isolated(tmp_path / "attempt")
    paths.ensure_directories()
    paths.action_log.write_text("{", encoding="utf-8")
    atomic_write_json(paths.url_prefix, {"Alice": "http://localhost:5000"})
    MinecraftAgent.runtime_paths_by_name["Alice"] = paths
    calls = []

    class Response:
        @staticmethod
        def json():
            return {"status": True}

    monkeypatch.setattr(
        "env.minecraft_client._minecraft_request",
        lambda *_args, **_kwargs: Response(),
    )

    @timeit
    def completed_action(*, player_name, emotion, murmur):
        calls.append(player_name)
        return {"message": "done", "status": True}

    try:
        with pytest.raises(MinecraftActionLogError):
            completed_action(player_name="Alice", emotion=[], murmur="")
    finally:
        MinecraftAgent.kill()

    assert calls == ["Alice"]


@pytest.mark.parametrize("content", ["{", "[]", '{"Alice": {}}'])
def test_environment_get_action_log_rejects_invalid_schema(tmp_path, content):
    paths = RuntimePaths.isolated(tmp_path / "attempt")
    paths.ensure_directories()
    paths.action_log.write_text(content, encoding="utf-8")
    environment = object.__new__(VillagerBench)
    environment.runtime_paths = paths

    with pytest.raises(MinecraftActionLogError):
        environment.get_action_log()


def test_environment_get_action_log_accepts_valid_agent_lists(tmp_path):
    paths = RuntimePaths.isolated(tmp_path / "attempt")
    action_log = {
        "Alice": [{"action": "navigateTo"}],
        "_attempt_id": "attempt-a",
    }
    atomic_write_json(paths.action_log, action_log)
    environment = object.__new__(VillagerBench)
    environment.runtime_paths = paths

    assert environment.get_action_log() == action_log


def test_judger_terminal_artifact_cannot_be_overwritten(tmp_path):
    paths = RuntimePaths.isolated(tmp_path / "attempt")
    writer = TerminalArtifactWriter(paths, paths.run_result_dir("run-a"))
    success = {
        "attempt_id": "attempt-a",
        "task_name": "run-a",
        "status": "success",
        "score": 100,
    }

    config = {"attempt_id": "attempt-a", "task_name": "run-a"}
    assert writer.write(success, config) is True
    assert writer.write({"status": "failure"}, config) is False

    assert read_json_artifact(paths.score).value == success
    assert read_json_artifact(paths.load_status).value == {"status": "end"}
    assert read_json_artifact(paths.run_result_dir("run-a") / "score.json").value == success


def test_judger_terminal_writer_rejects_missing_identity(tmp_path):
    paths = RuntimePaths.isolated(tmp_path / "attempt")
    writer = TerminalArtifactWriter(paths, paths.run_result_dir("run-a"))

    try:
        writer.write(
            {"task_name": "run-a", "status": "success", "score": 100},
            {"attempt_id": "attempt-a", "task_name": "run-a"},
        )
    except ScoreOwnershipError as exc:
        assert "missing: attempt_id" in str(exc)
    else:
        raise AssertionError("terminal score without explicit ownership must be rejected")


def test_environment_guarded_tool_balances_action_barrier():
    environment = object.__new__(VillagerBench)
    calls = []
    environment._tool_action_enter = lambda: calls.append("enter")
    environment._tool_action_exit = lambda: calls.append("exit")

    @tool
    def sample_action(value: int) -> dict:
        """Return the supplied value."""
        calls.append(("action", value))
        return {"message": str(value), "status": True}

    guarded = environment.guard_tool_actions([sample_action])[0]

    assert guarded.invoke({"value": 3}) == {"message": "3", "status": True}
    assert calls == ["enter", ("action", 3), "exit"]


def test_environment_guarded_tool_does_not_run_after_barrier_rejection():
    environment = object.__new__(VillagerBench)
    calls = []
    environment._tool_action_enter = lambda: (_ for _ in ()).throw(
        RuntimeError("barrier closed")
    )
    environment._tool_action_exit = lambda: calls.append("exit")

    @tool
    def sample_action(value: int) -> dict:
        """Return the supplied value."""
        calls.append(("action", value))
        return {"message": str(value), "status": True}

    guarded = environment.guard_tool_actions([sample_action])[0]

    with pytest.raises(RuntimeError, match="barrier closed"):
        guarded.invoke({"value": 3})
    assert calls == []


def test_environment_step_forwards_exact_cancellation_token_and_phase_callback():
    environment = object.__new__(VillagerBench)
    environment.logger = SimpleNamespace(debug=lambda *_: None, info=lambda *_: None)
    environment.agent_iteration_limit = None
    environment.log = {"Alice": []}
    token = threading.Event()
    phases = []
    observed = {}

    class FakeAgent:
        name = "Alice"
        tools = []

        def run(self, action, **kwargs):
            observed.update(kwargs)
            kwargs["phase_callback"]("fake_phase")
            return "done", {"final_answer": action}

    environment.agent_pool = [FakeAgent()]
    assert environment.step("Alice", "inspect", cancellation_token=token,
                            phase_callback=phases.append) == (
                                "done", {"final_answer": "inspect"})
    assert observed["cancellation_token"] is token
    assert observed["phase_callback"] is phases.append or callable(observed["phase_callback"])
    assert phases == ["fake_phase"]


def test_environment_step_does_not_append_log_after_cancellation():
    environment = object.__new__(VillagerBench)
    environment.logger = SimpleNamespace(debug=lambda *_: None, info=lambda *_: None)
    environment.agent_iteration_limit = None
    environment.log = {"Alice": []}
    token = threading.Event()

    class FakeAgent:
        name = "Alice"
        tools = []

        def run(self, _action, **kwargs):
            token.set()
            return "done", {"final_answer": "done"}

    environment.agent_pool = [FakeAgent()]
    with pytest.raises(AgentExecutionCancelledError) as raised:
        environment.step("Alice", "inspect", cancellation_token=token)
    assert raised.value.failure_detail["reason"] == "cancelled"
    assert environment.log["Alice"] == []


def test_invocation_local_tool_gate_blocks_before_authoritative_tool():
    environment = object.__new__(VillagerBench)
    token = threading.Event()
    calls = []

    @tool
    def sample_action(value: int) -> dict:
        """Return the supplied value."""
        calls.append(value)
        return {"status": True, "message": str(value)}

    gated = environment._cancellation_tools([sample_action], token, None)[0]
    token.set()
    with pytest.raises(AgentExecutionCancelledError):
        gated.invoke({"value": 4})
    assert calls == []

    token.clear()
    assert gated.invoke({"value": 5})["status"] is True
    assert calls == [5]


def test_cancellation_callback_handler_blocks_model_and_tool_admission():
    token = threading.Event()
    phases = []
    handler = CancellationCallbackHandler(token, phases.append)
    handler.on_llm_start({}, [])
    handler.on_tool_start({}, "input")
    assert phases == ["model_start", "tool_start"]
    token.set()
    with pytest.raises(AgentExecutionCancelledError) as raised:
        handler.on_llm_start({}, [])
    assert raised.value.failure_detail["reason"] == "cancelled"
    with pytest.raises(AgentExecutionCancelledError):
        handler.on_tool_start({}, "input")

    completion_phases = []
    completion_handler = CancellationCallbackHandler(
        token, completion_phases.append,
    )
    with pytest.raises(AgentExecutionCancelledError) as completed:
        completion_handler.on_llm_end(SimpleNamespace())
    assert completion_phases == ["model_end"]
    assert completed.value.failure_detail["blocking_operation_termination"] == "confirmed"


def test_langchain_callback_manager_propagates_cancellation_handler_error():
    token = threading.Event()
    token.set()
    manager = CallbackManager(
        handlers=[CancellationCallbackHandler(token)],
    )

    with pytest.raises(AgentExecutionCancelledError):
        manager.on_llm_start({}, ["prompt"])


def test_agent_run_installs_cancellation_handler_and_does_not_persist_after_cancel(
    monkeypatch, tmp_path,
):
    token = threading.Event()
    captured = []
    agent = object.__new__(MinecraftAgent)
    agent.name = "Alice"
    agent.model = "test"
    monkeypatch.setattr(MinecraftAgent, "api_key_list", ["test-key"])
    agent.tools = []
    agent.runtime_paths = RuntimePaths.isolated(tmp_path / "attempt")
    agent.reflection_output_dir = tmp_path / "results"
    persisted = []
    agent._save_interaction_history = lambda *args: persisted.append("save")
    agent.update_history = lambda *args: persisted.append("update")
    monkeypatch.setattr(MinecraftAgent, "provider", "ollama")
    monkeypatch.setattr("env.minecraft_client.OllamaReasoningChatOpenAI",
                        lambda **_kwargs: object())

    class FakeExecutor:
        handle_parsing_errors = False

        def __call__(self, _payload):
            token.set()
            return {"input": "inspect", "output": "done", "intermediate_steps": []}

    def initialize(**kwargs):
        captured.append(kwargs["callback_manager"])
        return FakeExecutor()

    monkeypatch.setattr("env.minecraft_client.initialize_agent", initialize)
    with pytest.raises(AgentExecutionCancelledError):
        agent.run("inspect", max_try_turn=1, cancellation_token=token)
    assert len(captured) == 1
    assert any(isinstance(handler, CancellationCallbackHandler)
               for handler in captured[0].handlers)
    assert persisted == []


def test_agent_run_keeps_blocking_provider_active_until_it_really_returns(
    monkeypatch, tmp_path,
):
    token = threading.Event()
    started = threading.Event()
    release = threading.Event()
    outcome = []
    agent = object.__new__(MinecraftAgent)
    agent.name = "Alice"
    agent.model = "test"
    agent.tools = []
    agent.runtime_paths = RuntimePaths.isolated(tmp_path / "attempt")
    agent.reflection_output_dir = tmp_path / "results"
    agent._save_interaction_history = lambda *_args: outcome.append("save")
    agent.update_history = lambda *_args: outcome.append("update")
    monkeypatch.setattr(MinecraftAgent, "api_key_list", ["test-key"])
    monkeypatch.setattr(MinecraftAgent, "provider", "ollama")
    monkeypatch.setattr(
        "env.minecraft_client.OllamaReasoningChatOpenAI", lambda **_kwargs: object(),
    )

    class BlockingExecutor:
        handle_parsing_errors = False

        def __call__(self, _payload):
            started.set()
            assert release.wait(1)
            return {"input": "inspect", "output": "done", "intermediate_steps": []}

    monkeypatch.setattr(
        "env.minecraft_client.initialize_agent", lambda **_kwargs: BlockingExecutor(),
    )

    def invoke():
        try:
            agent.run("inspect", max_try_turn=1, cancellation_token=token)
        except BaseException as error:
            outcome.append(error)

    worker = threading.Thread(target=invoke)
    worker.start()
    assert started.wait(1)
    token.set()
    worker.join(0.05)
    assert worker.is_alive()
    assert outcome == []

    release.set()
    worker.join(1)
    assert not worker.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], AgentExecutionCancelledError)


def test_minecraft_agent_does_not_retry_terminal_blocked_tool(monkeypatch):
    attempts = []
    agent = object.__new__(MinecraftAgent)
    agent.name = "Alice"
    agent.model = "test"
    agent.api_key_list = ["test-key"]
    agent.llm = object()
    agent.tools = []
    monkeypatch.setattr(MinecraftAgent, "provider", "ollama")
    monkeypatch.setattr(MinecraftAgent, "api_key_list", ["test-key"])
    monkeypatch.setattr(
        "env.minecraft_client.OllamaReasoningChatOpenAI",
        lambda **_kwargs: object(),
    )

    class BlockedExecutor:
        handle_parsing_errors = False

        def __call__(self, _input):
            attempts.append(True)
            raise ToolActionBlockedError("terminal barrier closed")

    monkeypatch.setattr(
        "env.minecraft_client.initialize_agent",
        lambda **_kwargs: BlockedExecutor(),
    )

    with pytest.raises(ToolActionBlockedError, match="terminal barrier closed"):
        agent.run("move", max_try_turn=10)
    assert attempts == [True]


def _capture_step_tools(monkeypatch, registered_tools, requested_tools, recommended_actions=()):
    captured = []
    agent = object.__new__(MinecraftAgent)
    agent.name = "Alice"
    agent.model = "test"
    agent.api_key_list = ["test-key"]
    agent.llm = object()
    agent.tools = registered_tools
    monkeypatch.setattr(MinecraftAgent, "provider", "ollama")
    monkeypatch.setattr(MinecraftAgent, "api_key_list", ["test-key"])
    monkeypatch.setattr("env.minecraft_client.random.shuffle", lambda unused: None)
    monkeypatch.setattr(
        "env.minecraft_client.OllamaReasoningChatOpenAI", lambda **unused: object())

    class StopExecutor:
        handle_parsing_errors = False

        def __call__(self, unused):
            raise ToolActionBlockedError("stop after tool selection")

    def initialize(**kwargs):
        captured.extend(kwargs["tools"])
        return StopExecutor()

    monkeypatch.setattr("env.minecraft_client.initialize_agent", initialize)
    with pytest.raises(ToolActionBlockedError, match="stop after tool selection"):
        agent.step(
            "move", max_try_turn=1, tools=requested_tools,
            recommended_actions=list(recommended_actions),
        )
    return captured


def test_minecraft_agent_step_resolves_explicit_subset_to_registered_wrapped_tools(monkeypatch):
    registered_mine = SimpleNamespace(name="MineBlock", wrapped=True)
    registered_scan = SimpleNamespace(name="scanNearbyEntities", wrapped=True)
    raw_mine = SimpleNamespace(name="MineBlock", wrapped=False)

    selected = _capture_step_tools(
        monkeypatch, [registered_mine, registered_scan], [raw_mine])

    assert selected == [registered_mine]
    assert selected[0] is registered_mine and selected[0] is not raw_mine


def test_minecraft_agent_step_rejects_unregistered_raw_tool(monkeypatch):
    registered = SimpleNamespace(name="MineBlock", wrapped=True)
    raw_unknown = SimpleNamespace(name="unregisteredTool", wrapped=False)

    assert _capture_step_tools(monkeypatch, [registered], [raw_unknown]) == []


def test_minecraft_agent_step_empty_subset_preserves_registered_tools(monkeypatch):
    registered = [SimpleNamespace(name="MineBlock"), SimpleNamespace(name="scanNearbyEntities")]

    selected = _capture_step_tools(monkeypatch, registered, [])

    assert selected == registered
    assert all(selected_tool is registered_tool
               for selected_tool, registered_tool in zip(selected, registered))


def test_minecraft_agent_step_recommendations_cannot_widen_explicit_subset(monkeypatch):
    registered_mine = SimpleNamespace(name="MineBlock")
    registered_scan = SimpleNamespace(name="scanNearbyEntities")

    selected = _capture_step_tools(
        monkeypatch, [registered_mine, registered_scan],
        [SimpleNamespace(name="MineBlock")],
        recommended_actions=("MineBlock", "scanNearbyEntities"),
    )

    assert selected == [registered_mine]


def test_minecraft_agent_step_invalid_recommendations_fail_closed(monkeypatch):
    registered = SimpleNamespace(name="MineBlock")

    selected = _capture_step_tools(
        monkeypatch, [registered], [], recommended_actions=("unregisteredTool",))

    assert selected == []


@pytest.mark.parametrize("timeout_error", [requests.ConnectTimeout, requests.ReadTimeout])
def test_minecraft_request_converts_transport_timeout(tmp_path, monkeypatch, timeout_error):
    paths = RuntimePaths.isolated(tmp_path / "attempt")
    atomic_write_json(paths.url_prefix, {"Alice": "http://localhost:5000"})
    monkeypatch.setattr(MinecraftAgent, "runtime_paths_by_name", {"Alice": paths})
    monkeypatch.setattr(
        "env.minecraft_client.requests.request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(timeout_error("blocked")),
    )
    monkeypatch.setattr(MinecraftAgent, "last_tool_timeout", None)

    with pytest.raises(MinecraftToolTimeoutError, match="post_action"):
        _minecraft_request("POST", "http://localhost:5000/post_action")

    assert MinecraftAgent.last_tool_timeout == {
        "agent": "Alice",
        "tool": "post_action",
        "outcome_certainty": "unknown",
        "retry_safe": False,
    }


def test_minecraft_request_passes_connect_and_read_timeout(monkeypatch):
    calls = []
    response = object()
    monkeypatch.setattr(
        "env.minecraft_client.requests.request",
        lambda *args, **kwargs: calls.append((args, kwargs)) or response,
    )

    assert _minecraft_request("GET", "http://localhost:5000/post_ping") is response
    assert calls[0][1]["timeout"] == (5.0, 30.0)


def test_minecraft_request_preserves_explicit_bridge_effect_unknown(tmp_path, monkeypatch):
    paths = RuntimePaths.isolated(tmp_path / "attempt")
    atomic_write_json(paths.url_prefix, {"Alice": "http://localhost:5000"})
    monkeypatch.setattr(MinecraftAgent, "runtime_paths_by_name", {"Alice": paths})
    app = FastAPI()
    app.add_exception_handler(MovementEffectUnknownError, movement_effect_unknown_response)

    @app.post("/post_move_to_pos")
    async def cleanup_unknown():
        raise MovementEffectUnknownError(
            "movement cleanup did not complete", reason="cleanup_timeout",
            status_code=503, terminal=False,
        )

    response = TestClient(app).post("/post_move_to_pos")
    assert response.status_code == 503
    assert response.headers[OUTCOME_CERTAINTY_HEADER] == "unknown"
    assert response.headers[RETRY_SAFE_HEADER] == "false"
    assert response.headers[MOVEMENT_TERMINAL_HEADER] == "false"
    assert response.headers[MOVEMENT_FAILURE_REASON_HEADER] == "cleanup_timeout"
    monkeypatch.setattr(
        "env.minecraft_client.requests.request", lambda *_args, **_kwargs: response,
    )

    with pytest.raises(MinecraftToolEffectUnknownError) as captured:
        _minecraft_request("POST", "http://localhost:5000/post_move_to_pos")

    assert captured.value.failure_detail == {
        "reason": "minecraft_tool_effect_unknown",
        "outcome_certainty": "unknown",
        "retry_safe": False,
        "message": "Minecraft tool outcome is unknown: post_move_to_pos",
        "agent": "Alice",
        "tool": "post_move_to_pos",
        "request_id": captured.value.failure_detail["request_id"],
        "timeout_type": "bridge_effect_unknown",
        "status_code": 503,
        "bridge_reason": "cleanup_timeout",
        "coordinator_terminal": False,
    }
    MinecraftAgent._caller_diagnostic_recorder("Alice").flush()
    snapshot = read_json_artifact(paths.minecraft_bridge_caller_diagnostics).value
    events = snapshot["events"] + snapshot["critical_events"]
    matching = [event for event in events
                if event.get("correlation_id") == captured.value.failure_detail["request_id"]]
    assert any(event["event_type"] == "caller_request_failed" for event in matching)
    assert not any(event["event_type"] == "caller_request_completed" for event in matching)
    assert all(event.get("outcome_certainty", "unknown") == "unknown" for event in matching)


def test_minecraft_client_has_no_direct_unbounded_http_calls():
    source = (Path(__file__).resolve().parents[1] / "env" / "minecraft_client.py").read_text(
        encoding="utf-8"
    )

    assert "requests.get(" not in source
    assert "requests.post(" not in source


@pytest.mark.parametrize("method_name", ["run", "step"])
@pytest.mark.parametrize("error_type", [
    MinecraftToolTimeoutError, MinecraftToolEffectUnknownError,
])
def test_minecraft_agent_does_not_retry_unknown_tool_outcome(monkeypatch, method_name,
                                                             error_type):
    attempts = []
    agent = object.__new__(MinecraftAgent)
    agent.name = "Alice"
    agent.model = "test"
    agent.api_key_list = ["test-key"]
    agent.llm = object()
    agent.tools = []
    agent.all_tools = []
    monkeypatch.setattr(MinecraftAgent, "provider", "ollama")
    monkeypatch.setattr(MinecraftAgent, "api_key_list", ["test-key"])
    monkeypatch.setattr(
        "env.minecraft_client.OllamaReasoningChatOpenAI",
        lambda **_kwargs: object(),
    )

    class TimedOutExecutor:
        handle_parsing_errors = False

        def __call__(self, _input):
            attempts.append(True)
            raise error_type("bridge outcome unknown")

    monkeypatch.setattr(
        "env.minecraft_client.initialize_agent",
        lambda **_kwargs: TimedOutExecutor(),
    )

    with pytest.raises(error_type, match="bridge outcome unknown"):
        getattr(agent, method_name)("move", max_try_turn=10)
    assert attempts == [True]


def test_environment_escalates_persistently_invalid_status(tmp_path):
    paths = RuntimePaths.isolated(tmp_path / "attempt")
    paths.ensure_directories()
    paths.load_status.write_text("{", encoding="utf-8")
    environment = object.__new__(VillagerBench)
    environment.env_type = env_type.meta
    environment.runtime_paths = paths
    environment._invalid_status_reads = 0

    assert environment.is_task_complete() is False
    assert environment.is_task_complete() is False
    try:
        environment.is_task_complete()
    except RuntimeError as exc:
        assert "load status remained invalid" in str(exc)
    else:
        raise AssertionError("persistently invalid status must become a diagnostic error")


def test_base_agent_reflection_does_not_read_global_meta_setting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    output_dir = tmp_path / "attempt" / "result" / "run-a"
    agent = object.__new__(BaseAgent)
    agent.name = "Alice"
    agent.reflect_info = {"prompt": [], "response": []}
    agent.reflection_output_dir = output_dir

    agent.update_reflect("system", "user", "response")

    payload = json.loads((output_dir / "Alice_reflect.json").read_text(encoding="utf-8"))
    assert payload["response"] == ["response"]


def test_meta_judger_command_receives_absolute_runtime_root(tmp_path, monkeypatch):
    paths = RuntimePaths.isolated(tmp_path / "attempt")
    paths.ensure_directories()
    commands = []

    class FakeProcess:
        pid = 123

        @staticmethod
        def poll():
            return 1

    environment = object.__new__(VillagerBench)
    environment.running = True
    environment._virtual_debug = False
    environment.logger = SimpleNamespace(info=lambda *_: None, debug=lambda *_: None)
    environment.agent_pool = []
    environment.env_type = env_type.meta
    environment.task_id = 0
    environment.host = "127.0.0.1"
    environment.port = 25565
    environment.task_name = "meta-smoke"
    environment.runtime_paths = paths
    environment.meta_diagnostics_dir = None
    monkeypatch.setattr("env.env.time.sleep", lambda _seconds: None)

    def popen(command, **_kwargs):
        commands.append(command)
        return FakeProcess()

    monkeypatch.setattr("env.env.subprocess.Popen", popen)

    try:
        environment.reset()
    except RuntimeError:
        pass

    assert "--runtime-root" in commands[0]
    root_index = commands[0].index("--runtime-root") + 1
    assert commands[0][root_index] == str(paths.root.resolve())
