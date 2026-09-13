"""Authenticated, sealed runtime inputs for the K12 live qualification."""
from __future__ import annotations

import copy
import json
import hashlib
import importlib.metadata
from collections.abc import Mapping, Iterator
from dataclasses import dataclass
from types import MappingProxyType
from pathlib import Path
from typing import Any

from benchmarks.common.eac.canonical import canonical_bytes
from benchmarks.minecraft.k12_live_fixture import qualification_ids

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RUNTIME_PROFILE_PATH = HERE / "k12_live_runtime_profile_v1.json"
QUALIFICATION_MANIFEST_PATH = ROOT / "configs/minecraft/k12-live-qualification-manifest-v1.json"
LIVE_PROFILE_IDENTITY = "minecraft-k12-live-runtime-profile/1"
LIVE_QUALIFICATION_IDENTITY = "minecraft-eac-k12-live-runtime-qualification/1"
LIVE_SCHEDULE_IDENTITY = "minecraft-k12-live-runtime-qualification-schedule/1"


class K12RuntimeProfileError(ValueError):
    pass


class _FrozenList(tuple):
    def __eq__(self, other: object) -> bool:
        return tuple(self) == tuple(other) if isinstance(other, (list, tuple)) else False


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_freeze(item) for item in value)
    return value


_PROFILE_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class K12RuntimeProfile(Mapping[str, Any]):
    """The only trusted root produced by the detached-digest loader."""
    values: tuple[tuple[str, Any], ...]

    def __init__(self, values: tuple[tuple[str, Any], ...], token: object = None) -> None:
        if token is not _PROFILE_TOKEN:
            raise TypeError("K12RuntimeProfile is loader-minted")
        object.__setattr__(self, "values", values)
        self.__post_init__()

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", tuple((key, _freeze(value))
                                                  for key, value in self.values))

    def __getitem__(self, key: str) -> Any:
        for name, value in self.values:
            if name == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self.values)

    def __len__(self) -> int:
        return len(self.values)

    @property
    def profile_id(self) -> str:
        return self["profile_version"]

    @property
    def profile_digest(self) -> str:
        return self["detached_artifact_sha256"]


def detached_digest(value: Mapping[str, Any]) -> str:
    import hashlib
    return hashlib.sha256(canonical_bytes({k: v for k, v in value.items()
                                           if k != "detached_artifact_sha256"})).hexdigest()


def strict_json_load(path: Path, label: str) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise K12RuntimeProfileError(f"duplicate JSON key in {label}: {key}")
            result[key] = value
        return result
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=object_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise K12RuntimeProfileError(f"cannot load {label}") from exc
    if not isinstance(value, dict):
        raise K12RuntimeProfileError(f"{label} must be an object")
    declared = value.get("detached_artifact_sha256")
    observed = detached_digest(value)
    if (not isinstance(declared, str) or len(declared) != 64
            or any(char not in "0123456789abcdef" for char in declared)
            or declared != observed):
        raise K12RuntimeProfileError(f"{label} detached digest mismatch")
    return value


_IDENTITIES = {
    "source_identity": "minecraft-eac-k12-live-source/1",
    "config_identity": "minecraft-eac-k12-live-config/1",
    "fixture_identity": "minecraft-eac-k12-live-fixture/1",
    "bridge_identity": "minecraft-server-fast/1",
    "capability_identity": "minecraft-k12-guarded-tool-capability/1",
    "provider_identity": "minecraft-eac-provider/1",
    "server_identity": "minecraft-server/1.19.2",
    "data_identity": "minecraft-data/1",
    "fastapi_identity": "fastapi/0.136.1",
    "starlette_identity": "starlette/1.0.0",
    "openai_sdk_identity": "openai/1.6.1",
    "tiktoken_identity": "tiktoken/0.5.1",
    "httpx_identity": "httpx/0.25.2",
    "pyyaml_identity": "PyYAML/6.0.3",
    "eac_identity": "eac/1",
    "rcon_identity": "minecraft-rcon/1",
    "reset_identity": "minecraft-k12-live-reset-readback/1",
    "parser_identity": "minecraft-eac-parser/1",
    "state_identity": "minecraft-k12-live-state/1",
    "containment_identity": "minecraft-k12-live-containment/1",
    "stop_identity": "minecraft-k12-live-stop-policy/1",
    "action_identity": "minecraft-eac-actions/1",
    "oracle_identity": "minecraft-k12-live-oracle/1",
    "model_identity": "live-model/1",
}


def load_k12_live_runtime_profile(path: str | Path = RUNTIME_PROFILE_PATH) -> K12RuntimeProfile:
    value = strict_json_load(Path(path), "K12 live runtime profile")
    if (value.get("artifact_id") != "minecraft-k12-live-runtime-profile"
            or value.get("artifact_version") != 1
             or value.get("profile_version") != LIVE_PROFILE_IDENTITY
            or value.get("runtime_mode") != "guarded_real"
            or value.get("execution_mode") != "live"
             or value.get("locale") != "en_us"
            or value.get("minecraft_version") != "1.19.2"
            or value.get("server_jar_sha256") != "b26727069ef5f61c704add9a378ac90e3d271fd7876c0bd3dcfbe9fd0bec4d96"
            or value.get("source_identity") != "minecraft-eac-k12-live-source/1"
            or value.get("config_identity") != "minecraft-eac-k12-live-config/1"
            or value.get("fixture_identity") != "minecraft-eac-k12-live-fixture/1"
            or value.get("sealed") is not True
            or value.get("before_campaign_identity") is not True
            or any(value.get(key) is not False for key in
                   ("fake_backend_allowed", "offline_mode_allowed", "caller_overrides_allowed"))):
        raise K12RuntimeProfileError("K12 live runtime profile is not sealed guarded_real")
    expected = list(qualification_ids())
    if (value.get("schedule") != expected or len(expected) != 15
            or value.get("schedule_identity") != LIVE_SCHEDULE_IDENTITY):
        raise K12RuntimeProfileError("K12 live schedule mismatch")
    revision = value.get("execution_revision")
    if not isinstance(revision, str) or len(revision) != 40 or any(
            char not in "0123456789abcdef" for char in revision):
        raise K12RuntimeProfileError("full execution revision is required")
    if any(value.get(key) != expected_value for key, expected_value in _IDENTITIES.items()):
        raise K12RuntimeProfileError("K12 frozen identity mismatch")
    for package,key in (("fastapi","fastapi_identity"),("starlette","starlette_identity"),("openai","openai_sdk_identity"),("tiktoken","tiktoken_identity"),("httpx","httpx_identity"),("PyYAML","pyyaml_identity")):
        try:
            installed=importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError as exc:
            raise K12RuntimeProfileError(f"missing runtime package: {package}") from exc
        if value[key] != f"{package}/{installed}":
            raise K12RuntimeProfileError(f"installed package identity mismatch: {package}")
    # The bridge is authenticated as content, not merely by its symbolic name.
    bridge = HERE.parent.parent / "env" / "minecraft_server_fast.py"
    try:
        bridge_digest = hashlib.sha256(bridge.read_bytes()).hexdigest()
    except OSError as exc:
        raise K12RuntimeProfileError("cannot authenticate bridge content") from exc
    if value.get("bridge_content_sha256") != bridge_digest:
        raise K12RuntimeProfileError("bridge content digest mismatch")
    policy = value.get("provider_policy")
    expected_policy = {
        "transport": "openai_compatible_sealed", "temperature": 0,
        "attempts": 1, "retries": 0, "cache": False, "stream": False,
        "image": False, "connect_timeout_seconds": 5, "request_timeout_seconds": 120,
    }
    if policy != expected_policy:
        raise K12RuntimeProfileError("K12 provider policy mismatch")
    if any(not isinstance(value.get(name), str) or len(value[name]) != 64
           for name in ("endpoint_sha256", "endpoint_hash", "eac_runtime_sha256",
                        "eac_config_sha256")):
        raise K12RuntimeProfileError("K12 source closure is incomplete")
    source_paths = {
        "eac_runtime_sha256": HERE / "eac_runtime.py",
        "eac_config_sha256": HERE / "k12_protocol_v1.json",
    }
    try:
        if any(value[name] != hashlib.sha256(source.read_bytes()).hexdigest()
               for name, source in source_paths.items()):
            raise K12RuntimeProfileError("K12 source closure mismatch")
    except OSError as exc:
        raise K12RuntimeProfileError("cannot authenticate K12 source closure") from exc
    if value["endpoint_sha256"] != value["endpoint_hash"]:
        raise K12RuntimeProfileError("provider endpoint identity mismatch")
    expected_sources = {
        "benchmarks/minecraft/k12_runtime_profile.py",
        "benchmarks/minecraft/k12_guarded_backend.py",
        "benchmarks/minecraft/k12_live_fixture.py",
        "benchmarks/minecraft/k12_live_state.py",
        "benchmarks/minecraft/k12_live_reset.py",
        "benchmarks/minecraft/k12_live_oracle.py",
        "benchmarks/minecraft/k12_live_containment.py",
        "benchmarks/minecraft/k12_live_runner.py",
        "benchmarks/minecraft/k12_live_validation.py",
        "benchmarks/minecraft/k12_live_artifacts.py",
        "benchmarks/minecraft/k12_live_qualification.py",
        "benchmarks/minecraft/k12_model.py",
        "benchmarks/minecraft/k12_evidence.py",
        "model/openai_models.py",
        "model/abstract_language_model.py",
        "model/utils.py",
        "env/runtime_paths.py",
        "benchmarks/common/eac/canonical.py",
        "benchmarks/minecraft/k12_containment.py",
        "requirements.txt",
    }
    closure = value.get("source_closure")
    if not isinstance(closure, dict) or set(closure) != expected_sources:
        raise K12RuntimeProfileError("K12 live source closure is incomplete")
    try:
        if any(not isinstance(digest, str) or len(digest) != 64
               or hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != digest
               for relative, digest in closure.items()):
            raise K12RuntimeProfileError("K12 live source closure mismatch")
    except OSError as exc:
        raise K12RuntimeProfileError("cannot authenticate K12 live source closure") from exc
    contract_paths = {
        "reset": HERE / "k12_live_reset_readback_v1.json",
        "oracle": HERE / "k12_live_oracle_v1.json",
        "stop_policy": HERE / "k12_live_stop_policy_v1.json",
        "qualification": HERE / "k12_live_qualification_v1.json",
        "qualification_manifest": QUALIFICATION_MANIFEST_PATH,
        "containment_probe": ROOT / "configs/minecraft/k12-live-containment-probe-v1.json",
    }
    contracts = value.get("contract_digests")
    if not isinstance(contracts, dict) or set(contracts) != set(contract_paths):
        raise K12RuntimeProfileError("K12 live contract closure is incomplete")
    for name, contract_path in contract_paths.items():
        contract = strict_json_load(contract_path, f"K12 live {name} contract")
        if contracts[name] != contract.get("detached_artifact_sha256"):
            raise K12RuntimeProfileError("K12 live contract closure mismatch")
    return K12RuntimeProfile(tuple(value.items()), _PROFILE_TOKEN)


def load_k12_live_qualification_manifest(path: str | Path = QUALIFICATION_MANIFEST_PATH) -> dict[str, Any]:
    value = strict_json_load(Path(path), "K12 live qualification manifest")
    profile = load_k12_live_runtime_profile()
    if (value.get("artifact_id") != "minecraft-eac-k12-live-runtime-qualification"
            or value.get("artifact_version") != 1
            or value.get("schema_version") != LIVE_QUALIFICATION_IDENTITY
            or value.get("runtime_profile") != LIVE_PROFILE_IDENTITY
            or value.get("cell_count") != 15
             or tuple(value.get("schedule", ())) != tuple(profile["schedule"])
             or value.get("schedule_identity") != LIVE_SCHEDULE_IDENTITY):
        raise K12RuntimeProfileError("K12 live qualification manifest mismatch")
    return copy.deepcopy(value)


# Short names are intentionally aliases, not alternate loading paths; callers
# must use the same sealed profile and detached-digest checks.
load_live_runtime_profile = load_k12_live_runtime_profile
load_live_qualification_manifest = load_k12_live_qualification_manifest


__all__ = ["K12RuntimeProfile", "K12RuntimeProfileError", "LIVE_PROFILE_IDENTITY", "LIVE_QUALIFICATION_IDENTITY",
           "LIVE_SCHEDULE_IDENTITY",
           "detached_digest", "strict_json_load", "load_k12_live_qualification_manifest", "load_k12_live_runtime_profile",
           "load_live_qualification_manifest", "load_live_runtime_profile"]
