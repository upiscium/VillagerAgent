"""Offline K12 design census and authenticated manifest loaders."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from benchmarks.common.eac.canonical import canonical_bytes, canonical_sha256
from benchmarks.minecraft.k12_identity import (
    AGGREGATE_IDENTITY, ANALYSIS_IDENTITY, CELL_RESULT_IDENTITY, FIXTURE_IDENTITY, PROTOCOL_IDENTITY,
    RANDOMIZATION_IDENTITY, REQUEST_CANONICALIZATION_IDENTITY,
    TRACE_IDENTITY, VALIDATION_CONTRACT_IDENTITY,
)
from benchmarks.minecraft.k12_request import request_content_digest
from benchmarks.minecraft.k12_fixture import build_k12_fixture, STRATUM_ACTIONS

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PROTOCOL_PATH = HERE / "k12_protocol_v1.json"
REQUEST_SCHEMA_PATH = HERE / "k12_request_schema_v1.json"
TRACE_SCHEMA_PATH = HERE / "k12_trace_schema_v1.json"
RESULT_SCHEMA_PATH = HERE / "k12_result_schema_v1.json"
VALIDATION_CONTRACT_PATH = HERE / "k12_validation_contract_v1.json"
FIXTURE_MANIFEST_PATH = ROOT / "configs/minecraft/k12-recovery-fixture-manifest-v1.json"
RANDOMIZATION_MANIFEST_PATH = ROOT / "configs/minecraft/k12-recovery-randomization-manifest-v1.json"
PUBLIC_SALT = "minecraft-k12-recovery-public-salt-v1"
RANDOMIZATION_ALGORITHM = "balanced-block-ordinal-v1+sha256-commitment"
STRATA = ("S1", "S2", "S3", "S4", "S5")
ARMS = ("A", "R", "S")
PERMUTATIONS = (("A", "R", "S"), ("A", "S", "R"), ("R", "A", "S"),
                ("R", "S", "A"), ("S", "A", "R"), ("S", "R", "A"))


class K12ProtocolError(ValueError):
    pass


def detached_digest(value: Mapping[str, Any]) -> str:
    body = {key: item for key, item in value.items() if key != "detached_artifact_sha256"}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def _load(path: Path, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, val in items:
            if key in out:
                raise K12ProtocolError(f"duplicate key in {label}: {key}")
            out[key] = val
        return out
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise K12ProtocolError(f"cannot load {label}") from exc
    if not isinstance(value, dict):
        raise K12ProtocolError(f"{label} must be an object")
    declared = value.get("detached_artifact_sha256")
    observed = detached_digest(value)
    if not isinstance(declared, str) or declared != observed:
        raise K12ProtocolError(f"{label} detached digest mismatch")
    value["detached_artifact_sha256"] = observed
    return value


@dataclass(frozen=True, slots=True)
class K12Cell:
    cell_id: str
    triplet_id: str
    stratum: str
    template: int
    seed: int
    arm: str
    arm_permutation: tuple[str, str, str]
    randomization_digest: str


def _perm_for(stratum: str, template: int, seed: int) -> tuple[str, str, str]:
    # The salt authenticates the assignment; the stable ordinal guarantees a
    # balanced block (rather than relying on chance to produce six unique rows).
    index = ((template - 1) * 3 + (seed - 1)) % 6
    return PERMUTATIONS[index]


def build_k12_cells() -> tuple[K12Cell, ...]:
    cells = []
    for stratum in STRATA:
        rows = []
        for template in (1, 2):
            for seed in (1, 2, 3):
                permutation = _perm_for(stratum, template, seed)
                descriptor = {"stratum": stratum, "template": template, "seed": seed,
                              "arms": list(permutation)}
                digest = hashlib.sha256(PUBLIC_SALT.encode() + canonical_bytes(descriptor)).hexdigest()
                triplet_id = f"K12-{stratum}-T{template}-N{seed}"
                rows.append((triplet_id, stratum, template, seed, permutation, digest))
        # Each stratum is a complete six-permutation block over the two
        # templates and three seeds.
        if sorted(cell[4] for cell in rows) != sorted(PERMUTATIONS):
            raise K12ProtocolError(f"{stratum} does not contain all arm permutations")
        for triplet_id, row_stratum, template, seed, permutation, digest in rows:
            for arm in permutation:
                cells.append(K12Cell(
                    f"{triplet_id}-{arm}", triplet_id, row_stratum, template, seed,
                    arm, permutation, digest,
                ))
    result = tuple(cells)
    if len(result) != 90 or len({cell.cell_id for cell in result}) != 90:
        raise K12ProtocolError("K12 census is not exactly 90 unique cells")
    return result


def load_k12_manifest(path: str | Path, *, expected_artifact: str | None = None) -> dict[str, Any]:
    value = _load(Path(path), "K12 manifest")
    if expected_artifact is not None and value.get("artifact_id") != expected_artifact:
        raise K12ProtocolError("K12 manifest identity mismatch")
    if value.get("artifact_version") != 1 or value.get("results") not in (None, []):
        raise K12ProtocolError("K12 manifest is not a results-free v1 artifact")
    return copy.deepcopy(value)


def load_fixture_manifest(path: str | Path = FIXTURE_MANIFEST_PATH) -> dict[str, Any]:
    return load_k12_manifest(path, expected_artifact="minecraft-k12-recovery-fixture-manifest")


def validate_fixture_descriptor(descriptor: Mapping[str, Any]) -> None:
    """Fail closed when an authenticated triplet descriptor disagrees with the generator."""
    required = {"triplet_id", "stratum", "template", "seed", "fixture_descriptor_digest"}
    if not required.issubset(descriptor):
        raise K12ProtocolError("incomplete K12 fixture descriptor")
    try:
        fixture_id = f'{descriptor["triplet_id"]}-fixture'
        expected = build_k12_fixture(
            fixture_id, action=STRATUM_ACTIONS[str(descriptor["stratum"])],
            template=int(descriptor["template"]), seed=int(descriptor["seed"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise K12ProtocolError("invalid K12 fixture descriptor binding") from exc
    def pairs(value: Any) -> list[list[Any]]:
        return [[key, item] for key, item in value]
    actual = {
        "triplet_id": descriptor["triplet_id"], "stratum": descriptor["stratum"],
        "template": descriptor["template"], "seed": descriptor["seed"],
        "fixture_id": fixture_id, "action": expected.action,
        "original_arguments": pairs(expected.original_arguments),
        "alternative_arguments": pairs(expected.alternative_arguments),
        "original_state_digest": expected.original.digest,
        "alternative_state_digest": expected.alternative.digest,
        "fixture_digest": expected.fixture_digest,
    }
    committed = canonical_sha256(json.loads(json.dumps(actual)))
    if descriptor.get("fixture_descriptor_digest") != committed:
        raise K12ProtocolError("fixture descriptor commitment mismatch")


def load_randomization_manifest(path: str | Path = RANDOMIZATION_MANIFEST_PATH) -> dict[str, Any]:
    return load_k12_manifest(path, expected_artifact="minecraft-k12-recovery-randomization-manifest")


@lru_cache(maxsize=4)
def _load_k12_protocol_cached(path: str) -> dict[str, Any]:
    protocol = _load(Path(path), "K12 protocol")
    if protocol.get("artifact_id") != "minecraft-k12-recovery-protocol" or protocol.get("artifact_version") != 1:
        raise K12ProtocolError("K12 protocol identity mismatch")
    fixture = load_fixture_manifest()
    randomization = load_randomization_manifest()
    request_schema = _load(REQUEST_SCHEMA_PATH, "K12 request schema")
    trace_schema = _load(TRACE_SCHEMA_PATH, "K12 trace schema")
    result_schema = _load(RESULT_SCHEMA_PATH, "K12 result schema")
    validation_contract = _load(VALIDATION_CONTRACT_PATH, "K12 validation contract")
    schema_artifacts = (
        (request_schema, "minecraft-k12-request-content-schema", REQUEST_CANONICALIZATION_IDENTITY),
        (trace_schema, "minecraft-k12-recovery-trace-schema", TRACE_IDENTITY),
        (result_schema, "minecraft-k12-recovery-cell-result-schema", CELL_RESULT_IDENTITY),
        (validation_contract, "minecraft-k12-recovery-validation-contract", VALIDATION_CONTRACT_IDENTITY),
    )
    if any(document.get("artifact_id") != artifact_id
           or document.get("artifact_version") != 1
           or document.get("schema_version") != schema_version
           for document, artifact_id, schema_version in schema_artifacts):
        raise K12ProtocolError("K12 schema artifact identity mismatch")
    cells = build_k12_cells()
    expected_identity = {
        "protocol": PROTOCOL_IDENTITY,
        "fixture_manifest": FIXTURE_IDENTITY,
        "randomization_manifest": RANDOMIZATION_IDENTITY,
        "request_schema": REQUEST_CANONICALIZATION_IDENTITY,
        "trace_schema": TRACE_IDENTITY,
        "result_schema": CELL_RESULT_IDENTITY,
        "validation_contract": VALIDATION_CONTRACT_IDENTITY,
        "aggregate": AGGREGATE_IDENTITY,
        "analysis": ANALYSIS_IDENTITY,
    }
    if (protocol.get("protocol_version") != PROTOCOL_IDENTITY
            or protocol.get("identity") != expected_identity):
        raise K12ProtocolError("K12 frozen identity mismatch")
    if protocol.get("public_salt") != PUBLIC_SALT or protocol.get("cell_count") != 90:
        raise K12ProtocolError("K12 protocol census mismatch")
    expected_worker_events = [
        "cell_started", "prepared_request_frozen", "invalidation_ingested",
        "advisory_would_block", "rejection_observation_emitted", "recovery_started",
        "recovery_step", "recovery_step_terminal", "model_call_admitted", "model_call_terminal",
        "observation_started", "observation_terminal", "evidence_ingested",
        "recovery_proposed", "effect_decision", "effect_entered", "effect_terminal",
        "worker_terminal_candidate",
    ]
    expected_parent_events = [
        "reset_attested", "current_inadmissible_confirmed", "authority_rejected",
        "proposal_validated", "new_request_prepared", "permit_issued",
        "effect_decision", "effect_entered", "effect_terminal",
        "objective_oracle_evaluated", "budget_reached", "process_finalized",
        "cell_terminal",
    ]
    if (protocol.get("worker_events") != expected_worker_events
            or protocol.get("parent_events") != expected_parent_events
            or protocol.get("operation_lifecycle") != {
                "id_field": "operation_id", "proposal_id_field": "message_digest",
                "non_interleaving": True, "unique_ids": True,
                "paired": [["model_call_admitted", "model_call_terminal"],
                           ["observation_started", "observation_terminal"],
                           ["recovery_step", "recovery_step_terminal"],
                           ["recovery_proposed", "proposal_validated"],
                           ["effect_entered", "effect_terminal"]],
                "atomic": ["evidence_ingested"],
            }):
        raise K12ProtocolError("K12 worker/parent event boundary mismatch")
    if fixture.get("triplet_count") != 30 or randomization.get("cell_count") != 90:
        raise K12ProtocolError("K12 manifest census mismatch")
    if (randomization.get("public_salt") != PUBLIC_SALT
            or randomization.get("algorithm") != RANDOMIZATION_ALGORITHM
            or randomization.get("scenario_inputs") != {
                "strata": list(STRATA), "templates": [1, 2], "seeds": [1, 2, 3],
                "arms": [list(arm) for arm in PERMUTATIONS],
            }):
        raise K12ProtocolError("K12 randomization salt mismatch")
    if randomization.get("ordered_schedule") != [cell.cell_id for cell in cells]:
        raise K12ProtocolError("K12 ordered schedule mismatch")
    expected_triplets = list(dict.fromkeys(cell.triplet_id for cell in cells))
    fixture_triplets = fixture.get("triplets")
    if (not isinstance(fixture_triplets, list) or len(fixture_triplets) != 30
            or [item.get("triplet_id") for item in fixture_triplets] != expected_triplets
            or fixture.get("actions") != ["MineBlock", "placeBlock", "navigateTo", "attackTarget", "handoverBlock"]
            or fixture.get("action_mapping") != {
                "MineBlock": "post_dig", "placeBlock": "post_place", "navigateTo": "post_move_to_pos",
                "attackTarget": "post_attack", "handoverBlock": "post_hand",
            }
            or fixture.get("stratum_action") != {
                "S1": "MineBlock", "S2": "placeBlock", "S3": "navigateTo",
                "S4": "attackTarget", "S5": "handoverBlock",
            }):
        raise K12ProtocolError("K12 fixture triplet census mismatch")
    for descriptor in fixture_triplets:
        validate_fixture_descriptor(descriptor)
    expected_coordinates = {
        cell.triplet_id: (cell.stratum, cell.template, cell.seed)
        for cell in cells
    }
    if any((descriptor.get("stratum"), descriptor.get("template"), descriptor.get("seed"))
           != expected_coordinates.get(descriptor.get("triplet_id"))
           for descriptor in fixture_triplets):
        raise K12ProtocolError("fixture descriptor schedule binding mismatch")
    campaign_digest = hashlib.sha256(canonical_bytes({
        "protocol": detached_digest(protocol),
        "fixture_manifest": fixture["detached_artifact_sha256"],
        "randomization_manifest": randomization["detached_artifact_sha256"],
        "request_schema": request_schema["detached_artifact_sha256"],
        "trace_schema": trace_schema["detached_artifact_sha256"],
        "result_schema": result_schema["detached_artifact_sha256"],
        "validation_contract": validation_contract["detached_artifact_sha256"],
    })).hexdigest()
    result = copy.deepcopy(protocol)
    result.update({"validated_protocol_digest": campaign_digest,
                   "validated_fixture_digest": fixture["detached_artifact_sha256"],
                   "validated_randomization_digest": randomization["detached_artifact_sha256"],
                   "cells": [cell.cell_id for cell in cells]})
    return result


def load_k12_protocol(path: str | Path = PROTOCOL_PATH) -> dict[str, Any]:
    """Return a fresh authenticated protocol view; callers cannot poison the cache."""
    return copy.deepcopy(_load_k12_protocol_cached(str(Path(path).resolve())))


__all__ = ["ARMS", "K12Cell", "K12ProtocolError", "PUBLIC_SALT", "STRATA",
           "build_k12_cells", "detached_digest", "load_fixture_manifest",
            "load_k12_manifest", "load_k12_protocol", "load_randomization_manifest",
            "validate_fixture_descriptor"]
