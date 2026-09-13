"""Offline K12 worker boundary.

The worker is intentionally boring: it receives one frozen cell description and
can only serialize untrusted worker messages.  Campaign identity, provenance,
and all scientific decisions remain parent responsibilities.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping

from benchmarks.common.eac.canonical import canonical_bytes
from .k12_worker_protocol import MAX_MESSAGE_BYTES, WORKER_SCHEMA, EVENTS, PARENT_ONLY_EVENTS
from .k12_identity import FROZEN_ARGUMENT_SPECS


class K12WorkerError(ValueError):
    pass


_FORBIDDEN_PAYLOAD_KEYS = frozenset({
    "source", "sequence", "received_monotonic_ns", "previous_digest",
    "message_digest", "trace_digest", "schema_version", "provenance",
    "oracle", "recovered", "containment", "digest", "disposition", "authority",
    "reset_invalid", "runtime_failure", "containment_failure", "budget_exhausted",
    "reset_generation", "generation",
    "steps", "model_calls", "evidence_calls", "effect_attempts",
})
_SEMANTIC_KEYS = frozenset(key for spec in FROZEN_ARGUMENT_SPECS.values() for key in spec)


def _assert_worker_payload(value: Any) -> None:
    if isinstance(value, Mapping):
        keys = {str(key).lower() for key in value}
        if (_FORBIDDEN_PAYLOAD_KEYS.intersection(keys)
                or any(key.startswith("budget") or (key not in _SEMANTIC_KEYS and key.endswith(("_count", "_counts", "_total", "_totals")))
                       for key in keys)):
            raise K12WorkerError("worker cannot author parent-owned fields")
        for child in value.values():
            _assert_worker_payload(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_worker_payload(child)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


@dataclass(frozen=True, slots=True)
class CellManifest:
    cell_id: str
    triplet_id: str
    stratum: str
    template: int
    seed: int
    arm: str
    arm_permutation: tuple[str, str, str]
    randomization_digest: str

    @classmethod
    def from_cell(cls, cell: Any) -> "CellManifest":
        names = ("cell_id", "triplet_id", "stratum", "template", "seed", "arm",
                 "arm_permutation", "randomization_digest")
        try:
            values = ({name: cell[name] for name in names} if isinstance(cell, Mapping)
                      else {name: getattr(cell, name) for name in names})
        except (AttributeError, KeyError, TypeError) as exc:
            raise K12WorkerError("complete immutable cell manifest required") from exc
        values["arm_permutation"] = tuple(values["arm_permutation"])
        if len(values["arm_permutation"]) != 3 or values["arm"] not in values["arm_permutation"]:
            raise K12WorkerError("invalid arm sequence")
        if not all(isinstance(values[n], str) and values[n] for n in ("cell_id", "triplet_id", "stratum", "arm", "randomization_digest")):
            raise K12WorkerError("cell identity fields are required")
        if type(values["template"]) is not int or type(values["seed"]) is not int or values["template"] <= 0 or values["seed"] <= 0:
            raise K12WorkerError("manifest coordinates are invalid")
        if values["arm"] not in {"A", "R", "S"}:
            raise K12WorkerError("manifest arm is invalid")
        return cls(**values)

    def as_dict(self) -> dict[str, Any]:
        return {"cell_id": self.cell_id, "triplet_id": self.triplet_id,
                "stratum": self.stratum, "template": self.template, "seed": self.seed,
                "arm": self.arm, "arm_permutation": list(self.arm_permutation),
                "randomization_digest": self.randomization_digest}


class K12Worker:
    """A deterministic serializer, not a process or Minecraft adapter."""

    def __init__(self, manifest: CellManifest | Any, *, worker_id: str) -> None:
        self.manifest = CellManifest.from_cell(manifest)
        if not isinstance(worker_id, str) or not worker_id:
            raise K12WorkerError("worker_id is required")
        self.worker_id = worker_id
        self._active_operation_ids: dict[str, str] = {}
        self._operation_serial = 0

    def message(self, event: str, payload: Mapping[str, Any] | None = None) -> bytes:
        if event not in EVENTS:
            raise K12WorkerError("unknown worker event")
        if (event in PARENT_ONLY_EVENTS
                or (event == "recovery_proposed" and self.manifest.arm != "R")
                or (self.manifest.arm != "A"
                    and event in {"effect_decision", "effect_entered", "effect_terminal"})):
            raise K12WorkerError("worker event crosses the parent authority boundary")
        body = dict(payload or {})
        operation_kinds = {
            "model_call_admitted": "model_call", "model_call_terminal": "model_call",
            "observation_started": "observation", "observation_terminal": "observation",
            "effect_entered": "effect", "effect_terminal": "effect",
            "recovery_step": "recovery_step", "recovery_step_terminal": "recovery_step",
            "evidence_ingested": "evidence",
        }
        kind = operation_kinds.get(event)
        is_start = event.endswith(("admitted", "started", "entered")) or event == "recovery_step"
        is_terminal = event.endswith("terminal")
        if kind and "operation_id" not in body:
            supplied = body.get({"observation": "observation_id", "effect": "effect_id",
                                 "recovery_step": "step_id", "evidence": "evidence_id"}.get(kind, "model_call_id"))
            if isinstance(supplied, str) and supplied:
                operation_id = supplied
            elif is_terminal and kind in self._active_operation_ids:
                operation_id = self._active_operation_ids[kind]
            else:
                self._operation_serial += 1
                operation_id = f"{kind}-{self._operation_serial}"
            body["operation_id"] = operation_id
            if is_start:
                self._active_operation_ids[kind] = operation_id
            elif is_terminal:
                self._active_operation_ids.pop(kind, None)
        elif kind:
            operation_id = body["operation_id"]
            if is_start:
                self._active_operation_ids[kind] = operation_id
            elif is_terminal:
                self._active_operation_ids.pop(kind, None)
        _assert_worker_payload(body)
        canonical_bytes(body)
        raw = json.dumps({"schema": WORKER_SCHEMA, "worker_id": self.worker_id,
                          "cell_id": self.manifest.cell_id, "triplet_id": self.manifest.triplet_id,
                          "arm": self.manifest.arm, "event": event, "payload": body},
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_MESSAGE_BYTES:
            raise K12WorkerError("worker message exceeds byte bound")
        return raw

    def run(self, outcome: str = "succeeded", payload: Mapping[str, Any] | None = None) -> tuple[bytes, ...]:
        """Produce a bounded offline stream; ``outcome`` is supplied by a fake seam."""
        if outcome not in {"worker_terminal_candidate", "succeeded", "failed", "cancelled"}:
            raise K12WorkerError("outcome must be a deterministic worker scenario")
        candidate_payload = dict(payload or {})
        if outcome != "worker_terminal_candidate":
            candidate_payload.setdefault("worker_status", outcome)
        original = {
            "request_id": candidate_payload.get("original_request_id", "original-request"),
            "candidate_id": candidate_payload.get("original_candidate_id", "original-candidate"),
            "attempt_id": candidate_payload.get("original_attempt_id", "original-attempt"),
            "request_content_digest": candidate_payload.get("original_content_digest", "original-content"),
        }
        prefix = [
            self.message("cell_started", {"arm_sequence": list(self.manifest.arm_permutation)}),
            self.message("prepared_request_frozen", original),
            self.message("invalidation_ingested", {"evidence_root_id": "superseding-root"}),
        ]
        if self.manifest.arm == "A":
            candidate_payload.setdefault("advisory_path", True)
            candidate_payload.setdefault("effect_id", "original-effect")
            effect_id = candidate_payload["effect_id"]
            return tuple(prefix + [self.message("advisory_would_block", {"would_block": True}),
                                    self.message("effect_decision", {**original, "eadm": False, "advisory": True}),
                                     self.message("effect_entered", {"effect_id": effect_id, "operation_id": effect_id, "attempt_id": original["attempt_id"]}),
                                     self.message("effect_terminal", {"effect_id": effect_id, "operation_id": effect_id, "outcome": "known"}),
                                    self.message("worker_terminal_candidate", candidate_payload)])
        rejection_ref = candidate_payload.get("rejection_digest", "offline-unbound-rejection")
        rejected: list[bytes] = []
        if self.manifest.arm == "S":
            return tuple(prefix + rejected + [self.message("worker_terminal_candidate", candidate_payload)])
        proposal_action = candidate_payload.get("proposal_action", {
            "S1": "MineBlock", "S2": "placeBlock", "S3": "navigateTo",
            "S4": "attackTarget", "S5": "handoverBlock",
        }[self.manifest.stratum])
        proposal_arguments = candidate_payload.get("proposal_arguments")
        if not isinstance(proposal_arguments, Mapping):
            defaults = {
                "MineBlock": {"x": 4, "y": 64, "z": 0},
                "placeBlock": {"item_name": "stone", "x": 4, "y": 64, "z": 0, "facing": "west"},
                "navigateTo": {"x": 8, "y": 64, "z": 0},
                "attackTarget": {"target_name": "skeleton"},
                "handoverBlock": {"target_player_name": "villager2", "item_name": "stone", "item_count": 1},
            }
            proposal_arguments = defaults[proposal_action]
        terminal_payload = {"worker_status": candidate_payload.get("worker_status", "succeeded")}
        return tuple(prefix + rejected + [
            self.message("rejection_observation_emitted", {"rejection_id": rejection_ref}),
            self.message("recovery_started", {"rejection_id": rejection_ref}),
            self.message("recovery_step", {"step": 1, "operation_id": "recovery-step-1"}),
            self.message("recovery_step_terminal", {"step": 1, "operation_id": "recovery-step-1", "outcome": "known"}),
            self.message("observation_started", {"observation_id": "observation-1", "operation_id": "observation-1"}),
            self.message("observation_terminal", {"observation_id": "observation-1", "operation_id": "observation-1", "outcome": "known"}),
            self.message("evidence_ingested", {"evidence_root_id": "worker-observation-1", "operation_id": "evidence-1"}),
            self.message("recovery_proposed", {"action": proposal_action,
                                                 "arguments": dict(proposal_arguments)}),
            self.message("worker_terminal_candidate", terminal_payload),
        ])


def launch_worker(manifest: CellManifest | Any, *, worker_id: str,
                  outcome: str = "succeeded", payload: Mapping[str, Any] | None = None) -> tuple[bytes, ...]:
    """Explicit in-memory launch seam.  It never invokes subprocesses."""
    return K12Worker(manifest, worker_id=worker_id).run(outcome, payload)


WorkerCellManifest = CellManifest

__all__ = ["CellManifest", "K12Worker", "K12WorkerError", "WorkerCellManifest",
           "launch_worker"]
