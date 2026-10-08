from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import queue
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

import benchmarks.minecraft.k12_sample_claim_registry as registry_module
from benchmarks.minecraft.k12_sample_claim_registry import (
    CLAIM_ARTIFACT,
    SampleClaimRegistry,
    SampleExecutionBinding,
)
from benchmarks.minecraft.k12_sample_identity import (
    FINAL_COORDINATES,
    FIXTURE_DIGEST,
    K12SamplePlan,
    PROBE_SCHEDULE_DIGEST,
    PROBE_SCHEDULE_IDENTITY,
    PHASE_FINAL_CELL,
    PHASE_QUALIFICATION_CELL,
    PHASE_QUALIFICATION_PROBE,
    PROTOCOL_IDENTITY,
    PROBE_COORDINATES,
    QUALIFICATION_COORDINATES,
    QUALIFICATION_SCHEDULE_DIGEST,
    QUALIFICATION_SCHEDULE_IDENTITY,
    RANDOMIZATION_DIGEST,
    SAMPLE_PLAN_ARTIFACT,
    canonical_json_bytes,
)


def _plan() -> K12SamplePlan:
    payload = {
        "artifact": SAMPLE_PLAN_ARTIFACT,
        "study_instance_identity": "registry-tests",
        "replicate_designation": "local-only",
        "protocol_identity": PROTOCOL_IDENTITY,
        "qualification": {
            "schedule_identity": QUALIFICATION_SCHEDULE_IDENTITY,
            "schedule_digest": QUALIFICATION_SCHEDULE_DIGEST,
            "ordered_coordinates": list(QUALIFICATION_COORDINATES),
        },
        "probe": {
            "schedule_identity": PROBE_SCHEDULE_IDENTITY,
            "schedule_digest": PROBE_SCHEDULE_DIGEST,
            "ordered_coordinates": list(PROBE_COORDINATES),
        },
        "final": {
            "randomization_digest": RANDOMIZATION_DIGEST,
            "fixture_digest": FIXTURE_DIGEST,
            "ordered_coordinates": [dict(item) for item in FINAL_COORDINATES],
        },
        "retry": "forbidden",
        "resume": "forbidden",
        "replacement": "forbidden",
    }
    payload["sample_plan_digest"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return K12SamplePlan.from_mapping(payload)


def _bindings(plan: K12SamplePlan, phase: str) -> tuple[SampleExecutionBinding, ...]:
    return tuple(
        SampleExecutionBinding(
            sample_id=identity.sample_id,
            reservation_id=f"reservation-{index}",
            output_root_identity=f"output-root-{index}",
            nonce=f"nonce-{index}",
            target_identity=f"target-{index}",
            ledger_namespace=f"ledger-{index}",
        )
        for index, identity in enumerate(plan.identities(phase))
    )


def _registry_root(tmp_path: Path, name: str = "claims") -> Path:
    root = tmp_path / name
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


def _claim_in_child(root: str, plan: K12SamplePlan, phase: str, gate: object,
                    result_queue: object, output_binding: str | None = None) -> None:
    gate.wait()
    try:
        with SampleClaimRegistry(root) as registry:
            bindings = _bindings(plan, phase)
            if output_binding is not None:
                bindings = (replace(bindings[0], output_root_identity=output_binding),
                            *bindings[1:])
            audit = registry.claim_phase(plan=plan, phase=phase,
                                         bindings=bindings)
        result_queue.put(("claimed", audit.claim_batch_digest))
    except Exception as exc:
        result_queue.put((type(exc).__name__, str(exc)))


def _inspect_in_child(root: str, result_queue: object) -> None:
    with SampleClaimRegistry(root) as registry:
        receipts = registry.inspect_claims()
        result_queue.put((len(receipts), receipts[0].to_dict(),
                          tuple(name for name in ("unclaim", "retry", "resume",
                                                  "replace", "reopen_for_execution",
                                                  "mint_authority")
                                if hasattr(registry, name))))


def _use_inherited_registry_in_child(registry: SampleClaimRegistry,
                                     plan: K12SamplePlan, result_queue: object) -> None:
    try:
        registry.claim_phase(plan=plan, phase=PHASE_QUALIFICATION_CELL,
                             bindings=_bindings(plan, PHASE_QUALIFICATION_CELL))
    except Exception as exc:
        result_queue.put((type(exc).__name__, str(exc)))
    else:
        result_queue.put(("incorrectly claimed", ""))


def _reopen_after_fork_while_parent_guard_held(
    inherited: SampleClaimRegistry, root: str, result_queue: object,
) -> None:
    try:
        result_queue.put(("inherited_closed", inherited._closed,
                          inherited._root_fd, inherited._lock_fd))
        inherited.close()
        with SampleClaimRegistry(root) as fresh:
            records = fresh.inspect_claims()
        result_queue.put(("audit_only", len(records)))
    except BaseException as exc:
        result_queue.put((type(exc).__name__, str(exc)))


@pytest.fixture
def plan() -> K12SamplePlan:
    return _plan()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return _registry_root(tmp_path)


def test_phase_claims_persist_complete_ordered_batches_and_expected_counts(
    root: Path, plan: K12SamplePlan,
) -> None:
    expected = {
        PHASE_QUALIFICATION_CELL: 15,
        PHASE_QUALIFICATION_PROBE: 4,
        PHASE_FINAL_CELL: 90,
    }
    with SampleClaimRegistry(root) as registry:
        audits = []
        for phase, count in expected.items():
            identities = plan.identities(phase)
            audit = registry.claim_phase(
                plan=plan, phase=phase, bindings=_bindings(plan, phase),
            )
            audits.append(audit)
            assert audit.artifact == CLAIM_ARTIFACT
            assert audit.phase == phase
            assert len(audit.ordered_sample_identities) == count
            assert audit.sample_ids == tuple(identity.sample_id for identity in identities)
            assert tuple(binding.sample_id for binding in audit.ordered_execution_bindings) == audit.sample_ids
            persisted = audit.to_dict()
            assert set(persisted) == {
                "artifact", "registry_root_identity", "sample_plan_digest", "phase",
                "ordered_sample_identities", "ordered_execution_bindings",
                "claim_batch_digest", "created_by_pid", "created_at_ns",
            }
            assert persisted["ordered_sample_identities"] == [
                identity.to_dict() for identity in identities
            ]
            digest_body = {key: value for key, value in persisted.items()
                           if key != "claim_batch_digest"}
            assert hashlib.sha256(canonical_json_bytes(digest_body)).hexdigest() == audit.claim_batch_digest
        found = registry.inspect_claims()
    assert tuple(record.claim_batch_digest for record in found) == tuple(
        sorted(record.claim_batch_digest for record in audits)
    )
    assert {record.phase for record in found} == set(expected)


def test_claim_is_audit_only_and_has_no_unconsume_or_authority_api(
    root: Path, plan: K12SamplePlan,
) -> None:
    with SampleClaimRegistry(root) as registry:
        audit = registry.claim_phase(
            plan=plan, phase=PHASE_QUALIFICATION_CELL,
            bindings=_bindings(plan, PHASE_QUALIFICATION_CELL),
        )
        forbidden = (
            "delete", "delete_claim", "unclaim", "unclaim_sample", "retry",
            "retry_claim", "reset", "reset_claim", "resume", "resume_claim",
            "replace", "replace_claim", "reopen_for_execution", "restore_authority",
            "mint_authority", "mint_execution_authority",
        )
        assert not any(hasattr(registry, name) for name in forbidden)
        assert not any(hasattr(audit, name) for name in forbidden)
        assert not any(hasattr(registry_module, name) for name in forbidden)
        detached = audit.to_dict()
        detached["registry_root_identity"]["path"] = "/forged"
        assert audit.registry_root_identity["path"] == str(root)


@pytest.mark.parametrize("field", [
    "sample_id", "reservation_id", "output_root_identity", "nonce",
    "target_identity", "ledger_namespace",
])
def test_binding_fields_require_strict_nonempty_text(field: str) -> None:
    values = {
        "sample_id": "sample",
        "reservation_id": "reservation",
        "output_root_identity": "output",
        "nonce": "nonce",
        "target_identity": "target",
        "ledger_namespace": "ledger",
    }
    for invalid in ("", "  ", None, 1):
        invalid_values = dict(values)
        invalid_values[field] = invalid
        with pytest.raises(ValueError):
            SampleExecutionBinding(**invalid_values)


def test_binding_is_frozen() -> None:
    binding = SampleExecutionBinding("sample", "reservation", "output", "nonce",
                                     "target", "ledger")
    with pytest.raises((AttributeError, TypeError)):
        binding.nonce = "changed"  # type: ignore[misc]


def test_rejects_non_plan_and_unknown_phase(root: Path, plan: K12SamplePlan) -> None:
    with SampleClaimRegistry(root) as registry:
        with pytest.raises(TypeError):
            registry.claim_phase(plan=object(), phase=PHASE_QUALIFICATION_CELL,
                                 bindings=[])  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            registry.claim_phase(plan=plan, phase="unknown", bindings=[])


@pytest.mark.parametrize("make_bindings", [
    lambda plan, phase, bindings: bindings[:-1],
    lambda plan, phase, bindings: bindings + bindings[:1],
    lambda plan, phase, bindings: tuple(reversed(bindings)),
    lambda plan, phase, bindings: bindings[:1] + bindings[:1] + bindings[2:],
])
def test_rejects_partial_extra_reordered_and_duplicate_bindings(
    root: Path, plan: K12SamplePlan, make_bindings,
) -> None:
    phase = PHASE_QUALIFICATION_CELL
    bindings = _bindings(plan, phase)
    with SampleClaimRegistry(root) as registry:
        with pytest.raises(ValueError):
            registry.claim_phase(plan=plan, phase=phase,
                                 bindings=make_bindings(plan, phase, bindings))
    assert not list(root.glob("claim-*.json"))


def test_rejects_non_sequence_and_wrong_binding_type(root: Path, plan: K12SamplePlan) -> None:
    phase = PHASE_QUALIFICATION_CELL
    with SampleClaimRegistry(root) as registry:
        with pytest.raises(TypeError):
            registry.claim_phase(plan=plan, phase=phase,
                                 bindings="not-a-sequence")  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            registry.claim_phase(plan=plan, phase=phase,
                                 bindings=[object()] * len(plan.identities(phase)))  # type: ignore[list-item]


def test_second_call_same_mapping_fails_as_already_claimed(root: Path, plan: K12SamplePlan) -> None:
    phase = PHASE_QUALIFICATION_CELL
    bindings = _bindings(plan, phase)
    with SampleClaimRegistry(root) as registry:
        first = registry.claim_phase(plan=plan, phase=phase, bindings=bindings)
        with pytest.raises(ValueError, match="ALREADY CLAIMED"):
            registry.claim_phase(plan=plan, phase=phase, bindings=bindings)
        assert registry.inspect_claims() == (first,)


@pytest.mark.parametrize("field", [
    "reservation_id", "output_root_identity", "nonce", "target_identity",
    "ledger_namespace",
])
def test_changed_bindings_do_not_make_consumed_ids_claimable(
    root: Path, plan: K12SamplePlan, field: str,
) -> None:
    phase = PHASE_QUALIFICATION_CELL
    bindings = _bindings(plan, phase)
    changed = (replace(bindings[0], **{field: f"different-{field}"}), *bindings[1:])
    with SampleClaimRegistry(root) as registry:
        registry.claim_phase(plan=plan, phase=phase, bindings=bindings)
        with pytest.raises(ValueError, match="ALREADY CLAIMED"):
            registry.claim_phase(plan=plan, phase=phase, bindings=changed)


def test_trusted_parent_root_is_not_derived_from_a_new_output_root(
    root: Path, tmp_path: Path, plan: K12SamplePlan,
) -> None:
    different_output_root = _registry_root(tmp_path, "new-output-root")
    phase = PHASE_QUALIFICATION_CELL
    bindings = _bindings(plan, phase)
    changed = (replace(bindings[0], output_root_identity=str(different_output_root)),
               *bindings[1:])
    with SampleClaimRegistry(root) as parent_registry:
        parent_registry.claim_phase(plan=plan, phase=phase, bindings=bindings)
        with pytest.raises(ValueError, match="ALREADY CLAIMED"):
            parent_registry.claim_phase(plan=plan, phase=phase, bindings=changed)
    assert not list(different_output_root.glob("claim-*.json"))


def test_registry_requires_existing_absolute_canonical_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SampleClaimRegistry("relative-registry")
    with pytest.raises((ValueError, FileNotFoundError)):
        SampleClaimRegistry(tmp_path / "missing")
    assert not any(path.name == "missing" for path in tmp_path.iterdir())


def test_rejects_root_and_ancestor_symlinks(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    root = _registry_root(real_parent)
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(root, target_is_directory=True)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ValueError):
        SampleClaimRegistry(linked_root)
    with pytest.raises(ValueError):
        SampleClaimRegistry(linked_parent / root.name)


def test_requires_private_owned_writable_root(tmp_path: Path) -> None:
    root = tmp_path / "open"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    with pytest.raises(PermissionError):
        SampleClaimRegistry(root)
    root.chmod(0o700)
    actual_uid = os.geteuid()
    with pytest.raises(PermissionError):
        SampleClaimRegistry(root, expected_uid=actual_uid + 1)


def test_rejects_writable_nonsticky_registry_ancestor(tmp_path: Path) -> None:
    parent = tmp_path / "untrusted-parent"
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)
    root = _registry_root(parent)
    with pytest.raises(PermissionError, match="ancestor"):
        SampleClaimRegistry(root)


@pytest.mark.parametrize("ancestor_mode", [0o755, 0o1777])
def test_rejects_ancestor_owned_by_another_principal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ancestor_mode: int,
) -> None:
    ancestor = tmp_path / "foreign-parent"
    ancestor.mkdir()
    ancestor.chmod(ancestor_mode)
    root = _registry_root(ancestor)
    original_fstat = registry_module.os.fstat
    observed = []

    def foreign_owned_ancestor(fd: int) -> os.stat_result:
        result = original_fstat(fd)
        if os.readlink(f"/proc/self/fd/{fd}") == str(ancestor):
            observed.append(fd)
            fields = list(result)
            fields[4] = os.geteuid() + 1
            return os.stat_result(fields)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(registry_module.os, "fstat", foreign_owned_ancestor)
        with pytest.raises(PermissionError, match="ancestor"):
            SampleClaimRegistry(root)
    assert observed
    assert not (root / registry_module._LOCK_NAME).exists()


def test_rejects_untrusted_filesystem_root_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _registry_root(tmp_path)
    original_fstat = registry_module.os.fstat
    observed = []

    def foreign_owned_filesystem_root(fd: int) -> os.stat_result:
        result = original_fstat(fd)
        if os.readlink(f"/proc/self/fd/{fd}") == "/":
            observed.append(fd)
            fields = list(result)
            fields[4] = os.geteuid() + 1
            return os.stat_result(fields)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(registry_module.os, "fstat", foreign_owned_filesystem_root)
        with pytest.raises(PermissionError, match="ancestor"):
            SampleClaimRegistry(root)
    assert observed
    assert not (root / registry_module._LOCK_NAME).exists()


def test_rejects_bad_existing_lock_file(tmp_path: Path) -> None:
    root = _registry_root(tmp_path)
    lock_path = root / registry_module._LOCK_NAME
    lock_path.write_text("not a lock", encoding="utf-8")
    lock_path.chmod(0o640)
    with pytest.raises(PermissionError):
        SampleClaimRegistry(root)


def test_lock_path_inode_replacement_fails_closed(root: Path) -> None:
    registry = SampleClaimRegistry(root)
    lock_path = root / registry_module._LOCK_NAME
    moved_lock = root / "old-lock-file"
    lock_path.rename(moved_lock)
    lock_path.write_bytes(b"")
    lock_path.chmod(0o600)
    try:
        with pytest.raises(OSError):
            registry.inspect_claims()
    finally:
        registry.close()


def test_root_path_inode_replacement_fails_closed(tmp_path: Path) -> None:
    root = _registry_root(tmp_path)
    registry = SampleClaimRegistry(root)
    moved_root = tmp_path / "moved-root"
    root.rename(moved_root)
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    try:
        with pytest.raises(OSError):
            registry.inspect_claims()
    finally:
        registry.close()


def test_stale_temporary_unknown_and_corrupt_files_fail_closed(
    tmp_path: Path, plan: K12SamplePlan,
) -> None:
    root = _registry_root(tmp_path)
    with SampleClaimRegistry(root) as registry:
        stale = root / ".claim-tmp-interrupted.partial"
        stale.write_bytes(b"partial")
        stale.chmod(0o600)
        with pytest.raises(ValueError, match="unknown, stale, or partial"):
            registry.inspect_claims()
    stale.unlink()
    with SampleClaimRegistry(root) as registry:
        registry.claim_phase(plan=plan, phase=PHASE_QUALIFICATION_CELL,
                             bindings=_bindings(plan, PHASE_QUALIFICATION_CELL))
    record_path = next(root.glob("claim-*.json"))
    record_path.write_bytes(b"{broken-json")
    record_path.chmod(0o600)
    with SampleClaimRegistry(root) as registry:
        with pytest.raises(ValueError):
            registry.inspect_claims()


def test_rehashed_off_domain_record_blocks_audit_and_new_claim(
    root: Path, plan: K12SamplePlan,
) -> None:
    phase = PHASE_QUALIFICATION_CELL
    with SampleClaimRegistry(root) as registry:
        audit = registry.claim_phase(plan=plan, phase=phase, bindings=_bindings(plan, phase))
    old_path = root / f"claim-{audit.claim_batch_digest}.json"
    record = json.loads(old_path.read_text(encoding="utf-8"))
    for identity, binding in zip(record["ordered_sample_identities"],
                                 record["ordered_execution_bindings"], strict=True):
        identity["schedule_identity"] = "forged-qualification-schedule/1"
        binding["sample_id"] = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
    body = {key: value for key, value in record.items() if key != "claim_batch_digest"}
    record["claim_batch_digest"] = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    old_path.rename(root / f"claim-{record['claim_batch_digest']}.json")
    new_path = root / f"claim-{record['claim_batch_digest']}.json"
    new_path.write_bytes(canonical_json_bytes(record))
    with SampleClaimRegistry(root) as registry:
        with pytest.raises(ValueError, match="plan and phase"):
            registry.inspect_claims()
        with pytest.raises(ValueError, match="plan and phase"):
            registry.claim_phase(plan=plan, phase=phase, bindings=_bindings(plan, phase))


def test_overlapping_rehashed_claim_files_block_registry(
    root: Path, plan: K12SamplePlan,
) -> None:
    phase = PHASE_QUALIFICATION_CELL
    with SampleClaimRegistry(root) as registry:
        audit = registry.claim_phase(plan=plan, phase=phase, bindings=_bindings(plan, phase))
    record = audit.to_dict()
    record["created_at_ns"] += 1
    body = {key: value for key, value in record.items() if key != "claim_batch_digest"}
    record["claim_batch_digest"] = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    second = root / f"claim-{record['claim_batch_digest']}.json"
    second.write_bytes(canonical_json_bytes(record))
    second.chmod(0o600)
    with SampleClaimRegistry(root) as registry:
        with pytest.raises(ValueError, match="overlapping"):
            registry.inspect_claims()
        with pytest.raises(ValueError, match="overlapping"):
            registry.claim_phase(plan=plan, phase=phase, bindings=_bindings(plan, phase))


def test_oversized_binding_is_rejected_before_creating_partial_state(
    root: Path, plan: K12SamplePlan,
) -> None:
    phase = PHASE_QUALIFICATION_CELL
    bindings = _bindings(plan, phase)
    oversized = (replace(bindings[0], reservation_id="x" * registry_module._MAX_CLAIM_BYTES),
                 *bindings[1:])
    with SampleClaimRegistry(root) as registry:
        with pytest.raises(ValueError, match="maximum durable record size"):
            registry.claim_phase(plan=plan, phase=phase, bindings=oversized)
        assert registry.inspect_claims() == ()
    assert not list(root.glob(".claim-tmp-*"))


def test_unknown_registry_entry_and_symlink_claim_are_rejected(
    root: Path, plan: K12SamplePlan,
) -> None:
    unknown = root / "surprise"
    unknown.write_text("unknown", encoding="utf-8")
    unknown.chmod(0o600)
    with SampleClaimRegistry(root) as registry:
        with pytest.raises(ValueError, match="unknown, stale, or partial"):
            registry.inspect_claims()
    unknown.unlink()
    with SampleClaimRegistry(root) as registry:
        audit = registry.claim_phase(plan=plan, phase=PHASE_QUALIFICATION_CELL,
                                     bindings=_bindings(plan, PHASE_QUALIFICATION_CELL))
    claim_path = root / f"claim-{audit.claim_batch_digest}.json"
    moved_claim = root / "saved-claim"
    claim_path.rename(moved_claim)
    claim_path.symlink_to(moved_claim.name)
    with SampleClaimRegistry(root) as registry:
        with pytest.raises((ValueError, OSError)):
            registry.inspect_claims()


@pytest.mark.parametrize("failure_point", [
    "temp-inode-fsync", "temp-dir-fsync", "write-short", "write-interrupted",
    "file-fsync", "publish-dir-fsync",
])
def test_interrupted_persistence_never_reports_success_and_leaves_marker(
    root: Path, plan: K12SamplePlan, monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    registry = SampleClaimRegistry(root)
    original_write = registry_module.os.write
    original_fsync = registry_module.os.fsync
    fsync_calls = 0

    def broken_write(fd: int, payload: bytes) -> int:
        if failure_point == "write-short":
            return max(0, len(payload) - 1)
        if failure_point == "write-interrupted":
            raise OSError("injected interrupted write")
        return original_write(fd, payload)

    def broken_fsync(fd: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if ((failure_point == "temp-inode-fsync" and fsync_calls == 1)
                or (failure_point == "temp-dir-fsync" and fsync_calls == 2)
                or (failure_point == "file-fsync" and fsync_calls == 3)
                or (failure_point == "publish-dir-fsync" and fsync_calls == 4)):
            raise OSError("injected interrupted fsync")
        original_fsync(fd)

    monkeypatch.setattr(registry_module.os, "write", broken_write)
    monkeypatch.setattr(registry_module.os, "fsync", broken_fsync)
    try:
        with pytest.raises(OSError):
            registry.claim_phase(plan=plan, phase=PHASE_QUALIFICATION_CELL,
                                 bindings=_bindings(plan, PHASE_QUALIFICATION_CELL))
    finally:
        registry.close()
    assert list(root.glob(".claim-tmp-*.partial"))
    with SampleClaimRegistry(root) as fresh:
        with pytest.raises(ValueError):
            fresh.inspect_claims()


def test_keyboard_interrupt_during_file_fsync_leaves_fail_closed_marker(
    root: Path, plan: K12SamplePlan, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = SampleClaimRegistry(root)
    original_fsync = registry_module.os.fsync
    calls = 0

    def interrupted_file_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise KeyboardInterrupt()
        original_fsync(fd)

    monkeypatch.setattr(registry_module.os, "fsync", interrupted_file_fsync)
    try:
        with pytest.raises(KeyboardInterrupt):
            registry.claim_phase(plan=plan, phase=PHASE_QUALIFICATION_CELL,
                                 bindings=_bindings(plan, PHASE_QUALIFICATION_CELL))
    finally:
        registry.close()
    assert list(root.glob(".claim-tmp-*.partial"))


def test_cleanup_directory_fsync_failure_is_not_success_but_claim_stays_consumed(
    root: Path, plan: K12SamplePlan, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = SampleClaimRegistry(root)
    original_fsync = registry_module.os.fsync
    calls = 0

    def fail_cleanup_dir(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 5:
            raise OSError("injected cleanup directory fsync failure")
        original_fsync(fd)

    monkeypatch.setattr(registry_module.os, "fsync", fail_cleanup_dir)
    phase = PHASE_QUALIFICATION_CELL
    try:
        with pytest.raises(OSError):
            registry.claim_phase(plan=plan, phase=phase, bindings=_bindings(plan, phase))
    finally:
        registry.close()
    assert calls == 5
    with SampleClaimRegistry(root) as fresh:
        assert len(fresh.inspect_claims()) == 1
        with pytest.raises(ValueError, match="ALREADY CLAIMED"):
            fresh.claim_phase(plan=plan, phase=phase, bindings=_bindings(plan, phase))


def test_claim_file_mode_and_digest_are_validated_on_inspection(
    root: Path, plan: K12SamplePlan,
) -> None:
    phase = PHASE_QUALIFICATION_CELL
    with SampleClaimRegistry(root) as registry:
        audit = registry.claim_phase(plan=plan, phase=phase, bindings=_bindings(plan, phase))
    path = root / f"claim-{audit.claim_batch_digest}.json"
    path.chmod(0o640)
    with SampleClaimRegistry(root) as registry:
        with pytest.raises(ValueError):
            registry.inspect_claims()
    path.chmod(0o600)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["created_at_ns"] += 1
    path.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True),
                    encoding="utf-8")
    path.chmod(0o600)
    with SampleClaimRegistry(root) as registry:
        with pytest.raises(ValueError):
            registry.inspect_claims()


def test_fresh_process_can_reopen_for_audit_only(root: Path, plan: K12SamplePlan) -> None:
    phase = PHASE_QUALIFICATION_CELL
    with SampleClaimRegistry(root) as registry:
        original = registry.claim_phase(plan=plan, phase=phase, bindings=_bindings(plan, phase))
    context = multiprocessing.get_context("fork")
    result_queue = context.Queue()
    process = context.Process(target=_inspect_in_child, args=(str(root), result_queue))
    process.start()
    process.join(timeout=10)
    if process.is_alive():
        process.terminate()
        process.join()
        pytest.fail("audit-only subprocess did not finish")
    assert process.exitcode == 0
    count, record, authority_methods = result_queue.get(timeout=2)
    assert count == 1
    assert record["claim_batch_digest"] == original.claim_batch_digest
    assert record["artifact"] == CLAIM_ARTIFACT
    assert authority_methods == ()


def test_cold_start_subprocess_inspects_claim_without_execution_authority(
    root: Path, plan: K12SamplePlan,
) -> None:
    phase = PHASE_QUALIFICATION_CELL
    with SampleClaimRegistry(root) as registry:
        original = registry.claim_phase(plan=plan, phase=phase, bindings=_bindings(plan, phase))
    script = """
import json, sys
from benchmarks.minecraft.k12_sample_claim_registry import SampleClaimRegistry
with SampleClaimRegistry(sys.argv[1]) as registry:
    records = registry.inspect_claims()
    print(json.dumps({"count": len(records), "digest": records[0].claim_batch_digest,
                      "authority": any(hasattr(registry, name) for name in
                      ("restore_authority", "mint_execution_authority", "resume_claim"))}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(root)],
        cwd=Path(__file__).resolve().parents[1], text=True,
        capture_output=True, check=True, timeout=10,
    )
    assert json.loads(completed.stdout) == {
        "count": 1, "digest": original.claim_batch_digest, "authority": False,
    }
    with SampleClaimRegistry(root) as registry:
        with pytest.raises(ValueError, match="ALREADY CLAIMED"):
            registry.claim_phase(plan=plan, phase=phase, bindings=_bindings(plan, phase))


def test_registry_inherited_across_fork_cannot_claim(root: Path, plan: K12SamplePlan) -> None:
    try:
        context = multiprocessing.get_context("fork")
    except ValueError:
        pytest.skip("fork is unavailable")
    registry = SampleClaimRegistry(root)
    result_queue = context.Queue()
    child = context.Process(target=_use_inherited_registry_in_child,
                            args=(registry, plan, result_queue))
    child.start()
    child.join(timeout=10)
    if child.is_alive():
        child.terminate()
        child.join()
        pytest.fail("inherited-registry child did not finish")
    try:
        assert child.exitcode == 0
        result_type, message = result_queue.get(timeout=2)
        assert result_type == "RuntimeError"
        assert "after fork" in message
        registry.claim_phase(plan=plan, phase=PHASE_QUALIFICATION_CELL,
                             bindings=_bindings(plan, PHASE_QUALIFICATION_CELL))
    finally:
        registry.close()


def test_child_reopens_registry_while_parent_thread_holds_local_guard(
    root: Path, plan: K12SamplePlan,
) -> None:
    try:
        context = multiprocessing.get_context("fork")
    except ValueError:
        pytest.skip("fork is unavailable")
    registry = SampleClaimRegistry(root)
    held = threading.Event()
    release = threading.Event()

    def hold_local_guard() -> None:
        with registry._thread_lock:
            held.set()
            release.wait(timeout=8)

    thread = threading.Thread(target=hold_local_guard)
    thread.start()
    process = None
    try:
        assert held.wait(timeout=2)
        result_queue = context.Queue()
        process = context.Process(target=_reopen_after_fork_while_parent_guard_held,
                                  args=(registry, str(root), result_queue))
        process.start()
        process.join(timeout=4)
        if process.is_alive():
            process.terminate()
            process.join()
            pytest.fail("forked child hung on inherited registry-local RLock")
        assert process.exitcode == 0
        assert result_queue.get(timeout=2) == ("inherited_closed", True, -1, -1)
        assert result_queue.get(timeout=2) == ("audit_only", 0)
    finally:
        release.set()
        thread.join(timeout=3)
        if process is not None and process.is_alive():
            process.terminate()
            process.join(timeout=2)
        registry.close()

    with SampleClaimRegistry(root) as fresh:
        fresh.claim_phase(plan=plan, phase=PHASE_QUALIFICATION_CELL,
                          bindings=_bindings(plan, PHASE_QUALIFICATION_CELL))


def test_forked_child_uses_new_flock_fd_while_parent_holds_file_lock(root: Path) -> None:
    try:
        context = multiprocessing.get_context("fork")
    except ValueError:
        pytest.skip("fork is unavailable")
    registry = SampleClaimRegistry(root)
    held = threading.Event()
    release = threading.Event()

    def hold_file_lock() -> None:
        with registry._exclusive_registry_lock():
            held.set()
            release.wait(timeout=8)

    thread = threading.Thread(target=hold_file_lock)
    thread.start()
    process = None
    try:
        assert held.wait(timeout=2)
        result_queue = context.Queue()
        process = context.Process(target=_reopen_after_fork_while_parent_guard_held,
                                  args=(registry, str(root), result_queue))
        process.start()
        assert result_queue.get(timeout=2) == ("inherited_closed", True, -1, -1)
        with pytest.raises(queue.Empty):
            result_queue.get(timeout=0.05)
        release.set()
        process.join(timeout=4)
        if process.is_alive():
            process.terminate()
            process.join()
            pytest.fail("fresh child registry hung on inherited flock")
        assert process.exitcode == 0
        assert result_queue.get(timeout=2) == ("audit_only", 0)
    finally:
        release.set()
        thread.join(timeout=3)
        if process is not None and process.is_alive():
            process.terminate()
            process.join(timeout=2)
        registry.close()


def test_close_waits_for_active_claim_serialization(
    root: Path, plan: K12SamplePlan, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = SampleClaimRegistry(root)
    real_publish = registry._durably_publish
    entered = threading.Event()
    continue_claim = threading.Event()
    closed = threading.Event()
    errors = []

    def delayed_publish(*args):
        entered.set()
        if not continue_claim.wait(timeout=3):
            raise TimeoutError("claim release signal missing")
        return real_publish(*args)

    def claim() -> None:
        try:
            registry.claim_phase(plan=plan, phase=PHASE_QUALIFICATION_CELL,
                                 bindings=_bindings(plan, PHASE_QUALIFICATION_CELL))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(registry, "_durably_publish", delayed_publish)
    claim_thread = threading.Thread(target=claim)
    close_thread = threading.Thread(target=lambda: (registry.close(), closed.set()))
    claim_thread.start()
    try:
        assert entered.wait(timeout=2)
        close_thread.start()
        assert not closed.wait(timeout=0.05)
    finally:
        continue_claim.set()
        claim_thread.join(timeout=3)
        if close_thread.is_alive():
            close_thread.join(timeout=3)
        if not closed.is_set():
            registry.close()
    assert not errors
    assert closed.is_set()
    with SampleClaimRegistry(root) as fresh:
        assert len(fresh.inspect_claims()) == 1


def test_process_shared_flock_allows_exactly_one_concurrent_double_claim(
    root: Path, plan: K12SamplePlan,
) -> None:
    try:
        context = multiprocessing.get_context("fork")
    except ValueError:
        pytest.skip("fork-based process-shared flock test is unavailable")
    gate = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(target=_claim_in_child,
                        args=(str(root), plan, PHASE_QUALIFICATION_CELL, gate, result_queue))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    gate.set()
    for process in processes:
        process.join(timeout=15)
        if process.is_alive():
            process.terminate()
            process.join()
            pytest.fail("concurrent registry claimant did not finish")
        assert process.exitcode == 0
    results = [result_queue.get(timeout=2) for _ in processes]
    assert sum(result[0] == "claimed" for result in results) == 1
    assert sum(result[0] == "ValueError" and "ALREADY CLAIMED" in result[1]
               for result in results) == 1
    with SampleClaimRegistry(root) as registry:
        assert len(registry.inspect_claims()) == 1
        with pytest.raises(ValueError, match="ALREADY CLAIMED"):
            registry.claim_phase(plan=plan, phase=PHASE_QUALIFICATION_CELL,
                                 bindings=_bindings(plan, PHASE_QUALIFICATION_CELL))


def test_different_output_bindings_in_two_processes_share_one_claim_authority(
    root: Path, plan: K12SamplePlan,
) -> None:
    try:
        context = multiprocessing.get_context("fork")
    except ValueError:
        pytest.skip("fork is unavailable")
    gate = context.Event()
    results = context.Queue()
    processes = [
        context.Process(target=_claim_in_child, args=(
            str(root), plan, PHASE_QUALIFICATION_CELL, gate, results, output_binding,
        ))
        for output_binding in ("output-root-a", "output-root-b")
    ]
    for process in processes:
        process.start()
    gate.set()
    for process in processes:
        process.join(timeout=15)
        if process.is_alive():
            process.terminate()
            process.join()
            pytest.fail("competing claim child did not finish")
        assert process.exitcode == 0
    outcomes = [results.get(timeout=2) for _ in processes]
    assert sum(result[0] == "claimed" for result in outcomes) == 1
    assert sum(result[0] == "ValueError" and "ALREADY CLAIMED" in result[1]
               for result in outcomes) == 1
    with SampleClaimRegistry(root) as registry:
        assert len(registry.inspect_claims()) == 1


def test_dead_claim_creator_never_unconsumes_its_sample_ids(
    root: Path, plan: K12SamplePlan,
) -> None:
    try:
        context = multiprocessing.get_context("fork")
    except ValueError:
        pytest.skip("fork is unavailable")
    gate = context.Event()
    results = context.Queue()
    child = context.Process(target=_claim_in_child, args=(
        str(root), plan, PHASE_QUALIFICATION_CELL, gate, results,
    ))
    child.start()
    gate.set()
    child.join(timeout=10)
    if child.is_alive():
        child.terminate()
        child.join()
        pytest.fail("claim creator did not exit")
    assert child.exitcode == 0
    assert results.get(timeout=2)[0] == "claimed"
    with SampleClaimRegistry(root) as registry:
        claims = registry.inspect_claims()
        assert len(claims) == 1
        assert claims[0].created_by_pid == child.pid
        bindings = _bindings(plan, PHASE_QUALIFICATION_CELL)
        changed = (replace(bindings[0], reservation_id="replacement-attempt"),
                   *bindings[1:])
        with pytest.raises(ValueError, match="ALREADY CLAIMED"):
            registry.claim_phase(plan=plan, phase=PHASE_QUALIFICATION_CELL,
                                 bindings=changed)


def test_context_close_only_closes_descriptors_and_preserves_claims(
    root: Path, plan: K12SamplePlan,
) -> None:
    phase = PHASE_QUALIFICATION_CELL
    with SampleClaimRegistry(root) as registry:
        original = registry.claim_phase(plan=plan, phase=phase, bindings=_bindings(plan, phase))
    with SampleClaimRegistry(root) as reopened:
        assert reopened.inspect_claims() == (original,)
        reopened.close()
        with pytest.raises(RuntimeError, match="closed"):
            reopened.inspect_claims()
    assert list(root.glob("claim-*.json"))
