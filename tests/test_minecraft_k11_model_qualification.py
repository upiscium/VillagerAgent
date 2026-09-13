import json
from pathlib import Path
from types import SimpleNamespace

from benchmarks.minecraft import k11_model_qualification as qualification


def _source_runtime(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    (root / "cache").mkdir(parents=True)
    (root / "result" / "run").mkdir(parents=True)
    (root / "cache" / "meta_setting.json").write_text(json.dumps({
        "task_goal": "secret task goal", "agent_num": 2,
    }), encoding="utf-8")
    (root / "result" / "run" / "DM_query.json").write_text(json.dumps({
        "prompt": [
            "secret environment prompt\n*** The task *** : secret task goalsecret sign"
            ".\nReturn with Entity, Blocks, Creatures, Interactive-Items and Environment"
        ],
        "response": ["secret environment response"],
    }), encoding="utf-8")
    return root


def test_model_qualification_writes_metadata_without_prompt_or_response(
    tmp_path, monkeypatch,
):
    class FakeModel:
        def __init__(self, *, runtime_paths, **_kwargs):
            self.runtime_paths = runtime_paths
            self.client = SimpleNamespace(close=lambda: None)

        def few_shot_generate_thoughts(self, *_args, **_kwargs):
            self.runtime_paths.openai_diagnostics.parent.mkdir(parents=True, exist_ok=True)
            self.runtime_paths.openai_diagnostics.write_text(json.dumps({
                "outcome": "success", "finish_reason": "stop",
                "public_content_chars": 80, "reasoning_chars": 12,
                "validation_category": None,
            }) + "\n", encoding="utf-8")
            return '{"description":"ok","milestones":[],"assigned agents":[]}'

    monkeypatch.setattr(qualification, "OpenAILanguageModel", FakeModel)
    monkeypatch.setattr(qualification, "_provider_version", lambda _origin: "test")
    output = tmp_path / "qualification"

    artifact = qualification.run_qualification(
        output_root=output,
        source_runtime_dir=_source_runtime(tmp_path),
        api_origin="http://127.0.0.1:11434",
        model="gemma4:12b",
    )

    artifact_text = (output / "MODEL_QUALIFICATION.json").read_text(encoding="utf-8")
    assert artifact["gate_a_passed"] is True
    assert len(artifact["conditions"]) == 4
    assert "secret task goal" not in artifact_text
    assert "secret environment prompt" not in artifact_text
    assert "secret environment response" not in artifact_text
    assert all(row["contract_valid"] for row in artifact["conditions"])


def test_qualification_prompt_profile_reproduces_task_manager_contract(tmp_path):
    system_prompt, user_prompt, profile = qualification._load_call_inputs(
        _source_runtime(tmp_path)
    )

    assert system_prompt
    assert user_prompt
    assert profile["call_source"] == "TaskManager.init_task"
    assert profile["required_tags"] == ["description", "milestones", "assigned agents"]
    assert profile["json_check"] is True
    assert "secret task goal" not in json.dumps(profile)
