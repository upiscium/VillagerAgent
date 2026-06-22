# CRAFT Dual-DAG Ablation Manifests

## Scope

This document records the staged CRAFT Dual-DAG ablation manifests for #130.

The smoke manifest covers C0-C6:

| Condition | Config | Purpose |
| --- | --- | --- |
| C0 VA baseline | `configs/craft/eval_gemma4_12b_ollama.yaml` | VillagerAgent baseline with Dual-DAG intervention flags off. |
| C1 metadata only | `configs/craft/eval_gemma4_12b_ollama_dual_dag_metadata_only.yaml` | Dual-DAG graph/artifact bookkeeping with evidence summary, runtime decision support, retrieval, and gating disabled. |
| C2 current-turn evidence | `configs/craft/eval_gemma4_12b_ollama_dual_dag_current_evidence.yaml` | Current-turn evidence summary and decision support enabled, historical retrieval and gating disabled. |
| C3 historical retrieval | `configs/craft/eval_gemma4_12b_ollama_dual_dag_retrieval.yaml` | C2 plus historical public graph retrieval, gating disabled. |
| C4 gating without coordination actions | `configs/craft/eval_gemma4_12b_ollama_dual_dag_gating_no_coordination.yaml` | Gate metadata is recorded, but Clarify/Wait coordination actions are not emitted, so physical-action throughput is not reduced by coordination turns. |
| C5 Clarify only | `configs/craft/eval_gemma4_12b_ollama_dual_dag_clarify_only.yaml` | Gating may emit Clarify, while WaitForEvidence remains disabled. |
| C6 full current Dual-DAG | `configs/craft/eval_gemma4_12b_ollama_dual_dag.yaml` | Current full Dual-DAG setting. |

## Commands

Validate config parity for C1-C6 against C0:

```bash
python -m benchmarks.craft.validate_comparison_configs --baseline configs/craft/eval_gemma4_12b_ollama.yaml --treatment configs/craft/eval_gemma4_12b_ollama_dual_dag_metadata_only.yaml
python -m benchmarks.craft.validate_comparison_configs --baseline configs/craft/eval_gemma4_12b_ollama.yaml --treatment configs/craft/eval_gemma4_12b_ollama_dual_dag_current_evidence.yaml
python -m benchmarks.craft.validate_comparison_configs --baseline configs/craft/eval_gemma4_12b_ollama.yaml --treatment configs/craft/eval_gemma4_12b_ollama_dual_dag_retrieval.yaml
python -m benchmarks.craft.validate_comparison_configs --baseline configs/craft/eval_gemma4_12b_ollama.yaml --treatment configs/craft/eval_gemma4_12b_ollama_dual_dag_gating_no_coordination.yaml
python -m benchmarks.craft.validate_comparison_configs --baseline configs/craft/eval_gemma4_12b_ollama.yaml --treatment configs/craft/eval_gemma4_12b_ollama_dual_dag_clarify_only.yaml
python -m benchmarks.craft.validate_comparison_configs --baseline configs/craft/eval_gemma4_12b_ollama.yaml --treatment configs/craft/eval_gemma4_12b_ollama_dual_dag.yaml
```

Dry-run the smoke manifest:

```bash
python -m benchmarks.craft.experiment --config configs/craft/experiments/gemma4_12b_dual_dag_ablation_smoke.yaml --dry-run
```

Run the smoke manifest when model budget is available:

```bash
python -m benchmarks.craft.experiment --config configs/craft/experiments/gemma4_12b_dual_dag_ablation_smoke.yaml
```

## Expected Outputs

- `result/craft/comparison_gemma4_12b_dual_dag_ablation_smoke.csv`
- `result/craft/comparison_gemma4_12b_dual_dag_ablation_smoke.json`
- `result/craft/summary_gemma4_12b_dual_dag_ablation_smoke.csv`
- `result/craft/summary_gemma4_12b_dual_dag_ablation_smoke.json`
- `result/craft/variance_gemma4_12b_dual_dag_ablation_smoke.csv`
- `result/craft/variance_gemma4_12b_dual_dag_ablation_smoke.json`

Reports must use `run_group` grouping so the C0-C3 conditions remain separate even when the CRAFT `condition` label is `villageragent_directors`.
