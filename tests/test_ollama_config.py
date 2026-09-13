import importlib
import json
import sys
import types

import pytest

from model import ollama_config
from model.ollama_config import OLLAMA_API_BASE, OLLAMA_API_KEY, OLLAMA_MODEL, configure_ollama_agent, load_agent_api_key_list, make_ollama_llm_config, normalize_ollama_api_base


def test_ollama_default_endpoint_is_local_openai_compatible_url():
    assert OLLAMA_API_BASE == "http://localhost:11434/v1"


def test_ollama_default_model_is_local_smoke_model():
    assert OLLAMA_MODEL == "gemma4:12b"


def test_ollama_root_origin_resolves_to_same_origin_openai_v1_base():
    assert normalize_ollama_api_base("http://10.255.255.5:11434") == (
        "http://10.255.255.5:11434/v1"
    )
    assert normalize_ollama_api_base("http://10.255.255.5:11434/v1/") == (
        "http://10.255.255.5:11434/v1"
    )


@pytest.mark.parametrize(
    "value",
    [
        "http://user:secret@localhost:11434",
        "http://localhost:11434/api",
        "http://localhost:11434?fallback=1",
    ],
)
def test_ollama_api_base_rejects_identity_and_path_drift(value):
    with pytest.raises(ValueError, match="Ollama API base"):
        normalize_ollama_api_base(value)


def test_load_agent_api_key_list_falls_back_to_ollama_key_when_file_missing(tmp_path):
    missing_path = tmp_path / "API_KEY_LIST"

    assert load_agent_api_key_list(missing_path) == [OLLAMA_API_KEY]


def test_load_agent_api_key_list_falls_back_when_file_is_empty(tmp_path):
    key_path = tmp_path / "API_KEY_LIST"
    key_path.write_text("", encoding="utf-8")

    assert load_agent_api_key_list(key_path) == [OLLAMA_API_KEY]


def test_load_agent_api_key_list_preserves_legacy_file_keys(tmp_path):
    key_path = tmp_path / "API_KEY_LIST"
    key_path.write_text(json.dumps({"AGENT_KEY": ["key-a", "key-b"]}), encoding="utf-8")

    assert load_agent_api_key_list(key_path) == ["key-a", "key-b"]


def test_load_agent_api_key_list_accepts_legacy_string_key(tmp_path):
    key_path = tmp_path / "API_KEY_LIST"
    key_path.write_text(json.dumps({"AGENT_KEY": "key-a"}), encoding="utf-8")

    assert load_agent_api_key_list(key_path) == ["key-a"]


def test_load_agent_api_key_list_falls_back_when_legacy_key_list_is_empty(tmp_path):
    key_path = tmp_path / "API_KEY_LIST"
    key_path.write_text(json.dumps({"AGENT_KEY": []}), encoding="utf-8")

    assert load_agent_api_key_list(key_path) == [OLLAMA_API_KEY]


def test_make_ollama_llm_config_uses_explicit_argument_overrides():
    config = make_ollama_llm_config(
        api_model="custom-model",
        api_base="http://ollama.example/v1",
        api_key="custom-key",
        reasoning_effort="none",
    )

    assert config["api_model"] == "custom-model"
    assert config["api_base"] == "http://ollama.example/v1"
    assert config["api_key"] == "custom-key"
    assert config["api_key_list"] == ["custom-key"]
    assert config["reasoning_effort"] == "none"


def test_configure_ollama_agent_uses_explicit_argument_overrides():
    class Agent:
        pass

    configure_ollama_agent(
        Agent,
        api_model="custom-model",
        api_base="http://ollama.example/v1",
        api_key="custom-key",
    )

    assert Agent.provider == "ollama"
    assert Agent.model == "custom-model"
    assert Agent.base_url == "http://ollama.example/v1"
    assert Agent.api_key_list == ["custom-key"]


def test_ollama_environment_overrides(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_BASE", "http://env-ollama/v1")
    monkeypatch.setenv("OLLAMA_MODEL", "env-model")
    monkeypatch.setenv("OLLAMA_API_KEY", "env-key")

    reloaded = importlib.reload(ollama_config)
    try:
        config = reloaded.make_ollama_llm_config()
        assert config["api_base"] == "http://env-ollama/v1"
        assert config["api_model"] == "env-model"
        assert config["api_key"] == "env-key"
        assert config["api_key_list"] == ["env-key"]
    finally:
        importlib.reload(ollama_config)


def test_start_with_config_run_uses_ollama_defaults_without_api_key_list(monkeypatch, tmp_path):
    monkeypatch.delenv("OLLAMA_API_BASE", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    reloaded_ollama = importlib.reload(ollama_config)
    captured = {}

    class FakeRunContext:
        def __init__(self, env, fast_api):
            env.fast_api_values.append(fast_api)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    class FakeEnv:
        def __init__(self, **kwargs):
            captured["env_kwargs"] = kwargs
            self.fast_api_values = []
            captured["env"] = self

        def agent_register(self, **kwargs):
            captured["agent_register"] = kwargs

        def run(self, fast_api=False):
            return FakeRunContext(self, fast_api)

        def get_init_state(self):
            return {"status": "ready"}

        def get_score(self):
            captured["get_score_called"] = True

    class FakeDataManager:
        def __init__(self, silent=False, history_output_dir=None):
            captured["data_manager_silent"] = silent
            captured["data_manager_history_output_dir"] = history_output_dir

        def update_database_init(self, init_state):
            captured["init_state"] = init_state

    class FakeTaskManager:
        def __init__(self, silent=False, cache_enabled=True, history_output_dir=None):
            captured["task_manager"] = {"silent": silent, "cache_enabled": cache_enabled}
            captured["task_manager_history_output_dir"] = history_output_dir

        def init_task(self, description, document):
            captured["init_task"] = {"description": description, "document": document}

    class FakeController:
        def __init__(self, llm_config, tm, dm, env, **kwargs):
            captured["controller"] = {"llm_config": llm_config, "kwargs": kwargs}

        def run(self):
            captured["controller_run_called"] = True

    class FakeAgent:
        placeBlock = object()
        fetchContainerContents = object()
        MineBlock = object()
        scanNearbyEntities = object()
        equipItem = object()
        navigateTo = object()
        withdrawItem = object()
        dismantleDirtLadder = object()
        erectDirtLadder = object()
        handoverBlock = object()

    fake_env_package = types.ModuleType("env")
    fake_env_package.__path__ = []
    fake_env_module = types.ModuleType("env.env")
    fake_env_module.VillagerBench = lambda **kwargs: FakeEnv(**kwargs)
    fake_env_module.env_type = types.SimpleNamespace(construction="construction")
    fake_env_module.Agent = FakeAgent
    fake_init_model_module = types.ModuleType("model.init_model")
    fake_init_model_module.init_language_model = lambda *args, **kwargs: None
    fake_pipeline_package = types.ModuleType("pipeline")
    fake_pipeline_package.__path__ = []
    fake_controller_module = types.ModuleType("pipeline.controller_tiny")
    fake_controller_module.GlobalController = FakeController
    fake_data_manager_module = types.ModuleType("pipeline.data_manager")
    fake_data_manager_module.DataManager = FakeDataManager
    fake_task_manager_module = types.ModuleType("pipeline.task_manager")
    fake_task_manager_module.TaskManager = FakeTaskManager

    monkeypatch.setitem(sys.modules, "env", fake_env_package)
    monkeypatch.setitem(sys.modules, "env.env", fake_env_module)
    monkeypatch.setitem(sys.modules, "model.init_model", fake_init_model_module)
    monkeypatch.setitem(sys.modules, "pipeline", fake_pipeline_package)
    monkeypatch.setitem(sys.modules, "pipeline.controller_tiny", fake_controller_module)
    monkeypatch.setitem(sys.modules, "pipeline.data_manager", fake_data_manager_module)
    monkeypatch.setitem(sys.modules, "pipeline.task_manager", fake_task_manager_module)
    sys.modules.pop("start_with_config", None)

    start_with_config = importlib.import_module("start_with_config")

    monkeypatch.chdir(tmp_path)
    start_with_config.run(
        api_model=reloaded_ollama.OLLAMA_MODEL,
        api_base=reloaded_ollama.OLLAMA_API_BASE,
        task_type="construction",
        task_idx=0,
        agent_num=1,
        dig_needed=False,
        max_task_num=1,
        task_goal="build a hut",
        document_file="",
        host="127.0.0.1",
        port=40000,
        task_name="ollama_no_key_smoke",
    )

    meta_setting = json.loads((tmp_path / ".cache" / "meta_setting.json").read_text(encoding="utf-8"))
    assert meta_setting["api_model"] == reloaded_ollama.OLLAMA_MODEL
    assert meta_setting["api_base"] == reloaded_ollama.OLLAMA_API_BASE
    assert captured["env"].fast_api_values == [True]
    assert FakeAgent.model == reloaded_ollama.OLLAMA_MODEL
    assert FakeAgent.base_url == reloaded_ollama.OLLAMA_API_BASE
    assert FakeAgent.api_key_list == [reloaded_ollama.OLLAMA_API_KEY]
    assert captured["controller"]["llm_config"]["api_model"] == reloaded_ollama.OLLAMA_MODEL
    assert captured["controller"]["llm_config"]["api_base"] == reloaded_ollama.OLLAMA_API_BASE
    assert captured["controller"]["llm_config"]["api_key_list"] == [reloaded_ollama.OLLAMA_API_KEY]
    assert captured["controller"]["kwargs"]["base_agent_config"] == captured["controller"]["llm_config"]
    assert captured["init_state"] == {"status": "ready"}
    assert captured["controller_run_called"] is True
    assert captured["get_score_called"] is True
    sys.modules.pop("start_with_config", None)
    importlib.reload(ollama_config)
