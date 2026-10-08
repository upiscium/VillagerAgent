import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from benchmarks.minecraft.k12_sample_identity import (
    FINAL_COORDINATES,
    FIXTURE_DIGEST,
    K12SampleIdentityV1,
    K12SamplePlan,
    PHASE_FINAL_CELL,
    PHASE_QUALIFICATION_CELL,
    PHASE_QUALIFICATION_PROBE,
    PROBE_COORDINATES,
    PROBE_SCHEDULE_DIGEST,
    PROBE_SCHEDULE_IDENTITY,
    PROTOCOL_IDENTITY,
    QUALIFICATION_COORDINATES,
    QUALIFICATION_SCHEDULE_DIGEST,
    QUALIFICATION_SCHEDULE_IDENTITY,
    RANDOMIZATION_DIGEST,
    RANDOMIZATION_SCHEDULE_IDENTITY,
    SAMPLE_IDENTITY_ARTIFACT,
    SAMPLE_PLAN_ARTIFACT,
    canonical_json_bytes,
    sample_plan_digest_for,
)

ROOT = Path(__file__).resolve().parents[1]
_FINAL_FIELDS = (
    "ordinal", "cell_id", "triplet_id", "stratum", "template", "seed", "arm",
)


def _plain_final_coordinates():
    return [{name: coordinate[name] for name in _FINAL_FIELDS}
            for coordinate in FINAL_COORDINATES]


def _plan_payload(*, study="study-instance-α", replicate="replicate-1"):
    payload = {
        "artifact": SAMPLE_PLAN_ARTIFACT,
        "study_instance_identity": study,
        "replicate_designation": replicate,
        "protocol_identity": PROTOCOL_IDENTITY,
        "qualification": {
            "schedule_identity": QUALIFICATION_SCHEDULE_IDENTITY,
            "schedule_digest": QUALIFICATION_SCHEDULE_DIGEST,
            "ordered_coordinates": list(QUALIFICATION_COORDINATES),
        },
        "probe": {
            "schedule_identity": PROBE_SCHEDULE_IDENTITY,
            "schedule_digest": PROBE_SCHEDULE_DIGEST,
            "ordered_coordinates": list(PROBE_COORDINATES),
        },
        "final": {
            "randomization_digest": RANDOMIZATION_DIGEST,
            "fixture_digest": FIXTURE_DIGEST,
            "ordered_coordinates": _plain_final_coordinates(),
        },
        "retry": "forbidden",
        "resume": "forbidden",
        "replacement": "forbidden",
    }
    payload["sample_plan_digest"] = sample_plan_digest_for(payload)
    return payload


def _plan():
    return K12SamplePlan.from_mapping(_plan_payload())


def _detached_digest(document):
    body = {key: value for key, value in document.items()
            if key != "detached_artifact_sha256"}
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def test_frozen_phase_censuses_order_and_uniqueness():
    plan = _plan()
    qualification = plan.identities(PHASE_QUALIFICATION_CELL)
    probes = plan.identities(PHASE_QUALIFICATION_PROBE)
    final = plan.identities(PHASE_FINAL_CELL)

    assert len(qualification) == 15
    assert tuple(item.coordinate for item in qualification) == QUALIFICATION_COORDINATES
    assert len({item.coordinate for item in qualification}) == 15
    assert len(probes) == 4
    assert tuple(item.coordinate for item in probes) == PROBE_COORDINATES
    assert len({item.coordinate for item in probes}) == 4
    assert len(final) == 90
    assert tuple(item.coordinate for item in final) == tuple(_plain_final_coordinates())
    assert len({item.coordinate["cell_id"] for item in final}) == 90

    all_identities = qualification + probes + final
    assert len(all_identities) == 109
    assert len({item.sample_id for item in all_identities}) == 109
    assert {item.phase for item in all_identities} == {
        PHASE_QUALIFICATION_CELL, PHASE_QUALIFICATION_PROBE, PHASE_FINAL_CELL,
    }
    assert len({item.coordinate for item in qualification}.intersection(
        {item.coordinate for item in probes})) == 0


def test_plan_and_identity_projections_are_closed_fresh_and_immutable():
    payload = _plan_payload()
    plan = K12SamplePlan.from_mapping(payload)
    first = plan.to_dict()
    first["qualification"]["ordered_coordinates"][0] = "altered"
    first["final"]["ordered_coordinates"][0]["cell_id"] = "altered"
    assert plan.to_dict() == payload

    with pytest.raises((AttributeError, TypeError)):
        plan.study_instance_identity = "changed"

    identity = plan.identities(PHASE_FINAL_CELL)[0]
    identity_view = identity.to_dict()
    assert set(identity_view) == {
        "artifact", "sample_plan_digest", "phase", "schedule_identity",
        "schedule_digest", "coordinate",
    }
    assert identity_view["artifact"] == SAMPLE_IDENTITY_ARTIFACT
    assert "sample_id" not in identity_view
    identity_view["coordinate"]["cell_id"] = "tampered"
    assert identity.to_dict()["coordinate"]["cell_id"] == "K12-S1-T1-N1-A"
    assert identity.sample_plan_digest == plan.sample_plan_digest
    assert K12SampleIdentityV1.from_mapping(identity.to_dict(), plan=plan).sample_id == identity.sample_id


def test_identity_sample_id_is_canonical_sha256_and_has_no_execution_bindings():
    plan = _plan()
    identity = plan.identities(PHASE_QUALIFICATION_CELL)[0]
    expected = hashlib.sha256(canonical_json_bytes(identity.to_dict())).hexdigest()
    assert identity.sample_id == expected
    assert len(identity.sample_id) == 64
    assert set(identity.to_dict()) == {
        "artifact", "sample_plan_digest", "phase", "schedule_identity",
        "schedule_digest", "coordinate",
    }

    # Execution-context changes are intentionally outside the identity API and
    # cannot enter its canonical projection.
    execution_a = {
        "campaign_id": "campaign-a", "target": "target-a", "pid": 101, "time": 1,
        "source_revision": "source-a", "profile_digest": "profile-a",
        "reservation_id": "reservation-a", "output_root": "root-a", "nonce": "nonce-a",
        "authorization_expiry": 100,
    }
    execution_b = {
        "campaign_id": "campaign-b", "target": "target-b", "pid": 202, "time": 2,
        "source_revision": "source-b", "profile_digest": "profile-b",
        "reservation_id": "reservation-b", "output_root": "root-b", "nonce": "nonce-b",
        "authorization_expiry": 200,
    }
    assert execution_a != execution_b
    projected_ids = []
    for execution_bindings in (execution_a, execution_b):
        envelope = {
            "sample_identity": identity.to_dict(),
            "execution_bindings": execution_bindings,
        }
        projected_ids.append(K12SampleIdentityV1.from_mapping(
            envelope["sample_identity"], plan=plan,
        ).sample_id)
    assert projected_ids == [identity.sample_id, identity.sample_id]
    with pytest.raises(ValueError):
        K12SampleIdentityV1.from_mapping(
            {**identity.to_dict(), **execution_b}, plan=plan,
        )


@pytest.mark.parametrize(
    "study,replicate",
    [
        ("study-instance-β", "replicate-1"),
        ("study-instance-α", "replicate-2"),
    ],
)
def test_study_or_replicate_changes_plan_and_all_sample_identities(study, replicate):
    baseline = _plan()
    changed = K12SamplePlan.from_mapping(_plan_payload(study=study, replicate=replicate))
    assert changed.sample_plan_digest != baseline.sample_plan_digest
    baseline_ids = tuple(item.sample_id for item in baseline.identities(PHASE_FINAL_CELL))
    changed_ids = tuple(item.sample_id for item in changed.identities(PHASE_FINAL_CELL))
    assert all(left != right for left, right in zip(baseline_ids, changed_ids, strict=True))


def test_canonical_golden_bytes_and_frozen_schedule_constants():
    assert canonical_json_bytes({}) == b"{}"
    assert hashlib.sha256(canonical_json_bytes({})).hexdigest() == (
        "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    )
    assert canonical_json_bytes({"é": "雪", "a": [True, None, 1]}) == (
        b'{"a":[true,null,1],"\xc3\xa9":"\xe9\x9b\xaa"}'
    )
    assert SAMPLE_PLAN_ARTIFACT == "minecraft-k12-sample-plan/1"
    assert SAMPLE_IDENTITY_ARTIFACT == "minecraft-k12-sample-identity/1"
    assert QUALIFICATION_SCHEDULE_IDENTITY == (
        "minecraft-k12-live-runtime-qualification-schedule/1"
    )
    assert QUALIFICATION_SCHEDULE_DIGEST == (
        "330f41ae0bb2e817e4d22d348fc4dd73d8f0838f144eb9aa654494b832e760f7"
    )
    assert PROBE_SCHEDULE_IDENTITY == "minecraft-k12-live-containment-probe/1"
    assert PROBE_SCHEDULE_DIGEST == (
        "824eb0374a286b13077945cf0b9e289f447cfda78d72d27ed2ba0febab2db871"
    )
    assert RANDOMIZATION_SCHEDULE_IDENTITY == (
        "minecraft-k12-recovery-randomization-manifest/1"
    )
    assert RANDOMIZATION_DIGEST == (
        "116e568b5c5f87ac957bf8ceea25ce9443c2370012a6ec2f5f180e10602c3693"
    )
    assert FIXTURE_DIGEST == (
        "a321af43ed18c6ef3dae4e5d1f64d8cffdd36c4472f6b9510bc60e5c0a588d1f"
    )


def test_fixed_plan_and_three_phase_sample_id_golden_vectors():
    plan = _plan()
    assert plan.sample_plan_digest == (
        "1763f35b9364606bf640a28aa9f771a1481de5668209a867804a9cde9c50d109"
    )
    assert plan.identities(PHASE_QUALIFICATION_CELL)[0].sample_id == (
        "c588294c4bc42677e51624c6b0caf3d0d57b051d92ac6013b24616e09d46be90"
    )
    assert plan.identities(PHASE_QUALIFICATION_PROBE)[0].sample_id == (
        "f2d252697a65649b8341c507a0c7f4533b9c7fd9d04fcba07342b071ba263b89"
    )
    assert plan.identities(PHASE_FINAL_CELL)[0].sample_id == (
        "388a526e64ece20bbe638785ce1a4217b8ea3b802ee76d430dd32130f6f1a74c"
    )


def test_plan_digest_is_canonical_full_payload_except_own_digest():
    payload = _plan_payload()
    declared = payload.pop("sample_plan_digest")
    assert sample_plan_digest_for(payload) == declared
    payload["sample_plan_digest"] = declared
    plan = K12SamplePlan.from_mapping(payload)
    assert plan.sample_plan_digest == declared
    assert plan.to_dict() == payload
    assert hashlib.sha256(canonical_json_bytes({
        key: value for key, value in payload.items() if key != "sample_plan_digest"
    })).hexdigest() == declared


def test_checked_in_manifests_match_embedded_randomization_and_fixture_domains():
    config = ROOT / "configs" / "minecraft"
    qualification = json.loads(
        (config / "k12-live-qualification-manifest-v1.json").read_text(encoding="utf-8")
    )
    probe = json.loads(
        (config / "k12-live-containment-probe-v1.json").read_text(encoding="utf-8")
    )
    randomization = json.loads(
        (config / "k12-recovery-randomization-manifest-v1.json").read_text(encoding="utf-8")
    )
    fixture = json.loads(
        (config / "k12-recovery-fixture-manifest-v1.json").read_text(encoding="utf-8")
    )

    assert qualification["schedule_identity"] == QUALIFICATION_SCHEDULE_IDENTITY
    assert qualification["cell_count"] == 15
    assert qualification["schedule"] == list(QUALIFICATION_COORDINATES)
    assert qualification["detached_artifact_sha256"] == _detached_digest(qualification)
    # The frozen #588 schedule commitment is deliberately distinct from the
    # detached digest of the larger qualification manifest.
    assert QUALIFICATION_SCHEDULE_DIGEST != qualification["detached_artifact_sha256"]
    assert probe["identity"] == PROBE_SCHEDULE_IDENTITY
    assert probe["probes"] == list(PROBE_COORDINATES)
    assert _detached_digest(probe) == PROBE_SCHEDULE_DIGEST
    assert randomization["schema_version"] == RANDOMIZATION_SCHEDULE_IDENTITY
    assert randomization["detached_artifact_sha256"] == RANDOMIZATION_DIGEST
    assert _detached_digest(randomization) == RANDOMIZATION_DIGEST
    assert [item["cell_id"] for item in FINAL_COORDINATES] == randomization["ordered_schedule"]
    assert fixture["detached_artifact_sha256"] == FIXTURE_DIGEST
    assert _detached_digest(fixture) == FIXTURE_DIGEST
    assert len(fixture["triplets"]) == 30

    triplets = {item["triplet_id"]: item for item in fixture["triplets"]}
    coordinates = _plain_final_coordinates()
    assert len(coordinates) == 90
    for coordinate in coordinates:
        triplet = triplets[coordinate["triplet_id"]]
        assert coordinate["stratum"] == triplet["stratum"]
        assert coordinate["template"] == triplet["template"]
        assert coordinate["seed"] == triplet["seed"]
    assert [item["ordinal"] for item in coordinates] == list(range(1, 91))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(extra="no"),
        lambda payload: payload["qualification"].update(extra="no"),
        lambda payload: payload["probe"].update(extra="no"),
        lambda payload: payload["final"].update(extra="no"),
        lambda payload: payload["final"]["ordered_coordinates"][0].update(extra="no"),
    ],
)
def test_plan_rejects_unknown_fields_at_every_schema_level(mutate):
    payload = _plan_payload()
    mutate(payload)
    with pytest.raises((TypeError, ValueError)):
        K12SamplePlan.from_mapping(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.__setitem__("protocol_identity", "other/1"),
        lambda payload: payload["qualification"].__setitem__("schedule_digest", "0" * 64),
        lambda payload: payload["qualification"]["ordered_coordinates"].reverse(),
        lambda payload: payload["probe"].__setitem__("schedule_identity", "other/1"),
        lambda payload: payload["probe"]["ordered_coordinates"].append("P5"),
        lambda payload: payload["final"].__setitem__("fixture_digest", "0" * 64),
        lambda payload: payload["final"]["ordered_coordinates"].pop(),
        lambda payload: payload.__setitem__("retry", "allowed"),
        lambda payload: payload.__setitem__("resume", "allowed"),
        lambda payload: payload.__setitem__("replacement", "allowed"),
    ],
)
def test_plan_rejects_wrong_fields_and_schedule_or_policy_domains(mutate):
    payload = _plan_payload()
    mutate(payload)
    with pytest.raises((TypeError, ValueError)):
        K12SamplePlan.from_mapping(payload)


@pytest.mark.parametrize("field", ["ordinal", "template", "seed"])
def test_final_coordinate_integer_fields_reject_bool(field):
    payload = _plan_payload()
    payload["final"]["ordered_coordinates"][0][field] = True
    with pytest.raises(TypeError):
        sample_plan_digest_for(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(sample_id="forged"),
        lambda value: value.update(execution_revision="bound"),
        lambda value: value.__setitem__("artifact", "other/1"),
        lambda value: value.__setitem__("sample_plan_digest", "0" * 64),
        lambda value: value.__setitem__("phase", "unknown-phase"),
        lambda value: value.__setitem__("schedule_identity", "other/1"),
        lambda value: value.__setitem__("schedule_digest", "0" * 64),
        lambda value: value.__setitem__("coordinate", "not-scheduled"),
    ],
)
def test_identity_from_mapping_rejects_forged_or_unknown_authority(mutate):
    plan = _plan()
    value = plan.identities(PHASE_QUALIFICATION_CELL)[0].to_dict()
    mutate(value)
    with pytest.raises((TypeError, ValueError)):
        K12SampleIdentityV1.from_mapping(value, plan=plan)


def test_identity_membership_is_phase_exact_and_final_coordinate_is_closed():
    plan = _plan()
    with pytest.raises(ValueError):
        K12SampleIdentityV1(plan, PHASE_QUALIFICATION_CELL, "P1")
    with pytest.raises(ValueError):
        K12SampleIdentityV1(plan, PHASE_QUALIFICATION_PROBE, QUALIFICATION_COORDINATES[0])
    with pytest.raises(ValueError):
        K12SampleIdentityV1(plan, PHASE_FINAL_CELL, {**_plain_final_coordinates()[0], "extra": 1})
    with pytest.raises(TypeError):
        K12SampleIdentityV1(
            plan,
            PHASE_FINAL_CELL,
            {**_plain_final_coordinates()[0], "ordinal": True},
        )


def test_canonical_json_rejects_non_json_aliases_and_non_finite_values():
    class DuplicateItems(dict):
        def items(self):
            return [("key", 1), ("key", 2)]

    with pytest.raises(ValueError):
        canonical_json_bytes(DuplicateItems(key=1))
    with pytest.raises(TypeError):
        canonical_json_bytes({1: "not a JSON object key"})
    with pytest.raises(TypeError):
        canonical_json_bytes((1, 2))
    with pytest.raises(ValueError):
        canonical_json_bytes(float("nan"))
    with pytest.raises(ValueError):
        canonical_json_bytes(float("inf"))


def test_plan_payload_inputs_and_outputs_do_not_alias_caller_state():
    source = _plan_payload()
    original = copy.deepcopy(source)
    plan = K12SamplePlan.from_mapping(source)
    source["qualification"]["ordered_coordinates"][0] = "mutated"
    source["final"]["ordered_coordinates"][0]["ordinal"] = 77
    assert plan.to_dict() == original


def test_plan_rejects_bad_digest_and_missing_required_fields():
    payload = _plan_payload()
    payload["sample_plan_digest"] = "0" * 64
    with pytest.raises(ValueError, match="digest"):
        K12SamplePlan.from_mapping(payload)

    payload = _plan_payload()
    del payload["sample_plan_digest"]
    with pytest.raises(ValueError, match="required"):
        K12SamplePlan.from_mapping(payload)


def test_sample_identity_is_deterministic_across_processes():
    payload = _plan_payload()
    encoded_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    script = r'''
import json
import sys
from benchmarks.minecraft.k12_sample_identity import K12SamplePlan, PHASE_FINAL_CELL
plan = K12SamplePlan.from_mapping(json.loads(sys.argv[1]))
identity = plan.identities(PHASE_FINAL_CELL)[37]
print(json.dumps({"plan": plan.sample_plan_digest, "sample": identity.sample_id,
                  "identity": identity.to_dict()}, ensure_ascii=False,
                 sort_keys=True, separators=(",", ":")))
'''
    environment = os.environ.copy()
    old_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(ROOT) if not old_path else f"{ROOT}{os.pathsep}{old_path}"
    results = []
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-c", script, encoded_payload],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=True,
        )
        results.append(result.stdout.strip())
    assert results[0] == results[1]
    parsed = json.loads(results[0])
    in_process = K12SamplePlan.from_mapping(payload).identities(PHASE_FINAL_CELL)[37]
    assert parsed["plan"] == in_process.sample_plan_digest
    assert parsed["sample"] == in_process.sample_id
    assert parsed["identity"] == in_process.to_dict()
