import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

import start_with_config
from env.minecraft_client import MinecraftBridgeCleanupError
from env.runtime_execution import RuntimeExecution
from env.runtime_paths import RuntimePaths
from pipeline.controller_tiny import ControllerShutdownError


def test_runtime_result_preserves_observed_agent_iteration_limit():
    result = start_with_config._runtime_result(
        env=SimpleNamespace(agent_iteration_limit=13, bridge_cleanup_result={}),
        attempt_id="attempt-a",
        task_name="task-a",
    )

    assert result["agent_iteration_limit"] == 13
    assert result["agent_iteration_limit_source"] == "VillagerBench.step max_turn"


def test_controller_snapshot_includes_separate_execution_ledger():
    ledger = {"schema_version": "controller-late-execution-ledger/1", "groups": {}}
    controller = SimpleNamespace(
        shutdown_complete=True, controller_state="shutdown",
        shutdown_context={"frozen": True, "diagnostics": {"verdict": {}}},
        assignment={"Alice": 1}, snapshot_execution_ledger=lambda: ledger,
        get_k11_provider_ledger_snapshot=lambda: {
            "schema_version": "k11-execution-provider-ledger/1",
        },
        get_k11_late_lifecycle_snapshot=lambda: {
            "schema_version": "k11-late-lifecycle-ledger/1",
        },
        _k11_late_cleanup_identity={},
    )
    snapshot = start_with_config._controller_snapshot(controller)
    assert snapshot["context"] == {
        "frozen": True, "diagnostics": {"verdict": {}},
    }
    assert snapshot["execution_ledger"] is ledger


def test_controller_snapshot_uses_fail_closed_ledger_when_unavailable_or_broken():
    for controller in (SimpleNamespace(
        shutdown_context={"diagnostics": {"verdict": {}}},
        _k11_late_cleanup_identity={},
        get_k11_provider_ledger_snapshot=lambda: {},
    ), SimpleNamespace(
        shutdown_context={"diagnostics": {"verdict": {}}},
        _k11_late_cleanup_identity={},
        get_k11_provider_ledger_snapshot=lambda: {},
        snapshot_execution_ledger=lambda: (_ for _ in ()).throw(RuntimeError("secret")),
    )):
        ledger = start_with_config._controller_snapshot(controller)["execution_ledger"]
        assert ledger["schema_version"] == "controller-late-execution-ledger/1"
        assert ledger["groups"]["items"] == []
        assert ledger["diagnostic_collection_error"]["error_type"] in {
            "Unavailable", "RuntimeError",
        }


def test_controller_snapshot_is_unchanged_outside_k11():
    snapshot = start_with_config._controller_snapshot(SimpleNamespace())
    assert "execution_ledger" not in snapshot
    assert "provider_ledger" not in snapshot


def test_k11_late_sink_persists_callback_snapshot(tmp_path):
    controller = SimpleNamespace(
        snapshot_execution_ledger=lambda: {"captured_at_monotonic_ns": 2},
        get_k11_provider_ledger_snapshot=lambda: {"captured_at_monotonic_ns": 3},
        get_k11_late_lifecycle_snapshot=lambda: {"captured_at_monotonic_ns": 4},
    )
    identity = {"run_id": "K11-P0-01"}
    start_with_config._configure_k11_late_diagnostic_sink(
        controller, tmp_path / "runtime_result.json", identity,
    )
    controller._k11_late_diagnostic_sink()
    writers = [
        thread for thread in threading.enumerate()
        if thread.name == "k11-late-diagnostics-writer"
    ]
    for writer in writers:
        writer.join(1)
    artifact = json.loads(
        (tmp_path / "k11_late_runtime_diagnostics.json").read_text(encoding="utf-8")
    )
    assert artifact["schema_version"] == "k11-late-runtime-diagnostics/1"
    assert artifact["identity"] == identity
    assert artifact["execution_ledger"]["captured_at_monotonic_ns"] == 2
    assert artifact["provider_ledger"]["captured_at_monotonic_ns"] == 3
    assert artifact["late_lifecycle_ledger"]["captured_at_monotonic_ns"] == 4


def _install_harness(monkeypatch, run_minecraft_experiment):
    monkeypatch.setitem(
        sys.modules,
        "benchmarks.minecraft.experiment",
        SimpleNamespace(run_minecraft_experiment=run_minecraft_experiment),
    )


def test_main_defaults_to_dry_run_and_forwards_config_selection(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    calls = []

    def fake_run_minecraft_experiment(**kwargs):
        calls.append(kwargs)
        return {"mode": "dry_run", "error": None}

    _install_harness(monkeypatch, fake_run_minecraft_experiment)

    result = start_with_config.main([
        "--config", str(config_path),
        "--config-index", "2",
        "--output-root", str(tmp_path / "artifacts"),
    ])

    assert result == 0
    assert calls == [{
        "config_path": str(config_path),
        "config_index": 2,
        "output_root": str(tmp_path / "artifacts"),
        "execute": False,
        "execute_timeout_seconds": None,
    }]
    assert json.loads(capsys.readouterr().out) == {"mode": "dry_run", "error": None}


def test_main_execute_requires_and_forwards_positive_timeout(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    calls = []

    def fake_run_minecraft_experiment(**kwargs):
        calls.append(kwargs)
        return {"mode": "execute", "error": None}

    _install_harness(monkeypatch, fake_run_minecraft_experiment)

    assert start_with_config.main([
        "--config", str(config_path),
        "--execute",
        "--timeout", "12.5",
    ]) == 0
    assert calls[0]["execute"] is True
    assert calls[0]["execute_timeout_seconds"] == 12.5

    with pytest.raises(SystemExit, match="2"):
        start_with_config.main(["--config", str(config_path), "--execute"])
    with pytest.raises(SystemExit, match="2"):
        start_with_config.main([
            "--config", str(config_path),
            "--execute",
            "--timeout", "0",
        ])
    with pytest.raises(SystemExit, match="2"):
        start_with_config.main([
            "--config", str(config_path),
            "--execute",
            "--timeout", "nan",
        ])

    assert len(calls) == 1


def test_main_reports_missing_config_before_loading_harness(tmp_path, capsys):
    config_path = tmp_path / "missing.json"

    with pytest.raises(SystemExit, match="2"):
        start_with_config.main(["--config", str(config_path)])

    assert f"config file not found: {config_path}" in capsys.readouterr().err


def test_main_reports_harness_errors(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "invalid.json"
    config_path.write_text("{}", encoding="utf-8")

    def fake_run_minecraft_experiment(**_kwargs):
        raise ValueError("config missing required field(s): task_name")

    _install_harness(monkeypatch, fake_run_minecraft_experiment)

    assert start_with_config.main(["--config", str(config_path)]) == 1
    assert (
        "error: Minecraft experiment harness failed: "
        "config missing required field(s): task_name"
    ) in capsys.readouterr().err


def test_main_returns_failure_for_harness_error_summary(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    _install_harness(
        monkeypatch,
        lambda **_kwargs: {"mode": "execute", "error": "runtime failed"},
    )

    assert start_with_config.main(["--config", str(config_path)]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "runtime failed"


def test_run_preserves_required_meta_judger_settings(monkeypatch, tmp_path):
    class StopAfterMetaSetting(Exception):
        pass

    class FakeEnvironment:
        def agent_register(self, **_kwargs):
            pass

        def run(self, **_kwargs):
            raise StopAfterMetaSetting

    environment = FakeEnvironment()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(start_with_config, "VillagerBench", lambda **_kwargs: environment)
    monkeypatch.setattr(start_with_config, "load_agent_api_key_list", lambda: [])
    monkeypatch.setattr(start_with_config, "configure_ollama_agent", lambda *_args, **_kwargs: None)

    evaluation_arg = {
        "action": "move",
        "x": 1,
        "y": 2,
        "z": 3,
        "position_convention": "entity_feet",
        "initial_state": {
            "x": 14,
            "y": -59,
            "z": 5,
            "position_convention": "entity_feet",
        },
    }
    with pytest.raises(ValueError, match="position convention is required"):
        start_with_config.run(
            "model",
            "http://localhost:11434/v1",
            "meta",
            0,
            1,
            False,
            1,
            "move safely",
            "",
            "127.0.0.1",
            25565,
            "meta-smoke",
            document=evaluation_arg,
            task_scenario="move",
            world_initialization="preserve_restored_snapshot",
        )
    with pytest.raises(StopAfterMetaSetting):
        start_with_config.run(
            "model",
            "http://localhost:11434/v1",
            "meta",
            0,
            1,
            False,
            1,
            "move safely",
            "",
            "127.0.0.1",
            25565,
            "meta-smoke",
            document=evaluation_arg,
            task_scenario="move",
            world_initialization="preserve_restored_snapshot",
            position_convention="entity_feet",
        )

    setting = json.loads((tmp_path / ".cache" / "meta_setting.json").read_text(encoding="utf-8"))
    assert setting["task_scenario"] == "move"
    assert setting["evaluation_arg"] == evaluation_arg
    assert setting["world_initialization"] == "preserve_restored_snapshot"
    assert setting["position_convention"] == "entity_feet"
    assert isinstance(setting["attempt_id"], str) and setting["attempt_id"]
    assert environment.attempt_id == setting["attempt_id"]


def test_run_rejects_unknown_world_initialization(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="unsupported world initialization mode"):
        start_with_config.run(
            "model",
            "http://localhost:11434/v1",
            "meta",
            0,
            1,
            False,
            1,
            "move safely",
            "",
            "127.0.0.1",
            25565,
            "meta-smoke",
            document={"action": "move", "x": 1, "y": 2, "z": 3},
            task_scenario="move",
            world_initialization="unknown",
        )


def test_attempt_id_resolver_preserves_explicit_identity():
    assert start_with_config._resolve_attempt_id("attempt-a") == "attempt-a"


def test_attempt_id_resolver_generates_distinct_uuid_hex_values():
    first = start_with_config._resolve_attempt_id(None)
    second = start_with_config._resolve_attempt_id(None)

    assert len(first) == 32
    assert len(second) == 32
    assert int(first, 16) >= 0
    assert int(second, 16) >= 0
    assert first != second


def test_generated_attempt_identity_is_used_for_score_ownership():
    attempt_id = start_with_config._resolve_attempt_id(None)
    environment = SimpleNamespace(
        get_score=lambda: {
            "attempt_id": attempt_id,
            "task_name": "runtime-task-a",
            "status": "success",
            "score": 100,
        },
        get_action_log=lambda: {},
    )

    result = start_with_config._runtime_result(
        environment,
        attempt_id=attempt_id,
        task_name="runtime-task-a",
    )

    assert result["attempt_id"] == attempt_id
    assert result["score"]["attempt_id"] == attempt_id
    assert result["expected_score_identity"]["attempt_id"] == attempt_id


@pytest.mark.parametrize("value", ["", "   ", 0, False])
def test_attempt_id_resolver_rejects_invalid_explicit_identity(value):
    with pytest.raises(ValueError, match="non-empty string"):
        start_with_config._resolve_attempt_id(value)


def test_runtime_result_does_not_supplement_score_identity():
    environment = type("Environment", (), {"get_score": lambda self: {"score": 100}, "get_action_log": lambda self: {}})()

    result = start_with_config._runtime_result(
        environment,
        attempt_id="attempt-a",
        task_name="runtime-task-a",
    )

    assert result["score"] == {"score": 100}
    assert result["expected_score_identity"] == {
        "attempt_id": "attempt-a",
        "task_name": "runtime-task-a",
    }


def test_runtime_result_includes_bridge_cleanup_metadata():
    cleanup = {
        "processes": {
            "Alice": {
                "terminated": True,
                "killed": False,
                "alive_after_kill": False,
            }
        },
        "cleanup_complete": True,
    }
    environment = SimpleNamespace(
        get_score=lambda: {},
        get_action_log=lambda: {},
        bridge_cleanup_result=cleanup,
    )

    result = start_with_config._runtime_result(environment)

    assert result["bridge_cleanup"] == cleanup


def test_bridge_cleanup_failure_suppresses_successful_score():
    cleanup = {
        "processes": {"Alice": {"alive_after_kill": True}},
        "cleanup_complete": False,
    }
    result = {"score": {"status": "success", "score": 100}}
    error = MinecraftBridgeCleanupError(
        "cleanup failed",
        cleanup_result=cleanup,
    )

    start_with_config._apply_runtime_cleanup_failure(result, error)

    assert result["score"] == {}
    assert result["bridge_cleanup"] == cleanup
    assert result["cleanup_failure"]["error_type"] == "MinecraftBridgeCleanupError"


def test_cleanup_failure_is_structured_separately_from_primary_runtime_error():
    cleanup = {
        "processes": {"Alice": {"alive_after_kill": True}},
        "cleanup_complete": False,
    }
    cleanup_error = MinecraftBridgeCleanupError(
        "cleanup failed", cleanup_result=cleanup,
    )
    primary_error = ControllerShutdownError("controller shutdown incomplete")
    result = {
        "score": {"status": "success", "score": 100},
        "runtime_failure_chain": {
            "primary_failure": {"error_type": "ControllerShutdownError"},
            "cleanup_failure": {
                "error_type": "MinecraftBridgeCleanupError",
                "cleanup_result": cleanup,
            },
        },
    }

    start_with_config._apply_runtime_cleanup_failure(result, primary_error)

    assert result["score"] == {}
    assert result["primary_failure"] == {
        "error_type": "ControllerShutdownError",
    }
    assert result["cleanup_failure"] == {
        "error_type": "MinecraftBridgeCleanupError",
        "cleanup_result": cleanup,
    }


def test_cleanup_failure_can_be_read_from_explicit_runtime_failure_chain():
    cleanup = {"processes": {}, "cleanup_complete": False}
    result = {
        "score": {"status": "success"},
        "runtime_failure_chain": {
            "primary_failure": {"error_type": "ControllerShutdownError"},
            "cleanup_failure": {
                "error_type": "MinecraftBridgeCleanupError",
                "cleanup_result": cleanup,
            },
        },
    }

    start_with_config._apply_runtime_cleanup_failure(
        result, ControllerShutdownError("shutdown incomplete"),
    )

    assert result["primary_failure"]["error_type"] == "ControllerShutdownError"
    assert result["cleanup_failure"]["cleanup_result"] == cleanup
    assert result["score"] == {}


def test_generic_cleanup_failure_is_preserved_without_bridge_result():
    result = {
        "score": {"status": "success"},
        "runtime_failure_chain": {
            "primary_failure": {"error_type": "RuntimeError"},
            "cleanup_failure": {"error_type": "OSError"},
        },
    }

    start_with_config._apply_runtime_cleanup_failure(
        result, RuntimeError("primary failure"),
    )

    assert result["score"] == {}
    assert result["primary_failure"] == {"error_type": "RuntimeError"}
    assert result["cleanup_failure"] == {"error_type": "OSError"}


def test_judged_runtime_validator_accepts_consistent_success():
    result = _judged_runtime_result()

    start_with_config.validate_judged_runtime_result(result)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda result: result["runtime_task_dag_snapshot"]["nodes"][0]["lifecycle"].update(active_agents=["Alice"]), "actively assigned"),
        (lambda result: result["controller"].update(shutdown_complete=False), "shutdown is incomplete"),
        (lambda result: result["score"].update(attempt_id="stale"), "score attempt mismatch"),
    ],
)
def test_judged_runtime_validator_rejects_cross_field_mismatch(mutation, message):
    result = _judged_runtime_result()
    mutation(result)

    with pytest.raises(start_with_config.JudgedRuntimeValidationError, match=message):
        start_with_config.validate_judged_runtime_result(result)


@pytest.mark.parametrize(
    ("score", "message"),
    [
        ({"task_name": "runtime-task-a", "status": "success"}, "missing: attempt_id"),
        ({"attempt_id": "attempt-a", "status": "success"}, "missing: task_name"),
        ({"attempt_id": "", "task_name": "runtime-task-a", "status": "success"}, "missing: attempt_id"),
        ({"attempt_id": "attempt-a", "task_name": "runtime-task-a", "status": "running"}, "status must be"),
    ],
)
def test_judged_runtime_validator_rejects_unverified_score_identity(score, message):
    result = _judged_runtime_result()
    result["score"] = score

    with pytest.raises(start_with_config.JudgedRuntimeValidationError, match=message):
        start_with_config.validate_judged_runtime_result(result)


def test_runtime_result_collects_secondary_errors_without_overwriting_primary():
    class BrokenStore:
        @staticmethod
        def snapshot():
            raise RuntimeError("snapshot unavailable")

    class BrokenEnvironment:
        @staticmethod
        def get_score():
            raise ValueError("score unavailable")

        @staticmethod
        def get_action_log():
            raise OSError("action log unavailable")

    manager = SimpleNamespace(
        runtime_task_store=BrokenStore(),
        graph=SimpleNamespace(vertex=[], edge=[]),
    )

    result = start_with_config._runtime_result(
        BrokenEnvironment(),
        manager,
        error="primary controller failure",
        error_type="ControllerShutdownError",
        attempt_id="attempt-a",
        task_name="runtime-task-a",
    )

    assert result["error"] == "primary controller failure"
    assert result["error_type"] == "ControllerShutdownError"
    assert result["score"] == {}
    assert result["action_log"] == {}
    assert result["runtime_task_dag_snapshot"] == {}
    assert [item["field"] for item in result["collection_errors"]] == [
        "runtime_task_dag_snapshot",
        "score",
        "action_log",
    ]


def test_failure_result_write_error_is_non_throwing(monkeypatch):
    monkeypatch.setattr(
        start_with_config,
        "_write_runtime_result",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    error = start_with_config._write_failure_runtime_result(
        "runtime-result.json",
        {"error": "primary failure"},
    )

    assert error == {
        "field": "runtime_result",
        "error": "disk unavailable",
        "error_type": "OSError",
    }


def test_judged_runtime_validator_rejects_collection_errors():
    result = _judged_runtime_result()
    result["collection_errors"] = [{
        "field": "action_log",
        "error": "unavailable",
        "error_type": "RuntimeError",
    }]

    with pytest.raises(start_with_config.JudgedRuntimeValidationError, match="collection errors"):
        start_with_config.validate_judged_runtime_result(result)


def test_judged_runtime_validator_requires_action_evidence_by_default():
    result = _judged_runtime_result()
    result["action_log"] = {"Alice": []}

    with pytest.raises(start_with_config.JudgedRuntimeValidationError, match="no action evidence"):
        start_with_config.validate_judged_runtime_result(result)


def test_judged_runtime_validator_allows_policy_optional_empty_action_log():
    result = _judged_runtime_result()
    result["action_log"] = {}

    start_with_config.validate_judged_runtime_result(
        result,
        require_action_evidence=False,
    )


@pytest.mark.parametrize("action_log", [None, [], {"Alice": {}}])
def test_judged_runtime_validator_rejects_invalid_action_log(action_log):
    result = _judged_runtime_result()
    result["action_log"] = action_log

    with pytest.raises(start_with_config.JudgedRuntimeValidationError, match="action log"):
        start_with_config.validate_judged_runtime_result(result)


def test_eac_modes_pass_real_frozen_admission_and_install_before_environment_run(
        monkeypatch, tmp_path, request):
    class StopBeforeEnvironmentRun(Exception):
        pass

    root = Path(__file__).resolve().parents[1]
    fixture_paths = [
        root / "docs/eac/minecraft_eac_nonjudged_fixture_v1.json",
        root / "docs/eac/minecraft_eac_nonjudged_advisory_fixture_v1.json",
    ]
    fixtures = [json.loads(path.read_text(encoding="utf-8")) for path in fixture_paths]
    revision = fixtures[0]["eac_execution_revision"]
    assert all(fixture["eac_execution_revision"] == revision for fixture in fixtures)
    checkout = tmp_path / "frozen-execution"
    subprocess.run(
        ["git", "worktree", "add", "--quiet", "--detach", str(checkout), revision],
        cwd=root,
        check=True, capture_output=True,
    )
    request.addfinalizer(lambda: subprocess.run(
        ["git", "worktree", "remove", "--force", str(checkout)], cwd=root,
        check=False, capture_output=True,
    ))
    execution = RuntimeExecution.resolve(checkout)

    installed = []
    registered = []
    original_configure = start_with_config.VillagerBench.configure_eac_runtime

    def capture_configure(environment, runtime):
        original_configure(environment, runtime)
        installed.append(runtime)

    monkeypatch.setattr(
        start_with_config.VillagerBench, "configure_eac_runtime", capture_configure)
    monkeypatch.setattr(
        start_with_config.VillagerBench, "agent_register",
        lambda self, **kwargs: registered.append((self, kwargs)),
    )
    monkeypatch.setattr(
        start_with_config.VillagerBench, "run",
        lambda self, **unused: (_ for _ in ()).throw(StopBeforeEnvironmentRun()),
    )
    monkeypatch.setattr(start_with_config, "load_agent_api_key_list", lambda: [])
    monkeypatch.setattr(start_with_config, "configure_ollama_agent", lambda *args, **kwargs: None)

    for fixture in fixtures:
        mode = fixture["task_selection_policy"]
        with pytest.raises(StopBeforeEnvironmentRun):
            start_with_config.run(
                fixture["api_model"], fixture["api_base"], fixture["task_type"],
                fixture["task_idx"], fixture["agent_num"], False, 1,
                fixture["task_goal"], None, fixture["host"], fixture["port"],
                fixture["task_name"], document={},
                minecraft_dual_dag_config={
                    "eac_mode": mode,
                    "eac_premanifest": str(root / fixture["eac_premanifest"]),
                    "eac_execution_revision": revision,
                    "judged_execution": fixture["judged_execution"],
                    "production": fixture["production"],
                },
                task_scenario=fixture["task_scenario"],
                runtime_paths=RuntimePaths.isolated(tmp_path / mode),
                attempt_id="admission-" + mode,
                runtime_execution=execution,
            )

    assert len(installed) == 2 and len(registered) == 2
    authority, advisory = installed
    assert [runtime.mode for runtime in installed] == [
        "dual_dag_authority", "dual_dag_advisory"]
    assert [runtime.authority.mode for runtime in installed] == ["authority", "advisory"]
    assert authority.identity_binding == advisory.identity_binding
    assert authority.policy_binding.digest_sha256 == advisory.policy_binding.digest_sha256
    assert authority.profile_binding.digest_sha256 == advisory.profile_binding.digest_sha256
    assert authority.classification_identity == advisory.classification_identity
    assert authority.identity_binding["execution_revision"] == revision


def _judged_runtime_result():
    return {
        "attempt_id": "attempt-a",
        "task_name": "runtime-task-a",
        "score": {
            "attempt_id": "attempt-a",
            "task_name": "runtime-task-a",
            "status": "success",
            "score": 100,
        },
        "runtime_task_dag_snapshot": {
            "summary": {"terminal_state": "success"},
            "nodes": [{
                "lifecycle": {"status": "success", "active_agents": []},
            }],
        },
        "controller": {"shutdown_complete": True, "state": "shutdown"},
        "action_log": {"Alice": [{"action": "navigateTo"}]},
        "collection_errors": [],
        "error": None,
    }
