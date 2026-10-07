"""Minecraft adapters for the shared EAC Runtime Authority.

This module owns no witness or permit semantics. It classifies Minecraft tool
calls and actor-visible records, then delegates those semantics to
``benchmarks.common.eac``.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections import deque
from dataclasses import dataclass, fields, is_dataclass, replace
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterable, Mapping

from benchmarks.common.eac import (
    ActionRef, ActorScope, AuthorityError, EPreRef, EffectGateway, EffectRejected,
    ExactRequest, Proposition, PropositionKey, ProvenanceRecord, RuntimeAuthority,
    bind_source_profile, load_support_policy,
)
from benchmarks.common.eac.model import EvidenceRoot, NativeEffectResult
from benchmarks.common.eac.canonical import canonical_bytes, thaw_json
from benchmarks.common.eac.policy import match_mapping
from env.eac_observation_adapter import sanitized_scan_rows, sanitized_visible_blocks
from benchmarks.common.eac.authority import _plain as _authority_plain
from benchmarks.common.eac.witness import MAX_DERIVATIONS, MAX_PROVENANCE, MAX_ROOTS
from env.runtime_paths import atomic_write_json

ROOT = Path(__file__).resolve().parents[2]
CLASSIFICATION_PATH = ROOT / "docs/eac/minecraft_preconditions_v1.json"
SOURCE_PROFILE_PATH = ROOT / "docs/eac/minecraft_source_profile_v1.json"
INGESTION_CONTRACT_PATH = ROOT / "docs/eac/minecraft_ingestion_contract_v1.json"
SOURCE_PROFILE_V2_PATH = ROOT / "docs/eac/minecraft_source_profile_v2.json"
INGESTION_CONTRACT_V2_PATH = ROOT / "docs/eac/minecraft_ingestion_contract_v2.json"
K11_EVIDENCE_IMPLEMENTATION_PATH = "benchmarks/minecraft/k11_hold_evidence.py"
K11_SENSOR_IMPLEMENTATION_PATH = "env/k11_visible_block_capture.js"
K11_V2_IMPLEMENTATION_PATHS = (
    "env/k11_visible_block_capture.js",
    "env/minecraft_server_fast.py",
    "env/minecraft_client.py",
    "benchmarks/minecraft/k11_hold_evidence.py",
    "benchmarks/minecraft/eac_runtime.py",
    "benchmarks/minecraft/k11_hold_protocol.py",
    "benchmarks/minecraft/k11_hold_trace.py",
)
K11_IMPLEMENTATION_PATHS = K11_V2_IMPLEMENTATION_PATHS
RUNTIME_ID = "minecraft-eac-runtime-v1"
RUNTIME_ID_V2 = "minecraft-eac-runtime-v2"
SUPPORTED_MODES = frozenset(("dual_dag_advisory", "dual_dag_authority"))
K11_PASSIVE_RULE_ID = "minecraft-k11-passive-direct"
K11_PASSIVE_RECORD_TYPE = "k11_passive_observation"
K11_PASSIVE_STREAM_ID = "minecraft-k11-passive-state"
K11_PASSIVE_ISSUER = "minecraft-k11-passive-sensor"
K11_PROVENANCE_FIELDS = frozenset((
    "bridge_id", "capture_seq", "request_digest", "response_digest",
    "cell_payload_digest", "cell_state", "block_name", "registry_id",
    "coverage_sha256", "sensor_id", "sensor_digest", "profile_digest",
    "ingestion_digest", "geometry_id", "geometry_digest",
))
K11_PROVENANCE_MAX_BYTES = 4096
_K11_BRIDGE_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_K11_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_K11_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
_K11_BLOCK_NAME_RE = re.compile(r"[a-z0-9_]+\Z")
FORBIDDEN_EVIDENCE_ORIGINS = frozenset((
    "score", "final_score", "progress", "evaluator", "meta_judger",
    "simulator_truth", "experiment_oracle", "post_hoc_action_log",
))


class MinecraftEACError(RuntimeError):
    pass


class K11CapacityExhausted(MinecraftEACError):
    """A complete K11 snapshot would exceed the shared witness bounds."""

    def __init__(self, counts: Mapping[str, int]):
        self.counts = dict(counts)
        super().__init__("K11 evidence capacity exhausted: " + ", ".join(
            f"{name}={value}" for name, value in self.counts.items()))


@dataclass(frozen=True, slots=True)
class MinecraftPreparedAction:
    tool_name: str
    request: ExactRequest
    gateway: EffectGateway
    permit: Any = None
    arguments: tuple[tuple[str, Any], ...] = ()


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(_plain(value))).hexdigest()


def _minecraft_identifier(value: Any) -> Any:
    return value.lower().replace(" ", "_") if isinstance(value, str) else value


def _load_json(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise MinecraftEACError(f"EAC artifact is not an object: {path}")
    return value


def _authenticate_classification(value: Mapping[str, Any]) -> str:
    declared = value.get("detached_artifact_sha256")
    detached = dict(value)
    detached.pop("detached_artifact_sha256", None)
    observed = _digest(detached)
    if declared != observed:
        raise MinecraftEACError("Minecraft EPre classification digest mismatch")
    return observed


def _authenticate_ingestion_contract(
        value: Mapping[str, Any], *, expected_implementation_path: str | None = None,
) -> tuple[str, str]:
    detached = dict(value)
    declared = detached.pop("detached_artifact_sha256", None)
    observed = _digest(detached)
    if declared != observed:
        raise MinecraftEACError("Minecraft ingestion contract digest mismatch")
    adapter = value["trusted_observation_adapter"]
    if (expected_implementation_path is not None
            and (not isinstance(adapter, Mapping)
                 or adapter.get("implementation_path") != expected_implementation_path)):
        raise MinecraftEACError("Minecraft observation adapter implementation path mismatch")
    implementation = ROOT / adapter["implementation_path"]
    try:
        implementation_digest = hashlib.sha256(implementation.read_bytes()).hexdigest()
    except OSError as exc:
        if expected_implementation_path is None:
            raise
        raise MinecraftEACError(
            "Minecraft observation adapter implementation is unavailable") from exc
    if implementation_digest != adapter["implementation_sha256"]:
        raise MinecraftEACError("Minecraft observation adapter implementation digest mismatch")
    tool_digest = _digest(value["trusted_observation_adapter"])
    rule_digest = _digest(value["rule_evaluation"])
    return tool_digest, rule_digest


def _authenticate_v2_implementation(contract: Mapping[str, Any]) -> Mapping[str, str]:
    """Authenticate the exact, versioned seven-source K11 implementation set."""
    manifest = contract.get("implementation_manifest")
    if (type(contract.get("implementation_manifest_version")) is not int
            or contract.get("implementation_manifest_version") != 1
            or not isinstance(manifest, Mapping)
            or set(manifest) != set(K11_V2_IMPLEMENTATION_PATHS)):
        raise MinecraftEACError(
            "Minecraft v2 implementation manifest version or exact source set mismatch")

    observed_manifest: dict[str, str] = {}
    for relative_path in K11_V2_IMPLEMENTATION_PATHS:
        declared = manifest.get(relative_path)
        if not isinstance(declared, str) or _K11_SHA256_RE.fullmatch(declared) is None:
            raise MinecraftEACError(
                f"Minecraft v2 implementation manifest digest is invalid: {relative_path}")
        implementation = ROOT / relative_path
        try:
            observed = hashlib.sha256(implementation.read_bytes()).hexdigest()
        except OSError as exc:
            raise MinecraftEACError(
                f"Minecraft v2 implementation is unavailable: {relative_path}") from exc
        if observed != declared:
            raise MinecraftEACError(
                "Minecraft v2 implementation digest mismatch in implementation manifest: "
                f"{relative_path}")
        observed_manifest[relative_path] = observed
    return observed_manifest


def _authenticate_v2_sensor_implementation(contract: Mapping[str, Any]) -> str:
    """Retain the explicit fixed-path check for the authenticated sensor helper."""
    adapter = contract.get("trusted_observation_adapter")
    if (not isinstance(adapter, Mapping)
            or adapter.get("sensor_implementation_path") != K11_SENSOR_IMPLEMENTATION_PATH):
        raise MinecraftEACError("Minecraft v2 sensor implementation path mismatch")
    implementation = ROOT / K11_SENSOR_IMPLEMENTATION_PATH
    try:
        observed = hashlib.sha256(implementation.read_bytes()).hexdigest()
    except OSError as exc:
        raise MinecraftEACError("Minecraft v2 sensor implementation is unavailable") from exc
    if observed != adapter.get("sensor_implementation_sha256"):
        raise MinecraftEACError("Minecraft v2 sensor implementation digest mismatch")
    return observed


def _authenticate_v2_profile(profile: Mapping[str, Any], contract: Mapping[str, Any],
                             tool_digest: str, rule_digest: str,
                             legacy_tool_digest: str) -> None:
    """Bind the opt-in K11 profile to its contract without changing v1."""
    if (profile.get("profile_id"), profile.get("profile_version")) != (
            "minecraft-eac-k11-fixed-passive", 2):
        raise MinecraftEACError("Minecraft SourceProfile v2 identity mismatch")
    if (contract.get("artifact_id"), contract.get("artifact_version")) != (
            "minecraft-eac-ingestion-contract", 2):
        raise MinecraftEACError("Minecraft ingestion contract v2 identity mismatch")

    mapping_rules = profile.get("mapping_rules")
    if not isinstance(mapping_rules, list):
        raise MinecraftEACError("Minecraft SourceProfile v2 mapping rules are invalid")
    unique_priorities = set()
    for mapping_rule in mapping_rules:
        if (not isinstance(mapping_rule, Mapping)
                or not isinstance(mapping_rule.get("record_namespace"), str)
                or not isinstance(mapping_rule.get("record_type"), str)
                or type(mapping_rule.get("priority")) is not int):
            raise MinecraftEACError("Minecraft SourceProfile v2 mapping rule is invalid")
        priority_key = (mapping_rule["record_namespace"], mapping_rule["record_type"],
                        mapping_rule["priority"])
        if priority_key in unique_priorities:
            raise MinecraftEACError(
                "Minecraft SourceProfile v2 has duplicate mapping rules at equal priority")
        unique_priorities.add(priority_key)

    rules = [item for item in mapping_rules
             if isinstance(item, Mapping) and item.get("rule_id") == K11_PASSIVE_RULE_ID]
    if (len(rules) != 1 or rules[0].get("record_namespace") != "minecraft"
            or rules[0].get("record_type") != K11_PASSIVE_RECORD_TYPE
            or rules[0].get("root_type") != "direct_observation"):
        raise MinecraftEACError("Minecraft K11 passive mapping rule mismatch")

    streams = [item for item in profile.get("supersession_streams", ())
               if isinstance(item, Mapping)
               and item.get("source_stream_id") == K11_PASSIVE_STREAM_ID]
    if (len(streams) != 1 or streams[0].get("authorized_issuer") != K11_PASSIVE_ISSUER
            or streams[0].get("revision_field") != "source_stream_revision"
            or streams[0].get("tracked_proposition_rule_id") != K11_PASSIVE_RULE_ID):
        raise MinecraftEACError("Minecraft K11 passive supersession stream mismatch")

    integrity = profile.get("integrity_contract")
    issuer_authentication = contract.get("issuer_authentication")
    if (not isinstance(integrity, Mapping)
            or not isinstance(issuer_authentication, Mapping)
            or integrity.get("canonical_content_sha256")
            != contract.get("detached_artifact_sha256")
            or integrity.get("rule_evaluation_contract_sha256") != rule_digest
            or integrity.get("issuer_authentication_rule_id")
            != issuer_authentication.get("rule_id")):
        raise MinecraftEACError("Minecraft SourceProfile v2 integrity contract mismatch")

    adapter = contract.get("trusted_observation_adapter")
    if (not isinstance(adapter, Mapping)
            or adapter.get("implementation_path") != K11_EVIDENCE_IMPLEMENTATION_PATH):
        raise MinecraftEACError("Minecraft v2 observation adapter binding is missing")
    adapter_identity = adapter.get("tool_identity")
    adapter_version = adapter.get("tool_version")
    trusted_tools = profile.get("trusted_tools")
    if (not isinstance(adapter_identity, str) or not adapter_identity
            or not isinstance(adapter_version, str) or not adapter_version
            or not isinstance(trusted_tools, list)):
        raise MinecraftEACError("Minecraft v2 trusted adapter identity is invalid")
    matching_tools = [item for item in trusted_tools
                      if isinstance(item, Mapping)
                      and item.get("tool_identity") == adapter_identity
                      and item.get("tool_version") == adapter_version]
    if (len(matching_tools) != 1
            or matching_tools[0].get("integrity_contract_sha256") != tool_digest):
        raise MinecraftEACError("Minecraft SourceProfile v2 trusted adapter binding mismatch")
    legacy_tools = [item for item in trusted_tools
                    if isinstance(item, Mapping)
                    and item.get("tool_identity") == "minecraft-observation-adapter"
                    and item.get("tool_version") == "1"]
    if (len(legacy_tools) != 1
            or legacy_tools[0].get("integrity_contract_sha256") != legacy_tool_digest):
        raise MinecraftEACError("Minecraft SourceProfile v2 retained legacy tool binding mismatch")


def _validate_k11_provenance(value: Mapping[str, Any], *, revision: int,
                             profile_digest: str,
                             ingestion_digest: str) -> tuple[tuple[str, Any], ...]:
    """Copy and validate bounded safe provenance; revision is capture metadata."""
    if not isinstance(value, Mapping):
        raise MinecraftEACError("K11 provenance must be a mapping")
    try:
        metadata = dict(value)
    except (TypeError, ValueError):
        raise MinecraftEACError("K11 provenance must be a plain field mapping") from None
    if set(metadata) != K11_PROVENANCE_FIELDS:
        raise MinecraftEACError("K11 provenance fields do not match the safe allowlist")

    if (not isinstance(metadata["bridge_id"], str)
            or _K11_BRIDGE_ID_RE.fullmatch(metadata["bridge_id"]) is None):
        raise MinecraftEACError("K11 provenance bridge identity is invalid")
    capture_seq = metadata["capture_seq"]
    registry_id = metadata["registry_id"]
    if (type(capture_seq) is not int or not 1 <= capture_seq <= (2**63 - 1)
            or capture_seq != revision):
        raise MinecraftEACError("K11 provenance capture sequence does not match its metadata")
    if type(registry_id) is not int or not 0 <= registry_id <= (2**63 - 1):
        raise MinecraftEACError("K11 provenance registry identity is invalid")
    if (not isinstance(metadata["cell_state"], str)
            or metadata["cell_state"] not in {"known_air", "known_non_air"}):
        raise MinecraftEACError("K11 provenance cell state is invalid")
    if (not isinstance(metadata["block_name"], str)
            or _K11_BLOCK_NAME_RE.fullmatch(metadata["block_name"]) is None):
        raise MinecraftEACError("K11 provenance block name is invalid")
    for field in ("sensor_id", "geometry_id"):
        if (not isinstance(metadata[field], str)
                or _K11_IDENTIFIER_RE.fullmatch(metadata[field]) is None):
            raise MinecraftEACError(f"K11 provenance {field} is invalid")
    for field in ("request_digest", "response_digest", "cell_payload_digest",
                  "coverage_sha256", "sensor_digest", "profile_digest",
                  "ingestion_digest", "geometry_digest"):
        if (not isinstance(metadata[field], str)
                or _K11_SHA256_RE.fullmatch(metadata[field]) is None):
            raise MinecraftEACError(f"K11 provenance {field} is invalid")
    if (metadata["profile_digest"] != profile_digest
            or metadata["ingestion_digest"] != ingestion_digest):
        raise MinecraftEACError("K11 provenance artifact digests do not match the runtime")

    ordered = tuple((field, metadata[field]) for field in sorted(K11_PROVENANCE_FIELDS))
    try:
        encoded = canonical_bytes(dict(ordered))
    except (TypeError, ValueError, UnicodeError):
        raise MinecraftEACError("K11 provenance is not canonically encodable") from None
    if len(encoded) > K11_PROVENANCE_MAX_BYTES:
        raise MinecraftEACError("K11 provenance exceeds the bounded metadata limit")
    return ordered


class MinecraftEACRuntime:
    """One immutable Minecraft Advisory or Authority runtime.

    Only wrappers installed by :meth:`VillagerBench.guard_tool_actions` are in
    scope. Direct ``Agent`` calls and direct bridge HTTP calls are excluded.
    """

    def __init__(self, *, mode: str, run_id: str,
                 env_prechecks: Mapping[str, Callable[[ExactRequest], bool]] | None = None,
                 sec_prechecks: Mapping[str, Callable[[ExactRequest], bool]] | None = None,
                 audit_path: str | Path | None = None,
                 identity_binding: Mapping[str, Any] | None = None,
                 source_version: int = 1):
        if mode not in SUPPORTED_MODES:
            raise ValueError(f"unsupported Minecraft EAC mode: {mode}")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("Minecraft EAC run_id is required")
        if type(source_version) is not int or source_version not in (1, 2):
            raise ValueError("Minecraft EAC source_version must be 1 or 2")
        self.mode = mode
        self.run_id = run_id
        self.source_version = source_version
        self.runtime_identity = RUNTIME_ID if source_version == 1 else RUNTIME_ID_V2
        self.identity_binding = _plain(dict(identity_binding)) if identity_binding is not None else None
        self._k11_lineage = "minecraft-k11-passive-sensor:" + run_id
        self.classification = _load_json(CLASSIFICATION_PATH)
        self._classification_digest = _authenticate_classification(self.classification)
        profile_path = SOURCE_PROFILE_PATH if source_version == 1 else SOURCE_PROFILE_V2_PATH
        contract_path = INGESTION_CONTRACT_PATH if source_version == 1 else INGESTION_CONTRACT_V2_PATH
        self.profile_document = _load_json(profile_path)
        self.ingestion_contract = _load_json(contract_path)
        if source_version == 2:
            _authenticate_v2_implementation(self.ingestion_contract)
        tool_digest, rule_digest = _authenticate_ingestion_contract(
            self.ingestion_contract,
            expected_implementation_path=(
                K11_EVIDENCE_IMPLEMENTATION_PATH if source_version == 2 else None),
        )
        if source_version == 1:
            trusted_tool = self.profile_document["trusted_tools"][0]
            integrity = self.profile_document["integrity_contract"]
            if (trusted_tool["integrity_contract_sha256"] != tool_digest
                    or integrity["canonical_content_sha256"] != self.ingestion_contract["detached_artifact_sha256"]
                    or integrity["rule_evaluation_contract_sha256"] != rule_digest):
                raise MinecraftEACError("Minecraft SourceProfile integrity contract mismatch")
        else:
            _authenticate_v2_sensor_implementation(self.ingestion_contract)
            legacy_contract = _load_json(INGESTION_CONTRACT_PATH)
            legacy_tool_digest, _ = _authenticate_ingestion_contract(legacy_contract)
            _authenticate_v2_profile(
                self.profile_document, self.ingestion_contract, tool_digest, rule_digest,
                legacy_tool_digest,
            )
        self.policy_binding = load_support_policy()
        self.profile_binding = bind_source_profile(self.profile_document)
        if source_version == 2 and self.identity_binding is not None:
            source_identity = self.identity_binding.get("eac_source_profile")
            if (self.identity_binding.get("execution_identity") != RUNTIME_ID_V2
                    or not isinstance(source_identity, Mapping)
                    or source_identity.get("identity") != self.profile_binding.profile_id
                    or source_identity.get("version") != self.profile_binding.profile_version
                    or source_identity.get("digest") != self.profile_binding.digest_sha256):
                raise MinecraftEACError("v2 execution identity binding mismatch")
        authority_mode = "authority" if mode == "dual_dag_authority" else "advisory"
        self._k11_commit_gate: tuple[object, Mapping[str, Any], Proposition,
                                      str, int] | None = None
        self._k11_verifier_adapter: object | None = None
        self._k11_verifier_token: object | None = None
        self.authority = RuntimeAuthority(
            policy_binding=self.policy_binding,
            profile_binding=self.profile_binding,
            mode=authority_mode,
            source_authenticator=self._authenticate_record,
            authority_nonce="minecraft-eac:" + run_id,
        )
        self._actions = {item["action_identity"]: item for item in self.classification["actions"]}
        self._env_prechecks = dict(env_prechecks or {})
        self._sec_prechecks = dict(sec_prechecks or {})
        for name, item in self._actions.items():
            if item["sec_pre"] and name not in self._sec_prechecks:
                raise MinecraftEACError(f"classified SecPre requires an explicit adapter: {name}")
        self._sequence = 0
        self._lock = RLock()
        self._records = deque(maxlen=256)
        self._evidence_total = 0
        self._current_roots: dict[tuple[str, PropositionKey], str] = {}
        self._k11_current_roots: dict[tuple[str, PropositionKey], str] = {}
        self._k11_last_failure: dict[str, Any] | None = None
        self._fluent_revision = 0
        self._initial_state_ingested: set[str] = set()
        self._last_permit: dict[str, Any] = {}
        self._audit_path = Path(audit_path) if audit_path is not None else None
        self._persist_audit()

    @property
    def classification_identity(self) -> str:
        return self._classification_digest

    @property
    def source_profile_identity(self) -> str:
        return self.profile_binding.digest_sha256

    def _bind_k11_verifier(self, adapter: object) -> object:
        """Bind the sole v2 passive-evidence verifier after adapter validation."""
        with self._lock:
            if self.source_version != 2:
                raise MinecraftEACError("K11 verifier binding requires source_version=2")
            from benchmarks.minecraft.k11_hold_evidence import K11HoldEvidenceAdapter

            if (type(adapter) is not K11HoldEvidenceAdapter
                    or getattr(adapter, "runtime", None) is not self
                    or getattr(adapter, "_admission_ready", None) is not True):
                raise MinecraftEACError(
                    "K11 verifier requires a ready K11HoldEvidenceAdapter bound to this runtime")
            if self._k11_verifier_adapter is not None:
                raise MinecraftEACError("K11 verifier is already bound")
            token = object()
            self._k11_verifier_adapter = adapter
            self._k11_verifier_token = token
            return token

    def _require_k11_verification_token(self, verification_token: object) -> None:
        from benchmarks.minecraft.k11_hold_evidence import K11HoldEvidenceAdapter

        adapter = self._k11_verifier_adapter
        if (self.source_version != 2
                or type(adapter) is not K11HoldEvidenceAdapter
                or getattr(adapter, "runtime", None) is not self
                or getattr(adapter, "_admission_ready", None) is not True
                or self._k11_verifier_token is None
                or verification_token is not self._k11_verifier_token):
            raise MinecraftEACError("K11 observation verifier capability is invalid")

    @staticmethod
    def _validate_k11_actor(actor_id: str) -> None:
        if not isinstance(actor_id, str) or not actor_id or any(
                "\ud800" <= char <= "\udfff" for char in actor_id):
            raise MinecraftEACError("K11 observation requires a valid actor")

    @staticmethod
    def _validate_k11_proposition(proposition: Proposition) -> PropositionKey:
        if not isinstance(proposition, Proposition):
            raise MinecraftEACError("K11 observation requires a typed proposition")
        key = proposition.key
        coordinates = key.arguments
        if (key.namespace != "minecraft" or key.predicate != "target_block_present"
                or key.temporal_scope != "current" or len(coordinates) != 3
                or any(type(value) is not int for value in coordinates)):
            raise MinecraftEACError(
                "K11 observation proposition is outside the passive target scope")
        return key

    def _k11_tracked_root(self, actor_id: str, key: PropositionKey) -> EvidenceRoot | None:
        """Validate and return one actor/key's indexed current K11 root."""
        previous_id = self._k11_current_roots.get((actor_id, key))
        if previous_id is None:
            return None
        previous = self.authority._roots.get(previous_id)
        if (not isinstance(previous, EvidenceRoot) or previous.current is not True
                or previous.root_type != "direct_observation"
                or previous.mapping_rule_id != K11_PASSIVE_RULE_ID
                or previous.issuer != K11_PASSIVE_ISSUER
                or previous.source != K11_PASSIVE_ISSUER
                or previous.source_stream_id != K11_PASSIVE_STREAM_ID
                or previous.proposition.key != key
                or previous.visible_to != (actor_id,)
                or previous.source_lineage_id != self._k11_lineage
                or previous.upstream_origin_id
                != "minecraft-k11-passive-observation:" + previous.root_id
                or previous.provenance_id not in self.authority._provenance
                or type(previous.source_stream_revision) is not int):
            raise MinecraftEACError("tracked K11 passive root is inconsistent")
        previous_provenance = self.authority._provenance[previous.provenance_id]
        if (not isinstance(previous_provenance, ProvenanceRecord)
                or previous_provenance.issuer != K11_PASSIVE_ISSUER
                or previous_provenance.origin != K11_PASSIVE_ISSUER):
            raise MinecraftEACError("tracked K11 passive provenance is inconsistent")
        return previous

    def _check_k11_capacity(self, planned_roots: int) -> None:
        counts = {
            "roots": len(self.authority._roots),
            "derivations": len(self.authority._derivations),
            "provenance": len(self.authority._provenance),
            "planned_roots": planned_roots,
            "planned_derivations": 0,
            "planned_provenance": planned_roots,
            "max_roots": MAX_ROOTS,
            "max_derivations": MAX_DERIVATIONS,
            "max_provenance": MAX_PROVENANCE,
        }
        if (counts["roots"] + counts["planned_roots"] > counts["max_roots"]
                or counts["derivations"] + counts["planned_derivations"]
                > counts["max_derivations"]
                or counts["provenance"] + counts["planned_provenance"]
                > counts["max_provenance"]):
            raise K11CapacityExhausted(counts)

    def _preflight_k11_observations(
            self, actor_id: str, propositions: Iterable[Proposition],
            verification_token: object) -> tuple[bool, ...]:
        """Validate an entire known-cell snapshot without mutating Authority."""
        with self._lock:
            with self.authority._lock:
                self._require_k11_verification_token(verification_token)
                self._validate_k11_actor(actor_id)
                if self._k11_commit_gate is not None:
                    raise MinecraftEACError("K11 observation commit is already active")
                try:
                    snapshot = tuple(propositions)
                except (TypeError, ValueError):
                    raise MinecraftEACError("K11 observation snapshot must be iterable") from None

                transitions: list[bool] = []
                seen: set[PropositionKey] = set()
                planned_roots = 0
                for proposition in snapshot:
                    key = self._validate_k11_proposition(proposition)
                    if key in seen:
                        raise MinecraftEACError(
                            "K11 observation snapshot contains a duplicate proposition")
                    seen.add(key)
                    previous = self._k11_tracked_root(actor_id, key)
                    transition = (previous is None
                                  or previous.proposition.polarity is not proposition.polarity)
                    transitions.append(transition)
                    planned_roots += int(transition)

                self._check_k11_capacity(planned_roots)
                next_sequence = self._sequence
                for transition in transitions:
                    if not transition:
                        continue
                    next_sequence += 1
                    root_id = f"minecraft-k11-passive-root:{self.run_id}:{next_sequence}"
                    provenance_id = "minecraft-k11-passive-provenance:" + root_id
                    if (root_id in self.authority._roots
                            or provenance_id in self.authority._provenance):
                        raise MinecraftEACError(
                            "K11 passive root or provenance identity collision")
                return tuple(transitions)

    def _authenticate_record(self, record, proposition, binding, rule) -> bool:
        if record.get("type") == K11_PASSIVE_RECORD_TYPE:
            gate = self._k11_commit_gate
            if gate is None:
                return False
            token, expected_record, expected_proposition, actor_id, revision = gate
            valid = (
                self.source_version == 2
                and token is not None
                and record is expected_record
                and proposition is expected_proposition
                and binding.profile_id == "minecraft-eac-k11-fixed-passive"
                and binding.profile_version == 2
                and rule["rule_id"] == K11_PASSIVE_RULE_ID
                and record.get("issuer") == K11_PASSIVE_ISSUER
                and record.get("source") == K11_PASSIVE_ISSUER
                and record.get("source_lineage_id") == self._k11_lineage
                and record.get("visible_to") == [actor_id]
                and record.get("source_stream_id") == K11_PASSIVE_STREAM_ID
                and record.get("source_stream_revision") == revision
            )
            if valid:
                self._k11_commit_gate = None
            return valid
        if (self.source_version == 2
                and proposition.key.namespace == "minecraft"
                and proposition.key.predicate == "target_block_present"):
            return False
        return (record.get("issuer") == "minecraft-eac-adapter"
                and binding.profile_id == ("minecraft-eac-primary" if self.source_version == 1
                                           else "minecraft-eac-k11-fixed-passive")
                and proposition.key.namespace == rule["record_namespace"])

    def classification_for(self, tool_name: str) -> Mapping[str, Any]:
        try:
            return self._actions[tool_name]
        except KeyError:
            raise MinecraftEACError(f"unclassified Minecraft tool: {tool_name}") from None

    def supports_tool(self, tool_name: str) -> bool:
        return tool_name in self._actions

    @staticmethod
    def bind_tool_arguments(function, args, kwargs) -> dict[str, Any]:
        if not args and kwargs and any(parameter.kind is inspect.Parameter.VAR_KEYWORD
                                       for parameter in inspect.signature(function).parameters.values()):
            return {key: value for key, value in kwargs.items() if key not in {"emotion", "murmur"}}
        signature = inspect.signature(function)
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        return {key: value for key, value in bound.arguments.items()
                if key not in {"emotion", "murmur"}}

    def _proposition(self, classification, arguments) -> Proposition:
        names = classification["proposition_argument_fields"]
        values = tuple(_minecraft_identifier(arguments[name]) for name in names if name in arguments)
        return Proposition(PropositionKey(
            classification["proposition_namespace"],
            classification["proposition_predicate"], values,
            classification["temporal_scope"],
        ))

    def _definitions(self, classification, proposition):
        action_definition = {
            "action_identity": classification["action_identity"],
            "action_version": classification["action_version"],
            "argument_fields": classification["argument_fields"],
            "effect_gateway_mapping": classification["effect_gateway_mapping"],
            "classification": {
                "identity": self.classification["artifact_id"],
                "version": self.classification["artifact_version"],
                "digest": self.classification_identity,
            },
        }
        action = ActionRef(classification["action_identity"], classification["action_version"],
                           _digest(action_definition))
        declared = (proposition,) if classification["epre"] else ()
        epre_definition = {
            "classification_identity": self.classification_identity,
            "action_identity": classification["action_identity"],
            "propositions": [_plain(item) for item in declared],
        }
        declared_digest = _digest(declared)
        epre = EPreRef("minecraft-epre:" + classification["action_identity"] + ":" + declared_digest,
                       1, declared_digest)
        return action_definition, action, epre_definition, epre, declared

    def mediate_tool(self, tool_name: str, function, args, kwargs):
        """Adapt one registered tool invocation and enter the shared gateway."""
        prepared = self.prepare_tool(tool_name, function, args, kwargs)
        return self.execute_prepared(prepared)

    def prepare_tool(self, tool_name: str, function, args, kwargs) -> MinecraftPreparedAction:
        """Create/evaluate a candidate without crossing the native effect boundary."""
        with self._lock:
            classification = self.classification_for(tool_name)
            arguments = self.bind_tool_arguments(function, args, kwargs)
            actor_id = arguments.pop("player_name", None)
            if not isinstance(actor_id, str) or not actor_id:
                raise MinecraftEACError("classified tool requires player_name")
            proposition = self._proposition(classification, arguments)
            action_definition, action, unused_epre_definition, epre, declared = self._definitions(
                classification, proposition)
            self.authority.register_action_definition(action, action_definition)
            self.authority.register_epre_definition(epre, declared)
            self._sequence += 1
            candidate_id = f"{self.run_id}:{self._sequence}:{tool_name}"
            request = ExactRequest(
                candidate_id, candidate_id + ":attempt", action,
                tuple((key, value) for key, value in arguments.items()),
                target={key: arguments[key] for key in classification["argument_fields"] if key in arguments},
            )
            actor = ActorScope(actor_id, self._sequence, ("minecraft", self.run_id))
            env_preconditions = tuple(
                Proposition(PropositionKey(
                    item["namespace"], item["predicate"],
                    tuple(_minecraft_identifier(arguments[name]) for name in item["argument_fields"]),
                    item["temporal_scope"],
                ))
                for item in classification.get("env_preconditions", ())
            )
            self.authority.register_candidate(
                request, actor=actor, epre_ref=epre, epre=declared,
                env_pre=env_preconditions,
                sec_pre=(classification["action_identity"],) if classification["sec_pre"] else (),
                capability_dependencies=(classification["capability_dependency"],),
            )
            env_pre = self._env_prechecks.get(tool_name)
            if env_pre is None:
                env_pre = lambda unused: self._native_preflight(actor_id, tool_name, arguments)
            sec_pre = self._sec_prechecks.get(tool_name, lambda unused: True)
            frozen_args = tuple(_plain(item) for item in args)
            frozen_kwargs = {key: _plain(value) for key, value in kwargs.items()}
            def native(unused):
                # execute_fenced has admitted the effect immediately before this
                # callback. Persist that state before crossing the HTTP boundary.
                self._persist_audit()
                result = function(*frozen_args, **frozen_kwargs)
                if isinstance(result, Mapping) and result.get("status") is not True:
                    return NativeEffectResult(result, "effect_failed")
                return result
            gateway = EffectGateway(self.authority, native, env_pre=env_pre, sec_pre=sec_pre)
            try:
                if self.mode == "dual_dag_authority":
                    permit = self.authority.issue_permit(candidate_id)
                    self._last_permit[tool_name] = permit
                else:
                    permit = None
            except (AuthorityError, EffectRejected) as exc:
                self._persist_audit()
                raise MinecraftEACError(str(exc)) from exc
            self._persist_audit()
            return MinecraftPreparedAction(tool_name, request, gateway, permit,
                                            tuple((key, _plain(value)) for key, value in arguments.items()))

    def execute_prepared(self, prepared: MinecraftPreparedAction):
        """Execute one previously prepared action through the shared gateway."""
        with self._lock:
            try:
                if self.mode == "dual_dag_authority":
                    result = prepared.gateway.execute(prepared.request, prepared.permit)
                else:
                    result = prepared.gateway.execute_advisory(prepared.request)
            except (AuthorityError, EffectRejected) as exc:
                self._persist_audit()
                raise MinecraftEACError(str(exc)) from exc
            finally:
                self._persist_audit()
            # ExactRequest intentionally has no actor field; recover the canonical candidate scope.
            actor_id = self.authority._candidates[prepared.request.candidate_id].actor.actor_id
            proposition = self.authority._candidates[prepared.request.candidate_id].epre
            if proposition and not (isinstance(result, Mapping) and result.get("status") is not True):
                self._ingest_visible_outcome(actor_id, prepared.tool_name, proposition[0], result)
            self._ingest_result_evidence(actor_id, prepared.tool_name, result,
                                         dict(prepared.arguments))
            self._persist_audit()
            return result

    def ingest_actor_record(self, *, actor_id: str, proposition: Proposition,
                            record_type: str, source: str, payload: Mapping[str, Any] | None = None,
                            visible_to: tuple[str, ...] | None = None,
                            root_id: str | None = None, revision: int | str = 1,
                            supersedes: tuple[str, ...] = ()):
        with self._lock:
            if source in FORBIDDEN_EVIDENCE_ORIGINS:
                raise MinecraftEACError("forbidden evaluator/oracle evidence origin")
            if self.source_version == 2 and source == K11_PASSIVE_ISSUER:
                raise MinecraftEACError("K11 passive evidence requires the authenticated internal commit")
            if record_type not in {"direct_observation", "trusted_tool_result",
                                   "visible_action_outcome", "peer_report"}:
                raise MinecraftEACError("unknown Minecraft evidence record type")
            is_v2_target_fluent = (
                self.source_version == 2
                and proposition.key.namespace == "minecraft"
                and proposition.key.predicate == "target_block_present"
            )
            if is_v2_target_fluent:
                raise MinecraftEACError("v2 target sensing requires authenticated passive evidence")
            visible = tuple(visible_to or (actor_id,))
            if visible != (actor_id,):
                raise MinecraftEACError("evidence must be private to its observing actor")
            current_slot = ((actor_id, proposition.key)
                            if record_type in {"direct_observation", "visible_action_outcome"} else None)
            tracked_current = self._current_roots.get(current_slot) if current_slot else None
            if current_slot is not None:
                if isinstance(revision, bool) or not isinstance(revision, int):
                    raise MinecraftEACError("current-fluent revision must be an integer")
                if tracked_current is None and supersedes:
                    raise MinecraftEACError("supersession requires a tracked current fluent")
                if tracked_current is not None:
                    if supersedes != (tracked_current,):
                        raise MinecraftEACError("new current evidence must supersede the tracked fluent")
                    previous_revision = self.authority._roots[tracked_current].source_stream_revision
                    if previous_revision is None or revision <= previous_revision:
                        raise MinecraftEACError("current-fluent revision must increase monotonically")
            self._sequence += 1
            rid = root_id or f"minecraft-root:{self.run_id}:{self._sequence}"
            provenance_id = "minecraft-prov:" + rid
            self.authority.put_provenance(ProvenanceRecord(provenance_id, source))
            record = {
                "namespace": "minecraft", "type": record_type,
                "visible_to": list(visible), "source_lineage_id": source,
                "upstream_origin_id": source, "issuer": "minecraft-eac-adapter",
                "source": source, "proposition": _authority_plain(proposition),
            }
            if record_type == "trusted_tool_result":
                trusted_tool = (self.profile_document["trusted_tools"][0]
                                if self.source_version == 1 else next(
                                    (item for item in self.profile_document["trusted_tools"]
                                     if (item["tool_identity"], item["tool_version"])
                                     == ("minecraft-observation-adapter", "1")), None))
                if trusted_tool is None:
                    raise MinecraftEACError(
                        "Minecraft observation adapter is not trusted by SourceProfile")
                record.update({
                    "tool_identity": "minecraft-observation-adapter", "tool_version": "1",
                    "integrity_contract_sha256": trusted_tool["integrity_contract_sha256"],
                })
            if payload:
                record["sanitized_payload"] = _plain(dict(payload))
            if record_type in {"direct_observation", "visible_action_outcome"}:
                if not isinstance(revision, int):
                    raise MinecraftEACError("supersession requires a monotonic direct-observation revision")
                stream = ("minecraft-visible-state" if record_type == "direct_observation"
                          else "minecraft-visible-action-state")
                record.update({"source_stream_id": stream,
                               "source_stream_revision": revision})
            elif supersedes:
                raise MinecraftEACError("supersession requires a monotonic direct-observation revision")
            root = self.authority.ingest_record(
                record, proposition=proposition, root_id=rid, revision=revision,
                provenance_id=provenance_id, supersedes=supersedes)
            if current_slot is not None:
                self._current_roots[current_slot] = root.root_id
                self._fluent_revision = max(self._fluent_revision, revision)
            evidence_kind = payload.get("evidence_kind") if isinstance(payload, Mapping) else None
            self._records.append({"root_id": rid, "record_type": evidence_kind or record_type,
                                  "authority_record_type": record_type,
                                  "actor_id": actor_id, "source": source})
            self._evidence_total += 1
            self._persist_audit()
            return root

    def _commit_authenticated_k11_observation(
            self, *, actor_id: str, proposition: Proposition, revision: int,
            provenance: Mapping[str, Any], verification_token: object) -> Any:
        """Commit one verified passive observation with runtime-local stream revision."""
        with self._lock:
            if self.source_version != 2:
                raise MinecraftEACError("K11 passive observations require source_version=2")
            self._require_k11_verification_token(verification_token)
            self._validate_k11_actor(actor_id)
            key = self._validate_k11_proposition(proposition)
            if type(revision) is not int or revision < 0:
                raise MinecraftEACError("K11 capture metadata revision must be a non-negative integer")
            provenance_metadata = _validate_k11_provenance(
                provenance, revision=revision,
                profile_digest=self.profile_binding.digest_sha256,
                ingestion_digest=self.ingestion_contract["detached_artifact_sha256"],
            )
            if self._k11_commit_gate is not None:
                raise MinecraftEACError("K11 observation commit is already active")

            slot = (actor_id, key)
            ingest_sequence = self._sequence + 1
            root_id = f"minecraft-k11-passive-root:{self.run_id}:{ingest_sequence}"
            provenance_id = "minecraft-k11-passive-provenance:" + root_id
            phase = "prevalidate"
            token = object()
            provenance_record = None
            evidence_index_entry = None
            evidence_total_before = self._evidence_total
            with self.authority._lock:
                try:
                    previous = self._k11_tracked_root(actor_id, key)
                    previous_id = previous.root_id if previous is not None else None
                    supersedes: tuple[str, ...] = ()
                    if previous is not None:
                        if previous.proposition.polarity is proposition.polarity:
                            # Semantic repeats do not consume the runtime sequence,
                            # touch provenance, or invalidate dependent permits.
                            return None
                        if ingest_sequence <= previous.source_stream_revision:
                            raise MinecraftEACError(
                                "K11 runtime-local source-stream revision must increase monotonically")
                        supersedes = (previous_id,)

                    self._check_k11_capacity(1)
                    record = {
                        "namespace": "minecraft",
                        "type": K11_PASSIVE_RECORD_TYPE,
                        "visible_to": [actor_id],
                        "source_lineage_id": self._k11_lineage,
                        "upstream_origin_id": "minecraft-k11-passive-observation:" + root_id,
                        "issuer": K11_PASSIVE_ISSUER,
                        "source": K11_PASSIVE_ISSUER,
                        "source_stream_id": K11_PASSIVE_STREAM_ID,
                        "source_stream_revision": ingest_sequence,
                        "proposition": _authority_plain(proposition),
                    }
                    mapping = match_mapping(record, self.profile_binding)
                    streams = [item for item in self.profile_binding.profile["supersession_streams"]
                               if item["source_stream_id"] == K11_PASSIVE_STREAM_ID]
                    if (mapping.get("rule_id") != K11_PASSIVE_RULE_ID
                            or mapping.get("record_namespace") != "minecraft"
                            or mapping.get("record_type") != K11_PASSIVE_RECORD_TYPE
                            or mapping.get("root_type") != "direct_observation"
                            or proposition.key.namespace != mapping.get("record_namespace")
                            or record["visible_to"] != [actor_id]
                            or record["source_lineage_id"] != self._k11_lineage
                            or record["issuer"] != K11_PASSIVE_ISSUER
                            or record["source"] != K11_PASSIVE_ISSUER
                            or len(streams) != 1
                            or streams[0].get("authorized_issuer") != K11_PASSIVE_ISSUER
                            or streams[0].get("revision_field") != "source_stream_revision"
                            or streams[0].get("tracked_proposition_rule_id") != K11_PASSIVE_RULE_ID
                            or record.get("source_stream_id") != K11_PASSIVE_STREAM_ID
                            or record.get("source_stream_revision") != ingest_sequence
                            or supersedes != ((previous_id,) if previous_id is not None else ())):
                        raise MinecraftEACError(
                            "K11 passive record mapping, scope, or stream mismatch")

                    provenance_record = ProvenanceRecord(
                        provenance_id, K11_PASSIVE_ISSUER, issuer=K11_PASSIVE_ISSUER,
                        metadata=provenance_metadata,
                    )
                    evidence_index_entry = {
                        "root_id": root_id,
                        "record_type": K11_PASSIVE_RECORD_TYPE,
                        "authority_record_type": K11_PASSIVE_RECORD_TYPE,
                        "actor_id": actor_id,
                        "source": K11_PASSIVE_ISSUER,
                    }
                    if (root_id in self.authority._roots
                            or provenance_id in self.authority._provenance):
                        raise MinecraftEACError(
                            "K11 passive root or provenance identity collision")

                    # The first mutation follows complete snapshot and single-cell
                    # validation. Root revisions are runtime-local, never capture_seq.
                    self._sequence = ingest_sequence
                    self._k11_commit_gate = (
                        token, record, proposition, actor_id, ingest_sequence)
                    phase = "put_provenance"
                    self.authority.put_provenance(provenance_record)
                    phase = "ingest_record"
                    root = self.authority.ingest_record(
                        record, proposition=proposition, root_id=root_id,
                        revision=ingest_sequence, provenance_id=provenance_id,
                        supersedes=supersedes,
                    )

                    phase = "runtime_bookkeeping"
                    self._k11_current_roots[slot] = root.root_id
                    self._records.append(evidence_index_entry)
                    self._evidence_total += 1
                    phase = "persist_audit"
                    self._persist_audit()
                    self._k11_last_failure = None
                    return root
                except K11CapacityExhausted:
                    # Only the pre-mutation capacity check is an ordinary refusal.
                    if (phase == "prevalidate" and self._sequence < ingest_sequence
                            and root_id not in self.authority._roots
                            and provenance_id not in self.authority._provenance):
                        raise
                    self._diagnose_k11_failure(
                        actor_id, root_id, provenance_id, ingest_sequence, phase,
                        proposition, provenance_record, evidence_index_entry, supersedes,
                        evidence_total_before,
                    )
                    raise
                except BaseException:
                    self._diagnose_k11_failure(
                        actor_id, root_id, provenance_id, ingest_sequence, phase,
                        proposition, provenance_record, evidence_index_entry,
                        supersedes if "supersedes" in locals() else (), evidence_total_before,
                    )
                    raise
                finally:
                    gate = self._k11_commit_gate
                    if gate is not None and gate[0] is token:
                        self._k11_commit_gate = None

    def _diagnose_k11_failure(self, actor_id, root_id, provenance_id, ingest_sequence,
                              phase, proposition, provenance_record, evidence_index_entry,
                              supersedes, evidence_total_before) -> None:
        """Reconcile any insertion before propagating the original commit error."""
        try:
            inserted_root = self.authority._roots.get(root_id)
            inserted_provenance = self.authority._provenance.get(provenance_id)
            root_present = root_id in self.authority._roots
            provenance_present = provenance_id in self.authority._provenance
            root_committed = (
                root_present and provenance_present
                and phase in {"ingest_record", "runtime_bookkeeping", "persist_audit"}
                and isinstance(inserted_root, EvidenceRoot)
                and isinstance(inserted_provenance, ProvenanceRecord)
                and inserted_provenance == provenance_record
                and inserted_root.root_id == root_id
                and inserted_root.provenance_id == provenance_id
                and inserted_root.proposition == proposition
                and inserted_root.root_type == "direct_observation"
                and inserted_root.valid is True and inserted_root.current is True
                and inserted_root.mapping_rule_id == K11_PASSIVE_RULE_ID
                and inserted_root.issuer == K11_PASSIVE_ISSUER
                and inserted_root.source == K11_PASSIVE_ISSUER
                and inserted_root.visible_to == (actor_id,)
                and inserted_root.source_lineage_id == self._k11_lineage
                and inserted_root.upstream_origin_id
                == "minecraft-k11-passive-observation:" + root_id
                and inserted_root.source_stream_id == K11_PASSIVE_STREAM_ID
                and inserted_root.source_stream_revision == ingest_sequence
                and inserted_root.supersedes == supersedes
            )
            if root_committed:
                self._k11_current_roots[(actor_id, proposition.key)] = root_id
                if not any(item.get("root_id") == root_id for item in self._records):
                    self._records.append(evidence_index_entry)
                if self._evidence_total <= evidence_total_before:
                    self._evidence_total += 1
            failure = {
                "actor_id": actor_id,
                "root_id": root_id,
                "provenance_id": provenance_id,
                "ingest_sequence": ingest_sequence,
                "provenance_present": provenance_present,
                "root_present": root_present,
                "orphan_provenance": provenance_present and not root_present,
                "phase": phase,
            }
            if root_committed:
                failure["_root"] = inserted_root
            self._k11_last_failure = failure
        except BaseException:
            # Preserve the original exception even if diagnostic reconciliation fails.
            failure = {
                "actor_id": actor_id, "root_id": root_id,
                "provenance_id": provenance_id,
                "ingest_sequence": ingest_sequence,
                "provenance_present": provenance_id in self.authority._provenance,
                "root_present": root_id in self.authority._roots,
                "orphan_provenance": (provenance_id in self.authority._provenance
                                       and root_id not in self.authority._roots),
                "phase": phase,
            }
            inserted_root = self.authority._roots.get(root_id)
            if isinstance(inserted_root, EvidenceRoot):
                failure["_root"] = inserted_root
            self._k11_last_failure = failure

    def ingest_target_observation(self, actor_id: str, action_name: str,
                                  arguments: Mapping[str, Any], *, revision: int | str = 1):
        classification = self.classification_for(action_name)
        proposition = self._proposition(classification, arguments)
        return self._ingest_current_fluent(actor_id, proposition,
                                           source="minecraft-visible-observation")

    def _ingest_current_fluent(self, actor_id: str, proposition: Proposition, *, source: str,
                               evidence_kind: str = "direct_observation", payload=None,
                               record_type: str = "direct_observation"):
        with self._lock:
            slot = (actor_id, proposition.key)
            self._fluent_revision += 1
            previous = self._current_roots.get(slot)
            root = self.ingest_actor_record(
                actor_id=actor_id, proposition=proposition, record_type=record_type,
                source=source, payload={"evidence_kind": evidence_kind, **(payload or {})},
                revision=self._fluent_revision, supersedes=(previous,) if previous else (),
            )
            self._current_roots[slot] = root.root_id
            return root

    def ingest_initial_actor_state(self, actor_id: str, state: Mapping[str, Any]):
        with self._lock:
            # Actor-visible legacy snapshots are not authenticated passive K11 evidence.
            if self.source_version == 2:
                return ()
            if actor_id in self._initial_state_ingested:
                return ()
            if (not isinstance(state, Mapping) or state.get("status") is not True
                    or not isinstance(state.get("message"), Mapping)
                    or not isinstance(state["message"].get("blocks"), list)):
                return ()
            roots = []
            for block_name, coordinates in sanitized_visible_blocks(state):
                proposition = Proposition(PropositionKey(
                    "minecraft", "target_block_present", tuple(coordinates), "current"))
                roots.append(self._ingest_current_fluent(
                    actor_id, proposition, source="minecraft-initial-visible-state",
                    evidence_kind="initial_visible_block", payload={"block_name": block_name},
                ))
            self._initial_state_ingested.add(actor_id)
            return tuple(roots)

    def ingest_peer_report(self, actor_id: str, proposition: Proposition, sender: str):
        return self.ingest_actor_record(
            actor_id=actor_id, proposition=proposition, record_type="peer_report",
            source="minecraft-peer:" + sender)

    def _ingest_visible_outcome(self, actor_id, tool_name, proposition, result) -> None:
        if tool_name != "MineBlock":
            return
        if self.source_version == 1:
            self._ingest_current_fluent(
                actor_id, replace(proposition, polarity=False), source="minecraft-action:MineBlock",
                evidence_kind="action_derived_direct_observation",
                payload={"status": result.get("status") if isinstance(result, Mapping) else None})
        outcome = Proposition(PropositionKey(
            proposition.key.namespace, "mineblock_success_observed",
            proposition.key.arguments, proposition.key.temporal_scope))
        self._ingest_current_fluent(
            actor_id, outcome, source="minecraft-action:MineBlock",
            evidence_kind="visible_action_outcome", record_type="visible_action_outcome",
            payload={"status": result.get("status") if isinstance(result, Mapping) else None})

    def _ingest_result_evidence(self, actor_id: str, tool_name: str, result: Any,
                                request_arguments: Mapping[str, Any] | None = None) -> None:
        """Convert sanitized observation/message results at event time."""
        if not isinstance(result, Mapping) or result.get("status") is not True:
            return
        if tool_name == "scanNearbyEntities":
            rows = sanitized_scan_rows(result, request_arguments)
            for index, (name, position) in enumerate(rows):
                proposition = Proposition(PropositionKey(
                    "minecraft", "entity_observed", (_minecraft_identifier(name), position), "current"))
                self.ingest_actor_record(
                    actor_id=actor_id, proposition=proposition,
                    record_type="trusted_tool_result", source="minecraft-observation-adapter",
                    root_id=f"minecraft-scan:{self.run_id}:{self._sequence}:{index}")
        elif tool_name in {"talkTo", "waitForFeedback"}:
            events = result.get("new_events", ())
            if not isinstance(events, (list, tuple)):
                return
            for index, event in enumerate(events[:128]):
                proposition = Proposition(PropositionKey(
                    "minecraft", "peer_message_received", (_plain(event),), "current"))
                self.ingest_peer_report(actor_id, proposition, "peer")
        if tool_name == "scanNearbyEntities":
            for index, (name, position) in enumerate(sanitized_scan_rows(result, request_arguments)):
                observed_values = []
                if isinstance(position, (list, tuple)) and len(position) == 3:
                    coordinates = tuple(position)
                    observed_values.extend((("placement_target_observed", coordinates),
                                            ("destination_observed", coordinates)))
                if name:
                    normalized = (_minecraft_identifier(name),)
                    observed_values.extend((("entity_target_observed", normalized),
                                            ("recipient_observed", normalized)))
                for suffix, (predicate, values) in enumerate(observed_values):
                    proposition = Proposition(PropositionKey(
                        "minecraft", predicate, values, "current"))
                    self.ingest_actor_record(
                        actor_id=actor_id, proposition=proposition,
                        record_type="trusted_tool_result", source="minecraft-observation-adapter",
                        root_id=f"minecraft-target:{self.run_id}:{self._sequence}:{index}:{suffix}")

    def audit_artifact(self) -> dict[str, Any]:
        sequence = self.authority._sequence
        authority_audit = self.authority.audit_snapshot(
            limit=256, after_sequence=max(0, sequence - 256))
        attempts = self.authority.attempt_snapshot()
        return {
            "schema_version": "minecraft-eac-audit/1",
            "runtime_identity": self.runtime_identity,
            "execution_identity": ({
                "execution_revision": self.identity_binding["execution_revision"],
                "runtime_digest": self.identity_binding["runtime_digest"],
                "premanifest_identity": self.identity_binding["premanifest_identity"],
            } if self.identity_binding is not None else None),
            "mode": self.mode,
            "support_policy": _plain(self.authority.policy),
            "source_profile": _plain(self.authority.profile),
            "classification": {
                "identity": self.classification["artifact_id"],
                "version": self.classification["artifact_version"],
                "digest": self.classification_identity,
            },
            "authority_audit": [_plain(item) for item in authority_audit],
            "audit_sequence": sequence,
            "audit_truncated": sequence > len(authority_audit),
            "attempts": [_plain(item) for item in attempts[-256:]],
            "attempts_truncated": len(attempts) > 256,
            "evidence_index": tuple(self._records),
            "evidence_total": self._evidence_total,
            "evidence_truncated": self._evidence_total > len(self._records),
            "read_only_projection": True,
            "oracle_state_included": False,
            "bounded": True,
            "audit_limit": 256,
        }

    def _persist_audit(self) -> None:
        if self._audit_path is not None:
            atomic_write_json(self._audit_path, self.audit_artifact())

    @staticmethod
    def _native_preflight(actor_id: str, tool_name: str, arguments: Mapping[str, Any]) -> bool:
        """Ask the trusted bridge for a read-only effect-time legality decision."""
        from env.minecraft_client import Agent, _minecraft_request
        response = _minecraft_request(
            "POST", Agent.get_agent_url(actor_id) + "/post_eac_preflight",
            data=json.dumps({"action": tool_name, "arguments": _plain(dict(arguments))}),
            headers=Agent.headers,
        )
        payload = response.json()
        return isinstance(payload, Mapping) and payload.get("status") is True


def install_minecraft_eac(environment, *, mode: str, run_id: str,
                          env_prechecks=None, sec_prechecks=None,
                          identity_binding=None, source_version: int = 1) -> MinecraftEACRuntime:
    runtime = MinecraftEACRuntime(
        mode=mode, run_id=run_id, env_prechecks=env_prechecks, sec_prechecks=sec_prechecks,
        identity_binding=identity_binding, source_version=source_version,
        audit_path=environment.runtime_paths.data_dir / "minecraft_eac_audit.json")
    environment.configure_eac_runtime(runtime)
    return runtime
