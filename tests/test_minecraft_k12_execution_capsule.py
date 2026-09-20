import dataclasses
import os
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from benchmarks.minecraft.k12_execution_capsule import (
    CapsuleError,
    CapsuleRecord,
    DurableLedger,
    LedgerStorage,
    MINIMUM_CAPSULE_CATEGORIES,
    _CAPSULE_MINT_TOKEN,
    attest_capsule,
    mint_capsule,
)


D = "d" * 64
A = "sha256:" + D


class _TestCapsuleParent:
    def __init__(self):
        self._capsules = {}

    def register(self, capsule):
        self._capsules[id(capsule)] = capsule

    def owns_capsule(self, capsule):
        return self._capsules.get(id(capsule)) is capsule

    def mint_capsule(self, **values):
        capsule = mint_capsule(
            token=_CAPSULE_MINT_TOKEN,
            owner=self,
            **values,
        )
        self.register(capsule)
        return capsule


def _records(*, node=False, java=False):
    values = [
        CapsuleRecord("repo", D, "repo"),
        CapsuleRecord("interpreter", D, "interpreter"),
        CapsuleRecord("stdlib", D, "stdlib"),
        CapsuleRecord("imports", D, "import_roots"),
        CapsuleRecord("distributions", D, "distributions"),
        CapsuleRecord("native", D, "native"),
        CapsuleRecord("startup", D, "startup"),
    ]
    if node:
        values.append(CapsuleRecord("node", D, "node"))
    if java:
        values.append(CapsuleRecord("java", D, "java"))
    return tuple(values)


def _capsule_values(**changes):
    values = dict(
        identity="minecraft-k12-live-execution-capsule/1",
        source_aggregate=D,
        immutable_store_path_digest=D,
        recursive_store_closure_digest=D,
        interpreter_digest=D,
        import_roots_digest=D,
        records=_records(),
        immutable=True,
        read_only=True,
        outside_worktree=True,
        user_site_enabled=False,
        editable_installs=False,
        unapproved_pth=False,
        sitecustomize=False,
        startup_hooks=False,
        writable_worktree_imports=False,
    )
    values.update(changes)
    return values


def _capsule_with_parent(**changes):
    parent = _TestCapsuleParent()
    return parent.mint_capsule(**_capsule_values(**changes)), parent


def _capsule(**changes):
    return _capsule_with_parent(**changes)[0]


def _ledger(tmp_path, reservation="e" * 64, output="output/1", namespace="qualification"):
    root = tmp_path / namespace
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    controller_key = secrets.token_bytes(32)
    ledger = DurableLedger.create_parent_owned(
        controller_key=controller_key,
        root=root,
        namespace=namespace,
        reservation_id=reservation,
        output_root_identity=output,
    )
    return ledger, controller_key


def test_positive_capsule_has_the_complete_minimum_and_is_immutable():
    capsule = _capsule()
    assert MINIMUM_CAPSULE_CATEGORIES <= capsule.categories
    assert capsule.verify()
    assert capsule.canonical()["capsule_digest"] == capsule.capsule_digest
    with pytest.raises(dataclasses.FrozenInstanceError):
        capsule.read_only = False


def test_capsule_mint_is_parent_owned_and_caller_attestation_is_rejected():
    with pytest.raises(TypeError, match="parent-minted"):
        attest_capsule(**_capsule_values())
    with pytest.raises(TypeError, match="parent-minted"):
        attest_capsule(owner=_TestCapsuleParent(), **_capsule_values())
    with pytest.raises(TypeError, match="parent-minted"):
        attest_capsule(token=_CAPSULE_MINT_TOKEN, **_capsule_values())

    capsule, parent = _capsule_with_parent()
    assert capsule.owned_by(parent)
    assert not capsule.owned_by(_TestCapsuleParent())

    unregistered_parent = _TestCapsuleParent()
    unregistered = mint_capsule(
        token=_CAPSULE_MINT_TOKEN,
        owner=unregistered_parent,
        **_capsule_values(),
    )
    assert not unregistered.owned_by(unregistered_parent)
    unregistered_parent.register(unregistered)
    assert unregistered.owned_by(unregistered_parent)


@pytest.mark.parametrize("kind", sorted(MINIMUM_CAPSULE_CATEGORIES))
def test_every_minimum_category_is_required(kind):
    records = tuple(record for record in _records() if record.kind != kind)
    with pytest.raises(CapsuleError, match="capsule_mismatch"):
        _capsule(records=records)


@pytest.mark.parametrize("kind", sorted(MINIMUM_CAPSULE_CATEGORIES))
def test_every_required_category_has_exactly_one_record(kind):
    duplicate = CapsuleRecord(f"duplicate-{kind}", D, kind)
    with pytest.raises(CapsuleError, match="capsule_mismatch"):
        _capsule(records=(*_records(), duplicate))


@pytest.mark.parametrize(
    ("kind", "declared_field"),
    (
        ("repo", "source_aggregate"),
        ("interpreter", "interpreter_digest"),
        ("import_roots", "import_roots_digest"),
    ),
)
def test_capsule_record_digests_are_bound_to_declared_aggregates(kind, declared_field):
    record = next(item for item in _records() if item.kind == kind)
    changed_record = dataclasses.replace(record, content_digest="e" * 64)
    changed_records = tuple(
        changed_record if item.kind == kind else item for item in _records()
    )
    with pytest.raises(CapsuleError, match="capsule_mismatch"):
        _capsule(records=changed_records)
    with pytest.raises(CapsuleError, match="capsule_mismatch"):
        _capsule(**{declared_field: "e" * 64})


def test_node_and_java_are_conditional_and_writable_lazy_imports_are_rejected():
    assert "node" not in _capsule().categories
    assert {"node", "java"} <= _capsule(records=_records(node=True, java=True),
                                            node_required=True, java_required=True).categories
    with pytest.raises(CapsuleError, match="capsule_mismatch"):
        _capsule(records=_records(node=True), node_required=True, java_required=True)
    with pytest.raises(CapsuleError, match="capsule_mismatch"):
        _capsule(lazy_imports=True)
    with pytest.raises(CapsuleError, match="capsule_mismatch"):
        _capsule(records=(*_records(), CapsuleRecord("lazy", D, "native", lazy=True)))


def test_capsule_digest_detects_payload_tampering():
    capsule = _capsule()
    object.__setattr__(capsule, "source_aggregate", "e" * 64)
    assert not capsule.verify()


def test_raw_storage_has_no_semantic_transition_surface(tmp_path):
    root = tmp_path / "raw"
    root.mkdir(mode=0o700)
    storage = LedgerStorage(
        root=root,
        namespace="qualification",
        reservation_id="a" * 64,
        output_root_identity="raw-output",
        nonce="n-raw",
    )
    try:
        assert not hasattr(storage, "authority_minted")
        with pytest.raises(AttributeError):
            storage.authority_minted(A)  # type: ignore[attr-defined]
        assert not hasattr(storage, "append_bytes")
        with pytest.raises(AttributeError):
            storage.append_bytes(b"raw\n")  # type: ignore[attr-defined]
    finally:
        storage.close()


def test_ledger_controller_and_storage_require_parent_ownership(tmp_path):
    ledger, controller_key = _ledger(tmp_path)
    try:
        with pytest.raises(CapsuleError, match="parent_controller_required"):
            _ = ledger.controller
        with pytest.raises(CapsuleError, match="parent_controller_required"):
            ledger.acquire_parent_controller(secrets.token_bytes(32))
        with pytest.raises(CapsuleError, match="parent_controller_required"):
            ledger.authority_minted(A)
        with pytest.raises(CapsuleError, match="parent_controller_required"):
            ledger._append("authority_minted", {"authority_digest": A})
        assert not hasattr(ledger, "storage")
        assert not hasattr(ledger, "controller_key")
        assert not hasattr(ledger, "_parent_marker")
        assert controller_key not in vars(ledger).values()
        assert controller_key not in ledger.events[0].payload.values()
        verifier = ledger.events[0].payload["controller_key_digest"]
        with pytest.raises(CapsuleError, match="parent_controller_required"):
            ledger.acquire_parent_controller(bytes.fromhex(verifier))
        with pytest.raises(AttributeError):
            ledger.storage  # type: ignore[attr-defined]
        controller = ledger.acquire_parent_controller(controller_key)
        assert not hasattr(controller, "controller_key")
        assert not hasattr(controller, "key")
        controller.authority_minted(A)
        assert ledger.state == "authority_minted"
    finally:
        ledger.close()


def test_parent_controller_keys_are_fresh_high_entropy_bytes(tmp_path):
    first, first_key = _ledger(tmp_path, reservation="1" * 64, output="output/key-1")
    second, second_key = _ledger(
        tmp_path / "second", reservation="2" * 64, output="output/key-2"
    )
    try:
        assert type(first_key) is bytes and len(first_key) == 32
        assert type(second_key) is bytes and len(second_key) == 32
        assert first_key != second_key
    finally:
        second.close()
        first.close()


def test_ledger_is_append_only_fsynced_and_reopenable(tmp_path):
    ledger, controller_key = _ledger(tmp_path)
    reservation = ledger.reservation_id
    nonce = ledger.nonce
    controller = ledger.acquire_parent_controller(controller_key)
    controller.authority_minted(A)
    controller.first_consume_verified(A, A)
    active = controller.activate(A)
    assert ledger.state == "active" and ledger.verify_chain()
    ledger.close()

    with pytest.raises(CapsuleError, match="authority_replay"):
        DurableLedger.open_parent_owned(
            controller_key=secrets.token_bytes(32),
            root=tmp_path / "qualification",
            namespace="qualification",
            reservation_id=reservation,
            output_root_identity="output/1",
            nonce=nonce,
        )

    reopened = DurableLedger.open_parent_owned(
        controller_key=controller_key,
        root=tmp_path / "qualification",
        namespace="qualification",
        reservation_id=reservation,
        output_root_identity="output/1",
        nonce=nonce,
    )
    try:
        assert reopened.state == "active"
        assert reopened.head_digest == active
        assert reopened.verify_chain()
        with pytest.raises(CapsuleError, match="parent_controller_required"):
            reopened.activate(A)
        with pytest.raises(CapsuleError, match="parent_controller_required"):
            reopened.acquire_parent_controller(secrets.token_bytes(32))
    finally:
        reopened.close()


def test_second_handle_reloads_before_appending(tmp_path):
    root = tmp_path / "qualification"
    root.mkdir(mode=0o700)
    controller_key = secrets.token_bytes(32)
    first = DurableLedger.create_parent_owned(
        controller_key=controller_key,
        root=root,
        namespace="qualification",
        reservation_id="c" * 64,
        output_root_identity="output/concurrent-reload",
    )
    second = DurableLedger.open_parent_owned(
        controller_key=controller_key,
        root=root,
        namespace="qualification",
        reservation_id=first.reservation_id,
        output_root_identity=first.output_root_identity,
        nonce=first.nonce,
    )
    try:
        first.acquire_parent_controller(controller_key).authority_minted(A)
        second.acquire_parent_controller(controller_key).first_consume_verified(A, A)
        assert second.state == "first_consume_verified"
        assert first.verify_chain() and second.verify_chain()
        assert len(second.events) == 3
        assert second.events[-1].ordinal == 3
        assert second.events[-1].previous_digest == second.events[-2].digest
    finally:
        second.close()
        first.close()


def test_two_handles_serialize_same_transition_and_preserve_chain(tmp_path):
    root = tmp_path / "qualification"
    root.mkdir(mode=0o700)
    controller_key = secrets.token_bytes(32)
    first = DurableLedger.create_parent_owned(
        controller_key=controller_key,
        root=root,
        namespace="qualification",
        reservation_id="d" * 64,
        output_root_identity="output/concurrent-transition",
    )
    second = DurableLedger.open_parent_owned(
        controller_key=controller_key,
        root=root,
        namespace="qualification",
        reservation_id=first.reservation_id,
        output_root_identity=first.output_root_identity,
        nonce=first.nonce,
    )
    ready = threading.Barrier(2)

    def append_authority(ledger):
        ready.wait()
        try:
            controller = ledger.acquire_parent_controller(controller_key)
            return ("ok", controller.authority_minted(A))
        except CapsuleError as exc:
            return ("error", exc.reason)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(append_authority, (first, second)))
        assert [result[0] for result in results].count("ok") == 1
        assert [result[0] for result in results].count("error") == 1
        assert all(result[1] for result in results)
        assert first.verify_chain() and second.verify_chain()
        assert len(first.events) == len(second.events) == 2
        assert first.events[-1].ordinal == second.events[-1].ordinal == 2
        assert first.events[-1].previous_digest == second.events[-1].previous_digest
    finally:
        second.close()
        first.close()


def test_ledger_integrity_detects_mode_and_truncation(tmp_path):
    ledger, controller_key = _ledger(tmp_path)
    try:
        ledger.acquire_parent_controller(controller_key).authority_minted(A)
        os.chmod(ledger.root / ledger._name, 0o640)
        assert not ledger.verify_chain()
    finally:
        ledger.close()

    truncated, _ = _ledger(tmp_path / "truncated", reservation="f" * 64, output="output/2")
    try:
        ledger_bytes = os.fstat(truncated._fd).st_size
        os.ftruncate(truncated._fd, max(0, ledger_bytes - 2))
        assert not truncated.verify_chain()
    finally:
        truncated.close()


def test_global_reservation_root_and_nonce_replay_is_cross_namespace(tmp_path):
    first, _ = _ledger(tmp_path, reservation="1" * 64, output="same-root")
    nonce = first.nonce
    try:
        with pytest.raises(CapsuleError, match="authority_replay"):
            _ledger(tmp_path / "other", reservation="1" * 64, output="other-root")
        with pytest.raises(CapsuleError, match="authority_replay"):
            _ledger(tmp_path / "other-root", reservation="2" * 64, output="same-root")
        with pytest.raises(CapsuleError, match="authority_replay"):
            DurableLedger(
                root=tmp_path / "other-nonce",
                namespace="final",
                reservation_id="3" * 64,
                output_root_identity="other-root-2",
                nonce=nonce,
            )
    finally:
        first.close()
