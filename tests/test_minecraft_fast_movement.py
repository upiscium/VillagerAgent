import ast
from pathlib import Path


SOURCE = Path("env/minecraft_server_fast.py")
HTTP_CONTRACT_SOURCE = Path("env/movement_http_contract.py")


def _function(tree, name):
    return next(node for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name)


def test_fast_movement_awaits_cooperative_runtime_without_threads():
    source = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    handler = _function(tree, "move_to_pos")
    calls = [node for node in ast.walk(handler) if isinstance(node, ast.Call)]

    assert any(isinstance(node, ast.Await) for node in ast.walk(handler))
    assert any(isinstance(call.func, ast.Name) and call.func.id == "_execute_movement"
               for call in calls)
    assert not any(isinstance(call.func, ast.Name) and call.func.id == "move_to"
                   for call in calls)
    assert "asyncio.to_thread" not in source
    assert "run_in_executor" not in source


def test_fast_bridge_has_explicit_movement_cancel_and_overlap_contract():
    source = SOURCE.read_text(encoding="utf-8")

    assert "@app.post('/post_cancel_movement')" in source
    assert '"movement already active"' in source
    assert "status_code=409" in source
    assert "AUTHORITATIVE_MOVEMENT_DEADLINE_SECONDS" in source
    assert "MOVEMENT_REQUEST_DEADLINE_SECONDS" in source


def test_ping_handler_remains_independent_of_movement_coordinator():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    handler = _function(tree, "ping")

    assert not any(
        isinstance(node, ast.Name) and node.id == "movement_coordinator"
        for node in ast.walk(handler)
    )


def test_fast_bridge_routes_all_reachable_pathfinder_movement_through_coordinator():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    legacy_names = {"move_to", "move_to_nearest_"}

    assert not any(
        isinstance(call.func, ast.Name) and call.func.id in legacy_names
        for call in ast.walk(tree) if isinstance(call, ast.Call)
    )
    for call in (
        call for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id in {"interact_nearest", "dig_at_cooperative", "place_axis"}
    ):
        assert any(keyword.arg == "movement_runner" for keyword in call.keywords)

    assert any(
        isinstance(node, ast.Attribute) and node.attr == "call_soon_threadsafe"
        for function in ast.walk(tree)
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        and function.name == "handle"
        for node in ast.walk(function)
    )

    use_on = _function(tree, "use_on")
    use_on_calls = [node for node in ast.walk(use_on) if isinstance(node, ast.Call)]
    helper_call = next(
        call for call in use_on_calls
        if isinstance(call.func, ast.Name) and call.func.id == "useOnNearest"
    )
    assert any(
        keyword.arg == "allow_movement"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is False
        for keyword in helper_call.keywords
    )


def test_shutdown_cancellation_closes_item_drop_movement_admission():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    cancel = _function(tree, "cancel_movement")
    scheduler = _function(tree, "_start_item_drop_movement")

    assert any(
        isinstance(call.func, ast.Name)
        and call.func.id == "_cancel_movement_and_close_admission"
        for call in ast.walk(cancel) if isinstance(call, ast.Call)
    )
    assert any(
        isinstance(node, ast.Name) and node.id == "bridge_accepting_movement"
        for node in ast.walk(scheduler)
    )
    assert any(
        isinstance(node, ast.Name) and node.id == "item_drop_tasks"
        for node in ast.walk(scheduler)
    )


def test_coordinated_movement_routes_share_authoritative_request_budget():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    movement_handlers = {
        "hand", "move_to_", "move_to_pos", "use_on", "sleep_", "dig", "place",
        "get", "put", "smelt", "craft", "enchant", "trade", "repair", "eat",
        "drink", "wear", "open_", "close_", "activate",
    }

    for name in movement_handlers:
        function = _function(tree, name)
        assert not any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id == "timeout"
            for decorator in function.decorator_list
        )
        assert any(
            isinstance(decorator, ast.Name)
            and decorator.id == "movement_request_budget"
            for decorator in function.decorator_list
        )

    wear = _function(tree, "wear")
    handlers = [node for node in ast.walk(wear) if isinstance(node, ast.ExceptHandler)]
    assert all(handler.type is not None for handler in handlers)
    assert any(
        isinstance(node, ast.Name) and node.id == "CooperativeMovementError"
        for handler in handlers for node in ast.walk(handler.type)
    )


def test_cleanup_nonterminal_uses_explicit_effect_unknown_http_contract():
    source = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    execute = _function(tree, "_execute_movement")

    assert "cleanup_completed" in ast.unparse(execute)
    assert "MovementEffectUnknownError" in ast.unparse(execute)
    assert "app.add_exception_handler(MovementEffectUnknownError" in source
    handler_source = HTTP_CONTRACT_SOURCE.read_text(encoding="utf-8")
    assert "OUTCOME_CERTAINTY_HEADER" in handler_source
    assert "RETRY_SAFE_HEADER" in handler_source
    assert "MOVEMENT_TERMINAL_HEADER" in handler_source
    assert "MOVEMENT_FAILURE_REASON_HEADER" in handler_source
