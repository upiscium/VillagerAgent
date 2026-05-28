from typing import Mapping, Protocol, Sequence


class ChatLLMClient(Protocol):
    def chat(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        stop: Sequence[str] | None = None,
    ) -> str:
        ...
