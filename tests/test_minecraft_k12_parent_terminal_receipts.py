from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, replace
import threading
from types import MappingProxyType

import pytest

from benchmarks.common.eac.canonical import canonical_sha256
from benchmarks.minecraft.k12_execution_provenance import (
    ProvenanceError,
    QualificationCoordinateExecutionReceipt,
    QualificationExecutionStageReceipt,
    _terminal_receipt_census_digest,
    _terminal_receipt_census_digest_from_identities,
    _coordinate_execution_receipt_identity,
)
from benchmarks.minecraft.k12_live_qualification import (
    _normalized_live_cell,
    issue_live_qualification_capabilities,
    publish_live_qualification_terminals,
    qualify_live_cell,
    qualify_live_probes,
)


def _fixture(tmp_path, label: str):
    helpers = pytest.importorskip("test_minecraft_k12_authority_e2e")
    return helpers._build_qualification(tmp_path, label, publish=False)


def _copy(value):
    copied = object.__new__(type(value))
    for item in fields(type(value)):
        object.__setattr__(copied, item.name, getattr(value, item.name))
    return copied


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _terminal_snapshot_identity(entry):
    return canonical_sha256({
        "artifact": "minecraft-k12-qualification-coordinate-terminal-receipt/2",
        "authority": entry["authority"],
        "activation": entry["activation"],
        "reservation": entry["reservation"],
        "domain": entry["domain"],
        "coordinate": entry["coordinate"],
        "capability": entry["capability"],
        "record": entry["record"],
        "execution_receipt": entry["execution_receipt"],
        "execution_stages": list(entry["execution_stages"]),
    })


def _replace_last_ledger_payload(ledger, monkeypatch, payload):
    event = ledger.events[-1]
    ledger._events[-1] = replace(
        event, payload=MappingProxyType(payload),
    )
    monkeypatch.setattr(ledger, "verify_chain", lambda: True)


def _recovery_registry_snapshot(controller):
    names = (
        "_ParentExecutionAuthority__qualification_coordinate_capabilities",
        "_ParentExecutionAuthority__qualification_coordinate_observations",
        "_ParentExecutionAuthority__qualification_execution_stage_receipts",
        "_ParentExecutionAuthority__qualification_execution_stage_identities",
        "_ParentExecutionAuthority__qualification_execution_stage_observations",
        "_ParentExecutionAuthority__qualification_execution_boundary_artifacts",
        "_ParentExecutionAuthority__qualification_execution_boundary_identities",
        "_ParentExecutionAuthority__qualification_coordinate_execution_receipts",
        "_ParentExecutionAuthority__qualification_coordinate_execution_identities",
        "_ParentExecutionAuthority__qualification_semantic_attestations",
    )
    return {name: dict(getattr(controller, name)) for name in names}


def _session_snapshot(session):
    return {
        key: dict(value) if isinstance(value, dict)
        else set(value) if isinstance(value, set)
        else value
        for key, value in session.items()
    }


def test_exact_parent_execution_receipts_mint_one_attestation(tmp_path):
    fixture = _fixture(tmp_path, "owned-receipts")
    try:
        receipts = tuple(
            value.execution_receipt for value in (*fixture.cells, *fixture.probes)
        )
        assert len(receipts) == 19
        assert all(
            isinstance(receipt, QualificationCoordinateExecutionReceipt)
            and fixture.controller.owns_qualification_coordinate_execution_receipt(receipt)
            for receipt in receipts
        )
        assert all(
            fixture.controller.owns_qualification_execution_stage_receipt(stage)
            for receipt in receipts
            for stage in fixture.controller.qualification_execution_stage_receipts(receipt)
        )
        attestation = publish_live_qualification_terminals(
            fixture.active_qualification,
            fixture.cells,
            fixture.probes,
            ledger=fixture.qualification_ledger,
        )
        assert attestation.semantic_result == "passed"
    finally:
        fixture.close()


def test_passed_terminal_append_retains_qualification_target_lease(
    tmp_path, monkeypatch,
):
    fixture = _fixture(tmp_path, "terminal-target-lease-linearization")
    terminal_entered = threading.Event()
    release_contended = threading.Event()
    release_uncontended = threading.Event()
    release_finished = threading.Event()
    continue_terminal = threading.Event()
    errors = []
    release_thread = None
    publisher = None

    class _ObservedRLock:
        def __init__(self, lock):
            self.lock = lock

        def __enter__(self):
            if threading.current_thread().name == "qualification-target-release":
                if self.lock.acquire(blocking=False):
                    release_uncontended.set()
                else:
                    release_contended.set()
                    self.lock.acquire()
                return self
            self.lock.acquire()
            return self

        def __exit__(self, _type, _value, _traceback):
            self.lock.release()

    fixture.qualification_lock._lifecycle_lock = _ObservedRLock(
        fixture.qualification_lock._lifecycle_lock
    )
    original_controller = fixture.controller._ledger_controller(
        fixture.qualification_ledger
    )

    class _PausingController:
        def terminal(self, payload):
            nonlocal release_thread

            def release_target():
                try:
                    fixture.qualification_lock.release()
                except BaseException as exc:  # pragma: no cover - diagnostic capture
                    errors.append(exc)
                finally:
                    release_finished.set()

            terminal_entered.set()
            release_thread = threading.Thread(
                target=release_target,
                name="qualification-target-release",
                daemon=True,
            )
            release_thread.start()
            if not continue_terminal.wait(timeout=30):
                raise AssertionError("terminal append was not released")
            return original_controller.terminal(payload)

    monkeypatch.setattr(
        fixture.controller,
        "_ledger_controller",
        lambda _ledger: _PausingController(),
    )

    def publish():
        try:
            publish_live_qualification_terminals(
                fixture.active_qualification,
                fixture.cells,
                fixture.probes,
                ledger=fixture.qualification_ledger,
            )
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    released_during_append = False
    try:
        publisher = threading.Thread(target=publish, daemon=True)
        publisher.start()
        assert terminal_entered.wait(timeout=60)
        assert release_contended.wait(timeout=5) or release_uncontended.wait(timeout=5)
        released_during_append = release_finished.wait(timeout=0.2)
    finally:
        continue_terminal.set()
        if publisher is not None:
            publisher.join(timeout=30)
        if release_thread is not None:
            release_thread.join(timeout=5)
        fixture.close()

    assert not released_during_append
    assert publisher is not None and not publisher.is_alive()
    assert release_thread is not None and not release_thread.is_alive()
    assert release_contended.is_set()
    assert not release_uncontended.is_set()
    assert not errors
    assert fixture.qualification_ledger.state == "terminal"
    assert release_finished.is_set()


def test_passed_terminal_anchor_failure_quarantines_target(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path, "terminal-anchor-failure-quarantine")

    def fail_anchor(**_kwargs):
        raise OSError("head anchor fsync failed")

    monkeypatch.setattr(
        fixture.qualification_ledger._storage,
        "_write_head_anchor_locked",
        fail_anchor,
    )
    try:
        with pytest.raises(ProvenanceError, match="authority_replay"):
            publish_live_qualification_terminals(
                fixture.active_qualification,
                fixture.cells,
                fixture.probes,
                ledger=fixture.qualification_ledger,
            )
        assert fixture.qualification_ledger._durability_unknown is True
        assert fixture.qualification_lock.quarantined is True
        assert fixture.qualification_lock.quarantine_record["status"] == "quarantined"
    finally:
        fixture.close()


def test_copied_active_qualification_handle_is_not_parent_owned(tmp_path):
    fixture = _fixture(tmp_path, "copied-active-qualification")
    try:
        copied = _copy(fixture.active_qualification)
        assert not copied.current_at()
        assert not fixture.controller.validate_current_authority(copied)
        with pytest.raises(ProvenanceError, match="authority_replay"):
            issue_live_qualification_capabilities(copied)
        with pytest.raises(ProvenanceError, match="authority_replay"):
            qualify_live_probes(copied, fixture.probes)
    finally:
        fixture.close()


def test_584_legitimate_capability_plus_fabricated_normalized_cell_is_rejected(tmp_path):
    fixture = _fixture(tmp_path, "issue-584-lower-layer")
    try:
        evidence = fixture.cells[0]
        fabricated = _normalized_live_cell(
            fixture.active_qualification,
            evidence,
            qualify_live_cell(fixture.active_qualification, evidence),
        )
        with pytest.raises(ProvenanceError, match="authority_replay"):
            fixture.controller._observe_qualification_coordinate_terminal(
                fixture.active_qualification,
                evidence.terminal_capability,
                fabricated,
            )
        assert fixture.qualification_ledger.state == "active"
        attestation = publish_live_qualification_terminals(
            fixture.active_qualification,
            fixture.cells,
            fixture.probes,
            ledger=fixture.qualification_ledger,
        )
        assert attestation.semantic_result == "passed"
    finally:
        fixture.close()


def test_caller_cell_semantics_and_origin_cannot_replace_execution_receipt(tmp_path):
    fixture = _fixture(tmp_path, "caller-cell")
    try:
        cells = list(fixture.cells)
        cells[0] = replace(
            cells[0],
            fresh_root=True,
            reset_passed=True,
            provider_terminal="success",
            oracle_value="true",
            containment_clean=True,
            evidence_valid=True,
            terminal_verified=True,
            evidence_origin="runtime_verified",
            execution_receipt=None,
        )
        with pytest.raises(ProvenanceError, match="authority_replay"):
            publish_live_qualification_terminals(
                fixture.active_qualification,
                tuple(cells),
                fixture.probes,
                ledger=fixture.qualification_ledger,
            )
        assert fixture.qualification_ledger.state == "active"
    finally:
        fixture.close()


def test_caller_probe_success_cannot_replace_execution_receipt(tmp_path):
    fixture = _fixture(tmp_path, "caller-probe")
    try:
        probe = replace(
            fixture.probes[0], passed=True, terminal_verified=True,
            evidence_origin="runtime_verified", execution_receipt=None,
        )
        with pytest.raises(ProvenanceError, match="authority_replay"):
            fixture.controller._observe_qualification_coordinate_terminal(
                fixture.active_qualification,
                probe.terminal_capability,
                probe.execution_receipt,
            )
        assert fixture.qualification_ledger.state == "active"
    finally:
        fixture.close()


@pytest.mark.parametrize(
    "stage_name", ("reset", "provider", "permit", "effect", "oracle", "containment"),
)
def test_mutated_owned_stage_receipt_fails_before_registry_insertion(
    tmp_path, stage_name,
):
    fixture = _fixture(tmp_path, f"mutated-stage-{stage_name}")
    try:
        evidence = fixture.cells[0]
        stages = fixture.controller.qualification_execution_stage_receipts(
            evidence.execution_receipt,
        )
        stage = next(value for value in stages if value.stage == stage_name)
        assert isinstance(stage, QualificationExecutionStageReceipt)
        copied = _copy(stage)
        assert not fixture.controller.owns_qualification_execution_stage_receipt(copied)
        object.__setattr__(stage, "observation_digest", "sha256:" + "f" * 64)
        with pytest.raises(ProvenanceError, match="authority_replay"):
            fixture.controller._observe_qualification_coordinate_terminal(
                fixture.active_qualification,
                evidence.terminal_capability,
                evidence.execution_receipt,
            )
        assert fixture.qualification_ledger.state == "active"
    finally:
        fixture.close()


def test_copied_or_partial_composite_receipt_is_rejected(tmp_path):
    fixture = _fixture(tmp_path, "copied-composite")
    try:
        evidence = fixture.cells[0]
        copied = _copy(evidence.execution_receipt)
        assert not fixture.controller.owns_qualification_coordinate_execution_receipt(copied)
        with pytest.raises(ProvenanceError, match="authority_replay"):
            fixture.controller._observe_qualification_coordinate_terminal(
                fixture.active_qualification,
                evidence.terminal_capability,
                copied,
            )
        object.__setattr__(
            evidence.execution_receipt,
            "stage_receipt_identities",
            evidence.execution_receipt.stage_receipt_identities[:-1],
        )
        with pytest.raises(ProvenanceError, match="authority_replay"):
            fixture.controller._observe_qualification_coordinate_terminal(
                fixture.active_qualification,
                evidence.terminal_capability,
                evidence.execution_receipt,
            )
    finally:
        fixture.close()


def test_rehashed_mutated_execution_receipt_is_not_parent_owned(tmp_path):
    fixture = _fixture(tmp_path, "rehashed-receipt")
    try:
        receipt = fixture.cells[0].execution_receipt
        object.__setattr__(receipt, "coordinate", "forged-coordinate")
        object.__setattr__(receipt, "identity", _coordinate_execution_receipt_identity(receipt))
        assert not fixture.controller.owns_qualification_coordinate_execution_receipt(receipt)
        with pytest.raises(ProvenanceError, match="authority_replay"):
            fixture.controller._observe_qualification_coordinate_terminal(
                fixture.active_qualification,
                fixture.cells[0].terminal_capability,
                receipt,
            )
    finally:
        fixture.close()


def test_cross_cell_and_cross_authority_execution_receipts_are_rejected(tmp_path):
    left = _fixture(tmp_path / "left", "receipt-left")
    right = _fixture(tmp_path / "right", "receipt-right")
    try:
        with pytest.raises(ProvenanceError, match="authority_replay"):
            left.controller._observe_qualification_coordinate_terminal(
                left.active_qualification,
                left.cells[0].terminal_capability,
                left.cells[1].execution_receipt,
            )
        with pytest.raises(ProvenanceError, match="authority_replay"):
            right.controller._observe_qualification_coordinate_terminal(
                right.active_qualification,
                right.cells[0].terminal_capability,
                left.cells[0].execution_receipt,
            )
    finally:
        left.close()
        right.close()


def test_s_arm_caller_rejection_fields_cannot_replace_owned_receipts(tmp_path):
    fixture = _fixture(tmp_path, "forged-s-fields")
    try:
        evidence = next(value for value in fixture.cells if value.cell_id.endswith("-S"))
        fabricated = replace(
            evidence,
            rejection_evidence=None,
            rejection_verified=True,
            oracle_value="not_applicable",
            execution_receipt=None,
        )
        with pytest.raises(ProvenanceError, match="authority_replay"):
            fixture.controller._observe_qualification_coordinate_terminal(
                fixture.active_qualification,
                fabricated.terminal_capability,
                fabricated.execution_receipt,
            )
    finally:
        fixture.close()


def test_arbitrary_stage_and_normalized_record_mints_are_not_exposed(tmp_path):
    fixture = _fixture(tmp_path, "closed-mint-surface")
    try:
        assert not hasattr(fixture.controller, "_mint_execution_stage_receipt")
        assert not hasattr(fixture.controller, "_mint_coordinate_execution_receipt")
    finally:
        fixture.close()


def test_late_invalid_receipt_batch_is_atomic_and_retryable(tmp_path):
    fixture = _fixture(tmp_path, "atomic-batch")
    try:
        probes = list(fixture.probes)
        probes[-1] = replace(probes[-1], execution_receipt=None)
        first_receipt = fixture.cells[0].execution_receipt
        with pytest.raises(ProvenanceError, match="authority_replay"):
            publish_live_qualification_terminals(
                fixture.active_qualification,
                fixture.cells,
                tuple(probes),
                ledger=fixture.qualification_ledger,
            )
        assert fixture.controller.owns_qualification_coordinate_execution_receipt(
            first_receipt
        )
        attestation = publish_live_qualification_terminals(
            fixture.active_qualification,
            fixture.cells,
            fixture.probes,
            ledger=fixture.qualification_ledger,
        )
        assert attestation.semantic_result == "passed"
    finally:
        fixture.close()


def test_batch_commit_uses_validated_snapshots_after_receipt_mutation(
    tmp_path, monkeypatch,
):
    fixture = _fixture(tmp_path, "atomic-mutation")
    try:
        original = fixture.controller._validate_qualification_execution_pair
        calls = 0

        def validate(authority, capability, execution_receipt):
            nonlocal calls
            result = original(authority, capability, execution_receipt)
            calls += 1
            if calls == 19:
                object.__setattr__(
                    fixture.cells[0].execution_receipt,
                    "coordinate",
                    "forged-coordinate",
                )
            return result

        monkeypatch.setattr(
            fixture.controller, "_validate_qualification_execution_pair", validate,
        )
        attestation = publish_live_qualification_terminals(
            fixture.active_qualification,
            fixture.cells,
            fixture.probes,
            ledger=fixture.qualification_ledger,
        )
        assert calls == 19
        assert attestation.semantic_result == "passed"
    finally:
        fixture.close()


def test_passed_ledger_append_failure_does_not_consume_receipts(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path, "ledger-append-failure")
    try:
        first_receipt = fixture.cells[0].execution_receipt

        def fail_append(*_args, **_kwargs):
            raise ProvenanceError("authority_replay")

        monkeypatch.setattr(
            fixture.controller,
            "_ledger_terminal_qualification_batch",
            fail_append,
        )
        with pytest.raises(ProvenanceError, match="authority_replay"):
            publish_live_qualification_terminals(
                fixture.active_qualification,
                fixture.cells,
                fixture.probes,
                ledger=fixture.qualification_ledger,
            )
        assert fixture.qualification_ledger.state == "active"
        assert fixture.controller.owns_qualification_coordinate_execution_receipt(
            first_receipt
        )
    finally:
        fixture.close()


def test_durable_pass_event_recovers_interrupted_registry_commit(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path, "registry-recovery")
    try:
        def fail_commit(*_args, **_kwargs):
            raise ProvenanceError("authority_replay")

        monkeypatch.setattr(
            fixture.controller,
            "_register_qualification_terminal",
            fail_commit,
        )
        with pytest.raises(ProvenanceError, match="authority_replay"):
            publish_live_qualification_terminals(
                fixture.active_qualification,
                fixture.cells,
                fixture.probes,
                ledger=fixture.qualification_ledger,
            )
        assert fixture.qualification_ledger.state == "terminal"
        attestation = fixture.controller.recover_qualification_semantics(
            fixture.active_qualification,
        )
        assert attestation.semantic_result == "passed"
        assert fixture.controller.owns_qualification_semantic_attestation(
            attestation,
            authority=fixture.active_qualification,
        )
    finally:
        fixture.close()


def test_pass_recovery_requires_parent_recorded_event_digest(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path, "missing-recovery-event-digest")
    try:
        def fail_commit(*_args, **_kwargs):
            raise ProvenanceError("authority_replay")

        monkeypatch.setattr(
            fixture.controller,
            "_register_qualification_terminal",
            fail_commit,
        )
        with pytest.raises(ProvenanceError, match="authority_replay"):
            publish_live_qualification_terminals(
                fixture.active_qualification,
                fixture.cells,
                fixture.probes,
                ledger=fixture.qualification_ledger,
            )
        session = fixture.controller._ParentExecutionAuthority__qualification_semantic_sessions[
            fixture.active_qualification.identity
        ]
        session["terminal_event_digest"] = None
        with pytest.raises(ProvenanceError, match="final_prerequisite_mismatch"):
            fixture.controller.recover_qualification_semantics(
                fixture.active_qualification,
            )
        assert fixture.qualification_ledger.state == "terminal"
    finally:
        fixture.close()


def test_pass_recovery_requires_registered_parent_execution_receipt(
    tmp_path, monkeypatch,
):
    fixture = _fixture(tmp_path, "unregistered-recovery-receipt")
    try:
        def fail_commit(*_args, **_kwargs):
            raise ProvenanceError("authority_replay")

        monkeypatch.setattr(
            fixture.controller,
            "_register_qualification_terminal",
            fail_commit,
        )
        with pytest.raises(ProvenanceError, match="authority_replay"):
            publish_live_qualification_terminals(
                fixture.active_qualification,
                fixture.cells,
                fixture.probes,
                ledger=fixture.qualification_ledger,
            )
        payload = _thaw(fixture.qualification_ledger.events[-1].payload)
        snapshot = payload["terminal_registry_snapshot"]
        forged = snapshot["receipts"][0]
        forged["execution_receipt"] = "sha256:" + "f" * 64
        forged["identity"] = _terminal_snapshot_identity(forged)
        payload["terminal_receipt_digest"] = (
            _terminal_receipt_census_digest_from_identities(
                tuple(item["identity"] for item in snapshot["receipts"])
            )
        )
        _replace_last_ledger_payload(
            fixture.qualification_ledger, monkeypatch, payload,
        )

        with pytest.raises(ProvenanceError, match="authority_replay"):
            fixture.controller.recover_qualification_semantics(
                fixture.active_qualification,
            )
        session = fixture.controller._ParentExecutionAuthority__qualification_semantic_sessions[
            fixture.active_qualification.identity
        ]
        assert session["state"] == "collecting"
        assert fixture.controller.owns_qualification_coordinate_execution_receipt(
            fixture.cells[0].execution_receipt,
        )
    finally:
        fixture.close()


def test_pass_recovery_requires_parent_capability_identity(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path, "unbound-recovery-capability")
    try:
        def fail_commit(*_args, **_kwargs):
            raise ProvenanceError("authority_replay")

        monkeypatch.setattr(
            fixture.controller,
            "_register_qualification_terminal",
            fail_commit,
        )
        with pytest.raises(ProvenanceError, match="authority_replay"):
            publish_live_qualification_terminals(
                fixture.active_qualification,
                fixture.cells,
                fixture.probes,
                ledger=fixture.qualification_ledger,
            )
        payload = _thaw(fixture.qualification_ledger.events[-1].payload)
        snapshot = payload["terminal_registry_snapshot"]
        forged = snapshot["receipts"][0]
        forged["capability"] = "sha256:" + "e" * 64
        forged["identity"] = _terminal_snapshot_identity(forged)
        payload["terminal_receipt_digest"] = (
            _terminal_receipt_census_digest_from_identities(
                tuple(item["identity"] for item in snapshot["receipts"])
            )
        )
        _replace_last_ledger_payload(
            fixture.qualification_ledger, monkeypatch, payload,
        )

        with pytest.raises(ProvenanceError, match="authority_replay"):
            fixture.controller.recover_qualification_semantics(
                fixture.active_qualification,
            )
        session = fixture.controller._ParentExecutionAuthority__qualification_semantic_sessions[
            fixture.active_qualification.identity
        ]
        assert session["state"] == "collecting"
    finally:
        fixture.close()


def test_pass_recovery_rolls_back_when_attestation_mint_fails(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path, "atomic-recovery-mint")
    try:
        def fail_commit(*_args, **_kwargs):
            raise ProvenanceError("authority_replay")

        monkeypatch.setattr(
            fixture.controller,
            "_register_qualification_terminal",
            fail_commit,
        )
        with pytest.raises(ProvenanceError, match="authority_replay"):
            publish_live_qualification_terminals(
                fixture.active_qualification,
                fixture.cells,
                fixture.probes,
                ledger=fixture.qualification_ledger,
            )
        monkeypatch.undo()
        original_receipt = fixture.cells[0].execution_receipt

        def fail_mint(*_args, **_kwargs):
            raise ProvenanceError("authority_replay")

        monkeypatch.setattr(
            fixture.controller,
            "_mint_qualification_semantic_attestation",
            fail_mint,
        )
        session = fixture.controller._ParentExecutionAuthority__qualification_semantic_sessions[
            fixture.active_qualification.identity
        ]
        before_registries = _recovery_registry_snapshot(fixture.controller)
        before_session = _session_snapshot(session)
        with pytest.raises(ProvenanceError, match="authority_replay"):
            fixture.controller.recover_qualification_semantics(
                fixture.active_qualification,
            )
        assert _recovery_registry_snapshot(fixture.controller) == before_registries
        assert _session_snapshot(session) == before_session
        assert session["state"] == "collecting"
        assert fixture.controller.owns_qualification_coordinate_execution_receipt(
            original_receipt,
        )
        monkeypatch.undo()
        attestation = fixture.controller.recover_qualification_semantics(
            fixture.active_qualification,
        )
        assert attestation.semantic_result == "passed"
    finally:
        fixture.close()


def test_pass_recovery_uses_parent_terminal_registry_after_late_failure(
    tmp_path, monkeypatch,
):
    fixture = _fixture(tmp_path, "late-attestation-recovery")
    try:
        original_mint = fixture.controller._mint_qualification_semantic_attestation

        def fail_mint(*_args, **_kwargs):
            raise ProvenanceError("authority_replay")

        monkeypatch.setattr(
            fixture.controller,
            "_mint_qualification_semantic_attestation",
            fail_mint,
        )
        with pytest.raises(ProvenanceError, match="authority_replay"):
            publish_live_qualification_terminals(
                fixture.active_qualification,
                fixture.cells,
                fixture.probes,
                ledger=fixture.qualification_ledger,
            )
        assert fixture.qualification_ledger.state == "terminal"
        assert not fixture.controller.owns_qualification_coordinate_execution_receipt(
            fixture.cells[0].execution_receipt,
        )
        monkeypatch.setattr(
            fixture.controller,
            "_mint_qualification_semantic_attestation",
            original_mint,
        )
        attestation = fixture.controller.qualification_semantic_attestation(
            fixture.active_qualification,
        )
        assert attestation.semantic_result == "passed"
    finally:
        fixture.close()


def test_durable_failed_event_recovers_interrupted_cleanup(tmp_path, monkeypatch):
    helpers = pytest.importorskip("test_minecraft_k12_authority_e2e")
    fixture = helpers._build_qualification(
        tmp_path, "failed-registry-recovery", publish=False, failed_coordinate="P3",
    )
    try:
        def fail_cleanup(*_args, **_kwargs):
            raise ProvenanceError("authority_replay")

        monkeypatch.setattr(
            fixture.controller,
            "_discard_qualification_execution_scope",
            fail_cleanup,
        )
        with pytest.raises(ProvenanceError, match="authority_replay"):
            publish_live_qualification_terminals(
                fixture.active_qualification,
                fixture.cells,
                fixture.probes,
                ledger=fixture.qualification_ledger,
            )
        assert fixture.qualification_ledger.state == "terminal"
        monkeypatch.undo()
        with pytest.raises(ProvenanceError, match="qualification_failed"):
            fixture.controller.recover_qualification_semantics(
                fixture.active_qualification,
            )
        assert not fixture.controller.owns_qualification_coordinate_execution_receipt(
            fixture.cells[0].execution_receipt,
        )
    finally:
        fixture.close()


def test_failed_recovery_requires_parent_recorded_event_digest(tmp_path, monkeypatch):
    helpers = pytest.importorskip("test_minecraft_k12_authority_e2e")
    fixture = helpers._build_qualification(
        tmp_path, "missing-failed-recovery-event-digest", publish=False,
        failed_coordinate="P3",
    )
    try:
        def fail_cleanup(*_args, **_kwargs):
            raise ProvenanceError("authority_replay")

        monkeypatch.setattr(
            fixture.controller,
            "_discard_qualification_execution_scope",
            fail_cleanup,
        )
        with pytest.raises(ProvenanceError, match="authority_replay"):
            publish_live_qualification_terminals(
                fixture.active_qualification,
                fixture.cells,
                fixture.probes,
                ledger=fixture.qualification_ledger,
            )
        monkeypatch.undo()
        session = fixture.controller._ParentExecutionAuthority__qualification_semantic_sessions[
            fixture.active_qualification.identity
        ]
        session["terminal_event_digest"] = None
        with pytest.raises(ProvenanceError, match="final_prerequisite_mismatch"):
            fixture.controller.recover_qualification_semantics(
                fixture.active_qualification,
            )
        assert session["state"] == "failed"
        assert fixture.controller.owns_qualification_coordinate_execution_receipt(
            fixture.cells[0].execution_receipt,
        )
    finally:
        fixture.close()


def test_failed_recovery_requires_exact_durable_payload(tmp_path, monkeypatch):
    helpers = pytest.importorskip("test_minecraft_k12_authority_e2e")
    fixture = helpers._build_qualification(
        tmp_path, "malformed-failed-recovery-payload", publish=False,
        failed_coordinate="P3",
    )
    try:
        def fail_cleanup(*_args, **_kwargs):
            raise ProvenanceError("authority_replay")

        monkeypatch.setattr(
            fixture.controller,
            "_discard_qualification_execution_scope",
            fail_cleanup,
        )
        with pytest.raises(ProvenanceError, match="authority_replay"):
            publish_live_qualification_terminals(
                fixture.active_qualification,
                fixture.cells,
                fixture.probes,
                ledger=fixture.qualification_ledger,
            )
        monkeypatch.undo()
        payload = _thaw(fixture.qualification_ledger.events[-1].payload)
        payload["failure_reason"] = "forged-failure"
        _replace_last_ledger_payload(
            fixture.qualification_ledger, monkeypatch, payload,
        )
        with pytest.raises(ProvenanceError, match="final_prerequisite_mismatch"):
            fixture.controller.recover_qualification_semantics(
                fixture.active_qualification,
            )
        session = fixture.controller._ParentExecutionAuthority__qualification_semantic_sessions[
            fixture.active_qualification.identity
        ]
        assert session["state"] == "failed"
        assert fixture.controller.owns_qualification_coordinate_execution_receipt(
            fixture.cells[0].execution_receipt,
        )
    finally:
        fixture.close()


def test_failed_recovery_rejects_tampered_activation_binding(tmp_path, monkeypatch):
    helpers = pytest.importorskip("test_minecraft_k12_authority_e2e")
    fixture = helpers._build_qualification(
        tmp_path, "tampered-failed-recovery-binding", publish=False,
        failed_coordinate="P3",
    )
    try:
        def fail_cleanup(*_args, **_kwargs):
            raise ProvenanceError("authority_replay")

        monkeypatch.setattr(
            fixture.controller,
            "_discard_qualification_execution_scope",
            fail_cleanup,
        )
        with pytest.raises(ProvenanceError, match="authority_replay"):
            publish_live_qualification_terminals(
                fixture.active_qualification,
                fixture.cells,
                fixture.probes,
                ledger=fixture.qualification_ledger,
            )
        monkeypatch.undo()
        object.__setattr__(
            fixture.active_qualification,
            "activation_digest",
            "sha256:" + "f" * 64,
        )
        with pytest.raises(ProvenanceError, match="final_prerequisite_mismatch"):
            fixture.controller.recover_qualification_semantics(
                fixture.active_qualification,
            )
    finally:
        fixture.close()


def test_direct_failed_ledger_terminalization_requires_staged_parent_failure(tmp_path):
    fixture = _fixture(tmp_path, "direct-failed-ledger")
    try:
        with pytest.raises(ProvenanceError, match="final_prerequisite_mismatch"):
            fixture.controller.ledger_terminal(
                fixture.qualification_ledger,
                {
                    "result": "failed",
                    "phase": "qualification",
                    "authority_digest": fixture.active_qualification.identity,
                    "qualification_aggregate_digest": "forged-aggregate",
                    "probe_aggregate_digest": "forged-probes",
                    "evidence_origin": "injected_fake",
                    "terminal_verified": False,
                    "failure_reason": "forged",
                },
            )
        assert fixture.qualification_ledger.state == "active"
    finally:
        fixture.close()


def test_stale_authority_rejects_owned_execution_receipts(tmp_path):
    fixture = _fixture(tmp_path, "stale-receipts")
    try:
        fixture.controller.advance_trusted_time(400)
        with pytest.raises(ProvenanceError, match="authority_replay"):
            publish_live_qualification_terminals(
                fixture.active_qualification,
                fixture.cells,
                fixture.probes,
                ledger=fixture.qualification_ledger,
            )
        assert fixture.qualification_ledger.state == "active"
    finally:
        fixture.close()


def test_authority_expiry_before_durable_pass_event_keeps_ledger_active(
    tmp_path, monkeypatch,
):
    fixture = _fixture(tmp_path, "stale-before-pass-event")
    original_terminal = fixture.controller._ledger_terminal_qualification_batch

    def expire_before_append(*args, **kwargs):
        fixture.controller.advance_trusted_time(400)
        return original_terminal(*args, **kwargs)

    monkeypatch.setattr(
        fixture.controller,
        "_ledger_terminal_qualification_batch",
        expire_before_append,
    )
    try:
        with pytest.raises(ProvenanceError, match="authority_replay"):
            from benchmarks.minecraft.k12_live_qualification import (
                publish_live_qualification_terminals,
            )

            publish_live_qualification_terminals(
                fixture.active_qualification,
                fixture.cells,
                fixture.probes,
                ledger=fixture.qualification_ledger,
            )
        assert fixture.qualification_ledger.state == "active"
    finally:
        fixture.close()


def test_authority_expiry_before_durable_failed_event_keeps_ledger_active(
    tmp_path, monkeypatch,
):
    helpers = pytest.importorskip("test_minecraft_k12_authority_e2e")
    fixture = helpers._build_qualification(
        tmp_path, "stale-before-failed-event", publish=False, failed_coordinate="P3",
    )
    original_terminal = fixture.controller.ledger_terminal

    def expire_before_append(*args, **kwargs):
        fixture.controller.advance_trusted_time(400)
        return original_terminal(*args, **kwargs)

    monkeypatch.setattr(fixture.controller, "ledger_terminal", expire_before_append)
    try:
        with pytest.raises(ProvenanceError, match="authority_replay"):
            from benchmarks.minecraft.k12_live_qualification import (
                publish_live_qualification_terminals,
            )

            publish_live_qualification_terminals(
                fixture.active_qualification,
                fixture.cells,
                fixture.probes,
                ledger=fixture.qualification_ledger,
            )
        assert fixture.qualification_ledger.state == "active"
    finally:
        fixture.close()


def test_explicit_time_update_waits_for_durable_terminal_append(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path, "explicit-time-terminalization-race")
    original_controller = fixture.controller._ledger_controller
    updater_started = threading.Event()
    updater_finished = threading.Event()
    updater_errors = []
    updater_threads = []

    def update_explicit_time():
        updater_started.set()
        try:
            fixture.controller.validate_current_authority(
                fixture.active_qualification, now=200,
            )
        except BaseException as exc:  # pragma: no cover - diagnostic assertion below
            updater_errors.append(exc)
        finally:
            updater_finished.set()

    def guarded_controller(ledger):
        updater = threading.Thread(target=update_explicit_time)
        updater_threads.append(updater)
        updater.start()
        assert updater_started.wait(1)
        assert not updater_finished.wait(0.05)
        return original_controller(ledger)

    monkeypatch.setattr(fixture.controller, "_ledger_controller", guarded_controller)
    try:
        try:
            from benchmarks.minecraft.k12_live_qualification import (
                publish_live_qualification_terminals,
            )

            publish_live_qualification_terminals(
                fixture.active_qualification,
                fixture.cells,
                fixture.probes,
                ledger=fixture.qualification_ledger,
            )
        except ProvenanceError:
            pass
        for updater in updater_threads:
            updater.join(timeout=1)
        assert updater_finished.is_set()
        assert updater_errors == []
        assert fixture.qualification_ledger.state == "terminal"
    finally:
        fixture.close()


@pytest.mark.parametrize("payload", ({}, {"result": "passed-ish"}, {"result": None}))
def test_qualification_ledger_rejects_unrecognized_terminal_payload(
    tmp_path, payload,
):
    fixture = _fixture(tmp_path, "unrecognized-terminal-payload")
    try:
        with pytest.raises(ProvenanceError, match="final_prerequisite_mismatch"):
            fixture.controller.ledger_terminal(fixture.qualification_ledger, payload)
        assert fixture.qualification_ledger.state == "active"
    finally:
        fixture.close()


def test_parent_public_pass_terminal_requires_exact_payload_and_records_event(tmp_path):
    fixture = _fixture(tmp_path, "public-pass-terminal")
    try:
        verdict = fixture.controller._publish_qualification_terminal_batch(
            fixture.active_qualification,
            tuple(
                (evidence.terminal_capability, evidence.execution_receipt)
                for evidence in (*fixture.cells, *fixture.probes)
            ),
        )
        assert verdict.passed is True
        census, verified_verdict = fixture.controller._prepare_qualification_semantics(
            fixture.active_qualification,
        )
        session = fixture.controller._ParentExecutionAuthority__qualification_semantic_sessions[
            fixture.active_qualification.identity
        ]
        payload = fixture.controller._qualification_pass_payload(
            fixture.active_qualification,
            census,
            verified_verdict,
            _terminal_receipt_census_digest(session),
            tuple(session["receipts"].values()),
        )
        with pytest.raises(ProvenanceError, match="final_prerequisite_mismatch"):
            fixture.controller.ledger_terminal(
                fixture.qualification_ledger,
                {**payload, "unexpected": True},
            )
        assert fixture.qualification_ledger.state == "active"
        digest = fixture.controller.ledger_terminal(
            fixture.qualification_ledger, payload,
        )
        assert fixture.qualification_ledger.state == "terminal"
        assert session["terminal_event_digest"] == digest
        attestation = fixture.controller.finalize_qualification_semantics(
            fixture.active_qualification,
        )
        assert attestation.semantic_result == "passed"
    finally:
        fixture.close()


def test_copied_or_rehashed_active_authority_cannot_open_receipt_scope(tmp_path):
    fixture = _fixture(tmp_path, "copied-active")
    try:
        copied = _copy(fixture.active_qualification)
        object.__setattr__(copied, "activation_digest", "sha256:" + "f" * 64)
        with pytest.raises(ProvenanceError, match="authority_replay"):
            issue_live_qualification_capabilities(copied)
        with pytest.raises(ProvenanceError, match="authority_replay"):
            fixture.controller.mint_injected_qualification_execution_receipts(
                copied,
                tuple(),
                tuple(),
                cell_campaign_id="cells",
                probe_campaign_id="probes",
                execution_identity="copied",
            )
    finally:
        fixture.close()
