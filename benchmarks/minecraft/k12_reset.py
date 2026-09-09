"""Typed, fail-closed reset authority for the offline K12 fake."""
from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Any

from benchmarks.common.eac.canonical import canonical_sha256
from benchmarks.minecraft.k12_fixture import K12Fixture, K12State


class K12ResetError(RuntimeError): pass


def _canonical_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_canonical_value(item) for item in value]
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonical_value(item) for key, item in value.items()}
    return value


@dataclass(frozen=True, slots=True)
class K12ObservedResetState:
    fixture_digest: str
    world_digest: str
    positions_digest: str
    inventory_digest: str
    entity_digest: str
    block_digest: str
    evidence_root_digest: str
    revisions: tuple[tuple[str, int], ...]
    eac_empty: bool = True
    candidate_empty: bool = True
    permit_empty: bool = True
    cache_empty: bool = True
    rng_state: str = "frozen"
    model_state: str = "fresh"
    oracle_state: str = "fresh"
    prior_containment: str = "contained"
    cell_id: str = ""
    triplet_id: str = ""
    arm: str = ""
    rng_state_digest: str = ""
    model_config_digest: str = ""
    oracle_initial_state_digest: str = ""
    seed: int = 0
    token_id: str = ""
    reset_generation: int = 0
    initial_state_digest: str = ""

    @classmethod
    def from_fixture(cls, fixture: K12Fixture, generation: int = 0, *, cell_id: str,
                     triplet_id: str = "triplet", arm: str = "A", seed: int = 0,
                     initial_state: K12State | None = None,
                     prior_containment: str = "contained") -> "K12ObservedResetState":
        return cls(fixture.fixture_digest, fixture.world_digest, fixture.positions_digest,
                   fixture.inventory_digest, fixture.entity_digest, fixture.block_digest,
                   fixture.evidence_root_digest, fixture.revisions, cell_id=cell_id,
                   triplet_id=triplet_id, arm=arm,
                   rng_state_digest=canonical_sha256({"cell_id": cell_id, "seed": seed}),
                   model_config_digest=canonical_sha256({"temperature": 0, "cache": False,
                                                         "model_call_attempts": 1}),
                   oracle_initial_state_digest=(initial_state or fixture.original).digest,
                   seed=seed,
                   reset_generation=generation,
                   initial_state_digest=(initial_state or fixture.original).digest,
                   prior_containment=prior_containment)


@dataclass(frozen=True, slots=True)
class K12ResetAttestation:
    fixture_digest: str; world_digest: str; positions_digest: str; inventory_digest: str
    entity_digest: str; block_digest: str; evidence_root_digest: str
    revisions: tuple[tuple[str, int], ...]; eac_empty: bool; candidate_empty: bool
    permit_empty: bool; cache_empty: bool; rng_state: str; model_state: str
    oracle_state: str; prior_containment: str; reset_generation: int; digest: str
    cell_id: str = ""; triplet_id: str = ""; arm: str = ""
    rng_state_digest: str = ""; model_config_digest: str = ""; oracle_initial_state_digest: str = ""
    seed: int = 0
    token_id: str = ""
    initial_state_digest: str = ""

    @classmethod
    def create(cls, fixture: K12Fixture, generation: int, observed: K12ObservedResetState) -> "K12ResetAttestation":
        obs = observed
        fields = {name: getattr(obs, name) for name in K12ObservedResetState.__dataclass_fields__}
        fields["reset_generation"] = generation
        digest = canonical_sha256(_canonical_value(fields))
        return cls(**fields, digest=digest)

    def verified(self) -> bool:
        fields = {name: getattr(self, name) for name in K12ResetAttestation.__dataclass_fields__ if name != "digest"}
        return self.digest == canonical_sha256(_canonical_value(fields))

    @property
    def status(self) -> str:
        return "verified" if self.verified() else "failed"


@dataclass(frozen=True, slots=True)
class K12ResetToken:
    token_id: str; attestation_digest: str; generation: int; cell_id: str
    triplet_id: str; arm: str


class K12ResetAuthority:
    def __init__(self, fixture: K12Fixture, *, initial_generation: int = 0,
                 expected_initial_state_digest: str | None = None,
                 expected_prior_containment: str = "contained") -> None:
        self.fixture = fixture; self._lock = RLock(); self._generation = initial_generation
        self.expected_initial_state_digest = expected_initial_state_digest
        self.expected_prior_containment = expected_prior_containment
        self._issued: dict[str, K12ResetToken] = {}; self._used: set[str] = set(); self._serial = 0

    def attest(self, observed: K12ObservedResetState | None = None, *, cell_id: str = "",
               triplet_id: str = "triplet", arm: str = "A", seed: int = 0) -> K12ResetAttestation:
        with self._lock:
            observation = observed or K12ObservedResetState.from_fixture(
                self.fixture, self._generation, cell_id=cell_id,
                triplet_id=triplet_id, arm=arm, seed=seed,
                initial_state=self.fixture.alternative,
                prior_containment=self.expected_prior_containment,
            )
            return K12ResetAttestation.create(self.fixture, self._generation, observation)

    def _valid(self, attestation: K12ResetAttestation) -> bool:
        return (isinstance(attestation, K12ResetAttestation) and attestation.verified()
                and attestation.fixture_digest == self.fixture.fixture_digest
                and attestation.world_digest == self.fixture.world_digest
                and attestation.positions_digest == self.fixture.positions_digest
                and attestation.inventory_digest == self.fixture.inventory_digest
                and attestation.entity_digest == self.fixture.entity_digest
                and attestation.block_digest == self.fixture.block_digest
                and attestation.evidence_root_digest == self.fixture.evidence_root_digest
                and attestation.revisions == self.fixture.revisions
                and attestation.eac_empty and attestation.candidate_empty
                and attestation.permit_empty and attestation.cache_empty
                and attestation.rng_state == "frozen"
                and attestation.model_state == "fresh"
                and attestation.oracle_state == "fresh"
                and attestation.prior_containment == "contained"
                and attestation.prior_containment == self.expected_prior_containment
                and bool(attestation.cell_id) and bool(attestation.triplet_id)
                and attestation.arm in {"A", "R", "S"}
                and attestation.rng_state_digest == canonical_sha256(
                    {"cell_id": attestation.cell_id, "seed": attestation.seed})
                and attestation.model_config_digest == canonical_sha256(
                    {"temperature": 0, "cache": False, "model_call_attempts": 1})
                and attestation.oracle_initial_state_digest == attestation.initial_state_digest
                and attestation.reset_generation == self._generation
                and (self.expected_initial_state_digest is None
                     or attestation.initial_state_digest == self.expected_initial_state_digest)
                )

    def issue(self, attestation: K12ResetAttestation | None = None) -> K12ResetToken:
        with self._lock:
            attestation = attestation or self.attest()
            if not self._valid(attestation): raise K12ResetError("reset observation is not verified")
            self._serial += 1
            token_id = canonical_sha256({"fixture": self.fixture.fixture_digest,
                                         "cell_id": attestation.cell_id,
                                         "triplet_id": attestation.triplet_id,
                                         "arm": attestation.arm,
                                         "generation": self._generation,
                                         "serial": self._serial})
            token = K12ResetToken(token_id, attestation.digest, self._generation,
                                  attestation.cell_id, attestation.triplet_id, attestation.arm)
            self._issued[token_id] = token
            return token

    def launch(self, token: K12ResetToken, attestation: K12ResetAttestation, *,
               expected_cell_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            if (not isinstance(token, K12ResetToken) or token.token_id in self._used
                    or self._issued.get(token.token_id) != token or not self._valid(attestation)
                    or token.attestation_digest != attestation.digest or token.generation != self._generation
                    or token.cell_id != attestation.cell_id or token.triplet_id != attestation.triplet_id
                    or token.arm != attestation.arm
                    or (expected_cell_id is not None and token.cell_id != expected_cell_id)):
                raise K12ResetError("reset launch failed closed")
            self._used.add(token.token_id); self._generation += 1
            return {"status": "reset", "generation": token.generation,
                    "next_generation": self._generation,
                    "fixture_digest": self.fixture.fixture_digest, "token_id": token.token_id,
                    "initial_state_digest": attestation.initial_state_digest}


__all__ = ["K12ObservedResetState", "K12ResetAttestation", "K12ResetAuthority", "K12ResetError", "K12ResetToken"]
