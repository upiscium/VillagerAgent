import pytest
import threading
from types import SimpleNamespace

from benchmarks.common.eac.gateway import EffectGateway
from benchmarks.minecraft.k11_instrumentation import (
    K11ProcessInstrumentation,
    _ObservationWindow,
    _controlled_shutdown_is_complete,
)
from benchmarks.minecraft.k11_trace import K11TraceRecorder, K11TraceScope, use_scope, PROSPECTIVE_TRACE_SCHEMA_VERSION
from env.runtime_paths import RuntimePaths
from model.openai_models import OpenAILanguageModel, ProviderCallCancellationError
from env.minecraft_client import LLMHandler
from pipeline.task_manager import TaskManager, TaskManagerFeedbackInterruptedError


class _FakeWaiter:
    def __init__(self):
        self.delay = None
        self.callback = None
        self.cancelled = False

    def __call__(self, delay, callback):
        self.delay, self.callback = delay, callback
        return self

    def cancel(self):
        self.cancelled = True


class _FakeController:
    def __init__(self):
        self.shutdown_requests = 0

    def _request_shutdown(self):
        self.shutdown_requests += 1


def test_k11_observation_window_fixed_close_is_authoritative_and_idempotent():
    trace = K11TraceRecorder("k11-window-fixed")
    waiter = _FakeWaiter()
    clock_values = iter((100,))
    window = _ObservationWindow(trace, 5, waiter, lambda: next(clock_values))
    controller = _FakeController()
    window.attach_controller(controller)

    window.open()
    assert waiter.delay == 5
    waiter.callback()
    waiter.callback()

    events = trace.artifact()["events"]
    assert [event["event_type"] for event in events] == [
        "k11.observation_window_opened", "k11.observation_window_closed",
    ]
    assert events[1]["payload"]["reason"] == "fixed_observation_horizon"
    assert events[1]["payload"]["window_close_monotonic_ns"] - events[0]["monotonic_ns"] == 5_000_000_000
    assert controller.shutdown_requests == 1


def test_k11_prospective_window_cut_has_exact_measurement_schema():
    trace = K11TraceRecorder("k11-window-prospective", schema_version=PROSPECTIVE_TRACE_SCHEMA_VERSION)
    waiter = _FakeWaiter()
    window = _ObservationWindow(trace, 5, waiter, lambda: 100)
    controller = _FakeController()
    window.attach_controller(controller)
    window.open()
    waiter.callback()
    cut = trace.artifact()["measurement_cut"]
    assert cut["boundary"] == "[open,close)"
    assert cut["close_reason"] == "fixed_observation_horizon"
    assert cut["snapshot_valid"] is False
    assert set(cut) >= {"active_executions", "open_lifecycles", "prepared_requests",
                         "evidence_high_water", "censoring_inventory"}


def test_k11_observation_window_natural_close_cancels_waiter():
    trace = K11TraceRecorder("k11-window-natural")
    waiter = _FakeWaiter()
    clock_values = iter((10, 20))
    window = _ObservationWindow(trace, 2, waiter, lambda: next(clock_values))
    window.attach_controller(_FakeController())
    window.open()
    window.natural_close()
    window.natural_close()

    assert waiter.cancelled
    assert trace.artifact()["events"][1]["payload"]["reason"] == "natural_runtime_terminal"


@pytest.mark.parametrize("horizon", [0, -1, float("inf"), float("nan"), True])
def test_k11_observation_horizon_must_be_positive_finite(horizon):
    with pytest.raises(ValueError, match="positive and finite"):
        K11ProcessInstrumentation(K11TraceRecorder("k11-invalid-window"), observation_horizon_seconds=horizon)


def test_k11_only_suppresses_controller_owned_horizon_interruption():
    expected = RuntimeError("Controller shutdown incomplete; interrupted tasks")
    controller = SimpleNamespace(
        _first_failure=(expected, None, {
            "thread": "run", "error": "Controller shutdown incomplete",
        }),
        shutdown_context={
            "shutdown_complete": True,
            "interrupted_task_ids": ["task-1"],
            "live_threads": [],
            "undrained_queues": [],
            "active_task_ids": [],
            "incomplete_submission_task_ids": [],
        },
    )

    assert _controlled_shutdown_is_complete(controller, expected) is True
    assert _controlled_shutdown_is_complete(
        controller, RuntimeError("unrelated failure"),
    ) is False


def test_k11_does_not_suppress_worker_failure_after_horizon():
    failure = RuntimeError("model transport failed")
    controller = SimpleNamespace(
        _first_failure=(failure, None, {"thread": "worker", "error": str(failure)}),
        shutdown_context={
            "shutdown_complete": True,
            "interrupted_task_ids": ["task-1"],
            "live_threads": [],
            "undrained_queues": [],
            "active_task_ids": [],
            "incomplete_submission_task_ids": [],
        },
    )

    assert _controlled_shutdown_is_complete(controller, failure) is False


def test_k11_does_not_suppress_checkpoint_failure_during_controlled_shutdown():
    failure = RuntimeError("Controller shutdown incomplete")
    controller = SimpleNamespace(
        _first_failure=(failure, None, {
            "thread": "run",
            "error": str(failure),
            "checkpoint_error": {"error_type": "OSError", "error": "disk full"},
        }),
        shutdown_context={
            "shutdown_complete": True,
            "interrupted_task_ids": ["task-1"],
            "live_threads": [],
            "undrained_queues": [],
            "active_task_ids": [],
            "incomplete_submission_task_ids": [],
        },
    )

    assert _controlled_shutdown_is_complete(controller, failure) is False


def test_k11_observation_window_can_arm_after_controller_clear_without_shifting_end():
    trace = K11TraceRecorder("k11-window-delayed-arm")
    waiter = _FakeWaiter()
    clock_values = iter((100, 2_000_000_100))
    window = _ObservationWindow(trace, 5, waiter, lambda: next(clock_values))
    window.attach_controller(_FakeController())

    window.open(arm=False)
    assert waiter.callback is None
    window.arm()

    assert waiter.delay == 3.0
    opened = trace.artifact()["events"][0]
    assert opened["payload"]["horizon_monotonic_ns"] == 5_000_000_100


def test_k11_wrapped_controller_arms_after_clear_and_cannot_lose_immediate_horizon(
    monkeypatch,
):
    from pipeline.controller_tiny import GlobalController

    trace = K11TraceRecorder("k11-window-controller-clear")
    waiter = _FakeWaiter()
    controller = SimpleNamespace(shutdown_event=threading.Event())
    controller._request_shutdown = controller.shutdown_event.set

    def run(fake_controller):
        fake_controller.shutdown_event.clear()
        waiter.callback()
        assert fake_controller.shutdown_event.is_set()
        return "stopped"

    monkeypatch.setattr(GlobalController, "run", run)
    with K11ProcessInstrumentation(
        trace,
        observation_horizon_seconds=5,
        observation_waiter=waiter,
        monotonic_clock=lambda: 100,
    ):
        assert GlobalController.run(controller) == "stopped"
        with pytest.raises(RuntimeError, match="one controller run"):
            GlobalController.run(controller)

    events = trace.artifact()["events"]
    assert [event["event_type"] for event in events] == [
        "k11.observation_window_opened", "k11.observation_window_closed",
    ]
    assert events[-1]["payload"]["reason"] == "fixed_observation_horizon"


def _model():
    model = object.__new__(OpenAILanguageModel)
    model.api_model = "gemma4:12b"
    return model


def test_k11_instruments_direct_openai_compatible_call_without_actor_scope(monkeypatch) -> None:
    def provider_call(unused_self, messages, model, temperature):
        return "ok"

    monkeypatch.setattr(OpenAILanguageModel, "gpt_api", provider_call)
    trace = K11TraceRecorder("k11-direct-openai")
    with K11ProcessInstrumentation(trace):
        assert _model().gpt_api([{"content": "secret-user"}], "override-model", 0) == "ok"

    events = trace.artifact()["events"]
    starts = [event for event in events if event["event_type"] == "k11.model_call_started"]
    completed = [event for event in events if event["event_type"] == "k11.model_call_completed"]
    assert len(starts) == len(completed) == 1
    assert starts[0]["payload"] == {
        "model_call_id": completed[0]["payload"]["model_call_id"],
        "model_name": "override-model",
    }
    assert "secret-user" not in str(events)


def test_k11_instruments_direct_openai_failure_in_actor_scope(monkeypatch) -> None:
    def provider_call(unused_self, messages, model, temperature):
        raise RuntimeError("provider failed")

    monkeypatch.setattr(OpenAILanguageModel, "gpt_api_stream", provider_call)
    trace = K11TraceRecorder("k11-direct-openai-failure")
    scope = K11TraceScope(
        trace.run_id, task_id="task-1", actor_id="Alice", agent_step_id="step-1",
    )
    with K11ProcessInstrumentation(trace), use_scope(scope):
        with pytest.raises(RuntimeError, match="provider failed"):
            _model().gpt_api_stream([{"content": "secret"}], "gemma4:12b", 0)

    events = trace.artifact()["events"]
    starts = [event for event in events if event["event_type"] == "k11.model_call_started"]
    failed = [event for event in events if event["event_type"] == "k11.model_call_failed"]
    assert len(starts) == len(failed) == 1
    assert starts[0]["actor_id"] == failed[0]["actor_id"] == "Alice"
    assert starts[0]["payload"]["model_call_id"] == failed[0]["payload"]["model_call_id"]
    assert failed[0]["payload"]["error_type"] == "RuntimeError"
    assert "provider failed" not in str(events)


def test_k11_instruments_provider_cancellation_as_one_terminal_failure(monkeypatch) -> None:
    def provider_call(unused_self, messages, model, temperature, **kwargs):
        raise ProviderCallCancellationError(
            "cancelled", provider_termination_confirmed=False,
            close_failure_diagnostics={"phase": "provider"},
        )

    monkeypatch.setattr(OpenAILanguageModel, "gpt_api_stream", provider_call)
    trace = K11TraceRecorder("k11-cancelled-openai")
    scope = K11TraceScope(
        trace.run_id, task_id="task-1", actor_id="Alice", agent_step_id="step-1",
    )
    with K11ProcessInstrumentation(trace), use_scope(scope):
        with pytest.raises(ProviderCallCancellationError):
            _model().gpt_api_stream([], "gemma4:12b", 0, cancellation_event=threading.Event())

    events = trace.artifact()["events"]
    starts = [event for event in events if event["event_type"] == "k11.model_call_started"]
    terminals = [event for event in events if event["event_type"] == "k11.model_call_failed"]
    assert len(starts) == len(terminals) == 1
    assert starts[0]["payload"]["model_call_id"] == terminals[0]["payload"]["model_call_id"]
    assert terminals[0]["payload"]["error_type"] == "ProviderCallCancellationError"


def test_k11_task_manager_feedback_cancellation_has_one_model_terminal(monkeypatch) -> None:
    def provider_call(unused_self, messages, model, temperature, **kwargs):
        raise ProviderCallCancellationError(
            "feedback cancelled",
            provider_termination_confirmed=True,
            close_failure_diagnostics={"phase": "task_manager_feedback"},
        )

    def feedback_call(model_self, *_args, cancellation_event=None, **_kwargs):
        return model_self.gpt_api_stream(
            [], "gemma4:12b", 0, cancellation_event=cancellation_event,
        )

    monkeypatch.setattr(OpenAILanguageModel, "gpt_api_stream", provider_call)
    monkeypatch.setattr(
        OpenAILanguageModel, "few_shot_generate_thoughts", feedback_call,
    )
    manager = object.__new__(TaskManager)
    manager.llm = _model()
    trace = K11TraceRecorder("k11-task-manager-feedback-cancelled")

    with K11ProcessInstrumentation(trace):
        with pytest.raises(TaskManagerFeedbackInterruptedError):
            manager._feedback_model_call(
                "system", "user", cancellation_token=threading.Event(),
            )

    events = trace.artifact()["events"]
    starts = [
        event for event in events
        if event["event_type"] == "k11.model_call_started"
    ]
    terminals = [
        event for event in events
        if event["event_type"] == "k11.model_call_failed"
    ]
    assert len(starts) == len(terminals) == 1
    assert starts[0]["payload"]["model_call_id"] == terminals[0]["payload"]["model_call_id"]


def test_k11_does_not_count_openai_cache_hit_as_provider_call(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("model.openai_models.OpenAI", lambda **unused: object())
    model = OpenAILanguageModel(
        api_key="test-key",
        runtime_paths=RuntimePaths.isolated(tmp_path),
    )
    model.save_cache("system\nuser", "cached")
    trace = K11TraceRecorder("k11-direct-openai-cache")

    with K11ProcessInstrumentation(trace):
        assert model.few_shot_generate_thoughts("system", "user", cache_enabled=True) == "cached"

    assert not [
        event for event in trace.artifact()["events"]
        if event["event_type"].startswith("k11.model_call_")
    ]


def test_k11_langchain_callbacks_pair_by_run_id_when_interleaved() -> None:
    trace = K11TraceRecorder("k11-langchain-interleaved")
    handler = LLMHandler()
    first_scope = K11TraceScope(trace.run_id, task_id="task-1", actor_id="Alice", agent_step_id="step-1")
    second_scope = K11TraceScope(trace.run_id, task_id="task-2", actor_id="Bob", agent_step_id="step-2")

    with K11ProcessInstrumentation(trace):
        with use_scope(first_scope):
            handler.on_llm_start({"name": "first"}, ["secret"], run_id="run-1")
        with use_scope(second_scope):
            handler.on_llm_start({"name": "second"}, ["secret"], run_id="run-2")
        handler.on_llm_end(SimpleNamespace(llm_output=None), run_id="run-1")
        handler.on_llm_end(SimpleNamespace(llm_output=None), run_id="run-2")

    events = trace.artifact()["events"]
    starts = {event["actor_id"]: event for event in events if event["event_type"] == "k11.model_call_started"}
    terminals = {event["actor_id"]: event for event in events if event["event_type"] == "k11.model_call_completed"}
    assert set(starts) == set(terminals) == {"Alice", "Bob"}
    assert all(
        starts[actor]["payload"]["model_call_id"] == terminals[actor]["payload"]["model_call_id"]
        for actor in starts
    )


def test_k11_partial_install_failure_restores_already_patched_symbols(monkeypatch) -> None:
    trace = K11TraceRecorder("k11-install-failure")
    instrumentation = K11ProcessInstrumentation(trace)
    original_gateway_init = EffectGateway.__init__
    original_patch = instrumentation._patch
    calls = 0

    def fail_second_patch(owner, name, replacement):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("install failed")
        original_patch(owner, name, replacement)

    monkeypatch.setattr(instrumentation, "_patch", fail_second_patch)

    with pytest.raises(RuntimeError, match="install failed"):
        instrumentation.__enter__()

    assert EffectGateway.__init__ is original_gateway_init
    assert instrumentation._restores == []


def test_k11_provider_ledger_records_sanitized_success_and_restores_snapshot_hook(monkeypatch):
    from pipeline.controller_tiny import GlobalController

    monkeypatch.setattr(
        "pipeline.controller_tiny.current_execution_diagnostic_identity",
        lambda: {"execution_id": "execution-1", "task_id": "task-1", "actor_id": "Alice"},
        raising=False,
    )
    original = getattr(GlobalController, "get_k11_provider_ledger_snapshot", None)
    trace = K11TraceRecorder("k11-provider-ledger")
    monkeypatch.setattr(OpenAILanguageModel, "gpt_api", lambda *_args, **_kwargs: "ok")
    with K11ProcessInstrumentation(
        trace, provider_ledger_clock=iter(range(10)).__next__,
        late_cleanup_identity={},
    ):
        ledger = GlobalController.get_k11_provider_ledger_snapshot(SimpleNamespace())
        assert ledger["schema_version"] == "k11-execution-provider-ledger/1"
        assert ledger["operations"]["items"] == []
        model = _model()
        model.gpt_api([], "model", 0)
        operation = GlobalController.get_k11_provider_ledger_snapshot(
            SimpleNamespace(),
        )["operations"]["items"][0]
        assert operation["outcome"] == "completed"
        assert set(operation) == {
            "provider_operation_id", "model_call_id", "execution_id", "task_id",
            "actor_id", "source",
            "start_monotonic_ns", "terminal_monotonic_ns", "terminal", "outcome",
        }
    assert getattr(GlobalController, "get_k11_provider_ledger_snapshot", None) is original


def test_k11_provider_ledger_bound_and_unresolved_metadata():
    trace = K11TraceRecorder("k11-provider-ledger-bound")
    instrumentation = K11ProcessInstrumentation(trace, provider_ledger_limit=1)
    ledger = instrumentation._provider_ledger
    ledger._identity = lambda: {
        "execution_id": "execution-1", "task_id": "task-1", "actor_id": "Alice",
    }
    ledger.start("first")
    second = ledger.start("second")
    ledger.terminal(second, outcome="failed", error=ValueError("secret payload"))
    ledger.terminal("missing", outcome="failed", error=RuntimeError("secret"))
    snapshot = ledger.snapshot()
    assert len(snapshot["operations"]["items"]) == 1
    assert snapshot["operations"]["retention"]["dropped_count"] == 1
    assert snapshot["operations"]["items"][0]["error_class"] == "ValueError"
    assert "secret payload" not in str(snapshot)
    assert any(
        item["reason"] == "terminal_without_retained_start"
        for item in snapshot["unresolved"]["items"]
    )


def test_k11_provider_ledger_ignores_operations_outside_execution_context():
    instrumentation = K11ProcessInstrumentation(
        K11TraceRecorder("k11-provider-unbound"),
    )
    operation_id = instrumentation._provider_ledger.start("controller-provider")
    instrumentation._provider_ledger.terminal(operation_id, outcome="completed")
    snapshot = instrumentation._provider_ledger.snapshot()
    assert operation_id is None
    assert snapshot["operations"]["items"] == []
    assert snapshot["unresolved"]["items"] == []
    assert instrumentation._provider_ledger.start(
        "agent-provider", identity_required=True,
    ) is None
    assert instrumentation._provider_ledger.snapshot()["unresolved"]["items"] == [
        {"reason": "provider_start_without_execution_identity"},
    ]


def test_k11_provider_unresolved_retention_is_bounded():
    instrumentation = K11ProcessInstrumentation(
        K11TraceRecorder("k11-provider-unresolved"), provider_ledger_limit=1,
    )
    ledger = instrumentation._provider_ledger
    ledger.terminal("missing-1", outcome="failed")
    ledger.terminal("missing-2", outcome="failed")
    snapshot = ledger.snapshot()
    assert len(snapshot["unresolved"]["items"]) == 1
    assert snapshot["unresolved"]["retention"] == {
        "capacity": 1, "retained": 1, "truncated": True, "dropped_count": 1,
    }


def test_k11_provider_terminal_callback_survives_instrumentation_restore(monkeypatch):
    monkeypatch.setattr(
        "pipeline.controller_tiny.current_execution_diagnostic_identity",
        lambda: {"execution_id": "execution-1", "task_id": "task-1", "actor_id": "Alice"},
    )
    instrumentation = K11ProcessInstrumentation(
        K11TraceRecorder("k11-provider-late-terminal"),
        late_cleanup_identity={},
    )
    handler = LLMHandler()
    with instrumentation:
        handler.on_llm_start({"name": "provider"}, ["secret"], run_id="late")
    handler.on_llm_error(RuntimeError("secret provider text"), run_id="late")
    operation = instrumentation._provider_ledger.snapshot()["operations"]["items"][0]
    assert operation["terminal"] is True
    assert operation["outcome"] == "failed"
    assert operation["error_class"] == "RuntimeError"
    assert "secret provider text" not in str(operation)
    handler.on_llm_start({"name": "second"}, ["secret"], run_id="after-restore")
    handler.on_llm_end(SimpleNamespace(llm_output=None), run_id="after-restore")
    operations = instrumentation._provider_ledger.snapshot()["operations"]["items"]
    assert len(operations) == 2
    assert operations[1]["terminal"] is True
