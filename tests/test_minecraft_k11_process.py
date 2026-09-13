import json
import os
import signal
import subprocess
import sys
import time

import pytest

from benchmarks.minecraft.k11_process import cleanup_process_group_descendants, supervise_process


pytestmark = pytest.mark.skipif(os.name != "posix", reason="K11 supervisor requires POSIX process groups")


def test_natural_exit_returns_structured_metadata(tmp_path):
    result = supervise_process([sys.executable, "-c", "pass"], timeout_seconds=2)
    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    assert result["term_sent"] is False
    assert result["kill_sent"] is False
    assert result["post_parent_group_linger"] is False
    assert result["lingering_processes_before_cleanup"] == []
    assert result["process_group_alive_after_cleanup"] is False
    json.dumps(result)


def test_blocking_child_is_terminated_and_then_killed(tmp_path):
    result = supervise_process(
        [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
        timeout_seconds=0.1, termination_grace_seconds=0.05, kill_grace_seconds=0.5,
    )
    assert result["timed_out"] is True
    assert result["term_sent"] is True
    assert result["kill_sent"] is True
    assert result["exit_code"] == -signal.SIGKILL
    assert result["process_group_alive_after_cleanup"] is False


def test_descendant_is_cleaned_with_the_process_group(tmp_path):
    descendant_pid = tmp_path / "descendant.pid"
    descendant_code = (
        "import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open({str(descendant_pid)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    )
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {descendant_code!r}]); time.sleep(60)"
    )
    result = supervise_process(
        [sys.executable, "-c", parent_code], timeout_seconds=0.1,
        termination_grace_seconds=0.05, kill_grace_seconds=0.5,
    )
    pid = _wait_for_pid(descendant_pid)
    assert result["timed_out"] is True
    assert result["kill_sent"] is True
    assert result["process_group_alive_after_cleanup"] is False
    assert _pid_gone(pid)


def test_descendant_is_cleaned_after_parent_exits(tmp_path):
    descendant_pid = tmp_path / "orphan.pid"
    descendant_code = (
        "import os, time; "
        f"open({str(descendant_pid)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    )
    parent_code = (
        "import os, subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {descendant_code!r}]); "
        f"p={str(descendant_pid)!r}; "
        "exec('while not os.path.exists(p): time.sleep(0.01)')"
    )

    result = supervise_process(
        [sys.executable, "-c", parent_code], timeout_seconds=2,
        termination_grace_seconds=0.1, kill_grace_seconds=0.5,
    )

    pid = _wait_for_pid(descendant_pid)
    assert result["timed_out"] is False
    assert result["post_parent_group_linger"] is True
    census = result["lingering_processes_before_cleanup"]
    assert any(item["pid"] == pid for item in census)
    assert all(
        set(item) == {"pid", "ppid", "pgid", "state", "executable", "start_ticks"}
        for item in census
    )
    assert result["term_sent"] is True
    assert result["process_group_alive_after_cleanup"] is False
    assert _pid_gone(pid)


def test_worker_can_clean_descendants_before_natural_exit(tmp_path):
    descendant_pid = tmp_path / "worker-child.pid"
    descendant_code = (
        "import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open({str(descendant_pid)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    )
    worker_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {descendant_code!r}]); "
        f"p={str(descendant_pid)!r}; exec('while not __import__(\"os\").path.exists(p): time.sleep(0.01)'); "
        "from benchmarks.minecraft.k11_process import cleanup_process_group_descendants; "
        "cleanup_process_group_descendants(termination_grace_seconds=0.02, kill_grace_seconds=0.2)"
    )
    result = supervise_process([sys.executable, "-c", worker_code], timeout_seconds=2)

    pid = _wait_for_pid(descendant_pid)
    assert result["exit_code"] == 0
    assert result["post_parent_group_linger"] is False
    assert _pid_gone(pid)


def test_artifact_then_lingering_child_is_escalated_without_removing_artifact(tmp_path):
    artifact = tmp_path / "result.json"
    code = (
        "import json, time; "
        f"open({str(artifact)!r}, 'w').write(json.dumps({{'score': 1}})); time.sleep(60)"
    )
    result = supervise_process(
        [sys.executable, "-c", code], timeout_seconds=0.1,
        artifact_ready_path=artifact, completion_grace_seconds=0.05,
        termination_grace_seconds=0.05, kill_grace_seconds=0.5,
    )
    assert result["artifact_ready"] is True
    assert result["post_artifact_linger"] is True
    assert result["term_sent"] is True
    assert artifact.read_text(encoding="utf-8") == '{"score": 1}'
    assert result["process_group_alive_after_cleanup"] is False


def test_descendant_cleanup_rejects_non_leader_caller():
    with pytest.raises(RuntimeError, match="lead its own session"):
        cleanup_process_group_descendants(process_group_id=os.getpgrp() + 1)


def test_descendant_cleanup_rejects_group_leader_in_shared_session():
    code = (
        "import os\n"
        "os.setpgrp()\n"
        "from benchmarks.minecraft.k11_process import cleanup_process_group_descendants\n"
        "try:\n"
        "    cleanup_process_group_descendants()\n"
        "except RuntimeError:\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(1)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], check=False)
    assert result.returncode == 0


def _wait_for_pid(path, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return int(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            time.sleep(0.01)
    pytest.fail(f"timed out waiting for {path}")


def _pid_gone(pid):
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.01)
    return False
