import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from pipeline.agent import (
    BaseAgent,
    ReflectionInterruptedError,
    ReflectionOutcome,
)
from pipeline.controller_tiny import GlobalController, TaskExecutionGroup
from pipeline.task_manager import (
    TaskManager,
    TaskManagerFeedbackCommitError,
    TaskManagerFeedbackOutcome,
)
from model.openai_models import (
    OpenAILanguageModel,
    ProviderCallCancellationError,
    ProviderCallTerminationError,
)
from type_define.graph import Graph, GraphState, Task


class Manager:
    def __init__(self, task):
        self.graph = SimpleNamespace(vertex=[task])
        self.status_updates = []
        self.feedback_calls = 0

    def mark_task_status(self, task_id, status, feedback=None, **kwargs):
        self.status_updates.append((task_id, status, feedback, kwargs))

    def feedback_task(self, _task):
        self.feedback_calls += 1


class Agent:
    def __init__(self, name, reflection=None, cancellation=False):
        self.name = name
        self.reflection = reflection
        self.reflection_calls = 0
        self.cancellation = cancellation

    def supports_cooperative_cancellation(self):
        return False

    def reflect(self, _task, _detail, cancellation_token=None):
        self.reflection_calls += 1
        if self.reflection is not None:
            return self.reflection(cancellation_token)
        return True


class FeedbackModel(OpenAILanguageModel):
    def __init__(self, callback):
        self.callback = callback

    def few_shot_generate_thoughts(self, *_args, **kwargs):
        return self.callback(kwargs.get("cancellation_event"))


class ModelFeedbackManager(TaskManager):
    def __init__(self, task, model):
        self.graph = SimpleNamespace(vertex=[task])
        self.status = TaskManager.idle
        self.manage_method = "update"
        self.llm = model
        self.logger = SimpleNamespace(error=lambda *a, **k: None)
        self.runtime_task_store = SimpleNamespace(
            terminal_state=lambda: GraphState.FAILURE,
        )
        self.status_updates = []
        self.trace_calls = 0
        self.model_calls = 0
        self.mutation_calls = 0

    def mark_task_status(self, task_id, status, feedback=None):
        self.status_updates.append((task_id, status, feedback, {}))

    def add_task_to_trace(self):
        self.trace_calls += 1

    def update_task(
        self,
        _task,
        cancellation_token=None,
        commit_lock=None,
        persistence_gate=None,
    ):
        self.model_calls += 1
        self._feedback_model_call(
            "system",
            "user",
            cancellation_token=cancellation_token,
            cache_enabled=False,
        )
        with self._feedback_commit_context(commit_lock):
            self._require_feedback_admission(
                cancellation_token, phase="test_feedback_commit",
            )
            self.mutation_calls += 1


def controller(names=("Alice",)):
    task = Task("deterministic post processing", {})
    task.candidate_list = list(names)
    task.number = len(names)
    task.available = True
    task.status = Task.running
    ctl = object.__new__(GlobalController)
    ctl.agent_list = [Agent(name) for name in names]
    ctl.assignment = {name: task.id for name in names}
    ctl.task_list = [task]
    ctl.task_queue = []
    ctl.result_queue = []
    ctl.task_list_lock = threading.Lock()
    ctl.result_list_lock = threading.Lock()
    ctl._execution_state_lock = threading.RLock()
    ctl._tool_action_condition = threading.Condition(ctl._execution_state_lock)
    ctl._active_tool_actions = 0
    ctl.shutdown_event = threading.Event()
    ctl._judger_terminal_pending = False
    ctl._judger_terminal_observed = False
    ctl._judger_terminal_reconciled = False
    ctl.max_task_time = 30
    ctl.query_interval = .001
    ctl.cancellation_grace_period = .05
    ctl.post_processing_cancellation_grace_period = .05
    ctl.task_manager = Manager(task)
    ctl.logger = SimpleNamespace(error=lambda *a, **k: None,
                                 exception=lambda *a, **k: None,
                                 info=lambda *a, **k: None)
    ctl._post_processing_interruption_ledger = {}
    ctl._provider_termination_unconfirmed_task_ids = []
    ctl._result_claim_cursor = 0
    ctl._post_processing_cancel_event = threading.Event()
    ctl.env = SimpleNamespace(
        agents_ping=lambda: {"status": True},
        is_task_complete=lambda: False,
    )
    return ctl, task


def group_for(ctl, task, agents=None):
    agents = agents or ctl.agent_list
    group = TaskExecutionGroup(task=task, agents=agents, submission_complete=True)
    group.started_at = time.time()
    for agent in agents:
        future = Future()
        future.set_result(("done", {"answer": agent.name}))
        group.futures[agent.name] = future
    ctl.result_queue.append(group)
    return group


def test_result_lock_is_acquirable_while_reflection_is_blocked():
    ctl, task = controller()
    entered, release = threading.Event(), threading.Event()
    ctl.agent_list[0].reflection = lambda _token: (entered.set(), release.wait(1), True)[2]
    group = group_for(ctl, task)
    thread = threading.Thread(target=ctl.process_completed_tasks)
    thread.start()
    try:
        assert entered.wait(.5)
        assert ctl.result_list_lock.acquire(timeout=.1)
        ctl.result_list_lock.release()
    finally:
        ctl._request_shutdown()
        release.set()
        thread.join(1)
    assert not thread.is_alive()


def test_concurrent_append_is_preserved_while_a_reflects():
    ctl, task = controller()
    entered, release = threading.Event(), threading.Event()
    ctl.agent_list[0].reflection = lambda _token: (entered.set(), release.wait(1), True)[2]
    first = group_for(ctl, task)
    thread = threading.Thread(target=ctl.process_completed_tasks)
    thread.start()
    try:
        assert entered.wait(.5)
        second = TaskExecutionGroup(Task("B", {}), [])
        with ctl.result_list_lock:
            ctl.result_queue.append(second)
    finally:
        release.set()
        deadline = time.monotonic() + .5
        while not first.completed and time.monotonic() < deadline:
            time.sleep(.005)
        ctl._request_shutdown()
        thread.join(1)
    assert first.completed is True
    assert ctl.result_queue == [second]


def test_post_processing_persists_each_side_effect_once():
    ctl, task = controller()
    group = group_for(ctl, task)
    assert ctl.finalize_execution_group(group) is True
    assert ctl.finalize_execution_group(group) is True
    assert len(ctl.task_manager.status_updates) == 1
    assert ctl.task_manager.feedback_calls == 1
    assert ctl.agent_list[0].reflection_calls == 1
    assert ctl.assignment == {}


def test_unfinished_group_is_requeued_exactly_once():
    ctl, task = controller()
    group = group_for(ctl, task)
    group.submission_complete = False
    claimed, token = ctl._claim_next_result_group()
    assert claimed is group
    assert ctl.finalize_execution_group(group) is False
    ctl._finish_result_group_claim(group, token, remove=False)
    assert ctl.result_queue == [group]
    assert group.post_processing_claim_token is None


def test_shutdown_before_reflection_makes_no_reflection_or_terminal_result():
    ctl, task = controller()
    group = group_for(ctl, task)
    ctl._request_shutdown()
    assert ctl.finalize_execution_group(group) is False
    assert ctl.agent_list[0].reflection_calls == 0
    assert ctl.task_manager.status_updates == []
    assert group.post_processing_interrupted is True
    shutdown_state = ctl._finalize_shutdown_groups()
    assert ctl.task_manager.status_updates[0][1] == Task.running
    assert task.status == Task.running
    assert ctl.result_queue == []
    assert shutdown_state == ([], [], [], [], [])
    assert ctl._provider_termination_unconfirmed_task_ids == []


@pytest.mark.parametrize("cooperative", [True, False])
def test_blocked_reflection_shutdown_is_bounded_and_truthful(cooperative):
    ctl, task = controller()
    entered, release = threading.Event(), threading.Event()

    def blocked(token):
        entered.set()
        if cooperative:
            token.wait(1)
            return True
        release.wait(1)
        return True

    ctl.agent_list[0].reflection = blocked
    group = group_for(ctl, task)
    thread = threading.Thread(target=ctl.finalize_execution_group, args=(group,))
    thread.start()
    try:
        assert entered.wait(.5)
        ctl._request_shutdown()
        thread.join(.6)
        assert not thread.is_alive()
        ctl._finalize_shutdown_groups()
        interruption = ctl._post_processing_interruption_ledger[task.id]
        assert interruption["provider_termination_confirmed"] is cooperative
    finally:
        release.set()
        thread.join(1)


def test_judger_terminal_detection_cancels_in_flight_reflection():
    ctl, task = controller()
    entered = threading.Event()

    def blocked(token):
        entered.set()
        assert token.wait(.5)
        return True

    ctl.agent_list[0].reflection = blocked
    group = group_for(ctl, task)
    thread = threading.Thread(target=ctl.finalize_execution_group, args=(group,))
    terminal = threading.Event()
    ctl.env = SimpleNamespace(
        is_task_complete=terminal.is_set,
        get_score=lambda: {"status": "success"},
    )
    thread.start()
    assert entered.wait(.5)
    terminal.set()
    assert ctl.observe_judger_terminal() is True
    thread.join(.5)

    assert not thread.is_alive()
    assert ctl.shutdown_event.is_set() is False
    assert group.post_processing_interrupted is True
    assert group.post_processing_interruption[
        "provider_termination_confirmed"
    ] is True


def test_shutdown_does_not_fabricate_detail_or_mutate_task_result():
    ctl, task = controller()
    group = group_for(ctl, task)
    ctl._request_shutdown()
    assert ctl.finalize_execution_group(group) is False
    assert ctl.task_manager.status_updates == []
    assert task.status == Task.running
    assert task.reflect is None


def test_queue_and_snapshot_locks_remain_observable_during_reflection():
    ctl, task = controller()
    entered, release = threading.Event(), threading.Event()
    ctl.agent_list[0].reflection = lambda _token: (entered.set(), release.wait(1), True)[2]
    group = group_for(ctl, task)
    thread = threading.Thread(target=ctl.process_completed_tasks)
    thread.start()
    try:
        assert entered.wait(.5)
        with ctl.task_list_lock:
            with ctl.result_list_lock:
                assert list(ctl.result_queue) == [group]
        assert ctl._execution_groups_snapshot() == [group]
    finally:
        ctl._request_shutdown()
        release.set()
        thread.join(1)


def test_normal_path_is_clean_and_uses_original_detail():
    ctl, task = controller()
    group = group_for(ctl, task)
    assert ctl.finalize_execution_group(group) is True
    assert ctl.task_manager.status_updates[0][1] == Task.success
    assert ctl.task_manager.status_updates[0][2] == {"answer": "Alice"}
    assert group.post_processing_interrupted is False


def test_committed_reflection_is_not_reclassified_when_shutdown_follows():
    ctl, task = controller()
    group = group_for(ctl, task)

    def committed_then_shutdown(*_args):
        ctl._request_shutdown()
        return ReflectionOutcome(True)

    ctl._reflect_agent_bounded = committed_then_shutdown

    assert ctl.finalize_execution_group(group) is True
    assert group.reflection_committed == {"Alice"}
    assert group.post_processing_interrupted is False
    assert ctl.task_manager.status_updates[0][1] == Task.success
    assert ctl.task_manager.feedback_calls == 0
    assert group.feedback_interrupted is True
    assert ctl._feedback_interruption_ledger[task.id]["feedback_started"] is False


def test_terminal_persistence_is_linearized_before_shutdown():
    ctl, task = controller()
    group = group_for(ctl, task)
    persistence_entered = threading.Event()
    release_persistence = threading.Event()
    original_mark = ctl.task_manager.mark_task_status

    def blocked_mark(*args, **kwargs):
        persistence_entered.set()
        assert release_persistence.wait(.5)
        original_mark(*args, **kwargs)

    ctl.task_manager.mark_task_status = blocked_mark
    finalizer = threading.Thread(target=ctl.finalize_execution_group, args=(group,))
    finalizer.start()
    assert persistence_entered.wait(.5)
    shutdown = threading.Thread(target=ctl._request_shutdown)
    shutdown.start()
    time.sleep(.02)
    assert shutdown.is_alive()
    release_persistence.set()
    finalizer.join(.5)
    shutdown.join(.5)

    assert not finalizer.is_alive()
    assert not shutdown.is_alive()
    assert group.terminal_state_persisted is True
    assert group.post_processing_interrupted is False


def model_feedback_controller(callback):
    ctl, task = controller()
    manager = ModelFeedbackManager(task, FeedbackModel(callback))
    ctl.task_manager = manager
    ctl.agent_list[0].reflection = lambda _token: ReflectionOutcome(True)
    return ctl, task, manager, group_for(ctl, task)


def test_shutdown_between_terminal_persistence_and_feedback_starts_no_model():
    ctl, task, manager, group = model_feedback_controller(
        lambda _token: "unused",
    )
    original_mark = manager.mark_task_status

    def shutdown_after_terminal(*args, **kwargs):
        original_mark(*args, **kwargs)
        ctl._request_shutdown()

    manager.mark_task_status = shutdown_after_terminal

    assert ctl.finalize_execution_group(group) is True
    assert task.status == Task.success
    assert manager.model_calls == 0
    assert manager.mutation_calls == 0
    assert group.feedback_interrupted is True
    assert group.feedback_interruption["feedback_started"] is False


def test_in_flight_task_manager_model_is_cancelled_with_one_feedback_attempt():
    entered = threading.Event()

    def blocked(token):
        entered.set()
        assert token.wait(.5)
        raise ProviderCallCancellationError(
            "feedback cancelled",
            provider_termination_confirmed=True,
            close_failure_diagnostics={"phase": "test_provider"},
        )

    ctl, task, manager, group = model_feedback_controller(blocked)
    finalizer = threading.Thread(target=ctl.finalize_execution_group, args=(group,))
    finalizer.start()
    assert entered.wait(.5)
    ctl._request_shutdown()
    finalizer.join(.5)

    assert not finalizer.is_alive()
    assert task.status == Task.success
    assert manager.model_calls == 1
    assert manager.mutation_calls == 0
    assert group.feedback_interrupted is True
    assert group.feedback_interruption["provider_termination_confirmed"] is True


def test_real_task_manager_update_model_is_cancelled_before_graph_mutation(
    tmp_path, monkeypatch,
):
    entered = threading.Event()

    def blocked(token):
        entered.set()
        assert token.wait(.5)
        raise ProviderCallCancellationError(
            "real feedback cancelled",
            provider_termination_confirmed=True,
            close_failure_diagnostics={"phase": "real_task_manager"},
        )

    ctl, task = controller()
    manager = TaskManager(
        silent=True,
        method="update",
        history_output_dir=tmp_path,
    )
    manager.set_task_list_from_decomposition([task])
    projected_task = manager.graph.vertex[0]
    manager.mark_task_running(projected_task, ["Alice"])
    manager.llm = FeedbackModel(blocked)
    manager.dm = SimpleNamespace(
        query_env_with_task=lambda _description: "environment",
        query_history=lambda _name: "history",
    )
    manager.agent_list = ctl.agent_list
    manager.task_description = "root task"
    manager.task_document = {}
    monkeypatch.setattr(Graph, "write_graph_to_md", lambda *_a, **_k: None)
    monkeypatch.setattr(Graph, "write_graph_to_json", lambda *_a, **_k: None)
    ctl.task_manager = manager
    ctl.task_list = [projected_task]
    ctl.assignment = {"Alice": projected_task.id}
    ctl.agent_list[0].reflection = lambda _token: ReflectionOutcome(True)
    group = group_for(ctl, projected_task)
    finalizer = threading.Thread(target=ctl.finalize_execution_group, args=(group,))
    finalizer.start()
    assert entered.wait(.5)
    ctl._request_shutdown()
    finalizer.join(.5)

    assert not finalizer.is_alive()
    assert projected_task.status == Task.success
    assert group.feedback_interrupted is True
    snapshot = manager.runtime_task_store.snapshot()
    assert len(snapshot["nodes"]) == 1
    assert snapshot["nodes"][0]["lifecycle"]["status"] == Task.success


def test_uncooperative_task_manager_model_keeps_shutdown_unconfirmed():
    entered = threading.Event()
    release = threading.Event()

    def blocked(_token):
        entered.set()
        release.wait(1)
        return "late"

    ctl, task, manager, group = model_feedback_controller(blocked)
    ctl.post_processing_cancellation_grace_period = .05
    finalizer = threading.Thread(target=ctl.finalize_execution_group, args=(group,))
    finalizer.start()
    try:
        assert entered.wait(.5)
        ctl._request_shutdown()
        finalizer.join(.5)
        assert not finalizer.is_alive()
        assert manager.model_calls == 1
        assert manager.mutation_calls == 0
        assert group.feedback_interruption[
            "provider_termination_confirmed"
        ] is False
        assert ctl._provider_termination_unconfirmed_task_ids == [task.id]
    finally:
        release.set()
        finalizer.join(1)


def test_task_manager_provider_timeout_forces_incomplete_shutdown_state():
    def terminated(_token):
        raise ProviderCallTerminationError(
            "provider worker remained active",
        )

    ctl, task, manager, group = model_feedback_controller(terminated)

    assert ctl.finalize_execution_group(group) is True
    assert ctl.shutdown_event.is_set() is True
    assert manager.model_calls == 1
    assert manager.mutation_calls == 0
    assert group.feedback_interruption[
        "provider_termination_confirmed"
    ] is False
    assert ctl._provider_termination_unconfirmed_task_ids == [task.id]


def test_base_agent_reflection_provider_timeout_is_unconfirmed():
    def terminated(_token):
        raise ProviderCallTerminationError(
            "reflection provider worker remained active",
        )

    agent = object.__new__(BaseAgent)
    agent.llm = FeedbackModel(terminated)
    agent.name = "Alice"
    agent.data_manager = SimpleNamespace(
        query_history=lambda _name: "history",
    )
    task = Task("reflection timeout", {})
    task.milestones = []

    with pytest.raises(ReflectionInterruptedError) as raised:
        agent.reflect(
            task,
            {"action_list": []},
            cancellation_token=threading.Event(),
            commit_lock=threading.RLock(),
        )

    assert raised.value.provider_termination_confirmed is False
    assert raised.value.diagnostics["phase"] == "provider_timeout"


def test_normal_task_manager_feedback_model_and_mutation_execute_once():
    ctl, task, manager, group = model_feedback_controller(
        lambda _token: "feedback response",
    )

    assert ctl.finalize_execution_group(group) is True
    assert ctl.finalize_execution_group(group) is True
    assert task.status == Task.success
    assert manager.model_calls == 1
    assert manager.mutation_calls == 1
    assert group.feedback_completed is True
    assert group.feedback_interrupted is False


def test_post_commit_feedback_artifact_failure_is_not_retried():
    ctl, task = controller()
    group = group_for(ctl, task)
    calls = []

    def committed_then_failed(_task, **_kwargs):
        calls.append(1)
        raise TaskManagerFeedbackCommitError("artifact write failed")

    ctl.task_manager.feedback_task = committed_then_failed

    errors = []

    def process():
        try:
            ctl.process_completed_tasks()
        except BaseException as exc:
            errors.append(exc)

    processor = threading.Thread(target=process)
    processor.start()
    processor.join(.5)

    assert not processor.is_alive()
    assert isinstance(errors[0], TaskManagerFeedbackCommitError)
    assert group.completed is True
    assert group.feedback_persisted is True
    assert ctl.finalize_execution_group(group) is True
    assert calls == [1]
    assert ctl.result_queue == []


def test_committed_feedback_with_deferred_artifacts_is_auditable():
    ctl, task, manager, group = model_feedback_controller(
        lambda _token: "unused",
    )
    manager.update_task = lambda *_a, **_k: TaskManagerFeedbackOutcome(
        ancillary_complete=False,
    )

    assert ctl.finalize_execution_group(group) is True
    assert group.feedback_persisted is True
    assert group.feedback_ancillary_complete is False
    assert ctl._feedback_interruption_ledger[task.id] == {
        "task_id": task.id,
        "reason": "task_manager_feedback_ancillary_deferred",
        "feedback_started": True,
        "committed": True,
        "ancillary_complete": False,
        "provider_termination_confirmed": True,
        "diagnostics": {"phase": "after_feedback_commit"},
    }


def test_feedback_artifact_persistence_stops_after_cancellation():
    task = Task("artifact cancellation", {})
    cancellation = threading.Event()
    manager = ModelFeedbackManager(task, FeedbackModel(lambda _token: "unused"))
    calls = []

    def update_history(*_args):
        calls.append("history")
        cancellation.set()

    manager.update_history = update_history
    manager.checkpoint_runtime_state = lambda **_kwargs: calls.append("checkpoint")
    manager.emit_task_graph_snapshot = lambda *_args: calls.append("snapshot")
    manager.graph.write_graph_to_md = lambda *_args: calls.append("markdown")
    manager.graph.write_graph_to_json = lambda *_args: calls.append("json")

    outcome = manager._persist_feedback_artifacts(
        history=("system", "user", "response"),
        snapshot_source="test",
        cancellation_token=cancellation,
    )

    assert outcome.committed is True
    assert outcome.ancillary_complete is False
    assert calls == ["history"]


def test_feedback_persistence_gate_closes_atomically_with_shutdown():
    ctl, _task = controller()
    entered = threading.Event()
    release = threading.Event()
    completed = []

    def admitted_operation():
        entered.set()
        assert release.wait(.5)
        completed.append("admitted")

    operation = threading.Thread(
        target=lambda: ctl._run_feedback_persistence_operation(
            admitted_operation,
            ctl._post_processing_cancellation_token(),
        ),
    )
    operation.start()
    assert entered.wait(.5)
    shutdown = threading.Thread(target=ctl._request_shutdown)
    shutdown.start()
    time.sleep(.02)
    assert not shutdown.is_alive()
    release.set()
    operation.join(.5)
    shutdown.join(.5)

    late_calls = []
    admitted = ctl._run_feedback_persistence_operation(
        lambda: late_calls.append("late"),
        ctl._post_processing_cancellation_token(),
    )
    assert completed == ["admitted"]
    assert admitted is False
    assert late_calls == []


def test_explicit_failure_shutdown_cannot_start_task_manager_feedback_model():
    ctl, task, manager, group = model_feedback_controller(
        lambda _token: "unused",
    )
    failed = Future()
    failed.set_result(("done", {"failure": {"reason": "explicit"}}))
    group.futures["Alice"] = failed
    original_mark = manager.mark_task_status

    def shutdown_after_terminal(*args, **kwargs):
        original_mark(*args, **kwargs)
        ctl._request_shutdown()

    manager.mark_task_status = shutdown_after_terminal

    assert ctl.finalize_execution_group(group) is True
    assert task.status == Task.failure
    assert ctl.agent_list[0].reflection_calls == 0
    assert manager.model_calls == 0
    assert group.feedback_interrupted is True
