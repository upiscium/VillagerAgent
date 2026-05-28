import argparse
from pathlib import Path

from benchmarks.craft.config import (
    InvalidConfigError,
    condition_from_config,
    load_config,
    output_dir_for_config,
    save_resolved_config,
)
from benchmarks.craft.craft_env_adapter import CraftEnvAdapter
from benchmarks.craft.result_converter import normalize_results


CONDITIONS = {"official_baseline", "villageragent_directors", "single_director_ablation"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run VillagerAgent on CRAFT.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--structure", default=None)
    parser.add_argument("--turns", type=int, default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--condition", choices=sorted(CONDITIONS), default=None)
    return parser.parse_args()


def _structure_override(value: str | None) -> list[int] | None:
    if value is None:
        return None
    return [int(part) for part in value.split(",") if part.strip()]


def _write_command(output_dir: Path, args: argparse.Namespace) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    command = "python -m benchmarks.craft.run --config " + args.config
    if args.dry_run:
        command += " --dry-run"
    if args.structure is not None:
        command += f" --structure {args.structure}"
    if args.turns is not None:
        command += f" --turns {args.turns}"
    if args.seed is not None:
        command += f" --seed {args.seed}"
    if args.condition is not None:
        command += f" --condition {args.condition}"
    (output_dir / "command.txt").write_text(command + "\n", encoding="utf-8")


def _print_dry_run(config: dict, condition: str, output_dir: Path) -> None:
    print("Benchmark: CRAFT")
    print(f"Condition: {condition}")
    print(f"Structures: {config['run'].get('structures')}")
    print(f"Turns: {config['run'].get('turns')}")
    print(f"Seed: {config['run'].get('seed')}")
    print(f"CRAFT repo: {config['craft'].get('repo_path')}")
    print(f"Director model: {config['models']['director'].get('model')}")
    print(f"Builder model: {config['models']['builder'].get('model')}")
    print("Partial information guard: enabled")
    print(f"Output: {output_dir}")


def _require_runtime_api_keys(config: dict) -> None:
    for model_name in ("director", "builder"):
        model_config = config.get("models", {}).get(model_name, {})
        if model_config.get("api_key_env") and not model_config.get("api_key"):
            raise InvalidConfigError(
                f"Environment variable {model_config['api_key_env']} is not set"
            )


def main() -> None:
    args = parse_args()
    overrides = {
        "structures": _structure_override(args.structure),
        "turns": args.turns,
        "seed": args.seed,
        "condition": args.condition,
    }
    config = load_config(args.config, overrides=overrides, require_api_keys=False)
    condition = args.condition or condition_from_config(config)
    if not args.dry_run and condition != "official_baseline":
        _require_runtime_api_keys(config)
    output_dir = output_dir_for_config(config)
    save_resolved_config(config, output_dir)
    _write_command(output_dir, args)
    (output_dir / "logs").mkdir(parents=True, exist_ok=True)
    (output_dir / "raw").mkdir(parents=True, exist_ok=True)
    (output_dir / "normalized").mkdir(parents=True, exist_ok=True)
    if args.dry_run:
        _print_dry_run(config, condition, output_dir)
        return

    adapter = CraftEnvAdapter(config, output_dir)
    raw_result = adapter.run(condition)
    normalize_results(
        config=config,
        condition=condition,
        raw_result=raw_result,
        output_dir=output_dir,
    )


if __name__ == "__main__":
    main()
