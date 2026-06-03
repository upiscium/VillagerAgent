import re

from benchmarks.craft.craft_protocol import CraftPrivateView, CraftPublicState
from benchmarks.craft.dual_dag.epistemic import EpistemicNode, Provenance, nodes_to_dicts


SIZE_LABELS = {1: "small", 2: "large"}
RELATIVE_VERTICAL = {"row_0": "bottom", "row_1": "middle", "row_2": "top"}
RELATIVE_HORIZONTAL = {0: "left", 1: "middle", 2: "right"}


def empty_epistemic_metadata() -> dict:
    return {
        "observed_facts": [],
        "public_facts": [],
        "reported_claims": [],
        "hypotheses": [],
        "edges": [],
    }


def epistemic_metadata_for_director(
    *,
    director_id: str,
    turn_index: int,
    private_view: CraftPrivateView,
    public_state: CraftPublicState,
) -> dict:
    return {
        "observed_facts": nodes_to_dicts(observed_facts_from_private_view(
            director_id=director_id,
            turn_index=turn_index,
            private_view=private_view,
        )),
        "public_facts": nodes_to_dicts(public_facts_from_state(
            turn_index=turn_index,
            public_state=public_state,
        )),
        "reported_claims": [],
        "hypotheses": [],
        "edges": [],
    }


def observed_facts_from_private_view(
    *,
    director_id: str,
    turn_index: int,
    private_view: CraftPrivateView,
) -> list[EpistemicNode]:
    nodes = []
    structured = private_view.structured_view or {}
    for row_key in sorted(structured):
        row = structured.get(row_key)
        if not isinstance(row, list):
            continue
        for column_index, cell in enumerate(row):
            if not isinstance(cell, dict):
                continue
            color = cell.get("color")
            size = cell.get("size")
            if color is None and size is None:
                continue
            nodes.append(EpistemicNode(
                node_id=f"observed:{director_id}:{turn_index}:{row_key}:{column_index}",
                node_type="observed_fact",
                content={
                    "director_id": director_id,
                    "row": row_key,
                    "column": column_index,
                    "relative_vertical": RELATIVE_VERTICAL.get(row_key, row_key),
                    "relative_horizontal": RELATIVE_HORIZONTAL.get(column_index, str(column_index)),
                    "color": color,
                    "size": size,
                    "size_label": SIZE_LABELS.get(size, str(size) if size is not None else "unknown"),
                },
                confidence=1.0,
                provenance=Provenance(
                    source="private_view",
                    director_id=director_id,
                    turn_index=turn_index,
                    visibility="private",
                ),
            ))
    return nodes


def public_facts_from_state(
    *,
    turn_index: int,
    public_state: CraftPublicState,
) -> list[EpistemicNode]:
    nodes = []
    for index, action in enumerate(public_state.builder_actions):
        if not isinstance(action, dict):
            continue
        public_action = {
            key: value
            for key, value in action.items()
            if not key.startswith("_")
        }
        nodes.append(EpistemicNode(
            node_id=f"public:builder_action:{turn_index}:{index}",
            node_type="public_fact",
            content={"builder_action": public_action},
            confidence=1.0,
            provenance=Provenance(
                source="builder_action",
                director_id=None,
                turn_index=turn_index,
                visibility="public",
            ),
        ))
    return nodes


def reported_claim_from_message(
    *,
    director_id: str,
    turn_index: int,
    message: str,
) -> dict:
    return EpistemicNode(
        node_id=f"claim:{director_id}:{turn_index}",
        node_type="reported_claim",
        content={
            "director_id": director_id,
            "message": message,
            "keywords": _message_keywords(message),
            "uncertain": _message_has_uncertainty(message),
        },
        confidence=0.7 if message.strip() else 0.0,
        provenance=Provenance(
            source="director_message",
            director_id=director_id,
            turn_index=turn_index,
            visibility="public",
        ),
    ).to_dict()


def _message_keywords(message: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9_()]+", message.lower())
    keep = {
        "yellow", "blue", "orange", "green", "red",
        "small", "large", "bottom", "middle", "top",
        "left", "right", "place", "remove",
        "ys", "bs", "os", "gs", "rs", "yl", "bl", "ol", "gl", "rl",
    }
    return [word for word in words if word in keep]


def _message_has_uncertainty(message: str) -> bool:
    lowered = message.lower()
    markers = (
        "uncertain",
        "cannot confirm",
        "can't confirm",
        "please confirm",
        "not sure",
        "unsure",
    )
    return any(marker in lowered for marker in markers)
