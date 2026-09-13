from benchmarks.minecraft.k12_live_validation import *
from benchmarks.minecraft.k12_live_containment import Descendant, LiveContainment, MockContainmentIO, parse_observation
from benchmarks.minecraft.k12_live_qualification import (MockCellQualificationEvidence,
    MockProbeEvidence, qualification_ids, qualify_mock_campaign, qualify_mock_probes)
from benchmarks.minecraft.k12_runtime_profile import load_k12_live_runtime_profile

def passing_aggregate(profile):
    values=[]
    for cell_id in qualification_ids():
        values.append(MockCellQualificationEvidence(cell_id,{"status":"passed"},profile.profile_digest,
            "qualification-campaign","reset-"+cell_id,"evidence-"+cell_id,True,True,
            "REVOKED","success","not_applicable" if cell_id.endswith("-S") else "true",True,True))
    return qualify_mock_campaign(tuple(values))

def test_containment_validation_requires_empty_cgroup_and_events_zero():
    show="MainPID=0\nControlGroup=/cg\nActiveState=inactive\nSubState=dead"
    running="MainPID=1\nControlGroup=/cg\nActiveState=active\nSubState=running"
    main=Descendant(1,1,"/mock/worker",0,1,"/cg")
    observation=LiveContainment(MockContainmentIO((running,running,show),waits=(True,),descendants=((main,),(main,),()),
        cgroup_sources=(("/cg",(1,),1),("/cg",(1,),1),("/cg",(),0))),unit="u",cgroup="/cg",clock=lambda:0).stop()
    assert validate_live_containment(observation, "/cg")[0]
    bad = parse_observation(MockContainmentIO((show,),descendants=((),),
        cgroup_sources=(("/cg",(),1),)),"u","/cg")
    assert not validate_live_containment(bad, "/cg")[0]

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
