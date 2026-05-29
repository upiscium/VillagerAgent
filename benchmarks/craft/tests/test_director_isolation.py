from benchmarks.craft.craft_protocol import CraftPrivateView, CraftPublicState
from benchmarks.craft.villager.prompt_builder import build_director_prompt
from benchmarks.craft.villager.state_translator import craft_private_view_to_agent_state
from benchmarks.craft.villager.villager_craft_agent import VillagerCraftDirectorGroup


class StubClient:
    def chat(self, messages, *, model, temperature=0.0, max_tokens=None, stop=None):
        return "Builder, I can only speak from my own view."


def test_each_director_receives_only_own_private_view():
    public = CraftPublicState(1, [], [], {}, None)
    d1_view = CraftPrivateView("D1", "front", "D1_RAW_PRIVATE", "D1_PRIVATE_TEXT", {"v": "D1"})
    state = craft_private_view_to_agent_state(
        director_id="D1",
        private_view=d1_view,
        public_state=public,
        own_message_history=[],
    )
    prompt = build_director_prompt(
        director_id="D1",
        private_agent_state=state,
        public_state=public,
        task_objective="objective",
    )
    prompt_text = "\n".join(m["content"] for m in prompt)
    assert "D1_PRIVATE_TEXT" in prompt_text
    assert "D2_RAW_PRIVATE" not in prompt_text
    assert "D3_RAW_PRIVATE" not in prompt_text


def test_controller_returns_three_director_messages():
    group = VillagerCraftDirectorGroup(
        villager_config={"director_ids": ["D1", "D2", "D3"]},
        llm_config={"provider": "openai_compatible", "base_url": "http://unused", "api_key": "test", "model": "test"},
    )
    group.controller.llm_client = StubClient()
    public = CraftPublicState(1, [], [], {}, None)
    private_views = {
        did: CraftPrivateView(did, did, {}, f"{did} private", {})
        for did in ["D1", "D2", "D3"]
    }
    messages = group.generate_director_messages(
        private_views=private_views,
        public_state=public,
        turn_index=1,
    )
    assert set(messages) == {"D1", "D2", "D3"}
    assert all(messages.values())
    snapshot = group.controller.state_manager.snapshot_for_metadata()
    assert snapshot["stores_target_structure"] is False
    assert snapshot["stores_oracle_moves"] is False
    assert snapshot["private_state_agents"] == ["D1", "D2", "D3"]
