import json
from copy import deepcopy

import pytest

from benchmarks.minecraft.k12_runtime_profile import (
    K12RuntimeProfileError, load_k12_live_qualification_manifest,
    load_k12_live_runtime_profile, RUNTIME_PROFILE_PATH,
)
from benchmarks.minecraft.k12_guarded_backend import K12AuthenticatedProfile


def test_live_profile_is_sealed_and_precedes_identity():
    profile = load_k12_live_runtime_profile()
    assert profile["runtime_mode"] == "guarded_real"
    assert profile["sealed"] and profile["before_campaign_identity"]
    assert not profile["fake_backend_allowed"] and not profile["offline_mode_allowed"]
    assert len(profile["schedule"]) == 15
    assert profile.profile_id == "minecraft-k12-live-runtime-profile/1"
    assert len(profile.profile_digest) == 64
    assert profile["bridge_content_sha256"] and profile["endpoint_sha256"]
    with pytest.raises(TypeError):
        K12AuthenticatedProfile(profile.profile_id, profile.profile_digest)


def test_live_manifest_schedule_matches_profile():
    profile = load_k12_live_runtime_profile()
    manifest = load_k12_live_qualification_manifest()
    assert manifest["schedule"] == profile["schedule"]


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
