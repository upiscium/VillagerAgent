import pytest
from benchmarks.minecraft.k12_live_oracle import *
from benchmarks.minecraft.k12_live_state import *
from benchmarks.minecraft.k12_guarded_backend import K12AuthenticatedProfile
from benchmarks.minecraft.k12_runtime_profile import load_k12_live_runtime_profile

LOADED=load_k12_live_runtime_profile(); PROFILE=LOADED.profile_digest
AUTHORITY=ParentPlanAuthority(K12AuthenticatedProfile.from_runtime_profile(LOADED),"c")

def test_binding_and_truth_contract_are_fail_closed():
    assert evaluate("other", None, None, arm="A") is Truth.NOT_APPLICABLE
    assert evaluate("S1", None, None, arm="A") is Truth.UNKNOWN
    with pytest.raises(LiveStateError): normalize_mock_state(profile="p", campaign="c", cell="x", reset_token="r", generation=1)

def test_rejection_evidence_is_typed_and_authenticated():
    b = LiveBinding("p", "c", "x", "r", 1, "q", "permit", "effect", "0"*64, "S")
    with pytest.raises(LiveStateError): RejectionEvidence(b, True, 1, "0" * 64)

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
