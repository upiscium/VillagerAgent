# Issue 107 Minecraft Smoke Comparison

Command:

```bash
python -m benchmarks.minecraft.experiment --config configs/minecraft/experiments/issue107_smoke_comparison.json --run-name issue107_disabled
python -m benchmarks.minecraft.experiment --config configs/minecraft/experiments/issue107_smoke_comparison.json --run-name issue107_enabled --dual-dag-task-selection
```

Outputs:

- `result/minecraft/issue107_disabled/`
- `result/minecraft/issue107_enabled/`

Result:

- Both dry-run smoke runs completed without environment instability.
- Decision support recommended `minecraft:task:find_chest` (`Find chest`) in both runs.
- Disabled run preserved the original order: `Open locked door`, `Find chest`.
- Enabled run reordered tasks to: `Find chest`, `Open locked door`.
- Selected task changed from `Open locked door` to `Find chest` when Dual-DAG task selection was enabled.
- The comparison uses fixture action logs only; no real Minecraft environment was launched.
