"""Small, cooperative movement runtime for event-loop based bot adapters.

The runtime deliberately does not own a thread or a background worker.  A
movement owns one lease, and all of its work (including cleanup) is complete
when :meth:`move` returns.
"""

import asyncio
import contextvars
import inspect
import math
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

try:
    from env.movement_diagnostics import (
        EUCLIDEAN_DISTANCE,
        STRICT_PER_AXIS,
        evaluate_movement_completion,
    )
except ImportError:
    from movement_diagnostics import (
        EUCLIDEAN_DISTANCE,
        STRICT_PER_AXIS,
        evaluate_movement_completion,
    )


AUTHORITATIVE_MOVEMENT_DEADLINE_SECONDS = 9.0
MOVEMENT_DEADLINE_SECONDS = AUTHORITATIVE_MOVEMENT_DEADLINE_SECONDS
CLEANUP_DEADLINE_SECONDS = 0.5
MOVEMENT_REQUEST_DEADLINE_SECONDS = 10.0
_MOVEMENT_REQUEST_DEADLINE = contextvars.ContextVar(
    "movement_request_deadline", default=None,
)


class MovementOverlapError(RuntimeError):
    """Raised when a coordinator already has an active movement lease."""


class MovementAdmissionClosedError(RuntimeError):
    """Raised after cleanup uncertainty permanently closes local admission."""


class MovementRequestDeadlineError(TimeoutError):
    """Raised after the authoritative whole-request movement budget expires."""


class CooperativeMovementError(RuntimeError):
    """Carries a coordinated movement failure through legacy helper layers."""

    def __init__(self, message, *, reason, status_code):
        super().__init__(message)
        self.reason = reason
        self.status_code = int(status_code)


class MovementEffectUnknownError(CooperativeMovementError):
    """Carries an explicit retry-unsafe HTTP effect boundary."""

    def __init__(self, message: str, *, reason: str, status_code: int,
                 terminal: bool):
        super().__init__(message, reason=reason, status_code=status_code)
        self.terminal = bool(terminal)


def movement_request_remaining(clock: Callable[[], float] = time.monotonic) -> Optional[float]:
    deadline = _MOVEMENT_REQUEST_DEADLINE.get()
    if deadline is None:
        return None
    return max(0.0, float(deadline) - float(clock()))


def movement_lease_deadline(default_seconds: float, *,
                            cleanup_reserve_seconds: float = CLEANUP_DEADLINE_SECONDS,
                            clock: Callable[[], float] = time.monotonic) -> float:
    remaining = movement_request_remaining(clock)
    if remaining is None:
        return max(0.0, float(default_seconds))
    return max(0.0, min(
        float(default_seconds), remaining - float(cleanup_reserve_seconds),
    ))


async def run_with_movement_request_budget(awaitable, *,
                                           timeout_seconds: float = MOVEMENT_REQUEST_DEADLINE_SECONDS,
                                           clock: Callable[[], float] = time.monotonic):
    timeout_seconds = max(0.0, float(timeout_seconds))
    token = _MOVEMENT_REQUEST_DEADLINE.set(float(clock()) + timeout_seconds)
    try:
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
        except asyncio.TimeoutError as error:
            raise MovementRequestDeadlineError(
                "movement-capable request deadline exceeded"
            ) from error
    finally:
        _MOVEMENT_REQUEST_DEADLINE.reset(token)


@dataclass
class MovementResult:
    success: bool
    reason: str
    metadata: dict
    message: str

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def __iter__(self):
        """Permit the conventional ``ok, message = await move(...)`` use."""
        yield self.success
        yield self.message

    def as_dict(self) -> dict:
        return {"success": self.success, "reason": self.reason, "metadata": self.metadata, "message": self.message}


def _field(value: Any, name: str) -> float:
    if isinstance(value, dict):
        return float(value[name])
    return float(getattr(value, name))


def _distance(current: Any, target: Any) -> float:
    return math.sqrt(sum((_field(current, axis) - _field(target, axis)) ** 2 for axis in ("x", "y", "z")))


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class MovementCoordinator:
    """Per-bot, single-lease cooperative movement coordinator."""

    def __init__(
        self,
        bot: Any,
        pathfinder: Any = None,
        *,
        deadline_seconds: float = AUTHORITATIVE_MOVEMENT_DEADLINE_SECONDS,
        poll_interval_seconds: float = 0.1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.bot = bot
        self.pathfinder_module = pathfinder
        self.bot_pathfinder = getattr(bot, "pathfinder", None)
        self.deadline_seconds = float(deadline_seconds)
        self.poll_interval_seconds = max(0.0, float(poll_interval_seconds))
        self._clock = clock
        self._sleep = sleep
        self._active_task: Optional[asyncio.Task] = None
        self._cancel_event: Optional[asyncio.Event] = None
        self._cancel_reason: Optional[str] = None
        self._active_metadata: Optional[dict] = None
        self._cleanup_complete: Optional[asyncio.Event] = None
        self._last_result: Optional[MovementResult] = None
        self._admission_closed = False
        self._admission_closed_reason: Optional[str] = None

    @property
    def active(self) -> bool:
        return self._active_task is not None and not self._active_task.done()

    @property
    def admission_closed(self) -> bool:
        return self._admission_closed

    def request_cancel(self, reason: str = "cancelled") -> bool:
        if self._cancel_event is None or not self.active:
            return False
        self._cancel_reason = reason
        self._cancel_event.set()
        return True

    cancel = request_cancel

    def snapshot(self) -> dict:
        metadata = self._active_metadata or {}
        return {
            "active": self.active,
            "admission_closed": self._admission_closed,
            "terminal": not self._admission_closed and not self.active,
            **({"admission_closed_reason": self._admission_closed_reason}
               if self._admission_closed_reason else {}),
            **{key: metadata[key] for key in (
                "movement_id", "correlation_id", "operation", "target_identity",
            ) if key in metadata},
        }

    @property
    def last_result(self) -> Optional[MovementResult]:
        return self._last_result

    async def cancel_active(self, timeout_seconds: float = 1.0,
                            reason: str = "cancelled") -> dict:
        """Request cancellation and wait a bounded time for owned work to end."""
        task = self._active_task
        if task is None:
            return {
                "active": False,
                "cancel_requested": False,
                "terminal": not self._admission_closed,
                "admission_closed": self._admission_closed,
            }
        cleanup_complete = self._cleanup_complete
        self.request_cancel(reason)
        try:
            await asyncio.wait_for(asyncio.shield(task), max(0.0, float(timeout_seconds)))
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(task), CLEANUP_DEADLINE_SECONDS + 0.1,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        except asyncio.CancelledError:
            raise
        return {
            "active": True,
            "cancel_requested": True,
            "terminal": bool(task.done() and cleanup_complete is not None and cleanup_complete.is_set()),
        }

    async def move(
        self,
        target: Any,
        *,
        tolerance: float = 1.0,
        completion_policy: str = EUCLIDEAN_DISTANCE,
        position_convention: Any = None,
        distance_fn: Optional[Callable[[Any, Any], float]] = None,
        deadline_seconds: Optional[float] = None,
        poll_interval_seconds: Optional[float] = None,
        correlation_id: Optional[str] = None,
        movement_id: Optional[str] = None,
        target_identity: Optional[str] = None,
        operation: str = "move",
        cancel_event: Optional[asyncio.Event] = None,
    ) -> MovementResult:
        if self.active:
            raise MovementOverlapError("movement already active")
        if self._admission_closed:
            raise MovementAdmissionClosedError("movement admission is closed")
        self._cancel_event = cancel_event or asyncio.Event()
        self._cancel_reason = None
        self._cleanup_complete = asyncio.Event()
        task = asyncio.current_task()
        self._active_task = task
        try:
            return await self._run_move(
                target,
                tolerance=tolerance,
                completion_policy=completion_policy,
                position_convention=position_convention,
                distance_fn=distance_fn or _distance,
                deadline_seconds=self.deadline_seconds if deadline_seconds is None else float(deadline_seconds),
                poll_interval_seconds=self.poll_interval_seconds if poll_interval_seconds is None else max(0.0, float(poll_interval_seconds)),
                correlation_id=correlation_id,
                movement_id=movement_id,
                target_identity=target_identity,
                operation=operation,
            )
        finally:
            self._active_task = None
            self._cancel_event = None
            self._cancel_reason = None
            self._active_metadata = None
            self._cleanup_complete = None

    move_to = move

    async def _run_move(self, target: Any, *, tolerance: float, completion_policy: str, position_convention: Any,
                        distance_fn: Callable, deadline_seconds: float, poll_interval_seconds: float,
                         correlation_id: Optional[str], movement_id: Optional[str],
                         target_identity: Optional[str], operation: str) -> MovementResult:
        start = self._clock()
        current = self.bot.entity.position
        initial = float(distance_fn(current, target))
        movement_id = movement_id or uuid.uuid4().hex
        metadata = {"movement_id": movement_id, "correlation_id": correlation_id or movement_id,
                    "operation": operation, "target_identity": target_identity or self._safe_identity(target),
                    "start_monotonic": start, "end_monotonic": None, "elapsed": None,
                     "initial_distance": initial, "final_distance": initial, "deadline": deadline_seconds,
                     "goal_clear_attempted": False, "goal_clear_succeeded": False,
                     "cleanup_completed": False, "error_class": None}
        self._active_metadata = metadata
        reason = "pathfinder_error"
        error_class = None
        cleanup = _Cleanup(self.bot, self.bot_pathfinder, metadata)
        self._target = target
        external_cancelled = False
        try:
            if self._cancel_event is not None and self._cancel_event.is_set():
                reason = "cancelled"
            elif self._reached(
                self.bot.entity.position, tolerance, completion_policy,
                position_convention, distance_fn,
            ):
                reason = "reached"
            elif initial >= 50:
                reason = "target_too_far"
            else:
                await self._start_pathfinder(target)
                deadline = start + max(0.0, deadline_seconds)
                while True:
                    if self._cancel_event is not None and self._cancel_event.is_set():
                        reason = "cancelled"
                        break
                    current = self.bot.entity.position
                    if self._reached(
                        current, tolerance, completion_policy,
                        position_convention, distance_fn,
                    ):
                        reason = "reached"
                        break
                    if self._clock() >= deadline:
                        reason = "deadline"
                        break
                    await self._sleep(poll_interval_seconds)
        except asyncio.CancelledError:
            # Coordinator-requested cancellation returns a terminal result. A
            # caller/task cancellation is re-raised after bounded cleanup.
            reason = "cancelled"
            external_cancelled = self._cancel_reason is None
        except Exception as exc:
            error_class = type(exc).__name__
            reason = "pathfinder_error"
        finally:
            cleanup_task = asyncio.create_task(cleanup.run())
            try:
                await asyncio.wait_for(
                    asyncio.shield(cleanup_task), CLEANUP_DEADLINE_SECONDS,
                )
                metadata["cleanup_completed"] = True
            except asyncio.TimeoutError:
                cleanup_task.cancel()
                await asyncio.gather(cleanup_task, return_exceptions=True)
                reason = "cleanup_timeout"
                self._admission_closed = True
                self._admission_closed_reason = "cleanup_timeout"
            except asyncio.CancelledError:
                cleanup_task.cancel()
                await asyncio.gather(cleanup_task, return_exceptions=True)
                reason = "cleanup_timeout"
                self._admission_closed = True
                self._admission_closed_reason = "cleanup_timeout"
                external_cancelled = external_cancelled or self._cancel_reason is None
            finally:
                if metadata["cleanup_completed"] and self._cleanup_complete is not None:
                    self._cleanup_complete.set()
            end = self._clock()
            metadata["end_monotonic"] = end
            metadata["elapsed"] = max(0.0, end - start)
            metadata["final_distance"] = self._safe_distance(self.bot.entity.position, target, distance_fn)
            metadata["error_class"] = error_class
            if self._cancel_reason is not None:
                metadata["cancellation_reason"] = self._cancel_reason
        result = MovementResult(
            reason == "reached", reason, metadata, self._message(reason, metadata),
        )
        self._last_result = result
        if external_cancelled:
            raise asyncio.CancelledError
        return result

    async def _start_pathfinder(self, target: Any) -> None:
        module = self.pathfinder_module
        bot_pathfinder = self.bot_pathfinder
        if module is None or bot_pathfinder is None:
            raise RuntimeError("pathfinder unavailable")
        movements = module.Movements(self.bot) if hasattr(module, "Movements") else None
        if movements is not None:
            movements.allow1by1towers = False
            movements.canDig = False
            movements.canOpenDoors = True
            await _maybe_await(_call(bot_pathfinder, "setMovements", movements))
        goals = getattr(module, "goals", None)
        goal_near = getattr(goals, "GoalNear", None)
        if goal_near is None:
            raise RuntimeError("pathfinder goal unavailable")
        goal = goal_near(
            _field(target, "x"), _field(target, "y"), _field(target, "z"),
            self._goal_radius(target),
        )
        last_error = None
        for attempt in range(3):
            try:
                await _maybe_await(_call(bot_pathfinder, "setGoal", goal))
                return
            except Exception as error:
                last_error = error
                if attempt < 2:
                    await self._sleep(1.0)
        raise last_error or RuntimeError("pathfinder goal unavailable")

    def _goal_radius(self, target: Any) -> float:
        try:
            block_name = self.bot.blockAt(target)["name"]
            below = target.offset(0, -1, 0)
            below_name = self.bot.blockAt(below)["name"]
        except Exception:
            return 0.0
        if "pressure_plate" in block_name or "pressure_plate" in below_name:
            return 1.4
        return 0.0

    def _reached(self, current: Any, tolerance: float, policy: str, convention: Any, distance_fn: Callable) -> bool:
        if policy == STRICT_PER_AXIS:
            return bool(evaluate_movement_completion(current, self._target, tolerance, policy=policy, position_convention=convention)["target_reached"])
        return float(distance_fn(current, self._target)) < tolerance

    @staticmethod
    def _safe_identity(target: Any) -> str:
        return "target:" + ",".join(str(_field(target, axis)) for axis in ("x", "y", "z"))

    @staticmethod
    def _safe_distance(current: Any, target: Any, fn: Callable) -> Optional[float]:
        try:
            return float(fn(current, target))
        except Exception:
            return None

    @staticmethod
    def _message(reason: str, metadata: dict) -> str:
        return {
            "reached": "movement complete",
            "deadline": "movement deadline exceeded",
            "cancelled": "movement cancelled",
            "target_too_far": "movement target is too far away",
            "pathfinder_error": "movement could not be completed",
            "cleanup_timeout": "movement cleanup did not complete",
        }[reason]


class _Cleanup:
    def __init__(self, bot: Any, pathfinder: Any, metadata: dict) -> None:
        self.bot, self.pathfinder, self.metadata = bot, pathfinder, metadata
        self._done = False

    async def run(self) -> None:
        if self._done:
            return
        self._done = True
        self.metadata["goal_clear_attempted"] = True
        try:
            if self.pathfinder is not None and hasattr(self.pathfinder, "setGoal"):
                await _maybe_await(self.pathfinder.setGoal(None))
                self.metadata["goal_clear_succeeded"] = True
        except Exception:
            pass
        try:
            if self.pathfinder is not None and callable(getattr(self.pathfinder, "stop", None)):
                await _maybe_await(self.pathfinder.stop())
        except Exception:
            pass
        try:
            clear = getattr(self.bot, "clearControlStates", None)
            if callable(clear):
                await _maybe_await(clear())
        except Exception:
            pass


def _call(obj: Any, name: str, *args: Any) -> Any:
    return getattr(obj, name)(*args)


CooperativeMovementRuntime = MovementCoordinator
MovementRuntime = MovementCoordinator
BotMovementCoordinator = MovementCoordinator


def reconcile_mineflayer_state(bot: Any) -> dict:
    """Return only currently observable state; never invent historical events."""
    ready = getattr(bot, "entity", None) is not None
    client = getattr(bot, "_client", None)
    client_state = getattr(client, "state", None)
    connected = ready or getattr(bot, "player", None) is not None or client_state == "play"
    return {
        "connected": bool(connected),
        "ready": bool(ready),
        "source": "reconciled_current_state",
    }
