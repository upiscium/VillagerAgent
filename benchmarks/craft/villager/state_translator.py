from benchmarks.craft.craft_protocol import CraftPrivateView, CraftPublicState


def craft_private_view_to_agent_state(
    *,
    director_id: str,
    private_view: CraftPrivateView,
    public_state: CraftPublicState,
    own_message_history: list[dict],
) -> dict:
    if private_view.director_id != director_id:
        raise ValueError(
            f"private_view.director_id={private_view.director_id} does not match {director_id}"
        )

    return {
        "agent_id": director_id,
        "role": "craft_director",
        "private_observation": {
            "view_name": private_view.view_name,
            "text": private_view.text_view,
            "structured": private_view.structured_view,
        },
        "own_message_history": list(own_message_history),
        "public_coordination_state": {
            "turn_index": public_state.turn_index,
            "messages": list(public_state.public_messages),
            "builder_actions": list(public_state.builder_actions),
            "visible_constructed_structure": public_state.visible_constructed_structure,
            "progress_summary": public_state.progress_summary,
        },
    }
