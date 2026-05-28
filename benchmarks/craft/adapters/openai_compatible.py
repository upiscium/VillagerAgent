from openai import OpenAI


class OpenAICompatibleClient:
    def __init__(self, *, base_url: str, api_key: str):
        self.client = OpenAI(base_url=base_url, api_key=api_key)

    def chat(self, messages, *, model, temperature=0.0, max_tokens=None, stop=None):
        kwargs = {}
        if "qwen3" in model.lower():
            kwargs["extra_body"] = {"enable_thinking": False}
        response = self.client.chat.completions.create(
            model=model,
            messages=list(messages),
            temperature=temperature,
            max_tokens=max_tokens,
            stop=stop,
            **kwargs,
        )
        return response.choices[0].message.content or ""
