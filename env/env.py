from env.minecraft_client import (
    Agent,
    check_agent_cancellation,
    MinecraftActionLogError,
    MinecraftBridgeCleanupError,
)
from contextlib import contextmanager
from copy import copy
from functools import wraps
import traceback
import names
import subprocess
import json
import time
import os
from env.utils import init_logger
import logging
import inspect
from pathlib import Path
from langchain_core.pydantic_v1 import BaseModel

from env.runtime_paths import RuntimePaths, atomic_write_json, read_json_artifact
from env.runtime_execution import RuntimeExecution


LOAD_WAIT_SECONDS = 160


class env_type:
    none = -1
    construction = 0
    farming = 1
    puzzle = 2
    auto = 3

    meta = 10
    gen = 13

class VillagerBench:
    '''
    VillagerBench is the environment for the Minecraft task
    
    Args:
    - env_type: int, the type of the environment, 0 for construction, 1 for farming, 2 for puzzle, -1 for none (this is for pure agent environment, no judger will be launched)
    - task_id: int, the id of the task, different task_id means different task in the same scenario
    - dig_needed: bool, whether the agent need to dig the block
    - host: str, the host of the minecraft server
    - port: int, the port of the minecraft server default 25565
    - max_task_num: int, the max task number for the puzzle task
    - task_name: str, the name of the task
    - _virtual_debug: bool, whether the environment is in virtual debug mode
    '''
    def __init__(self, env_type, task_id: int, dig_needed: bool, host: str = "0.0.0.0", port: int = 25565, max_task_num: int = 1, task_name: str = "test", _virtual_debug: bool = False, runtime_paths: RuntimePaths | None = None, runtime_execution=None):
        self.env_type = env_type
        self.task_id = task_id
        self.host = host
        self.port = port
        self.task_name = task_name
        self.runtime_paths = runtime_paths or RuntimePaths.legacy()
        self.runtime_execution = runtime_execution or RuntimeExecution.resolve()
        self.runtime_paths.ensure_directories()
        self._invalid_status_reads = 0
        self.agent_pool = []
        self.log = {}
        self.reset_token()
        self.running = False
        self.bridge_cleanup_result = None
        self.bridge_cleanup_error = None
        self._virtual_debug = _virtual_debug
        self.logger = init_logger(name="Env", level=logging.DEBUG)
        self.max_task_num = max_task_num  # For puzzle
        self.dig_needed = dig_needed  # For construction
        self.launch_time = None
        self.langchain_model = ""
        self.base_port = 5000
        self.op_path = ""
        self.meta_diagnostics_dir = None
        self._tool_action_enter = lambda: None
        self._tool_action_exit = lambda: None
        self._eac_runtime = None
        atomic_write_json(self.runtime_paths.score, {})
        atomic_write_json(self.runtime_paths.action_log, {})
        atomic_write_json(self.runtime_paths.llm_inference, {"time": 0})
        atomic_write_json(self.runtime_paths.state, {"state": "idle"})
        
        # 删除之前的log
        if self.runtime_paths.logs_dir.exists():
            for file_path in self.runtime_paths.logs_dir.iterdir():
                if not file_path.is_file():
                    continue
                for _ in range(3):  # 尝试3次
                    try:
                        file_path.unlink()
                        break  # 成功删除，跳出循环
                    except Exception as e:
                        print(f"删除失败：{e}")
                        time.sleep(1)  # 等待1秒再次尝试
                else:
                    print(f"无法删除文件 {file_path}，可能仍然被锁定。")

    def _paths(self) -> RuntimePaths:
        return getattr(self, "runtime_paths", RuntimePaths.legacy())
          
    @contextmanager
    def run(self, server_debug: bool = False, fast_api=False):
        self.runtime_failure_chain = None
        self.runtime_cleanup_failure = None
        primary_error = None
        primary_traceback = None
        try:
            if not self._virtual_debug:
                self.launch(debug=server_debug, fast_api=fast_api)
                self.logger.info(f"[env launched at {self.host}]")
            else:
                self.logger.info("[virtual debug mode, env not launched]")
            self.launch_time = time.time()
            yield
        except BaseException as e:
            tb = traceback.format_exc()
            self.logger.error(f"Exception occurred: {e}\n{tb}")
            primary_error = e
            primary_traceback = e.__traceback__

        cleanup_error = None
        try:
            self.stop()
        except Exception as error:
            cleanup_error = error
        finally:
            paths = self._paths()
            state_result = read_json_artifact(paths.state)
            if state_result.state == "valid" and isinstance(state_result.value, dict):
                state = state_result.value
                state["state"] = "idle"
                atomic_write_json(paths.state, state)
            if paths.env_cache.exists():
                atomic_write_json(paths.env_cache, [])

        if primary_error is not None:
            if isinstance(cleanup_error, Exception):
                cleanup_failure = {
                    "error_type": type(cleanup_error).__name__,
                }
                if isinstance(cleanup_error, MinecraftBridgeCleanupError):
                    cleanup_failure["cleanup_result"] = dict(
                        cleanup_error.cleanup_result
                    )
                self.runtime_cleanup_failure = cleanup_failure
                self.runtime_failure_chain = {
                    "primary_failure": {
                        "error_type": type(primary_error).__name__,
                    },
                    "cleanup_failure": cleanup_failure,
                }
                try:
                    setattr(primary_error, "cleanup_error", cleanup_error)
                    setattr(primary_error, "cleanup_failure", cleanup_failure)
                except (AttributeError, TypeError):
                    pass
                raise primary_error.with_traceback(primary_traceback) from cleanup_error
            raise primary_error.with_traceback(primary_traceback)
        if cleanup_error is not None:
            if isinstance(cleanup_error, Exception):
                cleanup_failure = {
                    "error_type": type(cleanup_error).__name__,
                }
                if isinstance(cleanup_error, MinecraftBridgeCleanupError):
                    cleanup_failure["cleanup_result"] = dict(
                        cleanup_error.cleanup_result
                    )
                self.runtime_cleanup_failure = cleanup_failure
                self.runtime_failure_chain = {
                    "primary_failure": None,
                    "cleanup_failure": cleanup_failure,
                }
            raise cleanup_error

    def stop(self):
        if self.bridge_cleanup_result is not None:
            return self.bridge_cleanup_result
        if not self.running:
            return Agent.empty_bridge_cleanup_result()
        try:
            self.bridge_cleanup_result = Agent.kill()
            return self.bridge_cleanup_result
        except MinecraftBridgeCleanupError as exc:
            self.bridge_cleanup_result = exc.cleanup_result
            self.bridge_cleanup_error = exc
            raise
        finally:
            self.running = False

    def cancel_active_movements(self, actor_names=None, *, reason="controller_shutdown",
                                timeout_seconds=None):
        return Agent.cancel_active_movements(
            actor_names, reason=reason, total_timeout_seconds=timeout_seconds,
        )

    def virtual_env(name: str):
        env = {
            "I_held_item": {
                "spruce_planks": 1
            },
            "sign": "text",
            "blocks": [
                {
                    "spruce_planks": [
                        -3,
                        -60,
                        0
                    ]
                }
            ],
            "equipment": "hidden",
            "food": 20,
            "health": 20,
            "my_name": name,
            "my_position": [
                -1,
                -59,
                1
            ],
            "nearby_entities": [

            ],
            "oxygen": 20,
            "saturation": 2,
            "timeOfDay": "sunrise"
        }

        env = {
            "message": env,
            "status": True
        }
        return env
    
    def get_total_time(self):
        if self.launch_time is None:
            return 0
        return time.time() - self.launch_time
    
    def get_token_info(self):
        token_result = read_json_artifact(self._paths().tokens)
        if token_result.state == "valid":
            return token_result.value
        else:
            return {"message": "token info not found", "status": False}
    
    def get_action_log(self):
        action_result = read_json_artifact(self._paths().action_log)
        if action_result.state == "absent":
            return {}
        if action_result.state == "invalid":
            raise MinecraftActionLogError(
                f"action log is invalid: {action_result.error}"
            )
        if not isinstance(action_result.value, dict):
            raise MinecraftActionLogError("action log must contain an object")
        for agent_name, entries in action_result.value.items():
            if agent_name == "_attempt_id":
                if not isinstance(entries, str):
                    raise MinecraftActionLogError("action log attempt identity must be a string")
                continue
            if not isinstance(agent_name, str) or not isinstance(entries, list):
                raise MinecraftActionLogError(
                    "action log must map agent names to lists"
                )
        return action_result.value
        
    def get_init_state(self) -> [dict]:
        if not self.running and not self._virtual_debug:
            raise RuntimeError("Environment is not running; call '.launch()' first")
        if self.running:
            states = [self.agent_status(agent.name) for agent in self.agent_pool]
            if self._eac_runtime is not None:
                for agent, state in zip(self.agent_pool, states):
                    self._eac_runtime.ingest_initial_actor_state(agent.name, state)
            return states
        else:
            return [VillagerBench.virtual_env(agent.name) for agent in self.agent_pool]

    def reset_token(self):
        tokens = {}
        current_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        tokens["dates"] = current_time
        tokens["tokens_used"] = 0
        tokens["prompt_tokens"] = 0
        tokens["completion_tokens"] = 0
        tokens["successful_requests"] = 0
        tokens["total_cost"] = 0
        tokens["action_cost"] = 0
        atomic_write_json(self._paths().tokens, tokens)

    def get_all_agent_description(self) -> dict:
        agent_dict = {}
        for agent in self.agent_pool:
            tools = agent.tools
            tool_dict = {}
            for tool in tools:
                tool_dict[tool.name] = tool.description
            agent_dict[agent.name] = tool_dict
        return agent_dict


    def get_all_agent_description_tiny(self) -> dict:
        agent_dict = {}
        for agent in self.agent_pool:
            tools = agent.tools
            tool_list = []
            for tool in tools:
                tool_list.append(tool.name)
            agent_dict[agent.name] = tool_list
        
        # 分成共有和私有两部分，共有是所有agent tools的交集，私有是每个agent的独有tools
        public_tools = []
        private_tools = {}
        # 交集

        for agent in self.agent_pool:
            if len(public_tools) == 0:
                public_tools = agent_dict[agent.name]
            else:
                public_tools = list(set(public_tools).intersection(set(agent_dict[agent.name])))
        
        for agent in self.agent_pool:
            private_tools[agent.name] = list(set(agent_dict[agent.name]) - set(public_tools))
        

        return {"public_tools": public_tools, "private_tools": private_tools}
    

    def agent_describe(self, agent_name: str):
        for agent in self.agent_pool:
            if agent.name == agent_name:
                tools = agent.tools
                tool_dict = {}
                description = f"agent {agent_name} has tools:"
                for tool in tools:
                    tool_dict[tool.name] = tool.description
                    description += f" {tool.name}, {tool.description}\n"
                return tool_dict, description
        return {}, f"agent {agent_name} not found"

    def agents_ping(self):
        try:
            for agent in self.agent_pool:
                Agent.ping(agent.name)
        except:
            return {"message": "some agent not found", "status": False}
        return {"message": "all agents are online", "status": True}

    def agent_status(self, agent_name: str):  # 返回一个dict
        for agent in self.agent_pool:
            if agent.name == agent_name:
               return Agent.get_environment_info_dict(agent_name)
        return {"message": f"agent {agent_name} not found", "status": False}

    def agent_register(self, agent_tool=None, agent_number: int = 1, name_list: list[str] | None = None):
        '''
        register the agent to the environment
        '''
        agent_tool = list(agent_tool or ())
        name_list = list(name_list or ())
        if len(name_list) != agent_number:
            self.logger.warning(
                "[warning but dont worry] agent number not equal to names number, random names will be used")
            name_list = [names.get_first_name() for i in range(agent_number)]

        for i in range(agent_number):
            owned_tools = self.guard_tool_actions(agent_tool, actor_name=name_list[i])
            agent = Agent(
                name_list[i],
                tools=owned_tools,
                local_port=self.base_port + len(self.agent_pool),
                model=self.langchain_model,
                runtime_paths=self.runtime_paths,
                runtime_execution=self.runtime_execution,
            )
            agent.reflection_output_dir = self.runtime_paths.run_result_dir(self.task_name)
            if len(owned_tools) != 0:
                agent.tool = owned_tools
            self.agent_pool.append(agent)
            self.log[agent.name] = []

    def configure_tool_action_barrier(self, enter, exit) -> None:
        self._tool_action_enter = enter
        self._tool_action_exit = exit

    def configure_eac_runtime(self, runtime) -> None:
        if self.agent_pool:
            raise RuntimeError("Minecraft EAC runtime must be configured before agent registration")
        if self._eac_runtime is not None and self._eac_runtime is not runtime:
            raise RuntimeError("Minecraft EAC runtime mode is immutable")
        self._eac_runtime = runtime

    def get_eac_audit_artifact(self) -> dict:
        if self._eac_runtime is None:
            return {"configured": False, "read_only_projection": True}
        return self._eac_runtime.audit_artifact()

    def guard_tool_actions(self, tools, *, actor_name: str | None = None) -> list:
        runtime = getattr(self, "_eac_runtime", None)
        if runtime is None:
            return [self._guard_tool_action(tool) for tool in tools]
        if not actor_name:
            raise RuntimeError("Minecraft EAC guarded tools require an owning actor")
        return [self._guard_tool_action(tool, actor_name=actor_name) for tool in tools
                if runtime.supports_tool(getattr(tool, "name", ""))]

    @staticmethod
    def _copy_tool_for_guard(tool):
        """Copy the tool's callable binding before installing an actor wrapper.

        LangChain's Pydantic-v1 tools implement ``__copy__`` by sharing their
        internal ``__dict__``.  Assigning ``func`` on such a shallow copy also
        mutates the raw tool and every earlier actor copy, creating nested
        cross-actor guards.  Split the Pydantic-v1 instance bookkeeping while
        intentionally retaining immutable schemas and callback configuration.
        """
        guarded_tool = copy(tool)
        if guarded_tool is tool:
            raise RuntimeError("Minecraft guarded tool must support independent copying")
        source_state = getattr(tool, "__dict__", None)
        if source_state is not None and getattr(guarded_tool, "__dict__", None) is source_state:
            # Pydantic-v1's __copy__ returns a distinct model object but reuses
            # this mapping.  Split only the object state; callback/schema
            # objects remain intentionally shared and immutable here.
            if not isinstance(tool, BaseModel):
                raise RuntimeError("Minecraft guarded tool copy shares unsupported mutable state")
            object.__setattr__(guarded_tool, "__dict__", source_state.copy())
            fields_set = getattr(tool, "__fields_set__", None)
            if isinstance(fields_set, set):
                object.__setattr__(guarded_tool, "__fields_set__", fields_set.copy())
            constructor_state = getattr(tool, "_lc_kwargs", None)
            if isinstance(constructor_state, dict):
                object.__setattr__(guarded_tool, "_lc_kwargs", constructor_state.copy())
        return guarded_tool

    @staticmethod
    def _set_tool_func(tool, function) -> None:
        tool.func = function
        constructor_state = getattr(tool, "_lc_kwargs", None)
        if isinstance(constructor_state, dict):
            constructor_state["func"] = function

    def _guard_tool_action(self, tool, *, actor_name: str | None = None):
        original = getattr(tool, "func", None)
        if not callable(original):
            if getattr(self, "_eac_runtime", None) is not None:
                raise RuntimeError("Minecraft EAC guarded tool requires callable func")
            return tool
        guarded_tool = self._copy_tool_for_guard(tool)

        @wraps(original)
        def guarded(*args, **kwargs):
            self._tool_action_enter()
            try:
                if getattr(self, "_eac_runtime", None) is not None:
                    tool_name = getattr(tool, "name", None) or getattr(original, "__name__", "")
                    supplied_actor = kwargs.get("player_name", args[0] if args else None)
                    if supplied_actor != actor_name:
                        raise RuntimeError("Minecraft EAC actor identity mismatch")
                    return self._eac_runtime.mediate_tool(tool_name, original, args, kwargs)
                return original(*args, **kwargs)
            finally:
                self._tool_action_exit()

        self._set_tool_func(guarded_tool, guarded)
        return guarded_tool

    def _cancellation_tools(self, tools, cancellation_token, phase_callback):
        """Make invocation-local gates without replacing the authoritative guards."""
        if cancellation_token is None:
            return tools
        wrapped = []
        for tool in tools:
            invocation_tool = self._copy_tool_for_guard(tool)
            original = getattr(tool, "func", None)
            if not callable(original):
                wrapped.append(invocation_tool)
                continue
            @wraps(original)
            def gated(*args, _original=original, **kwargs):
                check_agent_cancellation(cancellation_token, phase="before_tool_guard")
                if callable(phase_callback):
                    phase_callback("tool_start")
                result = _original(*args, **kwargs)
                if callable(phase_callback):
                    phase_callback("tool_end")
                check_agent_cancellation(cancellation_token, phase="after_tool_return")
                return result
            self._set_tool_func(invocation_tool, gated)
            wrapped.append(invocation_tool)
        return wrapped

    def launch(self, debug: bool = False, fast_api=False):
        try:
            Agent.launch(
                host=self.host,
                port=self.port,
                debug=debug,
                fast=fast_api,
                runtime_paths=self.runtime_paths,
                runtime_execution=self.runtime_execution,
            )
        except BaseException:
            try:
                self.bridge_cleanup_result = Agent.kill()
            except MinecraftBridgeCleanupError as cleanup_error:
                self.bridge_cleanup_result = cleanup_error.cleanup_result
                self.bridge_cleanup_error = cleanup_error
            raise
        self.running = True
        self.reset()

    def reset(self):
        if self._virtual_debug:
            return
        self.logger.info("resetting...")
        paths = self._paths()
        if paths.load_status.exists():
            atomic_write_json(paths.load_status, {"status": "loading"})
        self.logger.info("waiting for server to start...")
        agent_names = [agent.name for agent in self.agent_pool]
        agent_names_str = ",".join(agent_names)
        if not self.running:
            raise RuntimeError("Environment is not running; call '.launch()' before '.reset()'")
        execution = getattr(self, "runtime_execution", None)
        if execution is None:
            execution = RuntimeExecution.resolve()

        def spawn(entrypoint, args, **kwargs):
            execution.verify(entrypoint)
            command = execution.python_command(entrypoint, *args)
            child = execution.child_kwargs(paths)
            child.update(kwargs)
            return subprocess.Popen(command, **child)

        def public(entrypoint, args):
            return execution.public_command(entrypoint, *args)

        if self.env_type == env_type.construction:
            if self.dig_needed:
                command_args = ("--idx", str(self.task_id), "--host", self.host, "--port", str(self.port), "--agent_num", str(len(self.agent_pool)), "--dig_needed", "true", "--agent_names", agent_names_str, "--task_name", self.task_name)
                spawn("build_judger", command_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.logger.debug(public("build_judger", command_args))
            else:
                command_args = ("--idx", str(self.task_id), "--host", self.host, "--port", str(self.port), "--agent_num", str(len(self.agent_pool)), "--agent_names", agent_names_str, "--task_name", self.task_name)
                spawn("build_judger", command_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.logger.debug(public("build_judger", command_args))
        elif self.env_type == env_type.farming:
            command_args = ("--idx", str(self.task_id), "--host", self.host, "--port", str(self.port), "--agent_num", str(len(self.agent_pool)), "--agent_names", agent_names_str, "--task_name", self.task_name)
            spawn("farm_craft_judger", command_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.logger.debug(public("farm_craft_judger", command_args))
        elif self.env_type == env_type.puzzle:
            command_args = ("--idx", str(self.task_id), "--host", self.host, "--port", str(self.port), "--max_task_num", str(self.max_task_num), "--agent_num", str(len(self.agent_pool)), "--agent_names", agent_names_str, "--task_name", self.task_name)
            spawn("escape_room_judger", command_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.logger.debug(public("escape_room_judger", command_args))
        elif self.env_type == env_type.auto:
            command_args = ("--idx", str(self.task_id), "--host", self.host, "--port", str(self.port), "--agent_num", str(len(self.agent_pool)), "--agent_names", agent_names_str, "--task_name", self.task_name, "--op_path", self.op_path)
            spawn("auto_judger", command_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.logger.debug(public("auto_judger", command_args))
        elif self.env_type == env_type.meta:
            command_args = ("--idx", str(self.task_id), "--host", self.host, "--port", str(self.port), "--agent_num", str(len(self.agent_pool)), "--agent_names", agent_names_str, "--task_name", self.task_name, "--runtime-root", str(paths.root.resolve()), "--runtime-layout", paths.layout)
            diagnostics_dir = Path(getattr(self, "meta_diagnostics_dir", None) or paths.data_dir)
            diagnostics_dir.mkdir(parents=True, exist_ok=True)
            stdout_path = str(diagnostics_dir / "meta_judger.stdout.log")
            stderr_path = str(diagnostics_dir / "meta_judger.stderr.log")
            with open(stdout_path, "wb") as stdout, open(stderr_path, "wb") as stderr:
                judger_process = spawn("meta_judger", command_args, stdout=stdout, stderr=stderr)
            diagnostics = {
                "command": list(public("meta_judger", command_args)),
                "pid": judger_process.pid,
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
                "load_status_history": [],
                "exit_code": None,
                "timeout_reason": None,
            }
            self._write_meta_judger_diagnostics(diagnostics)
            self.logger.debug(public("meta_judger", command_args))
        elif self.env_type == env_type.gen:
            command_args = ("--host", self.host, "--port", str(self.port), "--agent_num", str(len(self.agent_pool)), "--agent_names", agent_names_str, "--task_name", self.task_name)
            spawn("llm_gen_judger", command_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.logger.debug(public("llm_gen_judger", command_args))
        elif self.env_type == env_type.none:
            self.logger.info("no env type specified, only agent will be launched")
            return
        else:
            raise ValueError(f"Unsupported environment type: {self.env_type!r}")
        max_wait_num = LOAD_WAIT_SECONDS
        loaded = False
        while max_wait_num:
            time.sleep(1)
            max_wait_num -= 1
            try:
                if max_wait_num % 30 == 0 and max_wait_num != 120:
                    self.logger.info(f"waiting for server to start, guess the server is starting this task for the first time, please wait")
                status_result = read_json_artifact(paths.load_status)
                if status_result.state == "absent":
                    if self.env_type == env_type.meta:
                        diagnostics["load_status_history"].append({"status": "missing", "time": time.time()})
                        diagnostics["exit_code"] = judger_process.poll()
                        self._write_meta_judger_diagnostics(diagnostics)
                        if diagnostics["exit_code"] is not None:
                            raise RuntimeError(f"meta judger exited before loading with code {diagnostics['exit_code']}")
                    continue
                if status_result.state == "invalid":
                    self._invalid_status_reads = getattr(self, "_invalid_status_reads", 0) + 1
                    if self._invalid_status_reads >= 3:
                        raise RuntimeError(
                            f"load status remained invalid: {status_result.error}"
                        )
                    continue
                self._invalid_status_reads = 0
                status_data = status_result.value
                if self.env_type == env_type.meta:
                    phase = None
                    phase_path = paths.meta_judger_phase
                    if phase_path.exists():
                        with phase_path.open("r", encoding="utf-8") as f:
                            phase = f.read().strip() or None
                    diagnostics["load_status_history"].append({
                        "status": status_data.get("status"),
                        "phase": phase,
                        "time": time.time(),
                    })
                    diagnostics["load_phase"] = phase
                    diagnostics["exit_code"] = judger_process.poll()
                    self._write_meta_judger_diagnostics(diagnostics)
                    if diagnostics["exit_code"] is not None and status_data.get("status") != "loaded":
                        raise RuntimeError(f"meta judger exited before loading with code {diagnostics['exit_code']}")
                if status_data["status"] == "loaded":
                    self.logger.info("server started in background")
                    loaded = True
                    break
            except RuntimeError:
                raise
            except Exception as exc:
                raise Exception("server failed to start") from exc
        if not loaded:
            if self.env_type == env_type.meta:
                diagnostics["exit_code"] = judger_process.poll()
                diagnostics["timeout_reason"] = f"load_status did not reach loaded within {LOAD_WAIT_SECONDS} seconds"
                self._write_meta_judger_diagnostics(diagnostics)
            raise Exception("server failed to start")

    def _write_meta_judger_diagnostics(self, diagnostics):
        diagnostics_dir = Path(getattr(self, "meta_diagnostics_dir", None) or self._paths().data_dir)
        atomic_write_json(diagnostics_dir / "meta_judger_diagnostics.json", diagnostics)
    
    def get_msg(self, agent_name: str):
        '''
        get the message of the agent
        '''
        if self.running:
            return Agent.getMsg(agent_name)
        else:
            return {"message": "env not running", "status": False}
    
    def chat(self, from_agent: str, to_agent: str, message: str):
        '''
        chat with other agent
        '''
        if self.running:
            msg_instruction = f"/msg {to_agent} {message}"
            for agent in self.agent_pool:
                if agent.name == from_agent:
                    agent.run(msg_instruction)
                    return {"message": "success", "status": True}
            return {"message": "agent not found", "status": False}
        else:
            return {"message": "env not running", "status": False}

    def step(self, agent_name: str, action: str, max_turn: int = 7,
             cancellation_token=None, phase_callback=None):
        '''
        final_answer, {"input": response["input"], "action_list": action_list, "final_answer": final_answer}
        '''
        self.logger.debug("=" * 20 + " Env Step " + "=" * 20)
        self.logger.info(f"agent {agent_name}")
        self.logger.info("=" * 20 + " Env Step " + "=" * 20)
        self.agent_iteration_limit = max_turn
        find_agent = False
        for agent in self.agent_pool:
            if agent.name == agent_name:
                check_agent_cancellation(cancellation_token, phase="before_env_step")
                tools = self._cancellation_tools(agent.tools, cancellation_token, phase_callback)
                run_kwargs = {"max_iterations": max_turn, "tools": tools,
                              "cancellation_token": cancellation_token,
                              "phase_callback": phase_callback}
                try:
                    parameters = inspect.signature(agent.run).parameters
                    if not any(p.kind == inspect.Parameter.VAR_KEYWORD
                               for p in parameters.values()):
                        run_kwargs = {k: v for k, v in run_kwargs.items() if k in parameters}
                except (TypeError, ValueError):
                    pass
                feedback, detail = agent.run(action, **run_kwargs)

                check_agent_cancellation(cancellation_token, phase="after_env_step")

                self.log[agent_name].append(detail)

                return feedback, detail

        if not find_agent:
            self.logger.warning(f"agent {agent_name} not found")
            return None, {"input": None, "action_list": None, "final_answer": None}
        
    def iter_step(self, agent_name: str, instruction: str, actions: [], observations: [], recommended_actions: []):
        '''
        final_answer, {"input": response["input"], "action_list": action_list, "final_answer": final_answer}
        '''
        self.logger.debug("=" * 20 + " Env Step (iter) " + "=" * 20)
        self.logger.info(f"agent {agent_name}")
        self.logger.info("=" * 20 + " Env Step (iter)" + "=" * 20)
        find_agent = False
        for agent in self.agent_pool:
            if agent.name == agent_name:
                feedback, detail = agent.step(instruction, actions=actions, observations=observations, recommended_actions=recommended_actions)

                self.log[agent_name].append(detail)

                return feedback, detail

        if not find_agent:
            self.logger.warning(f"agent {agent_name} not found")
            return (None, None), {"input": None, "action_list": None, "final_answer": None}

    def get_metadata(self):
        if self.env_type == env_type.construction:
            with self._paths().map_description.open("r", encoding="utf-8") as f:
                metadata = json.load(f)
            return metadata

    def get_score(self):
        if self.env_type in (env_type.construction, env_type.farming, env_type.puzzle, env_type.meta):
            paths = self._paths()
            score_result = read_json_artifact(paths.score)
            if score_result.state != "valid":
                raise RuntimeError(
                    f"score artifact is {score_result.state}: {score_result.error or paths.score}"
                )
            return score_result.value

    def get_tool_runtime_context(self) -> dict:
        return Agent.tool_runtime_context()

    def get_tool_runtime_context_snapshot(self) -> dict:
        return Agent.tool_runtime_snapshot()

    def get_minecraft_bridge_diagnostics(self) -> dict:
        return (
            Agent.last_bridge_diagnostics
            if Agent.last_bridge_diagnostics is not None
            else Agent.bridge_diagnostics_summary()
        )

    def is_task_complete(self):
        if self.env_type != env_type.meta:
            return False
        status_result = read_json_artifact(self._paths().load_status)
        if status_result.state == "absent":
            return False
        if status_result.state == "invalid":
            self._invalid_status_reads = getattr(self, "_invalid_status_reads", 0) + 1
            if self._invalid_status_reads >= 3:
                raise RuntimeError(f"load status remained invalid: {status_result.error}")
            return False
        self._invalid_status_reads = 0
        return isinstance(status_result.value, dict) and status_result.value.get("status") == "end"


if __name__ == "__main__":

    try:

        env = VillagerBench(env_type.construction, 0)
        agent_tool = [Agent.place_item, Agent.open_container, Agent.dig_block, Agent.find_item]
        env.agent_register(agent_tool=agent_tool, agent_number=2)
        agent_tool = [Agent.place_item, Agent.open_container, Agent.dig_block, Agent.find_item]
        env.agent_register(agent_tool=agent_tool, agent_number=2)
        env.launch()

        feedback, detail = env.step(env.agent_pool[0].name, "open chest and get 1 dirt ")
        status = env.agent_status(env.agent_pool[0].name)

        env.get_score()

    finally:
        Agent.kill()
