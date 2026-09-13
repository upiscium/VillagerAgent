"""Parent-side admission and evidence joining for worker events."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .k12_trace import TRACE_SCHEMA, TraceChain, TraceRecord
from .k12_worker_protocol import WorkerMessage, WorkerProtocolError, WorkerStream


class ParentEventError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ParentEvent:
    schema: str
    source: str
    sequence: int
    received_monotonic_ns: int
    message_digest: str
    previous_digest: str
    worker_id: str
    cell_id: str
    triplet_id: str
    arm: str
    event: str
    payload: Any
    trace_digest: str

    @property
    def terminal(self) -> bool:
        return self.event in {"worker_terminal_candidate", "cell_terminal"}

    def unsigned(self) -> dict[str, Any]:
        return {"schema": self.schema, "source": self.source, "sequence": self.sequence,
                "received_monotonic_ns": self.received_monotonic_ns, "message_digest": self.message_digest,
                "previous_digest": self.previous_digest, "event": self.event, "worker_id": self.worker_id,
                "cell_id": self.cell_id, "triplet_id": self.triplet_id, "arm": self.arm, "payload": self.payload}

    @property
    def digest(self) -> str:
        return self.trace_digest

    @classmethod
    def from_record(cls, record: TraceRecord) -> "ParentEvent":
        return cls(schema=record.schema, source=record.source, sequence=record.sequence,
                   received_monotonic_ns=record.received_monotonic_ns,
                   message_digest=record.message_digest, previous_digest=record.previous_digest,
                   worker_id=record.worker_id, event=record.event, payload=record.payload,
                   trace_digest=record.digest, cell_id=record.cell_id,
                   triplet_id=record.triplet_id, arm=record.arm)


class ParentEventLog:
    """The only component allowed to assign trace provenance."""
    def __init__(self, worker_id: str, *, cell_id: str | None = None, triplet_id: str | None = None,
                 arm: str | None = None, source: str = "minecraft-k12-parent", clock_ns=None) -> None:
        self.stream = WorkerStream(worker_id, cell_id=cell_id, triplet_id=triplet_id, arm=arm)
        self.chain = TraceChain(source=source, clock_ns=clock_ns)
        self._by_message: dict[str, ParentEvent] = {}
        self._terminal = False
        self._process_finalized = False
        self._cell_terminal = False
        self._reset_attested = False
        self._oracle_evaluated = False
        self._parent_effect_operation: str | None = None
        self._parent_operation_ids: set[str] = set()

    @property
    def events(self) -> tuple[ParentEvent, ...]:
        return tuple(self._by_message.values())

    def receive(self, line: bytes | str, *, received_monotonic_ns: int | None = None) -> ParentEvent:
        try:
            message = self.stream.accept(line)
        except WorkerProtocolError as exc:
            raise ParentEventError(str(exc)) from exc
        if message.digest in self._by_message:
            raise ParentEventError("duplicate message digest")
        record = self.chain.append(worker_id=message.worker_id, cell_id=message.cell_id,
                                   triplet_id=message.triplet_id, arm=message.arm, event=message.event,
                                   payload=message.payload, message={"schema": "minecraft-k12-worker-message/1",
                                                                       "worker_id": message.worker_id,
                                                                       "cell_id": message.cell_id,
                                                                       "triplet_id": message.triplet_id,
                                                                       "arm": message.arm,
                                                                       "event": message.event,
                                                                       "payload": message.payload},
                                   received_monotonic_ns=received_monotonic_ns)
        event = ParentEvent.from_record(record)
        self._by_message[message.digest] = event
        self._terminal = message.terminal
        return event

    def preview(self, line: bytes | str) -> WorkerMessage:
        try:
            return self.stream.preview(line)
        except WorkerProtocolError as exc:
            raise ParentEventError(str(exc)) from exc

    def append_process_finalized(self, *, cell_id: str, triplet_id: str, arm: str,
                                 payload: Any = None, received_monotonic_ns: int | None = None) -> ParentEvent:
        return self._append_parent("process_finalized", cell_id, triplet_id, arm, payload,
                                   received_monotonic_ns)

    def append_budget_reached(self, *, cell_id: str, triplet_id: str, arm: str,
                              payload: Any = None,
                              received_monotonic_ns: int | None = None) -> ParentEvent:
        return self._append_parent("budget_reached", cell_id, triplet_id, arm, payload,
                                   received_monotonic_ns)

    def append_authority_event(self, event: str, *, cell_id: str, triplet_id: str,
                               arm: str, payload: Any = None,
                               received_monotonic_ns: int | None = None) -> ParentEvent:
        allowed = {"current_inadmissible_confirmed", "authority_rejected",
                   "proposal_validated", "new_request_prepared", "permit_issued",
                   "effect_decision", "effect_entered", "effect_terminal"}
        if event not in allowed:
            raise ParentEventError("unknown parent authority event")
        if self._terminal:
            raise ParentEventError("authority event cannot follow worker terminal")
        prior = tuple(item.event for item in self.events)
        requirements = {
            "authority_rejected": "current_inadmissible_confirmed",
            "proposal_validated": "recovery_proposed",
            "new_request_prepared": "proposal_validated",
            "permit_issued": "new_request_prepared",
            "effect_decision": "permit_issued",
            "effect_entered": "effect_decision",
            "effect_terminal": "effect_entered",
        }
        required = requirements.get(event)
        if required is not None and (required not in prior or event in prior):
            raise ParentEventError("parent authority event is out of order or duplicated")
        if event == "proposal_validated":
            if not isinstance(payload, dict) or not isinstance(payload.get("proposal_message_digest"), str):
                raise ParentEventError("proposal validation requires its operation identity")
            if (self._process_finalized or self._cell_terminal
                    or any((actual != expected) for actual, expected in (
                        (cell_id, self.stream.cell_id), (triplet_id, self.stream.triplet_id),
                        (arm, self.stream.arm)) if expected is not None)):
                raise ParentEventError("proposal validation identity or lifecycle mismatch")
            try:
                self.stream.complete_parent_operation("proposal", payload["proposal_message_digest"])
            except WorkerProtocolError as exc:
                raise ParentEventError(str(exc)) from exc
        operation_id = payload.get("operation_id") if isinstance(payload, dict) else None
        if event == "effect_entered":
            if (not isinstance(operation_id, str) or not operation_id
                    or self._parent_effect_operation is not None
                    or operation_id in self._parent_operation_ids):
                raise ParentEventError("invalid parent effect operation start")
        elif event == "effect_terminal" and operation_id != self._parent_effect_operation:
            raise ParentEventError("parent effect terminal does not match its start")
        result = self._append_parent(event, cell_id, triplet_id, arm, payload,
                                     received_monotonic_ns)
        if event == "effect_entered":
            self._parent_effect_operation = operation_id
            self._parent_operation_ids.add(operation_id)
        elif event == "effect_terminal":
            self._parent_effect_operation = None
        return result

    def append_reset_attested(self, *, cell_id: str, triplet_id: str, arm: str,
                               payload: Any = None, received_monotonic_ns: int | None = None) -> ParentEvent:
        return self._append_parent("reset_attested", cell_id, triplet_id, arm, payload,
                                   received_monotonic_ns)

    def append_objective_oracle_evaluated(self, *, cell_id: str, triplet_id: str, arm: str,
                                          payload: Any = None, received_monotonic_ns: int | None = None) -> ParentEvent:
        return self._append_parent("objective_oracle_evaluated", cell_id, triplet_id, arm, payload,
                                   received_monotonic_ns)

    def append_cell_terminal(self, *, cell_id: str, triplet_id: str, arm: str,
                             payload: Any = None, received_monotonic_ns: int | None = None) -> ParentEvent:
        return self._append_parent("cell_terminal", cell_id, triplet_id, arm, payload,
                                   received_monotonic_ns)

    def _append_parent(self, event: str, cell_id: str, triplet_id: str, arm: str,
                       payload: Any, received_monotonic_ns: int | None) -> ParentEvent:
        for actual, expected, name in ((cell_id, self.stream.cell_id, "cell_id"),
                                       (triplet_id, self.stream.triplet_id, "triplet_id"),
                                       (arm, self.stream.arm, "arm")):
            if expected is not None and actual != expected:
                raise ParentEventError(f"{name} identity mismatch")
        if event == "reset_attested":
            if self._reset_attested or self._terminal:
                raise ParentEventError("reset_attested must precede worker events and occur once")
        elif event == "objective_oracle_evaluated":
            if not self._terminal or self._oracle_evaluated or self._process_finalized:
                raise ParentEventError("objective oracle requires the worker terminal")
        elif event == "process_finalized":
            failure = isinstance(payload, dict) and any(payload.get(key) is True for key in
                ("runtime_failure", "reset_invalid", "containment_failure", "budget_exhausted"))
            if (not self._terminal and not failure) or self._process_finalized or self._parent_effect_operation is not None:
                raise ParentEventError("process_finalized requires worker terminal or parent failure")
        elif event == "cell_terminal":
            if not self._process_finalized or self._cell_terminal:
                raise ParentEventError("cell_terminal requires process_finalized")
        elif event == "budget_reached":
            if self._process_finalized or any(item.event == "budget_reached" for item in self.events):
                raise ParentEventError("budget_reached must precede finalization")
        elif event in {"current_inadmissible_confirmed", "authority_rejected",
                       "proposal_validated", "new_request_prepared", "permit_issued",
                       "effect_decision", "effect_entered", "effect_terminal"}:
            if self._process_finalized or self._cell_terminal:
                raise ParentEventError("authority event after finalization")
        else:
            raise ParentEventError("unknown parent event")
        record = self.chain.append(worker_id=self.stream.worker_id, cell_id=cell_id,
                                   triplet_id=triplet_id, arm=arm, event=event,
                                   payload={} if payload is None else payload,
                                   message={"parent_event": event, "cell_id": cell_id,
                                            "triplet_id": triplet_id, "arm": arm,
                                            "payload": {} if payload is None else payload},
                                   received_monotonic_ns=received_monotonic_ns)
        result = ParentEvent.from_record(record)
        self._by_message[record.message_digest] = result
        if event == "reset_attested":
            self._reset_attested = True
        elif event == "objective_oracle_evaluated":
            self._oracle_evaluated = True
        elif event == "process_finalized":
            self._process_finalized = True
        elif event == "cell_terminal":
            self._cell_terminal = True
        return result

    def by_digest(self, digest: str) -> ParentEvent:
        try:
            return self._by_message[digest]
        except KeyError as exc:
            raise ParentEventError("unknown evidence digest") from exc

    def verify(self) -> bool:
        return self.chain.verify() and all(event.schema == TRACE_SCHEMA for event in self.events)


__all__ = ["ParentEvent", "ParentEventError", "ParentEventLog"]
