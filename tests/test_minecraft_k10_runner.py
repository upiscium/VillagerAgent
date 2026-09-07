import copy
import hashlib
import json
from pathlib import Path

import pytest

from benchmarks.minecraft import k10_protocol, k10_runner

ROOT = Path(__file__).resolve().parents[1]
REVISION = "a" * 40


def _git_responses(monkeypatch, *, dirty="", head=REVISION):
    def fake_git(root, *args):
        if args == ("rev-parse", "--show-toplevel"):
            return str(Path(root).resolve()) + "\n"
        if args == ("worktree", "list", "--porcelain"):
            return f"worktree {Path(root).resolve()}\n"
        if args == ("rev-parse", "HEAD"):
            return head + "\n"
        if args == ("status", "--porcelain", "--untracked-files=all"):
            return dirty
        raise AssertionError(f"unexpected git request: {args}")
    monkeypatch.setattr(k10_runner, "_git", fake_git)


class _ProtocolFacade:
    CONDITIONS = k10_protocol.CONDITIONS
    SELECTION_MANIFEST_DIGEST = k10_protocol.SELECTION_MANIFEST_DIGEST

    def __init__(self, *, binding_override=None, aggregate_complete=True):
        self._cells = k10_protocol.build_k10_cells()
        self._protocol = copy.deepcopy(k10_protocol.load_k10_protocol())
        self._aggregate_complete = aggregate_complete
        self.live_validation_calls = 0
        if binding_override is not None:
            self._protocol["validated_protocol_digest"] = binding_override

    def load_k10_protocol(self):
        return self._protocol

    def build_k10_cells(self):
        return self._cells

    def validate_live_k10_checkout(self, protocol, *, root):
        assert protocol is self._protocol
        assert Path(root).resolve() == ROOT.resolve()
        self.live_validation_calls += 1

    def validate_k10_trace(self, trace, *, cell):
        expected = {name: getattr(cell, name) for name in (
            "cell_id", "scenario_family", "inventory_id", "condition",
            "affected_actor", "matrix",
        )}
        if trace["cell"] != expected:
            raise ValueError("synthetic cell identity mismatch")
        return trace

    def aggregate_k10_results(self, traces):
        return {
            "complete": self._aggregate_complete and len(traces) == 120,
            "observed_primary_cells": sum(t["cell"]["matrix"] == "primary" for t in traces),
            "observed_control_cells": sum(t["cell"]["matrix"] == "control" for t in traces),
            "observed_pair_count": 60,
            "synthetic": True,
        }


def _trace(cell):
    identity = {name: getattr(cell, name) for name in (
        "cell_id", "scenario_family", "inventory_id", "condition",
        "affected_actor", "matrix",
    )}
    pair_key = "|".join(str(identity[name]) for name in (
        "scenario_family", "inventory_id", "affected_actor", "matrix"))
    return {
        "schema_version": "synthetic-k10-trace/1",
        "cell": identity,
        "pairing_digest": hashlib.sha256(pair_key.encode()).hexdigest(),
        "synthetic_trace": True,
    }


class _Fixture:
    def __init__(self, calls, fail_at=None):
        self.calls = calls
        self.fail_at = fail_at

    def construct_k10_trial(self, cell):
        self.calls["construct"] += 1
        if self.calls["construct"] == self.fail_at:
            raise RuntimeError("synthetic construction failure")
        return _Trial(self.calls, cell)


class _Trial:
    def __init__(self, calls, cell):
        self.calls = calls
        self.cell = cell

    def submit(self):
        self.calls["fake_submit"] += 1
        return _trace(self.cell)


def _run(monkeypatch, tmp_path, *, run_id="census", fixture=None, protocol=None, fault_hook=None):
    _git_responses(monkeypatch)
    return k10_runner._run_with_dependencies(
        ROOT, run_id=run_id, expected_execution_revision=REVISION, output_dir=tmp_path,
        fixture_module=fixture or _Fixture({"construct": 0, "fake_submit": 0}),
        protocol_module=protocol or _ProtocolFacade(), fault_hook=fault_hook,
    )


def _final(tmp_path, run_id="census"):
    return tmp_path / run_id / "final"


def _assert_no_final(tmp_path, run_id="census"):
    final = _final(tmp_path, run_id)
    assert not (final / "final_manifest.json").exists()
    assert not (final / "aggregate.json").exists()
    assert not (tmp_path / run_id / "aggregate.json").exists()


def _checks_from_final(final):
    manifest = json.loads((final / "final_manifest.json").read_text())
    return {
        "execution_revision": manifest["execution_revision"],
        "runner_id": manifest["runner"]["identity"],
        "runner_version": manifest["runner"]["version"],
        "runner_contract_digest": manifest["runner"]["contract_digest"],
        "implementation_sha256": manifest["runner"]["implementation_sha256"],
        "protocol_digest": manifest["protocol_digest"],
        "candidate_pool_digest": manifest["candidate_pool_digest"],
        "inventory_digest": manifest["inventory_digest"],
        "result_schema_digest": manifest["result_schema_digest"],
        "selection_manifest_digest": manifest["selection_manifest_digest"],
        "historical_audit_digest": manifest["historical_audit_digest"],
        "cell_ids": list(manifest["canonical_cell_ids"]),
    }


def test_runner_contract_is_materialized_and_protocol_bound():
    contract, digest = k10_runner.load_k10_contract()
    assert contract["detached_artifact_sha256"] == digest
    assert contract["implementation_sha256"] == hashlib.sha256(
        k10_runner.__file__ and Path(k10_runner.__file__).read_bytes()).hexdigest()
    protocol = k10_protocol.load_k10_protocol()
    assert contract["protocol_binding"] == {
        "protocol_digest": protocol["validated_protocol_digest"],
        "candidate_pool_digest": protocol["validated_candidate_pool_digest"],
        "inventory_digest": protocol["validated_inventory_digest"],
        "result_schema_digest": protocol["validated_result_schema_digest"],
        "selection_manifest_digest": k10_protocol.SELECTION_MANIFEST_DIGEST,
        "historical_audit_digest": protocol["validated_historical_audit_digest"],
    }


def test_runner_preflight_invokes_explicit_live_checkout_admission(monkeypatch, tmp_path):
    facade = _ProtocolFacade()

    _run(monkeypatch, tmp_path, protocol=facade)

    assert facade.live_validation_calls == 1


def test_public_runner_preflights_before_fixture_import(monkeypatch, tmp_path):
    observed = []

    def reject(*_args, **_kwargs):
        observed.append("preflight")
        raise k10_runner.K10RunnerError("synthetic admission rejection")

    monkeypatch.setattr(k10_runner, "_preflight_with_protocol", reject)

    with pytest.raises(k10_runner.K10RunnerError, match="admission rejection"):
        k10_runner.run(
            ROOT, run_id="blocked", expected_execution_revision=REVISION,
            output_dir=tmp_path,
        )
    assert observed == ["preflight"]


def test_initial_manifest_has_120_canonical_cells_and_real_submission_count_stays_zero(monkeypatch, tmp_path):
    from benchmarks.minecraft.k10_fixture import real_submission_count
    before = real_submission_count()
    calls = {"construct": 0, "fake_submit": 0}
    captured = []
    original = k10_runner._durable_json
    def capture(path, value, replace_existing=False):
        if Path(path).name == "run_manifest.json" and not replace_existing:
            captured.append(copy.deepcopy(value))
        return original(path, value, replace_existing)
    monkeypatch.setattr(k10_runner, "_durable_json", capture)
    _run(monkeypatch, tmp_path, fixture=_Fixture(calls))
    manifest = captured[0]
    assert [entry["ordinal"] for entry in manifest["cells"]] == list(range(1, 121))
    assert [entry["status"] for entry in manifest["cells"]] == ["not_started"] * 120
    assert manifest["schema_version"] == "minecraft-k10-run/1"
    assert calls == {"construct": 120, "fake_submit": 120}
    assert real_submission_count() == before == 0


def test_revision_dirty_tree_and_existing_run_fail_before_fake_submission(monkeypatch, tmp_path):
    for index, (dirty, head) in enumerate((("", "b" * 40), ("?? stray\n", REVISION))):
        calls = {"construct": 0, "fake_submit": 0}
        _git_responses(monkeypatch, dirty=dirty, head=head)
        with pytest.raises(k10_runner.K10RunnerError):
            k10_runner._run_with_dependencies(
                ROOT, run_id=f"gate-{index}", expected_execution_revision=REVISION,
                output_dir=tmp_path, fixture_module=_Fixture(calls),
                protocol_module=_ProtocolFacade(),
            )
        assert calls == {"construct": 0, "fake_submit": 0}
    (tmp_path / "already").mkdir()
    with pytest.raises(k10_runner.K10RunnerError, match="must not already exist"):
        _run(monkeypatch, tmp_path, run_id="already")


def test_cell_failure_records_prefix_failed_cell_and_suffix_without_final(monkeypatch, tmp_path):
    calls = {"construct": 0, "fake_submit": 0}
    with pytest.raises(k10_runner.K10RunnerError, match="K10 cell"):
        _run(monkeypatch, tmp_path, fixture=_Fixture(calls, fail_at=4))
    manifest = json.loads((tmp_path / "census" / "run_manifest.json").read_text())
    assert [entry["status"] for entry in manifest["cells"][:6]] == (
        ["completed"] * 3 + ["failed", "not_started", "not_started"])
    assert manifest["run_status"] == "failed"
    _assert_no_final(tmp_path)
    assert calls == {"construct": 4, "fake_submit": 3}


@pytest.mark.parametrize("mutation", ["missing", "identity", "pair"])
def test_disk_reload_count_identity_and_pair_gates_fail_closed(monkeypatch, tmp_path, mutation):
    original = k10_runner._durable_json
    written = 0
    def corrupt(path, value, replace_existing=False):
        nonlocal written
        path = Path(path)
        if path.parent.name == "cells" and not replace_existing:
            written += 1
            if mutation == "missing" and written == 120:
                return None
            if mutation == "identity" and written == 2:
                value = copy.deepcopy(value); value["cell"]["cell_id"] = "duplicate-cell"
            if mutation == "pair" and written == 2:
                value = copy.deepcopy(value); value["pairing_digest"] = "f" * 64
        return original(path, value, replace_existing)
    monkeypatch.setattr(k10_runner, "_durable_json", corrupt)
    with pytest.raises((k10_runner.K10RunnerError, ValueError),
                       match="completeness|identity|pair|cell"):
        _run(monkeypatch, tmp_path)
    _assert_no_final(tmp_path)


def test_complete_fake_census_publishes_final_once(monkeypatch, tmp_path):
    publications = []
    original = k10_runner._rename_directory_no_replace
    def observe(source, destination):
        publications.append((Path(source), Path(destination)))
        return original(source, destination)
    monkeypatch.setattr(k10_runner, "_rename_directory_no_replace", observe)
    result = _run(monkeypatch, tmp_path)
    final = _final(tmp_path)
    final_manifest = json.loads((final / "final_manifest.json").read_text())
    raw = (final / "aggregate.json").read_bytes()
    assert result["schema_version"] == "minecraft-k10-run-aggregate/1"
    assert result["raw_trace_count"] == 120 and result["pair_count"] == 60
    assert final_manifest["aggregate_sha256"] == hashlib.sha256(raw).hexdigest()
    assert final_manifest["counts"] == {"total_cells": 120, "primary_cells": 80, "control_cells": 40}
    assert len(publications) == 1
    assert not (tmp_path / "census" / ".final.tmp").exists()
    assert not (tmp_path / "census" / "aggregate.json").exists()
    marker = json.loads((tmp_path / k10_runner.EXPOSURE_MARKER).read_text())
    assert marker["run_id"] == "census"
    assert marker["effect_boundary_submissions_before_marker"] == 0
    assert k10_runner._authoritative_final_valid(final, "census", _checks_from_final(final))


def test_global_exposure_marker_blocks_a_second_attempt_in_authorized_root(monkeypatch, tmp_path):
    _run(monkeypatch, tmp_path, run_id="first")
    calls = {"construct": 0, "fake_submit": 0}
    with pytest.raises(k10_runner.K10RunnerError, match="already started"):
        _run(monkeypatch, tmp_path, run_id="second", fixture=_Fixture(calls))
    assert calls == {"construct": 0, "fake_submit": 0}


def test_incomplete_aggregate_never_publishes(monkeypatch, tmp_path):
    with pytest.raises(k10_runner.K10RunnerError, match="aggregate completeness"):
        _run(monkeypatch, tmp_path, protocol=_ProtocolFacade(aggregate_complete=False))
    _assert_no_final(tmp_path)


@pytest.mark.parametrize("stage", [
    "after_staging_mkdir", "after_aggregate_staged",
    "after_final_manifest_staged", "before_atomic_publication",
])
def test_prepublication_fault_windows_have_no_authoritative_final(monkeypatch, tmp_path, stage):
    def fault(observed):
        if observed == stage:
            raise RuntimeError(f"synthetic {stage} failure")
    with pytest.raises(RuntimeError, match=stage):
        _run(monkeypatch, tmp_path, fault_hook=fault)
    _assert_no_final(tmp_path)
    progress = json.loads((tmp_path / "census" / "run_manifest.json").read_text())
    assert progress["run_status"] == "failed"


def test_atomic_publication_race_preserves_existing_final(monkeypatch, tmp_path):
    original = k10_runner._rename_directory_no_replace
    def race(source, destination):
        destination.mkdir(); (destination / "sentinel").write_text("preserve")
        return original(source, destination)
    monkeypatch.setattr(k10_runner, "_rename_directory_no_replace", race)
    with pytest.raises(k10_runner.K10RunnerError, match="already exists"):
        _run(monkeypatch, tmp_path)
    assert (_final(tmp_path) / "sentinel").read_text() == "preserve"
    _assert_no_final(tmp_path)


def test_interrupt_after_publication_does_not_downgrade_authority(monkeypatch, tmp_path):
    def fault(stage):
        if stage == "after_atomic_publication":
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        _run(monkeypatch, tmp_path, fault_hook=fault)
    final = _final(tmp_path)
    assert (final / "final_manifest.json").exists()
    assert (final / "aggregate.json").exists()
    progress = json.loads((tmp_path / "census" / "run_manifest.json").read_text())
    assert progress["run_status"] != "failed"


def test_parent_fsync_failure_is_indeterminate_without_downgrade(monkeypatch, tmp_path):
    original = k10_runner._fsync_directory
    def fail(path):
        path = Path(path)
        if path.name == "census" and (path / "final").exists():
            raise OSError("synthetic parent fsync failure")
        return original(path)
    monkeypatch.setattr(k10_runner, "_fsync_directory", fail)
    with pytest.raises(k10_runner.K10FinalizationDurabilityError, match="parent fsync failed"):
        _run(monkeypatch, tmp_path)
    assert (_final(tmp_path) / "final_manifest.json").exists()
    progress = json.loads((tmp_path / "census" / "run_manifest.json").read_text())
    assert progress["run_status"] != "failed"


def test_postcommit_progress_failure_keeps_authoritative_final(monkeypatch, tmp_path):
    original = k10_runner._durable_json
    def fail(path, value, replace_existing=False):
        if Path(path).name == "run_manifest.json" and value.get("completed") is True:
            raise OSError("synthetic refresh failure")
        return original(path, value, replace_existing)
    monkeypatch.setattr(k10_runner, "_durable_json", fail)
    result = _run(monkeypatch, tmp_path)
    assert result["aggregate"]["synthetic"] is True
    assert (_final(tmp_path) / "final_manifest.json").exists()


@pytest.mark.parametrize("mutation", [
    "manifest", "hash", "binding", "cell_ids", "aggregate_schema", "aggregate_incomplete",
])
def test_authoritative_predicate_rejects_tampered_bundle(monkeypatch, tmp_path, mutation):
    _run(monkeypatch, tmp_path)
    final = _final(tmp_path)
    checks = _checks_from_final(final)
    manifest_path, aggregate_path = final / "final_manifest.json", final / "aggregate.json"
    manifest = json.loads(manifest_path.read_text())
    if mutation == "manifest":
        manifest_path.write_text("{")
    elif mutation == "hash":
        aggregate_path.write_text("{}\n")
    elif mutation == "binding":
        manifest["candidate_pool_digest"] = "0" * 64; manifest_path.write_text(json.dumps(manifest))
    elif mutation == "cell_ids":
        manifest["canonical_cell_ids"][0] = "forged"; manifest_path.write_text(json.dumps(manifest))
    else:
        aggregate = json.loads(aggregate_path.read_text())
        if mutation == "aggregate_schema": aggregate["schema_version"] = "forged/1"
        else: aggregate["aggregate"]["complete"] = False
        payload = (json.dumps(aggregate, sort_keys=True) + "\n").encode()
        aggregate_path.write_bytes(payload)
        manifest["aggregate_sha256"] = hashlib.sha256(payload).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
    assert k10_runner._authoritative_final_valid(final, "census", checks) is False


def test_binding_and_contract_drift_fail_preflight_before_fixture(monkeypatch, tmp_path):
    calls = {"construct": 0, "fake_submit": 0}
    with pytest.raises(k10_runner.K10RunnerError, match="binding mismatch"):
        _run(monkeypatch, tmp_path, fixture=_Fixture(calls),
             protocol=_ProtocolFacade(binding_override="d" * 64))
    assert calls == {"construct": 0, "fake_submit": 0}
    document = json.loads(k10_runner.CONTRACT_PATH.read_text())
    document["canonical_order_source"] = "forged.order"
    path = tmp_path / "contract.json"; path.write_text(json.dumps(document))
    original = k10_runner.load_k10_contract
    monkeypatch.setattr(k10_runner, "load_k10_contract", lambda: original(path))
    _git_responses(monkeypatch)
    with pytest.raises(k10_runner.K10RunnerError, match="digest mismatch"):
        k10_runner.preflight(ROOT, expected_execution_revision=REVISION)
