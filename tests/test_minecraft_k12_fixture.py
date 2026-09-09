from benchmarks.minecraft.k12_backends import K12FakeBackend
from benchmarks.minecraft.k12_fixture import K12_ACTIONS, K12Cell, build_k12_fixture


def test_fixture_and_fake_backend_are_frozen_and_deterministic():
    fixture = build_k12_fixture()
    assert fixture.planner_payload()["actions"] == K12_ACTIONS
    assert "alternative" not in str(fixture.planner_payload()).lower()
    first = K12FakeBackend(fixture).MineBlock(position=(1, 64, 0))
    second = K12FakeBackend(fixture).MineBlock(position=(1, 64, 0))
    assert first == second


def test_only_the_five_frozen_actions_are_available():
    fixture = build_k12_fixture()
    backend = K12FakeBackend(fixture)
    for action in K12_ACTIONS:
        assert getattr(backend, action)(value=1).action == action


def test_each_stratum_has_hidden_alternative_and_real_mapping_is_inert():
    fixture = build_k12_fixture()
    assert fixture.original.block((1, 64, 0)) == "stone"
    assert fixture.alternative.block((1, 64, 0)) == "dirt"
    assert "alternative" not in str(fixture.planner_payload()).lower()
    assert {a.mapping for a in fixture.actions} == {
        "post_dig", "post_place", "post_move_to_pos", "post_attack", "post_hand",
    }


def test_fake_backend_transitions_block_facing_inventory_position_and_entity_state():
    backend = K12FakeBackend(build_k12_fixture())
    placed = backend.placeBlock(position=(0, 64, 0), item="stone", facing="east")
    assert placed.after.block((0, 64, 0)) == "stone"
    assert isinstance(placed.after.inventory, tuple)
    assert placed.after.inventory == (("agent", ()), ("villager", ()))
    assert dict(placed.after.facings)[(0, 64, 0)] == "east"
    moved = backend.navigateTo(target=(3, 64, 2))
    assert moved.after.position() == (3, 64, 2)
    attacked = backend.attackTarget(target="zombie", damage=20)
    assert attacked.after.entity("zombie") == (0, True)
