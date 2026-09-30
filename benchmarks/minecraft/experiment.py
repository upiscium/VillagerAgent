import argparse
from copy import deepcopy
import importlib
import json
import math
import multiprocessing
import os
import queue
import signal
import sys
import time
from pathlib import Path

from benchmarks.common.run_artifacts import finalize_run_directory, prepare_run_directory
from benchmarks.common.sanitization import collect_secret_values, sanitize_artifact_value
from benchmarks.experiment_provenance import (
    file_identity,
    finalize_provenance,
    model_identity,
    project_public_argv_paths,
    project_public_path_fields,
    standard_run_name,
    update_provenance_assets,
    update_provenance_settings,
    write_provenance,
)
from benchmarks.minecraft.metrics import build_minecraft_metrics
from benchmarks.minecraft.events import (
    ATTEMPT_TERMINAL_EVENT_TYPES,
    build_normalized_events,
    finalize_attempt_events,
    validate_attempt_event_lifecycle,
)
from benchmarks.minecraft.judged_diagnostics import (
    build_judged_iteration_trace,
    build_judged_terminal_diagnostics,
)
from benchmarks.minecraft.run_lock import (
    MinecraftTargetLock,
    MinecraftTargetLockBusyError,
    MinecraftTargetLockMetadataError,
    MinecraftTargetLockUnavailableError,
    MinecraftTargetQuarantinedError,
    minecraft_target_lock_key,
)
from benchmarks.minecraft.seed_contract import SeedContract, SeedScope, resolve_seed_contract
from benchmarks.minecraft.target_safety import assess_minecraft_target_safety
from env.runtime_paths import RuntimePaths
from env.runtime_execution import RuntimeExecution
from env.judger_artifacts import ScoreOwnershipError, validate_score_identity
from env.minecraft_dual_dag import (
    build_minecraft_dual_dag_artifact,
    build_minecraft_runtime_decision_support,
    rank_minecraft_runtime_tasks,
    sanitize_public_value,
)
from pipeline.dual_dag_task_store import RuntimeTaskDAGStore
from pipeline.runtime_events import NoOpRuntimeEventSink, RuntimeEventSink, safe_emit_runtime_event
from type_define.graph import Graph, Task


DEFAULT_OUTPUT_ROOT = Path("result/minecraft")
REQUIRED_CONFIG_FIELDS = ("task_type", "task_idx", "agent_num", "task_goal", "host", "port", "task_name")
TASK_SELECTION_POLICIES = ("dual-dag", "original", "dual_dag_advisory", "dual_dag_authority")
RUNTIME_TERMINATE_GRACE_SECONDS = 1.0
DEFAULT_JUDGED_REQUIRED_ARTIFACTS = (
    "score",
    "runtime_task_dag",
    "action_log",
    "events",
    "bridge_cleanup",
    "child_protocol",
)
SUPPORTED_REQUIRED_ARTIFACTS = frozenset(DEFAULT_JUDGED_REQUIRED_ARTIFACTS)


class MinecraftExecuteTimeoutError(TimeoutError):
    """Raised when a bounded real Minecraft run exceeds its timeout."""

    def __init__(self, message: str, *, process_metadata: dict | None = None):
        super().__init__(message)
        self.process_metadata = process_metadata or {}


class MinecraftRuntimeChildError(RuntimeError):
    def __init__(self, message: str, *, error_type: str, process_metadata: dict, child_protocol: dict | None = None, timed_out: bool = False, primary_error: dict | None = None, cleanup_state: dict | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.process_metadata = process_metadata
        self.child_protocol = child_protocol or {}
        self.timed_out = timed_out
        self.primary_error = primary_error or {}
        self.cleanup_state = cleanup_state or {}


class RequiredArtifactError(RuntimeError):
    pass


def _require_positive_finite_timeout(value: float | None) -> float:
    if (
        value is None
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(
            "real Minecraft execute requires a positive finite timeout"
        )
    return float(value)


def run_minecraft_experiment(
    *,
    config_path: str | Path,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    run_name: str | None = None,
    config_index: int = 0,
    enable_dual_dag_task_selection: bool = True,
    task_selection_policy: str | None = None,
    execute: bool = False,
    execute_timeout_seconds: float | None = None,
    retain_runtime_result: bool = False,
    command_text: str | None = None,
    overwrite: bool = False,
    event_sink: RuntimeEventSink | None = None,
    execution_root: str | Path | None = None,
) -> dict:
    if execute:
        execute_timeout_seconds = _require_positive_finite_timeout(
            execute_timeout_seconds
        )
    attempt_state: dict = {}
    try:
        return _run_minecraft_experiment_attempt(
            config_path=config_path,
            output_root=output_root,
            run_name=run_name,
            config_index=config_index,
            enable_dual_dag_task_selection=enable_dual_dag_task_selection,
            task_selection_policy=task_selection_policy,
            execute=execute,
            execute_timeout_seconds=execute_timeout_seconds,
            retain_runtime_result=retain_runtime_result,
            command_text=command_text,
            overwrite=overwrite,
            event_sink=event_sink or NoOpRuntimeEventSink(),
            execution_root=execution_root,
            attempt_state=attempt_state,
        )
    except BaseException as exc:
        if attempt_state:
            attempt_state["error"] = str(exc)
            attempt_state["error_type"] = exc.__class__.__name__
            _emit_attempt_terminal_event(
                attempt_state,
                "run_timed_out" if isinstance(exc, (TimeoutError, MinecraftExecuteTimeoutError)) else "run_failed",
                payload={"error": str(exc), "error_type": exc.__class__.__name__},
            )
            _write_minimal_failure_artifacts(attempt_state)
            _repair_failed_event_artifact(attempt_state)
            _sanitize_retained_meta_judger_diagnostics(
                attempt_state.get(
                    "runtime_result_path",
                    attempt_state["output_dir"] / ".runtime" / "runtime_result.json",
                ).parent / "meta_judger_diagnostics.json",
                output_dir=attempt_state["output_dir"],
                secret_values=attempt_state.get("secret_values", ()),
            )
            _sanitize_runtime_checkpoint(
                attempt_state.get(
                    "runtime_result_path",
                    attempt_state["output_dir"] / ".runtime" / "runtime_result.json",
                ),
                secret_values=attempt_state.get("secret_values", ()),
            )
            finalize_provenance(
                attempt_state["output_dir"],
                status="timeout" if isinstance(exc, (TimeoutError, MinecraftExecuteTimeoutError)) else "failure",
            )
            finalize_run_directory(
                attempt_state["output_dir"],
                attempt_id=attempt_state["attempt_id"],
                producer="benchmarks.minecraft.experiment",
                status="failed",
            )
        raise


def _run_minecraft_experiment_attempt(
    *,
    config_path: str | Path,
    output_root: str | Path,
    run_name: str | None,
    config_index: int,
    enable_dual_dag_task_selection: bool,
    task_selection_policy: str | None,
    execute: bool,
    execute_timeout_seconds: float | None,
    retain_runtime_result: bool,
    command_text: str | None,
    overwrite: bool,
    event_sink: RuntimeEventSink,
    execution_root: str | Path | None,
    attempt_state: dict,
) -> dict:
    """Run or dry-run a Minecraft experiment and write normalized artifacts.

    Dry-run is the default so CI and local development can validate artifact capture
    without requiring a Minecraft server. ``execute=True`` calls the existing real
    runtime and then captures the same public artifact set from the run outputs.
    """
    launch_config = _load_config(config_path, config_index=config_index, execute=execute)
    seed_resolution = _resolve_minecraft_seed_contract(launch_config)
    secret_values = collect_secret_values(launch_config)
    selected_run_name = standard_run_name(run_name or launch_config.get("task_name") or _default_run_name(config_path))
    output_dir = Path(output_root) / selected_run_name
    attempt_id = prepare_run_directory(
        output_dir,
        producer="benchmarks.minecraft.experiment",
        overwrite=overwrite,
    )
    runtime_root = output_dir / ".runtime" / "attempts" / attempt_id
    runtime_paths = RuntimePaths.isolated(runtime_root)
    if execute:
        runtime_paths.ensure_directories()
    runtime_result_path = runtime_root / "runtime_result.json"
    child_runtime_event_path = runtime_root / "runtime_events.jsonl"
    runtime_event_path = child_runtime_event_path if execute else None
    lock_root = Path(
        os.environ.get(
            "VILLAGER_MINECRAFT_LOCK_ROOT",
            str(DEFAULT_OUTPUT_ROOT / ".locks"),
        )
    )
    lock_key = minecraft_target_lock_key(
        host=str(launch_config["host"]),
        port=int(launch_config["port"]),
    )
    attempt_state.update({
        "output_dir": output_dir,
        "attempt_id": attempt_id,
        "secret_values": secret_values,
        "execute": execute,
        "run_name": selected_run_name,
        "event_sink": event_sink,
        "terminal_event_emitted": False,
        "runtime_result_path": runtime_result_path,
    })
    runtime_launch_config = dict(launch_config)
    if execute:
        premanifest = runtime_launch_config.get("eac_premanifest")
        if isinstance(premanifest, str) and premanifest:
            runtime_launch_config["eac_premanifest"] = str(Path(premanifest).resolve(strict=True))
        runtime_launch_config["task_name"] = f"{launch_config['task_name']}_{attempt_id[:12]}"
        runtime_launch_config["attempt_id"] = attempt_id
    safe_emit_runtime_event(event_sink, "run_started", source="benchmarks.minecraft.experiment", payload={"mode": "execute" if execute else "dry_run"})
    runtime_llm_config = {}
    requested_policy = task_selection_policy or launch_config.get("task_selection_policy")
    effective_settings = _minecraft_effective_settings(
        launch_config=runtime_launch_config,
        config_path=config_path,
        config_index=config_index,
        run_name=selected_run_name,
        task_selection_policy=requested_policy,
        execute=execute,
        execute_timeout_seconds=execute_timeout_seconds,
        retain_runtime_result=retain_runtime_result,
        runtime_llm_config=runtime_llm_config,
        attempt_id=attempt_id,
    )
    if seed_resolution is not None:
        effective_settings["seed_contract"] = seed_resolution.to_dict()
    write_provenance(
        output_dir,
        benchmark="minecraft",
        command=command_text or _command_text(),
        resolved_config=effective_settings,
        environment_notes="real_environment_execute=" + str(bool(execute)).lower(),
        assets=_minecraft_provenance_assets(
            config_path=Path(config_path),
            launch_config=launch_config,
            execute=execute,
            runtime_llm_config=runtime_llm_config,
        ),
    )
    if execute:
        runtime_llm_config = _runtime_llm_config(launch_config)
        runtime_secret_values = collect_secret_values(runtime_llm_config)
        secret_values = tuple(dict.fromkeys((*secret_values, *runtime_secret_values)))
        attempt_state["secret_values"] = secret_values

    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
    attempt_state["started_at"] = started_at
    selected_policy = _task_selection_policy(
        requested_policy,
        enable_dual_dag_task_selection=enable_dual_dag_task_selection,
    )
    dual_dag_config = _dual_dag_config(selected_policy)
    effective_settings = _minecraft_effective_settings(
        launch_config=runtime_launch_config,
        config_path=config_path,
        config_index=config_index,
        run_name=selected_run_name,
        task_selection_policy=selected_policy,
        execute=execute,
        execute_timeout_seconds=execute_timeout_seconds,
        retain_runtime_result=retain_runtime_result,
        runtime_llm_config=runtime_llm_config,
        attempt_id=attempt_id,
    )
    if seed_resolution is not None:
        effective_settings["seed_contract"] = seed_resolution.to_dict()
    effective_settings["runtime"] = {
        "root": f".runtime/attempts/{attempt_id}",
        "result": f".runtime/attempts/{attempt_id}/runtime_result.json",
        "events": f".runtime/attempts/{attempt_id}/runtime_events.jsonl",
        "score": f".runtime/attempts/{attempt_id}/data/score.json",
        "load_status": f".runtime/attempts/{attempt_id}/cache/load_status.cache",
        "server_lock_key": lock_key,
        "server_lock_acquired": False,
        "world_id": str(launch_config.get("world_id", "")),
    }
    update_provenance_settings(output_dir, effective_settings)
    update_provenance_assets(
        output_dir,
        [_minecraft_model_identity(
            launch_config=launch_config,
            runtime_llm_config=runtime_llm_config,
            required=execute,
        )],
    )
    tasks, graph, task_store = _task_graph_from_config(launch_config)
    action_log: dict = _fixture_action_log(launch_config)
    action_log_available = "smoke_action_log" in launch_config
    score: dict = {}
    score_ownership_verified = False
    runtime_result: dict = {}
    minecraft_eac_audit: dict = {"configured": False, "read_only_projection": True}
    error = None
    error_type = ""
    timed_out = False
    runtime_process: object = None
    runtime_primary_error = {}
    runtime_cleanup_state = {}
    bridge_cleanup = {}
    child_protocol = {}
    server_lock_acquired = False
    server_lock_released = False
    server_lock_stale_owner_detected = False
    server_lock_quarantine_detected = False
    runtime_started = False
    runtime_target_lock_admission = "not_applicable" if not execute else "pending"
    runtime_target_lock_unavailable = False
    runtime_target_lock_unavailable_reason = None
    runtime_target_lock_owner = {}
    runtime_target_lock_metadata_valid = None
    runtime_target_quarantined = False
    runtime_target_quarantine = {}
    target_safety = assess_minecraft_target_safety(
        runtime_started=False,
        runtime_process={},
        bridge_cleanup={},
    )

    if execute:
        runtime_execution, runtime_execution_identity = _resolve_runtime_execution(execution_root)
        attempt_state["runtime_execution_identity"] = runtime_execution_identity
        _remove_runtime_result(runtime_result_path)
        target_lock = MinecraftTargetLock(
            lock_root=lock_root,
            host=str(launch_config["host"]),
            port=int(launch_config["port"]),
            world_id=str(launch_config.get("world_id", "")),
            attempt_id=attempt_id,
            timeout_seconds=float(launch_config.get("server_lock_timeout_seconds", 0.0)),
        )
        try:
            with target_lock:
                runtime_target_lock_admission = "granted"
                runtime_target_lock_metadata_valid = True
                server_lock_acquired = True
                server_lock_stale_owner_detected = target_lock.stale_owner_detected
                try:
                    runtime_started = True
                    runtime_result = _execute_real_runtime_bounded(
                        runtime_launch_config,
                        dual_dag_config=dual_dag_config,
                        timeout_seconds=execute_timeout_seconds,
                        runtime_result_path=runtime_result_path,
                        runtime_event_path=runtime_event_path,
                        runtime_root=runtime_root,
                        attempt_id=attempt_id,
                        runtime_execution=runtime_execution,
                        runtime_execution_identity=runtime_execution_identity,
                    ) or {}
                except MinecraftExecuteTimeoutError as exc:
                    error = str(exc)
                    error_type = "timeout"
                    timed_out = True
                    runtime_process = exc.process_metadata
                except MinecraftRuntimeChildError as exc:
                    error = str(exc)
                    error_type = exc.error_type
                    timed_out = exc.timed_out
                    runtime_process = exc.process_metadata
                    child_protocol = exc.child_protocol
                    runtime_primary_error = exc.primary_error
                    runtime_cleanup_state = exc.cleanup_state
                except Exception as exc:  # Preserve partial artifacts for failed smoke runs.
                    error = str(exc)
                    error_type = exc.__class__.__name__
                finally:
                    persisted_runtime_result, persisted_result_error = _read_partial_runtime_result(
                        runtime_result_path
                    )
                    if persisted_result_error is not None:
                        runtime_result.setdefault("collection_errors", []).append(
                            persisted_result_error
                        )
                    runtime_result = runtime_result or persisted_runtime_result
                    if runtime_process is None:
                        runtime_process = runtime_result.pop("runtime_process", {})
                    raw_bridge_cleanup = runtime_result.get("bridge_cleanup")
                    child_protocol = child_protocol or runtime_result.get("child_protocol", {})
                    target_safety = assess_minecraft_target_safety(
                        runtime_started=runtime_started,
                        runtime_process=runtime_process,
                        bridge_cleanup=raw_bridge_cleanup,
                    )
                    if not target_safety.safe:
                        quarantine_record = target_lock.quarantine(
                            run_name=selected_run_name,
                            reasons=target_safety.reasons,
                            diagnostics=target_safety.diagnostics,
                        )
                        runtime_target_quarantined = True
                        runtime_target_quarantine = _public_target_quarantine(
                            quarantine_record,
                            status="created",
                        )
                        if error is None:
                            error = "Minecraft target cleanup could not be verified as safe"
                            error_type = "MinecraftTargetCleanupError"
                    bridge_cleanup = _public_bridge_cleanup(raw_bridge_cleanup)
        except MinecraftTargetQuarantinedError as exc:
            error = str(exc)
            error_type = "MinecraftTargetQuarantinedError"
            runtime_target_lock_admission = "quarantined"
            runtime_target_lock_metadata_valid = True
            server_lock_quarantine_detected = True
            runtime_target_quarantined = True
            runtime_target_quarantine = _public_target_quarantine(
                exc.quarantine,
                status="preexisting",
            )
        except MinecraftTargetLockMetadataError as exc:
            error = str(exc)
            error_type = "MinecraftTargetLockMetadataError"
            runtime_target_lock_admission = "metadata_invalid"
            runtime_target_lock_metadata_valid = False
        except MinecraftTargetLockBusyError as exc:
            error = str(exc)
            error_type = "MinecraftTargetLockBusyError"
            runtime_target_lock_admission = "busy"
            runtime_target_lock_unavailable = True
            runtime_target_lock_unavailable_reason = exc.reason
            runtime_target_lock_owner = _public_target_lock_owner(exc.owner)
            runtime_target_lock_metadata_valid = None
        except MinecraftTargetLockUnavailableError as exc:
            error = str(exc)
            error_type = "MinecraftTargetLockUnavailableError"
            runtime_target_lock_admission = "unavailable"
            runtime_target_lock_unavailable = True
            runtime_target_lock_unavailable_reason = exc.reason
            runtime_target_lock_metadata_valid = None
        except Exception as exc:
            error = str(exc)
            error_type = exc.__class__.__name__
            if runtime_target_lock_admission == "pending" and not runtime_started:
                runtime_target_lock_admission = "unavailable"
                runtime_target_lock_unavailable = True
                runtime_target_lock_unavailable_reason = "unknown_error"
                runtime_target_lock_metadata_valid = None
        finally:
            server_lock_released = (
                server_lock_acquired
                and
                not target_lock.acquired
                and target_lock.release_succeeded is not False
            )
            if target_lock.release_succeeded is False:
                runtime_target_quarantined = True
                runtime_target_quarantine = runtime_target_quarantine or {
                    "status": "incomplete",
                    "reasons": ["lock_release_incomplete"],
                    "diagnostics": {},
                }
                if error is None:
                    error = "Minecraft target lock release could not be verified"
                    error_type = "MinecraftTargetLockReleaseError"
        action_log_available = isinstance(runtime_result.get("action_log"), dict)
        action_log = runtime_result.get("action_log") if action_log_available else {}
        score = runtime_result.get("score") if isinstance(runtime_result.get("score"), dict) else {}
        minecraft_eac_audit = runtime_result.get("minecraft_eac_audit", {"configured": False, "read_only_projection": True})
        persisted_eac_audit = _read_json(
            runtime_result_path.parent / "data" / "minecraft_eac_audit.json", default={})
        if persisted_eac_audit:
            minecraft_eac_audit = persisted_eac_audit
        if score:
            try:
                validate_score_identity(
                    score,
                    expected_attempt_id=attempt_id,
                    expected_task_name=runtime_launch_config["task_name"],
                )
            except ScoreOwnershipError as exc:
                if error is None:
                    error = str(exc)
                    error_type = "ScoreOwnershipError"
                score = {}
            else:
                score_ownership_verified = True

    public_runtime_process = _public_runtime_process(runtime_process)
    runtime_target_safe_to_reuse = bool(
        (
            not execute
            and target_safety.safe
            and not runtime_target_quarantined
        )
        or (
            execute
            and runtime_target_lock_admission == "granted"
            and runtime_target_lock_metadata_valid is True
            and server_lock_released
            and target_safety.safe
            and not runtime_target_quarantined
        )
    )
    effective_settings["runtime"].update({
        "server_lock_acquired": server_lock_acquired,
        "server_lock_released": server_lock_released,
        "server_lock_stale_owner_detected": server_lock_stale_owner_detected,
        "server_lock_quarantine_detected": server_lock_quarantine_detected,
    })
    update_provenance_settings(output_dir, effective_settings)

    meta_judger_diagnostics = (
        _read_json(runtime_result_path.parent / "meta_judger_diagnostics.json", default={})
        if execute and launch_config.get("task_type") == "meta"
        else {}
    )

    runtime_task_dag_snapshot = _runtime_task_dag_snapshot(
        runtime_result=runtime_result,
        fallback_snapshot=task_store.snapshot(),
        execute=execute,
    )
    task_state_source = runtime_task_dag_snapshot.get("snapshot_source", "config_fixture")
    artifact_tasks = tasks
    artifact_graph = graph
    if execute and task_state_source == "real_runtime":
        artifact_tasks, artifact_graph = task_graph_from_runtime_task_dag_snapshot(
            runtime_task_dag_snapshot
        )

    artifact = build_minecraft_dual_dag_artifact(
        action_log=action_log,
        tasks=artifact_tasks,
        graph=artifact_graph,
    )
    artifact["task_state_source"] = task_state_source
    decision_support = build_minecraft_runtime_decision_support(
        artifact,
        candidate_tasks=artifact_tasks,
    )
    decision_support["task_state_source"] = task_state_source
    ranked = rank_minecraft_runtime_tasks(
        artifact_tasks,
        graph=artifact_graph,
        action_log=action_log,
        config=dual_dag_config,
    )
    ranked_tasks = ranked.get("tasks", artifact_tasks)
    task_graph_snapshot = runtime_result.get("task_graph_snapshot") or _task_graph_snapshot(artifact_graph)
    runtime_selected_task_ids = _runtime_selected_task_ids(runtime_result) if execute else []
    selected_task = _find_task(artifact_tasks, runtime_selected_task_ids[0]) if runtime_selected_task_ids else None
    if not execute and ranked_tasks:
        selected_task = ranked_tasks[0]
    base_normalized_events = None
    final_normalized_events = None
    event_artifact_error = None
    event_artifact_error_detail = None
    try:
        base_normalized_events = build_normalized_events(
            run_id=selected_run_name,
            runtime_journal=runtime_event_path,
            action_log=action_log,
            analysis_artifact=artifact,
        )
    except Exception as exc:
        base_normalized_events = None
        event_artifact_error = type(exc).__name__
        event_artifact_error_detail = str(exc)
    summary = {
        "attempt_id": attempt_id,
        "run_name": selected_run_name,
        "mode": "execute" if execute else "dry_run",
        "started_at": started_at,
        "output_dir": str(output_dir),
        "task_name": launch_config.get("task_name", ""),
        "runtime_task_name": runtime_launch_config.get("task_name", ""),
        "world_id": str(launch_config.get("world_id", "")),
        "execution": runtime_execution_identity if execute else None,
        "runtime_root": f".runtime/attempts/{attempt_id}",
        "runtime_result_path": f".runtime/attempts/{attempt_id}/runtime_result.json",
        "runtime_event_path": f".runtime/attempts/{attempt_id}/runtime_events.jsonl",
        "score_path": f".runtime/attempts/{attempt_id}/data/score.json",
        "load_status_path": f".runtime/attempts/{attempt_id}/cache/load_status.cache",
        "server_lock_key": lock_key,
        "server_lock_acquired": server_lock_acquired,
        "server_lock_released": server_lock_released,
        "server_lock_stale_owner_detected": server_lock_stale_owner_detected,
        "server_lock_quarantine_detected": server_lock_quarantine_detected,
        "task_type": launch_config.get("task_type", ""),
        "task_idx": launch_config.get("task_idx"),
        "dual_dag_runtime_enabled": True,
        "dual_dag_task_selection_enabled": selected_policy == "dual-dag",
        "task_selection_policy": selected_policy,
        "runtime_selection_policy": selected_policy,
        "runtime_task_store": "runtime_task_dag",
        "source_of_truth": "runtime_task_dag",
        "snapshot_source": runtime_task_dag_snapshot.get("snapshot_source", "config_fixture"),
        "task_state_source": task_state_source,
        "execute_real_environment": bool(execute),
        "runtime_execution": runtime_execution_identity if execute else None,
        "execute_timeout_seconds": execute_timeout_seconds,
        "mutates_environment": bool(execute),
        "artifact_generation_mutates_runtime": False,
        "task_selection_mutates_order": ranked["task_selection_mutates_order"],
        "task_order_changed": ranked["task_order_changed"],
        "mutates_runtime": False,
        "artifact_summary": artifact.get("summary", {}),
        "recommended_task_id": decision_support.get("recommended_task_id", ""),
        "recommended_description": decision_support.get("recommended_description", ""),
        "task_order": _task_order(artifact_tasks),
        "ranked_task_order": _task_order(ranked_tasks),
        "posthoc_ranked_task_order": _task_order(ranked_tasks),
        "runtime_selected_task_ids": runtime_selected_task_ids,
        "selected_task_id": (
            runtime_selected_task_ids[0]
            if execute and runtime_selected_task_ids
            else _task_id(selected_task) if selected_task is not None else ""
        ),
        "selected_description": selected_task.description if selected_task is not None else "",
        "final_score": sanitize_public_value(score),
        "progress": _progress_from_score(score),
        "score_available": bool(score),
        "score_ownership_verified": score_ownership_verified,
        "expected_score_identity": {
            "attempt_id": attempt_id,
            "task_name": runtime_launch_config.get("task_name", ""),
        },
        "meta_judger_diagnostics_available": bool(meta_judger_diagnostics),
        "load_status": _last_load_status(meta_judger_diagnostics),
        "error": error,
        "error_type": error_type,
        "timed_out": timed_out,
        "runtime_result_retained": bool(execute and retain_runtime_result and runtime_result_path.exists()),
        "runtime_process_isolated": bool(execute),
        "runtime_started": runtime_started,
        "runtime_process_exit_code": public_runtime_process["exit_code"],
        "runtime_process_terminated": public_runtime_process["terminated"],
        "runtime_process_killed": public_runtime_process["killed"],
        "runtime_process_alive_after_kill": public_runtime_process[
            "process_alive_after_kill"
        ],
        "runtime_process_group_alive_after_kill": public_runtime_process[
            "process_group_alive_after_kill"
        ],
        "runtime_target_lock_admission": runtime_target_lock_admission,
        "runtime_target_lock_unavailable": runtime_target_lock_unavailable,
        "runtime_target_lock_unavailable_reason": runtime_target_lock_unavailable_reason,
        "runtime_target_lock_owner": runtime_target_lock_owner,
        "runtime_target_lock_metadata_valid": runtime_target_lock_metadata_valid,
        "runtime_target_safe_to_reuse": runtime_target_safe_to_reuse,
        "runtime_target_quarantined": runtime_target_quarantined,
        "runtime_target_quarantine": runtime_target_quarantine,
        "runtime_primary_error": runtime_primary_error,
        "runtime_cleanup_state": runtime_cleanup_state,
        "bridge_cleanup": bridge_cleanup,
        "child_protocol": sanitize_public_value(child_protocol),
        "controller_shutdown_complete": bool(
            runtime_result.get("controller", {}).get("shutdown_complete", False)
        ),
        "controller_active_assignments": sanitize_public_value(
            runtime_result.get("controller", {}).get("active_assignments", {})
        ),
        "runtime_collection_errors": sanitize_public_value(
            runtime_result.get("collection_errors", [])
        ),
        "action_log_available": action_log_available,
        "events_available": False,
        "event_count": None,
        "event_warnings": list(base_normalized_events.warnings) if base_normalized_events is not None else [],
        "event_artifact_error": event_artifact_error,
        "event_artifact_error_detail": event_artifact_error_detail,
        "event_lifecycle_valid": False,
        "event_terminal_count": 0,
        "terminal_event_type": None,
        "finished_at": None,
    }
    if seed_resolution is not None:
        summary["seed_contract"] = seed_resolution.to_dict()
    if execute and launch_config.get("task_type") == "meta" and score.get("status") == "success":
        try:
            validate_judged_artifact_consistency(
                summary=summary,
                runtime_snapshot=runtime_task_dag_snapshot,
                score=score,
            )
        except ValueError as exc:
            summary["error"] = str(exc)
            summary["error_type"] = "JudgedArtifactConsistencyError"
            error = str(exc)
            error_type = "JudgedArtifactConsistencyError"
    required_artifacts = _required_artifacts(
        launch_config,
        execute=execute,
    )
    non_event_required_artifacts = tuple(
        name for name in required_artifacts if name != "events"
    )
    pre_event_admission = validate_experiment_artifact_admission(
        summary=summary,
        runtime_snapshot=runtime_task_dag_snapshot,
        action_log=action_log,
        required_artifacts=non_event_required_artifacts,
    )
    pre_event_guard_errors = validate_experiment_runtime_guards(
        summary=summary,
        enabled=bool(required_artifacts),
    )
    if (
        not pre_event_admission["passed"] or pre_event_guard_errors
    ) and error is None:
        error = "required experiment artifact admission failed"
        error_type = "RequiredArtifactError"
        summary["error"] = error
        summary["error_type"] = error_type
    if "events" in required_artifacts and base_normalized_events is None and error is None:
        error = "required experiment artifact admission failed"
        error_type = "RequiredArtifactError"
        summary["error"] = error
        summary["error_type"] = error_type

    finished_at = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
    terminal_event_type = (
        "run_timed_out" if timed_out else "run_failed" if error else "run_completed"
    )
    if base_normalized_events is not None:
        try:
            candidate_events = finalize_attempt_events(
                base_normalized_events.events,
                run_id=selected_run_name,
                attempt_id=attempt_id,
                mode=summary["mode"],
                started_at=started_at,
                finished_at=finished_at,
                terminal_event_type=terminal_event_type,
                error=error,
                error_type=error_type or None,
                warnings=base_normalized_events.warnings,
            )
            candidate_events = type(candidate_events)(
                events=tuple(sanitize_artifact_value(
                    candidate_events.events,
                    secret_values=secret_values,
                )),
                warnings=candidate_events.warnings,
            )
            validate_attempt_event_lifecycle(
                candidate_events.events,
                expected_run_id=selected_run_name,
                expected_attempt_id=attempt_id,
                expected_terminal_event_type=terminal_event_type,
            )
            _write_jsonl(output_dir / "events.jsonl", candidate_events.events)
            final_normalized_events = candidate_events
        except Exception as exc:
            final_normalized_events = None
            event_artifact_error = type(exc).__name__
            event_artifact_error_detail = str(exc)
    if "events" in required_artifacts and final_normalized_events is None and error is None:
        error = "required experiment artifact admission failed"
        error_type = "RequiredArtifactError"
        terminal_event_type = "run_timed_out" if timed_out else "run_failed"
        summary["error"] = error
        summary["error_type"] = error_type

    summary.update({
        "finished_at": finished_at,
        "terminal_event_type": terminal_event_type,
        "events_available": final_normalized_events is not None,
        "event_count": (
            len(final_normalized_events.events)
            if final_normalized_events is not None
            else None
        ),
        "event_warnings": (
            list(final_normalized_events.warnings)
            if final_normalized_events is not None
            else list(base_normalized_events.warnings)
            if base_normalized_events is not None
            else []
        ),
        "event_artifact_error": event_artifact_error,
        "event_artifact_error_detail": event_artifact_error_detail,
        "event_lifecycle_valid": final_normalized_events is not None,
        "event_terminal_count": (
            sum(
                event.get("event_type") in ATTEMPT_TERMINAL_EVENT_TYPES
                for event in final_normalized_events.events
            )
            if final_normalized_events is not None
            else 0
        ),
    })
    artifact_admission = validate_experiment_artifact_admission(
        summary=summary,
        runtime_snapshot=runtime_task_dag_snapshot,
        action_log=action_log,
        required_artifacts=required_artifacts,
        normalized_events=(
            final_normalized_events.events
            if final_normalized_events is not None
            else None
        ),
        expected_terminal_event_type=terminal_event_type,
        expected_run_id=selected_run_name,
        expected_attempt_id=attempt_id,
    )
    summary["artifact_admission"] = artifact_admission
    if not artifact_admission["passed"] and error is None:
        error = "required experiment artifact admission failed"
        error_type = "RequiredArtifactError"
        summary["error"] = error
        summary["error_type"] = error_type
        reconciled_terminal_event_type = (
            "run_timed_out" if timed_out else "run_failed"
        )
        event_admission_failed = "events" in (
            artifact_admission["missing"] + artifact_admission["invalid"]
        )
        if event_admission_failed:
            (output_dir / "events.jsonl").unlink(missing_ok=True)
            final_normalized_events = None
            terminal_event_type = reconciled_terminal_event_type
            summary.update({
                "terminal_event_type": terminal_event_type,
                "events_available": False,
                "event_count": None,
                "event_artifact_error": (
                    summary.get("event_artifact_error")
                    or "EventLifecycleConsistencyError"
                ),
                "event_artifact_error_detail": (
                    summary.get("event_artifact_error_detail")
                    or "final artifact admission rejected the event artifact"
                ),
                "event_lifecycle_valid": False,
                "event_terminal_count": 0,
            })
        elif final_normalized_events is not None:
            try:
                reconciled_events = finalize_attempt_events(
                    final_normalized_events.events,
                    run_id=selected_run_name,
                    attempt_id=attempt_id,
                    mode=summary["mode"],
                    started_at=started_at,
                    finished_at=finished_at,
                    terminal_event_type=reconciled_terminal_event_type,
                    error=error,
                    error_type=error_type,
                    warnings=final_normalized_events.warnings,
                )
                reconciled_events = type(reconciled_events)(
                    events=tuple(sanitize_artifact_value(
                        reconciled_events.events,
                        secret_values=secret_values,
                    )),
                    warnings=reconciled_events.warnings,
                )
                validate_attempt_event_lifecycle(
                    reconciled_events.events,
                    expected_run_id=selected_run_name,
                    expected_attempt_id=attempt_id,
                    expected_terminal_event_type=reconciled_terminal_event_type,
                )
                _write_jsonl(output_dir / "events.jsonl", reconciled_events.events)
                final_normalized_events = reconciled_events
                terminal_event_type = reconciled_terminal_event_type
                summary.update({
                    "terminal_event_type": terminal_event_type,
                    "event_count": len(reconciled_events.events),
                    "event_warnings": list(reconciled_events.warnings),
                    "event_terminal_count": 1,
                })
            except Exception as exc:
                final_normalized_events = None
                terminal_event_type = reconciled_terminal_event_type
                summary.update({
                    "terminal_event_type": terminal_event_type,
                    "events_available": False,
                    "event_count": None,
                    "event_artifact_error": type(exc).__name__,
                    "event_artifact_error_detail": str(exc),
                    "event_lifecycle_valid": False,
                    "event_terminal_count": 0,
                })
    judged_iteration_trace = {}
    judged_terminal_diagnostics = {}
    if execute and launch_config.get("task_type") == "meta":
        judged_iteration_trace = build_judged_iteration_trace(
            action_log=action_log,
            agent_history=_read_agent_history(
                runtime_root,
                runtime_launch_config.get("task_name", ""),
            ),
            final_score=score,
            agent_iteration_limit=runtime_result.get("agent_iteration_limit"),
            agent_iteration_limit_source=runtime_result.get(
                "agent_iteration_limit_source"
            ),
        )
        judged_terminal_diagnostics = build_judged_terminal_diagnostics(
            summary=summary,
            launch_config=launch_config,
            trace=judged_iteration_trace,
            runtime_snapshot=runtime_task_dag_snapshot,
        )
        summary.update({
            "judged_iteration_trace_available": True,
            "judged_terminal_diagnostics_available": True,
            "judged_root_cause_category": judged_terminal_diagnostics.get(
                "root_cause_category"
            ),
        })

    metrics = build_minecraft_metrics(
        summary=summary,
        action_log=action_log,
        task_graph_snapshot=task_graph_snapshot,
        decision_support=decision_support,
    )

    sanitized_launch_config = sanitize_artifact_value(
        sanitize_public_value(launch_config),
        secret_values=secret_values,
    )
    action_log = sanitize_artifact_value(action_log, secret_values=secret_values)
    task_graph_snapshot = sanitize_artifact_value(task_graph_snapshot, secret_values=secret_values)
    runtime_task_dag_snapshot = sanitize_artifact_value(runtime_task_dag_snapshot, secret_values=secret_values)
    artifact = sanitize_artifact_value(artifact, secret_values=secret_values)
    decision_support = sanitize_artifact_value(decision_support, secret_values=secret_values)
    metrics = sanitize_artifact_value(metrics, secret_values=secret_values)
    minecraft_eac_audit = sanitize_artifact_value(minecraft_eac_audit, secret_values=secret_values)
    judged_iteration_trace = sanitize_artifact_value(
        judged_iteration_trace,
        secret_values=secret_values,
    )
    judged_terminal_diagnostics = sanitize_artifact_value(
        judged_terminal_diagnostics,
        secret_values=secret_values,
    )
    summary = sanitize_artifact_value(summary, secret_values=secret_values)
    persisted_summary = {**summary, "output_dir": "."}
    _write_json(output_dir / "launch_config.json", sanitized_launch_config)
    if seed_resolution is not None:
        _write_json(output_dir / "seed_contract.json", seed_resolution.to_dict())
    _write_json(output_dir / "action_log.json", action_log)
    _write_json(output_dir / "task_graph_snapshot.json", task_graph_snapshot)
    _write_json(output_dir / "runtime_dual_dag_snapshot.json", runtime_task_dag_snapshot)
    _write_json(output_dir / "dual_dag_artifact.json", artifact)
    _write_json(output_dir / "decision_support.json", decision_support)
    _write_json(output_dir / "metrics.json", metrics)
    if minecraft_eac_audit.get("configured") is not False:
        _write_json(output_dir / "minecraft_eac_audit.json", minecraft_eac_audit)
    _write_json(output_dir / "summary.json", persisted_summary)
    if execute and launch_config.get("task_type") == "meta":
        _write_json(output_dir / "judged_iteration_trace.json", judged_iteration_trace)
        _write_json(
            output_dir / "judged_terminal_diagnostics.json",
            judged_terminal_diagnostics,
        )
    public_meta_judger_diagnostics = _sanitize_retained_meta_judger_diagnostics(
        runtime_result_path.parent / "meta_judger_diagnostics.json",
        output_dir=output_dir,
        secret_values=secret_values,
    )
    if public_meta_judger_diagnostics:
        _write_json(
            output_dir / "meta_judger_diagnostics.json",
            public_meta_judger_diagnostics,
        )
    if execute and not retain_runtime_result:
        _remove_runtime_result(runtime_result_path)
    elif execute:
        _sanitize_runtime_checkpoint(runtime_result_path, secret_values=secret_values)
    provenance_status = "timeout" if timed_out else "failure" if error else "success"
    finalize_provenance(output_dir, status=provenance_status)
    finalize_run_directory(
        output_dir,
        attempt_id=attempt_id,
        producer="benchmarks.minecraft.experiment",
        status="failed" if error else "completed",
    )
    _emit_attempt_terminal_event(
        attempt_state,
        "run_timed_out" if timed_out else "run_failed" if error else "run_completed",
        payload={"error": error, "error_type": error_type},
    )
    return summary


def _required_artifacts(config: dict, *, execute: bool) -> tuple[str, ...]:
    configured = config.get("required_artifacts")
    if configured is None:
        return DEFAULT_JUDGED_REQUIRED_ARTIFACTS if execute and config.get("task_type") == "meta" else ()
    if not isinstance(configured, list) or not all(
        isinstance(item, str) and item for item in configured
    ):
        raise ValueError("required_artifacts must be a list of non-empty strings")
    unknown = sorted(set(configured) - SUPPORTED_REQUIRED_ARTIFACTS)
    if unknown:
        raise ValueError(f"unsupported required artifact(s): {', '.join(unknown)}")
    return tuple(dict.fromkeys(configured))


def validate_experiment_artifact_admission(
    *,
    summary: dict,
    runtime_snapshot: dict,
    action_log: dict,
    required_artifacts: tuple[str, ...] | set[str],
    normalized_events: tuple[dict, ...] | None = None,
    expected_terminal_event_type: str | None = None,
    expected_run_id: str | None = None,
    expected_attempt_id: str | None = None,
) -> dict:
    required = list(required_artifacts)
    missing = []
    invalid = []

    if "score" in required:
        score = summary.get("final_score")
        if summary.get("score_available") is not True or not isinstance(score, dict):
            missing.append("score")
        elif summary.get("score_ownership_verified") is not True or score.get("status") != "success":
            invalid.append("score")
    if "runtime_task_dag" in required:
        nodes = runtime_snapshot.get("nodes") if isinstance(runtime_snapshot, dict) else None
        if not isinstance(nodes, list) or not nodes:
            missing.append("runtime_task_dag")
        elif (
            runtime_snapshot.get("summary", {}).get("terminal_state") != "success"
            or any(
                not isinstance(node, dict)
                or node.get("lifecycle", {}).get("status") != Task.success
                or bool(node.get("lifecycle", {}).get("active_agents"))
                for node in nodes
            )
        ):
            invalid.append("runtime_task_dag")
    if "action_log" in required:
        if summary.get("action_log_available") is not True or not isinstance(action_log, dict):
            missing.append("action_log")
        else:
            entries = [
                value
                for key, value in action_log.items()
                if key != "_attempt_id"
            ]
            if any(not isinstance(key, str) or not isinstance(value, list) for key, value in action_log.items() if key != "_attempt_id"):
                invalid.append("action_log")
            elif not any(entries):
                invalid.append("action_log")
    if "events" in required:
        if summary.get("events_available") is not True:
            missing.append("events")
        elif summary.get("event_artifact_error") is not None:
            invalid.append("events")
        elif (
            normalized_events is None
            or expected_terminal_event_type is None
            or expected_run_id is None
            or expected_attempt_id is None
        ):
            invalid.append("events")
        else:
            try:
                validate_attempt_event_lifecycle(
                    normalized_events,
                    expected_run_id=expected_run_id,
                    expected_attempt_id=expected_attempt_id,
                    expected_terminal_event_type=expected_terminal_event_type,
                )
            except ValueError:
                invalid.append("events")
    if "bridge_cleanup" in required:
        cleanup = summary.get("bridge_cleanup")
        if not isinstance(cleanup, dict) or not cleanup:
            missing.append("bridge_cleanup")
        else:
            processes = cleanup.get("processes")
            if (
                cleanup.get("cleanup_complete") is not True
                or not isinstance(processes, dict)
                or any(
                    not isinstance(item, dict)
                    or item.get("alive_after_kill") is True
                    for item in processes.values()
                )
            ):
                invalid.append("bridge_cleanup")
    if "child_protocol" in required:
        protocol = summary.get("child_protocol")
        if not isinstance(protocol, dict) or not protocol:
            missing.append("child_protocol")
        elif (
            protocol.get("status") != "completed"
            or protocol.get("result_valid") is not True
            or protocol.get("result_written") is not True
        ):
            invalid.append("child_protocol")

    invalid.extend(validate_experiment_runtime_guards(
        summary=summary,
        enabled=bool(required),
    ))
    return {
        "passed": not missing and not invalid,
        "required": required,
        "missing": list(dict.fromkeys(missing)),
        "invalid": list(dict.fromkeys(invalid)),
    }


def validate_experiment_runtime_guards(
    *,
    summary: dict,
    enabled: bool,
) -> list[str]:
    if not enabled:
        return []
    invalid = []
    if summary.get("runtime_collection_errors"):
        invalid.append("runtime_collection_errors")
    if (
        summary.get("controller_shutdown_complete") is not True
        or summary.get("controller_active_assignments")
    ):
        invalid.append("controller")
    if summary.get("runtime_target_safe_to_reuse") is not True:
        invalid.append("runtime_target")
    return invalid


def _emit_attempt_terminal_event(attempt_state: dict, event_type: str, *, payload: dict) -> None:
    if attempt_state.get("terminal_event_emitted"):
        return
    safe_emit_runtime_event(
        attempt_state.get("event_sink", NoOpRuntimeEventSink()),
        event_type,
        source="benchmarks.minecraft.experiment",
        payload=payload,
    )
    attempt_state["terminal_event_emitted"] = True


def _minecraft_provenance_assets(
    *,
    config_path: Path,
    launch_config: dict,
    execute: bool,
    runtime_llm_config: dict,
) -> list[dict]:
    task_type = str(launch_config.get("task_type", ""))
    judgers = {
        "construction": "env/build_judger.py",
        "farming": "env/farm_craft_judger.py",
        "puzzle": "env/escape_room_judger.py",
        "meta": "env/meta_judger.py",
        "gen": "env/llm_gen_judger.py",
    }
    root = Path(__file__).resolve().parents[2]
    server_version = launch_config.get("server_version")
    server_protocol = launch_config.get("server_protocol")
    server_identity = {
        "name": "minecraft_server",
        "kind": "runtime",
        "required": execute,
        "available": bool(server_version and server_protocol),
        "host": launch_config.get("host"),
        "port": launch_config.get("port"),
        "version": server_version,
        "protocol": server_protocol,
    }
    if not server_identity["available"]:
        server_identity["reason"] = "server_version_or_protocol_unavailable"
    return [
        file_identity(config_path, name="task_config", kind="task"),
        file_identity(
            launch_config.get("world_snapshot_path") or launch_config.get("reset_snapshot_path", ""),
            name="world_reset_snapshot",
            kind="dataset",
            required=execute,
        ),
        file_identity(
            launch_config.get("bridge_path", ""),
            name="minecraft_bridge",
            kind="runtime",
            required=execute,
        ),
        file_identity(
            root / judgers[task_type] if task_type in judgers else "",
            name="judger",
            kind="executable",
            required=execute,
        ),
        server_identity,
        _minecraft_model_identity(
            launch_config=launch_config,
            runtime_llm_config=runtime_llm_config,
            required=execute,
        ),
    ]


def _minecraft_model_identity(
    *,
    launch_config: dict,
    runtime_llm_config: dict,
    required: bool,
) -> dict:
    return model_identity(
        name="runtime_model",
        provider=str(runtime_llm_config.get("provider", "ollama")),
        model=str(runtime_llm_config.get("api_model", "")),
        metadata={
            "digest": launch_config.get("model_digest"),
            "revision": launch_config.get("model_revision"),
            "system_fingerprint": launch_config.get("model_system_fingerprint"),
        },
        required=required,
    )


def _minecraft_effective_settings(
    *,
    launch_config: dict,
    config_path: str | Path,
    config_index: int,
    run_name: str,
    task_selection_policy: str | None,
    execute: bool,
    execute_timeout_seconds: float | None,
    retain_runtime_result: bool,
    runtime_llm_config: dict,
    attempt_id: str,
) -> dict:
    return {
        "launch_config": launch_config,
        "config_path": str(Path(config_path)),
        "config_index": config_index,
        "run_name": run_name,
        "task_selection_policy": task_selection_policy,
        "execute": execute,
        "execute_timeout_seconds": execute_timeout_seconds,
        "retain_runtime_result": retain_runtime_result,
        "llm": runtime_llm_config,
        "attempt_id": attempt_id,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Minecraft real-environment experiment harness")
    parser.add_argument("--config", required=True, help="Launch config JSON file")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--run-name", default=None)
    parser.add_argument("--config-index", type=int, default=0)
    parser.add_argument("--task-selection-policy", choices=TASK_SELECTION_POLICIES, default="dual-dag", help="Runtime task ordering policy")
    parser.add_argument("--dual-dag-task-selection", action="store_true", help="Deprecated compatibility flag; equivalent to --task-selection-policy dual-dag")
    parser.add_argument("--no-dual-dag-task-selection", action="store_true", help="Deprecated compatibility flag; equivalent to --task-selection-policy original")
    parser.add_argument("--execute", action="store_true", help="Run the real Minecraft environment")
    parser.add_argument("--execute-timeout-seconds", type=float, default=None, help="Bound real execute mode and preserve artifacts on timeout")
    parser.add_argument("--retain-runtime-result", action="store_true", help="Keep the per-run internal runtime result after normalized artifacts are written")
    parser.add_argument("--overwrite", action="store_true", help="Explicitly replace an existing non-empty run directory")
    parser.add_argument("--execution-root", help="Immutable runtime execution checkout")
    args = parser.parse_args(argv)

    summary = run_minecraft_experiment(
        config_path=args.config,
        output_root=args.output_root,
        run_name=args.run_name,
        config_index=args.config_index,
        enable_dual_dag_task_selection=not args.no_dual_dag_task_selection,
        task_selection_policy=_policy_from_args(args),
        execute=args.execute,
        execute_timeout_seconds=args.execute_timeout_seconds,
        retain_runtime_result=args.retain_runtime_result,
        command_text=_command_text(args),
        overwrite=args.overwrite,
        execution_root=args.execution_root,
    )
    print(json.dumps(summary, indent=2))
    return 0 if summary.get("error") is None else 1


def _load_config(config_path: str | Path, *, config_index: int, execute: bool = False) -> dict:
    config = _read_json(Path(config_path), default=None)
    if isinstance(config, list):
        if config_index < 0 or config_index >= len(config):
            raise ValueError(f"Minecraft config index out of range: {config_index}")
        selected = config[config_index]
        if not isinstance(selected, dict):
            raise ValueError(f"Minecraft config entry at index {config_index} must be an object")
        return validate_minecraft_config(dict(selected), context=f"config[{config_index}]", execute=execute)
    if isinstance(config, dict):
        return validate_minecraft_config(dict(config), context="config", execute=execute)
    raise ValueError(f"Unsupported Minecraft config shape: {config_path}")


def validate_minecraft_config(config: dict, *, context: str = "config", execute: bool = False) -> dict:
    missing = [field for field in REQUIRED_CONFIG_FIELDS if field not in config]
    if missing:
        raise ValueError(f"{context} missing required field(s): {', '.join(missing)}")
    agent_num = _required_int(config, "agent_num", context=context)
    port = _required_int(config, "port", context=context)
    task_idx = _required_int(config, "task_idx", context=context)
    if agent_num < 0 or execute and agent_num < 1:
        requirement = "positive" if execute else "non-negative"
        raise ValueError(f"{context}.agent_num must be {requirement}")
    if port <= 0:
        raise ValueError(f"{context}.port must be positive")
    if task_idx < 0:
        raise ValueError(f"{context}.task_idx must be non-negative")
    if execute and config.get("task_type") == "meta":
        task_scenario = config.get("task_scenario")
        if not isinstance(task_scenario, str) or not task_scenario.strip():
            raise ValueError(f"{context}.task_scenario must be a non-empty string for meta execute mode")
    document_file = config.get("document_file")
    if document_file is not None and not isinstance(document_file, str):
        raise ValueError(f"{context}.document_file must be a string or null")
    if config.get("task_selection_policy") not in (None, *TASK_SELECTION_POLICIES):
        raise ValueError(f"{context}.task_selection_policy must be one of: {', '.join(TASK_SELECTION_POLICIES)}")
    seed_contract = SeedContract.from_value(config.get("seed_contract"))
    if seed_contract is not None:
        config["seed_contract"] = seed_contract.to_dict()
    _validate_smoke_tasks(config, context=context)
    action_log = config.get("smoke_action_log", {})
    if action_log is not None and not isinstance(action_log, dict):
        raise ValueError(f"{context}.smoke_action_log must be an object")
    return config


def _resolve_minecraft_seed_contract(config: dict):
    if config.get("seed_contract") is None:
        return None
    supported = set()
    if config.get("task_type") == "meta":
        supported.update({SeedScope.PYTHON_RANDOM, SeedScope.META_JUDGER})
    return resolve_seed_contract(
        config["seed_contract"],
        supported_scopes=supported,
        applied_scopes=supported,
    )


def _validate_smoke_tasks(config: dict, *, context: str) -> None:
    smoke_tasks = config.get("smoke_tasks")
    if smoke_tasks is None:
        return
    if not isinstance(smoke_tasks, list):
        raise ValueError(f"{context}.smoke_tasks must be a list")
    for index, task in enumerate(smoke_tasks):
        task_context = f"{context}.smoke_tasks[{index}]"
        if not isinstance(task, dict):
            raise ValueError(f"{task_context} must be an object")
        if "description" not in task:
            raise ValueError(f"{task_context} missing required field: description")
        for list_field in ("candidate_agents", "assigned_agents", "required_subtasks", "required subtasks"):
            if list_field in task and not isinstance(task[list_field], list):
                raise ValueError(f"{task_context}.{list_field} must be a list")
        candidate_agents = task.get("candidate_agents")
        assigned_agents = task.get("assigned_agents")
        if candidate_agents is None and assigned_agents is None:
            raise ValueError(f"{task_context} must provide non-empty candidate_agents")
        for field, names in (
            ("candidate_agents", candidate_agents),
            ("assigned_agents", assigned_agents),
        ):
            if names is None:
                continue
            if not names:
                raise ValueError(f"{task_context}.{field} must be non-empty")
            if any(not isinstance(name, str) or not name for name in names):
                raise ValueError(f"{task_context}.{field} must contain non-empty names")
            if len({name.casefold() for name in names}) != len(names):
                raise ValueError(f"{task_context}.{field} must contain unique names")
        candidates = candidate_agents if candidate_agents is not None else assigned_agents
        required = _required_int(task, "number", context=task_context) if "number" in task else (
            len(assigned_agents) if assigned_agents is not None else 1
        )
        if required <= 0:
            raise ValueError(f"{task_context}.number must be positive")
        if required > len(candidates):
            raise ValueError(
                f"{task_context}.number must not exceed candidate agent count"
            )
        if assigned_agents is not None and required != len(assigned_agents):
            raise ValueError(
                f"{task_context}.number must match assigned agent count"
            )
        if candidate_agents is not None and assigned_agents is not None and any(
            name.casefold() not in {candidate.casefold() for candidate in candidate_agents}
            for name in assigned_agents
        ):
            raise ValueError(f"{task_context}.assigned_agents must be candidate agents")


def _required_int(config: dict, field: str, *, context: str) -> int:
    value = config[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{context}.{field} must be an integer")
    return value


def _last_load_status(diagnostics: dict) -> str | None:
    history = diagnostics.get("load_status_history", []) if isinstance(diagnostics, dict) else []
    if not history:
        return None
    last = history[-1]
    return last.get("status") if isinstance(last, dict) else None


def validate_judged_artifact_consistency(
    *,
    summary: dict,
    runtime_snapshot: dict,
    score: dict,
) -> None:
    errors = []
    if summary.get("error") is not None:
        errors.append("summary.error must be null")
    if summary.get("timed_out") is not False:
        errors.append("summary.timed_out must be false")
    if summary.get("score_available") is not True:
        errors.append("summary.score_available must be true")
    if summary.get("score_ownership_verified") is not True:
        errors.append("score ownership must be verified")
    if score.get("attempt_id") != summary.get("attempt_id"):
        errors.append("score attempt_id must match summary attempt_id")
    if runtime_snapshot.get("summary", {}).get("terminal_state") != "success":
        errors.append("runtime task DAG terminal_state must be success")
    nodes = runtime_snapshot.get("nodes", [])
    if not nodes:
        errors.append("runtime task DAG must contain at least one task")
    for node in nodes:
        lifecycle = node.get("lifecycle", {})
        if lifecycle.get("status") != Task.success:
            errors.append("all runtime tasks must have success status")
            break
        if lifecycle.get("active_agents"):
            errors.append("all runtime task active_agents must be empty")
            break
    if summary.get("controller_shutdown_complete") is not True:
        errors.append("controller shutdown must be complete")
    if summary.get("controller_active_assignments"):
        errors.append("controller active assignments must be empty")
    if errors:
        raise ValueError("inconsistent judged artifact: " + "; ".join(errors))


def _sanitize_retained_meta_judger_diagnostics(
    path: Path,
    *,
    output_dir: Path,
    secret_values: tuple[str, ...],
) -> dict:
    if not path.exists():
        return {}
    try:
        diagnostics = _read_json(path, default={})
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        public_diagnostics = _invalid_retained_diagnostics(type(exc).__name__)
        _write_runtime_checkpoint(path, public_diagnostics)
        return public_diagnostics
    if not isinstance(diagnostics, dict):
        public_diagnostics = _invalid_retained_diagnostics("TypeError")
        _write_runtime_checkpoint(path, public_diagnostics)
        return public_diagnostics
    public_diagnostics = project_public_path_fields(
        diagnostics,
        repository_root=output_dir.resolve(),
    )
    command = diagnostics.get("command")
    if isinstance(command, list):
        public_diagnostics["command"] = project_public_argv_paths(
            [str(argument) for argument in command],
            repository_root=output_dir.resolve(),
        )
    public_diagnostics = sanitize_artifact_value(
        public_diagnostics,
        secret_values=secret_values,
    )
    _write_runtime_checkpoint(path, public_diagnostics)
    return public_diagnostics


def _invalid_retained_diagnostics(error_type: str) -> dict:
    return {
        "schema_version": 1,
        "status": "invalid",
        "error": "retained meta judger diagnostics could not be published",
        "error_type": error_type,
    }


def _runtime_execution_identity(execution: RuntimeExecution) -> dict:
    category = getattr(execution, "category", None)
    if not category:
        module_root = Path(__file__).resolve().parents[2]
        category = (
            "module_checkout"
            if execution.root == module_root
            else "explicit_root"
        )
    return {
        "category": str(category),
        "manifest_sha256": str(execution.manifest_sha256),
    }


def _resolve_runtime_execution(root: str | Path | None):
    try:
        execution = RuntimeExecution.resolve(root=root)
        execution.verify()
        return execution, _runtime_execution_identity(execution)
    except Exception as exc:
        # Verification errors must not turn an untrusted checkout path into a
        # public failure artifact.
        raise RuntimeError(
            f"runtime execution verification failed ({exc.__class__.__name__})"
        ) from None


def _execute_real_runtime(
    launch_config: dict,
    *,
    dual_dag_config: dict,
    runtime_result_path: Path,
    runtime_event_path: Path | None = None,
    runtime_root: Path | None = None,
    attempt_id: str | None = None,
    runtime_execution: RuntimeExecution | None = None,
    runtime_execution_identity: dict | None = None,
) -> dict:
    from start_with_config import run
    llm_config = _runtime_llm_config(launch_config)
    config = dict(launch_config)
    if dual_dag_config.get("eac_mode"):
        if config.get("judged_execution") is not False or config.get("production") is not False:
            raise ValueError("EAC execute requires explicit non-judged, non-production admission")
        premanifest = config.get("eac_premanifest")
        if not isinstance(premanifest, str) or not premanifest:
            raise ValueError("EAC execute requires launch_config.eac_premanifest")
        execution_revision = config.get("eac_execution_revision")
        if not isinstance(execution_revision, str) or not execution_revision:
            raise ValueError("EAC execute requires launch_config.eac_execution_revision")
        dual_dag_config = {
            **dual_dag_config,
            "eac_premanifest": premanifest,
            "eac_execution_revision": execution_revision,
            "judged_execution": False,
            "production": False,
        }
    document = config.get("evaluation_arg", {}) if config.get("task_type") == "meta" else {}
    runtime_root = runtime_root or runtime_result_path.parent
    attempt_id = attempt_id or config.get("attempt_id")
    return run(
        llm_config["api_model"],
        llm_config["api_base"],
        config["task_type"],
        config["task_idx"],
        config["agent_num"],
        config.get("dig_needed", False),
        config.get("max_task_num", 0),
        config["task_goal"],
        config.get("document_file", ""),
        config["host"],
        config["port"],
        config["task_name"],
        config.get("role", "same"),
        [llm_config.get("api_key_list", [])],
        document,
        minecraft_dual_dag_config=dual_dag_config,
        runtime_result_path=str(runtime_result_path),
        task_scenario=config.get("task_scenario"),
        runtime_event_path=str(runtime_event_path) if runtime_event_path is not None else None,
        emit_controller_terminal_event=False,
        runtime_paths=RuntimePaths.isolated(runtime_root),
        attempt_id=attempt_id,
        require_action_evidence=bool(config.get("require_action_evidence", True)),
        seed_contract=config.get("seed_contract"),
        world_initialization=config.get("world_initialization"),
        position_convention=config.get("position_convention"),
        runtime_execution=runtime_execution,
    )


def _runtime_llm_config(launch_config: dict) -> dict:
    from model.ollama_config import make_ollama_llm_config

    key_env = launch_config.get("api_key_env")
    api_model = launch_config.get("api_model")
    api_base = launch_config.get("api_base")
    if key_env is None:
        if api_model is None and api_base is None:
            return make_ollama_llm_config()
        if not all(isinstance(value, str) and value.strip() for value in (api_model, api_base)):
            raise RuntimeError("real Minecraft execution requires api_model and api_base")
        return make_ollama_llm_config(api_model=api_model, api_base=api_base, api_key="ollama")
    if not isinstance(key_env, str) or not key_env.strip():
        raise RuntimeError("api_key_env must be a non-empty environment variable name")
    if not all(isinstance(value, str) and value.strip() for value in (api_model, api_base)):
        raise RuntimeError("real Minecraft execution requires api_model and api_base")
    api_key = os.environ.get(key_env)
    if not api_key:
        raise RuntimeError(f"required model credential environment variable is not set: {key_env}")
    return make_ollama_llm_config(api_model=api_model, api_base=api_base, api_key=api_key)


def _execute_real_runtime_bounded(
    launch_config: dict,
    *,
    dual_dag_config: dict,
    timeout_seconds: float | None,
    runtime_result_path: Path,
    runtime_event_path: Path | None = None,
    runtime_root: Path | None = None,
    attempt_id: str | None = None,
    runtime_execution: RuntimeExecution | None = None,
    runtime_execution_identity: dict | None = None,
) -> dict:
    timeout_seconds = _require_positive_finite_timeout(timeout_seconds)
    context = multiprocessing.get_context()
    status_queue = context.Queue()
    process = context.Process(
        target=_runtime_process_entry,
        args=(
            launch_config,
            dual_dag_config,
            str(runtime_result_path),
            str(runtime_event_path) if runtime_event_path is not None else None,
            str(runtime_root) if runtime_root is not None else None,
            attempt_id,
            str(runtime_execution.root) if runtime_execution is not None else None,
            runtime_execution_identity,
            status_queue,
        ),
    )
    process_started = False
    process_group_id = None
    try:
        process.start()
        process_started = True
        process_group_id = _wait_for_isolated_process_group(process)
        process.join(timeout_seconds)
        if process.is_alive():
            partial_before_termination, _ = _read_partial_runtime_result(runtime_result_path)
            timeout_message = f"Minecraft execute mode timed out after {timeout_seconds} seconds"
            process_metadata = _terminate_runtime_process(
                process,
                grace_seconds=RUNTIME_TERMINATE_GRACE_SECONDS,
                process_group_id=process_group_id,
            )
            if partial_before_termination and not runtime_result_path.exists():
                _write_runtime_checkpoint(runtime_result_path, partial_before_termination)
            _validate_runtime_cleanup(
                process_metadata,
                context="after execute timeout",
                timed_out=True,
                primary_error={
                    "error_type": "MinecraftExecuteTimeoutError",
                    "message": timeout_message,
                },
            )
            raise MinecraftExecuteTimeoutError(
                timeout_message,
                process_metadata=process_metadata,
            )

        process_metadata = _cleanup_exited_runtime_process_group(
            process,
            process_group_id=process_group_id,
        )
        child_status = _read_child_status(status_queue)
        child_protocol = _validate_child_status(
            child_status,
            expected_attempt_id=attempt_id,
            expected_task_name=launch_config.get("task_name"),
            process_metadata=process_metadata,
        )
        if child_status["status"] == "error":
            raise MinecraftRuntimeChildError(
                child_status.get("error", "Minecraft runtime child failed"),
                error_type=child_status.get("error_type", "RuntimeError"),
                process_metadata=process_metadata,
                child_protocol=child_protocol,
            )
        if process.exitcode not in (0, None):
            raise MinecraftRuntimeChildError(
                f"Minecraft runtime child exited with code {process.exitcode}",
                error_type="ChildProcessError",
                process_metadata=process_metadata,
                child_protocol=child_protocol,
            )
        result = _read_completed_runtime_result(
            runtime_result_path,
            expected_attempt_id=attempt_id,
            expected_task_name=launch_config.get("task_name"),
            process_metadata=process_metadata,
            child_protocol=child_protocol,
        )
        result["runtime_process"] = process_metadata
        result["child_protocol"] = child_protocol
        return result
    finally:
        group_alive = process_group_id is not None and _process_group_exists(process_group_id)
        if process_started and (process.is_alive() or group_alive):
            final_cleanup_metadata = _terminate_runtime_process(
                process,
                grace_seconds=RUNTIME_TERMINATE_GRACE_SECONDS,
                process_group_id=process_group_id,
            )
            active_error = sys.exc_info()[1]
            if isinstance(active_error, (MinecraftExecuteTimeoutError, MinecraftRuntimeChildError)):
                initial_cleanup_metadata = dict(active_error.process_metadata)
                active_error.process_metadata = final_cleanup_metadata
                cleanup_state = {
                    "initial": initial_cleanup_metadata,
                    "final": dict(final_cleanup_metadata),
                }
                if isinstance(active_error, MinecraftRuntimeChildError):
                    active_error.cleanup_state = cleanup_state
            if not (
                isinstance(active_error, MinecraftRuntimeChildError)
                and active_error.error_type == "ProcessGroupCleanupError"
            ):
                _validate_runtime_cleanup(
                    final_cleanup_metadata,
                    context="during final cleanup",
                    timed_out=isinstance(active_error, MinecraftExecuteTimeoutError)
                    or bool(getattr(active_error, "timed_out", False)),
                    primary_error=_primary_error_metadata(active_error),
                    cleanup_state=cleanup_state
                    if isinstance(active_error, (MinecraftExecuteTimeoutError, MinecraftRuntimeChildError))
                    else {"final": dict(final_cleanup_metadata)},
                )
        status_queue.close()
        if hasattr(status_queue, "join_thread"):
            if hasattr(status_queue, "cancel_join_thread"):
                status_queue.cancel_join_thread()
            else:
                status_queue.join_thread()


def _runtime_process_entry(
    launch_config: dict,
    dual_dag_config: dict,
    runtime_result_path: str,
    runtime_event_path: str | None,
    runtime_root: str | None,
    attempt_id: str | None,
    execution_root: str | None,
    runtime_execution_identity: dict | None,
    status_queue,
) -> None:
    try:
        if hasattr(os, "setsid"):
            os.setsid()
        runtime_execution = None
        if runtime_execution_identity is not None:
            runtime_execution, resolved_identity = _resolve_runtime_execution(execution_root)
            if resolved_identity != runtime_execution_identity:
                raise RuntimeError("runtime execution manifest identity mismatch")
            # The forked worker must not inherit module or relative-path lookup
            # behavior from the caller's artifact directory.
            os.chdir(runtime_execution.root)
            os.environ["PYTHONPATH"] = str(runtime_execution.root)
            root_text = str(runtime_execution.root)
            trusted_prefixes = tuple(
                Path(prefix).resolve() for prefix in {sys.prefix, sys.base_prefix}
            )
            sys.path[:] = [
                root_text,
                *(
                    entry
                    for entry in sys.path
                    if entry
                    and entry != root_text
                    and any(
                        Path(entry).resolve() == prefix
                        or prefix in Path(entry).resolve().parents
                        for prefix in trusted_prefixes
                    )
                ),
            ]
            # Fork inherits caller modules. Drop runtime packages so imports
            # below resolve from the approved execution root.
            root_modules = tuple(
                Path(asset.relative_path).stem
                for asset in runtime_execution.assets.values()
                if "/" not in asset.relative_path and asset.relative_path.endswith(".py")
            )
            runtime_prefixes = (
                "start_with_config", "config", "env", "model", "pipeline", "rl_env",
                "speaking_style", "type_define",
                *root_modules,
            )
            for module_name in tuple(sys.modules):
                if module_name == __name__:
                    continue
                if (
                    module_name == "benchmarks.common"
                    or module_name.startswith("benchmarks.common.")
                    or module_name == "benchmarks.minecraft"
                    or module_name.startswith("benchmarks.minecraft.")
                    or any(
                        module_name == prefix or module_name.startswith(prefix + ".")
                        for prefix in runtime_prefixes
                    )
                ):
                    sys.modules.pop(module_name, None)
            for package_name in ("benchmarks",):
                package = sys.modules.get(package_name)
                if package is not None and hasattr(package, "__path__"):
                    relative = Path(*package_name.split("."))
                    package.__path__[:] = [str(runtime_execution.root / relative)]
            importlib.invalidate_caches()
        result = _execute_real_runtime(
            launch_config,
            dual_dag_config=dual_dag_config,
            runtime_result_path=Path(runtime_result_path),
            runtime_event_path=Path(runtime_event_path) if runtime_event_path is not None else None,
            runtime_root=Path(runtime_root) if runtime_root is not None else None,
            attempt_id=attempt_id,
            runtime_execution=runtime_execution,
            runtime_execution_identity=runtime_execution_identity,
        ) or {}
        if not isinstance(result, dict):
            raise TypeError("Minecraft runtime result must be an object")
        result_path = Path(runtime_result_path)
        if not result and result_path.exists():
            persisted, persisted_error = _read_partial_runtime_result(result_path)
            if persisted_error is not None:
                raise RuntimeError(persisted_error["error"])
            result = persisted
        result.setdefault("attempt_id", attempt_id)
        result.setdefault("task_name", launch_config.get("task_name"))
        _write_runtime_checkpoint(result_path, result)
        status_queue.put({
            "schema_version": 1,
            "status": "completed",
            "attempt_id": attempt_id,
            "task_name": launch_config.get("task_name"),
            "result_written": True,
        })
    except BaseException as exc:
        result_written = False
        partial, _ = _read_partial_runtime_result(Path(runtime_result_path))
        partial.setdefault("attempt_id", attempt_id)
        partial.setdefault("task_name", launch_config.get("task_name"))
        partial["error"] = str(exc)
        partial["error_type"] = exc.__class__.__name__
        try:
            _write_runtime_checkpoint(Path(runtime_result_path), partial)
            result_written = True
        except Exception:
            pass
        try:
            status_queue.put({
                "schema_version": 1,
                "status": "error",
                "attempt_id": attempt_id,
                "task_name": launch_config.get("task_name"),
                "error": str(exc),
                "error_type": exc.__class__.__name__,
                "result_written": result_written,
            })
        finally:
            raise


def _terminate_runtime_process(
    process,
    *,
    grace_seconds: float,
    process_group_id: int | None = None,
) -> dict:
    process_group_id = process_group_id or _isolated_process_group_id(process)
    group_signaled = process_group_id is not None and _signal_process_group(
        process_group_id,
        signal.SIGTERM,
    )
    if not group_signaled and process.is_alive():
        process.terminate()
    process.join(grace_seconds)
    killed = False
    group_alive = process_group_id is not None and _process_group_exists(process_group_id)
    if process.is_alive() or group_alive:
        if process_group_id is not None:
            group_signaled = _signal_process_group(process_group_id, signal.SIGKILL)
        if not group_signaled and process.is_alive():
            process.kill()
        process.join(grace_seconds)
        killed = True
    process_alive_after_kill = process.is_alive()
    process_group_alive_after_kill = (
        process_group_id is not None
        and not _wait_for_process_group_exit(
            process_group_id,
            timeout_seconds=grace_seconds,
        )
    )
    return {
        "exit_code": process.exitcode,
        "terminated": True,
        "killed": killed,
        "process_alive_after_kill": process_alive_after_kill,
        "process_group_alive_after_kill": process_group_alive_after_kill,
    }


def _cleanup_exited_runtime_process_group(
    process,
    *,
    process_group_id: int | None,
) -> dict:
    process_metadata = {
        "exit_code": process.exitcode,
        "terminated": False,
        "killed": False,
        "process_alive_after_kill": False,
        "process_group_alive_after_kill": False,
    }
    if process_group_id is None or not _process_group_exists(process_group_id):
        return process_metadata
    process_metadata = _terminate_runtime_process(
        process,
        grace_seconds=RUNTIME_TERMINATE_GRACE_SECONDS,
        process_group_id=process_group_id,
    )
    _validate_runtime_cleanup(
        process_metadata,
        context="after child exit",
    )
    return process_metadata


def _validate_runtime_cleanup(
    process_metadata: dict,
    *,
    context: str,
    timed_out: bool = False,
    primary_error: dict | None = None,
    cleanup_state: dict | None = None,
) -> None:
    if not (
        process_metadata.get("process_alive_after_kill")
        or process_metadata.get("process_group_alive_after_kill")
    ):
        return
    raise MinecraftRuntimeChildError(
        f"Minecraft runtime cleanup failed {context}",
        error_type="ProcessGroupCleanupError",
        process_metadata=process_metadata,
        timed_out=timed_out,
        primary_error=primary_error,
        cleanup_state=cleanup_state,
    )


def _primary_error_metadata(error: BaseException | None) -> dict:
    if error is None:
        return {}
    return {
        "error_type": error.__class__.__name__,
        "message": str(error),
    }


def _public_bridge_cleanup(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    processes = value.get("processes")
    public_processes = {}
    if isinstance(processes, dict):
        for name, metadata in processes.items():
            if not isinstance(name, str) or not name:
                continue
            if not isinstance(metadata, dict):
                public_processes[name] = {
                    "terminated": None,
                    "killed": None,
                    "alive_after_kill": None,
                }
                continue
            public_processes[name] = {
                key: _public_optional_bool(metadata, key)
                for key in ("terminated", "killed", "alive_after_kill")
            }
    return {
        "cleanup_complete": _public_optional_bool(value, "cleanup_complete"),
        "processes": public_processes,
    }


def _public_runtime_process(value: object) -> dict:
    if not isinstance(value, dict):
        value = {}
    return {
        "exit_code": value.get("exit_code"),
        "terminated": _public_optional_bool(value, "terminated"),
        "killed": _public_optional_bool(value, "killed"),
        "process_alive_after_kill": _public_optional_bool(
            value,
            "process_alive_after_kill",
        ),
        "process_group_alive_after_kill": _public_optional_bool(
            value,
            "process_group_alive_after_kill",
        ),
    }


def _public_optional_bool(mapping: dict, field: str) -> bool | None:
    value = mapping.get(field)
    return value if isinstance(value, bool) else None


def _public_target_quarantine(record: dict, *, status: str) -> dict:
    return {
        "status": status,
        "lock_key": record.get("lock_key", ""),
        "source_attempt_id": record.get("attempt_id", ""),
        "source_run_name": record.get("run_name", ""),
        "reasons": list(record.get("reasons", [])),
        "diagnostics": dict(record.get("diagnostics", {})),
    }


def _public_target_lock_owner(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    return {
        field: value[field]
        for field in ("status", "attempt_id", "run_name")
        if isinstance(value.get(field), str) and value[field]
    }


def _isolated_process_group_id(process) -> int | None:
    if os.name != "posix" or not getattr(process, "pid", None):
        return None
    try:
        process_group_id = os.getpgid(process.pid)
    except ProcessLookupError:
        return None
    return process_group_id if process_group_id == process.pid else None


def _wait_for_isolated_process_group(process, *, timeout_seconds: float = 0.5) -> int | None:
    if os.name != "posix" or not getattr(process, "pid", None):
        return None
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        process_group_id = _isolated_process_group_id(process)
        if process_group_id is not None:
            return process_group_id
        if not process.is_alive():
            return process.pid
        time.sleep(0.01)
    return _isolated_process_group_id(process) or process.pid


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_process_group_exit(
    process_group_id: int,
    *,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while _process_group_exists(process_group_id):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def _signal_process_group(process_group_id: int, sent_signal: int) -> bool:
    try:
        os.killpg(process_group_id, sent_signal)
    except ProcessLookupError:
        return False
    return True


def _read_child_status(status_queue):
    try:
        return status_queue.get(timeout=0.5)
    except queue.Empty:
        return None


def _validate_child_status(
    child_status,
    *,
    expected_attempt_id: str | None,
    expected_task_name: str | None,
    process_metadata: dict,
) -> dict:
    def protocol_error(message: str):
        raise MinecraftRuntimeChildError(
            message,
            error_type="ChildProtocolError",
            process_metadata=process_metadata,
            child_protocol={
                "schema_version": 1,
                "status_received": child_status is not None,
                "status": child_status.get("status") if isinstance(child_status, dict) else None,
                "exit_code": process_metadata.get("exit_code"),
                "result_written": False,
                "result_valid": False,
            },
        )

    if not isinstance(child_status, dict):
        protocol_error("Minecraft runtime child exited without a completion status")
    if child_status.get("schema_version") != 1:
        protocol_error("Minecraft runtime child status has an unsupported schema")
    if child_status.get("status") not in ("completed", "error"):
        protocol_error("Minecraft runtime child status is not terminal")
    if child_status.get("attempt_id") != expected_attempt_id:
        protocol_error("Minecraft runtime child status attempt mismatch")
    if child_status.get("task_name") != expected_task_name:
        protocol_error("Minecraft runtime child status task mismatch")
    if child_status.get("status") == "completed" and child_status.get("result_written") is not True:
        protocol_error("Minecraft runtime child did not persist a runtime result")
    return {
        "schema_version": 1,
        "status_received": True,
        "status": child_status["status"],
        "exit_code": process_metadata.get("exit_code"),
        "result_written": child_status.get("result_written") is True,
        "result_valid": False,
    }


def _read_completed_runtime_result(
    path: Path,
    *,
    expected_attempt_id: str | None,
    expected_task_name: str | None,
    process_metadata: dict,
    child_protocol: dict,
) -> dict:
    def protocol_error(message: str):
        raise MinecraftRuntimeChildError(
            message,
            error_type="ChildProtocolError",
            process_metadata=process_metadata,
            child_protocol=child_protocol,
        )

    if not path.is_file():
        protocol_error("Minecraft runtime child completed without a runtime result")
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        protocol_error("Minecraft runtime child wrote an invalid runtime result")
    if not isinstance(result, dict):
        protocol_error("Minecraft runtime child result must be an object")
    if result.get("attempt_id") != expected_attempt_id:
        protocol_error("Minecraft runtime child result attempt mismatch")
    if result.get("task_name") != expected_task_name:
        protocol_error("Minecraft runtime child result task mismatch")
    if result.get("error") is not None:
        raise MinecraftRuntimeChildError(
            str(result["error"]),
            error_type=str(result.get("error_type") or "RuntimeError"),
            process_metadata=process_metadata,
            child_protocol=child_protocol,
        )
    child_protocol["result_valid"] = True
    return result


def _read_partial_runtime_result(path: Path) -> tuple[dict, dict | None]:
    if not path.exists():
        return {}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, {
            "field": "runtime_result",
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
    if not isinstance(payload, dict):
        return {}, {
            "field": "runtime_result",
            "error": "runtime result must be an object",
            "error_type": "TypeError",
        }
    return payload, None


def _write_runtime_checkpoint(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary_path, path)


def _sanitize_runtime_checkpoint(path: Path, *, secret_values: tuple[str, ...]) -> None:
    if not path.exists():
        return
    payload = _read_json(path, default={})
    if isinstance(payload, dict):
        _write_runtime_checkpoint(
            path,
            sanitize_artifact_value(payload, secret_values=secret_values),
        )


def _write_minimal_failure_artifacts(attempt_state: dict) -> None:
    output_dir = attempt_state["output_dir"]
    secret_values = attempt_state.get("secret_values", ())
    error = sanitize_artifact_value(
        str(attempt_state.get("error") or "Benchmark setup failed before normalized artifacts were written."),
        secret_values=secret_values,
    )
    summary_path = output_dir / "summary.json"
    if not summary_path.exists():
        _write_json(summary_path, {
            "attempt_id": attempt_state["attempt_id"],
            "run_name": attempt_state.get("run_name", output_dir.name),
            "mode": "execute" if attempt_state.get("execute") else "dry_run",
            "artifact_summary": {},
            "action_log_available": False,
            "error": error,
            "error_type": attempt_state.get("error_type", "setup_error"),
        })
    metrics_path = output_dir / "metrics.json"
    if not metrics_path.exists():
        _write_json(metrics_path, {
            "task_count": 0,
            "completed_task_count": 0,
            "task_completion_rate": None,
            "progress": None,
            "action_count": None,
            "error": error,
        })


def _repair_failed_event_artifact(attempt_state: dict) -> None:
    output_dir = attempt_state["output_dir"]
    events_path = output_dir / "events.jsonl"
    existing_events = []
    if events_path.exists():
        try:
            existing_events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, json.JSONDecodeError):
            existing_events = []
    error = sanitize_artifact_value(
        str(attempt_state.get("error") or "experiment finalization failed"),
        secret_values=attempt_state.get("secret_values", ()),
    )
    error_type = str(attempt_state.get("error_type") or "RuntimeError")
    terminal_event_type = (
        "run_timed_out"
        if error_type in {"TimeoutError", "MinecraftExecuteTimeoutError"}
        else "run_failed"
    )
    finished_at = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
    try:
        repaired = finalize_attempt_events(
            tuple(existing_events),
            run_id=attempt_state.get("run_name", output_dir.name),
            attempt_id=attempt_state["attempt_id"],
            mode="execute" if attempt_state.get("execute") else "dry_run",
            started_at=attempt_state.get("started_at", finished_at),
            finished_at=finished_at,
            terminal_event_type=terminal_event_type,
            error=error,
            error_type=error_type,
        )
        validate_attempt_event_lifecycle(
            repaired.events,
            expected_run_id=attempt_state.get("run_name", output_dir.name),
            expected_attempt_id=attempt_state["attempt_id"],
            expected_terminal_event_type=terminal_event_type,
        )
        _write_jsonl(events_path, repaired.events)
    except BaseException:
        events_path.unlink(missing_ok=True)
        return

    summary_path = output_dir / "summary.json"
    summary = _read_json(summary_path, default={})
    if not isinstance(summary, dict):
        return
    summary.update({
        "error": error,
        "error_type": error_type,
        "finished_at": finished_at,
        "terminal_event_type": terminal_event_type,
        "events_available": True,
        "event_count": len(repaired.events),
        "event_warnings": list(repaired.warnings),
        "event_artifact_error": None,
        "event_artifact_error_detail": None,
        "event_lifecycle_valid": True,
        "event_terminal_count": 1,
    })
    _write_json(summary_path, summary)


def _task_graph_from_config(config: dict) -> tuple[list[Task], Graph, RuntimeTaskDAGStore]:
    task_configs = config.get("smoke_tasks")
    if isinstance(task_configs, list) and task_configs:
        tasks = [_task_from_config(config, task_config) for task_config in task_configs]
    else:
        tasks = [_task_from_config(config, {
            "description": config.get("task_goal", config.get("task_name", "Minecraft task")),
        })]
    task_store = RuntimeTaskDAGStore()
    task_store.load_tasks_from_decomposition(tasks)
    graph = task_store.to_task_graph_projection()
    return tasks, graph, task_store


def _runtime_task_dag_snapshot(*, runtime_result: dict, fallback_snapshot: dict, execute: bool) -> dict:
    if execute and isinstance(runtime_result.get("runtime_task_dag_snapshot"), dict) and runtime_result["runtime_task_dag_snapshot"]:
        snapshot = dict(runtime_result["runtime_task_dag_snapshot"])
        snapshot["snapshot_source"] = "real_runtime"
        return snapshot
    snapshot = dict(fallback_snapshot)
    snapshot["snapshot_source"] = "config_fixture"
    return snapshot


def task_graph_from_runtime_task_dag_snapshot(snapshot: dict) -> tuple[list[Task], Graph]:
    tasks = []
    task_by_node_id = {}
    graph = Graph()
    for node in snapshot.get("nodes", []) or []:
        if not isinstance(node, dict) or node.get("node_type") != "runtime_task":
            continue
        node_id = str(node.get("node_id", ""))
        if not node_id:
            continue
        content = node.get("content", {}) if isinstance(node.get("content"), dict) else {}
        lifecycle = node.get("lifecycle", {}) if isinstance(node.get("lifecycle"), dict) else {}
        derived = node.get("derived", {}) if isinstance(node.get("derived"), dict) else {}
        metadata = deepcopy(content.get("metadata", {}))
        if not isinstance(metadata, dict):
            metadata = {"value": metadata}
        metadata["runtime_snapshot"] = {
            "last_assigned_agents": deepcopy(lifecycle.get("last_assigned_agents", []) or []),
            "provenance": deepcopy(node.get("provenance", {})),
        }
        task = Task(str(content.get("description", "")), metadata)
        task.id = node_id.removeprefix("runtime:task:")
        task.milestones = deepcopy(content.get("milestones", []) or [])
        task.reflect = deepcopy(content.get("reflect"))
        task.status = str(lifecycle.get("status", Task.unknown))
        task.candidate_list = list(lifecycle.get("candidate_agents", []) or [])
        task._candidate_agents_explicit = bool(lifecycle.get("candidate_agents_explicit", False))
        task._candidate_agent_count_exact = bool(lifecycle.get("candidate_agent_count_exact", False))
        task._agent = list(lifecycle.get("active_agents", []) or [])
        task.number = lifecycle.get("required_agent_count")
        task.available = bool(derived.get("dependency_ready", False)) and task.status == Task.unknown
        tasks.append(task)
        task_by_node_id[node_id] = task
        graph.add_node(task)

    for edge in snapshot.get("edges", []) or []:
        if not isinstance(edge, dict) or edge.get("edge_type") != "precedes_task":
            continue
        source_id = str(edge.get("source_id", ""))
        target_id = str(edge.get("target_id", ""))
        if source_id not in task_by_node_id or target_id not in task_by_node_id:
            raise ValueError(f"Runtime task snapshot edge references unknown task: {source_id} -> {target_id}")
        graph.add_edge(task_by_node_id[source_id], task_by_node_id[target_id])

    for task in tasks:
        task._direct_pre_task_list = list(graph.get_node_to(task))
        task.predecessor_task_list = _graph_predecessors(graph, task)
    return tasks, graph


def _task_from_config(config: dict, task_config: dict) -> Task:
    task = Task(task_config.get("description", "Minecraft task"), {
        "task_name": config.get("task_name", ""),
        "task_type": config.get("task_type", ""),
        "task_idx": config.get("task_idx"),
        "smoke_task_id": task_config.get("id", ""),
    })
    if task_config.get("id"):
        task.id = str(task_config["id"])
    agent_num = int(config.get("agent_num", 0) or 0)
    if "candidate_agents" in task_config:
        task.candidate_list = list(task_config["candidate_agents"])
        task._candidate_agents_explicit = True
    elif "assigned_agents" in task_config:
        task.candidate_list = list(task_config["assigned_agents"])
        task._candidate_agents_explicit = True
    else:
        task.candidate_list = _agent_names(agent_num)
    task._agent = task_config.get("assigned_agents", [])
    task.number = task_config.get("number", max(1, min(agent_num, 1)))
    dependency_key = next(
        (key for key in ("required_subtasks", "required subtasks") if key in task_config),
        None,
    )
    task._pre_idxs = list(task_config.get(dependency_key, [])) if dependency_key else []
    task._pre_idxs_explicit = dependency_key is not None
    return task


def _task_graph_snapshot(graph: Graph) -> dict:
    return {
        "artifact_generation_mutates_runtime": False,
        "mutates_runtime": False,
        "tasks": [sanitize_public_value(task.to_json()) for task in graph.vertex],
        "edges": [
            {"source": start.description, "target": end.description}
            for start, end in graph.edge
        ],
    }


def _task_selection_policy(value: str | None, *, enable_dual_dag_task_selection: bool = True) -> str:
    if value is None:
        return "dual-dag" if enable_dual_dag_task_selection else "original"
    if value not in TASK_SELECTION_POLICIES:
        raise ValueError(f"Unsupported task_selection_policy: {value}")
    return value


def _policy_from_args(args: argparse.Namespace) -> str:
    if args.no_dual_dag_task_selection:
        return "original"
    if args.dual_dag_task_selection:
        return "dual-dag"
    return args.task_selection_policy


def _dual_dag_config(task_selection_policy: str) -> dict:
    scheduling_policy = "dual-dag" if task_selection_policy.startswith("dual_dag_") else task_selection_policy
    config = {
        "runtime_task_selection": {
            "enabled": scheduling_policy == "dual-dag",
            "policy": scheduling_policy,
        }
    }
    if task_selection_policy in {"dual_dag_advisory", "dual_dag_authority"}:
        config["eac_mode"] = task_selection_policy
    return config


def _fixture_action_log(config: dict) -> dict:
    action_log = config.get("smoke_action_log", {})
    return action_log if isinstance(action_log, dict) else {}


def _task_order(tasks: list[Task]) -> list[dict]:
    return [
        {"task_id": _task_id(task), "description": task.description}
        for task in tasks
    ]


def _runtime_selected_task_ids(runtime_result: dict) -> list[str]:
    selected = runtime_result.get("runtime_selected_task_ids", [])
    if not isinstance(selected, list):
        return []
    return [str(task_id) for task_id in selected if task_id is not None]


def _find_task(tasks: list[Task], task_id: str) -> Task | None:
    for task in tasks:
        if task_id in {_task_id(task), str(task.id), f"runtime:task:{task.id}"}:
            return task
    return None


def _graph_predecessors(graph: Graph, task: Task) -> list[Task]:
    predecessors = []
    pending = list(graph.get_node_to(task))
    while pending:
        predecessor = pending.pop(0)
        if predecessor in predecessors:
            continue
        predecessors.append(predecessor)
        pending.extend(graph.get_node_to(predecessor))
    return predecessors


def _task_id(task: Task) -> str:
    return f"minecraft:task:{task.id}"


def _agent_names(agent_num: int) -> list[str]:
    names = ["Alice", "Bob", "Cindy", "David", "Eve", "Frank"]
    return names[:agent_num]


def _progress_from_score(score: dict):
    if not isinstance(score, dict):
        return None
    for key in ("progress", "score", "completion", "success_rate"):
        if key in score:
            return score[key]
    return None


def _default_run_name(config_path: str | Path) -> str:
    return f"minecraft_experiment_{Path(config_path).stem}"


def _command_text(args: argparse.Namespace | None = None) -> str:
    if args is None:
        return "python -m benchmarks.minecraft.experiment"
    parts = ["python -m benchmarks.minecraft.experiment", "--config", args.config]
    if args.output_root != str(DEFAULT_OUTPUT_ROOT):
        parts.extend(["--output-root", args.output_root])
    if args.run_name:
        parts.extend(["--run-name", args.run_name])
    if args.config_index:
        parts.extend(["--config-index", str(args.config_index)])
    if args.task_selection_policy != "dual-dag":
        parts.extend(["--task-selection-policy", args.task_selection_policy])
    if args.dual_dag_task_selection:
        parts.append("--dual-dag-task-selection")
    if args.no_dual_dag_task_selection:
        parts.append("--no-dual-dag-task-selection")
    if args.execute:
        parts.append("--execute")
    if args.execute_timeout_seconds is not None:
        parts.extend(["--execute-timeout-seconds", str(args.execute_timeout_seconds)])
    if args.retain_runtime_result:
        parts.append("--retain-runtime-result")
    if getattr(args, "overwrite", False):
        parts.append("--overwrite")
    return " ".join(parts)


def _read_json(path: Path, *, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _read_agent_history(runtime_root: Path, runtime_task_name: str) -> list | None:
    if not runtime_task_name:
        return None
    path = runtime_root / "result" / runtime_task_name / "Alice_history.json"
    if not path.exists():
        return None
    try:
        history = _read_json(path, default=[])
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return history if isinstance(history, list) else None


def _remove_runtime_result(path: Path) -> None:
    for candidate in (path, path.with_suffix(path.suffix + ".tmp")):
        if candidate.exists():
            candidate.unlink()
    runtime_dir = path.parent
    if runtime_dir.exists() and not any(runtime_dir.iterdir()):
        runtime_dir.rmdir()


def _write_json(path: Path, payload) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def _write_jsonl(path: Path, rows) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
