from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from env.runtime_execution import (
    DEFAULT_RUNTIME_ASSETS,
    RuntimeAssetError,
    RuntimeAssetSpec,
    RuntimeExecution,
)
from env.runtime_paths import RuntimePaths


def _execution(tmp_path: Path, content: str = "ok") -> RuntimeExecution:
    (tmp_path / "entry.py").write_text(content, encoding="utf-8")
    return RuntimeExecution.resolve(tmp_path, [RuntimeAssetSpec("entry", "entry.py")])


def _minimal_default_root(root: Path) -> None:
    for spec in DEFAULT_RUNTIME_ASSETS:
        path = root / spec.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}" if path.suffix == ".json" else "", encoding="utf-8")


def test_resolution_and_commands_are_independent_of_cwd(tmp_path, monkeypatch):
    execution = _execution(tmp_path)
    monkeypatch.chdir("/")
    assert execution.asset("entry").relative_path == "entry.py"
    assert execution.python_command("entry")[1] == str(tmp_path / "entry.py")
    assert execution.public_command("entry") == ["python", "entry.py"]


def test_manifest_is_deterministic_and_verify_detects_drift(tmp_path):
    first = _execution(tmp_path)
    second = _execution(tmp_path)
    assert first.manifest_sha256 == second.manifest_sha256
    (tmp_path / "entry.py").write_text("changed", encoding="utf-8")
    with pytest.raises(RuntimeAssetError) as error:
        first.verify("entry")
    assert error.value.code == "identity_drift"
    assert str(tmp_path) not in str(error.value)


def test_invalid_asset_locations_are_rejected(tmp_path):
    outside = tmp_path.parent / "outside.py"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(RuntimeAssetError) as error:
        RuntimeExecution.resolve(tmp_path, [RuntimeAssetSpec("bad", "../outside.py")])
    assert error.value.code == "outside"

    link = tmp_path / "link.py"
    link.symlink_to(outside)
    with pytest.raises(RuntimeAssetError) as error:
        RuntimeExecution.resolve(tmp_path, [RuntimeAssetSpec("link", "link.py")])
    assert error.value.code == "symlink"


def test_child_kwargs_preserves_runtime_root(tmp_path):
    execution = _execution(tmp_path)
    runtime_root = tmp_path / "attempt"
    runtime_root.mkdir()
    kwargs = execution.child_kwargs(
        RuntimePaths.isolated(runtime_root), {"BASE": "1", "NODE_PATH": "/hostile/node"}
    )
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["env"]["VILLAGER_RUNTIME_ROOT"] == str(runtime_root.resolve())
    assert kwargs["env"]["PYTHONPATH"] == str(tmp_path)
    assert "NODE_PATH" not in kwargs["env"]


def test_child_ignores_hostile_cwd_and_inherited_pythonpath(tmp_path):
    execution_root = tmp_path / "execution"
    execution_root.mkdir()
    (execution_root / "approved_module.py").write_text("VALUE = 'approved'\n", encoding="utf-8")
    (execution_root / "entry.py").write_text(
        "from approved_module import VALUE\nprint(VALUE)\n", encoding="utf-8"
    )
    hostile = tmp_path / "artifact output [hostile]"
    hostile.mkdir()
    (hostile / "approved_module.py").write_text("VALUE = 'hostile'\n", encoding="utf-8")
    execution = RuntimeExecution.resolve(
        execution_root,
        [RuntimeAssetSpec("entry", "entry.py"), RuntimeAssetSpec("module", "approved_module.py")],
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(hostile)

    result = subprocess.run(
        execution.python_command("entry"),
        cwd=hostile,
        env=execution.child_environment(RuntimePaths.isolated(tmp_path / "runtime"), environment),
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == "approved\n"


@pytest.mark.parametrize("kind", ["missing", "directory", "fifo"])
def test_non_regular_runtime_assets_fail_closed(tmp_path, kind):
    path = tmp_path / "entry.py"
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO creation is unavailable")
        os.mkfifo(path)

    with pytest.raises(RuntimeAssetError) as error:
        RuntimeExecution.resolve(tmp_path, [RuntimeAssetSpec("entry", "entry.py")])

    assert error.value.code in {"missing", "wrong_type"}
    assert str(tmp_path) not in str(error.value)


def test_invalid_specs_and_symlinked_roots_are_rejected(tmp_path):
    execution_root = tmp_path / "execution"
    execution_root.mkdir()
    (execution_root / "entry.py").write_text("ok", encoding="utf-8")
    root_link = tmp_path / "linked-root"
    root_link.symlink_to(execution_root, target_is_directory=True)

    with pytest.raises(RuntimeAssetError, match="symlink"):
        RuntimeExecution.resolve(root_link, [RuntimeAssetSpec("entry", "entry.py")])
    with pytest.raises(RuntimeAssetError, match="invalid_spec"):
        RuntimeExecution.resolve(
            execution_root,
            [RuntimeAssetSpec("entry", "entry.py"), RuntimeAssetSpec("entry", "entry.py")],
        )
    with pytest.raises(RuntimeAssetError, match="outside"):
        RuntimeExecution.resolve(execution_root, [RuntimeAssetSpec("entry", "/entry.py")])


def test_default_manifest_covers_runtime_import_closure():
    repository_root = Path(__file__).resolve().parents[1]
    execution = RuntimeExecution.resolve(repository_root)

    for relative_path in (
        "model/init_model.py",
        "pipeline/controller_prompt.py",
        "pipeline/runtime_events.py",
        "type_define/graph.py",
        "env/minecraft_dual_dag.py",
        "rl_env/minecraft_rl_env.py",
    ):
        assert f"source:{relative_path}" in execution.assets
    assert execution.asset("speaking_style").relative_path == "speaking_style.py"


def test_default_closure_rejects_new_root_packages(tmp_path):
    _minimal_default_root(tmp_path)
    package = tmp_path / "json"
    package.mkdir()
    (package / "__init__.py").write_text("raise RuntimeError('hostile')\n", encoding="utf-8")

    with pytest.raises(RuntimeAssetError, match="unexpected_root_entry"):
        RuntimeExecution.resolve(tmp_path)


@pytest.mark.parametrize("module_name", ["json.py", "requests.py"])
def test_default_closure_rejects_unexpected_root_modules(tmp_path, module_name):
    _minimal_default_root(tmp_path)
    (tmp_path / module_name).write_text("raise RuntimeError('hostile')\n", encoding="utf-8")

    with pytest.raises(RuntimeAssetError, match="unexpected_root_entry"):
        RuntimeExecution.resolve(tmp_path)


def test_allowlisted_root_runtime_module_resolves(tmp_path):
    _minimal_default_root(tmp_path)

    execution = RuntimeExecution.resolve(tmp_path)

    assert execution.asset("start_with_config").relative_path == "start_with_config.py"
    assert execution.asset("config").relative_path == "config.py"
    assert execution.asset("speaking_style").relative_path == "speaking_style.py"


def test_allowlisted_non_runtime_root_file_does_not_change_identity(tmp_path):
    _minimal_default_root(tmp_path)
    first = RuntimeExecution.resolve(tmp_path)
    (tmp_path / "proposal.md").write_text("local notes\n", encoding="utf-8")

    second = RuntimeExecution.resolve(tmp_path)

    assert second.manifest_sha256 == first.manifest_sha256
    first.verify()


def test_allowlisted_results_directory_is_non_runtime_and_deterministic(tmp_path):
    _minimal_default_root(tmp_path)
    archive = tmp_path / "results" / "archived-study"
    archive.mkdir(parents=True)
    (archive / "evidence.json").write_text('{"status": "archived"}\n', encoding="utf-8")

    first = RuntimeExecution.resolve(tmp_path)
    second = RuntimeExecution.resolve(tmp_path)

    assert second.manifest_sha256 == first.manifest_sha256
    assert second.assets == first.assets
    assert "results" not in first.assets
    assert all(not asset.relative_path.startswith("results/") for asset in first.assets.values())


def test_default_closure_identities_installed_node_dependencies(tmp_path):
    _minimal_default_root(tmp_path)
    dependency = tmp_path / "node_modules" / "mineflayer" / "index.js"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("module.exports = 'approved';\n", encoding="utf-8")
    execution = RuntimeExecution.resolve(tmp_path)

    dependency.write_text("module.exports = 'changed';\n", encoding="utf-8")

    with pytest.raises(RuntimeAssetError, match="identity_drift"):
        execution.verify()


def test_default_closure_rejects_symlinked_node_packages(tmp_path):
    execution_root = tmp_path / "execution"
    execution_root.mkdir()
    _minimal_default_root(execution_root)
    outside = tmp_path / "hostile-node-package"
    outside.mkdir()
    (outside / "index.js").write_text("module.exports = 'hostile';\n", encoding="utf-8")
    node_modules = execution_root / "node_modules"
    node_modules.mkdir()
    (node_modules / "mineflayer").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeAssetError, match="symlink"):
        RuntimeExecution.resolve(execution_root)
