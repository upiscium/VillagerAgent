import ast
import asyncio
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse, Response

from benchmarks.common.eac.canonical import canonical_bytes


ROOT = Path(__file__).resolve().parents[1]
FAST_BRIDGE = ROOT / "env/minecraft_server_fast.py"
CLIENT = ROOT / "env/minecraft_client.py"
SCHEMA = "minecraft-k11-visible-block-snapshot/1"
SENSOR = "minecraft-k11-fixed-passive-sensor/1"
GEOMETRY_ID = "minecraft-k11-360-supercover-5x3x5/1"
GEOMETRY = {
    "id": GEOMETRY_ID,
    "offsets": {"x": [-2, 2], "y": [-1, 1], "z": [-2, 2]},
    "max_steps": 16,
    "eye": "entity.position+eyeHeight",
    "los": "3d-supercover/1",
}
GEOMETRY_DIGEST = hashlib.sha256(canonical_bytes(GEOMETRY)).hexdigest()
SENSOR_DIGEST = "a" * 64
PROFILE_DIGEST = "b" * 64
INGESTION_DIGEST = "c" * 64
SECRET = b"K11 fixture-only bridge key, not a production credential"
IMPLEMENTATION_PATHS = frozenset({
    "env/k11_visible_block_capture.js",
    "env/minecraft_server_fast.py",
    "env/minecraft_client.py",
    "benchmarks/minecraft/k11_hold_evidence.py",
    "benchmarks/minecraft/eac_runtime.py",
    "benchmarks/minecraft/k11_hold_protocol.py",
    "benchmarks/minecraft/k11_hold_trace.py",
})
REQUEST_KEYS = {
    "schema", "run_id", "window_id", "actor_id", "tick_index", "nonce",
    "sensor_id", "sensor_digest", "profile_digest", "ingestion_digest",
    "geometry_id", "geometry_digest", "request_hmac",
}
RESPONSE_BINDING_FIELDS = (
    "run_id", "window_id", "actor_id", "tick_index", "nonce", "sensor_id",
    "sensor_digest", "profile_digest", "ingestion_digest", "geometry_id", "geometry_digest",
)
UNKNOWN_REASONS = frozenset({
    "unloaded_target", "unloaded_path", "occluded", "outside_region", "invalid_pose",
    "step_limit", "incoherent_capture", "unmapped_registry",
})


def _load_function(path, name, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    node = next(
        value for value in tree.body
        if isinstance(value, (ast.FunctionDef, ast.AsyncFunctionDef)) and value.name == name
    )
    node.decorator_list = []
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[name]


def test_sealed_helper_bootstrap_returns_js_module_across_actual_rpc():
    """Hermetic Python-to-Node VM test; no Mineflayer, server or HTTP runtime."""
    from javascript import require

    loader = _load_function(FAST_BRIDGE, "_k11_load_verified_capture", {})
    vm = require("vm")
    options = vm.runInThisContext("""({
      Vec3: class Vec3 { constructor(x,y,z) { this.x=x; this.y=y; this.z=z; } },
      mcData: {blocksByName: {air: {id:0, name:'air'}}}
    })""")
    bot = vm.runInThisContext("""new Proxy({
      entity: {position: {x:0.5,y:0,z:0.5}, eyeHeight:1.5},
      blockAt(p, extra) {
        if (extra !== false) throw new Error('chunk loading forbidden');
        return {type:0, name:'air'};
      }
    }, {get(obj,key) {
        // The RPC bridge awaits return values and identifies proxy references.
        // These two metadata probes are not gameplay API accesses.
        if (key === 'then' || key === 'ffid') return undefined;
        if (key === 'entity' || key === 'blockAt') return obj[key];
        throw new Error('unexpected gameplay access');
      }, set() { throw new Error('gameplay mutation forbidden'); }
    })""")
    capture = loader((ROOT / "env/k11_visible_block_capture.js").read_bytes(), options, require)
    for sequence in (1, 2):
        response = json.loads(capture(bot))
        assert response["capture_seq"] == sequence
        assert response["complete"] is True and response["error"] is None
        assert len(response["cells"]) == 75
        assert sum(cell["state"] == "known_air" for cell in response["cells"]) == 74


def _load_method(path, class_name, method_name, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                 and node.name == class_name)
    method = next(node for node in owner.body if isinstance(node, ast.FunctionDef)
                  and node.name == method_name)
    method.decorator_list = []
    module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[method_name]


def _valid_identifier(value):
    return isinstance(value, str) and 1 <= len(value) <= 128 and bool(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", value)
    )


def _request(actor="Alice"):
    payload = {
        "schema": SCHEMA,
        "run_id": "run-k11-1",
        "window_id": "window-1",
        "actor_id": actor,
        "tick_index": 4,
        "nonce": "0123456789abcdef0123456789abcdef",
        "sensor_id": SENSOR,
        "sensor_digest": SENSOR_DIGEST,
        "profile_digest": PROFILE_DIGEST,
        "ingestion_digest": INGESTION_DIGEST,
        "geometry_id": GEOMETRY_ID,
        "geometry_digest": GEOMETRY_DIGEST,
    }
    payload["request_hmac"] = hmac.new(
        SECRET, canonical_bytes(payload), hashlib.sha256,
    ).hexdigest()
    return payload


def _load_server_validation():
    valid_identifier = _load_function(FAST_BRIDGE, "_k11_valid_identifier", {"re": re})
    return _load_function(FAST_BRIDGE, "_k11_validate_request", {
        "hmac": hmac,
        "hashlib": hashlib,
        "re": re,
        "canonical_bytes": canonical_bytes,
        "K11_REQUEST_KEYS": REQUEST_KEYS,
        "K11_REQUEST_SCHEMA": SCHEMA,
        "K11_SENSOR_ID": SENSOR,
        "K11_GEOMETRY_ID": GEOMETRY_ID,
        "_k11_valid_identifier": valid_identifier,
    })


def _load_response_encoder():
    namespace = {
        "hmac": hmac,
        "hashlib": hashlib,
        "json": json,
        "re": re,
        "floor": __import__("math").floor,
        "isfinite": __import__("math").isfinite,
        "canonical_bytes": canonical_bytes,
        "K11_RESPONSE_BINDING_FIELDS": RESPONSE_BINDING_FIELDS,
        "K11_EXPECTED_OFFSETS": tuple(
            {"x": x, "y": y, "z": z}
            for x in range(-2, 3) for y in range(-1, 2) for z in range(-2, 3)
        ),
        "K11_UNKNOWN_REASONS": UNKNOWN_REASONS,
        "_k11_sha256": lambda value: hashlib.sha256(value).hexdigest(),
    }
    for function in ("_k11_finite_number_string", "_k11_integer_position",
                     "_k11_expected_supercover", "_k11_valid_registry_proof"):
        _load_function(FAST_BRIDGE, function, namespace)
    return _load_function(FAST_BRIDGE, "_k11_response_bytes", namespace)


def _capture_fixture():
    pose = {"x": "0.25", "y": "5.1", "z": "-0.25"}
    eye = {"x": "0.25", "y": "6.72", "z": "-0.25", "eye_height": "1.62"}
    expected_supercover = _load_function(FAST_BRIDGE, "_k11_expected_supercover", {
        "floor": __import__("math").floor,
        "isfinite": __import__("math").isfinite,
    })
    eye_origin = {"x": 0.25, "y": 6.72, "z": -0.25}
    eye_voxel = {"x": 0, "y": 6, "z": -1}
    cells = []
    for x in range(-2, 3):
        for y in range(-1, 2):
            for z in range(-2, 3):
                position = {"x": x, "y": 5 + y, "z": -1 + z}
                if position == {"x": 0, "y": 6, "z": -1}:
                    cells.append({
                        "offset": {"x": x, "y": y, "z": z}, "position": position,
                        "state": "unknown", "unknown_reason": "outside_region",
                    })
                else:
                    cells.append({
                        "offset": {"x": x, "y": y, "z": z}, "position": position,
                        "state": "known_air", "registry_id": 0, "block_name": "air",
                        "coverage": {
                            "eye_loaded": {
                                "position": {"x": 0, "y": 6, "z": -1},
                                "registry_id": 0, "block_name": "air",
                            },
                            "target_loaded": {
                                "position": position, "registry_id": 0, "block_name": "air",
                            },
                            "path": [
                                {"position": point, "registry_id": 0, "block_name": "air"}
                                for point in expected_supercover(eye_origin, position)
                                if point not in (eye_voxel, position)
                            ],
                        },
                    })
    return {
        "capture_seq": 1,
        "capture_started_monotonic_ns": "100",
        "capture_ended_monotonic_ns": "110",
        "pose": pose,
        "eye": eye,
        "cells": cells,
        "complete": True,
        "truncated": False,
        "error": None,
    }


def test_fixed_request_schema_rejects_action_fields_before_capture():
    validate = _load_server_validation()
    forbidden_fields = (
        "target", "position", "movement", "pathfinder", "look", "turn", "chat",
        "whisper", "inventory", "equip", "place", "dig", "attack", "use",
        "tool", "planner", "model", "task", "event", "emit", "action",
    )
    for field in forbidden_fields:
        payload = _request()
        payload[field] = {"x": 1, "y": 2, "z": 3}
        assert validate(
            payload, actor_id="Alice", bot_username="Alice", run_id="run-k11-1",
            secret=SECRET, sensor_digest=SENSOR_DIGEST, profile_digest=PROFILE_DIGEST,
            ingestion_digest=INGESTION_DIGEST, geometry_digest=GEOMETRY_DIGEST,
        ) == "invalid_request"


def test_response_proof_positions_and_pose_must_match_fixed_geometry():
    encode = _load_response_encoder()
    capture = _capture_fixture()
    encoded = encode(
        _request(), capture, secret=SECRET, bridge_id="d" * 32,
        profile_digest=PROFILE_DIGEST, ingestion_digest=INGESTION_DIGEST,
        sensor_digest=SENSOR_DIGEST, geometry_digest=GEOMETRY_DIGEST,
    )
    assert json.loads(encoded)["cells"] == capture["cells"]

    bad_position = json.loads(json.dumps(capture))
    bad_position["cells"][0]["position"]["x"] += 1
    with pytest.raises(ValueError, match="invalid_capture"):
        encode(
            _request(), bad_position, secret=SECRET, bridge_id="d" * 32,
            profile_digest=PROFILE_DIGEST, ingestion_digest=INGESTION_DIGEST,
            sensor_digest=SENSOR_DIGEST, geometry_digest=GEOMETRY_DIGEST,
        )
    bad_pose = json.loads(json.dumps(capture))
    bad_pose["eye"]["y"] = "not-a-number"
    with pytest.raises(ValueError, match="invalid_capture"):
        encode(
            _request(), bad_pose, secret=SECRET, bridge_id="d" * 32,
            profile_digest=PROFILE_DIGEST, ingestion_digest=INGESTION_DIGEST,
            sensor_digest=SENSOR_DIGEST, geometry_digest=GEOMETRY_DIGEST,
        )


def test_client_rejects_signed_but_geometrically_malformed_response():
    encode = _load_response_encoder()
    capture = _capture_fixture()
    request = _request()
    response = json.loads(encode(
        request, capture, secret=SECRET, bridge_id="d" * 32,
        profile_digest=PROFILE_DIGEST, ingestion_digest=INGESTION_DIGEST,
        sensor_digest=SENSOR_DIGEST, geometry_digest=GEOMETRY_DIGEST,
    ))
    namespace = {
        "hashlib": hashlib,
        "hmac": hmac,
        "re": re,
        "floor": __import__("math").floor,
        "isfinite": __import__("math").isfinite,
        "canonical_bytes": canonical_bytes,
        "K11_RESPONSE_KEYS": frozenset({
            *RESPONSE_BINDING_FIELDS, "bridge_id", "capture_seq",
            "capture_started_monotonic_ns", "capture_ended_monotonic_ns", "pose", "eye",
            "cells", "cell_payload_digest", "complete", "truncated", "error",
            "request_digest", "hmac_sha256",
        }),
        "K11_RESPONSE_BINDING_FIELDS": RESPONSE_BINDING_FIELDS,
        "K11_EXPECTED_OFFSETS": tuple(
            {"x": x, "y": y, "z": z}
            for x in range(-2, 3) for y in range(-1, 2) for z in range(-2, 3)
        ),
        "K11_UNKNOWN_REASONS": UNKNOWN_REASONS,
        "_k11_sha256": lambda data: hashlib.sha256(data).hexdigest(),
    }
    for function in ("_k11_finite_number_string", "_k11_integer_position",
                     "_k11_expected_supercover", "_k11_valid_registry_proof"):
        _load_function(CLIENT, function, namespace)
    verify = _load_function(CLIENT, "_k11_verify_capture_response", namespace)
    assert verify(response, request, SECRET) == response

    malformed = json.loads(json.dumps(response))
    malformed["cells"][0]["coverage"]["target_loaded"]["position"]["x"] += 1
    # Re-sign to show geometry is checked independently of HMAC validity.
    malformed["hmac_sha256"] = hmac.new(
        SECRET,
        canonical_bytes({key: value for key, value in malformed.items() if key != "hmac_sha256"}),
        hashlib.sha256,
    ).hexdigest()
    with pytest.raises(ValueError, match="invalid K11 response"):
        verify(malformed, request, SECRET)


class _Request:
    def __init__(self, content):
        self.content = content

    async def stream(self):
        yield self.content


def test_observation_route_calls_only_the_capture_boundary_and_sanitizes_rejection():
    calls = []
    bot_effects = []

    class HostileBot:
        username = "Alice"

        def __getattr__(self, name):
            bot_effects.append(name)
            raise AssertionError("passive bridge route accessed a gameplay API")

    bot = HostileBot()
    capture = _capture_fixture()

    def capture_once(actual_bot):
        assert actual_bot is bot
        calls.append("capture")
        return json.dumps(capture)

    validate = _load_server_validation()
    encode = _load_response_encoder()
    claim_nonce = lambda *_args: True
    namespace = {
        "Request": object,
        "Response": Response,
        "JSONResponse": JSONResponse,
        "json": json,
        "_k11_json_no_duplicate_keys": _load_function(
            FAST_BRIDGE, "_k11_json_no_duplicate_keys", {},
        ),
        "_k11_source_seal": lambda: (SENSOR_DIGEST, PROFILE_DIGEST, INGESTION_DIGEST),
        "K11_SENSOR_DIGEST": SENSOR_DIGEST,
        "K11_GEOMETRY_DIGEST": GEOMETRY_DIGEST,
        "K11_BRIDGE_ID": "d" * 32,
        "K11_GEOMETRY": GEOMETRY,
        "_k11_secret": SECRET,
        "_k11_run_id": "run-k11-1",
        "_k11_validate_request": validate,
        "_k11_claim_nonce": claim_nonce,
        "_k11_response_bytes": encode,
        "K11_RESPONSE_BINDING_FIELDS": RESPONSE_BINDING_FIELDS,
        "args": SimpleNamespace(username="Alice"),
        "bot": bot,
        "k11_visible_block_capture": capture_once,
    }
    handler = _load_function(FAST_BRIDGE, "post_k11_visible_block_region_v1", namespace)
    accepted = asyncio.run(handler(_Request(canonical_bytes(_request()))))
    assert accepted.status_code == 200
    assert calls == ["capture"] and bot_effects == []

    rejected_payload = _request()
    rejected_payload["attack"] = "target"
    rejected = asyncio.run(handler(_Request(canonical_bytes(rejected_payload))))
    assert rejected.status_code == 400
    assert json.loads(rejected.body)["reason"] == "invalid_request"
    assert calls == ["capture"] and bot_effects == []


def test_sensor_key_pipe_is_fd_only_one_shot_and_closes_parent_descriptors(monkeypatch):
    key = bytes(range(32))
    read_fd, write_fd = os.pipe()
    try:
        assert os.write(write_fd, key) == len(key)
    finally:
        os.close(write_fd)
    monkeypatch.setenv("K11_SENSOR_KEY_FD", str(read_fd))
    monkeypatch.setenv("K11_SENSOR_RUN_ID", "run-1")
    monkeypatch.setenv("K11_SENSOR_KEY_HEX", "must-not-be-read")
    credentials = _load_function(FAST_BRIDGE, "_k11_environment_credentials", {
        "os": SimpleNamespace(environ=os.environ, read=os.read, close=os.close),
        "re": re,
    })
    secret, run_id = credentials()
    assert secret == key and run_id == "run-1"
    assert "K11_SENSOR_KEY_FD" not in os.environ
    assert "K11_SENSOR_RUN_ID" not in os.environ
    assert "K11_SENSOR_KEY_HEX" not in os.environ
    with pytest.raises(OSError):
        os.fstat(read_fd)

    created = []
    observed = []

    def tracked_pipe():
        pair = os.pipe()
        created.extend(pair)
        return pair

    def fake_popen(command, *, shell, **child):
        assert shell is False
        fd = int(child["env"]["K11_SENSOR_KEY_FD"])
        piped_key = os.read(fd, 256)
        observed.append((list(command), dict(child["env"]), child["pass_fds"], piped_key))
        return object()

    spawn = _load_function(CLIENT, "_k11_spawn_with_sensor_key", {
        "os": SimpleNamespace(pipe=tracked_pipe, write=os.write, close=os.close),
        "subprocess": SimpleNamespace(Popen=fake_popen),
        "K11_SENSOR_KEY_MAX_BYTES": 256,
        "_k11_valid_identifier": _valid_identifier,
    })
    command = ["/python", "bridge_fast.py", "-U", "Alice"]
    child = {"env": {"PATH": "/bin", "K11_SENSOR_KEY_HEX": "ambient"}, "pass_fds": ()}
    spawn(command, child, key, "run-fixed")
    public = repr((observed[0][0], observed[0][1], command))
    assert observed[0][3] == key
    assert observed[0][1]["K11_SENSOR_RUN_ID"] == "run-fixed"
    assert "K11_SENSOR_KEY_HEX" not in observed[0][1]
    assert key.hex() not in public and "ambient" not in public
    for descriptor in created:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_client_passive_adapter_is_not_a_tool_and_uses_no_gameplay_or_model_apis():
    calls = []
    prohibited = []

    def forbidden(name):
        def fail(*_args, **_kwargs):
            prohibited.append(name)
            raise AssertionError("passive client invoked a prohibited operation")
        return fail

    class GuardedAgent:
        headers = {"Content-Type": "application/json"}
        bridge_entrypoint_by_name = {"Alice": "bridge_fast"}
        k11_sensor_keys_by_actor = {"Alice": SECRET}
        k11_sensor_run_id = "run-1"
        get_agent_url = staticmethod(lambda _actor: "http://localhost:5000")

    for name in (
        "movement", "pathfinder", "look", "turn", "chat", "whisper", "inventory",
        "equip", "place", "dig", "attack", "use", "tool", "planner", "model",
        "task", "event", "emit",
    ):
        setattr(GuardedAgent, name, staticmethod(forbidden(name)))

    class Client:
        name = "Alice"

        def __getattr__(self, name):
            prohibited.append(f"client.{name}")
            raise AssertionError("passive adapter read a gameplay client API")

    class FakeResponse:
        content = b"{}"
        status_code = 200

        @staticmethod
        def json():
            return {"passive": True}

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse()

    namespace = {
        "Agent": GuardedAgent,
        "secrets": __import__("secrets"),
        "re": re,
        "hashlib": hashlib,
        "hmac": hmac,
        "canonical_bytes": canonical_bytes,
        "K11_SENSOR_KEY_MAX_BYTES": 256,
        "K11_REQUEST_SCHEMA": SCHEMA,
        "K11_SENSOR_ID": SENSOR,
        "K11_GEOMETRY_ID": GEOMETRY_ID,
        "K11_GEOMETRY": GEOMETRY,
        "_k11_valid_identifier": _valid_identifier,
        "_k11_source_seal": lambda: (SENSOR_DIGEST, PROFILE_DIGEST, INGESTION_DIGEST),
        "_k11_sha256": lambda data: hashlib.sha256(data).hexdigest(),
        "_minecraft_request": request,
        "_k11_verify_capture_response": lambda result, _payload, _secret: result,
    }
    capture = _load_method(CLIENT, "Agent", "capture_k11_visible_block_region", namespace)
    result = capture(
        Client(), window_id="window-1", tick_index=0,
        nonce="0123456789abcdef0123456789abcdef",
    )
    assert result == {"passive": True}
    assert len(calls) == 1 and calls[0][0] == "POST"
    assert calls[0][1].endswith("/post_k11_visible_block_region_v1")
    assert calls[0][2]["timeout"] == (0.1, 0.35)
    assert json.loads(calls[0][2]["data"])["actor_id"] == "Alice"
    assert prohibited == []


def test_fast_bridge_only_registers_fixed_route_and_keeps_v1_find_unmodified():
    server = ast.parse(FAST_BRIDGE.read_text(encoding="utf-8"))
    find = next(node for node in server.body if isinstance(node, ast.AsyncFunctionDef)
                and node.name == "find")
    assert "K11_REQUEST_SCHEMA" not in ast.unparse(find)
    assert "use_k11_visible_block_region_endpoint" not in ast.unparse(find)
    source = FAST_BRIDGE.read_text(encoding="utf-8")
    assert '"implementation_manifest_version") != 1' in source
    assert "runInThisContext" in source
    assert "read_bytes()" in source
    assert "require(str(_k11_helper_path))" not in source

    client = ast.parse(CLIENT.read_text(encoding="utf-8"))
    agent = next(node for node in client.body if isinstance(node, ast.ClassDef)
                 and node.name == "Agent")
    adapter = next(node for node in agent.body if isinstance(node, ast.FunctionDef)
                   and node.name == "capture_k11_visible_block_region")
    assert adapter.decorator_list == []
    launch = next(node for node in agent.body if isinstance(node, ast.FunctionDef)
                  and node.name == "launch")
    assert {arg.arg for arg in (*launch.args.args, *launch.args.kwonlyargs)} >= {
        "k11_sensor_keys", "k11_sensor_run_id",
    }
    launch_source = ast.unparse(launch)
    assert "K11 sensor credentials are supported only by the fast bridge" in launch_source
    assert "runtime_execution.public_command(entrypoint, *args)" in launch_source
    assert "capture_k11_visible_block_region" not in ast.unparse(
        next(node for node in agent.body if isinstance(node, ast.FunctionDef)
             and node.name == "__init__")
    )
