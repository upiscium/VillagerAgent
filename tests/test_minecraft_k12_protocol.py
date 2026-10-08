from copy import deepcopy

import pytest

from benchmarks.minecraft.k12_protocol import (
    K12ProtocolError, STRATA, build_k12_cells, load_fixture_manifest,
    load_k12_protocol, validate_fixture_descriptor,
)


def test_k12_census_is_deterministic_and_balanced():
    cells = build_k12_cells()
    assert cells == build_k12_cells()
    assert len(cells) == 90
    for stratum in STRATA:
        assert len({cell.arm_permutation for cell in cells if cell.stratum == stratum}) == 6
        assert [cell.arm for cell in cells if cell.stratum == stratum].count("A") == 6
        assert [cell.arm for cell in cells if cell.stratum == stratum].count("R") == 6
        assert [cell.arm for cell in cells if cell.stratum == stratum].count("S") == 6


def test_checked_in_protocol_is_results_free():
    protocol = load_k12_protocol()
    assert protocol["cell_count"] == 90
    assert protocol["results"] == []


def test_authenticated_protocol_cache_cannot_be_mutated_by_a_caller():
    first = load_k12_protocol()
    expected = first["validated_protocol_digest"]
    first["validated_protocol_digest"] = "caller-controlled"
    assert load_k12_protocol()["validated_protocol_digest"] == expected


def test_authenticated_fixture_descriptor_tamper_fails_closed():
    descriptor = deepcopy(load_fixture_manifest()["triplets"][0])
    validate_fixture_descriptor(descriptor)
    descriptor["seed"] = 99
    with pytest.raises(K12ProtocolError, match="binding|commitment"):
        validate_fixture_descriptor(descriptor)
