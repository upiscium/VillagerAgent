import dataclasses
import hashlib
import hmac
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.common.eac.canonical import canonical_bytes, canonical_sha256
from benchmarks.minecraft.k12_execution_capsule import (
    CapsuleRecord, DurableLedger, attest_capsule,
)
from benchmarks.minecraft.k12_execution_provenance import (
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
    ExternalRevisionAuthorization,
    external_revision_attestation_payload,
    authority_owns_profile,
    durable_ledger_root_digest,
    git_blob_oid,
    retained_target_binding,
    verify_first_consume,
    source_closure_from_observations,
    verify_final_first_consume,
    verify_qualification_first_consume,
    _quarantine_retained_target,
)
from benchmarks.minecraft.k12_guarded_backend import authority_binding_is_current
from benchmarks.minecraft.k12_runtime_profile import (
    K12RuntimeProfileError,
    load_k12_live_runtime_profile,
    load_k12_live_source_policy,
)
from benchmarks.minecraft.run_lock import MinecraftTargetLock


D = "b" * 64
REPOSITORY = "example/VillagerAgent"
BRANCH = "refs/heads/experiment/k12-arbitrary"
HEAD = "a" * 40
TREE = "c" * 40
BASE_REF = "main"
BASE_SHA = "d" * 40
PR_NUMBER = 580
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
        repository_identity=REPOSITORY,
        worktree_identity=D,
        git_dir_identity=D,
        common_dir_identity=D,
        symbolic_head_ref=BRANCH,
        head_commit=HEAD,
        head_tree=TREE,
        index_tree=TREE,
        upstream_ref=BRANCH,
        upstream_commit=HEAD,
        remote_repository=REPOSITORY,
        remote_ref=BRANCH,
        remote_commit=HEAD,
        staged_clean=True,
        tracked_clean=True,
        untracked_clean=True,
        generated_artifacts_absent=True,
    )


def _preflight(parent, ledger, *, revision_verifier_key=None):
    checkout = _checkout()
    now = 100 if parent.origin == INJECTED_TEST_ORIGIN else parent.current_time()
    pull_request = PullRequestObservation(
        REPOSITORY, PR_NUMBER, "OPEN", True,
        REPOSITORY, BRANCH.removeprefix("refs/heads/"), HEAD,
        BASE_REF, BASE_SHA, now, "observer/1", D,
    )
    revision_kwargs = {
        "verifier_identity": "test-verifier/1",
        "now": now,
        "expires_at": now + 300,
    }
    if parent.origin == RUNTIME_VERIFIED_ORIGIN:
        if revision_verifier_key is None:
            raise TypeError("runtime test verifier key required")
        payload = external_revision_attestation_payload(
            checkout, pull_request, expires_at=now + 300,
            origin=RUNTIME_VERIFIED_ORIGIN,
            verifier_identity="test-verifier/1",
        )
        revision_kwargs["verifier_receipt_digest"] = hmac.new(
            revision_verifier_key, payload, hashlib.sha256,
        ).hexdigest()
    revision = parent.mint_external_revision_authorization(
        checkout, pull_request, **revision_kwargs,
    )
    source_kwargs = {
        "root": SOURCE_ROOT,
        "policy": POLICY,
        "injected_only": True,
    }
    if parent.origin == INJECTED_TEST_ORIGIN:
        source_kwargs["revision_authorization"] = revision
    source = parent.collect_source_closure(**source_kwargs)
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
        now=now,
        external_revision_authorization=revision,
    )
    return QualificationPreflight(
        reservation_id=ledger.reservation_id,
        run_authorization_digest=run_auth.identity,
        run_authorization=run_auth,
        external_revision_authorization=revision,
        checkout=checkout,
        pull_request=pull_request,
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


def test_external_revision_authorization_is_parent_owned_and_dynamic():
    parent = ParentExecutionAuthority().injected_test_controller()
    revision = parent.mint_external_revision_authorization(
        _checkout(),
        PullRequestObservation(
            REPOSITORY, PR_NUMBER, "OPEN", True, REPOSITORY,
            BRANCH.removeprefix("refs/heads/"), HEAD, BASE_REF, BASE_SHA,
            100, "observer/1", D,
        ),
        verifier_identity="test-verifier/1", now=100,
    )
    assert revision.head_commit == HEAD
    assert revision.base_sha == BASE_SHA
    assert revision.owned_by(parent)
    assert revision.runtime_admissible is False
    with pytest.raises(TypeError):
        type(revision)(**revision.canonical(), ownership_token=object(), token=object())


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


def test_source_collector_rejects_rehashed_policy_object():
    injected = ParentExecutionAuthority().injected_test_controller()
    forged_policy = dataclasses.replace(POLICY, digest="f" * 64)
    with pytest.raises(ProvenanceError, match="source_closure_incomplete"):
        injected.collect_source_closure(
            root=SOURCE_ROOT, policy=forged_policy, injected_only=True,
        )


def test_profile_and_policy_loaders_bind_to_the_supplied_source_root(tmp_path):
    profile = load_k12_live_runtime_profile(
        SOURCE_ROOT / "benchmarks/minecraft/k12_live_runtime_profile_v2.json",
        source_root=SOURCE_ROOT,
    )
    policy = load_k12_live_source_policy(
        SOURCE_ROOT / "configs/minecraft/k12-live-source-closure-policy-v2.json",
        source_root=SOURCE_ROOT,
    )
    assert profile.values == PROFILE.values
    assert policy == POLICY
    with pytest.raises(K12RuntimeProfileError, match="outside the source root"):
        load_k12_live_runtime_profile(
            SOURCE_ROOT / "benchmarks/minecraft/k12_live_runtime_profile_v2.json",
            source_root=tmp_path,
        )
    with pytest.raises(K12RuntimeProfileError):
        load_k12_live_source_policy(
            SOURCE_ROOT / "configs/minecraft/k12-live-source-closure-policy-v2.json",
            source_root=tmp_path,
        )


def test_durable_ledger_fails_closed_after_post_append_storage_failure(tmp_path, monkeypatch):
    root = tmp_path / "post-append-ledger"
    root.mkdir(mode=0o700)
    key = b"l" * 32
    ledger = DurableLedger.create_parent_owned(
        controller_key=key,
        root=root,
        namespace="qualification",
        reservation_id="9" * 64,
        output_root_identity="qualification-output",
    )
    try:
        controller = ledger.acquire_parent_controller(key)
        authority_digest = "sha256:" + "a" * 64
        observation_digest = "sha256:" + "b" * 64
        controller.authority_minted(authority_digest)
        controller.first_consume_verified(authority_digest, observation_digest)
        controller.activate(authority_digest)
        original_append = ledger._storage._append_bytes_locked

        def append_then_fail(data):
            original_append(data)
            raise OSError("post-append acknowledgement failure")

        monkeypatch.setattr(ledger._storage, "_append_bytes_locked", append_then_fail)
        payload = {"result": "failed", "phase": "qualification"}
        with pytest.raises(OSError, match="post-append acknowledgement failure"):
            controller.terminal(payload)
        assert not ledger.verify_chain()
    finally:
        ledger.close()


def test_qualification_preflight_rejects_replaced_runtime_profile(tmp_path):
    controller = ParentExecutionAuthority().injected_test_controller()
    root = tmp_path / "forged-profile"
    root.mkdir(mode=0o700)
    ledger = controller.create_ledger(
        root=root,
        namespace="qualification",
        reservation_id="8" * 64,
        output_root_identity="qualification-output",
    )
    preflight = _preflight(controller, ledger)
    try:
        forged_values = tuple(
            (
                key,
                "f" * 64 if key == "detached_artifact_sha256" else value,
            )
            for key, value in PROFILE.values
        )
        forged_profile = object.__new__(type(PROFILE))
        object.__setattr__(forged_profile, "values", forged_values)
        forged_auth = controller.mint_qualification_run_authorization(
            reservation_id=ledger.reservation_id,
            output_root_identity=ledger.output_root_identity,
            profile_digest="f" * 64,
            ledger=ledger,
            now=100,
            external_revision_authorization=preflight.external_revision_authorization,
        )
        forged_preflight = dataclasses.replace(
            preflight,
            authenticated_profile=forged_profile,
            profile_digest="f" * 64,
            run_authorization=forged_auth,
            run_authorization_digest=forged_auth.identity,
        )
        with pytest.raises(ProvenanceError, match="profile_mismatch"):
            controller.mint_qualification(forged_preflight, now=101, ledger=ledger)
        assert ledger.state == "quarantined"
    finally:
        preflight.target_lease.lock.release()
        ledger.close()


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


def test_first_consume_clock_rollback_quarantines_bound_resources(tmp_path):
    parent = ParentExecutionAuthority().injected_test_controller()
    root = tmp_path / "qualification"
    root.mkdir(mode=0o700)
    ledger = parent.create_ledger(
        root=root,
        namespace="qualification",
        reservation_id="b" * 64,
        output_root_identity="qualification-output",
    )
    try:
        preflight = _preflight(parent, ledger)
        authority = parent.mint_qualification(preflight, now=101, ledger=ledger)
        with pytest.raises(ProvenanceError, match="trusted_clock_rollback"):
            verify_qualification_first_consume(
                authority,
                _first(preflight),
                now=100,
                ledger=ledger,
            )
        assert ledger.state == "quarantined"
        assert preflight.target_lease.lock.quarantined is True
    finally:
        preflight.target_lease.lock.release()
        ledger.close()


def test_ledger_quarantine_failure_still_quarantines_first_consume_target(
    tmp_path, monkeypatch,
):
    parent = ParentExecutionAuthority().injected_test_controller()
    root = tmp_path / "qualification"
    root.mkdir(mode=0o700)
    ledger = parent.create_ledger(
        root=root,
        namespace="qualification",
        reservation_id="a" * 64,
        output_root_identity="qualification-output",
    )
    try:
        preflight = _preflight(parent, ledger)
        authority = parent.mint_qualification(preflight, now=101, ledger=ledger)
        monkeypatch.setattr(
            parent,
            "quarantine_ledger",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("ledger quarantine unavailable")
            ),
        )
        with pytest.raises(ProvenanceError, match="quarantine_incomplete"):
            verify_qualification_first_consume(
                authority,
                _first(preflight),
                now=100,
                ledger=ledger,
            )
        assert ledger.state == "authority_minted"
        assert preflight.target_lease.lock.quarantined is True
    finally:
        preflight.target_lease.lock.release()
        ledger.close()


def test_first_consume_after_ledger_quarantine_quarantines_bound_target(tmp_path):
    parent = ParentExecutionAuthority().injected_test_controller()
    root = tmp_path / "qualification"
    root.mkdir(mode=0o700)
    ledger = parent.create_ledger(
        root=root,
        namespace="qualification",
        reservation_id="c" * 64,
        output_root_identity="qualification-output",
    )
    try:
        preflight = _preflight(parent, ledger)
        authority = parent.mint_qualification(preflight, now=101, ledger=ledger)
        parent.quarantine_ledger(ledger, {"reason": "preexisting_revoke"})
        with pytest.raises(ProvenanceError, match="authority_replay"):
            verify_qualification_first_consume(
                authority,
                _first(preflight),
                now=102,
                ledger=ledger,
            )
        assert ledger.state == "quarantined"
        assert preflight.target_lease.lock.quarantined is True
    finally:
        preflight.target_lease.lock.release()
        ledger.close()


def test_first_consume_alternate_parent_ledger_quarantines_bound_resources(tmp_path):
    parent = ParentExecutionAuthority().injected_test_controller()
    root = tmp_path / "qualification"
    alternate_root = tmp_path / "alternate"
    root.mkdir(mode=0o700)
    alternate_root.mkdir(mode=0o700)
    ledger = parent.create_ledger(
        root=root,
        namespace="qualification",
        reservation_id="f" * 64,
        output_root_identity="qualification-output",
    )
    alternate = parent.create_ledger(
        root=alternate_root,
        namespace="qualification",
        reservation_id="0" * 64,
        output_root_identity="alternate-output",
    )
    try:
        preflight = _preflight(parent, ledger)
        authority = parent.mint_qualification(preflight, now=101, ledger=ledger)
        with pytest.raises(ProvenanceError, match="authority_replay"):
            verify_qualification_first_consume(
                authority,
                _first(preflight),
                now=102,
                ledger=alternate,
            )
        assert ledger.state == "quarantined"
        assert alternate.state == "reserved"
        assert preflight.target_lease.lock.quarantined is True
    finally:
        preflight.target_lease.lock.release()
        alternate.close()
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
        assert active.sealed_source[preflight.source.records[0].path]
        with pytest.raises(TypeError):
            active.sealed_source[preflight.source.records[0].path] = b"drift"  # type: ignore[index]
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


def test_foreign_ledger_argument_cleans_up_bound_parent_resources(tmp_path):
    parent = ParentExecutionAuthority().injected_test_controller()
    foreign = ParentExecutionAuthority().injected_test_controller()
    root = tmp_path / "qualification"
    foreign_root = tmp_path / "foreign"
    root.mkdir(mode=0o700)
    foreign_root.mkdir(mode=0o700)
    ledger = parent.create_ledger(
        root=root,
        namespace="qualification",
        reservation_id="9" * 64,
        output_root_identity="qualification-output",
    )
    foreign_ledger = foreign.create_ledger(
        root=foreign_root,
        namespace="qualification",
        reservation_id="a" * 64,
        output_root_identity="foreign-output",
    )
    try:
        preflight = _preflight(parent, ledger)
        with pytest.raises(ProvenanceError, match="authority_replay"):
            parent.mint_qualification(preflight, now=101, ledger=foreign_ledger)
        assert ledger.state == "quarantined"
        assert foreign_ledger.state == "reserved"
        assert preflight.target_lease.lock.quarantined is True
    finally:
        preflight.target_lease.lock.release()
        foreign_ledger.close()
        ledger.close()


def test_same_parent_ledger_mismatch_quarantines_bound_resources(tmp_path):
    parent = ParentExecutionAuthority().injected_test_controller()
    root = tmp_path / "qualification"
    other_root = tmp_path / "other"
    root.mkdir(mode=0o700)
    other_root.mkdir(mode=0o700)
    ledger = parent.create_ledger(
        root=root,
        namespace="qualification",
        reservation_id="d" * 64,
        output_root_identity="qualification-output",
    )
    other = parent.create_ledger(
        root=other_root,
        namespace="qualification",
        reservation_id="e" * 64,
        output_root_identity="other-output",
    )
    try:
        preflight = _preflight(parent, ledger)
        with pytest.raises(ProvenanceError, match="target_lock_loss"):
            parent.mint_qualification(preflight, now=101, ledger=other)
        assert ledger.state == "quarantined"
        assert other.state == "reserved"
        assert preflight.target_lease.lock.quarantined is True
    finally:
        preflight.target_lease.lock.release()
        other.close()
        ledger.close()


def test_qualification_mint_wrong_output_observation_still_cleans_bound_resources(tmp_path):
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
        forged = dataclasses.replace(
            preflight,
            output=dataclasses.replace(
                preflight.output,
                root_identity="syntactically-valid-but-wrong-output",
            ),
        )
        with pytest.raises(ProvenanceError, match="authority_replay"):
            parent.mint_qualification(forged, now=101, ledger=ledger)
        assert ledger.state == "quarantined"
        assert preflight.target_lease.lock.quarantined is True
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


def test_retained_target_quarantine_rejects_post_quarantine_inode_drift(tmp_path):
    lock = MinecraftTargetLock(
        lock_root=tmp_path / "locks",
        host="127.0.0.1",
        port=25575,
        world_id="world/1",
        attempt_id="post-quarantine-drift",
    ).acquire()
    parent = ParentExecutionAuthority().injected_test_controller()
    lease = parent.retain_target_lease(lock, "post-quarantine-drift")
    replacement = lock.path.with_name("replacement.lock")
    try:
        lock.quarantine(
            run_name="post-quarantine-drift",
            reasons=("target_lock_loss",),
            diagnostics={},
        )
        replacement.write_bytes(lock.path.read_bytes())
        os.replace(replacement, lock.path)
        assert _quarantine_retained_target(
            lease,
            reason="target_lock_loss",
            authority_identity="authority",
            run_name="post-quarantine-drift",
        ) is False
    finally:
        lock.release()
        if lock.path.exists():
            lock.path.unlink()


def test_activation_rechecks_the_parent_source_seal(tmp_path, monkeypatch):
    import benchmarks.minecraft.k12_execution_provenance as provenance

    parent = ParentExecutionAuthority().injected_test_controller()
    root = tmp_path / "qualification"
    root.mkdir(mode=0o700)
    ledger = parent.create_ledger(
        root=root, namespace="qualification", reservation_id="a" * 64,
        output_root_identity="qualification-output",
    )
    try:
        preflight = _preflight(parent, ledger)
        authority = parent.mint_qualification(preflight, now=101, ledger=ledger)
        verify_first_consume(authority, _first(preflight), now=102, ledger=ledger)

        def drift_after_consume(*args, **kwargs):
            del args, kwargs
            raise ProvenanceError("source_content_mismatch")

        monkeypatch.setattr(
            provenance, "_revalidate_parent_source_closure", drift_after_consume,
        )
        with pytest.raises(ProvenanceError, match="source_content_mismatch"):
            parent.activate_qualification(authority, ledger=ledger)
        assert ledger.state == "first_consume_verified"
    finally:
        preflight.target_lease.lock.release()
        ledger.close()


def test_activation_rejects_a_released_parent_target_lease(tmp_path):
    parent = ParentExecutionAuthority().injected_test_controller()
    root = tmp_path / "qualification"
    root.mkdir(mode=0o700)
    ledger = parent.create_ledger(
        root=root, namespace="qualification", reservation_id="c" * 64,
        output_root_identity="qualification-output",
    )
    try:
        preflight = _preflight(parent, ledger)
        authority = parent.mint_qualification(preflight, now=101, ledger=ledger)
        verify_first_consume(authority, _first(preflight), now=102, ledger=ledger)
        preflight.target_lease.lock.release()
        with pytest.raises(ProvenanceError, match="target_lock_loss"):
            parent.activate_qualification(authority, ledger=ledger)
        assert ledger.state == "first_consume_verified"
    finally:
        preflight.target_lease.lock.release()
        ledger.close()


def test_authority_currentness_refreshes_a_second_durable_ledger_handle(tmp_path):
    parent = ParentExecutionAuthority().injected_test_controller()
    root = tmp_path / "qualification"
    root.mkdir(mode=0o700)
    ledger = parent.create_ledger(
        root=root, namespace="qualification", reservation_id="d" * 64,
        output_root_identity="qualification-output",
    )
    second = None
    try:
        preflight = _preflight(parent, ledger)
        authority = parent.mint_qualification(preflight, now=101, ledger=ledger)
        active = parent.verify_qualification_first_consume(
            authority, _first(preflight), now=102, ledger=ledger,
        )
        second = parent.open_ledger(
            root=root, namespace="qualification", reservation_id=ledger.reservation_id,
            output_root_identity=ledger.output_root_identity, nonce=ledger.nonce,
        )
        parent.quarantine_ledger(second, {"reason": "external_parent_revoke"})
        assert active.current_at() is False
        assert parent.validate_current_authority(active) is False
    finally:
        preflight.target_lease.lock.release()
        if second is not None:
            second.close()
        ledger.close()


def test_active_authority_currentness_rejects_target_lease_loss(tmp_path):
    parent = ParentExecutionAuthority().injected_test_controller()
    root = tmp_path / "qualification"
    root.mkdir(mode=0o700)
    ledger = parent.create_ledger(
        root=root, namespace="qualification", reservation_id="e" * 64,
        output_root_identity="qualification-output",
    )
    try:
        preflight = _preflight(parent, ledger)
        authority = parent.mint_qualification(preflight, now=101, ledger=ledger)
        active = parent.verify_qualification_first_consume(
            authority, _first(preflight), now=102, ledger=ledger,
        )
        preflight.target_lease.lock.release()
        assert active.current_at() is False
        assert parent.validate_current_authority(active) is False
    finally:
        preflight.target_lease.lock.release()
        ledger.close()


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
