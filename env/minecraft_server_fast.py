# Description: This is the fastapi server for the minecraft agent.
# This file still need to be tested, and it is not finished yet.

import argparse
import asyncio
import sys
import time
from uuid import uuid4
from math import floor
import names
from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from fastapi import FastAPI, HTTPException
from functools import wraps
from env_api import *
import uvicorn
import platform
from pathlib import Path

try:
    from env.runtime_paths import RuntimePaths, read_json_artifact
    from env.minecraft_bridge_diagnostics import (
        BoundedDiagnosticRecorder,
        CORRELATION_HEADER,
        install_fastapi_request_diagnostics,
        safe_error_class,
        safe_identifier,
        valid_correlation_id,
    )
    from env.movement_runtime import (
        AUTHORITATIVE_MOVEMENT_DEADLINE_SECONDS,
        MOVEMENT_REQUEST_DEADLINE_SECONDS,
        MovementCoordinator,
        MovementAdmissionClosedError,
        MovementEffectUnknownError,
        MovementOverlapError,
        MovementRequestDeadlineError,
        movement_lease_deadline,
        reconcile_mineflayer_state,
        run_with_movement_request_budget,
    )
    from env.movement_http_contract import movement_effect_unknown_response
    from benchmarks.minecraft.position_contract import resolve_position_convention
    from env.world_initialization import PRESERVE_RESTORED_SNAPSHOT
except ImportError:
    from runtime_paths import RuntimePaths, read_json_artifact
    from minecraft_bridge_diagnostics import (
        BoundedDiagnosticRecorder,
        CORRELATION_HEADER,
        install_fastapi_request_diagnostics,
        safe_error_class,
        safe_identifier,
        valid_correlation_id,
    )
    from movement_runtime import (
        AUTHORITATIVE_MOVEMENT_DEADLINE_SECONDS,
        MOVEMENT_REQUEST_DEADLINE_SECONDS,
        MovementCoordinator,
        MovementAdmissionClosedError,
        MovementEffectUnknownError,
        MovementOverlapError,
        MovementRequestDeadlineError,
        movement_lease_deadline,
        reconcile_mineflayer_state,
        run_with_movement_request_budget,
    )
    from movement_http_contract import movement_effect_unknown_response
    from benchmarks.minecraft.position_contract import resolve_position_convention
    from world_initialization import PRESERVE_RESTORED_SNAPSHOT

# sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf8')
os.environ["REQ_TIMEOUT"] = "1800000"
app = FastAPI()
runtime_paths = RuntimePaths.from_environment()
msg_list = []  # 用于存储消息队列，每次获取后清除当前的消息队列
server_event_loop = None
bridge_accepting_movement = True
item_drop_tasks = set()


def configured_position_convention():
    result = read_json_artifact(runtime_paths.meta_setting)
    config = result.value if result.state == "valid" and isinstance(result.value, dict) else {}
    convention = resolve_position_convention(
        config.get("position_convention"),
        required=config.get("world_initialization") == PRESERVE_RESTORED_SNAPSHOT,
    )
    return convention.value if convention is not None else None
# python minecraft_server_fast.py -U Tom
parser = argparse.ArgumentParser()
parser.add_argument('-P', '--port', type=int, default=25565)
parser.add_argument('-H', '--host', type=str, default='10.21.31.18')
parser.add_argument('-U', '--username', type=str, default=names.get_full_name().replace(' ', '_'))
parser.add_argument('-W', '--worldname', type=str)
parser.add_argument('-LP', '--local_port', type=int, default=5000)
parser.add_argument('-D', '--debug', type=bool, default=False)
args = parser.parse_args()
local_port = args.local_port
bridge_diagnostics = BoundedDiagnosticRecorder(
    runtime_paths.minecraft_bridge_actor_diagnostics(args.username),
    producer="bridge", actor=args.username,
)
install_fastapi_request_diagnostics(app, bridge_diagnostics, actor=args.username)
bridge_diagnostics.record(
    "listener_starting", actor=args.username,
    endpoint_identity=f"actor:{args.username}", expected_local_port=local_port,
)


def record_listener_failure(error):
    bridge_diagnostics.record_once(
        "listener_failed", actor=args.username,
        endpoint_identity=f"actor:{args.username}", expected_local_port=local_port,
        error_class=safe_error_class(error),
    )
    bridge_diagnostics.flush()


_original_excepthook = sys.excepthook


def diagnostic_excepthook(error_type, error, traceback):
    record_listener_failure(error)
    _original_excepthook(error_type, error, traceback)


sys.excepthook = diagnostic_excepthook
print(f"Agent {args.username} login {args.worldname} at {args.host}:{args.port}")
# VIEW_PORT = 3000
mineflayer = require('mineflayer')
pathfinder = require('mineflayer-pathfinder')
collectBlock = require('mineflayer-collectblock')
pvp = require("mineflayer-pvp").plugin
Vec3 = require("vec3")
# viewer = require('prismarine-viewer').mineflayer
Socks = require("socks5-client")
minecraftData = require('minecraft-data')
mcData = minecraftData('1.19.2')
# Match the non-fast server's module shape.
if platform.system().lower() == 'linux':
    minecraftHawkEye = require("minecrafthawkeye").default
else:
    minecraftHawkEye = require("minecrafthawkeye")
# print(mcData.itemsByName['yellow_carpet'])
try:
    bot = mineflayer.createBot({
        "host": args.host,
        "port": args.port,
        'username': args.username.replace(' ', '_'),
        'checkTimeoutInterval': 600000,
        'auth': 'offline',
        'version': '1.19.2',
    })
except BaseException as error:
    bridge_diagnostics.record(
        "mineflayer_connection_error", actor=args.username,
        endpoint_identity=f"actor:{args.username}", connection_state="connection_error",
        error_class=safe_error_class(error),
    )
    bridge_diagnostics.flush()
    raise
bridge_diagnostics.record(
    "mineflayer_bot_created", actor=args.username,
    endpoint_identity=f"actor:{args.username}", connection_state="created",
)


@On(bot, "login")
def diagnostic_login(*unused):
    bridge_diagnostics.record(
        "mineflayer_connected", actor=args.username,
        endpoint_identity=f"actor:{args.username}", connection_state="connected",
    )


@On(bot, "spawn")
def diagnostic_spawn(*unused):
    bridge_diagnostics.record(
        "mineflayer_ready", actor=args.username,
        endpoint_identity=f"actor:{args.username}", connection_state="ready",
    )


@On(bot, "end")
def diagnostic_end(*unused):
    bridge_diagnostics.record(
        "mineflayer_disconnected", actor=args.username,
        endpoint_identity=f"actor:{args.username}", connection_state="disconnected",
    )


@On(bot, "kicked")
def diagnostic_kicked(*unused):
    bridge_diagnostics.record(
        "mineflayer_disconnected", actor=args.username,
        endpoint_identity=f"actor:{args.username}", connection_state="kicked",
    )


@On(bot, "error")
def diagnostic_error(unused_this=None, error=None, *unused):
    bridge_diagnostics.record(
        "mineflayer_connection_error", actor=args.username,
        endpoint_identity=f"actor:{args.username}", connection_state="connection_error",
        error_class=safe_error_class(error) if error is not None else "unknown",
    )


time.sleep(3)
bot.loadPlugin(pathfinder.pathfinder)
bot.loadPlugin(collectBlock.plugin)
bot.loadPlugin(pvp)
bot.loadPlugin(minecraftHawkEye)
movement_coordinator = MovementCoordinator(
    bot, pathfinder, deadline_seconds=AUTHORITATIVE_MOVEMENT_DEADLINE_SECONDS,
)


@app.exception_handler(MovementOverlapError)
async def movement_overlap_response(_request, _error):
    return JSONResponse(
        {"message": "movement already active", "status": False,
         "reason": "movement_in_progress"},
        status_code=409,
    )


@app.exception_handler(CooperativeMovementError)
async def cooperative_movement_error_response(_request, error):
    return JSONResponse(
        {"message": str(error), "status": False, "reason": error.reason},
        status_code=error.status_code,
    )


app.add_exception_handler(MovementEffectUnknownError, movement_effect_unknown_response)


def reconcile_current_mineflayer_state():
    state = reconcile_mineflayer_state(bot)
    if state["connected"]:
        bridge_diagnostics.record(
            "mineflayer_connected", actor=args.username,
            endpoint_identity=f"actor:{args.username}", connection_state="connected",
            result=state["source"],
        )
    if state["ready"]:
        bridge_diagnostics.record(
            "mineflayer_ready", actor=args.username,
            endpoint_identity=f"actor:{args.username}", connection_state="ready",
            result=state["source"],
        )
    return state


@app.on_event("startup")
async def diagnostic_startup():
    global bridge_accepting_movement
    bridge_accepting_movement = True
    reconcile_current_mineflayer_state()
    bridge_diagnostics.record(
        "listener_startup_completed", actor=args.username,
        endpoint_identity=f"actor:{args.username}", expected_local_port=local_port,
    )


async def _cancel_movement_and_close_admission(reason, *, timeout_seconds=1.0):
    global bridge_accepting_movement
    bridge_accepting_movement = False
    cancellation = await movement_coordinator.cancel_active(
        timeout_seconds=timeout_seconds, reason=reason,
    )
    if item_drop_tasks:
        try:
            await asyncio.wait_for(
                asyncio.gather(*tuple(item_drop_tasks), return_exceptions=True),
                timeout=0.2,
            )
        except asyncio.TimeoutError:
            pending = tuple(item_drop_tasks)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    return cancellation


@app.on_event("shutdown")
async def diagnostic_shutdown():
    global bridge_accepting_movement, server_event_loop
    cancellation = await _cancel_movement_and_close_admission(
        "bridge_shutdown", timeout_seconds=1.0,
    )
    if cancellation["cancel_requested"]:
        bridge_diagnostics.record(
            "movement_cancel_requested", actor=args.username,
            endpoint_identity=f"actor:{args.username}",
            cancel_requested=True, cancellation_reason="bridge_shutdown",
            result="terminal" if cancellation["terminal"] else "cancel_pending",
        )
    server_event_loop = None
    bridge_diagnostics.record(
        "listener_shutdown", actor=args.username,
        endpoint_identity=f"actor:{args.username}", expected_local_port=local_port,
    )
    bridge_diagnostics.flush()


def timeout(seconds: float):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await asyncio.wait_for(func(*args, **kwargs), timeout=seconds)
            except asyncio.TimeoutError:
                raise HTTPException(status_code=408, detail="Request timed out")
        return wrapper
    return decorator


def movement_request_budget(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        global bridge_accepting_movement
        try:
            return await run_with_movement_request_budget(
                func(*args, **kwargs),
                timeout_seconds=MOVEMENT_REQUEST_DEADLINE_SECONDS,
            )
        except MovementRequestDeadlineError:
            terminal = not movement_coordinator.admission_closed and not movement_coordinator.active
            reason = (
                "cleanup_timeout" if movement_coordinator.admission_closed
                else "movement_request_deadline"
            )
            if movement_coordinator.admission_closed:
                bridge_accepting_movement = False
            raise MovementEffectUnknownError(
                "movement request outcome is unknown",
                reason=reason, status_code=504, terminal=terminal,
            )
    return wrapper


async def _bounded_control_json(request: Request, limit=1024):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status_code=413, detail="Request body too large")
    try:
        payload = json.loads(body or b"{}")
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON object required")
    return payload


@app.get('/post_ping')
@app.post('/post_ping')
async def ping():
    return JSONResponse({'message': 'pong', 'status': True})


@app.post("/post_render")
@timeout(10)
async def render_structure(request: Request):
    """render_structure: render the structure."""
    data = await request.json()
    id = data.get('id')
    center_pos = data.get('center_pos')
    try:
        blueprint_path = Path(__file__).resolve().parents[1] / "data" / "building_blue_print.json"
        with blueprint_path.open("r", encoding="utf-8") as f:
            structure_list = json.load(f)
        structure = structure_list[id]
        for b in structure["blocks"]:
            time.sleep(.05)  
            x, y, z = b["position"][0] + center_pos[0], b["position"][1] + center_pos[1], b["position"][2] + center_pos[2]
            if b["facing"] in ["W", "E", "S", "N"]:
                cvt = {"W": "west", "E": "east", "S": "south", "N": "north"}
                bot.chat(f'/setblock {x} {y} {z} {b["name"]}[facing={cvt[b["facing"]]}]')
            elif b["facing"] in ["x", "y", "z"]:
                bot.chat(f'/setblock {x} {y} {z} {b["name"]}[axis={b["facing"]}]')
            elif b["facing"] == "A":
                bot.chat(f'/setblock {x} {y} {z} {b["name"]}')

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
    return {"message": "render success", "status": True}

@app.post('/post_msg')  # 获取前端发来的消息
@timeout(10)
async def get_msg(request: Request):
    """get_msg: get the message from the message queue."""
    global msg_list
    msg = msg_list
    msg_list = []
    return JSONResponse({'message': msg, 'status': True})


@app.post('/post_time')  # 获取前端的时间
async def get_time(request: Request):
    return JSONResponse({'time': str(bot.time.timeOfDay)})


@app.post('/post_find')
@timeout(10)
async def find(request: Request):
    """find name distance count: find tag in the distance, and count is the number of items you want to find."""
    data = await request.json()
    name, distance, count = data.get('name'), data.get('distance'), data.get('count')
    name = name_check(bot, Vec3, mcData, name)
    bot.chat(f"name_check {name}")
    if name == "":
        return JSONResponse({'message': "can not find anything match", 'status': False, 'data':[]})
    observation = ""
    name, pos_list_raw = find_everything_(
        bot=bot,
        Vec3=Vec3,
        envs_info=get_envs_info(bot, 128),
        mcData=mcData,
        name=name,
        distance=distance,
        count=count,
    )
    # remove duplicate
    pos_list = []
    for pos in pos_list_raw:
        for pos2 in pos_list:
            if floor(pos.x + .5) == floor(pos2.x + .5) and floor(pos.y + .5) == floor(pos2.y + .5) and floor(pos.z + .5) == floor(
                    pos2.z + .5):
                break
        else:
            pos_list.append(pos)

    pos_data = []
    if len(pos_list) > 0:
        str_pos_list = f'I found {name} '
        # if pos_list is dict:
        if type(pos_list) == dict:
            for pos in pos_list:
                str_pos_list += f'at {pos},'
                pos_data.append({"x": floor(pos["x"] + .5), "y": floor(pos["y"] + .5), "z": floor(pos["z"] + .5)})
        else:
            for pos in pos_list:
                str_pos_list += f'at {floor(pos.x + .5)} {floor(pos.y + .5)} {floor(pos.z + .5)},'
                pos_data.append({"x": floor(pos.x + .5), "y": floor(pos.y + .5), "z": floor(pos.z + .5)})
        observation += str_pos_list
        done = True
        return JSONResponse({'message': observation, 'status': done, 'data':pos_data,
                             'observed_name': name})
    else:
        observation += f"can not find {name}, there is no {name} around."
        done = False
        return JSONResponse({'message': observation, 'status': done, 'data':[]})
    
@app.post('/post_hand')
@movement_request_budget
async def hand(request: Request):
    """hand item to entity_name: hand item to entity_name."""
    data = await request.json()
    entity_name, item_name, count = data.get('target_name'), data.get('item_name'), data.get('item_count')
    envs_info = get_envs_info(bot, 128)
    target = find_nearest_(bot, Vec3, envs_info, mcData, entity_name)
    if target is None:
        return JSONResponse({'message': f"can not find anything named {entity_name} nearby", 'status': False})
    movement = await _movement_runner(request, "hand_to_entity")(target, 1)
    if not movement.success:
        return JSONResponse({'message': movement.message, 'status': False})
    
    # toss item
    msg, tag = toss(bot, mcData, item_name, count)
    return JSONResponse({'message': msg, 'status': tag})

def _movement_correlation_id(request: Request | None) -> str:
    supplied = request.headers.get(CORRELATION_HEADER) if request is not None else None
    return supplied if valid_correlation_id(supplied) else uuid4().hex


def _movement_target_identity(target) -> str:
    return safe_identifier(f"block:{target.x}:{target.y}:{target.z}")


def _movement_fields(result) -> dict:
    metadata = result.metadata
    fields = {
        "movement_id": metadata["movement_id"],
        "operation": metadata["operation"],
        "target_identity": metadata["target_identity"],
        "terminal_reason": result.reason,
        "configured_movement_deadline_s": metadata["deadline"],
        "initial_distance": metadata["initial_distance"],
        "final_distance": metadata.get("final_distance"),
        "movement_elapsed_s": metadata["elapsed"],
        "goal_clear_attempted": metadata["goal_clear_attempted"],
        "goal_clear_succeeded": metadata["goal_clear_succeeded"],
        "cleanup_completed": metadata["cleanup_completed"],
        "error_class": metadata.get("error_class"),
    }
    if metadata.get("cancellation_reason"):
        fields["cancellation_reason"] = metadata["cancellation_reason"]
    return fields


async def _execute_movement(request: Request | None, target, *, operation: str,
                            tolerance: float, completion_policy=EUCLIDEAN_DISTANCE):
    global bridge_accepting_movement
    if movement_coordinator.admission_closed:
        bridge_accepting_movement = False
        raise MovementEffectUnknownError(
            "movement cleanup remains unconfirmed",
            reason="cleanup_timeout", status_code=503, terminal=False,
        )
    if not bridge_accepting_movement:
        return None, JSONResponse(
            {"message": "bridge is shutting down", "status": False,
             "reason": "bridge_shutting_down"},
            status_code=503,
        )
    correlation_id = _movement_correlation_id(request)
    movement_id = uuid4().hex
    target_identity = _movement_target_identity(target)
    if movement_coordinator.active:
        completed = time.monotonic_ns()
        bridge_diagnostics.record(
            "movement_overlap_rejected", correlation_id=correlation_id,
            actor=args.username, endpoint_identity=f"actor:{args.username}",
            movement_id=movement_id, operation=operation, target_identity=target_identity,
            movement_overlap_rejected=True, terminal_reason="overlap_rejected",
            completed_monotonic_ns=completed,
        )
        return None, JSONResponse(
            {"message": "movement already active", "status": False,
             "reason": "movement_in_progress"},
            status_code=409,
        )
    started = time.monotonic_ns()
    bridge_diagnostics.record(
        "movement_started", correlation_id=correlation_id, actor=args.username,
        endpoint_identity=f"actor:{args.username}", movement_id=movement_id,
        operation=operation, target_identity=target_identity, movement_started=True,
        started_monotonic_ns=started,
        configured_movement_deadline_s=AUTHORITATIVE_MOVEMENT_DEADLINE_SECONDS,
    )
    lease_deadline = movement_lease_deadline(
        AUTHORITATIVE_MOVEMENT_DEADLINE_SECONDS,
    )
    if lease_deadline <= 0:
        raise MovementRequestDeadlineError(
            "movement request budget exhausted before lease admission"
        )
    try:
        result = await movement_coordinator.move(
            target,
            tolerance=tolerance,
            completion_policy=completion_policy,
            position_convention=(
                configured_position_convention()
                if completion_policy == STRICT_PER_AXIS else None
            ),
            correlation_id=correlation_id,
            movement_id=movement_id,
            target_identity=target_identity,
            operation=operation,
            deadline_seconds=lease_deadline,
        )
    except asyncio.CancelledError:
        cancelled_result = movement_coordinator.last_result
        if (cancelled_result is not None
                and cancelled_result.metadata.get("movement_id") == movement_id):
            terminal = bool(cancelled_result.metadata.get("cleanup_completed"))
            bridge_diagnostics.record(
                "movement_terminal" if terminal else "movement_nonterminal",
                correlation_id=correlation_id,
                actor=args.username, endpoint_identity=f"actor:{args.username}",
                completed_monotonic_ns=time.monotonic_ns(), movement_terminal=terminal,
                movement_nonterminal=not terminal,
                movement_completed=False,
                movement_deadline=cancelled_result.reason == "deadline",
                movement_cancelled=cancelled_result.reason == "cancelled",
                movement_failed=cancelled_result.reason not in {
                    "deadline", "cancelled",
                },
                **_movement_fields(cancelled_result),
            )
            if not terminal:
                bridge_accepting_movement = False
        raise
    except MovementOverlapError:
        completed = time.monotonic_ns()
        bridge_diagnostics.record(
            "movement_overlap_rejected", correlation_id=correlation_id,
            actor=args.username, endpoint_identity=f"actor:{args.username}",
            movement_id=movement_id, operation=operation, target_identity=target_identity,
            movement_overlap_rejected=True, terminal_reason="overlap_rejected",
            completed_monotonic_ns=completed,
        )
        return None, JSONResponse(
            {"message": "movement already active", "status": False,
             "reason": "movement_in_progress"},
            status_code=409,
        )
    except MovementAdmissionClosedError:
        bridge_accepting_movement = False
        raise MovementEffectUnknownError(
            "movement cleanup remains unconfirmed",
            reason="cleanup_timeout", status_code=503, terminal=False,
        )
    completed = time.monotonic_ns()
    terminal = bool(result.metadata.get("cleanup_completed"))
    bridge_diagnostics.record(
        "movement_terminal" if terminal else "movement_nonterminal",
        correlation_id=correlation_id, actor=args.username,
        endpoint_identity=f"actor:{args.username}", completed_monotonic_ns=completed,
        movement_terminal=terminal, movement_nonterminal=not terminal,
        movement_completed=terminal and result.reason == "reached",
        movement_deadline=result.reason == "deadline",
        movement_cancelled=result.reason == "cancelled",
        movement_failed=result.reason not in {"reached", "deadline", "cancelled"},
        **_movement_fields(result),
    )
    if not terminal:
        bridge_accepting_movement = False
        raise MovementEffectUnknownError(
            result.message, reason=result.reason, status_code=503, terminal=False,
        )
    status_code = {
        "deadline": 408,
        "cancelled": 409,
        "pathfinder_error": 500,
    }.get(result.reason, 200)
    return result, status_code


def _movement_runner(request: Request, operation: str):
    async def run(target, tolerance):
        result, _status = await _execute_movement(
            request, target, operation=operation, tolerance=tolerance,
        )
        if result is None:
            status_code = getattr(_status, "status_code", 409)
            raise CooperativeMovementError(
                "movement already active" if status_code == 409 else "bridge is shutting down",
                reason="movement_in_progress" if status_code == 409 else "bridge_shutting_down",
                status_code=status_code,
            )
        if not result.success:
            raise CooperativeMovementError(
                result.message, reason=result.reason, status_code=_status,
            )
        return result

    return run


@app.post('/post_move_to')
@movement_request_budget
async def move_to_(request: Request):
    """move_to name: move to the entity by name or postion x y z."""
    data = await request.json()
    name = data.get('name')
    envs_info = get_envs_info(bot, 128)
    target = find_nearest_(bot, Vec3, envs_info, mcData, name)
    if target is None:
        return JSONResponse({
            'message': f"can not find anything named {name} nearby", 'status': False,
        })
    result, status = await _execute_movement(
        request, target, operation="move_to_nearest", tolerance=1,
    )
    if result is None:
        return status
    return JSONResponse(
        {'message': result.message, 'status': result.success, 'reason': result.reason},
        status_code=status,
    )


@app.post('/post_move_to_pos')
@movement_request_budget
async def move_to_pos(request: Request):
    """move_to_pos x y z: move to the position x y z."""
    data = await request.json()
    x, y, z = data.get('x'), data.get('y'), data.get('z')
    # Judged coordinate tasks require every axis to be within one block.
    target = Vec3(x, y, z)
    position_convention = configured_position_convention()
    result, status = await _execute_movement(
        request, target, operation="move_to_pos", tolerance=1,
        completion_policy=STRICT_PER_AXIS,
    )
    if result is None:
        return status
    completion = evaluate_movement_completion(
        bot.entity.position,
        target,
        1,
        policy=STRICT_PER_AXIS,
        position_convention=position_convention,
    )
    if position_convention is not None:
        completion["pathfinder_goal"] = {
            "type": "GoalNear",
            "target": {"x": float(x), "y": float(y), "z": float(z)},
            "position_convention": position_convention,
        }
    done = movement_status(result.success, completion)
    # lookAtPlayer(bot, entity['position'])
    return JSONResponse(
        {'message': result.message, 'status': done, 'reason': result.reason, **completion},
        status_code=status,
    )


@app.post('/post_cancel_movement')
async def cancel_movement(request: Request):
    data = await _bounded_control_json(request)
    requested_reason = data.get("reason")
    reason = requested_reason if requested_reason in {
        "controller_shutdown", "bridge_shutdown",
    } else "control_request"
    active = movement_coordinator.snapshot()
    if reason in {"controller_shutdown", "bridge_shutdown"}:
        cancellation = await _cancel_movement_and_close_admission(
            reason, timeout_seconds=1.0,
        )
    else:
        cancellation = await movement_coordinator.cancel_active(
            timeout_seconds=1.0, reason=reason,
        )
    if cancellation["cancel_requested"]:
        bridge_diagnostics.record(
            "movement_cancel_requested",
            correlation_id=active.get("correlation_id"), actor=args.username,
            endpoint_identity=f"actor:{args.username}",
            movement_id=active.get("movement_id"), operation=active.get("operation"),
            target_identity=active.get("target_identity"), cancel_requested=True,
            cancellation_reason=reason,
            result="terminal" if cancellation["terminal"] else "cancel_pending",
        )
    return JSONResponse({"status": cancellation["terminal"], **cancellation})


@app.post('/post_use_on')
@movement_request_budget
async def use_on(request: Request):
    """use_on item_name entity_name: For example, you can use shears on sheep, use bucket on cow."""
    data = await request.json()
    item_name, entity_name = data.get('item_name'), data.get('entity_name')
    envs_info = get_envs_info(bot, 128)
    target = find_nearest_(bot, Vec3, envs_info, mcData, entity_name)
    if target is None:
        return JSONResponse({'message': f"cannot find {entity_name} nearby", 'status': False})
    await _movement_runner(request, "use_on_target")(target, 3)
    blocks = BlocksNearby(bot, Vec3, mcData, RenderRange=32, max_same_block=32)
    msg, tag = useOnNearest(
        bot, Vec3, pathfinder, envs_info, mcData, blocks, item_name, entity_name,
        allow_movement=False,
    )
    done = tag
    return JSONResponse({'message': msg, 'status': done})


@app.post('/post_sleep')
@movement_request_budget
async def sleep_(request: Request):
    """sleep: to sleep."""
    beds = BlocksSearch(bot, Vec3, mcData, 16, 'bed', count=1)
    if not beds:
        return JSONResponse({'message': "failed to sleep because no bed found", 'status': False})
    bed = beds[0]
    await _movement_runner(request, "sleep_at_bed")(bed['position'], 2)
    if not bot.isABed(bed):
        return JSONResponse({'message': "failed to sleep because no bed found", 'status': False})
    bot.sleep(bed)
    return JSONResponse({'message': "Sleep!", 'status': True})


@app.post('/post_wake')
@timeout(10)
async def wake_():
    """wake: to wake."""
    msg = wake(bot)
    done = True
    return JSONResponse({'message': msg, 'status': done})


@app.post('/post_dig')
@movement_request_budget
async def dig(request: Request):
    """dig x y z: dig block at x y z."""
    data = await request.json()
    x, y, z = data.get('x'), data.get('y'), data.get('z')
    msg, tag = await dig_at_cooperative(
        bot, pathfinder, Vec3, (x, y, z),
        movement_runner=_movement_runner(request, "dig_to_block"),
    )
    return JSONResponse({'message': msg, 'status': tag})


@app.post('/post_place')
@movement_request_budget
async def place(request: Request):
    """place item_name x y z facing: place item at x y z, facing is one of [W, E, S, N, x, y, z]."""
    data = await request.json()
    item_name, x, y, z, facing = data.get('item_name'), data.get('x'), data.get('y'), data.get('z'), data.get('facing')
    if facing.lower() == 'default':
        facing = 'A'
    if facing.lower() == 'up' or facing.lower() == 'down':
        facing = 'y'
    if facing.lower() == 'north' or facing.lower() == 'south':
        facing = 'z'
    if facing.lower() == 'west' or facing.lower() == 'east':
        facing = 'x'
    if facing not in ['x', 'y', 'z', "W", "E", "S", "N", "A"]:
        return JSONResponse({'message': "facing is one of [W, E, S, N, x, y, z, A]", 'status': False})
    flag, msg = await place_axis(
        bot, mcData, pathfinder, Vec3, item_name, (x, y, z), facing,
        movement_runner=_movement_runner(request, "place_to_position"),
    )
    if not flag and item_name == 'ladder':
        return JSONResponse({'message': f"{msg}, there is no dirt block to support it.", 'status': False})
    return JSONResponse({'message': msg, 'status': flag})


@app.post('/post_attack')
@timeout(10)
async def attack_(request: Request):
    """attack name:  to attack the nearest entity."""
    data = await request.json()
    name = data.get('name')
    envs_info = get_envs_info(bot, 128)
    msg, tag = await attack(bot, envs_info, mcData, name)
    done = tag
    return JSONResponse({'message': msg, 'status': done})


@app.post('/post_equip')
@timeout(10)
async def equip_(request: Request):
    """equip slot item_name:  to equip item on hand,head,torso,legs,feet,off-hand."""
    data = await request.json() 
    slot, item_name = data.get('slot'), data.get('item_name')
    observation = ""
    value_data = []
    try:
        if not findInventoryItems(bot, item_name):
            observation += f"I don't have {item_name} in my inventory"
            return JSONResponse({'message': observation, 'status': False, 'data': []})
        else:
            msg, done = equip(bot, item_name, slot)
            observation += msg
            return JSONResponse({'message': observation, 'status': done, 'data': value_data})
    except (CooperativeMovementError, asyncio.CancelledError):
        raise
    except Exception:
        observation += "equip fail"
        done = False
        return JSONResponse({'message': observation, 'status': done, 'data': value_data})


@app.post('/post_toss')
@timeout(10)
async def toss_(request: Request):
    """toss item_name count:  to throw item out."""
    data = await request.json()
    item_name, count = data.get('item_name'), data.get('count', 1)
    msg, tag = toss(bot, mcData, item_name, count)
    return JSONResponse({'message': msg, 'status': tag})


@app.post('/post_environment')
@timeout(10)  # 获取环境信息
async def environment(request: Request):
    """environment:  to get the environment info."""
    msg = get_envs_info2str(bot, RENDER_DISTANCE=32, same_entity_num=3)
    blocks = BlocksNearby(bot, Vec3, mcData, RenderRange=16, max_same_block=3)
    hint = readNearestSign(bot, Vec3, mcData, max_distance=5)
    for block in blocks:
        for key in block.keys():
            if key != 'facing':
                msg += f"{key} at {block[key]}\n"
    if hint:
        msg += f"the sign nearby said: {hint}"
    
    cache_result = read_json_artifact(runtime_paths.env_cache)
    if cache_result.state == "valid" and isinstance(cache_result.value, list):
        cache = cache_result.value
        # 找到距离小于5的cache
        for c in cache:
            pos = c["center"]
            if (pos[0] - bot.entity.position.x) ** 2 + (pos[1] - bot.entity.position.y) ** 2 + (
                    pos[2] - bot.entity.position.z) ** 2 < 25:
                msg += f"the env in the room: {c['state']}"
    done = True
    return JSONResponse({'message': msg, 'status': done})

@app.post('/post_environment_dict')
@timeout(10)  # 获取环境信息
async def environment_info(request: Request):
    """environment:  to get the environment info."""
    msg = get_envs_info_dict(bot, RENDER_DISTANCE=32, same_entity_num=3)
    blocks = BlocksNearby(bot, Vec3, mcData, RenderRange=32, max_same_block=3)
    hint = readNearestSign(bot, Vec3, mcData, max_distance=5)
    msg["blocks"] = blocks
    msg["sign"] = str(hint)
    cache_result = read_json_artifact(runtime_paths.env_cache)
    if cache_result.state == "valid" and isinstance(cache_result.value, list):
        cache = cache_result.value
        # 找到距离小于5的cache
        for c in cache:
            pos = c["center"]
            if (pos[0] - bot.entity.position.x) ** 2 + (pos[1] - bot.entity.position.y) ** 2 + (
                    pos[2] - bot.entity.position.z) ** 2 < 25:
                msg["sign"] += f"The env in the room: {c['state']}"
    done = True
    return JSONResponse({'message': msg, 'status': done})


@app.post('/post_entity')
@timeout(10)
async def entity(request: Request):
    """entity distance name:  to get the entity info in range distance."""
    data = await request.json()
    name = data.get('name', "")
    info, num = get_agent_info2str(bot, RENDER_DISTANCE=32, idle=False, with_humans=False, name=name)
    return JSONResponse({'message': info, 'status': True, 'data': num})


@app.post('/post_get')
@movement_request_budget
async def get(request: Request):
    """get item_name count:  to get item from one chest, container, etc."""
    data = await request.json()
    item_name, from_name, item_count = data.get('item_name'), data.get('from_name'), data.get('item_count')
    envs_info = get_envs_info(bot, 128)
    tag, flag, data = await interact_nearest(
        pathfinder, bot, Vec3, envs_info, mcData, 3, from_name,
        get_item_name=item_name, count=-item_count,
        movement_runner=_movement_runner(request, "get_from_container"),
    )
    return JSONResponse({'message': tag, 'status': flag, 'data': data})


@app.post('/post_put')
@movement_request_budget
async def put(request: Request):
    """put item_name count:  to put item to one chest, container, etc."""
    data = await request.json()
    item_name, to_name, item_count = data.get('item_name'), data.get('to_name'), data.get('item_count')
    envs_info = get_envs_info(bot, 128)
    tag, flag, data = await interact_nearest(
        pathfinder, bot, Vec3, envs_info, mcData, 3, to_name,
        get_item_name=item_name, count=item_count,
        movement_runner=_movement_runner(request, "put_into_container"),
    )
    return JSONResponse({'message': tag, 'status': flag, 'data': data})


@app.post('/post_smelt')
@movement_request_budget
async def smelt(request: Request):
    """smelt item_name item_count material:  to smelt item in the furnace. fuel_item is one of [wood, coal, charcoal, lava_bucket, etc]."""
    data = await request.json()
    item_name, item_count, fuel_item_name = data.get('item_name'), data.get('item_count'), data.get('fuel_item_name')
    envs_info = get_envs_info(bot, 128)
    tag, flag, data = await interact_nearest(
        pathfinder, bot, Vec3, envs_info, mcData, 3, item_name,
        get_item_name=item_count, fuel_item_name=fuel_item_name,
        movement_runner=_movement_runner(request, "smelt_at_furnace"),
    )
    return JSONResponse({'message': tag, 'status': flag, 'data': data})


@app.post('/post_craft')
@movement_request_budget
async def craft(request: Request):
    """craft item_name count:  to craft item in the crafting_table."""
    data = await request.json()
    item_name, count = data.get('item_name'), data.get('count')
    envs_info = get_envs_info(bot, 128)
    tag, flag, data = await interact_nearest(
        pathfinder, bot, Vec3, envs_info, mcData, 3, 'crafting',
        get_item_name=item_name, count=count,
        movement_runner=_movement_runner(request, "craft_at_table"),
    )
    return JSONResponse({'message': tag, 'status': flag, 'data': data})


@app.post('/post_enchant')
@movement_request_budget
async def enchant(request: Request):
    """enchant item_name count:  to enchant item in the enchanting_table."""
    data = await request.json()
    item_name, count = data.get('item_name'), data.get('count')
    envs_info = get_envs_info(bot, 128)
    tag, flag, data = await interact_nearest(
        pathfinder, bot, Vec3, envs_info, mcData, 3, 'enchanting_table',
        get_item_name=item_name, count=count,
        movement_runner=_movement_runner(request, "enchant_at_table"),
    )
    return JSONResponse({'message': tag, 'status': flag, 'data': data})


@app.post('/post_trade')
@movement_request_budget
async def trade(request: Request):
    """trade item_name count:  to trade item with the entity."""
    data = await request.json()
    item_name, with_name, count = data.get('item_name'), data.get('with_name'), data.get('count')
    envs_info = get_envs_info(bot, 128)
    tag, flag, data = await interact_nearest(
        pathfinder, bot, Vec3, envs_info, mcData, 3, with_name,
        get_item_name=item_name, count=count,
        movement_runner=_movement_runner(request, "trade_with_entity"),
    )
    return JSONResponse({'message': tag, 'status': flag, 'data': data})


@app.post('/post_repair')
@movement_request_budget
async def repair(request: Request):
    """repair item_name material:  to repair item in the anvil. material is one of [wood, stone, iron, diamond, gold]."""
    data = await request.json()
    item_name, material = data.get('item_name'), data.get('material')
    envs_info = get_envs_info(bot, 128)
    tag, flag, data = await interact_nearest(
        pathfinder, bot, Vec3, envs_info, mcData, 3, 'anvil',
        repair_item_name=item_name, get_item_name=material,
        movement_runner=_movement_runner(request, "repair_at_anvil"),
    )
    return JSONResponse({'message': tag, 'status': flag, 'data': data})


@app.post('/post_eat')
@movement_request_budget
async def eat(request: Request):
    """eat item_name:  to eat item."""
    data = await request.json()
    item_name = data.get('item_name')
    envs_info = get_envs_info(bot, 128)
    tag, flag, data = await interact_nearest(
        pathfinder, bot, Vec3, envs_info, mcData, 3, item_name,
        movement_runner=_movement_runner(request, "eat_near_item"),
    )
    return JSONResponse({'message': tag, 'status': flag, 'data': data})


@app.post('/post_drink')
@movement_request_budget
async def drink(request: Request):
    """drink item_name count:  to drink item."""
    data = await request.json()
    item_name, count = data.get('item_name'), data.get('count')
    envs_info = get_envs_info(bot, 128)
    tag, flag, data = await interact_nearest(
        pathfinder, bot, Vec3, envs_info, mcData, 3, item_name,
        movement_runner=_movement_runner(request, "drink_near_item"),
    )
    return JSONResponse({'message': tag, 'status': flag, 'data': data})


@app.post('/post_wear')
@movement_request_budget
async def wear(request: Request):
    """wear slot item_name:  to wear item on head,torso,legs,feet,off-hand."""
    data = await request.json()
    slot, item_name = data.get('slot'), data.get('item_name')
    observation = ""
    value_data = []
    try:
        if not findInventoryItems(bot, item_name):
            envs_info = get_envs_info(bot, 128)
            msg, flag, value_data = await interact_nearest(
                pathfinder, bot, Vec3, envs_info, mcData, 3, 'chest',
                get_item_name=item_name,
                movement_runner=_movement_runner(request, "wear_from_container"),
            )
            observation += msg
        msg, done = equip(bot, item_name, slot)
        observation += msg
        return JSONResponse({'message': observation, 'status': done, 'data': value_data})
    except (CooperativeMovementError, asyncio.CancelledError):
        raise
    except Exception:
        observation += "equip fail"
        done = False
        return JSONResponse({'message': observation, 'status': done, 'data': value_data})
    
@app.post('/post_find_inventory')
@timeout(10)
async def find_inventory(request: Request):
    """find_inventory item_name:  to find if there is item in the inventory and return count."""
    data = await request.json()
    item_name = data.get('item_name')
    tag, count = findInventoryItems(bot, item_name)
    return JSONResponse({'message': "", 'status': tag, 'data': count})


@app.post('/post_open')
@movement_request_budget
async def open_(request: Request):
    """open item_name:  to open the door, gate, fence_gate, trapdoor, chest, etc, return the items names if open chest"""
    data = await request.json()
    item_name = data.get('item_name')
    envs_info = get_envs_info(bot, 128)
    tag, flag, data = await interact_nearest(
        pathfinder, bot, Vec3, envs_info, mcData, 3, item_name,
        movement_runner=_movement_runner(request, "open_near_item"),
    )
    return JSONResponse({'message': tag, 'status': flag, 'data': data})


@app.post('/post_close')
@movement_request_budget
async def close_(request: Request):
    """close item_name:  to close the door, gate, fence_gate, trapdoor, chest, etc."""
    data = await request.json()
    item_name = data.get('item_name')
    envs_info = get_envs_info(bot, 128)
    tag, flag, data = await interact_nearest(
        pathfinder, bot, Vec3, envs_info, mcData, 3, item_name,
        movement_runner=_movement_runner(request, "close_near_item"),
    )
    if flag:
        return JSONResponse({'message': "I close " + item_name, 'status': flag, 'data': data})
    else:
        return JSONResponse({'message': "I cannot close " + item_name + ", it is still open.", 'status': flag, 'data': data})


@app.post('/post_activate')
@movement_request_budget
async def activate(request: Request):
    """activate item_name:  to activate the button, lever, pressure_plate, etc."""
    data = await request.json()
    item_name = data.get('item_name')
    x, y, z = data.get('x'), data.get('y'), data.get('z')
    if x is not None and y is not None and z is not None:
        movement = await _movement_runner(request, "activate_to_position")(Vec3(x, y, z), 1)
        if not movement.success:
            return JSONResponse({'message': movement.message, 'status': False})
    envs_info = get_envs_info(bot, 128)
    target_position = Vec3(x, y, z) if x is not None and y is not None and z is not None else None
    tag, flag, data = await interact_nearest(
        pathfinder, bot, Vec3, envs_info, mcData, 3, item_name,
        target_position=target_position,
        movement_runner=_movement_runner(request, "activate_near_item"),
    )
    if flag:
        return JSONResponse({'message': "I activate " + item_name, 'status': flag, 'data': data})
    else:
        return JSONResponse({'message': "I cannot activate " + item_name + ", it is not working.", 'status': flag,
                        'data': data})


@app.post('/post_mount')
@timeout(10)
async def mount_(request: Request):
    """mount entity_name:  to mount the entity."""
    data = await request.json()
    entity_name = data.get('entity_name')
    try:
        msg, done = mount(bot, entity_name)
        return JSONResponse({'message': msg, 'status': done})
    except:
        done = False
        return JSONResponse({'message': "mount fail", 'status': done})


@app.post('/post_dismount')
@timeout(10)
async def dismount_(request: Request):
    """dismount:  to dismount the entity."""
    try:
        msg, done = dismount(bot)
        return JSONResponse({'message': msg, 'status': done})
    except:
        done = False
        return JSONResponse({'message': "dismount fail", 'status': done})


@app.post('/post_ride')
@timeout(10)
async def ride(request: Request):
    """ride entity_name:  to ride the entity."""
    data = await request.json()
    entity_name = data.get('entity_name')
    try:
        msg, done = mount(bot, entity_name)
        return JSONResponse({'message': msg, 'status': done})
    except:
        done = False
        return JSONResponse({'message': "ride fail", 'status': done})


@app.post('/post_disride')
@timeout(10)
async def disride(request: Request):
    """disride:  to disride the entity."""
    try:
        msg, done = dismount(bot)
        return JSONResponse({'message': msg, 'status': done})
    except:
        done = False
        return JSONResponse({'message': "disride fail", 'status': done})


@app.post('/post_talk_to')
@timeout(10)
async def talk_to(request: Request):
    """talk_to entity_name message:  to talk to the entity."""
    data = await request.json()
    entity_name, message = data.get('entity_name'), data.get('message')
    chat_long(bot, entity_name, message, "talk")
    return JSONResponse({'message': f"I talk to {entity_name} {message}", 'status': True})


from minecraft_eac_bridge import install_minecraft_server_eac_route
eac_preflight = install_minecraft_server_eac_route(
    app, native_bot=bot, Vec3=Vec3, timeout_decorator=timeout,
)


@app.post('/post_wait_for_feedback')
@timeout(35)
async def wait_for_feedback(request: Request):
    data = await request.json()
    entity_name, seconds = data.get('entity_name'), min(int(data.get('seconds', 10)), 30)
    chat_long(bot, entity_name, f"I am waiting for feedback, please reply in {seconds} seconds.", "talk")
    start_time = time.time()
    while time.time() - start_time < seconds:
        tag, message = info_bot.check_new_reply_from(entity_name)
        if tag:
            events = info_bot.get_action_description_new()
            return JSONResponse({'message': f"I receive feedback from {entity_name}: {message}",
                                 'status': True, 'new_events': events})
        await asyncio.sleep(0.2)
    return JSONResponse({'message': f"I do not receive feedback from {entity_name}",
                         'status': False, 'new_events': info_bot.get_action_description_new()})


@app.post('/post_done')
@timeout(10)
async def done(request: Request):
    """done:  to end the task."""
    data = await request.json()
    feedback = data.get('feedback')
    print(feedback)
    return JSONResponse({'message': "I done", 'status': True})


@app.post('/post_action')
@timeout(10)
async def action(request: Request):
    """action action_name seconds:  to do action for seconds, action_name is one of [swing_arm, forward, back, left, right, jump, sprint]."""
    data = await request.json()
    action_name, seconds = data.get('action_name'), data.get('seconds')
    if action_name == 'swing_arm':
        start_time = time.time()
        while time.time() - start_time < seconds:
            bot.swingArm()
        return JSONResponse({'message': "I swing my arms.", 'status': True})
    elif action_name == 'forward':
        while seconds > 0:
            bot.setControlState('forward', True)
            seconds -= 1
            time.sleep(1)
        bot.setControlState('forward', False)
        return JSONResponse({'message': "I move forward in a few seconds", 'status': True})
    elif action_name == 'back':
        while seconds > 0:
            bot.setControlState('back', True)
            seconds -= 1
            time.sleep(1)
        bot.setControlState('back', False)
        return JSONResponse({'message': "I move back in a few seconds", 'status': True})
    elif action_name == 'left':
        seconds = 1
        while seconds > 0:
            bot.setControlState('left', True)
            seconds -= 1
            time.sleep(1)
        bot.setControlState('left', False)
        return JSONResponse({'message': "I move left in a few seconds", 'status': True})
    elif action_name == 'right':
        while seconds > 0:
            bot.setControlState('right', True)
            seconds -= 1
            time.sleep(1)
        bot.setControlState('right', False)
        return JSONResponse({'message': "I move right in a few seconds", 'status': True})
    elif action_name == 'sprint':
        while seconds > 0:
            bot.setControlState('sprint', True)
            seconds -= 1
            time.sleep(1)
        bot.setControlState('sprint', False)
        return JSONResponse({'message': "I sprint in a few seconds", 'status': True})
    elif action_name == 'jump':
        while seconds > 0:
            bot.setControlState('jump', True)
            seconds -= 1
            time.sleep(1)
        bot.setControlState('jump', False)
        return JSONResponse({'message': "I jump in a few seconds", 'status': True})
    else:
        return JSONResponse({'message': "I cannot do this action", 'status': False})


@app.post('/post_look_at')
@timeout(10)
async def look_at(request: Request):
    """look_at name: use this to look at someone or something."""
    data = await request.json()
    name = data.get('name')
    envs_info = get_envs_info(bot, 128)
    pos = find_nearest_(bot, envs_info, mcData, name)
    if pos != None:
        lookAtPlayer(bot, pos)
    done = pos != None
    if not done:
        return JSONResponse({'message': f"cannot find {name}.", 'status': done})
    else:
        return JSONResponse({'message': f"I look at {name}.", 'status': done})


@app.post('/post_start_fishing')
@timeout(10)
async def start_fishing(request: Request):
    """start_fishing: start fishing."""
    envs_info = get_envs_info(bot, 128)
    msg, tag = startFishing(bot, envs_info, mcData)
    done = tag
    return JSONResponse({'message': msg, 'status': done})


@app.post('/post_stop_fishing')
@timeout(10)
async def stop_fishing(request: Request):
    """stop_fishing: stop fishing."""
    msg, tag = stopFishing(bot)
    done = tag
    return JSONResponse({'message': msg, 'status': done})


@app.post('/post_read')
@timeout(10)
async def read_(request: Request):
    """read name: only support read book or sign."""
    data = await request.json()
    name = data.get('name')
    envs_info = get_envs_info(bot, 128)
    msg, tag = read(bot, envs_info, mcData, name)
    done = tag
    return JSONResponse({'message': msg, 'status': done})


@app.post('/post_read_page')
@timeout(10)
async def read_page(request: Request):
    """read name: this is how you read content from book page."""
    data = await request.json()
    name, page = data.get('name'), data.get('page')
    envs_info = get_envs_info(bot, 128)
    msg, tag = read(bot, envs_info, mcData, name, page)
    done = tag
    return JSONResponse({'message': msg, 'status': done})


@app.post('/post_write')
@timeout(10)
async def write_(request: Request):
    """write name: this is how you write content on writable book or sign."""
    data = await request.json()
    name, content = data.get('name'), data.get('content')
    envs_info = get_envs_info(bot, 128)
    msg, tag = write(bot, envs_info, mcData, name, content)
    done = tag
    return JSONResponse({'message': msg, 'status': done})


@app.post('/post_chat')
@timeout(10)
async def chat_(request: Request):
    """chat message: this is how you chat."""
    data = await request.json()
    message = data.get('msg')
    message_copy = message
    while True:
        if len(message_copy) > 256:
            bot.chat(message_copy[:256])
            time.sleep(.5)
            message_copy = message_copy[256:]
        else:
            bot.chat(message_copy)
            break
    return JSONResponse({'message': f"I chat {message}", 'status': True})


@On(bot, 'spawn')
async def handleViewer(*args):
    path = [bot.entity.position]

    bot.chat('/gamemode survival')
    bot.chat('/clear @s')
    bot.chat('/give @s minecraft:book 1')
    bot.chat('/give @s minecraft:ladder 64')

    @On(bot, 'move')
    def handleMove(*args):
        try:
            if (path[-1].distanceTo(bot.entity.position) > 1.5):
                path.append(bot.entity.position)
                # bot.viewer.drawLine('path', path)
        except:
            pass

    @On(bot, 'chat')
    def handle(this, username, message, *args):
        try:
            global msg_list
            msg_list += [{"username": username, "message": message}]
        except:
            pass
        
    @On(bot, "whisper")
    def handle(this, username, message, *args):
        global msg_list
        msg_list += [{"username": username, "message": message}]


@On(bot, "itemDrop")
def handle(this, entity, *args):
    # bot.chat("item drop")
    dis = distanceTo(bot.entity.position, entity['position'])
    if (dis < 4 and bridge_accepting_movement and server_event_loop is not None
            and not movement_coordinator.active and not item_drop_tasks):
        target = entity['position']
        try:
            server_event_loop.call_soon_threadsafe(_start_item_drop_movement, target)
        except RuntimeError:
            pass


def _start_item_drop_movement(target):
    if (not bridge_accepting_movement or movement_coordinator.active
            or item_drop_tasks):
        return
    task = asyncio.create_task(_execute_movement(
        None, target, operation="item_drop_pickup", tolerance=1,
    ))
    item_drop_tasks.add(task)
    task.add_done_callback(_finish_item_drop_movement)


def _finish_item_drop_movement(task):
    item_drop_tasks.discard(task)
    if not task.cancelled():
        task.exception()

async def main():
    global server_event_loop, bridge_accepting_movement
    bridge_accepting_movement = True
    server_event_loop = asyncio.get_running_loop()
    # 配置 Uvicorn 服务器
    config = uvicorn.Config(app, port=local_port)
    server = uvicorn.Server(config)

    # 启动服务器
    serve_task = asyncio.create_task(server.serve())
    try:
        while not serve_task.done() and not server.started:
            await asyncio.sleep(0.01)
        if server.started:
            bridge_diagnostics.record_once(
                "listener_ready", actor=args.username,
                endpoint_identity=f"actor:{args.username}", expected_local_port=local_port,
            )
        await serve_task
        if not server.started:
            record_listener_failure(RuntimeError("listener_not_started"))
    except BaseException as error:
        record_listener_failure(error)
        raise
    finally:
        bridge_accepting_movement = False
        server_event_loop = None
    
# The entry point for starting the application
if __name__ == "__main__":
    # Detect if the current context is already running inside an event loop
    try:
        # If this raises an exception, we're not in an event loop
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop running, we can use asyncio.run()
        asyncio.run(main())
    else:
        # An event loop is running, we should configure and start the server directly
        uvicorn.run(app=app, port=local_port)
