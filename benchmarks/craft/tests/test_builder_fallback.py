from benchmarks.craft.craft_env_adapter import (
    _append_builder_action_contract,
    _builder_system_prompt,
    _format_candidate_response_line,
    _matches_oracle_candidate,
    _oracle_fallback_action,
)


def test_matches_oracle_candidate_requires_block_and_span_match():
    oracle_moves = [{
        "action": "place",
        "block": "bl",
        "position": "(1,0)",
        "layer": 0,
        "span_to": "(2,0)",
    }]

    assert _matches_oracle_candidate({
        "action": "place",
        "block": "bl",
        "position": "(1,0)",
        "layer": 0,
        "span_to": "(2,0)",
    }, oracle_moves)
    assert not _matches_oracle_candidate({
        "action": "place",
        "block": "yl",
        "position": "(1,0)",
        "layer": 0,
        "span_to": None,
    }, oracle_moves)


def test_oracle_fallback_preserves_diagnostics():
    fallback = _oracle_fallback_action(
        oracle_moves=[{"action": "place", "block": "bl", "position": "(1,0)", "layer": 0}],
        response_info={"content_empty": False},
        first_line="PLACE:yl:(0,0):0:CONFIRM:bad",
        reason="oracle_first_candidate_after_non_candidate_response",
    )

    assert fallback["block"] == "bl"
    assert fallback["_builder_response_info"] == {"content_empty": False}
    assert fallback["_builder_raw_first_line"].startswith("PLACE:yl")
    assert fallback["_builder_fallback"] == "oracle_first_candidate_after_non_candidate_response"


def test_builder_contract_lists_exact_oracle_response_lines():
    oracle_moves = [
        {"action": "place", "block": "bl", "position": "(1,0)", "layer": 0, "span_to": "(2,0)"},
        {"action": "place", "block": "ys", "position": "(0,0)", "layer": 0, "span_to": None},
    ]

    prompt = _append_builder_action_contract("base prompt", oracle_moves)

    assert "CANDIDATE RESPONSE LINES" in prompt
    assert "PLACE:bl:(1,0):0:(2,0):CONFIRM:Choosing this verified candidate." in prompt
    assert "PLACE:ys:(0,0):0:CONFIRM:Choosing this verified candidate." in prompt
    assert "Do not output natural-language candidate descriptions" in prompt


def test_format_candidate_response_line_handles_remove_without_block_code():
    assert _format_candidate_response_line({
        "action": "remove",
        "position": "(1,2)",
        "layer": 0,
        "span_to": "(2,2)",
    }) == "REMOVE:(1,2):0:(2,2):CONFIRM:Choosing this verified candidate."


def test_builder_system_prompt_requires_copying_candidate_line():
    system = _builder_system_prompt([{"action": "place"}])
    assert "copied from the CANDIDATE RESPONSE LINES" in system
    assert "exactly one line" in system
