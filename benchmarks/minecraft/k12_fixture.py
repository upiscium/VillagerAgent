"""Offline, deterministic contracts for the five K12 Minecraft strata."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from benchmarks.common.eac.canonical import canonical_sha256

K12_ACTIONS = ("MineBlock", "placeBlock", "navigateTo", "attackTarget", "handoverBlock")
STRATUM_ACTIONS = {f"S{index}": action for index, action in enumerate(K12_ACTIONS, 1)}
REAL_ACTION_MAPPINGS = {"MineBlock": "post_dig", "placeBlock": "post_place",
                        "navigateTo": "post_move_to_pos", "attackTarget": "post_attack",
                        "handoverBlock": "post_hand"}
Position = tuple[int, int, int]


def _canonical_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_canonical_value(item) for item in value]
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonical_value(item) for key, item in value.items()}
    return value


@dataclass(frozen=True, slots=True)
class K12ActionContract:
    name: str
    version: int = 1
    mapping: str = ""
    stratum: str = "original"
    alternative_stratum: str = "hidden-alternative"


@dataclass(frozen=True, slots=True)
class K12State:
    """The complete state observed by a fake backend (and by the oracle)."""
    positions: tuple[tuple[str, Position], ...] = (("agent", (0, 64, 0)), ("villager", (2, 64, 0)))
    blocks: tuple[tuple[Position, str], ...] = (((1, 64, 0), "stone"),)
    inventory: tuple[tuple[str, tuple[tuple[str, int], ...]], ...] = (("agent", (("stone", 1),)), ("villager", ()))
    entities: tuple[tuple[str, tuple[int, bool]], ...] = (("zombie", (20, False)),)
    facings: tuple[tuple[Position, str], ...] = ()
    residual_items: tuple[tuple[str, str, int], ...] = ()

    def position(self, name: str = "agent") -> Position | None:
        return dict(self.positions).get(name)

    def block(self, position: Position) -> str | None:
        return dict(self.blocks).get(position)

    def items(self, owner: str) -> dict[str, int]:
        return dict(dict(self.inventory).get(owner, ()))

    def entity(self, name: str) -> tuple[int, bool] | None:
        return dict(self.entities).get(name)

    @property
    def digest(self) -> str:
        return canonical_sha256(_canonical_value({"positions": self.positions, "blocks": self.blocks,
                                 "inventory": self.inventory, "entities": self.entities,
                                 "facings": self.facings, "residual_items": self.residual_items}))


@dataclass(frozen=True, slots=True)
class K12Cell:
    cell_id: str
    fixture_id: str
    action: str
    stratum: str = "original"

    def __post_init__(self) -> None:
        if not self.cell_id or not self.fixture_id or not self.action:
            raise ValueError("invalid K12 cell identity")


@dataclass(frozen=True, slots=True)
class K12Fixture:
    fixture_id: str
    world_digest: str
    positions_digest: str
    inventory_digest: str
    entity_digest: str
    block_digest: str
    evidence_root_digest: str
    revisions: tuple[tuple[str, int], ...]
    original: K12State
    alternative: K12State
    action: str
    original_arguments: tuple[tuple[str, Any], ...]
    alternative_arguments: tuple[tuple[str, Any], ...]
    actions: tuple[K12ActionContract, ...] = tuple(
        K12ActionContract(name, mapping=REAL_ACTION_MAPPINGS[name]) for name in K12_ACTIONS
    )
    schema: ClassVar[str] = "minecraft-k12-local-fixture/2"

    def __post_init__(self) -> None:
        if (not self.fixture_id or tuple(a.name for a in self.actions) != K12_ACTIONS
                or self.action not in K12_ACTIONS):
            raise ValueError("invalid frozen K12 fixture")
        if any(type(r) is not int or r < 0 for _, r in self.revisions):
            raise ValueError("invalid K12 revision")

    @property
    def fixture_digest(self) -> str:
        return canonical_sha256(_canonical_value({"schema": self.schema, "fixture_id": self.fixture_id,
            "digests": (self.world_digest, self.positions_digest, self.inventory_digest,
                         self.entity_digest, self.block_digest, self.evidence_root_digest),
            "revisions": self.revisions,
            "actions": tuple((a.name, a.version, a.mapping, a.stratum, a.alternative_stratum) for a in self.actions),
            "action": self.action,
            "original_arguments": self.original_arguments,
            "alternative_arguments": self.alternative_arguments,
            "original": self.original.digest, "alternative": self.alternative.digest}))

    def planner_payload(self) -> dict[str, Any]:
        return {"schema": self.schema, "fixture_id": self.fixture_id,
                "fixture_digest": self.fixture_digest, "action": self.action,
                "actions": K12_ACTIONS, "original": self.original.digest,
                "original_arguments": self.original_arguments}

    def invalidation_payload(self) -> dict[str, Any]:
        return {"fixture_id": self.fixture_id, "fixture_digest": self.fixture_digest,
                "revisions": self.revisions}


def build_k12_fixture(fixture_id: str = "k12-local-1", *, action: str = "MineBlock",
                      template: int = 1, seed: int = 1) -> K12Fixture:
    if action not in K12_ACTIONS:
        raise ValueError("unknown K12 fixture action")
    if template not in {1, 2} or seed not in {1, 2, 3}:
        raise ValueError("fixture template/seed is outside the frozen census")
    def digest(label: str) -> str:
        return canonical_sha256({"fixture": fixture_id, "part": label})
    offset = (template - 1) * 12 + (seed - 1) * 3
    original_position, alternative_position = (1 + offset, 64, 0), (4 + offset, 64, 0)
    original_target = "zombie" if offset == 0 else f"zombie-t{template}-n{seed}"
    alternative_target = "skeleton" if offset == 0 else f"skeleton-t{template}-n{seed}"
    original_recipient = "villager"
    alternative_recipient = "villager2" if offset == 0 else f"villager-t{template}-n{seed}"
    original = K12State(blocks=((original_position, "stone"),))
    arguments: dict[str, tuple[tuple[str, Any], ...]] = {
        "MineBlock": (("position", original_position),),
        "placeBlock": (("position", (3 + offset, 64, 0)), ("item", "stone"), ("facing", "east")),
        "navigateTo": (("target", (4 + offset, 64, 0)),),
        "attackTarget": (("target", original_target), ("damage", 20)),
        "handoverBlock": (("sender", "agent"), ("recipient", original_recipient),
                          ("item", "stone"), ("quantity", 1)),
    }
    alternatives: dict[str, tuple[tuple[str, Any], ...]] = {
        "MineBlock": (("position", alternative_position),),
        "placeBlock": (("position", (4 + offset, 64, 0)), ("item", "stone"), ("facing", "west")),
        "navigateTo": (("target", (8 + offset, 64, 0)),),
        "attackTarget": (("target", alternative_target), ("damage", 20)),
        "handoverBlock": (("sender", "agent"), ("recipient", alternative_recipient),
                          ("item", "stone"), ("quantity", 1)),
    }
    if action == "placeBlock":
        original = K12State(blocks=(), inventory=K12State().inventory)
    elif action == "attackTarget":
        original = K12State(entities=((original_target, (20, False)), (alternative_target, (20, False))))
    elif action == "handoverBlock":
        original = K12State(positions=(("agent", (0, 64, 0)), (original_recipient, (2, 64, 0)),
                                            (alternative_recipient, (3 + offset, 64, 0))))
    alternative = original
    if action == "MineBlock":
        alternative = K12State(blocks=((original_position, "dirt"), (alternative_position, "stone")),
                               inventory=original.inventory)
    return K12Fixture(fixture_id, *(digest(x) for x in ("world", "positions", "inventory", "entity", "block", "evidence")),
                       (("world", 1), ("positions", 1), ("inventory", 1), ("entity", 1), ("block", 1), ("evidence", 1)),
                       original, alternative, action, arguments[action], alternatives[action])


__all__ = ["K12_ACTIONS", "REAL_ACTION_MAPPINGS", "STRATUM_ACTIONS", "K12ActionContract", "K12Cell", "K12Fixture", "K12State", "Position", "build_k12_fixture"]
