import pytest
from benchmarks.minecraft.k12_live_oracle import *
from benchmarks.minecraft.k12_live_state import *
from benchmarks.minecraft.k12_guarded_backend import K12AuthenticatedProfile
from benchmarks.minecraft.k12_execution_provenance import ParentExecutionAuthority
from benchmarks.minecraft.k12_runtime_profile import load_k12_live_runtime_profile

LOADED=load_k12_live_runtime_profile(); PROFILE=LOADED.profile_digest
AUTHORITY=ParentPlanAuthority(K12AuthenticatedProfile.from_runtime_profile(LOADED),"c")

def test_binding_and_truth_contract_are_fail_closed():
    assert evaluate("other", None, None, arm="A") is Truth.NOT_APPLICABLE
    assert evaluate("S1", None, None, arm="A") is Truth.UNKNOWN
    with pytest.raises(LiveStateError): normalize_mock_state(profile="p", campaign="c", cell="x", reset_token="r", generation=1)

def test_rejection_evidence_is_typed_and_authenticated():
    b = LiveBinding("p", "c", "x", "r", 1, "q", "permit", "effect", "0"*64, "S")
    with pytest.raises(TypeError): RejectionEvidence(b, True, 0, "0" * 64)


def test_expected_rejection_is_not_task_success():
    assert qualification_truth("A", Truth.TRUE)
    assert qualification_truth("R", Truth.TRUE)
    assert qualification_truth("S", Truth.NOT_APPLICABLE, rejection_verified=True)
    assert not qualification_truth("S", Truth.NOT_APPLICABLE)
    assert expected_rejection_is_not_success("S", Truth.NOT_APPLICABLE)


def test_rejection_evidence_origin_is_separate_from_operational_binding():
    binding = LiveBinding("p", "c", "x", "r", 1, "q", "permit", "effect", "0" * 64, "S")
    with pytest.raises(TypeError): RejectionEvidence(binding, True, 0, "a" * 64)
    with pytest.raises(TypeError): RejectionEvidence(
        binding, True, 0, "b" * 64, RUNTIME_VERIFIED_ORIGIN,
    )

def test_oracle_contract_digest_is_authenticated():
    assert len(load_oracle_contract()["detached_artifact_sha256"]) == 64

def _s2_state(block,count,purpose):
    commands=(execute_if_block((2,64,0),block),count_items("agent","stone"),data_inventory("agent"))
    plan=AUTHORITY.mint(commands,cell="x",reset_token="r",generation=1,descriptor="S2",purpose=purpose)
    inventory="[]" if count==0 else '[{Slot:0b,id:"minecraft:stone",Count:1b}]'
    raw=("Test passed, count: 1",
         "No items were found on player agent" if count==0 else "Found 1 matching item on player agent",
         f"agent has the following entity data: {inventory}")
    return normalize_state(plan,execute_plan(plan,MockTransport(dict(zip((c.text for c in commands),raw)))))

def test_oracle_consumes_only_plan_result_normalized_s2_state():
    before=_s2_state("air",1,"before"); after=_s2_state("stone",0,"after")
    binding=LiveBinding(PROFILE,"c","x","r",1,"request","permit","effect",AUTHORITY.authority_id,"A")
    assert evaluate("S2",before,after,binding=binding,observed_binding=binding,
                    position=(2,64,0),sender="agent",item="stone") is Truth.TRUE


def _rejection_fixture(tmp_path):
    helpers = pytest.importorskip("test_minecraft_k12_execution_provenance")
    parent = ParentExecutionAuthority()
    controller = parent.injected_test_controller()
    root = tmp_path / "injected"
    root.mkdir(mode=0o700)
    reservation = "a" * 64
    ledger = controller.create_ledger(
        root=root,
        namespace="qualification",
        reservation_id=reservation,
        output_root_identity=f"qualification-output-{reservation[:4]}",
    )
    preflight = helpers._preflight(controller, ledger)
    qualification = controller.mint_qualification(preflight, now=101, ledger=ledger)
    active = helpers.verify_qualification_first_consume(
        qualification,
        helpers._first(preflight),
        now=102,
        ledger=ledger,
    )
    profile = K12AuthenticatedProfile.from_runtime_profile(helpers.PROFILE)
    campaign = "qualification-rejection"
    cell = "S1-S"
    reset_token = "rejection-reset"
    plan_authority = ParentPlanAuthority(profile, campaign, authority=active)
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
    binding = LiveBinding(
        helpers.PROFILE.profile_digest,
        campaign,
        cell,
        reset_token,
        1,
        "request-rejection",
        "permit-rejection",
        "effect-rejection",
        plan_authority.authority_id,
        "S",
    )
    rejection = RejectionEvidence.mint(
        active,
        binding,
        states[0],
        states[1],
        evidence_digest="d" * 64,
    )
    return ledger, controller, binding, states[0], states[1], rejection, preflight.target_lease


def test_rejection_evidence_is_revalidated_after_parent_revocation(tmp_path):
    ledger, controller, binding, before, after, rejection, lease = _rejection_fixture(tmp_path)
    try:
        kwargs = dict(
            binding=binding,
            observed_binding=binding,
            position=(0, 64, 0),
            sender="agent",
            item="stone",
            arm="S",
            rejection=rejection,
        )
        assert evaluate("S1", before, after, **kwargs) is Truth.NOT_APPLICABLE
        controller.quarantine_ledger(ledger, {"reason": "post-mint revocation"})
        assert evaluate("S1", before, after, **kwargs) is Truth.UNKNOWN
    finally:
        lease.lock.release()
        ledger.close()
