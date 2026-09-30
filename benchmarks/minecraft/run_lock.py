from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
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
from typing import Any


LOCK_METADATA_SCHEMA_VERSION = 2
LOCK_METADATA_STATUSES = frozenset({"acquired", "released", "quarantined", "cleared"})


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
        if not math.isfinite(poll_interval_seconds) or poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be finite and non-negative")
        self.lock_root = Path(lock_root)
        self.key = minecraft_target_lock_key(host=host, port=port)
        self.path = self.lock_root / f"{self.key}.lock"
        self._name_path = self.lock_root / f"{self.key}.guard"
        self.host = host
        self.port = int(port)
        self.world_id = world_id
        self.attempt_id = attempt_id
        self.timeout_seconds = float(timeout_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.acquired = False
        self.quarantined = False
        self._quarantine_uncertain = False
        self.quarantine_record = None
        self.stale_owner_detected = False
        self._stream = None
        self._name_stream = None
        self.release_succeeded: bool | None = None

        self._lifecycle_lock = threading.RLock()

    @contextmanager
    def lifecycle_guard(self):
        """Serialize lease observation and release in this process."""
        with self._lifecycle_lock:
            yield

    def acquire(self) -> "MinecraftTargetLock":
        with self._lifecycle_lock:
            return self._acquire_locked()

    def _acquire_locked(self) -> "MinecraftTargetLock":
        deadline = time.monotonic() + self.timeout_seconds
        self.release_succeeded = None
        try:
            self.lock_root.mkdir(parents=True, exist_ok=True)
            self._name_stream = _open_lock_stream(self._name_path, writable=True)
            while True:
                try:
                    fcntl.flock(
                        self._name_stream.fileno(),
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        try:
                            self._stream = _open_lock_stream(self.path, writable=False)
                            owner = self._read_contention_owner_snapshot()
                        except OSError:
                            owner = {}
                        self._close_failed_acquire(unlock=False)
                        message = f"Minecraft target {self.host}:{self.port} is busy"
                        if owner.get("attempt_id"):
                            message += f" with attempt {owner['attempt_id']}"
                        raise MinecraftTargetLockBusyError(
                            message,
                            reason="busy",
                            owner=owner,
                        ) from exc
                    time.sleep(self.poll_interval_seconds)
            if not self._name_path_matches_stream():
                self._close_failed_acquire(unlock=True)
                raise MinecraftTargetLockMetadataError(
                    "Minecraft target lock guard path changed before admission"
                )
            uncertain = self._read_uncertain_marker()
            if uncertain is not None:
                self._close_failed_acquire(unlock=True)
                raise MinecraftTargetQuarantinedError(
                    f"Minecraft target {self.host}:{self.port} is quarantined",
                    quarantine=uncertain,
                )
            self._stream = _open_lock_stream(self.path, writable=True)
        except OSError as exc:
            self._close_failed_acquire(unlock=False)
            raise self._unavailable_error() from exc
        except BaseException:
            self._close_failed_acquire(unlock=False)
            raise
        if (
            not self._retained_path_matches_stream()
            or not self._name_path_matches_stream()
        ):
            self._close_failed_acquire(unlock=False)
            raise MinecraftTargetLockMetadataError(
                "Minecraft target lock path no longer names the opened lease"
            )
        while True:
            try:
                fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
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
                time.sleep(self.poll_interval_seconds)
            except OSError as exc:
                self._close_failed_acquire(unlock=False)
                raise self._unavailable_error() from exc

        try:
            if (
                not self._retained_path_matches_stream()
                or not self._name_path_matches_stream()
            ):
                raise MinecraftTargetLockMetadataError(
                    "Minecraft target lock path changed before acquisition"
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
            if (
                not self._retained_path_matches_stream()
                or not self._name_path_matches_stream()
            ):
                raise MinecraftTargetLockMetadataError(
                    "Minecraft target lock path changed before acquisition write"
                )
            self._write_metadata(acquired_metadata)
            if (
                not self._retained_path_matches_stream()
                or not self._name_path_matches_stream()
            ):
                raise MinecraftTargetLockMetadataError(
                    "Minecraft target lock path changed during acquisition"
                )
        except OSError as exc:
            self._close_failed_acquire(unlock=True)
            raise self._unavailable_error() from exc
        except BaseException:
            self._close_failed_acquire(unlock=True)
            raise
        self.acquired = True
        return self

    def quarantine(
        self,
        *,
        run_name: str,
        reasons: tuple[str, ...] | list[str],
        diagnostics: dict,
    ) -> dict:
        with self._lifecycle_lock:
            return self._quarantine_locked(
                run_name=run_name, reasons=reasons, diagnostics=diagnostics,
            )

    def _quarantine_locked(
        self,
        *,
        run_name: str,
        reasons: tuple[str, ...] | list[str],
        diagnostics: dict,
    ) -> dict:
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
        if (
            not self._retained_path_matches_stream()
            or not self._name_path_matches_stream()
        ):
            raise MinecraftTargetLockMetadataError(
                "Minecraft target lock path no longer names the retained lease"
            )
        acquired = self._read_metadata()
        if acquired.get("status") != "acquired" or acquired.get("attempt_id") != self.attempt_id:
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
            if (
                not self._retained_path_matches_stream()
                or not self._name_path_matches_stream()
            ):
                self._quarantine_uncertain = True
                raise MinecraftTargetLockMetadataError(
                    "Minecraft target lock path changed during quarantine"
                )
        except MinecraftTargetLockMetadataError:
            # JSON serialization is rejected before the retained lock stream
            # is modified.  Path drift, unlike serialization, has already set
            # the uncertainty flag and requires the independent guard marker.
            if self._quarantine_uncertain:
                try:
                    self._write_uncertain_marker()
                except BaseException:
                    pass
            raise
        except BaseException:
            # A write/fsync failure may have persisted the quarantine even
            # though the local assignment below was not reached.  Release
            # re-reads the on-disk status and will never downgrade such a
            # record to ``released``.  Also commit quarantine to the independently
            # locked guard inode now; callers may be unable to append a ledger
            # cleanup receipt or reach their normal release path.
            self._quarantine_uncertain = True
            try:
                self._write_uncertain_marker()
            except BaseException:
                pass
            raise
        self.quarantined = True
        self.quarantine_record = record
        return dict(record)

    def _retained_path_matches_stream(self) -> bool:
        return _stream_path_matches(self.path, self._stream)

    def _name_path_matches_stream(self) -> bool:
        return _stream_path_matches(self._name_path, self._name_stream)

    def _read_uncertain_marker(self) -> dict | None:
        if self._name_stream is None:
            return None
        return _read_uncertain_marker_stream(
            self._name_stream,
            expected_key=self.key,
            expected_host=self.host,
            expected_port=self.port,
        )

    def _write_uncertain_marker(self) -> bool:
        if self._name_stream is None or not self._name_path_matches_stream():
            return False
        marker = {
            "schema_version": LOCK_METADATA_SCHEMA_VERSION,
            "status": "quarantined",
            "attempt_id": self.attempt_id,
            "pid": os.getpid(),
            "host": self.host,
            "port": self.port,
            "world_id": self.world_id,
            "lock_key": self.key,
            "acquired_at": time.time(),
            "stale_owner_detected": False,
            "run_name": f"lock-release-{self.attempt_id}",
            "quarantined_at": time.time(),
            "reasons": ["lock_release_incomplete"],
            "diagnostics": {"path": str(self.path)},
        }
        try:
            _write_stream_metadata(self._name_stream, marker)
        except (MinecraftTargetLockMetadataError, OSError, TypeError, ValueError):
            return False
        return True

    def release(self) -> bool:
        with self._lifecycle_lock:
            if self._stream is None and self._name_stream is None:
                return self.release_succeeded is not False
            try:
                result = self._release_locked()
            except BaseException:
                self.release_succeeded = False
                raise
            self.release_succeeded = result
            return result

    def _release_locked(self) -> bool:
        stream = self._stream
        name_stream = self._name_stream
        if stream is None and name_stream is None:
            return True
        release_result = True
        retain_handles = False
        try:
            if self.acquired and (
                not self._retained_path_matches_stream()
                or not self._name_path_matches_stream()
            ):
                # Never publish ``released`` through an unlinked or replaced
                # descriptor.  The retained inode is no longer the admission
                # pathname, so its state is uncertain and the caller must
                # treat cleanup as incomplete.
                self._quarantine_uncertain = True
                if not self._write_uncertain_marker():
                    retain_handles = True
                release_result = False
                return False
            if self.acquired and not self.quarantined:
                metadata = self._read_metadata()
                if metadata.get("status") == "quarantined":
                    self.quarantined = True
                    self.quarantine_record = metadata
                elif self._quarantine_uncertain:
                    raise MinecraftTargetLockError(
                        "Minecraft target quarantine status is uncertain"
                    )
                else:
                    metadata.update({
                        "status": "released",
                        "released_at": max(time.time(), metadata["acquired_at"]),
                    })
                    self._write_metadata(metadata)
                    if (
                        not self._retained_path_matches_stream()
                        or not self._name_path_matches_stream()
                    ):
                        self._quarantine_uncertain = True
                        if not self._write_uncertain_marker():
                            retain_handles = True
                        release_result = False
        except BaseException:
            self._quarantine_uncertain = True
            if not self._write_uncertain_marker():
                retain_handles = True
            raise
        finally:
            if self.acquired and (
                not self._retained_path_matches_stream()
                or not self._name_path_matches_stream()
            ):
                self._quarantine_uncertain = True
                if not self._write_uncertain_marker():
                    retain_handles = True
                release_result = False
            if not retain_handles:
                try:
                    if self.acquired and stream is not None:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                finally:
                    try:
                        if stream is not None:
                            stream.close()
                    finally:
                        try:
                            if name_stream is not None:
                                fcntl.flock(name_stream.fileno(), fcntl.LOCK_UN)
                        finally:
                            try:
                                if name_stream is not None:
                                    name_stream.close()
                            finally:
                                self._stream = None
                                self._name_stream = None
                                self.acquired = False
        return release_result

    def retained_lease_snapshot(self) -> MinecraftTargetLeaseSnapshot:
        with self._lifecycle_lock:
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
        if not self._name_path_matches_stream():
            raise self._unavailable_error()

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
        _write_stream_metadata(self._stream, payload)

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
        stream = self._stream
        name_stream = self._name_stream
        if stream is None and name_stream is None:
            return
        if unlock:
            try:
                if stream is not None:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass
        try:
            if name_stream is not None:
                fcntl.flock(name_stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            if name_stream is not None:
                name_stream.close()
        except OSError:
            pass
        self._stream = None
        self._name_stream = None

    def _unavailable_error(self) -> MinecraftTargetLockUnavailableError:
        return MinecraftTargetLockUnavailableError(
            f"Minecraft target lock is unavailable for {self.host}:{self.port}",
            reason="io_error",
        )

    def __enter__(self) -> "MinecraftTargetLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        if not self.release():
            raise MinecraftTargetLockError(
                "Minecraft target lock release could not be verified"
            )


def _open_lock_stream(path: Path, *, writable: bool):
    """Open a lock path without following a final symlink."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise OSError("O_NOFOLLOW is required for Minecraft target locks")
    flags = os.O_CLOEXEC | nofollow
    if writable:
        flags |= os.O_RDWR | os.O_CREAT | os.O_APPEND
        mode = "a+"
    else:
        flags |= os.O_RDONLY
        mode = "r"
    fd = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("Minecraft target lock is not a regular file")
        return os.fdopen(fd, mode, encoding="utf-8")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _stream_path_matches(path: Path, stream: Any) -> bool:
    if stream is None:
        return False
    try:
        fd_stat = os.fstat(stream.fileno())
        path_stat = os.lstat(path)
    except (OSError, TypeError, ValueError):
        return False
    return (fd_stat.st_dev, fd_stat.st_ino) == (path_stat.st_dev, path_stat.st_ino)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
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
    return _read_lock_metadata_path(
        path,
        key=key,
        host=host,
        port=int(port),
    )


def read_minecraft_target_lock_state(
    *,
    lock_root: str | Path,
    host: str,
    port: int,
) -> dict:
    """Read target metadata and guard-only quarantine under the target guard."""
    key = minecraft_target_lock_key(host=host, port=port)
    root = Path(lock_root)
    root.mkdir(parents=True, exist_ok=True)
    guard_path = root / f"{key}.guard"
    try:
        guard = _open_lock_stream(guard_path, writable=True)
    except OSError as exc:
        raise MinecraftTargetLockMetadataError(
            "Minecraft target lock guard must be a regular non-symlink file"
        ) from exc
    try:
        if not _stream_path_matches(guard_path, guard):
            raise MinecraftTargetLockMetadataError(
                "Minecraft target lock guard path no longer names the opened guard"
            )
        try:
            fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MinecraftTargetLockError(
                f"Minecraft target {host}:{int(port)} is actively locked"
            ) from exc
        if not _stream_path_matches(guard_path, guard):
            raise MinecraftTargetLockMetadataError(
                "Minecraft target lock guard path changed during status read"
            )
        return {
            "metadata": _read_lock_metadata_path(
                root / f"{key}.lock",
                key=key,
                host=host,
                port=int(port),
            ),
            "guard_quarantine": _read_uncertain_marker_stream(
                guard,
                expected_key=key,
                expected_host=host,
                expected_port=int(port),
            ),
        }
    finally:
        try:
            fcntl.flock(guard.fileno(), fcntl.LOCK_UN)
        finally:
            guard.close()


def _read_lock_metadata_path(
    path: Path,
    *,
    key: str,
    host: str,
    port: int,
) -> dict:
    if not path.exists():
        if path.is_symlink():
            raise MinecraftTargetLockMetadataError(
                "Minecraft target lock path must not be a symlink"
            )
        return {}
    try:
        with _open_lock_stream(path, writable=False) as stream:
            content = stream.read()
    except UnicodeError as exc:
        raise MinecraftTargetLockMetadataError(
            "Minecraft target lock metadata encoding is invalid"
        ) from exc
    except OSError as exc:
        raise MinecraftTargetLockMetadataError(
            "Minecraft target lock path is invalid"
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
        expected_key=key,
        expected_host=host,
        expected_port=port,
    )


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
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{key}.lock"
    guard_path = root / f"{key}.guard"
    try:
        guard = _open_lock_stream(guard_path, writable=True)
    except OSError as exc:
        raise MinecraftTargetLockMetadataError(
            "Minecraft target lock guard must be a regular non-symlink file"
        ) from exc
    stream = None
    try:
        if not _stream_path_matches(guard_path, guard):
            raise MinecraftTargetLockMetadataError(
                "Minecraft target lock guard path no longer names the opened guard"
            )
        try:
            fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MinecraftTargetLockError(
                f"Minecraft target {host}:{int(port)} is actively locked"
            ) from exc
        if not _stream_path_matches(guard_path, guard):
            raise MinecraftTargetLockMetadataError(
                "Minecraft target lock guard path changed before quarantine clearing"
            )
        guard_quarantine = _read_uncertain_marker_stream(
            guard,
            expected_key=key,
            expected_host=host,
            expected_port=int(port),
        )
        try:
            stream = _open_lock_stream(path, writable=True)
        except OSError as exc:
            raise MinecraftTargetLockMetadataError(
                "Minecraft target lock path must be a regular non-symlink file"
            ) from exc
        if not _stream_path_matches(path, stream):
            raise MinecraftTargetLockMetadataError(
                "Minecraft target lock path no longer names the opened lease"
            )
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MinecraftTargetLockError(
                f"Minecraft target {host}:{int(port)} is actively locked"
            ) from exc
        if not _stream_path_matches(path, stream):
            raise MinecraftTargetLockMetadataError(
                "Minecraft target lock path changed before quarantine clearing"
            )
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
            and guard_quarantine is None
            and not force_corrupt
        ):
            raise MinecraftTargetLockError("Minecraft target is not quarantined")
        last_quarantine = (
            _public_quarantine_history(previous)
            or _public_quarantine_history(guard_quarantine or {})
        )
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
        if (
            not _stream_path_matches(path, stream)
            or not _stream_path_matches(guard_path, guard)
        ):
            raise MinecraftTargetLockMetadataError(
                "Minecraft target lock path changed before quarantine clear write"
            )
        _write_stream_metadata(stream, cleared)
        if (
            not _stream_path_matches(path, stream)
            or not _stream_path_matches(guard_path, guard)
        ):
            raise MinecraftTargetLockMetadataError(
                "Minecraft target lock path changed during quarantine clear"
            )
        if guard_quarantine is not None:
            _clear_uncertain_marker_stream(guard)
            if not _stream_path_matches(guard_path, guard):
                raise MinecraftTargetLockMetadataError(
                    "Minecraft target lock guard path changed during uncertainty clear"
                )
        return cleared
    finally:
        try:
            if stream is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                if stream is not None:
                    stream.close()
            finally:
                try:
                    fcntl.flock(guard.fileno(), fcntl.LOCK_UN)
                finally:
                    guard.close()


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


def _validate_uncertain_marker(
    payload: object,
    *,
    expected_key: str,
    expected_host: str,
    expected_port: int,
) -> None:
    parsed = _parse_lock_metadata(
        payload,
        expected_key=expected_key,
        expected_host=expected_host,
        expected_port=expected_port,
    )
    if (
        parsed.get("status") != "quarantined"
        or "lock_release_incomplete" not in parsed.get("reasons", [])
    ):
        raise MinecraftTargetLockMetadataError(
            "Minecraft target lock uncertainty marker is invalid"
        )


def _read_uncertain_marker_stream(
    stream: Any,
    *,
    expected_key: str,
    expected_host: str,
    expected_port: int,
) -> dict | None:
    try:
        stream.seek(0)
        content = stream.read()
        stream.seek(0)
    except (OSError, TypeError, ValueError, UnicodeError) as exc:
        raise MinecraftTargetLockMetadataError(
            "Minecraft target lock uncertainty marker is unreadable"
        ) from exc
    if not content.strip():
        return None
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise MinecraftTargetLockMetadataError(
            "Minecraft target lock uncertainty marker is invalid JSON"
        ) from exc
    _validate_uncertain_marker(
        payload,
        expected_key=expected_key,
        expected_host=expected_host,
        expected_port=expected_port,
    )
    return dict(payload)


def _clear_uncertain_marker_stream(stream: Any) -> None:
    stream.seek(0)
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())


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
    try:
        encoded = json.dumps(payload, indent=2, allow_nan=False) + "\n"
    except (TypeError, ValueError, OverflowError) as exc:
        raise MinecraftTargetLockMetadataError(
            "Minecraft target lock metadata is not JSON serializable"
        ) from exc
    stream.seek(0)
    # The lock stream is opened in ``a+`` mode so acquisition can create it;
    # truncate before writing because O_APPEND would otherwise append even
    # after seek(0).  Serialization happens first, so invalid diagnostics can
    # never destroy an otherwise valid metadata record.  Any later write or
    # fsync failure leaves the lock fail-closed rather than silently accepting
    # a partial JSON object.
    stream.truncate()
    stream.write(encoded)
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
