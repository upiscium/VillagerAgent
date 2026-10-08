import hashlib
import fcntl
import json
import multiprocessing
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from types import SimpleNamespace

import pytest

import benchmarks.minecraft.run_lock as run_lock_module
from benchmarks.minecraft.run_lock import (
    MinecraftTargetLock,
    MinecraftTargetLockBusyError,
    MinecraftTargetLockError,
    MinecraftTargetLockMetadataError,
    MinecraftTargetLockReleaseOutcome,
    MinecraftTargetLockReleaseStatus,
    MinecraftTargetLockUnavailableError,
    MinecraftTargetLeaseSnapshot,
    MinecraftTargetPredecessorHistoryStatus,
    MinecraftTargetPredecessorInspectionToken,
    MinecraftTargetPredecessorAcknowledgementStatus,
    MinecraftTargetStorageQualification,
    MinecraftTargetQuarantinedError,
    clear_minecraft_target_quarantine,
    acknowledge_minecraft_target_predecessor,
    load_minecraft_target_storage_qualification,
    minecraft_target_lock_key,
    read_minecraft_target_lock_metadata,
    read_minecraft_target_lock_status,
    read_minecraft_target_predecessor_status,
)


def _lock(tmp_path, attempt_id, *, port=25565):
    return MinecraftTargetLock(
        lock_root=tmp_path / "locks",
        host="127.0.0.1",
        port=port,
        world_id="world-a",
        attempt_id=attempt_id,
    )


@pytest.fixture(autouse=True)
def _restore_test_boot_id_provider():
    """Keep test-only boot identity injection local to one test case."""
    previous = run_lock_module._BOOT_ID_PROVIDER
    run_lock_module._BOOT_ID_PROVIDER = lambda: "pytest-default-boot-id"
    try:
        yield
    finally:
        run_lock_module._BOOT_ID_PROVIDER = previous


def test_retained_lease_snapshot_exposes_immutable_lease_identity(tmp_path):
    lock = _lock(tmp_path, "attempt-snapshot").acquire()
    try:
        snapshot = lock.retained_lease_snapshot()
        fd_stat = os.fstat(lock._stream.fileno())
        path_stat = os.lstat(lock.path)

        assert isinstance(snapshot, MinecraftTargetLeaseSnapshot)
        assert tuple(field.name for field in fields(snapshot)) == (
            "fd",
            "fd_dev",
            "fd_ino",
            "path_dev",
            "path_ino",
            "attempt_id",
            "lock_key",
            "owner_pid",
            "owner_alive",
            "metadata",
            "acquired",
            "quarantined",
        )
        assert snapshot.fd == lock._stream.fileno()
        assert (snapshot.fd_dev, snapshot.fd_ino) == (fd_stat.st_dev, fd_stat.st_ino)
        assert (snapshot.path_dev, snapshot.path_ino) == (path_stat.st_dev, path_stat.st_ino)
        assert snapshot.attempt_id == "attempt-snapshot"
        assert snapshot.lock_key == lock.key
        assert snapshot.owner_pid == os.getpid()
        assert snapshot.owner_alive is True
        assert snapshot.metadata["status"] == "acquired"
        assert snapshot.acquired is True
        assert snapshot.quarantined is False
        assert not hasattr(snapshot, "_stream")
        with pytest.raises(FrozenInstanceError):
            snapshot.fd = -1
    finally:
        lock.release()


def test_retained_lease_snapshot_requires_acquisition(tmp_path):
    lock = _lock(tmp_path, "attempt-never-acquired")

    with pytest.raises(MinecraftTargetLockError):
        lock.retained_lease_snapshot()


def test_retained_lease_snapshot_is_read_only_and_repeated_calls_are_idempotent(tmp_path):
    lock = _lock(tmp_path, "attempt-read-only").acquire()
    try:
        before_bytes = lock.path.read_bytes()
        before_hash = hashlib.sha256(before_bytes).hexdigest()
        before_stat = os.stat(lock.path, follow_symlinks=False)
        before_position = lock._stream.tell()

        first = lock.retained_lease_snapshot()
        second = lock.retained_lease_snapshot()

        after_bytes = lock.path.read_bytes()
        after_stat = os.stat(lock.path, follow_symlinks=False)
        assert hashlib.sha256(after_bytes).hexdigest() == before_hash
        assert after_bytes == before_bytes
        assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
        assert lock._stream.tell() == before_position
        assert first == second
        assert first.metadata == second.metadata
    finally:
        lock.release()


def test_retained_lease_snapshot_surfaces_path_inode_drift(tmp_path):
    lock = _lock(tmp_path, "attempt-inode-drift").acquire()
    replacement = lock.path.with_name("replacement.lock")
    try:
        replacement.write_bytes(lock.path.read_bytes())
        os.replace(replacement, lock.path)

        snapshot = lock.retained_lease_snapshot()

        assert snapshot.fd_ino != snapshot.path_ino
        assert (snapshot.path_dev, snapshot.path_ino) == (
            os.lstat(lock.path).st_dev,
            os.lstat(lock.path).st_ino,
        )
    finally:
        outcome = lock.release()
        assert outcome.status is MinecraftTargetLockReleaseStatus.UNCERTAIN
        assert outcome.uncertainty_persisted is True
        status = read_minecraft_target_lock_status(
            lock_root=lock.lock_root,
            host=lock.host,
            port=lock.port,
        )
        assert status["quarantined"] is True
        assert status["uncertain"] is True
        with pytest.raises(MinecraftTargetLockUnavailableError) as raised:
            _lock(tmp_path, "attempt-after-inode-drift").acquire()
        assert raised.value.reason == "uncertain"
        if lock.path.exists():
            lock.path.unlink()


def test_retained_lease_snapshot_fails_for_missing_path(tmp_path):
    lock = _lock(tmp_path, "attempt-missing-path").acquire()
    try:
        lock.path.unlink()
        with pytest.raises(MinecraftTargetLockUnavailableError):
            lock.retained_lease_snapshot()
    finally:
        outcome = lock.release()
        assert outcome.status is MinecraftTargetLockReleaseStatus.UNCERTAIN
        assert outcome.uncertainty_persisted is True


def test_retained_lease_snapshot_fails_for_closed_retained_stream(tmp_path):
    lock = _lock(tmp_path, "attempt-closed-stream").acquire()
    try:
        lock._stream.close()
        with pytest.raises(MinecraftTargetLockUnavailableError):
            lock.retained_lease_snapshot()
    finally:
        lock._stream = None
        lock.acquired = False


def test_retained_lease_snapshot_fails_for_malformed_metadata(tmp_path):
    lock = _lock(tmp_path, "attempt-malformed-snapshot").acquire()
    original_content = lock.path.read_text(encoding="utf-8")
    try:
        lock.path.write_text("{", encoding="utf-8")
        with pytest.raises(MinecraftTargetLockMetadataError):
            lock.retained_lease_snapshot()
    finally:
        lock.path.write_text(original_content, encoding="utf-8")
        lock.release()


@pytest.mark.parametrize(
    ("schema_version", "field"),
    [(2, "status"), (1, "status"), (2, "previous_status")],
)
def test_retained_lease_snapshot_classifies_structured_metadata_corruption(
    tmp_path,
    schema_version,
    field,
):
    lock = _lock(tmp_path, "attempt-structured-corruption").acquire()
    original_content = lock.path.read_text(encoding="utf-8")
    original_position = lock._stream.tell()
    try:
        metadata = json.loads(original_content)
        metadata["schema_version"] = schema_version
        metadata[field] = ["acquired"]
        lock.path.write_text(json.dumps(metadata), encoding="utf-8")

        with pytest.raises(MinecraftTargetLockMetadataError):
            lock.retained_lease_snapshot()
        assert lock._stream.tell() == original_position
    finally:
        lock.path.write_text(original_content, encoding="utf-8")
        lock.release()


def test_retained_lease_snapshot_rejects_tampered_owner_pid_in_v3_metadata(tmp_path):
    lock = _lock(tmp_path, "attempt-oversized-pid").acquire()
    original_content = lock.path.read_text(encoding="utf-8")
    try:
        metadata = json.loads(original_content)
        metadata["pid"] = 1 << 100
        lock.path.write_text(json.dumps(metadata), encoding="utf-8")

        with pytest.raises(MinecraftTargetLockMetadataError):
            lock.retained_lease_snapshot()
    finally:
        lock.path.write_text(original_content, encoding="utf-8")
        lock.release()


def test_acquire_classifies_unrepresentable_stale_owner_pid(tmp_path, monkeypatch):
    lock = _lock(tmp_path, "attempt-new-owner")
    lock.path.parent.mkdir(parents=True)
    lock.path.write_text(json.dumps({
        "schema_version": 2,
        "status": "acquired",
        "lock_key": lock.key,
        "host": lock.host,
        "port": lock.port,
        "world_id": lock.world_id,
        "pid": 1 << 100,
        "attempt_id": "attempt-old-owner",
        "acquired_at": 1.0,
        "stale_owner_detected": False,
    }), encoding="utf-8")

    def overflow_pid(_pid, _signal):
        raise OverflowError("pid is outside the platform range")

    monkeypatch.setattr("benchmarks.minecraft.run_lock.os.kill", overflow_pid)
    with pytest.raises(MinecraftTargetLockMetadataError, match="owner pid"):
        lock.acquire()

    assert lock.acquired is False
    assert lock._stream is None


@pytest.mark.parametrize("dangling", [False, True])
def test_metadata_reader_rejects_lock_path_symlinks(tmp_path, dangling):
    lock = _lock(tmp_path, f"attempt-symlink-reader-{dangling}")
    lock.path.parent.mkdir(parents=True)
    target = tmp_path / "other-lock-metadata"
    if not dangling:
        target.write_text("{}", encoding="utf-8")
    lock.path.symlink_to(target)

    with pytest.raises(MinecraftTargetLockMetadataError, match="regular file"):
        read_minecraft_target_lock_metadata(
            lock_root=lock.lock_root,
            host=lock.host,
            port=lock.port,
        )


def test_retained_lease_snapshot_observes_quarantined_state_and_freezes_metadata(tmp_path):
    lock = _lock(tmp_path, "attempt-quarantined-snapshot").acquire()
    try:
        record = lock.quarantine(
            run_name="run-a",
            reasons=["bridge_cleanup_incomplete"],
            diagnostics={"nested": {"items": [{"safe": True}]}},
        )
        snapshot = lock.retained_lease_snapshot()

        assert snapshot.acquired is True
        assert snapshot.quarantined is True
        assert snapshot.attempt_id == record["attempt_id"]
        assert snapshot.owner_pid == os.getpid()
        assert snapshot.owner_alive is True
        assert snapshot.metadata["status"] == "quarantined"
        assert snapshot.metadata["reasons"] == ("bridge_cleanup_incomplete",)
        assert snapshot.metadata["diagnostics"]["nested"]["items"][0]["safe"] is True
        with pytest.raises(TypeError):
            snapshot.metadata["status"] = "acquired"
        with pytest.raises(TypeError):
            snapshot.metadata["diagnostics"]["nested"]["items"][0]["safe"] = False
        with pytest.raises(AttributeError):
            snapshot.metadata["reasons"].append("new-reason")

        constructed = MinecraftTargetLeaseSnapshot(
            fd=snapshot.fd,
            fd_dev=snapshot.fd_dev,
            fd_ino=snapshot.fd_ino,
            path_dev=snapshot.path_dev,
            path_ino=snapshot.path_ino,
            attempt_id=snapshot.attempt_id,
            lock_key=snapshot.lock_key,
            owner_pid=snapshot.owner_pid,
            owner_alive=snapshot.owner_alive,
            metadata={"mutable_set": {"value"}, "mutable_bytes": bytearray(b"x")},
            acquired=snapshot.acquired,
            quarantined=snapshot.quarantined,
        )
        assert constructed.metadata["mutable_set"] == frozenset({"value"})
        assert constructed.metadata["mutable_bytes"] == b"x"
    finally:
        lock.release()


def test_retained_lease_snapshot_preserves_owner_liveness_semantics(tmp_path, monkeypatch):
    lock = _lock(tmp_path, "attempt-liveness").acquire()
    try:
        monkeypatch.setattr(
            "benchmarks.minecraft.run_lock._pid_exists",
            lambda pid: False,
        )
        snapshot = lock.retained_lease_snapshot()
        assert snapshot.owner_pid == os.getpid()
        assert snapshot.owner_alive is False
    finally:
        lock.release()


def test_retained_lease_snapshot_lstats_symlink_without_following(tmp_path):
    lock = _lock(tmp_path, "attempt-symlink-path").acquire()
    target = tmp_path / "target.lock"
    try:
        target.write_bytes(lock.path.read_bytes())
        lock.path.unlink()
        lock.path.symlink_to(target)

        snapshot = lock.retained_lease_snapshot()

        link_stat = os.lstat(lock.path)
        target_stat = os.stat(lock.path)
        assert (snapshot.path_dev, snapshot.path_ino) == (link_stat.st_dev, link_stat.st_ino)
        assert snapshot.path_ino != target_stat.st_ino
    finally:
        if lock.path.is_symlink():
            lock.path.unlink()
        outcome = lock.release()
        assert outcome.status is MinecraftTargetLockReleaseStatus.UNCERTAIN


def test_retained_lease_snapshot_accepts_legacy_metadata_after_compatibility_migration(tmp_path):
    lock = _lock(tmp_path, "attempt-legacy-snapshot")
    _write_schema_v1_metadata(lock, status="released", attempt_id="attempt-old")

    with lock:
        snapshot = lock.retained_lease_snapshot()
        assert snapshot.metadata["schema_version"] == 3
        assert snapshot.metadata["status"] == "acquired"
        assert snapshot.attempt_id == "attempt-legacy-snapshot"


def test_retained_lease_snapshot_rejects_released_lock(tmp_path):
    lock = _lock(tmp_path, "attempt-released-snapshot").acquire()
    lock.release()

    with pytest.raises(MinecraftTargetLockError):
        lock.retained_lease_snapshot()


def test_contended_lock_cannot_publish_a_retained_lease_snapshot(tmp_path):
    owner = _lock(tmp_path, "attempt-owner").acquire()
    contender = _lock(tmp_path, "attempt-contender")
    try:
        with pytest.raises(MinecraftTargetLockBusyError):
            contender.acquire()
        with pytest.raises(MinecraftTargetLockError):
            contender.retained_lease_snapshot()
    finally:
        owner.release()


def test_same_minecraft_target_rejects_second_owner(tmp_path):
    first = _lock(tmp_path, "attempt-owner").acquire()
    contender = _lock(tmp_path, "attempt-contender")
    try:
        with pytest.raises(MinecraftTargetLockBusyError, match="attempt attempt-owner") as raised:
            contender.acquire()
        assert raised.value.reason == "busy"
        assert raised.value.owner == {
            "status": "acquired",
            "attempt_id": "attempt-owner",
        }
        assert contender.acquired is False
        assert contender._stream is None
    finally:
        first.release()


def test_repeated_acquire_on_same_instance_preserves_existing_flock(tmp_path):
    owner = _lock(tmp_path, "attempt-repeated-acquire").acquire()
    retained_stream = owner._stream
    contender = _lock(tmp_path, "attempt-repeated-contender")
    try:
        with pytest.raises(MinecraftTargetLockError, match="already retains a lease"):
            owner.acquire()
        assert owner.acquired is True
        assert owner._stream is retained_stream
        owner.retained_lease_snapshot()
        with pytest.raises(MinecraftTargetLockBusyError):
            contender.acquire()
    finally:
        assert owner.release().status is MinecraftTargetLockReleaseStatus.VERIFIED_RELEASED

    with contender:
        assert contender.acquired is True


def test_lifecycle_guard_is_reentrant_and_serializes_same_target_release(tmp_path):
    owner = _lock(tmp_path, "attempt-owner").acquire()
    same_target = _lock(tmp_path, "attempt-other")
    guard_entered = threading.Event()
    allow_guard_exit = threading.Event()
    release_started = threading.Event()
    release_finished = threading.Event()
    release_result = []

    def hold_guard():
        with same_target.lifecycle_guard():
            with owner.lifecycle_guard():
                guard_entered.set()
                assert allow_guard_exit.wait(2)

    def release_owner():
        release_started.set()
        release_result.append(owner.release())
        release_finished.set()

    guard_thread = threading.Thread(target=hold_guard)
    guard_thread.start()
    assert guard_entered.wait(1)
    release_thread = threading.Thread(target=release_owner)
    release_thread.start()
    try:
        assert release_started.wait(1)
        assert not release_finished.wait(0.05)
    finally:
        allow_guard_exit.set()
        guard_thread.join(timeout=1)
        release_thread.join(timeout=1)

    assert release_finished.is_set()
    assert release_result[0].status is MinecraftTargetLockReleaseStatus.VERIFIED_RELEASED


@pytest.mark.parametrize("operation_name", ["acquire", "snapshot", "quarantine", "release"])
def test_all_public_lifecycle_operations_honor_same_guard(tmp_path, operation_name):
    owner = _lock(tmp_path, f"attempt-{operation_name}-owner").acquire()
    contender = _lock(tmp_path, f"attempt-{operation_name}-contender")
    started = threading.Event()
    finished = threading.Event()
    errors = []

    def operation():
        started.set()
        try:
            if operation_name == "acquire":
                try:
                    contender.acquire()
                except MinecraftTargetLockBusyError:
                    pass
            elif operation_name == "snapshot":
                owner.retained_lease_snapshot()
            elif operation_name == "quarantine":
                owner.quarantine(
                    run_name="guard-run",
                    reasons=["guard-test"],
                    diagnostics={},
                )
            else:
                owner.release()
        except Exception as exc:  # Surface worker-thread failures in the test thread.
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=operation)
    try:
        with owner.lifecycle_guard():
            thread.start()
            assert started.wait(1)
            assert not finished.wait(0.05)
        thread.join(timeout=1)
        assert finished.is_set()
        assert not errors
    finally:
        if thread.is_alive():
            thread.join(timeout=1)
        if owner.acquired:
            owner.release()
        if contender.acquired:
            contender.release()


def test_acknowledged_clear_waits_for_same_target_lifecycle_guard(tmp_path):
    quarantined = _lock(tmp_path, "attempt-clear-guard").acquire()
    quarantined.quarantine(
        run_name="clear-guard-run",
        reasons=["operator-check"],
        diagnostics={},
    )
    quarantined.release()
    guard_owner = _lock(tmp_path, "attempt-clear-guard-guard")
    started = threading.Event()
    finished = threading.Event()
    result = []

    def clear():
        started.set()
        result.append(clear_minecraft_target_quarantine(
            lock_root=guard_owner.lock_root,
            host=guard_owner.host,
            port=guard_owner.port,
            reason="Target safely inspected",
            acknowledge_target_safe=True,
        ))
        finished.set()

    thread = threading.Thread(target=clear)
    with guard_owner.lifecycle_guard():
        thread.start()
        assert started.wait(1)
        assert not finished.wait(0.05)
    thread.join(timeout=1)

    assert finished.is_set()
    assert result[0]["status"] == "cleared"
    replacement = _lock(tmp_path, "attempt-after-clear-guard").acquire()
    replacement.release()


def test_uncertainty_clear_requires_target_safe_acknowledgement(tmp_path):
    lock = _lock(tmp_path, "attempt-uncertainty-clear-ack")
    lock.lock_root.mkdir(parents=True)
    marker = lock.path.with_suffix(".uncertain")
    marker.write_text("corrupt marker", encoding="utf-8")

    with pytest.raises(ValueError, match="acknowledge_target_safe"):
        clear_minecraft_target_quarantine(
            lock_root=lock.lock_root,
            host=lock.host,
            port=lock.port,
            reason="Target inspected",
            acknowledge_target_safe=False,
        )

    assert marker.exists()
    assert read_minecraft_target_lock_status(
        lock_root=lock.lock_root,
        host=lock.host,
        port=lock.port,
    )["blocking"] is True


def test_poll_interval_constructor_semantics_remain_unvalidated(tmp_path):
    lock = MinecraftTargetLock(
        lock_root=tmp_path / "locks",
        host="127.0.0.1",
        port=25565,
        world_id="world-a",
        attempt_id="attempt-negative-poll",
        poll_interval_seconds=-0.25,
    )

    assert lock.poll_interval_seconds == -0.25
    assert lock.release().status is MinecraftTargetLockReleaseStatus.NOT_ACQUIRED


def test_invalid_poll_sleep_closes_contender_stream_without_constructor_policy_change(
    tmp_path,
):
    owner = _lock(tmp_path, "attempt-poll-owner").acquire()
    contender = MinecraftTargetLock(
        lock_root=tmp_path / "locks",
        host="127.0.0.1",
        port=25565,
        world_id="world-a",
        attempt_id="attempt-poll-contender",
        timeout_seconds=1,
        poll_interval_seconds=-0.25,
    )
    try:
        with pytest.raises(ValueError):
            contender.acquire()
        assert contender.acquired is False
        assert contender._stream is None
        assert owner.acquired is True
    finally:
        owner.release()


def test_release_returns_and_remembers_explicit_outcomes(tmp_path):
    unacquired = _lock(tmp_path, "attempt-not-acquired")
    not_acquired = unacquired.release()
    assert isinstance(not_acquired, MinecraftTargetLockReleaseOutcome)
    assert not_acquired.status is MinecraftTargetLockReleaseStatus.NOT_ACQUIRED
    assert unacquired.last_release_outcome is not_acquired

    acquired = _lock(tmp_path, "attempt-released").acquire()
    released = acquired.release()
    assert released.status is MinecraftTargetLockReleaseStatus.VERIFIED_RELEASED
    assert released.verified_released is True
    assert acquired.last_release_outcome is released
    assert not acquired.path.with_suffix(".uncertain").exists()


def test_release_metadata_failure_persists_uncertainty_and_blocks_admission(
    tmp_path,
    monkeypatch,
):
    lock = _lock(tmp_path, "attempt-metadata-failure").acquire()

    def fail_metadata_write(_payload):
        raise OSError("simulated metadata fsync failure")

    monkeypatch.setattr(lock, "_write_metadata", fail_metadata_write)
    outcome = lock.release()

    marker = lock.path.with_suffix(".uncertain")
    assert outcome.status is MinecraftTargetLockReleaseStatus.UNCERTAIN
    assert outcome.uncertainty_persisted is True
    assert lock.last_release_outcome is outcome
    assert json.loads(marker.read_text(encoding="utf-8"))["status"] == "uncertain"
    with pytest.raises(MinecraftTargetLockUnavailableError) as raised:
        _lock(tmp_path, "attempt-after-uncertainty").acquire()
    assert raised.value.reason == "uncertain"


@pytest.mark.parametrize("write_failure", [OSError, KeyboardInterrupt])
@pytest.mark.parametrize("first_marker_result", ["non_durable", "durable", "raises"])
def test_failed_quarantine_cannot_become_verified_release_on_context_exit(
    tmp_path, monkeypatch, write_failure, first_marker_result,
):
    lock = _lock(tmp_path, "attempt-failed-quarantine")
    real_write = lock._write_metadata
    real_persist = run_lock_module._persist_uncertainty_marker
    writes = []
    marker_attempts = []

    def write_with_one_shot_failure(payload):
        writes.append(payload["status"])
        if payload["status"] == "quarantined":
            raise write_failure("quarantine metadata write failed")
        return real_write(payload)

    def persist_with_one_shot_failure(*args, **kwargs):
        marker_attempts.append(kwargs["error_type"])
        if len(marker_attempts) == 1:
            if first_marker_result == "non_durable":
                return False
            if first_marker_result == "raises":
                raise OSError("uncertainty marker could not be persisted")
        return real_persist(*args, **kwargs)

    with pytest.raises(write_failure, match="quarantine metadata write failed"):
        with lock:
            monkeypatch.setattr(lock, "_write_metadata", write_with_one_shot_failure)
            monkeypatch.setattr(
                run_lock_module, "_persist_uncertainty_marker",
                persist_with_one_shot_failure,
            )
            lock.quarantine(
                run_name="unsafe-run",
                reasons=["cleanup_unverified"],
                diagnostics={},
            )

    assert writes == ["quarantined"]
    assert len(marker_attempts) == 2
    assert lock.last_release_outcome.status is MinecraftTargetLockReleaseStatus.UNCERTAIN
    assert lock.last_release_outcome.uncertainty_persisted is True
    assert lock.last_release_outcome.error_type == write_failure.__name__
    assert lock.last_release_outcome.error == "quarantine metadata write failed"
    marker = lock.path.with_suffix(".uncertain")
    marker_metadata = json.loads(marker.read_text(encoding="utf-8"))
    assert marker_metadata["status"] == "uncertain"
    assert marker_metadata["error_type"] == write_failure.__name__
    assert read_minecraft_target_lock_status(
        lock_root=lock.lock_root, host=lock.host, port=lock.port,
    )["blocking"] is True
    contender = _lock(tmp_path, "attempt-after-failed-quarantine")
    with pytest.raises(MinecraftTargetLockUnavailableError) as raised:
        contender.acquire()
    assert raised.value.reason == "uncertain"

    with pytest.raises(ValueError, match="acknowledge_target_safe"):
        clear_minecraft_target_quarantine(
            lock_root=lock.lock_root, host=lock.host, port=lock.port,
            reason="Target examined", acknowledge_target_safe=False,
        )
    clear_minecraft_target_quarantine(
        lock_root=lock.lock_root, host=lock.host, port=lock.port,
        reason="Target examined and safe", acknowledge_target_safe=True,
    )
    assert not marker.exists()
    with contender:
        assert contender.acquired is True


def test_direct_release_retains_failed_quarantine_when_marker_is_not_durable(
    tmp_path, monkeypatch,
):
    lock = _lock(tmp_path, "attempt-failed-direct-quarantine").acquire()
    real_write = lock._write_metadata
    real_persist = run_lock_module._persist_uncertainty_marker
    marker_attempts = []

    def write_with_one_shot_failure(payload):
        if payload["status"] == "quarantined":
            raise OSError("quarantine metadata write failed")
        return real_write(payload)

    def persist_with_temporary_failure(*args, **kwargs):
        marker_attempts.append(kwargs["error_type"])
        if len(marker_attempts) <= 2:
            return False
        return real_persist(*args, **kwargs)

    monkeypatch.setattr(lock, "_write_metadata", write_with_one_shot_failure)
    monkeypatch.setattr(
        run_lock_module, "_persist_uncertainty_marker", persist_with_temporary_failure,
    )
    with pytest.raises(OSError, match="quarantine metadata write failed"):
        lock.quarantine(
            run_name="unsafe-run", reasons=["cleanup_unverified"], diagnostics={},
        )

    outcome = lock.release()
    assert outcome.status is MinecraftTargetLockReleaseStatus.FAILED
    assert outcome.uncertainty_persisted is False
    assert outcome.error_type == "OSError"
    assert outcome.error == "quarantine metadata write failed"
    assert len(marker_attempts) == 2
    assert lock.acquired is True
    assert lock._stream is not None
    contender = _lock(tmp_path, "attempt-while-failed-quarantine-is-held")
    with pytest.raises(MinecraftTargetLockBusyError):
        contender.acquire()

    outcome = lock.release()
    assert outcome.status is MinecraftTargetLockReleaseStatus.UNCERTAIN
    assert outcome.uncertainty_persisted is True
    assert len(marker_attempts) == 3
    marker_metadata = json.loads(lock.path.with_suffix(".uncertain").read_text(encoding="utf-8"))
    assert marker_metadata["error_type"] == "OSError"
    assert marker_metadata["error"] == "quarantine metadata write failed"
    with pytest.raises(MinecraftTargetLockUnavailableError) as raised:
        contender.acquire()
    assert raised.value.reason == "uncertain"
    clear_minecraft_target_quarantine(
        lock_root=lock.lock_root, host=lock.host, port=lock.port,
        reason="Target examined and safe", acknowledge_target_safe=True,
    )
    with contender:
        assert contender.acquired is True


@pytest.mark.parametrize("retry_failure", [OSError, KeyboardInterrupt])
def test_quarantine_marker_retry_exception_keeps_original_and_retains_flock(
    tmp_path, monkeypatch, retry_failure,
):
    lock = _lock(tmp_path, "attempt-marker-retry-interrupted")
    real_write = lock._write_metadata
    marker_attempts = []

    def fail_quarantine_write(payload):
        if payload["status"] == "quarantined":
            raise OSError("original quarantine write failure")
        return real_write(payload)

    def fail_both_marker_attempts(*args, **kwargs):
        marker_attempts.append(kwargs["error_type"])
        if len(marker_attempts) == 1:
            return False
        raise retry_failure("marker retry interrupted")

    with pytest.raises(OSError, match="original quarantine write failure"):
        with lock:
            monkeypatch.setattr(lock, "_write_metadata", fail_quarantine_write)
            monkeypatch.setattr(
                run_lock_module, "_persist_uncertainty_marker", fail_both_marker_attempts,
            )
            lock.quarantine(
                run_name="unsafe-run", reasons=["cleanup_unverified"], diagnostics={},
            )

    assert marker_attempts == ["OSError", "OSError"]
    assert lock.last_release_outcome.status is MinecraftTargetLockReleaseStatus.FAILED
    assert lock.last_release_outcome.error_type == "OSError"
    assert lock.last_release_outcome.error == "original quarantine write failure"
    assert lock.last_release_outcome.uncertainty_persisted is False
    assert lock.acquired is True
    assert run_lock_module._UNVERIFIED_RELEASE_LOCKS[id(lock)] is lock
    contender = _lock(tmp_path, "attempt-while-retry-interrupted")
    with pytest.raises(MinecraftTargetLockBusyError):
        contender.acquire()

    monkeypatch.undo()
    assert lock.release().status is MinecraftTargetLockReleaseStatus.UNCERTAIN
    with pytest.raises(MinecraftTargetLockUnavailableError) as raised:
        contender.acquire()
    assert raised.value.reason == "uncertain"
    clear_minecraft_target_quarantine(
        lock_root=lock.lock_root, host=lock.host, port=lock.port,
        reason="Target examined and safe", acknowledge_target_safe=True,
    )
    with contender:
        assert contender.acquired is True


def test_release_retains_flock_when_uncertainty_cannot_be_persisted(
    tmp_path,
    monkeypatch,
):
    lock = _lock(tmp_path, "attempt-unpersistable-uncertainty").acquire()

    def fail_metadata_write(_payload):
        raise OSError("simulated metadata failure")

    monkeypatch.setattr(lock, "_write_metadata", fail_metadata_write)
    monkeypatch.setattr(
        "benchmarks.minecraft.run_lock._persist_uncertainty_marker",
        lambda *_args, **_kwargs: False,
    )
    outcome = lock.release()
    assert outcome.status is MinecraftTargetLockReleaseStatus.FAILED
    assert outcome.uncertainty_persisted is False
    assert lock.acquired is True
    assert lock._stream is not None
    with pytest.raises(MinecraftTargetLockBusyError):
        _lock(tmp_path, "attempt-contender").acquire()

    monkeypatch.undo()
    assert lock.release().status is MinecraftTargetLockReleaseStatus.VERIFIED_RELEASED


def test_directory_fsync_failure_is_not_reported_as_durable_uncertainty(
    tmp_path,
    monkeypatch,
):
    lock = _lock(tmp_path, "attempt-marker-fsync-failure").acquire()

    def fail_metadata_write(_payload):
        raise OSError("simulated release metadata failure")

    def fail_directory_fsync(_path):
        raise OSError("simulated parent directory fsync failure")

    monkeypatch.setattr(lock, "_write_metadata", fail_metadata_write)
    monkeypatch.setattr(
        "benchmarks.minecraft.run_lock._fsync_parent_directory",
        fail_directory_fsync,
    )
    outcome = lock.release()

    assert outcome.status is MinecraftTargetLockReleaseStatus.FAILED
    assert outcome.uncertainty_persisted is False
    assert outcome.verified_released is False
    assert lock.acquired is True
    assert lock.path.with_suffix(".uncertain").exists()
    status = read_minecraft_target_lock_status(
        lock_root=lock.lock_root,
        host=lock.host,
        port=lock.port,
    )
    assert status["quarantined"] is True
    assert status["uncertain"] is True

    monkeypatch.undo()
    retried = lock.release()
    assert retried.status is MinecraftTargetLockReleaseStatus.UNCERTAIN
    assert retried.uncertainty_persisted is True


def test_context_manager_cleanup_failure_does_not_mask_runtime_exception(
    tmp_path,
    monkeypatch,
):
    lock = _lock(tmp_path, "attempt-context-release-failure")

    def fail_metadata_write(_payload):
        raise OSError("simulated release write failure")

    with pytest.raises(RuntimeError, match="runtime failure"):
        with lock:
            monkeypatch.setattr(lock, "_write_metadata", fail_metadata_write)
            monkeypatch.setattr(
                "benchmarks.minecraft.run_lock._persist_uncertainty_marker",
                lambda *_args, **_kwargs: False,
            )
            raise RuntimeError("runtime failure")

    assert lock.last_release_outcome.status is MinecraftTargetLockReleaseStatus.FAILED
    assert lock.acquired is True
    monkeypatch.undo()
    assert lock.release().status is MinecraftTargetLockReleaseStatus.VERIFIED_RELEASED


def test_context_manager_exposes_unverified_release_result_after_normal_exit(
    tmp_path,
    monkeypatch,
):
    lock = _lock(tmp_path, "attempt-context-normal-release-failure")

    def fail_metadata_write(_payload):
        raise OSError("simulated release metadata failure")

    with lock:
        monkeypatch.setattr(lock, "_write_metadata", fail_metadata_write)
        monkeypatch.setattr(
            "benchmarks.minecraft.run_lock._persist_uncertainty_marker",
            lambda *_args, **_kwargs: False,
        )

    assert lock.last_release_outcome.status is MinecraftTargetLockReleaseStatus.FAILED
    assert lock.last_release_outcome.uncertainty_persisted is False
    assert lock.acquired is True
    monkeypatch.undo()
    assert lock.release().status is MinecraftTargetLockReleaseStatus.VERIFIED_RELEASED


@pytest.mark.parametrize("marker_content", ["{", "[]", '{"status":"released"}'])
def test_any_present_uncertainty_marker_blocks_acquire_and_status_reports_it(
    tmp_path,
    marker_content,
):
    lock = _lock(tmp_path, "attempt-marker")
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    marker = lock.path.with_suffix(".uncertain")
    marker.write_text(marker_content, encoding="utf-8")

    status = read_minecraft_target_lock_status(
        lock_root=lock.lock_root,
        host=lock.host,
        port=lock.port,
    )
    assert status["metadata"] == {}
    assert status["quarantined"] is True
    assert status["uncertain"] is True
    assert status["actively_owned"] is False
    assert status["uncertainty"]["present"] is True
    assert status["uncertainty"]["valid"] is False
    assert status["blocking"] is True
    with pytest.raises(MinecraftTargetLockUnavailableError) as raised:
        lock.acquire()
    assert raised.value.reason == "uncertain"


def test_symlink_uncertainty_marker_blocks_acquire(tmp_path):
    lock = _lock(tmp_path, "attempt-symlink-marker")
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    marker = lock.path.with_suffix(".uncertain")
    marker.symlink_to(tmp_path / "missing-marker-target")

    status = read_minecraft_target_lock_status(
        lock_root=lock.lock_root,
        host=lock.host,
        port=lock.port,
    )
    assert status["uncertain"] is True
    assert status["uncertainty"]["kind"] == "symlink"
    with pytest.raises(MinecraftTargetLockUnavailableError) as raised:
        lock.acquire()
    assert raised.value.reason == "uncertain"


def test_status_reports_an_actively_owned_target(tmp_path):
    owner = _lock(tmp_path, "attempt-active").acquire()
    try:
        status = read_minecraft_target_lock_status(
            lock_root=owner.lock_root,
            host=owner.host,
            port=owner.port,
        )
        assert status["metadata"]["attempt_id"] == "attempt-active"
        assert status["actively_owned"] is True
        assert status["uncertain"] is False
    finally:
        owner.release()


def test_acknowledged_clear_removes_quarantine_and_uncertainty_marker(tmp_path):
    owner = _lock(tmp_path, "attempt-quarantine-clear").acquire()
    owner.quarantine(
        run_name="run-a",
        reasons=["cleanup_incomplete"],
        diagnostics={"safe": False},
    )
    owner.release()
    marker = owner.path.with_suffix(".uncertain")
    marker.write_text("corrupt marker", encoding="utf-8")

    cleared = clear_minecraft_target_quarantine(
        lock_root=owner.lock_root,
        host=owner.host,
        port=owner.port,
        reason="Target safety verified",
        acknowledge_target_safe=True,
    )

    assert cleared["status"] == "cleared"
    assert cleared["last_quarantine"]["attempt_id"] == "attempt-quarantine-clear"
    assert not os.path.lexists(marker)
    assert read_minecraft_target_lock_status(
        lock_root=owner.lock_root,
        host=owner.host,
        port=owner.port,
    )["uncertain"] is False


@pytest.mark.parametrize("content", ["", "{", "[]", '{"status": "acquired"}'])
def test_contention_owner_metadata_is_best_effort(tmp_path, content):
    owner = _lock(tmp_path, "attempt-owner").acquire()
    original_content = owner.path.read_text(encoding="utf-8")
    owner.path.write_text(content, encoding="utf-8")
    contender = _lock(tmp_path, "attempt-contender")
    try:
        with pytest.raises(MinecraftTargetLockBusyError):
            contender.acquire()
        assert contender._stream is None
        assert owner.path.read_text(encoding="utf-8") == content
    finally:
        owner.path.write_text(original_content, encoding="utf-8")
        owner.release()


def test_contender_acquires_after_owner_releases_within_timeout(tmp_path):
    owner = _lock(tmp_path, "attempt-owner").acquire()
    contender = MinecraftTargetLock(
        lock_root=tmp_path / "locks",
        host="127.0.0.1",
        port=25565,
        world_id="world-a",
        attempt_id="attempt-contender",
        timeout_seconds=1,
        poll_interval_seconds=0.01,
    )

    def release_owner():
        time.sleep(0.05)
        owner.release()

    release_thread = threading.Thread(target=release_owner)
    release_thread.start()
    try:
        contender.acquire()
        assert contender.acquired is True
    finally:
        contender.release()
        release_thread.join(timeout=1)


def test_non_contention_flock_error_is_unavailable(tmp_path, monkeypatch):
    lock = _lock(tmp_path, "attempt-a")
    monkeypatch.setattr(
        "benchmarks.minecraft.run_lock.fcntl.flock",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("flock failed")),
    )

    with pytest.raises(MinecraftTargetLockUnavailableError) as raised:
        lock.acquire()

    assert raised.value.reason == "io_error"
    assert str(lock.path) not in str(raised.value)
    assert lock.acquired is False
    assert lock._stream is None


def test_different_minecraft_targets_can_be_locked(tmp_path):
    first = _lock(tmp_path, "attempt-a", port=25565).acquire()
    second = _lock(tmp_path, "attempt-b", port=25566).acquire()
    try:
        assert first.acquired is True
        assert second.acquired is True
    finally:
        second.release()
        first.release()


def test_lock_is_released_after_context_failure(tmp_path):
    lock = _lock(tmp_path, "attempt-a")
    with pytest.raises(RuntimeError, match="child failed"):
        with lock:
            raise RuntimeError("child failed")
    assert lock.last_release_outcome.status is MinecraftTargetLockReleaseStatus.VERIFIED_RELEASED

    replacement = _lock(tmp_path, "attempt-b")
    with replacement:
        assert replacement.acquired is True
    assert replacement.last_release_outcome.status is MinecraftTargetLockReleaseStatus.VERIFIED_RELEASED


def test_unlocked_dead_owner_metadata_is_detected_as_stale(tmp_path):
    lock = _lock(tmp_path, "attempt-a")
    lock.path.parent.mkdir(parents=True)
    lock.path.write_text(json.dumps({
        "schema_version": 2,
        "status": "acquired",
        "lock_key": lock.key,
        "host": "127.0.0.1",
        "port": 25565,
        "world_id": "world-a",
        "pid": 99999999,
        "attempt_id": "dead",
        "acquired_at": 1.0,
        "stale_owner_detected": False,
    }), encoding="utf-8")

    with lock:
        assert lock.stale_owner_detected is True
        metadata = json.loads(lock.path.read_text(encoding="utf-8"))
        assert metadata["pid"] == os.getpid()


def test_schema_v1_released_metadata_migrates_to_schema_v3_on_acquire(tmp_path):
    lock = _lock(tmp_path, "attempt-new")
    _write_schema_v1_metadata(lock, status="released", attempt_id="attempt-old")

    with lock:
        metadata = json.loads(lock.path.read_text(encoding="utf-8"))
        assert metadata["schema_version"] == 3
        assert metadata["status"] == "acquired"
        assert metadata["attempt_id"] == "attempt-new"
        assert metadata["lock_key"] == lock.key
        assert metadata["host"] == "127.0.0.1"
        assert metadata["port"] == 25565
        assert metadata["migrated_from_schema_version"] == 1
        assert metadata["previous_status"] == "released"


def test_schema_v1_dead_acquired_owner_migrates_to_schema_v3_as_stale(tmp_path):
    lock = _lock(tmp_path, "attempt-new")
    _write_schema_v1_metadata(
        lock,
        status="acquired",
        attempt_id="attempt-dead",
        pid=99999999,
    )

    with lock:
        metadata = json.loads(lock.path.read_text(encoding="utf-8"))
        assert lock.stale_owner_detected is True
        assert metadata["schema_version"] == 3
        assert metadata["stale_owner_detected"] is True
        assert metadata["previous_status"] == "acquired"


def test_schema_v1_identity_mismatch_fails_closed(tmp_path):
    lock = _lock(tmp_path, "attempt-new")
    _write_schema_v1_metadata(
        lock,
        status="released",
        attempt_id="attempt-old",
        host="other-host",
    )

    with pytest.raises(MinecraftTargetLockMetadataError, match="identity mismatch"):
        lock.acquire()


@pytest.mark.parametrize(
    ("status", "field", "replacement"),
    [
        ("acquired", "pid", None),
        ("acquired", "acquired_at", None),
        ("released", "attempt_id", None),
        ("released", "released_at", None),
        ("quarantined", "run_name", ""),
        ("quarantined", "quarantined_at", None),
        ("cleared", "clear_reason", None),
        ("cleared", "cleared_at", None),
        ("cleared", "last_quarantine", {"attempt_id": "attempt-a"}),
    ],
)
def test_invalid_schema_v2_state_is_rejected_without_modification(
    tmp_path,
    status,
    field,
    replacement,
):
    lock = _lock(tmp_path, "attempt-new")
    metadata = _schema_v2_metadata(lock, status=status)
    if replacement is None:
        del metadata[field]
    else:
        metadata[field] = replacement
    original_content = json.dumps(metadata, indent=2) + "\n"
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_text(original_content, encoding="utf-8")

    with pytest.raises(MinecraftTargetLockMetadataError):
        lock.acquire()

    assert lock.path.read_text(encoding="utf-8") == original_content


def test_generated_schema_v3_states_validate(tmp_path):
    first = _lock(tmp_path, "attempt-a").acquire()
    assert _read_lock_metadata(tmp_path)["status"] == "acquired"
    first.release()
    assert _read_lock_metadata(tmp_path)["status"] == "released"

    second = _lock(tmp_path, "attempt-b").acquire()
    second.quarantine(
        run_name="run-b",
        reasons=["bridge_cleanup_incomplete"],
        diagnostics={},
    )
    second.release()
    assert _read_lock_metadata(tmp_path)["status"] == "quarantined"

    clear_minecraft_target_quarantine(
        lock_root=tmp_path / "locks",
        host="127.0.0.1",
        port=25565,
        reason="Verified cleanup",
        acknowledge_target_safe=True,
    )
    assert _read_lock_metadata(tmp_path)["status"] == "cleared"


def test_quarantine_persists_and_rejects_next_owner(tmp_path):
    first = _lock(tmp_path, "attempt-a").acquire()
    record = first.quarantine(
        run_name="run-a",
        reasons=["bridge_cleanup_incomplete"],
        diagnostics={"bridge_cleanup_complete": False},
    )
    first.release()

    persisted = json.loads(first.path.read_text(encoding="utf-8"))
    assert record["status"] == persisted["status"] == "quarantined"
    assert persisted["reasons"] == ["bridge_cleanup_incomplete"]
    with pytest.raises(MinecraftTargetQuarantinedError) as raised:
        _lock(tmp_path, "attempt-b").acquire()
    assert raised.value.quarantine["attempt_id"] == "attempt-a"
    assert json.loads(first.path.read_text(encoding="utf-8"))["status"] == "quarantined"


def test_quarantine_does_not_affect_different_target(tmp_path):
    first = _lock(tmp_path, "attempt-a", port=25565).acquire()
    first.quarantine(
        run_name="run-a",
        reasons=["runtime_process_alive_after_kill"],
        diagnostics={},
    )
    first.release()

    with _lock(tmp_path, "attempt-b", port=25566) as second:
        assert second.acquired is True


def test_clear_allows_new_owner_after_explicit_acknowledgement(tmp_path):
    first = _lock(tmp_path, "attempt-a").acquire()
    first.quarantine(
        run_name="run-a",
        reasons=["bridge_cleanup_incomplete"],
        diagnostics={},
    )
    first.release()

    cleared = clear_minecraft_target_quarantine(
        lock_root=tmp_path / "locks",
        host="127.0.0.1",
        port=25565,
        reason="Verified all processes stopped",
        acknowledge_target_safe=True,
    )

    assert cleared["status"] == "cleared"
    assert cleared["last_quarantine"]["attempt_id"] == "attempt-a"
    with _lock(tmp_path, "attempt-b") as replacement:
        assert replacement.acquired is True


def test_clear_rejects_active_owner(tmp_path):
    active = _lock(tmp_path, "attempt-a").acquire()
    try:
        with pytest.raises(MinecraftTargetLockError, match="actively locked"):
            clear_minecraft_target_quarantine(
                lock_root=tmp_path / "locks",
                host="127.0.0.1",
                port=25565,
                reason="Unsafe override",
                acknowledge_target_safe=True,
            )
    finally:
        active.release()


@pytest.mark.parametrize("content", ["{", "[]", '{"schema_version": 99}'])
def test_invalid_or_unsupported_metadata_fails_closed(tmp_path, content):
    lock = _lock(tmp_path, "attempt-a")
    lock.path.parent.mkdir(parents=True)
    lock.path.write_text(content, encoding="utf-8")

    with pytest.raises(MinecraftTargetLockMetadataError):
        lock.acquire()


def test_invalid_utf8_metadata_fails_closed_without_modification(tmp_path):
    lock = _lock(tmp_path, "attempt-a")
    original_content = b"\xff\xfeinvalid-lock-metadata"
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_bytes(original_content)

    with pytest.raises(MinecraftTargetLockMetadataError, match="encoding is invalid"):
        lock.acquire()

    assert lock.acquired is False
    assert lock._stream is None
    assert lock.path.read_bytes() == original_content


def test_quarantine_requires_non_empty_reasons(tmp_path):
    lock = _lock(tmp_path, "attempt-a").acquire()
    try:
        with pytest.raises(ValueError, match="reasons"):
            lock.quarantine(run_name="run-a", reasons=[], diagnostics={})
    finally:
        lock.release()


def test_quarantine_is_observed_across_process_lock_handoff(tmp_path):
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    release = context.Event()
    result = context.Queue()
    lock_root = str(tmp_path / "locks")
    owner = context.Process(
        target=_quarantine_owner,
        args=(lock_root, ready, release),
    )
    waiter = context.Process(
        target=_quarantine_waiter,
        args=(lock_root, result),
    )

    owner.start()
    assert ready.wait(2)
    waiter.start()
    time.sleep(0.1)
    release.set()
    owner.join(3)
    waiter.join(3)

    assert owner.exitcode == 0
    assert waiter.exitcode == 0
    assert result.get(timeout=1) == "quarantined"
    metadata = read_minecraft_target_lock_metadata(
        lock_root=lock_root,
        host="127.0.0.1",
        port=25565,
    )
    assert metadata["status"] == "quarantined"
    assert metadata["attempt_id"] == "attempt-owner"


def _quarantine_owner(lock_root, ready, release):
    lock = MinecraftTargetLock(
        lock_root=lock_root,
        host="127.0.0.1",
        port=25565,
        world_id="world-a",
        attempt_id="attempt-owner",
    ).acquire()
    lock.quarantine(
        run_name="owner-run",
        reasons=["bridge_cleanup_incomplete"],
        diagnostics={},
    )
    ready.set()
    release.wait(2)
    lock.release()


def _quarantine_waiter(lock_root, result):
    lock = MinecraftTargetLock(
        lock_root=lock_root,
        host="127.0.0.1",
        port=25565,
        world_id="world-a",
        attempt_id="attempt-waiter",
        timeout_seconds=2,
    )
    try:
        lock.acquire()
    except MinecraftTargetQuarantinedError:
        result.put("quarantined")
    else:
        result.put("acquired")
        lock.release()


def _write_schema_v1_metadata(
    lock,
    *,
    status,
    attempt_id,
    pid=None,
    host="127.0.0.1",
):
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "schema_version": 1,
        "status": status,
        "attempt_id": attempt_id,
        "pid": os.getpid() if pid is None else pid,
        "host": host,
        "port": lock.port,
        "world_id": lock.world_id,
        "lock_key": lock.key,
    }
    lock.path.write_text(json.dumps(metadata), encoding="utf-8")


def _schema_v2_metadata(lock, *, status):
    metadata = {
        "schema_version": 2,
        "status": status,
        "lock_key": lock.key,
        "host": lock.host,
        "port": lock.port,
    }
    if status in {"acquired", "released", "quarantined"}:
        metadata.update({
            "attempt_id": "attempt-a",
            "pid": os.getpid(),
            "world_id": "world-a",
            "acquired_at": 1.0,
            "stale_owner_detected": False,
        })
    if status == "released":
        metadata["released_at"] = 2.0
    elif status == "quarantined":
        metadata.update({
            "run_name": "run-a",
            "quarantined_at": 2.0,
            "reasons": ["bridge_cleanup_incomplete"],
            "diagnostics": {},
        })
    elif status == "cleared":
        metadata.update({
            "cleared_at": 3.0,
            "cleared_by_pid": os.getpid(),
            "clear_reason": "Verified cleanup",
            "last_quarantine": {
                "attempt_id": "attempt-a",
                "run_name": "run-a",
                "quarantined_at": 2.0,
                "reasons": ["bridge_cleanup_incomplete"],
            },
        })
    return metadata


def _read_lock_metadata(tmp_path):
    return read_minecraft_target_lock_metadata(
        lock_root=tmp_path / "locks",
        host="127.0.0.1",
        port=25565,
    )


def _predecessor_status(lock, storage_qualification=None):
    return read_minecraft_target_predecessor_status(
        lock_root=lock.lock_root,
        host=lock.host,
        port=lock.port,
        storage_qualification=storage_qualification,
    )


def _crash_close_without_release(lock):
    """Drop retained descriptors as process death would; do not publish release."""
    stream = lock._stream
    assert stream is not None
    history_io = lock._history_io
    lock._history_io = None
    lock._stream = None
    lock._lease_identity = None
    lock.acquired = False
    if history_io is not None:
        history_io.close()
    stream.close()


def _test_storage_qualification(lock, *, profile_id="pytest-test-profile", profile_version=1,
                                boot_id="pytest-boot-id", receipt_name="storage-qualification.json"):
    """Make a TEST-ONLY receipt; this fixture is not production durability proof."""
    run_lock_module._BOOT_ID_PROVIDER = lambda: boot_id
    lock.lock_root.mkdir(parents=True, exist_ok=True)
    root_stat = os.stat(lock.lock_root, follow_symlinks=False)
    payload = {
        "artifact_id": "minecraft-target-storage-qualification",
        "artifact_version": 1,
        "storage_profile_id": profile_id,
        "storage_profile_version": profile_version,
        "receipt_id": f"pytest-{receipt_name}",
        "boot_id": boot_id,
        "qualified_root_absolute_path": str(lock.lock_root.resolve()),
        "qualified_root_dev": root_stat.st_dev,
        "qualified_root_ino": root_stat.st_ino,
        "qualified_filesystem_device": root_stat.st_dev,
        "capabilities": sorted(run_lock_module._STORAGE_CAPABILITIES),
        "issuer_audit_id": "TEST ONLY: deterministic pytest fixture, not production proof",
    }
    payload["detached_artifact_sha256"] = run_lock_module._digest(
        run_lock_module._canonical_bytes(payload)
    )
    receipt = lock.lock_root.parent / receipt_name
    serialized = run_lock_module._canonical_bytes(payload)
    if not receipt.exists() or receipt.read_bytes() != serialized:
        receipt.write_bytes(serialized)
        os.chmod(receipt, 0o600)
    return load_minecraft_target_storage_qualification(receipt)


def _acknowledge_predecessor(lock, snapshot, **overrides):
    options = {
        "acknowledge_target_safe": True,
        "acknowledge_whole_prefix": True,
        "reason": "Target safety independently verified",
        "operator": "operator-603-test",
    }
    if "storage_qualification" not in overrides:
        options["storage_qualification"] = _test_storage_qualification(lock)
    options.update(overrides)
    return acknowledge_minecraft_target_predecessor(
        lock_root=lock.lock_root,
        host=lock.host,
        port=lock.port,
        expected=snapshot.token,
        **options,
    )


def test_acknowledged_diagnostics_are_separate_from_new_unresolved_prefix(tmp_path):
    first_owner = _lock(tmp_path, "attempt-a-before-ack").acquire()
    _crash_close_without_release(first_owner)
    initial = _predecessor_status(first_owner)
    receipt = _test_storage_qualification(first_owner)
    ack = _acknowledge_predecessor(first_owner, initial, storage_qualification=receipt)
    assert ack.acknowledged is True
    assert ack.snapshot.status is MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN
    assert ack.snapshot.acknowledgement["storage_profile_id"] == "pytest-test-profile"

    second_owner = _lock(tmp_path, "attempt-b-after-ack").acquire()
    _crash_close_without_release(second_owner)
    third_owner = _lock(tmp_path, "attempt-c-after-ack").acquire()
    _crash_close_without_release(third_owner)
    fourth_owner = _lock(tmp_path, "attempt-d-after-ack").acquire()
    assert fourth_owner.release().status is MinecraftTargetLockReleaseStatus.VERIFIED_RELEASED
    observed = _predecessor_status(fourth_owner, receipt)

    assert observed.status is MinecraftTargetPredecessorHistoryStatus.UNRESOLVED
    assert observed.first["attempt_id"] == "attempt-b-after-ack"
    assert observed.latest["attempt_id"] == "attempt-c-after-ack"
    assert observed.observation_count == 2
    assert observed.acknowledgement["storage_profile_id"] == "pytest-test-profile"
    assert observed.acknowledged_diagnostics["first"]["attempt_id"] == "attempt-a-before-ack"
    assert observed.to_dict()["acknowledgement"] is not None
    acknowledged_again = _acknowledge_predecessor(
        fourth_owner, observed, storage_qualification=receipt
    )
    assert acknowledged_again.acknowledged is True
    assert acknowledged_again.snapshot.acknowledged_diagnostics["first"]["attempt_id"] == "attempt-b-after-ack"
    assert acknowledged_again.snapshot.acknowledged_diagnostics["latest"]["attempt_id"] == "attempt-c-after-ack"
    assert acknowledged_again.snapshot.acknowledged_diagnostics["observation_count"] == 2
    assert acknowledged_again.snapshot.first is None
    assert acknowledged_again.snapshot.latest is None
    assert acknowledged_again.snapshot.prior_acknowledged_diagnostics["first"]["attempt_id"] == "attempt-a-before-ack"
    history = run_lock_module._validate_history(
        run_lock_module._strict_json(fourth_owner.path.with_suffix(".history").read_bytes(), canonical=True)
    )
    assert history["acknowledged_diagnostics"]["first"]["attempt_id"] == "attempt-b-after-ack"
    assert history["acknowledged_diagnostics"]["observation_count"] == 2
    assert history["prior_acknowledged_diagnostics"]["first"]["attempt_id"] == "attempt-a-before-ack"


def test_acknowledgement_binds_exact_inspected_history_and_raw_metadata(tmp_path):
    prior = _lock(tmp_path, "attempt-inspected-prior").acquire()
    _crash_close_without_release(prior)
    current = _lock(tmp_path, "attempt-inspected-current").acquire()
    _crash_close_without_release(current)
    inspected = _predecessor_status(current)
    token_state = inspected.token.state
    pointer = token_state["history_pointer"]
    receipt = _test_storage_qualification(current)

    outcome = _acknowledge_predecessor(current, inspected, storage_qualification=receipt)

    assert outcome.acknowledged is True
    covered = outcome.snapshot.acknowledgement
    assert covered["covered_generation"] == pointer["generation"]
    assert covered["covered_ordinal"] == pointer["ordinal"]
    assert covered["covered_digest"] == pointer["digest"]
    assert covered["covered_metadata_digest"] == token_state["metadata_digest"]
    assert covered["covered_writer_epoch"] == token_state["writer_epoch"]
    assert covered["covered_unflocked_acquired_owner"]["attempt_id"] == "attempt-inspected-current"
    assert covered["covered_unflocked_acquired_owner"]["source_digest"] == token_state["metadata_digest"]
    assert covered["covered_digest"] != outcome.snapshot.digest


def test_absent_token_ack_records_absent_metadata_identity_without_synthesizing_digest(tmp_path):
    lock = _lock(tmp_path, "attempt-absent-metadata-coverage")
    lock.lock_root.mkdir(parents=True)
    qualification = _test_storage_qualification(lock)
    inspected = _predecessor_status(lock)
    assert inspected.token.state["metadata_kind"] == "absent"
    assert inspected.token.state["metadata_digest"] is None

    outcome = _acknowledge_predecessor(
        lock, inspected, storage_qualification=qualification, reconcile_unknown_history=True
    )

    assert outcome.acknowledged is True
    coverage = outcome.snapshot.acknowledgement
    assert coverage["covered_metadata_digest"] is None
    assert coverage["covered_metadata_kind"] == "absent"
    assert coverage["covered_writer_epoch"] is None
    assert coverage["covered_revision"] is None
    assert coverage["covered_transition_nonce"] is None
    assert coverage["covered_unflocked_acquired_owner"] is None


def test_released_unknown_ack_binds_exact_persisted_metadata_writer_fence(tmp_path):
    previous = _lock(tmp_path, "attempt-released-u-prior").acquire()
    _crash_close_without_release(previous)
    current = _lock(tmp_path, "attempt-released-u-current").acquire()
    assert current.release().status is MinecraftTargetLockReleaseStatus.VERIFIED_RELEASED
    qualification = _test_storage_qualification(current)
    inspected = _predecessor_status(current)
    state = inspected.token.state
    pointer = state["history_pointer"]
    assert inspected.status is MinecraftTargetPredecessorHistoryStatus.UNRESOLVED
    assert state["metadata_status"] == "released"

    outcome = _acknowledge_predecessor(
        current, inspected, storage_qualification=qualification
    )

    assert outcome.acknowledged is True
    coverage = outcome.snapshot.acknowledgement
    assert coverage["covered_generation"] == pointer["generation"]
    assert coverage["covered_ordinal"] == pointer["ordinal"]
    assert coverage["covered_digest"] == pointer["digest"]
    assert coverage["covered_metadata_digest"] == state["metadata_digest"]
    assert coverage["covered_metadata_kind"] == "v3"
    assert coverage["covered_writer_epoch"] == state["writer_epoch"]
    assert coverage["covered_revision"] == state["revision"]
    assert coverage["covered_transition_nonce"] == state["transition_nonce"]
    assert coverage["covered_unflocked_acquired_owner"] is None


def test_ack_cas_rejects_intervening_metadata_mutation_under_stable_receipt(tmp_path):
    crashed = _lock(tmp_path, "attempt-a2-cas-intervening-mutation").acquire()
    _crash_close_without_release(crashed)
    inspected = _predecessor_status(crashed)
    qualification = _test_storage_qualification(crashed)
    raw_before = crashed.path.read_bytes()
    metadata = run_lock_module._strict_json(raw_before, canonical=True)
    metadata["attempt_id"] = "same-record-but-mutated-after-inspection"
    crashed.path.write_bytes(run_lock_module._canonical_bytes(run_lock_module._seal(metadata)))

    outcome = _acknowledge_predecessor(
        crashed, inspected, storage_qualification=qualification, reconcile_unknown_history=True
    )

    assert outcome.acknowledged is False
    assert crashed.path.read_bytes() != raw_before
    assert not crashed.path.with_suffix(".history-clear-pending").exists()
    assert _predecessor_status(crashed, qualification).status is not MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN


def test_open_lock_rejects_identity_drift_between_lstat_and_open(tmp_path, monkeypatch):
    lock = _lock(tmp_path, "attempt-lstat-open-drift")
    lock.lock_root.mkdir(parents=True)
    lock.path.write_bytes(b"initial existing lock inode")
    replacement = lock.path.with_name("replacement-lock-inode")
    replacement.write_bytes(b"replacement must not be admitted")
    real_open = os.open
    swapped = []

    def replace_then_open(path, flags, *args, **kwargs):
        if Path(path) == lock.path and not swapped:
            swapped.append(True)
            os.replace(replacement, lock.path)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(run_lock_module.os, "open", replace_then_open)
    with pytest.raises(MinecraftTargetLockUnavailableError):
        lock.acquire()
    assert swapped == [True]
    assert lock.acquired is False
    assert lock._stream is None
    assert lock.path.read_bytes() == b"replacement must not be admitted"


def test_unqualified_or_stale_storage_receipt_never_returns_clean(tmp_path):
    crashed = _lock(tmp_path, "attempt-storage-gate").acquire()
    _crash_close_without_release(crashed)
    inspected = _predecessor_status(crashed)
    receipt = _test_storage_qualification(crashed)
    ack = _acknowledge_predecessor(crashed, inspected, storage_qualification=receipt)
    assert ack.acknowledged is True
    assert _predecessor_status(crashed).status is MinecraftTargetPredecessorHistoryStatus.AMBIGUOUS
    assert _predecessor_status(crashed, receipt).status is MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN

    receipt.receipt_path.unlink()
    stale = _predecessor_status(crashed, receipt)
    assert stale.status is MinecraftTargetPredecessorHistoryStatus.AMBIGUOUS
    assert "storage qualification" in stale.error


def test_storage_receipt_profile_and_boot_revalidation(tmp_path):
    crashed = _lock(tmp_path, "attempt-storage-profile").acquire()
    _crash_close_without_release(crashed)
    initial = _predecessor_status(crashed)
    original = _test_storage_qualification(crashed, profile_id="profile-a", boot_id="boot-a")
    assert _acknowledge_predecessor(crashed, initial, storage_qualification=original).acknowledged

    different_profile = _test_storage_qualification(
        crashed, profile_id="profile-b", boot_id="boot-a", receipt_name="profile-b.json"
    )
    assert _predecessor_status(crashed, different_profile).status is MinecraftTargetPredecessorHistoryStatus.AMBIGUOUS

    # A new same-profile receipt for a later boot revalidates the committed pair;
    # boot identity is intentionally not persisted in the acknowledgement.
    reboot_receipt = _test_storage_qualification(
        crashed, profile_id="profile-a", boot_id="boot-b", receipt_name="profile-a-after-reboot.json"
    )
    assert _predecessor_status(crashed, original).status is MinecraftTargetPredecessorHistoryStatus.AMBIGUOUS
    assert _predecessor_status(crashed, reboot_receipt).status is MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN

    fresh_other_profile = _test_storage_qualification(
        crashed, profile_id="profile-b", boot_id="boot-b", receipt_name="profile-b-after-reboot.json"
    )
    observed_with_new_profile = _predecessor_status(crashed, fresh_other_profile)
    refreshed = _acknowledge_predecessor(
        crashed, observed_with_new_profile, storage_qualification=fresh_other_profile,
        reason="Explicit new-profile target-safe re-acknowledgement",
        reconcile_unknown_history=True,
    )
    assert refreshed.acknowledged is True
    assert refreshed.snapshot.acknowledgement["storage_profile_id"] == "profile-b"
    assert _predecessor_status(crashed, fresh_other_profile).status is MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN


@pytest.mark.parametrize(
    "tamper",
    ["unknown", "missing", "duplicate", "digest", "capabilities", "extra_capability",
     "wrong_boot", "root_path", "root_dev", "root_ino", "root_device", "missing_profile"],
)
def test_storage_receipt_is_closed_canonical_and_root_bound(tmp_path, tamper):
    lock = _lock(tmp_path, f"attempt-storage-receipt-{tamper}")
    receipt = _test_storage_qualification(lock)
    payload = run_lock_module._strict_json(receipt.receipt_path.read_bytes(), canonical=True)
    if tamper == "unknown":
        payload["unexpected"] = "field"
    elif tamper == "missing":
        del payload["issuer_audit_id"]
    elif tamper == "duplicate":
        raw = receipt.receipt_path.read_bytes().rstrip(b"\n")[:-1] + b',"artifact_id":"duplicate"}\n'
        receipt.receipt_path.write_bytes(raw)
        with pytest.raises(MinecraftTargetLockMetadataError):
            load_minecraft_target_storage_qualification(receipt.receipt_path)
        return
    elif tamper == "digest":
        payload["detached_artifact_sha256"] = "0" * 64
    elif tamper == "capabilities":
        payload["capabilities"].pop()
    elif tamper == "extra_capability":
        payload["capabilities"][-1] = "caller_asserted_durability"
    elif tamper == "wrong_boot":
        payload["boot_id"] = "stale-boot-id"
    elif tamper == "root_path":
        payload["qualified_root_absolute_path"] = str(lock.lock_root.parent / "other-root")
    elif tamper == "root_dev":
        payload["qualified_root_dev"] += 1
    elif tamper == "root_ino":
        payload["qualified_root_ino"] += 1
    elif tamper == "missing_profile":
        del payload["storage_profile_id"]
    else:
        payload["qualified_filesystem_device"] += 1
    if tamper != "digest":
        unsigned = {key: value for key, value in payload.items() if key != "detached_artifact_sha256"}
        payload["detached_artifact_sha256"] = run_lock_module._digest(
            run_lock_module._canonical_bytes(unsigned)
        )
    receipt.receipt_path.write_bytes(run_lock_module._canonical_bytes(payload))
    with pytest.raises(MinecraftTargetLockMetadataError):
        load_minecraft_target_storage_qualification(receipt.receipt_path)


def test_storage_receipt_group_world_write_and_replacement_invalidate_clean(tmp_path):
    crashed = _lock(tmp_path, "attempt-storage-replacement").acquire()
    _crash_close_without_release(crashed)
    initial = _predecessor_status(crashed)
    receipt = _test_storage_qualification(crashed)
    assert _acknowledge_predecessor(crashed, initial, storage_qualification=receipt).acknowledged
    replacement = receipt.receipt_path.with_name("receipt-replacement.json")
    replacement.write_bytes(receipt.receipt_path.read_bytes())
    os.chmod(replacement, 0o600)
    os.replace(replacement, receipt.receipt_path)
    assert _predecessor_status(crashed, receipt).status is MinecraftTargetPredecessorHistoryStatus.AMBIGUOUS
    os.chmod(receipt.receipt_path, 0o622)
    with pytest.raises(MinecraftTargetLockMetadataError):
        load_minecraft_target_storage_qualification(receipt.receipt_path)


def test_storage_receipt_path_is_canonical_absolute_and_final_symlink_is_rejected(tmp_path, monkeypatch):
    lock = _lock(tmp_path, "attempt-canonical-receipt-path")
    receipt = _test_storage_qualification(lock)
    monkeypatch.chdir(receipt.receipt_path.parent)
    loaded = load_minecraft_target_storage_qualification(receipt.receipt_path.name)
    assert loaded.receipt_path.is_absolute()
    assert loaded.receipt_path == receipt.receipt_path.resolve()

    symlink = receipt.receipt_path.with_name("receipt-link.json")
    symlink.symlink_to(receipt.receipt_path)
    with pytest.raises(MinecraftTargetLockMetadataError):
        load_minecraft_target_storage_qualification(symlink)


@pytest.mark.parametrize("kind", ["directory", "oversized", "wrong_owner"])
def test_storage_receipt_rejects_nonregular_oversized_or_wrong_owner_file(
    tmp_path, monkeypatch, kind,
):
    lock = _lock(tmp_path, f"attempt-receipt-file-{kind}")
    receipt = _test_storage_qualification(lock)
    if kind == "directory":
        replacement = receipt.receipt_path.with_name("receipt-directory")
        receipt.receipt_path.unlink()
        replacement.mkdir()
        with pytest.raises(MinecraftTargetLockMetadataError):
            load_minecraft_target_storage_qualification(replacement)
    elif kind == "oversized":
        receipt.receipt_path.write_bytes(b" " * (run_lock_module._MAX_RECORD_BYTES + 1))
        os.chmod(receipt.receipt_path, 0o600)
        with pytest.raises(MinecraftTargetLockMetadataError, match="bounded size"):
            load_minecraft_target_storage_qualification(receipt.receipt_path)
    else:
        real_lstat = run_lock_module.os.lstat

        def different_owner(path, *args, **kwargs):
            result = real_lstat(path, *args, **kwargs)
            if Path(path) == receipt.receipt_path:
                return SimpleNamespace(
                    st_mode=result.st_mode, st_uid=os.geteuid() + 1,
                    st_dev=result.st_dev, st_ino=result.st_ino, st_size=result.st_size,
                    st_mtime_ns=result.st_mtime_ns,
                )
            return result

        monkeypatch.setattr(run_lock_module.os, "lstat", different_owner)
        with pytest.raises(MinecraftTargetLockMetadataError, match="permissions"):
            load_minecraft_target_storage_qualification(receipt.receipt_path)


def test_boot_provider_exception_keeps_positive_storage_observation_unavailable(tmp_path):
    crashed = _lock(tmp_path, "attempt-boot-provider-error").acquire()
    _crash_close_without_release(crashed)
    initial = _predecessor_status(crashed)
    receipt = _test_storage_qualification(crashed)
    assert _acknowledge_predecessor(
        crashed, initial, storage_qualification=receipt
    ).acknowledged
    run_lock_module._BOOT_ID_PROVIDER = lambda: (_ for _ in ()).throw(RuntimeError("boot id unavailable"))
    observed = _predecessor_status(crashed, receipt)
    assert observed.status is MinecraftTargetPredecessorHistoryStatus.AMBIGUOUS


@pytest.mark.parametrize("mismatch", ["entry_device", "fd_device"])
def test_qualified_layout_rejects_sidecar_entry_or_fd_device_mismatch_before_ack_mutation(
    tmp_path, monkeypatch, mismatch,
):
    crashed = _lock(tmp_path, f"attempt-qualified-layout-{mismatch}").acquire()
    _crash_close_without_release(crashed)
    inspected = _predecessor_status(crashed)
    receipt = _test_storage_qualification(crashed)
    metadata_before = crashed.path.read_bytes()
    history_path = crashed.path.with_suffix(".history")
    history_before = history_path.read_bytes()
    real_stat = os.stat
    real_fstat = os.fstat

    if mismatch == "entry_device":
        target_name = history_path.name

        def foreign_entry_device(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if path == target_name and kwargs.get("dir_fd") is not None:
                return SimpleNamespace(
                    st_dev=result.st_dev + 1, st_ino=result.st_ino, st_mode=result.st_mode,
                )
            return result

        monkeypatch.setattr(run_lock_module.os, "stat", foreign_entry_device)
    else:
        history_stat = real_stat(history_path, follow_symlinks=False)

        def foreign_fd_device(fd):
            result = real_fstat(fd)
            if (result.st_dev, result.st_ino) == (history_stat.st_dev, history_stat.st_ino):
                return SimpleNamespace(
                    st_dev=result.st_dev + 1, st_ino=result.st_ino, st_mode=result.st_mode,
                )
            return result

        monkeypatch.setattr(run_lock_module.os, "fstat", foreign_fd_device)

    outcome = acknowledge_minecraft_target_predecessor(
        lock_root=crashed.lock_root, host=crashed.host, port=crashed.port,
        expected=inspected.token, acknowledge_target_safe=True, acknowledge_whole_prefix=True,
        reason="qualified layout mismatch must fail before mutation", operator="pytest-layout",
        storage_qualification=receipt, reconcile_unknown_history=True,
    )
    assert outcome.acknowledged is False
    assert crashed.path.read_bytes() == metadata_before
    assert history_path.read_bytes() == history_before
    assert not crashed.path.with_suffix(".history-clear-pending").exists()


def test_acknowledgement_without_storage_qualification_rejects_safely(tmp_path):
    crashed = _lock(tmp_path, "attempt-ack-no-storage").acquire()
    _crash_close_without_release(crashed)
    inspected = _predecessor_status(crashed)
    with pytest.raises(ValueError, match="storage qualification"):
        acknowledge_minecraft_target_predecessor(
            lock_root=crashed.lock_root, host=crashed.host, port=crashed.port,
            expected=inspected.token, acknowledge_target_safe=True, acknowledge_whole_prefix=True,
            reason="target checked", operator="operator-test",
        )
    assert _predecessor_status(crashed).status is MinecraftTargetPredecessorHistoryStatus.UNRESOLVED


def test_storage_qualification_object_is_immutable_loader_only_and_boot_required(tmp_path, monkeypatch):
    lock = _lock(tmp_path, "attempt-storage-object")
    receipt = _test_storage_qualification(lock)
    assert isinstance(receipt, MinecraftTargetStorageQualification)
    with pytest.raises(FrozenInstanceError):
        receipt.storage_profile_id = "caller-asserted-profile"
    with pytest.raises(TypeError, match="receipt loader"):
        MinecraftTargetStorageQualification(
            artifact_id=receipt.artifact_id,
            artifact_version=receipt.artifact_version,
            storage_profile_id=receipt.storage_profile_id,
            storage_profile_version=receipt.storage_profile_version,
            receipt_id=receipt.receipt_id,
            boot_id=receipt.boot_id,
            qualified_root_absolute_path=receipt.qualified_root_absolute_path,
            qualified_root_dev=receipt.qualified_root_dev,
            qualified_root_ino=receipt.qualified_root_ino,
            qualified_filesystem_device=receipt.qualified_filesystem_device,
            capabilities=receipt.capabilities,
            issuer_audit_id=receipt.issuer_audit_id,
            detached_artifact_sha256=receipt.detached_artifact_sha256,
            receipt_path=receipt.receipt_path,
            receipt_identity=receipt.receipt_identity,
            receipt_file_digest=receipt.receipt_file_digest,
            _validation_marker=None,
        )
    monkeypatch.setattr(run_lock_module, "_BOOT_ID_PROVIDER", lambda: None)
    with pytest.raises(MinecraftTargetLockMetadataError, match="boot identity"):
        load_minecraft_target_storage_qualification(receipt.receipt_path)


@pytest.mark.parametrize("legacy_schema", [1, 2])
def test_acquired_legacy_migration_records_owner_and_unknown_gap(tmp_path, legacy_schema):
    lock = _lock(tmp_path, f"attempt-migrate-acquired-{legacy_schema}")
    _write_schema_v1_metadata(
        lock, status="acquired", attempt_id=f"legacy-owner-{legacy_schema}", pid=99999999
    ) if legacy_schema == 1 else None
    if legacy_schema == 2:
        lock.path.parent.mkdir(parents=True, exist_ok=True)
        legacy = _schema_v2_metadata(lock, status="acquired")
        legacy["attempt_id"] = "legacy-v2-owner"
        legacy["pid"] = 99999999
        lock.path.write_text(json.dumps(legacy), encoding="utf-8")
    with lock:
        observed = lock.retained_predecessor_snapshot()
        assert observed.status is MinecraftTargetPredecessorHistoryStatus.UNRESOLVED
        assert observed.first["attempt_id"] == (f"legacy-owner-{legacy_schema}" if legacy_schema == 1 else "legacy-v2-owner")
        history = run_lock_module._validate_history(
            run_lock_module._strict_json(lock.path.with_suffix(".history").read_bytes(), canonical=True)
        )
        assert history["observation_count"] == 1
        assert history["gaps"]["count"] > 0


def test_oversized_metadata_and_history_are_nonclean(tmp_path):
    lock = _lock(tmp_path, "attempt-oversized-record").acquire()
    _crash_close_without_release(lock)
    lock.path.write_bytes(b" " * (run_lock_module._MAX_RECORD_BYTES + 1))
    observed = _predecessor_status(lock)
    assert observed.status is MinecraftTargetPredecessorHistoryStatus.HISTORY_CORRUPT

    # Reconstruct an acquired record and make only its sidecar oversized.
    lock2 = _lock(tmp_path / "history-only", "attempt-oversized-history").acquire()
    _crash_close_without_release(lock2)
    lock2.path.with_suffix(".history").write_bytes(b"x" * (run_lock_module._MAX_RECORD_BYTES + 1))
    assert _predecessor_status(lock2).status is MinecraftTargetPredecessorHistoryStatus.HISTORY_CORRUPT


@pytest.mark.parametrize("corruption", ["duplicate", "noncanonical", "unknown", "checksum"])
def test_corrupt_history_schema_or_digest_is_never_clean(tmp_path, corruption):
    lock = _lock(tmp_path, f"attempt-history-corruption-{corruption}").acquire()
    _crash_close_without_release(lock)
    history_path = lock.path.with_suffix(".history")
    raw = history_path.read_bytes()
    if corruption == "duplicate":
        changed = raw.rstrip(b"\n")[:-1] + b',"state":"UNRESOLVED"}\n'
    elif corruption == "noncanonical":
        changed = raw + b" "
    else:
        history = run_lock_module._strict_json(raw, canonical=True)
        if corruption == "unknown":
            history["extra"] = True
            history = run_lock_module._seal(history)
        else:
            history["checksum"] = "0" * 64
        changed = run_lock_module._canonical_bytes(history)
    history_path.write_bytes(changed)
    assert _predecessor_status(lock).status is MinecraftTargetPredecessorHistoryStatus.HISTORY_CORRUPT


def test_acquisition_displacement_completes_short_writes(tmp_path, monkeypatch):
    previous = _lock(tmp_path, "attempt-short-write-previous").acquire()
    _crash_close_without_release(previous)
    real_write = os.write
    short_calls = []

    def short_write(fd, data):
        view = memoryview(data)
        amount = max(1, len(view) // 4)
        written = real_write(fd, view[:amount])
        short_calls.append(written)
        return written

    contender = _lock(tmp_path, "attempt-short-write-next")
    monkeypatch.setattr(run_lock_module.os, "write", short_write)
    contender.acquire()
    assert short_calls
    history = run_lock_module._validate_history(
        run_lock_module._strict_json(contender.path.with_suffix(".history").read_bytes(), canonical=True)
    )
    assert history["state"] == "UNRESOLVED"
    assert history["first"]["attempt_id"] == previous.attempt_id
    contender.release()


def test_acquisition_temporary_readback_failure_preserves_old_metadata(tmp_path, monkeypatch):
    previous = _lock(tmp_path, "attempt-readback-previous").acquire()
    _crash_close_without_release(previous)
    old_metadata = previous.path.read_bytes()
    real_read_named = run_lock_module._HistoryIO._read_named
    injected = []

    def mismatch_temporary(self, name):
        raw = real_read_named(self, name)
        if ".tmp-" in name and not injected:
            injected.append(name)
            return raw + b" "
        return raw

    monkeypatch.setattr(run_lock_module._HistoryIO, "_read_named", mismatch_temporary)
    contender = _lock(tmp_path, "attempt-readback-next")
    with pytest.raises(MinecraftTargetLockError):
        contender.acquire()
    assert injected
    assert contender.acquired is False
    assert previous.path.read_bytes() == old_metadata


def test_acquisition_metadata_tear_keeps_predecessor_u_and_does_not_rewrite_epoch(tmp_path, monkeypatch):
    previous = _lock(tmp_path, "attempt-tear-previous").acquire()
    _crash_close_without_release(previous)
    old_metadata = previous.path.read_bytes()
    contender = _lock(tmp_path, "attempt-tear-next")
    real_write = run_lock_module._write_stream_metadata

    def tear_new_acquisition(stream, payload):
        if payload.get("attempt_id") == contender.attempt_id:
            raw = run_lock_module._canonical_bytes(payload)
            stream.seek(0)
            stream.truncate()
            run_lock_module._write_complete(stream.fileno(), raw[: max(1, len(raw) // 2)])
            os.fsync(stream.fileno())
            raise OSError("injected post-witness acquisition tear")
        return real_write(stream, payload)

    monkeypatch.setattr(run_lock_module, "_write_stream_metadata", tear_new_acquisition)
    with pytest.raises(MinecraftTargetLockUnavailableError):
        contender.acquire()
    history_path = previous.path.with_suffix(".history")
    history_bytes = history_path.read_bytes()
    history = run_lock_module._validate_history(run_lock_module._strict_json(history_bytes, canonical=True))
    assert history["state"] == "UNRESOLVED"
    assert history["first"]["attempt_id"] == previous.attempt_id
    assert history["source"]["metadata_digest"] == run_lock_module._digest(old_metadata)
    monkeypatch.undo()
    another = _lock(tmp_path, "attempt-tear-no-revival")
    with pytest.raises(MinecraftTargetLockMetadataError):
        another.acquire()
    assert history_path.read_bytes() == history_bytes


@pytest.mark.parametrize(
    "cut", ["release_marker", "released_write", "released_tear", "marker_unlink",
            "marker_unlink_dirsync", "after_marker_unlink_before_unlock"]
)
def test_ordered_release_cuts_keep_inherited_prefix_nonclean(tmp_path, monkeypatch, cut):
    previous = _lock(tmp_path, f"attempt-release-cut-prior-{cut}").acquire()
    _crash_close_without_release(previous)
    owner = _lock(tmp_path, f"attempt-release-cut-current-{cut}").acquire()
    if cut == "release_marker":
        real_persist = run_lock_module._persist_uncertainty_marker
        failed = []

        def fail_release_in_progress(path, **kwargs):
            if kwargs.get("error_type") == "ReleaseInProgress" and not failed:
                failed.append(True)
                return False
            return real_persist(path, **kwargs)

        monkeypatch.setattr(run_lock_module, "_persist_uncertainty_marker", fail_release_in_progress)
    elif cut == "released_write":
        monkeypatch.setattr(owner, "_write_metadata", lambda _payload: (_ for _ in ()).throw(OSError("release write cut")))
    elif cut == "released_tear":
        real_write = run_lock_module._write_stream_metadata

        def tear_released(stream, payload):
            if payload.get("status") == "released":
                stream.seek(0)
                stream.truncate()
                stream.write("{")
                stream.flush()
                os.fsync(stream.fileno())
                raise OSError("release metadata tear")
            return real_write(stream, payload)

        monkeypatch.setattr(run_lock_module, "_write_stream_metadata", tear_released)
    elif cut == "marker_unlink":
        monkeypatch.setattr(run_lock_module, "_remove_uncertainty_marker", lambda _path: (_ for _ in ()).throw(OSError("unlink cut")))
    elif cut == "marker_unlink_dirsync":
        real_sync = run_lock_module._fsync_parent_directory
        failed = []

        def fail_unlink_sync(path):
            if path.name.endswith(".uncertain") and not failed:
                failed.append(True)
                raise OSError("marker unlink directory fsync cut")
            return real_sync(path)

        monkeypatch.setattr(run_lock_module, "_fsync_parent_directory", fail_unlink_sync)
    else:
        real_flock = run_lock_module.fcntl.flock
        failed = []

        def fail_unlock(fd, operation):
            if operation == run_lock_module.fcntl.LOCK_UN and not failed:
                failed.append(True)
                raise OSError("post-unlink pre-unlock cut")
            return real_flock(fd, operation)

        monkeypatch.setattr(run_lock_module.fcntl, "flock", fail_unlock)

    result = owner.release()
    assert result.status in {MinecraftTargetLockReleaseStatus.UNCERTAIN, MinecraftTargetLockReleaseStatus.FAILED}
    observed = _predecessor_status(owner)
    assert observed.status is not MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN
    assert observed.first is not None


def test_context_exception_and_release_failure_preserve_inherited_u(tmp_path, monkeypatch):
    previous = _lock(tmp_path, "attempt-context-u-previous").acquire()
    _crash_close_without_release(previous)
    owner = _lock(tmp_path, "attempt-context-u-current")

    with pytest.raises(RuntimeError, match="body exception remains primary"):
        with owner:
            monkeypatch.setattr(owner, "_write_metadata", lambda _payload: (_ for _ in ()).throw(OSError("release failure")))
            raise RuntimeError("body exception remains primary")
    assert owner.last_release_outcome.status in {
        MinecraftTargetLockReleaseStatus.UNCERTAIN, MinecraftTargetLockReleaseStatus.FAILED
    }
    observed = _predecessor_status(owner)
    assert observed.status is not MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN
    assert observed.first["attempt_id"] == previous.attempt_id
    assert observed.uncertain is True


def test_corrupt_quarantine_requires_force_clear_before_predecessor_ack(tmp_path):
    owner = _lock(tmp_path, "attempt-corrupt-quarantine-order").acquire()
    owner.quarantine(run_name="unsafe", reasons=["corruption-order"], diagnostics={})
    owner.release()
    owner.path.write_bytes(b"{")
    corrupted = _predecessor_status(owner)
    assert corrupted.status is MinecraftTargetPredecessorHistoryStatus.HISTORY_CORRUPT
    receipt = _test_storage_qualification(owner)
    refused = _acknowledge_predecessor(owner, corrupted, storage_qualification=receipt,
                                       reconcile_unknown_history=True)
    assert refused.acknowledged is False
    assert _predecessor_status(owner).status is MinecraftTargetPredecessorHistoryStatus.HISTORY_CORRUPT

    clear_minecraft_target_quarantine(
        lock_root=owner.lock_root, host=owner.host, port=owner.port,
        reason="Corrupt quarantine record independently reviewed", acknowledge_target_safe=True,
        force_corrupt=True,
    )
    after_clear = _predecessor_status(owner)
    assert after_clear.status is not MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN
    history = run_lock_module._validate_history(
        run_lock_module._strict_json(owner.path.with_suffix(".history").read_bytes(), canonical=True)
    )
    assert history["gaps"]["count"] > 0
    acknowledged = _acknowledge_predecessor(
        owner, after_clear, storage_qualification=receipt, reconcile_unknown_history=True
    )
    assert acknowledged.acknowledged is True
    assert acknowledged.snapshot.status is MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN


@pytest.mark.parametrize("barrier", ["write_error", "zero_write", "temporary_fsync",
                                      "temporary_readback", "replace", "directory_fsync",
                                      "installed_readback"])
def test_pending_clear_low_level_barriers_return_uncertain_and_never_clean(tmp_path, monkeypatch, barrier):
    previous = _lock(tmp_path, f"attempt-pending-cut-prior-{barrier}").acquire()
    _crash_close_without_release(previous)
    inspected = _predecessor_status(previous)
    receipt = _test_storage_qualification(previous)
    fired = []
    io_type = run_lock_module._HistoryIO

    if barrier == "write_error":
        real_write = run_lock_module._write_complete

        def fail_first_write(fd, raw):
            if not fired:
                fired.append(True)
                raise OSError("pending low-level write error")
            return real_write(fd, raw)

        monkeypatch.setattr(run_lock_module, "_write_complete", fail_first_write)
    elif barrier == "zero_write":
        real_write = os.write

        def no_progress(fd, raw):
            if not fired:
                fired.append(True)
                return 0
            return real_write(fd, raw)

        monkeypatch.setattr(run_lock_module.os, "write", no_progress)
    elif barrier == "temporary_fsync":
        real_sync = os.fsync

        def fail_first_sync(fd):
            if not fired:
                fired.append(True)
                raise OSError("pending temp fsync")
            return real_sync(fd)

        monkeypatch.setattr(run_lock_module.os, "fsync", fail_first_sync)
    elif barrier == "temporary_readback":
        real_read_named = io_type._read_named

        def mismatch_temp(self, name):
            raw = real_read_named(self, name)
            if ".history-clear-pending.tmp-" in name and not fired:
                fired.append(True)
                return raw + b" "
            return raw

        monkeypatch.setattr(io_type, "_read_named", mismatch_temp)
    elif barrier == "replace":
        real_replace = os.replace

        def fail_pending_replace(source, target, *args, **kwargs):
            if str(target).endswith(".history-clear-pending") and not fired:
                fired.append(True)
                raise OSError("pending replace")
            return real_replace(source, target, *args, **kwargs)

        monkeypatch.setattr(run_lock_module.os, "replace", fail_pending_replace)
    elif barrier == "directory_fsync":
        real_sync = run_lock_module._fsync_parent_directory

        def fail_pending_directory(path):
            if path.name.endswith(".history-clear-pending") and not fired:
                fired.append(True)
                raise OSError("pending directory fsync")
            return real_sync(path)

        monkeypatch.setattr(run_lock_module, "_fsync_parent_directory", fail_pending_directory)
    else:
        real_read = io_type.read

        def mismatch_pending(self, suffix):
            raw = real_read(self, suffix)
            if suffix == "history-clear-pending" and raw is not None and not fired:
                fired.append(True)
                return raw + b" "
            return raw

        monkeypatch.setattr(io_type, "read", mismatch_pending)

    outcome = _acknowledge_predecessor(previous, inspected, storage_qualification=receipt,
                                        reconcile_unknown_history=True)
    assert fired
    assert outcome.status is MinecraftTargetPredecessorAcknowledgementStatus.UNCERTAIN
    assert outcome.acknowledged is False
    monkeypatch.undo()
    on_disk = _predecessor_status(previous, receipt)
    assert on_disk.status is MinecraftTargetPredecessorHistoryStatus.DURABILITY_UNCERTAIN
    recovered = _acknowledge_predecessor(
        previous, on_disk, storage_qualification=receipt, reconcile_unknown_history=True
    )
    assert recovered.acknowledged is True
    assert recovered.snapshot.status is MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN


def test_acknowledgement_metadata_tear_keeps_pending_and_never_returns_clean(tmp_path, monkeypatch):
    previous = _lock(tmp_path, "attempt-ack-tear-prior").acquire()
    _crash_close_without_release(previous)
    inspected = _predecessor_status(previous)
    receipt = _test_storage_qualification(previous)
    real_write = run_lock_module._write_stream_metadata

    def tear_reconciled(stream, payload):
        if payload.get("status") == "reconciled":
            stream.seek(0)
            stream.truncate()
            stream.write("{")
            stream.flush()
            os.fsync(stream.fileno())
            raise OSError("ack metadata tear")
        return real_write(stream, payload)

    monkeypatch.setattr(run_lock_module, "_write_stream_metadata", tear_reconciled)
    outcome = _acknowledge_predecessor(previous, inspected, storage_qualification=receipt,
                                        reconcile_unknown_history=True)
    assert outcome.status is MinecraftTargetPredecessorAcknowledgementStatus.UNCERTAIN
    assert previous.path.with_suffix(".history-clear-pending").exists()
    monkeypatch.undo()
    assert _predecessor_status(previous, receipt).status is MinecraftTargetPredecessorHistoryStatus.DURABILITY_UNCERTAIN


def test_pending_publication_completes_short_writes_and_is_individually_observed(tmp_path, monkeypatch):
    previous = _lock(tmp_path, "attempt-pending-short-write").acquire()
    _crash_close_without_release(previous)
    inspected = _predecessor_status(previous)
    receipt = _test_storage_qualification(previous)
    real_write = os.write
    real_publish = run_lock_module._HistoryIO.publish
    short_writes = []
    pending_publications = []

    def short_write(fd, raw):
        view = memoryview(raw)
        written = real_write(fd, view[:max(1, len(view) // 3)])
        short_writes.append(written)
        return written

    def record_pending(self, suffix, payload):
        if suffix == "history-clear-pending":
            pending_publications.append(dict(payload))
        return real_publish(self, suffix, payload)

    monkeypatch.setattr(run_lock_module.os, "write", short_write)
    monkeypatch.setattr(run_lock_module._HistoryIO, "publish", record_pending)
    result = _acknowledge_predecessor(previous, inspected, storage_qualification=receipt,
                                      reconcile_unknown_history=True)
    assert short_writes
    assert pending_publications
    assert result.acknowledged is True
    assert result.snapshot.status is MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN


@pytest.mark.parametrize("fault", ["metadata_fsync", "metadata_readback"])
def test_acknowledgement_metadata_fsync_and_readback_cuts_remain_nonclean(tmp_path, monkeypatch, fault):
    previous = _lock(tmp_path, f"attempt-ack-metadata-{fault}").acquire()
    _crash_close_without_release(previous)
    inspected = _predecessor_status(previous)
    receipt = _test_storage_qualification(previous)
    lock_stat = os.stat(previous.path, follow_symlinks=False)
    fired = []
    if fault == "metadata_fsync":
        real_fsync = os.fsync

        def fail_lock_fsync(fd):
            identity = os.fstat(fd)
            if ((identity.st_dev, identity.st_ino) == (lock_stat.st_dev, lock_stat.st_ino)
                    and not fired):
                fired.append(True)
                raise OSError("ack metadata fsync cut")
            return real_fsync(fd)

        monkeypatch.setattr(run_lock_module.os, "fsync", fail_lock_fsync)
    else:
        real_pread = os.pread
        real_metadata_write = run_lock_module._write_stream_metadata
        metadata_write_started = []

        def mark_metadata_write(stream, payload):
            if payload.get("status") == "reconciled":
                metadata_write_started.append(True)
            return real_metadata_write(stream, payload)

        def mismatch_lock_readback(fd, size, offset=0):
            raw = real_pread(fd, size, offset)
            identity = os.fstat(fd)
            if ((identity.st_dev, identity.st_ino) == (lock_stat.st_dev, lock_stat.st_ino)
                    and metadata_write_started and not fired):
                fired.append(True)
                return raw + b" "
            return raw

        monkeypatch.setattr(run_lock_module, "_write_stream_metadata", mark_metadata_write)
        monkeypatch.setattr(run_lock_module.os, "pread", mismatch_lock_readback)
    outcome = _acknowledge_predecessor(previous, inspected, storage_qualification=receipt,
                                       reconcile_unknown_history=True)
    assert fired
    assert outcome.status is MinecraftTargetPredecessorAcknowledgementStatus.UNCERTAIN
    monkeypatch.undo()
    assert _predecessor_status(previous, receipt).status is MinecraftTargetPredecessorHistoryStatus.DURABILITY_UNCERTAIN


def test_final_pending_unlink_fsync_failure_is_uncertain_then_fresh_parent_recovers_clean(tmp_path, monkeypatch):
    previous = _lock(tmp_path, "attempt-final-pending-fsync").acquire()
    _crash_close_without_release(previous)
    inspected = _predecessor_status(previous)
    receipt = _test_storage_qualification(previous)
    real_unlink = os.unlink
    real_sync = run_lock_module._fsync_parent_directory
    real_publish = run_lock_module._HistoryIO.publish
    pending_unlinked = []
    pending_publications = []

    def mark_unlink(path, *args, **kwargs):
        result = real_unlink(path, *args, **kwargs)
        if Path(path).name.endswith(".history-clear-pending"):
            pending_unlinked.append(True)
        return result

    def fail_final_dirsync(path):
        if path.name.endswith(".history-clear-pending") and pending_unlinked:
            raise OSError("final pending unlink directory fsync cut")
        return real_sync(path)

    def fail_restore(self, suffix, payload):
        if suffix == "history-clear-pending":
            pending_publications.append(True)
            if pending_unlinked:
                raise OSError("pending restoration cut")
        return real_publish(self, suffix, payload)

    monkeypatch.setattr(run_lock_module.os, "unlink", mark_unlink)
    monkeypatch.setattr(run_lock_module, "_fsync_parent_directory", fail_final_dirsync)
    monkeypatch.setattr(run_lock_module._HistoryIO, "publish", fail_restore)
    outcome = _acknowledge_predecessor(previous, inspected, storage_qualification=receipt,
                                       reconcile_unknown_history=True)
    assert pending_unlinked
    assert outcome.status is MinecraftTargetPredecessorAcknowledgementStatus.UNCERTAIN
    assert outcome.retained_lock is True
    assert not previous.path.with_suffix(".history-clear-pending").exists()
    contender = _lock(tmp_path, "attempt-final-pending-contender")
    with pytest.raises(MinecraftTargetLockBusyError):
        contender.acquire()

    retained = next(
        (transaction_id, io) for transaction_id, io in run_lock_module._UNVERIFIED_ACK_LOCKS.items()
        if io.path == previous.path.absolute()
    )
    transaction_id, io = retained
    if io.stream is not None:
        fcntl.flock(io.stream.fileno(), fcntl.LOCK_UN)
        io.stream.close()
    io.close()
    run_lock_module._UNVERIFIED_ACK_LOCKS.pop(transaction_id, None)
    monkeypatch.undo()
    observed = _predecessor_status(previous, receipt)
    assert observed.status is MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN


def test_pending_restore_failure_retains_exclusive_flock(tmp_path, monkeypatch):
    previous = _lock(tmp_path, "attempt-pending-held-prior").acquire()
    _crash_close_without_release(previous)
    inspected = _predecessor_status(previous)
    receipt = _test_storage_qualification(previous)
    real_publish = run_lock_module._HistoryIO.publish
    calls = []

    def fail_pending_publish(self, suffix, payload):
        if suffix == "history-clear-pending":
            calls.append("pending")
            raise OSError("pending publication/restoration unavailable")
        return real_publish(self, suffix, payload)

    monkeypatch.setattr(run_lock_module._HistoryIO, "publish", fail_pending_publish)
    outcome = _acknowledge_predecessor(previous, inspected, storage_qualification=receipt,
                                        reconcile_unknown_history=True)
    assert outcome.status is MinecraftTargetPredecessorAcknowledgementStatus.UNCERTAIN
    assert outcome.retained_lock is True
    assert len(calls) >= 2
    contender = _lock(tmp_path, "attempt-pending-held-contender")
    with pytest.raises(MinecraftTargetLockBusyError):
        contender.acquire()
    retained = [
        (transaction_id, io) for transaction_id, io in run_lock_module._UNVERIFIED_ACK_LOCKS.items()
        if io.path == previous.path.absolute()
    ]
    assert len(retained) == 1
    transaction_id, io = retained[0]
    if io.stream is not None:
        fcntl.flock(io.stream.fileno(), fcntl.LOCK_UN)
        io.stream.close()
    io.close()
    run_lock_module._UNVERIFIED_ACK_LOCKS.pop(transaction_id, None)


def _assert_deeply_immutable(value):
    if isinstance(value, Mapping):
        if value:
            key = next(iter(value))
            with pytest.raises(TypeError):
                value[key] = object()
        for nested in value.values():
            _assert_deeply_immutable(nested)
    elif isinstance(value, (tuple, frozenset)):
        for nested in value:
            _assert_deeply_immutable(nested)


def _assert_v3_metadata_history_pointer_coherent(lock):
    metadata = run_lock_module._strict_json(lock.path.read_bytes(), canonical=True)
    metadata = run_lock_module._parse_lock_metadata(
        metadata, expected_key=lock.key, expected_host=lock.host, expected_port=lock.port
    )
    history = run_lock_module._validate_history(
        run_lock_module._strict_json(lock.path.with_suffix(".history").read_bytes(), canonical=True)
    )
    assert metadata["history_pointer"] == run_lock_module._history_pointer(history)
    assert metadata["writer_epoch"] == history["writer_epoch"]
    return metadata, history


def _create_u_ahead_acquisition(lock_root, monkeypatch, attempt_suffix):
    previous = MinecraftTargetLock(
        lock_root=lock_root, host="127.0.0.1", port=25565, world_id="world-a",
        attempt_id=f"attempt-u-ahead-previous-{attempt_suffix}",
    ).acquire()
    _crash_close_without_release(previous)
    old_metadata = previous.path.read_bytes()
    real_sync = run_lock_module._fsync_parent_directory
    failed = []

    def fail_history_dirsync(path):
        if str(path).endswith(".history") and not failed:
            failed.append(True)
            raise OSError("injected U-ahead history directory fsync failure")
        return real_sync(path)

    contender = MinecraftTargetLock(
        lock_root=lock_root, host="127.0.0.1", port=25565, world_id="world-a",
        attempt_id=f"attempt-u-ahead-contender-{attempt_suffix}",
    )
    with monkeypatch.context() as patch:
        patch.setattr(run_lock_module, "_fsync_parent_directory", fail_history_dirsync)
        with pytest.raises(MinecraftTargetLockError):
            contender.acquire()
    assert failed
    assert contender.acquired is False
    assert contender._stream is None
    assert previous.path.read_bytes() == old_metadata
    return previous


def test_acquisition_metadata_readback_failure_never_reports_success(tmp_path, monkeypatch):
    previous = _lock(tmp_path, "attempt-metadata-readback-prior").acquire()
    _crash_close_without_release(previous)
    contender = _lock(tmp_path, "attempt-metadata-readback-current")
    lock_stat = os.stat(previous.path, follow_symlinks=False)
    real_writer = run_lock_module._write_stream_metadata
    real_pread = os.pread
    writer_started, readback_failed = [], []

    def mark_acquisition_write(stream, payload):
        if payload.get("attempt_id") == contender.attempt_id:
            writer_started.append(True)
        return real_writer(stream, payload)

    def mismatch_acquisition_readback(fd, size, offset=0):
        raw = real_pread(fd, size, offset)
        identity = os.fstat(fd)
        if (writer_started and not readback_failed
                and (identity.st_dev, identity.st_ino) == (lock_stat.st_dev, lock_stat.st_ino)):
            readback_failed.append(True)
            return raw + b" "
        return raw

    monkeypatch.setattr(run_lock_module, "_write_stream_metadata", mark_acquisition_write)
    monkeypatch.setattr(run_lock_module.os, "pread", mismatch_acquisition_readback)
    with pytest.raises(MinecraftTargetLockMetadataError, match="metadata readback mismatch"):
        contender.acquire()
    assert writer_started and readback_failed
    assert contender.acquired is False
    assert contender._stream is None
    history = run_lock_module._validate_history(
        run_lock_module._strict_json(previous.path.with_suffix(".history").read_bytes(), canonical=True)
    )
    assert history["state"] == "UNRESOLVED"
    assert history["first"]["attempt_id"] == previous.attempt_id


def _close_unverified_ack_locks_for_restart(lock):
    matches = [
        (transaction_id, io)
        for transaction_id, io in run_lock_module._UNVERIFIED_ACK_LOCKS.items()
        if io.path == lock.path.absolute()
    ]
    for transaction_id, io in matches:
        if io.stream is not None:
            io.stream.close()
        io.close()
        run_lock_module._UNVERIFIED_ACK_LOCKS.pop(transaction_id, None)
    return len(matches)


def test_predecessor_history_survives_generic_cycles_without_laundering_u(tmp_path):
    crashed = _lock(tmp_path, "attempt-history-prefix-a").acquire()
    _crash_close_without_release(crashed)
    first = _predecessor_status(crashed)
    first_owner = first.first
    assert first.status is MinecraftTargetPredecessorHistoryStatus.UNRESOLVED
    assert first_owner is not None
    for index in range(4):
        cycle = _lock(tmp_path, f"attempt-history-prefix-cycle-{index}").acquire()
        assert cycle.release().status is MinecraftTargetLockReleaseStatus.VERIFIED_RELEASED
        observed = _predecessor_status(cycle)
        assert observed.status is MinecraftTargetPredecessorHistoryStatus.UNRESOLVED
        assert observed.first == first_owner


@pytest.mark.parametrize(("pid_case", "alive"), [("same", None), ("live", True), ("reused", True), ("dead", False)])
def test_predecessor_cleanliness_is_independent_of_pid_liveness(tmp_path, monkeypatch, pid_case, alive):
    crashed = _lock(tmp_path, f"attempt-history-pid-{pid_case}").acquire()
    _crash_close_without_release(crashed)
    if alive is None:
        observed = _predecessor_status(crashed)
    else:
        with monkeypatch.context() as patch:
            patch.setattr(run_lock_module, "_pid_exists", lambda _pid: alive)
            observed = _predecessor_status(crashed)
    assert observed.status is MinecraftTargetPredecessorHistoryStatus.UNRESOLVED


def test_unresolved_history_remains_nonblocking_for_generic_lock_admission(tmp_path):
    crashed = _lock(tmp_path, "attempt-generic-admission-u").acquire()
    _crash_close_without_release(crashed)
    observed = _predecessor_status(crashed)
    generic = read_minecraft_target_lock_status(
        lock_root=crashed.lock_root, host=crashed.host, port=crashed.port
    )
    assert observed.status is MinecraftTargetPredecessorHistoryStatus.UNRESOLVED
    assert observed.uncertain is False
    assert generic["blocking"] is False


def test_generic_uncertainty_clear_preserves_predecessor_first(tmp_path):
    crashed = _lock(tmp_path, "attempt-generic-clear-keeps-u").acquire()
    _crash_close_without_release(crashed)
    before = _predecessor_status(crashed)
    marker = crashed.path.with_suffix(".uncertain")
    marker.write_text(json.dumps({"schema_version": 1, "status": "uncertain", "attempt_id": "u-state"}))
    clear_minecraft_target_quarantine(
        lock_root=crashed.lock_root, host=crashed.host, port=crashed.port,
        reason="Independent U blocker reviewed", acknowledge_target_safe=True,
    )
    after = _predecessor_status(crashed)
    assert not os.path.lexists(marker)
    assert after.status is MinecraftTargetPredecessorHistoryStatus.UNRESOLVED
    assert after.first == before.first


@pytest.mark.parametrize("legacy", ["v1", "v2", "blank"])
def test_legacy_metadata_stays_unknown_until_explicit_target_safe_reconciliation(tmp_path, legacy):
    lock = _lock(tmp_path, f"attempt-legacy-unknown-{legacy}")
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    if legacy == "v1":
        _write_schema_v1_metadata(lock, status="released", attempt_id="legacy-released")
    elif legacy == "v2":
        lock.path.write_text(json.dumps(_schema_v2_metadata(lock, status="released")), encoding="utf-8")
    else:
        lock.path.write_bytes(b"")
    observed = _predecessor_status(lock)
    assert observed.status is MinecraftTargetPredecessorHistoryStatus.LEGACY_UNKNOWN
    declined = _acknowledge_predecessor(lock, observed, reconcile_unknown_history=False)
    assert declined.acknowledged is False
    assert _predecessor_status(lock).status is MinecraftTargetPredecessorHistoryStatus.LEGACY_UNKNOWN
    reconciled = _acknowledge_predecessor(lock, observed, reconcile_unknown_history=True)
    assert reconciled.acknowledged is True
    assert reconciled.snapshot.status is MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN
    qualification = _test_storage_qualification(lock)
    assert _predecessor_status(lock, qualification).status is MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN


def test_downgraded_legacy_metadata_cannot_revive_acknowledged_epoch(tmp_path):
    lock = _lock(tmp_path, "attempt-downgrade-epoch").acquire()
    _crash_close_without_release(lock)
    qualification = _test_storage_qualification(lock)
    initial = _predecessor_status(lock)
    acknowledged = _acknowledge_predecessor(
        lock, initial, storage_qualification=qualification, reconcile_unknown_history=True
    )
    assert acknowledged.acknowledged
    old_epoch = acknowledged.snapshot.writer_epoch
    lock.path.write_text(json.dumps(_schema_v2_metadata(lock, status="cleared")), encoding="utf-8")
    downgraded = _predecessor_status(lock)
    assert downgraded.status is MinecraftTargetPredecessorHistoryStatus.LEGACY_UNKNOWN
    declined = _acknowledge_predecessor(
        lock, downgraded, storage_qualification=qualification, reconcile_unknown_history=False
    )
    assert not declined.acknowledged
    upgrade = _lock(tmp_path, "attempt-new-downgrade-epoch").acquire()
    try:
        assert upgrade.retained_predecessor_snapshot().writer_epoch != old_epoch
    finally:
        upgrade.release()


def test_absent_lock_cas_is_qualified_and_bound_to_its_root(tmp_path):
    inspected_lock = _lock(tmp_path, "attempt-absent-lock-cas")
    inspected_lock.lock_root.mkdir(parents=True)
    absent = _predecessor_status(inspected_lock)
    qualification = _test_storage_qualification(inspected_lock)
    wrong_root = tmp_path / "different-lock-root"
    outcome = acknowledge_minecraft_target_predecessor(
        lock_root=wrong_root, host=inspected_lock.host, port=inspected_lock.port,
        expected=absent.token, acknowledge_target_safe=True, acknowledge_whole_prefix=True,
        reason="must remain bound to the inspected root", operator="pytest-root-cas",
        storage_qualification=qualification, reconcile_unknown_history=True,
    )
    assert outcome.acknowledged is False
    assert not wrong_root.exists()
    assert not inspected_lock.path.exists()


@pytest.mark.parametrize("tamper", ["missing", "corrupt", "duplicate", "noncanonical", "oversized"])
def test_history_sidecar_failures_are_not_cleanliness_evidence(tmp_path, tamper):
    crashed = _lock(tmp_path, f"attempt-history-integrity-{tamper}").acquire()
    _crash_close_without_release(crashed)
    history = crashed.path.with_suffix(".history")
    if tamper == "missing":
        history.unlink()
    elif tamper == "corrupt":
        history.write_bytes(b"{")
    elif tamper == "duplicate":
        history.write_text('{"record":{},"record":{}}', encoding="utf-8")
    elif tamper == "noncanonical":
        history.write_bytes(history.read_bytes() + b" ")
    else:
        history.write_bytes(b"x" * (run_lock_module._MAX_RECORD_BYTES + 1))
    expected = (MinecraftTargetPredecessorHistoryStatus.HISTORY_UNAVAILABLE
                if tamper == "missing" else MinecraftTargetPredecessorHistoryStatus.HISTORY_CORRUPT)
    assert _predecessor_status(crashed).status is expected


@pytest.mark.parametrize(("suffix", "kind"), [
    ("history", "symlink"), ("history", "directory"),
    ("history-clear-pending", "symlink"), ("history-clear-pending", "directory"),
])
def test_history_and_pending_sidecars_reject_symlink_or_nonregular_entries(tmp_path, suffix, kind):
    crashed = _lock(tmp_path, f"attempt-sidecar-nonregular-{suffix}-{kind}").acquire()
    _crash_close_without_release(crashed)
    path = crashed.path.with_suffix(f".{suffix}")
    if path.exists() or os.path.lexists(path):
        path.unlink()
    if kind == "symlink":
        target = tmp_path / "sidecar-target"
        target.write_bytes(b"not the history")
        path.symlink_to(target)
    else:
        path.mkdir()
    observed = _predecessor_status(crashed)
    assert observed.status is not MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN
    assert observed.error is not None or observed.status in {
        MinecraftTargetPredecessorHistoryStatus.AMBIGUOUS,
        MinecraftTargetPredecessorHistoryStatus.HISTORY_CORRUPT,
    }


@pytest.mark.parametrize("change_after_inspection", [False, True])
def test_orphan_census_is_exact_cas_and_only_explicitly_reconciled(tmp_path, change_after_inspection):
    crashed = _lock(tmp_path, f"attempt-orphan-cas-{change_after_inspection}").acquire()
    _crash_close_without_release(crashed)
    orphan = crashed.lock_root / f".{crashed.key}.history.tmp-{'a' * 32}"
    orphan.write_bytes(b"bounded orphan diagnostic")
    inspected = _predecessor_status(crashed)
    assert inspected.status is MinecraftTargetPredecessorHistoryStatus.AMBIGUOUS
    assert len(inspected.token.state["orphans"]) == 1
    if change_after_inspection:
        orphan.write_bytes(b"changed after inspection")
    outcome = _acknowledge_predecessor(crashed, inspected, reconcile_unknown_history=True)
    if change_after_inspection:
        assert outcome.acknowledged is False
        assert orphan.exists()
        assert _predecessor_status(crashed).status is not MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN
    else:
        assert outcome.acknowledged is True
        assert not orphan.exists()


@pytest.mark.parametrize("corruption", ["duplicate", "noncanonical", "unknown", "schema", "checksum"])
def test_v3_metadata_rejects_duplicate_noncanonical_open_or_bad_checksum(tmp_path, corruption):
    crashed = _lock(tmp_path, f"attempt-metadata-integrity-{corruption}").acquire()
    _crash_close_without_release(crashed)
    original = crashed.path.read_bytes()
    if corruption == "duplicate":
        changed = original.rstrip(b"\n")[:-1] + b',"status":"acquired"}\n'
    elif corruption == "noncanonical":
        changed = original + b" "
    else:
        metadata = run_lock_module._strict_json(original, canonical=True)
        if corruption == "unknown":
            metadata["unexpected"] = True
            metadata = run_lock_module._seal(metadata)
        elif corruption == "schema":
            metadata["schema_version"] = 99
            metadata = run_lock_module._seal(metadata)
        else:
            metadata["checksum"] = "0" * 64
        changed = run_lock_module._canonical_bytes(metadata)
    crashed.path.write_bytes(changed)
    assert _predecessor_status(crashed).status is MinecraftTargetPredecessorHistoryStatus.HISTORY_CORRUPT


def test_root_directory_replacement_invalidates_retained_writer_and_observer(tmp_path):
    owner = _lock(tmp_path, "attempt-root-identity-replacement").acquire()
    root = owner.lock_root
    moved = tmp_path / "moved-lock-root"
    root.rename(moved)
    try:
        with pytest.raises(MinecraftTargetLockError):
            owner.retained_predecessor_snapshot()
    finally:
        _crash_close_without_release(owner)
    root.mkdir()
    (root / owner.path.name).write_bytes((moved / owner.path.name).read_bytes())
    (root / owner.path.with_suffix(".history").name).write_bytes(
        (moved / owner.path.with_suffix(".history").name).read_bytes()
    )
    contender = _lock(tmp_path, "attempt-after-root-replacement")
    with pytest.raises(MinecraftTargetLockError):
        contender.acquire()
    assert contender.acquired is False
    assert _predecessor_status(contender).status is not MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN


def test_obsolete_but_self_consistent_history_pointer_is_rejected(tmp_path):
    crashed = _lock(tmp_path, "attempt-obsolete-history-pointer").acquire()
    _crash_close_without_release(crashed)
    sidecar = crashed.path.with_suffix(".history")
    old = sidecar.read_bytes()
    successor = _lock(tmp_path, "attempt-obsolete-history-successor").acquire()
    assert successor.release().status is MinecraftTargetLockReleaseStatus.VERIFIED_RELEASED
    assert sidecar.read_bytes() != old
    sidecar.write_bytes(old)
    assert _predecessor_status(crashed).status is MinecraftTargetPredecessorHistoryStatus.HISTORY_CORRUPT


def test_exact_u_ahead_history_retry_adopts_owner_once(tmp_path, monkeypatch):
    previous = _create_u_ahead_acquisition(tmp_path / "locks", monkeypatch, "exact-retry")
    sidecar = previous.path.with_suffix(".history")
    ahead = run_lock_module._validate_history(run_lock_module._strict_json(sidecar.read_bytes(), canonical=True))
    assert ahead["state"] == "UNRESOLVED"
    assert ahead["first"]["attempt_id"] == "attempt-u-ahead-previous-exact-retry"
    retry = _lock(tmp_path, "attempt-u-ahead-retry-exact").acquire()
    try:
        adopted = run_lock_module._validate_history(
            run_lock_module._strict_json(sidecar.read_bytes(), canonical=True)
        )
        assert adopted["observation_count"] == 1
        assert adopted["first"]["attempt_id"] == previous.attempt_id
        assert adopted["latest"]["attempt_id"] == previous.attempt_id
    finally:
        retry.release()


@pytest.mark.parametrize("mismatch", ["source", "epoch", "prior_pointer", "inode"])
def test_u_ahead_mismatched_provenance_cannot_be_adopted(tmp_path, monkeypatch, mismatch):
    previous = _create_u_ahead_acquisition(tmp_path / "locks", monkeypatch, mismatch)
    old_metadata = previous.path.read_bytes()
    sidecar = previous.path.with_suffix(".history")
    history = run_lock_module._strict_json(sidecar.read_bytes(), canonical=True)
    if mismatch == "source":
        history["source"]["metadata_digest"] = "0" * 64
    elif mismatch == "epoch":
        history["writer_epoch"] = "f" * 32
    elif mismatch == "prior_pointer":
        history["source"]["prior_pointer"] = {
            **history["source"]["prior_pointer"], "digest": "0" * 64,
        }
    else:
        history["lock_identity"][1] += 1
    sidecar.write_bytes(run_lock_module._canonical_bytes(run_lock_module._seal(history)))
    contender = _lock(tmp_path, f"attempt-u-ahead-mismatch-{mismatch}")
    with pytest.raises(MinecraftTargetLockError):
        contender.acquire()
    assert contender.acquired is False
    assert contender._stream is None
    assert previous.path.read_bytes() == old_metadata
    assert _predecessor_status(previous).status is not MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN


@pytest.mark.parametrize("failure", ["write", "tear", "fsync"])
def test_acquisition_metadata_failure_after_u_ahead_never_reports_success(tmp_path, monkeypatch, failure):
    previous = _lock(tmp_path, f"attempt-u-ahead-metadata-{failure}").acquire()
    _crash_close_without_release(previous)
    old_metadata = previous.path.read_bytes()
    contender = _lock(tmp_path, f"attempt-u-ahead-metadata-next-{failure}")
    real_write = run_lock_module._write_stream_metadata
    if failure == "fsync":
        lock_stat = os.stat(previous.path, follow_symlinks=False)
        real_fsync = os.fsync

        def fail_lock_fsync(fd):
            descriptor = os.fstat(fd)
            if (descriptor.st_dev, descriptor.st_ino) == (lock_stat.st_dev, lock_stat.st_ino):
                raise OSError("U-ahead acquisition metadata fsync cut")
            return real_fsync(fd)

        monkeypatch.setattr(run_lock_module.os, "fsync", fail_lock_fsync)
    else:
        def fail_lock_write(stream, payload):
            if payload.get("attempt_id") == contender.attempt_id:
                if failure == "tear":
                    raw = run_lock_module._canonical_bytes(payload)
                    stream.seek(0)
                    stream.truncate()
                    run_lock_module._write_complete(stream.fileno(), raw[:max(1, len(raw) // 2)])
                    os.fsync(stream.fileno())
                raise OSError(f"U-ahead metadata {failure} cut")
            return real_write(stream, payload)

        monkeypatch.setattr(run_lock_module, "_write_stream_metadata", fail_lock_write)
    with pytest.raises(MinecraftTargetLockUnavailableError):
        contender.acquire()
    history = run_lock_module._validate_history(
        run_lock_module._strict_json(previous.path.with_suffix(".history").read_bytes(), canonical=True)
    )
    assert history["state"] == "UNRESOLVED"
    assert history["first"]["attempt_id"] == previous.attempt_id
    assert history["source"]["metadata_digest"] == run_lock_module._digest(old_metadata)


@pytest.mark.parametrize("barrier", ["write_error", "zero_write", "temporary_fsync",
                                      "temporary_readback", "replace", "directory_fsync",
                                      "installed_readback"])
def test_acquisition_sidecar_fault_matrix_preserves_old_acquired_record(tmp_path, monkeypatch, barrier):
    previous = _lock(tmp_path, f"attempt-acquire-sidecar-prior-{barrier}").acquire()
    _crash_close_without_release(previous)
    original = previous.path.read_bytes()
    contender = _lock(tmp_path, f"attempt-acquire-sidecar-next-{barrier}")
    history_type = run_lock_module._HistoryIO
    calls = []
    if barrier == "write_error":
        real_write = run_lock_module._write_complete

        def fail_write(fd, raw):
            calls.append(barrier)
            raise OSError("sidecar write error")

        monkeypatch.setattr(run_lock_module, "_write_complete", fail_write)
    elif barrier == "zero_write":
        def zero_write(fd, raw):
            calls.append(barrier)
            return 0

        monkeypatch.setattr(run_lock_module.os, "write", zero_write)
    elif barrier == "temporary_fsync":
        def fail_fsync(fd):
            calls.append(barrier)
            raise OSError("sidecar temp fsync error")

        monkeypatch.setattr(run_lock_module.os, "fsync", fail_fsync)
    elif barrier == "temporary_readback":
        real_read = history_type._read_named

        def fail_temp_read(self, name):
            raw = real_read(self, name)
            if ".tmp-" in name:
                calls.append(barrier)
                return raw + b" "
            return raw

        monkeypatch.setattr(history_type, "_read_named", fail_temp_read)
    elif barrier == "replace":
        real_replace = os.replace

        def fail_replace(source, target, *args, **kwargs):
            if str(target).endswith(".history"):
                calls.append(barrier)
                raise OSError("history replace error")
            return real_replace(source, target, *args, **kwargs)

        monkeypatch.setattr(run_lock_module.os, "replace", fail_replace)
    elif barrier == "directory_fsync":
        real_sync = run_lock_module._fsync_parent_directory

        def fail_dirsync(path):
            if str(path).endswith(".history"):
                calls.append(barrier)
                raise OSError("history directory sync error")
            return real_sync(path)

        monkeypatch.setattr(run_lock_module, "_fsync_parent_directory", fail_dirsync)
    else:
        real_replace = os.replace
        real_read = history_type.read
        installed = []

        def mark_installed(source, target, *args, **kwargs):
            result = real_replace(source, target, *args, **kwargs)
            if str(target).endswith(".history"):
                installed.append(True)
            return result

        def mismatch_installed(self, suffix):
            raw = real_read(self, suffix)
            if suffix == "history" and installed:
                calls.append(barrier)
                return raw + b" "
            return raw

        monkeypatch.setattr(run_lock_module.os, "replace", mark_installed)
        monkeypatch.setattr(history_type, "read", mismatch_installed)
    with pytest.raises(MinecraftTargetLockError):
        contender.acquire()
    assert calls
    assert contender.acquired is False
    assert contender._stream is None
    assert previous.path.read_bytes() == original
    assert _predecessor_status(previous).status is not MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN


def test_predecessor_token_round_trip_and_nested_snapshot_data_are_immutable(tmp_path):
    crashed = _lock(tmp_path, "attempt-token-immutability").acquire()
    _crash_close_without_release(crashed)
    snapshot = _predecessor_status(crashed)
    token = snapshot.token
    assert isinstance(token, MinecraftTargetPredecessorInspectionToken)
    assert MinecraftTargetPredecessorInspectionToken.from_json(token.to_json()) == token
    _assert_deeply_immutable(token.state)
    with pytest.raises(FrozenInstanceError):
        token.state = {}
    first_export = snapshot.to_dict()
    repeated = _predecessor_status(crashed)
    assert repeated.to_dict() == first_export
    assert snapshot.token.to_json() == token.to_json()


def test_retained_predecessor_observation_is_readonly_and_requires_live_lease(tmp_path):
    lock = _lock(tmp_path, "attempt-retained-predecessor-observation").acquire()
    try:
        metadata = lock.path.read_bytes()
        sidecar = lock.path.with_suffix(".history")
        history = sidecar.read_bytes()
        metadata_stat = os.stat(lock.path, follow_symlinks=False)
        history_stat = os.stat(sidecar, follow_symlinks=False)
        position = lock._stream.tell()
        first = lock.retained_predecessor_snapshot()
        second = lock.retained_predecessor_snapshot()
        assert first == second
        assert first.active_owner is True
        assert first.current_owner is not None
        assert lock._stream.tell() == position
        assert lock.path.read_bytes() == metadata
        assert sidecar.read_bytes() == history
        assert os.stat(lock.path, follow_symlinks=False).st_mtime_ns == metadata_stat.st_mtime_ns
        assert os.stat(sidecar, follow_symlinks=False).st_mtime_ns == history_stat.st_mtime_ns
        with lock.lifecycle_guard():
            assert lock.release().status is MinecraftTargetLockReleaseStatus.VERIFIED_RELEASED
            with pytest.raises(MinecraftTargetLockError):
                lock.retained_predecessor_snapshot()
    finally:
        if lock.acquired:
            lock.release()


def test_retained_predecessor_observation_rejects_lock_inode_drift(tmp_path):
    lock = _lock(tmp_path, "attempt-retained-predecessor-inode-drift").acquire()
    replacement = lock.path.with_name("predecessor-lock-replacement")
    replacement.write_bytes(lock.path.read_bytes())
    os.replace(replacement, lock.path)
    replaced_bytes = lock.path.read_bytes()
    try:
        with pytest.raises(MinecraftTargetLockError):
            lock.retained_predecessor_snapshot()
        assert lock.path.read_bytes() == replaced_bytes
    finally:
        _crash_close_without_release(lock)


def test_retained_predecessor_observation_rejects_noncurrent_metadata(tmp_path):
    lock = _lock(tmp_path, "attempt-retained-predecessor-noncurrent").acquire()
    original = lock.path.read_bytes()
    try:
        metadata = run_lock_module._strict_json(original, canonical=True)
        metadata["attempt_id"] = "not-the-retained-owner"
        lock.path.write_bytes(run_lock_module._canonical_bytes(run_lock_module._seal(metadata)))
        with pytest.raises(MinecraftTargetLockError):
            lock.retained_predecessor_snapshot()
    finally:
        lock.path.write_bytes(original)
        if lock.acquired:
            lock.release()


def test_offline_predecessor_observation_is_immutable_and_busy_is_ambiguous(tmp_path):
    crashed = _lock(tmp_path, "attempt-offline-immutable-original").acquire()
    _crash_close_without_release(crashed)
    history_path = crashed.path.with_suffix(".history")
    metadata_bytes, history_bytes = crashed.path.read_bytes(), history_path.read_bytes()
    metadata_mtime = os.stat(crashed.path, follow_symlinks=False).st_mtime_ns
    history_mtime = os.stat(history_path, follow_symlinks=False).st_mtime_ns
    first = _predecessor_status(crashed)
    second = _predecessor_status(crashed)
    assert first == second
    assert first.token.to_json() == second.token.to_json()
    assert crashed.path.read_bytes() == metadata_bytes
    assert history_path.read_bytes() == history_bytes
    assert os.stat(crashed.path, follow_symlinks=False).st_mtime_ns == metadata_mtime
    assert os.stat(history_path, follow_symlinks=False).st_mtime_ns == history_mtime

    owner = _lock(tmp_path, "attempt-offline-lock-busy").acquire()
    try:
        assert _predecessor_status(owner).status is MinecraftTargetPredecessorHistoryStatus.AMBIGUOUS
    finally:
        owner.release()


def test_retained_predecessor_observation_honors_other_thread_lifecycle_guard(tmp_path):
    owner = _lock(tmp_path, "attempt-retained-predecessor-thread-guard").acquire()
    started, finished = threading.Event(), threading.Event()
    result, errors = [], []

    def inspect():
        started.set()
        try:
            result.append(owner.retained_predecessor_snapshot())
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=inspect)
    try:
        with owner.lifecycle_guard():
            thread.start()
            assert started.wait(1)
            assert not finished.wait(0.05)
        thread.join(timeout=1)
        assert finished.is_set()
        assert not errors
        assert result[0].status is MinecraftTargetPredecessorHistoryStatus.LEGACY_UNKNOWN
    finally:
        if thread.is_alive():
            thread.join(timeout=1)
        if owner.acquired:
            owner.release()


@pytest.mark.parametrize("raise_body", [False, True])
def test_context_exit_preserves_inherited_legacy_unknown_history(tmp_path, raise_body):
    lock = _lock(tmp_path, f"attempt-context-legacy-unknown-{raise_body}")
    _write_schema_v1_metadata(lock, status="released", attempt_id="old-released-owner")
    if raise_body:
        with pytest.raises(RuntimeError, match="context body exception"):
            with lock:
                assert lock.retained_predecessor_snapshot().status is MinecraftTargetPredecessorHistoryStatus.LEGACY_UNKNOWN
                raise RuntimeError("context body exception")
    else:
        with lock:
            assert lock.retained_predecessor_snapshot().status is MinecraftTargetPredecessorHistoryStatus.LEGACY_UNKNOWN
    assert lock.last_release_outcome.status is MinecraftTargetLockReleaseStatus.VERIFIED_RELEASED
    assert _predecessor_status(lock).status is MinecraftTargetPredecessorHistoryStatus.LEGACY_UNKNOWN
    _assert_v3_metadata_history_pointer_coherent(lock)


def test_quarantine_release_and_generic_clear_preserve_history_pointer_inventory(tmp_path):
    owner = _lock(tmp_path, "attempt-quarantine-pointer-inventory").acquire()
    initial = owner.retained_predecessor_snapshot()
    initial_metadata = _assert_v3_metadata_history_pointer_coherent(owner)[0]
    owner.quarantine(run_name="inventory-run", reasons=["pointer-test"], diagnostics={})
    quarantined = owner.retained_predecessor_snapshot()
    quarantine_metadata, quarantine_history = _assert_v3_metadata_history_pointer_coherent(owner)
    assert (quarantined.generation, quarantined.ordinal, quarantined.digest) == (
        initial.generation, initial.ordinal, initial.digest
    )
    assert quarantine_metadata["revision"] == initial_metadata["revision"] + 1
    assert quarantine_metadata["transition_nonce"] != initial_metadata["transition_nonce"]
    assert owner.release().status is MinecraftTargetLockReleaseStatus.VERIFIED_RELEASED
    cleared = clear_minecraft_target_quarantine(
        lock_root=owner.lock_root, host=owner.host, port=owner.port,
        reason="Pointer inventory independently reviewed", acknowledge_target_safe=True,
    )
    current = _predecessor_status(owner)
    cleared_metadata, cleared_history = _assert_v3_metadata_history_pointer_coherent(owner)
    assert cleared["status"] == "cleared"
    assert current.status is MinecraftTargetPredecessorHistoryStatus.UNRESOLVED
    assert current.first["attempt_id"] == owner.attempt_id
    assert cleared_history["generation"] == quarantine_history["generation"]
    assert cleared_history["ordinal"] > quarantine_history["ordinal"]
    assert cleared_metadata["transition_nonce"] != quarantine_metadata["transition_nonce"]


def test_failed_quarantine_preserves_history_and_release_keeps_u(tmp_path, monkeypatch):
    owner = _lock(tmp_path, "attempt-quarantine-pointer-failure").acquire()
    before = owner.retained_predecessor_snapshot()

    def fail_quarantine(payload):
        if payload.get("status") == "quarantined":
            raise OSError("injected quarantine metadata write failure")
        raise AssertionError("unexpected metadata transition")

    monkeypatch.setattr(owner, "_write_metadata", fail_quarantine)
    with pytest.raises(OSError, match="injected quarantine metadata write failure"):
        owner.quarantine(run_name="failed-quarantine", reasons=["write-failure"], diagnostics={})
    after = owner.retained_predecessor_snapshot()
    assert (after.generation, after.ordinal, after.digest) == (before.generation, before.ordinal, before.digest)
    assert after.uncertain is True
    outcome = owner.release()
    assert outcome.status is MinecraftTargetLockReleaseStatus.UNCERTAIN
    observed = _predecessor_status(owner)
    assert observed.status is not MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN
    assert observed.uncertain is True


@pytest.mark.parametrize("invalid", [
    {"acknowledge_target_safe": False}, {"acknowledge_whole_prefix": False},
    {"reason": ""}, {"operator": ""},
])
def test_acknowledgement_requires_explicit_whole_prefix_intent(tmp_path, invalid):
    crashed = _lock(tmp_path, "attempt-ack-intent-validation").acquire()
    _crash_close_without_release(crashed)
    inspected = _predecessor_status(crashed)
    with pytest.raises(ValueError):
        _acknowledge_predecessor(crashed, inspected, **invalid)
    assert _predecessor_status(crashed).status is MinecraftTargetPredecessorHistoryStatus.UNRESOLVED


@pytest.mark.parametrize("failure", ["fsync", "readback", "replace", "dirfsync"])
def test_history_publication_failure_cannot_acknowledge_clean_prefix(tmp_path, monkeypatch, failure):
    crashed = _lock(tmp_path, f"attempt-ack-history-publish-{failure}").acquire()
    _crash_close_without_release(crashed)
    inspected = _predecessor_status(crashed)
    qualification = _test_storage_qualification(crashed)
    io_type = run_lock_module._HistoryIO
    real_publish, real_read = io_type.publish, io_type.read
    real_replace, real_sync = os.replace, run_lock_module._fsync_parent_directory
    installed, fired = [], []

    def fail_published_history(self, suffix, payload):
        if suffix != "history":
            return real_publish(self, suffix, payload)
        if failure == "fsync":
            with monkeypatch.context() as patch:
                patch.setattr(run_lock_module.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("history fsync cut")))
                return real_publish(self, suffix, payload)
        if failure == "replace":
            def fail_replace(source, target, *args, **kwargs):
                if Path(target).name.endswith(".history"):
                    raise OSError("history replace cut")
                return real_replace(source, target, *args, **kwargs)
            with monkeypatch.context() as patch:
                patch.setattr(run_lock_module.os, "replace", fail_replace)
                return real_publish(self, suffix, payload)
        if failure == "dirfsync":
            def fail_sync(path):
                if path.name.endswith(".history"):
                    raise OSError("history directory cut")
                return real_sync(path)
            with monkeypatch.context() as patch:
                patch.setattr(run_lock_module, "_fsync_parent_directory", fail_sync)
                return real_publish(self, suffix, payload)
        def mark_replace(source, target, *args, **kwargs):
            result = real_replace(source, target, *args, **kwargs)
            if Path(target).name.endswith(".history"):
                installed.append(True)
            return result
        def mismatch_read(instance, read_suffix):
            raw = real_read(instance, read_suffix)
            if installed and read_suffix == "history" and not fired:
                fired.append(True)
                raise OSError("history installed readback cut")
            return raw
        with monkeypatch.context() as patch:
            patch.setattr(run_lock_module.os, "replace", mark_replace)
            patch.setattr(io_type, "read", mismatch_read)
            return real_publish(self, suffix, payload)

    monkeypatch.setattr(io_type, "publish", fail_published_history)
    outcome = _acknowledge_predecessor(
        crashed, inspected, storage_qualification=qualification, reconcile_unknown_history=True
    )
    assert outcome.status is MinecraftTargetPredecessorAcknowledgementStatus.UNCERTAIN
    assert outcome.acknowledged is False
    assert _predecessor_status(crashed, qualification).status is not MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN


def test_stale_predecessor_cas_is_rejected_after_generic_cycle(tmp_path):
    crashed = _lock(tmp_path, "attempt-stale-cas-first").acquire()
    _crash_close_without_release(crashed)
    stale = _predecessor_status(crashed)
    successor = _lock(tmp_path, "attempt-stale-cas-successor").acquire()
    assert successor.release().status is MinecraftTargetLockReleaseStatus.VERIFIED_RELEASED
    outcome = _acknowledge_predecessor(crashed, stale)
    assert outcome.acknowledged is False
    assert _predecessor_status(crashed).status is MinecraftTargetPredecessorHistoryStatus.UNRESOLVED


def test_acknowledgement_cas_binds_exact_target_and_lock_inode(tmp_path):
    crashed = _lock(tmp_path, "attempt-target-inode-cas").acquire()
    _crash_close_without_release(crashed)
    inspected = _predecessor_status(crashed)
    qualification = _test_storage_qualification(crashed)
    wrong_target = acknowledge_minecraft_target_predecessor(
        lock_root=crashed.lock_root, host=crashed.host, port=crashed.port + 1,
        expected=inspected.token, acknowledge_target_safe=True, acknowledge_whole_prefix=True,
        reason="wrong target rejected", operator="pytest-cas",
        storage_qualification=qualification,
    )
    assert wrong_target.acknowledged is False
    replacement = crashed.path.with_name("cas-replacement-lock")
    replacement.write_bytes(crashed.path.read_bytes())
    os.replace(replacement, crashed.path)
    changed = _acknowledge_predecessor(crashed, inspected, storage_qualification=qualification)
    assert changed.acknowledged is False
    assert _predecessor_status(crashed, qualification).status is not MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN


@pytest.mark.parametrize("blocker", ["quarantine", "uncertainty"])
def test_predecessor_ack_does_not_clear_independent_generic_blockers(tmp_path, blocker):
    owner = _lock(tmp_path, f"attempt-independent-blocker-{blocker}").acquire()
    if blocker == "quarantine":
        owner.quarantine(run_name="independent", reasons=["manual-review"], diagnostics={})
        owner.release()
    else:
        owner.release()
        owner.path.with_suffix(".uncertain").write_text(
            json.dumps({"schema_version": 1, "status": "uncertain", "attempt_id": "test"}),
            encoding="utf-8",
        )
    qualification = _test_storage_qualification(owner)
    inspected = _predecessor_status(owner, qualification)
    _acknowledge_predecessor(
        owner, inspected, storage_qualification=qualification, reconcile_unknown_history=True
    )
    after = _predecessor_status(owner, qualification)
    generic = read_minecraft_target_lock_status(
        lock_root=owner.lock_root, host=owner.host, port=owner.port
    )
    assert generic["blocking"] is True
    if blocker == "quarantine":
        assert after.quarantined is True
    else:
        assert after.uncertain is True
        assert os.path.lexists(owner.path.with_suffix(".uncertain"))


@pytest.mark.parametrize("pid_case", ["same", "live", "reused", "dead"])
def test_displacement_records_every_pid_incarnation_before_overwrite(tmp_path, monkeypatch, pid_case):
    old = _lock(tmp_path, f"attempt-pid-incarnation-{pid_case}").acquire()
    _crash_close_without_release(old)
    metadata = run_lock_module._strict_json(old.path.read_bytes(), canonical=True)
    metadata["pid"] = os.getpid() if pid_case == "same" else 99999999
    old.path.write_bytes(run_lock_module._canonical_bytes(run_lock_module._seal(metadata)))
    source_digest = run_lock_module._digest(old.path.read_bytes())
    monkeypatch.setattr(run_lock_module, "_pid_exists", lambda _pid: pid_case != "dead")
    with _lock(tmp_path, "attempt-pid-incarnation-successor") as successor:
        observed = successor.retained_predecessor_snapshot()
        assert observed.status is MinecraftTargetPredecessorHistoryStatus.UNRESOLVED
        assert observed.first["pid"] == metadata["pid"]
        assert observed.first["attempt_id"] == f"attempt-pid-incarnation-{pid_case}"
    assert observed.first["source_digest"] == source_digest
    assert _predecessor_status(old).status is MinecraftTargetPredecessorHistoryStatus.UNRESOLVED


def test_acknowledged_snapshot_exports_deeply_frozen_diagnostics(tmp_path):
    crashed = _lock(tmp_path, "attempt-acknowledged-diagnostics-freeze").acquire()
    _crash_close_without_release(crashed)
    inspected = _predecessor_status(crashed)
    qualification = _test_storage_qualification(crashed)
    acknowledged = _acknowledge_predecessor(
        crashed, inspected, storage_qualification=qualification
    )
    snapshot = acknowledged.snapshot
    assert snapshot.acknowledged_diagnostics["first"]["attempt_id"] == crashed.attempt_id
    _assert_deeply_immutable(snapshot.acknowledged_diagnostics)
    exported = snapshot.to_dict()
    assert exported["acknowledged_diagnostics"]["first"]["attempt_id"] == crashed.attempt_id
    exported["acknowledged_diagnostics"]["first"]["pid"] = -1
    assert snapshot.acknowledged_diagnostics["first"]["pid"] > 0
    with pytest.raises(TypeError):
        snapshot.acknowledged_diagnostics["first"]["pid"] = -1


def _acknowledge_while_pending_held(lock_root, token_json, receipt_path, ready, proceed, results):
    real_publish = run_lock_module._HistoryIO.publish
    qualification = load_minecraft_target_storage_qualification(receipt_path)

    def hold_pending(self, suffix, payload):
        if suffix == "history-clear-pending":
            ready.set()
            if not proceed.wait(5):
                raise TimeoutError("pending-clear serialization test timed out")
        return real_publish(self, suffix, payload)

    run_lock_module._HistoryIO.publish = hold_pending
    outcome = acknowledge_minecraft_target_predecessor(
        lock_root=lock_root, host="127.0.0.1", port=25565,
        expected=MinecraftTargetPredecessorInspectionToken.from_json(token_json),
        acknowledge_target_safe=True, acknowledge_whole_prefix=True,
        reason="cross-process serialization assertion", operator="pytest-child",
        storage_qualification=qualification, reconcile_unknown_history=True,
    )
    results.put(outcome.acknowledged)


def test_acknowledgement_serializes_with_offline_status_and_generic_acquire(tmp_path):
    crashed = _lock(tmp_path, "attempt-cross-process-ack-serialization").acquire()
    _crash_close_without_release(crashed)
    inspected = _predecessor_status(crashed)
    qualification = _test_storage_qualification(crashed)
    context = multiprocessing.get_context("fork")
    ready, proceed, results = context.Event(), context.Event(), context.Queue()
    child = context.Process(
        target=_acknowledge_while_pending_held,
        args=(str(crashed.lock_root), inspected.token.to_json(), str(qualification.receipt_path),
              ready, proceed, results),
    )
    child.start()
    try:
        assert ready.wait(3)
        observed = _predecessor_status(crashed, qualification)
        assert observed.status is MinecraftTargetPredecessorHistoryStatus.AMBIGUOUS
        assert observed.active_owner is True
        assert observed.token is None
        with pytest.raises(MinecraftTargetLockBusyError):
            _lock(tmp_path, "attempt-cross-process-ack-contender").acquire()
    finally:
        proceed.set()
        child.join(7)
    assert child.exitcode == 0
    assert results.get(timeout=1) is True
    assert _predecessor_status(crashed, qualification).status is MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN


def test_quarantined_release_requires_history_integrity_without_metadata_rewrite(tmp_path):
    lock = _lock(tmp_path, "attempt-quarantined-history-integrity").acquire()
    lock.quarantine(run_name="integrity-check", reasons=["target-not-safe"], diagnostics={})
    before = lock.path.read_bytes()
    lock.path.with_suffix(".history").unlink()
    outcome = lock.release()
    assert outcome.status is MinecraftTargetLockReleaseStatus.UNCERTAIN
    assert outcome.uncertainty_persisted is True
    assert lock.path.read_bytes() == before
    assert lock.path.with_suffix(".uncertain").exists()
    assert _predecessor_status(lock).status is not MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN


def test_release_uncertainty_fallback_never_writes_into_substituted_root(tmp_path):
    owner = _lock(tmp_path, "attempt-pinned-root-uncertainty").acquire()
    root = owner.lock_root
    moved_root = tmp_path / "pinned-original-root"
    root.rename(moved_root)
    root.mkdir()

    outcome = owner.release()

    assert outcome.status is MinecraftTargetLockReleaseStatus.FAILED
    assert outcome.uncertainty_persisted is False
    assert owner.acquired is True
    assert owner._stream is not None
    assert not os.path.lexists(root / f"{owner.key}.uncertain")
    assert not os.path.lexists(moved_root / f"{owner.key}.uncertain")
    assert run_lock_module._UNVERIFIED_RELEASE_LOCKS[id(owner)] is owner
    _crash_close_without_release(owner)
    run_lock_module._UNVERIFIED_RELEASE_LOCKS.pop(id(owner), None)


def test_durable_root_rejects_symlink_ancestor_before_creating_descendants(tmp_path):
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    root_alias = tmp_path / "root-alias"
    root_alias.symlink_to(real_parent, target_is_directory=True)
    unsafe_root = root_alias / "new-lock-root"
    lock = MinecraftTargetLock(
        lock_root=unsafe_root, host="127.0.0.1", port=25565,
        world_id="world-a", attempt_id="attempt-symlink-root",
    )
    with pytest.raises(MinecraftTargetLockMetadataError, match="no-follow directory"):
        lock.acquire()
    assert not (real_parent / "new-lock-root").exists()
    assert lock._stream is None


@pytest.mark.parametrize(
    "content",
    [
        b'{"schema_version":1,"schema_version":1,"status":"uncertain"}',
        b"x" * (run_lock_module._MAX_RECORD_BYTES + 1),
    ],
)
def test_uncertainty_marker_duplicate_or_oversize_is_present_nonclean(tmp_path, content):
    lock = _lock(tmp_path, "attempt-marker-strict-bounds")
    lock.lock_root.mkdir(parents=True)
    marker = lock.path.with_suffix(".uncertain")
    marker.write_bytes(content)
    present, details = run_lock_module._uncertainty_marker_state(marker)
    assert present is True
    assert details["valid"] is False
    assert read_minecraft_target_lock_status(
        lock_root=lock.lock_root, host=lock.host, port=lock.port
    )["blocking"] is True


def test_contention_owner_snapshot_is_bounded_for_oversized_record(tmp_path):
    owner = _lock(tmp_path, "attempt-large-contention-owner").acquire()
    oversized = b'{"status":"acquired","attempt_id":"' + b"x" * run_lock_module._MAX_RECORD_BYTES
    owner.path.write_bytes(oversized)
    contender = _lock(tmp_path, "attempt-large-contention-contender")
    try:
        with pytest.raises(MinecraftTargetLockBusyError) as raised:
            contender.acquire()
        assert raised.value.owner == {}
        assert owner.path.read_bytes() == oversized
    finally:
        _crash_close_without_release(owner)


@pytest.mark.parametrize("corruption", ["missing_history", "corrupt_history", "metadata_tear", "v2_downgrade"])
def test_previously_clean_pair_never_masks_missing_corrupt_or_downgraded_state(tmp_path, corruption):
    lock = _lock(tmp_path, f"attempt-clean-pair-corruption-{corruption}").acquire()
    assert lock.release().verified_released
    qualification = _test_storage_qualification(lock)
    outcome = _acknowledge_predecessor(
        lock, _predecessor_status(lock), storage_qualification=qualification,
        reconcile_unknown_history=True,
    )
    assert outcome.acknowledged
    if corruption == "missing_history":
        lock.path.with_suffix(".history").unlink()
    elif corruption == "corrupt_history":
        lock.path.with_suffix(".history").write_bytes(b"{")
    elif corruption == "metadata_tear":
        lock.path.write_bytes(b"{")
    else:
        lock.path.write_text(json.dumps(_schema_v2_metadata(lock, status="cleared")), encoding="utf-8")
        old_epoch = outcome.snapshot.writer_epoch
        with _lock(tmp_path, f"attempt-clean-pair-new-epoch-{corruption}") as upgraded:
            assert upgraded.retained_predecessor_snapshot().writer_epoch != old_epoch
    assert _predecessor_status(lock, qualification).status is not MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN


def _crash_ack_transaction_at_stage(lock_root, host, port, token_json, receipt_path, stage):
    """Child-only abrupt crash cuts; receipt is a test fixture, not proof."""
    token = MinecraftTargetPredecessorInspectionToken.from_json(token_json)
    receipt = load_minecraft_target_storage_qualification(receipt_path)
    real_publish = run_lock_module._HistoryIO.publish
    real_metadata_write = run_lock_module._write_stream_metadata
    real_unlink = os.unlink

    if stage in {"pending", "clean_history"}:
        def crash_after_publish(self, suffix, payload):
            result = real_publish(self, suffix, payload)
            if stage == "pending" and suffix == "history-clear-pending":
                os._exit(77)
            if stage == "clean_history" and suffix == "history":
                os._exit(77)
            return result

        run_lock_module._HistoryIO.publish = crash_after_publish
    elif stage == "metadata":
        def crash_after_metadata(stream, payload):
            result = real_metadata_write(stream, payload)
            if payload.get("status") == "reconciled":
                os._exit(77)
            return result

        run_lock_module._write_stream_metadata = crash_after_metadata
    else:
        def crash_at_unlink(path, *args, **kwargs):
            if Path(path).name.endswith(".history-clear-pending"):
                if stage == "before_pending_unlink":
                    os._exit(77)
                result = real_unlink(path, *args, **kwargs)
                if stage == "after_pending_unlink":
                    os._exit(77)
                return result
            return real_unlink(path, *args, **kwargs)

        run_lock_module.os.unlink = crash_at_unlink

    acknowledge_minecraft_target_predecessor(
        lock_root=lock_root, host=host, port=port, expected=token,
        acknowledge_target_safe=True, acknowledge_whole_prefix=True,
        reason="subprocess abrupt crash matrix", operator="pytest-child",
        storage_qualification=receipt, reconcile_unknown_history=True,
    )
    os._exit(78)


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        ("pending", MinecraftTargetPredecessorHistoryStatus.DURABILITY_UNCERTAIN),
        ("clean_history", MinecraftTargetPredecessorHistoryStatus.DURABILITY_UNCERTAIN),
        ("metadata", MinecraftTargetPredecessorHistoryStatus.DURABILITY_UNCERTAIN),
        ("before_pending_unlink", MinecraftTargetPredecessorHistoryStatus.DURABILITY_UNCERTAIN),
        ("after_pending_unlink", MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN),
    ],
)
def test_acknowledgement_abrupt_process_crashes_reobserve_only_complete_pair(tmp_path, stage, expected):
    previous = _lock(tmp_path, f"attempt-abrupt-ack-{stage}").acquire()
    _crash_close_without_release(previous)
    inspected = _predecessor_status(previous)
    receipt = _test_storage_qualification(previous)
    context = multiprocessing.get_context("fork")
    child = context.Process(
        target=_crash_ack_transaction_at_stage,
        args=(str(previous.lock_root), previous.host, previous.port, inspected.token.to_json(),
              str(receipt.receipt_path), stage),
    )
    child.start()
    child.join(3)
    assert child.exitcode == 77
    observed = _predecessor_status(previous, receipt)
    assert observed.status is expected
    if stage != "after_pending_unlink":
        assert previous.path.with_suffix(".history-clear-pending").exists()


def _crash_acquire_at_stage(lock_root, attempt_id, stage):
    real_publish = run_lock_module._HistoryIO.publish
    real_metadata_write = run_lock_module._write_stream_metadata
    if stage == "before_witness":
        def crash_before_history(self, suffix, payload):
            if suffix == "history":
                os._exit(61)
            return real_publish(self, suffix, payload)
        run_lock_module._HistoryIO.publish = crash_before_history
    elif stage == "after_durable_witness":
        def crash_after_history(self, suffix, payload):
            result = real_publish(self, suffix, payload)
            if suffix == "history":
                os._exit(61)
            return result
        run_lock_module._HistoryIO.publish = crash_after_history
    elif stage == "metadata_tear":
        def crash_during_metadata(stream, payload):
            if payload.get("attempt_id") == attempt_id:
                raw = run_lock_module._canonical_bytes(payload)
                stream.seek(0)
                stream.truncate()
                run_lock_module._write_complete(stream.fileno(), raw[:max(1, len(raw) // 2)])
                os.fsync(stream.fileno())
                os._exit(61)
            return real_metadata_write(stream, payload)
        run_lock_module._write_stream_metadata = crash_during_metadata
    lock = MinecraftTargetLock(
        lock_root=lock_root, host="127.0.0.1", port=25565, world_id="world-a",
        attempt_id=attempt_id,
    ).acquire()
    if stage == "after_acquired":
        os._exit(61)
    lock.release()
    os._exit(62)


@pytest.mark.parametrize("stage", ["before_witness", "after_durable_witness", "metadata_tear", "after_acquired"])
def test_acquisition_abrupt_process_cuts_preserve_old_owner_or_u_witness(tmp_path, stage):
    first = _lock(tmp_path, f"attempt-abrupt-acquire-old-{stage}").acquire()
    _crash_close_without_release(first)
    old_metadata = first.path.read_bytes()
    old_history = first.path.with_suffix(".history").read_bytes()
    context = multiprocessing.get_context("fork")
    child = context.Process(
        target=_crash_acquire_at_stage,
        args=(str(first.lock_root), f"attempt-abrupt-acquire-new-{stage}", stage),
    )
    child.start()
    child.join(5)
    assert child.exitcode == 61

    current_metadata = first.path.read_bytes()
    sidecar = run_lock_module._validate_history(
        run_lock_module._strict_json(first.path.with_suffix(".history").read_bytes(), canonical=True)
    )
    observed = _predecessor_status(first)
    assert observed.status is not MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN
    assert observed.first["attempt_id"] == first.attempt_id
    if stage == "before_witness":
        assert current_metadata == old_metadata
        assert first.path.with_suffix(".history").read_bytes() == old_history
        assert sidecar["state"] == "LEGACY_UNKNOWN"
        assert sidecar["observation_count"] == 0
    elif stage == "after_durable_witness":
        assert current_metadata == old_metadata
        assert sidecar["state"] == "UNRESOLVED"
        assert sidecar["observation_count"] == 1
    elif stage == "metadata_tear":
        assert current_metadata != old_metadata
        assert observed.status is MinecraftTargetPredecessorHistoryStatus.HISTORY_CORRUPT
        assert sidecar["state"] == "UNRESOLVED"
        assert sidecar["observation_count"] == 1
    else:
        installed = run_lock_module._parse_lock_metadata(
            run_lock_module._strict_json(current_metadata, canonical=True),
            expected_key=first.key, expected_host=first.host, expected_port=first.port,
        )
        assert installed["attempt_id"] == f"attempt-abrupt-acquire-new-{stage}"
        assert sidecar["state"] == "UNRESOLVED"
        assert sidecar["observation_count"] == 1


def _crash_release_at_stage(lock_root, stage):
    real_persist = run_lock_module._persist_uncertainty_marker
    real_write = run_lock_module._write_stream_metadata
    real_remove = run_lock_module._remove_uncertainty_marker
    owner = MinecraftTargetLock(
        lock_root=lock_root, host="127.0.0.1", port=25565, world_id="world-a",
        attempt_id=f"attempt-release-cut-owner-{stage}",
    ).acquire()
    if stage in {"before_release_marker", "after_release_marker_fsync"}:
        def cut_release_marker(path, **kwargs):
            if kwargs.get("error_type") == "ReleaseInProgress":
                if stage == "before_release_marker":
                    os._exit(71)
                persisted = real_persist(path, **kwargs)
                if persisted:
                    os._exit(71)
                return persisted
            return real_persist(path, **kwargs)
        run_lock_module._persist_uncertainty_marker = cut_release_marker
    elif stage == "after_released_metadata":
        def cut_released_metadata(stream, payload):
            result = real_write(stream, payload)
            if payload.get("status") == "released":
                os._exit(71)
            return result
        run_lock_module._write_stream_metadata = cut_released_metadata
    else:
        def cut_after_marker_remove(path):
            result = real_remove(path)
            os._exit(71)
            return result
        run_lock_module._remove_uncertainty_marker = cut_after_marker_remove
    owner.release()
    os._exit(72)


@pytest.mark.parametrize("stage", [
    "before_release_marker", "after_release_marker_fsync",
    "after_released_metadata", "after_marker_dirsync_before_unlock",
])
def test_release_abrupt_process_cuts_preserve_inherited_u_and_marker_order(tmp_path, stage):
    first = _lock(tmp_path, f"attempt-release-cut-first-{stage}").acquire()
    _crash_close_without_release(first)
    context = multiprocessing.get_context("fork")
    child = context.Process(target=_crash_release_at_stage, args=(str(first.lock_root), stage))
    child.start()
    child.join(5)
    assert child.exitcode == 71
    observed = _predecessor_status(first)
    assert observed.status is not MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN
    assert observed.first["attempt_id"] == first.attempt_id
    metadata = read_minecraft_target_lock_metadata(
        lock_root=first.lock_root, host=first.host, port=first.port
    )
    marker = first.path.with_suffix(".uncertain")
    if stage == "before_release_marker":
        assert metadata["status"] == "acquired"
        assert not marker.exists()
    elif stage in {"after_release_marker_fsync", "after_released_metadata"}:
        assert marker.exists()
        if stage == "after_released_metadata":
            assert metadata["status"] == "released"
    else:
        assert metadata["status"] == "released"
        assert not marker.exists()


def _v3_clean_high_water(tmp_path):
    """TEST ONLY qualification; deliberately establish a nontrivial v3 fence."""
    original = _lock(tmp_path, "v3-original-acknowledged-owner").acquire()
    _crash_close_without_release(original)
    receipt = _test_storage_qualification(original)
    acknowledged = _acknowledge_predecessor(
        original, _predecessor_status(original), storage_qualification=receipt,
    )
    assert acknowledged.acknowledged
    for index in range(3):
        with _lock(tmp_path, f"v3-high-water-cycle-{index}"):
            pass
    metadata, history = _assert_v3_metadata_history_pointer_coherent(original)
    assert metadata["writer_epoch"] == acknowledged.snapshot.writer_epoch
    assert metadata["revision"] > 1
    assert _predecessor_status(original, receipt).status is MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN
    return original, receipt, metadata, history


def _v3_abandoned_owner(tmp_path, name):
    owner = _lock(tmp_path, name).acquire()
    _crash_close_without_release(owner)
    return owner


def _v3_fd_path(fd):
    # Linux test runner: attribute the fault to the *opened* descriptor, not a
    # guessed os.write/fsync call count or an unrelated installed sidecar.
    return Path(os.readlink(f"/proc/self/fd/{fd}"))


def _v3_join_abrupt_child(child, exitcode):
    try:
        child.join(10)
        assert not child.is_alive(), "fault child did not reach its deterministic exit"
        assert child.exitcode == exitcode
    finally:
        if child.is_alive():
            child.terminate()
            child.join(5)
        assert not child.is_alive(), "fault child leaked"
        child.close()


def test_v3_5a_11_v2_released_generic_migration_has_coherent_provisional_pair(tmp_path):
    # 5a-11: observational UNKNOWN or explicit ack is not generic migration.
    owner = _lock(tmp_path, "v3-v2-released-migration")
    owner.lock_root.mkdir(parents=True)
    old_raw = json.dumps(_schema_v2_metadata(owner, status="released")).encode()
    owner.path.write_bytes(old_raw)
    assert not owner.path.with_suffix(".history").exists()
    with owner:
        metadata, history = _assert_v3_metadata_history_pointer_coherent(owner)
        assert metadata["status"] == "acquired"
        assert metadata["revision"] == 1
        assert history["state"] == "LEGACY_UNKNOWN"
        assert history["source"]["metadata_digest"] == run_lock_module._digest(old_raw)
        assert history["gaps"]["count"] > 0
        assert history["first"] is None and history["observation_count"] == 0
        assert owner.retained_predecessor_snapshot().status is MinecraftTargetPredecessorHistoryStatus.LEGACY_UNKNOWN
    metadata, history = _assert_v3_metadata_history_pointer_coherent(owner)
    assert metadata["status"] == "released" and metadata["revision"] == 2
    assert history["state"] == "LEGACY_UNKNOWN" and history["acknowledgement"] is None
    assert _predecessor_status(owner).status is MinecraftTargetPredecessorHistoryStatus.LEGACY_UNKNOWN
    assert read_minecraft_target_lock_status(lock_root=owner.lock_root, host=owner.host, port=owner.port)["blocking"] is False


def test_v3_5a_16_downgrade_then_force_corrupt_clear_never_revives_clean_epoch(tmp_path):
    # 5a-16: combine the downgrade and force-corrupt clear, not separate tests.
    owner, receipt, old_metadata, _ = _v3_clean_high_water(tmp_path)
    old_history = owner.path.with_suffix(".history").read_bytes()
    downgraded = json.dumps(_schema_v2_metadata(owner, status="cleared")).encode()
    owner.path.write_bytes(downgraded)
    assert _predecessor_status(owner, receipt).status is MinecraftTargetPredecessorHistoryStatus.LEGACY_UNKNOWN
    cleared = clear_minecraft_target_quarantine(
        lock_root=owner.lock_root, host=owner.host, port=owner.port,
        reason="test-only downgraded writer diagnosis", acknowledge_target_safe=True,
        force_corrupt=True,
    )
    metadata, history = _assert_v3_metadata_history_pointer_coherent(owner)
    assert metadata == cleared and metadata["status"] == "cleared"
    assert metadata["writer_epoch"] != old_metadata["writer_epoch"]
    assert metadata["revision"] == 1
    assert history["state"] == "LEGACY_UNKNOWN"
    assert history["source"]["metadata_digest"] == run_lock_module._digest(downgraded)
    assert history["gaps"]["latest_digest"] == run_lock_module._digest(old_history)
    assert _predecessor_status(owner, receipt).status is MinecraftTargetPredecessorHistoryStatus.LEGACY_UNKNOWN
    with _lock(tmp_path, "v3-after-downgrade-clear") as successor:
        assert successor.retained_predecessor_snapshot(storage_qualification=receipt).status is MinecraftTargetPredecessorHistoryStatus.LEGACY_UNKNOWN
    assert _predecessor_status(owner, receipt).status is not MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN


def test_v3_5a_22_canonical_unknown_history_schema_never_clean(tmp_path):
    # 5a-22: a valid checksum/canonical JSON with an unsupported HISTORY schema.
    owner, receipt, _, _ = _v3_clean_high_water(tmp_path)
    metadata_before = owner.path.read_bytes()
    path = owner.path.with_suffix(".history")
    history = run_lock_module._strict_json(path.read_bytes(), canonical=True)
    history["schema"] = "minecraft-target-predecessor-history/999"
    changed = run_lock_module._canonical_bytes(run_lock_module._seal(history))
    path.write_bytes(changed)
    observed = _predecessor_status(owner, receipt)
    assert observed.status is MinecraftTargetPredecessorHistoryStatus.HISTORY_CORRUPT
    with pytest.raises(MinecraftTargetLockMetadataError):
        _lock(tmp_path, "v3-unknown-history-schema-contender").acquire()
    assert owner.path.read_bytes() == metadata_before
    assert path.read_bytes() == changed


@pytest.mark.parametrize("transition", ["acquire", "quarantine", "release", "clear", "acknowledge"])
@pytest.mark.parametrize("tear", ["blank", "half", "last_byte"])
def test_v3_5b_47_each_torn_acknowledged_high_water_is_not_guessed(tmp_path, monkeypatch, transition, tear):
    # 5b-47: every metadata-writing transition, at distinct lost/partial fences.
    owner, receipt, high_water, _ = _v3_clean_high_water(tmp_path)
    active = None
    if transition in {"quarantine", "release"}:
        active = _lock(tmp_path, f"v3-high-water-{transition}").acquire()
        high_water, _ = _assert_v3_metadata_history_pointer_coherent(active)
    inspected = _predecessor_status(owner, receipt) if active is None else None
    real_writer = run_lock_module._write_stream_metadata
    attempted = []
    torn = []

    def tear_metadata(stream, payload):
        assert _v3_fd_path(stream.fileno()) == owner.path
        attempted.append(dict(payload))
        raw = run_lock_module._canonical_bytes(payload)
        amount = {"blank": 0, "half": len(raw) // 2, "last_byte": len(raw.rstrip()) - 1}[tear]
        stream.seek(0)
        stream.truncate()
        stream.flush()
        run_lock_module._write_complete(stream.fileno(), raw[:amount])
        os.fsync(stream.fileno())
        torn.append(raw[:amount])
        raise OSError("v3 torn acknowledged writer fence")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(run_lock_module, "_write_stream_metadata", tear_metadata)
            if transition == "acquire":
                with pytest.raises(MinecraftTargetLockUnavailableError):
                    _lock(tmp_path, "v3-torn-acquire").acquire()
            elif transition == "quarantine":
                with pytest.raises(OSError, match="torn acknowledged"):
                    active.quarantine(run_name="v3-tear", reasons=["test-only"], diagnostics={})
            elif transition == "release":
                assert active.release().status is MinecraftTargetLockReleaseStatus.UNCERTAIN
            elif transition == "clear":
                with pytest.raises(OSError, match="torn acknowledged"):
                    clear_minecraft_target_quarantine(
                        lock_root=owner.lock_root, host=owner.host, port=owner.port,
                        reason="test-only tear", acknowledge_target_safe=True, force_corrupt=True,
                    )
            else:
                outcome = _acknowledge_predecessor(owner, inspected, storage_qualification=receipt)
                assert outcome.status is MinecraftTargetPredecessorAcknowledgementStatus.UNCERTAIN
                assert outcome.acknowledged is False
        assert len(attempted) == len(torn) == 1
        assert owner.path.read_bytes() == torn[0]
    finally:
        if active is not None and active._stream is not None:
            _crash_close_without_release(active)
            run_lock_module._UNVERIFIED_RELEASE_LOCKS.pop(id(active), None)
    observed = _predecessor_status(owner, receipt)
    assert observed.status is not MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN
    assert observed.writer_epoch is None and observed.revision is None
    assert observed.token.state["writer_epoch"] is None
    assert observed.token.state["revision"] is None
    preserved_history = owner.path.with_suffix(".history").read_bytes()
    assert preserved_history
    if tear != "blank" or owner.path.with_suffix(".uncertain").exists():
        with pytest.raises(MinecraftTargetLockError):
            _lock(tmp_path, "v3-no-guessed-high-water").acquire()
        assert owner.path.read_bytes() == torn[0]
        assert owner.path.with_suffix(".history").read_bytes() == preserved_history
    else:
        # Blank metadata has no intact writer fence. Generic migration is
        # allowed ONLY into a fresh provisional epoch; pending remains present
        # and nonclean. It is not an independent admission block like uncertain.
        pending_path = owner.path.with_suffix(".history-clear-pending")
        pending_before = pending_path.read_bytes() if pending_path.exists() else None
        provisional = _lock(tmp_path, "v3-blank-fresh-provisional").acquire()
        try:
            migrated, history = _assert_v3_metadata_history_pointer_coherent(provisional)
            assert migrated["writer_epoch"] not in {high_water["writer_epoch"], attempted[0]["writer_epoch"]}
            assert migrated["revision"] == 1
            assert history["state"] == "LEGACY_UNKNOWN"
            assert provisional.retained_predecessor_snapshot(storage_qualification=receipt).status is not MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN
            assert (pending_path.read_bytes() if pending_path.exists() else None) == pending_before
        finally:
            _crash_close_without_release(provisional)
    # Separate explicit raw diagnosis establishes a NEW provisional epoch;
    # it must not resurrect either the prior ack or a partially written fence.
    cleared = clear_minecraft_target_quarantine(
        lock_root=owner.lock_root, host=owner.host, port=owner.port,
        reason="test-only torn fence independently diagnosed", acknowledge_target_safe=True,
        force_corrupt=True,
    )
    assert cleared["writer_epoch"] not in {high_water["writer_epoch"], attempted[0]["writer_epoch"]}
    assert cleared["revision"] == 1
    assert cleared["revision"] != high_water["revision"]
    assert _predecessor_status(owner, receipt).status is not MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN


@pytest.mark.parametrize("fault", ["fsync", "installed_readback"])
def test_v3_5c_57_release_metadata_low_level_failure_preserves_u(tmp_path, monkeypatch, fault):
    # 5c-57: fault *release* metadata, not acquire/ack or marker persistence.
    previous = _v3_abandoned_owner(tmp_path, "v3-release-low-level-prior")
    owner = _lock(tmp_path, "v3-release-low-level-current").acquire()
    history_path = owner.path.with_suffix(".history")
    before_history = history_path.read_bytes()
    before_metadata, _ = _assert_v3_metadata_history_pointer_coherent(owner)
    real_write = run_lock_module._write_stream_metadata
    real_fsync, real_pread = os.fsync, os.pread
    started, fired = [], []

    def mark_release(stream, payload):
        assert payload["status"] == "released"
        started.append(dict(payload))
        return real_write(stream, payload)

    def cut_fsync(fd):
        if started and _v3_fd_path(fd) == owner.path and not fired:
            fired.append("released metadata fsync")
            raise OSError(fired[0])
        return real_fsync(fd)

    def cut_readback(fd, size, offset=0):
        raw = real_pread(fd, size, offset)
        if started and _v3_fd_path(fd) == owner.path and not fired:
            fired.append("released metadata installed readback")
            assert raw == run_lock_module._canonical_bytes(started[0])
            return raw + b" "
        return raw

    try:
        with monkeypatch.context() as patch:
            patch.setattr(run_lock_module, "_write_stream_metadata", mark_release)
            patch.setattr(run_lock_module.os, "fsync" if fault == "fsync" else "pread",
                          cut_fsync if fault == "fsync" else cut_readback)
            outcome = owner.release()
        assert len(started) == len(fired) == 1
        assert outcome.status is MinecraftTargetLockReleaseStatus.UNCERTAIN
        assert outcome.uncertainty_persisted is True
        metadata, history = _assert_v3_metadata_history_pointer_coherent(owner)
        assert metadata["status"] == "released"
        assert metadata["revision"] == before_metadata["revision"] + 1
        assert history_path.read_bytes() == before_history
        assert history["first"]["attempt_id"] == previous.attempt_id
        assert owner.path.with_suffix(".uncertain").exists()
        assert _predecessor_status(owner).status is not MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN
    finally:
        if owner._stream is not None:
            _crash_close_without_release(owner)
            run_lock_module._UNVERIFIED_RELEASE_LOCKS.pop(id(owner), None)


def test_v3_5c_60_final_release_marker_removal_directory_fsync_cut(tmp_path, monkeypatch):
    # 5c-60: spy the unlink; initial ReleaseInProgress publication MUST succeed.
    previous = _v3_abandoned_owner(tmp_path, "v3-release-marker-prior")
    owner = _lock(tmp_path, "v3-release-marker-current").acquire()
    history_path = owner.path.with_suffix(".history")
    before_history = history_path.read_bytes()
    marker = owner.path.with_suffix(".uncertain")
    real_unlink = os.unlink
    real_sync = run_lock_module._fsync_parent_directory
    events = []
    unlinked = []

    def mark_unlink(path, *args, **kwargs):
        if Path(path) == marker:
            record = json.loads(marker.read_text())
            assert record["error_type"] == "ReleaseInProgress"
        result = real_unlink(path, *args, **kwargs)
        if Path(path) == marker:
            unlinked.append(True)
            events.append("unlink ReleaseInProgress")
        return result

    def cut_final_sync(path):
        if Path(path) == marker:
            if unlinked and not marker.exists():
                events.append("final removal directory fsync failure")
                raise OSError("final ReleaseInProgress removal fsync cut")
            result = real_sync(path)
            events.append("fallback marker durable" if unlinked else "initial marker durable")
            return result
        return real_sync(path)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(run_lock_module.os, "unlink", mark_unlink)
            patch.setattr(run_lock_module, "_fsync_parent_directory", cut_final_sync)
            outcome = owner.release()
        assert events == ["initial marker durable", "unlink ReleaseInProgress",
                          "final removal directory fsync failure", "fallback marker durable",
                          "fallback marker durable"]
        assert outcome.status is MinecraftTargetLockReleaseStatus.UNCERTAIN
        assert outcome.uncertainty_persisted is True
        assert "final ReleaseInProgress" in outcome.error
        metadata, history = _assert_v3_metadata_history_pointer_coherent(owner)
        assert metadata["status"] == "released"
        assert history_path.read_bytes() == before_history
        assert history["first"]["attempt_id"] == previous.attempt_id
        assert json.loads(marker.read_text())["error_type"] == "OSError"
        assert _predecessor_status(owner).status is not MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN
    finally:
        if owner._stream is not None:
            _crash_close_without_release(owner)
            run_lock_module._UNVERIFIED_RELEASE_LOCKS.pop(id(owner), None)


def test_v3_5c_64_normal_body_inherited_u_failed_cleanup(tmp_path, monkeypatch):
    # 5c-64: normal body, not the already-covered exception-body path.
    previous = _v3_abandoned_owner(tmp_path, "v3-normal-context-prior")
    owner = _lock(tmp_path, "v3-normal-context-current")
    writes = []
    with owner:
        history_bytes = owner.path.with_suffix(".history").read_bytes()
        metadata_bytes = owner.path.read_bytes()
        inherited = owner.retained_predecessor_snapshot()
        assert inherited.first["attempt_id"] == previous.attempt_id

        def fail_cleanup(payload):
            writes.append(payload["status"])
            raise OSError("v3 normal-body cleanup write failure")

        monkeypatch.setattr(owner, "_write_metadata", fail_cleanup)
    assert writes == ["released"]
    assert owner.last_release_outcome.status is MinecraftTargetLockReleaseStatus.UNCERTAIN
    assert owner.path.read_bytes() == metadata_bytes
    assert owner.path.with_suffix(".history").read_bytes() == history_bytes
    assert owner.path.with_suffix(".uncertain").exists()
    after = _predecessor_status(owner)
    assert after.status is MinecraftTargetPredecessorHistoryStatus.UNRESOLVED
    assert after.first == inherited.first
    assert read_minecraft_target_lock_status(lock_root=owner.lock_root, host=owner.host, port=owner.port)["blocking"] is True


def _v3_assert_every_container_immutable(value):
    if isinstance(value, Mapping):
        for key, nested in value.items():
            with pytest.raises(TypeError):
                value[key] = object()
            _v3_assert_every_container_immutable(nested)
    elif isinstance(value, tuple):
        for index, nested in enumerate(value):
            with pytest.raises(TypeError):
                value[index] = object()
            _v3_assert_every_container_immutable(nested)


def _v3_mutate_every_exported_leaf(value):
    if isinstance(value, dict):
        for key in list(value):
            if isinstance(value[key], (dict, list)):
                _v3_mutate_every_exported_leaf(value[key])
            else:
                value[key] = "v3-mutated-export"
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            if isinstance(nested, (dict, list)):
                _v3_mutate_every_exported_leaf(nested)
            else:
                value[index] = "v3-mutated-export"


def test_v3_5c_70_every_populated_snapshot_nested_field_is_detached_and_immutable(tmp_path):
    # 5c-70: obtain real populated current and BOTH historical prefix diagnostics.
    original = _v3_abandoned_owner(tmp_path, "v3-snapshot-a")
    receipt = _test_storage_qualification(original)
    assert _acknowledge_predecessor(original, _predecessor_status(original), storage_qualification=receipt).acknowledged
    _v3_abandoned_owner(tmp_path, "v3-snapshot-b")
    _v3_abandoned_owner(tmp_path, "v3-snapshot-c")
    with _lock(tmp_path, "v3-snapshot-d"):
        pass
    assert _acknowledge_predecessor(original, _predecessor_status(original), storage_qualification=receipt).acknowledged
    _v3_abandoned_owner(tmp_path, "v3-snapshot-e")
    owner = _lock(tmp_path, "v3-snapshot-f").acquire()
    try:
        metadata_before = owner.path.read_bytes()
        history_before = owner.path.with_suffix(".history").read_bytes()
        snapshot = owner.retained_predecessor_snapshot(storage_qualification=receipt)
        expected = snapshot.to_dict()
        nested_names = {"first", "latest", "gaps", "acknowledgement", "current_owner",
                        "acknowledged_diagnostics", "prior_acknowledged_diagnostics",
                        "root_identity", "lock_identity"}
        actual_nested = {field.name for field in fields(snapshot)
                         if isinstance(getattr(snapshot, field.name), (Mapping, tuple))}
        assert actual_nested == nested_names
        for name in nested_names:
            assert getattr(snapshot, name) is not None
            _v3_assert_every_container_immutable(getattr(snapshot, name))
        _v3_assert_every_container_immutable(snapshot.token.state)
        for field in fields(snapshot):
            with pytest.raises(FrozenInstanceError):
                setattr(snapshot, field.name, None)
        exported = snapshot.to_dict()
        inputs = snapshot.to_dict()
        inputs["status"] = snapshot.status
        inputs["token"] = MinecraftTargetPredecessorInspectionToken(inputs["token"])
        detached = type(snapshot)(**inputs)
        _v3_mutate_every_exported_leaf(exported)
        for name in nested_names:
            _v3_mutate_every_exported_leaf(inputs[name])
        assert snapshot.to_dict() == detached.to_dict() == expected
        assert owner.retained_predecessor_snapshot(storage_qualification=receipt).to_dict() == expected
        assert owner.path.read_bytes() == metadata_before
        assert owner.path.with_suffix(".history").read_bytes() == history_before
        assert snapshot.status is MinecraftTargetPredecessorHistoryStatus.UNRESOLVED
    finally:
        owner.release()


def test_v3_5d_81_pending_short_writes_are_attributed_to_exact_opened_fd(tmp_path, monkeypatch):
    # 5d-81: shorten ONLY pending-temp writes; inspect installed bytes before CLEAN.
    previous = _v3_abandoned_owner(tmp_path, "v3-pending-short-fd")
    receipt = _test_storage_qualification(previous)
    inspected = _predecessor_status(previous)
    old_metadata = previous.path.read_bytes()
    old_history = previous.path.with_suffix(".history").read_bytes()
    real_write = os.write
    real_publish = run_lock_module._HistoryIO.publish
    writes, installed = [], []

    def short_pending(fd, raw):
        path = _v3_fd_path(fd)
        if path.parent == previous.lock_root and path.name.startswith(f".{previous.key}.history-clear-pending.tmp-"):
            amount = max(1, len(raw) // 3)
            written = real_write(fd, memoryview(raw)[:amount])
            writes.append((path.name, bytes(raw), written))
            return written
        return real_write(fd, raw)

    def observe_pending(self, suffix, payload):
        result = real_publish(self, suffix, payload)
        if suffix == "history-clear-pending":
            raw = self.read(suffix)
            assert raw == run_lock_module._canonical_bytes(payload)
            assert self.metadata_raw() == old_metadata and self.read("history") == old_history
            assert run_lock_module._inspect_predecessor(self).status is MinecraftTargetPredecessorHistoryStatus.DURABILITY_UNCERTAIN
            installed.append(raw)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(run_lock_module.os, "write", short_pending)
        patch.setattr(run_lock_module._HistoryIO, "publish", observe_pending)
        outcome = _acknowledge_predecessor(previous, inspected, storage_qualification=receipt)
    assert outcome.acknowledged is True
    assert len(installed) == 1 and len(writes) > 1
    assert len({name for name, _, _ in writes}) == 1
    assert any(written < len(raw) for _, raw, written in writes)
    assert b"".join(raw[:written] for _, raw, written in writes) == installed[0]
    assert not previous.path.with_suffix(".history-clear-pending").exists()
    metadata, history = _assert_v3_metadata_history_pointer_coherent(previous)
    assert metadata["status"] == "reconciled"
    assert history["acknowledgement"]["covered_metadata_digest"] == run_lock_module._digest(old_metadata)


@pytest.mark.parametrize("fault", ["write_error", "zero_progress"])
def test_v3_5d_91_ack_metadata_plain_write_or_no_progress_never_clean(tmp_path, monkeypatch, fault):
    # 5d-91: real writer truncates, then exact .lock FD write fails/makes no progress.
    previous = _v3_abandoned_owner(tmp_path, "v3-plain-ack-write")
    receipt = _test_storage_qualification(previous)
    inspected = _predecessor_status(previous)
    old_metadata = previous.path.read_bytes()
    real_write = os.write
    hits = []

    def fail_metadata(fd, raw):
        if _v3_fd_path(fd) == previous.path:
            hits.append(bytes(raw))
            assert json.loads(bytes(raw))["status"] == "reconciled"
            if fault == "write_error":
                raise OSError("plain acknowledgement metadata write failure")
            return 0
        return real_write(fd, raw)

    with monkeypatch.context() as patch:
        patch.setattr(run_lock_module.os, "write", fail_metadata)
        outcome = _acknowledge_predecessor(previous, inspected, storage_qualification=receipt)
    assert len(hits) == 1
    assert outcome.status is MinecraftTargetPredecessorAcknowledgementStatus.UNCERTAIN
    assert outcome.acknowledged is False and outcome.retained_lock is False
    assert previous.path.read_bytes() == b""
    pending = run_lock_module._strict_json(previous.path.with_suffix(".history-clear-pending").read_bytes(), canonical=True)
    assert pending["expected"]["metadata_digest"] == run_lock_module._digest(old_metadata)
    history = run_lock_module._strict_json(previous.path.with_suffix(".history").read_bytes(), canonical=True)
    assert history["acknowledged_diagnostics"]["first"]["attempt_id"] == previous.attempt_id
    observed = _predecessor_status(previous, receipt)
    assert observed.status is MinecraftTargetPredecessorHistoryStatus.DURABILITY_UNCERTAIN
    assert observed.writer_epoch is None and observed.revision is None


def test_v3_5d_95_ack_metadata_tear_abrupt_child_exit_is_nonclean(tmp_path):
    # 5d-95: actual process death DURING a tear, not a caught returned exception.
    previous = _v3_abandoned_owner(tmp_path, "v3-abrupt-ack-tear")
    receipt = _test_storage_qualification(previous)
    inspected = _predecessor_status(previous)
    old_metadata = previous.path.read_bytes()

    def die_during_metadata():
        real_write = run_lock_module._write_stream_metadata

        def tear_then_die(stream, payload):
            if payload.get("status") == "reconciled":
                raw = run_lock_module._canonical_bytes(payload)
                stream.seek(0)
                stream.truncate()
                stream.flush()
                run_lock_module._write_complete(stream.fileno(), raw[:len(raw) // 2])
                os.fsync(stream.fileno())
                os._exit(95)
            return real_write(stream, payload)

        run_lock_module._write_stream_metadata = tear_then_die
        _acknowledge_predecessor(previous, inspected, storage_qualification=receipt)
        os._exit(96)

    child = multiprocessing.get_context("fork").Process(target=die_during_metadata)
    child.start()
    _v3_join_abrupt_child(child, 95)
    torn = previous.path.read_bytes()
    assert torn and torn != old_metadata
    with pytest.raises((ValueError, MinecraftTargetLockMetadataError)):
        run_lock_module._strict_json(torn, canonical=True)
    pending = run_lock_module._strict_json(previous.path.with_suffix(".history-clear-pending").read_bytes(), canonical=True)
    assert pending["expected"] == run_lock_module._thaw(inspected.token.state)
    history = run_lock_module._validate_history(run_lock_module._strict_json(previous.path.with_suffix(".history").read_bytes(), canonical=True))
    assert history["state"] == "ACKNOWLEDGED_CLEAN"
    assert history["acknowledged_diagnostics"]["first"]["attempt_id"] == previous.attempt_id
    fresh = _predecessor_status(previous, receipt)
    assert fresh.status is MinecraftTargetPredecessorHistoryStatus.DURABILITY_UNCERTAIN
    assert fresh.writer_epoch is None and fresh.revision is None
    assert previous.path.read_bytes() == torn


def test_v3_5d_100_failed_unlink_fsync_restore_then_actual_exit_fresh_recovery(tmp_path):
    # 5d-100: combined failed final fsync + failed restoration + retained EX +
    # actual exit. Fresh independently verified CLEAN is legitimate recovery.
    previous = _v3_abandoned_owner(tmp_path, "v3-final-unlink-child")
    receipt = _test_storage_qualification(previous)
    inspected = _predecessor_status(previous)
    context = multiprocessing.get_context("fork")
    parent, child_pipe = context.Pipe()

    def final_cut_then_exit():
        parent.close()
        real_unlink, real_sync = os.unlink, run_lock_module._fsync_parent_directory
        real_publish = run_lock_module._HistoryIO.publish
        stages = []

        def mark_unlink(path, *args, **kwargs):
            result = real_unlink(path, *args, **kwargs)
            if Path(path).name == previous.path.with_suffix(".history-clear-pending").name:
                stages.append("unlinked")
            return result

        def fail_final(path):
            if path.name.endswith(".history-clear-pending") and "unlinked" in stages:
                stages.append("final fsync failed")
                raise OSError("v3 final pending removal fsync failed")
            return real_sync(path)

        def fail_restore(self, suffix, payload):
            if suffix == "history-clear-pending" and "unlinked" in stages:
                stages.append("restore failed")
                raise OSError("v3 pending restoration failed")
            return real_publish(self, suffix, payload)

        run_lock_module.os.unlink = mark_unlink
        run_lock_module._fsync_parent_directory = fail_final
        run_lock_module._HistoryIO.publish = fail_restore
        outcome = _acknowledge_predecessor(previous, inspected, storage_qualification=receipt)
        assert outcome.status is MinecraftTargetPredecessorAcknowledgementStatus.UNCERTAIN
        assert outcome.snapshot.status is MinecraftTargetPredecessorHistoryStatus.DURABILITY_UNCERTAIN
        assert outcome.retained_lock is True
        assert stages == ["unlinked", "final fsync failed", "restore failed"]
        assert not previous.path.with_suffix(".history-clear-pending").exists()
        child_pipe.send({"stages": stages, "status": outcome.status.value, "retained": outcome.retained_lock})
        assert child_pipe.poll(5), "parent never released the deterministic exit barrier"
        assert child_pipe.recv() == "exit"
        os._exit(100)

    child = context.Process(target=final_cut_then_exit)
    child.start()
    child_pipe.close()
    try:
        assert parent.poll(5), "child never reached retained-flock failure branch"
        message = parent.recv()
        assert message == {"stages": ["unlinked", "final fsync failed", "restore failed"], "status": "UNCERTAIN", "retained": True}
        metadata_before = previous.path.read_bytes()
        history_before = previous.path.with_suffix(".history").read_bytes()
        held = _predecessor_status(previous, receipt)
        assert held.status is MinecraftTargetPredecessorHistoryStatus.AMBIGUOUS
        assert held.active_owner is True
        with pytest.raises(MinecraftTargetLockBusyError):
            _lock(tmp_path, "v3-final-unlink-contender").acquire()
        parent.send("exit")
        _v3_join_abrupt_child(child, 100)
    finally:
        parent.close()
        if not child._closed:
            if child.is_alive():
                child.terminate()
            child.join(5)
            assert not child.is_alive()
            child.close()
    fresh = _predecessor_status(previous, receipt)
    assert fresh.status is MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN
    metadata, history = _assert_v3_metadata_history_pointer_coherent(previous)
    assert metadata["status"] == "reconciled" and history["state"] == "ACKNOWLEDGED_CLEAN"
    assert history["acknowledgement"]["covered_metadata_digest"] == inspected.token.state["metadata_digest"]
    assert history["acknowledged_diagnostics"]["first"]["attempt_id"] == previous.attempt_id
    assert history["acknowledgement"]["storage_profile_id"] == receipt.storage_profile_id
    assert not previous.path.with_suffix(".history-clear-pending").exists()
    assert not previous.path.with_suffix(".uncertain").exists()
    assert previous.path.read_bytes() == metadata_before
    assert previous.path.with_suffix(".history").read_bytes() == history_before


def test_v3_5d_101_acknowledgement_rejects_already_active_owner(tmp_path, monkeypatch):
    # 5d-101: predecessor ack, not quarantine clear, while EX is already held.
    previous = _v3_abandoned_owner(tmp_path, "v3-active-ack-prior")
    receipt = _test_storage_qualification(previous)
    inspected = _predecessor_status(previous)
    owner = _lock(tmp_path, "v3-active-ack-current").acquire()
    try:
        metadata_before = owner.path.read_bytes()
        history_before = owner.path.with_suffix(".history").read_bytes()
        publications = []
        real_publish = run_lock_module._HistoryIO.publish

        def observe_publish(self, suffix, payload):
            publications.append(suffix)
            return real_publish(self, suffix, payload)

        with monkeypatch.context() as patch:
            patch.setattr(run_lock_module._HistoryIO, "publish", observe_publish)
            outcome = _acknowledge_predecessor(previous, inspected, storage_qualification=receipt)
        assert outcome.status is MinecraftTargetPredecessorAcknowledgementStatus.UNCERTAIN
        assert outcome.acknowledged is False and outcome.retained_lock is False
        assert "MinecraftTargetLockBusyError: target owner is active" == outcome.error
        assert publications == []
        assert owner.path.read_bytes() == metadata_before
        assert owner.path.with_suffix(".history").read_bytes() == history_before
        assert not owner.path.with_suffix(".history-clear-pending").exists()
        assert not owner.path.with_suffix(".uncertain").exists()
        assert owner.retained_predecessor_snapshot().status is MinecraftTargetPredecessorHistoryStatus.UNRESOLVED
        assert _predecessor_status(owner, receipt).status is MinecraftTargetPredecessorHistoryStatus.AMBIGUOUS
    finally:
        owner.release()


@pytest.mark.parametrize("legacy", ["v1_released", "v2_released", "blank", "absent"])
@pytest.mark.parametrize("stage", ["temporary_write", "temporary_fsync", "replace",
                                   "directory_fsync", "installed_readback", "metadata_tear", "after_acquired"])
def test_v3_migration_102_legacy_blank_generic_abrupt_publication_cuts(tmp_path, legacy, stage):
    # migration-102: actual child exits across each materially distinct generic
    # provisional publication boundary, including legacy/blank/virgin inputs.
    owner = _lock(tmp_path, f"v3-migration-{legacy}-{stage}")
    owner.lock_root.mkdir(parents=True)
    if legacy == "v1_released":
        _write_schema_v1_metadata(owner, status="released", attempt_id="v3-legacy-released")
    elif legacy == "v2_released":
        owner.path.write_text(json.dumps(_schema_v2_metadata(owner, status="released")))
    elif legacy == "blank":
        owner.path.write_bytes(b"")
    original = owner.path.read_bytes() if owner.path.exists() else b""

    def die_during_migration():
        real_write = run_lock_module._write_complete
        real_fsync, real_replace = os.fsync, os.replace
        real_sync = run_lock_module._fsync_parent_directory
        real_read = run_lock_module._HistoryIO.read
        real_metadata = run_lock_module._write_stream_metadata
        installed = []

        def is_history_temp(fd):
            path = _v3_fd_path(fd)
            return path.parent == owner.lock_root and path.name.startswith(f".{owner.key}.history.tmp-")

        def after_write(fd, raw):
            result = real_write(fd, raw)
            if stage == "temporary_write" and is_history_temp(fd):
                os._exit(102)
            return result

        def after_fsync(fd):
            result = real_fsync(fd)
            if stage == "temporary_fsync" and is_history_temp(fd):
                os._exit(102)
            return result

        def after_replace(source, target, *args, **kwargs):
            result = real_replace(source, target, *args, **kwargs)
            if Path(target) == owner.path.with_suffix(".history"):
                installed.append(True)
                if stage == "replace":
                    os._exit(102)
            return result

        def after_directory_fsync(path):
            result = real_sync(path)
            if stage == "directory_fsync" and Path(path) == owner.path.with_suffix(".history") and installed:
                os._exit(102)
            return result

        def after_installed_readback(self, suffix):
            result = real_read(self, suffix)
            if stage == "installed_readback" and suffix == "history" and installed:
                assert result is not None
                os._exit(102)
            return result

        def during_metadata(stream, payload):
            if stage == "metadata_tear" and payload.get("status") == "acquired":
                raw = run_lock_module._canonical_bytes(payload)
                stream.seek(0)
                stream.truncate()
                stream.flush()
                real_write(stream.fileno(), raw[:len(raw) // 2])
                real_fsync(stream.fileno())
                os._exit(102)
            return real_metadata(stream, payload)

        run_lock_module._write_complete = after_write
        run_lock_module.os.fsync = after_fsync
        run_lock_module.os.replace = after_replace
        run_lock_module._fsync_parent_directory = after_directory_fsync
        run_lock_module._HistoryIO.read = after_installed_readback
        run_lock_module._write_stream_metadata = during_metadata
        owner.acquire()
        if stage == "after_acquired":
            os._exit(102)
        os._exit(103)

    child = multiprocessing.get_context("fork").Process(target=die_during_migration)
    child.start()
    _v3_join_abrupt_child(child, 102)
    current = owner.path.read_bytes()
    history_path = owner.path.with_suffix(".history")
    if stage in {"temporary_write", "temporary_fsync"}:
        assert current == original
        assert not history_path.exists()
        orphans = list(owner.lock_root.glob(f".{owner.key}.history.tmp-*"))
        assert len(orphans) == 1
        history_raw = orphans[0].read_bytes()
    else:
        assert history_path.exists()
        assert list(owner.lock_root.glob(f".{owner.key}.history.tmp-*")) == []
        history_raw = history_path.read_bytes()
        if stage not in {"metadata_tear", "after_acquired"}:
            assert current == original
    history = run_lock_module._validate_history(run_lock_module._strict_json(history_raw, canonical=True))
    assert history["state"] == "LEGACY_UNKNOWN" and history["acknowledgement"] is None
    assert history["source"]["metadata_digest"] == run_lock_module._digest(original)
    assert history["gaps"]["count"] > 0 and history["observation_count"] == 0
    if stage == "metadata_tear":
        assert current and current != original
        with pytest.raises((ValueError, MinecraftTargetLockMetadataError)):
            run_lock_module._strict_json(current, canonical=True)
    elif stage == "after_acquired":
        metadata, committed = _assert_v3_metadata_history_pointer_coherent(owner)
        assert committed == history
        assert metadata["status"] == "acquired" and metadata["revision"] == 1
    observed = _predecessor_status(owner)
    assert observed.status is not MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN
    assert observed.active_owner is not True
    assert owner.path.read_bytes() == current
    assert not owner.path.with_suffix(".history-clear-pending").exists()
    assert not owner.path.with_suffix(".uncertain").exists()
