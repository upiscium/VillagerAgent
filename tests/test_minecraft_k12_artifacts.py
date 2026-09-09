from itertools import count

import pytest

from benchmarks.common.eac.canonical import canonical_sha256
from benchmarks.minecraft.k12_artifacts import K12CampaignStop, aggregate_cells
from benchmarks.minecraft.k12_protocol import build_k12_cells, load_k12_protocol
from benchmarks.minecraft.k12_runner import K12Campaign, K12RunnerError
from benchmarks.minecraft.k12_validation import Disposition, K12CellResult

_BLOCKED_SERIAL = count()


def campaign_results(seed="k12", count_=3):
    campaign = K12Campaign(campaign_seed=seed)
    for _ in range(count_):
        campaign.launch()
    return campaign.results()


def blocked_aggregate(count_=2):
    seed = f"artifact-blocked-{count_}-{next(_BLOCKED_SERIAL)}"
    campaign = K12Campaign(campaign_seed=seed)
    for _ in range(count_ - 1):
        campaign.launch()
    with pytest.raises(K12RunnerError):
        campaign.launch(containment_hook=lambda client: setattr(client, "unknown_descendants", True))
    return campaign.finalize()[0]


def test_aggregate_is_immutable_and_finite():
    result = blocked_aggregate(2)
    assert result.campaign_disposition == "INFRASTRUCTURE_STOP"
    assert result.objective_completion == (("A", 1), ("R", 0), ("S", 0))
    assert result.identity


def test_aggregate_rejects_mixed_campaign_results():
    first = campaign_results("first", 2)
    second = campaign_results("second", 2)
    with pytest.raises(ValueError, match="campaign"):
        aggregate_cells([first[0], second[1]])


def test_schedule_report_is_canonical_and_affects_identity():
    result = blocked_aggregate(2)
    assert result.schedule_report.as_tuple() == (90, 2, 2, 2, 1, 1, 0, 0, 0, 88)
    with pytest.raises(AttributeError):
        result.schedule_report.recovered = 0
    assert result.identity != blocked_aggregate(3).identity


def test_aggregate_requires_authenticated_identities_and_frozen_artifact():
    cell = campaign_results(count_=1)[0]
    with pytest.raises(ValueError, match="artifact identity is frozen"):
        aggregate_cells([cell], artifact_id="caller-selected")
    scheduled = build_k12_cells()[0]
    with pytest.raises(TypeError):
        K12CellResult(scheduled.cell_id, scheduled.triplet_id,
                      Disposition.A_TERMINAL, scheduled.arm, True, ())
    forged = cell._replace(objective_completed=not cell.objective_completed)
    with pytest.raises(ValueError, match="replay"):
        forged.require_public_authentication()


def test_zero_attempt_campaign_stop_finalizes_the_full_schedule():
    campaign = K12Campaign(campaign_seed="zero")
    campaign.stop(cause="parent_authority_loss", reason="prelaunch parent authority loss")
    aggregate, _ = campaign.finalize()
    stop = aggregate.campaign_stop
    assert aggregate.schedule_report.as_dict()["scheduled"] == 90
    assert aggregate.schedule_report.as_dict()["attempted"] == 0
    assert aggregate.schedule_report.as_dict()["not_started"] == 90
    assert {slot[1] for slot in aggregate.finalized_slots} == {"not_started"}
    assert {slot[2] for slot in aggregate.finalized_slots} == {stop.identity}
