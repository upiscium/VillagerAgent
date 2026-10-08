"""Pure offline fake backend with explicit, deterministic state transitions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from benchmarks.common.eac.canonical import canonical_sha256
from benchmarks.minecraft.k12_fixture import K12_ACTIONS, K12Fixture, K12State, Position


def _canonical_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_canonical_value(item) for item in value]
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonical_value(item) for key, item in value.items()}
    return value


@dataclass(frozen=True, slots=True)
class K12Effect:
    action: str
    arguments: tuple[tuple[str, Any], ...]
    effect_digest: str
    before: K12State
    after: K12State
    executed: bool = True


class K12BackendError(RuntimeError):
    pass


def _replace_state(state: K12State, **changes: Any) -> K12State:
    values = {"positions": state.positions, "blocks": state.blocks,
              "inventory": state.inventory, "entities": state.entities, "facings": state.facings,
              "residual_items": state.residual_items}
    values.update(changes)
    return K12State(**values)


@dataclass(slots=True)
class K12FakeBackend:
    fixture: K12Fixture
    state: K12State | None = None

    def __post_init__(self) -> None:
        if self.state is None:
            self.state = self.fixture.original

    def _effect(self, action: str, arguments: dict[str, Any]) -> K12Effect:
        if action not in K12_ACTIONS:
            raise K12BackendError("action is outside the frozen K12 seam")
        before = self.state
        assert before is not None
        after = before
        pos = arguments.get("position", (arguments.get("x"), arguments.get("y"), arguments.get("z")))
        if action == "MineBlock" and all(isinstance(v, int) for v in pos):
            blocks = dict(before.blocks); item = blocks.pop(pos, None)
            if item is not None:
                inv = before.items("agent"); inv[item] = inv.get(item, 0) + 1
                after = _replace_state(before, blocks=tuple(sorted(blocks.items())),
                    inventory=tuple(sorted((owner, tuple(sorted(items.items()))) for owner, items in
                                           [("agent", inv), ("villager", before.items("villager"))])))
        elif action == "placeBlock" and all(isinstance(v, int) for v in pos):
            args = arguments.get("block", arguments.get("item", "stone")); inv = before.items("agent")
            if inv.get(args, 0) > 0 and before.block(pos) is None:
                inv[args] -= 1
                facings = dict(before.facings); facings[pos] = str(arguments.get("facing", "north"))
                inventories = {owner: dict(items) for owner, items in before.inventory}
                inventories["agent"] = inv
                after = _replace_state(before, blocks=tuple(sorted((*before.blocks, (pos, args)))),
                    inventory=tuple(sorted((owner, tuple(sorted((k, v) for k, v in items.items() if v)))
                                           for owner, items in inventories.items())),
                    facings=tuple(sorted(facings.items())))
        elif action == "navigateTo":
            target = arguments.get("target", pos)
            if isinstance(target, tuple) and len(target) == 3:
                positions = dict(before.positions); positions["agent"] = target
                after = _replace_state(before, positions=tuple(sorted(positions.items())))
        elif action == "attackTarget":
            target = arguments.get("target", "zombie"); damage = int(arguments.get("damage", 1))
            entity = before.entity(target)
            if entity is not None:
                health = max(0, entity[0] - damage); entities = dict(before.entities)
                entities[target] = (health, health == 0)
                after = _replace_state(before, entities=tuple(sorted(entities.items())))
        elif action == "handoverBlock":
            sender = arguments.get("sender", "agent"); recipient = arguments.get("recipient", "villager")
            item = arguments.get("item", "stone"); quantity = int(arguments.get("quantity", 1))
            inventories = {owner: dict(items) for owner, items in before.inventory}
            if inventories.get(sender, {}).get(item, 0) >= quantity:
                inventories.setdefault(sender, {})[item] -= quantity
                inventories.setdefault(recipient, {})[item] = inventories.get(recipient, {}).get(item, 0) + quantity
                after = _replace_state(before, inventory=tuple(sorted((o, tuple(sorted((k, v) for k, v in i.items() if v))) for o, i in inventories.items())))
        frozen = tuple(sorted(arguments.items()))
        digest = canonical_sha256(_canonical_value({"fixture": self.fixture.fixture_digest, "action": action,
                                   "arguments": [[k, v] for k, v in frozen], "before": before.digest,
                                   "after": after.digest}))
        self.state = after
        return K12Effect(action, frozen, digest, before, after, after != before)

    def MineBlock(self, **kwargs: Any) -> K12Effect: return self._effect("MineBlock", kwargs)
    def placeBlock(self, **kwargs: Any) -> K12Effect: return self._effect("placeBlock", kwargs)
    def navigateTo(self, **kwargs: Any) -> K12Effect: return self._effect("navigateTo", kwargs)
    def attackTarget(self, **kwargs: Any) -> K12Effect: return self._effect("attackTarget", kwargs)
    def handoverBlock(self, **kwargs: Any) -> K12Effect: return self._effect("handoverBlock", kwargs)


DeterministicK12Backend = K12FakeBackend
__all__ = ["K12BackendError", "K12Effect", "K12FakeBackend", "DeterministicK12Backend"]
