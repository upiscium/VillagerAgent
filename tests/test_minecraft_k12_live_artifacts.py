from benchmarks.minecraft.k12_live_artifacts import LiveArtifact

def test_artifact_is_non_promotable_and_reports_remaining_slots():
    artifact = LiveArtifact("qualification", ({"status":"not_started"},))
    assert artifact.rejected_by_final_gates and artifact.remaining_slots == 89
    assert not artifact.promotable
    assert artifact.execution_provenance == "mock_only"
    assert artifact.evidence_origin == "test_only"


def test_artifact_cannot_clear_its_non_promotion_invariant():
    artifact = LiveArtifact("final", (), rejected_by_final_gates=False)
    assert artifact.rejected_by_final_gates
    assert not artifact.final_launch_ready()
