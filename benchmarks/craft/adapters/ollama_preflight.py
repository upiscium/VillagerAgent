import socket
from urllib.parse import urlparse, urlunparse

import requests


class OllamaPreflightError(RuntimeError):
    """Raised when an Ollama endpoint is unavailable before a run starts."""


def preflight_ollama_model(*, base_url: str, model: str, timeout: float = 5.0) -> dict:
    root_url = _ollama_root_url(base_url)
    parsed = urlparse(root_url)
    host = parsed.hostname
    if not host:
        raise OllamaPreflightError(
            f"Ollama preflight failed for {model}: invalid base_url={base_url!r}."
        )

    try:
        socket.getaddrinfo(host, parsed.port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OllamaPreflightError(
            f"Ollama preflight DNS failure for {model} at {root_url}: {exc}."
        ) from exc

    tags_url = f"{root_url}/api/tags"
    try:
        response = requests.get(tags_url, timeout=timeout)
        response.raise_for_status()
    except requests.exceptions.Timeout as exc:
        raise OllamaPreflightError(
            f"Ollama preflight connectivity timeout for {model} at {tags_url}."
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise OllamaPreflightError(
            f"Ollama preflight connectivity failure for {model} at {tags_url}: {exc}."
        ) from exc
    except requests.exceptions.HTTPError as exc:
        status = getattr(exc.response, "status_code", response.status_code)
        raise OllamaPreflightError(
            f"Ollama preflight HTTP failure for {model} at {tags_url}: status={status}."
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise OllamaPreflightError(
            f"Ollama preflight request failure for {model} at {tags_url}: {exc}."
        ) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise OllamaPreflightError(
            f"Ollama preflight invalid JSON for {model} at {tags_url}."
        ) from exc

    available_models = sorted(
        item.get("name") or item.get("model")
        for item in payload.get("models", [])
        if isinstance(item, dict) and (item.get("name") or item.get("model"))
    )
    if model not in available_models:
        preview = ", ".join(available_models[:10]) or "none"
        raise OllamaPreflightError(
            f"Ollama preflight model unavailable at {root_url}: requested={model!r}, available={preview}."
        )

    return {
        "provider": "ollama",
        "base_url": root_url,
        "model": model,
        "available_model_count": len(available_models),
    }


def _ollama_root_url(base_url: str) -> str:
    parsed = urlparse(base_url.rstrip("/"))
    path = parsed.path.rstrip("/")
    if path == "/v1":
        path = ""
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", "")).rstrip("/")
