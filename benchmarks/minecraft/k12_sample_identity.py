"""Durable, execution-independent identities for the K12 sample schedule.

This module intentionally embeds only the frozen schedule authority needed to
name samples.  It does not read manifests, inspect the current directory, or
bind sample identities to runtime/execution state.  The parent must separately
authenticate the plan, including study/replicate designations; a new campaign
label is never a new authorized replication plan by itself.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

SAMPLE_PLAN_ARTIFACT = "minecraft-k12-sample-plan/1"
SAMPLE_IDENTITY_ARTIFACT = "minecraft-k12-sample-identity/1"
PROTOCOL_IDENTITY = "minecraft-eac-k12-controlled-recovery/1"

PHASE_QUALIFICATION_CELL = "qualification_cell"
PHASE_QUALIFICATION_PROBE = "qualification_probe"
PHASE_FINAL_CELL = "final_cell"

QUALIFICATION_SCHEDULE_IDENTITY = (
    "minecraft-k12-live-runtime-qualification-schedule/1"
)
QUALIFICATION_SCHEDULE_DIGEST = (
    "330f41ae0bb2e817e4d22d348fc4dd73d8f0838f144eb9aa654494b832e760f7"
)
PROBE_SCHEDULE_IDENTITY = "minecraft-k12-live-containment-probe/1"
PROBE_SCHEDULE_DIGEST = (
    "824eb0374a286b13077945cf0b9e289f447cfda78d72d27ed2ba0febab2db871"
)
RANDOMIZATION_SCHEDULE_IDENTITY = "minecraft-k12-recovery-randomization-manifest/1"
RANDOMIZATION_DIGEST = (
    "116e568b5c5f87ac957bf8ceea25ce9443c2370012a6ec2f5f180e10602c3693"
)
FIXTURE_DIGEST = (
    "a321af43ed18c6ef3dae4e5d1f64d8cffdd36c4472f6b9510bc60e5c0a588d1f"
)

QUALIFICATION_COORDINATES = (
    "K12Q-S1-T1-N1-A",
    "K12Q-S1-T1-N1-R",
    "K12Q-S1-T1-N1-S",
    "K12Q-S2-T1-N1-R",
    "K12Q-S2-T1-N1-S",
    "K12Q-S2-T1-N1-A",
    "K12Q-S3-T1-N1-S",
    "K12Q-S3-T1-N1-A",
    "K12Q-S3-T1-N1-R",
    "K12Q-S4-T1-N1-A",
    "K12Q-S4-T1-N1-S",
    "K12Q-S4-T1-N1-R",
    "K12Q-S5-T1-N1-R",
    "K12Q-S5-T1-N1-A",
    "K12Q-S5-T1-N1-S",
)
PROBE_COORDINATES = ("P1", "P2", "P3", "P4")

_FINAL_COORDINATE_FIELDS = (
    "ordinal", "cell_id", "triplet_id", "stratum", "template", "seed", "arm",
)
_STRATA = ("S1", "S2", "S3", "S4", "S5")
# Frozen arm order copied from the canonical ordered_schedule: template, seed,
# then arms.  It is repeated in this exact order for each stratum.
_FINAL_TEMPLATE_SEED_ARM_ORDER = (
    (1, 1, ("A", "R", "S")),
    (1, 2, ("A", "S", "R")),
    (1, 3, ("R", "A", "S")),
    (2, 1, ("R", "S", "A")),
    (2, 2, ("S", "A", "R")),
    (2, 3, ("S", "R", "A")),
)


def _make_final_coordinates() -> tuple[Mapping[str, Any], ...]:
    coordinates: list[Mapping[str, Any]] = []
    for stratum in _STRATA:
        for template, seed, arms in _FINAL_TEMPLATE_SEED_ARM_ORDER:
            triplet_id = f"K12-{stratum}-T{template}-N{seed}"
            for arm in arms:
                ordinal = len(coordinates) + 1
                coordinates.append(MappingProxyType({
                    "ordinal": ordinal,
                    "cell_id": f"{triplet_id}-{arm}",
                    "triplet_id": triplet_id,
                    "stratum": stratum,
                    "template": template,
                    "seed": seed,
                    "arm": arm,
                }))
    return tuple(coordinates)


FINAL_COORDINATES = _make_final_coordinates()

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PLAN_FIELDS = frozenset({
    "artifact", "study_instance_identity", "replicate_designation",
    "protocol_identity", "qualification", "probe", "final", "retry",
    "resume", "replacement", "sample_plan_digest",
})
_IDENTITY_FIELDS = frozenset({
    "artifact", "sample_plan_digest", "phase", "schedule_identity",
    "schedule_digest", "coordinate",
})


def _copy_json(value: Any, *, path: str = "$") -> Any:
    """Validate and detach a JSON value into plain built-in containers."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"non-finite JSON number at {path}")
        return value
    if isinstance(value, Mapping):
        items = list(value.items())
        keys = [key for key, _ in items]
        if any(type(key) is not str for key in keys):
            raise TypeError(f"JSON object keys must be strings at {path}")
        if len(keys) != len(set(keys)):
            raise ValueError(f"duplicate JSON object key at {path}")
        return {
            key: _copy_json(item, path=f"{path}.{key}")
            for key, item in items
        }
    if type(value) is list:
        return [
            _copy_json(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise TypeError(f"not a JSON value at {path}: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON deterministically using the sample identity encoding."""
    detached = _copy_json(value)
    text = json.dumps(
        detached,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return text.encode("utf-8")


def _require_exact_keys(value: Any, keys: frozenset[str], label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be a JSON object")
    actual = set(value)
    if actual != keys:
        unknown = sorted(actual - keys)
        missing = sorted(keys - actual)
        raise ValueError(f"{label} fields mismatch (unknown={unknown}, missing={missing})")
    return value


def _require_nonempty_string(value: Any, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _require_digest(value: Any, label: str) -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _final_coordinate_dict(coordinate: Mapping[str, Any]) -> dict[str, Any]:
    return {name: coordinate[name] for name in _FINAL_COORDINATE_FIELDS}


def _validate_final_coordinate(value: Any, *, label: str) -> dict[str, Any]:
    coordinate = _require_exact_keys(value, frozenset(_FINAL_COORDINATE_FIELDS), label)
    for name in ("ordinal", "template", "seed"):
        if type(coordinate[name]) is not int:
            raise TypeError(f"{label}.{name} must be an integer (not bool)")
        if coordinate[name] <= 0:
            raise ValueError(f"{label}.{name} must be positive")
    for name in ("cell_id", "triplet_id", "stratum", "arm"):
        _require_nonempty_string(coordinate[name], f"{label}.{name}")
    return coordinate


def _expected_final_coordinates() -> list[dict[str, Any]]:
    return [_final_coordinate_dict(item) for item in FINAL_COORDINATES]


def _plan_body(study_instance_identity: str, replicate_designation: str) -> dict[str, Any]:
    return {
        "artifact": SAMPLE_PLAN_ARTIFACT,
        "study_instance_identity": study_instance_identity,
        "replicate_designation": replicate_designation,
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
            "ordered_coordinates": _expected_final_coordinates(),
        },
        "retry": "forbidden",
        "resume": "forbidden",
        "replacement": "forbidden",
    }


def _validated_plan_body(
    payload: Any, *, require_sample_plan_digest: bool,
) -> tuple[dict[str, Any], str]:
    copied = _copy_json(payload)
    if type(copied) is not dict:
        raise TypeError("sample plan must be a JSON object")
    has_digest = "sample_plan_digest" in copied
    if require_sample_plan_digest and not has_digest:
        raise ValueError("sample_plan_digest is required")
    allowed = _PLAN_FIELDS if has_digest else _PLAN_FIELDS - {"sample_plan_digest"}
    _require_exact_keys(copied, allowed, "sample plan")

    if copied["artifact"] != SAMPLE_PLAN_ARTIFACT:
        raise ValueError("sample plan artifact mismatch")
    if copied["protocol_identity"] != PROTOCOL_IDENTITY:
        raise ValueError("sample plan protocol identity mismatch")
    study = _require_nonempty_string(
        copied["study_instance_identity"], "study_instance_identity",
    )
    replicate = _require_nonempty_string(
        copied["replicate_designation"], "replicate_designation",
    )

    qualification = _require_exact_keys(
        copied["qualification"],
        frozenset({"schedule_identity", "schedule_digest", "ordered_coordinates"}),
        "qualification",
    )
    if qualification["schedule_identity"] != QUALIFICATION_SCHEDULE_IDENTITY:
        raise ValueError("qualification schedule identity mismatch")
    if _require_digest(qualification["schedule_digest"], "qualification.schedule_digest") != QUALIFICATION_SCHEDULE_DIGEST:
        raise ValueError("qualification schedule digest mismatch")
    q_coordinates = qualification["ordered_coordinates"]
    if type(q_coordinates) is not list or any(type(item) is not str for item in q_coordinates):
        raise TypeError("qualification.ordered_coordinates must be an array of strings")
    if q_coordinates != list(QUALIFICATION_COORDINATES):
        raise ValueError("qualification coordinates do not match the frozen schedule")

    probe = _require_exact_keys(
        copied["probe"],
        frozenset({"schedule_identity", "schedule_digest", "ordered_coordinates"}),
        "probe",
    )
    if probe["schedule_identity"] != PROBE_SCHEDULE_IDENTITY:
        raise ValueError("probe schedule identity mismatch")
    if _require_digest(probe["schedule_digest"], "probe.schedule_digest") != PROBE_SCHEDULE_DIGEST:
        raise ValueError("probe schedule digest mismatch")
    p_coordinates = probe["ordered_coordinates"]
    if type(p_coordinates) is not list or any(type(item) is not str for item in p_coordinates):
        raise TypeError("probe.ordered_coordinates must be an array of strings")
    if p_coordinates != list(PROBE_COORDINATES):
        raise ValueError("probe coordinates do not match the frozen schedule")

    final = _require_exact_keys(
        copied["final"],
        frozenset({"randomization_digest", "fixture_digest", "ordered_coordinates"}),
        "final",
    )
    if _require_digest(final["randomization_digest"], "final.randomization_digest") != RANDOMIZATION_DIGEST:
        raise ValueError("final randomization digest mismatch")
    if _require_digest(final["fixture_digest"], "final.fixture_digest") != FIXTURE_DIGEST:
        raise ValueError("final fixture digest mismatch")
    f_coordinates = final["ordered_coordinates"]
    if type(f_coordinates) is not list:
        raise TypeError("final.ordered_coordinates must be an array")
    for index, coordinate in enumerate(f_coordinates):
        _validate_final_coordinate(coordinate, label=f"final.ordered_coordinates[{index}]")
    expected_final = _expected_final_coordinates()
    if canonical_json_bytes(f_coordinates) != canonical_json_bytes(expected_final):
        raise ValueError("final coordinates do not match the frozen randomization schedule")

    for name in ("retry", "resume", "replacement"):
        if copied[name] != "forbidden":
            raise ValueError(f"{name} must be forbidden")

    body = _plan_body(study, replicate)
    digest = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    if has_digest:
        declared_digest = _require_digest(copied["sample_plan_digest"], "sample_plan_digest")
        if declared_digest != digest:
            raise ValueError("sample plan digest mismatch")
    return body, digest


def sample_plan_digest_for(payload: Any) -> str:
    """Return the validated canonical plan digest.

    The plan's own ``sample_plan_digest`` may be omitted while initially
    computing the digest; if present it must already match.
    """
    _, digest = _validated_plan_body(payload, require_sample_plan_digest=False)
    return digest


@dataclass(frozen=True, slots=True, init=False)
class K12SamplePlan:
    """Immutable value object for the closed, frozen K12 sample plan."""

    study_instance_identity: str
    replicate_designation: str
    sample_plan_digest: str

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("construct K12SamplePlan with from_mapping()")

    @classmethod
    def from_mapping(cls, payload: Any) -> "K12SamplePlan":
        body, digest = _validated_plan_body(payload, require_sample_plan_digest=True)
        plan = object.__new__(cls)
        object.__setattr__(plan, "study_instance_identity", body["study_instance_identity"])
        object.__setattr__(plan, "replicate_designation", body["replicate_designation"])
        object.__setattr__(plan, "sample_plan_digest", digest)
        return plan

    def to_dict(self) -> dict[str, Any]:
        result = _plan_body(self.study_instance_identity, self.replicate_designation)
        result["sample_plan_digest"] = self.sample_plan_digest
        return result

    def identities(self, phase: str) -> tuple["K12SampleIdentityV1", ...]:
        _, _, coordinates = _phase_schedule(phase)
        return tuple(K12SampleIdentityV1(self, phase, coordinate) for coordinate in coordinates)


def _phase_schedule(phase: Any) -> tuple[str, str, tuple[Any, ...]]:
    if type(phase) is not str:
        raise TypeError("phase must be a string")
    if phase == PHASE_QUALIFICATION_CELL:
        return (
            QUALIFICATION_SCHEDULE_IDENTITY,
            QUALIFICATION_SCHEDULE_DIGEST,
            QUALIFICATION_COORDINATES,
        )
    if phase == PHASE_QUALIFICATION_PROBE:
        return PROBE_SCHEDULE_IDENTITY, PROBE_SCHEDULE_DIGEST, PROBE_COORDINATES
    if phase == PHASE_FINAL_CELL:
        return RANDOMIZATION_SCHEDULE_IDENTITY, RANDOMIZATION_DIGEST, FINAL_COORDINATES
    raise ValueError(f"unknown sample phase: {phase!r}")


def _normalize_identity_coordinate(phase: str, coordinate: Any) -> str | tuple[Any, ...]:
    _, _, allowed_coordinates = _phase_schedule(phase)
    if phase in (PHASE_QUALIFICATION_CELL, PHASE_QUALIFICATION_PROBE):
        if type(coordinate) is not str:
            raise TypeError("qualification/probe coordinate must be a string")
        if coordinate not in allowed_coordinates:
            raise ValueError("coordinate is not a member of the requested phase schedule")
        return coordinate

    copied = _copy_json(coordinate, path="coordinate")
    _validate_final_coordinate(copied, label="coordinate")
    encoded = canonical_json_bytes(copied)
    for expected in allowed_coordinates:
        expected_dict = _final_coordinate_dict(expected)
        if encoded == canonical_json_bytes(expected_dict):
            return tuple(copied[name] for name in _FINAL_COORDINATE_FIELDS)
    raise ValueError("coordinate is not a member of the requested phase schedule")


@dataclass(frozen=True, slots=True, init=False)
class K12SampleIdentityV1:
    """A phase- and plan-bound identity for exactly one scheduled sample."""

    _sample_plan_digest: str
    phase: str
    schedule_identity: str
    schedule_digest: str
    _coordinate: str | tuple[Any, ...]

    def __init__(self, plan: K12SamplePlan, phase: str, coordinate: Any) -> None:
        if not isinstance(plan, K12SamplePlan):
            raise TypeError("a validated K12SamplePlan is required")
        schedule_identity, schedule_digest, _ = _phase_schedule(phase)
        normalized_coordinate = _normalize_identity_coordinate(phase, coordinate)
        object.__setattr__(self, "_sample_plan_digest", plan.sample_plan_digest)
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "schedule_identity", schedule_identity)
        object.__setattr__(self, "schedule_digest", schedule_digest)
        object.__setattr__(self, "_coordinate", normalized_coordinate)

    @property
    def sample_plan_digest(self) -> str:
        return self._sample_plan_digest

    @property
    def coordinate(self) -> str | dict[str, Any]:
        if self.phase != PHASE_FINAL_CELL:
            return self._coordinate  # type: ignore[return-value]
        assert isinstance(self._coordinate, tuple)
        return dict(zip(_FINAL_COORDINATE_FIELDS, self._coordinate, strict=True))

    @property
    def sample_id(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.to_dict())).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact": SAMPLE_IDENTITY_ARTIFACT,
            "sample_plan_digest": self.sample_plan_digest,
            "phase": self.phase,
            "schedule_identity": self.schedule_identity,
            "schedule_digest": self.schedule_digest,
            "coordinate": self.coordinate,
        }

    @classmethod
    def from_mapping(
        cls, payload: Any, *, plan: K12SamplePlan,
    ) -> "K12SampleIdentityV1":
        if not isinstance(plan, K12SamplePlan):
            raise TypeError("a validated K12SamplePlan is required")
        copied = _copy_json(payload)
        identity = _require_exact_keys(copied, _IDENTITY_FIELDS, "sample identity")
        if identity["artifact"] != SAMPLE_IDENTITY_ARTIFACT:
            raise ValueError("sample identity artifact mismatch")
        if _require_digest(identity["sample_plan_digest"], "sample_plan_digest") != plan.sample_plan_digest:
            raise ValueError("sample identity belongs to a different plan")
        if type(identity["phase"]) is not str:
            raise TypeError("sample identity phase must be a string")
        schedule_identity, schedule_digest, _ = _phase_schedule(identity["phase"])
        if identity["schedule_identity"] != schedule_identity:
            raise ValueError("sample identity schedule identity mismatch")
        if _require_digest(identity["schedule_digest"], "schedule_digest") != schedule_digest:
            raise ValueError("sample identity schedule digest mismatch")
        result = cls(plan, identity["phase"], identity["coordinate"])
        if canonical_json_bytes(result.to_dict()) != canonical_json_bytes(identity):
            raise ValueError("sample identity does not match the frozen plan domain")
        return result


__all__ = [
    "FINAL_COORDINATES",
    "FIXTURE_DIGEST",
    "K12SampleIdentityV1",
    "K12SamplePlan",
    "PHASE_FINAL_CELL",
    "PHASE_QUALIFICATION_CELL",
    "PHASE_QUALIFICATION_PROBE",
    "PROBE_COORDINATES",
    "PROBE_SCHEDULE_DIGEST",
    "PROBE_SCHEDULE_IDENTITY",
    "PROTOCOL_IDENTITY",
    "QUALIFICATION_COORDINATES",
    "QUALIFICATION_SCHEDULE_DIGEST",
    "QUALIFICATION_SCHEDULE_IDENTITY",
    "RANDOMIZATION_DIGEST",
    "RANDOMIZATION_SCHEDULE_IDENTITY",
    "SAMPLE_IDENTITY_ARTIFACT",
    "SAMPLE_PLAN_ARTIFACT",
    "canonical_json_bytes",
    "sample_plan_digest_for",
]
