import csv
import json

import pytest

from benchmarks.craft.trace_compare import TraceCompareError, compare_runs, write_csv, write_json


def test_compare_runs_detects_zero_progress_streak_and_divergence(tmp_path):
    _write_run(
        tmp_path,
        "baseline",
        final_progress=0.8,
        progress_auc=0.5,
        fallback_count=2,
        actions=[
            ("place", "ys", "(0,0)", 0, 0.1),
            ("place", "bs", "(0,1)", 0, 0.0),
            ("place", "bs", "(0,1)", 0, None),
            ("remove", "bs", "(0,1)", 0, -0.1),
        ],
    )
    _write_run(
        tmp_path,
        "variant",
        final_progress=0.7,
        progress_auc=0.4,
        fallback_count=1,
        actions=[
            ("place", "ys", "(0,0)", 0, 0.1),
            ("place", "gs", "(0,1)", 0, 0.0),
            ("place", "gs", "(0,1)", 0, None),
            ("place", "gs", "(0,1)", 0, None),
        ],
    )

    report = compare_runs("baseline", "variant", result_root=tmp_path)

    assert report["structure_count"] == 1
    assert report["mean_delta_final_progress"] == pytest.approx(-0.1)
    row = report["rows"][0]
    assert row["delta_final_progress"] == pytest.approx(-0.1)
    assert row["baseline_repeated_zero_progress_streak_max"] == 2
    assert row["variant_repeated_zero_progress_streak_max"] == 3
    assert row["baseline_negative_progress_turn_count"] == 1
    assert row["variant_negative_progress_turn_count"] == 0
    assert row["first_action_divergence_turn"] == 2
    assert row["variant_final_action"] == "place:gs:(0,1):0:"
    assert row["variant_final_progress_delta"] is None


def test_compare_runs_rejects_missing_inputs(tmp_path):
    with pytest.raises(TraceCompareError, match="Missing CRAFT metrics"):
        compare_runs("missing", "also_missing", result_root=tmp_path)


def test_trace_compare_writes_reports(tmp_path):
    _write_run(tmp_path, "baseline", final_progress=0.4, progress_auc=0.2, fallback_count=0)
    _write_run(tmp_path, "variant", final_progress=0.5, progress_auc=0.3, fallback_count=0)
    report = compare_runs("baseline", "variant", result_root=tmp_path)
    csv_path = tmp_path / "trace.csv"
    json_path = tmp_path / "trace.json"

    write_csv(report["rows"], csv_path)
    write_json(report, json_path)

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["structure_id"] == "0"
    assert json.loads(json_path.read_text(encoding="utf-8"))["structure_count"] == 1


def _write_run(
    tmp_path,
    run_name,
    *,
    final_progress,
    progress_auc,
    fallback_count,
    actions=None,
):
    normalized = tmp_path / run_name / "normalized"
    normalized.mkdir(parents=True)
    with (normalized / "metrics.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["structure_id", "final_progress", "progress_auc", "fallback_count"],
        )
        writer.writeheader()
        writer.writerow({
            "structure_id": "0",
            "final_progress": str(final_progress),
            "progress_auc": str(progress_auc),
            "fallback_count": str(fallback_count),
        })
    actions = actions or [("place", "ys", "(0,0)", 0, 0.1)]
    with (normalized / "turns.jsonl").open("w", encoding="utf-8") as f:
        for index, (action, block, position, layer, delta) in enumerate(actions, start=1):
            f.write(
                json.dumps({
                    "structure_id": 0,
                    "turn_index": index,
                    "builder_action": {
                        "action": action,
                        "block": block,
                        "position": position,
                        "layer": layer,
                    },
                    "progress": {"progress_delta": delta},
                })
                + "\n"
            )
