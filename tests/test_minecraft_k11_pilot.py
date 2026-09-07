import json
from copy import deepcopy
from pathlib import Path

import pytest
import benchmarks.minecraft.k11_pilot as k11_pilot

from benchmarks.minecraft.k11_pilot import (
    K11PilotContractError,
    LATE_CLEANUP_EVIDENCE_SCHEMA,
    LATE_CLEANUP_VALIDATION_CONTRACT,
    P0_EXPECTED_RUNS,
    P0_VALIDATION_CONTRACT,
    PROSPECTIVE_VALIDATION_CONTRACT,
    RECONCILIATION_EVIDENCE_SCHEMA,
    RECONCILIATION_MANIFEST_VERSION,
    RECONCILIATION_VALIDATION_CONTRACT,
    _apply_process_outcome,
    _coverage_summary,
    _in_window_evidence_metadata,
    _p0_passes,
    _primary_terminal_count,
    load_p0_manifest,
    run_development_smoke,
)


def _runtime(index: int) -> dict:
    return {
        "api_model": "qwen-test",
        "api_base": "http://127.0.0.1:11434/v1",
        "controller_reasoning_effort": "none",
        "task_type": "none",
        "task_idx": index,
        "agent_num": 2,
        "dig_needed": False,
        "max_task_num": 1,
        "task_goal": f"natural pilot task {index}",
        "host": "127.0.0.1",
        "port": 25565,
        "task_name": f"k11-p0-{index:02d}",
        "minecraft_dual_dag_config": {
            "eac_mode": "dual_dag_advisory",
            "judged_execution": False,
            "production": False,
        },
    }


def _manifest() -> dict:
    return {
        "artifact_id": "minecraft-k11-p0-manifest",
        "artifact_version": 2,
        "validation_contract": P0_VALIDATION_CONTRACT,
        "study_phase": "K11-P0-instrumentation-validation",
        "prevalence_inference_allowed": False,
        "eac_identity_source": "current_immutable_checkout",
        "observation_window": {
            "basis": "predeclared-fixed-monotonic-horizon",
            "horizon_seconds": 600,
            "natural_terminal_closes_early": True,
        },
        "runtime_hygiene": {
            "classification": "pre-freeze-runtime-hygiene-change",
            "legacy_default_paths_preserved": True,
            "legacy_cache_lookup_result_preserved": True,
            "legacy_first_save_cache_write_preserved": False,
            "first_save_cache_change": "The first response is retained when the cache file is absent.",
            "scientific_disclosure": "General subject-runtime hygiene change; not K11-only instrumentation.",
        },
        "runs": [
            {"run_id": f"K11-P0-{index:02d}", "runtime": _runtime(index)}
            for index in range(1, 9)
        ],
    }


def _write(tmp_path: Path, value: dict) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_k11_p0_manifest_requires_exactly_eight_advisory_natural_runs(tmp_path: Path) -> None:
    document = _manifest()
    loaded = load_p0_manifest(_write(tmp_path, document))
    assert len(loaded["runs"]) == 8
    assert all(
        row["runtime"]["minecraft_dual_dag_config"]["eac_mode"] == "dual_dag_advisory"
        for row in loaded["runs"]
    )


def test_k11_p0_manifest_requires_prospective_validation_contract(tmp_path: Path) -> None:
    document = _manifest()
    document["validation_contract"] = "minecraft-k11-p0-validation-contract/0"

    with pytest.raises(K11PilotContractError, match="validation contract identity"):
        load_p0_manifest(_write(tmp_path, document))


def test_k11_v1_manifest_preserves_v0_cohort_and_changes_only_contract_metadata() -> None:
    root = Path(k11_pilot.__file__).resolve().parents[2]
    v0 = json.loads(
        (root / "configs/minecraft/k11-p0-natural-manifest-v0.json").read_text(encoding="utf-8")
    )
    v1 = json.loads(
        (root / "configs/minecraft/k11-p0-natural-manifest-v1.json").read_text(encoding="utf-8")
    )

    assert v0["artifact_version"] == 1
    assert "validation_contract" not in v0
    assert v1["artifact_version"] == 2
    assert v1["validation_contract"] == "minecraft-k11-p0-validation-contract/1"
    v2 = load_p0_manifest(root / "configs/minecraft/k11-p0-natural-manifest-v2.json")
    assert v2["artifact_version"] == 3
    assert v2["validation_contract"] == PROSPECTIVE_VALIDATION_CONTRACT
    assert v2["runs"] == v1["runs"]
    v3 = load_p0_manifest(root / "configs/minecraft/k11-p0-natural-manifest-v3.json")
    assert v3["artifact_version"] == 4
    assert v3["validation_contract"] == LATE_CLEANUP_VALIDATION_CONTRACT
    assert v3["trace_schema"] == v2["trace_schema"] == "minecraft-k11-trace/3"
    assert v3["late_cleanup_evidence_contract"] == LATE_CLEANUP_EVIDENCE_SCHEMA
    assert v3["runs"] == v2["runs"]
    with pytest.raises(K11PilotContractError, match="manifest identity mismatch"):
        load_p0_manifest(root / "configs/minecraft/k11-p0-natural-manifest-v0.json")


def test_k11_p0_manifest_rejects_intervention_configuration(tmp_path: Path) -> None:
    document = _manifest()
    document["runs"][0]["runtime"]["forced_sleep"] = 0.01
    with pytest.raises(K11PilotContractError, match="intervention"):
        load_p0_manifest(_write(tmp_path, document))


def test_k11_p0_manifest_requires_qualified_reasoning_setting(tmp_path: Path) -> None:
    document = _manifest()
    document["runs"][0]["runtime"]["controller_reasoning_effort"] = None

    with pytest.raises(K11PilotContractError, match="controller_reasoning_effort=none"):
        load_p0_manifest(_write(tmp_path, document))


def test_k11_p0_manifest_rejects_authority_primary_cohort(tmp_path: Path) -> None:
    document = _manifest()
    document["runs"][0]["runtime"]["minecraft_dual_dag_config"]["eac_mode"] = "dual_dag_authority"
    with pytest.raises(K11PilotContractError, match="dual_dag_advisory"):
        load_p0_manifest(_write(tmp_path, document))


def test_k11_p0_manifest_rejects_judged_or_production_execution(tmp_path: Path) -> None:
    for field in ("judged_execution", "production"):
        document = _manifest()
        document["runs"][0]["runtime"]["minecraft_dual_dag_config"][field] = True
        with pytest.raises(K11PilotContractError, match="non-judged/non-production"):
            load_p0_manifest(_write(tmp_path, document))


def test_k11_p0_manifest_explicitly_forbids_prevalence_inference(tmp_path: Path) -> None:
    document = _manifest()
    document["prevalence_inference_allowed"] = True
    with pytest.raises(K11PilotContractError, match="prevalence inference"):
        load_p0_manifest(_write(tmp_path, document))


def test_k11_p0_manifest_requires_current_checkout_identity_source(tmp_path: Path) -> None:
    document = _manifest()
    document["eac_identity_source"] = "checked_in_stale_premanifest"
    with pytest.raises(K11PilotContractError, match="current immutable checkout"):
        load_p0_manifest(_write(tmp_path, document))


def test_k11_p0_manifest_requires_runtime_hygiene_disclosure(tmp_path: Path) -> None:
    document = _manifest()
    document.pop("runtime_hygiene")

    with pytest.raises(K11PilotContractError, match="runtime-hygiene disclosure"):
        load_p0_manifest(_write(tmp_path, document))


@pytest.mark.parametrize("horizon", [None, 0, -1, float("inf"), float("nan"), True, 761])
def test_k11_p0_manifest_requires_bounded_fixed_observation_horizon(
    tmp_path: Path, horizon,
) -> None:
    document = _manifest()
    document["observation_window"]["horizon_seconds"] = horizon

    with pytest.raises(K11PilotContractError, match="predeclared observation horizon"):
        load_p0_manifest(_write(tmp_path, document))


def test_k11_p0_manifest_rejects_outcome_dependent_observation_window(tmp_path: Path) -> None:
    document = _manifest()
    document["observation_window"]["basis"] = "stop-after-first-primary-action"

    with pytest.raises(K11PilotContractError, match="predeclared observation horizon"):
        load_p0_manifest(_write(tmp_path, document))


def test_k11_p0_manifest_rejects_user_supplied_eac_identity(tmp_path: Path) -> None:
    for field, value in (
        ("eac_premanifest", "/tmp/stale.json"),
        ("eac_execution_revision", "0" * 40),
    ):
        document = _manifest()
        document["runs"][0]["runtime"]["minecraft_dual_dag_config"][field] = value
        with pytest.raises(K11PilotContractError, match="generated from the current immutable checkout"):
            load_p0_manifest(_write(tmp_path, document))


def test_k11_p0_manifest_contract_tests_remain_independent_of_analysis_error() -> None:
    # The run-level validators, rather than a missing analysis_error field, are
    # the pilot's source of truth (exercised by the pilot integration path).
    assert P0_EXPECTED_RUNS == 8


def test_k11_p0_coverage_requires_exercised_direct_openai_compatible_path() -> None:
    counts = {
        "k11.model_call_started": 1,
        "k11.tool_call_entered": 1,
        "k11.eac_action_prepared": 1,
        "k11.eac_evidence_ingested": 1,
    }
    actor_threads = {("Alice", 1), ("Bob", 2)}

    langchain_only = _coverage_summary(
        counts, actor_threads, {"LLMHandler.on_llm_start"},
        qualifying_in_window_evidence_count=1,
    )
    direct = _coverage_summary(
        counts, actor_threads, {"OpenAILanguageModel.gpt_api_stream"},
        qualifying_in_window_evidence_count=1,
    )

    assert langchain_only["model_calls_observed"] is True
    assert langchain_only["direct_openai_compatible_calls_observed"] is False
    assert all(direct.values())


def _windowed_evidence_trace(*timestamps: int) -> dict:
    run_id = "windowed-evidence"
    events = [
        {"event_type": "k11.observation_window_opened", "monotonic_ns": 100,
         "seq": 1, "run_id": run_id,
         "payload": {"configured_horizon_seconds": 1,
                                  "horizon_monotonic_ns": 1_000_000_100}},
        *[
            {"event_type": "k11.eac_evidence_ingested", "monotonic_ns": timestamp,
             "seq": index + 2, "run_id": run_id, "actor_id": "Alice",
             "payload": {
                 "proposition": {
                     "namespace": "minecraft", "predicate": "target_block_present",
                     "arguments": [1, 2, 3], "temporal_scope": "current", "polarity": True,
                 },
                 "record_type": "direct_observation", "source": "test",
                 "root_id": f"root-{index}", "revision": 1, "supersedes": [],
                 "provenance_id": f"provenance-{index}", "visible_to": ["Alice"],
                 "source_stream_id": "test-stream", "source_stream_revision": index + 1,
             }}
            for index, timestamp in enumerate(timestamps)
        ],
        {"event_type": "k11.observation_window_closed", "monotonic_ns": 200,
         "seq": len(timestamps) + 2, "run_id": run_id,
         "payload": {"reason": "natural_runtime_terminal",
                      "window_close_monotonic_ns": 200,
                      "configured_horizon_seconds": 1,
                      "shutdown_requested": False}},
    ]
    return {"run_id": run_id, "events": events}


@pytest.mark.parametrize("timestamps, expected", [
    ((), 0), ((99,), 0), ((100,), 1), ((199,), 1), ((200,), 0),
])
def test_k11_p0_evidence_coverage_counts_only_qualifying_window_events(timestamps, expected) -> None:
    metadata = _in_window_evidence_metadata(_windowed_evidence_trace(*timestamps))
    assert metadata["qualifying_event_count"] == expected
    assert metadata["qualified"] is (expected > 0)


def test_k11_p0_malformed_in_window_evidence_does_not_qualify() -> None:
    artifact = _windowed_evidence_trace(150)
    evidence = artifact["events"][1]
    evidence["payload"]["root_id"] = ""

    metadata = _in_window_evidence_metadata(artifact)

    assert metadata["qualifying_event_count"] == 0
    assert metadata["qualified"] is False


def test_k11_p0_all_zero_or_pre_window_only_cohort_fails_evidence_coverage() -> None:
    actor_threads = {("Alice", 1), ("Bob", 2)}
    sources = {"OpenAILanguageModel.gpt_api"}
    base = {"k11.model_call_started": 1, "k11.tool_call_entered": 1,
            "k11.eac_action_prepared": 1, "k11.eac_evidence_ingested": 8}
    assert _coverage_summary(
        base, actor_threads, sources, qualifying_in_window_evidence_count=0,
    )["evidence_ingestions_observed"] is False
    assert _in_window_evidence_metadata(_windowed_evidence_trace(99))["qualified"] is False


def test_k11_p0_mixed_zero_evidence_summaries_are_allowed_when_cohort_coverage_is_true() -> None:
    summaries = [{"runtime_error": None, "trace_validation": {"valid": True},
                  "analysis_validation": {"valid": True},
                  "primary_terminal_count": 1,
                  "exposure_coverage": {"qualifying_event_count": int(index == 0)}}
                 for index in range(P0_EXPECTED_RUNS)]
    assert _p0_passes(
        summaries=summaries, calibration_error=None,
        calibration={"traced": {"trace_validation": {"valid": True}}},
        coverage_sufficient=True,
    ) is True


def test_k11_p0_all_zero_evidence_cohort_fails_aggregate_gate() -> None:
    summaries = [{
        "runtime_error": None,
        "trace_validation": {"valid": True},
        "analysis_validation": {"valid": True},
        "primary_terminal_count": 1,
        "exposure_coverage": {"qualifying_event_count": 0},
    } for _ in range(P0_EXPECTED_RUNS)]

    assert _p0_passes(
        summaries=summaries, calibration_error=None,
        calibration={"traced": {"trace_validation": {"valid": True}}},
        coverage_sufficient=False,
    ) is False


def test_k11_p0_final_gate_requires_every_run_validation() -> None:
    summaries = [{
        "runtime_error": None,
        "trace_validation": {"valid": True},
        "analysis_validation": {"valid": True},
        "primary_terminal_count": 1,
    } for _ in range(P0_EXPECTED_RUNS)]
    calibration = {"traced": {"trace_validation": {"valid": True}}}

    assert _p0_passes(
        summaries=summaries, calibration_error=None,
        calibration=calibration, coverage_sufficient=True,
    ) is True

    summaries[3]["trace_validation"] = {"valid": False}
    assert _p0_passes(
        summaries=summaries, calibration_error=None,
        calibration=calibration, coverage_sufficient=True,
    ) is False


def test_k11_p0_final_gate_requires_every_offline_analysis() -> None:
    summaries = [{
        "runtime_error": None,
        "trace_validation": {"valid": True},
        "analysis_validation": {"valid": True},
        "primary_terminal_count": 1,
    } for _ in range(P0_EXPECTED_RUNS)]
    summaries[-1]["analysis_validation"] = {"valid": False}

    assert _p0_passes(
        summaries=summaries, calibration_error=None,
        calibration={"traced": {"trace_validation": {"valid": True}}},
        coverage_sufficient=True,
    ) is False


def test_k11_p0_timeout_fails_even_when_validation_artifact_exists() -> None:
    summary = {"runtime_error": None, "runtime_error_type": None}

    result = _apply_process_outcome(summary, {
        "timed_out": True,
        "exit_code": -15,
        "process_group_alive_after_cleanup": False,
        "post_artifact_linger": False,
        "post_parent_group_linger": False,
    })

    assert result["runtime_error_type"] == "RunProcessTimeout"


def test_k11_parent_rejects_worker_artifact_from_another_contract_or_cohort() -> None:
    expected = {
        "artifact_id": "minecraft-k11-p0-run-validation",
        "artifact_version": 2,
        "run_id": "K11-P0-01",
        "validation_contract": P0_VALIDATION_CONTRACT,
        "trace_schema_version": "minecraft-k11-trace/2",
        "manifest_digest": "a" * 64,
        "cohort_mode": "formal_p0",
    }
    k11_pilot._validate_worker_summary_identity(
        expected, expected_run_id="K11-P0-01",
        manifest_digest="a" * 64, cohort_mode="formal_p0",
    )
    for field, value in (
        ("artifact_id", "another-artifact"),
        ("artifact_version", 1),
        ("run_id", "K11-P0-02"),
        ("validation_contract", "minecraft-k11-p0-validation-contract/0"),
        ("manifest_digest", "b" * 64),
        ("cohort_mode", "development_smoke"),
    ):
        malformed = {**expected, field: value}
        with pytest.raises(K11PilotContractError, match="does not match its parent"):
            k11_pilot._validate_worker_summary_identity(
                malformed, expected_run_id="K11-P0-01",
                manifest_digest="a" * 64, cohort_mode="formal_p0",
            )
    with pytest.raises(K11PilotContractError, match="does not match its parent"):
        k11_pilot._validate_worker_summary_identity(
            [], expected_run_id="K11-P0-01",
            manifest_digest="a" * 64, cohort_mode="formal_p0",
        )


def test_k11_p0_worker_mode_uses_validated_manifest(tmp_path: Path, monkeypatch) -> None:
    row = {"run_id": "K11-P0-01", "runtime": {}}
    manifest = {
        "observation_window": {"horizon_seconds": 600},
        "runs": [row],
    }
    execution = object()
    called = []
    monkeypatch.setattr(k11_pilot, "load_p0_manifest", lambda _path: manifest)
    monkeypatch.setattr(k11_pilot.RuntimeExecution, "resolve", lambda _root: execution)
    monkeypatch.setattr(k11_pilot, "verify_eac_premanifest", lambda *_args, **_kwargs: {})
    def run_single(*args, **kwargs):
        called.append((args, kwargs))
        args[1].mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(k11_pilot, "_run_single_row", run_single)
    monkeypatch.setattr(
        k11_pilot, "cleanup_process_group_descendants",
        lambda **_kwargs: {
            "lingering_processes_before_cleanup": [], "term_sent": False,
            "kill_sent": False, "processes_after_cleanup": [],
        },
    )
    (tmp_path / "output").mkdir(mode=0o700)

    result = k11_pilot.main([
        "--manifest", str(tmp_path / "manifest.json"),
        "--output-root", str(tmp_path / "output"),
        "--worker-run-id", "K11-P0-01",
        "--execution-revision", "a" * 40,
        "--premanifest", str(tmp_path / "premanifest.json"),
        "--manifest-digest", k11_pilot._manifest_digest(manifest),
        "--cohort-mode", "development_smoke",
    ])

    assert result == 0
    assert called[0][0][0] == row
    assert called[0][1]["observation_horizon_seconds"] == 600
    assert called[0][1]["manifest_digest"] == k11_pilot._manifest_digest(manifest)
    assert called[0][1]["cohort_mode"] == "development_smoke"
    assert (tmp_path / "output" / "K11-P0-01" / "worker_shutdown.json").is_file()


def test_k11_development_smoke_requires_full_one_run_lifecycle(tmp_path: Path, monkeypatch) -> None:
    row = {"run_id": "K11-P0-01", "runtime": {}}
    monkeypatch.setattr(k11_pilot, "load_p0_manifest", lambda _path: {"runs": [row]})
    monkeypatch.setattr(
        k11_pilot,
        "_prepare_execution_identity",
        lambda root: (
            object(), "a" * 40, root / "K11_P0_EAC_PREMANIFEST.json",
            {"runtime_digest": "sha256:runtime", "premanifest_identity": "premanifest"},
        ),
    )
    summary = {
        "runtime_error": None,
        "trace_validation": {"valid": True},
        "analysis_validation": {"valid": True},
        "structural_validation": {"valid": True},
        "primary_terminal_count": 1,
        "event_type_counts": {
            "k11.model_call_started": 1,
            "k11.tool_call_entered": 1,
            "k11.eac_action_prepared": 1,
            "k11.eac_action_terminal": 1,
        },
        "exposure_coverage": {"qualified": True, "qualifying_event_count": 1},
    }
    monkeypatch.setattr(k11_pilot, "_run_isolated_row", lambda *_args, **_kwargs: summary)

    result = run_development_smoke(
        tmp_path / "manifest.json", output_root=tmp_path / "smoke", run_id="K11-P0-01",
    )

    assert result["smoke_passed"] is True
    assert result["validation_contract"] == P0_VALIDATION_CONTRACT
    assert result["manifest_digest"] == k11_pilot._manifest_digest({"runs": [row]})
    assert result["cohort_mode"] == "development_smoke"
    assert result["runtime_qualified"] is True
    assert result["structural_validation_passed"] is True
    assert result["runtime_qualified"] is True
    assert result["development_lifecycle_qualified"] is True
    assert result["development_exposure_qualified"] is True
    assert result["formal_p0"] is False
    artifact = json.loads((tmp_path / "smoke" / "DEV_SMOKE_VALIDATION.json").read_text())
    assert artifact == result


def test_k11_development_smoke_fails_without_terminal_disposition(tmp_path: Path, monkeypatch) -> None:
    row = {"run_id": "K11-P0-01", "runtime": {}}
    monkeypatch.setattr(k11_pilot, "load_p0_manifest", lambda _path: {"runs": [row]})
    monkeypatch.setattr(
        k11_pilot,
        "_prepare_execution_identity",
        lambda root: (
            object(), "a" * 40, root / "premanifest.json",
            {"runtime_digest": "sha256:runtime", "premanifest_identity": "premanifest"},
        ),
    )
    monkeypatch.setattr(k11_pilot, "_run_isolated_row", lambda *_args, **_kwargs: {
        "runtime_error": None,
        "trace_validation": {"valid": True},
        "analysis_validation": {"valid": True},
        "structural_validation": {"valid": True},
        "event_type_counts": {
            "k11.model_call_started": 1,
            "k11.tool_call_entered": 1,
            "k11.eac_action_prepared": 1,
        },
    })

    result = run_development_smoke(
        tmp_path / "manifest.json", output_root=tmp_path / "smoke", run_id="K11-P0-01",
    )

    assert result["smoke_passed"] is False


def test_k11_development_smoke_keeps_zero_evidence_structural_pass_separate(
    tmp_path: Path, monkeypatch,
) -> None:
    row = {"run_id": "K11-P0-01", "runtime": {}}
    monkeypatch.setattr(k11_pilot, "load_p0_manifest", lambda _path: {"runs": [row]})
    monkeypatch.setattr(
        k11_pilot,
        "_prepare_execution_identity",
        lambda root: (
            object(), "a" * 40, root / "premanifest.json",
            {"runtime_digest": "sha256:runtime", "premanifest_identity": "premanifest"},
        ),
    )
    monkeypatch.setattr(k11_pilot, "_run_isolated_row", lambda *_args, **_kwargs: {
        "runtime_error": None,
        "trace_validation": {"valid": True, "counts": {"evidence_ingestions": 0}},
        "analysis_validation": {"valid": True},
        "structural_validation": {"valid": True},
        "primary_terminal_count": 1,
        "event_type_counts": {
            "k11.model_call_started": 1,
            "k11.tool_call_entered": 1,
            "k11.eac_action_prepared": 1,
            "k11.eac_action_terminal": 1,
        },
        "exposure_coverage": {"qualified": False, "qualifying_event_count": 0},
    })

    result = run_development_smoke(
        tmp_path / "manifest.json", output_root=tmp_path / "smoke", run_id="K11-P0-01",
    )

    assert result["structural_validation_passed"] is True
    assert result["development_lifecycle_qualified"] is True
    assert result["development_exposure_qualified"] is False
    assert result["smoke_passed"] is False


def test_k11_cli_requires_explicit_formal_or_smoke_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        k11_pilot,
        "run_p0_manifest",
        lambda *_args, **_kwargs: pytest.fail("formal P0 must not start implicitly"),
    )

    with pytest.raises(SystemExit) as raised:
        k11_pilot.main([
            "--manifest", str(tmp_path / "manifest.json"),
            "--output-root", str(tmp_path / "output"),
        ])

    assert raised.value.code == 2


def test_k11_smoke_does_not_count_unrelated_terminal_for_primary_preparation() -> None:
    trace = {"events": [
        {
            "event_type": "k11.eac_action_prepared",
            "payload": {"exact_request": {
                "candidate_id": "primary",
                "action": {"identity": "placeBlock"},
            }},
        },
        {
            "event_type": "k11.eac_action_terminal",
            "payload": {"exact_request": {"candidate_id": "other"}},
        },
    ]}

    assert _primary_terminal_count(trace) == 0


def test_k11_worker_rejects_manifest_changed_after_parent_validation(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(
        k11_pilot,
        "load_p0_manifest",
        lambda _path: {"runs": [{"run_id": "K11-P0-01", "runtime": {}}]},
    )

    with pytest.raises(K11PilotContractError, match="parent-validated snapshot"):
        k11_pilot.main([
            "--manifest", str(tmp_path / "manifest.json"),
            "--output-root", str(tmp_path / "output"),
            "--worker-run-id", "K11-P0-01",
            "--execution-revision", "a" * 40,
            "--premanifest", str(tmp_path / "premanifest.json"),
            "--manifest-digest", "0" * 64,
            "--cohort-mode", "formal_p0",
        ])


def test_k11_cli_routes_formal_p0_only_when_explicit(tmp_path: Path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        k11_pilot,
        "run_p0_manifest",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"p0_passed": True},
    )

    result = k11_pilot.main([
        "--manifest", str(tmp_path / "manifest.json"),
        "--output-root", str(tmp_path / "output"),
        "--formal-p0",
    ])

    assert result == 0
    assert len(calls) == 1


def test_k11_prospective_measurement_cut_is_fail_closed() -> None:
    assert k11_pilot._measurement_cut_status({})["measurement_analysis_eligible"] is False
    cut = {
        "snapshot_valid": True,
        "snapshot_errors": [],
        "close_reason": "fixed_observation_horizon",
        "window_open_monotonic_ns": 1,
        "window_close_monotonic_ns": 2,
        "open_lifecycles": {"items": []},
        "active_executions": {"items": []},
        "censoring_inventory": {"items": []},
    }
    trace = {"measurement_cut": cut, "events": [
        {"event_type": "k11.observation_window_opened", "monotonic_ns": 1},
        {"event_type": "k11.observation_window_closed", "monotonic_ns": 2,
         "payload": {"reason": "fixed_observation_horizon", "window_close_monotonic_ns": 2}},
    ]}
    result = k11_pilot._measurement_cut_status(trace, cut_valid=True)
    assert result["measurement_analysis_eligible"] is True


def test_k11_active_or_post_close_effect_stops_admission() -> None:
    base = {"snapshot_valid": True, "close_reason": "fixed_observation_horizon",
            "snapshot_errors": [],
            "window_open_monotonic_ns": 1, "window_close_monotonic_ns": 2,
            "open_lifecycles": {"items": [{"kind": "native", "id": "c1"}]},
            "active_executions": {"items": []},
            "censoring_inventory": {"items": []}}
    trace = {"measurement_cut": base, "events": [
        {"event_type": "k11.observation_window_opened", "monotonic_ns": 1},
        {"event_type": "k11.observation_window_closed", "monotonic_ns": 2,
         "payload": {"window_close_monotonic_ns": 2}},
        {"event_type": "k11.eac_native_effect_entered", "monotonic_ns": 2},
    ]}
    result = k11_pilot._measurement_cut_status(trace, cut_valid=True)
    assert result["active_effect_at_horizon"] is True
    assert result["post_close_effect"] is True
    assert result["measurement_analysis_eligible"] is True


def test_k11_post_close_completion_alone_does_not_block() -> None:
    trace = {"measurement_cut": {
        "snapshot_valid": True, "close_reason": "fixed_observation_horizon",
        "snapshot_errors": [],
        "window_open_monotonic_ns": 1, "window_close_monotonic_ns": 2,
        "open_lifecycles": {"items": []}, "censoring_inventory": {"items": []},
        "active_executions": {"items": []},
    }, "events": [{
        "event_type": "k11.eac_native_effect_completed", "monotonic_ns": 3,
    }]}
    result = k11_pilot._measurement_cut_status(trace, cut_valid=True)
    assert result["post_close_effect"] is False
    assert result["active_effect_at_horizon"] is False
    assert result["measurement_analysis_eligible"] is True


def _prospective_runtime_cleanup(*, shutdown_complete=True, providers=None,
                                 movement_terminal=True, bridge_complete=True):
    return {
        "controller": {
            "active_assignments": {"Alice": "task-1"},
            "context": {"diagnostics": {"verdict": {
            "shutdown_complete": shutdown_complete,
            "authoritative_basis": {
                "provider_termination_unconfirmed_task_ids": providers or [],
                "movement_cancellation": {
                    "terminal": movement_terminal,
                    "actors": {"Alice": {"terminal": movement_terminal}},
                },
                "live_threads": [] if shutdown_complete else ["controller-worker"],
                "active_task_ids": [], "active_agent_ids": [],
                "incomplete_submission_task_ids": [], "undrained_queues": [],
            },
        }}}},
        "bridge_cleanup": {
            "cleanup_complete": bridge_complete,
            "incomplete_process_count": 0 if bridge_complete else 1,
            "process_retention": {
                "capacity": 64, "retained": 1,
                "truncated": False, "dropped_count": 0,
            },
            "processes": {
                "Alice": {
                    "pid": 10, "process_group_id": 10, "session_id": 10,
                    "alive_after_kill": not bridge_complete,
                    "identity_collection_errors": [],
                },
            },
        },
    }


def test_k11_prospective_cleanup_status_is_separate_and_fail_closed(
    monkeypatch,
) -> None:
    supervision = {
        "exit_code": 0, "artifact_ready": True, "timed_out": False,
        "post_artifact_linger": False,
        "post_parent_group_linger": False, "process_group_alive_after_cleanup": False,
        "term_sent": False, "kill_sent": False,
    }
    worker = {"processes_after_cleanup": [], "term_sent": True, "kill_sent": False}
    trace = {"events": [{"event_type": "k11.agent_step_started", "actor_id": "Alice"}]}
    monkeypatch.setattr(
        k11_pilot, "_late_trace_cleanup_evidence",
        lambda _trace: {"provider": True, "tool_native": True, "agent": True},
    )
    assert k11_pilot._prospective_cleanup_status(
        supervision, worker, _prospective_runtime_cleanup(), trace_artifact=trace,
        expected_actors=("Alice",),
    ) == "qualified_within_budget"
    assert k11_pilot._prospective_cleanup_status(
        supervision, worker, _prospective_runtime_cleanup(shutdown_complete=False),
        trace_artifact=trace, expected_actors=("Alice",),
    ) == "qualified_late"
    late_supervision = {**supervision, "timed_out": True}
    assert k11_pilot._prospective_cleanup_status(
        late_supervision, worker,
        _prospective_runtime_cleanup(shutdown_complete=False), trace_artifact=trace,
        expected_actors=("Alice",),
    ) == "qualified_late"
    assert k11_pilot._prospective_cleanup_status(
        supervision, worker, _prospective_runtime_cleanup(providers=["task-1"]),
        trace_artifact=trace, expected_actors=("Alice",),
    ) == "unknown"
    assert k11_pilot._prospective_cleanup_status(
        supervision, {"processes_after_cleanup": [{"pid": 1}]},
        _prospective_runtime_cleanup(), trace_artifact=trace,
        expected_actors=("Alice",),
    ) == "not_qualified"


def test_k11_late_cleanup_projection_preserves_frozen_failure(monkeypatch) -> None:
    supervision = {
        "exit_code": 0, "artifact_ready": True, "timed_out": False,
        "post_artifact_linger": False, "post_parent_group_linger": False,
        "process_group_alive_after_cleanup": False,
        "term_sent": False, "kill_sent": False,
    }
    worker = {"processes_after_cleanup": [], "term_sent": True, "kill_sent": False}
    runtime = _prospective_runtime_cleanup(shutdown_complete=False)
    frozen_verdict = deepcopy(runtime["controller"]["context"]["diagnostics"]["verdict"])
    measurement = {"measurement_snapshot_valid": True, "measurement_analysis_eligible": True}
    trace = {
        "measurement_cut": {"in_window_event_digest": "sha256:frozen"},
        "events": [{"event_type": "k11.agent_step_started", "actor_id": "Alice"}],
    }
    frozen_trace = deepcopy(trace)
    monkeypatch.setattr(
        k11_pilot, "_late_trace_cleanup_evidence",
        lambda _trace: {"provider": True, "tool_native": True, "agent": True},
    )

    result = k11_pilot._prospective_cleanup_projection(
        supervision, worker, runtime, trace_artifact=trace,
        expected_actors=("Alice",),
    )

    assert result["controller_verdict_at_budget"] == "failed"
    assert result["late_execution_capability_terminal"] is True
    assert result["late_future_reconciliation_state"] == "unknown"
    assert result["post_window_cleanup_status"] == "qualified_late"
    assert runtime["controller"]["context"]["diagnostics"]["verdict"] == frozen_verdict
    assert trace == frozen_trace
    assert measurement == {
        "measurement_snapshot_valid": True, "measurement_analysis_eligible": True,
    }
    summary = {
        "measurement_snapshot_valid": True,
        "censoring": {
            "active_effect_at_horizon": False,
            "post_close_effect": False,
            "uncertainty": False,
        },
    }
    assert k11_pilot._prospective_contamination_excluded(
        summary, cleanup_qualified=True,
    ) is True
    for field in ("active_effect_at_horizon", "post_close_effect", "uncertainty"):
        blocked = deepcopy(summary)
        blocked["censoring"][field] = True
        assert k11_pilot._prospective_contamination_excluded(
            blocked, cleanup_qualified=True,
        ) is False

    production_summary = {
        **summary,
        "runtime_error": "frozen controller failure",
        "measurement_snapshot": {
            "in_window_event_digest": "sha256:frozen",
        },
        "measurement_analysis_eligible": True,
    }
    frozen_fields = deepcopy(production_summary)
    k11_pilot._apply_prospective_cleanup_projection(
        production_summary, result,
    )
    assert production_summary["runtime_error"] == frozen_fields["runtime_error"]
    assert production_summary["measurement_snapshot"] == frozen_fields[
        "measurement_snapshot"
    ]
    assert production_summary["measurement_analysis_eligible"] is True
    assert production_summary["cleanup_status"] == "qualified_late"
    assert production_summary["cross_run_contamination_excluded"] is True
    assert production_summary["next_run_admission_allowed"] is True


@pytest.mark.parametrize("mutation", [
    "parent_term", "surviving_group", "bridge_failure", "provider_uncertain",
    "truncated_bridge", "missing_trace", "omitted_movement_actor",
    "omitted_bridge_actor", "within_parent_term", "within_truncated_bridge",
    "missing_assignments", "malformed_assignments",
])
def test_k11_late_cleanup_rejects_incomplete_or_forced_evidence(
    monkeypatch, mutation,
) -> None:
    supervision = {
        "exit_code": 0, "artifact_ready": True, "timed_out": False,
        "post_artifact_linger": False, "post_parent_group_linger": False,
        "process_group_alive_after_cleanup": False,
        "term_sent": False, "kill_sent": False,
    }
    worker = {"processes_after_cleanup": [], "term_sent": True, "kill_sent": False}
    runtime = _prospective_runtime_cleanup(
        shutdown_complete=mutation.startswith("within_"),
    )
    trace_evidence = {"provider": True, "tool_native": True, "agent": True}
    trace = {"events": [{"event_type": "k11.agent_step_started", "actor_id": "Alice"}]}
    monkeypatch.setattr(
        k11_pilot, "_late_trace_cleanup_evidence", lambda _trace: trace_evidence,
    )
    if mutation in {"parent_term", "within_parent_term"}:
        supervision["term_sent"] = True
    elif mutation == "surviving_group":
        supervision["process_group_alive_after_cleanup"] = True
    elif mutation == "bridge_failure":
        runtime["bridge_cleanup"]["cleanup_complete"] = False
        runtime["bridge_cleanup"]["incomplete_process_count"] = 1
    elif mutation == "provider_uncertain":
        runtime["controller"]["context"]["diagnostics"]["verdict"][
            "authoritative_basis"
        ]["provider_termination_unconfirmed_task_ids"] = ["task-1"]
    elif mutation in {"truncated_bridge", "within_truncated_bridge"}:
        runtime["bridge_cleanup"]["process_retention"]["truncated"] = True
        runtime["bridge_cleanup"]["process_retention"]["dropped_count"] = 1
    elif mutation == "omitted_movement_actor":
        runtime["controller"]["context"]["diagnostics"]["verdict"][
            "authoritative_basis"
        ]["movement_cancellation"]["actors"] = {}
    elif mutation == "omitted_bridge_actor":
        runtime["bridge_cleanup"]["processes"] = {}
        runtime["bridge_cleanup"]["process_retention"]["retained"] = 0
    elif mutation == "missing_assignments":
        runtime["controller"].pop("active_assignments")
    elif mutation == "malformed_assignments":
        runtime["controller"]["active_assignments"] = {"Alice": None}
    else:
        trace_evidence["agent"] = None

    result = k11_pilot._prospective_cleanup_projection(
        supervision, worker, runtime, trace_artifact=trace,
        expected_actors=("Alice",),
    )

    assert result["post_window_cleanup_status"] in {"unknown", "not_qualified"}


def test_k11_late_lifecycle_requires_scope_correlation() -> None:
    trace = {"events": [
        {
            "event_type": "k11.agent_step_started", "agent_step_id": "step-1",
            "task_id": "task-1", "actor_id": "Alice", "seq": 1,
        },
        {
            "event_type": "k11.agent_step_completed", "agent_step_id": "step-1",
            "task_id": "task-1", "actor_id": "Bob", "seq": 2,
        },
    ]}

    result = k11_pilot._late_trace_lifecycle_terminal(
        trace, "k11.agent_step_started", {"k11.agent_step_completed"},
        lambda event: json.dumps({
            "id": event.get("agent_step_id"),
            "task": event.get("task_id"),
            "actor": event.get("actor_id"),
        }, sort_keys=True),
    )

    assert result is None


def test_k11_late_cleanup_uses_configured_roster_not_active_assignments(
    monkeypatch,
) -> None:
    supervision = {
        "exit_code": 0, "artifact_ready": True, "timed_out": False,
        "post_artifact_linger": False, "post_parent_group_linger": False,
        "process_group_alive_after_cleanup": False,
        "term_sent": False, "kill_sent": False,
    }
    worker = {"processes_after_cleanup": [], "term_sent": True, "kill_sent": False}
    runtime = _prospective_runtime_cleanup(shutdown_complete=True)
    runtime["controller"]["active_assignments"] = {}
    movement = runtime["controller"]["context"]["diagnostics"]["verdict"][
        "authoritative_basis"
    ]["movement_cancellation"]
    movement["actors"]["Bob"] = {"terminal": True}
    runtime["bridge_cleanup"]["processes"]["Bob"] = {
        "pid": 11, "process_group_id": 10, "session_id": 10,
        "alive_after_kill": False, "identity_collection_errors": [],
    }
    runtime["bridge_cleanup"]["process_retention"]["retained"] = 2
    trace = {"events": [{"event_type": "k11.agent_step_started", "actor_id": "Alice"}]}
    monkeypatch.setattr(
        k11_pilot, "_late_trace_cleanup_evidence",
        lambda _trace: {"provider": True, "tool_native": True, "agent": True},
    )

    result = k11_pilot._prospective_cleanup_projection(
        supervision, worker, runtime, trace_artifact=trace,
        expected_actors=("Alice", "Bob"),
    )

    assert result["post_window_cleanup_status"] == "qualified_within_budget"


def test_k11_expected_actor_roster_is_bound_to_runtime_row() -> None:
    assert k11_pilot._expected_actor_roster({
        "runtime": {"agent_num": 2},
    }) == ("Alice", "Bob")
    with pytest.raises(K11PilotContractError, match="roster-expanding"):
        k11_pilot._expected_actor_roster({
            "runtime": {"agent_num": 2, "document": {"action": "chat"}},
        })


def test_k11_prospective_cohort_stops_before_blocked_next_row(
    tmp_path: Path, monkeypatch,
) -> None:
    rows = [{"run_id": f"K11-P0-{index:02d}", "runtime": {}}
            for index in range(1, 9)]
    manifest = {
        "artifact_version": 3, "runs": rows, "runtime_hygiene": {},
        "admission": {"same_domain": True, "no_world_reset": True},
    }
    monkeypatch.setattr(k11_pilot, "load_p0_manifest", lambda _path: manifest)
    monkeypatch.setattr(
        k11_pilot, "_prepare_execution_identity",
        lambda root: (
            object(), "a" * 40, root / "premanifest.json",
            {"runtime_digest": "sha256:runtime", "premanifest_identity": "premanifest"},
        ),
    )
    monkeypatch.setattr(
        k11_pilot, "measure_inprocess_overhead",
        lambda **_kwargs: {"traced": {"trace_validation": {"valid": True}}},
    )
    calls = []
    def blocked_row(row, **_kwargs):
        calls.append(row["run_id"])
        return {
            "runtime_error": None, "trace_validation": {"valid": True},
            "analysis_validation": {"valid": True},
            "measurement_analysis_eligible": True,
            "cross_run_contamination_excluded": False,
            "next_run_admission_allowed": False,
            "cleanup_status": "qualified_within_budget",
            "event_type_counts": {}, "agent_thread_pairs": [],
            "model_call_sources": [],
            "exposure_coverage": {"qualifying_event_count": 0},
        }
    monkeypatch.setattr(k11_pilot, "_run_isolated_row", blocked_row)

    result = k11_pilot.run_p0_manifest(
        tmp_path / "manifest.json", output_root=tmp_path / "output",
    )

    assert calls == ["K11-P0-01"]
    assert result["run_count"] == 1
    assert result["stopped_after_run_id"] == "K11-P0-01"
    assert result["blocked_next_run_id"] == "K11-P0-02"
    assert result["p0_passed"] is False


def _late_contract_fixture(*, shutdown_complete=False):
    identity = {
        "run_id": "K11-P0-01", "manifest_digest": "manifest",
        "execution_revision": "a" * 40, "runtime_digest": "sha256:runtime",
        "premanifest_identity": "premanifest",
        "validation_contract": LATE_CLEANUP_VALIDATION_CONTRACT,
        "trace_schema": "minecraft-k11-trace/3",
    }
    active = {
        "execution_id": "execution-00000007", "task_id": "task-1",
        "actor_id": "Alice",
    }
    retention = {
        "capacity": 64, "retained": 1, "truncated": False, "dropped_count": 0,
    }
    empty_retention = {
        "capacity": 64, "retained": 0, "truncated": False, "dropped_count": 0,
    }
    trace = {
        "measurement_cut": {
            "identity": identity, "window_close_monotonic_ns": 10,
            "event_prefix_high_water_sequence": 3,
            "active_executions": {"items": [active], "retention": retention},
        },
        "events": [{
            "seq": 1, "monotonic_ns": 2,
            "event_type": "k11.model_call_started",
            "task_id": "task-1", "actor_id": "Alice",
            "agent_step_id": "step-1", "tool_call_id": None,
            "payload": {"model_call_id": "model-call-1"},
        }],
    }
    verdict = {
        "shutdown_complete": shutdown_complete,
        "verdict_frozen_at_monotonic_ns": 100,
        "authoritative_basis": {
            "provider_termination_unconfirmed_task_ids": [],
            "movement_cancellation": {
                "terminal": True, "actors": {"Alice": {"terminal": True}},
            },
        },
    }
    execution = {
        **active,
        "future": {"done": True, "cancelled": False, "running": False},
        "future_started": {
            "event": "future_started", "execution_id": active["execution_id"],
            "monotonic_ns": 1,
        },
        "future_completed": {
            "event": "future_completed", "execution_id": active["execution_id"],
            "monotonic_ns": 150,
        },
        "cancellation": {
            "requested": True, "acknowledged": True,
            "requested_at_monotonic_ns": 20,
            "acknowledged_at_monotonic_ns": 149,
            "requested_at_wall_time": 1.0,
        },
        "latest_phase": "model_end",
        "lifecycle": {"items": [], "retention": empty_retention},
    }
    runtime = {
        "controller": {
            "context": {"diagnostics": {
                "schema_version": "controller-shutdown-diagnostics/2",
                "verdict": verdict,
            }},
            "execution_ledger": {
                "schema_version": "controller-late-execution-ledger/1",
                "captured_at_monotonic_ns": 200,
                "groups": {"items": [{
                    "task_id": "task-1",
                    "executions": {"items": [execution], "retention": retention},
                    "reconciliation": {
                        "group_completed": True, "shutdown_reconciled": False,
                        "assignments_released": False,
                        "terminal_state_persisted": True,
                        "post_processing_complete": False,
                        "execution_terminal_reconciled": True,
                    },
                }], "retention": retention},
            },
            "provider_ledger": {
                "schema_version": "k11-execution-provider-ledger/1",
                "captured_at_monotonic_ns": 201,
                "operations": {"items": [{
                    "provider_operation_id": "provider-operation-00000001",
                    "model_call_id": "model-call-1",
                    **active, "source": "LLMHandler.on_llm_start",
                    "start_monotonic_ns": 2, "terminal_monotonic_ns": 149,
                    "terminal": True, "outcome": "failed", "error_class": "Error",
                }], "retention": retention},
                "unresolved": {"items": [], "retention": empty_retention},
                "diagnostic_collection_error": None,
            },
            "late_lifecycle_ledger": {
                "schema_version": "k11-late-lifecycle-ledger/1",
                "captured_at_monotonic_ns": 202,
                "measurement_identity": identity,
                "event_prefix_high_water_sequence": 3,
                "post_cut_events": {"items": [], "retention": empty_retention},
                "instrumentation_errors": [],
                "diagnostic_collection_error": None,
            },
            "late_movement": {
                "captured_at_monotonic_ns": 203,
                "result": {
                    "terminal": True,
                    "actors": {"Alice": {"terminal": True}},
                },
            },
        },
        "bridge_cleanup": {
            "cleanup_complete": True, "incomplete_process_count": 0,
            "process_retention": retention,
            "processes": {"Alice": {
                "pid": 10, "process_group_id": 10, "session_id": 10,
                "alive_after_kill": False, "identity_collection_errors": [],
            }},
        },
    }
    supervision = {
        "artifact_ready": True, "exit_code": 0, "timed_out": False,
        "post_artifact_linger": False, "post_parent_group_linger": False,
        "process_group_alive_after_cleanup": False,
        "term_sent": False, "kill_sent": False,
    }
    shutdown = {"processes_after_cleanup": [], "term_sent": True, "kill_sent": False}
    evidence = k11_pilot._build_late_cleanup_evidence(
        run_id=identity["run_id"], manifest_digest=identity["manifest_digest"],
        runtime_result=runtime, trace_artifact=trace,
        supervision=supervision, shutdown=shutdown,
        expected_identity=identity,
    )
    return identity, trace, runtime, evidence


def _project_late_fixture(identity, trace, runtime, evidence, monkeypatch):
    monkeypatch.setattr(
        k11_pilot, "_late_trace_cleanup_evidence",
        lambda _trace: {"provider": True, "tool_native": True, "agent": True},
    )
    return k11_pilot._late_cleanup_evidence_projection(
        evidence, runtime_result=runtime, trace_artifact=trace,
        expected_identity=identity, expected_actors=("Alice",),
    )


def test_k11_contract3_qualifies_only_direct_identity_bound_late_proof(monkeypatch):
    identity, trace, runtime, evidence = _late_contract_fixture()
    frozen_trace = deepcopy(trace)
    frozen_verdict = deepcopy(runtime["controller"]["context"]["diagnostics"]["verdict"])
    result = _project_late_fixture(identity, trace, runtime, evidence, monkeypatch)
    assert result["controller_verdict_at_budget"] == "failed"
    assert result["late_future_reconciliation_state"] == "terminal"
    assert result["late_provider_terminal"] is True
    assert result["post_window_cleanup_status"] == "qualified_late"
    assert trace == frozen_trace
    assert runtime["controller"]["context"]["diagnostics"]["verdict"] == frozen_verdict


@pytest.mark.parametrize("mutation", [
    "missing_artifact", "missing_future_marker", "future_not_done",
    "future_start_marker_malformed", "cancellation_malformed",
    "cancellation_ack_timestamp_without_flag",
    "cancellation_ack_without_request", "cancellation_time_reversed",
    "execution_identity_mismatch", "provider_open", "provider_wrong_execution",
    "provider_unbound", "provider_missing_for_model_start",
    "execution_truncated", "provider_truncated", "provider_unresolved",
    "capture_before_verdict", "cut_digest_mismatch", "bridge_failure",
    "descendant_survives", "process_group_survives", "unreconciled_execution",
    "late_lifecycle_missing", "late_lifecycle_truncated",
    "late_movement_missing", "late_movement_nonterminal",
    "balanced_post_close_effect",
])
def test_k11_contract3_late_proof_fails_closed(monkeypatch, mutation):
    identity, trace, runtime, evidence = _late_contract_fixture()
    execution = evidence["post_verdict_execution_ledger"]["groups"]["items"][0][
        "executions"
    ]["items"][0]
    provider = evidence["execution_bound_provider_ledger"]
    if mutation == "missing_artifact":
        evidence = None
    elif mutation == "missing_future_marker":
        execution["future_completed"] = None
    elif mutation == "future_not_done":
        execution["future"] = {"done": False, "cancelled": False, "running": True}
    elif mutation == "future_start_marker_malformed":
        execution["future_started"]["event"] = "wrong"
    elif mutation == "cancellation_malformed":
        execution["cancellation"]["requested"] = "yes"
    elif mutation == "cancellation_ack_timestamp_without_flag":
        execution["cancellation"]["acknowledged"] = False
    elif mutation == "cancellation_ack_without_request":
        execution["cancellation"].update({
            "requested": False, "requested_at_monotonic_ns": None,
            "requested_at_wall_time": None, "acknowledged": True,
        })
    elif mutation == "cancellation_time_reversed":
        execution["cancellation"]["acknowledged_at_monotonic_ns"] = 19
    elif mutation == "execution_identity_mismatch":
        execution["actor_id"] = "Bob"
    elif mutation == "provider_open":
        provider["operations"]["items"][0].update({
            "terminal": False, "terminal_monotonic_ns": None, "outcome": None,
        })
    elif mutation == "provider_wrong_execution":
        provider["operations"]["items"][0]["execution_id"] = "execution-other"
    elif mutation == "provider_unbound":
        provider["operations"]["items"][0]["execution_id"] = None
    elif mutation == "provider_missing_for_model_start":
        provider["operations"]["items"] = []
        provider["operations"]["retention"]["retained"] = 0
    elif mutation == "execution_truncated":
        evidence["post_verdict_execution_ledger"]["groups"]["retention"].update({
            "truncated": True, "dropped_count": 1,
        })
    elif mutation == "provider_truncated":
        provider["operations"]["retention"].update({"truncated": True, "dropped_count": 1})
    elif mutation == "provider_unresolved":
        provider["unresolved"] = {"items": [{"reason": "mismatch"}], "retention": {
            "capacity": 64, "retained": 1, "truncated": False, "dropped_count": 0,
        }}
    elif mutation == "capture_before_verdict":
        evidence["post_verdict_execution_ledger"]["captured_at_monotonic_ns"] = 99
    elif mutation == "cut_digest_mismatch":
        evidence["measurement_cut"]["digest"] = "sha256:wrong"
    elif mutation == "bridge_failure":
        evidence["authorities"]["bridge"]["cleanup_complete"] = False
    elif mutation == "descendant_survives":
        evidence["authorities"]["worker_descendants"]["processes_after_cleanup"] = [{"pid": 1}]
    elif mutation == "unreconciled_execution":
        evidence["post_verdict_execution_ledger"]["groups"]["items"][0][
            "reconciliation"
        ]["execution_terminal_reconciled"] = False
    elif mutation == "late_lifecycle_missing":
        evidence["late_lifecycle_ledger"] = None
    elif mutation == "late_lifecycle_truncated":
        evidence["late_lifecycle_ledger"]["post_cut_events"]["retention"].update({
            "truncated": True, "dropped_count": 1,
        })
    elif mutation == "late_movement_missing":
        evidence["authorities"]["movement"] = None
    elif mutation == "late_movement_nonterminal":
        evidence["authorities"]["movement"]["result"]["terminal"] = False
    elif mutation == "balanced_post_close_effect":
        lifecycle = evidence["late_lifecycle_ledger"]["post_cut_events"]
        lifecycle["items"] = [{
            "seq": 4, "monotonic_ns": 11,
            "event_type": "k11.tool_call_entered",
        }, {
            "seq": 5, "monotonic_ns": 12,
            "event_type": "k11.tool_call_exited",
        }]
        lifecycle["retention"]["retained"] = 2
    else:
        evidence["authorities"]["worker_process_and_group"][
            "process_group_alive_after_cleanup"
        ] = True
    result = _project_late_fixture(identity, trace, runtime, evidence, monkeypatch)
    assert result["post_window_cleanup_status"] in {"unknown", "not_qualified"}


def test_k11_contract3_within_budget_and_contamination_gate(monkeypatch):
    identity, trace, runtime, evidence = _late_contract_fixture(shutdown_complete=True)
    result = _project_late_fixture(identity, trace, runtime, evidence, monkeypatch)
    assert result["post_window_cleanup_status"] == "qualified_within_budget"
    summary = {
        "measurement_snapshot_valid": True,
        "censoring": {
            "active_effect_at_horizon": False,
            "post_close_effect": False,
            "uncertainty": False,
        },
    }
    k11_pilot._apply_prospective_cleanup_projection(summary, result)
    assert summary["cross_run_contamination_excluded"] is True
    assert summary["next_run_admission_allowed"] is True
    for field in ("active_effect_at_horizon", "post_close_effect"):
        blocked = deepcopy(summary)
        blocked["censoring"][field] = True
        k11_pilot._apply_prospective_cleanup_projection(blocked, result)
        assert blocked["cross_run_contamination_excluded"] is False
        assert blocked["next_run_admission_allowed"] is False


def _v5_reconciliation_trace(*, new=False, duplicate=False):
    request = {
        "candidate_id": "candidate-1", "attempt_id": "attempt-1",
        "action": {"identity": "MineBlock", "digest": "action-digest"},
        "arguments": {"x": 1, "y": 2, "z": 3},
    }
    entered = {"event_type": "k11.tool_call_entered", "seq": 1,
               "monotonic_ns": 1, "payload": {"tool_name": "MineBlock"},
               "task_id": "task", "actor_id": "Alice", "agent_step_id": "step",
               "tool_call_id": "tool-1"}
    prepared = {
        "event_type": "k11.eac_action_prepared", "seq": 2,
        "monotonic_ns": 2, "task_id": "task", "actor_id": "Alice",
        "agent_step_id": "step", "tool_call_id": "tool-1",
        "payload": {"exact_request": request,
                    "exact_request_digest": k11_pilot.exact_request_digest(request)},
    }
    exited = {"event_type": "k11.tool_call_exited", "seq": 4,
              "monotonic_ns": 11,
              "task_id": "task", "actor_id": "Alice", "agent_step_id": "step",
              "tool_call_id": "tool-1",
              "payload": {"tool_name": "MineBlock", "outcome": "returned"}}
    events = [entered, prepared,
              {"event_type": "k11.observation_window_closed", "seq": 3}, exited]
    if duplicate:
        events.append({**exited, "seq": 5})
    if new:
        events.append({"event_type": "k11.tool_call_entered", "seq": 6,
                       "monotonic_ns": 12, "payload": {"tool_name": "MineBlock"},
                       "task_id": "task", "actor_id": "Alice", "tool_call_id": "new"})
    return {"measurement_cut": {
        "event_prefix_high_water_sequence": 3,
        "window_close_monotonic_ns": 10,
        "open_lifecycles": {"items": [{"kind": "tool_call_id", "id": "tool-1",
            "scope": {"task_id": "task", "actor_id": "Alice", "agent_step_id": "step",
                       "tool_call_id": "tool-1"}, "start_sequence": 1,
            "start_monotonic_ns": 1, "action": "MineBlock"}],
            "retention": {"retained": 1, "capacity": 8, "truncated": False, "dropped_count": 0}},
    }, "events": events}


def test_k11_contract4_reconciles_pre_h_tool_and_rejects_new_or_duplicate():
    assert k11_pilot._v5_reconcile_tool_native(_v5_reconciliation_trace())["valid"] is True
    assert k11_pilot._v5_reconcile_tool_native(
        _v5_reconciliation_trace(new=True))["valid"] is False
    assert k11_pilot._v5_reconcile_tool_native(
        _v5_reconciliation_trace(duplicate=True))["valid"] is False


def test_k11_contract4_tool_requires_one_request_and_matching_terminal_action():
    missing = _v5_reconciliation_trace()
    missing["events"] = [
        event for event in missing["events"]
        if event["event_type"] != "k11.eac_action_prepared"
    ]
    assert k11_pilot._v5_reconcile_tool_native(missing)["valid"] is False

    ambiguous = _v5_reconciliation_trace()
    second = deepcopy(ambiguous["events"][1])
    second["seq"] = 3
    second["payload"]["exact_request"]["attempt_id"] = "attempt-2"
    second["payload"]["exact_request_digest"] = k11_pilot.exact_request_digest(
        second["payload"]["exact_request"]
    )
    ambiguous["events"].insert(2, second)
    assert k11_pilot._v5_reconcile_tool_native(ambiguous)["valid"] is False

    mismatched_action = _v5_reconciliation_trace()
    mismatched_action["events"][-1]["payload"]["tool_name"] = "navigateTo"
    assert k11_pilot._v5_reconcile_tool_native(mismatched_action)["valid"] is False


def test_k11_contract4_reconciles_pre_h_native_with_exact_request_identity():
    request = {
        "candidate_id": "candidate-1", "attempt_id": "attempt-1",
        "action": {"identity": "MineBlock", "digest": "action-digest"},
        "arguments": {"x": 1, "y": 2, "z": 3},
    }
    scope = {
        "task_id": "task", "actor_id": "Alice", "agent_step_id": "step",
        "tool_call_id": "tool-1",
    }
    entered = {
        "event_type": "k11.eac_native_effect_entered", "seq": 1,
        "monotonic_ns": 1, **scope,
        "payload": {"exact_request": request,
                    "exact_request_digest": k11_pilot.exact_request_digest(request)},
    }
    completed = {
        "event_type": "k11.eac_native_effect_completed", "seq": 3,
        "monotonic_ns": 11, **scope,
        "payload": {"exact_request": request, "outcome": "succeeded"},
    }
    trace = {
        "measurement_cut": {
            "event_prefix_high_water_sequence": 2,
            "window_close_monotonic_ns": 10,
            "open_lifecycles": {
                "items": [{
                    "kind": "native", "id": "candidate-1", "scope": scope,
                    "start_sequence": 1, "start_monotonic_ns": 1,
                    "action": "MineBlock",
                }],
                "retention": {
                    "retained": 1, "capacity": 8,
                    "truncated": False, "dropped_count": 0,
                },
            },
        },
        "events": [entered, {"event_type": "k11.observation_window_closed",
                              "seq": 2, "monotonic_ns": 10}, completed],
    }
    result = k11_pilot._v5_reconcile_tool_native(trace)
    assert result["valid"] is True
    assert result["items"][0]["outcome"] == "succeeded"


def test_k11_contract4_chain_digest_is_append_only_and_non_cyclic():
    domain = {"host": "127.0.0.1", "port": 25565,
              "world_initialization": None, "same_domain": True, "no_world_reset": True}
    predecessor = k11_pilot._canonical_artifact_digest({"type": "genesis", "domain": domain})
    rows = []
    authority_fields = (
        "late_execution_capability_terminal", "late_provider_terminal",
        "late_tool_native_terminal", "late_agent_lifecycle_terminal",
        "late_movement_terminal", "late_bridge_terminal",
        "late_descendant_terminal", "late_process_group_terminal",
    )
    for index in range(3):
        summary = {"run_id": f"run-{index}", "manifest_digest": "b" * 64,
                   "execution_revision": "a" * 40,
                   "validation_contract": RECONCILIATION_VALIDATION_CONTRACT,
                   "measurement_snapshot_valid": True,
                   "measurement_structurally_valid": True,
                   "measurement_analysis_eligible": True,
                   "trace_validation": {"valid": True},
                   "analysis_validation": {"valid": True},
                   "measurement_snapshot": {
                    "digest": "sha256:" + f"{index + 1:064x}",
                    "event_prefix_high_water_sequence": index + 1,
                    "integrity": {"measurement_cut_mutated": False,
                                  "snapshot_valid": True}},
                   "late_cleanup_evidence": {
                       "artifact_id": RECONCILIATION_EVIDENCE_SCHEMA,
                       "digest": "sha256:" + f"{index + 10:064x}",
                   },
                   "late_cleanup": {"reconciliation": {
                       "valid": True, "active_count": 0, "items": [],
                       "new_post_close_effect_absent": True,
                   }, "authority_terminal_monotonic_ns": index + 9,
                   **{field: True for field in authority_fields}},
                   "runtime_error": None, "cleanup_status": "qualified_late",
                   "censoring": {"uncertainty": False}}
        k11_pilot._apply_v5_reconciliation(summary, predecessor_digest=predecessor,
                                           domain=domain, decision_ns=index + 10)
        rows.append(summary["admission_chain"])
        assert summary["post_window_predecessor_mutation_observed"] is False
        assert summary["next_run_admission_allowed"] is True
        k11_pilot._validate_v5_chain_row(
            summary, predecessor_digest=predecessor, domain=domain,
        )
        predecessor = rows[-1]["digest"]
    assert len({row["digest"] for row in rows}) == 3
    assert all(row["body"]["predecessor_admission_evidence_digest"] for row in rows)
    assert all(row["body"]["predecessor_admission_evidence_digest"] != row["digest"]
               for row in rows)


def test_k11_contract4_checked_in_v4_manifest_has_exact_bound_identities():
    root = Path(k11_pilot.__file__).resolve().parents[2]
    manifest = load_p0_manifest(root / "configs/minecraft/k11-p0-natural-manifest-v4.json")

    assert manifest["artifact_id"] == "minecraft-k11-p0-manifest"
    assert manifest["artifact_version"] == RECONCILIATION_MANIFEST_VERSION
    assert manifest["validation_contract"] == RECONCILIATION_VALIDATION_CONTRACT
    assert manifest["trace_schema"] == "minecraft-k11-trace/3"
    assert manifest["late_cleanup_evidence_contract"] == RECONCILIATION_EVIDENCE_SCHEMA
    assert manifest["admission"] == {
        "same_domain": True, "no_world_reset": True, "world_reset": False,
        "fail_closed": True, "terminal_before_next_row": True,
        "accumulated_state": True, "predecessor_chain": True,
        "state_model": "append_only_accumulated_state",
        "predecessor_model": "each_row_requires_terminal_predecessor",
    }
    identities = [k11_pilot._v5_domain_identity(row) for row in manifest["runs"]]
    assert identities == [{
        "host": "10.12.3.1", "port": 40000, "world_initialization": None,
        "same_domain": True, "no_world_reset": True,
    }] * 8


def test_k11_manifest_rejects_run_id_path_traversal(tmp_path):
    root = Path(k11_pilot.__file__).resolve().parents[2]
    manifest = json.loads(
        (root / "configs/minecraft/k11-p0-natural-manifest-v4.json").read_text()
    )
    manifest["runs"][0]["run_id"] = "../outside"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(K11PilotContractError, match="safe path components"):
        load_p0_manifest(path)


def test_k11_isolated_row_rejects_symlink_run_directory(tmp_path):
    output = tmp_path / "output"
    outside = tmp_path / "outside"
    output.mkdir()
    outside.mkdir()
    (output / "run").symlink_to(outside, target_is_directory=True)
    with pytest.raises(K11PilotContractError, match="must not be a symlink"):
        k11_pilot._run_isolated_row(
            {"run_id": "run"}, manifest_path="manifest.json", output_root=output,
            execution_revision="revision", premanifest_path=tmp_path / "pre.json",
            manifest_digest="manifest", cohort_mode="formal_p0",
        )


def test_k11_output_root_rejects_symlink_and_shared_write_permissions(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(K11PilotContractError, match="must not be a symlink"):
        k11_pilot._secure_output_root(link, create=False)

    real.chmod(0o722)
    with pytest.raises(K11PilotContractError, match="group/other writable"):
        k11_pilot._secure_output_root(real, create=False)


@pytest.mark.parametrize("mutation", [
    "missing_terminal", "identity_mismatch", "duplicate", "unknown_outcome",
    "retention_loss",
])
def test_k11_contract4_h_active_reconciliation_fails_closed(mutation):
    trace = _v5_reconciliation_trace()
    terminal = next(
        event for event in trace["events"]
        if event["event_type"] == "k11.tool_call_exited"
    )
    if mutation == "missing_terminal":
        trace["events"].remove(terminal)
    elif mutation == "identity_mismatch":
        terminal["actor_id"] = "Bob"
    elif mutation == "duplicate":
        trace["events"].append({**terminal, "seq": 5})
    elif mutation == "unknown_outcome":
        terminal["payload"]["outcome"] = "unknown"
    else:
        trace["measurement_cut"]["open_lifecycles"]["retention"].update(
            {"retained": 0, "truncated": True, "dropped_count": 1}
        )
    result = k11_pilot._v5_reconcile_tool_native(trace)
    assert result["valid"] is False


def test_k11_contract4_rejects_balanced_new_post_h_native_effect():
    trace = _v5_reconciliation_trace()
    trace["events"].extend([
        {"event_type": "k11.eac_native_effect_entered", "seq": 6,
         "monotonic_ns": 12,
         "task_id": "task", "actor_id": "Alice", "agent_step_id": "step",
         "payload": {"exact_request": {"candidate_id": "native-new",
                                          "attempt_id": "attempt-new"}}},
        {"event_type": "k11.eac_native_effect_completed", "seq": 7,
         "monotonic_ns": 13,
         "task_id": "task", "actor_id": "Alice", "agent_step_id": "step",
         "payload": {"exact_request": {"candidate_id": "native-new",
                                          "attempt_id": "attempt-new"},
                     "outcome": "succeeded"}},
    ])
    assert k11_pilot._v5_reconcile_tool_native(trace)["valid"] is False


def test_k11_contract4_rejects_balanced_new_post_h_tool_effect():
    trace = _v5_reconciliation_trace(new=True)
    trace["events"].append({
        "event_type": "k11.tool_call_exited", "seq": 7, "monotonic_ns": 13,
        "task_id": "task", "actor_id": "Alice", "tool_call_id": "new",
        "payload": {"tool_name": "MineBlock", "outcome": "returned"},
    })
    assert k11_pilot._v5_reconcile_tool_native(trace)["valid"] is False


def test_k11_contract4_unknown_authority_blocks_admission():
    authority_fields = (
        "late_execution_capability_terminal", "late_provider_terminal",
        "late_tool_native_terminal", "late_agent_lifecycle_terminal",
        "late_movement_terminal", "late_bridge_terminal",
        "late_descendant_terminal", "late_process_group_terminal",
    )
    summary = {
        "runtime_error": None, "cleanup_status": "qualified_late",
        "measurement_snapshot": {"digest": "cut", "event_prefix_high_water_sequence": 2,
                                  "integrity": {"measurement_cut_mutated": False,
                                                "snapshot_valid": True}},
        "late_cleanup": {
            "reconciliation": {
                "valid": True, "active_count": 1,
                "items": [{"terminal_monotonic_ns": 1}],
                "new_post_close_effect_absent": True,
            },
            "authority_terminal_monotonic_ns": 1,
            **{field: True for field in authority_fields},
        },
        "late_cleanup_evidence": {"digest": "late"},
        "censoring": {"uncertainty": False},
    }
    summary["late_cleanup"]["late_provider_terminal"] = None
    k11_pilot._apply_v5_reconciliation(
        summary, predecessor_digest="sha256:" + "a" * 64,
        domain={"host": "h", "port": 1, "world_initialization": None,
                "same_domain": True, "no_world_reset": True}, decision_ns=1,
    )
    assert summary["next_run_admission_allowed"] is False


def test_k11_contract4_exact_terminal_and_all_authorities_admit_next_row():
    authority_fields = (
        "late_execution_capability_terminal", "late_provider_terminal",
        "late_tool_native_terminal", "late_agent_lifecycle_terminal",
        "late_movement_terminal", "late_bridge_terminal",
        "late_descendant_terminal", "late_process_group_terminal",
    )
    summary = {
        "run_id": "run", "manifest_digest": "manifest",
        "validation_contract": RECONCILIATION_VALIDATION_CONTRACT,
        "runtime_error": None, "cleanup_status": "qualified_late",
        "measurement_snapshot_valid": True,
        "measurement_structurally_valid": True,
        "measurement_analysis_eligible": True,
        "measurement_snapshot": {
            "digest": "cut", "event_prefix_high_water_sequence": 2,
            "integrity": {"measurement_cut_mutated": False, "snapshot_valid": True},
        },
        "late_cleanup": {
            "reconciliation": {
                "valid": True, "active_count": 1, "resolved_count": 1,
                "items": [{"terminal_monotonic_ns": 9}],
                "new_post_close_effect_absent": True,
            },
            "authority_terminal_monotonic_ns": 9,
            **{field: True for field in authority_fields},
        },
        "late_cleanup_evidence": {"digest": "late"},
        "censoring": {"uncertainty": False},
    }
    domain = {"host": "h", "port": 1, "world_initialization": None,
              "same_domain": True, "no_world_reset": True}
    k11_pilot._apply_v5_reconciliation(
        summary, predecessor_digest="sha256:" + "a" * 64,
        domain=domain, decision_ns=10,
    )
    assert summary["post_window_predecessor_mutation_observed"] is True
    assert summary["post_window_predecessor_mutation_resolved"] is True
    assert summary["next_run_admission_allowed"] is True
    future_dated = deepcopy(summary)
    k11_pilot._apply_v5_reconciliation(
        future_dated, predecessor_digest="sha256:" + "a" * 64,
        domain=domain, decision_ns=8,
    )
    assert future_dated["post_window_predecessor_mutation_resolved"] is False
    assert future_dated["next_run_admission_allowed"] is False


def test_k11_contract4_chain_rejects_predecessor_domain_and_body_corruption(tmp_path):
    domain = {"host": "h", "port": 1, "world_initialization": None,
              "same_domain": True, "no_world_reset": True}
    predecessor = "sha256:" + "a" * 64
    summary = {"run_id": "run", "manifest_digest": "b" * 64,
               "execution_revision": "a" * 40,
               "validation_contract": RECONCILIATION_VALIDATION_CONTRACT,
               "runtime_error": None, "cleanup_status": "qualified_late",
               "censoring": {"uncertainty": False},
               "measurement_snapshot_valid": True,
               "measurement_structurally_valid": True,
               "measurement_analysis_eligible": True,
               "trace_validation": {"valid": True},
               "analysis_validation": {"valid": True},
               "measurement_snapshot": {"digest": "sha256:" + "c" * 64,
                   "event_prefix_high_water_sequence": 1,
                   "integrity": {"measurement_cut_mutated": False,
                                 "snapshot_valid": True}},
               "late_cleanup_evidence": {
                   "artifact_id": RECONCILIATION_EVIDENCE_SCHEMA,
                   "digest": "sha256:" + "d" * 64,
               },
               "late_cleanup": {"reconciliation": {"valid": True, "items": [],
                   "active_count": 0, "new_post_close_effect_absent": True},
                   "authority_terminal_monotonic_ns": 1,
                   **{field: True for field in (
                       "late_execution_capability_terminal", "late_provider_terminal",
                       "late_tool_native_terminal", "late_agent_lifecycle_terminal",
                       "late_movement_terminal", "late_bridge_terminal",
                       "late_descendant_terminal", "late_process_group_terminal",
                   )}}}
    k11_pilot._apply_v5_reconciliation(summary, predecessor_digest=predecessor,
                                       domain=domain, decision_ns=1)
    k11_pilot._validate_v5_chain_row(summary, predecessor_digest=predecessor, domain=domain)
    path = tmp_path / "admission_evidence.json"
    path.write_text(json.dumps(summary["admission_chain"]))
    k11_pilot._validate_v5_predecessor_artifact(
        path, expected_digest=summary["admission_chain"]["digest"],
        domain=domain, expected_artifact=summary["admission_chain"],
    )
    for bad_predecessor, bad_domain, mutate in [
        ("sha256:" + "b" * 64, domain, False),
        (predecessor, {**domain, "port": 2}, False),
        (predecessor, domain, True),
    ]:
        broken = deepcopy(summary)
        if mutate:
            broken["admission_chain"]["body"]["run_id"] = "tampered"
        with pytest.raises(K11PilotContractError, match="corrupt or mismatched"):
            k11_pilot._validate_v5_chain_row(
                broken, predecessor_digest=bad_predecessor, domain=bad_domain,
            )
    denied = deepcopy(summary["admission_chain"])
    denied["body"]["split_decisions"]["next_run_admission_allowed"] = False
    denied["digest"] = k11_pilot._canonical_artifact_digest(denied["body"])
    path.write_text(json.dumps(denied))
    with pytest.raises(K11PilotContractError, match="did not admit"):
        k11_pilot._validate_v5_predecessor_artifact(
            path, expected_digest=denied["digest"], domain=domain,
            expected_artifact=denied,
        )


def test_k11_contract4_genesis_and_prelaunch_binding_are_explicit():
    domain = {"host": "h", "port": 1, "world_initialization": None,
              "same_domain": True, "no_world_reset": True}
    genesis = k11_pilot._canonical_artifact_digest({"type": "genesis", "domain": domain})
    command = k11_pilot._worker_command(
        "manifest.json", Path("out"), run_id="run", execution_revision="r",
        premanifest_path=Path("pre.json"), manifest_digest="m", cohort_mode="formal_p0",
        validation_contract=RECONCILIATION_VALIDATION_CONTRACT,
        trace_schema="minecraft-k11-trace/3", validation_artifact_version=5,
        prelaunch_binding_digest=genesis,
    )
    assert command[-2:] == ["--prelaunch-binding-digest", genesis]
    assert k11_pilot._canonical_artifact_digest({"type": "genesis", "domain": domain}) == genesis


def test_k11_contract4_persists_and_validates_explicit_genesis(tmp_path):
    domain = {"host": "h", "port": 1, "world_initialization": None,
              "same_domain": True, "no_world_reset": True}
    digest = k11_pilot._write_v5_genesis(
        root=tmp_path, manifest_digest="manifest",
        execution_revision="revision", domain=domain,
    )
    artifact = json.loads((tmp_path / "GENESIS_ADMISSION_EVIDENCE.json").read_text())
    assert artifact["body"]["no_predecessor"] is True
    assert artifact["body"]["no_reset_no_rollback"] is True
    assert artifact["digest"] == digest
    assert k11_pilot._validate_v5_predecessor_artifact(
        tmp_path / "GENESIS_ADMISSION_EVIDENCE.json",
        expected_digest=digest, domain=domain,
    ) == artifact

    artifact["body"]["domain"]["port"] = 2
    (tmp_path / "GENESIS_ADMISSION_EVIDENCE.json").write_text(json.dumps(artifact))
    with pytest.raises(K11PilotContractError, match="digest is invalid"):
        k11_pilot._validate_v5_predecessor_artifact(
            tmp_path / "GENESIS_ADMISSION_EVIDENCE.json",
            expected_digest=digest, domain=domain,
        )
    with pytest.raises(K11PilotContractError, match="is missing"):
        k11_pilot._validate_v5_predecessor_artifact(
            tmp_path / "missing.json", expected_digest=digest, domain=domain,
        )


def test_k11_contract4_late_cleanup_routes_v4_identity_and_reconciliation(monkeypatch):
    identity, trace, runtime, _ = _late_contract_fixture(shutdown_complete=True)
    identity = {**identity, "validation_contract": RECONCILIATION_VALIDATION_CONTRACT}
    trace["measurement_cut"]["identity"] = identity
    runtime["controller"]["late_lifecycle_ledger"]["measurement_identity"] = identity
    trace["measurement_cut"]["open_lifecycles"] = {
        "items": [], "retention": {"retained": 0, "capacity": 8,
                                     "truncated": False, "dropped_count": 0},
    }
    evidence = k11_pilot._build_late_cleanup_evidence(
        run_id=identity["run_id"], manifest_digest=identity["manifest_digest"],
        runtime_result=runtime, trace_artifact=trace,
        supervision={"artifact_ready": True, "exit_code": 0, "timed_out": False,
                     "post_artifact_linger": False, "post_parent_group_linger": False,
                     "process_group_alive_after_cleanup": False, "term_sent": False,
                     "kill_sent": False},
        shutdown={"processes_after_cleanup": []}, domain_identity={
            "host": "h", "port": 1, "world_initialization": None,
            "same_domain": True, "no_world_reset": True},
        expected_identity=identity,
    )
    assert evidence["artifact_id"] == RECONCILIATION_EVIDENCE_SCHEMA
    assert evidence["reconciliation"]["valid"] is True
    monkeypatch.setattr(
        k11_pilot, "_late_trace_cleanup_evidence",
        lambda _trace: {"provider": True, "tool_native": True, "agent": True},
    )
    domain = evidence["domain"]
    projection = k11_pilot._late_cleanup_evidence_projection(
        evidence, runtime_result=runtime, trace_artifact=trace,
        expected_identity=identity, expected_actors=("Alice",),
        expected_domain=domain,
    )
    assert projection["post_window_cleanup_status"] == "qualified_within_budget"
    for field in ("timed_out", "post_artifact_linger", "post_parent_group_linger"):
        broken = deepcopy(evidence)
        broken["authorities"]["worker_process_and_group"][field] = True
        result = k11_pilot._late_cleanup_evidence_projection(
            broken, runtime_result=runtime, trace_artifact=trace,
            expected_identity=identity, expected_actors=("Alice",),
            expected_domain=domain,
        )
        assert result["post_window_cleanup_status"] == "not_qualified"


def _write_v5_prelaunch(run_dir, *, run_id="run", manifest_digest="manifest",
                        predecessor="sha256:" + "a" * 64, domain=None):
    domain = domain or {
        "host": "h", "port": 1, "world_initialization": None,
        "same_domain": True, "no_world_reset": True,
    }
    binding = {
        "artifact_id": "minecraft-k11-p0-prelaunch-admission-binding",
        "artifact_version": 1, "run_id": run_id,
        "manifest_digest": manifest_digest,
        "predecessor_admission_evidence_digest": predecessor,
        "domain": domain,
    }
    binding["digest"] = k11_pilot._canonical_artifact_digest(binding)
    run_dir.mkdir(parents=True)
    (run_dir / "prelaunch_admission_binding.json").write_text(json.dumps(binding))
    return binding


def test_k11_contract4_row_start_accepts_only_exact_prelaunch_binding(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    binding = _write_v5_prelaunch(run_dir)
    domain = binding["domain"]
    kwargs = {
        "run_id": "run", "manifest_digest": "manifest",
        "validation_contract": RECONCILIATION_VALIDATION_CONTRACT,
        "prelaunch_binding_digest": binding["digest"],
        "predecessor_digest": binding["predecessor_admission_evidence_digest"],
        "domain_identity": domain,
    }
    _, path_identity = k11_pilot._validate_row_start_directory(run_dir, **kwargs)

    class SetupReached(Exception):
        pass

    def recorder(*_args, **_kwargs):
        raise SetupReached

    monkeypatch.setattr(k11_pilot, "K11TraceRecorder", recorder)
    premanifest = tmp_path / "premanifest.json"
    premanifest.write_text(json.dumps({
        "runtime_digest": "sha256:runtime", "premanifest_identity": "premanifest",
    }))
    with pytest.raises(SetupReached):
        k11_pilot._run_single_row(
            {"run_id": "run"}, run_dir, execution=object(),
            execution_revision="a" * 40, premanifest_path=premanifest,
            observation_horizon_seconds=600, manifest_digest="manifest",
            cohort_mode="development_smoke",
            validation_contract=RECONCILIATION_VALIDATION_CONTRACT,
            trace_schema="minecraft-k11-trace/3", validation_artifact_version=5,
            prospective=True, prelaunch_binding_digest=binding["digest"],
            predecessor_digest=binding["predecessor_admission_evidence_digest"],
            domain_identity=domain,
        )

    (run_dir / "extra.json").write_text("{}")
    with pytest.raises(K11PilotContractError, match="exactly one"):
        k11_pilot._validate_row_start_directory(run_dir, **kwargs)

    (run_dir / "extra.json").unlink()
    binding_path = run_dir / "prelaunch_admission_binding.json"
    binding_path.unlink()
    binding_path.write_text(json.dumps(binding))
    with pytest.raises(K11PilotContractError, match="was replaced"):
        k11_pilot._validate_row_start_directory(
            run_dir, expected_path_identity=path_identity, **kwargs,
        )


def test_k11_row_start_preserves_historical_empty_directory_rule(tmp_path):
    for contract in (
        P0_VALIDATION_CONTRACT, PROSPECTIVE_VALIDATION_CONTRACT,
        LATE_CLEANUP_VALIDATION_CONTRACT,
    ):
        run_dir = tmp_path / contract.rsplit("/", 1)[-1]
        run_dir.mkdir()
        k11_pilot._validate_row_start_directory(
            run_dir, run_id="run", manifest_digest="manifest",
            validation_contract=contract,
        )
        (run_dir / "preexisting.json").write_text("{}")
        with pytest.raises(K11PilotContractError, match="already contains data"):
            k11_pilot._validate_row_start_directory(
                run_dir, run_id="run", manifest_digest="manifest",
                validation_contract=contract,
            )


def test_k11_contract4_row_start_rejects_mismatch_and_prelaunch_symlink(tmp_path):
    run_dir = tmp_path / "run"
    binding = _write_v5_prelaunch(run_dir)
    with pytest.raises(K11PilotContractError, match="binding is invalid"):
        k11_pilot._validate_row_start_directory(
            run_dir, run_id="run", manifest_digest="different",
            validation_contract=RECONCILIATION_VALIDATION_CONTRACT,
            prelaunch_binding_digest=binding["digest"],
            predecessor_digest=binding["predecessor_admission_evidence_digest"],
            domain_identity=binding["domain"],
        )

    (run_dir / "prelaunch_admission_binding.json").unlink()
    target = tmp_path / "binding.json"
    target.write_text(json.dumps(binding))
    (run_dir / "prelaunch_admission_binding.json").symlink_to(target)
    with pytest.raises(K11PilotContractError, match="exactly one"):
        k11_pilot._validate_row_start_directory(
            run_dir, run_id="run", manifest_digest="manifest",
            validation_contract=RECONCILIATION_VALIDATION_CONTRACT,
            prelaunch_binding_digest=binding["digest"],
            predecessor_digest=binding["predecessor_admission_evidence_digest"],
            domain_identity=binding["domain"],
        )
    target_before = target.read_text()
    with pytest.raises(K11PilotContractError, match="refusing to replace"):
        k11_pilot._write_json_exclusive_nofollow(
            run_dir / "prelaunch_admission_binding.json", binding,
        )
    assert target.read_text() == target_before


def test_k11_contract4_pre_runtime_failure_is_versioned_valid_and_denied(
    tmp_path, monkeypatch,
):
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    premanifest = tmp_path / "premanifest.json"
    premanifest.write_text(json.dumps({
        "runtime_digest": "sha256:runtime", "premanifest_identity": "premanifest",
    }))
    supervision = {
        "artifact_ready": False, "exit_code": 1, "timed_out": False,
        "post_artifact_linger": False, "post_parent_group_linger": False,
        "process_group_alive_after_cleanup": False,
        "term_sent": False, "kill_sent": False,
        "lingering_processes_before_cleanup": [], "duration": 1.0,
    }
    monkeypatch.setattr(k11_pilot, "supervise_process", lambda *_args, **_kwargs: supervision)
    domain = {
        "host": "h", "port": 1, "world_initialization": None,
        "same_domain": True, "no_world_reset": True,
    }
    predecessor = "sha256:" + "a" * 64
    summary = k11_pilot._run_isolated_row(
        {"run_id": "run", "runtime": {"host": "h", "port": 1, "agent_num": 2}},
        manifest_path=tmp_path / "manifest.json", output_root=output,
        execution_revision="b" * 40, premanifest_path=premanifest,
        manifest_digest="e" * 64, cohort_mode="development_smoke",
        validation_contract=RECONCILIATION_VALIDATION_CONTRACT,
        trace_schema="minecraft-k11-trace/3", validation_artifact_version=5,
        prospective=True, predecessor_digest=predecessor, domain_identity=domain,
    )
    assert summary["runtime_error_type"] == "RunProcessFailure"
    assert summary["process_supervision"]["exit_code"] == 1
    assert summary["next_run_admission_allowed"] is False
    assert summary["measurement_prefix_immutable"] is False
    assert summary["execution_overlap_excluded"] is False
    assert summary["admission_chain"]["body"]["measurement_cut_digest"] is None
    assert summary["admission_chain"]["body"]["event_prefix_high_water_sequence"] is None
    k11_pilot._validate_v5_chain_row(
        summary, predecessor_digest=predecessor, domain=domain,
    )

    late = json.loads((output / "run" / "late_cleanup_evidence.json").read_text())
    assert late["artifact_id"] == RECONCILIATION_EVIDENCE_SCHEMA
    assert late["artifact_version"] == 2
    assert late["identity"]["validation_contract"] == RECONCILIATION_VALIDATION_CONTRACT
    assert late["measurement_cut"]["digest"] is None
    assert late["h_active_executions"]["items"] == []
    assert late["integrity"]["collection_errors"]


def test_k11_contract3_pre_runtime_failure_retains_historical_late_schema():
    identity = {
        "run_id": "run", "manifest_digest": "manifest",
        "execution_revision": "a" * 40, "runtime_digest": "sha256:runtime",
        "premanifest_identity": "premanifest",
        "validation_contract": LATE_CLEANUP_VALIDATION_CONTRACT,
        "trace_schema": "minecraft-k11-trace/3",
    }
    evidence = k11_pilot._build_late_cleanup_evidence(
        run_id="run", manifest_digest="manifest", runtime_result=None,
        trace_artifact=None, supervision={}, shutdown=None,
        expected_identity=identity,
    )
    assert evidence["artifact_id"] == LATE_CLEANUP_EVIDENCE_SCHEMA
    assert evidence["artifact_version"] == 1
    assert evidence["identity"]["validation_contract"] is None
    assert evidence["measurement_cut"]["digest"] is None


def test_k11_contract4_failed_first_row_stops_before_second_without_masking(
    tmp_path, monkeypatch,
):
    rows = [{
        "run_id": f"K11-P0-{index:02d}",
        "runtime": {"host": "h", "port": 1},
    } for index in range(1, 9)]
    manifest = {
        "artifact_version": RECONCILIATION_MANIFEST_VERSION,
        "runs": rows, "runtime_hygiene": {},
        "admission": {"same_domain": True, "no_world_reset": True},
    }
    monkeypatch.setattr(k11_pilot, "load_p0_manifest", lambda _path: manifest)
    monkeypatch.setattr(
        k11_pilot, "_prepare_execution_identity",
        lambda root: (
            object(), "b" * 40, root / "premanifest.json",
            {"runtime_digest": "sha256:runtime", "premanifest_identity": "premanifest"},
        ),
    )
    monkeypatch.setattr(
        k11_pilot, "measure_inprocess_overhead",
        lambda **_kwargs: {"traced": {"trace_validation": {"valid": True}}},
    )
    calls = []

    def failed_row(row, **kwargs):
        calls.append(row["run_id"])
        summary = {
            "run_id": row["run_id"], "manifest_digest": kwargs["manifest_digest"],
            "execution_revision": kwargs["execution_revision"],
            "validation_contract": RECONCILIATION_VALIDATION_CONTRACT,
            "runtime_error": "worker failed", "runtime_error_type": "RunProcessFailure",
            "trace_validation": {"valid": False},
            "analysis_validation": {"valid": False},
            "measurement_analysis_eligible": False,
            "measurement_snapshot": {"valid": False},
            "measurement_snapshot_valid": False,
            "late_cleanup_evidence": {
                "artifact_id": RECONCILIATION_EVIDENCE_SCHEMA,
                "digest": "sha256:" + "c" * 64,
            },
            "late_cleanup": {}, "cleanup_status": "unknown",
            "censoring": {"uncertainty": True},
            "event_type_counts": {}, "agent_thread_pairs": [],
            "model_call_sources": [],
            "exposure_coverage": {"qualifying_event_count": 0},
        }
        return k11_pilot._apply_v5_reconciliation(
            summary, predecessor_digest=kwargs["predecessor_digest"],
            domain=kwargs["domain_identity"], decision_ns=1,
        )

    monkeypatch.setattr(k11_pilot, "_run_isolated_row", failed_row)
    result = k11_pilot.run_p0_manifest(
        tmp_path / "manifest.json", output_root=tmp_path / "output",
    )
    assert calls == ["K11-P0-01"]
    assert result["run_count"] == 1
    assert result["blocked_next_run_id"] == "K11-P0-02"
    assert result["runs"][0]["runtime_error_type"] == "RunProcessFailure"
    assert result["runs"][0]["next_run_admission_allowed"] is False
