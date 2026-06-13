FINAL_ANSWER_PREFIXES = ("PLACE:", "REMOVE:", "CLARIFY:")


def normalize_llm_content(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    return str(content).strip()


def validate_llm_output(content: str, *, require_final_answer: bool = False) -> dict:
    normalized = normalize_llm_content(content)
    diagnostics = {
        "content_empty": not bool(normalized),
        "content_chars": len(normalized),
        "malformed_final_answer": False,
        "validation_errors": [],
    }
    if not normalized:
        diagnostics["validation_errors"].append("empty_content")
    if require_final_answer and normalized:
        first_line = next((line.strip() for line in normalized.splitlines() if line.strip()), "")
        if not first_line.startswith(FINAL_ANSWER_PREFIXES):
            diagnostics["malformed_final_answer"] = True
            diagnostics["validation_errors"].append("malformed_final_answer")
    return diagnostics


def response_attempt_info(
    *,
    content,
    reasoning="",
    finish_reason=None,
    usage=None,
    require_final_answer: bool = False,
) -> dict:
    normalized = normalize_llm_content(content)
    reasoning_text = normalize_llm_content(reasoning)
    diagnostics = validate_llm_output(
        normalized,
        require_final_answer=require_final_answer,
    )
    return {
        "content": normalized,
        "content_chars": diagnostics["content_chars"],
        "reasoning_chars": len(reasoning_text),
        "finish_reason": finish_reason,
        "usage": usage,
        "validation": diagnostics,
    }
