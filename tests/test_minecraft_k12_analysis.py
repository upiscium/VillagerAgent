import pytest
from itertools import count

from benchmarks.minecraft.k12_analysis import analyze
from benchmarks.minecraft.k12_artifacts import aggregate_cells
from benchmarks.minecraft.k12_runner import K12Campaign, K12RunnerError

_BLOCKED_SERIAL = count()


def results(count=3):
    campaign = K12Campaign()
    for _ in range(count):
        campaign.launch()
    return campaign.results()


def blocked_aggregate(count):
    campaign = K12Campaign(campaign_seed=f"analysis-blocked-{count}-{next(_BLOCKED_SERIAL)}")
    for _ in range(count - 1):
        campaign.launch()
    with pytest.raises(K12RunnerError):
        campaign.launch(containment_hook=lambda client: setattr(client, "unknown_descendants", True))
    return campaign.finalize()[0]


def test_incomplete_triplets_are_excluded_from_pairing():
    aggregate = blocked_aggregate(1)
    cell = aggregate.cells[0]
    analysis = analyze(aggregate)
    assert analysis.paired_triplets == ()
    assert len(analysis.excluded_triplets) == 30
    assert cell.triplet_id in analysis.excluded_triplets
    assert analysis.recovery_by_stratum == tuple((f"S{index}", 0) for index in range(1, 6))


def test_k8_identity_is_rejected():
    aggregate = blocked_aggregate(1)
    with pytest.raises(ValueError):
        analyze(aggregate, analysis_id="k8-analysis")


def test_schedule_report_is_propagated_and_differences_have_distinct_meanings():
    aggregate = blocked_aggregate(3)
    cells = aggregate.cells
    analysis = analyze(aggregate)
    triplet = cells[0].triplet_id
    assert analysis.schedule_report == aggregate.schedule_report
    assert analysis.paired_differences == ()
    assert analysis.utility_differences == ()
    assert analyze(blocked_aggregate(2)).identity != analysis.identity


def test_analysis_revalidates_all_aggregate_fields():
    aggregate = blocked_aggregate(1)
    object.__setattr__(aggregate, "schedule_count", 89)
    with pytest.raises(ValueError, match="aggregate identity or authentication"):
        analyze(aggregate)
