import json

import gc
import pytest

from benchmarks.common.eac.canonical import thaw_json
from benchmarks.minecraft.k12_fixture import build_k12_fixture
from benchmarks.minecraft.k12_protocol import build_k12_cells
from benchmarks.minecraft.k12_runner import K12Campaign, K12RunnerError
from benchmarks.minecraft.k12_orchestration import ParentBudgetAuthority
from benchmarks.minecraft.k12_worker import K12Worker, launch_worker


def run_one(campaign):
    return campaign.launch()


def test_parent_materializes_full_90_slot_ledger_before_launch():
    campaign = K12Campaign()
    assert len(campaign.ledger) == 90
    assert all(slot.status == "not_started" and slot.terminal is None for slot in campaign.ledger)


def test_malformed_event_fails_and_cannot_be_replaced():
    campaign = K12Campaign()
    with pytest.raises(K12RunnerError):
        campaign.launch(lambda cell, worker_id: [b"not-json"])
    assert campaign.ledger[0].status == "terminal"
    run_one(campaign)
    assert campaign.ledger[1].status == "terminal"


def test_containment_unknown_blocks_next_cell_without_replacement():
    campaign = K12Campaign(campaign_seed="containment-block-test")
    with pytest.raises(K12RunnerError, match="INFRASTRUCTURE_FAILURE|containment"):
        campaign.launch(containment_hook=lambda client: setattr(client, "unknown_descendants", True))
    assert campaign.ledger[0].status == "terminal"
    with pytest.raises(K12RunnerError):
        run_one(campaign)
    aggregate, analysis = campaign.finalize()
    assert aggregate.campaign_disposition == "INFRASTRUCTURE_STOP"
    assert (aggregate.attempted, aggregate.schedule_report.not_started) == (1, 89)
    assert all(slot.status == "not_started" for slot in campaign.ledger[1:])
    assert {slot.stop_reference for slot in campaign.ledger[1:]} == {
        aggregate.campaign_stop.identity}
    assert analysis.paired_triplets == ()
    sibling = K12Campaign(campaign_seed="containment-block-test")
    with pytest.raises(K12RunnerError, match="permanently stopped"):
        sibling.launch()


def test_terminal_is_parent_append_only_and_campaign_identity_is_deterministic():
    first, second = K12Campaign(campaign_seed="fixed"), K12Campaign(campaign_seed="fixed")
    assert first.campaign_id == second.campaign_id
    assert first.cohort_id == second.cohort_id
    run_one(first)
    assert first.ledger[0].terminal.event == "cell_terminal"
    assert first.results()[0].disposition.value == "A_TERMINAL"


def test_same_identity_alias_retains_shared_state_after_original_collection():
    original = K12Campaign(campaign_seed="shared-state")
    sibling = K12Campaign(campaign_seed="shared-state")
    original.launch()
    del original
    gc.collect()
    third = K12Campaign(campaign_seed="shared-state")
    assert len(third.results()) == 1
    assert third.next_cell == sibling.next_cell


def test_full_fake_campaign_can_finalize_only_after_all_slots():
    campaign = K12Campaign()
    for _ in range(90):
        run_one(campaign)
    result = campaign.finalize(lambda ledger: (len(ledger), ledger[-1].status))
    assert result == (90, "terminal")
    aggregate, analysis = campaign.finalize()
    assert aggregate.attempted == 90
    assert analysis.schedule_count == 90
    assert [cell.reset_generation for cell in campaign.results()] == list(range(1, 91))


def test_arm_order_is_not_reordered_by_parent():
    cells = build_k12_cells()
    campaign = K12Campaign(cells)
    observed = []
    run_one(campaign)
    observed.append(cells[0].arm)
    assert observed == [cells[0].arm]


def test_parent_budget_denial_precedes_native_entry():
    campaign = K12Campaign()
    with pytest.raises(K12RunnerError, match="budget admission denied"):
        campaign.launch(budget_admission=lambda: False)
    assert "effect_entered" not in {event.event for event in campaign._traces[campaign.cells[0].cell_id]}


def test_effect_budget_is_open_before_recovery_permit_issuance():
    campaign = K12Campaign(); campaign.launch()
    budget = ParentBudgetAuthority()
    budget.reserve("effects"); budget.reserve("effects")
    result = campaign.launch(budget_authority=budget)
    assert result.status == "terminal"
    trace = campaign._traces[campaign.cells[1].cell_id]
    assert "permit_issued" not in {event.event for event in trace}
    assert campaign.results()[-1].disposition.value == "BUDGET_EXHAUSTED"


def test_reset_attests_one_exact_post_invalidation_state_for_all_arms():
    campaign = K12Campaign()
    for _ in range(3):
        run_one(campaign)
    for cell in campaign.cells[:3]:
        fixture = build_k12_fixture(f"{cell.triplet_id}-fixture", action="MineBlock")
        reset = thaw_json(campaign._traces[cell.cell_id][0].payload)
        assert reset["initial_state_digest"] == fixture.alternative.digest
        assert reset["prior_containment"] == "contained"


def test_parent_finalizes_budget_exhaustion_without_accepting_over_cap_event():
    campaign = K12Campaign()
    run_one(campaign)

    def exhausting_worker(manifest, worker_id):
        worker = K12Worker(manifest, worker_id=worker_id)
        prefix = (
            worker.message("cell_started", {}),
            worker.message("prepared_request_frozen", {}),
            worker.message("invalidation_ingested", {}),
            worker.message("rejection_observation_emitted", {}),
            worker.message("recovery_started", {}),
        )
        steps = tuple(message for step in range(1, 5) for message in (
            worker.message("recovery_step", {"step": step}),
            worker.message("recovery_step_terminal", {"step": step, "outcome": "known"}),
        ))
        evidence = (
            worker.message("observation_started", {"observation_id": "o"}),
            worker.message("observation_terminal", {"observation_id": "o"}),
            worker.message("evidence_ingested", {"evidence_root_id": "e"}),
        )
        return prefix + steps + evidence + (
            worker.message("recovery_step", {"step": 5}),
        )

    run_one_result = campaign.launch(exhausting_worker)
    assert run_one_result.status == "terminal"
    assert campaign.results()[-1].disposition.value == "BUDGET_EXHAUSTED"
    trace = campaign._traces[campaign.cells[1].cell_id]
    assert [event.event for event in trace][-3:] == [
        "budget_reached", "process_finalized", "cell_terminal"]
    assert sum(event.event == "recovery_step" for event in trace) == 4


def test_manifest_corruption_finalizes_zero_attempt_campaign():
    campaign = K12Campaign(campaign_seed="manifest-stop")
    campaign._fixture_manifest["triplets"] = []
    with pytest.raises(K12RunnerError, match="manifest corruption"):
        campaign.launch()
    aggregate, _ = campaign.finalize()
    assert aggregate.campaign_stop.cause == "manifest_corruption"
    assert aggregate.attempted == 0
    assert aggregate.schedule_report.not_started == 90


def test_parent_authority_stop_blocks_preexisting_same_identity_instance():
    first = K12Campaign(campaign_seed="authority-stop")
    second = K12Campaign(campaign_seed="authority-stop")
    first.stop(cause="parent_authority_loss", reason="parent authority unavailable")
    with pytest.raises(K12RunnerError, match="permanently stopped"):
        second.launch()
    aggregate, _ = first.finalize()
    assert aggregate.campaign_stop.cause == "parent_authority_loss"
    assert aggregate.schedule_report.not_started == 90
