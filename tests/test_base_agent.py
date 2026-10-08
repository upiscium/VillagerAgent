import threading

import pytest

from pipeline.agent import BaseAgent
from env.minecraft_client import (
    MinecraftActionLogError,
    MinecraftToolEffectUnknownError,
    MinecraftToolTimeoutError,
    ToolActionBlockedError,
)
from model.vllm_model import VLLMLanguageModel
from type_define.graph import Task


class FakeEnv:
    running = True

    def __init__(self, *, failures_before_success=0):
        self.failures_before_success = failures_before_success
        self.step_calls = 0
        self.agent_status_calls = 0
        self.last_task_prompt = ""

    def step(self, name, task_prompt):
        self.step_calls += 1
        self.last_task_prompt = task_prompt
        if self.step_calls <= self.failures_before_success:
            raise RuntimeError(f"step failed {self.step_calls}")
        return "done", {"action_list": [], "final_answer": "done"}

    def agent_status(self, name):
        self.agent_status_calls += 1
        return {"status": True, "message": {"my_name": name}}


class FakeDataManager:
    def __init__(self):
        self.updated = []

    def query_env_with_task(self, description, agent_query=False):
        return "env summary"

    def query_history(self, name):
        return "agent history"

    def query_other_agent_state(self, name):
        return "no other agents"

    def update_database(self, payload):
        self.updated.append(payload)


class FakeLocalModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    def few_shot_generate_thoughts(self, *args, **kwargs):
        self.calls += 1
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class FakeTool:
    name = "inspect"

    def __init__(self, feedback=None, error=None, on_call=None):
        self.feedback = feedback
        self.error = error
        self.on_call = on_call
        self.calls = 0

    def __call__(self, tool_input):
        self.calls += 1
        if self.on_call is not None:
            self.on_call()
        if self.error is not None:
            raise self.error
        return self.feedback


def make_local_agent(responses, *, tools=None, **kwargs):
    env = FakeEnv()
    dm = FakeDataManager()
    llm = FakeLocalModel(responses)
    agent = BaseAgent(
        llm=llm,
        env=env,
        data_manager=dm,
        name="Alice",
        silent=True,
        all_tools=tools or [],
        **kwargs,
    )
    task = Task("Inspect area", {"document": "public"})
    task._agent = ["Alice"]
    return agent, task, llm, dm


def test_base_agent_normal_step_raises_original_error_after_retry_exhaustion(monkeypatch):
    monkeypatch.setattr("pipeline.agent.time.sleep", lambda seconds: None)
    env = FakeEnv(failures_before_success=3)
    dm = FakeDataManager()
    agent = BaseAgent(llm=object(), env=env, data_manager=dm, name="Alice", silent=True)
    task = Task("Inspect area", {"document": "public"})
    task._agent = ["Alice"]

    with pytest.raises(RuntimeError, match="step failed 3"):
        agent.normal_step(task)

    assert env.step_calls == 3
    assert env.agent_status_calls == 0
    assert dm.updated == []
    assert agent.IDLE is True


def test_base_agent_normal_step_updates_database_after_success(monkeypatch):
    monkeypatch.setattr("pipeline.agent.time.sleep", lambda seconds: None)
    env = FakeEnv(failures_before_success=2)
    dm = FakeDataManager()
    agent = BaseAgent(llm=object(), env=env, data_manager=dm, name="Alice", silent=True)
    task = Task("Inspect area", {"document": "public"})
    task._agent = ["Alice"]

    feedback, detail = agent.normal_step(task)

    assert feedback == "done"
    assert detail["final_answer"] == "done"
    assert env.step_calls == 3
    assert env.agent_status_calls == 1
    assert len(dm.updated) == 1
    assert dm.updated[0]["detail"] == detail
    assert agent.IDLE is True


def test_base_agent_normal_step_does_not_retry_minecraft_timeout(monkeypatch):
    env = FakeEnv()
    dm = FakeDataManager()
    agent = BaseAgent(llm=object(), env=env, data_manager=dm, name="Alice", silent=True)
    task = Task("Place block", {"document": "public"})
    task._agent = ["Alice"]
    sleep_calls = []

    def timeout_step(*_args, **_kwargs):
        env.step_calls += 1
        raise MinecraftToolTimeoutError(
            "response timed out",
            agent="Alice",
            tool="post_place",
        )

    env.step = timeout_step
    monkeypatch.setattr("pipeline.agent.time.sleep", sleep_calls.append)

    with pytest.raises(MinecraftToolTimeoutError, match="response timed out") as raised:
        agent.normal_step(task)

    assert env.step_calls == 1
    assert sleep_calls == []
    assert raised.value.failure_detail["outcome_certainty"] == "unknown"
    assert raised.value.failure_detail["retry_safe"] is False
    assert agent.IDLE is True


def test_base_agent_normal_step_does_not_retry_action_log_failure(monkeypatch):
    env = FakeEnv()
    dm = FakeDataManager()
    agent = BaseAgent(llm=object(), env=env, data_manager=dm, name="Alice", silent=True)
    task = Task("Place block", {"document": "public"})
    task._agent = ["Alice"]
    sleep_calls = []

    def logging_failure(*_args, **_kwargs):
        env.step_calls += 1
        raise MinecraftActionLogError("action log invalid", agent="Alice")

    env.step = logging_failure
    monkeypatch.setattr("pipeline.agent.time.sleep", sleep_calls.append)

    with pytest.raises(MinecraftActionLogError, match="action log invalid"):
        agent.normal_step(task)

    assert env.step_calls == 1
    assert sleep_calls == []


@pytest.mark.parametrize("error", [
    ToolActionBlockedError("admission closed"),
    MinecraftToolEffectUnknownError("effect unknown"),
])
def test_base_agent_normal_step_does_not_retry_non_retryable_tool_outcomes(error, monkeypatch):
    env = FakeEnv()
    agent = BaseAgent(llm=object(), env=env, data_manager=FakeDataManager(),
                      name="Alice", silent=True)
    task = Task("Place block", {})
    task._agent = ["Alice"]
    sleeps = []

    def fail_once(*_args, **_kwargs):
        env.step_calls += 1
        raise error

    env.step = fail_once
    monkeypatch.setattr("pipeline.agent.time.sleep", sleeps.append)
    with pytest.raises(type(error), match=str(error)):
        agent.normal_step(task)
    assert env.step_calls == 1
    assert sleeps == []


def test_normal_step_cancelled_before_env_step_returns_canonical_detail():
    cancellation = threading.Event()
    cancellation.set()
    agent, task, _llm, dm = make_local_agent([])

    feedback, detail = agent.normal_step(task, cancellation_token=cancellation)

    assert feedback["status"] is False
    assert detail["failure"]["reason"] == "cancelled"
    assert detail["failure"]["cancellation_acknowledged"] is True
    assert detail["failure"]["phase"] == "before_env_step"
    assert agent.env.step_calls == 0
    assert dm.updated == []


def test_normal_step_cancelled_after_env_step_has_no_status_or_database_side_effects():
    cancellation = threading.Event()

    class CancelAfterStepEnv(FakeEnv):
        def step(self, name, task_prompt, **kwargs):
            result = super().step(name, task_prompt)
            cancellation.set()
            return result

    env = CancelAfterStepEnv()
    dm = FakeDataManager()
    agent = BaseAgent(llm=object(), env=env, data_manager=dm, name="Alice", silent=True)
    task = Task("Inspect area", {})
    task._agent = ["Alice"]

    feedback, detail = agent.normal_step(task, cancellation_token=cancellation)

    assert feedback["status"] is False
    assert detail["failure"]["reason"] == "cancelled"
    assert detail["failure"]["phase"] == "after_env_return"
    assert env.agent_status_calls == 0
    assert dm.updated == []


def test_normal_step_retry_wait_is_cancellation_aware_and_does_not_call_env_again():
    cancellation = threading.Event()
    env = FakeEnv(failures_before_success=3)
    dm = FakeDataManager()
    agent = BaseAgent(llm=object(), env=env, data_manager=dm, name="Alice", silent=True)
    task = Task("Inspect area", {})
    task._agent = ["Alice"]

    def cancel_during_wait(_token, _timeout):
        cancellation.set()
        return True

    # The production wait helper is intentionally replaced only to make the
    # cancellation boundary deterministic and avoid wall-clock sleeps.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("pipeline.agent.wait_for_agent_cancellation", cancel_during_wait)
    try:
        feedback, detail = agent.normal_step(task, cancellation_token=cancellation)
    finally:
        monkeypatch.undo()

    assert env.step_calls == 1
    assert feedback["status"] is False
    assert detail["failure"]["reason"] == "cancelled"
    assert dm.updated == []


def test_local_step_records_minecraft_timeout_as_retry_unsafe():
    tool = FakeTool(error=MinecraftToolTimeoutError("response timed out"))
    agent, task, _llm, _dm = make_local_agent(
        ["Action: {'tool': 'inspect', 'tool_input': {}}"],
        tools=[tool],
    )

    feedback, detail = agent.local_step(task)

    assert tool.calls == 1
    assert feedback["status"] is False
    assert detail["failure"]["reason"] == "minecraft_tool_timeout"
    assert detail["failure"]["outcome_certainty"] == "unknown"
    assert detail["failure"]["retry_safe"] is False


def test_base_agent_normal_step_truncates_long_task_content(monkeypatch):
    monkeypatch.setattr("pipeline.agent.time.sleep", lambda seconds: None)
    env = FakeEnv()
    dm = FakeDataManager()
    agent = BaseAgent(llm=object(), env=env, data_manager=dm, name="Alice", silent=True)
    task = Task("Inspect area", {"document": "visible-start" + ("x" * 20000) + "visible-tail"})
    task._agent = ["Alice"]

    agent.normal_step(task)

    assert "visible-tail" not in env.last_task_prompt
    assert "..." in env.last_task_prompt
    assert "*** The relevant data of task(not environment data)***" in env.last_task_prompt


def test_local_step_bounds_repeated_malformed_model_output():
    agent, task, llm, dm = make_local_agent(
        ["not an action"] * 3,
        local_model_max_attempts=3,
    )

    feedback, detail = agent.local_step(task)

    assert llm.calls == 3
    assert feedback["status"] is False
    assert detail["failure"]["reason"] == "model_attempt_budget_exhausted"
    assert detail["failure"]["model_attempts"] == 3
    assert detail["failure"]["successful_actions"] == 0
    assert detail["failure"]["last_failure"]["reason"] == "malformed_model_output"
    assert dm.updated[0]["detail"] == detail


def test_local_step_bounds_repeated_unknown_tools():
    response = "Action: {'tool': 'missing', 'tool_input': {}}"
    agent, task, llm, _ = make_local_agent(
        [response] * 2,
        local_model_max_attempts=2,
    )

    feedback, detail = agent.local_step(task)

    assert llm.calls == 2
    assert feedback["status"] is False
    assert detail["failure"]["reason"] == "model_attempt_budget_exhausted"
    assert detail["failure"]["last_failure"]["reason"] == "unknown_tool"


def test_local_step_immediate_stop_returns_initialized_success():
    response = "Action: {'tool': 'stop', 'tool_input': {'final_answer': 'finished'}}"
    agent, task, _, _ = make_local_agent([response])

    feedback, detail = agent.local_step(task)

    assert feedback == {"message": "finished", "status": True, "new_events": []}
    assert detail["final_answer"] == "finished"
    assert detail["action_list"] == []
    assert "failure" not in detail
    assert agent.IDLE is True


@pytest.mark.parametrize(
    ("tool", "reason"),
    [
        (FakeTool(error=RuntimeError("broken tool")), "tool_exception"),
        (FakeTool(feedback={"status": True}), "invalid_tool_feedback"),
    ],
)
def test_local_step_returns_structured_tool_failures(tool, reason):
    response = "Action: {'tool': 'inspect', 'tool_input': {}}"
    agent, task, _, _ = make_local_agent([response], tools=[tool])

    feedback, detail = agent.local_step(task)

    assert feedback["status"] is False
    assert feedback["error"]["reason"] == reason
    assert detail["failure"]["reason"] == reason
    assert detail["failure"]["model_attempts"] == 1
    assert detail["failure"]["successful_actions"] == 0
    assert agent.IDLE is True


def test_local_step_cancellation_prevents_another_tool_action():
    cancellation = threading.Event()
    response = "Action: {'tool': 'inspect', 'tool_input': {}}"
    tool = FakeTool(
        feedback={"message": "ok", "status": True},
        on_call=cancellation.set,
    )
    agent, task, llm, _ = make_local_agent(
        [response, response],
        tools=[tool],
        local_model_inter_action_delay=0,
    )

    feedback, detail = agent.local_step(task, cancellation_token=cancellation)

    assert tool.calls == 1
    assert llm.calls == 1
    assert feedback["status"] is False
    assert detail["failure"]["reason"] == "cancelled"
    assert detail["failure"]["cancellation_acknowledged"] is True
    assert detail["failure"]["successful_actions"] == 1
    assert [action["action"]["tool"] for action in detail["action_list"]] == ["inspect"]


def test_local_step_acknowledges_cancellation_at_action_budget_boundary():
    cancellation = threading.Event()
    response = "Action: {'tool': 'inspect', 'tool_input': {}}"
    tool = FakeTool(
        feedback={"message": "ok", "status": True},
        on_call=cancellation.set,
    )
    agent, task, _, _ = make_local_agent(
        [response],
        tools=[tool],
        local_model_max_attempts=1,
        local_model_max_actions=1,
    )

    feedback, detail = agent.local_step(task, cancellation_token=cancellation)

    assert feedback["status"] is False
    assert detail["failure"]["reason"] == "cancelled"
    assert detail["failure"]["model_attempts"] == 1
    assert detail["failure"]["successful_actions"] == 1


def test_local_step_uses_bounded_configurable_inter_action_delay(monkeypatch):
    sleeps = []
    monkeypatch.setattr("pipeline.agent.time.sleep", sleeps.append)
    response = "Action: {'tool': 'inspect', 'tool_input': {}}"
    tool = FakeTool(feedback={"message": "ok", "status": True})
    agent, task, _, _ = make_local_agent(
        [response, "Action: {'tool': 'stop', 'tool_input': {'final_answer': 'done'}}"],
        tools=[tool],
        local_model_inter_action_delay=100,
    )

    agent.local_step(task)

    assert sleeps == [BaseAgent.MAX_LOCAL_INTER_ACTION_DELAY]


def test_local_step_separates_model_attempt_and_action_budgets():
    malformed = "not an action"
    action = "Action: {'tool': 'inspect', 'tool_input': {}}"
    tool = FakeTool(feedback={"message": "ok", "status": True})
    agent, task, llm, _ = make_local_agent(
        [malformed, action, action],
        tools=[tool],
        local_model_max_attempts=4,
        local_model_max_actions=2,
    )

    feedback, detail = agent.local_step(task)

    assert llm.calls == 3
    assert tool.calls == 2
    assert feedback["status"] is False
    assert detail["failure"]["reason"] == "action_budget_exhausted"
    assert detail["failure"]["model_attempts"] == 3
    assert detail["failure"]["successful_actions"] == 2


def test_local_step_has_no_default_inter_action_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr("pipeline.agent.time.sleep", sleeps.append)
    response = "Action: {'tool': 'inspect', 'tool_input': {}}"
    tool = FakeTool(feedback={"message": "ok", "status": True})
    agent, task, _, _ = make_local_agent(
        [response, "Action: {'tool': 'stop', 'tool_input': {'final_answer': 'done'}}"],
        tools=[tool],
    )

    agent.local_step(task)

    assert sleeps == []


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"local_model_max_attempts": True}, "local_model_max_attempts"),
        ({"local_model_max_attempts": 1.5}, "local_model_max_attempts"),
        ({"local_model_max_attempts": 0}, "local_model_max_attempts"),
        ({"local_model_max_actions": False}, "local_model_max_actions"),
        ({"local_model_max_actions": 2.5}, "local_model_max_actions"),
        ({"local_model_max_actions": 0}, "local_model_max_actions"),
        ({"local_model_inter_action_delay": True}, "local_model_inter_action_delay"),
        ({"local_model_inter_action_delay": float("nan")}, "local_model_inter_action_delay"),
        ({"local_model_inter_action_delay": 10 ** 1000}, "local_model_inter_action_delay"),
    ],
)
def test_base_agent_rejects_ambiguous_local_budget_configuration(config, message):
    with pytest.raises(ValueError, match=message):
        make_local_agent([], **config)


def test_step_accepts_cancellation_token_for_non_local_path():
    env = FakeEnv()
    agent = BaseAgent(
        llm=object(),
        env=env,
        data_manager=FakeDataManager(),
        name="Alice",
        silent=True,
    )
    task = Task("Inspect area", {"document": "public"})

    cancellation = threading.Event()
    cancellation.set()
    feedback, detail = agent.step(task, cancellation_token=cancellation)

    assert feedback["status"] is False
    assert detail["failure"]["reason"] == "cancelled"
    assert env.step_calls == 0


@pytest.mark.parametrize("creation_order", [(False, True), (True, False)])
def test_runtime_mode_is_scoped_to_each_agent(creation_order):
    agents = {}
    for running in creation_order:
        env = FakeEnv()
        env.running = running
        agents[running] = BaseAgent(
            llm=object(),
            env=env,
            data_manager=FakeDataManager(),
            name=f"Agent-{running}",
            silent=True,
        )

    task = Task("Inspect area", {"document": "public"})
    agents[True].normal_step = lambda current_task: ("real", current_task)
    agents[True].virtual_step = lambda current_task: ("virtual", current_task)
    agents[False].normal_step = lambda current_task: ("real", current_task)
    agents[False].virtual_step = lambda current_task: ("virtual", current_task)

    assert agents[True].step(task) == ("real", task)
    assert agents[False].step(task) == ("virtual", task)


def test_cancellation_capability_is_scoped_to_each_agent_mode():
    llm = object.__new__(VLLMLanguageModel)
    real_env = FakeEnv()
    virtual_env = FakeEnv()
    virtual_env.running = False

    real_agent = BaseAgent(llm, real_env, FakeDataManager(), "Real", silent=True)
    virtual_agent = BaseAgent(llm, virtual_env, FakeDataManager(), "Virtual", silent=True)

    assert real_agent.supports_cooperative_cancellation() is True
    assert virtual_agent.supports_cooperative_cancellation() is False


def test_all_tools_is_not_shared_between_agents_or_with_input():
    source_tools = [FakeTool()]
    first = BaseAgent(
        object(), FakeEnv(), FakeDataManager(), "First", silent=True, all_tools=source_tools
    )
    second = BaseAgent(
        object(), FakeEnv(), FakeDataManager(), "Second", silent=True, all_tools=source_tools
    )

    source_tools.append(FakeTool())
    first.all_tools.append(FakeTool())

    assert len(first.all_tools) == 2
    assert len(second.all_tools) == 1
