"""Deterministic containment state machine; it never touches the host."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
from typing import Protocol


class ContainmentError(RuntimeError):
    pass


def containment_identity(cell_id: str, launch_id: str) -> tuple[str, str]:
    """Return stable, collision-resistant unit and cgroup identities."""
    if not isinstance(cell_id, str) or not cell_id or not isinstance(launch_id, str) or not launch_id:
        raise ValueError("cell_id and launch_id are required")
    token = hashlib.sha256(f"{cell_id}\0{launch_id}".encode()).hexdigest()[:32]
    return f"minecraft-k12-{token}.service", f"/minecraft-k12/{token}"


class ContainmentState(str, Enum):
    RUNNING = "running"
    TERM_SENT = "term_sent"
    KILL_SENT = "kill_sent"
    QUARANTINED = "quarantined"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"


@dataclass(frozen=True, slots=True)
class InfraFailure:
    reason: str
    unit_id: str
    deadline_ns: int


@dataclass(frozen=True, slots=True)
class QuarantineRecord:
    unit_id: str
    cgroup_id: str
    reason: str
    cgroup_empty: bool
    immutable: bool = True


class ContainmentClient(Protocol):
    unit_id: str
    cgroup_id: str
    signals: list[str]
    def term(self) -> None: ...
    def kill(self) -> None: ...
    def cgroup_empty(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class ContainmentResult:
    unit_id: str
    cgroup_id: str
    state: ContainmentState
    term_sent: bool
    kill_sent: bool
    cgroup_empty: bool
    blocked_next_launch: bool
    failures: tuple[InfraFailure, ...]
    quarantine: QuarantineRecord | None = None


class FakeSystemdClient:
    """In-memory fake, intentionally incapable of starting processes or talking to systemd."""
    def __init__(self, unit_id: str, cgroup_id: str | None = None) -> None:
        if not unit_id or not isinstance(unit_id, str):
            raise ValueError("unit_id is required")
        self.unit_id = unit_id
        self.cgroup_id = cgroup_id or f"k12-{unit_id}"
        self.signals: list[str] = []
        self.empty = True
        self.escaped_descendants = False
        self.unknown_descendants = False

    def term(self) -> None: self.signals.append("TERM")
    def kill(self) -> None: self.signals.append("KILL")
    def cgroup_empty(self) -> bool: return self.empty and not self.escaped_descendants and not self.unknown_descendants


class ContainmentController:
    def __init__(self, client: ContainmentClient, *, deadline_ns: int) -> None:
        if not isinstance(deadline_ns, int) or isinstance(deadline_ns, bool):
            raise TypeError("integer deadline is required")
        for name in ("unit_id", "cgroup_id", "term", "kill", "cgroup_empty"):
            if not hasattr(client, name):
                raise TypeError("client does not implement containment interface")
        self.client = client
        self.deadline_ns = deadline_ns
        self.state = ContainmentState.RUNNING
        self.failures: list[InfraFailure] = []
        self.blocked_next_launch = False
        self._quarantine = False
        self._quarantine_record: QuarantineRecord | None = None

    def _fail(self, reason: str) -> None:
        self.failures.append(InfraFailure(reason, self.client.unit_id, self.deadline_ns))
        self.state = ContainmentState.INFRASTRUCTURE_FAILURE
        self.blocked_next_launch = True

    def _quarantine_now(self, reason: str = "contained") -> None:
        self._quarantine = True
        self.state = ContainmentState.QUARANTINED
        self._quarantine_record = QuarantineRecord(self.client.unit_id, self.client.cgroup_id,
                                                   reason, self.client.cgroup_empty())

    def contain(self, *, now_ns: int) -> ContainmentResult:
        if self._quarantine:
            return self.result()
        if self.state is ContainmentState.INFRASTRUCTURE_FAILURE:
            return self.result()
        if type(now_ns) is not int:
            raise TypeError("now_ns must be an integer")
        if self.state is ContainmentState.RUNNING:
            self.client.term(); self.state = ContainmentState.TERM_SENT
        if self.client.cgroup_empty():
            self._quarantine_now()
            return self.result()
        if now_ns < self.deadline_ns:
            return self.result()
        self.client.kill(); self.state = ContainmentState.KILL_SENT
        if not self.client.cgroup_empty():
            self._fail("cgroup_not_empty_after_kill")
            return self.result()
        self._quarantine_now()
        return self.result()

    def result(self) -> ContainmentResult:
        return ContainmentResult(self.client.unit_id, self.client.cgroup_id, self.state,
                                 "TERM" in self.client.signals, "KILL" in self.client.signals,
                                 self.client.cgroup_empty(), self.blocked_next_launch, tuple(self.failures),
                                 self._quarantine_record)

    def assert_launch_allowed(self) -> None:
        if self.blocked_next_launch or self.state is not ContainmentState.QUARANTINED:
            raise ContainmentError("launch, retry, and replacement are blocked")


__all__ = ["ContainmentClient", "ContainmentController", "ContainmentError", "ContainmentResult", "ContainmentState", "FakeSystemdClient", "InfraFailure", "QuarantineRecord", "containment_identity"]
