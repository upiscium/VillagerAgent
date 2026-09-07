import time
import langchain
# langchain.debug = True
from langchain.agents import tool, initialize_agent, AgentType
from langchain.callbacks import get_openai_callback
from langchain.chat_models import ChatOpenAI
from langchain.load.dump import dumps
from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.callbacks.manager import CallbackManager
from langchain_core.outputs import LLMResult

import json
import requests
import subprocess
import logging
import datetime
import threading
from copy import deepcopy
from functools import wraps
import os
import random
import re
import platform
from urllib.parse import urlsplit
from model.ollama_config import load_agent_api_key_list
from env.runtime_paths import RuntimePaths, atomic_write_json, read_json_artifact
from env.minecraft_bridge_diagnostics import (
    BoundedDiagnosticRecorder,
    CORRELATION_HEADER,
    OUTCOME_CERTAINTY_HEADER,
    RETRY_SAFE_HEADER,
    MOVEMENT_TERMINAL_HEADER,
    MOVEMENT_FAILURE_REASON_HEADER,
    artifact_projection,
    classify_request_exception,
    new_correlation_id,
    stable_process_start_ticks,
)

from env.runtime_execution import RuntimeExecution

env = os.environ.copy()
env["PYTHONIOENCODING"] = "utf-8"


class ToolActionBlockedError(RuntimeError):
    pass


class AgentExecutionCancelledError(RuntimeError):
    """Cooperative cancellation reached a deterministic agent boundary."""

    def __init__(self, message="Agent execution was cancelled.", *, phase="unknown",
                 blocking_operation_termination="not_active"):
        super().__init__(message)
        self.phase = phase
        self.failure_detail = {
            "reason": "cancelled",
            "message": message,
            "cancellation_acknowledged": True,
            "phase": phase,
            "blocking_operation_termination": blocking_operation_termination,
        }


def check_agent_cancellation(cancellation_token, *, phase="unknown"):
    """Raise the canonical error when a cooperative cancellation is requested."""
    if cancellation_token is None:
        return
    is_set = getattr(cancellation_token, "is_set", None)
    if callable(is_set):
        cancelled = bool(is_set())
    elif callable(cancellation_token):
        cancelled = bool(cancellation_token())
    else:
        raise TypeError("cancellation_token must be callable or expose is_set()")
    if cancelled:
        terminated = "confirmed" if any(marker in phase for marker in
                                         ("after", "_end", "return")) else "not_active"
        raise AgentExecutionCancelledError(
            phase=phase, blocking_operation_termination=terminated,
        )


def wait_for_agent_cancellation(cancellation_token, timeout):
    if cancellation_token is None:
        time.sleep(timeout)
        return False
    wait = getattr(cancellation_token, "wait", None)
    if callable(wait):
        return bool(wait(timeout))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        check_agent_cancellation(cancellation_token, phase="retry_wait")
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.05, remaining))
    return False


class MinecraftToolTimeoutError(TimeoutError):
    def __init__(self, message: str, *, agent: str | None = None, tool: str | None = None,
                 request_id: str | None = None, timeout_type: str | None = None):
        super().__init__(message)
        self.failure_detail = {
            "reason": "minecraft_tool_timeout",
            "outcome_certainty": "unknown",
            "retry_safe": False,
            "message": message,
        }
        if agent is not None:
            self.failure_detail["agent"] = agent
        if tool is not None:
            self.failure_detail["tool"] = tool
        if request_id is not None:
            self.failure_detail["request_id"] = request_id
        if timeout_type is not None:
            self.failure_detail["timeout_type"] = timeout_type


class MinecraftToolEffectUnknownError(MinecraftToolTimeoutError):
    def __init__(self, message: str, *, agent: str | None = None,
                 tool: str | None = None, request_id: str | None = None,
                 status_code: int | None = None, bridge_reason: str | None = None,
                 coordinator_terminal: bool | None = None):
        super().__init__(
            message, agent=agent, tool=tool, request_id=request_id,
            timeout_type="bridge_effect_unknown",
        )
        self.failure_detail["reason"] = "minecraft_tool_effect_unknown"
        if status_code is not None:
            self.failure_detail["status_code"] = status_code
        if bridge_reason is not None:
            self.failure_detail["bridge_reason"] = bridge_reason
        if coordinator_terminal is not None:
            self.failure_detail["coordinator_terminal"] = coordinator_terminal


class MinecraftActionLogError(RuntimeError):
    def __init__(self, message: str, *, agent: str | None = None):
        super().__init__(message)
        self.failure_detail = {
            "reason": "minecraft_action_log_error",
            "outcome_certainty": "unknown",
            "retry_safe": False,
            "message": message,
        }
        if agent is not None:
            self.failure_detail["agent"] = agent


class MinecraftBridgeCleanupError(RuntimeError):
    def __init__(self, message: str, *, cleanup_result: dict):
        super().__init__(message)
        self.cleanup_result = cleanup_result


DEFAULT_MINECRAFT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_MINECRAFT_READ_TIMEOUT_SECONDS = 30.0
DEFAULT_BRIDGE_TERMINATE_GRACE_SECONDS = 2.0
DEFAULT_BRIDGE_KILL_GRACE_SECONDS = 1.0
BRIDGE_CLEANUP_PROCESS_DIAGNOSTIC_LIMIT = 64
DEFAULT_MOVEMENT_CANCEL_CONNECT_TIMEOUT_SECONDS = 0.5
DEFAULT_MOVEMENT_CANCEL_READ_TIMEOUT_SECONDS = 2.0


def _request_monotonic_ns() -> int:
    return time.monotonic_ns()


def _minecraft_request(method: str, url: str, **kwargs):
    diagnostic_kind = kwargs.pop("_diagnostic_kind", None)
    timeout = kwargs.pop("timeout", None) or (
        Agent.minecraft_connect_timeout_seconds,
        Agent.minecraft_read_timeout_seconds,
    )
    connect_timeout, read_timeout = (
        timeout if isinstance(timeout, tuple) else (float(timeout), float(timeout))
    )
    actor_name = Agent.agent_name_for_url(url)
    route = urlsplit(url).path or "/"
    tool_name = route.rstrip("/").rsplit("/", 1)[-1]
    correlation_id = new_correlation_id()
    headers = dict(kwargs.pop("headers", {}) or {})
    headers[CORRELATION_HEADER] = correlation_id
    started = _request_monotonic_ns()
    common = {
        "correlation_id": correlation_id,
        "actor": actor_name,
        "method": method,
        "route": route,
        "endpoint_identity": f"actor:{actor_name or 'unknown'}",
        "started_monotonic_ns": started,
    }
    Agent.record_bridge_diagnostic(
        actor_name, "caller_request_started", **common,
        configured_connect_timeout_s=connect_timeout,
        configured_read_timeout_s=read_timeout,
        outcome_certainty="unknown", retry_safe=False,
    )
    if diagnostic_kind == "ping":
        Agent.record_bridge_diagnostic(actor_name, "ping_started", **common)
    try:
        response = requests.request(method, url, timeout=timeout, headers=headers, **kwargs)
    except requests.RequestException as exc:
        completed = _request_monotonic_ns()
        timeout_type = classify_request_exception(exc)
        terminal = "caller_request_timed_out" if timeout_type.endswith("timeout") else "caller_request_failed"
        terminal_fields = {
            **common,
            "completed_monotonic_ns": completed,
            "elapsed_ns": max(0, completed - started),
            "configured_connect_timeout_s": connect_timeout,
            "configured_read_timeout_s": read_timeout,
            "timeout_type": timeout_type,
            "error_class": type(exc).__name__,
            "outcome_certainty": "unknown",
            "retry_safe": False,
        }
        Agent.record_bridge_diagnostic(actor_name, terminal, **terminal_fields)
        if diagnostic_kind == "ping":
            Agent.record_bridge_diagnostic(
                actor_name,
                "ping_timed_out" if timeout_type.endswith("timeout") else "ping_failed",
                **terminal_fields,
            )
        if isinstance(exc, requests.Timeout):
            Agent.last_tool_timeout = {
                "agent": actor_name,
                "tool": tool_name,
                "outcome_certainty": "unknown",
                "retry_safe": False,
            }
            raise MinecraftToolTimeoutError(
                f"Minecraft tool request timed out: {tool_name}",
                agent=actor_name,
                tool=tool_name,
                request_id=correlation_id,
                timeout_type=timeout_type,
            ) from exc
        raise
    completed = _request_monotonic_ns()
    response_headers = getattr(response, "headers", {}) or {}
    effect_unknown = (
        str(response_headers.get(OUTCOME_CERTAINTY_HEADER, "")).lower() == "unknown"
        and str(response_headers.get(RETRY_SAFE_HEADER, "")).lower() == "false"
    )
    if effect_unknown:
        status_code = getattr(response, "status_code", None)
        terminal_header = str(
            response_headers.get(MOVEMENT_TERMINAL_HEADER, "")
        ).lower()
        coordinator_terminal = (
            True if terminal_header == "true"
            else False if terminal_header == "false"
            else None
        )
        bridge_reason = response_headers.get(MOVEMENT_FAILURE_REASON_HEADER)
        Agent.record_bridge_diagnostic(
            actor_name, "caller_request_failed", **common,
            completed_monotonic_ns=completed,
            elapsed_ns=max(0, completed - started),
            status_code=status_code,
            configured_connect_timeout_s=connect_timeout,
            configured_read_timeout_s=read_timeout,
            timeout_type="bridge_effect_unknown",
            outcome_certainty="unknown", retry_safe=False,
        )
        Agent.last_tool_timeout = {
            "agent": actor_name,
            "tool": tool_name,
            "outcome_certainty": "unknown",
            "retry_safe": False,
        }
        raise MinecraftToolEffectUnknownError(
            f"Minecraft tool outcome is unknown: {tool_name}",
            agent=actor_name, tool=tool_name, request_id=correlation_id,
            status_code=status_code, bridge_reason=bridge_reason,
            coordinator_terminal=coordinator_terminal,
        )
    completed_fields = {
        **common,
        "completed_monotonic_ns": completed,
        "elapsed_ns": max(0, completed - started),
        "status_code": getattr(response, "status_code", None),
    }
    Agent.record_bridge_diagnostic(
        actor_name, "caller_request_completed", **completed_fields,
        configured_connect_timeout_s=connect_timeout,
        configured_read_timeout_s=read_timeout,
        outcome_certainty="known", retry_safe=False,
    )
    if diagnostic_kind == "ping":
        Agent.record_bridge_diagnostic(
            actor_name, "caller_ping_transport_completed", **completed_fields,
        )
        try:
            response._villager_ping_diagnostic = completed_fields
        except Exception:
            pass
    return response


def filter_emoji(text: str) -> str:
    ret_str = []
    for c in text:
        try:
            c.encode('gbk')
            ret_str.append(c)
        except UnicodeEncodeError:
            continue
    return ''.join(ret_str)

def filter_emoji_from_dict(obj):
    if isinstance(obj, dict):
        return {k: filter_emoji_from_dict(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [filter_emoji_from_dict(i) for i in obj]
    elif isinstance(obj, str):
        return filter_emoji(obj)
    else:
        return obj


def _short_diagnostic_value(value, max_length=500):
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except TypeError:
            text = str(value)
    text = filter_emoji(text).replace("\n", " ").strip()
    if len(text) > max_length:
        return text[:max_length] + "..."
    return text


def _summarize_intermediate_step(step):
    action = step[0] if isinstance(step, (list, tuple)) and len(step) > 0 else step
    feedback = step[1] if isinstance(step, (list, tuple)) and len(step) > 1 else None
    kwargs = action.get("kwargs", {}) if isinstance(action, dict) else {}
    return {
        "tool": kwargs.get("tool"),
        "tool_input": kwargs.get("tool_input"),
        "log": _short_diagnostic_value(kwargs.get("log"), max_length=200),
        "feedback": _short_diagnostic_value(feedback, max_length=200),
    }


def _log_action_diagnostics(agent_name, call_site, response, action_list, llmhandler):
    intermediate_steps = response.get("intermediate_steps", []) if isinstance(response, dict) else []
    final_answer = response.get("output", "") if isinstance(response, dict) else ""
    response_keys = sorted(response.keys()) if isinstance(response, dict) else []
    last_llm_output = llmhandler.llm_out[-1] if getattr(llmhandler, "llm_out", []) else ""

    if action_list:
        action_names = []
        for action in action_list:
            action_kwargs = action.get("action", {}) if isinstance(action, dict) else {}
            action_kwargs = action_kwargs if isinstance(action_kwargs, dict) else {}
            action_names.append(action_kwargs.get("tool") or action_kwargs.get("action") or "unknown")
        logging.info(
            "Minecraft action diagnostics: agent=%s call_site=%s parsed_actions=%d intermediate_steps=%d actions=%s",
            agent_name,
            call_site,
            len(action_list),
            len(intermediate_steps),
            action_names,
        )
        return

    step_summaries = [_summarize_intermediate_step(step) for step in intermediate_steps]
    if intermediate_steps:
        logging.warning(
            "Minecraft action diagnostics: agent=%s call_site=%s tool-call parsing failure; "
            "intermediate_steps=%d parsed_actions=0 response_keys=%s steps=%s final_answer=%s",
            agent_name,
            call_site,
            len(intermediate_steps),
            response_keys,
            step_summaries,
            _short_diagnostic_value(final_answer),
        )
        return

    if _short_diagnostic_value(final_answer):
        logging.warning(
            "Minecraft action diagnostics: agent=%s call_site=%s thought-only response; "
            "intermediate_steps=0 parsed_actions=0 response_keys=%s final_answer=%s last_llm_output=%s",
            agent_name,
            call_site,
            response_keys,
            _short_diagnostic_value(final_answer),
            _short_diagnostic_value(last_llm_output),
        )
        return

    logging.warning(
        "Minecraft action diagnostics: agent=%s call_site=%s tool-call failure; "
        "intermediate_steps=0 parsed_actions=0 response_keys=%s final_answer_empty=true last_llm_output=%s",
        agent_name,
        call_site,
        response_keys,
        _short_diagnostic_value(last_llm_output),
    )


class OllamaReasoningChatOpenAI(ChatOpenAI):
    """Expose Ollama reasoning text to legacy structured-chat parsers."""

    _structured_action_pattern = re.compile(r"```(?:json\s+)?(\W.*?)```", re.DOTALL)

    @classmethod
    def _reasoning_as_structured_chat_content(cls, reasoning):
        match = cls._structured_action_pattern.search(reasoning)
        if match is not None:
            try:
                payload = json.loads(match.group(1).strip(), strict=False)
            except json.JSONDecodeError:
                return reasoning
            action = payload.get("action") if isinstance(payload, dict) else None
            if isinstance(action, str) and action.strip():
                return reasoning

        try:
            payload = json.loads(reasoning)
        except json.JSONDecodeError:
            payload = None
        action = payload.get("action") if isinstance(payload, dict) else None
        if isinstance(action, str) and action.strip():
            return f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"

        # A malformed fenced action makes the legacy parser request another
        # iteration instead of accepting natural-language thought as a final answer.
        return f"```json\n{{}}\n```\n{reasoning}"

    def _create_chat_result(self, response):
        if isinstance(response, dict):
            payload = deepcopy(response)
        elif hasattr(response, "model_dump"):
            payload = response.model_dump()
        else:
            payload = response.dict()
        for choice in payload.get("choices", []):
            message = choice.get("message", {})
            reasoning = message.get("reasoning")
            if not message.get("content") and isinstance(reasoning, str) and reasoning:
                message["content"] = self._reasoning_as_structured_chat_content(reasoning)
        return super()._create_chat_result(payload)


def timeit(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.time()

        ### SEND EMOTION AND MURMUR TO THE SERVER
        agent_name = kwargs["player_name"] # 第一个参数是 agent_name
        emotion = kwargs.get("emotion", [])
        murmur = kwargs.get("murmur", "")

        system_type = platform.system().lower()
        if system_type != "linux":
            emotion = []

        url = Agent.get_agent_url(agent_name) + "/post_emojimurmur"
        data = {
            "emotion": emotion,
            "murmur": murmur,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        ###
        kwargs_in = kwargs.copy()
        if "emotion" in kwargs:
            kwargs_in["emotion"] = []
        if "murmur" in kwargs:
            kwargs_in["murmur"] = ""

        result = func(*args, **kwargs_in)
        end_time = time.time()
        
        Agent.append_action_log(agent_name, {
            "action": func.__name__,
            "start_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time)),
            "end_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end_time)),
            "duration": end_time - start_time,
            "kwargs": kwargs,
            "result": result,
        })
        
        return result
    return wrapper

    
class LLMHandler(BaseCallbackHandler):
    def __init__(self):
        self.llm_out = []
        self.seralized_input = []
        self.chain_input = []

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs):
        cleaned_inputs = {k: filter_emoji(v) if isinstance(v, str) else v for k, v in inputs.items()}
        self.seralized_input.append(serialized)
        self.chain_input.append(cleaned_inputs)


    def on_llm_start(self, serialized, prompts, **kwargs):
        print("x" * 30 + "llm_start called" + "x" * 30)
        clean_prompts = [filter_emoji(p) for p in prompts]
        self.seralized_input.append(serialized)
        self.chain_input.append(clean_prompts)

        
    def on_llm_end(self, llm_result: LLMResult, **kwargs):
        # 强制使用UTF-8编码打印
        print("x" * 30 + "llm_end called" + "x" * 30)
        
        # 安全处理llm_output（可能包含Unicode字符）
        if llm_result.llm_output is not None:
            try:
                # 如果是字典形式（如OpenAI返回的token_usage等）
                llm_result.llm_output = filter_emoji(llm_result.llm_output)
                if isinstance(llm_result.llm_output, dict):
                    import json
                    # 将字典转为JSON字符串确保UTF-8编码
                    output_str = json.dumps(llm_result.llm_output, ensure_ascii=False)
                    self.llm_out.append(output_str)
                else:
                    # 其他情况直接存储，确保是Unicode字符串
                    self.llm_out.append(str(llm_result.llm_output))
            except UnicodeEncodeError:
                # 如果仍有编码问题，强制UTF-8编码
                self.llm_out.append(llm_result.llm_output.encode('utf-8', errors='replace').decode('utf-8'))


class CancellationCallbackHandler(BaseCallbackHandler):
    raise_error = True

    def __init__(self, cancellation_token, phase_callback=None):
        self.cancellation_token = cancellation_token
        self.phase_callback = phase_callback

    def _check(self, phase, *, completion=False):
        if completion and callable(self.phase_callback):
            self.phase_callback(phase)
        check_agent_cancellation(self.cancellation_token, phase=phase)
        if not completion and callable(self.phase_callback):
            self.phase_callback(phase)

    def on_llm_start(self, serialized, prompts, **kwargs):
        self._check("model_start")

    def on_llm_end(self, response, **kwargs):
        self._check("model_end", completion=True)

    def on_tool_start(self, serialized, input_str, **kwargs):
        self._check("tool_start")

    def on_tool_end(self, output, **kwargs):
        self._check("tool_end", completion=True)
        

class Agent():
    '''
    Agent is the basic class for the agent in the Minecraft environment.
    Agent supports high-level and low-level functions for the agent to interact with the Minecraft environment.
    It works as a bridge between the Minecraft environment and the AI model.
    '''
    headers = {'Content-Type': 'application/json'}

    logging.basicConfig()
    logging.getLogger("langchain.retrievers.multi_query").setLevel(logging.INFO)
    model = "gpt-4-1106-preview"
    provider = ""
    temperature = 0
    max_tokens = 1024
    api_key_list = []
    base_url = "https://api.chatanywhere.tech/v1"
    verbose = True

    name2port = {}
    bridge_entrypoint_by_name = {}
    agent_process = {}
    url_prefix = {}
    runtime_paths_by_name: dict[str, RuntimePaths] = {}
    _url_registry_lock = threading.Lock()
    _action_log_locks: dict[str, threading.Lock] = {}
    _action_log_locks_guard = threading.Lock()
    last_bridge_cleanup: dict | None = None
    minecraft_connect_timeout_seconds = DEFAULT_MINECRAFT_CONNECT_TIMEOUT_SECONDS
    minecraft_read_timeout_seconds = DEFAULT_MINECRAFT_READ_TIMEOUT_SECONDS
    last_tool_timeout = None
    _bridge_diagnostic_recorders: dict[str, BoundedDiagnosticRecorder] = {}
    _bridge_diagnostic_lock = threading.Lock()
    last_bridge_diagnostics: dict | None = None

    @classmethod
    def _caller_diagnostic_recorder(cls, actor_name: str | None):
        if not actor_name or actor_name not in cls.runtime_paths_by_name:
            return None
        path = cls.runtime_paths_by_name[actor_name].minecraft_bridge_caller_diagnostics
        key = str(path)
        with cls._bridge_diagnostic_lock:
            recorder = cls._bridge_diagnostic_recorders.get(key)
            if recorder is None:
                recorder = BoundedDiagnosticRecorder(path, producer="caller")
                cls._bridge_diagnostic_recorders[key] = recorder
            return recorder

    @classmethod
    def record_bridge_diagnostic(cls, actor_name: str | None, event_type: str, **fields) -> bool:
        recorder = cls._caller_diagnostic_recorder(actor_name)
        return recorder.record(event_type, **fields) if recorder is not None else False

    @classmethod
    def _close_bridge_diagnostic_recorders(cls) -> None:
        with cls._bridge_diagnostic_lock:
            recorders = list(cls._bridge_diagnostic_recorders.values())
            cls._bridge_diagnostic_recorders.clear()
        for recorder in recorders:
            recorder.close()

    @classmethod
    def bridge_diagnostics_summary(cls, paths_by_name=None) -> dict:
        selected = dict(paths_by_name or cls.runtime_paths_by_name)
        with cls._bridge_diagnostic_lock:
            recorders = list(cls._bridge_diagnostic_recorders.values())
        for recorder in recorders:
            recorder.flush()
        artifacts = {}
        actors = {}
        errors = []
        caller_paths = sorted({
            str(paths.minecraft_bridge_caller_diagnostics): paths
            for paths in selected.values()
        }.items())
        caller_projections = {}
        caller_artifact_keys = {}
        for index, (caller_path, paths) in enumerate(caller_paths):
            artifact_key = "caller" if len(caller_paths) == 1 else f"caller:{index}"
            projection = artifact_projection(caller_path, runtime_root=paths.root)
            caller_projections[caller_path] = projection
            caller_artifact_keys[caller_path] = artifact_key
            artifacts[artifact_key] = projection
        for actor_name, paths in sorted(selected.items()):
            actor_artifacts = {}
            caller_path = paths.minecraft_bridge_caller_diagnostics
            caller_key = str(caller_path)
            caller_projection = caller_projections[caller_key]
            bridge_projection = artifact_projection(
                paths.minecraft_bridge_actor_diagnostics(actor_name), runtime_root=paths.root,
            )
            artifacts[f"bridge:{actor_name}"] = bridge_projection
            actor_artifacts["caller"] = caller_projection.get("state")
            actor_artifacts["caller_artifact"] = caller_artifact_keys[caller_key]
            actor_artifacts["bridge"] = bridge_projection.get("state")
            caller_events = (
                caller_projection.get("snapshot", {}).get("events", [])
                if caller_projection.get("state") == "valid" else []
            )
            bridge_events = (
                bridge_projection.get("snapshot", {}).get("events", [])
                if bridge_projection.get("state") == "valid" else []
            )
            caller_snapshot = (
                caller_projection.get("snapshot", {})
                if caller_projection.get("state") == "valid" else {}
            )
            bridge_snapshot = (
                bridge_projection.get("snapshot", {})
                if bridge_projection.get("state") == "valid" else {}
            )
            actor_caller = [event for event in caller_events if event.get("actor") == actor_name]
            caller_critical = [
                event for event in caller_snapshot.get("critical_events", [])
                if event.get("actor") == actor_name
            ]
            bridge_critical = [
                event for event in bridge_snapshot.get("critical_events", [])
                if event.get("actor") in (None, actor_name)
            ]
            correlations = {}
            for summary in caller_snapshot.get("correlations", []):
                if summary.get("actor") == actor_name:
                    correlations[summary["correlation_id"]] = {
                        "correlation_id": summary["correlation_id"],
                        "caller": dict(summary),
                        "bridge": None,
                    }
            for summary in bridge_snapshot.get("correlations", []):
                if summary.get("actor") in (None, actor_name):
                    correlation_id = summary["correlation_id"]
                    merged = correlations.setdefault(correlation_id, {
                        "correlation_id": correlation_id,
                        "caller": None,
                        "bridge": None,
                    })
                    merged["bridge"] = dict(summary)
            caller_lifecycle = caller_snapshot.get("lifecycle", {}).get("actors", {}).get(
                actor_name, {},
            )
            bridge_lifecycle = bridge_snapshot.get("lifecycle", {}).get("actors", {}).get(
                actor_name, {},
            )

            def retained_lifecycle_events(source, category, recent, prefix):
                retained = {
                    (event.get("event_type"), event.get("timestamp_monotonic_ns")): event
                    for event in source.get(category, {}).values()
                }
                if retained:
                    return sorted(
                        retained.values(),
                        key=lambda event: event.get("timestamp_monotonic_ns", 0),
                    )
                return [event for event in recent
                        if event.get("event_type", "").startswith(prefix)]

            actors[actor_name] = {
                "artifacts": actor_artifacts,
                "last_request": next((event for event in reversed(actor_caller)
                                      if event.get("event_type", "").startswith("caller_request_")), None),
                "last_ping": next((event for event in reversed(actor_caller)
                                   if event.get("event_type", "").startswith("ping_")), None),
                "process_lifecycle": retained_lifecycle_events(
                    caller_lifecycle, "process", actor_caller, "bridge_process_",
                ),
                "listener_lifecycle": retained_lifecycle_events(
                    bridge_lifecycle, "listener", bridge_events, "listener_",
                ),
                "mineflayer_lifecycle": retained_lifecycle_events(
                    bridge_lifecycle, "mineflayer", bridge_events, "mineflayer_",
                ),
                "last_bridge_request": next((event for event in reversed(bridge_events)
                                              if event.get("event_type", "").startswith("request_")), None),
                "critical_events": {
                    "caller": caller_critical,
                    "bridge": bridge_critical,
                },
                "correlation_summaries": list(correlations.values()),
                "unresolved_bridge_requests": [
                    summary for summary in bridge_snapshot.get("unresolved_requests", [])
                    if summary.get("actor") in (None, actor_name)
                ],
                "long_duration_bridge_requests": [
                    summary for summary in bridge_snapshot.get("long_duration_requests", [])
                    if summary.get("actor") in (None, actor_name)
                ],
                "lifecycle_milestones": {
                    "process": caller_lifecycle.get("process", {}),
                    "listener": bridge_lifecycle.get("listener", {}),
                    "mineflayer": bridge_lifecycle.get("mineflayer", {}),
                },
                "retention": {
                    "caller": caller_snapshot.get("retention"),
                    "bridge": bridge_snapshot.get("retention"),
                },
            }
            for projection in (caller_projection, bridge_projection):
                if projection.get("state") != "valid":
                    errors.append({
                        "actor": actor_name,
                        "error": projection.get("error") or "diagnostic_artifact_absent",
                    })
        return {
            "schema_version": "minecraft-bridge-diagnostics-summary/1",
            "actors": actors,
            "artifacts": artifacts,
            "diagnostic_collection_error": errors or None,
        }

    @classmethod
    def tool_runtime_context(cls) -> dict:
        return {
            "http_timeout_seconds": {
                "connect": cls.minecraft_connect_timeout_seconds,
                "read": cls.minecraft_read_timeout_seconds,
            },
            "last_tool_timeout": cls.last_tool_timeout,
            "bridge_diagnostics": (
                cls.last_bridge_diagnostics
                if cls.last_bridge_diagnostics is not None
                else cls.bridge_diagnostics_summary()
            ),
        }

    @classmethod
    def tool_runtime_snapshot(cls) -> dict:
        """Return an in-memory-only projection for post-verdict diagnostics."""
        return {
            "http_timeout_seconds": {
                "connect": cls.minecraft_connect_timeout_seconds,
                "read": cls.minecraft_read_timeout_seconds,
            },
            "last_tool_timeout": deepcopy(cls.last_tool_timeout),
            "bridge_diagnostics": deepcopy(cls.last_bridge_diagnostics),
            "bridge_diagnostics_state": (
                "finalized" if cls.last_bridge_diagnostics is not None
                else "not_finalized_active_recorder_snapshot_unavailable"
            ),
            "snapshot_source": "in_memory_only",
        }

    @staticmethod
    def get_url_prefix(runtime_paths: RuntimePaths) -> dict:
        url_prefix_path = runtime_paths.url_prefix
        if url_prefix_path.exists():
            with url_prefix_path.open("r", encoding='utf-8') as f:
                url_prefix = json.load(f)
        else:
            url_prefix = {}
        return url_prefix

    @classmethod
    def runtime_paths_for(cls, player_name: str) -> RuntimePaths:
        try:
            return cls.runtime_paths_by_name[player_name]
        except KeyError as exc:
            raise RuntimeError(
                f"No runtime paths registered for agent {player_name}"
            ) from exc

    @classmethod
    def get_agent_url(cls, player_name: str) -> str:
        registry = cls.get_url_prefix(cls.runtime_paths_for(player_name))
        try:
            return registry[player_name]
        except KeyError as exc:
            raise RuntimeError(
                f"No bridge URL registered for agent {player_name}"
            ) from exc

    @classmethod
    def agent_name_for_url(cls, url: str) -> str | None:
        for player_name, runtime_paths in tuple(cls.runtime_paths_by_name.items()):
            try:
                prefix = cls.get_url_prefix(runtime_paths).get(player_name)
            except (OSError, TypeError, ValueError):
                continue
            if isinstance(prefix, str) and url.startswith(prefix + "/"):
                return player_name
        return None

    @classmethod
    def action_log_lock_for(cls, runtime_paths: RuntimePaths) -> threading.Lock:
        key = str(runtime_paths.action_log.resolve())
        with cls._action_log_locks_guard:
            return cls._action_log_locks.setdefault(key, threading.Lock())

    @classmethod
    def append_action_log(cls, player_name: str, entry: dict) -> None:
        runtime_paths = cls.runtime_paths_for(player_name)
        runtime_paths.ensure_directories()
        action_log_path = runtime_paths.action_log
        try:
            with cls.action_log_lock_for(runtime_paths):
                result = read_json_artifact(action_log_path)
                if result.state == "absent":
                    action_log = {}
                elif result.state == "invalid":
                    raise MinecraftActionLogError(
                        f"action log is invalid: {result.error}",
                        agent=player_name,
                    )
                elif not isinstance(result.value, dict):
                    raise MinecraftActionLogError(
                        "action log must contain an object",
                        agent=player_name,
                    )
                else:
                    action_log = result.value
                entries = action_log.setdefault(player_name, [])
                if not isinstance(entries, list):
                    raise MinecraftActionLogError(
                        f"action log entry for {player_name} must be a list",
                        agent=player_name,
                    )
                entries.append(entry)
                atomic_write_json(action_log_path, action_log)
        except MinecraftActionLogError:
            raise
        except Exception as exc:
            raise MinecraftActionLogError(
                f"failed to persist action log: {exc}",
                agent=player_name,
            ) from exc

    def __init__(self, name, prefix=None, context=None, prompt=None, tools=[], local_port=5000, model="", runtime_paths: RuntimePaths | None = None, runtime_execution=None):
        self.name = name
        self.prefix = prefix
        self.context = context
        self.prompt = prompt
        self.local_port = local_port
        self.model = Agent.model if model == "" else model
        self.runtime_paths = runtime_paths or RuntimePaths.legacy()
        self.runtime_execution = runtime_execution
        self.reflection_output_dir = self.runtime_paths.run_result_dir("test")
        self.action_history = []
        self.basic_tools = [
            Agent.scanNearbyEntities, Agent.navigateTo, Agent.attackTarget,
            Agent.useItemOnEntity, Agent.useItemOnBlock, Agent.fetchContainerContents,
            Agent.MineBlock, Agent.placeBlock, Agent.equipItem,
            Agent.handoverBlock, Agent.SmeltingCooking, Agent.talkTo, Agent.waitForFeedback,
            Agent.withdrawItem, Agent.storeItem, Agent.craftBlock, Agent.ToggleAction, 
        ]
        self.all_tools = [
            Agent.scanNearbyEntities, Agent.navigateTo, Agent.attackTarget, Agent.useItemOnEntity, Agent.useItemOnBlock, 
            Agent.MineBlock, Agent.placeBlock, Agent.equipItem, Agent.handoverBlock, Agent.SmeltingCooking, Agent.withdrawItem, 
            Agent.storeItem, Agent.craftBlock, Agent.eat, Agent.fetchContainerContents, 
            Agent.openContainer, Agent.performMovement, 
            Agent.sleep, Agent.wake, Agent.talkTo, Agent.waitForFeedback, Agent.startFishing, Agent.ToggleAction, 
            Agent.read, Agent.mountEntity, Agent.dismountEntity
        ]
        # self.all_tools = [
        #     Agent.scanNearbyEntities, Agent.navigateTo, Agent.attackTarget,
        #     Agent.navigateToBuilding, Agent.navigateToAnimal, Agent.navigateToPlayer,
        #     Agent.useItemOnEntity, Agent.sleep, Agent.wake,
        #     Agent.MineBlock, Agent.placeBlock, Agent.waitForFeedback, Agent.equipItem,
        #     Agent.tossItem, Agent.talkTo, Agent.handoverBlock,
        #     Agent.withdrawItem, Agent.storeItem, Agent.craftBlock,
        #     Agent.SmeltingCooking, Agent.erectDirtLadder, Agent.dismantleDirtLadder,
        #     Agent.enchantItem, Agent.trade, Agent.repairItem, Agent.eat,
        #     Agent.drink, Agent.wear, Agent.layDirtBeam, Agent.removeDirtBeam,
        #     Agent.openContainer, Agent.closeContainer,
        #     Agent.fetchContainerContents, Agent.ToggleAction,
        #     Agent.get_entity_info, Agent.get_environment_info, 
        #     Agent.performMovement, Agent.lookAt, Agent.startFishing,
        #     Agent.stopFishing, Agent.read, Agent.readPage, Agent.write,
        #     Agent.mountEntity, Agent.dismountEntity, Agent.rideEntity, Agent.disrideEntity,
        # ]
        if tools:
            self.tools = tools
        else:
            self.tools = self.basic_tools

        if name == "nobody":
            return
        with Agent._url_registry_lock:
            Agent.runtime_paths_by_name[name] = self.runtime_paths
            url_prefix = Agent.get_url_prefix(self.runtime_paths)
            url_prefix[name] = f"http://localhost:{local_port}"
            atomic_write_json(self.runtime_paths.url_prefix, url_prefix)

        Agent.name2port[name] = local_port
        if prefix is None:
            self.prefix = "You are a helpful friendly assistant.\n"

    def render(self, structure_idx, center_pos):
        url = Agent.get_agent_url(self.name) + "/post_render"
        data = {
            "id": structure_idx,
            "center_pos": center_pos,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    def env(self, prompt):
        """Get the Environment Information"""
        url = Agent.get_agent_url(self.name) + "/post_environment"
        response = _minecraft_request("POST", url, headers=Agent.headers)
        return str(response.json())
    
    def get_environment_info_dict(player_name: str):
        """Get the Environment Information, return string contains time of day, weather"""
        url = Agent.get_agent_url(player_name) + "/post_environment_dict"
        response = _minecraft_request("POST", url, headers=Agent.headers)
        return response.json()
    
    def ping(player_name: str):
        """Ping the Server"""
        response = None

        def record_response_failure(error):
            diagnostic = (
                getattr(response, "_villager_ping_diagnostic", {})
                if response is not None else {}
            )
            ping_fields = dict(diagnostic)
            ping_fields.setdefault("actor", player_name)
            ping_fields.setdefault("endpoint_identity", f"actor:{player_name}")
            Agent.record_bridge_diagnostic(
                player_name, "ping_failed", **ping_fields,
                error_class=type(error).__name__, result="invalid_response",
            )

        try:
            url = Agent.get_agent_url(player_name) + "/post_ping"
            response = _minecraft_request("GET", url, _diagnostic_kind="ping")
            result = response.json()
            diagnostic = getattr(response, "_villager_ping_diagnostic", {})
            succeeded = (
                isinstance(result, dict) and result.get("status") is True
                and isinstance(getattr(response, "status_code", None), int)
                and 200 <= response.status_code < 300
            )
            ping_fields = dict(diagnostic)
            ping_fields.setdefault("actor", player_name)
            ping_fields.setdefault("endpoint_identity", f"actor:{player_name}")
            Agent.record_bridge_diagnostic(
                player_name, "ping_succeeded" if succeeded else "ping_failed",
                **ping_fields,
                result="healthy" if succeeded else "unhealthy",
            )
            return result
        except MinecraftToolTimeoutError:
            return {'message': 'Exception', 'status': False}
        except requests.RequestException as e:
            if response is not None:
                record_response_failure(e)
            return {'message': 'Exception', 'status': False}
        except Exception as e:
            record_response_failure(e)
            return {'message': 'Exception', 'status': False}

    @staticmethod
    def launch(host="10.21.31.18", port=25565, world="world", verbose=False, ignore_name=[], debug=False, fast=False, runtime_paths: RuntimePaths | None = None, runtime_execution=None):
        Agent.port = port
        Agent.last_bridge_cleanup = None
        Agent.last_bridge_diagnostics = None
        Agent._close_bridge_diagnostic_recorders()
        runtime_paths = runtime_paths or RuntimePaths.legacy()
        if runtime_execution is None:
            runtime_execution = RuntimeExecution.resolve()
        entrypoint = "bridge_fast" if fast else "bridge_standard"
        if verbose:
            print("launch ...")
        for key, value in Agent.name2port.items():
            if key in ignore_name:
                continue
            runtime_execution.verify(entrypoint)
            args = ("-H", host, "-P", str(port), "-LP", str(value), "-U", key,
                    "-W", world, "-D", str(debug))
            command = runtime_execution.python_command(entrypoint, *args)
            child = runtime_execution.child_kwargs(runtime_paths, base=env)
            try:
                process = subprocess.Popen(command, shell=False, **child)
            except BaseException as error:
                Agent.record_bridge_diagnostic(
                    key, "bridge_process_spawn_failed", actor=key,
                    endpoint_identity=f"actor:{key}", expected_local_port=value,
                    entrypoint=entrypoint, error_class=type(error).__name__,
                )
                raise
            Agent.agent_process[key] = process
            Agent.bridge_entrypoint_by_name[key] = entrypoint
            Agent.record_bridge_diagnostic(
                key, "bridge_process_spawned", actor=key,
                endpoint_identity=f"actor:{key}", pid=getattr(process, "pid", None),
                process_start_ticks=stable_process_start_ticks(getattr(process, "pid", None)),
                expected_local_port=value, entrypoint=entrypoint,
            )
            print(runtime_execution.public_command(entrypoint, *args))
            time.sleep(10 if fast else 2)
        if verbose:
            print("launch done.")

    @classmethod
    def cancel_active_movements(
        cls,
        actor_names=None,
        *,
        reason="controller_shutdown",
        timeout=(DEFAULT_MOVEMENT_CANCEL_CONNECT_TIMEOUT_SECONDS,
                  DEFAULT_MOVEMENT_CANCEL_READ_TIMEOUT_SECONDS),
        total_timeout_seconds=None,
    ) -> dict:
        """Request bounded bridge-side movement cancellation without retry."""
        selected = list(cls.name2port if actor_names is None else actor_names)
        fast_actor_count = sum(
            cls.bridge_entrypoint_by_name.get(name) == "bridge_fast"
            for name in selected
        )
        base_timeout_total = float(timeout[0]) + float(timeout[1])
        total_budget = (
            base_timeout_total if total_timeout_seconds is None
            else max(0.0, float(total_timeout_seconds))
        )
        per_actor_budget = total_budget / fast_actor_count if fast_actor_count else 0.0
        timeout_scale = per_actor_budget / base_timeout_total if base_timeout_total else 0.0
        request_timeout = (
            max(0.001, float(timeout[0]) * timeout_scale),
            max(0.001, float(timeout[1]) * timeout_scale),
        )
        actors = {}
        for name in selected:
            if cls.bridge_entrypoint_by_name.get(name) != "bridge_fast":
                actors[name] = {"state": "not_applicable", "terminal": True}
                continue
            correlation_id = new_correlation_id()
            try:
                response = requests.request(
                    "POST", cls.get_agent_url(name) + "/post_cancel_movement",
                    timeout=request_timeout,
                    headers={**cls.headers, CORRELATION_HEADER: correlation_id},
                    json={"reason": reason},
                )
                payload = response.json()
                terminal = (
                    isinstance(payload, dict) and payload.get("terminal") is True
                    and 200 <= response.status_code < 300
                )
                actors[name] = {
                    "state": "terminal" if terminal else "not_terminal",
                    "terminal": terminal,
                    "status_code": response.status_code,
                    "cancel_requested": (
                        payload.get("cancel_requested") is True
                        if isinstance(payload, dict) else False
                    ),
                }
            except Exception as error:
                actors[name] = {
                    "state": "request_failed",
                    "terminal": False,
                    "error_class": type(error).__name__,
                }
        return {
            "reason": reason,
            "actors": actors,
            "terminal": all(item["terminal"] for item in actors.values()),
        }

    @classmethod
    def empty_bridge_cleanup_result(cls) -> dict:
        return {
            "processes": {},
            "process_retention": {
                "capacity": BRIDGE_CLEANUP_PROCESS_DIAGNOSTIC_LIMIT,
                "retained": 0,
                "truncated": False,
                "dropped_count": 0,
            },
            "incomplete_process_count": 0,
            "cleanup_complete": True,
        }

    @classmethod
    def kill(
        cls,
        *,
        terminate_grace_seconds: float = DEFAULT_BRIDGE_TERMINATE_GRACE_SECONDS,
        kill_grace_seconds: float = DEFAULT_BRIDGE_KILL_GRACE_SECONDS,
    ) -> dict:
        if (
            not cls.agent_process
            and not cls.runtime_paths_by_name
            and not cls.name2port
            and cls.last_bridge_cleanup is not None
        ):
            return cls.last_bridge_cleanup
        process_results = {}
        process_count = 0
        incomplete_process_count = 0
        paths_snapshot = dict(cls.runtime_paths_by_name)

        def stage(*, budget_seconds=None):
            return {
                "attempted": False,
                "completed": False,
                "timed_out": False,
                "budget_seconds": budget_seconds,
                "started_monotonic_ns": None,
                "completed_monotonic_ns": None,
                "elapsed_ns": None,
                "error_type": None,
                "error_text": None,
                "returncode": None,
            }

        def begin(item):
            item["attempted"] = True
            item["started_monotonic_ns"] = time.monotonic_ns()

        def finish(item):
            item["completed_monotonic_ns"] = time.monotonic_ns()
            item["elapsed_ns"] = (
                item["completed_monotonic_ns"] - item["started_monotonic_ns"]
            )

        def fail(item, error):
            item["error_type"] = type(error).__name__
            item["error_text"] = "operation_failed"

        for name, process in tuple(cls.agent_process.items()):
            process_count += 1
            pid = getattr(process, "pid", None)
            metadata = {
                "pid": pid,
                "process_group_id": None,
                "session_id": None,
                "identity_collection_errors": [],
                "initial_poll": stage(),
                "terminate": stage(),
                "terminate_wait": stage(
                    budget_seconds=terminate_grace_seconds
                ),
                "post_terminate_poll": stage(),
                "kill": stage(),
                "kill_wait": stage(budget_seconds=kill_grace_seconds),
                "final_poll": stage(),
                "exit_code": None,
                "terminated": False,
                "killed": False,
                "alive_after_kill": False,
            }
            initial_poll = None
            begin(metadata["initial_poll"])
            try:
                initial_poll = process.poll()
                metadata["initial_poll"]["returncode"] = initial_poll
                metadata["initial_poll"]["completed"] = True
            except Exception as error:
                fail(metadata["initial_poll"], error)
            finally:
                finish(metadata["initial_poll"])
            if initial_poll is None:
                for field_name, collector in (
                    ("process_group_id", getattr(os, "getpgid", None)),
                    ("session_id", getattr(os, "getsid", None)),
                ):
                    if not callable(collector) or not isinstance(pid, int):
                        continue
                    try:
                        metadata[field_name] = collector(pid)
                    except (OSError, ValueError) as error:
                        metadata["identity_collection_errors"].append({
                            "field": field_name,
                            "error_type": type(error).__name__,
                        })
                begin(metadata["terminate"])
                try:
                    process.terminate()
                    metadata["terminate"]["completed"] = True
                    metadata["terminated"] = True
                    cls.record_bridge_diagnostic(
                        name, "bridge_process_terminate_sent", actor=name,
                        endpoint_identity=f"actor:{name}", pid=pid,
                    )
                except Exception as error:
                    fail(metadata["terminate"], error)
                finally:
                    finish(metadata["terminate"])

                begin(metadata["terminate_wait"])
                try:
                    metadata["terminate_wait"]["returncode"] = process.wait(
                        timeout=terminate_grace_seconds
                    )
                    metadata["terminate_wait"]["completed"] = True
                except subprocess.TimeoutExpired:
                    metadata["terminate_wait"]["timed_out"] = True
                except Exception as error:
                    fail(metadata["terminate_wait"], error)
                finally:
                    finish(metadata["terminate_wait"])

                post_terminate_poll = None
                begin(metadata["post_terminate_poll"])
                try:
                    post_terminate_poll = process.poll()
                    metadata["post_terminate_poll"]["returncode"] = (
                        post_terminate_poll
                    )
                    metadata["post_terminate_poll"]["completed"] = True
                except Exception as error:
                    fail(metadata["post_terminate_poll"], error)
                finally:
                    finish(metadata["post_terminate_poll"])

                if post_terminate_poll is None:
                    begin(metadata["kill"])
                    try:
                        process.kill()
                        metadata["kill"]["completed"] = True
                        metadata["killed"] = True
                        cls.record_bridge_diagnostic(
                            name, "bridge_process_kill_sent", actor=name,
                            endpoint_identity=f"actor:{name}", pid=pid,
                        )
                    except Exception as error:
                        fail(metadata["kill"], error)
                    finally:
                        finish(metadata["kill"])

                    begin(metadata["kill_wait"])
                    try:
                        metadata["kill_wait"]["returncode"] = process.wait(
                            timeout=kill_grace_seconds
                        )
                        metadata["kill_wait"]["completed"] = True
                    except subprocess.TimeoutExpired:
                        metadata["kill_wait"]["timed_out"] = True
                    except Exception as error:
                        fail(metadata["kill_wait"], error)
                    finally:
                        finish(metadata["kill_wait"])

            final_poll = None
            begin(metadata["final_poll"])
            try:
                final_poll = process.poll()
                metadata["final_poll"]["returncode"] = final_poll
                metadata["final_poll"]["completed"] = True
            except Exception as error:
                fail(metadata["final_poll"], error)
            finally:
                finish(metadata["final_poll"])
            metadata["exit_code"] = final_poll
            metadata["alive_after_kill"] = final_poll is None
            cls.record_bridge_diagnostic(
                name,
                "bridge_process_still_alive" if metadata["alive_after_kill"]
                else "bridge_process_exited",
                actor=name, endpoint_identity=f"actor:{name}",
                pid=pid, exit_code=final_poll,
                result="alive" if metadata["alive_after_kill"] else "exited",
            )
            if metadata["alive_after_kill"]:
                incomplete_process_count += 1
            if len(process_results) < BRIDGE_CLEANUP_PROCESS_DIAGNOSTIC_LIMIT:
                process_results[name] = metadata
            elif metadata["alive_after_kill"]:
                completed_name = next((
                    retained_name
                    for retained_name, retained in process_results.items()
                    if not retained["alive_after_kill"]
                ), None)
                if completed_name is not None:
                    process_results.pop(completed_name)
                    process_results[name] = metadata

        cleanup_result = {
            "processes": process_results,
            "process_retention": {
                "capacity": BRIDGE_CLEANUP_PROCESS_DIAGNOSTIC_LIMIT,
                "retained": len(process_results),
                "truncated": process_count > len(process_results),
                "dropped_count": process_count - len(process_results),
            },
            "incomplete_process_count": incomplete_process_count,
            "cleanup_complete": incomplete_process_count == 0,
        }
        cls.last_bridge_cleanup = cleanup_result
        cls.last_bridge_diagnostics = cls.bridge_diagnostics_summary(paths_snapshot)
        cls._close_bridge_diagnostic_recorders()
        if not cleanup_result["cleanup_complete"]:
            raise MinecraftBridgeCleanupError(
                "Minecraft bridge subprocess cleanup did not complete",
                cleanup_result=cleanup_result,
            )

        with cls._url_registry_lock:
            for name in set(cls.runtime_paths_by_name) | set(cls.name2port):
                cls.runtime_paths_by_name.pop(name, None)
                cls.name2port.pop(name, None)
                cls.bridge_entrypoint_by_name.pop(name, None)
            cls.agent_process.clear()
        with cls._action_log_locks_guard:
            cls._action_log_locks.clear()
        cls.last_tool_timeout = None
        return cleanup_result

    # @tool
    # @timeit
    # def getMsg(player_name: str):
    #     """Get the Message from the Server"""
    #     url = Agent.get_agent_url(player_name) + "/post_msg"
    #     response = _minecraft_request("POST", url, headers=Agent.headers)
    #     return response.json()

    @tool
    @timeit
    def erectDirtLadder(player_name: str, top_x, top_y, top_z, emotion: list, murmur: str):
        """Helpful to place item at higher place Erect a Dirt Ladder Structure at Specific Position x y z, remember to dismantle it after use"""
        url = Agent.get_agent_url(player_name) + "/post_erect"
        data = {
            "top_x": top_x,
            "top_y": top_y,
            "top_z": top_z,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()
    
    @tool
    @timeit
    def dismantleDirtLadder(player_name: str, top_x, top_y, top_z, emotion: list, murmur: str):
        """Dismantle a Dirt Ladder Structure from ground to top at Specific Position x y z"""
        url = Agent.get_agent_url(player_name) + "/post_dismantle"
        data = {
            "top_x": top_x,
            "top_y": top_y,
            "top_z": top_z,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def layDirtBeam(player_name: str, x_1, y_1, z_1, x_2, y_2, z_2, emotion: list, murmur: str):
        """Lay a Dirt Beam from Position x1 y1 z1 to Position x2 y2 z2"""
        url = Agent.get_agent_url(player_name) + "/post_lay"
        data = {
            "x_1": x_1,
            "y_1": y_1,
            "z_1": z_1,
            "x_2": x_2,
            "y_2": y_2,
            "z_2": z_2,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()
    
    @tool
    @timeit
    def removeDirtBeam(player_name: str, x_1, y_1, z_1, x_2, y_2, z_2, emotion: list, murmur: str):
        """Remove a Dirt Beam from Position x1 y1 z1 to Position x2 y2 z2"""
        url = Agent.get_agent_url(player_name) + "/post_remove"
        data = {
            "x_1": x_1,
            "y_1": y_1,
            "z_1": z_1,
            "x_2": x_2,
            "y_2": y_2,
            "z_2": z_2,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()


    @tool
    @timeit
    def scanNearbyEntities(player_name: str, item_name: str, radius: int, item_num: int, emotion: list, murmur: str):
        """Find minecraft item blocks chests creatures in a radius, return ('message': msg, 'status': True/False, 'data':[('x':x,'y':y,'z':z),...]) This function can not find items in the chest, container,or player's inventory."""
        url = Agent.get_agent_url(player_name) + "/post_find"
        data = {
            "name": item_name.lower().replace(" ", "_"),
            "distance": radius,
            "count": item_num,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def handoverBlock(player_name: str, target_player_name: str, item_name: str, item_count: int, emotion: list, murmur: str):
        """Hand Item to a target player you work with, return ('message': msg, 'status': True/False), item num will be automatically checked and player will automatically move to the target player"""
        url = Agent.get_agent_url(player_name) + "/post_hand"
        data = {
            "item_name": item_name.lower().replace(" ", "_"),
            "from_name": player_name, 
            "target_name": target_player_name,
            "item_count": item_count,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def navigateToPlayer(player_name: str, target_name: str, emotion: list, murmur: str):
        """Move to a target Player,return ('message': msg, 'status': True/False)"""
        url = Agent.get_agent_url(player_name) + "/post_move_to"
        data = {
            "name": target_name,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def navigateToBuilding(player_name: str, building_name: str, emotion: list, murmur: str):
        """Move to a building by name, return string result"""
        url = Agent.get_agent_url(player_name) + "/post_move_to"
        data = {
            "name": building_name,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def navigateToAnimal(player_name: str, animal_name: str, emotion: list, murmur: str):
        """Move to an animal by name, return string result"""
        url = Agent.get_agent_url(player_name) + "/post_move_to"
        data = {
            "name": animal_name,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def navigateTo(player_name: str, x: int, y: int, z: int, emotion: list, murmur: str):
        """Move to a Specific Position x y z, return string result"""
        url = Agent.get_agent_url(player_name) + "/post_move_to_pos"
        data = {
            "x": x,
            "y": y,
            "z": z,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()
    
    def _navigateTo(player_name: str, x: int, y: int, z: int):
        """Move to a Specific Position x y z, return string result"""
        url = Agent.get_agent_url(player_name) + "/post_move_to_pos"
        data = {
            "x": x,
            "y": y,
            "z": z,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def useItemOnEntity(player_name: str, item_name: str, entity_name: str, emotion: list, murmur: str):
        """Use a Specific Item on a Specific Entity, return string result (bone on dog, bucket on cow, shears on sheep, saddle on horse, etc)"""
        url = Agent.get_agent_url(player_name) + "/post_use_on"
        data = {
            "item_name": item_name.lower().replace(" ", "_"),
            "entity_name": entity_name,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()
    
    @tool
    @timeit
    def useItemOnBlock(player_name: str, item_name: str, x: int, y: int, z: int, emotion: list, murmur: str):
        """Use a Specific Item on a Specific block at x y z, return string result (minecaft on rail, hoe on dirt, seeds on farmland, bucket on water, etc)"""
        url = Agent.get_agent_url(player_name) + "/post_use_on_block"
        data = {
            "item_name": item_name.lower().replace(" ", "_"),
            "x": x,
            "y": y,
            "z": z,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def sleep(player_name: str, emotion: list, murmur: str):
        """Go to Sleep"""
        url = Agent.get_agent_url(player_name) + "/post_sleep"
        response = _minecraft_request("POST", url, headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def wake(player_name: str, emotion: list, murmur: str):
        """Wake Up"""
        url = Agent.get_agent_url(player_name) + "/post_wake"
        response = _minecraft_request("POST", url, headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def MineBlock(player_name: str, x: int, y: int, z: int, emotion: list, murmur: str):
        """Dig Block at Specific Position x y z"""
        url = Agent.get_agent_url(player_name) + "/post_dig"
        data = {
            "x": x,
            "y": y,
            "z": z,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def placeBlock(player_name: str, item_name: str, x: int, y: int, z: int, facing: str, emotion: list, murmur: str):
        """Place a Specific Item at Specific Position x y z with Specific facing in one of [W, E, S, N, x, y, z, A] default is 'A'., return ('message': msg, 'status': True/False)"""
        url = Agent.get_agent_url(player_name) + "/post_place"
        data = {
            "item_name": item_name.lower().replace(" ", "_"),
            "x": x,
            "y": y,
            "z": z,
            "facing": facing,
        }            
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()
    @tool
    @timeit
    def attackTarget(player_name: str, target_name: str, emotion: list = ['😢'], murmur: str=""):
        """Attack the Nearest Entity with a Specific Name"""
        url = Agent.get_agent_url(player_name) + "/post_attack"
        data = {
            "name": target_name.lower().replace(" ", "_"),
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def equipItem(player_name: str, slot: str, item_name: str, emotion: list, murmur: str):
        """Equip a Specific Item on a Specific Slot | to equip item on hand,head,torso,legs,feet."""
        url = Agent.get_agent_url(player_name) + "/post_equip"
        data = {
            "slot": slot,
            "item_name": item_name.lower().replace(" ", "_"),
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def tossItem(player_name: str, item_name: str, count: int, emotion: list, murmur: str):
        """Throw a Specific Item Out with a Specific Count"""
        url = Agent.get_agent_url(player_name) + "/post_toss"
        data = {
            "item_name": item_name.lower().replace(" ", "_"),
            "count": count,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def get_environment_info(player_name: str, emotion: list, murmur: str):
        """Get the Environment Information, return string contains time of day, weather"""
        url = Agent.get_agent_url(player_name) + "/post_environment"
        response = _minecraft_request("POST", url, headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def get_entity_info(player_name: str, target_name: str, emotion: list, murmur: str):
        """Get the Entity Information, return string contains entity name, entity pos x y z, entity held item"""
        url = Agent.get_agent_url(player_name) + "/post_entity"
        data = {
            "name": target_name.lower().replace(" ", "_"),
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def withdrawItem(player_name: str, item_name: str, from_name: str, item_count: int, emotion: list, murmur: str):
        """Take out Item from nearest 'chest' | 'container' | 'furnace' return string result"""
        url = Agent.get_agent_url(player_name) + "/post_get"
        data = {
            "item_name": item_name.lower().replace(" ", "_"),
            "from_name": from_name.lower().replace(" ", "_"),
            "item_count": item_count,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def storeItem(player_name: str, item_name: str, to_name: str, item_count: int, emotion: list, murmur: str):
        """Put in Item to One Chest, Container, etc, return string result"""
        url = Agent.get_agent_url(player_name) + "/post_put"
        data = {
            "item_name": item_name.lower().replace(" ", "_"),
            "to_name": to_name.lower().replace(" ", "_"),
            "item_count": item_count,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def SmeltingCooking(player_name: str, item_name: str, item_count: int, fuel_item_name: str, emotion: list, murmur: str):
        """Smelt or Cook Item in the Furnace, item_name is the item to be smelted, item_count is the number of items to be smelted, fuel_item_name is the fuel item."""
        url = Agent.get_agent_url(player_name) + "/post_smelt"
        data = {
            "item_name": item_name.lower().replace(" ", "_"),
            "item_count": item_count,
            "fuel_item_name": fuel_item_name,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def craftBlock(player_name: str, item_name: str, count: int, emotion: list, murmur: str):
        """Craft Item in the Crafting Table"""
        url = Agent.get_agent_url(player_name) + "/post_craft"
        data = {
            "item_name": item_name.lower().replace(" ", "_"),
            "count": count,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def enchantItem(player_name: str, item_name: str, count: int, emotion: list, murmur: str):
        """Enchant Item in the Enchanting Table"""
        url = Agent.get_agent_url(player_name) + "/post_enchant"
        data = {
            "item_name": item_name.lower().replace(" ", "_"),
            "count": count,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def trade(player_name: str, item_name: str, with_name: str, count: int, emotion: list, murmur: str):
        """Trade Item with the villager npc, return the details of trade items and num."""
        url = Agent.get_agent_url(player_name) + "/post_trade"
        data = {
            "item_name": item_name.lower().replace(" ", "_"),
            "with_name": with_name,
            "count": count,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def repairItem(player_name: str, item_name: str, material: str, emotion: list, murmur: str):
        """Repair Item in the Anvil"""
        url = Agent.get_agent_url(player_name) + "/post_repair"
        data = {
            "item_name": item_name.lower().replace(" ", "_"),
            "material": material.lower().replace(" ", "_"),
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def eat(player_name: str, item_name: str, emotion: list, murmur: str):
        """Eat Item"""
        url = Agent.get_agent_url(player_name) + "/post_eat"
        data = {
            "item_name": item_name.lower().replace(" ", "_"),
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def drink(player_name: str, item_name: str, count: int, emotion: list, murmur: str):
        """Drink Item"""
        url = Agent.get_agent_url(player_name) + "/post_drink"
        data = {
            "item_name": item_name.lower().replace(" ", "_"),
            "count": count,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def wear(player_name: str, slot: str, item_name: str, emotion: list, murmur: str):
        """Wear Item on Specific Slot"""
        url = Agent.get_agent_url(player_name) + "/post_wear"
        data = {
            "slot": slot,
            "item_name": item_name.lower().replace(" ", "_"),
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()
    
    @tool
    @timeit
    def openContainer(player_name: str, container_name: str, position: list, emotion: list, murmur: str):
        """Open the nearest or at [x, y, z] 'chest' | 'container' | 'furnace' position is optional, return ('message': msg, 'status': True/False, 'data':[('name':name, 'count':count),...])"""
        if position != [0, 0, 0] and position != []:
            response = Agent._navigateTo(player_name, position[0], position[1], position[2])
            if response["status"] == False:
                return response
        url = Agent.get_agent_url(player_name) + "/post_open"
        data = {
            "item_name": container_name,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()


    @tool
    @timeit
    def fetchContainerContents(player_name: str, item_name: str, position: list, emotion: list, murmur: str):
        """Get the details of item_name at [x, y, z] 'chest' | 'container' | 'furnace', arg position is [x, y, z], return ('message': msg, 'status': True/False, 'data':[('name':name, 'count':count),...])"""
        if item_name not in ["chest", "inventory", "furnace", "container"]:
            return {'data': [], 'message': 'Failed item name not in ["chest", "inventory", "furnace", "container"]', 'status': False}
        if position != [0, 0, 0] and position != []:
            response = Agent._navigateTo(player_name, position[0], position[1], position[2])
            if response["status"] == False:
                return response
        url = Agent.get_agent_url(player_name) + "/post_open"
        data = {
            "item_name": item_name,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def closeContainer(player_name: str, item_name: str, position: list, emotion: list, murmur: str):
        """Close 'chest' | 'container' | 'furnace' at [x, y, z]"""
        if position != [0, 0, 0] and position != []:
            response = Agent._navigateTo(player_name, position[0], position[1], position[2])
            if response["status"] == False:
                return response
        url = Agent.get_agent_url(player_name) + "/post_close"
        data = {
            "item_name": item_name,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def ToggleAction(player_name: str, item_name: str, x: int, y: int, z: int, emotion: list, murmur: str):
        """open/close Gate, Lever, Press Button (pressure_plate need to stand on it, iron door need to be powered, they are not included), at Specific Position x y z"""
        if "plate" in item_name:
            return {'message': "pressure_plate need to stand on it", 'status': False}
        url = Agent.get_agent_url(player_name) + "/post_activate"
        data = {
            "item_name": item_name.lower().replace(" ", "_"),
            "x": x,
            "y": y,
            "z": z,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def mountEntity(player_name: str, entity_name: str, emotion: list = ['🏇','😊'], murmur: str=""):
        """Mount the Entity"""
        url = Agent.get_agent_url(player_name) + "/post_mount"
        data = {
            "entity_name": entity_name,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def dismountEntity(player_name: str, emotion: list, murmur: str):
        """Dismount the Entity"""
        url = Agent.get_agent_url(player_name) + "/post_dismount"
        response = _minecraft_request("POST", url, headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def rideEntity(player_name: str, entity_name: str, emotion: list, murmur: str):
        """Ride the Entity"""
        url = Agent.get_agent_url(player_name) + "/post_ride"
        data = {
            "entity_name": entity_name,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def disrideEntity(player_name: str, emotion: list, murmur: str):
        """Disride the Entity"""
        url = Agent.get_agent_url(player_name) + "/post_disride"
        response = _minecraft_request("POST", url, headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def talkTo(player_name: str, entity_name: str, message: str, emotion: list = ["😊"]):
        """Talk to the Entity with Emojis, entity_name is the name of other player.
        """
        # Agent._lookAt(player_name, entity_name) # 容易出现问题

        if entity_name == "nobody" or entity_name == "anyone" or entity_name == "everyone" or entity_name == "all" \
            or entity_name == "somebody" or entity_name == "some" or entity_name == "any" or entity_name == ""\
            or entity_name == "none" or entity_name == "everybody" or entity_name == "someone" or entity_name == "anybody":
            return {'message': 'You need to specify the other player name.', 'status': False, 'new_events': []}
        url = Agent.get_agent_url(player_name) + "/post_talk_to"
        data = {
            "entity_name": entity_name,
            "message": message,
            "emotion": emotion,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()
    
    @tool
    @timeit
    def waitForFeedback(player_name: str, entity_name: str, seconds: int=10, emotion: list = ["⏱️"], murmur: str=""):
        """Wait for other player's reply, except you or others are expecting to end the conversation."""
        url = Agent.get_agent_url(player_name) + "/post_wait_for_feedback"
        data = {
            "entity_name": entity_name,
            "seconds": seconds,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def performMovement(player_name: str, action_name: str, seconds: int, emotion: list, murmur: str):
        """Perform Action jump forward back left right for Seconds"""
        url = Agent.get_agent_url(player_name) + "/post_action"
        data = {
            "action_name": action_name,
            "seconds": seconds,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def lookAt(player_name: str, name: str, emotion: list, murmur: str):
        """Look at Someone or Something"""
        url = Agent.get_agent_url(player_name) + "/post_look_at"
        data = {
            "name": name,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    def _lookAt(player_name: str, name: str):
        """Look at Someone or Something"""
        url = Agent.get_agent_url(player_name) + "/post_look_at"
        data = {
            "name": name,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def startFishing(player_name: str, fish_name: str, emotion: list, murmur: str):
        """Start Fishing"""
        url = Agent.get_agent_url(player_name) + "/post_start_fishing"
        data = {
            "fish_name": fish_name,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def stopFishing(player_name: str, emotion: list, murmur: str):
        """Stop Fishing"""
        url = Agent.get_agent_url(player_name) + "/post_stop_fishing"
        response = _minecraft_request("POST", url, headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def read(player_name: str, item_name: str, emotion: list, murmur: str):
        """Read Book or Sign neaby, return string details"""
        url = Agent.get_agent_url(player_name) + "/post_read"
        data = {
            "name": item_name,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def readPage(player_name: str, item_name: str, page: int, emotion: list, murmur: str):
        """Read Content from Book Page"""
        url = Agent.get_agent_url(player_name) + "/post_read_page"
        data = {
            "name": item_name,
            "page": page,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    @tool
    @timeit
    def write(player_name: str, item_name: str, content: str, emotion: list, murmur: str):
        """Write Content on Writable Book or Sign"""
        url = Agent.get_agent_url(player_name) + "/post_write"
        data = {
            "name": item_name,
            "content": content,
        }
        response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
        return response.json()

    def update_history(self, response):
        self.action_history.append(response)
        atomic_write_json(
            self.reflection_output_dir / f"{self.name}_history.json",
            self.action_history,
        )

    def _save_interaction_history(self, response, action_list, final_answer):
        atomic_write_json(
            self.runtime_paths.history_dir / f"{hash(response['input'])}.json",
            {
                "input": response["input"],
                "action_list": action_list,
                "final_answer": final_answer,
            },
        )

    def step(self, instruction: str, actions=[], observations=[], player_name_list=[], max_try_turn=2, max_iterations=1, tools=[], recommended_actions=[]):
        # return the (action, observation), details.
        if not self.api_key_list:
            raise RuntimeError(
                "Minecraft Agent has no API keys configured; set Agent.api_key_list before calling 'step()'"
            )
        if getattr(Agent, "provider", "") == "ollama":
            self.llm = OllamaReasoningChatOpenAI(model=self.model, temperature=0, max_tokens=1024, openai_api_key=random.choice(Agent.api_key_list), base_url=Agent.base_url)
        elif 'qwen' in self.model:
            from langchain_community.chat_models.tongyi import ChatTongyi
            self.llm = ChatTongyi(model=self.model, temperature=0, max_tokens=256, dashscope_api_key=random.choice(Agent.api_key_list), base_url=Agent.base_url,model_kwargs={"enable_thinking": False})
        elif "default" in self.model:
            from langchain.llms import OpenAI
            self.llm = OpenAI(model=self.model, temperature=0, max_tokens=256, openai_api_key=random.choice(Agent.api_key_list), base_url=Agent.base_url)
        elif "deepseek" in self.model:
            from openai import OpenAI
            self.llm = OpenAI(model=self.model, temperature=0, max_token=256, openai_api_key=random.choice(Agent.api_key_list), base_url=Agent.base_url)
        elif "instruct" in self.model and "gpt" in self.model:
            from langchain.llms import OpenAI
            self.llm = OpenAI(model=self.model, temperature=0, max_tokens=256, openai_api_key=random.choice(Agent.api_key_list), base_url=Agent.base_url)
        elif "gpt" in self.model:
            from langchain.chat_models import ChatOpenAI
            self.llm = ChatOpenAI(model=self.model, temperature=0,  max_tokens=256, openai_api_key=random.choice(Agent.api_key_list), base_url=Agent.base_url)
        elif "glm" in self.model:
            from zhipu import ChatZhipuAI
            self.llm = ChatZhipuAI(model_name=self.model, temperature=0.01, api_key=random.choice(Agent.api_key_list))
        else:
            raise ValueError(
                f"Unsupported Minecraft Agent model {self.model!r} for 'step()'; "
                "expected qwen, default, deepseek, instruct-gpt, gpt, or glm"
            )
        # elif "default" in self.model:
        #     from openai import OpenAI
        #     self.llm = OpenAI(model=self.model, openai_api_key=random.choice(Agent.api_key_list), base_url=Agent.base_url, model_kwargs={"encoding": "utf-8"})
        
        for act, obs in zip(actions, observations):
            instruction += f"\n{act['log']}\n{obs}"
        
        # Caller tools are selectors only. Resolve names against the registered
        # capability objects so raw inputs cannot bypass VillagerBench wrappers.
        selected_tools = self.tools
        if tools:
            requested_names = {
                getattr(requested_tool, "name", None) for requested_tool in tools
            }
            selected_tools = [
                registered_tool for registered_tool in self.tools
                if registered_tool.name in requested_names
            ]
        recommended_tools = (
            [registered_tool for registered_tool in selected_tools
             if registered_tool.name in recommended_actions]
            if recommended_actions else selected_tools
        )
        llmhandler = LLMHandler()

        while max_try_turn > 0:
            random.shuffle(self.tools)
            agent = initialize_agent(
                tools=recommended_tools,
                llm=self.llm,
                verbose=Agent.verbose,
                agent=AgentType.STRUCTURED_CHAT_ZERO_SHOT_REACT_DESCRIPTION,
                return_intermediate_steps=True,
                max_execution_time=120,  # seconds
                max_iterations=1,  # 决定了最大的迭代次数
                callback_manager=CallbackManager(handlers=[llmhandler]),
            )
            agent.handle_parsing_errors = True
            response = None
            try:
                if len(player_name_list) == 0:
                    task = f"Your name is {self.name}.\n{instruction}"
                    response = agent({"input": filter_emoji(task)})
                else:
                    task = f"You should control {player_name_list} work together. \n{instruction}"
                    response = agent({"input": filter_emoji(task)})
                break
            except KeyboardInterrupt:
                logging.info("KeyboardInterrupt")
                raise KeyboardInterrupt
            except ConnectionError as e:
                logging.info(filter_emoji(str(e)))
                raise ConnectionError
            except ConnectionRefusedError as e:
                logging.info(filter_emoji(str(e)))
                raise ConnectionRefusedError
            except (AgentExecutionCancelledError, ToolActionBlockedError,
                    MinecraftToolEffectUnknownError, MinecraftToolTimeoutError):
                raise
            except Exception as e:
                print(filter_emoji(str(e)))
                print("retrying...")
                time.sleep(1)
                max_try_turn -= 1
        response = filter_emoji_from_dict(response)
        if response is None:
            return (None, None), {"input": f"Your name is {self.name}.\n{instruction}", "action_list": [],
                                                "final_answer": "The task execute failed.", "chain_input": llmhandler.chain_input, "seralized_input": llmhandler.seralized_input}
        # print(response)
        # print(dumps(response, pretty=True),type(dumps(response, pretty=True)))
        action_list = []
        response = json.loads(dumps(response, pretty=True))
        for step in response["intermediate_steps"]:
            action_list.append({"action": step[0]["kwargs"], "feedback": step[1]})
        _log_action_diagnostics(self.name, "step", response, action_list, llmhandler)
        
        if len(action_list) == 0:
            return (None, None), {"input": f"Your name is {self.name}.\n{instruction}", "action_list": [],
                                                "final_answer": "The task execute failed.", "chain_input": llmhandler.chain_input, "seralized_input": llmhandler.seralized_input}
    

        final_answer = response["output"]
        # save the action_list and final_answer

        self._save_interaction_history(response, action_list, final_answer)
        action = action_list[0]
        return (action['action'], action["feedback"]), {"input": response["input"], "action_list": action_list, "final_answer": final_answer}

    def run(self, instruction: str, player_name_list=[], max_try_turn=10, max_iterations=5, tools=[],
            cancellation_token=None, phase_callback=None):
        # print(f"Your name is {self.name}. \n{instruction}")
        if not self.api_key_list:
            raise RuntimeError(
                "Minecraft Agent has no API keys configured; set Agent.api_key_list before calling 'run()'"
            )
        # dynamic api key

        if getattr(Agent, "provider", "") == "ollama":
            self.llm = OllamaReasoningChatOpenAI(model=self.model, temperature=0, max_tokens=1024, openai_api_key=random.choice(Agent.api_key_list), base_url=Agent.base_url)
        elif 'qwen' in self.model:
            from langchain_community.chat_models.tongyi import ChatTongyi
            self.llm = ChatTongyi(model=self.model, temperature=0, max_tokens=256, dashscope_api_key=random.choice(Agent.api_key_list), base_url=Agent.base_url,model_kwargs={"enable_thinking": False})
        elif "default" in self.model:
            from langchain.llms import OpenAI
            self.llm = OpenAI(model=self.model, temperature=0, max_tokens=256, openai_api_key=random.choice(Agent.api_key_list), base_url=Agent.base_url)
        elif ("instruct" in self.model and "gpt" in self.model):
            from langchain.llms import OpenAI
            self.llm = OpenAI(model=self.model, temperature=0, max_tokens=256, openai_api_key=random.choice(Agent.api_key_list), base_url=Agent.base_url)
        elif "gpt" in self.model or "NAS" in self.model or "llama" in self.model:
            from langchain.chat_models import ChatOpenAI
            self.llm = ChatOpenAI(model=self.model, temperature=0,  max_tokens=256, openai_api_key=random.choice(Agent.api_key_list), base_url=Agent.base_url)
        elif "gemini" in self.model:
            from langchain_google_genai import ChatGoogleGenerativeAI
            self.llm = ChatGoogleGenerativeAI(model=self.model, temperature=0, google_api_key=random.choice(Agent.api_key_list))
        elif "glm" in self.model:
            from zhipu import ChatZhipuAI
            self.llm = ChatZhipuAI(model_name=self.model, temperature=0.01, api_key=random.choice(Agent.api_key_list))
        elif "deepseek" in self.model:
            from langchain.chat_models import ChatOpenAI
            self.llm = ChatOpenAI(model=self.model, temperature=0,  max_tokens=256, openai_api_key=random.choice(Agent.api_key_list), base_url=Agent.base_url)
        else:
            raise ValueError(
                f"Unsupported Minecraft Agent model {self.model!r} for 'run()'; expected ollama provider "
                "or a qwen, default, instruct-gpt, gpt, NAS, llama, gemini, glm, or deepseek model"
            )
        # 这个地方是定义的agent的类型，初始化位置的agent没有被使用
        if callable(phase_callback):
            phase_callback("before_agent_invocation")
        check_agent_cancellation(cancellation_token, phase="before_agent_invocation")
        while max_try_turn > 0:
            if callable(phase_callback):
                phase_callback("before_retry")
            check_agent_cancellation(cancellation_token, phase="before_retry")
            random.shuffle(self.tools)
            llmhandler = LLMHandler()
            cancellation_handler = CancellationCallbackHandler(cancellation_token, phase_callback)
            agent = initialize_agent(
                tools=self.tools if len(tools) == 0 else tools,
                llm=self.llm,
                verbose=Agent.verbose,
                agent=AgentType.STRUCTURED_CHAT_ZERO_SHOT_REACT_DESCRIPTION,
                return_intermediate_steps=True,
                max_execution_time=120,  # seconds
                max_iterations=max_iterations,  # 决定了最大的迭代次数
                callback_manager=CallbackManager(handlers=[llmhandler, cancellation_handler]),
            )
            agent.handle_parsing_errors = True
            response = None
            try:
                with get_openai_callback() as cb:
                    start_time = time.time()
                    if len(player_name_list) == 0:
                        task = f"Your name is {self.name}.\n{instruction}"
                        response = agent({"input": filter_emoji(task)})
                    else:
                        task = f"You should control {player_name_list} work together. \n{instruction}"
                        response = agent({"input": filter_emoji(task)})
                    if callable(phase_callback):
                        phase_callback("after_agent_invocation")
                    check_agent_cancellation(cancellation_token, phase="after_agent_invocation")
                    # print(llmhandler.chain_input)
                    # print(llmhandler.seralized_input)

                    end_time = time.time()
                    # save in pipeLine/tokens
                    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    # if 'gpt' in Agent.model:
                    #     from env.utils import parse_token_text
                    #     token_usage = parse_token_text(cb)
                    #     try:
                    #         with open("data/tokens.json", "r") as f:
                    #             tokens = json.load(f)
                    #         tokens["dates"] = current_time
                    #         tokens["tokens_used"] += token_usage["tokens_used"]
                    #         tokens["prompt_tokens"] += token_usage["prompt_tokens"]
                    #         tokens["completion_tokens"] += token_usage["completion_tokens"]
                    #         tokens["successful_requests"] += token_usage["successful_requests"]
                    #         tokens["total_cost"] += token_usage["total_cost"]
                    #         tokens["action_cost"] += end_time - start_time
                    #         with open("data/tokens.json", "w") as f:
                    #             json.dump(tokens, f, indent=4)
                    #     except KeyboardInterrupt:
                    #         logging.info("KeyboardInterrupt")
                    #         raise KeyboardInterrupt
                    #     except Exception as e:
                    #         logging.info(e)
                break
            except KeyboardInterrupt:
                logging.info("KeyboardInterrupt")
                raise KeyboardInterrupt
            except ConnectionError as e:
                logging.info(filter_emoji(str(e)))
                raise ConnectionError
            except ConnectionRefusedError as e:
                logging.info(filter_emoji(str(e)))
                raise ConnectionRefusedError
            except (AgentExecutionCancelledError, ToolActionBlockedError,
                    MinecraftToolEffectUnknownError, MinecraftToolTimeoutError,
                    MinecraftActionLogError):
                raise
            except Exception as e:
                print(filter_emoji(str(e)))
                print("retrying...")
                wait_for_agent_cancellation(cancellation_token, 1)
                check_agent_cancellation(cancellation_token, phase="retry_wait")
                max_try_turn -= 1
        response = filter_emoji_from_dict(response)
        if max_try_turn < 0 or response is None:
            return "The task execute failed.", {"input": f"Your name is {self.name}.\n{instruction}", "action_list": [],
                                                "final_answer": "The task execute failed.", "chain_input": llmhandler.chain_input, "seralized_input": llmhandler.seralized_input}
        # print(response)
        # print(dumps(response, pretty=True),type(dumps(response, pretty=True)))
        action_list = []
        response = json.loads(dumps(response, pretty=True))
        for step in response["intermediate_steps"]:
            action_list.append({"action": step[0]["kwargs"], "feedback": step[1]})
        _log_action_diagnostics(self.name, "run", response, action_list, llmhandler)
        final_answer = response["output"]
        # save the action_list and final_answer


        # print("=== LLM Interaction Log ===")
        # for i, (prompt, llm_output) in enumerate(zip(llmhandler.chain_input, llmhandler.llm_out)):
        #     print(f"Step {i + 1}:")
        #     print(f"Prompt: {prompt}")
        #     print(f"LLM Output: {llm_output}")
        #     print("-" * 40)
        # print("========= End ========")

        check_agent_cancellation(cancellation_token, phase="before_history_persistence")
        self._save_interaction_history(response, action_list, final_answer)
        self.update_history({"input": response["input"], "action_list": action_list, "final_answer": final_answer})
        return final_answer, {"input": response["input"], "action_list": action_list, "final_answer": final_answer}

    def chat(self, msg, async_tag=False):
        url = Agent.get_agent_url(self.name) + "/post_chat"
        data = {
            "msg": msg,
        }
        if async_tag:
            threading.Thread(
                target=_minecraft_request,
                args=("POST", url),
                kwargs={"data": json.dumps(data), "headers": Agent.headers},
                daemon=True,
            ).start()
            return {}
        else:
            time.sleep(.05)
            response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
            return response.json()


if __name__ == "__main__":


    # Agent.model = "gpt-4-1106-preview"
    # agent1 = Agent(name="Alice", local_port=5001, tools=[Agent.equipItem, Agent.startFishing])
    # Agent.base_url = "https://api.chatanywhere.tech/v1"
    # Agent.api_key_list = api_key_list

    Agent.model = "deepseek-chat"
    Agent.base_url =  "https://api.deepseek.com"
    Agent.api_key_list = load_agent_api_key_list()
    agent1 = Agent(name="Alice", local_port=5001, tools=[])
    Agent.launch(host="10.214.180.148", port=25565)
    time.sleep(5)
    start_time = time.time()
    url = Agent.get_agent_url("Alice") + "/post_use_on"
    data = {
        "item_name": "saddle",
        "entity_name": "horse",
    }
    response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
    print(response.json())
    print(time.time() - start_time)
    # # print(Agent.ping("Alice"))
    # url = Agent.get_agent_url("Alice") + "/post_use_on"
    # response = _minecraft_request("POST", url, headers=Agent.headers)
    # data = {
    #     "item_name": "bucket",
    #     "entity_name": "water",
    #     }
    # response = _minecraft_request("POST", url, data=json.dumps(data), headers=Agent.headers)
    # print(response.json)
    # response = Agent.attackTarget({"player_name":"Alice", "target_name":"panda"})
    # from langchain.chat_models import ChatOpenAI
    # llm = ChatOpenAI(model=Agent.model, temperature=0.1, max_tokens=256, openai_api_key=random.choice(Agent.api_key_list), base_url=Agent.base_url)
    # response = llm.invoke("use bone_meal on the large_fern")
    # print(response)
    # Prompt = "You are act as Alice, use bucket on water."
    # agent1.run(Prompt, tools=[Agent.useItemOnEntity])
    # actions = []
    # observations = []
    # while True:
    #     (act, obs), detail = agent1.step(Prompt, actions=actions, observations=observations)
    #     if act == None:
    #         continue
    #     actions.append(act)
    #     observations.append(obs)
    #     input()
    
