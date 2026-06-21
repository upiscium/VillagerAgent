import json

from benchmarks.craft.fallback_monitor import build_fallback_monitor_report


def test_fallback_monitor_groups_by_condition_and_structure(tmp_path):
    _write_run(
        tmp_path,
        "craft_baseline_seed3",
        [
            {"structure_id": 0, "builder_action": {"_builder_fallback": "fallback"}},
            {"structure_id": 0, "builder_action": {}},
            {"structure_id": 1, "builder_action": {}},
        ],
    )
    _write_run(
        tmp_path,
        "craft_dual_dag_seed3",
        [
            {"structure_id": 0, "builder_action": {"_builder_fallback": "fallback"}},
            {"structure_id": 1, "builder_action": {"_builder_fallback": "fallback"}},
        ],
    )

    report = build_fallback_monitor_report(
        ["craft_baseline_seed3", "craft_dual_dag_seed3"],
        result_root=tmp_path,
    )

    by_condition = {row["group"]: row for row in report["by_condition"]}
    assert by_condition["baseline"]["builder_fallback_count"] == 1
    assert by_condition["baseline"]["builder_fallback_rate"] == 1 / 3
    assert by_condition["dual_dag"]["builder_fallback_count"] == 2
    by_structure = {row["group"]: row for row in report["by_condition_and_structure"]}
    assert by_structure["baseline|structure=0"]["builder_fallback_rate"] == 0.5
    assert by_structure["dual_dag|structure=1"]["builder_fallback_rate"] == 1.0


def test_fallback_monitor_records_failed_runs_separately(tmp_path):
    normalized = tmp_path / "craft_dual_dag_failed" / "normalized"
    normalized.mkdir(parents=True)
    (normalized / "summary.json").write_text(
        json.dumps({
            "status": "failed",
            "failure": {"type": "OllamaPreflightError", "message": "dns failure"},
        }),
        encoding="utf-8",
    )

    report = build_fallback_monitor_report(["craft_dual_dag_failed"], result_root=tmp_path)

    row = report["runs"][0]["structure_rows"][0]
    assert row["status"] == "failed"
    assert row["error_type"] == "OllamaPreflightError"
    assert report["by_condition"][0]["failed_run_count"] == 1


def _write_run(tmp_path, run_name, turns):
    normalized = tmp_path / run_name / "normalized"
    normalized.mkdir(parents=True)
    (normalized / "summary.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    with (normalized / "turns.jsonl").open("w", encoding="utf-8") as f:
        for turn in turns:
            f.write(json.dumps(turn) + "\n")
