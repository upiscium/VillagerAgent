from dataclasses import replace

from benchmarks.minecraft.k12_fixture import K12Cell, build_k12_fixture
from benchmarks.minecraft.k12_oracle import K12Oracle, OracleValue, make_fact
from benchmarks.minecraft.k12_backends import K12FakeBackend


def test_oracle_covers_positive_negative_unknown_and_not_applicable():
    fixture = build_k12_fixture()
    cell = K12Cell("cell-1", fixture.fixture_id, "MineBlock")
    assert K12Oracle(fixture, cell).evaluate("e") is OracleValue.UNKNOWN
    positive = make_fact(fixture, cell, "e", OracleValue.TRUE, reset_generation=1)
    assert K12Oracle(fixture, cell, positive, reset_generation=1).evaluate("e") is OracleValue.TRUE
    negative = make_fact(fixture, cell, "e", OracleValue.FALSE, reset_generation=1)
    assert K12Oracle(fixture, cell, negative, reset_generation=1).evaluate("e") is OracleValue.FALSE
    assert K12Oracle(fixture, cell, positive, reset_generation=1).evaluate("other") is OracleValue.UNKNOWN
    assert K12Oracle(fixture, K12Cell("other", fixture.fixture_id, "unsupported")).evaluate("e") is OracleValue.NOT_APPLICABLE


def test_oracle_payload_has_no_evaluator_alternative():
    fixture = build_k12_fixture()
    cell = K12Cell("cell-1", fixture.fixture_id, "MineBlock")
    payload = K12Oracle(fixture, cell).payload("effect")
    assert "alternative" not in str(payload).lower()


def test_oracle_derives_mine_place_navigation_and_handover_from_observations():
    fixture = build_k12_fixture(); backend = K12FakeBackend(fixture)
    for action, kwargs in (("MineBlock", {"position": (1, 64, 0)}),
                           ("navigateTo", {"target": (4, 64, 0)})):
        effect = getattr(backend, action)(**kwargs)
        cell = K12Cell(action, fixture.fixture_id, action)
        assert K12Oracle(fixture, cell, reset_generation=1).evaluate_bound(
            effect.effect_digest, effect.before, effect.after, kwargs,
            cell_id=cell.cell_id, reset_generation=1) is OracleValue.TRUE
    effect = backend.handoverBlock(item="stone", quantity=1)
    cell = K12Cell("handover", fixture.fixture_id, "handoverBlock")
    assert K12Oracle(fixture, cell, reset_generation=1).evaluate_bound(
        effect.effect_digest, effect.before, effect.after,
        {"item": "stone", "quantity": 1}, cell_id=cell.cell_id,
        reset_generation=1) is OracleValue.TRUE


def test_oracle_marks_missing_and_concurrent_attack_as_not_applicable_or_unknown():
    fixture = build_k12_fixture(); backend = K12FakeBackend(fixture)
    missing = backend.attackTarget(target="missing", damage=1)
    cell = K12Cell("attack", fixture.fixture_id, "attackTarget")
    oracle = K12Oracle(fixture, cell, reset_generation=1)
    assert oracle.evaluate_bound(missing.effect_digest, missing.before, missing.after,
                                 {"target": "missing", "damage": 1},
                                 cell_id=cell.cell_id, reset_generation=1) is OracleValue.NOT_APPLICABLE
    effect = backend.attackTarget(target="zombie", damage=1)
    altered = replace(effect.after, entities=(("zombie", (15, False)),))
    assert oracle.evaluate_bound(effect.effect_digest, effect.before, altered,
                                 {"target": "zombie", "damage": 1},
                                 cell_id=cell.cell_id, reset_generation=1) is OracleValue.UNKNOWN


def test_oracle_accepts_exact_damage_when_target_survives():
    fixture = build_k12_fixture(action="attackTarget")
    backend = K12FakeBackend(fixture)
    effect = backend.attackTarget(target="zombie", damage=1)
    cell = K12Cell("attack-survives", fixture.fixture_id, "attackTarget")
    assert K12Oracle(fixture, cell, reset_generation=1).evaluate_bound(
        effect.effect_digest, effect.before, effect.after,
        {"target": "zombie", "damage": 1},
        cell_id=cell.cell_id, reset_generation=1,
    ) is OracleValue.TRUE


def test_bound_oracle_rejects_cross_cell_or_reset_generation_observations():
    fixture = build_k12_fixture(); backend = K12FakeBackend(fixture)
    effect = backend.MineBlock(position=(1, 64, 0))
    cell = K12Cell("cell-bound", fixture.fixture_id, "MineBlock")
    oracle = K12Oracle(fixture, cell, reset_generation=7)
    arguments = {"position": (1, 64, 0)}
    assert oracle.evaluate_bound(effect.effect_digest, effect.before, effect.after, arguments,
                                 cell_id=cell.cell_id, reset_generation=7) is OracleValue.TRUE
    assert oracle.evaluate_bound(effect.effect_digest, effect.before, effect.after, arguments,
                                 cell_id="other", reset_generation=7) is OracleValue.UNKNOWN
    assert oracle.evaluate_bound(effect.effect_digest, effect.before, effect.after, arguments,
                                 cell_id=cell.cell_id, reset_generation=8) is OracleValue.UNKNOWN
