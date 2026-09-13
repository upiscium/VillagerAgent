"""K12 composition adapter over the unchanged Minecraft EAC public APIs."""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any, Callable, ClassVar, Mapping

from benchmarks.common.eac import (
    EffectRejected,
    EpistemicAdmissibility,
    ExactRequest,
    PermitLifecycle,
    PermitView,
    AttemptRecord,
)
from benchmarks.common.eac.model import EvidenceRoot
from benchmarks.common.eac.canonical import canonical_bytes
from benchmarks.minecraft.eac_runtime import (
    MinecraftEACRuntime,
    MinecraftPreparedAction,
    RUNTIME_ID,
)
from benchmarks.minecraft.k12_identity import (
    AUTHORITY_REJECTION_SCHEMA,
    FROZEN_ARGUMENT_SPECS,
    evidence_root_digest,
    exact_request_digest,
    exact_request_view,
    request_content_placeholder,
    sha256_identity,
)
from benchmarks.minecraft.k12_request import request_content_digest


class K12AuthorityAdapterError(RuntimeError):
    """The controlled public evidence does not prove the frozen rejection."""


@dataclass(frozen=True, slots=True)
class ControlledStaleEvidence:
    collector: Any
    seal: Any
    runtime: MinecraftEACRuntime
    prepared: MinecraftPreparedAction
    retained_request: ExactRequest
    retained_permit: PermitView
    retained_gateway: Any
    evaluation_before: EpistemicAdmissibility
    evaluation_after: EpistemicAdmissibility
    permit_before: PermitView
    permit_after: PermitView
    evidence_root: EvidenceRoot
    superseding_root: EvidenceRoot
    evidence_root_commitment: str
    superseding_root_commitment: str
    attempts_before: tuple[Any, ...]
    attempts_after: tuple[Any, ...]
    gateway_reason: str
    native_entry_count: int


@dataclass(frozen=True, slots=True)
class AuthorityRejectionV1:
    schema_version: ClassVar[str] = AUTHORITY_REJECTION_SCHEMA
    runtime_identity: str
    mode: str
    actor_id: str
    request_identity: str
    request_digest: str
    candidate_id: str
    attempt_id: str
    action_identity: str
    action_version: int | str
    action_digest: str
    request_content_schema: str
    request_content_digest: str
    request_content_scientific: bool
    permit_id: str
    permit_fingerprint: str
    permit_lifecycle_before: str
    permit_lifecycle_after: str
    evaluation_reasons: tuple[str, ...]
    evaluation_recoveries: tuple[str, ...]
    eadm_before: bool
    eadm_after: bool
    evidence_root_id: str
    evidence_revision: int | str
    evidence_root_digest: str
    superseding_root_id: str
    superseding_revision: int | str
    superseding_root_digest: str
    supersedes: tuple[str, ...]
    rejection_stage: str
    rejection_reason: str
    outcome_certainty: str
    retry_safe: bool
    original_attempt_absent: bool
    native_entry_count: int
    projection_digest: str

    def unsigned(self) -> dict[str, Any]:
        return {
            field.name: getattr(self, field.name)
            for field in fields(self)
            if field.name != "projection_digest"
        }

    def verify_digest(self) -> bool:
        return self.projection_digest == sha256_identity({
            "schema_version": self.schema_version,
            **self.unsigned(),
        })


@dataclass(frozen=True, slots=True)
class AdvisoryBaseline:
    admissible: bool
    native_entry_count: int
    would_block: bool


@dataclass(frozen=True, slots=True)
class AdvisoryEvidence:
    """The complete, typed K12 advisory observation and execution evidence."""
    runtime: MinecraftEACRuntime
    prepared: MinecraftPreparedAction
    original_request: ExactRequest
    original_request_digest: str
    original_content_digest: str
    evidence_root: EvidenceRoot
    superseding_root: EvidenceRoot
    evaluation: EpistemicAdmissibility
    would_block: bool
    attempt: AttemptRecord
    native_entry_count: int
    native_result: Any

    @property
    def candidate_id(self) -> str:
        return self.original_request.candidate_id

    @property
    def attempt_id(self) -> str:
        return self.attempt.attempt_id


@dataclass(frozen=True, slots=True)
class AuthorityRecoveryEvidence:
    """Public K12 evidence for stale rejection followed by semantic recovery."""
    runtime: MinecraftEACRuntime
    original: ControlledStaleEvidence
    rejection: AuthorityRejectionV1
    original_request: ExactRequest
    original_request_digest: str
    original_content_digest: str
    recovery_request: ExactRequest
    recovery_request_digest: str
    recovery_content_digest: str
    recovery_evidence_root: EvidenceRoot
    recovery_evaluation: EpistemicAdmissibility
    recovery_permit: PermitView
    recovery_attempt: AttemptRecord
    native_entry_count: int
    native_result: Any

    @property
    def original_permit(self) -> PermitView:
        return self.original.retained_permit

    @property
    def original_evaluation(self) -> EpistemicAdmissibility:
        return self.original.evaluation_before

    @property
    def original_permit_id(self) -> str:
        return self.original.retained_permit.permit_id

    @property
    def recovery_permit_id(self) -> str:
        return self.recovery_permit.permit_id

    @property
    def original_candidate_id(self) -> str:
        return self.original_request.candidate_id

    @property
    def recovery_candidate_id(self) -> str:
        return self.recovery_request.candidate_id

    @property
    def original_attempt_id(self) -> str:
        return self.original_request.attempt_id

    @property
    def recovery_attempt_id(self) -> str:
        return self.recovery_request.attempt_id

    @property
    def original_native_entry_count(self) -> int:
        return self.original.native_entry_count


@dataclass(slots=True)
class PreparedAuthorityRecovery:
    runtime: MinecraftEACRuntime
    original: ControlledStaleEvidence
    rejection: AuthorityRejectionV1
    prepared: MinecraftPreparedAction
    recovery_evidence_root: EvidenceRoot
    recovery_evaluation: EpistemicAdmissibility
    entries: list[dict[str, Any]]


@dataclass(slots=True)
class StagedAuthorityRecovery:
    runtime: MinecraftEACRuntime
    original: ControlledStaleEvidence
    rejection: AuthorityRejectionV1
    preview_request: ExactRequest
    recovery_evidence_root: EvidenceRoot
    alternative: dict[str, Any]
    entries: list[dict[str, Any]]


def _content_digest(request: ExactRequest, actor_id: str) -> str:
    return request_content_placeholder(request, actor_id=actor_id)[1]


def _request_identity(value: dict[str, Any]) -> str:
    # UTF-8 canonical JSON is immutable and retains the complete public view.
    return canonical_bytes(value).decode("utf-8")


def _attempt_present(attempts: tuple[Any, ...], attempt_id: str) -> bool:
    return any(getattr(attempt, "attempt_id", None) == attempt_id for attempt in attempts)


def project_stale_rejection(evidence: ControlledStaleEvidence) -> AuthorityRejectionV1:
    """Validate public evidence and construct the immutable `/1` projection."""
    if not isinstance(evidence, ControlledStaleEvidence):
        raise K12AuthorityAdapterError("typed controlled evidence is required")
    collector = evidence.collector
    if (not isinstance(collector, ControlledEACAdapter)
            or collector.trusted_commitments != (
                evidence.seal,
                evidence.evidence_root_commitment,
                evidence.superseding_root_commitment,
            )):
        raise K12AuthorityAdapterError("evidence commitment is not collector-anchored")
    if (type(evidence.runtime) is not MinecraftEACRuntime
            or evidence.runtime.mode != "dual_dag_authority"
            or evidence.runtime.authority.mode != "authority"):
        raise K12AuthorityAdapterError("the exact authority runtime is required")
    prepared = evidence.prepared
    request = evidence.retained_request
    before = evidence.permit_before
    after = evidence.permit_after
    if not isinstance(prepared, MinecraftPreparedAction):
        raise K12AuthorityAdapterError("typed prepared action is required")
    if (prepared.request is not request or prepared.permit is not evidence.retained_permit
            or prepared.gateway is not evidence.retained_gateway):
        raise K12AuthorityAdapterError("retained request/permit/gateway identity changed")
    if not isinstance(request, ExactRequest) or not isinstance(before, PermitView) or not isinstance(after, PermitView):
        raise K12AuthorityAdapterError("typed public request and permits are required")
    if evidence.retained_permit is not before:
        raise K12AuthorityAdapterError("issued permit identity was not retained")
    if (before.lifecycle is not PermitLifecycle.ISSUED
            or after.lifecycle is not PermitLifecycle.STALE):
        raise K12AuthorityAdapterError("permit lifecycle is not issued-to-stale")
    if (before.permit_id != after.permit_id or before.request != request
            or after.request != request or before.fingerprint != after.fingerprint):
        raise K12AuthorityAdapterError("permit identity or request binding changed")
    manifest = before.manifest
    if (manifest is None or after.manifest != manifest
            or manifest.request != request or manifest.action != request.action
            or manifest.fingerprint != before.fingerprint
            or before.mode != "authority" or after.mode != "authority"):
        raise K12AuthorityAdapterError("permit manifest binding is invalid")
    if evidence.evaluation_before.admissible is not True:
        raise K12AuthorityAdapterError("prepared request was not initially admissible")
    if evidence.evaluation_after.admissible is not False:
        raise K12AuthorityAdapterError("current evaluation is still admissible")
    if (evidence.evaluation_before.policy != manifest.policy
            or evidence.evaluation_before.profile != manifest.profile
            or evidence.evaluation_after.policy != manifest.policy
            or evidence.evaluation_after.profile != manifest.profile):
        raise K12AuthorityAdapterError("evaluation semantic binding is invalid")
    old, new = evidence.evidence_root, evidence.superseding_root
    actor_id = manifest.actor.actor_id
    if (not isinstance(old, EvidenceRoot) or not isinstance(new, EvidenceRoot)
            or old.proposition.key != new.proposition.key
            or old.proposition.polarity is not True or new.proposition.polarity is not False
            or new.supersedes != (old.root_id,)
            or not isinstance(old.revision, int) or not isinstance(new.revision, int)
            or new.revision <= old.revision
            or old.visible_to != (actor_id,) or new.visible_to != (actor_id,)
            or not old.root_id.startswith(f"minecraft-root:{evidence.runtime.run_id}:")
            or not new.root_id.startswith(f"minecraft-root:{evidence.runtime.run_id}:")
            or new.provenance_id != "minecraft-prov:" + new.root_id
            or new.source_lineage_id != new.source or new.upstream_origin_id != new.source
            or new.valid is not True or new.current is not True
            or new.source_stream_id != old.source_stream_id
            or new.source_stream_revision != new.revision
            or new.issuer != "minecraft-eac-adapter"
            or new.mapping_rule_id != "minecraft-direct-observation"):
        raise K12AuthorityAdapterError("visible evidence supersession is not exact")
    if (evidence_root_digest(old) != evidence.evidence_root_commitment
            or evidence_root_digest(new) != evidence.superseding_root_commitment):
        raise K12AuthorityAdapterError("evidence root commitment is invalid")
    if not any(old in witness.roots for witness in evidence.evaluation_before.witnesses):
        raise K12AuthorityAdapterError("initial evaluation does not bind the evidence root")
    if not any(assessment.proposition.key == old.proposition.key
               and assessment.admissible is False
               for assessment in evidence.evaluation_after.assessments):
        raise K12AuthorityAdapterError("failed evaluation does not bind the proposition")
    audit = evidence.runtime.audit_artifact()
    if not any(item.get("root_id") == new.root_id
               and item.get("actor_id") == actor_id
               and item.get("source") == new.source
               and item.get("authority_record_type") == new.root_type
               for item in audit.get("evidence_index", ())):
        raise K12AuthorityAdapterError("superseding root is not in the public runtime projection")
    if evidence.gateway_reason != "stale":
        raise K12AuthorityAdapterError("gateway rejection reason is not stale")
    if (_attempt_present(evidence.attempts_before, request.attempt_id)
            or _attempt_present(evidence.attempts_after, request.attempt_id)):
        raise K12AuthorityAdapterError("original AttemptRecord exists")
    if evidence.native_entry_count != 0:
        raise K12AuthorityAdapterError("original native callable was entered")
    # Re-read only public authority projections to prevent runtime/evaluation/
    # permit/attempt splicing in a caller-supplied evidence bundle.
    if evidence.runtime.authority.evaluate(request.candidate_id) != evidence.evaluation_after:
        raise K12AuthorityAdapterError("evaluation does not belong to the retained runtime")
    if evidence.runtime.authority.permit(after.permit_id) != after:
        raise K12AuthorityAdapterError("permit does not belong to the retained runtime")
    if evidence.runtime.authority.attempt_snapshot() != evidence.attempts_after:
        raise K12AuthorityAdapterError("attempt snapshot does not belong to the retained runtime")

    request_view = exact_request_view(request)
    content_schema, content_digest, scientific = request_content_placeholder(
        request, actor_id=actor_id,
    )
    values = {
        "runtime_identity": RUNTIME_ID,
        "mode": evidence.runtime.mode,
        "actor_id": actor_id,
        "request_identity": _request_identity(request_view),
        "request_digest": exact_request_digest(request),
        "candidate_id": request.candidate_id,
        "attempt_id": request.attempt_id,
        "action_identity": request.action.identity,
        "action_version": request.action.version,
        "action_digest": request.action.digest,
        "request_content_schema": content_schema,
        "request_content_digest": content_digest,
        "request_content_scientific": scientific,
        "permit_id": after.permit_id,
        "permit_fingerprint": after.fingerprint,
        "permit_lifecycle_before": before.lifecycle.value,
        "permit_lifecycle_after": after.lifecycle.value,
        "evaluation_reasons": tuple(evidence.evaluation_after.reasons),
        "evaluation_recoveries": tuple(evidence.evaluation_after.recoveries),
        "eadm_before": evidence.evaluation_before.admissible,
        "eadm_after": evidence.evaluation_after.admissible,
        "evidence_root_id": old.root_id,
        "evidence_revision": old.revision,
        "evidence_root_digest": evidence.evidence_root_commitment,
        "superseding_root_id": new.root_id,
        "superseding_revision": new.revision,
        "superseding_root_digest": evidence.superseding_root_commitment,
        "supersedes": new.supersedes,
        "rejection_stage": "permit_validate",
        "rejection_reason": evidence.gateway_reason,
        "outcome_certainty": "no_effect",
        "retry_safe": False,
        "original_attempt_absent": True,
        "native_entry_count": evidence.native_entry_count,
    }
    digest = sha256_identity({"schema_version": AUTHORITY_REJECTION_SCHEMA, **values})
    return AuthorityRejectionV1(**values, projection_digest=digest)


class ControlledEACAdapter:
    """Run bounded K12 cases through unchanged public EAC APIs.

    The defaults intentionally reproduce the Gate-557 MineBlock fixture.  All
    variation is data-only: the runtime and its gateway are never replaced.
    """

    def __init__(self, *, run_id: str = "k12-gate557-authority",
                 actor: str = "Alice", actor_id: str | None = None,
                 action: str = "MineBlock", action_name: str | None = None,
                 runtime_kwargs: Mapping[str, Any] | None = None,
                 tool_kwargs: Mapping[str, Any] | None = None,
                  kwargs: Mapping[str, Any] | None = None,
                  observation_arguments: Mapping[str, Any] | None = None,
                  alternative_tool_kwargs: Mapping[str, Any] | None = None,
                  native_callback: Callable[..., Any] | None = None) -> None:
        self.run_id = run_id
        self.actor = actor_id if actor_id is not None else actor
        self.action = action_name if action_name is not None else action
        if self.action not in {"MineBlock", "placeBlock", "navigateTo", "attackTarget", "handoverBlock"}:
            raise ValueError("unknown K12 action")
        supplied = tool_kwargs if tool_kwargs is not None else kwargs
        self._tool_kwargs = dict(supplied) if supplied is not None else self._default_kwargs(self.action)
        self._tool_kwargs["player_name"] = self.actor
        self._observation_arguments = dict(observation_arguments or self._tool_kwargs)
        self._observation_arguments["player_name"] = self.actor
        self._alternative_tool_kwargs = (dict(alternative_tool_kwargs)
                                         if alternative_tool_kwargs is not None else None)
        self._runtime_kwargs = dict(runtime_kwargs or {})
        self._native_callback = native_callback
        self.trusted_commitments: tuple[Any, str, str] | None = None

    @staticmethod
    def _kwargs() -> dict[str, Any]:
        return {
            "player_name": "Alice", "x": 1, "y": 2, "z": 3,
            "emotion": [], "murmur": "",
        }

    @staticmethod
    def _default_kwargs(action: str) -> dict[str, Any]:
        values = {
            "MineBlock": {"x": 1, "y": 2, "z": 3, "emotion": [], "murmur": ""},
            "placeBlock": {"x": 1, "y": 2, "z": 3, "item_name": "stone", "facing": "east"},
            "navigateTo": {"x": 4, "y": 2, "z": 3},
            "attackTarget": {"target_name": "zombie", "emotion": ["😢"], "murmur": ""},
            "handoverBlock": {"target_player_name": "Bob", "item_name": "stone", "item_count": 1},
        }
        return dict(values[action])

    def _runtime(self, mode: str, run_id: str | None = None) -> MinecraftEACRuntime:
        options = dict(self._runtime_kwargs)
        if "mode" in options and options["mode"] != mode:
            raise ValueError("runtime_kwargs mode conflicts with controlled flow")
        if "run_id" in options and run_id is not None and options["run_id"] != run_id:
            raise ValueError("runtime_kwargs run_id conflicts with controlled flow")
        options.setdefault("mode", mode)
        options.setdefault("run_id", run_id or self.run_id)
        options.setdefault("env_prechecks", {self.action: lambda unused: True})
        options.setdefault("sec_prechecks", {self.action: lambda unused: True})
        return MinecraftEACRuntime(**options)

    def _native(self, entries: list[dict[str, Any]]) -> Callable[..., Any]:
        callback = self._native_callback
        def native(**call_kwargs: Any) -> Any:
            entries.append(dict(call_kwargs))
            if callback is not None:
                return callback(**dict(call_kwargs))
            return {"status": True}
        return native

    def collect(self) -> ControlledStaleEvidence:
        native_entries: list[dict[str, Any]] = []
        runtime = self._runtime("dual_dag_authority")
        observation = dict(self._observation_arguments)
        observation.pop("player_name", None)
        positive = runtime.ingest_target_observation(
            self.actor, self.action, observation, revision=1,
        )
        prepared = runtime.prepare_tool(self.action, self._native(native_entries), (), self._tool_kwargs)
        request, permit, gateway = prepared.request, prepared.permit, prepared.gateway
        if not isinstance(permit, PermitView):
            raise K12AuthorityAdapterError("authority preparation did not issue a permit")
        evaluation_before = runtime.authority.evaluate(request.candidate_id)
        permit_before = permit
        attempts_before = runtime.authority.attempt_snapshot()
        negative = runtime.ingest_actor_record(
            actor_id=self.actor,
            proposition=replace(positive.proposition, polarity=False),
            record_type="direct_observation",
            source="minecraft-k12-controlled-visible-invalidation",
            revision=2,
            supersedes=(positive.root_id,),
        )
        evaluation_after = runtime.authority.evaluate(request.candidate_id)
        permit_after = runtime.authority.permit(permit.permit_id)
        reason = ""
        try:
            gateway.execute(request, permit)
        except EffectRejected as error:
            reason = error.reason
        else:
            raise K12AuthorityAdapterError("stale gateway invocation unexpectedly succeeded")
        attempts_after = runtime.authority.attempt_snapshot()
        old_commitment = evidence_root_digest(positive)
        new_commitment = evidence_root_digest(negative)
        seal = object()
        self.trusted_commitments = (seal, old_commitment, new_commitment)
        return ControlledStaleEvidence(
            collector=self,
            seal=seal,
            runtime=runtime,
            prepared=prepared,
            retained_request=request,
            retained_permit=permit,
            retained_gateway=gateway,
            evaluation_before=evaluation_before,
            evaluation_after=evaluation_after,
            permit_before=permit_before,
            permit_after=permit_after,
            evidence_root=positive,
            superseding_root=negative,
            evidence_root_commitment=old_commitment,
            superseding_root_commitment=new_commitment,
            attempts_before=attempts_before,
            attempts_after=attempts_after,
            gateway_reason=reason,
            native_entry_count=len(native_entries),
        )

    def run(self) -> AuthorityRejectionV1:
        return project_stale_rejection(self.collect())

    def _alternative_kwargs(self) -> dict[str, Any]:
        if self._alternative_tool_kwargs is not None:
            values = dict(self._alternative_tool_kwargs)
            values["player_name"] = self.actor
            return values
        values = dict(self._tool_kwargs)
        alternatives = {
            "MineBlock": {"x": 4, "y": 2, "z": 3},
            "placeBlock": {"x": 4, "y": 2, "z": 3, "facing": "west"},
            "navigateTo": {"x": 8, "y": 2, "z": 3},
            "attackTarget": {"target_name": "skeleton"},
            "handoverBlock": {"target_player_name": "villager2"},
        }
        values.update(alternatives[self.action])
        values["player_name"] = self.actor
        return values

    def collect_advisory(self) -> AdvisoryEvidence:
        entries: list[dict[str, Any]] = []
        runtime = self._runtime("dual_dag_advisory", "k12-gate557-advisory")
        observation = dict(self._observation_arguments)
        observation.pop("player_name", None)
        root = runtime.ingest_target_observation(self.actor, self.action, observation, revision=1)
        prepared = runtime.prepare_tool(self.action, self._native(entries), (), self._tool_kwargs)
        negative = runtime.ingest_actor_record(
            actor_id=self.actor, proposition=replace(root.proposition, polarity=False),
            record_type="direct_observation", source="minecraft-k12-controlled-visible-invalidation",
            revision=2, supersedes=(root.root_id,),
        )
        result = runtime.execute_prepared(prepared)
        attempts = runtime.authority.attempt_snapshot()
        matching = tuple(item for item in attempts if item.attempt_id == prepared.request.attempt_id)
        if len(matching) != 1 or len(entries) != 1:
            raise K12AuthorityAdapterError("advisory execution did not retain one AttemptRecord and native entry")
        decision = runtime.authority.evaluate(prepared.request.candidate_id)
        return AdvisoryEvidence(
            runtime=runtime, prepared=prepared, original_request=prepared.request,
            original_request_digest=exact_request_digest(prepared.request),
            original_content_digest=_content_digest(prepared.request, self.actor),
            evidence_root=root, superseding_root=negative, evaluation=decision,
            would_block=bool(matching[0].would_block), attempt=matching[0],
            native_entry_count=len(entries), native_result=result,
        )

    def run_advisory(self) -> AdvisoryEvidence:
        return self.collect_advisory()

    def prepare_authority_recovery(self) -> PreparedAuthorityRecovery:
        original = self.collect()
        rejection = project_stale_rejection(original)
        return self.prepare_recovery(original, rejection)

    def stage_recovery(self, original: ControlledStaleEvidence,
                       rejection: AuthorityRejectionV1) -> StagedAuthorityRecovery:
        if rejection != project_stale_rejection(original):
            raise K12AuthorityAdapterError("recovery rejection is not bound to original evidence")
        runtime = original.runtime
        alternative = self._alternative_kwargs()
        action_view = exact_request_view(original.retained_request)["action"]
        semantic_arguments = {
            name: alternative[name]
            for name in FROZEN_ARGUMENT_SPECS[self.action]
            if name in alternative
        }
        alternative_content = request_content_digest(
            self.actor, action_view, semantic_arguments, semantic_arguments,
        )
        if alternative_content == rejection.request_content_digest:
            raise K12AuthorityAdapterError("recovery request repeats rejected semantic content")
        visible = dict(alternative)
        visible.pop("player_name", None)
        recovery_root = runtime.ingest_target_observation(self.actor, self.action, visible, revision=3)
        entries: list[dict[str, Any]] = []
        classification = runtime.classification_for(self.action)
        arguments = runtime.bind_tool_arguments(self._native(entries), (), alternative)
        arguments.pop("player_name", None)
        proposition = runtime._proposition(classification, arguments)
        unused_definition, action, unused_epre, unused_ref, unused_declared = runtime._definitions(
            classification, proposition)
        candidate_id = f"{runtime.run_id}:{runtime._sequence + 1}:{self.action}"
        preview = ExactRequest(
            candidate_id, candidate_id + ":attempt", action,
            tuple((key, value) for key, value in arguments.items()),
            target={key: arguments[key] for key in classification["argument_fields"]
                    if key in arguments},
        )
        return StagedAuthorityRecovery(runtime, original, rejection, preview, recovery_root,
                                       alternative, entries)

    def issue_recovery(self, staged: StagedAuthorityRecovery) -> PreparedAuthorityRecovery:
        if not isinstance(staged, StagedAuthorityRecovery):
            raise K12AuthorityAdapterError("staged recovery is required")
        runtime, original, rejection = staged.runtime, staged.original, staged.rejection
        if runtime is not original.runtime or rejection != project_stale_rejection(original):
            raise K12AuthorityAdapterError("staged recovery binding changed")
        prepared = runtime.prepare_tool(self.action, self._native(staged.entries), (), staged.alternative)
        if prepared.request != staged.preview_request:
            raise K12AuthorityAdapterError("issued recovery differs from parent-staged request")
        permit = prepared.permit
        if not isinstance(permit, PermitView) or permit.lifecycle is not PermitLifecycle.ISSUED:
            raise K12AuthorityAdapterError("recovery preparation did not issue a fresh permit")
        if prepared.request == original.retained_request:
            raise K12AuthorityAdapterError("recovery request is not semantically novel")
        evaluation = runtime.authority.evaluate(prepared.request.candidate_id)
        if evaluation.admissible is not True:
            raise K12AuthorityAdapterError("recovery request is not admissible")
        return PreparedAuthorityRecovery(runtime, original, rejection, prepared,
                                         staged.recovery_evidence_root, evaluation, staged.entries)

    def prepare_recovery(self, original: ControlledStaleEvidence,
                         rejection: AuthorityRejectionV1) -> PreparedAuthorityRecovery:
        return self.issue_recovery(self.stage_recovery(original, rejection))

    def execute_authority_recovery(self, recovery: PreparedAuthorityRecovery) -> AuthorityRecoveryEvidence:
        runtime, original, rejection, prepared = (
            recovery.runtime, recovery.original, recovery.rejection, recovery.prepared)
        permit = prepared.permit
        if not isinstance(permit, PermitView):
            raise K12AuthorityAdapterError("prepared recovery permit is invalid")
        result = runtime.execute_prepared(prepared)
        attempts = tuple(item for item in runtime.authority.attempt_snapshot()
                         if item.attempt_id == prepared.request.attempt_id)
        if len(attempts) != 1 or len(recovery.entries) != 1:
            raise K12AuthorityAdapterError("recovery did not retain one AttemptRecord and native entry")
        return AuthorityRecoveryEvidence(
            runtime=runtime, original=original, rejection=rejection,
            original_request=original.retained_request,
            original_request_digest=exact_request_digest(original.retained_request),
            original_content_digest=_content_digest(original.retained_request, self.actor),
            recovery_request=prepared.request,
            recovery_request_digest=(attempts[0].request_digest
                                     or exact_request_digest(prepared.request)),
            recovery_content_digest=_content_digest(prepared.request, self.actor),
            recovery_evidence_root=recovery.recovery_evidence_root,
            recovery_evaluation=recovery.recovery_evaluation,
            recovery_permit=permit, recovery_attempt=attempts[0],
            native_entry_count=len(recovery.entries), native_result=result,
        )

    def collect_authority_recovery(self) -> AuthorityRecoveryEvidence:
        return self.execute_authority_recovery(self.prepare_authority_recovery())

    def run_authority_recovery(self) -> AuthorityRecoveryEvidence:
        return self.collect_authority_recovery()

    @staticmethod
    def advisory_baseline() -> AdvisoryBaseline:
        entries: list[dict[str, Any]] = []
        runtime = MinecraftEACRuntime(
            mode="dual_dag_advisory",
            run_id="k12-gate557-advisory",
            env_prechecks={"MineBlock": lambda unused: True},
        )
        runtime.ingest_target_observation(
            "Alice", "MineBlock", {"x": 1, "y": 2, "z": 3}, revision=1,
        )

        def native(**kwargs: Any) -> dict[str, Any]:
            entries.append(dict(kwargs))
            return {"status": True}

        prepared = runtime.prepare_tool("MineBlock", native, (), ControlledEACAdapter._kwargs())
        decision = runtime.authority.evaluate(prepared.request.candidate_id)
        runtime.execute_prepared(prepared)
        attempts = runtime.authority.attempt_snapshot()
        if len(attempts) != 1 or len(entries) != 1:
            raise K12AuthorityAdapterError("unchanged advisory baseline is unavailable")
        return AdvisoryBaseline(decision.admissible, len(entries), bool(attempts[0].would_block))


def run_controlled_stale_flow() -> AuthorityRejectionV1:
    return ControlledEACAdapter().run()


__all__ = [
    "AdvisoryBaseline",
    "AdvisoryEvidence",
    "AuthorityRecoveryEvidence",
    "AuthorityRejectionV1",
    "ControlledEACAdapter",
    "ControlledStaleEvidence",
    "K12AuthorityAdapterError",
    "PreparedAuthorityRecovery",
    "project_stale_rejection",
    "run_controlled_stale_flow",
]
