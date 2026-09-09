import argparse
from copy import deepcopy
import json
import math
import os
import sys
import time
import inspect
import threading
from dataclasses import dataclass
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4
from env.env import VillagerBench, env_type, Agent
from env.minecraft_client import MinecraftBridgeCleanupError
from model.init_model import init_language_model
from model.ollama_config import make_ollama_llm_config, configure_ollama_agent, load_agent_api_key_list
from env.runtime_paths import RuntimePaths, atomic_write_json
from env.runtime_execution import RuntimeExecution
from env.judger_artifacts import ScoreOwnershipError, validate_score_identity
from env.world_initialization import resolve_world_initialization
from benchmarks.minecraft.position_contract import (
    PositionConvention,
    entity_feet_position,
    resolve_position_convention,
)

start_time = time.time()
from pipeline.controller_tiny import GlobalController
from pipeline.data_manager import DataManager
from pipeline.task_manager import TaskManager


def _task_graph_snapshot(graph) -> dict:
    return {
        "artifact_generation_mutates_runtime": False,
        "mutates_runtime": False,
        "projection": "type_define.Graph compatibility projection",
        "tasks": [task.to_json() for task in getattr(graph, "vertex", [])],
        "edges": [
            {"source": start.description, "target": end.description}
            for start, end in getattr(graph, "edge", [])
        ],
    }


class JudgedRuntimeValidationError(RuntimeError):
    pass


def _safe_collect(collector, *, field_name: str, default):
    try:
        return collector(), None
    except Exception as exc:
        return default, {
            "field": field_name,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }


def _controller_snapshot(controller) -> dict:
    snapshot = {
        "shutdown_complete": bool(getattr(controller, "shutdown_complete", False)),
        "state": getattr(controller, "controller_state", None),
        "context": getattr(controller, "shutdown_context", None),
        "active_assignments": dict(getattr(controller, "assignment", {})),
    }
    provider_collector = getattr(controller, "get_k11_provider_ledger_snapshot", None)
    late_identity = getattr(controller, "_k11_late_cleanup_identity", None)
    context = snapshot["context"]
    diagnostics = context.get("diagnostics") if isinstance(context, dict) else None
    verdict = diagnostics.get("verdict") if isinstance(diagnostics, dict) else None
    if (not callable(provider_collector) or not isinstance(late_identity, dict)
            or not isinstance(verdict, dict)):
        return snapshot
    ledger = {
        "schema_version": "controller-late-execution-ledger/1",
        "captured_at_monotonic_ns": time.monotonic_ns(),
        "groups": {"items": [], "retention": {
            "capacity": 0, "retained": 0, "truncated": False, "dropped_count": 0,
        }},
        "diagnostic_collection_error": {
            "collector": "execution_ledger",
            "error_type": "Unavailable",
        },
    }
    collector = getattr(controller, "snapshot_execution_ledger", None)
    if callable(collector):
        try:
            candidate = collector()
            if isinstance(candidate, dict):
                ledger = candidate
            else:
                ledger["diagnostic_collection_error"] = {
                    "collector": "execution_ledger", "error_type": "MalformedSnapshot",
                }
        except Exception as exc:
            ledger["diagnostic_collection_error"] = {
                "collector": "execution_ledger", "error_type": type(exc).__name__,
            }
    provider_ledger = {
        "schema_version": "k11-execution-provider-ledger/1",
        "captured_at_monotonic_ns": time.monotonic_ns(),
        "operations": {"items": [], "retention": {
            "capacity": 0, "retained": 0, "truncated": False,
            "dropped_count": 0,
        }},
        "unresolved": {"items": [], "retention": {
            "capacity": 0, "retained": 0, "truncated": False,
            "dropped_count": 0,
        }},
        "diagnostic_collection_error": {
            "collector": "provider_ledger", "error_type": "Unavailable",
        },
    }
    try:
        candidate = provider_collector()
        if isinstance(candidate, dict):
            provider_ledger = candidate
        else:
            provider_ledger["diagnostic_collection_error"] = {
                "collector": "provider_ledger", "error_type": "MalformedSnapshot",
            }
    except Exception as exc:
        provider_ledger["diagnostic_collection_error"] = {
            "collector": "provider_ledger", "error_type": type(exc).__name__,
        }
    snapshot.update({
        "execution_ledger": ledger,
        "provider_ledger": provider_ledger,
        "late_movement": {
            "captured_at_monotonic_ns": time.monotonic_ns(),
            "result": deepcopy(getattr(controller, "movement_shutdown_result", None)),
        },
    })
    lifecycle_collector = getattr(controller, "get_k11_late_lifecycle_snapshot", None)
    if callable(lifecycle_collector):
        try:
            candidate = lifecycle_collector()
            snapshot["late_lifecycle_ledger"] = (
                candidate if isinstance(candidate, dict) else {
                    "diagnostic_collection_error": {
                        "collector": "late_lifecycle_ledger",
                        "error_type": "MalformedSnapshot",
                    },
                }
            )
        except Exception as exc:
            snapshot["late_lifecycle_ledger"] = {
                "diagnostic_collection_error": {
                    "collector": "late_lifecycle_ledger",
                    "error_type": type(exc).__name__,
                },
            }
    return snapshot


def _configure_k11_late_diagnostic_sink(
    controller, runtime_result_path, identity,
) -> None:
    provider_collector = getattr(controller, "get_k11_provider_ledger_snapshot", None)
    execution_collector = getattr(controller, "snapshot_execution_ledger", None)
    lifecycle_collector = getattr(controller, "get_k11_late_lifecycle_snapshot", None)
    if (not runtime_result_path or not callable(provider_collector)
            or not callable(execution_collector)
            or not callable(lifecycle_collector)
            or not isinstance(identity, dict)):
        return
    controller._k11_late_cleanup_identity = dict(identity)
    path = Path(runtime_result_path).with_name("k11_late_runtime_diagnostics.json")
    writer_lock = threading.Lock()
    writer_event = threading.Event()
    writer_state = {"thread": None}

    def collect_and_write():
        while True:
            writer_event.clear()
            atomic_write_json(path, {
                "schema_version": "k11-late-runtime-diagnostics/1",
                "identity": dict(identity),
                "captured_at_monotonic_ns": time.monotonic_ns(),
                "execution_ledger": execution_collector(),
                "provider_ledger": provider_collector(),
                "late_lifecycle_ledger": lifecycle_collector(),
                "late_movement": {
                    "captured_at_monotonic_ns": time.monotonic_ns(),
                    "result": deepcopy(
                        getattr(controller, "movement_shutdown_result", None)
                    ),
                },
            })
            with writer_lock:
                if writer_event.is_set():
                    continue
                writer_state["thread"] = None
                return

    def persist_late_diagnostics():
        with writer_lock:
            writer_event.set()
            writer = writer_state["thread"]
            if writer is not None and writer.is_alive():
                return
            writer = threading.Thread(
                target=collect_and_write,
                name="k11-late-diagnostics-writer",
                daemon=False,
            )
            writer_state["thread"] = writer
            writer.start()

    controller._k11_late_diagnostic_sink = persist_late_diagnostics


def _runtime_result(env=None, tm=None, controller=None, *, error: str | None = None, error_type: str | None = None, attempt_id: str | None = None, task_name: str | None = None) -> dict:
    runtime_store = getattr(tm, "runtime_task_store", None) if tm is not None else None
    runtime_snapshot, runtime_error = _safe_collect(
        runtime_store.snapshot if runtime_store is not None else lambda: {},
        field_name="runtime_task_dag_snapshot",
        default={},
    )
    task_graph_snapshot, graph_error = _safe_collect(
        lambda: _task_graph_snapshot(tm.graph) if tm is not None and hasattr(tm, "graph") else {},
        field_name="task_graph_snapshot",
        default={},
    )
    score, score_error = _safe_collect(
        env.get_score if env is not None and hasattr(env, "get_score") else lambda: {},
        field_name="score",
        default={},
    )
    action_log, action_error = _safe_collect(
        env.get_action_log if env is not None and hasattr(env, "get_action_log") else lambda: {},
        field_name="action_log",
        default={},
    )
    controller_snapshot, controller_error = _safe_collect(
        lambda: _controller_snapshot(controller),
        field_name="controller",
        default={},
    )
    collection_errors = [
        item
        for item in (
            runtime_error,
            graph_error,
            score_error,
            action_error,
            controller_error,
        )
        if item is not None
    ]
    try:
        bridge_diagnostics = (
            env.get_minecraft_bridge_diagnostics()
            if env is not None and hasattr(env, "get_minecraft_bridge_diagnostics")
            else {
                "schema_version": "minecraft-bridge-diagnostics-summary/1",
                "actors": {}, "artifacts": {}, "diagnostic_collection_error": None,
            }
        )
    except Exception as diagnostic_error:
        bridge_diagnostics = {
            "schema_version": "minecraft-bridge-diagnostics-summary/1",
            "actors": {},
            "artifacts": {},
            "diagnostic_collection_error": [{
                "error_type": type(diagnostic_error).__name__,
            }],
        }
    return {
        "score": score,
        "attempt_id": attempt_id,
        "task_name": task_name,
        "expected_score_identity": {
            "attempt_id": attempt_id,
            "task_name": task_name,
        },
        "action_log": action_log,
        "agent_iteration_limit": (
            getattr(env, "agent_iteration_limit", None)
            if env is not None
            else None
        ),
        "agent_iteration_limit_source": (
            "VillagerBench.step max_turn"
            if env is not None and getattr(env, "agent_iteration_limit", None) is not None
            else None
        ),
        "runtime_task_dag_snapshot": runtime_snapshot,
        "task_graph_snapshot": task_graph_snapshot,
        "controller": controller_snapshot,
        "minecraft_eac_audit": (
            env.get_eac_audit_artifact()
            if env is not None and hasattr(env, "get_eac_audit_artifact")
            else {"configured": False, "read_only_projection": True}
        ),
        "bridge_cleanup": dict(getattr(env, "bridge_cleanup_result", {}) or {}),
        "runtime_failure_chain": getattr(env, "runtime_failure_chain", None),
        "minecraft_bridge_diagnostics": bridge_diagnostics,
        "collection_errors": collection_errors,
        "error": error,
        "error_type": error_type,
    }


def _runtime_checkpoint_result(env=None, tm=None, controller=None, *, attempt_id: str | None = None, task_name: str | None = None) -> dict:
    result = _runtime_result(None, tm, controller, attempt_id=attempt_id, task_name=task_name)
    if env is not None and hasattr(env, "get_minecraft_bridge_diagnostics"):
        try:
            result["minecraft_bridge_diagnostics"] = env.get_minecraft_bridge_diagnostics()
        except Exception as diagnostic_error:
            result["minecraft_bridge_diagnostics"] = {
                "schema_version": "minecraft-bridge-diagnostics-summary/1",
                "actors": {},
                "artifacts": {},
                "diagnostic_collection_error": [{
                    "error_type": type(diagnostic_error).__name__,
                }],
            }
    if env is not None and hasattr(env, "get_action_log"):
        action_log, action_error = _safe_collect(
            env.get_action_log,
            field_name="action_log",
            default={},
        )
        result["action_log"] = action_log
        if action_error is not None:
            result["collection_errors"].append(action_error)
    if result["collection_errors"]:
        result["checkpoint_collection_error"] = result["collection_errors"][0]
    return result


def _apply_runtime_cleanup_failure(result: dict, error: BaseException) -> dict:
    cleanup_error = error if isinstance(error, MinecraftBridgeCleanupError) else None
    if isinstance(cleanup_error, MinecraftBridgeCleanupError):
        cleanup_failure = {
            "error_type": type(cleanup_error).__name__,
            "cleanup_result": dict(cleanup_error.cleanup_result),
        }
        primary_failure = (
            {"error_type": type(error).__name__}
            if cleanup_error is not error else None
        )
    else:
        failure_chain = result.get("runtime_failure_chain")
        if not isinstance(failure_chain, dict):
            return result
        cleanup_failure = failure_chain.get("cleanup_failure")
        primary_failure = failure_chain.get("primary_failure")
        if not isinstance(cleanup_failure, dict):
            return result
    cleanup_result = cleanup_failure.get("cleanup_result")
    result["score"] = {}
    result["cleanup_failure"] = dict(cleanup_failure)
    if isinstance(cleanup_result, dict):
        result["bridge_cleanup"] = dict(cleanup_result)
    if isinstance(primary_failure, dict):
        result["primary_failure"] = dict(primary_failure)
    return result


def validate_judged_runtime_result(
    result: dict,
    *,
    require_action_evidence: bool = True,
) -> None:
    score = result.get("score")
    if not isinstance(score, dict) or not score:
        raise JudgedRuntimeValidationError("judged runtime result has no score payload")
    try:
        validate_score_identity(
            score,
            expected_attempt_id=result.get("attempt_id"),
            expected_task_name=result.get("task_name"),
        )
    except ScoreOwnershipError as exc:
        raise JudgedRuntimeValidationError(str(exc)) from exc
    if score.get("status") != "success":
        raise JudgedRuntimeValidationError(
            f"judger reported terminal status {score.get('status')!r}"
        )
    snapshot = result.get("runtime_task_dag_snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("summary", {}).get("terminal_state") != "success":
        raise JudgedRuntimeValidationError("runtime task DAG is not terminal success")
    nodes = snapshot.get("nodes", [])
    if not nodes or any(
        node.get("lifecycle", {}).get("status") != "success"
        or node.get("lifecycle", {}).get("active_agents")
        for node in nodes
    ):
        raise JudgedRuntimeValidationError(
            "runtime task DAG contains non-success or actively assigned tasks"
        )
    if result.get("controller", {}).get("shutdown_complete") is not True:
        raise JudgedRuntimeValidationError("controller shutdown is incomplete")
    if result.get("controller", {}).get("active_assignments"):
        raise JudgedRuntimeValidationError("controller still has active assignments")
    if result.get("error") is not None:
        raise JudgedRuntimeValidationError("judged runtime result contains an error")
    if result.get("collection_errors"):
        raise JudgedRuntimeValidationError("judged runtime result contains collection errors")
    action_log = result.get("action_log")
    if not isinstance(action_log, dict):
        raise JudgedRuntimeValidationError(
            "judged runtime action log is unavailable or invalid"
        )
    invalid_entries = [
        name
        for name, entries in action_log.items()
        if name != "_attempt_id"
        and (not isinstance(name, str) or not isinstance(entries, list))
    ]
    if invalid_entries:
        raise JudgedRuntimeValidationError("judged runtime action log schema is invalid")
    if require_action_evidence and not any(
        entries
        for name, entries in action_log.items()
        if name != "_attempt_id" and isinstance(entries, list)
    ):
        raise JudgedRuntimeValidationError(
            "judged runtime contains no action evidence"
        )


def _requires_action_evidence(config: dict) -> bool:
    return bool(config.get("require_action_evidence", True))


def _write_runtime_result(path: str | None, payload: dict) -> None:
    if not path:
        return
    atomic_write_json(path, payload)


@dataclass(frozen=True)
class RuntimeDocumentResolution:
    path: Path | None
    source: Literal["none", "generated", "external"]


def _resolve_runtime_document_path(
    document_file: str | None,
    runtime_paths: RuntimePaths,
) -> RuntimeDocumentResolution:
    if document_file is None:
        return RuntimeDocumentResolution(None, "none")
    if not isinstance(document_file, str):
        raise ValueError("document_file must be a string or null")
    value = document_file.strip()
    if not value:
        return RuntimeDocumentResolution(None, "none")
    normalized = PurePosixPath(value.replace("\\", "/"))
    if normalized == PurePosixPath("data/recipe_hint.json"):
        return RuntimeDocumentResolution(runtime_paths.recipe_hint, "generated")
    if normalized == PurePosixPath("data/map_description.json"):
        return RuntimeDocumentResolution(runtime_paths.map_description, "generated")
    return RuntimeDocumentResolution(Path(value), "external")


def _load_runtime_document(resolution: RuntimeDocumentResolution):
    path = resolution.path
    if path is None:
        return None
    if not path.exists():
        if resolution.source == "generated":
            return None
        raise FileNotFoundError(f"document_file does not exist: {path}")
    if path.is_symlink():
        raise ValueError(f"document_file must not be a symbolic link: {path}")
    if not path.is_file():
        raise ValueError(f"document_file must be a regular file: {path}")
    with path.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, (dict, list)):
        raise ValueError("document_file JSON must contain an object or list")
    return payload


def _resolve_attempt_id(value: str | None) -> str:
    if value is None:
        return uuid4().hex
    if not isinstance(value, str) or not value.strip():
        raise ValueError("attempt_id must be a non-empty string")
    return value


def _write_failure_runtime_result(path: str | None, payload: dict) -> dict | None:
    try:
        _write_runtime_result(path, payload)
    except Exception as exc:
        return {
            "field": "runtime_result",
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
    return None

print(f"pipeline Time taken: {time.time() - start_time}")
start_time = time.time()

os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"
os.environ["no_proxy"] = "localhost,127.0.0.1,::1"


def _with_runtime_paths(function):
    signature = inspect.signature(function)

    @wraps(function)
    def wrapped(*args, **kwargs):
        bound = signature.bind_partial(*args, **kwargs)
        paths = bound.arguments.get("runtime_paths") or RuntimePaths.legacy()
        bound.arguments["runtime_paths"] = paths
        with paths.activated():
            return function(*bound.args, **bound.kwargs)

    return wrapped


@_with_runtime_paths
def run(api_model: str, api_base: str, task_type: str, task_idx: int, agent_num: int, dig_needed: bool, max_task_num: int, task_goal: str, document_file: str | None, host: str, port: int, task_name: str, role: str = "same", api_key_list: list | None = None, document: dict | None = None, minecraft_dual_dag_config: dict | None = None, runtime_result_path: str | None = None, task_scenario: str | None = None, runtime_event_path: str | None = None, emit_controller_terminal_event: bool = True, runtime_paths: RuntimePaths | None = None, attempt_id: str | None = None, require_action_evidence: bool = True, seed_contract: dict | None = None, world_initialization: str | None = None, position_convention: str | None = None, runtime_execution=None, controller_reasoning_effort: str | None = None, k11_late_cleanup_identity: dict | None = None):
    start_time = time.time()
    runtime_execution = runtime_execution or RuntimeExecution.resolve()
    runtime_execution.verify()

    if task_type == "meta" and not task_scenario:
        raise ValueError("meta task requires task_scenario")
    attempt_id = _resolve_attempt_id(attempt_id)
    runtime_paths = runtime_paths or RuntimePaths.legacy()
    runtime_paths.ensure_directories()
    if controller_reasoning_effort not in {None, "high", "medium", "low", "max", "none"}:
        raise ValueError("unsupported reasoning effort")
    document = dict(document or {})
    api_key_list = load_agent_api_key_list()
    meta_setting = {
            "api_model": api_model,
            "api_base": api_base,
            "task_type": task_type,
            "task_idx": task_idx,
            "agent_num": agent_num,
            "dig_needed": dig_needed,
            "max_task_num": max_task_num,
            "task_goal": task_goal,
            "document_file": document_file,
            "host": host,
            "port": port,
            "task_name": task_name,
            "role": role,
            "attempt_id": attempt_id,
            "controller_reasoning_effort": controller_reasoning_effort,
        }
    if task_type == "meta":
        resolved_world_initialization = resolve_world_initialization(world_initialization)
        resolved_position_convention = resolve_position_convention(
            position_convention,
            required=resolved_world_initialization == "preserve_restored_snapshot",
        )
        if (
            resolved_world_initialization == "preserve_restored_snapshot"
            and resolved_position_convention != PositionConvention.ENTITY_FEET
        ):
            raise ValueError("preserved Minecraft worlds require entity_feet position convention")
        if (
            resolved_world_initialization == "preserve_restored_snapshot"
            and document.get("position_convention")
            != resolved_position_convention.value
        ):
            raise ValueError(
                "preserved Minecraft movement target position convention does not match runtime"
            )
        if resolved_world_initialization == "preserve_restored_snapshot":
            initial_state = document.get("initial_state")
            if (
                not isinstance(initial_state, dict)
                or initial_state.get("position_convention")
                != resolved_position_convention.value
            ):
                raise ValueError(
                    "preserved Minecraft initial state position convention does not match runtime"
                )
            entity_feet_position(initial_state)
        meta_setting["task_scenario"] = task_scenario
        meta_setting["evaluation_arg"] = document
        meta_setting["world_initialization"] = resolved_world_initialization
        if resolved_position_convention is not None:
            meta_setting["position_convention"] = resolved_position_convention.value
        if seed_contract is not None:
            meta_setting["seed_contract"] = seed_contract
    atomic_write_json(runtime_paths.meta_setting, meta_setting)

    # Agent.base_url = "https://api.deepseek.com/v1"
    # Agent.model = "deepseek-chat"

    # Agent.base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    # # Agent.model = "qwen3-235b-a22b"
    # Agent.model = "qwen3-next-80b-a3b-instruct"
    # Agent.api_key_list = api_key_list

    # Agent.base_url = "http://10.112.59.240:55049/v1"
    selected_api_key = api_key_list[0] if api_key_list else None
    configure_ollama_agent(Agent, api_model=api_model, api_base=api_base, api_key=selected_api_key)

    # 设置env
    if task_type == "construction":
        env = VillagerBench(env_type=env_type.construction, task_id=task_idx, dig_needed=dig_needed, host=host, port=port, max_task_num=max_task_num, task_name=task_name, _virtual_debug=False, runtime_paths=runtime_paths, runtime_execution=runtime_execution)
    elif task_type == "farming":
        env = VillagerBench(env_type=env_type.farming, task_id=task_idx, dig_needed=False, host=host, port=port, max_task_num=max_task_num, task_name=task_name, _virtual_debug=False, runtime_paths=runtime_paths, runtime_execution=runtime_execution)
    elif task_type == "puzzle":
        env = VillagerBench(env_type=env_type.puzzle, task_id=task_idx, dig_needed=False, host=host, port=port, max_task_num=max_task_num, task_name=task_name, _virtual_debug=False, runtime_paths=runtime_paths, runtime_execution=runtime_execution)
    elif task_type == "meta":
        env = VillagerBench(env_type=env_type.meta, task_id=task_idx, dig_needed=False, host=host, port=port, max_task_num=max_task_num, task_name=task_name, _virtual_debug=False, runtime_paths=runtime_paths, runtime_execution=runtime_execution)
    elif task_type == "gen":
        env = VillagerBench(env_type=env_type.gen, task_id=task_idx, dig_needed=False, host=host, port=port, max_task_num=max_task_num, task_name=task_name, _virtual_debug=False, runtime_paths=runtime_paths, runtime_execution=runtime_execution)
    elif task_type == "none":
        env = VillagerBench(env_type=env_type.none, task_id=task_idx, dig_needed=False, host=host, port=port, max_task_num=max_task_num, task_name=task_name, _virtual_debug=False, runtime_paths=runtime_paths, runtime_execution=runtime_execution)
    else:
        raise NotImplementedError
    env.attempt_id = attempt_id
    eac_mode = (minecraft_dual_dag_config or {}).get("eac_mode")
    if eac_mode is not None:
        from benchmarks.minecraft.eac_identity import verify_eac_premanifest
        from benchmarks.minecraft.eac_runtime import install_minecraft_eac
        premanifest_path = (minecraft_dual_dag_config or {}).get("eac_premanifest")
        if not premanifest_path:
            raise ValueError("Minecraft EAC runtime requires an explicit premanifest")
        execution_revision = (minecraft_dual_dag_config or {}).get("eac_execution_revision")
        if not execution_revision:
            raise ValueError("Minecraft EAC runtime requires an explicit execution revision")
        if ((minecraft_dual_dag_config or {}).get("judged_execution") is not False
                or (minecraft_dual_dag_config or {}).get("production") is not False):
            raise ValueError("Minecraft EAC runtime requires explicit non-judged, non-production admission")
        if eac_mode not in {"dual_dag_advisory", "dual_dag_authority"}:
            raise ValueError("Minecraft EAC execution requires an admitted EAC mode")
        if task_type != "none":
            raise ValueError("Minecraft EAC non-judged identity requires task_type=none")
        identity = verify_eac_premanifest(
            Path(premanifest_path), execution=runtime_execution,
            execution_revision=execution_revision,
        )
        install_minecraft_eac(env, mode=eac_mode, run_id=attempt_id, identity_binding=identity)
    if task_type == "meta" and runtime_result_path:
        env.meta_diagnostics_dir = os.path.dirname(runtime_result_path) or "."

    # 设置agent_tool
    if task_type == "construction":
        agent_tool = [Agent.placeBlock, Agent.fetchContainerContents, Agent.MineBlock, Agent.scanNearbyEntities, Agent.equipItem,
                      Agent.navigateTo, Agent.withdrawItem, Agent.dismantleDirtLadder, Agent.erectDirtLadder, Agent.handoverBlock]
    elif task_type == "farming":
        agent_tool = [Agent.fetchContainerContents, Agent.MineBlock, Agent.scanNearbyEntities, Agent.equipItem, Agent.SmeltingCooking,
                      Agent.navigateTo, Agent.withdrawItem, Agent.craftBlock, Agent.attackTarget, Agent.useItemOnEntity,
                      Agent.handoverBlock]
    elif task_type == "puzzle":
        agent_tool = [Agent.placeBlock, Agent.fetchContainerContents, Agent.MineBlock, Agent.scanNearbyEntities, Agent.equipItem,
                      Agent.navigateTo, Agent.withdrawItem, Agent.ToggleAction, Agent.handoverBlock]
    elif task_type == "meta" or task_type == "gen" or task_type == "none":
        agent_tool = [Agent.scanNearbyEntities, Agent.navigateTo, Agent.attackTarget, Agent.useItemOnEntity, Agent.useItemOnBlock,
                      Agent.MineBlock, Agent.placeBlock, Agent.equipItem, Agent.handoverBlock, Agent.SmeltingCooking, Agent.withdrawItem, 
                      Agent.storeItem, Agent.craftBlock, Agent.eat, Agent.fetchContainerContents, Agent.wake, Agent.talkTo, Agent.waitForFeedback,
                      Agent.openContainer, Agent.performMovement, 
                      Agent.sleep, Agent.startFishing, Agent.ToggleAction, 
                      Agent.read, Agent.mountEntity, Agent.dismountEntity]
    else:
        raise NotImplementedError

    print(f"VillagerBench Time taken: {time.time() - start_time}")
    start_time = time.time()

    # 设置agent_pool
    name_list = ["Alice", "Bob", "Cindy", "David", "Eve", "Frank", "Grace", "Helen", "Ivy", "Jack", "Kevin", "Lily",
                 "Mary", "Nancy", "Olivia", "Peter", "Queen", "Rose", "Sam", "Tom", "Umbrella", "Vivian", "Wendy",
                 "Xavier", "Yolanda", "Zoe"]
    if agent_num == 3 and task_type == "farming" and role == "different":
        agent_tool = [Agent.fetchContainerContents, Agent.scanNearbyEntities, Agent.equipItem,
                      Agent.navigateTo, Agent.withdrawItem, Agent.craftBlock, Agent.SmeltingCooking,
                      Agent.handoverBlock]
        env.agent_register(agent_tool=agent_tool, agent_number=1, name_list=[name_list[0]])
        agent_tool = [Agent.fetchContainerContents, Agent.scanNearbyEntities, Agent.equipItem,
                      Agent.navigateTo, Agent.withdrawItem, Agent.craftBlock, Agent.MineBlock,
                      Agent.handoverBlock]
        env.agent_register(agent_tool=agent_tool, agent_number=1, name_list=[name_list[1]])
        agent_tool = [Agent.fetchContainerContents, Agent.scanNearbyEntities, Agent.equipItem,
                      Agent.navigateTo, Agent.withdrawItem, Agent.craftBlock, Agent.attackTarget, 
                      Agent.handoverBlock]
        env.agent_register(agent_tool=agent_tool, agent_number=1, name_list=[name_list[2]])
    else:
        action = document.get("action", None)
        if action == "chat" or action == "handover":
            env.agent_register(agent_tool=agent_tool, agent_number=agent_num+1, name_list=name_list[:agent_num+1])
        else:
            env.agent_register(agent_tool=agent_tool, agent_number=agent_num, name_list=name_list[:agent_num])

    runtime_tm = None
    runtime_ctrl = None
    try:
        with env.run(fast_api=True):  # Use the FastAPI bridge; it avoids viewer-only Node dependencies such as canvas.
            # 启动DM
            history_output_dir = runtime_paths.run_result_dir(task_name)
            dm = DataManager(silent=False, history_output_dir=history_output_dir)
            dm.update_database_init(env.get_init_state())

            print(f"DataManager Time taken: {time.time() - start_time}")
            start_time = time.time()

            # 启动TM
            from pipeline.runtime_events import JsonlRuntimeEventRecorder, NoOpRuntimeEventSink
            event_sink = JsonlRuntimeEventRecorder(runtime_event_path, run_id=task_name) if runtime_event_path else NoOpRuntimeEventSink()
            tm = TaskManager(
                silent=False,
                cache_enabled=False,
                history_output_dir=history_output_dir,
            )
            tm.event_sink = event_sink
            runtime_tm = tm
            tm.runtime_checkpoint = lambda: _write_runtime_result(
                runtime_result_path,
                _runtime_checkpoint_result(env, tm, runtime_ctrl, attempt_id=attempt_id, task_name=task_name),
            )
            _write_runtime_result(
                runtime_result_path,
                _runtime_checkpoint_result(env, tm, runtime_ctrl, attempt_id=attempt_id, task_name=task_name),
            )

            print(f"TaskManager Time taken: {time.time() - start_time}")
            start_time = time.time()

            # 设置llm
            llm_config = make_ollama_llm_config(
                api_model=api_model, api_base=api_base, api_key=selected_api_key,
                reasoning_effort=controller_reasoning_effort,
            )
            # llm_config = {
            #     "api_key": api_key_list[0],
            #     "api_base": "https://api.deepseek.com/v1",
            #     "api_model": "deepseek-chat",
            #     "api_key_list": api_key_list
            # }
        
            # llm_config = {
            #     "api_key": "sk-VillagerTuning",
            #     # "api_base": "http://10.112.59.240:50892/v1",
            #     "api_base": "http://localhost:8264/v1/",
            #     "api_model": "default",
            #     "api_key_list": ["sk-VillagerTuning"]
            # }

            tm_llm_config = llm_config
            dm_llm_config = llm_config
            # base_llm_config = llm_config

            # tm_llm_config = {
            #     "api_key": api_key_list[0],
            #     "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            #     "api_model": "qwen-max",
            #     "api_key_list": api_key_list
            # }

            # dm_llm_config = {
            #     "api_key": api_key_list[0],
            #     "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            #     "api_model": "qwen-plus",
            #     "api_key_list": api_key_list
            # }

            # base_llm_config = {
            #     "api_key": api_key_list[0],
            #     "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            #     "api_model": "qwen3-next-80b-a3b-instruct",
            #     "api_key_list": api_key_list
            # }
            base_llm_config = make_ollama_llm_config(
                api_model=api_model, api_base=api_base, api_key=selected_api_key,
                reasoning_effort=controller_reasoning_effort,
            )


            ctrl = GlobalController(llm_config, tm, dm, env,
                                tm_llm_config=tm_llm_config, 
                                dm_llm_config=dm_llm_config,
                                base_agent_config=base_llm_config,
                                all_tools=agent_tool,
                                minecraft_dual_dag_config=minecraft_dual_dag_config,
                                event_sink=event_sink,
                                emit_terminal_events=emit_controller_terminal_event)
            runtime_ctrl = ctrl
            _configure_k11_late_diagnostic_sink(
                ctrl, runtime_result_path, k11_late_cleanup_identity,
            )

            # response = ctrl.agent_list[0].llm.few_shot_generate_thoughts(system_prompt="", example_prompt="hi")
            # print(response)
            if task_type == "farming": #补充材料来源prompt
                with runtime_execution.asset("farm_setting").path.open("r", encoding="utf-8") as f:
                    task_settings = json.load(f)
                task_data = task_settings[task_idx]
                task_goal += f"\nBelow is a detailed list of ingredients and their specific sources. Use this information to plan and coordinate your actions efficiently:\n"
                if "cake" in task_data["name"]:
                    task_goal += f"egg: egg in chest\n"
                    task_goal += f"milk: {task_data['milk']}\n"
                    task_goal += f"wheat: {task_data['wheat']}\n"
                    task_goal += f"sugar: {task_data['sugar']}\n"
                elif "rabbit_stew" in task_data["name"]:
                    task_goal += f"cooked_rabbit: {task_data['cooked_rabbit']}\n"
                    task_goal += f"baked_potato: {task_data['baked_potato']}\n"
                    task_goal += f"carrot: {task_data['carrot']}\n"
                    task_goal += f"brown_mushroom: {task_data['brown_mushroom']}\n"
                    task_goal += f"bowl: {task_data['bowl']}\n"
                
            document_resolution = _resolve_runtime_document_path(
                document_file,
                runtime_paths,
            )
            runtime_document = _load_runtime_document(document_resolution)
            if runtime_document is not None:
                document["recipe"] = runtime_document
            tm.init_task(description=task_goal, document=document)
            _write_runtime_result(
                runtime_result_path,
                _runtime_result(env, tm, ctrl, attempt_id=attempt_id, task_name=task_name),
            )

            ctrl.run()

            result = _runtime_result(env, tm, ctrl, attempt_id=attempt_id, task_name=task_name)
            if task_type == "meta":
                validate_judged_runtime_result(
                    result,
                    require_action_evidence=require_action_evidence,
                )
        result["bridge_cleanup"] = dict(
            getattr(env, "bridge_cleanup_result", {}) or {}
        )
        try:
            result["minecraft_bridge_diagnostics"] = env.get_minecraft_bridge_diagnostics()
        except Exception as diagnostic_error:
            result["minecraft_bridge_diagnostics"] = {
                "schema_version": "minecraft-bridge-diagnostics-summary/1",
                "actors": {}, "artifacts": {},
                "diagnostic_collection_error": [{
                    "error_type": type(diagnostic_error).__name__,
                }],
            }
        _write_runtime_result(runtime_result_path, result)
        return result
    except Exception as exc:
        result = _runtime_result(
            env,
            runtime_tm,
            runtime_ctrl,
            error=str(exc),
            error_type=type(exc).__name__,
            attempt_id=attempt_id,
            task_name=task_name,
        )
        _apply_runtime_cleanup_failure(result, exc)
        _write_failure_runtime_result(runtime_result_path, result)
        raise


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely run or validate a Minecraft experiment config",
    )
    parser.add_argument("--config", required=True, help="Launch config JSON file")
    parser.add_argument("--config-index", type=int, default=0, help="Config list entry to select")
    parser.add_argument("--output-root", default="result/minecraft", help="Artifact output directory")
    parser.add_argument("--timeout", type=float, default=None, help="Positive execute timeout in seconds")
    parser.add_argument("--execute", action="store_true", help="Run the real Minecraft environment")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_cli_parser()
    args = parser.parse_args(argv)

    if not os.path.isfile(args.config):
        parser.error(f"config file not found: {args.config}")
    if args.execute and (
        args.timeout is None or not math.isfinite(args.timeout) or args.timeout <= 0
    ):
        parser.error("--execute requires --timeout with a positive value")

    try:
        from benchmarks.minecraft.experiment import run_minecraft_experiment
    except (ImportError, AttributeError) as exc:
        print(f"error: unable to load Minecraft experiment harness: {exc}", file=sys.stderr)
        return 1

    try:
        summary = run_minecraft_experiment(
            config_path=args.config,
            config_index=args.config_index,
            output_root=args.output_root,
            execute=args.execute,
            execute_timeout_seconds=args.timeout,
        )
    except Exception as exc:
        print(f"error: Minecraft experiment harness failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2))
    return 0 if summary.get("error") is None else 1


if __name__ == "__main__":
    raise SystemExit(main())

# python env/minecraft_server.py -H 10.214.180.148 -P 25565 -LP 5000 -U Alice -W world -D false
# python env/meta_judger.py --idx 0 --host 10.214.180.148 --port 25565 --agent_num 1 --agent_names Alice --task_name meta_test_task0
