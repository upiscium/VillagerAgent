import asyncio
from types import SimpleNamespace

import pytest

from env.movement_runtime import (
    AUTHORITATIVE_MOVEMENT_DEADLINE_SECONDS,
    MOVEMENT_REQUEST_DEADLINE_SECONDS,
    MovementAdmissionClosedError,
    MovementCoordinator,
    MovementOverlapError,
    MovementRequestDeadlineError,
    reconcile_mineflayer_state,
    run_with_movement_request_budget,
)
from env.movement_diagnostics import STRICT_PER_AXIS


def pos(x, y=0, z=0):
    return SimpleNamespace(x=x, y=y, z=z)


class Pathfinder:
    class goals:
        GoalNear = staticmethod(lambda x, y, z, r: (x, y, z, r))

    def __init__(self, bot):
        self.bot, self.goals_seen, self.clear_count, self.stop_count = bot, [], 0, 0

    def Movements(self, bot):
        return SimpleNamespace()

    def setMovements(self, movements):
        pass

    def setGoal(self, goal):
        self.goals_seen.append(goal)
        if goal is None:
            self.clear_count += 1

    def stop(self):
        self.stop_count += 1


def setup(start=0):
    bot = SimpleNamespace(entity=SimpleNamespace(position=pos(start)), clear_count=0)
    bot.clearControlStates = lambda: setattr(bot, "clear_count", bot.clear_count + 1)
    pathfinder = Pathfinder(bot)
    bot.pathfinder = pathfinder
    bot.blockAt = lambda _target: {"name": "air"}
    return bot, pathfinder


def test_success_and_metadata_cleanup():
    async def exercise():
        bot, pf = setup(0)
        async def advance(_): bot.entity.position = pos(0.5)
        runtime = MovementCoordinator(bot, pf, poll_interval_seconds=0, sleep=advance)
        result = await runtime.move(pos(0), tolerance=1, target_identity="home")
        assert result.success and result.reason == "reached"
        assert result.metadata["goal_clear_attempted"] is True
        assert result.metadata["goal_clear_succeeded"] is True
        assert pf.clear_count == 1
    asyncio.run(exercise())


def test_deadline_is_authoritative_and_before_thirty_seconds():
    async def exercise():
        bot, pf = setup(10)
        runtime = MovementCoordinator(bot, pf, deadline_seconds=.01, poll_interval_seconds=.001)
        result = await runtime.move(pos(0))
        assert result.reason == "deadline" and result.metadata["deadline"] < 30
        assert AUTHORITATIVE_MOVEMENT_DEADLINE_SECONDS < MOVEMENT_REQUEST_DEADLINE_SECONDS < 30
    asyncio.run(exercise())


def test_cancel_overlap_and_cancel_active():
    async def exercise():
        bot, pf = setup(10)
        runtime = MovementCoordinator(bot, pf, poll_interval_seconds=.001)
        task = asyncio.create_task(runtime.move(pos(0)))
        await asyncio.sleep(0)
        with pytest.raises(MovementOverlapError):
            await runtime.move(pos(1))
        cancelled = await runtime.cancel_active(.2, reason="controller_shutdown")
        assert cancelled == {"active": True, "cancel_requested": True, "terminal": True}
        result = await task
        assert result.reason == "cancelled"
        assert result.metadata["cancellation_reason"] == "controller_shutdown"
        assert not runtime.active
    asyncio.run(exercise())


def test_strict_per_axis_and_sanitized_error():
    async def exercise():
        bot, pf = setup(0)
        result = await MovementCoordinator(bot, pf).move(
            pos(0.5, 0.5, 0.5), tolerance=1, completion_policy=STRICT_PER_AXIS,
        )
        assert result.success
        pf.goals = None
        result = await MovementCoordinator(bot, pf, deadline_seconds=.01).move(pos(10))
        assert result.reason == "pathfinder_error"
        assert result.metadata["error_class"] == "RuntimeError"
        assert "unavailable" not in result.message
    asyncio.run(exercise())


def test_cooperative_polling_allows_ping_and_cleanup_once():
    async def exercise():
        bot, pf = setup(10)
        pings = []
        async def ping():
            pings.append(1)
        async def tick(_): await ping()
        runtime = MovementCoordinator(
            bot, pf, deadline_seconds=.01, poll_interval_seconds=0, sleep=tick,
        )
        result = await runtime.move(pos(0))
        assert result.reason == "deadline" and pings
        assert pf.clear_count == 1 and pf.stop_count == 1 and bot.clear_count == 1
        assert AUTHORITATIVE_MOVEMENT_DEADLINE_SECONDS == 9.0
    asyncio.run(exercise())


def test_independent_ping_task_completes_while_movement_is_active():
    async def exercise():
        bot, pf = setup(10)
        polling = asyncio.Event()
        release = asyncio.Event()

        async def held_poll(_interval):
            polling.set()
            await release.wait()

        runtime = MovementCoordinator(bot, pf, sleep=held_poll)
        movement = asyncio.create_task(runtime.move(pos(0)))
        await polling.wait()

        async def ping():
            await asyncio.sleep(0)
            return "pong"

        assert runtime.active is True
        assert await asyncio.wait_for(ping(), timeout=.05) == "pong"
        cancellation = await runtime.cancel_active(0)
        assert cancellation["terminal"] is True
        result = await movement
        terminal_goal_count = len(pf.goals_seen)
        await asyncio.sleep(0)
        assert result.reason == "cancelled"
        assert len(pf.goals_seen) == terminal_goal_count

    asyncio.run(exercise())


def test_stalled_cleanup_is_bounded_and_not_reported_terminal():
    async def exercise():
        bot, pf = setup(10)

        async def delayed_set_goal(goal):
            pf.goals_seen.append(goal)
            if goal is None:
                pf.clear_count += 1
                await asyncio.Event().wait()

        pf.setGoal = delayed_set_goal
        runtime = MovementCoordinator(bot, pf, sleep=lambda _: asyncio.sleep(10))
        task = asyncio.create_task(runtime.move(pos(0)))
        await asyncio.sleep(0)

        cancellation = await runtime.cancel_active(0, reason="controller_shutdown")
        assert cancellation["terminal"] is False
        result = await task
        assert result.reason == "cleanup_timeout"
        assert result.metadata["cleanup_completed"] is False
        assert result.metadata["goal_clear_succeeded"] is False
        assert pf.clear_count == 1
        assert runtime.admission_closed is True
        assert (await runtime.cancel_active())["terminal"] is False
        with pytest.raises(MovementAdmissionClosedError):
            await runtime.move(pos(1))

    asyncio.run(exercise())


def test_request_budget_bounds_after_two_successful_leases_and_cleans_third():
    async def exercise():
        bot, pf = setup(10)
        polls = 0

        async def advance(_interval):
            nonlocal polls
            polls += 1
            if polls <= 2:
                bot.entity.position = runtime._target
                await asyncio.sleep(0)
                return
            await asyncio.Event().wait()

        runtime = MovementCoordinator(bot, pf, sleep=advance)

        async def three_leases():
            await runtime.move(pos(0))
            bot.entity.position = pos(10)
            await runtime.move(pos(1))
            bot.entity.position = pos(10)
            await runtime.move(pos(2))

        with pytest.raises(MovementRequestDeadlineError):
            await run_with_movement_request_budget(
                three_leases(), timeout_seconds=.05,
            )

        assert runtime.active is False
        assert runtime.last_result.reason == "cancelled"
        assert runtime.last_result.metadata["cleanup_completed"] is True
        assert pf.clear_count == 3

    asyncio.run(exercise())


def test_external_task_cancellation_cleans_up_and_propagates():
    async def exercise():
        bot, pf = setup(10)
        runtime = MovementCoordinator(bot, pf, sleep=lambda _: asyncio.sleep(10))
        task = asyncio.create_task(runtime.move(pos(0)))
        await asyncio.sleep(0)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert runtime.last_result.reason == "cancelled"
        assert runtime.last_result.metadata["cleanup_completed"] is True
        assert pf.clear_count == 1

    asyncio.run(exercise())


def test_request_cancellation_during_cleanup_quarantines_admission():
    async def exercise():
        bot, pf = setup(0)
        cleanup_started = asyncio.Event()

        async def stalled_clear(goal):
            pf.goals_seen.append(goal)
            if goal is None:
                pf.clear_count += 1
                cleanup_started.set()
                await asyncio.Event().wait()

        pf.setGoal = stalled_clear
        runtime = MovementCoordinator(bot, pf)
        task = asyncio.create_task(runtime.move(pos(0)))
        await cleanup_started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert runtime.active is False
        assert runtime.admission_closed is True
        assert runtime.last_result.reason == "cleanup_timeout"
        assert runtime.last_result.metadata["cleanup_completed"] is False
        assert (await runtime.cancel_active())["terminal"] is False
        with pytest.raises(MovementAdmissionClosedError):
            await runtime.move(pos(1))

    asyncio.run(exercise())


def test_reconcile_mineflayer_state_reports_only_current_state():
    disconnected = SimpleNamespace(entity=None, player=None, _client=SimpleNamespace(state="login"))
    connected = SimpleNamespace(entity=None, player=object(), _client=SimpleNamespace(state="play"))
    ready = SimpleNamespace(entity=SimpleNamespace(position=pos(0)), player=None)

    assert reconcile_mineflayer_state(disconnected) == {
        "connected": False, "ready": False, "source": "reconciled_current_state",
    }
    assert reconcile_mineflayer_state(connected)["connected"] is True
    assert reconcile_mineflayer_state(connected)["ready"] is False
    assert reconcile_mineflayer_state(ready)["connected"] is True
    assert reconcile_mineflayer_state(ready)["ready"] is True
