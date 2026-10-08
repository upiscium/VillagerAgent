"""Frozen, non-executing K10 holdout contracts and census helpers.

This module authenticates the prospective candidate pool, selected inventory,
protocol, historical-unseen audit, result schema, cell traces, and aggregate.
It never constructs a runtime or crosses an effect boundary.
"""
from __future__ import annotations

import hashlib
import json
import copy
import os
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from benchmarks.common.eac import ActionRef, ExactRequest
from benchmarks.common.eac.canonical import canonical_bytes
from benchmarks.minecraft import k6_protocol as k6

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CANDIDATE_POOL_PATH = HERE / "k10_candidate_pool_v1.json"
INVENTORY_PATH = HERE / "k10_inventory_v1.json"
PROTOCOL_PATH = HERE / "k10_protocol_v1.json"
RESULT_SCHEMA_PATH = HERE / "k10_result_schema_v1.json"

SELECTION_SALT = "issue511-k10-holdout-v1"
CANDIDATE_POOL_DIGEST = "5e95279edfbd45ee258932ebfaa33ef2c1dc563273afa0b4878eb86eaf7bb2ff"
INVENTORY_DIGEST = "51ba4d0e8e387fd06450367f48d3049c07fc01665be888519acec556d5e3e58a"
PROTOCOL_DIGEST = "94b70d7d746863f7febb5d79169cf91b37bb78b79e73c7733fe58490ec81424e"
RESULT_SCHEMA_DIGEST = "6cd3a1919a3278aca80646a82233c54834c408f91bdb5a43aaf4f7587aa9c9b1"
SELECTION_MANIFEST_DIGEST = "ce92e9426a10486b17dd0551a4dfce5a46cfcb4524fb67f3c2404b50df7f8480"
SUBJECT_RUNTIME_REFERENCE = "ecb553323487bff69be2cfa375caea8dd02eada5"
_COMMIT_ID = re.compile(r"[0-9a-f]{40}\Z")
CONDITIONS = ("dual_dag_advisory", "dual_dag_authority")
PRIMARY_FAMILIES = ("S1", "S2", "S3")
CONTROL_FAMILIES = ("C1", "C2")
ACTORS = ("Alice", "Bob")
EXACT_FIELDS = (
    "candidate_id", "attempt_id", "exact_request_digest", "action", "arguments", "target",
)
C2_EVALUATOR_FIELDS = (
    "evaluator_truth_before", "evaluator_truth_after", "evaluator_truth_before_digest",
    "evaluator_truth_after_digest", "evaluator_truth_changed",
    "evaluator_truth_authority_input", "evaluator_truth_precondition_input",
)
_FROZEN_PHASE_KEYS = {
    "r_p": {"candidate_id", "attempt_id", "exact_request_digest", "action", "arguments",
            "target", "EAdm", "authority_epoch", "witness_root_ids", "dependency_ids"},
    "r_d": {"current_EAdm", "authority_epoch", "reasons", "mutation_type",
            "mutation_dependency_ids", "intersecting_dependency_ids",
            "relevant_action_dependency_changed", "permit_or_shadow_fresh"},
    "r_e": {"candidate_id", "attempt_id", "exact_request_digest", "action", "arguments",
            "target", "current_EAdm", "authority_epoch_before_execution",
            "exact_action_submitted", "permit_or_shadow_fresh", "EnvPre_oracle",
            "SecPre_oracle", "execution_allowed", "rejection_reason", "native_callable_reached"},
}
_FROZEN_SEMANTIC_BINDINGS = {
    "support_policy": {"identity": "eac-primary-support", "version": 1,
                       "digest": "ef34b67ef618ed4b34a9c2720d854e02d8fb6af917a0cbe472daef8cc5603d51"},
    "source_profile": {"identity": "minecraft-eac-primary", "version": 1,
                       "digest": "01f65a8fd4bb68b1631e81d3c8d50f073747b5179995eeb60be3a55fdb6979be"},
    "epre_classification": {"identity": "minecraft-preconditions", "version": 1,
                            "digest": "7c8bf97b80c96f1d05e8250cb9d89bb21b35c073f49979501090d72f13b56001"},
    "ingestion_contract": {"identity": "minecraft-eac-ingestion-contract", "version": 1,
                           "digest": "33c9fd27a70ab3f6edffad14c07f9f66dc04b795363e7b1b55518a4d1a1ef42f"},
}
_FROZEN_K6_ACTIONS = {
    "I1": ("MineBlock", 1, "f21619931b543f80e769e954ba66e9e401d22966db93f55d42de5cff9aabe315",
           "minecraft", "target_block_present", ("x", "y", "z"), "current", ("x", "y", "z")),
    "I2": ("placeBlock", 1, "f92a7ce9a8ab545e9cdabb4b68c85437a2304c40c2832cd3a379e8de59d0087b",
           "minecraft", "placement_target_observed", ("x", "y", "z"), "current",
           ("item_name", "x", "y", "z", "facing")),
    "I3": ("navigateTo", 1, "eecfdbaf2fbe3577bc96645bd4836f495e9afc977818f661fd56290de4ab3a8b",
           "minecraft", "destination_observed", ("x", "y", "z"), "current", ("x", "y", "z")),
    "I4": ("attackTarget", 1, "88a5e9bf16c247a3184b4f75e4dbebe5c1a2198e508d4a267de14eb6639f68f4",
           "minecraft", "entity_target_observed", ("target_name",), "current", ("target_name",)),
    "I5": ("handoverBlock", 1, "2f273f5f6f9c61e3661f53aea31992d811945c873959768bf2391f790faab51a",
           "minecraft", "recipient_observed", ("target_player_name",), "current",
           ("target_player_name", "item_name", "item_count")),
}
_DISCLOSED_SOURCE_PATHS = frozenset({
    "benchmarks/minecraft/k1_f1.py",
    "benchmarks/minecraft/k2_dependency_ablation.py",
    "benchmarks/minecraft/k3a_actor_scope.py",
    "benchmarks/minecraft/k3b_contradiction.py",
    "tests/test_minecraft_eac_runtime.py",
    "tests/test_minecraft_k6_fixture.py",
})

EXPECTED_SELECTED = (
    ("K10-I1-H1", "K10-P-I1-04", "sha256:25621faadb7d12c82afc8ae7c745e374f76e22e2c6602955ac880d5d12e2e711"),
    ("K10-I1-H2", "K10-P-I1-01", "sha256:91de0d95de8a227c8b4810bd29e1b699f7eb300841ecb97e18e01b70ad6a5e16"),
    ("K10-I2-H1", "K10-P-I2-03", "sha256:1302c5612ed578f9aa21f4fdec849981d4f20dcd0a8d4ca63a7ba7db871d28bd"),
    ("K10-I2-H2", "K10-P-I2-02", "sha256:f6f42acdf052e5b47bca3d6601fe2eb5fe9a8c602417de452b39c7b45e451118"),
    ("K10-I3-H1", "K10-P-I3-02", "sha256:763f7508361c95c7c0a2b3d5a1a0f665ec43948790cb83e37c51045455f544ff"),
    ("K10-I3-H2", "K10-P-I3-03", "sha256:a0e23803d782b5d78973c009de6a49c28bc960684aac081680ec78b6d00e4165"),
    ("K10-I4-H1", "K10-P-I4-04", "sha256:c3cb689517f43d304ad4daafac3d9c9d8ce8226244cd303f0639e0a47f77cfb9"),
    ("K10-I4-H2", "K10-P-I4-03", "sha256:73423e9e7cd633599018ec11a76dcbe73420f8322cc69b80ff1b99c8b9798333"),
    ("K10-I5-H1", "K10-P-I5-03", "sha256:c398b2cbf1f51447c52be4036e711f84ceffbd5b955c84886c9db87a53d8b555"),
    ("K10-I5-H2", "K10-P-I5-02", "sha256:fa78c48016858ab9c2676c2c5296940429d270bcad4272b37b61116297e39e2a"),
)


class K10ContractError(ValueError):
    """A frozen K10 artifact or candidate trace violates its contract."""


def _load_json(path: Path) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise K10ContractError(f"duplicate JSON key in {path}: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise K10ContractError(f"cannot load K10 JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise K10ContractError(f"K10 artifact must be an object: {path}")
    return value


def detached_digest(value: Mapping[str, Any]) -> str:
    detached = dict(value)
    detached.pop("detached_artifact_sha256", None)
    return hashlib.sha256(canonical_bytes(detached)).hexdigest()


def _validate_detached(value: Mapping[str, Any], label: str) -> str:
    declared = value.get("detached_artifact_sha256")
    if (not isinstance(declared, str) or len(declared) != 64
            or any(character not in "0123456789abcdef" for character in declared)):
        raise K10ContractError(f"{label} digest is missing or malformed")
    if detached_digest(value) != declared:
        raise K10ContractError(f"{label} detached digest mismatch")
    return declared


def _sha256_prefixed(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def canonical_request_digest(action: Mapping[str, Any], arguments: Mapping[str, Any], target: Any) -> str:
    """Return the run-ID-independent request-content identity used by the unseen audit."""
    return _sha256_prefixed({"action": dict(action), "arguments": dict(arguments), "target": target})


def _selection_digest(descriptor: Mapping[str, Any]) -> str:
    return hashlib.sha256(SELECTION_SALT.encode("utf-8") + canonical_bytes(descriptor)).hexdigest()


def _selection_projection(candidates: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    selected: list[dict[str, Any]] = []
    rows = tuple(candidates)
    for stratum in ("I1", "I2", "I3", "I4", "I5"):
        ranked = sorted(
            (row for row in rows if row["descriptor"]["stratum"] == stratum and row["eligible"] is True),
            key=lambda row: row["selection_digest"],
        )
        for rank, row in enumerate(ranked[:2], start=1):
            selected.append({
                "inventory_id": f"K10-{stratum}-H{rank}",
                "pool_id": row["pool_id"],
                "rank": rank,
                "canonical_request_digest": row["canonical_request_digest"],
                "selection_digest": row["selection_digest"],
                "descriptor": row["descriptor"],
            })
    return {
        "artifact_id": "minecraft-k10-holdout-selection",
        "artifact_version": 1,
        "selection_rule": "ascending selection_digest within stratum, first two",
        "selected": selected,
    }


@lru_cache(maxsize=8)
def _load_k10_candidate_pool_cached(path: str) -> tuple[dict[str, Any], ...]:
    document = _load_json(Path(path))
    digest = _validate_detached(document, "K10 candidate pool")
    if set(document) != {
        "artifact_id", "artifact_version", "detached_artifact_sha256",
        "selection_salt", "canonicalization", "candidates",
    } or (document["artifact_id"], document["artifact_version"], digest) != (
        "minecraft-k10-candidate-pool", 1, CANDIDATE_POOL_DIGEST,
    ):
        raise K10ContractError("K10 candidate-pool identity mismatch")
    if document["selection_salt"] != SELECTION_SALT or document["canonicalization"] != (
        "benchmarks.common.eac.canonical.canonical_bytes constrained RFC8785 domain"
    ):
        raise K10ContractError("K10 candidate selection serialization mismatch")
    rows = document["candidates"]
    if not isinstance(rows, list) or len(rows) != 20:
        raise K10ContractError("K10 candidate pool must contain exactly 20 rows")
    if len({row.get("pool_id") for row in rows if isinstance(row, Mapping)}) != 20:
        raise K10ContractError("K10 candidate pool IDs are not unique")
    descriptor_keys = {
        "descriptor_version", "stratum", "action", "arguments", "target",
        "expected_epre", "required_actor_scope", "source_profile_route",
        "env_pre_diagnostic_assumptions", "sec_pre_diagnostic_assumptions",
        "production_runtime_change_required",
    }
    expected_actions = _FROZEN_K6_ACTIONS
    expected_pool_ids = [f"K10-P-I{stratum}-{index:02d}" for stratum in range(1, 6) for index in range(1, 5)]
    if [row.get("pool_id") for row in rows] != expected_pool_ids:
        raise K10ContractError("K10 candidate pool order or IDs changed")
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {
            "pool_id", "descriptor", "canonical_request_digest", "selection_digest", "eligible",
        } or row["eligible"] is not True:
            raise K10ContractError("K10 candidate row schema or eligibility mismatch")
        descriptor = row["descriptor"]
        if not isinstance(descriptor, Mapping) or set(descriptor) != descriptor_keys:
            raise K10ContractError("K10 candidate descriptor schema mismatch")
        stratum = descriptor["stratum"]
        expected = expected_actions.get(stratum)
        if expected is None:
            raise K10ContractError("K10 candidate uses an unknown stratum")
        (identity, version, action_digest, namespace, predicate, fields, temporal_scope,
         argument_fields) = expected
        if descriptor["descriptor_version"] != "minecraft-k10-candidate/1" or descriptor["action"] != {
            "identity": identity, "version": version, "digest": action_digest,
        }:
            raise K10ContractError("K10 candidate action binding mismatch")
        arguments = descriptor["arguments"]
        if (not isinstance(arguments, Mapping) or descriptor["target"] != arguments
                or set(arguments) != set(argument_fields)):
            raise K10ContractError("K10 candidate arguments or target mismatch")
        proposition_arguments = {field: arguments[field] for field in fields}
        if descriptor["expected_epre"] != {
            "namespace": namespace, "predicate": predicate,
            "argument_fields": list(fields), "arguments": proposition_arguments,
            "temporal_scope": temporal_scope,
        }:
            raise K10ContractError("K10 candidate EPre binding mismatch")
        if (descriptor["required_actor_scope"] != "acting-player-visible"
                or descriptor["source_profile_route"] != "minecraft-direct-observation"
                or not isinstance(descriptor["env_pre_diagnostic_assumptions"], list)
                or not descriptor["env_pre_diagnostic_assumptions"]
                or descriptor["sec_pre_diagnostic_assumptions"] != {
                    "declared": False, "oracle_expected": True,
                }
                or descriptor["production_runtime_change_required"] is not False):
            raise K10ContractError("K10 candidate semantic eligibility mismatch")
        observed_request = canonical_request_digest(descriptor["action"], arguments, descriptor["target"])
        if row["canonical_request_digest"] != observed_request:
            raise K10ContractError("K10 candidate request-content digest mismatch")
        if row["selection_digest"] != _selection_digest(descriptor):
            raise K10ContractError("K10 candidate selection digest mismatch")
    if {row["descriptor"]["stratum"] for row in rows} != {"I1", "I2", "I3", "I4", "I5"}:
        raise K10ContractError("K10 candidate strata mismatch")
    if any(sum(row["descriptor"]["stratum"] == stratum for row in rows) != 4
           for stratum in ("I1", "I2", "I3", "I4", "I5")):
        raise K10ContractError("K10 candidate stratum must contain exactly four rows")
    selection = _selection_projection(rows)
    if _sha256_prefixed(selection) != "sha256:" + SELECTION_MANIFEST_DIGEST:
        raise K10ContractError("K10 selection-manifest digest mismatch")
    observed = tuple(
        (row["inventory_id"], row["pool_id"], row["canonical_request_digest"])
        for row in selection["selected"]
    )
    if observed != EXPECTED_SELECTED:
        raise K10ContractError("K10 selected candidate set changed")
    return tuple(dict(row) for row in rows)


def load_k10_candidate_pool(
    path: str | Path = CANDIDATE_POOL_PATH,
) -> tuple[dict[str, Any], ...]:
    """Return a detached copy of the authenticated K10 candidate pool."""
    return copy.deepcopy(_load_k10_candidate_pool_cached(str(Path(path))))


@dataclass(frozen=True, slots=True)
class K10InventoryItem:
    inventory_id: str
    pool_id: str
    selection_rank: int
    stratum: str
    action_identity: str
    action_version: int
    action_digest: str
    request_arguments: tuple[tuple[str, Any], ...]
    target_items: tuple[tuple[str, Any], ...]
    proposition_namespace: str
    proposition_predicate: str
    proposition_argument_fields: tuple[str, ...]
    proposition_arguments: tuple[tuple[str, Any], ...]
    temporal_scope: str
    canonical_request_digest: str
    env_pre_diagnostic_assumptions: tuple[str, ...]

    def request(self) -> dict[str, Any]:
        return dict(self.request_arguments)

    def target(self) -> dict[str, Any]:
        return dict(self.target_items)


@lru_cache(maxsize=8)
def load_k10_inventory(path: str | Path = INVENTORY_PATH) -> tuple[K10InventoryItem, ...]:
    document = _load_json(Path(path))
    digest = _validate_detached(document, "K10 inventory")
    if digest != INVENTORY_DIGEST or set(document) != {
        "artifact_id", "artifact_version", "detached_artifact_sha256",
        "candidate_pool_binding", "selection_manifest_digest", "iid_samples",
        "inventory_census", "items",
    } or (document["artifact_id"], document["artifact_version"], document["iid_samples"],
          document["inventory_census"]) != (
        "minecraft-k10-action-inventory", 1, False, True,
    ):
        raise K10ContractError("K10 inventory identity mismatch")
    if document["candidate_pool_binding"] != {
        "artifact_id": "minecraft-k10-candidate-pool", "artifact_version": 1,
        "detached_artifact_sha256": CANDIDATE_POOL_DIGEST,
    } or document["selection_manifest_digest"] != SELECTION_MANIFEST_DIGEST:
        raise K10ContractError("K10 inventory selection binding mismatch")
    candidates = {row["pool_id"]: row for row in load_k10_candidate_pool()}
    selected_projection = _selection_projection(candidates.values())["selected"]
    raw_items = document["items"]
    if not isinstance(raw_items, list) or len(raw_items) != 10:
        raise K10ContractError("K10 inventory must contain exactly ten selected items")
    expected_keys = {
        "inventory_id", "pool_id", "selection_rank", "stratum", "action",
        "request_arguments", "target", "expected_epre", "required_actor_scope",
        "source_profile_route", "env_pre_diagnostic_assumptions",
        "sec_pre_diagnostic_assumptions", "production_runtime_change_required",
        "canonical_request_digest", "selection_digest",
    }
    result: list[K10InventoryItem] = []
    for raw, selected, expected_identity in zip(raw_items, selected_projection, EXPECTED_SELECTED):
        if not isinstance(raw, Mapping) or set(raw) != expected_keys:
            raise K10ContractError("K10 inventory item schema mismatch")
        candidate = candidates.get(raw["pool_id"])
        descriptor = candidate["descriptor"] if candidate is not None else None
        expected_raw = {
            "inventory_id": selected["inventory_id"], "pool_id": selected["pool_id"],
            "selection_rank": selected["rank"], "stratum": descriptor["stratum"],
            "action": descriptor["action"], "request_arguments": descriptor["arguments"],
            "target": descriptor["target"], "expected_epre": descriptor["expected_epre"],
            "required_actor_scope": descriptor["required_actor_scope"],
            "source_profile_route": descriptor["source_profile_route"],
            "env_pre_diagnostic_assumptions": descriptor["env_pre_diagnostic_assumptions"],
            "sec_pre_diagnostic_assumptions": descriptor["sec_pre_diagnostic_assumptions"],
            "production_runtime_change_required": False,
            "canonical_request_digest": selected["canonical_request_digest"],
            "selection_digest": selected["selection_digest"],
        }
        if dict(raw) != expected_raw or (
            raw["inventory_id"], raw["pool_id"], raw["canonical_request_digest"]
        ) != expected_identity:
            raise K10ContractError("K10 inventory does not reproduce the frozen selection")
        epre = raw["expected_epre"]
        result.append(K10InventoryItem(
            raw["inventory_id"], raw["pool_id"], raw["selection_rank"], raw["stratum"],
            raw["action"]["identity"], raw["action"]["version"], raw["action"]["digest"],
            tuple(raw["request_arguments"].items()), tuple(raw["target"].items()),
            epre["namespace"], epre["predicate"], tuple(epre["argument_fields"]),
            tuple(epre["arguments"].items()), epre["temporal_scope"],
            raw["canonical_request_digest"], tuple(raw["env_pre_diagnostic_assumptions"]),
        ))
    if len({item.inventory_id for item in result}) != 10 or detached_digest(document) != digest:
        raise K10ContractError("K10 inventory uniqueness or digest mismatch")
    return tuple(result)


def expected_action_digest(item: K10InventoryItem) -> str:
    return item.action_digest


@dataclass(frozen=True, slots=True)
class K10CellSpec:
    cell_id: str
    scenario_family: str
    inventory_id: str
    condition: str
    affected_actor: str
    matrix: str


def build_k10_cells(inventory: Iterable[K10InventoryItem] | None = None) -> tuple[K10CellSpec, ...]:
    items = tuple(inventory or load_k10_inventory())
    cells: list[K10CellSpec] = []
    for family in ("S1", "S2"):
        for item in items:
            for condition in CONDITIONS:
                cells.append(K10CellSpec(
                    f"K10-{family}-{item.inventory_id}-{condition}", family,
                    item.inventory_id, condition, "Alice", "primary",
                ))
    for item in items:
        for actor in ACTORS:
            for condition in CONDITIONS:
                cells.append(K10CellSpec(
                    f"K10-S3-{item.inventory_id}-{actor}-{condition}", "S3",
                    item.inventory_id, condition, actor, "primary",
                ))
    for family in CONTROL_FAMILIES:
        for item in items:
            for condition in CONDITIONS:
                cells.append(K10CellSpec(
                    f"K10-{family}-{item.inventory_id}-{condition}", family,
                    item.inventory_id, condition, "Alice", "control",
                ))
    counts = {family: sum(cell.scenario_family == family for cell in cells)
              for family in PRIMARY_FAMILIES + CONTROL_FAMILIES}
    if (len(cells), len({cell.cell_id for cell in cells}), counts) != (
        120, 120, {"S1": 20, "S2": 20, "S3": 40, "C1": 20, "C2": 20},
    ) or sum(cell.matrix == "primary" for cell in cells) != 80:
        raise K10ContractError("K10 cell census is not the frozen 120-cell matrix")
    return tuple(cells)


def _validate_result_schema(document: Mapping[str, Any]) -> str:
    digest = _validate_detached(document, "K10 result schema")
    if digest != RESULT_SCHEMA_DIGEST or set(document) != {
        "artifact_id", "artifact_version", "detached_artifact_sha256", "schema_version",
        "required_top_level", "exact_action_fields", "phase_fields", "cell_fields",
        "s3_fields", "c2_evaluator_truth_fields", "ratio_encoding",
        "statistical_fields_forbidden", "aggregate_schema_version",
    } or (document["artifact_id"], document["artifact_version"], document["schema_version"],
          document["aggregate_schema_version"]) != (
        "minecraft-k10-cell-trace-schema", 1, "minecraft-k10-cell-trace/1",
        "minecraft-k10-aggregate/1",
    ):
        raise K10ContractError("K10 result schema identity mismatch")
    if tuple(document["exact_action_fields"]) != EXACT_FIELDS:
        raise K10ContractError("K10 exact-action schema mismatch")
    if (set(document["phase_fields"]) != set(_FROZEN_PHASE_KEYS)
            or any(set(document["phase_fields"][name]) != fields
                   for name, fields in _FROZEN_PHASE_KEYS.items())):
        raise K10ContractError("K10 phase schema mismatch")
    if tuple(document["c2_evaluator_truth_fields"]) != C2_EVALUATOR_FIELDS:
        raise K10ContractError("K10 C2 schema mismatch")
    if document["ratio_encoding"] != {"numerator": "integer", "denominator": "integer"} or document[
        "statistical_fields_forbidden"
    ] != ["confidence_interval", "p_value", "iid_standard_error"]:
        raise K10ContractError("K10 statistical schema mismatch")
    return digest


def _validate_frozen_source_path(relative: object) -> str:
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or "\x00" in relative or ":" in relative or relative.startswith("/")
            or any(part in ("", ".", "..") for part in relative.split("/"))
            or PurePosixPath(relative).as_posix() != relative):
        raise K10ContractError("K10 frozen source path is invalid")
    return relative


def _protected_content_bindings(protocol: Mapping[str, Any]) -> Mapping[str, str]:
    bindings = protocol.get("protected_runtime_content_bindings")
    if not isinstance(bindings, Mapping) or len(bindings) != 19:
        raise K10ContractError("K10 protected-content manifest mismatch")
    for relative, expected in bindings.items():
        _validate_frozen_source_path(relative)
        if (not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None):
            raise K10ContractError("K10 protected-content digest is malformed")
    return bindings


def _read_frozen_source_blob(revision: str, relative: str, *, root: Path = ROOT) -> bytes:
    if _COMMIT_ID.fullmatch(revision) is None:
        raise K10ContractError("K10 frozen source revision is invalid")
    root = Path(root)
    git_env = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"}
    try:
        listing = subprocess.run(
            ["git", "-C", str(root), "ls-tree", "-z", revision, "--", relative],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env=git_env, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise K10ContractError(f"K10 frozen source object unavailable: {relative}") from exc
    entries = listing.stdout.split(b"\0") if listing.returncode == 0 else []
    entries = [entry for entry in entries if entry]
    if len(entries) != 1:
        raise K10ContractError(f"K10 frozen source object unavailable: {relative}")
    try:
        metadata, encoded_path = entries[0].split(b"\t", 1)
        mode, object_type, object_id = metadata.split(b" ", 2)
        listed_path = encoded_path.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise K10ContractError(f"K10 frozen source object unavailable: {relative}") from exc
    if (listed_path != relative or mode not in (b"100644", b"100755")
            or object_type != b"blob" or re.fullmatch(rb"[0-9a-f]{40,64}", object_id) is None):
        raise K10ContractError(f"K10 frozen source object unavailable: {relative}")
    try:
        blob = subprocess.run(
            ["git", "-C", str(root), "cat-file", "blob", object_id.decode("ascii")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env=git_env, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise K10ContractError(f"K10 frozen source object unavailable: {relative}") from exc
    if blob.returncode != 0:
        raise K10ContractError(f"K10 frozen source object unavailable: {relative}")
    return blob.stdout


def _load_frozen_json(
    revision: str, relative: str, *, source_reader=None,
) -> dict[str, Any]:
    """Load one frozen Git blob as a duplicate-key-safe JSON object."""
    relative = _validate_frozen_source_path(relative)

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise K10ContractError(f"duplicate JSON key in frozen K10 source: {relative}")
            value[key] = item
        return value

    reader = source_reader or _read_frozen_source_blob
    try:
        text = reader(revision, relative).decode("utf-8")
        value = json.loads(text, object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise K10ContractError(f"cannot load frozen K10 JSON source: {relative}") from exc
    if not isinstance(value, dict):
        raise K10ContractError(f"frozen K10 JSON source must be an object: {relative}")
    return value


def _validate_frozen_protected_content(
    protocol: Mapping[str, Any], *, source_reader=None,
) -> None:
    revision = protocol.get("subject_runtime_semantic_reference")
    if revision != SUBJECT_RUNTIME_REFERENCE:
        raise K10ContractError("K10 frozen source revision identity mismatch")
    bindings = _protected_content_bindings(protocol)
    reader = source_reader or _read_frozen_source_blob
    for relative, expected in bindings.items():
        observed = hashlib.sha256(reader(revision, relative)).hexdigest()
        if observed != expected:
            raise K10ContractError(f"K10 frozen protected source mismatch: {relative}")


def validate_live_k10_checkout(
    protocol: Mapping[str, Any], *, root: str | Path = ROOT,
) -> None:
    """Fail closed unless a prospective K10 execution checkout matches frozen source."""
    bindings = _protected_content_bindings(protocol)
    root = Path(root).resolve()
    for relative, expected in bindings.items():
        path = root.joinpath(*relative.split("/"))
        if (not path.is_file() or path.is_symlink()
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
            raise K10ContractError(f"K10 protected runtime content mismatch: {relative}")


def audit_historical_submissions(
    protocol: Mapping[str, Any] | None = None,
    inventory: Iterable[K10InventoryItem] | None = None,
    *, source_reader=None, frozen_json_loader=None,
) -> dict[str, Any]:
    """Fail closed unless selected requests are absent from all disclosed sources."""
    protocol = dict(protocol or _load_json(PROTOCOL_PATH))
    protocol_source = {
        key: value for key, value in protocol.items()
        if not key.startswith("validated_")
    }
    if (_validate_detached(protocol_source, "K10 historical-audit protocol")
            != PROTOCOL_DIGEST):
        raise K10ContractError("K10 historical-audit protocol identity mismatch")
    audit = protocol.get("historical_unseen_audit")
    if not isinstance(audit, Mapping):
        raise K10ContractError("K10 historical-unseen audit declaration is missing")
    archive = ROOT / str(audit.get("archive_cells_path", ""))
    if not archive.is_dir() or archive.is_symlink():
        raise K10ContractError("authoritative K8 archive is unavailable")
    files = sorted(archive.glob("*.json"))
    if len(files) != audit.get("expected_trace_count") or len(files) != 60:
        raise K10ContractError("authoritative K8 trace census is incomplete")
    historical: set[str] = set()
    for path in files:
        trace = _load_json(path)
        phase = trace.get("r_p")
        if not isinstance(phase, Mapping) or not all(name in phase for name in ("action", "arguments", "target")):
            raise K10ContractError(f"K8 historical trace request is malformed: {path.name}")
        historical.add(canonical_request_digest(phase["action"], phase["arguments"], phase["target"]))
        s3 = trace.get("s3")
        if isinstance(s3, Mapping):
            other = s3.get("unaffected_r_p")
            if not isinstance(other, Mapping):
                raise K10ContractError(f"K8 S3 historical request is malformed: {path.name}")
            historical.add(canonical_request_digest(other["action"], other["arguments"], other["target"]))
    declared = {"sha256:" + value for value in audit.get("prior_request_content_digests", [])}
    if historical != declared or len(historical) != 5:
        raise K10ContractError("K8 historical request-content set changed")
    revision = protocol.get("subject_runtime_semantic_reference")
    if revision != SUBJECT_RUNTIME_REFERENCE:
        raise K10ContractError("K10 frozen source revision identity mismatch")
    k6_protocol_path = _validate_frozen_source_path(audit.get("k6_protocol_path"))
    json_loader = frozen_json_loader or _load_frozen_json
    k6_protocol = json_loader(revision, k6_protocol_path)
    exposure = k6_protocol.get("pre_run_exposure", {}).get("representative_submission_validation")
    if (not isinstance(exposure, Mapping) or exposure.get("cell_count") != 7
            or len(exposure.get("cells", [])) != 7):
        raise K10ContractError("K6 disclosed engineering exposure metadata changed")
    sources = audit.get("disclosed_source_bindings")
    if not isinstance(sources, Mapping) or set(sources) != _DISCLOSED_SOURCE_PATHS:
        raise K10ContractError("K10 disclosed-source audit bindings are incomplete")
    reader = source_reader or _read_frozen_source_blob
    for relative, declaration in sources.items():
        relative = _validate_frozen_source_path(relative)
        if (not isinstance(declaration, Mapping)
                or re.fullmatch(r"[0-9a-f]{64}", str(declaration.get("sha256", ""))) is None
                or hashlib.sha256(reader(revision, relative)).hexdigest()
                != declaration.get("sha256")):
            raise K10ContractError(f"K10 disclosed submission source changed: {relative}")
        source_digests = {"sha256:" + value for value in declaration.get("request_content_digests", [])}
        if not source_digests or not source_digests.issubset(historical):
            raise K10ContractError(f"K10 disclosed submission identity is invalid: {relative}")
    selected = {item.canonical_request_digest for item in (inventory or load_k10_inventory())}
    overlap = sorted(selected.intersection(historical))
    if overlap:
        raise K10ContractError(f"selected K10 request was previously submitted: {overlap[0]}")
    result = {
        "definition": "previously effect-boundary-unsubmitted",
        "archive_trace_count": len(files),
        "historical_request_content_digests": sorted(historical),
        "selected_request_content_digests": sorted(selected),
        "selected_absent": True,
        "disclosed_source_bindings": sources,
        "read_only": True,
    }
    result["audit_digest"] = hashlib.sha256(canonical_bytes(result)).hexdigest()
    return result


@lru_cache(maxsize=8)
def _load_k10_protocol_cached(path: str) -> dict[str, Any]:
    protocol = _load_json(Path(path))
    protocol_digest = _validate_detached(protocol, "K10 protocol")
    pool_document = _load_json(CANDIDATE_POOL_PATH)
    inventory_document = _load_json(INVENTORY_PATH)
    schema_document = _load_json(RESULT_SCHEMA_PATH)
    pool_digest = _validate_detached(pool_document, "K10 candidate pool")
    inventory_digest = _validate_detached(inventory_document, "K10 inventory")
    schema_digest = _validate_result_schema(schema_document)
    load_k10_candidate_pool()
    inventory = load_k10_inventory()
    cells = build_k10_cells(inventory)
    if protocol_digest != PROTOCOL_DIGEST or (protocol.get("artifact_id"), protocol.get("artifact_version"),
            protocol.get("protocol_id"), protocol.get("protocol_version"),
            protocol.get("subject_runtime_semantic_reference")) != (
        "minecraft-k10-holdout-protocol", 1, "minecraft-eac-k10-holdout", 1,
        SUBJECT_RUNTIME_REFERENCE,
    ):
        raise K10ContractError("K10 protocol identity mismatch")
    if protocol.get("artifact_bindings") != {
        "candidate_pool": {"artifact_id": "minecraft-k10-candidate-pool", "artifact_version": 1,
                           "detached_artifact_sha256": pool_digest},
        "inventory": {"artifact_id": "minecraft-k10-action-inventory", "artifact_version": 1,
                      "detached_artifact_sha256": inventory_digest},
        "result_schema": {"artifact_id": "minecraft-k10-cell-trace-schema", "artifact_version": 1,
                          "detached_artifact_sha256": schema_digest},
        "selection_manifest_digest": SELECTION_MANIFEST_DIGEST,
    }:
        raise K10ContractError("K10 protocol artifact binding mismatch")
    if protocol.get("semantic_bindings") != _FROZEN_SEMANTIC_BINDINGS:
        raise K10ContractError("K10 semantic bindings differ from K8/K6")
    _validate_frozen_protected_content(protocol)
    if protocol.get("study_design") != {
        "iid_samples": False, "inventory_census": True, "runtime_seeds": False,
        "primary_cell_count": 80, "control_cell_count": 40,
        "total_cell_count": 120, "pair_count": 60,
        "confidence_intervals": False, "p_values": False,
    } or protocol.get("zero_pre_exposure") != {
        "engineering_validation_executed": True, "holdout_construction_executed": True,
        "holdout_effect_boundary_submissions": 0, "holdout_advisory_submissions": 0,
        "holdout_authority_submissions": 0, "holdout_full_census_executed": False,
        "holdout_aggregate_generated": False, "representative_pilot": False,
    }:
        raise K10ContractError("K10 study-design or zero-exposure declaration mismatch")
    if tuple(protocol.get("conditions", ())) != CONDITIONS:
        raise K10ContractError("K10 conditions changed")
    construction = protocol.get("cell_construction", {})
    if construction != {
        "inventory_order": [item.inventory_id for item in inventory],
        "primary_family_order": ["S1", "S2", "S3"],
        "s3_affected_actor_order": ["Alice", "Bob"],
        "condition_order": list(CONDITIONS), "control_family_order": ["C1", "C2"],
        "family_counts": {"S1": 20, "S2": 20, "S3": 40, "C1": 20, "C2": 20},
    } or len(cells) != 120:
        raise K10ContractError("K10 cell construction changed")
    if tuple(protocol.get("exact_action_invariant", {}).get("compared_fields", ())) != EXACT_FIELDS:
        raise K10ContractError("K10 exact-action invariant changed")
    if any(protocol.get("no_reconsideration_invariant", {}).get(name) != 0 for name in (
        "planner_calls", "model_calls", "controller_redecisions", "action_regenerations",
    )):
        raise K10ContractError("K10 no-reconsideration invariant changed")
    if protocol.get("reporting_separation", {}).get("pooling_forbidden") is not True:
        raise K10ContractError("K8/K10 pooling prohibition is missing")
    audit = audit_historical_submissions(protocol, inventory)
    result = dict(protocol)
    result.update({
        "validated_protocol_digest": protocol_digest,
        "validated_candidate_pool_digest": pool_digest,
        "validated_inventory_digest": inventory_digest,
        "validated_result_schema_digest": schema_digest,
        "validated_historical_audit_digest": audit["audit_digest"],
    })
    return result


def load_k10_protocol(path: str | Path = PROTOCOL_PATH) -> dict[str, Any]:
    """Return a detached copy of the historically authenticated K10 protocol."""
    return copy.deepcopy(_load_k10_protocol_cached(str(Path(path))))


def _selected_attestation(item: K10InventoryItem, protocol: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "attested": True,
        "definition": "previously effect-boundary-unsubmitted",
        "selected_request_digest": item.canonical_request_digest,
        "historical_audit_digest": protocol["validated_historical_audit_digest"],
    }


def _cell_record(cell: K10CellSpec) -> dict[str, Any]:
    return {name: getattr(cell, name) for name in (
        "cell_id", "scenario_family", "inventory_id", "condition", "affected_actor", "matrix",
    )}


def _validate_content_request(phase: Mapping[str, Any], item: K10InventoryItem) -> None:
    if (phase.get("action") != {"identity": item.action_identity, "version": item.action_version,
                                "digest": item.action_digest}
            or phase.get("arguments") != item.request()
            or phase.get("target") != item.target()
            or canonical_request_digest(phase["action"], phase["arguments"], phase["target"])
            != item.canonical_request_digest):
        raise K10ContractError("K10 trace request is not the selected inventory item")


def validate_k10_trace(
    trace: Mapping[str, Any], *, cell: K10CellSpec | None = None,
) -> dict[str, Any]:
    schema = _load_json(RESULT_SCHEMA_PATH)
    required = set(schema["required_top_level"])
    if set(trace) != required or trace.get("schema_version") != "minecraft-k10-cell-trace/1":
        raise K10ContractError("K10 trace top-level schema mismatch")
    protocol = load_k10_protocol()
    if (trace["protocol_digest"] != protocol["validated_protocol_digest"]
            or trace["candidate_pool_digest"] != protocol["validated_candidate_pool_digest"]
            or trace["inventory_digest"] != protocol["validated_inventory_digest"]
            or trace["result_schema_digest"] != protocol["validated_result_schema_digest"]
            or trace["selection_manifest_digest"] != SELECTION_MANIFEST_DIGEST
            or trace["semantic_bindings"] != protocol["semantic_bindings"]):
        raise K10ContractError("K10 trace binding mismatch")
    all_cells = {candidate.cell_id: candidate for candidate in build_k10_cells()}
    record = trace["cell"]
    expected_cell = cell or (all_cells.get(record.get("cell_id")) if isinstance(record, Mapping) else None)
    if expected_cell is None or record != _cell_record(expected_cell):
        raise K10ContractError("K10 trace cell identity mismatch")
    item = next(item for item in load_k10_inventory() if item.inventory_id == expected_cell.inventory_id)
    if trace["selected_request_digest"] != item.canonical_request_digest:
        raise K10ContractError("K10 selected request digest mismatch")
    if trace["previously_unsubmitted"] != _selected_attestation(item, protocol):
        raise K10ContractError("K10 previously-unsubmitted attestation mismatch")
    for phase, keys in k6._PHASE_KEYS.items():
        if not isinstance(trace[phase], Mapping) or set(trace[phase]) != keys:
            raise K10ContractError(f"K10 trace {phase} schema mismatch")
    if trace["r_p"]["EAdm"] is not True:
        raise K10ContractError("K10 action was not admissible at preparation")
    if any(trace["r_p"][name] != trace["r_e"][name] for name in EXACT_FIELDS):
        raise K10ContractError("K10 retained exact request identity changed")
    _validate_content_request(trace["r_p"], item)
    _validate_content_request(trace["r_e"], item)
    try:
        k6._validate_exact_request_digest(trace["r_p"])
        k6._validate_exact_request_digest(trace["r_e"])
    except k6.K6ContractError as exc:
        raise K10ContractError(str(exc).replace("K6", "K10")) from exc
    if trace["exact_action"] != {"same_prepared_object": True, "exact_action_preserved": True}:
        raise K10ContractError("K10 prepared action was reconstructed or substituted")
    freeze = trace["no_reconsideration"]
    if set(freeze) != {
        "planner_instantiated", "model_instantiated", "controller_instantiated",
        "planner_calls", "model_calls", "controller_redecisions", "action_regenerations",
    } or any(freeze[name] is not False for name in (
        "planner_instantiated", "model_instantiated", "controller_instantiated",
    )) or any(freeze[name] != 0 for name in (
        "planner_calls", "model_calls", "controller_redecisions", "action_regenerations",
    )):
        raise K10ContractError("K10 no-reconsideration invariant failed")
    if trace["r_e"]["EnvPre_oracle"] is not True or trace["r_e"]["SecPre_oracle"] is not True:
        raise K10ContractError("K10 detached precondition oracle failed")
    mutation = trace["mutation"]
    if mutation.get("hidden_truth_ingested") is not False:
        raise K10ContractError("K10 hidden evaluator truth entered runtime evidence")
    if trace["actor_scope"] != {
        "actor_id": expected_cell.affected_actor,
        "visible_to": [expected_cell.affected_actor], "private_actor_scope": True,
    }:
        raise K10ContractError("K10 actor scope mismatch")
    if (trace["r_e"]["current_EAdm"] is not trace["r_d"]["current_EAdm"]
            or trace["r_e"]["exact_action_submitted"] is not True):
        raise K10ContractError("K10 effect-submission phase is inconsistent")
    family = expected_cell.scenario_family
    if family == "S1":
        if (mutation.get("mutation_type") != "opposite_polarity_explicit_supersession"
                or mutation.get("superseded_root_id") is None
                or mutation.get("replacement_root_id") is None
                or mutation.get("contradiction") is not None
                or not k6._valid_supersession(mutation.get("supersession"), expected_cell.affected_actor)
                or mutation["supersession"]["old_root_id"] != mutation["superseded_root_id"]
                or mutation["supersession"]["new_root_id"] != mutation["replacement_root_id"]):
            raise K10ContractError("K10 S1 supersession contract mismatch")
    elif family == "S2":
        if (mutation.get("mutation_type") != "independent_opposite_trusted_tool_result"
                or mutation.get("superseded_root_id") is not None
                or mutation.get("contradiction") != {
                    "positive_current": True, "negative_current": True,
                    "positive_supersedes": [], "negative_supersedes": [],
                    "non_defeated": False,
                }):
            raise K10ContractError("K10 S2 contradiction contract mismatch")
    elif family == "S3":
        if (mutation.get("mutation_type") != "affected_actor_explicit_supersession"
                or not k6._valid_supersession(mutation.get("supersession"), expected_cell.affected_actor)
                or mutation["supersession"]["old_root_id"] != mutation.get("superseded_root_id")
                or mutation["supersession"]["new_root_id"] != mutation.get("replacement_root_id")):
            raise K10ContractError("K10 S3 selective-revision contract mismatch")
    elif family == "C1":
        if (mutation.get("mutation_type") != "unrelated_weather_visible_update"
                or trace["r_d"]["authority_epoch"] <= trace["r_p"]["authority_epoch"]):
            raise K10ContractError("K10 C1 unrelated-revision contract mismatch")
    elif family == "C2":
        before, after = mutation.get("evaluator_truth_before"), mutation.get("evaluator_truth_after")
        epochs = (mutation.get("authority_epoch_before"), mutation.get("authority_epoch_after"),
                  trace["r_p"]["authority_epoch"], trace["r_d"]["authority_epoch"],
                  trace["r_e"]["authority_epoch_before_execution"])
        evidence_before = mutation.get("evidence_total_before")
        evidence_after = mutation.get("evidence_total_after")
        if (mutation.get("mutation_type") != "evaluator_only_hidden_truth_mutation"
                or not isinstance(before, Mapping) or not isinstance(after, Mapping)
                or canonical_bytes(before) == canonical_bytes(after)
                or mutation.get("evaluator_truth_changed") is not True
                or mutation.get("evaluator_truth_before_digest") != hashlib.sha256(canonical_bytes(before)).hexdigest()
                or mutation.get("evaluator_truth_after_digest") != hashlib.sha256(canonical_bytes(after)).hexdigest()
                or mutation.get("evaluator_truth_authority_input") is not False
                or mutation.get("evaluator_truth_precondition_input") is not False
                or any(isinstance(epoch, bool) or not isinstance(epoch, int) for epoch in epochs)
                or len(set(epochs)) != 1
                or isinstance(evidence_before, bool) or not isinstance(evidence_before, int)
                or isinstance(evidence_after, bool) or not isinstance(evidence_after, int)
                or evidence_before != evidence_after
                or trace["r_d"]["current_EAdm"] is not True
                or trace["r_d"]["permit_or_shadow_fresh"] is not True
                or trace["r_e"]["permit_or_shadow_fresh"] is not True):
            raise K10ContractError("K10 C2 hidden-truth contract mismatch")
    if family != "C2" and (
        any(mutation.get(field) is not None for field in C2_EVALUATOR_FIELDS[:4])
        or mutation.get("evaluator_truth_changed") is not False
        or mutation.get("evaluator_truth_authority_input") is not False
        or mutation.get("evaluator_truth_precondition_input") is not False
    ):
        raise K10ContractError("K10 non-C2 trace contains evaluator-only truth")
    s3 = trace["s3"]
    if family == "S3":
        if not isinstance(s3, Mapping) or set(s3) != {
            "affected_actor", "unaffected_actor", "unaffected_current_EAdm",
            "unaffected_r_p", "unaffected_r_d", "unaffected_r_e",
            "unaffected_same_prepared_object", "unaffected_exact_action_preserved",
            "unaffected_mechanism_analysis", "cross_actor_dependency_leak",
            "cross_actor_state_change_leak",
        } or s3["affected_actor"] != expected_cell.affected_actor:
            raise K10ContractError("K10 S3 actor-isolation schema mismatch")
        other = "Bob" if expected_cell.affected_actor == "Alice" else "Alice"
        if s3["unaffected_actor"] != other:
            raise K10ContractError("K10 S3 unaffected actor mismatch")
        if (set(s3["unaffected_r_p"]) != k6._PHASE_KEYS["r_p"]
                or set(s3["unaffected_r_d"]) != k6._PHASE_KEYS["r_d"]
                or set(s3["unaffected_r_e"]) != k6._PHASE_KEYS["r_e"]):
            raise K10ContractError("K10 S3 unaffected phase schema mismatch")
        if (any(s3["unaffected_r_p"][name] != s3["unaffected_r_e"][name] for name in EXACT_FIELDS)
                or s3["unaffected_same_prepared_object"] is not True
                or s3["unaffected_exact_action_preserved"] is not True):
            raise K10ContractError("K10 S3 unaffected exact action changed")
        _validate_content_request(s3["unaffected_r_p"], item)
        _validate_content_request(s3["unaffected_r_e"], item)
        try:
            k6._validate_exact_request_digest(s3["unaffected_r_p"])
            k6._validate_exact_request_digest(s3["unaffected_r_e"])
            k6._validate_mechanism_semantics(
                s3["unaffected_mechanism_analysis"], s3["unaffected_r_p"],
                s3["unaffected_r_d"], s3["unaffected_r_e"], expected_cell.condition,
            )
        except k6.K6ContractError as exc:
            raise K10ContractError(str(exc).replace("K6", "K10")) from exc
        if (s3["unaffected_current_EAdm"] is not True
                or s3["unaffected_r_d"]["current_EAdm"] is not True
                or s3["unaffected_r_e"]["current_EAdm"] is not True
                or s3["unaffected_r_e"]["exact_action_submitted"] is not True
                or s3["unaffected_r_e"]["EnvPre_oracle"] is not True
                or s3["unaffected_r_e"]["SecPre_oracle"] is not True
                or mutation.get("cross_actor_dependency_leak") is not s3["cross_actor_dependency_leak"]
                or mutation.get("cross_actor_state_change_leak") is not s3["cross_actor_state_change_leak"]):
            raise K10ContractError("K10 S3 unaffected state mismatch")
    elif s3 is not None:
        raise K10ContractError("K10 non-S3 trace contains S3 fields")
    try:
        k6._validate_mechanism_semantics(
            trace["mechanism_analysis"], trace["r_p"], trace["r_d"], trace["r_e"],
            expected_cell.condition,
        )
    except k6.K6ContractError as exc:
        raise K10ContractError(str(exc).replace("K6", "K10")) from exc
    if (not isinstance(trace["pairing_digest"], str)
            or len(trace["pairing_digest"]) != 64
            or any(character not in "0123456789abcdef" for character in trace["pairing_digest"])
            or trace["pairing_digest"] != trace_pairing_digest(trace)):
        raise K10ContractError("K10 pairing digest mismatch")
    return dict(trace)


def trace_pairing_digest(trace: Mapping[str, Any]) -> str:
    return k6.trace_pairing_digest(trace)


def _fraction(numerator: int, denominator: int) -> dict[str, int]:
    return {"numerator": numerator, "denominator": denominator}


def _metric_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    return k6._metric_rows(rows)


def _mechanism_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return k6._mechanism_rows(rows)


def aggregate_k10_results(traces: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for trace in traces:
        row = validate_k10_trace(trace)
        cell_id = row["cell"]["cell_id"]
        if cell_id in seen:
            raise K10ContractError(f"duplicate K10 cell trace: {cell_id}")
        seen.add(cell_id)
        rows.append(row)
    rows.sort(key=lambda row: row["cell"]["cell_id"])
    pairs: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        cell = row["cell"]
        key = (cell["scenario_family"], cell["inventory_id"],
               cell["affected_actor"], cell["matrix"])
        pairs.setdefault(key, []).append(row)
    for pair in pairs.values():
        if len(pair) > 2 or (len(pair) == 2 and {
            row["cell"]["condition"] for row in pair
        } != set(CONDITIONS)):
            raise K10ContractError("K10 Advisory/Authority pair mismatch")
        if len(pair) == 2 and len({trace_pairing_digest(row) for row in pair}) != 1:
            raise K10ContractError("K10 Advisory/Authority pre-enforcement construction mismatch")
    expected_ids = {cell.cell_id for cell in build_k10_cells()}
    primary = sum(row["cell"]["matrix"] == "primary" for row in rows)
    control = sum(row["cell"]["matrix"] == "control" for row in rows)
    complete = (seen == expected_ids and len(rows) == 120 and primary == 80 and control == 40
                and len(pairs) == 60 and all(len(pair) == 2 for pair in pairs.values()))

    def grouped(field: str) -> dict[str, Any]:
        return {value: _metric_rows([row for row in rows if row["cell"][field] == value])
                for value in sorted({row["cell"][field] for row in rows})}

    def mechanisms(field: str) -> dict[str, Any]:
        return {value: _mechanism_rows([row for row in rows if row["cell"][field] == value])
                for value in sorted({row["cell"][field] for row in rows})}

    return {
        "schema_version": "minecraft-k10-aggregate/1",
        "iid_samples": False,
        "inventory_census": True,
        "confidence_intervals_added": False,
        "p_values_added": False,
        "k8_k10_pooled": False,
        "expected_primary_cells": 80,
        "observed_primary_cells": primary,
        "expected_control_cells": 40,
        "observed_control_cells": control,
        "expected_pair_count": 60,
        "observed_pair_count": sum(len(pair) == 2 for pair in pairs.values()),
        "complete": complete,
        "verdict": None,
        "overall": _metric_rows(rows),
        "by_family": grouped("scenario_family"),
        "by_inventory": grouped("inventory_id"),
        "by_condition": grouped("condition"),
        "mechanism_isolation": {
            "overall": _mechanism_rows(rows),
            "by_family": mechanisms("scenario_family"),
            "by_inventory": mechanisms("inventory_id"),
            "by_condition": mechanisms("condition"),
        },
    }
