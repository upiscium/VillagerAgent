"""Fail-closed parent oracle for the five live mock strata."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from math import dist
from typing import Any, Sequence
import json
from pathlib import Path
import re
from benchmarks.minecraft.k12_live_state import (LiveState, LiveStateError, bound_uuid,
    detached_artifact_digest, validate_state)
from benchmarks.minecraft.k12_runtime_profile import strict_json_load

class Truth(str,Enum): TRUE="true"; FALSE="false"; UNKNOWN="unknown"; NOT_APPLICABLE="not_applicable"
OracleValue=Truth
@dataclass(frozen=True,slots=True)
class LiveBinding:
    profile:str; campaign:str; cell:str; reset_token:str; generation:int; request:str; permit:str; effect:str; authority_id:str; arm:str="A"
    def matches(self, other): return isinstance(other,LiveBinding) and self==other and self.generation>0
@dataclass(frozen=True,slots=True)
class PollSample:
    elapsed_ms:int; state:LiveState
@dataclass(frozen=True,slots=True)
class RejectionEvidence:
    binding: LiveBinding; current_inadmissible: bool; native_entries: int; evidence_digest: str
    def __post_init__(self):
        if (self.current_inadmissible is not True or self.native_entries != 0
                or not re.fullmatch(r"[0-9a-f]{64}",self.evidence_digest)):
            raise LiveStateError("invalid stale-rejection evidence")
def _bound(a:LiveBinding|None,b:LiveBinding|None,before,after):
    return a is not None and a.matches(b) and before is not None and after is not None and before.profile==a.profile and after.profile==a.profile and before.campaign==a.campaign and after.campaign==a.campaign and before.cell==a.cell and after.cell==a.cell and before.reset_token==a.reset_token and after.reset_token==a.reset_token and before.generation==a.generation and after.generation==a.generation and before.plan_authority_id==a.authority_id and after.plan_authority_id==a.authority_id
def _ready(s, required, descriptor, purpose):
    try: validate_state(s)
    except LiveStateError: return False
    return (bool(s.plan_authority_digest) and bool(s.plan_authority_id)
            and s.plan_descriptor==descriptor and s.plan_purpose==purpose
            and s.read_after_write and set(required) <= set(s.complete))
def _score(state, player, objective):
    rows=[x for x in state.scoreboard if isinstance(x,(tuple,list)) and len(x)==3 and x[0]==player and x[1]==objective]
    return rows[0][2] if len(rows)==1 and type(rows[0][2]) is int else None
def _target(state, uuid):
    rows=[x for x in state.entities if isinstance(x,(tuple,list)) and len(x)>=2 and x[0]==uuid]
    return rows[0] if len(rows)==1 else None
def _unchanged(stratum,before,after,position,sender,recipient):
    if stratum in {"S1","S2"}: return dict(before.blocks).get(tuple(position or ()))==dict(after.blocks).get(tuple(position or ())) and dict(before.inventories).get(sender)==dict(after.inventories).get(sender)
    if stratum=="S3": return dict(before.positions).get(sender)==dict(after.positions).get(sender)
    if stratum=="S4": return before.entities==after.entities and before.scoreboard==after.scoreboard
    return (dict(before.inventories).get(sender)==dict(after.inventories).get(sender)
            and dict(before.inventories).get(recipient)==dict(after.inventories).get(recipient)
            and before.residual==after.residual)
def _has_objective(stratum,before,after,position,item,sender,recipient,target_uuid):
    if stratum in {"S1","S2"}:
        p=tuple(position) if position is not None else None
        return (p is not None and p in dict(before.blocks) and p in dict(after.blocks)
                and sender in dict(before.inventories) and sender in dict(after.inventories)
                and item in dict(dict(before.inventories)[sender])
                and item in dict(dict(after.inventories)[sender]))
    if stratum=="S3": return sender in dict(before.positions) and sender in dict(after.positions)
    if stratum=="S4":
        try: u=bound_uuid(target_uuid)
        except LiveStateError: return False
        return _target(before,u) is not None and _score(before,sender,"k12_kill") is not None
    return (sender in dict(before.inventories) and sender in dict(after.inventories)
            and recipient in dict(before.inventories) and recipient in dict(after.inventories)
            and sender in dict(before.positions) and sender in dict(after.positions)
            and recipient in dict(before.positions) and recipient in dict(after.positions))
def evaluate(stratum:str,before:LiveState|None,after:LiveState|None,*,binding:LiveBinding|None=None,observed_binding:LiveBinding|None=None,position=None,item="stone",sender="agent",recipient="recipient",target_uuid=None,requested_quantity=1,polls:Sequence[PollSample]=(),arm="A",elapsed_seconds=None,rejection:RejectionEvidence|None=None)->Truth:
    if stratum not in {"S1","S2","S3","S4","S5"}: return Truth.NOT_APPLICABLE if arm in {"A","R","S"} else Truth.UNKNOWN
    if arm not in {"A","R","S"}: return Truth.NOT_APPLICABLE
    required={"S1":{"blocks","inventories"},"S2":{"blocks","inventories"},"S3":{"positions"},"S4":{"entities","scoreboard"},"S5":{"positions","inventories","residual"}}[stratum]
    if binding is None or binding.arm!=arm or not _bound(binding,observed_binding,before,after) or not _ready(before,required,stratum,"before") or not _ready(after,required,stratum,"after"): return Truth.UNKNOWN
    if not _has_objective(stratum,before,after,position,item,sender,recipient,target_uuid): return Truth.UNKNOWN
    if arm == "S":
        return Truth.NOT_APPLICABLE if (isinstance(rejection,RejectionEvidence)
            and rejection.binding==binding and _unchanged(stratum,before,after,position,sender,recipient)) else Truth.UNKNOWN
    if stratum=="S3":
        if position is None:return Truth.UNKNOWN
        p=dict(after.positions).get(sender)
        try:return Truth.TRUE if p is not None and dist(p,position)<=1 else Truth.FALSE
        except (TypeError,ValueError):return Truth.UNKNOWN
    if stratum in {"S1","S2"}:
        p=tuple(position) if position is not None else None
        if p is None:return Truth.UNKNOWN
        bb=dict(before.blocks); ab=dict(after.blocks); bi=dict(dict(before.inventories).get(sender,())); ai=dict(dict(after.inventories).get(sender,()))
        block="air" if stratum=="S1" else "stone"; delta=1 if stratum=="S1" else -1
        ok=bb.get(p)==("stone" if stratum=="S1" else "air") and ab.get(p)==block and ai.get(item,0)==bi.get(item,0)+delta
        return Truth.TRUE if ok else Truth.FALSE if (bb==ab and bi==ai) else Truth.UNKNOWN
    if stratum=="S4":
        try:u=bound_uuid(target_uuid)
        except LiveStateError:return Truth.UNKNOWN
        initial=_target(before,u); final=_target(after,u)
        # The reset must prove a unique, full-health target before judging its
        # death; a missing/ambiguous target is evidence failure, not success.
        initial_card=_score(before,"#count","k12_cardinality")
        initial_total=_score(before,"#count","k12_total_zombies")
        final_card=_score(after,"#count","k12_cardinality")
        final_total=_score(after,"#count","k12_total_zombies")
        before_score=_score(before,sender,"k12_kill")
        after_score=_score(after,sender,"k12_kill")
        if (initial is None or len(initial) < 3 or initial[1] != 20 or initial[2] is not True
                or initial_card != 1 or initial_total != 1 or before_score != 0): return Truth.UNKNOWN
        if len([e for e in after.entities if isinstance(e,(tuple,list)) and len(e)>=1 and e[0]==u]) > 1: return Truth.UNKNOWN
        alive = final is not None and len(final)>=3 and final[2] is True
        death = final is None and final_card == 0 and final_total == 0 and after_score == 1
        return Truth.TRUE if death else Truth.FALSE if alive and final_card == 1 and final_total == 1 and after_score == 0 else Truth.UNKNOWN
    if type(requested_quantity) is not int or requested_quantity<1:return Truth.UNKNOWN
    expected=(0,250,500,750,1000,1250,1500,1750,2000,2250,2500,2750,3000,3250,3500,3750,4000,4250,4500,4750,5000)
    if elapsed_seconds is not None and (type(elapsed_seconds) not in {int,float} or elapsed_seconds>5): return Truth.UNKNOWN
    if (tuple(p.elapsed_ms for p in polls) != expected
            or any(not _ready(p.state,required,"S5","after") or not _bound(binding, observed_binding, p.state, p.state)
                   for p in polls)
            or not polls or polls[-1].state != after): return Truth.UNKNOWN
    sb,sa=dict(dict(before.inventories).get(sender,())),dict(dict(after.inventories).get(sender,())); rb,ra=dict(dict(before.inventories).get(recipient,())),dict(dict(after.inventories).get(recipient,()))
    if any(not isinstance(r,(tuple,list)) or len(r)!=3 or type(r[2]) is not int or r[2]<1
           for r in after.residual): return Truth.UNKNOWN
    relevant_residual=sum(r[2] for r in after.residual if r[1]==item)
    other_residual=any(r[1]!=item for r in after.residual)
    ok=sa.get(item,0)==sb.get(item,0)-requested_quantity and ra.get(item,0)==rb.get(item,0)+requested_quantity
    unchanged=(sa.get(item,0)==sb.get(item,0) and ra.get(item,0)==rb.get(item,0))
    dropped=(sa.get(item,0)==sb.get(item,0)-requested_quantity
             and ra.get(item,0)==rb.get(item,0) and relevant_residual==requested_quantity)
    if ok and relevant_residual==0 and not other_residual: return Truth.TRUE
    if (unchanged and relevant_residual==0 and not other_residual) or (dropped and not other_residual): return Truth.FALSE
    return Truth.UNKNOWN
def load_oracle_contract(path=None):
    path=Path(path or Path(__file__).with_name("k12_live_oracle_v1.json"))
    try: data=strict_json_load(path,"K12 live oracle contract")
    except (OSError, ValueError) as e: raise LiveStateError("unreadable oracle contract") from e
    digest=data.get("detached_artifact_sha256")
    if ("detached_digest" in data or type(digest) is not str
            or not re.fullmatch(r"[0-9a-f]{64}",digest)
            or digest != detached_artifact_digest(data)):
        raise LiveStateError("invalid detached artifact digest")
    if data.get("schema")!="minecraft-k12-live-oracle/1" or data.get("acknowledgement")!="en_us": raise LiveStateError("invalid oracle contract")
    return data
__all__=["Truth","OracleValue","LiveBinding","PollSample","RejectionEvidence","evaluate","load_oracle_contract"]
