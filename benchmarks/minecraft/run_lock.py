from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
import fcntl
import hashlib
import json
import math
import os
import stat
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType


LOCK_METADATA_SCHEMA_VERSION = 3
LOCK_METADATA_STATUSES = frozenset({"acquired", "released", "quarantined", "cleared", "reconciled"})
_MAX_RECORD_BYTES = 65536
_MAX_COUNTER = (1 << 63) - 1
_HISTORY_SCHEMA = "minecraft-target-predecessor-history/1"
_PENDING_SCHEMA = "minecraft-target-predecessor-clear/1"
_TOKEN_SCHEMA = "minecraft-target-predecessor-inspection/1"
_STORAGE_ARTIFACT_ID = "minecraft-target-storage-qualification"
_STORAGE_ARTIFACT_VERSION = 1
_STORAGE_CAPABILITIES = frozenset({
    "single_host_local_persistent_storage",
    "advisory_flock_on_stable_inode",
    "regular_file_fsync",
    "directory_fsync",
    "same_directory_atomic_replace",
    "stable_path_inode_observation",
})
_BOOT_ID_PROVIDER = None
_STORAGE_VALIDATION_MARKER = object()

_LIFECYCLE_LOCKS_GUARD = threading.Lock()
_LIFECYCLE_LOCKS: dict[str, threading.RLock] = {}
# A release whose uncertainty marker could not be durably persisted must keep
# its flock alive for the remainder of this process where possible.  This
# deliberate strong reference prevents ordinary object collection from
# silently turning that unresolved result into an unblocked target.
_UNVERIFIED_RELEASE_LOCKS: dict[int, object] = {}
_UNVERIFIED_ACK_LOCKS: dict[str, object] = {}


class MinecraftTargetLockReleaseStatus(str, Enum):
    VERIFIED_RELEASED = "verified_released"
    NOT_ACQUIRED = "not_acquired"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class MinecraftTargetLockReleaseOutcome:
    status: MinecraftTargetLockReleaseStatus
    error_type: str | None = None
    error: str | None = None
    uncertainty_persisted: bool = False

    @property
    def verified_released(self) -> bool:
        return self.status is MinecraftTargetLockReleaseStatus.VERIFIED_RELEASED


class MinecraftTargetLockError(RuntimeError):
    pass


class MinecraftTargetQuarantinedError(MinecraftTargetLockError):
    def __init__(self, message: str, *, quarantine: dict):
        super().__init__(message)
        self.quarantine = quarantine


class MinecraftTargetLockMetadataError(MinecraftTargetLockError):
    pass


class MinecraftTargetLockUnavailableError(MinecraftTargetLockError):
    def __init__(self, message: str, *, reason: str, owner: dict | None = None):
        super().__init__(message)
        self.reason = reason
        self.owner = dict(owner or {})


class MinecraftTargetLockBusyError(MinecraftTargetLockUnavailableError):
    pass


@dataclass(frozen=True, slots=True)
class MinecraftTargetLeaseSnapshot:
    """Read-only identity for a lease retained by a target lock."""

    fd: int
    fd_dev: int
    fd_ino: int
    path_dev: int
    path_ino: int
    attempt_id: str
    lock_key: str
    owner_pid: int
    owner_alive: bool
    metadata: Mapping[str, object]
    acquired: bool
    quarantined: bool

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))


class MinecraftTargetPredecessorHistoryStatus(str, Enum):
    ACKNOWLEDGED_CLEAN = "ACKNOWLEDGED_CLEAN"
    UNRESOLVED = "UNRESOLVED"
    LEGACY_UNKNOWN = "LEGACY_UNKNOWN"
    HISTORY_UNAVAILABLE = "HISTORY_UNAVAILABLE"
    HISTORY_CORRUPT = "HISTORY_CORRUPT"
    AMBIGUOUS = "AMBIGUOUS"
    DURABILITY_UNCERTAIN = "DURABILITY_UNCERTAIN"


@dataclass(frozen=True, slots=True)
class MinecraftTargetPredecessorInspectionToken:
    """Detached compare-and-swap evidence, not authentication or a permit."""

    state: Mapping[str, object]

    def __post_init__(self) -> None:
        state = _thaw(self.state)
        _validate_inspection_token(state)
        object.__setattr__(self, "state", _freeze_metadata(state))

    def to_json(self) -> str:
        return _canonical_bytes(_thaw(self.state)).decode("utf-8")

    @classmethod
    def from_json(cls, raw: str) -> "MinecraftTargetPredecessorInspectionToken":
        return cls(_strict_json(raw.encode("utf-8"), canonical=True))


@dataclass(frozen=True, slots=True)
class MinecraftTargetPredecessorSnapshot:
    """Coherent observational evidence only. Does not extend a retained lease.

    Durability assumes a single-host persistent local filesystem honoring
    flock, fsync and rename. Checksums do not authenticate same-principal
    writers or prevent backup rollback. Old force-corrupt writers must be
    quiesced before relying on a positive observation.
    """

    status: MinecraftTargetPredecessorHistoryStatus
    first: Mapping[str, object] | None = None
    latest: Mapping[str, object] | None = None
    gaps: Mapping[str, object] | None = None
    acknowledgement: Mapping[str, object] | None = None
    generation: str | None = None
    ordinal: int | None = None
    digest: str | None = None
    observation_count: int | None = None
    rolling_digest: str | None = None
    writer_epoch: str | None = None
    revision: int | None = None
    transition_nonce: str | None = None
    root_identity: tuple[int, int] | None = None
    lock_identity: tuple[int, int] | None = None
    current_owner: Mapping[str, object] | None = None
    active_owner: bool | None = None
    quarantined: bool | None = None
    uncertain: bool = False
    token: MinecraftTargetPredecessorInspectionToken | None = None
    error: str | None = None
    acknowledged_diagnostics: Mapping[str, object] | None = None
    prior_acknowledged_diagnostics: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, MinecraftTargetPredecessorHistoryStatus):
            raise TypeError("history status must be a closed enum value")
        if self.token is not None and not isinstance(self.token, MinecraftTargetPredecessorInspectionToken):
            raise TypeError("inspection token must be detached and immutable")
        for name in ("first", "latest", "gaps", "acknowledgement",
                     "acknowledged_diagnostics", "prior_acknowledged_diagnostics", "current_owner"):
            object.__setattr__(self, name, _freeze_metadata(getattr(self, name)))
        for name in ("root_identity", "lock_identity"):
            value = getattr(self, name)
            if value is not None:
                _validate_identity_pair(value)
                object.__setattr__(self, name, tuple(value))

    def to_dict(self) -> dict:
        return {
            name: (_thaw(self.token.state) if self.token else None)
            if name == "token" else _thaw(getattr(self, name))
            for name in self.__dataclass_fields__
        }


class MinecraftTargetPredecessorAcknowledgementStatus(str, Enum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True, slots=True)
class MinecraftTargetPredecessorAcknowledgementOutcome:
    status: MinecraftTargetPredecessorAcknowledgementStatus
    snapshot: MinecraftTargetPredecessorSnapshot
    error: str | None = None
    retained_lock: bool = False

    @property
    def acknowledged(self) -> bool:
        return self.status is MinecraftTargetPredecessorAcknowledgementStatus.ACKNOWLEDGED


@dataclass(frozen=True, slots=True)
class MinecraftTargetStorageQualification:
    """Validated receipt; internal file identity binds it to the loaded bytes."""

    artifact_id: str
    artifact_version: int
    storage_profile_id: str
    storage_profile_version: int
    receipt_id: str
    boot_id: str
    qualified_root_absolute_path: str
    qualified_root_dev: int
    qualified_root_ino: int
    qualified_filesystem_device: int
    capabilities: frozenset[str]
    issuer_audit_id: str
    detached_artifact_sha256: str
    receipt_path: Path
    receipt_identity: tuple[int, int]
    receipt_file_digest: str
    _validation_marker: object

    def __post_init__(self) -> None:
        if self._validation_marker is not _STORAGE_VALIDATION_MARKER:
            raise TypeError("storage qualification objects must come from the receipt loader")
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        object.__setattr__(self, "receipt_path", Path(self.receipt_path))
        object.__setattr__(self, "receipt_identity", tuple(self.receipt_identity))


def minecraft_target_lock_key(*, host: str, port: int) -> str:
    identity = f"{host.casefold()}:{int(port)}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class MinecraftTargetLock:
    def __init__(
        self,
        *,
        lock_root: str | Path,
        host: str,
        port: int,
        world_id: str,
        attempt_id: str,
        timeout_seconds: float = 0.0,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
            raise ValueError("timeout_seconds must be finite and non-negative")
        self.lock_root = Path(lock_root)
        self.key = minecraft_target_lock_key(host=host, port=port)
        self.path = self.lock_root / f"{self.key}.lock"
        self.host = host
        self.port = int(port)
        self.world_id = world_id
        self.attempt_id = attempt_id
        self.timeout_seconds = float(timeout_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.acquired = False
        self.quarantined = False
        self.quarantine_record = None
        self._quarantine_persistence_failed = False
        self._quarantine_persistence_error: BaseException | None = None
        self.stale_owner_detected = False
        self._stream = None
        self._lease_identity: tuple[int, int] | None = None
        self._history_io = None
        self._prepared_history = None
        self._prepared_metadata_digest = None
        self.last_release_outcome: MinecraftTargetLockReleaseOutcome | None = None

    def acquire(self) -> "MinecraftTargetLock":
        guard = self.lifecycle_guard()
        with guard:
            if self.acquired or self._stream is not None:
                raise MinecraftTargetLockError(
                    "Minecraft target lock instance already retains a lease"
                )
            self.last_release_outcome = None
            self.quarantined = False
            self.quarantine_record = None
            self._quarantine_persistence_failed = False
            self._quarantine_persistence_error = None
            self.stale_owner_detected = False
            try:
                _durable_root(self.lock_root)
                self._stream = _open_lock_stream(self.path)
            except OSError as exc:
                self._close_failed_acquire(unlock=False)
                raise self._unavailable_error() from exc
            deadline = time.monotonic() + self.timeout_seconds

        while True:
            retry = False
            with guard:
                try:
                    fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        owner = self._read_contention_owner_snapshot()
                        message = f"Minecraft target {self.host}:{self.port} is busy"
                        if owner.get("attempt_id"):
                            message += f" with attempt {owner['attempt_id']}"
                        self._close_failed_acquire(unlock=False)
                        raise MinecraftTargetLockBusyError(
                            message,
                            reason="busy",
                            owner=owner,
                        ) from exc
                    retry = True
                except OSError as exc:
                    self._close_failed_acquire(unlock=False)
                    raise self._unavailable_error() from exc
                else:
                    try:
                        self._lease_identity = _verify_open_path_identity(
                            self._stream, self.path
                        )
                        self._history_io = _HistoryIO(
                            self.lock_root, self.key, self.host, self.port, self._stream
                        )
                        uncertain, uncertainty = _uncertainty_marker_state(
                            self.lock_root / f"{self.key}.uncertain"
                        )
                        if uncertain:
                            raise MinecraftTargetLockUnavailableError(
                                f"Minecraft target {self.host}:{self.port} has unresolved lock uncertainty",
                                reason="uncertain",
                                owner=uncertainty,
                            )
                        previous = self._read_metadata()
                        if previous.get("status") == "quarantined":
                            raise MinecraftTargetQuarantinedError(
                                f"Minecraft target {self.host}:{self.port} is quarantined",
                                quarantine=previous,
                            )
                        previous_pid = previous.get("pid")
                        self.stale_owner_detected = (
                            previous.get("status") == "acquired"
                            and isinstance(previous_pid, int)
                            and previous_pid != os.getpid()
                            and not _pid_exists(previous_pid)
                        )
                        acquired_metadata = {
                            "schema_version": LOCK_METADATA_SCHEMA_VERSION,
                            "status": "acquired",
                            "attempt_id": self.attempt_id,
                            "pid": os.getpid(),
                            "host": self.host,
                            "port": self.port,
                            "world_id": self.world_id,
                            "lock_key": self.key,
                            "acquired_at": time.time(),
                            "stale_owner_detected": self.stale_owner_detected,
                        }
                        if previous.get("schema_version") == 1:
                            acquired_metadata.update({
                                "migrated_from_schema_version": 1,
                                "previous_status": previous["status"],
                            })
                        raw = self._history_io.metadata_raw()
                        self._prepared_history = _prepare_history(self._history_io, previous, raw)
                        self._prepared_metadata_digest = _digest(raw)
                        self._write_metadata(acquired_metadata)
                    except OSError as exc:
                        self._close_failed_acquire(unlock=True)
                        raise self._unavailable_error() from exc
                    except BaseException:
                        self._close_failed_acquire(unlock=True)
                        raise
                    self.acquired = True
                    return self

            if retry:
                try:
                    time.sleep(self.poll_interval_seconds)
                except BaseException:
                    with guard:
                        self._close_failed_acquire(unlock=False)
                    raise

    def lifecycle_guard(self):
        """Return the reentrant, process-local guard for this lock pathname.

        This serializes lifecycle operations in this process only.  The file
        lock remains the inter-process admission mechanism.
        """
        return _lifecycle_guard_for(self.path)

    def quarantine(
        self,
        *,
        run_name: str,
        reasons: tuple[str, ...] | list[str],
        diagnostics: dict,
    ) -> dict:
        with self.lifecycle_guard():
            if not self.acquired or self._stream is None:
                raise MinecraftTargetLockError("Minecraft target must be acquired before quarantine")
            if not isinstance(run_name, str) or not run_name.strip():
                raise ValueError("quarantine run_name must be a non-empty string")
            normalized_reasons = tuple(dict.fromkeys(
                reason.strip()
                for reason in reasons
                if isinstance(reason, str) and reason.strip()
            ))
            if not normalized_reasons:
                raise ValueError("quarantine reasons must contain at least one non-empty string")
            if not isinstance(diagnostics, dict):
                raise ValueError("quarantine diagnostics must be an object")
            _verify_retained_identity(self._stream, self.path, self._lease_identity)
            uncertain, uncertainty = _uncertainty_marker_state(
                self.lock_root / f"{self.key}.uncertain"
            )
            if uncertain:
                raise MinecraftTargetLockUnavailableError(
                    "Minecraft target lock has unresolved uncertainty",
                    reason="uncertain",
                    owner=uncertainty,
                )
            acquired = self._read_metadata()
            _validate_current_owner_metadata(
                acquired, self.attempt_id, self.key, self.host, self.port
            )
            if acquired.get("status") != "acquired":
                raise MinecraftTargetLockMetadataError(
                    "Minecraft target acquired metadata does not match the current owner"
                )
            record = {
                **acquired,
                "status": "quarantined",
                "run_name": run_name.strip(),
                "quarantined_at": max(time.time(), acquired["acquired_at"]),
                "reasons": list(normalized_reasons),
                "diagnostics": diagnostics,
            }
            try:
                self._write_metadata(record)
            except BaseException as exc:
                # Cleanup must never turn an unrecorded quarantine request into
                # a verified ordinary release, even if storage later recovers.
                self._quarantine_persistence_failed = True
                self._quarantine_persistence_error = exc
                try:
                    self._persist_uncertainty(
                        attempt_id=self.attempt_id,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                except BaseException:
                    # The failed quarantine write remains the primary error.
                    # release() retries the marker before letting go of flock.
                    pass
                raise
            self.quarantined = True
            self.quarantine_record = record
            return dict(record)

    def release(self) -> MinecraftTargetLockReleaseOutcome:
        with self.lifecycle_guard():
            if not self.acquired or self._stream is None:
                outcome = MinecraftTargetLockReleaseOutcome(
                    MinecraftTargetLockReleaseStatus.NOT_ACQUIRED
                )
                self.last_release_outcome = outcome
                return outcome

            if self._quarantine_persistence_failed:
                failure = self._quarantine_persistence_error
                if failure is None:
                    failure = MinecraftTargetLockError(
                        "Minecraft target quarantine persistence was not verified"
                    )
                # Retain the lease even if persistence is interrupted before
                # _failed_release can record a typed outcome.
                _UNVERIFIED_RELEASE_LOCKS[id(self)] = self
                return self._failed_release(failure)

            try:
                _verify_retained_identity(self._stream, self.path, self._lease_identity)
                metadata = self._read_metadata()
                _validate_current_owner_metadata(
                    metadata, self.attempt_id, self.key, self.host, self.port
                )
                if metadata.get("status") not in {"acquired", "quarantined"}:
                    raise MinecraftTargetLockMetadataError(
                        "Minecraft target acquired metadata does not match the current owner"
                    )
                if self.quarantined != (metadata.get("status") == "quarantined"):
                    raise MinecraftTargetLockMetadataError(
                        "Minecraft target quarantine state does not match its metadata"
                    )
                if self._history_io is None:
                    raise MinecraftTargetLockMetadataError("missing retained predecessor identity")
                # Quarantined release does not rewrite lifecycle metadata, but
                # must participate in the same fence before verified unlock.
                _matching_history(self._history_io, metadata)
                if (self._history_io.read("history-clear-pending") is not None
                        or self._history_io.orphans()):
                    raise MinecraftTargetLockMetadataError(
                        "unfinished predecessor transaction during release"
                    )
                marker = self.lock_root / f"{self.key}.uncertain"
                uncertain, uncertainty = _uncertainty_marker_state(marker)
                if uncertain:
                    raise MinecraftTargetLockUnavailableError(
                        "Minecraft target lock has unresolved uncertainty",
                        reason="uncertain",
                        owner=uncertainty,
                    )
                if not self.quarantined:
                    if not self._persist_uncertainty(
                        attempt_id=self.attempt_id,
                        error_type="ReleaseInProgress",
                        error="lock release is being verified",
                    ):
                        raise OSError(
                            "release uncertainty guard could not be durably persisted"
                        )
                    metadata.update({
                        "status": "released",
                        "released_at": max(time.time(), metadata["acquired_at"]),
                    })
                    self._write_metadata(metadata)
                    verified = self._read_metadata()
                    if verified != metadata:
                        raise MinecraftTargetLockMetadataError(
                            "Minecraft target released metadata could not be verified"
                        )
                _verify_retained_identity(self._stream, self.path, self._lease_identity)
                final_metadata = self._read_metadata()
                _validate_current_owner_metadata(
                    final_metadata, self.attempt_id, self.key, self.host, self.port
                )
                expected_status = "quarantined" if self.quarantined else "released"
                if final_metadata.get("status") != expected_status:
                    raise MinecraftTargetLockMetadataError(
                        "Minecraft target release metadata changed before unlock"
                    )
                if not self.quarantined:
                    _remove_uncertainty_marker(marker)
                    if _uncertainty_marker_state(marker)[0]:
                        raise OSError(
                            "release uncertainty guard remains after verified metadata write"
                        )
            except Exception as exc:
                return self._failed_release(exc)

            try:
                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            except Exception as exc:
                return self._failed_release(exc)
            # A successful explicit LOCK_UN plus the fsynced released metadata
            # proves target release.  Descriptor close is still attempted, but
            # a later close error cannot reverse the kernel's successful unlock
            # or create a gap where an unmarked target becomes reusable.
            stream = self._stream
            if self._history_io is not None:
                self._history_io.close()
                self._history_io = None
            self._stream = None
            self._lease_identity = None
            self.acquired = False
            _UNVERIFIED_RELEASE_LOCKS.pop(id(self), None)
            try:
                stream.close()
            except Exception:
                # Best-effort descriptor cleanup only; the target lease was
                # already explicitly unlocked and positively verified.
                pass
            outcome = MinecraftTargetLockReleaseOutcome(
                MinecraftTargetLockReleaseStatus.VERIFIED_RELEASED
            )
            self.last_release_outcome = outcome
            return outcome

    def _failed_release(self, exc: BaseException) -> MinecraftTargetLockReleaseOutcome:
        if self._stream is not None:
            try:
                # Re-establish an exclusive hold if failure occurred after an
                # earlier LOCK_UN succeeded but before descriptor cleanup.
                fcntl.flock(
                    self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                )
            except Exception:
                pass
        marker = self.lock_root / f"{self.key}.uncertain"
        try:
            uncertainty_persisted = self._persist_uncertainty(
                attempt_id=self.attempt_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
        except BaseException:
            uncertainty_persisted = False
        if not uncertainty_persisted:
            _UNVERIFIED_RELEASE_LOCKS[id(self)] = self
            outcome = MinecraftTargetLockReleaseOutcome(
                MinecraftTargetLockReleaseStatus.FAILED,
                error_type=type(exc).__name__,
                error=str(exc),
                uncertainty_persisted=False,
            )
            self.last_release_outcome = outcome
            return outcome

        # A durable marker makes it safe to let other processes observe the
        # failure, even if unlocking or closing the descriptor itself fails.
        if self._stream is not None:
            try:
                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self._stream.close()
            except Exception:
                pass
            else:
                if self._history_io is not None:
                    self._history_io.close()
                    self._history_io = None
                self._stream = None
                self._lease_identity = None
                self.acquired = False
        if self._stream is not None and self.acquired:
            _UNVERIFIED_RELEASE_LOCKS[id(self)] = self
        else:
            _UNVERIFIED_RELEASE_LOCKS.pop(id(self), None)
        outcome = MinecraftTargetLockReleaseOutcome(
            MinecraftTargetLockReleaseStatus.UNCERTAIN,
            error_type=type(exc).__name__,
            error=str(exc),
            uncertainty_persisted=True,
        )
        self.last_release_outcome = outcome
        return outcome

    def retained_lease_snapshot(self) -> MinecraftTargetLeaseSnapshot:
        with self.lifecycle_guard():
            return self._retained_lease_snapshot_locked()

    def retained_predecessor_snapshot(
        self,
        *,
        storage_qualification: MinecraftTargetStorageQualification | None = None,
    ) -> MinecraftTargetPredecessorSnapshot:
        with self.lifecycle_guard():
            if not self.acquired or self._stream is None or self._history_io is None:
                raise MinecraftTargetLockError("predecessor snapshot requires a retained lease")
            self._history_io.verify()
            metadata = self._read_metadata()
            _validate_current_owner_metadata(metadata, self.attempt_id, self.key, self.host, self.port)
            if metadata["status"] not in {"acquired", "quarantined"}:
                raise MinecraftTargetLockError("predecessor snapshot requires a current owner")
            snapshot = _inspect_predecessor(self._history_io, active_owner=True)
            return _qualified_observation(
                snapshot, storage_qualification, self.lock_root, self._history_io
            )

    def _persist_uncertainty(self, **kwargs):
        # Identity-failure fallback may mark the same pinned root even when
        # the .lock path drifted. Never write into a substituted root.
        if self._history_io is not None:
            self._history_io.verify_root()
        result = _persist_uncertainty_marker(self.lock_root / f"{self.key}.uncertain", **kwargs)
        if self._history_io is not None:
            self._history_io.verify_root()
        return result

    def _retained_lease_snapshot_locked(self) -> MinecraftTargetLeaseSnapshot:
        """Return an observational snapshot of the currently retained lease.

        The descriptor identity comes from the retained stream while the path
        identity is obtained independently with ``lstat``.  In particular,
        the path identity is not substituted for the descriptor identity if
        the lock path has drifted since acquisition.
        """
        stream = self._stream
        if not self.acquired or stream is None:
            raise MinecraftTargetLockError(
                "Minecraft target lock must be acquired before lease snapshot"
            )

        try:
            fd = stream.fileno()
            fd_stat = os.fstat(fd)
        except (OSError, TypeError, ValueError) as exc:
            raise self._unavailable_error() from exc

        try:
            path_stat = os.lstat(self.path)
        except OSError as exc:
            raise self._unavailable_error() from exc

        try:
            stream_position = stream.tell()
        except (OSError, TypeError, ValueError) as exc:
            raise self._unavailable_error() from exc
        try:
            metadata = self._read_metadata()
        except MinecraftTargetLockMetadataError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise self._unavailable_error() from exc
        finally:
            try:
                stream.seek(stream_position)
            except (OSError, TypeError, ValueError) as exc:
                raise self._unavailable_error() from exc

        if not metadata:
            raise MinecraftTargetLockMetadataError(
                "Minecraft target lock metadata is missing"
            )

        status = metadata.get("status")
        if status not in {"acquired", "quarantined"}:
            raise MinecraftTargetLockError(
                "Minecraft target lock does not retain an acquired lease"
            )
        if self.quarantined and status != "quarantined":
            raise MinecraftTargetLockMetadataError(
                "Minecraft target quarantine state does not match its metadata"
            )
        if metadata.get("attempt_id") != self.attempt_id:
            raise MinecraftTargetLockMetadataError(
                "Minecraft target acquired metadata does not match the current owner"
            )

        owner_pid = metadata["pid"]
        try:
            owner_alive = _pid_exists(owner_pid)
        except OverflowError as exc:
            raise MinecraftTargetLockMetadataError(
                "Minecraft target lock owner pid is invalid"
            ) from exc
        return MinecraftTargetLeaseSnapshot(
            fd=fd,
            fd_dev=fd_stat.st_dev,
            fd_ino=fd_stat.st_ino,
            path_dev=path_stat.st_dev,
            path_ino=path_stat.st_ino,
            attempt_id=metadata["attempt_id"],
            lock_key=metadata["lock_key"],
            owner_pid=owner_pid,
            owner_alive=owner_alive,
            metadata=metadata,
            acquired=True,
            quarantined=status == "quarantined",
        )

    def _read_metadata(self) -> dict:
        if self._stream is None:
            return {}
        try:
            raw = os.pread(self._stream.fileno(), _MAX_RECORD_BYTES + 1, 0)
            content = raw.decode("utf-8")
        except UnicodeError as exc:
            raise MinecraftTargetLockMetadataError(
                "Minecraft target lock metadata encoding is invalid"
            ) from exc
        if len(raw) > _MAX_RECORD_BYTES:
            raise MinecraftTargetLockMetadataError("metadata exceeds bounded size")
        if not content.strip():
            return {}
        try:
            payload = _strict_json(raw)
            if payload.get("schema_version") == 3:
                _strict_json(raw, canonical=True)
        except (ValueError, MinecraftTargetLockMetadataError) as exc:
            raise MinecraftTargetLockMetadataError(
                "Minecraft target lock metadata is invalid JSON"
            ) from exc
        return _parse_lock_metadata(
            payload,
            expected_key=self.key,
            expected_host=self.host,
            expected_port=self.port,
        )

    def _write_metadata(self, payload: dict) -> None:
        if self._stream is None:
            raise RuntimeError("lock stream is not open")
        _verify_retained_identity(self._stream, self.path, self._lease_identity)
        io = self._history_io
        if io is None:
            raise MinecraftTargetLockMetadataError("missing retained history identity")
        io.verify()
        previous = self._read_metadata()
        if self.acquired:
            history = _matching_history(io, previous)
            if io.read("history-clear-pending") is not None or io.orphans():
                raise MinecraftTargetLockMetadataError("unfinished predecessor transaction")
        else:
            history = self._prepared_history
            if (history is None or _digest(io.metadata_raw()) != self._prepared_metadata_digest
                    or io.history() != history):
                raise MinecraftTargetLockMetadataError("pre-edit predecessor evidence changed")
        _stamp_metadata(payload, previous, history)
        _parse_lock_metadata(payload, expected_key=self.key, expected_host=self.host, expected_port=self.port)
        _write_stream_metadata(self._stream, payload)
        if self._read_metadata() != payload:
            raise MinecraftTargetLockMetadataError("written metadata readback mismatch")
        io.verify()
        _verify_retained_identity(self._stream, self.path, self._lease_identity)

    def _read_contention_owner_snapshot(self) -> dict:
        if self._stream is None:
            return {}
        try:
            self._stream.seek(0)
            content = self._stream.read(_MAX_RECORD_BYTES + 1)
        except (OSError, UnicodeError):
            return {}
        if not content.strip():
            return {}
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return {}
        return _public_lock_owner_snapshot(payload) if isinstance(payload, dict) else {}

    def _close_failed_acquire(self, *, unlock: bool) -> None:
        if self._stream is None:
            return
        if unlock:
            try:
                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            self._stream.close()
        except OSError:
            pass
        self._stream = None
        self._lease_identity = None
        if self._history_io is not None:
            self._history_io.close()
            self._history_io = None

    def _unavailable_error(self) -> MinecraftTargetLockUnavailableError:
        return MinecraftTargetLockUnavailableError(
            f"Minecraft target lock is unavailable for {self.host}:{self.port}",
            reason="io_error",
        )

    def __enter__(self) -> "MinecraftTargetLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self.release()
        except BaseException:
            if exc_type is None:
                raise


def _lifecycle_guard_for(path: Path) -> threading.RLock:
    normalized = os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(path))))
    with _LIFECYCLE_LOCKS_GUARD:
        guard = _LIFECYCLE_LOCKS.get(normalized)
        if guard is None:
            guard = threading.RLock()
            _LIFECYCLE_LOCKS[normalized] = guard
        return guard


def _open_lock_stream(path: Path, *, exclusive_create: bool = False):
    try:
        current = os.lstat(path)
    except FileNotFoundError:
        current = None
    if current is not None and (
        stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode)
    ):
        raise OSError("Minecraft target lock path is not a regular file")
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    if current is None:
        flags |= os.O_CREAT
        if exclusive_create:
            flags |= os.O_EXCL
    elif exclusive_create:
        raise FileExistsError("Minecraft target lock appeared before exclusive creation")
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o666)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("Minecraft target lock descriptor is not a regular file")
        if current is not None and (current.st_dev, current.st_ino) != (
            opened.st_dev, opened.st_ino
        ):
            raise OSError("Minecraft target lock path identity changed before open")
        if current is None:
            os.fsync(fd)
            _fsync_parent_directory(path)
        return os.fdopen(fd, "r+", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


def _verify_open_path_identity(stream, path: Path) -> tuple[int, int]:
    try:
        fd_stat = os.fstat(stream.fileno())
        path_stat = os.lstat(path)
    except (OSError, TypeError, ValueError) as exc:
        raise MinecraftTargetLockError(
            "Minecraft target lock path or descriptor identity is unavailable"
        ) from exc
    if (
        stat.S_ISLNK(path_stat.st_mode)
        or not stat.S_ISREG(path_stat.st_mode)
        or not stat.S_ISREG(fd_stat.st_mode)
        or (fd_stat.st_dev, fd_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino)
    ):
        raise MinecraftTargetLockError(
            "Minecraft target lock path and descriptor identities do not match"
        )
    return fd_stat.st_dev, fd_stat.st_ino


def _verify_retained_identity(
    stream,
    path: Path,
    expected: tuple[int, int] | None,
) -> None:
    identity = _verify_open_path_identity(stream, path)
    if expected is None or identity != expected:
        raise MinecraftTargetLockError(
            "Minecraft target retained lock identity changed"
        )


def _validate_current_owner_metadata(
    metadata: dict,
    attempt_id: str,
    key: str,
    host: str,
    port: int,
) -> None:
    if (
        metadata.get("status") not in {"acquired", "quarantined", "released"}
        or metadata.get("attempt_id") != attempt_id
        or metadata.get("pid") != os.getpid()
        or metadata.get("lock_key") != key
        or not isinstance(metadata.get("host"), str)
        or metadata["host"].casefold() != host.casefold()
        or metadata.get("port") != int(port)
    ):
        raise MinecraftTargetLockMetadataError(
            "Minecraft target acquired metadata does not match the current owner"
        )


def _uncertainty_marker_state(path: Path) -> tuple[bool, dict]:
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        return False, {}
    except OSError as exc:
        return True, {
            "present": True,
            "valid": False,
            "kind": "unreadable",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }

    kind = (
        "symlink" if stat.S_ISLNK(path_stat.st_mode)
        else "regular_file" if stat.S_ISREG(path_stat.st_mode)
        else "non_file"
    )
    if kind != "regular_file":
        return True, {"present": True, "valid": False, "kind": kind}

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            fd_stat = os.fstat(fd)
            current_path_stat = os.lstat(path)
            if (
                not stat.S_ISREG(fd_stat.st_mode)
                or stat.S_ISLNK(current_path_stat.st_mode)
                or (fd_stat.st_dev, fd_stat.st_ino)
                != (current_path_stat.st_dev, current_path_stat.st_ino)
            ):
                return True, {
                    "present": True,
                    "valid": False,
                    "kind": "identity_ambiguous",
                }
            with os.fdopen(fd, "rb") as stream:
                fd = -1
                raw = stream.read(_MAX_RECORD_BYTES + 1)
        finally:
            if fd >= 0:
                os.close(fd)
    except FileNotFoundError:
        return False, {}
    except OSError as exc:
        return True, {
            "present": True,
            "valid": False,
            "kind": "unreadable",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }

    try:
        payload = _strict_json(raw)
    except (UnicodeError, ValueError, MinecraftTargetLockMetadataError) as exc:
        return True, {
            "present": True,
            "valid": False,
            "kind": "regular_file",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    if not isinstance(payload, dict):
        return True, {
            "present": True,
            "valid": False,
            "kind": "regular_file",
            "error": "uncertainty marker must contain a JSON object",
        }
    valid = (
        payload.get("schema_version") == 1
        and not isinstance(payload.get("schema_version"), bool)
        and payload.get("status") == "uncertain"
    )
    return True, {**payload, "present": True, "valid": valid}


def _fsync_parent_directory(path: Path) -> None:
    flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(path.parent, flags)
    try:
        fd_stat = os.fstat(fd)
        path_stat = os.lstat(path.parent)
        if not stat.S_ISDIR(fd_stat.st_mode) or (
            fd_stat.st_dev,
            fd_stat.st_ino,
        ) != (path_stat.st_dev, path_stat.st_ino):
            raise OSError("uncertainty marker parent directory identity changed")
        os.fsync(fd)
    finally:
        os.close(fd)


def _persist_uncertainty_marker(
    path: Path,
    *,
    attempt_id: str,
    error_type: str,
    error: str,
) -> bool:
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
    )
    payload = {
        "schema_version": 1,
        "status": "uncertain",
        "attempt_id": attempt_id,
        "pid": os.getpid(),
        "created_at": time.time(),
        "error_type": error_type,
        "error": error,
    }
    data = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    fd = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temporary, flags, 0o600)
        opened_stat = os.fstat(fd)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise OSError("uncertainty marker temporary path is not a regular file")
        remaining = memoryview(data)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("uncertainty marker write made no progress")
            remaining = remaining[written:]
        os.fsync(fd)
        temp_path_stat = os.lstat(temporary)
        if (
            stat.S_ISLNK(temp_path_stat.st_mode)
            or (opened_stat.st_dev, opened_stat.st_ino)
            != (temp_path_stat.st_dev, temp_path_stat.st_ino)
        ):
            raise OSError("uncertainty marker temporary path identity changed")
        os.close(fd)
        fd = None
        os.replace(temporary, path)
        marker_stat = os.lstat(path)
        if (
            stat.S_ISLNK(marker_stat.st_mode)
            or not stat.S_ISREG(marker_stat.st_mode)
            or (opened_stat.st_dev, opened_stat.st_ino)
            != (marker_stat.st_dev, marker_stat.st_ino)
        ):
            raise OSError("uncertainty marker path identity changed")
        _fsync_parent_directory(path)
        final_stat = os.lstat(path)
        if (final_stat.st_dev, final_stat.st_ino) != (
            opened_stat.st_dev,
            opened_stat.st_ino,
        ):
            raise OSError("uncertainty marker path changed after directory fsync")
        return True
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            temporary.unlink()
        except OSError:
            pass


def _remove_uncertainty_marker(path: Path) -> bool:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return False
    current = os.lstat(path)
    if (before.st_dev, before.st_ino, before.st_mode) != (
        current.st_dev,
        current.st_ino,
        current.st_mode,
    ):
        raise OSError("uncertainty marker path changed before clear")
    if stat.S_ISDIR(current.st_mode) and not stat.S_ISLNK(current.st_mode):
        os.rmdir(path)
    else:
        os.unlink(path)
    try:
        _fsync_parent_directory(path)
    except OSError:
        # Restore the blocking condition when deletion durability is unknown.
        _persist_uncertainty_marker(
            path,
            attempt_id="clear-fsync-failed",
            error_type="DirectoryFsyncError",
            error="uncertainty marker removal could not be durably verified",
        )
        raise
    return True


def _is_actively_owned(path: Path) -> tuple[bool, str | None]:
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        return False, None
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        return False, "lock path is not a regular file"
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return False, None
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    try:
        fd_stat = os.fstat(fd)
        current_path_stat = os.lstat(path)
        if (fd_stat.st_dev, fd_stat.st_ino) != (
            current_path_stat.st_dev,
            current_path_stat.st_ino,
        ) or stat.S_ISLNK(current_path_stat.st_mode):
            return False, "lock path and descriptor identities do not match"
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True, None
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False, None
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        os.close(fd)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OverflowError as exc:
        raise MinecraftTargetLockMetadataError(
            "Minecraft target lock owner pid is invalid"
        ) from exc
    return True


def _freeze_metadata(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({
            key: _freeze_metadata(item)
            for key, item in value.items()
        })
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_metadata(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_metadata(item) for item in value)
    if isinstance(value, bytearray):
        return bytes(value)
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return value
    raise TypeError("metadata contains an unsupported mutable value")


def _public_lock_owner_snapshot(payload: dict) -> dict:
    snapshot = {}
    for field in ("status", "attempt_id", "run_name"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            snapshot[field] = value.strip()
    return snapshot


def read_minecraft_target_lock_metadata(
    *,
    lock_root: str | Path,
    host: str,
    port: int,
) -> dict:
    key = minecraft_target_lock_key(host=host, port=port)
    path = Path(lock_root) / f"{key}.lock"
    try:
        initial_path_stat = os.lstat(path)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise MinecraftTargetLockMetadataError(
            "Minecraft target lock path identity is unavailable"
        ) from exc
    if stat.S_ISLNK(initial_path_stat.st_mode) or not stat.S_ISREG(
        initial_path_stat.st_mode
    ):
        raise MinecraftTargetLockMetadataError(
            "Minecraft target lock path is not a regular file"
        )
    fd = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        fd_stat = os.fstat(fd)
        current_path_stat = os.lstat(path)
        if (
            not stat.S_ISREG(fd_stat.st_mode)
            or stat.S_ISLNK(current_path_stat.st_mode)
            or (fd_stat.st_dev, fd_stat.st_ino)
            != (current_path_stat.st_dev, current_path_stat.st_ino)
            or (fd_stat.st_dev, fd_stat.st_ino)
            != (initial_path_stat.st_dev, initial_path_stat.st_ino)
        ):
            raise MinecraftTargetLockMetadataError(
                "Minecraft target lock path identity changed while reading"
            )
        with os.fdopen(fd, "rb") as stream:
            fd = None
            raw = os.pread(stream.fileno(), _MAX_RECORD_BYTES + 1, 0)
            if len(raw) > _MAX_RECORD_BYTES:
                raise MinecraftTargetLockMetadataError("metadata exceeds bounded size")
            content = raw.decode("utf-8")
            final_fd_stat = os.fstat(stream.fileno())
            final_path_stat = os.lstat(path)
        if (final_fd_stat.st_dev, final_fd_stat.st_ino) != (
            final_path_stat.st_dev,
            final_path_stat.st_ino,
        ) or stat.S_ISLNK(final_path_stat.st_mode):
            raise MinecraftTargetLockMetadataError(
                "Minecraft target lock path identity changed while reading"
            )
    except UnicodeError as exc:
        raise MinecraftTargetLockMetadataError(
            "Minecraft target lock metadata encoding is invalid"
        ) from exc
    except OSError as exc:
        raise MinecraftTargetLockMetadataError(
            "Minecraft target lock path could not be read safely"
        ) from exc
    finally:
        if fd is not None:
            os.close(fd)
    if not content.strip():
        return {}
    try:
        payload = _strict_json(raw)
        if payload.get("schema_version") == 3:
            _strict_json(raw, canonical=True)
    except (ValueError, MinecraftTargetLockMetadataError) as exc:
        raise MinecraftTargetLockMetadataError(
            "Minecraft target lock metadata is invalid JSON"
        ) from exc
    return _parse_lock_metadata(
        payload,
        expected_key=key,
        expected_host=host,
        expected_port=int(port),
    )


def read_minecraft_target_lock_status(
    *,
    lock_root: str | Path,
    host: str,
    port: int,
) -> dict:
    key = minecraft_target_lock_key(host=host, port=port)
    root = Path(lock_root)
    path = root / f"{key}.lock"
    marker = root / f"{key}.uncertain"
    with _lifecycle_guard_for(path):
        uncertain, uncertainty = _uncertainty_marker_state(marker)
        metadata = {}
        metadata_error = None
        try:
            try:
                lock_stat = os.lstat(path)
            except FileNotFoundError:
                lock_stat = None
            if lock_stat is not None:
                if stat.S_ISLNK(lock_stat.st_mode) or not stat.S_ISREG(lock_stat.st_mode):
                    raise MinecraftTargetLockMetadataError(
                        "Minecraft target lock path is not a regular file"
                    )
                metadata = read_minecraft_target_lock_metadata(
                    lock_root=root, host=host, port=port
                )
        except (OSError, MinecraftTargetLockMetadataError) as exc:
            metadata_error = {
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        actively_owned, ownership_error = _is_actively_owned(path)
        metadata_quarantined = metadata.get("status") == "quarantined"
        uncertain = bool(
            uncertain
            or metadata_error is not None
            or ownership_error is not None
        )
        quarantined = bool(metadata_quarantined or uncertain)
        if uncertain and uncertainty is None:
            uncertainty = metadata_error or {
                "present": True,
                "valid": False,
                "kind": "identity_ambiguous",
                "error": ownership_error,
            }
        status = {
            "metadata": metadata,
            "quarantined": quarantined,
            "uncertain": uncertain,
            "actively_owned": actively_owned,
            "uncertainty": uncertainty if uncertain else None,
            "blocking": bool(
                quarantined
                or uncertain
                or actively_owned
                or metadata_error is not None
                or ownership_error is not None
            ),
        }
        if metadata_error is not None:
            status["metadata_error"] = metadata_error
        if ownership_error is not None:
            status["ownership_error"] = ownership_error
        return status


def clear_minecraft_target_quarantine(
    *,
    lock_root: str | Path,
    host: str,
    port: int,
    reason: str,
    acknowledge_target_safe: bool,
    force_corrupt: bool = False,
) -> dict:
    if not acknowledge_target_safe:
        raise ValueError("clearing quarantine requires acknowledge_target_safe")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("clearing quarantine requires a non-empty reason")
    key = minecraft_target_lock_key(host=host, port=port)
    root = Path(lock_root)
    path = root / f"{key}.lock"
    marker = root / f"{key}.uncertain"
    with _lifecycle_guard_for(path):
        _durable_root(root)
        stream = None
        io = None
        locked = False
        try:
            try:
                stream = _open_lock_stream(path)
            except OSError as exc:
                raise MinecraftTargetLockMetadataError(
                    "Minecraft target lock path is not a safe regular file"
                ) from exc
            try:
                identity = _verify_open_path_identity(stream, path)
            except Exception as exc:
                _persist_uncertainty_marker(
                    marker,
                    attempt_id="quarantine-clear",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                raise
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError as exc:
                raise MinecraftTargetLockError(
                    f"Minecraft target {host}:{int(port)} is actively locked"
                ) from exc
            io = _HistoryIO(root, key, host, port, stream)
            try:
                _verify_retained_identity(stream, path, identity)
            except Exception as exc:
                _persist_uncertainty_marker(
                    marker,
                    attempt_id="quarantine-clear",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                raise
            uncertainty_present, _uncertainty = _uncertainty_marker_state(marker)
            raw = io.metadata_raw()
            content = raw.decode("utf-8", errors="replace")
            previous = {}
            if content.strip():
                try:
                    previous = _parse_raw_metadata(io, raw)
                except (ValueError, MinecraftTargetLockMetadataError) as exc:
                    if not force_corrupt:
                        raise MinecraftTargetLockMetadataError(
                            "corrupt Minecraft target metadata requires force_corrupt"
                        ) from exc
            if (
                previous
                and previous.get("status") != "quarantined"
                and not force_corrupt
                and not uncertainty_present
            ):
                raise MinecraftTargetLockError("Minecraft target is not quarantined")
            last_quarantine = _public_quarantine_history(previous)
            cleared = {
                "schema_version": LOCK_METADATA_SCHEMA_VERSION,
                "status": "cleared",
                "lock_key": key,
                "host": host,
                "port": int(port),
                "cleared_at": time.time(),
                "cleared_by_pid": os.getpid(),
                "clear_reason": reason.strip(),
                "last_quarantine": last_quarantine,
            }
            try:
                _verify_retained_identity(stream, path, identity)
            except Exception as exc:
                _persist_uncertainty_marker(
                    marker,
                    attempt_id="quarantine-clear",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                raise
            history = _prepare_history(io, previous, raw, repair_corrupt=force_corrupt)
            _stamp_metadata(cleared, previous, history)
            io.verify()
            _write_stream_metadata(stream, cleared)
            written = _parse_raw_metadata(io, io.metadata_raw())
            if written != cleared:
                raise MinecraftTargetLockMetadataError(
                    "cleared Minecraft target metadata could not be verified"
                )
            io.verify()
            _verify_retained_identity(stream, path, identity)
            if uncertainty_present:
                _remove_uncertainty_marker(marker)
            try:
                _verify_retained_identity(stream, path, identity)
            except Exception as exc:
                _persist_uncertainty_marker(
                    marker,
                    attempt_id="quarantine-clear",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                raise
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            locked = False
            return cleared
        finally:
            if io is not None:
                io.close()
            if stream is not None:
                if locked:
                    try:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                try:
                    stream.close()
                except OSError:
                    pass


def _parse_lock_metadata(
    payload: object,
    *,
    expected_key: str,
    expected_host: str,
    expected_port: int,
) -> dict:
    if not isinstance(payload, dict):
        raise MinecraftTargetLockMetadataError("Minecraft target lock metadata must be an object")
    schema_version = payload.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version not in {1, 2, LOCK_METADATA_SCHEMA_VERSION}
    ):
        raise MinecraftTargetLockMetadataError("Minecraft target lock metadata schema is unsupported")
    _validate_metadata_identity(
        payload,
        expected_key=expected_key,
        expected_host=expected_host,
        expected_port=expected_port,
    )
    if schema_version == 1:
        allowed = {"schema_version", "status", "lock_key", "host", "port", "attempt_id", "pid",
                   "world_id", "acquired_at", "released_at", "stale_owner_detected"}
        if set(payload) - allowed:
            raise MinecraftTargetLockMetadataError("unknown legacy metadata fields")
        return _validate_schema_v1_metadata(payload)
    if schema_version == 3:
        return _validate_schema_v3_metadata(payload)
    allowed = {"schema_version", "status", "lock_key", "host", "port", "attempt_id", "pid", "world_id",
               "acquired_at", "released_at", "stale_owner_detected", "migrated_from_schema_version",
               "previous_status", "run_name", "quarantined_at", "reasons", "diagnostics", "cleared_at",
               "cleared_by_pid", "clear_reason", "last_quarantine"}
    if set(payload) - allowed:
        raise MinecraftTargetLockMetadataError("unknown legacy metadata fields")
    return _validate_schema_v2_metadata(payload)


def _validate_metadata_identity(
    payload: dict,
    *,
    expected_key: str,
    expected_host: str,
    expected_port: int,
) -> None:
    if (
        payload.get("lock_key") != expected_key
        or not isinstance(payload.get("host"), str)
        or payload["host"].casefold() != expected_host.casefold()
        or not isinstance(payload.get("port"), int)
        or isinstance(payload.get("port"), bool)
        or payload["port"] != int(expected_port)
    ):
        raise MinecraftTargetLockMetadataError("Minecraft target lock metadata identity mismatch")


def _validate_schema_v1_metadata(payload: dict) -> dict:
    status = payload.get("status")
    if not isinstance(status, str) or status not in {"acquired", "released"}:
        raise MinecraftTargetLockMetadataError("Minecraft target lock metadata status is invalid")
    attempt_id = payload.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        raise MinecraftTargetLockMetadataError("Minecraft target legacy metadata is invalid")
    if payload["status"] == "acquired":
        pid = payload.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise MinecraftTargetLockMetadataError("Minecraft target legacy metadata is invalid")
    return dict(payload)


def _validate_schema_v2_metadata(payload: dict) -> dict:
    status = payload.get("status")
    if not isinstance(status, str) or status not in LOCK_METADATA_STATUSES - {"reconciled"}:
        raise MinecraftTargetLockMetadataError("Minecraft target lock metadata status is invalid")
    if status == "acquired":
        _validate_acquisition_metadata(payload)
    elif status == "released":
        _validate_acquisition_metadata(payload)
        if (
            not _is_non_negative_finite_number(payload.get("released_at"))
            or payload["released_at"] < payload["acquired_at"]
        ):
            raise MinecraftTargetLockMetadataError("Minecraft target released metadata is invalid")
    elif status == "quarantined":
        _validate_acquisition_metadata(payload)
        if (
            not _is_non_empty_string(payload.get("run_name"))
            or not _is_non_negative_finite_number(payload.get("quarantined_at"))
            or payload["quarantined_at"] < payload["acquired_at"]
            or not _is_non_empty_string_list(payload.get("reasons"))
            or not isinstance(payload.get("diagnostics"), dict)
        ):
            raise MinecraftTargetLockMetadataError("Minecraft target quarantine metadata is invalid")
    else:
        _validate_cleared_metadata(payload)
    return dict(payload)


def _validate_acquisition_metadata(payload: dict) -> None:
    if (
        not _is_non_empty_string(payload.get("attempt_id"))
        or not _is_positive_int(payload.get("pid"))
        or not isinstance(payload.get("world_id"), str)
        or not _is_non_negative_finite_number(payload.get("acquired_at"))
        or not isinstance(payload.get("stale_owner_detected"), bool)
    ):
        raise MinecraftTargetLockMetadataError("Minecraft target acquisition metadata is invalid")
    if (
        "migrated_from_schema_version" in payload
        and (
            not isinstance(payload["migrated_from_schema_version"], int)
            or isinstance(payload["migrated_from_schema_version"], bool)
            or payload["migrated_from_schema_version"] != 1
        )
    ):
        raise MinecraftTargetLockMetadataError("Minecraft target migration metadata is invalid")
    if (
        "previous_status" in payload
        and (
            not isinstance(payload["previous_status"], str)
            or payload["previous_status"] not in {"acquired", "released"}
        )
    ):
        raise MinecraftTargetLockMetadataError("Minecraft target migration metadata is invalid")


def _validate_cleared_metadata(payload: dict) -> None:
    last_quarantine = payload.get("last_quarantine")
    if (
        not _is_non_negative_finite_number(payload.get("cleared_at"))
        or not _is_positive_int(payload.get("cleared_by_pid"))
        or not _is_non_empty_string(payload.get("clear_reason"))
        or not isinstance(last_quarantine, dict)
    ):
        raise MinecraftTargetLockMetadataError("Minecraft target cleared metadata is invalid")
    if last_quarantine and (
        not _is_non_empty_string(last_quarantine.get("attempt_id"))
        or not _is_non_empty_string(last_quarantine.get("run_name"))
        or not _is_non_negative_finite_number(last_quarantine.get("quarantined_at"))
        or not _is_non_empty_string_list(last_quarantine.get("reasons"))
    ):
        raise MinecraftTargetLockMetadataError("Minecraft target cleared metadata is invalid")
    if last_quarantine:
        _closed(last_quarantine, {"attempt_id", "run_name", "quarantined_at", "reasons"})


def _validate_schema_v3_metadata(payload: dict) -> dict:
    common = {"schema_version", "status", "lock_key", "host", "port", "writer_epoch",
              "revision", "transition_nonce", "history_pointer", "checksum"}
    owner = {"attempt_id", "pid", "world_id", "acquired_at", "stale_owner_detected"}
    states = {
        "acquired": owner,
        "released": owner | {"released_at"},
        "quarantined": owner | {"run_name", "quarantined_at", "reasons", "diagnostics"},
        "cleared": {"cleared_at", "cleared_by_pid", "clear_reason", "last_quarantine"},
        "reconciled": {"reconciled_at"},
    }
    status = payload.get("status")
    if not isinstance(status, str) or status not in states:
        raise MinecraftTargetLockMetadataError("invalid v3 metadata lifecycle")
    optional = {"migrated_from_schema_version", "previous_status"} if status in {
        "acquired", "released", "quarantined"
    } else set()
    allowed = common | states[status]
    if set(payload) - allowed not in (set(), optional):
        raise MinecraftTargetLockMetadataError("unknown or incomplete v3 metadata fields")
    _closed(payload, allowed | (optional & set(payload)))
    if (not _hex(payload["writer_epoch"], 32) or not _counter(payload["revision"], minimum=1)
            or not _hex(payload["transition_nonce"], 32)):
        raise MinecraftTargetLockMetadataError("invalid v3 writer fence")
    _validate_pointer(payload["history_pointer"])
    if status == "reconciled":
        if not _is_non_negative_finite_number(payload["reconciled_at"]):
            raise MinecraftTargetLockMetadataError("invalid reconciliation metadata")
    else:
        _validate_schema_v2_metadata(payload)
    _verify_checksum(payload)
    return dict(payload)


def _is_non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_non_empty_string_list(value: object) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(_is_non_empty_string(item) for item in value)
    )


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_non_negative_finite_number(value: object) -> bool:
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0)
    except OverflowError:
        return False


def _write_stream_metadata(stream, payload: dict) -> None:
    data = _canonical_bytes(payload)
    stream.seek(0)
    stream.truncate()
    stream.flush()
    _write_complete(stream.fileno(), data)
    os.fsync(stream.fileno())
    if os.pread(stream.fileno(), _MAX_RECORD_BYTES + 1, 0) != data:
        raise MinecraftTargetLockMetadataError("metadata readback mismatch")


def _public_quarantine_history(metadata: dict) -> dict:
    if metadata.get("status") != "quarantined":
        return {}
    return {
        "attempt_id": metadata.get("attempt_id"),
        "run_name": metadata.get("run_name"),
        "quarantined_at": metadata.get("quarantined_at"),
        "reasons": list(metadata.get("reasons", [])),
    }


# Every lifecycle writer participates in this protocol. Acquire/migration and
# unowned safety-clear use _prepare_history before overwrite. Retained release
# and quarantine validate/carry the exact pointer via _write_metadata. Their
# failure paths only publish .uncertain under the retained flock. Context exit
# delegates to release. Only acknowledge_minecraft_target_predecessor promotes
# history to CLEAN, through pending -> history -> reconciled -> pending removal.


def _thaw(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_thaw(item) for item in value)
    return value


def _canonical_bytes(payload: object) -> bytes:
    try:
        raw = (json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise MinecraftTargetLockMetadataError("invalid canonical record") from exc
    if len(raw) > _MAX_RECORD_BYTES:
        raise MinecraftTargetLockMetadataError("record exceeds bounded size")
    return raw


def _strict_json(raw: bytes, *, canonical: bool = False) -> dict:
    if len(raw) > _MAX_RECORD_BYTES:
        raise MinecraftTargetLockMetadataError("record exceeds bounded size")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise MinecraftTargetLockMetadataError("duplicate JSON field")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise MinecraftTargetLockMetadataError("nonfinite JSON value")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                           parse_constant=invalid_constant)
    except UnicodeError as exc:
        raise MinecraftTargetLockMetadataError("metadata encoding is invalid") from exc
    except (ValueError, RecursionError) as exc:
        raise MinecraftTargetLockMetadataError("invalid JSON record") from exc
    if not isinstance(value, dict):
        raise MinecraftTargetLockMetadataError("record must be a JSON object")
    if canonical and raw != _canonical_bytes(value):
        raise MinecraftTargetLockMetadataError("noncanonical JSON record")
    return value


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hex(value: object, length: int) -> bool:
    return (isinstance(value, str) and len(value) == length
            and all(char in "0123456789abcdef" for char in value))


def _counter(value: object, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= _MAX_COUNTER


def _validate_identity_pair(value: object, *, absent: bool = False) -> None:
    if absent and value is None:
        return
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or not all(_counter(item) for item in value)):
        raise MinecraftTargetLockMetadataError("invalid filesystem identity")


def _increment(value: int) -> int:
    if not _counter(value) or value == _MAX_COUNTER:
        raise MinecraftTargetLockMetadataError("bounded counter exhausted")
    return value + 1


def _closed(payload: object, fields: set[str]) -> None:
    if not isinstance(payload, dict) or set(payload) != fields:
        raise MinecraftTargetLockMetadataError("missing or unknown record fields")


def _seal(payload: dict) -> dict:
    value = {key: item for key, item in payload.items() if key != "checksum"}
    value["checksum"] = _digest(_canonical_bytes(value))
    return value


def _verify_checksum(payload: dict) -> None:
    if not _hex(payload.get("checksum"), 64) or payload != _seal(payload):
        raise MinecraftTargetLockMetadataError("record checksum mismatch")


def _history_pointer(history: dict) -> dict:
    return {"generation": history["generation"], "ordinal": history["ordinal"],
            "digest": history["checksum"]}


def _validate_pointer(pointer: object) -> None:
    _closed(pointer, {"generation", "ordinal", "digest"})
    if (not _hex(pointer["generation"], 32) or not _counter(pointer["ordinal"], minimum=1)
            or not _hex(pointer["digest"], 64)):
        raise MinecraftTargetLockMetadataError("invalid history pointer")


def _validate_observation(observation: object) -> None:
    if observation is None:
        return
    _closed(observation, {"attempt_id", "acquired_at", "pid", "source_digest"})
    if (not _is_non_empty_string(observation["attempt_id"])
            or not _is_positive_int(observation["pid"])
            or not _hex(observation["source_digest"], 64)
            or (observation["acquired_at"] is not None
                and not _is_non_negative_finite_number(observation["acquired_at"]))):
        raise MinecraftTargetLockMetadataError("invalid predecessor observation")


def _validate_diagnostics(value: object) -> None:
    _closed(value, {"first", "latest", "observation_count", "rolling_digest"})
    _validate_observation(value["first"])
    _validate_observation(value["latest"])
    if (not _counter(value["observation_count"])
            or (value["observation_count"] == 0) != (value["first"] is None)
            or (value["observation_count"] == 0) != (value["latest"] is None)
            or not _hex(value["rolling_digest"], 64)):
        raise MinecraftTargetLockMetadataError("invalid acknowledged diagnostics")


def _merge_diagnostics(prior, latest):
    """Keep a bounded summary of older acknowledgements, never an owner journal."""
    if prior is None:
        return latest
    if latest is None or latest["observation_count"] == 0:
        return prior
    return {
        "first": prior["first"] or latest["first"],
        "latest": latest["latest"] or prior["latest"],
        "observation_count": prior["observation_count"] + latest["observation_count"],
        "rolling_digest": _digest((prior["rolling_digest"] + latest["rolling_digest"]).encode("ascii")),
    }


def _validate_history(payload: dict) -> dict:
    _closed(payload, {"schema", "lock_key", "host", "port", "root_identity", "lock_identity",
                      "generation", "ordinal", "state", "writer_epoch", "source", "first",
                      "latest", "observation_count", "rolling_digest", "acknowledged_diagnostics",
                      "prior_acknowledged_diagnostics", "gaps", "acknowledgement", "checksum"})
    if (payload["schema"] != _HISTORY_SCHEMA or not _hex(payload["lock_key"], 64)
            or not _is_non_empty_string(payload["host"]) or payload["host"] != payload["host"].casefold()
            or not _counter(payload["port"], minimum=1) or payload["port"] > 65535
            or not _hex(payload["generation"], 32) or not _hex(payload["writer_epoch"], 32)
            or not _counter(payload["ordinal"], minimum=1)
            or not isinstance(payload["state"], str)
            or payload["state"] not in {"LEGACY_UNKNOWN", "UNRESOLVED", "ACKNOWLEDGED_CLEAN"}
            or not _counter(payload["observation_count"]) or not _hex(payload["rolling_digest"], 64)):
        raise MinecraftTargetLockMetadataError("invalid history record")
    for name in ("root_identity", "lock_identity"):
        _validate_identity_pair(payload[name])
    source = payload["source"]
    _closed(source, {"metadata_digest", "writer_epoch", "prior_pointer"})
    if (not _hex(source["metadata_digest"], 64)
            or (source["writer_epoch"] is not None and not _hex(source["writer_epoch"], 32))):
        raise MinecraftTargetLockMetadataError("invalid history source")
    if source["prior_pointer"] is not None:
        _validate_pointer(source["prior_pointer"])
    for name in ("first", "latest"):
        _validate_observation(payload[name])
    if (payload["observation_count"] == 0 and (payload["first"] is not None or payload["latest"] is not None)) or (
            payload["observation_count"] > 0 and (payload["first"] is None or payload["latest"] is None)):
        raise MinecraftTargetLockMetadataError("inconsistent observation count")
    if payload["state"] == "UNRESOLVED" and payload["observation_count"] == 0:
        raise MinecraftTargetLockMetadataError("unresolved history without observation")
    if payload["acknowledged_diagnostics"] is not None:
        _validate_diagnostics(payload["acknowledged_diagnostics"])
    if payload["prior_acknowledged_diagnostics"] is not None:
        _validate_diagnostics(payload["prior_acknowledged_diagnostics"])
    gaps = payload["gaps"]
    _closed(gaps, {"count", "first_digest", "latest_digest", "rolling_digest"})
    if (not _counter(gaps["count"]) or not _hex(gaps["rolling_digest"], 64)
            or any((not _hex(gaps[name], 64) if gaps["count"] else gaps[name] is not None)
                   for name in ("first_digest", "latest_digest"))):
        raise MinecraftTargetLockMetadataError("invalid history gaps")
    ack = payload["acknowledgement"]
    if ack is not None:
        _closed(ack, {"operator", "reason", "acknowledged_at", "covered_generation", "covered_ordinal",
                      "covered_digest", "covered_metadata_digest", "covered_writer_epoch",
                      "covered_metadata_kind", "covered_revision", "covered_transition_nonce",
                      "covered_unflocked_acquired_owner", "whole_prefix", "writer_epoch",
                      "reconcile_unknown_history", "storage_profile_id", "storage_profile_version"})
        optional_identity = (ack["covered_generation"], ack["covered_ordinal"], ack["covered_digest"])
        if any(value is not None for value in optional_identity):
            if (not _hex(ack["covered_generation"], 32)
                    or not _counter(ack["covered_ordinal"], minimum=1)
                    or ack["covered_generation"] != payload["generation"]
                    or ack["covered_ordinal"] >= payload["ordinal"]
                    or not _hex(ack["covered_digest"], 64)):
                raise MinecraftTargetLockMetadataError("invalid inspected history coverage")
        if (not _is_non_empty_string(ack["operator"]) or not _is_non_empty_string(ack["reason"])
                or not _is_non_negative_finite_number(ack["acknowledged_at"])
                or not isinstance(ack["covered_metadata_kind"], str)
                or ack["covered_metadata_kind"] not in {"absent", "blank", "legacy", "v3", "corrupt"}
                or (ack["covered_metadata_digest"] is None
                    if ack["covered_metadata_kind"] != "absent"
                    else ack["covered_metadata_digest"] is not None)
                or (ack["covered_metadata_digest"] is not None
                    and not _hex(ack["covered_metadata_digest"], 64))
                or (ack["covered_writer_epoch"] is not None and not _hex(ack["covered_writer_epoch"], 32))
                or ack["whole_prefix"] is not True or not _hex(ack["writer_epoch"], 32)
                or not isinstance(ack["reconcile_unknown_history"], bool)
                or not _is_bounded_string(ack["storage_profile_id"])
                or not _is_positive_int(ack["storage_profile_version"])):
            raise MinecraftTargetLockMetadataError("invalid whole-prefix acknowledgement")
        if ack["covered_metadata_kind"] == "v3":
            if (not _hex(ack["covered_writer_epoch"], 32)
                    or not _counter(ack["covered_revision"], minimum=1)
                    or not _hex(ack["covered_transition_nonce"], 32)):
                raise MinecraftTargetLockMetadataError("invalid covered writer identity")
        elif any(ack[name] is not None for name in (
            "covered_writer_epoch", "covered_revision", "covered_transition_nonce",
        )):
            raise MinecraftTargetLockMetadataError("unexpected covered legacy writer identity")
        if ack["covered_unflocked_acquired_owner"] is not None:
            _validate_observation(ack["covered_unflocked_acquired_owner"])
            if ack["covered_unflocked_acquired_owner"]["source_digest"] != ack["covered_metadata_digest"]:
                raise MinecraftTargetLockMetadataError("covered owner does not match inspected metadata")
    if payload["state"] == "ACKNOWLEDGED_CLEAN" and (
            ack is None or ack["writer_epoch"] != payload["writer_epoch"]
            or payload["observation_count"] != 0 or payload["first"] is not None or payload["latest"] is not None):
        raise MinecraftTargetLockMetadataError("unacknowledged clean history")
    _verify_checksum(payload)
    return payload


def _observation_from_ack(ack):
    return ack["covered_unflocked_acquired_owner"]


def _validate_pending(payload):
    _closed(payload, {"schema", "transaction_id", "expected", "checksum"})
    if payload["schema"] != _PENDING_SCHEMA or not _hex(payload["transaction_id"], 32):
        raise MinecraftTargetLockMetadataError("invalid pending-clear transaction")
    _validate_inspection_token(payload["expected"])
    _verify_checksum(payload)


def _validate_inspection_token(payload: dict) -> None:
    fields = {"schema", "lock_key", "host", "port", "root_path", "root_identity", "lock_identity",
              "metadata_digest", "metadata_kind", "metadata_status", "writer_epoch", "revision",
              "transition_nonce", "history_digest", "history_pointer", "history_kind", "pending_digest",
              "uncertainty_digest", "quarantined", "orphans"}
    _closed(payload, fields)
    if (payload["schema"] != _TOKEN_SCHEMA or not _hex(payload["lock_key"], 64)
            or not isinstance(payload["root_path"], str) or not os.path.isabs(payload["root_path"])
            or os.path.abspath(payload["root_path"]) != payload["root_path"]
            or not _is_non_empty_string(payload["host"]) or payload["host"] != payload["host"].casefold()
            or not _counter(payload["port"], minimum=1) or payload["port"] > 65535
            or payload["lock_key"] != minecraft_target_lock_key(host=payload["host"], port=payload["port"])
            or not isinstance(payload["metadata_kind"], str)
            or payload["metadata_kind"] not in {"absent", "blank", "legacy", "v3", "corrupt"}
            or not isinstance(payload["history_kind"], str)
            or payload["history_kind"] not in {"absent", "valid", "corrupt"}
            or (payload["metadata_status"] is not None and (
                not isinstance(payload["metadata_status"], str)
                or payload["metadata_status"] not in LOCK_METADATA_STATUSES
            ))
            or (payload["quarantined"] is not None and not isinstance(payload["quarantined"], bool))):
        raise MinecraftTargetLockMetadataError("invalid inspection token")
    for name in ("root_identity", "lock_identity"):
        _validate_identity_pair(payload[name], absent=True)
    for name in ("metadata_digest", "history_digest", "pending_digest", "uncertainty_digest"):
        if payload[name] is not None and not _hex(payload[name], 64):
            raise MinecraftTargetLockMetadataError("invalid token digest")
    for name in ("writer_epoch", "transition_nonce"):
        if payload[name] is not None and not _hex(payload[name], 32):
            raise MinecraftTargetLockMetadataError("invalid token writer fence")
    if payload["revision"] is not None and not _counter(payload["revision"], minimum=1):
        raise MinecraftTargetLockMetadataError("invalid token revision")
    if payload["history_pointer"] is not None:
        _validate_pointer(payload["history_pointer"])
    if not isinstance(payload["orphans"], list) or len(payload["orphans"]) > 16:
        raise MinecraftTargetLockMetadataError("unbounded orphan census")
    for orphan in payload["orphans"]:
        _closed(orphan, {"name", "identity", "digest"})
        if (not _history_temp_name(orphan["name"], payload["lock_key"])
                or not _hex(orphan["digest"], 64)):
            raise MinecraftTargetLockMetadataError("invalid orphan token")
        _validate_identity_pair(orphan["identity"])
    _canonical_bytes(payload)


def _directory_chain(root: Path) -> tuple:
    chain = []
    for path in reversed((root, *root.parents)):
        current = os.lstat(path)
        if not stat.S_ISDIR(current.st_mode) or stat.S_ISLNK(current.st_mode):
            raise MinecraftTargetLockMetadataError("lock root/ancestor is not a no-follow directory")
        chain.append((path, current.st_dev, current.st_ino))
    return tuple(chain)


def _durable_root(root: Path) -> None:
    root = root.absolute()
    missing = []
    current = root
    while not current.exists():
        missing.append(current)
        current = current.parent
    _directory_chain(current)
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            pass
        _directory_chain(directory)
        _fsync_parent_directory(directory)
        _fsync_parent_directory(directory / ".entry")
    # Explicit reconciliation must not assume an existing but historically
    # unproven directory entry was durable. Re-establish each ancestor link.
    for directory, _dev, _ino in _directory_chain(root):
        _fsync_parent_directory(directory / ".entry")


class _HistoryIO:
    """Pinned root and retained .lock identity, with bounded no-follow I/O.

    The caller must own the lifecycle guard and appropriate retained flock.
    Before/after namespace checks are not protection against a hostile writer.
    We never replace or unlink the cooperating flocked .lock inode.
    """

    def __init__(self, root: Path, key: str, host: str, port: int, stream):
        self.root = Path(os.path.abspath(root))
        self.path = self.root / f"{key}.lock"
        self.key, self.host, self.port = key, host.casefold(), int(port)
        self.stream = stream
        self.chain = _directory_chain(self.root)
        self.root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        self.root_identity = self.chain[-1][1:]
        try:
            self.lock_identity = _verify_open_path_identity(stream, self.path) if stream is not None else None
            self.verify()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.root_fd is not None:
            os.close(self.root_fd)
            self.root_fd = None

    def verify_root(self):
        try:
            chain = _directory_chain(self.root)
        except OSError as exc:
            raise MinecraftTargetLockMetadataError("lock root identity unavailable") from exc
        if self.root_fd is None or chain != self.chain:
            raise MinecraftTargetLockMetadataError("lock root identity drift")
        opened = os.fstat(self.root_fd)
        if (opened.st_dev, opened.st_ino) != self.root_identity:
            raise MinecraftTargetLockMetadataError("lock root descriptor drift")

    def verify(self):
        self.verify_root()
        if self.stream is None:
            try:
                os.lstat(self.path)
            except FileNotFoundError:
                return
            raise MinecraftTargetLockMetadataError("absent lock namespace changed")
        _verify_retained_identity(self.stream, self.path, self.lock_identity)

    def sidepath(self, suffix):
        if suffix not in {"history", "history-clear-pending", "uncertain"}:
            raise ValueError("invalid target sidecar")
        return self.root / f"{self.key}.{suffix}"

    def _read_named(self, name: str) -> bytes | None:
        self.verify()
        try:
            before = os.stat(name, dir_fd=self.root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(before.st_mode):
            raise MinecraftTargetLockMetadataError("sidecar is not a regular no-follow file")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                     dir_fd=self.root_fd)
        try:
            opened = os.fstat(fd)
            raw = os.pread(fd, _MAX_RECORD_BYTES + 1, 0)
            after = os.stat(name, dir_fd=self.root_fd, follow_symlinks=False)
            if ((before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                    or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
                    or not stat.S_ISREG(after.st_mode)):
                raise MinecraftTargetLockMetadataError("sidecar identity drift")
            if len(raw) > _MAX_RECORD_BYTES:
                raise MinecraftTargetLockMetadataError("sidecar exceeds bounded size")
            self.verify()
            return raw
        finally:
            os.close(fd)

    def read(self, suffix):
        return self._read_named(self.sidepath(suffix).name)

    def metadata_raw(self):
        self.verify()
        if self.stream is None:
            return None
        raw = os.pread(self.stream.fileno(), _MAX_RECORD_BYTES + 1, 0)
        if len(raw) > _MAX_RECORD_BYTES:
            raise MinecraftTargetLockMetadataError("metadata exceeds bounded size")
        self.verify()
        return raw

    def history(self):
        raw = self.read("history")
        if raw is None:
            raise MinecraftTargetLockMetadataError("v3 history is unavailable")
        history = _validate_history(_strict_json(raw, canonical=True))
        if (history["lock_key"] != self.key or history["host"] != self.host
                or history["port"] != self.port or tuple(history["root_identity"]) != self.root_identity
                or tuple(history["lock_identity"]) != self.lock_identity):
            raise MinecraftTargetLockMetadataError("history root/lock/target identity mismatch")
        return history

    def publish(self, suffix, payload):
        self.verify()
        if suffix == "history":
            _validate_history(payload)
        elif suffix == "history-clear-pending":
            _validate_pending(payload)
        else:
            raise ValueError("history publisher cannot publish admission markers")
        raw = _canonical_bytes(payload)
        path = self.sidepath(suffix)
        temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
        fd = None
        identity = None
        try:
            fd = os.open(temporary.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=self.root_fd)
            opened = os.fstat(fd)
            identity = opened.st_dev, opened.st_ino
            if not stat.S_ISREG(opened.st_mode) or opened.st_dev != self.root_identity[0]:
                raise MinecraftTargetLockMetadataError("temporary sidecar device mismatch")
            _write_complete(fd, raw)
            os.fsync(fd)
            if os.pread(fd, _MAX_RECORD_BYTES + 1, 0) != raw or self._read_named(temporary.name) != raw:
                raise MinecraftTargetLockMetadataError("temporary sidecar readback mismatch")
            self.verify()
            try:
                existing = os.stat(path.name, dir_fd=self.root_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if ((existing is not None and existing.st_dev != opened.st_dev)
                    or os.stat(path.parent).st_dev != opened.st_dev):
                raise MinecraftTargetLockMetadataError("cross-device sidecar replacement")
            os.replace(temporary, path)
            installed = os.lstat(path)
            if (installed.st_dev, installed.st_ino) != identity or not stat.S_ISREG(installed.st_mode):
                raise MinecraftTargetLockMetadataError("installed sidecar identity mismatch")
            _fsync_parent_directory(path)
            if self.read(suffix) != raw:
                raise MinecraftTargetLockMetadataError("installed sidecar readback mismatch")
            self.verify()
        finally:
            if fd is not None:
                os.close(fd)
            # Only our exact private temp may be cleaned up. Unknown/orphan
            # temps are never silently adopted or removed by ordinary writers.
            if identity is not None:
                try:
                    current = os.stat(temporary.name, dir_fd=self.root_fd, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == identity:
                        os.unlink(temporary.name, dir_fd=self.root_fd)
                except OSError:
                    pass

    def sync_history(self, history):
        """Obtain a fresh positive durability witness for exact U-ahead adoption."""
        self.verify()
        fd = os.open(self.sidepath("history").name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.root_fd)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_parent_directory(self.sidepath("history"))
        if self.history() != history:
            raise MinecraftTargetLockMetadataError("U-ahead history changed during durability verification")

    def orphans(self):
        self.verify()
        result = []
        with os.scandir(self.root_fd) as entries:
            for entry in entries:
                if _history_temp_name(entry.name, self.key):
                    if len(result) == 16:
                        raise MinecraftTargetLockMetadataError("unbounded history orphan census")
                    raw = self._read_named(entry.name)
                    if raw is None:
                        raise MinecraftTargetLockMetadataError("orphan disappeared during census")
                    identity = entry.stat(follow_symlinks=False)
                    result.append({"name": entry.name, "identity": [identity.st_dev, identity.st_ino],
                                   "digest": _digest(raw)})
        self.verify()
        return sorted(result, key=lambda item: item["name"])


def _history_temp_name(name, key):
    return isinstance(name, str) and any(
        name.startswith(prefix) and _hex(name[len(prefix):], 32)
        for prefix in (f".{key}.history.tmp-", f".{key}.history-clear-pending.tmp-")
    )


def _write_complete(fd, raw):
    remaining = memoryview(raw)
    while remaining:
        try:
            written = os.write(fd, remaining)
        except InterruptedError:
            continue
        if written <= 0 or written > len(remaining):
            raise OSError("record write made invalid progress")
        remaining = remaining[written:]


def _new_history(io, epoch):
    return {
        "schema": _HISTORY_SCHEMA, "lock_key": io.key, "host": io.host, "port": io.port,
        "root_identity": list(io.root_identity), "lock_identity": list(io.lock_identity),
        "generation": uuid.uuid4().hex, "ordinal": 1, "state": "LEGACY_UNKNOWN",
        "writer_epoch": epoch,
        "source": {"metadata_digest": _digest(b""), "writer_epoch": None, "prior_pointer": None},
        "first": None, "latest": None, "observation_count": 0, "rolling_digest": _digest(b""),
        "acknowledged_diagnostics": None,
        "prior_acknowledged_diagnostics": None,
        "gaps": {"count": 0, "first_digest": None, "latest_digest": None,
                 "rolling_digest": _digest(b"")},
        "acknowledgement": None,
    }


def _add_gap(history, digest):
    gaps = history["gaps"]
    gaps["count"] = _increment(gaps["count"])
    gaps["first_digest"] = gaps["first_digest"] or digest
    gaps["latest_digest"] = digest
    gaps["rolling_digest"] = _digest((gaps["rolling_digest"] + digest).encode("ascii"))


def _observation(metadata, raw_digest):
    return {"attempt_id": metadata["attempt_id"], "acquired_at": metadata.get("acquired_at"),
            "pid": metadata["pid"], "source_digest": raw_digest}


def _record_owner(history, metadata, raw_digest):
    if history["state"] == "ACKNOWLEDGED_CLEAN":
        history["first"] = None
        history["latest"] = None
        history["observation_count"] = 0
        history["rolling_digest"] = _digest(b"")
    observation = _observation(metadata, raw_digest)
    if history["first"] is None:
        history["first"] = observation
    history["latest"] = observation
    history["observation_count"] = _increment(history["observation_count"])
    history["rolling_digest"] = _digest(
        (history["rolling_digest"] + _digest(_canonical_bytes(observation))).encode("ascii")
    )
    history["ordinal"] = _increment(history["ordinal"])
    history["state"] = "UNRESOLVED"


def _matching_history(io, metadata):
    history = io.history()
    if (metadata.get("history_pointer") != _history_pointer(history)
            or metadata.get("writer_epoch") != history["writer_epoch"]):
        raise MinecraftTargetLockMetadataError("history pointer/writer epoch mismatch")
    return history


def _prepare_history(io, previous, raw, *, repair_corrupt=False):
    """Positively durable nonclean preservation BEFORE metadata overwrite."""
    pending = io.read("history-clear-pending")
    orphans = io.orphans()
    source = {"metadata_digest": _digest(raw), "writer_epoch": previous.get("writer_epoch"),
              "prior_pointer": previous.get("history_pointer")}
    if previous.get("schema_version") == 3 and not repair_corrupt:
        if pending is not None or orphans:
            raise MinecraftTargetLockMetadataError("unfinished history transaction requires diagnosis")
        history = io.history()
        if previous["history_pointer"] != _history_pointer(history) or previous["writer_epoch"] != history["writer_epoch"]:
            if (previous.get("status") == "acquired" and history["state"] == "UNRESOLVED"
                    and history["source"] == source and history["writer_epoch"] == previous["writer_epoch"]
                    and history["generation"] == previous["history_pointer"]["generation"]
                    and history["ordinal"] == previous["history_pointer"]["ordinal"] + 1
                    and history["latest"] == _observation(previous, _digest(raw))):
                io.sync_history(history)
                return history
            raise MinecraftTargetLockMetadataError("history pointer/writer epoch mismatch")
        if previous.get("status") not in {"acquired", "quarantined"}:
            return history
    else:
        epoch = uuid.uuid4().hex  # Never revive an acknowledged or torn epoch.
        try:
            history = io.history()
        except MinecraftTargetLockMetadataError:
            history = _new_history(io, epoch)
        history["writer_epoch"] = epoch
        history["ordinal"] = _increment(history["ordinal"])
        if history["state"] != "UNRESOLVED":
            history["state"] = "LEGACY_UNKNOWN"
        _add_gap(history, _digest(raw))
        old_raw = io.read("history")
        if old_raw is not None:
            _add_gap(history, _digest(old_raw))
        if pending is not None:
            _add_gap(history, _digest(pending))
        for orphan in orphans:
            _add_gap(history, orphan["digest"])
    history["source"] = source
    if previous.get("status") in {"acquired", "quarantined"}:
        _record_owner(history, previous, _digest(raw))
    history = _seal(history)
    io.publish("history", history)
    io.verify()
    return history


def _stamp_metadata(payload, previous, history):
    epoch = history["writer_epoch"]
    revision = (_increment(previous["revision"]) if previous.get("schema_version") == 3
                and previous.get("writer_epoch") == epoch else 1)
    payload.update({"schema_version": 3, "writer_epoch": epoch, "revision": revision,
                    "transition_nonce": uuid.uuid4().hex, "history_pointer": _history_pointer(history)})
    payload.update(_seal(payload))
    return payload


def _parse_raw_metadata(io, raw):
    if raw is None or not raw.strip():
        return {}
    payload = _strict_json(raw)
    if payload.get("schema_version") == 3:
        _strict_json(raw, canonical=True)
    return _parse_lock_metadata(payload, expected_key=io.key, expected_host=io.host,
                                expected_port=io.port)


def _inspect_predecessor(io, *, active_owner=False):
    """Called only with a coherent retained SH/EX flock, or checked absence."""
    io.verify()
    raw = io.metadata_raw()
    history_raw = io.read("history")
    pending = io.read("history-clear-pending")
    uncertainty = io.read("uncertain")
    orphans = io.orphans()
    metadata, history, error = {}, None, None
    metadata_kind = "absent" if raw is None else "blank"
    try:
        metadata = _parse_raw_metadata(io, raw)
        if metadata:
            metadata_kind = "v3" if metadata["schema_version"] == 3 else "legacy"
    except MinecraftTargetLockMetadataError as exc:
        metadata_kind, error = "corrupt", str(exc)
    history_kind = "absent" if history_raw is None else "corrupt"
    if history_raw is not None:
        try:
            history, history_kind = io.history(), "valid"
        except MinecraftTargetLockMetadataError as exc:
            error = str(exc)
    state = {
        "schema": _TOKEN_SCHEMA, "lock_key": io.key, "host": io.host, "port": io.port,
        "root_path": str(io.root), "root_identity": list(io.root_identity),
        "lock_identity": list(io.lock_identity) if io.lock_identity is not None else None,
        "metadata_digest": _digest(raw) if raw is not None else None,
        "metadata_kind": metadata_kind, "metadata_status": metadata.get("status"),
        "writer_epoch": metadata.get("writer_epoch"), "revision": metadata.get("revision"),
        "transition_nonce": metadata.get("transition_nonce"),
        "history_digest": _digest(history_raw) if history_raw is not None else None,
        "history_pointer": _history_pointer(history) if history is not None else None,
        "history_kind": history_kind, "pending_digest": _digest(pending) if pending is not None else None,
        "uncertainty_digest": _digest(uncertainty) if uncertainty is not None else None,
        "quarantined": None if metadata_kind == "corrupt" else metadata.get("status") == "quarantined",
        "orphans": orphans,
    }
    token = MinecraftTargetPredecessorInspectionToken(state)
    status_type = MinecraftTargetPredecessorHistoryStatus
    first, latest = (history["first"], history["latest"]) if history else (None, None)
    if metadata_kind == "corrupt":
        status = status_type.HISTORY_CORRUPT
    elif metadata_kind != "v3":
        status = status_type.LEGACY_UNKNOWN
    elif history_kind == "absent":
        status = status_type.HISTORY_UNAVAILABLE
    elif history_kind == "corrupt":
        status = status_type.HISTORY_CORRUPT
    elif (metadata["history_pointer"] != _history_pointer(history)
          or metadata["writer_epoch"] != history["writer_epoch"]):
        status, error = status_type.HISTORY_CORRUPT, "history pointer/writer epoch mismatch"
    else:
        status = status_type(history["state"])
        if status is status_type.ACKNOWLEDGED_CLEAN:
            ack = history["acknowledgement"]
            if (ack["writer_epoch"] != metadata["writer_epoch"]
                    or (metadata["status"] == "reconciled"
                        and metadata["reconciled_at"] != ack["acknowledged_at"])):
                status = status_type.HISTORY_CORRUPT
            elif uncertainty is not None or state["quarantined"]:
                status = status_type.DURABILITY_UNCERTAIN
        # An abandoned current acquisition is unresolved independently of PID.
        # Include its detached diagnostic witness without mutating persistence.
        if not active_owner and metadata.get("status") == "acquired":
            status = status_type.UNRESOLVED
            latest = _observation(metadata, _digest(raw))
            first = first or latest
    if pending is not None:
        status = status_type.DURABILITY_UNCERTAIN
    elif orphans:
        status = status_type.AMBIGUOUS
    io.verify()
    # Re-read every bound byte before returning. Cooperative writers are
    # serialized by flock. This also rejects observed noncooperating drift.
    if (raw != io.metadata_raw() or history_raw != io.read("history")
            or pending != io.read("history-clear-pending") or uncertainty != io.read("uncertain")
            or orphans != io.orphans()):
        raise MinecraftTargetLockMetadataError("inspection bytes changed during observation")
    return MinecraftTargetPredecessorSnapshot(
        status=status, first=first, latest=latest, gaps=history["gaps"] if history else None,
        acknowledgement=history["acknowledgement"] if history else None,
        acknowledged_diagnostics=history["acknowledged_diagnostics"] if history else None,
        prior_acknowledged_diagnostics=history["prior_acknowledged_diagnostics"] if history else None,
        generation=history["generation"] if history else None,
        ordinal=history["ordinal"] if history else None,
        digest=history["checksum"] if history else None,
        observation_count=history["observation_count"] if history else None,
        rolling_digest=history["rolling_digest"] if history else None,
        writer_epoch=metadata.get("writer_epoch"), revision=metadata.get("revision"),
        transition_nonce=metadata.get("transition_nonce"), root_identity=io.root_identity,
        lock_identity=io.lock_identity,
        current_owner=metadata if metadata.get("status") in {"acquired", "quarantined"} else None,
        active_owner=active_owner, quarantined=state["quarantined"],
        uncertain=uncertainty is not None, token=token, error=error,
    )


def _absent_root_snapshot(key, host, port, root):
    state = {
        "schema": _TOKEN_SCHEMA, "lock_key": key, "host": host.casefold(), "port": int(port),
        "root_path": os.path.abspath(root), "root_identity": None, "lock_identity": None,
        "metadata_digest": None, "metadata_kind": "absent", "metadata_status": None,
        "writer_epoch": None, "revision": None, "transition_nonce": None, "history_digest": None,
        "history_pointer": None, "history_kind": "absent", "pending_digest": None,
        "uncertainty_digest": None, "quarantined": False, "orphans": [],
    }
    return MinecraftTargetPredecessorSnapshot(
        MinecraftTargetPredecessorHistoryStatus.LEGACY_UNKNOWN, active_owner=False, quarantined=False,
        token=MinecraftTargetPredecessorInspectionToken(state),
    )


def read_minecraft_target_predecessor_status(
    *, lock_root, host, port,
    storage_qualification: MinecraftTargetStorageQualification | None = None,
):
    """Non-mutating SH-flock observation on the EXISTING stable lock inode.

    A positive CLEAN result additionally requires a currently valid storage
    qualification receipt bound to this root and boot.
    """
    root = Path(lock_root)
    key = minecraft_target_lock_key(host=host, port=port)
    path = root / f"{key}.lock"
    with _lifecycle_guard_for(path):
        stream = io = None
        locked = False
        try:
            try:
                _directory_chain(root.absolute())
            except FileNotFoundError:
                # No existing inode to flock. This absence token cannot claim
                # clean and is revalidated before any exclusive provisioning.
                absent = _absent_root_snapshot(key, host, port, root)
                return _qualified_observation(absent, storage_qualification, root, None)
            try:
                before = os.lstat(path)
            except FileNotFoundError:
                io = _HistoryIO(root, key, host, port, None)
                return _qualified_observation(_inspect_predecessor(io), storage_qualification, root, io)
            if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
                raise MinecraftTargetLockMetadataError("existing lock is not a regular no-follow file")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
            stream = os.fdopen(fd, "r", encoding="utf-8")
            identity = _verify_open_path_identity(stream, path)
            if identity != (before.st_dev, before.st_ino):
                raise MinecraftTargetLockMetadataError("offline lock identity drift")
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                busy = MinecraftTargetPredecessorSnapshot(
                    MinecraftTargetPredecessorHistoryStatus.AMBIGUOUS, active_owner=True,
                    error="existing lock flock is busy", lock_identity=identity,
                )
                return _qualified_observation(busy, storage_qualification, root, None)
            io = _HistoryIO(root, key, host, port, stream)
            return _qualified_observation(_inspect_predecessor(io), storage_qualification, root, io)
        except (OSError, MinecraftTargetLockError, ValueError) as exc:
            status = (MinecraftTargetPredecessorHistoryStatus.HISTORY_CORRUPT
                      if "bounded size" in str(exc) else MinecraftTargetPredecessorHistoryStatus.AMBIGUOUS)
            return MinecraftTargetPredecessorSnapshot(status, error=str(exc))
        finally:
            if io is not None:
                io.close()
            if stream is not None:
                if locked:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                stream.close()


def _acknowledgement_prefix(io, previous, raw, snapshot):
    try:
        history = io.history()
    except MinecraftTargetLockMetadataError:
        history = _new_history(io, uuid.uuid4().hex)
    history["source"] = {"metadata_digest": _digest(raw), "writer_epoch": previous.get("writer_epoch"),
                         "prior_pointer": previous.get("history_pointer")}
    state = snapshot.token.state
    if (state["metadata_kind"] != "v3" or state["history_kind"] != "valid"
            or previous.get("history_pointer") != _history_pointer(history)
            or state["pending_digest"] is not None or state["orphans"]):
        for digest in (state["metadata_digest"], state["history_digest"], state["pending_digest"]):
            if digest is not None:
                _add_gap(history, digest)
        for orphan in state["orphans"]:
            _add_gap(history, orphan["digest"])
    if previous.get("status") in {"acquired", "quarantined"}:
        _record_owner(history, previous, _digest(raw))
    return _seal(history)


def acknowledge_minecraft_target_predecessor(
    *, lock_root, host, port, expected, acknowledge_target_safe, acknowledge_whole_prefix,
    reason, operator, storage_qualification=None, reconcile_unknown_history=False,
):
    """Explicit whole-prefix reconciliation, NOT release or quarantine clear.

    The caller supplies externally established target safety. Intent flags and
    audit strings are not authentication. Outcomes never grant execution.
    """
    if acknowledge_target_safe is not True or acknowledge_whole_prefix is not True:
        raise ValueError("predecessor acknowledgement requires explicit target-safe whole-prefix intent")
    if not _is_non_empty_string(reason) or not _is_non_empty_string(operator):
        raise ValueError("predecessor acknowledgement requires nonempty reason and operator")
    if not isinstance(expected, MinecraftTargetPredecessorInspectionToken):
        raise ValueError("predecessor acknowledgement requires a detached inspected CAS token")
    if not isinstance(reconcile_unknown_history, bool):
        raise ValueError("unknown-history election must be a bool")
    if not isinstance(storage_qualification, MinecraftTargetStorageQualification):
        raise ValueError("predecessor acknowledgement requires a validated storage qualification")
    root = Path(lock_root)
    key = minecraft_target_lock_key(host=host, port=port)
    path = root / f"{key}.lock"
    with _lifecycle_guard_for(path):
        stream = io = None
        locked = retained = mutation_started = False
        pending = None
        snapshot = read_minecraft_target_predecessor_status(
            lock_root=root, host=host, port=port, storage_qualification=storage_qualification
        )
        try:
            _validate_storage_qualification(storage_qualification, root)
            if (expected.state["lock_key"] != key or expected.state["host"] != host.casefold()
                    or expected.state["port"] != int(port)
                    or expected.state["root_path"] != os.path.abspath(root)):
                raise MinecraftTargetLockMetadataError("CAS token belongs to another target")
            absent = expected.state["lock_identity"] is None
            if absent:
                if snapshot.token != expected:
                    raise MinecraftTargetLockMetadataError("absent-state CAS changed")
                if not reconcile_unknown_history:
                    raise MinecraftTargetLockMetadataError("explicit unknown-history reconciliation required")
                _durable_root(root)
                stream = _open_lock_stream(path, exclusive_create=True)
            else:
                # Never create a missing/replaced inode for an existing token.
                before = os.lstat(path)
                if (before.st_dev, before.st_ino) != tuple(expected.state["lock_identity"]):
                    raise MinecraftTargetLockMetadataError("CAS lock inode changed")
                stream = _open_lock_stream(path)
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError as exc:
                raise MinecraftTargetLockBusyError("target owner is active", reason="busy") from exc
            io = _HistoryIO(root, key, host, port, stream)
            _validate_storage_qualification(storage_qualification, root, io)
            snapshot = _inspect_predecessor(io)
            actual = _thaw(snapshot.token.state)
            if absent:
                if io.metadata_raw() != b"":
                    raise MinecraftTargetLockMetadataError("provisioned absent lock is not virgin")
                actual.update({"lock_identity": None, "metadata_digest": None, "metadata_kind": "absent"})
                if expected.state["root_identity"] is None:
                    actual["root_identity"] = None
            if actual != _thaw(expected.state):
                raise MinecraftTargetLockMetadataError("exact inspected CAS state changed")
            if snapshot.uncertain or snapshot.quarantined:
                raise MinecraftTargetLockMetadataError("independent quarantine/uncertainty must be cleared separately")
            if snapshot.token.state["metadata_kind"] == "corrupt":
                raise MinecraftTargetLockMetadataError("corrupt metadata requires separate force-corrupt clear first")
            if (snapshot.status not in {MinecraftTargetPredecessorHistoryStatus.UNRESOLVED,
                                        MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN}
                    and not reconcile_unknown_history):
                raise MinecraftTargetLockMetadataError("explicit unknown-history reconciliation required")
            raw = io.metadata_raw()
            previous = _parse_raw_metadata(io, raw)
            # For virgin provisioning, the inspected state was ABSENT, not the
            # newly created blank inode. Never synthesize its covered raw digest.
            inspected = expected.state
            inspected_history = inspected["history_pointer"]
            prefix = _acknowledgement_prefix(io, previous, raw, snapshot)
            epoch = uuid.uuid4().hex
            unflocked = (_observation(previous, _digest(raw))
                         if previous.get("status") == "acquired" and not snapshot.active_owner else None)
            current_diagnostics = {
                "first": prefix["first"], "latest": prefix["latest"],
                "observation_count": prefix["observation_count"],
                "rolling_digest": prefix["rolling_digest"],
            }
            last_acknowledged = prefix["acknowledged_diagnostics"]
            prior_diagnostics = prefix["prior_acknowledged_diagnostics"]
            if current_diagnostics["observation_count"] == 0:
                diagnostics = last_acknowledged
            else:
                diagnostics = current_diagnostics
                if last_acknowledged is not None:
                    prior_diagnostics = _merge_diagnostics(prior_diagnostics, last_acknowledged)
            clean = dict(prefix)
            clean["acknowledged_diagnostics"] = diagnostics
            clean["prior_acknowledged_diagnostics"] = prior_diagnostics
            clean.update({"ordinal": _increment(prefix["ordinal"]), "state": "ACKNOWLEDGED_CLEAN",
                          "first": None, "latest": None, "observation_count": 0,
                          "rolling_digest": _digest(b""), "writer_epoch": epoch,
                          "acknowledgement": {
                              "operator": operator.strip(), "reason": reason.strip(),
                              "acknowledged_at": time.time(),
                              "covered_generation": inspected_history["generation"] if inspected_history else None,
                              "covered_ordinal": inspected_history["ordinal"] if inspected_history else None,
                              "covered_digest": inspected_history["digest"] if inspected_history else None,
                              "covered_metadata_digest": inspected["metadata_digest"],
                              "covered_metadata_kind": inspected["metadata_kind"],
                              "covered_writer_epoch": inspected["writer_epoch"],
                              "covered_revision": inspected["revision"],
                              "covered_transition_nonce": inspected["transition_nonce"],
                              "covered_unflocked_acquired_owner": unflocked,
                              "whole_prefix": True, "writer_epoch": epoch,
                              "reconcile_unknown_history": reconcile_unknown_history,
                              "storage_profile_id": storage_qualification.storage_profile_id,
                              "storage_profile_version": storage_qualification.storage_profile_version,
                          }})
            clean = _seal(clean)
            _validate_history(clean)
            expected_coverage = (clean["acknowledgement"]["covered_generation"],
                                 clean["acknowledgement"]["covered_ordinal"],
                                 clean["acknowledgement"]["covered_digest"],
                                 clean["acknowledgement"]["covered_metadata_digest"],
                                 clean["acknowledgement"]["covered_writer_epoch"],
                                 clean["acknowledgement"]["covered_metadata_kind"],
                                 clean["acknowledgement"]["covered_revision"],
                                 clean["acknowledgement"]["covered_transition_nonce"],
                                 clean["acknowledgement"]["covered_unflocked_acquired_owner"])
            pending = _seal({"schema": _PENDING_SCHEMA, "transaction_id": uuid.uuid4().hex,
                              "expected": _thaw(expected.state)})
            mutation_started = True
            io.publish("history-clear-pending", pending)
            io.publish("history", clean)
            reconciled = {"schema_version": 3, "status": "reconciled", "lock_key": key,
                          "host": io.host, "port": int(port),
                          "reconciled_at": clean["acknowledgement"]["acknowledged_at"]}
            _stamp_metadata(reconciled, previous, clean)
            _parse_lock_metadata(reconciled, expected_key=key, expected_host=host, expected_port=int(port))
            io.verify()
            _write_stream_metadata(stream, reconciled)
            installed = _parse_raw_metadata(io, io.metadata_raw())
            actual_clean = _matching_history(io, installed)
            actual_ack = actual_clean["acknowledgement"]
            actual_coverage = (actual_ack["covered_generation"], actual_ack["covered_ordinal"],
                               actual_ack["covered_digest"], actual_ack["covered_metadata_digest"],
                               actual_ack["covered_writer_epoch"],
                               actual_ack["covered_metadata_kind"], actual_ack["covered_revision"],
                               actual_ack["covered_transition_nonce"],
                               actual_ack["covered_unflocked_acquired_owner"])
            if installed != reconciled or actual_clean != clean or actual_coverage != expected_coverage:
                raise MinecraftTargetLockMetadataError("reconciled pair exact readback mismatch")
            io.verify()
            if io.read("history-clear-pending") != _canonical_bytes(pending):
                raise MinecraftTargetLockMetadataError("pending-clear transaction changed")
            # Explicit unknown reconciliation diagnoses exactly the inspected
            # orphan prefix. It does not clean arbitrary temps by glob.
            for orphan in expected.state["orphans"]:
                current = os.stat(orphan["name"], dir_fd=io.root_fd, follow_symlinks=False)
                if ((current.st_dev, current.st_ino) != tuple(orphan["identity"])
                        or _digest(io._read_named(orphan["name"])) != orphan["digest"]):
                    raise MinecraftTargetLockMetadataError("inspected orphan changed")
                os.unlink(orphan["name"], dir_fd=io.root_fd)
            io.verify()
            os.unlink(io.sidepath("history-clear-pending").name, dir_fd=io.root_fd)
            _fsync_parent_directory(io.sidepath("history-clear-pending"))
            snapshot = _qualified_observation(
                _inspect_predecessor(io), storage_qualification, root, io
            )
            if snapshot.status is not MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN:
                raise MinecraftTargetLockMetadataError("final coherent acknowledgement not clean")
            return MinecraftTargetPredecessorAcknowledgementOutcome(
                MinecraftTargetPredecessorAcknowledgementStatus.ACKNOWLEDGED, snapshot
            )
        except BaseException as exc:
            if mutation_started and io is not None and pending is not None:
                try:
                    # Includes final pending-unlink directory-fsync uncertainty.
                    # Keep EX until the pending guard has a positive durable
                    # publication, or retain it for process lifetime as fallback.
                    io.publish("history-clear-pending", pending)
                except BaseException:
                    _UNVERIFIED_ACK_LOCKS[pending["transaction_id"]] = io
                    retained = True
            if io is not None:
                try:
                    snapshot = _inspect_predecessor(io)
                except (OSError, MinecraftTargetLockError, ValueError):
                    pass
            if not isinstance(exc, Exception):
                raise
            return MinecraftTargetPredecessorAcknowledgementOutcome(
                MinecraftTargetPredecessorAcknowledgementStatus.UNCERTAIN,
                replace(snapshot, status=MinecraftTargetPredecessorHistoryStatus.DURABILITY_UNCERTAIN),
                error=f"{type(exc).__name__}: {exc}", retained_lock=retained
            )
        finally:
            if not retained:
                if io is not None:
                    io.close()
                if stream is not None:
                    if locked:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                    stream.close()


def _current_boot_id() -> str | None:
    provider = _BOOT_ID_PROVIDER
    if provider is not None:
        try:
            value = provider()
        except Exception:
            return None
    else:
        try:
            value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return None
    return value if _is_bounded_string(value) else None


def _is_bounded_string(value: object, maximum: int = 256) -> bool:
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= maximum
            and "\x00" not in value)


def _read_storage_receipt(path: Path):
    requested_path = Path(os.path.abspath(path))
    try:
        requested_stat = os.lstat(requested_path)
    except OSError as exc:
        raise MinecraftTargetLockMetadataError("storage qualification receipt is unavailable") from exc
    if (not stat.S_ISREG(requested_stat.st_mode) or stat.S_ISLNK(requested_stat.st_mode)
            or requested_stat.st_mode & 0o022 or requested_stat.st_uid not in {os.geteuid(), 0}):
        raise MinecraftTargetLockMetadataError("storage qualification receipt permissions are invalid")
    try:
        path = requested_path.parent.resolve(strict=True) / requested_path.name
        before = os.lstat(path)
    except OSError as exc:
        raise MinecraftTargetLockMetadataError("storage qualification receipt parent is unavailable") from exc
    if ((before.st_dev, before.st_ino) != (requested_stat.st_dev, requested_stat.st_ino)
            or not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)):
        raise MinecraftTargetLockMetadataError("storage qualification receipt path is not canonical")
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_mode & 0o022
                or opened.st_uid not in {os.geteuid(), 0}
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise MinecraftTargetLockMetadataError("storage qualification receipt identity drift")
        if opened.st_size > _MAX_RECORD_BYTES:
            raise MinecraftTargetLockMetadataError("storage qualification receipt exceeds bounded size")
        raw = os.pread(fd, _MAX_RECORD_BYTES + 1, 0)
        if len(raw) > _MAX_RECORD_BYTES:
            raise MinecraftTargetLockMetadataError("storage qualification receipt exceeds bounded size")
        after_fd = os.fstat(fd)
        after_path = os.lstat(path)
        if ((after_fd.st_dev, after_fd.st_ino) != (opened.st_dev, opened.st_ino)
                or (after_path.st_dev, after_path.st_ino) != (opened.st_dev, opened.st_ino)
                or not stat.S_ISREG(after_path.st_mode)
                or after_fd.st_mode & 0o022 or after_path.st_mode & 0o022
                or after_fd.st_uid not in {os.geteuid(), 0}
                or after_path.st_uid not in {os.geteuid(), 0}
                or len(raw) != opened.st_size or after_fd.st_size != opened.st_size
                or after_path.st_size != opened.st_size
                or after_fd.st_mtime_ns != opened.st_mtime_ns
                or after_path.st_mtime_ns != opened.st_mtime_ns):
            raise MinecraftTargetLockMetadataError("storage qualification receipt changed while reading")
    except OSError as exc:
        raise MinecraftTargetLockMetadataError("storage qualification receipt could not be read safely") from exc
    finally:
        if fd is not None:
            os.close(fd)
    payload = _strict_json(raw, canonical=True)
    fields = {
        "artifact_id", "artifact_version", "storage_profile_id", "storage_profile_version",
        "receipt_id", "boot_id", "qualified_root_absolute_path", "qualified_root_dev",
        "qualified_root_ino", "qualified_filesystem_device", "capabilities", "issuer_audit_id",
        "detached_artifact_sha256",
    }
    _closed(payload, fields)
    digest_payload = {key: value for key, value in payload.items() if key != "detached_artifact_sha256"}
    detached_digest = _digest(_canonical_bytes(digest_payload))
    capabilities = payload["capabilities"]
    root_text = payload["qualified_root_absolute_path"]
    if (payload["artifact_id"] != _STORAGE_ARTIFACT_ID
            or not _is_positive_int(payload["artifact_version"])
            or payload["artifact_version"] != _STORAGE_ARTIFACT_VERSION
            or not _is_bounded_string(payload["storage_profile_id"])
            or not _is_positive_int(payload["storage_profile_version"])
            or not _is_bounded_string(payload["receipt_id"])
            or not _is_bounded_string(payload["boot_id"])
            or not isinstance(root_text, str) or not os.path.isabs(root_text)
            or os.path.normpath(root_text) != root_text
            or not _counter(payload["qualified_root_dev"])
            or not _is_positive_int(payload["qualified_root_ino"])
            or not _counter(payload["qualified_filesystem_device"])
            or not isinstance(capabilities, list) or len(capabilities) != len(_STORAGE_CAPABILITIES)
            or not all(isinstance(item, str) for item in capabilities)
            or len(set(capabilities)) != len(capabilities)
            or frozenset(capabilities) != _STORAGE_CAPABILITIES
            or not _is_bounded_string(payload["issuer_audit_id"])
            or payload["detached_artifact_sha256"] != detached_digest):
        raise MinecraftTargetLockMetadataError("storage qualification receipt schema/digest is invalid")
    current_boot = _current_boot_id()
    if current_boot is None or payload["boot_id"] != current_boot:
        raise MinecraftTargetLockMetadataError("storage qualification boot identity is unavailable or stale")
    root = Path(root_text)
    try:
        root_stat = os.lstat(root)
        canonical_root = str(root.resolve(strict=True))
    except OSError as exc:
        raise MinecraftTargetLockMetadataError("qualified storage root is unavailable") from exc
    if (not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode)
            or canonical_root != root_text
            or root_stat.st_dev != payload["qualified_root_dev"]
            or root_stat.st_ino != payload["qualified_root_ino"]
            or root_stat.st_dev != payload["qualified_filesystem_device"]):
        raise MinecraftTargetLockMetadataError("qualified storage root identity/device mismatch")
    return payload, (opened.st_dev, opened.st_ino), _digest(raw), path


def load_minecraft_target_storage_qualification(receipt_path) -> MinecraftTargetStorageQualification:
    path = Path(receipt_path)
    payload, identity, file_digest, canonical_path = _read_storage_receipt(path)
    return MinecraftTargetStorageQualification(
        artifact_id=payload["artifact_id"], artifact_version=payload["artifact_version"],
        storage_profile_id=payload["storage_profile_id"],
        storage_profile_version=payload["storage_profile_version"], receipt_id=payload["receipt_id"],
        boot_id=payload["boot_id"], qualified_root_absolute_path=payload["qualified_root_absolute_path"],
        qualified_root_dev=payload["qualified_root_dev"], qualified_root_ino=payload["qualified_root_ino"],
        qualified_filesystem_device=payload["qualified_filesystem_device"],
        capabilities=frozenset(payload["capabilities"]), issuer_audit_id=payload["issuer_audit_id"],
        detached_artifact_sha256=payload["detached_artifact_sha256"], receipt_path=canonical_path,
        receipt_identity=identity, receipt_file_digest=file_digest,
        _validation_marker=_STORAGE_VALIDATION_MARKER,
    )


def _validate_storage_qualification(qualification, lock_root, io=None):
    if not isinstance(qualification, MinecraftTargetStorageQualification):
        raise MinecraftTargetLockMetadataError("validated storage qualification is required")
    try:
        current = load_minecraft_target_storage_qualification(qualification.receipt_path)
    except (OSError, ValueError, MinecraftTargetLockError) as exc:
        raise MinecraftTargetLockMetadataError("storage qualification receipt is no longer valid") from exc
    if current != qualification:
        raise MinecraftTargetLockMetadataError("storage qualification receipt was replaced or changed")
    root = Path(os.path.abspath(lock_root))
    if str(root) != qualification.qualified_root_absolute_path:
        raise MinecraftTargetLockMetadataError("storage qualification root path mismatch")
    chain = _directory_chain(root)
    actual = chain[-1][1:]
    if (actual != (qualification.qualified_root_dev, qualification.qualified_root_ino)
            or actual[0] != qualification.qualified_filesystem_device):
        raise MinecraftTargetLockMetadataError("storage qualification root identity/device drift")
    if io is not None:
        io.verify()
        if io.root_identity != actual:
            raise MinecraftTargetLockMetadataError("qualified history root identity mismatch")
        if io.lock_identity is not None and io.lock_identity[0] != qualification.qualified_filesystem_device:
            raise MinecraftTargetLockMetadataError("qualified lock device mismatch")
        root_fd_stat = os.fstat(io.root_fd)
        if root_fd_stat.st_dev != qualification.qualified_filesystem_device:
            raise MinecraftTargetLockMetadataError("qualified root descriptor device mismatch")
        for suffix in ("history", "history-clear-pending", "uncertain"):
            entry = io.read(suffix)
            if entry is not None:
                current_entry = os.stat(io.sidepath(suffix), follow_symlinks=False)
                if current_entry.st_dev != qualification.qualified_filesystem_device:
                    raise MinecraftTargetLockMetadataError("qualified sidecar device mismatch")
    return current


def _qualified_observation(snapshot, qualification, root, io):
    clean = snapshot.status is MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN
    if qualification is None and not clean:
        return snapshot
    try:
        _validate_storage_qualification(qualification, root, io)
        if clean:
            ack = snapshot.acknowledgement
            if (ack is None or ack["storage_profile_id"] != qualification.storage_profile_id
                    or ack["storage_profile_version"] != qualification.storage_profile_version):
                raise MinecraftTargetLockMetadataError("acknowledgement storage profile does not match receipt")
    except (OSError, ValueError, MinecraftTargetLockError) as exc:
        return replace(
            snapshot,
            status=(MinecraftTargetPredecessorHistoryStatus.AMBIGUOUS if clean else snapshot.status),
            error=f"storage qualification unavailable: {exc}",
        )
    return snapshot
