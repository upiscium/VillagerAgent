import pytest

from benchmarks.minecraft.k12_live_containment import (
    Descendant, LiveContainment, LiveState, MockContainmentIO, Probe,
    parse_observation, systemd_run_command,
)


def show(pid=99, state="active", sub="running"):
    return f"MainPID={pid}\nControlGroup=/cg\nActiveState={state}\nSubState={sub}"


MAIN = Descendant(99, 8, "/mock/worker", 1, 99, "/cg")


def scripted(*, cooperative=True, child=None, drift=None):
    running=(MAIN,)+((child,) if child else ())
    middle=(drift if drift is not None else MAIN,)+((child,) if child else ())
    return MockContainmentIO(
        (show(),show(),show(pid=0,state="inactive",sub="dead")),
        waits=((True,) if cooperative else (False,True)),
        descendants=(running,middle,()),
        cgroup_sources=(("/cg",tuple(item.pid for item in running),1),
                        ("/cg",tuple(item.pid for item in middle),1),
                        ("/cg",(),0)),
        final_identities=({child.pid:None} if child else {}),
    )


def test_exact_command_and_probes():
    assert systemd_run_command("u.service",("server",)) == (
        "systemd-run","--user","--unit=u.service","--collect",
        "--service-type=exec","--same-dir","--quiet","--","server")
    assert tuple(Probe)==(Probe.P1,Probe.P2,Probe.P3,Probe.P4)


def test_cooperative_term_and_kill_escalation_are_authority_driven():
    cooperative=scripted(); result=LiveContainment(cooperative,unit="u",cgroup="/cg",clock=lambda:0).stop()
    assert result.active_state=="inactive" and cooperative.authority_signals==["TERM"]
    forced=scripted(cooperative=False); LiveContainment(forced,unit="u",cgroup="/cg",clock=lambda:0).stop()
    assert forced.authority_signals==["TERM","KILL"]


def test_split_authorities_and_descendant_retention():
    child=Descendant(100,9,"/mock/child",99,100,"/cg")
    io=scripted(child=child); result=LiveContainment(io,unit="u",cgroup="/cg",clock=lambda:0).stop()
    assert result.systemd_authority.digest != result.cgroup_authority.digest
    assert result.procfs_authority.processes == ()


def test_pid_reuse_escape_and_unknown_authority_fail_closed():
    reused=Descendant(99,10,"/mock/other",1,99,"/cg")
    with pytest.raises(RuntimeError,match="identity"):
        LiveContainment(scripted(drift=reused),unit="u",cgroup="/cg",clock=lambda:0).stop()
    bad=MockContainmentIO((show(),),descendants=((MAIN,),),cgroup_sources=(("/other",(99,),1),))
    with pytest.raises(RuntimeError,match="cgroup"):
        parse_observation(bad,"u","/cg")
    unknown=MockContainmentIO((show(),),descendants=((MAIN,),),cgroup_sources=())
    with pytest.raises(RuntimeError,match="cgroup"):
        parse_observation(unknown,"u","/cg")


def test_new_final_related_process_is_not_clean():
    child=Descendant(100,9,"/mock/child",99,100,"/cg")
    running=(MAIN,child)
    survivor=(child,)
    io=MockContainmentIO((show(),show(),show(pid=0,state="inactive",sub="dead")),waits=(True,),
        descendants=(running,running,survivor),
        cgroup_sources=(("/cg",(99,100),1),("/cg",(99,100),1),("/cg",(100,),1)),
        final_identities={100:child})
    controller=LiveContainment(io,unit="u",cgroup="/cg",clock=lambda:0)
    with pytest.raises(RuntimeError,match="clean"):
        controller.stop()
    assert controller.state is LiveState.FAILED

def test_escaped_retained_descendant_is_rejected_by_independent_pid_lookup():
    child=Descendant(100,9,"/mock/child",99,100,"/cg")
    escaped=Descendant(100,9,"/mock/child",99,100,"/other")
    running=(MAIN,child)
    io=MockContainmentIO((show(),show(),show(pid=0,state="inactive",sub="dead")),waits=(True,),
        descendants=(running,running,()),cgroup_sources=(("/cg",(99,100),1),("/cg",(99,100),1),("/cg",(),0)),
        final_identities={100:escaped})
    with pytest.raises(RuntimeError,match="identity drift"):
        LiveContainment(io,unit="u",cgroup="/cg",clock=lambda:0).stop()
