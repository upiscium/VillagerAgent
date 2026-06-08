from benchmarks.craft.craft_protocol import CraftPrivateView, CraftPublicState
from benchmarks.craft.dual_dag.action_candidates import (
    action_candidates_from_moves,
    action_location_keywords,
    claim_action_relation,
)
from benchmarks.craft.dual_dag.epistemic_extractor import (
    observed_facts_from_private_view,
    public_facts_from_state,
    reported_claim_from_message,
)
from benchmarks.craft.dual_dag.serialization import snapshot_to_dict


class DualDAGRuntime:
    def __init__(self, *, director_ids: list[str], config: dict):
        self.director_ids = list(director_ids)
        self.config = config
        self.reset()

    def reset(self) -> None:
        self.epistemic_nodes: dict[str, dict] = {}
        self.epistemic_edges: list[dict] = []
        self.action_nodes: dict[str, dict] = {}
        self.action_edges: list[dict] = []

    def update_private_observation(
        self,
        *,
        director_id: str,
        turn_index: int,
        private_view: CraftPrivateView,
    ) -> None:
        for node in observed_facts_from_private_view(
            director_id=director_id,
            turn_index=turn_index,
            private_view=private_view,
        ):
            self.epistemic_nodes[node.node_id] = node.to_dict()

    def update_public_state(self, *, turn_index: int, public_state: CraftPublicState) -> None:
        for node in public_facts_from_state(turn_index=turn_index, public_state=public_state):
            self.epistemic_nodes[node.node_id] = node.to_dict()

    def add_reported_claim(self, *, director_id: str, turn_index: int, message: str) -> dict:
        claim = reported_claim_from_message(
            director_id=director_id,
            turn_index=turn_index,
            message=message,
        )
        self.epistemic_nodes[claim["node_id"]] = claim
        return claim

    def add_action_candidates(
        self,
        *,
        turn_index: int,
        candidates: list[dict],
    ) -> None:
        for candidate in candidates:
            candidate_id = candidate.get("node_id")
            if not candidate_id:
                continue
            self.action_nodes[candidate_id] = candidate
            for claim_id in candidate.get("supported_by", []):
                self.action_edges.append({
                    "source_id": claim_id,
                    "target_id": candidate_id,
                    "edge_type": "supports",
                    "metadata": {"turn_index": turn_index},
                })
            for claim_id in candidate.get("conflicts_with", []):
                self.action_edges.append({
                    "source_id": claim_id,
                    "target_id": candidate_id,
                    "edge_type": "conflicts_with",
                    "metadata": {"turn_index": turn_index},
                })

    def build_action_candidates(
        self,
        *,
        turn_index: int,
        oracle_moves: list[dict] | None,
        parsed_action: dict | None = None,
    ) -> list[dict]:
        candidates = action_candidates_from_moves(
            moves=oracle_moves or ([parsed_action] if parsed_action else []),
            reported_claims=self.reported_claims(),
            turn_index=turn_index,
        )
        candidate_dicts = [candidate.to_dict() for candidate in candidates]
        self.add_action_candidates(turn_index=turn_index, candidates=candidate_dicts)
        return candidate_dicts

    def add_public_builder_action(self, *, turn_index: int, action: dict) -> None:
        public_action = {key: value for key, value in action.items() if not key.startswith("_")}
        index = sum(
            1 for node_id in self.epistemic_nodes
            if node_id.startswith(f"public:builder_action:{turn_index}:")
        )
        node_id = f"public:builder_action:{turn_index}:{index}"
        self.epistemic_nodes[node_id] = {
            "node_id": node_id,
            "node_type": "public_fact",
            "content": {"builder_action": public_action},
            "confidence": 1.0,
            "provenance": {
                "source": "builder_action",
                "director_id": None,
                "turn_index": turn_index,
                "visibility": "public",
            },
        }

    def reported_claims(self) -> dict[str, dict]:
        return {
            node_id: node
            for node_id, node in self.epistemic_nodes.items()
            if node.get("node_type") == "reported_claim"
        }

    def current_turn_decision_support(
        self,
        *,
        turn_index: int,
        candidates: list[dict],
        use_historical_graph_context: bool = False,
    ) -> dict:
        rows = [
            _decision_support_candidate(
                candidate,
                graph_context=self.retrieve_public_graph_context(
                    turn_index=turn_index,
                    action=candidate.get("action", {}),
                ) if use_historical_graph_context else None,
            )
            for candidate in candidates
        ]
        recommended = _recommended_candidate(rows)
        return {
            "turn_index": turn_index,
            "candidate_count": len(rows),
            "recommended_candidate_id": recommended.get("node_id", "") if recommended else "",
            "has_conflicts": any(row["claim_conflict_count"] > 0 for row in rows),
            "has_required_evidence": any(row["claim_required_evidence_count"] > 0 for row in rows),
            "candidates": rows,
        }

    def retrieve_public_graph_context(
        self,
        *,
        turn_index: int,
        action: dict,
        max_claims: int = 5,
        max_actions: int = 5,
    ) -> dict:
        location_keywords = action_location_keywords(action)
        relevant_claims = []
        for claim in self.reported_claims().values():
            if not _is_prior_public_node(claim, turn_index):
                continue
            relation = claim_action_relation(claim, action, location_keywords)
            if relation is None:
                continue
            relevant_claims.append({
                "node_id": claim.get("node_id", ""),
                "relation": relation,
                "turn_index": (claim.get("provenance", {}) or {}).get("turn_index"),
                "content": _public_claim_content(claim),
            })

        relevant_actions = []
        for node in self.epistemic_nodes.values():
            if not _is_prior_public_node(node, turn_index):
                continue
            content = node.get("content", {}) if isinstance(node, dict) else {}
            builder_action = content.get("builder_action") if isinstance(content, dict) else None
            if not isinstance(builder_action, dict):
                continue
            if not _actions_share_location_or_type(action, builder_action, location_keywords):
                continue
            relevant_actions.append({
                "node_id": node.get("node_id", ""),
                "turn_index": (node.get("provenance", {}) or {}).get("turn_index"),
                "action": {
                    key: value
                    for key, value in builder_action.items()
                    if not str(key).startswith("_")
                },
            })

        return {
            "query": {
                "turn_index": turn_index,
                "action": {
                    key: value
                    for key, value in action.items()
                    if not str(key).startswith("_")
                },
                "location_keywords": sorted(location_keywords),
            },
            "relevant_public_claims": sorted(
                relevant_claims,
                key=lambda claim: claim.get("turn_index") or 0,
                reverse=True,
            )[:max_claims],
            "relevant_public_actions": sorted(
                relevant_actions,
                key=lambda action_row: action_row.get("turn_index") or 0,
                reverse=True,
            )[:max_actions],
        }

    def snapshot_summary(self) -> dict:
        return {
            "epistemic_node_count": len(self.epistemic_nodes),
            "action_node_count": len(self.action_nodes),
            "epistemic_edge_count": len(self.epistemic_edges),
            "action_edge_count": len(self.action_edges),
            "reported_claim_count": sum(
                1 for node in self.epistemic_nodes.values()
                if node.get("node_type") == "reported_claim"
            ),
            "action_candidate_count": len(self.action_nodes),
        }

    def snapshot(self) -> dict:
        return {
            "director_ids": self.director_ids,
            "summary": self.snapshot_summary(),
            "epistemic_nodes": list(self.epistemic_nodes.values()),
            "epistemic_edges": self.epistemic_edges,
            "action_nodes": list(self.action_nodes.values()),
            "action_edges": self.action_edges,
        }

    def serialized_snapshot(self) -> dict:
        return snapshot_to_dict(self)


def _decision_support_candidate(candidate: dict, *, graph_context: dict | None = None) -> dict:
    historical_claims = (graph_context or {}).get("relevant_public_claims", [])
    historical_support_count = sum(1 for claim in historical_claims if claim.get("relation") == "supports")
    historical_conflict_count = sum(1 for claim in historical_claims if claim.get("relation") == "conflicts_with")
    historical_required_count = sum(1 for claim in historical_claims if claim.get("relation") == "requires_evidence")
    confidence = float(candidate.get("confidence", 0.0) or 0.0)
    if graph_context:
        confidence = _bounded_confidence(
            confidence
            + 0.05 * historical_support_count
            - 0.1 * historical_conflict_count
            - 0.05 * historical_required_count
        )
    row = {
        "node_id": candidate.get("node_id", ""),
        "action": candidate.get("action", {}),
        "confidence": confidence,
        "claim_support_count": len(candidate.get("supported_by", []) or []) + historical_support_count,
        "claim_conflict_count": len(candidate.get("conflicts_with", []) or []) + historical_conflict_count,
        "claim_required_evidence_count": len(candidate.get("required_evidence", []) or []) + historical_required_count,
    }
    if graph_context:
        row["graph_context"] = graph_context
    return row


def _recommended_candidate(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    return max(
        rows,
        key=lambda row: (
            row["claim_conflict_count"] == 0,
            row["claim_required_evidence_count"] == 0,
            row["confidence"],
            row["claim_support_count"],
        ),
    )


def _is_prior_public_node(node: dict, turn_index: int) -> bool:
    provenance = node.get("provenance", {}) if isinstance(node, dict) else {}
    if provenance.get("visibility") != "public":
        return False
    node_turn = provenance.get("turn_index")
    return isinstance(node_turn, int) and node_turn < turn_index


def _public_claim_content(claim: dict) -> dict:
    content = claim.get("content", {}) if isinstance(claim, dict) else {}
    if not isinstance(content, dict):
        return {}
    return {
        "director_id": content.get("director_id", ""),
        "message": content.get("message", ""),
        "keywords": list(content.get("keywords", []) or []),
        "uncertain": bool(content.get("uncertain", False)),
    }


def _actions_share_location_or_type(action: dict, prior_action: dict, location_keywords: set[str]) -> bool:
    prior_location_keywords = action_location_keywords(prior_action)
    if location_keywords and prior_location_keywords:
        return bool(location_keywords & prior_location_keywords)
    return bool(action.get("action") and action.get("action") == prior_action.get("action"))


def _bounded_confidence(value: float) -> float:
    return max(0.0, min(1.0, value))
