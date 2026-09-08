"""K12-only identities for the compatibility-spine gate."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from benchmarks.common.eac.canonical import canonical_sha256
from benchmarks.common.eac.canonical import thaw_json
from benchmarks.common.eac.model import EvidenceRoot, ExactRequest

AUTHORITY_REJECTION_SCHEMA = "eac-authority-rejection/1"
REQUEST_CONTENT_PLACEHOLDER_SCHEMA = (
    "minecraft-k12-gate-local-request-content-placeholder/1"
)


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


def request_content_placeholder(request: Any) -> tuple[str, str, bool]:
    """Return the explicitly non-scientific Gate-557 content placeholder."""
    view = exact_request_view(request)
    content = {key: view[key] for key in ("action", "arguments", "target")}
    return REQUEST_CONTENT_PLACEHOLDER_SCHEMA, sha256_identity(content), False


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
    "exact_request_digest",
    "exact_request_view",
    "evidence_root_digest",
    "request_content_placeholder",
    "sha256_identity",
]
