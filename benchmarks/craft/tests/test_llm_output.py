from benchmarks.craft.adapters.llm_output import (
    normalize_llm_content,
    response_attempt_info,
    validate_llm_output,
)


def test_normalize_llm_content_strips_and_handles_none():
    assert normalize_llm_content("  PLACE:ys:(0,0):0  ") == "PLACE:ys:(0,0):0"
    assert normalize_llm_content(None) == ""


def test_validate_llm_output_reports_empty_content():
    diagnostics = validate_llm_output("", require_final_answer=True)

    assert diagnostics["content_empty"] is True
    assert diagnostics["malformed_final_answer"] is False
    assert diagnostics["validation_errors"] == ["empty_content"]


def test_validate_llm_output_reports_malformed_final_answer():
    diagnostics = validate_llm_output("I would place yellow", require_final_answer=True)

    assert diagnostics["content_empty"] is False
    assert diagnostics["malformed_final_answer"] is True
    assert diagnostics["validation_errors"] == ["malformed_final_answer"]


def test_response_attempt_info_uses_common_validation_shape():
    info = response_attempt_info(
        content="  CLARIFY:Need color. ",
        reasoning="thinking",
        finish_reason="stop",
        usage={"total_tokens": 3},
        require_final_answer=True,
    )

    assert info["content"] == "CLARIFY:Need color."
    assert info["reasoning_chars"] == len("thinking")
    assert info["validation"] == {
        "content_empty": False,
        "content_chars": len("CLARIFY:Need color."),
        "malformed_final_answer": False,
        "validation_errors": [],
    }
