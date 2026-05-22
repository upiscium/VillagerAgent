import os

OLLAMA_PROVIDER = "ollama"
OLLAMA_API_BASE = os.environ.get("OLLAMA_API_BASE", "https://ollama-melchior.arc.upiscium.dev/v1")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "ollama")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.6:27b")


def make_ollama_llm_config(api_model: str | None = None) -> dict:
    return {
        "provider": OLLAMA_PROVIDER,
        "api_key": OLLAMA_API_KEY,
        "api_base": OLLAMA_API_BASE,
        "api_model": api_model or OLLAMA_MODEL,
        "api_key_list": [OLLAMA_API_KEY],
    }


def configure_ollama_agent(agent, api_model: str | None = None) -> None:
    agent.provider = OLLAMA_PROVIDER
    agent.base_url = OLLAMA_API_BASE
    agent.model = api_model or OLLAMA_MODEL
    agent.api_key_list = [OLLAMA_API_KEY]
