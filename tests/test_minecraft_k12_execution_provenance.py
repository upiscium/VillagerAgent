import dataclasses
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.common.eac.canonical import canonical_bytes, canonical_sha256
from benchmarks.minecraft.k12_execution_capsule import CapsuleRecord, DurableLedger, attest_capsule
from benchmarks.minecraft.k12_execution_provenance import (
    EXPECTED_BASE_REF,
    EXPECTED_BASE_SHA,
    EXPECTED_BRANCH,
    EXPECTED_HEAD,
    EXPECTED_PR,
    EXPECTED_REPOSITORY,
    PROFILE_V2,
    INJECTED_FAKE_ORIGIN,
    INJECTED_TEST_ORIGIN,
    RUNTIME_VERIFIED_ORIGIN,
    ActiveFinalAuthority,
    ActiveQualificationAuthority,
    AuthorityBinding,
    FinalExecutionPrerequisites,
    FinalFirstConsumeObservation,
    FirstConsumeObservation,
    K12FinalRunAuthorization,
    K12QualificationRunAuthorization,
    K12RetainedTargetLease,
    ParentExecutionAuthority,
    InjectedTestController,
    ProvenanceError,
    PullRequestObservation,
    QualificationExecutionAuthority,
    QualificationPreflight,
    TargetLockObservation,
    EnvironmentObservation,
    CheckoutObservation,
    OutputRootObservation,
    authority_owns_profile,
    durable_ledger_root_digest,
    git_blob_oid,
    retained_target_binding,
    source_closure_from_observations,
    verify_final_first_consume,
    verify_qualification_first_consume,
)
from benchmarks.minecraft.k12_guarded_backend import authority_binding_is_current
from benchmarks.minecraft.k12_runtime_profile import (
    load_k12_live_runtime_profile,
    load_k12_live_source_policy,
)
from benchmarks.minecraft.run_lock import MinecraftTargetLock


D = "b" * 64
PROFILE = load_k12_live_runtime_profile()
POLICY = load_k12_live_source_policy()
SOURCE_ROOT = Path(__file__).resolve().parents[1]


def _source(parent):
    return parent.collect_source_closure(
        root=SOURCE_ROOT,
        policy=POLICY,
        injected_only=True,
    )


def _capsule(parent, source):
    records = tuple([
        CapsuleRecord("repo", source.aggregate_sha256, "repo"),
        CapsuleRecord("interpreter", D, "interpreter"),
        CapsuleRecord("stdlib", D, "stdlib"),
        CapsuleRecord("import-roots", D, "import_roots"),
        CapsuleRecord("distributions", D, "distributions"),
        CapsuleRecord("native", D, "native"),
        CapsuleRecord("startup", D, "startup"),
        CapsuleRecord("node", D, "node"),
        CapsuleRecord("java", D, "java"),
    ])
    return parent.attest_execution_capsule(
        identity="minecraft-k12-live-execution-capsule/1",
        source_aggregate=source.aggregate_sha256,
        immutable_store_path_digest=D,
        recursive_store_closure_digest=D,
        interpreter_digest=D,
        import_roots_digest=D,
        records=records,
        immutable=True,
        read_only=True,
        outside_worktree=True,
        user_site_enabled=False,
        editable_installs=False,
        unapproved_pth=False,
        sitecustomize=False,
        startup_hooks=False,
        writable_worktree_imports=False,
        node_required=True,
        java_required=True,
    )


def _checkout():
    return CheckoutObservation(
        repository_identity=EXPECTED_REPOSITORY,
        worktree_identity=D,
        git_dir_identity=D,
        common_dir_identity=D,
        symbolic_head_ref=EXPECTED_BRANCH,
        head_commit=EXPECTED_HEAD,
        head_tree="c" * 40,
        index_tree="c" * 40,
        upstream_ref=EXPECTED_BRANCH,
        upstream_commit=EXPECTED_HEAD,
        remote_repository=EXPECTED_REPOSITORY,
        remote_ref=EXPECTED_BRANCH,
        remote_commit=EXPECTED_HEAD,
        staged_clean=True,
        tracked_clean=True,
        untracked_clean=True,
        generated_artifacts_absent=True,
    )


def _preflight(parent, ledger):
    source = _source(parent)
    lock = MinecraftTargetLock(
        lock_root=ledger.root.parent / f"provenance-lock-{ledger.reservation_id[:8]}",
        host="127.0.0.1",
        port=25575,
        world_id="world/1",
        attempt_id=ledger.reservation_id,
    ).acquire()
    lease = parent.retain_target_lease(lock, ledger.reservation_id)
    retained = retained_target_binding(lease)
    run_auth = parent.mint_qualification_run_authorization(
        reservation_id=ledger.reservation_id,
        output_root_identity=ledger.output_root_identity,
        profile_digest=PROFILE.profile_digest,
        ledger=ledger,
        now=100,
    )
    return QualificationPreflight(
        reservation_id=ledger.reservation_id,
        run_authorization_digest=run_auth.identity,
        run_authorization=run_auth,
        expected_head=EXPECTED_HEAD,
        checkout=_checkout(),
        pull_request=PullRequestObservation(
            EXPECTED_REPOSITORY, EXPECTED_PR, "OPEN", True,
            EXPECTED_REPOSITORY, EXPECTED_BRANCH.removeprefix("refs/heads/"), EXPECTED_HEAD,
            EXPECTED_BASE_REF, EXPECTED_BASE_SHA, 100, "observer/1", D,
        ),
        source=source,
        capsule=_capsule(parent, source),
        profile_identity=PROFILE.profile_id,
        authenticated_source_policy=POLICY,
        authenticated_profile=PROFILE,
        profile_digest=PROFILE.profile_digest,
        contracts=dict(PROFILE["contract_digests"]),
        schedule_digest=hashlib.sha256(canonical_bytes(list(PROFILE["schedule"]))).hexdigest(),
        environment=EnvironmentObservation(
            PROFILE["environment_policy_identity"], PROFILE["environment_policy_digest"],
            "CPython", "3.10.19", D, D, PROFILE["node_identity"], PROFILE["java_identity"],
            PROFILE["bridge_content_sha256"], PROFILE["server_jar_sha256"],
            PROFILE["model_identity"], PROFILE["endpoint_hash"], "unsupported", "en_us",
            D, D, D,
        ),
        target=TargetLockObservation(
            "target/1", retained["host_hash"], retained["port"],
            PROFILE["rcon_identity"], PROFILE["server_identity"],
            "1.19.2", PROFILE["data_identity"], "world/1", tuple(PROFILE["actors"]),
            PROFILE["region"]["name"], retained["lock_schema"], retained["lock_key"],
            ledger.reservation_id, retained["lock_object_identity"], retained["fd_open"],
            retained["regular_file"], retained["device_inode_digest"],
            retained["owner_metadata_digest"], retained["continuously_owned"],
        ),
        output=OutputRootObservation(
            ledger.output_root_identity, D, D, "fresh/1", True, True, False,
        ),
        ledger_identity=ledger.identity,
        ledger_root_digest=durable_ledger_root_digest(ledger),
        target_lease=lease,
    )


def _first(preflight):
    return FirstConsumeObservation(
        preflight.checkout,
        preflight.pull_request,
        preflight.source,
        preflight.capsule,
        preflight.environment,
        preflight.target,
        preflight.output,
        target_lease=preflight.target_lease,
    )


def test_issue_524_constants_and_canonical_head_are_frozen():
    assert EXPECTED_REPOSITORY == "upiscium/VillagerAgent"
    assert EXPECTED_BRANCH == "refs/heads/experiment/k11-k12-ecological-validation"
    assert EXPECTED_PR == 524
    assert EXPECTED_BASE_REF == "main"
    assert EXPECTED_BASE_SHA == "66a904de8af2b0bbaf79071628f06bed91a40078"
    assert EXPECTED_HEAD.startswith("36a1453") and len(EXPECTED_HEAD) == 40


def test_runtime_source_collector_authenticates_checkout_git_tree():
    parent = ParentExecutionAuthority()
    with pytest.raises(ProvenanceError, match="git_tree_mismatch"):
        parent.collect_source_closure(
            root=SOURCE_ROOT, policy=POLICY, checkout=_checkout(),
        )
    with pytest.raises(TypeError):
        parent.collect_source_closure(
            root=SOURCE_ROOT, policy=POLICY, expected_git_tree={},
        )


def test_source_observation_dto_cannot_supply_authority_evidence():
    with pytest.raises(TypeError, match="parent-side source collector"):
        source_closure_from_observations(policy=POLICY, observations={})


def test_injected_only_source_collector_reads_missing_tree_paths_but_is_non_runtime(tmp_path):
    missing_path = POLICY.paths[0]
    injected = ParentExecutionAuthority().injected_test_controller()
    source = injected.collect_source_closure(
        root=SOURCE_ROOT,
        policy=POLICY,
        injected_only=True,
    )
    record = next(item for item in source.records if item.path == missing_path)
    data = (SOURCE_ROOT / missing_path).read_bytes()
    assert record.git_blob_oid == git_blob_oid(data)
    assert source.origin == INJECTED_TEST_ORIGIN
    assert not source.runtime_admissible

    linked_root = tmp_path / "source-link"
    linked_root.symlink_to(SOURCE_ROOT, target_is_directory=True)
    with pytest.raises(ProvenanceError, match="source_closure_incomplete"):
        injected.collect_source_closure(
            root=linked_root, policy=POLICY,
            injected_only=True,
        )


def test_injected_trusted_clock_expires_active_authority_at_guard_boundary(tmp_path):
    controller = ParentExecutionAuthority().injected_test_controller()
    root = tmp_path / "qualification"
    root.mkdir(mode=0o700)
    ledger = controller.create_ledger(
        root=root,
        namespace="qualification",
        reservation_id="7" * 64,
        output_root_identity="qualification-output",
    )
    try:
        preflight = _preflight(controller, ledger)
        authority = controller.mint_qualification(preflight, now=101, ledger=ledger)
        active = controller.verify_qualification_first_consume(
            authority, _first(preflight), now=102, ledger=ledger,
        )
        assert controller.current_time() == 102
        assert controller.validate_current_authority(active) is True
        assert authority_binding_is_current(
            active,
            active.binding,
            profile_digest=PROFILE.profile_digest,
            allow_injected=True,
        ) is True

        expires_at = authority.body["expires_at"]
        controller.advance_trusted_time(expires_at - controller.current_time() + 1)
        assert controller.current_time() > expires_at
        assert active.current_at() is False
        assert controller.validate_current_authority(active) is False
        assert authority_binding_is_current(
            active,
            active.binding,
            profile_digest=PROFILE.profile_digest,
            allow_injected=True,
        ) is False
    finally:
        preflight.target_lease.lock.release()
        ledger.close()


def test_parent_mints_typed_run_authorization_and_active_qualification(tmp_path):
    parent = ParentExecutionAuthority().injected_test_controller()
    root = tmp_path / "qualification"
    root.mkdir(mode=0o700)
    ledger = parent.create_ledger(
        root=root,
        namespace="qualification",
        reservation_id="1" * 64,
        output_root_identity="qualification-output",
    )
    try:
        preflight = _preflight(parent, ledger)
        authority = parent.mint_qualification(preflight, now=101, ledger=ledger)
        assert isinstance(authority.run_authorization, K12QualificationRunAuthorization)
        active = verify_qualification_first_consume(authority, _first(preflight), now=102, ledger=ledger)
        assert isinstance(active, ActiveQualificationAuthority)
        assert ledger.state == "active"
        assert active.origin == INJECTED_TEST_ORIGIN
        assert not active.runtime_admissible
        assert active.binding.origin == INJECTED_TEST_ORIGIN
        assert not active.binding.runtime_admissible
        assert authority_owns_profile(
            active,
            active.binding,
            profile_id=PROFILE_V2,
            profile_digest=PROFILE.profile_digest,
        )
    finally:
        preflight.target_lease.lock.release()
        ledger.close()


def test_foreign_parent_capsule_cannot_authorize_qualification(tmp_path):
    parent = ParentExecutionAuthority().injected_test_controller()
    foreign = ParentExecutionAuthority().injected_test_controller()
    root = tmp_path / "foreign-capsule"
    root.mkdir(mode=0o700)
    ledger = parent.create_ledger(
        root=root,
        namespace="qualification",
        reservation_id="f" * 64,
        output_root_identity="qualification-output",
    )
    preflight = _preflight(parent, ledger)
    try:
        capsule_values = preflight.capsule.unsigned()
        capsule_values["records"] = preflight.capsule.records
        foreign_capsule = foreign.attest_execution_capsule(**capsule_values)
        with pytest.raises(ProvenanceError, match="capsule_mismatch"):
            parent.mint_qualification(
                dataclasses.replace(preflight, capsule=foreign_capsule),
                now=101,
                ledger=ledger,
            )
        assert ledger.state == "quarantined"
    finally:
        preflight.target_lease.lock.release()
        ledger.close()


def test_authorization_constructors_are_parent_owned_and_capabilities_are_distinct():
    with pytest.raises(TypeError):
        K12QualificationRunAuthorization({}, object())
    with pytest.raises(TypeError):
        K12FinalRunAuthorization({}, object())
    assert not AuthorityBinding.mock_only().authority_type == "minecraft-k12-live-final-execution-authority/1"


@pytest.mark.parametrize(
    "alias",
    ("live_qualification", "qualification_probe", "live_final", "test_only"),
)
def test_namespace_and_legacy_test_aliases_cannot_become_authorization_origins(alias):
    with pytest.raises((ProvenanceError, TypeError)):
        ParentExecutionAuthority(_origin=alias)


def test_final_prerequisites_require_nominal_live_aggregate_and_receipts(tmp_path):
    parent = ParentExecutionAuthority().injected_test_controller()
    root = tmp_path / "qualification"
    root.mkdir(mode=0o700)
    ledger = parent.create_ledger(
        root=root, namespace="qualification", reservation_id="2" * 64,
        output_root_identity="qualification-output",
    )
    try:
        preflight = _preflight(parent, ledger)
        authority = parent.mint_qualification(preflight, now=101, ledger=ledger)
        active = verify_qualification_first_consume(
            authority, _first(preflight), now=102, ledger=ledger,
        )
        lookalike = SimpleNamespace(
            execution_provenance="live_qualification",
            evidence_origin=INJECTED_FAKE_ORIGIN,
            profile_digest=PROFILE.profile_digest,
            authority_binding=active.binding,
            qualification_aggregate_digest="sha256:" + "a" * 64,
            probe_aggregate_digest="sha256:" + "b" * 64,
            qualification_terminal_ledger_digest="sha256:" + "c" * 64,
            passed=True,
        )
        lookalike.qualifies = lambda: True
        with pytest.raises(ProvenanceError, match="final_prerequisite_mismatch"):
            FinalExecutionPrerequisites.from_live_qualification(active, lookalike)
    finally:
        preflight.target_lease.lock.release()
        ledger.close()


def test_injected_fake_lookalike_cannot_cross_nominal_boundary(tmp_path):
    injected = ParentExecutionAuthority().injected_test_controller()
    root = tmp_path / "qualification"
    root.mkdir(mode=0o700)
    ledger = injected.create_ledger(
        root=root, namespace="qualification", reservation_id="6" * 64,
        output_root_identity="qualification-output",
    )
    try:
        preflight = _preflight(injected, ledger)
        authority = injected.mint_qualification(preflight, now=101, ledger=ledger)
        active = injected.verify_qualification_first_consume(
            authority, _first(preflight), now=102, ledger=ledger,
        )
        lookalike = SimpleNamespace(
            execution_provenance="live_qualification",
            evidence_origin=INJECTED_FAKE_ORIGIN,
            profile_digest=PROFILE.profile_digest,
            authority_binding=active.binding,
            passed=True,
        )
        lookalike.qualifies = lambda: True
        with pytest.raises(ProvenanceError, match="final_prerequisite_mismatch"):
            FinalExecutionPrerequisites.from_live_qualification(active, lookalike)
    finally:
        preflight.target_lease.lock.release()
        ledger.close()


def test_test_only_evidence_cannot_be_used_for_final_admission(tmp_path):
    parent = ParentExecutionAuthority().injected_test_controller()
    root = tmp_path / "qualification"
    root.mkdir(mode=0o700)
    ledger = parent.create_ledger(
        root=root, namespace="qualification", reservation_id="4" * 64,
        output_root_identity="qualification-output",
    )
    try:
        preflight = _preflight(parent, ledger)
        authority = parent.mint_qualification(preflight, now=101, ledger=ledger)
        active = verify_qualification_first_consume(
            authority, _first(preflight), now=102, ledger=ledger,
        )
        # A mock/test origin is intentionally not a live qualification
        # evidence object even when its shape is otherwise aggregate-like.
        evidence = SimpleNamespace(
            execution_provenance="mock_only",
            evidence_origin="test_only",
            profile_digest=PROFILE.profile_digest,
            authority_binding=active.binding,
            qualification_aggregate_digest="sha256:" + "a" * 64,
            probe_aggregate_digest="sha256:" + "b" * 64,
            qualification_terminal_ledger_digest="sha256:" + "c" * 64,
            passed=True,
        )
        evidence.qualifies = lambda: True
        with pytest.raises(ProvenanceError, match="final_prerequisite_mismatch"):
            FinalExecutionPrerequisites.from_live_qualification(active, evidence)
    finally:
        preflight.target_lease.lock.release()
        ledger.close()


def test_retained_target_lease_uses_only_public_snapshot_and_revalidates(tmp_path):
    lock = MinecraftTargetLock(
        lock_root=tmp_path / "locks",
        host="127.0.0.1",
        port=25575,
        world_id="world/1",
        attempt_id="lease-attempt",
    ).acquire()
    parent = ParentExecutionAuthority().injected_test_controller()
    try:
        lease = parent.retain_target_lease(lock, reservation_id="lease-attempt")
        assert isinstance(lease, K12RetainedTargetLease)
        assert lease.evidence["attempt_id"] == "lease-attempt"
        assert lease.revalidate() == lease.evidence
        with pytest.raises(TypeError):
            K12RetainedTargetLease(lock, "lease-attempt")
    finally:
        lock.release()


def test_first_consume_revalidates_pr_semantics_and_quarantines(tmp_path):
    parent = ParentExecutionAuthority().injected_test_controller()
    root = tmp_path / "qualification"
    root.mkdir(mode=0o700)
    ledger = parent.create_ledger(
        root=root, namespace="qualification", reservation_id="5" * 64,
        output_root_identity="qualification-output",
    )
    try:
        preflight = _preflight(parent, ledger)
        authority = parent.mint_qualification(preflight, now=101, ledger=ledger)
        changed = dataclasses.replace(preflight.pull_request, head_sha="e" * 40)
        with pytest.raises(ProvenanceError, match="pr_semantic_mismatch"):
            verify_qualification_first_consume(
                authority,
                dataclasses.replace(_first(preflight), pull_request=changed),
                now=102,
                ledger=ledger,
            )
        assert ledger.state == "quarantined"
    finally:
        preflight.target_lease.lock.release()
        ledger.close()
