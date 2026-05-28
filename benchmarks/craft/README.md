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

## Known Limitations

- The initial implementation does not replace the Builder with VillagerAgent.
- Minecraft server integration is not performed.
- Full CRAFT judge script integration is a follow-up task.
- CRAFT environment and metric logic are not modified.
