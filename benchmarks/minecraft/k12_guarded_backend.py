"""Offline K12 effect mock with parent-only state transitions.

The worker receives an immutable capability and, after admission, a one-shot
effect-entry object.  Only the parent retains the transition token; the mock
tool and backend are deliberately unable to advance the capability FSM.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum
from threading import RLock
from typing import Any, Mapping

from benchmarks.common.eac.canonical import canonical_sha256


class K12GuardedBackendError(RuntimeError):
    pass


class CapabilityState(str, Enum):
    CREATED = "CREATED"
    BOUND = "BOUND"
    REQUEST_VALIDATED = "REQUEST_VALIDATED"
    PERMIT_ISSUED = "PERMIT_ISSUED"
    EFFECT_ENTERED = "EFFECT_ENTERED"
    EFFECT_TERMINAL = "EFFECT_TERMINAL"
    REVOKED = "REVOKED"
    POISONED = "POISONED"


class K12AuthenticatedProfile:
    __slots__ = ("__profile_id", "__profile_digest")
    __TOKEN = object()

    def __init__(self, profile_id: str, profile_digest: str, token: object = None) -> None:
        if token is not self.__TOKEN:
            raise TypeError("use K12AuthenticatedProfile.from_runtime_profile")
        self.__profile_id, self.__profile_digest = profile_id, profile_digest

    @property
    def profile_id(self) -> str: return self.__profile_id

    @property
    def profile_digest(self) -> str: return self.__profile_digest

    @classmethod
    def from_runtime_profile(cls, profile: Any) -> "K12AuthenticatedProfile":
        from .k12_runtime_profile import K12RuntimeProfile
        if type(profile) is not K12RuntimeProfile:
            raise TypeError("a loader-issued runtime profile is required")
        return cls(profile.profile_id, profile.profile_digest, cls.__TOKEN)


class K12ScriptedMockTool:
    __slots__ = ("__results", "calls")

    def __init__(self, results: Mapping[str, Any] | None = None) -> None:
        self.__results, self.calls = dict(results or {}), []

    def invoke(self, action: str, **arguments: Any) -> Any:
        self.calls.append((action, dict(arguments)))
        return self.__results.get(action, {"status": True})


class K12GuardedToolHandle:
    __slots__ = ("__tool", "__authority")

    def __init__(self, tool: Any, authority: object) -> None:
        if type(tool) is not K12ScriptedMockTool:
            raise TypeError("concrete K12ScriptedMockTool required")
        self.__tool, self.__authority = tool, authority

    def _invoke(self, authority: object, action: str, arguments: dict[str, Any]) -> Any:
        if authority is not self.__authority:
            raise K12GuardedBackendError("tool handle authority mismatch")
        return self.__tool.invoke(action, **arguments)


@dataclass(frozen=True, slots=True)
class K12CapabilityBinding:
    profile_id: str; campaign_id: str; cohort_id: str; cell_id: str; triplet_id: str
    arm_id: str; actor_id: str; action_id: str; request_namespace: str; runtime_id: str
    tool_id: str; process_id: str; unit_id: str; cgroup_id: str; proposal_id: str
    request_id: str; candidate_id: str; attempt_id: str; permit_id: str; nonce: str
    argument_digest: str; profile_digest: str = ""

    @property
    def capability_digest(self) -> str:
        return canonical_sha256({f.name: getattr(self, f.name) for f in fields(self)})


class K12GuardedToolCapability:
    """Immutable identity view.  Every mutating method requires the parent token."""
    __slots__ = ("__binding",)

    def __init__(self, binding: K12CapabilityBinding, mint: object) -> None:
        if mint is not K12GuardedToolCapability.__MINT:
            raise TypeError("capabilities are parent-minted")
        if not isinstance(binding, K12CapabilityBinding):
            raise TypeError("typed capability binding is required")
        if any(type(getattr(binding, f.name)) is not str or not getattr(binding, f.name) for f in fields(binding)):
            raise ValueError("complete capability binding is required")
        self.__binding = binding

    __MINT = object()

    @property
    def binding(self) -> K12CapabilityBinding: return self.__binding

    @property
    def capability_digest(self) -> str: return self.__binding.capability_digest

class K12AdmittedEffectEntry:
    """One-shot effect handle; it contains no parent-controller reference."""
    __slots__=("__capability","__action","__arguments","__ids","__consumer")
    __MINT=object()
    def __init__(self,capability:K12GuardedToolCapability,action:str,arguments:tuple[tuple[str,Any],...],
                 ids:tuple[tuple[str,str],...],consumer:Any,mint:object):
        if mint is not self.__MINT: raise TypeError("effect entries are parent-minted")
        self.__capability,self.__action,self.__arguments,self.__ids=capability,action,arguments,ids
        self.__consumer=consumer
    @property
    def capability(self): return self.__capability
    @property
    def action(self): return self.__action
    @property
    def arguments(self): return self.__arguments
    @property
    def ids(self): return self.__ids
    def _consume(self)->Any: return self.__consumer()


ACTION_MAPPINGS = {name: name for name in ("MineBlock", "placeBlock", "navigateTo", "attackTarget", "handoverBlock")}
_ARGUMENTS = {
    "MineBlock": {"x": int, "y": int, "z": int},
    "placeBlock": {"item_name": str, "x": int, "y": int, "z": int, "facing": str},
    "navigateTo": {"x": int, "y": int, "z": int}, "attackTarget": {"target_name": str},
    "handoverBlock": {"target_player_name": str, "item_name": str, "item_count": int},
}


class K12ParentAuthority:
    def __init__(self, profile: K12AuthenticatedProfile, campaign_id: str) -> None:
        if not isinstance(profile, K12AuthenticatedProfile): profile = K12AuthenticatedProfile.from_runtime_profile(profile)
        if not isinstance(campaign_id, str) or not campaign_id: raise ValueError("campaign_id is required")
        self.__profile, self.__campaign, self.__token, self.__lock = profile, campaign_id, object(), RLock()
        self.__minted: set[int] = set(); self.__caps: set[K12GuardedToolCapability] = set(); self.__states:dict[K12GuardedToolCapability,CapabilityState]={}; self.__handles:dict[K12GuardedToolCapability,K12GuardedToolHandle]={}; self.__cells: set[str] = set(); self.__runtimes: set[str] = set(); self.__tools: set[str] = set()
        self.__evidence: list[dict[str, Any]] = []

    @property
    def evidence(self) -> tuple[dict[str, Any], ...]: return tuple(self.__evidence)

    def state(self,cap:K12GuardedToolCapability)->CapabilityState:
        with self.__lock:
            if cap not in self.__caps: raise K12GuardedBackendError("capability is not parent-owned")
            return self.__states[cap]

    def handle(self, tool: Any) -> K12GuardedToolHandle: return K12GuardedToolHandle(tool, self.__token)

    def mint(self, handle: K12GuardedToolHandle, **ids: str) -> K12GuardedToolCapability:
        with self.__lock:
            if not isinstance(handle, K12GuardedToolHandle): raise K12GuardedBackendError("typed handle required")
            if getattr(handle, "_K12GuardedToolHandle__authority") is not self.__token: raise K12GuardedBackendError("handle is not bound to this parent")
            ids = dict(ids); ids.setdefault("profile_digest", self.__profile.profile_digest)
            required = {f.name for f in fields(K12CapabilityBinding)}
            if set(ids) != required or ids.get("profile_id") != self.__profile.profile_id or ids.get("profile_digest") != self.__profile.profile_digest or ids.get("campaign_id") != self.__campaign:
                raise K12GuardedBackendError("mint identities are not parent-bound")
            if id(handle) in self.__minted or ids["cell_id"] in self.__cells or ids["runtime_id"] in self.__runtimes or ids["tool_id"] in self.__tools:
                raise K12GuardedBackendError("one tool capability per cell is permitted")
            self.__minted.add(id(handle)); self.__cells.add(ids["cell_id"]); self.__runtimes.add(ids["runtime_id"]); self.__tools.add(ids["tool_id"])
            cap = K12GuardedToolCapability(K12CapabilityBinding(**ids), K12GuardedToolCapability._K12GuardedToolCapability__MINT)
            self.__caps.add(cap); self.__states[cap]=CapabilityState.CREATED; self.__handles[cap]=handle; return cap

    def _ids(self, cap: K12GuardedToolCapability, ids: Mapping[str, Any]) -> None:
        expected = {f.name for f in fields(cap.binding)}; supplied = dict(ids)
        supplied.setdefault("profile_digest", cap.binding.profile_digest)
        if set(supplied) != expected or any(supplied[k] != getattr(cap.binding, k) for k in expected): self._fail(cap, "capability identity mismatch")

    def _fail(self, cap: K12GuardedToolCapability, msg: str) -> None:
        if cap not in self.__caps: raise K12GuardedBackendError("capability is not parent-owned")
        self.__states[cap]=CapabilityState.POISONED; raise K12GuardedBackendError(msg)

    def _move(self, cap: K12GuardedToolCapability, name: str, expected: CapabilityState, target: CapabilityState, ids: Mapping[str, Any]) -> None:
        with self.__lock:
            if cap not in self.__caps: raise K12GuardedBackendError("capability is not parent-owned")
            self._ids(cap, ids)
            if self.__states[cap] is not expected: self._fail(cap,"invalid or duplicate capability transition")
            self.__states[cap]=target
            self.__evidence.append({"authority": "parent", "transition": name, "capability_digest": cap.capability_digest})

    def bind(self, cap: K12GuardedToolCapability, **ids: str) -> None: self._move(cap, "bind", CapabilityState.CREATED, CapabilityState.BOUND, ids)
    def validate_request(self, cap: K12GuardedToolCapability, **ids: str) -> None: self._move(cap, "validate_request", CapabilityState.BOUND, CapabilityState.REQUEST_VALIDATED, ids)
    def issue_permit(self, cap: K12GuardedToolCapability, **ids: str) -> None: self._move(cap, "issue_permit", CapabilityState.REQUEST_VALIDATED, CapabilityState.PERMIT_ISSUED, ids)

    def enter_effect(self, cap: K12GuardedToolCapability, **ids: str) -> None:
        self._move(cap, "effect_entered", CapabilityState.PERMIT_ISSUED, CapabilityState.EFFECT_ENTERED, ids)

    def terminal(self, cap: K12GuardedToolCapability, *, success: bool, **ids: str) -> None:
        if type(success) is not bool: self._fail(cap, "known terminal outcome required")
        self._move(cap, "terminal", CapabilityState.EFFECT_ENTERED, CapabilityState.EFFECT_TERMINAL, ids)
        self._move(cap, "revoke", CapabilityState.EFFECT_TERMINAL, CapabilityState.REVOKED, ids)

    def poison(self, cap: K12GuardedToolCapability, **ids: str) -> None:
        with self.__lock:
            if cap not in self.__caps: raise K12GuardedBackendError("capability is not parent-owned")
            self._ids(cap, ids); self.__states[cap]=CapabilityState.POISONED
            self.__evidence.append({"authority": "parent", "transition": "poison", "capability_digest": cap.capability_digest})

    def revoke(self, cap: K12GuardedToolCapability, **ids: str) -> None:
        with self.__lock:
            self._ids(cap, ids)
            state=self.__states.get(cap)
            if state not in {CapabilityState.BOUND, CapabilityState.REQUEST_VALIDATED}: self._fail(cap, "invalid or duplicate capability transition")
            self.__states[cap]=CapabilityState.REVOKED; self.__evidence.append({"authority": "parent", "transition": "revoke", "capability_digest": cap.capability_digest})

    def admit_effect(self, cap: K12GuardedToolCapability, action: str, *, ids: Mapping[str, str], **arguments: Any) -> K12AdmittedEffectEntry:
        with self.__lock:
            if action not in ACTION_MAPPINGS or cap.binding.action_id != action: self._fail(cap, "action identity mismatch")
            self._ids(cap, ids); spec = _ARGUMENTS[action]
            if set(arguments) != set(spec) or any(type(arguments[k]) is not t for k, t in spec.items()): self._fail(cap, "strict action arguments required")
            if canonical_sha256(arguments) != cap.binding.argument_digest: self._fail(cap, "admitted action arguments changed")
            if self.__states.get(cap) is not CapabilityState.PERMIT_ISSUED: self._fail(cap, "effect admission requires an issued permit")
            consumed=False; lock=self.__lock; token=self.__token; evidence=self.__evidence
            states=self.__states; handle=self.__handles[cap]
            def consume()->Any:
                nonlocal consumed
                with lock:
                    if consumed: raise K12GuardedBackendError("entry is not parent-issued or was already consumed")
                    if states.get(cap) is not CapabilityState.PERMIT_ISSUED: raise K12GuardedBackendError("invalid capability state")
                    consumed=True; states[cap]=CapabilityState.EFFECT_ENTERED
                    evidence.append({"authority":"parent","transition":"effect_entered","capability_digest":cap.capability_digest})
                    try: result=handle._invoke(token,action,arguments)
                    except BaseException:
                        states[cap]=CapabilityState.POISONED; evidence.append({"authority":"parent","transition":"poison","capability_digest":cap.capability_digest}); raise
                    states[cap]=CapabilityState.EFFECT_TERMINAL; states[cap]=CapabilityState.REVOKED
                    evidence.extend({"authority":"parent","transition":name,"capability_digest":cap.capability_digest} for name in ("terminal","revoke")); return result
            entry = K12AdmittedEffectEntry(cap, action, tuple(arguments.items()), tuple(dict(ids).items()),consume,K12AdmittedEffectEntry._K12AdmittedEffectEntry__MINT)
            self.__evidence.append({"authority": "parent", "transition": "effect_admitted", "capability_digest": cap.capability_digest})
            return entry

    def consume(self, entry: K12AdmittedEffectEntry) -> Any:
        if not isinstance(entry,K12AdmittedEffectEntry): raise K12GuardedBackendError("parent-issued admitted effect entry required")
        return entry._consume()


class K12GuardedBackend:
    def execute(self, action: str, capability: K12GuardedToolCapability, *, ids: Mapping[str, str], entry: K12AdmittedEffectEntry | None = None, **arguments: Any) -> Any:
        if (entry is None or entry.capability is not capability or entry.action != action
                or dict(entry.arguments) != arguments or dict(entry.ids) != dict(ids)):
            raise K12GuardedBackendError("parent-issued admitted effect entry required")
        return entry._consume()


GuardedK12Backend = K12GuardedBackend
__all__ = ["ACTION_MAPPINGS", "CapabilityState", "K12AuthenticatedProfile", "K12CapabilityBinding", "K12AdmittedEffectEntry", "K12GuardedBackend", "GuardedK12Backend", "K12GuardedBackendError", "K12GuardedToolCapability", "K12GuardedToolHandle", "K12ParentAuthority", "K12ScriptedMockTool"]
