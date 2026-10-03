import hashlib
import json
import multiprocessing
import os
import threading
import time
from dataclasses import FrozenInstanceError, fields

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
    MinecraftTargetQuarantinedError,
    clear_minecraft_target_quarantine,
    minecraft_target_lock_key,
    read_minecraft_target_lock_metadata,
    read_minecraft_target_lock_status,
)


def _lock(tmp_path, attempt_id, *, port=25565):
    return MinecraftTargetLock(
        lock_root=tmp_path / "locks",
        host="127.0.0.1",
        port=port,
        world_id="world-a",
        attempt_id=attempt_id,
    )


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


def test_retained_lease_snapshot_classifies_unrepresentable_owner_pid(tmp_path):
    lock = _lock(tmp_path, "attempt-oversized-pid").acquire()
    original_content = lock.path.read_text(encoding="utf-8")
    try:
        metadata = json.loads(original_content)
        metadata["pid"] = 1 << 100
        lock.path.write_text(json.dumps(metadata), encoding="utf-8")

        with pytest.raises(MinecraftTargetLockMetadataError, match="owner pid"):
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
        assert snapshot.metadata["schema_version"] == 2
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


def test_schema_v1_released_metadata_migrates_on_acquire(tmp_path):
    lock = _lock(tmp_path, "attempt-new")
    _write_schema_v1_metadata(lock, status="released", attempt_id="attempt-old")

    with lock:
        metadata = json.loads(lock.path.read_text(encoding="utf-8"))
        assert metadata["schema_version"] == 2
        assert metadata["status"] == "acquired"
        assert metadata["attempt_id"] == "attempt-new"
        assert metadata["lock_key"] == lock.key
        assert metadata["host"] == "127.0.0.1"
        assert metadata["port"] == 25565
        assert metadata["migrated_from_schema_version"] == 1
        assert metadata["previous_status"] == "released"


def test_schema_v1_dead_acquired_owner_migrates_as_stale(tmp_path):
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
        assert metadata["schema_version"] == 2
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


def test_generated_schema_v2_states_validate(tmp_path):
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
