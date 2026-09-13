"""A small, offline, typed state machine for the K12 orchestration fixture.

The selected arm is an input, not a state which recovery can mutate.  This is
important: R may produce a *raw* terminal candidate, but it cannot pronounce
that candidate recovered.  All mutations, including budget reservations, are
serialized by the same lock.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class K12State(str, Enum):
    A = "A"
    R = "R"
    S = "S"


class LifecycleState(str, Enum):
    NEW = "new"
    ENTERED = "entered"
    ADVISORY = "advisory"
    REJECTED = "rejected"
    OBSERVED = "observed"
    PROPOSAL_VALIDATED = "proposal_validated"
    REPLANNING = "replanning"
    STOPPED_AFTER_REJECTION = "stopped_after_rejection"
    TERMINAL = "terminal"


class TerminalDisposition(str, Enum):
    COMPLETED = "completed"
    RECOVERED = "recovered"  # retained for wire compatibility; never emitted
    AUTHORITY_REJECTED = "authority_rejected"
    REPEATED_REQUEST = "repeated_request"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    BRANCH_EQUIVALENT = "branch_equivalent"
    INVALID_ORDER = "invalid_order"
    STOPPED_AFTER_REJECTION = "stopped_after_rejection"


class TransitionKind(str, Enum):
    ENTER = "enter"
    AUTHORITY_REJECTION = "authority_rejection"
    OBSERVE_REJECTION = "observe_rejection"
    RECOVERY_REQUEST = "recovery_request"
    EFFECT = "effect"
    MODEL = "model"
    EVIDENCE = "evidence"
    STEPS = "steps"
    ADVISORY = "advisory"
    STOP = "stop"


class K12TransitionError(ValueError):
    pass


class K12BudgetExhausted(K12TransitionError):
    pass


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    wall_seconds: float
    steps: int
    model: int
    evidence: int
    effects: int


class ParentBudgetAuthority:
    """Single parent-owned admission ledger, checked before side effects."""
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 parent_started: float | None = None) -> None:
        self.clock = clock
        self.parent_started = clock() if parent_started is None else parent_started
        self.branch_started: float | None = None
        self.steps = self.model = self.evidence = self.effects = 0
        self._lock = threading.RLock()

    def begin_branch(self) -> None:
        with self._lock:
            if self.branch_started is None:
                self.branch_started = self.clock()

    def check_deadline(self) -> None:
        with self._lock:
            now = self.clock()
            if now - self.parent_started >= (K12Orchestrator.ABSOLUTE_PARENT_SECONDS
                                             - K12Orchestrator.FINALIZATION_RESERVE_SECONDS):
                raise K12BudgetExhausted("finalization reserve reached")
            if (self.branch_started is not None
                    and now - self.branch_started >= K12Orchestrator.USABLE_POST_BRANCH_SECONDS):
                raise K12BudgetExhausted("post-branch deadline")

    def check_absolute_deadline(self) -> None:
        with self._lock:
            if self.clock() - self.parent_started >= K12Orchestrator.ABSOLUTE_PARENT_SECONDS:
                raise K12BudgetExhausted("absolute parent deadline")

    def reserve(self, kind: str) -> BudgetSnapshot:
        with self._lock:
            self.check_deadline()
            limits = {"steps": K12Orchestrator.STEP_BUDGET, "model": K12Orchestrator.MODEL_BUDGET,
                      "evidence": K12Orchestrator.EVIDENCE_BUDGET, "effects": K12Orchestrator.EFFECT_BUDGET}
            if kind not in limits or getattr(self, kind) >= limits[kind]:
                raise K12BudgetExhausted(f"{kind} budget exhausted")
            setattr(self, kind, getattr(self, kind) + 1)
            return self.snapshot()

    def snapshot(self) -> BudgetSnapshot:
        with self._lock:
            return BudgetSnapshot(self.clock() - self.parent_started, self.steps, self.model,
                                  self.evidence, self.effects)


@dataclass(frozen=True, slots=True)
class Transition:
    kind: TransitionKind
    state: K12State
    sequence: int
    monotonic_seconds: float
    request_id: str
    parent_id: str | None = None
    details: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class TerminalCandidate:
    disposition: TerminalDisposition
    request_id: str
    state: K12State
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ShadowObservation:
    would_block: bool
    original_effect: bool


@dataclass(frozen=True, slots=True)
class AuthorityRejection:
    request_id: str
    rejection_id: str
    reason: str
    causal_parent: str
    branch_digest: str | None = None


class K12Orchestrator:
    POST_BRANCH_WALL_SECONDS = 120.0
    ABSOLUTE_PARENT_SECONDS = 180.0
    SETUP_TARGET_SECONDS = 30.0
    FINALIZATION_RESERVE_SECONDS = 30.0
    USABLE_POST_BRANCH_SECONDS = POST_BRANCH_WALL_SECONDS
    STEP_BUDGET, MODEL_BUDGET, EVIDENCE_BUDGET, EFFECT_BUDGET = 4, 2, 3, 2

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 parent_started: float | None = None, selected_arm: K12State = K12State.A,
                 arm: K12State | None = None, paired_branch_digest: str | None = None,
                 paired_rejection_id: str | None = None) -> None:
        if arm is not None:
            if selected_arm is not K12State.A and selected_arm is not arm:
                raise K12TransitionError("selected arm is immutable")
            selected_arm = arm
        self._selected_arm = K12State(selected_arm)
        self._clock = clock
        self._parent_started = float(clock() if parent_started is None else parent_started)
        self._branch_started: float | None = None
        self._lock = threading.RLock()
        self._state = self._selected_arm
        self._lifecycle = LifecycleState.NEW
        self._sequence = 0
        self._request: str | None = None
        self._parent: str | None = None
        self._steps = self._models = self._evidence = self._effects = 0
        self._rejection_observed = False
        self._rejection_id: str | None = None
        self._branch_digest: str | None = None
        self._paired_digest, self._paired_rejection = paired_branch_digest, paired_rejection_id
        self._terminal: TerminalCandidate | None = None
        self._trace: list[Transition] = []
        self._seen_requests: set[str] = set()
        self._validated_recovery: tuple[str, str, str, str, str, str, str] | None = None
        self._model_active = False
        self.shadow: ShadowObservation | None = None

    @property
    def selected_arm(self) -> K12State: return self._selected_arm
    @property
    def lifecycle_state(self) -> LifecycleState: return self._lifecycle
    @property
    def state(self) -> K12State: return self._state  # legacy wire field: never the selector
    @property
    def terminal(self) -> TerminalCandidate | None: return self._terminal
    @property
    def transitions(self) -> tuple[Transition, ...]: return tuple(self._trace)

    @property
    def budgets(self) -> BudgetSnapshot:
        with self._lock:
            now = self._clock()
            started = self._branch_started if self._branch_started is not None else now
            return BudgetSnapshot(now - started, self._steps, self._models, self._evidence, self._effects)

    def _finish(self, disposition: TerminalDisposition, reason: str = "") -> None:
        if self._terminal is None:
            self._terminal = TerminalCandidate(disposition, self._request or "", self._selected_arm, reason)
            self._lifecycle = (LifecycleState.STOPPED_AFTER_REJECTION
                               if disposition is TerminalDisposition.STOPPED_AFTER_REJECTION
                               else LifecycleState.TERMINAL)

    def _deadline(self) -> None:
        elapsed = self._clock() - self._parent_started
        if elapsed >= self.ABSOLUTE_PARENT_SECONDS:
            self._finish(TerminalDisposition.DEADLINE_EXCEEDED, "absolute parent deadline")
            raise K12BudgetExhausted("absolute parent deadline")

    def _record(self, kind: TransitionKind, request: str, parent: str | None = None, **details: str) -> Transition:
        self._deadline()
        if parent is None and self._sequence:
            parent = request or self._rejection_id
        if parent is not None and not isinstance(parent, str):
            raise K12TransitionError("causal id must be textual")
        self._sequence += 1
        item = Transition(kind, self._state, self._sequence, float(self._clock()), request, parent,
                          tuple(sorted(details.items())))
        self._trace.append(item)
        return item

    def enter(self, request_id: str, *, parent_id: str | None = None, branch_digest: str | None = None,
              shadow_would_block: bool = False, original_effect: bool = False) -> Transition:
        with self._lock:
            if self._lifecycle is not LifecycleState.NEW or not request_id or request_id in self._seen_requests:
                raise K12TransitionError("enter is only valid once with a fresh request")
            if parent_id == request_id:
                raise K12TransitionError("causal parent cannot be the request")
            self._deadline()
            self._request, self._parent, self._branch_digest = request_id, parent_id, branch_digest
            self._seen_requests.add(request_id)
            self.shadow = ShadowObservation(shadow_would_block, original_effect)
            self._lifecycle = LifecycleState.ENTERED
            transition = self._record(TransitionKind.ENTER, request_id, parent_id, branch_digest=branch_digest or "")
            if self._selected_arm is K12State.S and self._paired_digest is None:
                self._paired_digest = branch_digest
            return transition

    def _use(self, kind: TransitionKind, name: str, limit: int) -> Transition:
        with self._lock:
            if self._terminal: raise K12TransitionError("terminal candidate already emitted")
            if self._branch_started is None:
                raise K12TransitionError("post-branch activity requires a branch origin")
            self._deadline()
            used = getattr(self, name)
            # The reserve is not spendable by branch work.  This makes the
            # first simultaneous hit deterministic: wall, then declared cap.
            if self.budgets.wall_seconds >= self.USABLE_POST_BRANCH_SECONDS:
                self._finish(TerminalDisposition.BUDGET_EXHAUSTED, "finalization reserve")
                raise K12BudgetExhausted("finalization reserve")
            if used >= limit:
                self._finish(TerminalDisposition.BUDGET_EXHAUSTED, name)
                raise K12TransitionError("budget exhausted")
            setattr(self, name, used + 1)
            return self._record(kind, self._request or "")

    def step(self): return self._use(TransitionKind.STEPS, "_steps", self.STEP_BUDGET)
    def model(self): return self._use(TransitionKind.MODEL, "_models", self.MODEL_BUDGET)
    def evidence(self): return self._use(TransitionKind.EVIDENCE, "_evidence", self.EVIDENCE_BUDGET)
    def effect(self):
        with self._lock:
            # The R branch cannot replay A's effect.  An effect is admissible
            # only after a genuinely new recovery request; S is terminal.
            if self._selected_arm is K12State.R and self._lifecycle is not LifecycleState.REPLANNING:
                raise K12TransitionError("R has no original effect")
            if self._selected_arm is K12State.A and self._lifecycle not in (LifecycleState.ENTERED, LifecycleState.ADVISORY):
                raise K12TransitionError("effect is out of order")
        return self._use(TransitionKind.EFFECT, "_effects", self.EFFECT_BUDGET)

    @contextmanager
    def model_attempt(self):
        with self._lock:
            if self._model_active:
                self._finish(TerminalDisposition.INVALID_ORDER, "nested model attempt")
                raise K12TransitionError("nested model attempt")
            self._model_active = True
        try:
            yield self.model()
        finally:
            with self._lock: self._model_active = False

    def advisory_would_block(self) -> Transition:
        with self._lock:
            if self._selected_arm is not K12State.A or self._lifecycle is not LifecycleState.ENTERED or not self.shadow or not self.shadow.would_block:
                raise K12TransitionError("advisory is only the A branch origin")
            self._lifecycle = LifecycleState.ADVISORY
            self._branch_started = self._clock()
            return self._record(TransitionKind.ADVISORY, self._request or "", would_block="true")

    def original_advisory_effect(self) -> Transition:
        if self._selected_arm is not K12State.A or not self.shadow or not self.shadow.original_effect:
            raise K12TransitionError("original advisory effect was not admitted")
        return self.effect()

    def matched_post_branch(self) -> Transition:
        if self._selected_arm is not K12State.A or self._lifecycle is not LifecycleState.ADVISORY:
            raise K12TransitionError("matched post-branch requires A advisory")
        return self.step()

    def authority_reject(self, rejection: AuthorityRejection | None = None, *, reason: str | None = None,
                         rejection_id: str | None = None, causal_parent: str | None = None,
                         branch_digest: str | None = None) -> Transition:
        with self._lock:
            # A legacy fixture may enter without selecting an arm and then
            # supply the rejection explicitly.  The selector remains A; the
            # compatibility state field records the rejection branch.
            if self._lifecycle is not LifecycleState.ENTERED or self._selected_arm not in (K12State.R, K12State.S):
                raise K12TransitionError("typed stale permit_validate rejection must be first")
            if rejection:
                reason, rejection_id, causal_parent, branch_digest = rejection.reason, rejection.rejection_id, rejection.causal_parent, rejection.branch_digest
                if rejection.request_id != self._request: raise K12TransitionError("wrong rejection request")
            if not reason or not rejection_id or causal_parent != self._request:
                raise K12TransitionError("typed authority rejection has invalid causal id")
            if self._selected_arm is K12State.S and self._paired_rejection and rejection_id != self._paired_rejection:
                raise K12TransitionError("S rejection is not paired with R")
            if self._selected_arm is K12State.S and self._paired_digest and (branch_digest or self._branch_digest) != self._paired_digest:
                raise K12TransitionError("S branch digest is not paired with R")
            self._rejection_id = rejection_id
            self._branch_started = self._clock()
            self._lifecycle = LifecycleState.REJECTED
            result = self._record(TransitionKind.AUTHORITY_REJECTION, self._request or "", causal_parent,
                                  reason=reason, rejection_id=rejection_id, branch_digest=branch_digest or self._branch_digest or "")
            if self._selected_arm is K12State.S:
                self._finish(TerminalDisposition.STOPPED_AFTER_REJECTION, "paired rejection")
            return result

    def observe_rejection(self) -> Transition:
        with self._lock:
            if self._selected_arm not in (K12State.A, K12State.R) or self._lifecycle is not LifecycleState.REJECTED or self._rejection_observed:
                raise K12TransitionError("exactly one R rejection observation is allowed")
            self._rejection_observed = True; self._lifecycle = LifecycleState.OBSERVED
            return self._record(TransitionKind.OBSERVE_REJECTION, self._request or "", self._rejection_id)

    def validate_recovery_request(self, request_id: str, *, branch_digest: str,
                                   causal_parent: str, candidate_id: str,
                                   attempt_id: str, evidence_root_id: str,
                                   proposal_message_digest: str) -> None:
        """Fail closed on recovery freshness before authority permit issuance."""
        with self._lock:
            if (self._selected_arm is not K12State.R
                    or self._lifecycle is not LifecycleState.OBSERVED
                    or causal_parent != self._request
                    or not all((request_id, branch_digest, candidate_id, attempt_id,
                                evidence_root_id, proposal_message_digest))):
                raise K12TransitionError("recovery request failed pre-permit validation")
            if request_id in self._seen_requests:
                self._finish(TerminalDisposition.REPEATED_REQUEST, "repeated request identity")
                raise K12TransitionError("repeated request")
            if branch_digest == self._branch_digest:
                self._state = K12State.S
                self._finish(TerminalDisposition.BRANCH_EQUIVALENT, "repeated semantic digest")
                raise K12TransitionError("repeated semantic digest")
            self._validated_recovery = (request_id, branch_digest, causal_parent,
                                        candidate_id, attempt_id, evidence_root_id,
                                        proposal_message_digest)
            self._lifecycle = LifecycleState.PROPOSAL_VALIDATED

    def recovery(self, request_id: str, *, permit_id: str, effect_id: str, evidence_root_id: str,
                  oracle_id: str, branch_digest: str, causal_parent: str, candidate_id: str | None = None,
                  attempt_id: str | None = None, eadm_id: str | None = None,
                  proposal_message_digest: str | None = None) -> Transition:
        with self._lock:
            if self._selected_arm is not K12State.R or self._lifecycle is not LifecycleState.PROPOSAL_VALIDATED:
                raise K12TransitionError("recovery requires the observed R rejection")
            expected = (request_id, branch_digest, causal_parent, candidate_id or request_id,
                        attempt_id or request_id, evidence_root_id, proposal_message_digest or "")
            if self._validated_recovery != expected:
                raise K12TransitionError("recovery does not consume the validated proposal")
            ids = (permit_id, effect_id, evidence_root_id, oracle_id, candidate_id or request_id,
                   attempt_id or request_id, eadm_id or oracle_id)
            if causal_parent != self._request or not all(ids): raise K12TransitionError("fresh causal identities required")
            if request_id in self._seen_requests:
                self._finish(TerminalDisposition.REPEATED_REQUEST, "repeated semantic request")
                raise K12TransitionError("repeated request")
            if branch_digest == self._branch_digest:
                self._state = K12State.S
                self._finish(TerminalDisposition.BRANCH_EQUIVALENT, "repeated semantic digest")
                return self._trace[-1] if self._trace else self._record(
                    TransitionKind.STOP, self._request or "", self._rejection_id,
                    reason="repeated semantic digest")
            self._validated_recovery = None
            self._request = request_id; self._seen_requests.add(request_id); self._lifecycle = LifecycleState.REPLANNING
            self._state = K12State.A
            return self._record(TransitionKind.RECOVERY_REQUEST, request_id, causal_parent,
                                permit_id=permit_id, effect_id=effect_id, evidence_root_id=evidence_root_id,
                                oracle_id=oracle_id, candidate_id=candidate_id or request_id,
                                attempt_id=attempt_id or request_id, eadm_id=eadm_id or oracle_id)

    def stop(self, disposition: TerminalDisposition = TerminalDisposition.COMPLETED) -> TerminalCandidate:
        with self._lock:
            if disposition is TerminalDisposition.RECOVERED:
                raise K12TransitionError("R cannot self-declare RECOVERED")
            self._deadline()
            self._finish(disposition)
            return self._terminal  # type: ignore[return-value]

    def raw_terminal_candidate(self, reason: str = "candidate produced by fresh R content") -> TerminalCandidate:
        """Emit an unratified candidate; only an external evaluator may recover it."""
        with self._lock:
            if self._selected_arm is not K12State.R or self._lifecycle is not LifecycleState.REPLANNING:
                raise K12TransitionError("raw candidate requires fresh R replanning")
            self._deadline()
            self._finish(TerminalDisposition.COMPLETED, reason)
            return self._terminal  # type: ignore[return-value]


K12StateMachine = K12Orchestrator
__all__ = ["AuthorityRejection", "BudgetSnapshot", "ParentBudgetAuthority", "K12BudgetExhausted", "K12Orchestrator", "K12State", "K12StateMachine",
           "K12TransitionError", "LifecycleState", "ShadowObservation", "TerminalCandidate",
           "TerminalDisposition", "Transition", "TransitionKind"]
