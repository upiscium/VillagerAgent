"""Authenticated, deeply immutable identities used by the K12 live path."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping
from types import MappingProxyType

from benchmarks.common.eac.canonical import canonical_sha256


class FrozenMapping(Mapping[str, Any]):
    """A recursively frozen JSON mapping (and not a forgeable dict)."""
    __slots__ = ("_values",)

    def __init__(self, value: Mapping[str, Any]) -> None:
        object.__setattr__(self, "_values", MappingProxyType({k: freeze(v) for k, v in value.items()}))

    def __getitem__(self, key: str) -> Any: return self._values[key]
    def __iter__(self) -> Iterator[str]: return iter(self._values)
    def __len__(self) -> int: return len(self._values)
    def __setattr__(self, *_: Any) -> None: raise TypeError("immutable artifact")


def freeze(value: Any) -> Any:
    if isinstance(value, Mapping): return FrozenMapping(value)
    if isinstance(value, (list, tuple)): return tuple(freeze(v) for v in value)
    return value


def thaw(value: Any) -> Any:
    if isinstance(value, FrozenMapping): return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, tuple): return [thaw(v) for v in value]
    return value


@dataclass(frozen=True, slots=True)
class DomainIdentity:
    profile_digest: str
    campaign_id: str
    cell_id: str = ""
    reset_identity: str = ""
    evidence_digest: str = ""
    identity: str = field(init=False)

    def __post_init__(self) -> None:
        if not all(isinstance(v, str) and v for v in
                   (self.profile_digest, self.campaign_id)):
            raise ValueError("authenticated profile and campaign identities are required")
        object.__setattr__(self, "identity", canonical_sha256({
            "profile_digest": self.profile_digest, "campaign_id": self.campaign_id,
            "cell_id": self.cell_id, "reset_identity": self.reset_identity,
            "evidence_digest": self.evidence_digest}))


@dataclass(frozen=True, slots=True)
class LiveArtifact:
    kind: str
    cells: tuple[Mapping[str, Any], ...]
    identity: DomainIdentity | None = None
    rejected_by_final_gates: bool = True
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        frozen = tuple(freeze(cell) for cell in self.cells)
        object.__setattr__(self, "cells", frozen)
        object.__setattr__(self, "digest", canonical_sha256({
            "kind": self.kind, "cells": [thaw(c) for c in frozen],
            "domain": self.identity.identity if self.identity else None}))

    @property
    def remaining_slots(self) -> int: return max(0, 90 - len(self.cells))

    def final_launch_ready(self) -> bool:
        return False  # Only FinalGateInput is accepted by the final gate.


__all__ = ["DomainIdentity", "FrozenMapping", "LiveArtifact", "freeze", "thaw"]
