# Minecraft E2E Verification

Issue: #31

Date: 2026-06-08

## Setup

- Repository branch: `issue-31-minecraft-e2e-verification`
- Runtime entry point: `env.env.VillagerBench`
- Environment type: `env_type.none`
- Agent: `Alice`
- Agent bridge: `env/minecraft_server_fast.py`
- Local bridge URL: `http://127.0.0.1:5000`
- Minecraft server host: `10.12.1.200`
- Minecraft server port: `40000`
- Minecraft world: `world`
- Python bridge dependencies: `javascript`, `fastapi`, and `uvicorn` import successfully
- Local Java availability: `java` was not installed, so no local Minecraft server was launched

## Connectivity Checks

- `127.0.0.1:25565`: closed (`connect_ex=111`)
- `10.12.1.200:40000`: reachable (`connect_ex=0`)
- Node runtime: `v22.22.3`

## Minimal E2E Run

The verification launched one FastAPI/mineflayer bridge for `Alice`, connected it to the remote Minecraft server, pinged the bridge, fetched the initial environment state, executed a non-destructive movement action, and fetched the final environment state.

Command shape:

```bash
python -c "from env.env import VillagerBench, env_type, Agent; env=VillagerBench(env_type.none, task_id=0, dig_needed=False, host='10.12.1.200', port=40000, _virtual_debug=False); env.agent_register(agent_tool=[Agent.performMovement], agent_number=1, name_list=['Alice']);
with env.run(fast_api=True):
    print('ping', env.agents_ping()); print('before', env.get_init_state()); print('action', Agent.performMovement.func(player_name='Alice', action_name='jump', seconds=1, emotion=[], murmur='')); print('after', env.get_init_state())"
```

Observed bridge launch:

```text
python env/minecraft_server_fast.py -H "10.12.1.200" -P 40000 -LP 5000 -U "Alice" -W "world" -D False
```

Observed success signals:

```text
ping {'message': 'all agents are online', 'status': True}
action {'message': 'I jump in a few seconds', 'status': True}
before position [426, 78, 48]
after position [426, 79, 48]
```

The bridge also returned HTTP 200 for `/post_ping`, `/post_environment_dict`, and `/post_action`.

## Result

Status: success for the minimal runtime-to-Minecraft action path.

Verified path:

```text
Python VillagerBench runtime -> local FastAPI bridge -> mineflayer bot -> remote Minecraft server -> action response -> environment state refresh
```

## Notes

- The run used `env_type.none`, so no benchmark judger was launched and no scored construction/farming/puzzle task completion was measured.
- A `/post_emojimurmur` request returned HTTP 404 during the movement action, but the movement endpoint itself returned success and the observed Y coordinate changed from `78` to `79`.
- The mineflayer bridge emitted a deprecation warning for `physicTick`; it did not block the run.

## Next Debugging Target

Move from the bridge/action smoke test to a judged benchmark task:

- Start with a single-agent `env_type.construction` task on a disposable world or resettable server.
- Confirm judger startup by observing `.cache/load_status.cache` transition to `loaded`.
- Record score/task completion via `env.get_score()` after a bounded controller run.
