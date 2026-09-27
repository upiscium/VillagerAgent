"""Parent-owned K12 authority, provenance, and first-consume contracts.

The module is intentionally non-circular.  It consumes typed observations and
protocol-shaped qualification evidence; it does not import the later
qualification aggregate implementation and it never talks to Git, GitHub,
Minecraft, RCON, providers, systemd, or a process recorder.

Operational namespaces/provenance remain ``live_qualification``,
``qualification_probe``, and ``live_final``.  Authorization/evidence origin is
separate: runtime chains use ``runtime_verified`` and deterministic chains use
``injected_test``/``injected_fake``.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from benchmarks.common.eac.canonical import canonical_bytes, canonical_sha256
from .k12_authority_contracts import (
    QualificationEvidenceContract,
)
from .k12_qualification_semantics import (
    NormalizedCell,
    NormalizedProbe,
    NormalizedRejectionBinding,
    NormalizedTerminal,
    QualificationCensus,
    QualificationVerdict,
    QUALIFICATION_PROBE_IDENTITY,
    QUALIFICATION_SCHEDULE,
    QUALIFICATION_SCHEDULE_DIGEST,
    QUALIFICATION_SCHEDULE_IDENTITY,
    PROBES as QUALIFICATION_PROBES,
    PROBE_SCHEDULE_DIGEST,
    SEMANTIC_VERIFIER_IDENTITY,
    verify_qualification,
    verify_qualification_projection,
)


QUALIFICATION_AUTHORITY = "minecraft-k12-live-qualification-execution-authority/1"
FINAL_AUTHORITY = "minecraft-k12-live-final-execution-authority/1"
QUALIFICATION_RUN_AUTHORIZATION = "minecraft-k12-live-qualification-run-authorization/1"
FINAL_RUN_AUTHORIZATION = "minecraft-k12-live-final-run-authorization/1"
PROFILE_V2 = "minecraft-k12-live-runtime-profile/2"
RUNTIME_VERIFIED_ORIGIN = "runtime_verified"
INJECTED_TEST_ORIGIN = "injected_test"
INJECTED_FAKE_ORIGIN = "injected_fake"
LIVE_QUALIFICATION_NAMESPACE = "live_qualification"
QUALIFICATION_PROBE_NAMESPACE = "qualification_probe"
LIVE_FINAL_NAMESPACE = "live_final"
AUTHORIZATION_ORIGINS = frozenset({
    RUNTIME_VERIFIED_ORIGIN, INJECTED_TEST_ORIGIN, INJECTED_FAKE_ORIGIN,
})
OPERATIONAL_PROVENANCES = frozenset({
    "mock_only", LIVE_QUALIFICATION_NAMESPACE, QUALIFICATION_PROBE_NAMESPACE,
    LIVE_FINAL_NAMESPACE,
})
MAX_PR_AGE_SECONDS = 300

_SHA1 = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CANONICAL_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_AUTHORITY_TOKEN = object()
_BINDING_TOKEN = object()
_RUN_AUTH_TOKEN = object()
_ACTIVE_TOKEN = object()
_LEASE_TOKEN = object()
_INJECTED_CONTROLLER_TOKEN = object()
_SOURCE_CLOSURE_TOKEN = object()
_REVISION_AUTH_TOKEN = object()
_MISSING_TREE_ENTRY = object()

_LEDGER_ROOT_ARTIFACT = "minecraft-k12-live-execution-ledger-root/1"

SOURCE_SEMANTIC_CLASSES = frozenset({
    "python", "config", "contract", "documentation", "dependency", "javascript",
})


class ProvenanceError(ValueError):
    """A provenance observation or authority violates the frozen contract."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class MismatchClass:
    PRELAUNCH_STOP = "prelaunch_stop"
    TARGET_QUARANTINE = "target_quarantine"
    DIAGNOSTIC_ONLY = "diagnostic_only"


MISMATCH_CLASSIFICATION = MappingProxyType({
    "git_repository_mismatch": MismatchClass.PRELAUNCH_STOP,
    "git_worktree_mismatch": MismatchClass.PRELAUNCH_STOP,
    "git_environment_override": MismatchClass.PRELAUNCH_STOP,
    "git_head_mismatch": MismatchClass.PRELAUNCH_STOP,
    "git_tree_mismatch": MismatchClass.PRELAUNCH_STOP,
    "git_dirty_staged": MismatchClass.PRELAUNCH_STOP,
    "git_dirty_tracked": MismatchClass.PRELAUNCH_STOP,
    "git_dirty_untracked": MismatchClass.PRELAUNCH_STOP,
    "git_remote_mismatch": MismatchClass.PRELAUNCH_STOP,
    "git_upstream_mismatch": MismatchClass.PRELAUNCH_STOP,
    "git_submodule_mismatch": MismatchClass.PRELAUNCH_STOP,
    "pr_observation_missing": MismatchClass.PRELAUNCH_STOP,
    "pr_observation_stale": MismatchClass.PRELAUNCH_STOP,
    "pr_semantic_mismatch": MismatchClass.PRELAUNCH_STOP,
    "source_closure_incomplete": MismatchClass.PRELAUNCH_STOP,
    "source_content_mismatch": MismatchClass.PRELAUNCH_STOP,
    "source_mode_mismatch": MismatchClass.PRELAUNCH_STOP,
    "source_semantic_class_mismatch": MismatchClass.PRELAUNCH_STOP,
    "profile_mismatch": MismatchClass.PRELAUNCH_STOP,
    "contract_mismatch": MismatchClass.PRELAUNCH_STOP,
    "environment_mismatch": MismatchClass.PRELAUNCH_STOP,
    "capsule_mismatch": MismatchClass.PRELAUNCH_STOP,
    "authority_namespace_mismatch": MismatchClass.PRELAUNCH_STOP,
    "authority_digest_mismatch": MismatchClass.PRELAUNCH_STOP,
    "authority_capability_mismatch": MismatchClass.PRELAUNCH_STOP,
    "authority_origin_mismatch": MismatchClass.PRELAUNCH_STOP,
    "parent_controller_required": MismatchClass.PRELAUNCH_STOP,
    "ledger_corrupt": MismatchClass.PRELAUNCH_STOP,
    "final_prerequisite_mismatch": MismatchClass.PRELAUNCH_STOP,
    "fresh_root_violation": MismatchClass.PRELAUNCH_STOP,
    "authority_replay": MismatchClass.PRELAUNCH_STOP,
    "first_consume_mismatch": MismatchClass.TARGET_QUARANTINE,
    "target_lock_busy": MismatchClass.PRELAUNCH_STOP,
    "target_lock_loss": MismatchClass.TARGET_QUARANTINE,
    "target_identity_mismatch": MismatchClass.TARGET_QUARANTINE,
    "containment_unknown": MismatchClass.TARGET_QUARANTINE,
    "host_inventory_drift": MismatchClass.DIAGNOSTIC_ONLY,
})


def raw_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_sha1(value: str, reason: str) -> str:
    if not isinstance(value, str) or _SHA1.fullmatch(value) is None:
        raise ProvenanceError(reason)
    return value


def _require_sha256(value: str, reason: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ProvenanceError(reason)
    return value


def _require_canonical_digest(value: str, reason: str) -> str:
    if not isinstance(value, str) or _CANONICAL_SHA256.fullmatch(value) is None:
        raise ProvenanceError(reason)
    return value


def _require_identity(value: str, reason: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ProvenanceError(reason)
    return value


def _normalize_origin(value: Any, reason: str = "authority_origin_mismatch") -> str:
    """Accept only explicit authorization origins, never namespace aliases."""
    if not isinstance(value, str):
        raise ProvenanceError(reason)
    if value not in AUTHORIZATION_ORIGINS:
        raise ProvenanceError(reason)
    return value


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return value


def _normalize_source_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ProvenanceError("source_closure_incomplete")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ProvenanceError("source_closure_incomplete")
    normalized = path.as_posix()
    if normalized != value:
        raise ProvenanceError("source_closure_incomplete")
    return normalized


@dataclass(frozen=True, slots=True)
class SourceRecord:
    path: str
    git_mode: str
    git_blob_oid: str
    sha256: str
    semantic_class: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _normalize_source_path(self.path))
        if type(self.git_mode) is not str or self.git_mode not in {"100644", "100755"}:
            raise ProvenanceError("source_mode_mismatch")
        if type(self.semantic_class) is not str or self.semantic_class not in SOURCE_SEMANTIC_CLASSES:
            raise ProvenanceError("source_semantic_class_mismatch")
        _require_sha1(self.git_blob_oid, "source_content_mismatch")
        _require_sha256(self.sha256, "source_content_mismatch")

    def canonical(self) -> dict[str, Any]:
        return {"path": self.path, "git_mode": self.git_mode,
                "git_blob_oid": self.git_blob_oid, "sha256": self.sha256,
                "semantic_class": self.semantic_class}


@dataclass(frozen=True, slots=True, init=False)
class SourceClosure:
    policy_identity: str
    policy_digest: str
    records: tuple[SourceRecord, ...]
    head_commit: str
    head_tree: str
    aggregate_sha256: str = field(init=False)
    _collector_marker: object = field(repr=False, compare=False)
    _origin: str = field(repr=False, compare=False)
    _collector_owner: Any = field(repr=False, compare=False)

    def __init__(self, policy_identity: str, policy_digest: str,
                  records: Sequence[SourceRecord], token: object = None,
                  marker: object = None, origin: str = RUNTIME_VERIFIED_ORIGIN,
                  owner: Any = None, head_commit: str = "", head_tree: str = "") -> None:
        if token is not _SOURCE_CLOSURE_TOKEN or marker is None or owner is None:
            raise TypeError("source closures are parent-collected")
        _require_identity(policy_identity, "source_closure_incomplete")
        _require_sha256(policy_digest, "source_closure_incomplete")
        if not isinstance(records, Sequence) or any(
                not isinstance(item, SourceRecord) for item in records):
            raise ProvenanceError("source_closure_incomplete")
        try:
            normalized_origin = _normalize_origin(origin)
        except ProvenanceError as exc:
            raise ProvenanceError("source_closure_incomplete") from exc
        if normalized_origin not in {RUNTIME_VERIFIED_ORIGIN, INJECTED_TEST_ORIGIN}:
            raise ProvenanceError("source_closure_incomplete")
        if normalized_origin == RUNTIME_VERIFIED_ORIGIN:
            _require_sha1(head_commit, "git_head_mismatch")
            _require_sha1(head_tree, "git_tree_mismatch")
        elif head_commit or head_tree:
            # An injected closure is never runtime-admissible, but a
            # parent-owned revision authorization may still carry the
            # authenticated ancestry used to bind a deterministic fixture.
            _require_sha1(head_commit, "git_head_mismatch")
            _require_sha1(head_tree, "git_tree_mismatch")
        ordered = tuple(sorted(tuple(records), key=lambda item: item.path))
        names = tuple(item.path for item in ordered)
        if (not ordered or len(names) != len(set(names))
                or len(names) != len({name.casefold() for name in names})):
            raise ProvenanceError("source_closure_incomplete")
        object.__setattr__(self, "policy_identity", policy_identity)
        object.__setattr__(self, "policy_digest", policy_digest)
        object.__setattr__(self, "records", ordered)
        object.__setattr__(self, "head_commit", head_commit)
        object.__setattr__(self, "head_tree", head_tree)
        object.__setattr__(self, "aggregate_sha256",
                           raw_sha256(canonical_bytes({
                               "head_commit": head_commit,
                               "head_tree": head_tree,
                               "records": [item.canonical() for item in ordered],
                           })))
        object.__setattr__(self, "_collector_marker", marker)
        object.__setattr__(self, "_origin", normalized_origin)
        object.__setattr__(self, "_collector_owner", owner)

    @property
    def origin(self) -> str:
        return self._origin

    @property
    def evidence_origin(self) -> str:
        return self._origin

    @property
    def runtime_admissible(self) -> bool:
        return self._origin == RUNTIME_VERIFIED_ORIGIN

    def owned_by(self, owner: Any) -> bool:
        return self._collector_owner is owner and bool(
            getattr(owner, "owns_source_closure", lambda _closure: False)(self)
        )

    def _owned_by(self, marker: object) -> bool:
        """Compatibility view for older in-module callers."""
        return self._collector_marker is marker

    def canonical(self) -> dict[str, Any]:
        return {"policy_identity": self.policy_identity,
                "policy_digest": self.policy_digest,
                "head_commit": self.head_commit,
                "head_tree": self.head_tree,
                "records": [item.canonical() for item in self.records],
                "aggregate_sha256": self.aggregate_sha256}


def source_closure_from_observations(
    *, policy: Any, observations: Mapping[str, Mapping[str, Any]],
) -> SourceClosure:
    """Reject the removed caller-supplied source DTO path.

    Source closure values are authority inputs.  Accepting strings supplied in
    an observation mapping would let a caller mint a closure without opening
    the corresponding files.  The parent-side ``collect_source_closure`` API
    is the only constructor path now.
    """
    del policy, observations
    raise TypeError("parent-side source collector required")


def git_blob_oid(data: bytes) -> str:
    """Return the SHA-1 OID Git assigns to a blob containing ``data``."""
    if type(data) is not bytes:
        raise TypeError("Git blob data must be bytes")
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data, usedforsecurity=False).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class ExternalRevisionAuthorization:
    """Parent-owned authorization for one externally verified revision.

    Revision values are intentionally not module constants.  A parent mints
    this tuple from the checkout/PR observation authenticated for the current
    run and embeds the resulting receipt in the qualification run
    authorization.  The ownership token and object identity prevent a raw
    caller mapping, SHA, or detached receipt from becoming authority.
    """

    repository: str
    branch: str
    head_commit: str
    head_tree: str
    pull_request: int
    pull_request_semantic_tuple: tuple[Any, ...]
    base_ref: str
    base_sha: str
    issued_at: int
    expires_at: int
    origin: str
    verifier_identity: str
    pull_request_observed_at: int
    pull_request_observer: str
    pull_request_receipt_digest: str
    verifier_receipt_digest: str
    detached_artifact_sha256: str
    identity: str
    ownership_token: object = field(repr=False, compare=False)
    owner: Any = field(repr=False, compare=False)

    def __init__(
        self,
        *,
        repository: str,
        branch: str,
        head_commit: str,
        head_tree: str,
        pull_request: int,
        pull_request_semantic_tuple: Sequence[Any],
        base_ref: str,
        base_sha: str,
        issued_at: int,
        expires_at: int,
        origin: str,
        verifier_identity: str,
        pull_request_observed_at: int,
        pull_request_observer: str,
        pull_request_receipt_digest: str,
        verifier_receipt_digest: str,
        ownership_token: object = None,
        owner: Any = None,
        token: object = None,
    ) -> None:
        if token is not _REVISION_AUTH_TOKEN or ownership_token is None or owner is None:
            raise TypeError("external revision authorizations are parent-minted")
        if (not isinstance(repository, str) or not repository
                or not isinstance(branch, str) or not branch
                or not branch.startswith("refs/heads/")
                or not isinstance(base_ref, str) or not base_ref
                or not isinstance(verifier_identity, str) or not verifier_identity
                or not isinstance(pull_request_observer, str)
                or not pull_request_observer):
            raise ProvenanceError("pr_semantic_mismatch")
        _require_sha1(head_commit, "git_head_mismatch")
        _require_sha1(head_tree, "git_tree_mismatch")
        _require_sha1(base_sha, "pr_semantic_mismatch")
        _require_sha256(pull_request_receipt_digest, "pr_observation_missing")
        _require_sha256(verifier_receipt_digest, "pr_observation_missing")
        if type(pull_request) is not int or pull_request <= 0:
            raise ProvenanceError("pr_semantic_mismatch")
        semantic = tuple(pull_request_semantic_tuple)
        if len(semantic) != 9:
            raise ProvenanceError("pr_semantic_mismatch")
        if (semantic[0] != repository or semantic[1] != pull_request
                or semantic[4] != repository
                or semantic[5] != branch.removeprefix("refs/heads/")
                or semantic[6] != head_commit
                or semantic[7] != base_ref or semantic[8] != base_sha):
            raise ProvenanceError("pr_semantic_mismatch")
        if (type(issued_at) is not int or type(expires_at) is not int
                or type(pull_request_observed_at) is not int):
            raise ProvenanceError("pr_observation_stale")
        if issued_at > expires_at or expires_at - issued_at > MAX_PR_AGE_SECONDS:
            raise ProvenanceError("pr_observation_stale")
        normalized_origin = _normalize_origin(origin)
        canonical = {
            "artifact_id": "minecraft-k12-live-external-revision-authorization",
            "artifact_version": 1,
            "repository": repository,
            "branch": branch,
            "head_commit": head_commit,
            "head_tree": head_tree,
            "pull_request": pull_request,
            "pull_request_semantic_tuple": list(semantic),
            "base_ref": base_ref,
            "base_sha": base_sha,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "origin": normalized_origin,
            "verifier_identity": verifier_identity,
            "pull_request_observed_at": pull_request_observed_at,
            "pull_request_observer": pull_request_observer,
            "pull_request_receipt_digest": pull_request_receipt_digest,
            "verifier_receipt_digest": verifier_receipt_digest,
        }
        detached = raw_sha256(canonical_bytes(canonical))
        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "branch", branch)
        object.__setattr__(self, "head_commit", head_commit)
        object.__setattr__(self, "head_tree", head_tree)
        object.__setattr__(self, "pull_request", pull_request)
        object.__setattr__(self, "pull_request_semantic_tuple", semantic)
        object.__setattr__(self, "base_ref", base_ref)
        object.__setattr__(self, "base_sha", base_sha)
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "origin", normalized_origin)
        object.__setattr__(self, "verifier_identity", verifier_identity)
        object.__setattr__(self, "pull_request_observed_at", pull_request_observed_at)
        object.__setattr__(self, "pull_request_observer", pull_request_observer)
        object.__setattr__(self, "pull_request_receipt_digest", pull_request_receipt_digest)
        object.__setattr__(self, "verifier_receipt_digest", verifier_receipt_digest)
        object.__setattr__(self, "detached_artifact_sha256", detached)
        object.__setattr__(
            self,
            "identity",
            canonical_sha256({
                "schema": "minecraft-k12-live-external-revision-authorization/1",
                "digest": detached,
            }),
        )
        object.__setattr__(self, "ownership_token", ownership_token)
        object.__setattr__(self, "owner", owner)

    @property
    def repository_identity(self) -> str:
        return self.repository

    @property
    def branch_ref(self) -> str:
        return self.branch

    @property
    def pr(self) -> int:
        return self.pull_request

    @property
    def pr_number(self) -> int:
        return self.pull_request

    @property
    def fresh_semantic_tuple(self) -> tuple[Any, ...]:
        return self.pull_request_semantic_tuple

    @property
    def semantic_tuple(self) -> tuple[Any, ...]:
        return self.pull_request_semantic_tuple

    @property
    def runtime_admissible(self) -> bool:
        return self.origin == RUNTIME_VERIFIED_ORIGIN

    @property
    def evidence_origin(self) -> str:
        return self.origin

    def owned_by(self, owner: Any) -> bool:
        return self.owner is owner and bool(
            getattr(owner, "owns_external_revision", lambda _value: False)(self)
        )

    def current_at(self, now: int) -> bool:
        return type(now) is int and self.issued_at <= now <= self.expires_at

    def canonical(self) -> dict[str, Any]:
        return {
            "artifact_id": "minecraft-k12-live-external-revision-authorization",
            "artifact_version": 1,
            "repository": self.repository,
            "branch": self.branch,
            "head_commit": self.head_commit,
            "head_tree": self.head_tree,
            "pull_request": self.pull_request,
            "pull_request_semantic_tuple": list(self.pull_request_semantic_tuple),
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "origin": self.origin,
            "verifier_identity": self.verifier_identity,
            "pull_request_observed_at": self.pull_request_observed_at,
            "pull_request_observer": self.pull_request_observer,
            "pull_request_receipt_digest": self.pull_request_receipt_digest,
            "verifier_receipt_digest": self.verifier_receipt_digest,
        }

    def receipt(self) -> dict[str, Any]:
        return {
            **self.canonical(),
            "detached_artifact_sha256": self.detached_artifact_sha256,
            "authorization_digest": self.identity,
        }

    def matches_checkout(self, checkout: "CheckoutObservation") -> bool:
        return (
            isinstance(checkout, CheckoutObservation)
            and checkout.repository_identity == self.repository
            and checkout.symbolic_head_ref == self.branch
            and checkout.head_commit == self.head_commit
            and checkout.head_tree == self.head_tree
            and checkout.index_tree == self.head_tree
            and checkout.upstream_ref == self.branch
            and checkout.upstream_commit == self.head_commit
            and checkout.remote_repository == self.repository
            and checkout.remote_ref == self.branch
            and checkout.remote_commit == self.head_commit
        )

    def matches_pull_request(
        self, observation: "PullRequestObservation", *, now: int
    ) -> bool:
        return (
            isinstance(observation, PullRequestObservation)
            and observation.semantic_tuple() == self.pull_request_semantic_tuple
            and observation.repository == self.repository
            and observation.number == self.pull_request
            and observation.head_sha == self.head_commit
            and observation.base_ref == self.base_ref
            and observation.base_sha == self.base_sha
            and observation.observed_at == self.pull_request_observed_at
            and observation.observer == self.pull_request_observer
            and observation.receipt_digest == self.pull_request_receipt_digest
            and type(observation.observed_at) is int
            and observation.observed_at <= now
            and now - observation.observed_at <= MAX_PR_AGE_SECONDS
            and self.current_at(now)
        )

    def matches_source(self, source: "SourceClosure") -> bool:
        if not isinstance(source, SourceClosure):
            return False
        return source.head_commit == self.head_commit and source.head_tree == self.head_tree


def external_revision_attestation_payload(
    checkout: "CheckoutObservation",
    pull_request: "PullRequestObservation",
    *,
    expires_at: int,
    origin: str,
    verifier_identity: str,
) -> bytes:
    """Canonical evidence bytes signed by the external revision verifier."""

    if not isinstance(checkout, CheckoutObservation):
        raise TypeError("typed checkout observation required")
    if not isinstance(pull_request, PullRequestObservation):
        raise TypeError("typed pull-request observation required")
    return canonical_bytes({
        "schema": "minecraft-k12-live-external-revision-attestation/1",
        "checkout": checkout.canonical(),
        "pull_request": pull_request.canonical(),
        "expires_at": expires_at,
        "origin": _normalize_origin(origin),
        "verifier_identity": verifier_identity,
    })


def _open_trusted_source_root(root: str | Path) -> int:
    try:
        supplied = Path(root)
    except (TypeError, ValueError) as exc:
        raise ProvenanceError("source_closure_incomplete") from exc
    try:
        absolute = Path(os.path.abspath(os.fspath(supplied)))
        current = Path(absolute.anchor)
        for component in absolute.parts[1:]:
            current /= component
            if current.is_symlink():
                raise ProvenanceError("source_closure_incomplete")
    except ProvenanceError:
        raise
    except (OSError, ValueError) as exc:
        raise ProvenanceError("source_closure_incomplete") from exc
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    fd: int | None = None
    try:
        fd = os.open(os.fspath(supplied), flags)
        metadata = os.fstat(fd)
    except (OSError, ValueError) as exc:
        if fd is not None:
            os.close(fd)
        raise ProvenanceError("source_closure_incomplete") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(fd)
        raise ProvenanceError("source_closure_incomplete")
    return fd


def _open_source_path(root_fd: int, path: str) -> int:
    """Open one policy path below ``root_fd`` without following symlinks."""
    parts = PurePosixPath(path).parts
    if not parts:
        raise ProvenanceError("source_closure_incomplete")
    current = os.dup(root_fd)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, directory_flags, dir_fd=current)
            except (OSError, ValueError) as exc:
                raise ProvenanceError("source_closure_incomplete") from exc
            os.close(current)
            current = child
        try:
            fd = os.open(parts[-1], file_flags, dir_fd=current)
        except (OSError, ValueError) as exc:
            raise ProvenanceError("source_closure_incomplete") from exc
        try:
            metadata = os.fstat(fd)
        except (OSError, ValueError) as exc:
            os.close(fd)
            raise ProvenanceError("source_closure_incomplete") from exc
        if not stat.S_ISREG(metadata.st_mode):
            os.close(fd)
            raise ProvenanceError("source_closure_incomplete")
        return fd
    finally:
        os.close(current)


def _read_source_fd(fd: int) -> tuple[bytes, os.stat_result]:
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ProvenanceError("source_closure_incomplete")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
    except ProvenanceError:
        raise
    except (OSError, ValueError) as exc:
        raise ProvenanceError("source_content_mismatch") from exc
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise ProvenanceError("source_content_mismatch")
    return b"".join(chunks), after


def _expected_tree_entry(value: Any) -> tuple[str, str]:
    if isinstance(value, Mapping):
        mode = value.get("git_mode", value.get("mode"))
        blob = None
        for name in ("git_blob_oid", "blob_oid", "oid", "object_id", "sha1", "blob"):
            if name in value:
                blob = value[name]
                break
        if value.get("type", "blob") != "blob":
            raise ProvenanceError("git_tree_mismatch")
    elif isinstance(value, (tuple, list)) and len(value) == 2:
        mode, blob = value
    else:
        mode = getattr(value, "git_mode", getattr(value, "mode", None))
        blob = getattr(value, "git_blob_oid",
                       getattr(value, "blob_oid",
                               getattr(value, "oid", getattr(value, "object_id", None))))
    if mode not in {"100644", "100755"}:
        raise ProvenanceError("git_tree_mismatch")
    try:
        _require_sha1(blob, "source_content_mismatch")
    except ProvenanceError as exc:
        raise ProvenanceError("git_tree_mismatch") from exc
    return mode, blob


def _lookup_expected_tree_entry(expected_tree: Any, path: str) -> Any:
    if expected_tree is None:
        return _MISSING_TREE_ENTRY
    if isinstance(expected_tree, Mapping):
        entries = expected_tree
        nested = expected_tree.get("entries")
        if not isinstance(nested, Mapping):
            nested = expected_tree.get("tree")
        if isinstance(nested, Mapping):
            entries = nested
        if path not in entries or entries[path] is None:
            return _MISSING_TREE_ENTRY
        return _expected_tree_entry(entries[path])
    for method_name in ("entry_for", "get_entry", "lookup"):
        method = getattr(expected_tree, method_name, None)
        if callable(method):
            try:
                value = method(path)
            except KeyError:
                return _MISSING_TREE_ENTRY
            if value is None:
                return _MISSING_TREE_ENTRY
            return _expected_tree_entry(value)
    entries = getattr(expected_tree, "entries", None)
    if isinstance(entries, Mapping):
        if path not in entries or entries[path] is None:
            return _MISSING_TREE_ENTRY
        return _expected_tree_entry(entries[path])
    if callable(expected_tree):
        try:
            value = expected_tree(path)
        except KeyError:
            return _MISSING_TREE_ENTRY
        if value is None:
            return _MISSING_TREE_ENTRY
        return _expected_tree_entry(value)
    try:
        candidates = tuple(expected_tree)
    except TypeError as exc:
        raise ProvenanceError("git_tree_mismatch") from exc
    found: Any = _MISSING_TREE_ENTRY
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            candidate_path = candidate.get("path")
        else:
            candidate_path = getattr(candidate, "path", None)
        if candidate_path == path:
            if found is not _MISSING_TREE_ENTRY:
                raise ProvenanceError("git_tree_mismatch")
            found = _expected_tree_entry(candidate)
    return found


def _git_mode_from_fd(metadata: os.stat_result) -> str:
    return "100755" if stat.S_IMODE(metadata.st_mode) & 0o111 else "100644"


def _authenticated_git_tree(root: str | Path, checkout: Any, policy: Any) -> Mapping[str, Any]:
    """Read exact blob identities from the checkout's authenticated commit tree."""
    if not isinstance(checkout, CheckoutObservation):
        raise TypeError("typed checkout observation required for runtime source collection")
    checkout.validate_structure()
    candidate = shutil.which("git")
    if not candidate:
        raise ProvenanceError("git_tree_mismatch")
    try:
        git_executable = Path(candidate).resolve(strict=True)
        executable_stat = git_executable.stat()
        if (not stat.S_ISREG(executable_stat.st_mode)
                or executable_stat.st_uid != 0
                or executable_stat.st_mode & 0o022
                or os.access(git_executable, os.W_OK)):
            raise ProvenanceError("git_tree_mismatch")
        for parent in git_executable.parents:
            if os.access(parent, os.W_OK):
                raise ProvenanceError("git_tree_mismatch")
    except (OSError, ValueError) as exc:
        raise ProvenanceError("git_tree_mismatch") from exc
    env = {
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }

    def run(*arguments: str) -> bytes:
        try:
            return subprocess.run(
                (os.fspath(git_executable), "-c", "core.fsmonitor=false", "-C", os.fspath(root), *arguments),
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, timeout=30,
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProvenanceError("git_tree_mismatch") from exc

    try:
        trusted_root = Path(root).resolve(strict=True)
        observed_root = Path(run("rev-parse", "--show-toplevel").decode("utf-8").strip()).resolve(strict=True)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ProvenanceError("git_tree_mismatch") from exc
    if observed_root != trusted_root:
        raise ProvenanceError("git_tree_mismatch")
    commit = run("rev-parse", "--verify", f"{checkout.head_commit}^{{commit}}").decode("ascii").strip()
    tree = run("rev-parse", f"{commit}^{{tree}}").decode("ascii").strip()
    if commit != checkout.head_commit or tree != checkout.head_tree:
        raise ProvenanceError("git_tree_mismatch")
    output = run("ls-tree", "-rz", tree, "--", *(entry.path for entry in policy.entries))
    entries: dict[str, dict[str, str]] = {}
    try:
        for raw in output.split(b"\0"):
            if not raw:
                continue
            metadata, raw_path = raw.split(b"\t", 1)
            mode, object_type, oid = metadata.decode("ascii").split(" ", 2)
            path = raw_path.decode("utf-8")
            if object_type != "blob" or path in entries:
                raise ProvenanceError("git_tree_mismatch")
            entries[path] = {"git_mode": mode, "git_blob_oid": oid}
    except (UnicodeError, ValueError) as exc:
        raise ProvenanceError("git_tree_mismatch") from exc
    if set(entries) != {entry.path for entry in policy.entries}:
        raise ProvenanceError("source_closure_incomplete")
    return MappingProxyType(entries)


def _collect_source_closure(*, policy: Any, root: str | Path,
                            expected_tree: Any, injected_only: bool,
                            marker: object, origin: str, owner: Any,
                            revision_authorization: ExternalRevisionAuthorization | None = None,
                            head_commit: str = "", head_tree: str = "") -> SourceClosure:
    from .k12_runtime_profile import K12SourcePolicy

    if not isinstance(policy, K12SourcePolicy):
        raise TypeError("loader-issued source policy required")
    if origin == RUNTIME_VERIFIED_ORIGIN:
        if injected_only or expected_tree is None:
            raise ProvenanceError("source_closure_incomplete")
    elif origin == INJECTED_TEST_ORIGIN:
        if not injected_only:
            raise ProvenanceError("source_closure_incomplete")
    else:
        raise ProvenanceError("authority_origin_mismatch")
    if revision_authorization is not None:
        if (not isinstance(revision_authorization, ExternalRevisionAuthorization)
                or not revision_authorization.owned_by(owner)
                or revision_authorization.origin != origin):
            raise ProvenanceError("authority_replay")
        if head_commit and head_commit != revision_authorization.head_commit:
            raise ProvenanceError("git_head_mismatch")
        if head_tree and head_tree != revision_authorization.head_tree:
            raise ProvenanceError("git_tree_mismatch")
        head_commit = revision_authorization.head_commit
        head_tree = revision_authorization.head_tree

    root_fd = _open_trusted_source_root(root)
    records: list[SourceRecord] = []
    try:
        for entry in policy.entries:
            path = _normalize_source_path(entry.path)
            fd = _open_source_path(root_fd, path)
            try:
                data, metadata = _read_source_fd(fd)
            finally:
                os.close(fd)
            observed_sha256 = raw_sha256(data)
            observed_blob = git_blob_oid(data)
            tree_entry = _lookup_expected_tree_entry(expected_tree, path)
            if tree_entry is _MISSING_TREE_ENTRY:
                if not injected_only:
                    raise ProvenanceError("source_closure_incomplete")
                git_mode = _git_mode_from_fd(metadata)
                if git_mode != entry.git_mode:
                    raise ProvenanceError("source_mode_mismatch")
            else:
                tree_mode, tree_blob = tree_entry
                if tree_mode != entry.git_mode:
                    raise ProvenanceError("source_mode_mismatch")
                if tree_blob != observed_blob:
                    raise ProvenanceError("source_content_mismatch")
                git_mode = tree_mode
            records.append(SourceRecord(path, git_mode, observed_blob, observed_sha256,
                                        entry.semantic_class))
    finally:
        os.close(root_fd)
    return SourceClosure(policy.identity, policy.digest, tuple(records),
                         _SOURCE_CLOSURE_TOKEN, marker, origin, owner,
                         head_commit, head_tree)


@dataclass(frozen=True, slots=True)
class SubmoduleObservation:
    path: str
    gitlink_commit: str
    observed_commit: str
    clean: bool

    def __post_init__(self) -> None:
        _normalize_source_path(self.path)
        _require_sha1(self.gitlink_commit, "git_submodule_mismatch")
        _require_sha1(self.observed_commit, "git_submodule_mismatch")
        if self.clean is not True or self.gitlink_commit != self.observed_commit:
            raise ProvenanceError("git_submodule_mismatch")

    def canonical(self) -> dict[str, Any]:
        return {"path": self.path, "gitlink_commit": self.gitlink_commit,
                "observed_commit": self.observed_commit, "clean": self.clean}


@dataclass(frozen=True, slots=True)
class CheckoutObservation:
    repository_identity: str
    worktree_identity: str
    git_dir_identity: str
    common_dir_identity: str
    symbolic_head_ref: str
    head_commit: str
    head_tree: str
    index_tree: str
    upstream_ref: str
    upstream_commit: str
    remote_repository: str
    remote_ref: str
    remote_commit: str
    staged_clean: bool
    tracked_clean: bool
    untracked_clean: bool
    generated_artifacts_absent: bool
    git_environment_overrides: tuple[str, ...] = ()
    submodules: tuple[SubmoduleObservation, ...] = ()

    def validate_structure(self) -> None:
        for value in (self.worktree_identity, self.git_dir_identity, self.common_dir_identity):
            _require_sha256(value, "git_worktree_mismatch")
        for value in (self.head_commit, self.head_tree, self.index_tree,
                      self.upstream_commit, self.remote_commit):
            _require_sha1(value, "git_head_mismatch")
        if self.git_environment_overrides:
            raise ProvenanceError("git_environment_override")
        if (not isinstance(self.repository_identity, str) or not self.repository_identity
                or not isinstance(self.symbolic_head_ref, str) or not self.symbolic_head_ref
                or not isinstance(self.upstream_ref, str) or not self.upstream_ref):
            raise ProvenanceError("git_head_mismatch")
        if (not isinstance(self.remote_repository, str) or not self.remote_repository
                or not isinstance(self.remote_ref, str) or not self.remote_ref):
            raise ProvenanceError("git_remote_mismatch")
        if self.index_tree != self.head_tree:
            raise ProvenanceError("git_tree_mismatch")
        if self.upstream_commit != self.head_commit or self.remote_commit != self.head_commit:
            raise ProvenanceError("git_upstream_mismatch")
        if self.staged_clean is not True:
            raise ProvenanceError("git_dirty_staged")
        if self.tracked_clean is not True:
            raise ProvenanceError("git_dirty_tracked")
        if self.untracked_clean is not True or self.generated_artifacts_absent is not True:
            raise ProvenanceError("git_dirty_untracked")
        if any(not isinstance(item, SubmoduleObservation) for item in self.submodules):
            raise ProvenanceError("git_submodule_mismatch")

    def validate(self, authorization: ExternalRevisionAuthorization) -> None:
        if not isinstance(authorization, ExternalRevisionAuthorization):
            raise TypeError("parent-owned external revision authorization required")
        self.validate_structure()
        if self.repository_identity != authorization.repository:
            raise ProvenanceError("git_repository_mismatch")
        if (self.symbolic_head_ref != authorization.branch
                or self.upstream_ref != authorization.branch):
            raise ProvenanceError("git_head_mismatch")
        if (self.remote_repository != authorization.repository
                or self.remote_ref != authorization.branch):
            raise ProvenanceError("git_remote_mismatch")
        if self.head_commit != authorization.head_commit:
            raise ProvenanceError("git_head_mismatch")
        if self.head_tree != authorization.head_tree or self.index_tree != authorization.head_tree:
            raise ProvenanceError("git_tree_mismatch")
        if (self.upstream_commit != authorization.head_commit
                or self.remote_commit != authorization.head_commit):
            raise ProvenanceError("git_upstream_mismatch")

    def canonical(self) -> dict[str, Any]:
        return {
            "repository_identity": self.repository_identity,
            "worktree_identity": self.worktree_identity,
            "git_dir_identity": self.git_dir_identity,
            "common_dir_identity": self.common_dir_identity,
            "symbolic_head_ref": self.symbolic_head_ref,
            "head_commit": self.head_commit, "head_tree": self.head_tree,
            "index_tree": self.index_tree, "upstream_ref": self.upstream_ref,
            "upstream_commit": self.upstream_commit,
            "remote_repository": self.remote_repository,
            "remote_ref": self.remote_ref, "remote_commit": self.remote_commit,
            "staged_clean": self.staged_clean, "tracked_clean": self.tracked_clean,
            "untracked_clean": self.untracked_clean,
            "generated_artifacts_absent": self.generated_artifacts_absent,
            "git_environment_overrides": list(self.git_environment_overrides),
            "submodules": [item.canonical() for item in self.submodules],
        }


@dataclass(frozen=True, slots=True)
class PullRequestObservation:
    repository: str
    number: int
    state: str
    is_draft: bool
    head_repository: str
    head_ref: str
    head_sha: str
    base_ref: str
    base_sha: str
    observed_at: int
    observer: str
    receipt_digest: str

    def semantic_tuple(self) -> tuple[Any, ...]:
        return (self.repository, self.number, self.state, self.is_draft,
                self.head_repository, self.head_ref, self.head_sha,
                self.base_ref, self.base_sha)

    def validate_structure(self, *, now: int) -> None:
        _require_sha1(self.head_sha, "pr_semantic_mismatch")
        _require_sha1(self.base_sha, "pr_semantic_mismatch")
        _require_sha256(self.receipt_digest, "pr_observation_missing")
        if (type(self.number) is not int or self.number <= 0
                or self.state != "OPEN" or self.is_draft is not True
                or not isinstance(self.repository, str) or not self.repository
                or not isinstance(self.head_repository, str) or not self.head_repository
                or not isinstance(self.head_ref, str) or not self.head_ref
                or not isinstance(self.base_ref, str) or not self.base_ref):
            raise ProvenanceError("pr_semantic_mismatch")
        if (type(now) is not int or type(self.observed_at) is not int
                or now < self.observed_at or now - self.observed_at > MAX_PR_AGE_SECONDS):
            raise ProvenanceError("pr_observation_stale")

    def validate(
        self, authorization: ExternalRevisionAuthorization, *, now: int
    ) -> None:
        if not isinstance(authorization, ExternalRevisionAuthorization):
            raise TypeError("parent-owned external revision authorization required")
        self.validate_structure(now=now)
        if not authorization.current_at(now):
            raise ProvenanceError("pr_observation_stale")
        if not authorization.matches_pull_request(self, now=now):
            raise ProvenanceError("pr_semantic_mismatch")

    def canonical(self) -> dict[str, Any]:
        return {"repository": self.repository, "number": self.number,
                "state": self.state, "is_draft": self.is_draft,
                "head_repository": self.head_repository, "head_ref": self.head_ref,
                "head_sha": self.head_sha, "base_ref": self.base_ref,
                "base_sha": self.base_sha, "observed_at": self.observed_at,
                "observer": self.observer, "receipt_digest": self.receipt_digest}


@dataclass(frozen=True, slots=True)
class EnvironmentObservation:
    policy_identity: str
    policy_digest: str
    python_implementation: str
    python_version: str
    distributions_digest: str
    native_runtime_digest: str
    node_identity: str
    java_identity: str
    bridge_sha256: str
    server_jar_sha256: str
    model_id: str
    endpoint_hash: str
    seed_support_state: str
    locale: str
    user_systemd_capability_digest: str
    cgroup_v2_capability_digest: str
    diagnostics_digest: str

    def validate(self) -> None:
        for value in (self.policy_digest, self.distributions_digest,
                      self.native_runtime_digest, self.bridge_sha256,
                      self.server_jar_sha256, self.endpoint_hash,
                      self.user_systemd_capability_digest,
                      self.cgroup_v2_capability_digest, self.diagnostics_digest):
            _require_sha256(value, "environment_mismatch")
        if (self.policy_identity != "minecraft-k12-live-environment-policy/1"
                or self.python_implementation != "CPython"
                or self.python_version != "3.10.19"
                or self.locale != "en_us" or self.seed_support_state != "unsupported"
                or not all((self.node_identity, self.java_identity, self.model_id))):
            raise ProvenanceError("environment_mismatch")

    def canonical(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class TargetLockObservation:
    target_identity: str
    host_hash: str
    port: int
    rcon_target_identity: str
    server_identity: str
    minecraft_version: str
    data_version: str
    world_id: str
    actor_identities: tuple[str, ...]
    region_identity: str
    lock_schema: int
    lock_key: str
    lock_attempt_id: str
    lock_object_identity: str
    fd_open: bool
    regular_file: bool
    device_inode_digest: str
    owner_metadata_digest: str
    continuously_owned: bool

    def validate(self, reservation_id: str) -> None:
        for value in (self.host_hash, self.lock_key, self.lock_object_identity,
                      self.device_inode_digest, self.owner_metadata_digest):
            _require_sha256(value, "target_lock_loss")
        _require_sha256(reservation_id, "authority_replay")
        if (not self.target_identity or self.lock_attempt_id != reservation_id
                or self.lock_schema != 2 or type(self.port) is not int
                or not 1 <= self.port <= 65535 or self.fd_open is not True
                or self.regular_file is not True or self.continuously_owned is not True):
            raise ProvenanceError("target_lock_loss")
        if (self.server_identity != "minecraft-server/1.19.2"
                or self.minecraft_version != "1.19.2"
                or not self.actor_identities or not all(self.actor_identities)):
            raise ProvenanceError("target_identity_mismatch")

    def locale_or_none(self) -> str:
        """Compatibility view for the profile-fixed target locale."""
        return "en_us" if self.server_identity else ""

    def canonical(self) -> dict[str, Any]:
        return {name: (list(value) if isinstance(value := getattr(self, name), tuple) else value)
                for name in self.__dataclass_fields__}


_TARGET_LOCK_INSTANCE_FIELDS = frozenset({
    "lock_attempt_id", "lock_object_identity", "fd_open", "regular_file",
    "device_inode_digest", "owner_metadata_digest", "continuously_owned",
})
_AUTHENTICATED_TARGET_FIELDS = (
    "rcon_target_identity", "server_identity", "minecraft_version",
    "data_version", "actor_identities", "region_identity",
)


def _target_semantics(value: TargetLockObservation | Mapping[str, Any]) -> dict[str, Any]:
    """Return target fields that cannot change across qualification/final scope."""
    canonical = (value.canonical()
                 if isinstance(value, TargetLockObservation)
                 else _deep_thaw(value))
    if not isinstance(canonical, Mapping):
        raise ProvenanceError("target_identity_mismatch")
    return {
        name: canonical[name]
        for name in canonical
        if name not in _TARGET_LOCK_INSTANCE_FIELDS
    }


def _authenticated_target_semantics(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Project the profile's authenticated endpoint semantics for later scopes."""
    try:
        return {
            "rcon_target_identity": profile["rcon_identity"],
            "server_identity": profile["server_identity"],
            "minecraft_version": profile["minecraft_version"],
            "data_version": profile["data_identity"],
            "actor_identities": list(profile["actors"]),
            "region_identity": profile["region"]["name"],
        }
    except (KeyError, TypeError) as exc:
        raise ProvenanceError("target_identity_mismatch") from exc


@dataclass(frozen=True, slots=True)
class OutputRootObservation:
    root_identity: str
    realpath_digest: str
    device_inode_digest: str
    fresh_root_token: str
    outside_all_worktrees: bool
    outside_ledger_root: bool
    existed_before_reservation: bool

    def validate(self) -> None:
        for value in (self.realpath_digest, self.device_inode_digest):
            _require_sha256(value, "fresh_root_violation")
        if (not self.root_identity or not self.fresh_root_token
                or self.outside_all_worktrees is not True
                or self.outside_ledger_root is not True
                or self.existed_before_reservation is not False):
            raise ProvenanceError("fresh_root_violation")

    def canonical(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def _scalar(value: Any) -> bool:
    return value is None or type(value) in {bool, int, float, str}


def _lease_scalar(value: Any) -> Any:
    """Keep the public lease receipt inside the canonical JSON value domain."""
    if type(value) is float:
        if not math.isfinite(value):
            raise ProvenanceError("target_lock_loss")
        return repr(value)
    return value


@dataclass(frozen=True, slots=True, init=False)
class K12RetainedTargetLease:
    """Parent-retained MinecraftTargetLock lease with a narrow public view.

    The retained lock is intentionally opaque.  Revalidation calls only the
    public ``retained_lease_snapshot`` API; no descriptor or private lock
    implementation detail is inspected here.
    """

    lock: Any
    reservation_id: str
    _evidence: Mapping[str, Any]
    identity: str
    _ownership_token: object = field(repr=False, compare=False)
    _owner: Any = field(repr=False, compare=False)

    _SCALARS = frozenset({
        "fd", "fd_dev", "fd_ino", "path_dev", "path_ino", "attempt_id",
        "lock_key", "owner_pid", "owner_alive", "acquired", "quarantined",
    })
    _METADATA_SCALARS = frozenset({
        "schema_version", "status", "attempt_id", "pid", "host", "port", "world_id",
        "lock_key", "acquired_at", "stale_owner_detected", "migrated_from_schema_version",
        "previous_status",
    })

    def __init__(self, lock: Any, reservation_id: str, ownership_token: object = None,
                 token: object = None, owner: Any = None) -> None:
        if token is not _LEASE_TOKEN or ownership_token is None or owner is None:
            raise TypeError("retained target leases are parent-minted")
        if not isinstance(reservation_id, str) or not reservation_id:
            raise ProvenanceError("target_lock_loss")
        from .run_lock import MinecraftTargetLock
        if not isinstance(lock, MinecraftTargetLock):
            raise TypeError("MinecraftTargetLock is required")
        snapshot_method = getattr(lock, "retained_lease_snapshot", None)
        if not callable(snapshot_method):
            raise TypeError("public retained_lease_snapshot is required")
        try:
            snapshot = snapshot_method()
        except Exception as exc:
            raise ProvenanceError("target_lock_loss") from exc
        evidence = self._read_allowlisted(snapshot)
        if evidence["attempt_id"] != reservation_id or evidence["acquired"] is not True \
                or evidence["owner_alive"] is not True or evidence["quarantined"] is True \
                or evidence["metadata"].get("status") != "acquired":
            raise ProvenanceError("target_lock_loss")
        object.__setattr__(self, "lock", lock)
        object.__setattr__(self, "reservation_id", reservation_id)
        object.__setattr__(self, "_evidence", _deep_freeze(evidence))
        object.__setattr__(self, "identity", canonical_sha256({
            "artifact": "minecraft-k12-retained-target-lease/1",
            "reservation_id": reservation_id, "evidence": evidence,
        }))
        object.__setattr__(self, "_ownership_token", ownership_token)
        object.__setattr__(self, "_owner", owner)

    @classmethod
    def _read_allowlisted(cls, snapshot: Any) -> dict[str, Any]:
        if snapshot is None:
            raise ProvenanceError("target_lock_loss")
        values: dict[str, Any] = {}
        for name in cls._SCALARS:
            if not hasattr(snapshot, name):
                raise ProvenanceError("target_lock_loss")
            value = getattr(snapshot, name)
            if not _scalar(value):
                raise ProvenanceError("target_lock_loss")
            values[name] = _lease_scalar(value)
        if (values["fd_dev"], values["fd_ino"]) != (values["path_dev"], values["path_ino"]):
            raise ProvenanceError("target_lock_loss")
        metadata = getattr(snapshot, "metadata", None)
        if not isinstance(metadata, Mapping):
            raise ProvenanceError("target_lock_loss")
        selected: dict[str, Any] = {}
        for name, value in metadata.items():
            if name not in cls._METADATA_SCALARS or not _scalar(value):
                raise ProvenanceError("target_lock_loss")
            selected[name] = _lease_scalar(value)
        values["metadata"] = selected
        if values["metadata"].get("attempt_id") != values["attempt_id"] \
                or values["metadata"].get("lock_key") != values["lock_key"]:
            raise ProvenanceError("target_lock_loss")
        return values

    @property
    def evidence(self) -> Mapping[str, Any]:
        return self._evidence

    @property
    def owner(self) -> Any:
        """The parent controller that retained this lease."""
        return self._owner

    def owned_by(self, owner: Any) -> bool:
        """Authenticate ownership without exposing the parent's token."""
        return self._owner is owner and owner is not None and bool(
            getattr(owner, "owns_target_lease", lambda _lease: False)(self)
        )

    def canonical(self) -> dict[str, Any]:
        return {"reservation_id": self.reservation_id, "identity": self.identity,
                "evidence": _deep_thaw(self._evidence)}

    def revalidate(self) -> Mapping[str, Any]:
        """Re-read and compare only the public retained-lease snapshot."""
        method = getattr(self.lock, "retained_lease_snapshot", None)
        if not callable(method):
            raise ProvenanceError("target_lock_loss")
        try:
            snapshot = method()
        except Exception as exc:
            raise ProvenanceError("target_lock_loss") from exc
        current = self._read_allowlisted(snapshot)
        if current != _deep_thaw(self._evidence):
            raise ProvenanceError("target_lock_loss")
        return self._evidence


def retained_target_binding(lease: K12RetainedTargetLease) -> Mapping[str, Any]:
    """Return the exact target-lock fields authenticated by a retained lease.

    The target observation deliberately contains no lock object.  These
    content-addressed fields are the public bridge between that observation
    and the parent-retained descriptor, so a target with the same reservation
    but a different lock cannot be spliced into an authority.
    """

    if not isinstance(lease, K12RetainedTargetLease):
        raise TypeError("typed retained target lease required")
    evidence = lease.evidence
    metadata = evidence["metadata"]
    inode_digest = raw_sha256(canonical_bytes({
        "fd_dev": evidence["fd_dev"],
        "fd_ino": evidence["fd_ino"],
        "path_dev": evidence["path_dev"],
        "path_ino": evidence["path_ino"],
    }))
    return MappingProxyType({
        "host_hash": raw_sha256(canonical_bytes(metadata.get("host", ""))),
        "port": metadata.get("port"),
        "world_id": metadata.get("world_id"),
        "lock_schema": metadata.get("schema_version"),
        "lock_key": evidence["lock_key"],
        "lock_attempt_id": evidence["attempt_id"],
        "lock_object_identity": raw_sha256(canonical_bytes(lease.canonical())),
        "fd_open": evidence["fd"] is not None,
        "regular_file": True,
        "device_inode_digest": inode_digest,
        "owner_metadata_digest": raw_sha256(canonical_bytes(dict(metadata))),
        "continuously_owned": evidence["owner_alive"] is True
        and evidence["acquired"] is True
        and evidence["quarantined"] is False,
    })


def _target_matches_retained_lease(
        target: TargetLockObservation, lease: K12RetainedTargetLease,
        reservation_id: str,
) -> bool:
    if not isinstance(target, TargetLockObservation) or not isinstance(
        lease, K12RetainedTargetLease
    ):
        return False
    if lease.reservation_id != reservation_id:
        return False
    expected = retained_target_binding(lease)
    return all(getattr(target, name) == value for name, value in expected.items())


@dataclass(frozen=True, slots=True, init=False)
class AuthorityBinding:
    """An unforgeable operational binding with a separate evidence origin."""

    authority_type: str
    namespace: str
    authority_digest: str
    provenance: str
    lifecycle: str
    reservation: str
    activation: str
    origin: str
    _ownership_token: object = field(repr=False, compare=False)

    def __init__(self, authority_type: str | None = None, namespace: str | None = None,
                  authority_digest: str | None = None, provenance: str | None = None,
                  token: object = None, ownership_token: object = None, *, authority: str | None = None,
                  lifecycle: str = "authorized", reservation: str = "",
                  activation: str = "", origin: str | None = None) -> None:
        authority_type = authority if authority_type is None else authority_type
        if token is not _BINDING_TOKEN:
            raise TypeError("live authority bindings are parent-minted")
        if authority_type not in {QUALIFICATION_AUTHORITY, FINAL_AUTHORITY, "mock-only/1"}:
            raise ProvenanceError("authority_namespace_mismatch")
        if provenance not in OPERATIONAL_PROVENANCES:
            raise ProvenanceError("authority_namespace_mismatch")
        origin = _normalize_origin(
            origin if origin is not None else (
                RUNTIME_VERIFIED_ORIGIN
                if provenance != "mock_only" else INJECTED_TEST_ORIGIN
            )
        )
        if _CANONICAL_SHA256.fullmatch(authority_digest or "") is None:
            raise ProvenanceError("authority_namespace_mismatch")
        if lifecycle not in {"authorized", "consumed", "active", "terminal", "quarantined"}:
            raise ProvenanceError("authority_namespace_mismatch")
        _require_identity(namespace or "", "authority_namespace_mismatch")
        if reservation and _SHA256.fullmatch(reservation) is None:
            raise ProvenanceError("authority_namespace_mismatch")
        if activation and _CANONICAL_SHA256.fullmatch(activation) is None:
            raise ProvenanceError("authority_namespace_mismatch")
        object.__setattr__(self, "authority_type", authority_type)
        object.__setattr__(self, "namespace", namespace)
        object.__setattr__(self, "authority_digest", authority_digest)
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "lifecycle", lifecycle)
        object.__setattr__(self, "reservation", reservation)
        object.__setattr__(self, "activation", activation)
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "_ownership_token", ownership_token)

    @classmethod
    def mock_only(cls, namespace: str = "mock") -> "AuthorityBinding":
        return cls("mock-only/1", namespace, canonical_sha256({"mock": namespace}),
                    "mock_only", _BINDING_TOKEN, None, lifecycle="authorized",
                    origin=INJECTED_TEST_ORIGIN)

    @property
    def authority(self) -> str:
        return self.authority_type

    @property
    def reservation_id(self) -> str:
        return self.reservation

    @property
    def activation_digest(self) -> str:
        return self.activation

    @property
    def runtime_admissible(self) -> bool:
        return self.origin == RUNTIME_VERIFIED_ORIGIN

    @property
    def evidence_origin(self) -> str:
        return self.origin

    def canonical(self) -> dict[str, Any]:
        return {
            "authority": self.authority_type,
            "authority_type": self.authority_type,
            "namespace": self.namespace,
            "authority_digest": self.authority_digest,
            "provenance": self.provenance,
            "origin": self.origin,
            "runtime_admissible": self.runtime_admissible,
            "lifecycle": self.lifecycle,
            "reservation": self.reservation,
            "activation": self.activation,
        }


@dataclass(frozen=True, slots=True)
class QualificationPreflight:
    reservation_id: str
    run_authorization_digest: str
    external_revision_authorization: ExternalRevisionAuthorization
    checkout: CheckoutObservation
    pull_request: PullRequestObservation
    source: SourceClosure
    capsule: Any
    authenticated_profile: Any
    authenticated_source_policy: Any
    profile_identity: str
    profile_digest: str
    contracts: Mapping[str, str]
    schedule_digest: str
    environment: EnvironmentObservation
    target: TargetLockObservation
    output: OutputRootObservation
    ledger_identity: str
    ledger_root_digest: str
    run_authorization: K12QualificationRunAuthorization
    target_lease: K12RetainedTargetLease

    def validate(self, *, now: int) -> None:
        from .k12_runtime_profile import K12RuntimeProfile, K12SourcePolicy
        from .k12_execution_capsule import ExecutionCapsule

        _require_sha256(self.reservation_id, "authority_replay")
        _require_sha256(self.ledger_root_digest, "authority_replay")
        if not isinstance(
            self.external_revision_authorization, ExternalRevisionAuthorization
        ):
            raise TypeError("typed external revision authorization required")
        if not self.external_revision_authorization.owned_by(
            self.run_authorization.owner
            if isinstance(self.run_authorization, K12QualificationRunAuthorization)
            else None
        ):
            raise ProvenanceError("authority_replay")
        if self.ledger_identity != "minecraft-k12-live-execution-ledger/1":
            raise ProvenanceError("authority_replay")
        if not isinstance(self.source, SourceClosure):
            raise ProvenanceError("source_closure_incomplete")
        self.checkout.validate(self.external_revision_authorization)
        if self.source.runtime_admissible and (
            self.source.head_commit != self.checkout.head_commit
            or self.source.head_tree != self.checkout.head_tree
        ):
            raise ProvenanceError("git_tree_mismatch")
        if not self.external_revision_authorization.matches_source(self.source):
            raise ProvenanceError("git_tree_mismatch")
        self.pull_request.validate(self.external_revision_authorization, now=now)
        if (not isinstance(self.authenticated_profile, K12RuntimeProfile)
                or not isinstance(self.authenticated_source_policy, K12SourcePolicy)
                or self.authenticated_profile.profile_id != PROFILE_V2
                or self.profile_identity != self.authenticated_profile.profile_id
                or self.profile_digest != self.authenticated_profile.profile_digest):
            raise ProvenanceError("profile_mismatch")
        _require_sha256(self.profile_digest, "profile_mismatch")
        _require_sha256(self.schedule_digest, "contract_mismatch")
        if (not self.contracts
                or any(not isinstance(name, str) or not isinstance(value, str)
                       or _SHA256.fullmatch(value) is None
                       for name, value in self.contracts.items())):
            raise ProvenanceError("contract_mismatch")
        if (dict(self.contracts) != dict(self.authenticated_profile["contract_digests"])
                or tuple(self.authenticated_profile["schedule"])
                    != QUALIFICATION_SCHEDULE
                or self.schedule_digest
                    != QUALIFICATION_SCHEDULE_DIGEST.removeprefix("sha256:")):
            raise ProvenanceError("contract_mismatch")
        if (self.source.policy_identity != self.authenticated_profile["source_policy_identity"]
                or self.source.policy_digest != self.authenticated_profile["source_policy_digest"]
                or self.source.policy_identity != self.authenticated_source_policy.identity
                or self.source.policy_digest != self.authenticated_source_policy.digest):
            raise ProvenanceError("source_closure_incomplete")
        expected_records = tuple(
            (entry.path, entry.git_mode, entry.semantic_class)
            for entry in self.authenticated_source_policy.entries
        )
        observed_records = tuple(
            (record.path, record.git_mode, record.semantic_class)
            for record in self.source.records
        )
        if tuple(record[0] for record in observed_records) != tuple(record[0] for record in expected_records):
            raise ProvenanceError("source_closure_incomplete")
        if tuple(record[1] for record in observed_records) != tuple(record[1] for record in expected_records):
            raise ProvenanceError("source_mode_mismatch")
        if tuple(record[2] for record in observed_records) != tuple(record[2] for record in expected_records):
            raise ProvenanceError("source_semantic_class_mismatch")
        if (self.environment.policy_identity
                != self.authenticated_profile["environment_policy_identity"]
                or self.environment.policy_digest
                != self.authenticated_profile["environment_policy_digest"]):
            raise ProvenanceError("environment_mismatch")
        if (self.environment.node_identity != self.authenticated_profile["node_identity"]
                or self.environment.java_identity != self.authenticated_profile["java_identity"]
                or self.environment.bridge_sha256 != self.authenticated_profile["bridge_content_sha256"]
                or self.environment.server_jar_sha256 != self.authenticated_profile["server_jar_sha256"]
                or self.environment.model_id != self.authenticated_profile["model_identity"]
                or self.environment.endpoint_hash != self.authenticated_profile["endpoint_hash"]):
            raise ProvenanceError("environment_mismatch")
        if (not isinstance(self.capsule, ExecutionCapsule)
                or self.capsule.identity != self.authenticated_profile["execution_capsule_policy_identity"]
                or not self.capsule.verify()):
            raise ProvenanceError("capsule_mismatch")
        if not {"node", "java"} <= self.capsule.categories:
            raise ProvenanceError("capsule_mismatch")
        self.environment.validate()
        self.target.validate(self.reservation_id)
        if (self.target.server_identity != self.authenticated_profile["server_identity"]
                or self.target.minecraft_version != self.authenticated_profile["minecraft_version"]
                or self.target.data_version != self.authenticated_profile["data_identity"]
                or self.target.rcon_target_identity != self.authenticated_profile["rcon_identity"]
                or self.target.actor_identities != tuple(self.authenticated_profile["actors"])
                or self.target.region_identity != self.authenticated_profile["region"]["name"]):
            raise ProvenanceError("target_identity_mismatch")
        self.output.validate()
        if not isinstance(self.target_lease, K12RetainedTargetLease):
            raise TypeError("typed retained target lease required")
        self.target_lease.revalidate()
        if (self.target_lease.reservation_id != self.reservation_id
                or not _target_matches_retained_lease(
                    self.target, self.target_lease, self.reservation_id
                )):
            raise ProvenanceError("target_lock_loss")
        if not isinstance(self.run_authorization, K12QualificationRunAuthorization):
            raise TypeError("typed qualification run authorization required")
        if (self.run_authorization.reservation_id != self.reservation_id
                or self.run_authorization.profile_digest != self.profile_digest
                or self.run_authorization.external_revision_authorization
                    is not self.external_revision_authorization
                or self.run_authorization.body.get("profile_identity") != PROFILE_V2
                or self.run_authorization.output_root_identity != self.output.root_identity
                or self.run_authorization.body.get("namespace") != "qualification"
                or not isinstance(self.run_authorization.body.get("ledger"), Mapping)):
            raise ProvenanceError("authority_replay")
        auth_ledger = self.run_authorization.body["ledger"]
        if (auth_ledger.get("identity") != self.ledger_identity
                or auth_ledger.get("root_digest") != self.ledger_root_digest
                or auth_ledger.get("namespace") != "qualification"
                or auth_ledger.get("reservation_id") != self.reservation_id
                or auth_ledger.get("output_root_identity") != self.output.root_identity
                or auth_ledger.get("nonce") != self.run_authorization.nonce
                or auth_ledger.get("state") != "reserved"):
            raise ProvenanceError("authority_replay")
        if not self.run_authorization.current_at(now):
            raise ProvenanceError("authority_replay")
        if self.run_authorization_digest != self.run_authorization.identity:
            raise ProvenanceError("authority_replay")
        if (_deep_thaw(self.run_authorization.body.get("external_revision_authorization"))
                != self.external_revision_authorization.receipt()
                or self.run_authorization.body.get("external_revision_authorization_digest")
                != self.external_revision_authorization.identity):
            raise ProvenanceError("authority_replay")
        if getattr(self.capsule, "source_aggregate", None) != self.source.aggregate_sha256:
            raise ProvenanceError("capsule_mismatch")


@dataclass(frozen=True, slots=True)
class FirstConsumeObservation:
    checkout: CheckoutObservation
    pull_request: PullRequestObservation
    source: SourceClosure
    capsule: Any
    environment: EnvironmentObservation
    target: TargetLockObservation
    output: OutputRootObservation
    target_lease: K12RetainedTargetLease

    def __post_init__(self) -> None:
        from .k12_execution_capsule import ExecutionCapsule
        expected = (CheckoutObservation, PullRequestObservation, SourceClosure,
                    ExecutionCapsule, EnvironmentObservation, TargetLockObservation,
                    OutputRootObservation)
        values = (self.checkout, self.pull_request, self.source, self.capsule,
                  self.environment, self.target, self.output)
        if any(not isinstance(value, kind) for value, kind in zip(values, expected)):
            raise TypeError("typed first-consume components required")
        if not isinstance(self.target_lease, K12RetainedTargetLease):
            raise TypeError("typed retained target lease required")

    def canonical(self) -> dict[str, Any]:
        result = {
            "checkout": self.checkout.canonical(),
            "pull_request": self.pull_request.canonical(),
            "source_closure": self.source.canonical(),
            "execution_capsule": self.capsule.canonical(),
            "environment": self.environment.canonical(),
            "target": self.target.canonical(),
            "output": self.output.canonical(),
        }
        result["target_lease"] = self.target_lease.canonical()
        return result


@dataclass(frozen=True, slots=True, init=False)
class QualificationSemanticAttestation:
    """Parent-owned proof of the frozen 15-cell plus P1--P4 semantics.

    Construction secrecy is not an authorization boundary.  Consumers must
    additionally ask the parent whether it owns this exact object and whether
    its body still matches the parent's immutable terminal registry.
    """

    qualification_authority_digest: str
    activation_digest: str
    reservation_id: str
    ledger_root_digest: str
    namespace: str
    evidence_origin: str
    schedule_identity: str
    schedule_digest: str
    probe_identity: str
    probe_schedule_digest: str
    cell_terminal_digest: str
    probe_terminal_digest: str
    terminal_receipt_digest: str
    cell_campaign_id: str
    probe_campaign_id: str
    semantic_projection_digest: str
    semantic_verdict_digest: str
    semantic_verifier_identity: str
    semantic_result: str
    source_aggregate: str
    profile_digest: str
    contract_set_digest: str
    capsule_digest: str
    environment_digest: str
    terminal_ledger_digest: str
    terminal_event_digest: str
    identity: str
    _owner: Any = field(repr=False, compare=False)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("qualification semantic attestations are parent-minted")

    def canonical(self) -> dict[str, Any]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if name not in {"identity", "_owner"}
        }


@dataclass(frozen=True, slots=True, init=False)
class _QualificationCoordinateCapability:
    authority_digest: str
    activation_digest: str
    reservation_id: str
    domain: str
    coordinate: str
    nonce: str
    identity: str

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("qualification coordinate capabilities are parent-issued")


@dataclass(frozen=True, slots=True, init=False)
class QualificationCoordinateTerminalReceipt:
    authority_digest: str
    activation_digest: str
    reservation_id: str
    domain: str
    coordinate: str
    capability_identity: str
    record_identity: str
    execution_receipt_identity: str
    execution_stage_receipt_identities: tuple[str, ...]
    identity: str
    _owner: Any = field(repr=False, compare=False)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("qualification terminal receipts are parent-minted")


@dataclass(frozen=True, slots=True, init=False)
class QualificationExecutionStageReceipt:
    """Parent-owned observation of one qualification execution boundary."""

    authority_digest: str
    activation_digest: str
    reservation_id: str
    profile_digest: str
    evidence_origin: str
    campaign_id: str
    domain: str
    coordinate: str
    stage: str
    coordinate_capability_identity: str
    predecessor_identity: str
    boundary_artifact_identity: str
    observation_digest: str
    identity: str
    _owner: Any = field(repr=False, compare=False)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("qualification execution stage receipts are parent-minted")


@dataclass(frozen=True, slots=True, init=False)
class QualificationCoordinateExecutionReceipt:
    """Parent-owned closed execution receipt consumed by terminal publication."""

    authority_digest: str
    activation_digest: str
    reservation_id: str
    profile_digest: str
    evidence_origin: str
    campaign_id: str
    domain: str
    coordinate: str
    coordinate_capability_identity: str
    stage_receipt_identities: tuple[str, ...]
    identity: str
    _owner: Any = field(repr=False, compare=False)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("qualification coordinate execution receipts are parent-minted")


@dataclass(frozen=True, slots=True, init=False)
class QualificationExecutionBoundaryArtifact:
    """Parent-owned output of one typed qualification execution boundary."""

    authority_digest: str
    activation_digest: str
    reservation_id: str
    profile_digest: str
    evidence_origin: str
    campaign_id: str
    domain: str
    coordinate: str
    stage: str
    values: Mapping[str, Any]
    identity: str
    _owner: Any = field(repr=False, compare=False)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("qualification boundary artifacts are parent-minted")


class QualificationResetBoundaryArtifact(QualificationExecutionBoundaryArtifact):
    pass


class QualificationProviderBoundaryArtifact(QualificationExecutionBoundaryArtifact):
    pass


class QualificationPermitBoundaryArtifact(QualificationExecutionBoundaryArtifact):
    pass


class QualificationEffectBoundaryArtifact(QualificationExecutionBoundaryArtifact):
    pass


class QualificationOracleBoundaryArtifact(QualificationExecutionBoundaryArtifact):
    pass


class QualificationContainmentBoundaryArtifact(QualificationExecutionBoundaryArtifact):
    pass


class QualificationProbeContainmentBoundaryArtifact(QualificationExecutionBoundaryArtifact):
    pass


_QUALIFICATION_BOUNDARY_ARTIFACT_TYPES = {
    "reset": QualificationResetBoundaryArtifact,
    "provider": QualificationProviderBoundaryArtifact,
    "permit": QualificationPermitBoundaryArtifact,
    "effect": QualificationEffectBoundaryArtifact,
    "oracle": QualificationOracleBoundaryArtifact,
    "containment": QualificationContainmentBoundaryArtifact,
    "probe_containment": QualificationProbeContainmentBoundaryArtifact,
}
_QUALIFICATION_CELL_STAGE_NAMES = (
    "reset", "provider", "permit", "effect", "oracle", "containment",
)
_QUALIFICATION_PROBE_STAGE_NAMES = ("probe_containment",)
_QUALIFICATION_BOUNDARY_REQUIRED_FIELDS = {
    "reset": frozenset({
        "fresh_root", "reset_passed", "reset_identity", "reset_token", "generation",
    }),
    "provider": frozenset({"terminal", "attempts"}),
    "permit": frozenset({"request_identity", "permit_identity"}),
    "effect": frozenset({
        "capability_state", "retry", "resumed", "replacement", "effect_identity",
        "current_inadmissible", "native_entries",
    }),
    "oracle": frozenset({"value", "evidence_digest", "rejection_verified"}),
    "containment": frozenset({"clean", "evidence_valid", "terminal_verified"}),
    "probe_containment": frozenset({
        "passed", "terminal_verified", "evidence_digest",
    }),
}


@dataclass(frozen=True, slots=True)
class _ValidatedQualificationExecution:
    """Immutable parent snapshot used by the terminal batch commit."""

    session: dict[str, Any]
    capability: _QualificationCoordinateCapability
    execution_receipt: QualificationCoordinateExecutionReceipt
    record: NormalizedCell | NormalizedProbe
    stages: tuple[QualificationExecutionStageReceipt, ...]
    execution_receipt_identity: str
    execution_stage_receipt_identities: tuple[str, ...]
    domain: str
    coordinate: str
    capability_identity: str
    record_identity: str


def _terminal_receipt_census_digest(session: Mapping[str, Any]) -> str:
    return _terminal_receipt_census_digest_from_identities(
        tuple(session["receipt_identities"].values())
    )


def _terminal_receipt_census_digest_from_identities(
    identities: Sequence[str],
) -> str:
    return canonical_sha256({
        "artifact": "minecraft-k12-qualification-terminal-receipt-census/1",
        "receipts": list(identities),
    })


def _normalized_record_snapshot(
    record: NormalizedCell | NormalizedProbe,
) -> dict[str, Any]:
    values = {
        item.name: getattr(record, item.name)
        for item in fields(record)
        if item.name != "identity"
    }
    rejection = values.get("rejection_binding")
    if isinstance(rejection, NormalizedRejectionBinding):
        values["rejection_binding"] = {
            item.name: getattr(rejection, item.name)
            for item in fields(rejection)
            if item.name != "identity"
        }
    return values


def _normalized_record_from_snapshot(
    domain: str,
    values: Mapping[str, Any],
) -> NormalizedCell | NormalizedProbe:
    copied = dict(values)
    rejection = copied.get("rejection_binding")
    if isinstance(rejection, Mapping):
        copied["rejection_binding"] = NormalizedRejectionBinding(**dict(rejection))
    record_type = NormalizedCell if domain == LIVE_QUALIFICATION_NAMESPACE else NormalizedProbe
    record = record_type(**copied)
    return record


def _qualification_terminal_registry_snapshot(
    cells: Sequence[NormalizedCell],
    probes: Sequence[NormalizedProbe],
    receipts: Sequence[QualificationCoordinateTerminalReceipt],
) -> dict[str, Any]:
    return {
        "cells": [
            {"identity": value.identity, "record": _normalized_record_snapshot(value)}
            for value in cells
        ],
        "probes": [
            {"identity": value.identity, "record": _normalized_record_snapshot(value)}
            for value in probes
        ],
        "receipts": [
            {
                "authority": value.authority_digest,
                "activation": value.activation_digest,
                "reservation": value.reservation_id,
                "domain": value.domain,
                "coordinate": value.coordinate,
                "capability": value.capability_identity,
                "record": value.record_identity,
                "execution_receipt": value.execution_receipt_identity,
                "execution_stages": list(value.execution_stage_receipt_identities),
                "identity": value.identity,
            }
            for value in receipts
        ],
    }


def _coordinate_capability_identity(value: _QualificationCoordinateCapability) -> str:
    return canonical_sha256({
        "artifact": "minecraft-k12-qualification-coordinate-capability/1",
        "authority": value.authority_digest,
        "activation": value.activation_digest,
        "reservation": value.reservation_id,
        "domain": value.domain,
        "coordinate": value.coordinate,
        "nonce": value.nonce,
    })


def _coordinate_terminal_receipt_identity(
    value: QualificationCoordinateTerminalReceipt,
) -> str:
    return canonical_sha256({
        "artifact": "minecraft-k12-qualification-coordinate-terminal-receipt/2",
        "authority": value.authority_digest,
        "activation": value.activation_digest,
        "reservation": value.reservation_id,
        "domain": value.domain,
        "coordinate": value.coordinate,
        "capability": value.capability_identity,
        "record": value.record_identity,
        "execution_receipt": value.execution_receipt_identity,
        "execution_stages": list(value.execution_stage_receipt_identities),
    })


def _execution_stage_receipt_identity(
    value: QualificationExecutionStageReceipt,
) -> str:
    return canonical_sha256({
        "artifact": "minecraft-k12-qualification-execution-stage-receipt/1",
        "authority": value.authority_digest,
        "activation": value.activation_digest,
        "reservation": value.reservation_id,
        "profile": value.profile_digest,
        "origin": value.evidence_origin,
        "campaign": value.campaign_id,
        "domain": value.domain,
        "coordinate": value.coordinate,
        "stage": value.stage,
        "coordinate_capability": value.coordinate_capability_identity,
        "predecessor": value.predecessor_identity,
        "boundary_artifact": value.boundary_artifact_identity,
        "observation": value.observation_digest,
    })


def _coordinate_execution_receipt_identity(
    value: QualificationCoordinateExecutionReceipt,
) -> str:
    return canonical_sha256({
        "artifact": "minecraft-k12-qualification-coordinate-execution-receipt/1",
        "authority": value.authority_digest,
        "activation": value.activation_digest,
        "reservation": value.reservation_id,
        "profile": value.profile_digest,
        "origin": value.evidence_origin,
        "campaign": value.campaign_id,
        "domain": value.domain,
        "coordinate": value.coordinate,
        "coordinate_capability": value.coordinate_capability_identity,
        "stages": list(value.stage_receipt_identities),
    })


def _qualification_boundary_artifact_identity(
    value: QualificationExecutionBoundaryArtifact,
) -> str:
    return canonical_sha256({
        "artifact": "minecraft-k12-qualification-boundary-artifact/1",
        "authority": value.authority_digest,
        "activation": value.activation_digest,
        "reservation": value.reservation_id,
        "profile": value.profile_digest,
        "origin": value.evidence_origin,
        "campaign": value.campaign_id,
        "domain": value.domain,
        "coordinate": value.coordinate,
        "stage": value.stage,
        "values": dict(value.values),
    })


@dataclass(frozen=True, slots=True)
class FinalExecutionPrerequisites:
    qualification_authority_digest: str
    qualification_semantic_projection_digest: str
    qualification_probe_projection_digest: str
    qualification_terminal_receipt_census_digest: str
    qualification_terminal_event_digest: str
    qualification_terminal_ledger_digest: str
    head_commit: str
    head_tree: str
    source_aggregate: str
    profile_digest: str
    contract_set_digest: str
    capsule_digest: str
    qualification_semantic_attestation: QualificationSemanticAttestation

    def __post_init__(self) -> None:
        _require_canonical_digest(self.qualification_authority_digest, "final_prerequisite_mismatch")
        for value in (
            self.qualification_semantic_projection_digest,
            self.qualification_probe_projection_digest,
            self.qualification_terminal_receipt_census_digest,
            self.qualification_terminal_event_digest,
            self.qualification_terminal_ledger_digest,
        ):
            _require_canonical_digest(value, "final_prerequisite_mismatch")
        _require_sha1(self.head_commit, "final_prerequisite_mismatch")
        _require_sha1(self.head_tree, "final_prerequisite_mismatch")
        _require_sha256(self.source_aggregate, "final_prerequisite_mismatch")
        _require_sha256(self.profile_digest, "final_prerequisite_mismatch")
        if _SHA256.fullmatch(self.contract_set_digest or "") is not None:
            object.__setattr__(self, "contract_set_digest", "sha256:" + self.contract_set_digest)
        _require_canonical_digest(self.contract_set_digest, "final_prerequisite_mismatch")
        _require_sha256(self.capsule_digest, "final_prerequisite_mismatch")
        try:
            attestation = self.qualification_semantic_attestation
            if (not isinstance(attestation, QualificationSemanticAttestation)
                    or not bool(getattr(
                        attestation._owner,
                        "owns_qualification_semantic_attestation",
                        lambda _attestation: False,
                    )(attestation))
                    or self.qualification_semantic_projection_digest
                        != attestation.semantic_projection_digest
                    or self.qualification_probe_projection_digest
                        != attestation.probe_terminal_digest
                    or self.qualification_terminal_receipt_census_digest
                        != attestation.terminal_receipt_digest
                    or self.qualification_terminal_event_digest
                        != attestation.terminal_event_digest
                    or self.qualification_terminal_ledger_digest
                        != attestation.terminal_ledger_digest
                    or self.qualification_authority_digest
                        != attestation.qualification_authority_digest
                    or self.source_aggregate != attestation.source_aggregate
                    or self.profile_digest != attestation.profile_digest
                    or self.contract_set_digest != attestation.contract_set_digest
                    or self.capsule_digest != attestation.capsule_digest):
                raise ProvenanceError("final_prerequisite_mismatch")
        except (AttributeError, KeyError, IndexError, TypeError, ValueError,
                ProvenanceError) as exc:
            if isinstance(exc, ProvenanceError):
                raise
            raise ProvenanceError("final_prerequisite_mismatch") from exc

    @classmethod
    def from_live_qualification(cls, authority: "QualificationExecutionAuthority",
                                 evidence: Any) -> "FinalExecutionPrerequisites":
        attestation = getattr(evidence, "semantic_attestation", None)
        if not isinstance(attestation, QualificationSemanticAttestation):
            raise ProvenanceError("final_prerequisite_mismatch")
        return cls.from_semantic_attestation(
            authority, attestation,
        )

    @classmethod
    def from_semantic_attestation(
        cls,
        authority: "QualificationExecutionAuthority | ActiveQualificationAuthority",
        attestation: QualificationSemanticAttestation,
    ) -> "FinalExecutionPrerequisites":
        if isinstance(authority, ActiveQualificationAuthority):
            qualification = authority.authority
            owner = authority.owner
        elif isinstance(authority, QualificationExecutionAuthority):
            qualification = authority
            owner = authority.owner
        else:
            raise TypeError("typed qualification authority required")
        if (not isinstance(owner, ParentExecutionAuthority)
                or not owner.owns_qualification_semantic_attestation(
                    attestation, authority=authority,
                )):
            raise ProvenanceError("final_prerequisite_mismatch")
        body = qualification.body
        if (attestation.profile_digest != body["profile"]["detached_digest"]
                or attestation.terminal_ledger_digest
                    != qualification.ledger.head_digest):
            raise ProvenanceError("final_prerequisite_mismatch")
        return cls(
            qualification.identity, attestation.semantic_projection_digest,
            attestation.probe_terminal_digest, attestation.terminal_receipt_digest,
            attestation.terminal_event_digest, attestation.terminal_ledger_digest,
            body["checkout"]["head_commit"], body["checkout"]["head_tree"],
            body["source_closure"]["aggregate_sha256"], body["profile"]["detached_digest"],
            canonical_sha256(_deep_thaw(body["contracts"])),
            body["execution_capsule"]["capsule_digest"], attestation,
        )

    @property
    def origin(self) -> str:
        return _normalize_origin(self.qualification_semantic_attestation.evidence_origin)

    @property
    def runtime_admissible(self) -> bool:
        return self.origin == RUNTIME_VERIFIED_ORIGIN

    @property
    def evidence_origin(self) -> str:
        return self.origin

    @property
    def qualification_semantic_attestation_digest(self) -> str:
        return self.qualification_semantic_attestation.identity

    def matches(self, authority: "QualificationExecutionAuthority") -> bool:
        if not isinstance(authority, QualificationExecutionAuthority):
            return False
        body = authority.body
        expected_origin = (
            RUNTIME_VERIFIED_ORIGIN
            if authority.origin == RUNTIME_VERIFIED_ORIGIN else INJECTED_FAKE_ORIGIN
        )
        return (self.qualification_authority_digest == authority.identity
                and self.origin == expected_origin
                and self.head_commit == body["checkout"]["head_commit"]
                and self.head_tree == body["checkout"]["head_tree"]
                and self.source_aggregate == body["source_closure"]["aggregate_sha256"]
                and self.profile_digest == body["profile"]["detached_digest"]
                and self.contract_set_digest == canonical_sha256(_deep_thaw(body["contracts"]))
                and self.capsule_digest == body["execution_capsule"]["capsule_digest"]
                and self.qualification_semantic_attestation.qualification_authority_digest
                    == authority.identity
                and self.qualification_semantic_projection_digest
                    == self.qualification_semantic_attestation.semantic_projection_digest
                and self.qualification_probe_projection_digest
                    == self.qualification_semantic_attestation.probe_terminal_digest
                and self.qualification_terminal_receipt_census_digest
                    == self.qualification_semantic_attestation.terminal_receipt_digest
                and self.qualification_terminal_event_digest
                    == self.qualification_semantic_attestation.terminal_event_digest
                and self.qualification_terminal_ledger_digest
                    == self.qualification_semantic_attestation.terminal_ledger_digest
                and bool(getattr(
                    authority.owner,
                    "owns_qualification_semantic_attestation",
                    lambda _attestation, **_kwargs: False,
                )(self.qualification_semantic_attestation, authority=authority))
        )


@dataclass(frozen=True, slots=True)
class FinalFirstConsumeObservation:
    prerequisites: FinalExecutionPrerequisites
    checkout: CheckoutObservation
    source: SourceClosure
    capsule: Any
    environment: EnvironmentObservation
    target: TargetLockObservation
    output: OutputRootObservation
    target_lease: K12RetainedTargetLease
    pull_request: PullRequestObservation

    def __post_init__(self) -> None:
        if not isinstance(self.prerequisites, FinalExecutionPrerequisites):
            raise TypeError("typed final prerequisites required")
        values = (
            self.checkout, self.source, self.environment, self.target, self.output,
            self.target_lease, self.pull_request,
        )
        kinds = (
            CheckoutObservation, SourceClosure, EnvironmentObservation,
            TargetLockObservation, OutputRootObservation, K12RetainedTargetLease,
            PullRequestObservation,
        )
        if any(value is None or not isinstance(value, kind)
               for value, kind in zip(values, kinds)):
            raise TypeError("typed final first-consume components required")
        from .k12_execution_capsule import ExecutionCapsule
        if not isinstance(self.capsule, ExecutionCapsule):
            raise TypeError("typed final execution capsule required")

    def canonical(self) -> dict[str, Any]:
        result: dict[str, Any] = {"prerequisites": {
            name: getattr(self.prerequisites, name)
            for name in self.prerequisites.__dataclass_fields__
            if name != "qualification_semantic_attestation"
        }}
        result.update({
            "checkout": self.checkout.canonical(),
            "source_closure": self.source.canonical(),
            "execution_capsule": self.capsule.canonical(),
            "environment": self.environment.canonical(),
            "target": self.target.canonical(),
            "output": self.output.canonical(),
            "target_lease": self.target_lease.canonical(),
            "pull_request": self.pull_request.canonical(),
        })
        return result


@dataclass(frozen=True, slots=True, init=False)
class _RunAuthorizationBase:
    body: Mapping[str, Any]
    detached_artifact_sha256: str
    identity: str
    capabilities: tuple[str, ...]
    evidence_origin: str
    reservation_id: str
    nonce: str
    output_root_identity: str
    ownership_token: object = field(repr=False, compare=False)
    owner: Any = field(repr=False, compare=False)

    def __init__(self, body: Mapping[str, Any], ownership_token: object, token: object,
                 schema: str, expected_capability: str, owner: Any = None) -> None:
        if token is not _RUN_AUTH_TOKEN or ownership_token is None or owner is None:
            raise TypeError("run authorizations are parent-minted")
        if not isinstance(body, Mapping) or "detached_artifact_sha256" in body:
            raise ProvenanceError("authority_digest_mismatch")
        canonical = dict(body)
        capabilities = tuple(canonical.get("capabilities", ()))
        if capabilities != (expected_capability,):
            raise ProvenanceError("authority_capability_mismatch")
        origin = _normalize_origin(canonical.get("evidence_origin"))
        canonical["evidence_origin"] = origin
        reservation_id = canonical.get("reservation_id")
        if _SHA256.fullmatch(reservation_id or "") is None:
            raise ProvenanceError("authority_replay")
        nonce = canonical.get("nonce")
        root = canonical.get("output_root_identity")
        if not isinstance(nonce, str) or not nonce or not isinstance(root, str) or not root:
            raise ProvenanceError("authority_replay")
        detached = raw_sha256(canonical_bytes(canonical))
        object.__setattr__(self, "body", _deep_freeze(canonical))
        object.__setattr__(self, "detached_artifact_sha256", detached)
        object.__setattr__(self, "identity", canonical_sha256({"schema": schema, "digest": detached}))
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "evidence_origin", origin)
        object.__setattr__(self, "reservation_id", reservation_id)
        object.__setattr__(self, "nonce", nonce)
        object.__setattr__(self, "output_root_identity", root)
        object.__setattr__(self, "ownership_token", ownership_token)
        object.__setattr__(self, "owner", owner)

    def receipt(self) -> dict[str, Any]:
        return {**_deep_thaw(self.body), "detached_artifact_sha256": self.detached_artifact_sha256}

    @property
    def capability(self) -> str:
        return self.capabilities[0]

    @property
    def authorization_digest(self) -> str:
        return self.identity

    @property
    def digest(self) -> str:
        return self.detached_artifact_sha256

    @property
    def namespace(self) -> str:
        return self.body["namespace"]

    @property
    def origin(self) -> str:
        return self.evidence_origin

    @property
    def runtime_admissible(self) -> bool:
        return self.origin == RUNTIME_VERIFIED_ORIGIN

    def current_at(self, now: int) -> bool:
        issued_at = self.body.get("issued_at")
        return (type(now) is int and type(issued_at) is int
                and issued_at <= now <= issued_at + MAX_PR_AGE_SECONDS)

    def binding(self, namespace: str | None = None) -> AuthorityBinding:
        if isinstance(self, K12QualificationRunAuthorization):
            selected = namespace or "live_qualification"
            if selected not in {"live_qualification", "qualification_probe"}:
                raise ProvenanceError("authority_namespace_mismatch")
            authority_type = QUALIFICATION_AUTHORITY
            provenance = selected
        else:
            selected = namespace or "live_final"
            if selected != "live_final":
                raise ProvenanceError("authority_namespace_mismatch")
            authority_type = FINAL_AUTHORITY
            provenance = "live_final"
        return AuthorityBinding(authority_type, selected, self.identity, provenance,
                                _BINDING_TOKEN, self.ownership_token,
                                lifecycle="authorized", reservation=self.reservation_id,
                                origin=self.origin)


@dataclass(frozen=True, slots=True, init=False)
class K12QualificationRunAuthorization(_RunAuthorizationBase):
    external_revision_authorization: ExternalRevisionAuthorization = field(
        repr=False, compare=False
    )

    def __init__(self, body: Mapping[str, Any], ownership_token: object,
                 token: object = None,
                 external_revision_authorization: ExternalRevisionAuthorization = None,
                 owner: Any = None) -> None:
        if not isinstance(external_revision_authorization, ExternalRevisionAuthorization):
            raise TypeError("typed external revision authorization required")
        _RunAuthorizationBase.__init__(
            self, body, ownership_token, token, QUALIFICATION_RUN_AUTHORIZATION,
            "qualification_execute", owner,
        )
        if (external_revision_authorization.ownership_token is not ownership_token
                or external_revision_authorization.owner is not owner
                or _deep_thaw(self.body.get("external_revision_authorization"))
                    != external_revision_authorization.receipt()
                or self.body.get("external_revision_authorization_digest")
                    != external_revision_authorization.identity):
            raise ProvenanceError("authority_replay")
        object.__setattr__(
            self, "external_revision_authorization", external_revision_authorization
        )
        if self.evidence_origin not in {RUNTIME_VERIFIED_ORIGIN, INJECTED_TEST_ORIGIN}:
            raise ProvenanceError("authority_origin_mismatch")

    @property
    def profile_digest(self) -> str:
        return self.body.get("profile_digest", "")

    @property
    def revision_authorization(self) -> ExternalRevisionAuthorization:
        return self.external_revision_authorization

    def current_at(self, now: int) -> bool:
        return (_RunAuthorizationBase.current_at(self, now)
                and self.external_revision_authorization.current_at(now))


@dataclass(frozen=True, slots=True, init=False)
class K12FinalRunAuthorization(_RunAuthorizationBase):
    external_revision_authorization: ExternalRevisionAuthorization = field(
        repr=False, compare=False
    )

    def __init__(self, body: Mapping[str, Any], ownership_token: object,
                 token: object = None,
                 external_revision_authorization: ExternalRevisionAuthorization = None,
                 owner: Any = None) -> None:
        if not isinstance(external_revision_authorization, ExternalRevisionAuthorization):
            raise TypeError("typed external revision authorization required")
        _RunAuthorizationBase.__init__(
            self, body, ownership_token, token, FINAL_RUN_AUTHORIZATION, "final_execute",
            owner,
        )
        if (external_revision_authorization.ownership_token is not ownership_token
                or external_revision_authorization.owner is not owner
                or _deep_thaw(self.body.get("external_revision_authorization"))
                    != external_revision_authorization.receipt()
                or self.body.get("external_revision_authorization_digest")
                    != external_revision_authorization.identity):
            raise ProvenanceError("authority_replay")
        object.__setattr__(
            self, "external_revision_authorization", external_revision_authorization
        )
        if self.evidence_origin not in AUTHORIZATION_ORIGINS:
            raise ProvenanceError("authority_origin_mismatch")

    @property
    def profile_digest(self) -> str:
        return self.body.get("profile_digest", "")

    @property
    def revision_authorization(self) -> ExternalRevisionAuthorization:
        return self.external_revision_authorization

    def current_at(self, now: int) -> bool:
        return (_RunAuthorizationBase.current_at(self, now)
                and self.external_revision_authorization.current_at(now))


@dataclass(frozen=True, slots=True, init=False)
class QualificationExecutionAuthority:
    body: Mapping[str, Any]
    detached_artifact_sha256: str
    identity: str
    run_authorization: K12QualificationRunAuthorization
    ownership_token: object = field(repr=False, compare=False)
    ledger: Any = field(repr=False, compare=False)
    owner: Any = field(repr=False, compare=False)

    def __init__(self, body: Mapping[str, Any], ownership_token: object, ledger: Any = None,
                 token: object = None, run_authorization: K12QualificationRunAuthorization = None,
                 parent: Any = None) -> None:
        if token is not _AUTHORITY_TOKEN or ownership_token is None \
                or not isinstance(run_authorization, K12QualificationRunAuthorization):
            raise TypeError("qualification execution authority is parent-minted")
        canonical = dict(body)
        if "detached_artifact_sha256" in canonical:
            raise ProvenanceError("authority_digest_mismatch")
        canonical["evidence_origin"] = _normalize_origin(canonical.get("evidence_origin"))
        if run_authorization.evidence_origin != canonical["evidence_origin"]:
            raise ProvenanceError("authority_origin_mismatch")
        if (canonical.get("external_revision_authorization")
                != run_authorization.external_revision_authorization.receipt()
                or canonical.get("external_revision_authorization_digest")
                != run_authorization.external_revision_authorization.identity):
            raise ProvenanceError("authority_replay")
        detached = raw_sha256(canonical_bytes(canonical))
        object.__setattr__(self, "body", _deep_freeze(canonical))
        object.__setattr__(self, "detached_artifact_sha256", detached)
        object.__setattr__(self, "identity", canonical_sha256({"schema": QUALIFICATION_AUTHORITY,
                                                               "digest": detached}))
        object.__setattr__(self, "run_authorization", run_authorization)
        object.__setattr__(self, "ownership_token", ownership_token)
        object.__setattr__(self, "ledger", ledger)
        object.__setattr__(self, "owner", parent)

    def receipt(self) -> dict[str, Any]:
        return {**_deep_thaw(self.body), "detached_artifact_sha256": self.detached_artifact_sha256}

    @property
    def lifecycle(self) -> str:
        return self.owner.lifecycle_for(self.ledger)[0]

    @property
    def reservation_id(self) -> str:
        return self.body["reservation_id"]

    @property
    def profile_digest(self) -> str:
        return self.body["profile"]["detached_digest"]

    @property
    def origin(self) -> str:
        return _normalize_origin(self.body.get("evidence_origin"))

    @property
    def runtime_admissible(self) -> bool:
        return self.origin == RUNTIME_VERIFIED_ORIGIN

    @property
    def evidence_origin(self) -> str:
        return self.origin

    @property
    def activation(self) -> str:
        return self.owner.lifecycle_for(self.ledger)[1]

    def current_at(self, now: int | None = None) -> bool:
        return self.owner.authority_is_current(self, now=now)

    def binding(self, namespace: str = "live_qualification") -> AuthorityBinding:
        if namespace not in {"live_qualification", "qualification_probe"}:
            raise ProvenanceError("authority_namespace_mismatch")
        lifecycle, activation = self.owner.lifecycle_for(self.ledger)
        return AuthorityBinding(QUALIFICATION_AUTHORITY, namespace, self.identity,
                                namespace, _BINDING_TOKEN, self.ownership_token,
                                lifecycle=lifecycle, reservation=self.body["reservation_id"],
                                activation=activation, origin=self.origin)

    @property
    def authority_binding(self) -> AuthorityBinding:
        return self.binding()

    def owns(self, binding: AuthorityBinding) -> bool:
        return (isinstance(binding, AuthorityBinding)
                and binding._ownership_token is self.ownership_token
                and binding.authority_type == QUALIFICATION_AUTHORITY
                and binding.authority_digest == self.identity
                and binding.reservation == self.body["reservation_id"]
                and binding.origin == self.origin
                and binding.namespace in {"live_qualification", "qualification_probe"})

    def refresh_pull_request(self, observation: PullRequestObservation, *, now: int) -> None:
        if not isinstance(self.owner, ParentExecutionAuthority):
            raise ProvenanceError("authority_replay")
        now = self.owner._resolve_time(now)
        observation.validate(
            self.run_authorization.external_revision_authorization, now=now
        )
        if tuple(observation.semantic_tuple()) != tuple(self.body["pull_request_semantic_tuple"]):
            raise ProvenanceError("pr_semantic_mismatch")

    # Compatibility views for qualification code outside this scoped change.
    # New authority code uses the public ``ledger``, ``owner``, and lifecycle
    # APIs above.
    @property
    def _ledger(self) -> Any:
        return self.ledger

    @property
    def _parent(self) -> Any:
        return self.owner

    @property
    def _marker(self) -> object:
        return self.ownership_token


@dataclass(frozen=True, slots=True, init=False)
class FinalExecutionAuthority:
    body: Mapping[str, Any]
    detached_artifact_sha256: str
    identity: str
    run_authorization: K12FinalRunAuthorization
    ownership_token: object = field(repr=False, compare=False)
    ledger: Any = field(repr=False, compare=False)
    owner: Any = field(repr=False, compare=False)

    def __init__(self, body: Mapping[str, Any], ownership_token: object, ledger: Any = None,
                 token: object = None, run_authorization: K12FinalRunAuthorization = None,
                 parent: Any = None) -> None:
        if token is not _AUTHORITY_TOKEN or ownership_token is None \
                or not isinstance(run_authorization, K12FinalRunAuthorization):
            raise TypeError("final execution authority is parent-minted")
        canonical = dict(body)
        if "detached_artifact_sha256" in canonical:
            raise ProvenanceError("authority_digest_mismatch")
        canonical["evidence_origin"] = _normalize_origin(canonical.get("evidence_origin"))
        if run_authorization.evidence_origin != canonical["evidence_origin"]:
            raise ProvenanceError("authority_origin_mismatch")
        if (canonical.get("external_revision_authorization")
                != run_authorization.external_revision_authorization.receipt()
                or canonical.get("external_revision_authorization_digest")
                != run_authorization.external_revision_authorization.identity):
            raise ProvenanceError("authority_replay")
        detached = raw_sha256(canonical_bytes(canonical))
        object.__setattr__(self, "body", _deep_freeze(canonical))
        object.__setattr__(self, "detached_artifact_sha256", detached)
        object.__setattr__(self, "identity", canonical_sha256({"schema": FINAL_AUTHORITY,
                                                               "digest": detached}))
        object.__setattr__(self, "run_authorization", run_authorization)
        object.__setattr__(self, "ownership_token", ownership_token)
        object.__setattr__(self, "ledger", ledger)
        object.__setattr__(self, "owner", parent)

    def receipt(self) -> dict[str, Any]:
        return {**_deep_thaw(self.body), "detached_artifact_sha256": self.detached_artifact_sha256}

    @property
    def lifecycle(self) -> str:
        return self.owner.lifecycle_for(self.ledger)[0]

    @property
    def reservation_id(self) -> str:
        return self.body["reservation_id"]

    @property
    def profile_digest(self) -> str:
        return self.body["profile_digest"]

    @property
    def origin(self) -> str:
        return _normalize_origin(self.body.get("evidence_origin"))

    @property
    def runtime_admissible(self) -> bool:
        return self.origin == RUNTIME_VERIFIED_ORIGIN

    @property
    def evidence_origin(self) -> str:
        return self.origin

    @property
    def activation(self) -> str:
        return self.owner.lifecycle_for(self.ledger)[1]

    def current_at(self, now: int | None = None) -> bool:
        return self.owner.authority_is_current(self, now=now)

    def binding(self) -> AuthorityBinding:
        lifecycle, activation = self.owner.lifecycle_for(self.ledger)
        return AuthorityBinding(FINAL_AUTHORITY, "live_final", self.identity, "live_final",
                                _BINDING_TOKEN, self.ownership_token, lifecycle=lifecycle,
                                reservation=self.body["reservation_id"], activation=activation,
                                origin=self.origin)

    @property
    def authority_binding(self) -> AuthorityBinding:
        return self.binding()

    def owns(self, binding: AuthorityBinding) -> bool:
        return (isinstance(binding, AuthorityBinding)
                and binding._ownership_token is self.ownership_token
                and binding.authority_type == FINAL_AUTHORITY
                and binding.authority_digest == self.identity
                and binding.namespace == "live_final"
                and binding.origin == self.origin
                and binding.reservation == self.body["reservation_id"])

    def refresh_pull_request(self, observation: PullRequestObservation, *, now: int) -> None:
        if not isinstance(self.owner, ParentExecutionAuthority):
            raise ProvenanceError("authority_replay")
        now = self.owner._resolve_time(now)
        semantic = self.body.get("pull_request_semantic_tuple")
        if semantic is None:
            raise ProvenanceError("pr_observation_missing")
        observation.validate(
            self.run_authorization.external_revision_authorization, now=now
        )
        if tuple(observation.semantic_tuple()) != tuple(semantic):
            raise ProvenanceError("pr_semantic_mismatch")

    @property
    def _ledger(self) -> Any:
        return self.ledger

    @property
    def _parent(self) -> Any:
        return self.owner

    @property
    def _marker(self) -> object:
        return self.ownership_token


@dataclass(frozen=True, slots=True, init=False)
class ActiveQualificationAuthority:
    authority: QualificationExecutionAuthority
    activation_digest: str
    binding: AuthorityBinding
    ownership_token: object = field(repr=False, compare=False)

    def __init__(self, authority: QualificationExecutionAuthority, activation_digest: str,
                 ownership_token: object, token: object = None) -> None:
        if token is not _ACTIVE_TOKEN or ownership_token is None or not isinstance(authority, QualificationExecutionAuthority):
            raise TypeError("active qualification authority is parent-minted")
        _require_canonical_digest(activation_digest, "authority_replay")
        binding = AuthorityBinding(QUALIFICATION_AUTHORITY, "live_qualification", authority.identity,
                                   "live_qualification", _BINDING_TOKEN, ownership_token,
                                   lifecycle="active", reservation=authority.body["reservation_id"],
                                   activation=activation_digest, origin=authority.origin)
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "activation_digest", activation_digest)
        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "ownership_token", ownership_token)

    @property
    def identity(self) -> str:
        return self.authority.identity

    @property
    def lifecycle(self) -> str:
        return "active"

    @property
    def reservation_id(self) -> str:
        return self.authority.reservation_id

    @property
    def activation(self) -> str:
        return self.activation_digest

    @property
    def owner(self) -> Any:
        return self.authority.owner

    def current_at(self, now: int | None = None) -> bool:
        return self.authority.current_at(now)

    @property
    def origin(self) -> str:
        return self.authority.origin

    @property
    def runtime_admissible(self) -> bool:
        return self.origin == RUNTIME_VERIFIED_ORIGIN

    @property
    def evidence_origin(self) -> str:
        return self.origin

    @property
    def body(self) -> Mapping[str, Any]:
        return self.authority.body

    @property
    def profile_digest(self) -> str:
        return self.authority.body["profile"]["detached_digest"]

    @property
    def authority_binding(self) -> AuthorityBinding:
        return self.binding

    def owns(self, binding: AuthorityBinding) -> bool:
        return self.authority.owns(binding) and binding.lifecycle == "active" \
            and binding.activation == self.activation_digest

    def refresh_pull_request(self, observation: PullRequestObservation, *, now: int) -> None:
        self.authority.refresh_pull_request(observation, now=now)


@dataclass(frozen=True, slots=True, init=False)
class ActiveFinalAuthority:
    authority: FinalExecutionAuthority
    activation_digest: str
    binding: AuthorityBinding
    ownership_token: object = field(repr=False, compare=False)

    def __init__(self, authority: FinalExecutionAuthority, activation_digest: str,
                 ownership_token: object, token: object = None) -> None:
        if token is not _ACTIVE_TOKEN or ownership_token is None or not isinstance(authority, FinalExecutionAuthority):
            raise TypeError("active final authority is parent-minted")
        _require_canonical_digest(activation_digest, "authority_replay")
        binding = AuthorityBinding(FINAL_AUTHORITY, "live_final", authority.identity, "live_final",
                                   _BINDING_TOKEN, ownership_token, lifecycle="active",
                                   reservation=authority.body["reservation_id"],
                                   activation=activation_digest, origin=authority.origin)
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "activation_digest", activation_digest)
        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "ownership_token", ownership_token)

    @property
    def identity(self) -> str:
        return self.authority.identity

    @property
    def lifecycle(self) -> str:
        return "active"

    @property
    def reservation_id(self) -> str:
        return self.authority.reservation_id

    @property
    def activation(self) -> str:
        return self.activation_digest

    @property
    def owner(self) -> Any:
        return self.authority.owner

    def current_at(self, now: int | None = None) -> bool:
        return self.authority.current_at(now)

    @property
    def origin(self) -> str:
        return self.authority.origin

    @property
    def runtime_admissible(self) -> bool:
        return self.origin == RUNTIME_VERIFIED_ORIGIN

    @property
    def evidence_origin(self) -> str:
        return self.origin

    @property
    def body(self) -> Mapping[str, Any]:
        return self.authority.body

    @property
    def profile_digest(self) -> str:
        return self.authority.body["profile_digest"]

    @property
    def authority_binding(self) -> AuthorityBinding:
        return self.binding

    def owns(self, binding: AuthorityBinding) -> bool:
        return self.authority.owns(binding) and binding.lifecycle == "active" \
            and binding.activation == self.activation_digest

    def refresh_pull_request(self, observation: PullRequestObservation, *, now: int) -> None:
        self.authority.refresh_pull_request(observation, now=now)


LiveQualificationEvidence = QualificationEvidenceContract
K12LiveQualificationEvidence = QualificationEvidenceContract
QualificationEvidenceProtocol = QualificationEvidenceContract


def _live_evidence_values(evidence: Any) -> dict[str, Any]:
    """Reject the retired aggregate-projection authorization boundary."""

    del evidence
    raise ProvenanceError("final_prerequisite_mismatch")


def _ledger_binding_state(ledger: Any) -> tuple[str, str]:
    state = getattr(ledger, "state", None)
    if state == "active":
        return "active", getattr(ledger, "head_digest", "")
    if state == "first_consume_verified":
        return "consumed", ""
    if state == "terminal":
        return "terminal", ""
    if state == "quarantined":
        return "quarantined", ""
    return "authorized", ""


def durable_ledger_root_digest(ledger: Any) -> str:
    """Return the content-addressed identity of a durable ledger root."""
    root = getattr(ledger, "root", None)
    if not isinstance(root, Path):
        try:
            root = Path(root)
        except (TypeError, ValueError) as exc:
            raise ProvenanceError("authority_replay") from exc
    return raw_sha256(canonical_bytes({
        "artifact": _LEDGER_ROOT_ARTIFACT,
        "root": str(root.resolve(strict=True)),
    }))


def durable_ledger_snapshot(ledger: Any) -> Mapping[str, Any]:
    """Read the public immutable ledger identity used by authority binding."""
    from .k12_execution_capsule import DurableLedger

    if not isinstance(ledger, DurableLedger):
        raise TypeError("typed durable ledger required")
    try:
        if not ledger.verify_chain():
            raise ProvenanceError("ledger_corrupt")
        root = ledger.root.resolve(strict=True)
        snapshot = {
            "identity": ledger.identity,
            "root": str(root),
            "root_digest": durable_ledger_root_digest(ledger),
            "namespace": ledger.namespace,
            "reservation_id": ledger.reservation_id,
            "output_root_identity": ledger.output_root_identity,
            "nonce": ledger.nonce,
            "state": ledger.state,
            "head_digest": ledger.head_digest,
            "reservation_record_digest": ledger.events[0].digest,
        }
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ProvenanceError("ledger_corrupt") from exc
    return MappingProxyType(snapshot)


def authority_owns_profile(authority: Any, binding: AuthorityBinding, *,
                           profile_id: str, profile_digest: str) -> bool:
    """Bind a downstream capability to an active parent authority/profile."""
    if not isinstance(binding, AuthorityBinding) or profile_id != PROFILE_V2:
        return False
    execution = authority.authority if isinstance(authority, (ActiveQualificationAuthority,
                                                               ActiveFinalAuthority)) else authority
    owner = getattr(execution, "owner", None)
    if (owner is None or not owner.owns_authority(execution)
            or execution.lifecycle != "active"):
        return False
    validator = getattr(owner, "validate_current_authority", None)
    if not callable(validator):
        return False
    try:
        if validator(execution) is not True:
            return False
    except (AttributeError, TypeError, ValueError, ProvenanceError):
        return False
    if getattr(binding, "origin", None) != getattr(execution, "origin", None):
        return False
    if isinstance(authority, ActiveQualificationAuthority):
        return (authority.owns(binding) and execution.body["profile"]["identity"] == profile_id
                and execution.body["profile"]["detached_digest"] == profile_digest)
    if isinstance(authority, QualificationExecutionAuthority):
        return (authority.owns(binding) and binding.lifecycle == "active"
                and execution.body["profile"]["identity"] == profile_id
                and execution.body["profile"]["detached_digest"] == profile_digest)
    if isinstance(authority, ActiveFinalAuthority):
        return authority.owns(binding) and execution.body["profile_digest"] == profile_digest
    if isinstance(authority, FinalExecutionAuthority):
        return (authority.owns(binding) and binding.lifecycle == "active"
                and execution.body["profile_digest"] == profile_digest)
    return False


def refresh_pull_request_observation(authority: Any, observation: PullRequestObservation,
                                     *, now: int) -> None:
    """Revalidate the frozen semantic PR tuple at a live authority boundary."""
    if not isinstance(authority, (QualificationExecutionAuthority, FinalExecutionAuthority,
                                   ActiveQualificationAuthority, ActiveFinalAuthority)):
        raise TypeError("typed execution authority required")
    authority.refresh_pull_request(observation, now=now)


def _ledger_is(ledger: Any, *, namespace: str, state: str, reservation: str | None = None) -> bool:
    return (getattr(ledger, "identity", None) == "minecraft-k12-live-execution-ledger/1"
            and getattr(ledger, "namespace", None) == namespace
            and getattr(ledger, "state", None) == state
            and (reservation is None or getattr(ledger, "reservation_id", None) == reservation)
            and getattr(ledger, "verify_chain", lambda: False)())


class _TrustedClock:
    """Parent-owned clock used at live authority consumption boundaries.

    Runtime parents read wall-clock seconds for every observation.  Injected
    controllers instead hold a deterministic logical value; the value can
    only move through an explicit timestamp supplied to an authority API or
    through the controller's test-only advance method.
    """

    def __init__(self, *, injected: bool) -> None:
        self._injected = injected
        self._lock = threading.RLock()
        self._value = 0

    def now(self) -> int:
        if not self._injected:
            observed = int(time.time())
            with self._lock:
                if observed < self._value:
                    raise ProvenanceError("trusted_clock_rollback")
                self._value = observed
                return self._value
        with self._lock:
            return self._value

    def observe_explicit(self, value: int) -> int:
        if type(value) is not int:
            raise TypeError("integer trusted time is required")
        if not self._injected:
            return self.now()
        with self._lock:
            if value < self._value:
                raise ProvenanceError("trusted_clock_rollback")
            self._value = value
            return self._value

    def advance(self, seconds: int) -> int:
        if type(seconds) is not int or seconds < 0:
            raise ValueError("non-negative integer trusted-time advance is required")
        if not self._injected:
            raise ProvenanceError("injected clock is required")
        with self._lock:
            self._value += seconds
            return self._value


class ParentExecutionAuthority:
    """The only parent mint for run authorizations and live capabilities."""

    def __init__(self, *, revision_verifier_key: bytes | None = None,
                 revision_verifier_identity: str | None = None,
                 _origin: str = RUNTIME_VERIFIED_ORIGIN,
                 _owner: Any = None, _token: object = None) -> None:
        if _origin != RUNTIME_VERIFIED_ORIGIN and _token is not _INJECTED_CONTROLLER_TOKEN:
            raise TypeError("injected-test controller is parent-minted")
        if _origin not in {RUNTIME_VERIFIED_ORIGIN, INJECTED_TEST_ORIGIN}:
            raise ProvenanceError("authority_origin_mismatch")
        if revision_verifier_key is not None:
            if (type(revision_verifier_key) is not bytes
                    or len(revision_verifier_key) < 32
                    or not isinstance(revision_verifier_identity, str)
                    or not revision_verifier_identity):
                raise TypeError("external revision verifier capability is invalid")
        elif revision_verifier_identity is not None:
            raise TypeError("external revision verifier key is required")
        if _origin != RUNTIME_VERIFIED_ORIGIN and revision_verifier_key is not None:
            raise TypeError("injected controller cannot hold runtime verifier capability")
        self.__ownership_token = object()
        self.__qualification_mint_lock = threading.RLock()
        self.__final_mint_lock = threading.RLock()
        self.__run_auth_lock = threading.RLock()
        self.__minted = False
        self.__final_minted = False
        self.__qualification_claimed = False
        self.__final_claimed = False
        self.__run_auths: set[str] = set()
        self.__ledgers: dict[int, Any] = {}
        self.__ledger_keys: dict[int, bytes] = {}
        self.__ledger_keys_by_root: dict[str, bytes] = {}
        self.__authorities: dict[int, Any] = {}
        self.__active_qualification_authorities: dict[
            int, ActiveQualificationAuthority
        ] = {}
        self.__leases: dict[int, K12RetainedTargetLease] = {}
        self.__source_closures: dict[int, SourceClosure] = {}
        self.__capsules: dict[int, Any] = {}
        self.__external_revisions: dict[int, ExternalRevisionAuthorization] = {}
        self.__external_revision_identities: set[str] = set()
        self.__qualification_semantic_lock = threading.RLock()
        self.__qualification_terminalization_lock = threading.RLock()
        self.__qualification_semantic_sessions: dict[str, dict[str, Any]] = {}
        self.__qualification_coordinate_capabilities: dict[int, _QualificationCoordinateCapability] = {}
        self.__qualification_coordinate_observations: dict[
            int, tuple[QualificationCoordinateTerminalReceipt, NormalizedCell | NormalizedProbe]
        ] = {}
        self.__qualification_execution_stage_receipts: dict[
            int, QualificationExecutionStageReceipt
        ] = {}
        self.__qualification_execution_stage_identities: dict[int, str] = {}
        self.__qualification_execution_stage_observations: dict[
            int, Mapping[str, Any]
        ] = {}
        self.__qualification_execution_boundary_artifacts: dict[
            int, QualificationExecutionBoundaryArtifact
        ] = {}
        self.__qualification_execution_boundary_identities: dict[int, str] = {}
        self.__qualification_coordinate_execution_receipts: dict[
            int, tuple[
                QualificationCoordinateExecutionReceipt,
                NormalizedCell | NormalizedProbe | None,
                tuple[QualificationExecutionStageReceipt, ...],
            ]
        ] = {}
        self.__qualification_coordinate_execution_identities: dict[int, str] = {}
        self.__qualification_semantic_attestations: dict[int, QualificationSemanticAttestation] = {}
        self.__origin = _origin
        self.__owner = _owner
        self.__revision_verifier_key = revision_verifier_key
        self.__revision_verifier_identity = revision_verifier_identity
        self.__trusted_clock = _TrustedClock(
            injected=_origin == INJECTED_TEST_ORIGIN,
        )

    @property
    def origin(self) -> str:
        return self.__origin

    @property
    def runtime_admissible(self) -> bool:
        return self.origin == RUNTIME_VERIFIED_ORIGIN

    @property
    def is_injected_test_controller(self) -> bool:
        return self.origin == INJECTED_TEST_ORIGIN and self.__owner is not None

    @property
    def owner(self) -> Any:
        return self.__owner

    def current_time(self) -> int:
        """Return the parent-owned trusted time for a live boundary."""
        with self.__qualification_terminalization_lock:
            return self.__trusted_clock.now()

    trusted_now = current_time

    def _resolve_time(self, now: int | None) -> int:
        with self.__qualification_terminalization_lock:
            if now is None:
                return self.__trusted_clock.now()
            return self.__trusted_clock.observe_explicit(now)

    def validate_current_authority(
            self, authority: Any, *, now: int | None = None,
    ) -> bool:
        """Validate a parent-owned authority against trusted current time."""
        return self.authority_is_current(authority, now=now)

    def owns_ledger(self, ledger: Any) -> bool:
        """Authenticate a ledger through the public parent ownership surface."""
        owned = self.__ledgers.get(id(ledger))
        return owned is ledger and _ledger_is(
            ledger, namespace=getattr(ledger, "namespace", ""),
            state=getattr(ledger, "state", None),
        )

    def owns_authority(self, authority: Any) -> bool:
        return self.__authorities.get(id(authority)) is authority

    def owns_target_lease(self, lease: Any) -> bool:
        return self.__leases.get(id(lease)) is lease

    def owns_source_closure(self, closure: Any) -> bool:
        return self.__source_closures.get(id(closure)) is closure

    def owns_capsule(self, capsule: Any) -> bool:
        return self.__capsules.get(id(capsule)) is capsule

    def owns_external_revision(self, authorization: Any) -> bool:
        return self.__external_revisions.get(id(authorization)) is authorization

    def _owns_active_qualification_authority(
        self, authority: Any,
    ) -> bool:
        """Authenticate the exact active handle and its live activation."""

        if (
            not isinstance(authority, ActiveQualificationAuthority)
            or self.__active_qualification_authorities.get(id(authority)) is not authority
            or authority.owner is not self
            or authority.ownership_token is not self.__ownership_token
            or not self.owns_authority(authority.authority)
        ):
            return False
        try:
            lifecycle, activation = self.lifecycle_for(authority.authority.ledger)
        except (AttributeError, ProvenanceError):
            return False
        binding = authority.binding
        return (
            lifecycle == "active"
            and activation == authority.activation_digest
            and binding._ownership_token is self.__ownership_token
            and binding.authority_type == QUALIFICATION_AUTHORITY
            and binding.namespace == LIVE_QUALIFICATION_NAMESPACE
            and binding.authority_digest == authority.identity
            and binding.lifecycle == "active"
            and binding.reservation == authority.reservation_id
            and binding.activation == activation
            and binding.origin == authority.origin
        )

    def _boundary_artifact_for_stage(
        self, stage: QualificationExecutionStageReceipt,
    ) -> QualificationExecutionBoundaryArtifact | None:
        for artifact in self.__qualification_execution_boundary_artifacts.values():
            if (
                artifact.identity == getattr(stage, "boundary_artifact_identity", None)
                and _qualification_boundary_artifact_identity(artifact) == artifact.identity
                and self.__qualification_execution_boundary_identities.get(id(artifact))
                    == artifact.identity
                and artifact._owner is self
            ):
                return artifact
        return None

    def _discard_boundary_artifact_for_stage(
        self, stage: QualificationExecutionStageReceipt,
    ) -> None:
        for artifact_id, artifact in tuple(
            self.__qualification_execution_boundary_artifacts.items()
        ):
            if artifact.identity == getattr(stage, "boundary_artifact_identity", None):
                del self.__qualification_execution_boundary_artifacts[artifact_id]
                self.__qualification_execution_boundary_identities.pop(artifact_id, None)
                return

    def _discard_qualification_execution_scope(
        self, authority: ActiveQualificationAuthority,
        session: dict[str, Any],
        *,
        clear_registry: bool = True,
    ) -> None:
        """Discard every uncommitted execution object for one authority."""

        capability_ids = {
            id(value) for value in session.get("capabilities", {}).values()
        }
        terminal_ids = {
            id(value) for value in session.get("receipts", {}).values()
        }
        execution_ids = set(session.get("execution_object_ids", ()))
        stage_ids = set(session.get("stage_object_ids", ()))
        boundary_ids = set(session.get("boundary_object_ids", ()))
        for object_id in capability_ids:
            self.__qualification_coordinate_capabilities.pop(object_id, None)
        for object_id in terminal_ids:
            self.__qualification_coordinate_observations.pop(object_id, None)
        for object_id in execution_ids:
            self.__qualification_coordinate_execution_receipts.pop(object_id, None)
            self.__qualification_coordinate_execution_identities.pop(object_id, None)
        for object_id in stage_ids:
            self.__qualification_execution_stage_receipts.pop(object_id, None)
            self.__qualification_execution_stage_identities.pop(object_id, None)
            self.__qualification_execution_stage_observations.pop(object_id, None)
        for object_id in boundary_ids:
            self.__qualification_execution_boundary_artifacts.pop(object_id, None)
            self.__qualification_execution_boundary_identities.pop(object_id, None)
        session["execution_object_ids"].clear()
        session["stage_object_ids"].clear()
        session["boundary_object_ids"].clear()

        # Sweep dependent maps independently so an interrupted cleanup cannot
        # leave an orphaned identity, observation, or boundary artifact.
        for receipt_id in tuple(self.__qualification_coordinate_execution_identities):
            if receipt_id not in self.__qualification_coordinate_execution_receipts:
                del self.__qualification_coordinate_execution_identities[receipt_id]
        for stage_id in tuple(self.__qualification_execution_stage_identities):
            if stage_id not in self.__qualification_execution_stage_receipts:
                del self.__qualification_execution_stage_identities[stage_id]
        for stage_id in tuple(self.__qualification_execution_stage_observations):
            if stage_id not in self.__qualification_execution_stage_receipts:
                del self.__qualification_execution_stage_observations[stage_id]
        for artifact_id in tuple(self.__qualification_execution_boundary_identities):
            if artifact_id not in self.__qualification_execution_boundary_artifacts:
                del self.__qualification_execution_boundary_identities[artifact_id]

        session.get("capabilities", {}).clear()
        session.get("capability_identities", {}).clear()
        if clear_registry:
            session["receipts"].clear()
            session["receipt_identities"].clear()
            session["cells"].clear()
            session["probes"].clear()
            session["record_identities"].clear()

    def _issue_qualification_coordinate_capabilities(
        self, authority: ActiveQualificationAuthority,
    ) -> tuple[tuple[_QualificationCoordinateCapability, ...],
               tuple[_QualificationCoordinateCapability, ...]]:
        """Issue the exact one-shot 15-cell and P1--P4 publication set."""

        if (not isinstance(authority, ActiveQualificationAuthority)
                or authority.owner is not self
                or not self._owns_active_qualification_authority(authority)
                or not self.owns_authority(authority.authority)
                or not authority.current_at()
                or authority.lifecycle != "active"):
            raise ProvenanceError("authority_replay")
        with self.__qualification_semantic_lock:
            if authority.identity in self.__qualification_semantic_sessions:
                raise ProvenanceError("authority_replay")
            session: dict[str, Any] = {
                "authority": authority,
                "cells": {},
                "probes": {},
                "receipts": {},
                "receipt_identities": {},
                "record_identities": {},
                "capabilities": {},
                "capability_identities": {},
                "execution_object_ids": set(),
                "stage_object_ids": set(),
                "boundary_object_ids": set(),
                "probe_binding": authority.authority.binding(QUALIFICATION_PROBE_NAMESPACE),
                "execution_receipts_issued": False,
                "state": "collecting",
            }
            issued: list[_QualificationCoordinateCapability] = []
            for domain, coordinates in (
                (LIVE_QUALIFICATION_NAMESPACE, QUALIFICATION_SCHEDULE),
                (QUALIFICATION_PROBE_NAMESPACE, QUALIFICATION_PROBES),
            ):
                for coordinate in coordinates:
                    value = object.__new__(_QualificationCoordinateCapability)
                    body = {
                        "artifact": "minecraft-k12-qualification-coordinate-capability/1",
                        "authority": authority.identity,
                        "activation": authority.activation_digest,
                        "reservation": authority.reservation_id,
                        "domain": domain,
                        "coordinate": coordinate,
                        "nonce": secrets.token_hex(32),
                    }
                    for name, item in (
                        ("authority_digest", body["authority"]),
                        ("activation_digest", body["activation"]),
                        ("reservation_id", body["reservation"]),
                        ("domain", domain),
                        ("coordinate", coordinate),
                        ("nonce", body["nonce"]),
                        ("identity", canonical_sha256(body)),
                    ):
                        object.__setattr__(value, name, item)
                    session["capabilities"][(domain, coordinate)] = value
                    session["capability_identities"][(domain, coordinate)] = value.identity
                    self.__qualification_coordinate_capabilities[id(value)] = value
                    issued.append(value)
            self.__qualification_semantic_sessions[authority.identity] = session
            split = len(QUALIFICATION_SCHEDULE)
            return tuple(issued[:split]), tuple(issued[split:])

    def mint_injected_qualification_execution_receipts(
        self,
        authority: ActiveQualificationAuthority,
        cell_capabilities: tuple[_QualificationCoordinateCapability, ...],
        probe_capabilities: tuple[_QualificationCoordinateCapability, ...],
        *,
        cell_campaign_id: str,
        probe_campaign_id: str,
        execution_identity: str,
        failed_coordinate: str | None = None,
    ) -> tuple[
        tuple[QualificationCoordinateExecutionReceipt, ...],
        tuple[QualificationCoordinateExecutionReceipt, ...],
    ]:
        """Mint the deterministic injected-test receipt bundle only."""

        if self.origin != INJECTED_TEST_ORIGIN:
            raise ProvenanceError("authority_origin_mismatch")
        return self._mint_qualification_execution_receipts_transaction(
            authority,
            cell_capabilities,
            probe_capabilities,
            cell_campaign_id=cell_campaign_id,
            probe_campaign_id=probe_campaign_id,
            execution_identity=execution_identity,
            failed_coordinate=failed_coordinate,
        )

    def _mint_qualification_execution_receipts_transaction(
        self, *args: Any, **kwargs: Any,
    ) -> tuple[
        tuple[QualificationCoordinateExecutionReceipt, ...],
        tuple[QualificationCoordinateExecutionReceipt, ...],
    ]:
        """Atomically run either the injected or typed runtime adapter."""

        self.__qualification_semantic_lock.acquire()
        authority = kwargs.get("authority")
        if authority is None and args:
            authority = args[0]
        before_execution_receipts = dict(
            self.__qualification_coordinate_execution_receipts
        )
        before_execution_identities = dict(self.__qualification_coordinate_execution_identities)
        before_stages = dict(self.__qualification_execution_stage_receipts)
        before_stage_observations = dict(self.__qualification_execution_stage_observations)
        before_stage_identities = dict(self.__qualification_execution_stage_identities)
        before_boundaries = dict(self.__qualification_execution_boundary_artifacts)
        before_boundary_identities = dict(self.__qualification_execution_boundary_identities)
        session = self.__qualification_semantic_sessions.get(
            getattr(authority, "identity", "")
        )
        before_record_identities = (
            None if session is None else dict(session.get("record_identities", {}))
        )
        before_execution_object_ids = (
            None if session is None else set(session.get("execution_object_ids", ()))
        )
        before_stage_object_ids = (
            None if session is None else set(session.get("stage_object_ids", ()))
        )
        before_boundary_object_ids = (
            None if session is None else set(session.get("boundary_object_ids", ()))
        )
        before_execution_receipts_issued = (
            None if session is None else session.get("execution_receipts_issued")
        )
        try:
            return self._mint_qualification_execution_receipts_impl(*args, **kwargs)
        except BaseException:
            self.__qualification_coordinate_execution_receipts.clear()
            self.__qualification_coordinate_execution_receipts.update(
                before_execution_receipts
            )
            self.__qualification_coordinate_execution_identities.clear()
            self.__qualification_coordinate_execution_identities.update(
                before_execution_identities
            )
            self.__qualification_execution_stage_receipts.clear()
            self.__qualification_execution_stage_receipts.update(before_stages)
            self.__qualification_execution_stage_identities.clear()
            self.__qualification_execution_stage_identities.update(before_stage_identities)
            self.__qualification_execution_stage_observations.clear()
            self.__qualification_execution_stage_observations.update(
                before_stage_observations
            )
            self.__qualification_execution_boundary_artifacts.clear()
            self.__qualification_execution_boundary_artifacts.update(before_boundaries)
            self.__qualification_execution_boundary_identities.clear()
            self.__qualification_execution_boundary_identities.update(
                before_boundary_identities
            )
            if session is not None:
                session["execution_receipts_issued"] = before_execution_receipts_issued
                session["record_identities"] = before_record_identities or {}
                session["execution_object_ids"] = before_execution_object_ids or set()
                session["stage_object_ids"] = before_stage_object_ids or set()
                session["boundary_object_ids"] = before_boundary_object_ids or set()
            raise
        finally:
            self.__qualification_semantic_lock.release()

    def _mint_qualification_execution_receipts_impl(
        self,
        authority: ActiveQualificationAuthority,
        cell_capabilities: tuple[_QualificationCoordinateCapability, ...],
        probe_capabilities: tuple[_QualificationCoordinateCapability, ...],
        *,
        cell_campaign_id: str,
        probe_campaign_id: str,
        execution_identity: str,
        failed_coordinate: str | None = None,
        _boundary_artifacts: tuple[
            tuple[tuple[QualificationExecutionBoundaryArtifact, ...], ...],
            tuple[QualificationExecutionBoundaryArtifact, ...],
        ] | None = None,
    ) -> tuple[
        tuple[QualificationCoordinateExecutionReceipt, ...],
        tuple[QualificationCoordinateExecutionReceipt, ...],
    ]:
        """Mint receipts for the deterministic harness or an artifact adapter.

        The only selectable outcome is one named deterministic failure used to
        exercise durable fail-closed behavior.  Arbitrary semantic values and
        normalized records are deliberately not accepted. Runtime parents
        cannot use this inert harness; their execution adapter must submit the
        Runtime authorities are accepted only when ``_boundary_artifacts`` is
        supplied; they can never take the deterministic fallback below.
        """

        if self.origin not in {INJECTED_TEST_ORIGIN, RUNTIME_VERIFIED_ORIGIN}:
            raise ProvenanceError("authority_origin_mismatch")
        if self.origin == RUNTIME_VERIFIED_ORIGIN and _boundary_artifacts is None:
            raise ProvenanceError("authority_origin_mismatch")
        expected_evidence_origin = (
            INJECTED_FAKE_ORIGIN
            if self.origin == INJECTED_TEST_ORIGIN else RUNTIME_VERIFIED_ORIGIN
        )
        if (
            not isinstance(authority, ActiveQualificationAuthority)
            or authority.owner is not self
            or not self._owns_active_qualification_authority(authority)
        ):
            raise ProvenanceError("authority_replay")
        if (
            type(cell_capabilities) is not tuple
            or type(probe_capabilities) is not tuple
            or tuple(capability.coordinate for capability in cell_capabilities)
                != QUALIFICATION_SCHEDULE
            or tuple(capability.coordinate for capability in probe_capabilities)
                != QUALIFICATION_PROBES
            or not all(
                isinstance(value, str) and value
                for value in (cell_campaign_id, probe_campaign_id, execution_identity)
            )
            or cell_campaign_id == probe_campaign_id
            or (
                failed_coordinate is not None
                and failed_coordinate not in {
                    *QUALIFICATION_SCHEDULE, *QUALIFICATION_PROBES,
                }
            )
            or (_boundary_artifacts is not None and failed_coordinate is not None)
        ):
            raise ProvenanceError("final_prerequisite_mismatch")
        if _boundary_artifacts is not None:
            boundary_cells, boundary_probes = _boundary_artifacts
            if (
                type(boundary_cells) is not tuple
                or type(boundary_probes) is not tuple
                or len(boundary_cells) != len(QUALIFICATION_SCHEDULE)
                or len(boundary_probes) != len(QUALIFICATION_PROBES)
                or any(type(value) is not tuple for value in boundary_cells)
                or any(
                    not isinstance(value, QualificationExecutionBoundaryArtifact)
                    for value in boundary_probes
                )
            ):
                raise ProvenanceError("final_prerequisite_mismatch")
        with self.__qualification_semantic_lock:
            session = self.__qualification_semantic_sessions.get(authority.identity)
            if (
                session is None
                or session["state"] != "collecting"
                or session["execution_receipts_issued"] is True
                or not authority.current_at()
                or any(
                    session["capabilities"].get((capability.domain, capability.coordinate))
                        is not capability
                    for capability in (*cell_capabilities, *probe_capabilities)
                )
            ):
                raise ProvenanceError("authority_replay")
            session["execution_receipts_issued"] = True

            def mint_stage(
                capability: _QualificationCoordinateCapability,
                campaign_id: str,
                stage_name: str,
                predecessor_identity: str,
                observation: Mapping[str, Any],
                boundary_artifact: QualificationExecutionBoundaryArtifact | None = None,
            ) -> QualificationExecutionStageReceipt:
                if boundary_artifact is None:
                    snapshot = MappingProxyType(dict(observation))
                    boundary_type = _QUALIFICATION_BOUNDARY_ARTIFACT_TYPES[stage_name]
                    boundary = object.__new__(boundary_type)
                    boundary_values = {
                        "authority_digest": authority.identity,
                        "activation_digest": authority.activation_digest,
                        "reservation_id": authority.reservation_id,
                        "profile_digest": authority.profile_digest,
                        "evidence_origin": expected_evidence_origin,
                        "campaign_id": campaign_id,
                        "domain": capability.domain,
                        "coordinate": capability.coordinate,
                        "stage": stage_name,
                        "values": snapshot,
                        "_owner": self,
                    }
                    for name, value in boundary_values.items():
                        object.__setattr__(boundary, name, value)
                    object.__setattr__(
                        boundary,
                        "identity",
                        _qualification_boundary_artifact_identity(boundary),
                    )
                    self.__qualification_execution_boundary_artifacts[id(boundary)] = boundary
                    self.__qualification_execution_boundary_identities[id(boundary)] = (
                        boundary.identity
                    )
                    session["boundary_object_ids"].add(id(boundary))
                else:
                    boundary = boundary_artifact
                    if not isinstance(boundary, QualificationExecutionBoundaryArtifact):
                        raise ProvenanceError("authority_replay")
                    snapshot = boundary.values
                    if (
                        type(boundary) is not _QUALIFICATION_BOUNDARY_ARTIFACT_TYPES[stage_name]
                        or self.__qualification_execution_boundary_artifacts.get(id(boundary))
                            is not boundary
                        or self.__qualification_execution_boundary_identities.get(
                            id(boundary)
                        ) != boundary.identity
                        or boundary._owner is not self
                        or _qualification_boundary_artifact_identity(boundary)
                            != boundary.identity
                        or boundary.authority_digest != authority.identity
                        or boundary.activation_digest != authority.activation_digest
                        or boundary.reservation_id != authority.reservation_id
                        or boundary.profile_digest != authority.profile_digest
                        or boundary.evidence_origin != expected_evidence_origin
                        or boundary.campaign_id != campaign_id
                        or boundary.domain != capability.domain
                        or boundary.coordinate != capability.coordinate
                        or boundary.stage != stage_name
                        or set(boundary.values) != _QUALIFICATION_BOUNDARY_REQUIRED_FIELDS[stage_name]
                        or dict(boundary.values) != dict(observation)
                    ):
                        raise ProvenanceError("authority_replay")
                    session["boundary_object_ids"].add(id(boundary))
                receipt = object.__new__(QualificationExecutionStageReceipt)
                values = {
                    "authority_digest": authority.identity,
                    "activation_digest": authority.activation_digest,
                    "reservation_id": authority.reservation_id,
                    "profile_digest": authority.profile_digest,
                    "evidence_origin": expected_evidence_origin,
                    "campaign_id": campaign_id,
                    "domain": capability.domain,
                    "coordinate": capability.coordinate,
                    "stage": stage_name,
                    "coordinate_capability_identity": capability.identity,
                    "predecessor_identity": predecessor_identity,
                    "boundary_artifact_identity": boundary.identity,
                    "observation_digest": canonical_sha256({
                        "stage": stage_name, "observation": dict(snapshot),
                    }),
                    "_owner": self,
                }
                for name, value in values.items():
                    object.__setattr__(receipt, name, value)
                object.__setattr__(
                    receipt, "identity", _execution_stage_receipt_identity(receipt),
                )
                self.__qualification_execution_stage_receipts[id(receipt)] = receipt
                self.__qualification_execution_stage_identities[id(receipt)] = receipt.identity
                self.__qualification_execution_stage_observations[id(receipt)] = snapshot
                session["stage_object_ids"].add(id(receipt))
                return receipt

            def normalized_from_stages(
                capability: _QualificationCoordinateCapability,
                campaign_id: str,
                stages: tuple[QualificationExecutionStageReceipt, ...],
            ) -> NormalizedCell | NormalizedProbe:
                facts = {
                    stage.stage: (
                        self._boundary_artifact_for_stage(stage).values
                        if self._boundary_artifact_for_stage(stage) is not None
                        else {}
                    )
                    for stage in stages
                }
                if capability.domain == QUALIFICATION_PROBE_NAMESPACE:
                    containment = facts["probe_containment"]
                    return NormalizedProbe(
                        probe=capability.coordinate,
                        authority=authority.identity,
                        activation=authority.activation_digest,
                        profile_digest=authority.profile_digest,
                        evidence_origin=expected_evidence_origin,
                        campaign_id=campaign_id,
                        evidence_digest=containment["evidence_digest"],
                        passed=containment["passed"],
                        terminal_verified=containment["terminal_verified"],
                        execution_provenance=QUALIFICATION_PROBE_NAMESPACE,
                    )
                reset, provider, permit, effect, oracle, containment = (
                    facts[name] for name in (
                        "reset", "provider", "permit", "effect", "oracle",
                        "containment",
                    )
                )
                arm = capability.coordinate.rsplit("-", 1)[-1]
                rejection = None
                if arm == "S":
                    rejection = NormalizedRejectionBinding(
                        arm="S", cell_id=capability.coordinate,
                        profile_digest=authority.profile_digest,
                        campaign_id=campaign_id,
                        authority=authority.identity,
                        activation=authority.activation_digest,
                        evidence_origin=expected_evidence_origin,
                        reset_token=reset["reset_token"],
                        generation=reset["generation"],
                        request_identity=permit["request_identity"],
                        permit_identity=permit["permit_identity"],
                        effect_identity=effect["effect_identity"],
                        evidence_digest=oracle["evidence_digest"],
                        current_inadmissible=effect["current_inadmissible"],
                        native_entries=effect["native_entries"],
                    )
                return NormalizedCell(
                    cell_id=capability.coordinate,
                    authority=authority.identity,
                    activation=authority.activation_digest,
                    profile_digest=authority.profile_digest,
                    evidence_origin=expected_evidence_origin,
                    campaign_id=campaign_id,
                    reset_identity=reset["reset_identity"],
                    evidence_digest=oracle["evidence_digest"],
                    fresh_root=reset["fresh_root"],
                    reset_passed=reset["reset_passed"],
                    capability_state=effect["capability_state"],
                    provider_terminal=provider["terminal"],
                    oracle_value=oracle["value"],
                    containment_clean=containment["clean"],
                    evidence_valid=containment["evidence_valid"],
                    terminal_verified=containment["terminal_verified"],
                    rejection_verified=oracle["rejection_verified"],
                    execution_provenance=LIVE_QUALIFICATION_NAMESPACE,
                    retry=effect["retry"],
                    resumed=effect["resumed"],
                    replacement=effect["replacement"],
                    rejection_binding=rejection,
                    reset_token=reset["reset_token"] if arm == "S" else None,
                    generation=reset["generation"] if arm == "S" else None,
                    request_identity=permit["request_identity"] if arm == "S" else None,
                    permit_identity=permit["permit_identity"] if arm == "S" else None,
                    effect_identity=effect["effect_identity"] if arm == "S" else None,
                )

            def normalized_from_receipt(
                capability: _QualificationCoordinateCapability,
                campaign_id: str,
                receipt: QualificationCoordinateExecutionReceipt,
            ) -> NormalizedCell | NormalizedProbe:
                owned = self.__qualification_coordinate_execution_receipts.get(
                    id(receipt)
                )
                if (
                    owned is None
                    or owned[0] is not receipt
                    or receipt._owner is not self
                    or _coordinate_execution_receipt_identity(receipt)
                    != receipt.identity
                ):
                    raise ProvenanceError("authority_replay")
                return normalized_from_stages(capability, campaign_id, owned[2])

            def close_coordinate(
                capability: _QualificationCoordinateCapability,
                campaign_id: str,
                stages: tuple[QualificationExecutionStageReceipt, ...],
            ) -> QualificationCoordinateExecutionReceipt:
                expected_names = (
                    _QUALIFICATION_CELL_STAGE_NAMES
                    if capability.domain == LIVE_QUALIFICATION_NAMESPACE
                    else _QUALIFICATION_PROBE_STAGE_NAMES
                )
                predecessor = ""
                for stage, expected_name in zip(stages, expected_names):
                    observation = self.__qualification_execution_stage_observations.get(
                        id(stage)
                    )
                    boundary = self._boundary_artifact_for_stage(stage)
                    if (
                        len(stages) != len(expected_names)
                        or stage.stage != expected_name
                        or stage.predecessor_identity != predecessor
                        or stage.authority_digest != authority.identity
                        or stage.activation_digest != authority.activation_digest
                        or stage.reservation_id != authority.reservation_id
                        or stage.profile_digest != authority.profile_digest
                        or stage.evidence_origin != expected_evidence_origin
                        or stage.campaign_id != campaign_id
                        or stage.domain != capability.domain
                        or stage.coordinate != capability.coordinate
                        or stage.coordinate_capability_identity != capability.identity
                        or self.__qualification_execution_stage_receipts.get(id(stage)) is not stage
                        or boundary is None
                        or type(boundary) is not _QUALIFICATION_BOUNDARY_ARTIFACT_TYPES[expected_name]
                        or boundary.stage != expected_name
                        or boundary.authority_digest != authority.identity
                        or boundary.activation_digest != authority.activation_digest
                        or boundary.reservation_id != authority.reservation_id
                        or boundary.profile_digest != authority.profile_digest
                        or boundary.evidence_origin != expected_evidence_origin
                        or boundary.campaign_id != campaign_id
                        or boundary.domain != capability.domain
                        or boundary.coordinate != capability.coordinate
                        or boundary.values != observation
                        or observation is None
                        or stage.observation_digest != canonical_sha256({
                            "stage": stage.stage, "observation": dict(observation),
                        })
                        or _execution_stage_receipt_identity(stage) != stage.identity
                    ):
                        raise ProvenanceError("final_prerequisite_mismatch")
                    predecessor = stage.identity
                receipt = object.__new__(QualificationCoordinateExecutionReceipt)
                values = {
                    "authority_digest": authority.identity,
                    "activation_digest": authority.activation_digest,
                    "reservation_id": authority.reservation_id,
                    "profile_digest": authority.profile_digest,
                    "evidence_origin": expected_evidence_origin,
                    "campaign_id": campaign_id,
                    "domain": capability.domain,
                    "coordinate": capability.coordinate,
                    "coordinate_capability_identity": capability.identity,
                    "stage_receipt_identities": tuple(stage.identity for stage in stages),
                    "_owner": self,
                }
                for name, value in values.items():
                    object.__setattr__(receipt, name, value)
                object.__setattr__(
                    receipt, "identity", _coordinate_execution_receipt_identity(receipt),
                )
                self.__qualification_coordinate_execution_receipts[id(receipt)] = (
                    receipt, None, stages,
                )
                self.__qualification_coordinate_execution_identities[id(receipt)] = (
                    receipt.identity
                )
                session["execution_object_ids"].add(id(receipt))
                try:
                    normalized = normalized_from_receipt(capability, campaign_id, receipt)
                except BaseException:
                    self.__qualification_coordinate_execution_receipts.pop(
                        id(receipt), None,
                    )
                    self.__qualification_coordinate_execution_identities.pop(
                        id(receipt), None,
                    )
                    session["execution_object_ids"].discard(id(receipt))
                    raise
                self.__qualification_coordinate_execution_receipts[id(receipt)] = (
                    receipt, normalized, stages,
                )
                session["record_identities"][(capability.domain, capability.coordinate)] = (
                    normalized.identity
                )
                return receipt

            cell_receipts: list[QualificationCoordinateExecutionReceipt] = []
            for ordinal, (cell_id, capability) in enumerate(
                zip(QUALIFICATION_SCHEDULE, cell_capabilities), 1
            ):
                arm = cell_id.rsplit("-", 1)[-1]
                evidence_digest = hashlib.sha256(
                    f"{execution_identity}:qualification:{ordinal}".encode("utf-8")
                ).hexdigest()
                reset_identity = f"reset-{execution_identity}-{ordinal}"
                request_identity = (
                    f"request-{execution_identity}-{ordinal}" if arm == "S" else None
                )
                permit_identity = (
                    f"permit-{execution_identity}-{ordinal}" if arm == "S" else None
                )
                effect_identity = (
                    f"effect-{execution_identity}-{ordinal}" if arm == "S" else None
                )
                failed = failed_coordinate == cell_id
                supplied_boundaries = (
                    None
                    if _boundary_artifacts is None
                    else _boundary_artifacts[0][ordinal - 1]
                )
                if supplied_boundaries is not None:
                    if (
                        type(supplied_boundaries) is not tuple
                        or len(supplied_boundaries) != len(_QUALIFICATION_CELL_STAGE_NAMES)
                        or any(
                            not isinstance(value, QualificationExecutionBoundaryArtifact)
                            for value in supplied_boundaries
                        )
                    ):
                        raise ProvenanceError("final_prerequisite_mismatch")
                    observations = tuple(
                        (value.stage, value.values) for value in supplied_boundaries
                    )
                else:
                    observations = (
                        ("reset", {"fresh_root": True, "reset_passed": True,
                                   "reset_identity": reset_identity,
                                   "reset_token": reset_identity, "generation": 1}),
                        ("provider", {"terminal": "success", "attempts": 1}),
                        ("permit", {"request_identity": request_identity,
                                    "permit_identity": permit_identity}),
                        ("effect", {"capability_state": "REVOKED",
                                     "retry": False, "resumed": False,
                                     "replacement": False,
                                     "effect_identity": effect_identity,
                                     "current_inadmissible": arm == "S",
                                     "native_entries": 0}),
                        ("oracle", {"value": (
                                        "unknown" if failed
                                        else "not_applicable" if arm == "S" else "true"
                                    ),
                                     "evidence_digest": evidence_digest,
                                     "rejection_verified": arm != "S" or not failed}),
                        ("containment", {"clean": True, "evidence_valid": True,
                                          "terminal_verified": True}),
                    )
                boundary_sources = (
                    tuple(value for value in supplied_boundaries)
                    if supplied_boundaries is not None else (None,) * len(observations)
                )
                stages: list[QualificationExecutionStageReceipt] = []
                predecessor = ""
                for (stage_name, observation), boundary_source in zip(
                    observations, boundary_sources,
                ):
                    stage = mint_stage(
                        capability, cell_campaign_id, stage_name, predecessor,
                        observation, boundary_source,
                    )
                    stages.append(stage)
                    predecessor = stage.identity
                cell_receipts.append(close_coordinate(
                    capability, cell_campaign_id, tuple(stages),
                ))
            probe_receipts: list[QualificationCoordinateExecutionReceipt] = []
            for probe, capability in zip(QUALIFICATION_PROBES, probe_capabilities):
                evidence_digest = hashlib.sha256(
                    f"{execution_identity}:probe:{probe}".encode("utf-8")
                ).hexdigest()
                supplied_probe = (
                    None
                    if _boundary_artifacts is None
                    else _boundary_artifacts[1][len(probe_receipts)]
                )
                if _boundary_artifacts is not None:
                    if (
                        supplied_probe is None
                        or type(supplied_probe)
                        is not QualificationProbeContainmentBoundaryArtifact
                    ):
                        raise ProvenanceError("final_prerequisite_mismatch")
                    probe_observation = supplied_probe.values
                else:
                    probe_observation = {
                        "passed": failed_coordinate != probe,
                        "terminal_verified": True,
                        "evidence_digest": evidence_digest,
                    }
                stage = mint_stage(
                    capability, probe_campaign_id, "probe_containment", "",
                    probe_observation, supplied_probe,
                )
                probe_receipts.append(close_coordinate(
                    capability, probe_campaign_id, (stage,),
                ))
            return tuple(cell_receipts), tuple(probe_receipts)

    def mint_runtime_qualification_execution_receipts(
        self,
        authority: ActiveQualificationAuthority,
        cell_capabilities: tuple[_QualificationCoordinateCapability, ...],
        probe_capabilities: tuple[_QualificationCoordinateCapability, ...],
        cell_boundary_artifacts: tuple[
            tuple[QualificationExecutionBoundaryArtifact, ...], ...
        ],
        probe_boundary_artifacts: tuple[QualificationExecutionBoundaryArtifact, ...],
        *,
        cell_campaign_id: str,
        probe_campaign_id: str,
    ) -> tuple[
        tuple[QualificationCoordinateExecutionReceipt, ...],
        tuple[QualificationCoordinateExecutionReceipt, ...],
    ]:
        """Adapt already-authenticated runtime boundary outputs.

        This method is an authority adapter, not an executor: it performs no
        Minecraft, provider, process, network, or containment I/O. Concrete
        runtime boundaries must first parent-mint the typed artifacts; scalar
        evidence fields and origin strings are never accepted here.
        """

        if self.origin != RUNTIME_VERIFIED_ORIGIN:
            raise ProvenanceError("authority_origin_mismatch")
        if (
            type(cell_boundary_artifacts) is not tuple
            or type(probe_boundary_artifacts) is not tuple
        ):
            raise ProvenanceError("final_prerequisite_mismatch")
        return self.mint_qualification_execution_receipts_from_boundary_artifacts(
            authority,
            cell_capabilities,
            probe_capabilities,
            cell_boundary_artifacts,
            probe_boundary_artifacts,
            cell_campaign_id=cell_campaign_id,
            probe_campaign_id=probe_campaign_id,
        )

    def mint_qualification_execution_receipts_from_boundary_artifacts(
        self,
        authority: ActiveQualificationAuthority,
        cell_capabilities: tuple[_QualificationCoordinateCapability, ...],
        probe_capabilities: tuple[_QualificationCoordinateCapability, ...],
        cell_boundary_artifacts: tuple[
            tuple[QualificationExecutionBoundaryArtifact, ...], ...
        ],
        probe_boundary_artifacts: tuple[QualificationExecutionBoundaryArtifact, ...],
        *,
        cell_campaign_id: str,
        probe_campaign_id: str,
    ) -> tuple[
        tuple[QualificationCoordinateExecutionReceipt, ...],
        tuple[QualificationCoordinateExecutionReceipt, ...],
    ]:
        """Consume a complete parent-owned boundary artifact bundle.

        The bundle is the integration point for concrete reset/provider/
        permit/effect/oracle/containment adapters.  This method accepts no
        semantic scalar or normalized record and does not perform execution.
        """

        return self._mint_qualification_execution_receipts_transaction(
            authority,
            cell_capabilities,
            probe_capabilities,
            cell_campaign_id=cell_campaign_id,
            probe_campaign_id=probe_campaign_id,
            execution_identity="boundary-artifact-adapter",
            _boundary_artifacts=(cell_boundary_artifacts, probe_boundary_artifacts),
        )

    def _validate_qualification_execution_pair(
        self,
        authority: ActiveQualificationAuthority,
        capability: _QualificationCoordinateCapability,
        execution_receipt: QualificationCoordinateExecutionReceipt,
    ) -> _ValidatedQualificationExecution:
        session = self.__qualification_semantic_sessions.get(authority.identity)
        owned_execution = self.__qualification_coordinate_execution_receipts.get(
            id(execution_receipt)
        )
        expected = None if session is None else session["capabilities"].get(
            (getattr(capability, "domain", None), getattr(capability, "coordinate", None))
        )
        stages = () if owned_execution is None else owned_execution[2]
        expected_stage_names = (
            _QUALIFICATION_CELL_STAGE_NAMES
            if getattr(capability, "domain", None) == LIVE_QUALIFICATION_NAMESPACE
            else _QUALIFICATION_PROBE_STAGE_NAMES
        )
        predecessor = ""
        stages_valid = len(stages) == len(expected_stage_names)
        for stage, expected_name in zip(stages, expected_stage_names):
            observation = self.__qualification_execution_stage_observations.get(id(stage))
            boundary = self._boundary_artifact_for_stage(stage)
            stages_valid = stages_valid and (
                self.__qualification_execution_stage_receipts.get(id(stage)) is stage
                and self.__qualification_execution_stage_identities.get(id(stage))
                    == stage.identity
                and stage._owner is self
                and stage.stage == expected_name
                and stage.predecessor_identity == predecessor
                and stage.authority_digest == authority.identity
                and stage.activation_digest == authority.activation_digest
                and stage.reservation_id == authority.reservation_id
                and stage.profile_digest == authority.profile_digest
                and stage.evidence_origin == execution_receipt.evidence_origin
                and stage.campaign_id == execution_receipt.campaign_id
                and stage.domain == capability.domain
                and stage.coordinate == capability.coordinate
                and stage.coordinate_capability_identity == capability.identity
                and boundary is not None
                and type(boundary) is _QUALIFICATION_BOUNDARY_ARTIFACT_TYPES[expected_name]
                and boundary.stage == expected_name
                and boundary.authority_digest == authority.identity
                and boundary.activation_digest == authority.activation_digest
                and boundary.reservation_id == authority.reservation_id
                and boundary.profile_digest == authority.profile_digest
                and boundary.evidence_origin == execution_receipt.evidence_origin
                and boundary.campaign_id == execution_receipt.campaign_id
                and boundary.domain == capability.domain
                and boundary.coordinate == capability.coordinate
                and boundary.values == observation
                and observation is not None
                and stage.observation_digest == canonical_sha256({
                    "stage": stage.stage, "observation": dict(observation or {}),
                })
                and _execution_stage_receipt_identity(stage) == stage.identity
            )
            predecessor = stage.identity
        if (session is None or session["state"] != "collecting"
                or not self._owns_active_qualification_authority(authority)
                or session["authority"] is not authority
                or expected is not capability
                or self.__qualification_coordinate_capabilities.get(id(capability)) is not capability
                or capability.identity != session["capability_identities"].get(
                    (capability.domain, capability.coordinate)
                )
                or _coordinate_capability_identity(capability) != capability.identity
                or capability.authority_digest != authority.identity
                or capability.activation_digest != authority.activation_digest
                or capability.reservation_id != authority.reservation_id
                or owned_execution is None
                or owned_execution[0] is not execution_receipt
                or type(execution_receipt) is not QualificationCoordinateExecutionReceipt
                or execution_receipt._owner is not self
                or _coordinate_execution_receipt_identity(execution_receipt)
                    != execution_receipt.identity
                or execution_receipt.authority_digest != authority.identity
                or execution_receipt.activation_digest != authority.activation_digest
                or execution_receipt.reservation_id != authority.reservation_id
                or execution_receipt.profile_digest != authority.profile_digest
                 or execution_receipt.domain != capability.domain
                or execution_receipt.coordinate != capability.coordinate
                or execution_receipt.coordinate_capability_identity != capability.identity
                 or tuple(stage.identity for stage in stages)
                     != execution_receipt.stage_receipt_identities
                 or self.__qualification_coordinate_execution_identities.get(
                     id(execution_receipt)
                 ) != execution_receipt.identity
                 or not stages_valid
                or not authority.current_at()
                or authority.lifecycle != "active"):
             raise ProvenanceError("authority_replay")
        record = owned_execution[1]
        try:
            record_snapshot = replace(record)
        except (TypeError, ValueError, AttributeError):
            raise ProvenanceError("authority_replay")
        record_identity = session.get("record_identities", {}).get(
            (capability.domain, capability.coordinate)
        )
        if capability.domain == LIVE_QUALIFICATION_NAMESPACE:
            target = session["cells"]
            valid = type(record) is NormalizedCell and record.cell_id == capability.coordinate
        elif capability.domain == QUALIFICATION_PROBE_NAMESPACE:
            target = session["probes"]
            valid = type(record) is NormalizedProbe and record.probe == capability.coordinate
        else:
            target = {}
            valid = False
        if (not valid or capability.coordinate in target
                or record.authority != authority.identity
                or record.activation != authority.activation_digest
                or record.profile_digest != authority.profile_digest
                or record.evidence_origin != execution_receipt.evidence_origin):
            raise ProvenanceError("final_prerequisite_mismatch")
        if (
            record_identity != record.identity
            or record_snapshot.identity != record.identity
        ):
            raise ProvenanceError("authority_replay")
        return _ValidatedQualificationExecution(
            session=session,
            capability=capability,
            execution_receipt=execution_receipt,
            record=replace(record),
            stages=stages,
            execution_receipt_identity=execution_receipt.identity,
            execution_stage_receipt_identities=tuple(stage.identity for stage in stages),
            domain=capability.domain,
            coordinate=capability.coordinate,
            capability_identity=capability.identity,
            record_identity=record_identity,
        )

    def _make_qualification_terminal_receipt(
        self,
        authority: ActiveQualificationAuthority,
        validated: _ValidatedQualificationExecution,
    ) -> QualificationCoordinateTerminalReceipt:
        """Build a terminal receipt only from an immutable validation snapshot."""

        receipt = object.__new__(QualificationCoordinateTerminalReceipt)
        receipt_body = {
            "artifact": "minecraft-k12-qualification-coordinate-terminal-receipt/2",
            "authority": authority.identity,
            "activation": authority.activation_digest,
            "reservation": authority.reservation_id,
            "domain": validated.domain,
            "coordinate": validated.coordinate,
            "capability": validated.capability_identity,
            "record": validated.record_identity,
            "execution_receipt": validated.execution_receipt_identity,
            "execution_stages": list(validated.execution_stage_receipt_identities),
        }
        for name, value in (
            ("authority_digest", authority.identity),
            ("activation_digest", authority.activation_digest),
            ("reservation_id", authority.reservation_id),
            ("domain", validated.domain),
            ("coordinate", validated.coordinate),
            ("capability_identity", validated.capability_identity),
            ("record_identity", validated.record_identity),
            ("execution_receipt_identity", validated.execution_receipt_identity),
            ("execution_stage_receipt_identities",
             validated.execution_stage_receipt_identities),
            ("identity", canonical_sha256(receipt_body)),
            ("_owner", self),
        ):
            object.__setattr__(receipt, name, value)
        return receipt

    def _register_qualification_terminal(
        self,
        session: dict[str, Any],
        domain: str,
        coordinate: str,
        receipt: QualificationCoordinateTerminalReceipt,
        record: NormalizedCell | NormalizedProbe,
    ) -> None:
        """Record the parent observation before moving it into the registry."""

        key = (domain, coordinate)
        if session["receipts"].get(key) is not None:
            raise ProvenanceError("authority_replay")
        session["receipts"][key] = receipt
        session["receipt_identities"][key] = receipt.identity
        self.__qualification_coordinate_observations[id(receipt)] = (
            receipt, record,
        )

    def _commit_registered_qualification_terminal(
        self,
        session: dict[str, Any],
        domain: str,
        coordinate: str,
        receipt: QualificationCoordinateTerminalReceipt,
        record: NormalizedCell | NormalizedProbe,
    ) -> None:
        """Move one already-registered parent observation into its registry."""

        key = (domain, coordinate)
        observed = self.__qualification_coordinate_observations.get(id(receipt))
        if (
            session["receipts"].get(key) is not receipt
            or observed != (receipt, record)
        ):
            raise ProvenanceError("authority_replay")
        target = session["cells"] if domain == LIVE_QUALIFICATION_NAMESPACE else (
            session["probes"] if domain == QUALIFICATION_PROBE_NAMESPACE else None
        )
        if target is None or coordinate in target:
            raise ProvenanceError("final_prerequisite_mismatch")
        target[coordinate] = record
        del self.__qualification_coordinate_observations[id(receipt)]

    def _observe_qualification_coordinate_terminal(
        self,
        authority: ActiveQualificationAuthority,
        capability: _QualificationCoordinateCapability,
        execution_receipt: QualificationCoordinateExecutionReceipt,
    ) -> QualificationCoordinateTerminalReceipt:
        """Consume a capability plus an owned execution receipt into a terminal."""

        if not isinstance(authority, ActiveQualificationAuthority) or authority.owner is not self:
            raise TypeError("active parent qualification authority required")
        with self.__qualification_semantic_lock:
            validated = self._validate_qualification_execution_pair(
                authority, capability, execution_receipt,
            )
            receipt = self._make_qualification_terminal_receipt(authority, validated)
            self._register_qualification_terminal(
                validated.session,
                validated.domain,
                validated.coordinate,
                receipt,
                validated.record,
            )
            del self.__qualification_coordinate_capabilities[id(validated.capability)]
            del self.__qualification_coordinate_execution_receipts[
                id(validated.execution_receipt)
            ]
            self.__qualification_coordinate_execution_identities.pop(
                id(validated.execution_receipt), None,
            )
            for stage in validated.stages:
                del self.__qualification_execution_stage_receipts[id(stage)]
                self.__qualification_execution_stage_identities.pop(id(stage), None)
                del self.__qualification_execution_stage_observations[id(stage)]
                self._discard_boundary_artifact_for_stage(stage)
            return receipt

    def _publish_qualification_terminal_batch(
        self,
        authority: ActiveQualificationAuthority,
        pairs: tuple[
            tuple[_QualificationCoordinateCapability, QualificationCoordinateExecutionReceipt],
            ...,
        ],
        *,
        on_failure: Any = None,
        on_success: Any = None,
    ) -> QualificationVerdict:
        """Project, durably handle failure, then commit one complete census.

        ``on_failure`` is called while the semantic lock is held, before any
        capability or receipt is consumed.  The failed census and verdict are
        staged in the session before the callback so a durable failed event can
        be recovered even if the callback is interrupted after its append.
        """

        expected = tuple(
            (domain, coordinate)
            for domain, coordinates in (
                (LIVE_QUALIFICATION_NAMESPACE, QUALIFICATION_SCHEDULE),
                (QUALIFICATION_PROBE_NAMESPACE, QUALIFICATION_PROBES),
            )
            for coordinate in coordinates
        )
        if type(pairs) is not tuple or tuple(
            (getattr(capability, "domain", None), getattr(capability, "coordinate", None))
            for capability, _receipt in pairs
        ) != expected:
            raise ProvenanceError("final_prerequisite_mismatch")
        with self.__qualification_semantic_lock:
            validated = tuple(
                self._validate_qualification_execution_pair(
                    authority, capability, execution_receipt,
                )
                for capability, execution_receipt in pairs
            )
            census = QualificationCensus(
                tuple(
                    item.record for item in validated
                    if item.domain == LIVE_QUALIFICATION_NAMESPACE
                ),
                tuple(
                    item.record for item in validated
                    if item.domain == QUALIFICATION_PROBE_NAMESPACE
                ),
            )
            verdict = verify_qualification_projection(census)
            if verdict.passed is not True:
                if not callable(on_failure):
                    raise ProvenanceError("final_prerequisite_mismatch")
                session = validated[0].session
                session.update({
                    "census": census,
                    "verdict": verdict,
                    "state": "failed",
                })
                try:
                    failure_event_digest = on_failure(census, verdict)
                except BaseException:
                    if getattr(authority.authority.ledger, "state", None) != "terminal":
                        session.pop("census", None)
                        session.pop("verdict", None)
                        session["state"] = "collecting"
                    raise
                event_digest = (
                    failure_event_digest
                    if isinstance(failure_event_digest, str)
                    else (
                        authority.authority.ledger.events[-1].digest
                        if getattr(authority.authority.ledger, "events", ())
                        else None
                    )
                )
                if not isinstance(event_digest, str):
                    raise ProvenanceError("first_consume_mismatch")
                session["terminal_event_digest"] = event_digest
                self._discard_qualification_execution_scope(authority, session)
                return verdict
            else:
                candidate_terminals = tuple(
                    self._make_qualification_terminal_receipt(authority, item)
                    for item in validated
                )
                if callable(on_success):
                    on_success(
                        census,
                        verdict,
                        _terminal_receipt_census_digest_from_identities(
                            tuple(item.identity for item in candidate_terminals)
                        ),
                        candidate_terminals,
                        validated,
                    )
            # From this point on, no caller-owned receipt/capability is read.
            # The commit consumes only the immutable snapshots above, so a
            # concurrent object mutation cannot partially consume the census.
            terminals = tuple(
                self._make_qualification_terminal_receipt(authority, item)
                for item in validated
            )
            for item, terminal in zip(validated, terminals):
                self._register_qualification_terminal(
                    item.session,
                    item.domain,
                    item.coordinate,
                    terminal,
                    item.record,
                )
            for item, terminal in zip(validated, terminals):
                self._commit_registered_qualification_terminal(
                    item.session,
                    item.domain,
                    item.coordinate,
                    terminal,
                    item.record,
                )
            for item in validated:
                del self.__qualification_coordinate_capabilities[id(item.capability)]
                del self.__qualification_coordinate_execution_receipts[
                    id(item.execution_receipt)
                ]
                self.__qualification_coordinate_execution_identities.pop(
                    id(item.execution_receipt), None,
                )
                for stage in item.stages:
                    del self.__qualification_execution_stage_receipts[id(stage)]
                    self.__qualification_execution_stage_identities.pop(id(stage), None)
                    del self.__qualification_execution_stage_observations[id(stage)]
                    self._discard_boundary_artifact_for_stage(stage)
            return verdict

    def _publish_qualification_coordinate_terminal(
        self,
        authority: ActiveQualificationAuthority,
        receipt: QualificationCoordinateTerminalReceipt,
    ) -> None:
        """Publish only an exact parent-observed terminal receipt to the registry."""

        if not isinstance(authority, ActiveQualificationAuthority) or authority.owner is not self:
            raise TypeError("active parent qualification authority required")
        with self.__qualification_semantic_lock:
            session = self.__qualification_semantic_sessions.get(authority.identity)
            observed = self.__qualification_coordinate_observations.get(id(receipt))
            if (session is None or session["state"] != "collecting"
                    or observed is None or observed[0] is not receipt
                    or receipt._owner is not self
                    or receipt.authority_digest != authority.identity
                    or receipt.activation_digest != authority.activation_digest
                    or receipt.reservation_id != authority.reservation_id
                    or session["receipts"].get((receipt.domain, receipt.coordinate)) is not receipt
                    or receipt.identity != session["receipt_identities"].get(
                        (receipt.domain, receipt.coordinate)
                    )
                    or _coordinate_terminal_receipt_identity(receipt)
                        != receipt.identity
                    or not authority.current_at()
                    or authority.lifecycle != "active"):
                raise ProvenanceError("authority_replay")
            record = observed[1]
            if receipt.domain == LIVE_QUALIFICATION_NAMESPACE:
                target = session["cells"]
            elif receipt.domain == QUALIFICATION_PROBE_NAMESPACE:
                target = session["probes"]
            else:
                raise ProvenanceError("final_prerequisite_mismatch")
            if (receipt.coordinate in target
                    or receipt.record_identity != record.identity):
                raise ProvenanceError("final_prerequisite_mismatch")
            self._commit_registered_qualification_terminal(
                session,
                receipt.domain,
                receipt.coordinate,
                receipt,
                record,
            )

    def _prepare_qualification_semantics(
        self, authority: ActiveQualificationAuthority,
    ) -> tuple[QualificationCensus, QualificationVerdict]:
        """Recompute the frozen predicate exclusively from the parent registry."""

        with self.__qualification_semantic_lock:
            census, verdict = self._qualification_semantic_projection(authority)
            if verdict.passed is not True:
                raise ProvenanceError("final_prerequisite_mismatch")
            session = self.__qualification_semantic_sessions[authority.identity]
            session["census"] = census
            session["verdict"] = verdict
            session["state"] = "verified"
            return census, verdict

    def _qualification_semantic_projection(
        self, authority: ActiveQualificationAuthority,
    ) -> tuple[QualificationCensus, QualificationVerdict]:
        """Return the parent-only projection, including deterministic failures."""

        with self.__qualification_semantic_lock:
            session = self.__qualification_semantic_sessions.get(authority.identity)
            if (session is None or session["authority"] is not authority
                    or session["state"] != "collecting"
                    or tuple(session["cells"]) != QUALIFICATION_SCHEDULE
                    or tuple(session["probes"]) != QUALIFICATION_PROBES
                    or tuple(session["receipts"]) != tuple(
                        (domain, coordinate)
                        for domain, coordinates in (
                            (LIVE_QUALIFICATION_NAMESPACE, QUALIFICATION_SCHEDULE),
                            (QUALIFICATION_PROBE_NAMESPACE, QUALIFICATION_PROBES),
                        )
                        for coordinate in coordinates
                    )
                    or tuple(session["receipt_identities"]) != tuple(session["receipts"])
                    or not authority.current_at()
                    or authority.lifecycle != "active"):
                raise ProvenanceError("final_prerequisite_mismatch")
            census = QualificationCensus(
                tuple(session["cells"][key] for key in QUALIFICATION_SCHEDULE),
                tuple(session["probes"][key] for key in QUALIFICATION_PROBES),
            )
            verdict = verify_qualification_projection(census)
            return census, verdict

    def _qualification_pass_payload(
        self,
        authority: ActiveQualificationAuthority,
        census: QualificationCensus,
        verdict: QualificationVerdict,
        terminal_receipt_digest: str,
        terminal_receipts: Sequence[QualificationCoordinateTerminalReceipt],
    ) -> dict[str, Any]:
        return {
            "result": "passed",
            "phase": "qualification",
            "authority_digest": authority.identity,
            "evidence_origin": (
                RUNTIME_VERIFIED_ORIGIN
                if authority.origin == RUNTIME_VERIFIED_ORIGIN else INJECTED_FAKE_ORIGIN
            ),
            "terminal_verified": True,
            "semantic_projection_digest": census.aggregate_digest,
            "semantic_probe_digest": census.probe_digest,
            "semantic_verdict_digest": verdict.identity,
            "semantic_verifier_identity": SEMANTIC_VERIFIER_IDENTITY,
            "terminal_receipt_digest": terminal_receipt_digest,
            "terminal_registry_snapshot": _qualification_terminal_registry_snapshot(
                census.cells, census.probes, terminal_receipts,
            ),
        }

    def _ledger_terminal_qualification_batch(
        self,
        authority: ActiveQualificationAuthority,
        ledger: Any,
        census: QualificationCensus,
        verdict: QualificationVerdict,
        terminal_receipt_digest: str,
        terminal_receipts: Sequence[QualificationCoordinateTerminalReceipt],
        *,
        _validated_batch: tuple[_ValidatedQualificationExecution, ...] | None = None,
    ) -> str:
        """Append a passed event for a validated, not-yet-committed census."""

        with self.__qualification_semantic_lock:
            session = self.__qualification_semantic_sessions.get(authority.identity)
            if (
                not self._owns_active_qualification_authority(authority)
                or session is None
                or session["authority"] is not authority
                or session["state"] != "collecting"
                or authority.authority.ledger is not ledger
                or getattr(ledger, "state", None) != "active"
                or verdict.passed is not True
            ):
                raise ProvenanceError("final_prerequisite_mismatch")
            expected = tuple(
                (domain, coordinate)
                for domain, coordinates in (
                    (LIVE_QUALIFICATION_NAMESPACE, QUALIFICATION_SCHEDULE),
                    (QUALIFICATION_PROBE_NAMESPACE, QUALIFICATION_PROBES),
                )
                for coordinate in coordinates
            )
            if (
                type(census) is not QualificationCensus
                or type(verdict) is not QualificationVerdict
                or type(terminal_receipts) is not tuple
                or type(_validated_batch) is not tuple
                or len(terminal_receipts) != len(expected)
                or len(_validated_batch) != len(expected)
                or any(type(item) is not _ValidatedQualificationExecution
                       for item in _validated_batch)
            ):
                raise ProvenanceError("final_prerequisite_mismatch")
            parent_records: list[NormalizedCell | NormalizedProbe] = []
            for (domain, coordinate), receipt, validated in zip(
                expected, terminal_receipts, _validated_batch,
            ):
                if (
                    type(receipt) is not QualificationCoordinateTerminalReceipt
                    or receipt._owner is not self
                    or receipt.authority_digest != authority.identity
                    or receipt.activation_digest != authority.activation_digest
                    or receipt.reservation_id != authority.reservation_id
                    or receipt.domain != domain
                    or receipt.coordinate != coordinate
                    or _coordinate_terminal_receipt_identity(receipt) != receipt.identity
                    or validated.session is not session
                    or validated.domain != domain
                    or validated.coordinate != coordinate
                    or receipt.capability_identity != validated.capability_identity
                    or receipt.record_identity != validated.record_identity
                    or receipt.execution_receipt_identity
                    != validated.execution_receipt_identity
                    or receipt.execution_stage_receipt_identities
                    != validated.execution_stage_receipt_identities
                ):
                    raise ProvenanceError("authority_replay")
                parent_records.append(validated.record)
            parent_census = QualificationCensus(
                tuple(
                    record for record in parent_records
                    if isinstance(record, NormalizedCell)
                ),
                tuple(
                    record for record in parent_records
                    if isinstance(record, NormalizedProbe)
                ),
            )
            parent_verdict = verify_qualification_projection(parent_census)
            if (
                tuple(record.identity for record in census.cells)
                != tuple(record.identity for record in parent_census.cells)
                or tuple(record.identity for record in census.probes)
                != tuple(record.identity for record in parent_census.probes)
                or verdict.identity != parent_verdict.identity
                or parent_verdict.passed is not True
                or terminal_receipt_digest
                != _terminal_receipt_census_digest_from_identities(
                    tuple(receipt.identity for receipt in terminal_receipts)
                )
            ):
                raise ProvenanceError("final_prerequisite_mismatch")
            payload = self._qualification_pass_payload(
                authority,
                parent_census,
                parent_verdict,
                terminal_receipt_digest,
                terminal_receipts,
            )
            event_digest = self._append_current_qualification_terminal(
                authority, ledger, payload,
            )
            session["terminal_event_digest"] = event_digest
            return event_digest

    def finalize_qualification_semantics(
        self, authority: ActiveQualificationAuthority,
    ) -> QualificationSemanticAttestation:
        """Finalize an already-appended parent terminal batch."""

        with self.__qualification_semantic_lock:
            session = self.__qualification_semantic_sessions.get(authority.identity)
            if session is None or session.get("authority") is not authority:
                raise ProvenanceError("final_prerequisite_mismatch")
            if session.get("state") == "attested":
                attestation = session.get("attestation")
                if self.owns_qualification_semantic_attestation(
                    attestation, authority=authority,
                ):
                    return attestation
                raise ProvenanceError("final_prerequisite_mismatch")
            if session.get("state") == "terminal":
                self._discard_qualification_execution_scope(
                    authority, session, clear_registry=False,
                )
                return self._mint_qualification_semantic_attestation(authority)
            if session.get("state") != "collecting":
                raise ProvenanceError("final_prerequisite_mismatch")
            ledger = authority.authority.ledger
            if getattr(ledger, "state", None) != "terminal":
                raise ProvenanceError("authority_replay")
            census, verdict = self._qualification_semantic_projection(authority)
            if verdict.passed is not True:
                raise ProvenanceError("final_prerequisite_mismatch")
            receipt_digest = _terminal_receipt_census_digest(session)
            terminal_receipts = tuple(session["receipts"].values())
            payload = self._qualification_pass_payload(
                authority, census, verdict, receipt_digest, terminal_receipts,
            )
            if (
                not getattr(ledger, "events", ())
                or _deep_thaw(dict(ledger.events[-1].payload)) != payload
                or session.get("terminal_event_digest") != ledger.events[-1].digest
            ):
                raise ProvenanceError("authority_replay")
            session["census"] = census
            session["verdict"] = verdict
            self._discard_qualification_execution_scope(
                authority, session, clear_registry=False,
            )
            session["state"] = "terminal"
            return self._mint_qualification_semantic_attestation(authority)

    def _mint_qualification_semantic_attestation(
        self,
        authority: ActiveQualificationAuthority,
    ) -> QualificationSemanticAttestation:
        """Bind a verified parent census to its durable terminal ledger event."""

        with self.__qualification_semantic_lock:
            session = self.__qualification_semantic_sessions.get(authority.identity)
            if (session is None or session["authority"] is not authority
                    or session["state"] != "terminal"):
                raise ProvenanceError("final_prerequisite_mismatch")
            census = session["census"]
            projection_verdict = session["verdict"]
            ledger = authority.authority.ledger
            verify_chain = getattr(ledger, "verify_chain", None)
            if not callable(verify_chain) or not verify_chain():
                raise ProvenanceError("ledger_corrupt")
            events = getattr(ledger, "events", ())
            payload = dict(events[-1].payload) if events else {}
            expected_payload = self._qualification_pass_payload(
                authority,
                census,
                projection_verdict,
                _terminal_receipt_census_digest(session),
                tuple(session["receipts"].values()),
            )
            if (not _ledger_is(ledger, namespace="qualification", state="terminal",
                               reservation=authority.reservation_id)
                     or _deep_thaw(payload) != _deep_thaw(expected_payload)
                     or payload.get("authority_digest") != authority.identity
                    or payload.get("semantic_projection_digest") != census.aggregate_digest
                    or payload.get("semantic_probe_digest") != census.probe_digest
                    or payload.get("semantic_verdict_digest") != projection_verdict.identity
                    or payload.get("semantic_verifier_identity") != SEMANTIC_VERIFIER_IDENTITY
                     or payload.get("terminal_receipt_digest")
                        != _terminal_receipt_census_digest(session)
                     or session.get("terminal_event_digest")
                        != (events[-1].digest if events else None)
                     or _deep_thaw(payload.get("terminal_registry_snapshot"))
                        != _qualification_terminal_registry_snapshot(
                            census.cells,
                            census.probes,
                            tuple(session["receipts"].values()),
                        )
                    or payload.get("terminal_verified") is not True):
                raise ProvenanceError("final_prerequisite_mismatch")
            terminal = NormalizedTerminal(
                authority=authority.identity,
                activation=authority.activation_digest,
                profile_digest=authority.profile_digest,
                evidence_origin=(RUNTIME_VERIFIED_ORIGIN if authority.origin == RUNTIME_VERIFIED_ORIGIN
                                 else INJECTED_FAKE_ORIGIN),
                ledger_digest=ledger.head_digest,
                aggregate_digest=census.aggregate_digest,
                probe_digest=census.probe_digest,
                result="passed",
                state="terminal",
                verified=True,
            )
            complete = QualificationCensus(census.cells, census.probes, terminal)
            verdict = verify_qualification(complete)
            if verdict.passed is not True:
                raise ProvenanceError("final_prerequisite_mismatch")
            body = authority.body
            values = {
                "qualification_authority_digest": authority.identity,
                "activation_digest": authority.activation_digest,
                "reservation_id": authority.reservation_id,
                "ledger_root_digest": body["ledger"]["root_digest"],
                "namespace": body["namespace"],
                "evidence_origin": terminal.evidence_origin,
                "schedule_identity": QUALIFICATION_SCHEDULE_IDENTITY,
                "schedule_digest": QUALIFICATION_SCHEDULE_DIGEST,
                "probe_identity": QUALIFICATION_PROBE_IDENTITY,
                "probe_schedule_digest": PROBE_SCHEDULE_DIGEST,
                "cell_terminal_digest": complete.cell_digest,
                "probe_terminal_digest": complete.probe_digest,
                "terminal_receipt_digest": _terminal_receipt_census_digest(session),
                "cell_campaign_id": complete.cells[0].campaign_id,
                "probe_campaign_id": complete.probes[0].campaign_id,
                "semantic_projection_digest": census.aggregate_digest,
                "semantic_verdict_digest": verdict.identity,
                "semantic_verifier_identity": SEMANTIC_VERIFIER_IDENTITY,
                "semantic_result": "passed",
                "source_aggregate": body["source_closure"]["aggregate_sha256"],
                "profile_digest": body["profile"]["detached_digest"],
                "contract_set_digest": canonical_sha256(_deep_thaw(body["contracts"])),
                "capsule_digest": body["execution_capsule"]["capsule_digest"],
                "environment_digest": canonical_sha256(_deep_thaw(body["environment"])),
                "terminal_ledger_digest": ledger.head_digest,
                "terminal_event_digest": events[-1].digest,
            }
            attestation = object.__new__(QualificationSemanticAttestation)
            for name, value in values.items():
                object.__setattr__(attestation, name, value)
            object.__setattr__(attestation, "identity", canonical_sha256({
                "artifact": "minecraft-k12-qualification-semantic-attestation/1",
                **values,
            }))
            object.__setattr__(attestation, "_owner", self)
            session.update({
                "complete_census": complete,
                "terminal_verdict": verdict,
                "attestation": attestation,
                "state": "attested",
            })
            self.__qualification_semantic_attestations[id(attestation)] = attestation
            return attestation

    def owns_qualification_semantic_attestation(
        self, attestation: Any, *, authority: Any = None,
    ) -> bool:
        """Authenticate an exact parent-minted attestation and its live closure."""

        if not isinstance(attestation, QualificationSemanticAttestation):
            return False
        with self.__qualification_semantic_lock:
            if self.__qualification_semantic_attestations.get(id(attestation)) is not attestation:
                return False
            session = self.__qualification_semantic_sessions.get(
                attestation.qualification_authority_digest
            )
            if (session is None or session.get("attestation") is not attestation
                    or session.get("state") != "attested"):
                return False
            active = session["authority"]
            execution = active.authority
            if isinstance(authority, ActiveQualificationAuthority):
                if authority is not active:
                    return False
            elif isinstance(authority, QualificationExecutionAuthority):
                if authority is not execution:
                    return False
            elif authority is not None:
                return False
            if not self._qualification_session_records_are_intact(session):
                return False
            complete = session["complete_census"]
            verdict = verify_qualification(complete)
            ledger = execution.ledger
            verify_chain = getattr(ledger, "verify_chain", None)
            if not callable(verify_chain) or not verify_chain():
                return False
            events = getattr(ledger, "events", ())
            if not events:
                return False
            payload = dict(events[-1].payload)
            if (
                _deep_thaw(payload.get("terminal_registry_snapshot"))
                != _qualification_terminal_registry_snapshot(
                    session["census"].cells,
                    session["census"].probes,
                    tuple(session["receipts"].values()),
                )
            ):
                return False
            body = execution.body
            expected = {
                "qualification_authority_digest": execution.identity,
                "activation_digest": active.activation_digest,
                "reservation_id": execution.reservation_id,
                "ledger_root_digest": body["ledger"]["root_digest"],
                "namespace": body["namespace"],
                "evidence_origin": (RUNTIME_VERIFIED_ORIGIN if execution.origin == RUNTIME_VERIFIED_ORIGIN
                                    else INJECTED_FAKE_ORIGIN),
                "schedule_identity": QUALIFICATION_SCHEDULE_IDENTITY,
                "schedule_digest": QUALIFICATION_SCHEDULE_DIGEST,
                "probe_identity": QUALIFICATION_PROBE_IDENTITY,
                "probe_schedule_digest": PROBE_SCHEDULE_DIGEST,
                "cell_terminal_digest": complete.cell_digest,
                "probe_terminal_digest": complete.probe_digest,
                "terminal_receipt_digest": _terminal_receipt_census_digest(session),
                "cell_campaign_id": complete.cells[0].campaign_id,
                "probe_campaign_id": complete.probes[0].campaign_id,
                "semantic_projection_digest": session["census"].aggregate_digest,
                "semantic_verdict_digest": verdict.identity,
                "semantic_verifier_identity": SEMANTIC_VERIFIER_IDENTITY,
                "semantic_result": "passed",
                "source_aggregate": body["source_closure"]["aggregate_sha256"],
                "profile_digest": body["profile"]["detached_digest"],
                "contract_set_digest": canonical_sha256(_deep_thaw(body["contracts"])),
                "capsule_digest": body["execution_capsule"]["capsule_digest"],
                "environment_digest": canonical_sha256(_deep_thaw(body["environment"])),
                "terminal_ledger_digest": getattr(ledger, "head_digest", ""),
                "terminal_event_digest": events[-1].digest if events else "",
            }
            expected_identity = canonical_sha256({
                "artifact": "minecraft-k12-qualification-semantic-attestation/1",
                **expected,
            })
            return (verdict.passed is True
                    and _ledger_is(ledger, namespace="qualification", state="terminal",
                                   reservation=execution.reservation_id)
                    and session.get("terminal_event_digest")
                        == (events[-1].digest if events else None)
                    and all(getattr(attestation, name, None) == value
                            for name, value in expected.items())
                    and attestation.identity == expected_identity)

    def qualification_semantic_attestation(
        self, authority: ActiveQualificationAuthority,
    ) -> QualificationSemanticAttestation:
        """Return the already-finalized attestation for descriptive consumers."""

        with self.__qualification_semantic_lock:
            session = self.__qualification_semantic_sessions.get(
                getattr(authority, "identity", "")
            )
            attestation = None if session is None else session.get("attestation")
        if not self.owns_qualification_semantic_attestation(
            attestation, authority=authority,
        ):
            try:
                return self.recover_qualification_semantics(authority)
            except ProvenanceError as exc:
                raise ProvenanceError("final_prerequisite_mismatch") from exc
        return attestation

    def recover_qualification_semantics(
        self, authority: ActiveQualificationAuthority,
    ) -> QualificationSemanticAttestation:
        """Replay a durable passed registry snapshot after an interrupted commit."""

        with self.__qualification_semantic_lock:
            session = self.__qualification_semantic_sessions.get(
                getattr(authority, "identity", "")
            )
            if (
                not isinstance(authority, ActiveQualificationAuthority)
                or self.__active_qualification_authorities.get(id(authority)) is not authority
                or session is None
                or session["authority"] is not authority
                or authority.owner is not self
            ):
                raise ProvenanceError("authority_replay")
            if session.get("state") == "attested":
                attestation = session.get("attestation")
                if self.owns_qualification_semantic_attestation(
                    attestation, authority=authority,
                ):
                    return attestation
                raise ProvenanceError("final_prerequisite_mismatch")
            ledger = authority.authority.ledger
            verify_chain = getattr(ledger, "verify_chain", None)
            if not callable(verify_chain) or not verify_chain():
                raise ProvenanceError("ledger_corrupt")
            events = getattr(ledger, "events", ())
            payload = dict(events[-1].payload) if events else {}
            if (
                getattr(ledger, "state", None) == "terminal"
                and payload.get("result") == "failed"
            ):
                event_digest = events[-1].digest if events else None
                stored_event_digest = session.get("terminal_event_digest")
                expected_origin = (
                    RUNTIME_VERIFIED_ORIGIN
                    if authority.origin == RUNTIME_VERIFIED_ORIGIN
                    else INJECTED_FAKE_ORIGIN
                )
                if (
                    payload.get("authority_digest") != authority.identity
                    or payload.get("phase") != "qualification"
                    or payload.get("evidence_origin") != expected_origin
                    or payload.get("terminal_verified") is not False
                    or not isinstance(payload.get("failure_reason"), str)
                    or not isinstance(session.get("census"), QualificationCensus)
                    or not isinstance(session.get("verdict"), QualificationVerdict)
                    or payload.get("qualification_aggregate_digest")
                        != session["census"].aggregate_digest
                    or payload.get("probe_aggregate_digest")
                        != session["verdict"].probe_digest
                    or not isinstance(event_digest, str)
                    or (
                        stored_event_digest is not None
                        and stored_event_digest != event_digest
                    )
                ):
                    raise ProvenanceError("final_prerequisite_mismatch")
                session["terminal_event_digest"] = event_digest
                self._discard_qualification_execution_scope(authority, session)
                session.update({
                    "state": "failed",
                    "failure_reason": payload["failure_reason"],
                })
                raise ProvenanceError("qualification_failed")
            snapshot = _deep_thaw(payload.get("terminal_registry_snapshot"))
            event_digest = events[-1].digest if events else None
            stored_event_digest = session.get("terminal_event_digest")
            if (
                not isinstance(stored_event_digest, str)
                or stored_event_digest != event_digest
            ):
                raise ProvenanceError("final_prerequisite_mismatch")
            if (
                getattr(ledger, "state", None) != "terminal"
                or payload.get("result") != "passed"
                or payload.get("phase") != "qualification"
                or payload.get("evidence_origin")
                    != (
                        RUNTIME_VERIFIED_ORIGIN
                        if authority.origin == RUNTIME_VERIFIED_ORIGIN
                        else INJECTED_FAKE_ORIGIN
                    )
                or payload.get("terminal_verified") is not True
                or payload.get("semantic_verifier_identity")
                    != SEMANTIC_VERIFIER_IDENTITY
                or not isinstance(snapshot, Mapping)
                or session.get("terminal_event_digest") != event_digest
            ):
                raise ProvenanceError("final_prerequisite_mismatch")

            def restore_records(
                domain: str,
                entries: Any,
                expected_coordinates: tuple[str, ...],
            ) -> tuple[NormalizedCell, ...] | tuple[NormalizedProbe, ...]:
                if type(entries) is not list or len(entries) != len(expected_coordinates):
                    raise ProvenanceError("final_prerequisite_mismatch")
                records: list[NormalizedCell | NormalizedProbe] = []
                for coordinate, entry in zip(expected_coordinates, entries):
                    if not isinstance(entry, Mapping) or not isinstance(
                        entry.get("record"), Mapping
                    ):
                        raise ProvenanceError("final_prerequisite_mismatch")
                    try:
                        record = _normalized_record_from_snapshot(domain, entry["record"])
                    except (TypeError, ValueError, KeyError, AttributeError, IndexError) as exc:
                        raise ProvenanceError("final_prerequisite_mismatch") from exc
                    if (
                        getattr(record, "cell_id", getattr(record, "probe", None))
                            != coordinate
                        or entry.get("identity") != record.identity
                    ):
                        raise ProvenanceError("final_prerequisite_mismatch")
                    records.append(record)
                return tuple(records)  # type: ignore[return-value]

            cells = restore_records(
                LIVE_QUALIFICATION_NAMESPACE,
                snapshot.get("cells"),
                QUALIFICATION_SCHEDULE,
            )
            probes = restore_records(
                QUALIFICATION_PROBE_NAMESPACE,
                snapshot.get("probes"),
                QUALIFICATION_PROBES,
            )
            receipt_entries = snapshot.get("receipts")
            expected_keys = tuple(
                (domain, coordinate)
                for domain, coordinates in (
                    (LIVE_QUALIFICATION_NAMESPACE, QUALIFICATION_SCHEDULE),
                    (QUALIFICATION_PROBE_NAMESPACE, QUALIFICATION_PROBES),
                )
                for coordinate in coordinates
            )
            if type(receipt_entries) is not list or len(receipt_entries) != len(expected_keys):
                raise ProvenanceError("final_prerequisite_mismatch")
            restored_receipts: list[QualificationCoordinateTerminalReceipt] = []
            for (domain, coordinate), entry in zip(expected_keys, receipt_entries):
                if not isinstance(entry, Mapping):
                    raise ProvenanceError("final_prerequisite_mismatch")
                try:
                    stage_identities = tuple(entry.get("execution_stages", ()))
                except (TypeError, ValueError) as exc:
                    raise ProvenanceError("final_prerequisite_mismatch") from exc
                receipt = object.__new__(QualificationCoordinateTerminalReceipt)
                values = {
                    "authority_digest": entry.get("authority"),
                    "activation_digest": entry.get("activation"),
                    "reservation_id": entry.get("reservation"),
                    "domain": entry.get("domain"),
                    "coordinate": entry.get("coordinate"),
                    "capability_identity": entry.get("capability"),
                    "record_identity": entry.get("record"),
                    "execution_receipt_identity": entry.get("execution_receipt"),
                    "execution_stage_receipt_identities": stage_identities,
                    "_owner": self,
                }
                if (
                    values["authority_digest"] != authority.identity
                    or values["activation_digest"] != authority.activation_digest
                    or values["reservation_id"] != authority.reservation_id
                    or values["domain"] != domain
                    or values["coordinate"] != coordinate
                    or not isinstance(values["execution_receipt_identity"], str)
                    or type(values["execution_stage_receipt_identities"]) is not tuple
                    or len(values["execution_stage_receipt_identities"])
                    != (6 if domain == LIVE_QUALIFICATION_NAMESPACE else 1)
                    or not all(
                        isinstance(item, str)
                        for item in values["execution_stage_receipt_identities"]
                    )
                    or values["record_identity"]
                        != (cells[QUALIFICATION_SCHEDULE.index(coordinate)].identity
                            if domain == LIVE_QUALIFICATION_NAMESPACE
                            else probes[QUALIFICATION_PROBES.index(coordinate)].identity)
                ):
                    raise ProvenanceError("final_prerequisite_mismatch")
                try:
                    _require_canonical_digest(
                        values["execution_receipt_identity"],
                        "final_prerequisite_mismatch",
                    )
                    for stage_identity in values["execution_stage_receipt_identities"]:
                        _require_canonical_digest(
                            stage_identity, "final_prerequisite_mismatch",
                        )
                except ProvenanceError:
                    raise ProvenanceError("final_prerequisite_mismatch")
                for name, value in values.items():
                    object.__setattr__(receipt, name, value)
                object.__setattr__(
                    receipt, "identity", _coordinate_terminal_receipt_identity(receipt),
                )
                if receipt.identity != entry.get("identity"):
                    raise ProvenanceError("final_prerequisite_mismatch")
                restored_receipts.append(receipt)

            try:
                census = QualificationCensus(cells, probes)
                verdict = verify_qualification_projection(census)
            except (TypeError, ValueError, KeyError, AttributeError, IndexError) as exc:
                raise ProvenanceError("final_prerequisite_mismatch") from exc
            if (
                verdict.passed is not True
                or payload.get("semantic_projection_digest") != census.aggregate_digest
                or payload.get("semantic_probe_digest") != census.probe_digest
                or payload.get("semantic_verdict_digest") != verdict.identity
                or payload.get("terminal_receipt_digest")
                     != _terminal_receipt_census_digest_from_identities(
                         tuple(receipt.identity for receipt in restored_receipts)
                     )
            ):
                raise ProvenanceError("final_prerequisite_mismatch")
            expected_payload = self._qualification_pass_payload(
                authority,
                census,
                verdict,
                _terminal_receipt_census_digest_from_identities(
                    tuple(receipt.identity for receipt in restored_receipts)
                ),
                tuple(restored_receipts),
            )
            if _deep_thaw(payload) != _deep_thaw(expected_payload):
                raise ProvenanceError("final_prerequisite_mismatch")
            interrupted_terminal_ids = {
                id(receipt) for receipt in session.get("receipts", {}).values()
            }
            session["cells"] = {
                value.cell_id: value for value in cells
            }
            session["probes"] = {
                value.probe: value for value in probes
            }
            session["receipts"] = {
                key: receipt for key, receipt in zip(expected_keys, restored_receipts)
            }
            session["receipt_identities"] = {
                key: receipt.identity
                for key, receipt in zip(expected_keys, restored_receipts)
            }
            session["record_identities"] = {
                (LIVE_QUALIFICATION_NAMESPACE, record.cell_id): record.identity
                for record in cells
            } | {
                (QUALIFICATION_PROBE_NAMESPACE, record.probe): record.identity
                for record in probes
            }
            for receipt_id in interrupted_terminal_ids:
                self.__qualification_coordinate_observations.pop(receipt_id, None)
            session["terminal_event_digest"] = event_digest
            session.update({"census": census, "verdict": verdict, "state": "terminal"})
            self._discard_qualification_execution_scope(
                authority, session, clear_registry=False,
            )
            return self._mint_qualification_semantic_attestation(authority)

    def _qualification_execution_receipt_is_intact(
        self, receipt: Any,
    ) -> bool:
        owned = self.__qualification_coordinate_execution_receipts.get(id(receipt))
        if (
            type(receipt) is not QualificationCoordinateExecutionReceipt
            or owned is None
            or owned[0] is not receipt
            or self.__qualification_coordinate_execution_identities.get(id(receipt))
                != receipt.identity
            or receipt._owner is not self
            or _coordinate_execution_receipt_identity(receipt) != receipt.identity
        ):
            return False
        expected_names = (
            _QUALIFICATION_CELL_STAGE_NAMES
            if receipt.domain == LIVE_QUALIFICATION_NAMESPACE
            else _QUALIFICATION_PROBE_STAGE_NAMES
        )
        predecessor = ""
        for stage, expected_name in zip(owned[2], expected_names):
            observation = self.__qualification_execution_stage_observations.get(id(stage))
            boundary = self._boundary_artifact_for_stage(stage)
            if (
                type(stage) is not QualificationExecutionStageReceipt
                or self.__qualification_execution_stage_receipts.get(id(stage)) is not stage
                or self.__qualification_execution_stage_identities.get(id(stage))
                    != stage.identity
                or stage._owner is not self
                or stage.stage != expected_name
                or stage.predecessor_identity != predecessor
                or observation is None
                or boundary is None
                or boundary.values != observation
                or stage.observation_digest != canonical_sha256({
                    "stage": stage.stage, "observation": dict(observation),
                })
                or _execution_stage_receipt_identity(stage) != stage.identity
            ):
                return False
            predecessor = stage.identity
        record = owned[1]
        session = self.__qualification_semantic_sessions.get(receipt.authority_digest)
        expected_record_identity = None if session is None else session.get(
            "record_identities", {}
        ).get((receipt.domain, receipt.coordinate))
        try:
            record_identity = replace(record).identity
        except (TypeError, ValueError, AttributeError):
            return False
        return (
            len(owned[2]) == len(expected_names)
            and expected_record_identity == record.identity == record_identity
        )

    def owns_qualification_execution_stage_receipt(self, receipt: Any) -> bool:
        with self.__qualification_semantic_lock:
            return (
                type(receipt) is QualificationExecutionStageReceipt
                and self.__qualification_execution_stage_receipts.get(id(receipt)) is receipt
                and self.__qualification_execution_stage_identities.get(id(receipt))
                    == receipt.identity
                and receipt._owner is self
                and _execution_stage_receipt_identity(receipt) == receipt.identity
            )

    def owns_qualification_coordinate_execution_receipt(self, receipt: Any) -> bool:
        with self.__qualification_semantic_lock:
            return self._qualification_execution_receipt_is_intact(receipt)

    def qualification_execution_stage_receipts(
        self, receipt: QualificationCoordinateExecutionReceipt,
    ) -> tuple[QualificationExecutionStageReceipt, ...]:
        with self.__qualification_semantic_lock:
            owned = self.__qualification_coordinate_execution_receipts.get(id(receipt))
            if (
                owned is None
                or owned[0] is not receipt
                or not self._qualification_execution_receipt_is_intact(receipt)
            ):
                raise ProvenanceError("authority_replay")
            return owned[2]

    def qualification_execution_boundary_artifacts(
        self, receipt: QualificationCoordinateExecutionReceipt,
    ) -> tuple[QualificationExecutionBoundaryArtifact, ...]:
        with self.__qualification_semantic_lock:
            stages = self.qualification_execution_stage_receipts(receipt)
            artifacts = tuple(
                self._boundary_artifact_for_stage(stage) for stage in stages
            )
            if any(artifact is None for artifact in artifacts):
                raise ProvenanceError("authority_replay")
            return tuple(artifact for artifact in artifacts if artifact is not None)

    def owns_qualification_terminal_receipt(self, receipt: Any) -> bool:
        if not isinstance(receipt, QualificationCoordinateTerminalReceipt):
            return False
        with self.__qualification_semantic_lock:
            session = self.__qualification_semantic_sessions.get(receipt.authority_digest)
            if session is None:
                return False
            expected = session["receipts"].get((receipt.domain, receipt.coordinate))
            expected_identity = session["receipt_identities"].get(
                (receipt.domain, receipt.coordinate)
            )
            return (
                expected is receipt
                and receipt._owner is self
                and receipt.identity == expected_identity
                and _coordinate_terminal_receipt_identity(receipt)
                    == receipt.identity
            )

    def _qualification_session_records_are_intact(
        self, session: Mapping[str, Any],
    ) -> bool:
        record_identities = session.get("record_identities")
        if not isinstance(record_identities, Mapping):
            return False
        for domain, coordinates, field_name, mapping_name in (
            (
                LIVE_QUALIFICATION_NAMESPACE,
                QUALIFICATION_SCHEDULE,
                "cell_id",
                "cells",
            ),
            (
                QUALIFICATION_PROBE_NAMESPACE,
                QUALIFICATION_PROBES,
                "probe",
                "probes",
            ),
        ):
            records = session.get(mapping_name)
            if not isinstance(records, Mapping):
                return False
            for coordinate in coordinates:
                record = records.get(coordinate)
                if record is None or getattr(record, field_name, None) != coordinate:
                    return False
                if record_identities.get((domain, coordinate)) != getattr(
                    record, "identity", None,
                ):
                    return False
                try:
                    detached = _normalized_record_from_snapshot(
                        domain, _normalized_record_snapshot(record),
                    )
                except (TypeError, ValueError, AttributeError):
                    return False
                if detached.identity != record.identity:
                    return False
        return True

    def qualification_terminal_receipts(
        self, authority: ActiveQualificationAuthority,
    ) -> tuple[QualificationCoordinateTerminalReceipt, ...]:
        attestation = self.qualification_semantic_attestation(authority)
        del attestation
        with self.__qualification_semantic_lock:
            session = self.__qualification_semantic_sessions[authority.identity]
            return tuple(session["receipts"].values())

    def qualification_semantic_census(
        self, authority: ActiveQualificationAuthority,
    ) -> QualificationCensus:
        """Return the parent-owned semantic projection for presentation only."""

        self.qualification_semantic_attestation(authority)
        with self.__qualification_semantic_lock:
            session = self.__qualification_semantic_sessions.get(authority.identity)
            census = None if session is None else session.get("census")
            if (
                session is None
                or session.get("authority") is not authority
                or session.get("state") != "attested"
                or type(census) is not QualificationCensus
            ):
                raise ProvenanceError("final_prerequisite_mismatch")
            return QualificationCensus(
                tuple(
                    _normalized_record_from_snapshot(
                        LIVE_QUALIFICATION_NAMESPACE,
                        _normalized_record_snapshot(record),
                    )
                    for record in census.cells
                ),
                tuple(
                    _normalized_record_from_snapshot(
                        QUALIFICATION_PROBE_NAMESPACE,
                        _normalized_record_snapshot(record),
                    )
                    for record in census.probes
                ),
            )

    def qualification_probe_binding(
        self, authority: ActiveQualificationAuthority,
    ) -> AuthorityBinding:
        self.qualification_semantic_attestation(authority)
        with self.__qualification_semantic_lock:
            return self.__qualification_semantic_sessions[authority.identity]["probe_binding"]

    def qualification_authority_is_current_for_attestation(
        self, attestation: QualificationSemanticAttestation, *, now: int,
    ) -> bool:
        if not self.owns_qualification_semantic_attestation(attestation):
            return False
        with self.__qualification_semantic_lock:
            session = self.__qualification_semantic_sessions.get(
                attestation.qualification_authority_digest
            )
            active = None if session is None else session.get("authority")
        return (
            isinstance(active, ActiveQualificationAuthority)
            and active.current_at(now)
        )

    def lifecycle_for(self, ledger: Any) -> tuple[str, str]:
        if not self.owns_ledger(ledger):
            raise ProvenanceError("authority_replay")
        return _ledger_binding_state(ledger)

    def authority_is_current(self, authority: Any, *, now: int | None = None) -> bool:
        if isinstance(authority, (ActiveQualificationAuthority, ActiveFinalAuthority)):
            authority = authority.authority
        now = self._resolve_time(now)
        if not self.owns_authority(authority):
            return False
        lifecycle = self.lifecycle_for(authority.ledger)[0]
        if lifecycle == "terminal":
            events = getattr(authority.ledger, "events", ())
            terminal = dict(events[-1].payload) if events else {}
            if (getattr(authority.ledger, "namespace", None) != "qualification"
                    or terminal.get("result") != "passed"
                    or terminal.get("authority_digest") != authority.identity):
                return False
        elif lifecycle not in {"authorized", "consumed", "active"}:
            return False
        issued_at = authority.body.get("issued_at")
        expires_at = authority.body.get("expires_at")
        if (type(issued_at) is not int or type(expires_at) is not int
                or not issued_at <= now <= expires_at):
            return False
        run_authorization = getattr(authority, "run_authorization", None)
        if isinstance(run_authorization, (K12QualificationRunAuthorization,
                                          K12FinalRunAuthorization)):
            return run_authorization.current_at(now)
        return True

    def advance_trusted_time(self, seconds: int = 1) -> int:
        """Advance only an injected controller's deterministic trusted clock."""
        if not self.is_injected_test_controller:
            raise ProvenanceError("injected clock is required")
        with self.__qualification_terminalization_lock:
            return self.__trusted_clock.advance(seconds)

    advance_test_time = advance_trusted_time
    advance_logical_time = advance_trusted_time

    def injected_test_controller(self) -> "InjectedTestController":
        return InjectedTestController(self, _INJECTED_CONTROLLER_TOKEN)

    create_injected_test_controller = injected_test_controller
    injected_test_verifier = injected_test_controller
    create_injected_test_verifier = injected_test_controller

    def create_ledger(self, *, root: str | Path, namespace: str, reservation_id: str,
                      output_root_identity: str, nonce: str | None = None,
                      worktree_roots: Sequence[str | Path] = ()) -> Any:
        from .k12_execution_capsule import DurableLedger
        controller_key = secrets.token_bytes(32)
        ledger = DurableLedger.create_parent_owned(
            controller_key=controller_key, root=root, namespace=namespace,
            reservation_id=reservation_id, output_root_identity=output_root_identity,
            nonce=nonce, worktree_roots=worktree_roots,
        )
        self.__ledgers[id(ledger)] = ledger
        self.__ledger_keys[id(ledger)] = controller_key
        self.__ledger_keys_by_root[str(ledger.root.resolve())] = controller_key
        return ledger

    reserve_ledger = create_ledger

    def open_ledger(self, *, root: str | Path, namespace: str, reservation_id: str,
                    output_root_identity: str, nonce: str,
                    worktree_roots: Sequence[str | Path] = ()) -> Any:
        from .k12_execution_capsule import DurableLedger
        try:
            root_key = str(Path(root).resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise ProvenanceError("authority_replay") from exc
        controller_key = self.__ledger_keys_by_root.get(root_key)
        if controller_key is None:
            raise ProvenanceError("parent ledger capability is unavailable")
        ledger = DurableLedger.open_parent_owned(
            controller_key=controller_key,
            root=root, namespace=namespace, reservation_id=reservation_id,
            output_root_identity=output_root_identity, nonce=nonce,
            worktree_roots=worktree_roots,
        )
        self.__ledgers[id(ledger)] = ledger
        self.__ledger_keys[id(ledger)] = controller_key
        self.__ledger_keys_by_root[root_key] = controller_key
        return ledger

    def _ledger_controller(self, ledger: Any) -> Any:
        if not self.owns_ledger(ledger):
            raise TypeError("parent-owned durable ledger required")
        key = self.__ledger_keys.get(id(ledger))
        if key is None:
            raise TypeError("parent-owned durable ledger required")
        return ledger.acquire_parent_controller(key)

    def ledger_authority_minted(self, ledger: Any, authority_digest: str) -> str:
        return self._ledger_controller(ledger).authority_minted(authority_digest)

    def ledger_first_consume_verified(
            self, ledger: Any, authority_digest: str, observation_digest: str) -> str:
        return self._ledger_controller(ledger).first_consume_verified(
            authority_digest, observation_digest,
        )

    def ledger_activate(self, ledger: Any, authority_digest: str) -> str:
        return self._ledger_controller(ledger).activate(authority_digest)

    def ledger_terminal(self, ledger: Any, payload: Mapping[str, Any]) -> str:
        terminal_payload = dict(payload)
        if (getattr(ledger, "namespace", None) == "qualification"
                and terminal_payload.get("result") == "passed"):
            authority_identity = terminal_payload.get("authority_digest")
            with self.__qualification_semantic_lock:
                session = self.__qualification_semantic_sessions.get(authority_identity)
                census = None if session is None else session.get("census")
                verdict = None if session is None else session.get("verdict")
                if (session is None or session.get("state") != "verified"
                        or session["authority"].authority.ledger is not ledger
                        or terminal_payload.get("semantic_projection_digest")
                            != getattr(census, "aggregate_digest", None)
                        or terminal_payload.get("semantic_probe_digest")
                            != getattr(census, "probe_digest", None)
                        or terminal_payload.get("semantic_verdict_digest")
                            != getattr(verdict, "identity", None)
                        or terminal_payload.get("semantic_verifier_identity")
                            != SEMANTIC_VERIFIER_IDENTITY
                        or terminal_payload.get("terminal_receipt_digest")
                            != _terminal_receipt_census_digest(session)
                        or terminal_payload.get("terminal_registry_snapshot")
                            != _qualification_terminal_registry_snapshot(
                                census.cells,
                                census.probes,
                                tuple(session["receipts"].values()),
                            )
                        or terminal_payload.get("evidence_origin") != (
                            RUNTIME_VERIFIED_ORIGIN
                            if session["authority"].origin == RUNTIME_VERIFIED_ORIGIN
                            else INJECTED_FAKE_ORIGIN
                        )
                        or terminal_payload.get("terminal_verified") is not True):
                    raise ProvenanceError("final_prerequisite_mismatch")
                expected_payload = self._qualification_pass_payload(
                    session["authority"],
                    census,
                    verdict,
                    _terminal_receipt_census_digest(session),
                    tuple(session["receipts"].values()),
                )
                if terminal_payload != expected_payload:
                    raise ProvenanceError("final_prerequisite_mismatch")
                digest = self._append_current_qualification_terminal(
                    session["authority"], ledger, terminal_payload,
                )
                session["terminal_event_digest"] = digest
                session["state"] = "terminal"
                return digest
        if (
            getattr(ledger, "namespace", None) == "qualification"
            and terminal_payload.get("result") == "failed"
        ):
            with self.__qualification_semantic_lock:
                session = self.__qualification_semantic_sessions.get(
                    terminal_payload.get("authority_digest")
                )
                authority = None if session is None else session.get("authority")
                census = None if session is None else session.get("census")
                verdict = None if session is None else session.get("verdict")
                expected_origin = (
                    None
                    if authority is None
                    else (
                        RUNTIME_VERIFIED_ORIGIN
                        if authority.origin == RUNTIME_VERIFIED_ORIGIN
                        else INJECTED_FAKE_ORIGIN
                    )
                )
                expected_reason = (
                    None
                    if census is None or verdict is None
                    else (
                        "qualification_probe_failed"
                        if any(probe.passed is not True for probe in census.probes)
                        else "qualification_cell_failed"
                    )
                )
                expected_payload = {
                    "result": "failed",
                    "phase": "qualification",
                    "authority_digest": terminal_payload.get("authority_digest"),
                    "qualification_aggregate_digest": (
                        None if verdict is None else verdict.aggregate_digest
                    ),
                    "probe_aggregate_digest": (
                        None if verdict is None else verdict.probe_digest
                    ),
                    "evidence_origin": expected_origin,
                    "terminal_verified": False,
                    "failure_reason": expected_reason,
                }
                if (
                    session is None
                    or authority is None
                    or authority.authority.ledger is not ledger
                    or session.get("state") != "failed"
                    or type(census) is not QualificationCensus
                    or type(verdict) is not QualificationVerdict
                    or verdict.passed is not False
                    or terminal_payload != expected_payload
                    or getattr(ledger, "state", None) != "active"
                ):
                    raise ProvenanceError("final_prerequisite_mismatch")
                digest = self._append_current_qualification_terminal(
                    authority, ledger, terminal_payload,
                )
                session["terminal_event_digest"] = digest
                return digest
        if getattr(ledger, "namespace", None) == "qualification":
            raise ProvenanceError("final_prerequisite_mismatch")
        return self._ledger_controller(ledger).terminal(terminal_payload)

    def _append_current_qualification_terminal(
        self,
        authority: ActiveQualificationAuthority,
        ledger: Any,
        payload: Mapping[str, Any],
    ) -> str:
        """Linearize a qualification terminal event with authority freshness."""

        with self.__qualification_terminalization_lock:
            if not authority.current_at() or authority.lifecycle != "active":
                raise ProvenanceError("authority_replay")
            return self._ledger_controller(ledger).terminal(dict(payload))

    def quarantine_ledger(self, ledger: Any, payload: Mapping[str, Any]) -> str:
        return self._ledger_controller(ledger).quarantine(payload)

    def attest_execution_capsule(self, **values: Any) -> Any:
        from .k12_execution_capsule import _CAPSULE_MINT_TOKEN, mint_capsule

        capsule = mint_capsule(
            token=_CAPSULE_MINT_TOKEN,
            owner=self,
            **values,
        )
        self.__capsules[id(capsule)] = capsule
        return capsule

    def collect_source_closure(
            self, root: str | Path | None = None, policy: Any = None, *,
            trusted_root: str | Path | None = None,
            checkout: CheckoutObservation | None = None,
            revision_authorization: ExternalRevisionAuthorization | None = None,
            injected_only: bool = False,
    ) -> SourceClosure:
        """Collect policy source bytes through parent-owned file descriptors.

        Runtime collection derives every expected blob from the typed
        checkout's commit tree.  Injected-only collection may cover prospective
        files not present in Git; all record values still come from file FDs.
        """
        if root is None:
            root = trusted_root
        if root is None:
            raise TypeError("trusted source root is required")
        collection_origin = INJECTED_TEST_ORIGIN if injected_only else self.origin
        if injected_only:
            if checkout is not None:
                raise TypeError("injected source collection does not assert a Git tree")
            tree = None
            head_commit = head_tree = ""
        else:
            if not isinstance(checkout, CheckoutObservation):
                raise TypeError("typed checkout observation required")
            tree = _authenticated_git_tree(root, checkout, policy)
            head_commit = checkout.head_commit
            head_tree = checkout.head_tree
        closure = _collect_source_closure(
            policy=policy,
            root=root,
            expected_tree=tree,
            injected_only=injected_only,
            marker=self.__ownership_token,
            origin=collection_origin,
            owner=self,
            revision_authorization=revision_authorization,
            head_commit=head_commit,
            head_tree=head_tree,
        )
        self.__source_closures[id(closure)] = closure
        return closure

    def mint_external_revision_authorization(
        self,
        checkout: CheckoutObservation,
        pull_request: PullRequestObservation,
        *,
        verifier_identity: str | None = None,
        verifier_receipt_digest: str | None = None,
        now: int | None = None,
        expires_at: int | None = None,
        origin: str | None = None,
    ) -> ExternalRevisionAuthorization:
        """Mint a parent-owned authorization for the observed external tuple."""

        if not isinstance(checkout, CheckoutObservation):
            raise TypeError("typed checkout observation required")
        if not isinstance(pull_request, PullRequestObservation):
            raise TypeError("typed pull-request observation required")
        now = self._resolve_time(now)
        checkout.validate_structure()
        pull_request.validate_structure(now=now)
        if (checkout.symbolic_head_ref != checkout.upstream_ref
                or checkout.symbolic_head_ref != checkout.remote_ref
                or checkout.repository_identity != checkout.remote_repository
                or checkout.head_commit != checkout.upstream_commit
                or checkout.head_commit != checkout.remote_commit
                or checkout.index_tree != checkout.head_tree):
            raise ProvenanceError("git_head_mismatch")
        if (pull_request.repository != checkout.repository_identity
                or pull_request.head_repository != checkout.repository_identity
                or pull_request.head_ref
                    != checkout.symbolic_head_ref.removeprefix("refs/heads/")
                or pull_request.head_sha != checkout.head_commit):
            raise ProvenanceError("pr_semantic_mismatch")
        selected_origin = _normalize_origin(self.origin if origin is None else origin)
        if selected_origin != self.origin:
            raise ProvenanceError("authority_origin_mismatch")
        if verifier_identity is None:
            verifier_identity = (
                self.__revision_verifier_identity
                or "injected-external-revision-verifier/1"
            )
        if expires_at is None:
            expires_at = now + MAX_PR_AGE_SECONDS
        if type(expires_at) is not int or expires_at < now:
            raise ProvenanceError("pr_observation_stale")
        payload = external_revision_attestation_payload(
            checkout,
            pull_request,
            expires_at=expires_at,
            origin=selected_origin,
            verifier_identity=verifier_identity,
        )
        if self.origin == RUNTIME_VERIFIED_ORIGIN:
            if (self.__revision_verifier_key is None
                    or verifier_identity != self.__revision_verifier_identity
                    or not isinstance(verifier_receipt_digest, str)):
                raise TypeError("externally verified revision receipt is required")
            expected_receipt = hmac.new(
                self.__revision_verifier_key, payload, hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected_receipt, verifier_receipt_digest):
                raise ProvenanceError("pr_observation_missing")
        else:
            if verifier_receipt_digest is not None:
                raise TypeError("injected revision authorization cannot claim runtime receipt")
            verifier_receipt_digest = raw_sha256(payload)
        authorization = ExternalRevisionAuthorization(
            repository=checkout.repository_identity,
            branch=checkout.symbolic_head_ref,
            head_commit=checkout.head_commit,
            head_tree=checkout.head_tree,
            pull_request=pull_request.number,
            pull_request_semantic_tuple=pull_request.semantic_tuple(),
            base_ref=pull_request.base_ref,
            base_sha=pull_request.base_sha,
            issued_at=now,
            expires_at=expires_at,
            origin=selected_origin,
            verifier_identity=verifier_identity,
            pull_request_observed_at=pull_request.observed_at,
            pull_request_observer=pull_request.observer,
            pull_request_receipt_digest=pull_request.receipt_digest,
            verifier_receipt_digest=verifier_receipt_digest,
            ownership_token=self.__ownership_token,
            owner=self,
            token=_REVISION_AUTH_TOKEN,
        )
        if not authorization.matches_pull_request(pull_request, now=now):
            raise ProvenanceError("pr_semantic_mismatch")
        with self.__run_auth_lock:
            if authorization.identity in self.__external_revision_identities:
                raise ProvenanceError("authority_replay")
            self.__external_revisions[id(authorization)] = authorization
            self.__external_revision_identities.add(authorization.identity)
        return authorization

    authorize_external_revision = mint_external_revision_authorization
    issue_external_revision_authorization = mint_external_revision_authorization
    mint_revision_authorization = mint_external_revision_authorization
    mint_external_revision = mint_external_revision_authorization

    def mint_qualification_run_authorization(
        self, preflight: QualificationPreflight | None = None, *,
        reservation_id: str | None = None, output_root_identity: str = "",
        nonce: str | None = None, profile_digest: str = "",
         profile_identity: str = PROFILE_V2, evidence_origin: str | None = None,
         external_revision_authorization: ExternalRevisionAuthorization | None = None,
         revision_authorization: ExternalRevisionAuthorization | None = None,
         now: int | None = None, ledger: Any = None,
    ) -> K12QualificationRunAuthorization:
        now = self._resolve_time(now)
        requested_origin = _normalize_origin(
            self.origin if evidence_origin is None else evidence_origin
        )
        if requested_origin != self.origin:
            raise ProvenanceError("authority_origin_mismatch")
        if preflight is not None:
            if not isinstance(preflight, QualificationPreflight):
                raise TypeError("typed qualification preflight required")
            if reservation_id is not None and reservation_id != preflight.reservation_id:
                raise ProvenanceError("authority_replay")
            if output_root_identity and output_root_identity != preflight.output.root_identity:
                raise ProvenanceError("authority_replay")
            if profile_digest and profile_digest != preflight.profile_digest:
                raise ProvenanceError("authority_replay")
            if (external_revision_authorization is not None
                    and external_revision_authorization
                    is not preflight.external_revision_authorization):
                raise ProvenanceError("authority_replay")
            external_revision_authorization = preflight.external_revision_authorization
            reservation_id = preflight.reservation_id
            output_root_identity = preflight.output.root_identity
            profile_digest = profile_digest or preflight.profile_digest
        if (external_revision_authorization is not None
                and revision_authorization is not None
                and external_revision_authorization is not revision_authorization):
            raise ProvenanceError("authority_replay")
        external_revision_authorization = (
            external_revision_authorization or revision_authorization
        )
        if (not isinstance(external_revision_authorization, ExternalRevisionAuthorization)
                or not external_revision_authorization.owned_by(self)
                or external_revision_authorization.origin != requested_origin):
            raise TypeError("parent-owned external revision authorization required")
        if reservation_id is None:
            raise TypeError("reservation identity is required")
        if type(now) is not int:
            raise TypeError("integer issuance time is required")
        supplied_nonce = nonce
        nonce = nonce or secrets.token_hex(32)
        ledger_snapshot: Mapping[str, Any] | None = None
        if ledger is not None:
            if (not self.owns_ledger(ledger)
                    or ledger.namespace != "qualification"
                    or ledger.reservation_id != reservation_id):
                raise ProvenanceError("authority_replay")
            if output_root_identity != ledger.output_root_identity:
                raise ProvenanceError("authority_replay")
            if supplied_nonce is not None and supplied_nonce != ledger.nonce:
                raise ProvenanceError("authority_replay")
            ledger_snapshot = durable_ledger_snapshot(ledger)
            output_root_identity = ledger.output_root_identity
            nonce = ledger.nonce
        if not profile_digest:
            try:
                from .k12_runtime_profile import load_k12_live_runtime_profile
                profile_digest = load_k12_live_runtime_profile().profile_digest
            except Exception:
                profile_digest = ""
        _require_sha256(profile_digest, "profile_mismatch")
        if not external_revision_authorization.current_at(now):
            raise ProvenanceError("pr_observation_stale")
        body = {
            "artifact_id": "minecraft-k12-live-qualification-run-authorization",
            "artifact_version": 1,
            "schema_version": QUALIFICATION_RUN_AUTHORIZATION,
            "namespace": "qualification",
            "reservation_id": reservation_id,
            "output_root_identity": output_root_identity,
            "nonce": nonce,
            "profile_identity": profile_identity,
            "profile_digest": profile_digest,
            "capabilities": ["qualification_execute"],
            "evidence_origin": requested_origin,
            "issued_at": now,
            "expires_at": min(
                now + MAX_PR_AGE_SECONDS,
                external_revision_authorization.expires_at,
            ),
            "external_revision_authorization": external_revision_authorization.receipt(),
            "external_revision_authorization_digest": external_revision_authorization.identity,
        }
        if ledger_snapshot is not None:
            body["ledger"] = dict(ledger_snapshot)
        auth = K12QualificationRunAuthorization(
            body, self.__ownership_token, _RUN_AUTH_TOKEN,
            external_revision_authorization=external_revision_authorization,
            owner=self,
        )
        with self.__run_auth_lock:
            if auth.identity in self.__run_auths:
                raise ProvenanceError("authority_replay")
            self.__run_auths.add(auth.identity)
        return auth

    authorize_qualification_run = mint_qualification_run_authorization
    issue_qualification_run_authorization = mint_qualification_run_authorization
    mint_qualification_authorization = mint_qualification_run_authorization
    authorize_qualification = mint_qualification_run_authorization

    def mint_final_run_authorization(
        self, *, reservation_id: str, output_root_identity: str,
        nonce: str | None = None, profile_digest: str, qualification_authority_digest: str,
        qualification_semantic_attestation: QualificationSemanticAttestation,
        evidence_origin: str | None = None, now: int | None = None, ledger: Any = None,
        external_revision_authorization: ExternalRevisionAuthorization | None = None,
    ) -> K12FinalRunAuthorization:
        now = self._resolve_time(now)
        requested_origin = _normalize_origin(
            evidence_origin if evidence_origin is not None else self.origin
        )
        if self.origin == RUNTIME_VERIFIED_ORIGIN:
            if requested_origin != RUNTIME_VERIFIED_ORIGIN:
                raise ProvenanceError("authority_origin_mismatch")
        elif requested_origin not in {INJECTED_TEST_ORIGIN, INJECTED_FAKE_ORIGIN}:
            raise ProvenanceError("authority_origin_mismatch")
        if type(now) is not int:
            raise TypeError("integer issuance time is required")
        _require_canonical_digest(
            qualification_authority_digest, "authority_replay",
        )
        _require_sha256(profile_digest, "profile_mismatch")
        if (not self.owns_qualification_semantic_attestation(
                    qualification_semantic_attestation)
                or qualification_semantic_attestation.qualification_authority_digest
                    != qualification_authority_digest
                or qualification_semantic_attestation.profile_digest != profile_digest
                or qualification_semantic_attestation.evidence_origin != requested_origin):
            raise ProvenanceError("final_prerequisite_mismatch")
        with self.__qualification_semantic_lock:
            semantic_session = self.__qualification_semantic_sessions.get(
                qualification_authority_digest
            )
            active_qualification = (
                None if semantic_session is None else semantic_session.get("authority")
            )
        if (not isinstance(active_qualification, ActiveQualificationAuthority)
                or not active_qualification.current_at(now)):
            raise ProvenanceError("authority_replay")
        if (not isinstance(external_revision_authorization, ExternalRevisionAuthorization)
                or not external_revision_authorization.owned_by(self)
                or external_revision_authorization.origin != self.origin
                or not external_revision_authorization.current_at(now)):
            raise ProvenanceError("authority_replay")
        supplied_nonce = nonce
        nonce = nonce or secrets.token_hex(32)
        ledger_snapshot: Mapping[str, Any] | None = None
        if ledger is not None:
            if (not self.owns_ledger(ledger)
                    or ledger.namespace != "final"
                    or ledger.reservation_id != reservation_id):
                raise ProvenanceError("authority_replay")
            if output_root_identity != ledger.output_root_identity:
                raise ProvenanceError("authority_replay")
            if supplied_nonce is not None and supplied_nonce != ledger.nonce:
                raise ProvenanceError("authority_replay")
            ledger_snapshot = durable_ledger_snapshot(ledger)
            output_root_identity = ledger.output_root_identity
            nonce = ledger.nonce
        body = {
            "artifact_id": "minecraft-k12-live-final-run-authorization",
            "artifact_version": 1,
            "schema_version": FINAL_RUN_AUTHORIZATION,
            "namespace": "final",
            "reservation_id": reservation_id,
            "output_root_identity": output_root_identity,
            "nonce": nonce,
            "profile_digest": profile_digest,
            "qualification_authority_digest": qualification_authority_digest,
            "qualification_semantic_attestation_digest":
                qualification_semantic_attestation.identity,
            "qualification_semantic_projection_digest":
                qualification_semantic_attestation.semantic_projection_digest,
            "qualification_probe_projection_digest":
                qualification_semantic_attestation.probe_terminal_digest,
            "qualification_terminal_receipt_census_digest":
                qualification_semantic_attestation.terminal_receipt_digest,
            "qualification_terminal_event_digest":
                qualification_semantic_attestation.terminal_event_digest,
            "qualification_terminal_ledger_digest":
                qualification_semantic_attestation.terminal_ledger_digest,
            "capabilities": ["final_execute"],
            "evidence_origin": requested_origin,
            "issued_at": now,
            "external_revision_authorization": external_revision_authorization.receipt(),
            "external_revision_authorization_digest": external_revision_authorization.identity,
        }
        if ledger_snapshot is not None:
            body["ledger"] = dict(ledger_snapshot)
        auth = K12FinalRunAuthorization(
            body, self.__ownership_token, _RUN_AUTH_TOKEN,
            external_revision_authorization=external_revision_authorization,
            owner=self,
        )
        with self.__run_auth_lock:
            if auth.identity in self.__run_auths:
                raise ProvenanceError("authority_replay")
            self.__run_auths.add(auth.identity)
        return auth

    authorize_final_run = mint_final_run_authorization
    issue_final_run_authorization = mint_final_run_authorization
    mint_final_authorization = mint_final_run_authorization
    authorize_final = mint_final_run_authorization

    def retain_target_lease(self, lock: Any, reservation_id: str) -> K12RetainedTargetLease:
        lease = K12RetainedTargetLease(
            lock, reservation_id, self.__ownership_token, _LEASE_TOKEN, self,
        )
        self.__leases[id(lease)] = lease
        return lease

    retain_minecraft_target_lease = retain_target_lease
    retain_target_lock = retain_target_lease

    def _coerce_run_authorization(self, preflight: QualificationPreflight,
                                   ledger: Any) -> K12QualificationRunAuthorization:
        auth = preflight.run_authorization
        if not isinstance(auth, K12QualificationRunAuthorization):
            raise TypeError("typed qualification run authorization required")
        if (auth.ownership_token is not self.__ownership_token
                or auth.owner is not self
                or auth.evidence_origin != self.origin
                or not auth.external_revision_authorization.owned_by(self)
                or auth.external_revision_authorization.origin != self.origin
                or auth.reservation_id != ledger.reservation_id
                or auth.output_root_identity != ledger.output_root_identity
                or auth.nonce != ledger.nonce):
            raise ProvenanceError("authority_replay")
        return auth

    def _mint_qualification_impl(
            self, preflight: QualificationPreflight, *, now: int | None, ledger: Any,
            run_authorization: K12QualificationRunAuthorization | None = None,
    ) -> QualificationExecutionAuthority:
        now = self._resolve_time(now)
        from .k12_execution_capsule import DurableLedger
        if not isinstance(preflight, QualificationPreflight):
            raise TypeError("typed qualification preflight required")
        if not isinstance(ledger, DurableLedger):
            raise TypeError("typed durable ledger required")
        if not self.owns_ledger(ledger):
            raise ProvenanceError("authority_replay")
        if (not isinstance(preflight.source, SourceClosure)
                or not preflight.source.owned_by(self)
                or preflight.source.origin != self.origin):
            raise ProvenanceError("source_closure_incomplete")
        if (not isinstance(preflight.external_revision_authorization,
                           ExternalRevisionAuthorization)
                or not preflight.external_revision_authorization.owned_by(self)
                or preflight.external_revision_authorization.origin != self.origin):
            raise ProvenanceError("authority_replay")
        if not preflight.capsule.owned_by(self):
            raise ProvenanceError("capsule_mismatch")
        if (not isinstance(preflight.target_lease, K12RetainedTargetLease)
                or not preflight.target_lease.owned_by(self)
                or preflight.target_lease.reservation_id != ledger.reservation_id
                or not _target_matches_retained_lease(
                    preflight.target, preflight.target_lease, ledger.reservation_id
                )):
            raise ProvenanceError("target_lock_loss")
        if run_authorization is not None:
            if not isinstance(run_authorization, K12QualificationRunAuthorization):
                raise TypeError("typed qualification run authorization required")
            preflight = _replace_preflight(preflight, run_authorization=run_authorization,
                                           run_authorization_digest=run_authorization.identity)
        run_auth = self._coerce_run_authorization(preflight, ledger)
        if (run_auth.reservation_id != preflight.reservation_id
                or run_auth.evidence_origin != self.origin
                or run_auth.ownership_token is not self.__ownership_token):
            raise ProvenanceError("authority_replay")
        snapshot = durable_ledger_snapshot(ledger)
        if (ledger.namespace != "qualification"
                or ledger.reservation_id != preflight.reservation_id
                or ledger.state != "reserved"
                or snapshot["identity"] != preflight.ledger_identity
                or snapshot["root_digest"] != preflight.ledger_root_digest
                or snapshot["output_root_identity"] != preflight.output.root_identity
                or run_auth.body.get("ledger") != dict(snapshot)):
            raise ProvenanceError("authority_replay")
        # Validate with the typed auth in place, while retaining the old field
        # as a detached compatibility receipt.
        if (preflight.run_authorization is None
                or preflight.run_authorization is not run_auth):
            preflight = _replace_preflight(preflight, run_authorization=run_auth,
                                           run_authorization_digest=run_auth.identity)
        else:
            if preflight.run_authorization_digest != run_auth.identity:
                raise ProvenanceError("authority_replay")
        preflight.validate(now=now)
        target_profile = _authenticated_target_semantics(preflight.authenticated_profile)
        observed_target = preflight.target.canonical()
        if any(observed_target[name] != value for name, value in target_profile.items()):
            raise ProvenanceError("target_identity_mismatch")
        body = {
            "artifact_id": "minecraft-k12-live-qualification-execution-authority",
            "artifact_version": 1,
            "schema_version": QUALIFICATION_AUTHORITY,
            "namespace": "qualification",
            "reservation_id": preflight.reservation_id,
            "run_authorization_digest": run_auth.identity,
            "run_authorization": run_auth.receipt(),
            "external_revision_authorization": (
                preflight.external_revision_authorization.receipt()
            ),
            "external_revision_authorization_digest": (
                preflight.external_revision_authorization.identity
            ),
            "capabilities": list(run_auth.capabilities),
            "evidence_origin": run_auth.evidence_origin,
            "checkout": preflight.checkout.canonical(),
            "pull_request": preflight.pull_request.canonical(),
            "pull_request_semantic_tuple": list(preflight.pull_request.semantic_tuple()),
            "source_closure": preflight.source.canonical(),
            "execution_capsule": preflight.capsule.canonical(),
            "profile": {"identity": preflight.profile_identity,
                         "detached_digest": preflight.profile_digest},
            "contracts": dict(sorted(preflight.contracts.items())),
            "schedule_digest": preflight.schedule_digest,
            "environment": preflight.environment.canonical(),
            "target": preflight.target.canonical(),
            "target_profile": target_profile,
            "output": preflight.output.canonical(),
            "ledger": {"identity": preflight.ledger_identity,
                       "root": snapshot["root"],
                       "root_digest": preflight.ledger_root_digest,
                       "namespace": snapshot["namespace"],
                       "reservation_id": snapshot["reservation_id"],
                       "output_root_identity": snapshot["output_root_identity"],
                       "nonce": snapshot["nonce"],
                       "reservation_record_digest": ledger.head_digest},
            "issued_at": now, "expires_at": now + MAX_PR_AGE_SECONDS,
        }
        body["target_lease"] = preflight.target_lease.canonical()
        authority = QualificationExecutionAuthority(
            body, self.__ownership_token, ledger, _AUTHORITY_TOKEN, run_auth, self,
        )
        self.ledger_authority_minted(ledger, authority.identity)
        self.__authorities[id(authority)] = authority
        self.__minted = True
        return authority

    def _safe_quarantine(self, ledger: Any, reason: str) -> None:
        try:
            if self.owns_ledger(ledger) and ledger.state not in {"terminal", "quarantined"}:
                self.quarantine_ledger(ledger, {"reason": reason})
        except Exception:
            # A failed quarantine is still a fail-closed mint failure.  Do not
            # turn a caller-visible provenance error into an implementation leak.
            pass

    def mint_qualification(self, preflight: QualificationPreflight, *, now: int | None = None,
                            ledger: Any,
                            run_authorization: K12QualificationRunAuthorization | None = None
                            ) -> QualificationExecutionAuthority:
        with self.__qualification_mint_lock:
            if self.__qualification_claimed:
                raise ProvenanceError("authority_replay")
            self.__qualification_claimed = True
            try:
                return self._mint_qualification_impl(
                    preflight, now=now, ledger=ledger,
                    run_authorization=run_authorization,
                )
            except Exception as exc:
                self._safe_quarantine(ledger, getattr(exc, "reason", "authority_replay"))
                raise

    issue_qualification_authority = mint_qualification
    mint_qualification_authority = mint_qualification

    def activate_qualification(self, authority: QualificationExecutionAuthority, *,
                               ledger: Any = None) -> ActiveQualificationAuthority:
        if (not isinstance(authority, QualificationExecutionAuthority)
                or not self.owns_authority(authority)):
            raise TypeError("parent-owned qualification authority required")
        ledger = authority.ledger if ledger is None else ledger
        if (not self.owns_ledger(ledger)
                or not _ledger_is(ledger, namespace="qualification", state="first_consume_verified",
                                  reservation=authority.body["reservation_id"])):
            raise ProvenanceError("authority_replay")
        activation = self.ledger_activate(ledger, authority.identity)
        active = ActiveQualificationAuthority(
            authority, activation, self.__ownership_token, _ACTIVE_TOKEN,
        )
        self.__active_qualification_authorities[id(active)] = active
        return active

    def _mint_final_impl(
        self, prerequisites: FinalExecutionPrerequisites,
        qualification_authority: QualificationExecutionAuthority | ActiveQualificationAuthority,
        *, now: int | None, qualification_ledger: Any = None, final_ledger: Any,
        target: TargetLockObservation,
        output: OutputRootObservation,
        target_lease: K12RetainedTargetLease,
    ) -> FinalExecutionAuthority:
        now = self._resolve_time(now)
        from .k12_execution_capsule import DurableLedger
        if not isinstance(prerequisites, FinalExecutionPrerequisites):
            raise TypeError("typed final prerequisites required")
        if not isinstance(qualification_authority, ActiveQualificationAuthority):
            raise TypeError("active parent qualification authority required")
        qualification_execution = qualification_authority.authority
        if (qualification_authority.owner is not self
                or not self.owns_authority(qualification_execution)
                or not isinstance(final_ledger, DurableLedger)
                or not self.owns_ledger(final_ledger)):
            raise TypeError("parent-owned qualification authority and final ledger required")
        if qualification_ledger is None:
            qualification_ledger = qualification_execution.ledger
        if not self.owns_ledger(qualification_ledger):
            raise ProvenanceError("final_prerequisite_mismatch")
        qualification_snapshot = durable_ledger_snapshot(qualification_ledger)
        qualification_bound_ledger = qualification_execution.body["ledger"]
        if any(qualification_snapshot[name] != qualification_bound_ledger[name] for name in (
            "identity", "root", "root_digest", "namespace", "reservation_id",
            "output_root_identity", "nonce",
        )):
            raise ProvenanceError("final_prerequisite_mismatch")
        if (not qualification_ledger.events
                or qualification_bound_ledger.get("reservation_record_digest")
                != qualification_ledger.events[0].digest):
            raise ProvenanceError("final_prerequisite_mismatch")
        if not prerequisites.matches(qualification_execution):
            raise ProvenanceError("final_prerequisite_mismatch")
        attestation = prerequisites.qualification_semantic_attestation
        if (not self.owns_qualification_semantic_attestation(
                    attestation, authority=qualification_authority)
                or attestation.semantic_result != "passed"
                or attestation.terminal_ledger_digest
                    != prerequisites.qualification_terminal_ledger_digest
                or attestation.terminal_receipt_digest
                    != prerequisites.qualification_terminal_receipt_census_digest
                or attestation.terminal_event_digest
                    != prerequisites.qualification_terminal_event_digest
                or attestation.source_aggregate != prerequisites.source_aggregate
                or attestation.profile_digest != prerequisites.profile_digest
                or attestation.contract_set_digest != prerequisites.contract_set_digest
                or attestation.capsule_digest != prerequisites.capsule_digest
                or attestation.environment_digest != canonical_sha256(
                    _deep_thaw(qualification_execution.body["environment"])
                )):
            raise ProvenanceError("final_prerequisite_mismatch")
        if not qualification_authority.current_at(now):
            raise ProvenanceError("authority_replay")
        if attestation.evidence_origin == RUNTIME_VERIFIED_ORIGIN:
            final_origin = RUNTIME_VERIFIED_ORIGIN
        elif attestation.evidence_origin == INJECTED_FAKE_ORIGIN:
            if self.origin != INJECTED_TEST_ORIGIN:
                raise ProvenanceError("final_prerequisite_mismatch")
            final_origin = INJECTED_FAKE_ORIGIN
        else:
            raise ProvenanceError("final_prerequisite_mismatch")
        if (qualification_execution.origin != (
                    RUNTIME_VERIFIED_ORIGIN
                    if final_origin == RUNTIME_VERIFIED_ORIGIN else INJECTED_TEST_ORIGIN
                )):
            raise ProvenanceError("final_prerequisite_mismatch")
        if not _ledger_is(qualification_ledger, namespace="qualification", state="terminal",
                          reservation=qualification_execution.body["reservation_id"]):
            raise ProvenanceError("final_prerequisite_mismatch")
        if qualification_ledger.head_digest != prerequisites.qualification_terminal_ledger_digest:
            raise ProvenanceError("final_prerequisite_mismatch")
        terminal = qualification_ledger.events[-1].payload
        if (terminal.get("result") != "passed"
                or terminal.get("semantic_projection_digest")
                    != prerequisites.qualification_semantic_projection_digest
                or attestation.probe_terminal_digest
                    != prerequisites.qualification_probe_projection_digest
                or terminal.get("semantic_verifier_identity")
                    != attestation.semantic_verifier_identity):
            raise ProvenanceError("final_prerequisite_mismatch")
        if (target is None or output is None
                or not isinstance(target, TargetLockObservation)
                or not isinstance(output, OutputRootObservation)
                or not isinstance(target_lease, K12RetainedTargetLease)
                or not target_lease.owned_by(self)):
            raise TypeError("typed final target, output, and retained lease required")
        qualification_target = qualification_execution.body.get("target")
        authenticated_target = qualification_execution.body.get("target_profile")
        final_target_semantics = _target_semantics(target)
        final_target_canonical = target.canonical()
        if (not isinstance(qualification_target, Mapping)
                or not isinstance(authenticated_target, Mapping)
                or final_target_semantics != _target_semantics(qualification_target)
                or _deep_thaw(authenticated_target) != {
                    name: final_target_canonical[name]
                    for name in _AUTHENTICATED_TARGET_FIELDS
                }):
            raise ProvenanceError("target_identity_mismatch")
        target.validate(final_ledger.reservation_id)
        output.validate()
        target_lease.revalidate()
        if (target_lease.reservation_id != final_ledger.reservation_id
                or not _target_matches_retained_lease(
                    target, target_lease, final_ledger.reservation_id
                )
                or output.root_identity != final_ledger.output_root_identity):
            raise ProvenanceError("target_lock_loss")
        final_snapshot = durable_ledger_snapshot(final_ledger)
        if (final_ledger.namespace != "final" or final_ledger.state != "reserved"
                or not final_ledger.reservation_id
                or final_snapshot["identity"] != qualification_snapshot["identity"]
                or final_snapshot["reservation_id"] == qualification_snapshot["reservation_id"]
                or final_snapshot["root"] == qualification_snapshot["root"]
                or final_snapshot["root_digest"] == qualification_snapshot["root_digest"]
                or final_snapshot["nonce"] == qualification_snapshot["nonce"]
                or final_snapshot["output_root_identity"]
                    == qualification_snapshot["output_root_identity"]):
            raise ProvenanceError("authority_replay")
        final_run = self.mint_final_run_authorization(
            reservation_id=final_ledger.reservation_id,
            output_root_identity=final_snapshot["output_root_identity"],
            nonce=final_snapshot["nonce"],
            profile_digest=prerequisites.profile_digest,
            qualification_authority_digest=qualification_execution.identity,
            qualification_semantic_attestation=attestation,
            evidence_origin=final_origin,
            ledger=final_ledger, now=now,
            external_revision_authorization=(
                qualification_execution.run_authorization
                .external_revision_authorization
            ),
        )
        if final_run.identity == qualification_execution.run_authorization.identity:
            raise ProvenanceError("authority_replay")
        body = {
            "artifact_id": "minecraft-k12-live-final-execution-authority",
            "artifact_version": 1,
            "schema_version": FINAL_AUTHORITY,
            "namespace": "final",
            "reservation_id": final_ledger.reservation_id,
            "run_authorization_digest": final_run.identity,
            "run_authorization": final_run.receipt(),
            "capabilities": list(final_run.capabilities),
            "evidence_origin": final_origin,
            "qualification_authority_digest": qualification_execution.identity,
            "qualification_semantic_attestation_digest": attestation.identity,
            "qualification_semantic_projection_digest":
                prerequisites.qualification_semantic_projection_digest,
            "qualification_probe_projection_digest":
                prerequisites.qualification_probe_projection_digest,
            "qualification_terminal_receipt_census_digest":
                prerequisites.qualification_terminal_receipt_census_digest,
            "qualification_terminal_event_digest":
                prerequisites.qualification_terminal_event_digest,
            "qualification_terminal_ledger_digest": prerequisites.qualification_terminal_ledger_digest,
            "checkout": _deep_thaw(qualification_execution.body["checkout"]),
            "source_closure": _deep_thaw(qualification_execution.body["source_closure"]),
            "execution_capsule": _deep_thaw(qualification_execution.body["execution_capsule"]),
            "environment": _deep_thaw(qualification_execution.body["environment"]),
            "target": target.canonical(),
            "target_profile": _deep_thaw(qualification_execution.body["target_profile"]),
            "output": output.canonical(),
            "target_lease": target_lease.canonical(),
            "source_aggregate": prerequisites.source_aggregate,
            "profile_digest": prerequisites.profile_digest,
            "contract_set_digest": prerequisites.contract_set_digest,
            "capsule_digest": prerequisites.capsule_digest,
            "pull_request_semantic_tuple": list(
                qualification_execution.body["pull_request_semantic_tuple"]),
            "external_revision_authorization": (
                qualification_execution.run_authorization
                .external_revision_authorization.receipt()
            ),
            "external_revision_authorization_digest": (
                qualification_execution.run_authorization
                .external_revision_authorization.identity
            ),
            "ledger": {
                "identity": final_snapshot["identity"],
                "root": final_snapshot["root"],
                "root_digest": final_snapshot["root_digest"],
                "namespace": final_snapshot["namespace"],
                "reservation_id": final_snapshot["reservation_id"],
                "output_root_identity": final_snapshot["output_root_identity"],
                "nonce": final_snapshot["nonce"],
                "reservation_record_digest": final_ledger.head_digest,
            },
            "issued_at": now, "expires_at": now + MAX_PR_AGE_SECONDS,
        }
        authority = FinalExecutionAuthority(
            body, self.__ownership_token, final_ledger, _AUTHORITY_TOKEN, final_run, self,
        )
        self.ledger_authority_minted(final_ledger, authority.identity)
        self.__authorities[id(authority)] = authority
        self.__final_minted = True
        return authority

    def mint_final(
        self, prerequisites: FinalExecutionPrerequisites,
         qualification_authority: QualificationExecutionAuthority | ActiveQualificationAuthority,
         *, now: int | None = None, qualification_ledger: Any = None, final_ledger: Any,
        target: TargetLockObservation,
        output: OutputRootObservation,
        target_lease: K12RetainedTargetLease,
    ) -> FinalExecutionAuthority:
        with self.__final_mint_lock:
            if self.__final_claimed:
                raise ProvenanceError("authority_replay")
            self.__final_claimed = True
            try:
                return self._mint_final_impl(
                    prerequisites, qualification_authority, now=now,
                    qualification_ledger=qualification_ledger,
                    final_ledger=final_ledger, target=target, output=output,
                    target_lease=target_lease,
                )
            except Exception as exc:
                self._safe_quarantine(final_ledger, getattr(exc, "reason", "authority_replay"))
                raise

    issue_final_authority = mint_final
    mint_final_authority = mint_final

    def activate_final(self, authority: FinalExecutionAuthority, *,
                       ledger: Any = None) -> ActiveFinalAuthority:
        if (not isinstance(authority, FinalExecutionAuthority)
                or not self.owns_authority(authority)):
            raise TypeError("parent-owned final authority required")
        ledger = authority.ledger if ledger is None else ledger
        if (not self.owns_ledger(ledger)
                or not _ledger_is(ledger, namespace="final", state="first_consume_verified",
                                  reservation=authority.body["reservation_id"])):
            raise ProvenanceError("authority_replay")
        activation = self.ledger_activate(ledger, authority.identity)
        return ActiveFinalAuthority(
            authority, activation, self.__ownership_token, _ACTIVE_TOKEN,
        )


class InjectedTestController(ParentExecutionAuthority):
    """Parent-minted deterministic controller for non-runtime test graphs."""

    def __init__(self, parent: ParentExecutionAuthority, token: object = None) -> None:
        if not isinstance(parent, ParentExecutionAuthority):
            raise TypeError("parent execution authority required")
        if token is not _INJECTED_CONTROLLER_TOKEN:
            raise TypeError("injected-test controller is parent-minted")
        super().__init__(
            _origin=INJECTED_TEST_ORIGIN,
            _owner=parent,
            _token=_INJECTED_CONTROLLER_TOKEN,
        )

    @property
    def parent(self) -> ParentExecutionAuthority:
        return self.owner

    def _require_owned(self, authority: Any) -> None:
        execution = authority.authority if isinstance(
            authority, (ActiveQualificationAuthority, ActiveFinalAuthority)
        ) else authority
        if not self.owns_authority(execution):
            raise ProvenanceError("authority_replay")

    def verify_qualification_first_consume(
            self, authority: QualificationExecutionAuthority,
            observation: FirstConsumeObservation, *, now: int, ledger: Any,
    ) -> ActiveQualificationAuthority:
        self._require_owned(authority)
        return verify_qualification_first_consume(
            authority, observation, now=now, ledger=ledger,
        )

    def verify_final_first_consume(
            self, authority: FinalExecutionAuthority,
            observation: FinalFirstConsumeObservation, *, now: int, ledger: Any,
    ) -> ActiveFinalAuthority:
        self._require_owned(authority)
        return verify_final_first_consume(
            authority, observation, now=now, ledger=ledger,
        )

    verify_first_consume_qualification = verify_qualification_first_consume
    verify_first_consume_final = verify_final_first_consume


InjectedTestAuthorityController = InjectedTestController
InjectedTestVerifier = InjectedTestController
InjectedTestExecutionController = InjectedTestController


def _replace_preflight(value: QualificationPreflight, **changes: Any) -> QualificationPreflight:
    values = {name: getattr(value, name) for name in value.__dataclass_fields__}
    values.update(changes)
    return QualificationPreflight(**values)


def _verify_qualification_first_consume(
        authority: QualificationExecutionAuthority, observation: FirstConsumeObservation,
        *, now: int, ledger: Any, activate: bool) -> Any:
    if not isinstance(authority, QualificationExecutionAuthority):
        raise TypeError("typed qualification authority required")
    if not isinstance(observation, FirstConsumeObservation):
        raise TypeError("typed first-consume observation required")
    bound = _deep_thaw(authority.body)
    reservation_id = bound["reservation_id"]
    owner = authority.owner
    if isinstance(owner, ParentExecutionAuthority):
        now = owner._resolve_time(now)
    try:
        current_ledger = durable_ledger_snapshot(ledger)
        bound_ledger = bound["ledger"]
        if (not isinstance(owner, ParentExecutionAuthority)
                or not owner.owns_authority(authority)
                or ledger is not authority.ledger or not owner.owns_ledger(ledger)
                or not authority.current_at(now)
                or not _ledger_is(ledger, namespace="qualification", state="authority_minted",
                                  reservation=reservation_id)
                or any(current_ledger[name] != bound_ledger[name] for name in (
                    "identity", "root", "root_digest", "namespace", "reservation_id",
                    "output_root_identity", "nonce",
                ))
                or bound_ledger.get("reservation_record_digest")
                    != current_ledger["reservation_record_digest"]
                or bound.get("run_authorization_digest")
                    != authority.run_authorization.identity
                or bound.get("run_authorization")
                    != authority.run_authorization.receipt()):
            raise ProvenanceError("authority_replay")
        revision_authorization = authority.run_authorization.external_revision_authorization
        if (not revision_authorization.owned_by(owner)
                or bound.get("external_revision_authorization")
                    != revision_authorization.receipt()
                or bound.get("external_revision_authorization_digest")
                    != revision_authorization.identity):
            raise ProvenanceError("authority_replay")
        observation.checkout.validate(revision_authorization)
        if observation.checkout.canonical() != bound["checkout"]:
            raise ProvenanceError("first_consume_mismatch")
        observation.pull_request.validate(revision_authorization, now=now)
        if tuple(observation.pull_request.semantic_tuple()) \
                != tuple(bound["pull_request_semantic_tuple"]):
            raise ProvenanceError("pr_semantic_mismatch")
        observation.environment.validate()
        observation.target.validate(reservation_id)
        observation.output.validate()
        if (not observation.capsule.owned_by(owner)
                or not observation.capsule.verify()):
            raise ProvenanceError("capsule_mismatch")
        if observation.capsule.canonical() != bound["execution_capsule"]:
            raise ProvenanceError("first_consume_mismatch")
        if (observation.source.origin != authority.origin
                or not observation.source.owned_by(owner)
                or observation.source.canonical() != bound["source_closure"]):
            raise ProvenanceError("first_consume_mismatch")
        if not revision_authorization.matches_source(observation.source):
            raise ProvenanceError("git_tree_mismatch")
        if observation.environment.canonical() != bound["environment"]:
            raise ProvenanceError("first_consume_mismatch")
        if observation.target.canonical() != bound["target"]:
            raise ProvenanceError("first_consume_mismatch")
        if observation.output.canonical() != bound["output"]:
            raise ProvenanceError("first_consume_mismatch")
        if (not isinstance(observation.target_lease, K12RetainedTargetLease)
                or not observation.target_lease.owned_by(owner)
                or observation.target_lease.reservation_id != reservation_id
                or not _target_matches_retained_lease(
                    observation.target, observation.target_lease, reservation_id
                )):
            raise ProvenanceError("target_lock_loss")
        observation.target_lease.revalidate()
        if observation.target_lease.canonical() != bound["target_lease"]:
            raise ProvenanceError("target_lock_loss")
    except Exception as exc:
        mapped = exc if isinstance(exc, ProvenanceError) else ProvenanceError(
            getattr(exc, "reason", "first_consume_mismatch")
        )
        if isinstance(owner, ParentExecutionAuthority):
            owner.quarantine_ledger(
                ledger, {"reason": mapped.reason, "authority": authority.identity},
            )
        if mapped is not exc:
            raise mapped from exc
        raise
    try:
        observed_digest = canonical_sha256(observation.canonical())
        if activate:
            owner.ledger_first_consume_verified(
                ledger, authority.identity, observed_digest,
            )
            return owner.activate_qualification(authority, ledger=ledger)
        return owner.ledger_first_consume_verified(
            ledger, authority.identity, observed_digest,
        )
    except Exception as exc:
        reason = getattr(exc, "reason", "first_consume_mismatch")
        if isinstance(owner, ParentExecutionAuthority):
            owner.quarantine_ledger(
                ledger, {"reason": reason, "authority": authority.identity},
            )
        raise


def verify_first_consume(authority: QualificationExecutionAuthority,
                         observation: FirstConsumeObservation, *, now: int, ledger: Any) -> str:
    """Compatibility `/1` spelling: consume, but do not activate."""
    return _verify_qualification_first_consume(authority, observation, now=now,
                                               ledger=ledger, activate=False)


def verify_qualification_first_consume(
        authority: QualificationExecutionAuthority, observation: FirstConsumeObservation,
        *, now: int, ledger: Any) -> ActiveQualificationAuthority:
    return _verify_qualification_first_consume(authority, observation, now=now,
                                               ledger=ledger, activate=True)


qualification_first_consume = verify_qualification_first_consume
first_consume_qualification = verify_qualification_first_consume


def verify_final_first_consume(
        authority: FinalExecutionAuthority,
        observation: FinalFirstConsumeObservation,
        *, now: int, ledger: Any,
) -> ActiveFinalAuthority:
    if not isinstance(authority, FinalExecutionAuthority):
        raise TypeError("typed final authority required")
    if not isinstance(observation, FinalFirstConsumeObservation):
        raise TypeError("typed final first-consume observation required")
    bound = _deep_thaw(authority.body)
    owner = authority.owner
    if isinstance(owner, ParentExecutionAuthority):
        now = owner._resolve_time(now)
    try:
        current_ledger = durable_ledger_snapshot(ledger)
        bound_ledger = bound["ledger"]
        if (not isinstance(owner, ParentExecutionAuthority)
                or not owner.owns_authority(authority)
                or ledger is not authority.ledger or not owner.owns_ledger(ledger)
                or not authority.current_at(now)
                or not _ledger_is(ledger, namespace="final", state="authority_minted",
                                  reservation=bound["reservation_id"])
                or any(current_ledger[name] != bound_ledger[name] for name in (
                    "identity", "root", "root_digest", "namespace", "reservation_id",
                    "output_root_identity", "nonce",
                ))
                or bound_ledger.get("reservation_record_digest")
                    != current_ledger["reservation_record_digest"]
                or bound.get("run_authorization_digest")
                    != authority.run_authorization.identity
                or bound.get("run_authorization")
                    != authority.run_authorization.receipt()):
            raise ProvenanceError("authority_replay")
        if observation.prerequisites.qualification_authority_digest \
                != bound["qualification_authority_digest"]:
            raise ProvenanceError("first_consume_mismatch")
        expected = {name: bound.get(name) for name in (
            "qualification_semantic_projection_digest",
            "qualification_probe_projection_digest",
            "qualification_terminal_receipt_census_digest",
            "qualification_terminal_event_digest",
            "qualification_terminal_ledger_digest", "source_aggregate",
            "profile_digest", "contract_set_digest", "capsule_digest",
            "qualification_semantic_attestation_digest",
        )}
        observed = {name: getattr(observation.prerequisites, name)
                    if hasattr(observation.prerequisites, name)
                    else None for name in expected}
        if observed != expected:
            raise ProvenanceError("first_consume_mismatch")
        attestation = observation.prerequisites.qualification_semantic_attestation
        if (not owner.owns_qualification_semantic_attestation(attestation)
                or not owner.qualification_authority_is_current_for_attestation(
                    attestation, now=now,
                )
                or attestation.identity
                    != bound["qualification_semantic_attestation_digest"]
                or authority.run_authorization.body.get(
                    "qualification_semantic_attestation_digest"
                ) != attestation.identity):
            raise ProvenanceError("final_prerequisite_mismatch")
        if (attestation.qualification_authority_digest
                != observation.prerequisites.qualification_authority_digest
                or attestation.evidence_origin != bound["evidence_origin"]
                or attestation.semantic_projection_digest
                    != observation.prerequisites.qualification_semantic_projection_digest
                or attestation.probe_terminal_digest
                    != observation.prerequisites.qualification_probe_projection_digest
                or attestation.terminal_receipt_digest
                    != observation.prerequisites.qualification_terminal_receipt_census_digest
                or attestation.terminal_event_digest
                    != observation.prerequisites.qualification_terminal_event_digest
                or attestation.terminal_ledger_digest
                    != observation.prerequisites.qualification_terminal_ledger_digest):
            raise ProvenanceError("final_prerequisite_mismatch")
        revision_authorization = authority.run_authorization.external_revision_authorization
        if (not revision_authorization.owned_by(owner)
                or bound.get("external_revision_authorization")
                    != revision_authorization.receipt()
                or bound.get("external_revision_authorization_digest")
                    != revision_authorization.identity):
            raise ProvenanceError("first_consume_mismatch")
        observation.checkout.validate(revision_authorization)
        if observation.checkout.canonical() != bound["checkout"]:
            raise ProvenanceError("first_consume_mismatch")
        expected_source_origin = (
            RUNTIME_VERIFIED_ORIGIN
            if attestation.evidence_origin == RUNTIME_VERIFIED_ORIGIN
            else INJECTED_TEST_ORIGIN
        )
        if (observation.source.origin != expected_source_origin
                or not observation.source.owned_by(owner)
                or observation.source.canonical() != bound["source_closure"]):
            raise ProvenanceError("first_consume_mismatch")
        if observation.source.aggregate_sha256 != bound["source_closure"]["aggregate_sha256"]:
            raise ProvenanceError("first_consume_mismatch")
        if not revision_authorization.matches_source(observation.source):
            raise ProvenanceError("git_tree_mismatch")
        observation.environment.validate()
        if observation.environment.canonical() != bound["environment"]:
            raise ProvenanceError("first_consume_mismatch")
        if (not observation.capsule.owned_by(owner)
                or not observation.capsule.verify()
                or observation.capsule.canonical() != bound["execution_capsule"]):
            raise ProvenanceError("capsule_mismatch")
        observation.target.validate(bound["reservation_id"])
        if (observation.target.canonical() != bound["target"]
                or not _target_matches_retained_lease(
                    observation.target, observation.target_lease, bound["reservation_id"]
                )):
            raise ProvenanceError("target_lock_loss")
        observation.output.validate()
        if observation.output.canonical() != bound["output"]:
            raise ProvenanceError("first_consume_mismatch")
        observation.pull_request.validate(revision_authorization, now=now)
        if tuple(observation.pull_request.semantic_tuple()) \
                != tuple(bound["pull_request_semantic_tuple"]):
            raise ProvenanceError("pr_semantic_mismatch")
        if (not isinstance(observation.target_lease, K12RetainedTargetLease)
                or not observation.target_lease.owned_by(owner)
                or observation.target_lease.reservation_id != bound["reservation_id"]):
            raise ProvenanceError("target_lock_loss")
        observation.target_lease.revalidate()
        if observation.target_lease.canonical() != bound["target_lease"]:
            raise ProvenanceError("target_lock_loss")
    except Exception as exc:
        mapped = exc if isinstance(exc, ProvenanceError) else ProvenanceError(
            getattr(exc, "reason", "first_consume_mismatch")
        )
        if isinstance(owner, ParentExecutionAuthority):
            owner.quarantine_ledger(
                ledger, {"reason": mapped.reason, "authority": authority.identity},
            )
        if mapped is not exc:
            raise mapped from exc
        raise
    try:
        digest = canonical_sha256(observation.canonical())
        owner.ledger_first_consume_verified(ledger, authority.identity, digest)
        return owner.activate_final(authority, ledger=ledger)
    except Exception as exc:
        reason = getattr(exc, "reason", "first_consume_mismatch")
        if isinstance(owner, ParentExecutionAuthority):
            owner.quarantine_ledger(
                ledger, {"reason": reason, "authority": authority.identity},
            )
        raise


final_first_consume = verify_final_first_consume
first_consume_final = verify_final_first_consume
verify_final_first_consume_recheck = verify_final_first_consume


def random_reservation_id() -> str:
    return secrets.token_hex(32)


__all__ = [
    "ActiveFinalAuthority", "ActiveQualificationAuthority", "AuthorityBinding",
    "AUTHORIZATION_ORIGINS", "INJECTED_FAKE_ORIGIN", "INJECTED_TEST_ORIGIN",
    "LIVE_FINAL_NAMESPACE", "LIVE_QUALIFICATION_NAMESPACE", "QUALIFICATION_PROBE_NAMESPACE",
    "CheckoutObservation", "ExternalRevisionAuthorization",
    "external_revision_attestation_payload", "FINAL_AUTHORITY",
    "FINAL_RUN_AUTHORIZATION", "FinalExecutionAuthority", "FinalExecutionPrerequisites",
    "FinalFirstConsumeObservation",
    "FirstConsumeObservation", "K12FinalRunAuthorization", "K12QualificationRunAuthorization",
    "K12RetainedTargetLease", "LiveQualificationEvidence", "MAX_PR_AGE_SECONDS",
    "K12LiveQualificationEvidence", "QualificationEvidenceProtocol",
    "MISMATCH_CLASSIFICATION", "MismatchClass", "OutputRootObservation", "PROFILE_V2",
    "OPERATIONAL_PROVENANCES", "ParentExecutionAuthority", "InjectedTestController",
    "InjectedTestAuthorityController", "InjectedTestExecutionController",
    "InjectedTestVerifier", "ProvenanceError",
    "PullRequestObservation", "RUNTIME_VERIFIED_ORIGIN", "SOURCE_SEMANTIC_CLASSES",
    "QUALIFICATION_AUTHORITY", "QUALIFICATION_RUN_AUTHORIZATION",
    "QualificationCoordinateExecutionReceipt", "QualificationExecutionStageReceipt",
    "QualificationExecutionBoundaryArtifact", "QualificationResetBoundaryArtifact",
    "QualificationProviderBoundaryArtifact", "QualificationPermitBoundaryArtifact",
    "QualificationEffectBoundaryArtifact", "QualificationOracleBoundaryArtifact",
    "QualificationContainmentBoundaryArtifact",
    "QualificationProbeContainmentBoundaryArtifact",
    "QualificationExecutionAuthority", "QualificationPreflight",
    "SourceClosure", "SourceRecord", "SubmoduleObservation", "TargetLockObservation",
    "authority_owns_profile", "final_first_consume", "first_consume_final",
    "first_consume_qualification", "qualification_first_consume", "random_reservation_id",
    "git_blob_oid", "raw_sha256", "durable_ledger_root_digest", "durable_ledger_snapshot",
    "retained_target_binding", "source_closure_from_observations",
    "verify_final_first_consume",
    "verify_first_consume", "verify_qualification_first_consume",
    "refresh_pull_request_observation",
    "verify_final_first_consume_recheck",
]
