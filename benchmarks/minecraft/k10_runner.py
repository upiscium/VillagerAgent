"""Durable, one-shot K10 census runner.

The fixture is deliberately imported only by :func:`run`; all testable work is
performed through the dependency-injected entry point below.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from benchmarks.common.eac.canonical import canonical_bytes
from benchmarks.minecraft import k10_protocol

HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
CONTRACT_PATH = HERE / "k10_runner_v1.json"
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
FINALIZATION_STAGES = {"after_staging_mkdir", "after_aggregate_staged", "after_final_manifest_staged", "before_atomic_publication", "after_atomic_publication", "after_parent_fsync"}
EXPOSURE_MARKER = ".issue511-k10-effect-boundary-started.json"

class K10RunnerError(RuntimeError):
    pass


class K10FinalizationDurabilityError(K10RunnerError):
    pass


def _load_json(path):
    path = Path(path)
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise K10RunnerError(f"duplicate JSON key in {path}: {key}")
            value[key] = item
        return value
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise K10RunnerError(f"cannot load K10 runner JSON: {path}") from exc
    if not isinstance(value, dict):
        raise K10RunnerError(f"K10 runner JSON must be an object: {path}")
    return value

def _digest_without_field(value):
    value = dict(value); value.pop("detached_artifact_sha256", None)
    return hashlib.sha256(canonical_bytes(value)).hexdigest()

def load_k10_contract(path=CONTRACT_PATH):
    value = _load_json(path)
    required = {"artifact_id","artifact_version","detached_artifact_sha256","runner_id","runner_version","implementation","implementation_sha256","protocol_binding","census","canonical_order_source","retry","resume","completeness_requirement","failure_policy","persistence_layout_version","finalization"}
    if not isinstance(value, dict) or set(value) != required: raise K10RunnerError("K10 runner contract schema mismatch")
    if (value["artifact_id"], value["artifact_version"], value["runner_id"], value["runner_version"]) != ("minecraft-k10-atomic-runner", 1, "minecraft-k10-atomic-census-runner", 1): raise K10RunnerError("K10 runner identity mismatch")
    if value["implementation"] != "benchmarks/minecraft/k10_runner.py": raise K10RunnerError("K10 implementation binding mismatch")
    if not re.fullmatch(r"[0-9a-f]{64}", value["implementation_sha256"]): raise K10RunnerError("K10 implementation digest is malformed")
    if value["detached_artifact_sha256"] != _digest_without_field(value): raise K10RunnerError("K10 runner contract detached digest mismatch")
    if value["census"] != {"primary":80,"control":40,"total":120}: raise K10RunnerError("K10 census mismatch")
    if value["canonical_order_source"] != "benchmarks.minecraft.k10_protocol.build_k10_cells": raise K10RunnerError("K10 canonical order mismatch")
    if value["retry"] is not False or value["resume"] is not False: raise K10RunnerError("K10 contract permits retry or resume")
    if value["completeness_requirement"] != {"exact_cells":120,"primary_cells":80,"control_cells":40,"pairs":60,"aggregate_complete":True}: raise K10RunnerError("K10 completeness mismatch")
    if value["failure_policy"] != {"zero_pilot":True,"post_submission_action":"abort_without_authoritative_aggregate","cell_retry":False,"resume":False,"global_exposure_marker":EXPOSURE_MARKER}: raise K10RunnerError("K10 failure policy mismatch")
    return value, _digest_without_field(value)

def runner_contract_digest(path=CONTRACT_PATH): return load_k10_contract(path)[1]

def _git(root, *args):
    result = subprocess.run(["git","-C",str(root),*args], capture_output=True, text=True, check=False, env={**os.environ,"GIT_OPTIONAL_LOCKS":"0","GIT_TERMINAL_PROMPT":"0"})
    if result.returncode: raise K10RunnerError("K10 git command failed")
    return result.stdout
def _repository_top(root):
    top = Path(_git(root,"rev-parse","--show-toplevel").strip()).resolve()
    if top != root.resolve(): raise K10RunnerError("K10 repo-root must be the Git top-level directory")
    return top
def _worktrees(root):
    return tuple(Path(x[9:]).resolve() for x in _git(root,"worktree","list","--porcelain").splitlines() if x.startswith("worktree ")) or (root.resolve(),)
def _validate_output_dir(path, worktrees):
    path=Path(path)
    if not path.is_absolute() or not path.is_dir() or path.is_symlink(): raise K10RunnerError("K10 output-dir must be an existing absolute non-symlink directory")
    if any(path.resolve()==w or w in path.resolve().parents for w in worktrees): raise K10RunnerError("K10 output-dir must be outside every Git worktree")
    st=path.resolve().stat()
    if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode)&0o022: raise K10RunnerError("K10 output-dir ownership or mode is unsafe")
    return path.resolve()
def _verify_repository(root, expected):
    if not COMMIT_RE.fullmatch(expected) or _git(root,"rev-parse","HEAD").strip()!=expected: raise K10RunnerError("K10 execution revision does not match expected execution revision")
    if _git(root,"status","--porcelain","--untracked-files=all").strip(): raise K10RunnerError("K10 Git tree is not clean")
    return expected

def _preflight_with_protocol(repo_root, *, expected_execution_revision, output_dir, protocol_module):
    root=Path(repo_root).resolve() if repo_root is not None else REPOSITORY_ROOT
    if root != REPOSITORY_ROOT.resolve(): raise K10RunnerError("K10 repo-root must be the checkout containing the census runner")
    root=_repository_top(root)
    contract,digest=load_k10_contract(); protocol=protocol_module.load_k10_protocol()
    live_validator=getattr(protocol_module,"validate_live_k10_checkout",None)
    if not callable(live_validator): raise K10RunnerError("K10 live checkout validator is unavailable")
    live_validator(protocol,root=root)
    cells=tuple(protocol_module.build_k10_cells())
    if len(cells)!=120 or sum(c.matrix=="primary" for c in cells)!=80 or sum(c.matrix=="control" for c in cells)!=40: raise K10RunnerError("K10 census counts mismatch")
    if output_dir is not None: _validate_output_dir(output_dir,_worktrees(root))
    bindings={"protocol_digest":protocol["validated_protocol_digest"],"candidate_pool_digest":protocol["validated_candidate_pool_digest"],"inventory_digest":protocol["validated_inventory_digest"],"result_schema_digest":protocol["validated_result_schema_digest"],"selection_manifest_digest":getattr(protocol_module,"SELECTION_MANIFEST_DIGEST",k10_protocol.SELECTION_MANIFEST_DIGEST),"historical_audit_digest":protocol["validated_historical_audit_digest"]}
    if contract["protocol_binding"] != bindings: raise K10RunnerError("K10 runner contract artifact binding mismatch")
    revision=_verify_repository(root,expected_execution_revision)
    implementation=REPOSITORY_ROOT/contract["implementation"]
    if implementation.resolve()!= (HERE/"k10_runner.py").resolve(): raise K10RunnerError("K10 implementation path mismatch")
    implementation_digest=hashlib.sha256(implementation.read_bytes()).hexdigest()
    if implementation_digest != contract["implementation_sha256"]: raise K10RunnerError("K10 runner implementation digest mismatch")
    return {"run_id":None,"execution_revision":revision,"runner_id":contract["runner_id"],"runner_version":contract["runner_version"],"runner_contract_digest":digest,"implementation_sha256":implementation_digest,"cell_ids":[c.cell_id for c in cells],**bindings,"primary_cells":80,"control_cells":40,"total_cells":120,"repository_clean":True}
def preflight(repo_root=None, *, expected_execution_revision, output_dir=None):
    return _preflight_with_protocol(repo_root,expected_execution_revision=expected_execution_revision,output_dir=output_dir,protocol_module=k10_protocol)

def _fsync_directory(path):
    fd=os.open(str(path),os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)
def _durable_json(path,value,replace_existing=False):
    path=Path(path); tmp=path.with_name("."+path.name+".tmp")
    if not path.parent.is_dir(): raise K10RunnerError("K10 durable target parent is missing")
    if path.exists() and not replace_existing: raise K10RunnerError("K10 durable target already exists")
    try:
        with tmp.open("x",encoding="utf-8") as f:
            json.dump(value,f,indent=2,sort_keys=True); f.write("\n"); f.flush(); os.fsync(f.fileno())
        if replace_existing:
            os.replace(tmp,path)
        else:
            try: os.link(tmp,path)
            except FileExistsError as e: raise K10RunnerError("K10 durable target already exists") from e
            tmp.unlink()
        _fsync_directory(path.parent)
    finally:
        if tmp.exists(): tmp.unlink()
def _rename_directory_no_replace(source,destination):
    try: fn=ctypes.CDLL(None,use_errno=True).renameat2
    except AttributeError as e: raise K10RunnerError("K10 atomic no-replace publication unavailable") from e
    fn.argtypes=(ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint); fn.restype=ctypes.c_int
    if fn(-100,os.fsencode(source),-100,os.fsencode(destination),1):
        e=ctypes.get_errno()
        if e in (errno.EEXIST,errno.ENOTEMPTY): raise K10RunnerError("K10 final directory already exists")
        raise OSError(e,os.strerror(e),str(destination))
def _hook(hook,stage):
    if hook:
        if stage not in FINALIZATION_STAGES: raise K10RunnerError("unknown K10 finalization stage")
        hook(stage)

def _authoritative_final_valid(final,run_id,checks):
    m=Path(final)/"final_manifest.json"; a=Path(final)/"aggregate.json"
    if final.is_symlink() or not final.is_dir() or m.is_symlink() or a.is_symlink() or not m.is_file() or not a.is_file(): return False
    try: manifest=_load_json(m); raw=a.read_bytes(); aggregate=_load_json(a)
    except (OSError,UnicodeError,json.JSONDecodeError,K10RunnerError): return False
    if not isinstance(aggregate,dict): return False
    return (manifest.get("schema_version")=="minecraft-k10-final-manifest/1"
            and manifest.get("completed") is True and manifest.get("run_id")==run_id
            and manifest.get("execution_revision")==checks["execution_revision"]
            and manifest.get("runner")=={"identity":checks["runner_id"],"version":checks["runner_version"],"contract_digest":checks["runner_contract_digest"],"implementation_sha256":checks["implementation_sha256"]}
            and all(manifest.get(name)==checks[name] for name in ("protocol_digest","candidate_pool_digest","inventory_digest","result_schema_digest","selection_manifest_digest","historical_audit_digest"))
            and manifest.get("canonical_cell_ids")==checks["cell_ids"]
            and manifest.get("cell_statuses")==["completed"]*120
            and manifest.get("counts")=={"total_cells":120,"primary_cells":80,"control_cells":40}
            and manifest.get("pair_count")==60 and manifest.get("aggregate_path")=="aggregate.json"
            and manifest.get("aggregate_sha256")==hashlib.sha256(raw).hexdigest()
            and aggregate.get("schema_version")=="minecraft-k10-run-aggregate/1"
            and aggregate.get("run_id")==run_id
            and aggregate.get("execution_revision")==checks["execution_revision"]
            and all(aggregate.get(name)==checks[name] for name in ("protocol_digest","candidate_pool_digest","inventory_digest","result_schema_digest","selection_manifest_digest","historical_audit_digest"))
            and aggregate.get("raw_trace_count")==120 and aggregate.get("pair_count")==60
            and aggregate.get("aggregate",{}).get("complete") is True
            and aggregate.get("aggregate",{}).get("observed_primary_cells")==80
            and aggregate.get("aggregate",{}).get("observed_control_cells")==40)

def run(repo_root=None, *, run_id, expected_execution_revision, output_dir):
    checks=_preflight_with_protocol(repo_root,expected_execution_revision=expected_execution_revision,output_dir=output_dir,protocol_module=k10_protocol)
    from benchmarks.minecraft import k10_fixture
    return _run_with_dependencies(repo_root,run_id=run_id,expected_execution_revision=expected_execution_revision,output_dir=output_dir,fixture_module=k10_fixture,protocol_module=k10_protocol,preflight_checks=checks)

def _run_with_dependencies(repo_root, *, run_id, expected_execution_revision, output_dir, fixture_module, protocol_module, fault_hook=None, preflight_checks=None):
    if not RUN_ID_RE.fullmatch(run_id) or run_id in {".", ".."}: raise K10RunnerError("K10 run_id is invalid")
    checks=preflight_checks if preflight_checks is not None else _preflight_with_protocol(repo_root,expected_execution_revision=expected_execution_revision,output_dir=output_dir,protocol_module=protocol_module); output=_validate_output_dir(output_dir,_worktrees(Path(repo_root or REPOSITORY_ROOT).resolve())); run_dir=output/run_id
    exposure_marker=output/EXPOSURE_MARKER
    if exposure_marker.exists() or exposure_marker.is_symlink(): raise K10RunnerError("K10 holdout effect boundary was already started in this authorized output root")
    if run_dir.exists() or run_dir.is_symlink(): raise K10RunnerError("K10 run directory must not already exist")
    run_dir.mkdir(mode=0o700); _fsync_directory(output); (run_dir/"cells").mkdir(mode=0o700); _fsync_directory(run_dir)
    cells=tuple(protocol_module.build_k10_cells()); manifest={"schema_version":"minecraft-k10-run/1","run_id":run_id,"execution_revision":checks["execution_revision"],"runner":{"identity":checks["runner_id"],"version":checks["runner_version"],"contract_digest":checks["runner_contract_digest"],"implementation_sha256":checks["implementation_sha256"]},"protocol_digest":checks["protocol_digest"],"candidate_pool_digest":checks["candidate_pool_digest"],"inventory_digest":checks["inventory_digest"],"result_schema_digest":checks["result_schema_digest"],"selection_manifest_digest":checks["selection_manifest_digest"],"historical_audit_digest":checks["historical_audit_digest"],"canonical_order_source":"benchmarks.minecraft.k10_protocol.build_k10_cells","planned_cell_ids":[c.cell_id for c in cells],"started":False,"completed":False,"aggregate_generated":False,"aggregate_path":None,"run_status":"not_started","failure":None,"cells":[{"ordinal":i,"cell_id":c.cell_id,"path":f"cells/{i:04d}_{c.cell_id}.json","status":"not_started"} for i,c in enumerate(cells,1)]}
    mp=run_dir/"run_manifest.json"; _durable_json(mp,manifest); staging=run_dir/".final.tmp"; final=run_dir/"final"; published=False
    try:
        for i,cell in enumerate(cells):
            e=manifest["cells"][i]; e["status"]="started"; manifest["started"]=True; manifest["run_status"]="started"; _durable_json(mp,manifest,True)
            try:
                trial=fixture_module.construct_k10_trial(cell)
                if i == 0:
                    _durable_json(exposure_marker,{"schema_version":"minecraft-k10-exposure-marker/1","run_id":run_id,"execution_revision":checks["execution_revision"],"first_cell_id":cell.cell_id,"effect_boundary_submissions_before_marker":0})
                trace=trial.submit()
                trace=protocol_module.validate_k10_trace(trace,cell=cell)
                _durable_json(run_dir/e["path"],trace)
                e["status"]="completed"
                _durable_json(mp,manifest,True)
            except Exception as exc:
                e["status"]="failed"; e["error"]=f"{type(exc).__name__}: {exc}"
                try: _durable_json(mp,manifest,True)
                except Exception as persistence_exc:
                    raise K10RunnerError(f"K10 cell {cell.cell_id} failed and failed status persistence also failed: {persistence_exc}") from exc
                raise K10RunnerError(f"K10 cell {cell.cell_id} failed: {exc}") from exc
        paths=list((run_dir/"cells").glob("*.json"));
        if len(paths)!=120 or {p for p in paths}!={run_dir/e["path"] for e in manifest["cells"]}: raise K10RunnerError("K10 completeness gate failed: exact durable trace set required")
        traces=[]
        seen=set()
        for e,c in zip(manifest["cells"],cells):
            trace=protocol_module.validate_k10_trace(_load_json(run_dir/e["path"]),cell=c)
            cell_id=trace["cell"]["cell_id"]
            if cell_id in seen or cell_id != c.cell_id: raise K10RunnerError("K10 completeness gate failed: duplicate or misplaced cell identity")
            seen.add(cell_id); traces.append(trace)
        if len({t["cell"]["cell_id"] for t in traces})!=120 or sum(t["cell"]["matrix"]=="primary" for t in traces)!=80 or sum(t["cell"]["matrix"]=="control" for t in traces)!=40: raise K10RunnerError("K10 exact count gate failed")
        pairs={};
        for t in traces: pairs.setdefault((t["cell"]["scenario_family"],t["cell"]["inventory_id"],t["cell"]["affected_actor"],t["cell"]["matrix"]),[]).append(t)
        if len(pairs)!=60 or any(len(v)!=2 or {x["cell"]["condition"] for x in v}!=set(protocol_module.CONDITIONS) or len({x["pairing_digest"] for x in v})!=1 for v in pairs.values()): raise K10RunnerError("K10 pair gate failed")
        if any(entry["status"] != "completed" for entry in manifest["cells"]): raise K10RunnerError("K10 completeness gate failed: manifest contains non-completed cells")
        aggregate=protocol_module.aggregate_k10_results(traces)
        if (aggregate.get("complete") is not True or aggregate.get("observed_primary_cells") != 80
                or aggregate.get("observed_control_cells") != 40 or aggregate.get("observed_pair_count") != 60):
            raise K10RunnerError("K10 aggregate completeness gate failed")
        protocol=protocol_module.load_k10_protocol(); wrapper={"schema_version":"minecraft-k10-aggregate/1","run_id":run_id,"execution_revision":checks["execution_revision"],"runner_identity":checks["runner_id"],"runner_version":checks["runner_version"],"runner_contract_digest":checks["runner_contract_digest"],**{k:checks[k] for k in ("protocol_digest","candidate_pool_digest","inventory_digest","result_schema_digest","selection_manifest_digest","historical_audit_digest")},"raw_trace_count":120,"pair_count":60,"protocol_pre_run_exposure":protocol.get("zero_pre_exposure"),"aggregate":aggregate}
        wrapper["schema_version"]="minecraft-k10-run-aggregate/1"
        if staging.exists() or staging.is_symlink() or final.exists() or final.is_symlink(): raise K10RunnerError("K10 finalization target already exists")
        staging.mkdir(mode=0o700); _hook(fault_hook,"after_staging_mkdir"); ap=staging/"aggregate.json"; _durable_json(ap,wrapper); _hook(fault_hook,"after_aggregate_staged"); fm={"schema_version":"minecraft-k10-final-manifest/1","completed":True,"run_id":run_id,"execution_revision":checks["execution_revision"],"runner":manifest["runner"],"protocol_digest":checks["protocol_digest"],"candidate_pool_digest":checks["candidate_pool_digest"],"inventory_digest":checks["inventory_digest"],"result_schema_digest":checks["result_schema_digest"],"selection_manifest_digest":checks["selection_manifest_digest"],"historical_audit_digest":checks["historical_audit_digest"],"canonical_cell_ids":checks["cell_ids"],"cell_statuses":["completed"]*120,"counts":{"total_cells":120,"primary_cells":80,"control_cells":40},"pair_count":60,"aggregate_path":"aggregate.json","aggregate_sha256":hashlib.sha256(ap.read_bytes()).hexdigest()}; _durable_json(staging/"final_manifest.json",fm); _hook(fault_hook,"after_final_manifest_staged"); _fsync_directory(staging); _hook(fault_hook,"before_atomic_publication")
        if final.exists() or final.is_symlink(): raise K10RunnerError("K10 final directory appeared before publication")
        _rename_directory_no_replace(staging,final); published=True; _hook(fault_hook,"after_atomic_publication")
        try: _fsync_directory(run_dir)
        except Exception as durability_failure:
            raise K10FinalizationDurabilityError(f"K10 final directory is published but parent fsync failed: {durability_failure}") from durability_failure
        _hook(fault_hook,"after_parent_fsync"); manifest.update(completed=True,aggregate_generated=True,aggregate_path="final/aggregate.json",run_status="completed")
        try: _durable_json(mp,manifest,True)
        except Exception as refresh_failure: print(f"K10 warning: postcommit progress refresh failed: {refresh_failure}",file=sys.stderr)
        return wrapper
    except BaseException as exc:
        if published or _authoritative_final_valid(final,run_id,checks): raise
        manifest.update(completed=False,aggregate_generated=False,aggregate_path=None,run_status="failed",failure={"type":type(exc).__name__,"message":str(exc)})
        try: _durable_json(mp,manifest,True)
        except Exception as persistence_failure:
            raise K10RunnerError(f"K10 run aborted and durable failure-state persistence also failed; on-disk status is indeterminate: {persistence_failure}") from exc
        raise

def main(argv=None):
    p=argparse.ArgumentParser(prog="k10_runner"); s=p.add_subparsers(dest="command",required=True); q=s.add_parser("preflight"); q.add_argument("--expected-execution-revision",required=True); r=s.add_parser("run"); r.add_argument("--run-id",required=True); r.add_argument("--expected-execution-revision",required=True); r.add_argument("--output-dir",required=True); a=p.parse_args(argv)
    result=preflight(expected_execution_revision=a.expected_execution_revision) if a.command=="preflight" else run(run_id=a.run_id,expected_execution_revision=a.expected_execution_revision,output_dir=a.output_dir); print(json.dumps(result,indent=2,sort_keys=True)); return 0
if __name__=="__main__": sys.exit(main())
