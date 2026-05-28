from benchmarks.craft.craft_protocol import CraftPrivateView, CraftPublicState
from benchmarks.craft.villager.controller_adapter import VillagerCraftControllerAdapter


class VillagerCraftDirectorGroup:
    def __init__(
        self,
        *,
        villager_config: dict,
        llm_config: dict,
        num_directors: int = 3,
    ):
        if num_directors != 3:
            raise ValueError("CRAFT integration expects exactly three directors.")
        self.controller = VillagerCraftControllerAdapter(villager_config, llm_config)

    def reset(self, craft_task_info: dict) -> None:
        self.controller.reset(craft_task_info)

    def generate_director_messages(
        self,
        *,
        private_views: dict[str, CraftPrivateView],
        public_state: CraftPublicState,
        turn_index: int,
    ) -> dict[str, str]:
        if public_state.turn_index != turn_index:
            raise ValueError("turn_index must match public_state.turn_index")
        outputs = self.controller.step(private_views, public_state)
        return {output.director_id: output.public_message for output in outputs}
