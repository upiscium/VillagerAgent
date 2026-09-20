"""Immutable execution capsules and parent-controlled durable ledgers.

This module is deliberately observation-only.  It does not inspect the live
machine, import packages, start a process, or resolve a target lock.  A caller
must provide the materialized observations and the parent authority owns every
semantic ledger transition.
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Sequence

from benchmarks.common.eac.canonical import canonical_bytes, canonical_sha256
from .k12_execution_provenance import ProvenanceError


_RAW_SHA256 = re.compile(r"[0-9a-f]{64}")
_CANONICAL_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
LEDGER_IDENTITY = "minecraft-k12-live-execution-ledger/1"
CAPSULE_IDENTITY = "minecraft-k12-live-execution-capsule/1"

MINIMUM_CAPSULE_CATEGORIES = frozenset({
    "repo", "interpreter", "stdlib", "import_roots", "distributions", "native", "startup",
})
CONDITIONAL_CAPSULE_CATEGORIES = frozenset({"node", "java"})
_CAPSULE_MINT_TOKEN = object()
_CONTROLLER_KEY_BYTES = 32


class CapsuleError(ProvenanceError):
    """A capsule, ledger, or retained durable record is invalid."""


def _raw_digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _require_digest(value: str, reason: str = "capsule_mismatch") -> str:
    if not isinstance(value, str) or _RAW_SHA256.fullmatch(value) is None:
        raise CapsuleError(reason)
    return value


def _require_canonical_digest(value: str, reason: str = "authority_replay") -> str:
    if not isinstance(value, str) or _CANONICAL_SHA256.fullmatch(value) is None:
        raise CapsuleError(reason)
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (str, bytes, bool, int, float)) or value is None:
        return value
    raise TypeError("ledger payload contains an unsupported value")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class CapsuleRecord:
    """One immutable, content-addressed capsule component."""

    identity: str
    content_digest: str
    kind: str
    immutable: bool = True
    writable: bool = False
    lazy: bool = False

    _KIND_ALIASES = {
        "source": "repo",
        "repository": "repo",
        "python": "interpreter",
        "stdlib_roots": "stdlib",
        "import-root": "import_roots",
        "import-root-set": "import_roots",
        "imports": "import_roots",
        "distribution": "distributions",
        "native_dependencies": "native",
        "native-dependencies": "native",
        "startup_hooks": "startup",
        "startup_behavior": "startup",
        "startup-behavior": "startup",
    }

    def __post_init__(self) -> None:
        if type(self.identity) is not str or not self.identity or len(self.identity) > 512:
            raise CapsuleError("capsule_mismatch")
        if type(self.kind) is not str:
            raise CapsuleError("capsule_mismatch")
        kind = self._KIND_ALIASES.get(self.kind, self.kind)
        if kind not in (MINIMUM_CAPSULE_CATEGORIES | CONDITIONAL_CAPSULE_CATEGORIES):
            raise CapsuleError("capsule_mismatch")
        object.__setattr__(self, "kind", kind)
        _require_digest(self.content_digest)
        if self.immutable is not True or self.writable is not False or self.lazy is not False:
            raise CapsuleError("capsule_mismatch")

    def canonical(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "content_digest": self.content_digest,
            "kind": self.kind,
            "immutable": self.immutable,
            "writable": self.writable,
            "lazy": self.lazy,
        }


@dataclass(frozen=True, slots=True, init=False)
class ExecutionCapsule:
    """A sealed minimum closure for one execution authority.

    The seven minimum categories are not descriptive metadata: each must have
    an immutable record.  Node and Java records are required only when the
    corresponding conditional bit is set.  All import and startup escape
    hatches are explicit rejection bits so a caller cannot omit an observation
    by relying on a default import mechanism.
    """

    MINIMUM_CATEGORIES = MINIMUM_CAPSULE_CATEGORIES
    CONDITIONAL_CATEGORIES = CONDITIONAL_CAPSULE_CATEGORIES

    identity: str
    source_aggregate: str
    immutable_store_path_digest: str
    recursive_store_closure_digest: str
    interpreter_digest: str
    import_roots_digest: str
    records: tuple[CapsuleRecord, ...]
    immutable: bool
    read_only: bool
    outside_worktree: bool
    user_site_enabled: bool
    editable_installs: bool
    unapproved_pth: bool
    sitecustomize: bool
    startup_hooks: bool
    writable_worktree_imports: bool
    capsule_digest: str = field(init=False)
    node_required: bool = False
    java_required: bool = False
    lazy_imports: bool = False
    writable_import_roots: bool = False
    writable_lazy_imports: bool = False
    writable_imports: bool = False
    _owner: Any = field(init=False, repr=False, compare=False)

    def __init__(
        self,
        identity: str,
        source_aggregate: str,
        immutable_store_path_digest: str,
        recursive_store_closure_digest: str,
        interpreter_digest: str,
        import_roots_digest: str,
        records: tuple[CapsuleRecord, ...],
        immutable: bool,
        read_only: bool,
        outside_worktree: bool,
        user_site_enabled: bool,
        editable_installs: bool,
        unapproved_pth: bool,
        sitecustomize: bool,
        startup_hooks: bool,
        writable_worktree_imports: bool,
        node_required: bool = False,
        java_required: bool = False,
        lazy_imports: bool = False,
        writable_import_roots: bool = False,
        writable_lazy_imports: bool = False,
        writable_imports: bool = False,
        *,
        token: object = None,
        owner: Any = None,
    ) -> None:
        if token is not _CAPSULE_MINT_TOKEN or owner is None:
            raise TypeError("execution capsules are parent-minted")
        for name, value in (
            ("identity", identity),
            ("source_aggregate", source_aggregate),
            ("immutable_store_path_digest", immutable_store_path_digest),
            ("recursive_store_closure_digest", recursive_store_closure_digest),
            ("interpreter_digest", interpreter_digest),
            ("import_roots_digest", import_roots_digest),
            ("records", records),
            ("immutable", immutable),
            ("read_only", read_only),
            ("outside_worktree", outside_worktree),
            ("user_site_enabled", user_site_enabled),
            ("editable_installs", editable_installs),
            ("unapproved_pth", unapproved_pth),
            ("sitecustomize", sitecustomize),
            ("startup_hooks", startup_hooks),
            ("writable_worktree_imports", writable_worktree_imports),
            ("node_required", node_required),
            ("java_required", java_required),
            ("lazy_imports", lazy_imports),
            ("writable_import_roots", writable_import_roots),
            ("writable_lazy_imports", writable_lazy_imports),
            ("writable_imports", writable_imports),
        ):
            object.__setattr__(self, name, value)
        object.__setattr__(self, "_owner", owner)
        self.__post_init__()

    def __post_init__(self) -> None:
        if getattr(self, "_owner", None) is None:
            raise CapsuleError("capsule_mismatch")
        for value in (
            self.source_aggregate, self.immutable_store_path_digest,
            self.recursive_store_closure_digest, self.interpreter_digest,
            self.import_roots_digest,
        ):
            _require_digest(value)
        if self.identity != CAPSULE_IDENTITY or self.immutable is not True or self.read_only is not True:
            raise CapsuleError("capsule_mismatch")
        if self.outside_worktree is not True:
            raise CapsuleError("capsule_mismatch")
        if any(value is not False for value in (
            self.user_site_enabled, self.editable_installs, self.unapproved_pth,
            self.sitecustomize, self.startup_hooks, self.writable_worktree_imports,
            self.lazy_imports, self.writable_import_roots, self.writable_lazy_imports,
            self.writable_imports,
        )):
            raise CapsuleError("capsule_mismatch")
        if type(self.node_required) is not bool or type(self.java_required) is not bool:
            raise CapsuleError("capsule_mismatch")
        if type(self.records) not in (tuple, list) or not self.records:
            raise CapsuleError("capsule_mismatch")
        if any(not isinstance(record, CapsuleRecord) for record in self.records):
            raise CapsuleError("capsule_mismatch")
        identities = tuple(record.identity for record in self.records)
        if len(identities) != len(set(identities)):
            raise CapsuleError("capsule_mismatch")
        category_counts: dict[str, int] = {}
        for record in self.records:
            category_counts[record.kind] = category_counts.get(record.kind, 0) + 1
        categories = {record.kind for record in self.records}
        required = set(MINIMUM_CAPSULE_CATEGORIES)
        if self.node_required:
            required.add("node")
        if self.java_required:
            required.add("java")
        if (not required <= categories
                or any(category_counts.get(category, 0) != 1 for category in required)
                or any(count != 1 for count in category_counts.values())):
            raise CapsuleError("capsule_mismatch")
        records_by_category = {record.kind: record for record in self.records}
        if (
            records_by_category["repo"].content_digest != self.source_aggregate
            or records_by_category["interpreter"].content_digest != self.interpreter_digest
            or records_by_category["import_roots"].content_digest != self.import_roots_digest
        ):
            raise CapsuleError("capsule_mismatch")
        # A lazy import is rejected even when its directory is nominally
        # outside the worktree.  The category and digest cannot be replaced by
        # a runtime import side effect.
        if any(record.lazy or record.writable for record in self.records):
            raise CapsuleError("capsule_mismatch")
        object.__setattr__(self, "records", tuple(sorted(self.records, key=lambda item: item.identity)))
        object.__setattr__(self, "capsule_digest", _raw_digest(self.unsigned()))

    def unsigned(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "source_aggregate": self.source_aggregate,
            "immutable_store_path_digest": self.immutable_store_path_digest,
            "recursive_store_closure_digest": self.recursive_store_closure_digest,
            "interpreter_digest": self.interpreter_digest,
            "import_roots_digest": self.import_roots_digest,
            "records": [record.canonical() for record in self.records],
            "immutable": self.immutable,
            "read_only": self.read_only,
            "outside_worktree": self.outside_worktree,
            "user_site_enabled": self.user_site_enabled,
            "editable_installs": self.editable_installs,
            "unapproved_pth": self.unapproved_pth,
            "sitecustomize": self.sitecustomize,
            "startup_hooks": self.startup_hooks,
            "writable_worktree_imports": self.writable_worktree_imports,
            "node_required": self.node_required,
            "java_required": self.java_required,
            "lazy_imports": self.lazy_imports,
            "writable_import_roots": self.writable_import_roots,
            "writable_lazy_imports": self.writable_lazy_imports,
            "writable_imports": self.writable_imports,
        }

    def canonical(self) -> dict[str, Any]:
        return {**self.unsigned(), "capsule_digest": self.capsule_digest}

    @property
    def categories(self) -> frozenset[str]:
        return frozenset(record.kind for record in self.records)

    def owned_by(self, owner: Any) -> bool:
        """Authenticate the parent that minted and registered this capsule."""
        return self._owner is owner and owner is not None and bool(
            getattr(owner, "owns_capsule", lambda _capsule: False)(self)
        )

    def verify(self) -> bool:
        declared = self.capsule_digest
        try:
            # Re-run the semantic checks against a tamperable object before
            # checking its detached digest.
            self.__post_init__()
        except (CapsuleError, TypeError, ValueError):
            return False
        observed = _raw_digest(self.unsigned())
        object.__setattr__(self, "capsule_digest", declared)
        return declared == observed


def _normalize_capsule_values(values: Mapping[str, Any]) -> dict[str, Any]:
    aliases = {
        "requires_node": "node_required",
        "requires_java": "java_required",
        "node": "node_required",
        "java": "java_required",
        "writable_import_root": "writable_import_roots",
        "writable_import_roots_allowed": "writable_import_roots",
        "writable": "writable_imports",
        "writable_import_escape": "writable_imports",
        "writable_import_escape_rejection": "writable_imports",
        "writable_import_fallback": "writable_imports",
        "writable_imports_allowed": "writable_imports",
        "allow_writable_imports": "writable_imports",
        "lazy_import": "lazy_imports",
        "lazy": "lazy_imports",
        "lazy_import_rejection": "lazy_imports",
        "lazy_import_escape": "lazy_imports",
        "lazy_import_escape_rejection": "lazy_imports",
        "lazy_import_fallback": "lazy_imports",
        "lazy_imports_allowed": "lazy_imports",
        "allow_lazy_imports": "lazy_imports",
        "lazy_import_fallback_allowed": "lazy_imports",
    }
    normalized = dict(values)
    for source, target in aliases.items():
        if source in normalized:
            if target in normalized and normalized[target] != normalized[source]:
                raise CapsuleError("capsule_mismatch")
            normalized[target] = normalized.pop(source)
    return normalized


def mint_capsule(*, token: object, owner: Any, **values: Any) -> ExecutionCapsule:
    """Mint a capsule through the parent-only capability boundary."""
    if token is not _CAPSULE_MINT_TOKEN or owner is None:
        raise TypeError("execution capsules are parent-minted")
    normalized = _normalize_capsule_values(values)
    normalized["token"] = token
    normalized["owner"] = owner
    return ExecutionCapsule(**normalized)


def attest_capsule(**values: Any) -> ExecutionCapsule:
    """Reject caller self-attestation unless the internal mint capability is supplied."""
    token = values.pop("_mint_token", None)
    if token is None:
        token = values.pop("token", None)
    owner = values.pop("_owner", None)
    if owner is None:
        owner = values.pop("owner", None)
    return mint_capsule(token=token, owner=owner, **values)


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    ordinal: int
    previous_digest: str
    state: str
    payload: Mapping[str, Any]
    digest: str

    def canonical_unsigned(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "previous_digest": self.previous_digest,
            "state": self.state,
            "payload": _thaw(self.payload),
        }


_GLOBAL_CLAIM_LOCK = threading.RLock()
_GLOBAL_RESERVATIONS: dict[tuple[str, str], tuple[str, str, str, str]] = {}
_GLOBAL_ROOTS: dict[tuple[str, str], tuple[str, str, str, str]] = {}
_GLOBAL_NONCES: dict[tuple[str, str], tuple[str, str, str, str]] = {}


def _valid_reservation(value: object) -> bool:
    return isinstance(value, str) and _RAW_SHA256.fullmatch(value) is not None


def _controller_key_digest(value: object) -> str:
    if type(value) is not bytes or len(value) != _CONTROLLER_KEY_BYTES:
        raise CapsuleError("parent_controller_required")
    return hashlib.sha256(value).hexdigest()


def _claim_digest(namespace: str, reservation_id: str, output_root_identity: str, nonce: str) -> str:
    return hashlib.sha256(canonical_bytes({
        "namespace": namespace,
        "reservation_id": reservation_id,
        "output_root_identity": output_root_identity,
        "nonce": nonce,
    })).hexdigest()


class LedgerStorage:
    """Raw append-only storage.

    It knows how to retain an inode, append bytes, fsync, and verify a file.
    It deliberately has no state-transition methods; semantic transitions live
    on :class:`DurableLedger`, which is the parent-owned controller facade.
    """

    def __init__(
        self,
        *,
        root: str | Path,
        namespace: str,
        reservation_id: str,
        output_root_identity: str,
        nonce: str | None = None,
        worktree_roots: Sequence[str | Path] = (),
        create: bool = True,
        resume: bool = False,
    ) -> None:
        supplied_root = Path(root)
        if supplied_root.is_symlink() or not supplied_root.exists():
            raise CapsuleError("authority_replay")
        self.root = supplied_root.resolve(strict=True)
        if not self.root.is_dir() or stat.S_IMODE(self.root.stat().st_mode) & 0o077:
            raise CapsuleError("authority_replay")
        if namespace not in {"qualification", "final"}:
            raise CapsuleError("authority_replay")
        nonce = nonce or secrets.token_hex(32)
        if not _valid_reservation(reservation_id) or not isinstance(output_root_identity, str) \
                or not output_root_identity or not isinstance(nonce, str) or not nonce:
            raise CapsuleError("authority_replay")
        for candidate in worktree_roots:
            worktree = Path(candidate).resolve(strict=True)
            if self.root == worktree or worktree in self.root.parents or self.root in worktree.parents:
                raise CapsuleError("fresh_root_violation")
        self.namespace = namespace
        self.reservation_id = reservation_id
        self.output_root_identity = output_root_identity
        self.nonce = nonce
        self.identity = LEDGER_IDENTITY
        self._dir_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        directory_stat = os.fstat(self._dir_fd)
        self._directory_inode = (directory_stat.st_dev, directory_stat.st_ino)
        self._directory_mode = stat.S_IMODE(directory_stat.st_mode)
        self._fd = -1
        self._name = f"{namespace}-{reservation_id}.ledger"
        self._inode: tuple[int, int] | None = None
        self._claim_path: Path | None = None
        try:
            self._claim_path = self._claim_global_identity(existing=resume)
            flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW
            if create:
                flags |= os.O_CREAT | os.O_EXCL
            self._fd = os.open(self._name, flags, 0o600, dir_fd=self._dir_fd)
            ledger_stat = os.fstat(self._fd)
            if (not stat.S_ISREG(ledger_stat.st_mode) or ledger_stat.st_nlink != 1
                    or stat.S_IMODE(ledger_stat.st_mode) != 0o600):
                raise CapsuleError("authority_replay")
            self._inode = (ledger_stat.st_dev, ledger_stat.st_ino)
        except BaseException:
            self.close()
            raise

    def _claim_global_identity(self, *, existing: bool = False) -> Path:
        claim_parent = self.root.parent
        claim_dir = next(
            (candidate / ".k12-ledger-claims"
             for candidate in (claim_parent, claim_parent.parent)
             if (candidate / ".k12-ledger-claims").exists()),
            claim_parent / ".k12-ledger-claims",
        )
        try:
            if claim_dir.is_symlink():
                raise OSError("claim directory must not be a symlink")
            claim_dir.mkdir(mode=0o700, exist_ok=True)
            os.chmod(claim_dir, 0o700)
            claim_stat = claim_dir.stat()
            if (not stat.S_ISDIR(claim_stat.st_mode)
                    or stat.S_IMODE(claim_stat.st_mode) != 0o700):
                raise OSError("claim directory has unsafe mode")
            claim_registry_identity = str(claim_dir.resolve(strict=True))
        except OSError as exc:
            raise CapsuleError("authority_replay") from exc
        claim_name = _claim_digest(self.namespace, self.reservation_id,
                                   self.output_root_identity, self.nonce) + ".claim"
        claim_path = claim_dir / claim_name
        claim = {
            "namespace": self.namespace,
            "reservation_id": self.reservation_id,
            "output_root_identity": self.output_root_identity,
            "nonce": self.nonce,
        }
        claim_bytes = canonical_bytes(claim)
        resource_paths = (
            claim_dir / ("reservation-" + hashlib.sha256(
                canonical_bytes(self.reservation_id)).hexdigest() + ".claim"),
            claim_dir / ("output-root-" + hashlib.sha256(
                canonical_bytes(self.output_root_identity)).hexdigest() + ".claim"),
            claim_dir / ("nonce-" + hashlib.sha256(
                canonical_bytes(self.nonce)).hexdigest() + ".claim"),
        )

        def read_claim(path: Path) -> None:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    observed_stat = os.fstat(fd)
                    if (not stat.S_ISREG(observed_stat.st_mode)
                            or observed_stat.st_nlink != 1
                            or stat.S_IMODE(observed_stat.st_mode) != 0o600):
                        raise CapsuleError("authority_replay")
                    observed_bytes = os.read(fd, observed_stat.st_size)
                finally:
                    os.close(fd)
                observed = json.loads(observed_bytes.decode("utf-8"))
            except CapsuleError:
                raise
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise CapsuleError("authority_replay") from exc
            if (observed != claim or canonical_bytes(observed) != observed_bytes):
                raise CapsuleError("authority_replay")

        def write_claim(path: Path) -> None:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW, 0o600)
                try:
                    written = 0
                    while written < len(claim_bytes):
                        written += os.write(fd, claim_bytes[written:])
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as exc:
                raise CapsuleError("authority_replay") from exc

        with _GLOBAL_CLAIM_LOCK:
            claim_tuple = (self.namespace, self.reservation_id,
                           self.output_root_identity, self.nonce)
            existing_claim = (
                _GLOBAL_RESERVATIONS.get((claim_registry_identity, self.reservation_id))
                or _GLOBAL_ROOTS.get((claim_registry_identity, self.output_root_identity))
                or _GLOBAL_NONCES.get((claim_registry_identity, self.nonce))
            )
            if existing:
                if existing_claim is not None and existing_claim != claim_tuple:
                    raise CapsuleError("authority_replay")
                for path in (claim_path, *resource_paths):
                    read_claim(path)
            else:
                if existing_claim is not None:
                    raise CapsuleError("authority_replay")
                for path in (claim_path, *resource_paths):
                    write_claim(path)
                try:
                    directory_fd = os.open(claim_dir, os.O_RDONLY | os.O_DIRECTORY
                                           | os.O_NOFOLLOW)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError as exc:
                    raise CapsuleError("authority_replay") from exc
            _GLOBAL_RESERVATIONS[(claim_registry_identity, self.reservation_id)] = claim_tuple
            _GLOBAL_ROOTS[(claim_registry_identity, self.output_root_identity)] = claim_tuple
            _GLOBAL_NONCES[(claim_registry_identity, self.nonce)] = claim_tuple
        return claim_path

    def _check_integrity(self) -> None:
        if self._fd < 0 or self._inode is None or self._dir_fd < 0:
            raise CapsuleError("authority_replay")
        current = os.fstat(self._fd)
        directory = os.fstat(self._dir_fd)
        named = os.stat(self._name, dir_fd=self._dir_fd, follow_symlinks=False)
        if ((current.st_dev, current.st_ino) != self._inode
                or (named.st_dev, named.st_ino) != self._inode
                or (directory.st_dev, directory.st_ino) != self._directory_inode
                or stat.S_IMODE(directory.st_mode) != self._directory_mode
                or stat.S_IMODE(directory.st_mode) & 0o077
                or current.st_nlink != 1 or named.st_nlink != 1
                or stat.S_IMODE(current.st_mode) != 0o600
                or stat.S_IMODE(named.st_mode) != 0o600
                or not stat.S_ISREG(current.st_mode) or not stat.S_ISREG(named.st_mode)):
            raise CapsuleError("authority_replay")

    @contextmanager
    def locked(self, *, exclusive: bool = False) -> Iterator[None]:
        """Hold a coherent interprocess snapshot of the ledger file.

        The process-local claim lock keeps separate handles in this process
        from relying on platform-specific same-process ``flock`` behavior;
        the file lock provides the interprocess part of the contract.  The
        integrity check after acquiring the file lock is intentional: a
        reader or writer must validate the inode and mode while its snapshot
        is protected from another compliant handle.
        """
        with _GLOBAL_CLAIM_LOCK:
            self._check_integrity()
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(self._fd, operation)
            try:
                self._check_integrity()
                yield
            finally:
                fcntl.flock(self._fd, fcntl.LOCK_UN)

    def _bytes_locked(self) -> bytes:
        self._check_integrity()
        current = os.fstat(self._fd)
        return os.pread(self._fd, current.st_size, 0)

    def _append_bytes_locked(self, encoded: bytes) -> None:
        self._check_integrity()
        written = 0
        while written < len(encoded):
            written += os.write(self._fd, encoded[written:])
        os.fsync(self._fd)
        os.fsync(self._dir_fd)

    def bytes(self) -> bytes:
        with self.locked():
            return self._bytes_locked()

    def verify_file(self) -> bool:
        try:
            with self.locked():
                content = self._bytes_locked()
                return bool(content.endswith(b"\n"))
        except (OSError, CapsuleError):
            return False

    def close(self) -> None:
        if getattr(self, "_fd", -1) >= 0:
            try:
                os.close(self._fd)
            finally:
                self._fd = -1
        if getattr(self, "_dir_fd", -1) >= 0:
            try:
                os.close(self._dir_fd)
            finally:
                self._dir_fd = -1


class DurableLedger:
    """Parent-owned semantic controller over :class:`LedgerStorage`.

    Direct construction is retained as a deterministic ledger handle without a
    parent-held key.  A production parent should call
    ``ParentExecutionAuthority.create_ledger`` with a fresh controller key;
    ``LedgerStorage`` itself cannot perform any semantic transition.
    """

    TRANSITIONS = {
        None: {"reserved"},
        "reserved": {"authority_minted", "quarantined"},
        "authority_minted": {"first_consume_verified", "quarantined"},
        "first_consume_verified": {"active", "quarantined"},
        "active": {"terminal", "quarantined"},
    }
    TERMINAL = {"terminal", "quarantined"}

    def __init__(
        self,
        *,
        root: str | Path,
        namespace: str,
        reservation_id: str,
        output_root_identity: str,
        worktree_roots: Sequence[str | Path] = (),
        nonce: str | None = None,
        _controller_key: bytes | None = None,
        _resume: bool = False,
    ) -> None:
        self.namespace = namespace
        self.reservation_id = reservation_id
        self.output_root_identity = output_root_identity
        self.nonce = nonce or secrets.token_hex(32)
        if _controller_key is not None:
            self._controller_key_digest = _controller_key_digest(_controller_key)
        elif not _resume:
            # Direct construction has no parent capable of using the resulting
            # handle.  Still seal its initial record with a fresh verifier so
            # the handle never needs to retain a raw capability.
            self._controller_key_digest = _controller_key_digest(
                secrets.token_bytes(_CONTROLLER_KEY_BYTES)
            )
        else:
            self._controller_key_digest: str | None = None
        self._storage = LedgerStorage(
            root=root, namespace=namespace, reservation_id=reservation_id,
            output_root_identity=output_root_identity, nonce=self.nonce,
            worktree_roots=worktree_roots, create=not _resume, resume=_resume,
        )
        self.root = self._storage.root
        self.identity = LEDGER_IDENTITY
        self._name = self._storage._name
        self._fd = self._storage._fd
        self._dir_fd = self._storage._dir_fd
        self._inode = self._storage._inode
        self._directory_inode = self._storage._directory_inode
        self._directory_mode = self._storage._directory_mode
        self._events: list[LedgerEvent] = []
        self._state: str | None = None
        self._thread_lock = threading.RLock()
        if _resume:
            try:
                self._load_existing()
            except BaseException:
                self.close()
                raise
        else:
            self._append("reserved", {
                "reservation_id": reservation_id,
                "namespace": namespace,
                "output_root_identity": output_root_identity,
                "nonce": self.nonce,
                "controller_key_digest": self._controller_key_digest,
            })

    @classmethod
    def create_parent_owned(
        cls, *, controller_key: bytes, root: str | Path, namespace: str,
        reservation_id: str, output_root_identity: str,
        worktree_roots: Sequence[str | Path] = (), nonce: str | None = None,
    ) -> "DurableLedger":
        """Create a ledger controlled by a parent-retained raw key."""
        return cls(
            root=root, namespace=namespace, reservation_id=reservation_id,
            output_root_identity=output_root_identity, nonce=nonce,
            worktree_roots=worktree_roots, _controller_key=controller_key,
        )

    @classmethod
    def open_parent_owned(
        cls, *, controller_key: bytes, root: str | Path, namespace: str,
        reservation_id: str, output_root_identity: str, nonce: str,
        worktree_roots: Sequence[str | Path] = (),
    ) -> "DurableLedger":
        """Open an existing ledger under a parent-retained raw key."""
        return cls.open_existing(
            root=root, namespace=namespace, reservation_id=reservation_id,
            output_root_identity=output_root_identity, nonce=nonce,
            worktree_roots=worktree_roots, controller_key=controller_key,
        )

    @classmethod
    def open_existing(
        cls,
        *,
        root: str | Path,
        namespace: str,
        reservation_id: str,
        output_root_identity: str,
        nonce: str,
        worktree_roots: Sequence[str | Path] = (),
        controller_key: bytes | None = None,
    ) -> "DurableLedger":
        return cls(root=root, namespace=namespace, reservation_id=reservation_id,
                   output_root_identity=output_root_identity, nonce=nonce,
                   worktree_roots=worktree_roots, _controller_key=controller_key,
                   _resume=True)

    def _load_existing_locked(self, *, allow_empty: bool = False) -> None:
        try:
            content = self._storage._bytes_locked()
            if not content:
                if allow_empty:
                    self._events.clear()
                    self._state = None
                    return
                raise CapsuleError("ledger_corrupt")
            if not content.endswith(b"\n"):
                raise CapsuleError("ledger_corrupt")
            self._events.clear()
            self._state = None
            previous = ""
            for line in content.splitlines():
                observed = json.loads(line)
                if (not isinstance(observed, dict)
                        or set(observed) != {"ordinal", "previous_digest", "state", "payload", "digest"}):
                    raise CapsuleError("ledger_corrupt")
                unsigned = {key: observed[key] for key in
                            ("ordinal", "previous_digest", "state", "payload")}
                digest = observed["digest"]
                if (type(unsigned["ordinal"]) is not int or unsigned["ordinal"] != len(self._events) + 1
                        or unsigned["previous_digest"] != previous
                        or not isinstance(unsigned["state"], str)
                        or not isinstance(unsigned["payload"], dict)
                        or canonical_bytes(observed) != line
                        or canonical_sha256(unsigned) != digest):
                    raise CapsuleError("ledger_corrupt")
                frozen_payload = _freeze(unsigned["payload"])
                event = LedgerEvent(unsigned["ordinal"], unsigned["previous_digest"],
                                    unsigned["state"], MappingProxyType(dict(frozen_payload)), digest)
                if unsigned["state"] not in self.TRANSITIONS.get(self._state, set()):
                    raise CapsuleError("ledger_corrupt")
                if unsigned["ordinal"] == 1:
                    observed_key_digest = unsigned["payload"].get("controller_key_digest")
                    if _RAW_SHA256.fullmatch(observed_key_digest or "") is None:
                        raise CapsuleError("ledger_corrupt")
                    if self._controller_key_digest is None:
                        self._controller_key_digest = observed_key_digest
                    elif not hmac.compare_digest(
                        self._controller_key_digest, observed_key_digest
                    ):
                        raise CapsuleError("authority_replay")
                    if (event.state != "reserved" or dict(event.payload) != {
                            "reservation_id": self.reservation_id,
                            "namespace": self.namespace,
                            "output_root_identity": self.output_root_identity,
                            "nonce": self.nonce,
                            "controller_key_digest": observed_key_digest,
                        }):
                        raise CapsuleError("ledger_corrupt")
                self._events.append(event)
                self._state = event.state
                previous = digest
        except (OSError, TypeError, ValueError, json.JSONDecodeError, CapsuleError) as exc:
            if isinstance(exc, CapsuleError) and exc.reason in {
                "ledger_corrupt", "authority_replay",
            }:
                raise
            raise CapsuleError("ledger_corrupt") from exc

    def _load_existing(self) -> None:
        with self._storage.locked():
            self._load_existing_locked()

    @property
    def state(self) -> str | None:
        return self._state

    @property
    def head_digest(self) -> str:
        return self._events[-1].digest if self._events else ""

    @property
    def events(self) -> tuple[LedgerEvent, ...]:
        return tuple(self._events)

    @property
    def controller(self) -> "LedgerController":
        raise CapsuleError("parent_controller_required")

    def acquire_parent_controller(self, controller_key: bytes) -> "LedgerController":
        """Acquire the semantic transition surface with the parent key.

        A ledger handle is intentionally insufficient to obtain a controller.
        The parent retains the raw high-entropy key supplied when the ledger
        was created or opened and must present it for every acquisition.
        """
        key = self._require_controller_key(controller_key)
        return LedgerController(self, key)

    def _append(
        self,
        state_name: str,
        payload: Mapping[str, Any],
        *,
        _controller_key: bytes | None = None,
    ) -> str:
        with self._thread_lock:
            with self._storage.locked(exclusive=True):
                # Never build a transition from this handle's potentially
                # stale cache.  Reload and verify the complete chain while the
                # same interprocess lock remains held through validation,
                # digest construction, and the append.
                self._load_existing_locked(allow_empty=not self._events)
                if self._state is not None or state_name != "reserved":
                    self._require_controller_key(_controller_key)
                if state_name not in self.TRANSITIONS.get(self._state, set()):
                    raise CapsuleError("invalid ledger transition or replay")
                if (state_name in {"first_consume_verified", "active"}
                        and (len(self._events) < 2
                             or payload.get("authority_digest")
                             != self._events[1].payload.get("authority_digest"))):
                    raise CapsuleError("authority_replay")
                frozen_payload = _freeze(dict(payload))
                unsigned = {
                    "ordinal": len(self._events) + 1,
                    "previous_digest": self.head_digest,
                    "state": state_name,
                    "payload": _thaw(frozen_payload),
                }
                event_digest = canonical_sha256(unsigned)
                self._storage._append_bytes_locked(
                    canonical_bytes({**unsigned, "digest": event_digest}) + b"\n"
                )
                event = LedgerEvent(unsigned["ordinal"], unsigned["previous_digest"], state_name,
                                    MappingProxyType(dict(frozen_payload)), event_digest)
                self._events.append(event)
                self._state = state_name
                return event_digest

    def _require_controller_key(self, controller_key: object) -> bytes:
        digest = _controller_key_digest(controller_key)
        if self._controller_key_digest is None or not hmac.compare_digest(
            self._controller_key_digest, digest
        ):
            raise CapsuleError("parent_controller_required")
        return controller_key

    def authority_minted(
        self, authority_digest: str, *, _controller_key: bytes | None = None
    ) -> str:
        _require_canonical_digest(authority_digest)
        return self._append(
            "authority_minted", {"authority_digest": authority_digest},
            _controller_key=_controller_key,
        )

    def first_consume_verified(self, authority_digest: str, observation_digest: str,
                               *, _controller_key: bytes | None = None) -> str:
        _require_canonical_digest(authority_digest)
        _require_canonical_digest(observation_digest)
        return self._append(
            "first_consume_verified", {
                "authority_digest": authority_digest,
                "observation_digest": observation_digest,
            },
            _controller_key=_controller_key,
        )

    def activate(
        self, authority_digest: str, *, _controller_key: bytes | None = None
    ) -> str:
        _require_canonical_digest(authority_digest)
        return self._append(
            "active", {"authority_digest": authority_digest},
            _controller_key=_controller_key,
        )

    def terminal(
        self, payload: Mapping[str, Any], *, _controller_key: bytes | None = None
    ) -> str:
        if not isinstance(payload, Mapping):
            raise CapsuleError("invalid ledger payload")
        return self._append("terminal", payload, _controller_key=_controller_key)

    def quarantine(
        self, payload: Mapping[str, Any], *, _controller_key: bytes | None = None
    ) -> str:
        if not isinstance(payload, Mapping):
            raise CapsuleError("invalid ledger payload")
        return self._append("quarantined", payload, _controller_key=_controller_key)

    def verify_chain(self) -> bool:
        try:
            with self._thread_lock:
                with self._storage.locked():
                    # Refresh the handle from one locked file snapshot so a
                    # reader never validates a mixture of two append states.
                    self._load_existing_locked()
                    return True
        except (OSError, ValueError, TypeError, json.JSONDecodeError, CapsuleError):
            return False

    def close(self) -> None:
        self._storage.close()
        self._fd = -1
        self._dir_fd = -1

    def __enter__(self) -> "DurableLedger":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


class LedgerController:
    """Parent-only transition surface for a durable ledger."""

    def __init__(self, ledger: DurableLedger, controller_key: bytes) -> None:
        if not isinstance(ledger, DurableLedger):
            raise TypeError("parent-owned ledger controller required")
        controller_key = ledger._require_controller_key(controller_key)
        self.ledger = ledger
        self._controller_key = controller_key

    def authority_minted(self, authority_digest: str) -> str:
        return self.ledger.authority_minted(
            authority_digest, _controller_key=self._controller_key
        )

    def first_consume_verified(self, authority_digest: str, observation_digest: str) -> str:
        return self.ledger.first_consume_verified(
            authority_digest, observation_digest, _controller_key=self._controller_key
        )

    def activate(self, authority_digest: str) -> str:
        return self.ledger.activate(authority_digest, _controller_key=self._controller_key)

    def terminal(self, payload: Mapping[str, Any]) -> str:
        return self.ledger.terminal(payload, _controller_key=self._controller_key)

    def quarantine(self, payload: Mapping[str, Any]) -> str:
        return self.ledger.quarantine(payload, _controller_key=self._controller_key)


CapsuleLedger = DurableLedger
DurableLedgerStorage = LedgerStorage
ParentLedgerController = LedgerController


__all__ = [
    "CAPSULE_IDENTITY", "CONDITIONAL_CAPSULE_CATEGORIES", "CapsuleError", "CapsuleLedger",
    "CapsuleRecord", "DurableLedger", "ExecutionCapsule", "LEDGER_IDENTITY",
    "LedgerController", "LedgerEvent", "LedgerStorage", "DurableLedgerStorage",
    "ParentLedgerController", "MINIMUM_CAPSULE_CATEGORIES", "attest_capsule", "mint_capsule",
]
