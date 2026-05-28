import json

from benchmarks.craft.craft_protocol import CraftPublicState


def _json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


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
        "Write only your next public message to the Builder.",
    ])
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
