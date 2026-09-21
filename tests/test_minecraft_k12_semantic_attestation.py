from __future__ import annotations

from dataclasses import fields, replace

import pytest

from benchmarks.minecraft.k12_execution_provenance import (
    FinalExecutionPrerequisites,
    ProvenanceError,
    QualificationSemanticAttestation,
)
from benchmarks.minecraft.k12_live_qualification import (
    LiveQualificationAggregate,
    _normalized_live_cell,
    _normalized_live_probe,
    aggregate_live_qualification,
    publish_live_qualification_terminals,
    qualify_live_cell,
)


def _qualification(tmp_path, label: str, *, publish: bool = True):
    helpers = pytest.importorskip("test_minecraft_k12_authority_e2e")
    return helpers._build_qualification(tmp_path, label, publish=publish)


def _aggregate(fixture):
    return aggregate_live_qualification(
        fixture.active_qualification,
        fixture.cells,
        fixture.probes,
        ledger=fixture.qualification_ledger,
    )


def _fabricated_attestation(source: QualificationSemanticAttestation):
    value = object.__new__(QualificationSemanticAttestation)
    for item in fields(QualificationSemanticAttestation):
        object.__setattr__(value, item.name, getattr(source, item.name))
    return value


def test_parent_registry_mints_one_owned_semantic_attestation_and_final_uses_it(tmp_path):
    helpers = pytest.importorskip("test_minecraft_k12_authority_e2e")
    graph = helpers._build_graph(tmp_path, "semantic-positive")
    try:
        attestation = graph.aggregate.semantic_attestation
        assert isinstance(attestation, QualificationSemanticAttestation)
        assert graph.controller.owns_qualification_semantic_attestation(
            attestation, authority=graph.active_qualification,
        )
        assert attestation.semantic_result == "passed"
        assert graph.prerequisites.qualification_semantic_attestation is attestation
        assert graph.final_authority.body[
            "qualification_semantic_attestation_digest"
        ] == attestation.identity
        assert graph.final_authority.run_authorization.body[
            "qualification_semantic_attestation_digest"
        ] == attestation.identity
        for body in (
            graph.final_authority.body,
            graph.final_authority.run_authorization.body,
        ):
            assert body["qualification_semantic_projection_digest"] \
                == attestation.semantic_projection_digest
            assert body["qualification_probe_projection_digest"] \
                == attestation.probe_terminal_digest
            assert body["qualification_terminal_receipt_census_digest"] \
                == attestation.terminal_receipt_digest
            assert body["qualification_terminal_event_digest"] \
                == attestation.terminal_event_digest
            assert body["qualification_terminal_ledger_digest"] \
                == attestation.terminal_ledger_digest
            assert "qualification_aggregate_digest" not in body
            assert "probe_aggregate_digest" not in body
        assert graph.fence.real_counts == {channel: 0 for channel in helpers.EXTERNAL_ENTRY_CHANNELS}
    finally:
        graph.close()


def test_terminal_registry_and_attestation_precede_optional_aggregate(tmp_path):
    fixture = _qualification(tmp_path, "registry-first", publish=False)
    try:
        assert fixture.qualification_ledger.state == "active"
        with pytest.raises(ProvenanceError, match="final_prerequisite_mismatch"):
            _aggregate(fixture)
        attestation = publish_live_qualification_terminals(
            fixture.active_qualification,
            fixture.cells,
            fixture.probes,
            ledger=fixture.qualification_ledger,
        )
        terminal = dict(fixture.qualification_ledger.events[-1].payload)
        assert terminal["semantic_projection_digest"] \
            == attestation.semantic_projection_digest
        assert terminal["semantic_probe_digest"] == attestation.probe_terminal_digest
        assert terminal["terminal_receipt_digest"] == attestation.terminal_receipt_digest
        assert "qualification_aggregate_digest" not in terminal
        receipts = fixture.controller.qualification_terminal_receipts(
            fixture.active_qualification
        )
        assert len(receipts) == 19
        assert all(
            fixture.controller.owns_qualification_terminal_receipt(receipt)
            for receipt in receipts
        )
        copied_receipt = object.__new__(type(receipts[0]))
        for item in fields(type(receipts[0])):
            object.__setattr__(copied_receipt, item.name, getattr(receipts[0], item.name))
        assert not fixture.controller.owns_qualification_terminal_receipt(copied_receipt)
        aggregate = _aggregate(fixture)
        assert aggregate.semantic_attestation is attestation
    finally:
        fixture.close()


def test_optional_probe_binding_survives_post_terminal_presentation(tmp_path):
    fixture = _qualification(tmp_path, "optional-probe-binding", publish=False)
    try:
        probes = tuple(replace(value, authority_binding=None) for value in fixture.probes)
        attestation = publish_live_qualification_terminals(
            fixture.active_qualification,
            fixture.cells,
            probes,
            ledger=fixture.qualification_ledger,
        )
        aggregate = aggregate_live_qualification(
            fixture.active_qualification,
            fixture.cells,
            probes,
            ledger=fixture.qualification_ledger,
        )
        assert aggregate.semantic_attestation is attestation
    finally:
        fixture.close()


def test_parent_registry_snapshots_normalized_records_before_publication(tmp_path):
    fixture = _qualification(tmp_path, "normalized-snapshot", publish=False)
    try:
        first_evidence = fixture.cells[0]
        first_result = qualify_live_cell(fixture.active_qualification, first_evidence)
        caller_record = _normalized_live_cell(
            fixture.active_qualification, first_evidence, first_result,
        )
        receipt = fixture.controller._observe_qualification_coordinate_terminal(
            fixture.active_qualification,
            first_evidence.terminal_capability,
            caller_record,
        )
        object.__setattr__(caller_record, "oracle_value", "false")
        fixture.controller._publish_qualification_coordinate_terminal(
            fixture.active_qualification, receipt,
        )
        object.__setattr__(receipt, "capability_identity", "sha256:" + "e" * 64)
        assert not fixture.controller.owns_qualification_terminal_receipt(receipt)
        for evidence in fixture.cells[1:]:
            result = qualify_live_cell(fixture.active_qualification, evidence)
            observed = fixture.controller._observe_qualification_coordinate_terminal(
                fixture.active_qualification,
                evidence.terminal_capability,
                _normalized_live_cell(fixture.active_qualification, evidence, result),
            )
            fixture.controller._publish_qualification_coordinate_terminal(
                fixture.active_qualification, observed,
            )
        for evidence in fixture.probes:
            observed = fixture.controller._observe_qualification_coordinate_terminal(
                fixture.active_qualification,
                evidence.terminal_capability,
                _normalized_live_probe(fixture.active_qualification, evidence),
            )
            fixture.controller._publish_qualification_coordinate_terminal(
                fixture.active_qualification, observed,
            )
        attestation = fixture.controller.finalize_qualification_semantics(
            fixture.active_qualification,
        )
        assert attestation.semantic_result == "passed"
    finally:
        fixture.close()


def test_parent_registry_rejects_stale_normalized_record_identity(tmp_path):
    fixture = _qualification(tmp_path, "normalized-stale-identity", publish=False)
    try:
        evidence = fixture.cells[0]
        record = _normalized_live_cell(
            fixture.active_qualification,
            evidence,
            qualify_live_cell(fixture.active_qualification, evidence),
        )
        object.__setattr__(record, "oracle_value", "false")
        with pytest.raises(ProvenanceError, match="final_prerequisite_mismatch"):
            fixture.controller._observe_qualification_coordinate_terminal(
                fixture.active_qualification,
                evidence.terminal_capability,
                record,
            )
    finally:
        fixture.close()


def test_parent_registry_rejects_mutated_coordinate_capability(tmp_path):
    fixture = _qualification(tmp_path, "mutated-capability", publish=False)
    try:
        evidence = fixture.cells[0]
        object.__setattr__(
            evidence.terminal_capability, "nonce", "caller-mutated-nonce",
        )
        record = _normalized_live_cell(
            fixture.active_qualification,
            evidence,
            qualify_live_cell(fixture.active_qualification, evidence),
        )
        with pytest.raises(ProvenanceError, match="authority_replay"):
            fixture.controller._observe_qualification_coordinate_terminal(
                fixture.active_qualification,
                evidence.terminal_capability,
                record,
            )
    finally:
        fixture.close()


def test_object_new_attestation_copy_has_no_parent_ownership(tmp_path):
    fixture = _qualification(tmp_path, "forged-attestation")
    try:
        aggregate = _aggregate(fixture)
        forged = _fabricated_attestation(aggregate.semantic_attestation)
        assert not fixture.controller.owns_qualification_semantic_attestation(forged)
        with pytest.raises(ProvenanceError, match="final_prerequisite_mismatch"):
            FinalExecutionPrerequisites.from_semantic_attestation(
                fixture.active_qualification, forged,
            )
        assert fixture.fence.real_counts == {
            channel: 0 for channel in fixture.fence.real_counts
        }
    finally:
        fixture.close()


def test_582_exact_class_shallow_aggregate_cannot_supply_semantic_authority(tmp_path):
    fixture = _qualification(tmp_path, "issue-582", publish=False)
    try:
        fabricated = object.__new__(LiveQualificationAggregate)
        object.__setattr__(fabricated, "identity", "sha256:" + "a" * 64)
        object.__setattr__(fabricated, "authority", fixture.active_qualification)
        object.__setattr__(fabricated, "authority_binding", fixture.active_qualification.binding)
        object.__setattr__(fabricated, "profile_digest", fixture.preflight.profile_digest)
        object.__setattr__(fabricated, "evidence_origin", "injected_fake")
        with pytest.raises(ProvenanceError, match="final_prerequisite_mismatch"):
            FinalExecutionPrerequisites.from_live_qualification(
                fixture.active_qualification, fabricated,
            )
        with pytest.raises(ProvenanceError, match="final_prerequisite_mismatch"):
            fixture.controller.ledger_terminal(
                fixture.qualification_ledger,
                {
                    "result": "passed",
                    "authority_digest": fixture.active_qualification.identity,
                    "qualification_aggregate_digest": fabricated.identity,
                    "probe_aggregate_digest": "sha256:" + "b" * 64,
                    "evidence_origin": "injected_fake",
                    "terminal_verified": True,
                },
            )
        assert fixture.fence.real_counts == {
            channel: 0 for channel in fixture.fence.real_counts
        }
    finally:
        fixture.close()


def test_descriptive_aggregate_field_drift_cannot_change_parent_decision(tmp_path):
    fixture = _qualification(tmp_path, "descriptive-drift")
    try:
        aggregate = _aggregate(fixture)
        attestation = aggregate.semantic_attestation
        object.__setattr__(aggregate, "identity", "sha256:" + "f" * 64)
        object.__setattr__(aggregate, "profile_digest", "0" * 64)
        object.__setattr__(aggregate, "campaign_id", "caller-mutated")
        prerequisites = FinalExecutionPrerequisites.from_live_qualification(
            fixture.active_qualification, aggregate,
        )
        assert prerequisites.qualification_semantic_attestation is attestation
        assert prerequisites.qualification_semantic_projection_digest \
            == attestation.semantic_projection_digest
        assert prerequisites.profile_digest == attestation.profile_digest
    finally:
        fixture.close()


def test_descriptive_aggregate_must_present_the_attested_census(tmp_path):
    fixture = _qualification(tmp_path, "descriptive-census-drift")
    try:
        cells = list(fixture.cells)
        cells[0] = replace(cells[0], evidence_digest="sha256:" + "d" * 64)
        with pytest.raises(ProvenanceError, match="final_prerequisite_mismatch"):
            aggregate_live_qualification(
                fixture.active_qualification,
                tuple(cells),
                fixture.probes,
                ledger=fixture.qualification_ledger,
            )
    finally:
        fixture.close()


def test_cross_authority_coordinate_capability_is_rejected(tmp_path):
    left = _qualification(tmp_path, "coordinate-left", publish=False)
    right = _qualification(tmp_path, "coordinate-right", publish=False)
    try:
        capability = left.cells[0].terminal_capability
        with pytest.raises(ProvenanceError):
            right.controller._observe_qualification_coordinate_terminal(
                right.active_qualification, capability, object(),
            )
    finally:
        left.close()
        right.close()


def test_terminal_publication_rejects_foreign_qualification_ledger(tmp_path):
    left = _qualification(tmp_path, "ledger-left", publish=False)
    right = _qualification(tmp_path, "ledger-right", publish=False)
    try:
        with pytest.raises(ProvenanceError, match="authority_replay"):
            publish_live_qualification_terminals(
                left.active_qualification,
                left.cells,
                left.probes,
                ledger=right.qualification_ledger,
            )
        assert left.qualification_ledger.state == "active"
        assert right.qualification_ledger.state == "active"
    finally:
        left.close()
        right.close()


def test_manual_coordinate_capability_copy_and_expired_authority_are_rejected(tmp_path):
    fixture = _qualification(tmp_path, "coordinate-copy", publish=False)
    try:
        authentic = fixture.cells[0].terminal_capability
        fabricated = object.__new__(type(authentic))
        for item in fields(type(authentic)):
            object.__setattr__(fabricated, item.name, getattr(authentic, item.name))
        with pytest.raises(ProvenanceError):
            fixture.controller._observe_qualification_coordinate_terminal(
                fixture.active_qualification, fabricated, object(),
            )
        fixture.controller.advance_trusted_time(400)
        with pytest.raises(ProvenanceError):
            fixture.controller._observe_qualification_coordinate_terminal(
                fixture.active_qualification, authentic, object(),
            )
    finally:
        fixture.close()


def test_terminal_registry_requires_exact_parent_observation_receipt(tmp_path):
    fixture = _qualification(tmp_path, "receipt-publication", publish=False)
    try:
        evidence = fixture.cells[0]
        result = qualify_live_cell(fixture.active_qualification, evidence)
        receipt = fixture.controller._observe_qualification_coordinate_terminal(
            fixture.active_qualification,
            evidence.terminal_capability,
            _normalized_live_cell(fixture.active_qualification, evidence, result),
        )
        copied = object.__new__(type(receipt))
        for item in fields(type(receipt)):
            object.__setattr__(copied, item.name, getattr(receipt, item.name))
        with pytest.raises(ProvenanceError, match="authority_replay"):
            fixture.controller._publish_qualification_coordinate_terminal(
                fixture.active_qualification, copied,
            )
        fixture.controller._publish_qualification_coordinate_terminal(
            fixture.active_qualification, receipt,
        )
        with pytest.raises(ProvenanceError, match="authority_replay"):
            fixture.controller._publish_qualification_coordinate_terminal(
                fixture.active_qualification, receipt,
            )
    finally:
        fixture.close()
