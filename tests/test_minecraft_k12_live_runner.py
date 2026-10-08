import itertools
import threading

import pytest

from benchmarks.minecraft.k12_guarded_backend import K12AuthenticatedProfile
from benchmarks.minecraft.k12_live_containment import Descendant, LiveContainment, MockContainmentIO
from benchmarks.minecraft.k12_live_runner import LiveRunner, ParentLaunchAuthority
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


def test_runner_requires_campaign_authority():
    with pytest.raises(RuntimeError): LiveRunner(executor=empty_io(),parent=None)  # type: ignore[arg-type]


def test_prepare_never_executes_and_launch_only_records_data():
    parent,state=authority_and_state(); io=empty_io(); runner=LiveRunner(executor=io,parent=parent)
    prepared=runner.prepare(state,"launch",("server",),namespace="qualification")
    assert not prepared.executed and io.launches==[]
    launched=runner.launch(state,"launch",("server",),namespace="qualification")
    assert launched.executed and len(io.launches)==1


def test_blocked_launch_is_denied():
    parent,state=authority_and_state(); runner=LiveRunner(executor=empty_io(),parent=parent); runner.block()
    with pytest.raises(RuntimeError,match="blocked"):
        runner.prepare(state,"launch",("server",),namespace="probe")


def test_block_invalidates_an_existing_preparation():
    parent,state=authority_and_state(); io=empty_io(); runner=LiveRunner(executor=io,parent=parent)
    runner.prepare(state,"launch",("server",),namespace="probe"); runner.block()
    with pytest.raises(RuntimeError,match="blocked"): runner.launch(state,"launch",("server",),namespace="probe")
    assert io.launches==[]


def test_shared_campaign_block_invalidates_other_runner_preparation():
    parent,state=authority_and_state(); io_a=empty_io(); first=LiveRunner(executor=io_a,parent=parent)
    second=LiveRunner(executor=empty_io(),parent=parent)
    first.prepare(state,"launch",("server",),namespace="probe"); second.block()
    with pytest.raises(RuntimeError,match="blocked"): first.launch(state,"launch",("server",),namespace="probe")
    assert io_a.launches==[]


def test_containment_unknown_atomically_blocks_campaign():
    parent,state=authority_and_state(); main=Descendant(1,1,"/mock/worker",2,1,"/cg")
    io=MockContainmentIO(("MainPID=1\nControlGroup=/cg\nActiveState=active\nSubState=running","MainPID=1"),
        waits=(True,),descendants=((main,),),cgroup_sources=(("/cg",(1,),1),))
    runner=LiveRunner(executor=io,parent=parent)
    with pytest.raises(RuntimeError): runner.stop(LiveContainment(io,unit="u",cgroup="/cg",clock=lambda:0))
    with pytest.raises(RuntimeError,match="blocked"):
        runner.prepare(state,"launch",("server",),namespace="probe")


def test_parent_reservation_replay_and_namespaces():
    parent,state=authority_and_state(); first=LiveRunner(executor=empty_io(),parent=parent); second=LiveRunner(executor=empty_io(),parent=parent)
    first.prepare(state,"launch",("server",),namespace="qualification")
    with pytest.raises(RuntimeError,match="reservation"): second.prepare(state,"launch",("server",),namespace="qualification")
    second.prepare(state,"launch",("server",),namespace="probe")


def test_same_cell_or_launch_replay_is_denied_before_recording():
    parent,state=authority_and_state(); io=empty_io(); runner=LiveRunner(executor=io,parent=parent)
    runner.prepare(state,"launch",("server",),namespace="final")
    with pytest.raises(RuntimeError,match="replay"): runner.prepare(state,"other",("server",),namespace="final")
    assert io.launches==[]


def test_launch_rejects_arguments_different_from_reservation():
    parent,state=authority_and_state(); io=empty_io(); runner=LiveRunner(executor=io,parent=parent)
    runner.prepare(state,"launch",("server","a"),namespace="qualification")
    with pytest.raises(RuntimeError,match="arguments"):
        runner.launch(state,"launch",("server","b"),namespace="qualification")
    assert io.launches==[]


def test_atomic_concurrent_reservation_has_exactly_one_winner():
    parent,state=authority_and_state(); outcomes=[]
    def reserve():
        try:
            LiveRunner(executor=empty_io(),parent=parent).prepare(state,"launch",("server",),namespace="qualification")
            outcomes.append("won")
        except RuntimeError: outcomes.append("rejected")
    threads=(threading.Thread(target=reserve),threading.Thread(target=reserve))
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert sorted(outcomes)==["rejected","won"]


def test_atomic_concurrent_launch_consumes_preparation_once():
    parent,state=authority_and_state(); io=empty_io(); runner=LiveRunner(executor=io,parent=parent)
    runner.prepare(state,"launch",("server",),namespace="qualification"); outcomes=[]
    def launch():
        try: runner.launch(state,"launch",("server",),namespace="qualification"); outcomes.append("won")
        except RuntimeError: outcomes.append("rejected")
    threads=(threading.Thread(target=launch),threading.Thread(target=launch))
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert sorted(outcomes)==["rejected","won"] and len(io.launches)==1
