# Issue 114 Builder Fallback Monitoring

## Intended Batch

- Manifest: `configs/craft/experiments/qwen_dual_dag_issue114_fallback.yaml`
- Structures: `0,1,2`
- Turns: `2`
- Seed: `3`
- Runs:
  - `craft_eval_qwen_ollama_issue114_fallback_baseline_seed3`
  - `craft_eval_qwen_ollama_dual_dag_issue114_fallback_dual_dag_seed3`

## Commands

```bash
python -m benchmarks.craft.experiment --config configs/craft/experiments/qwen_dual_dag_issue114_fallback.yaml
python -m benchmarks.craft.fallback_monitor --runs craft_eval_qwen_ollama_issue114_fallback_baseline_seed3 craft_eval_qwen_ollama_dual_dag_issue114_fallback_dual_dag_seed3 --result-root result/craft --output result/craft/fallback_monitor_issue114.json --csv-output result/craft/fallback_monitor_issue114.csv
```

## Current Environment Result

The batch produced diagnosable failure artifacts for both runs before any CRAFT turns executed:

- Error type: `OllamaPreflightError`
- Error message after endpoint update: DNS failure resolving `https://ollama.arc.upiscium.dev`
- Follow-up endpoint check showed the Ollama service is available over `http://ollama.arc.upiscium.dev`; HTTPS fails with TLS SNI `unrecognized name`.
- Dual-DAG behavior was not exercised, so no conclusion can be drawn about larger-batch fallback rates from this environment attempt.

## Monitoring Status

- `builder_fallback_count` and `builder_fallback_rate` are already present in normalized CRAFT summaries and comparison reports.
- `benchmarks.craft.fallback_monitor` now reports fallback counts/rates by condition and by condition plus structure.
- Failed runs are recorded separately with `error_type` and `error_message`, rather than being interpreted as Dual-DAG fallback behavior.

## Interpretation Rule

If a future successful larger run shows Dual-DAG consistently increasing fallback rate by condition and structure, inspect Builder prompt/action-candidate metadata and open an implementation fix issue.
