"""Bounded metadata-only diagnostics for Minecraft bridge transport lifecycle."""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import stat
import threading
import time
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

try:
    from env.runtime_paths import atomic_write_json
except ImportError:
    from runtime_paths import atomic_write_json


SCHEMA_VERSION = "minecraft-bridge-diagnostics/2"
LEGACY_SCHEMA_VERSION = "minecraft-bridge-diagnostics/1"
CORRELATION_HEADER = "X-Villager-Request-ID"
OUTCOME_CERTAINTY_HEADER = "X-Villager-Outcome-Certainty"
RETRY_SAFE_HEADER = "X-Villager-Retry-Safe"
MOVEMENT_TERMINAL_HEADER = "X-Villager-Movement-Terminal"
MOVEMENT_FAILURE_REASON_HEADER = "X-Villager-Movement-Failure-Reason"
MAX_EVENTS = 256
MAX_CRITICAL_EVENTS = 64
MAX_CORRELATIONS = 128
MAX_UNRESOLVED_REQUESTS = 64
MAX_LONG_DURATION_REQUESTS = 64
MAX_LIFECYCLE_ACTORS = 32
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
_ID = re.compile(r"^[0-9a-f]{32}$")
_SAFE = re.compile(r"[^A-Za-z0-9_.:/-]")
_INTEGER_FIELDS = frozenset({
    "started_monotonic_ns", "completed_monotonic_ns", "elapsed_ns",
    "timestamp_monotonic_ns", "pid", "process_start_ticks",
    "expected_local_port", "status_code",
    "caller_start_ns", "caller_end_ns", "bridge_start_ns", "bridge_end_ns",
})
_SIGNED_INTEGER_FIELDS = frozenset({"exit_code"})
_FLOAT_FIELDS = frozenset({
    "configured_connect_timeout_s", "configured_read_timeout_s",
    "configured_movement_deadline_s", "initial_distance", "final_distance",
    "movement_elapsed_s",
})
_BOOLEAN_FIELDS = frozenset({
    "retry_safe", "caller_correlated", "caller_started", "caller_completed",
    "caller_failed", "caller_timed_out", "ping_started", "ping_succeeded",
    "ping_failed", "ping_timed_out", "bridge_received", "bridge_completed",
    "bridge_failed", "movement_started", "movement_completed", "movement_deadline",
    "movement_cancelled", "movement_failed", "movement_overlap_rejected",
    "goal_clear_attempted", "goal_clear_succeeded", "cleanup_completed",
    "cancel_requested",
    "movement_terminal", "movement_nonterminal",
})
_STRING_FIELDS = frozenset({
    "correlation_id", "actor", "method", "route", "endpoint_identity",
    "timeout_type", "outcome_certainty", "error_class", "entrypoint",
    "connection_state", "result",
    "movement_id", "operation", "target_identity", "terminal_reason",
    "cancellation_reason",
})
_CRITICAL_PRIORITY = {
    "caller_request_timed_out": 3,
    "ping_timed_out": 3,
    "listener_failed": 3,
    "mineflayer_connection_error": 3,
    "mineflayer_disconnected": 3,
    "bridge_process_spawn_failed": 3,
    "bridge_process_still_alive": 3,
    "caller_request_failed": 2,
    "ping_failed": 2,
    "request_failed": 2,
    "movement_overlap_rejected": 2,
    "movement_nonterminal": 3,
}
_LIFECYCLE_MILESTONES = {
    "listener_starting": ("listener", "first_starting", "first"),
    "listener_startup_completed": ("listener", "startup_completed", "first"),
    "listener_ready": ("listener", "first_ready", "first"),
    "listener_request_accepted": ("listener", "first_request_accepted", "first"),
    "listener_failed": ("listener", "last_failed", "latest"),
    "listener_shutdown": ("listener", "shutdown", "latest"),
    "mineflayer_bot_created": ("mineflayer", "first_bot_created", "first"),
    "mineflayer_connected": ("mineflayer", "first_connected", "first"),
    "mineflayer_ready": ("mineflayer", "first_ready", "first"),
    "mineflayer_disconnected": ("mineflayer", "last_disconnected", "latest"),
    "mineflayer_connection_error": ("mineflayer", "last_connection_error", "latest"),
    "bridge_process_spawned": ("process", "first_spawned", "first"),
    "bridge_process_spawn_failed": ("process", "last_spawn_failed", "latest"),
    "bridge_process_terminate_sent": ("process", "last_terminate_sent", "latest"),
    "bridge_process_kill_sent": ("process", "last_kill_sent", "latest"),
    "bridge_process_exited": ("process", "last_exited", "latest"),
    "bridge_process_still_alive": ("process", "last_still_alive", "latest"),
}
_LIFECYCLE_EXPECTED = {
    category: {name: event_type for event_type, (candidate, name, _) in
               _LIFECYCLE_MILESTONES.items() if candidate == category}
    for category in ("listener", "mineflayer", "process")
}
_LIFECYCLE_EXPECTED["mineflayer"].update({
    "last_connected": "mineflayer_connected",
    "last_ready": "mineflayer_ready",
})


def new_correlation_id() -> str:
    return uuid4().hex


def valid_correlation_id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def safe_identifier(value: object, *, limit: int = 96) -> str:
    return _SAFE.sub("_", str(value))[:limit] or "unknown"


def safe_route(value: object, *, limit: int = 96) -> str:
    """Retain only a bounded path, never URL authority, query, or fragment."""
    raw = str(value).strip()
    if "\\" in raw or "%" in raw:
        return "/"
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return "/"
    if (parsed.scheme and not parsed.netloc) or (raw.startswith("//") and not parsed.netloc):
        return "/"
    path = parsed.path.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        return "/"
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", path.lstrip("/")):
        return "/"
    return safe_identifier(path or "/", limit=limit)


def safe_error_class(error: BaseException | object) -> str:
    return safe_identifier(type(error).__name__)


def classify_request_exception(error: BaseException) -> str:
    import requests

    if isinstance(error, requests.ConnectTimeout):
        return "connect_timeout"
    if isinstance(error, requests.ReadTimeout):
        return "read_timeout"
    if isinstance(error, requests.Timeout):
        return "timeout"
    if isinstance(error, requests.ConnectionError):
        values = list(error.args)
        while values:
            value = values.pop()
            if isinstance(value, BaseException):
                if getattr(value, "errno", None) == 111:
                    return "connection_refused"
                values.extend(value.args)
        return "connection_error"
    if isinstance(error, requests.RequestException):
        return "other_request_error"
    return "other_error"


def stable_process_start_ticks(pid: int | None) -> int | None:
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        return int(fields[21])
    except (OSError, ValueError, IndexError):
        return None


def _clean_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in fields.items():
        if value is None:
            continue
        if key == "correlation_id":
            if valid_correlation_id(value):
                clean[key] = value
        elif key == "route":
            clean[key] = safe_route(value)
        elif key in _STRING_FIELDS:
            clean[key] = safe_identifier(value)
        elif key in _INTEGER_FIELDS and type(value) is int and value >= 0:
            clean[key] = value
        elif key in _SIGNED_INTEGER_FIELDS and type(value) is int:
            clean[key] = value
        elif key in _FLOAT_FIELDS and type(value) in (int, float) and value >= 0:
            clean[key] = float(value)
        elif key in _BOOLEAN_FIELDS and type(value) is bool:
            clean[key] = value
    return clean


def critical_event_priority(event: Mapping[str, Any]) -> int:
    """Return a stable retention priority without inspecting payload text."""
    event_type = event.get("event_type")
    priority = _CRITICAL_PRIORITY.get(event_type, 0)
    if event_type == "caller_request_failed" and event.get("timeout_type") == "connection_refused":
        priority = max(priority, 3)
    if event_type in {"caller_request_completed", "request_completed"}:
        status_code = event.get("status_code")
        if type(status_code) is int and status_code >= 500:
            priority = max(priority, 2)
        elif type(status_code) is int and status_code >= 400:
            priority = max(priority, 1)
    if event_type == "bridge_process_exited" and event.get("exit_code") not in (None, 0):
        priority = max(priority, 2)
    if event_type == "movement_terminal" and event.get("terminal_reason") != "reached":
        priority = max(priority, 3)
    return priority


def is_critical_event(event: Mapping[str, Any]) -> bool:
    return critical_event_priority(event) > 0


def _correlation_priority(summary: Mapping[str, Any]) -> int:
    if summary.get("caller_timed_out") or summary.get("ping_timed_out"):
        return 3
    status_code = summary.get("status_code")
    if (summary.get("caller_failed") or summary.get("ping_failed")
            or summary.get("bridge_failed")
            or summary.get("movement_deadline") or summary.get("movement_cancelled")
            or summary.get("movement_failed") or summary.get("movement_overlap_rejected")
            or summary.get("movement_nonterminal")
            or (type(status_code) is int and status_code >= 500)):
        return 2
    if (type(status_code) is int and status_code >= 400):
        return 1
    caller_terminal = any(summary.get(key) for key in (
        "caller_completed", "caller_failed", "caller_timed_out",
    ))
    bridge_terminal = summary.get("bridge_completed") or summary.get("bridge_failed")
    if ((summary.get("caller_started") and not caller_terminal)
            or (summary.get("bridge_received") and not bridge_terminal)):
        return 1
    return 0


def _sanitized_event(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not isinstance(value.get("event_type"), str):
        return None
    return {
        "event_type": safe_identifier(value["event_type"]),
        **_clean_fields(value),
    }


def _sanitized_correlation(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not valid_correlation_id(value.get("correlation_id")):
        return None
    return _clean_fields(value)


def _bounded_artifact_bytes(path: str | Path) -> tuple[bytes | None, str | None]:
    try:
        with Path(path).open("rb") as artifact:
            if not stat.S_ISREG(os.fstat(artifact.fileno()).st_mode):
                return None, "unreadable"
            encoded = artifact.read(MAX_ARTIFACT_BYTES + 1)
    except FileNotFoundError:
        return None, "absent"
    except OSError:
        return None, "unreadable"
    if len(encoded) > MAX_ARTIFACT_BYTES:
        return None, "too_large"
    return encoded, None


class BoundedDiagnosticRecorder:
    """Single-process snapshot writer; failures never affect runtime behavior."""

    def __init__(self, path: str | Path, *, producer: str, actor: str | None = None,
                 max_events: int = MAX_EVENTS,
                 max_critical_events: int = MAX_CRITICAL_EVENTS,
                 max_correlations: int = MAX_CORRELATIONS,
                 max_unresolved_requests: int = MAX_UNRESOLVED_REQUESTS,
                 max_long_duration_requests: int = MAX_LONG_DURATION_REQUESTS,
                 max_lifecycle_actors: int = MAX_LIFECYCLE_ACTORS):
        self.path = Path(path)
        self.producer = safe_identifier(producer)
        self.actor = safe_identifier(actor) if actor else None
        self.max_events = min(MAX_EVENTS, max(1, int(max_events)))
        self.max_critical_events = min(
            MAX_CRITICAL_EVENTS, max(1, int(max_critical_events)),
        )
        self.max_correlations = min(MAX_CORRELATIONS, max(1, int(max_correlations)))
        self.max_unresolved_requests = min(
            MAX_UNRESOLVED_REQUESTS, max(1, int(max_unresolved_requests)),
        )
        self.max_long_duration_requests = min(
            MAX_LONG_DURATION_REQUESTS, max(1, int(max_long_duration_requests)),
        )
        self.max_lifecycle_actors = min(
            MAX_LIFECYCLE_ACTORS, max(1, int(max_lifecycle_actors)),
        )
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._critical_events: list[dict[str, Any]] = []
        self._correlations: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._unresolved_requests: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._long_duration_requests: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lifecycle: OrderedDict[str, dict[str, dict[str, dict[str, Any]]]] = OrderedDict()
        self._seen_once: set[str] = set()
        self._dropped = {
            "recent": 0, "critical": 0, "correlations": 0, "unresolved": 0,
            "long_duration": 0, "lifecycle": 0,
        }
        self.collection_error: str | None = None
        self._pending: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=1)
        self._closed = False
        self._writer = threading.Thread(
            target=self._write_loop,
            name=f"bridge-diagnostics-{self.producer}-{self.actor or 'all'}",
            daemon=True,
        )
        self._writer.start()

    def _write_loop(self) -> None:
        while True:
            snapshot = self._pending.get()
            try:
                if snapshot is None:
                    return
                atomic_write_json(self.path, snapshot)
            except Exception as error:
                self.collection_error = safe_error_class(error)
            finally:
                self._pending.task_done()

    def _enqueue_latest(self, snapshot: dict[str, Any]) -> None:
        try:
            self._pending.put_nowait(snapshot)
            return
        except queue.Full:
            pass
        try:
            self._pending.get_nowait()
            self._pending.task_done()
        except queue.Empty:
            pass
        try:
            self._pending.put_nowait(snapshot)
        except queue.Full:
            self.collection_error = "diagnostic_queue_full"

    def record(self, event_type: str, **fields: Any) -> bool:
        event = {
            "event_type": safe_identifier(event_type),
            "timestamp_monotonic_ns": time.monotonic_ns(),
            **_clean_fields(fields),
        }
        with self._lock:
            if self._closed:
                return False
            self._record_unlocked(event)
            snapshot = self._snapshot_unlocked()
            self._enqueue_latest(snapshot)
        return True

    def record_once(self, event_type: str, **fields: Any) -> bool:
        with self._lock:
            if self._closed:
                return False
            safe_event_type = safe_identifier(event_type)
            if safe_event_type in self._seen_once:
                return True
            event = {
                "event_type": safe_event_type,
                "timestamp_monotonic_ns": time.monotonic_ns(),
                **_clean_fields(fields),
            }
            self._seen_once.add(safe_event_type)
            self._record_unlocked(event)
            snapshot = self._snapshot_unlocked()
            self._enqueue_latest(snapshot)
        return True

    def _record_unlocked(self, event: dict[str, Any]) -> None:
        self._events.append(event)
        if len(self._events) > self.max_events:
            del self._events[0]
            self._dropped["recent"] += 1
        self._retain_critical_unlocked(event)
        self._update_correlation_unlocked(event)
        self._update_long_duration_unlocked(event)
        self._update_unresolved_unlocked(event)
        self._update_lifecycle_unlocked(event)

    def _retain_critical_unlocked(self, event: dict[str, Any]) -> None:
        priority = critical_event_priority(event)
        if priority == 0:
            return
        if len(self._critical_events) < self.max_critical_events:
            self._critical_events.append(event)
            return
        priorities = [critical_event_priority(candidate) for candidate in self._critical_events]
        lowest = min(priorities)
        if priority >= lowest:
            del self._critical_events[priorities.index(lowest)]
            self._critical_events.append(event)
        self._dropped["critical"] += 1

    def _update_correlation_unlocked(self, event: dict[str, Any]) -> None:
        correlation_id = event.get("correlation_id")
        if not valid_correlation_id(correlation_id):
            return
        summary = dict(self._correlations.get(correlation_id, {"correlation_id": correlation_id}))
        for key in ("actor", "method", "route", "endpoint_identity"):
            if key in event:
                summary[key] = event[key]
        event_type = event["event_type"]
        transitions = {
            "caller_request_started": ("caller_started", "caller_start_ns", "started_monotonic_ns"),
            "caller_request_completed": ("caller_completed", "caller_end_ns", "completed_monotonic_ns"),
            "caller_request_failed": ("caller_failed", "caller_end_ns", "completed_monotonic_ns"),
            "caller_request_timed_out": ("caller_timed_out", "caller_end_ns", "completed_monotonic_ns"),
            "ping_started": ("ping_started", "caller_start_ns", "started_monotonic_ns"),
            "ping_succeeded": ("ping_succeeded", "caller_end_ns", "completed_monotonic_ns"),
            "ping_failed": ("ping_failed", "caller_end_ns", "completed_monotonic_ns"),
            "ping_timed_out": ("ping_timed_out", "caller_end_ns", "completed_monotonic_ns"),
            "request_received": ("bridge_received", "bridge_start_ns", "started_monotonic_ns"),
            "request_completed": ("bridge_completed", "bridge_end_ns", "completed_monotonic_ns"),
            "request_failed": ("bridge_failed", "bridge_end_ns", "completed_monotonic_ns"),
            "movement_started": ("movement_started", "bridge_start_ns", "started_monotonic_ns"),
            "movement_terminal": ("movement_terminal", "bridge_end_ns", "completed_monotonic_ns"),
            "movement_nonterminal": (
                "movement_nonterminal", "bridge_end_ns", "completed_monotonic_ns",
            ),
            "movement_overlap_rejected": (
                "movement_overlap_rejected", "bridge_end_ns", "completed_monotonic_ns",
            ),
        }
        transition = transitions.get(event_type)
        if transition:
            flag, target_time, source_time = transition
            summary[flag] = True
            if source_time in event:
                summary[target_time] = event[source_time]
        if event_type in {"caller_request_completed", "caller_request_failed",
                          "caller_request_timed_out"} and "started_monotonic_ns" in event:
            summary["caller_started"] = True
            summary.setdefault("caller_start_ns", event["started_monotonic_ns"])
        if (event_type in {"ping_succeeded", "ping_failed", "ping_timed_out"}
                and "started_monotonic_ns" in event):
            summary["ping_started"] = True
            summary.setdefault("caller_start_ns", event["started_monotonic_ns"])
        if event_type in {"request_completed", "request_failed"} and "started_monotonic_ns" in event:
            summary["bridge_received"] = True
            summary.setdefault("bridge_start_ns", event["started_monotonic_ns"])
        for key in (
            "status_code", "elapsed_ns", "timeout_type", "outcome_certainty", "retry_safe",
            "error_class", "configured_connect_timeout_s", "configured_read_timeout_s",
            "movement_id", "operation", "target_identity", "terminal_reason",
            "cancellation_reason", "configured_movement_deadline_s", "initial_distance",
            "final_distance", "movement_elapsed_s", "goal_clear_attempted",
            "goal_clear_succeeded", "cleanup_completed", "cancel_requested",
        ):
            if key in event:
                summary[key] = event[key]
        if event_type in {"movement_terminal", "movement_nonterminal"}:
            terminal_reason = event.get("terminal_reason")
            summary["movement_completed"] = terminal_reason == "reached"
            summary["movement_deadline"] = terminal_reason == "deadline"
            summary["movement_cancelled"] = terminal_reason == "cancelled"
            summary["movement_failed"] = terminal_reason not in {
                None, "reached", "deadline", "cancelled",
            }
        if correlation_id in self._correlations:
            self._correlations[correlation_id] = summary
            self._correlations.move_to_end(correlation_id)
            return
        if len(self._correlations) < self.max_correlations:
            self._correlations[correlation_id] = summary
            return
        priorities = [
            _correlation_priority(candidate) for candidate in self._correlations.values()
        ]
        lowest = min(priorities)
        if _correlation_priority(summary) >= lowest:
            victim = list(self._correlations)[priorities.index(lowest)]
            del self._correlations[victim]
            self._correlations[correlation_id] = summary
        self._dropped["correlations"] += 1

    def _update_unresolved_unlocked(self, event: dict[str, Any]) -> None:
        correlation_id = event.get("correlation_id")
        if not valid_correlation_id(correlation_id):
            return
        if event["event_type"] in {"request_completed", "request_failed"}:
            self._unresolved_requests.pop(correlation_id, None)
            return
        if event["event_type"] != "request_received":
            return
        unresolved = {
            "correlation_id": correlation_id,
            "bridge_received": True,
            "bridge_start_ns": event.get("started_monotonic_ns", event["timestamp_monotonic_ns"]),
            **{key: event[key] for key in ("actor", "method", "route", "endpoint_identity")
               if key in event},
        }
        if correlation_id in self._unresolved_requests:
            self._unresolved_requests[correlation_id] = unresolved
            self._unresolved_requests.move_to_end(correlation_id)
        elif len(self._unresolved_requests) < self.max_unresolved_requests:
            self._unresolved_requests[correlation_id] = unresolved
        else:
            self._dropped["unresolved"] += 1

    def _update_long_duration_unlocked(self, event: dict[str, Any]) -> None:
        if event["event_type"] != "request_completed":
            return
        correlation_id = event.get("correlation_id")
        if not valid_correlation_id(correlation_id) or type(event.get("elapsed_ns")) is not int:
            return
        fallback = {
            "correlation_id": correlation_id,
            "bridge_received": True,
            "bridge_completed": True,
            **{key: event[key] for key in (
                "actor", "method", "route", "endpoint_identity", "elapsed_ns", "status_code",
            ) if key in event},
        }
        if "started_monotonic_ns" in event:
            fallback["bridge_start_ns"] = event["started_monotonic_ns"]
        if "completed_monotonic_ns" in event:
            fallback["bridge_end_ns"] = event["completed_monotonic_ns"]
        summary = dict(self._correlations.get(correlation_id, fallback))
        if correlation_id in self._long_duration_requests:
            self._long_duration_requests[correlation_id] = summary
            self._long_duration_requests.move_to_end(correlation_id)
            return
        if len(self._long_duration_requests) < self.max_long_duration_requests:
            self._long_duration_requests[correlation_id] = summary
            return
        durations = [candidate.get("elapsed_ns", 0)
                     for candidate in self._long_duration_requests.values()]
        shortest = min(durations)
        if summary["elapsed_ns"] >= shortest:
            victim = list(self._long_duration_requests)[durations.index(shortest)]
            del self._long_duration_requests[victim]
            self._long_duration_requests[correlation_id] = summary
        self._dropped["long_duration"] += 1

    def _update_lifecycle_unlocked(self, event: dict[str, Any]) -> None:
        milestone = _LIFECYCLE_MILESTONES.get(event["event_type"])
        if milestone is None:
            return
        actor = event.get("actor", self.actor or "unknown")
        if actor not in self._lifecycle:
            if len(self._lifecycle) >= self.max_lifecycle_actors:
                self._lifecycle.popitem(last=False)
                self._dropped["lifecycle"] += 1
            self._lifecycle[actor] = {"listener": {}, "mineflayer": {}, "process": {}}
        self._lifecycle.move_to_end(actor)
        category, name, policy = milestone
        actor_lifecycle = self._lifecycle[actor][category]
        if policy != "first" or name not in actor_lifecycle:
            actor_lifecycle[name] = event
        if event["event_type"] == "mineflayer_connected":
            actor_lifecycle["last_connected"] = event
        elif event["event_type"] == "mineflayer_ready":
            actor_lifecycle["last_ready"] = event

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_unlocked()

    def _snapshot_unlocked(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "producer": self.producer,
            "actor": self.actor,
            "truncated": self._dropped["recent"] > 0,
            "events": deepcopy(self._events),
            "critical_events": deepcopy(self._critical_events),
            "correlations": deepcopy(list(self._correlations.values())),
            "unresolved_requests": deepcopy(list(self._unresolved_requests.values())),
            "long_duration_requests": deepcopy(list(self._long_duration_requests.values())),
            "lifecycle": {"actors": deepcopy(dict(self._lifecycle))},
            "retention": {
                "recent": self._retention_lane("recent", self.max_events, len(self._events)),
                "critical": self._retention_lane(
                    "critical", self.max_critical_events, len(self._critical_events),
                ),
                "correlations": self._retention_lane(
                    "correlations", self.max_correlations, len(self._correlations),
                ),
                "unresolved": self._retention_lane(
                    "unresolved", self.max_unresolved_requests,
                    len(self._unresolved_requests),
                ),
                "long_duration": self._retention_lane(
                    "long_duration", self.max_long_duration_requests,
                    len(self._long_duration_requests),
                ),
                "lifecycle": self._retention_lane(
                    "lifecycle", self.max_lifecycle_actors, len(self._lifecycle),
                ),
            },
            "diagnostic_collection_error": self.collection_error,
        }

    def _retention_lane(self, name: str, capacity: int, retained: int) -> dict[str, Any]:
        dropped = self._dropped[name]
        return {
            "capacity": capacity,
            "retained": retained,
            "dropped_count": dropped,
            "truncated": dropped > 0,
        }

    def flush(self, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while self._pending.unfinished_tasks:
            if time.monotonic() >= deadline:
                self.collection_error = "diagnostic_flush_timeout"
                return False
            time.sleep(0.001)
        return True

    def close(self, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            if self._closed:
                return not self._writer.is_alive()
            self._closed = True
        flushed = self.flush(max(0.0, deadline - time.monotonic()))
        while True:
            try:
                self._pending.put_nowait(None)
                break
            except queue.Full:
                try:
                    self._pending.get_nowait()
                    self._pending.task_done()
                except queue.Empty:
                    continue
        self._writer.join(timeout=max(0.0, deadline - time.monotonic()))
        if self._writer.is_alive():
            self.collection_error = "diagnostic_close_timeout"
            return False
        return flushed


def install_fastapi_request_diagnostics(app, recorder: BoundedDiagnosticRecorder,
                                        *, actor: str) -> None:
    """Install metadata-only request lifecycle middleware on one bridge app."""
    from fastapi import Request

    @app.middleware("http")
    async def bridge_request_lifecycle(request: Request, call_next):
        supplied = request.headers.get(CORRELATION_HEADER)
        correlated = valid_correlation_id(supplied)
        correlation_id = supplied if correlated else new_correlation_id()
        started = time.monotonic_ns()
        route = request.url.path
        recorder.record_once(
            "listener_request_accepted", actor=actor, endpoint_identity=f"actor:{actor}",
        )
        recorder.record(
            "request_received", correlation_id=correlation_id, actor=actor,
            method=request.method, route=route, endpoint_identity=f"actor:{actor}",
            started_monotonic_ns=started, caller_correlated=correlated,
        )
        try:
            response = await call_next(request)
        except BaseException as error:
            completed = time.monotonic_ns()
            recorder.record(
                "request_failed", correlation_id=correlation_id, actor=actor,
                method=request.method, route=route, endpoint_identity=f"actor:{actor}",
                started_monotonic_ns=started, completed_monotonic_ns=completed,
                elapsed_ns=max(0, completed - started), error_class=safe_error_class(error),
                caller_correlated=correlated,
            )
            raise
        completed = time.monotonic_ns()
        recorder.record(
            "request_completed", correlation_id=correlation_id, actor=actor,
            method=request.method, route=route, endpoint_identity=f"actor:{actor}",
            started_monotonic_ns=started, completed_monotonic_ns=completed,
            elapsed_ns=max(0, completed - started), status_code=response.status_code,
            caller_correlated=correlated,
        )
        response.headers[CORRELATION_HEADER] = correlation_id
        return response


def read_diagnostic_snapshot(path: str | Path) -> tuple[dict[str, Any] | None, str | None]:
    encoded, read_error = _bounded_artifact_bytes(path)
    if read_error == "absent":
        return None, None
    if read_error == "unreadable":
        return None, "invalid_diagnostic_artifact"
    if read_error == "too_large":
        return None, "diagnostic_artifact_too_large"
    assert encoded is not None
    try:
        raw = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, MemoryError, ValueError):
        return None, "invalid_diagnostic_artifact"
    value = _sanitized_snapshot(raw)
    if value is None:
        return None, "invalid_diagnostic_schema"
    return value, None


def _sanitized_snapshot(value: object) -> dict[str, Any] | None:
    if (not isinstance(value, dict)
            or value.get("schema_version") not in {SCHEMA_VERSION, LEGACY_SCHEMA_VERSION}
            or not isinstance(value.get("events"), list)):
        return None
    raw_retention = value.get("retention", {})
    if not isinstance(raw_retention, dict):
        raw_retention = {}

    def declared_capacity(name: str, maximum: int) -> int:
        raw_lane = raw_retention.get(name, {})
        capacity = raw_lane.get("capacity") if isinstance(raw_lane, dict) else None
        if type(capacity) is int and 1 <= capacity <= maximum:
            return capacity
        return maximum

    event_capacity = (
        declared_capacity("recent", MAX_EVENTS)
        if value["schema_version"] == SCHEMA_VERSION else MAX_EVENTS
    )
    raw_events = value["events"]
    events: list[dict[str, Any]] = []
    event_omitted = 0
    for raw_event in raw_events:
        event = _sanitized_event(raw_event)
        if event is None:
            event_omitted += 1
            continue
        if len(events) >= event_capacity:
            del events[0]
            event_omitted += 1
        events.append(event)
    collection_error = value.get("diagnostic_collection_error")
    result = {
        "schema_version": value["schema_version"],
        "producer": safe_identifier(value.get("producer", "unknown")),
        "actor": safe_identifier(value["actor"]) if value.get("actor") else None,
        "truncated": value.get("truncated") is True or event_omitted > 0,
        "events": events,
        "diagnostic_collection_error": (
            safe_identifier(collection_error) if isinstance(collection_error, str) else None
        ),
    }
    if value["schema_version"] == LEGACY_SCHEMA_VERSION:
        return result

    critical_capacity = declared_capacity("critical", MAX_CRITICAL_EVENTS)
    correlation_capacity = declared_capacity("correlations", MAX_CORRELATIONS)
    unresolved_capacity = declared_capacity("unresolved", MAX_UNRESOLVED_REQUESTS)
    long_duration_capacity = declared_capacity("long_duration", MAX_LONG_DURATION_REQUESTS)
    lifecycle_capacity = declared_capacity("lifecycle", MAX_LIFECYCLE_ACTORS)

    raw_critical = value.get("critical_events", [])
    raw_correlations = value.get("correlations", [])
    raw_unresolved = value.get("unresolved_requests", [])
    raw_long_duration = value.get("long_duration_requests", [])
    raw_lifecycle = value.get("lifecycle", {})
    if (not isinstance(raw_critical, list) or not isinstance(raw_correlations, list)
            or not isinstance(raw_unresolved, list) or not isinstance(raw_long_duration, list)
            or not isinstance(raw_lifecycle, dict)):
        return None

    critical: list[dict[str, Any]] = []
    critical_omitted = 0
    for raw_event in raw_critical:
        event = _sanitized_event(raw_event)
        if event is None or not is_critical_event(event):
            critical_omitted += 1
            continue
        if len(critical) < critical_capacity:
            critical.append(event)
            continue
        priorities = [critical_event_priority(candidate) for candidate in critical]
        lowest = min(priorities)
        if critical_event_priority(event) >= lowest:
            del critical[priorities.index(lowest)]
            critical.append(event)
        critical_omitted += 1

    correlations: OrderedDict[str, dict[str, Any]] = OrderedDict()
    correlation_omitted = 0
    for raw_summary in raw_correlations:
        summary = _sanitized_correlation(raw_summary)
        if summary is None:
            correlation_omitted += 1
            continue
        correlation_id = summary["correlation_id"]
        if correlation_id in correlations:
            correlations[correlation_id] = summary
            correlations.move_to_end(correlation_id)
            continue
        if len(correlations) < correlation_capacity:
            correlations[correlation_id] = summary
            continue
        priorities = [_correlation_priority(candidate) for candidate in correlations.values()]
        lowest = min(priorities)
        if _correlation_priority(summary) >= lowest:
            victim = list(correlations)[priorities.index(lowest)]
            del correlations[victim]
            correlations[correlation_id] = summary
        correlation_omitted += 1

    unresolved: OrderedDict[str, dict[str, Any]] = OrderedDict()
    unresolved_omitted = 0
    for raw_summary in raw_unresolved:
        summary = _sanitized_correlation(raw_summary)
        if (summary is None or summary.get("bridge_received") is not True
                or summary.get("bridge_completed") or summary.get("bridge_failed")):
            unresolved_omitted += 1
            continue
        correlation_id = summary["correlation_id"]
        if correlation_id in unresolved:
            unresolved[correlation_id] = summary
            unresolved.move_to_end(correlation_id)
        elif len(unresolved) < unresolved_capacity:
            unresolved[correlation_id] = summary
        else:
            unresolved_omitted += 1

    long_duration: OrderedDict[str, dict[str, Any]] = OrderedDict()
    long_duration_omitted = 0
    for raw_summary in raw_long_duration:
        summary = _sanitized_correlation(raw_summary)
        if (summary is None or summary.get("bridge_completed") is not True
                or type(summary.get("elapsed_ns")) is not int):
            long_duration_omitted += 1
            continue
        correlation_id = summary["correlation_id"]
        if correlation_id in long_duration:
            long_duration[correlation_id] = summary
            long_duration.move_to_end(correlation_id)
        elif len(long_duration) < long_duration_capacity:
            long_duration[correlation_id] = summary
        else:
            durations = [candidate["elapsed_ns"] for candidate in long_duration.values()]
            shortest = min(durations)
            if summary["elapsed_ns"] >= shortest:
                victim = list(long_duration)[durations.index(shortest)]
                del long_duration[victim]
                long_duration[correlation_id] = summary
            long_duration_omitted += 1

    lifecycle_actors: OrderedDict[str, dict[str, dict[str, dict[str, Any]]]] = OrderedDict()
    lifecycle_omitted = 0
    raw_lifecycle_actors = raw_lifecycle.get("actors", {})
    if not isinstance(raw_lifecycle_actors, dict):
        raw_lifecycle_actors = {}
        lifecycle_omitted += 1
    for raw_actor, raw_actor_lifecycle in raw_lifecycle_actors.items():
        if not isinstance(raw_actor, str) or not isinstance(raw_actor_lifecycle, dict):
            lifecycle_omitted += 1
            continue
        actor = safe_identifier(raw_actor)
        actor_lifecycle: dict[str, dict[str, dict[str, Any]]] = {
            "listener": {}, "mineflayer": {}, "process": {},
        }
        for category, expected_events in _LIFECYCLE_EXPECTED.items():
            raw_category = raw_actor_lifecycle.get(category, {})
            if not isinstance(raw_category, dict):
                lifecycle_omitted += 1
                continue
            for name, expected_event_type in expected_events.items():
                event = _sanitized_event(raw_category.get(name))
                if event is not None and event["event_type"] == expected_event_type:
                    actor_lifecycle[category][name] = event
        if actor in lifecycle_actors:
            lifecycle_actors[actor] = actor_lifecycle
            lifecycle_actors.move_to_end(actor)
        elif len(lifecycle_actors) < lifecycle_capacity:
            lifecycle_actors[actor] = actor_lifecycle
        else:
            lifecycle_actors.popitem(last=False)
            lifecycle_actors[actor] = actor_lifecycle
            lifecycle_omitted += 1

    def retention_lane(name: str, capacity: int, retained: int, omitted: int) -> dict[str, Any]:
        raw_lane = raw_retention.get(name, {})
        raw_dropped = (
            raw_lane.get("dropped_count", raw_lane.get("dropped", 0))
            if isinstance(raw_lane, dict) else 0
        )
        dropped = raw_dropped if type(raw_dropped) is int and raw_dropped >= 0 else 0
        dropped += omitted
        return {
            "capacity": capacity,
            "retained": retained,
            "dropped_count": dropped,
            "truncated": dropped > 0,
        }

    result.update({
        "critical_events": critical,
        "correlations": list(correlations.values()),
        "unresolved_requests": list(unresolved.values()),
        "long_duration_requests": list(long_duration.values()),
        "lifecycle": {"actors": dict(lifecycle_actors)},
        "retention": {
            "recent": retention_lane("recent", event_capacity, len(events), event_omitted),
            "critical": retention_lane(
                "critical", critical_capacity, len(critical), critical_omitted,
            ),
            "correlations": retention_lane(
                "correlations", correlation_capacity, len(correlations), correlation_omitted,
            ),
            "unresolved": retention_lane(
                "unresolved", unresolved_capacity, len(unresolved), unresolved_omitted,
            ),
            "long_duration": retention_lane(
                "long_duration", long_duration_capacity,
                len(long_duration), long_duration_omitted,
            ),
            "lifecycle": retention_lane(
                "lifecycle", lifecycle_capacity, len(lifecycle_actors), lifecycle_omitted,
            ),
        },
    })
    return result


def artifact_projection(path: str | Path, *, runtime_root: str | Path) -> dict[str, Any]:
    target = Path(path)
    encoded, read_error = _bounded_artifact_bytes(target)
    if read_error == "absent":
        return {"state": "absent", "error": None}
    if read_error == "unreadable":
        return {"state": "invalid", "error": "diagnostic_artifact_unreadable"}
    if read_error == "too_large":
        return {"state": "invalid", "error": "diagnostic_artifact_too_large"}
    assert encoded is not None
    try:
        snapshot = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, MemoryError, ValueError):
        return {"state": "invalid", "error": "invalid_diagnostic_artifact"}
    snapshot = _sanitized_snapshot(snapshot)
    if snapshot is None:
        return {"state": "invalid", "error": "invalid_diagnostic_schema"}
    try:
        relative = target.relative_to(Path(runtime_root)).as_posix()
    except ValueError:
        relative = target.name
    return {
        "state": "valid",
        "path": relative,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "event_count": len(snapshot["events"]),
        "truncated": snapshot.get("truncated") is True,
        "snapshot": snapshot,
    }
