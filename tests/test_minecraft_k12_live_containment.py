from dataclasses import replace

import pytest

from benchmarks.minecraft.k12_live_containment import (
    Descendant, LiveContainment, LiveState, MockContainmentIO, Probe,
    INJECTED_FAKE_ORIGIN, parse_observation, systemd_run_command,
    validate_final_observation,
)
from benchmarks.minecraft.k12_containment import containment_identity
from benchmarks.minecraft.k12_live_runner import (
    FinalCellAdmissionRunner, InjectedFakeTransport,
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


def runner_scripted(cgroup):
    main = Descendant(99, 8, "/mock/worker", 1, 99, cgroup)
    running = f"MainPID=99\nControlGroup={cgroup}\nActiveState=active\nSubState=running"
    stopped = f"MainPID=0\nControlGroup={cgroup}\nActiveState=inactive\nSubState=dead"
    return MockContainmentIO(
        (running, running, stopped),
        waits=(True,),
        descendants=((main,), (main,), ()),
        cgroup_sources=((cgroup, (99,), 1), (cgroup, (99,), 1), (cgroup, (), 0)),
    )


@pytest.fixture
def launched_graph(tmp_path):
    helpers = pytest.importorskip("test_minecraft_k12_authority_e2e")
    graph = helpers._build_graph(tmp_path, f"containment-{tmp_path.name}")
    graph.cell_authority.consume_for_launch()
    graph.cell_authority.complete(graph.cell_evidence)
    try:
        yield graph
    finally:
        graph.close()


def test_exact_command_and_probes():
    assert systemd_run_command("u.service",("server",)) == (
        "systemd-run","--user","--unit=u.service","--collect",
        "--service-type=exec","--same-dir","--quiet","--","server")
    assert tuple(Probe)==(Probe.P1,Probe.P2,Probe.P3,Probe.P4)


def test_scripted_containment_transport_has_no_execution_fallback():
    io = MockContainmentIO((show(pid=0, state="inactive", sub="dead"),),
        descendants=((),), cgroup_sources=(("/cg", (), 0),))
    assert not hasattr(io, "execute")
    assert not hasattr(io, "run")
    assert io.evidence_origin == INJECTED_FAKE_ORIGIN
    assert io.runtime_admissible is False


def test_containment_requires_postdispatch_completion(tmp_path):
    helpers = pytest.importorskip("test_minecraft_k12_authority_e2e")
    graph = helpers._build_graph(tmp_path, "containment-order-gate")
    try:
        assert graph.cell_authority.validate_for_containment() is False
        with pytest.raises(RuntimeError, match="incomplete"):
            LiveContainment(
                scripted(),
                unit="u",
                cgroup="/cg",
                clock=lambda: 0,
                cell_authority=graph.cell_authority,
            )
        graph.cell_authority.consume_for_launch()
        assert graph.cell_authority.validate_for_containment() is False
        with pytest.raises(RuntimeError, match="incomplete"):
            LiveContainment(
                scripted(),
                unit="u",
                cgroup="/cg",
                clock=lambda: 0,
                cell_authority=graph.cell_authority,
            )
    finally:
        graph.close()


def test_runner_launch_complete_then_containment_stop(tmp_path):
    helpers = pytest.importorskip("test_minecraft_k12_authority_e2e")
    graph = helpers._build_graph(tmp_path, "containment-runner-order")
    try:
        launch_id = "containment-order"
        _, cgroup = containment_identity(
            f"{graph.cell_authority.binding.namespace}:{graph.state.cell}",
            launch_id,
        )
        io = runner_scripted(cgroup)
        runner = FinalCellAdmissionRunner(
            executor=io,
            parent=graph.launch_authority,
            admission=graph.admission,
            lease=graph.final_lease,
            ledger=graph.final_ledger,
            mode="injected_fake",
            external_entry_fence=graph.fence,
            fake_transport=InjectedFakeTransport(graph.fence),
        )
        prepared = runner.prepare(
            graph.cell_authority,
            graph.state,
            launch_id,
            graph.argv,
        )
        launched = runner.launch(
            graph.cell_authority,
            graph.state,
            launch_id,
            graph.argv,
        )
        assert prepared.executed is False
        assert launched.executed is True
        assert graph.cell_authority.launch_consumed is True
        assert graph.cell_authority.consumed is False

        completed = runner.complete(
            graph.cell_authority,
            graph.cell_evidence,
            launch_id=launch_id,
        )
        permit = graph.cell_authority.launch_permit
        assert completed is graph.cell_evidence
        assert permit is not None
        assert graph.cell_authority.consumed is True
        assert permit.validate_for_containment(graph.cell_evidence) is True
        assert graph.cell_authority.validate_for_containment(graph.cell_evidence) is True

        controller = LiveContainment(
            io,
            unit=launched.unit_id,
            cgroup=launched.cgroup_id,
            clock=lambda: 0,
            cell_authority=graph.cell_authority,
        )
        stopped = runner.stop(controller, graph.cell_authority)
        assert stopped.active_state == "inactive"
        assert controller.state is LiveState.CLEAN
        assert runner.closed is True
    finally:
        graph.close()


def test_cooperative_term_and_kill_escalation_are_authority_driven(launched_graph):
    cooperative=scripted(); result=LiveContainment(cooperative,unit="u",cgroup="/cg",clock=lambda:0,cell_authority=launched_graph.cell_authority).stop()
    assert result.active_state=="inactive" and cooperative.authority_signals==["TERM"]
    forced=scripted(cooperative=False); LiveContainment(forced,unit="u",cgroup="/cg",clock=lambda:0,cell_authority=launched_graph.cell_authority).stop()
    assert forced.authority_signals==["TERM","KILL"]


def test_split_authorities_and_descendant_retention(launched_graph):
    child=Descendant(100,9,"/mock/child",99,100,"/cg")
    io=scripted(child=child); result=LiveContainment(io,unit="u",cgroup="/cg",clock=lambda:0,cell_authority=launched_graph.cell_authority).stop()
    assert result.systemd_authority.digest != result.cgroup_authority.digest
    assert result.procfs_authority.processes == ()


def test_pid_reuse_escape_and_unknown_authority_fail_closed(launched_graph):
    reused=Descendant(99,10,"/mock/other",1,99,"/cg")
    with pytest.raises(RuntimeError,match="identity"):
        LiveContainment(scripted(drift=reused),unit="u",cgroup="/cg",clock=lambda:0,cell_authority=launched_graph.cell_authority).stop()
    bad=MockContainmentIO((show(),),descendants=((MAIN,),),cgroup_sources=(("/other",(99,),1),))
    with pytest.raises(RuntimeError,match="cgroup"):
        parse_observation(bad,"u","/cg",cell_authority=launched_graph.cell_authority)
    unknown=MockContainmentIO((show(),),descendants=((MAIN,),),cgroup_sources=())
    with pytest.raises(RuntimeError,match="cgroup"):
        parse_observation(unknown,"u","/cg",cell_authority=launched_graph.cell_authority)


def test_new_final_related_process_is_not_clean(launched_graph):
    child=Descendant(100,9,"/mock/child",99,100,"/cg")
    running=(MAIN,child)
    survivor=(child,)
    io=MockContainmentIO((show(),show(),show(pid=0,state="inactive",sub="dead")),waits=(True,),
        descendants=(running,running,survivor),
        cgroup_sources=(("/cg",(99,100),1),("/cg",(99,100),1),("/cg",(100,),1)),
        final_identities={100:child})
    controller=LiveContainment(io,unit="u",cgroup="/cg",clock=lambda:0,cell_authority=launched_graph.cell_authority)
    with pytest.raises(RuntimeError,match="clean"):
        controller.stop()
    assert controller.state is LiveState.FAILED

def test_escaped_retained_descendant_is_rejected_by_independent_pid_lookup(launched_graph):
    child=Descendant(100,9,"/mock/child",99,100,"/cg")
    escaped=Descendant(100,9,"/mock/child",99,100,"/other")
    running=(MAIN,child)
    io=MockContainmentIO((show(),show(),show(pid=0,state="inactive",sub="dead")),waits=(True,),
        descendants=(running,running,()),cgroup_sources=(("/cg",(99,100),1),("/cg",(99,100),1),("/cg",(),0)),
        final_identities={100:escaped})
    with pytest.raises(RuntimeError,match="identity drift"):
        LiveContainment(io,unit="u",cgroup="/cg",clock=lambda:0,cell_authority=launched_graph.cell_authority).stop()


def test_final_observation_rejects_cross_cell_and_controller_splice(launched_graph, tmp_path):
    second = launched_graph.admission.issue_cell(launched_graph.admission.schedule[1])
    second.consume_for_launch()
    second.complete(
        replace(
            launched_graph.cell_evidence,
            cell_id=second.cell_id,
            evidence_digest="e" * 64,
        )
    )
    assert launched_graph.cell_authority.validate_for_containment(
        launched_graph.cell_evidence,
        launch_permit=second.launch_permit,
    ) is False
    shared_io = scripted()
    LiveContainment(
        shared_io,
        unit="u",
        cgroup="/cg",
        clock=lambda: 0,
        cell_authority=launched_graph.cell_authority,
    )
    with pytest.raises(RuntimeError, match="controller authority"):
        LiveContainment(
            shared_io,
            unit="u",
            cgroup="/cg",
            clock=lambda: 0,
            cell_authority=second,
        )

    spliced = LiveContainment(
        scripted(),
        unit="u",
        cgroup="/cg",
        clock=lambda: 0,
        cell_authority=launched_graph.cell_authority,
    )
    with pytest.raises(RuntimeError, match="authority identity"):
        spliced.stop(cell_authority=second)
    assert spliced.io.signals == []

    io = scripted()
    observation = LiveContainment(
        io,
        unit="u",
        cgroup="/cg",
        clock=lambda: 0,
        cell_authority=launched_graph.cell_authority,
    ).stop()
    assert validate_final_observation(observation, second) is False

    helpers = pytest.importorskip("test_minecraft_k12_authority_e2e")
    other = helpers._build_graph(tmp_path / "other", "containment-other")
    try:
        other.cell_authority.consume_for_launch()
        assert validate_final_observation(observation, other.cell_authority) is False
    finally:
        other.close()


def test_stop_rejects_revoked_final_cell_lifecycle(launched_graph):
    io = scripted()
    controller = LiveContainment(
        io,
        unit="u",
        cgroup="/cg",
        clock=lambda: 0,
        cell_authority=launched_graph.cell_authority,
    )
    launched_graph.controller.quarantine_ledger(
        launched_graph.final_ledger, {"reason": "containment lifecycle revoke"}
    )
    assert launched_graph.cell_authority.validate_for_containment(
        launched_graph.cell_evidence
    ) is False
    with pytest.raises(RuntimeError, match="stale or revoked"):
        controller.stop()
    assert io.signals == []
