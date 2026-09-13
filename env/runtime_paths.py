from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4


RuntimeLayout = Literal["legacy", "isolated"]


@dataclass(frozen=True)
class JsonArtifactRead:
    state: Literal["absent", "invalid", "valid"]
    value: Any = None
    error: str | None = None


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    layout: RuntimeLayout = "legacy"

    def __post_init__(self) -> None:
        if self.layout not in ("legacy", "isolated"):
            raise ValueError(f"Unsupported runtime layout: {self.layout!r}")
        object.__setattr__(self, "root", Path(self.root))

    @classmethod
    def legacy(cls, root: str | Path = ".") -> "RuntimePaths":
        return cls(Path(root), layout="legacy")

    @classmethod
    def isolated(cls, root: str | Path) -> "RuntimePaths":
        return cls(Path(root), layout="isolated")

    @classmethod
    def from_environment(cls) -> "RuntimePaths":
        root = os.environ.get("VILLAGER_RUNTIME_ROOT")
        if not root:
            return cls.legacy()
        layout = os.environ.get("VILLAGER_RUNTIME_LAYOUT", "isolated")
        return cls(Path(root), layout=layout)  # type: ignore[arg-type]

    @property
    def cache_dir(self) -> Path:
        return self.root / (".cache" if self.layout == "legacy" else "cache")

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def result_dir(self) -> Path:
        return self.root / "result"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def meta_setting(self) -> Path:
        return self.cache_dir / "meta_setting.json"

    @property
    def load_status(self) -> Path:
        return self.cache_dir / "load_status.cache"

    @property
    def meta_judger_phase(self) -> Path:
        return self.cache_dir / "meta_judger_phase.cache"

    @property
    def env_cache(self) -> Path:
        return self.cache_dir / "env.cache"

    @property
    def state(self) -> Path:
        return self.cache_dir / "state.json"

    @property
    def heartbeat(self) -> Path:
        return self.cache_dir / "heart_beat.cache"

    @property
    def task_cache(self) -> Path:
        return self.cache_dir / "task.cache"

    @property
    def score(self) -> Path:
        return self.data_dir / "score.json"

    @property
    def action_log(self) -> Path:
        return self.data_dir / "action_log.json"

    @property
    def recipe_hint(self) -> Path:
        return self.data_dir / "recipe_hint.json"

    @property
    def build_map(self) -> Path:
        return self.data_dir / "map.json"

    @property
    def blueprint_descriptions(self) -> Path:
        return self.data_dir / "blueprint_description_all.json"

    @property
    def map_description(self) -> Path:
        return self.data_dir / "map_description.json"

    @property
    def url_prefix(self) -> Path:
        return self.data_dir / "url_prefix.json"

    @property
    def tokens(self) -> Path:
        return self.data_dir / "tokens.json"

    @property
    def openai_log(self) -> Path:
        return self.data_dir / "openai.logs"

    @property
    def openai_diagnostics(self) -> Path:
        return self.data_dir / "openai_diagnostics.jsonl"

    @property
    def minecraft_bridge_diagnostics_dir(self) -> Path:
        return self.data_dir / "minecraft_bridge_diagnostics"

    @property
    def minecraft_bridge_caller_diagnostics(self) -> Path:
        return self.minecraft_bridge_diagnostics_dir / "caller.json"

    def minecraft_bridge_actor_diagnostics(self, actor: str) -> Path:
        safe_actor = "".join(character if character.isalnum() or character in "_-" else "_"
                             for character in str(actor))[:64] or "unknown"
        return self.minecraft_bridge_diagnostics_dir / f"bridge-{safe_actor}.json"

    @property
    def openai_cache(self) -> Path:
        return self.cache_dir / "openai.cache"

    @property
    def llm_inference(self) -> Path:
        return self.data_dir / "llm_inference.json"

    @property
    def history_dir(self) -> Path:
        return self.data_dir / "history"

    @property
    def task_list_log(self) -> Path:
        return self.logs_dir / "task_list.json"

    def run_result_dir(self, run_id: str) -> Path:
        return self.result_dir / run_id

    def ensure_directories(self) -> None:
        for directory in (
            self.cache_dir,
            self.data_dir,
            self.result_dir,
            self.logs_dir,
            self.history_dir,
            self.minecraft_bridge_diagnostics_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def subprocess_environment(
        self,
        base: dict[str, str] | None = None,
        *,
        execution_root: str | Path | None = None,
    ) -> dict[str, str]:
        environment = dict(os.environ if base is None else base)
        environment["VILLAGER_RUNTIME_ROOT"] = str(self.root.resolve())
        environment["VILLAGER_RUNTIME_LAYOUT"] = self.layout
        python_root = str(Path(execution_root or Path(__file__).resolve().parents[1]).resolve())
        # Runtime children import only from the approved execution checkout.
        # Inheriting caller entries would make behavior depend on host state.
        environment["PYTHONPATH"] = python_root
        environment.pop("NODE_PATH", None)
        return environment

    @contextmanager
    def activated(self):
        previous_root = os.environ.get("VILLAGER_RUNTIME_ROOT")
        previous_layout = os.environ.get("VILLAGER_RUNTIME_LAYOUT")
        os.environ["VILLAGER_RUNTIME_ROOT"] = str(self.root.resolve())
        os.environ["VILLAGER_RUNTIME_LAYOUT"] = self.layout
        try:
            yield self
        finally:
            if previous_root is None:
                os.environ.pop("VILLAGER_RUNTIME_ROOT", None)
            else:
                os.environ["VILLAGER_RUNTIME_ROOT"] = previous_root
            if previous_layout is None:
                os.environ.pop("VILLAGER_RUNTIME_LAYOUT", None)
            else:
                os.environ["VILLAGER_RUNTIME_LAYOUT"] = previous_layout


def atomic_write_json(path: str | Path, payload: object) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_text(path: str | Path, value: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_json_artifact(path: str | Path) -> JsonArtifactRead:
    target = Path(path)
    if not target.exists():
        return JsonArtifactRead("absent")
    try:
        with target.open("r", encoding="utf-8") as stream:
            return JsonArtifactRead("valid", json.load(stream))
    except (OSError, json.JSONDecodeError) as exc:
        return JsonArtifactRead("invalid", error=str(exc))
