from dataclasses import replace
import pytest
from types import SimpleNamespace

from benchmarks.common.eac.canonical import canonical_sha256
from benchmarks.minecraft.k12_execution_provenance import (
    FinalExecutionPrerequisites,
    FinalFirstConsumeObservation,
    INJECTED_FAKE_ORIGIN,
    INJECTED_TEST_ORIGIN,
    ParentExecutionAuthority,
    OutputRootObservation,
    ProvenanceError,
    RUNTIME_VERIFIED_ORIGIN,
)
from benchmarks.minecraft.k12_live_validation import *
from benchmarks.minecraft.k12_live_containment import Descendant, LiveContainment, MockContainmentIO, parse_observation
from benchmarks.minecraft.k12_guarded_backend import K12AuthenticatedProfile
from benchmarks.minecraft.k12_live_qualification import (MockCellQualificationEvidence,
    LiveQualificationCellEvidence, LiveQualificationProbeEvidence, MockProbeEvidence,
    LIVE_QUALIFICATION_PROVENANCE, PROBES, aggregate_live_qualification, qualification_ids,
    qualify_live_cell, qualify_mock_campaign, qualify_mock_probes)
from benchmarks.minecraft.k12_live_oracle import LiveBinding, RejectionEvidence
from benchmarks.minecraft.k12_live_state import (
    ParentPlanAuthority,
    MockTransport,
    count_items,
    data_inventory,
    execute_if_block,
    execute_plan,
    normalize_state,
)
from benchmarks.minecraft.k12_runtime_profile import load_k12_live_runtime_profile
from benchmarks.minecraft.run_lock import MinecraftTargetLock

def passing_aggregate(profile):
    values=[]
    for cell_id in qualification_ids():
        values.append(MockCellQualificationEvidence(cell_id,{"status":"passed"},profile.profile_digest,
            "qualification-campaign","reset-"+cell_id,"evidence-"+cell_id,True,True,
            "REVOKED","success","not_applicable" if cell_id.endswith("-S") else "true",True,True))
    return qualify_mock_campaign(tuple(values))


def _rejection_states(active, profile, campaign, cell, reset_token):
    plan_authority = ParentPlanAuthority(
        profile,
        campaign,
        authority=active,
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
        states.append(normalize_state(plan, execute_plan(plan, MockTransport(dict(zip(
            (command.text for command in commands), raw,
        ))))))
    return plan_authority, states[0], states[1]


@pytest.fixture
def launched_graph(tmp_path):
    helpers = pytest.importorskip("test_minecraft_k12_authority_e2e")
    graph = helpers._build_graph(tmp_path, "validation-containment")
    graph.cell_authority.consume_for_launch()
    graph.cell_authority.complete(graph.cell_evidence)
    try:
        yield graph
    finally:
        graph.close()

def test_containment_validation_requires_empty_cgroup_and_events_zero(launched_graph):
    show="MainPID=0\nControlGroup=/cg\nActiveState=inactive\nSubState=dead"
    running="MainPID=1\nControlGroup=/cg\nActiveState=active\nSubState=running"
    main=Descendant(1,1,"/mock/worker",0,1,"/cg")
    observation=LiveContainment(MockContainmentIO((running,running,show),waits=(True,),descendants=((main,),(main,),()),
        cgroup_sources=(("/cg",(1,),1),("/cg",(1,),1),("/cg",(),0))),unit="u",cgroup="/cg",clock=lambda:0,
        cell_authority=launched_graph.cell_authority).stop()
    assert validate_live_containment(observation, "/cg", cell_authority=launched_graph.cell_authority)[0]
    with pytest.raises(RuntimeError, match="cgroup|MainPID"):
        parse_observation(MockContainmentIO((show,),descendants=((),),
            cgroup_sources=(("/cg",(),1),)),"u","/cg",
            cell_authority=launched_graph.cell_authority)

def test_non_final_artifacts_fail_launch_gate():
    assert not final_launch_gate({"cohort_kind":"qualification", "schedule_count":15})
    assert not final_launch_gate({"cohort_kind":"final", "schedule_count":90, "offline":True})


def test_containment_is_one_campaign_probe_set():
    from benchmarks.minecraft.k12_live_validation import validate_containment_probe_artifact
    assert validate_containment_probe_artifact({
        "identity": "minecraft-k12-live-containment-probe/1",
        "probes": ["P1", "P2", "P3", "P4"], "passed": True,
    })[0]

def test_detached_stop_and_probe_manifests_are_strict_and_exhaustive():
    stop=load_k12_live_stop_policy(); probe=load_k12_live_containment_probe()
    assert stop["consequences"] == STOP_POLICY_CONSEQUENCES
    assert stop["unknown"] == stop["unmapped"] == "reject"
    assert probe["probes"] == ["P1","P2","P3","P4"]

def test_typed_final_gate_rejects_mock_qualification_even_when_all_mock_gates_pass():
    profile=load_k12_live_runtime_profile(); qualification=passing_aggregate(profile)
    probes=qualify_mock_probes(tuple(MockProbeEvidence(probe,True,profile.profile_digest,
        "probe-campaign","evidence-"+probe) for probe in ("P1","P2","P3","P4")))
    wrapper=LiveFinalWrapper(90,profile.profile_digest,"final-campaign")
    value=FinalGateInput(wrapper,profile.profile_digest,"final-campaign",
                         qualification,probes,True)
    assert not final_launch_gate(value)
    assert not final_launch_gate({"schedule_count":90})
    assert not final_launch_gate(FinalGateInput(wrapper,profile.profile_digest,
        "final-campaign",qualification,probes,False))


def test_final_schedule_and_manifest_are_phase_specific():
    assert len(FINAL_SCHEDULE) == 90
    assert tuple(FINAL_SCHEDULE) == final_cell_ids()
    manifest = load_final_campaign_manifest()
    assert manifest.phase == FINAL_PHASE
    assert manifest.schedule == FINAL_SCHEDULE
    assert manifest.manifest_identity == FINAL_RANDOMIZATION_MANIFEST_IDENTITY
    with pytest.raises(ValueError):
        FinalCampaignManifest(
            "qualification", "minecraft-eac-k12-live-runtime-qualification/2",
            "a" * 64, FINAL_SCHEDULE,
        )


def test_final_cell_authority_is_not_directly_constructible():
    with pytest.raises(TypeError):
        FinalCellAuthority(object(), FINAL_SCHEDULE[0], 0)


def test_injected_controller_mints_typed_final_scope_but_not_runtime_scope(tmp_path):
    helpers = pytest.importorskip("test_minecraft_k12_execution_provenance")
    e2e_helpers = pytest.importorskip("test_minecraft_k12_authority_e2e")
    profile = helpers.PROFILE
    parent = ParentExecutionAuthority()
    injected = parent.injected_test_controller()
    qroot = tmp_path / "injected-qualification"
    froot = tmp_path / "injected-final"
    qroot.mkdir(mode=0o700)
    froot.mkdir(mode=0o700)
    qledger = injected.create_ledger(
        root=qroot,
        namespace="qualification",
        reservation_id="8" * 64,
        output_root_identity="qualification-output",
    )
    fledger = injected.create_ledger(
        root=froot,
        namespace="final",
        reservation_id="9" * 64,
        output_root_identity="final-output",
    )
    final_lock = MinecraftTargetLock(
        lock_root=tmp_path / "injected-final-lock",
        host="127.0.0.1",
        port=25575,
        world_id="world/1",
        attempt_id=fledger.reservation_id,
    ).acquire()
    final_lease = injected.retain_target_lease(final_lock, fledger.reservation_id)
    final_target = e2e_helpers._target(fledger.reservation_id, final_lease)
    final_output = OutputRootObservation(
        fledger.output_root_identity,
        helpers.D,
        helpers.D,
        "fresh/final",
        True,
        True,
        False,
    )
    try:
        preflight = helpers._preflight(injected, qledger)
        qauthority = injected.mint_qualification(preflight, now=101, ledger=qledger)
        active_q = injected.verify_qualification_first_consume(
            qauthority, helpers._first(preflight), now=102, ledger=qledger,
        )
        cells = []
        for ordinal, cell_id in enumerate(qualification_ids(), 1):
            arm = cell_id.rsplit("-", 1)[-1]
            reset = f"reset-{ordinal}"
            evidence_digest = f"{ordinal:064x}"
            rejection = None
            if arm == "S":
                reset = f"reset-{ordinal}"
                plan_authority, before, after = _rejection_states(
                    active_q,
                    K12AuthenticatedProfile.from_runtime_profile(profile),
                    "qualification-campaign",
                    cell_id,
                    reset,
                )
                rejection = RejectionEvidence.mint(
                    active_q,
                    LiveBinding(
                        profile.profile_digest,
                        "qualification-campaign",
                        cell_id,
                        reset,
                        1,
                        f"request-{ordinal}",
                        f"permit-{ordinal}",
                        f"effect-{ordinal}",
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
                    events={"terminal": "passed"},
                    profile_digest=profile.profile_digest,
                    campaign_id="qualification-campaign",
                    reset_identity=reset,
                    evidence_digest=evidence_digest,
                    fresh_root=True,
                    reset_passed=True,
                    capability_state="REVOKED",
                    provider_terminal="success",
                    oracle_value="not_applicable" if arm == "S" else "true",
                    containment_clean=True,
                    evidence_valid=True,
                    authority_binding=active_q.binding,
                    evidence_origin=INJECTED_FAKE_ORIGIN,
                    execution_provenance=LIVE_QUALIFICATION_PROVENANCE,
                    rejection_evidence=rejection,
                    reset_token=reset,
                    generation=1,
                    request_identity=f"request-{ordinal}",
                    permit_identity=f"permit-{ordinal}",
                    effect_identity=f"effect-{ordinal}",
                )
            )
        probes = tuple(
            LiveQualificationProbeEvidence(
                probe=probe,
                passed=True,
                profile_digest=profile.profile_digest,
                campaign_id="probe-campaign",
                evidence_digest=f"probe-{probe}",
                authority_binding=active_q.authority.binding("qualification_probe"),
                evidence_origin=INJECTED_FAKE_ORIGIN,
            )
            for probe in ("P1", "P2", "P3", "P4")
        )
        s_index = next(index for index, value in enumerate(cells) if value.cell_id.endswith("-S"))
        s_cell = cells[s_index]
        assert s_cell.rejection_evidence is not None
        bad_rejection = object.__new__(type(s_cell.rejection_evidence))
        for name in s_cell.rejection_evidence.__dataclass_fields__:
            object.__setattr__(
                bad_rejection,
                name,
                getattr(s_cell.rejection_evidence, name),
            )
        object.__setattr__(
            bad_rejection,
            "binding",
            replace(bad_rejection.binding, reset_token="wrong-reset"),
        )
        assert not qualify_live_cell(
            active_q, replace(s_cell, rejection_evidence=bad_rejection)
        ).passed
        assert not qualify_live_cell(
            active_q,
            replace(
                s_cell,
                request_identity=None,
                permit_identity=None,
                effect_identity=None,
            ),
        ).passed
        aggregate = aggregate_live_qualification(
            active_q, tuple(cells), probes, ledger=qledger,
        )
        assert aggregate.evidence_origin == INJECTED_FAKE_ORIGIN
        assert not aggregate.runtime_admissible
        receipt = aggregate.ownership_receipt
        assert aggregate.authenticate_for_final_prerequisites(active_q, injected) is receipt
        assert receipt.authority is active_q
        assert receipt.controller is injected
        lookalike = SimpleNamespace(
            identity=aggregate.identity,
            authority=active_q,
            authority_binding=aggregate.authority_binding,
            evidence_origin=aggregate.evidence_origin,
        )
        assert not receipt.authenticates(lookalike)
        prerequisites = FinalExecutionPrerequisites.from_live_qualification(
            qauthority, aggregate,
        )
        final = injected.mint_final(
            prerequisites,
            active_q,
            now=103,
            qualification_evidence=aggregate,
            qualification_ledger=qledger,
            final_ledger=fledger,
            target=final_target,
            output=final_output,
            target_lease=final_lease,
        )
        active_final = injected.verify_final_first_consume(
            final,
            FinalFirstConsumeObservation(
                prerequisites,
                checkout=preflight.checkout,
                source=preflight.source,
                capsule=preflight.capsule,
                environment=preflight.environment,
                target=final_target,
                output=final_output,
                target_lease=final_lease,
                pull_request=preflight.pull_request,
            ),
            now=104,
            ledger=fledger,
        )
        assert active_final.origin == INJECTED_FAKE_ORIGIN
        assert not active_final.runtime_admissible
        manifest = FinalCampaignManifest(
            FINAL_PHASE,
            "injected-final-manifest",
            "c" * 64,
            FINAL_SCHEDULE,
            evidence_origin=INJECTED_FAKE_ORIGIN,
        )
        admission = admit_final_campaign(
            active_final,
            aggregate,
            aggregate.probes,
            manifest=manifest,
            campaign_id="final-campaign",
            evidence_origin=INJECTED_FAKE_ORIGIN,
        )
        assert admission.evidence_origin == INJECTED_FAKE_ORIGIN
        assert not admission.runtime_admissible
        with pytest.raises(ProvenanceError, match="final_schedule_order"):
            admission.issue_cell(FINAL_SCHEDULE[1])
        cell_authority = admission.issue_cell(FINAL_SCHEDULE[0])
        second_authority = admission.issue_cell(FINAL_SCHEDULE[1])
        cell = FinalCellEvidence(
            cell_id=FINAL_SCHEDULE[0],
            profile_digest=profile.profile_digest,
            campaign_id="final-campaign",
            phase=FINAL_PHASE,
            manifest_identity=manifest.manifest_identity,
            manifest_digest=manifest.manifest_digest,
            common_closure_digest=admission.common_closure_digest,
            evidence_digest="d" * 64,
            authority_binding=admission.binding,
            evidence_origin=INJECTED_FAKE_ORIGIN,
        )
        second_cell = FinalCellEvidence(
            cell_id=FINAL_SCHEDULE[1],
            profile_digest=profile.profile_digest,
            campaign_id="final-campaign",
            phase=FINAL_PHASE,
            manifest_identity=manifest.manifest_identity,
            manifest_digest=manifest.manifest_digest,
            common_closure_digest=admission.common_closure_digest,
            evidence_digest="e" * 64,
            authority_binding=admission.binding,
            evidence_origin=INJECTED_FAKE_ORIGIN,
        )
        with pytest.raises(ProvenanceError, match="final_cell_launch_required"):
            cell_authority.complete(cell)
        with pytest.raises(ProvenanceError, match="post-launch"):
            cell_authority.consume(cell)
        with pytest.raises(ProvenanceError, match="final_schedule_order"):
            second_authority.consume_for_launch()
        first_permit = cell_authority.consume_for_launch()
        second_permit = second_authority.consume_for_launch()
        assert isinstance(first_permit, FinalLaunchPermit)
        assert isinstance(second_permit, FinalLaunchPermit)
        assert first_permit.cell_id == FINAL_SCHEDULE[0]
        assert second_permit.cell_id == FINAL_SCHEDULE[1]
        with pytest.raises(ProvenanceError, match="authority_replay"):
            cell_authority.consume_for_launch()
        with pytest.raises(ProvenanceError, match="final_schedule_order"):
            second_authority.complete(second_cell)
        assert cell_authority.complete(cell) is cell
        assert second_authority.complete(second_cell) is second_cell
        assert cell_authority.origin == INJECTED_FAKE_ORIGIN
        assert not cell_authority.runtime_admissible
        with pytest.raises(ProvenanceError, match="authority_replay"):
            cell_authority.consume_for_launch()
        with pytest.raises(ProvenanceError, match="authority_replay"):
            cell_authority.complete(cell)
        with pytest.raises(ProvenanceError, match="final_prerequisite_mismatch"):
            admit_final_campaign(
                active_final,
                aggregate,
                aggregate.probes,
                manifest=load_final_campaign_manifest(),
                campaign_id="runtime-substitution",
                evidence_origin=RUNTIME_VERIFIED_ORIGIN,
            )
    finally:
        final_lock.release()
        qledger.close()
        fledger.close()


def test_runtime_qualification_rejects_injected_source_closure(tmp_path):
    helpers = pytest.importorskip("test_minecraft_k12_execution_provenance")
    verifier_key = b"k" * 32
    parent = ParentExecutionAuthority(
        revision_verifier_key=verifier_key,
        revision_verifier_identity="test-verifier/1",
    )
    root = tmp_path / "runtime-qualification"
    root.mkdir(mode=0o700)
    ledger = parent.create_ledger(
        root=root,
        namespace="qualification",
        reservation_id="a" * 64,
        output_root_identity="qualification-output",
    )
    preflight = helpers._preflight(
        parent, ledger, revision_verifier_key=verifier_key,
    )
    try:
        with pytest.raises(ProvenanceError, match="source_closure_incomplete"):
            parent.mint_qualification(preflight, ledger=ledger)
        assert ledger.state == "quarantined"
    finally:
        preflight.target_lease.lock.release()
        ledger.close()


def test_failed_probe_durably_terminalizes_without_passed_event(tmp_path):
    helpers = pytest.importorskip("test_minecraft_k12_execution_provenance")
    profile = helpers.PROFILE
    parent = ParentExecutionAuthority()
    injected = parent.injected_test_controller()
    root = tmp_path / "failed-probe-qualification"
    root.mkdir(mode=0o700)
    ledger = injected.create_ledger(
        root=root,
        namespace="qualification",
        reservation_id="b" * 64,
        output_root_identity="qualification-output",
    )
    try:
        preflight = helpers._preflight(injected, ledger)
        authority = injected.mint_qualification(preflight, now=101, ledger=ledger)
        active = injected.verify_qualification_first_consume(
            authority, helpers._first(preflight), now=102, ledger=ledger,
        )
        cells = []
        for ordinal, cell_id in enumerate(qualification_ids(), 1):
            arm = cell_id.rsplit("-", 1)[-1]
            reset = f"failed-reset-{ordinal}"
            evidence_digest = f"{ordinal + 100:064x}"
            rejection = None
            if arm == "S":
                plan_authority, before, after = _rejection_states(
                    active,
                    K12AuthenticatedProfile.from_runtime_profile(profile),
                    "failed-qualification-campaign",
                    cell_id,
                    reset,
                )
                rejection = RejectionEvidence.mint(
                    active,
                    LiveBinding(
                        profile.profile_digest,
                        "failed-qualification-campaign",
                        cell_id,
                        reset,
                        1,
                        f"failed-request-{ordinal}",
                        f"failed-permit-{ordinal}",
                        f"failed-effect-{ordinal}",
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
                    events={"terminal": "passed"},
                    profile_digest=profile.profile_digest,
                    campaign_id="failed-qualification-campaign",
                    reset_identity=reset,
                    evidence_digest=evidence_digest,
                    fresh_root=True,
                    reset_passed=True,
                    capability_state="REVOKED",
                    provider_terminal="success",
                    oracle_value="not_applicable" if arm == "S" else "true",
                    containment_clean=True,
                    evidence_valid=True,
                    authority_binding=active.binding,
                    evidence_origin=INJECTED_FAKE_ORIGIN,
                    rejection_evidence=rejection,
                    reset_token=reset,
                    generation=1,
                    request_identity=f"failed-request-{ordinal}",
                    permit_identity=f"failed-permit-{ordinal}",
                    effect_identity=f"failed-effect-{ordinal}",
                )
            )
        probes = tuple(
            LiveQualificationProbeEvidence(
                probe=probe,
                passed=probe != "P3",
                profile_digest=profile.profile_digest,
                campaign_id="failed-probe-campaign",
                evidence_digest=f"failed-probe-{probe}",
                authority_binding=active.authority.binding("qualification_probe"),
                evidence_origin=INJECTED_FAKE_ORIGIN,
            )
            for probe in PROBES
        )
        with pytest.raises(ValueError, match="qualification probes failed"):
            aggregate_live_qualification(active, tuple(cells), probes, ledger=ledger)
        assert ledger.state == "terminal"
        payload = dict(ledger.events[-1].payload)
        assert payload["result"] == "failed"
        assert payload["failure_reason"] == "qualification_probe_failed"
        assert payload["terminal_verified"] is False
        assert all(event.payload.get("result") != "passed" for event in ledger.events)
    finally:
        ledger.close()
