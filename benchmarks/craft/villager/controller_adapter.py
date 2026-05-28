from benchmarks.craft.adapters.openai_compatible import OpenAICompatibleClient
from benchmarks.craft.adapters.ollama_client import OllamaNativeClient
from benchmarks.craft.craft_protocol import (
    CraftPrivateView,
    CraftPublicState,
    DirectorTurnOutput,
)
from benchmarks.craft.villager.prompt_builder import build_director_prompt
from benchmarks.craft.villager.state_translator import craft_private_view_to_agent_state
from benchmarks.craft.villager.task_translator import craft_task_to_villager_objective


class VillagerCraftControllerAdapter:
    def __init__(self, villager_config: dict, llm_config: dict):
        self.villager_config = villager_config
        self.llm_config = llm_config
        self.director_ids = villager_config.get("director_ids", ["D1", "D2", "D3"])
        self.own_message_history = {director_id: [] for director_id in self.director_ids}
        self.prompts_by_director = {}
        self.llm_client = self._make_llm_client(llm_config)

    def _make_llm_client(self, llm_config: dict):
        provider = llm_config.get("provider")
        if provider == "ollama_native":
            return OllamaNativeClient(base_url=llm_config["base_url"])
        if provider in {"openai", "openai_compatible", "ollama"}:
            return OpenAICompatibleClient(
                base_url=llm_config["base_url"],
                api_key=llm_config.get("api_key", "ollama"),
            )
        raise ValueError(f"Unsupported director LLM provider: {provider}")

    def reset(self, craft_task_info: dict) -> None:
        self.craft_task_info = dict(craft_task_info)
        self.own_message_history = {director_id: [] for director_id in self.director_ids}
        self.prompts_by_director = {}

    def step(
        self,
        private_views: dict[str, CraftPrivateView],
        public_state: CraftPublicState,
    ) -> list[DirectorTurnOutput]:
        outputs = []
        for director_id in self.director_ids:
            private_view = private_views[director_id]
            private_agent_state = craft_private_view_to_agent_state(
                director_id=director_id,
                private_view=private_view,
                public_state=public_state,
                own_message_history=self.own_message_history[director_id],
            )
            task_objective = craft_task_to_villager_objective(
                director_id=director_id,
                private_view=private_view,
                public_state=public_state,
            )
            prompt_messages = build_director_prompt(
                director_id=director_id,
                private_agent_state=private_agent_state,
                public_state=public_state,
                task_objective=task_objective,
            )
            self.prompts_by_director[director_id] = prompt_messages
            message = self.llm_client.chat(
                prompt_messages,
                model=self.llm_config["model"],
                temperature=self.llm_config.get("temperature", 0.0),
                max_tokens=self.llm_config.get("max_tokens"),
            ).strip()
            if not message:
                message = "I am uncertain based on my private view and the public history."
            output = DirectorTurnOutput(
                director_id=director_id,
                public_message=message,
                metadata={
                    "prompt_messages": prompt_messages,
                    "private_agent_state": private_agent_state,
                    "used_villageragent_components": {
                        "task_decomposer": self.villager_config.get("use_task_decomposer", False),
                        "agent_controller": self.villager_config.get("use_agent_controller", False),
                        "state_manager": self.villager_config.get("use_state_manager", False),
                    },
                },
            )
            self.own_message_history[director_id].append(
                {"turn_index": public_state.turn_index, "content": message}
            )
            outputs.append(output)

        if len(outputs) != 3:
            raise RuntimeError("CRAFT director adapter must return exactly three outputs.")
        return outputs
