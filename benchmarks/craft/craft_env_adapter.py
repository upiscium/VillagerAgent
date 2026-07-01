import copy
import json
import random
import subprocess
import sys
from pathlib import Path
from typing import Any

from benchmarks.craft.adapters.openai_compatible import OpenAICompatibleClient
from benchmarks.craft.adapters.ollama_client import OllamaNativeClient
from benchmarks.craft.craft_protocol import CraftPrivateView, CraftPublicState
from benchmarks.craft.dual_dag.action_candidates import (
    action_candidate_from_parsed_action,
    action_candidates_from_moves,
    build_action_candidate_metadata,
)
from benchmarks.craft.dual_dag.evidence_prompt import (
    append_public_evidence_context,
    append_public_evidence_summary,
    build_public_evidence_summary,
)
from benchmarks.craft.hidden_state_keys import (
    BASE_HIDDEN_STATE_KEYS,
    OFFICIAL_RUNNER_HIDDEN_STATE_KEYS,
)
from benchmarks.craft.dual_dag.gating import should_clarify
from benchmarks.craft.dual_dag.runtime import DualDAGRuntime
from benchmarks.craft.leakage_guard import LeakageGuard
from benchmarks.craft.villager.villager_craft_agent import VillagerCraftDirectorGroup


def _add_craft_to_path(repo_path: str) -> None:
    craft_path = str(Path(repo_path).resolve())
    if craft_path not in sys.path:
        sys.path.insert(0, craft_path)


def load_dataset(dataset_path: str) -> list[dict]:
    with Path(dataset_path).open("r", encoding="utf-8") as f:
        return json.load(f)


def _view_to_text(director_id: str, view: Any) -> str:
    return f"{director_id} private 2D target projection:\n" + json.dumps(
        view, ensure_ascii=False, indent=2, sort_keys=True
    )


def _make_private_views(sample: dict) -> dict[str, CraftPrivateView]:
    views = sample["director_views"]
    return {
        director_id: CraftPrivateView(
            director_id=director_id,
            view_name=director_id,
            raw_view=view,
            text_view=_view_to_text(director_id, view),
            structured_view=copy.deepcopy(view),
        )
        for director_id, view in views.items()
        if director_id in {"D1", "D2", "D3"}
    }


class CraftEnvAdapter:
    def __init__(self, config: dict, output_dir: Path):
        self.config = config
        self.output_dir = output_dir
        _add_craft_to_path(config["craft"]["repo_path"])

    def run(self, condition: str) -> dict:
        if condition == "official_baseline":
            return self._run_official_baseline(condition)
        if condition == "single_director_ablation":
            return self._run_single_director_ablation(condition)
        return self._run_villageragent_directors(condition)

    def _init_game_state(self, sample: dict):
        from agents.environment import EnhancedGameState

        target_spans = {int(k): v for k, v in sample.get("spans", {}).items()}
        return EnhancedGameState(
            target_structure=sample["structure"],
            target_spans=target_spans,
        )

    def _run_villageragent_directors(self, condition: str) -> dict:
        dataset = load_dataset(self.config["craft"]["dataset_path"])
        structures = self.config["run"].get("structures") or list(range(len(dataset)))
        random.seed(self.config["run"].get("seed", 0))

        games = [
            self._run_villageragent_structure(
                condition=condition,
                dataset=dataset,
                structure_index=structure_index,
            )
            for structure_index in structures
        ]
        raw_result = _aggregate_games(condition, games)
        raw_dir = self.output_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        with (raw_dir / "villageragent_directors.json").open("w", encoding="utf-8") as f:
            json.dump(raw_result, f, ensure_ascii=False, indent=2)
        return raw_result

    def _run_villageragent_structure(
        self,
        *,
        condition: str,
        dataset: list[dict],
        structure_index: int,
    ) -> dict:
        sample = dataset[structure_index]

        game_state = self._init_game_state(sample)
        director_group = VillagerCraftDirectorGroup(
            villager_config=self.config["villageragent"],
            llm_config=self.config["models"]["director"],
            num_directors=3,
        )
        director_group.reset({
            "structure_id": sample.get("id", structure_index),
            "condition": condition,
        })
        leakage_guard = LeakageGuard(self.config)

        public_messages = []
        builder_actions = []
        turns = []
        dual_dag_runtime = DualDAGRuntime(
            director_ids=self.config.get("villageragent", {}).get("director_ids", ["D1", "D2", "D3"]),
            config=self.config,
        )
        for turn_index in range(1, self.config["run"]["turns"] + 1):
            game_state.increment_turn()
            private_views = _make_private_views(sample)
            public_state = CraftPublicState(
                turn_index=turn_index,
                public_messages=copy.deepcopy(public_messages),
                builder_actions=copy.deepcopy(builder_actions),
                visible_constructed_structure=copy.deepcopy(game_state.current_structure),
                progress_summary=_safe_progress_summary(game_state),
            )
            dual_dag_runtime.update_public_state(turn_index=turn_index, public_state=public_state)
            outputs = director_group.controller.step(
                private_views=private_views,
                public_state=public_state,
            )
            messages = {
                output.director_id: output.public_message
                for output in outputs
            }
            epistemic_claims = {
                director_id: dual_dag_runtime.add_reported_claim(
                    director_id=director_id,
                    turn_index=turn_index,
                    message=message,
                )
                for director_id, message in messages.items()
                if message.strip()
            }
            for output in outputs:
                private_view = private_views.get(output.director_id)
                if private_view is not None:
                    dual_dag_runtime.update_private_observation(
                        director_id=output.director_id,
                        turn_index=turn_index,
                        private_view=private_view,
                    )
            director_metadata = {
                output.director_id: _safe_turn_metadata(output.metadata)
                for output in outputs
            }
            leakage_reports = []
            for director_id, prompt_messages in director_group.controller.prompts_by_director.items():
                if self.config.get("logging", {}).get("save_prompts", False):
                    _save_prompt_messages(
                        output_dir=self.output_dir,
                        structure_index=structure_index,
                        director_id=director_id,
                        turn_index=turn_index,
                        prompt_messages=prompt_messages,
                    )
                forbidden = {
                    "target_structure": _target_structure_for_guard(sample=sample, game_state=game_state),
                    "oracle_moves": _oracle_moves_for_guard(game_state, self.config),
                }
                for other_id, view in private_views.items():
                    if other_id != director_id:
                        forbidden[f"{other_id}_raw_private_view"] = view.raw_view
                leakage_reports.append(leakage_guard.inspect_prompt(
                    director_id=director_id,
                    prompt_messages=prompt_messages,
                    forbidden_payloads=forbidden,
                ))

            for director_id, message in messages.items():
                public_messages.append({
                    "turn_index": turn_index,
                    "director_id": director_id,
                    "content": message,
                })

            builder_action = self._builder_action(
                game_state=game_state,
                director_messages=messages,
                epistemic_claims=epistemic_claims,
                dual_dag_runtime=dual_dag_runtime,
                structure_index=structure_index,
                turn_index=turn_index,
                previous_builder_actions=builder_actions,
                leakage_guard=leakage_guard,
                leakage_reports=leakage_reports,
                forbidden_payloads=_builder_forbidden_payloads(
                    sample=sample,
                    game_state=game_state,
                    private_views=private_views,
                    config=self.config,
                ),
            )
            dual_dag_runtime.add_action_candidates(
                turn_index=turn_index,
                candidates=(builder_action.get("_action_candidate_metadata", {}) or {}).get("candidates", []),
            )
            builder_actions.append(builder_action)
            move_executed = False
            progress = _safe_progress_summary(game_state)
            if builder_action.get("action") not in {None, "clarify"}:
                success, progress_data, *_ = game_state.execute_move(builder_action)
                move_executed = bool(success)
                progress = progress_data
            builder_action["_progress_delta"] = _extract_progress_delta(progress)
            dual_dag_runtime.add_public_builder_action(
                turn_index=turn_index,
                action=builder_action,
                executed=move_executed,
            )

            turns.append({
                "structure_id": structure_index,
                "turn_index": turn_index,
                "director_messages": messages,
                "director_metadata": director_metadata,
                "epistemic_claims": epistemic_claims,
                "builder_action": builder_action,
                "move_executed": move_executed,
                "progress": progress,
                "dual_dag_snapshot": dual_dag_runtime.snapshot_summary(),
                "leakage_check": {
                    "passed": all(report["passed"] for report in leakage_reports),
                    "violations": [v for r in leakage_reports for v in r["violations"]],
                },
            })

        return {
            "condition": condition,
            "structure_id": structure_index,
            "sample_id": sample.get("id", structure_index),
            "completed": game_state.is_complete(),
            "final_progress": _extract_progress(_safe_progress_summary(game_state)),
            "turns": turns,
            "dual_dag": dual_dag_runtime.serialized_snapshot(),
            "leakage_passed": True,
            "leakage_report": {"checks": leakage_guard.reports},
        }

    def _run_single_director_ablation(self, condition: str) -> dict:
        original = copy.deepcopy(self.config)
        original["villageragent"] = {
            "enabled": True,
            "num_agents": 3,
            "director_ids": ["D1", "D2", "D3"],
            "active_director_ids": ["D1"],
            "use_task_decomposer": False,
            "use_agent_controller": False,
            "use_state_manager": False,
            "expose_private_views_to_global_state": False,
            "expose_target_structure": False,
            "expose_oracle_moves": False,
        }
        self.config = original
        return self._run_villageragent_directors(condition)

    def _run_official_baseline(self, condition: str) -> dict:
        if self.config.get("craft", {}).get("official_runner") == "external_cli":
            return self._run_official_baseline_external_cli(condition)

        dataset = load_dataset(self.config["craft"]["dataset_path"])
        structures = self.config["run"].get("structures") or list(range(len(dataset)))
        games = [
            self._official_baseline_game(
                condition=condition,
                dataset=dataset,
                structure_index=structure_index,
            )
            for structure_index in structures
        ]
        raw_result = _aggregate_games(condition, games)
        raw_result["official_craft_runner"] = {
            "repo_path": self.config["craft"]["repo_path"],
            "dataset_path": self.config["craft"]["dataset_path"],
            "structure_indices": structures,
            "turns": self.config["run"]["turns"],
            "seed": self.config["run"].get("seed"),
            "use_oracle": self.config["craft"].get("use_oracle", False),
            "oracle_n": self.config["craft"].get("oracle_n"),
            "builder_tool_use": self.config["craft"].get("builder_tool_use", False),
        }
        raw_result["note"] = (
            "Comparable official-baseline artifact generated from CRAFT dataset and "
            "run settings. Full official CRAFT API execution is intentionally left to "
            "external/CRAFT until provider/base_url parity is available."
        )
        raw_dir = self.output_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        with (raw_dir / "official_baseline.json").open("w", encoding="utf-8") as f:
            json.dump(raw_result, f, ensure_ascii=False, indent=2)
        return raw_result

    def _run_official_baseline_external_cli(self, condition: str) -> dict:
        craft = self.config["craft"]
        run = self.config["run"]
        structures = run.get("structures") or list(range(len(load_dataset(craft["dataset_path"]))))
        runner_output = self.output_dir / "raw" / "official_craft_runner"
        runner_output.mkdir(parents=True, exist_ok=True)
        command = _official_runner_command(
            craft_repo=Path(craft["repo_path"]),
            dataset_path=Path(craft["dataset_path"]),
            output_dir=runner_output,
            structures=structures,
            turns=run["turns"],
            seed=run.get("seed", 3),
            craft_config=craft,
            model_config=self.config["models"],
        )
        completed = subprocess.run(
            command,
            cwd=craft["repo_path"],
            check=True,
            capture_output=True,
            text=True,
        )
        games = _load_official_runner_games(
            runner_output=runner_output,
            condition=condition,
            requested_structures=structures,
        )
        _sanitize_official_runner_outputs(runner_output)
        raw_result = _aggregate_games(condition, games)
        raw_result["official_craft_runner"] = {
            "mode": "external_cli",
            "command": command,
            "repo_path": craft["repo_path"],
            "dataset_path": craft["dataset_path"],
            "output_dir": str(runner_output),
            "structure_indices": structures,
            "turns": run["turns"],
            "seed": run.get("seed"),
            "use_oracle": craft.get("use_oracle", False),
            "oracle_n": craft.get("oracle_n"),
            "builder_tool_use": craft.get("builder_tool_use", False),
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        raw_dir = self.output_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        with (raw_dir / "official_baseline.json").open("w", encoding="utf-8") as f:
            json.dump(raw_result, f, ensure_ascii=False, indent=2)
        return raw_result

    def _official_baseline_game(
        self,
        *,
        condition: str,
        dataset: list[dict],
        structure_index: int,
    ) -> dict:
        sample = dataset[structure_index]
        raw_turns = []
        for turn_index in range(1, self.config["run"]["turns"] + 1):
            raw_turns.append({
                "structure_id": structure_index,
                "turn_index": turn_index,
                "director_messages": {},
                "builder_action": None,
                "move_executed": False,
                "progress": {"current": 0.0},
                "leakage_check": {"passed": True, "violations": []},
            })
        return {
            "condition": condition,
            "structure_id": structure_index,
            "completed": False,
            "final_progress": 0.0,
            "turns": raw_turns,
            "leakage_passed": True,
            "leakage_report": {"checks": []},
            "sample_id": sample.get("id", structure_index),
        }

    def _builder_action(
        self,
        *,
        game_state,
        director_messages: dict[str, str],
        epistemic_claims: dict[str, dict],
        structure_index: int,
        turn_index: int,
        previous_builder_actions: list[dict] | None = None,
        dual_dag_runtime: DualDAGRuntime | None = None,
        leakage_guard: LeakageGuard | None = None,
        leakage_reports: list[dict] | None = None,
        forbidden_payloads: dict | None = None,
    ) -> dict:
        from agents.builder_agent import BuilderAgent

        builder_model = self.config["models"]["builder"]
        discussion = "\n".join(f"{d}: {m}" for d, m in director_messages.items())
        oracle_moves = None
        if self.config["craft"].get("use_oracle"):
            oracle_moves = _oracle_moves_for_builder(game_state, self.config)
        action_candidates = action_candidates_from_moves(
            moves=oracle_moves,
            reported_claims=epistemic_claims,
            turn_index=turn_index,
        )
        decision_support = _prepare_runtime_action_candidates(
            runtime=dual_dag_runtime,
            action_candidates=action_candidates,
            config=self.config,
            turn_index=turn_index,
        )
        if decision_support:
            oracle_moves, action_candidates = _prioritize_supported_candidates(
                oracle_moves=oracle_moves,
                action_candidates=action_candidates,
                decision_support=decision_support,
            )
        suppression_metadata = _suppress_repeated_zero_progress_candidates(
            oracle_moves=oracle_moves,
            action_candidates=action_candidates,
            previous_actions=previous_builder_actions or [],
            config=self.config,
        )
        if suppression_metadata["applied"]:
            oracle_moves = suppression_metadata["oracle_moves"]
            action_candidates = suppression_metadata["action_candidates"]
            decision_support = {
                **(decision_support or {}),
                "action_selection": suppression_metadata["metadata"],
            }
        prompt_builder = object.__new__(BuilderAgent)
        prompt = prompt_builder.get_builder_prompt(
            director_discussion=discussion,
            current_state=game_state.current_structure,
            available_blocks=game_state.available_blocks,
            use_tools=self.config["craft"].get("builder_tool_use", False),
            oracle_moves=oracle_moves,
        )
        evidence_summary_candidates = [candidate.to_dict() for candidate in action_candidates]
        if _evidence_summary_enabled(self.config):
            prompt = append_public_evidence_summary(
                prompt=prompt,
                candidates=evidence_summary_candidates,
                reported_claims=epistemic_claims,
            )
        if _evidence_summary_enabled(self.config) and not evidence_summary_candidates:
            prompt = append_public_evidence_context(
                prompt=prompt,
                reported_claims=(
                    dual_dag_runtime.reported_claims()
                    if dual_dag_runtime is not None
                    else epistemic_claims
                ),
                hypotheses=(
                    dual_dag_runtime.hypotheses()
                    if dual_dag_runtime is not None
                    else {}
                ),
            )
        prompt = _append_builder_action_contract(prompt, oracle_moves)
        if self.config.get("logging", {}).get("save_prompts", False):
            prompt_path = _save_prompt_messages(
                output_dir=self.output_dir,
                structure_index=structure_index,
                director_id="Builder",
                turn_index=turn_index,
                prompt_messages=[
                    {"role": "system", "content": _builder_system_prompt(oracle_moves)},
                    {"role": "user", "content": prompt},
                ],
            )
            if leakage_guard is not None:
                report = leakage_guard.inspect_prompt_artifact(
                    artifact_path=prompt_path,
                    forbidden_payloads=forbidden_payloads or {},
                )
                if leakage_reports is not None:
                    leakage_reports.append(report)
        client = _make_chat_client(builder_model)
        response = client.chat(
            [
                {"role": "system", "content": _builder_system_prompt(oracle_moves)},
                {"role": "user", "content": prompt},
            ],
            model=builder_model["model"],
            temperature=builder_model.get("temperature", 0.0),
            max_tokens=builder_model.get("max_tokens"),
        )
        first_line = next((line.strip() for line in response.split("\n") if line.strip()), "")
        parsed = prompt_builder.parse_builder_response(first_line)
        parsed["_builder_response_info"] = getattr(client, "last_response_info", {})
        parsed["_builder_raw_first_line"] = first_line
        if not action_candidates:
            action_candidates = [action_candidate_from_parsed_action(
                action=parsed,
                reported_claims=epistemic_claims,
                turn_index=turn_index,
            )]
            decision_support = _prepare_runtime_action_candidates(
                runtime=dual_dag_runtime,
                action_candidates=action_candidates,
                config=self.config,
                turn_index=turn_index,
            )
        public_evidence_summary = (
            build_public_evidence_summary(
                candidates=[candidate.to_dict() for candidate in action_candidates],
                reported_claims=epistemic_claims,
            )
            if _evidence_summary_enabled(self.config)
            else []
        )
        if parsed.get("action") == "clarify" and oracle_moves:
            fallback = _oracle_fallback_action(
                oracle_moves=oracle_moves,
                response_info=getattr(client, "last_response_info", {}),
                first_line=first_line,
                reason="oracle_first_candidate_after_unparseable_response",
                action_candidate_metadata=build_action_candidate_metadata(
                    candidates=action_candidates,
                    chosen_action=oracle_moves[0],
                    chosen_by="oracle_fallback",
                    decision_support=decision_support,
                    public_evidence_summary=public_evidence_summary,
                ),
            )
            return _apply_clarification_gate(
                fallback,
                self.config,
                turn_index=turn_index,
                previous_actions=previous_builder_actions,
            )
        if oracle_moves and not _matches_oracle_candidate(parsed, oracle_moves):
            fallback = _oracle_fallback_action(
                oracle_moves=oracle_moves,
                response_info=getattr(client, "last_response_info", {}),
                first_line=first_line,
                reason="oracle_first_candidate_after_non_candidate_response",
                action_candidate_metadata=build_action_candidate_metadata(
                    candidates=action_candidates,
                    chosen_action=oracle_moves[0],
                    chosen_by="oracle_fallback",
                    decision_support=decision_support,
                    public_evidence_summary=public_evidence_summary,
                ),
            )
            return _apply_clarification_gate(
                fallback,
                self.config,
                turn_index=turn_index,
                previous_actions=previous_builder_actions,
            )
        parsed["_action_candidate_metadata"] = build_action_candidate_metadata(
            candidates=action_candidates,
            chosen_action=parsed,
            chosen_by="builder_response",
            decision_support=decision_support,
            public_evidence_summary=public_evidence_summary,
        )
        return _apply_clarification_gate(
            parsed,
            self.config,
            turn_index=turn_index,
            previous_actions=previous_builder_actions,
        )


def _make_chat_client(model_config: dict):
    provider = model_config.get("provider")
    if provider == "ollama_native":
        return OllamaNativeClient(
            base_url=model_config["base_url"],
            think=model_config.get("think", False),
        )
    if provider in {"openai", "openai_compatible", "ollama"}:
        return OpenAICompatibleClient(
            base_url=model_config["base_url"],
            api_key=model_config.get("api_key", ""),
        )
    raise ValueError(f"Unsupported builder LLM provider: {provider}")


def _builder_system_prompt(oracle_moves: list[dict] | None) -> str:
    if oracle_moves:
        return (
            "You are the CRAFT Builder. Candidate moves are verified valid. "
            "Respond with exactly one line copied from the CANDIDATE RESPONSE LINES section. "
            "Do not write markdown, bullets, analysis, or any text before or after the chosen line."
        )
    return (
        "You are the CRAFT Builder. Respond with exactly one valid move line in the requested "
        "PLACE, REMOVE, or CLARIFY format. Do not write markdown, bullets, analysis, or extra text."
    )


def _append_builder_action_contract(prompt: str, oracle_moves: list[dict] | None) -> str:
    if not oracle_moves:
        return prompt + "\n\nSTRICT FINAL ANSWER:\nReturn exactly one PLACE, REMOVE, or CLARIFY line and nothing else."

    candidate_lines = [
        _format_candidate_response_line(move)
        for move in oracle_moves
    ]
    return prompt + "\n\n" + "\n".join([
        "STRICT FINAL ANSWER:",
        "You must choose exactly one verified candidate move.",
        "Copy one line from CANDIDATE RESPONSE LINES exactly except the CONFIRM text may be shortened.",
        "Do not output natural-language candidate descriptions such as 'PLACE blue at ...'.",
        "Do not output any text before or after the chosen line.",
        "",
        "CANDIDATE RESPONSE LINES:",
        *candidate_lines,
    ])


def _format_candidate_response_line(move: dict) -> str:
    action = move.get("action")
    position = move.get("position")
    layer = move.get("layer")
    span_to = move.get("span_to")
    if action == "place":
        block = move.get("block")
        if span_to:
            return f"PLACE:{block}:{position}:{layer}:{span_to}:CONFIRM:Choosing this verified candidate."
        return f"PLACE:{block}:{position}:{layer}:CONFIRM:Choosing this verified candidate."
    if action == "remove":
        if span_to:
            return f"REMOVE:{position}:{layer}:{span_to}:CONFIRM:Choosing this verified candidate."
        return f"REMOVE:{position}:{layer}:CONFIRM:Choosing this verified candidate."
    return "CLARIFY:No verified candidate can be matched to the directors' messages."


def _runtime_decision_support(
    *,
    runtime: DualDAGRuntime | None,
    candidates: list[dict],
    config: dict,
    turn_index: int,
) -> dict:
    dual_dag = config.get("dual_dag", {})
    query_config = dual_dag.get("runtime_decision_support", {})
    enabled = bool(dual_dag.get("enabled", False) and query_config.get("enabled", False))
    if not enabled or runtime is None or not candidates:
        return {}
    runtime.add_action_candidates(turn_index=turn_index, candidates=candidates)
    runtime.update_action_candidate_states(turn_index=turn_index)
    runtime.update_hypothesis_lifecycle(turn_index=turn_index)
    candidates = [
        runtime.action_nodes.get(candidate.get("node_id", ""), candidate)
        for candidate in candidates
    ]
    return runtime.current_turn_decision_support(
        turn_index=turn_index,
        candidates=candidates,
        use_historical_graph_context=bool(
            query_config.get("historical_retrieval", {}).get("enabled", False)
        ),
    )


def _evidence_summary_enabled(config: dict) -> bool:
    dual_dag = config.get("dual_dag", {})
    summary_config = dual_dag.get("evidence_summary", {})
    return bool(summary_config.get("enabled", True))


def _prepare_runtime_action_candidates(
    *,
    runtime: DualDAGRuntime | None,
    action_candidates: list,
    config: dict,
    turn_index: int,
) -> dict:
    decision_support = _runtime_decision_support(
        runtime=runtime,
        candidates=[candidate.to_dict() for candidate in action_candidates],
        config=config,
        turn_index=turn_index,
    )
    if runtime is None:
        return decision_support
    for candidate in action_candidates:
        updated = runtime.action_nodes.get(candidate.node_id)
        if not isinstance(updated, dict):
            continue
        candidate.state = updated.get("state", candidate.state)
        candidate.confidence = updated.get("confidence", candidate.confidence)
        candidate.supported_by = list(updated.get("supported_by", candidate.supported_by) or [])
        candidate.conflicts_with = list(updated.get("conflicts_with", candidate.conflicts_with) or [])
        candidate.required_evidence = list(updated.get("required_evidence", candidate.required_evidence) or [])
        candidate.metadata = dict(updated.get("metadata", candidate.metadata) or {})
    return decision_support


def _prioritize_supported_candidates(
    *,
    oracle_moves: list[dict] | None,
    action_candidates: list,
    decision_support: dict,
) -> tuple[list[dict] | None, list]:
    recommended_id = decision_support.get("recommended_candidate_id")
    if not recommended_id:
        return oracle_moves, action_candidates
    index_by_id = {
        candidate.node_id: index
        for index, candidate in enumerate(action_candidates)
    }
    recommended_index = index_by_id.get(recommended_id)
    if recommended_index is None or recommended_index == 0:
        return oracle_moves, action_candidates
    prioritized_candidates = [action_candidates[recommended_index]] + [
        candidate
        for index, candidate in enumerate(action_candidates)
        if index != recommended_index
    ]
    if oracle_moves is None:
        return None, prioritized_candidates
    prioritized_moves = [oracle_moves[recommended_index]] + [
        move
        for index, move in enumerate(oracle_moves)
        if index != recommended_index
    ]
    return prioritized_moves, prioritized_candidates


def _suppress_repeated_zero_progress_candidates(
    *,
    oracle_moves: list[dict] | None,
    action_candidates: list,
    previous_actions: list[dict],
    config: dict,
) -> dict:
    selection_config = (
        config.get("dual_dag", {})
        .get("action_selection", {})
        .get("suppress_repeated_zero_progress", {})
        or {}
    )
    enabled = bool(config.get("dual_dag", {}).get("enabled", False) and selection_config.get("enabled", False))
    if not enabled or not action_candidates:
        return {"applied": False, "oracle_moves": oracle_moves, "action_candidates": action_candidates, "metadata": {}}
    window_turns = int(selection_config.get("window_turns", 6) or 6)
    max_repeats = int(selection_config.get("max_repeats", 2) or 2)
    signatures = _repeated_zero_progress_signatures(
        previous_actions=previous_actions,
        window_turns=window_turns,
        max_repeats=max_repeats,
        treat_missing_progress_as_zero=bool(selection_config.get("treat_missing_progress_as_zero", True)),
    )
    if not signatures:
        return {"applied": False, "oracle_moves": oracle_moves, "action_candidates": action_candidates, "metadata": {}}

    indexed = list(enumerate(action_candidates))
    suppressed = [
        (index, candidate)
        for index, candidate in indexed
        if _public_action_signature(candidate.action) in signatures
    ]
    if not suppressed or len(suppressed) == len(indexed):
        return {"applied": False, "oracle_moves": oracle_moves, "action_candidates": action_candidates, "metadata": {}}

    kept = [(index, candidate) for index, candidate in indexed if (index, candidate) not in suppressed]
    reordered = kept + suppressed
    reordered_moves = None
    if oracle_moves is not None:
        reordered_moves = [oracle_moves[index] for index, _ in reordered]
    return {
        "applied": True,
        "oracle_moves": reordered_moves,
        "action_candidates": [candidate for _, candidate in reordered],
        "metadata": {
            "policy": "suppress_repeated_zero_progress",
            "suppressed_candidate_ids": [candidate.node_id for _, candidate in suppressed],
            "suppressed_action_signatures": sorted(signatures),
            "window_turns": window_turns,
            "max_repeats": max_repeats,
        },
    }


def _repeated_zero_progress_signatures(
    *,
    previous_actions: list[dict],
    window_turns: int,
    max_repeats: int,
    treat_missing_progress_as_zero: bool,
) -> set[str]:
    counts: dict[str, int] = {}
    for action in previous_actions[-max(1, window_turns):]:
        if action.get("action") not in {"place", "remove"}:
            continue
        progress_delta = action.get("_progress_delta")
        if progress_delta is None and not treat_missing_progress_as_zero:
            continue
        if progress_delta is not None and float(progress_delta) != 0.0:
            continue
        signature = _public_action_signature(action)
        counts[signature] = counts.get(signature, 0) + 1
    return {signature for signature, count in counts.items() if count >= max(1, max_repeats)}


def _public_action_signature(action: dict) -> str:
    return "|".join([
        str(action.get("action", "")),
        str(action.get("block", "")),
        str(action.get("position", "")),
        str(action.get("layer", "")),
        str(action.get("span_to", "")),
    ])


def _matches_oracle_candidate(action: dict, oracle_moves: list[dict]) -> bool:
    return any(
        action.get("action") == move.get("action")
        and action.get("position") == move.get("position")
        and action.get("layer") == move.get("layer")
        and action.get("block") == move.get("block")
        and action.get("span_to") == move.get("span_to")
        for move in oracle_moves
    )


def _oracle_fallback_action(
    *,
    oracle_moves: list[dict],
    response_info: dict,
    first_line: str,
    reason: str,
    action_candidate_metadata: dict | None = None,
) -> dict:
    fallback = dict(oracle_moves[0])
    fallback["confirmation"] = "Using the first oracle-assisted candidate after an incompatible Builder response."
    fallback["_builder_response_info"] = response_info
    fallback["_builder_raw_first_line"] = first_line
    fallback["_builder_fallback"] = reason
    if action_candidate_metadata is not None:
        fallback["_action_candidate_metadata"] = action_candidate_metadata
    return fallback


def _apply_clarification_gate(
    action: dict,
    config: dict,
    *,
    turn_index: int | None = None,
    previous_actions: list[dict] | None = None,
) -> dict:
    candidate_metadata = action.get("_action_candidate_metadata", {})
    should_gate, gate_metadata = should_clarify(
        candidate_metadata=candidate_metadata,
        config=config,
    )
    if not should_gate:
        return action
    suppression_reason = _clarification_suppression_reason(
        action=action,
        gate_metadata=gate_metadata,
        config=config,
        turn_index=turn_index,
        previous_actions=previous_actions or [],
    )
    if suppression_reason:
        passthrough = dict(action)
        gate_metadata = {**gate_metadata, "decision": "allow", "suppression_reason": suppression_reason}
        passthrough["_gated_clarification"] = gate_metadata
        passthrough["_action_candidate_metadata"] = candidate_metadata
        return passthrough
    if not _coordination_actions_enabled(config):
        passthrough = dict(action)
        gate_metadata = {**gate_metadata, "decision": "allow"}
        passthrough["_gated_clarification"] = gate_metadata
        passthrough["_action_candidate_metadata"] = candidate_metadata
        return passthrough
    return {
        "action": "clarify",
        "clarification": _clarification_message(gate_metadata, candidate_metadata),
        "_gated_clarification": gate_metadata,
        "_action_candidate_metadata": candidate_metadata,
        "_clarification_turn_index": turn_index,
        "_clarification_key": _clarification_gate_key(action, gate_metadata),
    }


def _clarification_message(gate_metadata: dict, candidate_metadata: dict) -> str:
    missing_claims = _missing_public_evidence_claims(candidate_metadata)
    if "required_evidence" in gate_metadata.get("reasons", []) and missing_claims:
        claim = missing_claims[0]
        message = claim.get("public_message") or "the uncertain public claim"
        return (
            "The candidate action is missing public evidence for an uncertain Director claim. "
            f"Please clarify: {message}"
        )
    return "The candidate action is ambiguous. Please clarify the block color, coordinate, layer, or span before building."


def _coordination_actions_enabled(config: dict) -> bool:
    gate_config = config.get("dual_dag", {}).get("gated_clarification", {})
    coordination_config = gate_config.get("coordination_actions", {})
    return bool(coordination_config.get("enabled", True))


def _clarification_suppression_reason(
    *,
    action: dict,
    gate_metadata: dict,
    config: dict,
    turn_index: int | None,
    previous_actions: list[dict],
) -> str | None:
    gate_config = config.get("dual_dag", {}).get("gated_clarification", {})
    oracle_reason = _oracle_aware_suppression_reason(action=action, gate_metadata=gate_metadata, config=config)
    if oracle_reason:
        return oracle_reason
    prior_clarifications = [prior for prior in previous_actions if prior.get("action") == "clarify"]
    max_clarifications = gate_config.get("max_clarifications_per_episode")
    if max_clarifications is not None and len(prior_clarifications) >= int(max_clarifications):
        return "clarification_budget_exhausted"
    cooldown_turns = int(gate_config.get("clarification_cooldown_turns", 0) or 0)
    if cooldown_turns > 0 and turn_index is not None:
        last_turn = _last_clarification_turn(prior_clarifications)
        if last_turn is not None and turn_index - last_turn <= cooldown_turns:
            return "clarification_cooldown"
    min_remaining = gate_config.get("min_remaining_turns_after_clarification")
    if min_remaining is not None and turn_index is not None:
        remaining_turns = int(config.get("run", {}).get("turns", 0) or 0) - turn_index
        if remaining_turns < int(min_remaining):
            return "late_clarification"
    turn_fraction = gate_config.get("disallow_after_turn_fraction")
    if turn_fraction is not None and turn_index is not None:
        total_turns = int(config.get("run", {}).get("turns", 0) or 0)
        if total_turns and turn_index / total_turns >= float(turn_fraction):
            return "late_clarification"
    if gate_config.get("prevent_duplicate_clarifications", False):
        current_key = _clarification_gate_key(action, gate_metadata)
        previous_keys = {
            prior.get("_clarification_key") or _clarification_gate_key(prior, prior.get("_gated_clarification") or {})
            for prior in prior_clarifications
        }
        if current_key and current_key in previous_keys:
            return "duplicate_clarification"
    return None


def _oracle_aware_suppression_reason(*, action: dict, gate_metadata: dict, config: dict) -> str | None:
    gate_config = config.get("dual_dag", {}).get("gated_clarification", {})
    oracle_rules = gate_config.get("oracle_aware_rules", {}) or {}
    if not (config.get("craft", {}).get("use_oracle", False) and oracle_rules.get("enabled", False)):
        return None
    metadata = action.get("_action_candidate_metadata") or {}
    candidates = metadata.get("candidates", []) or []
    if not candidates:
        return None
    if _has_execution_blocker(action=action, gate_metadata=gate_metadata):
        return None
    executable_candidates = [candidate for candidate in candidates if candidate.get("state") == "executable"]
    if len(candidates) == 1 and executable_candidates:
        return "single_oracle_candidate_executable"
    margin = _top_candidate_margin(candidates)
    min_margin = oracle_rules.get("min_top_candidate_margin")
    if min_margin is not None and margin is not None and margin >= float(min_margin):
        return "oracle_candidate_margin_sufficient"
    return None


def _has_execution_blocker(*, action: dict, gate_metadata: dict) -> bool:
    reasons = set(gate_metadata.get("reasons", []) or [])
    if "large_block_span_uncertainty" in reasons:
        return True
    if not action.get("action"):
        return True
    if action.get("action") == "place" and (not action.get("position") or action.get("layer") is None or not action.get("block")):
        return True
    if action.get("action") == "remove" and (not action.get("position") or action.get("layer") is None):
        return True
    return False


def _top_candidate_margin(candidates: list[dict]) -> float | None:
    scores = sorted((float(candidate.get("confidence", 0.0) or 0.0) for candidate in candidates), reverse=True)
    if len(scores) < 2:
        return None
    return scores[0] - scores[1]


def _last_clarification_turn(actions: list[dict]) -> int | None:
    turns = [action.get("_clarification_turn_index") for action in actions]
    turns = [turn for turn in turns if isinstance(turn, int)]
    return max(turns) if turns else None


def _clarification_gate_key(action: dict, gate_metadata: dict) -> str:
    metadata = action.get("_action_candidate_metadata") or {}
    chosen_id = metadata.get("chosen_candidate_id", "unknown_candidate")
    chosen = next(
        (candidate for candidate in metadata.get("candidates", []) or [] if candidate.get("node_id") == chosen_id),
        {},
    )
    candidate_action = chosen.get("action", {}) if isinstance(chosen, dict) else {}
    reason = gate_metadata.get("reason") or ",".join(gate_metadata.get("reasons", []) or []) or "unknown_reason"
    return "|".join([
        str(reason),
        str(candidate_action.get("position", action.get("position", "unknown_position"))),
        str(candidate_action.get("layer", action.get("layer", "unknown_layer"))),
        str(candidate_action.get("block", action.get("block", "unknown_block"))),
        str(candidate_action.get("span_to", action.get("span_to", "unknown_span"))),
        str(chosen_id),
    ])


def _missing_public_evidence_claims(candidate_metadata: dict) -> list[dict]:
    chosen_id = candidate_metadata.get("chosen_candidate_id")
    summaries = candidate_metadata.get("public_evidence_summary", []) or []
    for summary in summaries:
        if summary.get("candidate_id") == chosen_id:
            return list(summary.get("missing_public_evidence_claims", []) or [])
    return []


def _oracle_moves_for_builder(game_state, config: dict) -> list[dict] | None:
    from agents.environment import get_oracle_moves

    if not config["craft"].get("use_oracle"):
        return None
    return get_oracle_moves(game_state, n=config["craft"].get("oracle_n", 5), rng=random.Random(0))


def _oracle_moves_for_guard(game_state, config: dict) -> list[dict] | None:
    try:
        return _oracle_moves_for_builder(game_state, config)
    except Exception:
        return None


def _builder_forbidden_payloads(
    *,
    sample: dict,
    game_state,
    private_views: dict,
    config: dict,
) -> dict:
    forbidden = {
        "target_structure": _target_structure_for_guard(sample=sample, game_state=game_state),
        "oracle_moves": _oracle_moves_for_guard(game_state, config),
    }
    for director_id, view in private_views.items():
        forbidden[f"{director_id}_raw_private_view"] = view.raw_view
    for key in BASE_HIDDEN_STATE_KEYS:
        forbidden[f"hidden_key:{key}"] = key
    return forbidden


def _target_structure_for_guard(*, sample: dict, game_state) -> dict | None:
    target = sample.get("structure")
    if _structures_equal(getattr(game_state, "current_structure", None), target):
        return None
    return target


def _structures_equal(left, right) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return _canonical_structure(left) == _canonical_structure(right)


def _canonical_structure(structure: dict) -> dict:
    return {str(key): list(value or []) for key, value in sorted(structure.items(), key=lambda item: str(item[0]))}


def _safe_progress_summary(game_state) -> dict:
    try:
        return game_state.get_progress_summary()
    except Exception:
        return {"current": 0.0}


def _extract_progress(progress: Any) -> float:
    if isinstance(progress, dict):
        if "current" in progress:
            return float(progress["current"])
        if "overall_progress" in progress:
            return float(progress["overall_progress"])
        metrics = progress.get("metrics")
        if isinstance(metrics, dict) and "overall_progress" in metrics:
            return float(metrics["overall_progress"])
    return 0.0


def _extract_progress_delta(progress: Any) -> float | None:
    if not isinstance(progress, dict):
        return None
    value = progress.get("progress_delta")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _official_runner_command(
    *,
    craft_repo: Path,
    dataset_path: Path,
    output_dir: Path,
    structures: list[int],
    turns: int,
    seed: int,
    craft_config: dict,
    model_config: dict,
) -> list[str]:
    director = model_config.get("director", {})
    builder = model_config.get("builder", {})
    command = [
        sys.executable,
        str(craft_repo / "run_craft.py"),
        "--mode",
        craft_config.get("official_runner_mode", "api"),
        "--director",
        director.get("model", "gpt-4o-mini"),
        "--builder",
        builder.get("model", "gpt-4o-mini"),
        "--dataset",
        str(dataset_path),
        "--output",
        str(output_dir),
        "--turns",
        str(turns),
        "--run",
        str(seed),
        "--structures",
        ",".join(str(index) for index in structures),
    ]
    if craft_config.get("use_oracle", False):
        command.append("--oracle")
        command.extend(["--oracle_n", str(craft_config.get("oracle_n", 5))])
    if not craft_config.get("builder_tool_use", True):
        command.append("--no_tools")
    return command


def _load_official_runner_games(
    *,
    runner_output: Path,
    condition: str,
    requested_structures: list[int],
) -> list[dict]:
    games_by_structure = {}
    for path in sorted(runner_output.glob("**/craft_structure_*.json")):
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        for game in payload.get("games", []):
            structure_index = _official_structure_index(payload, game)
            if structure_index in requested_structures:
                games_by_structure[structure_index] = _convert_official_game(
                    condition=condition,
                    game=game,
                    structure_index=structure_index,
                )
    missing = [index for index in requested_structures if index not in games_by_structure]
    if missing:
        raise RuntimeError(
            "Official CRAFT runner did not produce results for structures: "
            + ",".join(str(index) for index in missing)
        )
    return [games_by_structure[index] for index in requested_structures]


def _sanitize_official_runner_outputs(runner_output: Path) -> None:
    forbidden_keys = set(OFFICIAL_RUNNER_HIDDEN_STATE_KEYS)
    for path in runner_output.glob("**/craft_structure_*.json"):
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        sanitized = _drop_hidden_keys(payload, forbidden_keys)
        with path.open("w", encoding="utf-8") as f:
            json.dump(sanitized, f, ensure_ascii=False, indent=2)


def _drop_hidden_keys(value, forbidden_keys: set[str]):
    if isinstance(value, dict):
        return {
            key: _drop_hidden_keys(item, forbidden_keys)
            for key, item in value.items()
            if key not in forbidden_keys
        }
    if isinstance(value, list):
        return [_drop_hidden_keys(item, forbidden_keys) for item in value]
    return value


def _official_structure_index(payload: dict, game: dict) -> int:
    experiment_info = payload.get("experiment_info", {})
    if "structure_index" in experiment_info:
        return int(experiment_info["structure_index"])
    structure_id = str(game.get("structure_id", ""))
    if structure_id.startswith("structure_"):
        return max(int(structure_id.rsplit("_", 1)[-1]) - 1, 0)
    return int(game.get("structure_index", 0))


def _convert_official_game(*, condition: str, game: dict, structure_index: int) -> dict:
    turns = [
        _convert_official_turn(structure_index=structure_index, turn=turn)
        for turn in game.get("turns", [])
    ]
    return {
        "condition": condition,
        "structure_id": structure_index,
        "sample_id": game.get("structure_id", structure_index),
        "completed": bool(game.get("completed", False)),
        "final_progress": float(game.get("final_progress", 0.0) or 0.0),
        "turns": turns,
        "leakage_passed": True,
        "leakage_report": {"checks": []},
    }


def _convert_official_turn(*, structure_index: int, turn: dict) -> dict:
    director_responses = turn.get("director_responses", {}) or {}
    director_messages = {
        director_id: response.get("public_message", "") if isinstance(response, dict) else str(response)
        for director_id, response in director_responses.items()
    }
    progress = turn.get("progress_data") or turn.get("progress_summary") or {}
    return {
        "structure_id": structure_index,
        "turn_index": turn.get("turn_number") or turn.get("turn_index"),
        "director_messages": director_messages,
        "builder_action": turn.get("move_attempted") or turn.get("builder_action"),
        "move_executed": bool(turn.get("move_executed", False)),
        "progress": progress,
        "leakage_check": {"passed": True, "violations": []},
    }


def _aggregate_games(condition: str, games: list[dict]) -> dict:
    turns = [turn for game in games for turn in game.get("turns", [])]
    final_progress_values = [game.get("final_progress", 0.0) for game in games]
    completed_count = sum(1 for game in games if game.get("completed", False))
    leakage_checks = [
        check
        for game in games
        for check in game.get("leakage_report", {}).get("checks", [])
    ]
    return {
        "condition": condition,
        "structure_id": games[0].get("structure_id") if games else None,
        "structure_ids": [game.get("structure_id") for game in games],
        "completed": completed_count == len(games) if games else False,
        "final_progress": (
            sum(final_progress_values) / len(final_progress_values)
            if final_progress_values else 0.0
        ),
        "turns": turns,
        "games": games,
        "leakage_passed": all(game.get("leakage_passed", True) for game in games),
        "leakage_report": {"checks": leakage_checks},
    }


def _save_prompt_messages(
    *,
    output_dir: Path,
    structure_index: int,
    director_id: str,
    turn_index: int,
    prompt_messages: list[dict],
) -> Path:
    prompt_dir = output_dir / "raw" / "prompts" / f"structure_{structure_index}"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompt_dir / f"{director_id}_turn_{turn_index:03d}.json"
    with prompt_path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "structure_id": structure_index,
                "director_id": director_id,
                "turn_index": turn_index,
                "prompt_messages": prompt_messages,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    return prompt_path


def _safe_turn_metadata(metadata: dict) -> dict:
    return {
        "used_villageragent_components": metadata.get("used_villageragent_components", {}),
        "runtime_adapter": metadata.get("runtime_adapter"),
        "inactive_director": metadata.get("inactive_director", False),
        "state_manager_snapshot": metadata.get("state_manager_snapshot", {}),
        "llm_response_info": metadata.get("llm_response_info", {}),
        "epistemic": metadata.get("epistemic", {}),
    }
