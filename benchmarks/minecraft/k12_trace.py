"""Canonical, parent-owned recovery trace primitives for K12."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from benchmarks.common.eac.canonical import canonical_argument, canonical_bytes, canonical_sha256

TRACE_SCHEMA = "minecraft-k12-recovery-trace/1"
GENESIS_DIGEST = "sha256:" + "0" * 64


@dataclass(frozen=True, slots=True)
class TraceRecord:
    source: str
    sequence: int
    received_monotonic_ns: int
    message_digest: str
    previous_digest: str
    event: str
    worker_id: str
    payload: Any
    cell_id: str = ""
    triplet_id: str = ""
    arm: str = ""
    schema: str = TRACE_SCHEMA
    _stored_digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", canonical_argument(self.payload))
        if not self._stored_digest:
            object.__setattr__(self, "_stored_digest", canonical_sha256(self.unsigned()))

    def unsigned(self) -> dict[str, Any]:
        return {"schema": self.schema, "source": self.source, "sequence": self.sequence,
                "received_monotonic_ns": self.received_monotonic_ns,
                "message_digest": self.message_digest, "previous_digest": self.previous_digest,
                "event": self.event, "worker_id": self.worker_id, "cell_id": self.cell_id,
                "triplet_id": self.triplet_id, "arm": self.arm, "payload": self.payload}

    @property
    def digest(self) -> str:
        return self._stored_digest


def message_digest(message: dict[str, Any]) -> str:
    """Digest only the untrusted worker message, using the shared canonicalizer."""
    return canonical_sha256(message)


def trace_digest(record: TraceRecord) -> str:
    if not isinstance(record, TraceRecord):
        raise TypeError("TraceRecord is required")
    return record.digest


class TraceChain:
    def __init__(self, *, source: str = "minecraft-k12-parent", clock_ns=None) -> None:
        if not isinstance(source, str) or not source:
            raise ValueError("source is required")
        self.source = source
        self._clock_ns = clock_ns or __import__("time").monotonic_ns
        self._records: list[TraceRecord] = []
        self._last_time: int | None = None

    @property
    def records(self) -> tuple[TraceRecord, ...]:
        return tuple(self._records)

    def append(self, *, worker_id: str, event: str, payload: Any,
               message: dict[str, Any], received_monotonic_ns: int | None = None,
               cell_id: str = "", triplet_id: str = "", arm: str = "") -> TraceRecord:
        seq = len(self._records)
        previous = self._records[-1].digest if self._records else GENESIS_DIGEST
        record = TraceRecord(self.source, seq, int(self._clock_ns() if received_monotonic_ns is None
                                                    else received_monotonic_ns),
                              message_digest(message), previous, event, worker_id, payload,
                              cell_id, triplet_id, arm)
        if self._last_time is not None and record.received_monotonic_ns <= self._last_time:
            raise ValueError("trace receive time must be monotonic")
        self._records.append(record)
        self._last_time = record.received_monotonic_ns
        return record

    def verify(self) -> bool:
        previous = GENESIS_DIGEST
        for expected, record in enumerate(self._records):
            if record.schema != TRACE_SCHEMA or record.source != self.source:
                return False
            if record.sequence != expected or record.previous_digest != previous:
                return False
            if record.digest != canonical_sha256(record.unsigned()):
                return False
            previous = record.digest
        return True


__all__ = ["GENESIS_DIGEST", "TRACE_SCHEMA", "TraceChain", "TraceRecord", "message_digest", "trace_digest"]
