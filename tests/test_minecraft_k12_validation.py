import pytest

from benchmarks.minecraft.k12_fixture import STRATUM_ACTIONS, build_k12_fixture
from benchmarks.minecraft.k12_identity import FROZEN_ARGUMENT_SPECS
from benchmarks.minecraft.k12_runner import K12Campaign
from benchmarks.minecraft.k12_validation import Disposition, validate_cell
from benchmarks.minecraft.k12_worker import K12Worker


def _recovered_campaign():
    campaign = K12Campaign()
    campaign.launch()  # A
    campaign.launch()  # R
    cell = campaign.cells[1]
    return campaign, cell


def test_recovery_is_derived_from_authenticated_raw_evidence():
    campaign, cell = _recovered_campaign()
    result = validate_cell(
        campaign._traces[cell.cell_id], cell_id=cell.cell_id,
        triplet_id=cell.triplet_id, expected_arm="R",
        expected_reset_generation=2,
        evidence_snapshot=campaign._evidence_snapshots[cell.cell_id],
        expected_protocol_digest=campaign._protocol_digest,
        expected_campaign_id=campaign.campaign_id, expected_cohort_id=campaign.cohort_id,
        expected_campaign_seed=campaign.campaign_seed,
    )
    assert result.disposition is Disposition.RECOVERED


def test_recovery_authority_order_is_parent_proposal_before_permit():
    campaign, cell = _recovered_campaign()
    kinds = [event.event for event in campaign._traces[cell.cell_id]]
    assert kinds.index("recovery_proposed") < kinds.index("proposal_validated")
    assert kinds.index("proposal_validated") < kinds.index("new_request_prepared")
    assert kinds.index("new_request_prepared") < kinds.index("permit_issued")
    assert kinds.index("permit_issued") < kinds.index("effect_entered")


def test_authority_recovery_requires_immutable_parent_evidence_snapshot():
    campaign, cell = _recovered_campaign()
    with pytest.raises(ValueError, match="authentication context"):
        validate_cell(
            campaign._traces[cell.cell_id], cell_id=cell.cell_id,
            triplet_id=cell.triplet_id, expected_arm="R",
            expected_reset_generation=2,
        )


def test_semantic_repetition_terminalizes_before_request_or_permit():
    campaign = K12Campaign()
    campaign.launch()  # A
    cell = campaign.cells[1]
    action = STRATUM_ACTIONS[cell.stratum]
    fixture = build_k12_fixture(f"{cell.triplet_id}-fixture", action=action,
                                template=cell.template, seed=cell.seed)
    original = dict(fixture.original_arguments)
    if action in {"MineBlock", "placeBlock"}:
        x, y, z = original["position"]
        runtime = {"x": x, "y": y, "z": z}
        if action == "placeBlock":
            runtime.update(item_name=original["item"], facing=original["facing"])
    elif action == "navigateTo":
        x, y, z = original["target"]; runtime = {"x": x, "y": y, "z": z}
    elif action == "attackTarget":
        runtime = {"target_name": original["target"]}
    else:
        runtime = {"target_player_name": original["recipient"], "item_name": original["item"],
                   "item_count": original["quantity"]}
    semantic = {name: runtime[name] for name in FROZEN_ARGUMENT_SPECS[action]}

    def repeated_worker(manifest, worker_id):
        return K12Worker(manifest, worker_id=worker_id).run(payload={
            "proposal_action": action, "proposal_arguments": semantic,
        })

    campaign.launch(repeated_worker)
    result = campaign.results()[-1]
    assert result.disposition is Disposition.REPEATED_ORIGINAL_REQUEST
    kinds = {event.event for event in campaign._traces[cell.cell_id]}
    assert not {"new_request_prepared", "permit_issued", "effect_decision",
                "effect_entered", "effect_terminal"}.intersection(kinds)
