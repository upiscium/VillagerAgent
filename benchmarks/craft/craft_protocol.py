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
class DirectorTurnOutput:
    director_id: str
    public_message: str
    metadata: dict
