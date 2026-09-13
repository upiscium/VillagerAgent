"""Core trace data structures for the K11 natural-exposure study.

Runtime instrumentation lives in ``benchmarks.minecraft.k11_instrumentation``.
This module intentionally contains no filesystem writes on ``record()`` and no
synchronization primitive that could widen the EAC prepare/evidence/execute seam.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import math
from copy import deepcopy
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from itertools import count
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from benchmarks.common.eac.canonical import canonical_bytes, thaw_json
from benchmarks.common.eac.model import ExactRequest, Proposition, PropositionKey


TRACE_SCHEMA_VERSION = "minecraft-k11-trace/2"
PROSPECTIVE_TRACE_SCHEMA_VERSION = "minecraft-k11-trace/3"
MEASUREMENT_CUT_SCHEMA_VERSION = "minecraft-k11-measurement-cut/1"
MEASUREMENT_CUT_KEYS = frozenset({
    "schema_version", "boundary", "window_open_monotonic_ns",
    "window_close_monotonic_ns", "close_reason", "identity",
    "close_sequence", "event_prefix_high_water_sequence",
    "in_window_event_count", "in_window_event_digest",
    "evidence_state_event_count", "evidence_state_digest",
    "snapshot_state_digest", "snapshot_valid", "snapshot_errors",
    "active_executions", "open_lifecycles", "prepared_requests",
    "evidence_high_water", "censoring_inventory",
})
MEASUREMENT_IDENTITY_KEYS = frozenset({
    "run_id", "manifest_digest", "execution_revision", "runtime_digest",
    "premanifest_identity", "validation_contract", "trace_schema",
})
PRIMARY_EFFECT_ACTIONS = frozenset({
    "MineBlock",
    "placeBlock",
    "navigateTo",
    "attackTarget",
    "handoverBlock",
})
K11_EVENT_TYPES = frozenset({
    "k11.agent_step_started",
    "k11.agent_step_completed",
    "k11.model_call_started",
    "k11.model_call_completed",
    "k11.model_call_failed",
    "k11.tool_call_entered",
    "k11.tool_call_exited",
    "k11.eac_action_prepared",
    "k11.eac_evidence_ingested",
    "k11.eac_execution_decision_attempted",
    "k11.eac_native_effect_entered",
    "k11.eac_native_effect_completed",
    "k11.eac_action_terminal",
    "k11.observation_window_opened",
    "k11.observation_window_closed",
})


def canonical_trace_bytes(value: Any) -> bytes:
    """Canonical JSON bytes for trace artifacts, including large monotonic ns."""
    return json.dumps(
        plain_value(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _freeze_trace_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({
            str(key): _freeze_trace_value(item) for key, item in value.items()
        })
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_trace_value(item) for item in value)
    return value

WINDOW_REASONS = frozenset({"fixed_observation_horizon", "natural_runtime_terminal"})


def observation_window(artifact: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    """Return the strict observation window, or None for legacy fixtures."""
    events = artifact.get("events", [])
    opened = [e for e in events if isinstance(e, Mapping) and e.get("event_type") == "k11.observation_window_opened"]
    closed = [e for e in events if isinstance(e, Mapping) and e.get("event_type") == "k11.observation_window_closed"]
    if not opened and not closed:
        return None
    if len(opened) != 1 or len(closed) != 1:
        return (opened[0] if opened else {}, closed[0] if closed else {})
    return opened[0], closed[0]


def observation_window_bounds(artifact: Mapping[str, Any]) -> tuple[int, int, str] | None:
    pair = observation_window(artifact)
    if pair is None:
        return None
    opened, closed = pair
    closed_payload = closed.get("payload", {})
    if not isinstance(closed_payload, Mapping):
        return None
    reason = closed_payload.get("reason")
    start = opened.get("monotonic_ns")
    end = closed_payload.get("window_close_monotonic_ns")
    if isinstance(start, int) and isinstance(end, int) and isinstance(reason, str):
        return start, end, reason
    return None


def event_in_observation_window(
    event: Mapping[str, Any], bounds: tuple[int, int, str] | None,
) -> bool:
    if bounds is None:
        return True
    value = event.get("monotonic_ns")
    return isinstance(value, int) and bounds[0] <= value < bounds[1]


def valid_evidence_ingestion(
    event: Mapping[str, Any], *, run_id: Any,
) -> bool:
    """Return whether an evidence event retains its replay identity."""
    payload = event.get("payload")
    actor_id = event.get("actor_id")
    proposition = payload.get("proposition") if isinstance(payload, Mapping) else None
    revision = payload.get("revision") if isinstance(payload, Mapping) else None
    supersedes = payload.get("supersedes") if isinstance(payload, Mapping) else None
    visible_to = payload.get("visible_to") if isinstance(payload, Mapping) else None
    source_stream_revision = (
        payload.get("source_stream_revision") if isinstance(payload, Mapping) else None
    )
    proposition_valid = False
    if (isinstance(proposition, Mapping)
            and isinstance(proposition.get("arguments"), list)):
        try:
            Proposition(
                PropositionKey(
                    proposition.get("namespace"),
                    proposition.get("predicate"),
                    tuple(proposition["arguments"]),
                    proposition.get("temporal_scope"),
                ),
                polarity=proposition.get("polarity"),
            )
        except (TypeError, ValueError):
            pass
        else:
            proposition_valid = True
    record_type = payload.get("record_type") if isinstance(payload, Mapping) else None
    stream_backed = record_type in {"direct_observation", "visible_action_outcome"}
    stream_identity_valid = (
        isinstance(payload.get("source_stream_id"), str)
        and bool(payload["source_stream_id"])
        and type(revision) is int
        and revision > 0
        and type(source_stream_revision) is int
        and source_stream_revision == revision
    ) if isinstance(payload, Mapping) and stream_backed else (
        isinstance(payload, Mapping)
        and payload.get("source_stream_id") is None
        and source_stream_revision is None
    )
    return bool(
        event.get("event_type") == "k11.eac_evidence_ingested"
        and isinstance(run_id, str) and run_id
        and event.get("run_id") == run_id
        and isinstance(actor_id, str) and actor_id
        and isinstance(proposition, Mapping)
        and proposition_valid
        and all(isinstance(proposition.get(key), str) and proposition[key]
                for key in ("namespace", "predicate", "temporal_scope"))
        and isinstance(proposition.get("arguments"), list)
        and type(proposition.get("polarity")) is bool
        and record_type in {
            "direct_observation", "trusted_tool_result", "visible_action_outcome", "peer_report",
        }
        and all(isinstance(payload.get(key), str) and payload[key]
                for key in ("root_id", "source", "provenance_id"))
        and ((stream_backed and type(revision) is int and revision > 0)
             or (not stream_backed and (
                 (type(revision) is int and revision > 0)
                 or (isinstance(revision, str) and revision)
             )))
        and isinstance(supersedes, list)
        and all(isinstance(item, str) and item for item in supersedes)
        and (stream_backed or not supersedes)
        and visible_to == [actor_id]
        and stream_identity_valid
    )


def _candidate_id(event: Mapping[str, Any]) -> str | None:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return None
    request = payload.get("exact_request")
    if not isinstance(request, Mapping):
        return None
    candidate_id = request.get("candidate_id")
    return candidate_id if isinstance(candidate_id, str) else None


@dataclass(frozen=True, slots=True)
class K11TraceScope:
    run_id: str
    task_id: str | None = None
    actor_id: str | None = None
    agent_step_id: str | None = None
    tool_call_id: str | None = None


_SCOPE: ContextVar[K11TraceScope | None] = ContextVar("k11_trace_scope", default=None)


def current_scope() -> K11TraceScope | None:
    return _SCOPE.get()


@contextmanager
def use_scope(scope: K11TraceScope):
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)


def proposition_payload(proposition: Proposition) -> dict[str, Any]:
    key = proposition.key
    return {
        "namespace": key.namespace,
        "predicate": key.predicate,
        "arguments": plain_value(key.arguments),
        "temporal_scope": key.temporal_scope,
        "polarity": proposition.polarity,
    }


def request_payload(request: ExactRequest) -> dict[str, Any]:
    return {
        "candidate_id": request.candidate_id,
        "attempt_id": request.attempt_id,
        "action": {
            "identity": request.action.identity,
            "version": request.action.version,
            "digest": request.action.digest,
        },
        "arguments": {name: plain_value(value) for name, value in request.arguments},
        "target": plain_value(request.target),
    }


def plain_value(value: Any) -> Any:
    """Convert captured immutable values only when exporting the trace.

    ExactRequest and Proposition have explicit projections because the generic
    dataclass representation does not match the frozen K11 trace schema.
    """
    if isinstance(value, ExactRequest):
        return request_payload(value)
    if isinstance(value, Proposition):
        return proposition_payload(value)
    try:
        from benchmarks.common.eac.canonical import FrozenJSONArray, FrozenJSONObject
        if isinstance(value, (FrozenJSONArray, FrozenJSONObject)):
            return thaw_json(value)
    except ImportError:
        pass
    if is_dataclass(value):
        return {field.name: plain_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): plain_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def exact_request_digest(request_value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(dict(request_value))).hexdigest()


def _valid_preparation(event: Mapping[str, Any], *, require_scope: bool) -> bool:
    payload = event.get("payload")
    request = payload.get("exact_request") if isinstance(payload, Mapping) else None
    action = request.get("action") if isinstance(request, Mapping) else None
    try:
        digest_valid = (
            isinstance(request, Mapping)
            and payload.get("exact_request_digest") == exact_request_digest(request)
        )
    except (TypeError, ValueError):
        digest_valid = False
    if (not isinstance(request, Mapping)
            or not isinstance(request.get("candidate_id"), str) or not request["candidate_id"]
            or not isinstance(request.get("attempt_id"), str) or not request["attempt_id"]
            or not isinstance(action, Mapping)
            or not isinstance(action.get("identity"), str) or not action["identity"]
            or type(action.get("version")) is not int or action["version"] < 1
            or not isinstance(action.get("digest"), str) or not action["digest"]
            or not digest_valid):
        return False
    if require_scope and any(not event.get(field) for field in (
            "actor_id", "task_id", "agent_step_id", "tool_call_id")):
        return False
    return True


def derive_positive_disposition(
    artifact: Mapping[str, Any], prepared: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Derive a positive pre-decision disposition from ordinary lifecycle facts.

    Mere disappearance is never sufficient. The prepared tool call must return
    normally without a decision, followed by either a same-actor/task successor
    preparation before the agent step returns, or the normal return of that step.
    """
    events = artifact.get("events")
    if not isinstance(events, list) or not isinstance(prepared, Mapping):
        return None
    prepared_payload = prepared.get("payload")
    if not isinstance(prepared_payload, Mapping) or not _valid_preparation(prepared, require_scope=True):
        return None
    prepared_seq = prepared.get("seq")
    prepared_ns = prepared.get("monotonic_ns")
    scope = tuple(prepared.get(field) for field in (
        "actor_id", "task_id", "agent_step_id", "tool_call_id",
    ))
    if (any(not value for value in scope) or not isinstance(prepared_seq, int)
            or not isinstance(prepared_ns, int)):
        return None
    preparation_counts: dict[str, int] = {}
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if event.get("event_type") != "k11.eac_action_prepared":
            continue
        payload = event.get("payload")
        request = payload.get("exact_request") if isinstance(payload, Mapping) else None
        candidate_id = request.get("candidate_id") if isinstance(request, Mapping) else None
        if isinstance(candidate_id, str):
            preparation_counts[candidate_id] = preparation_counts.get(candidate_id, 0) + 1
    prepared_request = prepared_payload.get("exact_request")
    original_candidate = prepared_request.get("candidate_id") if isinstance(prepared_request, Mapping) else None
    if not isinstance(original_candidate, str) or preparation_counts.get(original_candidate) != 1:
        return None

    tool_exits = [
        event for event in events
        if isinstance(event, Mapping)
        and event.get("event_type") == "k11.tool_call_exited"
        and tuple(event.get(field) for field in (
            "actor_id", "task_id", "agent_step_id", "tool_call_id",
        )) == scope
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("outcome") == "returned"
        and isinstance(event.get("seq"), int) and event["seq"] > prepared_seq
        and isinstance(event.get("monotonic_ns"), int) and event["monotonic_ns"] > prepared_ns
    ]
    if len(tool_exits) != 1:
        return None
    tool_exit = tool_exits[0]

    agent_returns = [
        event for event in events
        if isinstance(event, Mapping)
        and event.get("event_type") == "k11.agent_step_completed"
        and (event.get("actor_id"), event.get("task_id"), event.get("agent_step_id")) == scope[:3]
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("outcome") == "returned"
        and isinstance(event.get("seq"), int) and event["seq"] > tool_exit["seq"]
        and isinstance(event.get("monotonic_ns"), int)
        and event["monotonic_ns"] > tool_exit["monotonic_ns"]
    ]
    if len(agent_returns) != 1:
        return None
    agent_return = agent_returns[0]

    successor_preparations = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        payload = event.get("payload")
        request = payload.get("exact_request") if isinstance(payload, Mapping) else None
        successor_scope = tuple(event.get(field) for field in (
            "actor_id", "task_id", "agent_step_id", "tool_call_id",
        ))
        if (event.get("event_type") == "k11.eac_action_prepared"
                and isinstance(request, Mapping)
                and _valid_preparation(event, require_scope=True)
                and request.get("candidate_id") != original_candidate
                and preparation_counts.get(request.get("candidate_id")) == 1
                and successor_scope[:3] == scope[:3]
                and successor_scope[3] and successor_scope[3] != scope[3]
                and isinstance(event.get("seq"), int) and event["seq"] > tool_exit["seq"]
                and event["seq"] < agent_return["seq"]
                and isinstance(event.get("monotonic_ns"), int)
                and tool_exit["monotonic_ns"] < event["monotonic_ns"] < agent_return["monotonic_ns"]):
            successor_preparations.append(event)

    kind = "cancellation"
    marker = agent_return
    if successor_preparations:
        by_sequence = min(successor_preparations, key=lambda event: event["seq"])
        by_time = min(successor_preparations, key=lambda event: event["monotonic_ns"])
        if by_sequence is not by_time:
            return None
        kind = "replacement"
        marker = by_sequence
    successor_ids = []
    if kind == "replacement":
        successor_ids = [marker["payload"]["exact_request"]["candidate_id"]]
    return {
        "kind": kind,
        "marker": marker,
        "tool_exit": tool_exit,
        "successor_candidate_ids": successor_ids,
    }


def _event_precedes(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool | None:
    first_seq, second_seq = first.get("seq"), second.get("seq")
    first_ns, second_ns = first.get("monotonic_ns"), second.get("monotonic_ns")
    if not all(isinstance(value, int) for value in (first_seq, second_seq, first_ns, second_ns)):
        return None
    sequence_before = first_seq < second_seq
    time_before = first_ns < second_ns
    if sequence_before != time_before or first_seq == second_seq or first_ns == second_ns:
        return None
    return sequence_before


class K11TraceRecorder:
    """Append-only process-local recorder.

    Legacy ``/2`` ordering remains unchanged. Prospective ``/3`` additionally
    uses a recorder-local lock so appending the close marker and freezing the
    measurement cut have one linearization point. This lock is not shared with
    the EAC runtime and does not create a prepare/evidence/execute barrier.
    """

    def __init__(self, run_id: str, *, schema_version: str = TRACE_SCHEMA_VERSION,
                 measurement_identity: Mapping[str, Any] | None = None):
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("K11 trace run_id must be a non-empty string")
        self.run_id = run_id
        if schema_version not in (TRACE_SCHEMA_VERSION, PROSPECTIVE_TRACE_SCHEMA_VERSION):
            raise ValueError("unsupported K11 trace schema version")
        self.schema_version = schema_version
        self.measurement_identity = plain_value(dict(measurement_identity or {}))
        self._sequence = count(1)
        self._identity_sequence = count(1)
        self.events: list[dict[str, Any]] = []
        self.instrumentation_errors: list[str] = []
        self._cut_lock = threading.RLock()
        self._measurement_cut: dict[str, Any] | None = None
        self._measurement_cut_pending = False
        self._measurement_cut_pending_token = None

    def new_identity(self, kind: str, *, actor_id: str | None = None) -> str:
        ordinal = next(self._identity_sequence)
        return f"{self.run_id}:{actor_id or 'none'}:{kind}:{ordinal}"

    def record(
        self,
        event_type: str,
        *,
        source: str,
        payload: Mapping[str, Any] | None = None,
        scope: K11TraceScope | None = None,
        monotonic_ns: int | None = None,
    ) -> dict[str, Any] | None:
        """Append one small observed fact; tracing failures are non-authoritative."""
        try:
            guard = (
                self._cut_lock
                if self.schema_version == PROSPECTIVE_TRACE_SCHEMA_VERSION
                else nullcontext()
            )
            with guard:
                sequence = next(self._sequence)
                selected_scope = scope if scope is not None else current_scope()
                event = {
                    "schema_version": self.schema_version,
                    "run_id": self.run_id,
                    "seq": sequence,
                    "event_id": f"{self.run_id}:k11:{sequence}",
                    "event_type": event_type,
                    "source": source,
                    "task_id": selected_scope.task_id if selected_scope else None,
                    "actor_id": selected_scope.actor_id if selected_scope else None,
                    "agent_step_id": selected_scope.agent_step_id if selected_scope else None,
                    "tool_call_id": selected_scope.tool_call_id if selected_scope else None,
                    "payload": (
                        plain_value(dict(payload or {}))
                        if self.schema_version == PROSPECTIVE_TRACE_SCHEMA_VERSION
                        else dict(payload or {})
                    ),
                    "monotonic_ns": time.monotonic_ns() if monotonic_ns is None else monotonic_ns,
                    "thread_id": threading.get_ident(),
                }
                if self.schema_version == PROSPECTIVE_TRACE_SCHEMA_VERSION:
                    stored_event = _freeze_trace_value(event)
                    self.events.append(stored_event)
                    return plain_value(stored_event)
                self.events.append(event)
                return event
        except BaseException as exc:
            try:
                self.instrumentation_errors.append(type(exc).__name__)
            except BaseException:
                pass
            return None

    def artifact(self) -> dict[str, Any]:
        guard = (
            self._cut_lock
            if self.schema_version == PROSPECTIVE_TRACE_SCHEMA_VERSION
            else nullcontext()
        )
        with guard:
            raw_events = list(self.events)
            instrumentation_errors = list(self.instrumentation_errors)
            measurement_cut = deepcopy(self._measurement_cut)
        events: list[dict[str, Any]] = []
        for raw in sorted(raw_events, key=lambda item: item.get("seq", 0)):
            event = plain_value(raw)
            request = event.get("payload", {}).get("exact_request")
            if isinstance(request, Mapping):
                event["payload"]["exact_request_digest"] = exact_request_digest(request)
            events.append(event)
        result = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "event_count": len(events),
            "instrumentation_errors": instrumentation_errors,
            "events": events,
        }
        if self.schema_version == PROSPECTIVE_TRACE_SCHEMA_VERSION:
            result["measurement_cut"] = measurement_cut
        return result

    def measurement_cut(self, *, reason: str = "explicit",
                        window_open_monotonic_ns: int | None = None,
                        window_close_monotonic_ns: int | None = None,
                        active_executions: Mapping[str, Any] | None = None,
                        snapshot_errors: list[str] | None = None,
                        _frozen_raw_events: list[Mapping[str, Any]] | None = None,
                        _pending_token=None) -> dict[str, Any]:
        """Atomically close the recorder and return a prospective prefix.

        The cut owns both the last append and the close operation.  Callers get
        an independent projection; later mutation of it cannot affect the
        recorder. Diagnostics may continue appending after the cut.
        """
        if self.schema_version != PROSPECTIVE_TRACE_SCHEMA_VERSION:
            raise ValueError("measurement cuts require prospective trace schema /3")
        if not isinstance(reason, str) or not reason:
            raise ValueError("measurement cut reason must be a non-empty string")
        with self._cut_lock:
            if (self._measurement_cut_pending
                    and _pending_token is not self._measurement_cut_pending_token):
                raise ValueError("measurement cut is owned by another pending operation")
            if self._measurement_cut is None:
                if _frozen_raw_events is None:
                    events = _export_trace_events(self.events)
                else:
                    events = _export_trace_events(_frozen_raw_events)
                opened = window_open_monotonic_ns
                closed = window_close_monotonic_ns
                if opened is None:
                    opened = min((e.get("monotonic_ns", 0) for e in events), default=0)
                if closed is None:
                    closed = max((e.get("monotonic_ns", 0) for e in events), default=opened)
                in_window = [e for e in events if opened <= e.get("monotonic_ns", -1) < closed]
                close_rows = [e for e in events if e.get("event_type") == "k11.observation_window_closed"]
                high_water = (close_rows[-1].get("seq") if close_rows else
                              (events[-1]["seq"] if events else 0))
                digest = "sha256:" + hashlib.sha256(canonical_trace_bytes(in_window)).hexdigest()
                errors = list(snapshot_errors or [])
                required_identity = (
                    "run_id", "manifest_digest", "execution_revision", "runtime_digest",
                    "premanifest_identity", "validation_contract", "trace_schema",
                )
                if any(
                    not isinstance(self.measurement_identity.get(key), str)
                    or not self.measurement_identity.get(key)
                    for key in required_identity
                ):
                    errors.append("measurement identity unavailable")
                if (not isinstance(active_executions, Mapping)
                        or not isinstance(active_executions.get("items"), list)
                        or not isinstance(active_executions.get("retention"), Mapping)):
                    errors.append("active execution snapshot unavailable")
                cut_items = _prospective_inventories(
                    events, opened, closed, active_executions, reason=reason,
                )
                evidence_events = [
                    event for event in events
                    if event.get("event_type") == "k11.eac_evidence_ingested"
                    and isinstance(event.get("seq"), int)
                    and event["seq"] <= high_water
                    and isinstance(event.get("monotonic_ns"), int)
                    and event["monotonic_ns"] < closed
                ]
                snapshot_projection = {
                    name: cut_items[name] for name in (
                        "active_executions", "open_lifecycles", "prepared_requests",
                        "evidence_high_water", "censoring_inventory",
                    )
                }
                snapshot_projection["identity"] = self.measurement_identity
                if any(value["retention"]["dropped_count"] for value in cut_items.values()):
                    errors.append("prospective inventory overflow")
                self._measurement_cut = {
                    "schema_version": MEASUREMENT_CUT_SCHEMA_VERSION,
                    "boundary": "[open,close)",
                    "window_open_monotonic_ns": opened,
                    "window_close_monotonic_ns": closed,
                    "close_reason": reason,
                    "identity": deepcopy(self.measurement_identity),
                    "close_sequence": high_water,
                    "event_prefix_high_water_sequence": high_water,
                    "in_window_event_count": len(in_window),
                    "in_window_event_digest": digest,
                    "evidence_state_event_count": len(evidence_events),
                    "evidence_state_digest": "sha256:" + hashlib.sha256(
                        canonical_trace_bytes(evidence_events)
                    ).hexdigest(),
                    "snapshot_state_digest": "sha256:" + hashlib.sha256(
                        canonical_trace_bytes(snapshot_projection)
                    ).hexdigest(),
                    "snapshot_valid": not errors,
                    "snapshot_errors": errors,
                    **cut_items,
                }
            cut = deepcopy(self._measurement_cut)
            prefix = self.artifact()
            result = {
                "schema_version": PROSPECTIVE_TRACE_SCHEMA_VERSION,
                "run_id": self.run_id,
                "measurement_cut": cut,
                "events": prefix["events"],
                "event_count": len(prefix["events"]),
                "instrumentation_errors": prefix["instrumentation_errors"],
            }
            return result

    def begin_record_and_cut(self, event_type: str, *, source: str,
                             payload: Mapping[str, Any] | None = None,
                             scope: K11TraceScope | None = None,
                             monotonic_ns: int | None = None,
                             reason: str = "explicit",
                             window_open_monotonic_ns: int | None = None,
                             window_close_monotonic_ns: int | None = None,
                             active_executions: Mapping[str, Any] | None = None,
                             snapshot_errors: list[str] | None = None) -> dict[str, Any]:
        """Linearize the close and copy its raw prefix without hashing it."""
        with self._cut_lock:
            if self._measurement_cut is not None or self._measurement_cut_pending:
                raise ValueError("measurement cut is already pending or complete")
            self.record(
                event_type, source=source, payload=payload, scope=scope,
                monotonic_ns=monotonic_ns,
            )
            self._measurement_cut_pending = True
            token = object()
            self._measurement_cut_pending_token = token
            return {
                "token": token,
                "raw_events": list(self.events),
                "reason": reason,
                "window_open_monotonic_ns": window_open_monotonic_ns,
                "window_close_monotonic_ns": window_close_monotonic_ns,
                "active_executions": deepcopy(active_executions),
                "snapshot_errors": list(snapshot_errors or []),
            }

    def finalize_record_and_cut(self, pending: Mapping[str, Any]) -> dict[str, Any]:
        """Build inventories and digests after the controller lock is released."""
        try:
            return self.measurement_cut(
                reason=pending["reason"],
                window_open_monotonic_ns=pending["window_open_monotonic_ns"],
                window_close_monotonic_ns=pending["window_close_monotonic_ns"],
                active_executions=pending["active_executions"],
                snapshot_errors=pending["snapshot_errors"],
                _frozen_raw_events=list(pending["raw_events"]),
                _pending_token=pending.get("token"),
            )
        finally:
            with self._cut_lock:
                if pending.get("token") is self._measurement_cut_pending_token:
                    self._measurement_cut_pending = False
                    self._measurement_cut_pending_token = None

    def record_and_cut(self, event_type: str, *, source: str,
                       payload: Mapping[str, Any] | None = None,
                       scope: K11TraceScope | None = None,
                       monotonic_ns: int | None = None,
                       reason: str = "explicit",
                       window_open_monotonic_ns: int | None = None,
                       window_close_monotonic_ns: int | None = None,
                       active_executions: Mapping[str, Any] | None = None,
                       snapshot_errors: list[str] | None = None) -> dict[str, Any]:
        """Append the boundary marker and close in one recorder critical section."""
        pending = self.begin_record_and_cut(
            event_type, source=source, payload=payload, scope=scope,
            monotonic_ns=monotonic_ns, reason=reason,
            window_open_monotonic_ns=window_open_monotonic_ns,
            window_close_monotonic_ns=window_close_monotonic_ns,
            active_executions=active_executions, snapshot_errors=snapshot_errors,
        )
        return self.finalize_record_and_cut(pending)

    close_cut = measurement_cut
    prospective_artifact = measurement_cut
    cut = measurement_cut

    def write_json(self, path: str | Path) -> None:
        """Persist only after the measured runtime section has completed."""
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(self.artifact(), ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _retained(items, capacity=256):
    items = list(items)
    return {"items": items[:capacity], "retention": {
        "capacity": capacity, "retained": min(len(items), capacity),
        "truncated": len(items) > capacity,
        "dropped_count": max(0, len(items) - capacity),
    }}


def _export_trace_events(raw_events) -> list[dict[str, Any]]:
    events = []
    for raw in sorted(raw_events, key=lambda item: item.get("seq", 0)):
        event = plain_value(raw)
        request = event.get("payload", {}).get("exact_request")
        if isinstance(request, Mapping):
            event["payload"]["exact_request_digest"] = exact_request_digest(request)
        events.append(event)
    return events


def _prospective_inventories(
    events, opened, closed, active_executions=None, *, reason="fixed_observation_horizon",
):
    """Bounded, replay-safe inventories for the prospective cut."""
    limit = 256
    if type(opened) is not int or type(closed) is not int or opened >= closed:
        opened, closed = 0, 0
    in_window = [
        e for e in events if isinstance(e, Mapping)
        and isinstance(e.get("monotonic_ns"), int)
        and opened <= e["monotonic_ns"] < closed
    ]
    def ids(kind: str, field: str = "candidate_id") -> list[str]:
        values = []
        for event in in_window:
            if event.get("event_type") != kind:
                continue
            payload = event.get("payload", {})
            request = payload.get("exact_request", {}) if isinstance(payload, Mapping) else {}
            value = request.get(field) if isinstance(request, Mapping) else None
            if isinstance(value, str) and value and value not in values:
                values.append(value)
        return values
    prepared = ids("k11.eac_action_prepared")
    disposed = ids("k11.eac_execution_decision_attempted")
    scoped = {"events": in_window}
    for event in in_window:
        if event.get("event_type") != "k11.eac_action_prepared":
            continue
        if derive_positive_disposition(scoped, event) is not None:
            request = event.get("payload", {}).get("exact_request", {})
            if isinstance(request, Mapping) and isinstance(request.get("candidate_id"), str):
                disposed.append(request["candidate_id"])
    lifecycle_starts = {"k11.agent_step_started": "agent_step_id", "k11.tool_call_entered": "tool_call_id", "k11.model_call_started": "model_call_id"}
    open_items = []
    for start_type, field in lifecycle_starts.items():
        def lifecycle_id(event):
            if field in {"agent_step_id", "tool_call_id"}:
                return event.get(field)
            payload = event.get("payload")
            return payload.get(field) if isinstance(payload, Mapping) else None
        starts = {lifecycle_id(e): e for e in in_window if e.get("event_type") == start_type if lifecycle_id(e)}
        terminal_types = {"k11.agent_step_started": ("k11.agent_step_completed",), "k11.tool_call_entered": ("k11.tool_call_exited",), "k11.model_call_started": ("k11.model_call_completed", "k11.model_call_failed")}[start_type]
        if terminal_types:
            done = {lifecycle_id(e) for e in in_window if e.get("event_type") in terminal_types}
            for value, event in starts.items():
                if value not in done:
                    payload = event.get("payload", {})
                    open_items.append({"kind": field, "id": value,
                                       "start_sequence": event.get("seq"),
                                       "start_monotonic_ns": event.get("monotonic_ns"),
                                       "scope": {key: event.get(key) for key in ("actor_id", "task_id", "agent_step_id", "tool_call_id")},
                                       "action": payload.get("tool_name") if isinstance(payload, Mapping) else None})
    open_count = len(open_items)
    entered = {
        e["payload"]["exact_request"]["candidate_id"]: e
        for e in in_window
        if e.get("event_type") == "k11.eac_native_effect_entered"
        and isinstance(e.get("payload"), Mapping)
        and isinstance(e["payload"].get("exact_request"), Mapping)
        and isinstance(e["payload"]["exact_request"].get("candidate_id"), str)
        and e["payload"]["exact_request"]["candidate_id"]
    }
    completed = {
        e["payload"]["exact_request"].get("candidate_id")
        for e in in_window
        if e.get("event_type") == "k11.eac_native_effect_completed"
        and isinstance(e.get("payload"), Mapping)
        and isinstance(e["payload"].get("exact_request"), Mapping)
        and isinstance(e["payload"]["exact_request"].get("candidate_id"), str)
        and e["payload"]["exact_request"]["candidate_id"]
    }
    for value, event in entered.items():
        if value and value not in completed:
            request = event.get("payload", {}).get("exact_request")
            action = request.get("action") if isinstance(request, Mapping) else None
            open_items.append({"kind": "native", "id": value,
                               "start_sequence": event.get("seq"),
                               "start_monotonic_ns": event.get("monotonic_ns"),
                               "scope": {key: event.get(key) for key in ("actor_id", "task_id", "agent_step_id", "tool_call_id")},
                               "action": (action.get("identity")
                                          if isinstance(action, Mapping) else None)})
    open_count = len(open_items)
    open_items = sorted(open_items, key=lambda item: (item.get("kind", ""), str(item.get("id", ""))))
    prepared_items = []
    for event in in_window:
        if event.get("event_type") != "k11.eac_action_prepared":
            continue
        payload = event.get("payload", {})
        request = payload.get("exact_request") if isinstance(payload, Mapping) else None
        if isinstance(request, Mapping):
            prepared_items.append({"exact_request": request,
                                   "digest": exact_request_digest(request),
                                   "start_sequence": event.get("seq"),
                                   "start_monotonic_ns": event.get("monotonic_ns"),
                                   "scope": {key: event.get(key) for key in ("actor_id", "task_id", "agent_step_id", "tool_call_id")},
                                   "disposition_known_at_cut": request.get("candidate_id") in disposed})
    censored = []
    for item in prepared_items:
        if not item["disposition_known_at_cut"]:
            request = item["exact_request"]
            censored.append({"kind": "prepared", "id": request.get("candidate_id"),
                             "start_sequence": item["start_sequence"], "start_monotonic_ns": item["start_monotonic_ns"],
                             "scope": item["scope"], "action": request.get("action"),
                              "censor_reason": reason})
    for item in open_items:
        censored.append({**item, "censor_reason": reason})
    evidence_by_key = {}
    evidence_prefix = [
        event for event in events if isinstance(event, Mapping)
        if event.get("event_type") == "k11.eac_evidence_ingested"
        and isinstance(event.get("monotonic_ns"), int)
        and event["monotonic_ns"] < closed
    ]
    for event in evidence_prefix:
        if event.get("event_type") == "k11.eac_evidence_ingested":
            payload = event.get("payload", {})
            key = (event.get("actor_id"), payload.get("source_stream_id"))
            current = evidence_by_key.get(key)
            revision = payload.get("revision")
            newer = (current is not None and isinstance(revision, int)
                     and isinstance(current.get("revision"), int)
                     and revision > current["revision"])
            if current is None or newer:
                evidence_by_key[key] = {"actor_id": key[0], "source_stream_id": key[1],
                                        "revision": revision, "high_water_sequence": event.get("seq")}
    evidence = list(evidence_by_key.values())
    active_snapshot = active_executions if isinstance(active_executions, Mapping) else {}
    active_items = active_snapshot.get("items")
    if not isinstance(active_items, list):
        active_items = []
    execution_items = [
        dict(item) for item in active_items
        if isinstance(item, Mapping)
    ]
    execution_retention = active_snapshot.get("retention")
    execution_collection = (
        {"items": execution_items, "retention": dict(execution_retention)}
        if isinstance(execution_retention, Mapping)
        else _retained(execution_items, capacity=128)
    )
    return {
        "active_executions": execution_collection,
        "open_lifecycles": _retained(open_items),
        "prepared_requests": _retained(prepared_items),
        "evidence_high_water": _retained(evidence),
        "censoring_inventory": _retained(censored),
    }


def _validate_prospective_trace(
    artifact: Mapping[str, Any], *, require_primary: bool = False,
) -> dict[str, Any]:
    """Fail-closed validation for a recorder-owned prospective measurement cut."""
    errors: list[str] = []
    if artifact.get("schema_version") != PROSPECTIVE_TRACE_SCHEMA_VERSION:
        errors.append("prospective trace schema is invalid")
    events = artifact.get("events")
    cut = artifact.get("measurement_cut")
    if not isinstance(events, list) or not isinstance(cut, Mapping):
        return {"valid": False, "errors": errors + ["measurement cut is malformed"]}
    if any(not isinstance(event, Mapping) for event in events):
        return {"valid": False, "errors": errors + ["prospective trace event is malformed"]}
    if any(not isinstance(event.get("payload"), Mapping) for event in events):
        return {"valid": False, "errors": errors + ["prospective trace payload is malformed"]}
    candidate_events = {
        "k11.eac_action_prepared", "k11.eac_execution_decision_attempted",
        "k11.eac_native_effect_entered", "k11.eac_native_effect_completed",
        "k11.eac_action_terminal",
    }
    for event in events:
        if event.get("event_type") not in candidate_events:
            continue
        request = event["payload"].get("exact_request")
        candidate_id = request.get("candidate_id") if isinstance(request, Mapping) else None
        if candidate_id is not None and (
            not isinstance(candidate_id, str) or not candidate_id
        ):
            return {
                "valid": False,
                "errors": errors + ["prospective candidate identity is malformed"],
                "warnings": [],
            }
    artifact_run_id = artifact.get("run_id")
    if not isinstance(artifact_run_id, str) or not artifact_run_id:
        errors.append("prospective trace run identity is invalid")
    if any(event.get("run_id") != artifact_run_id for event in events):
        errors.append("prospective trace event run identity is not correlated")
    if any(event.get("schema_version") != PROSPECTIVE_TRACE_SCHEMA_VERSION for event in events):
        errors.append("prospective trace event schema identity is not correlated")
    if cut.get("schema_version") != MEASUREMENT_CUT_SCHEMA_VERSION:
        errors.append("measurement cut schema is invalid")
    if set(cut) != MEASUREMENT_CUT_KEYS:
        errors.append("measurement cut field set is invalid")
    high_water = cut.get("event_prefix_high_water_sequence")
    if type(high_water) is not int:
        errors.append("measurement cut high-water mark is invalid")
    opened, closed = cut.get("window_open_monotonic_ns"), cut.get("window_close_monotonic_ns")
    in_window = [e for e in events if isinstance(e, Mapping) and isinstance(e.get("monotonic_ns"), int)
                 and isinstance(opened, int) and isinstance(closed, int)
                 and opened <= e["monotonic_ns"] < closed]
    expected = "sha256:" + hashlib.sha256(canonical_trace_bytes(in_window)).hexdigest()
    if cut.get("in_window_event_digest") != expected:
        errors.append("measurement cut prefix digest is invalid")
    if cut.get("boundary") != "[open,close)" or not isinstance(opened, int) or not isinstance(closed, int) or opened >= closed:
        errors.append("measurement cut boundary metadata is invalid")
    if cut.get("in_window_event_count") != len(in_window):
        errors.append("measurement cut event count is invalid")
    if artifact.get("event_count") != len(events):
        errors.append("prospective artifact event count is invalid")
    close_events = [e for e in events if e.get("event_type") == "k11.observation_window_closed"]
    open_events = [e for e in events if e.get("event_type") == "k11.observation_window_opened"]
    close_seq = close_events[0].get("seq") if len(close_events) == 1 else None
    if close_seq != cut.get("close_sequence"):
        errors.append("measurement cut close sequence is invalid")
    if close_seq != high_water:
        errors.append("measurement cut event prefix high-water is invalid")
    if len(close_events) != 1 or not isinstance(close_events[0].get("payload"), Mapping) or close_events[0]["payload"].get("reason") != cut.get("close_reason"):
        errors.append("measurement cut close metadata is invalid")
    if (len(open_events) != 1 or open_events[0].get("monotonic_ns") != opened
            or len(close_events) != 1 or close_events[0].get("monotonic_ns") != closed
            or close_events[0].get("payload", {}).get("window_close_monotonic_ns") != closed):
        errors.append("measurement cut bounds do not match observation window")
    if any(isinstance(e.get("seq"), int) and e["seq"] > close_seq
           and isinstance(e.get("monotonic_ns"), int) and e["monotonic_ns"] < closed
           for e in events if isinstance(e, Mapping) and isinstance(close_seq, int)):
        errors.append("late pre-close event invalidates prospective cut")
    measured_events = [
        event for event in events
        if event.get("event_type") == "k11.observation_window_closed"
        or (isinstance(event.get("seq"), int) and isinstance(high_water, int)
            and event["seq"] <= high_water
            and isinstance(event.get("monotonic_ns"), int)
            and isinstance(closed, int) and event["monotonic_ns"] < closed)
    ]
    generic = validate_trace({
        "schema_version": TRACE_SCHEMA_VERSION, "run_id": artifact.get("run_id"),
        "events": [{**event, "schema_version": TRACE_SCHEMA_VERSION}
                   for event in measured_events],
    })
    errors.extend(generic["errors"])
    bounds = observation_window_bounds({"events": events})
    expected_inventories = _prospective_inventories(
        events, opened, closed, cut.get("active_executions"),
        reason=cut.get("close_reason"),
    )
    evidence_events = [
        event for event in events
        if event.get("event_type") == "k11.eac_evidence_ingested"
        and isinstance(event.get("seq"), int)
        and isinstance(high_water, int) and event["seq"] <= high_water
        and isinstance(event.get("monotonic_ns"), int)
        and isinstance(closed, int) and event["monotonic_ns"] < closed
    ]
    expected_evidence_digest = "sha256:" + hashlib.sha256(
        canonical_trace_bytes(evidence_events)
    ).hexdigest()
    if (cut.get("evidence_state_event_count") != len(evidence_events)
            or cut.get("evidence_state_digest") != expected_evidence_digest):
        errors.append("measurement cut evidence state binding is invalid")
    lifecycle_specs = (
        ("agent", "k11.agent_step_started", ("k11.agent_step_completed",),
         lambda event: event.get("agent_step_id")),
        ("tool", "k11.tool_call_entered", ("k11.tool_call_exited",),
         lambda event: event.get("tool_call_id")),
        ("model", "k11.model_call_started",
         ("k11.model_call_completed", "k11.model_call_failed"),
         lambda event: event.get("payload", {}).get("model_call_id")),
        ("native", "k11.eac_native_effect_entered",
         ("k11.eac_native_effect_completed",), _candidate_id),
    )
    for label, start_type, terminal_types, identity in lifecycle_specs:
        starts_by_id: dict[Any, list[Mapping[str, Any]]] = {}
        terminals_by_id: dict[Any, list[Mapping[str, Any]]] = {}
        for event in in_window:
            if event.get("event_type") == start_type:
                lifecycle_id = identity(event)
                if not isinstance(lifecycle_id, str) or not lifecycle_id:
                    errors.append(f"prospective {label} lifecycle lacks identity")
                    continue
                starts_by_id.setdefault(lifecycle_id, []).append(event)
            elif event.get("event_type") in terminal_types:
                lifecycle_id = identity(event)
                if not isinstance(lifecycle_id, str) or not lifecycle_id:
                    errors.append(f"prospective {label} lifecycle lacks identity")
                    continue
                terminals_by_id.setdefault(lifecycle_id, []).append(event)
        for lifecycle_id, starts in starts_by_id.items():
            if lifecycle_id is None:
                continue
            terminals = terminals_by_id.get(lifecycle_id, [])
            if len(starts) != 1 or len(terminals) > 1:
                errors.append(f"prospective {label} lifecycle {lifecycle_id} is not one-to-one")
            elif terminals and (
                not isinstance(starts[0].get("seq"), int)
                or not isinstance(terminals[0].get("seq"), int)
                or starts[0]["seq"] >= terminals[0]["seq"]
            ):
                errors.append(f"prospective {label} lifecycle {lifecycle_id} is misordered")
    for name in ("active_executions", "open_lifecycles", "prepared_requests",
                 "evidence_high_water", "censoring_inventory"):
        value = cut.get(name)
        if (not isinstance(value, Mapping) or set(value) != {"items", "retention"}
                or not isinstance(value.get("items"), list)):
            errors.append(f"measurement cut {name} inventory is malformed")
            continue
        retention = value.get("retention")
        if not isinstance(retention, Mapping) or set(retention) != {"capacity", "retained", "truncated", "dropped_count"}:
            errors.append(f"measurement cut {name} retention is malformed")
        else:
            capacity = retention.get("capacity")
            retained = retention.get("retained")
            truncated = retention.get("truncated")
            dropped = retention.get("dropped_count")
            if (type(capacity) is not int or capacity < 0
                    or type(retained) is not int or retained < 0
                    or type(truncated) is not bool
                    or type(dropped) is not int or dropped < 0):
                errors.append(f"measurement cut {name} retention is malformed")
            elif (retained != len(value["items"])
                  or truncated is not (dropped > 0)
                  or capacity < len(value["items"])):
                errors.append(f"measurement cut {name} retention is inconsistent")
            if type(dropped) is int and dropped > 0:
                errors.append(f"measurement cut {name} inventory overflow")
    for name in ("open_lifecycles", "prepared_requests", "evidence_high_water", "censoring_inventory"):
        if cut.get(name) != expected_inventories[name]:
            errors.append(f"measurement cut {name} inventory does not match frozen prefix")
    snapshot_projection = {
        name: cut.get(name) for name in (
            "active_executions", "open_lifecycles", "prepared_requests",
            "evidence_high_water", "censoring_inventory",
        )
    }
    snapshot_projection["identity"] = cut.get("identity")
    expected_snapshot_digest = "sha256:" + hashlib.sha256(
        canonical_trace_bytes(snapshot_projection)
    ).hexdigest()
    if cut.get("snapshot_state_digest") != expected_snapshot_digest:
        errors.append("measurement cut snapshot state digest is invalid")
    identity = cut.get("identity")
    required_identity = (
        "run_id", "manifest_digest", "execution_revision", "runtime_digest",
        "premanifest_identity", "validation_contract", "trace_schema",
    )
    if (not isinstance(identity, Mapping) or set(identity) != MEASUREMENT_IDENTITY_KEYS
            or any(not isinstance(identity.get(key), str) or not identity.get(key)
                   for key in required_identity)
            or identity.get("run_id") != artifact_run_id
            or identity.get("trace_schema") != PROSPECTIVE_TRACE_SCHEMA_VERSION):
        errors.append("measurement cut identity binding is invalid")
    active = cut.get("active_executions")
    active_scopes = set()
    if isinstance(active, Mapping):
        active_items = active.get("items")
        if not isinstance(active_items, list):
            active_items = []
        for item in active_items:
            if (not isinstance(item, Mapping)
                    or any(not isinstance(item.get(key), str) or not item.get(key)
                           for key in ("execution_id", "task_id", "actor_id"))):
                errors.append("measurement cut active execution identity is malformed")
                continue
            active_scopes.add((item["task_id"], item["actor_id"]))
    open_collection = cut.get("open_lifecycles")
    open_agent_scopes = set()
    if isinstance(open_collection, Mapping) and isinstance(open_collection.get("items"), list):
        for item in open_collection["items"]:
            if not isinstance(item, Mapping):
                continue
            scope = item.get("scope")
            if item.get("kind") == "agent_step_id" and isinstance(scope, Mapping):
                open_agent_scopes.add((scope.get("task_id"), scope.get("actor_id")))
    if active_scopes != open_agent_scopes:
        errors.append("measurement cut active executions do not match open agent lifecycles")
    if any(
        type(value.get("retention", {}).get("dropped_count")) is int
        and value["retention"]["dropped_count"] > 0
        for value in expected_inventories.values()
        if isinstance(value, Mapping) and isinstance(value.get("retention"), Mapping)
    ):
        errors.append("prospective inventory overflow")
    if cut.get("snapshot_valid") is not True or cut.get("snapshot_errors"):
        errors.append("prospective snapshot is invalid")
    unresolved = expected_inventories["censoring_inventory"]["items"]
    if bounds and bounds[2] == "fixed_observation_horizon" and unresolved:
        # An in-flight lifecycle is an explicitly censored observation, not a
        # malformed terminal lifecycle.
        candidate_ids = {item.get("id") for item in unresolved
                         if isinstance(item, Mapping) and item.get("kind") == "prepared"}
        allowed = []
        for error in errors:
            if any(error.startswith(f"candidate {candidate_id} ") for candidate_id in candidate_ids):
                if error.endswith("lacks exactly one disposition") or error.endswith("native entry/completion count differs"):
                    continue
            allowed.append(error)
        errors[:] = allowed
    if bounds and bounds[2] == "fixed_observation_horizon":
        decisions = {
            _candidate_id(event) for event in in_window
            if event.get("event_type") == "k11.eac_execution_decision_attempted"
        }
        open_native = {
            item.get("id") for item in unresolved
            if isinstance(item, Mapping) and item.get("kind") == "native"
        }
        filtered = []
        for error in errors:
            if any(
                error == f"candidate {candidate_id} has 0 terminal events"
                for candidate_id in decisions if candidate_id
            ):
                continue
            if any(
                error == f"candidate {candidate_id} native entry/completion count differs"
                for candidate_id in open_native if candidate_id
            ):
                continue
            filtered.append(error)
        errors[:] = filtered
    if unresolved and (bounds is None or bounds[2] != "fixed_observation_horizon"):
        errors.append("unresolved candidates require explicit fixed-horizon censoring")
    if bounds and bounds[2] == "natural_runtime_terminal" and unresolved:
        errors.append("natural-terminal prospective cut has unresolved candidates")
    primary_prepared = []
    for item in expected_inventories["prepared_requests"]["items"]:
        request = item.get("exact_request") if isinstance(item, Mapping) else None
        action = request.get("action") if isinstance(request, Mapping) else None
        if isinstance(action, Mapping) and action.get("identity") in PRIMARY_EFFECT_ACTIONS:
            primary_prepared.append(item)
    if require_primary and not primary_prepared:
        errors.append("prospective trace lacks a primary preparation")
    return {"valid": not errors, "errors": errors, "warnings": generic.get("warnings", [])}


def validate_prospective_trace(
    artifact: Mapping[str, Any], *, require_primary: bool = False,
) -> dict[str, Any]:
    """Validate an untrusted prospective artifact without propagating shape errors."""
    try:
        return _validate_prospective_trace(artifact, require_primary=require_primary)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return {
            "valid": False,
            "errors": ["prospective trace structure is malformed"],
            "warnings": [],
        }

def validate_trace(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Validate P0 trace completeness without making prevalence claims."""
    if artifact.get("schema_version") == PROSPECTIVE_TRACE_SCHEMA_VERSION:
        return validate_prospective_trace(artifact)
    events = artifact.get("events")
    errors: list[str] = []
    warnings: list[str] = []
    if artifact.get("schema_version") != TRACE_SCHEMA_VERSION or not isinstance(events, list):
        return {"valid": False, "errors": ["invalid K11 trace artifact"], "warnings": []}

    pair = observation_window(artifact)
    bounds = observation_window_bounds(artifact)
    if pair is not None:
        opened, closed = pair
        opened_rows = [event for event in events if isinstance(event, Mapping)
                       and event.get("event_type") == "k11.observation_window_opened"]
        closed_rows = [event for event in events if isinstance(event, Mapping)
                       and event.get("event_type") == "k11.observation_window_closed"]
        if len(opened_rows) != 1 or len(closed_rows) != 1:
            errors.append("observation window requires exactly one open and close event")
        if bounds is None:
            errors.append("observation window timestamps or reason are malformed")
        else:
            start, end, reason = bounds
            opened_payload = opened.get("payload", {})
            closed_payload = closed.get("payload", {})
            if not isinstance(opened_payload, Mapping):
                opened_payload = {}
            if not isinstance(closed_payload, Mapping):
                closed_payload = {}
            if start >= end:
                errors.append("observation window is misordered")
            if (not isinstance(opened.get("seq"), int) or not isinstance(closed.get("seq"), int)
                    or opened["seq"] >= closed["seq"]):
                errors.append("observation window sequence is misordered")
            if reason not in WINDOW_REASONS:
                errors.append("observation window reason is invalid")
            configured = opened_payload.get("configured_horizon_seconds")
            horizon = opened_payload.get("horizon_monotonic_ns")
            if (type(configured) not in (int, float) or isinstance(configured, bool)
                    or not math.isfinite(configured) or configured <= 0
                    or type(horizon) is not int
                    or horizon != start + round(configured * 1_000_000_000)
                    or closed_payload.get("configured_horizon_seconds") != configured
                    or closed.get("monotonic_ns") != end):
                errors.append("observation window horizon metadata is invalid")
            if reason == "fixed_observation_horizon" and (
                    type(horizon) is not int or end != horizon
                    or closed_payload.get("shutdown_requested") is not True):
                errors.append("fixed observation horizon close is invalid")
            if reason == "natural_runtime_terminal" and (
                    type(horizon) is not int or end >= horizon
                    or closed_payload.get("shutdown_requested") is not False):
                errors.append("natural observation close is invalid")

    sequences = [event.get("seq") if isinstance(event, Mapping) else None for event in events]
    if any(type(value) is not int or value <= 0 for value in sequences):
        errors.append("trace sequence is malformed")
    elif len(set(sequences)) != len(sequences):
        errors.append("trace sequence contains duplicates")
    elif sequences != sorted(sequences):
        errors.append("exported trace sequence is not sorted")

    prepared: dict[str, list[Mapping[str, Any]]] = {}
    decisions: dict[str, list[Mapping[str, Any]]] = {}
    native_entries: dict[str, list[Mapping[str, Any]]] = {}
    native_completions: dict[str, list[Mapping[str, Any]]] = {}
    terminals: dict[str, list[Mapping[str, Any]]] = {}
    evidence_count = 0

    for event in events:
        if not isinstance(event, Mapping):
            errors.append("trace event is malformed")
            continue
        event_type = event.get("event_type")
        if event_type not in K11_EVENT_TYPES:
            warnings.append(f"unknown event type: {event_type}")
            continue
        if (pair is not None and event_type.startswith("k11.eac_")
                and not event_in_observation_window(event, bounds)):
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            errors.append(f"trace {event_type} payload is malformed")
            continue
        if event_type == "k11.eac_evidence_ingested":
            evidence_count += 1
        if event_type == "k11.eac_action_prepared" and not _valid_preparation(
                event, require_scope=False):
            errors.append("trace preparation is malformed")
            continue
        request = payload.get("exact_request") if isinstance(payload, Mapping) else None
        candidate_id = request.get("candidate_id") if isinstance(request, Mapping) else None
        if candidate_id is None:
            continue
        table = None
        if event_type == "k11.eac_action_prepared":
            table = prepared
        elif event_type == "k11.eac_execution_decision_attempted":
            table = decisions
        elif event_type == "k11.eac_native_effect_entered":
            table = native_entries
        elif event_type == "k11.eac_native_effect_completed":
            table = native_completions
        elif event_type == "k11.eac_action_terminal":
            table = terminals
        if table is not None:
            table.setdefault(candidate_id, []).append(event)

    scoped_artifact = artifact
    if bounds is not None:
        scoped_artifact = dict(artifact)
        scoped_artifact["events"] = [
            item for item in events if event_in_observation_window(item, bounds)
        ]

    def all_candidate_rows(kind: str, candidate_id: str) -> list[Mapping[str, Any]]:
        return [
            item for item in events
            if isinstance(item, Mapping) and item.get("event_type") == kind
            and _candidate_id(item) == candidate_id
        ]

    for candidate_id, rows in prepared.items():
        if len(rows) != 1:
            errors.append(f"candidate {candidate_id} has {len(rows)} prepare events")
        decision_rows = decisions.get(candidate_id, [])
        terminal_rows = all_candidate_rows("k11.eac_action_terminal", candidate_id)
        # Positive abandonment is authoritative only inside a declared window.
        positive_disposition = derive_positive_disposition(scoped_artifact, rows[0])
        decision_precedes = None
        if len(decision_rows) == 1 and positive_disposition is not None:
            decision_precedes = _event_precedes(decision_rows[0], positive_disposition["marker"])
            if decision_precedes is False:
                errors.append(f"candidate {candidate_id} reaches a decision after positive abandonment")
            elif decision_precedes is None:
                errors.append(f"candidate {candidate_id} disposition ordering is ambiguous")
        selected_positive = positive_disposition if not decision_rows else None
        censored = (bounds is not None and bounds[2] == "fixed_observation_horizon"
                    and event_in_observation_window(rows[0], bounds)
                    and (not decision_rows or any(
                        not event_in_observation_window(item, bounds) for item in decision_rows)))
        if len(decision_rows) != 1 and selected_positive is None and not censored:
            errors.append(f"candidate {candidate_id} lacks exactly one disposition event")
        if decision_rows and len(terminal_rows) != 1:
            errors.append(f"candidate {candidate_id} has {len(terminal_rows)} terminal events")
        if selected_positive is not None and terminal_rows:
            errors.append(f"candidate {candidate_id} has a terminal after positive abandonment")
        disposition_rows = decision_rows or (
            [selected_positive["marker"]] if selected_positive is not None else []
        )
        if disposition_rows:
            prepare_ns = rows[0].get("monotonic_ns")
            disposition_ns = disposition_rows[0].get("monotonic_ns")
            if (not isinstance(prepare_ns, int) or not isinstance(disposition_ns, int)
                    or disposition_ns <= prepare_ns):
                warnings.append(f"candidate {candidate_id} has non-positive prepare-to-disposition interval")
            prepare_digest = rows[0].get("payload", {}).get("exact_request_digest")
            disposition_digest = disposition_rows[0].get("payload", {}).get("exact_request_digest")
            if decision_rows and prepare_digest != disposition_digest:
                errors.append(f"candidate {candidate_id} exact request changed before disposition")

    for candidate_id, rows in native_entries.items():
        if candidate_id not in decisions:
            errors.append(f"candidate {candidate_id} reached native effect without decision marker")
        completions = all_candidate_rows("k11.eac_native_effect_completed", candidate_id)
        if len(completions) != len(rows):
            errors.append(f"candidate {candidate_id} native entry/completion count differs")

    if artifact.get("instrumentation_errors"):
        errors.append("instrumentation recorder reported internal errors")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "counts": {
            "events": len(events),
            "prepared": sum(len(rows) for rows in prepared.values()),
            "positive_abandonments": sum(
                derive_positive_disposition(scoped_artifact, rows[0]) is not None
                for candidate_id, rows in prepared.items() if candidate_id not in decisions
            ),
            "execution_decisions": sum(len(rows) for rows in decisions.values()),
            "native_entries": sum(len(rows) for rows in native_entries.values()),
            "native_completions": sum(len(rows) for rows in native_completions.values()),
            "terminals": sum(len(rows) for rows in terminals.values()),
            "evidence_ingestions": evidence_count,
        },
    }


def validate_p0_trace(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the strict, per-run completeness gate required by K11 P0.

    ``validate_trace`` remains deliberately useful for small unit fixtures.  This
    validator is the admission gate for a pilot run: aggregate event presence is
    not sufficient, and every primary request must remain exactly correlated.
    """
    if artifact.get("schema_version") == PROSPECTIVE_TRACE_SCHEMA_VERSION:
        return validate_prospective_trace(artifact, require_primary=True)
    generic = validate_trace(artifact)
    errors = list(generic["errors"])
    events = artifact.get("events") if isinstance(artifact, Mapping) else None
    if not isinstance(events, list) or not events:
        return {"valid": False, "errors": errors + ["P0 trace is empty or malformed"], "warnings": generic["warnings"], "counts": generic.get("counts", {})}
    if any("malformed" in error for error in errors):
        return {
            "valid": False,
            "errors": errors,
            "warnings": generic["warnings"],
            "counts": generic.get("counts", {}),
        }
    bounds = observation_window_bounds(artifact)
    if bounds is None:
        errors.append("P0 trace requires one valid observation window")
    artifact_run_id = artifact.get("run_id")
    if not isinstance(artifact_run_id, str) or not artifact_run_id:
        errors.append("P0 trace requires a non-empty run identity")
    elif any(event.get("run_id") != artifact_run_id for event in events
             if isinstance(event, Mapping)):
        errors.append("P0 trace event run identity is not correlated")

    def rows(kind: str, *, within_window: bool = True) -> list[Mapping[str, Any]]:
        return [event for event in events if isinstance(event, Mapping) and event.get("event_type") == kind
                and (not within_window or kind in {
                    "k11.observation_window_opened", "k11.observation_window_closed"
                } or event_in_observation_window(event, bounds))]

    starts = rows("k11.agent_step_started", within_window=False)
    completed = rows("k11.agent_step_completed", within_window=False)
    def lifecycle_key(event: Mapping[str, Any], identity: Any) -> tuple[Any, ...] | None:
        if not identity:
            return None
        return (identity, event.get("agent_step_id"), event.get("actor_id"), event.get("task_id"))

    def require_lifecycles(start_rows: list[Mapping[str, Any]], terminal_rows: list[Mapping[str, Any]],
                           identity, label: str) -> None:
        starts_by_key: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
        terminals_by_key: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
        def collect(source_rows, table) -> None:
            for row in source_rows:
                key = lifecycle_key(row, identity(row))
                if key is None:
                    errors.append(f"P0 {label} lifecycle lacks identity")
                    continue
                table.setdefault(key, []).append(row)

        collect(start_rows, starts_by_key)
        collect(terminal_rows, terminals_by_key)
        if not starts_by_key:
            errors.append(f"P0 trace lacks a correlated {label} lifecycle")
            return
        if set(starts_by_key) != set(terminals_by_key):
            errors.append(f"P0 {label} lifecycle identities are incomplete")
        for key in set(starts_by_key) | set(terminals_by_key):
            start_matches = starts_by_key.get(key, [])
            terminal_matches = terminals_by_key.get(key, [])
            if len(start_matches) != 1 or len(terminal_matches) != 1:
                errors.append(f"P0 {label} lifecycle {key[0]} is not one-to-one")
            elif start_matches[0].get("seq", 0) >= terminal_matches[0].get("seq", 0):
                errors.append(f"P0 {label} lifecycle {key[0]} is misordered")

    require_lifecycles(starts, completed, lambda event: event.get("agent_step_id"), "agent")

    model_starts = rows("k11.model_call_started", within_window=False)
    model_terminals = (
        rows("k11.model_call_completed", within_window=False)
        + rows("k11.model_call_failed", within_window=False)
    )
    require_lifecycles(
        model_starts, model_terminals,
        lambda event: event.get("payload", {}).get("model_call_id"), "model",
    )

    tool_enters = rows("k11.tool_call_entered", within_window=False)
    tool_exits = rows("k11.tool_call_exited", within_window=False)
    require_lifecycles(tool_enters, tool_exits, lambda event: event.get("tool_call_id"), "tool")
    for evidence in rows("k11.eac_evidence_ingested"):
        if not valid_evidence_ingestion(evidence, run_id=artifact_run_id):
            errors.append("P0 evidence ingestion is malformed or lacks replay identity")
    prepared = [event for event in rows("k11.eac_action_prepared")
                if event.get("payload", {}).get("exact_request", {}).get("action", {}).get("identity") in PRIMARY_EFFECT_ACTIONS]
    if not prepared:
        errors.append("P0 trace lacks a primary preparation")

    decisions = rows("k11.eac_execution_decision_attempted")
    native_entries = rows("k11.eac_native_effect_entered")
    all_prepared_ids = {
        row.get("payload", {}).get("exact_request", {}).get("candidate_id")
        for row in rows("k11.eac_action_prepared")
    }
    def belongs_to_measured_preparation(event: Mapping[str, Any]) -> bool:
        return _candidate_id(event) in all_prepared_ids

    terminals = [
        event for event in rows("k11.eac_action_terminal", within_window=False)
        if belongs_to_measured_preparation(event)
    ]
    native_entry_candidate_ids = {
        _candidate_id(event)
        for event in native_entries
    }
    native_completions = [
        event for event in rows("k11.eac_native_effect_completed", within_window=False)
        if _candidate_id(event) in native_entry_candidate_ids
    ]
    for row in decisions + terminals + native_entries + native_completions:
        row_request = row.get("payload", {}).get("exact_request")
        row_candidate = row_request.get("candidate_id") if isinstance(row_request, Mapping) else None
        if not isinstance(row_candidate, str) or not row_candidate:
            errors.append(f"P0 {row.get('event_type')} event lacks candidate identity")
        elif row_candidate not in all_prepared_ids:
            errors.append(f"P0 {row.get('event_type')} event has no preparation")
        row_digest = row.get("payload", {}).get("exact_request_digest")
        if isinstance(row_request, Mapping) and row_digest != exact_request_digest(row_request):
            errors.append(f"P0 {row.get('event_type')} event has an invalid exact request digest")
    for event in prepared:
        request = event.get("payload", {}).get("exact_request", {})
        candidate = request.get("candidate_id") if isinstance(request, Mapping) else None
        attempt = request.get("attempt_id") if isinstance(request, Mapping) else None
        digest = event.get("payload", {}).get("exact_request_digest")
        scope = tuple(event.get(field) for field in (
            "actor_id", "task_id", "agent_step_id", "tool_call_id",
        ))
        if (not isinstance(candidate, str) or not candidate
                or not isinstance(attempt, str) or not attempt
                or not isinstance(digest, str) or not digest
                or any(not value for value in scope)):
            errors.append("P0 primary preparation lacks exact request or scoped identity")
        related_decisions = [row for row in decisions if row.get("payload", {}).get("exact_request", {}).get("candidate_id") == candidate]
        related_terminals = [row for row in terminals if row.get("payload", {}).get("exact_request", {}).get("candidate_id") == candidate]
        scoped_artifact = artifact
        if bounds is not None:
            scoped_artifact = dict(artifact)
            scoped_artifact["events"] = [
                item for item in events if event_in_observation_window(item, bounds)
            ]
        positive_disposition = derive_positive_disposition(scoped_artifact, event)
        decision_precedes = None
        if len(related_decisions) == 1 and positive_disposition is not None:
            decision_precedes = _event_precedes(related_decisions[0], positive_disposition["marker"])
            if decision_precedes is False:
                errors.append(f"primary candidate {candidate} reaches a decision after positive abandonment")
            elif decision_precedes is None:
                errors.append(f"primary candidate {candidate} disposition ordering is ambiguous")
        selected_positive = positive_disposition if not related_decisions else None
        censored = (bounds is not None and bounds[2] == "fixed_observation_horizon"
                    and event_in_observation_window(event, bounds)
                    and not related_decisions)
        if len(related_decisions) != 1 and selected_positive is None and not censored:
            errors.append(f"primary candidate {candidate} lacks exactly one disposition")
        if related_decisions and len(related_terminals) != 1:
            errors.append(f"primary candidate {candidate} lacks exactly one terminal")
        if selected_positive is not None and related_terminals:
            errors.append(f"primary candidate {candidate} has a terminal after abandonment")
        related_dispositions = related_decisions or (
            [selected_positive["marker"]] if selected_positive is not None else []
        )
        if related_dispositions:
            prepare_ns = event.get("monotonic_ns")
            disposition_ns = related_dispositions[0].get("monotonic_ns")
            if (not isinstance(prepare_ns, int) or not isinstance(disposition_ns, int)
                    or disposition_ns <= prepare_ns):
                errors.append(f"primary candidate {candidate} lacks a positive prepare-to-disposition interval")
        related_entries = [row for row in native_entries
                           if row.get("payload", {}).get("exact_request", {}).get("candidate_id") == candidate]
        related_completions = [row for row in native_completions
                               if row.get("payload", {}).get("exact_request", {}).get("candidate_id") == candidate]
        if len(related_entries) not in {0, 1} or len(related_completions) != len(related_entries):
            errors.append(f"primary candidate {candidate} native lifecycle is incomplete or duplicated")
        correlated = related_decisions + related_terminals + related_entries + related_completions
        for row in correlated:
            row_payload = row.get("payload", {})
            row_request = row_payload.get("exact_request")
            row_scope = tuple(row.get(field) for field in (
                "actor_id", "task_id", "agent_step_id", "tool_call_id",
            ))
            if (row_payload.get("exact_request_digest") != digest
                    or row_request != request or row_scope != scope):
                errors.append(f"primary candidate {candidate} exact request digest is not correlated")
        if isinstance(request, Mapping) and digest != exact_request_digest(request):
            errors.append(f"primary candidate {candidate} preparation has an invalid exact request digest")
        ordered = [event] + related_dispositions + related_entries + related_completions + related_terminals
        ordered_sequences = [row.get("seq") for row in ordered]
        if (not censored and (any(not isinstance(value, int) for value in ordered_sequences)
                or ordered_sequences != sorted(ordered_sequences)
                or len(set(ordered_sequences)) != len(ordered_sequences))):
            errors.append(f"primary candidate {candidate} EAC lifecycle is misordered")

    if artifact.get("instrumentation_errors"):
        # Keep this explicit here even though the generic validator also reports it.
        if "instrumentation recorder reported internal errors" not in errors:
            errors.append("instrumentation recorder reported internal errors")
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": generic["warnings"],
        "counts": generic.get("counts", {}),
    }
