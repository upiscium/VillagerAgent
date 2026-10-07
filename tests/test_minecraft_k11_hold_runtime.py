"""Runtime-scoped admission, manifest, and revision tests for K11 sensing."""
from __future__ import annotations

import json

import pytest

from benchmarks.common.eac import Proposition, PropositionKey
from benchmarks.minecraft.eac_runtime import (
    K11_V2_IMPLEMENTATION_PATHS, K11_PASSIVE_ISSUER, MinecraftEACError,
    MinecraftEACRuntime, RUNTIME_ID, RUNTIME_ID_V2,
    _authenticate_v2_implementation,
)
from benchmarks.minecraft.k11_hold_evidence import GEOMETRY_ID, SENSOR_ID
from test_minecraft_k11_hold_evidence import (
    SECRETS, _adapter, _capture, _known_cell, _register, _request, _response_bytes,
    _submit, runtime_factory,
)

EXPECTED_MANIFEST_PATHS = frozenset({
    "env/k11_visible_block_capture.js",
    "env/minecraft_server_fast.py",
    "env/minecraft_client.py",
    "benchmarks/minecraft/k11_hold_evidence.py",
    "benchmarks/minecraft/eac_runtime.py",
    "benchmarks/minecraft/k11_hold_protocol.py",
    "benchmarks/minecraft/k11_hold_trace.py",
})


@pytest.mark.parametrize("path", K11_V2_IMPLEMENTATION_PATHS)
def test_each_manifest_path_is_independently_authenticated(runtime_factory, path):
    runtime_factory()
    import benchmarks.minecraft.eac_runtime as runtime_module

    contract = json.loads(runtime_module.INGESTION_CONTRACT_V2_PATH.read_text(encoding="utf-8"))
    assert runtime_module._authenticate_v2_implementation(contract) == contract[
        "implementation_manifest"]
    contract["implementation_manifest"][path] = "0" * 64
    with pytest.raises(MinecraftEACError, match=f"manifest: {path}"):
        _authenticate_v2_implementation(contract)


def test_manifest_version_and_exact_seven_paths_are_mandatory(runtime_factory):
    runtime_factory()
    import benchmarks.minecraft.eac_runtime as runtime_module

    assert set(K11_V2_IMPLEMENTATION_PATHS) == EXPECTED_MANIFEST_PATHS
    contract = json.loads(runtime_module.INGESTION_CONTRACT_V2_PATH.read_text(encoding="utf-8"))
    assert set(contract["implementation_manifest"]) == EXPECTED_MANIFEST_PATHS
    contract["implementation_manifest_version"] = 2
    with pytest.raises(MinecraftEACError, match="manifest version"):
        _authenticate_v2_implementation(contract)
    contract["implementation_manifest_version"] = 1
    contract["implementation_manifest"].pop(K11_V2_IMPLEMENTATION_PATHS[-1])
    with pytest.raises(MinecraftEACError, match="exact source set"):
        _authenticate_v2_implementation(contract)


def test_v1_default_stays_unconditional_and_v2_is_explicit(runtime_factory):
    runtime_factory(source_version=1)
    legacy = MinecraftEACRuntime(mode="dual_dag_advisory", run_id="v1-default")
    assert legacy.source_version == 1
    assert legacy.audit_artifact()["runtime_identity"] == RUNTIME_ID
    assert legacy.authority._roots == {}

    v2 = runtime_factory(source_version=2)
    assert v2.source_version == 2
    assert v2.audit_artifact()["runtime_identity"] == RUNTIME_ID_V2


def test_authenticated_capture_sequence_is_metadata_not_root_stream_revision(runtime_factory):
    runtime = runtime_factory(mode="dual_dag_authority")
    adapter = _adapter(runtime)
    coordinate = {"x": 0, "y": 0, "z": 0}
    request = _request(adapter, tick=0)
    _register(adapter, request)
    payload, _ = _response_bytes(adapter, request, capture_seq=17,
                                 known=[_known_cell(coordinate, name="stone")])
    first = _submit(adapter, request, payload)[0]
    provenance = runtime.authority._provenance[first.provenance_id]
    assert dict(provenance.metadata)["capture_seq"] == 17
    assert first.source_stream_revision == 1
    assert first.root_id.endswith(":1")

    # A same-polarity observation at a later capture is a true no-op, and an
    # unknown gap does not expire the current root or its permit.
    prepared = runtime.prepare_tool(
        "MineBlock", lambda **_kwargs: None, (),
        {"player_name": "Alice", "x": 0, "y": 0, "z": 0})
    before = (runtime._sequence, runtime.authority._sequence,
              dict(runtime.authority._dependency_versions), set(runtime.authority._roots),
              set(runtime.authority._provenance), prepared.permit)
    request1 = _request(adapter, tick=1)
    _register(adapter, request1)
    repeated, _ = _response_bytes(adapter, request1, capture_seq=18,
                                  known=[_known_cell(coordinate, name="dirt", registry_id=2)])
    assert _submit(adapter, request1, repeated) == ()
    assert (runtime._sequence, runtime.authority._sequence,
            dict(runtime.authority._dependency_versions), set(runtime.authority._roots),
            set(runtime.authority._provenance), prepared.permit) == before
    request2 = _request(adapter, tick=2)
    _register(adapter, request2)
    gap, _ = _response_bytes(adapter, request2, capture_seq=19)
    assert _submit(adapter, request2, gap) == ()
    assert runtime._k11_current_roots[("Alice", first.proposition.key)] == first.root_id

    request3 = _request(adapter, tick=3)
    _register(adapter, request3)
    payload2, _ = _response_bytes(adapter, request3, capture_seq=23,
                                  known=[_known_cell(coordinate, name="air", registry_id=0)])
    second = _submit(adapter, request3, payload2)[0]
    assert dict(runtime.authority._provenance[second.provenance_id].metadata)["capture_seq"] == 23
    assert second.source_stream_revision == 3
    assert second.supersedes == (first.root_id,)
    assert runtime.authority._roots[first.root_id].current is False
    assert runtime.authority._roots[second.root_id].current is True


def test_v2_direct_target_and_unverified_passive_ingestion_are_closed(runtime_factory):
    runtime = runtime_factory()
    proposition = Proposition(PropositionKey(
        "minecraft", "target_block_present", (0, 0, 0), "current"))
    with pytest.raises(MinecraftEACError, match="authenticated passive evidence"):
        runtime.ingest_actor_record(
            actor_id="Alice", proposition=proposition,
            record_type="direct_observation", source="minecraft-visible-observation")
    with pytest.raises(MinecraftEACError, match="authenticated internal commit"):
        runtime.ingest_actor_record(
            actor_id="Alice", proposition=proposition,
            record_type="direct_observation", source=K11_PASSIVE_ISSUER)
    assert runtime.authority._roots == runtime.authority._provenance == {}


def test_legacy_snapshot_suppression_only_applies_to_explicit_v2(runtime_factory):
    v1 = runtime_factory(source_version=1)
    v2 = runtime_factory(source_version=2)
    state = {"status": True, "message": {"blocks": [
        {"name": "stone", "position": [0, 0, 0]}]}}
    assert len(v1.ingest_initial_actor_state("Alice", state)) == 1
    assert v2.ingest_initial_actor_state("Alice", state) == ()
    assert v2.authority._roots == {}
