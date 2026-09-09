import os
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

OLLAMA_PROVIDER = "ollama"


def normalize_ollama_api_base(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Ollama API base must be an HTTP(S) origin without credentials")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Ollama API base has an invalid port") from exc
    path = parsed.path.rstrip("/")
    if path not in {"", "/v1"}:
        raise ValueError("Ollama API base path must be empty or /v1")
    return urlunsplit((parsed.scheme, parsed.netloc, "/v1", "", ""))


OLLAMA_API_BASE = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434/v1")
OLLAMA_API_BASE = normalize_ollama_api_base(OLLAMA_API_BASE)
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "ollama")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:12b")


def make_ollama_llm_config(
    api_model: str | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
    reasoning_effort: str | None = None,
) -> dict:
    selected_api_key = api_key or OLLAMA_API_KEY
    return {
        "provider": OLLAMA_PROVIDER,
        "api_key": selected_api_key,
        "api_base": normalize_ollama_api_base(api_base or OLLAMA_API_BASE),
        "api_model": api_model or OLLAMA_MODEL,
        "api_key_list": [selected_api_key],
        "reasoning_effort": reasoning_effort,
    }


def configure_ollama_agent(
    agent,
    api_model: str | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
) -> None:
    agent.provider = OLLAMA_PROVIDER
    agent.base_url = normalize_ollama_api_base(api_base or OLLAMA_API_BASE)
    agent.model = api_model or OLLAMA_MODEL
    agent.api_key_list = [api_key or OLLAMA_API_KEY]


def load_agent_api_key_list(path: str | Path = "API_KEY_LIST", key: str = "AGENT_KEY") -> list[str]:
    """Load legacy agent keys, or use the Ollama dummy key when absent.

    Ollama/OpenAI-compatible local deployments still require a non-empty key in
    the OpenAI client, but it does not need to be a billable OpenAI key.
    """
    key_path = Path(path)
    if not key_path.exists():
        return [OLLAMA_API_KEY]
    content = key_path.read_text(encoding="utf-8")
    if not content.strip():
        return [OLLAMA_API_KEY]
    payload = json.loads(content)
    keys = payload.get(key, []) if isinstance(payload, dict) else []
    if isinstance(keys, str):
        keys = [keys]
    if not keys:
        return [OLLAMA_API_KEY]
    return list(keys)
