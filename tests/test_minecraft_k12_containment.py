from benchmarks.minecraft.k12_containment import ContainmentController, ContainmentState, FakeSystemdClient

def test_term_then_kill_and_quarantine():
    client = FakeSystemdClient("unit-1"); client.empty = False
    controller = ContainmentController(client, deadline_ns=10)
    assert controller.contain(now_ns=5).state is ContainmentState.TERM_SENT
    client.empty = True
    result = controller.contain(now_ns=10)
    assert result.state is ContainmentState.QUARANTINED
    assert client.signals == ["TERM"]

def test_surviving_descendant_is_durable_failure():
    client = FakeSystemdClient("unit-2"); client.empty = False; controller = ContainmentController(client, deadline_ns=1)
    result = controller.contain(now_ns=1)
    assert result.state is ContainmentState.INFRASTRUCTURE_FAILURE and result.blocked_next_launch
