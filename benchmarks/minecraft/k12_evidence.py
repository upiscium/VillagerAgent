"""Small, parent-owned evidence registry for the K12 recovery campaign."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypeVar

from benchmarks.common.eac.canonical import FrozenJSONArray, FrozenJSONObject
from benchmarks.common.eac.canonical import canonical_argument, canonical_sha256
from benchmarks.minecraft.k12_execution_provenance import (
    AuthorityBinding,
    INJECTED_FAKE_ORIGIN,
    INJECTED_TEST_ORIGIN,
    RUNTIME_VERIFIED_ORIGIN,
)
from benchmarks.minecraft.k12_guarded_backend import (
    authority_binding_is_current,
    is_mock_authority_binding,
    mock_authority_binding,
    resolve_authority_binding,
)

_ID_DOMAIN = "minecraft-k12-evidence-id/1"
_RECORD_DOMAIN = "minecraft-k12-evidence-record/1"
_SNAPSHOT_DOMAIN = "minecraft-k12-evidence-snapshot/1"
EVIDENCE_KINDS = frozenset({"reset", "authority_rejection", "model_call",
                            "backend_effect", "containment", "oracle", "finalization",
                            "campaign_stop", "live_containment", "live_validation",
                            "live_artifact", "live_qualification"})


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
    authority_binding: AuthorityBinding = field(default_factory=mock_authority_binding)
    authority: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in ("protocol", "campaign", "cohort", "cell", "triplet", "arm",
                     "fixture", "template", "reset_token", "attestation"):
            _text(name, getattr(self, name))
        if type(self.seed) is not int or self.seed < 0:
            raise ValueError("seed must be a non-negative integer")
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("generation must be a non-negative integer")
        if self.authority is not None:
            derived = resolve_authority_binding(self.authority)
            owner_cell = getattr(self.authority, "cell_id", None)
            if owner_cell is not None and owner_cell != self.cell:
                raise ValueError("evidence cell/authority binding mismatch")
            if is_mock_authority_binding(self.authority_binding):
                object.__setattr__(self, "authority_binding", derived)
            elif self.authority_binding != derived:
                raise ValueError("evidence authority binding does not match owner")
        if not isinstance(self.authority_binding, AuthorityBinding):
            raise TypeError("authority_binding must be an AuthorityBinding")

    def canonical(self) -> dict[str, Any]:
        return {
            name: (
                self.authority_binding.canonical()
                if name == "authority_binding"
                else getattr(self, name)
            )
            for name in self.__dataclass_fields__
            if name != "authority"
        }

    @property
    def binding(self) -> AuthorityBinding:
        return self.authority_binding

    @property
    def origin(self) -> str:
        return self.authority_binding.origin

    @property
    def evidence_origin(self) -> str:
        return self.origin

    @property
    def runtime_admissible(self) -> bool:
        return self.authority_binding.runtime_admissible


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

    @property
    def authority_binding(self) -> AuthorityBinding:
        return self.binding.authority_binding

    @property
    def binding_authority(self) -> AuthorityBinding:
        return self.authority_binding

    @property
    def origin(self) -> str:
        return self.authority_binding.origin

    @property
    def evidence_origin(self) -> str:
        return self.origin


def _evidence_id(kind: str, binding: CellBinding, payload: Any) -> str:
    return canonical_sha256({"domain": _ID_DOMAIN, "kind": kind,
                             "binding": binding.canonical(), "payload": payload})


@dataclass(frozen=True, slots=True, init=False)
class EvidenceSnapshot:
    records: tuple[EvidenceRecord, ...]
    digest: str = field(init=False)
    _authority: Any = field(default=None, repr=False, compare=False)

    def __init__(self, records: tuple[EvidenceRecord, ...], authority: Any = None) -> None:
        records = tuple(records)
        if any(not isinstance(record, EvidenceRecord) for record in records):
            raise TypeError("snapshot records must be EvidenceRecord instances")
        if len({record.id for record in records}) != len(records):
            raise ValueError("snapshot contains duplicate evidence IDs")
        if records and any(record.binding != records[0].binding for record in records[1:]):
            raise ValueError("snapshot contains mixed cell bindings")
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "digest", canonical_sha256(self._unsigned()))
        object.__setattr__(self, "_authority", authority)

    def _unsigned(self) -> dict[str, Any]:
        return {"domain": _SNAPSHOT_DOMAIN,
                "records": [{"id": record.id, "digest": record.digest}
                             for record in sorted(self.records, key=lambda item: item.id)]}

    @property
    def authority_binding(self) -> AuthorityBinding | None:
        return self.records[0].authority_binding if self.records else None

    @property
    def origin(self) -> str | None:
        return self.records[0].origin if self.records else None

    @property
    def evidence_origin(self) -> str | None:
        return self.origin

    @property
    def runtime_admissible(self) -> bool:
        if self.origin != RUNTIME_VERIFIED_ORIGIN:
            return False
        try:
            self._assert_current()
        except ValueError:
            return False
        return True

    def _assert_current(self) -> None:
        binding = self.authority_binding
        if binding is None or is_mock_authority_binding(binding):
            return
        if self._authority is None or not authority_binding_is_current(
            self._authority, binding,
            profile_digest=getattr(self._authority, "profile_digest", None),
            allow_injected=binding.origin in {INJECTED_FAKE_ORIGIN, INJECTED_TEST_ORIGIN},
        ):
            raise ValueError("evidence authority lifecycle is stale or revoked")

    def require(self, kind: str, evidence_id: str, expected_type: type[EvidenceRecord] = EvidenceRecord) -> EvidenceRecord:
        self._assert_current()
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
        try:
            self._assert_current()
        except ValueError:
            return False
        return all(record.verify() for record in self.records) and self.digest == canonical_sha256(self._unsigned())


T = TypeVar("T", bound=EvidenceRecord)


class EvidenceRegistry:
    """Mutable during collection; ``freeze`` produces the immutable boundary."""

    def __init__(self, binding: CellBinding, *, authority: Any = None) -> None:
        if not isinstance(binding, CellBinding):
            raise TypeError("binding must be a CellBinding")
        if authority is not None:
            derived = resolve_authority_binding(authority)
            if is_mock_authority_binding(binding.authority_binding):
                binding = CellBinding(
                    binding.protocol, binding.campaign, binding.cohort, binding.cell,
                    binding.triplet, binding.arm, binding.fixture, binding.template,
                    binding.seed, binding.reset_token, binding.generation,
                    binding.attestation, derived, authority,
                )
            elif binding.authority_binding != derived:
                raise ValueError("evidence authority binding does not match owner")
        self.binding = binding
        self.authority = authority if authority is not None else binding.authority
        self._records: dict[str, EvidenceRecord] = {}
        self._frozen = False

    @property
    def records(self) -> tuple[EvidenceRecord, ...]:
        return tuple(self._records.values())

    @property
    def authority_binding(self) -> AuthorityBinding:
        return self.binding.authority_binding

    def _assert_current(self) -> None:
        if is_mock_authority_binding(self.authority_binding):
            return
        if self.authority is None or not authority_binding_is_current(
            self.authority,
            self.authority_binding,
            profile_digest=getattr(self.authority, "profile_digest", None),
            allow_injected=self.authority_binding.origin in {
                INJECTED_FAKE_ORIGIN, INJECTED_TEST_ORIGIN,
            },
        ):
            raise ValueError("evidence authority lifecycle is stale or revoked")

    def register(self, kind: str | EvidenceRecord, payload: Any = None, *,
                 binding: CellBinding | None = None, evidence_id: str | None = None) -> EvidenceRecord:
        if self._frozen:
            raise RuntimeError("evidence registry is frozen")
        self._assert_current()
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
        self._assert_current()
        return EvidenceSnapshot(tuple(self._records.values()), self.authority)

    def freeze(self) -> EvidenceSnapshot:
        if self._frozen:
            raise RuntimeError("evidence registry is already frozen")
        self._assert_current()
        snapshot = self.freeze_preview()
        self._frozen = True
        return snapshot


__all__ = ["AuthorityBinding", "CellBinding", "EvidenceRecord", "EvidenceRegistry", "EvidenceSnapshot", "EVIDENCE_KINDS"]
