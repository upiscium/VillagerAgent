import threading

import pytest
import benchmarks.minecraft.k12_live_state as live_state
from benchmarks.minecraft.k12_live_state import *
PROFILE="0"*64
def test_only_typed_factories_render_commands():
    c=setblock((0,64,0),"stone"); assert c.text=="setblock 0 64 0 stone"
    with pytest.raises(LiveStateError): RconCommand(CommandKind.SETBLOCK,(),False)
    query=forceload_query(32,-17); plan=make_plan((query,))
    assert execute_plan(plan,MockTransport({query.text:"Chunk at [2, -2] in minecraft:overworld is marked for force loading"}))[0].value
def test_mock_marker_and_bounds():
    p=make_plan([data_pos("agent")]); t=MockTransport({p.commands[0].text: 'agent has the following entity data: [1.0d,2.0d,3.0d]'})
    result=execute_plan(p,t)[0]
    assert result.value==(1, 2, 3) and len(result.raw_sha256)==64
    with pytest.raises(LiveStateError): execute_plan(p,lambda x:"bad")
    with pytest.raises(LiveStateError): pos((0,64,"bad"))

def test_mock_authority_binding_propagates_through_plan_result_and_state():
    command = data_pos("agent")
    plan = make_plan((command,))
    results = execute_plan(
        plan,
        MockTransport({command.text: 'agent has the following entity data: [0.0d,64.0d,0.0d]'}),
    )
    state = normalize_state(plan, results)
    assert plan.authority_binding.provenance == "mock_only"
    assert results[0].authority_binding == plan.authority_binding
    assert state.authority_binding == plan.authority_binding

def test_state_digest_canonicalizes_field_provenance_and_authority_binding():
    command = data_pos("agent")
    plan = make_plan((command,))
    results = execute_plan(
        plan,
        MockTransport({command.text: 'agent has the following entity data: [0.0d,64.0d,0.0d]'}),
    )
    first = normalize_state(plan, results)
    second = normalize_state(plan, results)
    assert first.field_provenance
    assert all(isinstance(value, FieldProvenance) for value in first.field_provenance)
    assert canonical_digest_value(first.authority_binding) == first.authority_binding.canonical()
    assert len(first.digest) == 64
    assert first.digest == second.digest
def test_raw_digest_pairs_are_strict():
    with pytest.raises(LiveStateError): RawDigestPair("x","bad")

def test_parsers_timeout_ack_and_reset_freshness_fail_closed():
    p=make_plan([count_items("agent","stone")],profile=PROFILE,campaign="c",cell="x",reset_token="fresh",generation=1)
    with pytest.raises(LiveStateError,match="timeout"): execute_plan(p,MockTransport({p.commands[0].text:Timeout}))
    with pytest.raises(LiveStateError,match="acknowledgement"): execute_plan(p,MockTransport({p.commands[0].text:"bad|1"}))
    with pytest.raises(LiveStateError,match="typed"): execute_plan(p,MockTransport({p.commands[0].text:"Found many matching items on player agent"}))
    consume_reset_token("unique",1,profile=PROFILE,campaign="c",cell="x")
    with pytest.raises(LiveStateError,match="stale"): consume_reset_token("unique",1,profile=PROFILE,campaign="c",cell="x")


def test_concurrent_reset_token_consumption_has_exactly_one_winner(monkeypatch):
    class RecordingLock:
        def __init__(self):
            self._lock = threading.RLock()
            self._stats_lock = threading.Lock()
            self.entered = 0
            self.active = 0
            self.max_active = 0

        def __enter__(self):
            self._lock.acquire()
            with self._stats_lock:
                self.entered += 1
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            with self._stats_lock:
                self.active -= 1
            self._lock.release()

    reset_lock = RecordingLock()
    monkeypatch.setattr(live_state, "_RESET_LOCK", reset_lock)
    ready = threading.Barrier(2)
    outcomes = []
    outcomes_lock = threading.Lock()

    def consume() -> None:
        ready.wait()
        try:
            consume_reset_token(
                "atomic-concurrent-token",
                1,
                profile=PROFILE,
                campaign="atomic-concurrent-campaign",
                cell="atomic-concurrent-cell",
            )
        except LiveStateError:
            outcome = "rejected"
        else:
            outcome = "consumed"
        with outcomes_lock:
            outcomes.append(outcome)

    threads = (threading.Thread(target=consume), threading.Thread(target=consume))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["consumed", "rejected"]
    assert reset_lock.entered == 2 and reset_lock.max_active == 1

def test_identifier_selector_and_snbt_bounds():
    with pytest.raises(LiveStateError): tag_selector("bad tag")
    with pytest.raises(LiveStateError): item_snbt("@e[type=item]")
    with pytest.raises(LiveStateError): parse_snbt("{not-snbt}")

def test_realistic_en_us_results_are_plan_bound_and_normalized():
    commands=(data_pos("agent"),count_items("agent","stone"),data_inventory("agent"),execute_if_block((1,64,0),"stone"))
    plan=make_plan(commands,profile=PROFILE,campaign="c",cell="x",reset_token="n",generation=2)
    script={
        commands[0].text:"agent has the following entity data: [0.0d,64.0d,0.0d]",
        commands[1].text:"Found 1 matching item on player agent",
        commands[2].text:'agent has the following entity data: [{Slot: 0b, id: "minecraft:stone", Count: 1b}]',
        commands[3].text:"Test passed, count: 1",
    }
    results=execute_plan(plan,MockTransport(script)); state=normalize_state(plan,results)
    assert state.positions == (("agent",(0.0,64.0,0.0)),)
    assert state.inventories == (("agent",(("stone",1),)),)
    assert state.blocks == (((1,64,0),"stone"),)
    assert len({result.command_id for result in results}) == len(results)
    with pytest.raises(LiveStateError,match="order"):
        normalize_state(plan,tuple(reversed(results)))

def test_synthetic_envelope_and_subject_splice_are_rejected():
    command=data_pos("agent"); plan=make_plan((command,))
    for raw in ("[en_us] pos: [0,64,0]","other has the following entity data: [0.0d,64.0d,0.0d]"):
        with pytest.raises(LiveStateError): execute_plan(plan,MockTransport({command.text:raw}))

def test_minecraft_1_19_2_mutation_acknowledgements_and_duplicate_count():
    commands=(gamerule("doDaylightCycle","false"),clear_all("agent"),give("agent","stone",1))
    plan=make_plan(commands)
    execute_plan(plan,MockTransport({
        commands[0].text:"Gamerule doDaylightCycle is now set to: false",
        commands[1].text:"Removed 3 item(s) from player agent",
        commands[2].text:"Gave 1 [Stone] to agent",
    }))
    command=data_inventory("agent"); bad=make_plan((command,))
    raw='agent has the following entity data: [{Slot: 0b, id: "minecraft:stone", Count: 1b, count: 1}]'
    with pytest.raises(LiveStateError): execute_plan(bad,MockTransport({command.text:raw}))
