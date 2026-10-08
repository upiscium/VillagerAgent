"""Mock-only live probe runner; it never executes prepared argv."""
from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Sequence

from .k12_containment import ContainmentError, containment_identity
from .k12_live_containment import LiveContainment, MockContainmentIO, systemd_run_command
from .k12_guarded_backend import K12AuthenticatedProfile
from .k12_live_state import LiveState as NormalizedLiveState, ParentPlanAuthority, validate_state


@dataclass(frozen=True, slots=True)
class LiveLaunch:
    unit_id: str
    cgroup_id: str
    command: tuple[str, ...]
    executed: bool = False


@dataclass(frozen=True, slots=True)
class LaunchKey:
    profile_digest: str
    campaign: str
    cohort: str
    cell: str
    reset_generation: int
    reset_token: str
    reset_attestation: str
    launch: str
    namespace: str


class ParentLaunchAuthority:
    """Parent-owned, one-shot reservation ledger (entirely in memory)."""
    _owners_lock=threading.Lock()
    _owners:set[tuple[str,str,str]]=set()

    def __init__(self, profile: K12AuthenticatedProfile, campaign: str, cohort: str,plan_authority:ParentPlanAuthority) -> None:
        if (not isinstance(profile,K12AuthenticatedProfile) or not isinstance(plan_authority,ParentPlanAuthority)
                or plan_authority.profile is not profile or plan_authority.campaign!=campaign
                or not all(isinstance(value,str) and value for value in (campaign,cohort))):
            raise TypeError("authenticated profile and campaign/cohort required")
        owner=(profile.profile_digest,campaign,cohort)
        with self._owners_lock:
            if owner in self._owners: raise ContainmentError("campaign launch authority already exists")
            self._owners.add(owner)
        self.profile,self.campaign,self.cohort,self.plan_authority=profile,campaign,cohort,plan_authority
        self._lock = threading.Lock()
        self._reserved: set[LaunchKey] = set()
        self._consumed:set[LaunchKey]=set()
        self._cells: set[tuple[str,str,str,str,str]] = set()
        self._launches: set[tuple[str,str,str,str]] = set()
        self._tokens: set[tuple[str,str,str,str]] = set()
        self._generation: dict[tuple[str,str,str,str,str],int] = {}
        self._blocked = False

    def reserve(self, state: NormalizedLiveState, launch: str, namespace: str) -> LaunchKey:
        validate_state(state)
        if (state.profile!=self.profile.profile_digest or state.campaign!=self.campaign
                or not state.reset_attestation_sha256 or not state.plan_authority_digest
                or not self.plan_authority.owns_state(state)
                or namespace not in {"qualification","probe","final"}):
            raise ContainmentError("launch/reset/profile authority mismatch")
        key=LaunchKey(state.profile,self.campaign,self.cohort,state.cell,state.generation,
                      state.reset_token,state.reset_attestation_sha256,launch,namespace)
        with self._lock:
            if self._blocked:
                raise ContainmentError("launch is blocked after quarantine or unknown outcome")
            if key in self._reserved:
                raise ContainmentError("launch reservation already consumed; retry/resume/replacement denied")
            cell_key=(key.namespace,key.profile_digest,key.campaign,key.cohort,key.cell)
            launch_key=(key.namespace,key.profile_digest,key.campaign,key.launch)
            token_key=(key.namespace,key.profile_digest,key.campaign,key.reset_token)
            if (cell_key in self._cells or launch_key in self._launches or token_key in self._tokens
                    or key.reset_generation <= self._generation.get(cell_key,0)):
                raise ContainmentError("cell, launch, token, or generation replay denied")
            self._reserved.add(key)
            self._cells.add(cell_key); self._launches.add(launch_key); self._tokens.add(token_key)
            self._generation[cell_key]=key.reset_generation
            return key

    def block(self) -> None:
        with self._lock:
            self._blocked = True

    def consume(self,key:LaunchKey,callback) -> None:
        with self._lock:
            if self._blocked: raise ContainmentError("campaign launch authority is blocked")
            if key not in self._reserved or key in self._consumed: raise ContainmentError("launch reservation is absent or consumed")
            self._consumed.add(key); callback()


class LiveRunner:
    def __init__(self, *, executor: MockContainmentIO,
                 parent: ParentLaunchAuthority) -> None:
        if type(executor) is not MockContainmentIO:
            raise ContainmentError("concrete MockContainmentIO is required")
        self.executor = executor
        if not isinstance(parent,ParentLaunchAuthority): raise ContainmentError("campaign-owned launch authority required")
        self.parent = parent
        self._campaign_blocked = False
        self._prepared: dict[LaunchKey, LiveLaunch] = {}
        self._lock=threading.RLock()

    def prepare(self, state: NormalizedLiveState, launch_id: str, argv: Sequence[str], *,
                namespace: str) -> LiveLaunch:
        with self._lock: return self._prepare(state,launch_id,argv,namespace)
    def _prepare(self,state:NormalizedLiveState,launch_id:str,argv:Sequence[str],namespace:str)->LiveLaunch:
        if self._campaign_blocked or namespace not in {"qualification", "probe", "final"}:
            raise ContainmentError("launch is blocked or namespace is invalid")
        if not isinstance(state,NormalizedLiveState) or not isinstance(launch_id,str) or not launch_id:
            raise ValueError("complete launch reservation identity is required")
        # Namespace is part of the containment identity as well as the ledger key.
        unit, cgroup = containment_identity(f"{namespace}:{state.cell}", launch_id)
        key=self.parent.reserve(state,launch_id,namespace)
        prepared = LiveLaunch(unit, cgroup, systemd_run_command(unit, argv))
        self._prepared[key] = prepared
        return prepared

    def launch(self, state: NormalizedLiveState, launch_id: str, argv: Sequence[str], *, namespace:str) -> LiveLaunch:
        with self._lock:
            if self._campaign_blocked: raise ContainmentError("launch is blocked")
            key=LaunchKey(state.profile,self.parent.campaign,self.parent.cohort,state.cell,
                          state.generation,state.reset_token,state.reset_attestation_sha256,
                          launch_id,namespace)
            prepared = self._prepared.get(key)
            if prepared is None: prepared = self._prepare(state,launch_id,argv,namespace)
            expected=LiveLaunch(prepared.unit_id,prepared.cgroup_id,systemd_run_command(prepared.unit_id,argv))
            if prepared != expected: raise ContainmentError("launch arguments differ from reserved command")
            self._prepared.pop(key,None); self.parent.consume(key,lambda:self.executor.record_launch(prepared.command))
            return LiveLaunch(prepared.unit_id, prepared.cgroup_id, prepared.command, True)

    def block(self) -> None:
        with self._lock:
            self._campaign_blocked = True; self._prepared.clear(); self.parent.block()

    def stop(self, controller: LiveContainment) -> object:
        if not isinstance(controller, LiveContainment) or controller.io is not self.executor:
            self.block()
            raise ContainmentError("runner/controller authority mismatch")
        try:
            return controller.stop()
        except BaseException:
            self.block()
            raise


__all__ = ["LiveLaunch", "LaunchKey", "ParentLaunchAuthority", "LiveRunner"]
