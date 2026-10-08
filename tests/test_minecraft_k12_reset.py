from dataclasses import replace

import pytest

from benchmarks.minecraft.k12_fixture import build_k12_fixture
from benchmarks.minecraft.k12_reset import K12ObservedResetState, K12ResetAuthority, K12ResetError


def test_reset_is_attested_and_single_use():
    authority = K12ResetAuthority(build_k12_fixture())
    attestation = authority.attest(cell_id="cell-1")
    token = authority.issue(attestation)
    result = authority.launch(token, attestation)
    assert result["status"] == "reset"
    with pytest.raises(K12ResetError):
        authority.launch(token, attestation)


def test_reset_mismatch_and_tamper_fail_closed():
    authority = K12ResetAuthority(build_k12_fixture())
    attestation = authority.attest(cell_id="cell-1")
    with pytest.raises(K12ResetError):
        authority.issue(replace(attestation, world_digest="unknown"))
    token = authority.issue(attestation)
    with pytest.raises(K12ResetError):
        authority.launch(token, replace(attestation, digest="unknown"))


def test_reset_requires_matching_typed_observation_and_issues_unique_tokens():
    fixture = build_k12_fixture()
    authority = K12ResetAuthority(fixture)
    observation = K12ObservedResetState.from_fixture(fixture, cell_id="cell-1")
    first = authority.issue(authority.attest(observation))
    second = authority.issue(authority.attest(observation))
    assert first.token_id != second.token_id
    with pytest.raises(K12ResetError):
        authority.issue(replace(authority.attest(observation), inventory_digest="wrong"))
