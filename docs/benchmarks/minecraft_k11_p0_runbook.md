# K11 P0 Instrumentation Validation Runbook

Status: development/pilot procedure only. P0 is not a prevalence cohort and must never be pooled with final K11 evidence.

## 1. Required checkout

Use the dedicated branch/worktree:

```bash
git fetch origin
git worktree add ../VillagerAgent-k11-k12 experiment/k11-k12-ecological-validation
cd ../VillagerAgent-k11-k12
```

Before P0, the checkout must be clean:

```bash
git status --short
```

Expected output: empty.

The P0 runner independently checks cleanliness and fails closed otherwise.

## 2. Unit/integration contract tests

Run the K11-specific tests before any natural pilot run:

```bash
python -m pytest -q \
  tests/test_minecraft_k11_trace.py \
  tests/test_minecraft_k11_analysis.py \
  tests/test_minecraft_k11_instrumentation.py \
  tests/test_minecraft_k11_calibration.py \
  tests/test_minecraft_k11_pilot.py
```

Do not start P0 if these tests fail.

These tests are development fixtures, not K11 natural observations. The controlled invalidation used by `test_minecraft_k11_analysis.py` exists only to verify the offline classifier and must never enter a P0/P1/final prevalence denominator.

The controlled replacement fixture additionally proves that a complete positive abandonment/replacement lineage can produce offline N1. It does not assert that P0 contains a natural N1 event. Ambiguous disappearance remains `disposition_unresolved`.

### Pre-freeze runtime-hygiene disclosure

The K11 development checkout includes a general runtime-hygiene change that relocates OpenAI-compatible token, log, inference, and cache state into the run-specific `RuntimePaths` closure. This is not K11-only instrumentation and the final study must not describe the subject runtime as byte-for-byte unchanged from the pre-K11 audited base. Legacy default paths and cache lookup results are preserved. One legacy edge behavior is intentionally normalized: when the cache directory already existed but the cache file did not, the old first `save_cache()` call created an empty file and discarded the response; the retained implementation writes that first response. The P0 manifest records this disclosure, and the generated execution revision/runtime digest bind the retained change before any final freeze.

## 3. Endpoint preflight

The checked-in P0 manifest reuses infrastructure identities already disclosed by existing repository configs:

- model: `gemma4:12b`
- Ollama endpoint: `http://10.255.255.5:11434`
- controller-managed Ollama reasoning control: `reasoning_effort=none`
- Minecraft target: `10.12.3.1:40000`

These are configuration provenance, not an assertion that the endpoints are currently available.

Before P0, verify reachability from the runtime host using the ordinary operator tools available there. For example:

```bash
curl -fsS http://10.255.255.5:11434/api/tags >/dev/null
nc -vz 10.12.3.1 40000
```

Endpoint failure is infrastructure failure. Do not silently replace the endpoint, model, Minecraft target, or task prompts after inspecting P0 outcomes. If infrastructure must change, update and commit the P0 manifest before restarting the pilot from a fresh output directory.

The direct/internal endpoint above replaces the named reverse-proxy route only after the failed `2c72da1` cohort was closed and retained. Bounded development qualification observed materially lower direct-route latency and no HTTP failures on either route during the small qualification, while the failed formal cohort had sustained proxy 504 responses. This is an infrastructure-only endpoint substitution, not evidence about K11 prevalence, and requires a new execution revision/cohort.

The failed `061307a` development smoke then produced reasoning-only output until its 1024-token limit. A separate development-only provider qualification against Ollama `0.32.0` established that both a larger token budget and the documented `reasoning_effort=none` setting can satisfy the unchanged JSON/tag contract; the latter succeeds at the existing budget. The manifest binds that setting explicitly for controller-managed `OpenAILanguageModel` calls such as `TaskManager.init_task`. The distinct Minecraft-agent LangChain client retains its existing reasoning-aware structured-action adapter. This qualification is not P0 evidence and is not pooled with any smoke or formal cohort.

## 4. P0 manifest

The prospective validation-contract manifest is:

```text
configs/minecraft/k11-p0-natural-manifest-v2.json
```

`configs/minecraft/k11-p0-natural-manifest-v0.json` and v1 remain unchanged as provenance. The legacy v1 validation path retains contract `/1` and trace `/2`; the new prospective path requires manifest artifact version 3, `minecraft-k11-p0-validation-contract/2`, and trace schema `minecraft-k11-trace/3`. Earlier artifacts are never silently reinterpreted under the prospective semantics.

Every v2 per-run, development-smoke, and aggregate validation artifact records the canonical manifest digest and an explicit `cohort_mode` (`development_smoke` or `formal_p0`). The parent process rejects a worker validation artifact whose contract, manifest digest, or cohort mode differs from its invocation, so a detached smoke result cannot be accepted as a formal-cohort run artifact.

It contains exactly eight Advisory, `task_type=none`, non-judged, non-production runs, copied unchanged from v1. Admission metadata is explicit and fail-closed: same-domain execution is required, world reset is forbidden, and missing/invalid metadata blocks admission. It forbids supplied stale EAC premanifest/revision values. Its top-level `observation_window` prospectively binds a 600-second fixed monotonic horizon for this development pilot. That value is not the final K11 horizon and does not freeze the draft protocol.

A run may be structurally valid with zero qualifying in-window evidence. Such a zero-evidence run is retained and is not retried or replaced for that reason. The prospective measurement snapshot, structural validation, censoring, and analysis cuts are recorded separately. Contamination is excluded rather than repaired. An active tool/native effect at H, any post-close tool/native entry, uncertainty, or uncertain/failed cleanup blocks the next row. A post-close completion of an effect already censored at H remains cleanup metadata and does not rewrite measurement. The formal loop stops before that next row, with no retry or skip; counts and status counts report the completed prefix. Development smoke and other prior development artifacts are not eligible for retroactive promotion into the formal eight-run cohort.

The controller shutdown verdict remains the immutable verdict at its bounded grace deadline. A failed deadline verdict may be followed by a separate append-only `qualified_late` cleanup result only when identity-bound evidence affirmatively establishes natural worker exit, absent process group and descendants, terminal provider/tool/native/movement state, complete bridge cleanup, and complete non-truncated evidence. Direct Future reconciliation remains reported independently and may be `unknown` when process containment already proves that execution capability ended. Missing, truncated, collection-error, parent-forced-termination, or surviving-process evidence remains `unknown` or `not_qualified`; it never rewrites the controller verdict or measurement cut.

At pilot start the runner:

1. requires a clean Git checkout;
2. resolves the current `RuntimeExecution` source closure;
3. resolves exact `HEAD`;
4. deterministically derives the EAC runtime identity;
5. writes a run-local `K11_P0_EAC_PREMANIFEST.json`;
6. passes that exact revision and premanifest to the existing EAC admission path.

The K11-only wrapper opens the window when `GlobalController.run` starts. Natural runtime terminal behavior may close it early. Otherwise the wrapper records the exact fixed close boundary before requesting ordinary controller shutdown, then the worker writes trace, analysis, validation, and shutdown artifacts. The 900-second supervisor deadline remains a failure backstop; it is not used as the observation-window close. Manifest validation reserves 120 seconds for pre-controller startup plus the existing 20 seconds of completion/termination/kill grace, so a configured horizon above 760 seconds is rejected rather than allowed to collide with that backstop.

This prevents the historical checked-in Issue #510 premanifest from being incorrectly reused for K11 code.

## 5. Output location

P0 output must be outside the repository. Recommended sibling path:

```text
../VillagerAgent-k11-p0-results
```

The runner rejects an output root inside the source repository.

Do not reuse a non-empty run directory.

## 6. Run one development smoke

After committing the remediation and establishing a new clean execution revision, run exactly one development smoke before any replacement formal cohort:

```bash
python -m benchmarks.minecraft.k11_pilot \
  --manifest configs/minecraft/k11-p0-natural-manifest-v2.json \
  --output-root ../VillagerAgent-k11-p0-dev-smoke-01 \
  --development-smoke-run-id K11-P0-01
```

The smoke is explicitly non-formal and writes `DEV_SMOKE_VALIDATION.json`. It passes only after model, guarded-tool, primary-preparation, terminal-disposition, valid replay/analysis, clean process exit, and at least one qualifying in-window evidence ingestion are present; it does not satisfy the formal P0 cohort gate. A structurally valid zero-evidence smoke reports structural validation separately but retains `smoke_passed=false`. A preparation cut by the fixed boundary is retained as right-censored D1 and cannot enter D2-D6 or N0-N4. Stop after this one smoke; do not start the formal eight-run cohort without a separate decision.

### Development basis for the 600-second pilot binding

The retained failed smoke at `/tmp/opencode/VillagerAgent-k11-p0-dev-smoke-18e72b3` reached the external 900-second process timeout and did not flush its in-memory K11 trace. Its baseline runtime journal nevertheless contains 910 events spanning 869.072 seconds from first to last emitted event. Activity continued throughout: 21 task assignments occurred, with 15 observed by 600 seconds and 19 by 750 seconds; 12 task-graph snapshots occurred, with 9 observed by 600 seconds and 11 by 750 seconds. Candidate ranking contained 816 cycles (12 non-empty and 804 empty). Nineteen assignments had failed and two were still running at timeout.

For a bounded replacement development smoke, 600 seconds captures substantial repeated assignment/replanning activity while reserving roughly 300 seconds under the independent supervisor deadline for startup, controlled controller shutdown, trace serialization, analysis, and process cleanup. This is a pragmatic instrumentation-recovery choice from a failed development artifact, not a prevalence optimization and not evidence that 600 seconds is adequate for final K11 estimation. Do not adjust the horizon after inspecting within-run EAC or taxonomy outcomes.

## 7. Run P0

```bash
python -m benchmarks.minecraft.k11_pilot \
  --manifest configs/minecraft/k11-p0-natural-manifest-v2.json \
  --output-root ../VillagerAgent-k11-p0-results \
  --formal-p0
```

Formal P0 must be selected explicitly; omitting both `--formal-p0` and the development-smoke mode is rejected. The command exits `0` only when `p0_passed=true`. Otherwise it exits `2` for a completed-but-invalid P0 validation result or raises on a manifest/identity precondition failure.

## 8. Expected artifacts

At the output root:

```text
K11_P0_EAC_PREMANIFEST.json
P0_CALIBRATION.json
P0_VALIDATION.json
K11-P0-01/
...
K11-P0-08/
```

Each run directory contains at minimum:

```text
k11_trace.json
k11_analysis.json
p0_validation.json
process_supervision.json
runtime_result.json        # when runtime collection reaches that path
runtime_events.jsonl       # existing baseline runtime journal
runtime/                   # isolated runtime artifacts
```

On runtime exception, `exception.txt` is preserved as pilot evidence.

## 9. P0 pass conditions

`P0_VALIDATION.json` must report all of the following:

```text
p0_passed = true
run_count = 8
runtime_error_count = 0
trace_valid_count = 8
offline_analysis_valid_count = 8
coverage_sufficient = true
calibration_error = null
```

Coverage additionally requires observation of:

- model-call start events, including the direct `OpenAILanguageModel` path used by the configured Ollama provider;
- guarded tool-call entry events;
- EAC prepared-action events;
- actor-visible evidence-ingestion events;
- more than one actor/thread pair across the pilot.

For the formal cohort gate, the evidence-ingestion coverage above is counted only when the qualifying event is inside its run's observation window. `p0_passed` is false for zero qualifying in-window events across the fixed eight runs or for evidence that is pre-window-only, even when every run is structurally valid.

Observed model-call events prove only that the listed paths were exercised. Complete direct-call lifecycle coverage is established by the K11 instrumentation contract tests, not inferred from a nonzero aggregate event count.

The traced in-process calibration must also have a valid trace.

## 9. P0 does not answer prevalence

Even if the P0 diagnostic outputs contain D1-D6 or N0-N4 rows, they are development observations only.

Forbidden:

- reporting P0 N2 as a K11 prevalence result;
- pooling P0 actions with P1 or final K11;
- changing the final task pool because a P0 task happened to produce N2;
- treating zero P0 N2 as evidence that natural N2 cannot occur.

P0 answers only whether the instrumentation, reconstruction, correlation, ordering, and overhead measurement are usable.

## 10. After execution

Do not proceed directly to final K11.

After P0, inspect:

- `P0_VALIDATION.json`;
- all trace validation warnings/errors;
- offline replay errors;
- prepare-to-decision timing diagnostics;
- prepared-inside-window, complete-disposition-inside-window, censored count, and censoring fraction;
- `P0_CALIBRATION.json` incremental overhead;
- whether multiple actor/thread execution was actually observed;
- whether the existing 256-entry EAC audit truncates while the K11 trace remains complete.

Only after that review may K11-D be marked passed and the workflow advance to the reconnaissance/freeze checkpoints specified by the protocol.
