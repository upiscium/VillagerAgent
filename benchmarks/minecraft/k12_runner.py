"""Parent-owned deterministic K12 campaign runner."""
from __future__ import annotations

import hashlib
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from benchmarks.common.eac.canonical import canonical_sha256, thaw_json
from .k12_containment import ContainmentController, FakeSystemdClient, containment_identity
from .k12_authority_adapter import ControlledEACAdapter, project_stale_rejection
from .k12_backends import K12FakeBackend
from .k12_fixture import K12Cell as FixtureCell, STRATUM_ACTIONS, build_k12_fixture
from .k12_identity import (FROZEN_ARGUMENT_SPECS, authority_request_digest, authority_request_view,
                            exact_request_digest, exact_request_view, request_content_placeholder)
from .k12_request import request_content_digest
from .k12_evidence import CellBinding, EvidenceRegistry
from .k12_oracle import K12Oracle, OracleValue
from .k12_orchestration import (AuthorityRejection, K12BudgetExhausted, K12Orchestrator, ParentBudgetAuthority,
                                K12State as OrchestrationArm,
                                TerminalDisposition)
from .k12_parent_events import ParentEvent, ParentEventError, ParentEventLog
from .k12_protocol import (K12Cell, build_k12_cells, load_fixture_manifest,
                           load_k12_protocol, validate_fixture_descriptor)
from .k12_reset import K12ObservedResetState, K12ResetAuthority, K12ResetError
from .k12_validation import K12CellResult, Disposition, validate_cell
from .k12_worker import CellManifest, launch_worker
from .k12_worker_protocol import parse_recovery_proposal
from .k12_artifacts import K12CampaignStop, _authentication, aggregate_cells
from .k12_analysis import analyze


class K12RunnerError(RuntimeError):
    pass


def _runtime_arguments(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {"player_name": "Alice", "emotion": [], "murmur": ""}
    if action in {"MineBlock", "placeBlock"}:
        x, y, z = arguments["position"]
        values.update({"x": x, "y": y, "z": z})
        if action == "placeBlock":
            values.update({"item_name": arguments["item"], "facing": arguments["facing"]})
    elif action == "navigateTo":
        x, y, z = arguments["target"]
        values.update({"x": x, "y": y, "z": z})
    elif action == "attackTarget":
        values["target_name"] = arguments["target"]
    elif action == "handoverBlock":
        values.update({"target_player_name": arguments["recipient"],
                       "item_name": arguments["item"], "item_count": arguments["quantity"]})
    return values


def _evidence_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_evidence_value(item) for item in value]
    if isinstance(value, list):
        return [_evidence_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _evidence_value(item) for key, item in value.items()}
    return value


@dataclass(frozen=True, slots=True)
class LedgerSlot:
    ordinal: int
    cell_id: str
    status: str = "not_started"
    terminal: ParentEvent | None = None
    stop_reference: str = ""


class _CampaignSharedState:
    __slots__ = ("values", "__weakref__")

    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values


class K12Campaign:
    __slots__ = ("_shared_state", "__dict__", "__weakref__")
    _stopped_campaign_ids: set[str] = set()
    _registry_lock = threading.RLock()
    _campaign_locks: dict[str, threading.RLock] = {}
    _published_stops: dict[str, K12CampaignStop] = {}
    _live_campaigns: weakref.WeakValueDictionary[str, _CampaignSharedState] = weakref.WeakValueDictionary()

    def __init__(self, cells: Iterable[K12Cell] | None = None, *, campaign_seed: str = "k12",
                 clock: Callable[[], float] = time.monotonic) -> None:
        # Authentication is deliberately before ledger materialisation.
        protocol = load_k12_protocol()
        self._protocol_digest = protocol["validated_protocol_digest"]
        (_, self._fixture_digest, self._randomization_digest,
         self._contract_identity, self._contract_digest) = _authentication()
        self._fixture_manifest = load_fixture_manifest()
        self.cells = tuple(cells if cells is not None else build_k12_cells())
        expected = build_k12_cells()
        if self.cells != expected or len(self.cells) != 90 or len({c.cell_id for c in self.cells}) != 90:
            raise K12RunnerError("campaign requires the authenticated 90-cell census")
        if not isinstance(campaign_seed, str) or not campaign_seed:
            raise K12RunnerError("campaign seed is required")
        self.campaign_seed = campaign_seed
        self._clock = clock
        identity_input = {"seed": campaign_seed, "protocol_digest": self._protocol_digest,
                          "cells": [c.cell_id for c in self.cells]}
        self.cohort_id = canonical_sha256({"campaign": identity_input})
        self.campaign_id = canonical_sha256(identity_input)
        with self._registry_lock:
            self._campaign_lock = self._campaign_locks.setdefault(
                self.campaign_id, threading.RLock())
        with self._campaign_lock:
            with self._registry_lock:
                shared = self._live_campaigns.get(self.campaign_id)
                if shared is not None:
                    self.__dict__ = shared.values
                    self._shared_state = shared
                    return
                if self.campaign_id in self._stopped_campaign_ids:
                    raise K12RunnerError("campaign identity is permanently stopped")
            self.ledger = [LedgerSlot(i, c.cell_id) for i, c in enumerate(self.cells)]
            self.events: list[ParentEvent] = []
            self._traces: dict[str, tuple[ParentEvent, ...]] = {}
            self._results: dict[str, K12CellResult] = {}
            self._evidence_snapshots: dict[str, Any] = {}
            self._next = 0
            self._blocked = False
            self._block_reason = ""
            self._campaign_stop: K12CampaignStop | None = None
            self._used_reset_tokens: set[str] = set()
            self._triplet_rejection_bindings: dict[str, str] = {}
            self._next_reset_generation = 1
            self._prior_containment = "contained"
            self._shared_state = _CampaignSharedState(self.__dict__)
            with self._registry_lock:
                self._live_campaigns[self.campaign_id] = self._shared_state

    @property
    def next_cell(self) -> K12Cell | None:
        return self.cells[self._next] if self._next < 90 else None

    def _failure(self, cell: K12Cell, log: ParentEventLog, reason: str, *,
                 reset_invalid: bool = False, containment_failure: bool = False,
                 budget_exhausted: bool = False,
                  budget_snapshot: Any | None = None,
                  evidence_payload: dict[str, Any] | None = None) -> ParentEvent:
        if not log.events:
            self.events.append(log.append_reset_attested(
                cell_id=cell.cell_id, triplet_id=cell.triplet_id, arm=cell.arm,
                payload={"status": "failed", "reason": reason},
            ))
        body = {
            "runtime_failure": not reset_invalid and not containment_failure and not budget_exhausted,
            "reset_invalid": reset_invalid, "containment_failure": containment_failure,
            "budget_exhausted": budget_exhausted,
            "budget_deadline_reached": budget_exhausted and ("deadline" in reason or "reserve" in reason),
            "containment": "failed" if containment_failure else "verified" if budget_exhausted else "unknown",
            "reason": reason, "budget_steps": getattr(budget_snapshot, "steps", 0),
            "budget_model_calls": getattr(budget_snapshot, "model", 0),
            "budget_evidence_calls": getattr(budget_snapshot, "evidence", 0),
            "budget_effect_attempts": getattr(budget_snapshot, "effects", 0),
            **(evidence_payload or {}),
        }
        self.events.append(log.append_process_finalized(
            cell_id=cell.cell_id, triplet_id=cell.triplet_id, arm=cell.arm,
            payload=body,
        ))
        terminal = log.append_cell_terminal(cell_id=cell.cell_id, triplet_id=cell.triplet_id, arm=cell.arm)
        self.events.append(terminal)
        self._traces[cell.cell_id] = log.events
        return terminal

    def _publish_stop(self, cause: str, reason: str, trigger_reference: str | None = None) -> None:
        if self._campaign_stop is not None:
            return
        stop = K12CampaignStop._from_parent_prefix(
            self.results(), protocol_digest=self._protocol_digest,
            campaign_id=self.campaign_id, cohort_id=self.cohort_id,
            campaign_seed=self.campaign_seed, cause=cause, reason=reason,
            trigger_reference=trigger_reference)
        with self._registry_lock:
            existing = self._published_stops.get(self.campaign_id)
            if existing is not None and existing != stop:
                raise K12RunnerError("campaign identity already has a conflicting terminal stop")
            stop = existing or stop
            self._published_stops[self.campaign_id] = stop
            self._stopped_campaign_ids.add(self.campaign_id)
        self._blocked = True
        self._block_reason = reason
        self._campaign_stop = stop
        for ordinal in range(self._next, len(self.ledger)):
            pending = self.ledger[ordinal]
            self.ledger[ordinal] = LedgerSlot(
                pending.ordinal, pending.cell_id, "not_started", None,
                self._campaign_stop.identity)

    def stop(self, *, cause: str, reason: str, trigger_reference: str | None = None) -> None:
        """Publish an authenticated finite stop for parent authority loss/corruption."""
        with self._campaign_lock:
            if cause not in {"manifest_corruption", "parent_authority_loss"}:
                raise K12RunnerError("external campaign stop cause is not admissible")
            self._publish_stop(cause, reason, trigger_reference)

    def launch(self, launch_hook: Callable[[CellManifest, str], Iterable[bytes | str]] | None = None, *,
               reset_admission: Callable[[CellManifest], bool] | None = None,
               validation_hook: Callable[[ParentEvent, CellManifest], bool] | None = None,
               containment_hook: Callable[[FakeSystemdClient], None] | None = None,
               budget_authority: ParentBudgetAuthority | None = None,
               budget_admission: Callable[[], bool] | None = None,
               finalize_hook: Callable[[tuple[LedgerSlot, ...]], Any] | None = None) -> Any:
        # Serialize campaign-state admission with permanent stop publication so
        # a pre-existing same-identity instance cannot race a containment stop.
        with self._campaign_lock:
            with self._registry_lock:
                if self.campaign_id in self._stopped_campaign_ids:
                    raise K12RunnerError("campaign identity is permanently stopped")
            return self._launch(launch_hook, reset_admission=reset_admission,
                                validation_hook=validation_hook,
                                containment_hook=containment_hook,
                                budget_authority=budget_authority,
                                budget_admission=budget_admission,
                                finalize_hook=finalize_hook)

    def _launch(self, launch_hook: Callable[[CellManifest, str], Iterable[bytes | str]] | None = None, *,
               reset_admission: Callable[[CellManifest], bool] | None = None,
               validation_hook: Callable[[ParentEvent, CellManifest], bool] | None = None,
               containment_hook: Callable[[FakeSystemdClient], None] | None = None,
               budget_authority: ParentBudgetAuthority | None = None,
               budget_admission: Callable[[], bool] | None = None,
               finalize_hook: Callable[[tuple[LedgerSlot, ...]], Any] | None = None) -> Any:
        if self._blocked or self._next >= 90:
            if self._next >= 90: return self.finalize(finalize_hook)
            raise K12RunnerError("campaign is blocked")
        cell = self.cells[self._next]
        action = STRATUM_ACTIONS[cell.stratum]
        fixture = build_k12_fixture(f"{cell.triplet_id}-fixture", action=action,
                                    template=cell.template, seed=cell.seed)
        try:
            descriptor = next((item for item in self._fixture_manifest["triplets"]
                               if item.get("triplet_id") == cell.triplet_id), None)
            if descriptor is None:
                raise K12RunnerError("authenticated fixture descriptor is missing")
            validate_fixture_descriptor(descriptor)
        except (KeyError, TypeError, ValueError, K12RunnerError) as exc:
            reason = f"manifest corruption: {exc}"
            self._publish_stop("manifest_corruption", reason)
            raise K12RunnerError(reason) from exc
        # All arms start from the same committed post-invalidation world. A
        # differs only by advisory enforcement, not by reset state.
        initial_state = fixture.alternative
        backend = K12FakeBackend(fixture, state=initial_state)
        authority = K12ResetAuthority(
            fixture, initial_generation=self._next_reset_generation,
            expected_initial_state_digest=initial_state.digest,
            expected_prior_containment=self._prior_containment,
        )
        manifest = CellManifest.from_cell(cell)
        worker_id = f"{self.campaign_id}:{cell.cell_id}"
        unit, cgroup = containment_identity(cell.cell_id, worker_id)
        client = FakeSystemdClient(unit, cgroup)
        controller = ContainmentController(client, deadline_ns=0)
        log = ParentEventLog(worker_id, cell_id=cell.cell_id, triplet_id=cell.triplet_id, arm=cell.arm)
        failure_reason: str | None = None
        evidence_registry: EvidenceRegistry | None = None
        evidence_snapshot = None
        reset_record = None
        containment_record = None
        parent_started = self._clock()
        budget_authority = budget_authority or ParentBudgetAuthority(
            clock=self._clock, parent_started=parent_started)
        def check_deadline() -> None:
            if self._clock() - parent_started >= 180:
                raise K12BudgetExhausted("absolute cell parent deadline")
        try:
            check_deadline()
            observed_reset = K12ObservedResetState.from_fixture(
                fixture, self._next_reset_generation, cell_id=cell.cell_id,
                triplet_id=cell.triplet_id, arm=cell.arm, seed=cell.seed,
                initial_state=backend.state, prior_containment=self._prior_containment,
            )
            attestation = authority.attest(observed_reset)
            token = authority.issue(attestation)
            if token.token_id in self._used_reset_tokens:
                raise K12ResetError("campaign reset token was reused")
            reset_result = authority.launch(token, attestation, expected_cell_id=cell.cell_id)
            self._used_reset_tokens.add(token.token_id)
            self._next_reset_generation = reset_result["next_generation"]
            self.events.append(log.append_reset_attested(cell_id=cell.cell_id, triplet_id=cell.triplet_id, arm=cell.arm,
                                                         payload={"status": "verified", "token_id": token.token_id,
                                                                   "attestation_digest": attestation.digest,
                                                                   "generation": reset_result["generation"],
                                                                   "initial_state_digest": attestation.initial_state_digest,
                                                                   "prior_containment": attestation.prior_containment}))
            evidence_binding = CellBinding(
                self._protocol_digest, self.campaign_id, self.cohort_id, cell.cell_id,
                cell.triplet_id, cell.arm, fixture.fixture_digest, f"T{cell.template}",
                cell.seed, token.token_id, reset_result["generation"], attestation.digest,
            )
            evidence_registry = EvidenceRegistry(evidence_binding)
            reset_record = evidence_registry.register("reset", {
                "token_id": token.token_id, "generation": reset_result["generation"],
                "attestation_digest": attestation.digest,
                "initial_state_digest": attestation.initial_state_digest,
            })
            if reset_admission is not None and reset_admission(manifest) is not True:
                raise K12ResetError("legacy reset admission denied")
            if containment_hook is not None: containment_hook(client)
            original_arguments = dict(fixture.original_arguments)
            alternative_arguments = dict(fixture.alternative_arguments)
            native_arguments = original_arguments if cell.arm == "A" else alternative_arguments
            alternative_runtime_arguments = _runtime_arguments(action, alternative_arguments)
            alternative_semantic_arguments = {
                name: alternative_runtime_arguments[name] for name in FROZEN_ARGUMENT_SPECS[action]
            }
            def admit_effect() -> None:
                if budget_admission is not None and budget_admission() is not True:
                    raise K12RunnerError("budget admission denied before native backend callback")
                budget_authority.reserve("effects")
            def admitted_native(**unused: Any) -> Any:
                return getattr(backend, action)(**native_arguments)
            adapter = ControlledEACAdapter(
                run_id=f"{cell.cell_id}-eac", action=action,
                tool_kwargs=_runtime_arguments(action, original_arguments),
                observation_arguments=_runtime_arguments(action, original_arguments),
                alternative_tool_kwargs=_runtime_arguments(action, alternative_arguments),
                native_callback=admitted_native,
            )
            rejection = None
            original_recovery = None
            prepared_recovery = None
            authority_recovery = None
            advisory_evidence = None
            backend_effect = None
            rejection_record = None
            effect_record = None
            repeated_proposal = False
            oracle_value = OracleValue.NOT_APPLICABLE
            budget_authority.begin_branch()
            if cell.arm == "A":
                admit_effect()
                advisory_evidence = adapter.collect_advisory()
                backend_effect = advisory_evidence.native_result
            elif cell.arm == "R":
                original_recovery = adapter.collect()
                rejection = project_stale_rejection(original_recovery)
            else:
                rejection = project_stale_rejection(adapter.collect())
            if rejection is not None:
                assert evidence_registry is not None
                rejection_record = evidence_registry.register("authority_rejection", _evidence_value({
                    **rejection.unsigned(), "projection_digest": rejection.projection_digest,
                }))
                registered_rejection = thaw_json(rejection_record.payload)
                branch_equivalence = canonical_sha256({
                    "action": action, "request_content_digest": rejection.request_content_digest,
                    "rejection_stage": registered_rejection["rejection_stage"],
                    "rejection_reason": registered_rejection["rejection_reason"],
                    "outcome_certainty": registered_rejection["outcome_certainty"],
                    "eadm_after": registered_rejection["eadm_after"],
                    "permit_lifecycle_before": registered_rejection["permit_lifecycle_before"],
                    "permit_lifecycle_after": registered_rejection["permit_lifecycle_after"],
                    "request_content_scientific": registered_rejection["request_content_scientific"],
                    "retry_safe": registered_rejection["retry_safe"],
                    "original_attempt_absent": registered_rejection["original_attempt_absent"],
                    "native_entry_count": registered_rejection["native_entry_count"],
                })
                prior_binding = self._triplet_rejection_bindings.get(cell.triplet_id)
                if prior_binding is not None and prior_binding != branch_equivalence:
                    raise K12RunnerError("R/S branch equivalence mismatch")
                self._triplet_rejection_bindings[cell.triplet_id] = branch_equivalence
            orchestrator = K12Orchestrator(selected_arm=OrchestrationArm(cell.arm),
                paired_branch_digest=(rejection.request_content_digest if rejection is not None else None),
                paired_rejection_id=(rejection.projection_digest if rejection is not None else None))
            original_request_id = (rejection.request_digest if rejection is not None
                                   else advisory_evidence.original_request_digest)
            branch_digest = (rejection.request_content_digest if rejection is not None
                             else advisory_evidence.original_content_digest)
            orchestrator.enter(original_request_id, branch_digest=branch_digest,
                               shadow_would_block=cell.arm == "A",
                               original_effect=backend_effect is not None and backend_effect.executed)
            if cell.arm == "A":
                orchestrator.advisory_would_block()
                orchestrator.original_advisory_effect()
                orchestrator.stop(TerminalDisposition.COMPLETED)
            else:
                assert rejection is not None
                orchestrator.authority_reject(AuthorityRejection(
                    original_request_id, rejection.projection_digest, rejection.rejection_reason,
                    original_request_id, rejection.request_content_digest,
                ))
            if backend_effect is not None:
                assert evidence_registry is not None
                effect_record = evidence_registry.register("backend_effect", {
                    "effect_digest": backend_effect.effect_digest,
                    "before_digest": backend_effect.before.digest,
                    "after_digest": backend_effect.after.digest,
                    "arguments": {name: _runtime_arguments(action, native_arguments)[name]
                                  for name in FROZEN_ARGUMENT_SPECS[action]},
                    "reset_evidence_id": reset_record.id,
                })
                oracle = K12Oracle(fixture, FixtureCell(cell.cell_id, fixture.fixture_id, action),
                                   reset_generation=reset_result["generation"])
                oracle_value = oracle.evaluate_bound(
                    backend_effect.effect_digest, backend_effect.before, backend_effect.after,
                    native_arguments, cell_id=cell.cell_id,
                    reset_generation=reset_result["generation"])
            worker_evidence = {}
            if backend_effect is not None:
                worker_evidence["effect_id"] = backend_effect.effect_digest
            if rejection is not None:
                worker_evidence["rejection_digest"] = rejection.projection_digest
                worker_evidence.update({
                    "original_request_id": rejection.request_digest,
                    "original_candidate_id": rejection.candidate_id,
                    "original_attempt_id": rejection.attempt_id,
                    "original_content_digest": rejection.request_content_digest,
                })
                if cell.arm == "R":
                    worker_evidence.update({
                        "proposal_action": action,
                        "proposal_arguments": alternative_semantic_arguments,
                    })
            elif advisory_evidence is not None:
                worker_evidence.update({
                    "original_request_id": advisory_evidence.original_request_digest,
                    "original_candidate_id": advisory_evidence.candidate_id,
                    "original_attempt_id": advisory_evidence.attempt_id,
                    "original_content_digest": advisory_evidence.original_content_digest,
                })
            hook = launch_hook or (lambda m, wid: launch_worker(m, worker_id=wid, payload=worker_evidence))
            observed_budget_events = {"steps": 0, "model": 0, "evidence": 0}
            model_starts: dict[str, str] = {}
            model_evidence_ids: list[str] = []
            for line in hook(manifest, worker_id):
                check_deadline()
                budget_authority.check_deadline()
                preview = log.preview(line)
                budget_kind = {
                    "recovery_step": "steps",
                    "model_call_admitted": "model",
                    "rejection_observation_emitted": "evidence",
                    "observation_started": "evidence",
                }.get(preview.event)
                if budget_kind is not None:
                    observed_budget_events[budget_kind] += 1
                    if observed_budget_events[budget_kind] > getattr(
                            budget_authority.snapshot(), budget_kind):
                        budget_authority.reserve(budget_kind)
                event = log.receive(line)
                if validation_hook is not None and validation_hook(event, manifest) is not True:
                    raise K12RunnerError("parent validation hook rejected artifact")
                self.events.append(event)
                if event.event == "model_call_admitted":
                    operation_id = thaw_json(event.payload).get("operation_id")
                    model_starts[operation_id] = event.message_digest
                elif event.event == "model_call_terminal":
                    operation_payload = thaw_json(event.payload)
                    operation_id = operation_payload.get("operation_id")
                    start_digest = model_starts.pop(operation_id, None)
                    if start_digest is None:
                        raise K12RunnerError("model terminal lacks parent-observed start")
                    assert evidence_registry is not None
                    model_record = evidence_registry.register("model_call", {
                        "operation_id": operation_id, "start_message_digest": start_digest,
                        "terminal_message_digest": event.message_digest,
                        "outcome": operation_payload.get("outcome", "known"),
                    })
                    model_evidence_ids.append(model_record.id)
                if event.event == "invalidation_ingested" and rejection is not None:
                    assert rejection_record is not None
                    for parent_name, parent_payload in (
                        ("current_inadmissible_confirmed", {
                            "candidate_id": rejection.candidate_id, "eadm": False,
                            "evidence_id": rejection_record.id,
                        }),
                        ("authority_rejected", {
                            "request_id": rejection.request_digest,
                            "candidate_id": rejection.candidate_id,
                            "attempt_id": rejection.attempt_id,
                            "permit_id": rejection.permit_id,
                            "rejection_stage": rejection.rejection_stage,
                            "rejection_reason": rejection.rejection_reason,
                            "outcome_certainty": rejection.outcome_certainty,
                            "eadm_after": rejection.eadm_after,
                            "permit_lifecycle_before": rejection.permit_lifecycle_before,
                            "permit_lifecycle_after": rejection.permit_lifecycle_after,
                            "request_content_scientific": rejection.request_content_scientific,
                            "retry_safe": rejection.retry_safe,
                            "projection_digest": rejection.projection_digest,
                            "original_attempt_absent": rejection.original_attempt_absent,
                            "native_entry_count": rejection.native_entry_count,
                            "evidence_id": rejection_record.id,
                        }),
                    ):
                        parent_event = log.append_authority_event(
                            parent_name, cell_id=cell.cell_id, triplet_id=cell.triplet_id,
                            arm=cell.arm, payload=parent_payload)
                        self.events.append(parent_event)
                elif event.event == "rejection_observation_emitted" and cell.arm == "R":
                    orchestrator.observe_rejection()
                elif event.event == "recovery_step" and cell.arm == "R":
                    orchestrator.step()
                elif event.event == "evidence_ingested" and cell.arm == "R":
                    orchestrator.evidence()
                elif event.event == "recovery_proposed" and cell.arm == "R":
                    assert rejection is not None and original_recovery is not None
                    proposal = parse_recovery_proposal(event.payload, expected_action=action)
                    expected_arguments = alternative_semantic_arguments
                    action_view = exact_request_view(original_recovery.retained_request)["action"]
                    proposal_content_digest = request_content_digest(
                        adapter.actor, action_view, proposal["arguments"], proposal["arguments"])
                    if proposal_content_digest == rejection.request_content_digest:
                        repeated_proposal = True
                        self.events.append(log.append_authority_event(
                            "proposal_validated", cell_id=cell.cell_id, triplet_id=cell.triplet_id,
                            arm=cell.arm, payload={"proposal_message_digest": event.message_digest,
                                                   "request_content_digest": proposal_content_digest,
                                                   "status": "repeated_original"}))
                        orchestrator.stop(TerminalDisposition.REPEATED_REQUEST)
                        continue
                    if proposal["arguments"] != expected_arguments:
                        raise K12RunnerError("worker recovery proposal does not match the frozen cell alternative")
                    staged_recovery = adapter.stage_recovery(original_recovery, rejection)
                    recovery_request_id = authority_request_digest(staged_recovery.preview_request)
                    recovery_content_digest = request_content_placeholder(
                        staged_recovery.preview_request, actor_id=adapter.actor)[1]
                    orchestrator.validate_recovery_request(
                        recovery_request_id, branch_digest=recovery_content_digest,
                        causal_parent=original_request_id,
                        candidate_id=staged_recovery.preview_request.candidate_id,
                        attempt_id=staged_recovery.preview_request.attempt_id,
                        evidence_root_id=staged_recovery.recovery_evidence_root.root_id,
                        proposal_message_digest=event.message_digest)
                    effect_budget_before = budget_authority.snapshot().effects
                    try:
                        admit_effect()
                    except Exception:
                        self.events.append(log.append_authority_event(
                            "proposal_validated", cell_id=cell.cell_id, triplet_id=cell.triplet_id,
                            arm=cell.arm, payload={"proposal_message_digest": event.message_digest,
                                                   "request_content_digest": proposal_content_digest,
                                                   "status": "budget_denied",
                                                   "effect_budget_before": effect_budget_before,
                                                   "effect_budget_after": budget_authority.snapshot().effects}))
                        raise
                    self.events.append(log.append_authority_event(
                        "proposal_validated", cell_id=cell.cell_id, triplet_id=cell.triplet_id,
                        arm=cell.arm, payload={"proposal_message_digest": event.message_digest,
                                               "request_content_digest": proposal_content_digest,
                                               "status": "admitted",
                                               "effect_budget_before": effect_budget_before,
                                               "effect_budget_after": budget_authority.snapshot().effects}))
                    self.events.append(log.append_authority_event(
                        "new_request_prepared", cell_id=cell.cell_id, triplet_id=cell.triplet_id,
                        arm=cell.arm, payload={"request_id": recovery_request_id,
                                                "candidate_id": staged_recovery.preview_request.candidate_id,
                                                "attempt_id": staged_recovery.preview_request.attempt_id,
                                                "request_content_digest": recovery_content_digest,
                                                "authority_request_projection": authority_request_view(
                                                    staged_recovery.preview_request)}))
                    prepared_recovery = adapter.issue_recovery(staged_recovery)
                    prepared = prepared_recovery.prepared
                    self.events.append(log.append_authority_event(
                        "permit_issued", cell_id=cell.cell_id, triplet_id=cell.triplet_id,
                        arm=cell.arm, payload={"permit_id": prepared.permit.permit_id,
                                               "request_id": recovery_request_id,
                                               "candidate_id": prepared.request.candidate_id,
                                               "attempt_id": prepared.request.attempt_id}))
                    self.events.append(log.append_authority_event(
                        "effect_decision", cell_id=cell.cell_id, triplet_id=cell.triplet_id,
                        arm=cell.arm, payload={"request_id": recovery_request_id,
                                               "candidate_id": prepared.request.candidate_id,
                                               "attempt_id": prepared.request.attempt_id,
                                               "permit_id": prepared.permit.permit_id,
                                               "eadm": True}))
                    operation_id = canonical_sha256({"cell_id": cell.cell_id,
                                                     "attempt_id": prepared.request.attempt_id})
                    orchestrator.recovery(
                        recovery_request_id, permit_id=prepared.permit.permit_id,
                        effect_id=operation_id,
                        evidence_root_id=prepared_recovery.recovery_evidence_root.root_id,
                        oracle_id="parent-oracle", branch_digest=recovery_content_digest,
                        causal_parent=original_request_id,
                        candidate_id=prepared.request.candidate_id,
                        attempt_id=prepared.request.attempt_id, eadm_id="effect-time-eadm",
                        proposal_message_digest=event.message_digest)
                    self.events.append(log.append_authority_event(
                        "effect_entered", cell_id=cell.cell_id, triplet_id=cell.triplet_id,
                        arm=cell.arm, payload={"operation_id": operation_id,
                                               "attempt_id": prepared.request.attempt_id}))
                    orchestrator.effect()
                    try:
                        authority_recovery = adapter.execute_authority_recovery(prepared_recovery)
                    except Exception:
                        self.events.append(log.append_authority_event(
                            "effect_terminal", cell_id=cell.cell_id, triplet_id=cell.triplet_id,
                            arm=cell.arm, payload={"operation_id": operation_id,
                                                   "effect_id": "unavailable",
                                                   "outcome": "failed"}))
                        raise
                    backend_effect = authority_recovery.native_result
                    worker_evidence["effect_id"] = backend_effect.effect_digest
                    assert evidence_registry is not None
                    effect_record = evidence_registry.register("backend_effect", {
                        "effect_digest": backend_effect.effect_digest,
                        "before_digest": backend_effect.before.digest,
                        "after_digest": backend_effect.after.digest,
                        "arguments": alternative_semantic_arguments,
                        "reset_evidence_id": reset_record.id,
                        "operation_id": operation_id,
                    })
                    self.events.append(log.append_authority_event(
                        "effect_terminal", cell_id=cell.cell_id, triplet_id=cell.triplet_id,
                        arm=cell.arm, payload={"operation_id": operation_id,
                                               "effect_id": backend_effect.effect_digest,
                                               "evidence_id": effect_record.id,
                                               "outcome": "known"}))
                    oracle = K12Oracle(fixture, FixtureCell(cell.cell_id, fixture.fixture_id, action),
                                       reset_generation=reset_result["generation"])
                    oracle_value = oracle.evaluate_bound(
                        backend_effect.effect_digest, backend_effect.before, backend_effect.after,
                        native_arguments, cell_id=cell.cell_id,
                        reset_generation=reset_result["generation"])
                    orchestrator.raw_terminal_candidate()
            if not log.events or not log.events[-1].terminal:
                raise K12RunnerError("worker terminal candidate is missing")
            check_deadline()
            budget_authority.check_deadline()
            assert evidence_registry is not None
            oracle_request_id = (authority_recovery.recovery_request_digest
                                 if authority_recovery is not None else original_request_id)
            oracle_record = evidence_registry.register("oracle", {
                "oracle_value": oracle_value.value,
                "effect_evidence_id": effect_record.id if effect_record is not None else None,
                "reset_evidence_id": reset_record.id,
                "cell_id": cell.cell_id, "reset_generation": reset_result["generation"],
                "request_id": oracle_request_id,
                "fixture_digest": fixture.fixture_digest,
                "template": f"T{cell.template}", "seed": cell.seed,
                "reset_token": token.token_id, "attestation_digest": attestation.digest,
                "initial_state_digest": attestation.initial_state_digest,
            })
            self.events.append(log.append_objective_oracle_evaluated(cell_id=cell.cell_id, triplet_id=cell.triplet_id,
                                                                       arm=cell.arm, payload={"oracle_value": oracle_value.value,
                                                                                              "effect_id": worker_evidence.get("effect_id", ""),
                                                                                              "fixture_digest": fixture.fixture_digest,
                                                                                              "reset_generation": reset_result["generation"],
                                                                                              "reset_evidence_id": reset_record.id,
                                                                                              "effect_evidence_id": effect_record.id if effect_record is not None else None,
                                                                                              "oracle_evidence_id": oracle_record.id,
                                                                                              "request_id": oracle_request_id,
                                                                                              "template": f"T{cell.template}", "seed": cell.seed,
                                                                                              "reset_token": token.token_id,
                                                                                              "attestation_digest": attestation.digest,
                                                                                              "initial_state_digest": attestation.initial_state_digest}))
            containment = controller.contain(now_ns=0)
            if containment.blocked_next_launch or not containment.cgroup_empty:
                raise K12RunnerError("containment is unknown")
            containment_record = evidence_registry.register("containment", {
                "unit": unit, "cgroup": cgroup, "blocked_next_launch": containment.blocked_next_launch,
                "cgroup_empty": containment.cgroup_empty, "reset_evidence_id": reset_record.id,
            })
            check_deadline()
            parent_budgets = budget_authority.snapshot()
            finalized_payload = {"containment": "verified",
                                 "backend_effect_digest": worker_evidence.get("effect_id", ""),
                                 "orchestration_transitions": len(orchestrator.transitions),
                                 "budget_steps": parent_budgets.steps,
                                 "budget_model_calls": parent_budgets.model,
                                 "budget_evidence_calls": parent_budgets.evidence,
                                 "budget_effect_attempts": parent_budgets.effects,
                                 "reset_evidence_id": reset_record.id,
                                 "oracle_evidence_id": oracle_record.id,
                                 "containment_evidence_id": containment_record.id}
            finalized_payload["model_call_evidence_ids"] = model_evidence_ids
            if rejection is not None:
                finalized_payload.update({
                    "authority_rejection_evidence_id": rejection_record.id,
                })
            if repeated_proposal:
                finalized_payload.update({
                    "repeated_original_request": True,
                    "recovery_content_digest": rejection.request_content_digest,
                    "recovery_native_entry_count": 0,
                })
            if authority_recovery is not None:
                attempt = authority_recovery.recovery_attempt
                attempt_record = {
                    "attempt_id": attempt.attempt_id, "permit_id": attempt.permit_id,
                    "state": attempt.state, "outcome": attempt.outcome,
                    "request_digest": attempt.request_digest,
                    "manifest_fingerprint": attempt.manifest_fingerprint,
                    "enforcement": attempt.enforcement,
                }
                finalized_payload.update({
                    "original_request_id": authority_recovery.original_request_digest,
                    "original_content_digest": authority_recovery.original_content_digest,
                    "original_candidate_id": authority_recovery.original_candidate_id,
                    "original_attempt_id": authority_recovery.original_attempt_id,
                    "recovery_request_id": authority_recovery.recovery_request_digest,
                    "recovery_content_digest": authority_recovery.recovery_content_digest,
                    "recovery_candidate_id": authority_recovery.recovery_candidate_id,
                    "recovery_attempt_id": authority_recovery.recovery_attempt_id,
                    "recovery_permit_id": authority_recovery.recovery_permit_id,
                    "recovery_evidence_root_id": authority_recovery.recovery_evidence_root.root_id,
                    "recovery_eadm": authority_recovery.recovery_evaluation.admissible,
                    "recovery_native_entry_count": authority_recovery.native_entry_count,
                    "recovery_attempt_record": attempt_record,
                    "recovery_attempt_record_digest": canonical_sha256(attempt_record),
                    "recovery_authority_request_digest": attempt.request_digest,
                })
            check_deadline()
            finalization_record = evidence_registry.register("finalization", finalized_payload)
            evidence_snapshot = evidence_registry.freeze()
            self.events.append(log.append_process_finalized(cell_id=cell.cell_id, triplet_id=cell.triplet_id, arm=cell.arm,
                                                             payload={"finalization_evidence_id": finalization_record.id,
                                                                      "evidence_snapshot_digest": evidence_snapshot.digest}))
            terminal = log.append_cell_terminal(cell_id=cell.cell_id, triplet_id=cell.triplet_id, arm=cell.arm)
            self.events.append(terminal)
        except Exception as exc:
            failure_reason = str(exc)
            budget_exhausted = isinstance(exc, K12BudgetExhausted)
            containment_failure = False
            failed_containment = None
            try:
                failed_containment = controller.contain(now_ns=0)
                containment_failure = containment_failure or bool(
                    failed_containment.blocked_next_launch or not failed_containment.cgroup_empty
                )
            except Exception:
                containment_failure = True
            budget_snapshot = budget_authority.snapshot()
            failure_evidence_payload: dict[str, Any] = {}
            if evidence_registry is not None:
                try:
                    if evidence_snapshot is None:
                        containment_record = evidence_registry.register("containment", {
                            "unit": unit, "cgroup": cgroup,
                            "blocked_next_launch": (failed_containment.blocked_next_launch
                                                    if failed_containment is not None else True),
                            "cgroup_empty": (failed_containment.cgroup_empty
                                             if failed_containment is not None else False),
                            "containment_failure": containment_failure,
                            "reset_evidence_id": reset_record.id if reset_record is not None else "",
                        })
                        failure_authority = {
                            "runtime_failure": not isinstance(exc, K12ResetError)
                                               and not containment_failure and not budget_exhausted,
                            "reset_invalid": isinstance(exc, K12ResetError),
                            "containment_failure": containment_failure,
                            "budget_exhausted": budget_exhausted and not containment_failure,
                            "budget_deadline_reached": budget_exhausted
                                                       and ("deadline" in str(exc) or "reserve" in str(exc)),
                            "containment": "failed" if containment_failure else "verified" if budget_exhausted else "unknown",
                            "reason": str(exc), "budget_steps": budget_snapshot.steps,
                            "budget_model_calls": budget_snapshot.model,
                            "budget_evidence_calls": budget_snapshot.evidence,
                            "budget_effect_attempts": budget_snapshot.effects,
                            "reset_evidence_id": reset_record.id if reset_record is not None else "",
                            "containment_evidence_id": containment_record.id,
                        }
                        finalization_record = evidence_registry.register(
                            "finalization", failure_authority)
                        evidence_snapshot = evidence_registry.freeze()
                    failure_evidence_payload = {
                        "evidence_snapshot_digest": evidence_snapshot.digest,
                        "finalization_evidence_id": finalization_record.id,
                    }
                except (RuntimeError, ValueError):
                    containment_failure = True
            if any(event.event == "process_finalized" for event in log.events):
                terminal = next((event for event in log.events if event.event == "cell_terminal"), None)
                if terminal is None:
                    terminal = log.append_cell_terminal(
                        cell_id=cell.cell_id, triplet_id=cell.triplet_id, arm=cell.arm)
                    self.events.append(terminal)
                self._traces[cell.cell_id] = log.events
            else:
                if budget_exhausted and not containment_failure:
                    self.events.append(log.append_budget_reached(
                        cell_id=cell.cell_id, triplet_id=cell.triplet_id, arm=cell.arm,
                        payload={"reason": str(exc), "steps": budget_snapshot.steps,
                                 "model_calls": budget_snapshot.model,
                                 "evidence": budget_snapshot.evidence,
                                 "effects": budget_snapshot.effects},
                    ))
                terminal = self._failure(cell, log, str(exc), reset_invalid=isinstance(exc, K12ResetError),
                                         containment_failure=containment_failure,
                                         budget_exhausted=budget_exhausted and not containment_failure,
                                         budget_snapshot=budget_snapshot,
                                         evidence_payload=failure_evidence_payload)
            self._blocked = containment_failure
            if containment_failure:
                self._block_reason = str(exc)
            self._prior_containment = "failed" if containment_failure else "contained"
        else:
            self._traces[cell.cell_id] = log.events
            self._prior_containment = "contained"
        # The result is derived from the complete authenticated trace, never
        # from the worker candidate or the cell-terminal payload.
        trace = self._traces[cell.cell_id]
        if evidence_registry is not None:
            if evidence_snapshot is not None:
                self._evidence_snapshots[cell.cell_id] = evidence_snapshot
        reset_payload = thaw_json(trace[0].payload)
        self._results[cell.cell_id] = validate_cell(self._traces[cell.cell_id], cell_id=cell.cell_id,
                                                      triplet_id=cell.triplet_id, expected_arm=cell.arm,
                                                      expected_reset_generation=reset_payload.get("reset_generation", reset_payload.get("generation")),
                                                      evidence_snapshot=evidence_snapshot,
                                                      expected_protocol_digest=self._protocol_digest,
                                                      expected_campaign_id=self.campaign_id,
                                                      expected_cohort_id=self.cohort_id,
                                                      expected_campaign_seed=self.campaign_seed)
        result = self._results[cell.cell_id]
        if not self._results[cell.cell_id].valid:
            failure_reason = failure_reason or self._results[cell.cell_id].disposition.value
        elif self._results[cell.cell_id].disposition is Disposition.BUDGET_EXHAUSTED:
            failure_reason = None
        self.ledger[self._next] = LedgerSlot(self._next, cell.cell_id, "terminal", terminal)
        self._next += 1
        if self._blocked and self._campaign_stop is None:
            self._publish_stop("containment_unknown",
                               self._block_reason or "containment failure",
                               self._results[cell.cell_id].trace_digest)
        if failure_reason:
            suffix = "; campaign is blocked" if self._blocked else ""
            raise K12RunnerError(f"{failure_reason}{suffix}")
        return self.ledger[self._next - 1]

    def results(self) -> tuple[K12CellResult, ...]:
        return tuple(self._results[c.cell_id] for c in self.cells if c.cell_id in self._results)

    def finalize(self, finalize_hook: Callable[[tuple[LedgerSlot, ...]], Any] | None = None) -> Any:
        with self._campaign_lock:
            return self._finalize(finalize_hook)

    def _finalize(self, finalize_hook: Callable[[tuple[LedgerSlot, ...]], Any] | None = None) -> Any:
        if self._blocked:
            if self._campaign_stop is None:
                raise K12RunnerError("blocked campaign has no terminal stop authority")
            aggregate = aggregate_cells(self.results(), campaign_stop=self._campaign_stop)
            return aggregate, analyze(aggregate)
        if self._next != 90 or any(s.terminal is None for s in self.ledger):
            raise K12RunnerError("aggregate requires 90 validated terminal artifacts")
        if finalize_hook:
            return finalize_hook(tuple(self.ledger))
        aggregate = aggregate_cells(self.results())
        return aggregate, analyze(aggregate)

    run = launch
    run_cell = launch


K12Runner = K12Campaign

def deterministic_campaign_identity(campaign_seed: str, cells: Iterable[K12Cell] | None = None) -> str:
    return K12Campaign(cells, campaign_seed=campaign_seed).campaign_id

__all__ = ["K12Campaign", "K12Runner", "K12RunnerError",
           "LedgerSlot", "deterministic_campaign_identity"]
