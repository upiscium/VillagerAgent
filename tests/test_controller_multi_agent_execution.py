import logging
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace

import pipeline.controller_tiny as controller_tiny
import pytest

from env.minecraft_client import MinecraftToolTimeoutError
from pipeline.controller_tiny import ControllerShutdownError, GlobalController, TaskExecutionGroup
from pipeline.task_manager import TaskManager
from type_define.graph import Task


def test_execute_assignments_queues_all_assigned_agents_as_one_group():
    controller, task, agents = _controller_with_task(["Alice", "Bob"], required=2)

    controller.execute_assignments([{
        "task_instance": task,
        "agent_instances": agents,
    }])

    group = controller.task_queue[0]
    assert group.task is task
    assert [agent.name for agent in group.agents] == ["Alice", "Bob"]


def test_start_execution_group_creates_one_future_per_agent():
    controller, task, agents = _controller_with_task(["Alice", "Bob"], required=2)
    controller.execute_assignments([{"task_instance": task, "agent_instances": agents}])
    controller.executor = _ExecutorStub()

    controller.start_execution_group(controller.task_queue.pop(0))

    group = controller.result_queue[0]
    assert list(group.futures) == ["Alice", "Bob"]
    assert controller.executor.submitted_agents == ["Alice", "Bob"]


def test_start_execution_group_fans_independent_tokens_to_simultaneous_agents():
    controller, task, agents = _controller_with_task(["Alice", "Bob"], required=2)
    controller.execute_assignments([{"task_instance": task, "agent_instances": agents}])
    controller.executor = _ExecutorStub()

    controller.start_execution_group(controller.task_queue.pop(0))
    group = controller.result_queue[0]

    assert set(group.cancellation_tokens) == {"Alice", "Bob"}
    assert group.cancellation_tokens["Alice"] is not group.cancellation_tokens["Bob"]
    assert all(token.is_set() is False for token in group.cancellation_tokens.values())


def test_terminal_observation_rejects_direct_group_start():
    controller, task, agents = _controller_with_task(["Alice"], required=1)
    controller.env = _terminal_env()
    controller.executor = _ExecutorStub()
    assert controller.observe_judger_terminal() is True

    with pytest.raises(ControllerShutdownError, match="after judger terminal"):
        controller.start_execution_group(TaskExecutionGroup(task=task, agents=agents))

    assert controller.executor.submitted_agents == []
    assert controller.result_queue == []


def test_terminal_observation_prevents_assignment_enqueue():
    controller, task, agents = _controller_with_task(["Alice"], required=1)
    controller.env = _terminal_env()
    assert controller.observe_judger_terminal() is True

    assigned = controller.execute_assignments([{
        "task_instance": task,
        "agent_instances": agents,
    }])

    assert assigned == 0
    assert controller.assignment == {}
    assert controller.task_queue == []
    assert task.status == Task.unknown


def test_terminal_observation_waits_for_complete_multi_agent_submission():
    controller, task, agents = _controller_with_task(["Alice", "Bob"], required=2)
    controller.env = _terminal_env()
    executor = _BlockingFirstSubmitExecutor()
    controller.executor = executor
    group = TaskExecutionGroup(task=task, agents=agents)
    submission = threading.Thread(target=controller.start_execution_group, args=(group,))
    observation_result = []
    observation = threading.Thread(
        target=lambda: observation_result.append(controller.observe_judger_terminal())
    )

    submission.start()
    assert executor.first_submit_started.wait(1)
    observation.start()
    time.sleep(0.05)
    assert observation.is_alive()
    executor.release_first_submit.set()
    submission.join(1)
    observation.join(1)

    assert not submission.is_alive()
    assert not observation.is_alive()
    assert executor.submitted_agents == ["Alice", "Bob"]
    assert group.submission_complete is True
    assert observation_result == [True]
    assert controller._judger_terminal_pending is True
    assert controller._judger_terminal_observed is False


def test_controller_forwards_local_runtime_config_to_base_agents(monkeypatch):
    created_agents = []
    model_configs = []

    class AgentFactory:
        LOCAL_MODEL_CONFIG_KEYS = (
            "local_model_max_attempts",
            "local_model_max_actions",
            "local_model_inter_action_delay",
        )

        def __init__(self, llm, env, data_manager, **kwargs):
            self.name = kwargs["name"]
            created_agents.append(kwargs)

    def init_model(config):
        model_configs.append(dict(config))
        return SimpleNamespace(role_name="")

    monkeypatch.setattr(controller_tiny, "BaseAgent", AgentFactory)
    monkeypatch.setattr(controller_tiny, "init_language_model", init_model)
    task_manager = SimpleNamespace()
    data_manager = SimpleNamespace()
    env = SimpleNamespace(agent_pool=[SimpleNamespace(name="Alice")])
    base_agent_config = {
        "provider": "vllm",
        "api_model": "local-model",
        "local_model_max_attempts": 7,
        "local_model_max_actions": 3,
        "local_model_inter_action_delay": 0.25,
    }

    controller = GlobalController(
        {"provider": "openai", "api_model": "controller-model"},
        task_manager,
        data_manager,
        env,
        silent=True,
        base_agent_config=base_agent_config,
    )
    controller.executor.shutdown()

    assert base_agent_config in model_configs
    assert created_agents == [{
        "name": "Alice",
        "silent": False,
        "all_tools": [],
        "local_model_max_attempts": 7,
        "local_model_max_actions": 3,
        "local_model_inter_action_delay": 0.25,
    }]


def test_agent_runtime_mode_does_not_leak_between_controllers(monkeypatch):
    def init_model(_config):
        return SimpleNamespace(role_name="")

    monkeypatch.setattr(controller_tiny, "init_language_model", init_model)

    def build_controller(running):
        manager = SimpleNamespace()
        data_manager = SimpleNamespace()
        env = SimpleNamespace(
            running=running,
            agent_pool=[SimpleNamespace(name="Alice")],
        )
        return GlobalController(
            {"provider": "openai", "api_model": "test"},
            manager,
            data_manager,
            env,
            silent=True,
        )

    virtual_controller = build_controller(False)
    real_controller = build_controller(True)
    virtual_controller.executor.shutdown()
    real_controller.executor.shutdown()

    assert virtual_controller.agent_list[0]._virtual_debug is True
    assert real_controller.agent_list[0]._virtual_debug is False


def test_execution_group_succeeds_only_after_all_agents_succeed():
    controller, task, agents = _controller_with_task(["Alice", "Bob"], required=2)
    group = _started_group(controller, task, agents)

    assert controller.finalize_execution_group(group) is True

    assert controller.task_manager.status_updates == [(task.id, Task.success, {
        "agent_results": {
            "Alice": {"status": "success", "detail": "Alice detail"},
            "Bob": {"status": "success", "detail": "Bob detail"},
        },
    })]


def test_single_agent_execution_preserves_detail_feedback_shape():
    controller, task, agents = _controller_with_task(["Alice"], required=1)
    group = _started_group(controller, task, agents)

    controller.finalize_execution_group(group)

    assert controller.task_manager.status_updates == [
        (task.id, Task.success, "Alice detail")
    ]


def test_execution_group_fails_once_when_one_agent_raises():
    controller, task, agents = _controller_with_task(["Alice", "Bob"], required=2)
    agents[1].step_error = RuntimeError("Bob failed")
    group = _started_group(controller, task, agents)

    assert controller.finalize_execution_group(group) is True

    assert len(controller.task_manager.status_updates) == 1
    task_id, status, feedback = controller.task_manager.status_updates[0]
    assert task_id == task.id
    assert status == Task.failure
    assert feedback["agent_results"]["Alice"]["status"] == "success"
    assert feedback["agent_results"]["Bob"] == {
        "status": "failure",
        "error": "Bob failed",
    }

    assert controller.finalize_execution_group(group) is True
    assert len(controller.task_manager.status_updates) == 1


def test_minecraft_timeout_failure_metadata_reaches_task_feedback():
    controller, task, agents = _controller_with_task(["Alice"], required=1)
    agents[0].step_error = MinecraftToolTimeoutError(
        "response timed out",
        agent="Alice",
        tool="post_place",
    )
    group = _started_group(controller, task, agents)

    assert controller.finalize_execution_group(group) is True

    _, status, feedback = controller.task_manager.status_updates[0]
    assert status == Task.failure
    assert feedback["error"] == "response timed out"
    assert feedback["failure"] == {
        "reason": "minecraft_tool_timeout",
        "outcome_certainty": "unknown",
        "retry_safe": False,
        "message": "response timed out",
        "agent": "Alice",
        "tool": "post_place",
    }


def test_execution_group_waits_for_all_agents_and_fails_on_reflection():
    controller, task, agents = _controller_with_task(["Alice", "Bob"], required=2)
    agents[0].reflect_success = False
    group = _started_group(controller, task, agents, pending_agent="Bob")

    assert controller.finalize_execution_group(group) is False
    assert controller.task_manager.status_updates == []

    group.futures["Bob"].set_result(("done", "Bob detail"))
    assert controller.finalize_execution_group(group) is True
    _, status, feedback = controller.task_manager.status_updates[0]
    assert status == Task.failure
    assert feedback["agent_results"]["Alice"]["status"] == "failure"
    assert feedback["agent_results"]["Bob"]["status"] == "success"


def test_execution_group_does_not_reflect_explicit_agent_failure():
    controller, task, agents = _controller_with_task(["Alice"], required=1)
    failure_detail = {
        "final_answer": "Local model attempt budget exhausted.",
        "failure": {"reason": "model_attempt_budget_exhausted"},
    }
    agents[0].step_detail = failure_detail
    agents[0].reflect_success = True
    group = _started_group(controller, task, agents)

    assert controller.finalize_execution_group(group) is True

    assert agents[0].reflect_calls == 0
    assert controller.task_manager.status_updates == [
        (task.id, Task.failure, failure_detail)
    ]


def test_running_future_timeout_does_not_release_or_reassign_agent():
    controller, task, agents = _controller_with_task(["Alice", "Bob"], required=2)
    agents[1].cooperative_cancellation = True
    group = _started_group(controller, task, agents, pending_agent="Bob")
    group.started_at = time.time() - controller.max_task_time - 1
    group.futures["Bob"].set_running_or_notify_cancel()

    assert controller.finalize_execution_group(group) is False

    assert group.timeout_detected == {"Bob"}
    assert group.cancellation_requested == {"Bob"}
    assert group.cancellation_acknowledged == set()
    assert group.cancellation_forced == set()
    assert group.cancellation_tokens["Bob"].is_set()
    assert controller.assignment == {"Alice": task.id, "Bob": task.id}
    assert controller.task_manager.status_updates == []
    next_task = _task("Next task", ["Bob"], required=1)
    controller.task_list = [next_task]
    assert controller.assign_runnable_tasks() == 0


def test_cancellation_acknowledgement_releases_assignments_and_fails_once():
    controller, task, agents = _controller_with_task(["Alice", "Bob"], required=2)
    agents[1].cooperative_cancellation = True
    group = _started_group(controller, task, agents, pending_agent="Bob")
    timeout_at = group.started_at + controller.max_task_time + 1

    assert controller.finalize_execution_group(group, now=timeout_at) is False
    group.futures["Bob"].set_result(_cancelled_result())
    assert controller.finalize_execution_group(group, now=timeout_at + 0.1) is True

    assert group.cancellation_acknowledged == {"Bob"}
    assert controller.assignment == {}
    assert len(controller.task_manager.status_updates) == 1
    _, status, feedback = controller.task_manager.status_updates[0]
    assert status == Task.failure
    assert feedback["agent_results"]["Bob"] == {
        "status": "timeout",
        "error": f"Task {task.description} timeout for agent Bob",
        "cooperative_cancellation": True,
        "timeout_detected": True,
        "shutdown_escalated": False,
        "cancellation_requested": True,
            "cancellation_acknowledged": True,
            "cancellation_forced": False,
            "phase": "unknown",
        }
    assert controller.finalize_execution_group(group) is True
    assert len(controller.task_manager.status_updates) == 1


def test_non_cooperative_timeout_stops_controller_and_retains_assignment():
    controller, task, agents = _controller_with_task(["Alice"], required=1)
    agents[0].cooperative_cancellation = False
    controller.cancellation_grace_period = 0.5
    group = _started_group(controller, task, agents, pending_agent="Alice")
    group.futures["Alice"].set_running_or_notify_cancel()
    timeout_at = group.started_at + controller.max_task_time + 1

    assert controller.finalize_execution_group(group, now=timeout_at) is False
    with pytest.raises(ControllerShutdownError, match="remained active after timeout for Alice"):
        controller.finalize_execution_group(group, now=timeout_at + 0.5)

    assert controller.shutdown_event.is_set()
    assert group.cancellation_tokens == {}
    assert group.timeout_detected == {"Alice"}
    assert group.cancellation_requested == set()
    assert group.cancellation_forced == set()
    assert group.shutdown_escalated == {"Alice"}
    assert controller.assignment == {"Alice": task.id}
    assert controller.task_manager.status_updates == []


def test_timeout_boundary_is_inclusive():
    controller, task, agents = _controller_with_task(["Alice"], required=1)
    agents[0].cooperative_cancellation = True
    group = _started_group(controller, task, agents, pending_agent="Alice")
    group.futures["Alice"].set_running_or_notify_cancel()

    assert controller.finalize_execution_group(
        group, now=group.started_at + controller.max_task_time
    ) is False

    assert group.timeout_detected == {"Alice"}
    assert group.cancellation_requested == {"Alice"}


def test_completion_racing_with_timeout_snapshot_does_not_request_or_escalate():
    controller, task, agents = _controller_with_task(["Alice"], required=1)
    agents[0].cooperative_cancellation = True
    group = _started_group(controller, task, agents, pending_agent="Alice")
    racing_future = _CompletesAfterFirstPendingSnapshot(("done", "late detail"))
    group.futures["Alice"] = racing_future
    timeout_at = group.started_at + controller.max_task_time

    assert controller.finalize_execution_group(group, now=timeout_at) is True

    assert group.timeout_detected == {"Alice"}
    assert group.cancellation_requested == set()
    assert group.shutdown_escalated == set()
    assert not group.cancellation_tokens["Alice"].is_set()
    assert controller.shutdown_event.is_set() is False
    assert controller.task_manager.status_updates[0][1] == Task.failure


def test_completion_racing_with_grace_snapshot_does_not_escalate():
    controller, task, agents = _controller_with_task(["Alice"], required=1)
    controller.cancellation_grace_period = 0.5
    group = _started_group(controller, task, agents, pending_agent="Alice")
    racing_future = _CompletesAfterPendingSnapshots(("done", "late detail"), 4)
    group.futures["Alice"] = racing_future
    timeout_at = group.started_at + controller.max_task_time

    assert controller.finalize_execution_group(group, now=timeout_at) is False
    assert controller.finalize_execution_group(group, now=timeout_at + 0.5) is True

    assert group.timeout_detected == {"Alice"}
    assert group.cancellation_requested == {"Alice"}
    assert group.shutdown_escalated == set()
    assert controller.shutdown_event.is_set() is False


def test_execution_finishing_during_grace_terminalizes_timeout_without_escalation():
    controller, task, agents = _controller_with_task(["Alice"], required=1)
    agents[0].cooperative_cancellation = True
    group = _started_group(controller, task, agents, pending_agent="Alice")
    group.futures["Alice"].set_running_or_notify_cancel()
    timeout_at = group.started_at + controller.max_task_time

    assert controller.finalize_execution_group(group, now=timeout_at) is False
    group.futures["Alice"].set_result(("done", "finished after deadline"))
    assert controller.finalize_execution_group(group, now=timeout_at + 0.5) is True

    assert group.cancellation_requested == {"Alice"}
    assert group.cancellation_acknowledged == set()
    assert group.shutdown_escalated == set()
    assert controller.shutdown_event.is_set() is False
    assert controller.assignment == {}


def test_cancelled_reason_without_explicit_marker_is_not_acknowledgement():
    controller, task, agents = _controller_with_task(["Alice"], required=1)
    agents[0].cooperative_cancellation = True
    group = _started_group(controller, task, agents, pending_agent="Alice")
    group.futures["Alice"].set_running_or_notify_cancel()
    timeout_at = group.started_at + controller.max_task_time

    assert controller.finalize_execution_group(group, now=timeout_at) is False
    group.futures["Alice"].set_result(("cancelled", {
        "failure": {"reason": "cancelled"},
    }))
    assert controller.finalize_execution_group(group, now=timeout_at + 0.1) is True

    assert group.cancellation_requested == {"Alice"}
    assert group.cancellation_acknowledged == set()
    assert controller.task_manager.status_updates[0][2]["cancellation_acknowledged"] is False


def test_terminal_status_is_not_persisted_twice_when_post_processing_raises():
    controller, task, agents = _controller_with_task(["Alice"], required=1)
    group = _started_group(controller, task, agents)
    controller.task_manager.feedback_errors = [RuntimeError("feedback failed"), None]

    with pytest.raises(RuntimeError, match="feedback failed"):
        controller.finalize_execution_group(group)

    assert group.terminal_state_persisted is True
    assert group.post_processing_complete is False
    assert group.completed is False
    assert len(controller.task_manager.status_updates) == 1

    assert controller.finalize_execution_group(group) is True
    assert group.post_processing_complete is True
    assert len(controller.task_manager.status_updates) == 1


def test_timeout_acknowledgement_preserves_multi_agent_lifecycle_history():
    manager = TaskManager(silent=True)
    task = _task("Shared task", ["Alice", "Bob"], required=2)
    manager.set_task_list_from_decomposition([task])
    projected_task = manager.query_runnable_subtasks(["Alice", "Bob"])[0]
    manager.feedback_task = lambda _task: None
    controller, _, agents = _controller_with_task(["Alice", "Bob"], required=2)
    agents[1].cooperative_cancellation = True
    controller.task_manager = manager
    controller.task_list = [projected_task]
    controller.execute_assignments([{
        "task_instance": projected_task,
        "agent_instances": agents,
    }])
    group = _started_group(
        controller, projected_task, agents, pending_agent="Bob", enqueue_assignment=False
    )
    timeout_at = group.started_at + controller.max_task_time + 1

    assert controller.finalize_execution_group(group, now=timeout_at) is False
    assert group.timeout_detected == {"Bob"}
    assert group.cancellation_requested == {"Bob"}
    assert controller.assignment == {"Alice": projected_task.id, "Bob": projected_task.id}
    running_node = manager.runtime_task_store.snapshot()["nodes"][0]
    assert running_node["lifecycle"]["active_agents"] == ["Alice", "Bob"]
    group.futures["Bob"].set_result(_cancelled_result())
    assert controller.finalize_execution_group(group, now=timeout_at + 0.1) is True

    node = manager.runtime_task_store.snapshot()["nodes"][0]
    assert node["lifecycle"]["status"] == Task.failure
    assert node["lifecycle"]["active_agents"] == []
    assert node["lifecycle"]["last_assigned_agents"] == ["Alice", "Bob"]


def test_execution_group_terminal_transition_preserves_assignment_history():
    manager = TaskManager(silent=True)
    task = _task("Shared task", ["Alice", "Bob"], required=2)
    manager.set_task_list_from_decomposition([task])
    projected_task = manager.query_runnable_subtasks(["Alice", "Bob"])[0]
    manager.feedback_task = lambda _task: None
    controller, _, agents = _controller_with_task(["Alice", "Bob"], required=2)
    controller.task_manager = manager
    controller.task_list = [projected_task]

    controller.execute_assignments([{
        "task_instance": projected_task,
        "agent_instances": agents,
    }])
    running_node = manager.runtime_task_store.snapshot()["nodes"][0]
    assert running_node["lifecycle"]["active_agents"] == ["Alice", "Bob"]
    group = _started_group(controller, projected_task, agents, enqueue_assignment=False)
    controller.finalize_execution_group(group)

    node = manager.runtime_task_store.snapshot()["nodes"][0]
    assert node["lifecycle"]["status"] == Task.success
    assert node["lifecycle"]["active_agents"] == []
    assert node["lifecycle"]["last_assigned_agents"] == ["Alice", "Bob"]


class _AgentStub:
    def __init__(self, name):
        self.name = name
        self.step_error = None
        self.step_detail = f"{name} detail"
        self.reflect_success = True
        self.reflect_calls = 0
        self.cooperative_cancellation = True

    def supports_cooperative_cancellation(self):
        return self.cooperative_cancellation

    def step(self, _task, cancellation_token=None):
        if self.step_error is not None:
            raise self.step_error
        return "done", self.step_detail

    def reflect(self, _task, _detail):
        self.reflect_calls += 1
        return self.reflect_success


class _ExecutorStub:
    def __init__(self, pending_agent=None):
        self.pending_agent = pending_agent
        self.submitted_agents = []

    def submit(self, fn, task, **kwargs):
        agent = fn.__self__
        self.submitted_agents.append(agent.name)
        future = Future()
        if agent.name == self.pending_agent:
            return future
        try:
            future.set_result(fn(task, **kwargs))
        except Exception as exc:
            future.set_exception(exc)
        return future


class _BlockingFirstSubmitExecutor(_ExecutorStub):
    def __init__(self):
        super().__init__()
        self.first_submit_started = threading.Event()
        self.release_first_submit = threading.Event()

    def submit(self, fn, task, **kwargs):
        if not self.submitted_agents:
            self.first_submit_started.set()
            assert self.release_first_submit.wait(1)
        return super().submit(fn, task, **kwargs)


class _TaskManagerStub:
    def __init__(self, task):
        self.running_updates = []
        self.status_updates = []
        self.graph = SimpleNamespace(vertex=[task])
        self.feedback_errors = []

    def mark_task_running(self, task, agent_names):
        self.running_updates.append((task.id, list(agent_names)))

    def mark_task_status(self, task_id, status, feedback=None):
        self.status_updates.append((task_id, status, feedback))

    def feedback_task(self, _task):
        if self.feedback_errors:
            error = self.feedback_errors.pop(0)
            if error is not None:
                raise error
        return None


def _controller_with_task(agent_names, required):
    task = _task("Shared task", agent_names, required)
    agents = [_AgentStub(name) for name in agent_names]
    controller = object.__new__(GlobalController)
    controller.agent_list = agents
    controller.assignment = {}
    controller.task_list = [task]
    controller.task_queue = []
    controller.result_queue = []
    controller.task_list_lock = threading.Lock()
    controller.result_list_lock = threading.Lock()
    controller._execution_state_lock = threading.RLock()
    controller._tool_action_condition = threading.Condition(controller._execution_state_lock)
    controller._active_tool_actions = 0
    controller._judger_terminal_pending = False
    controller._judger_terminal_observed = False
    controller._judger_terminal_detected_at = None
    controller._tool_drain_timed_out = False
    controller.shutdown_event = threading.Event()
    controller.task_manager = _TaskManagerStub(task)
    controller.logger = logging.getLogger("test-controller-multi-agent")
    controller.max_task_time = 30
    controller.shutdown_grace_period = 1
    controller.cancellation_grace_period = 1
    return controller, task, agents


def _terminal_env():
    return SimpleNamespace(
        attempt_id="attempt-a",
        task_name="runtime-task-a",
        is_task_complete=lambda: True,
        get_score=lambda: {
            "attempt_id": "attempt-a",
            "task_name": "runtime-task-a",
            "status": "success",
            "score": 100,
        },
    )


def _started_group(controller, task, agents, pending_agent=None, enqueue_assignment=True):
    if enqueue_assignment:
        controller.execute_assignments([{
            "task_instance": task,
            "agent_instances": agents,
        }])
        group = controller.task_queue.pop(0)
    else:
        group = controller.task_queue.pop(0)
    controller.executor = _ExecutorStub(pending_agent=pending_agent)
    controller.start_execution_group(group)
    return controller.result_queue.pop(0)


def _task(description, candidates, required):
    task = Task(description, {})
    task.candidate_list = list(candidates)
    task.number = required
    task.available = True
    task.status = Task.unknown
    return task


def _cancelled_result():
    return ({"status": False}, {
        "failure": {
            "reason": "cancelled",
            "cancellation_acknowledged": True,
        },
    })


class _CompletesAfterPendingSnapshots(Future):
    def __init__(self, result, pending_snapshots):
        super().__init__()
        self.result_after_snapshot = result
        self.pending_snapshots = pending_snapshots
        self.snapshot_count = 0

    def done(self):
        self.snapshot_count += 1
        if self.snapshot_count == self.pending_snapshots:
            self.set_result(self.result_after_snapshot)
            return False
        if self.snapshot_count < self.pending_snapshots:
            return False
        return super().done()


class _CompletesAfterFirstPendingSnapshot(_CompletesAfterPendingSnapshots):
    def __init__(self, result):
        super().__init__(result, 1)
