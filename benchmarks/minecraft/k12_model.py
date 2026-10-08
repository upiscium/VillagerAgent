"""K12-owned exactly-once accounting around the unchanged OpenAI model."""
from __future__ import annotations

import threading
import time
import math
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from model.openai_models import (
    OpenAILanguageModel,
    ProviderCallCancellationError,
    ProviderCallTerminationError,
)
from benchmarks.minecraft.k12_guarded_backend import K12AuthenticatedProfile

_ACTIVE_POLICY: ContextVar[Any] = ContextVar("k12_model_policy", default=None)


class K12ModelPolicyError(RuntimeError):
    pass


class K12ModelBudgetExhausted(RuntimeError):
    pass


class K12ModelReentryError(RuntimeError):
    pass


class K12ModelPoisonedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModelCallRecord:
    call_id: str
    order: int
    admitted_monotonic_ns: int
    terminal: str | None
    error_type: str | None
    provider_started_count: int
    provider_termination_confirmed: bool | None


class K12ModelPolicy:
    """One lock-protected, identity-exact K12 model-call budget."""

    def __init__(self, cell_id: str, *, model_call_budget: int = 2,
                 clock_ns: Callable[[], int] = time.monotonic_ns) -> None:
        if not isinstance(cell_id, str) or not cell_id:
            raise ValueError("cell_id is required")
        if type(model_call_budget) is not int or model_call_budget < 0:
            raise ValueError("model_call_budget must be a non-negative integer")
        self.cell_id = cell_id
        self.model_call_budget = model_call_budget
        self._clock_ns = clock_ns
        self._lock = threading.RLock()
        self._used = 0
        self._inflight = False
        self._poisoned = False

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def poisoned(self) -> bool:
        with self._lock:
            return self._poisoned

    def assert_available(self) -> None:
        with self._lock:
            if self._poisoned:
                raise K12ModelPoisonedError("K12 policy has unconfirmed provider activity")
            if self._inflight:
                raise K12ModelReentryError("nested K12 provider entry is forbidden")

    def begin_call(self) -> tuple[int, int]:
        with self._lock:
            if self._poisoned:
                raise K12ModelPoisonedError("K12 policy has unconfirmed provider activity")
            if self._inflight:
                raise K12ModelReentryError("nested K12 provider entry is forbidden")
            if self._used >= self.model_call_budget:
                raise K12ModelBudgetExhausted("K12 model-call budget exhausted")
            admitted_ns = int(self._clock_ns())
            self._used += 1
            self._inflight = True
            return self._used, admitted_ns

    def complete_call(self, *, termination_confirmed: bool | None) -> None:
        with self._lock:
            if not self._inflight:
                raise RuntimeError("K12 model policy has no active call")
            if termination_confirmed is False:
                self._poisoned = True
            else:
                self._inflight = False

    def activate(self) -> Token:
        if _ACTIVE_POLICY.get() is not None:
            raise K12ModelPolicyError("a K12 model policy is already active")
        return _ACTIVE_POLICY.set(self)

    def restore(self, token: Token) -> None:
        if _ACTIVE_POLICY.get() is not self:
            raise K12ModelPolicyError("the exact K12 model policy is not active")
        _ACTIVE_POLICY.reset(token)

    @contextmanager
    def active(self) -> Iterator["K12ModelPolicy"]:
        token = self.activate()
        try:
            yield self
        finally:
            self.restore(token)


class K12OpenAILanguageModel(OpenAILanguageModel):
    """Single-attempt OpenAI-compatible model owned only by a K12 worker.

    Both inherited stream and non-stream paths are qualified. Cache use and
    nonzero temperature are disabled by :meth:`controlled_planning`.
    """

    def __init__(self, *, policy: K12ModelPolicy, close_timeout_seconds: float = 0.1,
                 provider_client: Any | None = None,
                 **kwargs: Any) -> None:
        if not isinstance(policy, K12ModelPolicy):
            raise TypeError("a typed K12ModelPolicy is required")
        if not math.isfinite(close_timeout_seconds) or close_timeout_seconds <= 0:
            raise ValueError("close_timeout_seconds must be finite and positive")
        # This hook is deliberately K12-only.  It lets the sealed live fixture
        # install a transport without constructing an OpenAI client (and hence
        # without putting an endpoint or credential in the provider worker).
        self._k12_provider_client = provider_client
        kwargs["model_call_attempts"] = 1
        kwargs["retry_delay_seconds"] = 0
        super().__init__(**kwargs)
        self._k12_policy = policy
        self._k12_close_timeout_seconds = float(close_timeout_seconds)
        self._k12_state_lock = threading.RLock()
        self._k12_records: list[dict[str, Any]] = []

    def _new_client(self):
        if self._k12_provider_client is not None:
            return self._k12_provider_client
        return super()._new_client()

    @property
    def accounting(self) -> tuple[ModelCallRecord, ...]:
        with self._k12_state_lock:
            return tuple(ModelCallRecord(**dict(record)) for record in self._k12_records)

    def _require_policy(self) -> None:
        if _ACTIVE_POLICY.get() is not self._k12_policy:
            raise K12ModelPolicyError("exact active K12 model policy is required")

    def controlled_planning(self, system_prompt: str = "", example_prompt: Any = (),
                             **kwargs: Any) -> str:
        self._require_policy()
        kwargs["cache_enabled"] = False
        kwargs["temperature"] = 0.0
        return super().few_shot_generate_thoughts(
            system_prompt=system_prompt,
            example_prompt=list(example_prompt) if isinstance(example_prompt, tuple) else example_prompt,
            **kwargs,
        )

    def few_shot_generate_thoughts(self, system_prompt: str = "", example_prompt: Any = (),
                                   **kwargs: Any) -> str:
        return self.controlled_planning(system_prompt, example_prompt, **kwargs)

    @staticmethod
    def _require_zero_temperature(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        temperature = args[2] if len(args) > 2 else kwargs.get("temperature")
        if temperature != 0 and temperature != 0.0:
            raise K12ModelPolicyError("K12 provider temperature must be zero")

    def gpt_api(self, *args: Any, **kwargs: Any):
        self._require_policy()
        self._require_zero_temperature(args, kwargs)
        return super().gpt_api(*args, **kwargs)

    def gpt_api_stream(self, *args: Any, **kwargs: Any):
        self._require_policy()
        self._require_zero_temperature(args, kwargs)
        return super().gpt_api_stream(*args, **kwargs)

    def generate_with_image(self, *args: Any, **kwargs: Any):
        raise K12ModelPolicyError("image generation is outside K12 /1")

    def _bounded_provider_call(self, callback, provider_client,
                               cancellation_event=None,
                               provider_started_callback=None):
        # Reentry is checked first because the provider callback executes in a
        # fresh thread where ContextVars intentionally do not propagate.
        with self._k12_state_lock:
            self._k12_policy.assert_available()
            self._require_policy()
            order, admitted_ns = self._k12_policy.begin_call()
            record = {
                "call_id": f"{self._k12_policy.cell_id}:model:{order}",
                "order": order,
                "admitted_monotonic_ns": admitted_ns,
                "terminal": None,
                "error_type": None,
                "provider_started_count": 0,
                "provider_termination_confirmed": None,
            }
            self._k12_records.append(record)

        callback_errors: list[BaseException] = []
        def provider_started() -> None:
            with self._k12_state_lock:
                record["provider_started_count"] += 1
            if provider_started_callback is not None:
                try:
                    provider_started_callback()
                except BaseException as error:
                    # The unchanged base starts its worker before this callback.
                    # Retain the error until that worker has reached a terminal
                    # outcome so a callback cannot reopen concurrent activity.
                    callback_errors.append(error)

        terminal = "error"
        error: BaseException | None = None
        close_proxy = _BoundedCloseProxy(
            provider_client, timeout_seconds=self._k12_close_timeout_seconds,
        )
        try:
            result = super()._bounded_provider_call(
                callback,
                close_proxy,
                cancellation_event=cancellation_event,
                provider_started_callback=provider_started,
            )
            if callback_errors:
                raise callback_errors[0]
        except BaseException as caught:
            error = caught
            if isinstance(caught, ProviderCallCancellationError):
                terminal = "cancelled"
                confirmed = (caught.provider_termination_confirmed
                             and close_proxy.close_confirmed is not False)
            elif isinstance(caught, (ProviderCallTerminationError, TimeoutError)):
                terminal = "infrastructure_failure"
                confirmed = (not isinstance(caught, ProviderCallTerminationError)
                             and close_proxy.close_confirmed is not False)
            else:
                confirmed = None
            raise
        else:
            terminal = "success"
            confirmed = True
            return result
        finally:
            with self._k12_state_lock:
                if record["terminal"] is not None:
                    raise RuntimeError("K12 model call terminalized more than once")
                record["terminal"] = terminal
                record["error_type"] = type(error).__name__ if error is not None else None
                record["provider_termination_confirmed"] = confirmed
                self._k12_policy.complete_call(termination_confirmed=confirmed)


class _BoundedCloseProxy:
    """Delegate provider access while bounding the base model's close call."""

    def __init__(self, provider: Any, *, timeout_seconds: float) -> None:
        self._provider = provider
        self._timeout_seconds = timeout_seconds
        self.close_confirmed: bool | None = None
        self.close_error: BaseException | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    def close(self) -> None:
        close = getattr(self._provider, "close", None)
        if not callable(close):
            self.close_confirmed = True
            return
        done = threading.Event()

        def invoke() -> None:
            try:
                close()
            except BaseException as error:
                self.close_error = error
            finally:
                done.set()

        threading.Thread(target=invoke, name="k12-provider-close", daemon=True).start()
        self.close_confirmed = done.wait(self._timeout_seconds) and self.close_error is None


class K12LiveProviderPolicy(K12ModelPolicy):
    """The live-mock contract: one admission and one terminal outcome."""
    def __init__(self, cell_id: str, *, clock_ns: Callable[[], int] = time.monotonic_ns) -> None:
        super().__init__(cell_id, model_call_budget=1, clock_ns=clock_ns)


@dataclass(frozen=True, slots=True)
class K12LiveProviderConfig:
    profile_id: str
    profile_digest: str
    campaign_id: str
    cell_id: str


class K12ScriptedMockTransport:
    """Concrete deterministic transport; it has no endpoint or credentials."""
    __slots__ = ("__responses", "__index", "__closed", "__error",
                 "__delay_seconds", "__close_delay_seconds", "__close_error")

    def __init__(self, responses: tuple[str, ...] = ("ok",), *,
                 error: BaseException | None = None, delay_seconds: float = 0,
                 close_delay_seconds: float = 0,
                 close_error: BaseException | None = None) -> None:
        if (not isinstance(responses, tuple) or not responses
                or any(type(item) is not str for item in responses)):
            raise TypeError("scripted mock responses must be a non-empty tuple of strings")
        if any(not math.isfinite(float(value)) or float(value) < 0
               for value in (delay_seconds, close_delay_seconds)):
            raise ValueError("mock delays must be finite and non-negative")
        self.__responses, self.__index, self.__closed = responses, 0, False
        self.__error = error
        self.__delay_seconds = float(delay_seconds)
        self.__close_delay_seconds = float(close_delay_seconds)
        self.__close_error = close_error

    def complete(self, prompt: str) -> str:
        if self.__closed:
            raise K12ModelPoisonedError("mock transport is closed")
        if self.__delay_seconds:
            time.sleep(self.__delay_seconds)
        if self.__error is not None:
            raise self.__error
        response = self.__responses[min(self.__index, len(self.__responses) - 1)]
        self.__index += 1
        return response

    def close(self) -> None:
        if self.__close_delay_seconds:
            time.sleep(self.__close_delay_seconds)
        if self.__close_error is not None:
            raise self.__close_error
        self.__closed = True


class _K12MockClient:
    """Minimal OpenAI-shaped client; only the K12 transport is reachable."""
    __slots__ = ("chat", "_transport")

    def __init__(self, transport: K12ScriptedMockTransport) -> None:
        self._transport = transport
        self.chat = type("_Chat", (), {})()
        self.chat.completions = type("_Completions", (), {"create": self._create})()

    def _create(self, **kwargs: Any) -> Any:
        if kwargs.get("stream"):
            raise K12ModelPolicyError("streaming is forbidden for the live K12 mock")
        content = self._transport.complete(str(kwargs.get("messages", "")))
        choice = type("_Choice", (), {})()
        choice.message = type("_Message", (), {"content": content})()
        choice.finish_reason = "stop"
        return type("_Completion", (), {"choices": [choice]})()

    def close(self) -> None:
        self._transport.close()


class K12LiveProviderFactory:
    """Sealed, mock-only provider adapter.

    ``endpoint`` and ``key_resolver`` are intentionally not accepted: there is
    no path from this issue to an OpenAI client, network, or secret resolver.
    """
    def __init__(self, *, profile: Any, config: K12LiveProviderConfig,
                 transport: K12ScriptedMockTransport,
                 clock_ns: Callable[[], int] = time.monotonic_ns) -> None:
        if not isinstance(profile, K12AuthenticatedProfile):
            try:
                profile = K12AuthenticatedProfile.from_runtime_profile(profile)
            except TypeError:
                pass
        if not isinstance(profile, K12AuthenticatedProfile):
            raise K12ModelPolicyError("loader-issued typed profile required")
        if (not isinstance(config, K12LiveProviderConfig)
                 or type(transport) is not K12ScriptedMockTransport
                 or profile.profile_id != config.profile_id
                 or profile.profile_digest != config.profile_digest
                 or not all(isinstance(value, str) and value for value in
                            (config.profile_id, config.profile_digest,
                             config.campaign_id, config.cell_id))):
            raise K12ModelPolicyError("profile/config binding mismatch")
        self.__policy = K12LiveProviderPolicy(config.cell_id, clock_ns=clock_ns)
        self.__transport = transport
        self.__model = K12OpenAILanguageModel(
            policy=self.__policy,
            provider_client=_K12MockClient(transport),
            api_key="k12-sealed-mock",
            api_base="k12-sealed-mock",
            api_model="k12-sealed-mock",
            request_timeout_seconds=120,
            prompt_logging_enabled=False,
        )

    @property
    def accounting(self) -> tuple[ModelCallRecord, ...]:
        return self.__model.accounting

    def __getattr__(self, name: str) -> Any:
        raise K12ModelPolicyError("sealed provider exposes no underlying attributes")

    def plan(self, system_prompt: str = "", example_prompt: Any = (), **kwargs: Any) -> str:
        allowed = {"temperature", "cache_enabled", "stream", "image", "retries",
                   "attempts", "request_timeout_seconds", "connect_timeout_seconds",
                   "cancellation_event"}
        if set(kwargs) - allowed:
            raise K12ModelPolicyError("provider configuration overrides are forbidden")
        expected = {"temperature": 0.0, "cache_enabled": False, "stream": False,
                    "image": False, "retries": 0, "attempts": 1,
                    "request_timeout_seconds": 120, "connect_timeout_seconds": 5}
        if any(kwargs.get(key, value) != value for key, value in expected.items()):
            raise K12ModelPolicyError("K12 provider policy is fixed")
        # Make the sealed adapter's non-streaming choice explicit before it
        # enters the unchanged model generation loop.
        kwargs.setdefault("stream", False)
        call_kwargs = {
            key: value for key, value in kwargs.items()
            if key in {"temperature", "cache_enabled", "stream", "cancellation_event"}
        }
        with self.__policy.active():
            try:
                return self.__model.controlled_planning(
                    system_prompt, example_prompt, **call_kwargs,
                )
            except K12ModelPoisonedError as error:
                raise K12ModelPoisonedError("K12 live provider stop: uncertain termination") from error


def make_k12_live_provider(**kwargs: Any) -> K12LiveProviderFactory:
    return K12LiveProviderFactory(**kwargs)


__all__ = [
    "K12ModelBudgetExhausted",
    "K12ModelPolicy",
    "K12ModelPolicyError",
    "K12ModelPoisonedError",
    "K12ModelReentryError",
    "K12OpenAILanguageModel",
    "ModelCallRecord",
    "K12LiveProviderFactory",
    "K12LiveProviderPolicy",
    "K12LiveProviderConfig",
    "K12ScriptedMockTransport",
    "make_k12_live_provider",
]
