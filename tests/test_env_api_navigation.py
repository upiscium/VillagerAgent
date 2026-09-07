import asyncio
from types import SimpleNamespace

import pytest

from env.movement_diagnostics import STRICT_PER_AXIS
from env.movement_runtime import (
    MovementCoordinator,
    MovementEffectUnknownError,
    MovementRequestDeadlineError,
    run_with_movement_request_budget,
)


class Position:
    def __init__(self, x, y, z):
        self.x = x
        self.y = y
        self.z = z

    def offset(self, x, y, z):
        return Position(self.x + x, self.y + y, self.z + z)


class FakePathfinder:
    def __init__(self):
        self.goals = []
        self.movements = []

    def setGoal(self, goal):
        self.goals.append(goal)

    def setMovements(self, movements):
        self.movements.append(movements)


class FakeBot:
    def __init__(self, position):
        self.entity = SimpleNamespace(position=position)
        self.pathfinder = FakePathfinder()
        self.heldItem = None

    def blockAt(self, _position):
        return {"name": "air"}


class FakePathfinderModule:
    class Movements:
        def __init__(self, _bot):
            pass

    class goals:
        @staticmethod
        def GoalNear(x, y, z, radius):
            return (x, y, z, radius)


def test_move_to_waits_for_asynchronous_goal_completion(monkeypatch):
    from env.env_api import move_to

    bot = FakeBot(Position(14, -59, 5))
    target = Position(5, -60, 5)
    sleeps = []

    def advance_pathfinder(interval):
        sleeps.append(interval)
        if len(sleeps) == 3:
            bot.entity.position = Position(5.5, -60.0, 5.5)

    monkeypatch.setattr("env.env_api.time.sleep", advance_pathfinder)

    passed, _ = move_to(
        FakePathfinderModule,
        bot,
        Position,
        1.0,
        target,
        completion_policy=STRICT_PER_AXIS,
        position_convention="entity_feet",
        navigation_timeout_seconds=1.0,
        poll_interval_seconds=0.1,
    )

    assert passed is True
    assert sleeps == [0.1, 0.1, 0.1]
    assert bot.pathfinder.goals == [(5, -60, 5, 0)]


def test_move_to_stops_pathfinder_goal_on_timeout(monkeypatch):
    from env.env_api import move_to

    bot = FakeBot(Position(7.5, -60, 5.25))
    target = Position(5, -60, 5)
    clock = [0.0]

    monkeypatch.setattr("env.env_api.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "env.env_api.time.sleep",
        lambda interval: clock.__setitem__(0, clock[0] + interval),
    )

    passed, message = move_to(
        FakePathfinderModule,
        bot,
        Position,
        1.0,
        target,
        completion_policy=STRICT_PER_AXIS,
        position_convention="entity_feet",
        navigation_timeout_seconds=0.3,
        poll_interval_seconds=0.1,
    )

    assert passed is False
    assert "can not reach position" in message
    assert "navigation timeout=0.3s" in message
    assert bot.pathfinder.goals == [(5, -60, 5, 0), None]


def test_interact_nearest_uses_cooperative_runner_without_legacy_goal(monkeypatch):
    from env.env_api import interact_nearest

    target = Position(5, 0, 0)
    bot = FakeBot(Position(0, 0, 0))
    calls = []

    async def movement_runner(position, tolerance):
        calls.append((position, tolerance))
        return SimpleNamespace(success=False, message="movement deadline exceeded")

    monkeypatch.setattr("env.env_api.find_nearest_", lambda *_args: target)
    message, success, data = asyncio.run(interact_nearest(
        FakePathfinderModule, bot, Position, {}, SimpleNamespace(), 3, "stone",
        movement_runner=movement_runner,
    ))

    assert (message, success, data) == ("movement deadline exceeded", False, [])
    assert calls == [(target, 3)]
    assert bot.pathfinder.goals == []


def test_use_on_disallows_legacy_fallback_after_cooperative_selection(monkeypatch):
    from env.env_api import useOnNearest

    bot = FakeBot(Position(0, 0, 0))
    bot.entity.username = "Alice"
    bot.chat = lambda *_args: None
    entity = {"position": Position(10, 0, 0), "name": "sheep"}
    entity = type("Entity", (dict,), {"__getattr__": dict.__getitem__})(entity)
    monkeypatch.setattr("env.env_api.equip", lambda *_args: ("equipped", True))
    monkeypatch.setattr(
        "env.env_api.get_entity_by",
        lambda *_args, **_kwargs: [entity],
    )
    monkeypatch.setattr(
        "env.env_api.move_to",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy movement must not run")
        ),
    )

    message, success = useOnNearest(
        bot, Position, FakePathfinderModule, {}, SimpleNamespace(), [],
        "shears", "sheep", allow_movement=False,
    )

    assert success is False
    assert message == "cannot reach sheep"


def test_dig_retry_movements_share_request_budget_and_cleanup(monkeypatch):
    from env.env_api import dig_at_cooperative

    async def exercise():
        bot = FakeBot(Position(0, 0, 0))
        bot.chat = lambda *_args: None
        polls = 0

        async def movement_poll(_interval):
            nonlocal polls
            polls += 1
            if polls == 1:
                bot.entity.position = runtime._target
                await asyncio.sleep(0)
                return
            await asyncio.Event().wait()

        runtime = MovementCoordinator(
            bot, FakePathfinderModule, sleep=movement_poll,
        )
        async def run_movement(target, tolerance):
            return await runtime.move(target, tolerance=tolerance)
        monkeypatch.setattr("env.env_api.random.randint", lambda *_args: 4)

        with pytest.raises(MovementRequestDeadlineError):
            await run_with_movement_request_budget(
                dig_at_cooperative(
                    bot, FakePathfinderModule, Position, (0, 0, 0), run_movement,
                ),
                timeout_seconds=.05,
            )

        assert runtime.active is False
        assert runtime.last_result.reason == "cancelled"
        assert runtime.last_result.metadata["cleanup_completed"] is True
        assert bot.pathfinder.goals.count(None) == 3

    asyncio.run(exercise())


def test_place_candidate_movements_share_request_budget_and_cleanup(monkeypatch):
    from env.env_api import place_axis

    async def exercise():
        bot = FakeBot(Position(0, 0, 0))
        bot.heldItem = SimpleNamespace(name="stone")
        bot.dig = lambda *_args: None
        bot.blockAt = lambda position: {
            "name": "stone" if position.y < 0 else "air",
        }
        polls = 0

        async def movement_poll(_interval):
            nonlocal polls
            polls += 1
            if polls <= 2:
                bot.entity.position = runtime._target
                await asyncio.sleep(0)
                return
            await asyncio.Event().wait()

        async def placement_fails(*_args, **_kwargs):
            return False

        runtime = MovementCoordinator(
            bot, FakePathfinderModule, sleep=movement_poll,
        )
        async def run_movement(target, tolerance):
            return await runtime.move(target, tolerance=tolerance)
        monkeypatch.setattr("env.env_api.place_block", placement_fails)

        with pytest.raises(MovementRequestDeadlineError):
            await run_with_movement_request_budget(
                place_axis(
                    bot, SimpleNamespace(), FakePathfinderModule, Position,
                    "stone", (0, 0, 0), "A", movement_runner=run_movement,
                ),
                timeout_seconds=.05,
            )

        assert runtime.active is False
        assert runtime.last_result.reason == "cancelled"
        assert runtime.last_result.metadata["cleanup_completed"] is True
        assert bot.pathfinder.goals.count(None) >= 3

    asyncio.run(exercise())


def test_place_propagates_effect_unknown_without_trying_another_candidate(monkeypatch):
    from env.env_api import place_axis

    async def exercise():
        bot = FakeBot(Position(0, 0, 0))
        bot.heldItem = SimpleNamespace(name="stone")
        bot.dig = lambda *_args: None
        bot.blockAt = lambda position: {
            "name": "stone" if position.y < 0 else "air",
        }
        calls = 0

        async def movement_unknown(_target, _tolerance):
            nonlocal calls
            calls += 1
            raise MovementEffectUnknownError(
                "cleanup unknown", reason="cleanup_timeout",
                status_code=503, terminal=False,
            )

        monkeypatch.setattr(
            "env.env_api.place_block", lambda *_args, **_kwargs: None,
        )
        with pytest.raises(MovementEffectUnknownError):
            await place_axis(
                bot, SimpleNamespace(), FakePathfinderModule, Position,
                "stone", (0, 0, 0), "A", movement_runner=movement_unknown,
            )
        assert calls == 1

    asyncio.run(exercise())
