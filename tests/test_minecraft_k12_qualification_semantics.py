from dataclasses import replace

import pytest

from benchmarks.minecraft.k12_qualification_semantics import (
    PROBES,
    QUALIFICATION_SCHEDULE,
    QUALIFICATION_SCHEDULE_IDENTITY,
    NormalizedCell,
    NormalizedProbe,
    NormalizedRejectionBinding,
    NormalizedTerminal,
    QualificationCensus,
    verify_qualification,
)


def _valid_census() -> QualificationCensus:
    authority = "authority-1"
    activation = "activation-1"
    profile = "profile-1"
    origin = "runtime_verified"
    cell_campaign = "cell-campaign-1"
    probe_campaign = "probe-campaign-1"
    cells = []
    for index, cell_id in enumerate(QUALIFICATION_SCHEDULE):
        arm = cell_id.rsplit("-", 1)[1]
        evidence = f"{index + 1:064x}"
        reset = f"reset-{index}"
        binding = None
        operation = {}
        if arm == "S":
            operation = {
                "reset_token": reset,
                "generation": index + 1,
                "request_identity": f"request-{index}",
                "permit_identity": f"permit-{index}",
                "effect_identity": f"effect-{index}",
            }
            binding = NormalizedRejectionBinding(
                arm="S",
                cell_id=cell_id,
                profile_digest=profile,
                campaign_id=cell_campaign,
                authority=authority,
                activation=activation,
                evidence_origin=origin,
                reset_token=reset,
                generation=index + 1,
                request_identity=operation["request_identity"],
                permit_identity=operation["permit_identity"],
                effect_identity=operation["effect_identity"],
                evidence_digest=evidence,
            )
        cells.append(
            NormalizedCell(
                cell_id=cell_id,
                authority=authority,
                activation=activation,
                profile_digest=profile,
                evidence_origin=origin,
                campaign_id=cell_campaign,
                reset_identity=reset,
                evidence_digest=evidence,
                fresh_root=True,
                reset_passed=True,
                capability_state="REVOKED",
                provider_terminal="success",
                oracle_value="not_applicable" if arm == "S" else "true",
                containment_clean=True,
                evidence_valid=True,
                terminal_verified=True,
                rejection_verified=True,
                retry=False,
                resumed=False,
                replacement=False,
                rejection_binding=binding,
                **operation,
            )
        )
    probes = tuple(
        NormalizedProbe(
            probe=probe,
            authority=authority,
            activation=activation,
            profile_digest=profile,
            evidence_origin=origin,
            campaign_id=probe_campaign,
            passed=True,
            terminal_verified=True,
            evidence_digest=f"probe-{probe}",
        )
        for probe in PROBES
    )
    provisional = QualificationCensus(tuple(cells), probes, None)
    terminal = NormalizedTerminal(
        authority=authority,
        activation=activation,
        profile_digest=profile,
        evidence_origin=origin,
        ledger_digest="sha256:" + "f" * 64,
        aggregate_digest=provisional.aggregate_digest,
        probe_digest=provisional.probe_digest,
    )
    return QualificationCensus(tuple(cells), probes, terminal)


def test_valid_frozen_census_passes_and_is_deterministic():
    first = _valid_census()
    second = _valid_census()

    verdict = verify_qualification(first)
    assert verdict.passed is True
    assert verdict.identity == verify_qualification(second).identity
    assert first.identity == second.identity
    assert first.cells == second.cells
    assert first.probes == second.probes
    assert first.schedule_identity == QUALIFICATION_SCHEDULE_IDENTITY


def test_fourteen_cells_fail_the_exact_fifteen_cell_census():
    census = _valid_census()
    verdict = verify_qualification(
        QualificationCensus(census.cells[:-1], census.probes, census.terminal)
    )
    assert verdict.passed is False
    assert "cell_count" in verdict.reasons


def test_missing_probe_fails_the_exact_ordered_probe_set():
    census = _valid_census()
    verdict = verify_qualification(
        QualificationCensus(census.cells, census.probes[:-1], census.terminal)
    )
    assert verdict.passed is False
    assert "probe_count" in verdict.reasons


@pytest.mark.parametrize("kind", ("duplicate", "order", "domain"))
def test_duplicate_order_and_domain_probes_fail_closed(kind):
    census = _valid_census()
    probes = list(census.probes)
    if kind == "duplicate":
        probes[1] = probes[0]
    elif kind == "order":
        probes[0], probes[1] = probes[1], probes[0]
    else:
        probes[0] = replace(probes[0], probe="P9")
    provisional = QualificationCensus(census.cells, tuple(probes), None)
    terminal = replace(
        census.terminal,
        aggregate_digest=provisional.aggregate_digest,
        probe_digest=provisional.probe_digest,
    )
    verdict = verify_qualification(QualificationCensus(census.cells, tuple(probes), terminal))
    assert verdict.passed is False
    assert any(reason in verdict.reasons for reason in (
        "probe_order", "probe_duplicate", "probe_domain",
    ))


def test_terminal_ledger_projection_mismatch_fails_closed():
    census = _valid_census()
    terminal = replace(census.terminal, aggregate_digest="sha256:" + "0" * 64)
    verdict = verify_qualification(QualificationCensus(census.cells, census.probes, terminal))
    assert verdict.passed is False
    assert "terminal_aggregate" in verdict.reasons


@pytest.mark.parametrize("kind", ("duplicate", "order", "domain"))
def test_duplicate_order_and_domain_cells_fail_closed(kind):
    census = _valid_census()
    cells = list(census.cells)
    if kind == "duplicate":
        cells[1] = cells[0]
    elif kind == "order":
        cells[0], cells[1] = cells[1], cells[0]
    else:
        cells[0] = replace(cells[0], cell_id="K12Q-S9-T1-N1-A")
    provisional = QualificationCensus(tuple(cells), census.probes, None)
    terminal = replace(
        census.terminal,
        aggregate_digest=provisional.aggregate_digest,
        probe_digest=provisional.probe_digest,
    )
    verdict = verify_qualification(QualificationCensus(tuple(cells), census.probes, terminal))
    assert verdict.passed is False
    assert any(reason in verdict.reasons for reason in ("cell_order", "cell_duplicate", "cell_domain"))


def test_cross_authority_cell_fails_common_authority_binding():
    census = _valid_census()
    cells = list(census.cells)
    cells[4] = replace(cells[4], authority="authority-2")
    verdict = verify_qualification(
        QualificationCensus(tuple(cells), census.probes, census.terminal)
    )
    assert verdict.passed is False
    assert "common_authority" in verdict.reasons


@pytest.mark.parametrize(
    ("arm", "altered"),
    (("A", "false"), ("R", "not_applicable"), ("S", "true")),
)
def test_altered_a_r_s_oracle_field_fails(arm, altered):
    census = _valid_census()
    index = next(
        index for index, cell in enumerate(census.cells) if cell.cell_id.endswith(f"-{arm}")
    )
    cells = list(census.cells)
    cells[index] = replace(cells[index], oracle_value=altered)
    provisional = QualificationCensus(tuple(cells), census.probes, None)
    terminal = replace(
        census.terminal,
        aggregate_digest=provisional.aggregate_digest,
        probe_digest=provisional.probe_digest,
    )
    verdict = verify_qualification(QualificationCensus(tuple(cells), census.probes, terminal))
    assert verdict.passed is False
    assert f"oracle_{arm}" in verdict.reasons
