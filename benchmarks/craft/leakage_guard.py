import json
from pathlib import Path
from typing import Any


class PartialInformationLeakageError(RuntimeError):
    """Raised when hidden CRAFT information appears in a prompt."""


class LeakageGuard:
    def __init__(self, config: dict):
        self.config = config
        self.reports: list[dict] = []

    def inspect_prompt(
        self,
        *,
        director_id: str,
        prompt_messages: list[dict],
        forbidden_payloads: dict,
        artifact_path: Path | None = None,
    ) -> dict:
        prompt_text = "\n".join(m.get("content", "") for m in prompt_messages)
        violations = []
        for label, payload in forbidden_payloads.items():
            for needle in _payload_needles(payload):
                if needle and needle in prompt_text:
                    violations.append({"label": label, "match": needle[:200]})
                    break

        report = {
            "director_id": director_id,
            "passed": not violations,
            "violations": violations,
        }
        if artifact_path is not None:
            report["artifact_path"] = str(artifact_path)
        self.reports.append(report)
        if violations:
            raise PartialInformationLeakageError(
                f"Partial-information leakage detected for {director_id}: {violations}"
            )
        return report

    def inspect_prompt_artifact(
        self,
        *,
        artifact_path: Path,
        forbidden_payloads: dict,
    ) -> dict:
        with artifact_path.open("r", encoding="utf-8") as f:
            artifact = json.load(f)
        return self.inspect_prompt(
            director_id=artifact.get("director_id", artifact_path.stem),
            prompt_messages=artifact.get("prompt_messages", []),
            forbidden_payloads=forbidden_payloads,
            artifact_path=artifact_path,
        )

    def save_report(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump({"checks": self.reports}, f, ensure_ascii=False, indent=2)


def _payload_needles(payload: Any) -> list[str]:
    if payload is None:
        return []
    if isinstance(payload, str):
        return [payload] if len(payload) >= 6 else []
    if isinstance(payload, (int, float, bool)):
        return []
    if isinstance(payload, dict):
        compact = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        pretty = json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2)
        return [compact, pretty] if len(compact) >= 6 else []
    if isinstance(payload, list):
        compact = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        pretty = json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2)
        return [compact, pretty] if len(compact) >= 6 else []
    return [str(payload)] if len(str(payload)) >= 6 else []
