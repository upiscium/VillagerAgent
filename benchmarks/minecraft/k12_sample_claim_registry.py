"""Durable, audit-only claims for immutable K12 sample identities.

Claims consume sample identities permanently.  This module deliberately does
not create execution authority and has no operation for clearing, replacing,
or reopening a claim.  It is a local, single-host registry, not a distributed
lock service.  A dead creator PID, missing output, released reservation, or
available target never unconsumes an identity.  A process running as the
registry's owner can still erase or replace the registry files; the filesystem
checks below protect against accidents and path substitution, not a malicious
same-principal attacker.
"""
from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
import weakref
from typing import Any, Iterator, Mapping

from benchmarks.minecraft.k12_sample_identity import (
    FINAL_COORDINATES,
    K12SampleIdentityV1,
    K12SamplePlan,
    PHASE_FINAL_CELL,
    PHASE_QUALIFICATION_CELL,
    PHASE_QUALIFICATION_PROBE,
    PROBE_COORDINATES,
    PROBE_SCHEDULE_DIGEST,
    PROBE_SCHEDULE_IDENTITY,
    QUALIFICATION_COORDINATES,
    QUALIFICATION_SCHEDULE_DIGEST,
    QUALIFICATION_SCHEDULE_IDENTITY,
    RANDOMIZATION_DIGEST,
    RANDOMIZATION_SCHEDULE_IDENTITY,
    canonical_json_bytes,
)

CLAIM_ARTIFACT = "minecraft-k12-sample-claim-batch/1"
_LOCK_NAME = ".minecraft-k12-sample-claim-registry.lock"
_CLAIM_NAME = re.compile(r"^claim-([0-9a-f]{64})\.json$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_CLAIM_BYTES = 1024 * 1024
_BINDING_FIELDS = frozenset({
    "sample_id",
    "reservation_id",
    "output_root_identity",
    "nonce",
    "target_identity",
    "ledger_namespace",
})
_CLAIM_FIELDS = frozenset({
    "artifact",
    "registry_root_identity",
    "sample_plan_digest",
    "phase",
    "ordered_sample_identities",
    "ordered_execution_bindings",
    "claim_batch_digest",
    "created_by_pid",
    "created_at_ns",
})
_PHASE_COUNTS = {
    PHASE_QUALIFICATION_CELL: 15,
    PHASE_QUALIFICATION_PROBE: 4,
    PHASE_FINAL_CELL: 90,
}
_PHASE_SCHEDULES = {
    PHASE_QUALIFICATION_CELL: (
        QUALIFICATION_SCHEDULE_IDENTITY,
        QUALIFICATION_SCHEDULE_DIGEST,
        QUALIFICATION_COORDINATES,
    ),
    PHASE_QUALIFICATION_PROBE: (
        PROBE_SCHEDULE_IDENTITY,
        PROBE_SCHEDULE_DIGEST,
        PROBE_COORDINATES,
    ),
    PHASE_FINAL_CELL: (
        RANDOMIZATION_SCHEDULE_IDENTITY,
        RANDOMIZATION_DIGEST,
        FINAL_COORDINATES,
    ),
}
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_local_locks_guard = threading.Lock()
_local_locks: dict[str, threading.RLock] = {}
_registry_instances: weakref.WeakSet[SampleClaimRegistry] = weakref.WeakSet()


def _reset_local_locks_after_fork() -> None:
    # An RLock held by a different parent thread cannot be acquired in the
    # forked child.  More importantly, inherited flock FDs share their open
    # file description with the parent: if the parent dies holding one, a
    # child retaining its copy could lock itself out forever.  Close copied
    # descriptors WITHOUT issuing LOCK_UN against the parent's held lease.
    global _local_locks_guard, _local_locks, _registry_instances
    for registry in tuple(_registry_instances):
        for fd in (registry._lock_fd, registry._root_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        registry._lock_fd = registry._root_fd = -1
        registry._closed = True
    _local_locks_guard = threading.Lock()
    _local_locks = {}
    _registry_instances = weakref.WeakSet()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_local_locks_after_fork)


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _json_object(value: Any, *, what: str) -> dict[str, Any]:
    """Round-trip a value through the frozen canonical JSON implementation."""
    encoded = canonical_json_bytes(value)
    if not isinstance(encoded, bytes):
        raise TypeError(f"{what} canonical encoding must be bytes")
    decoded = _decode_json(encoded, what=what)
    if type(decoded) is not dict or canonical_json_bytes(decoded) != encoded:
        raise ValueError(f"{what} must be a canonical JSON object")
    return decoded


def _decode_json(raw: bytes, *, what: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-JSON constant in {what}: {value}")

    def object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key in {what}: {key}")
            result[key] = value
        return result

    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(text, object_pairs_hook=object_without_duplicates,
                           parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON in {what}") from exc
    return value


def _digest_body(body: Mapping[str, Any]) -> str:
    encoded = canonical_json_bytes(dict(body))
    if not isinstance(encoded, bytes):
        raise TypeError("canonical JSON encoding must be bytes")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_path(value: str | Path) -> str:
    if not isinstance(value, (str, Path)):
        raise TypeError("registry_root must be an absolute canonical path")
    raw = os.fspath(value)
    if type(raw) is not str or not raw or not os.path.isabs(raw):
        raise ValueError("registry_root must be an absolute canonical path")
    if raw == os.path.sep or raw != os.path.normpath(raw):
        raise ValueError("registry_root must be an existing canonical directory")
    if os.path.realpath(raw) != raw:
        raise ValueError("registry_root and its ancestors must not traverse symlinks")
    return raw


def _validate_ancestor_stat(result: os.stat_result, expected_uid: int | None) -> None:
    if result.st_uid not in (0, expected_uid):
        raise PermissionError("registry path ancestor owner is not trusted")
    if (stat.S_IMODE(result.st_mode) & 0o022
            and not result.st_mode & stat.S_ISVTX):
        raise PermissionError("registry path ancestor is writable by another principal")


def _open_directory_chain(path: str, *, expected_uid: int | None) -> int:
    """Open an absolute directory component-by-component without following links."""
    parts = Path(path).parts
    if not parts or parts[0] != os.path.sep:
        raise ValueError("registry_root must be absolute")
    fd = os.open(os.path.sep, _DIR_FLAGS)
    try:
        _validate_ancestor_stat(os.fstat(fd), expected_uid)
        for component in parts[1:]:
            if component in {"", ".", ".."}:
                raise ValueError("registry_root must be canonical")
            next_fd = os.open(component, _DIR_FLAGS | _NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            _validate_ancestor_stat(os.fstat(fd), expected_uid)
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise ValueError("registry_root must be a directory")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _strict_text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be nonempty text")
    return value


@dataclass(frozen=True, slots=True)
class SampleExecutionBinding:
    """Immutable descriptive binding for one already-reserved sample."""

    sample_id: str
    reservation_id: str
    output_root_identity: str
    nonce: str
    target_identity: str
    ledger_namespace: str

    def __post_init__(self) -> None:
        for name in _BINDING_FIELDS:
            _strict_text(getattr(self, name), name)

    def to_dict(self) -> dict[str, str]:
        return {
            "sample_id": self.sample_id,
            "reservation_id": self.reservation_id,
            "output_root_identity": self.output_root_identity,
            "nonce": self.nonce,
            "target_identity": self.target_identity,
            "ledger_namespace": self.ledger_namespace,
        }


@dataclass(frozen=True, slots=True)
class SampleClaimAudit:
    """Immutable audit-only receipt; it is never an execution permit."""

    artifact: str
    sample_plan_digest: str
    phase: str
    claim_batch_digest: str
    created_by_pid: int
    created_at_ns: int
    _registry_root_identity_json: bytes = field(repr=False)
    _ordered_sample_identities_json: tuple[bytes, ...] = field(repr=False)
    ordered_execution_bindings: tuple[SampleExecutionBinding, ...]

    @property
    def registry_root_identity(self) -> dict[str, Any]:
        return json.loads(self._registry_root_identity_json.decode("utf-8"))

    @property
    def ordered_sample_identities(self) -> tuple[dict[str, Any], ...]:
        return tuple(json.loads(value.decode("utf-8"))
                     for value in self._ordered_sample_identities_json)

    @property
    def sample_ids(self) -> tuple[str, ...]:
        return self.ordered_sample_ids

    @property
    def ordered_sample_ids(self) -> tuple[str, ...]:
        return tuple(hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
                     for identity in self.ordered_sample_identities)

    def to_dict(self) -> dict[str, Any]:
        """Return a detached, mutable copy of the persisted audit record."""
        body: dict[str, Any] = {
            "artifact": self.artifact,
            "registry_root_identity": self.registry_root_identity,
            "sample_plan_digest": self.sample_plan_digest,
            "phase": self.phase,
            "ordered_sample_identities": list(self.ordered_sample_identities),
            "ordered_execution_bindings": [binding.to_dict()
                                            for binding in self.ordered_execution_bindings],
            "created_by_pid": self.created_by_pid,
            "created_at_ns": self.created_at_ns,
        }
        body["claim_batch_digest"] = self.claim_batch_digest
        return body


class SampleClaimRegistry:
    """Process- and thread-serialized durable claims under an explicit root.

    The root directory must already exist, be canonical and symlink-free, and
    have owner-only permissions (normally 0700).  Claims only provide durable
    reconciliation metadata.  A fresh process may inspect records but cannot
    recover any execution authority from them.
    """

    def __init__(self, registry_root: str | Path, *, expected_uid: int | None = None):
        self._root_path = _canonical_path(registry_root)
        if expected_uid is not None and (type(expected_uid) is not int or expected_uid < 0):
            raise ValueError("expected_uid must be a nonnegative integer or None")
        effective_uid = getattr(os, "geteuid", None)
        process_uid = effective_uid() if effective_uid is not None else None
        if (process_uid is not None and expected_uid is not None
                and expected_uid != process_uid):
            raise PermissionError("expected_uid must match the parent process effective UID")
        self._expected_uid = process_uid if process_uid is not None else expected_uid
        self._root_fd = -1
        self._lock_fd = -1
        self._lock_identity: tuple[int, int] | None = None
        self._closed = False
        self._creator_pid = os.getpid()
        self._thread_lock = threading.RLock()
        _registry_instances.add(self)
        self._root_fd = _open_directory_chain(
            self._root_path, expected_uid=self._expected_uid,
        )
        try:
            root_stat = os.fstat(self._root_fd)
            self._validate_root_stat(root_stat)
            self._root_identity = {
                "path": self._root_path,
                "device": int(root_stat.st_dev),
                "inode": int(root_stat.st_ino),
                "uid": int(root_stat.st_uid),
            }
            with _local_locks_guard:
                self._thread_lock = _local_locks.setdefault(self._root_path, threading.RLock())
            self._open_lock_file()
            self._verify_root_and_lock()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> "SampleClaimRegistry":
        self._ensure_open()
        self._verify_root_and_lock()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        """Close retained descriptors only; this never clears registry state."""
        if os.getpid() != self._creator_pid:
            # The inherited instance is never usable for claims or audit.
            # Closing its copied FDs must not wait on a stale parent RLock.
            self._close_descriptors()
            return
        with self._thread_lock:
            self._close_descriptors()

    def _close_descriptors(self) -> None:
        if self._closed:
            return
        self._closed = True
        lock_fd, root_fd = self._lock_fd, self._root_fd
        self._lock_fd = self._root_fd = -1
        error: OSError | None = None
        if lock_fd >= 0:
            try:
                os.close(lock_fd)
            except OSError as exc:
                error = exc
        if root_fd >= 0:
            try:
                os.close(root_fd)
            except OSError as exc:
                if error is None:
                    error = exc
        if error is not None:
            raise error

    def claim_phase(
        self,
        *,
        plan: K12SamplePlan,
        phase: str,
        bindings: Sequence[SampleExecutionBinding],
    ) -> SampleClaimAudit:
        """Permanently consume a complete phase in identity order."""
        sample_plan_digest, identities, sample_ids, bindings_value = self._validate_claim_input(
            plan=plan, phase=phase, bindings=bindings,
        )
        with self._exclusive_registry_lock():
            existing = self._scan_claims()
            claimed_ids = {
                hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
                for record in existing
                for identity in record["ordered_sample_identities"]
            }
            overlapping = claimed_ids.intersection(sample_ids)
            if overlapping:
                raise ValueError("ALREADY CLAIMED: one or more sample identities were consumed")

            body: dict[str, Any] = {
                "artifact": CLAIM_ARTIFACT,
                "registry_root_identity": dict(self._root_identity),
                "sample_plan_digest": sample_plan_digest,
                "phase": phase,
                "ordered_sample_identities": identities,
                "ordered_execution_bindings": [item.to_dict() for item in bindings_value],
                "created_by_pid": os.getpid(),
                "created_at_ns": time.time_ns(),
            }
            body["claim_batch_digest"] = _digest_body(body)
            payload = canonical_json_bytes(body)
            if not isinstance(payload, bytes):
                raise TypeError("canonical JSON encoding must be bytes")
            if len(payload) > _MAX_CLAIM_BYTES:
                raise ValueError("claim batch exceeds the maximum durable record size")
            # Verify that a fresh strict read yields exactly this closed record
            # before any persistent write is attempted.
            self._validate_record(body, expected_root=self._root_identity)
            final_name = f"claim-{body['claim_batch_digest']}.json"
            self._durably_publish(final_name, payload, body)
            self._verify_root_and_lock()
            return _audit_from_payload(body)

    def inspect_claims(self) -> tuple[SampleClaimAudit, ...]:
        """Read-only audit of durable claims, never a resume/execution permit."""
        with self._exclusive_registry_lock():
            records = self._scan_claims()
            self._verify_root_and_lock()
            return tuple(_audit_from_payload(record) for record in records)

    def _ensure_open(self) -> None:
        if os.getpid() != self._creator_pid:
            raise RuntimeError("inherited registry cannot be used after fork; open a new registry")
        if self._closed or self._root_fd < 0 or self._lock_fd < 0:
            raise RuntimeError("sample claim registry is closed")

    def _validate_root_stat(self, result: os.stat_result) -> None:
        if not stat.S_ISDIR(result.st_mode):
            raise ValueError("registry_root must be a directory")
        if self._expected_uid is not None and result.st_uid != self._expected_uid:
            raise PermissionError("registry_root is not owned by the expected UID")
        mode = stat.S_IMODE(result.st_mode)
        if mode & 0o077 or mode & 0o700 != 0o700:
            raise PermissionError("registry_root must be owner-only and owner-writable (normally 0700)")

    def _verify_root_path(self) -> None:
        self._ensure_open()
        path_fd = _open_directory_chain(
            self._root_path, expected_uid=self._expected_uid,
        )
        try:
            path_stat = os.fstat(path_fd)
            root_stat = os.fstat(self._root_fd)
            self._validate_root_stat(path_stat)
            if (not _same_inode(path_stat, root_stat)
                    or path_stat.st_dev != self._root_identity["device"]
                    or path_stat.st_ino != self._root_identity["inode"]
                    or path_stat.st_uid != self._root_identity["uid"]):
                raise OSError("registry root path or inode changed")
            try:
                named_stat = os.stat(self._root_path, follow_symlinks=False)
            except OSError as exc:
                raise OSError("registry root path is unavailable") from exc
            if not stat.S_ISDIR(named_stat.st_mode) or not _same_inode(named_stat, root_stat):
                raise OSError("registry root path was replaced")
        finally:
            os.close(path_fd)

    def _verify_root_and_lock(self) -> None:
        self._verify_root_path()
        if self._lock_fd < 0 or self._lock_identity is None:
            raise RuntimeError("sample claim registry lock is unavailable")
        lock_fd_stat = os.fstat(self._lock_fd)
        try:
            lock_path_stat = os.stat(_LOCK_NAME, dir_fd=self._root_fd,
                                     follow_symlinks=False)
        except OSError as exc:
            raise OSError("registry lock path is unavailable") from exc
        if (not stat.S_ISREG(lock_fd_stat.st_mode)
                or not stat.S_ISREG(lock_path_stat.st_mode)
                or not _same_inode(lock_fd_stat, lock_path_stat)
                or (lock_fd_stat.st_dev, lock_fd_stat.st_ino) != self._lock_identity
                or (self._expected_uid is not None
                    and lock_fd_stat.st_uid != self._expected_uid)
                or stat.S_IMODE(lock_fd_stat.st_mode) != 0o600
                or lock_path_stat.st_nlink != 1):
            raise OSError("registry lock file path, owner, mode, or inode changed")

    def _open_lock_file(self) -> None:
        create_flags = (os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW
                        | getattr(os, "O_CLOEXEC", 0))
        open_flags = os.O_RDWR | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        created = False
        try:
            try:
                fd = os.open(_LOCK_NAME, create_flags, 0o600, dir_fd=self._root_fd)
                created = True
            except FileExistsError:
                fd = os.open(_LOCK_NAME, open_flags, dir_fd=self._root_fd)
        except OSError:
            raise
        try:
            result = os.fstat(fd)
            path_result = os.stat(_LOCK_NAME, dir_fd=self._root_fd, follow_symlinks=False)
            if (not stat.S_ISREG(result.st_mode) or not stat.S_ISREG(path_result.st_mode)
                    or not _same_inode(result, path_result) or result.st_nlink != 1
                    or (self._expected_uid is not None and result.st_uid != self._expected_uid)):
                raise PermissionError("registry lock must be a regular owned file")
            if created:
                os.fchmod(fd, 0o600)
                os.fsync(fd)
                os.fsync(self._root_fd)
                result = os.fstat(fd)
                path_result = os.stat(_LOCK_NAME, dir_fd=self._root_fd,
                                      follow_symlinks=False)
            if (stat.S_IMODE(result.st_mode) != 0o600 or result.st_nlink != 1
                    or not _same_inode(result, path_result)):
                raise PermissionError("registry lock must have mode 0600 and a stable inode")
            self._lock_fd = fd
            self._lock_identity = (result.st_dev, result.st_ino)
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def _exclusive_registry_lock(self) -> Iterator[None]:
        self._ensure_open()
        with self._thread_lock:
            self._verify_root_and_lock()
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
                self._verify_root_and_lock()
                yield
                self._verify_root_and_lock()
            finally:
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                finally:
                    self._verify_root_and_lock()

    def _validate_claim_input(
        self,
        *,
        plan: K12SamplePlan,
        phase: str,
        bindings: Sequence[SampleExecutionBinding],
    ) -> tuple[str, list[dict[str, Any]], list[str], tuple[SampleExecutionBinding, ...]]:
        if type(plan) is not K12SamplePlan:
            raise TypeError("plan must be a K12SamplePlan")
        if type(phase) is not str or phase not in _PHASE_COUNTS:
            raise ValueError("phase is not a frozen K12 sample phase")
        sample_plan_digest = plan.sample_plan_digest
        if not _is_sha256(sample_plan_digest):
            raise ValueError("sample plan digest must be SHA-256")
        identity_values = plan.identities(phase)
        if type(identity_values) is not tuple or len(identity_values) != _PHASE_COUNTS[phase]:
            raise ValueError("claim requires the complete ordered phase identity set")
        identities: list[dict[str, Any]] = []
        sample_ids: list[str] = []
        for identity in identity_values:
            if type(identity) is not K12SampleIdentityV1:
                raise TypeError("plan identities must be K12SampleIdentityV1 values")
            sample_id = identity.sample_id
            if not _is_sha256(sample_id):
                raise ValueError("sample identity must expose a SHA-256 sample_id")
            identity_dict = _json_object(identity.to_dict(), what="sample identity")
            if (identity_dict.get("sample_plan_digest") != sample_plan_digest
                    or identity_dict.get("phase") != phase
                    or hashlib.sha256(canonical_json_bytes(identity_dict)).hexdigest() != sample_id):
                raise ValueError("sample identity does not match its plan, phase, or sample_id")
            identities.append(identity_dict)
            sample_ids.append(sample_id)
        if len(set(sample_ids)) != len(sample_ids):
            raise ValueError("phase identities contain duplicate sample IDs")
        if not isinstance(bindings, Sequence) or isinstance(bindings, (str, bytes, bytearray)):
            raise TypeError("bindings must be an ordered sequence")
        binding_values = tuple(bindings)
        if len(binding_values) != len(sample_ids):
            raise ValueError("bindings must exactly cover the complete phase")
        if any(type(item) is not SampleExecutionBinding for item in binding_values):
            raise TypeError("bindings must be SampleExecutionBinding values")
        bound_ids = tuple(item.sample_id for item in binding_values)
        if bound_ids != tuple(sample_ids):
            raise ValueError("bindings must exactly match the ordered phase sample IDs")
        if len(set(bound_ids)) != len(bound_ids):
            raise ValueError("bindings contain duplicate sample IDs")
        return sample_plan_digest, identities, sample_ids, binding_values

    def _scan_claims(self) -> list[dict[str, Any]]:
        self._verify_root_and_lock()
        try:
            names = os.listdir(self._root_fd)
        except OSError:
            raise
        records: list[dict[str, Any]] = []
        seen_samples: set[str] = set()
        for name in sorted(names):
            if name == _LOCK_NAME:
                continue
            match = _CLAIM_NAME.fullmatch(name)
            if match is None:
                raise ValueError(f"unknown, stale, or partial registry entry: {name!r}")
            record = self._read_claim_file(name)
            if record["claim_batch_digest"] != match.group(1):
                raise ValueError("claim filename does not match its closed record digest")
            sample_ids = [hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
                          for identity in record["ordered_sample_identities"]]
            if seen_samples.intersection(sample_ids):
                raise ValueError("registry contains overlapping or ambiguous sample claims")
            seen_samples.update(sample_ids)
            records.append(record)
        self._verify_root_and_lock()
        return records

    def _read_claim_file(self, name: str, *, expected_nlink: int = 1) -> dict[str, Any]:
        try:
            path_stat = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
        except OSError as exc:
            raise OSError("claim path is unavailable") from exc
        if (not stat.S_ISREG(path_stat.st_mode)
                or (self._expected_uid is not None and path_stat.st_uid != self._expected_uid)
                or stat.S_IMODE(path_stat.st_mode) != 0o600
                 or path_stat.st_nlink != expected_nlink or path_stat.st_size > _MAX_CLAIM_BYTES):
            raise ValueError("claim file must be a bounded, owned regular 0600 file")
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                     dir_fd=self._root_fd)
        try:
            opened_stat = os.fstat(fd)
            if not _same_inode(path_stat, opened_stat):
                raise OSError("claim file path changed while opening")
            if (not stat.S_ISREG(opened_stat.st_mode)
                    or (self._expected_uid is not None
                        and opened_stat.st_uid != self._expected_uid)
                    or stat.S_IMODE(opened_stat.st_mode) != 0o600
                    or opened_stat.st_nlink != expected_nlink
                     or opened_stat.st_size > _MAX_CLAIM_BYTES):
                raise ValueError("claim file metadata changed while opening")
            chunks: list[bytes] = []
            remaining = opened_stat.st_size
            while remaining:
                chunk = os.read(fd, min(remaining, 64 * 1024))
                if not chunk:
                    raise OSError("short read of claim file")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(fd, 1):
                raise OSError("claim file grew while reading")
            final_fd_stat = os.fstat(fd)
            final_path_stat = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
            if (not _same_inode(opened_stat, final_fd_stat)
                    or not _same_inode(opened_stat, final_path_stat)
                    or final_fd_stat.st_size != opened_stat.st_size
                    or final_path_stat.st_nlink != expected_nlink):
                raise OSError("claim file changed while reading")
            raw = b"".join(chunks)
        finally:
            os.close(fd)
        value = _decode_json(raw, what="claim file")
        if type(value) is not dict or canonical_json_bytes(value) != raw:
            raise ValueError("claim file is not a canonical JSON object")
        self._validate_record(value, expected_root=self._root_identity)
        return value

    def _validate_record(self, value: Mapping[str, Any], *, expected_root: Mapping[str, Any]) -> None:
        if type(value) is not dict or set(value) != _CLAIM_FIELDS:
            raise ValueError("claim record schema is not closed")
        if value["artifact"] != CLAIM_ARTIFACT:
            raise ValueError("claim artifact marker is unknown")
        root_identity = value["registry_root_identity"]
        if (type(root_identity) is not dict
                or set(root_identity) != {"path", "device", "inode", "uid"}
                or root_identity != dict(expected_root)):
            raise ValueError("claim registry root identity does not match this directory")
        if (type(root_identity.get("path")) is not str
                or any(type(root_identity.get(key)) is not int
                       for key in ("device", "inode", "uid"))):
            raise ValueError("claim registry root identity fields are invalid")
        if not _is_sha256(value["sample_plan_digest"]):
            raise ValueError("claim sample plan digest is invalid")
        phase = value["phase"]
        if type(phase) is not str or phase not in _PHASE_COUNTS:
            raise ValueError("claim phase is unknown")
        identities = value["ordered_sample_identities"]
        bindings = value["ordered_execution_bindings"]
        if (type(identities) is not list or len(identities) != _PHASE_COUNTS[phase]
                or type(bindings) is not list or len(bindings) != len(identities)):
            raise ValueError("claim must persist a complete ordered phase")
        expected_schedule_identity, expected_schedule_digest, expected_coordinates = (
            _PHASE_SCHEDULES[phase]
        )
        sample_ids: list[str] = []
        schedule_pair: tuple[str, str] | None = None
        final_coordinate_fields = {
            "ordinal", "cell_id", "triplet_id", "stratum", "template", "seed", "arm",
        }
        for ordinal, identity in enumerate(identities, start=1):
            if type(identity) is not dict or set(identity) != {
                    "artifact", "sample_plan_digest", "phase", "schedule_identity",
                    "schedule_digest", "coordinate"}:
                raise ValueError("claim identity is not a complete sample identity object")
            # Every persisted identity is a closed JSON object, and its bytes
            # must be representable by the same canonical serializer.
            _json_object(identity, what="persisted sample identity")
            if (identity["artifact"] != "minecraft-k12-sample-identity/1"
                    or identity["sample_plan_digest"] != value["sample_plan_digest"]
                    or identity["phase"] != phase
                    or identity["schedule_identity"] != expected_schedule_identity
                    or identity["schedule_digest"] != expected_schedule_digest
                    or type(identity["coordinate"]) not in (str, dict)):
                raise ValueError("claim identity fields do not match its plan and phase")
            identity_schedule = (identity["schedule_identity"], identity["schedule_digest"])
            if schedule_pair is None:
                schedule_pair = identity_schedule
            elif identity_schedule != schedule_pair:
                raise ValueError("claim mixes identity schedules within one phase")
            coordinate = identity["coordinate"]
            if phase == PHASE_FINAL_CELL:
                if (type(coordinate) is not dict or set(coordinate) != final_coordinate_fields
                        or type(coordinate["ordinal"]) is not int
                        or coordinate["ordinal"] != ordinal
                        or coordinate != dict(expected_coordinates[ordinal - 1])
                        or any(type(coordinate[key]) is not int or coordinate[key] <= 0
                               for key in ("template", "seed"))
                        or any(type(coordinate[key]) is not str or not coordinate[key].strip()
                               for key in ("cell_id", "triplet_id", "stratum", "arm"))):
                    raise ValueError("claim final-cell coordinate schema is invalid")
            elif type(coordinate) is not str or coordinate != expected_coordinates[ordinal - 1]:
                raise ValueError("claim qualification/probe coordinate is invalid")
            sample_ids.append(hashlib.sha256(canonical_json_bytes(identity)).hexdigest())
        if len(set(sample_ids)) != len(sample_ids):
            raise ValueError("claim contains duplicate sample identities")
        binding_ids: list[str] = []
        for binding in bindings:
            _binding_from_mapping(binding)
            binding_ids.append(binding["sample_id"])
        if tuple(binding_ids) != tuple(sample_ids):
            raise ValueError("claim execution bindings do not match ordered identities")
        if (type(value["created_by_pid"]) is not int or value["created_by_pid"] <= 0
                or type(value["created_at_ns"]) is not int or value["created_at_ns"] <= 0):
            raise ValueError("claim audit timestamps are invalid")
        if not _is_sha256(value["claim_batch_digest"]):
            raise ValueError("claim batch digest is invalid")
        digest_body = {key: item for key, item in value.items()
                       if key != "claim_batch_digest"}
        if _digest_body(digest_body) != value["claim_batch_digest"]:
            raise ValueError("claim batch digest does not match the complete record")

    def _durably_publish(self, final_name: str, payload: bytes,
                         expected_record: Mapping[str, Any]) -> None:
        temp_name = f".claim-tmp-{secrets.token_hex(16)}.partial"
        self._verify_root_and_lock()
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=self._root_fd,
        )
        try:
            os.fchmod(temp_fd, 0o600)
            temp_stat = os.fstat(temp_fd)
            named_temp = os.stat(temp_name, dir_fd=self._root_fd, follow_symlinks=False)
            if (not stat.S_ISREG(temp_stat.st_mode) or not _same_inode(temp_stat, named_temp)
                    or (self._expected_uid is not None
                        and temp_stat.st_uid != self._expected_uid)
                    or stat.S_IMODE(temp_stat.st_mode) != 0o600 or temp_stat.st_nlink != 1):
                raise OSError("temporary claim file identity or permissions changed")
            # Establish the partial/uncertain entry durably before writing any
            # claim bytes. Both the inode and its directory name must be
            # fsynced; fsyncing the directory alone need not persist the inode.
            os.fsync(temp_fd)
            os.fsync(self._root_fd)
            self._verify_root_and_lock()
            if not _same_inode(temp_stat, os.stat(
                    temp_name, dir_fd=self._root_fd, follow_symlinks=False)):
                raise OSError("temporary claim path changed after directory fsync")
            # A partial write is never retried: the retained temporary file
            # becomes a fail-closed marker for manual reconciliation.
            written = os.write(temp_fd, payload)
            if written != len(payload):
                raise OSError("short write while persisting claim record")
            if os.fstat(temp_fd).st_size != len(payload):
                raise OSError("claim temporary file has an unexpected size")
            os.fsync(temp_fd)
            self._verify_root_and_lock()
            after_fsync = os.stat(temp_name, dir_fd=self._root_fd, follow_symlinks=False)
            if not _same_inode(temp_stat, after_fsync):
                raise OSError("temporary claim file was replaced")
        finally:
            os.close(temp_fd)

        self._verify_root_and_lock()
        # link(2) is an atomic no-replace publication on the same filesystem.
        # Unlike rename(2), it cannot overwrite an existing claim record.
        os.link(temp_name, final_name, src_dir_fd=self._root_fd,
                dst_dir_fd=self._root_fd, follow_symlinks=False)
        final_stat = os.stat(final_name, dir_fd=self._root_fd, follow_symlinks=False)
        temp_stat = os.stat(temp_name, dir_fd=self._root_fd, follow_symlinks=False)
        if (not stat.S_ISREG(final_stat.st_mode) or not _same_inode(final_stat, temp_stat)
                or final_stat.st_nlink != 2 or stat.S_IMODE(final_stat.st_mode) != 0o600
                or (self._expected_uid is not None and final_stat.st_uid != self._expected_uid)):
            raise OSError("published claim file identity or permissions changed")
        os.fsync(self._root_fd)
        self._verify_root_and_lock()
        readback = self._read_claim_file(final_name, expected_nlink=2)
        if readback != dict(expected_record) or canonical_json_bytes(readback) != payload:
            raise OSError("published claim readback differs from the requested record")
        self._verify_root_and_lock()

        # Only a fully durable and strictly verified publication may remove
        # its private temporary hard link.  Failed cleanup is also fail-closed.
        os.unlink(temp_name, dir_fd=self._root_fd)
        os.fsync(self._root_fd)
        self._verify_root_and_lock()
        final_after_cleanup = os.stat(final_name, dir_fd=self._root_fd,
                                      follow_symlinks=False)
        if final_after_cleanup.st_nlink != 1:
            raise OSError("published claim has an unexpected link count")
        final_readback = self._read_claim_file(final_name)
        if final_readback != dict(expected_record):
            raise OSError("published claim changed after temporary cleanup")
        self._verify_root_and_lock()


def _binding_from_mapping(value: object) -> SampleExecutionBinding:
    if type(value) is not dict or set(value) != _BINDING_FIELDS:
        raise ValueError("execution binding schema is not closed")
    return SampleExecutionBinding(**value)


def _audit_from_payload(value: Mapping[str, Any]) -> SampleClaimAudit:
    root_json = canonical_json_bytes(value["registry_root_identity"])
    identity_json = tuple(canonical_json_bytes(item)
                          for item in value["ordered_sample_identities"])
    if not isinstance(root_json, bytes) or any(not isinstance(item, bytes) for item in identity_json):
        raise TypeError("canonical JSON encoding must be bytes")
    bindings = tuple(_binding_from_mapping(item)
                     for item in value["ordered_execution_bindings"])
    return SampleClaimAudit(
        artifact=value["artifact"],
        sample_plan_digest=value["sample_plan_digest"],
        phase=value["phase"],
        claim_batch_digest=value["claim_batch_digest"],
        created_by_pid=value["created_by_pid"],
        created_at_ns=value["created_at_ns"],
        _registry_root_identity_json=root_json,
        _ordered_sample_identities_json=identity_json,
        ordered_execution_bindings=bindings,
    )


__all__ = [
    "CLAIM_ARTIFACT",
    "SampleClaimAudit",
    "SampleClaimRegistry",
    "SampleExecutionBinding",
]
