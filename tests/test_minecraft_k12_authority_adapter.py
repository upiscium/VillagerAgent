from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.common.eac import PermitLifecycle
from benchmarks.minecraft.eac_runtime import MinecraftEACRuntime
from benchmarks.minecraft.k12_authority_adapter import (
    ControlledEACAdapter,
    K12AuthorityAdapterError,
    project_stale_rejection,
)
from benchmarks.minecraft.k12_identity import (
    AUTHORITY_REJECTION_SCHEMA,
    REQUEST_CONTENT_SCHEMA,
    evidence_root_digest,
)
from benchmarks.minecraft.k12_fixture import K12_ACTIONS


@pytest.fixture()
def evidence():
    return ControlledEACAdapter(run_id="k12-gate557-test").collect()


def test_initial_request_is_admissible_and_exact_identity_is_retained(evidence):
    assert evidence.evaluation_before.admissible is True
    assert evidence.prepared.request is evidence.retained_request
    assert evidence.prepared.permit is evidence.retained_permit
    assert evidence.prepared.gateway is evidence.retained_gateway


def test_visible_supersession_makes_current_eadm_false(evidence):
    assert evidence.superseding_root.proposition.polarity is False
    assert evidence.superseding_root.supersedes == (evidence.evidence_root.root_id,)
    assert evidence.superseding_root.revision > evidence.evidence_root.revision
    assert evidence.evaluation_after.admissible is False


def test_stale_permit_gateway_rejection_has_no_attempt_or_native_entry(evidence):
    result = project_stale_rejection(evidence)
    assert evidence.permit_before.lifecycle is PermitLifecycle.ISSUED
    assert evidence.permit_after.lifecycle is PermitLifecycle.STALE
    assert evidence.gateway_reason == "stale"
    assert result.rejection_stage == "permit_validate"
    assert result.outcome_certainty == "no_effect"
    assert result.retry_safe is False
    assert result.original_attempt_absent is True
    assert result.native_entry_count == 0


def test_projection_binds_every_required_public_identity(evidence):
    result = project_stale_rejection(evidence)
    request = evidence.retained_request
    assert result.schema_version == AUTHORITY_REJECTION_SCHEMA
    assert request.candidate_id in result.request_identity
    assert request.attempt_id in result.request_identity
    assert result.candidate_id == request.candidate_id
    assert result.attempt_id == request.attempt_id
    assert (result.action_identity, result.action_version, result.action_digest) == (
        request.action.identity, request.action.version, request.action.digest,
    )
    assert result.permit_id == evidence.permit_after.permit_id
    assert result.permit_fingerprint == evidence.permit_after.fingerprint
    assert result.evidence_root_id == evidence.evidence_root.root_id
    assert result.superseding_root_id == evidence.superseding_root.root_id
    assert result.request_content_schema == REQUEST_CONTENT_SCHEMA
    assert result.request_content_scientific is True


def test_projection_digest_is_deterministic_for_identical_evidence(evidence):
    first = project_stale_rejection(evidence)
    second = project_stale_rejection(evidence)
    assert first.projection_digest == second.projection_digest
    assert first.verify_digest()


def test_projection_is_deeply_immutable_at_its_public_boundary(evidence):
    result = project_stale_rejection(evidence)
    with pytest.raises(FrozenInstanceError):
        result.rejection_reason = "mismatch"
    assert isinstance(result.evaluation_reasons, tuple)
    assert isinstance(result.supersedes, tuple)


def test_wrong_rejection_reason_fails_closed(evidence):
    with pytest.raises(K12AuthorityAdapterError, match="reason"):
        project_stale_rejection(replace(evidence, gateway_reason="mismatch"))


def test_permit_not_stale_fails_closed(evidence):
    permit = replace(evidence.permit_after, lifecycle=PermitLifecycle.ISSUED)
    with pytest.raises(K12AuthorityAdapterError, match="lifecycle"):
        project_stale_rejection(replace(evidence, permit_after=permit))


def test_current_evaluation_still_admissible_fails_closed(evidence):
    evaluation = replace(evidence.evaluation_after, admissible=True)
    with pytest.raises(K12AuthorityAdapterError, match="still admissible"):
        project_stale_rejection(replace(evidence, evaluation_after=evaluation))


def test_preexisting_or_created_original_attempt_fails_closed(evidence):
    attempt = SimpleNamespace(attempt_id=evidence.retained_request.attempt_id)
    for field in ("attempts_before", "attempts_after"):
        with pytest.raises(K12AuthorityAdapterError, match="AttemptRecord"):
            project_stale_rejection(replace(evidence, **{field: (attempt,)}))


def test_altered_request_candidate_or_attempt_identity_fails_closed(evidence):
    request = evidence.retained_request
    for field, value in (("candidate_id", "changed-candidate"), ("attempt_id", "changed-attempt")):
        altered = replace(request, **{field: value})
        with pytest.raises(K12AuthorityAdapterError, match="identity"):
            project_stale_rejection(replace(evidence, retained_request=altered))


def test_altered_retained_permit_or_gateway_identity_fails_closed(evidence):
    with pytest.raises(K12AuthorityAdapterError, match="identity"):
        project_stale_rejection(replace(evidence, retained_permit=evidence.permit_after))
    with pytest.raises(K12AuthorityAdapterError, match="identity"):
        project_stale_rejection(replace(evidence, retained_gateway=object()))


def test_nonopposite_or_nonmonotonic_supersession_fails_closed(evidence):
    same = replace(evidence.superseding_root, proposition=evidence.evidence_root.proposition)
    with pytest.raises(K12AuthorityAdapterError, match="supersession"):
        project_stale_rejection(replace(evidence, superseding_root=same))
    old_revision = replace(evidence.superseding_root, revision=evidence.evidence_root.revision)
    with pytest.raises(K12AuthorityAdapterError, match="supersession"):
        project_stale_rejection(replace(evidence, superseding_root=old_revision))


def test_runtime_evaluation_and_actor_splicing_fail_closed(evidence):
    advisory = MinecraftEACRuntime(
        mode="dual_dag_advisory", run_id="spliced-advisory",
        env_prechecks={"MineBlock": lambda unused: True},
    )
    with pytest.raises(K12AuthorityAdapterError, match="authority runtime"):
        project_stale_rejection(replace(evidence, runtime=advisory))
    wrong_policy = replace(evidence.evaluation_after, policy=None)
    with pytest.raises(K12AuthorityAdapterError, match="semantic binding"):
        project_stale_rejection(replace(evidence, evaluation_after=wrong_policy))
    wrong_actor = replace(evidence.evidence_root, visible_to=("Bob",))
    with pytest.raises(K12AuthorityAdapterError, match="supersession"):
        project_stale_rejection(replace(evidence, evidence_root=wrong_actor))


def test_unrelated_evidence_pair_is_not_accepted(evidence):
    old = replace(evidence.evidence_root, root_id="minecraft-root:other-run:1")
    new = replace(evidence.superseding_root, supersedes=(old.root_id,))
    with pytest.raises(K12AuthorityAdapterError, match="supersession"):
        project_stale_rejection(replace(evidence, evidence_root=old, superseding_root=new))


def test_same_id_superseding_metadata_tampering_fails_closed(evidence):
    for field, value in (
        ("revision", 99),
        ("provenance_id", "forged"),
        ("source_lineage_id", "forged"),
        ("issuer", "forged"),
    ):
        forged = replace(evidence.superseding_root, **{field: value})
        with pytest.raises(K12AuthorityAdapterError, match="supersession|runtime projection"):
            project_stale_rejection(replace(evidence, superseding_root=forged))
    forged_revision = replace(
        evidence.superseding_root, revision=99, source_stream_revision=99,
    )
    with pytest.raises(K12AuthorityAdapterError, match="commitment"):
        project_stale_rejection(replace(evidence, superseding_root=forged_revision))
    forged_commitment = evidence_root_digest(forged_revision)
    with pytest.raises(K12AuthorityAdapterError, match="collector-anchored"):
        project_stale_rejection(replace(
            evidence,
            superseding_root=forged_revision,
            superseding_root_commitment=forged_commitment,
        ))


def test_native_entry_is_never_accepted(evidence):
    with pytest.raises(K12AuthorityAdapterError, match="native"):
        project_stale_rejection(replace(evidence, native_entry_count=1))


def test_unchanged_advisory_path_remains_available():
    baseline = ControlledEACAdapter.advisory_baseline()
    assert baseline.admissible is True
    assert baseline.native_entry_count == 1
    assert baseline.would_block is False


def test_adapter_source_does_not_use_forbidden_private_authority_state():
    source = Path("benchmarks/minecraft/k12_authority_adapter.py").read_text(encoding="utf-8")
    for name in ("._candidates", "._permits", "._tokens", "._audit"):
        assert name not in source


@pytest.mark.parametrize("action", K12_ACTIONS)
def test_authority_recovery_is_semantically_new_for_each_k12_action(action):
    calls = []

    def native(**kwargs):
        calls.append(kwargs)
        return {"status": True, "action": action}

    evidence = ControlledEACAdapter(
        run_id=f"k12-recovery-{action}", action=action, native_callback=native,
    ).collect_authority_recovery()
    assert evidence.original_request != evidence.recovery_request
    assert evidence.original_request_digest != evidence.recovery_request_digest
    assert evidence.original_content_digest != evidence.recovery_content_digest
    assert evidence.recovery_evaluation.admissible is True
    assert evidence.recovery_permit.lifecycle is PermitLifecycle.ISSUED
    assert evidence.recovery_attempt.attempt_id == evidence.recovery_request.attempt_id
    assert evidence.native_entry_count == len(calls) == 1
    assert evidence.native_result["status"] is True


def test_recovery_preparation_never_enters_native_effect():
    calls = []
    adapter = ControlledEACAdapter(
        action="MineBlock", native_callback=lambda **kwargs: calls.append(kwargs) or {"status": True},
    )
    prepared = adapter.prepare_authority_recovery()
    assert calls == []
    evidence = adapter.execute_authority_recovery(prepared)
    assert len(calls) == evidence.native_entry_count == 1


def test_parent_can_stage_exact_recovery_request_before_permit_issuance():
    adapter = ControlledEACAdapter(action="MineBlock")
    stale = adapter.collect()
    original_permit = stale.runtime._last_permit["MineBlock"].permit_id
    staged = adapter.stage_recovery(stale, project_stale_rejection(stale))
    assert staged.runtime._last_permit["MineBlock"].permit_id == original_permit
    prepared = adapter.issue_recovery(staged)
    assert prepared.prepared.request == staged.preview_request
    assert prepared.prepared.permit.permit_id == staged.runtime._last_permit["MineBlock"].permit_id
    assert prepared.prepared.permit.permit_id != original_permit


def test_semantically_repeated_recovery_is_rejected_before_permit_or_effect():
    calls = []
    original = {"x": 1, "y": 2, "z": 3, "emotion": [], "murmur": ""}
    adapter = ControlledEACAdapter(
        action="MineBlock", tool_kwargs=original, alternative_tool_kwargs=original,
        native_callback=lambda **kwargs: calls.append(kwargs),
    )
    stale = adapter.collect()
    with pytest.raises(K12AuthorityAdapterError, match="repeats rejected semantic content"):
        adapter.prepare_recovery(stale, project_stale_rejection(stale))
    assert calls == []


@pytest.mark.parametrize("action", K12_ACTIONS)
def test_advisory_invalidation_keeps_gateway_execution_and_records_would_block(action):
    calls = []

    def native(**kwargs):
        calls.append(kwargs)
        return {"status": True}

    evidence = ControlledEACAdapter(
        run_id=f"k12-advisory-{action}", action=action, native_callback=native,
    ).collect_advisory()
    assert evidence.prepared.request is evidence.original_request
    assert evidence.would_block is True
    assert evidence.attempt.attempt_id == evidence.original_request.attempt_id
    assert evidence.native_entry_count == len(calls) == 1
