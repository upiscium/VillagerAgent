from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from benchmarks.common.eac.canonical import canonical_bytes, canonical_sha256
from benchmarks.minecraft.k12_execution_capsule import CapsuleRecord, attest_capsule
from benchmarks.minecraft.k12_execution_provenance import (
    EXPECTED_BASE_REF,
    EXPECTED_BASE_SHA,
    EXPECTED_BRANCH,
    EXPECTED_HEAD,
    EXPECTED_PR,
    INJECTED_FAKE_ORIGIN,
    INJECTED_TEST_ORIGIN,
    MAX_PR_AGE_SECONDS,
    PROFILE_V2,
    ActiveFinalAuthority,
    ActiveQualificationAuthority,
    CheckoutObservation,
    EnvironmentObservation,
    FinalExecutionAuthority,
    FinalExecutionPrerequisites,
    FinalFirstConsumeObservation,
    FirstConsumeObservation,
    InjectedTestController,
    K12FinalRunAuthorization,
    K12QualificationRunAuthorization,
    K12RetainedTargetLease,
    OutputRootObservation,
    ParentExecutionAuthority,
    ProvenanceError,
    PullRequestObservation,
    QualificationExecutionAuthority,
    QualificationPreflight,
    TargetLockObservation,
    durable_ledger_root_digest,
    git_blob_oid,
    retained_target_binding,
)
from benchmarks.minecraft.k12_guarded_backend import K12AuthenticatedProfile
from benchmarks.minecraft.k12_containment import ContainmentError, containment_identity
from benchmarks.minecraft.k12_live_containment import (
    Descendant,
    LiveContainment,
    LiveState,
    MockContainmentIO,
)
from benchmarks.minecraft.k12_live_oracle import LiveBinding, RejectionEvidence
from benchmarks.minecraft.k12_live_qualification import (
    PROBES,
    LiveQualificationAggregate,
    LiveQualificationCellEvidence,
    LiveQualificationProbeEvidence,
    aggregate_live_qualification,
    qualification_ids,
)
from benchmarks.minecraft.k12_live_runner import (
    EXTERNAL_ENTRY_CHANNELS,
    ExternalEntryFence,
    FinalCellAdmissionRunner,
    InjectedFakeTransport,
    ParentLaunchAuthority,
)
from benchmarks.minecraft.k12_live_state import (
    MockTransport,
    ParentPlanAuthority,
    count_items,
    data_pos,
    data_inventory,
    execute_if_block,
    execute_plan,
    normalize_state,
)
from benchmarks.minecraft.k12_live_validation import (
    FINAL_PHASE,
    FINAL_SCHEDULE,
    FinalCampaignAdmission,
    FinalCampaignManifest,
    FinalCellAuthority,
    FinalCellEvidence,
    admit_final_campaign,
    load_final_campaign_manifest,
)
from benchmarks.minecraft.k12_runtime_profile import (
    load_k12_live_runtime_profile,
    load_k12_live_source_policy,
)
from benchmarks.minecraft.run_lock import MinecraftTargetLock


PROFILE = load_k12_live_runtime_profile()
POLICY = load_k12_live_source_policy()
_DIGEST = "b" * 64
_TREE = "c" * 40
_SOURCE_ROOT = Path(__file__).resolve().parents[1]


def _reservation(label: str, namespace: str) -> str:
    return hashlib.sha256(f"{namespace}:{label}".encode("utf-8")).hexdigest()


def _nonce(label: str, namespace: str) -> str:
    return hashlib.sha256(f"nonce:{namespace}:{label}".encode("utf-8")).hexdigest()


def _source_closure(controller):
    return controller.collect_source_closure(
        root=_SOURCE_ROOT,
        policy=POLICY,
        injected_only=True,
    )


def _capsule(controller, source):
    bound_digests = {
        "repo": source.aggregate_sha256,
        "interpreter": _DIGEST,
        "import_roots": _DIGEST,
    }
    records = tuple(
        CapsuleRecord(identity, bound_digests.get(kind, _DIGEST), kind)
        for identity, kind in (
            ("repo", "repo"),
            ("interpreter", "interpreter"),
            ("stdlib", "stdlib"),
            ("import-roots", "import_roots"),
            ("distributions", "distributions"),
            ("native", "native"),
            ("startup", "startup"),
            ("node", "node"),
            ("java", "java"),
        )
    )
    return controller.attest_execution_capsule(
        identity="minecraft-k12-live-execution-capsule/1",
        source_aggregate=source.aggregate_sha256,
        immutable_store_path_digest=_DIGEST,
        recursive_store_closure_digest=_DIGEST,
        interpreter_digest=_DIGEST,
        import_roots_digest=_DIGEST,
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
        repository_identity="upiscium/VillagerAgent",
        worktree_identity=_DIGEST,
        git_dir_identity=_DIGEST,
        common_dir_identity=_DIGEST,
        symbolic_head_ref=EXPECTED_BRANCH,
        head_commit=EXPECTED_HEAD,
        head_tree=_TREE,
        index_tree=_TREE,
        upstream_ref=EXPECTED_BRANCH,
        upstream_commit=EXPECTED_HEAD,
        remote_repository="upiscium/VillagerAgent",
        remote_ref=EXPECTED_BRANCH,
        remote_commit=EXPECTED_HEAD,
        staged_clean=True,
        tracked_clean=True,
        untracked_clean=True,
        generated_artifacts_absent=True,
    )


def _pull_request():
    return PullRequestObservation(
        "upiscium/VillagerAgent",
        EXPECTED_PR,
        "OPEN",
        True,
        "upiscium/VillagerAgent",
        EXPECTED_BRANCH.removeprefix("refs/heads/"),
        EXPECTED_HEAD,
        EXPECTED_BASE_REF,
        EXPECTED_BASE_SHA,
        100,
        "observer/1",
        _DIGEST,
    )


def _environment():
    return EnvironmentObservation(
        PROFILE["environment_policy_identity"],
        PROFILE["environment_policy_digest"],
        "CPython",
        "3.10.19",
        _DIGEST,
        _DIGEST,
        PROFILE["node_identity"],
        PROFILE["java_identity"],
        PROFILE["bridge_content_sha256"],
        PROFILE["server_jar_sha256"],
        PROFILE["model_identity"],
        PROFILE["endpoint_hash"],
        "unsupported",
        "en_us",
        _DIGEST,
        _DIGEST,
        _DIGEST,
    )


def _target(reservation_id: str, lease: K12RetainedTargetLease):
    retained = retained_target_binding(lease)
    return TargetLockObservation(
        "target/1",
        retained["host_hash"],
        retained["port"],
        PROFILE["rcon_identity"],
        PROFILE["server_identity"],
        "1.19.2",
        PROFILE["data_identity"],
        "world/1",
        tuple(PROFILE["actors"]),
        PROFILE["region"]["name"],
        retained["lock_schema"],
        retained["lock_key"],
        reservation_id,
        retained["lock_object_identity"],
        retained["fd_open"],
        retained["regular_file"],
        retained["device_inode_digest"],
        retained["owner_metadata_digest"],
        retained["continuously_owned"],
    )


def _preflight(
    controller: InjectedTestController,
    ledger: Any,
    lease: K12RetainedTargetLease,
) -> QualificationPreflight:
    source = _source_closure(controller)
    run_authorization = controller.mint_qualification_run_authorization(
        reservation_id=ledger.reservation_id,
        output_root_identity=ledger.output_root_identity,
        profile_digest=PROFILE.profile_digest,
        ledger=ledger,
        now=100,
    )
    return QualificationPreflight(
        reservation_id=ledger.reservation_id,
        run_authorization_digest=run_authorization.identity,
        expected_head=EXPECTED_HEAD,
        checkout=_checkout(),
        pull_request=_pull_request(),
        source=source,
        capsule=_capsule(controller, source),
        authenticated_profile=PROFILE,
        authenticated_source_policy=POLICY,
        profile_identity=PROFILE_V2,
        profile_digest=PROFILE.profile_digest,
        contracts=dict(PROFILE["contract_digests"]),
        schedule_digest=hashlib.sha256(
            canonical_bytes(list(PROFILE["schedule"]))
        ).hexdigest(),
        environment=_environment(),
        target=_target(ledger.reservation_id, lease),
        output=OutputRootObservation(
            ledger.output_root_identity,
            _DIGEST,
            _DIGEST,
            f"fresh/{ledger.reservation_id[:8]}",
            True,
            True,
            False,
        ),
        ledger_identity=ledger.identity,
        ledger_root_digest=durable_ledger_root_digest(ledger),
        run_authorization=run_authorization,
        target_lease=lease,
    )


def _first_consume(preflight: QualificationPreflight) -> FirstConsumeObservation:
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


def _rejection_states(
    authority: ActiveQualificationAuthority,
    campaign: str,
    cell: str,
    reset_token: str,
):
    plan_authority = ParentPlanAuthority(
        K12AuthenticatedProfile.from_runtime_profile(PROFILE),
        campaign,
        authority=authority,
    )
    commands = (
        execute_if_block((0, 64, 0), "stone"),
        count_items("agent", "stone"),
        data_inventory("agent"),
    )
    raw = (
        "Test passed, count: 1",
        "Found 1 matching item on player agent",
        'agent has the following entity data: [{Slot:0b,id:"minecraft:stone",Count:1b}]',
    )
    states = []
    for purpose in ("before", "after"):
        plan = plan_authority.mint(
            commands,
            cell=cell,
            reset_token=reset_token,
            generation=1,
            descriptor="S1",
            purpose=purpose,
        )
        states.append(
            normalize_state(
                plan,
                execute_plan(plan, MockTransport(dict(zip(
                    (command.text for command in commands), raw,
                )))),
            )
        )
    return plan_authority, states[0], states[1]


def _qualification_evidence(
    authority: ActiveQualificationAuthority,
    label: str,
    campaign_id: str,
    probe_campaign_id: str,
):
    cells = []
    for ordinal, cell_id in enumerate(qualification_ids(), 1):
        evidence_digest = hashlib.sha256(
            f"{label}:qualification:{ordinal}".encode("utf-8")
        ).hexdigest()
        arm = cell_id.rsplit("-", 1)[-1]
        rejection = None
        if arm == "S":
            reset_token = f"reset-{label}-{ordinal}"
            plan_authority, before, after = _rejection_states(
                authority,
                campaign_id,
                cell_id,
                reset_token,
            )
            rejection = RejectionEvidence.mint(
                authority,
                LiveBinding(
                    PROFILE.profile_digest,
                    campaign_id,
                    cell_id,
                    reset_token,
                    1,
                    f"request-{label}-{ordinal}",
                    f"permit-{label}-{ordinal}",
                    f"effect-{label}-{ordinal}",
                    plan_authority.authority_id,
                    "S",
                ),
                before,
                after,
                evidence_digest=evidence_digest,
            )
        cells.append(
            LiveQualificationCellEvidence(
                cell_id=cell_id,
                events={"terminal": "passed", "ordinal": ordinal},
                profile_digest=PROFILE.profile_digest,
                campaign_id=campaign_id,
                reset_identity=f"reset-{label}-{ordinal}",
                evidence_digest=evidence_digest,
                fresh_root=True,
                reset_passed=True,
                capability_state="REVOKED",
                provider_terminal="success",
                oracle_value="not_applicable" if arm == "S" else "true",
                containment_clean=True,
                evidence_valid=True,
                terminal_verified=True,
                authority_binding=authority.binding,
                evidence_origin=INJECTED_FAKE_ORIGIN,
                rejection_verified=True,
                rejection_evidence=rejection,
                reset_token=(reset_token if arm == "S" else None),
                generation=(1 if arm == "S" else None),
                request_identity=(f"request-{label}-{ordinal}" if arm == "S" else None),
                permit_identity=(f"permit-{label}-{ordinal}" if arm == "S" else None),
                effect_identity=(f"effect-{label}-{ordinal}" if arm == "S" else None),
            )
        )
    probe_binding = authority.authority.binding("qualification_probe")
    probes = tuple(
        LiveQualificationProbeEvidence(
            probe=probe,
            passed=True,
            profile_digest=PROFILE.profile_digest,
            campaign_id=probe_campaign_id,
            evidence_digest=hashlib.sha256(
                f"{label}:probe:{probe}".encode("utf-8")
            ).hexdigest(),
            terminal_verified=True,
            authority_binding=probe_binding,
            evidence_origin=INJECTED_FAKE_ORIGIN,
        )
        for probe in PROBES
    )
    return tuple(cells), probes


@dataclass
class _QualificationFixture:
    label: str
    parent: ParentExecutionAuthority
    controller: InjectedTestController
    qualification_ledger: Any
    qualification_lock: MinecraftTargetLock
    qualification_lease: K12RetainedTargetLease
    preflight: QualificationPreflight
    qualification_authority: QualificationExecutionAuthority
    active_qualification: ActiveQualificationAuthority
    cells: tuple[LiveQualificationCellEvidence, ...]
    probes: tuple[LiveQualificationProbeEvidence, ...]
    qualification_campaign: str
    fence: ExternalEntryFence

    def close(self) -> None:
        self.qualification_lock.release()
        self.qualification_ledger.close()


@dataclass
class _Graph:
    label: str
    parent: ParentExecutionAuthority
    controller: InjectedTestController
    qualification_ledger: Any
    final_ledger: Any
    qualification_lock: MinecraftTargetLock
    final_lock: MinecraftTargetLock
    qualification_lease: K12RetainedTargetLease
    final_lease: K12RetainedTargetLease
    preflight: QualificationPreflight
    qualification_authority: QualificationExecutionAuthority
    active_qualification: ActiveQualificationAuthority
    cells: tuple[LiveQualificationCellEvidence, ...]
    probes: tuple[LiveQualificationProbeEvidence, ...]
    aggregate: LiveQualificationAggregate
    prerequisites: FinalExecutionPrerequisites
    final_authority: FinalExecutionAuthority
    active_final: ActiveFinalAuthority
    manifest: FinalCampaignManifest
    admission: FinalCampaignAdmission
    cell_authority: FinalCellAuthority
    cell_evidence: FinalCellEvidence
    profile: K12AuthenticatedProfile
    plan_authority: ParentPlanAuthority
    launch_authority: ParentLaunchAuthority
    state: Any
    argv: tuple[str, ...]
    executor: MockContainmentIO
    fence: ExternalEntryFence
    transport: InjectedFakeTransport
    runner: FinalCellAdmissionRunner
    qualification_campaign: str
    final_campaign: str

    def close(self) -> None:
        self.final_lock.release()
        self.qualification_lock.release()
        self.final_ledger.close()
        self.qualification_ledger.close()


def _build_qualification(tmp_path: Path, label: str) -> _QualificationFixture:
    """Build the graph through active qualification, before aggregation."""
    parent = ParentExecutionAuthority()
    controller = parent.injected_test_controller()
    assert isinstance(controller, InjectedTestController)

    qualification_reservation = _reservation(label, "qualification")
    qualification_campaign = f"qualification-{label}"
    probe_campaign = f"probe-{label}"
    lock_root = tmp_path / f"{label}-locks"
    qualification_root = tmp_path / f"{label}-qualification-ledger"
    qualification_root.mkdir(mode=0o700, parents=True)

    qualification_ledger = controller.create_ledger(
        root=qualification_root,
        namespace="qualification",
        reservation_id=qualification_reservation,
        output_root_identity=f"qualification-output-{label}",
        nonce=_nonce(label, "qualification"),
    )
    qualification_lock = MinecraftTargetLock(
        lock_root=lock_root,
        host="127.0.0.1",
        port=25575,
        world_id="world/1",
        attempt_id=qualification_reservation,
    ).acquire()
    qualification_snapshot = qualification_lock.retained_lease_snapshot()
    assert qualification_snapshot.acquired is True
    qualification_lease = controller.retain_target_lease(
        qualification_lock, qualification_reservation,
    )
    preflight = _preflight(controller, qualification_ledger, qualification_lease)
    qualification_authority = controller.mint_qualification(
        preflight, now=101, ledger=qualification_ledger,
    )
    active_qualification = controller.verify_qualification_first_consume(
        qualification_authority,
        _first_consume(preflight),
        now=102,
        ledger=qualification_ledger,
    )
    cells, probes = _qualification_evidence(
        active_qualification,
        label,
        qualification_campaign,
        probe_campaign,
    )
    return _QualificationFixture(
        label,
        parent,
        controller,
        qualification_ledger,
        qualification_lock,
        qualification_lease,
        preflight,
        qualification_authority,
        active_qualification,
        cells,
        probes,
        qualification_campaign,
        ExternalEntryFence("injected_fake"),
    )


def _build_graph(tmp_path: Path, label: str) -> _Graph:
    qualification = _build_qualification(tmp_path, label)
    parent = qualification.parent
    controller = qualification.controller
    qualification_ledger = qualification.qualification_ledger
    qualification_lock = qualification.qualification_lock
    qualification_lease = qualification.qualification_lease
    preflight = qualification.preflight
    qualification_authority = qualification.qualification_authority
    active_qualification = qualification.active_qualification
    cells = qualification.cells
    probes = qualification.probes
    qualification_campaign = qualification.qualification_campaign
    final_reservation = _reservation(label, "final")
    final_campaign = f"final-{label}"
    lock_root = tmp_path / f"{label}-locks"
    final_root = tmp_path / f"{label}-final-ledger"
    final_root.mkdir(mode=0o700, parents=True)

    aggregate = aggregate_live_qualification(
        active_qualification,
        cells,
        probes,
        ledger=qualification_ledger,
    )
    assert qualification_ledger.state == "terminal"
    qualification_lock.release()

    final_ledger = controller.create_ledger(
        root=final_root,
        namespace="final",
        reservation_id=final_reservation,
        output_root_identity=f"final-output-{label}",
        nonce=_nonce(label, "final"),
    )
    final_lock = MinecraftTargetLock(
        lock_root=lock_root,
        host="127.0.0.1",
        port=25575,
        world_id="world/1",
        attempt_id=final_reservation,
    ).acquire()
    final_snapshot = final_lock.retained_lease_snapshot()
    assert final_snapshot.acquired is True
    final_lease = controller.retain_target_lease(final_lock, final_reservation)
    prerequisites = FinalExecutionPrerequisites.from_live_qualification(
        active_qualification,
        aggregate,
    )
    final_authority = controller.mint_final(
        prerequisites,
        active_qualification,
        now=103,
        qualification_evidence=aggregate,
        qualification_ledger=qualification_ledger,
        final_ledger=final_ledger,
        target=_target(final_reservation, final_lease),
        output=OutputRootObservation(
            final_ledger.output_root_identity,
            _DIGEST,
            _DIGEST,
            f"fresh/{final_reservation[:8]}",
            True,
            True,
            False,
        ),
        target_lease=final_lease,
    )
    active_final = controller.verify_final_first_consume(
        final_authority,
        FinalFirstConsumeObservation(
            prerequisites,
            checkout=preflight.checkout,
            source=preflight.source,
            capsule=preflight.capsule,
            environment=preflight.environment,
            target=_target(final_reservation, final_lease),
            output=OutputRootObservation(
                final_ledger.output_root_identity,
                _DIGEST,
                _DIGEST,
                f"fresh/{final_reservation[:8]}",
                True,
                True,
                False,
            ),
            target_lease=final_lease,
            pull_request=preflight.pull_request,
        ),
        now=104,
        ledger=final_ledger,
    )
    manifest = load_final_campaign_manifest(evidence_origin=INJECTED_FAKE_ORIGIN)
    admission = admit_final_campaign(
        active_final,
        aggregate,
        aggregate.probes,
        manifest=manifest,
        campaign_id=final_campaign,
        schedule=FINAL_SCHEDULE,
        evidence_origin=INJECTED_FAKE_ORIGIN,
    )
    cell_id = FINAL_SCHEDULE[0]
    cell_authority = admission.issue_cell(cell_id)
    cell_evidence = FinalCellEvidence(
        cell_id=cell_id,
        profile_digest=PROFILE.profile_digest,
        campaign_id=final_campaign,
        phase=FINAL_PHASE,
        manifest_identity=manifest.manifest_identity,
        manifest_digest=manifest.manifest_digest,
        common_closure_digest=admission.common_closure_digest,
        evidence_digest=hashlib.sha256(
            f"{label}:final-cell".encode("utf-8")
        ).hexdigest(),
        authority_binding=admission.binding,
        terminal_verified=True,
        result="passed",
        execution_provenance="live_final",
        evidence_origin=INJECTED_FAKE_ORIGIN,
        fresh_root=True,
    )

    profile = K12AuthenticatedProfile.from_runtime_profile(PROFILE)
    plan_authority = ParentPlanAuthority(
        profile,
        final_campaign,
        authority=active_final,
    )
    command = data_pos("agent")
    plan = plan_authority.mint(
        (command,),
        cell=cell_id,
        reset_token=f"launch-reset-{label}",
        generation=1,
        descriptor="containment",
        purpose="launch",
        authority_binding=active_final.binding,
    )
    state = normalize_state(
        plan,
        execute_plan(
            plan,
            MockTransport({
                command.text: "agent has the following entity data: [0.0d,64.0d,0.0d]",
            }),
        ),
    )
    launch_authority = ParentLaunchAuthority(
        profile,
        final_campaign,
        f"cohort-{label}",
        plan_authority,
        authority=active_final,
    )
    executor = MockContainmentIO(("unused=1",))
    fence = qualification.fence
    transport = InjectedFakeTransport(fence)
    runner = FinalCellAdmissionRunner(
        executor=executor,
        parent=launch_authority,
        admission=admission,
        lease=final_lease,
        ledger=final_ledger,
        mode="injected_fake",
        external_entry_fence=fence,
        fake_transport=transport,
    )
    return _Graph(
        label,
        parent,
        controller,
        qualification_ledger,
        final_ledger,
        qualification_lock,
        final_lock,
        qualification_lease,
        final_lease,
        preflight,
        qualification_authority,
        active_qualification,
        cells,
        probes,
        aggregate,
        prerequisites,
        final_authority,
        active_final,
        manifest,
        admission,
        cell_authority,
        cell_evidence,
        profile,
        plan_authority,
        launch_authority,
        state,
        ("fake-minecraft", "--cell", cell_id),
        executor,
        fence,
        transport,
        runner,
        qualification_campaign,
        final_campaign,
    )


def _zero_counts() -> dict[str, int]:
    return {channel: 0 for channel in EXTERNAL_ENTRY_CHANNELS}


def _final_containment_executor(cgroup: str) -> MockContainmentIO:
    main = Descendant(99, 8, "/mock/worker", 1, 99, cgroup)
    running = f"MainPID=99\nControlGroup={cgroup}\nActiveState=active\nSubState=running"
    stopped = f"MainPID=0\nControlGroup={cgroup}\nActiveState=inactive\nSubState=dead"
    return MockContainmentIO(
        (running, running, stopped),
        waits=(True,),
        descendants=((main,), (main,), ()),
        cgroup_sources=((cgroup, (99,), 1), (cgroup, (99,), 1), (cgroup, (), 0)),
    )


def _assert_no_external_entries(graph: Any) -> None:
    expected = _zero_counts()
    assert graph.fence.real_counts == expected
    assert graph.fence.external_counters == expected
    assert graph.fence.entry_counts == expected
    assert graph.fence.real_entry_counts == expected
    assert graph.fence.fake_counts == expected
    assert graph.fence.fake_dispatches == ()
    graph.fence.assert_no_real_entries()


def _new_runner(
    graph: _Graph,
    *,
    parent: ParentLaunchAuthority | None = None,
    mode: str = "injected_fake",
    with_fake_transport: bool = True,
) -> FinalCellAdmissionRunner:
    return FinalCellAdmissionRunner(
        executor=graph.executor,
        parent=parent or graph.launch_authority,
        admission=graph.admission,
        lease=graph.final_lease,
        ledger=graph.final_ledger,
        mode=mode,
        external_entry_fence=graph.fence if mode == "injected_fake" else None,
        fake_transport=graph.transport if with_fake_transport else None,
    )


def test_minecraft_k12_authority_graph_uses_final_scope_launch_and_completion(tmp_path):
    graph = _build_graph(tmp_path, "positive")
    try:
        assert graph.parent.runtime_admissible is True
        assert graph.controller.origin == INJECTED_TEST_ORIGIN
        assert graph.controller.runtime_admissible is False
        assert isinstance(graph.qualification_authority, QualificationExecutionAuthority)
        assert isinstance(
            graph.qualification_authority.run_authorization,
            K12QualificationRunAuthorization,
        )
        assert isinstance(graph.preflight, QualificationPreflight)
        assert isinstance(graph.qualification_lease, K12RetainedTargetLease)
        assert graph.qualification_lease.evidence["attempt_id"] == (
            graph.qualification_ledger.reservation_id
        )
        assert isinstance(graph.active_qualification, ActiveQualificationAuthority)
        assert graph.active_qualification.lifecycle == "active"
        assert graph.active_qualification.origin == INJECTED_TEST_ORIGIN
        assert graph.controller.validate_current_authority(graph.active_qualification) is True

        assert len(graph.cells) == 15
        assert tuple(cell.cell_id for cell in graph.cells) == qualification_ids()
        assert len({cell.evidence_digest for cell in graph.cells}) == 15
        assert all(
            cell.evidence_origin == INJECTED_FAKE_ORIGIN
            and cell.authority_binding == graph.active_qualification.binding
            and cell.terminal_verified is True
            and cell.events["terminal"] == "passed"
            for cell in graph.cells
        )
        assert tuple(probe.probe for probe in graph.probes) == PROBES
        assert len({probe.evidence_digest for probe in graph.probes}) == 4
        assert graph.aggregate.probes.probes == PROBES
        assert graph.aggregate.probes.campaign_id != graph.aggregate.campaign_id
        assert graph.aggregate.evidence_origin == INJECTED_FAKE_ORIGIN
        assert graph.aggregate.qualifies() is True
        assert graph.qualification_ledger.state == "terminal"
        assert graph.qualification_ledger.events[-1].state == "terminal"
        assert graph.qualification_ledger.events[-1].payload["result"] == "passed"

        assert graph.qualification_ledger.root != graph.final_ledger.root
        assert graph.qualification_ledger.nonce != graph.final_ledger.nonce
        assert graph.qualification_ledger.reservation_id != graph.final_ledger.reservation_id
        assert isinstance(graph.final_authority.run_authorization, K12FinalRunAuthorization)
        assert isinstance(graph.final_authority, FinalExecutionAuthority)
        assert isinstance(graph.active_final, ActiveFinalAuthority)
        assert graph.active_final.lifecycle == "active"
        assert graph.final_ledger.state == "active"
        assert graph.final_ledger.verify_chain() is True
        assert graph.final_lease.evidence["attempt_id"] == graph.final_ledger.reservation_id
        assert graph.manifest.phase == FINAL_PHASE
        assert graph.manifest.schedule == FINAL_SCHEDULE
        assert graph.manifest.cell_count == 90
        assert graph.manifest.cell_ids == FINAL_SCHEDULE
        assert isinstance(graph.admission, FinalCampaignAdmission)
        assert graph.admission.schedule == FINAL_SCHEDULE
        assert graph.admission.manifest is graph.manifest
        assert isinstance(graph.cell_authority, FinalCellAuthority)

        launch_id = "launch-positive"
        _, containment_cgroup = containment_identity(
            f"{graph.cell_authority.binding.namespace}:{graph.state.cell}",
            launch_id,
        )
        containment_io = _final_containment_executor(containment_cgroup)
        fake_transport = InjectedFakeTransport(graph.fence)
        runner = FinalCellAdmissionRunner(
            executor=containment_io,
            parent=graph.launch_authority,
            admission=graph.admission,
            lease=graph.final_lease,
            ledger=graph.final_ledger,
            mode="injected_fake",
            external_entry_fence=graph.fence,
            fake_transport=fake_transport,
        )
        prepared = runner.prepare(
            graph.cell_authority,
            graph.state,
            launch_id,
            graph.argv,
        )
        assert prepared.executed is False
        assert graph.fence.fake_dispatch_count == 0
        launched = runner.launch(
            graph.cell_authority,
            graph.state,
            launch_id,
            graph.argv,
        )
        assert launched.executed is True
        assert graph.cell_authority.launch_consumed is True
        assert graph.cell_authority.consumed is False
        assert graph.admission.consumed_cells == ()
        assert fake_transport.dispatches == [launched.command]
        assert tuple(record.ordinal for record in graph.fence.ordered_fake_dispatches) == (0,)
        assert tuple(record.cell_id for record in graph.fence.ordered_fake_dispatches) == (
            FINAL_SCHEDULE[0],
        )
        assert graph.fence.fake_dispatch_count == 1
        assert graph.fence.fake_counts == {
            channel: 1 for channel in EXTERNAL_ENTRY_CHANNELS
        }
        completed = runner.complete(
            graph.cell_authority,
            graph.cell_evidence,
            launch_id=launch_id,
        )
        assert completed is graph.cell_evidence
        assert graph.cell_authority.consumed is True
        assert graph.admission.consumed_cells == (FINAL_SCHEDULE[0],)
        controller = LiveContainment(
            containment_io,
            unit=launched.unit_id,
            cgroup=launched.cgroup_id,
            clock=lambda: 0,
            cell_authority=graph.cell_authority,
        )
        stopped = runner.stop(controller, graph.cell_authority)
        assert stopped.active_state == "inactive"
        assert controller.state is LiveState.CLEAN
        assert graph.fence.real_counts == _zero_counts()
        assert graph.fence.external_counters == _zero_counts()
        assert graph.fence.entry_counts == _zero_counts()
        assert graph.fence.real_entry_counts == _zero_counts()
        graph.fence.assert_no_real_entries()
        assert runner.closed is True
    finally:
        graph.close()


def test_expired_final_authority_is_rejected_before_fake_dispatch(tmp_path):
    graph = _build_graph(tmp_path, "expired-final-authority")
    try:
        assert graph.controller.validate_current_authority(graph.active_final) is True
        prepared = graph.runner.prepare(
            graph.cell_authority,
            graph.state,
            "launch-expired",
            graph.argv,
        )
        assert prepared.executed is False
        assert graph.transport.dispatches == []

        expires_at = graph.active_final.authority.body["expires_at"]
        graph.controller.advance_trusted_time(
            expires_at - graph.controller.current_time() + 1,
        )
        assert graph.controller.current_time() > expires_at
        assert graph.controller.validate_current_authority(graph.active_final) is False

        with pytest.raises(ProvenanceError, match="authority_replay"):
            admit_final_campaign(
                graph.active_final,
                graph.aggregate,
                graph.aggregate.probes,
                manifest=graph.manifest,
                campaign_id=graph.final_campaign,
                evidence_origin=INJECTED_FAKE_ORIGIN,
            )
        with pytest.raises(ProvenanceError, match="authority_replay"):
            graph.admission.issue_cell(FINAL_SCHEDULE[1])
        with pytest.raises(ProvenanceError, match="authority_replay"):
            graph.cell_authority.consume_for_launch()

        with pytest.raises(ContainmentError, match="active or parent-owned"):
            FinalCellAdmissionRunner(
                executor=graph.executor,
                parent=graph.launch_authority,
                admission=graph.admission,
                lease=graph.final_lease,
                ledger=graph.final_ledger,
                mode="injected_fake",
                external_entry_fence=graph.fence,
                fake_transport=graph.transport,
            )
        with pytest.raises(ContainmentError, match="active or parent-owned"):
            graph.runner.prepare(
                graph.cell_authority,
                graph.state,
                "launch-expired",
                graph.argv,
            )
        with pytest.raises(ContainmentError, match="active or parent-owned"):
            graph.runner.launch(
                graph.cell_authority,
                graph.state,
                "launch-expired",
                graph.argv,
            )
        assert graph.transport.dispatches == []
        _assert_no_external_entries(graph)
    finally:
        graph.close()


def test_final_runner_rejects_foreign_parent_lease_before_fake_dispatch(tmp_path):
    graph = _build_graph(tmp_path, "foreign-parent-lease")
    foreign_parent = ParentExecutionAuthority()
    try:
        foreign_lease = foreign_parent.retain_target_lease(
            graph.final_lock, graph.final_ledger.reservation_id,
        )
        assert foreign_lease.canonical() == graph.active_final.authority.receipt()["target_lease"]
        with pytest.raises(ContainmentError, match="target lease"):
            FinalCellAdmissionRunner(
                executor=graph.executor,
                parent=graph.launch_authority,
                admission=graph.admission,
                lease=foreign_lease,
                ledger=graph.final_ledger,
                mode="injected_fake",
                external_entry_fence=graph.fence,
                fake_transport=graph.transport,
            )
        assert graph.transport.dispatches == []
        _assert_no_external_entries(graph)
    finally:
        graph.close()


def test_final_runner_rejects_unrelated_same_reservation_lease_before_fake_dispatch(tmp_path):
    graph = _build_graph(tmp_path, "unrelated-same-reservation-lease")
    unrelated_lock = MinecraftTargetLock(
        lock_root=tmp_path / "unrelated-lock",
        host="127.0.0.1",
        port=25575,
        world_id="world/1",
        attempt_id=graph.final_ledger.reservation_id,
    ).acquire()
    try:
        unrelated_lease = graph.controller.retain_target_lease(
            unrelated_lock, graph.final_ledger.reservation_id,
        )
        assert unrelated_lease.reservation_id == graph.final_ledger.reservation_id
        assert unrelated_lease.canonical() != graph.active_final.authority.receipt()["target_lease"]
        with pytest.raises(ContainmentError, match="target lease"):
            FinalCellAdmissionRunner(
                executor=graph.executor,
                parent=graph.launch_authority,
                admission=graph.admission,
                lease=unrelated_lease,
                ledger=graph.final_ledger,
                mode="injected_fake",
                external_entry_fence=graph.fence,
                fake_transport=graph.transport,
            )
        assert graph.transport.dispatches == []
        _assert_no_external_entries(graph)
    finally:
        unrelated_lock.release()
        graph.close()


def test_final_mint_rejects_cross_target_before_authority(tmp_path):
    qualification = _build_qualification(tmp_path, "cross-target")
    final_lock = None
    final_ledger = None
    try:
        aggregate = aggregate_live_qualification(
            qualification.active_qualification,
            qualification.cells,
            qualification.probes,
            ledger=qualification.qualification_ledger,
        )
        qualification.qualification_lock.release()
        prerequisites = FinalExecutionPrerequisites.from_live_qualification(
            qualification.active_qualification,
            aggregate,
        )
        final_reservation = _reservation("cross-target", "final")
        final_root = tmp_path / "cross-target-final-ledger"
        final_root.mkdir(mode=0o700, parents=True)
        final_ledger = qualification.controller.create_ledger(
            root=final_root,
            namespace="final",
            reservation_id=final_reservation,
            output_root_identity="cross-target-final-output",
            nonce=_nonce("cross-target", "final"),
        )
        final_lock = MinecraftTargetLock(
            lock_root=tmp_path / "cross-target-locks",
            host="127.0.0.2",
            port=25576,
            world_id="world/2",
            attempt_id=final_reservation,
        ).acquire()
        final_lease = qualification.controller.retain_target_lease(
            final_lock,
            final_reservation,
        )
        retained = retained_target_binding(final_lease)
        cross_target = TargetLockObservation(
            "target/1",
            retained["host_hash"],
            retained["port"],
            PROFILE["rcon_identity"],
            PROFILE["server_identity"],
            "1.19.2",
            PROFILE["data_identity"],
            "world/2",
            tuple(PROFILE["actors"]),
            PROFILE["region"]["name"],
            retained["lock_schema"],
            retained["lock_key"],
            final_reservation,
            retained["lock_object_identity"],
            retained["fd_open"],
            retained["regular_file"],
            retained["device_inode_digest"],
            retained["owner_metadata_digest"],
            retained["continuously_owned"],
        )
        with pytest.raises(ProvenanceError, match="target_identity_mismatch"):
            qualification.controller.mint_final(
                prerequisites,
                qualification.active_qualification,
                now=103,
                qualification_evidence=aggregate,
                qualification_ledger=qualification.qualification_ledger,
                final_ledger=final_ledger,
                target=cross_target,
                output=OutputRootObservation(
                    final_ledger.output_root_identity,
                    _DIGEST,
                    _DIGEST,
                    f"fresh/{final_reservation[:8]}",
                    True,
                    True,
                    False,
                ),
                target_lease=final_lease,
            )
        assert final_ledger.state == "quarantined"
        assert tuple(event.state for event in final_ledger.events) == (
            "reserved",
            "quarantined",
        )
    finally:
        if final_lock is not None:
            final_lock.release()
        if final_ledger is not None:
            final_ledger.close()
        qualification.close()


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("test_only_runtime_substitution", "origin does not match"),
        ("expired_auth", "pr_observation_stale"),
        ("wrong_auth", "binding mismatch"),
        ("lease_release", "stale or lost"),
        ("lease_drift", "stale or lost"),
        ("lease_quarantine", "stale or lost"),
        ("stale_snapshot", "target_lock_loss"),
        ("closure_mismatch", "final_prerequisite_mismatch"),
        ("lifecycle_revoke", "not active"),
        ("partial_aggregate", "exact ordered"),
        ("spliced_aggregate", "final_prerequisite_mismatch"),
        ("runner_denial", "evidence"),
    ],
    ids=[
        "test_only_runtime_substitution",
        "expired_auth",
        "wrong_auth",
        "lease_release",
        "lease_drift",
        "lease_quarantine",
        "stale_snapshot",
        "closure_mismatch",
        "lifecycle_revoke",
        "partial_aggregate",
        "spliced_aggregate",
        "runner_denial",
    ],
)
def test_minecraft_k12_authority_negative_matrix_denies_before_real_entry(
    tmp_path,
    case: str,
    expected: str,
):
    if case == "partial_aggregate":
        qualification = _build_qualification(tmp_path, f"negative-{case}")
        try:
            assert qualification.qualification_ledger.state == "active"
            assert len(qualification.cells) == 15
            assert len(qualification.cells[:-1]) == 14
            with pytest.raises(ValueError, match=expected):
                aggregate_live_qualification(
                    qualification.active_qualification,
                    qualification.cells[:-1],
                    qualification.probes,
                    ledger=qualification.qualification_ledger,
                )
            assert qualification.qualification_ledger.state == "active"
            _assert_no_external_entries(qualification)
        finally:
            qualification.close()
        return

    graph = _build_graph(tmp_path, f"negative-{case}")
    try:
        with pytest.raises(
            (ContainmentError, ProvenanceError, TypeError, ValueError),
            match=expected,
        ):
            if case == "test_only_runtime_substitution":
                _new_runner(graph, mode="runtime", with_fake_transport=False)
            elif case == "expired_auth":
                expired = replace(graph.preflight.pull_request, observed_at=0)
                graph.active_final.refresh_pull_request(
                    expired, now=MAX_PR_AGE_SECONDS + 1,
                )
            elif case == "wrong_auth":
                wrong_profile = K12AuthenticatedProfile.from_runtime_profile(PROFILE)
                wrong_plan = ParentPlanAuthority(
                    wrong_profile,
                    graph.final_campaign,
                    authority=graph.active_qualification,
                )
                wrong_parent = ParentLaunchAuthority(
                    wrong_profile,
                    graph.final_campaign,
                    f"wrong-cohort-{case}",
                    wrong_plan,
                    authority=graph.active_qualification,
                )
                _new_runner(graph, parent=wrong_parent)
            elif case == "lease_release":
                graph.final_lock.release()
                _new_runner(graph)
            elif case == "lease_drift":
                graph.final_lock.path.unlink()
                _new_runner(graph)
            elif case == "lease_quarantine":
                graph.final_lock.quarantine(
                    run_name=f"quarantine-{case}",
                    reasons=("injected test quarantine",),
                    diagnostics={"case": case},
                )
                _new_runner(graph)
            elif case == "stale_snapshot":
                graph.final_lock.release()
                graph.final_lease.revalidate()
            elif case == "closure_mismatch":
                admit_final_campaign(
                    graph.active_final,
                    graph.aggregate,
                    graph.aggregate.probes,
                    manifest=graph.manifest,
                    campaign_id=graph.final_campaign,
                    common_closure_digest=canonical_sha256({"closure": "wrong"}),
                    evidence_origin=INJECTED_FAKE_ORIGIN,
                )
            elif case == "lifecycle_revoke":
                graph.controller.quarantine_ledger(
                    graph.final_ledger, {"reason": "lifecycle revoke"}
                )
                _new_runner(graph)
            elif case == "spliced_aggregate":
                other = _build_graph(tmp_path / "spliced", f"{case}-other")
                try:
                    admit_final_campaign(
                        graph.active_final,
                        other.aggregate,
                        other.aggregate.probes,
                        manifest=graph.manifest,
                        campaign_id=graph.final_campaign,
                        evidence_origin=INJECTED_FAKE_ORIGIN,
                    )
                finally:
                    other.close()
            elif case == "runner_denial":
                graph.runner.prepare(
                    graph.cell_authority,
                    graph.state,
                    "launch-denied",
                    graph.argv,
                    evidence=graph.cell_evidence,
                )
            else:  # pragma: no cover - the parameter table is exhaustive
                raise AssertionError(f"unknown negative case: {case}")
        _assert_no_external_entries(graph)
    finally:
        graph.close()
