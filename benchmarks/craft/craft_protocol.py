from dataclasses import dataclass
from typing import Any


@dataclass
class CraftPrivateView:
    director_id: str
    view_name: str
    raw_view: Any
    text_view: str
    structured_view: dict


@dataclass
class CraftPublicState:
    turn_index: int
    public_messages: list[dict]
    builder_actions: list[dict]
    visible_constructed_structure: dict
    progress_summary: dict | None


@dataclass
class PrivateAgentState:
    agent_id: str
    private_view_text: str
    private_view_structured: dict
    own_message_history: list[dict]


@dataclass
class PublicCoordinationState:
    turn_index: int
    public_messages: list[dict]
    builder_actions: list[dict]
    visible_constructed_structure: dict
    progress_summary: dict | None


@dataclass
class DirectorTurnOutput:
    director_id: str
    public_message: str
    metadata: dict
