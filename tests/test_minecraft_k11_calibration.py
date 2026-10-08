from benchmarks.minecraft.k11_calibration import measure_inprocess_overhead


def test_k11_calibration_is_diagnostic_and_trace_valid() -> None:
    result = measure_inprocess_overhead(iterations=10)

    assert result["prevalence_inference_allowed"] is False
    assert result["network_effects_used"] is False
    assert result["audit_path_used"] is False
    assert result["iterations_per_condition"] == 10
    assert result["baseline"]["total"]["count"] == 10
    assert result["traced"]["total"]["count"] == 10
    assert result["traced"]["prepare_to_decision_marker"]["count"] == 10
    assert result["traced"]["trace_validation"]["valid"] is True
