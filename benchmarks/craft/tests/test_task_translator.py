from benchmarks.craft.craft_protocol import CraftPrivateView, CraftPublicState
from benchmarks.craft.villager.task_translator import craft_task_to_villager_objective


def test_task_translator_does_not_emit_target_coordinates():
    private = CraftPrivateView("D1", "front", {}, "private", {})
    public = CraftPublicState(1, [], [], {}, None)
    objective = craft_task_to_villager_objective(
        director_id="D1",
        private_view=private,
        public_state=public,
    )
    assert "partial-information" in objective
    assert "Build target coordinates" not in objective
    assert "[(0,0,0)" not in objective
