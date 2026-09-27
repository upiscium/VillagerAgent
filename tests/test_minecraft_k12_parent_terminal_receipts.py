from __future__ import annotations

from dataclasses import fields, replace
import threading

import pytest

from benchmarks.minecraft.k12_execution_provenance import (
    ProvenanceError,
    QualificationCoordinateExecutionReceipt,
    QualificationExecutionStageReceipt,
    _coordinate_execution_receipt_identity,
)
from benchmarks.minecraft.k12_live_qualification import (
    _normalized_live_cell,
    issue_live_qualification_capabilities,
    publish_live_qualification_terminals,
    qualify_live_cell,
)


def _fixture(tmp_path, label: str):
    helpers = pytest.importorskip("test_minecraft_k12_authority_e2e")
    return helpers._build_qualification(tmp_path, label, publish=False)


def _copy(value):
    copied = object.__new__(type(value))
    for item in fields(type(value)):
        object.__setattr__(copied, item.name, getattr(value, item.name))
    return copied


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
