from model.google_model import GoogleLanguageModel
from model.openai_models import OpenAILanguageModel
from model.zhipu_model import ZhipuLanguageModel
from model.huggingface_model import HFLanguageModel
from model.vllm_model import VLLMLanguageModel


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def _openai_compatible_args(args: dict) -> dict:
    return _drop_none({
        "api_key": args.get("api_key", None),
        "api_model": args.get("api_model", None),
        "api_base": args.get("api_base", None),
        "evaluation_strategy": args.get("evaluation_strategy", None),
        "enable_ReAct_prompting": args.get("enable_ReAct_prompting", None),
        "strategy": args.get("strategy", None),
        "role_name": args.get("role_name", None),
        "api_key_list": args.get("api_key_list", None),
        "runtime_paths": args.get("runtime_paths", None),
        "reasoning_effort": args.get("reasoning_effort", None),
    })


def _gemini_args(args: dict, api_model: str) -> dict:
    return _drop_none({
        "api_key": args.get("api_key", None),
        "api_model": api_model,
        "role_name": args.get("role_name", None),
        "api_key_list": args.get("api_key_list", None),
    })


def _zhipu_args(args: dict, api_model: str) -> dict:
    return _drop_none({
        "api_key": args.get("api_key", None),
        "api_model": api_model,
        "role_name": args.get("role_name", None),
        "api_key_list": args.get("api_key_list", None),
    })


def _vllm_args(args: dict, api_model: str) -> dict:
    return _drop_none({
        "api_key": args.get("api_key", None),
        "api_model": api_model,
        "api_base": args.get("api_base", None),
        "role_name": args.get("role_name", None),
    })


def init_language_model(args: dict):
    provider = (args.get("provider", "") or "").lower()
    api_model = args.get("api_model", "")

    if provider in {"openai", "ollama", "openai_compatible"}:
        if provider == "ollama":
            args.setdefault("api_key", "ollama")
            args.setdefault("api_key_list", ["ollama"])
        return OpenAILanguageModel(**_openai_compatible_args(args))

    if provider == "gemini":
        return GoogleLanguageModel(**_gemini_args(args, api_model))

    if provider == "zhipu":
        return ZhipuLanguageModel(**_zhipu_args(args, api_model))

    if provider == "vllm":
        new_args = _vllm_args(args, api_model)
        print(f"Loading VLLM model with args: {new_args}")
        return VLLMLanguageModel(**new_args)

    # Backward compatibility: provider unspecified uses the original model-name routing.
    if ("gpt" in api_model and "llama" not in api_model) or "qwen" in api_model or "deepseek" in api_model or "default" in api_model:
        return OpenAILanguageModel(**_openai_compatible_args(args))

    elif "gemini" in api_model:
        return GoogleLanguageModel(**_gemini_args(args, api_model))

    elif "glm" in api_model:
        return ZhipuLanguageModel(**_zhipu_args(args, api_model))

    elif "vllm" in api_model or "llama" in api_model or "NAS" in api_model:
        new_args = _vllm_args(args, api_model)
        print(f"Loading VLLM model with args: {new_args}")
        return VLLMLanguageModel(**new_args)

    else:
        return HFLanguageModel(**_drop_none({
            "api_key": args.get("api_key", None),
            "model_tokenizer": args.get("model_tokenizer", None),
            "verbose": args.get("verbose", None),
            "api_key_list": args.get("api_key_list", None),
        }))
