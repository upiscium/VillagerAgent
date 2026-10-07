"""Capacity admission and censoring checks for complete K11 snapshots."""
from __future__ import annotations

import pytest

import benchmarks.minecraft.eac_runtime as runtime_module
from benchmarks.minecraft.k11_hold_protocol import (
    CADENCE_NS, FixedPassiveScheduler, K11FatalSensorIngest, SensorBinding,
)
from benchmarks.minecraft.k11_hold_evidence import (
    EXPECTED_OFFSETS, GEOMETRY_ID, K11PassiveTransport, SENSOR_ID,
)
from benchmarks.common.eac import Proposition, PropositionKey, ProvenanceRecord
from test_minecraft_k11_hold_evidence import (
    BRIDGE_A, BRIDGE_B, FakeClock, _adapter, _assert_raw_observations,
    _known_cell, _request, _response_bytes, _submit, runtime_factory,
)


def _runtime_snapshot(runtime):
    authority = runtime.authority
    return (
        runtime._sequence, runtime._evidence_total, dict(runtime._k11_current_roots),
        tuple(runtime._records), authority._sequence, authority._epoch,
        dict(authority._dependency_versions), dict(authority._roots),
        dict(authority._derivations), dict(authority._provenance),
    )


@pytest.mark.parametrize("limit", ["MAX_ROOTS", "MAX_PROVENANCE"])
def test_complete_snapshot_capacity_refusal_is_zero_mutation_and_ineligible(
        runtime_factory, monkeypatch, limit):
    runtime = runtime_factory()
    adapter = _adapter(runtime, FakeClock())
    monkeypatch.setattr(runtime_module, limit, 0)
    planned = []
    original_capacity_check = runtime._check_k11_capacity
    def record_complete_plan(count):
        planned.append(count)
        return original_capacity_check(count)
    monkeypatch.setattr(runtime, "_check_k11_capacity", record_complete_plan)
    before = _runtime_snapshot(runtime)
    request = _request(adapter)
    adapter.register_pending("Alice", 0, request["nonce"], 10_000, 20_000)
    payload, _ = _response_bytes(adapter, request, known=[
        _known_cell({"x": 0, "y": 0, "z": 0}),
        _known_cell({"x": 2, "y": 1, "z": 2}),
    ])
    with pytest.raises(K11FatalSensorIngest) as caught:
        _submit(adapter, request, payload)
    receipt = caught.value.fatal_receipt
    assert receipt["phase"] == "capacity_exhausted"
    assert receipt["committed_cells"] == []
    assert receipt["failing_cell_index"] is None
    assert adapter.closed and not adapter.scientifically_eligible
    assert _runtime_snapshot(runtime) == before
    assert adapter.commit_ledger == ()
    assert adapter.bridge_ids == {}
    assert adapter._last_capture_seq == {}
    assert planned == [2]
    assert receipt["raw_cell_count"] == 75
    _assert_raw_observations(receipt["raw_observations"])
    assert adapter.observation_ledger == ()
    assert adapter.raw_observation_ledger[0]["raw_observations"] == receipt["raw_observations"]


def test_capacity_fatal_censors_scheduler_trace(runtime_factory, monkeypatch):
    runtime = runtime_factory()
    clock = FakeClock()
    adapter = _adapter(runtime, clock)
    monkeypatch.setattr(runtime_module, "MAX_ROOTS", 0)

    class Client:
        def __init__(self, actor, bridge):
            self.actor, self.bridge = actor, bridge
            self.calls = []

        def capture_k11_visible_block_region(self, *, window_id, tick_index, nonce):
            self.calls.append((window_id, tick_index, nonce))
            request = _request(adapter, actor=self.actor, tick=tick_index, nonce=nonce)
            _payload, response = _response_bytes(
                adapter, request, bridge_id=self.bridge,
                known=([_known_cell({"x": 0, "y": 0, "z": 0})]
                       if self.actor == "Alice" else ()),
            )
            return response

    clients = {"Alice": Client("Alice", BRIDGE_A), "Bob": Client("Bob", BRIDGE_B)}
    transport = K11PassiveTransport(
        clients_by_actor=clients, adapter=adapter, t0_ns=100,
        window_close_ns=100 + 2 * CADENCE_NS, clock_ns=clock, synchronous=True,
    )
    bindings = tuple(SensorBinding(
        actor, SENSOR_ID, adapter.sensor_digest, adapter.profile_digest,
        adapter.ingestion_digest, GEOMETRY_ID, adapter.geometry_digest,
    ) for actor in ("Alice", "Bob"))
    scheduler = FixedPassiveScheduler(
        run_id=adapter.run_id, window_id=adapter.window_id, bindings=bindings,
        window_duration_ns=2 * CADENCE_NS, transport=transport,
        ingest=adapter.ingest_callback, clock_ns=clock, t0_ns=100,
    )
    scheduler.poll()
    artifact = scheduler.trace_artifact()
    assert artifact["censored"] is True
    assert artifact["scientifically_eligible"] is False
    assert artifact["fatal_receipt"]["phase"] == "capacity_exhausted"
    assert artifact["fatal_receipt"]["committed_cells"] == []
    assert artifact["fatal_receipt"]["raw_cell_count"] == 75
    _assert_raw_observations(artifact["fatal_receipt"]["raw_observations"])
    assert runtime.authority._roots == runtime.authority._provenance == {}
    assert adapter.observation_ledger == ()


OBSERVABLE_OFFSETS = tuple(p for p in EXPECTED_OFFSETS if p != {"x": 0, "y": 1, "z": 0})


def _scheduled_capture(adapter, clock, actor, tick, known, *, duration_ticks=120):
    due = 100 + tick * CADENCE_NS
    clock.set(due)
    request = _request(adapter, actor=actor, tick=tick)
    adapter.register_pending(actor, tick, request["nonce"], due + 500_000_000,
                             100 + duration_ticks * CADENCE_NS)
    payload, _ = _response_bytes(adapter, request, actor=actor,
                               bridge_id=BRIDGE_A if actor == "Alice" else BRIDGE_B,
                               capture_seq=tick + 1, known=known)
    assert len(payload) <= 65_536
    return _submit(adapter, request, payload)


def test_recovery_static_120_seconds_measures_all_cells_without_semantic_accumulation(runtime_factory):
    runtime = runtime_factory()
    clock = FakeClock()
    adapter = _adapter(runtime, clock)
    air = [_known_cell(pos, name="air", registry_id=0) for pos in OBSERVABLE_OFFSETS]
    raw = noops = transitions = unknown = 0
    peak_roots = peak_provenance = 0
    for tick in range(120):
        for actor in ("Alice", "Bob"):
            receipt = _scheduled_capture(adapter, clock, actor, tick, air)
            rows = receipt.observations
            assert len(rows) == 75
            raw += len(rows)
            transitions += len(receipt.committed_cells)
            noops += sum(row["disposition"] == "semantic_noop" for row in rows)
            unknown += sum(row["state"] == "unknown" for row in rows)
            peak_roots = max(peak_roots, len(runtime.authority._roots))
            peak_provenance = max(peak_provenance, len(runtime.authority._provenance))
    assert (raw, transitions, noops, unknown) == (18_000, 148, 17_612, 240)
    assert peak_roots == peak_provenance == 148
    assert len(adapter.observation_ledger) == len(adapter.raw_observation_ledger) == 240
    assert len(adapter.commit_ledger) == 148
    assert runtime._evidence_total == 148
    assert adapter.scientifically_eligible
    # The witness evaluates the retained semantic store, not 17,760 repeated roots.
    assert len(runtime.authority._roots) <= runtime_module.MAX_ROOTS
    assert len(runtime.authority._provenance) <= runtime_module.MAX_PROVENANCE
    print({"recovery_mock_only": True, "static_seconds": 120, "actors": 2,
           "raw_cells": raw, "transitions": transitions, "semantic_noops": noops,
           "unknown": unknown, "peak_roots": peak_roots, "peak_provenance": peak_provenance})


def test_recovery_noop_nonair_change_unknown_gap_and_actual_polarity_transitions(runtime_factory):
    runtime = runtime_factory(mode="dual_dag_authority")
    clock = FakeClock()
    adapter = _adapter(runtime, clock)
    coordinate = {"x": 0, "y": 0, "z": 0}
    first = _scheduled_capture(adapter, clock, "Alice", 0, [_known_cell(coordinate)])[0]

    def mine_block(*, player_name, x, y, z):
        raise AssertionError("qualification sensing must not execute a prepared action")

    prepared = runtime.prepare_tool("MineBlock", mine_block, (),
                                    {"player_name": "Alice", **coordinate})
    exact = prepared.request
    identity = exact.identity_bytes()
    before = _runtime_snapshot(runtime)
    permit_state = runtime.authority._permits[prepared.permit.permit_id].lifecycle
    for tick, cells in ((1, [_known_cell(coordinate, name="dirt", registry_id=2)]),
                        (2, []), (3, [_known_cell(coordinate)])):
        receipt = _scheduled_capture(adapter, clock, "Alice", tick, cells)
        assert receipt == ()
        assert _runtime_snapshot(runtime) == before
        assert runtime.authority._permits[prepared.permit.permit_id].lifecycle == permit_state
        assert prepared.request is exact and exact.identity_bytes() == identity
    negative = _scheduled_capture(adapter, clock, "Alice", 4,
                                  [_known_cell(coordinate, name="air", registry_id=0)])[0]
    positive = _scheduled_capture(adapter, clock, "Alice", 5, [_known_cell(coordinate)])[0]
    assert negative.proposition.polarity is False and negative.supersedes == (first.root_id,)
    assert positive.proposition.polarity is True and positive.supersedes == (negative.root_id,)
    assert len(runtime.authority._roots) == len(runtime.authority._provenance) == 3
    assert len(adapter.commit_ledger) == 3
    assert runtime.authority.evaluate(exact.candidate_id).admissible is True


def test_recovery_adversarial_cell_flips_stop_before_shared_bound_with_zero_rejected_tick_mutation(runtime_factory):
    """Signed cell-state stress, not a natural simultaneous visibility claim."""
    runtime = runtime_factory()
    clock = FakeClock()
    adapter = _adapter(runtime, clock)
    nonair = [_known_cell(p) for p in OBSERVABLE_OFFSETS]
    air = [_known_cell(p, name="air", registry_id=0) for p in OBSERVABLE_OFFSETS]
    accepted = 0
    for tick in range(28):
        for actor in ("Alice", "Bob"):
            before = _runtime_snapshot(runtime)
            try:
                receipt = _scheduled_capture(adapter, clock, actor, tick,
                                              nonair if tick % 2 == 0 else air)
            except K11FatalSensorIngest as exc:
                assert (tick, actor) == (27, "Bob")
                fatal = exc.fatal_receipt
                assert fatal["phase"] == "capacity_exhausted"
                assert fatal["committed_cells"] == [] and fatal["orphan_provenance"] is False
                assert fatal["raw_cell_count"] == 75
                assert _runtime_snapshot(runtime) == before
                assert accepted == 55
                assert len(runtime.authority._roots) == len(runtime.authority._provenance) == 4070
                assert not adapter.scientifically_eligible and adapter.closed
                print({"recovery_mock_only": True, "adversarial_accepted_batches": accepted,
                       "peak_roots": 4070, "peak_provenance": 4070,
                       "rejected_tick_mutations": 0})
                return
            assert len(receipt.committed_cells) == 74
            accepted += 1
    raise AssertionError("capacity preflight did not stop the adversarial stream")


@pytest.mark.parametrize("occupancy", ["roots_and_provenance", "provenance_only"])
def test_recovery_near_actual_shared_capacity_rejects_complete_plan_before_mutation(runtime_factory, occupancy):
    runtime = runtime_factory()
    adapter = _adapter(runtime, FakeClock())
    for index in range(4095):
        if occupancy == "provenance_only":
            runtime.authority.put_provenance(ProvenanceRecord(f"capacity-seed:{index}", "mock-preexisting"))
        else:
            runtime.ingest_actor_record(
                actor_id="Alice", proposition=Proposition(PropositionKey(
                    "minecraft", "capacity_seed", (index,), "current")),
                record_type="direct_observation", source="mock-preexisting-nonsensor",
            )
    before = _runtime_snapshot(runtime)
    request = _request(adapter)
    adapter.register_pending("Alice", 0, request["nonce"], 10_000, 20_000)
    payload, _ = _response_bytes(adapter, request, known=[
        _known_cell({"x": 0, "y": 0, "z": 0}), _known_cell({"x": 2, "y": 1, "z": 2}),
    ])
    with pytest.raises(K11FatalSensorIngest) as caught:
        _submit(adapter, request, payload)
    assert caught.value.fatal_receipt["phase"] == "capacity_exhausted"
    assert caught.value.fatal_receipt["committed_cells"] == []
    assert _runtime_snapshot(runtime) == before
    assert len(runtime.authority._provenance) == 4095
