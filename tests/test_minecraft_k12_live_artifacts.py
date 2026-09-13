from benchmarks.minecraft.k12_live_artifacts import LiveArtifact

def test_artifact_is_non_promotable_and_reports_remaining_slots():
    artifact = LiveArtifact("qualification", ({"status":"not_started"},))
    assert artifact.rejected_by_final_gates and artifact.remaining_slots == 89
