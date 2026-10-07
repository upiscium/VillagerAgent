"""Authenticated actor-private K11 passive captures and sensor-only transport."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import re
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from benchmarks.common.eac import Proposition, PropositionKey
from benchmarks.minecraft.eac_runtime import (
    K11_V2_IMPLEMENTATION_PATHS as K11_IMPLEMENTATION_PATHS,
    K11CapacityExhausted, MinecraftEACError, MinecraftEACRuntime,
    _authenticate_v2_implementation, _validate_k11_provenance,
)
from benchmarks.minecraft.k11_hold_protocol import (
    CADENCE_NS, DEADLINE_NS, K11CommitReceipt, K11FatalSensorIngest,
    K11RejectedBeforeMutation, K11SensorAdmissionGate, SensorResponse,
    snapshot_canonical_bytes as canonical_bytes,
)


REQUEST_SCHEMA = "minecraft-k11-visible-block-snapshot/1"
SENSOR_ID = "minecraft-k11-fixed-passive-sensor/1"
GEOMETRY_ID = "minecraft-k11-360-supercover-5x3x5/1"
GEOMETRY = {
    "id": GEOMETRY_ID,
    "offsets": {"x": [-2, 2], "y": [-1, 1], "z": [-2, 2]},
    "max_steps": 16,
    "eye": "entity.position+eyeHeight",
    "los": "3d-supercover/1",
}
REQUEST_KEYS = frozenset({
    "schema", "run_id", "window_id", "actor_id", "tick_index", "nonce",
    "sensor_id", "sensor_digest", "profile_digest", "ingestion_digest",
    "geometry_id", "geometry_digest", "request_hmac",
})
RESPONSE_BINDING_FIELDS = (
    "run_id", "window_id", "actor_id", "tick_index", "nonce", "sensor_id",
    "sensor_digest", "profile_digest", "ingestion_digest", "geometry_id", "geometry_digest",
)
RESPONSE_KEYS = frozenset({
    *RESPONSE_BINDING_FIELDS, "bridge_id", "capture_seq",
    "capture_started_monotonic_ns", "capture_ended_monotonic_ns", "pose", "eye",
    "cells", "cell_payload_digest", "complete", "truncated", "error",
    "request_digest", "hmac_sha256",
})
EXPECTED_OFFSETS = tuple(
    {"x": x, "y": y, "z": z}
    for x in range(-2, 3) for y in range(-1, 2) for z in range(-2, 3)
)
UNKNOWN_REASONS = frozenset({
    "unloaded_target", "unloaded_path", "occluded", "outside_region", "invalid_pose",
    "step_limit", "incoherent_capture", "unmapped_registry",
})
AIR_NAMES = frozenset({"air", "cave_air", "void_air"})
MAX_PAYLOAD_BYTES = 65_536
MAX_REGISTERED_REQUESTS = 8_192
MAX_QC_DIAGNOSTICS = 128
MAX_COMMIT_LEDGER = MAX_REGISTERED_REQUESTS * len(EXPECTED_OFFSETS)
MAX_OBSERVATION_LEDGER = MAX_REGISTERED_REQUESTS
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_BRIDGE_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_NONCE_RE = re.compile(r"[0-9a-f]{32}\Z")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
_BLOCK_NAME_RE = re.compile(r"[a-z0-9_]+\Z")


class K11HoldEvidenceError(K11RejectedBeforeMutation):
    """A proven pre-mutation validation or admission rejection."""


class _InvalidCapture(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class _PendingCapture:
    actor_id: str
    tick_index: int
    nonce: str
    deadline_ns: int
    close_ns: int


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _valid_identifier(value: Any) -> bool:
    return isinstance(value, str) and _IDENTIFIER_RE.fullmatch(value) is not None


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_value):
    raise ValueError("non-finite JSON number")


def _position(value: Any) -> tuple[int, int, int] | None:
    if (not isinstance(value, dict) or set(value) != {"x", "y", "z"}
            or any(type(value[axis]) is not int or abs(value[axis]) > 2**31 - 1
                   for axis in ("x", "y", "z"))):
        return None
    return value["x"], value["y"], value["z"]


def _registry_proof(value: Any) -> tuple[tuple[int, int, int], int, str] | None:
    if not isinstance(value, dict) or set(value) != {"position", "registry_id", "block_name"}:
        return None
    position = _position(value.get("position"))
    registry_id, block_name = value.get("registry_id"), value.get("block_name")
    if (position is None or type(registry_id) is not int
            or not 0 <= registry_id <= 2**63 - 1
            or not isinstance(block_name, str) or _BLOCK_NAME_RE.fullmatch(block_name) is None):
        return None
    return position, registry_id, block_name


def _float_string(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _supercover_path(origin: tuple[float, float, float],
                     target: tuple[int, int, int]) -> list[tuple[int, int, int]] | None:
    """Reproduce the bridge's ordered, bounded 3-D voxel supercover."""
    current = [math.floor(value) for value in origin]
    start, goal = tuple(current), target
    delta = tuple(target[i] + 0.5 - origin[i] for i in range(3))
    step = [0 if value == 0 else (1 if value > 0 else -1) for value in delta]
    t_delta, t_max = [], []
    for index in range(3):
        if step[index] == 0:
            t_delta.append(math.inf)
            t_max.append(math.inf)
        else:
            t_delta.append(abs(1.0 / delta[index]))
            boundary = current[index] + 1 if step[index] > 0 else current[index]
            t_max.append((boundary - origin[index]) / delta[index])
    path: list[tuple[int, int, int]] = []
    seen: set[tuple[int, int, int]] = set()

    def add_touched(position: list[int]) -> bool:
        touched = tuple(position)
        if touched == start or touched in seen:
            return True
        path.append(touched)
        seen.add(touched)
        return 1 + len(path) <= GEOMETRY["max_steps"]

    boundary_axes = [index for index, value in enumerate(origin) if value.is_integer()]
    for mask in range(1, 1 << len(boundary_axes)):
        touched = list(current)
        for bit, axis in enumerate(boundary_axes):
            if mask & (1 << bit):
                touched[axis] -= 1
        if not add_touched(touched):
            return None
    while tuple(current) != goal:
        next_t = min(t_max)
        tied = [index for index, value in enumerate(t_max) if abs(value - next_t) <= 1e-12]
        if not tied or not math.isfinite(next_t):
            return None
        for mask in range(1, 1 << len(tied)):
            touched = list(current)
            for bit, axis in enumerate(tied):
                if mask & (1 << bit):
                    touched[axis] += step[axis]
            if not add_touched(touched):
                return None
        for axis in tied:
            current[axis] += step[axis]
            t_max[axis] += t_delta[axis]
    return path


class K11HoldEvidenceAdapter:
    """Authenticate complete K11 snapshots before actor-private EAC admission."""

    def __init__(self, *, runtime: MinecraftEACRuntime, run_id: str, window_id: str,
                 actor_secrets: Mapping[str, bytes], bridge_ids: Mapping[str, str] | None = None,
                 clock_ns: Callable[[], int] | None = None) -> None:
        if not isinstance(runtime, MinecraftEACRuntime) or runtime.source_version != 2:
            raise ValueError("K11 passive evidence requires an authenticated v2 runtime")
        if runtime.run_id != run_id:
            raise ValueError("K11 runtime and adapter run IDs differ")
        if not _valid_identifier(run_id) or not _valid_identifier(window_id):
            raise ValueError("invalid K11 run/window identity")
        if (not isinstance(actor_secrets, Mapping) or len(actor_secrets) != 2
                or any(not _valid_identifier(actor) or not isinstance(secret, bytes)
                       or len(secret) < 32 for actor, secret in actor_secrets.items())
                or len(set(actor_secrets.values())) != 2):
            raise ValueError("exactly two actors with distinct 32-byte-or-longer secrets are required")

        profile, ingestion = runtime.profile_document, runtime.ingestion_contract
        if (profile.get("profile_id") != "minecraft-eac-k11-fixed-passive"
                or profile.get("profile_version") != 2
                or ingestion.get("artifact_version") != 2):
            raise ValueError("authenticated K11 v2 artifacts are required")
        profile_digest = profile.get("detached_profile_sha256")
        ingestion_digest = ingestion.get("detached_artifact_sha256")
        if (not isinstance(profile_digest, str) or _DIGEST_RE.fullmatch(profile_digest) is None
                or not isinstance(ingestion_digest, str)
                or _DIGEST_RE.fullmatch(ingestion_digest) is None):
            raise ValueError("authenticated K11 artifact digests are invalid")
        detached_profile, detached_ingestion = dict(profile), dict(ingestion)
        detached_profile.pop("detached_profile_sha256", None)
        detached_ingestion.pop("detached_artifact_sha256", None)
        integrity = profile.get("integrity_contract")
        if (_sha256(canonical_bytes(detached_profile)) != profile_digest
                or _sha256(canonical_bytes(detached_ingestion)) != ingestion_digest
                or runtime.profile_binding.digest_sha256 != profile_digest
                or runtime.profile_binding.profile_id != "minecraft-eac-k11-fixed-passive"
                or runtime.profile_binding.profile_version != 2
                or not isinstance(integrity, Mapping)
                or integrity.get("canonical_content_sha256") != ingestion_digest):
            raise ValueError("authenticated K11 artifact binding changed")
        try:
            manifest = ingestion.get("implementation_manifest")
            if (type(ingestion.get("implementation_manifest_version")) is not int
                    or ingestion.get("implementation_manifest_version") != 1
                    or not isinstance(manifest, Mapping)
                    or set(manifest) != set(K11_IMPLEMENTATION_PATHS)):
                raise MinecraftEACError("Minecraft v2 implementation manifest exact source set mismatch")
            _authenticate_v2_implementation(ingestion)
        except MinecraftEACError as exc:
            raise ValueError(f"K11 implementation manifest authentication failed: {exc}") from exc

        adapter_binding = ingestion.get("trusted_observation_adapter")
        try:
            implementation_digest = _sha256(Path(__file__).resolve().read_bytes())
        except OSError as exc:
            raise ValueError("K11 evidence adapter artifact is unavailable") from exc
        if (not isinstance(adapter_binding, Mapping)
                or adapter_binding.get("implementation_path")
                != "benchmarks/minecraft/k11_hold_evidence.py"
                or adapter_binding.get("implementation_sha256") != implementation_digest):
            raise ValueError("authenticated K11 evidence adapter binding mismatch")
        adapter_identity, adapter_version = (adapter_binding.get("tool_identity"),
                                             adapter_binding.get("tool_version"))
        trusted = [item for item in profile.get("trusted_tools", ())
                   if isinstance(item, Mapping)
                   and item.get("tool_identity") == adapter_identity
                   and item.get("tool_version") == adapter_version]
        if (not adapter_identity or not adapter_version or len(trusted) != 1
                or trusted[0].get("integrity_contract_sha256")
                != _sha256(canonical_bytes(adapter_binding))):
            raise ValueError("authenticated K11 profile does not bind this adapter")

        sensor_path = Path(__file__).resolve().parents[2] / "env" / "k11_visible_block_capture.js"
        try:
            sensor_digest = _sha256(sensor_path.read_bytes())
        except OSError as exc:
            raise ValueError("K11 sensor artifact is unavailable") from exc
        geometry_digest = _sha256(canonical_bytes(GEOMETRY))
        if (adapter_binding.get("sensor_identity") != SENSOR_ID
                or adapter_binding.get("sensor_implementation_path")
                != "env/k11_visible_block_capture.js"
                or adapter_binding.get("sensor_implementation_sha256") != sensor_digest
                or adapter_binding.get("geometry_identity") != GEOMETRY_ID
                or adapter_binding.get("geometry_digest") != geometry_digest
                or adapter_binding.get("bridge_envelope_identity") != REQUEST_SCHEMA
                or adapter_binding.get("accepted_route")
                != "POST /post_k11_visible_block_region_v1"):
            raise ValueError("K11 sensor implementation or geometry contract digest mismatch")

        pinned: dict[str, str] = {}
        if bridge_ids is not None:
            if not isinstance(bridge_ids, Mapping) or not set(bridge_ids).issubset(actor_secrets):
                raise ValueError("prebound K11 bridge IDs must be actor-scoped")
            for actor, bridge_id in bridge_ids.items():
                if not isinstance(bridge_id, str) or _BRIDGE_ID_RE.fullmatch(bridge_id) is None:
                    raise ValueError("invalid prebound K11 bridge ID")
                pinned[actor] = bridge_id
            if len(set(pinned.values())) != len(pinned):
                raise ValueError("each K11 actor must have a distinct bridge ID")

        self.runtime, self.run_id, self.window_id = runtime, run_id, window_id
        self._secrets = dict(actor_secrets)
        self.actor_ids = tuple(self._secrets)
        self.sensor_digest, self.profile_digest = sensor_digest, profile_digest
        self.ingestion_digest, self.geometry_digest = ingestion_digest, geometry_digest
        self.bridge_ids = pinned
        self._clock_ns = clock_ns if clock_ns is not None else time.monotonic_ns
        if not callable(self._clock_ns):
            raise ValueError("clock_ns must be callable")
        self._admission_gate = K11SensorAdmissionGate()
        self._lock = threading.RLock()
        self._pending_by_actor: dict[str, _PendingCapture] = {}
        self._processing_actors: set[str] = set()
        self._registered_ticks: set[tuple[str, int]] = set()
        self._registered_nonces: set[tuple[str, str]] = set()
        self._last_registered_tick: dict[str, int] = {}
        self._last_capture_seq: dict[str, int] = {}
        self._closed = False
        self._qc: deque[dict[str, Any]] = deque(maxlen=MAX_QC_DIAGNOSTICS)
        self._commit_ledger: list[dict[str, Any]] = []
        self._observation_ledger: list[dict[str, Any]] = []
        self._raw_observation_ledger: list[dict[str, Any]] = []
        self._fatal_receipts: list[dict[str, Any]] = []
        self._fatal_notifier: Callable[[Mapping[str, Any]], Any] | None = None
        self._admission_ready = True
        self._verification_token = runtime._bind_k11_verifier(self)

    @property
    def qc_diagnostics(self) -> tuple[dict[str, Any], ...]:
        with self._admission_gate.lock, self._lock:
            return tuple(dict(item) for item in self._qc)

    @property
    def closed(self) -> bool:
        with self._admission_gate.lock, self._lock:
            return self._closed or self._admission_gate.fatal_receipt is not None

    @property
    def scientifically_eligible(self) -> bool:
        with self._admission_gate.lock, self._lock:
            return (not self._fatal_receipts
                    and self._admission_gate.fatal_receipt is None)

    @property
    def commit_ledger(self) -> tuple[dict[str, Any], ...]:
        with self._admission_gate.lock, self._lock:
            return tuple(copy.deepcopy(self._commit_ledger))

    @property
    def observation_ledger(self) -> tuple[dict[str, Any], ...]:
        with self._admission_gate.lock, self._lock:
            return tuple(copy.deepcopy(self._observation_ledger))

    @property
    def raw_observation_ledger(self) -> tuple[dict[str, Any], ...]:
        """Bounded verified-cell accounts, independent of semantic commits."""
        with self._admission_gate.lock, self._lock:
            return tuple(copy.deepcopy(self._raw_observation_ledger))

    @property
    def fatal_receipts(self) -> tuple[dict[str, Any], ...]:
        with self._admission_gate.lock, self._lock:
            local = tuple(copy.deepcopy(self._fatal_receipts))
            if local:
                return local
            shared = self._admission_gate.fatal_receipt
            return () if shared is None else (shared,)

    @property
    def admission_gate(self) -> K11SensorAdmissionGate:
        return self._admission_gate

    def bind_admission_gate(self, gate: K11SensorAdmissionGate) -> None:
        """Use the scheduler's shared fatal/admission gate before the first request."""
        if not isinstance(gate, K11SensorAdmissionGate):
            raise TypeError("K11 admission gate must be a K11SensorAdmissionGate")
        with gate.lock, self._lock:
            if gate is self._admission_gate:
                return
            if (self._registered_ticks or self._pending_by_actor or self._processing_actors
                    or self._fatal_notifier is not None or self._fatal_receipts):
                raise ValueError("K11 admission gate must be bound before the first request")
            if gate.fatal_receipt is not None:
                raise ValueError("cannot bind an already-fatal K11 admission gate")
            self._admission_gate = gate

    def bind_fatal_notifier(self, callback: Callable[[Mapping[str, Any]], Any]) -> None:
        if not callable(callback):
            raise TypeError("K11 fatal notifier must be callable")
        with self._admission_gate.lock, self._lock:
            if self._fatal_notifier is not None:
                raise ValueError("K11 fatal notifier is already bound")
            if (self._registered_ticks or self._pending_by_actor or self._processing_actors):
                raise ValueError("K11 fatal notifier must be bound before the first tick")
            self._fatal_notifier = callback

    def _commit_entry(self, pending: _PendingCapture, capture_seq: int,
                      cell_index: int, position: tuple[int, int, int], root,
                      *, expected_polarity: bool, ingest_sequence: int | None = None):
        prefix = f"minecraft-k11-passive-root:{self.run_id}:"
        if (not isinstance(root.root_id, str) or not root.root_id.startswith(prefix)
                or root.proposition.key.arguments != position
                or root.proposition.polarity is not expected_polarity
                or root.visible_to != (pending.actor_id,)):
            raise ValueError("K11 committed root does not match its observed cell")
        sequence = int(root.root_id[len(prefix):])
        if ingest_sequence is not None and sequence != ingest_sequence:
            raise ValueError("K11 committed root sequence mismatch")
        return {
            "actor_id": pending.actor_id, "tick_index": pending.tick_index,
            "capture_seq": capture_seq, "cell_index": cell_index,
            "coordinate": list(position), "root_id": root.root_id,
            "provenance_id": root.provenance_id,
            "polarity": root.proposition.polarity,
            "supersedes": list(root.supersedes), "ingest_sequence": sequence,
        }

    def _record_commit(self, pending, capture_seq, cell_index, position, root,
                       *, expected_polarity, ingest_sequence=None):
        entry = self._commit_entry(pending, capture_seq, cell_index, position, root,
                                   expected_polarity=expected_polarity,
                                   ingest_sequence=ingest_sequence)
        if len(self._commit_ledger) >= MAX_COMMIT_LEDGER:
            raise ValueError("K11 commit ledger exhausted")
        self._commit_ledger.append(entry)
        return entry

    def _reconcile_committed_root(self, pending, capture_seq, cell_index, position, root,
                                  *, expected_polarity, ingest_sequence=None):
        entry = self._commit_entry(pending, capture_seq, cell_index, position, root,
                                   expected_polarity=expected_polarity,
                                   ingest_sequence=ingest_sequence)
        if (self.runtime.authority._roots.get(root.root_id) is not root
                or root.provenance_id not in self.runtime.authority._provenance):
            raise ValueError("K11 failed commit has no authenticated root/provenance")
        already = [item for item in self._commit_ledger
                   if (item["actor_id"], item["tick_index"], item["cell_index"])
                   == (pending.actor_id, pending.tick_index, cell_index)]
        if already:
            if len(already) != 1 or already[0] != entry:
                raise ValueError("K11 failed commit ledger conflicts with Authority")
        else:
            if len(self._commit_ledger) >= MAX_COMMIT_LEDGER:
                raise ValueError("K11 failed commit ledger is full")
            self._commit_ledger.append(entry)
        return entry

    def _fatal_receipt(self, pending, capture_seq, committed, cell_index, position, phase,
                       orphan_provenance_id=None, raw_observations=None):
        receipt = {
            "status": "partial_commit_fatal" if committed else "sensor_ingest_fatal",
            "actor_id": pending.actor_id, "tick_index": pending.tick_index,
            "capture_seq": capture_seq, "committed_cells": copy.deepcopy(committed),
            "failing_cell_index": cell_index,
            "failing_coordinate": list(position) if position is not None else None,
            "phase": phase, "orphan_provenance_id": orphan_provenance_id,
            "orphan_provenance": orphan_provenance_id is not None,
        }
        if raw_observations is not None:
            receipt["raw_cell_count"] = len(raw_observations)
            receipt["raw_observations"] = copy.deepcopy(raw_observations)
        return receipt

    def _latch_fatal_locked(self, pending, capture_seq, committed, cell_index, position,
                            phase, orphan_provenance_id=None, raw_observations=None):
        """Publish fatal under gate -> adapter -> runtime -> Authority locks."""
        shared = self._admission_gate.fatal_receipt
        if shared is not None:
            error = K11FatalSensorIngest(shared)
            self._closed = True
            if not self._fatal_receipts:
                self._fatal_receipts.append(copy.deepcopy(error.fatal_receipt))
            return error, None
        receipt = self._fatal_receipt(
            pending, capture_seq, committed, cell_index, position, phase,
            orphan_provenance_id, raw_observations,
        )
        error = K11FatalSensorIngest(receipt)
        self._admission_gate.publish_fatal(error.fatal_receipt)
        self._closed = True
        self._fatal_receipts.append(copy.deepcopy(error.fatal_receipt))
        notifier = self._fatal_notifier
        self._record_qc("fatal", receipt["status"], actor_id=pending.actor_id,
                        tick_index=pending.tick_index)
        return error, notifier

    def _notify_fatal(self, error, notifier, pending):
        """Run scheduler callbacks only after every mutation lock has been released."""
        if notifier is not None:
            try:
                notifier(error.fatal_receipt)
            except Exception:
                self._record_qc("fatal", "fatal_notifier_failed", actor_id=pending.actor_id,
                                tick_index=pending.tick_index)

    def _record_qc(self, status, reason, *, actor_id=None, tick_index=None):
        # Never retain credentials, nonce values, payload, coordinates, names, or digests.
        item = {"event": "qc", "status": status, "reason": reason}
        if isinstance(actor_id, str) and actor_id in self._secrets:
            item["actor_id"] = actor_id
        if type(tick_index) is int and tick_index >= 0:
            item["tick_index"] = tick_index
        with self._admission_gate.lock, self._lock:
            self._qc.append(item)

    @staticmethod
    def _raw_observations(response: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Project exactly the verified 75 cells without semantic dispositions or IDs."""
        observations: list[dict[str, Any]] = []
        for index, cell in enumerate(response["cells"]):
            coordinate = _position(cell["position"])
            observation: dict[str, Any] = {
                "cell_index": index, "coordinate": list(coordinate), "state": cell["state"],
            }
            if cell["state"] == "unknown":
                observation["unknown_reason"] = cell["unknown_reason"]
            else:
                observation["block_name"] = cell["block_name"]
                observation["registry_id"] = cell["registry_id"]
            observations.append(observation)
        return observations

    def _record_raw_observation_capture(self, pending, capture_seq, observations) -> None:
        if len(observations) != len(EXPECTED_OFFSETS):
            raise ValueError("verified K11 raw observation account must contain 75 cells")
        entry = {
            "actor_id": pending.actor_id, "tick_index": pending.tick_index,
            "capture_seq": capture_seq, "raw_cell_count": len(observations),
            "raw_observations": copy.deepcopy(observations),
        }
        with self._admission_gate.lock, self._lock:
            if len(self._raw_observation_ledger) >= MAX_OBSERVATION_LEDGER:
                raise RuntimeError("K11 raw observation ledger exhausted")
            self._raw_observation_ledger.append(entry)

    def _error(self, reason, *, actor_id=None, tick_index=None, status="rejected"):
        self._record_qc(status, reason, actor_id=actor_id, tick_index=tick_index)
        return K11HoldEvidenceError(f"K11 passive capture {reason}")

    def _clock(self) -> int:
        value = self._clock_ns()
        if type(value) is not int or value < 0:
            raise ValueError("controller monotonic clock must return a non-negative integer")
        return value

    def register_pending(self, actor_id, tick_index, nonce, deadline_ns, close_ns) -> None:
        if not isinstance(actor_id, str) or actor_id not in self._secrets:
            raise self._error("unknown_actor")
        if type(tick_index) is not int or tick_index < 0:
            raise self._error("invalid_tick")
        if not isinstance(nonce, str) or _NONCE_RE.fullmatch(nonce) is None:
            raise self._error("invalid_nonce", actor_id=actor_id, tick_index=tick_index)
        if (type(deadline_ns) is not int or deadline_ns < 0
                or type(close_ns) is not int or close_ns < 0):
            raise self._error("invalid_cutoff", actor_id=actor_id, tick_index=tick_index)
        try:
            now = self._clock()
        except (TypeError, ValueError):
            raise self._error("invalid_clock", actor_id=actor_id, tick_index=tick_index) from None
        pair_tick, pair_nonce = (actor_id, tick_index), (actor_id, nonce)
        with self._admission_gate.lock, self._lock:
            if (self._fatal_receipts
                    or self._admission_gate.fatal_receipt is not None):
                raise self._error("fatal_sensor_ingest", actor_id=actor_id,
                                  tick_index=tick_index, status="fatal")
            if self._closed or now >= close_ns:
                raise self._error("closed_window", actor_id=actor_id,
                                  tick_index=tick_index, status="diagnostic_only")
            if now >= deadline_ns:
                raise self._error("expired_deadline", actor_id=actor_id,
                                  tick_index=tick_index, status="diagnostic_only")
            if (len(self._registered_ticks) >= MAX_REGISTERED_REQUESTS
                    or pair_tick in self._registered_ticks or pair_nonce in self._registered_nonces):
                raise self._error("replayed_request", actor_id=actor_id, tick_index=tick_index)
            previous_tick = self._last_registered_tick.get(actor_id)
            if previous_tick is not None and tick_index <= previous_tick:
                raise self._error("out_of_order_tick", actor_id=actor_id, tick_index=tick_index)
            if actor_id in self._pending_by_actor or actor_id in self._processing_actors:
                raise self._error("request_in_flight", actor_id=actor_id, tick_index=tick_index)
            self._registered_ticks.add(pair_tick)
            self._registered_nonces.add(pair_nonce)
            self._last_registered_tick[actor_id] = tick_index
            self._pending_by_actor[actor_id] = _PendingCapture(
                actor_id, tick_index, nonce, deadline_ns, close_ns)
        self._record_qc("registered", "pending_request", actor_id=actor_id,
                        tick_index=tick_index)

    def discard_pending(self, actor_id, tick_index, nonce) -> bool:
        if not isinstance(actor_id, str) or type(tick_index) is not int or not isinstance(nonce, str):
            return False
        with self._admission_gate.lock, self._lock:
            pending = self._pending_by_actor.get(actor_id)
            if pending is None or pending.tick_index != tick_index or pending.nonce != nonce:
                return False
            del self._pending_by_actor[actor_id]
        self._record_qc("discarded", "capture_failed", actor_id=actor_id, tick_index=tick_index)
        return True

    def close(self) -> None:
        with self._admission_gate.lock, self._lock:
            self._closed = True

    def _claim_pending(self, actor_id, request, tick_hint):
        if not isinstance(actor_id, str) or actor_id not in self._secrets:
            raise self._error("unknown_nonce", tick_index=tick_hint)
        nonce = request.get("nonce") if isinstance(request, Mapping) else None
        with self._admission_gate.lock, self._lock:
            if (self._fatal_receipts
                    or self._admission_gate.fatal_receipt is not None):
                raise self._error("fatal_sensor_ingest", actor_id=actor_id,
                                  tick_index=tick_hint, status="fatal")
            pending = self._pending_by_actor.get(actor_id)
            if pending is None or tick_hint != pending.tick_index or nonce != pending.nonce:
                raise self._error("unknown_nonce", actor_id=actor_id, tick_index=tick_hint)
            del self._pending_by_actor[actor_id]
            self._processing_actors.add(actor_id)
            return pending

    def _check_cutoff(self, pending):
        try:
            now = self._clock()
        except (TypeError, ValueError):
            raise self._error("invalid_clock", actor_id=pending.actor_id,
                              tick_index=pending.tick_index) from None
        with self._admission_gate.lock, self._lock:
            if (self._fatal_receipts
                    or self._admission_gate.fatal_receipt is not None):
                raise self._error("fatal_sensor_ingest", actor_id=pending.actor_id,
                                  tick_index=pending.tick_index, status="fatal")
            closed = self._closed
        if closed or now >= pending.close_ns:
            raise self._error("closed_window", actor_id=pending.actor_id,
                              tick_index=pending.tick_index, status="diagnostic_only")
        if now >= pending.deadline_ns:
            raise self._error("expired_deadline", actor_id=pending.actor_id,
                              tick_index=pending.tick_index, status="diagnostic_only")

    def _validate_request(self, actor_id, request, pending):
        if not isinstance(request, Mapping):
            raise _InvalidCapture("invalid_request")
        supplied = dict(request)
        if set(supplied) == REQUEST_KEYS - {"request_hmac"}:
            supplied_hmac = None
        elif set(supplied) == REQUEST_KEYS:
            supplied_hmac = supplied.pop("request_hmac")
        else:
            raise _InvalidCapture("invalid_request")
        expected = {
            "schema": REQUEST_SCHEMA, "run_id": self.run_id, "window_id": self.window_id,
            "actor_id": actor_id, "tick_index": pending.tick_index, "nonce": pending.nonce,
            "sensor_id": SENSOR_ID, "sensor_digest": self.sensor_digest,
            "profile_digest": self.profile_digest, "ingestion_digest": self.ingestion_digest,
            "geometry_id": GEOMETRY_ID, "geometry_digest": self.geometry_digest,
        }
        if any(supplied.get(key) != value for key, value in expected.items()):
            raise _InvalidCapture("request_binding_mismatch")
        if (type(supplied.get("tick_index")) is not int or supplied["tick_index"] < 0
                or not isinstance(supplied.get("nonce"), str)
                or _NONCE_RE.fullmatch(supplied["nonce"]) is None):
            raise _InvalidCapture("invalid_request")
        for field in ("run_id", "window_id", "actor_id", "sensor_id", "geometry_id"):
            if not _valid_identifier(supplied.get(field)):
                raise _InvalidCapture("invalid_request")
        for field in ("sensor_digest", "profile_digest", "ingestion_digest", "geometry_digest"):
            if not isinstance(supplied.get(field), str) or _DIGEST_RE.fullmatch(supplied[field]) is None:
                raise _InvalidCapture("invalid_request")
        try:
            expected_hmac = hmac.new(self._secrets[actor_id], canonical_bytes(supplied),
                                     hashlib.sha256).hexdigest()
        except (TypeError, ValueError):
            raise _InvalidCapture("invalid_request") from None
        if supplied_hmac is not None and (
                not isinstance(supplied_hmac, str)
                or not hmac.compare_digest(supplied_hmac, expected_hmac)):
            raise _InvalidCapture("request_authentication_failed")
        supplied["request_hmac"] = expected_hmac
        return supplied

    @staticmethod
    def _decode_response(payload_bytes):
        if not isinstance(payload_bytes, bytes):
            raise _InvalidCapture("invalid_payload")
        if len(payload_bytes) > MAX_PAYLOAD_BYTES:
            raise _InvalidCapture("payload_oversize")
        try:
            response = json.loads(payload_bytes.decode("utf-8"),
                                  object_pairs_hook=_no_duplicate_keys,
                                  parse_constant=_reject_json_constant)
        except (TypeError, ValueError, UnicodeError):
            raise _InvalidCapture("invalid_json") from None
        if not isinstance(response, dict) or set(response) != RESPONSE_KEYS:
            raise _InvalidCapture("invalid_response_shape")
        return response

    def _validate_response(self, response, request, secret):
        for field in RESPONSE_BINDING_FIELDS:
            if response.get(field) != request.get(field):
                raise _InvalidCapture("response_binding_mismatch")
        bridge_id, capture_seq = response.get("bridge_id"), response.get("capture_seq")
        if not isinstance(bridge_id, str) or _BRIDGE_ID_RE.fullmatch(bridge_id) is None:
            raise _InvalidCapture("invalid_bridge_identity")
        if type(capture_seq) is not int or not 1 <= capture_seq <= 2**63 - 1:
            raise _InvalidCapture("invalid_capture_sequence")
        started, ended = response.get("capture_started_monotonic_ns"), response.get("capture_ended_monotonic_ns")
        if (not isinstance(started, str) or re.fullmatch(r"[0-9]+", started) is None
                or not isinstance(ended, str) or re.fullmatch(r"[0-9]+", ended) is None
                or int(ended) < int(started)):
            raise _InvalidCapture("invalid_capture_time")
        for field, keys in (("pose", {"x", "y", "z"}),
                            ("eye", {"x", "y", "z", "eye_height"})):
            value = response.get(field)
            if not isinstance(value, dict) or set(value) != keys or any(
                    not isinstance(value[axis], str) for axis in keys):
                raise _InvalidCapture("invalid_pose")
        pose = tuple(_float_string(response["pose"][axis]) for axis in ("x", "y", "z"))
        eye = tuple(_float_string(response["eye"][axis]) for axis in ("x", "y", "z"))
        eye_height = _float_string(response["eye"]["eye_height"])
        if any(value is None for value in (*pose, *eye)) or eye_height is None or eye_height <= 0:
            raise _InvalidCapture("invalid_pose")
        px, py, pz = pose
        ex, ey, ez = eye
        if ex != px or ez != pz or ey != py + eye_height:
            raise _InvalidCapture("invalid_pose")
        foot, eye_voxel = (math.floor(px), math.floor(py), math.floor(pz)), (
            math.floor(ex), math.floor(ey), math.floor(ez))
        if (response.get("complete") is not True or response.get("truncated") is not False
                or response.get("error") is not None):
            raise _InvalidCapture("incomplete_capture")
        cells = response.get("cells")
        if not isinstance(cells, list) or len(cells) != 75:
            raise _InvalidCapture("invalid_geometry")
        try:
            if response.get("cell_payload_digest") != _sha256(canonical_bytes(cells)):
                raise _InvalidCapture("cell_digest_mismatch")
        except (TypeError, ValueError):
            raise _InvalidCapture("invalid_geometry") from None
        unsigned = {key: value for key, value in response.items() if key != "hmac_sha256"}
        try:
            expected_signature = hmac.new(secret, canonical_bytes(unsigned), hashlib.sha256).hexdigest()
        except (TypeError, ValueError):
            raise _InvalidCapture("invalid_response") from None
        signature = response.get("hmac_sha256")
        if (not isinstance(signature, str) or _DIGEST_RE.fullmatch(signature) is None
                or not hmac.compare_digest(signature, expected_signature)):
            raise _InvalidCapture("response_authentication_failed")
        if response.get("request_digest") != _sha256(canonical_bytes(request)):
            raise _InvalidCapture("request_digest_mismatch")

        known, id_to_name, name_to_id, first_eye = [], {}, {}, None
        def register(identity):
            _coord, registry_id, block_name = identity
            old_name, old_id = id_to_name.setdefault(registry_id, block_name), name_to_id.setdefault(block_name, registry_id)
            if old_name != block_name or old_id != registry_id:
                raise _InvalidCapture("registry_mapping_mismatch")

        for index, (cell, offset) in enumerate(zip(cells, EXPECTED_OFFSETS)):
            if not isinstance(cell, dict) or _position(cell.get("offset")) != (
                    offset["x"], offset["y"], offset["z"]):
                raise _InvalidCapture("invalid_geometry")
            pos = tuple(foot[i] + offset[axis] for i, axis in enumerate(("x", "y", "z")))
            if _position(cell.get("position")) != pos:
                raise _InvalidCapture("invalid_geometry")
            state = cell.get("state")
            if state == "unknown":
                if (set(cell) != {"offset", "position", "state", "unknown_reason"}
                        or not isinstance(cell.get("unknown_reason"), str)
                        or cell["unknown_reason"] not in UNKNOWN_REASONS):
                    raise _InvalidCapture("invalid_unknown_cell")
                continue
            if state not in {"known_air", "known_non_air"}:
                raise _InvalidCapture("invalid_cell_state")
            if set(cell) != {"offset", "position", "state", "registry_id", "block_name", "coverage"}:
                raise _InvalidCapture("invalid_known_cell")
            registry_id, block_name = cell.get("registry_id"), cell.get("block_name")
            if (type(registry_id) is not int or not 0 <= registry_id <= 2**63 - 1
                    or not isinstance(block_name, str)
                    or _BLOCK_NAME_RE.fullmatch(block_name) is None):
                raise _InvalidCapture("invalid_registry_identity")
            is_air = block_name in AIR_NAMES
            if (state == "known_air") != is_air:
                raise _InvalidCapture("cell_air_classification_mismatch")
            if pos == eye_voxel:
                raise _InvalidCapture("known_eye_voxel")
            coverage = cell.get("coverage")
            if (not isinstance(coverage, dict)
                    or set(coverage) != {"eye_loaded", "target_loaded", "path"}
                    or not isinstance(coverage.get("path"), list)):
                raise _InvalidCapture("invalid_coverage")
            eye_proof = _registry_proof(coverage.get("eye_loaded"))
            target_proof = _registry_proof(coverage.get("target_loaded"))
            if (eye_proof is None or eye_proof[0] != eye_voxel
                    or target_proof is None or target_proof[0] != pos
                    or target_proof[1:] != (registry_id, block_name)):
                raise _InvalidCapture("invalid_coverage_identity")
            identity = (eye_proof[1], eye_proof[2])
            if eye_proof[2] not in AIR_NAMES:
                raise _InvalidCapture("invalid_eye_coverage")
            if first_eye is None:
                first_eye = identity
            elif identity != first_eye:
                raise _InvalidCapture("eye_registry_mismatch")
            register(eye_proof)
            register(target_proof)
            path = _supercover_path((ex, ey, ez), pos)
            if path is None:
                raise _InvalidCapture("invalid_supercover_path")
            expected_path = [point for point in path if point not in {eye_voxel, pos}]
            proofs = coverage["path"]
            if len(proofs) != len(expected_path):
                raise _InvalidCapture("supercover_path_mismatch")
            for proof, expected in zip(proofs, expected_path):
                path_proof = _registry_proof(proof)
                if (path_proof is None or path_proof[0] != expected
                        or path_proof[2] not in AIR_NAMES):
                    raise _InvalidCapture("invalid_path_proof")
                register(path_proof)
            known.append((index, cell, pos))
        return known

    def ingest_callback(self, actor_id, request, payload_bytes, request_identity=None):
        del request_identity
        tick_hint = request.get("tick_index") if isinstance(request, Mapping) else None
        tick_hint = tick_hint if type(tick_hint) is int and tick_hint >= 0 else None
        pending = self._claim_pending(actor_id, request, tick_hint)
        validated = False
        response = None
        capture_seq = None
        raw_observations = None
        try:
            self._check_cutoff(pending)
            signed_request = self._validate_request(actor_id, request, pending)
            response = self._decode_response(payload_bytes)
            known_cells = self._validate_response(response, signed_request, self._secrets[actor_id])
            capture_seq = response["capture_seq"]
            validated = True
            raw_observations = self._raw_observations(response)
            self._record_raw_observation_capture(pending, capture_seq, raw_observations)
            self._check_cutoff(pending)
            bridge_id = response["bridge_id"]
            response_digest = _sha256(canonical_bytes(response))
            prepared = []
            for index, cell, position in known_cells:
                proposition = Proposition(PropositionKey(
                    "minecraft", "target_block_present", position, "current"),
                    polarity=(cell["state"] == "known_non_air"))
                metadata = {
                    "bridge_id": bridge_id, "capture_seq": capture_seq,
                    "request_digest": response["request_digest"],
                    "response_digest": response_digest,
                    "cell_payload_digest": response["cell_payload_digest"],
                    "cell_state": cell["state"], "block_name": cell["block_name"],
                    "registry_id": cell["registry_id"],
                    "coverage_sha256": _sha256(canonical_bytes(cell["coverage"])),
                    "sensor_id": SENSOR_ID, "sensor_digest": self.sensor_digest,
                    "profile_digest": self.profile_digest,
                    "ingestion_digest": self.ingestion_digest,
                    "geometry_id": GEOMETRY_ID, "geometry_digest": self.geometry_digest,
                }
                try:
                    _validate_k11_provenance(metadata, revision=capture_seq,
                                             profile_digest=self.profile_digest,
                                             ingestion_digest=self.ingestion_digest)
                except (MinecraftEACError, ValueError, TypeError):
                    raise _InvalidCapture("invalid_provenance") from None
                prepared.append((index, position, proposition, metadata))
            self._check_cutoff(pending)

            committed, roots, transitions_by_index = [], [], {}
            fatal = deferred = capacity_fatal = None
            with (self._admission_gate.lock, self._lock,
                  self.runtime._lock, self.runtime.authority._lock):
                try:
                    self._check_cutoff(pending)
                    pinned = self.bridge_ids.get(actor_id)
                    if pinned is not None and pinned != bridge_id:
                        raise _InvalidCapture("bridge_identity_mismatch")
                    if any(other != actor_id and value == bridge_id
                           for other, value in self.bridge_ids.items()):
                        raise _InvalidCapture("bridge_identity_mismatch")
                    if capture_seq <= self._last_capture_seq.get(actor_id, 0):
                        raise _InvalidCapture("capture_sequence_replay")
                    if len(self._commit_ledger) + len(known_cells) > MAX_COMMIT_LEDGER:
                        capacity_fatal = self._latch_fatal_locked(
                            pending, capture_seq, [], None, None, "commit_ledger_full",
                            raw_observations=raw_observations)
                    elif len(self._observation_ledger) >= MAX_OBSERVATION_LEDGER:
                        capacity_fatal = self._latch_fatal_locked(
                            pending, capture_seq, [], None, None, "observation_ledger_full",
                            raw_observations=raw_observations)

                    if capacity_fatal is None:
                        try:
                            flags = self.runtime._preflight_k11_observations(
                                actor_id, (item[2] for item in prepared), self._verification_token)
                            if (len(flags) != len(prepared)
                                    or any(type(flag) is not bool for flag in flags)):
                                raise RuntimeError("K11 runtime preflight returned invalid transition flags")
                        except K11CapacityExhausted:
                            capacity_fatal = self._latch_fatal_locked(
                                pending, capture_seq, [], None, None, "capacity_exhausted",
                                raw_observations=raw_observations)
                        except K11RejectedBeforeMutation as exc:
                            deferred = exc
                        except BaseException:
                            fatal = self._latch_fatal_locked(
                                pending, capture_seq, [], None, None, "runtime_preflight",
                                raw_observations=raw_observations)

                    if capacity_fatal is None and fatal is None and deferred is None:
                        self._check_cutoff(pending)
                        self.bridge_ids.setdefault(actor_id, bridge_id)
                        self._last_capture_seq[actor_id] = capture_seq
                        for (cell_index, position, proposition, metadata), transition in zip(prepared, flags):
                            phase, root, failure = "deadline_or_window_close", None, None
                            try:
                                self._check_cutoff(pending)
                                phase = "runtime_commit"
                                self.runtime._k11_last_failure = None
                                root = self.runtime._commit_authenticated_k11_observation(
                                    actor_id=actor_id, proposition=proposition, revision=capture_seq,
                                    provenance=metadata, verification_token=self._verification_token)
                                if root is None:
                                    if transition:
                                        raise RuntimeError("preflight transition was not committed")
                                    transitions_by_index[cell_index] = {
                                        "disposition": "semantic_noop"}
                                    continue
                                phase = "commit_ledger"
                                entry = self._record_commit(
                                    pending, capture_seq, cell_index, position, root,
                                    expected_polarity=proposition.polarity)
                                committed.append(entry)
                                phase = "runtime_bookkeeping"
                                roots.append(root)
                                transitions_by_index[cell_index] = {
                                    "disposition": "committed", "root_id": root.root_id}
                                if not transition:
                                    raise RuntimeError("preflight semantic no-op created a root")
                            except BaseException as exc:
                                if (phase == "deadline_or_window_close" and not committed
                                        and isinstance(exc, K11RejectedBeforeMutation)):
                                    deferred = exc
                                    break
                                failure = self.runtime._k11_last_failure
                                failure = dict(failure) if isinstance(failure, Mapping) else None
                                phase = (failure.get("phase", phase)
                                         if failure and failure.get("actor_id") == actor_id else phase)
                                orphan_id = (failure.get("provenance_id")
                                             if failure and failure.get("actor_id") == actor_id
                                             and failure.get("orphan_provenance") is True else None)
                                committed_root = (failure.get("_root") if failure
                                                  and failure.get("actor_id") == actor_id
                                                  and failure.get("phase") in {
                                                      "ingest_record", "runtime_bookkeeping",
                                                      "persist_audit"}
                                                  and failure.get("root_present") is True else root)
                                if committed_root is not None:
                                    try:
                                        entry = self._reconcile_committed_root(
                                            pending, capture_seq, cell_index, position,
                                            committed_root,
                                            expected_polarity=proposition.polarity,
                                            ingest_sequence=(failure.get("ingest_sequence")
                                                             if failure else None))
                                        if not any(item["cell_index"] == cell_index
                                                   for item in committed):
                                            committed.append(entry)
                                    except (AttributeError, TypeError, ValueError):
                                        phase = "committed_root_identity_unverified"
                                fatal = self._latch_fatal_locked(
                                    pending, capture_seq, committed, cell_index, position,
                                    phase, orphan_id, raw_observations)
                                break

                    if fatal is None and capacity_fatal is None and deferred is None:
                        try:
                            self._check_cutoff(pending)
                        except K11RejectedBeforeMutation as exc:
                            if committed:
                                fatal = self._latch_fatal_locked(
                                    pending, capture_seq, committed, None, None,
                                    "ingest_finished_after_cutoff",
                                    raw_observations=raw_observations)
                            else:
                                deferred = exc
                        if fatal is None and deferred is None:
                            try:
                                observations = []
                                for index, cell in enumerate(response["cells"]):
                                    position = _position(cell["position"])
                                    if cell["state"] == "unknown":
                                        observations.append({
                                            "cell_index": index, "coordinate": list(position),
                                            "state": "unknown", "disposition": "unknown",
                                            "unknown_reason": cell["unknown_reason"],
                                        })
                                        continue
                                    disposition = transitions_by_index[index]
                                    observation = {
                                        "cell_index": index, "coordinate": list(position),
                                        "state": cell["state"],
                                        "disposition": disposition["disposition"],
                                        "block_name": cell["block_name"],
                                        "registry_id": cell["registry_id"],
                                    }
                                    if "root_id" in disposition:
                                        observation["root_id"] = disposition["root_id"]
                                    observations.append(observation)
                                summary = {
                                    "actor_id": actor_id, "tick_index": pending.tick_index,
                                    "capture_seq": capture_seq, "bridge_id": bridge_id,
                                    "request_digest": response["request_digest"],
                                    "response_digest": response_digest,
                                    "raw_payload_digest": _sha256(payload_bytes),
                                    "cell_payload_digest": response["cell_payload_digest"],
                                    "raw_cell_count": len(observations),
                                    "transition_count": sum(x["disposition"] == "committed" for x in observations),
                                    "semantic_noop_count": sum(x["disposition"] == "semantic_noop" for x in observations),
                                    "unknown_count": sum(x["disposition"] == "unknown" for x in observations),
                                    "observations": observations,
                                }
                                self._observation_ledger.append(summary)
                                receipt = K11CommitReceipt(
                                    roots, committed, actor_id, pending.tick_index, capture_seq,
                                    observations=observations)
                                self._record_qc(
                                    "accepted", "authenticated_known_cells" if known_cells
                                    else "verified_unknown_only", actor_id=actor_id,
                                    tick_index=pending.tick_index)
                            except BaseException:
                                fatal = self._latch_fatal_locked(
                                    pending, capture_seq, committed, None, None,
                                    "post_commit_receipt",
                                    raw_observations=raw_observations)
                except (_InvalidCapture, K11RejectedBeforeMutation):
                    raise
                except BaseException:
                    # Keep an unexpected validated-snapshot/preflight failure
                    # inside the serialized fatal boundary. Cell-insertion
                    # failures above already reconcile their exact inventory.
                    fatal = self._latch_fatal_locked(
                        pending, capture_seq, committed, None, None,
                        "runtime_preflight", raw_observations=raw_observations)

            chosen_fatal = capacity_fatal or fatal
            if chosen_fatal is not None:
                error, notifier = chosen_fatal
                self._notify_fatal(error, notifier, pending)
                raise error from None
            if deferred is not None:
                if isinstance(deferred, K11RejectedBeforeMutation):
                    raise deferred
                raise self._error("runtime_preflight_rejected", actor_id=actor_id,
                                  tick_index=pending.tick_index) from None
            return receipt
        except _InvalidCapture as exc:
            raise self._error(exc.reason, actor_id=actor_id,
                              tick_index=pending.tick_index) from None
        except (K11RejectedBeforeMutation, K11FatalSensorIngest):
            raise
        except (TypeError, ValueError, OverflowError, UnicodeError):
            if validated:
                # Once the signed 75-cell snapshot passed validation, these are
                # internal failures, not ordinary malformed-input rejections.
                with (self._admission_gate.lock, self._lock,
                      self.runtime._lock, self.runtime.authority._lock):
                    if (not self._fatal_receipts
                            and self._admission_gate.fatal_receipt is None):
                        error, notifier = self._latch_fatal_locked(
                            pending, capture_seq, [], None, None, "runtime_preflight",
                            raw_observations=raw_observations)
                    else:
                        shared = self._admission_gate.fatal_receipt
                        error = (K11HoldEvidenceError("K11 passive capture fatal_sensor_ingest")
                                 if shared is not None else None)
                        notifier = None
                if error is not None:
                    if isinstance(error, K11FatalSensorIngest):
                        self._notify_fatal(error, notifier, pending)
                    raise error from None
                raise
            raise self._error("invalid_capture", actor_id=actor_id,
                              tick_index=pending.tick_index) from None
        except BaseException:
            if validated:
                with (self._admission_gate.lock, self._lock,
                      self.runtime._lock, self.runtime.authority._lock):
                    if (not self._fatal_receipts
                            and self._admission_gate.fatal_receipt is None):
                        error, notifier = self._latch_fatal_locked(
                            pending, capture_seq, [], None, None, "runtime_preflight",
                            raw_observations=raw_observations)
                    else:
                        shared = self._admission_gate.fatal_receipt
                        error = (K11HoldEvidenceError("K11 passive capture fatal_sensor_ingest")
                                 if shared is not None else None)
                        notifier = None
                if error is not None:
                    if isinstance(error, K11FatalSensorIngest):
                        self._notify_fatal(error, notifier, pending)
                    raise error from None
            raise
        finally:
            with self._admission_gate.lock, self._lock:
                self._processing_actors.discard(actor_id)

    def __call__(self, actor_id, request, payload_bytes, request_identity=None):
        return self.ingest_callback(actor_id, request, payload_bytes, request_identity)


class K11PassiveTransport:
    """Bounded sensor-only interface over injected passive observation clients."""
    _REQUEST_FIELDS = REQUEST_KEYS - {"request_hmac"}

    def __init__(self, *, clients_by_actor: Mapping[str, Any], adapter: K11HoldEvidenceAdapter,
                 t0_ns: int, window_close_ns: int, clock_ns=None, synchronous=False):
        if not isinstance(adapter, K11HoldEvidenceAdapter):
            raise TypeError("K11PassiveTransport requires a bound evidence adapter")
        if not isinstance(clients_by_actor, Mapping) or set(clients_by_actor) != set(adapter.actor_ids):
            raise ValueError("one injected observation client is required per actor")
        clients = dict(clients_by_actor)
        if any(not callable(getattr(client, "capture_k11_visible_block_region", None))
               for client in clients.values()):
            raise ValueError("each observation client must support passive K11 capture")
        if type(t0_ns) is not int or t0_ns < 0:
            raise ValueError("t0_ns must be a non-negative integer")
        if type(window_close_ns) is not int or window_close_ns <= t0_ns:
            raise ValueError("window_close_ns must be after t0_ns")
        if type(synchronous) is not bool:
            raise TypeError("synchronous must be a boolean")
        self.clients_by_actor, self.adapter = clients, adapter
        self.t0_ns, self.window_close_ns = t0_ns, window_close_ns
        self._clock_ns = clock_ns if clock_ns is not None else time.monotonic_ns
        if not callable(self._clock_ns):
            raise TypeError("clock_ns must be callable")
        self.synchronous = synchronous
        self._active_actors: set[str] = set()
        self._lock = threading.Lock()

    def _clock(self):
        value = self._clock_ns()
        if type(value) is not int or value < 0:
            raise ValueError("controller clock is invalid")
        return value

    def _normalize_request(self, request):
        if not isinstance(request, Mapping):
            raise ValueError("invalid passive request")
        copied = dict(request)
        if set(copied) != self._REQUEST_FIELDS:
            raise ValueError("invalid passive request")
        actor, tick = copied.get("actor_id"), copied.get("tick_index")
        if (actor not in self.clients_by_actor or type(tick) is not int or tick < 0
                or not isinstance(copied.get("nonce"), str)
                or _NONCE_RE.fullmatch(copied["nonce"]) is None):
            raise ValueError("invalid passive request")
        expected = {
            "schema": REQUEST_SCHEMA, "run_id": self.adapter.run_id,
            "window_id": self.adapter.window_id, "actor_id": actor,
            "sensor_id": SENSOR_ID, "sensor_digest": self.adapter.sensor_digest,
            "profile_digest": self.adapter.profile_digest,
            "ingestion_digest": self.adapter.ingestion_digest,
            "geometry_id": GEOMETRY_ID, "geometry_digest": self.adapter.geometry_digest,
        }
        if any(copied.get(key) != value for key, value in expected.items()):
            raise ValueError("invalid passive request")
        due, deadline = self.t0_ns + tick * CADENCE_NS, self.t0_ns + tick * CADENCE_NS + DEADLINE_NS
        now = self._clock()
        if due >= self.window_close_ns or now < due or now >= deadline or now >= self.window_close_ns:
            raise ValueError("passive request missed its controller window")
        return copied, due, deadline

    @staticmethod
    def _notify(receive, value):
        try:
            receive(value)
            return True
        except Exception:
            return False

    @staticmethod
    def _generic_failure():
        return RuntimeError("K11 passive capture failed")

    def __call__(self, request, receive):
        if not callable(receive):
            raise TypeError("receive callback must be callable")
        try:
            copied, _due, _deadline = self._normalize_request(request)
        except Exception:
            self._notify(receive, self._generic_failure())
            return
        actor = copied["actor_id"]
        with self._lock:
            if actor in self._active_actors:
                active = True
            else:
                self._active_actors.add(actor)
                active = False
        if active:
            self._notify(receive, self._generic_failure())
        elif self.synchronous:
            self._run_worker(copied, receive)
        else:
            try:
                threading.Thread(target=self._run_worker, args=(copied, receive),
                                 daemon=True, name="k11-passive-capture").start()
            except Exception:
                self._release_actor(actor)
                self._notify(receive, self._generic_failure())

    def _run_worker(self, request, receive):
        actor, tick, nonce = request["actor_id"], request["tick_index"], request["nonce"]
        deadline = self.t0_ns + tick * CADENCE_NS + DEADLINE_NS
        registered = False
        try:
            self.adapter.register_pending(actor, tick, nonce, deadline, self.window_close_ns)
            registered = True
            response = self.clients_by_actor[actor].capture_k11_visible_block_region(
                window_id=self.adapter.window_id, tick_index=tick, nonce=nonce)
            if not isinstance(response, dict):
                raise ValueError("invalid passive capture result")
            payload = canonical_bytes(response)
            if len(payload) > MAX_PAYLOAD_BYTES:
                raise ValueError("passive capture result is too large")
            result = SensorResponse(payload=payload, signature_valid=True, capture_ok=True)
        except Exception:
            if registered:
                try:
                    self.adapter.discard_pending(actor, tick, nonce)
                except Exception:
                    pass
            self._notify(receive, self._generic_failure())
            self._release_actor(actor)
            return
        try:
            now = self._clock()
        except Exception:
            try:
                self.adapter.discard_pending(actor, tick, nonce)
            except Exception:
                pass
            self._notify(receive, self._generic_failure())
            self._release_actor(actor)
            return
        if now >= deadline or now >= self.window_close_ns or self.adapter.closed:
            try:
                self.adapter.discard_pending(actor, tick, nonce)
            except Exception:
                pass
        if not self._notify(receive, result):
            try:
                self.adapter.discard_pending(actor, tick, nonce)
            except Exception:
                pass
        self._release_actor(actor)

    def _release_actor(self, actor):
        with self._lock:
            self._active_actors.discard(actor)


__all__ = [
    "EXPECTED_OFFSETS", "GEOMETRY", "GEOMETRY_ID", "K11HoldEvidenceAdapter",
    "K11HoldEvidenceError", "K11PassiveTransport", "MAX_PAYLOAD_BYTES", "REQUEST_KEYS",
    "REQUEST_SCHEMA", "RESPONSE_BINDING_FIELDS", "RESPONSE_KEYS", "SENSOR_ID",
    "UNKNOWN_REASONS",
]
