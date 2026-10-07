"""Fixed-cadence passive sensing scheduler for the bounded K11 hold scope.

This module schedules passive sensor requests only. It has no concept of
actions, targets, outcomes, Delta labels, Minecraft clients, or RCON. Controller
timestamps come from the injected monotonic clock and are never compared with
bridge timestamps.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from benchmarks.minecraft.k11_hold_trace import (
    CADENCE_NS, DEADLINE_NS, MAX_FATAL_COMMITTED_CELLS, MAX_TRACE_EVENTS,
    POLICY, REQUEST_KEYS, REQUEST_SCHEMA, TRACE_EVENTS,
    TRACE_EVENTS_PER_ACTOR_TICK, TRACE_SCHEMA, validate_request_payload,
)

MAX_PAYLOAD_BYTES = 64 * 1024
_SAFE_INTEGER = 2**53 - 1
_MAX_CANONICAL_ITEMS = 16_384
_MAX_CANONICAL_DEPTH = 32
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}\Z")
_PHASE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_BLOCK_RE = re.compile(r"[a-z0-9_]{1,128}\Z")
_STATES = frozenset({"known_air", "known_non_air", "unknown"})
_DISPOSITIONS = frozenset({"committed", "semantic_noop", "unknown"})
_AIR_NAMES = frozenset({"air", "cave_air", "void_air"})
_UNKNOWN_REASONS = frozenset({
    "unloaded_target", "unloaded_path", "occluded", "outside_region",
    "invalid_pose", "step_limit", "incoherent_capture", "unmapped_registry",
})


def snapshot_canonical_bytes(value: Any) -> bytes:
    """Encode a bounded sensor envelope in the constrained JCS value domain."""
    items = 0
    string_bytes = 0
    active: set[int] = set()

    def add_item() -> None:
        nonlocal items
        items += 1
        if items > _MAX_CANONICAL_ITEMS:
            raise ValueError("snapshot canonical item count exceeds bound")

    def visit(current: Any, depth: int) -> None:
        nonlocal string_bytes
        if depth > _MAX_CANONICAL_DEPTH:
            raise ValueError("snapshot canonical depth exceeds bound")
        if current is None or isinstance(current, bool):
            add_item()
            return
        if isinstance(current, str):
            if len(current) > MAX_PAYLOAD_BYTES or any("\ud800" <= c <= "\udfff" for c in current):
                raise ValueError("snapshot canonical string is invalid or too large")
            token = json.dumps(current, ensure_ascii=False, separators=(",", ":"))
            string_bytes += len(token.encode("utf-8"))
            if string_bytes > MAX_PAYLOAD_BYTES:
                raise ValueError("snapshot canonical value exceeds byte bound")
            add_item()
            return
        if isinstance(current, int):
            if abs(current) > _SAFE_INTEGER:
                raise ValueError("snapshot integer is outside the RFC 8785 safe range")
            add_item()
            return
        if isinstance(current, (float, complex)):
            raise ValueError("floating point values are not permitted in sensor envelopes")
        if isinstance(current, (list, dict)):
            ident = id(current)
            if ident in active:
                raise ValueError("recursive sensor envelope is not permitted")
            add_item()
            if len(current) > _MAX_CANONICAL_ITEMS - items:
                raise ValueError("snapshot canonical item count exceeds bound")
            active.add(ident)
            try:
                if isinstance(current, list):
                    for child in current:
                        visit(child, depth + 1)
                else:
                    for key, child in current.items():
                        if not isinstance(key, str) or not key.isascii():
                            raise ValueError("snapshot object keys must be ASCII strings")
                        visit(key, depth + 1)
                        visit(child, depth + 1)
            finally:
                active.remove(ident)
            return
        raise ValueError(f"unsupported snapshot canonical value type: {type(current).__name__}")

    visit(value, 0)
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"), check_circular=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError) as exc:
        raise ValueError("cannot canonically encode sensor envelope") from exc
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise ValueError("snapshot canonical value exceeds byte bound")
    return encoded


class HoldProtocolError(ValueError):
    """Invalid fixed-passive-sensing configuration or callback value."""


class K11RejectedBeforeMutation(ValueError):
    """Ingest rejection whose producer proves that no semantic mutation occurred.

    Callback implementations must raise this type only after proving that the
    rejected callback made zero semantic changes. Other exceptions after the
    scheduler admits a callback have unknown mutation status and censor the run.
    """


def _identifier(value: Any, name: str) -> str:
    if (not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None
            or re.fullmatch(r"[0-9a-f]{32}|[0-9a-f]{64}", value)):
        raise ValueError(f"invalid {name}")
    return value


def _coordinate(value: Any, name: str) -> list[int] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        if set(value) != {"x", "y", "z"}:
            raise ValueError(f"invalid {name}")
        value = [value["x"], value["y"], value["z"]]
    if (not isinstance(value, (list, tuple)) or len(value) != 3
            or any(type(part) is not int or abs(part) > 2**31 - 1 for part in value)):
        raise ValueError(f"invalid {name}")
    return list(value)


def _normalize_raw_observations(
    observations: Any, raw_cell_count: Any,
) -> list[dict[str, Any]]:
    """Retain the exact pre-mutation 75-cell account, never a commit ledger."""
    if type(raw_cell_count) is not int or raw_cell_count != MAX_FATAL_COMMITTED_CELLS:
        raise ValueError("fatal raw_cell_count must be exactly 75")
    if not isinstance(observations, (list, tuple)) or len(observations) != MAX_FATAL_COMMITTED_CELLS:
        raise ValueError("fatal raw_observations must contain exactly 75 cells")
    common = {"cell_index", "coordinate", "state"}
    normalized: list[dict[str, Any]] = []
    for expected_index, raw in enumerate(observations):
        if not isinstance(raw, Mapping):
            raise ValueError("fatal raw observation is malformed")
        item = dict(raw)
        index = item.get("cell_index")
        if type(index) is not int or index != expected_index:
            raise ValueError("fatal raw observations must enumerate cell indices 0..74")
        coordinate = _coordinate(item.get("coordinate"), "raw observation coordinate")
        state = item.get("state")
        if coordinate is None or not isinstance(state, str) or state not in _STATES:
            raise ValueError("fatal raw observation state or coordinate is invalid")
        result: dict[str, Any] = {
            "cell_index": index, "coordinate": coordinate, "state": state,
        }
        if state == "unknown":
            if set(item) != common | {"unknown_reason"}:
                raise ValueError("unknown fatal raw observations must contain only their reason")
            reason = item.get("unknown_reason")
            if not isinstance(reason, str) or reason not in _UNKNOWN_REASONS:
                raise ValueError("fatal raw observation unknown_reason is invalid")
            result["unknown_reason"] = reason
        else:
            if set(item) != common | {"block_name", "registry_id"}:
                raise ValueError("known fatal raw observations have unsupported fields")
            name, registry_id = item.get("block_name"), item.get("registry_id")
            if not isinstance(name, str) or _BLOCK_RE.fullmatch(name) is None:
                raise ValueError("fatal raw observation block_name is invalid")
            if (state == "known_air") != (name in _AIR_NAMES):
                raise ValueError("fatal raw observation state differs from block_name")
            if type(registry_id) is not int or not 0 <= registry_id < 2**63:
                raise ValueError("fatal raw observation registry_id is invalid")
            result.update({"block_name": name, "registry_id": registry_id})
        normalized.append(result)
    return normalized


def _normalize_observations(
    observations: Sequence[Mapping[str, Any]], committed_cells: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    if isinstance(observations, (str, bytes, Mapping)) or not isinstance(observations, Sequence):
        raise TypeError("K11 observations must be a sequence")
    if len(observations) > MAX_FATAL_COMMITTED_CELLS:
        raise ValueError("K11 observations exceed the 75-cell bound")
    required = {"cell_index", "coordinate", "state", "disposition"}
    seen: set[int] = set()
    result: list[dict[str, Any]] = []
    for raw in observations:
        if not isinstance(raw, Mapping):
            raise ValueError("invalid K11 observation")
        item = dict(raw)
        if not required.issubset(item):
            raise ValueError("K11 observation fields are incomplete")
        index = item["cell_index"]
        if type(index) is not int or not 0 <= index < 75 or index in seen:
            raise ValueError("invalid K11 observation cell_index")
        state, disposition = item["state"], item["disposition"]
        if not isinstance(state, str) or state not in _STATES:
            raise ValueError("invalid K11 observation state")
        if not isinstance(disposition, str) or disposition not in _DISPOSITIONS:
            raise ValueError("invalid K11 observation disposition")
        coordinate = _coordinate(item["coordinate"], "observation coordinate")
        if coordinate is None:
            raise ValueError("K11 observation coordinate is required")
        allowed = required | {"root_id"}
        normalized: dict[str, Any] = {
            "cell_index": index, "coordinate": coordinate,
            "state": state, "disposition": disposition,
        }
        if state == "unknown":
            if disposition != "unknown" or {"block_name", "registry_id"} & set(item):
                raise ValueError("unknown K11 observations must not expose block identity")
            reason = item.get("unknown_reason")
            if "unknown_reason" in item:
                allowed.add("unknown_reason")
                if not isinstance(reason, str) or reason not in _UNKNOWN_REASONS:
                    raise ValueError("invalid K11 observation unknown_reason")
                normalized["unknown_reason"] = reason
            if "root_id" in item:
                raise ValueError("unknown K11 observations cannot name a root")
        else:
            if disposition not in {"committed", "semantic_noop"}:
                raise ValueError("known K11 observations require a commit disposition")
            allowed.update({"block_name", "registry_id"})
            name = item.get("block_name")
            registry = item.get("registry_id")
            if not isinstance(name, str) or _BLOCK_RE.fullmatch(name) is None:
                raise ValueError("invalid K11 observation block_name")
            if (state == "known_air") != (name in _AIR_NAMES):
                raise ValueError("K11 observation state differs from block_name")
            if type(registry) is not int or not 0 <= registry < 2**63:
                raise ValueError("invalid K11 observation registry_id")
            if disposition == "semantic_noop" and "root_id" in item:
                raise ValueError("semantic no-op observations cannot fabricate a root")
            if "root_id" in item:
                if disposition != "committed":
                    raise ValueError("only committed K11 observations may name a root")
                normalized["root_id"] = _identifier(item["root_id"], "observation root_id")
            normalized["block_name"] = name
            normalized["registry_id"] = registry
        if not set(item).issubset(allowed):
            raise ValueError("K11 observation contains unsupported fields")
        seen.add(index)
        result.append(normalized)
    if [row["cell_index"] for row in result] != sorted(seen):
        raise ValueError("K11 observations are not in capture order")
    cells_by_index = {cell["cell_index"]: cell for cell in committed_cells}
    commits = [row for row in result if row["disposition"] == "committed"]
    if len(commits) != len(committed_cells):
        raise ValueError("K11 observation transitions and committed cells differ in size")
    for row in commits:
        cell = cells_by_index.get(row["cell_index"])
        if (cell is None or row["coordinate"] != cell["coordinate"]
                or cell["polarity"] != (row["state"] == "known_non_air")
                or ("root_id" in row and row["root_id"] != cell["root_id"])):
            raise ValueError("K11 observation transition differs from committed inventory")
    return tuple(result)


def _normalize_fatal_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(receipt, Mapping):
        raise TypeError("fatal_receipt must be a mapping")
    raw = dict(receipt)
    required = {
        "status", "actor_id", "tick_index", "capture_seq", "committed_cells",
        "failing_cell_index", "failing_coordinate", "phase",
    }
    if not required.issubset(raw):
        raise ValueError("fatal receipt is missing required fields")
    status, phase = raw.get("status"), raw.get("phase")
    if not isinstance(status, str) or status not in {"partial_commit_fatal", "sensor_ingest_fatal"}:
        raise ValueError("invalid fatal receipt status")
    actor = _identifier(raw.get("actor_id"), "fatal receipt actor_id")
    tick, capture = raw.get("tick_index"), raw.get("capture_seq")
    if type(tick) is not int or not 0 <= tick < 2**63:
        raise ValueError("invalid fatal receipt tick_index")
    if type(capture) is not int or not 1 <= capture < 2**63:
        raise ValueError("invalid fatal receipt capture_seq")
    if not isinstance(phase, str) or _PHASE_RE.fullmatch(phase) is None:
        raise ValueError("invalid fatal receipt phase")
    cells = raw.get("committed_cells")
    if not isinstance(cells, (list, tuple)) or len(cells) > 75:
        raise ValueError("invalid fatal receipt committed_cells")
    normalized_cells: list[dict[str, Any]] = []
    seen: set[int] = set()
    cell_keys = {
        "cell_index", "coordinate", "root_id", "provenance_id", "polarity",
        "supersedes", "ingest_sequence",
    }
    for cell in cells:
        if not isinstance(cell, Mapping):
            raise ValueError("invalid fatal receipt committed cell")
        item = dict(cell)
        if not cell_keys.issubset(item):
            raise ValueError("fatal receipt committed cell fields are incomplete")
        index, sequence = item["cell_index"], item["ingest_sequence"]
        if type(index) is not int or not 0 <= index < 75 or index in seen:
            raise ValueError("invalid fatal receipt cell_index")
        if type(sequence) is not int or not 1 <= sequence < 2**63:
            raise ValueError("invalid fatal receipt ingest_sequence")
        if type(item["polarity"]) is not bool:
            raise ValueError("invalid fatal receipt polarity")
        supersedes = item["supersedes"]
        if not isinstance(supersedes, (list, tuple)) or len(supersedes) > 75:
            raise ValueError("invalid fatal receipt supersedes")
        coordinate = _coordinate(item["coordinate"], "coordinate")
        if coordinate is None:
            raise ValueError("fatal receipt committed cell coordinate is required")
        normalized_cells.append({
            "cell_index": index, "coordinate": coordinate,
            "root_id": _identifier(item["root_id"], "root_id"),
            "provenance_id": _identifier(item["provenance_id"], "provenance_id"),
            "polarity": item["polarity"],
            "supersedes": [_identifier(value, "supersedes") for value in supersedes],
            "ingest_sequence": sequence,
        })
        seen.add(index)
    if [cell["cell_index"] for cell in normalized_cells] != sorted(seen):
        raise ValueError("fatal receipt committed cells are not in capture order")
    failing_index = raw.get("failing_cell_index")
    failing_coordinate = _coordinate(raw.get("failing_coordinate"), "failing_coordinate")
    if failing_index is not None and (
        type(failing_index) is not int or not 0 <= failing_index < 75
        or (failing_index in seen and phase not in {
            "ingest_record", "runtime_bookkeeping", "persist_audit", "commit_ledger",
        })
    ):
        raise ValueError("invalid fatal receipt failing_cell_index")
    if (failing_index is None) != (failing_coordinate is None):
        raise ValueError("fatal receipt failing cell and coordinate must be paired")
    by_index = {cell["cell_index"]: cell for cell in normalized_cells}
    if (type(failing_index) is int and failing_index in by_index
            and failing_coordinate != by_index[failing_index]["coordinate"]):
        raise ValueError("fatal receipt failing coordinate differs from committed cell")
    if (phase in {"runtime_bookkeeping", "persist_audit", "commit_ledger"}
            and failing_index not in seen):
        raise ValueError("post-insertion fatal receipt must inventory the failed cell root")
    orphan = raw.get("orphan_provenance_id")
    if orphan is not None:
        orphan = _identifier(orphan, "orphan_provenance_id")
    orphan_flag = raw.get("orphan_provenance", raw.get("orphan_provenance_flag", orphan is not None))
    if type(orphan_flag) is not bool or (orphan is not None and not orphan_flag):
        raise ValueError("invalid fatal receipt orphan provenance flag")
    if phase == "capacity_exhausted" and (
        normalized_cells or failing_index is not None or failing_coordinate is not None
        or orphan is not None or orphan_flag
    ):
        raise ValueError("capacity_exhausted fatal receipt must declare zero tick mutations")
    result = {
        "status": "partial_commit_fatal" if normalized_cells else "sensor_ingest_fatal",
        "actor_id": actor, "tick_index": tick, "capture_seq": capture,
        "committed_cells": normalized_cells, "failing_cell_index": failing_index,
        "failing_coordinate": failing_coordinate, "phase": phase,
        "orphan_provenance_id": orphan, "orphan_provenance": orphan_flag,
    }
    has_raw = "raw_observations" in raw
    has_raw_count = "raw_cell_count" in raw
    if has_raw != has_raw_count:
        raise ValueError("fatal raw observations and raw_cell_count must be paired")
    if has_raw:
        normalized_raw = _normalize_raw_observations(
            raw["raw_observations"], raw["raw_cell_count"],
        )
        for cell in normalized_cells:
            observation = normalized_raw[cell["cell_index"]]
            if (observation["state"] == "unknown"
                    or observation["coordinate"] != cell["coordinate"]
                    or cell["polarity"] != (observation["state"] == "known_non_air")):
                raise ValueError("fatal committed inventory differs from raw observation")
        result["raw_observations"] = normalized_raw
        result["raw_cell_count"] = MAX_FATAL_COMMITTED_CELLS
    return result


class K11SensorAdmissionGate:
    """Shared first-fatal gate; publishers never call the scheduler under it."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._fatal_receipt: dict[str, Any] | None = None

    @property
    def fatal_receipt(self) -> dict[str, Any] | None:
        with self.lock:
            return copy.deepcopy(self._fatal_receipt)

    def publish_fatal(self, receipt: Mapping[str, Any]) -> bool:
        """Normalize and latch only the first fatal receipt while holding ``lock``."""
        with self.lock:
            normalized = _normalize_fatal_receipt(receipt)
            if self._fatal_receipt is not None:
                return False
            self._fatal_receipt = copy.deepcopy(normalized)
            return True


class K11FatalSensorIngest(Exception):
    """A validated ingest failed after a bounded, sanitized mutation inventory."""

    def __init__(self, fatal_receipt: Mapping[str, Any]):
        self._fatal_receipt = _normalize_fatal_receipt(fatal_receipt)
        super().__init__("K11 passive sensor ingestion failed; run is censored")

    @property
    def fatal_receipt(self) -> dict[str, Any]:
        return copy.deepcopy(self._fatal_receipt)


@dataclass(frozen=True, slots=True, eq=False, init=False)
class K11CommitReceipt:
    """Tuple-compatible successful ingest result with a detached commit ledger."""
    roots: tuple[Any, ...]
    _committed_cells: tuple[dict[str, Any], ...]
    actor_id: str
    tick_index: int
    capture_seq: int
    _observations: tuple[dict[str, Any], ...] | None

    def __init__(self, roots: Sequence[Any], committed_cells: Sequence[Mapping[str, Any]],
                 actor_id: str, tick_index: int, capture_seq: int,
                 observations: Sequence[Mapping[str, Any]] | None = None) -> None:
        if isinstance(roots, (str, bytes, Mapping)):
            raise TypeError("K11 commit roots must be a sequence")
        root_tuple = tuple(roots)
        cells = _normalize_fatal_receipt({
            "status": "partial_commit_fatal" if committed_cells else "sensor_ingest_fatal",
            "actor_id": actor_id, "tick_index": tick_index, "capture_seq": capture_seq,
            "committed_cells": committed_cells, "failing_cell_index": None,
            "failing_coordinate": None, "phase": "commit_receipt",
            "orphan_provenance_id": None, "orphan_provenance": False,
        })["committed_cells"]
        if len(root_tuple) != len(cells):
            raise ValueError("K11 commit roots and cell inventory differ in size")
        normalized_observations = (
            None if observations is None else _normalize_observations(observations, cells)
        )
        object.__setattr__(self, "roots", root_tuple)
        object.__setattr__(self, "_committed_cells", tuple(cells))
        object.__setattr__(self, "actor_id", _identifier(actor_id, "actor_id"))
        object.__setattr__(self, "tick_index", tick_index)
        object.__setattr__(self, "capture_seq", capture_seq)
        object.__setattr__(self, "_observations", normalized_observations)

    @property
    def committed_cells(self) -> list[dict[str, Any]]:
        return copy.deepcopy(list(self._committed_cells))

    @property
    def observations(self) -> list[dict[str, Any]] | None:
        return None if self._observations is None else copy.deepcopy(list(self._observations))

    def __iter__(self):
        return iter(self.roots)

    def __len__(self) -> int:
        return len(self.roots)

    def __getitem__(self, index):
        return self.roots[index]

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, K11CommitReceipt):
            return (self.roots == other.roots and self.committed_cells == other.committed_cells
                    and self.actor_id == other.actor_id and self.tick_index == other.tick_index
                    and self.capture_seq == other.capture_seq and self.observations == other.observations)
        if isinstance(other, tuple):
            return self.roots == other
        return NotImplemented

    __hash__ = None


@dataclass(frozen=True, slots=True)
class SensorBinding:
    """Non-semantic identity/configuration for one passive sensor actor."""
    actor_id: str
    sensor_id: str
    sensor_digest: str
    profile_digest: str
    ingestion_digest: str
    geometry_id: str
    geometry_digest: str

    def __post_init__(self) -> None:
        for name in ("actor_id", "sensor_id", "sensor_digest", "profile_digest",
                     "ingestion_digest", "geometry_id", "geometry_digest"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise HoldProtocolError(f"{name} must be a non-empty string")


@dataclass(frozen=True, slots=True)
class SensorResponse:
    """Adapter response; bridge time is diagnostic only."""
    payload: bytes
    signature_valid: bool = True
    capture_ok: bool = True
    bridge_monotonic_ns: int | None = None
    request_identity: object | None = None


Transport = Callable[[Mapping[str, Any], Callable[[Any], None]], None]
Ingest = Callable[[str, Mapping[str, Any], bytes, object | None], Any]
Clock = Callable[[], int]


@dataclass(slots=True)
class _Pending:
    actor_id: str
    tick_index: int
    due_ns: int
    deadline_ns: int
    request: Mapping[str, Any]
    request_started: bool = False
    response_started: bool = False
    ingest_started: bool = False
    timed_out: bool = False


@dataclass(slots=True)
class _Actor:
    binding: SensorBinding
    admission_enabled: bool = True
    pending: _Pending | None = None


class FixedPassiveScheduler:
    """Two-actor absolute-cadence scheduler with one flight and no retries."""

    def __init__(self, *, run_id: str, window_id: str, bindings: Sequence[SensorBinding],
                 window_duration_ns: int, transport: Transport, ingest: Ingest,
                 clock_ns: Clock | None = None, t0_ns: int | None = None,
                 nonce_factory: Callable[[], str] | None = None) -> None:
        if not isinstance(run_id, str) or not run_id:
            raise HoldProtocolError("run_id is required")
        if not isinstance(window_id, str) or not window_id:
            raise HoldProtocolError("window_id is required")
        if type(window_duration_ns) is not int or window_duration_ns <= 0:
            raise HoldProtocolError("window_duration_ns must be a positive integer")
        if not callable(transport) or not callable(ingest):
            raise HoldProtocolError("transport and ingest callbacks are required")
        if nonce_factory is not None and not callable(nonce_factory):
            raise HoldProtocolError("nonce_factory must be callable")
        if (len(bindings) != 2 or any(not isinstance(item, SensorBinding) for item in bindings)
                or len({item.actor_id for item in bindings}) != 2):
            raise HoldProtocolError("exactly two distinct SensorBinding actors are required")
        self._clock_ns = clock_ns if clock_ns is not None else time.monotonic_ns
        if not callable(self._clock_ns):
            raise HoldProtocolError("clock_ns must be callable")
        start = self._read_clock()
        if t0_ns is None:
            t0_ns = start
        if type(t0_ns) is not int or t0_ns < 0:
            raise HoldProtocolError("t0_ns must be a non-negative integer")
        self.run_id, self.window_id, self.t0_ns = run_id, window_id, t0_ns
        self.window_duration_ns = window_duration_ns
        self.window_close_ns = t0_ns + window_duration_ns
        self.tick_count = (window_duration_ns + CADENCE_NS - 1) // CADENCE_NS
        self.trace_capacity = TRACE_EVENTS_PER_ACTOR_TICK * self.tick_count * 2 + 2
        if self.trace_capacity > MAX_TRACE_EVENTS:
            raise HoldProtocolError(f"window exceeds bounded trace capacity ({MAX_TRACE_EVENTS})")
        self._transport, self._ingest = transport, ingest
        self._nonce_factory = nonce_factory if nonce_factory is not None else (lambda: secrets.token_hex(16))
        self._nonces: set[str] = set()
        self._bindings = tuple(bindings)
        self._actors = {binding.actor_id: _Actor(binding) for binding in bindings}
        self._admission_gate = K11SensorAdmissionGate()
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._counter = 0
        self._next_tick = 0
        self._closed = False
        self._fatal_receipt: dict[str, Any] | None = None
        self._fatal_receipts_seen: list[dict[str, Any]] = []
        self._unverified_failure: dict[str, Any] | None = None
        self._overflow = False
        self._dropped = 0
        self._counters = {key: 0 for key in (
            "missed", "timeouts", "late_responses", "overlap_skips", "rejected",
            "completed", "ingested", "queue_capacity", "queue_depth_max",
        )}
        with self._admission_gate.lock, self._lock:
            self._record("window_opened", start, status="open", t0_monotonic_ns=t0_ns,
                         window_close_monotonic_ns=self.window_close_ns)
        owner = getattr(ingest, "__self__", None)
        if owner is None and (
            callable(getattr(ingest, "bind_admission_gate", None))
            or callable(getattr(ingest, "bind_fatal_notifier", None))
        ):
            owner = ingest
        gate_binder = getattr(owner, "bind_admission_gate", None)
        if callable(gate_binder):
            gate_binder(self._admission_gate)
        binder = getattr(owner, "bind_fatal_notifier", None)
        if callable(binder):
            binder(self.report_fatal)

    def _read_clock(self) -> int:
        value = self._clock_ns()
        if type(value) is not int or value < 0:
            raise HoldProtocolError("controller monotonic clock must return a non-negative integer")
        return value

    @property
    def actor_ids(self) -> tuple[str, str]:
        return tuple(item.actor_id for item in self._bindings)  # type: ignore[return-value]

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        with self._admission_gate.lock, self._lock:
            self._sync_fatal_admission_locked(self._read_clock())
            return tuple(copy.deepcopy(self._events))

    @property
    def queue_depth(self) -> int:
        return 0

    @property
    def pending_count(self) -> int:
        with self._admission_gate.lock, self._lock:
            self._sync_fatal_admission_locked(self._read_clock())
            return sum(actor.pending is not None for actor in self._actors.values())

    @property
    def counters(self) -> dict[str, int]:
        with self._admission_gate.lock, self._lock:
            self._sync_fatal_admission_locked(self._read_clock())
            return dict(self._counters)

    def _censored(self) -> bool:
        return self._fatal_receipt is not None or self._unverified_failure is not None

    def admission_enabled(self, actor_id: str) -> bool:
        with self._admission_gate.lock, self._lock:
            self._sync_fatal_admission_locked(self._read_clock())
            if actor_id not in self._actors:
                raise HoldProtocolError("unknown actor")
            return self._actors[actor_id].admission_enabled and not self._closed and not self._censored()

    def actor_status(self, actor_id: str) -> dict[str, bool]:
        with self._admission_gate.lock, self._lock:
            self._sync_fatal_admission_locked(self._read_clock())
            if actor_id not in self._actors:
                raise HoldProtocolError("unknown actor")
            actor = self._actors[actor_id]
            return {"admission_enabled": actor.admission_enabled and not self._closed and not self._censored(),
                    "in_flight": actor.pending is not None}

    def _record(self, event: str, now: int, *, actor_id: str | None = None,
                tick_index: int | None = None, due_ns: int | None = None,
                status: str, reason: str | None = None,
                bridge_monotonic_ns: int | None = None, **details: Any) -> bool:
        if event not in TRACE_EVENTS:
            raise HoldProtocolError(f"unknown hold trace event: {event}")
        if len(self._events) >= self.trace_capacity:
            self._overflow = True
            self._dropped += 1
            for actor in self._actors.values():
                actor.admission_enabled = False
            return False
        self._counter += 1
        row: dict[str, Any] = {
            "sequence": len(self._events), "linearization_counter": self._counter,
            "event": event, "controller_monotonic_ns": now, "status": status,
        }
        if actor_id is not None:
            row["actor_id"] = actor_id
        if tick_index is not None:
            row["tick_index"] = tick_index
        if due_ns is not None:
            row["due_monotonic_ns"] = due_ns
        if reason is not None:
            row["reason"] = reason
        if bridge_monotonic_ns is not None:
            row["bridge_monotonic_ns_diagnostic"] = bridge_monotonic_ns
        row.update(details)
        self._events.append(row)
        return True

    @staticmethod
    def _request_digest(request: Mapping[str, Any]) -> str:
        encoded = json.dumps(dict(request), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _bridge_time(response: Any) -> int | None:
        if not isinstance(response, SensorResponse):
            return None
        value = response.bridge_monotonic_ns
        return value if type(value) is int and value >= 0 else None

    def _make_request(self, binding: SensorBinding, tick: int) -> Mapping[str, Any]:
        nonce = self._nonce_factory()
        if (not isinstance(nonce, str) or len(nonce) != 32
                or any(ch not in "0123456789abcdef" for ch in nonce) or nonce in self._nonces):
            raise HoldProtocolError("nonce_factory must return a new 16-byte lowercase hex value")
        self._nonces.add(nonce)
        request = {
            "schema": REQUEST_SCHEMA, "run_id": self.run_id, "window_id": self.window_id,
            "actor_id": binding.actor_id, "tick_index": tick, "nonce": nonce,
            "sensor_id": binding.sensor_id, "sensor_digest": binding.sensor_digest,
            "profile_digest": binding.profile_digest, "ingestion_digest": binding.ingestion_digest,
            "geometry_id": binding.geometry_id, "geometry_digest": binding.geometry_digest,
        }
        if not validate_request_payload(request):
            raise HoldProtocolError("passive request contains an invalid key set")
        return MappingProxyType(request)

    def _record_timeout(self, actor: _Actor, pending: _Pending, now: int) -> None:
        if pending.timed_out:
            return
        pending.timed_out = True
        actor.admission_enabled = False
        self._counters["timeouts"] += 1
        self._record("response", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                     due_ns=pending.due_ns, status="timeout", reason="deadline_expired", unresolved=True)
        self._record("qc", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                     due_ns=pending.due_ns, status="blocked", reason="unresolved_timeout",
                     admission_enabled=False)

    def _check_timeouts(self, now: int) -> None:
        for actor in self._actors.values():
            pending = actor.pending
            if pending is not None and pending.request_started and not pending.timed_out and now >= pending.deadline_ns:
                self._record_timeout(actor, pending, now)

    def _close(self, now: int) -> None:
        if self._closed:
            return
        if self._censored():
            self._next_tick = self.tick_count
            self._closed = True
            for actor in self._actors.values():
                actor.admission_enabled = False
            self._record("window_closed", now, status="closed",
                         reason=("run_censored_unverified_sensor_ingest" if self._unverified_failure
                                 else "run_censored_fatal_ingest"),
                         window_close_monotonic_ns=self.window_close_ns)
            return
        while self._next_tick < self.tick_count:
            tick = self._next_tick
            due = self.t0_ns + tick * CADENCE_NS
            if due >= self.window_close_ns:
                break
            for binding in self._bindings:
                self._record("due", now, actor_id=binding.actor_id, tick_index=tick,
                             due_ns=due, status="missed", reason="window_closed_before_dispatch")
                self._record("qc", now, actor_id=binding.actor_id, tick_index=tick,
                             due_ns=due, status="missed", reason="window_closed_before_dispatch")
                self._counters["missed"] += 1
            self._next_tick += 1
        self._closed = True
        for actor in self._actors.values():
            actor.admission_enabled = False
        self._record("window_closed", now, status="closed", window_close_monotonic_ns=self.window_close_ns)

    def poll(self) -> None:
        """Advance due work once; never sleeps, retries, or catches up missed slots."""
        dispatch: list[_Pending] = []
        with self._admission_gate.lock, self._lock:
            now = self._read_clock()
            self._sync_fatal_admission_locked(now)
            if self._censored():
                if now >= self.window_close_ns:
                    self._close(now)
                return
            self._check_timeouts(now)
            if now >= self.window_close_ns:
                self._close(now)
                return
            if self._closed or self._overflow:
                return
            while self._next_tick < self.tick_count:
                tick = self._next_tick
                due = self.t0_ns + tick * CADENCE_NS
                if due > now or due >= self.window_close_ns:
                    break
                self._next_tick += 1
                expired = now >= due + DEADLINE_NS
                for binding in self._bindings:
                    actor = self._actors[binding.actor_id]
                    self._record("due", now, actor_id=binding.actor_id, tick_index=tick,
                                 due_ns=due, status="missed" if expired else "due")
                    if expired:
                        self._counters["missed"] += 1
                        self._record("qc", now, actor_id=binding.actor_id, tick_index=tick,
                                     due_ns=due, status="missed", reason="deadline_elapsed_before_dispatch")
                    elif not actor.admission_enabled:
                        self._record("qc", now, actor_id=binding.actor_id, tick_index=tick,
                                     due_ns=due, status="blocked", reason="admission_disabled",
                                     admission_enabled=False)
                    elif actor.pending is not None:
                        self._counters["overlap_skips"] += 1
                        self._record("qc", now, actor_id=binding.actor_id, tick_index=tick,
                                     due_ns=due, status="blocked", reason="request_in_flight",
                                     admission_enabled=True)
                    else:
                        pending = _Pending(binding.actor_id, tick, due, due + DEADLINE_NS,
                                           self._make_request(binding, tick))
                        actor.pending = pending
                        dispatch.append(pending)
        for pending in dispatch:
            self._dispatch(pending)

    def _dispatch(self, pending: _Pending) -> None:
        with self._admission_gate.lock, self._lock:
            now = self._read_clock()
            self._sync_fatal_admission_locked(now)
            actor = self._actors[pending.actor_id]
            if actor.pending is not pending:
                return
            if self._censored():
                actor.pending = None
                self._record("qc", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                             due_ns=pending.due_ns, status="blocked", reason="run_censored_ingest",
                             admission_enabled=False)
                return
            if self._closed or now >= self.window_close_ns or now >= pending.deadline_ns:
                if now >= self.window_close_ns:
                    self._close(now)
                actor.pending = None
                self._counters["missed"] += 1
                self._record("qc", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                             due_ns=pending.due_ns, status="missed",
                             reason=("window_closed_before_dispatch" if now >= self.window_close_ns
                                     else "deadline_elapsed_before_dispatch"))
                return
            pending.request_started = True
            self._record("request", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                         due_ns=pending.due_ns, status="submitted",
                         deadline_monotonic_ns=pending.deadline_ns,
                         request_digest=self._request_digest(pending.request))

        def receive(result: Any) -> None:
            self._receive(pending, result)

        try:
            self._transport(pending.request, receive)
        except Exception:
            receive(HoldProtocolError("transport_error"))

    def _receive(self, pending: _Pending, response: Any) -> None:
        with self._admission_gate.lock, self._lock:
            now = self._read_clock()
            self._sync_fatal_admission_locked(now)
            actor = self._actors[pending.actor_id]
            if actor.pending is not pending:
                self._counters["late_responses"] += 1
                self._record("response", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                             due_ns=pending.due_ns, status="diagnostic_only", reason="request_no_longer_pending")
                self._record("qc", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                             due_ns=pending.due_ns, status="diagnostic_only", reason="request_no_longer_pending")
                return
            if self._censored():
                if not pending.ingest_started:
                    actor.pending = None
                    self._record("response", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                                 due_ns=pending.due_ns, status="diagnostic_only", reason="run_censored_ingest")
                else:
                    self._record("response", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                                 due_ns=pending.due_ns, status="diagnostic_only", reason="duplicate_callback_after_fatal")
                return
            if pending.response_started:
                self._record("response", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                             due_ns=pending.due_ns, status="diagnostic_only", reason="duplicate_callback")
                self._record("qc", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                             due_ns=pending.due_ns, status="diagnostic_only", reason="duplicate_callback")
                return
            if now >= self.window_close_ns:
                self._close(now)
            if self._closed or now >= pending.deadline_ns or pending.timed_out:
                if now >= pending.deadline_ns:
                    self._record_timeout(actor, pending, now)
                self._counters["late_responses"] += 1
                self._record("response", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                             due_ns=pending.due_ns, status="diagnostic_only",
                             reason="post_window_receive" if self._closed else "late_after_deadline",
                             bridge_monotonic_ns=self._bridge_time(response))
                self._record("qc", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                             due_ns=pending.due_ns, status="diagnostic_only",
                             reason="post_window_receive" if self._closed else "late_after_deadline")
                actor.pending = None
                return
            pending.response_started = True
            self._record("response", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                         due_ns=pending.due_ns, status="received",
                         bridge_monotonic_ns=self._bridge_time(response))
        self._process_response(pending, response)

    def _reject(self, pending: _Pending, *, status: str, reason: str,
                capture_status: str = "captured", bridge_ns: int | None = None) -> None:
        with self._admission_gate.lock, self._lock:
            now = self._read_clock()
            self._sync_fatal_admission_locked(now)
            actor = self._actors[pending.actor_id]
            if actor.pending is not pending:
                return
            if self._censored():
                self._record("qc", now, actor_id=pending.actor_id,
                             tick_index=pending.tick_index, due_ns=pending.due_ns,
                             status="diagnostic_only", reason="run_censored_ingest",
                             admission_enabled=False)
                actor.pending = None
                return
            diagnostic = self._closed or pending.timed_out or now >= pending.deadline_ns
            if now >= pending.deadline_ns and not pending.timed_out:
                self._record_timeout(actor, pending, now)
                diagnostic = True
            event_status = "diagnostic_only" if diagnostic else capture_status
            qc_status = "diagnostic_only" if diagnostic else status
            self._record("capture", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                         due_ns=pending.due_ns, status=event_status,
                         reason=reason if diagnostic else None, bridge_monotonic_ns=bridge_ns)
            self._record("verified", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                         due_ns=pending.due_ns, status="diagnostic_only" if diagnostic else "rejected",
                         reason=reason, bridge_monotonic_ns=bridge_ns)
            self._record("ingest", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                         due_ns=pending.due_ns, status="diagnostic_only" if diagnostic else "skipped",
                         reason=reason)
            self._record("qc", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                         due_ns=pending.due_ns, status=qc_status, reason=reason,
                         admission_enabled=actor.admission_enabled)
            if diagnostic:
                self._counters["late_responses"] += 1
            else:
                self._counters["rejected"] += 1
            actor.pending = None

    @staticmethod
    def _observation_details(receipt: K11CommitReceipt | None) -> dict[str, Any]:
        if receipt is None or receipt.observations is None:
            return {}
        observations = receipt.observations
        return {
            "observations": observations, "capture_seq": receipt.capture_seq,
            "raw_cell_count": len(observations),
            "semantic_noop_count": sum(row["disposition"] == "semantic_noop" for row in observations),
            "transition_count": sum(row["disposition"] == "committed" for row in observations),
            "unknown_count": sum(row["disposition"] == "unknown" for row in observations),
        }

    def _mark_unverified(self, pending: _Pending, now: int, exc: BaseException) -> None:
        """Censor after an admitted callback raised without a zero-change contract."""
        if self._censored():
            return
        self._unverified_failure = {
            "actor_id": pending.actor_id, "tick_index": pending.tick_index,
            "mutation_status": "unknown", "error_type": type(exc).__name__,
        }
        for actor in self._actors.values():
            actor.admission_enabled = False
        self._record("qc", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                     due_ns=pending.due_ns, status="sensor_ingest_unverified_fatal",
                     reason="sensor_ingest_unverified_fatal", mutation_status="unknown",
                     error_type=type(exc).__name__, scientifically_eligible=False, censored=True)

    @staticmethod
    def _same_inventory(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
        return all(left.get(key) == right.get(key)
                   for key in ("actor_id", "tick_index", "capture_seq", "committed_cells",
                               "raw_observations", "raw_cell_count"))

    def _validate_fatal_binding(self, receipt: Mapping[str, Any]) -> None:
        actor, tick = receipt["actor_id"], receipt["tick_index"]
        if (actor not in self._actors or tick >= self.tick_count
                or self.t0_ns + tick * CADENCE_NS >= self.window_close_ns):
            raise HoldProtocolError("fatal receipt actor/tick is outside this schedule")
        if not any(row.get("event") == "ingest" and row.get("status") == "admitted"
                   and row.get("actor_id") == actor and row.get("tick_index") == tick
                   for row in self._events):
            raise HoldProtocolError("fatal receipt lacks a matching ingest reservation")

    def _sync_fatal_admission_locked(self, now: int) -> bool:
        """Import the gate's first fatal receipt before any scheduler admission.

        Callers hold locks in gate -> scheduler order. Gate publishers never
        call into this scheduler while holding their mutation locks.
        """
        receipt = self._admission_gate.fatal_receipt
        if receipt is None:
            return False
        if self._unverified_failure is not None and self._fatal_receipt is None:
            # Keep an earlier unknown-mutation failure primary; the gate still
            # blocks further admission via _censored().
            if now >= self.window_close_ns:
                self._close(now)
            return False
        if self._fatal_receipt is not None and self._same_inventory(receipt, self._fatal_receipt):
            if now >= self.window_close_ns:
                self._close(now)
            return False
        self._validate_fatal_binding(receipt)
        latched = self._latch_fatal(receipt, now)
        if now >= self.window_close_ns:
            self._close(now)
        return latched

    def _latch_fatal(self, receipt: dict[str, Any], now: int,
                     observation_details: Mapping[str, Any] | None = None) -> bool:
        if any(self._same_inventory(receipt, seen) for seen in self._fatal_receipts_seen):
            return False
        if self._fatal_receipt is None:
            self._fatal_receipt = copy.deepcopy(receipt)
            self._fatal_receipts_seen.append(copy.deepcopy(receipt))
            for actor in self._actors.values():
                actor.admission_enabled = False
            capacity = receipt["phase"] == "capacity_exhausted"
            details = dict(observation_details or {})
            details.pop("capture_seq", None)
            if "raw_observations" in receipt:
                details["raw_observations"] = copy.deepcopy(receipt["raw_observations"])
                details["raw_cell_count"] = receipt["raw_cell_count"]
            if capacity:
                details["tick_mutations"] = 0
            self._record("qc", now, actor_id=receipt["actor_id"], tick_index=receipt["tick_index"],
                         due_ns=self.t0_ns + receipt["tick_index"] * CADENCE_NS,
                         status=receipt["status"], reason="capacity_exhausted" if capacity else receipt["status"],
                         fatal_status=receipt["status"], fatal_receipt=copy.deepcopy(receipt),
                         capture_seq=receipt["capture_seq"], committed_cells=copy.deepcopy(receipt["committed_cells"]),
                         failing_cell_index=receipt["failing_cell_index"],
                         failing_coordinate=copy.deepcopy(receipt["failing_coordinate"]),
                         phase=receipt["phase"], orphan_provenance_id=receipt["orphan_provenance_id"],
                         orphan_provenance=receipt["orphan_provenance"],
                         scientifically_eligible=False, censored=True, **details)
            return True
        if len(self._fatal_receipts_seen) >= len(self._actors):
            return False
        self._fatal_receipts_seen.append(copy.deepcopy(receipt))
        details = dict(observation_details or {})
        details.pop("capture_seq", None)
        if "raw_observations" in receipt:
            details["raw_observations"] = copy.deepcopy(receipt["raw_observations"])
            details["raw_cell_count"] = receipt["raw_cell_count"]
        self._record("qc", now, actor_id=receipt["actor_id"], tick_index=receipt["tick_index"],
                     due_ns=self.t0_ns + receipt["tick_index"] * CADENCE_NS,
                     status="diagnostic_only", reason="secondary_fatal_inventory",
                     secondary_fatal_receipt=copy.deepcopy(receipt),
                     capture_seq=receipt["capture_seq"], committed_cells=copy.deepcopy(receipt["committed_cells"]),
                     failing_cell_index=receipt["failing_cell_index"],
                     failing_coordinate=copy.deepcopy(receipt["failing_coordinate"]),
                     phase=receipt["phase"], admission_enabled=False, **details)
        return False

    def report_fatal(self, receipt: Mapping[str, Any]) -> bool:
        """Synchronize a published fatal receipt after adapter locks are released."""
        with self._admission_gate.lock:
            self._admission_gate.publish_fatal(receipt)
            now = self._read_clock()
            with self._lock:
                return self._sync_fatal_admission_locked(now)

    def _process_response(self, pending: _Pending, response: Any) -> None:
        now = self._read_clock()
        if isinstance(response, BaseException):
            self._reject(pending, status="rejected", reason="transport_error", capture_status="unavailable")
            return
        if not isinstance(response, SensorResponse):
            self._reject(pending, status="rejected", reason="invalid_response", capture_status="unavailable")
            return
        bridge_ns = self._bridge_time(response)
        if type(response.capture_ok) is not bool or not response.capture_ok:
            self._reject(pending, status="rejected", reason="capture_failure", capture_status="failed", bridge_ns=bridge_ns)
            return
        if not isinstance(response.payload, bytes):
            self._reject(pending, status="rejected", reason="invalid_payload", bridge_ns=bridge_ns)
            return
        if len(response.payload) > MAX_PAYLOAD_BYTES:
            self._reject(pending, status="rejected", reason="payload_oversize", bridge_ns=bridge_ns)
            return
        if type(response.signature_valid) is not bool or not response.signature_valid:
            self._reject(pending, status="rejected", reason="bad_signature", bridge_ns=bridge_ns)
            return
        with self._admission_gate.lock, self._lock:
            now = self._read_clock()
            self._sync_fatal_admission_locked(now)
            actor = self._actors[pending.actor_id]
            if actor.pending is not pending:
                return
            if self._censored():
                actor.pending = None
                self._record("qc", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                             due_ns=pending.due_ns, status="diagnostic_only", reason="run_censored_ingest")
                return
            if now >= self.window_close_ns:
                self._close(now)
            if self._closed or pending.timed_out or now >= pending.deadline_ns:
                if now >= pending.deadline_ns:
                    self._record_timeout(actor, pending, now)
                actor.pending = None
                self._record("capture", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                             due_ns=pending.due_ns, status="diagnostic_only", reason="late_capture",
                             bridge_monotonic_ns=bridge_ns)
                self._record("verified", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                             due_ns=pending.due_ns, status="diagnostic_only", reason="late_capture",
                             bridge_monotonic_ns=bridge_ns)
                self._record("ingest", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                             due_ns=pending.due_ns, status="diagnostic_only", reason="late_capture")
                self._record("qc", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                             due_ns=pending.due_ns, status="diagnostic_only", reason="late_capture")
                return
            self._record("capture", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                         due_ns=pending.due_ns, status="captured", payload_bytes=len(response.payload),
                         bridge_monotonic_ns=bridge_ns)
            self._record("verified", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                         due_ns=pending.due_ns, status="verified", signature_valid=True,
                         payload_bytes=len(response.payload), bridge_monotonic_ns=bridge_ns)
            pending.ingest_started = True
            self._record("ingest", now, actor_id=pending.actor_id, tick_index=pending.tick_index,
                         due_ns=pending.due_ns, status="admitted")

        observation_details: dict[str, Any] = {}
        receipt: K11CommitReceipt | None = None
        rejected: K11RejectedBeforeMutation | None = None
        fatal: dict[str, Any] | None = None
        unverified: BaseException | None = None
        try:
            result = self._ingest(pending.actor_id, pending.request, response.payload,
                                  response.request_identity)
            if isinstance(result, K11CommitReceipt):
                receipt = result
        except K11FatalSensorIngest as exc:
            fatal = exc.fatal_receipt
        except K11RejectedBeforeMutation as exc:
            rejected = exc
        except BaseException as exc:
            unverified = exc

        finished = self._read_clock()
        with self._admission_gate.lock, self._lock:
            finished = self._read_clock()
            self._sync_fatal_admission_locked(finished)
            actor = self._actors[pending.actor_id]
            if actor.pending is not pending:
                return
            # Initialize this before every callback-result branch; it is safe to
            # expand only after a validated receipt has supplied bounded fields.
            if fatal is not None:
                try:
                    normalized = _normalize_fatal_receipt(fatal)
                    self._validate_fatal_binding(normalized)
                except (TypeError, ValueError, KeyError):
                    self._mark_unverified(pending, finished, HoldProtocolError("invalid_fatal_receipt"))
                else:
                    try:
                        self._admission_gate.publish_fatal(normalized)
                        self._sync_fatal_admission_locked(finished)
                    except (TypeError, ValueError, KeyError):
                        self._mark_unverified(pending, finished, HoldProtocolError("invalid_fatal_receipt"))
                        actor.pending = None
                        return
                    if (self._unverified_failure is not None and self._fatal_receipt is None):
                        # Preserve the earlier unknown-mutation primary marker;
                        # this is a truthful secondary receipt, not a replacement.
                        self._record(
                            "qc", finished, actor_id=normalized["actor_id"],
                            tick_index=normalized["tick_index"],
                            due_ns=self.t0_ns + normalized["tick_index"] * CADENCE_NS,
                            status="diagnostic_only", reason="fatal_receipt_after_unverified_fatal",
                            secondary_fatal_status=normalized["status"],
                            secondary_capture_seq=normalized["capture_seq"],
                            secondary_committed_cells=copy.deepcopy(normalized["committed_cells"]),
                            secondary_phase=normalized["phase"],
                        )
                    elif (self._fatal_receipt is not None
                          and not self._same_inventory(normalized, self._fatal_receipt)):
                        self._validate_fatal_binding(normalized)
                        self._latch_fatal(normalized, finished)
                    elif self._fatal_receipt is None:
                        if finished >= self.window_close_ns:
                            self._close(finished)
                        self._validate_fatal_binding(normalized)
                        self._latch_fatal(normalized, finished)
                actor.pending = None
                return
            if unverified is not None:
                self._mark_unverified(pending, finished, unverified)
                actor.pending = None
                return
            if rejected is not None:
                late = pending.timed_out or finished >= pending.deadline_ns or self._closed
                if finished >= pending.deadline_ns and not pending.timed_out:
                    self._record_timeout(actor, pending, finished)
                    late = True
                self._record(
                    "qc", finished, actor_id=pending.actor_id, tick_index=pending.tick_index,
                    due_ns=pending.due_ns,
                    status="diagnostic_only" if late else "rejected_before_mutation",
                    reason="typed_rejection_after_cutoff" if late else "typed_ingest_rejection",
                    mutation_status="zero",
                )
                self._counters["rejected"] += 1
                actor.pending = None
                return

            if receipt is not None:
                observation_details = self._observation_details(receipt)
                if (receipt.actor_id != pending.actor_id or receipt.tick_index != pending.tick_index):
                    if receipt.committed_cells:
                        try:
                            mismatch = _normalize_fatal_receipt({
                                "status": "partial_commit_fatal", "actor_id": pending.actor_id,
                                "tick_index": pending.tick_index, "capture_seq": receipt.capture_seq,
                                "committed_cells": receipt.committed_cells, "failing_cell_index": None,
                                "failing_coordinate": None, "phase": "commit_receipt_binding",
                            })
                            self._validate_fatal_binding(mismatch)
                            self._latch_fatal(mismatch, finished, observation_details)
                        except (TypeError, ValueError, KeyError):
                            self._mark_unverified(pending, finished, HoldProtocolError("invalid_receipt_binding"))
                    else:
                        self._mark_unverified(pending, finished, HoldProtocolError("invalid_receipt_binding"))
                    actor.pending = None
                    return
                if receipt.committed_cells and self._unverified_failure is not None:
                    details = dict(observation_details)
                    details.setdefault("capture_seq", receipt.capture_seq)
                    details.update({
                        "secondary_commit_status": "committed",
                        "secondary_committed_cells": receipt.committed_cells,
                        "secondary_eac_ingest_sequences": [
                            cell["ingest_sequence"] for cell in receipt.committed_cells
                        ],
                    })
                    self._record(
                        "qc", finished, actor_id=receipt.actor_id,
                        tick_index=receipt.tick_index,
                        due_ns=self.t0_ns + receipt.tick_index * CADENCE_NS,
                        status="diagnostic_only", reason="receipt_after_unverified_fatal",
                        **details,
                    )
                    actor.pending = None
                    return
                if receipt.committed_cells and self._fatal_receipt is not None:
                    secondary = _normalize_fatal_receipt({
                        "status": "partial_commit_fatal", "actor_id": receipt.actor_id,
                        "tick_index": receipt.tick_index, "capture_seq": receipt.capture_seq,
                        "committed_cells": receipt.committed_cells, "failing_cell_index": None,
                        "failing_coordinate": None, "phase": "secondary_commit_after_run_fatal",
                    })
                    self._latch_fatal(secondary, finished, observation_details)
                    actor.pending = None
                    return
                if receipt.committed_cells and (pending.timed_out or finished >= pending.deadline_ns
                                                or finished >= self.window_close_ns or self._closed):
                    late = _normalize_fatal_receipt({
                        "status": "partial_commit_fatal", "actor_id": receipt.actor_id,
                        "tick_index": receipt.tick_index, "capture_seq": receipt.capture_seq,
                        "committed_cells": receipt.committed_cells, "failing_cell_index": None,
                        "failing_coordinate": None, "phase": "ingest_finished_after_cutoff",
                    })
                    if finished >= self.window_close_ns:
                        self._close(finished)
                    self._latch_fatal(late, finished, observation_details)
                    actor.pending = None
                    return

            if self._censored():
                self._record("qc", finished, actor_id=pending.actor_id, tick_index=pending.tick_index,
                             due_ns=pending.due_ns, status="diagnostic_only",
                             reason="ingest_finished_after_fatal", **observation_details)
                actor.pending = None
                return
            if finished >= self.window_close_ns:
                self._close(finished)
            if pending.timed_out or finished >= pending.deadline_ns or self._closed:
                if finished >= pending.deadline_ns:
                    self._record_timeout(actor, pending, finished)
                self._record("qc", finished, actor_id=pending.actor_id, tick_index=pending.tick_index,
                             due_ns=pending.due_ns, status="diagnostic_only",
                             reason="ingest_finished_after_cutoff", **observation_details)
            else:
                details: dict[str, Any] = dict(observation_details)
                committed = receipt.committed_cells if receipt is not None else []
                if committed:
                    details.update({
                        "commit_marker": True, "commit_status": "committed",
                        "commit_actor_id": receipt.actor_id, "commit_tick_index": receipt.tick_index,
                        "capture_seq": receipt.capture_seq, "committed_cells": committed,
                        "eac_ingest_sequences": [cell["ingest_sequence"] for cell in committed],
                    })
                observations = receipt.observations if receipt is not None else None
                if observations:
                    dispositions = {row["disposition"] for row in observations}
                    if "committed" in dispositions:
                        details["evidence_status"] = "committed"
                    elif "semantic_noop" in dispositions:
                        details["evidence_status"] = "semantic_noop"
                    elif "unknown" in dispositions:
                        details["evidence_status"] = "unknown_only"
                elif receipt is not None and receipt.committed_cells:
                    details["evidence_status"] = "committed"
                else:
                    details["evidence_status"] = "unknown_no_evidence"
                self._record("qc", finished, actor_id=pending.actor_id, tick_index=pending.tick_index,
                             due_ns=pending.due_ns, status="accepted",
                             reason="verified_passive_capture", **details)
                self._counters["completed"] += 1
                self._counters["ingested"] += 1
            actor.pending = None

    def trace_artifact(self) -> dict[str, Any]:
        with self._admission_gate.lock, self._lock:
            self._sync_fatal_admission_locked(self._read_clock())
            fatal_status = (
                self._fatal_receipt["status"] if self._fatal_receipt is not None
                else "sensor_ingest_unverified_fatal" if self._unverified_failure is not None else None
            )
            return {
                "schema": TRACE_SCHEMA, "policy": POLICY, "run_id": self.run_id,
                "window_id": self.window_id, "t0_monotonic_ns": self.t0_ns,
                "window_close_monotonic_ns": self.window_close_ns,
                "cadence_ns": CADENCE_NS, "deadline_ns": DEADLINE_NS,
                "actor_ids": list(self.actor_ids), "events": copy.deepcopy(self._events),
                "counters": dict(self._counters), "fatal_status": fatal_status,
                "fatal_reason": (
                    "capacity_exhausted" if self._fatal_receipt is not None
                    and self._fatal_receipt["phase"] == "capacity_exhausted"
                    else fatal_status
                ),
                "fatal_receipt": copy.deepcopy(self._fatal_receipt),
                "unverified_ingest_failure": copy.deepcopy(self._unverified_failure),
                "scientifically_eligible": not self._censored(),
                "censored": self._censored(),
                "trace_retention": {
                    "capacity": self.trace_capacity, "retained": len(self._events),
                    "truncated": self._overflow, "dropped_count": self._dropped,
                },
                "window_closed": self._closed,
            }


__all__ = [
    "CADENCE_NS", "DEADLINE_NS", "FixedPassiveScheduler", "HoldProtocolError",
    "K11SensorAdmissionGate",
    "K11CommitReceipt", "K11FatalSensorIngest", "K11RejectedBeforeMutation",
    "MAX_FATAL_COMMITTED_CELLS", "MAX_PAYLOAD_BYTES", "MAX_TRACE_EVENTS", "POLICY",
    "REQUEST_KEYS", "REQUEST_SCHEMA", "SensorBinding", "SensorResponse", "TRACE_SCHEMA",
    "snapshot_canonical_bytes",
]
