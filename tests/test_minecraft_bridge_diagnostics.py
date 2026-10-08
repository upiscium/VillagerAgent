import json
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from env.minecraft_bridge_diagnostics import (
    BoundedDiagnosticRecorder,
    CORRELATION_HEADER,
    MAX_ARTIFACT_BYTES,
    SCHEMA_VERSION,
    artifact_projection,
    classify_request_exception,
    install_fastapi_request_diagnostics,
    read_diagnostic_snapshot,
)
from env.minecraft_client import (
    Agent,
    MinecraftBridgeCleanupError,
    MinecraftToolTimeoutError,
    _minecraft_request,
)
from env.runtime_paths import RuntimePaths, atomic_write_json
from start_with_config import _runtime_checkpoint_result, _runtime_result


def _flush_agent(actor="Alice"):
    recorder = Agent._caller_diagnostic_recorder(actor)
    assert recorder is not None and recorder.flush()


def _record_routine_requests(recorder, count=150, *, actor="Alice", side="bridge"):
    for index in range(count):
        correlation_id = f"{index + 1000:032x}"
        started = 10000 + index * 2
        if side == "caller":
            recorder.record(
                "caller_request_started", actor=actor, route="/post_ping",
                correlation_id=correlation_id, started_monotonic_ns=started,
            )
            recorder.record(
                "caller_request_completed", actor=actor, route="/post_ping",
                correlation_id=correlation_id, started_monotonic_ns=started,
                completed_monotonic_ns=started + 1, elapsed_ns=1, status_code=200,
            )
        else:
            recorder.record(
                "request_received", actor=actor, route="/post_ping",
                correlation_id=correlation_id, started_monotonic_ns=started,
            )
            recorder.record(
                "request_completed", actor=actor, route="/post_ping",
                correlation_id=correlation_id, started_monotonic_ns=started,
                completed_monotonic_ns=started + 1, elapsed_ns=1, status_code=200,
            )


@pytest.fixture
def diagnostic_agent(tmp_path, monkeypatch):
    paths = RuntimePaths.isolated(tmp_path / "attempt")
    paths.ensure_directories()
    atomic_write_json(paths.url_prefix, {
        "Alice": "http://localhost:5000",
        "Bob": "http://localhost:5001",
    })
    monkeypatch.setattr(Agent, "runtime_paths_by_name", {"Alice": paths, "Bob": paths})
    monkeypatch.setattr(Agent, "name2port", {"Alice": 5000, "Bob": 5001})
    monkeypatch.setattr(Agent, "_bridge_diagnostic_recorders", {})
    monkeypatch.setattr(Agent, "last_tool_timeout", None)
    monkeypatch.setattr(Agent, "last_bridge_diagnostics", None)
    yield paths
    Agent._close_bridge_diagnostic_recorders()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (requests.ConnectTimeout("blocked"), "connect_timeout"),
        (requests.ReadTimeout("blocked"), "read_timeout"),
        (requests.ConnectionError(ConnectionRefusedError(111, "refused")), "connection_refused"),
        (requests.ConnectionError("down"), "connection_error"),
        (requests.RequestException("bad"), "other_request_error"),
    ],
)
def test_request_failure_types_are_classified(error, expected):
    assert classify_request_exception(error) == expected


@pytest.mark.parametrize(
    ("error_type", "expected"),
    [(requests.ConnectTimeout, "connect_timeout"), (requests.ReadTimeout, "read_timeout")],
)
def test_timeout_records_classification_and_monotonic_elapsed_time(
    diagnostic_agent, monkeypatch, error_type, expected,
):
    ticks = iter((100, 350))
    monkeypatch.setattr("env.minecraft_client._request_monotonic_ns", lambda: next(ticks))
    monkeypatch.setattr(
        "env.minecraft_client.requests.request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error_type("blocked")),
    )

    with pytest.raises(MinecraftToolTimeoutError) as raised:
        _minecraft_request("POST", "http://localhost:5000/post_move_to_pos")

    assert raised.value.failure_detail["timeout_type"] == expected
    assert raised.value.failure_detail["outcome_certainty"] == "unknown"
    assert raised.value.failure_detail["retry_safe"] is False
    _flush_agent()
    snapshot, error = read_diagnostic_snapshot(
        diagnostic_agent.minecraft_bridge_caller_diagnostics
    )
    assert error is None
    terminal = snapshot["events"][-1]
    assert terminal["event_type"] == "caller_request_timed_out"
    assert terminal["timeout_type"] == expected
    assert terminal["started_monotonic_ns"] == 100
    assert terminal["completed_monotonic_ns"] == 350
    assert terminal["elapsed_ns"] == 250
    assert terminal["configured_connect_timeout_s"] == 5.0
    assert terminal["configured_read_timeout_s"] == 30.0


def test_ping_lifecycle_is_actor_scoped_and_timestamped(diagnostic_agent, monkeypatch):
    ticks = iter((1000, 1600))
    monkeypatch.setattr("env.minecraft_client._request_monotonic_ns", lambda: next(ticks))
    monkeypatch.setattr(
        "env.minecraft_client.requests.request",
        lambda *_args, **_kwargs: SimpleNamespace(status_code=200, json=lambda: {
            "message": "pong", "status": True,
        }),
    )

    assert Agent.ping("Alice")["status"] is True
    _flush_agent()

    snapshot, _ = read_diagnostic_snapshot(diagnostic_agent.minecraft_bridge_caller_diagnostics)
    ping = [event for event in snapshot["events"] if event["event_type"].startswith("ping_")]
    assert [event["event_type"] for event in ping] == ["ping_started", "ping_succeeded"]
    assert all(event["actor"] == "Alice" for event in ping)
    assert ping[0]["correlation_id"] == ping[1]["correlation_id"]
    assert ping[1]["elapsed_ns"] == 600


def test_ping_connection_failure_keeps_single_correlated_terminal_event(
    diagnostic_agent, monkeypatch,
):
    monkeypatch.setattr(
        "env.minecraft_client.requests.request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.ConnectionError("down")),
    )

    assert Agent.ping("Alice")["status"] is False
    _flush_agent()

    snapshot, _ = read_diagnostic_snapshot(diagnostic_agent.minecraft_bridge_caller_diagnostics)
    failures = [event for event in snapshot["events"] if event["event_type"] == "ping_failed"]
    assert len(failures) == 1
    assert failures[0]["correlation_id"]
    assert failures[0]["timeout_type"] == "connection_error"


def test_ping_timeout_preserves_return_contract_and_single_terminal_event(
    diagnostic_agent, monkeypatch,
):
    monkeypatch.setattr(
        "env.minecraft_client.requests.request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.ReadTimeout("stalled")),
    )

    assert Agent.ping("Alice") == {'message': 'Exception', 'status': False}
    _flush_agent()

    snapshot, _ = read_diagnostic_snapshot(diagnostic_agent.minecraft_bridge_caller_diagnostics)
    terminal = [event for event in snapshot["events"] if event["event_type"] == "ping_timed_out"]
    assert len(terminal) == 1
    assert terminal[0]["correlation_id"]
    assert terminal[0]["timeout_type"] == "read_timeout"


def test_ping_invalid_error_response_preserves_transport_correlation(
    diagnostic_agent, monkeypatch,
):
    response = requests.Response()
    response.status_code = 500
    response._content = b"not-json"
    monkeypatch.setattr("env.minecraft_client.requests.request", lambda *_args, **_kwargs: response)

    assert Agent.ping("Alice")["status"] is False
    _flush_agent()

    snapshot, _ = read_diagnostic_snapshot(diagnostic_agent.minecraft_bridge_caller_diagnostics)
    failure = [event for event in snapshot["events"] if event["event_type"] == "ping_failed"][-1]
    assert failure["correlation_id"]
    assert failure["status_code"] == 500


def test_request_correlation_pairs_caller_and_bridge_events(
    diagnostic_agent, monkeypatch, tmp_path,
):
    bridge_recorder = BoundedDiagnosticRecorder(
        tmp_path / "bridge.json", producer="bridge", actor="Alice",
    )
    app = FastAPI()
    install_fastapi_request_diagnostics(app, bridge_recorder, actor="Alice")

    @app.get("/post_ping")
    async def ping():
        return {"status": True}

    client = TestClient(app)

    def request(method, unused_url, **kwargs):
        return client.request(method, "/post_ping", headers=kwargs["headers"])

    monkeypatch.setattr("env.minecraft_client.requests.request", request)
    response = _minecraft_request("GET", "http://localhost:5000/post_ping")
    assert response.status_code == 200
    _flush_agent()
    assert bridge_recorder.flush()

    caller, _ = read_diagnostic_snapshot(diagnostic_agent.minecraft_bridge_caller_diagnostics)
    bridge, _ = read_diagnostic_snapshot(tmp_path / "bridge.json")
    caller_id = caller["events"][0]["correlation_id"]
    received = next(event for event in bridge["events"] if event["event_type"] == "request_received")
    completed = next(event for event in bridge["events"] if event["event_type"] == "request_completed")
    assert received["correlation_id"] == caller_id == completed["correlation_id"]
    assert received["caller_correlated"] is True


def test_stalled_received_request_is_distinguishable_from_unreachable_endpoint(
    diagnostic_agent, monkeypatch, tmp_path,
):
    bridge = BoundedDiagnosticRecorder(tmp_path / "bridge.json", producer="bridge", actor="Alice")
    fixed_id = "a" * 32
    monkeypatch.setattr("env.minecraft_client.new_correlation_id", lambda: fixed_id)

    def received_then_stalled(_method, _url, **kwargs):
        bridge.record(
            "request_received", correlation_id=kwargs["headers"][CORRELATION_HEADER],
            actor="Alice", route="/post_find", method="POST",
            endpoint_identity="actor:Alice", started_monotonic_ns=1,
            caller_correlated=True,
        )
        raise requests.ReadTimeout("stalled")

    monkeypatch.setattr("env.minecraft_client.requests.request", received_then_stalled)
    with pytest.raises(MinecraftToolTimeoutError):
        _minecraft_request("POST", "http://localhost:5000/post_find")
    assert bridge.flush()
    received_snapshot, _ = read_diagnostic_snapshot(tmp_path / "bridge.json")
    assert [event["event_type"] for event in received_snapshot["events"]] == ["request_received"]

    bridge_unreachable = BoundedDiagnosticRecorder(
        tmp_path / "unreachable.json", producer="bridge", actor="Bob",
    )
    monkeypatch.setattr(
        "env.minecraft_client.requests.request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.ConnectTimeout("down")),
    )
    with pytest.raises(MinecraftToolTimeoutError):
        _minecraft_request("GET", "http://localhost:5001/post_ping")
    assert not (tmp_path / "unreachable.json").exists()
    assert bridge_unreachable.snapshot()["events"] == []


def test_bridge_ready_and_not_ready_lifecycle_is_persisted(tmp_path):
    ready = BoundedDiagnosticRecorder(tmp_path / "ready.json", producer="bridge", actor="Alice")
    ready.record("listener_startup_completed", actor="Alice", expected_local_port=5000)
    ready.record("listener_ready", actor="Alice", expected_local_port=5000)
    not_ready = BoundedDiagnosticRecorder(tmp_path / "not-ready.json", producer="bridge", actor="Bob")
    not_ready.record("listener_starting", actor="Bob", expected_local_port=5001)
    not_ready.record("listener_failed", actor="Bob", expected_local_port=5001,
                     error_class="OSError")
    assert ready.flush() and not_ready.flush()

    ready_snapshot, _ = read_diagnostic_snapshot(tmp_path / "ready.json")
    failed_snapshot, _ = read_diagnostic_snapshot(tmp_path / "not-ready.json")
    assert ready_snapshot["events"][-1]["event_type"] == "listener_ready"
    assert failed_snapshot["events"][-1]["event_type"] == "listener_failed"


def test_failure_artifacts_survive_bridge_shutdown(diagnostic_agent, monkeypatch):
    class Process:
        pid = 12345
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout):
            return self.returncode

    monkeypatch.setattr(Agent, "agent_process", {"Alice": Process()})
    Agent.record_bridge_diagnostic(
        "Alice", "caller_request_failed", actor="Alice", correlation_id="b" * 32,
        route="/post_find", error_class="ConnectionError",
    )

    cleanup = Agent.kill()

    assert cleanup["cleanup_complete"] is True
    summary = Agent.last_bridge_diagnostics
    assert summary["artifacts"]["caller"]["state"] == "valid"
    assert summary["actors"]["Alice"]["process_lifecycle"][-1]["event_type"] == "bridge_process_exited"


def test_process_still_alive_is_not_recorded_as_exited(diagnostic_agent, monkeypatch):
    class Process:
        pid = 12345

        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("bridge", timeout)

    monkeypatch.setattr(Agent, "agent_process", {"Alice": Process()})

    with pytest.raises(MinecraftBridgeCleanupError):
        Agent.kill(terminate_grace_seconds=0, kill_grace_seconds=0)

    lifecycle = Agent.last_bridge_diagnostics["actors"]["Alice"]["process_lifecycle"]
    assert lifecycle[-1]["event_type"] == "bridge_process_still_alive"


def test_runtime_summary_merges_retained_caller_and_bridge_correlation(diagnostic_agent):
    correlation_id = "d" * 32
    caller = Agent._caller_diagnostic_recorder("Alice")
    bridge = BoundedDiagnosticRecorder(
        diagnostic_agent.minecraft_bridge_actor_diagnostics("Alice"),
        producer="bridge", actor="Alice",
    )
    bridge.record("listener_starting", actor="Alice")
    bridge.record("listener_ready", actor="Alice")
    bridge.record("mineflayer_connected", actor="Alice", connection_state="connected")
    bridge.record("mineflayer_ready", actor="Alice", connection_state="ready")
    bridge.record("mineflayer_disconnected", actor="Alice", connection_state="disconnected")
    bridge.record(
        "mineflayer_connection_error", actor="Alice", connection_state="error",
        error_class="ConnectionError",
    )
    bridge.record("mineflayer_connected", actor="Alice", connection_state="connected")
    bridge.record("mineflayer_ready", actor="Alice", connection_state="ready")
    bridge.record(
        "request_received", correlation_id=correlation_id, actor="Alice",
        route="/post_move_to_pos", started_monotonic_ns=2,
    )
    caller.record(
        "caller_request_started", correlation_id=correlation_id, actor="Alice",
        route="/post_move_to_pos", started_monotonic_ns=1,
    )
    caller.record(
        "caller_request_timed_out", correlation_id=correlation_id, actor="Alice",
        route="/post_move_to_pos", started_monotonic_ns=1, completed_monotonic_ns=31,
        elapsed_ns=30, timeout_type="read_timeout", outcome_certainty="unknown",
        retry_safe=False,
    )
    _record_routine_requests(caller, side="caller")
    _record_routine_requests(bridge)
    assert bridge.flush()

    actor_summary = Agent.bridge_diagnostics_summary()["actors"]["Alice"]

    correlation = next(item for item in actor_summary["correlation_summaries"]
                       if item["correlation_id"] == correlation_id)
    assert correlation["caller"]["caller_timed_out"] is True
    assert correlation["bridge"]["bridge_received"] is True
    assert actor_summary["unresolved_bridge_requests"][0]["correlation_id"] == correlation_id
    assert actor_summary["lifecycle_milestones"]["listener"]["first_starting"]
    assert actor_summary["lifecycle_milestones"]["listener"]["first_ready"]
    assert actor_summary["lifecycle_milestones"]["mineflayer"]["first_connected"]
    assert actor_summary["lifecycle_milestones"]["mineflayer"]["first_ready"]
    assert actor_summary["lifecycle_milestones"]["mineflayer"]["last_disconnected"]
    assert actor_summary["lifecycle_milestones"]["mineflayer"]["last_connection_error"]
    assert actor_summary["lifecycle_milestones"]["mineflayer"]["last_connected"]
    assert actor_summary["lifecycle_milestones"]["mineflayer"]["last_ready"]
    milestones = actor_summary["lifecycle_milestones"]["mineflayer"]
    assert milestones["first_ready"]["timestamp_monotonic_ns"] < (
        milestones["last_disconnected"]["timestamp_monotonic_ns"]
    )
    assert milestones["last_connection_error"]["timestamp_monotonic_ns"] < (
        milestones["last_connected"]["timestamp_monotonic_ns"]
    ) < milestones["last_ready"]["timestamp_monotonic_ns"]
    timeout = next(event for event in actor_summary["critical_events"]["caller"]
                   if event["correlation_id"] == correlation_id)
    assert milestones["last_ready"]["timestamp_monotonic_ns"] < timeout["timestamp_monotonic_ns"]
    assert {event["event_type"] for event in actor_summary["mineflayer_lifecycle"]} >= {
        "mineflayer_connected", "mineflayer_ready", "mineflayer_disconnected",
        "mineflayer_connection_error",
    }
    assert bridge.close()


def test_untrusted_artifact_fields_are_removed_from_projection(tmp_path):
    path = tmp_path / "bridge.json"
    secret = "never-retain-this-secret"
    query_secret = "never-retain-query-secret"
    path.write_text(json.dumps({
        "schema_version": "minecraft-bridge-diagnostics/1",
        "producer": "bridge",
        "actor": "Alice",
        "events": [{
            "event_type": "request_received",
            "route": f"https://user:{secret}@example.invalid/post_find?token={query_secret}#private",
            "correlation_id": f"invalid-{secret}",
            "payload": secret,
            "request_body": secret,
        }],
    }), encoding="utf-8")

    snapshot, error = read_diagnostic_snapshot(path)
    projection = artifact_projection(path, runtime_root=tmp_path)

    assert error is None
    assert projection["state"] == "valid"
    assert snapshot["events"] == projection["snapshot"]["events"] == [{
        "event_type": "request_received",
        "route": "/post_find",
    }]
    assert secret not in json.dumps(projection)
    assert query_secret not in json.dumps(projection)


@pytest.mark.parametrize("untrusted_route", [
    "https:/user:route-secret@example.invalid/post_find?token=query-secret",
    "https:\\user:route-secret@example.invalid\\post_find?token=query-secret",
    "https%3A%2F%2Fuser%3Aroute-secret%40example.invalid%2Fpost_find",
    "/https%3A%2F%2Fuser%3Aroute-secret%40example.invalid%2Fpost_find",
    "/https:user:route-secret@example.invalid/post_find",
    "///user:route-secret@example.invalid/post_find?token=query-secret",
])
def test_malformed_url_like_routes_are_rejected_without_secret_retention(
    tmp_path, untrusted_route,
):
    path = tmp_path / "bridge.json"
    recorder = BoundedDiagnosticRecorder(path, producer="bridge", actor="Alice")

    recorder.record(
        "request_received", actor="Alice", route=untrusted_route,
        correlation_id="e" * 32,
    )
    assert recorder.flush()
    snapshot, error = read_diagnostic_snapshot(path)
    projection = artifact_projection(path, runtime_root=tmp_path)

    assert error is None
    assert snapshot["events"][0]["route"] == "/"
    assert snapshot["correlations"][0]["route"] == "/"
    assert snapshot["unresolved_requests"][0]["route"] == "/"
    assert "route-secret" not in json.dumps(snapshot)
    assert "query-secret" not in json.dumps(snapshot)
    assert projection["state"] == "valid"
    assert "route-secret" not in json.dumps(projection)
    assert "query-secret" not in json.dumps(projection)


def test_recorder_close_stops_writer_and_rejects_new_events(tmp_path):
    recorder = BoundedDiagnosticRecorder(tmp_path / "bridge.json", producer="bridge")
    assert recorder.record("listener_starting")

    assert recorder.close()
    assert not recorder._writer.is_alive()
    assert recorder.record("listener_ready") is False


def test_diagnostics_are_bounded_and_do_not_record_payloads_or_secrets(tmp_path):
    path = tmp_path / "diagnostics.json"
    recorder = BoundedDiagnosticRecorder(path, producer="bridge", actor="Alice", max_events=2)
    for index in range(3):
        recorder.record(
            "request_received", actor="Alice",
            route="https://user:secret-value@example.invalid/post_find?token=secret-value",
            correlation_id=f"{index:032x}", payload={"api_key": "secret-value"},
            secret="secret-value", request_body="secret-value",
        )
    assert recorder.flush()
    serialized = path.read_text(encoding="utf-8")
    snapshot = json.loads(serialized)
    assert snapshot["truncated"] is True
    assert len(snapshot["events"]) == 2
    assert "secret-value" not in serialized
    assert "payload" not in serialized
    assert "request_body" not in serialized
    assert all(event["route"] == "/post_find" for event in snapshot["events"])
    assert all(summary["route"] == "/post_find" for summary in snapshot["correlations"])
    assert all(summary["route"] == "/post_find"
               for summary in snapshot["unresolved_requests"])


def test_early_caller_read_timeout_survives_more_than_256_routine_events(tmp_path):
    recorder = BoundedDiagnosticRecorder(tmp_path / "caller.json", producer="caller")
    correlation_id = "1" * 32
    recorder.record(
        "caller_request_started", correlation_id=correlation_id, actor="Alice",
        route="/post_move_to_pos", started_monotonic_ns=100,
        configured_connect_timeout_s=5, configured_read_timeout_s=30,
        outcome_certainty="unknown", retry_safe=False,
    )
    recorder.record(
        "caller_request_timed_out", correlation_id=correlation_id, actor="Alice",
        route="/post_move_to_pos", started_monotonic_ns=100,
        completed_monotonic_ns=30100, elapsed_ns=30000, timeout_type="read_timeout",
        configured_connect_timeout_s=5, configured_read_timeout_s=30,
        outcome_certainty="unknown", retry_safe=False, error_class="ReadTimeout",
    )
    _record_routine_requests(recorder, side="caller")

    snapshot = recorder.snapshot()

    assert all(event.get("correlation_id") != correlation_id for event in snapshot["events"])
    assert any(event.get("correlation_id") == correlation_id
               for event in snapshot["critical_events"])
    summary = next(item for item in snapshot["correlations"]
                   if item["correlation_id"] == correlation_id)
    assert summary == {
        "correlation_id": correlation_id,
        "actor": "Alice",
        "route": "/post_move_to_pos",
        "caller_started": True,
        "caller_start_ns": 100,
        "configured_connect_timeout_s": 5.0,
        "configured_read_timeout_s": 30.0,
        "outcome_certainty": "unknown",
        "retry_safe": False,
        "caller_timed_out": True,
        "caller_end_ns": 30100,
        "elapsed_ns": 30000,
        "timeout_type": "read_timeout",
        "error_class": "ReadTimeout",
    }


def test_early_inflight_bridge_request_survives_more_than_256_routine_events(tmp_path):
    recorder = BoundedDiagnosticRecorder(tmp_path / "bridge.json", producer="bridge")
    correlation_id = "2" * 32
    recorder.record(
        "request_received", correlation_id=correlation_id, actor="Alice",
        route="/post_move_to_pos", method="POST", started_monotonic_ns=100,
    )
    _record_routine_requests(recorder)

    snapshot = recorder.snapshot()

    assert snapshot["unresolved_requests"] == [{
        "correlation_id": correlation_id,
        "bridge_received": True,
        "bridge_start_ns": 100,
        "actor": "Alice",
        "method": "POST",
        "route": "/post_move_to_pos",
    }]


def test_early_bridge_failure_survives_more_than_256_routine_events(tmp_path):
    recorder = BoundedDiagnosticRecorder(tmp_path / "bridge.json", producer="bridge")
    correlation_id = "3" * 32
    recorder.record(
        "request_received", correlation_id=correlation_id, actor="Alice",
        route="/post_find", started_monotonic_ns=100,
    )
    recorder.record(
        "request_failed", correlation_id=correlation_id, actor="Alice",
        route="/post_find", started_monotonic_ns=100, completed_monotonic_ns=150,
        elapsed_ns=50, error_class="AttributeError",
    )
    _record_routine_requests(recorder)

    snapshot = recorder.snapshot()

    assert snapshot["unresolved_requests"] == []
    assert any(event.get("correlation_id") == correlation_id
               for event in snapshot["critical_events"])
    summary = next(item for item in snapshot["correlations"]
                   if item["correlation_id"] == correlation_id)
    assert summary["bridge_received"] is True
    assert summary["bridge_failed"] is True
    assert summary["error_class"] == "AttributeError"


def test_early_long_bridge_completion_survives_later_request_churn(tmp_path):
    recorder = BoundedDiagnosticRecorder(
        tmp_path / "bridge.json", producer="bridge", max_correlations=2,
        max_long_duration_requests=2,
    )
    correlation_id = "4" * 32
    recorder.record(
        "request_received", correlation_id=correlation_id, actor="Alice",
        route="/post_move_to_pos", started_monotonic_ns=1,
    )
    recorder.record(
        "request_completed", correlation_id=correlation_id, actor="Alice",
        route="/post_move_to_pos", started_monotonic_ns=1,
        completed_monotonic_ns=1001, elapsed_ns=1000, status_code=200,
    )
    for index in range(130):
        routine_id = f"{index + 100:032x}"
        recorder.record(
            "request_received", correlation_id=routine_id, actor="Alice",
            route="/post_ping", started_monotonic_ns=2000 + index * 2,
        )
        recorder.record(
            "request_completed", correlation_id=routine_id, actor="Alice",
            route="/post_ping", started_monotonic_ns=2000 + index * 2,
            completed_monotonic_ns=2001 + index * 2, elapsed_ns=1, status_code=200,
        )

    snapshot = recorder.snapshot()

    assert all(item["correlation_id"] != correlation_id for item in snapshot["correlations"])
    retained = next(item for item in snapshot["long_duration_requests"]
                    if item["correlation_id"] == correlation_id)
    assert retained["bridge_received"] is True
    assert retained["bridge_completed"] is True
    assert retained["elapsed_ns"] == 1000
    assert snapshot["retention"]["long_duration"]["truncated"] is True


def test_lifecycle_milestones_survive_more_than_256_routine_events(tmp_path):
    recorder = BoundedDiagnosticRecorder(tmp_path / "bridge.json", producer="bridge")
    for event_type in (
        "listener_starting", "listener_startup_completed", "listener_ready",
        "listener_request_accepted", "mineflayer_bot_created", "mineflayer_connected",
        "mineflayer_ready", "mineflayer_disconnected", "mineflayer_connection_error",
        "listener_failed", "listener_shutdown",
    ):
        recorder.record(event_type, actor="Alice", connection_state="ready")
    recorder.record("mineflayer_connected", actor="Alice", connection_state="connected")
    recorder.record("mineflayer_ready", actor="Alice", connection_state="ready")
    _record_routine_requests(recorder)

    assert recorder.flush()
    snapshot, error = read_diagnostic_snapshot(tmp_path / "bridge.json")
    assert error is None
    milestones = snapshot["lifecycle"]["actors"]["Alice"]

    assert set(milestones["listener"]) == {
        "first_starting", "startup_completed", "first_ready", "first_request_accepted",
        "last_failed", "shutdown",
    }
    assert set(milestones["mineflayer"]) == {
        "first_bot_created", "first_connected", "first_ready", "last_disconnected",
        "last_connection_error", "last_connected", "last_ready",
    }
    mineflayer = milestones["mineflayer"]
    assert mineflayer["first_ready"]["timestamp_monotonic_ns"] < (
        mineflayer["last_disconnected"]["timestamp_monotonic_ns"]
    )
    assert mineflayer["last_connection_error"]["timestamp_monotonic_ns"] < (
        mineflayer["last_connected"]["timestamp_monotonic_ns"]
    ) < mineflayer["last_ready"]["timestamp_monotonic_ns"]
    assert {event["event_type"] for event in snapshot["critical_events"]} >= {
        "mineflayer_disconnected", "mineflayer_connection_error", "listener_failed",
    }


def test_movement_deadline_diagnostics_are_retained_and_metadata_only(tmp_path):
    recorder = BoundedDiagnosticRecorder(tmp_path / "bridge.json", producer="bridge")
    correlation_id = "f" * 32
    recorder.record(
        "movement_started", correlation_id=correlation_id, actor="Alice",
        movement_id="e" * 32, operation="move_to_pos", target_identity="block:1:2:3",
        started_monotonic_ns=10, configured_movement_deadline_s=9.0,
        movement_started=True, request_body="secret-value",
    )
    recorder.record(
        "movement_terminal", correlation_id=correlation_id, actor="Alice",
        movement_id="e" * 32, operation="move_to_pos", target_identity="block:1:2:3",
        completed_monotonic_ns=20, terminal_reason="deadline", movement_terminal=True,
        movement_deadline=True, configured_movement_deadline_s=9.0,
        initial_distance=12.0, final_distance=12.0, movement_elapsed_s=9.0,
        goal_clear_attempted=True, goal_clear_succeeded=True,
        raw_exception="secret-value",
    )
    _record_routine_requests(recorder)

    snapshot = recorder.snapshot()
    terminal = next(event for event in snapshot["critical_events"]
                    if event.get("correlation_id") == correlation_id)
    summary = next(item for item in snapshot["correlations"]
                   if item["correlation_id"] == correlation_id)

    assert terminal["terminal_reason"] == "deadline"
    assert terminal["goal_clear_succeeded"] is True
    assert summary["movement_started"] is True
    assert summary["movement_deadline"] is True
    assert summary["configured_movement_deadline_s"] == 9.0
    assert "secret-value" not in json.dumps(snapshot)


def test_cleanup_timeout_is_retained_as_nonterminal(tmp_path):
    recorder = BoundedDiagnosticRecorder(tmp_path / "bridge.json", producer="bridge")
    correlation_id = "d" * 32
    recorder.record(
        "movement_nonterminal", correlation_id=correlation_id, actor="Alice",
        movement_id="c" * 32, operation="move_to_pos",
        target_identity="block:1:2:3", completed_monotonic_ns=20,
        terminal_reason="cleanup_timeout", movement_terminal=False,
        movement_nonterminal=True, movement_failed=True,
        cleanup_completed=False, goal_clear_attempted=True,
        goal_clear_succeeded=False,
    )

    snapshot = recorder.snapshot()
    event = next(item for item in snapshot["critical_events"]
                 if item.get("correlation_id") == correlation_id)
    summary = next(item for item in snapshot["correlations"]
                   if item["correlation_id"] == correlation_id)
    assert event["movement_nonterminal"] is True
    assert event["cleanup_completed"] is False
    assert summary["movement_nonterminal"] is True
    assert summary.get("movement_terminal") is not True


def test_retention_lane_overflow_is_independent_and_explicit(tmp_path):
    recorder = BoundedDiagnosticRecorder(
        tmp_path / "bounded.json", producer="caller", max_events=1,
        max_critical_events=1, max_correlations=1, max_unresolved_requests=1,
        max_long_duration_requests=1, max_lifecycle_actors=1,
    )
    for index in range(2):
        correlation_id = f"{index + 1:032x}"
        recorder.record(
            "request_received", correlation_id=correlation_id, actor=f"Actor{index}",
            started_monotonic_ns=index + 1,
        )
        recorder.record(
            "request_completed", correlation_id=correlation_id, actor=f"Actor{index}",
            started_monotonic_ns=index + 1, completed_monotonic_ns=index + 3,
            elapsed_ns=index + 2, status_code=500,
        )
        recorder.record("listener_failed", actor=f"Actor{index}", error_class="OSError")
    recorder.record(
        "request_received", correlation_id="9" * 32, actor="Alice",
        started_monotonic_ns=10,
    )
    recorder.record(
        "request_received", correlation_id="8" * 32, actor="Bob",
        started_monotonic_ns=11,
    )

    snapshot = recorder.snapshot()

    for lane in ("recent", "critical", "correlations", "unresolved", "long_duration", "lifecycle"):
        assert snapshot["retention"][lane]["truncated"] is True
        assert snapshot["retention"][lane]["dropped_count"] > 0
        assert snapshot["retention"][lane]["retained"] <= snapshot["retention"][lane]["capacity"]
    assert recorder.flush()
    sanitized, error = read_diagnostic_snapshot(tmp_path / "bounded.json")
    assert error is None
    assert all(sanitized["retention"][lane]["capacity"] == 1 for lane in snapshot["retention"])


def test_long_completion_is_reconstructed_after_correlation_eviction(tmp_path):
    recorder = BoundedDiagnosticRecorder(
        tmp_path / "bridge.json", producer="bridge", max_correlations=1,
    )
    long_id = "6" * 32
    recorder.record(
        "request_received", correlation_id=long_id, actor="Alice",
        route="/post_move_to_pos", started_monotonic_ns=10,
    )
    recorder.record(
        "request_received", correlation_id="7" * 32, actor="Alice",
        route="/post_ping", started_monotonic_ns=20,
    )
    recorder.record(
        "request_completed", correlation_id=long_id, actor="Alice",
        route="/post_move_to_pos", started_monotonic_ns=10,
        completed_monotonic_ns=1010, elapsed_ns=1000, status_code=200,
    )

    snapshot = recorder.snapshot()

    retained = snapshot["long_duration_requests"][0]
    assert retained["correlation_id"] == long_id
    assert retained["bridge_received"] is True
    assert retained["bridge_completed"] is True
    assert retained["bridge_start_ns"] == 10
    assert retained["bridge_end_ns"] == 1010


def test_ping_start_is_reconstructed_after_correlation_eviction(tmp_path):
    recorder = BoundedDiagnosticRecorder(
        tmp_path / "caller.json", producer="caller", max_correlations=1,
    )
    ping_id = "5" * 32
    recorder.record(
        "ping_started", correlation_id=ping_id, actor="Alice", started_monotonic_ns=10,
    )
    recorder.record(
        "caller_request_started", correlation_id="6" * 32, actor="Bob",
        started_monotonic_ns=20,
    )
    recorder.record(
        "ping_timed_out", correlation_id=ping_id, actor="Alice",
        started_monotonic_ns=10, completed_monotonic_ns=40,
        elapsed_ns=30, timeout_type="read_timeout",
    )

    summary = recorder.snapshot()["correlations"][0]

    assert summary["correlation_id"] == ping_id
    assert summary["ping_started"] is True
    assert summary["ping_timed_out"] is True
    assert summary["caller_start_ns"] == 10


def test_v2_retained_state_is_resanitized_and_bounded(tmp_path):
    path = tmp_path / "untrusted.json"
    secret = "never-retain-this-secret"
    query_secret = "never-retain-query-secret"
    secret_url = f"https://user:{secret}@example.invalid/post_find?token={query_secret}#private"
    timeout = {
        "event_type": "caller_request_timed_out", "correlation_id": "a" * 32,
        "actor": "Alice", "route": secret_url,
        "timeout_type": "read_timeout", "payload": secret,
    }
    path.write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "producer": "caller",
        "events": [
            {"event_type": "routine_event", "elapsed_ns": index, "route": secret_url,
             "correlation_id": f"invalid-{secret}", "payload": secret}
            for index in range(300)
        ],
        "critical_events": [timeout],
        "correlations": [{
            "correlation_id": "a" * 32, "actor": "Alice", "caller_timed_out": True,
            "route": secret_url, "request_body": secret,
        }],
        "unresolved_requests": [{
            "correlation_id": "b" * 32, "actor": "Alice", "bridge_received": True,
            "route": secret_url,
        }],
        "long_duration_requests": [{
            "correlation_id": "c" * 32, "actor": "Alice", "bridge_completed": True,
            "elapsed_ns": 10, "route": secret_url,
        }],
        "lifecycle": {"actors": {"Alice": {
            "listener": {"last_failed": {
                "event_type": "listener_failed", "actor": "Alice", "secret": secret,
            }, "first_ready": {"event_type": "routine_event", "actor": "Alice"}},
            "mineflayer": {}, "process": {},
        }}},
        "retention": {
            "recent": {"capacity": 2},
            "critical": {"dropped_count": 2},
        },
    }), encoding="utf-8")

    snapshot, error = read_diagnostic_snapshot(path)

    assert error is None
    assert snapshot["critical_events"][0]["timeout_type"] == "read_timeout"
    assert [event["elapsed_ns"] for event in snapshot["events"]] == [298, 299]
    assert all("correlation_id" not in event for event in snapshot["events"])
    assert snapshot["retention"]["recent"]["dropped_count"] == 298
    assert snapshot["correlations"][0]["caller_timed_out"] is True
    assert snapshot["unresolved_requests"][0]["route"] == "/post_find"
    assert snapshot["long_duration_requests"][0]["route"] == "/post_find"
    assert snapshot["retention"]["critical"]["dropped_count"] == 2
    assert "first_ready" not in snapshot["lifecycle"]["actors"]["Alice"]["listener"]
    assert secret not in json.dumps(snapshot)
    assert query_secret not in json.dumps(snapshot)
    projection = artifact_projection(path, runtime_root=tmp_path)
    assert projection["state"] == "valid"
    assert secret not in json.dumps(projection)
    assert query_secret not in json.dumps(projection)


def test_v1_artifact_remains_readable_and_oversized_artifact_is_rejected(tmp_path):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({
        "schema_version": "minecraft-bridge-diagnostics/1",
        "producer": "bridge",
        "events": [{"event_type": "listener_ready", "actor": "Alice"}],
    }), encoding="utf-8")

    snapshot, error = read_diagnostic_snapshot(legacy)

    assert error is None
    assert snapshot["schema_version"] == "minecraft-bridge-diagnostics/1"
    assert snapshot["events"][0]["event_type"] == "listener_ready"

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (MAX_ARTIFACT_BYTES + 1))
    assert read_diagnostic_snapshot(oversized) == (None, "diagnostic_artifact_too_large")
    assert artifact_projection(oversized, runtime_root=tmp_path) == {
        "state": "invalid", "error": "diagnostic_artifact_too_large",
    }

    nested = tmp_path / "nested.json"
    nested.write_text("[" * 1100 + "0" + "]" * 1100, encoding="utf-8")
    assert read_diagnostic_snapshot(nested) == (None, "invalid_diagnostic_artifact")
    assert artifact_projection(nested, runtime_root=tmp_path) == {
        "state": "invalid", "error": "invalid_diagnostic_artifact",
    }

    huge_integer = tmp_path / "huge-integer.json"
    huge_integer.write_text('{"value":' + "1" * 5000 + "}", encoding="utf-8")
    assert read_diagnostic_snapshot(huge_integer) == (None, "invalid_diagnostic_artifact")
    assert artifact_projection(huge_integer, runtime_root=tmp_path) == {
        "state": "invalid", "error": "invalid_diagnostic_artifact",
    }


def test_close_delivers_stop_signal_after_flush_timeout(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def blocked_write(_path, _snapshot):
        started.set()
        release.wait(2)

    monkeypatch.setattr("env.minecraft_bridge_diagnostics.atomic_write_json", blocked_write)
    recorder = BoundedDiagnosticRecorder(tmp_path / "bridge.json", producer="bridge")
    recorder.record("listener_starting", actor="Alice")
    assert started.wait(1)
    recorder.record("listener_ready", actor="Alice")

    assert recorder.close(timeout=0) is False
    release.set()
    deadline = time.monotonic() + 1
    while recorder._writer.is_alive() and time.monotonic() < deadline:
        time.sleep(0.001)
    assert recorder.close() is True


def test_runtime_result_preserves_diagnostics_and_collection_failure_is_non_authoritative():
    expected = {"schema_version": "minecraft-bridge-diagnostics-summary/1", "actors": {}}
    env = SimpleNamespace(
        get_score=lambda: {}, get_action_log=lambda: {},
        get_eac_audit_artifact=lambda: {}, bridge_cleanup_result={},
        get_minecraft_bridge_diagnostics=lambda: expected,
        agent_iteration_limit=None,
    )
    result = _runtime_result(env)
    assert result["minecraft_bridge_diagnostics"] is expected
    assert result["collection_errors"] == []

    env.get_minecraft_bridge_diagnostics = lambda: (_ for _ in ()).throw(OSError("secret"))
    failed = _runtime_result(env)
    assert failed["collection_errors"] == []
    assert failed["minecraft_bridge_diagnostics"]["diagnostic_collection_error"] == [
        {"error_type": "OSError"}
    ]


def test_runtime_checkpoint_preserves_diagnostics():
    expected = {"schema_version": "minecraft-bridge-diagnostics-summary/1", "actors": {}}
    env = SimpleNamespace(
        get_action_log=lambda: {},
        get_minecraft_bridge_diagnostics=lambda: expected,
    )

    result = _runtime_checkpoint_result(env)

    assert result["minecraft_bridge_diagnostics"] is expected


def test_diagnostics_do_not_change_timeout_or_retry_semantics():
    assert Agent.minecraft_connect_timeout_seconds == 5.0
    assert Agent.minecraft_read_timeout_seconds == 30.0
    error = MinecraftToolTimeoutError(
        "timed out", request_id="c" * 32, timeout_type="read_timeout",
    )
    assert error.failure_detail["outcome_certainty"] == "unknown"
    assert error.failure_detail["retry_safe"] is False


def test_controller_movement_cancellation_uses_bounded_no_retry_request(monkeypatch):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"terminal": True, "cancel_requested": True},
        )

    monkeypatch.setattr(Agent, "name2port", {"Alice": 5000})
    monkeypatch.setattr(Agent, "bridge_entrypoint_by_name", {"Alice": "bridge_fast"})
    monkeypatch.setattr(Agent, "get_agent_url", lambda _name: "http://localhost:5000")
    monkeypatch.setattr("env.minecraft_client.requests.request", request)

    result = Agent.cancel_active_movements(reason="controller_shutdown")

    assert result["terminal"] is True
    assert result["actors"]["Alice"] == {
        "state": "terminal", "terminal": True, "status_code": 200,
        "cancel_requested": True,
    }
    assert len(calls) == 1
    assert calls[0][0:2] == ("POST", "http://localhost:5000/post_cancel_movement")
    assert calls[0][2]["timeout"] == (0.5, 2.0)
    assert calls[0][2]["json"] == {"reason": "controller_shutdown"}


def test_controller_movement_cancellation_shares_one_multi_actor_budget(monkeypatch):
    timeouts = []

    def request(_method, _url, **kwargs):
        timeouts.append(kwargs["timeout"])
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"terminal": True, "cancel_requested": True},
        )

    monkeypatch.setattr(Agent, "name2port", {"Alice": 5000, "Bob": 5001})
    monkeypatch.setattr(
        Agent, "bridge_entrypoint_by_name",
        {"Alice": "bridge_fast", "Bob": "bridge_fast"},
    )
    monkeypatch.setattr(Agent, "get_agent_url", lambda name: f"http://localhost/{name}")
    monkeypatch.setattr("env.minecraft_client.requests.request", request)

    result = Agent.cancel_active_movements(
        reason="controller_shutdown", total_timeout_seconds=2.5,
    )

    assert result["terminal"] is True
    assert timeouts == [(0.25, 1.0), (0.25, 1.0)]
    assert sum(connect + read for connect, read in timeouts) == 2.5
