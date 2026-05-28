from benchmarks.craft.craft_protocol import CraftPrivateView, CraftPublicState
from benchmarks.craft.villager.state_translator import craft_private_view_to_agent_state


def test_state_translator_uses_only_private_and_public_state():
    private = CraftPrivateView("D1", "front", {"secret": "D1"}, "D1 only", {"cell": "blue"})
    public = CraftPublicState(1, [], [], {"(0,0)": []}, {"current": 0.0})
    state = craft_private_view_to_agent_state(
        director_id="D1",
        private_view=private,
        public_state=public,
        own_message_history=[],
    )
    assert state["agent_id"] == "D1"
    assert state["private_observation"]["text"] == "D1 only"
    assert "target_structure" not in str(state)
    assert "oracle" not in str(state).lower()
