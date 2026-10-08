import pytest

from benchmarks.minecraft.k12_orchestration import (
    K12Orchestrator,
    K12State,
    K12TransitionError,
    TerminalDisposition,
)


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


def entered(clock, digest="branch-a", arm=K12State.R):
    machine = K12Orchestrator(clock=clock, selected_arm=arm)
    machine.enter("request-1", branch_digest=digest)
    return machine


def test_authoritative_recovery_requires_order_and_fresh_identities():
    clock = Clock()
    machine = entered(clock)
    with pytest.raises(K12TransitionError):
        machine.recovery("request-2", permit_id="p", effect_id="e", evidence_root_id="r",
                         oracle_id="o", branch_digest="branch-b", causal_parent="request-1")
    machine.authority_reject(reason="stale", rejection_id="rej-1", causal_parent="request-1")
    machine.observe_rejection()
    with pytest.raises(K12TransitionError):
        machine.validate_recovery_request(
            "request-1", branch_digest="branch-b", causal_parent="request-1",
            candidate_id="c", attempt_id="a", evidence_root_id="r",
            proposal_message_digest="proposal-1")
    assert machine.terminal is not None
    assert machine.terminal.disposition is TerminalDisposition.REPEATED_REQUEST


def test_equivalent_recovery_stops_without_activity():
    clock = Clock()
    machine = entered(clock)
    machine.authority_reject(reason="stale", rejection_id="rej-1", causal_parent="request-1")
    machine.observe_rejection()
    with pytest.raises(K12TransitionError):
        machine.validate_recovery_request(
            "request-2", branch_digest="branch-a", causal_parent="request-1",
            candidate_id="c2", attempt_id="a2", evidence_root_id="r2",
            proposal_message_digest="proposal-2")
    assert machine.state is K12State.S
    assert machine.terminal.disposition is TerminalDisposition.BRANCH_EQUIVALENT
    assert machine.budgets.steps == machine.budgets.model == machine.budgets.evidence == machine.budgets.effects == 0


def test_caps_are_monotonic_and_first_hit_is_deterministic():
    clock = Clock()
    machine = K12Orchestrator(clock=clock, selected_arm=K12State.A)
    machine.enter("request-1", branch_digest="branch-a", shadow_would_block=True)
    machine.advisory_would_block()
    for _ in range(machine.STEP_BUDGET):
        machine.step()
    with pytest.raises(K12TransitionError):
        machine.step()
    assert machine.terminal.disposition is TerminalDisposition.BUDGET_EXHAUSTED
    assert machine.budgets.steps == machine.STEP_BUDGET


def test_nested_model_attempt_is_terminal():
    clock = Clock()
    machine = K12Orchestrator(clock=clock, selected_arm=K12State.A)
    machine.enter("request-1", branch_digest="branch-a", shadow_would_block=True)
    machine.advisory_would_block()
    with pytest.raises(K12TransitionError):
        with machine.model_attempt():
            with machine.model_attempt():
                pass
    assert machine.terminal.disposition is TerminalDisposition.INVALID_ORDER


def test_fake_clock_deadline_is_repeatable():
    clock = Clock()
    machine = K12Orchestrator(clock=clock, selected_arm=K12State.A)
    machine.enter("request-1", branch_digest="branch-a", shadow_would_block=True)
    machine.advisory_would_block()
    clock.value = machine.POST_BRANCH_WALL_SECONDS
    with pytest.raises(K12TransitionError):
        machine.effect()
    assert machine.terminal.disposition is TerminalDisposition.BUDGET_EXHAUSTED


def test_a_keeps_selector_immutable_and_preserves_advisory_effect():
    clock = Clock()
    machine = K12Orchestrator(clock=clock, selected_arm=K12State.A)
    machine.enter("a", branch_digest="a", shadow_would_block=True, original_effect=True)
    machine.advisory_would_block()
    machine.original_advisory_effect()
    machine.matched_post_branch()
    assert machine.selected_arm is K12State.A


def test_r_rejects_original_effect_and_never_self_declares_recovered():
    clock = Clock()
    machine = K12Orchestrator(clock=clock, selected_arm=K12State.R)
    machine.enter("r", branch_digest="old")
    machine.authority_reject(reason="stale permit_validate", rejection_id="rej", causal_parent="r")
    with pytest.raises(K12TransitionError):
        machine.effect()
    machine.observe_rejection()
    machine.validate_recovery_request(
        "new", branch_digest="new-content", causal_parent="r",
        candidate_id="c", attempt_id="a", evidence_root_id="root",
        proposal_message_digest="proposal-new")
    machine.recovery("new", permit_id="p", effect_id="e", evidence_root_id="root",
                     oracle_id="o", candidate_id="c", attempt_id="a", eadm_id="ea",
                     branch_digest="new-content", causal_parent="r",
                     proposal_message_digest="proposal-new")
    machine.effect()
    machine.raw_terminal_candidate()
    assert machine.terminal.disposition is TerminalDisposition.COMPLETED
    with pytest.raises(K12TransitionError):
        machine.stop(TerminalDisposition.RECOVERED)


def test_s_matching_rejection_stops_without_post_rejection_activity():
    clock = Clock()
    machine = K12Orchestrator(clock=clock, selected_arm=K12State.S,
                               paired_branch_digest="same", paired_rejection_id="rej")
    machine.enter("s", branch_digest="same")
    machine.authority_reject(reason="stale permit_validate", rejection_id="rej",
                             causal_parent="s", branch_digest="same")
    count = len(machine.transitions)
    assert machine.terminal.disposition is TerminalDisposition.STOPPED_AFTER_REJECTION
    with pytest.raises(K12TransitionError):
        machine.step()
    assert len(machine.transitions) == count
