"""Dependency-neutral nominal contracts for the K12 authority boundary.

The qualification implementation is a later scope than the provenance
implementation.  This module is consequently deliberately boring: it knows
neither implementation, and it carries only the nominal, object-identity-safe
handles that the parent needs when it consumes a qualification result.

In particular, a mapping with the same keys as a qualification aggregate is
not a qualification aggregate.  A qualification implementation must derive a
``QualificationEvidenceContract`` and install the projection/receipt pair
through the private mint boundary below.  Consumers can then authenticate the
exact aggregate, terminal evidence, authority, and controller objects without
importing the later qualification module.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import sys
from typing import Any

from benchmarks.common.eac.canonical import canonical_sha256


_PRE_RELOAD_ABI = globals().get("_STABLE_CONTRACT_ABI")
_CONTRACT_MINT_TOKEN = object()
_PROJECTION_MINT_TOKEN = object()
_RECEIPT_MINT_TOKEN = object()
_CANONICAL_QUALIFICATION_EVIDENCE_TYPE = globals().get(
    "_CANONICAL_QUALIFICATION_EVIDENCE_TYPE"
)
_PENDING_QUALIFICATION_EVIDENCE_TYPE = None


class QualificationTerminalEvidenceContract:
    """Nominal base for a parent-verified terminal qualification event."""

    __slots__ = ()


class _QualificationEvidenceMeta(type):
    def __new__(mcls, name: str, bases: tuple[type, ...], namespace: dict[str, Any],
                **kwargs: Any) -> type:
        global _CANONICAL_QUALIFICATION_EVIDENCE_TYPE
        global _PENDING_QUALIFICATION_EVIDENCE_TYPE
        module_name = namespace.get("__module__")
        if (name == "LiveQualificationAggregate"
                and module_name == "benchmarks.minecraft.k12_live_qualification"):
            existing = _CANONICAL_QUALIFICATION_EVIDENCE_TYPE
            module = sys.modules.get(module_name)
            caller_globals = sys._getframe(1).f_globals
            module_definition = module is not None and caller_globals is module.__dict__
            dataclass_slot_rebuild = (
                "__slots__" in namespace
                and existing is _PENDING_QUALIFICATION_EVIDENCE_TYPE
            )
            if not module_definition and not dataclass_slot_rebuild:
                raise TypeError("canonical qualification evidence type is module-owned")
            if (existing is not None and module_definition
                    and getattr(module, name, None) not in {None, existing}):
                raise TypeError("canonical qualification evidence type is already bound")
            value = super().__new__(mcls, name, bases, namespace, **kwargs)
            _CANONICAL_QUALIFICATION_EVIDENCE_TYPE = value
            _PENDING_QUALIFICATION_EVIDENCE_TYPE = (
                None if dataclass_slot_rebuild else value
            )
            return value
        return super().__new__(mcls, name, bases, namespace, **kwargs)


class QualificationEvidenceContract(metaclass=_QualificationEvidenceMeta):
    """Nominal base for a parent-owned qualification aggregate.

    The two installed handles are deliberately kept in base-class slots.  A
    caller cannot make an arbitrary namespace or dataclass look like an
    authenticated aggregate merely by adding similarly named attributes.
    """

    __slots__ = ("_qualification_projection", "_qualification_receipt")

    @property
    def qualification_projection(self) -> "QualificationEvidenceProjection":
        try:
            return self._qualification_projection
        except AttributeError as exc:  # pragma: no cover - defensive boundary
            raise TypeError("qualification evidence contract is not installed") from exc

    @property
    def ownership_receipt(self) -> "QualificationAggregateOwnershipReceipt":
        try:
            return self._qualification_receipt
        except AttributeError as exc:  # pragma: no cover - defensive boundary
            raise TypeError("qualification evidence contract is not installed") from exc

    @property
    def terminal_evidence(self) -> QualificationTerminalEvidenceContract:
        return self.qualification_projection.terminal_evidence


def is_canonical_qualification_evidence(value: Any) -> bool:
    """Return whether ``value`` has the one nominal live aggregate type."""

    return (type(value) is _CANONICAL_QUALIFICATION_EVIDENCE_TYPE
            and _CANONICAL_QUALIFICATION_EVIDENCE_TYPE is not None)


def _binding_identity(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("canonical authority binding is required")
    return dict(value)


@dataclass(frozen=True, slots=True, init=False)
class QualificationEvidenceProjection:
    """Immutable public projection minted beside one concrete aggregate.

    ``aggregate``/``authority``/``controller`` are intentionally retained as
    object references for identity checks and are excluded from the canonical
    digest.  The projection is not a replacement for the aggregate; it is the
    dependency-neutral view used by the parent authority.
    """

    aggregate_identity: str
    probe_aggregate_digest: str
    qualification_terminal_ledger_digest: str
    profile_digest: str
    authority_binding: Any
    authority_binding_canonical: Mapping[str, Any]
    evidence_origin: str
    execution_provenance: str
    passed: bool
    aggregate: QualificationEvidenceContract = field(repr=False, compare=False)
    terminal_evidence: QualificationTerminalEvidenceContract = field(
        repr=False, compare=False
    )
    authority: Any = field(repr=False, compare=False)
    controller: Any = field(repr=False, compare=False)
    _aggregate_marker: object = field(repr=False, compare=False)
    identity: str = field(init=False)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise TypeError("qualification projections are aggregate-minted")

    @classmethod
    def _mint(
        cls,
        aggregate: QualificationEvidenceContract,
        *,
        aggregate_identity: str,
        probe_aggregate_digest: str,
        qualification_terminal_ledger_digest: str,
        profile_digest: str,
        authority_binding: Any,
        authority_binding_canonical: Mapping[str, Any],
        evidence_origin: str,
        execution_provenance: str,
        passed: bool,
        terminal_evidence: QualificationTerminalEvidenceContract,
        authority: Any,
        controller: Any,
        aggregate_marker: object,
        token: object,
    ) -> "QualificationEvidenceProjection":
        if token is not _PROJECTION_MINT_TOKEN:
            raise TypeError("qualification projections are aggregate-minted")
        if not isinstance(aggregate, QualificationEvidenceContract):
            raise TypeError("nominal qualification evidence is required")
        if not isinstance(terminal_evidence, QualificationTerminalEvidenceContract):
            raise TypeError("nominal terminal evidence is required")
        canonical_binding = _binding_identity(authority_binding_canonical)
        value = object.__new__(cls)
        for name, item in (
            ("aggregate_identity", aggregate_identity),
            ("probe_aggregate_digest", probe_aggregate_digest),
            ("qualification_terminal_ledger_digest", qualification_terminal_ledger_digest),
            ("profile_digest", profile_digest),
            ("authority_binding", authority_binding),
            ("authority_binding_canonical", canonical_binding),
            ("evidence_origin", evidence_origin),
            ("execution_provenance", execution_provenance),
            ("passed", passed),
            ("aggregate", aggregate),
            ("terminal_evidence", terminal_evidence),
            ("authority", authority),
            ("controller", controller),
            ("_aggregate_marker", aggregate_marker),
        ):
            object.__setattr__(value, name, item)
        object.__setattr__(
            value,
            "identity",
            canonical_sha256(
                {
                    "artifact": "minecraft-k12-live-qualification-evidence-projection/1",
                    "aggregate": aggregate_identity,
                    "probe": probe_aggregate_digest,
                    "terminal": qualification_terminal_ledger_digest,
                    "profile": profile_digest,
                    "authority_binding": canonical_binding,
                    "evidence_origin": evidence_origin,
                    "execution_provenance": execution_provenance,
                    "passed": passed,
                }
            ),
        )
        return value


@dataclass(frozen=True, slots=True, init=False)
class QualificationAggregateOwnershipReceipt:
    """Object-identity-safe receipt for one qualification aggregate."""

    aggregate_identity: str
    projection_identity: str
    authority_identity: str
    authority_binding: Any
    evidence_origin: str
    aggregate: QualificationEvidenceContract = field(repr=False, compare=False)
    projection: QualificationEvidenceProjection = field(repr=False, compare=False)
    terminal_evidence: QualificationTerminalEvidenceContract = field(
        repr=False, compare=False
    )
    authority: Any = field(repr=False, compare=False)
    controller: Any = field(repr=False, compare=False)
    _aggregate_marker: object = field(repr=False, compare=False)
    identity: str = field(init=False)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise TypeError("aggregate ownership receipts are aggregate-minted")

    @classmethod
    def _mint(
        cls,
        aggregate: QualificationEvidenceContract,
        projection: QualificationEvidenceProjection,
        *,
        aggregate_marker: object,
        token: object,
    ) -> "QualificationAggregateOwnershipReceipt":
        if token is not _RECEIPT_MINT_TOKEN:
            raise TypeError("aggregate ownership receipts are aggregate-minted")
        if not isinstance(aggregate, QualificationEvidenceContract):
            raise TypeError("nominal qualification evidence is required")
        if not isinstance(projection, QualificationEvidenceProjection):
            raise TypeError("nominal qualification projection is required")
        if projection.aggregate is not aggregate or projection._aggregate_marker is not aggregate_marker:
            raise ValueError("qualification projection ownership mismatch")
        value = object.__new__(cls)
        authority_identity = getattr(projection.authority, "identity", "")
        if not isinstance(authority_identity, str) or not authority_identity:
            raise ValueError("qualification authority identity is required")
        for name, item in (
            ("aggregate_identity", projection.aggregate_identity),
            ("projection_identity", projection.identity),
            ("authority_identity", authority_identity),
            ("authority_binding", projection.authority_binding),
            ("evidence_origin", projection.evidence_origin),
            ("aggregate", aggregate),
            ("projection", projection),
            ("terminal_evidence", projection.terminal_evidence),
            ("authority", projection.authority),
            ("controller", projection.controller),
            ("_aggregate_marker", aggregate_marker),
        ):
            object.__setattr__(value, name, item)
        object.__setattr__(
            value,
            "identity",
            canonical_sha256(
                {
                    "artifact": "minecraft-k12-live-qualification-aggregate-ownership/2",
                    "aggregate": value.aggregate_identity,
                    "projection": value.projection_identity,
                    "authority": value.authority_identity,
                    "authority_binding": projection.authority_binding_canonical,
                    "evidence_origin": value.evidence_origin,
                }
            ),
        )
        return value

    @property
    def digest(self) -> str:
        return self.identity

    def authenticates(
        self,
        aggregate: Any,
        *,
        authority: Any = None,
        controller: Any = None,
        projection: Any = None,
    ) -> bool:
        if not isinstance(aggregate, QualificationEvidenceContract):
            return False
        if not isinstance(self.projection, QualificationEvidenceProjection):
            return False
        if projection is not None and projection is not self.projection:
            return False
        if (
            aggregate is not self.aggregate
            or aggregate.qualification_projection is not self.projection
            or aggregate.ownership_receipt is not self
            or self.projection.aggregate is not aggregate
            or self.projection._aggregate_marker is not self._aggregate_marker
            or self.aggregate_identity != self.projection.aggregate_identity
            or self.projection_identity != self.projection.identity
            or self.authority is not self.projection.authority
            or self.controller is not self.projection.controller
            or self.terminal_evidence is not self.projection.terminal_evidence
            or self.authority_binding != self.projection.authority_binding
            or self.evidence_origin != self.projection.evidence_origin
            or self.authority_identity != getattr(self.authority, "identity", None)
            or self.identity
            != canonical_sha256(
                {
                    "artifact": "minecraft-k12-live-qualification-aggregate-ownership/2",
                    "aggregate": self.aggregate_identity,
                    "projection": self.projection_identity,
                    "authority": self.authority_identity,
                    "authority_binding": self.projection.authority_binding_canonical,
                    "evidence_origin": self.evidence_origin,
                }
            )
        ):
            return False
        if authority is not None and authority is not self.authority:
            return False
        if controller is not None and controller is not self.controller:
            return False
        return True

    verify = authenticates


def install_qualification_evidence_contract(
    aggregate: QualificationEvidenceContract,
    *,
    aggregate_identity: str,
    probe_aggregate_digest: str,
    qualification_terminal_ledger_digest: str,
    profile_digest: str,
    authority_binding: Any,
    authority_binding_canonical: Mapping[str, Any],
    evidence_origin: str,
    execution_provenance: str,
    passed: bool,
    terminal_evidence: QualificationTerminalEvidenceContract,
    authority: Any,
    controller: Any,
    aggregate_marker: object,
    token: object,
) -> tuple[QualificationEvidenceProjection, QualificationAggregateOwnershipReceipt]:
    """Install the one aggregate-owned projection/receipt pair."""

    if token is not _CONTRACT_MINT_TOKEN:
        raise TypeError("qualification evidence contracts are coordinator-minted")
    if (not isinstance(aggregate, QualificationEvidenceContract)
            or not isinstance(terminal_evidence, QualificationTerminalEvidenceContract)):
        raise TypeError("nominal qualification evidence types are required")
    projection = QualificationEvidenceProjection._mint(
        aggregate,
        aggregate_identity=aggregate_identity,
        probe_aggregate_digest=probe_aggregate_digest,
        qualification_terminal_ledger_digest=qualification_terminal_ledger_digest,
        profile_digest=profile_digest,
        authority_binding=authority_binding,
        authority_binding_canonical=authority_binding_canonical,
        evidence_origin=evidence_origin,
        execution_provenance=execution_provenance,
        passed=passed,
        terminal_evidence=terminal_evidence,
        authority=authority,
        controller=controller,
        aggregate_marker=aggregate_marker,
        token=_PROJECTION_MINT_TOKEN,
    )
    receipt = QualificationAggregateOwnershipReceipt._mint(
        aggregate,
        projection,
        aggregate_marker=aggregate_marker,
        token=_RECEIPT_MINT_TOKEN,
    )
    object.__setattr__(aggregate, "_qualification_projection", projection)
    object.__setattr__(aggregate, "_qualification_receipt", receipt)
    return projection, receipt


def qualification_evidence_values(
    evidence: Any,
) -> dict[str, Any]:
    """Return the authenticated, implementation-neutral evidence projection."""

    if not isinstance(evidence, QualificationEvidenceContract):
        raise TypeError("nominal qualification evidence required")
    projection = evidence.qualification_projection
    receipt = evidence.ownership_receipt
    if not isinstance(projection, QualificationEvidenceProjection):
        raise TypeError("nominal qualification projection required")
    if not isinstance(receipt, QualificationAggregateOwnershipReceipt):
        raise TypeError("nominal qualification ownership receipt required")
    if not receipt.authenticates(evidence, projection=projection):
        raise ValueError("qualification evidence ownership mismatch")
    terminal = projection.terminal_evidence
    if not isinstance(terminal, QualificationTerminalEvidenceContract):
        raise TypeError("nominal qualification terminal evidence required")
    return {
        "qualification_aggregate_digest": projection.aggregate_identity,
        "probe_aggregate_digest": projection.probe_aggregate_digest,
        "qualification_terminal_ledger_digest": (
            projection.qualification_terminal_ledger_digest
        ),
        "profile_digest": projection.profile_digest,
        "authority_binding": projection.authority_binding,
        "evidence_origin": projection.evidence_origin,
        "execution_provenance": projection.execution_provenance,
        "passed": projection.passed,
        "authority": projection.authority,
        "controller": projection.controller,
        "ownership_receipt": receipt,
        "terminal_receipt": terminal,
        "projection": projection,
    }


# The longer spelling is useful to callers that do not know the historical
# aggregate class name.  Both names refer to the same nominal receipt type.
QualificationEvidenceOwnershipReceipt = QualificationAggregateOwnershipReceipt

if _PRE_RELOAD_ABI is not None:
    (
        QualificationTerminalEvidenceContract,
        QualificationEvidenceContract,
        QualificationEvidenceProjection,
        QualificationAggregateOwnershipReceipt,
        _CONTRACT_MINT_TOKEN,
        _PROJECTION_MINT_TOKEN,
        _RECEIPT_MINT_TOKEN,
    ) = _PRE_RELOAD_ABI
    QualificationEvidenceOwnershipReceipt = QualificationAggregateOwnershipReceipt

_STABLE_CONTRACT_ABI = (
    QualificationTerminalEvidenceContract,
    QualificationEvidenceContract,
    QualificationEvidenceProjection,
    QualificationAggregateOwnershipReceipt,
    _CONTRACT_MINT_TOKEN,
    _PROJECTION_MINT_TOKEN,
    _RECEIPT_MINT_TOKEN,
)


__all__ = [
    "QualificationAggregateOwnershipReceipt",
    "QualificationEvidenceContract",
    "QualificationEvidenceOwnershipReceipt",
    "QualificationEvidenceProjection",
    "QualificationTerminalEvidenceContract",
    "is_canonical_qualification_evidence",
    "qualification_evidence_values",
]
