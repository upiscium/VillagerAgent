"""Small, parent-owned evidence registry for the K12 recovery campaign."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypeVar

from benchmarks.common.eac.canonical import FrozenJSONArray, FrozenJSONObject
from benchmarks.common.eac.canonical import canonical_argument, canonical_sha256

_ID_DOMAIN = "minecraft-k12-evidence-id/1"
_RECORD_DOMAIN = "minecraft-k12-evidence-record/1"
_SNAPSHOT_DOMAIN = "minecraft-k12-evidence-snapshot/1"
EVIDENCE_KINDS = frozenset({"reset", "authority_rejection", "model_call",
                            "backend_effect", "containment", "oracle", "finalization",
                            "campaign_stop"})


def _text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value or not value.isascii():
        raise ValueError(f"{name} must be a non-empty ASCII string")
    return value


@dataclass(frozen=True, slots=True)
class CellBinding:
    protocol: str
    campaign: str
    cohort: str
    cell: str
    triplet: str
    arm: str
    fixture: str
    template: str
    seed: int
    reset_token: str
    generation: int
    attestation: str

    def __post_init__(self) -> None:
        for name in ("protocol", "campaign", "cohort", "cell", "triplet", "arm",
                     "fixture", "template", "reset_token", "attestation"):
            _text(name, getattr(self, name))
        if type(self.seed) is not int or self.seed < 0:
            raise ValueError("seed must be a non-negative integer")
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("generation must be a non-negative integer")

    def canonical(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True, init=False)
class EvidenceRecord:
    kind: str
    id: str
    binding: CellBinding
    payload: Any
    digest: str = field(init=False)

    def __init__(self, kind: str, id: str, binding: CellBinding, payload: Any) -> None:
        kind = _text("kind", kind)
        if kind not in EVIDENCE_KINDS:
            raise ValueError(f"unsupported K12 evidence kind: {kind}")
        _text("id", id)
        if not isinstance(binding, CellBinding):
            raise TypeError("binding must be a CellBinding")
        frozen = canonical_argument(payload)
        if id != _evidence_id(kind, binding, frozen):
            raise ValueError("evidence ID is not the deterministic K12 ID")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "id", id)
        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "payload", frozen)
        object.__setattr__(self, "digest", canonical_sha256(self._unsigned()))

    def _unsigned(self) -> dict[str, Any]:
        return {"domain": _RECORD_DOMAIN, "kind": self.kind, "id": self.id,
                "binding": self.binding.canonical(), "payload": self.payload}

    def verify(self) -> bool:
        return (self.id == _evidence_id(self.kind, self.binding, self.payload)
                and self.digest == canonical_sha256(self._unsigned()))


def _evidence_id(kind: str, binding: CellBinding, payload: Any) -> str:
    return canonical_sha256({"domain": _ID_DOMAIN, "kind": kind,
                             "binding": binding.canonical(), "payload": payload})


@dataclass(frozen=True, slots=True, init=False)
class EvidenceSnapshot:
    records: tuple[EvidenceRecord, ...]
    digest: str = field(init=False)

    def __init__(self, records: tuple[EvidenceRecord, ...]) -> None:
        records = tuple(records)
        if any(not isinstance(record, EvidenceRecord) for record in records):
            raise TypeError("snapshot records must be EvidenceRecord instances")
        if len({record.id for record in records}) != len(records):
            raise ValueError("snapshot contains duplicate evidence IDs")
        if records and any(record.binding != records[0].binding for record in records[1:]):
            raise ValueError("snapshot contains mixed cell bindings")
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "digest", canonical_sha256(self._unsigned()))

    def _unsigned(self) -> dict[str, Any]:
        return {"domain": _SNAPSHOT_DOMAIN,
                "records": [{"id": record.id, "digest": record.digest}
                            for record in sorted(self.records, key=lambda item: item.id)]}

    def require(self, kind: str, evidence_id: str, expected_type: type[EvidenceRecord] = EvidenceRecord) -> EvidenceRecord:
        if not isinstance(expected_type, type) or not issubclass(expected_type, EvidenceRecord):
            raise TypeError("expected_type must be an EvidenceRecord type")
        for record in self.records:
            if record.id == evidence_id:
                if record.kind != kind:
                    raise TypeError(f"evidence {evidence_id} has kind {record.kind}, not {kind}")
                if not isinstance(record, expected_type):
                    raise TypeError("evidence record has the wrong type")
                return record
        raise KeyError(evidence_id)

    def verify(self) -> bool:
        return all(record.verify() for record in self.records) and self.digest == canonical_sha256(self._unsigned())


T = TypeVar("T", bound=EvidenceRecord)


class EvidenceRegistry:
    """Mutable during collection; ``freeze`` produces the immutable boundary."""

    def __init__(self, binding: CellBinding) -> None:
        if not isinstance(binding, CellBinding):
            raise TypeError("binding must be a CellBinding")
        self.binding = binding
        self._records: dict[str, EvidenceRecord] = {}
        self._frozen = False

    @property
    def records(self) -> tuple[EvidenceRecord, ...]:
        return tuple(self._records.values())

    def register(self, kind: str | EvidenceRecord, payload: Any = None, *,
                 binding: CellBinding | None = None, evidence_id: str | None = None) -> EvidenceRecord:
        if self._frozen:
            raise RuntimeError("evidence registry is frozen")
        if isinstance(kind, EvidenceRecord):
            if payload is not None or binding is not None or evidence_id is not None:
                raise TypeError("record registration accepts no additional arguments")
            record = kind
            if record.binding != self.binding:
                raise ValueError("evidence binding does not match registry binding")
            expected_id = _evidence_id(record.kind, record.binding, record.payload)
            if record.id != expected_id:
                raise ValueError("evidence ID is not the deterministic K12 ID")
        else:
            actual_binding = self.binding if binding is None else binding
            if actual_binding != self.binding:
                raise ValueError("evidence binding does not match registry binding")
            frozen_payload = canonical_argument(payload)
            expected_id = _evidence_id(kind, actual_binding, frozen_payload)
            if evidence_id is not None and evidence_id != expected_id:
                raise ValueError("caller-supplied evidence ID is not deterministic")
            record = EvidenceRecord(kind, expected_id, actual_binding, frozen_payload)
        if record.id in self._records:
            raise ValueError("duplicate evidence ID")
        self._records[record.id] = record
        return record

    def require(self, kind: str, evidence_id: str, expected_type: type[T] = EvidenceRecord) -> T:
        return self.freeze_preview().require(kind, evidence_id, expected_type)  # type: ignore[return-value]

    def freeze_preview(self) -> EvidenceSnapshot:
        return EvidenceSnapshot(tuple(self._records.values()))

    def freeze(self) -> EvidenceSnapshot:
        if self._frozen:
            raise RuntimeError("evidence registry is already frozen")
        snapshot = self.freeze_preview()
        self._frozen = True
        return snapshot


__all__ = ["CellBinding", "EvidenceRecord", "EvidenceRegistry", "EvidenceSnapshot", "EVIDENCE_KINDS"]
