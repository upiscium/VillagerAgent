import pytest

from benchmarks.minecraft.k12_request import (
    K12RequestError, repetition_check, request_content, request_content_digest,
)


ACTION = {"identity": "MineBlock", "version": 1, "digest": "a" * 64}


def test_content_is_run_independent_and_binds_actor_and_action():
    attack = {**ACTION, "identity": "attackTarget"}
    args = {"target_name": "zombie"}
    left = request_content("Alice", attack, args, {"target_name": "zombie"})
    right = request_content("Alice", attack, {"target_name": "zombie"}, {"target_name": "zombie"})
    assert left == right
    assert request_content_digest("Alice", attack, args, {"target_name": "zombie"})
    assert repetition_check(left, right)


def test_attack_target_only_declares_native_semantics():
    attack = {**ACTION, "identity": "attackTarget"}
    assert request_content("A", attack, {"target_name": "zombie"}, None)["arguments"] == {
        "target_name": "zombie"}
    with pytest.raises(K12RequestError):
        request_content("A", attack, {"target_name": "zombie", "emotion": ["😢"]}, None)
    with pytest.raises(K12RequestError):
        request_content("A", attack, {"target_name": "zombie", "murmur": "hello"}, None)
    with pytest.raises(K12RequestError):
        request_content("A", ACTION, {"x": 1, "y": 2, "z": 3, "v": 1.5}, None)


@pytest.mark.parametrize("arguments", [
    {"x": True, "y": 2, "z": 3},
    {"x": "1", "y": 2, "z": 3},
])
def test_request_argument_types_match_the_frozen_schema(arguments):
    with pytest.raises(K12RequestError):
        request_content("A", ACTION, arguments, None)


def test_handover_quantity_must_be_positive():
    handover = {**ACTION, "identity": "handoverBlock"}
    with pytest.raises(K12RequestError):
        request_content("A", handover, {
            "target_player_name": "Bob", "item_name": "stone", "item_count": 0,
        }, None)
