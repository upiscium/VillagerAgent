import pytest

from benchmarks.minecraft.k12_evidence import CellBinding, EvidenceRegistry


def binding():
    return CellBinding("p", "c", "h", "cell", "trip", "arm", "fixture", "template", 7, "reset", 1, "attest")


def test_deterministic_digest_and_ids():
    a = EvidenceRegistry(binding()).register("oracle", {"ok": True, "items": [1, 2]})
    b = EvidenceRegistry(binding()).register("oracle", {"items": [1, 2], "ok": True})
    assert a.id == b.id and a.digest == b.digest and a.verify()


def test_duplicate_conflict_and_wrong_binding():
    registry = EvidenceRegistry(binding())
    first = registry.register("reset", {"valid": True})
    with pytest.raises(ValueError, match="duplicate"):
        registry.register("reset", {"valid": True})
    with pytest.raises(ValueError, match="binding"):
        registry.register("reset", {}, binding=CellBinding(*(("p", "c", "h", "cell", "trip", "other", "fixture", "template", 7, "reset", 1, "attest"))))
    assert first.kind == "reset"


def test_missing_type_tamper_and_immutability():
    registry = EvidenceRegistry(binding())
    record = registry.register("model_call", {"nested": {"value": [1]}})
    with pytest.raises(KeyError):
        registry.require("oracle", "missing")
    with pytest.raises(TypeError):
        registry.require("oracle", record.id)
    snapshot = registry.freeze()
    with pytest.raises(TypeError):
        snapshot.records[0].payload.items[0] = ("x", 1)
    assert snapshot.verify()
    object.__setattr__(snapshot.records[0], "digest", "sha256:" + "0" * 64)
    assert not snapshot.verify()
    with pytest.raises(RuntimeError):
        registry.register("oracle", {})
    with pytest.raises(RuntimeError):
        registry.freeze()
