from benchmarks.craft.adapters.openai_compatible import OpenAICompatibleClient


def test_client_retries_empty_content_with_reasoning():
    client = OpenAICompatibleClient(base_url="http://unused", api_key="test")
    client.client = _FakeOpenAI([
        _FakeResponse(content="", reasoning="thinking", finish_reason="length"),
        _FakeResponse(content="final public message", reasoning="", finish_reason="stop"),
    ])

    content = client.chat(
        [{"role": "user", "content": "Say something"}],
        model="qwen3.5:9b",
        max_tokens=10,
    )

    assert content == "final public message"
    assert client.last_response_info["content_empty"] is False
    assert client.last_response_info["malformed_final_answer"] is True
    assert client.last_response_info["validation_errors"] == ["malformed_final_answer"]
    assert len(client.last_response_info["attempts"]) == 2
    assert client.client.chat.completions.calls[1]["max_tokens"] == 4096


def test_client_records_empty_response_metadata():
    client = OpenAICompatibleClient(base_url="http://unused", api_key="test")
    client.client = _FakeOpenAI([
        _FakeResponse(content="", reasoning="", finish_reason="stop"),
    ])

    content = client.chat(
        [{"role": "user", "content": "Say something"}],
        model="gemma4:e4b",
        max_tokens=10,
    )

    assert content == ""
    assert client.last_response_info["content_empty"] is True
    assert client.last_response_info["validation_errors"] == ["empty_content"]
    assert len(client.last_response_info["attempts"]) == 1


def test_client_records_valid_final_answer_metadata():
    client = OpenAICompatibleClient(base_url="http://unused", api_key="test")
    client.client = _FakeOpenAI([
        _FakeResponse(content="PLACE:ys:(0,0):0:CONFIRM:ok", reasoning="", finish_reason="stop"),
    ])

    content = client.chat(
        [{"role": "user", "content": "Place something"}],
        model="gemma4:e4b",
        max_tokens=10,
    )

    assert content == "PLACE:ys:(0,0):0:CONFIRM:ok"
    assert client.last_response_info["malformed_final_answer"] is False
    assert client.last_response_info["validation_errors"] == []


class _FakeOpenAI:
    def __init__(self, responses):
        self.chat = _FakeChat(responses)


class _FakeChat:
    def __init__(self, responses):
        self.completions = _FakeCompletions(responses)


class _FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class _FakeResponse:
    def __init__(self, *, content, reasoning, finish_reason):
        self.choices = [_FakeChoice(content, reasoning, finish_reason)]
        self.usage = {"total_tokens": 1}


class _FakeChoice:
    def __init__(self, content, reasoning, finish_reason):
        self.message = _FakeMessage(content, reasoning)
        self.finish_reason = finish_reason


class _FakeMessage:
    def __init__(self, content, reasoning):
        self.content = content
        self.reasoning = reasoning
