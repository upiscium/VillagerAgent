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
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


LOCK_METADATA_SCHEMA_VERSION = 2
LOCK_METADATA_STATUSES = frozenset({"acquired", "released", "quarantined", "cleared"})

_LIFECYCLE_LOCKS_GUARD = threading.Lock()
_LIFECYCLE_LOCKS: dict[str, threading.RLock] = {}
# A release whose uncertainty marker could not be durably persisted must keep
# its flock alive for the remainder of this process where possible.  This
# deliberate strong reference prevents ordinary object collection from
# silently turning that unresolved result into an unblocked target.
_UNVERIFIED_RELEASE_LOCKS: dict[int, object] = {}


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
                self.lock_root.mkdir(parents=True, exist_ok=True)
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
                    _persist_uncertainty_marker(
                        self.lock_root / f"{self.key}.uncertain",
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
                marker = self.lock_root / f"{self.key}.uncertain"
                uncertain, uncertainty = _uncertainty_marker_state(marker)
                if uncertain:
                    raise MinecraftTargetLockUnavailableError(
                        "Minecraft target lock has unresolved uncertainty",
                        reason="uncertain",
                        owner=uncertainty,
                    )
                if not self.quarantined:
                    if not _persist_uncertainty_marker(
                        marker,
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
            uncertainty_persisted = _persist_uncertainty_marker(
                marker,
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
            self._stream.seek(0)
            content = self._stream.read()
        except UnicodeError as exc:
            raise MinecraftTargetLockMetadataError(
                "Minecraft target lock metadata encoding is invalid"
            ) from exc
        if not content.strip():
            return {}
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
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
        self._stream.seek(0)
        self._stream.truncate()
        json.dump(payload, self._stream, indent=2)
        self._stream.write("\n")
        self._stream.flush()
        os.fsync(self._stream.fileno())
        _verify_retained_identity(self._stream, self.path, self._lease_identity)

    def _read_contention_owner_snapshot(self) -> dict:
        if self._stream is None:
            return {}
        try:
            self._stream.seek(0)
            content = self._stream.read()
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


def _open_lock_stream(path: Path):
    try:
        current = os.lstat(path)
    except FileNotFoundError:
        current = None
    if current is not None and (
        stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode)
    ):
        raise OSError("Minecraft target lock path is not a regular file")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o666)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("Minecraft target lock descriptor is not a regular file")
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
                raw = stream.read()
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
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
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
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path.parent, flags)
    try:
        fd_stat = os.fstat(fd)
        path_stat = os.stat(path.parent)
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
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = None
            content = stream.read()
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
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
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
        root.mkdir(parents=True, exist_ok=True)
        stream = None
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
            stream.seek(0)
            content = stream.read()
            previous = {}
            if content.strip():
                try:
                    previous = _parse_lock_metadata(
                        json.loads(content),
                        expected_key=key,
                        expected_host=host,
                        expected_port=int(port),
                    )
                except (json.JSONDecodeError, MinecraftTargetLockMetadataError) as exc:
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
            _write_stream_metadata(stream, cleared)
            stream.seek(0)
            written_content = stream.read()
            written = _parse_lock_metadata(
                json.loads(written_content),
                expected_key=key,
                expected_host=host,
                expected_port=int(port),
            )
            if written != cleared:
                raise MinecraftTargetLockMetadataError(
                    "cleared Minecraft target metadata could not be verified"
                )
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
        or schema_version not in {1, LOCK_METADATA_SCHEMA_VERSION}
    ):
        raise MinecraftTargetLockMetadataError("Minecraft target lock metadata schema is unsupported")
    _validate_metadata_identity(
        payload,
        expected_key=expected_key,
        expected_host=expected_host,
        expected_port=expected_port,
    )
    if schema_version == 1:
        return _validate_schema_v1_metadata(payload)
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
    if not isinstance(status, str) or status not in LOCK_METADATA_STATUSES:
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
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _write_stream_metadata(stream, payload: dict) -> None:
    stream.seek(0)
    stream.truncate()
    json.dump(payload, stream, indent=2)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())


def _public_quarantine_history(metadata: dict) -> dict:
    if metadata.get("status") != "quarantined":
        return {}
    return {
        "attempt_id": metadata.get("attempt_id"),
        "run_name": metadata.get("run_name"),
        "quarantined_at": metadata.get("quarantined_at"),
        "reasons": list(metadata.get("reasons", [])),
    }
