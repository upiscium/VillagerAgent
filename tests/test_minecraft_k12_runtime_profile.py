import json
import subprocess
from pathlib import Path

import pytest

from benchmarks.minecraft.k12_runtime_profile import (
    HISTORICAL_QUALIFICATION_MANIFEST_PATH,
    HISTORICAL_RUNTIME_PROFILE_PATH,
    HISTORICAL_SOURCE_REVISION,
    K12RuntimeProfileError, load_k12_live_qualification_manifest,
    load_k12_live_qualification_manifest_v1, load_k12_live_runtime_profile,
    load_k12_live_runtime_profile_v1, load_k12_live_source_policy,
    HistoricalSourceContract, ROOT, RUNTIME_PROFILE_PATH, strict_json_load,
    QUALIFICATION_MANIFEST_PATH, SOURCE_POLICY_PATH, build_source_policy,
    build_source_policy_artifact,
    detached_digest,
)
from benchmarks.minecraft.k12_guarded_backend import K12AuthenticatedProfile


def test_live_profile_is_sealed_and_precedes_identity():
    profile = load_k12_live_runtime_profile()
    assert profile["runtime_mode"] == "guarded_real"
    assert profile["sealed"] and profile["before_campaign_identity"]
    assert not profile["fake_backend_allowed"] and not profile["offline_mode_allowed"]
    assert len(profile["schedule"]) == 15
    assert profile.profile_id == "minecraft-k12-live-runtime-profile/2"
    assert len(profile.profile_digest) == 64
    assert profile["bridge_content_sha256"] and profile["endpoint_sha256"]
    assert "execution_revision" not in profile
    assert "source_closure" not in profile
    with pytest.raises(TypeError):
        K12AuthenticatedProfile(profile.profile_id, profile.profile_digest)


def test_live_manifest_schedule_matches_profile():
    profile = load_k12_live_runtime_profile()
    manifest = load_k12_live_qualification_manifest()
    assert manifest["schedule"] == profile["schedule"]


def test_source_policy_is_exact_and_non_circular():
    policy = load_k12_live_source_policy()
    assert policy.identity == "minecraft-k12-live-source-closure-policy/2"
    assert policy.type_for("benchmarks/minecraft/k12_execution_provenance.py") == "python"
    assert len(policy.paths) == len(set(policy.paths))
    assert "benchmarks/minecraft/k12_live_runtime_profile_v1.json" not in policy.paths
    assert all({"path", "git_mode", "semantic_class"} == set(entry)
               for entry in json.loads(SOURCE_POLICY_PATH.read_text(encoding="utf-8"))["paths"])
    assert tuple(
        {"path": entry.path, "git_mode": entry.git_mode,
         "semantic_class": entry.semantic_class}
        for entry in policy.entries
    ) == build_source_policy(ROOT)
    checked_in = json.loads(SOURCE_POLICY_PATH.read_text(encoding="utf-8"))
    generated = build_source_policy_artifact(ROOT)
    assert {key: value for key, value in checked_in.items()
            if key != "detached_artifact_sha256"} == {
                key: value for key, value in generated.items()
                if key != "detached_artifact_sha256"
            }


@pytest.mark.parametrize(
    ("field", "value"),
    (("git_mode", "100755"), ("semantic_class", "contract")),
)
def test_source_policy_typed_record_mismatch_is_rejected(tmp_path, field, value):
    source = json.loads(SOURCE_POLICY_PATH.read_text(encoding="utf-8"))
    source["paths"][0][field] = value
    source["detached_artifact_sha256"] = detached_digest(source)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(K12RuntimeProfileError, match="policy"):
        load_k12_live_source_policy(path)


@pytest.mark.parametrize(
    "removed",
    (
        "benchmarks/minecraft/k12_protocol.py",
        "configs/minecraft/k12-live-environment-policy-v1.json",
    ),
)
def test_source_policy_omitted_import_or_config_is_rejected(tmp_path, removed):
    source = json.loads(SOURCE_POLICY_PATH.read_text(encoding="utf-8"))
    source["paths"] = [item for item in source["paths"] if item["path"] != removed]
    source["detached_artifact_sha256"] = detached_digest(source)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(K12RuntimeProfileError, match="policy"):
        load_k12_live_source_policy(path)


def test_source_policy_unlisted_entry_is_rejected(tmp_path):
    source = json.loads(SOURCE_POLICY_PATH.read_text(encoding="utf-8"))
    source["paths"].append({
        "path": "benchmarks/minecraft/not-a-live-input.py",
        "git_mode": "100644",
        "semantic_class": "python",
    })
    source["paths"].sort(key=lambda item: item["path"])
    source["detached_artifact_sha256"] = detached_digest(source)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(K12RuntimeProfileError, match="policy"):
        load_k12_live_source_policy(path)


def _git_blob(relative: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(ROOT), "cat-file", "blob",
         f"{HISTORICAL_SOURCE_REVISION}:{relative}"],
        check=True, capture_output=True,
    ).stdout


def _git_source_contract() -> HistoricalSourceContract:
    return HistoricalSourceContract.from_reader(_git_blob)


def _materialize_historical_root(root: Path) -> Path:
    profile = strict_json_load(HISTORICAL_RUNTIME_PROFILE_PATH, "historical profile fixture")
    paths = set(profile["source_closure"]) | {
        "benchmarks/minecraft/k12_live_runtime_profile_v1.json",
        "benchmarks/minecraft/eac_runtime.py",
        "benchmarks/minecraft/k12_protocol_v1.json",
        "env/minecraft_server_fast.py",
        "benchmarks/minecraft/k12_live_reset_readback_v1.json",
        "benchmarks/minecraft/k12_live_oracle_v1.json",
        "benchmarks/minecraft/k12_live_stop_policy_v1.json",
        "benchmarks/minecraft/k12_live_qualification_v1.json",
        "configs/minecraft/k12-live-qualification-manifest-v1.json",
        "configs/minecraft/k12-live-containment-probe-v1.json",
    }
    for relative in paths:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_git_blob(relative))
    return root


def test_historical_v1_loader_and_manifest_use_the_immutable_git_revision():
    contract = _git_source_contract()
    profile = load_k12_live_runtime_profile_v1(source_contract=contract)
    manifest = load_k12_live_qualification_manifest_v1(source_contract=contract)
    assert profile.profile_id == "minecraft-k12-live-runtime-profile/1"
    assert manifest["runtime_profile"] == profile.profile_id


def test_historical_default_and_current_root_reject_modified_checkout():
    with pytest.raises(K12RuntimeProfileError, match="(source closure|bridge content)"):
        load_k12_live_runtime_profile_v1()
    with pytest.raises(K12RuntimeProfileError, match="(source closure|bridge content)"):
        load_k12_live_runtime_profile_v1(source_root=ROOT)


def test_historical_materialized_root_authenticates_then_tamper_fails(tmp_path):
    root = _materialize_historical_root(tmp_path / "historical")
    profile = load_k12_live_runtime_profile_v1(source_root=root)
    manifest = load_k12_live_qualification_manifest_v1(source_root=root)
    assert profile.profile_id == "minecraft-k12-live-runtime-profile/1"
    assert manifest["runtime_profile"] == profile.profile_id

    tampered = root / "benchmarks/minecraft/k12_live_fixture.py"
    tampered.write_bytes(tampered.read_bytes() + b"\n# tampered historical bytes\n")
    with pytest.raises(K12RuntimeProfileError, match="source closure"):
        load_k12_live_runtime_profile_v1(source_root=root)


def test_historical_revision_contract_and_cross_version_manifests_reject():
    with pytest.raises(K12RuntimeProfileError, match="revision"):
        HistoricalSourceContract.from_reader(_git_blob, revision="0" * 40)

    contract = _git_source_contract()
    with pytest.raises(K12RuntimeProfileError):
        load_k12_live_runtime_profile(HISTORICAL_RUNTIME_PROFILE_PATH)
    with pytest.raises(K12RuntimeProfileError):
        load_k12_live_qualification_manifest_v1(
            QUALIFICATION_MANIFEST_PATH, source_contract=contract,
        )
    with pytest.raises(K12RuntimeProfileError):
        load_k12_live_qualification_manifest(HISTORICAL_QUALIFICATION_MANIFEST_PATH)


def test_historical_source_contract_rejects_unsafe_paths_and_nonbytes(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "safe.txt").write_bytes(b"safe")
    contract = HistoricalSourceContract.from_root(root)
    assert contract.read("safe.txt") == b"safe"
    with pytest.raises(K12RuntimeProfileError):
        contract.read("../safe.txt")

    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    (root / "escape.txt").symlink_to(outside)
    with pytest.raises(K12RuntimeProfileError, match="symlink|escaped"):
        contract.read("escape.txt")

    bad = HistoricalSourceContract.from_reader(lambda _relative: "not bytes")
    with pytest.raises(K12RuntimeProfileError, match="bytes"):
        bad.read("safe.txt")


def test_historical_manifest_tamper_is_rejected(tmp_path):
    source = json.loads(HISTORICAL_QUALIFICATION_MANIFEST_PATH.read_text(encoding="utf-8"))
    source["schedule"][0] = "tampered"
    path = tmp_path / "historical-manifest.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(K12RuntimeProfileError, match="(digest|historical source revision)"):
        load_k12_live_qualification_manifest_v1(
            path, source_contract=_git_source_contract(),
        )


def test_digest_is_detached_and_tamper_fails(tmp_path):
    source = json.loads(RUNTIME_PROFILE_PATH.read_text(encoding="utf-8"))
    source["runtime_mode"] = "fake"
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(K12RuntimeProfileError, match="digest"):
        load_k12_live_runtime_profile(path)

def test_duplicate_json_keys_fail_before_authentication(tmp_path):
    path=tmp_path/"duplicate.json"
    path.write_text('{"artifact_id":"a","artifact_id":"b","detached_artifact_sha256":"' + '0'*64 + '"}',encoding="utf-8")
    with pytest.raises(K12RuntimeProfileError,match="duplicate"):
        load_k12_live_runtime_profile(path)
