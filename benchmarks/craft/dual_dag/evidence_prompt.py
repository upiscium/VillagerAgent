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


def append_public_evidence_context(
    *,
    prompt: str,
    reported_claims: dict[str, dict],
    hypotheses: dict[str, dict] | None = None,
) -> str:
    context = build_public_evidence_context(
        reported_claims=reported_claims,
        hypotheses=hypotheses or {},
    )
    if not context["public_claims"] and not context["public_hypotheses"]:
        return prompt
    return prompt + "\n\n" + "\n".join([
        "PUBLIC EVIDENCE CONTEXT:",
        "No oracle candidates are available. Use only this public context when weighing support, conflict, or required evidence.",
        json.dumps(context, ensure_ascii=False, sort_keys=True),
    ])


def build_public_evidence_context(
    *,
    reported_claims: dict[str, dict],
    hypotheses: dict[str, dict] | None = None,
) -> dict:
    public_claims = []
    for claim in reported_claims.values():
        if not isinstance(claim, dict):
            continue
        provenance = claim.get("provenance", {}) or {}
        if provenance.get("visibility") != "public":
            continue
        content = claim.get("content", {}) if isinstance(claim.get("content"), dict) else {}
        public_claims.append({
            "claim_id": claim.get("node_id", ""),
            "director_id": content.get("director_id", ""),
            "turn_index": provenance.get("turn_index"),
            "public_message": content.get("message", ""),
            "keywords": list(content.get("keywords", []) or []),
            "evidence_status": "requires_confirmation" if content.get("uncertain") else "reported",
        })

    public_hypotheses = []
    for hypothesis in (hypotheses or {}).values():
        if not isinstance(hypothesis, dict):
            continue
        provenance = hypothesis.get("provenance", {}) or {}
        if provenance.get("visibility") != "public":
            continue
        content = hypothesis.get("content", {}) if isinstance(hypothesis.get("content"), dict) else {}
        public_hypotheses.append({
            "hypothesis_id": hypothesis.get("node_id", ""),
            "hypothesis_type": content.get("hypothesis_type", ""),
            "status": content.get("status", ""),
            "summary": content.get("summary", ""),
            "source_claim_ids": list(content.get("source_claim_ids", []) or []),
            "action_candidate_ids": list(content.get("action_candidate_ids", []) or []),
            "confidence": hypothesis.get("confidence"),
        })

    return {
        "public_claims": sorted(public_claims, key=lambda claim: (claim.get("turn_index") or 0, claim.get("claim_id", ""))),
        "public_hypotheses": sorted(public_hypotheses, key=lambda hypothesis: hypothesis.get("hypothesis_id", "")),
    }


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
