from benchmarks.craft.craft_protocol import CraftPrivateView, CraftPublicState


def craft_task_to_villager_objective(
    *,
    director_id: str,
    private_view: CraftPrivateView,
    public_state: CraftPublicState,
) -> str:
    return (
        f"You are Director {director_id} in a partial-information 3D construction task.\n"
        "You can only see your assigned 2D projection of the hidden target structure.\n"
        "You must coordinate with the other directors through public natural language messages.\n"
        "Do not claim information that is not visible from your view or public history.\n"
        "Help the builder construct the target structure by giving concise, spatially grounded instructions.\n"
        f"Your current private view is named {private_view.view_name}.\n"
        f"The current public turn index is {public_state.turn_index}."
    )
