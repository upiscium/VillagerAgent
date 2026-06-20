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
    suggested_constraints_from_message,
)
from benchmarks.craft.dual_dag.serialization import snapshot_to_dict


RESOLVED_FACT_PRIVATE_KEYS = {
    "target_structure",
    "oracle_moves",
    "raw_private_view",
    "hidden_spans",
    "hidden_labels",
}


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
            node_dict = node.to_dict()
            self.epistemic_nodes[node.node_id] = node_dict
            self._link_observation_to_claims(node_dict)

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
        suggested_constraints = suggested_constraints_from_message(
            director_id=director_id,
            turn_index=turn_index,
            message=message,
            claim_id=claim["node_id"],
        )
        if suggested_constraints:
            claim.setdefault("content", {})["suggested_constraint_ids"] = [
                constraint["node_id"] for constraint in suggested_constraints
            ]
        for constraint in suggested_constraints:
            self.epistemic_nodes[constraint["node_id"]] = constraint
            self._add_epistemic_edge(
                source_id=claim["node_id"],
                target_id=constraint["node_id"],
                edge_type="derived_from",
                turn_index=turn_index,
                metadata={"reason": "suggested_constraint"},
            )
        self._link_observations_to_claim(claim)
        self._upsert_claim_hypothesis(claim)
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
            self.action_edges = [
                edge for edge in self.action_edges
                if edge.get("target_id") != candidate_id
                or edge.get("edge_type") not in {"supports", "conflicts_with"}
            ]
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
                self._upsert_action_hypothesis(
                    hypothesis_type="conflicting_evidence",
                    claim_id=claim_id,
                    candidate_id=candidate_id,
                    turn_index=turn_index,
                )
            for claim_id in candidate.get("required_evidence", []):
                self._upsert_action_hypothesis(
                    hypothesis_type="required_evidence",
                    claim_id=claim_id,
                    candidate_id=candidate_id,
                    turn_index=turn_index,
                )

    def add_coordination_action_candidate(
        self,
        *,
        turn_index: int,
        action_type: str,
        reason: str,
        related_candidate_ids: list[str] | None = None,
        blocking_claim_ids: list[str] | None = None,
        required_evidence_ids: list[str] | None = None,
        question: str = "",
        confidence: float = 1.0,
    ) -> dict:
        if action_type not in {"clarify", "wait_for_evidence"}:
            raise ValueError("action_type must be clarify or wait_for_evidence")
        index = sum(
            1 for node_id in self.action_nodes
            if node_id.startswith(f"coordination:{action_type}:{turn_index}:")
        )
        node_id = f"coordination:{action_type}:{turn_index}:{index}"
        related_candidate_ids = sorted(set(related_candidate_ids or []))
        blocking_claim_ids = sorted(set(blocking_claim_ids or []))
        required_evidence_ids = sorted(set(required_evidence_ids or []))
        action = {
            "action": action_type,
            "reason": reason,
        }
        if question:
            action["clarification"] = question
        candidate = {
            "node_id": node_id,
            "action_type": action_type,
            "action": action,
            "state": "candidate" if action_type == "clarify" else "waiting_for_evidence",
            "confidence": _bounded_confidence(float(confidence)),
            "supported_by": [],
            "conflicts_with": blocking_claim_ids,
            "required_evidence": required_evidence_ids,
            "metadata": {
                "coordination_action": True,
                "reason": reason,
                "related_candidate_ids": related_candidate_ids,
            },
        }
        self.action_nodes[node_id] = candidate
        for claim_id in blocking_claim_ids:
            self._add_action_edge(
                source_id=claim_id,
                target_id=node_id,
                edge_type="requires_clarification",
                turn_index=turn_index,
                metadata={"reason": reason},
            )
        for evidence_id in required_evidence_ids:
            self._add_action_edge(
                source_id=node_id,
                target_id=evidence_id,
                edge_type="waits_for_evidence",
                turn_index=turn_index,
                metadata={"reason": reason},
            )
        for candidate_id in related_candidate_ids:
            self._add_action_edge(
                source_id=node_id,
                target_id=candidate_id,
                edge_type="coordinates_action",
                turn_index=turn_index,
                metadata={"reason": reason},
            )
        return candidate

    def ingest_clarification_response(
        self,
        *,
        clarify_candidate_id: str,
        director_id: str,
        turn_index: int,
        message: str,
    ) -> dict:
        claim = self.add_reported_claim(
            director_id=director_id,
            turn_index=turn_index,
            message=message,
        )
        claim_id = claim.get("node_id", "")
        clarify_candidate = self.action_nodes.get(clarify_candidate_id, {})
        metadata = clarify_candidate.get("metadata", {}) if isinstance(clarify_candidate, dict) else {}
        related_candidate_ids = list(metadata.get("related_candidate_ids", []) or [])
        requested_evidence_ids = list(clarify_candidate.get("required_evidence", []) or [])
        self._add_action_edge(
            source_id=claim_id,
            target_id=clarify_candidate_id,
            edge_type="clarification_response",
            turn_index=turn_index,
            metadata={"director_id": director_id},
        )

        updated_candidates = []
        resolved_fact = None
        for candidate_id in related_candidate_ids:
            candidate = self.action_nodes.get(candidate_id)
            if not isinstance(candidate, dict):
                continue
            relation = claim_action_relation(
                claim,
                candidate.get("action", {}) if isinstance(candidate.get("action"), dict) else {},
                action_location_keywords(candidate.get("action", {}) if isinstance(candidate.get("action"), dict) else {}),
            )
            if relation == "supports":
                _append_unique(candidate, "supported_by", claim_id)
                if resolved_fact is None and not (claim.get("content", {}) or {}).get("uncertain", False):
                    resolved_fact = self.add_resolved_fact(
                        fact_id=f"clarification_response:{claim_id}".replace(":", "_"),
                        turn_index=turn_index,
                        summary=(claim.get("content", {}) or {}).get("message", message),
                        evidence_ids=[claim_id, *requested_evidence_ids],
                        confidence=max(float(claim.get("confidence", 0.0) or 0.0), 0.8),
                        content=_public_claim_content(claim),
                    )
            elif relation == "conflicts_with":
                _append_unique(candidate, "conflicts_with", claim_id)
            elif relation == "requires_evidence":
                _append_unique(candidate, "required_evidence", claim_id)
            updated_candidates.append(candidate)

        self.update_action_candidate_states(turn_index=turn_index)
        self.update_hypothesis_lifecycle(turn_index=turn_index)
        return {
            "reported_claim": claim,
            "resolved_fact": resolved_fact,
            "updated_candidates": updated_candidates,
        }

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

    def hypotheses(self) -> dict[str, dict]:
        return {
            node_id: node
            for node_id, node in self.epistemic_nodes.items()
            if node.get("node_type") == "hypothesis"
        }

    def resolved_facts(self) -> dict[str, dict]:
        return {
            node_id: node
            for node_id, node in self.epistemic_nodes.items()
            if node.get("node_type") == "resolved_fact"
        }

    def suggested_constraints(self) -> dict[str, dict]:
        return {
            node_id: node
            for node_id, node in self.epistemic_nodes.items()
            if node.get("node_type") == "suggested_constraint"
        }

    def add_resolved_fact(
        self,
        *,
        fact_id: str,
        turn_index: int,
        summary: str,
        evidence_ids: list[str],
        confidence: float,
        content: dict | None = None,
    ) -> dict:
        node_id = f"resolved_fact:{fact_id}"
        public_content = _public_resolved_fact_content(content or {})
        node = {
            "node_id": node_id,
            "node_type": "resolved_fact",
            "content": {
                "summary": summary,
                "evidence_ids": sorted(set(evidence_ids)),
                **public_content,
            },
            "confidence": _bounded_confidence(float(confidence)),
            "provenance": {
                "source": "dual_dag_runtime",
                "director_id": None,
                "turn_index": turn_index,
                "visibility": "public",
            },
        }
        self.epistemic_nodes[node_id] = node
        for evidence_id in sorted(set(evidence_ids)):
            self._add_epistemic_edge(
                source_id=evidence_id,
                target_id=node_id,
                edge_type="resolved_by",
                turn_index=turn_index,
                metadata={"resolved_fact_id": node_id},
            )
        return node

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

    def update_action_candidate_states(
        self,
        *,
        turn_index: int,
        min_confidence: float = 0.6,
        min_support_count: int = 1,
    ) -> list[dict]:
        resolved_evidence_ids = _resolved_evidence_ids(self.resolved_facts().values())
        public_actions = _public_builder_actions(self.epistemic_nodes.values())
        updated = []
        for candidate_id, candidate in self.action_nodes.items():
            state, unlock = _action_candidate_state(
                candidate=candidate,
                resolved_evidence_ids=resolved_evidence_ids,
                public_actions=public_actions,
                min_confidence=min_confidence,
                min_support_count=min_support_count,
            )
            candidate["state"] = state
            metadata = candidate.setdefault("metadata", {})
            metadata["unlock"] = {
                "state": state,
                "turn_index": turn_index,
                **unlock,
            }
            updated.append(candidate)
            self._add_action_state_edge(
                candidate_id=candidate_id,
                state=state,
                unlock=unlock,
                turn_index=turn_index,
            )
        return updated

    def update_hypothesis_lifecycle(self, *, turn_index: int) -> list[dict]:
        resolved_evidence_ids = _resolved_evidence_ids(self.resolved_facts().values())
        updated = []
        for hypothesis in self.hypotheses().values():
            _update_hypothesis_lifecycle(
                hypothesis=hypothesis,
                epistemic_edges=self.epistemic_edges,
                action_nodes=self.action_nodes,
                resolved_evidence_ids=resolved_evidence_ids,
                turn_index=turn_index,
            )
            updated.append(hypothesis)
        return updated

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
            "hypothesis_count": sum(
                1 for node in self.epistemic_nodes.values()
                if node.get("node_type") == "hypothesis"
            ),
            "resolved_fact_count": sum(
                1 for node in self.epistemic_nodes.values()
                if node.get("node_type") == "resolved_fact"
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

    def _upsert_claim_hypothesis(self, claim: dict) -> None:
        content = claim.get("content", {}) if isinstance(claim, dict) else {}
        if not isinstance(content, dict) or not content.get("uncertain"):
            return
        claim_id = claim.get("node_id", "")
        if not claim_id:
            return
        self._upsert_hypothesis(
            node_id=f"hypothesis:unresolved_claim:{claim_id}",
            hypothesis_type="unresolved_claim",
            turn_index=(claim.get("provenance", {}) or {}).get("turn_index", 0),
            source_claim_ids=[claim_id],
            action_candidate_ids=[],
            confidence=0.4,
            summary=content.get("message", ""),
            keywords=list(content.get("keywords", []) or []),
        )
        self._add_epistemic_edge(
            source_id=claim_id,
            target_id=f"hypothesis:unresolved_claim:{claim_id}",
            edge_type="derived_from",
            turn_index=(claim.get("provenance", {}) or {}).get("turn_index", 0),
            metadata={"reason": "uncertain_claim"},
        )

    def _link_observations_to_claim(self, claim: dict) -> None:
        claim_content = claim.get("content", {}) if isinstance(claim, dict) else {}
        claim_keywords = set(claim_content.get("keywords", [])) if isinstance(claim_content, dict) else set()
        provenance = claim.get("provenance", {}) if isinstance(claim, dict) else {}
        director_id = provenance.get("director_id")
        turn_index = provenance.get("turn_index", 0)
        if not claim_keywords or not director_id:
            return
        for node in self.epistemic_nodes.values():
            if node.get("node_type") != "observed_fact":
                continue
            node_provenance = node.get("provenance", {}) or {}
            if node_provenance.get("director_id") != director_id:
                continue
            if node_provenance.get("turn_index") != turn_index:
                continue
            overlap = _observation_claim_keyword_overlap(node, claim_keywords)
            if not overlap:
                continue
            self._add_epistemic_edge(
                source_id=node.get("node_id", ""),
                target_id=claim.get("node_id", ""),
                edge_type="supports",
                turn_index=turn_index,
                metadata={"matched_keywords": sorted(overlap)},
            )

    def _link_observation_to_claims(self, observation: dict) -> None:
        provenance = observation.get("provenance", {}) if isinstance(observation, dict) else {}
        director_id = provenance.get("director_id")
        turn_index = provenance.get("turn_index")
        if not director_id or not isinstance(turn_index, int):
            return
        for claim in self.reported_claims().values():
            claim_provenance = claim.get("provenance", {}) or {}
            if claim_provenance.get("director_id") != director_id:
                continue
            if claim_provenance.get("turn_index") != turn_index:
                continue
            claim_content = claim.get("content", {}) if isinstance(claim, dict) else {}
            claim_keywords = set(claim_content.get("keywords", [])) if isinstance(claim_content, dict) else set()
            overlap = _observation_claim_keyword_overlap(observation, claim_keywords)
            if not overlap:
                continue
            self._add_epistemic_edge(
                source_id=observation.get("node_id", ""),
                target_id=claim.get("node_id", ""),
                edge_type="supports",
                turn_index=turn_index,
                metadata={"matched_keywords": sorted(overlap)},
            )

    def _upsert_action_hypothesis(
        self,
        *,
        hypothesis_type: str,
        claim_id: str,
        candidate_id: str,
        turn_index: int,
    ) -> None:
        claim = self.epistemic_nodes.get(claim_id, {})
        claim_content = claim.get("content", {}) if isinstance(claim, dict) else {}
        candidate = self.action_nodes.get(candidate_id, {})
        candidate_action = candidate.get("action", {}) if isinstance(candidate, dict) else {}
        self._upsert_hypothesis(
            node_id=f"hypothesis:{hypothesis_type}:{claim_id}:{candidate_id}",
            hypothesis_type=hypothesis_type,
            turn_index=turn_index,
            source_claim_ids=[claim_id],
            action_candidate_ids=[candidate_id],
            confidence=0.3 if hypothesis_type == "conflicting_evidence" else 0.35,
            summary=_hypothesis_summary(
                hypothesis_type=hypothesis_type,
                claim_content=claim_content if isinstance(claim_content, dict) else {},
                candidate_action=candidate_action if isinstance(candidate_action, dict) else {},
            ),
            keywords=list(claim_content.get("keywords", []) or []) if isinstance(claim_content, dict) else [],
        )
        edge_type = "conflicts_with" if hypothesis_type == "conflicting_evidence" else "requires_confirmation_from"
        self._add_epistemic_edge(
            source_id=claim_id,
            target_id=f"hypothesis:{hypothesis_type}:{claim_id}:{candidate_id}",
            edge_type=edge_type,
            turn_index=turn_index,
            metadata={"action_candidate_id": candidate_id},
        )
        self._add_epistemic_edge(
            source_id=f"hypothesis:{hypothesis_type}:{claim_id}:{candidate_id}",
            target_id=candidate_id,
            edge_type="supports" if hypothesis_type == "required_evidence" else "conflicts_with",
            turn_index=turn_index,
            metadata={"source_claim_id": claim_id},
        )

    def _upsert_hypothesis(
        self,
        *,
        node_id: str,
        hypothesis_type: str,
        turn_index: int,
        source_claim_ids: list[str],
        action_candidate_ids: list[str],
        confidence: float,
        summary: str,
        keywords: list[str],
    ) -> None:
        existing = self.epistemic_nodes.get(node_id)
        if existing:
            content = existing.setdefault("content", {})
            content["source_claim_ids"] = sorted(set(content.get("source_claim_ids", [])) | set(source_claim_ids))
            content["action_candidate_ids"] = sorted(
                set(content.get("action_candidate_ids", [])) | set(action_candidate_ids)
            )
            content["keywords"] = sorted(set(content.get("keywords", [])) | set(keywords))
            content["last_updated_turn"] = turn_index
            existing["confidence"] = max(float(existing.get("confidence", 0.0) or 0.0), confidence)
            content["base_confidence"] = max(float(content.get("base_confidence", 0.0) or 0.0), confidence)
            return
        self.epistemic_nodes[node_id] = {
            "node_id": node_id,
            "node_type": "hypothesis",
            "content": {
                "hypothesis_type": hypothesis_type,
                "status": "open",
                "summary": summary,
                "source_claim_ids": sorted(source_claim_ids),
                "action_candidate_ids": sorted(action_candidate_ids),
                "keywords": sorted(set(keywords)),
                "created_turn": turn_index,
                "last_updated_turn": turn_index,
                "support_count": 0,
                "conflict_count": 0,
                "resolved_evidence_count": 0,
                "resolution_ready": False,
                "base_confidence": confidence,
            },
            "confidence": confidence,
            "provenance": {
                "source": "dual_dag_runtime",
                "director_id": None,
                "turn_index": turn_index,
                "visibility": "public",
            },
        }

    def _add_epistemic_edge(
        self,
        *,
        source_id: str,
        target_id: str,
        edge_type: str,
        turn_index: int,
        metadata: dict | None = None,
    ) -> None:
        if not source_id or not target_id:
            return
        edge = {
            "source_id": source_id,
            "target_id": target_id,
            "edge_type": edge_type,
            "metadata": {
                "turn_index": turn_index,
                **(metadata or {}),
            },
        }
        existing = next(
            (
                row for row in self.epistemic_edges
                if row.get("source_id") == source_id
                and row.get("target_id") == target_id
                and row.get("edge_type") == edge_type
            ),
            None,
        )
        if existing:
            existing_metadata = existing.setdefault("metadata", {})
            existing_metadata.update(metadata or {})
            existing_metadata["last_updated_turn"] = turn_index
            return
        self.epistemic_edges.append(edge)

    def _add_action_edge(
        self,
        *,
        source_id: str,
        target_id: str,
        edge_type: str,
        turn_index: int,
        metadata: dict | None = None,
    ) -> None:
        if not source_id or not target_id:
            return
        edge = {
            "source_id": source_id,
            "target_id": target_id,
            "edge_type": edge_type,
            "metadata": {
                "turn_index": turn_index,
                **(metadata or {}),
            },
        }
        existing = next(
            (
                row for row in self.action_edges
                if row.get("source_id") == source_id
                and row.get("target_id") == target_id
                and row.get("edge_type") == edge_type
            ),
            None,
        )
        if existing:
            existing_metadata = existing.setdefault("metadata", {})
            existing_metadata.update(metadata or {})
            existing_metadata["last_updated_turn"] = turn_index
            return
        self.action_edges.append(edge)

    def _add_action_state_edge(
        self,
        *,
        candidate_id: str,
        state: str,
        unlock: dict,
        turn_index: int,
    ) -> None:
        for evidence_id in unlock.get("evidence_ids", []) or []:
            edge_type = "unlocks_action" if state == "executable" else "blocks_action"
            edge = {
                "source_id": evidence_id,
                "target_id": candidate_id,
                "edge_type": edge_type,
                "metadata": {
                    "turn_index": turn_index,
                    "state": state,
                    "reason": unlock.get("reason", ""),
                },
            }
            existing = next(
                (
                    row for row in self.action_edges
                    if row.get("source_id") == edge["source_id"]
                    and row.get("target_id") == edge["target_id"]
                    and row.get("edge_type") == edge["edge_type"]
                ),
                None,
            )
            if existing:
                existing_metadata = existing.setdefault("metadata", {})
                existing_metadata.update(edge["metadata"])
                existing_metadata["last_updated_turn"] = turn_index
                continue
            self.action_edges.append(edge)


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
        "state": candidate.get("state", "candidate"),
        "confidence": confidence,
        "claim_support_count": len(candidate.get("supported_by", []) or []) + historical_support_count,
        "claim_conflict_count": len(candidate.get("conflicts_with", []) or []) + historical_conflict_count,
        "claim_required_evidence_count": len(candidate.get("required_evidence", []) or []) + historical_required_count,
    }
    metadata = candidate.get("metadata", {}) if isinstance(candidate.get("metadata"), dict) else {}
    if metadata.get("unlock"):
        row["unlock"] = metadata["unlock"]
    if graph_context:
        row["graph_context"] = graph_context
    return row


def _recommended_candidate(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    return max(
        rows,
        key=lambda row: (
            row.get("state") == "executable",
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


def _public_resolved_fact_content(content: dict) -> dict:
    return {
        str(key): _public_resolved_fact_value(value)
        for key, value in content.items()
        if _is_public_resolved_fact_key(key)
    }


def _public_resolved_fact_value(value):
    if isinstance(value, dict):
        return _public_resolved_fact_content(value)
    if isinstance(value, list):
        return [_public_resolved_fact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_public_resolved_fact_value(item) for item in value]
    return value


def _is_public_resolved_fact_key(key) -> bool:
    key_text = str(key)
    return not key_text.startswith("_") and key_text not in RESOLVED_FACT_PRIVATE_KEYS


def _actions_share_location_or_type(action: dict, prior_action: dict, location_keywords: set[str]) -> bool:
    prior_location_keywords = action_location_keywords(prior_action)
    if location_keywords and prior_location_keywords:
        return bool(location_keywords & prior_location_keywords)
    return bool(action.get("action") and action.get("action") == prior_action.get("action"))


def _action_candidate_state(
    *,
    candidate: dict,
    resolved_evidence_ids: set[str],
    public_actions: list[dict],
    min_confidence: float,
    min_support_count: int,
) -> tuple[str, dict]:
    conflicts = list(candidate.get("conflicts_with", []) or [])
    if conflicts:
        return "invalidated", {"reason": "conflicting_evidence", "evidence_ids": conflicts}

    required = list(candidate.get("required_evidence", []) or [])
    unresolved = [evidence_id for evidence_id in required if evidence_id not in resolved_evidence_ids]
    if unresolved:
        return "waiting_for_evidence", {"reason": "required_evidence_unresolved", "evidence_ids": unresolved}
    if required:
        return "executable", {"reason": "required_evidence_resolved", "evidence_ids": required}

    supported_by = list(candidate.get("supported_by", []) or [])
    if len(supported_by) >= min_support_count:
        return "executable", {"reason": "supported_by_public_claims", "evidence_ids": supported_by}

    action = candidate.get("action", {}) if isinstance(candidate.get("action"), dict) else {}
    matching_public_actions = [
        public_action.get("node_id", "")
        for public_action in public_actions
        if _matches_public_builder_action(action, public_action.get("action", {}))
    ]
    if matching_public_actions:
        return "executable", {"reason": "matches_public_board_state", "evidence_ids": matching_public_actions}

    metadata = candidate.get("metadata", {}) if isinstance(candidate.get("metadata"), dict) else {}
    if metadata.get("physically_verified"):
        return "executable", {"reason": "physically_verified", "evidence_ids": []}

    if float(candidate.get("confidence", 0.0) or 0.0) >= min_confidence:
        return "executable", {"reason": "confidence_threshold", "evidence_ids": []}

    return "blocked", {"reason": "insufficient_public_evidence", "evidence_ids": []}


def _append_unique(row: dict, key: str, value: str) -> None:
    values = list(row.get(key, []) or [])
    if value not in values:
        values.append(value)
    row[key] = values


def _update_hypothesis_lifecycle(
    *,
    hypothesis: dict,
    epistemic_edges: list[dict],
    action_nodes: dict[str, dict],
    resolved_evidence_ids: set[str],
    turn_index: int,
) -> None:
    content = hypothesis.setdefault("content", {})
    hypothesis_id = hypothesis.get("node_id", "")
    source_claim_ids = list(content.get("source_claim_ids", []) or [])
    action_candidate_ids = list(content.get("action_candidate_ids", []) or [])

    support_count = _hypothesis_edge_count(epistemic_edges, hypothesis_id, "supports")
    conflict_count = _hypothesis_edge_count(epistemic_edges, hypothesis_id, "conflicts_with")
    resolved_count = sum(1 for evidence_id in source_claim_ids if evidence_id in resolved_evidence_ids)
    invalidated_action_count = sum(
        1 for action_id in action_candidate_ids
        if (action_nodes.get(action_id, {}) or {}).get("state") == "invalidated"
    )

    resolution_ready = bool(source_claim_ids) and resolved_count == len(set(source_claim_ids))
    if invalidated_action_count:
        status = "invalidated"
    elif conflict_count:
        status = "conflicted"
    elif resolution_ready:
        status = "resolved"
    elif support_count or resolved_count:
        status = "supported"
    else:
        status = "open"

    base_confidence = float(content.setdefault("base_confidence", hypothesis.get("confidence", 0.0)) or 0.0)
    hypothesis["confidence"] = round(_bounded_confidence(
        base_confidence
        + 0.15 * support_count
        + 0.2 * resolved_count
        - 0.2 * conflict_count
        - 0.25 * invalidated_action_count
    ), 6)
    content["status"] = status
    content["support_count"] = support_count
    content["conflict_count"] = conflict_count
    content["resolved_evidence_count"] = resolved_count
    content["resolution_ready"] = resolution_ready
    content["last_updated_turn"] = turn_index


def _hypothesis_edge_count(epistemic_edges: list[dict], hypothesis_id: str, edge_type: str) -> int:
    return sum(
        1 for edge in epistemic_edges
        if edge.get("edge_type") == edge_type
        and (edge.get("source_id") == hypothesis_id or edge.get("target_id") == hypothesis_id)
    )


def _resolved_evidence_ids(resolved_facts) -> set[str]:
    evidence_ids = set()
    for fact in resolved_facts:
        fact_id = fact.get("node_id", "") if isinstance(fact, dict) else ""
        if fact_id:
            evidence_ids.add(fact_id)
        content = fact.get("content", {}) if isinstance(fact, dict) else {}
        if isinstance(content, dict):
            evidence_ids.update(str(evidence_id) for evidence_id in content.get("evidence_ids", []) or [])
    return evidence_ids


def _public_builder_actions(nodes) -> list[dict]:
    actions = []
    for node in nodes:
        if not isinstance(node, dict) or node.get("node_type") != "public_fact":
            continue
        provenance = node.get("provenance", {}) or {}
        if provenance.get("visibility") != "public":
            continue
        content = node.get("content", {}) if isinstance(node.get("content"), dict) else {}
        action = content.get("builder_action") if isinstance(content, dict) else None
        if not isinstance(action, dict):
            continue
        actions.append({
            "node_id": node.get("node_id", ""),
            "action": {key: value for key, value in action.items() if not str(key).startswith("_")},
        })
    return actions


def _matches_public_builder_action(action: dict, public_action: dict) -> bool:
    keys = ["action", "block", "position", "layer", "span_to"]
    return all(action.get(key) == public_action.get(key) for key in keys if action.get(key) is not None)


def _hypothesis_summary(*, hypothesis_type: str, claim_content: dict, candidate_action: dict) -> str:
    message = claim_content.get("message", "")
    action = {
        key: value
        for key, value in candidate_action.items()
        if not str(key).startswith("_")
    }
    if hypothesis_type == "conflicting_evidence":
        return f"Claim conflicts with candidate action: {message} / {action}"
    if hypothesis_type == "required_evidence":
        return f"Claim requires more evidence before candidate action: {message} / {action}"
    return message


def _observation_claim_keyword_overlap(observation: dict, claim_keywords: set[str]) -> set[str]:
    content = observation.get("content", {}) if isinstance(observation, dict) else {}
    if not isinstance(content, dict):
        return set()
    observation_keywords = {
        content.get("color"),
        content.get("size_label"),
        content.get("relative_vertical"),
        content.get("relative_horizontal"),
    }
    return {str(keyword) for keyword in observation_keywords if keyword in claim_keywords}


def _bounded_confidence(value: float) -> float:
    return max(0.0, min(1.0, value))
