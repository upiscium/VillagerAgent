import copy
import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

from benchmarks.common.eac import ActionRef, ExactRequest
from benchmarks.common.eac.canonical import canonical_bytes
from benchmarks.minecraft import k10_protocol as k10_protocol_module
from benchmarks.minecraft.k10_protocol import (
    CANDIDATE_POOL_DIGEST,
    CONDITIONS,
    EXACT_FIELDS,
    EXPECTED_SELECTED,
    SELECTION_MANIFEST_DIGEST,
    K10ContractError,
    SUBJECT_RUNTIME_REFERENCE,
    _validate_frozen_protected_content,
    aggregate_k10_results,
    audit_historical_submissions,
    build_k10_cells,
    detached_digest,
    expected_action_digest,
    load_k10_candidate_pool,
    load_k10_inventory,
    load_k10_protocol,
    trace_pairing_digest,
    validate_k10_trace,
    validate_live_k10_checkout,
)


def _write_detached(path, value):
    value = copy.deepcopy(value)
    value["detached_artifact_sha256"] = detached_digest(value)
    path.write_text(json.dumps(value), encoding="utf-8")


def _execution_phase(identity, *, eadm, execution, permit_fresh, epoch):
    return {
        **identity,
        "current_EAdm": eadm,
        "authority_epoch_before_execution": epoch,
        "exact_action_submitted": True,
        "permit_or_shadow_fresh": permit_fresh,
        "EnvPre_oracle": True,
        "SecPre_oracle": True,
        "execution_allowed": execution,
        "rejection_reason": None if execution else "stale",
        "native_callable_reached": execution,
    }


def _identity(item, suffix="1"):
    identity = {
        "candidate_id": f"candidate-{suffix}",
        "attempt_id": f"attempt-{suffix}",
        "action": {"identity": item.action_identity, "version": item.action_version,
                   "digest": expected_action_digest(item)},
        "arguments": item.request(),
        "target": item.target(),
    }
    request = ExactRequest(
        identity["candidate_id"], identity["attempt_id"],
        ActionRef(identity["action"]["identity"], identity["action"]["version"],
                  identity["action"]["digest"]),
        tuple(identity["arguments"].items()), identity["target"],
    )
    identity["exact_request_digest"] = (
        "sha256:" + hashlib.sha256(request.identity_bytes()).hexdigest())
    return identity


def synthetic_trace(cell):
    protocol = load_k10_protocol()
    item = next(item for item in load_k10_inventory() if item.inventory_id == cell.inventory_id)
    identity = _identity(item)
    relevant = cell.scenario_family in {"S1", "S2", "S3"}
    c2 = cell.scenario_family == "C2"
    epoch = 1 if c2 else 2
    rp = {
        **identity, "EAdm": True, "authority_epoch": 1,
        "witness_root_ids": ["root-1"], "dependency_ids": ["evidence:root-1"],
    }
    mutation_type = {
        "S1": "opposite_polarity_explicit_supersession",
        "S2": "independent_opposite_trusted_tool_result",
        "S3": "affected_actor_explicit_supersession",
        "C1": "unrelated_weather_visible_update",
        "C2": "evaluator_only_hidden_truth_mutation",
    }[cell.scenario_family]
    rd = {
        "current_EAdm": not relevant, "authority_epoch": epoch,
        "reasons": ["non_defeated.conflict"] if cell.scenario_family == "S2" else [],
        "mutation_type": mutation_type,
        "mutation_dependency_ids": ["evidence:root-1"] if not c2 else [],
        "intersecting_dependency_ids": ["evidence:root-1"] if relevant else [],
        "relevant_action_dependency_changed": relevant,
        "permit_or_shadow_fresh": not relevant,
    }
    execution = not relevant or cell.condition == "dual_dag_advisory"
    re = _execution_phase(
        identity, eadm=not relevant, execution=execution,
        permit_fresh=not relevant, epoch=epoch,
    )
    other_identity = _identity(item, "2")
    other_rp = {
        **other_identity, "EAdm": True, "authority_epoch": 1,
        "witness_root_ids": ["root-other"], "dependency_ids": ["evidence:root-other"],
    }
    other_rd = {
        **rd, "current_EAdm": True, "reasons": [], "intersecting_dependency_ids": [],
        "relevant_action_dependency_changed": False, "permit_or_shadow_fresh": True,
    }
    other_re = _execution_phase(
        other_identity, eadm=True, execution=True, permit_fresh=True, epoch=epoch,
    )

    def mechanisms(*, unaffected=False):
        dependency_changed = relevant and not unaffected
        epoch_changed = not c2
        allowed = True if unaffected else execution
        models = {
            "M0": {"decision": "allow", "reason": "admission_epistemically_admissible",
                   "inputs_used": ["r_p.EAdm"], "relevant_action_dependency_changed": None},
            "M1": {"decision": "allow", "reason": "exact_request_unchanged",
                   "inputs_used": [f"r_p/r_e.{field}" for field in EXACT_FIELDS],
                   "relevant_action_dependency_changed": None},
            "M2": {"decision": "reject" if epoch_changed else "allow",
                   "reason": "global_authority_revision_changed" if epoch_changed
                   else "global_authority_revision_unchanged",
                   "inputs_used": ["r_p.authority_epoch", "r_e.authority_epoch_before_execution"],
                   "relevant_action_dependency_changed": None},
            "M3": {"decision": "reject" if dependency_changed else "allow",
                   "reason": "relevant_action_dependency_changed" if dependency_changed
                   else "relevant_action_dependencies_unchanged",
                   "inputs_used": ["r_d.relevant_action_dependency_changed"],
                   "relevant_action_dependency_changed": dependency_changed},
        }
        models["M4"] = ({
            "decision": "not_applicable",
            "reason": "existing_authority_not_run_in_advisory_mode",
            "inputs_used": [], "relevant_action_dependency_changed": None,
        } if cell.condition == "dual_dag_advisory" else {
            "decision": "allow" if allowed else "reject",
            "reason": "existing_authority_allowed" if allowed else "existing_authority_stale",
            "inputs_used": ["existing_authority_gateway_outcome"],
            "relevant_action_dependency_changed": None,
        })
        return models

    evaluator_before = {"hidden_target_available": True} if c2 else None
    evaluator_after = {"hidden_target_available": False} if c2 else None
    supersession = ({
        "actor_id": cell.affected_actor, "old_root_id": "root-1", "new_root_id": "root-2",
        "old_polarity": True, "new_polarity": False,
        "same_tracked_proposition": True, "old_revision": 1,
        "new_revision": 3 if cell.scenario_family == "S3" else 2,
        "supersedes": ["root-1"], "old_root_current_after": False,
        "new_root_current": True, "visibility": [cell.affected_actor],
    } if cell.scenario_family in {"S1", "S3"} else None)
    mutation = {
        "hidden_truth_ingested": False,
        "declared_sec_pre": False,
        "fixture_synthetic_semantic_mutation": relevant,
        "mutation_type": mutation_type,
        "authority_epoch_before": 1, "authority_epoch_after": epoch,
        "evidence_total_before": 1, "evidence_total_after": 1 if c2 else 2,
        "superseded_root_id": "root-1" if supersession else None,
        "replacement_root_id": None if c2 else "root-2",
        "contradiction": ({
            "positive_current": True, "negative_current": True,
            "positive_supersedes": [], "negative_supersedes": [],
            "non_defeated": False,
        } if cell.scenario_family == "S2" else None),
        "supersession": supersession,
        "actor_current_EAdm": ({
            cell.affected_actor: False,
            ("Bob" if cell.affected_actor == "Alice" else "Alice"): True,
        } if cell.scenario_family == "S3" else {cell.affected_actor: not relevant}),
        "cross_actor_dependency_leak": False,
        "cross_actor_state_change_leak": False,
        "evaluator_truth_before": evaluator_before,
        "evaluator_truth_after": evaluator_after,
        "evaluator_truth_before_digest": (
            hashlib.sha256(canonical_bytes(evaluator_before)).hexdigest()
            if evaluator_before is not None else None),
        "evaluator_truth_after_digest": (
            hashlib.sha256(canonical_bytes(evaluator_after)).hexdigest()
            if evaluator_after is not None else None),
        "evaluator_truth_changed": c2,
        "evaluator_truth_authority_input": False,
        "evaluator_truth_precondition_input": False,
        "gateway_calls": {"env": 1, "sec": 1},
    }
    s3 = None if cell.scenario_family != "S3" else {
        "affected_actor": cell.affected_actor,
        "unaffected_actor": "Bob" if cell.affected_actor == "Alice" else "Alice",
        "unaffected_current_EAdm": True,
        "unaffected_r_p": other_rp, "unaffected_r_d": other_rd,
        "unaffected_r_e": other_re,
        "unaffected_same_prepared_object": True,
        "unaffected_exact_action_preserved": True,
        "unaffected_mechanism_analysis": mechanisms(unaffected=True),
        "cross_actor_dependency_leak": False,
        "cross_actor_state_change_leak": False,
    }
    trace = {
        "schema_version": "minecraft-k10-cell-trace/1",
        "protocol_digest": protocol["validated_protocol_digest"],
        "candidate_pool_digest": protocol["validated_candidate_pool_digest"],
        "inventory_digest": protocol["validated_inventory_digest"],
        "result_schema_digest": protocol["validated_result_schema_digest"],
        "selection_manifest_digest": SELECTION_MANIFEST_DIGEST,
        "pairing_digest": "0" * 64,
        "cell": {name: getattr(cell, name) for name in (
            "cell_id", "scenario_family", "inventory_id", "condition", "affected_actor", "matrix")},
        "semantic_bindings": protocol["semantic_bindings"],
        "selected_request_digest": item.canonical_request_digest,
        "previously_unsubmitted": {
            "attested": True, "definition": "previously effect-boundary-unsubmitted",
            "selected_request_digest": item.canonical_request_digest,
            "historical_audit_digest": protocol["validated_historical_audit_digest"],
        },
        "r_p": rp, "r_d": rd, "r_e": re,
        "actor_scope": {"actor_id": cell.affected_actor,
                        "visible_to": [cell.affected_actor], "private_actor_scope": True},
        "mutation": mutation,
        "exact_action": {"same_prepared_object": True, "exact_action_preserved": True},
        "no_reconsideration": {
            "planner_instantiated": False, "model_instantiated": False,
            "controller_instantiated": False, "planner_calls": 0, "model_calls": 0,
            "controller_redecisions": 0, "action_regenerations": 0,
        },
        "s3": s3,
        "mechanism_analysis": mechanisms(),
    }
    trace["pairing_digest"] = trace_pairing_digest(trace)
    return trace


def test_candidate_pool_reproduces_selection_and_design_digest():
    rows = load_k10_candidate_pool()
    assert len(rows) == 20
    assert Counter(row["descriptor"]["stratum"] for row in rows) == {
        "I1": 4, "I2": 4, "I3": 4, "I4": 4, "I5": 4,
    }
    assert detached_digest(json.loads(Path(
        "benchmarks/minecraft/k10_candidate_pool_v1.json").read_text())) == CANDIDATE_POOL_DIGEST
    selected = []
    for stratum in ("I1", "I2", "I3", "I4", "I5"):
        ranked = sorted((row for row in rows if row["descriptor"]["stratum"] == stratum),
                        key=lambda row: row["selection_digest"])
        selected.extend(ranked[:2])
    assert tuple((expected[1], expected[2]) for expected in EXPECTED_SELECTED) == tuple(
        (row["pool_id"], row["canonical_request_digest"]) for row in selected)
    protocol = load_k10_protocol()
    assert protocol["artifact_bindings"]["selection_manifest_digest"] == SELECTION_MANIFEST_DIGEST


def test_candidate_pool_tamper_fails_even_with_recomputed_detached_digest(tmp_path):
    source = json.loads(Path("benchmarks/minecraft/k10_candidate_pool_v1.json").read_text())
    source["candidates"][0]["descriptor"]["arguments"]["x"] = 999
    path = tmp_path / "pool.json"
    _write_detached(path, source)
    with pytest.raises(K10ContractError, match="identity|request-content|selection"):
        load_k10_candidate_pool(path)


def test_selected_inventory_is_exact_and_request_bound():
    items = load_k10_inventory()
    assert tuple((item.inventory_id, item.pool_id, item.canonical_request_digest)
                 for item in items) == EXPECTED_SELECTED
    assert [item.request() for item in items] == [
        {"x": 14, "y": 64, "z": 14},
        {"x": 11, "y": 64, "z": 11},
        {"facing": "S", "item_name": "oak_planks", "x": 33, "y": 64, "z": 33},
        {"facing": "E", "item_name": "cobblestone", "x": 32, "y": 64, "z": 32},
        {"x": 42, "y": 64, "z": 42},
        {"x": 43, "y": 64, "z": 43},
        {"target_name": "skeleton"}, {"target_name": "zombie"},
        {"item_count": 3, "item_name": "oak_planks", "target_player_name": "frank"},
        {"item_count": 2, "item_name": "cobblestone", "target_player_name": "eve"},
    ]


def test_historical_unseen_audit_is_read_only_and_selected_absent():
    result = audit_historical_submissions()
    assert result["archive_trace_count"] == 60
    assert len(result["historical_request_content_digests"]) == 5
    assert len(result["selected_request_content_digests"]) == 10
    assert result["selected_absent"] is True and result["read_only"] is True


def test_historical_unseen_audit_does_not_depend_on_current_disclosed_source(monkeypatch):
    protocol = load_k10_protocol()
    disclosed = {
        (k10_protocol_module.ROOT / relative).resolve()
        for relative in protocol["historical_unseen_audit"]["disclosed_source_bindings"]
    }
    original = Path.read_bytes
    original_text = Path.read_text
    current_k6_protocol = (
        k10_protocol_module.ROOT
        / protocol["historical_unseen_audit"]["k6_protocol_path"]
    ).resolve()

    def reject_current_disclosed_source(path):
        if path.resolve() in disclosed:
            raise AssertionError("historical audit read current disclosed source")
        return original(path)

    def reject_current_k6_protocol(path, *args, **kwargs):
        if path.resolve() == current_k6_protocol:
            raise AssertionError("historical audit read current K6 protocol")
        return original_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", reject_current_disclosed_source)
    monkeypatch.setattr(Path, "read_text", reject_current_k6_protocol)

    assert audit_historical_submissions(protocol)["selected_absent"] is True


def test_historical_audit_valid_frozen_k6_protocol_passes():
    protocol = load_k10_protocol()
    path = protocol["historical_unseen_audit"]["k6_protocol_path"]
    frozen = k10_protocol_module._load_frozen_json(SUBJECT_RUNTIME_REFERENCE, path)

    result = audit_historical_submissions(
        protocol, frozen_json_loader=lambda revision, relative: frozen,
    )

    assert result["selected_absent"] is True


def test_frozen_k6_protocol_missing_and_malformed_objects_fail_closed():
    protocol = load_k10_protocol()

    def unavailable(_revision, _relative):
        raise K10ContractError("K10 frozen source object unavailable")

    with pytest.raises(K10ContractError, match="frozen source object unavailable"):
        audit_historical_submissions(protocol, frozen_json_loader=unavailable)

    def malformed(revision, relative):
        return k10_protocol_module._load_frozen_json(
            revision, relative, source_reader=lambda *_args: b"{not-json",
        )

    with pytest.raises(K10ContractError, match="cannot load frozen K10 JSON"):
        audit_historical_submissions(protocol, frozen_json_loader=malformed)


def test_frozen_k6_protocol_duplicate_key_and_non_object_fail_closed():
    protocol = load_k10_protocol()

    def duplicate(revision, relative):
        return k10_protocol_module._load_frozen_json(
            revision, relative, source_reader=lambda *_args: b'{"a": 1, "a": 2}',
        )

    with pytest.raises(K10ContractError, match="duplicate JSON key"):
        audit_historical_submissions(protocol, frozen_json_loader=duplicate)

    def non_object(revision, relative):
        return k10_protocol_module._load_frozen_json(
            revision, relative, source_reader=lambda *_args: b"[]",
        )

    with pytest.raises(K10ContractError, match="must be an object"):
        audit_historical_submissions(protocol, frozen_json_loader=non_object)


def test_frozen_k6_protocol_invalid_utf8_fails_closed():
    protocol = load_k10_protocol()

    def invalid_utf8(revision, relative):
        return k10_protocol_module._load_frozen_json(
            revision, relative, source_reader=lambda *_args: b"\xff",
        )

    with pytest.raises(K10ContractError, match="cannot load frozen K10 JSON"):
        audit_historical_submissions(protocol, frozen_json_loader=invalid_utf8)


def test_frozen_k6_protocol_exposure_metadata_tamper_fails():
    protocol = load_k10_protocol()
    path = protocol["historical_unseen_audit"]["k6_protocol_path"]
    frozen = k10_protocol_module._load_frozen_json(SUBJECT_RUNTIME_REFERENCE, path)
    frozen["pre_run_exposure"]["representative_submission_validation"]["cell_count"] = 8

    with pytest.raises(K10ContractError, match="exposure metadata changed"):
        audit_historical_submissions(
            protocol, frozen_json_loader=lambda _revision, _relative: frozen,
        )


def test_historical_disclosed_source_blob_mismatch_and_missing_object_fail_closed():
    protocol = load_k10_protocol()

    with pytest.raises(K10ContractError, match="disclosed submission source changed"):
        audit_historical_submissions(
            protocol, source_reader=lambda _revision, _path: b"tampered",
        )

    def unavailable(_revision, _path):
        raise K10ContractError("K10 frozen source object unavailable")

    with pytest.raises(K10ContractError, match="frozen source object unavailable"):
        audit_historical_submissions(protocol, source_reader=unavailable)


def test_historical_disclosed_source_declared_hash_tamper_fails():
    protocol = copy.deepcopy(load_k10_protocol())
    declarations = protocol["historical_unseen_audit"]["disclosed_source_bindings"]
    declarations[next(iter(declarations))]["sha256"] = "0" * 64

    with pytest.raises(K10ContractError, match="protocol|disclosed submission source changed"):
        audit_historical_submissions(protocol)


def test_historical_disclosed_source_path_and_hash_substitution_fails():
    protocol = copy.deepcopy(load_k10_protocol())
    declarations = protocol["historical_unseen_audit"]["disclosed_source_bindings"]
    replaced = declarations.pop(next(iter(declarations)))
    replacement = "README.md"
    replacement_bytes = k10_protocol_module._read_frozen_source_blob(
        SUBJECT_RUNTIME_REFERENCE, replacement,
    )
    declarations[replacement] = {
        **replaced,
        "sha256": hashlib.sha256(replacement_bytes).hexdigest(),
    }

    with pytest.raises(K10ContractError, match="protocol.*digest|bindings are incomplete"):
        audit_historical_submissions(protocol)


def test_historical_disclosed_source_request_digest_substitution_fails():
    protocol = json.loads(Path("benchmarks/minecraft/k10_protocol_v1.json").read_text())
    declarations = protocol["historical_unseen_audit"]["disclosed_source_bindings"]
    declaration = next(
        value for value in declarations.values()
        if len(value["request_content_digests"]) > 1
    )
    declaration["request_content_digests"] = declaration["request_content_digests"][:1]
    protocol["detached_artifact_sha256"] = detached_digest(protocol)

    with pytest.raises(K10ContractError, match="protocol identity"):
        audit_historical_submissions(protocol)


def test_protocol_freezes_zero_exposure_protected_content_and_reporting_separation(tmp_path):
    protocol = load_k10_protocol()
    assert protocol["zero_pre_exposure"] == {
        "engineering_validation_executed": True,
        "holdout_construction_executed": True,
        "holdout_effect_boundary_submissions": 0,
        "holdout_advisory_submissions": 0,
        "holdout_authority_submissions": 0,
        "holdout_full_census_executed": False,
        "holdout_aggregate_generated": False,
        "representative_pilot": False,
    }
    assert len(protocol["protected_runtime_content_bindings"]) == 19
    assert protocol["reporting_separation"]["pooling_forbidden"] is True
    changed = json.loads(Path("benchmarks/minecraft/k10_protocol_v1.json").read_text())
    changed["protected_runtime_content_bindings"]["benchmarks/minecraft/eac_runtime.py"] = "0" * 64
    path = tmp_path / "protocol.json"
    _write_detached(path, changed)
    with pytest.raises(K10ContractError, match="protocol identity|protected runtime content"):
        load_k10_protocol(path)


def test_historical_validation_passes_while_modified_live_checkout_fails():
    protocol = load_k10_protocol()

    _validate_frozen_protected_content(protocol)
    with pytest.raises(K10ContractError, match="protected runtime content mismatch"):
        validate_live_k10_checkout(protocol)


def test_frozen_source_blob_mismatch_and_unavailable_object_fail_closed():
    protocol = load_k10_protocol()

    with pytest.raises(K10ContractError, match="frozen protected source mismatch"):
        _validate_frozen_protected_content(
            protocol, source_reader=lambda _revision, _path: b"tampered",
        )

    def unavailable(_revision, _path):
        raise K10ContractError("K10 frozen source object unavailable")

    with pytest.raises(K10ContractError, match="frozen source object unavailable"):
        _validate_frozen_protected_content(protocol, source_reader=unavailable)


def test_frozen_source_revision_and_protected_path_tamper_fail_before_object_read():
    protocol = copy.deepcopy(load_k10_protocol())
    protocol["subject_runtime_semantic_reference"] = "0" * 40
    with pytest.raises(K10ContractError, match="revision identity mismatch"):
        _validate_frozen_protected_content(protocol, source_reader=lambda *_args: b"")

    protocol["subject_runtime_semantic_reference"] = SUBJECT_RUNTIME_REFERENCE
    bindings = protocol["protected_runtime_content_bindings"]
    expected = bindings.pop(next(iter(bindings)))
    bindings["../outside"] = expected
    called = False

    def reader(*_args):
        nonlocal called
        called = True
        return b""

    with pytest.raises(K10ContractError, match="path is invalid"):
        _validate_frozen_protected_content(protocol, source_reader=reader)
    assert called is False


def test_protocol_loader_returns_detached_copy_of_cached_historical_validation():
    first = load_k10_protocol()
    original = first["protected_runtime_content_bindings"]["env/minecraft_client.py"]
    first["protected_runtime_content_bindings"]["env/minecraft_client.py"] = "0" * 64

    second = load_k10_protocol()

    assert second["protected_runtime_content_bindings"]["env/minecraft_client.py"] == original


def test_historical_loader_does_not_call_live_k6_loaders(monkeypatch):
    k10_protocol_module._load_k10_protocol_cached.cache_clear()
    k10_protocol_module._load_k10_candidate_pool_cached.cache_clear()
    k10_protocol_module.load_k10_inventory.cache_clear()
    monkeypatch.setattr(
        k10_protocol_module.k6, "load_k6_protocol",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("live K6 protocol read")),
    )
    monkeypatch.setattr(
        k10_protocol_module.k6, "load_k6_inventory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("live K6 inventory read")),
    )

    assert load_k10_protocol()["subject_runtime_semantic_reference"] == SUBJECT_RUNTIME_REFERENCE


def test_candidate_pool_loader_returns_detached_nested_data():
    first = load_k10_candidate_pool()
    original = first[0]["descriptor"]["arguments"].copy()
    first[0]["descriptor"]["arguments"]["x"] = 999

    second = load_k10_candidate_pool()

    assert second[0]["descriptor"]["arguments"] == original


def test_frozen_source_git_reader_disables_lazy_fetch(monkeypatch):
    relative = "env/minecraft_client.py"
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if "ls-tree" in command:
            stdout = b"100644 blob " + b"a" * 40 + b"\t" + relative.encode() + b"\0"
        else:
            stdout = b"frozen bytes"
        return type("Result", (), {"returncode": 0, "stdout": stdout})()

    monkeypatch.setattr(k10_protocol_module.subprocess, "run", run)

    assert k10_protocol_module._read_frozen_source_blob(
        SUBJECT_RUNTIME_REFERENCE, relative,
    ) == b"frozen bytes"
    assert len(calls) == 2
    assert all(call[1]["env"]["GIT_NO_LAZY_FETCH"] == "1" for call in calls)
    assert all(call[1]["env"]["GIT_TERMINAL_PROMPT"] == "0" for call in calls)


def test_census_has_exact_cells_families_and_sixty_pairs():
    cells = build_k10_cells()
    assert len(cells) == len({cell.cell_id for cell in cells}) == 120
    assert Counter(cell.scenario_family for cell in cells) == {
        "S1": 20, "S2": 20, "S3": 40, "C1": 20, "C2": 20,
    }
    assert Counter(cell.matrix for cell in cells) == {"primary": 80, "control": 40}
    pairs = {}
    for cell in cells:
        pairs.setdefault((cell.scenario_family, cell.inventory_id,
                          cell.affected_actor, cell.matrix), []).append(cell)
    assert len(pairs) == 60
    assert all(len(pair) == 2 and {cell.condition for cell in pair} == set(CONDITIONS)
               for pair in pairs.values())


def test_trace_validator_accepts_k10_and_rejects_k6_cross_inventory_and_drift():
    cell = build_k10_cells()[0]
    trace = synthetic_trace(cell)
    assert validate_k10_trace(trace, cell=cell) == trace
    changed = copy.deepcopy(trace)
    changed["schema_version"] = "minecraft-k6-cell-trace/1"
    with pytest.raises(K10ContractError, match="top-level"):
        validate_k10_trace(changed)
    changed = copy.deepcopy(trace)
    other = load_k10_inventory()[2]
    changed["selected_request_digest"] = other.canonical_request_digest
    with pytest.raises(K10ContractError, match="selected request"):
        validate_k10_trace(changed)
    changed = copy.deepcopy(trace)
    changed["no_reconsideration"]["model_calls"] = 1
    with pytest.raises(K10ContractError, match="no-reconsideration"):
        validate_k10_trace(changed)
    changed = copy.deepcopy(trace)
    changed["r_e"]["attempt_id"] = "reconstructed"
    with pytest.raises(K10ContractError, match="identity changed"):
        validate_k10_trace(changed)


def test_complete_synthetic_aggregate_preserves_estimands_without_pooling():
    traces = [synthetic_trace(cell) for cell in build_k10_cells()]
    result = aggregate_k10_results(traces)
    assert result["complete"] is True
    assert result["observed_primary_cells"] == 80
    assert result["observed_control_cells"] == 40
    assert result["observed_pair_count"] == 60
    assert result["iid_samples"] is False and result["k8_k10_pooled"] is False
    assert result["confidence_intervals_added"] is False and result["p_values_added"] is False
    assert result["overall"]["post_admission_invalid_action_execution_rate"] == {
        "numerator": 40, "denominator": 80,
    }
    assert result["overall"]["relevant_revision_detection"] == {
        "numerator": 80, "denominator": 80,
    }
    assert result["overall"]["unrelated_retention"] == {
        "numerator": 20, "denominator": 20,
    }
    assert result["overall"]["actor_scope_isolation"] == {
        "numerator": 40, "denominator": 40,
    }
    assert result["overall"]["cross_actor_dependency_leakage"] == {
        "numerator": 0, "denominator": 40,
    }


def test_incomplete_aggregate_is_diagnostic_and_duplicates_fail():
    trace = synthetic_trace(build_k10_cells()[0])
    result = aggregate_k10_results([trace])
    assert result["complete"] is False and result["verdict"] is None
    with pytest.raises(K10ContractError, match="duplicate"):
        aggregate_k10_results([trace, copy.deepcopy(trace)])


def test_pair_digest_tamper_fails_closed():
    cells = build_k10_cells()
    advisory = synthetic_trace(cells[0])
    authority_cell = next(cell for cell in cells if (
        cell.scenario_family, cell.inventory_id, cell.affected_actor, cell.matrix, cell.condition
    ) == (
        cells[0].scenario_family, cells[0].inventory_id, cells[0].affected_actor,
        cells[0].matrix, "dual_dag_authority",
    ))
    authority = synthetic_trace(authority_cell)
    authority["r_d"]["reasons"] = ["tampered"]
    authority["pairing_digest"] = trace_pairing_digest(authority)
    with pytest.raises(K10ContractError, match="pre-enforcement"):
        aggregate_k10_results([advisory, authority])


def test_c2_rejects_non_integer_epoch_and_evidence_state():
    cell = next(cell for cell in build_k10_cells()
                if cell.scenario_family == "C2"
                and cell.condition == "dual_dag_authority")
    trace = synthetic_trace(cell)
    for location, field in (
        (trace["mutation"], "authority_epoch_before"),
        (trace["mutation"], "authority_epoch_after"),
        (trace["r_p"], "authority_epoch"),
        (trace["r_d"], "authority_epoch"),
        (trace["r_e"], "authority_epoch_before_execution"),
    ):
        location[field] = "one"
    trace["pairing_digest"] = trace_pairing_digest(trace)
    with pytest.raises(K10ContractError, match="C2 hidden-truth"):
        validate_k10_trace(trace)

    trace = synthetic_trace(cell)
    trace["mutation"]["evidence_total_before"] = True
    trace["mutation"]["evidence_total_after"] = True
    trace["pairing_digest"] = trace_pairing_digest(trace)
    with pytest.raises(K10ContractError, match="C2 hidden-truth"):
        validate_k10_trace(trace)
