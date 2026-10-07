import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "env/k11_visible_block_capture.js"


def _node_capture(source):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the standalone K11 capture helper")
    script = f"""
const helper = require({json.dumps(str(HELPER))});
class Vec3 {{ constructor(x, y, z) {{ this.x = x; this.y = y; this.z = z; }} }}
const mcData = {{ blocksByName: {{
  air: {{ id: 0, name: 'air' }}, cave_air: {{ id: 1, name: 'cave_air' }},
  void_air: {{ id: 2, name: 'void_air' }}, glass: {{ id: 20, name: 'glass' }}
}} }};
{source}
"""
    completed = subprocess.run(
        [node, "-e", script], cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return json.loads(completed.stdout)


def test_capture_is_fixed_and_hostile_gameplay_apis_are_never_touched():
    result = _node_capture(r"""
const reads = [];
const forbidden = [];
const mutations = [];
function forbiddenMethod(name) {
  return () => { forbidden.push(name); throw new Error('passive capture attempted an action'); };
}
const position = new Proxy({ x: 0.2, y: 10.1, z: -0.2 }, {
  set(_target, key) { mutations.push(`position.${String(key)}`); throw new Error('mutation'); },
  defineProperty(_target, key) { mutations.push(`position.${String(key)}`); throw new Error('mutation'); },
  deleteProperty(_target, key) { mutations.push(`position.${String(key)}`); throw new Error('mutation'); }
});
const entity = new Proxy({ position, eyeHeight: 1.62 }, {
  get(target, key, receiver) {
    if (key in target) return Reflect.get(target, key, receiver);
    forbidden.push(`entity.${String(key)}`);
    throw new Error('unexpected entity API access');
  },
  set(_target, key) { mutations.push(`entity.${String(key)}`); throw new Error('mutation'); }
});
const pathfinder = { setGoal: forbiddenMethod('pathfinder.setGoal'), goto: forbiddenMethod('pathfinder.goto') };
const bot = new Proxy({
  entity,
  blockAt(pos, extra) { reads.push([pos.x, pos.y, pos.z, extra]); return { type: 0, name: 'air' }; },
  pathfinder,
  lookAt: forbiddenMethod('lookAt'), look: forbiddenMethod('look'), turn: forbiddenMethod('turn'),
  chat: forbiddenMethod('chat'), whisper: forbiddenMethod('whisper'),
  inventory: forbiddenMethod('inventory'), equip: forbiddenMethod('equip'),
  placeBlock: forbiddenMethod('placeBlock'), dig: forbiddenMethod('dig'),
  attack: forbiddenMethod('attack'), useOn: forbiddenMethod('use'),
  tool: forbiddenMethod('tool'), planner: forbiddenMethod('planner'),
  model: forbiddenMethod('model'), task: forbiddenMethod('task'),
  emit: forbiddenMethod('event emission'), setControlState: forbiddenMethod('movement')
}, {
  get(target, key, receiver) {
    if (key in target) return Reflect.get(target, key, receiver);
    forbidden.push(`bot.${String(key)}`);
    throw new Error('unexpected bot API access');
  },
  set(_target, key) { mutations.push(`bot.${String(key)}`); throw new Error('mutation'); }
});
const capture = helper.createVisibleBlockCapture({ Vec3, mcData });
const first = JSON.parse(capture(bot));
const second = JSON.parse(capture(bot));
console.log(JSON.stringify({ first, second, reads, forbidden, mutations }));
""")
    expected = [
        {"x": x, "y": y, "z": z}
        for x in range(-2, 3) for y in range(-1, 2) for z in range(-2, 3)
    ]
    first = result["first"]
    assert len(first["cells"]) == 75
    assert [cell["offset"] for cell in first["cells"]] == expected
    assert all(cell["position"] == {
        "x": int(0.2 // 1) + cell["offset"]["x"],
        "y": int(10.1 // 1) + cell["offset"]["y"],
        "z": int(-0.2 // 1) + cell["offset"]["z"],
    } for cell in first["cells"])
    assert first["complete"] is True and first["truncated"] is False and first["error"] is None
    assert first["pose"] == {"x": "0.2", "y": "10.1", "z": "-0.2"}
    assert first["eye"]["eye_height"] == "1.62"
    assert first["capture_seq"] == 1 and result["second"]["capture_seq"] == 2
    assert result["reads"] and all(call[3] is False for call in result["reads"])
    assert result["forbidden"] == []
    assert result["mutations"] == []


def test_reentrant_capture_fails_closed_without_reads_and_releases_guard():
    result = _node_capture(r"""
let capture;
let nested;
let attempted = false;
const reads = [];
const bot = {
  entity: { position: { x: 0.5, y: 2.1, z: 0.5 }, eyeHeight: 0.5 },
  blockAt(pos, extra) {
    reads.push([pos.x, pos.y, pos.z, extra]);
    if (!attempted) {
      attempted = true;
      nested = JSON.parse(capture(bot));
    }
    return { type: 0, name: 'air' };
  }
};
capture = helper.createVisibleBlockCapture({ Vec3, mcData });
const outer = JSON.parse(capture(bot));
const readsAfterOuter = reads.length;
const afterReentry = JSON.parse(capture(bot));
console.log(JSON.stringify({ outer, nested, afterReentry, readsAfterOuter, reads }));
""")
    assert result["nested"]["capture_seq"] == 2
    assert result["nested"]["complete"] is False
    assert result["nested"]["error"] == "incoherent_capture"
    assert len(result["nested"]["cells"]) == 75
    assert all(cell["state"] == "unknown" and cell["position"] is None
               and cell["unknown_reason"] == "incoherent_capture"
               for cell in result["nested"]["cells"])
    assert result["outer"]["capture_seq"] == 1 and result["outer"]["complete"] is True
    assert result["afterReentry"]["capture_seq"] == 3
    assert result["afterReentry"]["complete"] is True
    assert result["readsAfterOuter"] > 0
    assert len(result["reads"]) > result["readsAfterOuter"]
    assert all(call[3] is False for call in result["reads"])


def test_target_presence_positive_air_negative_and_different_nonair_remains_positive():
    result = _node_capture(r"""
mcData.blocksByName.stone = { id: 3, name: 'stone' };
mcData.blocksByName.dirt = { id: 4, name: 'dirt' };
let name = 'stone';
const bot = {
  entity: { position: { x: 0.5, y: 0, z: 0.5 }, eyeHeight: 1.5 },
  blockAt(p, extra) {
    if (extra !== false) throw new Error('chunk loading forbidden');
    return p.x === 0 && p.y === 0 && p.z === 0
      ? { type: mcData.blocksByName[name].id, name }
      : { type: 0, name: 'air' };
  }
};
const capture = helper.createVisibleBlockCapture({ Vec3, mcData });
const cells = [];
for (const value of ['stone', 'dirt', 'air']) {
  name = value;
  cells.push(JSON.parse(capture(bot)).cells.find(c => c.offset.x === 0 && c.offset.y === 0 && c.offset.z === 0));
}
console.log(JSON.stringify(cells));
""")
    assert [cell["state"] for cell in result] == ["known_non_air", "known_non_air", "known_air"]
    assert [cell["block_name"] for cell in result] == ["stone", "dirt", "air"]
    assert all(cell["coverage"]["target_loaded"]["position"] == {"x": 0, "y": 0, "z": 0}
               for cell in result)


def test_complete_unknown_matrix_never_exposes_hidden_registry_identity():
    result = _node_capture(r"""
function captureCase(kind) {
  const entity = { position: { x: 0.5, y: 0, z: 0.5 }, eyeHeight: kind === 'step_limit' ? 100 : 1.5 };
  if (kind === 'invalid_pose') entity.position.x = NaN;
  if (kind === 'invalid_eye') delete entity.eyeHeight;
  const bot = { entity, blockAt(p, extra) {
    if (extra !== false) throw new Error('chunk loading forbidden');
    if (kind === 'incoherent_capture') entity.position.x += 0.01;
    const target = p.x === 2 && p.y === 1 && p.z === 2;
    const path = p.x === 1 && p.y === 1 && p.z === 0;
    const eye = p.x === 0 && p.y === 1 && p.z === 0;
    if (kind === 'unloaded_target' && target) return null;
    if (kind === 'unloaded_path' && path) return null;
    if (kind === 'occluded' && path) return { type: 20, name: 'glass' };
    if (kind === 'unmapped_registry' && target) return { type: 999, name: 'air' };
    if (kind === 'unmapped_eye' && eye) return { type: 999, name: 'air' };
    return { type: 0, name: 'air' };
  }};
  return JSON.parse(helper.createVisibleBlockCapture({ Vec3, mcData })(bot));
}
const cases = {};
for (const kind of ['invalid_pose', 'invalid_eye', 'step_limit', 'incoherent_capture',
                    'unloaded_target', 'unloaded_path', 'occluded', 'unmapped_registry', 'unmapped_eye']) {
  cases[kind] = captureCase(kind);
}
console.log(JSON.stringify(cases));
""")
    for case in result.values():
        assert len(case["cells"]) == 75
        for cell in case["cells"]:
            if cell["state"] == "unknown":
                assert set(cell) == {"offset", "position", "state", "unknown_reason"}
    for kind, reason in (("invalid_pose", "invalid_pose"), ("invalid_eye", "invalid_pose"),
                         ("step_limit", "step_limit"), ("incoherent_capture", "incoherent_capture"),
                         ("unmapped_eye", "unmapped_registry")):
        assert {cell["state"] for cell in result[kind]["cells"]} == {"unknown"}
        assert {cell["unknown_reason"] for cell in result[kind]["cells"]} == {reason}
    for kind in ("unloaded_target", "unloaded_path", "occluded", "unmapped_registry"):
        target = next(cell for cell in result[kind]["cells"]
                      if cell["offset"] == {"x": 2, "y": 1, "z": 2})
        assert target["state"] == "unknown" and target["unknown_reason"] == kind
    eye_target = next(cell for cell in result["occluded"]["cells"]
                      if cell["offset"] == {"x": 0, "y": 1, "z": 0})
    assert eye_target["state"] == "unknown"
