"""Authority-bound live runner with fail-closed runtime and injected-fake paths."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import threading
from typing import Any, Sequence

from benchmarks.common.eac.canonical import canonical_bytes

from .k12_containment import ContainmentError, containment_identity
from .k12_live_containment import LiveContainment, MockContainmentIO, systemd_run_command
from .k12_execution_capsule import DurableLedger
from .k12_execution_provenance import (
    AuthorityBinding,
    FINAL_AUTHORITY,
    INJECTED_FAKE_ORIGIN,
    INJECTED_TEST_ORIGIN,
    RUNTIME_VERIFIED_ORIGIN,
    ActiveFinalAuthority,
    git_blob_oid,
    ProvenanceError,
    raw_sha256,
)
from .k12_guarded_backend import (
    K12AuthenticatedProfile,
    authority_binding_is_current,
    is_mock_authority_binding,
    mock_authority_binding,
    resolve_authority_binding,
)
from .k12_live_state import LiveState as NormalizedLiveState, ParentPlanAuthority, validate_state


EXTERNAL_ENTRY_CHANNELS = (
    "minecraft",
    "rcon",
    "provider_network",
    "systemd_cgroup",
    "worker_process",
    "native_effect",
)
RUNTIME_DISPATCH_MODE = "runtime"
INJECTED_FAKE_DISPATCH_MODE = "injected_fake"


def _normalize_dispatch_mode(
    mode: str = RUNTIME_DISPATCH_MODE,
    *,
    origin: str | None = None,
    injected_fake: bool = False,
) -> tuple[str, str]:
    """Normalize the explicit final dispatch mode and its evidence origin."""

    aliases = {
        "runtime": RUNTIME_DISPATCH_MODE,
        "live_final": RUNTIME_DISPATCH_MODE,
        RUNTIME_VERIFIED_ORIGIN: RUNTIME_DISPATCH_MODE,
        "injected": INJECTED_FAKE_DISPATCH_MODE,
        "injected_test": INJECTED_FAKE_DISPATCH_MODE,
        "fake": INJECTED_FAKE_DISPATCH_MODE,
        INJECTED_FAKE_ORIGIN: INJECTED_FAKE_DISPATCH_MODE,
    }
    if not isinstance(mode, str) or mode not in aliases:
        raise ContainmentError("unknown final dispatch mode")
    selected = aliases[mode]
    if injected_fake:
        selected = INJECTED_FAKE_DISPATCH_MODE
    expected_origin = (
        INJECTED_FAKE_ORIGIN
        if selected == INJECTED_FAKE_DISPATCH_MODE
        else RUNTIME_VERIFIED_ORIGIN
    )
    if origin is not None and origin != expected_origin:
        raise ContainmentError("dispatch mode/evidence origin mismatch")
    return selected, expected_origin


@dataclass(frozen=True, slots=True)
class FakeDispatchRecord:
    """One ordered, inert dispatch made by an injected final runner."""

    ordinal: int
    command: tuple[str, ...]
    cell_id: str
    launch_id: str
    origin: str
    channels: tuple[str, ...] = EXTERNAL_ENTRY_CHANNELS
    authority_binding: AuthorityBinding | None = None


class ExternalEntryFence:
    """Fail-closed boundary for the six external-entry channels.

    The fence does not provide an external adapter.  Runtime callers may only
    record an operational entry, while an injected runner may only record one
    fake dispatch that is fanned out to the six inert channel counters.  A
    denied injected real entry is deliberately not counted as an external
    entry, which makes the negative-path assertion unambiguous.
    """

    channels = EXTERNAL_ENTRY_CHANNELS
    CHANNELS = EXTERNAL_ENTRY_CHANNELS

    def __init__(self, mode: str = RUNTIME_DISPATCH_MODE, *, origin: str | None = None) -> None:
        self.mode, self.origin = _normalize_dispatch_mode(mode, origin=origin)
        self._lock = threading.RLock()
        self._real_counts = {channel: 0 for channel in EXTERNAL_ENTRY_CHANNELS}
        self._fake_counts = {channel: 0 for channel in EXTERNAL_ENTRY_CHANNELS}
        self._denied_counts = {channel: 0 for channel in EXTERNAL_ENTRY_CHANNELS}
        self._fake_dispatches: list[FakeDispatchRecord] = []

    def _check_channel(self, channel: str) -> None:
        if channel not in EXTERNAL_ENTRY_CHANNELS:
            raise ContainmentError("unknown external-entry channel")

    def enter(
        self,
        channel: str,
        *,
        origin: str = RUNTIME_VERIFIED_ORIGIN,
        fake: bool = False,
    ) -> None:
        """Record an allowed entry or deny it before incrementing counters."""

        self._check_channel(channel)
        with self._lock:
            if fake:
                if self.mode != INJECTED_FAKE_DISPATCH_MODE or origin != INJECTED_FAKE_ORIGIN:
                    self._denied_counts[channel] += 1
                    raise ContainmentError("fake external entry is not allowed in this mode")
                self._fake_counts[channel] += 1
                return
            if origin != RUNTIME_VERIFIED_ORIGIN:
                self._denied_counts[channel] += 1
                raise ContainmentError("unverified external entry is denied")
            if self.mode == INJECTED_FAKE_DISPATCH_MODE:
                self._denied_counts[channel] += 1
                raise ContainmentError("real external entry is fenced in injected mode")
            self._real_counts[channel] += 1

    enter_real = enter

    def enter_fake(self, channel: str) -> None:
        self.enter(channel, origin=INJECTED_FAKE_ORIGIN, fake=True)

    def record_fake_dispatch(
        self,
        command: Sequence[str],
        *,
        cell_id: str = "",
        launch_id: str = "",
        authority_binding: AuthorityBinding | None = None,
        channels: Sequence[str] = EXTERNAL_ENTRY_CHANNELS,
    ) -> FakeDispatchRecord:
        """Record one ordered fake dispatch and no real external entry."""

        if not isinstance(command, Sequence) or isinstance(command, (str, bytes)):
            raise ContainmentError("typed final fake dispatch is required")
        command_tuple = tuple(command)
        if (
            not command_tuple
            or any(type(item) is not str or not item for item in command_tuple)
            or tuple(channels) != EXTERNAL_ENTRY_CHANNELS
        ):
            raise ContainmentError("typed final fake dispatch is required")
        if self.mode != INJECTED_FAKE_DISPATCH_MODE:
            raise ContainmentError("fake dispatch is not allowed in runtime mode")
        if authority_binding is not None and authority_binding.origin != INJECTED_FAKE_ORIGIN:
            raise ContainmentError("fake dispatch authority origin mismatch")
        with self._lock:
            for channel in EXTERNAL_ENTRY_CHANNELS:
                self.enter_fake(channel)
            record = FakeDispatchRecord(
                len(self._fake_dispatches), command_tuple, cell_id, launch_id,
                INJECTED_FAKE_ORIGIN, EXTERNAL_ENTRY_CHANNELS, authority_binding,
            )
            self._fake_dispatches.append(record)
            return record

    @property
    def real_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._real_counts)

    @property
    def counters(self) -> dict[str, int]:
        return self.real_counts

    @property
    def external_counters(self) -> dict[str, int]:
        return self.real_counts

    @property
    def entry_counts(self) -> dict[str, int]:
        return self.real_counts

    @property
    def real_entry_counts(self) -> dict[str, int]:
        return self.real_counts

    @property
    def fake_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._fake_counts)

    @property
    def fake_entry_counts(self) -> dict[str, int]:
        return self.fake_counts

    @property
    def denied_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._denied_counts)

    @property
    def fake_dispatches(self) -> tuple[FakeDispatchRecord, ...]:
        with self._lock:
            return tuple(self._fake_dispatches)

    @property
    def ordered_fake_dispatches(self) -> tuple[FakeDispatchRecord, ...]:
        return self.fake_dispatches

    @property
    def fake_dispatch_count(self) -> int:
        return len(self.fake_dispatches)

    def assert_no_real_entries(self) -> None:
        if any(self.real_counts.values()):
            raise AssertionError("real external entries were recorded")

    assert_no_external_entries = assert_no_real_entries
    assert_zero_real_entries = assert_no_real_entries


class InjectedFakeTransport:
    """Inert final transport used only by explicit injected fake mode."""

    def __init__(self, fence: ExternalEntryFence | None = None) -> None:
        self.fence = fence or ExternalEntryFence(INJECTED_FAKE_DISPATCH_MODE)
        if self.fence.mode != INJECTED_FAKE_DISPATCH_MODE:
            raise ContainmentError("injected fake transport requires an injected fence")
        self.dispatches: list[tuple[str, ...]] = []
        self.records: list[FakeDispatchRecord] = []

    def dispatch(
        self,
        command: Sequence[str],
        *,
        cell_id: str = "",
        launch_id: str = "",
        authority_binding: AuthorityBinding | None = None,
    ) -> FakeDispatchRecord:
        record = self.fence.record_fake_dispatch(
            command,
            cell_id=cell_id,
            launch_id=launch_id,
            authority_binding=authority_binding,
        )
        self.dispatches.append(record.command)
        self.records.append(record)
        return record

    def record_launch(self, command: Sequence[str]) -> FakeDispatchRecord:
        return self.dispatch(command)

    @property
    def launches(self) -> list[tuple[str, ...]]:
        return self.dispatches

    @property
    def ordered_dispatches(self) -> list[FakeDispatchRecord]:
        return self.records


FakeDispatchRecorder = InjectedFakeTransport
InjectedFakeRecorder = InjectedFakeTransport
FakeTransport = InjectedFakeTransport
FinalFakeTransport = InjectedFakeTransport


@dataclass(frozen=True, slots=True)
class LiveLaunch:
    unit_id: str
    cgroup_id: str
    command: tuple[str, ...]
    executed: bool = False
    authority_binding: AuthorityBinding = mock_authority_binding()

    @property
    def binding(self) -> AuthorityBinding:
        return self.authority_binding

    @property
    def origin(self) -> str:
        return self.authority_binding.origin

    @property
    def evidence_origin(self) -> str:
        return self.origin


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
    authority_binding: AuthorityBinding = mock_authority_binding()

    @property
    def binding(self) -> AuthorityBinding:
        return self.authority_binding

    @property
    def origin(self) -> str:
        return self.authority_binding.origin

    @property
    def evidence_origin(self) -> str:
        return self.origin


class ParentLaunchAuthority:
    """Parent-owned, one-shot reservation ledger (entirely in memory)."""
    _owners_lock=threading.Lock()
    _owners:set[tuple[str,str,str]]=set()

    def __init__(self, profile: K12AuthenticatedProfile, campaign: str, cohort: str,
                 plan_authority: ParentPlanAuthority, *,
                 authority_binding: AuthorityBinding | None = None,
                 authority: Any = None) -> None:
        inherited_authority = getattr(plan_authority, "authority", None)
        selected_authority = authority if authority is not None else inherited_authority
        binding = resolve_authority_binding(
            selected_authority if selected_authority is not None else authority_binding
        )
        if authority is not None and authority_binding is not None \
                and resolve_authority_binding(authority_binding) != binding:
            raise ContainmentError("launch authority binding mismatch")
        if (not isinstance(profile,K12AuthenticatedProfile) or not isinstance(plan_authority,ParentPlanAuthority)
                or plan_authority.profile is not profile or plan_authority.campaign!=campaign
                or not all(isinstance(value,str) and value for value in (campaign,cohort))):
            raise TypeError("authenticated profile and campaign/cohort required")
        if plan_authority.authority_binding != binding:
            raise ContainmentError("launch/plan authority binding mismatch")
        owner=(profile.profile_digest,campaign,cohort)
        with self._owners_lock:
            if owner in self._owners: raise ContainmentError("campaign launch authority already exists")
            self._owners.add(owner)
        self.profile,self.campaign,self.cohort,self.plan_authority=profile,campaign,cohort,plan_authority
        self.authority_binding, self.authority = binding, selected_authority
        self._lock = threading.Lock()
        self._reserved: set[LaunchKey] = set()
        self._consumed:set[LaunchKey]=set()
        self._cells: set[tuple[str,str,str,str,str]] = set()
        self._launches: set[tuple[str,str,str,str]] = set()
        self._tokens: set[tuple[str,str,str,str]] = set()
        self._generation: dict[tuple[str,str,str,str,str],int] = {}
        self._blocked = False

    @property
    def binding(self) -> AuthorityBinding:
        return self.authority_binding

    @property
    def origin(self) -> str:
        return self.authority_binding.origin

    @property
    def evidence_origin(self) -> str:
        return self.origin

    def _assert_current(self) -> None:
        if is_mock_authority_binding(self.authority_binding):
            return
        if self.authority is None or not authority_binding_is_current(
            self.authority,
            self.authority_binding,
            profile_digest=self.profile.profile_digest,
            allow_injected=self.authority_binding.origin in {
                INJECTED_FAKE_ORIGIN, INJECTED_TEST_ORIGIN,
            },
        ):
            raise ContainmentError("launch authority lifecycle is stale or revoked")

    def _parent_terminalization_guard(self):
        owner = getattr(self.authority, "owner", None)
        factory = getattr(owner, "_terminalization_guard", None)
        return factory() if callable(factory) else nullcontext()

    def _reserve_bound(
        self, state: NormalizedLiveState, launch: str, namespace: str,
        binding: AuthorityBinding,
    ) -> LaunchKey:
        with self._parent_terminalization_guard():
            self._assert_current()
            validate_state(state)
            if (
                state.profile != self.profile.profile_digest
                or state.campaign != self.campaign
                or state.authority_binding != binding
                or not state.reset_attestation_sha256
                or not state.plan_authority_digest
                or not self.plan_authority.owns_state(state)
            ):
                raise ContainmentError("launch/reset/profile authority mismatch")
            key=LaunchKey(state.profile,self.campaign,self.cohort,state.cell,state.generation,
                          state.reset_token,state.reset_attestation_sha256,launch,namespace,binding)
            with self._lock:
                self._assert_current()
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

    def reserve(self, state: NormalizedLiveState, launch: str, namespace: str) -> LaunchKey:
        if namespace not in {"qualification","probe","final"}:
            raise ContainmentError("launch/reset/profile authority mismatch")
        if (
            not is_mock_authority_binding(state.authority_binding)
            and state.authority_binding.provenance == "live_final"
        ):
            raise ContainmentError("caller namespace cannot authorize a final live launch")
        return self._reserve_bound(state, launch, namespace, state.authority_binding)

    def reserve_final(
        self,
        authority: Any,
        state: NormalizedLiveState,
        launch: str,
        *,
        expected_origin: str = RUNTIME_VERIFIED_ORIGIN,
    ) -> LaunchKey:
        """Reserve a final cell using only its typed admission capability."""
        self._assert_current()
        try:
            from .k12_live_validation import FinalCellAuthority
        except ImportError:  # pragma: no cover - import guard
            FinalCellAuthority = ()  # type: ignore[assignment]
        if not isinstance(authority, FinalCellAuthority):
            raise ContainmentError("typed final-cell authority is required")
        if expected_origin not in {RUNTIME_VERIFIED_ORIGIN, INJECTED_FAKE_ORIGIN}:
            raise ContainmentError("final-cell evidence origin is invalid")
        binding = authority.binding
        if (
            authority.admission.authority_binding != binding
            or authority.campaign_id != self.campaign
            or authority.evidence_origin != expected_origin
            or binding.origin != expected_origin
            or (expected_origin == RUNTIME_VERIFIED_ORIGIN and not authority.runtime_admissible)
            or (expected_origin == INJECTED_FAKE_ORIGIN and authority.runtime_admissible)
            or not authority_binding_is_current(
                authority, binding, profile_digest=self.profile.profile_digest,
                namespace="live_final",
                allow_injected=expected_origin == INJECTED_FAKE_ORIGIN,
            )
        ):
            raise ContainmentError("final-cell authority is stale or not live-admissible")
        if self.authority_binding != binding:
            raise ContainmentError("launch/final-cell authority binding mismatch")
        try:
            return self._reserve_bound(state, launch, binding.namespace, binding)
        except ValueError as exc:
            raise ContainmentError("final state is not launch-admissible") from exc

    def block(self) -> None:
        with self._lock:
            self._blocked = True

    def consume(self,key:LaunchKey,callback) -> None:
        with self._parent_terminalization_guard():
            with self._lock:
                self._assert_current()
                if self._blocked: raise ContainmentError("campaign launch authority is blocked")
                if key not in self._reserved or key in self._consumed: raise ContainmentError("launch reservation is absent or consumed")
                self._consumed.add(key); callback()


class LiveRunner:
    def __init__(self, *, executor: Any,
                  parent: ParentLaunchAuthority,
                  mode: str = RUNTIME_DISPATCH_MODE,
                  origin: str | None = None,
                  injected_fake: bool = False,
                  external_entry_fence: ExternalEntryFence | None = None,
                  fake_transport: InjectedFakeTransport | None = None) -> None:
        self.executor = executor
        if not isinstance(parent,ParentLaunchAuthority): raise ContainmentError("campaign-owned launch authority required")
        self.parent = parent
        self.dispatch_mode, self.final_origin = _normalize_dispatch_mode(
            mode, origin=origin, injected_fake=injected_fake,
        )
        if self.dispatch_mode == RUNTIME_DISPATCH_MODE:
            raise ContainmentError("runtime dispatch is unavailable in this package")
        else:
            if type(executor) is not MockContainmentIO:
                raise ContainmentError("concrete MockContainmentIO is required for injected mode")
        if external_entry_fence is None and fake_transport is not None:
            external_entry_fence = getattr(fake_transport, "fence", None)
        selected_fence = external_entry_fence or ExternalEntryFence(
            self.dispatch_mode, origin=self.final_origin,
        )
        if not isinstance(selected_fence, ExternalEntryFence):
            raise ContainmentError("typed external-entry fence is required")
        self.external_entry_fence = selected_fence
        if self.external_entry_fence.mode != self.dispatch_mode:
            raise ContainmentError("dispatch mode/external-entry fence mismatch")
        if self.external_entry_fence.origin != self.final_origin:
            raise ContainmentError("dispatch origin/external-entry fence mismatch")
        if self.dispatch_mode == INJECTED_FAKE_DISPATCH_MODE:
            if fake_transport is None:
                fake_transport = InjectedFakeTransport(self.external_entry_fence)
            if not isinstance(fake_transport, InjectedFakeTransport) \
                    or fake_transport.fence is not self.external_entry_fence:
                raise ContainmentError("injected fake transport/fence mismatch")
        elif fake_transport is not None:
            raise ContainmentError("fake transport is restricted to injected mode")
        self.fake_transport = fake_transport
        self._campaign_blocked = False
        self._closed = False
        self._stopping = False
        self._prepared: dict[LaunchKey, LiveLaunch] = {}
        self._prepared_final: dict[LaunchKey, tuple[Any, Any, Any]] = {}
        self._launched_final: dict[LaunchKey, tuple[Any, Any, Any, LiveLaunch]] = {}
        self._completed_final: set[LaunchKey] = set()
        self._lock=threading.RLock()

    @property
    def mode(self) -> str:
        return self.dispatch_mode

    @property
    def origin(self) -> str:
        return self.final_origin

    @property
    def closed(self) -> bool:
        return self._closed

    def _ensure_open(self) -> None:
        if self._closed or self._campaign_blocked or self._stopping:
            raise ContainmentError("launch is blocked or runner is terminally closed")

    def _record_launch(
        self,
        command: tuple[str, ...],
        *,
        final: bool = False,
        cell_authority: Any = None,
        launch_id: str = "",
    ) -> Any:
        if self.dispatch_mode == INJECTED_FAKE_DISPATCH_MODE:
            if self.fake_transport is None:
                raise ContainmentError("injected mode requires a typed fake dispatch")
            return self.fake_transport.dispatch(
                command,
                cell_id=getattr(cell_authority, "cell_id", ""),
                launch_id=launch_id,
                authority_binding=(
                    getattr(cell_authority, "binding", None) if final else None
                ),
            )
        raise ContainmentError("runtime dispatch is unavailable in this package")

    def prepare(self, state: NormalizedLiveState, launch_id: str, argv: Sequence[str], *,
                namespace: str) -> LiveLaunch:
        with self._lock: return self._prepare(state,launch_id,argv,namespace)
    def _prepare(self,state:NormalizedLiveState,launch_id:str,argv:Sequence[str],namespace:str)->LiveLaunch:
        self._ensure_open()
        if namespace not in {"qualification", "probe", "final"}:
            raise ContainmentError("launch is blocked or namespace is invalid")
        if not isinstance(state,NormalizedLiveState) or not isinstance(launch_id,str) or not launch_id:
            raise ValueError("complete launch reservation identity is required")
        # Namespace is part of the containment identity as well as the ledger key.
        unit, cgroup = containment_identity(f"{namespace}:{state.cell}", launch_id)
        key=self.parent.reserve(state,launch_id,namespace)
        prepared = LiveLaunch(unit, cgroup, systemd_run_command(unit, argv), False, state.authority_binding)
        self._prepared[key] = prepared
        return prepared

    def launch(self, state: NormalizedLiveState, launch_id: str, argv: Sequence[str], *, namespace:str) -> LiveLaunch:
        with self._lock:
            self._ensure_open()
            key=LaunchKey(state.profile,self.parent.campaign,self.parent.cohort,state.cell,
                          state.generation,state.reset_token,state.reset_attestation_sha256,
                          launch_id,namespace,state.authority_binding)
            prepared = self._prepared.get(key)
            if prepared is None: prepared = self._prepare(state,launch_id,argv,namespace)
            expected=LiveLaunch(prepared.unit_id,prepared.cgroup_id,systemd_run_command(prepared.unit_id,argv),False,state.authority_binding)
            if prepared != expected: raise ContainmentError("launch arguments differ from reserved command")
            self._prepared.pop(key,None)
            try:
                self.parent.consume(
                    key,
                    lambda: self._record_launch(prepared.command, launch_id=launch_id),
                )
            except BaseException:
                self.block()
                raise
            return LiveLaunch(prepared.unit_id, prepared.cgroup_id, prepared.command, True, prepared.authority_binding)

    @staticmethod
    def _final_types() -> tuple[type, type]:
        from .k12_live_validation import FinalCellAuthority, FinalCellEvidence
        return FinalCellAuthority, FinalCellEvidence

    @staticmethod
    def _require_injected_admission_graph(admission: Any) -> None:
        """Require the parent-minted injected-test authority graph."""

        active = getattr(admission, "authority", None)
        if (
            not isinstance(active, ActiveFinalAuthority)
            or active.origin != INJECTED_FAKE_ORIGIN
            or active.binding.origin != INJECTED_FAKE_ORIGIN
            or active.runtime_admissible
            or getattr(admission, "evidence_origin", None) != INJECTED_FAKE_ORIGIN
            or admission.runtime_admissible
            or not authority_binding_is_current(
                active,
                active.binding,
                profile_digest=active.profile_digest,
                namespace="live_final",
                allow_injected=True,
            )
        ):
            raise ContainmentError("injected final authority is not active or parent-owned")

    @staticmethod
    def _require_injected_final_graph(cell_authority: Any) -> None:
        if not isinstance(cell_authority, LiveRunner._final_types()[0]):
            raise ContainmentError("typed final-cell authority is required")
        LiveRunner._require_injected_admission_graph(cell_authority.admission)

    @staticmethod
    def _require_final_evidence(
        evidence: Any,
        cell_authority: Any,
        *,
        expected_origin: str = RUNTIME_VERIFIED_ORIGIN,
    ) -> None:
        FinalCellAuthority, FinalCellEvidence = LiveRunner._final_types()
        if not isinstance(cell_authority, FinalCellAuthority):
            raise ContainmentError("typed final-cell authority is required")
        if not isinstance(evidence, FinalCellEvidence):
            raise ContainmentError("typed final-cell evidence is required")
        if expected_origin not in {RUNTIME_VERIFIED_ORIGIN, INJECTED_FAKE_ORIGIN}:
            raise ContainmentError("final-cell evidence origin is invalid")
        if expected_origin == INJECTED_FAKE_ORIGIN:
            LiveRunner._require_injected_final_graph(cell_authority)
        if (
            evidence.evidence_origin != expected_origin
            or cell_authority.evidence_origin != expected_origin
            or evidence.execution_provenance != "live_final"
            or (
                expected_origin == RUNTIME_VERIFIED_ORIGIN
                and not cell_authority.runtime_admissible
            )
            or (
                expected_origin == INJECTED_FAKE_ORIGIN
                and cell_authority.runtime_admissible
            )
            or evidence.authority_binding != cell_authority.binding
            or evidence.cell_id != cell_authority.cell_id
            or evidence.profile_digest != cell_authority.profile_digest
            or evidence.campaign_id != cell_authority.campaign_id
            or evidence.phase != cell_authority.phase
            or evidence.manifest_identity != cell_authority.manifest.manifest_identity
            or evidence.manifest_digest != cell_authority.manifest.manifest_digest
            or evidence.common_closure_digest != cell_authority.common_closure_digest
            or evidence.terminal_verified is not True
            or evidence.result != "passed"
            or evidence.fresh_root is not True
            or evidence.retry
            or evidence.resumed
            or evidence.replacement
        ):
            raise ContainmentError("mock, mixed, or wrong-origin final-cell evidence is denied")

    @staticmethod
    def _require_live_final_evidence(evidence: Any, cell_authority: Any) -> None:
        """Compatibility spelling for the runtime-verified final path."""

        LiveRunner._require_final_evidence(
            evidence, cell_authority, expected_origin=RUNTIME_VERIFIED_ORIGIN,
        )

    @staticmethod
    def _require_final_lifecycle(
        cell_authority: Any,
        lease: Any,
        ledger: Any,
        *,
        profile_digest: str,
        expected_origin: str = RUNTIME_VERIFIED_ORIGIN,
        check_current: bool = True,
    ) -> None:
        FinalCellAuthority, _ = LiveRunner._final_types()
        from .k12_execution_provenance import K12RetainedTargetLease
        if not isinstance(cell_authority, FinalCellAuthority):
            raise ContainmentError("typed final-cell authority is required")
        LiveRunner._require_target_lease(cell_authority, lease)
        LiveRunner._require_sealed_source(cell_authority)
        if expected_origin == INJECTED_FAKE_ORIGIN:
            LiveRunner._require_injected_final_graph(cell_authority)
        if expected_origin not in {RUNTIME_VERIFIED_ORIGIN, INJECTED_FAKE_ORIGIN}:
            raise ContainmentError("final-cell evidence origin is invalid")
        if (
            cell_authority.evidence_origin != expected_origin
            or (
                expected_origin == RUNTIME_VERIFIED_ORIGIN
                and not cell_authority.runtime_admissible
            )
            or (
                expected_origin == INJECTED_FAKE_ORIGIN
                and cell_authority.runtime_admissible
            )
        ):
            raise ContainmentError("final-cell admission has the wrong evidence origin")
        if not isinstance(lease, K12RetainedTargetLease):
            raise ContainmentError("typed retained target lease is required")
        if not isinstance(ledger, DurableLedger):
            raise ContainmentError("typed final durable ledger is required")
        try:
            ledger_snapshot = ledger.snapshot()
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ContainmentError("final ledger is not active") from exc
        if (
            ledger_snapshot.get("namespace") != "final"
            or ledger_snapshot.get("reservation_id") != cell_authority.authority.reservation_id
            or ledger_snapshot.get("state") != "active"
            or ledger_snapshot.get("head_digest") != cell_authority.binding.activation
        ):
            raise ContainmentError("final ledger is not active")
        try:
            lease.revalidate()
        except Exception as exc:
            raise ContainmentError("retained target lease is stale or lost") from exc
        if lease.reservation_id != cell_authority.authority.reservation_id:
            raise ContainmentError("retained target lease/final authority mismatch")
        binding = cell_authority.binding
        if (
            binding.authority_type != FINAL_AUTHORITY
            or binding.namespace != "live_final"
            or binding.provenance != "live_final"
            or binding.lifecycle != "active"
        ):
            raise ContainmentError("final-cell authority is stale or revoked")
        if check_current and not authority_binding_is_current(
            cell_authority, binding, profile_digest=profile_digest,
            namespace="live_final",
            allow_injected=expected_origin == INJECTED_FAKE_ORIGIN,
        ):
            raise ContainmentError("final-cell authority is stale or revoked")

    @staticmethod
    def _require_target_lease(authority: Any, lease: Any) -> None:
        """Bind the retained lease to the parent final authority receipt."""

        from .k12_execution_provenance import K12RetainedTargetLease

        if not isinstance(lease, K12RetainedTargetLease):
            raise ContainmentError("typed retained target lease is required")
        try:
            active_authority = getattr(authority, "authority", authority)
            owner = getattr(active_authority, "owner", None)
            execution_authority = getattr(active_authority, "authority", active_authority)
            receipt_method = getattr(execution_authority, "receipt", None)
            if owner is None or not callable(receipt_method):
                raise ContainmentError("final authority target lease receipt is unavailable")
            if not lease.owned_by(owner):
                raise ContainmentError("target lease owner mismatch")
            receipt = receipt_method()
            if not isinstance(receipt, Mapping):
                raise ContainmentError("final authority target lease receipt is invalid")
            bound_lease = receipt.get("target_lease")
            if not isinstance(bound_lease, Mapping) or lease.canonical() != bound_lease:
                raise ContainmentError("target lease does not match final authority receipt")
        except ContainmentError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError, ProvenanceError) as exc:
            raise ContainmentError("target lease ownership or receipt validation failed") from exc

    @staticmethod
    def _require_sealed_source(cell_authority: Any) -> None:
        """Ensure final dispatch has the immutable source snapshot from its authority."""
        active = getattr(cell_authority, "authority", None)
        execution = getattr(active, "authority", None)
        sealed = getattr(active, "sealed_source", None)
        body = getattr(execution, "body", None)
        closure = body.get("source_closure") if isinstance(body, Mapping) else None
        records = closure.get("records") if isinstance(closure, Mapping) else None
        if (
            not isinstance(sealed, Mapping)
            or not isinstance(closure, Mapping)
            or not isinstance(records, (list, tuple))
            or not sealed
            or any(not isinstance(record, Mapping) for record in records)
        ):
            raise ContainmentError("immutable final source snapshot is unavailable")
        paths = {record.get("path") for record in records}
        if set(sealed) != paths or any(not isinstance(path, str) for path in paths):
            raise ContainmentError("immutable final source snapshot paths are invalid")
        for record in records:
            blob = sealed.get(record.get("path"))
            if (
                type(blob) is not bytes
                or raw_sha256(blob) != record.get("sha256")
                or git_blob_oid(blob) != record.get("git_blob_oid")
            ):
                raise ContainmentError("immutable final source snapshot does not match authority")
        canonical_records = [dict(record) for record in records]
        if raw_sha256(canonical_bytes({
            "head_commit": closure.get("head_commit"),
            "head_tree": closure.get("head_tree"),
            "records": canonical_records,
        })) != closure.get("aggregate_sha256"):
            raise ContainmentError("immutable final source snapshot closure is invalid")

    @staticmethod
    def _target_lease_guard(lease: Any) -> Any:
        """Hold the retained target lease across an admission/dispatch boundary."""
        from .k12_execution_provenance import K12RetainedTargetLease

        if not isinstance(lease, K12RetainedTargetLease):
            raise ContainmentError("typed retained target lease is required")
        guard = getattr(lease, "commit_guard", None)
        if not callable(guard):
            raise ContainmentError("retained target lease guard is unavailable")
        try:
            return guard()
        except (AttributeError, TypeError, ValueError, ProvenanceError) as exc:
            raise ContainmentError("retained target lease guard is unavailable") from exc

    @staticmethod
    def _failure_reason(exc: BaseException) -> str:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, str) and reason.strip():
            return reason.strip()
        return type(exc).__name__

    @staticmethod
    def _quarantined_target_is_intact(lock: Any) -> bool:
        """Require durable quarantine and retained FD/path identity."""
        try:
            snapshot = lock.retained_lease_snapshot()
            return bool(
                snapshot.quarantined
                and snapshot.metadata.get("status") == "quarantined"
                and (snapshot.fd_dev, snapshot.fd_ino)
                    == (snapshot.path_dev, snapshot.path_ino)
            )
        except Exception:
            return False

    @staticmethod
    def _bound_final_lease(cell_authority: Any, fallback: Any = None) -> Any:
        """Resolve the exact lease recorded on the parent final authority."""
        try:
            active = getattr(cell_authority, "authority", cell_authority)
            execution = getattr(active, "authority", active)
            owner = getattr(active, "owner", None)
            resolver = getattr(owner, "_require_authority_target_lease", None)
            if callable(resolver):
                bound = resolver(execution)
                if bound is not None:
                    return bound
        except Exception:
            pass
        return fallback

    @staticmethod
    def _quarantine_final_failure(
        cell_authority: Any,
        lease: Any,
        reason: str,
    ) -> bool:
        """Quarantine every failed final boundary and report completeness.

        Final dispatch is an injected boundary in this package, but its
        failure semantics are still durable: the parent-owned final ledger is
        revoked and the retained target is quarantined before another owner
        can reuse either capability.  Cleanup errors are recorded as an
        incomplete result so callers cannot mistake local blocking for durable
        quarantine.
        """
        FinalCellAuthority, _ = LiveRunner._final_types()
        if not isinstance(cell_authority, FinalCellAuthority):
            return False
        active = getattr(cell_authority, "authority", None)
        execution = getattr(active, "authority", None)
        owner = getattr(active, "owner", None)
        ledger = getattr(execution, "ledger", None)
        if owner is None or ledger is None:
            return False

        authority_identity = getattr(execution, "identity", "unknown")
        if not isinstance(authority_identity, str):
            authority_identity = "unknown"
        normalized_reason = reason.strip() if isinstance(reason, str) else "final_boundary_failure"
        if not normalized_reason:
            normalized_reason = "final_boundary_failure"
        run_name = f"k12-final-{authority_identity[:24]}"
        diagnostics = {
            "authority": authority_identity,
            "campaign": getattr(cell_authority, "campaign_id", ""),
            "reason": normalized_reason,
        }
        ledger_payload = {
            "reason": normalized_reason,
            "authority": authority_identity,
            "phase": "final_dispatch_failure",
        }

        parent_guard_factory = getattr(owner, "_terminalization_guard", None)
        try:
            parent_guard = (
                parent_guard_factory() if callable(parent_guard_factory) else nullcontext()
            )
        except Exception:
            parent_guard = nullcontext()

        target_lock = None
        target_guard = nullcontext()
        target_required = True
        try:
            # Cleanup follows the parent authority registry, not the lease
            # supplied by the failed caller.  A stale/mismatched argument must
            # never make the actual retained target escape quarantine.
            lease = LiveRunner._bound_final_lease(cell_authority)
            if lease is None:
                raise ContainmentError("final authority target lease is unavailable")
            target_lock = getattr(lease, "lock", None)
            target_guard = LiveRunner._target_lease_guard(lease)
        except Exception:
            target_lock = None
            target_guard = nullcontext()

        ledger_quarantined = False
        target_quarantined = False
        cleanup_failed = False
        try:
            with parent_guard:
                with target_guard:
                    if getattr(ledger, "namespace", None) == "final":
                        try:
                            if getattr(ledger, "state", None) == "quarantined":
                                ledger_quarantined = True
                            else:
                                owner.quarantine_ledger(ledger, ledger_payload)
                                ledger_quarantined = (
                                    getattr(ledger, "state", None) == "quarantined"
                                )
                        except Exception:
                            cleanup_failed = True
                    if target_lock is not None:
                        if getattr(target_lock, "quarantined", False):
                            target_quarantined = LiveRunner._quarantined_target_is_intact(
                                target_lock
                            )
                            if not target_quarantined:
                                cleanup_failed = True
                        elif not getattr(target_lock, "acquired", False):
                            cleanup_failed = True
                        else:
                            try:
                                lease.revalidate()
                                target_lock.quarantine(
                                    run_name=run_name,
                                    reasons=(normalized_reason,),
                                    diagnostics=diagnostics,
                                )
                                target_quarantined = LiveRunner._quarantined_target_is_intact(
                                    target_lock
                                )
                                if not target_quarantined:
                                    cleanup_failed = True
                            except Exception:
                                cleanup_failed = True
        except Exception:
            # Quarantine must never mask the original boundary failure, but a
            # failed guard/lock transition is still an incomplete cleanup.
            cleanup_failed = True
        return ledger_quarantined and (
            target_quarantined if target_required else True
        ) and not cleanup_failed

    @staticmethod
    @contextmanager
    def _final_boundary_guard(cell_authority: Any, lease: Any):
        """Acquire parent lifecycle before target lease for a fixed lock order."""
        if not isinstance(cell_authority, LiveRunner._final_types()[0]):
            raise ContainmentError("typed final-cell authority is required")
        active = getattr(cell_authority, "authority", cell_authority)
        owner = getattr(active, "owner", None)
        guard_lease = LiveRunner._bound_final_lease(cell_authority, lease)
        parent_guard_factory = getattr(owner, "_terminalization_guard", None)
        parent_guard = (
            parent_guard_factory() if callable(parent_guard_factory) else nullcontext()
        )
        with parent_guard:
            try:
                with LiveRunner._target_lease_guard(guard_lease):
                    yield
            except BaseException as exc:
                cleanup_complete = LiveRunner._quarantine_final_failure(
                    cell_authority,
                    lease,
                    LiveRunner._failure_reason(exc),
                )
                if not cleanup_complete:
                    raise ContainmentError(
                        "final failure cleanup incomplete"
                    ) from exc
                raise

    @contextmanager
    def _final_operation(self, cell_authority: Any, lease: Any):
        """Block this in-memory runner after a failed final boundary."""
        try:
            with self._final_boundary_guard(cell_authority, lease):
                yield
        except BaseException as exc:
            # The boundary guard has already attempted durable cleanup.  Keep
            # retained launch records when that cleanup was incomplete so a
            # parent can diagnose/recover the exact outstanding resources.
            self.block(
                clear_boundaries=not (
                    isinstance(exc, ContainmentError)
                    and str(exc) == "final failure cleanup incomplete"
                )
            )
            raise

    @staticmethod
    def _consume_final_cell_for_launch(cell_authority: Any) -> Any:
        """Consume the public prelaunch cell capability, never terminal evidence."""

        consume_for_launch = getattr(cell_authority, "consume_for_launch", None)
        if not callable(consume_for_launch):
            raise ContainmentError(
                "FinalCellAuthority.consume_for_launch() dependency is unavailable"
            )
        return consume_for_launch()

    @staticmethod
    def _complete_final_cell_authority(cell_authority: Any, evidence: Any) -> Any:
        """Complete a dispatched cell through the public terminal boundary."""

        complete = getattr(cell_authority, "complete", None)
        if not callable(complete):
            raise ContainmentError(
                "FinalCellAuthority.complete() dependency is unavailable"
            )
        return complete(evidence)

    def prepare_final_cell(
        self,
        cell_authority: Any,
        state: NormalizedLiveState,
        launch_id: str,
        argv: Sequence[str],
        *,
        lease: Any,
        ledger: Any,
    ) -> LiveLaunch:
        """Prepare one final cell without accepting a caller namespace.

        Preparation reserves only data.  The parent launch reservation and the
        final-cell admission are consumed at the dispatch boundary.  Terminal
        ``FinalCellEvidence`` is accepted only after dispatch through
        :meth:`complete_final_cell`.
        """

        with self._lock, self._final_operation(cell_authority, lease):
            self._ensure_open()
            FinalCellAuthority, _ = self._final_types()
            if not isinstance(state, NormalizedLiveState):
                raise ContainmentError("typed normalized final state is required")
            try:
                validate_state(state)
            except (TypeError, ValueError) as exc:
                raise ContainmentError("typed normalized final state is required") from exc
            if not isinstance(cell_authority, FinalCellAuthority):
                raise ContainmentError("typed final-cell authority is required")
            if self.dispatch_mode not in {
                RUNTIME_DISPATCH_MODE, INJECTED_FAKE_DISPATCH_MODE,
            }:
                raise ContainmentError("unknown final dispatch mode")
            self._require_final_lifecycle(
                cell_authority,
                lease,
                ledger,
                profile_digest=state.profile,
                expected_origin=self.final_origin,
            )
            if (
                state.cell != cell_authority.cell_id
                or state.profile != self.parent.profile.profile_digest
                or state.authority_binding != cell_authority.binding
            ):
                raise ContainmentError("final state/cell authority binding mismatch")
            if not isinstance(launch_id, str) or not launch_id:
                raise ValueError("complete launch reservation identity is required")
            unit, cgroup = containment_identity(
                f"{cell_authority.binding.namespace}:{state.cell}", launch_id,
            )
            key = LaunchKey(
                state.profile,
                self.parent.campaign,
                self.parent.cohort,
                state.cell,
                state.generation,
                state.reset_token,
                state.reset_attestation_sha256,
                launch_id,
                cell_authority.binding.namespace,
                cell_authority.binding,
            )
            if key in self._prepared or key in self._launched_final:
                raise ContainmentError("final-cell launch reservation already prepared or consumed")
            prepared = LiveLaunch(
                unit, cgroup, systemd_run_command(unit, argv), False,
                cell_authority.binding,
            )
            self._prepared[key] = prepared
            self._prepared_final[key] = (cell_authority, lease, ledger)
            return prepared

    prepare_admitted_cell = prepare_final_cell
    prepare_final = prepare_final_cell

    def launch_final_cell(
        self,
        cell_authority: Any,
        state: NormalizedLiveState,
        launch_id: str,
        argv: Sequence[str],
        *,
        lease: Any,
        ledger: Any,
    ) -> LiveLaunch:
        """Dispatch one prepared final cell exactly once.

        The parent launch reservation and public prelaunch cell capability are
        consumed immediately before the injected fake dispatch.  Terminal
        cell evidence belongs only to the post-dispatch completion boundary
        exposed by :meth:`complete_final_cell`.
        """

        with self._lock, self._final_operation(cell_authority, lease):
            self._ensure_open()
            if not isinstance(state, NormalizedLiveState):
                raise ContainmentError("typed normalized final state is required")
            try:
                validate_state(state)
            except (TypeError, ValueError) as exc:
                raise ContainmentError("typed normalized final state is required") from exc
            self._require_final_lifecycle(
                cell_authority,
                lease,
                ledger,
                profile_digest=state.profile,
                expected_origin=self.final_origin,
            )
            binding = cell_authority.binding
            key = LaunchKey(
                state.profile, self.parent.campaign, self.parent.cohort, state.cell,
                state.generation, state.reset_token, state.reset_attestation_sha256,
                launch_id, binding.namespace, binding,
            )
            prepared = self._prepared.get(key)
            if prepared is None:
                raise ContainmentError("final-cell launch was not prepared")
            selected = self._prepared_final.get(key)
            if selected is None:
                raise ContainmentError("final-cell admission preparation is absent")
            selected_authority, selected_lease, selected_ledger = selected
            if selected_authority is not cell_authority or selected_lease is not lease \
                    or selected_ledger is not ledger:
                raise ContainmentError("final-cell admission identity changed")
            expected=LiveLaunch(
                prepared.unit_id, prepared.cgroup_id,
                systemd_run_command(prepared.unit_id, argv), False, binding,
            )
            if prepared != expected:
                raise ContainmentError("launch arguments differ from reserved command")
            # Reserve the parent-owned final cell only at the dispatch
            # boundary.  This is the prelaunch admission consume point.
            reserved = self.parent.reserve_final(
                cell_authority,
                state,
                launch_id,
                expected_origin=self.final_origin,
            )
            if reserved != key:
                raise ContainmentError("final-cell launch reservation identity changed")

            def record_dispatch() -> None:
                self._require_final_lifecycle(
                    cell_authority,
                    lease,
                    ledger,
                    profile_digest=state.profile,
                    expected_origin=self.final_origin,
                )
                self._consume_final_cell_for_launch(cell_authority)
                self._record_launch(
                    prepared.command,
                    final=True,
                    cell_authority=cell_authority,
                    launch_id=launch_id,
                )

            try:
                self.parent.consume(key, record_dispatch)
            except BaseException:
                raise
            self._prepared.pop(key, None)
            self._prepared_final.pop(key, None)
            launched = LiveLaunch(
                prepared.unit_id, prepared.cgroup_id, prepared.command, True, binding,
            )
            self._launched_final[key] = (
                cell_authority, lease, ledger, launched,
            )
            return launched

    launch_admitted_cell = launch_final_cell
    launch_final = launch_final_cell

    def complete_final_cell(
        self,
        cell_authority: Any,
        evidence: Any,
        *,
        lease: Any,
        ledger: Any,
        launch_id: str | None = None,
    ) -> Any:
        """Submit terminal evidence after the external dispatch returned."""

        with self._lock, self._final_operation(cell_authority, lease):
            self._ensure_open()
            if not isinstance(cell_authority, self._final_types()[0]):
                raise ContainmentError("typed final-cell authority is required")
            candidates = [
                (key, value)
                for key, value in self._launched_final.items()
                if value[0] is cell_authority
                and (launch_id is None or key.launch == launch_id)
            ]
            if len(candidates) != 1:
                raise ContainmentError("final-cell dispatch is not awaiting completion")
            key, selected = candidates[0]
            if key in self._completed_final:
                raise ContainmentError("final-cell completion is already terminal")
            _, selected_lease, selected_ledger, _ = selected
            if selected_lease is not lease or selected_ledger is not ledger:
                raise ContainmentError("final-cell completion identity changed")
            try:
                self._require_final_lifecycle(
                    cell_authority,
                    lease,
                    ledger,
                    profile_digest=cell_authority.profile_digest,
                    expected_origin=self.final_origin,
                    check_current=False,
                )
                self._require_final_evidence(
                    evidence,
                    cell_authority,
                    expected_origin=self.final_origin,
                )
                result = self._complete_final_cell_authority(cell_authority, evidence)
            except BaseException as exc:
                if isinstance(exc, ContainmentError):
                    raise
                raise ContainmentError("final-cell completion denied") from exc
            self._completed_final.add(key)
            return result

    complete_admitted_cell = complete_final_cell
    complete_final = complete_final_cell

    def consume_final_cell(
        self, cell_authority: Any, *, lease: Any, ledger: Any, evidence: Any,
    ) -> Any:
        """Compatibility alias for the post-dispatch completion boundary."""

        return self.complete_final_cell(
            cell_authority,
            evidence=evidence,
            lease=lease,
            ledger=ledger,
        )

    consume_admitted_cell = consume_final_cell

    def block(self, *, clear_boundaries: bool = True) -> None:
        with self._lock:
            self._campaign_blocked = True
            self._closed = True
            self._stopping = True
            self._prepared.clear()
            self._prepared_final.clear()
            if clear_boundaries:
                self._launched_final.clear()
            self.parent.block()

    def _final_boundaries_for_stop(self, authority: Any = None) -> tuple[tuple[Any, Any], ...]:
        """Recover every dispatched authority/lease requiring stop cleanup."""
        with self._lock:
            launched = tuple(self._launched_final.values())
            if authority is not None:
                matching = tuple(
                    (value[0], value[1]) for value in launched if value[0] is authority
                )
                if matching:
                    return matching
            return tuple((value[0], value[1]) for value in launched)

    def _cleanup_stop_failure(self, authority: Any, reason: str) -> bool:
        """Durably contain a stop failure for the stored dispatched boundary."""
        boundaries = self._final_boundaries_for_stop(authority)
        with self._lock:
            has_dispatched_boundary = bool(self._launched_final)
            multiple_boundaries = len(self._launched_final) > 1
        if multiple_boundaries:
            boundaries = self._final_boundaries_for_stop(None)
        if not boundaries:
            return not has_dispatched_boundary
        complete = True
        for stored_authority, lease in boundaries:
            if not self._quarantine_final_failure(stored_authority, lease, reason):
                complete = False
        return complete

    def _raise_stop_failure(
            self, authority: Any, error: BaseException, reason: str,
    ) -> None:
        cleanup_complete = self._cleanup_stop_failure(authority, reason)
        if not cleanup_complete:
            self.block(clear_boundaries=False)
            raise ContainmentError("final failure cleanup incomplete") from error
        self.block()
        raise error

    def _begin_stop(self) -> None:
        with self._lock:
            self._ensure_open()
            self._stopping = True

    def _stop_started(self, controller: LiveContainment, selected_authority: Any) -> object:
        if not isinstance(controller, LiveContainment) or controller.io is not self.executor:
            self._raise_stop_failure(
                selected_authority,
                ContainmentError("runner/controller authority mismatch"),
                "runner/controller authority mismatch",
            )
        if selected_authority is not None:
            if not isinstance(selected_authority, self._final_types()[0]):
                self._raise_stop_failure(
                    selected_authority,
                    ContainmentError("typed final-cell authority is required"),
                    "typed final-cell authority is required",
                )
        with self._lock:
            if len(self._launched_final) > 1:
                self._raise_stop_failure(
                    selected_authority,
                    ContainmentError(
                        "multiple final-cell dispatches require coordinated stop"
                    ),
                    "multiple final-cell dispatches require coordinated stop",
                )
        try:
            result = controller.stop()
        except BaseException as exc:
            self._raise_stop_failure(
                selected_authority, exc, self._failure_reason(exc),
            )
        self.block()
        return result

    def stop(self, controller: LiveContainment, cell_authority: Any = None, *,
             authority: Any = None) -> object:
        selected_authority = cell_authority if cell_authority is not None else authority
        self._begin_stop()
        return self._stop_started(controller, selected_authority)


class FinalCellAdmissionRunner(LiveRunner):
    """Strict runner facade for the typed final campaign admission.

    The legacy :class:`LiveRunner` remains available for explicit mock
    qualification/probe tests.  This facade removes the caller namespace from
    the final API and retains the typed admission, lease, and final ledger for
    every prepare/launch operation.  Its default is the fail-closed
    ``runtime_verified`` mode; ``mode="injected_fake"`` is the only explicit,
    fake-transport-only dispatch path.
    """

    def __init__(
        self, *, executor: Any, parent: ParentLaunchAuthority,
        admission: Any,
        lease: Any,
        ledger: Any,
        mode: str = RUNTIME_DISPATCH_MODE,
        origin: str | None = None,
        injected_fake: bool = False,
        external_entry_fence: ExternalEntryFence | None = None,
        fake_transport: InjectedFakeTransport | None = None,
    ) -> None:
        from .k12_live_validation import FinalCampaignAdmission
        from .k12_execution_provenance import K12RetainedTargetLease
        if not isinstance(admission, FinalCampaignAdmission):
            raise ContainmentError("typed final campaign admission is required")
        dispatch_mode, expected_origin = _normalize_dispatch_mode(
            mode, origin=origin, injected_fake=injected_fake,
        )
        if (
            admission.evidence_origin != expected_origin
            or admission.authority_binding.origin != expected_origin
        ):
            raise ContainmentError("final campaign admission origin does not match dispatch mode")
        if expected_origin == RUNTIME_VERIFIED_ORIGIN:
            if not admission.runtime_admissible:
                raise ContainmentError("mock or stale final campaign admission is denied")
        else:
            if admission.runtime_admissible:
                raise ContainmentError("runtime final admission cannot use injected dispatch mode")
            LiveRunner._require_injected_admission_graph(admission)
        if not isinstance(lease, K12RetainedTargetLease):
            raise ContainmentError("typed retained target lease is required")
        LiveRunner._require_target_lease(admission, lease)
        if not isinstance(ledger, DurableLedger):
            raise ContainmentError("typed final durable ledger is required")
        try:
            ledger_snapshot = ledger.snapshot()
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ContainmentError("final ledger or retained target lease is not active") from exc
        if (
            ledger_snapshot.get("namespace") != "final"
            or ledger_snapshot.get("reservation_id") != admission.authority.reservation_id
            or ledger_snapshot.get("state") != "active"
            or ledger_snapshot.get("head_digest") != admission.authority_binding.activation
            or lease.reservation_id != admission.authority.reservation_id
        ):
            raise ContainmentError("final ledger or retained target lease is not active")
        try:
            lease.revalidate()
        except Exception as exc:
            raise ContainmentError("retained target lease is stale or lost") from exc
        if not isinstance(parent, ParentLaunchAuthority):
            raise ContainmentError("campaign-owned launch authority is required")
        if parent.authority_binding != admission.authority_binding:
            raise ContainmentError("launch/admission authority binding mismatch")
        super().__init__(
            executor=executor,
            parent=parent,
            mode=dispatch_mode,
            origin=expected_origin,
            external_entry_fence=external_entry_fence,
            fake_transport=fake_transport,
        )
        self.admission = admission
        self.lease = lease
        self.ledger = ledger

    @property
    def final_admission(self) -> Any:
        return self.admission

    @property
    def fence(self) -> ExternalEntryFence:
        return self.external_entry_fence

    @property
    def dispatch_recorder(self) -> InjectedFakeTransport | None:
        return self.fake_transport

    def _check_cell(self, authority: Any) -> None:
        from .k12_live_validation import FinalCellAuthority
        if not isinstance(authority, FinalCellAuthority) or authority.admission is not self.admission:
            raise ContainmentError("final-cell authority belongs to another admission")
        if authority.evidence_origin != self.final_origin:
            raise ContainmentError("final-cell authority origin does not match dispatch mode")
        self._require_target_lease(authority, self.lease)
        if self.final_origin == INJECTED_FAKE_ORIGIN:
            self._require_injected_final_graph(authority)

    def prepare(
        self, authority: Any, state: NormalizedLiveState, launch_id: str,
        argv: Sequence[str],
    ) -> LiveLaunch:
        self._check_cell(authority)
        return self.prepare_final_cell(
            authority, state, launch_id, argv,
            lease=self.lease, ledger=self.ledger,
        )

    def launch(
        self, authority: Any, state: NormalizedLiveState, launch_id: str,
        argv: Sequence[str],
    ) -> LiveLaunch:
        self._check_cell(authority)
        return self.launch_final_cell(
            authority, state, launch_id, argv,
            lease=self.lease, ledger=self.ledger,
        )

    def complete(
        self,
        authority: Any,
        evidence: Any,
        *,
        launch_id: str | None = None,
    ) -> Any:
        """Complete a dispatched cell with terminal evidence."""

        self._check_cell(authority)
        return self.complete_final_cell(
            authority,
            evidence=evidence,
            lease=self.lease,
            ledger=self.ledger,
            launch_id=launch_id,
        )

    complete_cell = complete

    def stop(self, controller: LiveContainment, cell_authority: Any = None, *,
             authority: Any = None) -> object:
        self._begin_stop()
        selected = cell_authority if cell_authority is not None else authority
        if selected is None:
            self._raise_stop_failure(
                selected,
                ContainmentError("exact final-cell authority is required for stop"),
                "exact final-cell authority is required for stop",
            )
        try:
            self._check_cell(selected)
        except BaseException as exc:
            self._raise_stop_failure(
                selected, exc, self._failure_reason(exc),
            )
        with self._lock:
            matches = [
                launch
                for value in self._launched_final.values()
                if value[0] is selected
                for launch in (value[3],)
            ]
        if not matches:
            # A prepared launch has a containment identity, but stopping
            # before dispatch is not a valid final-cell observation.
            self._raise_stop_failure(
                selected,
                ContainmentError("final-cell dispatch is not active"),
                "final-cell dispatch is not active",
            )
        with self._lock:
            launch = matches[0]
            if (
                getattr(controller, "unit", None) != launch.unit_id
                or getattr(controller, "cgroup", None) != launch.cgroup_id
            ):
                error = ContainmentError("final-cell containment authority mismatch")
            else:
                error = None
        if error is not None:
            self._raise_stop_failure(selected, error, self._failure_reason(error))
        return self._stop_started(controller, selected)


FinalCellRunner = FinalCellAdmissionRunner
TypedFinalCellRunner = FinalCellAdmissionRunner


__all__ = [
    "AuthorityBinding", "EXTERNAL_ENTRY_CHANNELS", "ExternalEntryFence",
    "FakeDispatchRecord", "FakeDispatchRecorder", "FakeTransport",
    "FinalCellAdmissionRunner", "FinalCellRunner", "FinalFakeTransport",
    "INJECTED_FAKE_DISPATCH_MODE", "InjectedFakeRecorder", "InjectedFakeTransport",
    "LaunchKey", "LiveLaunch", "LiveRunner",
    "ParentLaunchAuthority", "RUNTIME_DISPATCH_MODE",
    "TypedFinalCellRunner",
]
