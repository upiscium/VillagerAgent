"""Deterministic, mock-only model of the K12 live containment contract.

This module constructs systemd argv as data.  The concrete mock below is the
only I/O implementation; it cannot execute a command or inspect the host.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
import json
from typing import Any, Mapping, Sequence

from .k12_containment import ContainmentError

DEADLINE_SECONDS = 180
TERM_GRACE_SECONDS = 5
KILL_GRACE_SECONDS = 5


@dataclass(frozen=True, slots=True)
class Descendant:
    pid: int
    start: int
    exe: str
    ppid: int
    pgid: int
    cgroup: str


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SystemdAuthority:
    unit: str
    main_pid: int
    control_group: str
    active_state: str
    sub_state: str
    sequence: int
    digest: str

    @classmethod
    def make(cls, unit: str, pid: int, group: str, state: str, sub_state: str,
             sequence: int) -> "SystemdAuthority":
        return cls(unit, pid, group, state, sub_state, sequence,
                   _digest([unit, pid, group, state, sub_state, sequence]))


@dataclass(frozen=True, slots=True)
class CgroupAuthority:
    control_group: str
    processes: tuple[int, ...]
    events_populated: int
    sequence: int
    observed_ns: int
    digest: str

    @classmethod
    def make(cls, group: str, processes: tuple[int, ...], events: int,
             sequence: int, observed_ns: int) -> "CgroupAuthority":
        return cls(group, processes, events, sequence, observed_ns,
                   _digest([group, processes, events, sequence, observed_ns]))


@dataclass(frozen=True, slots=True)
class ProcfsAuthority:
    processes: tuple[Descendant, ...]
    sequence: int
    digest: str

    @classmethod
    def make(cls, processes: tuple[Descendant, ...], sequence: int) -> "ProcfsAuthority":
        return cls(processes, sequence, _digest({"sequence":sequence,"processes":[[x.pid, x.start, x.exe, x.ppid, x.pgid, x.cgroup] for x in processes]}))

@dataclass(frozen=True,slots=True)
class FinalProcfsAuthority:
    retained:tuple[Descendant,...]
    observed:tuple[Descendant|None,...]
    final_census_digest:str
    digest:str
    @classmethod
    def make(cls,retained:tuple[Descendant,...],observed:tuple[Descendant|None,...],final_census_digest:str):
        body=[[x.pid,x.start,x.exe,x.ppid,x.pgid,x.cgroup] for x in retained]
        seen=[None if x is None else [x.pid,x.start,x.exe,x.ppid,x.pgid,x.cgroup] for x in observed]
        return cls(retained,observed,final_census_digest,_digest([body,seen,final_census_digest]))


class MockContainmentIO:
    """A finite scripted fake, deliberately incapable of host I/O.

    ``shows`` supplies systemd property text in call order and ``waits``
    supplies the result of each grace-period wait.  Launches are recorded as
    argv data; there is no execute/process method by design.
    """

    def __init__(self, shows: Sequence[str], *, waits: Sequence[bool] = (),
                  descendants: Sequence[Sequence[Descendant]] = (),
                  cgroup_sources: Sequence[tuple[str, tuple[int, ...], int]] = (),
                  final_identities: Mapping[int, Descendant | None] | None = None) -> None:
        if type(shows) is not tuple or not shows or any(not isinstance(x, str) for x in shows):
            raise TypeError("shows must be a non-empty tuple of strings")
        if type(waits) is not tuple or any(type(x) is not bool for x in waits):
            raise TypeError("waits must be a tuple of booleans")
        if type(descendants) is not tuple:
            raise TypeError("descendants must be a tuple")
        if type(cgroup_sources) is not tuple:
            raise TypeError("cgroup_sources must be a tuple")
        self._shows = shows
        self._waits = waits
        self._descendants = descendants
        self._show_index = 0
        self._wait_index = 0
        self._descendant_index = 0
        self._cgroup_sources = cgroup_sources
        self._cgroup_index = 0
        self.signals: list[str] = []
        self.launches: list[tuple[str, ...]] = []
        self.authority_signals: list[str] = []
        self._final_identities = dict(final_identities or {})

    def systemd_show(self, unit: str) -> str:
        if self._show_index >= len(self._shows):
            raise ContainmentError("scripted systemd observation exhausted")
        value = self._shows[self._show_index]
        self._show_index += 1
        return value

    def signal_unit(self, unit: str, signal: str) -> None:
        if signal not in {"TERM", "KILL"}:
            raise ContainmentError("unsupported signal")
        self.signals.append(signal)

    def signal_from_authority(self, authority: SystemdAuthority, signal: str) -> None:
        if not isinstance(authority, SystemdAuthority):
            raise ContainmentError("unknown systemd authority")
        self.signal_unit(authority.unit, signal)
        self.authority_signals.append(signal)

    def wait_empty(self, cgroup: str, seconds: int) -> bool:
        if seconds not in {TERM_GRACE_SECONDS, KILL_GRACE_SECONDS}:
            raise ContainmentError("unsupported grace period")
        if self._wait_index >= len(self._waits):
            raise ContainmentError("scripted wait exhausted")
        result = self._waits[self._wait_index]
        self._wait_index += 1
        return result

    def procfs_authority(self) -> ProcfsAuthority:
        if self._descendant_index >= len(self._descendants):
            raise ContainmentError("scripted procfs authority exhausted")
        result = self._descendants[self._descendant_index]
        self._descendant_index += 1
        if any(type(item) is not Descendant for item in result): raise ContainmentError("untyped procfs authority")
        return ProcfsAuthority.make(tuple(result),self._descendant_index)

    def cgroup_authority(self) -> CgroupAuthority:
        if self._cgroup_index >= len(self._cgroup_sources): raise ContainmentError("scripted cgroup authority exhausted")
        value = self._cgroup_sources[self._cgroup_index]
        self._cgroup_index += 1
        try:
            return CgroupAuthority.make(*value,self._cgroup_index,self._cgroup_index*1_000_000)
        except (TypeError, ValueError) as exc:
            raise ContainmentError("unreadable cgroup authority") from exc

    def record_launch(self, command: tuple[str, ...]) -> None:
        self.launches.append(command)

    def revalidate_retained(self, retained: Mapping[int, Descendant],final_census_digest:str) -> FinalProcfsAuthority:
        if set(self._final_identities) != set(retained):
            raise ContainmentError("retained procfs identity is unreadable or missing")
        for pid, expected in retained.items():
            observed=self._final_identities[pid]
            if observed is None: continue
            if observed != expected: raise ContainmentError("retained descendant PID reuse or identity drift")
            raise ContainmentError("containment not clean: retained descendant survived finalization")
        ordered=tuple(retained[pid] for pid in sorted(retained))
        return FinalProcfsAuthority.make(ordered,tuple(self._final_identities[x.pid] for x in ordered),final_census_digest)


@dataclass(frozen=True, slots=True)
class LiveObservation:
    main_pid: int
    main_start: int
    control_group: str
    active_state: str
    cgroup_procs: tuple[int, ...]
    events_populated: int
    descendants: tuple[Descendant, ...]
    systemd_authority: SystemdAuthority
    cgroup_authority: CgroupAuthority
    procfs_authority: ProcfsAuthority
    final_procfs_authority: FinalProcfsAuthority|None = None
    retained_digest: str = ""
    final_digest: str = ""
    _final_marker: object = field(default=None,repr=False,compare=False)
    def __post_init__(self):
        systemd=self.systemd_authority; cgroup=self.cgroup_authority; procfs=self.procfs_authority
        if (systemd.digest!=_digest([systemd.unit,systemd.main_pid,systemd.control_group,systemd.active_state,systemd.sub_state,systemd.sequence])
                or cgroup.digest!=_digest([cgroup.control_group,cgroup.processes,cgroup.events_populated,cgroup.sequence,cgroup.observed_ns])
                or procfs.digest!=_digest({"sequence":procfs.sequence,"processes":[[x.pid,x.start,x.exe,x.ppid,x.pgid,x.cgroup] for x in procfs.processes]})):
            raise ContainmentError("containment authority digest mismatch")
        if (self.main_pid!=systemd.main_pid or self.control_group!=systemd.control_group
                or self.active_state!=systemd.active_state or self.cgroup_procs!=cgroup.processes
                or self.events_populated!=cgroup.events_populated): raise ContainmentError("containment authority binding mismatch")


_FINAL_MARKER=object()
def validate_final_observation(observation:LiveObservation)->bool:
    if not isinstance(observation,LiveObservation) or observation._final_marker is not _FINAL_MARKER: return False
    final=observation.final_procfs_authority
    if not isinstance(final,FinalProcfsAuthority) or any(item is not None for item in final.observed): return False
    body=[[x.pid,x.start,x.exe,x.ppid,x.pgid,x.cgroup] for x in final.retained]
    if final.final_census_digest!=observation.procfs_authority.digest or final.digest!=_digest([body,list(final.observed),final.final_census_digest]): return False
    expected=_digest([observation.systemd_authority.digest,observation.cgroup_authority.digest,
                      observation.procfs_authority.digest,final.digest,observation.retained_digest])
    return observation.final_digest==expected


def systemd_run_command(unit: str, argv: Sequence[str]) -> tuple[str, ...]:
    if (not isinstance(unit, str) or not unit or not isinstance(argv, Sequence)
            or isinstance(argv, (str, bytes)) or not argv
            or any(type(item) is not str or not item for item in argv)):
        raise ValueError("unit and argv are required")
    return ("systemd-run", "--user", f"--unit={unit}", "--collect",
            "--service-type=exec", "--same-dir", "--quiet", "--", *tuple(argv))


def _properties(text: str) -> dict[str, str]:
    if not isinstance(text, str):
        raise ContainmentError("systemd output is not text")
    result: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            raise ContainmentError("malformed systemd output")
        key, value = line.split("=", 1)
        if not key or key in result:
            raise ContainmentError("duplicate systemd property")
        result[key] = value
    return result


def _int(properties: dict[str, str], name: str) -> int:
    try:
        return int(properties[name], 10)
    except (KeyError, TypeError, ValueError) as exc:
        raise ContainmentError(f"invalid {name}") from exc


def parse_observation(io: MockContainmentIO, unit: str, cgroup: str,
                      *, expected_pid: tuple[int, int] | None = None) -> LiveObservation:
    if type(io) is not MockContainmentIO:
        raise TypeError("concrete MockContainmentIO required")
    props = _properties(io.systemd_show(unit))
    pid = _int(props, "MainPID")
    group, active, sub_state = props.get("ControlGroup"), props.get("ActiveState"), props.get("SubState")
    if set(props)!={"MainPID","ControlGroup","ActiveState","SubState"} or not isinstance(group, str) or not group or active not in {"active", "activating", "deactivating", "inactive"} or not isinstance(sub_state,str) or not sub_state:
        raise ContainmentError("incomplete systemd observation")
    if group != cgroup: raise ContainmentError("process or cgroup identity drift")
    cgroup_authority=io.cgroup_authority(); procfs=io.procfs_authority()
    procs=cgroup_authority.processes; events=cgroup_authority.events_populated
    if (cgroup_authority.control_group!=group or events not in (0,1)
            or any(value<=0 for value in procs) or len(set(procs))!=len(procs)):
        raise ContainmentError("independent cgroup authority drift")
    if any(item.pid<=0 or item.start<=0 or not item.exe or item.cgroup!=group for item in procfs.processes):
        raise ContainmentError("procfs identity escaped or is unknown")
    if set(procs)!={item.pid for item in procfs.processes}: raise ContainmentError("cgroup/procfs membership mismatch")
    final=active=="inactive" and pid==0 and not procs and not procfs.processes
    mains=[item for item in procfs.processes if item.pid==pid]
    if active=="inactive" and pid==0: start=0
    elif pid<=0 or len(mains)!=1: raise ContainmentError("MainPID escaped its cgroup")
    else: start=mains[0].start
    if expected_pid is not None and pid!=0 and (pid,start)!=expected_pid: raise ContainmentError("process identity drift")
    descendants=tuple(item for item in procfs.processes if item.pid!=pid)
    systemd = SystemdAuthority.make(unit,pid,group,active,sub_state,io._show_index)
    return LiveObservation(pid, start, group, active, procs, events, descendants,
                           systemd, cgroup_authority, procfs)


class LiveState(str, Enum):
    PREFLIGHT = "preflight"
    RUNNING = "running"
    TERM = "term"
    KILL = "kill"
    CLEAN = "clean"
    QUARANTINED = "quarantined"
    FAILED = "failed"


class Probe(str, Enum):
    P1 = "P1"  # cooperative TERM-only child
    P2 = "P2"  # TERM-ignoring child requires KILL
    P3 = "P3"  # setsid descendant remains in the cgroup and is removed
    P4 = "P4"  # unknown cgroup read quarantines and denies the next launch


class LiveContainment:
    def __init__(self, io: MockContainmentIO, *, unit: str, cgroup: str, clock: Any):
        if type(io) is not MockContainmentIO:
            raise TypeError("concrete MockContainmentIO required")
        self.io, self.unit, self.cgroup, self.clock = io, unit, cgroup, clock
        self.started = clock()
        self.deadline = self.started + DEADLINE_SECONDS
        self.state = LiveState.PREFLIGHT
        self.initial_pid: tuple[int, int] | None = None
        self._systemd_authority: SystemdAuthority | None = None
        self._descendant_identities: dict[int, Descendant] = {}
        self._authority_sequence = 0

    def command(self, argv: Sequence[str]) -> tuple[str, ...]:
        return systemd_run_command(self.unit, argv)

    def preflight(self) -> LiveObservation:
        observation = parse_observation(self.io, self.unit, self.cgroup)
        if observation.active_state not in {"active", "activating"} or observation.main_pid not in observation.cgroup_procs:
            raise ContainmentError("MainPID is not a cgroup member")
        self.initial_pid = (observation.main_pid, observation.main_start)
        self._systemd_authority = observation.systemd_authority
        self._accept_authorities(observation)
        self._remember_descendants(observation.descendants)
        self.state = LiveState.RUNNING
        return observation

    def _deadline(self) -> None:
        if self.clock() >= self.deadline:
            raise ContainmentError("containment deadline exceeded")

    def _clean(self, observation: LiveObservation) -> None:
        if observation.active_state != "inactive" or observation.control_group != self.cgroup or observation.cgroup_procs or observation.events_populated != 0 or observation.descendants:
            raise ContainmentError("containment is not clean")

    def _remember_descendants(self, descendants: tuple[Descendant, ...]) -> None:
        for child in descendants:
            prior = self._descendant_identities.get(child.pid)
            if prior is not None and prior != child:
                raise ContainmentError("descendant PID reuse or identity drift")
            self._descendant_identities[child.pid] = child

    def _accept_authorities(self, observation: LiveObservation) -> None:
        sequences=(observation.systemd_authority.sequence,observation.cgroup_authority.sequence,observation.procfs_authority.sequence)
        if len(set(sequences))!=1 or sequences[0]<=self._authority_sequence: raise ContainmentError("stale containment authority sequence")
        self._authority_sequence=sequences[0]

    def stop(self) -> LiveObservation:
        try:
            if self.state is LiveState.PREFLIGHT:
                self.preflight()
            self._deadline()
            if self.initial_pid is None or self._systemd_authority is None:
                raise ContainmentError("missing systemd authority")
            # Signals are emitted only from the immutable systemd authority.
            self.io.signal_from_authority(self._systemd_authority, "TERM")
            self.state = LiveState.TERM
            observation = parse_observation(self.io, self.unit, self.cgroup, expected_pid=self.initial_pid)
            self._accept_authorities(observation)
            self._remember_descendants(observation.descendants)
            self._deadline()
            if not self.io.wait_empty(self.cgroup, TERM_GRACE_SECONDS):
                self._deadline()
                self.io.signal_from_authority(observation.systemd_authority, "KILL")
                self.state = LiveState.KILL
                if not self.io.wait_empty(self.cgroup, KILL_GRACE_SECONDS):
                    raise ContainmentError("cgroup not empty after kill")
            observation = parse_observation(self.io, self.unit, self.cgroup, expected_pid=self.initial_pid)
            self._accept_authorities(observation)
            self._remember_descendants(observation.descendants)
            self._deadline()
            final_procfs=self.io.revalidate_retained(self._descendant_identities,observation.procfs_authority.digest)
            self._clean(observation)
            retained_digest=_digest([[x.pid,x.start,x.exe,x.ppid,x.pgid,x.cgroup] for x in sorted(self._descendant_identities.values(),key=lambda row:row.pid)])
            observation=replace(observation,retained_digest=retained_digest,
                final_procfs_authority=final_procfs,
                final_digest=_digest([observation.systemd_authority.digest,observation.cgroup_authority.digest,observation.procfs_authority.digest,final_procfs.digest,retained_digest]),
                _final_marker=_FINAL_MARKER)
            self.state = LiveState.CLEAN
            return observation
        except Exception:
            self.state = LiveState.FAILED
            raise


__all__ = ["MockContainmentIO", "Descendant", "SystemdAuthority", "CgroupAuthority", "ProcfsAuthority", "FinalProcfsAuthority", "LiveObservation", "LiveContainment",
           "LiveState", "Probe", "systemd_run_command", "parse_observation", "validate_final_observation",
           "DEADLINE_SECONDS", "TERM_GRACE_SECONDS", "KILL_GRACE_SECONDS"]
