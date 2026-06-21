# Experiment Run Commands And Naming

Use reproducible run names shaped as:

```text
<benchmark>_<condition>_<model>_seed<seed>_<scenario-or-structure>_<suffix>
```

Rules:

- Use lowercase benchmark names such as `craft` or `minecraft`.
- Use condition names such as `baseline`, `dual_dag`, `disabled`, or `enabled`.
- Include model family when an LLM is part of the run, for example `qwen_ollama`.
- Include `seed<seed>` whenever a seed is configured.
- Include the structure id or scenario name for scoped runs.
- Add a short suffix for issue or batch context, for example `issue107_smoke`.
- Keep names filesystem-safe: letters, numbers, `_`, `-`, and `.` only.

CRAFT command templates:

```bash
python -m benchmarks.craft.run --config configs/craft/eval_qwen_ollama.yaml --dry-run --structure 0 --turns 1 --seed 3
python -m benchmarks.craft.experiment --config configs/craft/experiments/qwen_dual_dag_issue104_small.yaml --structure 0,1 --turns 2 --seed 3 --run-name-suffix _issue104_small
python -m benchmarks.craft.experiment --config configs/craft/experiments/qwen_dual_dag_v1.yaml --seed 3
```

Minecraft command templates:

```bash
python -m benchmarks.minecraft.experiment --config configs/minecraft/experiments/issue110_smoke.json --run-name minecraft_disabled_local_seed0_issue110_smoke
python -m benchmarks.minecraft.experiment --config configs/minecraft/experiments/issue107_smoke_comparison.json --run-name minecraft_enabled_local_seed0_issue107_smoke --dual-dag-task-selection
python -m benchmarks.minecraft.experiment --config configs/minecraft/experiments/issue110_smoke.json --run-name minecraft_enabled_real_seed0_smoke --dual-dag-task-selection --execute
```

Each new run directory should include:

- `command.txt`: exact harness command.
- `config.resolved.yaml` and/or `config.resolved.json`: resolved config used by the run.
- `provenance.json`: benchmark, schema version, commit hash, command, and environment notes.
- Normalized benchmark artifacts such as CRAFT `normalized/summary.json` or Minecraft `summary.json` and `metrics.json`.

Re-running the same config and seed should produce comparable artifact shapes even when timestamps, UUIDs, or real environment outcomes differ.
