"""The checked-in assets and process boundary used by runtime children."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from .runtime_paths import RuntimePaths


def _safe(value: object) -> str:
    value = str(value)
    # Error text is intentionally a logical identifier, never a filesystem path.
    value = value.replace("\\", "/")
    value = value.rsplit("/", 1)[-1] if "/" in value else value
    return re.sub(r"[^A-Za-z0-9_.:-]", "_", value)[:96] or "unknown"


class RuntimeAssetError(RuntimeError):
    def __init__(self, code: str, logical_asset: str):
        self.code = _safe(code)
        self.logical_asset = _safe(logical_asset)
        super().__init__(f"runtime asset error: {self.code} ({self.logical_asset})")


@dataclass(frozen=True)
class RuntimeAssetSpec:
    name: str
    relative_path: str | Path
    kind: str = "file"


@dataclass(frozen=True)
class RuntimeAsset:
    name: str
    relative_path: str
    path: Path
    sha256: str
    stat_identity: tuple[int, int, int, int, int]
    kind: str = "file"

    @property
    def absolute_path(self) -> Path:
        return self.path

    @property
    def logical_name(self) -> str:
        return self.name

    @property
    def stat(self) -> tuple[int, int, int, int, int]:
        return self.stat_identity

    def __fspath__(self) -> str:
        return str(self.path)

    def __str__(self) -> str:
        return str(self.path)


# Keep logical names stable: callers use these names rather than reconstructing
# paths from the process working directory.
_DEFAULT = [
    ("bridge_fast", "env/minecraft_server_fast.py"),
    ("bridge_standard", "env/minecraft_server.py"),
    ("build_judger", "env/build_judger.py"),
    ("farm_craft_judger", "env/farm_craft_judger.py"),
    ("escape_room_judger", "env/escape_room_judger.py"),
    ("auto_judger", "env/auto_judger.py"),
    ("meta_judger", "env/meta_judger.py"),
    ("llm_gen_judger", "env/llm_gen_judger.py"),
    ("start_with_config", "start_with_config.py"),
    ("config", "config.py"),
    ("speaking_style", "speaking_style.py"),
    ("controller", "pipeline/controller_tiny.py"),
    ("task_manager", "pipeline/task_manager.py"),
    ("data_manager", "pipeline/data_manager.py"),
    ("agent", "pipeline/agent.py"),
    ("retriever", "pipeline/retriever.py"),
    ("pipeline_utils", "pipeline/utils.py"),
    ("ollama_config", "model/ollama_config.py"),
    ("env", "env/env.py"),
    ("env_api", "env/env_api.py"),
    ("minecraft_client", "env/minecraft_client.py"),
    ("minecraft_define", "env/minecraft_define.py"),
    ("utils", "env/utils.py"),
    ("world_initialization", "env/world_initialization.py"),
    ("judger_artifacts", "env/judger_artifacts.py"),
    ("judger_iteration", "env/judger_iteration.py"),
    ("runtime_paths", "env/runtime_paths.py"),
    ("runtime_execution", "env/runtime_execution.py"),
    ("recipes", "data/recipes.json"),
    ("template_houses", "data/template_houses.json"),
    ("building_blue_print", "data/building_blue_print.json"),
    ("escape_atom", "data/escape_atom.json"),
    ("farm_setting", "data/farm_setting.json"),
    ("farm_blue_print", "data/farm_blue_print.json"),
    ("mcData", "data/mcData.json"),
    ("dig_item", "data/dig_item.json"),
    ("docker_probe", "benchmarks/minecraft/docker_probe.js"),
    ("package", "package.json"),
    ("package_lock", "package-lock.json"),
]
DEFAULT_RUNTIME_ASSETS = tuple(RuntimeAssetSpec(name, path) for name, path in _DEFAULT)

_SOURCE_TREES = (
    "env", "pipeline", "model", "type_define", "rl_env", "benchmarks/common", "benchmarks/minecraft"
)
_CONTROL_PLANE_SOURCES = {
    "benchmarks/minecraft/docker_diagnostics.py",
    "benchmarks/minecraft/docker_runtime.py",
    "benchmarks/minecraft/experiment.py",
    "benchmarks/minecraft/production.py",
}
_ALLOWED_ROOT_DIRECTORIES = {
    ".cache", ".opencode", ".pytest_cache", "__pycache__", "benchmarks", "cache", "configs", "data", "docs",
    "env", "external", "fix-plans", "img", "impl-plans", "logs", "model", "node_modules", "pipeline",
    "result", "results", "rl_env", "tests", "type_define", "visualizer",
}
# These repository/control files are explicitly tolerated at the root but are
# not executable runtime inputs and therefore never enter the runtime digest.
_ALLOWED_NON_RUNTIME_ROOT_FILES = {
    ".envrc",
    ".gitignore",
    ".gitmodules",
    ".python-version",
    "Dockerfile",
    "README.ja.md",
    "README.md",
    "__init__ .py",
    "agent_demo.py",
    "auto_gen_gpt_task.py",
    "auto_monitor.py",
    "example.py",
    "filter_data.py",
    "flake.lock",
    "flake.nix",
    "js_setup.py",
    "justfile",
    "llm_gen_prompt.py",
    "llm_gen_task.py",
    "proposal.md",
    "pyproject.toml",
    "requirements.txt",
    "task_filter.py",
    "tiny_start.py",
    "villagertuning.sh",
}


def _default_runtime_assets(root: Path) -> tuple[RuntimeAssetSpec, ...]:
    """Conservatively identity the importable child-runtime source closure."""
    specs = list(DEFAULT_RUNTIME_ASSETS)
    included = {Path(spec.relative_path).as_posix() for spec in specs}
    candidates: list[Path] = []
    allowed_runtime_root_files = {
        relative for relative in included if "/" not in relative
    }
    allowed_root_files = allowed_runtime_root_files | _ALLOWED_NON_RUNTIME_ROOT_FILES | {".git"}
    for path in root.iterdir():
        if path.name != ".git" and path.is_symlink():
            raise RuntimeAssetError("symlink", "source_closure")
        if path.is_dir() and path.name not in _ALLOWED_ROOT_DIRECTORIES:
            raise RuntimeAssetError("unexpected_root_entry", "source_closure")
        if not path.is_dir() and path.name not in allowed_root_files:
            raise RuntimeAssetError("unexpected_root_entry", "source_closure")
    node_modules = root / "node_modules"
    if node_modules.is_symlink():
        raise RuntimeAssetError("symlink", "node_modules")
    if node_modules.is_dir():
        dependency_entries = list(node_modules.rglob("*"))
        if any(path.is_symlink() for path in dependency_entries):
            raise RuntimeAssetError("symlink", "node_modules")
        candidates.extend(path for path in dependency_entries if not path.is_dir())
    for tree in _SOURCE_TREES:
        source_entries = list((root / tree).rglob("*"))
        if any(path.is_symlink() for path in source_entries):
            raise RuntimeAssetError("symlink", "source_closure")
        candidates.extend(path for path in source_entries if path.match("*.py"))
        candidates.extend(
            path
            for suffix in ("*.pyc", "*.so", "*.pyd")
            for path in (root / tree).rglob(suffix)
            if "__pycache__" not in path.parts
        )
    candidates.extend((root / "data").glob("*.json"))
    eac_docs = root / "docs/eac"
    if eac_docs.is_dir():
        candidates.extend(
            path for path in eac_docs.glob("*.json")
            if "premanifest" not in path.name and "fixture" not in path.name
        )
    for path in sorted(candidates):
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            raise RuntimeAssetError("outside", "source_closure") from None
        if relative in included or relative in _CONTROL_PLANE_SOURCES:
            continue
        specs.append(RuntimeAssetSpec(f"source:{relative}", relative))
        included.add(relative)
    return tuple(specs)


def _identity(path: Path) -> tuple[int, int, int, int, int]:
    st = path.stat()
    return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns)


class RuntimeExecution:
    def __init__(
        self,
        root: Path,
        assets: Mapping[str, RuntimeAsset],
        manifest_sha256: str,
        *,
        dynamic_closure: bool = False,
    ):
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "assets", MappingProxyType(dict(assets)))
        object.__setattr__(self, "manifest_sha256", manifest_sha256)
        object.__setattr__(self, "_dynamic_closure", dynamic_closure)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("RuntimeExecution is immutable")
        object.__setattr__(self, name, value)

    @property
    def asset_count(self) -> int:
        return len(self.assets)

    @classmethod
    def resolve(cls, root: str | Path | None = None, specs: Iterable[RuntimeAssetSpec] | None = None):
        candidate = Path(__file__).resolve().parents[1] if root is None else Path(root)
        if not candidate.is_absolute():
            raise RuntimeAssetError("invalid_root", "root")
        if candidate != Path(os.path.normpath(str(candidate))):
            raise RuntimeAssetError("invalid_root", "root")
        current = Path(candidate.anchor)
        for part in candidate.parts[1:]:
            current /= part
            if current.is_symlink():
                raise RuntimeAssetError("symlink", "root")
        if candidate.is_symlink() or not candidate.exists():
            raise RuntimeAssetError("missing", "root")
        if not candidate.is_dir():
            raise RuntimeAssetError("wrong_type", "root")
        candidate = candidate.resolve(strict=True)
        dynamic_closure = specs is None
        selected = _default_runtime_assets(candidate) if dynamic_closure else tuple(specs)
        assets: dict[str, RuntimeAsset] = {}
        for spec in selected:
            if not isinstance(spec, RuntimeAssetSpec):
                raise RuntimeAssetError("invalid_spec", "asset")
            if not isinstance(spec.name, str) or not spec.name or spec.name in assets:
                raise RuntimeAssetError("invalid_spec", "asset")
            relative = Path(spec.relative_path)
            normalized = Path(os.path.normpath(str(relative)))
            if (normalized.is_absolute() or normalized != relative or str(normalized) in ("", ".", "..")
                    or str(normalized).startswith(".." + os.sep)):
                raise RuntimeAssetError("outside", spec.name)
            path = candidate / normalized
            cls._check_path(candidate, path, spec.name, spec.kind)
            try:
                digest = _sha256(path)
                identity = _identity(path)
            except OSError:
                raise RuntimeAssetError("missing", spec.name) from None
            assets[spec.name] = RuntimeAsset(spec.name, normalized.as_posix(), path, digest, identity, spec.kind)
        manifest = [{"name": a.name, "relative_path": a.relative_path, "kind": a.kind,
                     "size": a.stat_identity[3], "sha256": a.sha256} for a in sorted(assets.values(), key=lambda x: x.name)]
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        return cls(
            candidate,
            assets,
            hashlib.sha256(encoded).hexdigest(),
            dynamic_closure=dynamic_closure,
        )

    @staticmethod
    def _check_path(root: Path, path: Path, name: str, kind: str) -> None:
        current = root
        for part in path.relative_to(root).parts:
            current /= part
            if current.is_symlink():
                raise RuntimeAssetError("symlink", name)
        try:
            if path.resolve() != root and root not in path.resolve().parents:
                raise RuntimeAssetError("outside", name)
        except OSError:
            raise RuntimeAssetError("missing", name) from None
        if not path.exists():
            raise RuntimeAssetError("missing", name)
        if kind != "file":
            raise RuntimeAssetError("invalid_spec", name)
        if not path.is_file() or not stat.S_ISREG(os.lstat(path).st_mode):
            raise RuntimeAssetError("wrong_type", name)

    def asset(self, name: str) -> RuntimeAsset:
        try:
            return self.assets[name]
        except KeyError:
            raise RuntimeAssetError("unknown_asset", name) from None

    def verify(self, *names: str) -> None:
        if self._dynamic_closure:
            expected_paths = {asset.relative_path for asset in self.assets.values()}
            current_paths = {
                Path(spec.relative_path).as_posix()
                for spec in _default_runtime_assets(self.root)
            }
            if current_paths != expected_paths:
                raise RuntimeAssetError("identity_drift", "source_closure")
        for name in names or tuple(self.assets):
            asset = self.asset(name)
            self._check_path(self.root, asset.path, name, asset.kind)
            try:
                if _identity(asset.path) != asset.stat_identity or _sha256(asset.path) != asset.sha256:
                    raise RuntimeAssetError("identity_drift", name)
            except OSError:
                raise RuntimeAssetError("identity_drift", name) from None

    def child_environment(self, runtime_paths: RuntimePaths, base: dict[str, str] | None = None) -> dict[str, str]:
        return runtime_paths.subprocess_environment(base, execution_root=self.root)

    def child_kwargs(self, runtime_paths: RuntimePaths, base: dict[str, str] | None = None) -> dict[str, object]:
        return {"cwd": str(self.root), "env": self.child_environment(runtime_paths, base)}

    def python_command(self, name: str, *args: object) -> list[str]:
        return [sys.executable, str(self.asset(name).path), *(str(arg) for arg in args)]

    def public_command(self, name: str, *args: object) -> list[str]:
        return ["python", self.asset(name).relative_path, *(str(arg) for arg in args)]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
