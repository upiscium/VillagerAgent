import threading
from types import SimpleNamespace

import pytest

from benchmarks.minecraft.k12_model import (
    K12ModelBudgetExhausted,
    K12ModelPolicy,
    K12ModelPolicyError,
    K12ModelPoisonedError,
    K12ModelReentryError,
    K12OpenAILanguageModel,
)
from env.runtime_paths import RuntimePaths
from model.openai_models import (
    OpenAILanguageModel,
    ProviderCallCancellationError,
    ProviderCallTerminationError,
)


class FakeClient:
    def __init__(self, *, policy=None, content="ok", error=None, chunks=None):
        self.policy = policy
        self.content = content
        self.error = error
        self.chunks = chunks
        self.calls = []
        self.closed = 0
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        if self.policy is not None:
            assert self.policy.used == 1
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if kwargs.get("stream"):
            values = self.chunks if self.chunks is not None else [self.content]
            return [SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content=value), finish_reason="stop" if index == len(values) - 1 else None,
            )]) for index, value in enumerate(values)]
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=self.content), finish_reason="stop",
        )])

    def close(self):
        self.closed += 1


@pytest.fixture()
def construction(monkeypatch):
    captured = []
    monkeypatch.setattr(
        "model.openai_models.OpenAI",
        lambda **kwargs: captured.append(kwargs) or FakeClient(),
    )
    return captured


def make_model(tmp_path, construction, *, budget=2, timeout=0.2, clock_ns=None):
    clock_ns = clock_ns or (lambda: 1000)
    policy = K12ModelPolicy("cell-1", model_call_budget=budget, clock_ns=clock_ns)
    model = K12OpenAILanguageModel(
        policy=policy,
        api_key="offline",
        api_model="offline-model",
        api_base="http://offline.invalid/v1",
        runtime_paths=RuntimePaths.isolated(tmp_path),
        request_timeout_seconds=timeout,
        prompt_logging_enabled=False,
    )
    return model, policy


def test_constructor_freezes_one_attempt_zero_delay_and_sdk_retries(tmp_path, construction):
    model, _ = make_model(tmp_path, construction)
    assert model.model_call_attempts == 1
    assert model.retry_delay_seconds == 0
    assert len(construction) == 1
    assert construction[0]["api_key"] == "offline"
    assert construction[0]["base_url"] == "http://offline.invalid/v1"
    assert construction[0]["max_retries"] == 0
    assert construction[0]["timeout"].read == 0.2


def test_nonstream_reserves_once_before_provider_callback(tmp_path, construction):
    model, policy = make_model(tmp_path, construction)
    client = FakeClient(policy=policy, content="answer")
    with policy.active():
        response = model.gpt_api([], "offline-model", 0, provider_client=client)
    assert response.choices[0].message.content == "answer"
    assert len(client.calls) == 1
    record = model.accounting[0]
    assert (record.order, record.admitted_monotonic_ns, record.terminal) == (1, 1000, "success")
    assert record.provider_started_count == 1


def test_stream_reserves_once_before_single_stream_create(tmp_path, construction):
    model, policy = make_model(tmp_path, construction)
    client = FakeClient(policy=policy, chunks=["a", "b"])
    with policy.active():
        content = model.gpt_api_stream([], "offline-model", 0, provider_client=client)
    assert content == "ab"
    assert len(client.calls) == 1 and client.calls[0]["stream"] is True
    assert policy.used == 1
    assert [record.terminal for record in model.accounting] == ["success"]


def test_nested_provider_entry_fails_before_second_callback(tmp_path, construction):
    model, policy = make_model(tmp_path, construction)
    second_calls = []

    def outer():
        with pytest.raises(K12ModelReentryError):
            model._bounded_provider_call(
                lambda: second_calls.append(1), FakeClient(),
            )
        return "outer"

    with policy.active():
        assert model._bounded_provider_call(outer, FakeClient()) == "outer"
    assert second_calls == []
    assert policy.used == 1
    assert len(model.accounting) == 1


def test_application_failure_has_no_retry(tmp_path, construction, monkeypatch):
    model, policy = make_model(tmp_path, construction)
    client = FakeClient(error=RuntimeError("provider failed"))
    monkeypatch.setattr(model, "_new_client", lambda: client)
    with policy.active(), pytest.raises(RuntimeError, match="provider failed"):
        model.controlled_planning("system", ["user"], stream=False)
    assert len(client.calls) == 1
    assert policy.used == 1
    assert [record.terminal for record in model.accounting] == ["error"]


def test_controlled_planning_forces_cache_off_and_temperature_zero(
    tmp_path, construction, monkeypatch,
):
    model, policy = make_model(tmp_path, construction)
    observed = {}

    def fake(self, *args, **kwargs):
        observed.update(kwargs)
        return "planned"

    monkeypatch.setattr(OpenAILanguageModel, "few_shot_generate_thoughts", fake)
    with policy.active():
        assert model.controlled_planning(
            "system", ["user"], cache_enabled=True, temperature=0.9,
        ) == "planned"
    assert observed["cache_enabled"] is False
    assert observed["temperature"] == 0.0


def test_inherited_public_generation_routes_cannot_bypass_policy(tmp_path, construction):
    model, policy = make_model(tmp_path, construction)
    with pytest.raises(K12ModelPolicyError):
        model.few_shot_generate_thoughts("system", ["user"], cache_enabled=True)
    with policy.active(), pytest.raises(K12ModelPolicyError, match="temperature"):
        model.gpt_api([], "offline-model", 0.5, provider_client=FakeClient())
    with policy.active(), pytest.raises(K12ModelPolicyError, match="image"):
        model.generate_with_image("prompt", object())


def test_provider_exception_terminalizes_exactly_once(tmp_path, construction):
    model, policy = make_model(tmp_path, construction)
    with policy.active(), pytest.raises(ValueError, match="bad response"):
        model._bounded_provider_call(
            lambda: (_ for _ in ()).throw(ValueError("bad response")), FakeClient(),
        )
    assert len(model.accounting) == 1
    assert model.accounting[0].terminal == "error"
    assert model.accounting[0].error_type == "ValueError"


def test_cancellation_preserves_base_exception_and_terminalizes(tmp_path, construction):
    model, policy = make_model(tmp_path, construction)
    cancelled = threading.Event(); cancelled.set()
    with policy.active(), pytest.raises(ProviderCallCancellationError) as raised:
        model._bounded_provider_call(lambda: None, FakeClient(), cancellation_event=cancelled)
    assert raised.value.provider_termination_confirmed is True
    record = model.accounting[0]
    assert record.terminal == "cancelled"
    assert record.provider_started_count == 0
    assert record.provider_termination_confirmed is True


def test_provider_hang_is_infrastructure_terminal(tmp_path, construction):
    model, policy = make_model(tmp_path, construction, timeout=0.01)
    release = threading.Event()
    try:
        with policy.active(), pytest.raises(ProviderCallTerminationError):
            model._bounded_provider_call(lambda: release.wait(2), FakeClient())
    finally:
        release.set()
    record = model.accounting[0]
    assert record.terminal == "infrastructure_failure"
    assert record.provider_termination_confirmed is False
    with policy.active(), pytest.raises(K12ModelPoisonedError):
        model._bounded_provider_call(lambda: "must-not-run", FakeClient())


def test_hanging_provider_close_is_bounded_and_poisons_model(tmp_path, construction):
    model, policy = make_model(tmp_path, construction, timeout=0.01)
    model._k12_close_timeout_seconds = 0.02
    close_release = threading.Event()
    provider = FakeClient()
    provider.close = lambda: close_release.wait(2)

    def slow_callback():
        threading.Event().wait(0.04)
        return "late"

    monotonic = __import__("time").monotonic
    before = monotonic()
    try:
        with policy.active(), pytest.raises(TimeoutError):
            model._bounded_provider_call(slow_callback, provider)
    finally:
        close_release.set()
    assert monotonic() - before < 0.5
    assert model.accounting[0].provider_termination_confirmed is False
    with policy.active(), pytest.raises(K12ModelPoisonedError):
        model._bounded_provider_call(lambda: None, FakeClient())


def test_missing_or_wrong_policy_fails_before_callback(tmp_path, construction):
    model, policy = make_model(tmp_path, construction)
    calls = []
    with pytest.raises(K12ModelPolicyError):
        model._bounded_provider_call(lambda: calls.append(1), FakeClient())
    other = K12ModelPolicy("other")
    with other.active(), pytest.raises(K12ModelPolicyError):
        model._bounded_provider_call(lambda: calls.append(1), FakeClient())
    assert calls == [] and policy.used == 0 and model.accounting == ()


def test_callback_composition_does_not_double_count(tmp_path, construction):
    model, policy = make_model(tmp_path, construction)
    started = []
    with policy.active():
        assert model._bounded_provider_call(
            lambda: "ok", FakeClient(), provider_started_callback=lambda: started.append(1),
        ) == "ok"
    assert started == [1]
    assert policy.used == 1
    assert model.accounting[0].provider_started_count == 1


def test_raising_started_callback_waits_for_provider_terminal_before_reopening(
    tmp_path, construction,
):
    model, policy = make_model(tmp_path, construction)
    release = threading.Event()

    def started():
        release.set()
        raise RuntimeError("started callback failed")

    with policy.active(), pytest.raises(RuntimeError, match="started callback failed"):
        model._bounded_provider_call(
            lambda: release.wait(0.2) or "done", FakeClient(),
            provider_started_callback=started,
        )
    assert model.accounting[0].terminal == "error"
    with policy.active():
        assert model._bounded_provider_call(lambda: "next", FakeClient()) == "next"


def test_unconfirmed_activity_poisons_all_models_sharing_policy(
    tmp_path, construction,
):
    first, policy = make_model(tmp_path / "first", construction, timeout=0.01)
    second = K12OpenAILanguageModel(
        policy=policy, api_key="offline", api_model="offline-model",
        api_base="http://offline.invalid/v1",
        runtime_paths=RuntimePaths.isolated(tmp_path / "second"),
        request_timeout_seconds=0.01, prompt_logging_enabled=False,
    )
    release = threading.Event()
    try:
        with policy.active(), pytest.raises(ProviderCallTerminationError):
            first._bounded_provider_call(lambda: release.wait(2), FakeClient())
    finally:
        release.set()
    assert policy.poisoned is True
    with policy.active(), pytest.raises(K12ModelPoisonedError):
        second._bounded_provider_call(lambda: "forbidden", FakeClient())


@pytest.mark.parametrize("value", [float("inf"), float("nan"), 0])
def test_close_timeout_must_be_finite_and_positive(tmp_path, construction, value):
    policy = K12ModelPolicy("cell")
    with pytest.raises(ValueError, match="finite and positive"):
        K12OpenAILanguageModel(
            policy=policy, close_timeout_seconds=value, api_key="offline",
            runtime_paths=RuntimePaths.isolated(tmp_path),
        )


def test_budget_exhaustion_fails_before_second_callback(tmp_path, construction):
    model, policy = make_model(tmp_path, construction, budget=1)
    second = []
    with policy.active():
        assert model._bounded_provider_call(lambda: "first", FakeClient()) == "first"
        with pytest.raises(K12ModelBudgetExhausted):
            model._bounded_provider_call(lambda: second.append(1), FakeClient())
    assert second == [] and len(model.accounting) == 1


def test_production_model_method_is_not_replaced(tmp_path, construction):
    model, _ = make_model(tmp_path, construction)
    assert type(model)._bounded_provider_call is K12OpenAILanguageModel._bounded_provider_call
    assert OpenAILanguageModel._bounded_provider_call is not K12OpenAILanguageModel._bounded_provider_call
