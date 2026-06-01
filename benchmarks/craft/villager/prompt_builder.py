import json

from benchmarks.craft.craft_protocol import CraftPublicState


def _json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _craft_coordinate_guide(director_id: str) -> str:
    return "\n".join([
        "CRAFT coordinate and view guide:",
        "- Your private view is a 2D projection of one wall of the hidden 3D target.",
        "- row_0, row_1, row_2 are vertical stack layers from bottom to top in your view.",
        "- Each row has three visible cells from your left to your right.",
        "- Describe visible cells using relative terms such as bottom/middle/top and left/middle/right.",
        "- Mention block color and size when visible; size 1 means small, size 2 means large.",
        "- Do not claim coordinates or cells outside your own projection unless they are public builder actions.",
        "- D1 sees the left wall; D2 sees the back/top wall; D3 sees the right wall.",
        f"- You are {director_id}; speak only from {director_id}'s private projection and public history.",
    ])


def build_director_prompt(
    *,
    director_id: str,
    private_agent_state: dict,
    public_state: CraftPublicState,
    task_objective: str,
) -> list[dict]:
    private_observation = private_agent_state["private_observation"]
    system = (
        "You are one of three directors. "
        "You only know your private view and public messages. "
        "Do not invent unseen 3D details. "
        "Communicate uncertainty explicitly. "
        "Your output must be a message to the Builder."
    )
    user = "\n".join([
        task_objective,
        "",
        f"Director ID: {director_id}",
        f"Current turn index: {public_state.turn_index}",
        "",
        _craft_coordinate_guide(director_id),
        "",
        "Own private view text:",
        private_observation.get("text", ""),
        "",
        "Own private view structured data:",
        _json_text(private_observation.get("structured", {})),
        "",
        "Public messages so far:",
        _json_text(public_state.public_messages),
        "",
        "Builder actions so far:",
        _json_text(public_state.builder_actions),
        "",
        "Visible constructed structure:",
        _json_text(public_state.visible_constructed_structure),
        "",
        "Visible progress summary:",
        _json_text(public_state.progress_summary),
        "",
        "Write only your next public message to the Builder. Prefer concise, grounded instructions that name visible block color, size, relative cell, and whether you are uncertain.",
    ])
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
