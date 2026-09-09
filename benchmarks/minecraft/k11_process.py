"""Bounded subprocess supervision for Minecraft benchmark runs."""

from __future__ import annotations

import math
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Sequence


def supervise_process(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    artifact_ready_path: str | os.PathLike[str] | None = None,
    completion_grace_seconds: float = 0.5,
    termination_grace_seconds: float = 0.5,
    kill_grace_seconds: float = 0.5,
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, int | float | bool | None]:
    """Run *command* in an isolated session and clean up its whole process group."""
    if os.name != "posix":
        raise OSError("the K11 process supervisor requires POSIX process groups")
    timeout_seconds = _positive_finite(timeout_seconds, "timeout_seconds")
    completion_grace_seconds = _nonnegative_finite(completion_grace_seconds, "completion_grace_seconds")
    termination_grace_seconds = _nonnegative_finite(
        termination_grace_seconds, "termination_grace_seconds"
    )
    kill_grace_seconds = _nonnegative_finite(kill_grace_seconds, "kill_grace_seconds")
    artifact_path = Path(artifact_ready_path) if artifact_ready_path is not None else None

    started = time.monotonic()
    process = subprocess.Popen(
        list(command), cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    process_group_id = process.pid
    term_sent = False
    kill_sent = False
    timed_out = False
    post_artifact_linger = False
    post_parent_group_linger = False
    lingering_processes_before_cleanup: list[dict[str, int | str]] = []

    try:
        deadline = started + timeout_seconds
        artifact_seen_at = None
        while process.poll() is None:
            now = time.monotonic()
            if _artifact_exists(artifact_path):
                artifact_seen_at = artifact_seen_at or now
                if now - artifact_seen_at >= completion_grace_seconds:
                    post_artifact_linger = True
                    break
            if now >= deadline:
                timed_out = True
                break
            time.sleep(min(0.02, max(0.0, deadline - now)))

        if process.poll() is None:
            term_sent = _signal_group(process_group_id, signal.SIGTERM)
            _wait_for_group_exit(process_group_id, termination_grace_seconds)
            if _group_exists(process_group_id):
                kill_sent = _signal_group(process_group_id, signal.SIGKILL)
                _wait_for_group_exit(process_group_id, kill_grace_seconds)
        try:
            process.wait(timeout=kill_grace_seconds)
        except subprocess.TimeoutExpired:
            pass
    finally:
        # Capture the census after the leader has exited, but before any signal
        # is sent.  In particular, killpg(2) alone cannot distinguish a stale
        # group leader from a genuinely lingering worker.
        lingering_processes_before_cleanup = process_group_census(process_group_id)
        post_parent_group_linger = any(
            entry["pid"] != process.pid and entry["state"] != "Z"
            for entry in lingering_processes_before_cleanup
        )
        if _group_exists(process_group_id):
            if not term_sent:
                term_sent = _signal_group(process_group_id, signal.SIGTERM)
                _wait_for_group_exit(process_group_id, termination_grace_seconds)
            if _group_exists(process_group_id) and not kill_sent:
                kill_sent = _signal_group(process_group_id, signal.SIGKILL)
                _wait_for_group_exit(process_group_id, kill_grace_seconds)

    return {
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "artifact_ready": _artifact_exists(artifact_path),
        "post_artifact_linger": post_artifact_linger,
        "post_parent_group_linger": post_parent_group_linger,
        "lingering_processes_before_cleanup": lingering_processes_before_cleanup,
        "term_sent": term_sent,
        "kill_sent": kill_sent,
        "duration": time.monotonic() - started,
        "process_group_alive_after_cleanup": _group_exists(process_group_id),
    }


run_bounded_process = supervise_process


def _artifact_exists(path: Path | None) -> bool:
    return path is not None and path.exists()


def process_group_census(process_group_id: int) -> list[dict[str, int | str]]:
    """Return identity-only metadata for members of a process group.

    Linux exposes the required fields in ``/proc/<pid>/stat``.  Deliberately
    do not inspect cmdline, environ, or any other potentially sensitive data.
    Processes which disappear while the census is being collected are simply
    omitted.
    """
    if os.name != "posix":
        return []
    result: list[dict[str, int | str]] = []
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError:
        return result
    for entry in entries:
        if not entry.name.isdigit():
            continue
        metadata = _read_process_identity(int(entry.name))
        if metadata is None or metadata["pgid"] != process_group_id:
            continue
        result.append(metadata)
    return sorted(result, key=lambda item: int(item["pid"]))


def _read_process_identity(pid: int) -> dict[str, int | str] | None:
    entry = Path("/proc") / str(pid)
    try:
        stat = (entry / "stat").read_text(encoding="ascii")
        prefix, suffix = stat.rsplit(") ", 1)
        fields = suffix.split()
        if len(fields) < 20:
            return None
        return {
            "pid": int(prefix.split(" ", 1)[0]),
            "ppid": int(fields[1]),
            "pgid": int(fields[2]),
            "state": fields[0],
            "executable": os.path.basename(os.readlink(entry / "exe")),
            "start_ticks": int(fields[19]),
        }
    except (OSError, ValueError, UnicodeError):
        return None


def cleanup_process_group_descendants(
    process_group_id: int | None = None,
    *,
    termination_grace_seconds: float = 0.5,
    kill_grace_seconds: float = 0.5,
) -> dict[str, object]:
    """Clean group members other than the session leader before it exits.

    This is intended for a worker running as its own session leader: signaling
    the group would also terminate the worker, so members are signaled by PID.
    The returned census and flags are JSON-serializable worker-artifact data.
    """
    termination_grace_seconds = _nonnegative_finite(
        termination_grace_seconds, "termination_grace_seconds"
    )
    kill_grace_seconds = _nonnegative_finite(kill_grace_seconds, "kill_grace_seconds")
    group_id = os.getpgrp() if process_group_id is None else int(process_group_id)
    leader_pid = os.getpid()
    if (
        leader_pid != os.getpgrp()
        or leader_pid != os.getsid(0)
        or group_id != os.getpgrp()
    ):
        raise RuntimeError("descendant cleanup requires the caller to lead its own session")
    before = [item for item in process_group_census(group_id) if item["pid"] != leader_pid]
    term_sent = _signal_members(before, signal.SIGTERM)
    _wait_for_members(group_id, leader_pid, termination_grace_seconds)
    remaining = [item for item in process_group_census(group_id) if item["pid"] != leader_pid]
    kill_sent = _signal_members(remaining, signal.SIGKILL)
    _wait_for_members(group_id, leader_pid, kill_grace_seconds)
    return {
        "lingering_processes_before_cleanup": before,
        "term_sent": term_sent,
        "kill_sent": kill_sent,
        "processes_after_cleanup": [
            item for item in process_group_census(group_id) if item["pid"] != leader_pid
        ],
    }


def _signal_members(members: list[dict[str, int | str]], sent_signal: int) -> bool:
    sent = False
    for member in members:
        current = _read_process_identity(int(member["pid"]))
        if (
            current is None
            or current["pgid"] != member["pgid"]
            or current["start_ticks"] != member["start_ticks"]
        ):
            continue
        try:
            os.kill(int(member["pid"]), sent_signal)
            sent = True
        except (ProcessLookupError, PermissionError):
            pass
    return sent


def _wait_for_members(group_id: int, leader_pid: int, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not any(item["pid"] != leader_pid for item in process_group_census(group_id)):
            return
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))


def _group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _signal_group(process_group_id: int, sent_signal: int) -> bool:
    try:
        os.killpg(process_group_id, sent_signal)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _wait_for_group_exit(process_group_id: int, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while _group_exists(process_group_id) and time.monotonic() < deadline:
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))


def _positive_finite(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


def _nonnegative_finite(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return value
