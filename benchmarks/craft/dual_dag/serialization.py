FORBIDDEN_SERIALIZED_KEYS = {
    "target_structure",
    "oracle_moves",
    "all_private_views",
    "raw_private_view",
    "hidden_spans",
    "hidden_labels",
}


def sanitize_for_serialization(value):
    if isinstance(value, dict):
        return {
            key: sanitize_for_serialization(item)
            for key, item in value.items()
            if key not in FORBIDDEN_SERIALIZED_KEYS
        }
    if isinstance(value, list):
        return [sanitize_for_serialization(item) for item in value]
    return value


def node_to_dict(node: dict) -> dict:
    return sanitize_for_serialization(node)


def edge_to_dict(edge: dict) -> dict:
    return sanitize_for_serialization(edge)


def snapshot_to_dict(runtime) -> dict:
    return sanitize_for_serialization(runtime.snapshot())
