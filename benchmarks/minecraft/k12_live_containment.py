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
from .k12_execution_provenance import AuthorityBinding

DEADLINE_SECONDS = 180
TERM_GRACE_SECONDS = 5
KILL_GRACE_SECONDS = 5
RUNTIME_VERIFIED_ORIGIN = "runtime_verified"
INJECTED_FAKE_ORIGIN = "injected_fake"
TEST_ONLY_ORIGIN = "test_only"


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
    authority_binding: AuthorityBinding
    evidence_origin: str
    digest:str
    def __post_init__(self):
        if (
            not isinstance(self.authority_binding, AuthorityBinding)
            or self.evidence_origin != INJECTED_FAKE_ORIGIN
            or self.authority_binding.origin != self.evidence_origin
        ):
            raise ContainmentError("final procfs authority origin mismatch")
    @classmethod
    def make(
        cls,
        retained:tuple[Descendant,...],
        observed:tuple[Descendant|None,...],
        final_census_digest:str,
        *,
        authority_binding: AuthorityBinding,
        evidence_origin: str,
    ):
        body=[[x.pid,x.start,x.exe,x.ppid,x.pgid,x.cgroup] for x in retained]
        seen=[None if x is None else [x.pid,x.start,x.exe,x.ppid,x.pgid,x.cgroup] for x in observed]
        return cls(
            retained,
            observed,
            final_census_digest,
            authority_binding,
            evidence_origin,
            _digest([
                body,
                seen,
                final_census_digest,
                authority_binding.canonical(),
                evidence_origin,
            ]),
        )


def _cell_authority_parts(
    cell_authority: Any,
    *,
    require_current: bool = False,
    require_launched: bool = False,
) -> tuple[str, str, AuthorityBinding, str]:
    """Return exact identity; current checks require completed lifecycle state."""

    try:
        from .k12_live_validation import FinalCellAuthority
    except (ImportError, AttributeError) as exc:  # pragma: no cover - import guard
        raise ContainmentError("typed final-cell authority is unavailable") from exc
    if not isinstance(cell_authority, FinalCellAuthority):
        raise ContainmentError("typed final-cell authority is required")
    binding = getattr(cell_authority, "binding", None)
    origin = getattr(cell_authority, "evidence_origin", None)
    if (
        not isinstance(binding, AuthorityBinding)
        or origin != INJECTED_FAKE_ORIGIN
        or binding.origin != INJECTED_FAKE_ORIGIN
        or binding.namespace != "live_final"
        or binding.lifecycle != "active"
        or cell_authority.runtime_admissible
    ):
        raise ContainmentError("runtime or mismatched containment authority is denied")
    if require_current:
        try:
            cell_authority.require_for_containment()
        except Exception as exc:
            raise ContainmentError(
                "final-cell containment authority is incomplete; stale or revoked"
            ) from exc
    elif require_launched and not cell_authority.launch_consumed:
        raise ContainmentError("final-cell launch admission is not consumed")
    return cell_authority.identity, cell_authority.cell_id, binding, origin


def _bind_containment_io(io: "MockContainmentIO", cell_authority: Any) -> None:
    """Bind one scripted transport to exactly one final-cell authority."""

    bound = getattr(io, "_cell_authority", None)
    if bound is not None and bound is not cell_authority:
        raise ContainmentError("containment controller authority identity changed")
    if bound is None:
        io._cell_authority = cell_authority


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
        self._cell_authority: Any = None

    @property
    def evidence_origin(self) -> str:
        return INJECTED_FAKE_ORIGIN

    @property
    def origin(self) -> str:
        return self.evidence_origin

    @property
    def runtime_admissible(self) -> bool:
        return False

    @property
    def injected_test_only(self) -> bool:
        return True

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

    def revalidate_retained(
        self,
        retained: Mapping[int, Descendant],
        final_census_digest: str,
        *,
        authority_binding: AuthorityBinding,
        evidence_origin: str,
    ) -> FinalProcfsAuthority:
        if set(self._final_identities) != set(retained):
            raise ContainmentError("retained procfs identity is unreadable or missing")
        for pid, expected in retained.items():
            observed=self._final_identities[pid]
            if observed is None: continue
            if observed != expected: raise ContainmentError("retained descendant PID reuse or identity drift")
            raise ContainmentError("containment not clean: retained descendant survived finalization")
        ordered=tuple(retained[pid] for pid in sorted(retained))
        return FinalProcfsAuthority.make(
            ordered,
            tuple(self._final_identities[x.pid] for x in ordered),
            final_census_digest,
            authority_binding=authority_binding,
            evidence_origin=evidence_origin,
        )


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
    cell_id: str = ""
    cell_authority_identity: str = ""
    authority_binding: AuthorityBinding | None = None
    evidence_origin: str = ""
    cell_authority: Any = field(default=None, repr=False, compare=False)
    _final_marker: object = field(default=None,repr=False,compare=False)
    def __post_init__(self):
        identity, cell_id, binding, origin = _cell_authority_parts(self.cell_authority)
        if (
            self.cell_id != cell_id
            or self.cell_authority_identity != identity
            or self.authority_binding != binding
            or self.evidence_origin != origin
        ):
            raise ContainmentError("containment final-cell authority binding mismatch")
        systemd=self.systemd_authority; cgroup=self.cgroup_authority; procfs=self.procfs_authority
        if (systemd.digest!=_digest([systemd.unit,systemd.main_pid,systemd.control_group,systemd.active_state,systemd.sub_state,systemd.sequence])
                or cgroup.digest!=_digest([cgroup.control_group,cgroup.processes,cgroup.events_populated,cgroup.sequence,cgroup.observed_ns])
                or procfs.digest!=_digest({"sequence":procfs.sequence,"processes":[[x.pid,x.start,x.exe,x.ppid,x.pgid,x.cgroup] for x in procfs.processes]})):
            raise ContainmentError("containment authority digest mismatch")
        if (self.main_pid!=systemd.main_pid or self.control_group!=systemd.control_group
                or self.active_state!=systemd.active_state or self.cgroup_procs!=cgroup.processes
                or self.events_populated!=cgroup.events_populated): raise ContainmentError("containment authority binding mismatch")

    @property
    def authority(self) -> Any:
        return self.cell_authority

    @property
    def binding(self) -> AuthorityBinding:
        return self.authority_binding

    @property
    def origin(self) -> str:
        return self.evidence_origin


_FINAL_MARKER=object()
def validate_final_observation(
    observation: LiveObservation, cell_authority: Any = None
) -> bool:
    if not isinstance(observation,LiveObservation) or observation._final_marker is not _FINAL_MARKER: return False
    selected = observation.cell_authority if cell_authority is None else cell_authority
    if selected is not observation.cell_authority:
        return False
    try:
        identity, cell_id, binding, origin = _cell_authority_parts(
            selected, require_current=True, require_launched=True,
        )
    except (ContainmentError, TypeError, ValueError):
        return False
    if (
        observation.cell_id != cell_id
        or observation.cell_authority_identity != identity
        or observation.authority_binding != binding
        or observation.evidence_origin != origin
    ):
        return False
    final=observation.final_procfs_authority
    if (
        not isinstance(final,FinalProcfsAuthority)
        or any(item is not None for item in final.observed)
        or final.authority_binding != observation.authority_binding
        or final.evidence_origin != observation.evidence_origin
    ): return False
    body=[[x.pid,x.start,x.exe,x.ppid,x.pgid,x.cgroup] for x in final.retained]
    if final.final_census_digest!=observation.procfs_authority.digest or final.digest!=_digest([
        body,
        list(final.observed),
        final.final_census_digest,
        final.authority_binding.canonical(),
        final.evidence_origin,
    ]): return False
    expected=_digest([observation.systemd_authority.digest,observation.cgroup_authority.digest,
                      observation.procfs_authority.digest,final.digest,observation.retained_digest,
                      observation.cell_id,observation.cell_authority_identity,
                      observation.authority_binding.canonical(),observation.evidence_origin])
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
                      *, expected_pid: tuple[int, int] | None = None,
                      cell_authority: Any = None,
                      authority: Any = None,
                      controller: Any = None) -> LiveObservation:
    if type(io) is not MockContainmentIO:
        raise TypeError("concrete MockContainmentIO required")
    supplied = tuple(
        value for value in (cell_authority, authority, controller) if value is not None
    )
    if len({id(value) for value in supplied}) > 1:
        raise ContainmentError("containment authority identity changed")
    selected_authority = supplied[0] if supplied else None
    identity, cell_id, binding, origin = _cell_authority_parts(
        selected_authority, require_current=True,
    )
    if io.evidence_origin != origin or io.runtime_admissible:
        raise ContainmentError("runtime containment I/O is denied")
    _bind_containment_io(io, selected_authority)
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
    if final and events != 0:
        raise ContainmentError("final cgroup remains populated")
    mains=[item for item in procfs.processes if item.pid==pid]
    if active=="inactive" and pid==0: start=0
    elif pid<=0 or len(mains)!=1: raise ContainmentError("MainPID escaped its cgroup")
    else: start=mains[0].start
    if expected_pid is not None and pid!=0 and (pid,start)!=expected_pid: raise ContainmentError("process identity drift")
    descendants=tuple(item for item in procfs.processes if item.pid!=pid)
    systemd = SystemdAuthority.make(unit,pid,group,active,sub_state,io._show_index)
    return LiveObservation(
        pid,
        start,
        group,
        active,
        procs,
        events,
        descendants,
        systemd,
        cgroup_authority,
        procfs,
        cell_id=cell_id,
        cell_authority_identity=identity,
        authority_binding=binding,
        evidence_origin=origin,
        cell_authority=selected_authority,
    )


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
    """Injected containment after launch and terminal cell completion."""

    def __init__(
        self,
        io: MockContainmentIO,
        *,
        unit: str,
        cgroup: str,
        clock: Any,
        cell_authority: Any = None,
        authority: Any = None,
        controller: Any = None,
    ):
        if type(io) is not MockContainmentIO:
            raise TypeError("concrete MockContainmentIO required")
        supplied = tuple(
            value for value in (cell_authority, authority, controller) if value is not None
        )
        if len({id(value) for value in supplied}) > 1:
            raise ContainmentError("containment authority identity changed")
        selected_authority = supplied[0] if supplied else None
        identity, cell_id, binding, origin = _cell_authority_parts(
            selected_authority, require_current=True,
        )
        if io.evidence_origin != origin or io.runtime_admissible:
            raise ContainmentError("runtime containment I/O is denied")
        _bind_containment_io(io, selected_authority)
        self.io, self.unit, self.cgroup, self.clock = io, unit, cgroup, clock
        self.cell_authority = selected_authority
        self.controller = selected_authority
        self.cell_id = cell_id
        self.cell_authority_identity = identity
        self.authority_binding = binding
        self.binding = binding
        self.evidence_origin = origin
        self.origin = origin
        self.started = clock()
        self.deadline = self.started + DEADLINE_SECONDS
        self.state = LiveState.PREFLIGHT
        self.initial_pid: tuple[int, int] | None = None
        self._systemd_authority: SystemdAuthority | None = None
        self._descendant_identities: dict[int, Descendant] = {}
        self._authority_sequence = 0

    def _require_current(self, *, require_launched: bool = False) -> None:
        identity, cell_id, binding, origin = _cell_authority_parts(
            self.cell_authority,
            require_current=True,
            require_launched=require_launched,
        )
        if (
            identity != self.cell_authority_identity
            or cell_id != self.cell_id
            or binding != self.authority_binding
            or origin != self.evidence_origin
        ):
            raise ContainmentError("containment authority identity changed")

    def command(self, argv: Sequence[str]) -> tuple[str, ...]:
        return systemd_run_command(self.unit, argv)

    def preflight(self) -> LiveObservation:
        self._require_current()
        observation = parse_observation(
            self.io,
            self.unit,
            self.cgroup,
            cell_authority=self.cell_authority,
        )
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

    def stop(
        self,
        cell_authority: Any = None,
        *,
        authority: Any = None,
        controller: Any = None,
    ) -> LiveObservation:
        try:
            supplied = tuple(
                value for value in (cell_authority, authority, controller) if value is not None
            )
            if len({id(value) for value in supplied}) > 1:
                raise ContainmentError("containment authority identity changed")
            if supplied and supplied[0] is not self.cell_authority:
                raise ContainmentError("containment authority identity changed")
            self._require_current(require_launched=True)
            if self.state is LiveState.PREFLIGHT:
                self.preflight()
            self._deadline()
            if self.initial_pid is None or self._systemd_authority is None:
                raise ContainmentError("missing systemd authority")
            # Signals are emitted only from the immutable systemd authority.
            self.io.signal_from_authority(self._systemd_authority, "TERM")
            self.state = LiveState.TERM
            observation = parse_observation(
                self.io,
                self.unit,
                self.cgroup,
                expected_pid=self.initial_pid,
                cell_authority=self.cell_authority,
            )
            self._accept_authorities(observation)
            self._remember_descendants(observation.descendants)
            self._deadline()
            if not self.io.wait_empty(self.cgroup, TERM_GRACE_SECONDS):
                self._deadline()
                self.io.signal_from_authority(observation.systemd_authority, "KILL")
                self.state = LiveState.KILL
                if not self.io.wait_empty(self.cgroup, KILL_GRACE_SECONDS):
                    raise ContainmentError("cgroup not empty after kill")
            observation = parse_observation(
                self.io,
                self.unit,
                self.cgroup,
                expected_pid=self.initial_pid,
                cell_authority=self.cell_authority,
            )
            self._accept_authorities(observation)
            self._remember_descendants(observation.descendants)
            self._deadline()
            final_procfs=self.io.revalidate_retained(
                self._descendant_identities,
                observation.procfs_authority.digest,
                authority_binding=self.authority_binding,
                evidence_origin=self.evidence_origin,
            )
            self._clean(observation)
            self._require_current(require_launched=True)
            retained_digest=_digest([[x.pid,x.start,x.exe,x.ppid,x.pgid,x.cgroup] for x in sorted(self._descendant_identities.values(),key=lambda row:row.pid)])
            observation=replace(observation,retained_digest=retained_digest,
                final_procfs_authority=final_procfs,
                final_digest=_digest([observation.systemd_authority.digest,observation.cgroup_authority.digest,observation.procfs_authority.digest,final_procfs.digest,retained_digest,
                                      observation.cell_id,observation.cell_authority_identity,
                                      observation.authority_binding.canonical(),observation.evidence_origin]),
                _final_marker=_FINAL_MARKER)
            if not validate_final_observation(observation, self.cell_authority):
                raise ContainmentError("final containment authority digest is invalid")
            self.state = LiveState.CLEAN
            return observation
        except Exception:
            self.state = LiveState.FAILED
            raise


__all__ = ["MockContainmentIO", "Descendant", "SystemdAuthority", "CgroupAuthority", "ProcfsAuthority", "FinalProcfsAuthority", "LiveObservation", "LiveContainment",
           "LiveState", "Probe", "systemd_run_command", "parse_observation", "validate_final_observation",
           "DEADLINE_SECONDS", "TERM_GRACE_SECONDS", "KILL_GRACE_SECONDS",
           "RUNTIME_VERIFIED_ORIGIN", "INJECTED_FAKE_ORIGIN", "TEST_ONLY_ORIGIN"]
