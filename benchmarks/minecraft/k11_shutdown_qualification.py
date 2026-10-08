"""Development-only K11 process-group shutdown qualification."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from benchmarks.minecraft.k11_process import (
    cleanup_process_group_descendants,
    supervise_process,
)


def _category(process: Mapping[str, Any]) -> str:
    executable = str(process.get("executable", "")).casefold()
    if "node" in executable:
        return "Node.js/Mineflayer"
    if "python" in executable:
        return "Minecraft bridge/client"
    return "other"


def _worker(output_root: Path) -> int:
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Node.js is required for shutdown qualification")
    node_pid_path = output_root / "node.pid"
    node_code = (
        "require('fs').writeFileSync(process.argv[1], String(process.pid));"
        "setInterval(() => {}, 60000);"
    )
    bridge_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([{node!r}, '-e', {node_code!r}, {str(node_pid_path)!r}]); "
        "time.sleep(60)"
    )
    subprocess.Popen([sys.executable, "-c", bridge_code])
    deadline = time.monotonic() + 5
    while not node_pid_path.is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not node_pid_path.is_file():
        raise RuntimeError("synthetic Node descendant did not start")
    cleanup = cleanup_process_group_descendants(
        termination_grace_seconds=0.5,
        kill_grace_seconds=0.5,
    )
    cleanup["classified_processes_before_cleanup"] = [
        {**process, "category": _category(process)}
        for process in cleanup["lingering_processes_before_cleanup"]
    ]
    (output_root / "worker_cleanup.json").write_text(
        json.dumps(cleanup, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


def run_qualification(output_root: str | Path) -> dict[str, Any]:
    root = Path(output_root).resolve()
    if root.exists():
        raise ValueError(f"shutdown qualification output root already exists: {root}")
    root.mkdir(parents=True)
    supervision = supervise_process(
        [sys.executable, "-m", "benchmarks.minecraft.k11_shutdown_qualification",
         "--output-root", str(root), "--worker"],
        timeout_seconds=10,
        artifact_ready_path=root / "worker_cleanup.json",
        completion_grace_seconds=2,
        termination_grace_seconds=1,
        kill_grace_seconds=1,
    )
    cleanup_path = root / "worker_cleanup.json"
    cleanup = json.loads(cleanup_path.read_text(encoding="utf-8")) if cleanup_path.is_file() else None
    categories = {
        item.get("category") for item in (cleanup or {}).get(
            "classified_processes_before_cleanup", []
        )
    }
    gate_passed = bool(
        cleanup
        and cleanup.get("lingering_processes_before_cleanup")
        and {"Minecraft bridge/client", "Node.js/Mineflayer"}.issubset(categories)
        and cleanup.get("processes_after_cleanup") == []
        and supervision.get("exit_code") == 0
        and supervision.get("timed_out") is False
        and supervision.get("post_parent_group_linger") is False
        and supervision.get("process_group_alive_after_cleanup") is False
    )
    artifact = {
        "artifact_id": "minecraft-k11-shutdown-qualification",
        "artifact_version": 1,
        "development_only": True,
        "formal_p0": False,
        "prevalence_inference_allowed": False,
        "qualification_scope": "synthetic Python-bridge to Node descendant tree",
        "required_categories": ["Minecraft bridge/client", "Node.js/Mineflayer"],
        "worker_cleanup": cleanup,
        "process_supervision": supervision,
        "gate_b_passed": gate_passed,
    }
    (root / "SHUTDOWN_QUALIFICATION.json").write_text(
        json.dumps(artifact, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return artifact


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run development-only K11 shutdown qualification")
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args(argv)
    if args.worker:
        return _worker(Path(args.output_root).resolve())
    artifact = run_qualification(args.output_root)
    print(json.dumps(artifact, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if artifact["gate_b_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
