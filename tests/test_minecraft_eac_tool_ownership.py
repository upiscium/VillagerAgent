from contextlib import contextmanager, nullcontext
import inspect
from types import SimpleNamespace

import pytest
from langchain_core.tools import tool

from benchmarks.minecraft.eac_runtime import MinecraftEACRuntime
from benchmarks.minecraft.k11_analysis import replay_admissibility
from benchmarks.minecraft.k11_instrumentation import (
    K11ProcessInstrumentation,
    instrument_runtime,
)
from benchmarks.minecraft.k11_trace import K11TraceRecorder, validate_trace
from env.env import VillagerBench, env_type
from env.runtime_paths import RuntimePaths


TARGET = (18, 71, 44)


def _raw_tools(calls):
    @tool
    def scanNearbyEntities(
        player_name: str,
        item_name: str,
        radius: int,
        item_num: int,
        emotion: list,
        murmur: str,
    ) -> dict:
        """Return one sanitized, actor-visible nearby result."""
        calls.append(("scanNearbyEntities", player_name))
        return {
            "status": True,
            "data": [{"name": item_name, "position": list(TARGET)}],
        }

    @tool
    def navigateTo(
        player_name: str,
        x: int,
        y: int,
        z: int,
        emotion: list,
        murmur: str,
    ) -> dict:
        """Return a network-free successful navigation result."""
        calls.append(("navigateTo", player_name, x, y, z))
        return {"status": True, "position": [x, y, z]}

    return [scanNearbyEntities, navigateTo]


@contextmanager
def _registered_environment(tmp_path, *, k11: bool):
    calls = []
    raw_tools = _raw_tools(calls)
    raw_funcs = [item.func for item in raw_tools]
    trace = K11TraceRecorder("ownership-on") if k11 else None
    instrumentation = K11ProcessInstrumentation(trace) if k11 else nullcontext()
    with instrumentation:
        env = VillagerBench(
            env_type.none,
            0,
            False,
            _virtual_debug=True,
            runtime_paths=RuntimePaths.isolated(tmp_path / ("on" if k11 else "off")),
        )
        runtime = MinecraftEACRuntime(
            mode="dual_dag_advisory",
            run_id="ownership-on" if k11 else "ownership-off",
            audit_path=None,
            env_prechecks={
                "scanNearbyEntities": lambda unused: True,
                "navigateTo": lambda unused: True,
            },
        )
        if k11:
            instrument_runtime(runtime, trace)
        env.configure_eac_runtime(runtime)
        env.agent_register(raw_tools, 2, ["Alice", "Bob"])
        yield SimpleNamespace(
            env=env,
            runtime=runtime,
            trace=trace,
            calls=calls,
            raw_tools=raw_tools,
            raw_funcs=raw_funcs,
            alice=env.agent_pool[0],
            bob=env.agent_pool[1],
        )


def _owned_tool(owner, name):
    return next(item for item in owner.tools if item.name == name)


def _invoke_scan(tool_object, actor):
    return tool_object.invoke({
        "player_name": actor,
        "item_name": "stone",
        "radius": 30,
        "item_num": 1,
        "emotion": [],
        "murmur": "",
    })


def _invoke_navigate(tool_object, actor, target=TARGET):
    return tool_object.invoke({
        "player_name": actor,
        "x": target[0],
        "y": target[1],
        "z": target[2],
        "emotion": [],
        "murmur": "",
    })


def _captured_actor(function):
    return inspect.getclosurevars(function).nonlocals.get("actor_name")


def test_two_agents_receive_distinct_owned_tools_without_mutating_raw_tools(tmp_path):
    with _registered_environment(tmp_path, k11=False) as fixture:
        for index, name in enumerate(("scanNearbyEntities", "navigateTo")):
            raw = fixture.raw_tools[index]
            alice = _owned_tool(fixture.alice, name)
            bob = _owned_tool(fixture.bob, name)
            assert raw is not alice and raw is not bob and alice is not bob
            assert raw.func is fixture.raw_funcs[index]
            assert alice.__dict__ is not raw.__dict__
            assert bob.__dict__ is not raw.__dict__
            assert alice.__dict__ is not bob.__dict__
            assert alice.__fields_set__ is not raw.__fields_set__
            assert bob.__fields_set__ is not raw.__fields_set__
            assert alice._lc_kwargs is not raw._lc_kwargs
            assert bob._lc_kwargs is not raw._lc_kwargs
            assert alice.func is not raw.func
            assert bob.func is not raw.func
            assert alice.func is not bob.func
            assert _captured_actor(alice.func) == "Alice"
            assert _captured_actor(bob.func) == "Bob"
            assert alice._lc_kwargs["func"] is alice.func
            assert bob._lc_kwargs["func"] is bob.func
            assert raw._lc_kwargs["func"] is raw.func


@pytest.mark.parametrize("k11", [False, True], ids=["k11-off", "k11-on"])
def test_owner_correct_langchain_dispatch_is_accepted_with_k11_ablation(tmp_path, k11):
    with _registered_environment(tmp_path, k11=k11) as fixture:
        alice_result = _invoke_scan(
            _owned_tool(fixture.alice, "scanNearbyEntities"), "Alice",
        )
        bob_result = _invoke_scan(_owned_tool(fixture.bob, "scanNearbyEntities"), "Bob")
        assert alice_result["status"] is True
        assert bob_result["status"] is True
        assert fixture.calls == [
            ("scanNearbyEntities", "Alice"),
            ("scanNearbyEntities", "Bob"),
        ]
        assert fixture.runtime.audit_artifact()["evidence_total"] > 0


def test_cross_actor_tool_call_is_rejected_before_native_invocation(tmp_path):
    with _registered_environment(tmp_path, k11=False) as fixture:
        with pytest.raises(RuntimeError, match="Minecraft EAC actor identity mismatch"):
            _invoke_scan(_owned_tool(fixture.bob, "scanNearbyEntities"), "Alice")
        assert fixture.calls == []
        assert fixture.runtime.audit_artifact()["evidence_total"] == 0


def test_k11_instrumentation_preserves_owned_wrapper_chain(tmp_path):
    with _registered_environment(tmp_path, k11=True) as fixture:
        for owner, expected in ((fixture.alice, "Alice"), (fixture.bob, "Bob")):
            traced = _owned_tool(owner, "scanNearbyEntities").func
            assert _captured_actor(traced) == expected
            guarded = inspect.getclosurevars(traced).nonlocals["original_func"]
            assert _captured_actor(guarded) == expected
        _invoke_scan(_owned_tool(fixture.bob, "scanNearbyEntities"), "Bob")
        actors = [
            event["actor_id"] for event in fixture.trace.artifact()["events"]
            if event["event_type"] == "k11.tool_call_entered"
        ]
        assert actors == ["Bob"]


def test_controller_construction_does_not_rebind_registered_agent_tools(
    tmp_path, monkeypatch,
):
    import pipeline.controller_tiny as controller_module

    with _registered_environment(tmp_path, k11=False) as fixture:
        before = [
            [(id(item), id(item.func)) for item in owner.tools]
            for owner in (fixture.alice, fixture.bob)
        ]
        raw_before = [
            (id(item), id(item.func), id(item.__dict__)) for item in fixture.raw_tools
        ]
        constructed = []

        class FakeBaseAgent:
            LOCAL_MODEL_CONFIG_KEYS = frozenset()

            def __init__(self, unused_llm, unused_env, unused_dm, *, name, all_tools, **kwargs):
                self.name = name
                self.all_tools = all_tools
                constructed.append(self)

        monkeypatch.setattr(controller_module, "BaseAgent", FakeBaseAgent)
        monkeypatch.setattr(
            controller_module,
            "init_language_model",
            lambda unused: SimpleNamespace(role_name=""),
        )
        task_manager = SimpleNamespace(llm=None, dm=None, agent_list=None)
        data_manager = SimpleNamespace(llm=None)
        controller = controller_module.GlobalController(
            {}, task_manager, data_manager, fixture.env,
            all_tools=fixture.raw_tools,
        )
        try:
            after = [
                [(id(item), id(item.func)) for item in owner.tools]
                for owner in (fixture.alice, fixture.bob)
            ]
            assert after == before
            assert [
                (id(item), id(item.func), id(item.__dict__)) for item in fixture.raw_tools
            ] == raw_before
            assert [agent.name for agent in constructed] == ["Alice", "Bob"]
            assert constructed[0].all_tools[0] is not constructed[1].all_tools[0]
            assert constructed[0].all_tools[0].func is not constructed[1].all_tools[0].func
        finally:
            controller.executor.shutdown(wait=False, cancel_futures=True)


def test_eac_guard_fails_closed_for_supported_tool_without_func(tmp_path):
    class InvokeOnlyTool:
        name = "navigateTo"

        def invoke(self, arguments):
            return {"status": True, "arguments": arguments}

    with _registered_environment(tmp_path, k11=False) as fixture:
        with pytest.raises(RuntimeError, match="requires callable func"):
            fixture.env.guard_tool_actions([InvokeOnlyTool()], actor_name="Bob")


def test_successful_scan_ingests_actor_scoped_destination_evidence(tmp_path):
    with _registered_environment(tmp_path, k11=True) as fixture:
        _invoke_scan(_owned_tool(fixture.bob, "scanNearbyEntities"), "Bob")
        evidence = [
            event for event in fixture.trace.artifact()["events"]
            if event["event_type"] == "k11.eac_evidence_ingested"
            and event["payload"]["proposition"]["predicate"] == "destination_observed"
        ]
        assert len(evidence) == 1
        assert evidence[0]["actor_id"] == "Bob"
        assert evidence[0]["payload"]["visible_to"] == ["Bob"]
        assert evidence[0]["payload"]["proposition"]["arguments"] == list(TARGET)


def test_matching_navigation_is_live_and_offline_baseline_admissible(tmp_path):
    with _registered_environment(tmp_path, k11=True) as fixture:
        _invoke_scan(_owned_tool(fixture.bob, "scanNearbyEntities"), "Bob")
        _invoke_navigate(_owned_tool(fixture.bob, "navigateTo"), "Bob")
        artifact = fixture.trace.artifact()
        prepared = next(
            event for event in artifact["events"]
            if event["event_type"] == "k11.eac_action_prepared"
            and event["payload"]["exact_request"]["action"]["identity"] == "navigateTo"
        )
        candidate_id = prepared["payload"]["exact_request"]["candidate_id"]
        live = fixture.runtime.authority.evaluate(candidate_id)
        offline = replay_admissibility(
            artifact,
            prepared,
            cutoff_seq=prepared["seq"],
            replay_label="owner-parity:prepare",
        )
        assert live.admissible is True
        assert offline["admissible"] is True
        assert validate_trace(artifact)["valid"] is True


def test_other_actor_evidence_does_not_admit_bob_navigation(tmp_path):
    with _registered_environment(tmp_path, k11=True) as fixture:
        _invoke_scan(_owned_tool(fixture.alice, "scanNearbyEntities"), "Alice")
        _invoke_navigate(_owned_tool(fixture.bob, "navigateTo"), "Bob")
        artifact = fixture.trace.artifact()
        prepared = next(
            event for event in artifact["events"]
            if event["event_type"] == "k11.eac_action_prepared"
            and event["actor_id"] == "Bob"
            and event["payload"]["exact_request"]["action"]["identity"] == "navigateTo"
        )
        candidate_id = prepared["payload"]["exact_request"]["candidate_id"]
        assert fixture.runtime.authority.evaluate(candidate_id).admissible is False
        offline = replay_admissibility(
            artifact,
            prepared,
            cutoff_seq=prepared["seq"],
            replay_label="owner-isolation:prepare",
        )
        assert offline["admissible"] is False
