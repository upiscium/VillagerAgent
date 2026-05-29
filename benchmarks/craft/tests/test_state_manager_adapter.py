import pytest

from benchmarks.craft.craft_protocol import PrivateAgentState, PublicCoordinationState
from benchmarks.craft.villager.state_manager_adapter import (
    CraftStateManagerAdapter,
    PartialInformationStateError,
)


def test_state_manager_keeps_private_states_isolated():
    manager = CraftStateManagerAdapter(["D1", "D2", "D3"])
    manager.update_public_state(PublicCoordinationState(
        turn_index=1,
        public_messages=[],
        builder_actions=[],
        visible_constructed_structure={"(0,0)": []},
        progress_summary={"current": 0.0},
    ))
    manager.update_private_state(PrivateAgentState(
        agent_id="D1",
        private_view_text="D1_PRIVATE_ONLY",
        private_view_structured={"d1": "view"},
        own_message_history=[],
    ))
    manager.update_private_state(PrivateAgentState(
        agent_id="D2",
        private_view_text="D2_PRIVATE_ONLY",
        private_view_structured={"d2": "view"},
        own_message_history=[],
    ))

    d1_state = manager.get_private_state("D1")
    snapshot = manager.snapshot_for_metadata()
    assert d1_state.private_view_text == "D1_PRIVATE_ONLY"
    assert "D2_PRIVATE_ONLY" not in str(d1_state)
    assert snapshot["stores_target_structure"] is False
    assert snapshot["stores_oracle_moves"] is False
    assert snapshot["stores_all_private_views"] is False


def test_state_manager_rejects_hidden_keys():
    manager = CraftStateManagerAdapter(["D1", "D2", "D3"])
    with pytest.raises(PartialInformationStateError):
        manager.update_public_state(PublicCoordinationState(
            turn_index=1,
            public_messages=[],
            builder_actions=[{"oracle_moves": [{"action": "place"}]}],
            visible_constructed_structure={},
            progress_summary=None,
        ))

    with pytest.raises(PartialInformationStateError):
        manager.update_private_state(PrivateAgentState(
            agent_id="D1",
            private_view_text="private",
            private_view_structured={"target_structure": {"(0,0)": ["ys"]}},
            own_message_history=[],
        ))
