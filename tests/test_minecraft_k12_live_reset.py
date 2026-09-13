import pytest
from benchmarks.minecraft.k12_live_reset import reset_plan, residual_readback_plan, load_reset_contract
from benchmarks.minecraft.k12_guarded_backend import K12AuthenticatedProfile
from benchmarks.minecraft.k12_live_state import (LiveStateError, MockTransport, ParentPlanAuthority,
    count_items, data_inventory, data_pos, execute_plan, residual_census)
from benchmarks.minecraft.k12_runtime_profile import load_k12_live_runtime_profile
PROFILE=load_k12_live_runtime_profile()
def auth(): return ParentPlanAuthority(K12AuthenticatedProfile.from_runtime_profile(PROFILE),"c")
def test_reset_plans_are_bound_and_complete():
    for s in "S1 S2 S3 S5".split():
        p=reset_plan(s,authority=auth(),cell="x",reset_token="r",generation=2)
        assert p.profile==PROFILE.profile_digest and p.generation==2 and all(c.read_after_write for c in p.commands)
def test_s4_requires_no_bound_target_and_summons_tagged_target():
    p=reset_plan("S4",authority=auth()); text=" ".join(c.text for c in p.commands)
    assert "summon zombie" in text and "NoAI" in text and "Health:20f" in text
    assert "execute positioned" in text and "minecraft.killed:minecraft.zombie" in text
    assert "distance=..16" in text and "tp agent 0 64 0" in text

def test_s2_and_s5_establish_exact_inventory_and_contract_is_authenticated():
    s2=";".join(c.text for c in reset_plan("S2",authority=auth()).commands)
    assert "clear agent;" in s2 and "give agent stone 1" in s2
    s5=";".join(c.text for c in reset_plan("S5",authority=auth()).commands)
    assert "clear agent;" in s5 and "clear recipient;" in s5 and "give agent stone 1" in s5
    assert len(load_reset_contract()["detached_artifact_sha256"]) == 64
    assert "kill @e[type=item,distance=..16]" in s5
    assert s5.rindex("distance=..16") < len(s5)
    census_command=residual_census((0,64,0),16)
    authority=auth(); census_plan=authority.mint((census_command,),cell="x",reset_token="r",generation=2,descriptor="S5",purpose="census")
    census_results=execute_plan(census_plan,MockTransport({census_command.text:"Test passed, count: 2"}))
    after=(data_pos("agent"),count_items("agent","stone"),data_inventory("agent"))
    post=residual_readback_plan(authority=authority,census_plan=census_plan,census_results=census_results,after_commands=after)
    assert sum("data get entity @e[type=item" in command.text for command in post.commands)==4
    assert sum("run tag @s add k12_item_" in command.text for command in post.commands)==2
    assert all("distance=..16" in command.text for command in post.commands[len(after):])
    assert post.upstream_digest==census_results[0].digest
    with pytest.raises(LiveStateError,match="authority"):
        residual_readback_plan(authority=auth(),census_plan=census_plan,census_results=census_results,after_commands=after)
