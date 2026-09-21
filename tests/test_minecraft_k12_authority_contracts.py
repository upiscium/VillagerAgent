from __future__ import annotations

import ast
import hashlib
import hmac
import importlib
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
import sys
import time
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROVENANCE = ROOT / "benchmarks/minecraft/k12_execution_provenance.py"
QUALIFICATION = ROOT / "benchmarks/minecraft/k12_live_qualification.py"
CONTRACTS = ROOT / "benchmarks/minecraft/k12_authority_contracts.py"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def _imports_module(imports: set[str], module: str) -> bool:
    return any(item == module or item.endswith(f".{module}") for item in imports)


def _observations(suffix: str):
    from benchmarks.minecraft.k12_execution_provenance import (
        CheckoutObservation,
        PullRequestObservation,
    )

    head = (suffix * 40)[:40]
    tree = (("b" if suffix == "a" else "c") * 40)
    base = (("d" if suffix == "a" else "e") * 40)
    branch = f"refs/heads/issue-580-{suffix}"
    repository = f"example/{suffix}"
    checkout = CheckoutObservation(
        repository, "f" * 64, "f" * 64, "f" * 64, branch, head, tree, tree,
        branch, head, repository, branch, head, True, True, True, True,
    )
    pull_request = PullRequestObservation(
        repository, 580, "OPEN", True, repository,
        branch.removeprefix("refs/heads/"), head, "main", base, 100,
        "external-verifier/1", "f" * 64,
    )
    return checkout, pull_request


def test_authority_import_dag_is_neutral_and_one_way():
    provenance_imports = _imports(PROVENANCE)
    qualification_imports = _imports(QUALIFICATION)
    contract_imports = _imports(CONTRACTS)
    assert not _imports_module(provenance_imports, "k12_live_qualification")
    assert _imports_module(provenance_imports, "k12_authority_contracts")
    assert _imports_module(qualification_imports, "k12_authority_contracts")
    assert _imports_module(qualification_imports, "k12_execution_provenance")
    assert not {
        item for item in contract_imports
        if item.endswith("k12_live_qualification")
        or item.endswith("k12_execution_provenance")
    }


def test_prospective_runtime_contains_no_mutable_tracked_git_revision_authority():
    pattern = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")
    observed: set[tuple[str, str]] = set()
    for root, glob_pattern in (
        (ROOT / "benchmarks/minecraft", "k12*.py"),
        (ROOT / "benchmarks/minecraft", "k12*.json"),
        (ROOT / "configs/minecraft", "k12*.json"),
    ):
        for path in root.glob(glob_pattern):
            for value in pattern.findall(path.read_text(encoding="utf-8")):
                observed.add((str(path.relative_to(ROOT)), value))
    assert observed == {
        ("benchmarks/minecraft/k12_runtime_profile.py",
         "2687ce4ad0f360d815a81954ee4313eedaa61a8c"),
        ("benchmarks/minecraft/k12_live_runtime_profile_v1.json",
         "be54d4bf1063eb812105b86bba87ebb27f3cfc43"),
    }


@pytest.mark.parametrize("order", ["provenance-first", "qualification-first"])
def test_authority_modules_import_in_both_orders_with_stable_types(order):
    modules = (
        ["benchmarks.minecraft.k12_execution_provenance",
         "benchmarks.minecraft.k12_live_qualification"]
        if order == "provenance-first"
        else ["benchmarks.minecraft.k12_live_qualification",
              "benchmarks.minecraft.k12_execution_provenance"]
    )
    script = (
        "import importlib;"
        f"a=importlib.import_module({modules[0]!r});"
        f"b=importlib.import_module({modules[1]!r});"
        "q=importlib.import_module('benchmarks.minecraft.k12_live_qualification');"
        "c=importlib.import_module('benchmarks.minecraft.k12_authority_contracts');"
        "assert issubclass(q.LiveQualificationAggregate,c.QualificationEvidenceContract);"
        "assert importlib.import_module(q.__name__).LiveQualificationAggregate "
        "is q.LiveQualificationAggregate"
    )
    subprocess.run([sys.executable, "-c", script], cwd=ROOT, check=True)


@pytest.mark.parametrize("order", ["provenance-first", "qualification-first"])
def test_qualification_reload_preserves_authority_identity_and_behavior(order):
    first, second = (
        ("benchmarks.minecraft.k12_execution_provenance",
         "benchmarks.minecraft.k12_live_qualification")
        if order == "provenance-first"
        else ("benchmarks.minecraft.k12_live_qualification",
              "benchmarks.minecraft.k12_execution_provenance")
    )
    script = f"""
import importlib, pathlib, sys, tempfile
importlib.import_module({first!r})
importlib.import_module({second!r})
p = importlib.import_module('benchmarks.minecraft.k12_execution_provenance')
q = importlib.import_module('benchmarks.minecraft.k12_live_qualification')
c = importlib.import_module('benchmarks.minecraft.k12_authority_contracts')
authority_type = p.QualificationExecutionAuthority
contract_types = (
    c.QualificationTerminalEvidenceContract,
    c.QualificationEvidenceContract,
    c.QualificationEvidenceProjection,
    c.QualificationAggregateOwnershipReceipt,
)
sys.path.insert(0, 'tests')
with tempfile.TemporaryDirectory(prefix='k12-reload-') as root:
    c = importlib.reload(c)
    assert contract_types == (
        c.QualificationTerminalEvidenceContract,
        c.QualificationEvidenceContract,
        c.QualificationEvidenceProjection,
        c.QualificationAggregateOwnershipReceipt,
    )
    q = importlib.reload(q)
    assert p.QualificationExecutionAuthority is authority_type
    helpers = importlib.import_module('test_minecraft_k12_authority_e2e')
    graph = helpers._build_graph(pathlib.Path(root), 'reload')
    try:
        assert isinstance(graph.aggregate, q.LiveQualificationAggregate)
        assert graph.aggregate.ownership_receipt.authenticates(graph.aggregate)
    finally:
        graph.close()
"""
    subprocess.run([sys.executable, "-c", script], cwd=ROOT, check=True)


def test_parent_authenticates_arbitrary_revision_a_then_b_and_rejects_stale_a():
    from benchmarks.minecraft.k12_execution_provenance import (
        ParentExecutionAuthority,
        ProvenanceError,
    )

    parent = ParentExecutionAuthority().injected_test_controller()
    checkout_a, pull_request_a = _observations("a")
    checkout_b, pull_request_b = _observations("b")
    authorization_a = parent.mint_external_revision_authorization(
        checkout_a, pull_request_a, verifier_identity="verifier/a", now=100,
    )
    authorization_b = parent.mint_external_revision_authorization(
        checkout_b, pull_request_b, verifier_identity="verifier/b", now=100,
    )
    assert authorization_a.head_commit != authorization_b.head_commit
    assert authorization_a.owned_by(parent)
    assert authorization_b.owned_by(parent)
    with pytest.raises(ProvenanceError, match="pr_semantic_mismatch"):
        pull_request_b.validate(authorization_a, now=101)
    with pytest.raises(ProvenanceError, match="pr_observation_stale"):
        pull_request_a.validate(authorization_a, now=401)


def test_raw_revision_receipt_cannot_construct_parent_authorization():
    from benchmarks.minecraft.k12_execution_provenance import (
        K12QualificationRunAuthorization,
    )

    with pytest.raises(TypeError):
        K12QualificationRunAuthorization(
            {"external_revision_authorization": {}, "capabilities": ["qualification_execute"]},
            object(),
        )


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _subject_observations(root: Path, *, observed_at: int):
    from benchmarks.common.eac.canonical import canonical_sha256
    from benchmarks.minecraft.k12_execution_provenance import (
        CheckoutObservation,
        PullRequestObservation,
    )

    repository = "example/VillagerAgent"
    branch = "refs/heads/issue-580-subject"
    head = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    identity = canonical_sha256(str(root.resolve())).removeprefix("sha256:")
    checkout = CheckoutObservation(
        repository, identity, identity, identity, branch, head, tree, tree,
        branch, head, repository, branch, head, True, True, True, True,
    )
    receipt = canonical_sha256({
        "repository": repository, "branch": branch, "head": head,
        "tree": tree, "observed_at": observed_at,
    }).removeprefix("sha256:")
    pull_request = PullRequestObservation(
        repository, 580, "OPEN", True, repository,
        branch.removeprefix("refs/heads/"), head, "main", "d" * 40,
        observed_at, "issue-580-verifier/1", receipt,
    )
    return checkout, pull_request


def _signed_revision(parent, key: bytes, checkout, pull_request, *, expires_at: int):
    from benchmarks.minecraft.k12_execution_provenance import (
        RUNTIME_VERIFIED_ORIGIN,
        external_revision_attestation_payload,
    )

    verifier = "issue-580-verifier/1"
    payload = external_revision_attestation_payload(
        checkout, pull_request, expires_at=expires_at,
        origin=RUNTIME_VERIFIED_ORIGIN, verifier_identity=verifier,
    )
    receipt = hmac.new(key, payload, hashlib.sha256).hexdigest()
    return parent.mint_external_revision_authorization(
        checkout, pull_request, verifier_identity=verifier,
        verifier_receipt_digest=receipt, expires_at=expires_at,
    )


def test_runtime_revision_verifier_rejects_raw_or_invalid_receipts():
    from benchmarks.minecraft.k12_execution_provenance import (
        ParentExecutionAuthority,
        ProvenanceError,
    )

    checkout, pull_request = _observations("a")
    pull_request = replace(pull_request, observed_at=int(time.time()))
    with pytest.raises(TypeError, match="externally verified"):
        ParentExecutionAuthority().mint_external_revision_authorization(
            checkout, pull_request,
        )
    parent = ParentExecutionAuthority(
        revision_verifier_key=b"k" * 32,
        revision_verifier_identity="issue-580-verifier/1",
    )
    with pytest.raises(ProvenanceError, match="pr_observation_missing"):
        parent.mint_external_revision_authorization(
            checkout, pull_request,
            verifier_identity="issue-580-verifier/1",
            verifier_receipt_digest="0" * 64,
            expires_at=parent.current_time() + 300,
        )


def test_injected_trusted_clock_rejects_expiry_rollback():
    from benchmarks.minecraft.k12_execution_provenance import (
        ParentExecutionAuthority,
        ProvenanceError,
    )

    parent = ParentExecutionAuthority().injected_test_controller()
    checkout, pull_request = _observations("a")
    parent.mint_external_revision_authorization(
        checkout, pull_request, now=100, expires_at=200,
    )
    parent.advance_trusted_time(101)
    with pytest.raises(ProvenanceError, match="trusted_clock_rollback"):
        parent.mint_external_revision_authorization(
            checkout, pull_request, now=100, expires_at=200,
        )


def test_runtime_trusted_clock_rejects_wall_clock_rollback(monkeypatch):
    from benchmarks.minecraft import k12_execution_provenance as provenance

    values = iter((1_000, 999))
    monkeypatch.setattr(provenance.time, "time", lambda: next(values))
    parent = provenance.ParentExecutionAuthority()
    assert parent.current_time() == 1_000
    with pytest.raises(provenance.ProvenanceError, match="trusted_clock_rollback"):
        parent.current_time()


def test_runtime_source_authenticates_commit_a_then_b_and_rejects_stale_a(tmp_path):
    from benchmarks.minecraft.k12_execution_provenance import (
        CheckoutObservation,
        ParentExecutionAuthority,
        ProvenanceError,
    )
    from benchmarks.minecraft.k12_live_runner import ExternalEntryFence
    from benchmarks.minecraft.k12_runtime_profile import load_k12_live_source_policy

    policy = load_k12_live_source_policy()
    subject = tmp_path / "subject"
    subject.mkdir()
    for entry in policy.entries:
        source = ROOT / entry.path
        target = subject / entry.path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        target.chmod(0o755 if entry.git_mode == "100755" else 0o644)
    _git(subject, "init", "-b", "issue-580-subject")
    _git(subject, "config", "user.name", "Issue 580")
    _git(subject, "config", "user.email", "issue580@example.invalid")
    _git(subject, "add", ".")
    _git(subject, "commit", "-m", "subject A")

    key = b"k" * 32
    parent = ParentExecutionAuthority(
        revision_verifier_key=key,
        revision_verifier_identity="issue-580-verifier/1",
    )
    now_a = parent.current_time()
    checkout_a, pull_request_a = _subject_observations(subject, observed_at=now_a)
    authorization_a = _signed_revision(
        parent, key, checkout_a, pull_request_a, expires_at=now_a + 300,
    )
    source_a = parent.collect_source_closure(
        root=subject, policy=policy, checkout=checkout_a,
        revision_authorization=authorization_a,
    )
    assert authorization_a.matches_source(source_a)

    changed = subject / "benchmarks/minecraft/k12_authority_contracts.py"
    changed.write_bytes(changed.read_bytes() + b"\n# subject revision B\n")
    _git(subject, "add", str(changed.relative_to(subject)))
    _git(subject, "commit", "-m", "subject B")
    now_b = parent.current_time()
    checkout_b, pull_request_b = _subject_observations(subject, observed_at=now_b)
    authorization_b = _signed_revision(
        parent, key, checkout_b, pull_request_b, expires_at=now_b + 300,
    )
    source_b = parent.collect_source_closure(
        root=subject, policy=policy, checkout=checkout_b,
        revision_authorization=authorization_b,
    )
    assert authorization_b.matches_source(source_b)
    assert authorization_a.head_commit != authorization_b.head_commit

    fence = ExternalEntryFence()
    with pytest.raises(ProvenanceError, match="git_head_mismatch"):
        checkout_b.validate(authorization_a)
    with pytest.raises(ProvenanceError, match="pr_semantic_mismatch"):
        pull_request_b.validate(authorization_a, now=now_b)
    assert authorization_a.matches_source(source_b) is False

    stale_checkout = CheckoutObservation(
        checkout_b.repository_identity, checkout_b.worktree_identity,
        checkout_b.git_dir_identity, checkout_b.common_dir_identity,
        checkout_b.symbolic_head_ref,
        "36a145338ae839eaf145b879d0317b24ae852300", checkout_b.head_tree,
        checkout_b.head_tree, checkout_b.upstream_ref,
        "36a145338ae839eaf145b879d0317b24ae852300",
        checkout_b.remote_repository, checkout_b.remote_ref,
        "36a145338ae839eaf145b879d0317b24ae852300",
        True, True, True, True,
    )
    with pytest.raises(ProvenanceError, match="git_head_mismatch"):
        stale_checkout.validate(authorization_b)
    assert all(value == 0 for value in fence.real_counts.values())
    assert all(value == 0 for value in fence.fake_counts.values())


def test_custom_contract_subclass_cannot_mint_qualification_receipt():
    from benchmarks.minecraft import k12_authority_contracts as contracts
    from benchmarks.minecraft.k12_execution_provenance import ParentExecutionAuthority

    assert not hasattr(ParentExecutionAuthority, "register_qualification_evidence")

    class FakeTerminal(contracts.QualificationTerminalEvidenceContract):
        pass

    class FakeEvidence(contracts.QualificationEvidenceContract):
        pass

    with pytest.raises(TypeError, match="module-owned"):
        class LiveQualificationAggregate(contracts.QualificationEvidenceContract):
            __module__ = "benchmarks.minecraft.k12_live_qualification"

    class FakeAuthority:
        identity = "sha256:" + "e" * 64

    fake = FakeEvidence()
    assert contracts.is_canonical_qualification_evidence(fake) is False
    _, receipt = contracts.install_qualification_evidence_contract(
        fake, aggregate_identity="sha256:" + "a" * 64,
        probe_aggregate_digest="sha256:" + "b" * 64,
        qualification_terminal_ledger_digest="sha256:" + "c" * 64,
        profile_digest="d" * 64, authority_binding=object(),
        authority_binding_canonical={}, evidence_origin="injected_fake",
        execution_provenance="live_qualification", passed=True,
        terminal_evidence=FakeTerminal(), authority=FakeAuthority(),
        controller=object(), aggregate_marker=object(),
        token=contracts._CONTRACT_MINT_TOKEN,
    )
    assert receipt.authenticates(fake) is True
    from benchmarks.minecraft.k12_execution_provenance import (
        ProvenanceError,
        _live_evidence_values,
    )
    with pytest.raises(ProvenanceError, match="final_prerequisite_mismatch"):
        _live_evidence_values(fake)
