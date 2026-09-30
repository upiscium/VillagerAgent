import itertools
import threading
from types import SimpleNamespace

import pytest

from benchmarks.minecraft.k12_guarded_backend import K12AuthenticatedProfile
from benchmarks.minecraft.k12_live_containment import Descendant, LiveContainment, MockContainmentIO
from benchmarks.minecraft.k12_live_runner import (
    EXTERNAL_ENTRY_CHANNELS,
    ExternalEntryFence,
    InjectedFakeTransport,
    LiveRunner,
    ParentLaunchAuthority,
)
from benchmarks.minecraft.k12_live_state import MockTransport, ParentPlanAuthority, data_pos, execute_plan, normalize_state
from benchmarks.minecraft.k12_runtime_profile import load_k12_live_runtime_profile


_IDS=itertools.count()


def authority_and_state(cell="cell"):
    loaded=load_k12_live_runtime_profile(); profile=K12AuthenticatedProfile.from_runtime_profile(loaded)
    suffix=str(next(_IDS)); campaign="campaign"+suffix; cohort="cohort"+suffix
    command=data_pos("agent")
    plan_authority=ParentPlanAuthority(profile,campaign)
    plan=plan_authority.mint((command,),cell=cell,
        reset_token="reset"+suffix,generation=1,descriptor="containment",purpose="launch")
    results=execute_plan(plan,MockTransport({command.text:"agent has the following entity data: [0.0d,64.0d,0.0d]"}))
    return ParentLaunchAuthority(profile,campaign,cohort,plan_authority),normalize_state(plan,results)


def empty_io(): return MockContainmentIO(("x=1",))


def fake_runner(io, parent):
    fence = ExternalEntryFence("injected_fake")
    transport = InjectedFakeTransport(fence)
    return LiveRunner(
        executor=io,
        parent=parent,
        mode="injected_fake",
        external_entry_fence=fence,
        fake_transport=transport,
    )


def test_injected_external_entry_fence_denies_all_real_channels_without_entries():
    fence = ExternalEntryFence("injected_fake")
    for channel in EXTERNAL_ENTRY_CHANNELS:
        with pytest.raises(RuntimeError, match="fenced"):
            fence.enter(channel)
    assert fence.real_counts == {channel: 0 for channel in EXTERNAL_ENTRY_CHANNELS}
    assert fence.external_counters == fence.real_counts
    assert fence.fake_dispatches == ()


def test_injected_fake_transport_records_one_ordered_dispatch_and_no_real_entries():
    fence = ExternalEntryFence("injected_fake")
    transport = InjectedFakeTransport(fence)
    command = ("fake-final-dispatch", "cell-1")
    record = transport.dispatch(command, cell_id="cell-1", launch_id="launch-1")
    assert transport.dispatches == [command]
    assert transport.records == [record]
    assert tuple(item.ordinal for item in fence.ordered_fake_dispatches) == (0,)
    assert fence.real_counts == {channel: 0 for channel in EXTERNAL_ENTRY_CHANNELS}
    assert fence.fake_counts == {channel: 1 for channel in EXTERNAL_ENTRY_CHANNELS}


def test_runner_requires_campaign_authority():
    with pytest.raises(RuntimeError): LiveRunner(executor=empty_io(),parent=None)  # type: ignore[arg-type]


def test_runtime_mode_is_unavailable_before_any_dispatch():
    parent, _ = authority_and_state()
    with pytest.raises(RuntimeError, match="unavailable"):
        LiveRunner(executor=empty_io(), parent=parent, mode="runtime")


def test_prepare_never_executes_and_launch_only_records_data():
    parent,state=authority_and_state(); io=empty_io(); runner=fake_runner(io,parent)
    prepared=runner.prepare(state,"launch",("server",),namespace="qualification")
    assert not prepared.executed and runner.fake_transport.dispatches==[]
    launched=runner.launch(state,"launch",("server",),namespace="qualification")
    assert launched.executed and len(runner.fake_transport.dispatches)==1

def test_final_prepare_rejects_untyped_external_entry():
    parent, state = authority_and_state()
    runner = fake_runner(empty_io(), parent)
    with pytest.raises(RuntimeError, match="typed final-cell authority"):
        runner.prepare_final_cell(
            object(), state, "launch", ("server",),
            lease=object(), ledger=object(),
        )


def test_blocked_launch_is_denied():
    parent,state=authority_and_state(); runner=fake_runner(empty_io(),parent); runner.block()
    with pytest.raises(RuntimeError,match="blocked"):
        runner.prepare(state,"launch",("server",),namespace="probe")


def test_block_invalidates_an_existing_preparation():
    parent,state=authority_and_state(); io=empty_io(); runner=fake_runner(io,parent)
    runner.prepare(state,"launch",("server",),namespace="probe"); runner.block()
    with pytest.raises(RuntimeError,match="blocked"): runner.launch(state,"launch",("server",),namespace="probe")
    assert runner.fake_transport.dispatches==[]


def test_shared_campaign_block_invalidates_other_runner_preparation():
    parent,state=authority_and_state(); io_a=empty_io(); first=fake_runner(io_a,parent)
    second=fake_runner(empty_io(),parent)
    first.prepare(state,"launch",("server",),namespace="probe"); second.block()
    with pytest.raises(RuntimeError,match="blocked"): first.launch(state,"launch",("server",),namespace="probe")
    assert first.fake_transport.dispatches==[]


def test_containment_unknown_atomically_blocks_campaign():
    parent,state=authority_and_state(); main=Descendant(1,1,"/mock/worker",2,1,"/cg")
    io=MockContainmentIO(("MainPID=1\nControlGroup=/cg\nActiveState=active\nSubState=running","MainPID=1"),
        waits=(True,),descendants=((main,),),cgroup_sources=(("/cg",(1,),1),))
    runner=fake_runner(io,parent)
    with pytest.raises(RuntimeError, match="typed final-cell authority"):
        LiveContainment(io,unit="u",cgroup="/cg",clock=lambda:0)
    runner.block()
    with pytest.raises(RuntimeError,match="blocked"):
        runner.prepare(state,"launch",("server",),namespace="probe")


def test_parent_reservation_replay_and_namespaces():
    parent,state=authority_and_state(); first=fake_runner(empty_io(),parent); second=fake_runner(empty_io(),parent)
    first.prepare(state,"launch",("server",),namespace="qualification")
    with pytest.raises(RuntimeError,match="reservation"): second.prepare(state,"launch",("server",),namespace="qualification")
    second.prepare(state,"launch",("server",),namespace="probe")


def test_same_cell_or_launch_replay_is_denied_before_recording():
    parent,state=authority_and_state(); io=empty_io(); runner=fake_runner(io,parent)
    runner.prepare(state,"launch",("server",),namespace="final")
    with pytest.raises(RuntimeError,match="replay"): runner.prepare(state,"other",("server",),namespace="final")
    assert runner.fake_transport.dispatches==[]


def test_launch_rejects_arguments_different_from_reservation():
    parent,state=authority_and_state(); io=empty_io(); runner=fake_runner(io,parent)
    runner.prepare(state,"launch",("server","a"),namespace="qualification")
    with pytest.raises(RuntimeError,match="arguments"):
        runner.launch(state,"launch",("server","b"),namespace="qualification")
    assert runner.fake_transport.dispatches==[]


def test_atomic_concurrent_reservation_has_exactly_one_winner():
    parent,state=authority_and_state(); outcomes=[]
    def reserve():
        try:
            fake_runner(empty_io(), parent).prepare(
                state, "launch", ("server",), namespace="qualification",
            )
            outcomes.append("won")
        except RuntimeError: outcomes.append("rejected")
    threads=(threading.Thread(target=reserve),threading.Thread(target=reserve))
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert sorted(outcomes)==["rejected","won"]


def test_atomic_concurrent_launch_consumes_preparation_once():
    parent,state=authority_and_state(); io=empty_io(); runner=fake_runner(io,parent)
    runner.prepare(state,"launch",("server",),namespace="qualification"); outcomes=[]
    def launch():
        try: runner.launch(state,"launch",("server",),namespace="qualification"); outcomes.append("won")
        except RuntimeError: outcomes.append("rejected")
    threads=(threading.Thread(target=launch),threading.Thread(target=launch))
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert sorted(outcomes)==["rejected","won"] and len(runner.fake_transport.dispatches)==1


def test_parent_terminalization_precedes_launch_authority_lock():
    """The consume/revoke pair must not invert the parent/authority lock order."""
    parent, state = authority_and_state()
    key = parent.reserve(state, "lock-order", "qualification")
    terminalization = threading.Lock()
    authority_lock_acquired = threading.Event()
    release_assertion = threading.Event()
    assertion_started = threading.Event()
    errors = []

    class _Owner:
        def _terminalization_guard(self):
            return terminalization

    class _SignalingLock:
        def __init__(self):
            self._lock = threading.Lock()

        def __enter__(self):
            self._lock.acquire()
            authority_lock_acquired.set()
            return self

        def __exit__(self, _type, _value, _traceback):
            self._lock.release()

    parent.authority = SimpleNamespace(owner=_Owner())
    parent._lock = _SignalingLock()

    def gated_assert_current():
        assertion_started.set()
        if not release_assertion.wait(timeout=2):
            raise AssertionError("launch authority current check was not released")

    parent._assert_current = gated_assert_current

    def consume():
        try:
            parent.consume(key, lambda: None)
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    consume_thread = threading.Thread(target=consume, daemon=True)

    def terminalize():
        with terminalization:
            consume_thread.start()
            # With the old order consume acquired the authority lock first;
            # the parent thread then deterministically exposes the cycle by
            # trying to block that same authority.
            if authority_lock_acquired.wait(timeout=0.5):
                parent.block()
        release_assertion.set()

    terminal_thread = threading.Thread(target=terminalize, daemon=True)
    terminal_thread.start()
    terminal_thread.join(timeout=3)
    consume_thread.join(timeout=3)

    assert not terminal_thread.is_alive()
    assert not consume_thread.is_alive()
    assert not errors
