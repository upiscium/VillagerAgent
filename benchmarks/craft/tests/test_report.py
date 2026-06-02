import csv
import json

import pytest

from benchmarks.craft.report import (
    ReportInputError,
    build_comparison_report,
    write_csv_report,
    write_json_report,
)


def test_report_rejects_missing_run(tmp_path):
    with pytest.raises(ReportInputError, match="Missing CRAFT summary"):
        build_comparison_report(["missing_run"], result_root=tmp_path)


def test_report_aggregates_multiple_runs(tmp_path):
    _write_run(
        tmp_path,
        "craft_official_baseline",
        condition="official_baseline",
        leakage_values=["True", "True"],
        use_state_manager=False,
    )
    _write_run(
        tmp_path,
        "craft_villageragent_qwen",
        condition="villageragent_directors",
        leakage_values=["True"],
        use_state_manager=True,
    )

    rows = build_comparison_report(
        ["craft_official_baseline", "craft_villageragent_qwen"],
        result_root=tmp_path,
    )
    assert [row["condition"] for row in rows] == [
        "official_baseline",
        "villageragent_directors",
    ]
    assert rows[0]["leakage_passed"] is True
    assert rows[1]["use_state_manager"] is True
    assert rows[1]["builder_fallback_count"] == 1
    assert rows[1]["builder_fallback_rate"] == 1.0
    assert rows[1]["mean_action_confidence"] == 0.6
    assert rows[1]["claim_support_count"] == 2
    assert rows[1]["gated_clarification_count"] == 1
    assert rows[1]["mean_risk_score"] == 0.4

    csv_path = tmp_path / "comparison_summary.csv"
    json_path = tmp_path / "comparison_summary.json"
    write_csv_report(rows, csv_path)
    write_json_report(rows, json_path)

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        csv_rows = list(csv.DictReader(f))
    assert len(csv_rows) == 2
    assert csv_rows[1]["run_name"] == "craft_villageragent_qwen"
    assert json.loads(json_path.read_text(encoding="utf-8"))["runs"][0]["condition"] == "official_baseline"


def test_report_marks_leakage_failure(tmp_path):
    _write_run(
        tmp_path,
        "craft_bad_run",
        condition="villageragent_directors",
        leakage_values=["True", "False"],
        use_state_manager=True,
    )
    rows = build_comparison_report(["craft_bad_run"], result_root=tmp_path)
    assert rows[0]["leakage_passed"] is False


def _write_run(tmp_path, run_name, *, condition, leakage_values, use_state_manager):
    normalized = tmp_path / run_name / "normalized"
    normalized.mkdir(parents=True)
    summary = {
        "run_name": run_name,
        "condition": condition,
        "num_games": len(leakage_values),
        "turns": 5,
        "mean_final_progress": 0.5,
        "completion_rate": 0.25,
        "models": {"director": "director-model", "builder": "builder-model"},
        "villageragent": {
            "use_task_decomposer": use_state_manager,
            "use_agent_controller": use_state_manager,
            "use_state_manager": use_state_manager,
        },
    }
    (normalized / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    with (normalized / "turns.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps({"builder_action": {"_builder_fallback": "test"}}) + "\n")
    with (normalized / "metrics.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "leakage_passed",
                "mean_action_confidence",
                "claim_support_count",
                "gated_clarification_count",
                "mean_risk_score",
            ],
        )
        writer.writeheader()
        for value in leakage_values:
            writer.writerow({
                "leakage_passed": value,
                "mean_action_confidence": "0.6",
                "claim_support_count": "2",
                "gated_clarification_count": "1",
                "mean_risk_score": "0.4",
            })
