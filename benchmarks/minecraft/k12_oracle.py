"""Evidence-derived four-valued oracle for K12 effects."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import dist
from typing import Any

from benchmarks.common.eac.canonical import canonical_sha256
from benchmarks.minecraft.k12_fixture import K12_ACTIONS, K12Cell, K12Fixture, K12State


class OracleValue(str, Enum):
    TRUE = "true"; FALSE = "false"; UNKNOWN = "unknown"; NOT_APPLICABLE = "not_applicable"
TruthValue = OracleValue


def _canonical_value(value: Any) -> Any:
    if isinstance(value, tuple): return [_canonical_value(item) for item in value]
    if isinstance(value, list): return [_canonical_value(item) for item in value]
    if isinstance(value, dict): return {key: _canonical_value(item) for key, item in value.items()}
    return value


@dataclass(frozen=True, slots=True)
class K12OracleFact:
    fixture_id: str; cell_id: str; effect_identity: str; value: OracleValue
    evidence_root_digest: str; revision: int; reset_generation: int = 0
    @property
    def identity_digest(self) -> str:
        return canonical_sha256({"fixture": self.fixture_id, "cell": self.cell_id, "effect": self.effect_identity,
                                 "root": self.evidence_root_digest, "revision": self.revision,
                                 "reset_generation": self.reset_generation, "value": self.value.value})


def _derive(action: str, before: K12State, after: K12State, args: dict[str, Any]) -> OracleValue:
    position = args.get("position", (args.get("x"), args.get("y"), args.get("z")))
    if action == "MineBlock":
        item = before.block(position)
        if item is None: return OracleValue.NOT_APPLICABLE
        delta = after.items("agent").get(item, 0) - before.items("agent").get(item, 0)
        if after.block(position) is None and delta == 1: return OracleValue.TRUE
        if after == before: return OracleValue.FALSE
        return OracleValue.UNKNOWN
    if action == "placeBlock":
        item = args.get("block", args.get("item", "stone"))
        if before.block(position) is not None or before.items("agent").get(item, 0) < 1: return OracleValue.NOT_APPLICABLE
        facing = args.get("facing", "north")
        if (after.block(position) == item
            and dict(after.facings).get(position) == facing
            and after.items("agent").get(item, 0) == before.items("agent").get(item, 0) - 1): return OracleValue.TRUE
        return OracleValue.FALSE if after == before else OracleValue.UNKNOWN
    if action == "navigateTo":
        target = args.get("target", position); old = before.position(); new = after.position()
        if not isinstance(target, tuple) or old is None or new is None: return OracleValue.UNKNOWN
        return OracleValue.TRUE if dist(new, target) <= 1 else OracleValue.FALSE
    if action == "attackTarget":
        target = args.get("target", "zombie"); old = before.entity(target)
        new = after.entity(target); damage = int(args.get("damage", 1))
        if old is None: return OracleValue.NOT_APPLICABLE
        if new is None: return OracleValue.TRUE
        change = old[0] - new[0]
        # A larger unexplained health change is concurrent damage, not a caller assertion.
        if change == 0: return OracleValue.FALSE
        if change != damage: return OracleValue.UNKNOWN
        if new[0] == 0 and new[1] is True: return OracleValue.TRUE
        if new[0] > 0 and new[1] is False: return OracleValue.TRUE
        return OracleValue.UNKNOWN
    if action == "handoverBlock":
        sender = args.get("sender", "agent"); recipient = args.get("recipient", "villager")
        item = args.get("item", "stone"); quantity = int(args.get("quantity", 1))
        sb, sa = before.items(sender), after.items(sender); rb, ra = before.items(recipient), after.items(recipient)
        if sb.get(item, 0) < quantity: return OracleValue.NOT_APPLICABLE
        residual = any(name == item and count > 0
                       for owner, name, count in after.residual_items)
        if (sa.get(item, 0) == sb.get(item, 0)-quantity
            and ra.get(item, 0) == rb.get(item, 0)+quantity and not residual): return OracleValue.TRUE
        return OracleValue.FALSE if after == before else OracleValue.UNKNOWN
    return OracleValue.NOT_APPLICABLE


@dataclass(frozen=True, slots=True)
class K12Oracle:
    fixture: K12Fixture; cell: K12Cell; fact: K12OracleFact | None = None; reset_generation: int = 0

    def evaluate(self, effect_identity: str, before: K12State | None = None,
                 after: K12State | None = None, arguments: dict[str, Any] | None = None, *,
                 cell_id: str | None = None,
                 reset_generation: int | None = None) -> OracleValue:
        if self.cell.fixture_id != self.fixture.fixture_id or self.cell.action not in K12_ACTIONS:
            return OracleValue.NOT_APPLICABLE
        if before is not None and after is not None:
            if (cell_id != self.cell.cell_id or reset_generation != self.reset_generation
                    or self.reset_generation <= 0):
                return OracleValue.UNKNOWN
            args = arguments or {}
            expected = canonical_sha256(_canonical_value({
                "fixture": self.fixture.fixture_digest, "action": self.cell.action,
                "arguments": [[key, value] for key, value in sorted(args.items())],
                "before": before.digest, "after": after.digest,
            }))
            if effect_identity != expected:
                return OracleValue.UNKNOWN
            return _derive(self.cell.action, before, after, args)
        fact = self.fact
        if (fact is None or fact.fixture_id != self.fixture.fixture_id or fact.cell_id != self.cell.cell_id
                or fact.effect_identity != effect_identity or fact.evidence_root_digest != self.fixture.evidence_root_digest
                or self.reset_generation <= 0
                or fact.reset_generation != self.reset_generation): return OracleValue.UNKNOWN
        return fact.value if fact.value in (OracleValue.TRUE, OracleValue.FALSE) else OracleValue.UNKNOWN

    def evaluate_bound(self, effect_identity: str, before: K12State, after: K12State,
                       arguments: dict[str, Any], *, cell_id: str,
                       reset_generation: int) -> OracleValue:
        """Evaluate observations only under the oracle's exact reset/cell binding."""
        if (cell_id != self.cell.cell_id or type(reset_generation) is not int
                or reset_generation <= 0 or reset_generation != self.reset_generation):
            return OracleValue.UNKNOWN
        return self.evaluate(effect_identity, before, after, arguments,
                             cell_id=cell_id, reset_generation=reset_generation)

    def payload(self, effect_identity: str) -> dict[str, Any]:
        return {"fixture_id": self.fixture.fixture_id, "cell_id": self.cell.cell_id,
                "effect_identity": effect_identity, "oracle": self.evaluate(effect_identity).value,
                "evidence_root_digest": self.fixture.evidence_root_digest,
                "reset_generation": self.reset_generation}


def make_fact(fixture: K12Fixture, cell: K12Cell, effect_identity: str, value: OracleValue,
              revision: int = 1, reset_generation: int = 0) -> K12OracleFact:
    if value not in (OracleValue.TRUE, OracleValue.FALSE): raise ValueError("facts must be positive or negative")
    return K12OracleFact(fixture.fixture_id, cell.cell_id, effect_identity, value,
                         fixture.evidence_root_digest, revision, reset_generation)


__all__ = ["K12Oracle", "K12OracleFact", "OracleValue", "TruthValue", "make_fact"]
