from benchmarks.craft.adapters.ollama_client import OllamaNativeClient


def test_ollama_native_client_sends_think_false_and_records_metadata(monkeypatch):
    calls = []

    def fake_post(url, *, json, timeout):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return _FakeResponse({
            "message": {"content": "PLACE:ys:(0,0):0:CONFIRM:ok", "thinking": ""},
            "done_reason": "stop",
            "prompt_eval_count": 3,
            "eval_count": 2,
        })

    monkeypatch.setattr("benchmarks.craft.adapters.ollama_client.requests.post", fake_post)
    client = OllamaNativeClient(base_url="http://ollama", think=False)

    content = client.chat(
        [{"role": "user", "content": "Return OK"}],
        model="qwen3.5:9b",
        temperature=0.0,
        max_tokens=100,
    )

    assert content == "PLACE:ys:(0,0):0:CONFIRM:ok"
    assert calls[0]["url"] == "http://ollama/api/chat"
    assert calls[0]["json"]["think"] is False
    assert calls[0]["json"]["options"]["num_predict"] == 100
    assert client.last_response_info["content_empty"] is False
    assert client.last_response_info["malformed_final_answer"] is False
    assert client.last_response_info["attempts"][0]["usage"]["total_tokens"] == 5


def test_ollama_native_client_records_empty_and_malformed_metadata(monkeypatch):
    def fake_post(url, *, json, timeout):
        return _FakeResponse({
            "message": {"content": "natural language", "thinking": ""},
            "done_reason": "stop",
        })

    monkeypatch.setattr("benchmarks.craft.adapters.ollama_client.requests.post", fake_post)
    client = OllamaNativeClient(base_url="http://ollama", think=False)

    content = client.chat(
        [{"role": "user", "content": "Return final answer"}],
        model="qwen3.5:9b",
    )

    assert content == "natural language"
    assert client.last_response_info["content_empty"] is False
    assert client.last_response_info["malformed_final_answer"] is True
    assert client.last_response_info["validation_errors"] == ["malformed_final_answer"]


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload
