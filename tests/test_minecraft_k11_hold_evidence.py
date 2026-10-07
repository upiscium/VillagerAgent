"""Synthetic tests for bounded, authenticated K11 passive evidence ingestion."""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
from pathlib import Path

import pytest

from benchmarks.common.eac import Proposition, PropositionKey
import benchmarks.minecraft.k11_hold_evidence as evidence_module
from benchmarks.minecraft.k11_hold_protocol import (
    CADENCE_NS, DEADLINE_NS, FixedPassiveScheduler, K11FatalSensorIngest,
    SensorBinding, SensorResponse, snapshot_canonical_bytes as canonical_bytes,
)
from benchmarks.minecraft.k11_hold_trace import validate_hold_trace
from benchmarks.minecraft.eac_runtime import (
    K11_V2_IMPLEMENTATION_PATHS, K11_PASSIVE_ISSUER, K11_PASSIVE_STREAM_ID,
    MinecraftEACRuntime,
)
from benchmarks.minecraft.k11_hold_evidence import (
    EXPECTED_OFFSETS, GEOMETRY, GEOMETRY_ID, K11HoldEvidenceAdapter,
    K11HoldEvidenceError, K11PassiveTransport, REQUEST_SCHEMA,
    RESPONSE_BINDING_FIELDS, SENSOR_ID, UNKNOWN_REASONS, _supercover_path,
)


ROOT = Path(__file__).resolve().parents[1]
SECRETS = {"Alice": b"test-only Alice K11 sensor secret 1",
           "Bob": b"test-only Bob K11 sensor secret 2"}
BRIDGE_A, BRIDGE_B = "a" * 32, "b" * 32


@pytest.fixture
def runtime_factory(tmp_path, monkeypatch):
    """Create temporary sealed v2 artifacts; checked-in docs are parent-owned."""
    import benchmarks.minecraft.eac_runtime as runtime_module

    docs = tmp_path / "docs" / "eac"
    docs.mkdir(parents=True)
    legacy_copy = tmp_path / "env" / "eac_observation_adapter.py"
    legacy_copy.parent.mkdir(parents=True)
    for relative in runtime_module.K11_V2_IMPLEMENTATION_PATHS:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    legacy_copy.write_bytes((ROOT / "env/eac_observation_adapter.py").read_bytes())
    legacy_profile = json.loads(runtime_module.SOURCE_PROFILE_PATH.read_text(encoding="utf-8"))
    legacy_contract = json.loads(runtime_module.INGESTION_CONTRACT_PATH.read_text(encoding="utf-8"))
    monkeypatch.setattr(runtime_module, "ROOT", tmp_path)
    monkeypatch.setattr(runtime_module, "SOURCE_PROFILE_V2_PATH",
                        docs / "minecraft_source_profile_v2.json")
    monkeypatch.setattr(runtime_module, "INGESTION_CONTRACT_V2_PATH",
                        docs / "minecraft_ingestion_contract_v2.json")

    contract = dict(legacy_contract)
    contract.pop("detached_artifact_sha256")
    contract["artifact_version"] = 2
    contract["issuer_authentication"] = {
        **contract["issuer_authentication"],
        "rule_id": "minecraft-k11-passive-authentication",
        "issuer": K11_PASSIVE_ISSUER,
        "profile_id": "minecraft-eac-k11-fixed-passive",
    }
    adapter_copy = tmp_path / "benchmarks" / "minecraft" / "k11_hold_evidence.py"
    contract["trusted_observation_adapter"] = {
        **contract["trusted_observation_adapter"],
        "tool_identity": "minecraft-k11-hold-evidence-adapter",
        "tool_version": "1",
        "implementation_path": "benchmarks/minecraft/k11_hold_evidence.py",
        "implementation_sha256": hashlib.sha256(adapter_copy.read_bytes()).hexdigest(),
        "sensor_identity": SENSOR_ID,
        "sensor_implementation_path": "env/k11_visible_block_capture.js",
        "sensor_implementation_sha256": hashlib.sha256(
            (ROOT / "env/k11_visible_block_capture.js").read_bytes()).hexdigest(),
        "geometry_identity": GEOMETRY_ID,
        "geometry_digest": hashlib.sha256(canonical_bytes(GEOMETRY)).hexdigest(),
        "bridge_envelope_identity": REQUEST_SCHEMA,
        "accepted_route": "POST /post_k11_visible_block_region_v1",
    }
    contract["implementation_manifest_version"] = 1
    contract["implementation_manifest"] = {
        path: hashlib.sha256((tmp_path / path).read_bytes()).hexdigest()
        for path in K11_V2_IMPLEMENTATION_PATHS
    }
    contract["detached_artifact_sha256"] = runtime_module._digest(contract)

    profile = dict(legacy_profile)
    profile.pop("detached_profile_sha256")
    profile["profile_id"], profile["profile_version"] = "minecraft-eac-k11-fixed-passive", 2
    profile["mapping_rules"] = [*profile["mapping_rules"], {
        "rule_id": "minecraft-k11-passive-direct", "priority": 0,
        "record_namespace": "minecraft", "record_type": "k11_passive_observation",
        "root_type": "direct_observation", "visibility_field": "visible_to",
        "source_lineage_field": "source_lineage_id",
        "upstream_origin_field": "upstream_origin_id",
        "trusted_tool_identity": None, "trusted_tool_version": None,
    }]
    profile["trusted_tools"] = [{
        "tool_identity": "minecraft-k11-hold-evidence-adapter", "tool_version": "1",
        "allowed_proposition_namespaces": ["minecraft"],
        "integrity_contract_sha256": runtime_module._digest(
            contract["trusted_observation_adapter"]),
    }, *profile["trusted_tools"]]
    profile["supersession_streams"] = [*profile["supersession_streams"], {
        "source_stream_id": K11_PASSIVE_STREAM_ID,
        "authorized_issuer": K11_PASSIVE_ISSUER,
        "revision_field": "source_stream_revision",
        "tracked_proposition_rule_id": "minecraft-k11-passive-direct",
    }]
    profile["integrity_contract"] = {
        **profile["integrity_contract"], "contract_version": 2,
        "canonical_content_sha256": contract["detached_artifact_sha256"],
        "issuer_authentication_rule_id": contract["issuer_authentication"]["rule_id"],
        "rule_evaluation_contract_sha256": runtime_module._digest(contract["rule_evaluation"]),
    }
    profile["detached_profile_sha256"] = runtime_module._digest(profile)
    runtime_module.INGESTION_CONTRACT_V2_PATH.write_text(json.dumps(contract), encoding="utf-8")
    runtime_module.SOURCE_PROFILE_V2_PATH.write_text(json.dumps(profile), encoding="utf-8")

    def build(run_id="k11-evidence-test", *, source_version=2,
              mode="dual_dag_advisory"):
        return MinecraftEACRuntime(mode=mode, run_id=run_id, source_version=source_version)
    return build


class FakeClock:
    def __init__(self, value=100):
        self.value = value

    def __call__(self):
        return self.value

    def set(self, value):
        self.value = value


def _adapter(runtime, clock=None, *, actors=SECRETS, window="window-test"):
    return K11HoldEvidenceAdapter(
        runtime=runtime, run_id=runtime.run_id, window_id=window,
        actor_secrets=actors, clock_ns=clock or FakeClock(),
    )


def _request(adapter, actor="Alice", tick=0, nonce=None):
    return {
        "schema": REQUEST_SCHEMA, "run_id": adapter.run_id,
        "window_id": adapter.window_id, "actor_id": actor, "tick_index": tick,
        "nonce": nonce or f"{tick + 1:032x}", "sensor_id": SENSOR_ID,
        "sensor_digest": adapter.sensor_digest, "profile_digest": adapter.profile_digest,
        "ingestion_digest": adapter.ingestion_digest, "geometry_id": GEOMETRY_ID,
        "geometry_digest": adapter.geometry_digest,
    }


def _register(adapter, request, *, deadline=10_000, close=20_000):
    adapter.register_pending(request["actor_id"], request["tick_index"], request["nonce"],
                             deadline, close)


def _known_cell(offset, *, name="stone", registry_id=1, pose=(0.5, 0.0, 0.5), eye_height=1.5):
    foot = tuple(math_floor(value) for value in pose)
    position = tuple(foot[i] + offset[axis] for i, axis in enumerate(("x", "y", "z")))
    eye = (pose[0], pose[1] + eye_height, pose[2])
    eye_voxel = tuple(math_floor(value) for value in eye)
    path = _supercover_path(eye, position) or []
    path = [point for point in path if point not in {eye_voxel, position}]
    return {
        "offset": dict(offset), "position": dict(zip(("x", "y", "z"), position)),
        "state": "known_air" if name in {"air", "cave_air", "void_air"} else "known_non_air",
        "registry_id": registry_id, "block_name": name,
        "coverage": {
            "eye_loaded": {"position": dict(zip(("x", "y", "z"), eye_voxel)),
                           "registry_id": 0, "block_name": "air"},
            "target_loaded": {"position": dict(zip(("x", "y", "z"), position)),
                              "registry_id": registry_id, "block_name": name},
            "path": [{"position": dict(zip(("x", "y", "z"), point)),
                      "registry_id": 0, "block_name": "air"} for point in path],
        },
    }


def math_floor(value):
    return int(value // 1)


def _response_bytes(adapter, request, *, actor=None, bridge_id=BRIDGE_A, capture_seq=1, known=(),
                    reasons=("unloaded_target",), secret=None):
    actor = actor or request["actor_id"]
    if actor != request["actor_id"]:
        raise ValueError("response actor must match the signed request")
    secret = secret or SECRETS[actor]
    pose = {"x": "0.5", "y": "0", "z": "0.5"}
    eye = {"x": "0.5", "y": "1.5", "z": "0.5", "eye_height": "1.5"}
    by_offset = {tuple(cell["offset"][axis] for axis in ("x", "y", "z")): cell
                 for cell in known}
    cells = []
    for index, offset in enumerate(EXPECTED_OFFSETS):
        key = tuple(offset[axis] for axis in ("x", "y", "z"))
        cell = by_offset.get(key)
        if cell is None:
            cell = {"offset": dict(offset), "position": dict(offset), "state": "unknown",
                    "unknown_reason": reasons[index % len(reasons)]}
        cells.append(cell)
    signed = dict(request)
    signed["request_hmac"] = hmac.new(secret, canonical_bytes(signed), hashlib.sha256).hexdigest()
    response = {field: signed[field] for field in RESPONSE_BINDING_FIELDS}
    response.update({
        "bridge_id": bridge_id, "capture_seq": capture_seq,
        "capture_started_monotonic_ns": "101", "capture_ended_monotonic_ns": "102",
        "pose": pose, "eye": eye, "cells": cells,
        "cell_payload_digest": hashlib.sha256(canonical_bytes(cells)).hexdigest(),
        "complete": True, "truncated": False, "error": None,
        "request_digest": hashlib.sha256(canonical_bytes(signed)).hexdigest(),
    })
    response["hmac_sha256"] = hmac.new(secret, canonical_bytes(response), hashlib.sha256).hexdigest()
    return canonical_bytes(response), response


def _reseal(response, actor="Alice"):
    response["cell_payload_digest"] = hashlib.sha256(
        canonical_bytes(response["cells"])).hexdigest()
    response.pop("hmac_sha256", None)
    response["hmac_sha256"] = hmac.new(
        SECRETS[actor], canonical_bytes(response), hashlib.sha256).hexdigest()
    return canonical_bytes(response)


def _submit(adapter, request, payload=None):
    if payload is None:
        payload, _ = _response_bytes(adapter, request)
    return adapter.ingest_callback(request["actor_id"], request, payload)


def _capture(adapter, actor, tick, *, known=(), bridge_id=None):
    request = _request(adapter, actor=actor, tick=tick)
    _register(adapter, request)
    payload, _ = _response_bytes(
        adapter, request, bridge_id=bridge_id or (BRIDGE_A if actor == "Alice" else BRIDGE_B),
        capture_seq=tick + 1, known=known)
    return _submit(adapter, request, payload)


def _assert_raw_observations(observations):
    assert len(observations) == 75
    for index, item in enumerate(observations):
        assert item["cell_index"] == index
        assert "disposition" not in item
        assert "root_id" not in item and "provenance_id" not in item
        if item["state"] == "unknown":
            assert set(item) == {"cell_index", "coordinate", "state", "unknown_reason"}
            assert item["unknown_reason"] in UNKNOWN_REASONS
        else:
            assert set(item) == {"cell_index", "coordinate", "state", "block_name", "registry_id"}


def _assert_receipt_matches_commit_ledger(receipt, ledger):
    """Capture identity is on the receipt; the independent ledger binds every row."""
    fields = {"cell_index", "coordinate", "root_id", "provenance_id", "polarity",
              "supersedes", "ingest_sequence"}
    rows = []
    for row in ledger:
        assert row["actor_id"] == receipt["actor_id"]
        assert row["tick_index"] == receipt["tick_index"]
        assert row["capture_seq"] == receipt["capture_seq"]
        rows.append({field: row[field] for field in fields})
    assert receipt["committed_cells"] == rows
    assert len({row["root_id"] for row in rows}) == len(rows)


def test_complete_private_snapshot_and_unknown_reason_ledger(runtime_factory):
    runtime, adapter = runtime_factory(), None
    adapter = _adapter(runtime)
    root = _capture(adapter, "Alice", 0,
                    known=[_known_cell({"x": 0, "y": 0, "z": 0})])[0]
    assert root.proposition.polarity is True
    assert root.visible_to == ("Alice",)
    assert root.source_stream_revision == 1
    assert len(adapter.observation_ledger[0]["observations"]) == 75
    unknown = next(item for item in adapter.observation_ledger[0]["observations"]
                   if item["state"] == "unknown")
    assert unknown["unknown_reason"] == "unloaded_target"
    assert "root_id" not in unknown and len(runtime.authority._roots) == 1
    raw_capture = adapter.raw_observation_ledger[0]
    assert raw_capture["raw_cell_count"] == 75
    _assert_raw_observations(raw_capture["raw_observations"])


def test_same_coordinate_is_actor_private_with_independent_polarity_history(runtime_factory):
    runtime = runtime_factory()
    adapter = _adapter(runtime)
    coordinate = {"x": 0, "y": 0, "z": 0}
    alice = _capture(adapter, "Alice", 0,
                     known=[_known_cell(coordinate, name="stone")], bridge_id=BRIDGE_A)[0]
    bob = _capture(adapter, "Bob", 0,
                   known=[_known_cell(coordinate, name="air", registry_id=0)],
                   bridge_id=BRIDGE_B)[0]
    assert alice.visible_to == ("Alice",) and bob.visible_to == ("Bob",)
    assert alice.proposition.key == bob.proposition.key
    assert alice.proposition.polarity is True and bob.proposition.polarity is False
    assert alice.supersedes == bob.supersedes == ()
    assert runtime._k11_current_roots[("Alice", alice.proposition.key)] == alice.root_id
    assert runtime._k11_current_roots[("Bob", bob.proposition.key)] == bob.root_id


def test_repeated_known_unknown_gap_and_nonair_repeats_are_semantic_noops(runtime_factory):
    runtime, adapter = runtime_factory(), None
    adapter = _adapter(runtime)
    coordinate = {"x": 0, "y": 0, "z": 0}
    original = _capture(adapter, "Alice", 0,
                        known=[_known_cell(coordinate, name="stone", registry_id=1)])[0]
    def state():
        return (runtime._sequence, runtime.authority._sequence,
                dict(runtime.authority._dependency_versions), set(runtime.authority._roots),
                set(runtime.authority._provenance))
    before = state()
    assert _capture(adapter, "Alice", 1,
                    known=[_known_cell(coordinate, name="dirt", registry_id=2)]) == ()
    assert state() == before
    assert _capture(adapter, "Alice", 2) == ()
    assert state() == before
    assert _capture(adapter, "Alice", 3,
                    known=[_known_cell(coordinate, name="stone", registry_id=1)]) == ()
    assert state() == before
    assert adapter.observation_ledger[2]["observations"][0]["disposition"] == "unknown"
    air = _capture(adapter, "Alice", 4,
                   known=[_known_cell(coordinate, name="air", registry_id=0)])[0]
    assert air.proposition.polarity is False
    assert air.supersedes == (original.root_id,)
    assert air.source_stream_revision > original.source_stream_revision


def test_invalid_order_and_unknown_identity_reject_before_any_mutation(runtime_factory):
    runtime, adapter = runtime_factory(), None
    adapter = _adapter(runtime)
    request = _request(adapter)
    _register(adapter, request)
    payload, response = _response_bytes(adapter, request)
    response["cells"][1]["offset"], response["cells"][0]["offset"] = (
        response["cells"][0]["offset"], response["cells"][1]["offset"])
    with pytest.raises(K11HoldEvidenceError, match="invalid_geometry"):
        _submit(adapter, request, _reseal(response))
    assert runtime.authority._roots == runtime.authority._provenance == {}


def test_late_oversized_safe_provenance_rejects_before_first_known_root(runtime_factory):
    runtime = runtime_factory()
    adapter = _adapter(runtime)
    request = _request(adapter)
    _register(adapter, request)
    payload, _ = _response_bytes(adapter, request, known=[
        _known_cell({"x": 0, "y": 0, "z": 0}),
        _known_cell({"x": 2, "y": 1, "z": 2}, name="a" * 4096, registry_id=44),
    ])
    with pytest.raises(K11HoldEvidenceError, match="invalid_provenance"):
        _submit(adapter, request, payload)
    assert runtime.authority._roots == runtime.authority._provenance == {}
    assert adapter.commit_ledger == ()
    assert adapter.fatal_receipts == ()
    assert len(adapter.raw_observation_ledger) == 1
    _assert_raw_observations(adapter.raw_observation_ledger[0]["raw_observations"])

    request2 = _request(adapter, tick=1)
    _register(adapter, request2)
    _payload, response2 = _response_bytes(adapter, request2)
    response2["cells"][0]["unknown_reason"] = "not_allowlisted"
    with pytest.raises(K11HoldEvidenceError, match="invalid_unknown_cell"):
        _submit(adapter, request2, _reseal(response2))
    assert runtime.authority._roots == runtime.authority._provenance == {}


def test_actual_js_capture_interoperates_with_the_recovery_python_validator(runtime_factory):
    from test_minecraft_k11_hold_capture import _node_capture

    runtime = runtime_factory()
    adapter = _adapter(runtime)
    request = _request(adapter)
    _register(adapter, request)
    snapshot = _node_capture(r"""
const bot = {
  entity: { position: { x: 0.5, y: 0, z: 0.5 }, eyeHeight: 1.5 },
  blockAt(_p, extra) {
    if (extra !== false) throw new Error('chunk loading forbidden');
    return { type: 0, name: 'air' };
  }
};
console.log(helper.createVisibleBlockCapture({ Vec3, mcData })(bot));
""")
    _payload, response = _response_bytes(adapter, request)
    response.update(snapshot)
    receipt = _submit(adapter, request, _reseal(response))
    assert len(receipt.observations) == 75
    assert len(receipt.committed_cells) == 74
    assert len(runtime.authority._roots) == len(runtime.authority._provenance) == 74
    assert all(not root.proposition.polarity for root in receipt)


def test_unexpected_preflight_exception_latches_zero_commit_fatal(runtime_factory, monkeypatch):
    runtime, adapter = runtime_factory(), None
    adapter = _adapter(runtime)
    request = _request(adapter)
    _register(adapter, request)
    monkeypatch.setattr(runtime, "_preflight_k11_observations",
                        lambda *args: (_ for _ in ()).throw(RuntimeError("injected preflight")))
    payload, _ = _response_bytes(adapter, request, known=[
        _known_cell({"x": 0, "y": 0, "z": 0})])
    with pytest.raises(K11FatalSensorIngest) as caught:
        _submit(adapter, request, payload)
    assert caught.value.fatal_receipt["phase"] == "runtime_preflight"
    assert caught.value.fatal_receipt["committed_cells"] == []
    assert caught.value.fatal_receipt["raw_cell_count"] == 75
    _assert_raw_observations(caught.value.fatal_receipt["raw_observations"])
    assert adapter.closed and not adapter.scientifically_eligible
    assert runtime.authority._roots == runtime.authority._provenance == {}


def test_second_actor_cannot_commit_after_first_actor_latches_fatal(runtime_factory, monkeypatch):
    runtime, adapter = runtime_factory(), None
    adapter = _adapter(runtime)
    alice, bob = _request(adapter, actor="Alice"), _request(adapter, actor="Bob")
    _register(adapter, alice)
    _register(adapter, bob)
    payload_a, _ = _response_bytes(adapter, alice, bridge_id=BRIDGE_A, known=[
        _known_cell({"x": 0, "y": 0, "z": 0})])
    payload_b, _ = _response_bytes(adapter, bob, bridge_id=BRIDGE_B, known=[
        _known_cell({"x": 1, "y": 0, "z": 0})])
    entered, allow_failure, bob_started = threading.Event(), threading.Event(), threading.Event()
    original = runtime.authority.ingest_record

    def fail_alice(record, **kwargs):
        if record.get("visible_to") == ["Alice"]:
            entered.set()
            assert allow_failure.wait(timeout=5)
            raise RuntimeError("injected first-cell failure")
        return original(record, **kwargs)

    monkeypatch.setattr(runtime.authority, "ingest_record", fail_alice)
    results = {}
    def call_alice():
        try:
            _submit(adapter, alice, payload_a)
        except BaseException as exc:
            results["alice"] = exc
    def call_bob():
        bob_started.set()
        try:
            _submit(adapter, bob, payload_b)
        except BaseException as exc:
            results["bob"] = exc
    first = threading.Thread(target=call_alice)
    first.start()
    assert entered.wait(timeout=5)
    second = threading.Thread(target=call_bob)
    second.start()
    assert bob_started.wait(timeout=5)
    allow_failure.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert isinstance(results.get("alice"), K11FatalSensorIngest)
    assert isinstance(results.get("bob"), K11HoldEvidenceError)
    assert results["alice"].fatal_receipt["phase"] == "ingest_record"
    assert results["alice"].fatal_receipt["raw_cell_count"] == 75
    _assert_raw_observations(results["alice"].fatal_receipt["raw_observations"])
    assert adapter.closed and not adapter.scientifically_eligible
    assert runtime.authority._roots == {}
    assert len(runtime.authority._provenance) == 1


def test_shared_gate_censors_due_poll_before_paused_fatal_notifier(runtime_factory, monkeypatch):
    runtime = runtime_factory()
    clock = FakeClock(100)
    adapter = _adapter(runtime, clock)
    clients_called = {"Alice": [], "Bob": []}

    class Client:
        def __init__(self, actor, bridge):
            self.actor, self.bridge = actor, bridge

        def capture_k11_visible_block_region(self, *, window_id, tick_index, nonce):
            clients_called[self.actor].append((tick_index, nonce))
            request = _request(adapter, actor=self.actor, tick=tick_index, nonce=nonce)
            _payload, response = _response_bytes(
                adapter, request, actor=self.actor, bridge_id=self.bridge,
                capture_seq=tick_index + 1,
                known=([_known_cell({"x": 0, "y": 0, "z": 0})]
                       if self.actor == "Alice" else ()),
            )
            return response

    clients = {"Alice": Client("Alice", BRIDGE_A), "Bob": Client("Bob", BRIDGE_B)}
    transport = K11PassiveTransport(
        clients_by_actor=clients, adapter=adapter, t0_ns=100,
        window_close_ns=100 + 3 * CADENCE_NS, clock_ns=clock, synchronous=True,
    )
    bindings = tuple(SensorBinding(
        actor, SENSOR_ID, adapter.sensor_digest, adapter.profile_digest,
        adapter.ingestion_digest, GEOMETRY_ID, adapter.geometry_digest,
    ) for actor in ("Alice", "Bob"))
    original_report = FixedPassiveScheduler.report_fatal
    notifier_entered, release_notifier = threading.Event(), threading.Event()
    report_count = 0

    def pause_first_fatal_report(scheduler_self, receipt):
        nonlocal report_count
        report_count += 1
        if report_count == 1:
            notifier_entered.set()
            assert release_notifier.wait(timeout=5)
        return original_report(scheduler_self, receipt)

    monkeypatch.setattr(FixedPassiveScheduler, "report_fatal", pause_first_fatal_report)
    scheduler = FixedPassiveScheduler(
        run_id=adapter.run_id, window_id=adapter.window_id, bindings=bindings,
        window_duration_ns=3 * CADENCE_NS, transport=transport,
        ingest=adapter.ingest_callback, clock_ns=clock, t0_ns=100,
    )
    original_ingest = runtime.authority.ingest_record
    def fail_alice_root(record, **kwargs):
        if record.get("visible_to") == ["Alice"]:
            raise RuntimeError("injected fatal before first Alice root")
        return original_ingest(record, **kwargs)
    monkeypatch.setattr(runtime.authority, "ingest_record", fail_alice_root)

    poll_errors = []
    def initial_poll():
        try:
            scheduler.poll()
        except BaseException as exc:
            poll_errors.append(exc)

    worker = threading.Thread(target=initial_poll)
    worker.start()
    try:
        assert notifier_entered.wait(timeout=5)
        gate_receipt = adapter.admission_gate.fatal_receipt
        assert gate_receipt is not None
        assert gate_receipt["phase"] == "ingest_record"
        assert gate_receipt["committed_cells"] == []
        assert gate_receipt["orphan_provenance"] is True
        assert gate_receipt["raw_cell_count"] == 75
        _assert_raw_observations(gate_receipt["raw_observations"])

        # Tick 1 is due while the scheduler's fatal callback is deliberately
        # paused. Its shared gate must censor before due/request admission.
        clock.set(100 + CADENCE_NS)
        scheduler.poll()
        paused_artifact = scheduler.trace_artifact()
        assert paused_artifact["censored"] is True
        assert paused_artifact["scientifically_eligible"] is False
        assert paused_artifact["fatal_receipt"]["raw_observations"] == gate_receipt["raw_observations"]
        assert not any(row.get("event") == "due" and row.get("tick_index") == 1
                       for row in paused_artifact["events"])
        assert not any(row.get("event") == "request" and row.get("tick_index") == 1
                       for row in paused_artifact["events"])
        assert len(clients_called["Alice"]) == 1
        assert clients_called["Alice"][0][0] == 0
        assert clients_called["Bob"] == []
        assert adapter.raw_observation_ledger[0]["raw_observations"] == gate_receipt["raw_observations"]
    finally:
        release_notifier.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert not poll_errors
    assert clients_called["Alice"][0][0] == 0 and len(clients_called["Alice"]) == 1
    assert clients_called["Bob"] == []
    clock.set(100 + 3 * CADENCE_NS)
    scheduler.poll()
    artifact = scheduler.trace_artifact()
    assert artifact["censored"] is True
    assert artifact["fatal_receipt"]["raw_observations"] == gate_receipt["raw_observations"]
    assert validate_hold_trace(artifact)["valid"] is True


def test_nth_commit_failure_preserves_exact_prior_and_orphan_inventory(runtime_factory, monkeypatch):
    runtime = runtime_factory()
    adapter = _adapter(runtime)
    request = _request(adapter)
    _register(adapter, request)
    coords = [{"x": 0, "y": 0, "z": 0}, {"x": 1, "y": 0, "z": 0},
              {"x": 2, "y": 1, "z": 2}]
    payload, _ = _response_bytes(adapter, request, known=[_known_cell(pos) for pos in coords])
    original = runtime.authority.ingest_record
    calls = []
    def fail_second(record, **kwargs):
        calls.append(kwargs["root_id"])
        if len(calls) == 2:
            raise RuntimeError("injected second-root failure")
        return original(record, **kwargs)
    monkeypatch.setattr(runtime.authority, "ingest_record", fail_second)
    with pytest.raises(K11FatalSensorIngest) as caught:
        _submit(adapter, request, payload)
    receipt = caught.value.fatal_receipt
    assert receipt["status"] == "partial_commit_fatal"
    assert receipt["phase"] == "ingest_record"
    assert receipt["failing_cell_index"] == EXPECTED_OFFSETS.index(coords[1])
    assert len(receipt["committed_cells"]) == 1
    _assert_receipt_matches_commit_ledger(receipt, adapter.commit_ledger)
    assert len(runtime.authority._roots) == 1
    assert len(runtime.authority._provenance) == 2
    assert receipt["orphan_provenance"] is True
    assert receipt["orphan_provenance_id"] in runtime.authority._provenance
    assert len(calls) == 2
    assert receipt["raw_cell_count"] == 75
    _assert_raw_observations(receipt["raw_observations"])
    assert adapter.raw_observation_ledger[0]["raw_observations"] == receipt["raw_observations"]


def test_root_inserted_before_authority_raises_is_inventoried_once(runtime_factory, monkeypatch):
    runtime = runtime_factory()
    adapter = _adapter(runtime)
    request = _request(adapter)
    _register(adapter, request)
    payload, _ = _response_bytes(adapter, request, known=[
        _known_cell({"x": 0, "y": 0, "z": 0}),
        _known_cell({"x": 2, "y": 1, "z": 2}),
    ])
    original = runtime.authority.ingest_record
    calls = []

    def inserted_then_failed(record, **kwargs):
        calls.append(kwargs["root_id"])
        original(record, **kwargs)
        raise RuntimeError("synthetic authority exception after root insertion")

    monkeypatch.setattr(runtime.authority, "ingest_record", inserted_then_failed)
    with pytest.raises(K11FatalSensorIngest) as caught:
        _submit(adapter, request, payload)
    fatal = caught.value.fatal_receipt
    assert fatal["status"] == "partial_commit_fatal"
    assert fatal["phase"] == "ingest_record"
    assert len(calls) == 1
    assert len(fatal["committed_cells"]) == len(adapter.commit_ledger) == 1
    _assert_receipt_matches_commit_ledger(fatal, adapter.commit_ledger)
    entry = fatal["committed_cells"][0]
    assert entry["root_id"] in runtime.authority._roots
    assert entry["provenance_id"] in runtime.authority._provenance
    assert fatal["failing_cell_index"] == entry["cell_index"]
    assert fatal["orphan_provenance"] is False
    assert runtime._evidence_total == 1
    assert any(row["root_id"] == entry["root_id"] for row in runtime._records)
    assert not adapter.scientifically_eligible and adapter.closed


def test_post_commit_receipt_failure_latches_while_ledger_is_serialized(
        runtime_factory, monkeypatch):
    runtime = runtime_factory()
    adapter = _adapter(runtime)
    request = _request(adapter)
    _register(adapter, request)
    payload, _ = _response_bytes(adapter, request, known=[
        _known_cell({"x": 0, "y": 0, "z": 0})])
    monkeypatch.setattr(evidence_module, "K11CommitReceipt", lambda *args, **kwargs: (
        _ for _ in ()).throw(RuntimeError("injected receipt failure")))
    with pytest.raises(K11FatalSensorIngest) as caught:
        _submit(adapter, request, payload)
    assert caught.value.fatal_receipt["phase"] == "post_commit_receipt"
    assert len(adapter.commit_ledger) == len(runtime.authority._roots) == 1
    assert adapter.closed and not adapter.scientifically_eligible


def test_post_insert_commit_ledger_callback_is_reconciled_before_unlock(
        runtime_factory, monkeypatch):
    runtime = runtime_factory()
    adapter = _adapter(runtime)
    request = _request(adapter)
    _register(adapter, request)
    payload, _ = _response_bytes(adapter, request, known=[
        _known_cell({"x": 0, "y": 0, "z": 0})])
    original = adapter._record_commit
    def append_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected ledger callback failure")
    monkeypatch.setattr(adapter, "_record_commit", append_then_fail)
    with pytest.raises(K11FatalSensorIngest) as caught:
        _submit(adapter, request, payload)
    assert caught.value.fatal_receipt["phase"] == "commit_ledger"
    assert len(caught.value.fatal_receipt["committed_cells"]) == 1
    _assert_receipt_matches_commit_ledger(caught.value.fatal_receipt, adapter.commit_ledger)
    assert len(runtime.authority._roots) == 1


@pytest.mark.parametrize("field", ["hmac_sha256", "request_digest", "complete"])
def test_signed_response_tampering_never_creates_root(runtime_factory, field):
    runtime, adapter = runtime_factory(), None
    adapter = _adapter(runtime)
    request = _request(adapter)
    _register(adapter, request)
    payload, response = _response_bytes(adapter, request, known=[
        _known_cell({"x": 0, "y": 0, "z": 0})])
    if field == "hmac_sha256":
        response[field] = "0" * 64
        payload = canonical_bytes(response)
    else:
        response[field] = False if field == "complete" else "0" * 64
        payload = _reseal(response)
    with pytest.raises(K11HoldEvidenceError):
        _submit(adapter, request, payload)
    assert runtime.authority._roots == runtime.authority._provenance == {}


def test_actual_async_sensor_workers_progress_while_concrete_action_is_retained(runtime_factory):
    """A capture/retained interval is not an EAC mutation critical section."""
    runtime = runtime_factory()
    clock = FakeClock()
    adapter = _adapter(runtime, clock)
    entered = threading.Event()
    release = threading.Event()
    completed = {actor: threading.Event() for actor in ("Alice", "Bob")}
    forbidden_calls = []
    controller = {"task_dag": ["unchanged"], "planner_revision": 0}
    saved_controller = json.dumps(controller, sort_keys=True)

    class Forbidden:
        def __call__(self, *args, **kwargs):
            forbidden_calls.append("planner/model/tool")
            raise AssertionError("sensing crossed a prohibited boundary")

        def __getattr__(self, name):
            forbidden_calls.append(name)
            raise AssertionError("sensing accessed a prohibited boundary")

    def native_action(**kwargs):
        forbidden_calls.append("native_action")
        raise AssertionError("retained action was executed by sensing")

    prepared = runtime.prepare_tool("MineBlock", native_action, (),
                                    {"player_name": "Alice", "x": 1, "y": 64, "z": -2})
    exact = prepared.request
    saved_identity = exact.identity_bytes()
    saved_candidates = tuple(runtime.authority._candidates)
    saved_permits = tuple(runtime.authority._permits)
    saved_actions = tuple(runtime.authority._action_definitions)
    saved_preconditions = tuple(runtime.authority._epre_definitions)
    runtime.prepare_tool = runtime.mediate_tool = Forbidden()
    runtime.authority.register_candidate = Forbidden()
    runtime.execute_prepared = Forbidden()
    runtime.planner = runtime.model = Forbidden()
    runtime.controller = controller

    class Client:
        def __init__(self, actor):
            self.actor = actor

        def __getattr__(self, name):
            forbidden_calls.append(name)
            raise AssertionError("passive client used non-observation API")

        def capture_k11_visible_block_region(self, *, window_id, tick_index, nonce):
            assert prepared.request is exact and exact.identity_bytes() == saved_identity
            if self.actor == "Alice":
                entered.set()
                assert release.wait(timeout=5)
            request = _request(adapter, actor=self.actor, tick=tick_index, nonce=nonce)
            _bytes, response = _response_bytes(
                adapter, request, actor=self.actor,
                bridge_id=BRIDGE_A if self.actor == "Alice" else BRIDGE_B,
                capture_seq=tick_index + 1,
                known=[_known_cell({"x": 0, "y": 0, "z": 0}, name="air", registry_id=0)],
            )
            return response

    actual_transport = K11PassiveTransport(
        clients_by_actor={actor: Client(actor) for actor in ("Alice", "Bob")},
        adapter=adapter, t0_ns=100, window_close_ns=100 + 2 * CADENCE_NS,
        clock_ns=clock,
    )

    def submit(request, receive):
        actor = request["actor_id"]
        def terminal(result):
            try:
                receive(result)
            finally:
                completed[actor].set()
        actual_transport(request, terminal)

    bindings = tuple(SensorBinding(actor, SENSOR_ID, adapter.sensor_digest,
                                   adapter.profile_digest, adapter.ingestion_digest,
                                   GEOMETRY_ID, adapter.geometry_digest)
                     for actor in ("Alice", "Bob"))
    scheduler = FixedPassiveScheduler(
        run_id=adapter.run_id, window_id=adapter.window_id, bindings=bindings,
        window_duration_ns=2 * CADENCE_NS, transport=submit,
        ingest=adapter.ingest_callback, clock_ns=clock, t0_ns=100,
    )
    try:
        scheduler.poll()
        assert entered.wait(timeout=5)
        assert completed["Bob"].wait(timeout=5)
        assert len(adapter.commit_ledger) == 1
        assert adapter.commit_ledger[0]["actor_id"] == "Bob"
        assert any(root.visible_to == ("Bob",) for root in runtime.authority._roots.values())
        assert runtime._lock.acquire(blocking=False)
        runtime._lock.release()
        assert runtime.authority._lock.acquire(blocking=False)
        runtime.authority._lock.release()
        assert prepared.request is exact and exact.identity_bytes() == saved_identity
    finally:
        release.set()
    assert completed["Alice"].wait(timeout=5)
    assert scheduler.counters["completed"] == 2
    assert forbidden_calls == []
    assert json.dumps(controller, sort_keys=True) == saved_controller
    assert tuple(runtime.authority._candidates) == saved_candidates
    assert tuple(runtime.authority._permits) == saved_permits
    assert tuple(runtime.authority._action_definitions) == saved_actions
    assert tuple(runtime.authority._epre_definitions) == saved_preconditions
    assert prepared.request is exact and exact.identity_bytes() == saved_identity
    assert validate_hold_trace(scheduler.trace_artifact())["valid"] is True
