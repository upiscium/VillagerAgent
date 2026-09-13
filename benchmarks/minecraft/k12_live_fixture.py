"""Pure, deterministic fixture contracts for the guarded-real K12 smoke set."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

LIVE_STRATA = ("S1", "S2", "S3", "S4", "S5")
LIVE_TEMPLATES = (1, 2)
LIVE_ARMS = ("A", "R", "S")
LIVE_CONTRACTS = {
    "S1": "tagged block transition and actor inventory delta",
    "S2": "Option B: facing legality only",
    "S3": "actor position reaches the bound destination within one block",
    "S4": "unique tagged zombie death contract; no damage argument",
    "S5": "FastAPI two dedicated player actors with recipient polling",
}


@dataclass(frozen=True, slots=True)
class FixtureGeometry:
    actor_start: tuple[int,int,int]
    recipient_start: tuple[int,int,int] | None
    target: tuple[int,int,int]
    support: tuple[int,int,int]
    observation_center: tuple[int,int,int]
    radius: int
    region_bounds: tuple[tuple[int,int,int],tuple[int,int,int]]

    @property
    def force_loaded_chunks(self) -> tuple[tuple[int,int],...]:
        low,high=self.region_bounds
        return tuple((x*16,z*16) for x in range(low[0]//16,high[0]//16+1)
                     for z in range(low[2]//16,high[2]//16+1))


@dataclass(frozen=True, slots=True)
class K12LiveFixture:
    cell_id: str
    stratum: str
    template: int
    seed: int
    action: str
    mapping: str
    contract: str
    arguments: tuple[tuple[str, Any], ...]
    geometry: FixtureGeometry

    def payload(self) -> dict[str, Any]:
        return {"cell_id": self.cell_id, "stratum": self.stratum,
                "template": self.template, "seed": self.seed,
                "action": self.action, "mapping": self.mapping,
                "contract": self.contract, "arguments": self.arguments,
                "geometry": self.geometry}


_ACTIONS = {"S1": ("MineBlock", "post_dig"), "S2": ("placeBlock", "post_place"),
            "S3": ("navigateTo", "post_move_to_pos"), "S4": ("attackTarget", "post_attack"),
            "S5": ("handoverBlock", "post_hand")}


def build_live_fixture(stratum: str, template: int, seed: int) -> K12LiveFixture:
    if stratum not in LIVE_STRATA or template not in LIVE_TEMPLATES or seed not in (1, 2, 3):
        raise ValueError("descriptor is outside the sealed live qualification set")
    action, mapping = _ACTIONS[stratum]
    cell_id = f"K12-LIVE-{stratum}-T{template}-N{seed}"
    ordinal = LIVE_STRATA.index(stratum) * 6 + (template - 1) * 3 + (seed - 1)
    offset = ordinal * 40
    actor_start=(offset,64,0)
    target=(offset+(4 if stratum=="S3" else 1 if stratum=="S5" else 2),64,0)
    support=(offset+1,64 if stratum=="S2" else 63,0)
    recipient=(offset+1,64,0) if stratum=="S5" else None
    center=(offset+1,64,0); radius=16
    geometry=FixtureGeometry(actor_start,recipient,target,support,center,radius,
        ((center[0]-radius,48,-radius),(center[0]+radius,80,radius)))
    args: dict[str, tuple[tuple[str, Any], ...]] = {
        "S1": (("actor", "agent"), ("support", support), ("target", target)),
        "S2": (("actor", "agent"), ("support", (offset+1,64,0)), ("target", target), ("item", "stone"), ("facing", "east")),
        "S3": (("actor", "agent"), ("support", support), ("target", (offset+4,64,0))),
        "S4": (("actor", "agent"), ("support", support), ("target", "zombie"), ("tag", f"k12-live-zombie-t{template}-n{seed}"), ("radius", radius), ("reach", 3)),
        "S5": (("actor", "k12-player-a"), ("support", support), ("target", recipient), ("sender", "k12-player-a"), ("recipient", "k12-player-b"),
               ("item", "stone"), ("quantity", 1), ("poll", "/players/k12-player-b/inventory")),
    }
    return K12LiveFixture(cell_id, stratum, template, seed, action, mapping,
                          LIVE_CONTRACTS[stratum], args[stratum], geometry)


def build_live_schedule() -> tuple[K12LiveFixture, ...]:
    """Return the five T1/N1 fixtures expanded by the qualification arms."""
    return tuple(build_live_fixture(stratum, 1, 1) for stratum in LIVE_STRATA)


def qualification_ids() -> tuple[str, ...]:
    """Return the exact accepted non-scientific qualification order."""
    arm_order = {
        "S1": ("A", "R", "S"),
        "S2": ("R", "S", "A"),
        "S3": ("S", "A", "R"),
        "S4": ("A", "S", "R"),
        "S5": ("R", "A", "S"),
    }
    return tuple(
        f"K12Q-{fixture.stratum}-T1-N1-{arm}"
        for fixture in build_live_schedule()
        for arm in arm_order[fixture.stratum]
    )


__all__ = ["FixtureGeometry", "K12LiveFixture", "LIVE_ARMS", "LIVE_CONTRACTS", "LIVE_STRATA", "LIVE_TEMPLATES",
           "build_live_fixture", "build_live_schedule", "qualification_ids"]
