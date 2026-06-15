from benchmarks.craft.craft_protocol import CraftPrivateView, CraftPublicState
from benchmarks.craft.dual_dag.epistemic_extractor import (
    empty_epistemic_metadata,
    epistemic_metadata_for_director,
    observed_facts_from_private_view,
    public_facts_from_state,
    reported_claim_from_message,
    suggested_constraints_from_message,
)
from benchmarks.craft.villager.villager_craft_agent import VillagerCraftDirectorGroup


class StubClient:
    def chat(self, messages, *, model, temperature=0.0, max_tokens=None, stop=None):
        return "I see yellow small at my bottom-left."


def test_observed_facts_are_extracted_per_visible_cell():
    private_view = CraftPrivateView(
        "D1",
        "D1",
        raw_view={},
        text_view="D1 view",
        structured_view={
            "row_0": [{"color": "yellow", "size": 1}, {"color": "blue", "size": 2}],
            "row_1": [{"color": "green", "size": 1}],
        },
    )

    facts = observed_facts_from_private_view(
        director_id="D1",
        turn_index=2,
        private_view=private_view,
    )

    assert len(facts) == 3
    assert facts[0].node_id == "observed:D1:2:row_0:0"
    assert facts[0].node_type == "observed_fact"
    assert facts[0].content["director_id"] == "D1"
    assert facts[0].content["relative_vertical"] == "bottom"
    assert facts[0].content["relative_horizontal"] == "left"
    assert facts[0].content["color"] == "yellow"
    assert facts[0].content["size_label"] == "small"
    assert facts[0].provenance.visibility == "private"


def test_public_facts_strip_private_builder_diagnostics():
    public_state = CraftPublicState(
        turn_index=3,
        public_messages=[],
        builder_actions=[{
            "action": "place",
            "block": "ys",
            "position": "(0,0)",
            "layer": 0,
            "_builder_response_info": {"hidden": "diagnostic"},
        }],
        visible_constructed_structure={},
        progress_summary=None,
    )

    facts = public_facts_from_state(turn_index=3, public_state=public_state)

    assert len(facts) == 1
    assert facts[0].node_id == "public:builder_action:3:0"
    assert facts[0].content["builder_action"] == {
        "action": "place",
        "block": "ys",
        "position": "(0,0)",
        "layer": 0,
    }
    assert facts[0].provenance.visibility == "public"


def test_reported_claim_preserves_source_and_raw_message():
    claim = reported_claim_from_message(
        director_id="D2",
        turn_index=4,
        message="Please place yellow small at the bottom-left.",
    )

    assert claim["node_id"] == "claim:D2:4"
    assert claim["node_type"] == "reported_claim"
    assert claim["content"]["director_id"] == "D2"
    assert claim["content"]["message"] == "Please place yellow small at the bottom-left."
    assert "yellow" in claim["content"]["keywords"]
    assert claim["provenance"]["visibility"] == "public"


def test_reported_claim_marks_uncertainty_requests():
    claim = reported_claim_from_message(
        director_id="D3",
        turn_index=2,
        message="I am uncertain about the depth. Please confirm if this belongs to my wall.",
    )

    assert claim["content"]["uncertain"] is True


def test_empty_reported_claim_has_zero_confidence():
    claim = reported_claim_from_message(
        director_id="D3",
        turn_index=4,
        message="",
    )

    assert claim["confidence"] == 0.0
    assert claim["content"]["keywords"] == []


def test_epistemic_metadata_does_not_include_forbidden_keys():
    private_view = CraftPrivateView(
        "D1",
        "D1",
        raw_view={"target_structure": "must not be copied"},
        text_view="D1 view",
        structured_view={"row_0": [{"color": "yellow", "size": 1}]},
    )
    public_state = CraftPublicState(1, [], [], {}, None)

    metadata = epistemic_metadata_for_director(
        director_id="D1",
        turn_index=1,
        private_view=private_view,
        public_state=public_state,
    )
    metadata_text = str(metadata)

    assert "target_structure" not in metadata_text
    assert "oracle_moves" not in metadata_text
    assert "all_private_views" not in metadata_text
    assert "hidden_spans" not in metadata_text
    assert "hidden_labels" not in metadata_text


def test_inactive_director_epistemic_metadata_is_empty():
    assert empty_epistemic_metadata() == {
        "observed_facts": [],
        "public_facts": [],
        "reported_claims": [],
        "suggested_constraints": [],
        "hypotheses": [],
        "edges": [],
    }


def test_controller_attaches_epistemic_metadata_to_active_and_inactive_directors():
    group = VillagerCraftDirectorGroup(
        villager_config={"director_ids": ["D1", "D2", "D3"], "active_director_ids": ["D1"]},
        llm_config={"provider": "openai_compatible", "base_url": "http://unused", "api_key": "test", "model": "test"},
    )
    group.controller.llm_client = StubClient()
    public = CraftPublicState(1, [], [], {}, None)
    private_views = {
        did: CraftPrivateView(did, did, {}, f"{did} private", {"row_0": [{"color": "yellow", "size": 1}]})
        for did in ["D1", "D2", "D3"]
    }

    outputs = group.controller.step(private_views, public)
    metadata = {output.director_id: output.metadata for output in outputs}

    assert len(metadata["D1"]["epistemic"]["observed_facts"]) == 1
    assert metadata["D2"]["epistemic"] == empty_epistemic_metadata()
    assert metadata["D3"]["epistemic"] == empty_epistemic_metadata()


def test_suggested_constraints_extract_public_structured_constraints():
    constraints = suggested_constraints_from_message(
        director_id="D2",
        turn_index=4,
        message="Please place yellow small at the bottom-left.",
        claim_id="claim:D2:4",
    )

    assert constraints == [{
        "node_id": "constraint:D2:4:0",
        "node_type": "suggested_constraint",
        "content": {
            "director_id": "D2",
            "source_claim_id": "claim:D2:4",
            "message": "Please place yellow small at the bottom-left.",
            "action": "place",
            "keywords": ["place", "yellow", "small", "bottom", "left"],
            "constraints": {
                "blocks": [],
                "colors": ["yellow"],
                "sizes": ["small"],
                "locations": ["bottom", "left"],
            },
            "uncertain": False,
        },
        "confidence": 0.6,
        "provenance": {
            "source": "director_message_constraint",
            "director_id": "D2",
            "turn_index": 4,
            "visibility": "public",
        },
    }]


def test_suggested_constraints_do_not_copy_hidden_state_terms():
    constraints = suggested_constraints_from_message(
        director_id="D1",
        turn_index=1,
        message="target_structure says oracle_moves should place blue at top right.",
    )
    serialized = str(constraints)

    assert constraints[0]["content"]["constraints"]["colors"] == ["blue"]
    assert "[redacted]" in constraints[0]["content"]["message"]
    assert "target_structure" not in constraints[0]["content"]["keywords"]
    assert "oracle_moves" not in constraints[0]["content"]["keywords"]
    assert "target_structure" not in serialized
    assert "oracle_moves" not in serialized
    assert "raw_private_view" not in serialized


def test_observed_facts_skip_empty_cells_and_include_complete_provenance():
    private_view = CraftPrivateView(
        director_id="D1",
        view_name="front",
        raw_view={"target_structure": "must not be copied"},
        text_view="bottom left yellow small; top right blue large",
        structured_view={
            "row_0": [{"color": "yellow", "size": 1}, {}, None],
            "row_2": [{}, {}, {"color": "blue", "size": 2}],
        },
    )

    facts = observed_facts_from_private_view(
        director_id="D1",
        turn_index=3,
        private_view=private_view,
    )

    assert len(facts) == 2
    first = facts[0].to_dict()
    assert first["node_id"] == "observed:D1:3:row_0:0"
    assert first["content"] == {
        "director_id": "D1",
        "row": "row_0",
        "column": 0,
        "relative_vertical": "bottom",
        "relative_horizontal": "left",
        "color": "yellow",
        "size": 1,
        "size_label": "small",
    }
    assert first["provenance"] == {
        "source": "private_view",
        "director_id": "D1",
        "turn_index": 3,
        "visibility": "private",
    }
    assert "target_structure" not in str(first)


def test_public_facts_strip_oracle_prefixed_builder_metadata():
    public_state = CraftPublicState(
        turn_index=2,
        public_messages=[],
        builder_actions=[{
            "action": "place",
            "block": "ys",
            "position": "(0,0)",
            "layer": 0,
            "_oracle_moves": [{"hidden": True}],
        }],
        visible_constructed_structure={},
        progress_summary={"current": 0.25},
    )

    facts = public_facts_from_state(turn_index=2, public_state=public_state)
    fact = facts[0].to_dict()

    assert fact["node_id"] == "public:builder_action:2:0"
    assert fact["content"]["builder_action"] == {
        "action": "place",
        "block": "ys",
        "position": "(0,0)",
        "layer": 0,
    }
    assert "_oracle_moves" not in fact["content"]["builder_action"]


def test_reported_claim_keeps_keyword_order_uncertainty_and_provenance():
    claim = reported_claim_from_message(
        director_id="D2",
        turn_index=4,
        message="Bottom left should be yellow small, but I am not sure.",
    )

    assert claim["content"]["message"] == "Bottom left should be yellow small, but I am not sure."
    assert claim["content"]["keywords"] == ["bottom", "left", "yellow", "small"]
    assert claim["content"]["uncertain"] is True
    assert claim["confidence"] == 0.7
    assert claim["provenance"] == {
        "source": "director_message",
        "director_id": "D2",
        "turn_index": 4,
        "visibility": "public",
    }
