"""Prospective recovery v2 artifacts: acyclic exact source-byte binding.

These tests are source for future qualification, not historical pass evidence.
"""
import hashlib
import json
from pathlib import Path

import pytest

from benchmarks.common.eac.canonical import canonical_bytes
from benchmarks.minecraft.eac_runtime import K11_V2_IMPLEMENTATION_PATHS
from benchmarks.minecraft.k11_hold_evidence import GEOMETRY


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "eac"
EXPECTED_PATHS = {
    "env/k11_visible_block_capture.js",
    "env/minecraft_server_fast.py",
    "env/minecraft_client.py",
    "benchmarks/minecraft/k11_hold_evidence.py",
    "benchmarks/minecraft/eac_runtime.py",
    "benchmarks/minecraft/k11_hold_protocol.py",
    "benchmarks/minecraft/k11_hold_trace.py",
}


def _hash(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _document(name):
    return json.loads((DOCS / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name,digest_field", [
    ("minecraft_ingestion_contract_v2.json", "detached_artifact_sha256"),
    ("minecraft_source_profile_v2.json", "detached_profile_sha256"),
])
def test_detached_v2_artifact_digest(name, digest_field):
    detached = _document(name)
    declared = detached.pop(digest_field)
    assert declared == _hash(detached)


def test_complete_versioned_source_manifest_has_no_document_digest_cycle():
    contract = _document("minecraft_ingestion_contract_v2.json")
    assert type(contract["implementation_manifest_version"]) is int
    assert contract["implementation_manifest_version"] == 1
    manifest = contract["implementation_manifest"]
    assert set(manifest) == EXPECTED_PATHS == set(K11_V2_IMPLEMENTATION_PATHS)
    assert set(contract["implementation_manifest_purpose"]) == EXPECTED_PATHS
    assert all(manifest[path] == hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
               for path in EXPECTED_PATHS)
    assert not any(path.startswith("docs/") for path in manifest)


def test_profile_binds_contract_adapter_sensor_geometry_and_rule():
    contract = _document("minecraft_ingestion_contract_v2.json")
    profile = _document("minecraft_source_profile_v2.json")
    integrity = profile["integrity_contract"]
    adapter = contract["trusted_observation_adapter"]
    assert integrity["canonical_content_sha256"] == contract["detached_artifact_sha256"]
    assert integrity["rule_evaluation_contract_sha256"] == _hash(contract["rule_evaluation"])
    assert integrity["issuer_authentication_rule_id"] == contract["issuer_authentication"]["rule_id"]
    assert adapter["geometry_digest"] == _hash(GEOMETRY)
    for path_field, digest_field in (("implementation_path", "implementation_sha256"),
                                   ("sensor_implementation_path", "sensor_implementation_sha256")):
        path = adapter[path_field]
        assert adapter[digest_field] == contract["implementation_manifest"][path]
    tools = [tool for tool in profile["trusted_tools"]
             if (tool["tool_identity"], tool["tool_version"])
             == (adapter["tool_identity"], adapter["tool_version"])]
    assert len(tools) == 1
    assert tools[0]["integrity_contract_sha256"] == _hash(adapter)


@pytest.mark.parametrize("name,sha256", [
    ("minecraft_ingestion_contract_v1.json", "ddeb257dbf3f6e042d7faf2c94fc230818260cc4fa9d97d39c0c428bd952462d"),
    ("minecraft_source_profile_v1.json", "01414650511b2ded27a5025fbc7797426b02d874cea468316fbce3711e797abe"),
])
def test_historical_v1_document_bytes_remain_frozen(name, sha256):
    assert hashlib.sha256((DOCS / name).read_bytes()).hexdigest() == sha256
