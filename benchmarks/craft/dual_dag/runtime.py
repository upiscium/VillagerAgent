from benchmarks.craft.craft_protocol import CraftPrivateView, CraftPublicState
from benchmarks.craft.dual_dag.action_candidates import action_candidates_from_moves
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
