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

## Single Director Ablation

```bash
python -m benchmarks.craft.run --config configs/craft/single_director_ablation.yaml --dry-run
```

This mode is intended to compare multi-agent coordination against a reduced single-director-style condition under the same seed, structures, and turns.

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

## Compare Runs

Use `benchmarks.craft.report` to summarize one or more normalized CRAFT runs:

```bash
python -m benchmarks.craft.report \
  --runs craft_official_baseline craft_smoke_test \
  --output result/craft/comparison_summary.csv \
  --json-output result/craft/comparison_summary.json
```

The comparison report includes run condition, number of games, turns, final progress, completion rate, models, VillagerAgent component flags, and leakage status.

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
- The official baseline artifact path is comparable by seed/structure/turn settings, but full official CRAFT API execution remains follow-up work.
