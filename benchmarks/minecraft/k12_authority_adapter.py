"""K12 composition adapter over the unchanged Minecraft EAC public APIs."""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any, ClassVar

from benchmarks.common.eac import (
    EffectRejected,
    EpistemicAdmissibility,
    ExactRequest,
    PermitLifecycle,
    PermitView,
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
    evidence_root_digest,
    exact_request_digest,
    exact_request_view,
    request_content_placeholder,
    sha256_identity,
)


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
            or collector._trusted_commitments != (
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
    content_schema, content_digest, scientific = request_content_placeholder(request)
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
    """Run the one controlled stale case through unchanged public EAC APIs."""

    def __init__(self, *, run_id: str = "k12-gate557-authority") -> None:
        self.run_id = run_id
        self._trusted_commitments: tuple[Any, str, str] | None = None

    @staticmethod
    def _kwargs() -> dict[str, Any]:
        return {
            "player_name": "Alice", "x": 1, "y": 2, "z": 3,
            "emotion": [], "murmur": "",
        }

    def collect(self) -> ControlledStaleEvidence:
        native_entries: list[dict[str, Any]] = []
        runtime = MinecraftEACRuntime(
            mode="dual_dag_authority",
            run_id=self.run_id,
            env_prechecks={"MineBlock": lambda unused: True},
        )
        positive = runtime.ingest_target_observation(
            "Alice", "MineBlock", {"x": 1, "y": 2, "z": 3}, revision=1,
        )

        def native(**kwargs: Any) -> dict[str, Any]:
            native_entries.append(dict(kwargs))
            return {"status": True}

        prepared = runtime.prepare_tool("MineBlock", native, (), self._kwargs())
        request, permit, gateway = prepared.request, prepared.permit, prepared.gateway
        if not isinstance(permit, PermitView):
            raise K12AuthorityAdapterError("authority preparation did not issue a permit")
        evaluation_before = runtime.authority.evaluate(request.candidate_id)
        permit_before = permit
        attempts_before = runtime.authority.attempt_snapshot()
        negative = runtime.ingest_actor_record(
            actor_id="Alice",
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
        self._trusted_commitments = (seal, old_commitment, new_commitment)
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
    "AuthorityRejectionV1",
    "ControlledEACAdapter",
    "ControlledStaleEvidence",
    "K12AuthorityAdapterError",
    "project_stale_rejection",
    "run_controlled_stale_flow",
]
