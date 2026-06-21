import re
import subprocess
from pathlib import Path

import yaml

from benchmarks.craft.dual_dag.schema import DUAL_DAG_SCHEMA_VERSION


def standard_run_name(*parts: object) -> str:
    text = "_".join(str(part) for part in parts if part not in (None, ""))
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text.strip())
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "experiment_run"


def write_provenance(
    output_dir: Path,
    *,
    benchmark: str,
    command: str,
    resolved_config,
    environment_notes: str = "",
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    commit = _git_commit()
    provenance = {
        "schema_version": DUAL_DAG_SCHEMA_VERSION,
        "benchmark": benchmark,
        "commit": commit,
        "command": command,
        "environment_notes": environment_notes,
    }
    (output_dir / "command.txt").write_text(command + "\n", encoding="utf-8")
    _write_resolved_config(output_dir, resolved_config)
    with (output_dir / "provenance.json").open("w", encoding="utf-8") as f:
        import json

        json.dump(provenance, f, indent=2)
        f.write("\n")
    return provenance


def _write_resolved_config(output_dir: Path, resolved_config) -> None:
    if isinstance(resolved_config, dict):
        with (output_dir / "config.resolved.json").open("w", encoding="utf-8") as f:
            import json

            json.dump(resolved_config, f, indent=2)
            f.write("\n")
    with (output_dir / "config.resolved.yaml").open("w", encoding="utf-8") as f:
        yaml.safe_dump(resolved_config, f, sort_keys=False, allow_unicode=True)


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return "unknown"
    return result.stdout.strip() or "unknown"
