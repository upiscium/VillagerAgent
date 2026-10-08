"""K12-only identities for the compatibility-spine gate."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import fields, is_dataclass
from enum import Enum
from collections.abc import Mapping
from typing import Any

from benchmarks.common.eac.canonical import (FrozenJSONArray, FrozenJSONObject,
                                              canonical_bytes, canonical_sha256)
from benchmarks.common.eac.canonical import thaw_json
from benchmarks.common.eac.model import EvidenceRoot, ExactRequest

PROTOCOL_IDENTITY = "minecraft-eac-k12-controlled-recovery/1"
FIXTURE_IDENTITY = "minecraft-k12-recovery-fixture-manifest/1"
RANDOMIZATION_IDENTITY = "minecraft-k12-recovery-randomization-manifest/1"
RESET_IDENTITY = "minecraft-k12-reset-attestation/1"
TRACE_IDENTITY = "minecraft-k12-recovery-trace/1"
VALIDATION_CONTRACT_IDENTITY = "minecraft-k12-recovery-validation-contract/1"
CELL_RESULT_IDENTITY = "minecraft-k12-recovery-cell-result/1"
AGGREGATE_IDENTITY = "minecraft-k12-recovery-aggregate/1"
ANALYSIS_IDENTITY = "minecraft-k12-recovery-analysis/1"
REQUEST_CANONICALIZATION_IDENTITY = "minecraft-k12-request-content-canonicalization/1"
AUTHORITY_REJECTION_SCHEMA = "eac-authority-rejection/1"
REQUEST_CONTENT_PLACEHOLDER_SCHEMA = (
    "minecraft-k12-gate-local-request-content-placeholder/1"
)
REQUEST_CONTENT_SCHEMA = REQUEST_CANONICALIZATION_IDENTITY
_SHA256 = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")

# This is intentionally local to the K12 request boundary.  A caller may use
# ``materialize_arguments`` with an explicit legacy spec, but scientific
# requests use this frozen table and never accept a caller-supplied schema.
FROZEN_ARGUMENT_SPECS: dict[str, dict[str, dict[str, Any]]] = {
    "MineBlock": {name: {"required": True, "type": "integer"} for name in ("x", "y", "z")},
    "placeBlock": {
        "item_name": {"required": True, "type": "string"},
        "x": {"required": True, "type": "integer"},
        "y": {"required": True, "type": "integer"},
        "z": {"required": True, "type": "integer"},
        "facing": {"required": True, "type": "string"},
    },
    "navigateTo": {name: {"required": True, "type": "integer"} for name in ("x", "y", "z")},
    "attackTarget": {
        "target_name": {"required": True, "type": "string"},
    },
    "handoverBlock": {
        "target_player_name": {"required": True, "type": "string"},
        "item_name": {"required": True, "type": "string"},
        "item_count": {"required": True, "type": "integer", "minimum": 1},
    },
}
def sha256_identity(value: Any) -> str:
    def json_value(item: Any) -> Any:
        if isinstance(item, tuple):
            return [json_value(child) for child in item]
        if isinstance(item, list):
            return [json_value(child) for child in item]
        if isinstance(item, dict):
            return {key: json_value(child) for key, child in item.items()}
        return item

    return canonical_sha256(json_value(value))


def exact_request_view(request: ExactRequest) -> dict[str, Any]:
    if not isinstance(request, ExactRequest):
        raise TypeError("ExactRequest is required")
    value = json.loads(request.identity_bytes().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("ExactRequest identity is not an object")
    return value


def exact_request_digest(request: ExactRequest) -> str:
    if not isinstance(request, ExactRequest):
        raise TypeError("ExactRequest is required")
    return "sha256:" + hashlib.sha256(request.identity_bytes()).hexdigest()


def authority_request_view(request: ExactRequest) -> dict[str, Any]:
    """Return the complete canonical authority projection used for admission."""
    if not isinstance(request, ExactRequest):
        raise TypeError("ExactRequest is required")
    def plain(value: Any) -> Any:
        if isinstance(value, (FrozenJSONArray, FrozenJSONObject)):
            return thaw_json(value)
        if is_dataclass(value):
            return {field.name: plain(getattr(value, field.name)) for field in fields(value)}
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, Mapping):
            return {str(key): plain(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [plain(item) for item in value]
        return value
    value = plain(request)
    if not isinstance(value, dict):
        raise ValueError("ExactRequest authority projection is not an object")
    return value


def authority_request_digest(request: ExactRequest) -> str:
    """Match the authority's immutable request commitment before execution."""
    return "sha256:" + hashlib.sha256(canonical_bytes(authority_request_view(request))).hexdigest()


def request_content_placeholder(
    request: Any, *, actor_id: str | None = None,
) -> tuple[str, str, bool]:
    """Return the Gate-557 request-content commitment (the tuple is legacy API)."""
    view = exact_request_view(request)
    request_actor = view["arguments"].get("player_name")
    if actor_id is None:
        actor_id = request_actor
    elif request_actor is not None and request_actor != actor_id:
        raise ValueError("request content actor identity mismatch")
    if not isinstance(actor_id, str) or not actor_id:
        raise ValueError("request content has no materialized actor identity")
    spec = FROZEN_ARGUMENT_SPECS.get(view["action"]["identity"], {})
    semantic_arguments = {name: view["arguments"][name] for name in spec
                          if name in view["arguments"]}
    return REQUEST_CONTENT_SCHEMA, canonical_request_content_digest(
        actor_id=actor_id, action=view["action"],
        arguments=semantic_arguments, target=view["target"], argument_spec=spec,
    ), True


def canonical_request_content(
    actor_id: str, action: Any, arguments: Any, target: Any,
    *, argument_spec: Any = None,
) -> dict[str, Any]:
    """Build the scientific, run-independent request-content projection.

    ``argument_spec`` is deliberately explicit: a missing argument is only
    materialized when its spec contains a ``default`` key.  In particular,
    ``None`` is a value, not an indication that a value was absent.
    """
    if not isinstance(actor_id, str) or not actor_id:
        raise ValueError("actor_id is required")
    if not isinstance(action, dict) or set(action) != {"identity", "version", "digest"}:
        raise ValueError("action identity must contain identity, version, and digest")
    if not isinstance(action["identity"], str) or not action["identity"]:
        raise ValueError("action identity is required")
    if action["identity"] not in FROZEN_ARGUMENT_SPECS:
        raise ValueError("action identity is outside the frozen K12 action set")
    if type(action["version"]) is not int or action["version"] <= 0:
        raise ValueError("action version must be a positive integer")
    if not isinstance(action["digest"], str) or not _SHA256.fullmatch(action["digest"]):
        raise ValueError("action digest must be a SHA-256 digest")
    if not isinstance(arguments, dict):
        raise TypeError("arguments must be an object")
    if argument_spec is not None and not isinstance(argument_spec, dict):
        raise TypeError("argument_spec must be an object")
    materialized = dict(arguments)
    unknown = set(materialized) - set(argument_spec or {})
    if unknown:
        raise ValueError(f"unsupported semantic arguments: {sorted(unknown)}")
    for name, spec in (argument_spec or {}).items():
        if not isinstance(name, str) or not isinstance(spec, dict):
            raise TypeError("argument specs must be objects")
        if name not in materialized and "default" in spec:
            materialized[name] = spec["default"]
        if spec.get("required") is True and name not in materialized:
            raise ValueError(f"required argument is absent: {name}")
        if name in materialized:
            item = materialized[name]
            if spec.get("type") == "integer" and type(item) is not int:
                raise TypeError(f"argument must be an integer: {name}")
            if spec.get("type") == "string" and not isinstance(item, str):
                raise TypeError(f"argument must be a string: {name}")
            if "minimum" in spec and item < spec["minimum"]:
                raise ValueError(f"argument is below its minimum: {name}")
    # canonical_bytes performs the constrained numeric/type validation and
    # retains list order.  Do not use dict.get: absent and JSON null differ.
    value = {"actor_id": actor_id, "action": dict(action),
             "arguments": materialized, "target": target}
    from benchmarks.common.eac.canonical import canonical_bytes
    canonical_bytes(value)
    return value


def canonical_request_content_digest(
    actor_id: str, action: Any, arguments: Any, target: Any,
    *, argument_spec: Any = None,
) -> str:
    return canonical_sha256(canonical_request_content(
        actor_id, action, arguments, target, argument_spec=argument_spec,
    ))


def verify_request_content_repetition(first: Any, second: Any) -> bool:
    """Require repeated observations to have identical scientific content."""
    if not isinstance(first, dict) or not isinstance(second, dict):
        raise TypeError("request-content projections must be objects")
    left = canonical_sha256(first)
    right = canonical_sha256(second)
    if left != right:
        raise ValueError("repeated request content differs")
    return True


def evidence_root_digest(root: EvidenceRoot) -> str:
    if not isinstance(root, EvidenceRoot):
        raise TypeError("EvidenceRoot is required")
    proposition = root.proposition
    value = {
        "root_id": root.root_id,
        "root_type": root.root_type,
        "proposition": {
            "key": {
                "namespace": proposition.key.namespace,
                "predicate": proposition.key.predicate,
                "arguments": [thaw_json(item) for item in proposition.key.arguments],
                "temporal_scope": proposition.key.temporal_scope,
            },
            "polarity": proposition.polarity,
        },
        "source": root.source,
        "revision": root.revision,
        "visible_to": list(root.visible_to),
        "provenance_id": root.provenance_id,
        "supersedes": list(root.supersedes),
        "source_lineage_id": root.source_lineage_id,
        "upstream_origin_id": root.upstream_origin_id,
        "valid": root.valid,
        "current": root.current,
        "source_stream_id": root.source_stream_id,
        "source_stream_revision": root.source_stream_revision,
        "issuer": root.issuer,
        "mapping_rule_id": root.mapping_rule_id,
        "originating_action_identity": root.originating_action_identity,
        "evidence_gathering_action": root.evidence_gathering_action,
    }
    return sha256_identity(value)


__all__ = [
    "AUTHORITY_REJECTION_SCHEMA",
    "REQUEST_CONTENT_PLACEHOLDER_SCHEMA",
    "REQUEST_CONTENT_SCHEMA",
    "canonical_request_content",
    "canonical_request_content_digest",
    "exact_request_digest",
    "exact_request_view",
    "evidence_root_digest",
    "request_content_placeholder",
    "verify_request_content_repetition",
    "sha256_identity",
]
