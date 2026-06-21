import argparse
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from benchmarks.craft.config import load_config, repo_root


ALLOWED_EXACT_PATHS = {
    "run.name",
    "run.output_dir",
}
ALLOWED_PREFIXES = ("dual_dag",)
IGNORED_PREFIXES = ("_meta",)


def compare_config_files(baseline_path: str, treatment_path: str) -> dict:
    baseline = load_config(baseline_path)
    treatment = load_config(treatment_path)
    return compare_configs(baseline, treatment)


def compare_configs(baseline: dict, treatment: dict) -> dict:
    baseline_fields = _flatten(baseline)
    treatment_fields = _flatten(treatment)
    paths = sorted(set(baseline_fields) | set(treatment_fields))
    matching = []
    allowed = []
    disallowed = []
    for path in paths:
        if _ignored_path(path):
            continue
        baseline_value = baseline_fields.get(path, _Missing.VALUE)
        treatment_value = treatment_fields.get(path, _Missing.VALUE)
        if baseline_value == treatment_value:
            matching.append(path)
            continue
        diff = {
            "path": path,
            "baseline": _render_value(baseline_value),
            "treatment": _render_value(treatment_value),
        }
        if _allowed_difference(path):
            allowed.append(diff)
        else:
            disallowed.append(diff)
    return {
        "passed": not disallowed,
        "matching_fields": matching,
        "allowed_differences": allowed,
        "disallowed_differences": disallowed,
        "differing_fields": allowed + disallowed,
    }


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate that paired CRAFT configs differ only in allowed fields."
    )
    parser.add_argument("--baseline", required=True, help="Baseline V config path.")
    parser.add_argument("--treatment", required=True, help="Treatment D config path.")
    parser.add_argument("--json-output", default=None, help="Optional JSON report path.")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    report = compare_config_files(args.baseline, args.treatment)
    if args.json_output:
        output = _resolve_path(args.json_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
            f.write("\n")
    print(_format_report(report))
    return 0 if report["passed"] else 1


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        fields = {}
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            fields.update(_flatten(item, path))
        return fields
    if isinstance(value, list):
        return {prefix: tuple(_freeze(item) for item in value)}
    return {prefix: value}


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _allowed_difference(path: str) -> bool:
    return path in ALLOWED_EXACT_PATHS or any(
        path == prefix or path.startswith(f"{prefix}.") for prefix in ALLOWED_PREFIXES
    )


def _ignored_path(path: str) -> bool:
    return any(path == prefix or path.startswith(f"{prefix}.") for prefix in IGNORED_PREFIXES)


def _render_value(value: Any) -> Any:
    if value is _Missing.VALUE:
        return "<missing>"
    return value


def _format_report(report: dict) -> str:
    lines = [f"validation: {'pass' if report['passed'] else 'fail'}"]
    lines.append(f"matching_fields: {len(report['matching_fields'])}")
    for path in report["matching_fields"]:
        lines.append(f"  matching {path}")
    lines.append(f"allowed_differences: {len(report['allowed_differences'])}")
    for diff in report["allowed_differences"]:
        lines.append(f"  allowed {diff['path']}: {diff['baseline']} -> {diff['treatment']}")
    lines.append(f"disallowed_differences: {len(report['disallowed_differences'])}")
    for diff in report["disallowed_differences"]:
        lines.append(f"  disallowed {diff['path']}: {diff['baseline']} -> {diff['treatment']}")
    return "\n".join(lines)


def _resolve_path(path: str) -> Path:
    output = Path(path)
    if output.is_absolute():
        return output
    return repo_root() / output


class _Missing:
    VALUE = object()


if __name__ == "__main__":
    raise SystemExit(main())
