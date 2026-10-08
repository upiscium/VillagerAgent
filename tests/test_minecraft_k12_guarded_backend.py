import threading

import pytest

from benchmarks.common.eac.canonical import canonical_sha256
from benchmarks.minecraft.k12_guarded_backend import *
from benchmarks.minecraft.k12_runtime_profile import load_k12_live_runtime_profile

PROFILE = load_k12_live_runtime_profile()
IDS = dict(profile_id=PROFILE.profile_id, campaign_id="c", cohort_id="h", cell_id="cell", triplet_id="t",
           arm_id="a", actor_id="actor", action_id="MineBlock", request_namespace="ns", runtime_id="r",
           tool_id="tool", process_id="proc", unit_id="unit", cgroup_id="cg", proposal_id="prop",
           request_id="req", candidate_id="cand", attempt_id="att", permit_id="permit", nonce="nonce",
           argument_digest=canonical_sha256({"x": 1, "y": 2, "z": 3}))


def setup():
    parent = K12ParentAuthority(K12AuthenticatedProfile.from_runtime_profile(PROFILE), "c")
    tool = K12ScriptedMockTool()
    handle = parent.handle(tool)
    return parent.mint(handle, **IDS), handle, parent, tool


def admitted():
    cap, handle, parent, tool = setup()
    parent.bind(cap, **IDS); parent.validate_request(cap, **IDS); parent.issue_permit(cap, **IDS)
    entry = parent.admit_effect(cap, "MineBlock", ids=IDS, x=1, y=2, z=3)
    return cap, handle, parent, tool, entry


def test_parent_minted_fsm_and_parent_evidence():
    cap, handle, parent, tool, entry = admitted()
    assert parent.state(cap) is CapabilityState.PERMIT_ISSUED
    assert K12GuardedBackend().execute("MineBlock", cap, ids=IDS, entry=entry, x=1, y=2, z=3)["status"]
    assert parent.state(cap) is CapabilityState.REVOKED and len(tool.calls) == 1
    assert all(item["authority"] == "parent" for item in parent.evidence)


def test_holder_cannot_transition_or_backend_admit():
    cap, handle, parent, tool = setup()
    assert not any(hasattr(cap,name) for name in ("bind","validate_request","issue_permit","enter_effect","terminal","revoke"))
    assert parent.state(cap) is CapabilityState.CREATED
    backend = K12GuardedBackend()
    with pytest.raises(K12GuardedBackendError): backend.execute("MineBlock", cap, ids=IDS, x=1, y=2, z=3)
    assert parent.state(cap) is CapabilityState.CREATED and tool.calls == []
    assert not hasattr(cap,"_K12GuardedToolCapability__token")
    assert not hasattr(cap,"_transition") and not hasattr(cap,"_poison") and not hasattr(cap,"_invoke")


def test_wrong_ids_poison_and_bare_handle_cannot_mint():
    cap, handle, parent, tool = setup(); parent.bind(cap, **IDS)
    bad = dict(IDS); bad["nonce"] = "wrong"
    with pytest.raises(K12GuardedBackendError): parent.validate_request(cap, **bad)
    assert parent.state(cap) is CapabilityState.POISONED
    with pytest.raises(TypeError): K12GuardedToolHandle(lambda: None, parent)
    with pytest.raises(TypeError): K12GuardedToolHandle({}, parent)


def test_foreign_parent_cannot_poison_or_record_capability():
    cap,handle,parent,tool=setup()
    foreign=K12ParentAuthority(K12AuthenticatedProfile.from_runtime_profile(PROFILE),"c")
    with pytest.raises(K12GuardedBackendError,match="parent-owned"): foreign.poison(cap,**IDS)
    assert parent.state(cap) is CapabilityState.CREATED and foreign.evidence==()


def test_digest_and_entry_identity_are_parent_validated():
    cap, handle, parent, tool = setup(); parent.bind(cap, **IDS); parent.validate_request(cap, **IDS); parent.issue_permit(cap, **IDS)
    with pytest.raises(K12GuardedBackendError): parent.admit_effect(cap, "MineBlock", ids=IDS, x=1, y=2, z=4)
    assert parent.state(cap) is CapabilityState.POISONED and tool.calls == []


def test_concurrent_admission_and_consumption_is_one_shot():
    cap, handle, parent, tool, entry = admitted()
    results, errors = [], []
    def run():
        try: results.append(K12GuardedBackend().execute("MineBlock", cap, ids=IDS, entry=entry, x=1, y=2, z=3))
        except Exception as exc: errors.append(exc)
    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert len(results) == 1 and len(errors) == 1 and len(tool.calls) == 1
    assert parent.state(cap) is CapabilityState.REVOKED
