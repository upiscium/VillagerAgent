import threading
import time
import pytest
from benchmarks.minecraft.k12_model import (K12LiveProviderConfig, K12LiveProviderFactory,
    K12LiveProviderPolicy, K12ModelPolicyError, K12ModelPoisonedError,
    K12OpenAILanguageModel, K12ScriptedMockTransport)
from env.runtime_paths import RuntimePaths
from model.openai_models import ProviderCallCancellationError, ProviderCallTerminationError
from benchmarks.minecraft.k12_guarded_backend import K12AuthenticatedProfile
from benchmarks.minecraft.k12_runtime_profile import load_k12_live_runtime_profile

@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path,monkeypatch):
    monkeypatch.setenv("VILLAGER_RUNTIME_ROOT",str(tmp_path))
    monkeypatch.setenv("VILLAGER_RUNTIME_LAYOUT","isolated")

def test_sealed_live_provider_is_mockable_and_nonstreaming():
    loaded = load_k12_live_runtime_profile()
    profile = K12AuthenticatedProfile.from_runtime_profile(loaded)
    config = K12LiveProviderConfig(profile.profile_id, profile.profile_digest, "campaign", "cell")
    provider = K12LiveProviderFactory(profile=profile, config=config,
                                      transport=K12ScriptedMockTransport(("ok",)))
    assert provider.plan("system", []) == "ok"
    assert len(provider.accounting) == 1 and provider.accounting[0].terminal == "success"
    with pytest.raises(K12ModelPolicyError): provider.client

def test_provider_rejects_overrides_reentry_and_transport_subclasses():
    loaded = load_k12_live_runtime_profile()
    profile = K12AuthenticatedProfile.from_runtime_profile(loaded)
    config = K12LiveProviderConfig(profile.profile_id, profile.profile_digest, "campaign", "cell")
    transport = K12ScriptedMockTransport(("ok",))
    provider = K12LiveProviderFactory(profile=profile, config=config, transport=transport)
    with pytest.raises(K12ModelPolicyError, match="fixed"):
        provider.plan(stream=True)
    assert provider.plan() == "ok"
    with pytest.raises(Exception, match="budget"):
        provider.plan()
    class Unsafe(K12ScriptedMockTransport): pass
    with pytest.raises(K12ModelPolicyError):
        K12LiveProviderFactory(profile=profile, config=config, transport=Unsafe())


def _provider(transport):
    loaded = load_k12_live_runtime_profile()
    profile = K12AuthenticatedProfile.from_runtime_profile(loaded)
    config = K12LiveProviderConfig(profile.profile_id, profile.profile_digest, "campaign", "cell")
    return K12LiveProviderFactory(profile=profile, config=config, transport=transport)


def test_live_terminal_accounting_is_once_for_provider_exception():
    provider = _provider(K12ScriptedMockTransport(error=ValueError("provider")))
    with pytest.raises(ValueError, match="provider"):
        provider.plan()
    assert len(provider.accounting) == 1
    assert provider.accounting[0].terminal == "error"


def test_live_timeout_poisons_and_rejects_stop():
    provider = _provider(K12ScriptedMockTransport(error=ProviderCallTerminationError("timeout")))
    with pytest.raises(ProviderCallTerminationError):
        provider.plan()
    assert len(provider.accounting) == 1
    assert provider.accounting[0].provider_termination_confirmed is False
    with pytest.raises(K12ModelPoisonedError):
        provider.plan()


def test_shared_model_boundary_enforces_real_wall_clock_timeout(tmp_path):
    policy=K12LiveProviderPolicy("deadline-cell")
    transport=K12ScriptedMockTransport(delay_seconds=0.2)
    client=type("Client",(),{"close":transport.close})()
    model=K12OpenAILanguageModel(policy=policy,provider_client=client,api_key="sealed",
        api_base="sealed",api_model="sealed",request_timeout_seconds=0.01,
        runtime_paths=RuntimePaths.isolated(tmp_path),prompt_logging_enabled=False)
    with policy.active(),pytest.raises(TimeoutError):
        model._bounded_provider_call(lambda: time.sleep(0.2),client)
    assert model.accounting[0].terminal=="infrastructure_failure"
    assert model.accounting[0].provider_termination_confirmed is True


def test_live_cancellation_has_one_terminal_record():
    event = threading.Event()
    provider = _provider(K12ScriptedMockTransport(delay_seconds=0.2))
    def cancel():
        time.sleep(0.01)
        event.set()
    threading.Thread(target=cancel, daemon=True).start()
    with pytest.raises(ProviderCallCancellationError):
        provider.plan(cancellation_event=event)
    assert len(provider.accounting) == 1
    assert provider.accounting[0].terminal == "cancelled"


def test_live_uncertain_close_poisons_and_rejects_stop():
    provider = _provider(K12ScriptedMockTransport(
        delay_seconds=0.2, close_error=RuntimeError("close uncertain"),
    ))
    event = threading.Event()
    threading.Timer(0.01, event.set).start()
    with pytest.raises(ProviderCallCancellationError):
        provider.plan(cancellation_event=event)
    assert len(provider.accounting) == 1
    assert provider.accounting[0].provider_termination_confirmed is False
    with pytest.raises(K12ModelPoisonedError):
        provider.plan()


def test_live_uncertain_worker_termination_poisons_and_rejects_stop():
    provider = _provider(K12ScriptedMockTransport(error=ProviderCallTerminationError("unconfirmed")))
    with pytest.raises(ProviderCallTerminationError):
        provider.plan()
    assert provider.accounting[0].provider_termination_confirmed is False
    with pytest.raises(K12ModelPoisonedError):
        provider.plan()
