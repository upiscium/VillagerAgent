"""Closed, offline contract for the K12 live-Minecraft gate.

There is intentionally no RCON client here.  Commands are rendered only by the
typed factories below and can only be sent to :class:`MockTransport`.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import hashlib, json, re, secrets
from typing import Any, Mapping, Sequence
from benchmarks.common.eac.canonical import canonical_bytes

class LiveStateError(ValueError): pass
IDENT = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
ITEM = re.compile(r"^[a-z0-9_./:-]{1,64}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
MAX_COMMANDS, MAX_TEXT, MAX_ROWS = 128, 4096, 1024
MAX_TIMEOUT_MS = 30_000

def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
def detached_artifact_digest(value: Mapping[str, Any]) -> str:
    body = {key: item for key, item in value.items()
            if key != "detached_artifact_sha256"}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()
def _ident(v: Any, label="identifier") -> str:
    if type(v) is not str or not IDENT.fullmatch(v): raise LiveStateError(f"invalid {label}")
    return v
def coord(v: Any) -> int:
    if type(v) is not int or not -30_000_000 <= v <= 30_000_000: raise LiveStateError("invalid coordinate")
    return v
def pos(v: Any) -> tuple[int, int, int]:
    if not isinstance(v, (tuple, list)) or len(v) != 3: raise LiveStateError("invalid position")
    return tuple(coord(x) for x in v)  # type: ignore
def qty(v: Any) -> int:
    if type(v) is not int or not 0 <= v <= 64: raise LiveStateError("invalid quantity")
    return v
def bounded_int(v: Any, *, minimum: int, maximum: int, label: str) -> int:
    if type(v) is not int or not minimum <= v <= maximum: raise LiveStateError(f"invalid {label}")
    return v
def item(v: Any) -> str:
    if type(v) is not str or not ITEM.fullmatch(v): raise LiveStateError("invalid item")
    return v
def _state_item(v: Any) -> str:
    return item(v).removeprefix("minecraft:")
def tag_selector(tag: str, radius:int=16) -> str:
    return f'@e[type=zombie,tag={_ident(tag,"entity tag")},distance=..{bounded_int(radius,minimum=1,maximum=64,label="radius")},limit=1]'
def tag_count_selector(tag: str, radius:int=16) -> str:
    return f'@e[type=zombie,tag={_ident(tag,"entity tag")},distance=..{bounded_int(radius,minimum=1,maximum=64,label="radius")}]'
def zombie_count_selector(radius:int=16) -> str:
    return f'@e[type=zombie,distance=..{bounded_int(radius,minimum=1,maximum=64,label="radius")}]'
def _selector(v: Any) -> str:
    if type(v) is not str or not re.fullmatch(r"@e\[type=zombie,tag=[A-Za-z0-9_.:-]{1,64},distance=\.\.(?:[1-9]|[1-5]\d|6[0-4]),limit=1\]", v):
        raise LiveStateError("invalid bounded selector")
    return v
def _count_selector(v: Any) -> str:
    if type(v) is not str or not re.fullmatch(r"@e\[type=zombie(?:,tag=[A-Za-z0-9_.:-]{1,64})?,distance=\.\.(?:[1-9]|[1-5]\d|6[0-4])\]",v):
        raise LiveStateError("invalid bounded count selector")
    return v
def bound_uuid(v: Any) -> str:
    if type(v) is not str or not UUID_RE.fullmatch(v): raise LiveStateError("invalid UUID")
    return v.lower()

class CommandKind(str, Enum):
    FORCELOAD_ADD="forceload_add"; FORCELOAD_QUERY="forceload_query"; GAMERULE="gamerule"
    ACTOR_EXISTS="actor_exists"; TP="tp"; CLEAR_ALL="clear_all"; CLEAR="clear"; GIVE="give"; COUNT="count"
    SETBLOCK="setblock"; IF_BLOCK="if_block"; POS="pos"; INVENTORY="inventory"
    CARDINALITY="cardinality"; UUID="uuid"; HEALTH="health"; KILL_SCORE="kill_score"; SCORE="score"
    RESIDUAL_CENSUS="residual_census"; ITEM_SNBT="item_snbt"; SELECT_RESIDUAL="select_residual"; SUMMON="summon"; KILL_COMPETING="kill_competing"; KILL_ITEMS="kill_items"; OBJECTIVE="objective"; OBJECTIVE_REMOVE="objective_remove"

@dataclass(frozen=True, slots=True)
class RconCommand:
    kind: CommandKind
    args: tuple[tuple[str, Any], ...]
    read_after_write: bool = False
    expected_ack: str = "ok"
    parser: str = "en_us.text.v1"
    timeout_ms: int = 2_000
    completeness: tuple[str, ...] = ()
    raw_digest: str = ""
    _marker: object = None
    def __post_init__(self):
        if self._marker is not _COMMAND_MARKER: raise LiveStateError("use typed command factories")
        if type(self.read_after_write) is not bool: raise LiveStateError("invalid read-after-write bit")
        if type(self.expected_ack) is not str or not self.expected_ack: raise LiveStateError("invalid acknowledgement")
        if not re.fullmatch(r"minecraft\.en_us\.[a-z_]+\.v1", self.parser): raise LiveStateError("invalid parser")
        if type(self.timeout_ms) is not int or not 1 <= self.timeout_ms <= MAX_TIMEOUT_MS: raise LiveStateError("invalid timeout")
        if any(x not in {"blocks","positions","inventories","entities","scoreboard","residual"} for x in self.completeness): raise LiveStateError("invalid completeness")
        if self.raw_digest and not re.fullmatch(r"[0-9a-f]{64}", self.raw_digest): raise LiveStateError("invalid raw digest")
    @property
    def command_id(self): return self.kind.value
    @property
    def text(self) -> str: return _render(self.kind, dict(self.args))

_COMMAND_MARKER = object()
_PARSERS = {
    CommandKind.FORCELOAD_QUERY: "minecraft.en_us.forceload_query.v1",
    CommandKind.ACTOR_EXISTS: "minecraft.en_us.actor_exists.v1",
    CommandKind.COUNT: "minecraft.en_us.count.v1",
    CommandKind.IF_BLOCK: "minecraft.en_us.if_block.v1",
    CommandKind.POS: "minecraft.en_us.position.v1",
    CommandKind.INVENTORY: "minecraft.en_us.inventory.v1",
    CommandKind.CARDINALITY: "minecraft.en_us.cardinality.v1",
    CommandKind.UUID: "minecraft.en_us.uuid.v1",
    CommandKind.HEALTH: "minecraft.en_us.health.v1",
    CommandKind.SCORE: "minecraft.en_us.score.v1",
    CommandKind.RESIDUAL_CENSUS: "minecraft.en_us.residual_census.v1",
    CommandKind.ITEM_SNBT: "minecraft.en_us.item_snbt.v1",
}
def _command(kind: CommandKind, read_after_write=False, *, parser=None, completeness=(), **args: Any) -> RconCommand:
    frozen_args=tuple(sorted(args.items()))
    return RconCommand(kind, frozen_args, read_after_write,
                       "en_us", parser or _PARSERS.get(kind, "minecraft.en_us.text.v1"), 2_000, tuple(completeness), _sha((kind.value,frozen_args)), _COMMAND_MARKER)

def forceload_add(x:int,z:int): return _command(CommandKind.FORCELOAD_ADD, True, x=coord(x), z=coord(z))
def forceload_query(x:int,z:int): return _command(CommandKind.FORCELOAD_QUERY, True, x=coord(x), z=coord(z))
def gamerule(name:str, value:str): return _command(CommandKind.GAMERULE, name=_ident(name), value=_ident(value), read_after_write=True)
def actor_exists(actor:str): return _command(CommandKind.ACTOR_EXISTS, True, completeness=("entities",), actor=_ident(actor))
def teleport(actor:str, p:Sequence[int]): return _command(CommandKind.TP, actor=_ident(actor), position=pos(p), read_after_write=True)
def clear_all(actor:str): return _command(CommandKind.CLEAR_ALL, actor=_ident(actor), read_after_write=True)
def clear(actor:str, thing:str, count:int=0): return _command(CommandKind.CLEAR, actor=_ident(actor), item=item(thing), count=qty(count), read_after_write=True)
def give(actor:str, thing:str, count:int): return _command(CommandKind.GIVE, actor=_ident(actor), item=item(thing), count=qty(count), read_after_write=True)
def count_items(actor:str, thing:str): return _command(CommandKind.COUNT, True, completeness=("inventories",), actor=_ident(actor), item=item(thing))
def setblock(p:Sequence[int], block:str): return _command(CommandKind.SETBLOCK, position=pos(p), block=item(block), read_after_write=True)
def execute_if_block(p:Sequence[int], block:str): return _command(CommandKind.IF_BLOCK, position=pos(p), block=item(block), read_after_write=True, completeness=("blocks",))
def data_pos(actor:str): return _command(CommandKind.POS, True, completeness=("positions",), actor=_ident(actor))
def data_inventory(actor:str): return _command(CommandKind.INVENTORY, True, completeness=("inventories",), actor=_ident(actor))
def cardinality(selector:str, objective:str, p:Sequence[int]=(0,64,0)): return _command(CommandKind.CARDINALITY, selector=_count_selector(selector), objective=_ident(objective), position=pos(p), read_after_write=True, completeness=("entities",))
def entity_uuid(selector:str, p:Sequence[int]=(0,64,0)): return _command(CommandKind.UUID, True, completeness=("entities",), position=pos(p), selector=_selector(selector) if type(selector) is str and selector.startswith("@e[") else _ident(selector), entity_kind="zombie", subject="Zombie")
def entity_health(selector:str, p:Sequence[int]=(0,64,0)): return _command(CommandKind.HEALTH, True, completeness=("entities",), position=pos(p), selector=_selector(selector) if type(selector) is str and selector.startswith("@e[") else _ident(selector), entity_kind="zombie", subject="Zombie")
def kill_score(player:str, objective:str, score:int): return _command(CommandKind.KILL_SCORE, player=_ident(player), objective=_ident(objective), score=bounded_int(score,minimum=0,maximum=2_147_483_647,label="score"), read_after_write=True)
def score_query(player:str, objective:str): return _command(CommandKind.SCORE, True, completeness=("scoreboard",), player=_ident(player), objective=_ident(objective))
def residual_census(p:Sequence[int], radius:int=5): return _command(CommandKind.RESIDUAL_CENSUS, position=pos(p), radius=bounded_int(radius,minimum=1,maximum=64,label="radius"), read_after_write=True, completeness=("residual",))
def item_snbt(tag:str, p:Sequence[int]=(0,64,0), radius:int=16):
    tag=_ident(tag,"item tag"); radius=bounded_int(radius,minimum=1,maximum=64,label="radius")
    selector=f"@e[type=item,tag={tag},distance=..{radius},limit=1]"
    return _command(CommandKind.ITEM_SNBT, True, selector=selector, position=pos(p), radius=radius, entity_kind="item", subject="Item", completeness=("residual",))
def item_uuid(tag:str, p:Sequence[int]=(0,64,0), radius:int=16):
    tag=_ident(tag,"item tag"); radius=bounded_int(radius,minimum=1,maximum=64,label="radius")
    selector=f"@e[type=item,tag={tag},distance=..{radius},limit=1]"
    return _command(CommandKind.UUID,True,selector=selector,position=pos(p),entity_kind="item",subject="Item",completeness=("residual",))
def select_residual(p:Sequence[int],radius:int,tag:str,excluded:Sequence[str]=()):
    tag=_ident(tag,"item tag"); excluded=tuple(_ident(value,"item tag") for value in excluded)
    return _command(CommandKind.SELECT_RESIDUAL,True,position=pos(p),radius=bounded_int(radius,minimum=1,maximum=64,label="radius"),tag=tag,excluded=excluded)
def summon_zombie(p:Sequence[int], tag:str): return _command(CommandKind.SUMMON, position=pos(p), tag=_ident(tag), read_after_write=True)
def kill_competing(p:Sequence[int], radius:int=8): return _command(CommandKind.KILL_COMPETING, position=pos(p), radius=bounded_int(radius,minimum=1,maximum=64,label="radius"), read_after_write=True)
def kill_items(p:Sequence[int], radius:int=16): return _command(CommandKind.KILL_ITEMS, position=pos(p), radius=bounded_int(radius,minimum=1,maximum=64,label="radius"), read_after_write=True)
def objective_add(objective:str, criterion:str="dummy"):
    if criterion not in {"dummy", "minecraft.killed:minecraft.zombie"}: raise LiveStateError("invalid objective criterion")
    return _command(CommandKind.OBJECTIVE, True, objective=_ident(objective), criterion=criterion)
def objective_remove(objective:str): return _command(CommandKind.OBJECTIVE_REMOVE, True, objective=_ident(objective))

def _render(kind, a):
    x,y,z=a.get("position", (None,None,None)); k=kind
    if k is CommandKind.FORCELOAD_ADD: return f"forceload add {a['x']} {a['z']}"
    if k is CommandKind.FORCELOAD_QUERY: return f"forceload query {a['x']} {a['z']}"
    if k is CommandKind.GAMERULE: return f"gamerule {a['name']} {a['value']}"
    if k is CommandKind.ACTOR_EXISTS: return f"execute if entity {a['actor']}"
    if k is CommandKind.TP: return f"tp {a['actor']} {x} {y} {z}"
    if k is CommandKind.CLEAR_ALL: return f"clear {a['actor']}"
    if k is CommandKind.CLEAR: return f"clear {a['actor']} {a['item']} {a['count']}"
    if k is CommandKind.GIVE: return f"give {a['actor']} {a['item']} {a['count']}"
    if k is CommandKind.COUNT: return f"clear {a['actor']} {a['item']} 0"
    if k is CommandKind.SETBLOCK: return f"setblock {x} {y} {z} {a['block']}"
    if k is CommandKind.IF_BLOCK: return f"execute if block {x} {y} {z} {a['block']}"
    if k is CommandKind.POS: return f"data get entity {a['actor']} Pos"
    if k is CommandKind.INVENTORY: return f"data get entity {a['actor']} Inventory"
    if k is CommandKind.CARDINALITY: return f"execute positioned {x} {y} {z} if entity {a['selector']}"
    if k is CommandKind.UUID: return f"execute positioned {x} {y} {z} run data get entity {a['selector']} UUID"
    if k is CommandKind.HEALTH: return f"execute positioned {x} {y} {z} run data get entity {a['selector']} Health"
    if k is CommandKind.KILL_SCORE: return f"scoreboard players set {a['player']} {a['objective']} {a['score']}"
    if k is CommandKind.SCORE: return f"scoreboard players get {a['player']} {a['objective']}"
    if k is CommandKind.RESIDUAL_CENSUS: return f"execute positioned {x} {y} {z} run execute if entity @e[type=item,distance=..{a['radius']}]"
    if k is CommandKind.ITEM_SNBT: return f"execute positioned {x} {y} {z} run data get entity {a['selector']} Item"
    if k is CommandKind.SELECT_RESIDUAL:
        exclusions="".join(f",tag=!{tag}" for tag in a["excluded"])
        return f"execute positioned {x} {y} {z} as @e[type=item,distance=..{a['radius']},sort=nearest,limit=1{exclusions}] run tag @s add {a['tag']}"
    if k is CommandKind.SUMMON: return f"summon zombie {x} {y} {z} {{Tags:[\"{a['tag']}\"],PersistenceRequired:1b,NoAI:1b,Health:20f}}"
    if k is CommandKind.OBJECTIVE: return f"scoreboard objectives add {a['objective']} {a['criterion']}"
    if k is CommandKind.OBJECTIVE_REMOVE: return f"scoreboard objectives remove {a['objective']}"
    if k is CommandKind.KILL_ITEMS: return f"execute positioned {x} {y} {z} run kill @e[type=item,distance=..{a['radius']}]"
    return f"execute positioned {x} {y} {z} run kill @e[type=zombie,distance=..{a['radius']}]"

_PLAN_MARKER=object()

@dataclass(frozen=True, slots=True)
class CommandPlan:
    profile: str; campaign: str; cell: str; reset_token: str; generation: int; commands: tuple[RconCommand,...]; digest: str; acknowledgement: str="en_us"; upstream_digest: str=""; authority_digest:str=""; authority_id:str=""; descriptor:str="mock"; purpose:str="mock"; _authority_marker:object=field(default=None,repr=False,compare=False)
    _marker: object=field(default=None,repr=False,compare=False)
    def __post_init__(self):
        if self._marker is not _PLAN_MARKER: raise LiveStateError("command plans are parent-minted")
        if not re.fullmatch(r"[0-9a-f]{64}",self.profile): raise LiveStateError("authenticated profile digest required")
        if self.upstream_digest and not re.fullmatch(r"[0-9a-f]{64}",self.upstream_digest): raise LiveStateError("invalid upstream plan binding")
        if self.authority_digest and not re.fullmatch(r"[0-9a-f]{64}",self.authority_digest): raise LiveStateError("invalid plan authority binding")
        if self.authority_id and not re.fullmatch(r"[0-9a-f]{64}",self.authority_id): raise LiveStateError("invalid parent authority identity")
        if not re.fullmatch(r"[A-Za-z0-9_]+",self.descriptor) or not re.fullmatch(r"[a-z_]+",self.purpose): raise LiveStateError("invalid plan descriptor binding")
        for v in (self.campaign,self.cell,self.reset_token): _ident(v)
        if type(self.generation) is not int or self.generation < 1 or len(self.commands) > MAX_COMMANDS or not self.commands or self.acknowledgement != "en_us": raise LiveStateError("invalid plan binding")
        if self.digest != plan_digest(self): raise LiveStateError("stale plan")
def plan_digest(p: CommandPlan) -> str: return _sha({"profile":p.profile,"campaign":p.campaign,"cell":p.cell,"token":p.reset_token,"generation":p.generation,"ack":p.acknowledgement,"upstream":p.upstream_digest,"authority":p.authority_digest,"authority_id":p.authority_id,"descriptor":p.descriptor,"purpose":p.purpose,"commands":[(c.kind.value,c.args,c.read_after_write,c.expected_ack,c.parser,c.timeout_ms,c.completeness,c.raw_digest) for c in p.commands]})
def make_plan(commands: Sequence[RconCommand], *, profile="0"*64, campaign="k12", cell="cell", reset_token="token", generation=1,upstream_digest=""):
    return _make_plan(commands,profile,campaign,cell,reset_token,generation,upstream_digest)
def _make_plan(commands, profile,campaign,cell,token,generation,upstream_digest="",authority_digest="",descriptor="mock",purpose="mock",authority_id="",authority_marker=None):
    p=CommandPlan.__new__(CommandPlan); object.__setattr__(p,"profile",_ident(profile)); object.__setattr__(p,"campaign",_ident(campaign)); object.__setattr__(p,"cell",_ident(cell)); object.__setattr__(p,"reset_token",_ident(token)); object.__setattr__(p,"generation",generation); object.__setattr__(p,"commands",tuple(commands)); object.__setattr__(p,"acknowledgement","en_us"); object.__setattr__(p,"upstream_digest",upstream_digest); object.__setattr__(p,"authority_digest",authority_digest); object.__setattr__(p,"authority_id",authority_id); object.__setattr__(p,"descriptor",descriptor); object.__setattr__(p,"purpose",purpose); object.__setattr__(p,"_authority_marker",authority_marker); object.__setattr__(p,"_marker",_PLAN_MARKER); object.__setattr__(p,"digest",plan_digest(p)); CommandPlan.__post_init__(p); return p

class ParentPlanAuthority:
    """Coordinator-owned mint for launch-eligible read-back plans."""
    def __init__(self,profile:Any,campaign:str):
        from .k12_guarded_backend import K12AuthenticatedProfile
        if not isinstance(profile,K12AuthenticatedProfile): raise TypeError("authenticated profile required")
        self.profile,self.campaign=profile,_ident(campaign); self.__marker=object(); self.authority_id=secrets.token_hex(32)
    def owns(self,plan:CommandPlan)->bool:
        return isinstance(plan,CommandPlan) and plan._authority_marker is self.__marker and plan.authority_id==self.authority_id
    def owns_state(self,state:Any)->bool:
        return isinstance(state,LiveState) and state._plan_authority_marker is self.__marker and state.plan_authority_id==self.authority_id
    def mint(self,commands:Sequence[RconCommand],*,cell:str,reset_token:str,generation:int,descriptor:str,purpose:str,upstream_digest:str="")->CommandPlan:
        if descriptor not in {"S1","S2","S3","S4","S5","containment"} or purpose not in {"before","after","census","residual","launch"}: raise LiveStateError("invalid descriptor plan authority")
        commands=tuple(commands); domains={domain for command in commands for domain in command.completeness}
        required={"S1":{"blocks","inventories"},"S2":{"blocks","inventories"},"S3":{"positions"},"S4":{"entities","scoreboard"},"S5":{"positions","inventories","residual"},"containment":set()}[descriptor]
        if purpose in {"before","after"} and not required<=domains: raise LiveStateError("descriptor read-back plan is incomplete")
        if purpose=="census" and (descriptor!="S5" or len(commands)!=1 or commands[0].kind is not CommandKind.RESIDUAL_CENSUS): raise LiveStateError("exact residual census plan required")
        authority=_sha((self.authority_id,self.profile.profile_digest,self.campaign,cell,reset_token,generation,descriptor,purpose,upstream_digest,tuple(command.raw_digest for command in commands)))
        return _make_plan(commands,self.profile.profile_digest,self.campaign,cell,reset_token,generation,upstream_digest,authority,descriptor,purpose,self.authority_id,self.__marker)

class MockTransport:
    """Explicit test-only transport marker; it records no real connection."""
    def __init__(self, script: Mapping[str, Any], *, default_timeout_ms=None):
        if not isinstance(script, Mapping): raise LiveStateError("scripted mapping required")
        self.script=dict(script); self.calls=[]; self.default_timeout_ms=default_timeout_ms
    def send(self, text):
        self.calls.append(text)
        if text not in self.script: raise LiveStateError("unscripted command")
        value=self.script[text]
        if value is Timeout: raise LiveStateError("command timeout")
        return value

class _Timeout: pass
Timeout = _Timeout()

@dataclass(frozen=True, slots=True)
class CommandResult:
    plan_digest: str
    ordinal: int
    command_digest: str
    command_id: str
    value: Any
    raw_sha256: str
    completeness: tuple[str, ...]
    read_after_write: bool
    parser_identity: str
    digest: str
    _marker: object=field(default=None,repr=False,compare=False)
    def __post_init__(self):
        if self._marker is not _RESULT_MARKER: raise LiveStateError("command results are parser-minted")
        if (not re.fullmatch(r"r\d{3}_[a-z_]+", self.command_id)
                or not re.fullmatch(r"[0-9a-f]{64}", self.raw_sha256)):
            raise LiveStateError("invalid command result identity")
        if (not re.fullmatch(r"[0-9a-f]{64}",self.plan_digest)
                or not re.fullmatch(r"[0-9a-f]{64}",self.command_digest)
                or type(self.ordinal) is not int or self.ordinal < 0): raise LiveStateError("invalid command result binding")
        if not re.fullmatch(r"minecraft\.en_us\.[a-z_]+\.v1", self.parser_identity): raise LiveStateError("invalid parser identity")
        if self.digest != _sha((self.plan_digest,self.ordinal,self.command_digest,self.command_id,self.raw_sha256,self.parser_identity,self.value,self.completeness,self.read_after_write)): raise LiveStateError("invalid command result digest")

_RESULT_MARKER=object()

def _parse_response(command, raw):
    if type(raw) is not str or len(raw)>MAX_TEXT: raise LiveStateError("malformed response")
    if raw != raw.strip() or "\n" in raw or raw.startswith("[en_us]") or "|" in raw: raise LiveStateError("wrong acknowledgement")
    args=dict(command.args); kind=command.kind
    try:
        if command.parser.endswith(".forceload_query.v1"):
            match=re.fullmatch(r"Chunk at \[(-?\d+), (-?\d+)\] in minecraft:overworld is (not )?marked for force loading",raw)
            if not match or (int(match.group(1)),int(match.group(2)))!=(args["x"]//16,args["z"]//16): raise ValueError
            return match.group(3) is None
        if command.parser.endswith((".actor_exists.v1", ".if_block.v1")):
            if raw=="Test failed": return False
            match=re.fullmatch(r"Test passed, count: (\d+)",raw)
            if not match: raise ValueError
            return bounded_int(int(match.group(1)),minimum=1,maximum=MAX_ROWS,label="response count")>0
        if command.parser.endswith(".count.v1"):
            if raw==f"No items were found on player {args['actor']}": return 0
            match=re.fullmatch(rf"Found (\d+) matching items? on player {re.escape(args['actor'])}",raw)
            if not match: raise ValueError
            return bounded_int(int(match.group(1)),minimum=0,maximum=2304,label="inventory count")
        if command.parser.endswith(".cardinality.v1"):
            if raw=="Test failed": return 0
            match=re.fullmatch(r"Test passed, count: (\d+)",raw)
            if not match: raise ValueError
            return bounded_int(int(match.group(1)),minimum=0,maximum=MAX_ROWS,label="cardinality")
        if command.parser.endswith(".score.v1"):
            match=re.fullmatch(rf"{re.escape(args['player'])} has (-?\d+) \[{re.escape(args['objective'])}\]",raw)
            if not match: raise ValueError
            return bounded_int(int(match.group(1)),minimum=0,maximum=2_147_483_647,label="score")
        if command.parser.endswith(".residual_census.v1"):
            if raw=="Test failed": return 0
            match=re.fullmatch(r"Test passed, count: (\d+)",raw)
            if not match: raise ValueError
            return bounded_int(int(match.group(1)),minimum=1,maximum=64,label="residual count")
        if command.parser.endswith(".position.v1"):
            prefix=f"{args['actor']} has the following entity data: "
            if not raw.startswith(prefix): raise ValueError
            v=parse_snbt(raw[len(prefix):])
            if not isinstance(v,list) or len(v)!=3: raise ValueError
            values=tuple(float(x) for x in v)
            if any(x!=x or x in {float('inf'),float('-inf')} or not -30_000_000<=x<=30_000_000 for x in values): raise ValueError
            return values
        if command.parser.endswith(".inventory.v1"):
            prefix=f"{args['actor']} has the following entity data: "
            if not raw.startswith(prefix): raise ValueError
            value=parse_snbt(raw[len(prefix):])
            if not isinstance(value,list): raise ValueError
            totals:dict[str,int]={}
            for row in value:
                if not isinstance(row,dict) or set(row)-{"Slot","id","Count","count"}: raise ValueError
                item_id=_state_item(row.get("id"))
                if ("Count" in row)==("count" in row): raise ValueError
                count=row.get("Count",row.get("count"))
                if type(count) is not int or not 1<=count<=64: raise ValueError
                totals[item_id]=totals.get(item_id,0)+count
            return tuple(sorted(totals.items()))
        if command.parser.endswith(".uuid.v1"):
            prefix=f"{args['subject']} has the following entity data: "
            if not raw.startswith(prefix): raise ValueError
            match=re.fullmatch(r"\[I;\s*(-?\d+),\s*(-?\d+),\s*(-?\d+),\s*(-?\d+)\]",raw[len(prefix):])
            if not match: raise ValueError
            parts=[int(x)&0xffffffff for x in match.groups()]
            hexed="".join(f"{x:08x}" for x in parts)
            return bound_uuid(f"{hexed[:8]}-{hexed[8:12]}-{hexed[12:16]}-{hexed[16:20]}-{hexed[20:]}")
        if command.parser.endswith(".health.v1"):
            prefix=f"{args['subject']} has the following entity data: "
            if not raw.startswith(prefix): raise ValueError
            payload=raw[len(prefix):]
            if not re.fullmatch(r"[-+]?(?:\d+(?:\.\d+)?)[fFdD]?",payload): raise ValueError
            value=float(payload.rstrip("fFdD"))
            if not 0 <= value <= 2048 or value != value or value in {float("inf"),float("-inf")}: raise ValueError
            return value
        if command.parser.endswith(".item_snbt.v1"):
            prefix="Item has the following entity data: "
            if not raw.startswith(prefix): raise ValueError
            value=parse_snbt(raw[len(prefix):])
            if not isinstance(value,dict) or set(value)-{"id","Count","count"}: raise ValueError
            item_id=_state_item(value.get("id"))
            if ("Count" in value)==("count" in value): raise ValueError
            count=value.get("Count",value.get("count"))
            if type(count) is not int or not 1<=count<=64: raise ValueError
            return (args["selector"],item_id,count)
        if kind is CommandKind.TP:
            match=re.fullmatch(rf"Teleported {re.escape(args['actor'])} to (-?\d+(?:\.\d+)?), (-?\d+(?:\.\d+)?), (-?\d+(?:\.\d+)?)",raw)
            if not match or tuple(float(value) for value in match.groups())!=tuple(float(value) for value in args["position"]): raise ValueError
            return True
        # Mutation acknowledgements are command-specific and fully anchored.
        patterns={
            CommandKind.FORCELOAD_ADD:rf"Marked chunk \[{args.get('x',0)//16}, {args.get('z',0)//16}\] in minecraft:overworld to be force loaded",
            CommandKind.GAMERULE:rf"Gamerule {re.escape(args.get('name',''))} is now set to: {re.escape(args.get('value',''))}",
            CommandKind.CLEAR_ALL:rf"(?:Removed \d+ item\(s\) from player {re.escape(args.get('actor',''))}|No items were found on player {re.escape(args.get('actor',''))})",
            CommandKind.CLEAR:rf"(?:Removed \d+ item\(s\) from player {re.escape(args.get('actor',''))}|No items were found on player {re.escape(args.get('actor',''))})",
            CommandKind.GIVE:rf"Gave {args.get('count')} \[[^\]\r\n]{{1,128}}\] to {re.escape(args.get('actor',''))}",
            CommandKind.SETBLOCK:rf"Changed the block at {args.get('position',(0,0,0))[0]}, {args.get('position',(0,0,0))[1]}, {args.get('position',(0,0,0))[2]}",
            CommandKind.KILL_SCORE:rf"Set \[{re.escape(args.get('objective',''))}\] for {re.escape(args.get('player',''))} to {args.get('score')}",
            CommandKind.SUMMON:r"Summoned new Zombie",
            CommandKind.KILL_COMPETING:r"(?:Killed \d+ entities|No entity was found)",
            CommandKind.KILL_ITEMS:r"(?:Killed \d+ entities|No entity was found)",
            CommandKind.SELECT_RESIDUAL:rf"Added tag '{re.escape(args.get('tag',''))}' to Item",
            CommandKind.OBJECTIVE:rf"Created new objective \[{re.escape(args.get('objective',''))}\]",
            CommandKind.OBJECTIVE_REMOVE:rf"(?:Removed objective \[{re.escape(args.get('objective',''))}\]|Unknown scoreboard objective '{re.escape(args.get('objective',''))}')",
        }
        if kind not in patterns or not re.fullmatch(patterns[kind],raw): raise ValueError
        return True
    except (ValueError, TypeError, LiveStateError) as e: raise LiveStateError("malformed typed response") from e

def execute_plan(plan:CommandPlan, transport:MockTransport):
    if type(transport) is not MockTransport: raise LiveStateError("concrete MockTransport required")
    results=[]
    for ordinal,command in enumerate(plan.commands):
        raw=transport.send(command.text)
        value = _parse_response(command,raw)
        raw_sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        result_id=f"r{ordinal:03d}_{command.command_id}"
        digest=_sha((plan.digest,ordinal,command.raw_digest,result_id,raw_sha,command.parser,value,command.completeness,command.read_after_write))
        results.append(CommandResult(plan.digest,ordinal,command.raw_digest,result_id,value,raw_sha,command.completeness,
            command.read_after_write,command.parser,digest,_RESULT_MARKER))
    return tuple(results)

_USED_RESET_TOKENS: set[tuple[str, str, str, str]] = set()
_RESET_GENERATIONS: dict[tuple[str, str, str], int] = {}
def validate_reset_token(token: str, generation: int, *, profile="0"*64, campaign="k12", cell="cell") -> None:
    token = _ident(token, "reset token")
    cell_key=(_ident(profile),_ident(campaign),_ident(cell)); token_key=(*cell_key,token)
    if (type(generation) is not int or generation < 1 or token_key in _USED_RESET_TOKENS
            or generation <= _RESET_GENERATIONS.get(cell_key, 0)):
        raise LiveStateError("stale or reused reset token")
def consume_reset_token(token: str, generation: int, *, profile="0"*64, campaign="k12", cell="cell") -> None:
    validate_reset_token(token, generation, profile=profile, campaign=campaign, cell=cell)
    cell_key=(_ident(profile),_ident(campaign),_ident(cell))
    _USED_RESET_TOKENS.add((*cell_key,_ident(token)))
    _RESET_GENERATIONS[cell_key] = generation

_STATE_TOKEN = object()

@dataclass(frozen=True, slots=True, init=False)
class LiveState:
    profile:str; campaign:str; cell:str; reset_token:str; generation:int; plan_digest:str=""; plan_authority_digest:str=""; plan_authority_id:str=""; plan_descriptor:str="mock"; plan_purpose:str="mock"; blocks:tuple=(); positions:tuple=(); inventories:tuple=(); entities:tuple=(); scoreboard:tuple=(); residual:tuple=(); residual_count:int|None=None; complete:tuple=(); provenance:tuple=(); field_provenance:tuple=(); raw_digests:tuple=(); reset_attestation_sha256:str=""; read_after_write:bool=False
    _plan_authority_marker:object=field(default=None,repr=False,compare=False)
    def __init__(self,profile,campaign,cell,reset_token,generation,*,plan_digest="",plan_authority_digest="",plan_authority_id="",plan_descriptor="mock",plan_purpose="mock",blocks=(),positions=(),inventories=(),entities=(),scoreboard=(),residual=(),residual_count=None,complete=(),provenance=(),field_provenance=(),raw_digests=(),reset_attestation_sha256="",read_after_write=False,_marker=None,_plan_authority_marker=None):
        if _marker is not _STATE_TOKEN: raise LiveStateError("LiveState is normalizer-minted")
        for name,value in (("profile",profile),("campaign",campaign),("cell",cell),("reset_token",reset_token),("generation",generation),("plan_digest",plan_digest),("plan_authority_digest",plan_authority_digest),("plan_authority_id",plan_authority_id),("plan_descriptor",plan_descriptor),("plan_purpose",plan_purpose),("blocks",tuple(blocks)),("positions",tuple(positions)),("inventories",tuple(inventories)),("entities",tuple(entities)),("scoreboard",tuple(scoreboard)),("residual",tuple(residual)),("residual_count",residual_count),("complete",tuple(complete)),("provenance",tuple(provenance)),("field_provenance",tuple(field_provenance)),("raw_digests",tuple(raw_digests)),("reset_attestation_sha256",reset_attestation_sha256),("read_after_write",read_after_write)): object.__setattr__(self,name,value)
        object.__setattr__(self,"_plan_authority_marker",_plan_authority_marker)
    @property
    def digest(self): return _sha(self.__dict__ if hasattr(self,"__dict__") else (self.profile,self.campaign,self.cell,self.reset_token,self.generation,self.plan_digest,self.plan_authority_digest,self.plan_authority_id,self.plan_descriptor,self.plan_purpose,self.blocks,self.positions,self.inventories,self.entities,self.scoreboard,self.residual,self.residual_count,self.complete,self.provenance,self.field_provenance,self.raw_digests,self.reset_attestation_sha256,self.read_after_write))
@dataclass(frozen=True,slots=True)
class RawDigestPair:
    command_id:str; raw_sha256:str
    def __post_init__(self):
        if type(self.command_id) is not str or not re.fullmatch(r"r\d{3}_[a-z_]+",self.command_id) or not re.fullmatch(r"[0-9a-f]{64}",self.raw_sha256): raise LiveStateError("invalid raw digest pair")
@dataclass(frozen=True,slots=True)
class FieldProvenance:
    field_key:str; result_id:str; raw_sha256:str; result_digest:str
    def __post_init__(self):
        if (type(self.field_key) is not str or not self.field_key or len(self.field_key)>256
                or not re.fullmatch(r"r\d{3}_[a-z_]+",self.result_id)
                or not re.fullmatch(r"[0-9a-f]{64}",self.raw_sha256)
                or not re.fullmatch(r"[0-9a-f]{64}",self.result_digest)): raise LiveStateError("invalid field provenance")
def validate_state(state:LiveState)->LiveState:
    domains={"blocks","positions","inventories","entities","scoreboard","residual"}
    if not isinstance(state,LiveState) or type(state.generation) is not int or state.generation<1 or not set(state.complete) or not set(state.complete)<=domains: raise LiveStateError("invalid or partial state")
    if not re.fullmatch(r"[0-9a-f]{64}",state.profile): raise LiveStateError("state profile digest mismatch")
    if not re.fullmatch(r"[0-9a-f]{64}",state.plan_digest): raise LiveStateError("state plan digest mismatch")
    if state.plan_authority_digest and not re.fullmatch(r"[0-9a-f]{64}",state.plan_authority_digest): raise LiveStateError("state plan authority mismatch")
    if state.plan_authority_id and not re.fullmatch(r"[0-9a-f]{64}",state.plan_authority_id): raise LiveStateError("state parent authority mismatch")
    for value in (state.campaign,state.cell,state.reset_token): _ident(value)
    if (any(not isinstance(x,RawDigestPair) for x in state.raw_digests)
            or len(state.raw_digests) < len(state.complete)
            or len(state.provenance) != len(state.complete)
            or not state.field_provenance
            or any(not isinstance(x,FieldProvenance) for x in state.field_provenance)
            or len({x.field_key for x in state.field_provenance})!=len(state.field_provenance)
            or any((x.result_id,x.raw_sha256) not in {(r.command_id,r.raw_sha256) for r in state.raw_digests} for x in state.field_provenance)
            or any(d not in state.complete or not ids for d, ids in state.provenance)):
        raise LiveStateError("raw digest pairs required")
    return state
def reset_attestation_digest(plan: CommandPlan, results: Sequence[CommandResult]) -> str:
    return _sha((plan.digest, tuple(result.digest for result in results)))
def _field_keys(command:RconCommand)->tuple[str,...]:
    args=dict(command.args); kind=command.kind
    if kind is CommandKind.ACTOR_EXISTS: return (f"entity_presence:{args['actor']}",)
    if kind is CommandKind.IF_BLOCK: return (f"block:{','.join(map(str,args['position']))}",)
    if kind is CommandKind.POS: return (f"position:{args['actor']}",)
    if kind is CommandKind.COUNT: return (f"inventory_count:{args['actor']}:{args['item']}",)
    if kind is CommandKind.INVENTORY: return (f"inventory_snbt:{args['actor']}",)
    if kind is CommandKind.CARDINALITY: return (f"cardinality:{args['objective']}:{args['selector']}",)
    if kind in {CommandKind.UUID,CommandKind.HEALTH}: return (f"{args.get('entity_kind','entity')}:{args['selector']}:{kind.value}",)
    if kind is CommandKind.SCORE: return (f"score:{args['player']}:{args['objective']}",)
    if kind is CommandKind.RESIDUAL_CENSUS: return (f"residual_census:{args['position']}:{args['radius']}",)
    if kind is CommandKind.ITEM_SNBT: return (f"residual_item:{args['selector']}",)
    return ()
def normalize_state(plan: CommandPlan, results: Sequence[CommandResult]) -> LiveState:
    """The sole state mint: authenticated plan plus its exact ordered read-back."""
    if not isinstance(plan, CommandPlan) or type(results) not in {tuple, list} or len(results) != len(plan.commands): raise LiveStateError("exact command results required")
    if any(not isinstance(r, CommandResult) for r in results): raise LiveStateError("invalid command result")
    for ordinal,(command, result) in enumerate(zip(plan.commands, results)):
        if (result.plan_digest != plan.digest or result.ordinal != ordinal
                or result.command_digest != command.raw_digest
                or result.command_id != f"r{ordinal:03d}_{command.command_id}" or result.parser_identity != command.parser
                or result.completeness != command.completeness or result.read_after_write != command.read_after_write): raise LiveStateError("command result order or identity mismatch")
    blocks:dict[tuple[int,int,int],str]={}; positions:dict[str,tuple[float,float,float]]={}
    inventories:dict[str,dict[str,int]]={}; inventory_snbt:dict[str,dict[str,int]]={}
    entity_parts:dict[str,dict[str,Any]]={}; residual_parts:dict[str,dict[str,Any]]={}; scoreboard:dict[tuple[str,str],int]={}
    residual:list[tuple[str,str,int]]=[]; residual_count: int|None=None
    domain_ids:dict[str,list[str]]={name:[] for name in ("blocks","positions","inventories","entities","scoreboard","residual")}
    for command,result in zip(plan.commands,results):
        args=dict(command.args)
        for domain in result.completeness: domain_ids[domain].append(result.command_id)
        if command.kind is CommandKind.ACTOR_EXISTS and result.value is not True: raise LiveStateError("required actor is absent")
        if command.kind is CommandKind.IF_BLOCK:
            if result.value is not True: raise LiveStateError("block read-back mismatch")
            key=tuple(args["position"]); value=args["block"]
            if key in blocks and blocks[key]!=value: raise LiveStateError("contradictory block read-back")
            blocks[key]=value
        elif command.kind is CommandKind.POS:
            actor=args["actor"]
            if actor in positions and positions[actor]!=result.value: raise LiveStateError("contradictory position read-back")
            positions[actor]=tuple(result.value)
        elif command.kind is CommandKind.COUNT:
            inventories.setdefault(args["actor"],{})[args["item"]]=result.value
        elif command.kind is CommandKind.INVENTORY:
            inventory_snbt[args["actor"]]=dict(result.value)
        elif command.kind is CommandKind.CARDINALITY:
            scoreboard[("#count",args["objective"])]=result.value
        elif command.kind is CommandKind.SCORE:
            scoreboard[(args["player"],args["objective"])]=result.value
        elif command.kind in {CommandKind.UUID,CommandKind.HEALTH}:
            parts=residual_parts if args.get("entity_kind")=="item" else entity_parts
            part=parts.setdefault(args["selector"],{})
            part["uuid" if command.kind is CommandKind.UUID else "health"]=result.value
        elif command.kind is CommandKind.RESIDUAL_CENSUS:
            if residual_count is not None and residual_count!=result.value: raise LiveStateError("contradictory residual census")
            residual_count=result.value
        elif command.kind is CommandKind.ITEM_SNBT:
            selector,item_id,count=result.value
            residual_parts.setdefault(selector,{}).update(item=item_id,count=count)
    for actor,observed in inventory_snbt.items():
        counted=inventories.get(actor,{})
        if any(observed.get(name,0)!=count for name,count in counted.items()): raise LiveStateError("inventory count/SNBT mismatch")
        merged=dict(observed)
        for name,count in counted.items(): merged.setdefault(name,count)
        inventories[actor]=merged
    entities=[]
    for part in entity_parts.values():
        if set(part)!={"uuid","health"}: raise LiveStateError("partial entity read-back")
        entities.append((part["uuid"],part["health"],True))
    for part in residual_parts.values():
        if set(part)!={"uuid","item","count"}: raise LiveStateError("partial residual item read-back")
        residual.append((part["uuid"],part["item"],part["count"]))
    if residual_count is not None:
        enumerated=any(command.kind is CommandKind.ITEM_SNBT for command in plan.commands)
        if enumerated and residual_count != len(residual):
            if residual_count != 0 or residual: raise LiveStateError("residual census/enumeration mismatch")
        if len({row[0] for row in residual})!=len(residual): raise LiveStateError("duplicate residual entity")
    values={
        "blocks":tuple(sorted(blocks.items())),
        "positions":tuple(sorted(positions.items())),
        "inventories":tuple(sorted((actor,tuple(sorted(items.items()))) for actor,items in inventories.items())),
        "entities":tuple(sorted(entities)),
        "scoreboard":tuple(sorted((player,objective,value) for (player,objective),value in scoreboard.items())),
        "residual":tuple(sorted(residual)),
        "residual_count":residual_count,
    }
    names=tuple(domain for domain in ("blocks","positions","inventories","entities","scoreboard","residual") if domain_ids[domain])
    pairs=tuple(RawDigestPair(r.command_id,r.raw_sha256) for r in results)
    field_provenance=tuple(FieldProvenance(key,result.command_id,result.raw_sha256,result.digest)
        for command,result in zip(plan.commands,results) for key in _field_keys(command))
    state=LiveState(plan.profile,plan.campaign,plan.cell,plan.reset_token,plan.generation,**values,complete=names,
                     provenance=tuple((d,tuple(domain_ids[d])) for d in names),
                     plan_digest=plan.digest,plan_authority_digest=plan.authority_digest,plan_authority_id=plan.authority_id,
                     plan_descriptor=plan.descriptor,plan_purpose=plan.purpose,field_provenance=field_provenance,raw_digests=pairs,
                     reset_attestation_sha256=reset_attestation_digest(plan,results),read_after_write=True,
                     _plan_authority_marker=plan._authority_marker,_marker=_STATE_TOKEN)
    return validate_state(state)
def normalize_mock_state(**kwargs):
    raise LiveStateError("arbitrary normalized state construction is unavailable")
def parse_snbt(raw:str):
    if type(raw) is not str or len(raw)>16384: raise LiveStateError("malformed SNBT")
    if not raw or raw[0] not in "[{": raise LiveStateError("malformed SNBT")
    # Deliberately small SNBT reader: compounds, lists, quoted strings and
    # bounded scalar numbers.  It is not a general NBT implementation.
    token=re.compile(r'\s*(?:("(?:\\.|[^"\\])*"|\'[^\'\\]*(?:\\.[^\'\\]*)*\')|([-+]?(?:\d+\.\d+|\d+)(?:[bBsSlLfFdD])?)|([A-Za-z0-9_.:+-]+))')
    i=0
    def ws():
        nonlocal i
        while i<len(raw) and raw[i].isspace(): i+=1
    def parse(depth=0):
        nonlocal i
        if depth>16: raise ValueError
        ws()
        if i>=len(raw): raise ValueError
        if raw[i]=='{':
            i+=1; out={}; ws()
            while i<len(raw) and raw[i]!='}':
                if raw[i] in {'"',"'"}:
                    m=token.match(raw,i)
                    if not m or not m.group(1): raise ValueError
                    key=m.group(1).strip('"\''); i=m.end()
                else:
                    m=re.match(r"[A-Za-z0-9_.+-]+",raw[i:])
                    if not m: raise ValueError
                    key=m.group(0); i+=len(key)
                if not IDENT.fullmatch(key): raise ValueError
                ws()
                if i>=len(raw) or raw[i] != ':': raise ValueError
                if key in out: raise ValueError
                i+=1; out[key]=parse(depth+1); ws()
                if i<len(raw) and raw[i]==',': i+=1; ws()
                elif i>=len(raw) or raw[i]!='}': raise ValueError
            if i>=len(raw): raise ValueError
            i+=1; return out
        if raw[i]=='[':
            i+=1; out=[]; ws()
            while i<len(raw) and raw[i]!=']':
                out.append(parse(depth+1));
                if len(out)>MAX_ROWS: raise ValueError
                ws()
                if i<len(raw) and raw[i]==',': i+=1; ws()
                elif i>=len(raw) or raw[i]!=']': raise ValueError
            if i>=len(raw): raise ValueError
            i+=1; return out
        m=token.match(raw,i)
        if not m: raise ValueError
        i=m.end(); value=m.group(1) or m.group(2) or m.group(3)
        if m.group(1):
            decoded=value[1:-1]
            if len(decoded)>256: raise ValueError
            return decoded
        if m.group(2):
            suffix=value[-1].lower() if value[-1].isalpha() else ''
            number=value[:-1] if suffix else value
            parsed=float(number) if '.' in number or suffix in {'f','d'} else int(number)
            if isinstance(parsed,float) and (parsed!=parsed or parsed in {float('inf'),float('-inf')}): raise ValueError
            if isinstance(parsed,int) and abs(parsed)>2**63-1: raise ValueError
            return parsed
        return value
    try: value=parse(); ws()
    except (ValueError,IndexError) as e: raise LiveStateError("malformed SNBT") from e
    if i != len(raw) or not isinstance(value,(dict,list)): raise LiveStateError("invalid SNBT root")
    def walk(x):
        if isinstance(x, dict):
            if any(type(k) is not str or not IDENT.fullmatch(k) for k in x): raise LiveStateError("invalid SNBT key")
            for y in x.values(): walk(y)
        elif isinstance(x, list):
            if len(x)>MAX_ROWS: raise LiveStateError("SNBT bound exceeded")
            for y in x: walk(y)
        elif type(x) not in {str,int,float,bool} and x is not None: raise LiveStateError("unsupported SNBT")
    walk(value); return value
__all__=["LiveStateError","CommandKind","RconCommand","CommandPlan","ParentPlanAuthority","MockTransport","Timeout","CommandResult","LiveState","RawDigestPair","FieldProvenance","validate_state","normalize_state","normalize_mock_state","reset_attestation_digest","make_plan","plan_digest","execute_plan","parse_snbt","detached_artifact_digest","forceload_add","forceload_query","gamerule","actor_exists","teleport","clear_all","clear","give","count_items","setblock","execute_if_block","data_pos","data_inventory","cardinality","entity_uuid","entity_health","kill_score","score_query","residual_census","item_snbt","item_uuid","select_residual","summon_zombie","kill_competing","kill_items","objective_add","objective_remove","validate_reset_token","consume_reset_token","tag_selector","tag_count_selector","zombie_count_selector","bound_uuid","pos","qty"]
