"""Scientific request-content identity for Minecraft K12."""
from __future__ import annotations

from typing import Any, Mapping

from benchmarks.common.eac.canonical import canonical_sha256
from benchmarks.minecraft.k12_identity import (
    FROZEN_ARGUMENT_SPECS,
    REQUEST_CONTENT_SCHEMA,
    canonical_request_content,
    canonical_request_content_digest,
    verify_request_content_repetition,
)


class K12RequestError(ValueError):
    pass


def materialize_arguments(arguments: Mapping[str, Any], spec: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(arguments, Mapping):
        raise K12RequestError("arguments must be an object")
    try:
        value = dict(arguments)
        for name, declaration in (spec or {}).items():
            if name not in value and isinstance(declaration, Mapping) and "default" in declaration:
                value[name] = declaration["default"]
        canonical_sha256(value)
    except (TypeError, ValueError) as exc:
        raise K12RequestError(str(exc)) from exc
    return value


def request_content(
    actor_id: str, action: Mapping[str, Any], arguments: Mapping[str, Any], target: Any,
) -> dict[str, Any]:
    try:
        action_value = dict(action)
        # The scientific path is authoritative: callers cannot supply schemas
        # or invent defaults that change request identity.
        frozen_spec = FROZEN_ARGUMENT_SPECS.get(action_value.get("identity"), {})
        return canonical_request_content(actor_id, action_value, dict(arguments), target,
                                         argument_spec=frozen_spec)
    except (TypeError, ValueError) as exc:
        raise K12RequestError(str(exc)) from exc


def request_content_digest(*args: Any, **kwargs: Any) -> str:
    try:
        return canonical_sha256(request_content(*args, **kwargs))
    except (TypeError, ValueError) as exc:
        raise K12RequestError(str(exc)) from exc


def repetition_check(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    try:
        return verify_request_content_repetition(dict(first), dict(second))
    except (TypeError, ValueError) as exc:
        raise K12RequestError(str(exc)) from exc


__all__ = ["K12RequestError", "materialize_arguments", "request_content",
           "request_content_digest", "repetition_check", "REQUEST_CONTENT_SCHEMA"]
