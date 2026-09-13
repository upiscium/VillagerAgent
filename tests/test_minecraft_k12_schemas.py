import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
BM = ROOT / "benchmarks/minecraft"
CFG = ROOT / "configs/minecraft"


def load(path):
    pairs = []
    def hook(items):
        keys = [key for key, _ in items]
        assert len(keys) == len(set(keys)), f"duplicate JSON key in {path}"
        return dict(items)
    return json.loads(path.read_text(), object_pairs_hook=hook)


def digest(document):
    body = {key: value for key, value in document.items() if key != "detached_artifact_sha256"}
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def test_artifacts_load_without_duplicate_keys_and_have_identity():
    paths = [BM / name for name in ("k12_protocol_v1.json", "k12_request_schema_v1.json", "k12_trace_schema_v1.json", "k12_result_schema_v1.json", "k12_validation_contract_v1.json")]
    paths += [CFG / name for name in ("k12-recovery-fixture-manifest-v1.json", "k12-recovery-randomization-manifest-v1.json")]
    for path in paths:
        value = load(path)
        assert value["artifact_version"] == 1
        assert isinstance(value["detached_artifact_sha256"], str)
        assert len(value["detached_artifact_sha256"]) == 64
        assert digest(value) == value["detached_artifact_sha256"]


def test_census_and_schedule_are_complete_and_duplicate_safe():
    fixture = load(CFG / "k12-recovery-fixture-manifest-v1.json")
    randomization = load(CFG / "k12-recovery-randomization-manifest-v1.json")
    assert len(fixture["triplets"]) == 30
    schedule = randomization["ordered_schedule"]
    assert len(schedule) == 90 and len(set(schedule)) == 90
    assert randomization["permutations"] == [["A", "R", "S"], ["A", "S", "R"], ["R", "A", "S"], ["R", "S", "A"], ["S", "A", "R"], ["S", "R", "A"]]
    for stratum in fixture["strata"]:
        cells = [item for item in schedule if item.startswith(f"K12-{stratum}-")]
        assert len(cells) == 18


def test_no_inferential_fields_and_evaluator_boundary():
    protocol = load(BM / "k12_protocol_v1.json")
    result = load(BM / "k12_result_schema_v1.json")
    assert protocol["analysis"] == {"mode": "finite_descriptive_only", "inferential_statistics": False, "results": []}
    assert result["forbidden_fields"]
    fixture = load(CFG / "k12-recovery-fixture-manifest-v1.json")
    assert fixture["evaluator_only_alternatives"]["planner_access"] is False


def test_detached_digest_rejects_tamper():
    document = load(BM / "k12_trace_schema_v1.json")
    declared = document["detached_artifact_sha256"]
    document["schema_version"] = "tampered"
    assert digest(document) != declared
