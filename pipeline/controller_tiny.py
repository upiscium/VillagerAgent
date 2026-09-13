import threading
import time
import traceback
import inspect
import queue
import os
import sys
from collections import OrderedDict
from copy import deepcopy
from types import MappingProxyType
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from model.init_model import init_language_model

from type_define.graph import Task
from pipeline.task_manager import (
    TaskManager,
    TaskManagerFeedbackInterruptedError,
    TaskManagerFeedbackOutcome,
    TaskManagerFeedbackCommitError,
)
from pipeline.data_manager import DataManager
from pipeline.agent import BaseAgent, ReflectionInterruptedError, ReflectionOutcome
from pipeline.utils import *
from pipeline.controller_prompt import *
from pipeline.runtime_events import NoOpRuntimeEventSink, safe_emit_runtime_event
from env.env import VillagerBench
from env.minecraft_client import (
    ToolActionBlockedError, AgentExecutionCancelledError,
)
from env.minecraft_dual_dag import rank_minecraft_runtime_tasks
from env.runtime_paths import RuntimePaths, atomic_write_json
import logging
from contextvars import ContextVar


_execution_identity: ContextVar[dict | None] = ContextVar(
    "villageragent_execution_identity", default=None,
)


def current_execution_diagnostic_identity():
    """Return the current execution identity without exposing mutable state."""
    identity = _execution_identity.get()
    return MappingProxyType(dict(identity)) if identity is not None else None


@dataclass
class TaskExecutionGroup:
    task: Task
    agents: list[BaseAgent]
    futures: dict[str, Future] = field(default_factory=dict)
    started_at: float | None = None
    submission_complete: bool = False
    completed: bool = False
    terminal_state_persisted: bool = False
    post_processing_complete: bool = False
    cancellation_tokens: dict[str, threading.Event] = field(default_factory=dict)
    cancellation_phases: dict[str, str] = field(default_factory=dict)
    timeout_detected: set[str] = field(default_factory=set)
    timeout_detected_at: dict[str, float] = field(default_factory=dict)
    cancellation_requested: set[str] = field(default_factory=set)
    cancellation_acknowledged: set[str] = field(default_factory=set)
    cancellation_forced: set[str] = field(default_factory=set)
    cancellation_requested_at: dict[str, float] = field(default_factory=dict)
    shutdown_escalated: set[str] = field(default_factory=set)
    timeout_details: dict[str, dict] = field(default_factory=dict)
    timeout_checkpoint_persisted: bool = False
    post_processing_claim_token: object | None = None
    post_processing_started: bool = False
    post_processing_interrupted: bool = False
    post_processing_interruption: dict = field(default_factory=dict)
    post_processing_results: dict[str, dict] = field(default_factory=dict)
    reflection_started: set[str] = field(default_factory=set)
    reflection_completed: set[str] = field(default_factory=set)
    reflection_committed: set[str] = field(default_factory=set)
    reflection_workers: dict[str, threading.Thread] = field(default_factory=dict)
    shutdown_reconciled: bool = False
    assignments_released: bool = False
    feedback_persisted: bool = False
    feedback_started: bool = False
    feedback_completed: bool = False
    feedback_ancillary_complete: bool = False
    feedback_interrupted: bool = False
    feedback_interruption: dict = field(default_factory=dict)
    feedback_worker: threading.Thread | None = None
    phase_history: dict[str, list[dict]] = field(default_factory=dict)
    phase_history_truncated: dict[str, int] = field(default_factory=dict)
    phase_history_sequence: int = 0
    execution_ids: dict[str, str] = field(default_factory=dict)
    execution_started_markers: dict[str, dict] = field(default_factory=dict)
    execution_completion_markers: dict[str, dict] = field(default_factory=dict)


class ControllerShutdownError(RuntimeError):
    pass


class _ExecutionWorkerInvocation:
    """Callable wrapper that preserves the historical executor submit shape."""

    def __init__(self, controller, group, agent, kwargs):
        self.controller = controller
        self.group = group
        self.agent = agent
        self.kwargs = kwargs
        self.__self__ = agent

    def __call__(self, task, **_submitted_kwargs):
        return self.controller._run_execution_worker(
            self.group, self.agent, self.kwargs, task,
        )


class JudgedTaskFailure(RuntimeError):
    def __init__(self, payload: dict):
        iteration = payload.get("iteration") if isinstance(payload.get("iteration"), dict) else {}
        message = (
            "judged task failed: "
            f"status={payload.get('status')}, "
            f"end_reason={payload.get('end_reason')}, "
            f"progress={payload.get('progress', payload.get('score'))}, "
            f"judger_iteration_source={iteration.get('source')}, "
            f"judger_iterations={iteration.get('used')}/{iteration.get('limit')}, "
            "diagnostics=judged_terminal_diagnostics.json"
        )
        super().__init__(message)
        self.payload = dict(payload)


class JudgedEvidenceConsistencyError(ControllerShutdownError):
    def __init__(self, message: str, *, agent_failures: dict):
        super().__init__(message)
        self.agent_failures = agent_failures


class GlobalController:
    '''
    Global Controller for Minecraft game agents. The task is to assign tasks to agents. Create a plan that assigns tasks to suitable agents and return a list of task-assignment JSON objects.
    
    This is a tiny version of the GlobalController, which is used for faster task assignment and execution. It is designed for the purpose of testing and debugging.
    
    Args:
    - llm_config (dict): Configuration for the language model.
    - task_manager (TaskManager): TaskManager object.
    - data_manager (DataManager): DataManager object.
    - env (VillagerBench): VillagerBench object.
    - silent (bool): Whether to suppress the log output. Default is False.
    - max_workers (int): The maximum number of workers in the thread pool. Default is 4.
    '''
    STATE_RUNNING = "running"
    STATE_JUDGER_TERMINAL_PENDING = "judger_terminal_pending"
    STATE_JUDGER_TERMINAL_OBSERVED = "judger_terminal_observed"
    STATE_DRAINING = "draining"
    STATE_RECONCILING = "reconciling"
    STATE_SHUTDOWN = "shutdown"
    EXECUTION_HISTORY_LIMIT = 64
    EXECUTION_HISTORY_TOTAL_LIMIT = 1024
    EXECUTION_LIFECYCLE_LIMIT = EXECUTION_HISTORY_LIMIT + 2
    EXECUTION_ACTOR_LIMIT = 64
    K11_SNAPSHOT_EXECUTION_LIMIT = 128
    SHUTDOWN_DIAGNOSTIC_GROUP_LIMIT = 128
    SHUTDOWN_DIAGNOSTIC_THREAD_LIMIT = 128
    def __init__(self, llm_config: dict, task_manager: TaskManager, data_manager: DataManager, env: VillagerBench,
                 silent: bool = False, max_workers=4, tm_llm_config: dict = None, dm_llm_config: dict = None,
                 base_agent_config: dict = None, all_tools=None, minecraft_dual_dag_config: dict | None = None,
                 event_sink=None, emit_terminal_events: bool = True):

        self.task_manager = task_manager
        self._execution_state_lock = threading.RLock()
        self._tool_action_condition = threading.Condition(self._execution_state_lock)
        self._active_tool_actions = 0
        self._judger_terminal_pending = False
        self._judger_terminal_observed = False
        if hasattr(env, "configure_tool_action_barrier"):
            env.configure_tool_action_barrier(
                self._begin_tool_action,
                self._end_tool_action,
            )
        all_tools = list(all_tools or ())
        tm_llm_config = llm_config.copy() if tm_llm_config is None else tm_llm_config
        tm_llm_config["role_name"] = "TaskManager"
        self.task_manager.llm = init_language_model(tm_llm_config)

        self.task_manager.dm = data_manager
        self.data_manager = data_manager
        dm_llm_config = llm_config.copy() if dm_llm_config is None else dm_llm_config
        dm_llm_config["role_name"] = "DataManager"
        self.data_manager.llm = init_language_model(dm_llm_config)

        llm = init_language_model(llm_config)
        base_agent_config = llm_config.copy() if base_agent_config is None else base_agent_config
        base_llm = init_language_model(base_agent_config)
        base_agent_runtime_config = {
            key: base_agent_config[key]
            for key in BaseAgent.LOCAL_MODEL_CONFIG_KEYS
            if key in base_agent_config
        }
        runtime_paths = getattr(env, "runtime_paths", None)
        base_agent_output_config = (
            {
                "run_id": env.task_name,
                "reflection_output_dir": runtime_paths.run_result_dir(env.task_name),
            }
            if runtime_paths is not None
            else {}
        )
        self.agent_list = [
            BaseAgent(
                base_llm,
                env,
                data_manager,
                name=a.name,
                silent=False,
                all_tools=(env.guard_tool_actions(all_tools, actor_name=a.name)
                           if hasattr(env, "guard_tool_actions") else all_tools),
                **base_agent_output_config,
                **base_agent_runtime_config,
            )
            for a in env.agent_pool
        ]
        self.task_manager.agent_list = self.agent_list
        self.assignment = {}
        self.feedback = {}

        self.logger = init_logger("GlobalController", level=logging.DEBUG, dump=True, silent=silent)
        self.env = env
        self.llm = llm
        self.llm.role_name = "GlobalController"

        self.task_list = [Task]  # task published by tm
        self.query_interval = 1  # time interval between two query

        # init lock
        self.task_list_lock = threading.Lock()
        self.result_list_lock = threading.Lock()

        self.task_queue = []
        self.result_queue = []

        # init thread pool
        self.executor = ThreadPoolExecutor(max_workers=max_workers)  # 可以根据需要调整max_workers的数量
        self._started_execution_groups: list[TaskExecutionGroup] = []
        self._execution_history_index = OrderedDict()
        self._execution_history_dropped_count = 0
        self._next_execution_diagnostic_id = 0

        # init max task time
        self.max_task_time = 60 * 30 # 3min

        self.shutdown_event = threading.Event()
        self._post_processing_cancel_event = threading.Event()
        self._feedback_persistence_lock = threading.Lock()
        self._feedback_persistence_closed = False
        self._failure_lock = threading.Lock()
        self._first_failure = None
        self._controller_threads = []
        self.shutdown_grace_period = 5.0
        self.judger_drain_grace_period = 120.0
        self.cancellation_grace_period = 5.0
        self.post_processing_cancellation_grace_period = 1.5
        self._run_started = False
        self._judger_terminal_payload = None
        self._judger_terminal_detected_at = None
        self._judger_terminal_observed_at = None
        self._tool_drain_timed_out = False
        self.judger_tool_drain_grace_period = 45.0
        self._judger_terminal_reconciled = False
        self.controller_state = self.STATE_RUNNING
        self.shutdown_complete = False
        self.movement_shutdown_result = None
        self.shutdown_context = None
        self.shutdown_diagnostics = None
        self._shutdown_authoritative_verdict = None
        self._result_claim_cursor = 0
        self._post_processing_interruption_ledger = {}
        self._feedback_interruption_ledger = {}
        self._provider_termination_unconfirmed_task_ids = []
        self.minecraft_dual_dag_config = minecraft_dual_dag_config or {}
        self.event_sink = event_sink or getattr(task_manager, "event_sink", NoOpRuntimeEventSink())
        self.emit_terminal_events = emit_terminal_events
        self.task_manager.event_sink = self.event_sink

    def emit_runtime_event(self, event_type, *, entity_id=None, source, payload=None):
        safe_emit_runtime_event(getattr(self, "event_sink", NoOpRuntimeEventSink()), event_type, entity_id=entity_id, source=source, payload=payload)

    def with_execution_lock_nonblocking(
        self, callback, *, cutoff_monotonic_ns=None, seal_admission=False,
    ):
        """Snapshot execution state under its lock, or fail closed immediately."""
        if seal_admission:
            # Admission checks read this flag while holding the execution lock.
            # Publish it before the nonblocking attempt so a failed snapshot
            # cannot leave a post-cut admission gap.
            self._measurement_cut_admission_closed = True
        acquired = self._execution_state_lock.acquire(blocking=False)
        if not acquired:
            callback({"items": [], "count": 0, "errors": ["execution state lock unavailable"],
                      "retention": {"capacity": self.K11_SNAPSHOT_EXECUTION_LIMIT,
                                     "retained": 0, "truncated": False, "dropped_count": 0}})
            return False
        try:
            items = []
            for group in self._all_execution_groups():
                for agent_name in group.execution_ids:
                    completion = group.execution_completion_markers.get(agent_name)
                    completion_ns = (
                        completion.get("monotonic_ns")
                        if isinstance(completion, dict) else None
                    )
                    if (completion is not None
                            and (cutoff_monotonic_ns is None
                                 or (isinstance(completion_ns, int)
                                     and completion_ns < cutoff_monotonic_ns))):
                        continue
                    task_id = getattr(group.task, "id", None)
                    items.append({
                        "execution_id": group.execution_ids.get(agent_name),
                        "task_id": str(task_id) if task_id is not None else None,
                        "actor_id": agent_name,
                    })
            errors = []
            total_items = len(items)
            if total_items > self.K11_SNAPSHOT_EXECUTION_LIMIT:
                errors.append("active execution snapshot truncated")
            items = items[:self.K11_SNAPSHOT_EXECUTION_LIMIT]
            if any(not item["execution_id"] or item["task_id"] is None or not item["actor_id"] for item in items):
                errors.append("active execution snapshot lacks stable identity")
            snapshot = {"items": items, "count": len(items), "errors": errors,
                        "retention": {
                            "capacity": self.K11_SNAPSHOT_EXECUTION_LIMIT,
                            "retained": len(items), "truncated": bool(errors and "truncated" in errors[0]),
                            "dropped_count": max(0, total_items - self.K11_SNAPSHOT_EXECUTION_LIMIT),
                        }}
            callback(deepcopy(snapshot))
            return True
        finally:
            self._execution_state_lock.release()

    def _request_shutdown(self):
        with self._execution_state_lock:
            for group in self._all_execution_groups():
                for agent_name, token in group.cancellation_tokens.items():
                    future = group.futures.get(agent_name)
                    if future is None or future.done():
                        continue
                    self._request_cancellation(
                        group, agent_name, requested_at=time.time(),
                    )
            self._post_processing_cancellation_token().set()
            self.shutdown_event.set()
        with self._feedback_persistence_gate_lock():
            self._feedback_persistence_closed = True

    def _all_execution_groups(self):
        return list(getattr(self, "_started_execution_groups", ()))

    def _bounded_diagnostic_groups(self):
        groups = self._all_execution_groups()
        incomplete = [group for group in groups if not group.completed]
        selected = incomplete[-self.SHUTDOWN_DIAGNOSTIC_GROUP_LIMIT:]
        remaining = self.SHUTDOWN_DIAGNOSTIC_GROUP_LIMIT - len(selected)
        if remaining > 0:
            completed = [group for group in groups if group.completed]
            selected = completed[-remaining:] + selected
        return selected, max(0, len(groups) - len(selected))

    @staticmethod
    def _redact_diagnostic_label(value) -> str:
        value = str(value)
        if len(value) > 80 or not all(
            character.isalnum() or character in "_-.:" for character in value
        ):
            return "redacted"
        return value

    def _allocate_execution_diagnostic_id_locked(self) -> str:
        self._next_execution_diagnostic_id = getattr(
            self, "_next_execution_diagnostic_id", 0,
        ) + 1
        return f"execution-{self._next_execution_diagnostic_id:08d}"

    def _record_execution_history(
        self, group: TaskExecutionGroup, agent_name: str, event: str, *, phase=None,
    ) -> None:
        with self._execution_state_lock:
            self._record_execution_history_locked(
                group, agent_name, event, phase=phase,
            )

    def _record_execution_history_locked(
        self, group: TaskExecutionGroup, agent_name: str, event: str, *, phase=None,
    ) -> None:
        """Record bounded metadata only; never include payloads or model content."""
        group.phase_history_sequence += 1
        entry = {
            "sequence": group.phase_history_sequence,
            "monotonic_ns": time.monotonic_ns(),
            "execution_id": group.execution_ids.get(agent_name),
            "event": self._redact_diagnostic_label(event),
        }
        if phase is not None:
            entry["phase"] = self._redact_diagnostic_label(phase)
        history = group.phase_history.setdefault(agent_name, [])
        history.append(entry)
        history_index = getattr(self, "_execution_history_index", None)
        if history_index is None:
            history_index = self._execution_history_index = OrderedDict()
        history_key = (id(group), agent_name, entry["sequence"])
        history_index[history_key] = (group, agent_name)
        overflow = len(history) - self.EXECUTION_HISTORY_LIMIT
        if overflow > 0:
            removed = history[:overflow]
            del history[:overflow]
            for removed_entry in removed:
                history_index.pop(
                    (id(group), agent_name, removed_entry["sequence"]), None,
                )
            group.phase_history_truncated[agent_name] = (
                group.phase_history_truncated.get(agent_name, 0) + overflow
            )
            self._execution_history_dropped_count = getattr(
                self, "_execution_history_dropped_count", 0,
            ) + overflow
        while len(history_index) > self.EXECUTION_HISTORY_TOTAL_LIMIT:
            (_, _, sequence), (old_group, old_agent) = history_index.popitem(
                last=False
            )
            old_history = old_group.phase_history.get(old_agent, [])
            for index, old_entry in enumerate(old_history):
                if old_entry["sequence"] == sequence:
                    del old_history[index]
                    old_group.phase_history_truncated[old_agent] = (
                        old_group.phase_history_truncated.get(old_agent, 0) + 1
                    )
                    self._execution_history_dropped_count = getattr(
                        self, "_execution_history_dropped_count", 0,
                    ) + 1
                    break

    def _record_future_completion(
        self, group: TaskExecutionGroup, agent_name: str,
    ) -> None:
        """CPython dict assignment is GIL-serialized and never waits on controller locks."""
        group.execution_completion_markers[agent_name] = {
            "event": "future_completed",
            "execution_id": group.execution_ids[agent_name],
            "monotonic_ns": time.monotonic_ns(),
        }
        sink = getattr(self, "_k11_late_diagnostic_sink", None)
        if callable(sink):
            try:
                sink()
            except Exception as exc:
                self._k11_late_diagnostic_sink_error = type(exc).__name__

    def _run_execution_worker(self, group, agent, kwargs, task):
        identity = {
            "execution_id": group.execution_ids[agent.name],
            "task_id": task.id,
            "actor_id": agent.name,
        }
        # This remains the first worker-side operation.
        group.execution_started_markers[agent.name] = {
            "event": "future_started",
            "execution_id": group.execution_ids[agent.name],
            "monotonic_ns": time.monotonic_ns(),
            "thread_identity": threading.get_ident(),
            "native_thread_identity": threading.get_native_id(),
        }
        if not callable(getattr(self, "get_k11_provider_ledger_snapshot", None)):
            return agent.step(task, **kwargs)
        token = _execution_identity.set(identity)
        try:
            return agent.step(task, **kwargs)
        finally:
            _execution_identity.reset(token)

    def _phase_callback(self, group, agent_name):
        def update(phase):
            phase = str(phase)
            with self._execution_state_lock:
                group.cancellation_phases[agent_name] = phase
                self._record_execution_history_locked(
                    group, agent_name, "phase", phase=phase,
                )
                token = group.cancellation_tokens.get(agent_name)
                token_set = (
                    token is not None and token.is_set()
                )
                if token_set or self._execution_admission_closed():
                    if token_set:
                        self._acknowledge_cancellation(
                            group, agent_name, phase=phase,
                        )
                    operation = "confirmed" if any(marker in phase for marker in ("after", "_end", "return")) else "not_active"
                    raise AgentExecutionCancelledError(
                        phase=phase, blocking_operation_termination=operation,
                    )
        return update

    def _acknowledge_cancellation(
        self, group: TaskExecutionGroup, agent_name: str, *, phase=None,
    ) -> bool:
        """Atomically record the canonical cancellation acknowledgement.

        An acknowledgement is valid only for an existing canonical request and
        a token that has actually been set.  The set membership and its single
        bounded lifecycle marker are deliberately updated under one lock.
        """
        with self._execution_state_lock:
            token = group.cancellation_tokens.get(agent_name)
            if (
                token is None
                or not token.is_set()
                or agent_name not in group.cancellation_requested
            ):
                return False
            timeout_detail = group.timeout_details.get(agent_name)
            if isinstance(timeout_detail, dict):
                timeout_detail["cancellation_acknowledged"] = True
            if agent_name in group.cancellation_acknowledged:
                return True
            group.cancellation_acknowledged.add(agent_name)
            self._record_execution_history_locked(
                group, agent_name, "token_acknowledged",
                phase=phase if phase is not None else group.cancellation_phases.get(
                    agent_name, "unknown"
                ),
            )
            return True

    def _request_cancellation(
        self, group: TaskExecutionGroup, agent_name: str, *, requested_at: float,
    ) -> bool:
        """Publish a cooperative cancellation request as one locked record."""
        with self._execution_state_lock:
            token = group.cancellation_tokens.get(agent_name)
            if token is None:
                return False
            token.set()
            timeout_detail = group.timeout_details.get(agent_name)
            if isinstance(timeout_detail, dict):
                timeout_detail["cancellation_requested"] = True
            if agent_name not in group.cancellation_requested:
                group.cancellation_requested.add(agent_name)
                group.cancellation_requested_at[agent_name] = requested_at
                self._record_execution_history_locked(
                    group, agent_name, "token_requested",
                )
            return True

    def _post_processing_cancellation_token(self):
        token = getattr(self, "_post_processing_cancel_event", None)
        if token is None:
            token = self._post_processing_cancel_event = threading.Event()
        return token

    def _feedback_persistence_gate_lock(self):
        lock = getattr(self, "_feedback_persistence_lock", None)
        if lock is None:
            lock = self._feedback_persistence_lock = threading.Lock()
            self._feedback_persistence_closed = False
        return lock

    def _run_feedback_persistence_operation(
        self, callback, cancellation_token,
    ) -> bool:
        with self._feedback_persistence_gate_lock():
            if (
                self._feedback_persistence_closed
                or cancellation_token.is_set()
            ):
                return False
        callback()
        return True

    def _record_failure(self, name, exc):
        failure = {
            "thread": name,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        }
        with self._failure_lock:
            if self._first_failure is None:
                self._first_failure = (exc, exc.__traceback__, failure)
        self._request_shutdown()

    def _run_thread(self, name, entrypoint):
        try:
            entrypoint()
            if not self.shutdown_event.is_set():
                self._record_failure(
                    name,
                    ControllerShutdownError(f"Controller thread {name} exited before shutdown"),
                )
        except BaseException as exc:
            self._record_failure(name, exc)

    def should_shutdown(self):
        return self.shutdown_event.is_set()

    def _execution_admission_closed(self) -> bool:
        return bool(
            getattr(self, "_judger_terminal_pending", False)
            or getattr(self, "_judger_terminal_observed", False)
            or getattr(self, "_measurement_cut_admission_closed", False)
            or self.shutdown_event.is_set()
        )

    def observe_judger_terminal(self) -> bool:
        with self._tool_action_condition:
            if self._judger_terminal_pending or self._judger_terminal_observed:
                return True
            if not hasattr(self.env, "is_task_complete") or not self.env.is_task_complete():
                return False
            payload = self.env.get_score()
            self._validate_judger_payload_ownership(payload)
            self._judger_terminal_payload = dict(payload)
            self._judger_terminal_pending = True
            self._post_processing_cancellation_token().set()
            self._judger_terminal_detected_at = time.monotonic()
            self.controller_state = self.STATE_JUDGER_TERMINAL_PENDING
            self._tool_action_condition.notify_all()
            return True

    def _drain_active_tool_actions(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._tool_action_condition:
            if self._active_tool_actions == 0:
                return True
            detected_at = self._judger_terminal_detected_at or now
            grace_period = getattr(
                self,
                "judger_tool_drain_grace_period",
                self.shutdown_grace_period,
            )
            if now - detected_at < grace_period:
                return False
            self._tool_drain_timed_out = True
            raise ControllerShutdownError(
                "judger reached terminal status but "
                f"{self._active_tool_actions} Minecraft tool action(s) remained active"
            )

    def _mark_judger_terminal_observed(self) -> None:
        with self._tool_action_condition:
            if self._judger_terminal_observed:
                return
            if self._active_tool_actions:
                raise ControllerShutdownError(
                    "Cannot observe judger terminal status while Minecraft tools remain active"
                )
            self.logger.info("judged task reached terminal score status")
            self._judger_terminal_observed = True
            self._judger_terminal_observed_at = time.monotonic()
            self.controller_state = self.STATE_JUDGER_TERMINAL_OBSERVED

    def _begin_tool_action(self) -> None:
        with self._tool_action_condition:
            if self._execution_admission_closed():
                from env.minecraft_client import ToolActionBlockedError
                raise ToolActionBlockedError(
                    "Cannot start Minecraft tool action after judger terminal detection"
                )
            self._active_tool_actions += 1

    def _end_tool_action(self) -> None:
        with self._tool_action_condition:
            if self._active_tool_actions <= 0:
                raise ControllerShutdownError("Minecraft tool action barrier is unbalanced")
            self._active_tool_actions -= 1
            if self._active_tool_actions == 0:
                self._tool_action_condition.notify_all()

    def _validate_judger_payload_ownership(self, payload) -> None:
        if not isinstance(payload, dict) or not payload:
            raise ControllerShutdownError("judger terminal payload is missing or invalid")
        expected_attempt_id = getattr(self.env, "attempt_id", None)
        if expected_attempt_id is not None and payload.get("attempt_id") != expected_attempt_id:
            raise ControllerShutdownError(
                f"judger payload attempt mismatch: expected {expected_attempt_id!r}, "
                f"got {payload.get('attempt_id')!r}"
            )
        expected_task_name = getattr(self.env, "task_name", None)
        if expected_task_name is not None and payload.get("task_name") != expected_task_name:
            raise ControllerShutdownError(
                f"judger payload task mismatch: expected {expected_task_name!r}, "
                f"got {payload.get('task_name')!r}"
            )
        if payload.get("status") not in ("success", "failure"):
            raise ControllerShutdownError("judger terminal payload must contain success/failure status")

    def validate_assignments(self, result: [dict]):
        validated_assignments = []
        reserved_agent_names = set()

        for assign in result:
            task_id = assign.get("task_id")
            agent_names = assign.get("agent", [])
            if isinstance(agent_names, BaseAgent):
                agent_names = [agent_names.name]
            elif isinstance(agent_names, tuple):
                agent_names = list(agent_names)
            elif not isinstance(agent_names, list):
                agent_names = [agent_names]

            # Check if task exists
            if not isinstance(task_id, int) or task_id >= len(self.task_list) or task_id < 0:
                self.logger.warning("Choose a non exist task!")
                continue

            task_instance = self.task_list[task_id]
            required_agent_count = task_instance.number
            if (
                isinstance(required_agent_count, bool)
                or not isinstance(required_agent_count, int)
                or required_agent_count <= 0
            ):
                raise ValueError(
                    f"Task {task_instance.description} required agent count must be a positive integer"
                )
            if len(agent_names) != required_agent_count or len(set(agent_names)) != len(agent_names):
                self.logger.warning(
                    f"Task {task_instance.description} requires exactly {required_agent_count} unique agent(s)!"
                )
                continue

            agent_instances = []
            assignment_is_valid = True

            # Check if agents exist and are valid for the task
            for agent_name in agent_names:
                agent = next((a for a in self.agent_list if a.name == agent_name), None)
                if agent is None:
                    self.logger.warning(f"Agent {agent_name} does not exist!")
                    assignment_is_valid = False
                    break

                if (
                    self.assignment.get(agent.name) is not None
                    or agent_name in reserved_agent_names
                    or agent_name not in task_instance.candidate_list
                ):
                    self.logger.warning(f"Agent {agent_name} is not valid for the task!")
                    assignment_is_valid = False
                    break

                agent_instances.append(agent)

            if assignment_is_valid and len(agent_instances) == required_agent_count:
                validated_assignments.append({
                    "task_instance": task_instance,
                    "agent_instances": agent_instances
                })
                reserved_agent_names.update(agent.name for agent in agent_instances)

        return validated_assignments

    
    def execute_assignments(self, validated_assignments):
        with self._execution_state_lock:
            if self._execution_admission_closed():
                return 0
            assigned_count = 0
            for assignment in validated_assignments:
                task_instance = assignment["task_instance"]
                agent_instances = assignment["agent_instances"]
                agent_names = [agent.name for agent in agent_instances]

                for agent in agent_instances:
                    self.assignment[agent.name] = task_instance.id
                    task_instance._agent.append(agent.name)

                with self.task_list_lock:
                    self.task_manager.mark_task_running(task_instance, agent_names)
                    task_instance.status = Task.running
                    self.task_queue.append(TaskExecutionGroup(
                        task=task_instance,
                        agents=list(agent_instances),
                    ))
                self.emit_runtime_event("task_assigned", entity_id=task_instance.id, source="GlobalController.execute_assignments", payload={"agents": agent_names, "required_agent_count": task_instance.number})

                name_list = ", ".join(agent_names)
                self.logger.info(f"Agent(s) {name_list} assigned to do task {task_instance.description}")
                assigned_count += 1
            return assigned_count

    def start_execution_group(self, group: TaskExecutionGroup, *, enqueue: bool = True) -> None:
        with self._execution_state_lock:
            if self._execution_admission_closed():
                raise ControllerShutdownError(
                    "Cannot start execution after judger terminal detection or controller shutdown"
                )
            with self.result_list_lock:
                if enqueue:
                    self.result_queue.append(group)
                group.started_at = time.time()
                registry = getattr(self, "_started_execution_groups", None)
                if registry is None:
                    registry = self._started_execution_groups = []
                registry.append(group)
                for agent in group.agents:
                    if self._execution_admission_closed():
                        raise ControllerShutdownError(
                            f"Task {group.task.description} submission interrupted by controller shutdown"
                        )
                    group.execution_ids[agent.name] = (
                        self._allocate_execution_diagnostic_id_locked()
                    )
                    supports_cancellation = getattr(
                        agent, "supports_cooperative_cancellation", None,
                    )
                    cooperative = (
                        callable(supports_cancellation)
                        and supports_cancellation()
                    )
                    kwargs = {}
                    if cooperative:
                        token = threading.Event()
                        group.cancellation_tokens[agent.name] = token
                        kwargs = {
                            "cancellation_token": token,
                            "phase_callback": self._phase_callback(
                                group, agent.name,
                            ),
                        }
                    self._record_execution_history(
                        group, agent.name, "submission_created",
                    )
                    try:
                        parameters = inspect.signature(agent.step).parameters
                        if not any(p.kind == inspect.Parameter.VAR_KEYWORD
                                   for p in parameters.values()):
                            kwargs = {k: v for k, v in kwargs.items() if k in parameters}
                    except (TypeError, ValueError):
                        pass
                    try:
                        future = self.executor.submit(
                            _ExecutionWorkerInvocation(
                                self, group, agent, kwargs,
                            ),
                            group.task,
                            **kwargs,
                        )
                    except BaseException:
                        self._record_execution_history(
                            group, agent.name, "submission_failed",
                        )
                        raise
                    group.futures[agent.name] = future
                    future.add_done_callback(
                        lambda completed, execution_group=group, name=agent.name:
                        self._record_future_completion(execution_group, name)
                    )
                    self.logger.info(f"Agent {agent.name} is executing task now ...")
                group.submission_complete = True

    def _take_and_start_next_execution_group(self) -> bool:
        with self._execution_state_lock:
            if self._execution_admission_closed():
                return False
            with self.task_list_lock:
                if not self.task_queue:
                    return False
                with self.result_list_lock:
                    group = self.task_queue.pop(0)
                    self.result_queue.append(group)
            self.start_execution_group(group, enqueue=False)
            return True

    def _admit_group_post_processing(self, group: TaskExecutionGroup) -> bool:
        """Linearize optional reflection admission against controller shutdown."""
        with self._execution_state_lock:
            if self._execution_admission_closed():
                self._mark_post_processing_interrupted(
                    group,
                    reason="reflection_admission_closed",
                    provider_termination_confirmed=True,
                    diagnostics={"phase": "before_reflection"},
                )
                return False
            group.post_processing_started = True
            return True

    def _reflect_agent(self, agent, task, detail):
        try:
            parameters = inspect.signature(agent.reflect).parameters.values()
        except (TypeError, ValueError):
            parameters = ()
        names = {parameter.name for parameter in parameters}
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        kwargs = {}
        if "cancellation_token" in names or accepts_kwargs:
            kwargs["cancellation_token"] = self._post_processing_cancellation_token()
        if "commit_lock" in names or accepts_kwargs:
            kwargs["commit_lock"] = self._execution_state_lock
        return agent.reflect(task, detail, **kwargs)

    def _reflect_agent_bounded(self, group, agent, detail):
        outcome = queue.Queue(maxsize=1)

        def invoke():
            try:
                outcome.put((True, self._reflect_agent(agent, group.task, detail)))
            except BaseException as exc:
                outcome.put((False, exc))

        worker = threading.Thread(
            target=invoke,
            name=f"controller-reflection-{agent.name}",
            daemon=True,
        )
        group.reflection_workers[agent.name] = worker
        with self._execution_state_lock:
            if self._execution_admission_closed():
                raise ReflectionInterruptedError(
                    "Reflection admission closed before worker start",
                    provider_termination_confirmed=True,
                    diagnostics={"phase": "before_reflection_worker_start"},
                )
            worker.start()
        cancellation_deadline = None
        while True:
            try:
                succeeded, value = outcome.get(timeout=0.05)
            except queue.Empty:
                if not self._post_processing_cancellation_token().is_set():
                    continue
                if cancellation_deadline is None:
                    cancellation_deadline = time.monotonic() + getattr(
                        self, "post_processing_cancellation_grace_period", 1.5,
                    )
                if time.monotonic() < cancellation_deadline:
                    continue
                raise ReflectionInterruptedError(
                    "Reflection worker termination was not confirmed",
                    provider_termination_confirmed=False,
                    diagnostics={
                        "phase": "reflection_worker_join",
                        "reflection_worker_alive": worker.is_alive(),
                    },
                )
            if succeeded:
                return value
            raise value

    def _mark_post_processing_interrupted(
        self,
        group: TaskExecutionGroup,
        *,
        reason: str,
        provider_termination_confirmed: bool,
        diagnostics: dict | None = None,
        agent_name: str | None = None,
    ) -> None:
        group.post_processing_interrupted = True
        if agent_name:
            group.reflection_started.add(agent_name)
        previous_confirmation = group.post_processing_interruption.get(
            "provider_termination_confirmed", True,
        )
        group.post_processing_interruption = {
            "reason": reason,
            "provider_termination_confirmed": bool(
                previous_confirmation and provider_termination_confirmed
            ),
            "agent": agent_name,
            "diagnostics": dict(diagnostics or {}),
            "reflection_started": sorted(group.reflection_started),
            "reflection_completed": sorted(group.reflection_completed),
        }
        if not group.post_processing_interruption[
            "provider_termination_confirmed"
        ]:
            unconfirmed = getattr(
                self, "_provider_termination_unconfirmed_task_ids", None,
            )
            if unconfirmed is None:
                unconfirmed = self._provider_termination_unconfirmed_task_ids = []
            if group.task.id not in unconfirmed:
                unconfirmed.append(group.task.id)
            self._request_shutdown()

    def finalize_execution_group(self, group: TaskExecutionGroup, now: float | None = None) -> bool:
        if group.completed:
            return True
        if group.terminal_state_persisted:
            self._complete_execution_group(group)
            return True
        if not group.submission_complete:
            return False
        now = time.time() if now is None else now
        future_snapshots = {
            agent_name: self._snapshot_future(future)
            for agent_name, future in group.futures.items()
        }
        deadline_reached = (
            group.started_at is not None
            and now - group.started_at >= self.max_task_time
        )
        if deadline_reached:
            for agent in group.agents:
                agent_name = agent.name
                if future_snapshots[agent_name]["done"]:
                    continue
                with self._execution_state_lock:
                    if agent_name not in group.timeout_detected:
                        group.timeout_detected.add(agent_name)
                        group.timeout_detected_at[agent_name] = now
                        group.timeout_details[agent_name] = {
                            "status": "timeout",
                            "error": f"Task {group.task.description} timeout for agent {agent_name}",
                            "cooperative_cancellation": agent_name in group.cancellation_tokens,
                            "timeout_detected": True,
                            "shutdown_escalated": False,
                            "cancellation_requested": (
                                agent_name in group.cancellation_requested
                            ),
                            "cancellation_acknowledged": (
                                agent_name in group.cancellation_acknowledged
                            ),
                            "cancellation_forced": False,
                            "phase": group.cancellation_phases.get(
                                agent_name, "unknown"
                            ),
                        }

                # Completion may race with the deadline snapshot. Recheck before
                # delivering a cancellation signal or deciding work is active.
                future_snapshots[agent_name] = self._snapshot_future(
                    group.futures[agent_name]
                )
                if future_snapshots[agent_name]["done"]:
                    continue
                token = group.cancellation_tokens.get(agent_name)
                if token is not None:
                    self._request_cancellation(
                        group, agent_name, requested_at=now,
                    )

        with self._execution_state_lock:
            cancellation_requested = tuple(group.cancellation_requested)
        for agent_name in cancellation_requested:
            snapshot = future_snapshots[agent_name]
            if snapshot["done"] and self._is_cancellation_acknowledgement(snapshot):
                self._acknowledge_cancellation(group, agent_name)

        active_agents = [
            agent_name
            for agent_name, snapshot in future_snapshots.items()
            if not snapshot["done"]
        ]
        if active_agents:
            if not group.timeout_detected:
                return False
            cancellation_grace_period = getattr(
                self, "cancellation_grace_period", self.shutdown_grace_period
            )
            escalation_agents = [
                agent_name
                for agent_name in active_agents
                if agent_name in group.timeout_detected
                and now - group.timeout_detected_at[agent_name] >= cancellation_grace_period
            ]
            if escalation_agents:
                for agent_name in escalation_agents:
                    future_snapshots[agent_name] = self._snapshot_future(
                        group.futures[agent_name]
                    )
                escalation_agents = [
                    agent_name
                    for agent_name in escalation_agents
                    if not future_snapshots[agent_name]["done"]
                ]
            if escalation_agents:
                with self._execution_state_lock:
                    for agent_name in escalation_agents:
                        group.shutdown_escalated.add(agent_name)
                        group.timeout_details[agent_name][
                            "shutdown_escalated"
                        ] = True
                self._request_shutdown()
                names = ", ".join(sorted(escalation_agents))
                raise ControllerShutdownError(
                    f"Task {group.task.description} remained active after timeout for {names}"
                )
            if any(not snapshot["done"] for snapshot in future_snapshots.values()):
                return False

        agent_results = group.post_processing_results
        group_succeeded = not group.timeout_detected
        reflection_candidates = []
        for agent in group.agents:
            if agent.name in group.timeout_detected or agent.name in agent_results:
                continue
            snapshot = future_snapshots[agent.name]
            if snapshot["exception"] is not None:
                continue
            _, detail = snapshot["result"]
            if not (isinstance(detail, dict) and "failure" in detail):
                reflection_candidates.append(agent.name)
        if reflection_candidates and not group.post_processing_started:
            if not self._admit_group_post_processing(group):
                return False

        for agent in group.agents:
            if agent.name in agent_results:
                if agent_results[agent.name].get("status") != "success":
                    group_succeeded = False
                continue
            snapshot = future_snapshots[agent.name]
            if agent.name in group.timeout_detected:
                agent_results[agent.name] = dict(group.timeout_details[agent.name])
                group_succeeded = False
                continue
            try:
                if snapshot["exception"] is not None:
                    raise snapshot["exception"]
                _, detail = snapshot["result"]
                explicit_failure = isinstance(detail, dict) and "failure" in detail
                if explicit_failure:
                    reflected_success = False
                else:
                    if self._post_processing_cancellation_token().is_set():
                        self._mark_post_processing_interrupted(
                            group,
                            reason="controller_shutdown_before_reflection",
                            provider_termination_confirmed=True,
                            diagnostics={"phase": "before_reflection"},
                            agent_name=agent.name,
                        )
                        return False
                    group.reflection_started.add(agent.name)
                    try:
                        reflection_outcome = self._reflect_agent_bounded(
                            group, agent, detail,
                        )
                    except ReflectionInterruptedError as exc:
                        self._mark_post_processing_interrupted(
                            group,
                            reason="reflection_interrupted",
                            provider_termination_confirmed=(
                                exc.provider_termination_confirmed
                            ),
                            diagnostics=exc.diagnostics,
                            agent_name=agent.name,
                        )
                        return False
                    reflection_committed = (
                        isinstance(reflection_outcome, ReflectionOutcome)
                        and reflection_outcome.committed
                    )
                    if (
                        self._post_processing_cancellation_token().is_set()
                        and not reflection_committed
                    ):
                        self._mark_post_processing_interrupted(
                            group,
                            reason="controller_shutdown_during_reflection",
                            provider_termination_confirmed=True,
                            diagnostics={"phase": "after_reflection"},
                            agent_name=agent.name,
                        )
                        return False
                    reflected_success = bool(reflection_outcome)
                    if reflection_committed:
                        group.reflection_committed.add(agent.name)
                    group.reflection_completed.add(agent.name)
                agent_results[agent.name] = {
                    "status": "success" if reflected_success else "failure",
                    "detail": detail,
                }
                if not reflected_success:
                    group_succeeded = False
            except Exception as exc:
                self.logger.error(
                    f"Task {group.task.description} failed for agent {agent.name} with exception: {exc}"
                )
                self.logger.exception(exc)
                agent_results[agent.name] = {
                    "status": "failure",
                    "error": str(exc),
                }
                failure_detail = getattr(exc, "failure_detail", None)
                if isinstance(failure_detail, dict):
                    agent_results[agent.name]["failure"] = dict(failure_detail)
                group_succeeded = False

        status = Task.success if group_succeeded else Task.failure
        if len(group.agents) == 1:
            result = agent_results[group.agents[0].name]
            feedback = result if "failure" in result else (
                result
                if result.get("status") == "timeout"
                else result.get("detail", result.get("error"))
            )
        else:
            feedback = {"agent_results": agent_results}
        with self._execution_state_lock:
            uncommitted_reflections = (
                group.reflection_started - group.reflection_committed
            )
            if (
                self._post_processing_cancellation_token().is_set()
                and uncommitted_reflections
            ):
                self._mark_post_processing_interrupted(
                    group,
                    reason="controller_shutdown_before_terminal_commit",
                    provider_termination_confirmed=True,
                    diagnostics={
                        "phase": "before_terminal_commit",
                        "uncommitted_reflections": sorted(
                            uncommitted_reflections
                        ),
                    },
                )
                return False
            self.set_task_status(group.task.id, status, feedback)
            group.task.status = status
            group.terminal_state_persisted = True
        self._complete_execution_group(group)
        return True

    @staticmethod
    def _snapshot_future(future: Future) -> dict:
        if not future.done():
            return {"done": False, "cancelled": False, "result": None, "exception": None}
        if future.cancelled():
            return {"done": True, "cancelled": True, "result": None, "exception": None}
        try:
            result = future.result()
        except BaseException as exc:
            return {"done": True, "cancelled": False, "result": None, "exception": exc}
        return {"done": True, "cancelled": False, "result": result, "exception": None}

    @staticmethod
    def _is_cancellation_acknowledgement(snapshot: dict) -> bool:
        if snapshot["cancelled"] or snapshot["exception"] is not None:
            return False
        result = snapshot["result"]
        if not isinstance(result, tuple) or len(result) != 2:
            return False
        detail = result[1]
        return (
            isinstance(detail, dict)
            and isinstance(detail.get("failure"), dict)
            and detail["failure"].get("reason") == "cancelled"
            and detail["failure"].get("cancellation_acknowledged") is True
        )

    def _task_manager_feedback(self, task, cancellation_token, commit_lock):
        try:
            parameters = inspect.signature(
                self.task_manager.feedback_task
            ).parameters.values()
        except (TypeError, ValueError):
            parameters = ()
        names = {parameter.name for parameter in parameters}
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        kwargs = {}
        if "cancellation_token" in names or accepts_kwargs:
            kwargs["cancellation_token"] = cancellation_token
        if "commit_lock" in names or accepts_kwargs:
            kwargs["commit_lock"] = commit_lock
        if "persistence_gate" in names or accepts_kwargs:
            kwargs["persistence_gate"] = (
                self._run_feedback_persistence_operation
            )
        return self.task_manager.feedback_task(task, **kwargs)

    def _run_task_manager_feedback_bounded(
        self, group: TaskExecutionGroup, task,
    ):
        outcome = queue.Queue(maxsize=1)
        cancellation_token = self._post_processing_cancellation_token()

        def invoke():
            try:
                outcome.put((True, self._task_manager_feedback(
                    task, cancellation_token, self._execution_state_lock,
                )))
            except BaseException as exc:
                outcome.put((False, exc))

        worker = threading.Thread(
            target=invoke,
            name=f"controller-task-feedback-{group.task.id}",
            daemon=True,
        )
        with self._execution_state_lock:
            if self._execution_admission_closed():
                raise TaskManagerFeedbackInterruptedError(
                    "TaskManager feedback admission closed before start",
                    provider_termination_confirmed=True,
                    diagnostics={"phase": "before_feedback_worker_start"},
                )
            group.feedback_started = True
            group.feedback_worker = worker
            worker.start()

        cancellation_deadline = None
        while True:
            try:
                succeeded, value = outcome.get(timeout=0.05)
            except queue.Empty:
                if not cancellation_token.is_set():
                    continue
                if cancellation_deadline is None:
                    cancellation_deadline = time.monotonic() + getattr(
                        self, "post_processing_cancellation_grace_period", 1.5,
                    )
                if time.monotonic() < cancellation_deadline:
                    continue
                raise TaskManagerFeedbackInterruptedError(
                    "TaskManager feedback worker termination was not confirmed",
                    provider_termination_confirmed=False,
                    diagnostics={
                        "phase": "feedback_worker_join",
                        "feedback_worker_alive": worker.is_alive(),
                    },
                )
            if not succeeded:
                raise value
            feedback_committed = (
                isinstance(value, TaskManagerFeedbackOutcome)
                and value.committed
            )
            if cancellation_token.is_set() and not feedback_committed:
                raise TaskManagerFeedbackInterruptedError(
                    "TaskManager feedback was interrupted before commit",
                    provider_termination_confirmed=True,
                    diagnostics={"phase": "after_feedback"},
                )
            return value

    def _mark_feedback_interrupted(
        self,
        group: TaskExecutionGroup,
        exc: TaskManagerFeedbackInterruptedError,
    ) -> None:
        group.feedback_interrupted = True
        group.feedback_interruption = {
            "reason": "task_manager_feedback_interrupted",
            "feedback_started": group.feedback_started,
            "provider_termination_confirmed": (
                exc.provider_termination_confirmed
            ),
            "diagnostics": dict(exc.diagnostics),
        }
        ledger = getattr(self, "_feedback_interruption_ledger", None)
        if ledger is None:
            ledger = self._feedback_interruption_ledger = {}
        ledger[group.task.id] = {
            "task_id": group.task.id,
            **group.feedback_interruption,
        }
        if not exc.provider_termination_confirmed:
            unconfirmed = getattr(
                self, "_provider_termination_unconfirmed_task_ids", None,
            )
            if unconfirmed is None:
                unconfirmed = self._provider_termination_unconfirmed_task_ids = []
            if group.task.id not in unconfirmed:
                unconfirmed.append(group.task.id)
            self._request_shutdown()

    def _record_feedback_ancillary_deferred(
        self, group: TaskExecutionGroup,
    ) -> None:
        group.feedback_interruption = {
            "reason": "task_manager_feedback_ancillary_deferred",
            "feedback_started": group.feedback_started,
            "committed": True,
            "ancillary_complete": False,
            "provider_termination_confirmed": True,
            "diagnostics": {"phase": "after_feedback_commit"},
        }
        ledger = getattr(self, "_feedback_interruption_ledger", None)
        if ledger is None:
            ledger = self._feedback_interruption_ledger = {}
        ledger[group.task.id] = {
            "task_id": group.task.id,
            **group.feedback_interruption,
        }

    def _complete_execution_group(self, group: TaskExecutionGroup) -> None:
        if not group.assignments_released:
            for agent in self.agent_list:
                if self.assignment.get(agent.name) == group.task.id:
                    self.assignment.pop(agent.name)
            group.assignments_released = True
            self.logger.info(
                f"task {group.task.description} has been executed, the result is {group.task.status}"
            )
        if not group.feedback_persisted:
            try:
                feedback_outcome = self._run_task_manager_feedback_bounded(
                    group, self.get_task_by_id(group.task.id),
                )
            except TaskManagerFeedbackInterruptedError as exc:
                self._mark_feedback_interrupted(group, exc)
            except TaskManagerFeedbackCommitError:
                group.feedback_persisted = True
                group.feedback_completed = True
                group.completed = True
                group.post_processing_complete = True
                raise
            else:
                group.feedback_persisted = True
                group.feedback_completed = True
                group.feedback_ancillary_complete = bool(
                    getattr(feedback_outcome, "ancillary_complete", True)
                )
                if not group.feedback_ancillary_complete:
                    self._record_feedback_ancillary_deferred(group)
        group.post_processing_complete = group.feedback_persisted
        group.completed = True

    def reconcile_judger_terminal(self) -> bool:
        if self._judger_terminal_pending and not self._judger_terminal_observed:
            if not self._drain_active_tool_actions():
                return False
            self._mark_judger_terminal_observed()
        if not self._judger_terminal_observed:
            return False
        if self._judger_terminal_reconciled:
            return True
        self.controller_state = self.STATE_DRAINING
        running_task_ids = self._running_runtime_task_ids()
        if len(running_task_ids) != 1:
            raise ControllerShutdownError(
                f"judger terminal reconciliation requires exactly one running task; "
                f"found {len(running_task_ids)}"
            )
        task_id = running_task_ids[0]
        groups = self._execution_groups_snapshot()
        matching_groups = [
            group for group in groups
            if group.task.id == task_id and not group.completed
        ]
        if len(matching_groups) > 1:
            raise ControllerShutdownError(
                f"judger terminal reconciliation found multiple execution groups for task {task_id}"
            )

        group = matching_groups[0] if matching_groups else None
        now = time.monotonic()
        observed_at = (
            self._judger_terminal_detected_at
            or self._judger_terminal_observed_at
            or now
        )
        drain_grace = getattr(
            self, "judger_drain_grace_period", self.shutdown_grace_period
        )
        cancellation_grace = getattr(
            self, "cancellation_grace_period", self.shutdown_grace_period
        )
        if group is not None:
            if not group.submission_complete and now - observed_at < drain_grace:
                return False
            active_agents = [
                agent_name for agent_name, future in group.futures.items()
                if not future.done()
            ]
            if active_agents and now - observed_at < drain_grace:
                return False
            if active_agents:
                for agent_name in active_agents:
                    token = group.cancellation_tokens.get(agent_name)
                    if token is not None:
                        self._request_cancellation(
                            group, agent_name, requested_at=now,
                        )
                    group.futures[agent_name].cancel()
                with self._execution_state_lock:
                    cancellation_requested_at = tuple(
                        group.cancellation_requested_at.values()
                    )
                cancellation_started_at = min(
                    cancellation_requested_at,
                    default=observed_at + drain_grace,
                )
                if now - cancellation_started_at < cancellation_grace:
                    return False
                active_agents = [
                    agent_name for agent_name, future in group.futures.items()
                    if not future.done()
                ]
                if active_agents:
                    raise ControllerShutdownError(
                        f"judger reached terminal status but task {task_id} remained active "
                        f"for {', '.join(sorted(active_agents))}"
                    )
            if not group.submission_complete:
                raise ControllerShutdownError(
                    f"judger reached terminal status before task {task_id} submission completed"
                )

        self.controller_state = self.STATE_RECONCILING
        payload = dict(self._judger_terminal_payload)
        agent_execution = self._validate_reconciled_agent_execution(group)
        if payload["status"] == "success" and not agent_execution["valid"]:
            feedback = {
                "terminal_source": "external_judger",
                "judger_status": payload["status"],
                "score": payload.get("score"),
                "progress": payload.get("progress", payload.get("score")),
                "attempt_id": payload.get("attempt_id"),
                "task_name": payload.get("task_name"),
                "evidence_consistency": "failed",
                "agent_failures": agent_execution["agent_failures"],
                "agent_execution": agent_execution,
            }
            self.task_manager.mark_task_status(task_id, Task.failure, feedback)
            if group is not None:
                group.task.status = Task.failure
                group.terminal_state_persisted = True
                self._complete_reconciled_group(group)
            else:
                self._release_task_assignments(task_id)
            self._remove_completed_groups()
            self._judger_terminal_reconciled = True
            self.controller_state = self.STATE_SHUTDOWN
            error = JudgedEvidenceConsistencyError(
                "external judger success conflicts with failed agent evidence",
                agent_failures=agent_execution["agent_failures"],
            )
            self._record_failure("judged_evidence_consistency", error)
            self.env.stop()
            raise error
        status = Task.success if payload["status"] == "success" else Task.failure
        feedback = {
            "terminal_source": "external_judger",
            "judger_status": payload["status"],
            "score": payload.get("score"),
            "progress": payload.get("progress", payload.get("score")),
            "attempt_id": payload.get("attempt_id"),
            "task_name": payload.get("task_name"),
            "agent_execution": agent_execution,
        }
        self.task_manager.mark_task_status(task_id, status, feedback)
        if group is not None:
            group.task.status = status
            group.terminal_state_persisted = True
            self._complete_reconciled_group(group)
        else:
            self._release_task_assignments(task_id)
        self._remove_completed_groups()
        self._judger_terminal_reconciled = True
        self.controller_state = self.STATE_SHUTDOWN
        self.env.stop()
        if status == Task.failure:
            self._record_failure(
                "external_judger",
                JudgedTaskFailure(payload),
            )
        else:
            self._request_shutdown()
        return True

    def _validate_reconciled_agent_execution(
        self,
        group: TaskExecutionGroup | None,
    ) -> dict:
        if group is None:
            return {
                "valid": True,
                "drained": True,
                "result_available": False,
                "agent_results": {},
                "agent_failures": {},
            }
        snapshots = {
            name: self._snapshot_future(future)
            for name, future in group.futures.items()
        }
        results = {}
        failures = {}
        for name, snapshot in snapshots.items():
            if snapshot["cancelled"]:
                failures[name] = {
                    "reason": "future_cancelled",
                    "retry_safe": False,
                }
                continue
            exception = snapshot["exception"]
            if exception is not None:
                if isinstance(exception, ToolActionBlockedError):
                    results[name] = {
                        "status": "terminal_blocked",
                        "detail_available": False,
                    }
                    continue
                detail = getattr(exception, "failure_detail", None)
                failures[name] = dict(detail) if isinstance(detail, dict) else {
                    "reason": "future_exception",
                    "error_type": type(exception).__name__,
                    "message": str(exception),
                    "retry_safe": False,
                }
                continue
            result = snapshot["result"]
            if not isinstance(result, tuple) or len(result) != 2:
                failures[name] = {
                    "reason": "malformed_future_result",
                    "retry_safe": False,
                }
                continue
            detail = result[1]
            failure = detail.get("failure") if isinstance(detail, dict) else None
            if isinstance(failure, dict) and (
                failure.get("retry_safe") is False
                or failure.get("reason") in {
                    "minecraft_action_log_error",
                    "minecraft_tool_timeout",
                }
            ):
                failures[name] = dict(failure)
                continue
            results[name] = {
                "status": "completed",
                "detail_available": detail is not None,
            }
        return {
            "valid": not failures,
            "drained": True,
            "result_available": bool(group.futures),
            "agent_results": results,
            "agent_failures": failures,
        }

    def _running_runtime_task_ids(self) -> list[str]:
        runtime_store = getattr(self.task_manager, "runtime_task_store", None)
        if runtime_store is None:
            raise ControllerShutdownError("runtime task DAG store is unavailable")
        return [
            node_id.removeprefix("runtime:task:")
            for node_id, node in runtime_store.nodes.items()
            if node.get("lifecycle", {}).get("status") == Task.running
        ]

    def _execution_groups_snapshot(self) -> list[TaskExecutionGroup]:
        with self.task_list_lock:
            with self.result_list_lock:
                groups = [*self.task_queue, *self.result_queue]
        unique_groups = []
        seen = set()
        for group in groups:
            if id(group) not in seen:
                unique_groups.append(group)
                seen.add(id(group))
        return unique_groups

    def _release_task_assignments(self, task_id: str) -> None:
        for agent_name, assigned_task_id in list(self.assignment.items()):
            if assigned_task_id == task_id:
                self.assignment.pop(agent_name)

    def _complete_reconciled_group(self, group: TaskExecutionGroup) -> None:
        self._release_task_assignments(group.task.id)
        group.post_processing_complete = True
        group.completed = True

    def _remove_completed_groups(self) -> None:
        with self.task_list_lock:
            self.task_queue = [group for group in self.task_queue if not group.completed]
            with self.result_list_lock:
                self.result_queue = [group for group in self.result_queue if not group.completed]

    # worker
    def worker(self):
        while True:
            if self.should_shutdown():
                break
            if self._execution_admission_closed():
                self.shutdown_event.wait(self.query_interval)
                continue
            if self.observe_judger_terminal():
                self.shutdown_event.wait(self.query_interval)
                continue

            # if future.done() and task.id in [t.id for t in self.task_list] and task.status == Task.running:
            if self.env.agents_ping()["status"] == False:
                raise ControllerShutdownError("Some agents are offline")

            if not self._take_and_start_next_execution_group():
                self.shutdown_event.wait(self.query_interval)
                continue

    def set_task_status(self, task_id, status, feedback):
        self.task_manager.mark_task_status(task_id, status, feedback)

    def get_task_by_id(self, task_id):
        for task in self.task_manager.graph.vertex:
            if task.id == task_id:
                return task
        return None
    
    def update_feedback(self, task, agent, detail):
        task.status = Task.success if agent.reflect(task, detail) else Task.failure
        # task.status = Task.success
        self.set_task_status(task.id, task.status, detail)

        for agent in self.agent_list:
            if self.assignment.get(agent.name) == task.id:
                self.assignment.pop(agent.name)
        self.logger.info(
            f"task {task.description} has been executed, the result is {task.status}")
        self.task_manager.feedback_task(self.get_task_by_id(task.id))

        return

    def update_task_status(self, task, status, detail): 
        task.status = status
        self.set_task_status(task.id, status, detail)

        for agent in self.agent_list:
            if self.assignment.get(agent.name) == task.id:
                self.assignment.pop(agent.name)

        self.logger.info(
            f"task {task.description} has been executed, the result is {task.status}")
        self.task_manager.feedback_task(self.get_task_by_id(task.id))

        return

    def _claim_next_result_group(self):
        """Claim one group fairly without removing or copying concurrent appends."""
        with self.result_list_lock:
            queue_size = len(self.result_queue)
            if queue_size == 0:
                return None, None
            start = getattr(self, "_result_claim_cursor", 0) % queue_size
            for offset in range(queue_size):
                index = (start + offset) % queue_size
                group = self.result_queue[index]
                if (
                    group.completed
                    or group.shutdown_reconciled
                    or group.post_processing_claim_token is not None
                ):
                    continue
                token = object()
                group.post_processing_claim_token = token
                self._result_claim_cursor = (index + 1) % queue_size
                return group, token
            return None, None

    def _finish_result_group_claim(
        self,
        group: TaskExecutionGroup,
        token,
        *,
        remove: bool,
    ) -> None:
        with self.result_list_lock:
            if group.post_processing_claim_token is not token:
                raise ControllerShutdownError(
                    f"Task {group.task.description} result claim ownership changed"
                )
            if remove:
                for index, queued_group in enumerate(self.result_queue):
                    if queued_group is group:
                        self.result_queue.pop(index)
                        break
                else:
                    raise ControllerShutdownError(
                        f"Task {group.task.description} disappeared from result_queue"
                    )
            group.post_processing_claim_token = None
        

    def process_completed_tasks(self):
        while True:
            if self.should_shutdown():
                break
            if self.observe_judger_terminal():
                if self.reconcile_judger_terminal():
                    break
                self.shutdown_event.wait(self.query_interval)
                continue

            # if future.done() and task.id in [t.id for t in self.task_list] and task.status == Task.running:
            if self.env.agents_ping()["status"] == False:
                raise ControllerShutdownError("Some agents are offline")

            group, claim_token = self._claim_next_result_group()
            if group is None:
                self.shutdown_event.wait(self.query_interval)
                continue
            completed = False
            try:
                completed = self.finalize_execution_group(group)
                if completed:
                    self.logger.info(f"Task {group.task.description} finished!")
            finally:
                self._finish_result_group_claim(
                    group, claim_token, remove=(completed or group.completed),
                )
            self.shutdown_event.wait(self.query_interval)

                
    def check_task_list_available(self):
        return [
            task for task in self.task_list
            if task.available and task.status == Task.unknown
        ]

    def assign_runnable_tasks(self):
        with self._execution_state_lock:
            if self._execution_admission_closed():
                return 0
            assigned_count = 0
            for task_id, task in enumerate(self.task_list):
                if not task.available or task.status != Task.unknown:
                    continue
                if getattr(task, "_candidate_agents_explicit", False) and not task.candidate_list:
                    raise ValueError(
                        f"Task {task.description} has an explicit empty candidate list"
                    )
                if (
                    isinstance(task.number, bool)
                    or not isinstance(task.number, int)
                    or task.number <= 0
                ):
                    raise ValueError(
                        f"Task {task.description} required agent count must be a positive integer"
                    )

                eligible_agents = [
                    agent
                    for agent in self.agent_list
                    if self.assignment.get(agent.name) is None
                    and agent.name in task.candidate_list
                ]
                selected_agents = eligible_agents[:task.number]
                if len(selected_agents) != task.number:
                    continue

                validated_assignments = self.validate_assignments([{
                    "task_id": task_id,
                    "agent": [agent.name for agent in selected_agents],
                }])
                if not validated_assignments:
                    continue

                self.logger.info(
                    f"Task {task.description} is assigned to {[agent.name for agent in selected_agents]}"
                )
                self.emit_runtime_event("task_selected", entity_id=task.id, source="GlobalController.assign_runnable_tasks", payload={"agents": [agent.name for agent in selected_agents], "selection_policy": getattr(self, "minecraft_dual_dag_config", {}).get("task_selection_policy", "original")})
                assigned_count += self.execute_assignments(validated_assignments)

            return assigned_count

    # 生产者
    def execute_tasks(self):
        while True:
            if self.should_shutdown():
                break
            if self._execution_admission_closed():
                self.shutdown_event.wait(self.query_interval)
                continue
            if self.observe_judger_terminal():
                self.shutdown_event.wait(self.query_interval)
                continue

            # if future.done() and task.id in [t.id for t in self.task_list] and task.status == Task.running:
            if self.env.agents_ping()["status"] == False:
                raise ControllerShutdownError("Some agents are offline")

            open_task_list = self.task_manager.query_subtask_list()
            if open_task_list == []:
                self.logger.info("all assigned tasks are finished ...")
                self._request_shutdown()
                break

            free_agent_names = [
                agent.name for agent in self.agent_list
                if self.assignment.get(agent.name) is None
            ]
            self.task_list = self.task_manager.query_runnable_subtasks(free_agent_names)
            self.task_list = self._rank_task_list_with_minecraft_dual_dag(self.task_list)
            agent_states = []
            for agent in self.agent_list:
                if self.assignment.get(agent.name) is None:
                    agent_states.append({"name": agent.name, "state": "free", "task": None})
                else:
                    tmp_description = ""
                    for task in self.task_list:
                        if task.id == self.assignment.get(agent.name):
                            tmp_description = task.description
                            break
                    agent_states.append({"name": agent.name, "state": "busy", "task": tmp_description})

            runtime_paths = getattr(self.env, "runtime_paths", RuntimePaths.legacy())
            atomic_write_json(runtime_paths.task_list_log, {
                "agent_states": agent_states,
                "task_list": [task.assign_json(idx) for idx, task in enumerate(self.task_list)],
            })

            if self.check_task_list_available() == []:
                # self.logger.info("no available task ...")
                self.shutdown_event.wait(self.query_interval)
                continue

            self.assign_runnable_tasks()

    def _rank_task_list_with_minecraft_dual_dag(self, task_list):
        ranked = rank_minecraft_runtime_tasks(
            task_list,
            graph=getattr(self.task_manager, "graph", None),
            action_log=self.env.get_action_log() if hasattr(self.env, "get_action_log") else None,
            config=self.minecraft_dual_dag_config,
        )
        support = ranked.get("decision_support", {})
        if ranked.get("enabled") and support.get("recommended_task_id"):
            self.logger.info(
                "Dual-DAG recommended task %s for runtime selection",
                support.get("recommended_task_id"),
            )
        self.emit_runtime_event("task_candidates_ranked", source="GlobalController._rank_task_list_with_minecraft_dual_dag", payload={"candidate_task_ids": [task.id for task in task_list], "ranked_task_ids": [task.id for task in ranked.get("tasks", task_list)], "enabled": bool(ranked.get("enabled"))})
        return ranked.get("tasks", task_list)

    def run(self):
        if self._run_started:
            raise ControllerShutdownError("Controller instances cannot be reused after run() starts")
        self._run_started = True
        self.shutdown_event.clear()
        if not hasattr(self, "_post_processing_cancel_event"):
            self._post_processing_cancel_event = threading.Event()
        self._post_processing_cancel_event.clear()
        with self._feedback_persistence_gate_lock():
            self._feedback_persistence_closed = False
        self._first_failure = None
        self.controller_state = self.STATE_RUNNING
        self.shutdown_complete = False
        self.shutdown_diagnostics = None
        self._shutdown_authoritative_verdict = None
        self._controller_threads = [
            threading.Thread(
                name=f"controller-{name}",
                target=self._run_thread,
                args=(name, entrypoint),
                daemon=True,
            )
            for name, entrypoint in (
                ("execute_tasks", self.execute_tasks),
                ("worker", self.worker),
                ("process_completed_tasks", self.process_completed_tasks),
            )
        ]
        started_threads = []
        try:
            for thread in self._controller_threads:
                thread.start()
                started_threads.append(thread)
            self.shutdown_event.wait()
        except BaseException as exc:
            self._record_failure("run", exc)

        self._request_shutdown()
        self.controller_state = self.STATE_SHUTDOWN
        shutdown_started_monotonic = time.monotonic()
        deadline = shutdown_started_monotonic + self.shutdown_grace_period
        movement_cancel_budget = max(0.0, self.shutdown_grace_period / 2.0)
        self.movement_shutdown_result = self._cancel_active_movements_for_shutdown(
            timeout_seconds=movement_cancel_budget,
        )
        self.executor.shutdown(wait=False, cancel_futures=True)
        executor_threads = list(getattr(self.executor, "_threads", ()))
        for thread in [*started_threads, *executor_threads]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)

        authoritative_capture_started_monotonic_ns = time.monotonic_ns()
        authoritative_threads = [*started_threads, *executor_threads]
        authoritative_thread_states = [{
            "name": thread.name,
            "identity": thread.ident,
            "native_identity": getattr(thread, "native_id", None),
            "alive": thread.is_alive(),
        } for thread in authoritative_threads]
        alive_threads = [
            state["name"] for state in authoritative_thread_states
            if state["alive"]
        ]
        (
            interrupted_task_ids,
            active_task_ids,
            active_agent_ids,
            incomplete_submission_task_ids,
            undrained_queues,
        ) = self._finalize_shutdown_groups()
        try:
            frozen_execution_diagnostics = self._freeze_execution_diagnostics()
        except Exception as error:
            frozen_execution_diagnostics = {
                "execution_groups": {
                    "items": [],
                    "retention": self._retention_metadata(
                        self.SHUTDOWN_DIAGNOSTIC_GROUP_LIMIT, 0, 0,
                    ),
                },
                "execution_cancellation": [],
                "controller_history_retention": self._retention_metadata(
                    self.EXECUTION_HISTORY_TOTAL_LIMIT, 0, 0,
                ),
                "diagnostic_collection_error": {
                    "collector": "execution_snapshot",
                    "error_type": type(error).__name__,
                },
            }
        frozen_terminal_barrier = self._freeze_terminal_barrier_context()
        provider_termination_unconfirmed_task_ids = list(getattr(
            self, "_provider_termination_unconfirmed_task_ids", [],
        ))
        shutdown_complete = (
            not alive_threads
            and not active_task_ids
            and not incomplete_submission_task_ids
            and not undrained_queues
            and not provider_termination_unconfirmed_task_ids
            and self.movement_shutdown_result.get("terminal") is True
        )
        shutdown_failure_message = None
        if not shutdown_complete and self._first_failure is None:
            shutdown_failure_message = "Controller shutdown incomplete"
            if alive_threads:
                shutdown_failure_message += (
                    f"; live threads: {', '.join(alive_threads)}"
                )
            if undrained_queues:
                shutdown_failure_message += (
                    f"; undrained queues: {', '.join(undrained_queues)}"
                )
        primary_failure = (
            {
                "thread": self._first_failure[2]["thread"],
                "error_type": self._first_failure[2]["error_type"],
            }
            if self._first_failure is not None
            else {
                "thread": "run",
                "error_type": "ControllerShutdownError",
            } if shutdown_failure_message is not None
            else None
        )
        authoritative_basis = deepcopy({
            "capture_started_monotonic_ns": (
                authoritative_capture_started_monotonic_ns
            ),
            "threads": {
                "items": authoritative_thread_states[
                    :self.SHUTDOWN_DIAGNOSTIC_THREAD_LIMIT
                ],
                "retention": self._retention_metadata(
                    self.SHUTDOWN_DIAGNOSTIC_THREAD_LIMIT,
                    min(
                        len(authoritative_thread_states),
                        self.SHUTDOWN_DIAGNOSTIC_THREAD_LIMIT,
                    ),
                    max(
                        0,
                        len(authoritative_thread_states)
                        - self.SHUTDOWN_DIAGNOSTIC_THREAD_LIMIT,
                    ),
                ),
            },
            "live_threads": list(alive_threads),
            "undrained_queues": list(undrained_queues),
            "interrupted_task_ids": list(interrupted_task_ids),
            "active_task_ids": list(active_task_ids),
            "active_agent_ids": list(active_agent_ids),
            "incomplete_submission_task_ids": list(
                incomplete_submission_task_ids
            ),
            "provider_termination_unconfirmed_task_ids": (
                provider_termination_unconfirmed_task_ids
            ),
            "movement_cancellation": self.movement_shutdown_result,
            "terminal_barrier": frozen_terminal_barrier,
            "primary_failure": primary_failure,
            **frozen_execution_diagnostics,
        })
        authoritative_capture_completed_monotonic_ns = time.monotonic_ns()
        authoritative_basis["capture_completed_monotonic_ns"] = (
            authoritative_capture_completed_monotonic_ns
        )
        authoritative_verdict = {
            "shutdown_complete": shutdown_complete,
            "shutdown_started_monotonic_ns": int(
                shutdown_started_monotonic * 1_000_000_000
            ),
            "deadline_monotonic_ns": int(deadline * 1_000_000_000),
            "verdict_frozen_at_monotonic_ns": (
                authoritative_capture_completed_monotonic_ns
            ),
            "authoritative_basis": authoritative_basis,
        }
        # This value is assigned once. Later diagnostic completion cannot revise it.
        self._shutdown_authoritative_verdict = self._deep_freeze(
            deepcopy(authoritative_verdict)
        )
        self.shutdown_complete = shutdown_complete
        if shutdown_failure_message is not None:
            error = ControllerShutdownError(shutdown_failure_message)
            # Shutdown is already requested. A direct GIL-serialized assignment
            # avoids any post-deadline lock acquisition before stack capture.
            if self._first_failure is None:
                self._first_failure = (error, error.__traceback__, {
                    "thread": "run",
                    "error": str(error),
                    "error_type": type(error).__name__,
                    "traceback": "",
                })
        self.shutdown_context = {
            "shutdown_complete": shutdown_complete,
            "controller_state": self.controller_state,
            "live_threads": alive_threads,
            "undrained_queues": undrained_queues,
            "interrupted_task_ids": interrupted_task_ids,
            "active_task_ids": active_task_ids,
            "active_agent_ids": active_agent_ids,
            "incomplete_submission_task_ids": incomplete_submission_task_ids,
            "post_processing_interrupted": list(
                getattr(self, "_post_processing_interruption_ledger", {}).values()
            ),
            "feedback_interrupted": list(
                getattr(self, "_feedback_interruption_ledger", {}).values()
            ),
            "provider_termination_unconfirmed_task_ids": (
                provider_termination_unconfirmed_task_ids
            ),
            "terminal_barrier": frozen_terminal_barrier,
            "movement_cancellation": self.movement_shutdown_result,
            "execution_cancellation": frozen_execution_diagnostics[
                "execution_cancellation"
            ],
        }
        try:
            execution_ids_by_thread = {}
            for execution_group in frozen_execution_diagnostics[
                "execution_groups"
            ]["items"]:
                for execution in execution_group["executions"]["items"]:
                    if execution["future"]["done"]:
                        continue
                    started = next((
                        item for item in execution["lifecycle"]["items"]
                        if item["event"] == "future_started"
                    ), None)
                    if started is not None:
                        execution_ids_by_thread.setdefault(
                            started.get("thread_identity"), []
                        ).append(execution["execution_id"])
            diagnostic_threads, thread_enumeration = (
                self._bounded_shutdown_diagnostic_threads(
                    [
                        thread for thread, state in zip(
                            authoritative_threads, authoritative_thread_states,
                        ) if state["alive"]
                    ]
                )
            )
            post_verdict_diagnostics = self._capture_post_verdict_thread_stacks(
                diagnostic_threads,
                execution_ids_by_thread=execution_ids_by_thread,
            )
            post_verdict_diagnostics.update(thread_enumeration)
        except Exception as error:
            post_verdict_diagnostics = {
                "captured_at_monotonic_ns": time.monotonic_ns(),
                "threads": {
                    "items": [],
                    "retention": self._retention_metadata(
                        self.SHUTDOWN_DIAGNOSTIC_THREAD_LIMIT, 0, 0,
                    ),
                },
                "diagnostic_collection_error": {
                    "collector": "thread_stacks",
                    "error_type": type(error).__name__,
                },
            }
        post_verdict_diagnostics["tool_runtime"] = (
            self._bounded_tool_runtime_context()
        )
        self.shutdown_context["tool_runtime"] = post_verdict_diagnostics[
            "tool_runtime"
        ]
        self.shutdown_diagnostics = {
            "schema_version": "controller-shutdown-diagnostics/2",
            "verdict": authoritative_verdict,
            "post_verdict": post_verdict_diagnostics,
        }
        self.shutdown_context["diagnostics"] = self.shutdown_diagnostics
        if not shutdown_complete:
            self._first_failure[2].update(self.shutdown_context)
            setattr(
                self._first_failure[0],
                "controller_shutdown_context",
                dict(self.shutdown_context),
            )

        try:
            self.task_manager.checkpoint_runtime_state(raise_on_error=True)
        except BaseException as exc:
            if self._first_failure is not None:
                self._first_failure[2]["checkpoint_error"] = {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
            self._record_failure("run.checkpoint", exc)

        if self._first_failure is None:
            if self.emit_terminal_events:
                self.emit_runtime_event(
                    "run_completed",
                    source="GlobalController.run",
                )
            return

        exc, exc_traceback, failure = self._first_failure
        if self.emit_terminal_events:
            self.emit_runtime_event(
                "run_failed",
                source="GlobalController.run",
                payload=failure,
            )
        raise exc.with_traceback(exc_traceback)

    def _cancel_active_movements_for_shutdown(self, *, timeout_seconds: float) -> dict:
        cancel = getattr(getattr(self, "env", None), "cancel_active_movements", None)
        if not callable(cancel):
            return {"state": "not_supported", "terminal": True}
        try:
            result = cancel(
                reason="controller_shutdown", timeout_seconds=timeout_seconds,
            )
        except Exception as error:
            return {
                "state": "request_failed",
                "terminal": False,
                "error_class": type(error).__name__,
            }
        if not isinstance(result, dict):
            return {"state": "invalid_result", "terminal": False}
        return result

    def _execution_cancellation_context(self) -> list[dict]:
        return [{
            "task_id": group.task.id,
            "requested": sorted(group.cancellation_requested),
            "acknowledged": sorted(group.cancellation_acknowledged),
            "phases": dict(group.cancellation_phases),
            "active_agents": [name for name, future in group.futures.items()
                              if not future.done()],
        } for group in self._all_execution_groups()]

    @staticmethod
    def _retention_metadata(capacity: int, retained: int, dropped_count: int) -> dict:
        return {
            "capacity": capacity,
            "retained": retained,
            "truncated": dropped_count > 0,
            "dropped_count": dropped_count,
        }

    @staticmethod
    def _deep_freeze(value):
        if isinstance(value, dict):
            return MappingProxyType({
                key: GlobalController._deep_freeze(item)
                for key, item in value.items()
            })
        if isinstance(value, list):
            return tuple(GlobalController._deep_freeze(item) for item in value)
        return value

    @staticmethod
    def _diagnostic_execution_state(group, agent_name: str) -> dict:
        started = group.execution_started_markers.get(agent_name)
        completed = group.execution_completion_markers.get(agent_name)
        return {
            "done": completed is not None,
            "running": started is not None and completed is None,
            "cancelled_before_start": completed is not None and started is None,
        }

    def _freeze_execution_diagnostics(self, *, blocking=False) -> dict:
        """Copy primitive execution state while holding its existing state lock."""
        if not self._execution_state_lock.acquire(blocking=blocking):
            return {
                "execution_groups": {
                    "items": [],
                    "retention": self._retention_metadata(
                        self.SHUTDOWN_DIAGNOSTIC_GROUP_LIMIT, 0, 0,
                    ),
                },
                "execution_cancellation": [],
                "controller_history_retention": self._retention_metadata(
                    self.EXECUTION_HISTORY_TOTAL_LIMIT, 0, 0,
                ),
                "diagnostic_collection_error": {
                    "collector": "execution_snapshot",
                    "error_type": "ExecutionStateLockUnavailable",
                },
            }
        try:
            groups = []
            selected_groups, truncated_groups = self._bounded_diagnostic_groups()
            for group in selected_groups:
                all_agent_names = sorted(
                    set(group.execution_ids)
                    | set(group.futures)
                    | set(group.cancellation_tokens)
                    | set(group.phase_history)
                )
                retained_agent_names = all_agent_names[:self.EXECUTION_ACTOR_LIMIT]
                executions = []
                for name in retained_agent_names:
                    lifecycle = [
                        dict(entry) for entry in group.phase_history.get(name, [])
                    ]
                    requested_at_monotonic_ns = next((
                        entry["monotonic_ns"] for entry in reversed(lifecycle)
                        if entry["event"] == "token_requested"
                    ), None)
                    for marker in (
                        group.execution_started_markers.get(name),
                        group.execution_completion_markers.get(name),
                    ):
                        if marker is not None:
                            lifecycle.append(dict(marker))
                    lifecycle.sort(key=lambda entry: entry["monotonic_ns"])
                    lifecycle_dropped = group.phase_history_truncated.get(name, 0)
                    if len(lifecycle) > self.EXECUTION_LIFECYCLE_LIMIT:
                        overflow = len(lifecycle) - self.EXECUTION_LIFECYCLE_LIMIT
                        lifecycle = lifecycle[-self.EXECUTION_LIFECYCLE_LIMIT:]
                        lifecycle_dropped += overflow
                    token = group.cancellation_tokens.get(name)
                    executions.append({
                        "execution_id": group.execution_ids.get(name),
                        "task_id": group.task.id,
                        "actor_id": name,
                        "future": self._diagnostic_execution_state(group, name),
                        "token": {
                            "requested": bool(token is not None and token.is_set()),
                            "requested_at_monotonic_ns": (
                                requested_at_monotonic_ns
                            ),
                            "requested_at_wall_time": group.cancellation_requested_at.get(name),
                            "acknowledged": name in group.cancellation_acknowledged,
                        },
                        "latest_phase": group.cancellation_phases.get(name),
                        "lifecycle": {
                            "items": lifecycle,
                            "retention": self._retention_metadata(
                                self.EXECUTION_LIFECYCLE_LIMIT,
                                len(lifecycle),
                                lifecycle_dropped,
                            ),
                        },
                    })
                groups.append({
                    "task_id": group.task.id,
                    "submission_complete": bool(group.submission_complete),
                    "executions": {
                        "items": executions,
                        "retention": self._retention_metadata(
                            self.EXECUTION_ACTOR_LIMIT,
                            len(executions),
                            len(all_agent_names) - len(executions),
                        ),
                    },
                })
            execution_cancellation = []
            for group in groups:
                executions = group["executions"]["items"]
                execution_cancellation.append({
                    "task_id": group["task_id"],
                    "requested": sorted(
                        item["actor_id"] for item in executions
                        if item["token"]["requested"]
                    ),
                    "acknowledged": sorted(
                        item["actor_id"] for item in executions
                        if item["token"]["acknowledged"]
                    ),
                    "phases": {
                        item["actor_id"]: item["latest_phase"]
                        for item in executions if item["latest_phase"] is not None
                    },
                    "active_agents": sorted(
                        item["actor_id"] for item in executions
                        if not item["future"]["done"]
                    ),
                })
            retained_history = len(getattr(self, "_execution_history_index", ()))
            dropped_history = getattr(
                self, "_execution_history_dropped_count", 0,
            )
            return {
                "execution_groups": {
                    "items": groups,
                    "retention": self._retention_metadata(
                        self.SHUTDOWN_DIAGNOSTIC_GROUP_LIMIT,
                        len(groups),
                        truncated_groups,
                    ),
                },
                "execution_cancellation": execution_cancellation,
                "controller_history_retention": self._retention_metadata(
                    self.EXECUTION_HISTORY_TOTAL_LIMIT,
                    retained_history,
                    dropped_history,
                ),
            }
        finally:
            self._execution_state_lock.release()

    def snapshot_execution_ledger(self) -> dict:
        """Return a read-only, non-waiting execution ledger for diagnostics."""
        if not self._execution_state_lock.acquire(blocking=False):
            return {
                "schema_version": "controller-late-execution-ledger/1",
                "captured_at_monotonic_ns": time.monotonic_ns(),
                "groups": {"items": [], "retention": self._retention_metadata(
                    self.SHUTDOWN_DIAGNOSTIC_GROUP_LIMIT, 0, 0,
                )},
                "diagnostic_collection_error": {
                    "collector": "execution_ledger",
                    "error_type": "ExecutionStateLockUnavailable",
                },
            }
        try:
            selected, group_dropped = self._bounded_diagnostic_groups()
            group_refs = []
            for group in selected:
                names = sorted(set(group.execution_ids) | set(group.futures)
                               | set(group.phase_history)
                               | set(group.cancellation_tokens))
                retained_names = names[:self.EXECUTION_ACTOR_LIMIT]
                group_refs.append({
                    "task_id": group.task.id,
                    "completed": bool(group.completed),
                    "submission_complete": bool(group.submission_complete),
                    "terminal_state_persisted": bool(group.terminal_state_persisted),
                    "post_processing_complete": bool(group.post_processing_complete),
                    "shutdown_reconciled": bool(group.shutdown_reconciled),
                    "assignments_released": bool(group.assignments_released),
                    "names": retained_names,
                    "dropped": len(names) - len(retained_names),
                    "futures": {name: group.futures.get(name) for name in retained_names},
                    "ids": {name: group.execution_ids.get(name) for name in retained_names},
                    "started": {name: dict(group.execution_started_markers[name])
                                for name in retained_names
                                if isinstance(group.execution_started_markers.get(name), dict)},
                    "completed_markers": {name: dict(group.execution_completion_markers[name])
                                          for name in retained_names
                                          if isinstance(group.execution_completion_markers.get(name), dict)},
                    "phases": {name: [dict(entry) for entry in group.phase_history.get(name, [])]
                               for name in retained_names},
                    "phase_dropped": {name: group.phase_history_truncated.get(name, 0)
                                      for name in retained_names},
                    "latest_phase": {name: group.cancellation_phases.get(name)
                                     for name in retained_names},
                    "requested": set(group.cancellation_requested),
                    "acknowledged": set(group.cancellation_acknowledged),
                    "requested_wall": dict(group.cancellation_requested_at),
                })
        except Exception as exc:
            return {
                "schema_version": "controller-late-execution-ledger/1",
                "captured_at_monotonic_ns": time.monotonic_ns(),
                "groups": {"items": [], "retention": self._retention_metadata(
                    self.SHUTDOWN_DIAGNOSTIC_GROUP_LIMIT, 0, 0,
                )},
                "diagnostic_collection_error": {
                    "collector": "execution_ledger",
                    "error_type": type(exc).__name__,
                },
            }
        finally:
            self._execution_state_lock.release()

        groups = []
        errors = []
        for ref in group_refs:
            executions = []
            for name in ref["names"]:
                future = ref["futures"].get(name)
                future_state = {"done": False, "cancelled": False, "running": False}
                if future is None:
                    errors.append({"collector": "execution_ledger", "error_type": "MalformedFuture"})
                else:
                    try:
                        future_state = {"done": bool(future.done()),
                                        "cancelled": bool(future.cancelled()),
                                        "running": bool(future.running())}
                    except Exception as exc:
                        errors.append({"collector": "execution_ledger", "error_type": type(exc).__name__})
                lifecycle = list(ref["phases"].get(name, []))
                lifecycle.extend([ref["started"][name]] if name in ref["started"] else [])
                lifecycle.extend([ref["completed_markers"][name]] if name in ref["completed_markers"] else [])
                lifecycle.sort(key=lambda item: item.get("monotonic_ns", 0))
                dropped = ref["phase_dropped"].get(name, 0)
                if len(lifecycle) > self.EXECUTION_LIFECYCLE_LIMIT:
                    dropped += len(lifecycle) - self.EXECUTION_LIFECYCLE_LIMIT
                    lifecycle = lifecycle[-self.EXECUTION_LIFECYCLE_LIMIT:]
                requested_ns = next((entry.get("monotonic_ns") for entry in reversed(lifecycle)
                                     if entry.get("event") == "token_requested"),
                                    None)
                acknowledged_ns = next((entry.get("monotonic_ns") for entry in reversed(lifecycle)
                                        if entry.get("event") == "token_acknowledged"),
                                       None)
                executions.append({
                    "execution_id": ref["ids"].get(name), "task_id": ref["task_id"], "actor_id": name,
                    "future": future_state,
                    "future_started": ref["started"].get(name),
                    "future_completed": ref["completed_markers"].get(name),
                    "cancellation": {
                        "requested": name in ref["requested"],
                        "acknowledged": name in ref["acknowledged"],
                        "requested_at_monotonic_ns": requested_ns,
                        "acknowledged_at_monotonic_ns": acknowledged_ns,
                        "requested_at_wall_time": ref["requested_wall"].get(name),
                    },
                    "latest_phase": ref["latest_phase"].get(name),
                    "lifecycle": {"items": lifecycle, "retention": self._retention_metadata(
                        self.EXECUTION_LIFECYCLE_LIMIT, len(lifecycle), dropped,
                    )},
                })
            groups.append({
                "task_id": ref["task_id"],
                "executions": {"items": executions, "retention": self._retention_metadata(
                    self.EXECUTION_ACTOR_LIMIT, len(executions), ref["dropped"],
                )},
                "reconciliation": {"group_completed": ref["completed"],
                    "shutdown_reconciled": ref["shutdown_reconciled"],
                    "assignments_released": ref["assignments_released"],
                    "terminal_state_persisted": ref["terminal_state_persisted"],
                    "post_processing_complete": ref["post_processing_complete"],
                    "execution_terminal_reconciled": bool(executions) and all(
                        item["future"]["done"] is True
                        and item["future"]["running"] is False
                        and isinstance(item["future_completed"], dict)
                        and item["future_completed"].get("execution_id")
                        == item["execution_id"]
                        for item in executions
                    ),
                },
            })
        result = {"schema_version": "controller-late-execution-ledger/1",
                  "captured_at_monotonic_ns": time.monotonic_ns(),
                  "groups": {"items": groups, "retention": self._retention_metadata(
                      self.SHUTDOWN_DIAGNOSTIC_GROUP_LIMIT, len(groups), group_dropped,
                  )}}
        if errors:
            result["diagnostic_collection_error"] = errors
        return result

    def _freeze_terminal_barrier_context(self) -> dict:
        if not self._execution_state_lock.acquire(blocking=False):
            return {
                "diagnostic_collection_error": {
                    "collector": "terminal_barrier",
                    "error_type": "ExecutionStateLockUnavailable",
                },
            }
        try:
            return {
                "pending": self._judger_terminal_pending,
                "observed": self._judger_terminal_observed,
                "detected_at": self._judger_terminal_detected_at,
                "active_tool_actions": self._active_tool_actions,
                "tool_drain_timed_out": self._tool_drain_timed_out,
            }
        finally:
            self._execution_state_lock.release()

    def _bounded_tool_runtime_context(self) -> dict:
        snapshot = getattr(
            getattr(self, "env", None),
            "get_tool_runtime_context_snapshot",
            None,
        )
        if not callable(snapshot):
            return {
                "diagnostic_collection_error": {
                    "collector": "tool_runtime",
                    "error_type": "NonBlockingSnapshotUnavailable",
                },
            }
        try:
            context = snapshot()
        except Exception as error:
            return {
                "diagnostic_collection_error": {
                    "collector": "tool_runtime",
                    "error_type": type(error).__name__,
                },
            }
        return context if isinstance(context, dict) else {}

    def _bounded_shutdown_diagnostic_threads(self, base_threads):
        threads = list(base_threads)
        enumeration_error = None
        if self._execution_state_lock.acquire(blocking=False):
            try:
                groups, _ = self._bounded_diagnostic_groups()
                for group in groups:
                    threads.extend(tuple(group.reflection_workers.values()))
                    if group.feedback_worker is not None:
                        threads.append(group.feedback_worker)
            except RuntimeError:
                enumeration_error = {
                    "collector": "post_processing_threads",
                    "error_type": "ConcurrentThreadRegistryMutation",
                }
            finally:
                self._execution_state_lock.release()
        else:
            enumeration_error = {
                "collector": "post_processing_threads",
                "error_type": "ExecutionStateLockUnavailable",
            }
        truncated = max(0, len(threads) - self.SHUTDOWN_DIAGNOSTIC_THREAD_LIMIT)
        retained = min(len(threads), self.SHUTDOWN_DIAGNOSTIC_THREAD_LIMIT)
        result = {
            "thread_candidates_retention": self._retention_metadata(
                self.SHUTDOWN_DIAGNOSTIC_THREAD_LIMIT, retained, truncated,
            ),
        }
        if enumeration_error is not None:
            result["thread_enumeration_error"] = enumeration_error
        return threads[:self.SHUTDOWN_DIAGNOSTIC_THREAD_LIMIT], result

    @staticmethod
    def _sanitized_stack_path(filename: str) -> str:
        source_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        absolute = os.path.abspath(filename)
        try:
            if os.path.commonpath((source_root, absolute)) == source_root:
                return os.path.relpath(absolute, source_root)
        except ValueError:
            pass
        return os.path.basename(absolute)

    @staticmethod
    def _capture_post_verdict_thread_stacks(
        threads, *, execution_ids_by_thread=None,
    ) -> dict:
        """Capture bounded stack coordinates without locals or source text."""
        captured_at_monotonic_ns = time.monotonic_ns()
        current_frames = sys._current_frames()
        thread_states = [
            (thread, thread.ident, thread.is_alive()) for thread in threads
        ]
        snapshots = []
        seen = set()
        execution_ids_by_thread = execution_ids_by_thread or {}
        for thread, identity, alive_at_capture in thread_states:
            key = (thread.name, identity)
            if key in seen:
                continue
            seen.add(key)
            frame = current_frames.get(identity) if alive_at_capture else None
            stack = []
            dropped_frames = 0
            if frame is not None:
                frames = []
                current = frame
                total_frames = 0
                while current is not None:
                    if len(frames) < 64:
                        frames.append(current)
                    total_frames += 1
                    current = current.f_back
                dropped_frames = max(0, total_frames - 64)
                stack = [{
                    "file": GlobalController._sanitized_stack_path(
                        item.f_code.co_filename
                    ),
                    "line": item.f_lineno,
                    "function": item.f_code.co_name,
                } for item in reversed(frames[:64])]
            snapshots.append({
                "name": thread.name,
                "identity": identity,
                "native_identity": getattr(thread, "native_id", None),
                "alive_at_capture": alive_at_capture,
                "stack_state": (
                    "captured" if frame is not None
                    else "frame_unavailable" if alive_at_capture
                    else "thread_not_alive"
                ),
                "execution_ids": {
                    "items": list(execution_ids_by_thread.get(identity, ()))[:64],
                    "retention": GlobalController._retention_metadata(
                        64,
                        min(len(execution_ids_by_thread.get(identity, ())), 64),
                        max(
                            0,
                            len(execution_ids_by_thread.get(identity, ())) - 64,
                        ),
                    ),
                },
                "stack": {
                    "items": stack,
                    "retention": GlobalController._retention_metadata(
                        64, len(stack), dropped_frames,
                    ),
                },
            })
        return {
            "captured_at_monotonic_ns": captured_at_monotonic_ns,
            "threads": {
                "items": snapshots,
                "retention": GlobalController._retention_metadata(
                    GlobalController.SHUTDOWN_DIAGNOSTIC_THREAD_LIMIT,
                    len(snapshots),
                    max(0, len(threads) - len(snapshots)),
                ),
            },
            "redaction": "coordinates_only_no_locals_no_source_text",
        }

    def _terminal_barrier_context(self) -> dict:
        with self._tool_action_condition:
            return {
                "pending": self._judger_terminal_pending,
                "observed": self._judger_terminal_observed,
                "detected_at": self._judger_terminal_detected_at,
                "active_tool_actions": self._active_tool_actions,
                "tool_drain_timed_out": self._tool_drain_timed_out,
            }

    def _tool_runtime_context(self) -> dict:
        collector = getattr(
            getattr(self, "env", None),
            "get_tool_runtime_context",
            None,
        )
        if not callable(collector):
            return {}
        try:
            context = collector()
        except Exception as exc:
            return {"collection_error": str(exc)}
        return context if isinstance(context, dict) else {}

    def _finalize_shutdown_groups(self):
        interrupted_task_ids = []
        active_task_ids = []
        active_agent_ids = []
        incomplete_submission_task_ids = []
        undrained_queues = []
        groups = []
        for name, lock, queue in (
            ("task_queue", self.task_list_lock, self.task_queue),
            ("result_queue", self.result_list_lock, self.result_queue),
        ):
            if not lock.acquire(blocking=False):
                undrained_queues.append(name)
                continue
            try:
                groups.extend((name, group) for group in queue)
            finally:
                lock.release()

        for queue_name, group in groups:
            if group.completed or group.terminal_state_persisted:
                continue
            if group.post_processing_claim_token is not None:
                if queue_name not in undrained_queues:
                    undrained_queues.append(queue_name)
                active_task_ids.append(group.task.id)
                active_agent_ids.extend(agent.name for agent in group.agents)
                continue
            if group.post_processing_interrupted:
                interruption = dict(group.post_processing_interruption)
                termination_confirmed = interruption.get(
                    "provider_termination_confirmed"
                ) is True
                feedback = {
                    "reason": "post_processing_interrupted",
                    "completed_agent_execution": all(
                        future.done() for future in group.futures.values()
                    ),
                    "reflection_incomplete": True,
                    "reflection_started": sorted(group.reflection_started),
                    "reflection_completed": sorted(group.reflection_completed),
                    "provider_termination_confirmed": termination_confirmed,
                    "interruption": interruption,
                    "agent_reuse_blocked": True,
                    "requires_agent_reconciliation": True,
                }
                try:
                    self.task_manager.mark_task_status(
                        group.task.id,
                        Task.running,
                        feedback,
                    )
                except BaseException as exc:
                    if queue_name not in undrained_queues:
                        undrained_queues.append(queue_name)
                    self._record_failure("run.finalize_shutdown", exc)
                    continue
                group.shutdown_reconciled = True
                ledger = getattr(
                    self, "_post_processing_interruption_ledger", None,
                )
                if ledger is None:
                    ledger = self._post_processing_interruption_ledger = {}
                ledger[group.task.id] = {
                    "task_id": group.task.id,
                    **feedback,
                }
                if not termination_confirmed:
                    unconfirmed = getattr(
                        self, "_provider_termination_unconfirmed_task_ids", None,
                    )
                    if unconfirmed is None:
                        unconfirmed = self._provider_termination_unconfirmed_task_ids = []
                    if group.task.id not in unconfirmed:
                        unconfirmed.append(group.task.id)
                    active_task_ids.append(group.task.id)
                    active_agent_ids.extend(agent.name for agent in group.agents)
                continue
            try:
                with self._execution_state_lock:
                    execution_may_still_be_active = any(
                        future.running() for future in group.futures.values()
                    )
                    for agent_name, future in group.futures.items():
                        if (agent_name in group.cancellation_requested
                                and future.done()
                                and self._is_cancellation_acknowledgement(
                                    self._snapshot_future(future))):
                            self._acknowledge_cancellation(group, agent_name)
                    agent_names = [agent.name for agent in group.agents]
                    submitted_agent_names = list(group.futures)
                    active_group_agents = [
                        agent_name
                        for agent_name, future in group.futures.items()
                        if future.running()
                    ]
                    submission_complete = group.submission_complete
                    cancellation_requested = set(group.cancellation_requested)
                    cancellation_acknowledged = set(
                        group.cancellation_acknowledged
                    )
                    cancellation_phases = dict(group.cancellation_phases)
                    timeout_detected = set(group.timeout_detected)
                    shutdown_escalated = set(group.shutdown_escalated)
                    cancellation_forced = set(group.cancellation_forced)
                    timeout_details = deepcopy(group.timeout_details)
                    terminal_snapshots = {
                        agent_name: self._snapshot_future(future)
                        for agent_name, future in group.futures.items()
                    }
                feedback = {
                    "reason": "controller_shutdown",
                    "execution_may_still_be_active": execution_may_still_be_active,
                    "assigned_agents": agent_names,
                    "submitted_agents": submitted_agent_names,
                    "active_agents": active_group_agents,
                    "unsubmitted_agents": [
                        agent_name for agent_name in agent_names
                        if agent_name not in submitted_agent_names
                    ],
                    "submission_complete": submission_complete,
                    "agent_reuse_blocked": True,
                    "requires_agent_reconciliation": True,
                    "cancellation_requested": sorted(cancellation_requested),
                    "cancellation_acknowledged": sorted(cancellation_acknowledged),
                    "cancellation_phases": cancellation_phases,
                    "blocking_operation_termination": (
                        "unconfirmed" if execution_may_still_be_active
                        else ("confirmed" if cancellation_acknowledged
                              else "not_active")
                    ),
                }
                if timeout_detected:
                    feedback.update({
                        "timeout_detected": sorted(timeout_detected),
                        "shutdown_escalated": sorted(shutdown_escalated),
                        "cancellation_requested": sorted(cancellation_requested),
                        "cancellation_acknowledged": sorted(cancellation_acknowledged),
                        "cancellation_forced": sorted(cancellation_forced),
                        "timeout_details": timeout_details,
                        "cancellation_phases": cancellation_phases,
                    })
                clean_interruption = (
                    not execution_may_still_be_active
                    and submission_complete
                    and all(
                        snapshot["done"] and snapshot["exception"] is None
                        for snapshot in terminal_snapshots.values()
                    )
                )
                if clean_interruption:
                    cancellation_confirmed = (
                        bool(cancellation_requested)
                        and cancellation_requested.issubset(
                            cancellation_acknowledged
                        )
                    )
                    feedback["reason"] = (
                        "controller_shutdown_cancelled"
                        if cancellation_confirmed
                        else "controller_shutdown_interrupted"
                    )
                    self.task_manager.mark_task_status(group.task.id, Task.running, feedback)
                    group.task.status = Task.running
                    group.shutdown_reconciled = True
                    group.post_processing_complete = True
                    group.completed = True
                elif shutdown_escalated and execution_may_still_be_active:
                    feedback["reason"] = "task_timeout_shutdown_escalation"
                    if not group.timeout_checkpoint_persisted:
                        self.task_manager.mark_task_status(group.task.id, Task.running, feedback)
                        group.timeout_checkpoint_persisted = True
                else:
                    self.task_manager.mark_task_status(group.task.id, Task.failure, feedback)
                    group.task.status = Task.failure
                    group.terminal_state_persisted = True
                    group.post_processing_complete = True
                    group.completed = True
                interrupted_task_ids.append(group.task.id)
                if execution_may_still_be_active:
                    active_task_ids.append(group.task.id)
                    active_agent_ids.extend(active_group_agents)
                if not submission_complete:
                    incomplete_submission_task_ids.append(group.task.id)
            except BaseException as exc:
                if queue_name not in undrained_queues:
                    undrained_queues.append(queue_name)
                self._record_failure("run.finalize_shutdown", exc)
        with self.result_list_lock:
            for index in range(len(self.result_queue) - 1, -1, -1):
                if self.result_queue[index].shutdown_reconciled:
                    self.result_queue.pop(index)
        return (
            interrupted_task_ids,
            active_task_ids,
            active_agent_ids,
            incomplete_submission_task_ids,
            undrained_queues,
        )
