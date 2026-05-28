import requests


class OllamaNativeClient:
    def __init__(self, *, base_url: str):
        self.base_url = base_url.rstrip("/")

    def chat(self, messages, *, model, temperature=0.0, max_tokens=None, stop=None):
        payload = {
            "model": model,
            "messages": list(messages),
            "stream": False,
            "options": {
                "temperature": temperature,
            },
        }
        if max_tokens is not None:
            payload["options"]["num_predict"] = max_tokens
        if stop is not None:
            payload["options"]["stop"] = list(stop)

        response = requests.post(
            f"{self.base_url}/api/chat",
            json=payload,
            timeout=120,
        )
        response.raise_for_status()
        return response.json()["message"]["content"]
