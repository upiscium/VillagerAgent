"""Strict parser for the deliberately untrusted K12 worker NDJSON stream."""
from __future__ import annotations
import json
from dataclasses import dataclass
from typing import Any

from benchmarks.common.eac.canonical import canonical_argument, canonical_bytes, thaw_json
from .k12_identity import FROZEN_ARGUMENT_SPECS
from .k12_trace import message_digest

MAX_MESSAGE_BYTES = 64 * 1024
MAX_EVENTS = 256
MAX_TOTAL_BYTES = 4 * 1024 * 1024
WORKER_SCHEMA = "minecraft-k12-worker-message/1"
EVENT_ORDER = (
    "cell_started", "prepared_request_frozen", "invalidation_ingested",
    "current_inadmissible_confirmed", "advisory_would_block", "authority_rejected",
    "rejection_observation_emitted", "recovery_started", "recovery_step", "recovery_step_terminal",
    "model_call_admitted", "model_call_terminal", "observation_started",
    "observation_terminal", "evidence_ingested", "recovery_proposed", "new_request_prepared",
    "permit_issued", "effect_decision", "effect_entered", "effect_terminal",
    "budget_reached", "worker_terminal_candidate",
)
EVENTS = frozenset(EVENT_ORDER)
PARENT_ONLY_EVENTS = frozenset({"current_inadmissible_confirmed", "authority_rejected",
                                "new_request_prepared", "permit_issued", "budget_reached"})
TERMINAL_EVENTS = frozenset({"worker_terminal_candidate"})
REJECTED_FIELDS = frozenset({"source", "sequence", "received_monotonic_ns", "previous_digest",
    "message_digest", "trace_digest", "schema_version", "provenance", "oracle", "authority",
    "recovered", "containment", "reset_invalid", "runtime_failure", "containment_failure",
    "budget_exhausted", "reset_generation", "generation"})
REJECTED_FIELDS = REJECTED_FIELDS | frozenset({
    "steps", "model_calls", "evidence_calls", "effect_attempts",
})
_SEMANTIC_KEYS = frozenset(key for spec in FROZEN_ARGUMENT_SPECS.values() for key in spec)


class WorkerProtocolError(ValueError):
    pass


def parse_recovery_proposal(payload: Any, *, expected_action: str) -> dict[str, Any]:
    value = thaw_json(payload)
    if not isinstance(value, dict) or set(value) != {"action", "arguments"}:
        raise WorkerProtocolError("recovery proposal must contain only action and arguments")
    if value["action"] != expected_action or expected_action not in FROZEN_ARGUMENT_SPECS:
        raise WorkerProtocolError("recovery proposal action mismatch")
    arguments = value["arguments"]
    if not isinstance(arguments, dict) or set(arguments) != set(FROZEN_ARGUMENT_SPECS[expected_action]):
        raise WorkerProtocolError("recovery proposal arguments mismatch")
    for name, declaration in FROZEN_ARGUMENT_SPECS[expected_action].items():
        item = arguments[name]
        if declaration.get("type") == "integer" and type(item) is not int:
            raise WorkerProtocolError(f"proposal argument must be integer: {name}")
        if declaration.get("type") == "string" and not isinstance(item, str):
            raise WorkerProtocolError(f"proposal argument must be string: {name}")
        if "minimum" in declaration and item < declaration["minimum"]:
            raise WorkerProtocolError(f"proposal argument below minimum: {name}")
    canonical_bytes(value)
    return {"action": expected_action, "arguments": dict(arguments)}


@dataclass(frozen=True, slots=True)
class WorkerMessage:
    schema: str
    worker_id: str
    cell_id: str
    triplet_id: str
    arm: str
    event: str
    payload: Any
    digest: str

    @property
    def terminal(self) -> bool:
        return self.event in TERMINAL_EVENTS


def _one(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise WorkerProtocolError(f"{name} must be a bounded non-empty string")
    return value


def _reject_nested(value: Any) -> None:
    if isinstance(value, dict):
        keys = {str(key).lower() for key in value}
        if (REJECTED_FIELDS.intersection(keys)
                or any(term in key for key in keys for term in
                    ("provenance", "oracle", "authority", "recovered", "containment"))
                or any(key.startswith("budget") or (key not in _SEMANTIC_KEYS and key.endswith(("_count", "_counts", "_total", "_totals")))
                       for key in keys)):
            raise WorkerProtocolError("worker payload contains parent-owned authority")
        for item in value.values():
            _reject_nested(item)
    elif isinstance(value, list):
        for item in value:
            _reject_nested(item)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkerProtocolError("duplicate JSON object key")
        result[key] = value
    return result


def parse_worker_message(line: bytes | str, *, expected_worker_id: str | None = None,
                         expected_cell_id: str | None = None, expected_triplet_id: str | None = None,
                         expected_arm: str | None = None) -> WorkerMessage:
    raw = line.encode() if isinstance(line, str) else line
    if not isinstance(raw, bytes) or len(raw) > MAX_MESSAGE_BYTES:
        raise WorkerProtocolError("worker message exceeds byte bound")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise WorkerProtocolError("worker message is not valid JSON") from exc
    fields = {"schema", "worker_id", "cell_id", "triplet_id", "arm", "event", "payload"}
    if not isinstance(value, dict) or set(value) != fields:
        raise WorkerProtocolError("worker message has the exact field set required by /1")
    if value["schema"] != WORKER_SCHEMA:
        raise WorkerProtocolError("unknown worker schema")
    worker_id = _one(value["worker_id"], "worker_id")
    if expected_worker_id is not None and worker_id != expected_worker_id:
        raise WorkerProtocolError("worker identity mismatch")
    cell_id = _one(value["cell_id"], "cell_id")
    triplet_id = _one(value["triplet_id"], "triplet_id")
    arm = _one(value["arm"], "arm")
    for actual, expected, name in ((cell_id, expected_cell_id, "cell_id"), (triplet_id, expected_triplet_id, "triplet_id"), (arm, expected_arm, "arm")):
        if expected is not None and actual != expected:
            raise WorkerProtocolError(f"{name} identity mismatch")
    event = _one(value["event"], "event")
    if event not in EVENTS:
        raise WorkerProtocolError("unknown worker event")
    if (event in PARENT_ONLY_EVENTS or (event == "recovery_proposed" and arm != "R")
            or (arm != "A" and event in {"effect_decision", "effect_entered", "effect_terminal"})):
        raise WorkerProtocolError("worker event crosses the parent authority boundary")
    payload = value["payload"]
    _reject_nested(payload)
    try:
        canonical_bytes(payload)
    except Exception as exc:
        raise WorkerProtocolError("payload is not bounded canonical JSON") from exc
    return WorkerMessage(WORKER_SCHEMA, worker_id, cell_id, triplet_id, arm, event,
                         canonical_argument(payload), message_digest(value))


class WorkerStream:
    def __init__(self, worker_id: str, *, cell_id: str | None = None, triplet_id: str | None = None,
                 arm: str | None = None, max_message_bytes: int = MAX_MESSAGE_BYTES,
                 max_total_bytes: int = MAX_TOTAL_BYTES, max_events: int = MAX_EVENTS) -> None:
        self.worker_id = _one(worker_id, "worker_id")
        if type(max_message_bytes) is not int or max_message_bytes <= 0 or max_message_bytes > MAX_MESSAGE_BYTES:
            raise ValueError("invalid message bound")
        self.max_message_bytes = max_message_bytes
        self.cell_id, self.triplet_id, self.arm = cell_id, triplet_id, arm
        if type(max_total_bytes) is not int or max_total_bytes < max_message_bytes or max_total_bytes > MAX_TOTAL_BYTES:
            raise ValueError("invalid total byte bound")
        if type(max_events) is not int or max_events <= 0 or max_events > MAX_EVENTS:
            raise ValueError("invalid event bound")
        self.max_total_bytes, self.max_events = max_total_bytes, max_events
        self._total_bytes = 0
        self._seen: set[str] = set()
        self._last_rank = -1
        self._terminal = False
        self._operations: dict[str, tuple[str, str]] = {}
        self._operation_ids: set[str] = set()

    @staticmethod
    def _operation(message: WorkerMessage) -> tuple[str, str, str] | None:
        """Return (kind, id, state) for an operation event.

        ``operation_id`` is the sole canonical spelling at this boundary.
        """
        payload = thaw_json(message.payload)
        if not isinstance(payload, dict):
            payload = {}
        names = {
            "model_call_admitted": ("model_call", "model_call_id"),
            "model_call_terminal": ("model_call", "model_call_id"),
            "observation_started": ("observation", "observation_id"),
            "observation_terminal": ("observation", "observation_id"),
            "effect_entered": ("effect", "effect_id"),
            "effect_terminal": ("effect", "effect_id"),
        }
        if message.event in {"recovery_step", "recovery_step_terminal"}:
            kind, state = "recovery_step", ("start" if message.event == "recovery_step" else "terminal")
        elif message.event == "evidence_ingested":
            kind, state = "evidence", "atomic"
        elif message.event == "recovery_proposed":
            return "proposal", message.digest, "start"
        elif message.event in names:
            kind = names[message.event][0]
            state = "start" if message.event.endswith(("admitted", "started", "entered")) else "terminal"
        else:
            return None
        identifier = payload.get("operation_id")
        if not isinstance(identifier, str) or not identifier or len(identifier) > 256:
            raise WorkerProtocolError(f"{message.event} requires a bounded operation_id")
        return kind, identifier, state

    def accept(self, line: bytes | str) -> WorkerMessage:
        raw = line.encode() if isinstance(line, str) else line
        if not isinstance(raw, bytes) or len(raw) > self.max_message_bytes:
            raise WorkerProtocolError("worker message exceeds byte bound")
        if len(self._seen) >= self.max_events or self._total_bytes + len(raw) > self.max_total_bytes:
            raise WorkerProtocolError("worker stream budget exceeded")
        message = parse_worker_message(raw, expected_worker_id=self.worker_id,
                                       expected_cell_id=self.cell_id, expected_triplet_id=self.triplet_id,
                                       expected_arm=self.arm)
        if message.digest in self._seen:
            raise WorkerProtocolError("duplicate worker message")
        if self._terminal:
            raise WorkerProtocolError("event received after terminal")
        rank = EVENT_ORDER.index(message.event)
        operation = self._operation(message)
        # The structural envelope is ordered; operations are deliberately
        # exempt from that ordering so bounded, repeated legal cycles work.
        if self._last_rank < 0 and message.event != "cell_started":
            raise WorkerProtocolError("out-of-order or post-terminal worker event")
        if operation is None and rank <= self._last_rank:
            raise WorkerProtocolError("invalid worker lifecycle transition")
        if operation is not None:
            kind, identifier, state = operation
            if state != "terminal" and identifier in self._operation_ids:
                raise WorkerProtocolError("duplicate operation id")
            if state == "atomic" and self._operations:
                raise WorkerProtocolError("interleaved operation")
            if state == "terminal":
                # A terminal must immediately match its admitted/entered cell.
                active = self._operations.get(kind)
                if active != (kind, identifier):
                    raise WorkerProtocolError("operation terminal does not match its start")
                del self._operations[kind]
            elif state == "start":
                if self._operations:
                    raise WorkerProtocolError("interleaved operation")
                self._operations[kind] = (kind, identifier)
            self._operation_ids.add(identifier)
        elif message.event in {"new_request_prepared", "worker_terminal_candidate"} and self._operations:
            raise WorkerProtocolError("active operation at lifecycle boundary")
        self._seen.add(message.digest); self._total_bytes += len(raw); self._last_rank = rank
        self._last_event = message.event
        if message.terminal: self._terminal = True
        return message

    def preview(self, line: bytes | str) -> WorkerMessage:
        """Validate one transition against a cloned FSM without committing it."""
        clone = WorkerStream(
            self.worker_id, cell_id=self.cell_id, triplet_id=self.triplet_id, arm=self.arm,
            max_message_bytes=self.max_message_bytes, max_total_bytes=self.max_total_bytes,
            max_events=self.max_events)
        clone._total_bytes = self._total_bytes
        clone._seen = set(self._seen)
        clone._last_rank = self._last_rank
        clone._terminal = self._terminal
        clone._operations = dict(self._operations)
        clone._operation_ids = set(self._operation_ids)
        return clone.accept(line)

    def complete_parent_operation(self, kind: str, operation_id: str) -> None:
        if self._operations.get(kind) != (kind, operation_id):
            raise WorkerProtocolError("parent terminal does not match active worker operation")
        del self._operations[kind]


__all__ = ["EVENTS", "EVENT_ORDER", "MAX_MESSAGE_BYTES", "PARENT_ONLY_EVENTS",
           "TERMINAL_EVENTS", "WORKER_SCHEMA", "WorkerMessage", "WorkerProtocolError",
           "WorkerStream", "parse_recovery_proposal", "parse_worker_message"]
