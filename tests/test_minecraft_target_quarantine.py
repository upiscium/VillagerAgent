import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.minecraft import run_lock
from benchmarks.minecraft.run_lock import (
    MinecraftTargetLock,
    MinecraftTargetLockReleaseStatus,
)
from benchmarks.minecraft.target_quarantine import main, parse_args


def test_status_reports_absent_and_persistent_quarantine(tmp_path, capsys):
    args = _base_args(tmp_path)
    assert main(["status", *args]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["quarantined"] is False
    assert payload["uncertain"] is False
    assert payload["actively_owned"] is False
    assert payload["blocking"] is False

    lock = _lock(tmp_path).acquire()
    lock.quarantine(
        run_name="run-a",
        reasons=["bridge_cleanup_incomplete"],
        diagnostics={},
    )
    lock.release()

    assert main(["status", *args]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["quarantined"] is True
    assert payload["uncertain"] is False
    assert payload["actively_owned"] is False
    assert payload["blocking"] is True
    assert payload["metadata"]["attempt_id"] == "attempt-a"


def test_status_reports_marker_only_as_blocking_uncertainty(tmp_path, capsys):
    lock = _lock(tmp_path)
    lock.path.parent.mkdir(parents=True)
    lock.path.with_suffix(".uncertain").write_text(json.dumps({
        "schema_version": 1,
        "status": "uncertain",
        "attempt_id": "attempt-marker",
        "error_type": "OSError",
        "error": "simulated uncertain outcome",
    }), encoding="utf-8")

    assert main(["status", *_base_args(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["metadata"] == {}
    assert payload["quarantined"] is True
    assert payload["uncertain"] is True
    assert payload["actively_owned"] is False
    assert payload["uncertainty"]["valid"] is True
    assert payload["blocking"] is True


def test_status_reports_corrupt_marker_as_blocking_uncertainty(tmp_path, capsys):
    lock = _lock(tmp_path)
    lock.path.parent.mkdir(parents=True)
    lock.path.with_suffix(".uncertain").write_text("{", encoding="utf-8")

    assert main(["status", *_base_args(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["metadata"] == {}
    assert payload["quarantined"] is True
    assert payload["uncertain"] is True
    assert payload["uncertainty"]["present"] is True
    assert payload["uncertainty"]["valid"] is False
    assert payload["blocking"] is True


def test_status_reports_active_owner_as_blocking_not_uncertain(tmp_path, capsys):
    owner = _lock(tmp_path).acquire()
    try:
        assert main(["status", *_base_args(tmp_path)]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["metadata"]["attempt_id"] == "attempt-a"
        assert payload["quarantined"] is False
        assert payload["uncertain"] is False
        assert payload["actively_owned"] is True
        assert payload["blocking"] is True
    finally:
        owner.release()


def test_clear_requires_acknowledgement(tmp_path, capsys):
    _quarantine(tmp_path)

    assert main([
        "clear",
        *_base_args(tmp_path),
        "--reason",
        "Verified cleanup",
    ]) == 1
    assert "acknowledge_target_safe" in capsys.readouterr().out


def test_clear_rejects_empty_reason(tmp_path, capsys):
    _quarantine(tmp_path)

    assert main([
        "clear",
        *_base_args(tmp_path),
        "--reason",
        " ",
        "--acknowledge-target-safe",
    ]) == 1
    assert "non-empty reason" in capsys.readouterr().out


def test_clear_rejects_active_owner(tmp_path, capsys):
    lock = _lock(tmp_path).acquire()
    try:
        assert main([
            "clear",
            *_base_args(tmp_path),
            "--reason",
            "Verified cleanup",
            "--acknowledge-target-safe",
        ]) == 1
        assert "actively locked" in capsys.readouterr().out
    finally:
        lock.release()


def test_clear_records_reason_and_allows_new_owner(tmp_path, capsys):
    _quarantine(tmp_path)

    assert main([
        "clear",
        *_base_args(tmp_path),
        "--reason",
        "Verified cleanup",
        "--acknowledge-target-safe",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "cleared"
    assert payload["clear_reason"] == "Verified cleanup"
    assert main(["status", *_base_args(tmp_path)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["metadata"]["status"] == "cleared"
    assert status["quarantined"] is False
    assert status["uncertain"] is False
    assert status["actively_owned"] is False
    assert status["blocking"] is False
    replacement = MinecraftTargetLock(
        lock_root=tmp_path / "locks",
        host="127.0.0.1",
        port=25565,
        world_id="world-a",
        attempt_id="attempt-b",
    ).acquire()
    replacement.release()


def test_clear_corrupt_metadata_requires_force_corrupt(tmp_path, capsys):
    lock = _lock(tmp_path)
    lock.path.parent.mkdir(parents=True)
    lock.path.write_text("{", encoding="utf-8")
    clear_args = [
        "clear",
        *_base_args(tmp_path),
        "--reason",
        "Verified cleanup",
        "--acknowledge-target-safe",
    ]

    assert main(clear_args) == 1
    assert "force_corrupt" in capsys.readouterr().out
    assert main([*clear_args, "--force-corrupt"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "cleared"


def test_status_accepts_legacy_schema_v1_metadata(tmp_path, capsys):
    _write_schema_v1_released_metadata(tmp_path)

    assert main(["status", *_base_args(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["quarantined"] is False
    assert payload["metadata"]["schema_version"] == 1
    assert payload["metadata"]["status"] == "released"


def test_clear_reports_legacy_schema_as_not_quarantined(tmp_path, capsys):
    _write_schema_v1_released_metadata(tmp_path)

    assert main([
        "clear",
        *_base_args(tmp_path),
        "--reason",
        "Verified cleanup",
        "--acknowledge-target-safe",
    ]) == 1
    output = capsys.readouterr().out
    assert "not quarantined" in output
    assert "force_corrupt" not in output


def _base_args(tmp_path):
    return [
        "--host",
        "127.0.0.1",
        "--port",
        "25565",
        "--lock-root",
        str(tmp_path / "locks"),
    ]


def _lock(tmp_path, attempt_id="attempt-a"):
    return MinecraftTargetLock(
        lock_root=tmp_path / "locks",
        host="127.0.0.1",
        port=25565,
        world_id="world-a",
        attempt_id=attempt_id,
    )


def _quarantine(tmp_path):
    lock = _lock(tmp_path).acquire()
    lock.quarantine(
        run_name="run-a",
        reasons=["bridge_cleanup_incomplete"],
        diagnostics={},
    )
    lock.release()


def _write_schema_v1_released_metadata(tmp_path):
    lock = _lock(tmp_path)
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_text(json.dumps({
        "schema_version": 1,
        "status": "released",
        "attempt_id": "attempt-legacy",
        "pid": 99999999,
        "host": "127.0.0.1",
        "port": 25565,
        "world_id": "world-a",
        "lock_key": lock.key,
    }), encoding="utf-8")


def test_status_without_storage_qualification_never_reports_clean(tmp_path, capsys):
    assert main(["status", *_base_args(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["blocking"] is False
    assert payload["history"]["status"] != "ACKNOWLEDGED_CLEAN"
    assert payload["storage_qualification"] is None


def test_status_exposes_separate_qualified_history_and_exact_inspection_token(tmp_path, capsys):
    assert main(_qualified_status_args(tmp_path)) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["blocking"] is False
    assert payload["quarantined"] is False
    assert payload["uncertain"] is False
    assert payload["history"]["status"] == "LEGACY_UNKNOWN"
    assert payload["storage_qualification"] == {
        "storage_profile_id": "test-only",
        "storage_profile_version": 1,
    }
    token = payload["inspection_token"]
    assert isinstance(token, str)
    assert token.endswith("\n")
    assert json.loads(token) == payload["history"]["token"]


def test_status_reports_busy_predecessor_observation_without_inspection_token(
    tmp_path, capsys,
):
    _test_qualification_path(tmp_path)
    owner = _lock(tmp_path).acquire()
    try:
        assert main(_qualified_status_args(tmp_path)) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["actively_owned"] is True
        assert payload["blocking"] is True
        assert payload["history"]["status"] == "AMBIGUOUS"
        assert payload["history"]["token"] is None
        assert payload["inspection_token"] is None
    finally:
        owner.release()


def test_generic_acquire_release_does_not_launder_unresolved_history(tmp_path, capsys):
    _test_qualification_path(tmp_path)
    crashed = _lock(tmp_path, attempt_id="attempt-crashed").acquire()
    _crash_close_without_release(crashed)

    assert main(_qualified_status_args(tmp_path)) == 0
    before = json.loads(capsys.readouterr().out)
    assert before["history"]["status"] == "UNRESOLVED"

    cycle = _lock(tmp_path, attempt_id="attempt-cycle").acquire()
    assert cycle.release().status is MinecraftTargetLockReleaseStatus.VERIFIED_RELEASED

    assert main(_qualified_status_args(tmp_path)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["blocking"] is False
    assert payload["history"]["status"] == "UNRESOLVED"


def test_predecessor_clear_rejects_stale_token_after_generic_cycle(tmp_path, capsys):
    token = _inspection_token(tmp_path, capsys)
    cycle = _lock(tmp_path, attempt_id="attempt-cycle").acquire()
    assert cycle.release().status is MinecraftTargetLockReleaseStatus.VERIFIED_RELEASED

    assert main(_predecessor_clear_args(tmp_path, token, reconcile=True)) == 1
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["status"] == "UNCERTAIN"
    assert outcome["acknowledged"] is False
    assert "CAS" in outcome["error"] or "changed" in outcome["error"]

    assert main(_qualified_status_args(tmp_path)) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["history"]["status"] == "LEGACY_UNKNOWN"


def test_predecessor_clear_acknowledges_exact_token_and_reports_clean_status(tmp_path, capsys):
    _test_qualification_path(tmp_path)
    crashed = _lock(tmp_path, attempt_id="attempt-crashed").acquire()
    _crash_close_without_release(crashed)
    cycle = _lock(tmp_path, attempt_id="attempt-cycle").acquire()
    assert cycle.release().status is MinecraftTargetLockReleaseStatus.VERIFIED_RELEASED
    token = _inspection_token(tmp_path, capsys)
    inspected = json.loads(token)
    inspected_history = inspected["history_pointer"]
    assert inspected_history is not None
    assert inspected["metadata_digest"] is not None

    assert main(_predecessor_clear_args(tmp_path, token, reconcile=True)) == 0
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["status"] == "ACKNOWLEDGED"
    assert outcome["acknowledged"] is True
    assert outcome["error"] is None
    assert outcome["retained_lock"] is False
    assert outcome["inspection_token"] == token
    snapshot = outcome["snapshot"]
    assert snapshot["status"] == "ACKNOWLEDGED_CLEAN"
    acknowledgement = snapshot["acknowledgement"]
    assert acknowledgement["whole_prefix"] is True
    assert acknowledgement["operator"] == "operator-test"
    assert acknowledgement["covered_history"] == inspected_history
    assert acknowledgement["covered_metadata"] == {
        "raw_digest": inspected["metadata_digest"],
        "kind": inspected["metadata_kind"],
        "writer_epoch": inspected["writer_epoch"],
        "revision": inspected["revision"],
        "transition_nonce": inspected["transition_nonce"],
    }
    assert {
        "generation": acknowledgement["covered_generation"],
        "ordinal": acknowledgement["covered_ordinal"],
        "digest": acknowledgement["covered_digest"],
    } == inspected_history
    assert acknowledgement["covered_metadata_digest"] == inspected["metadata_digest"]
    assert acknowledgement["covered_writer_epoch"] == inspected["writer_epoch"]
    assert acknowledgement["storage_profile_id"] == "test-only"
    assert acknowledgement["storage_profile_version"] == 1
    acknowledgement_json = json.dumps(acknowledgement)
    assert "receipt-test-only" not in acknowledgement_json
    actual_boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    assert actual_boot_id not in acknowledgement_json

    assert main(_qualified_status_args(tmp_path)) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["metadata"]["status"] == "reconciled"
    assert status["blocking"] is False
    assert status["history"]["status"] == "ACKNOWLEDGED_CLEAN"

    # A complete pair is not enough for strict CLEAN when this observation has
    # no current root-bound qualification receipt.
    assert main(["status", *_base_args(tmp_path)]) == 0
    unqualified = json.loads(capsys.readouterr().out)
    assert unqualified["history"]["status"] != "ACKNOWLEDGED_CLEAN"


def test_clean_history_requires_profile_and_accepts_fresh_same_profile_boot_receipt(
    tmp_path, capsys, monkeypatch,
):
    token = _inspection_token(tmp_path, capsys)
    assert main(_predecessor_clear_args(tmp_path, token, reconcile=True)) == 0
    capsys.readouterr()

    rebooted_boot_id = "test-only-simulated-reboot"
    monkeypatch.setattr(run_lock, "_current_boot_id", lambda: rebooted_boot_id)
    different_profile = _test_qualification_path(
        tmp_path,
        filename="different-profile.json",
        profile_id="other-test-only-profile",
        boot_id=rebooted_boot_id,
    )
    assert main(_qualified_status_args(tmp_path, receipt_path=different_profile)) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["history"]["status"] != "ACKNOWLEDGED_CLEAN"

    # A fresh receipt for the same profile remains usable; receipt and boot
    # identifiers are not the persisted profile identity.
    fresh_same_profile = _test_qualification_path(
        tmp_path,
        filename="fresh-same-profile.json",
        profile_id="test-only",
        receipt_id="receipt-test-only-fresh",
        boot_id=rebooted_boot_id,
    )
    assert main(_qualified_status_args(tmp_path, receipt_path=fresh_same_profile)) == 0
    same_profile_status = json.loads(capsys.readouterr().out)
    assert same_profile_status["history"]["status"] == "ACKNOWLEDGED_CLEAN"


def test_unknown_history_requires_explicit_reconciliation_election(tmp_path, capsys):
    token = _inspection_token(tmp_path, capsys)

    assert main(_predecessor_clear_args(tmp_path, token)) == 1
    declined = json.loads(capsys.readouterr().out)
    assert declined["status"] == "UNCERTAIN"
    assert declined["acknowledged"] is False
    assert "unknown-history" in declined["error"]

    assert main(_predecessor_clear_args(tmp_path, token, reconcile=True)) == 0
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["acknowledged"] is True
    assert accepted["snapshot"]["status"] == "ACKNOWLEDGED_CLEAN"


@pytest.mark.parametrize("blocker", ["quarantine", "uncertainty"])
def test_predecessor_clear_preserves_separate_quarantine_and_uncertainty_blockers(
    tmp_path, capsys, blocker,
):
    _test_qualification_path(tmp_path)
    lock = _lock(tmp_path)
    if blocker == "quarantine":
        _quarantine(tmp_path)
    else:
        lock.path.parent.mkdir(parents=True, exist_ok=True)
        lock.path.with_suffix(".uncertain").write_text(
            json.dumps({
                "schema_version": 1,
                "status": "uncertain",
                "attempt_id": "attempt-independent-uncertainty",
                "error_type": "OSError",
                "error": "independent blocker",
            }),
            encoding="utf-8",
        )

    assert main(_qualified_status_args(tmp_path)) == 0
    before = json.loads(capsys.readouterr().out)
    token = before["inspection_token"]
    assert before["blocking"] is True

    assert main(_predecessor_clear_args(tmp_path, token, reconcile=True)) == 1
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["status"] == "UNCERTAIN"
    assert outcome["acknowledged"] is False

    assert main(_qualified_status_args(tmp_path)) == 0
    after = json.loads(capsys.readouterr().out)
    assert after["blocking"] is True
    assert after["quarantined"] is before["quarantined"]
    assert after["uncertain"] is before["uncertain"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("no-target-safe", "target-safe"),
        ("no-whole-prefix", "whole-prefix"),
        ("empty-reason", "nonempty reason"),
        ("empty-operator", "nonempty reason and operator"),
    ],
)
def test_predecessor_clear_requires_explicit_intent_and_nonempty_audit_values(
    tmp_path, capsys, change, message,
):
    token = _inspection_token(tmp_path, capsys)
    args = _predecessor_clear_args(tmp_path, token)
    if change == "no-target-safe":
        args.remove("--acknowledge-target-safe")
    elif change == "no-whole-prefix":
        args.remove("--acknowledge-whole-prefix")
    elif change == "empty-reason":
        args[args.index("--reason") + 1] = " "
    else:
        args[args.index("--operator") + 1] = " "

    assert main(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert message in payload["error"]
    assert not _lock(tmp_path).path.exists()


@pytest.mark.parametrize("bad_token_kind", ["invalid", "unknown-field", "duplicate-field", "oversized"])
def test_predecessor_clear_rejects_invalid_or_unbounded_expected_tokens_without_mutation(
    tmp_path, capsys, bad_token_kind,
):
    valid = _inspection_token(tmp_path, capsys)
    if bad_token_kind == "invalid":
        token = "not canonical JSON"
    elif bad_token_kind == "unknown-field":
        payload = json.loads(valid)
        payload["unexpected"] = True
        token = _canonical_json(payload)
    elif bad_token_kind == "duplicate-field":
        payload = json.loads(valid)
        host = payload["host"]
        token = valid.replace(
            f'"host":"{host}"',
            f'"host":"{host}","host":"{host}"',
        )
    else:
        token = " " * 65537

    assert main(_predecessor_clear_args(tmp_path, token, reconcile=True)) == 1
    error = json.loads(capsys.readouterr().out)
    assert "error" in error
    assert not _lock(tmp_path).path.exists()


def test_predecessor_clear_exits_nonzero_for_uncertain_outcome(tmp_path, capsys):
    token = _inspection_token(tmp_path, capsys)

    assert main(_predecessor_clear_args(tmp_path, token)) == 1
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["status"] == "UNCERTAIN"
    assert outcome["acknowledged"] is False


def test_predecessor_clear_requires_explicit_absolute_receipt_path(tmp_path):
    args = [
        "predecessor-clear",
        *_base_args(tmp_path),
        "--expected-token",
        "{}",
        "--reason",
        "reason",
        "--operator",
        "operator",
    ]
    with pytest.raises(SystemExit):
        parse_args(args)
    with pytest.raises(SystemExit):
        parse_args([*args, "--storage-qualification", "relative-receipt.json"])
    assert not (tmp_path / "locks").exists()


@pytest.mark.parametrize(
    "invalid_receipt",
    ["permissions", "duplicate", "digest", "symlink", "root-device", "filesystem-device", "profile"],
)
def test_status_rejects_invalid_storage_qualification_receipts(
    tmp_path, capsys, invalid_receipt,
):
    path = _test_qualification_path(tmp_path)
    if invalid_receipt == "permissions":
        path.chmod(0o620)
    elif invalid_receipt == "duplicate":
        raw = path.read_text(encoding="utf-8")
        raw = raw.replace(
            '"artifact_id":"minecraft-target-storage-qualification"',
            '"artifact_id":"minecraft-target-storage-qualification",'
            '"artifact_id":"minecraft-target-storage-qualification"',
            1,
        )
        path.write_text(raw, encoding="utf-8")
    elif invalid_receipt == "digest":
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["storage_profile_id"] = "tampered-after-sealing"
        path.write_text(_canonical_json(payload), encoding="utf-8")
    elif invalid_receipt == "symlink":
        link = tmp_path / "receipt-link.json"
        link.symlink_to(path)
        path = link
    elif invalid_receipt in {"root-device", "filesystem-device"}:
        payload = json.loads(path.read_text(encoding="utf-8"))
        field = (
            "qualified_root_dev"
            if invalid_receipt == "root-device"
            else "qualified_filesystem_device"
        )
        payload[field] += 1
        path.write_text(_sealed_qualification(payload), encoding="utf-8")
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["storage_profile_id"] = ""
        path.write_text(_sealed_qualification(payload), encoding="utf-8")

    assert main(_qualified_status_args(tmp_path, receipt_path=path)) == 1
    error = json.loads(capsys.readouterr().out)
    assert "error" in error
    assert not _lock(tmp_path).path.exists()


def _qualified_status_args(tmp_path, *, receipt_path=None):
    if receipt_path is None:
        receipt_path = _test_qualification_path(tmp_path)
    return [
        "status",
        *_base_args(tmp_path),
        "--storage-qualification",
        str(receipt_path),
    ]


def _test_qualification_path(
    tmp_path,
    *,
    filename="storage-qualification.json",
    profile_id="test-only",
    receipt_id="receipt-test-only",
    boot_id=None,
):
    root = tmp_path / "locks"
    root.mkdir(parents=True, exist_ok=True)
    root_stat = root.stat()
    payload = {
        "artifact_id": "minecraft-target-storage-qualification",
        "artifact_version": 1,
        "storage_profile_id": profile_id,
        "storage_profile_version": 1,
        "receipt_id": receipt_id,
        "boot_id": (
            boot_id
            if boot_id is not None
            else Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        ),
        "qualified_root_absolute_path": str(root.resolve()),
        "qualified_root_dev": root_stat.st_dev,
        "qualified_root_ino": root_stat.st_ino,
        "qualified_filesystem_device": root_stat.st_dev,
        "capabilities": [
            "single_host_local_persistent_storage",
            "advisory_flock_on_stable_inode",
            "regular_file_fsync",
            "directory_fsync",
            "same_directory_atomic_replace",
            "stable_path_inode_observation",
        ],
        "issuer_audit_id": "test-only-issuer-audit",
    }
    path = tmp_path / filename
    path.write_text(_sealed_qualification(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


def _sealed_qualification(payload):
    detached = {
        key: value
        for key, value in payload.items()
        if key != "detached_artifact_sha256"
    }
    sealed = {
        **detached,
        "detached_artifact_sha256": hashlib.sha256(
            _canonical_json(detached).encode("utf-8")
        ).hexdigest(),
    }
    return _canonical_json(sealed)


def _inspection_token(tmp_path, capsys):
    assert main(_qualified_status_args(tmp_path)) == 0
    return json.loads(capsys.readouterr().out)["inspection_token"]


def _predecessor_clear_args(tmp_path, token, *, reconcile=False):
    args = [
        "predecessor-clear",
        *_base_args(tmp_path),
        "--storage-qualification",
        str(_test_qualification_path(tmp_path)),
        "--expected-token",
        token,
        "--reason",
        "Target safety independently verified",
        "--operator",
        "operator-test",
        "--acknowledge-target-safe",
        "--acknowledge-whole-prefix",
    ]
    if reconcile:
        args.append("--reconcile-unknown-history")
    return args


def _canonical_json(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def _crash_close_without_release(lock):
    """Drop the retained flock without publishing a release, as process death would."""
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


def _v3_clean_pair(tmp_path, *, attempt_id):
    """Create a test-only qualified CLEAN pair; this is not storage certification."""
    lock = _lock(tmp_path, attempt_id=attempt_id).acquire()
    _crash_close_without_release(lock)
    qualification_path = _test_qualification_path(
        tmp_path, filename=f"{attempt_id}-storage-qualification.json",
    )
    qualification = run_lock.load_minecraft_target_storage_qualification(
        qualification_path
    )
    inspected = run_lock.read_minecraft_target_predecessor_status(
        lock_root=lock.lock_root,
        host=lock.host,
        port=lock.port,
        storage_qualification=qualification,
    )
    assert inspected.status is run_lock.MinecraftTargetPredecessorHistoryStatus.UNRESOLVED
    outcome = run_lock.acknowledge_minecraft_target_predecessor(
        lock_root=lock.lock_root,
        host=lock.host,
        port=lock.port,
        expected=inspected.token,
        acknowledge_target_safe=True,
        acknowledge_whole_prefix=True,
        reason="test-only positive-control acknowledgement",
        operator="operator-v3-c1-test",
        storage_qualification=qualification,
    )
    assert outcome.acknowledged is True
    clean = run_lock.read_minecraft_target_predecessor_status(
        lock_root=lock.lock_root,
        host=lock.host,
        port=lock.port,
        storage_qualification=qualification,
    )
    assert clean.status is run_lock.MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN
    return lock, qualification


def _v3_stat_with_device(result, device):
    return SimpleNamespace(
        st_dev=device,
        st_ino=result.st_ino,
        st_mode=result.st_mode,
    )


def _v3_inject_sidecar_device_mismatch(monkeypatch, lock, suffix, mismatch):
    sidecar = lock.path.with_suffix(f".{suffix}")
    real_stat = run_lock.os.stat
    real_fstat = run_lock.os.fstat
    actual = real_stat(sidecar, follow_symlinks=False)
    entry_hits = []
    dirfd_entry_hits = []
    fd_hits = []

    def foreign_entry_device(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if mismatch == "entry_device":
            if kwargs.get("dir_fd") is None and Path(path) == sidecar:
                entry_hits.append(result.st_dev)
                return _v3_stat_with_device(result, result.st_dev + 1)
        else:
            if kwargs.get("dir_fd") is not None and path == sidecar.name:
                dirfd_entry_hits.append(result.st_dev)
                return _v3_stat_with_device(result, result.st_dev + 1)
            if kwargs.get("dir_fd") is None and Path(path) == sidecar:
                entry_hits.append(result.st_dev)
                return _v3_stat_with_device(result, result.st_dev + 1)
        return result

    def foreign_opened_fd_device(fd):
        result = real_fstat(fd)
        if (mismatch == "opened_fd_device"
                and (result.st_dev, result.st_ino) == (actual.st_dev, actual.st_ino)):
            fd_hits.append(result.st_dev)
            return _v3_stat_with_device(result, result.st_dev + 1)
        return result

    monkeypatch.setattr(run_lock.os, "stat", foreign_entry_device)
    monkeypatch.setattr(run_lock.os, "fstat", foreign_opened_fd_device)
    return sidecar, entry_hits, dirfd_entry_hits, fd_hits


def _v3_qualified_sidecar_observation(lock, qualification):
    return run_lock.read_minecraft_target_predecessor_status(
        lock_root=lock.lock_root,
        host=lock.host,
        port=lock.port,
        storage_qualification=qualification,
    )


def test_v3_c1_2_malformed_raw_json_receipt_is_rejected_before_observation(
    tmp_path, capsys, monkeypatch,
):
    # V3-C1-2 barrier: corrupt raw receipt JSON must fail in receipt parsing,
    # before lock/history inspection can use it as a qualification.
    receipt_path = _test_qualification_path(tmp_path)
    receipt_path.write_bytes(b"{")
    real_strict_json = run_lock._strict_json
    malformed_raw_hits = []

    def observe_malformed_raw(raw, *args, **kwargs):
        if raw == b"{":
            malformed_raw_hits.append(raw)
        return real_strict_json(raw, *args, **kwargs)

    monkeypatch.setattr(run_lock, "_strict_json", observe_malformed_raw)

    assert main(_qualified_status_args(tmp_path, receipt_path=receipt_path)) == 1
    error = json.loads(capsys.readouterr().out)
    assert error["error_type"] == "MinecraftTargetLockMetadataError"
    assert "invalid JSON record" in error["error"]
    assert malformed_raw_hits == [b"{"]
    assert not _lock(tmp_path).path.exists()


def test_v3_c1_11_qualified_lock_entry_and_opened_fd_device_mismatch_never_clean(
    tmp_path, monkeypatch,
):
    # V3-C1-11 barrier: preserve the positive control, then give the lock path
    # entry and its matching opened descriptor a foreign device identity. The
    # qualification-specific lock-device check must reject that coherent view.
    lock, qualification = _v3_clean_pair(
        tmp_path, attempt_id="attempt-v3-c1-11-lock-device",
    )
    metadata_before = lock.path.read_bytes()
    history_path = lock.path.with_suffix(".history")
    history_before = history_path.read_bytes()
    real_lstat = run_lock.os.lstat
    real_fstat = run_lock.os.fstat
    actual_lock = real_lstat(lock.path)
    entry_hits = []
    fd_hits = []
    validation_hits = []
    real_validate = run_lock._validate_storage_qualification

    def foreign_lock_entry(path, *args, **kwargs):
        result = real_lstat(path, *args, **kwargs)
        if Path(path) == lock.path:
            entry_hits.append(result.st_dev)
            return _v3_stat_with_device(result, result.st_dev + 1)
        return result

    def foreign_lock_fd(fd):
        result = real_fstat(fd)
        if (result.st_dev, result.st_ino) == (actual_lock.st_dev, actual_lock.st_ino):
            fd_hits.append(result.st_dev)
            return _v3_stat_with_device(result, result.st_dev + 1)
        return result

    def observe_storage_validation(receipt, root, io=None):
        validation_hits.append(None if io is None else io.lock_identity)
        return real_validate(receipt, root, io)

    monkeypatch.setattr(run_lock.os, "lstat", foreign_lock_entry)
    monkeypatch.setattr(run_lock.os, "fstat", foreign_lock_fd)
    monkeypatch.setattr(
        run_lock, "_validate_storage_qualification", observe_storage_validation,
    )

    observed = _v3_qualified_sidecar_observation(lock, qualification)

    assert entry_hits
    assert fd_hits
    assert validation_hits == [(actual_lock.st_dev + 1, actual_lock.st_ino)]
    assert observed.status is not run_lock.MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN
    assert observed.error == "storage qualification unavailable: qualified lock device mismatch"
    assert lock.path.read_bytes() == metadata_before
    assert history_path.read_bytes() == history_before
    assert not lock.path.with_suffix(".history-clear-pending").exists()
    assert not lock.path.with_suffix(".uncertain").exists()


@pytest.mark.parametrize("mismatch", ["entry_device", "opened_fd_device"])
def test_v3_c1_13_pending_entry_or_opened_fd_device_mismatch_never_clean(
    tmp_path, monkeypatch, mismatch,
):
    # V3-C1-13 barrier: start from a qualified CLEAN pair, persist a pending
    # transaction blocker, and inject a device mismatch specifically while the
    # qualified pending-sidecar census runs.
    lock, qualification = _v3_clean_pair(
        tmp_path, attempt_id=f"attempt-v3-c1-13-pending-{mismatch}",
    )
    metadata_before = lock.path.read_bytes()
    history_path = lock.path.with_suffix(".history")
    history_before = history_path.read_bytes()
    pending_path = lock.path.with_suffix(".history-clear-pending")
    pending_before = b'{"test_only_pending_blocker":true}\n'
    pending_path.write_bytes(pending_before)
    pre_fault = _v3_qualified_sidecar_observation(lock, qualification)
    assert pre_fault.status is run_lock.MinecraftTargetPredecessorHistoryStatus.DURABILITY_UNCERTAIN

    _, entry_hits, dirfd_entry_hits, fd_hits = _v3_inject_sidecar_device_mismatch(
        monkeypatch, lock, "history-clear-pending", mismatch,
    )
    validation_hits = []
    real_validate = run_lock._validate_storage_qualification

    def observe_storage_validation(receipt, root, io=None):
        validation_hits.append(None if io is None else io.lock_identity)
        return real_validate(receipt, root, io)

    monkeypatch.setattr(
        run_lock, "_validate_storage_qualification", observe_storage_validation,
    )
    observed = _v3_qualified_sidecar_observation(lock, qualification)

    assert len(validation_hits) == 1
    assert validation_hits[0] is not None
    assert entry_hits
    if mismatch == "opened_fd_device":
        assert dirfd_entry_hits
        assert fd_hits
    else:
        assert not dirfd_entry_hits
        assert not fd_hits
    assert observed.status is not run_lock.MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN
    assert observed.error == "storage qualification unavailable: qualified sidecar device mismatch"
    assert lock.path.read_bytes() == metadata_before
    assert history_path.read_bytes() == history_before
    assert pending_path.read_bytes() == pending_before
    assert not lock.path.with_suffix(".uncertain").exists()


@pytest.mark.parametrize("mismatch", ["entry_device", "opened_fd_device"])
def test_v3_c1_14_uncertain_entry_or_opened_fd_device_mismatch_never_clean(
    tmp_path, monkeypatch, mismatch,
):
    # V3-C1-14 barrier: a durable uncertainty blocker survives the injected
    # qualified-sidecar device mismatch and can never be observed as CLEAN.
    lock, qualification = _v3_clean_pair(
        tmp_path, attempt_id=f"attempt-v3-c1-14-uncertain-{mismatch}",
    )
    metadata_before = lock.path.read_bytes()
    history_path = lock.path.with_suffix(".history")
    history_before = history_path.read_bytes()
    marker_path = lock.path.with_suffix(".uncertain")
    marker_before = (
        b'{"schema_version":1,"status":"uncertain",'
        b'"attempt_id":"test-only-independent-blocker"}\n'
    )
    marker_path.write_bytes(marker_before)
    pre_fault = _v3_qualified_sidecar_observation(lock, qualification)
    assert pre_fault.status is run_lock.MinecraftTargetPredecessorHistoryStatus.DURABILITY_UNCERTAIN

    _, entry_hits, dirfd_entry_hits, fd_hits = _v3_inject_sidecar_device_mismatch(
        monkeypatch, lock, "uncertain", mismatch,
    )
    validation_hits = []
    real_validate = run_lock._validate_storage_qualification

    def observe_storage_validation(receipt, root, io=None):
        validation_hits.append(None if io is None else io.lock_identity)
        return real_validate(receipt, root, io)

    monkeypatch.setattr(
        run_lock, "_validate_storage_qualification", observe_storage_validation,
    )
    observed = _v3_qualified_sidecar_observation(lock, qualification)

    assert len(validation_hits) == 1
    assert validation_hits[0] is not None
    assert entry_hits
    if mismatch == "opened_fd_device":
        assert dirfd_entry_hits
        assert fd_hits
    else:
        assert not dirfd_entry_hits
        assert not fd_hits
    assert observed.status is not run_lock.MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN
    assert observed.error == "storage qualification unavailable: qualified sidecar device mismatch"
    assert lock.path.read_bytes() == metadata_before
    assert history_path.read_bytes() == history_before
    assert marker_path.read_bytes() == marker_before
    assert not lock.path.with_suffix(".history-clear-pending").exists()


@pytest.mark.parametrize(
    ("transaction_sidecar", "fault_create_number"),
    [("history-clear-pending", 1), ("history", 2)],
)
def test_v3_c1_15_transaction_temporary_opened_fd_device_mismatch_precedes_replace(
    tmp_path, monkeypatch, transaction_sidecar, fault_create_number,
):
    # V3-C1-15 barrier: prove receipt/clean as a positive control, then inject
    # only the chosen newly-created transaction temp's opened-FD device mismatch.
    lock, qualification = _v3_clean_pair(
        tmp_path,
        attempt_id=f"attempt-v3-c1-15-{transaction_sidecar}",
    )
    metadata_before = lock.path.read_bytes()
    history_path = lock.path.with_suffix(".history")
    history_before = history_path.read_bytes()
    pending_path = lock.path.with_suffix(".history-clear-pending")
    assert not pending_path.exists()
    inspected = _v3_qualified_sidecar_observation(lock, qualification)
    assert inspected.status is run_lock.MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN

    real_open = run_lock.os.open
    real_fstat = run_lock.os.fstat
    real_close = run_lock.os.close
    real_replace = run_lock.os.replace
    created_temp_names = []
    tracked_temp_fds = {}
    temp_fd_device_hits = []
    replace_sources = []
    temp_prefix = f".{lock.key}.{transaction_sidecar}.tmp-"
    injected = []

    def track_created_transaction_temp(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if flags & getattr(run_lock.os, "O_CREAT", 0):
            name = Path(path).name
            if name.startswith(f".{lock.key}.history-clear-pending.tmp-") or name.startswith(
                f".{lock.key}.history.tmp-"
            ):
                created_temp_names.append(name)
                tracked_temp_fds[fd] = name
        return fd

    def foreign_transaction_temp_fd(fd):
        result = real_fstat(fd)
        name = tracked_temp_fds.get(fd)
        if (name is not None and name.startswith(temp_prefix)
                and len(created_temp_names) == fault_create_number and not injected):
            injected.append(name)
            temp_fd_device_hits.append(name)
            return _v3_stat_with_device(result, result.st_dev + 1)
        return result

    def close_tracked_temp(fd):
        try:
            return real_close(fd)
        finally:
            tracked_temp_fds.pop(fd, None)

    def observe_replace(source, destination, *args, **kwargs):
        replace_sources.append(Path(source).name)
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(run_lock.os, "open", track_created_transaction_temp)
    monkeypatch.setattr(run_lock.os, "fstat", foreign_transaction_temp_fd)
    monkeypatch.setattr(run_lock.os, "close", close_tracked_temp)
    monkeypatch.setattr(run_lock.os, "replace", observe_replace)

    outcome = run_lock.acknowledge_minecraft_target_predecessor(
        lock_root=lock.lock_root,
        host=lock.host,
        port=lock.port,
        expected=inspected.token,
        acknowledge_target_safe=True,
        acknowledge_whole_prefix=True,
        reason="test-only transaction temp device barrier",
        operator="operator-v3-c1-test",
        storage_qualification=qualification,
    )

    assert len(created_temp_names) >= fault_create_number
    failed_temp_name = created_temp_names[fault_create_number - 1]
    assert injected == [failed_temp_name]
    assert temp_fd_device_hits == [failed_temp_name]
    assert failed_temp_name.startswith(temp_prefix)
    assert failed_temp_name not in replace_sources
    assert outcome.status is run_lock.MinecraftTargetPredecessorAcknowledgementStatus.UNCERTAIN
    assert outcome.acknowledged is False
    assert "temporary sidecar device mismatch" in outcome.error
    assert lock.path.read_bytes() == metadata_before
    assert history_path.read_bytes() == history_before
    assert pending_path.exists()
    pending_record = json.loads(pending_path.read_text(encoding="utf-8"))
    assert pending_record["schema"] == "minecraft-target-predecessor-clear/1"
    assert not lock.path.with_suffix(".uncertain").exists()

    after = _v3_qualified_sidecar_observation(lock, qualification)
    assert after.status is not run_lock.MinecraftTargetPredecessorHistoryStatus.ACKNOWLEDGED_CLEAN
