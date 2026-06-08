import json


HIDDEN_STATE_KEYS = {
    "target_structure",
    "oracle_moves",
    "all_private_views",
    "raw_private_view",
    "hidden_spans",
    "hidden_labels",
}


def append_public_evidence_summary(
    *,
    prompt: str,
    candidates: list[dict],
    reported_claims: dict[str, dict],
) -> str:
    summary = build_public_evidence_summary(
        candidates=candidates,
        reported_claims=reported_claims,
    )
    if not summary:
        return prompt
    return prompt + "\n\n" + "\n".join([
        "PUBLIC EVIDENCE SUMMARY:",
        "Use only these public Director claims when weighing support, conflict, or missing evidence.",
        json.dumps(summary, ensure_ascii=False, sort_keys=True),
    ])


def build_public_evidence_summary(*, candidates: list[dict], reported_claims: dict[str, dict]) -> list[dict]:
    claims_by_id = {
        claim.get("node_id"): claim
        for claim in reported_claims.values()
        if isinstance(claim, dict) and claim.get("node_id")
    }
    summaries = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        support_ids = list(candidate.get("supported_by", []) or [])
        conflict_ids = list(candidate.get("conflicts_with", []) or [])
        required_ids = list(candidate.get("required_evidence", []) or [])
        if not (support_ids or conflict_ids or required_ids):
            continue
        summaries.append({
            "candidate_id": candidate.get("node_id", ""),
            "action": _public_action(candidate.get("action", {})),
            "supporting_public_claims": _public_claims(support_ids, claims_by_id),
            "conflicting_public_claims": _public_claims(conflict_ids, claims_by_id),
            "missing_public_evidence_claims": _public_claims(required_ids, claims_by_id),
        })
    return summaries


def prompt_contains_hidden_state_key(prompt: str) -> bool:
    return any(key in prompt for key in HIDDEN_STATE_KEYS)


def _public_claims(claim_ids: list[str], claims_by_id: dict[str, dict]) -> list[dict]:
    claims = []
    for claim_id in claim_ids:
        claim = claims_by_id.get(claim_id)
        if not claim:
            claims.append({"claim_id": claim_id})
            continue
        content = claim.get("content", {}) if isinstance(claim.get("content"), dict) else {}
        claims.append({
            "claim_id": claim_id,
            "director_id": content.get("director_id", ""),
            "turn_index": (claim.get("provenance", {}) or {}).get("turn_index"),
            "public_message": content.get("message", ""),
            "keywords": list(content.get("keywords", []) or []),
            "uncertain": bool(content.get("uncertain", False)),
        })
    return claims


def _public_action(action: dict) -> dict:
    if not isinstance(action, dict):
        return {}
    return {
        key: value
        for key, value in action.items()
        if not str(key).startswith("_")
    }
