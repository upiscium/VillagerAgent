"""Development-only qualification for the K11 OpenAI-compatible model contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

from env.runtime_paths import RuntimePaths
from model.ollama_config import normalize_ollama_api_base
from model.openai_models import ModelOutputContractError, OpenAILanguageModel
from pipeline.task_prompt import PART_DECOMPOSE_SYSTEM_PROMPT, PART_DECOMPOSE_USER_PROMPT
from pipeline.utils import format_string


ARTIFACT_ID = "minecraft-k11-model-qualification"
REQUIRED_TAGS = ("description", "milestones", "assigned agents")
CONDITIONS = (
    {"condition_id": "stream-current", "stream": True, "max_tokens": 1024,
     "reasoning_effort": None},
    {"condition_id": "nonstream-current", "stream": False, "max_tokens": 1024,
     "reasoning_effort": None},
    {"condition_id": "stream-larger-budget", "stream": True, "max_tokens": 4096,
     "reasoning_effort": None},
    {"condition_id": "stream-reasoning-none", "stream": True, "max_tokens": 1024,
     "reasoning_effort": "none"},
)


class QualificationError(ValueError):
    pass


def _load_call_inputs(source_runtime_dir: Path) -> tuple[str, str, dict[str, Any]]:
    meta_path = source_runtime_dir / "cache" / "meta_setting.json"
    if not meta_path.is_file():
        raise QualificationError(f"missing source runtime metadata: {meta_path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    query_paths = sorted((source_runtime_dir / "result").glob("*/DM_query.json"))
    if len(query_paths) != 1:
        raise QualificationError("source runtime must contain exactly one DM_query.json")
    query = json.loads(query_paths[0].read_text(encoding="utf-8"))
    env_description = query.get("response")
    if (
        isinstance(env_description, list)
        and len(env_description) == 1
        and isinstance(env_description[0], str)
    ):
        env_description = env_description[0]
    task_goal = meta.get("task_goal")
    agent_num = meta.get("agent_num")
    if not isinstance(env_description, str):
        raise QualificationError("source runtime lacks an environment-description response")
    if not isinstance(task_goal, str) or not task_goal or type(agent_num) is not int:
        raise QualificationError("source runtime task metadata is invalid")
    query_prompts = query.get("prompt")
    if not (isinstance(query_prompts, list) and len(query_prompts) == 1
            and isinstance(query_prompts[0], str)):
        raise QualificationError("source runtime lacks the environment-query prompt")
    marker = f"*** The task *** : {task_goal}"
    suffix = ".\nReturn with Entity, Blocks, Creatures, Interactive-Items and Environment"
    prompt = query_prompts[0]
    if prompt.count(marker) != 1:
        raise QualificationError("source runtime environment-query task marker is ambiguous")
    sign_and_suffix = prompt.split(marker, 1)[1]
    if suffix not in sign_and_suffix:
        raise QualificationError("source runtime environment-query suffix is invalid")
    sign_info = sign_and_suffix.split(suffix, 1)[0]
    env_description = env_description + "\nSign info: " + sign_info
    user_prompt = format_string(PART_DECOMPOSE_USER_PROMPT, {
        "task": {"description": task_goal, "meta-data": {}},
        "env": env_description,
        "num": agent_num,
    })
    metadata = {
        "call_source": "TaskManager.init_task",
        "system_prompt_sha256": hashlib.sha256(
            PART_DECOMPOSE_SYSTEM_PROMPT.encode("utf-8")
        ).hexdigest(),
        "system_prompt_chars": len(PART_DECOMPOSE_SYSTEM_PROMPT),
        "user_prompt_sha256": hashlib.sha256(user_prompt.encode("utf-8")).hexdigest(),
        "user_prompt_chars": len(user_prompt),
        "required_tags": list(REQUIRED_TAGS),
        "json_check": True,
        "prompt_reconstruction_exact": True,
    }
    return PART_DECOMPOSE_SYSTEM_PROMPT, user_prompt, metadata


def _provider_version(api_origin: str) -> str | None:
    request = urllib.request.Request(api_origin.rstrip("/") + "/api/version")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            value = json.load(response)
    except Exception:
        return None
    version = value.get("version") if isinstance(value, Mapping) else None
    return version if isinstance(version, str) else None


def _latest_diagnostic(paths: RuntimePaths) -> dict[str, Any]:
    if not paths.openai_diagnostics.is_file():
        return {}
    rows = [line for line in paths.openai_diagnostics.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    return json.loads(rows[-1]) if rows else {}


def run_qualification(
    *, output_root: str | Path, source_runtime_dir: str | Path,
    api_origin: str, model: str,
) -> dict[str, Any]:
    root = Path(output_root).resolve()
    if root.exists():
        raise QualificationError(f"qualification output root already exists: {root}")
    source_runtime = Path(source_runtime_dir).resolve()
    system_prompt, user_prompt, call_profile = _load_call_inputs(source_runtime)
    root.mkdir(parents=True)
    api_base = normalize_ollama_api_base(api_origin)
    results = []
    for condition in CONDITIONS:
        paths = RuntimePaths.isolated(root / condition["condition_id"])
        language_model = OpenAILanguageModel(
            api_key=os.environ.get("OLLAMA_API_KEY", "ollama"),
            api_model=model,
            api_base=api_base,
            runtime_paths=paths,
            model_call_attempts=1,
            prompt_logging_enabled=False,
        )
        error_type = None
        error_category = None
        contract_valid = False
        try:
            content = language_model.few_shot_generate_thoughts(
                system_prompt,
                user_prompt,
                max_tokens=condition["max_tokens"],
                cache_enabled=False,
                check_tags=list(REQUIRED_TAGS),
                json_check=True,
                stream=condition["stream"],
                reasoning_effort=condition["reasoning_effort"],
            )
            contract_valid = bool(content)
            del content
        except ModelOutputContractError as exc:
            error_type = type(exc).__name__
            error_category = exc.category
        except Exception as exc:
            error_type = type(exc).__name__
        finally:
            close = getattr(language_model.client, "close", None)
            if callable(close):
                close()
        diagnostic = _latest_diagnostic(paths)
        results.append({
            **condition,
            "contract_valid": contract_valid,
            "error_type": error_type,
            "error_category": error_category,
            "provider_metadata": diagnostic,
        })

    qualified_condition = next(
        row for row in results if row["condition_id"] == "stream-reasoning-none"
    )
    artifact = {
        "artifact_id": ARTIFACT_ID,
        "artifact_version": 1,
        "development_only": True,
        "formal_p0": False,
        "prevalence_inference_allowed": False,
        "model": model,
        "api_origin_sha256": hashlib.sha256(api_origin.encode("utf-8")).hexdigest(),
        "provider_version": _provider_version(api_origin),
        "call_profile": call_profile,
        "conditions": results,
        "qualified_runtime_condition": "stream-reasoning-none",
        "gate_a_passed": qualified_condition["contract_valid"],
    }
    (root / "MODEL_QUALIFICATION.json").write_text(
        json.dumps(artifact, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return artifact


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run development-only K11 model qualification")
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--source-runtime-dir", required=True)
    parser.add_argument("--api-origin", required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args(argv)
    artifact = run_qualification(
        output_root=args.output_root,
        source_runtime_dir=args.source_runtime_dir,
        api_origin=args.api_origin,
        model=args.model,
    )
    print(json.dumps(artifact, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if artifact["gate_a_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
