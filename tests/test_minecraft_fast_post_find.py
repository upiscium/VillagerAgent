import ast
import asyncio
import inspect
import json
from math import floor
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse


ROOT = Path(__file__).resolve().parents[1]
FAST_BRIDGE = ROOT / "env/minecraft_server_fast.py"
ENV_API = ROOT / "env/env_api.py"


def _function_node(path: Path, name: str):
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return next(node for node in module.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name)


def _load_fast_find(namespace):
    node = _function_node(FAST_BRIDGE, "find")
    node.decorator_list = []
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(FAST_BRIDGE), "exec"), namespace)
    return namespace["find"]


class Request:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class Position:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


def test_legacy_fast_post_find_positional_call_reproduces_shifted_binding():
    node = _function_node(ENV_API, "find_everything_")
    names = [argument.arg for argument in node.args.args]
    assert names[:7] == ["bot", "Vec3", "envs_info", "mcData", "name", "distance", "count"]
    signature = inspect.Signature([
        inspect.Parameter(
            name,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=(1 if name == "count" else inspect.Parameter.empty),
        )
        for name in names[:7]
    ])
    bot, envs_info, mc_data = object(), object(), object()
    bound = signature.bind(bot, envs_info, mc_data, "diamond", 64, 3)

    assert bound.arguments == {
        "bot": bot,
        "Vec3": envs_info,
        "envs_info": mc_data,
        "mcData": "diamond",
        "name": 64,
        "distance": 3,
    }


def test_fast_post_find_preserves_helper_identity_and_success_response_contract():
    bot = type("Bot", (), {"chat": lambda *_args: None})()
    vec3, envs_info, mc_data = object(), object(), object()
    captured = {}

    def find_everything_(bot, Vec3, envs_info, mcData, name="", distance=32, count=1,
                         optimize=False, visible_only=True):
        captured.update({
            "bot": bot, "Vec3": Vec3, "envs_info": envs_info, "mcData": mcData,
            "name": name, "distance": distance, "count": count,
        })
        return name, [Position(0.6, 1.6, 2.6)]

    handler = _load_fast_find({
        "Request": Request,
        "JSONResponse": JSONResponse,
        "bot": bot,
        "Vec3": vec3,
        "mcData": mc_data,
        "name_check": lambda _bot, _vec3, _mc_data, name: name,
        "get_envs_info": lambda _bot, _distance: envs_info,
        "find_everything_": find_everything_,
        "floor": floor,
    })

    response = asyncio.run(handler(Request({"name": "diamond", "distance": 64, "count": 3})))

    assert captured == {
        "bot": bot,
        "Vec3": vec3,
        "envs_info": envs_info,
        "mcData": mc_data,
        "name": "diamond",
        "distance": 64,
        "count": 3,
    }
    assert json.loads(response.body) == {
        "message": "I found diamond at 1 2 3,",
        "status": True,
        "data": [{"x": 1, "y": 2, "z": 3}],
        "observed_name": "diamond",
    }


def test_fast_post_find_internal_helper_failure_is_not_a_success_response():
    def fail(*_args, **_kwargs):
        raise AttributeError("synthetic helper failure")

    handler = _load_fast_find({
        "Request": Request,
        "JSONResponse": JSONResponse,
        "bot": type("Bot", (), {"chat": lambda *_args: None})(),
        "Vec3": object(),
        "mcData": object(),
        "name_check": lambda _bot, _vec3, _mc_data, name: name,
        "get_envs_info": lambda _bot, _distance: object(),
        "find_everything_": fail,
        "floor": floor,
    })

    with pytest.raises(AttributeError, match="synthetic helper failure"):
        asyncio.run(handler(Request({"name": "diamond", "distance": 64, "count": 3})))
