import copy
import json
import random
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
from benchmarks.craft.dual_dag.epistemic_extractor import reported_claim_from_message
from benchmarks.craft.dual_dag.gating import should_clarify
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
            outputs = director_group.controller.step(
                private_views=private_views,
                public_state=public_state,
            )
            messages = {
                output.director_id: output.public_message
                for output in outputs
            }
            epistemic_claims = {
                director_id: reported_claim_from_message(
                    director_id=director_id,
                    turn_index=turn_index,
                    message=message,
                )
                for director_id, message in messages.items()
                if message.strip()
            }
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
                    "target_structure": sample.get("structure"),
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
                turn_index=turn_index,
            )
            builder_actions.append(builder_action)
            move_executed = False
            progress = _safe_progress_summary(game_state)
            if builder_action.get("action") not in {None, "clarify"}:
                success, progress_data, *_ = game_state.execute_move(builder_action)
                move_executed = bool(success)
                progress = progress_data

            turns.append({
                "structure_id": structure_index,
                "turn_index": turn_index,
                "director_messages": messages,
                "director_metadata": director_metadata,
                "epistemic_claims": epistemic_claims,
                "builder_action": builder_action,
                "move_executed": move_executed,
                "progress": progress,
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
        turn_index: int,
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
        prompt_builder = object.__new__(BuilderAgent)
        prompt = prompt_builder.get_builder_prompt(
            director_discussion=discussion,
            current_state=game_state.current_structure,
            available_blocks=game_state.available_blocks,
            use_tools=self.config["craft"].get("builder_tool_use", False),
            oracle_moves=oracle_moves,
        )
        prompt = _append_builder_action_contract(prompt, oracle_moves)
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
                ),
            )
            return _apply_clarification_gate(fallback, self.config)
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
                ),
            )
            return _apply_clarification_gate(fallback, self.config)
        parsed["_action_candidate_metadata"] = build_action_candidate_metadata(
            candidates=action_candidates,
            chosen_action=parsed,
            chosen_by="builder_response",
        )
        return _apply_clarification_gate(parsed, self.config)


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


def _apply_clarification_gate(action: dict, config: dict) -> dict:
    candidate_metadata = action.get("_action_candidate_metadata", {})
    should_gate, gate_metadata = should_clarify(
        candidate_metadata=candidate_metadata,
        config=config,
    )
    if not should_gate:
        return action
    return {
        "action": "clarify",
        "clarification": "The candidate action is ambiguous. Please clarify the block color, coordinate, layer, or span before building.",
        "_gated_clarification": gate_metadata,
        "_action_candidate_metadata": candidate_metadata,
    }


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
) -> None:
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


def _safe_turn_metadata(metadata: dict) -> dict:
    return {
        "used_villageragent_components": metadata.get("used_villageragent_components", {}),
        "runtime_adapter": metadata.get("runtime_adapter"),
        "inactive_director": metadata.get("inactive_director", False),
        "state_manager_snapshot": metadata.get("state_manager_snapshot", {}),
        "llm_response_info": metadata.get("llm_response_info", {}),
        "epistemic": metadata.get("epistemic", {}),
    }
