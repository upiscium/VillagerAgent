BASE_HIDDEN_STATE_KEYS = frozenset({
    "target_structure",
    "oracle_moves",
    "all_private_views",
    "raw_private_view",
    "hidden_spans",
    "hidden_labels",
})

OFFICIAL_RUNNER_HIDDEN_STATE_KEYS = BASE_HIDDEN_STATE_KEYS | frozenset({
    "target_spans",
    "internal_thinking",
})


def hidden_state_key_labels() -> list[str]:
    return sorted(BASE_HIDDEN_STATE_KEYS)


def official_runner_hidden_state_key_labels() -> list[str]:
    return sorted(OFFICIAL_RUNNER_HIDDEN_STATE_KEYS)
