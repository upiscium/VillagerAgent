import os
from type_define.graph import Graph, GraphState, Task
from pipeline.dual_dag_task_store import RuntimeTaskDAGStore
from pipeline.task_prompt import *
from pipeline.data_manager import DataManager
from pipeline.retriever import Retriever
from pipeline.runtime_events import NoOpRuntimeEventSink, safe_emit_runtime_event
from model.openai_models import (
    OpenAILanguageModel,
    ProviderCallCancellationError,
    ProviderCallTerminationError,
)
from pipeline.utils import *
from typing import Union
import json
import time
import logging
from contextlib import nullcontext
from functools import wraps
from pathlib import Path

from env.runtime_paths import RuntimePaths, atomic_write_json

TASK_MANAGER_WAIT_TIME = 1
PARTIAL_GRAPH_TASK_NUM = 5


class TaskManagerFeedbackInterruptedError(InterruptedError):
    """Adaptive task feedback stopped at the controller shutdown boundary."""

    def __init__(self, message, *, provider_termination_confirmed, diagnostics=None):
        super().__init__(message)
        self.provider_termination_confirmed = bool(provider_termination_confirmed)
        self.diagnostics = diagnostics or {}


class TaskManagerFeedbackOutcome:
    """Evidence that TaskManager feedback reached its mutation commit."""

    committed = True

    def __init__(self, *, ancillary_complete: bool = True):
        self.ancillary_complete = bool(ancillary_complete)


class TaskManagerFeedbackCommitError(RuntimeError):
    """Canonical feedback committed, but ancillary persistence failed."""

    committed = True


def _reset_status_after_task_management(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        self.status = TaskManager.running
        try:
            return method(self, *args, **kwargs)
        finally:
            self.status = TaskManager.idle

    return wrapped


class TaskManager:
    '''
    Task Manager is used to manage the task list and the task graph
    1. Init the task
    2. Generate the subtask list
    3. Construct the subtask graph
    4. Feedback the subtask status
    5. Update the task graph
    '''
    running = "running"
    idle = "idle"

    update_task: str = "update"
    merge_task: str = "merge"

    @staticmethod
    def _feedback_cancelled(cancellation_token) -> bool:
        if cancellation_token is None:
            return False
        if callable(cancellation_token):
            return bool(cancellation_token())
        is_set = getattr(cancellation_token, "is_set", None)
        if not callable(is_set):
            raise TypeError("cancellation_token must be callable or expose is_set()")
        return bool(is_set())

    @classmethod
    def _require_feedback_admission(cls, cancellation_token, *, phase: str) -> None:
        if cls._feedback_cancelled(cancellation_token):
            raise TaskManagerFeedbackInterruptedError(
                "TaskManager feedback was interrupted by controller shutdown",
                provider_termination_confirmed=True,
                diagnostics={"phase": phase},
            )

    def _feedback_model_call(
        self,
        *args,
        cancellation_token=None,
        model_admission_lock=None,
        **kwargs,
    ):
        self._require_feedback_admission(
            cancellation_token, phase="before_model_call",
        )
        if cancellation_token is not None and isinstance(
            self.llm, OpenAILanguageModel
        ):
            kwargs["cancellation_event"] = cancellation_token
            kwargs["model_admission_lock"] = model_admission_lock
        try:
            response = self.llm.few_shot_generate_thoughts(*args, **kwargs)
        except ProviderCallCancellationError as exc:
            raise TaskManagerFeedbackInterruptedError(
                "TaskManager model call was interrupted by controller shutdown",
                provider_termination_confirmed=exc.provider_termination_confirmed,
                diagnostics=exc.close_failure_diagnostics,
            ) from exc
        except ProviderCallTerminationError as exc:
            raise TaskManagerFeedbackInterruptedError(
                "TaskManager provider termination was not confirmed",
                provider_termination_confirmed=False,
                diagnostics={
                    "phase": "provider_timeout",
                    "error_type": type(exc).__name__,
                },
            ) from exc
        self._require_feedback_admission(
            cancellation_token, phase="after_model_call",
        )
        return response

    @staticmethod
    def _feedback_commit_context(commit_lock):
        return nullcontext() if commit_lock is None else commit_lock

    def _persist_feedback_artifacts(
        self,
        *,
        history,
        snapshot_source: str,
        cancellation_token=None,
        persistence_gate=None,
    ):
        def run(operation) -> bool:
            if persistence_gate is not None:
                return bool(persistence_gate(operation, cancellation_token))
            if self._feedback_cancelled(cancellation_token):
                return False
            operation()
            return True

        try:
            if history is not None and not run(
                lambda: self.update_history(*history)
            ):
                return TaskManagerFeedbackOutcome(ancillary_complete=False)
            if not run(
                lambda: self.checkpoint_runtime_state(raise_on_error=True)
            ):
                return TaskManagerFeedbackOutcome(ancillary_complete=False)
            if not run(lambda: self.emit_task_graph_snapshot(snapshot_source)):
                return TaskManagerFeedbackOutcome(ancillary_complete=False)
            time_str = time.strftime("%Y_%m_%d_%H_%M_%S_graph", time.localtime())
            if not run(
                lambda: self.graph.write_graph_to_md("img/" + time_str + ".md")
            ):
                return TaskManagerFeedbackOutcome(ancillary_complete=False)
            if not run(lambda: self.graph.write_graph_to_json("logs/")):
                return TaskManagerFeedbackOutcome(ancillary_complete=False)
        except BaseException as exc:
            raise TaskManagerFeedbackCommitError(
                "TaskManager feedback committed but artifact persistence failed"
            ) from exc
        return TaskManagerFeedbackOutcome()

    def __init__(self, silent:bool = False, method:str = "update", cache_enabled:bool = False, event_sink=None, history_output_dir: str | os.PathLike | None = None):
        if method not in ("update", "merge"):
            raise ValueError(
                f"Unsupported task manager method {method!r}; expected one of: update, merge"
            )
        self.llm = None
        self.dm:DataManager = None
        self.runtime_task_store = RuntimeTaskDAGStore()
        # Deprecated compatibility attribute. Use runtime_task_store.
        self.dual_dag_store = self.runtime_task_store
        self.graph:Graph = self.runtime_task_store.to_task_graph_projection()
        self.logger = init_logger("TaskManager", level= logging.WARNING ,dump=True, silent=silent)
        self.status = TaskManager.idle
        self.agent_describe = None
        self.retriever = Retriever()
        self.agent_list = []
        self.task_document = None
        self.method = method
        self.cache_enabled = cache_enabled
        self.task_description = None
        self.runtime_checkpoint = None
        self.event_sink = event_sink or NoOpRuntimeEventSink()
        self.history_output_dir = Path(history_output_dir) if history_output_dir is not None else RuntimePaths.legacy().run_result_dir("test")

        self.task_trace = []
        self.task_trace_description = []
        self.total_trace = []
        self.total_trace_description = []
        self.fail_trace = []
        self.fail_trace_description = []

        self.history = {"prompt": [], "response": []}

        self.manage_method = method


        # delete generated graph files when the runtime directories exist
        for directory in ("img", "logs"):
            if not os.path.isdir(directory):
                continue
            for file in os.listdir(directory):
                if "graph" in file:
                    os.remove(os.path.join(directory, file))
                
    def get_relevant_content_by_path(self, subtask_data: dict, query: [str]) -> list:
        # Initialize an empty dictionary to store the extracted information
        extracted_data = {}

        # Iterate over each path in the query list
        for path in query:
            # Split the path by '/' to get the individual components
            # Also, remove the initial '~' if present
            path_components = path.lstrip('~').split('/')
            # Remove empty strings that may result from splitting
            path_components = [comp for comp in path_components if comp]

            # Initialize a temporary variable to hold the current level of the subtask_data
            current_level = subtask_data

            # Traverse the subtask_data dictionary using the components of the path
            for component in path_components:
                # print(component, current_level)
                # print("")
                # Check if the component is a key in the current level of the dictionary
                if isinstance(current_level, dict) and component in current_level:
                    # If it is, go one level deeper
                    current_level = current_level[component]
                elif isinstance(current_level, list) and component.isdigit():
                    # If the current level is a list, and the component is a number,
                    # try to convert the component to an integer and use it as an index
                    index = int(component)
                    # If the index is within the range of the list, go one level deeper
                    if index < len(current_level):
                        current_level = current_level[index]
                    else:
                        # If the index is out of range, skip this path and continue with the next one
                        current_level = None
                        break
                else:
                    # If the component is not found, try to find a close match
                    close_match = None
                    if isinstance(current_level, dict):
                        for key in current_level.keys():
                            if key.startswith(component):
                                close_match = key
                                break
                    if close_match:
                        # If a close match is found, go one level deeper using the close match
                        current_level = current_level[close_match]
                    else:
                        # If no close match is found, skip this path and continue with the next one
                        current_level = None
                        break

                # After traversing, if the current_level is not the original subtask_data,
                # and it is not None, it means we have found relevant data,
                # so we add it to the extracted_data dictionary
                if current_level is not None and current_level is not subtask_data:
                    extracted_data[path] = current_level

        # Return the dictionary containing all the extracted information
        # self.logger.debug(f"Task {smart_truncate(subtask_data, max_length=4096)} \n {query} \nrelated check. \n{extracted_data}")
        # input("press any key to continue")
        data_list = []
        for data in extracted_data.values():
            if data not in data_list:
                data_list.append(data)
        return data_list

    def query_graph(self, task_list:list[Task] = None) -> Graph:
        '''
        Generate the graph of the task list. Transfer the task list to a graph
        - task_list: list of Task
        '''
        store = RuntimeTaskDAGStore()
        store.load_tasks_from_decomposition(task_list)
        return store.to_task_graph_projection()

    def set_task_list_from_decomposition(self, task_list: list[Task]) -> None:
        self.runtime_task_store.load_tasks_from_decomposition(task_list)
        self.sync_graph_from_dual_dag()
        self.checkpoint_runtime_state()
        self.emit_task_graph_snapshot("TaskManager.decomposition")

    def sync_dual_dag_from_graph(self) -> None:
        self.runtime_task_store.load_tasks_from_graph(self.graph)
        self.sync_graph_from_dual_dag()

    def sync_graph_from_dual_dag(self) -> None:
        self.graph = self.runtime_task_store.to_task_graph_projection()

    def mark_task_running(self, task: Task, assigned_agents: list[str]) -> None:
        self.runtime_task_store.mark_task_running(task.id, assigned_agents=assigned_agents)
        self.sync_graph_from_dual_dag()
        self.checkpoint_runtime_state()
        self.emit_runtime_event("task_status_changed", entity_id=task.id, source="TaskManager.mark_task_running", payload={"status": Task.running, "assigned_agents": assigned_agents})

    def mark_task_status(self, task_id: str, status: str, feedback=None) -> None:
        self.runtime_task_store.mark_task_status(task_id, status, feedback=feedback)
        self.sync_graph_from_dual_dag()
        self.checkpoint_runtime_state()
        self.emit_runtime_event("task_status_changed", entity_id=task_id, source="TaskManager.mark_task_status", payload={"status": status, "feedback": feedback})

    def emit_runtime_event(self, event_type: str, *, entity_id=None, source: str, payload=None) -> None:
        safe_emit_runtime_event(getattr(self, "event_sink", NoOpRuntimeEventSink()), event_type, entity_id=entity_id, source=source, payload=payload)

    def emit_task_graph_snapshot(self, source: str) -> None:
        self.emit_runtime_event("task_graph_snapshot", source=source, payload={"graph": self.runtime_task_store.snapshot()})

    def checkpoint_runtime_state(self, *, raise_on_error: bool = False) -> None:
        if not callable(self.runtime_checkpoint):
            return
        try:
            self.runtime_checkpoint()
        except Exception as exc:
            self.logger.warning(f"Failed to checkpoint runtime task state: {exc}")
            if raise_on_error:
                raise

    def update_history(self, system_prompt, user_prompt, response):
        if type(user_prompt) == str:
            user_prompt = [user_prompt]
        prompt = str(system_prompt) + "\n"
        for i in range(len(user_prompt)):
            prompt += user_prompt[i] + "\n"

        self.history["prompt"].append(prompt)
        self.history["response"].append(response)
        atomic_write_json(self.history_output_dir / "TM_history.json", self.history)

    '''
        Public API
    '''
    @_reset_status_after_task_management
    def init_task(self, description:str, document:dict = {}):
        # task append
        # query state
        # query experience
        if isinstance(self.llm, OpenAILanguageModel):
            # print(self.llm.api_base)
            pass
        self.logger.debug("="*20 + " Task Manager Init Task " + "="*20)
        
        self.task_document = document
        self.task_description = description
        # experience = self.dm.query_task_experience(task=Task(name=description, content=document))
        # env_description = self.dm.query_env()[0]
        env_description = self.dm.query_env_with_task(description) 
        # self.logger.debug(f"dm env_description: {env_description}")
        content = document

        # decompose the task to subtask DAG list
        if self.manage_method == "update":
            system_prompt = PART_DECOMPOSE_SYSTEM_PROMPT
            user_prompt = format_string(PART_DECOMPOSE_USER_PROMPT, {"task": {"description": description, 
                                                                     "meta-data": content},
                                                            "env": env_description,
                                                            "num": len(self.agent_list)})
        elif self.manage_method == "merge":
            system_prompt = DECOMPOSE_SYSTEM_PROMPT
            user_prompt = format_string(DECOMPOSE_USER_PROMPT, {"task": {"description": description, 
                                                                        "meta-data": content},
                                                                "env": env_description})
        else:
            self.logger.error("Task Manager Method Error.")
            raise ValueError(
                f"Unsupported task manager method {self.manage_method!r}; expected one of: update, merge"
            )
        # self.logger.warning("TM DEBUG:")
        # self.logger.warning(system_prompt)
        self.logger.warning(user_prompt)
        response = self.llm.few_shot_generate_thoughts(system_prompt, user_prompt, cache_enabled=self.cache_enabled, json_check=True,
                                                       check_tags=["description", "milestones", "assigned agents"])
        self.update_history(system_prompt, user_prompt, response)
        result = extract_info(response, guard_keys=["description", "milestones"])
        omit_keys = [("assigned agents", "list"), ("required subtasks", "list"), ("retrieval paths", "list")]
        result = self.fill_keys_omit(
            result,
            omit_keys,
            required_keys={"assigned agents"} if self.manage_method == "update" else set(),
        )
        if self.manage_method == "update":
            result = self.fill_agents(result, self.agent_list)
        self.logger.warning(result)

        subtask_list = []
        for subtask_data in result:
            sub_content = self.get_relevant_content_by_path({"description": description, 
                                                                     "meta-data": content}, query=subtask_data["retrieval paths"])
            subtask = Task(name=subtask_data["description"], content=sub_content) 
            subtask.description = subtask_data["description"]
            subtask.parent_task_list = [Task(name=description, content=document)]
            subtask.goal = "omit"
            subtask.criticism = "omit"
            subtask.milestones = subtask_data["milestones"]
            if self.manage_method == "update":
                subtask.candidate_list = subtask_data["assigned agents"]
                subtask.number = len(subtask_data["assigned agents"])
                subtask._candidate_agents_explicit = True
                subtask._candidate_agent_count_exact = True
            else:
                subtask.candidate_list = subtask_data["candidate list"]
                subtask.number = subtask_data["minimum required agents"]
                subtask._candidate_agents_explicit = True
            subtask._pre_idxs = list(subtask_data["required subtasks"])
            subtask._pre_idxs_explicit = True
            subtask_list.append(subtask)

        self.set_task_list_from_decomposition(subtask_list)

        time_str = time.strftime("%Y_%m_%d_%H_%M_%S_graph", time.localtime())
        
        self.graph.write_graph_to_md("img/" + time_str + ".md")
        # input("press any key to continue")
        self.graph.write_graph_to_json("logs/")

    def query_subtask_list(self) -> [Task]:
        '''
        Generate the subtask list of the current task
        1. If the task is not in the graph, return empty list
        2. If the task is in the graph, return the subtask list
        3. If the task is in the graph and the task is completed, return empty list
        '''

        # self.logger.debug("="*20 + " Task Manager Support Open Task " + "="*20)
        while self.status == TaskManager.running:
            time.sleep(TASK_MANAGER_WAIT_TIME)


        task_list = self.runtime_task_store.query_open_tasks()
        self.sync_graph_from_dual_dag()
        return task_list

    def query_runnable_subtasks(self, free_agent_names: list[str]) -> list[Task]:
        while self.status == TaskManager.running:
            time.sleep(TASK_MANAGER_WAIT_TIME)

        task_list = self.runtime_task_store.query_runnable_tasks(free_agent_names=free_agent_names)
        self.sync_graph_from_dual_dag()
        return task_list


    def get_graph_strategy(
        self,
        task: Task,
        *,
        cancellation_token=None,
        model_admission_lock=None,
        defer_history: bool = False,
    ) -> {str: Union[str, int, list]}:
        '''
        This function is used to get the strategy of the task in merge method

        '''


        env_description = self.dm.query_env_with_task(task.description)

        task_description = self.graph.get_graph_status_with_id()

        current_task_description = task.analyze_json()

        format_data = {
                        "task_description": task_description, 
                        "current_task": current_task_description, 
                        "env": env_description, 
                        "agent_state": [self.dm.query_history(agent.name) for agent in self.agent_list], 
                       }
        strategy_system_prompt = STRATEGY_SYSTEM_PROMPT
        strategy_user_prompt = format_string(STRATEGY_USER_PROMPT, format_data)
        # self.logger.warning("TM STRATEGY DEBUG:")
        # self.logger.warning(strategy_system_prompt)
        # self.logger.warning(strategy_user_prompt)
        response = self._feedback_model_call(strategy_system_prompt, strategy_user_prompt, cache_enabled=self.cache_enabled, json_check=True,
                                                       cancellation_token=cancellation_token,
                                                       model_admission_lock=model_admission_lock,
                                                    #    api_model="gpt-4-1106-preview",
                                                    #    check_tags=["reasoning", "strategy", "info"]
                                                       
                                                       )
        result = extract_info(response)[0]
        if defer_history:
            return result, (strategy_system_prompt, strategy_user_prompt, response)
        self.update_history(strategy_system_prompt, strategy_user_prompt, response)
        
        # self.logger.warning(strategy_user_prompt)
        self.logger.warning(response)

        self.logger.debug(f"new strategy is {result}")

        return result
    
    def fill_agents(self, result:[dict], agents:list):
        self.logger.debug(f"fill agents:")
        canonical_by_casefold = {}
        for agent in agents:
            folded = agent.name.casefold()
            if folded in canonical_by_casefold and canonical_by_casefold[folded] != agent.name:
                raise ValueError(
                    f"Ambiguous agent names {canonical_by_casefold[folded]!r} and {agent.name!r}"
                )
            canonical_by_casefold[folded] = agent.name
        for res in result:
            if "assigned agents" not in res:
                raise ValueError(
                    f'Task {res.get("description", "")!r} must provide assigned agents'
                )
            if not isinstance(res["assigned agents"], list):
                raise ValueError(
                    f'Task {res.get("description", "")!r} assigned agents must be a list'
                )
            if not res["assigned agents"]:
                raise ValueError(
                    f'Task {res.get("description", "")!r} must assign at least one known agent'
                )
            assigned_agents = []
            for agent_name in res["assigned agents"]:
                if not isinstance(agent_name, str) or agent_name.casefold() not in canonical_by_casefold:
                    raise ValueError(
                        f'Unknown assigned agent {agent_name!r} for task {res.get("description", "")!r}'
                    )
                canonical_name = canonical_by_casefold[agent_name.casefold()]
                if canonical_name in assigned_agents:
                    raise ValueError(
                        f'Duplicate assigned agent {agent_name!r} for task {res.get("description", "")!r}'
                    )
                assigned_agents.append(canonical_name)
            res["assigned agents"] = assigned_agents
        self.logger.debug(f"fill agents: {result}")
        return result

    def fill_keys_omit(self, result:[dict], keys:list, required_keys:set[str] | None = None):
        required_keys = required_keys or set()
        def normalize_key(value):
            return " ".join(str(value).casefold().replace("_", " ").replace("-", " ").split())

        for res in result:
            for key in keys:
                if key[0] not in res.keys():
                    # 从keys中找到最相近的key， 相似度大于0.8
                    similar_key = None
                    similar_score = 0
                    for k in res.keys():
                        similar_score = 0
                        if normalize_key(k) == normalize_key(key[0]):
                            similar_score = 1
                            similar_key = k
                    if similar_score > 0.8:
                        print(f"match key {key[0]} to {similar_key}")
                        res[key[0]] = res[similar_key]

                    else:
                        if key[0] in required_keys:
                            raise ValueError(
                                f'Task {res.get("description", "")!r} missing required field: {key[0]}'
                            )
                        if key[1] == "dict":
                            res[key[0]] = {}
                        elif key[1] == "list":
                            res[key[0]] = []
                        elif key[1] == "str":
                            res[key[0]] = ""
                        elif key[1] == "int" or key[1] == "float":
                            res[key[0]] = 0
                        else:
                            res[key[0]] = None
        return result

    
    def feedback_task(
        self,
        task: Task,
        cancellation_token=None,
        commit_lock=None,
        persistence_gate=None,
    ):
        # self.logger.debug("="*20 + " Task Manager Handle Feedback " + "="*20)
        # self.logger.warning("open task list:")
        # self.logger.warning("=" * 40)
        self.status = TaskManager.running
        try:
            self._require_feedback_admission(
                cancellation_token, phase="feedback_start",
            )
            if type(task) != Task:
                self.logger.error("Task type error.")
                return TaskManagerFeedbackOutcome()

            with self._feedback_commit_context(commit_lock):
                self._require_feedback_admission(
                    cancellation_token, phase="trace_commit",
                )
                self.add_task_to_trace()

            if self.runtime_task_store.terminal_state() == GraphState.RUNNING:
                return TaskManagerFeedbackOutcome()

            if task.status == Task.unknown or task.status == Task.running:
                self.logger.error("Should not feedback unknown or running task.")
                return TaskManagerFeedbackOutcome()

            if self.manage_method == "update":
                return self.update_task(
                    task,
                    cancellation_token=cancellation_token,
                    commit_lock=commit_lock,
                    persistence_gate=persistence_gate,
                )
            elif self.manage_method == "merge":
                return self.merge_task(
                    task,
                    cancellation_token=cancellation_token,
                    commit_lock=commit_lock,
                    persistence_gate=persistence_gate,
                )
            else:
                self.logger.error("Task Manager Method Error.")
                raise ValueError(
                    f"Unsupported task manager method {self.manage_method!r}; expected one of: update, merge"
                )
        finally:
            self.status = TaskManager.idle

    def merge_task(
        self,
        task: Task,
        cancellation_token=None,
        commit_lock=None,
        persistence_gate=None,
    ):
        if cancellation_token is None and commit_lock is None:
            result = self.get_graph_strategy(task)
            history = None
        else:
            result, history = self.get_graph_strategy(
                task,
                cancellation_token=cancellation_token,
                model_admission_lock=commit_lock,
                defer_history=True,
            )
        strategy = result["strategy"]
        prepared_subtasks = None
        prepared_origin_task_id = None
        if strategy == "decompose":
            prepared_origin_task_id = self.runtime_task_store.task_id_at(
                int(result["origin-id"]) - 1
            )
            origin_task = self.runtime_task_store.get_task(prepared_origin_task_id)
            prepared_subtasks = []
            for subtask_data in result["subtasks"]:
                sub_content = self.get_relevant_content_by_path(
                    origin_task.analyze_json(), subtask_data["retrieval paths"]
                )
                subtask = Task(name=subtask_data["description"], content=sub_content)
                subtask.description = subtask_data["description"]
                subtask.parent_task_list.append(origin_task)
                subtask.goal = "omit"
                subtask.criticism = "omit"
                subtask.milestones = subtask_data["milestones"]
                subtask.candidate_list = subtask_data["candidate list"]
                subtask.number = int(subtask_data["minimum required agents"])
                subtask._pre_idxs = list(subtask_data["required subtasks"])
                subtask._pre_idxs_explicit = True
                prepared_subtasks.append(subtask)

        with self._feedback_commit_context(commit_lock):
            self._require_feedback_admission(
                cancellation_token, phase="merge_commit",
            )
            if strategy == "replan":
                origin_task_id = self.runtime_task_store.task_id_at(int(result["origin-id"]) - 1)
                origin_task = self.runtime_task_store.get_task(origin_task_id)
                replan_task = Task(name=result["description"], content=origin_task.content)
                replan_task.milestones = result["milestones"]
                self.runtime_task_store.replace_task(origin_task_id, replan_task)
            elif strategy == "decompose":
                self.runtime_task_store.replace_task_with_subgraph(
                    prepared_origin_task_id, prepared_subtasks,
                )
            elif strategy == "move":
                origin_task_id = self.runtime_task_store.task_id_at(int(result["origin-id"]) - 1)
                predecessor_id = self.runtime_task_store.task_id_at(int(result["new-id"]) - 1)
                self.runtime_task_store.move_task_after(origin_task_id, predecessor_id)
            elif strategy == "insert":
                new_task = Task(name=result["description"], content=task.content)
                new_task.milestones = result["milestones"]
                predecessor_id = self.runtime_task_store.task_id_at(int(result["insert-id"]) - 1)
                self.runtime_task_store.insert_task_after(predecessor_id, new_task)
            elif strategy == "delete":
                delete_task_id = self.runtime_task_store.task_id_at(int(result["delete-id"]) - 1)
                self.runtime_task_store.remove_task(delete_task_id)
            else:
                self.logger.error("Task status error.")
                return TaskManagerFeedbackOutcome()

            self.sync_graph_from_dual_dag()
        if self._feedback_cancelled(cancellation_token):
            return TaskManagerFeedbackOutcome(ancillary_complete=False)
        return self._persist_feedback_artifacts(
            history=history,
            snapshot_source=f"TaskManager.merge_task.{strategy}",
            cancellation_token=cancellation_token,
            persistence_gate=persistence_gate,
        )

    def trace_format(self, task:Task):
        # generate the trace format
        template = "{{agent}} execute task {{task}} and feedback: {{status}}"
        return format_string(template, {"agent": task._agent, "task": task.description, "status": "".join(task._summary[1:])})

    def add_task_to_trace(self):
        # fail trace append
        self.fail_trace = []
        self.fail_trace_description = []

        for task in self.graph.vertex:
            if task.status == Task.success and task not in self.task_trace:
                self.task_trace.append(task)
                self.task_trace_description.append(self.trace_format(task))
            elif task.status == Task.running and task not in self.task_trace:
                self.task_trace.append(task)
                self.task_trace_description.append(self.trace_format(task))
            
            if (task.status == Task.failure or task.status == Task.success) and task not in self.total_trace:
                self.total_trace.append(task)
                self.total_trace_description.append(self.trace_format(task))
                
                if task.status == Task.failure and task not in self.fail_trace:
                    self.fail_trace.append(task)
                    self.fail_trace_description.append(self.trace_format(task))
            
        # delete the failure task from the trace
        for idx, t in enumerate(self.task_trace):
            if t.status == Task.failure:
                self.task_trace.pop(idx)
                self.task_trace_description.pop(idx)


    @_reset_status_after_task_management
    def update_task(
        self,
        task: Task,
        cancellation_token=None,
        commit_lock=None,
        persistence_gate=None,
    ):
        # task append
        # query state
        # query experience

        if isinstance(self.llm, OpenAILanguageModel):
            pass
            # print(self.llm.api_base)
        self.logger.debug("="*20 + " Task Manager Update Task " + "="*20)
        self.logger.debug(f"trace: {self.task_trace_description}")
        self.logger.debug("="*20 + " Task Manager Update Task " + "="*20)
        self.logger.debug(f"total trace: {self.total_trace_description}")
        self.logger.debug("="*20 + " Task Manager Update Task " + "="*20)        
        # experience = self.dm.query_task_experience(task=Task(name=self.task_description, content=self.task_document))
        # # env_description = self.dm.query_env()[0]
        env_description = self.dm.query_env_with_task(self.task_description) 
        # self.logger.debug(f"dm env_description: {env_description}")

        # decompose the task to subtask DAG list
        system_prompt = REDECOMPOSE_SYSTEM_PROMPT
        user_prompt = format_string(REDECOMPOSE_USER_PROMPT, {"task": {"description": self.task_description, 
                                                                     "meta-data": self.task_document},
                                                            "env": env_description, 
                                                            "agent_state": [self.dm.query_history(agent.name) for agent in self.agent_list], 
                                                            "failure_previous_subtask": self.fail_trace_description,
                                                            "success_previous_subtask": self.task_trace_description,
                                                            "num": len(self.agent_list)})
    


        # self.logger.warning("TM DEBUG:")
        # self.logger.warning(system_prompt)
        self.logger.warning(user_prompt)
        response = self._feedback_model_call(
            system_prompt,
            user_prompt,
            cache_enabled=False,
            json_check=True,
            check_tags=["description", "milestones", "assigned agents"],
            cancellation_token=cancellation_token,
            model_admission_lock=commit_lock,
        )
        result = extract_info(response, guard_keys=["description", "milestones"])
        omit_keys = [("assigned agents", "list"), ("required subtasks", "list"), ("retrieval paths", "list")]
        result = self.fill_keys_omit(
            result,
            omit_keys,
            required_keys={"assigned agents"},
        )
        result = self.fill_agents(result, self.agent_list)
        self.logger.warning(result)

        subtask_list = []
        for subtask_data in result:
            sub_content = self.get_relevant_content_by_path({"description": self.task_description, 
                                                                     "meta-data": self.task_document}, query=subtask_data["retrieval paths"])
            subtask = Task(name=subtask_data["description"], content=sub_content) 
            subtask.description = subtask_data["description"]
            subtask.parent_task_list = [Task(name=self.task_description, content=self.task_document)]
            subtask.goal = "omit"
            subtask.criticism = "omit"
            subtask.milestones = subtask_data["milestones"]
            subtask.candidate_list = subtask_data["assigned agents"]
            subtask.number = len(subtask_data["assigned agents"])
            subtask._candidate_agents_explicit = True
            subtask._candidate_agent_count_exact = True
            subtask._pre_idxs = list(subtask_data["required subtasks"])
            subtask._pre_idxs_explicit = True
            subtask_list.append(subtask)

        with self._feedback_commit_context(commit_lock):
            self._require_feedback_admission(
                cancellation_token, phase="update_commit",
            )
            self.runtime_task_store.load_tasks_from_decomposition(subtask_list)
            self.sync_graph_from_dual_dag()
        if self._feedback_cancelled(cancellation_token):
            return TaskManagerFeedbackOutcome(ancillary_complete=False)
        return self._persist_feedback_artifacts(
            history=(system_prompt, user_prompt, response),
            snapshot_source="TaskManager.decomposition",
            cancellation_token=cancellation_token,
            persistence_gate=persistence_gate,
        )
