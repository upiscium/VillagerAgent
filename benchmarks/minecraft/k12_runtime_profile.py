"""Authenticated, sealed runtime inputs for the K12 live qualification.

The versioned ``/2`` profile is prospective: it authenticates a *policy* for
source closure rather than copying source bytes or a Git revision into the
profile.  The historical ``/1`` loader remains explicit and deliberately
keeps the byte and manifest checks used by the original campaign artifact.
"""
from __future__ import annotations

import ast
import copy
import fnmatch
import hashlib
import importlib.metadata
import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any

from benchmarks.common.eac.canonical import canonical_bytes
from benchmarks.minecraft.k12_live_fixture import qualification_ids

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RUNTIME_PROFILE_PATH = HERE / "k12_live_runtime_profile_v2.json"
HISTORICAL_RUNTIME_PROFILE_PATH = HERE / "k12_live_runtime_profile_v1.json"
QUALIFICATION_MANIFEST_PATH = ROOT / "configs/minecraft/k12-live-qualification-manifest-v2.json"
HISTORICAL_QUALIFICATION_MANIFEST_PATH = ROOT / "configs/minecraft/k12-live-qualification-manifest-v1.json"
SOURCE_POLICY_PATH = ROOT / "configs/minecraft/k12-live-source-closure-policy-v2.json"
ENVIRONMENT_POLICY_PATH = ROOT / "configs/minecraft/k12-live-environment-policy-v1.json"

LIVE_PROFILE_IDENTITY = "minecraft-k12-live-runtime-profile/2"
HISTORICAL_LIVE_PROFILE_IDENTITY = "minecraft-k12-live-runtime-profile/1"
LIVE_QUALIFICATION_IDENTITY = "minecraft-eac-k12-live-runtime-qualification/2"
HISTORICAL_QUALIFICATION_IDENTITY = "minecraft-eac-k12-live-runtime-qualification/1"
LIVE_SCHEDULE_IDENTITY = "minecraft-k12-live-runtime-qualification-schedule/1"
HISTORICAL_SOURCE_REVISION = "2687ce4ad0f360d815a81954ee4313eedaa61a8c"


class K12RuntimeProfileError(ValueError):
    """A detached K12 profile or policy is not authenticated."""


def _normalize_historical_source_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise K12RuntimeProfileError("historical source path mismatch")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise K12RuntimeProfileError("historical source path mismatch")
    normalized = path.as_posix()
    if normalized != value:
        raise K12RuntimeProfileError("historical source path mismatch")
    return normalized


@dataclass(frozen=True, slots=True)
class HistoricalSourceContract:
    """Read-only source contract for authenticating frozen `/1` bytes.

    The revision is part of the contract rather than an advisory argument.
    A reader may obtain bytes from Git objects or from a materialized temporary
    root, but every returned byte string is still checked against the detached
    `/1` closure before it can authenticate.
    """

    revision: str
    reader: Callable[[str], bytes]
    root: Path | None = None

    def __post_init__(self) -> None:
        if self.revision != HISTORICAL_SOURCE_REVISION:
            raise K12RuntimeProfileError("historical source revision mismatch")
        if not callable(self.reader):
            raise TypeError("historical source reader is required")
        if self.root is not None:
            root = Path(self.root)
            if root.is_symlink():
                raise K12RuntimeProfileError("historical source root mismatch")
            try:
                resolved = root.resolve(strict=True)
            except OSError as exc:
                raise K12RuntimeProfileError("historical source root mismatch") from exc
            if not resolved.is_dir():
                raise K12RuntimeProfileError("historical source root mismatch")
            object.__setattr__(self, "root", resolved)

    @classmethod
    def from_reader(cls, reader: Callable[[str], bytes], *,
                    revision: str = HISTORICAL_SOURCE_REVISION) -> "HistoricalSourceContract":
        return cls(revision, reader)

    @classmethod
    def from_root(cls, root: str | Path, *,
                  revision: str = HISTORICAL_SOURCE_REVISION) -> "HistoricalSourceContract":
        supplied = Path(root)
        if supplied.is_symlink():
            raise K12RuntimeProfileError("historical source root mismatch")
        try:
            resolved = supplied.resolve(strict=True)
        except OSError as exc:
            raise K12RuntimeProfileError("historical source root mismatch") from exc
        if not resolved.is_dir():
            raise K12RuntimeProfileError("historical source root mismatch")

        def read(relative: str) -> bytes:
            relative = _normalize_historical_source_path(relative)
            candidate = resolved / relative
            component = resolved
            for part in PurePosixPath(relative).parts:
                component /= part
                if component.is_symlink():
                    raise K12RuntimeProfileError("historical source symlink rejected")
            try:
                observed = candidate.resolve(strict=True)
            except OSError as exc:
                raise K12RuntimeProfileError("historical source byte missing") from exc
            if observed == resolved or resolved not in observed.parents or not observed.is_file():
                raise K12RuntimeProfileError("historical source path escaped root")
            try:
                return observed.read_bytes()
            except OSError as exc:
                raise K12RuntimeProfileError("historical source byte unreadable") from exc

        return cls(revision, read, resolved)

    def read(self, relative: str) -> bytes:
        relative = _normalize_historical_source_path(relative)
        try:
            value = self.reader(relative)
        except K12RuntimeProfileError:
            raise
        except Exception as exc:
            raise K12RuntimeProfileError("historical source byte unreadable") from exc
        if type(value) is not bytes:
            raise K12RuntimeProfileError("historical source reader must return bytes")
        return value


HistoricalSourceRoot = HistoricalSourceContract
K12HistoricalSourceContract = HistoricalSourceContract


class _FrozenList(tuple):
    def __eq__(self, other: object) -> bool:
        return tuple(self) == tuple(other) if isinstance(other, (list, tuple)) else False


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_freeze(item) for item in value)
    return value


_PROFILE_TOKEN = object()
_SOURCE_POLICY_TOKEN = object()


@dataclass(frozen=True, slots=True)
class K12SourcePolicy:
    """Loader-issued, exact source-closure policy.

    Hashes are intentionally not stored in this policy.  The policy is a
    non-circular allowlist; a later observation supplies the Git blob and raw
    byte hash for every listed path.  Git mode and semantic class are part of
    the policy itself because they describe what the observed source is
    allowed to be, rather than an observation of its contents.
    """

    identity: str
    digest: str
    entries: tuple["K12SourcePolicyEntry", ...]
    _token: object = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if self._token is not _SOURCE_POLICY_TOKEN:
            raise TypeError("K12SourcePolicy is loader-minted")
        if not isinstance(self.identity, str) or not isinstance(self.digest, str):
            raise K12RuntimeProfileError("prospective source policy mismatch")
        if len(self.digest) != 64 or any(char not in "0123456789abcdef" for char in self.digest):
            raise K12RuntimeProfileError("prospective source policy mismatch")
        if (not self.entries or any(not isinstance(item, K12SourcePolicyEntry)
                                    for item in self.entries)):
            raise K12RuntimeProfileError("prospective source policy mismatch")
        entries = tuple(self.entries)
        names = tuple(item.path for item in entries)
        if (names != tuple(sorted(names)) or len(names) != len(set(names))
                or len(names) != len({name.casefold() for name in names})):
            raise K12RuntimeProfileError("prospective source policy mismatch")
        object.__setattr__(self, "entries", entries)

    @property
    def paths(self) -> tuple[str, ...]:
        """Compatibility view of the policy's canonical paths."""

        return tuple(item.path for item in self.entries)

    @property
    def types(self) -> tuple[tuple[str, str], ...]:
        """Compatibility view; semantic class replaces the old ``type`` key."""

        return tuple((item.path, item.semantic_class) for item in self.entries)

    def type_for(self, path: str) -> str:
        return self.semantic_class_for(path)

    def entry_for(self, path: str) -> "K12SourcePolicyEntry":
        for entry in self.entries:
            if entry.path == path:
                return entry
        raise KeyError(path)

    def mode_for(self, path: str) -> str:
        return self.entry_for(path).git_mode

    def semantic_class_for(self, path: str) -> str:
        return self.entry_for(path).semantic_class


SOURCE_SEMANTIC_CLASSES = frozenset({
    "python", "config", "contract", "documentation", "dependency", "javascript",
})


@dataclass(frozen=True, slots=True)
class K12SourcePolicyEntry:
    """One typed, canonical entry in the checked-in source policy."""

    path: str
    git_mode: str
    semantic_class: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path or "\\" in self.path:
            raise K12RuntimeProfileError("prospective source policy mismatch")
        path = PurePosixPath(self.path)
        if (path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts)
                or path.as_posix() != self.path):
            raise K12RuntimeProfileError("prospective source policy mismatch")
        if type(self.git_mode) is not str or self.git_mode not in {"100644", "100755"}:
            raise K12RuntimeProfileError("prospective source policy mismatch")
        if (type(self.semantic_class) is not str
                or self.semantic_class not in SOURCE_SEMANTIC_CLASSES):
            raise K12RuntimeProfileError("prospective source policy mismatch")


@dataclass(frozen=True, slots=True, init=False)
class K12RuntimeProfile(Mapping[str, Any]):
    """The only trusted root produced by a detached-digest loader."""

    values: tuple[tuple[str, Any], ...]

    def __init__(self, values: tuple[tuple[str, Any], ...], token: object = None) -> None:
        if token is not _PROFILE_TOKEN:
            raise TypeError("K12RuntimeProfile is loader-minted")
        object.__setattr__(self, "values", values)
        self.__post_init__()

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", tuple(
            (key, _freeze(value)) for key, value in self.values
        ))

    def __getitem__(self, key: str) -> Any:
        for name, value in self.values:
            if name == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self.values)

    def __len__(self) -> int:
        return len(self.values)

    @property
    def profile_id(self) -> str:
        return self["profile_version"]

    @property
    def profile_digest(self) -> str:
        return self["detached_artifact_sha256"]


def detached_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes({
        key: item for key, item in value.items()
        if key != "detached_artifact_sha256"
    })).hexdigest()


def _strict_json_parse(text: str, label: str) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise K12RuntimeProfileError(f"duplicate JSON key in {label}: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=object_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise K12RuntimeProfileError(f"cannot load {label}") from exc
    if not isinstance(value, dict):
        raise K12RuntimeProfileError(f"{label} must be an object")
    declared = value.get("detached_artifact_sha256")
    observed = detached_digest(value)
    if (not isinstance(declared, str) or len(declared) != 64
            or any(char not in "0123456789abcdef" for char in declared)
            or declared != observed):
        raise K12RuntimeProfileError(f"{label} detached digest mismatch")
    return value


def strict_json_load(path: Path, label: str) -> dict[str, Any]:
    """Load one detached JSON object while rejecting duplicate keys."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise K12RuntimeProfileError(f"cannot load {label}") from exc
    return _strict_json_parse(text, label)


def strict_json_load_bytes(data: bytes, label: str) -> dict[str, Any]:
    if type(data) is not bytes:
        raise K12RuntimeProfileError(f"cannot load {label}")
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise K12RuntimeProfileError(f"cannot load {label}") from exc
    return _strict_json_parse(text, label)


def _historical_json_load(
        path: str | Path, *, source_contract: HistoricalSourceContract,
        relative: str, label: str,
) -> dict[str, Any]:
    """Load a candidate fixture only when it is byte-identical to `/1` bytes."""
    try:
        candidate_bytes = Path(path).read_bytes()
    except (OSError, UnicodeError) as exc:
        raise K12RuntimeProfileError(f"cannot load {label}") from exc
    candidate = strict_json_load_bytes(candidate_bytes, label)
    historical_bytes = source_contract.read(relative)
    if candidate_bytes != historical_bytes:
        raise K12RuntimeProfileError(f"{label} does not match historical source revision")
    return candidate


def historical_source_contract(
        *, source_root: str | Path | None = None,
        source_reader: Callable[[str], bytes] | HistoricalSourceContract | None = None,
        source_contract: HistoricalSourceContract | None = None,
        revision: str = HISTORICAL_SOURCE_REVISION,
) -> HistoricalSourceContract:
    """Build the explicit immutable-revision reader used by the `/1` loader."""
    if source_contract is not None:
        if source_root is not None or source_reader is not None:
            raise TypeError("historical source contract cannot be combined with root or reader")
        if not isinstance(source_contract, HistoricalSourceContract):
            raise TypeError("historical source contract is required")
        if source_contract.revision != revision:
            raise K12RuntimeProfileError("historical source revision mismatch")
        return source_contract
    if source_root is not None and source_reader is not None:
        raise TypeError("historical source root and reader are mutually exclusive")
    if isinstance(source_reader, HistoricalSourceContract):
        if source_reader.revision != revision:
            raise K12RuntimeProfileError("historical source revision mismatch")
        return source_reader
    if source_reader is not None:
        return HistoricalSourceContract.from_reader(source_reader, revision=revision)
    return HistoricalSourceContract.from_root(source_root if source_root is not None else ROOT,
                                              revision=revision)


# The policy is derived from these declared production entrypoint families;
# it is deliberately not a second hand-maintained copy of the checked-in
# policy paths.  New K12 production modules therefore enter the closure when
# they are added under the K12 family, while imports from those modules are
# still followed transitively by the AST resolver below.
_SOURCE_ENTRYPOINT_PATTERNS = (
    "benchmarks/__init__.py",
    "benchmarks/common/__init__.py",
    "benchmarks/common/eac/*.py",
    "benchmarks/minecraft/__init__.py",
    "benchmarks/minecraft/k12_live_*.py",
    "benchmarks/minecraft/k12_authority_contracts.py",
    "benchmarks/minecraft/k12_execution_*.py",
    "benchmarks/minecraft/k12_containment.py",
    "benchmarks/minecraft/k12_evidence.py",
    "benchmarks/minecraft/k12_fixture.py",
    "benchmarks/minecraft/k12_guarded_backend.py",
    "benchmarks/minecraft/k12_identity.py",
    "benchmarks/minecraft/k12_model.py",
    "benchmarks/minecraft/k12_protocol.py",
    "benchmarks/minecraft/k12_request.py",
    "benchmarks/minecraft/eac_runtime.py",
    "benchmarks/minecraft/run_lock.py",
    "env/eac_observation_adapter.py",
    "env/__init__.py",
    "env/env.py",
    "env/minecraft_bridge_diagnostics.py",
    "env/minecraft_client.py",
    "env/minecraft_eac_bridge.py",
    "env/minecraft_server_fast.py",
    "env/runtime_execution.py",
    "env/runtime_paths.py",
    "env/utils.py",
    "model/abstract_language_model.py",
    "model/__init__.py",
    "model/ollama_config.py",
    "model/openai_models.py",
    "model/utils.py",
)
_REVIEWED_INPUT_PATTERNS = (
    "docs/eac/minecraft_*.json",
    "benchmarks/minecraft/k12_live_*.json",
    "benchmarks/minecraft/k12_protocol_v1.json",
    "benchmarks/minecraft/k12_request_schema_v1.json",
    "benchmarks/minecraft/k12_trace_schema_v1.json",
    "benchmarks/minecraft/k12_result_schema_v1.json",
    "benchmarks/minecraft/k12_validation_contract_v1.json",
    "configs/minecraft/k12-live-*.json",
    "configs/minecraft/k12-recovery-*.json",
    "package-lock.json",
    "requirements.txt",
)
_EXCLUDED_SOURCE_INPUTS = frozenset({
    "benchmarks/minecraft/k12_live_runtime_profile_v1.json",
    # A policy cannot authenticate itself as one of its own reviewed inputs.
    "configs/minecraft/k12-live-source-closure-policy-v2.json",
})
_EXPECTED_SOURCE_GIT_MODE = "100644"


def _relative_source_path(root: Path, path: Path) -> str | None:
    try:
        relative = path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return None
    if path.is_symlink() or not path.is_file():
        return None
    value = relative.as_posix()
    if (not value or value.startswith(".") or "\\" in value
            or any(part in {"", ".", ".."} for part in PurePosixPath(value).parts)):
        return None
    return value


def _matching_source_files(root: Path, patterns: tuple[str, ...]) -> tuple[Path, ...]:
    candidates: dict[str, Path] = {}
    for candidate in root.rglob("*"):
        try:
            lexical = candidate.relative_to(root).as_posix()
        except ValueError:
            lexical = ""
        if candidate.is_symlink() and any(fnmatch.fnmatch(lexical, pattern)
                                          for pattern in patterns):
            raise K12RuntimeProfileError("source policy entrypoint symlink rejected")
        relative = _relative_source_path(root, candidate)
        if relative is None:
            continue
        if any(fnmatch.fnmatch(relative, pattern) for pattern in patterns):
            candidates[relative] = candidate
    return tuple(candidates[name] for name in sorted(candidates))


def _add_source_file(root: Path, paths: set[str], candidate: Path) -> None:
    relative = _relative_source_path(root, candidate)
    if relative is None:
        raise K12RuntimeProfileError("source policy references a non-regular local input")
    paths.add(relative)
    # Importing a module also executes the package initializers that contain
    # it.  Add only actual package initializers, not every directory.
    parent = candidate.parent
    root_resolved = root.resolve(strict=True)
    while parent != root_resolved and root_resolved in parent.parents:
        initializer = parent / "__init__.py"
        if initializer.is_file() and not initializer.is_symlink():
            initializer_relative = _relative_source_path(root, initializer)
            if initializer_relative is not None:
                paths.add(initializer_relative)
        parent = parent.parent


def _module_candidates(root: Path, source: Path, module: str, level: int = 0) -> tuple[Path, ...]:
    if level:
        source_relative = source.resolve(strict=True).relative_to(root.resolve(strict=True))
        source_parts = list(source_relative.with_suffix("").parts)
        package_parts = source_parts[:-1]
        if level > len(package_parts) + 1:
            return ()
        prefix = package_parts[:len(package_parts) - level + 1]
        module_parts = tuple(part for part in (module or "").split(".") if part)
        parts = tuple(prefix) + module_parts
    else:
        parts = tuple(part for part in module.split(".") if part)
    if not parts:
        return (source.parent,)

    bases = [root.joinpath(*parts)]
    # A few repository entrypoints are also executable as scripts and use a
    # same-directory, non-package import (for example ``env_api``).  Admit
    # that local form without admitting arbitrary worktree imports.
    if not level and len(parts) == 1:
        bases.append(source.parent / parts[0])
    result: list[Path] = []
    for base in bases:
        candidates = (base, base.with_suffix(".py"), base / "__init__.py")
        for candidate in candidates:
            if candidate.is_symlink():
                raise K12RuntimeProfileError("source policy import symlink rejected")
            if candidate.is_file() and candidate not in result:
                result.append(candidate)
    return tuple(result)


def _parse_source_imports(root: Path, source: Path) -> tuple[Path, ...]:
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise K12RuntimeProfileError("cannot parse source policy entrypoint") from exc
    result: set[Path] = set()

    def add_module(module: str | None, level: int = 0) -> tuple[Path, ...]:
        if not module and not level:
            return ()
        candidates = _module_candidates(root, source, module or "", level)
        result.update(candidates)
        return candidates

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add_module(alias.name)
        elif isinstance(node, ast.ImportFrom):
            bases = add_module(node.module, node.level)
            for alias in node.names:
                if alias.name == "*":
                    continue
                for base in bases:
                    package = (base if base.is_dir()
                               else base.parent)
                    candidate = package / alias.name
                    for local in (candidate, candidate.with_suffix(".py"),
                                  candidate / "__init__.py"):
                        if local.is_symlink():
                            raise K12RuntimeProfileError("source policy import symlink rejected")
                        if local.is_file():
                            result.add(local)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr not in {"import_module", "__import__"} or not node.args:
                continue
            argument = node.args[0]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                add_module(argument.value)
    return tuple(result)


def _transitive_import_closure(root: Path) -> set[str]:
    entrypoints = _matching_source_files(root, _SOURCE_ENTRYPOINT_PATTERNS)
    if not entrypoints:
        raise K12RuntimeProfileError("source policy has no declared entrypoints")
    paths: set[str] = set()
    pending = list(entrypoints)
    seen: set[Path] = set()
    while pending:
        source = pending.pop()
        resolved = source.resolve(strict=True)
        if resolved in seen:
            continue
        seen.add(resolved)
        _add_source_file(root, paths, resolved)
        for imported in _parse_source_imports(root, resolved):
            if imported.resolve(strict=True) not in seen:
                pending.append(imported)
    return paths


def _literal_json_references(root: Path, sources: set[str]) -> set[str]:
    """Resolve literal repository-local JSON inputs used by source modules."""

    result: set[str] = set()
    for relative in sorted(sources):
        if not relative.endswith(".py"):
            continue
        source = root / relative
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, UnicodeError, SyntaxError) as exc:
            raise K12RuntimeProfileError("cannot parse source policy input reference") from exc
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            value = node.value.replace("\\", "/")
            if not value.endswith(".json") or "://" in value or value.startswith("/"):
                continue
            candidates = [source.parent / value, root / value]
            for candidate in candidates:
                normalized = _relative_source_path(root, candidate)
                if normalized is not None and normalized not in _EXCLUDED_SOURCE_INPUTS:
                    result.add(normalized)
    return result


def _semantic_class(path: str) -> str:
    if path.endswith(".py"):
        return "python"
    if path == "package-lock.json":
        return "javascript"
    if path == "requirements.txt":
        return "dependency"
    if path.startswith("docs/"):
        return "documentation"
    if path.startswith("configs/"):
        return "config"
    return "contract"


def build_source_policy(root: str | Path = ROOT) -> tuple[dict[str, str], ...]:
    """Derive the exact typed policy from entrypoints and reviewed inputs.

    This function is the single source of truth used by the loader and by the
    artifact-generation tests.  It intentionally returns ordinary JSON-ready
    records so a generator can serialize the result without importing any
    runtime authority types.
    """

    root = Path(root).resolve(strict=True)
    paths = _transitive_import_closure(root)
    paths.update(_literal_json_references(root, paths))
    for candidate in _matching_source_files(root, _REVIEWED_INPUT_PATTERNS):
        relative = _relative_source_path(root, candidate)
        if relative is not None and relative not in _EXCLUDED_SOURCE_INPUTS:
            paths.add(relative)
    paths.difference_update(_EXCLUDED_SOURCE_INPUTS)
    if not paths:
        raise K12RuntimeProfileError("source policy closure is empty")
    records = tuple(
        {
            "path": relative,
            "git_mode": _EXPECTED_SOURCE_GIT_MODE,
            "semantic_class": _semantic_class(relative),
        }
        for relative in sorted(paths)
    )
    return records


def build_source_policy_artifact(root: str | Path = ROOT) -> dict[str, Any]:
    """Return a freshly generated detached policy artifact for release tooling."""

    value: dict[str, Any] = {
        "artifact_id": "minecraft-k12-live-source-closure-policy",
        "artifact_version": 2,
        "schema_version": "minecraft-k12-live-source-closure-policy/2",
        "paths": list(build_source_policy(root)),
        "unlisted_executable_input": "reject",
        "observed_hashes_stored_in_policy": False,
    }
    value["detached_artifact_sha256"] = detached_digest(value)
    return value


_COMMON_IDENTITIES = {
    "config_identity": "minecraft-eac-k12-live-config/1",
    "fixture_identity": "minecraft-eac-k12-live-fixture/1",
    "bridge_identity": "minecraft-server-fast/1",
    "capability_identity": "minecraft-k12-guarded-tool-capability/1",
    "provider_identity": "minecraft-eac-provider/1",
    "server_identity": "minecraft-server/1.19.2",
    "data_identity": "minecraft-data/1",
    "fastapi_identity": "fastapi/0.136.1",
    "starlette_identity": "starlette/1.0.0",
    "openai_sdk_identity": "openai/1.6.1",
    "tiktoken_identity": "tiktoken/0.5.1",
    "httpx_identity": "httpx/0.25.2",
    "pyyaml_identity": "PyYAML/6.0.3",
    "eac_identity": "eac/1",
    "rcon_identity": "minecraft-rcon/1",
    "reset_identity": "minecraft-k12-live-reset-readback/1",
    "parser_identity": "minecraft-eac-parser/1",
    "state_identity": "minecraft-k12-live-state/1",
    "containment_identity": "minecraft-k12-live-containment/1",
    "stop_identity": "minecraft-k12-live-stop-policy/1",
    "action_identity": "minecraft-eac-actions/1",
    "oracle_identity": "minecraft-k12-live-oracle/1",
    "model_identity": "live-model/1",
}
_V2_IDENTITIES = {
    **_COMMON_IDENTITIES,
    "source_identity": "minecraft-eac-k12-live-source/2",
    "source_policy_identity": "minecraft-k12-live-source-closure-policy/2",
    "environment_policy_identity": "minecraft-k12-live-environment-policy/1",
    "execution_capsule_policy_identity": "minecraft-k12-live-execution-capsule/1",
    "qualification_execution_authority_identity":
        "minecraft-k12-live-qualification-execution-authority/1",
    "final_execution_authority_identity":
        "minecraft-k12-live-final-execution-authority/1",
    "node_identity": "node/22.23.2",
    "java_identity": "java/17",
}
_V1_IDENTITIES = {**_COMMON_IDENTITIES, "source_identity": "minecraft-eac-k12-live-source/1"}


def load_k12_live_source_policy(path: str | Path = SOURCE_POLICY_PATH) -> K12SourcePolicy:
    value = strict_json_load(Path(path), "K12 live source policy")
    entries = value.get("paths")
    if (value.get("artifact_id") != "minecraft-k12-live-source-closure-policy"
            or value.get("artifact_version") != 2
            or value.get("schema_version") != "minecraft-k12-live-source-closure-policy/2"
             or value.get("unlisted_executable_input") != "reject"
             or value.get("observed_hashes_stored_in_policy") is not False
             or not isinstance(entries, list)):
        raise K12RuntimeProfileError("prospective source policy mismatch")
    observed: list[K12SourcePolicyEntry] = []
    for entry in entries:
        if (not isinstance(entry, dict)
                or set(entry) != {"path", "git_mode", "semantic_class"}):
            raise K12RuntimeProfileError("prospective source policy mismatch")
        try:
            observed.append(K12SourcePolicyEntry(
                entry["path"], entry["git_mode"], entry["semantic_class"],
            ))
        except (TypeError, K12RuntimeProfileError) as exc:
            raise K12RuntimeProfileError("prospective source policy mismatch") from exc
    expected = build_source_policy(ROOT)
    observed_json = tuple({
        "path": item.path,
        "git_mode": item.git_mode,
        "semantic_class": item.semantic_class,
    } for item in observed)
    if observed_json != expected:
        raise K12RuntimeProfileError("prospective source policy mismatch")
    if tuple(item.path for item in observed) != tuple(sorted(item.path for item in observed)):
        raise K12RuntimeProfileError("prospective source policy mismatch")
    return K12SourcePolicy(
        value["schema_version"], value["detached_artifact_sha256"],
        tuple(observed), _SOURCE_POLICY_TOKEN,
    )


def _validate_common(value: Mapping[str, Any], *, historical: bool) -> None:
    expected_version = 1 if historical else 2
    expected_identity = HISTORICAL_LIVE_PROFILE_IDENTITY if historical else LIVE_PROFILE_IDENTITY
    if (value.get("artifact_id") != "minecraft-k12-live-runtime-profile"
            or value.get("artifact_version") != expected_version
            or value.get("profile_version") != expected_identity
            or value.get("runtime_mode") != "guarded_real"
            or value.get("execution_mode") != "live"
            or value.get("locale") != "en_us"
            or value.get("minecraft_version") != "1.19.2"
            or value.get("server_jar_sha256") != "b26727069ef5f61c704add9a378ac90e3d271fd7876c0bd3dcfbe9fd0bec4d96"
            or value.get("config_identity") != "minecraft-eac-k12-live-config/1"
            or value.get("fixture_identity") != "minecraft-eac-k12-live-fixture/1"
            or value.get("sealed") is not True
            or value.get("before_campaign_identity") is not True
            or any(value.get(key) is not False for key in
                   ("fake_backend_allowed", "offline_mode_allowed", "caller_overrides_allowed"))):
        raise K12RuntimeProfileError("K12 live runtime profile is not sealed guarded_real")
    expected = list(qualification_ids())
    if (value.get("schedule") != expected or len(expected) != 15
            or value.get("schedule_identity") != LIVE_SCHEDULE_IDENTITY):
        raise K12RuntimeProfileError("K12 live schedule mismatch")
    identities = _V1_IDENTITIES if historical else _V2_IDENTITIES
    if any(value.get(key) != expected_value for key, expected_value in identities.items()):
        raise K12RuntimeProfileError("K12 frozen identity mismatch")


def _validate_installed_distributions(value: Mapping[str, Any]) -> None:
    for package, key in (
        ("fastapi", "fastapi_identity"), ("starlette", "starlette_identity"),
        ("openai", "openai_sdk_identity"), ("tiktoken", "tiktoken_identity"),
        ("httpx", "httpx_identity"), ("PyYAML", "pyyaml_identity"),
    ):
        try:
            installed = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError as exc:
            raise K12RuntimeProfileError(f"missing runtime package: {package}") from exc
        if value[key] != f"{package}/{installed}":
            raise K12RuntimeProfileError(f"installed package identity mismatch: {package}")


def _validate_provider_and_bridge(
        value: Mapping[str, Any], *, source_contract: HistoricalSourceContract | None = None,
) -> None:
    bridge = HERE.parent.parent / "env" / "minecraft_server_fast.py"
    try:
        bridge_bytes = (source_contract.read("env/minecraft_server_fast.py")
                        if source_contract is not None else bridge.read_bytes())
    except OSError as exc:
        raise K12RuntimeProfileError("cannot authenticate bridge content") from exc
    bridge_digest = hashlib.sha256(bridge_bytes).hexdigest()
    if value.get("bridge_content_sha256") != bridge_digest:
        raise K12RuntimeProfileError("bridge content digest mismatch")
    expected_policy = {
        "transport": "openai_compatible_sealed", "temperature": 0,
        "attempts": 1, "retries": 0, "cache": False, "stream": False,
        "image": False, "connect_timeout_seconds": 5, "request_timeout_seconds": 120,
    }
    if value.get("provider_policy") != expected_policy:
        raise K12RuntimeProfileError("K12 provider policy mismatch")
    if any(not isinstance(value.get(name), str) or len(value[name]) != 64
           for name in ("endpoint_sha256", "endpoint_hash")):
        raise K12RuntimeProfileError("K12 endpoint identity is incomplete")
    if value["endpoint_sha256"] != value["endpoint_hash"]:
        raise K12RuntimeProfileError("provider endpoint identity mismatch")


def _validate_historical_source_bytes(
        value: Mapping[str, Any], *, source_contract: HistoricalSourceContract,
) -> None:
    """Authenticate every frozen `/1` source byte through one reader contract."""
    if source_contract.revision != HISTORICAL_SOURCE_REVISION:
        raise K12RuntimeProfileError("historical source revision mismatch")
    source_paths = {
        "eac_runtime_sha256": "benchmarks/minecraft/eac_runtime.py",
        "eac_config_sha256": "benchmarks/minecraft/k12_protocol_v1.json",
    }
    for name, relative in source_paths.items():
        digest = value.get(name)
        if (not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            raise K12RuntimeProfileError("K12 source closure is incomplete")
        observed = hashlib.sha256(source_contract.read(relative)).hexdigest()
        if digest != observed:
            raise K12RuntimeProfileError("K12 source closure mismatch")
    expected_sources = {
        "benchmarks/minecraft/k12_runtime_profile.py",
        "benchmarks/minecraft/k12_guarded_backend.py",
        "benchmarks/minecraft/k12_live_fixture.py",
        "benchmarks/minecraft/k12_live_state.py",
        "benchmarks/minecraft/k12_live_reset.py",
        "benchmarks/minecraft/k12_live_oracle.py",
        "benchmarks/minecraft/k12_live_containment.py",
        "benchmarks/minecraft/k12_live_runner.py",
        "benchmarks/minecraft/k12_live_validation.py",
        "benchmarks/minecraft/k12_live_artifacts.py",
        "benchmarks/minecraft/k12_live_qualification.py",
        "benchmarks/minecraft/k12_model.py",
        "benchmarks/minecraft/k12_evidence.py",
        "model/openai_models.py", "model/abstract_language_model.py", "model/utils.py",
        "env/runtime_paths.py", "benchmarks/common/eac/canonical.py",
        "benchmarks/minecraft/k12_containment.py", "requirements.txt",
    }
    closure = value.get("source_closure")
    if not isinstance(closure, dict) or set(closure) != expected_sources:
        raise K12RuntimeProfileError("K12 live source closure is incomplete")
    for relative, digest in closure.items():
        if (not isinstance(relative, str) or not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            raise K12RuntimeProfileError("K12 live source closure is incomplete")
        observed = hashlib.sha256(source_contract.read(relative)).hexdigest()
        if digest != observed:
            raise K12RuntimeProfileError("K12 live source closure mismatch")


def _validate_contracts(
        value: Mapping[str, Any], *, historical: bool,
        source_contract: HistoricalSourceContract | None = None,
) -> None:
    contract_paths = {
        "reset": HERE / "k12_live_reset_readback_v1.json",
        "oracle": HERE / "k12_live_oracle_v1.json",
        "stop_policy": HERE / "k12_live_stop_policy_v1.json",
        "qualification": HERE / "k12_live_qualification_v1.json",
        "qualification_manifest": (HISTORICAL_QUALIFICATION_MANIFEST_PATH
                                    if historical else QUALIFICATION_MANIFEST_PATH),
        "containment_probe": ROOT / "configs/minecraft/k12-live-containment-probe-v1.json",
    }
    contracts = value.get("contract_digests")
    if not isinstance(contracts, dict) or set(contracts) != set(contract_paths):
        raise K12RuntimeProfileError("K12 live contract closure is incomplete")
    for name, contract_path in contract_paths.items():
        label = f"K12 live {name} contract"
        if source_contract is None:
            contract = strict_json_load(contract_path, label)
        else:
            try:
                relative = contract_path.resolve(strict=True).relative_to(ROOT).as_posix()
            except (OSError, ValueError) as exc:
                raise K12RuntimeProfileError("historical contract path mismatch") from exc
            contract = strict_json_load_bytes(source_contract.read(relative), label)
        if contracts[name] != contract.get("detached_artifact_sha256"):
            raise K12RuntimeProfileError("K12 live contract closure mismatch")


def _validate_environment_policy(value: Mapping[str, Any]) -> None:
    expected = {
        "artifact_id": "minecraft-k12-live-environment-policy",
        "artifact_version": 1,
        "schema_version": "minecraft-k12-live-environment-policy/1",
        "capsule_identity": "minecraft-k12-live-execution-capsule/1",
        "python_identity": "CPython/3.10.19",
        "critical_distributions": {
            "PyYAML": "6.0.3", "fastapi": "0.136.1", "httpx": "0.25.2",
            "openai": "1.6.1", "starlette": "1.0.0", "tiktoken": "0.5.1",
        },
        "node_identity": "node/22.23.2",
        "java_identity": "java/17",
        "locale": "en_us",
        "seed_support_state": "unsupported",
        "user_systemd_required": True,
        "cgroup_v2_required": True,
        "user_site_allowed": False,
        "editable_installs_allowed": False,
        "pth_allowed": False,
        "sitecustomize_allowed": False,
        "startup_hooks_allowed": False,
        "writable_worktree_imports_allowed": False,
        "secret_policy": "reference-name-and-presence-only",
        "diagnostic_host_inventory_gates_execution": False,
    }
    if any(value.get(key) != expected_value for key, expected_value in expected.items()):
        raise K12RuntimeProfileError("prospective environment policy mismatch")


def _load_runtime_profile(
        path: str | Path, *, historical: bool,
        source_contract: HistoricalSourceContract | None = None,
) -> K12RuntimeProfile:
    historical_contract: HistoricalSourceContract | None = None
    if historical:
        historical_contract = source_contract or historical_source_contract()
        if not isinstance(historical_contract, HistoricalSourceContract):
            raise TypeError("historical source contract is required")
        value = _historical_json_load(
            path, source_contract=historical_contract,
            relative="benchmarks/minecraft/k12_live_runtime_profile_v1.json",
            label="K12 live runtime profile",
        )
        revision = value.get("execution_revision")
        if (not isinstance(revision, str) or len(revision) != 40
                or any(char not in "0123456789abcdef" for char in revision)):
            raise K12RuntimeProfileError("full execution revision is required")
    else:
        value = strict_json_load(Path(path), "K12 live runtime profile")
        # A `/2` profile must not smuggle a historical commit or generated
        # capsule hash back into the prospective authority root.
        forbidden = {"execution_revision", "compatibility_base_revision",
                     "generated_capsule_sha256", "execution_capsule_digest",
                     "source_closure", "eac_runtime_sha256", "eac_config_sha256"}
        if forbidden.intersection(value):
            raise K12RuntimeProfileError("prospective profile contains historical execution identity")
    _validate_common(value, historical=historical)
    _validate_installed_distributions(value)
    _validate_provider_and_bridge(value, source_contract=historical_contract)
    if historical:
        _validate_historical_source_bytes(value, source_contract=historical_contract)
    else:
        source_policy = load_k12_live_source_policy()
        environment_policy = strict_json_load(ENVIRONMENT_POLICY_PATH, "K12 live environment policy")
        _validate_environment_policy(environment_policy)
        if value.get("source_policy_digest") != source_policy.digest:
            raise K12RuntimeProfileError("prospective source policy mismatch")
        if (environment_policy.get("artifact_id") != "minecraft-k12-live-environment-policy"
                or environment_policy.get("schema_version") != "minecraft-k12-live-environment-policy/1"
                or value.get("environment_policy_digest")
                != environment_policy["detached_artifact_sha256"]):
            raise K12RuntimeProfileError("prospective environment policy mismatch")
    _validate_contracts(value, historical=historical, source_contract=historical_contract)
    return K12RuntimeProfile(tuple(value.items()), _PROFILE_TOKEN)


def load_k12_live_runtime_profile(path: str | Path = RUNTIME_PROFILE_PATH) -> K12RuntimeProfile:
    """Load only the prospective `/2` profile; there is no `/1` fallback."""
    return _load_runtime_profile(path, historical=False)


def load_k12_live_runtime_profile_v1(
        path: str | Path = HISTORICAL_RUNTIME_PROFILE_PATH, *,
        source_contract: HistoricalSourceContract | None = None,
        source_root: str | Path | None = None,
        source_reader: Callable[[str], bytes] | HistoricalSourceContract | None = None,
        source_revision: str = HISTORICAL_SOURCE_REVISION) -> K12RuntimeProfile:
    """Load `/1` only through an immutable-revision source contract."""
    contract = historical_source_contract(
        source_root=source_root, source_reader=source_reader,
        source_contract=source_contract, revision=source_revision,
    )
    return _load_runtime_profile(path, historical=True, source_contract=contract)


def load_k12_live_qualification_manifest(
        path: str | Path = QUALIFICATION_MANIFEST_PATH) -> dict[str, Any]:
    value = strict_json_load(Path(path), "K12 live qualification manifest")
    profile = load_k12_live_runtime_profile()
    if (value.get("artifact_id") != "minecraft-eac-k12-live-runtime-qualification"
            or value.get("artifact_version") != 2
            or value.get("schema_version") != LIVE_QUALIFICATION_IDENTITY
            or value.get("runtime_profile") != profile["profile_version"]
            or value.get("cell_count") != 15
            or tuple(value.get("schedule", ())) != tuple(profile["schedule"])
            or value.get("schedule_identity") != LIVE_SCHEDULE_IDENTITY
            or profile["contract_digests"]["qualification_manifest"]
            != value["detached_artifact_sha256"]):
        raise K12RuntimeProfileError("K12 live qualification manifest mismatch")
    return copy.deepcopy(value)


def load_k12_live_qualification_manifest_v1(
        path: str | Path = HISTORICAL_QUALIFICATION_MANIFEST_PATH, *,
        source_contract: HistoricalSourceContract | None = None,
        source_root: str | Path | None = None,
        source_reader: Callable[[str], bytes] | HistoricalSourceContract | None = None,
        source_revision: str = HISTORICAL_SOURCE_REVISION) -> dict[str, Any]:
    contract = historical_source_contract(
        source_root=source_root, source_reader=source_reader,
        source_contract=source_contract, revision=source_revision,
    )
    value = _historical_json_load(
        path, source_contract=contract,
        relative="configs/minecraft/k12-live-qualification-manifest-v1.json",
        label="historical K12 live qualification manifest",
    )
    profile = load_k12_live_runtime_profile_v1(source_contract=contract)
    if (value.get("artifact_id") != "minecraft-eac-k12-live-runtime-qualification"
            or value.get("artifact_version") != 1
            or value.get("schema_version") != HISTORICAL_QUALIFICATION_IDENTITY
            or value.get("runtime_profile") != HISTORICAL_LIVE_PROFILE_IDENTITY
            or value.get("cell_count") != 15
            or tuple(value.get("schedule", ())) != tuple(profile["schedule"])
            or value.get("schedule_identity") != LIVE_SCHEDULE_IDENTITY
            or profile["contract_digests"]["qualification_manifest"]
            != value["detached_artifact_sha256"]):
        raise K12RuntimeProfileError("historical K12 qualification manifest mismatch")
    return copy.deepcopy(value)


# Short names are aliases, not alternate loading paths.  Callers must use the
# same sealed profile and detached-digest checks.
load_live_runtime_profile = load_k12_live_runtime_profile
load_live_qualification_manifest = load_k12_live_qualification_manifest
load_k12_live_runtime_profile_v2 = load_k12_live_runtime_profile
load_k12_live_qualification_manifest_v2 = load_k12_live_qualification_manifest
load_k12_live_source_policy_v2 = load_k12_live_source_policy
load_live_runtime_profile_v1 = load_k12_live_runtime_profile_v1
load_live_qualification_manifest_v1 = load_k12_live_qualification_manifest_v1


__all__ = [
    "ENVIRONMENT_POLICY_PATH", "HISTORICAL_LIVE_PROFILE_IDENTITY",
    "HISTORICAL_QUALIFICATION_IDENTITY", "HISTORICAL_QUALIFICATION_MANIFEST_PATH",
    "HISTORICAL_RUNTIME_PROFILE_PATH", "HISTORICAL_SOURCE_REVISION",
    "HistoricalSourceContract", "HistoricalSourceRoot", "K12HistoricalSourceContract",
    "K12RuntimeProfile", "K12RuntimeProfileError",
    "K12SourcePolicy", "K12SourcePolicyEntry", "LIVE_PROFILE_IDENTITY",
    "LIVE_QUALIFICATION_IDENTITY", "SOURCE_SEMANTIC_CLASSES", "build_source_policy",
    "build_source_policy_artifact",
    "LIVE_SCHEDULE_IDENTITY", "QUALIFICATION_MANIFEST_PATH", "ROOT", "RUNTIME_PROFILE_PATH",
    "SOURCE_POLICY_PATH", "detached_digest", "load_k12_live_qualification_manifest",
    "load_k12_live_qualification_manifest_v1", "load_k12_live_runtime_profile",
    "load_k12_live_runtime_profile_v1", "load_k12_live_runtime_profile_v2",
    "load_k12_live_qualification_manifest_v2", "load_k12_live_source_policy",
    "load_k12_live_source_policy_v2", "historical_source_contract",
    "strict_json_load_bytes", "load_live_qualification_manifest",
    "load_live_qualification_manifest_v1", "load_live_runtime_profile", "load_live_runtime_profile_v1",
    "strict_json_load",
]
