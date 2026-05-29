import copy

from benchmarks.craft.craft_protocol import PrivateAgentState, PublicCoordinationState


FORBIDDEN_STATE_KEYS = {
    "target_structure",
    "oracle_moves",
    "all_private_views",
    "hidden_spans",
    "hidden_labels",
}


class PartialInformationStateError(ValueError):
    """Raised when hidden CRAFT state is passed into the VillagerAgent adapter."""


class CraftStateManagerAdapter:
    def __init__(self, director_ids: list[str]):
        self.director_ids = list(director_ids)
        self._private_states: dict[str, PrivateAgentState] = {}
        self._public_state: PublicCoordinationState | None = None

    def reset(self) -> None:
        self._private_states = {}
        self._public_state = None

    def update_private_state(self, state: PrivateAgentState) -> None:
        if state.agent_id not in self.director_ids:
            raise PartialInformationStateError(f"Unknown director id: {state.agent_id}")
        _reject_forbidden_payload(state.__dict__)
        self._private_states[state.agent_id] = copy.deepcopy(state)

    def update_public_state(self, state: PublicCoordinationState) -> None:
        _reject_forbidden_payload(state.__dict__)
        self._public_state = copy.deepcopy(state)

    def get_private_state(self, director_id: str) -> PrivateAgentState:
        return copy.deepcopy(self._private_states[director_id])

    def get_public_state(self) -> PublicCoordinationState:
        if self._public_state is None:
            raise PartialInformationStateError("Public state has not been initialized.")
        return copy.deepcopy(self._public_state)

    def snapshot_for_metadata(self) -> dict:
        return {
            "director_ids": list(self.director_ids),
            "private_state_agents": sorted(self._private_states.keys()),
            "public_turn_index": self._public_state.turn_index if self._public_state else None,
            "stores_target_structure": False,
            "stores_oracle_moves": False,
            "stores_all_private_views": False,
        }


def _reject_forbidden_payload(payload) -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in FORBIDDEN_STATE_KEYS:
                raise PartialInformationStateError(f"Forbidden hidden state key: {key}")
            _reject_forbidden_payload(value)
    elif isinstance(payload, list):
        for value in payload:
            _reject_forbidden_payload(value)
