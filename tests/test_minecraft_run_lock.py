import hashlib
import json
import multiprocessing
import os
import threading
import time
from dataclasses import FrozenInstanceError, fields

import pytest

from benchmarks.minecraft.run_lock import (
    MinecraftTargetLock,
    MinecraftTargetLockBusyError,
    MinecraftTargetLockError,
    MinecraftTargetLockMetadataError,
    MinecraftTargetLockUnavailableError,
    MinecraftTargetLeaseSnapshot,
    MinecraftTargetQuarantinedError,
    clear_minecraft_target_quarantine,
    minecraft_target_lock_key,
    read_minecraft_target_lock_metadata,
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
        lock.release()
        if lock.path.exists():
            lock.path.unlink()


def test_retained_lease_snapshot_fails_for_missing_path(tmp_path):
    lock = _lock(tmp_path, "attempt-missing-path").acquire()
    try:
        lock.path.unlink()
        with pytest.raises(MinecraftTargetLockUnavailableError):
            lock.retained_lease_snapshot()
    finally:
        lock.release()


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
        lock.release()


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
    with pytest.raises(RuntimeError, match="child failed"):
        with _lock(tmp_path, "attempt-a"):
            raise RuntimeError("child failed")

    with _lock(tmp_path, "attempt-b") as replacement:
        assert replacement.acquired is True


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
