# CRAFT Integration

CRAFT is a partial-information multi-agent benchmark where three Directors each see an incomplete private 2D view of a hidden 3D target structure. The Directors coordinate through public natural language messages, and a Builder constructs the final structure.

This integration evaluates VillagerAgent on CRAFT by adapting VillagerAgent's director-side coordination components to CRAFT's Director protocol. It is not intended to wrap CRAFT as a standalone executable.

## Why Submodule

CRAFT is kept under `external/CRAFT` as a git submodule because it is the benchmark environment, while VillagerAgent is the evaluated system. Benchmark logic, datasets, and metrics should remain external and unchanged.

## Setup

```bash
git submodule update --init --recursive
```

For OpenAI-compatible Director models such as Ollama, configure `configs/craft/*.yaml`. For OpenAI Builder models, set `OPENAI_API_KEY` before non-dry runs.

## Official Baseline

```bash
python -m benchmarks.craft.run --config configs/craft/official_baseline.yaml --dry-run
```

The official baseline condition is tracked for comparability. CRAFT environment and metric logic remain in `external/CRAFT`.

## VillagerAgent Directors

```bash
python -m benchmarks.craft.run --config configs/craft/villageragent_qwen.yaml --dry-run
```

For an Ollama-only qwen smoke/non-dry run that does not require `OPENAI_API_KEY`, use:

```bash
python -m benchmarks.craft.run \
  --config configs/craft/villageragent_qwen_ollama.yaml \
  --structure 0 \
  --turns 1
```

This condition maps CRAFT Directors to VillagerAgent-side Director adapters:

- `D1` -> VillagerAgent BaseAgent-style Director D1
- `D2` -> VillagerAgent BaseAgent-style Director D2
- `D3` -> VillagerAgent BaseAgent-style Director D3

The current runtime is `villageragent_director_runtime_v1`. It provides a CRAFT-specific VillagerAgent adapter with explicit private/public state separation, Controller-style three-director turn production, and metadata indicating which VillagerAgent components are enabled for the run. It is not the Minecraft task pipeline.

When `logging.save_prompts=true`, Director prompts are saved under:

```text
result/craft/{run_name}/raw/prompts/structure_{id}/{director}_turn_{nnn}.json
```

These prompt files contain only the messages sent to the Director LLM. They do not include forbidden leakage guard payloads such as `target_structure`, oracle moves, or other Directors' raw private views.

For qwen3-style OpenAI-compatible endpoints, the CRAFT adapter records response diagnostics in raw turn metadata. If a response returns reasoning but empty public `content`, the adapter retries once with a stricter final-answer instruction and larger token budget. This is intended to make qwen smoke runs diagnosable without exposing hidden CRAFT state.

For a small qwen/Ollama batch evaluation across structures `0, 1, 2` and five turns, use:

```bash
python -m benchmarks.craft.run --config configs/craft/eval_qwen_ollama.yaml
```

To run the qwen/Ollama VillagerAgent condition, the D1-only single-director ablation, the comparable official baseline artifact, and then generate a comparison report in one command, use:

```bash
python -m benchmarks.craft.experiment --config configs/craft/experiments/qwen_batch_v1.yaml
```

Use `--dry-run` to validate the manifest and resolved run outputs without calling model endpoints.

To compare the VillagerAgent Director condition across the configured Ollama models, use:

```bash
python -m benchmarks.craft.experiment --config configs/craft/experiments/ollama_model_comparison_v1.yaml
```

This model comparison manifest uses the same seed, structures, and turn count as the qwen batch evaluation for `qwen3.5:9b`, `qwen3.5:4b`, `qwen3.6:27b`, `gemma4:26b`, and `gemma4:e4b`. It records individual endpoint or model failures as failed run artifacts and still writes the comparison report plus compact summary table.

To evaluate robustness across broader structures and multiple seeds, use:

```bash
python -m benchmarks.craft.experiment --config configs/craft/experiments/qwen_robustness_v1.yaml
```

This robustness manifest runs the qwen baseline, Dual-DAG condition, single-Director Dual-DAG ablation, and comparable official baseline artifact over seeds `1, 3, 5` and structures `0, 1, 2, 3, 4`. It writes comparison, compact summary, and variance summary artifacts under `result/craft/`.

To run the full official CRAFT baseline through `external/CRAFT/run_craft.py`, use:

```bash
python -m benchmarks.craft.run --config configs/craft/official_baseline_full.yaml
```

This invokes the upstream CRAFT CLI with the configured seed, structures, turns, oracle, and no-tools settings, then normalizes the official JSON logs into the same `summary.json`, `turns.jsonl`, and `metrics.csv` format used by other CRAFT runs. The existing `configs/craft/official_baseline.yaml` remains available as a lightweight comparable artifact baseline.

## Single Director Ablation

```bash
python -m benchmarks.craft.run --config configs/craft/single_director_ablation.yaml --dry-run
```

This mode is intended to compare multi-agent coordination against a reduced single-director-style condition under the same seed, structures, and turns.

The qwen/Ollama version uses D1 as the only active Director while preserving the three-Director CRAFT message shape with empty inactive D2/D3 messages:

```bash
python -m benchmarks.craft.run --config configs/craft/single_director_qwen_ollama.yaml
```

## Results

Runs write to `result/craft/{run_name}/`:

- `config.resolved.yaml`
- `command.txt`
- `raw/`
- `normalized/summary.json`
- `normalized/turns.jsonl`
- `normalized/metrics.csv`
- `normalized/leakage_report.json`
- `logs/run.log`

See [`METRICS.md`](METRICS.md) for the meaning of normalized metrics, comparison report columns, Dual-DAG analysis fields, and compact experiment summary columns.

## Compare Runs

Use `benchmarks.craft.report` to summarize one or more normalized CRAFT runs:

```bash
python -m benchmarks.craft.report \
  --runs craft_eval_qwen_ollama craft_single_director_qwen_ollama craft_official_baseline \
  --output result/craft/comparison_summary.csv \
  --json-output result/craft/comparison_summary.json
```

The comparison report includes run condition, number of games, turns, final progress, completion rate, models, providers, active Directors, Builder fallback rate, VillagerAgent component flags, baseline type, run status, failure details, and leakage status.

## Final Qwen Dual-DAG Evaluation

The current paper-facing CRAFT evaluation uses `configs/craft/experiments/qwen_dual_dag_v1.yaml`. It runs four comparable conditions over the configured seed, structures, and turn budget:

- qwen/Ollama 3-Director VillagerAgent Directors
- qwen/Ollama 3-Director VillagerAgent Directors with Dual-DAG gated clarification
- qwen/Ollama D1-only single-Director ablation with Dual-DAG enabled
- comparable official baseline artifact

Run the final evaluation with a suffix so artifacts do not overwrite earlier exploratory runs:

```bash
python -m benchmarks.craft.experiment \
  --config configs/craft/experiments/qwen_dual_dag_v1.yaml \
  --run-name-suffix _final
```

This writes the comparison report to:

```text
result/craft/comparison_qwen_dual_dag_v1_final.csv
result/craft/comparison_qwen_dual_dag_v1_final.json
```

After the experiment completes, generate aggregate Dual-DAG analysis artifacts:

```bash
python -m benchmarks.craft.dual_dag.analysis \
  --runs \
    craft_eval_qwen_ollama_final \
    craft_eval_qwen_ollama_dual_dag_final \
    craft_single_director_qwen_ollama_dual_dag_final \
    craft_official_baseline_final \
  --output result/craft/dual_dag_analysis_qwen_dual_dag_v1_final.json \
  --turn-csv-output result/craft/dual_dag_turns_qwen_dual_dag_v1_final.csv
```

Then generate the compact summary table used for quick interpretation:

```bash
python -m benchmarks.craft.experiment_summary \
  --runs \
    craft_eval_qwen_ollama_final \
    craft_eval_qwen_ollama_dual_dag_final \
    craft_single_director_qwen_ollama_dual_dag_final \
    craft_official_baseline_final \
  --analysis-input result/craft/dual_dag_analysis_qwen_dual_dag_v1_final.json \
  --output result/craft/experiment_summary_qwen_dual_dag_v1_final.csv \
  --json-output result/craft/experiment_summary_qwen_dual_dag_v1_final.json
```

The compact summary table is the easiest artifact to inspect first. Read it as follows:

- `mean_final_progress` and `completion_rate` summarize task outcome.
- `builder_fallback_rate` indicates how often Builder output had to be replaced with a verified fallback candidate.
- `gated_clarification_rate` indicates how often the Dual-DAG gate intervened with `CLARIFY`.
- `claim_support_count`, `claim_conflict_count`, and `claim_required_evidence_count` summarize Director coordination evidence for chosen actions.
- `dual_dag_node_count` and `dual_dag_edge_count` show runtime graph size.
- `supported_action_count`, `conflicted_action_count`, and `required_evidence_action_count` summarize action-level graph evidence.
- `leakage_passed` must remain true for partial-information-safe runs.

For the latest verified `_final` run, the generated compact summary was:

```text
run_name,mean_final_progress,builder_fallback_rate,gated_clarification_rate,claim_support_count,claim_conflict_count,claim_required_evidence_count,dual_dag_node_count,dual_dag_edge_count,leakage_passed
craft_eval_qwen_ollama_final,0.2384219001610306,0.5333333333333333,0.0,29,0,3,510,29,True
craft_eval_qwen_ollama_dual_dag_final,0.2384219001610306,0.5333333333333333,0.0,36,0,1,510,36,True
craft_single_director_qwen_ollama_dual_dag_final,0.2384219001610306,0.13333333333333333,0.0,10,0,1,480,10,True
craft_official_baseline_final,0.0,0.0,0.0,0,0,0,0,0,True
```

Interpretation notes:

- The 3-Director Dual-DAG run matched the non-gated 3-Director progress while increasing support evidence and reducing required-evidence count.
- The single-Director ablation reached the same progress on this small final slice, but with much less support evidence.
- Use `configs/craft/official_baseline_full.yaml` when a full upstream CRAFT CLI baseline is required; `configs/craft/official_baseline.yaml` remains a comparable artifact baseline.
- `leakage_passed=True` confirms that the prompt/artifact partial-information checks passed for all rows.

## Partial-Information Guard

The adapter separates each Director's private state from public coordination state. Director prompts may include only:

- that Director's private 2D view
- public Director messages
- Builder actions
- visible constructed structure
- public progress summary

Director prompts must not include:

- `target_structure`
- oracle candidate moves
- hidden spans or labels
- other Directors' raw private views
- a combined complete state made from all private views

`LeakageGuard` inspects prompts and raises on violations.

The runtime also uses `CraftStateManagerAdapter` to keep each `PrivateAgentState` keyed by Director and separated from `PublicCoordinationState`. The state manager rejects forbidden hidden keys such as `target_structure`, `oracle_moves`, `all_private_views`, `hidden_spans`, and `hidden_labels`.

## Known Limitations

- The initial implementation does not replace the Builder with VillagerAgent.
- Minecraft server integration is not performed.
- Full CRAFT judge script integration is a follow-up task.
- CRAFT environment and metric logic are not modified.
- The Director side is a CRAFT-specific VillagerAgent adapter runtime, not the full Minecraft-oriented task pipeline.
- The official baseline artifact path is marked as `baseline_type=comparable_artifact` and is comparable by seed/structure/turn settings, but full official CRAFT API execution remains follow-up work.
