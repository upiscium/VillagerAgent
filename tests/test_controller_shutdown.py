import logging
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from pipeline.controller_tiny import (
    ControllerShutdownError,
    GlobalController,
    JudgedEvidenceConsistencyError,
    JudgedTaskFailure,
    TaskExecutionGroup,
    current_execution_diagnostic_identity,
)
from env.minecraft_client import MinecraftActionLogError, MinecraftToolTimeoutError
from env.minecraft_client import AgentExecutionCancelledError, ToolActionBlockedError
from pipeline.runtime_events import InMemoryRuntimeEventRecorder
from pipeline.agent import BaseAgent
from pipeline.task_manager import TaskManager
from type_define.graph import Task


@pytest.mark.parametrize("failing_entrypoint", [
    "execute_tasks",
    "worker",
    "process_completed_tasks",
])
def test_thread_failure_stops_controller_and_preserves_first_exception(failing_entrypoint):
    controller, checkpoints, sink = _controller()
    failure = RuntimeError(f"{failing_entrypoint} failed")

    def fail():
        raise failure

    def wait_for_shutdown():
        assert controller.shutdown_event.wait(2)

    for entrypoint in ("execute_tasks", "worker", "process_completed_tasks"):
        setattr(controller, entrypoint, fail if entrypoint == failing_entrypoint else wait_for_shutdown)

    with pytest.raises(RuntimeError) as raised:
        controller.run()

    assert raised.value is failure
    assert all(not thread.is_alive() for thread in controller._controller_threads)
    assert all(not thread.is_alive() for thread in controller.executor._threads)
    assert checkpoints == ["checkpoint"]
    assert [event["event_type"] for event in sink.events] == ["run_failed"]
    assert sink.events[0]["payload"]["thread"] == failing_entrypoint
    assert "raise failure" in sink.events[0]["payload"]["traceback"]


def test_normal_completion_stops_all_threads_executor_and_checkpoints():
    controller, checkpoints, sink = _controller()

    def complete():
        controller._request_shutdown()

    def wait_for_shutdown():
        assert controller.shutdown_event.wait(2)

    controller.execute_tasks = complete
    controller.worker = wait_for_shutdown
    controller.process_completed_tasks = wait_for_shutdown

    controller.run()

    assert all(not thread.is_alive() for thread in controller._controller_threads)
    assert all(not thread.is_alive() for thread in controller.executor._threads)
    assert checkpoints == ["checkpoint"]
    assert [event["event_type"] for event in sink.events] == ["run_completed"]


def test_shutdown_cancels_active_movement_before_executor_join():
    controller, checkpoints, sink = _controller()
    release = threading.Event()
    running = threading.Event()
    order = []

    def active_movement():
        running.set()
        assert release.wait(1)
        order.append("movement_terminal")

    controller.executor.submit(active_movement)
    assert running.wait(1)

    def cancel_active_movements(*, reason, timeout_seconds):
        order.append(f"cancel:{reason}:{timeout_seconds}")
        release.set()
        return {"terminal": True, "actors": {"Alice": {"terminal": True}}}

    controller.env = SimpleNamespace(cancel_active_movements=cancel_active_movements)
    controller.execute_tasks = controller._request_shutdown
    controller.worker = controller.shutdown_event.wait
    controller.process_completed_tasks = controller.shutdown_event.wait

    controller.run()

    assert order == ["cancel:controller_shutdown:0.1", "movement_terminal"]
    assert controller.shutdown_context["movement_cancellation"]["terminal"] is True
    assert controller.shutdown_complete is True
    assert checkpoints == ["checkpoint"]
    assert [event["event_type"] for event in sink.events] == ["run_completed"]


def test_nonterminal_movement_cleanup_prevents_successful_shutdown():
    controller, checkpoints, sink = _controller()
    controller.env = SimpleNamespace(cancel_active_movements=lambda **_kwargs: {
        "terminal": False, "actors": {"Alice": {"terminal": False}},
    })
    controller.execute_tasks = controller._request_shutdown
    controller.worker = controller.shutdown_event.wait
    controller.process_completed_tasks = controller.shutdown_event.wait

    with pytest.raises(ControllerShutdownError, match="shutdown incomplete"):
        controller.run()

    assert controller.shutdown_complete is False
    assert checkpoints == ["checkpoint"]
    assert [event["event_type"] for event in sink.events] == ["run_failed"]


def test_should_shutdown_is_a_side_effect_free_query():
    controller, _, _ = _controller()
    controller.env = SimpleNamespace(
        is_task_complete=lambda: True,
        stop=lambda: (_ for _ in ()).throw(AssertionError("must not stop environment")),
    )

    assert controller.should_shutdown() is False
    assert controller._judger_terminal_observed is False


def test_nonblocking_execution_snapshot_callback_freezes_stable_ids():
    controller, _, _ = _controller()
    task = Task("snapshot", {})
    task.status = Task.running
    release = threading.Event()
    agent = SimpleNamespace(name="Alice", step=lambda _task: release.wait(2))
    group = TaskExecutionGroup(task=task, agents=[agent])
    controller.start_execution_group(group)
    received = []
    assert controller.with_execution_lock_nonblocking(received.append) is True
    assert received[0]["items"][0]["task_id"] == str(task.id)
    assert received[0]["items"][0]["actor_id"] == "Alice"
    release.set()
    controller.executor.shutdown(wait=True)


def test_execution_snapshot_uses_cutoff_and_atomically_seals_admission():
    controller, _, _ = _controller()
    task = Task("snapshot", {})
    task.id = 0
    task.status = Task.running
    release = threading.Event()
    agent = SimpleNamespace(name="Alice", step=lambda _task: release.wait(2))
    group = TaskExecutionGroup(task=task, agents=[agent])
    controller.start_execution_group(group)
    group.execution_completion_markers["Alice"] = {
        "event": "future_completed",
        "execution_id": group.execution_ids["Alice"],
        "monotonic_ns": 200,
    }
    received = []

    assert controller.with_execution_lock_nonblocking(
        received.append, cutoff_monotonic_ns=100, seal_admission=True,
    ) is True
    assert received[0]["items"][0]["task_id"] == "0"

    later = TaskExecutionGroup(
        task=Task("later", {}),
        agents=[SimpleNamespace(name="Bob", step=lambda _task: None)],
    )
    with pytest.raises(ControllerShutdownError, match="Cannot start execution"):
        controller.start_execution_group(later)
    release.set()
    controller.executor.shutdown(wait=True)


def test_execution_measurement_snapshot_fails_closed_without_waiting_for_lock():
    controller, _, _ = _controller()
    locked = threading.Event()
    release = threading.Event()
    def holder():
        with controller._execution_state_lock:
            locked.set()
            release.wait(2)
    thread = threading.Thread(target=holder)
    thread.start()
    assert locked.wait(1)
    received = []
    started = time.monotonic()
    assert controller.with_execution_lock_nonblocking(
        received.append, seal_admission=True,
    ) is False
    assert time.monotonic() - started < 0.1
    assert received[0]["errors"] == ["execution state lock unavailable"]
    assert controller._execution_admission_closed() is True
    release.set()
    thread.join(1)


def test_judged_completion_persists_canonical_success_before_shutdown():
    controller, _, sink = _controller()
    task = Task("Judged task", {})
    task.candidate_list = ["Alice"]
    task.number = 1
    manager = TaskManager(silent=True, event_sink=sink)
    manager.set_task_list_from_decomposition([task])
    projected_task = manager.query_runnable_subtasks(["Alice"])[0]
    manager.mark_task_running(projected_task, ["Alice"])
    sink.events.clear()
    snapshots = []
    manager.runtime_checkpoint = lambda: snapshots.append(manager.runtime_task_store.snapshot())
    controller.task_manager = manager
    agent = SimpleNamespace(name="Alice")
    future = controller.executor.submit(lambda: ("done", "detail"))
    future.result(timeout=1)
    group = TaskExecutionGroup(
        task=projected_task,
        agents=[agent],
        futures={"Alice": future},
        started_at=time.time(),
        submission_complete=True,
    )
    controller.result_queue.append(group)
    controller.assignment["Alice"] = projected_task.id
    stopped = []
    controller.env = SimpleNamespace(
        attempt_id="attempt-a",
        task_name="runtime-task-a",
        is_task_complete=lambda: True,
        get_score=lambda: {
            "attempt_id": "attempt-a",
            "task_name": "runtime-task-a",
            "status": "success",
            "score": 100,
            "progress": 100,
        },
        stop=lambda: stopped.append(True),
        agents_ping=lambda: {"status": True},
    )
    controller.query_interval = 0
    controller.execute_tasks = controller.shutdown_event.wait
    controller.worker = controller.shutdown_event.wait

    controller.run()

    assert group.completed is True
    assert group.terminal_state_persisted is True
    assert controller.assignment == {}
    final_snapshot = snapshots[-1]
    assert final_snapshot["summary"]["terminal_state"] == "success"
    assert final_snapshot["nodes"][0]["lifecycle"]["status"] == Task.success
    assert final_snapshot["nodes"][0]["lifecycle"]["active_agents"] == []
    assert final_snapshot["nodes"][0]["content"]["reflect"]["terminal_source"] == "external_judger"
    assert controller.shutdown_complete is True
    assert stopped == [True]
    assert [event["event_type"] for event in sink.events] == [
        "task_status_changed",
        "run_completed",
    ]


def test_judger_failure_persists_canonical_failure_once():
    controller, manager, sink, _ = _judged_reconciliation_controller(status="failure")

    assert controller.observe_judger_terminal() is True
    assert controller.reconcile_judger_terminal() is True
    assert controller.reconcile_judger_terminal() is True

    snapshot = manager.runtime_task_store.snapshot()
    assert snapshot["summary"]["terminal_state"] == "failure"
    assert snapshot["nodes"][0]["lifecycle"]["status"] == Task.failure
    status_events = [event for event in sink.events if event["event_type"] == "task_status_changed"]
    assert len(status_events) == 1
    assert controller.shutdown_event.is_set() is True
    assert controller._first_failure[2]["thread"] == "external_judger"
    error = controller._first_failure[0]
    assert isinstance(error, JudgedTaskFailure)
    assert "status=failure" in str(error)
    assert "diagnostics=judged_terminal_diagnostics.json" in str(error)


@pytest.mark.parametrize(
    "error",
    [
        MinecraftActionLogError("action evidence unavailable", agent="Alice"),
        MinecraftToolTimeoutError("tool response timed out", agent="Alice"),
    ],
)
def test_judger_success_rejects_retry_unsafe_future_exception(error):
    controller, manager, _, task = _judged_reconciliation_controller()
    future = Future()
    future.set_exception(error)
    group = TaskExecutionGroup(
        task=task,
        agents=[SimpleNamespace(name="Alice")],
        futures={"Alice": future},
        submission_complete=True,
    )
    controller.result_queue.append(group)
    controller.assignment["Alice"] = task.id
    controller.observe_judger_terminal()

    with pytest.raises(JudgedEvidenceConsistencyError) as raised:
        controller.reconcile_judger_terminal()

    snapshot = manager.runtime_task_store.snapshot()
    assert snapshot["summary"]["terminal_state"] == "failure"
    assert snapshot["nodes"][0]["lifecycle"]["status"] == Task.failure
    feedback = snapshot["nodes"][0]["content"]["reflect"]
    assert feedback["judger_status"] == "success"
    assert feedback["evidence_consistency"] == "failed"
    assert feedback["agent_failures"]["Alice"]["retry_safe"] is False
    assert raised.value.agent_failures == feedback["agent_failures"]


@pytest.mark.parametrize("future_state", ["cancelled", "malformed"])
def test_judger_success_rejects_invalid_completed_future(future_state):
    controller, manager, _, task = _judged_reconciliation_controller()
    future = Future()
    if future_state == "cancelled":
        future.cancel()
    else:
        future.set_result("not a step result")
    group = TaskExecutionGroup(
        task=task,
        agents=[SimpleNamespace(name="Alice")],
        futures={"Alice": future},
        submission_complete=True,
    )
    controller.result_queue.append(group)
    controller.assignment["Alice"] = task.id
    controller.observe_judger_terminal()

    with pytest.raises(JudgedEvidenceConsistencyError):
        controller.reconcile_judger_terminal()

    assert manager.runtime_task_store.snapshot()["summary"]["terminal_state"] == "failure"


def test_judger_success_accepts_valid_completed_future():
    controller, manager, _, task = _judged_reconciliation_controller()
    future = Future()
    future.set_result(("done", {"action_list": [{"action": "navigateTo"}]}))
    group = TaskExecutionGroup(
        task=task,
        agents=[SimpleNamespace(name="Alice")],
        futures={"Alice": future},
        submission_complete=True,
    )
    controller.result_queue.append(group)
    controller.assignment["Alice"] = task.id
    controller.observe_judger_terminal()

    assert controller.reconcile_judger_terminal() is True

    snapshot = manager.runtime_task_store.snapshot()
    assert snapshot["summary"]["terminal_state"] == "success"
    feedback = snapshot["nodes"][0]["content"]["reflect"]
    assert feedback["agent_execution"]["valid"] is True


def test_judger_success_accepts_tool_blocked_by_terminal_barrier():
    controller, manager, _, task = _judged_reconciliation_controller()
    future = Future()
    future.set_exception(
        ToolActionBlockedError(
            "Cannot start Minecraft tool action after judger terminal detection"
        )
    )
    group = TaskExecutionGroup(
        task=task,
        agents=[SimpleNamespace(name="Alice")],
        futures={"Alice": future},
        submission_complete=True,
    )
    controller.result_queue.append(group)
    controller.assignment["Alice"] = task.id
    controller.observe_judger_terminal()

    assert controller.reconcile_judger_terminal() is True

    snapshot = manager.runtime_task_store.snapshot()
    assert snapshot["summary"]["terminal_state"] == "success"
    feedback = snapshot["nodes"][0]["content"]["reflect"]
    assert feedback["agent_execution"]["valid"] is True
    assert feedback["agent_execution"]["agent_results"]["Alice"]["status"] == "terminal_blocked"


def test_judger_payload_attempt_mismatch_is_rejected():
    controller, manager, _, _ = _judged_reconciliation_controller()
    controller.env.get_score = lambda: {
        "attempt_id": "stale-attempt",
        "task_name": "runtime-task-a",
        "status": "success",
        "score": 100,
    }

    with pytest.raises(ControllerShutdownError, match="attempt mismatch"):
        controller.observe_judger_terminal()

    assert manager.runtime_task_store.snapshot()["summary"]["terminal_state"] == "running"


def test_worker_does_not_pop_queue_after_terminal_observation():
    controller, _, _ = _controller()
    task = Task("Queued after judged completion", {})
    group = TaskExecutionGroup(task=task, agents=[SimpleNamespace(name="Alice")])
    controller.task_queue.append(group)
    controller.query_interval = 0.01
    controller.env = SimpleNamespace(
        attempt_id="attempt-a",
        task_name="runtime-task-a",
        is_task_complete=lambda: True,
        get_score=lambda: {
            "attempt_id": "attempt-a",
            "task_name": "runtime-task-a",
            "status": "success",
            "score": 100,
        },
        agents_ping=lambda: (_ for _ in ()).throw(
            AssertionError("worker must not continue after terminal observation")
        ),
    )
    worker = threading.Thread(target=controller.worker)

    worker.start()
    deadline = time.monotonic() + 1
    while not controller._judger_terminal_pending and time.monotonic() < deadline:
        time.sleep(0.01)
    controller._request_shutdown()
    worker.join(1)
    controller.executor.shutdown(wait=True)

    assert not worker.is_alive()
    assert controller.task_queue == [group]
    assert controller.result_queue == []


def test_terminal_detection_closes_tool_barrier_before_waiting_for_active_action():
    controller, _, _, _ = _judged_reconciliation_controller()
    controller._begin_tool_action()
    observation_result = []
    observation = threading.Thread(
        target=lambda: observation_result.append(controller.observe_judger_terminal())
    )

    observation.start()
    deadline = time.monotonic() + 1
    while not controller._judger_terminal_pending and time.monotonic() < deadline:
        time.sleep(0.01)

    assert controller._judger_terminal_pending is True
    assert controller._judger_terminal_observed is False
    observation.join(1)
    assert not observation.is_alive()
    assert observation_result == [True]
    with pytest.raises(ToolActionBlockedError, match="after judger terminal"):
        controller._begin_tool_action()

    controller._end_tool_action()
    assert controller.reconcile_judger_terminal() is True
    assert controller._judger_terminal_observed is True


def test_terminal_pending_blocks_assignment_queue_handoff_and_submission():
    controller, _, _, task = _judged_reconciliation_controller()
    agent = SimpleNamespace(name="Alice", supports_cooperative_cancellation=lambda: False)
    queued = TaskExecutionGroup(task=task, agents=[agent])
    controller.task_queue.append(queued)
    controller.observe_judger_terminal()

    assert controller.execute_assignments([{
        "task_instance": task,
        "agent_instances": [agent],
    }]) == 0
    assert controller._take_and_start_next_execution_group() is False
    with pytest.raises(ControllerShutdownError, match="after judger terminal"):
        controller.start_execution_group(queued)

    assert controller.task_queue == [queued]
    assert controller.result_queue == []
    assert queued.futures == {}


def test_active_tool_drain_timeout_does_not_publish_judged_success():
    controller, manager, _, task = _judged_reconciliation_controller()
    controller.assignment["Alice"] = task.id
    controller.judger_tool_drain_grace_period = 0
    controller._begin_tool_action()
    controller.observe_judger_terminal()

    try:
        with pytest.raises(ControllerShutdownError, match="tool action.*remained active"):
            controller.reconcile_judger_terminal()
    finally:
        controller._end_tool_action()

    snapshot = manager.runtime_task_store.snapshot()
    assert snapshot["summary"]["terminal_state"] == "running"
    assert snapshot["nodes"][0]["lifecycle"]["active_agents"] == ["Alice"]
    assert controller.assignment == {"Alice": task.id}
    assert controller._terminal_barrier_context() == {
        "pending": True,
        "observed": False,
        "detected_at": controller._judger_terminal_detected_at,
        "active_tool_actions": 0,
        "tool_drain_timed_out": True,
    }


@pytest.mark.parametrize("task_count", [0, 2])
def test_judger_terminal_requires_exactly_one_running_task(task_count):
    controller, _, _, _ = _judged_reconciliation_controller(task_count=task_count)
    controller.observe_judger_terminal()

    with pytest.raises(ControllerShutdownError, match=f"found {task_count}"):
        controller.reconcile_judger_terminal()


def test_judger_success_does_not_publish_while_future_can_mutate_environment():
    release = threading.Event()
    running = threading.Event()

    def active_step():
        running.set()
        release.wait()
        return "done", "detail"

    controller, manager, _, task = _judged_reconciliation_controller()
    future = controller.executor.submit(active_step)
    assert running.wait(1)
    group = TaskExecutionGroup(
        task=task,
        agents=[SimpleNamespace(name="Alice")],
        futures={"Alice": future},
        started_at=time.time(),
        submission_complete=True,
    )
    controller.result_queue.append(group)
    controller.assignment["Alice"] = task.id
    controller.shutdown_grace_period = 0
    controller.cancellation_grace_period = 0
    controller.observe_judger_terminal()

    try:
        with pytest.raises(ControllerShutdownError, match="remained active"):
            controller.reconcile_judger_terminal()
    finally:
        release.set()
        future.result(timeout=1)
        controller.executor.shutdown(wait=True)

    snapshot = manager.runtime_task_store.snapshot()
    assert snapshot["nodes"][0]["lifecycle"]["status"] == Task.running
    assert snapshot["nodes"][0]["lifecycle"]["active_agents"] == ["Alice"]
    assert controller.shutdown_event.is_set() is False


def test_judger_terminal_uses_dedicated_natural_drain_grace():
    release = threading.Event()
    running = threading.Event()

    def active_step():
        running.set()
        release.wait()
        return "done", "detail"

    controller, _, _, task = _judged_reconciliation_controller()
    future = controller.executor.submit(active_step)
    assert running.wait(1)
    group = TaskExecutionGroup(
        task=task,
        agents=[SimpleNamespace(name="Alice")],
        futures={"Alice": future},
        started_at=time.time(),
        submission_complete=True,
    )
    controller.result_queue.append(group)
    controller.assignment["Alice"] = task.id
    controller.shutdown_grace_period = 0
    controller.judger_drain_grace_period = 1
    controller.observe_judger_terminal()

    try:
        assert controller.reconcile_judger_terminal() is False
        assert future.running() is True
    finally:
        release.set()
        future.result(timeout=1)
        controller.executor.shutdown(wait=True)


def test_non_cooperative_controller_thread_has_bounded_incomplete_shutdown():
    controller, _, sink = _controller()
    controller.shutdown_grace_period = 0.05
    release = threading.Event()
    failure = RuntimeError("ranking failed")

    def fail():
        raise failure

    controller.execute_tasks = fail
    controller.worker = release.wait
    controller.process_completed_tasks = controller.shutdown_event.wait

    started_at = time.monotonic()
    try:
        with pytest.raises(RuntimeError) as raised:
            controller.run()
    finally:
        release.set()
        for thread in controller._controller_threads:
            thread.join(1)

    assert raised.value is failure
    assert time.monotonic() - started_at < 1
    assert failure.controller_shutdown_context["shutdown_complete"] is False
    assert sink.events[0]["payload"]["shutdown_complete"] is False
    assert "controller-worker" in sink.events[0]["payload"]["live_threads"]


def test_active_future_is_interrupted_without_releasing_agent_for_reuse():
    controller, checkpoints, sink = _controller()
    controller.shutdown_grace_period = 0.05
    release = threading.Event()
    running = threading.Event()
    task = Task("Active task", {})
    task.status = Task.running
    agent = SimpleNamespace(name="Alice")

    def active_step():
        running.set()
        release.wait()

    future = controller.executor.submit(active_step)
    assert running.wait(1)
    group = TaskExecutionGroup(
        task=task,
        agents=[agent],
        futures={"Alice": future},
        started_at=time.time(),
        submission_complete=True,
    )
    controller.result_queue.append(group)
    controller.assignment["Alice"] = task.id
    failure = RuntimeError("controller failed")
    controller.execute_tasks = lambda: (_ for _ in ()).throw(failure)
    controller.worker = controller.shutdown_event.wait
    controller.process_completed_tasks = controller.shutdown_event.wait

    try:
        with pytest.raises(RuntimeError) as raised:
            controller.run()
    finally:
        release.set()
        for thread in controller.executor._threads:
            thread.join(1)

    assert raised.value is failure
    assert controller.assignment == {"Alice": task.id}
    assert controller.task_manager.status_updates == [(task.id, Task.failure, {
        "reason": "controller_shutdown",
        "execution_may_still_be_active": True,
        "assigned_agents": ["Alice"],
        "submitted_agents": ["Alice"],
        "active_agents": ["Alice"],
        "unsubmitted_agents": [],
        "submission_complete": True,
        "agent_reuse_blocked": True,
        "requires_agent_reconciliation": True,
        "cancellation_requested": [],
        "cancellation_acknowledged": [],
        "cancellation_phases": {},
        "blocking_operation_termination": "unconfirmed",
    })]
    next_task = Task("Must not be reassigned", {})
    next_task.candidate_list = ["Alice"]
    controller.agent_list = [agent]
    controller.task_list = [next_task]
    assert controller.assign_runnable_tasks() == 0
    assert controller.result_queue == [group]
    assert checkpoints == ["checkpoint"]
    assert sink.events[0]["payload"]["shutdown_complete"] is False
    assert sink.events[0]["payload"]["active_task_ids"] == [task.id]


def test_shutdown_cancels_two_cooperative_agents_once_and_preserves_running_task_state():
    controller, checkpoints, sink = _controller()
    old_executor = controller.executor
    old_executor.shutdown(wait=True)
    controller.executor = ThreadPoolExecutor(max_workers=2)
    task = Task("Two-agent cancellation", {})
    task.candidate_list = ["Alice", "Bob"]
    task.number = 2
    agents = []

    class CooperativeAgent:
        def __init__(self, name):
            self.name = name

        def supports_cooperative_cancellation(self):
            return True

        def step(self, _task, cancellation_token=None, phase_callback=None):
            cancellation_token.wait(1)
            return {"status": False}, {
                "failure": {
                    "reason": "cancelled",
                    "cancellation_acknowledged": True,
                },
            }

    agents[:] = [CooperativeAgent("Alice"), CooperativeAgent("Bob")]
    task.status = Task.running
    controller.agent_list = agents
    controller.task_list = [task]
    controller.assignment = {agent.name: task.id for agent in agents}
    group = TaskExecutionGroup(task=task, agents=agents)
    controller.start_execution_group(group)

    controller.execute_tasks = controller._request_shutdown
    controller.worker = controller.shutdown_event.wait
    controller.process_completed_tasks = controller.shutdown_event.wait
    controller.run()

    assert all(not thread.is_alive() for thread in controller.executor._threads)
    assert controller.shutdown_complete is True
    assert group.cancellation_requested == {"Alice", "Bob"}
    assert group.cancellation_acknowledged == {"Alice", "Bob"}
    assert group.task.status == Task.running
    assert len(controller.task_manager.status_updates) == 1
    assert controller.task_manager.status_updates[0][1] == Task.running
    assert controller.result_queue == []
    assert checkpoints == ["checkpoint"]
    assert [event["event_type"] for event in sink.events] == ["run_completed"]


def test_queued_group_is_checkpointed_as_interrupted():
    controller, checkpoints, _ = _controller()
    task = Task("Queued task", {})
    task.status = Task.running
    controller.task_queue.append(TaskExecutionGroup(
        task=task,
        agents=[SimpleNamespace(name="Alice")],
    ))
    failure = RuntimeError("task ranking failed")
    controller.execute_tasks = lambda: (_ for _ in ()).throw(failure)
    controller.worker = controller.shutdown_event.wait
    controller.process_completed_tasks = controller.shutdown_event.wait

    with pytest.raises(RuntimeError) as raised:
        controller.run()

    assert raised.value is failure
    assert controller.task_manager.status_updates == [(task.id, Task.failure, {
        "reason": "controller_shutdown",
        "execution_may_still_be_active": False,
        "assigned_agents": ["Alice"],
        "submitted_agents": [],
        "active_agents": [],
        "unsubmitted_agents": ["Alice"],
        "submission_complete": False,
        "agent_reuse_blocked": True,
        "requires_agent_reconciliation": True,
        "cancellation_requested": [],
        "cancellation_acknowledged": [],
        "cancellation_phases": {},
        "blocking_operation_termination": "not_active",
    })]
    assert len(controller.task_queue) == 1
    assert checkpoints == ["checkpoint"]


def test_offline_agent_is_reported_as_run_failure():
    controller, _, sink = _controller()
    controller.env = SimpleNamespace(agents_ping=lambda: {"status": False})
    controller.execute_tasks = controller.shutdown_event.wait
    controller.process_completed_tasks = controller.shutdown_event.wait

    with pytest.raises(ControllerShutdownError, match="Some agents are offline"):
        controller.run()

    assert [event["event_type"] for event in sink.events] == ["run_failed"]
    assert sink.events[0]["payload"]["error"] == "Some agents are offline"


def test_result_processor_preserves_unprocessed_groups_on_shutdown():
    controller, _, _ = _controller()
    controller.env = SimpleNamespace(agents_ping=lambda: {"status": True})
    controller.query_interval = 0
    groups = [
        TaskExecutionGroup(Task(f"Task {index}", {}), [])
        for index in range(3)
    ]
    controller.result_queue = list(groups)

    def finalize(group):
        assert group is groups[0]
        controller._request_shutdown()
        return True

    controller.finalize_execution_group = finalize

    controller.process_completed_tasks()

    assert controller.result_queue == groups[1:]
    controller.executor.shutdown(wait=True)


def test_shutdown_finalization_never_reflects_completed_future():
    controller, _, _ = _controller()
    task = Task("Completed but unprocessed", {})
    future = controller.executor.submit(lambda: ("done", "detail"))
    future.result(timeout=1)
    reflected = []
    agent = SimpleNamespace(
        name="Alice",
        reflect=lambda *_args: reflected.append(True),
    )
    controller.result_queue.append(TaskExecutionGroup(
        task=task,
        agents=[agent],
        futures={"Alice": future},
        started_at=time.time(),
    ))
    failure = RuntimeError("controller failed")
    controller.execute_tasks = lambda: (_ for _ in ()).throw(failure)
    controller.worker = controller.shutdown_event.wait
    controller.process_completed_tasks = controller.shutdown_event.wait

    with pytest.raises(RuntimeError):
        controller.run()

    assert reflected == []
    assert controller.task_manager.status_updates[0][2]["reason"] == "controller_shutdown"


def test_group_remains_queued_when_interrupted_marking_fails():
    controller, _, sink = _controller()
    task = Task("Preserve me", {})
    group = TaskExecutionGroup(task, [SimpleNamespace(name="Alice")])
    controller.task_queue.append(group)
    controller.task_manager.mark_error = RuntimeError("task store unavailable")
    failure = RuntimeError("controller failed")
    controller.execute_tasks = lambda: (_ for _ in ()).throw(failure)
    controller.worker = controller.shutdown_event.wait
    controller.process_completed_tasks = controller.shutdown_event.wait

    with pytest.raises(RuntimeError) as raised:
        controller.run()

    assert raised.value is failure
    assert controller.task_queue == [group]
    assert group.completed is False
    assert sink.events[0]["payload"]["undrained_queues"] == ["task_queue"]


def test_group_remains_queued_when_final_checkpoint_fails():
    controller, _, sink = _controller()
    task = Task("Checkpoint me", {})
    group = TaskExecutionGroup(task, [SimpleNamespace(name="Alice")])
    controller.task_queue.append(group)
    controller.task_manager.checkpoint_error = RuntimeError("checkpoint unavailable")
    failure = RuntimeError("controller failed")
    controller.execute_tasks = lambda: (_ for _ in ()).throw(failure)
    controller.worker = controller.shutdown_event.wait
    controller.process_completed_tasks = controller.shutdown_event.wait

    with pytest.raises(RuntimeError) as raised:
        controller.run()

    assert raised.value is failure
    assert controller.task_queue == [group]
    assert sink.events[0]["payload"]["checkpoint_error"] == {
        "error": "checkpoint unavailable",
        "error_type": "RuntimeError",
    }


def test_worker_retains_group_when_shutdown_interrupts_second_submit():
    controller, _, sink = _controller()
    task = Task("Shared task", {})
    task.status = Task.running
    agents = [SimpleNamespace(name=name, step=lambda _task: None) for name in ("Alice", "Bob")]
    group = TaskExecutionGroup(task, agents)
    controller.task_queue.append(group)
    controller.assignment = {agent.name: task.id for agent in agents}
    controller.agent_list = agents
    controller.env = SimpleNamespace(agents_ping=lambda: {"status": True})
    controller.executor.shutdown(wait=True)
    controller.executor = _ShutdownDuringSecondSubmitExecutor(controller)
    controller.execute_tasks = controller.shutdown_event.wait
    controller.process_completed_tasks = controller.shutdown_event.wait

    with pytest.raises(RuntimeError, match="second submit failed"):
        controller.run()

    assert controller.task_queue == []
    assert controller.result_queue == [group]
    assert list(group.futures) == ["Alice"]
    assert group.submission_complete is False
    assert controller.assignment == {"Alice": task.id, "Bob": task.id}
    assert controller.task_manager.status_updates[0][2] == {
        "reason": "controller_shutdown",
        "execution_may_still_be_active": True,
        "assigned_agents": ["Alice", "Bob"],
        "submitted_agents": ["Alice"],
        "active_agents": ["Alice"],
        "unsubmitted_agents": ["Bob"],
        "submission_complete": False,
        "agent_reuse_blocked": True,
        "requires_agent_reconciliation": True,
        "cancellation_requested": [],
        "cancellation_acknowledged": [],
        "cancellation_phases": {},
        "blocking_operation_termination": "unconfirmed",
    }
    assert sink.events[0]["payload"]["active_task_ids"] == [task.id]
    assert sink.events[0]["payload"]["active_agent_ids"] == ["Alice"]
    assert sink.events[0]["payload"]["incomplete_submission_task_ids"] == [task.id]

    next_task = Task("Do not reuse agents", {})
    next_task.candidate_list = ["Alice", "Bob"]
    next_task.number = 1
    controller.task_list = [next_task]
    assert controller.assign_runnable_tasks() == 0


def test_terminal_checkpoint_failure_surfaces_through_real_task_manager():
    controller, _, sink = _controller()
    manager = TaskManager(silent=True, event_sink=sink)
    checkpoint_error = RuntimeError("terminal checkpoint failed")
    manager.runtime_checkpoint = lambda: (_ for _ in ()).throw(checkpoint_error)
    controller.task_manager = manager
    controller.execute_tasks = controller._request_shutdown
    controller.worker = controller.shutdown_event.wait
    controller.process_completed_tasks = controller.shutdown_event.wait

    manager.checkpoint_runtime_state()
    with pytest.raises(RuntimeError) as raised:
        controller.run()

    assert raised.value is checkpoint_error
    assert [event["event_type"] for event in sink.events] == ["run_failed"]
    assert sink.events[0]["payload"]["thread"] == "run.checkpoint"


def test_non_cooperative_timeout_run_checkpoints_running_lifecycle():
    controller, _, sink = _controller()
    controller.shutdown_grace_period = 0.05
    controller.cancellation_grace_period = 0
    controller.max_task_time = 0
    controller.query_interval = 0
    controller.env = SimpleNamespace(agents_ping=lambda: {"status": True})
    release = threading.Event()
    running = threading.Event()
    task = Task("Non-cooperative timeout", {})
    task.candidate_list = ["Alice"]
    task.number = 1
    manager = TaskManager(silent=True, event_sink=sink)
    manager.set_task_list_from_decomposition([task])
    projected_task = manager.query_runnable_subtasks(["Alice"])[0]
    manager.mark_task_running(projected_task, ["Alice"])
    checkpoints = []
    manager.runtime_checkpoint = lambda: checkpoints.append(
        manager.runtime_task_store.snapshot()
    )
    controller.task_manager = manager
    agent = SimpleNamespace(name="Alice")

    def active_step():
        running.set()
        release.wait()
        return "done", "detail"

    future = controller.executor.submit(active_step)
    assert running.wait(1)
    group = TaskExecutionGroup(
        task=projected_task,
        agents=[agent],
        futures={"Alice": future},
        started_at=time.time() - 1,
        submission_complete=True,
    )
    controller.result_queue.append(group)
    controller.assignment["Alice"] = projected_task.id
    controller.execute_tasks = controller.shutdown_event.wait
    controller.worker = controller.shutdown_event.wait

    try:
        with pytest.raises(ControllerShutdownError, match="remained active after timeout"):
            controller.run()
    finally:
        release.set()
        for thread in controller.executor._threads:
            thread.join(1)

    node = checkpoints[-1]["nodes"][0]
    assert node["lifecycle"]["status"] == Task.running
    assert node["lifecycle"]["active_agents"] == ["Alice"]
    assert node["lifecycle"]["last_assigned_agents"] == ["Alice"]
    assert node["content"]["reflect"] == {
        "reason": "task_timeout_shutdown_escalation",
        "execution_may_still_be_active": True,
        "assigned_agents": ["Alice"],
        "submitted_agents": ["Alice"],
        "active_agents": ["Alice"],
        "unsubmitted_agents": [],
        "submission_complete": True,
        "agent_reuse_blocked": True,
        "requires_agent_reconciliation": True,
        "timeout_detected": ["Alice"],
        "shutdown_escalated": ["Alice"],
        "cancellation_requested": [],
        "cancellation_acknowledged": [],
        "cancellation_forced": [],
        "cancellation_phases": {},
        "blocking_operation_termination": "unconfirmed",
        "timeout_details": {
            "Alice": {
                "status": "timeout",
                "error": f"Task {projected_task.description} timeout for agent Alice",
                "cooperative_cancellation": False,
                "timeout_detected": True,
                "shutdown_escalated": True,
                "cancellation_requested": False,
                "cancellation_acknowledged": False,
                "cancellation_forced": False,
                "phase": "unknown",
            },
        },
    }
    assert controller.assignment == {"Alice": projected_task.id}
    assert group.terminal_state_persisted is False
    assert group.completed is False
    assert sink.events[-1]["event_type"] == "run_failed"


@pytest.mark.parametrize(
    "held_phase",
    ["tool_end", "before_retry", "before_agent_status", "before_database_update"],
)
def test_shutdown_snapshot_preserves_last_phase_before_held_boundary(held_phase):
    controller, _, _ = _controller()
    release = threading.Event()
    entered = threading.Event()
    task = Task("Held cooperative boundary", {})

    class Agent:
        name = "Alice"

        @staticmethod
        def supports_cooperative_cancellation():
            return True

        @staticmethod
        def step(_task, *, cancellation_token, phase_callback):
            phase_callback(held_phase)
            entered.set()
            release.wait()
            phase_callback("next_boundary")

    group = TaskExecutionGroup(task=task, agents=[Agent()])
    controller.start_execution_group(group)
    assert entered.wait(1)

    controller._request_shutdown()
    frozen = controller._freeze_execution_diagnostics()

    try:
        snapshot = frozen["execution_groups"]["items"][0]["executions"]["items"][0]
        assert snapshot["latest_phase"] == held_phase
        assert snapshot["future"]["done"] is False
        assert snapshot["token"] == {
            "requested": True,
            "requested_at_monotonic_ns": snapshot["lifecycle"]["items"][-1][
                "monotonic_ns"
            ],
            "requested_at_wall_time": group.cancellation_requested_at["Alice"],
            "acknowledged": False,
        }
        assert snapshot["execution_id"] == "execution-00000001"
        assert [entry["event"] for entry in snapshot["lifecycle"]["items"]] == [
            "submission_created",
            "future_started",
            "phase",
            "token_requested",
        ]
        assert {
            entry["execution_id"] for entry in snapshot["lifecycle"]["items"]
        } == {snapshot["execution_id"]}
    finally:
        release.set()
        with pytest.raises(AgentExecutionCancelledError):
            group.futures["Alice"].result(timeout=1)
        controller.executor.shutdown(wait=True)


def test_phase_acknowledgement_requires_requested_set_token_and_is_idempotent():
    controller, _, _ = _controller()
    group = TaskExecutionGroup(
        task=Task("Cancellation acknowledgement", {}),
        agents=[SimpleNamespace(name="Alice")],
    )
    token = threading.Event()
    group.cancellation_tokens["Alice"] = token
    future = Future()
    future.set_running_or_notify_cancel()
    group.futures["Alice"] = future
    group.execution_ids["Alice"] = "execution-00000001"
    controller._started_execution_groups = [group]
    callback = controller._phase_callback(group, "Alice")

    callback("before_request")
    assert group.cancellation_acknowledged == set()
    assert not any(
        entry["event"] == "token_acknowledged"
        for entry in group.phase_history["Alice"]
    )

    controller._request_shutdown()
    controller._request_shutdown()
    assert [
        entry["event"] for entry in group.phase_history["Alice"]
    ].count("token_requested") == 1
    for _ in range(2):
        with pytest.raises(AgentExecutionCancelledError):
            callback("tool_end")

    group.timeout_details["Alice"] = {"cancellation_acknowledged": False}
    assert controller._acknowledge_cancellation(group, "Alice") is True
    assert group.timeout_details["Alice"]["cancellation_acknowledged"] is True

    assert group.cancellation_acknowledged == {"Alice"}
    acknowledgement_events = [
        entry for entry in group.phase_history["Alice"]
        if entry["event"] == "token_acknowledged"
    ]
    assert len(acknowledgement_events) == 1
    execution = controller.snapshot_execution_ledger()["groups"]["items"][0][
        "executions"
    ]["items"][0]
    assert execution["cancellation"] == {
        "requested": True,
        "acknowledged": True,
        "requested_at_monotonic_ns": next(
            entry["monotonic_ns"] for entry in group.phase_history["Alice"]
            if entry["event"] == "token_requested"
        ),
        "acknowledged_at_monotonic_ns": acknowledgement_events[0]["monotonic_ns"],
        "requested_at_wall_time": group.cancellation_requested_at["Alice"],
    }
    future.set_result(("cancelled", {
        "failure": {
            "reason": "cancelled",
            "cancellation_acknowledged": True,
        },
    }))


def test_admission_closed_with_unset_or_missing_token_does_not_acknowledge():
    controller, _, _ = _controller()
    group = TaskExecutionGroup(
        task=Task("Admission race", {}),
        agents=[SimpleNamespace(name="Alice")],
    )
    callback = controller._phase_callback(group, "Alice")
    controller._measurement_cut_admission_closed = True

    with pytest.raises(AgentExecutionCancelledError):
        callback("before_tool")

    token = threading.Event()
    group.cancellation_tokens["Alice"] = token
    with pytest.raises(AgentExecutionCancelledError):
        callback("before_tool")

    assert token.is_set() is False
    assert group.cancellation_requested == set()
    assert group.cancellation_acknowledged == set()
    assert not any(
        entry["event"] == "token_acknowledged"
        for entry in group.phase_history["Alice"]
    )


def test_cut_phase_then_shutdown_request_keeps_request_and_acknowledgement_distinct():
    controller, _, _ = _controller()
    group = TaskExecutionGroup(
        task=Task("Cut to token race", {}),
        agents=[SimpleNamespace(name="Alice")],
    )
    token = threading.Event()
    pending = Future()
    pending.set_running_or_notify_cancel()
    group.cancellation_tokens["Alice"] = token
    group.futures["Alice"] = pending
    group.execution_ids["Alice"] = "execution-00000001"
    controller._started_execution_groups = [group]
    controller._measurement_cut_admission_closed = True

    with pytest.raises(AgentExecutionCancelledError):
        controller._phase_callback(group, "Alice")("before_retry")

    assert token.is_set() is False
    assert group.cancellation_requested == set()
    assert group.cancellation_acknowledged == set()
    assert [
        entry["event"] for entry in group.phase_history["Alice"]
    ] == ["phase"]

    controller._request_shutdown()

    assert token.is_set() is True
    assert group.cancellation_requested == {"Alice"}
    assert group.cancellation_acknowledged == set()
    assert [
        entry["event"] for entry in group.phase_history["Alice"]
    ] == ["phase", "token_requested"]
    pending.set_result(("finished", {}))


@pytest.mark.parametrize("phase_acknowledged", [False, True])
def test_future_reconciliation_uses_one_canonical_acknowledgement(phase_acknowledged):
    controller, _, _ = _controller()
    task = Task("Future acknowledgement", {})
    agent = SimpleNamespace(name="Alice")
    token = threading.Event()
    future = Future()
    group = TaskExecutionGroup(
        task=task,
        agents=[agent],
        futures={"Alice": future},
        cancellation_tokens={"Alice": token},
        submission_complete=True,
        execution_ids={"Alice": "execution-00000001"},
    )
    controller.result_queue = [group]
    controller._started_execution_groups = [group]
    controller._request_cancellation(group, "Alice", requested_at=time.time())
    if phase_acknowledged:
        with pytest.raises(AgentExecutionCancelledError):
            controller._phase_callback(group, "Alice")("after_agent_invocation")
    future.set_result(("cancelled", {
        "failure": {
            "reason": "cancelled",
            "cancellation_acknowledged": True,
        },
    }))

    controller._finalize_shutdown_groups()

    assert group.cancellation_acknowledged == {"Alice"}
    assert group.shutdown_reconciled is True
    assert [
        entry["event"] for entry in group.phase_history["Alice"]
    ].count("token_acknowledged") == 1
    assert controller.task_manager.status_updates[0][2]["reason"] == (
        "controller_shutdown_cancelled"
    )


def test_execution_history_is_bounded_monotonic_and_redacts_unsafe_labels():
    controller, _, _ = _controller()
    task = Task("Bounded diagnostics", {})
    group = TaskExecutionGroup(task=task, agents=[])

    with controller._execution_state_lock:
        for index in range(controller.EXECUTION_HISTORY_LIMIT + 5):
            controller._record_execution_history_locked(
                group,
                "Alice",
                "phase",
                phase=(
                    "secret payload with spaces"
                    if index == controller.EXECUTION_HISTORY_LIMIT + 4
                    else f"phase_{index}"
                ),
            )

    history = group.phase_history["Alice"]
    assert len(history) == controller.EXECUTION_HISTORY_LIMIT
    assert group.phase_history_truncated == {"Alice": 5}
    assert [item["sequence"] for item in history] == sorted(
        item["sequence"] for item in history
    )
    assert [item["monotonic_ns"] for item in history] == sorted(
        item["monotonic_ns"] for item in history
    )
    assert history[-1]["phase"] == "redacted"
    assert "secret payload with spaces" not in str(history)


def test_execution_history_has_controller_wide_bound():
    controller, _, _ = _controller()
    groups = [
        TaskExecutionGroup(task=Task(f"History {index}", {}), agents=[])
        for index in range(20)
    ]

    with controller._execution_state_lock:
        for group in groups:
            for index in range(controller.EXECUTION_HISTORY_LIMIT):
                controller._record_execution_history_locked(
                    group, "Alice", "phase", phase=f"phase_{index}",
                )

    assert sum(
        len(entries)
        for group in groups
        for entries in group.phase_history.values()
    ) == controller.EXECUTION_HISTORY_TOTAL_LIMIT
    assert len(controller._execution_history_index) == (
        controller.EXECUTION_HISTORY_TOTAL_LIMIT
    )


def test_shutdown_execution_snapshot_bounds_historical_groups():
    controller, _, _ = _controller()
    groups = [
        TaskExecutionGroup(task=Task(f"Completed {index}", {}), agents=[])
        for index in range(controller.SHUTDOWN_DIAGNOSTIC_GROUP_LIMIT + 12)
    ]
    for group in groups:
        group.completed = True
    controller._started_execution_groups = groups

    snapshot = controller._freeze_execution_diagnostics()

    assert len(snapshot["execution_groups"]["items"]) == (
        controller.SHUTDOWN_DIAGNOSTIC_GROUP_LIMIT
    )
    assert snapshot["execution_groups"]["retention"] == {
        "capacity": controller.SHUTDOWN_DIAGNOSTIC_GROUP_LIMIT,
        "retained": controller.SHUTDOWN_DIAGNOSTIC_GROUP_LIMIT,
        "truncated": True,
        "dropped_count": 12,
    }


def test_post_verdict_diagnostics_cannot_change_frozen_shutdown_failure():
    controller, checkpoints, _ = _controller()
    controller.shutdown_grace_period = 0.02
    release = threading.Event()
    running = threading.Event()

    def held_worker():
        running.set()
        release.wait()

    future = controller.executor.submit(held_worker)
    assert running.wait(1)
    controller.execute_tasks = controller._request_shutdown
    controller.worker = controller.shutdown_event.wait
    controller.process_completed_tasks = controller.shutdown_event.wait

    def slow_capture(_threads, **_kwargs):
        release.set()
        future.result(timeout=1)
        return {
            "captured_at_monotonic_ns": time.monotonic_ns(),
            "threads": {"items": [], "retention": {
                "capacity": controller.SHUTDOWN_DIAGNOSTIC_THREAD_LIMIT,
                "retained": 0, "truncated": False, "dropped_count": 0,
            }},
        }

    controller._capture_post_verdict_thread_stacks = slow_capture

    with pytest.raises(ControllerShutdownError, match="shutdown incomplete"):
        controller.run()

    assert controller.shutdown_complete is False
    assert controller.shutdown_context["shutdown_complete"] is False
    assert controller.shutdown_diagnostics["verdict"]["shutdown_complete"] is False
    assert controller.shutdown_diagnostics["verdict"][
        "authoritative_basis"
    ]["live_threads"]
    assert checkpoints == ["checkpoint"]


def test_post_verdict_collection_failure_does_not_change_success_or_checkpoint():
    controller, checkpoints, sink = _controller()
    controller.execute_tasks = controller._request_shutdown
    controller.worker = controller.shutdown_event.wait
    controller.process_completed_tasks = controller.shutdown_event.wait
    controller._capture_post_verdict_thread_stacks = lambda _threads, **_kwargs: (
        _ for _ in ()
    ).throw(RuntimeError("diagnostics unavailable"))

    controller.run()

    assert controller.shutdown_complete is True
    assert controller.shutdown_diagnostics["post_verdict"][
        "diagnostic_collection_error"
    ] == {
        "collector": "thread_stacks",
        "error_type": "RuntimeError",
    }
    assert checkpoints == ["checkpoint"]
    assert sink.events[-1]["event_type"] == "run_completed"


def test_tool_runtime_diagnostics_require_nonblocking_snapshot_after_verdict():
    controller, checkpoints, _ = _controller()
    calls = []
    controller.env = SimpleNamespace(
        get_tool_runtime_context=lambda: calls.append(True),
    )
    controller.execute_tasks = controller._request_shutdown
    controller.worker = controller.shutdown_event.wait
    controller.process_completed_tasks = controller.shutdown_event.wait

    controller.run()

    assert controller.shutdown_context["tool_runtime"] == {
        "diagnostic_collection_error": {
            "collector": "tool_runtime",
            "error_type": "NonBlockingSnapshotUnavailable",
        },
    }
    assert calls == []
    assert checkpoints == ["checkpoint"]


def test_execution_snapshot_does_not_block_on_held_state_lock():
    controller, _, _ = _controller()
    release = threading.Event()
    locked = threading.Event()

    def hold_lock():
        with controller._execution_state_lock:
            locked.set()
            release.wait()

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert locked.wait(1)
    try:
        snapshot = controller._freeze_execution_diagnostics()
    finally:
        release.set()
        thread.join(1)

    assert snapshot["diagnostic_collection_error"] == {
        "collector": "execution_snapshot",
        "error_type": "ExecutionStateLockUnavailable",
    }


def test_future_snapshot_does_not_use_held_future_lock():
    future = Future()
    release = threading.Event()
    locked = threading.Event()

    def hold_lock():
        with future._condition:
            locked.set()
            release.wait()

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert locked.wait(1)
    group = TaskExecutionGroup(task=Task("Snapshot", {}), agents=[])
    group.execution_ids["Alice"] = "execution-00000001"
    group.execution_started_markers["Alice"] = {
        "event": "future_started",
        "execution_id": "execution-00000001",
        "monotonic_ns": time.monotonic_ns(),
    }
    try:
        snapshot = GlobalController._diagnostic_execution_state(group, "Alice")
    finally:
        release.set()
        thread.join(1)

    assert snapshot == {
        "done": False,
        "running": True,
        "cancelled_before_start": False,
    }


def test_post_verdict_capture_includes_reflection_and_feedback_workers():
    controller, _, _ = _controller()
    release = threading.Event()
    running = threading.Event()

    def held_post_processing():
        running.set()
        release.wait()

    reflection = threading.Thread(
        target=held_post_processing, name="controller-reflection-Alice", daemon=True,
    )
    feedback = threading.Thread(
        target=held_post_processing, name="controller-feedback", daemon=True,
    )
    reflection.start()
    feedback.start()
    assert running.wait(1)
    group = TaskExecutionGroup(task=Task("Post processing", {}), agents=[])
    group.completed = True
    group.reflection_workers["Alice"] = reflection
    group.feedback_worker = feedback
    controller._started_execution_groups = [group]
    controller.execute_tasks = controller._request_shutdown
    controller.worker = controller.shutdown_event.wait
    controller.process_completed_tasks = controller.shutdown_event.wait

    try:
        controller.run()
        names = {
            item["name"]
            for item in controller.shutdown_diagnostics[
                "post_verdict"
            ]["threads"]["items"]
        }
        assert "controller-reflection-Alice" in names
        assert "controller-feedback" in names
    finally:
        release.set()
        reflection.join(1)
        feedback.join(1)


def test_post_verdict_stack_capture_contains_coordinates_but_no_locals():
    release = threading.Event()
    running = threading.Event()

    def worker():
        secret_local = "must-not-be-captured"
        running.set()
        release.wait()
        return secret_local

    thread = threading.Thread(target=worker, name="diagnostic-worker")
    thread.start()
    assert running.wait(1)
    try:
        diagnostics = GlobalController._capture_post_verdict_thread_stacks([thread])
    finally:
        release.set()
        thread.join(1)

    snapshot = diagnostics["threads"]["items"][0]
    assert snapshot["name"] == "diagnostic-worker"
    assert snapshot["stack"]["items"]
    assert set(snapshot["stack"]["items"][-1]) == {"file", "line", "function"}
    assert any(
        frame["file"] == "tests/test_controller_shutdown.py"
        for frame in snapshot["stack"]["items"]
    )
    assert all(
        not frame["file"].startswith("/")
        for frame in snapshot["stack"]["items"]
    )
    assert "must-not-be-captured" not in str(diagnostics)


def test_stack_retention_reports_exact_dropped_frame_count():
    release = threading.Event()
    running = threading.Event()

    def recurse(depth):
        if depth:
            return recurse(depth - 1)
        running.set()
        release.wait()

    thread = threading.Thread(target=lambda: recurse(80), name="deep-stack")
    thread.start()
    assert running.wait(1)
    try:
        frame = sys._current_frames()[thread.ident]
        total_frames = 0
        while frame is not None:
            total_frames += 1
            frame = frame.f_back
        diagnostics = GlobalController._capture_post_verdict_thread_stacks([thread])
    finally:
        release.set()
        thread.join(1)

    retention = diagnostics["threads"]["items"][0]["stack"]["retention"]
    assert retention == {
        "capacity": 64,
        "retained": min(total_frames, 64),
        "truncated": total_frames > 64,
        "dropped_count": max(0, total_frames - 64),
    }


@pytest.mark.parametrize(
    ("held_operation", "expected_phase", "expected_frame"),
    [
        ("agent_status", "before_agent_status", "agent_status"),
        ("update_database", "before_database_update", "update_database"),
    ],
)
def test_normal_step_held_boundary_is_correlated_end_to_end(
    held_operation, expected_phase, expected_frame,
):
    controller, checkpoints, _ = _controller()
    controller.shutdown_grace_period = 0.03
    entered = threading.Event()
    release = threading.Event()

    class HeldEnvironment:
        running = True

        @staticmethod
        def step(_name, _prompt, **_kwargs):
            return "done", {"action_list": [], "final_answer": "done"}

        @staticmethod
        def agent_status(name):
            if held_operation == "agent_status":
                entered.set()
                release.wait()
            return {"status": True, "message": {"my_name": name}}

    class HeldDataManager:
        @staticmethod
        def query_env_with_task(_description, agent_query=False):
            return "environment"

        @staticmethod
        def query_history(_name):
            return "history"

        @staticmethod
        def query_other_agent_state(_name):
            return "other"

        @staticmethod
        def update_database(_payload):
            if held_operation == "update_database":
                entered.set()
                release.wait()

    environment = HeldEnvironment()
    manager = HeldDataManager()
    agent = BaseAgent(
        llm=object(), env=environment, data_manager=manager,
        name="Alice", silent=True,
    )
    task = Task("Held normal step", {"document": "public"})
    task._agent = ["Alice"]
    task.id = f"held-{held_operation}"
    group = TaskExecutionGroup(task=task, agents=[agent])
    controller.start_execution_group(group)
    assert entered.wait(1)
    controller.assignment["Alice"] = task.id
    controller.execute_tasks = controller._request_shutdown
    controller.worker = controller.shutdown_event.wait
    controller.process_completed_tasks = controller.shutdown_event.wait

    try:
        with pytest.raises(ControllerShutdownError, match="shutdown incomplete"):
            controller.run()
        verdict = controller.shutdown_diagnostics["verdict"]
        basis = verdict["authoritative_basis"]
        execution = basis["execution_groups"]["items"][0][
            "executions"
        ]["items"][0]
        lifecycle = execution["lifecycle"]["items"]
        assert execution["execution_id"] == "execution-00000001"
        assert execution["latest_phase"] == expected_phase
        assert execution["future"] == {
            "done": False,
            "running": True,
            "cancelled_before_start": False,
        }
        assert [item["event"] for item in lifecycle[:3]] == [
            "submission_created", "future_started", "phase",
        ]
        assert lifecycle[-1]["event"] == "token_requested"
        assert {item["execution_id"] for item in lifecycle} == {
            execution["execution_id"]
        }
        assert verdict["shutdown_complete"] is False
        assert verdict["verdict_frozen_at_monotonic_ns"] == basis[
            "capture_completed_monotonic_ns"
        ]
        assert basis["capture_started_monotonic_ns"] <= basis[
            "capture_completed_monotonic_ns"
        ]
        assert controller.shutdown_diagnostics["post_verdict"][
            "captured_at_monotonic_ns"
        ] >= verdict["verdict_frozen_at_monotonic_ns"]
        stacks = controller.shutdown_diagnostics["post_verdict"][
            "threads"
        ]["items"]
        held_thread = next(
            thread_snapshot for thread_snapshot in stacks
            if any(
                frame["function"] == expected_frame
                for frame in thread_snapshot["stack"]["items"]
            )
        )
        assert held_thread["execution_ids"]["items"] == [
            execution["execution_id"]
        ]
    finally:
        release.set()
        group.futures["Alice"].result(timeout=1)
        for thread in controller.executor._threads:
            thread.join(1)

    assert controller.shutdown_complete is False
    assert controller._shutdown_authoritative_verdict["shutdown_complete"] is False
    with pytest.raises(TypeError):
        controller._shutdown_authoritative_verdict["shutdown_complete"] = True
    assert group.execution_completion_markers["Alice"]["execution_id"] == (
        execution["execution_id"]
    )
    assert checkpoints == ["checkpoint"]


@pytest.mark.parametrize("held_phase", ["tool_end", "before_retry"])
def test_phase_hold_is_correlated_through_verdict_and_stack(held_phase):
    controller, _, _ = _controller()
    controller.shutdown_grace_period = 0.03
    entered = threading.Event()
    release = threading.Event()
    task = Task(f"Held {held_phase}", {})

    def held_after_phase():
        entered.set()
        release.wait()

    class Agent:
        name = "Alice"

        @staticmethod
        def supports_cooperative_cancellation():
            return True

        @staticmethod
        def step(_task, *, cancellation_token, phase_callback):
            phase_callback(held_phase)
            held_after_phase()
            phase_callback("next_boundary")

    group = TaskExecutionGroup(task=task, agents=[Agent()])
    controller.start_execution_group(group)
    assert entered.wait(1)
    controller.assignment["Alice"] = task.id
    controller.execute_tasks = controller._request_shutdown
    controller.worker = controller.shutdown_event.wait
    controller.process_completed_tasks = controller.shutdown_event.wait

    try:
        with pytest.raises(ControllerShutdownError, match="shutdown incomplete"):
            controller.run()
        verdict = controller.shutdown_diagnostics["verdict"]
        execution = verdict["authoritative_basis"]["execution_groups"][
            "items"
        ][0]["executions"]["items"][0]
        assert execution["execution_id"] == "execution-00000001"
        assert execution["latest_phase"] == held_phase
        assert [
            item["event"] for item in execution["lifecycle"]["items"]
        ] == [
            "submission_created", "future_started", "phase", "token_requested",
        ]
        held_thread = next(
            thread_snapshot
            for thread_snapshot in controller.shutdown_diagnostics[
                "post_verdict"
            ]["threads"]["items"]
            if any(
                frame["function"] == "held_after_phase"
                for frame in thread_snapshot["stack"]["items"]
            )
        )
        assert held_thread["execution_ids"]["items"] == [
            execution["execution_id"]
        ]
    finally:
        release.set()
        with pytest.raises(AgentExecutionCancelledError):
            group.futures["Alice"].result(timeout=1)
        for thread in controller.executor._threads:
            thread.join(1)

    assert controller.shutdown_complete is False
    assert group.execution_completion_markers["Alice"]["execution_id"] == (
        execution["execution_id"]
    )


def test_completion_callback_never_waits_for_controller_execution_lock():
    controller, _, _ = _controller()
    release_worker = threading.Event()
    worker_started = threading.Event()
    lock_held = threading.Event()
    release_lock = threading.Event()
    task = Task("Nonblocking completion", {})

    class Agent:
        name = "Alice"

        @staticmethod
        def supports_cooperative_cancellation():
            return False

        @staticmethod
        def step(_task):
            worker_started.set()
            release_worker.wait()
            return "done", {}

    group = TaskExecutionGroup(task=task, agents=[Agent()])
    controller.start_execution_group(group)
    assert worker_started.wait(1)

    def hold_controller_lock():
        with controller._execution_state_lock:
            lock_held.set()
            release_lock.wait()

    lock_owner = threading.Thread(target=hold_controller_lock)
    lock_owner.start()
    assert lock_held.wait(1)
    release_worker.set()
    assert group.futures["Alice"].result(timeout=0.2) == ("done", {})
    assert group.execution_completion_markers["Alice"]["event"] == (
        "future_completed"
    )
    release_lock.set()
    lock_owner.join(1)
    controller.executor.shutdown(wait=True)


def test_execution_ids_are_monotonic_and_follow_worker_lifecycle():
    controller, _, _ = _controller()
    task = Task("Two executions", {})

    class Agent:
        def __init__(self, name):
            self.name = name

        @staticmethod
        def supports_cooperative_cancellation():
            return False

        def step(self, _task):
            return self.name, {}

    group = TaskExecutionGroup(
        task=task, agents=[Agent("Alice"), Agent("Bob")],
    )
    controller.start_execution_group(group)
    for future in group.futures.values():
        future.result(timeout=1)

    assert group.execution_ids == {
        "Alice": "execution-00000001",
        "Bob": "execution-00000002",
    }
    for name in ("Alice", "Bob"):
        execution_id = group.execution_ids[name]
        assert group.execution_started_markers[name]["execution_id"] == execution_id
        assert group.execution_completion_markers[name]["execution_id"] == execution_id
        frozen = controller._freeze_execution_diagnostics()
        executions = frozen["execution_groups"]["items"][0][
            "executions"
        ]["items"]
        execution = next(
            item for item in executions if item["actor_id"] == name
        )
        assert execution["execution_id"] == execution_id
        assert [
            item["event"] for item in execution["lifecycle"]["items"]
        ] == ["submission_created", "future_started", "future_completed"]
    controller.executor.shutdown(wait=True)


def test_late_execution_ledger_is_nonblocking_and_identity_is_reset():
    controller, _, _ = _controller()
    task = Task("Ledger", {})
    observed = []

    class Agent:
        name = "Alice"
        supports_cooperative_cancellation = staticmethod(lambda: False)

        def step(self, _task):
            observed.append(dict(current_execution_diagnostic_identity()))
            return "done", {}

    group = TaskExecutionGroup(task=task, agents=[Agent()])
    sink_calls = []
    controller.get_k11_provider_ledger_snapshot = lambda: {}
    controller._k11_late_diagnostic_sink = lambda: sink_calls.append("captured")
    controller.start_execution_group(group)
    assert group.futures["Alice"].result(timeout=1) == ("done", {})
    assert observed == [{
        "execution_id": "execution-00000001",
        "task_id": task.id,
        "actor_id": "Alice",
    }]
    assert current_execution_diagnostic_identity() is None
    assert sink_calls == ["captured"]
    before = dict(controller.assignment)
    snapshot = controller.snapshot_execution_ledger()
    execution = snapshot["groups"]["items"][0]["executions"]["items"][0]
    assert snapshot["schema_version"] == "controller-late-execution-ledger/1"
    assert execution["future"] == {"done": True, "cancelled": False, "running": False}
    assert controller.assignment == before
    controller.executor.shutdown(wait=True)


def test_late_execution_ledger_reports_missing_marker_and_lock_failure():
    controller, _, _ = _controller()
    future = Future()
    future.cancel()
    task = Task("Cancelled", {})
    group = TaskExecutionGroup(
        task=task, agents=[SimpleNamespace(name="Alice")],
        futures={"Alice": future},
    )
    controller._started_execution_groups = [group]
    execution = controller.snapshot_execution_ledger()["groups"]["items"][0]["executions"]["items"][0]
    assert execution["future"] == {"done": True, "cancelled": True, "running": False}
    assert execution["future_started"] is None
    assert execution["future_completed"] is None
    held = threading.Event()
    release = threading.Event()
    def hold_lock():
        with controller._execution_state_lock:
            held.set()
            release.wait(1)
    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert held.wait(1)
    locked = controller.snapshot_execution_ledger()
    release.set()
    thread.join(1)
    assert locked["diagnostic_collection_error"]["error_type"] == "ExecutionStateLockUnavailable"
    controller.executor.shutdown(wait=True)


def test_enabled_and_disabled_post_verdict_diagnostics_preserve_same_verdict():
    def execute(*, diagnostics_enabled):
        controller, _, _ = _controller()
        controller.executor.shutdown(wait=True)
        controller.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="diagnostic-parity",
        )
        controller.shutdown_grace_period = 0.02
        release = threading.Event()
        running = threading.Event()
        task = Task("Diagnostic parity", {})
        task.id = "diagnostic-parity-task"

        def held_worker():
            running.set()
            release.wait()

        future = controller.executor.submit(held_worker)
        assert running.wait(1)
        group = TaskExecutionGroup(
            task=task,
            agents=[SimpleNamespace(name="Alice")],
            futures={"Alice": future},
            submission_complete=True,
        )
        controller._started_execution_groups = [group]
        controller.result_queue = [group]
        controller.assignment = {"Alice": task.id}
        controller.execute_tasks = controller._request_shutdown
        controller.worker = controller.shutdown_event.wait
        controller.process_completed_tasks = controller.shutdown_event.wait
        if not diagnostics_enabled:
            controller._bounded_shutdown_diagnostic_threads = (
                lambda _threads: ([], {
                    "thread_candidates_retention": {
                        "capacity": controller.SHUTDOWN_DIAGNOSTIC_THREAD_LIMIT,
                        "retained": 0,
                        "truncated": False,
                        "dropped_count": 0,
                    },
                })
            )
            controller._capture_post_verdict_thread_stacks = lambda _threads, **_kwargs: {
                "captured_at_monotonic_ns": time.monotonic_ns(),
                "threads": {"items": [], "retention": {
                    "capacity": controller.SHUTDOWN_DIAGNOSTIC_THREAD_LIMIT,
                    "retained": 0,
                    "truncated": False,
                    "dropped_count": 0,
                }},
                "state": "disabled",
            }
            controller._bounded_tool_runtime_context = lambda: {
                "state": "disabled",
            }
        try:
            with pytest.raises(ControllerShutdownError):
                controller.run()
            basis = controller.shutdown_diagnostics["verdict"][
                "authoritative_basis"
            ]
            return {
                "shutdown_complete": controller.shutdown_complete,
                "live_threads": basis["live_threads"],
                "active_task_ids": basis["active_task_ids"],
                "active_agent_ids": basis["active_agent_ids"],
                "incomplete_submission_task_ids": basis[
                    "incomplete_submission_task_ids"
                ],
                "undrained_queues": basis["undrained_queues"],
            }
        finally:
            release.set()
            future.result(timeout=1)
            for thread in controller.executor._threads:
                thread.join(1)

    assert execute(diagnostics_enabled=True) == execute(diagnostics_enabled=False)


def _judged_reconciliation_controller(status="success", task_count=1):
    controller, _, sink = _controller()
    manager = TaskManager(silent=True, event_sink=sink)
    tasks = []
    for index in range(task_count):
        task = Task(f"Judged task {index}", {})
        task.candidate_list = ["Alice"]
        task.number = 1
        tasks.append(task)
    manager.set_task_list_from_decomposition(tasks)
    projected_tasks = list(manager.graph.vertex)
    for task in projected_tasks:
        manager.mark_task_running(task, ["Alice"])
    sink.events.clear()
    controller.task_manager = manager
    controller.env = SimpleNamespace(
        attempt_id="attempt-a",
        task_name="runtime-task-a",
        is_task_complete=lambda: True,
        get_score=lambda: {
            "attempt_id": "attempt-a",
            "task_name": "runtime-task-a",
            "status": status,
            "score": 100 if status == "success" else 0,
            "progress": 100 if status == "success" else 0,
        },
        stop=lambda: None,
    )
    return controller, manager, sink, projected_tasks[0] if projected_tasks else None


def _controller():
    controller = object.__new__(GlobalController)
    controller.logger = logging.getLogger("test-controller-shutdown")
    controller.shutdown_event = threading.Event()
    controller._failure_lock = threading.Lock()
    controller._first_failure = None
    controller._controller_threads = []
    controller._run_started = False
    controller._execution_state_lock = threading.RLock()
    controller._tool_action_condition = threading.Condition(controller._execution_state_lock)
    controller._active_tool_actions = 0
    controller._judger_terminal_pending = False
    controller._judger_terminal_observed = False
    controller._judger_terminal_payload = None
    controller._judger_terminal_detected_at = None
    controller._judger_terminal_observed_at = None
    controller._tool_drain_timed_out = False
    controller.judger_tool_drain_grace_period = 0.2
    controller._judger_terminal_reconciled = False
    controller.controller_state = GlobalController.STATE_RUNNING
    controller.shutdown_complete = False
    controller.shutdown_context = None
    controller.shutdown_grace_period = 0.2
    controller.executor = ThreadPoolExecutor(max_workers=1)
    controller.executor.submit(lambda: None).result(timeout=2)
    controller.task_queue = []
    controller.result_queue = []
    controller.task_list_lock = threading.Lock()
    controller.result_list_lock = threading.Lock()
    controller.assignment = {}
    checkpoints = []
    controller.task_manager = _TaskManagerStub(checkpoints)
    sink = InMemoryRuntimeEventRecorder("controller-test")
    controller.event_sink = sink
    controller.emit_terminal_events = True
    return controller, checkpoints, sink


class _TaskManagerStub:
    def __init__(self, checkpoints):
        self.checkpoints = checkpoints
        self.status_updates = []
        self.mark_error = None
        self.checkpoint_error = None

    def mark_task_status(self, task_id, status, feedback):
        if self.mark_error is not None:
            raise self.mark_error
        self.status_updates.append((task_id, status, feedback))

    def checkpoint_runtime_state(self, *, raise_on_error=False):
        if self.checkpoint_error is not None:
            if raise_on_error:
                raise self.checkpoint_error
            return
        self.checkpoints.append("checkpoint")


class _ShutdownDuringSecondSubmitExecutor:
    def __init__(self, controller):
        self.controller = controller
        self.submit_count = 0
        self._threads = set()

    def submit(self, _fn, _task):
        self.submit_count += 1
        if self.submit_count == 2:
            self.controller._request_shutdown()
            raise RuntimeError("second submit failed")
        future = Future()
        future.set_running_or_notify_cancel()
        return future

    def shutdown(self, wait=True, cancel_futures=False):
        return None
