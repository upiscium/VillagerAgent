import logging
import math
import os
import time
from openai import OpenAI
import tiktoken
from model.abstract_language_model import AbstractLanguageModel
import json
import random
import threading
import queue
import httpx
import base64
import hashlib

from env.runtime_paths import RuntimePaths, atomic_write_json
from model.utils import extract_info

logging.basicConfig(
    level=logging.ERROR, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)
_OPENAI_STATE_LOCK = threading.RLock()
DEFAULT_OPENAI_REQUEST_TIMEOUT_SECONDS = 120.0
DEFAULT_OPENAI_MODEL_CALL_ATTEMPTS = 3
DEFAULT_OPENAI_RETRY_DELAY_SECONDS = 1.0


class ModelOutputContractError(ValueError):
    """A successful provider response that fails the required output contract."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


class ProviderCallTerminationError(TimeoutError):
    """A timed-out provider call whose worker could not be stopped cooperatively."""


class ProviderCallCancellationError(InterruptedError):
    """A provider call interrupted by its caller, with bounded-stop evidence."""

    def __init__(self, message, *, provider_termination_confirmed, close_failure_diagnostics=None):
        super().__init__(message)
        self.provider_termination_confirmed = bool(provider_termination_confirmed)
        self.close_failure_diagnostics = close_failure_diagnostics or {}
        self.diagnostics = self.close_failure_diagnostics

def _contains_tag(content: str, tag: str) -> bool:
    def normalize(value: str) -> str:
        return " ".join(value.casefold().replace("_", " ").replace("-", " ").split())

    return normalize(tag) in normalize(content)


class OpenAILanguageModel(AbstractLanguageModel):
    _supported_models = ["gpt-4o-mini", "gpt-4o", "gpt-4-0125-preview", "gpt-4-1106-preview", "gpt-4", "gpt-4-0314", "gpt-4-0613", "gpt-4-32k", "gpt-4-32k-0314",
                         "gpt-4-32k-0613", "gpt-3.5-turbo", "gpt-3.5-turbo-16k", "gpt-3.5-turbo-0301",
                         "gpt-3.5-turbo-0613", "gpt-3.5-turbo-1106", "gpt-3.5-turbo-16k-0613", "gpt-3.5-turbo-instruct"]
    
    # def __init__(self, api_key="", api_model="gpt-3.5-turbo-1106", evaluation_strategy="value", api_base="https://api.openai.com/v1/",
    #              enable_ReAct_prompting=True, strategy="cot", role_name="", api_key_list=None):
    def __init__(self, api_key="", api_model="qwen-max", evaluation_strategy="value", api_base="https://api.chatanywhere.tech/v1",
                 enable_ReAct_prompting=True, strategy="cot", role_name="", api_key_list=None,
                 runtime_paths: RuntimePaths | None = None,
                 request_timeout_seconds: float = DEFAULT_OPENAI_REQUEST_TIMEOUT_SECONDS,
                 model_call_attempts: int = DEFAULT_OPENAI_MODEL_CALL_ATTEMPTS,
                 retry_delay_seconds: float = DEFAULT_OPENAI_RETRY_DELAY_SECONDS,
                 prompt_logging_enabled: bool = True,
                 reasoning_effort: str | None = None):
        self.runtime_paths = runtime_paths or RuntimePaths.from_environment()
        if api_key == "" or api_key is None:
            api_key = os.environ.get("OPENAI_API_KEY", "")
        if api_key == "":
            raise Exception("Please provide OpenAI API key")
        self.api_key = api_key
        request_timeout_seconds = float(request_timeout_seconds)
        retry_delay_seconds = float(retry_delay_seconds)
        if not math.isfinite(request_timeout_seconds) or request_timeout_seconds <= 0:
            raise ValueError("OpenAI request timeout must be positive")
        if type(model_call_attempts) is not int or model_call_attempts < 1:
            raise ValueError("OpenAI model call attempts must be a positive integer")
        if not math.isfinite(retry_delay_seconds) or retry_delay_seconds < 0:
            raise ValueError("OpenAI retry delay must be non-negative")
        self.request_timeout_seconds = request_timeout_seconds
        self.model_call_attempts = model_call_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.prompt_logging_enabled = bool(prompt_logging_enabled)
        if reasoning_effort not in {None, "high", "medium", "low", "max", "none"}:
            raise ValueError("unsupported reasoning effort")
        self.reasoning_effort = reasoning_effort
        self._response_metadata = threading.local()

        if api_key_list:
            self.api_key_list = list(set(api_key_list))
        else:
            self.api_key_list = [api_key]

        if api_base == "" or api_base is None:
            api_base = os.environ.get(
                "OPENAI_API_BASE", ""
            )  # if not set, use the default base path of "https://api.openai.com/v1"
        self.api_base = api_base
        if api_model == "" or api_model is None:
            api_model = os.environ.get("OPENAI_API_MODEL", "")
        if api_model != "":
            # if api_model not in OpenAILanguageModel._supported_models:
            #     raise Exception(
            #         f"only support {OpenAILanguageModel._supported_models}, but got {api_model}"
            #     )

            self.api_model = api_model
        else:
            self.api_model = "qwen-max"
        # logger.info(f"Using api_model {self.api_model}")

        self.use_chat_api = True
        self.role_name = role_name

        # reference : https://www.promptingguide.ai/techniques/react
        self.ReAct_prompt = ""
        if enable_ReAct_prompting:
            self.ReAct_prompt = "Write down your observations in format 'Observation:xxxx', then write down your thoughts in format 'Thoughts:xxxx'."

        self.strategy = strategy
        self.evaluation_strategy = evaluation_strategy

        self.client = self._new_client()

        with _OPENAI_STATE_LOCK:
            self.runtime_paths.data_dir.mkdir(parents=True, exist_ok=True)
            self.runtime_paths.cache_dir.mkdir(parents=True, exist_ok=True)
            if not self.runtime_paths.openai_log.exists():
                with self.runtime_paths.openai_log.open("w") as log_file:
                    log_file.write("")
            if not self.runtime_paths.tokens.exists():
                atomic_write_json(self.runtime_paths.tokens, {
                    "dates": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                    "tokens_used": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "successful_requests": 0,
                    "total_cost": 0,
                    "action_cost": 0,
                })

    def generate_thoughts(self, state, k):
        pass

    def evaluate_states(self, states):
        pass

    def _new_client(self):
        return OpenAI(
            api_key=random.choice(self.api_key_list) if self.api_key_list else self.api_key,
            base_url=self.api_base,
            max_retries=0,
            timeout=httpx.Timeout(self.request_timeout_seconds, connect=5.0),
        )

    def _diagnostic_base(self, *, attempt: int, model: str, stream: bool) -> dict:
        endpoint_hash = hashlib.sha256(str(self.api_base).encode("utf-8")).hexdigest()
        return {
            "schema_version": "openai-model-diagnostic/1",
            "attempt": attempt,
            "model": model,
            "stream": stream,
            "endpoint_sha256": endpoint_hash,
        }

    def _write_diagnostic(self, value: dict) -> None:
        path = self.runtime_paths.openai_diagnostics
        with _OPENAI_STATE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(value, ensure_ascii=True, sort_keys=True) + "\n")

    def _bounded_provider_call(
        self,
        callback,
        provider_client,
        cancellation_event=None,
        provider_started_callback=None,
    ):
        if cancellation_event is not None and cancellation_event.is_set():
            raise ProviderCallCancellationError(
                "OpenAI provider call was cancelled before start",
                provider_termination_confirmed=True,
                close_failure_diagnostics={"phase": "before_start"},
            )
        outcome = queue.Queue(maxsize=1)

        def invoke():
            try:
                outcome.put((True, callback()))
            except BaseException as exc:
                outcome.put((False, exc))

        worker = threading.Thread(target=invoke, name="openai-provider-call", daemon=True)
        worker.start()
        if provider_started_callback is not None:
            provider_started_callback()
        deadline = time.monotonic() + self.request_timeout_seconds
        succeeded = None
        value = None
        while True:
            if cancellation_event is not None and cancellation_event.is_set():
                cancel_deadline = time.monotonic() + 1.0
                diagnostics = {}
                close = getattr(provider_client, "close", None)
                if callable(close):
                    close_done = threading.Event()
                    close_error = []

                    def close_provider():
                        try:
                            close()
                        except BaseException as exc:  # diagnostics must not mask cancellation
                            close_error.append(exc)
                        finally:
                            close_done.set()

                    threading.Thread(target=close_provider, name="openai-provider-close", daemon=True).start()
                    remaining = max(0.0, cancel_deadline - time.monotonic())
                    close_done.wait(remaining)
                    if close_error:
                        diagnostics["close_error_type"] = type(close_error[0]).__name__
                    elif not close_done.is_set():
                        diagnostics["close_pending"] = True
                remaining = max(0.0, cancel_deadline - time.monotonic())
                worker.join(remaining)
                if worker.is_alive():
                    diagnostics["worker_pending"] = True
                confirmed = (
                    not worker.is_alive()
                    and not diagnostics.get("close_pending")
                    and "close_error_type" not in diagnostics
                )
                raise ProviderCallCancellationError(
                    "OpenAI provider call was cancelled",
                    provider_termination_confirmed=confirmed,
                    close_failure_diagnostics=diagnostics,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                succeeded, value = outcome.get(timeout=min(remaining, 0.05))
                break
            except queue.Empty:
                continue
        if succeeded is None:
            close = getattr(provider_client, "close", None)
            if callable(close):
                close()
            worker.join(timeout=1.0)
            if worker.is_alive():
                raise ProviderCallTerminationError(
                    "OpenAI request worker remained active after its wall-clock budget"
                )
            raise TimeoutError("OpenAI request exceeded the model-call wall-clock budget")
        if not succeeded:
            raise value
        return value

    @staticmethod
    def _transport_metadata(exc: Exception) -> dict:
        status_code = getattr(exc, "status_code", None)
        response = getattr(exc, "response", None)
        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)
        return {
            "outcome": "transport_failure",
            "error_type": type(exc).__name__,
            "http_status": status_code if isinstance(status_code, int) else None,
        }

    def cache_api_call_handler(self, prompt, max_tokens, temperature, k=1, stop=None):
        cache_path = self.runtime_paths.openai_cache
        with _OPENAI_STATE_LOCK:
            if cache_path.exists():
                with cache_path.open("r") as cache_file:
                    cache = json.load(cache_file)
            else:
                cache = {}

        if prompt in cache:
            return cache[prompt]
        else:
            return None

    def save_cache(self, prompt, response):
        cache_path = self.runtime_paths.openai_cache
        with _OPENAI_STATE_LOCK:
            if cache_path.exists():
                with cache_path.open("r") as cache_file:
                    cache = json.load(cache_file)
            else:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache = {}
            cache[prompt] = response
            atomic_write_json(cache_path, cache)


    def update_token_usage(self, prompt_tokens, completion_tokens):
        with _OPENAI_STATE_LOCK:
            with self.runtime_paths.tokens.open("r") as token_file:
                tokens = json.load(token_file)
            tokens["dates"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            tokens["tokens_used"] += prompt_tokens + completion_tokens
            tokens["prompt_tokens"] += prompt_tokens
            tokens["completion_tokens"] += completion_tokens
            tokens["successful_requests"] += 1
            if self.api_model == "gpt-3.5-turbo":
                tokens["total_cost"] += 0.001 * prompt_tokens * 0.001 + 0.0002 * completion_tokens * 0.001
            if self.api_model == "gpt-4":
                tokens["total_cost"] += 0.03 * prompt_tokens * 0.001 + 0.06 * completion_tokens * 0.001
            if self.api_model == "gpt-4-turbo":
                tokens["total_cost"] += 0.01 * prompt_tokens * 0.001 + 0.03 * completion_tokens * 0.001
            atomic_write_json(self.runtime_paths.tokens, tokens)

    def guard_token_number(self, messages, encoding_name, max_output_tokens=2048) -> [str]:
        text = "".join([message["content"] for message in messages])
        num_tokens = self.num_tokens_from_string(text, encoding_name)
        # logger.info(f"api {encoding_name} num_tokens {num_tokens}")
        if self.api_model == "gpt-4-1106-preview" or self.api_model == "gpt-4-0125-preview":
            if num_tokens >= 1024 * 128 - max_output_tokens:
                logger.warning(f"num_tokens {num_tokens} auto resize waiting please")
            return self.resizing_token(1024 * 128 - max_output_tokens, encoding_name, messages)
        elif self.api_model == "gpt-4" or self.api_model == "gpt-4o-mini":
            if num_tokens >= 1024 * 8 - max_output_tokens:
                logger.warning(f"num_tokens {num_tokens} auto resize waiting please")
            return self.resizing_token(1024 * 8 - max_output_tokens, "gpt-4", messages)
        elif self.api_model == "gpt-4-32k":
            if num_tokens >= 1024 * 32 - max_output_tokens:
                logger.warning(f"num_tokens {num_tokens} auto resize waiting please")
            return self.resizing_token(1024 * 32 - max_output_tokens, encoding_name, messages)
        elif self.api_model == "gpt-3.5-turbo-16k" or self.api_model == "gpt-3.5-turbo-1106":
            if num_tokens >= 1024 * 16 - max_output_tokens:
                logger.warning(f"num_tokens {num_tokens} auto resize waiting please")
            return self.resizing_token(1024 * 16 - max_output_tokens, encoding_name, messages)
        elif self.api_model == "gpt-3.5-turbo-instruct":
            if num_tokens >= 1024 * 4 - max_output_tokens:
                logger.warning(f"num_tokens {num_tokens} auto resize waiting please")
            return self.resizing_token(1024 * 4 - max_output_tokens, encoding_name, messages)
        else:
            logger.warning(f"num_tokens {num_tokens} auto resize waiting please")
            return self.resizing_token(1024 * 8 - max_output_tokens, encoding_name, messages)

    def resizing_token(self, target_text_num, encoding_name, messages: [str]) -> [str]:
        while True:
            text = "".join([message["content"] for message in messages])
            num_tokens = self.num_tokens_from_string(text, encoding_name)
            if num_tokens > target_text_num:
                if len(messages[-1]["content"]) < 100:
                    messages.pop()
                else:
                    messages[-1]["content"] = messages[-1]["content"][:-100]
            else:
                break
        return messages

    def num_tokens_from_string(self, string: str, encoding_name: str) -> int:
        encoding = tiktoken.encoding_for_model(encoding_name)
        num_tokens = len(encoding.encode(string))
        return num_tokens

    def gpt_api(self, messages: list, model: str, temperature: float,
                max_tokens: int | None = None, provider_client=None,
                reasoning_effort: str | None = None, cancellation_event=None,
                provider_started_callback=None):
        """为提供的对话消息创建新的回答

        Args:
            messages (list): 完整的对话消息
        """
        # logger.info("api")
        start_time = time.monotonic()

        provider_client = provider_client or self.client

        def complete():
            extra_body = None
            if "qwen3" in model:
                extra_body = {"enable_thinking": False}
            if reasoning_effort is not None:
                extra_body = {**(extra_body or {}), "reasoning_effort": reasoning_effort}
            return provider_client.chat.completions.create(
                model=model, messages=messages, temperature=temperature,
                max_tokens=max_tokens, extra_body=extra_body,
            )

        completion = self._bounded_provider_call(
            complete,
            provider_client,
            cancellation_event,
            provider_started_callback,
        )
        choice = completion.choices[0]
        content = choice.message.content or ""
        reasoning = (
            getattr(choice.message, "reasoning", None)
            or getattr(choice.message, "reasoning_content", None)
            or getattr(choice.message, "thinking", None)
        )
        self._response_metadata.value = {
            "chunk_count": 1,
            "public_content_chunks": 1 if content else 0,
            "public_content_chars": len(content),
            "reasoning_chunks": 1 if isinstance(reasoning, str) and reasoning else 0,
            "reasoning_chars": len(reasoning) if isinstance(reasoning, str) else 0,
            "finish_reason": getattr(choice, "finish_reason", None),
        }
        # logger.warning(completion.choices[0].message.content)
        logger.debug(f"Time taken: {time.monotonic() - start_time}")
        return completion
    
    def gpt_api_stream(self, messages: list, model: str, temperature: float,
                       max_tokens: int | None = None, provider_client=None,
                       reasoning_effort: str | None = None, cancellation_event=None,
                       provider_started_callback=None):
        """为提供的对话消息创建新的回答 (流式传输)

        Args:
            messages (list): 完整的对话消息
        """
        # logger.info("streaming api")
        start_time = time.monotonic()
        provider_client = provider_client or self.client
        # print(messages)
        def consume_stream():
            extra_body = (
                {"reasoning_effort": reasoning_effort}
                if reasoning_effort is not None else None
            )
            stream = provider_client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
                temperature=temperature,
                max_tokens=max_tokens,
                extra_body=extra_body,
            )
            stream_content = ""
            chunk_count = 0
            content_chunks = 0
            reasoning_chunks = 0
            reasoning_chars = 0
            finish_reason = None
            for chunk in stream:
                chunk_count += 1
                choice = chunk.choices[0]
                delta = choice.delta
                if delta.content is not None:
                    content_chunks += 1
                    stream_content += delta.content
                reasoning = (
                    getattr(delta, "reasoning", None)
                    or getattr(delta, "reasoning_content", None)
                    or getattr(delta, "thinking", None)
                )
                if isinstance(reasoning, str) and reasoning:
                    reasoning_chunks += 1
                    reasoning_chars += len(reasoning)
                if getattr(choice, "finish_reason", None) is not None:
                    finish_reason = choice.finish_reason
            return stream_content, {
                "chunk_count": chunk_count,
                "public_content_chunks": content_chunks,
                "public_content_chars": len(stream_content),
                "reasoning_chunks": reasoning_chunks,
                "reasoning_chars": reasoning_chars,
                "finish_reason": finish_reason,
            }

        content, response_metadata = self._bounded_provider_call(
            consume_stream,
            provider_client,
            cancellation_event,
            provider_started_callback,
        )
        self._response_metadata.value = response_metadata
        logger.debug(f"Time taken: {time.monotonic() - start_time}")
        return content

    def filter_emoji(self, text: str) -> str:
        ret_str = []
        for c in text:
            try:
                c.encode('gbk')
                ret_str.append(c)
            except UnicodeEncodeError:
                continue
        return ''.join(ret_str)

    def few_shot_generate_thoughts(self, system_prompt: str = "", example_prompt: [str] or str = [], max_tokens=1024,
                                   temperature=0.0, k=1, stop=None, cache_enabled=False, api_model="", check_tags=[],
                                   json_check=False, stream=True, reasoning_effort: str | None = None,
                                   cancellation_event=None, model_admission_lock=None):
        if api_model == "":
            api_model = self.api_model
        if type(example_prompt) == str:
            example_prompt = [example_prompt]
        assert self.use_chat_api == True, "few shot generation only support chat api"
        assert len(example_prompt) % 2 == 1 or len(example_prompt) == 0, "example prompt should be odd number or empty"

        prompt = str(system_prompt) + "\n" + "\n".join(example_prompt)
        if cancellation_event is not None and cancellation_event.is_set():
            raise ProviderCallCancellationError(
                "OpenAI provider call was cancelled before start",
                provider_termination_confirmed=True,
                close_failure_diagnostics={"phase": "before_start"},
            )
        if cache_enabled:
            content = self.cache_api_call_handler(prompt, max_tokens, temperature, k, stop)
            if content is not None:
                return content

        def record_cancellation(error, *, attempt, start_time, stream):
            diagnostic = self._diagnostic_base(
                attempt=attempt, model=api_model, stream=stream,
            )
            diagnostic.update({
                "outcome": "cancelled",
                "error_type": type(error).__name__,
                "provider_termination_confirmed": (
                    error.provider_termination_confirmed
                ),
                "close_failure_diagnostics": error.close_failure_diagnostics,
                "duration_ms": round(
                    (time.monotonic() - start_time) * 1000, 3,
                ),
            })
            self._write_diagnostic(diagnostic)

        for attempt in range(1, self.model_call_attempts + 1):
            start_time = time.monotonic()
            provider_client = self._new_client()
            self.client = provider_client
            selected_reasoning_effort = (
                self.reasoning_effort if reasoning_effort is None else reasoning_effort
            )
            messages = [{"role": "system", "content": system_prompt}]
            for i in range(len(example_prompt)):
                if i % 2 == 0:
                    messages.append({"role": "user", "content": example_prompt[i]})
                else:
                    messages.append({"role": "assistant", "content": example_prompt[i]})

            try:
                admission_held = False

                def release_model_admission():
                    nonlocal admission_held
                    if admission_held:
                        admission_held = False
                        model_admission_lock.release()

                if model_admission_lock is not None:
                    model_admission_lock.acquire()
                    admission_held = True
                    if cancellation_event is not None and cancellation_event.is_set():
                        raise ProviderCallCancellationError(
                            "OpenAI provider call was cancelled before model admission",
                            provider_termination_confirmed=True,
                            close_failure_diagnostics={"phase": "model_admission"},
                        )
                cancellation_kwargs = (
                    {"cancellation_event": cancellation_event}
                    if cancellation_event is not None else {}
                )
                admission_kwargs = (
                    {"provider_started_callback": release_model_admission}
                    if admission_held else {}
                )
                if stream:
                    content = self.gpt_api_stream(
                        messages, api_model, temperature, max_tokens=max_tokens,
                        provider_client=provider_client,
                        reasoning_effort=selected_reasoning_effort,
                        **cancellation_kwargs,
                        **admission_kwargs,
                    )
                else:
                    response = self.gpt_api(
                        messages, api_model, temperature, max_tokens=max_tokens,
                        provider_client=provider_client,
                        reasoning_effort=selected_reasoning_effort,
                        **cancellation_kwargs,
                        **admission_kwargs,
                    )
                    content = response.choices[0].message.content or ""
                release_model_admission()
            except Exception as exc:
                if "release_model_admission" in locals():
                    release_model_admission()
                diagnostic = self._diagnostic_base(
                    attempt=attempt, model=api_model, stream=stream,
                )
                diagnostic.update(self._transport_metadata(exc))
                diagnostic["duration_ms"] = round(
                    (time.monotonic() - start_time) * 1000, 3,
                )
                if isinstance(exc, ProviderCallCancellationError):
                    record_cancellation(
                        exc,
                        attempt=attempt,
                        start_time=start_time,
                        stream=stream,
                    )
                    raise
                if isinstance(exc, ProviderCallTerminationError) or attempt >= self.model_call_attempts:
                    self._write_diagnostic(diagnostic)
                    raise
                delay = self.retry_delay_seconds * attempt
                if cancellation_event is not None:
                    if cancellation_event.wait(delay):
                        error = ProviderCallCancellationError(
                            "OpenAI provider call was cancelled during retry delay",
                            provider_termination_confirmed=True,
                            close_failure_diagnostics={"phase": "retry_delay"},
                        )
                        record_cancellation(
                            error,
                            attempt=attempt,
                            start_time=start_time,
                            stream=stream,
                        )
                        raise error
                elif delay:
                    time.sleep(delay)
                self._write_diagnostic(diagnostic)
                continue

            content = self.filter_emoji(content)
            response_metadata = dict(getattr(self._response_metadata, "value", {}))
            if cancellation_event is not None and cancellation_event.is_set():
                error = ProviderCallCancellationError(
                    "OpenAI provider call was cancelled before result commit",
                    provider_termination_confirmed=True,
                    close_failure_diagnostics={"phase": "before_commit"},
                )
                record_cancellation(
                    error,
                    attempt=attempt,
                    start_time=start_time,
                    stream=stream,
                )
                raise error
            missing_tags = [tag for tag in check_tags if not _contains_tag(content, tag)]
            parsed_count = len(extract_info(content)) if json_check and content else 0
            category = None
            if missing_tags:
                if not content and response_metadata.get("finish_reason") == "length":
                    category = "truncated_public_content"
                elif not content:
                    category = "empty_public_content"
                else:
                    category = "missing_required_tags"
            elif json_check and parsed_count == 0:
                category = "invalid_json_output"

            diagnostic = self._diagnostic_base(
                attempt=attempt, model=api_model, stream=stream,
            )
            diagnostic.update(response_metadata)
            diagnostic.update({
                "duration_ms": round((time.monotonic() - start_time) * 1000, 3),
                "outcome": "model_contract_failure" if category else "success",
                "validation_category": category,
                "missing_tag_count": len(missing_tags),
                "json_object_count": parsed_count,
            })
            if category:
                if attempt >= self.model_call_attempts:
                    self._write_diagnostic(diagnostic)
                    raise ModelOutputContractError(
                        category,
                        f"model output contract failed: {category}",
                    )
                delay = self.retry_delay_seconds * attempt
                if cancellation_event is not None:
                    if cancellation_event.wait(delay):
                        error = ProviderCallCancellationError(
                            "OpenAI provider call was cancelled during retry delay",
                            provider_termination_confirmed=True,
                            close_failure_diagnostics={"phase": "retry_delay"},
                        )
                        record_cancellation(
                            error,
                            attempt=attempt,
                            start_time=start_time,
                            stream=stream,
                        )
                        raise error
                elif delay:
                    time.sleep(delay)
                self._write_diagnostic(diagnostic)
                continue

            self._write_diagnostic(diagnostic)
            if cache_enabled:
                self.save_cache(prompt, content)
            if self.prompt_logging_enabled:
                with _OPENAI_STATE_LOCK:
                    with self.runtime_paths.openai_log.open("a") as log_file:
                        log_file.write(
                            "\n" + "-----------" + "\n" + "Prompt : " + str(messages) + "\n"
                        )

            return content

        raise RuntimeError("OpenAI model call exhausted without a terminal outcome")
            
            # except openai.APIConnectionError as e:
            #     logger.warning("[Proxy] The server could not be reached")
            #     # logger.warning(e.__cause__)  # an underlying Exception, likely raised within httpx.
                

            # except openai.RateLimitError as e:
            #     sleep_duratoin = os.environ.get("OPENAI_RATE_TIMEOUT", 30)
            #     logger.warning("A 429 status code was received; we should back off a bit.")
            #     # time.sleep(sleep_duratoin)
            #     raise e

            # except openai.APIStatusError as e:
            #     logger.warning("[Proxy] Another non-200-range status code was received")
            #     logger.warning(e.status_code)

            #     if e.status_code == 403:
            #         # API KEY expired, remove it from the list
            #         if self.api_key in self.api_key_list:
            #             self.api_key_list.remove(self.api_key)
            #     logger.warning(e.response)
            #     self.client = OpenAI(
            #         # This is the default and can be omitted
            #         api_key=random.choice(self.api_key_list) if len(self.api_key_list) > 0 else self.api_key,
            #         base_url=self.api_base,
            #         max_retries=5,
            #     )
    

            # except openai.InternalServerError as e:
            #     logger.warning("Something went wrong on OpenAI's end")
            #     logger.warning(e.status_code)
            #     logger.warning(e.response)
            #     raise e

            # except Exception as e:
            #     logger.warning("Something other than an HTTP error occurred")
            #     logger.warning(e)
            #     logger.warning(e.__cause__)
            #     raise e

    def encode_image(self, image_path):
        with open(image_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')

    def generate_with_image(self, prompt_before_image: [str] or str, image_path: str, prompt_after_image: [str] or str="", system_prompt: str=None, max_tokens: int=-1,
                 temperature: float=0.0, k: int=1, stop=None, cache_enabled: bool=True, api_model: str="",
                 check_tags: list=[], json_check: bool=False, stream: bool=True,
                 reasoning_effort: str | None = None):
        if api_model == "":
            api_model = self.api_model
        # else:
        #     if api_model not in OpenAILanguageModel._supported_models:
        #         raise Exception(f"only support {OpenAILanguageModel._supported_models}, but got {api_model}")
        
        if type(prompt_before_image) == str:
            prompt_before_image = [prompt_before_image]
        if type(prompt_after_image) == str:
            prompt_after_image = [prompt_after_image]
        
        assert self.use_chat_api, "few shot generation only support chat api"

        # Concatenate the prompts and image URL into the message structure
        if system_prompt is None:
            system_prompt = "You are a helpful assistant."
        messages = [{"role": "system", "content": system_prompt}]
        for prompt in prompt_before_image:
            messages.append({"role": "user", "content": prompt})
        
        # Getting the base64 string
        base64_image = self.encode_image(image_path)
        if ".jpg" in image_path:
            url = f"data:image/jpeg;base64,{base64_image}"
        elif ".png" in image_path:
            url = f"data:image/png;base64,{base64_image}"
        else:
            raise Exception("Image format not supported")
        
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": "This is an image:"},
                {"type": "image_url", "image_url": {"url": url}}
            ]
        })
        
        for prompt in prompt_after_image:
            messages.append({"role": "user", "content": prompt})
        
        
        prompt = str(system_prompt) + "\n" + "\n".join(prompt_before_image + prompt_after_image)
        if cache_enabled:
            content = self.cache_api_call_handler(prompt, max_tokens, temperature, k, stop)
            if content is not None:
                return content

        requested_max_tokens = None if max_tokens == -1 else max_tokens
        for attempt in range(1, self.model_call_attempts + 1):
            start_time = time.monotonic()
            provider_client = self._new_client()
            self.client = provider_client
            selected_reasoning_effort = (
                self.reasoning_effort if reasoning_effort is None else reasoning_effort
            )
            try:
                if stream:
                    content = self.gpt_api_stream(
                        messages, api_model, temperature, max_tokens=requested_max_tokens,
                        provider_client=provider_client,
                        reasoning_effort=selected_reasoning_effort,
                    )
                    usage_data = {"prompt_tokens": self.num_tokens_from_string(prompt, api_model),
                                    "completion_tokens": self.num_tokens_from_string(content, api_model)}
                else:
                    response = self.gpt_api(
                        messages, api_model, temperature, max_tokens=requested_max_tokens,
                        provider_client=provider_client,
                        reasoning_effort=selected_reasoning_effort,
                    )
                    usage_data = {
                        "prompt_tokens": response.usage.prompt_tokens,
                        "completion_tokens": response.usage.completion_tokens,
                    }
                    content = response.choices[0].message.content or ""
            except Exception as exc:
                diagnostic = self._diagnostic_base(attempt=attempt, model=api_model, stream=stream)
                diagnostic.update(self._transport_metadata(exc))
                diagnostic["duration_ms"] = round((time.monotonic() - start_time) * 1000, 3)
                self._write_diagnostic(diagnostic)
                if isinstance(exc, ProviderCallTerminationError) or attempt >= self.model_call_attempts:
                    raise
                time.sleep(self.retry_delay_seconds * attempt)
                continue

            content = self.filter_emoji(content)
            response_metadata = dict(getattr(self._response_metadata, "value", {}))
            missing_tags = [tag for tag in check_tags if not _contains_tag(content, tag)]
            parsed_count = len(extract_info(content)) if json_check and content else 0
            category = None
            if missing_tags:
                if not content and response_metadata.get("finish_reason") == "length":
                    category = "truncated_public_content"
                elif not content:
                    category = "empty_public_content"
                else:
                    category = "missing_required_tags"
            elif json_check and parsed_count == 0:
                category = "invalid_json_output"

            diagnostic = self._diagnostic_base(attempt=attempt, model=api_model, stream=stream)
            diagnostic.update(response_metadata)
            diagnostic.update({
                "duration_ms": round((time.monotonic() - start_time) * 1000, 3),
                "outcome": "model_contract_failure" if category else "success",
                "validation_category": category,
                "missing_tag_count": len(missing_tags),
                "json_object_count": parsed_count,
            })
            self._write_diagnostic(diagnostic)
            if category:
                if attempt >= self.model_call_attempts:
                    raise ModelOutputContractError(category, f"model output contract failed: {category}")
                time.sleep(self.retry_delay_seconds * attempt)
                continue

            self.update_token_usage(usage_data["prompt_tokens"], usage_data["completion_tokens"])
            if cache_enabled:
                self.save_cache(prompt, content)
            with _OPENAI_STATE_LOCK:
                if self.prompt_logging_enabled:
                    with self.runtime_paths.openai_log.open("a") as log_file:
                        log_file.write("\n" + "-----------" + "\n" + "Prompt : " + str(messages) + "\n")
                if self.runtime_paths.llm_inference.exists():
                    with self.runtime_paths.llm_inference.open("r") as log_file:
                        log = json.load(log_file)
                    log["time"] += time.monotonic() - start_time
                    atomic_write_json(self.runtime_paths.llm_inference, log)
            return content

        raise RuntimeError("OpenAI image model call exhausted without a terminal outcome")
               
