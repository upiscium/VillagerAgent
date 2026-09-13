"""Non-executing, fully bound S1--S5 reset plans."""
from __future__ import annotations
import json
import hashlib
from pathlib import Path
import re
from benchmarks.minecraft.k12_live_state import *
from benchmarks.minecraft.k12_runtime_profile import strict_json_load

def reset_plan(stratum: str, *, authority:ParentPlanAuthority, cell="cell", reset_token="token", generation=1, origin=(0,64,0), support=(0,63,0), actor="agent", recipient="recipient", item="stone", zombie_uuid=None, fixture=None):
    if stratum not in {"S1","S2","S3","S4","S5"}: raise LiveStateError("invalid stratum")
    if not isinstance(authority,ParentPlanAuthority): raise LiveStateError("parent plan authority required")
    profile,campaign=authority.profile.profile_digest,authority.campaign
    origin=pos(origin); support=pos(support); actor=actor; recipient=recipient
    if fixture is not None:
        from benchmarks.minecraft.k12_live_fixture import K12LiveFixture
        if not isinstance(fixture,K12LiveFixture) or fixture.stratum!=stratum: raise LiveStateError("fixture/stratum mismatch")
        geometry=fixture.geometry; actor_pos=geometry.actor_start; target_pos=geometry.target
        support_pos=geometry.support; center=geometry.observation_center; radius=geometry.radius
        if geometry.recipient_start is not None: recipient_pos=geometry.recipient_start
        else: recipient_pos=(actor_pos[0]+1,actor_pos[1],actor_pos[2])
        values=dict(fixture.arguments); actor=values.get("actor",actor); recipient=values.get("recipient",recipient); item=values.get("item",item)
    else:
        actor_pos=origin; target_pos=origin; support_pos=support; center=origin; radius=16; recipient_pos=(origin[0]+1,origin[1],origin[2])
    if fixture is not None: chunks=fixture.geometry.force_loaded_chunks
    else:
        chunks=tuple((x*16,z*16) for x in range((center[0]-radius)//16,(center[0]+radius)//16+1)
                     for z in range((center[2]-radius)//16,(center[2]+radius)//16+1))
    load=tuple(command for x,z in chunks for command in (forceload_add(x,z),forceload_query(x,z)))
    if stratum == "S1":
        cs=load+(gamerule("doImmediateRespawn","true"), actor_exists(actor), teleport(actor,actor_pos), data_pos(actor), setblock(support_pos,"stone"), setblock(target_pos,"stone"), clear_all(actor), count_items(actor,item), data_inventory(actor), execute_if_block(target_pos,"stone"))
    elif stratum == "S2":
        cs=load+(actor_exists(actor), teleport(actor,actor_pos), data_pos(actor), setblock(support_pos,"stone"), setblock(target_pos,"air"), clear_all(actor), give(actor,item,1), count_items(actor,item), data_inventory(actor), execute_if_block(target_pos,"air"), execute_if_block(support_pos,"stone"))
    elif stratum == "S3":
        cs=load+(actor_exists(actor), teleport(actor,actor_pos), setblock(support_pos,"stone"), setblock((target_pos[0],target_pos[1]-1,target_pos[2]),"stone"), data_pos(actor), execute_if_block(support_pos,"stone"))
    elif stratum == "S4":
        if zombie_uuid is not None: bound_uuid(zombie_uuid)
        tag="k12_"+hashlib.sha256(f"{profile}:{campaign}:{cell}:{reset_token}:{generation}".encode()).hexdigest()[:16]
        target=tag_selector(tag,16)
        cs=load+(kill_competing(center,radius), summon_zombie(target_pos,tag), gamerule("doMobLoot","false"), objective_remove("k12_kill"), objective_remove("k12_cardinality"), objective_remove("k12_total_zombies"), objective_add("k12_kill", "minecraft.killed:minecraft.zombie"), objective_add("k12_cardinality"), objective_add("k12_total_zombies"), kill_score(actor, "k12_kill", 0), score_query(actor,"k12_kill"), cardinality(tag_count_selector(tag,radius),"k12_cardinality",center), cardinality(zombie_count_selector(radius),"k12_total_zombies",center), entity_uuid(target,center), entity_health(target,center), actor_exists(actor), teleport(actor,actor_pos), data_pos(actor))
    else:
        cs=load+(actor_exists(actor), actor_exists(recipient), teleport(actor,actor_pos), teleport(recipient,recipient_pos), data_pos(actor), data_pos(recipient), clear_all(actor), clear_all(recipient), give(actor,item,1), count_items(actor,item), count_items(recipient,item), data_inventory(actor), data_inventory(recipient), kill_items(center,radius), residual_census(center,radius))
    return authority.mint(cs,cell=cell,reset_token=reset_token,generation=generation,descriptor=stratum,purpose="before")

def execute_reset(plan, transport):
    consume_reset_token(plan.reset_token, plan.generation, profile=plan.profile,
                        campaign=plan.campaign, cell=plan.cell)
    return execute_plan(plan, transport)

def residual_readback_plan(*,authority:ParentPlanAuthority,census_plan:CommandPlan,census_results:tuple[CommandResult,...],after_commands:tuple[RconCommand,...]):
    if (not isinstance(census_plan,CommandPlan) or len(census_plan.commands)!=1
            or census_plan.commands[0].kind is not CommandKind.RESIDUAL_CENSUS or not census_plan.authority_digest
            or census_plan.descriptor!="S5" or census_plan.purpose!="census"
            or type(census_results) is not tuple or len(census_results)!=1): raise LiveStateError("authenticated residual census required")
    census=normalize_state(census_plan,census_results)
    expected_count=census.residual_count
    if type(expected_count) is not int or not 0<=expected_count<=64: raise LiveStateError("invalid residual census count")
    profile,campaign,cell,reset_token,generation=(census.profile,census.campaign,census.cell,census.reset_token,census.generation)
    if (not isinstance(authority,ParentPlanAuthority) or not authority.owns(census_plan)
            or authority.profile.profile_digest!=profile or authority.campaign!=campaign): raise LiveStateError("residual parent authority mismatch")
    census_args=dict(census_plan.commands[0].args); center=census_args["position"]; radius=census_args["radius"]
    tags=tuple("k12_item_"+hashlib.sha256(f"{profile}:{campaign}:{cell}:{reset_token}:{generation}:{index}".encode()).hexdigest()[:16] for index in range(expected_count))
    if type(after_commands) is not tuple or not {"positions","inventories"}<={domain for command in after_commands for domain in command.completeness}: raise LiveStateError("complete S5 after read-back required")
    commands=[*after_commands,residual_census(center,radius)]
    for index,tag in enumerate(tags):
        commands.extend((select_residual(center,radius,tag,tags[:index]),item_uuid(tag,center,radius),item_snbt(tag,center,radius)))
    return authority.mint(commands,cell=cell,reset_token=reset_token,generation=generation,
        descriptor="S5",purpose="after",upstream_digest=census_results[0].digest)

def load_reset_contract(path=None):
    path=Path(path or Path(__file__).with_name("k12_live_reset_readback_v1.json"))
    try: data=strict_json_load(path,"K12 live reset contract")
    except (OSError, ValueError) as e: raise LiveStateError("unreadable reset contract") from e
    digest=data.get("detached_artifact_sha256")
    if (set(data).intersection({"detached_digest"}) or type(digest) is not str
            or not re.fullmatch(r"[0-9a-f]{64}",digest)
            or digest != detached_artifact_digest(data)):
        raise LiveStateError("invalid detached artifact digest")
    if data.get("schema")!="minecraft-k12-live-reset-readback/1" or data.get("acknowledgement")!="en_us": raise LiveStateError("invalid reset contract")
    return data

def make_s4_reset_plan(**kwargs): return reset_plan("S4",**kwargs)
__all__=["reset_plan","execute_reset","residual_readback_plan","load_reset_contract","make_s4_reset_plan"]
