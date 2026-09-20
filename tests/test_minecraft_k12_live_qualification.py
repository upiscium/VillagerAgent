import pytest

from benchmarks.minecraft.k12_live_qualification import *
from benchmarks.minecraft.k12_runtime_profile import load_k12_live_runtime_profile

def test_fifteen_cells_and_four_isolated_probes():
    artifact = qualification_artifact()
    assert len(qualification_cells()) == 15
    assert all(cell["status"] == "not_started" for cell in artifact.cells)
    assert all(len(row) == 3 for row in qualification_matrix())
    assert all("probes" not in cell for cell in artifact.cells)

def passing_aggregate():
    profile=load_k12_live_runtime_profile()
    values=[]
    for cell_id in qualification_ids():
        values.append(MockCellQualificationEvidence(cell_id,{"status":"passed"},profile.profile_digest,
            "qualification-campaign","reset-"+cell_id,"evidence-"+cell_id,True,True,
            "REVOKED","success","not_applicable" if cell_id.endswith("-S") else "true",True,True))
    return qualify_mock_campaign(tuple(values))

def test_aggregate_requires_exact_order_bindings_fresh_root_and_no_retry():
    aggregate=passing_aggregate()
    assert aggregate.qualifies()
    profile=load_k12_live_runtime_profile(); cell_id=qualification_ids()[0]
    failed=qualify_mock_cell(MockCellQualificationEvidence(cell_id,{},profile.profile_digest,
        "qualification-campaign","reset","evidence",True,True,"REVOKED","success",
        "true",True,True,retry=True))
    assert not failed.passed

def test_qualification_json_is_authenticated_and_exact():
    value=load_k12_live_qualification()
    assert tuple(value["schedule"]) == qualification_ids()
    assert len(value["detached_artifact_sha256"]) == 64


def test_generic_mock_aggregate_cannot_be_retyped_as_live():
    aggregate = passing_aggregate()
    assert aggregate.execution_provenance == "mock_only"
    assert aggregate.evidence_origin == "test_only"
    with pytest.raises(TypeError):
        LiveQualificationAggregate((), object(), "p", "c", object(), object())


def test_live_cell_and_probe_orders_are_separate_and_exact():
    profile = load_k12_live_runtime_profile()
    assert tuple(cell["cell_id"] for cell in qualification_cells()) == qualification_ids()
    assert PROBES == ("P1", "P2", "P3", "P4")
    cell = LiveQualificationCellEvidence(
        qualification_ids()[0], {"status": "passed"}, profile.profile_digest,
        "qualification-campaign", "reset", "evidence", True, True,
        "REVOKED", "success", "true", True, True,
    )
    assert cell.evidence_origin == "injected_fake"
    with pytest.raises(ValueError):
        qualify_mock_probes((MockProbeEvidence("P1", True, profile.profile_digest,
            "probe-campaign", "evidence"),))
