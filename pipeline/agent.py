import os
import time
import logging
from env.env import VillagerBench
from type_define.graph import Task
from pipeline.data_manager import DataManager
from pipeline.utils import *
from model.openai_models import (
    OpenAILanguageModel,
    ProviderCallCancellationError,
    ProviderCallTerminationError,
)
from model.vllm_model import VLLMLanguageModel
from random import random, randint, choice, sample
from pipeline.agent_prompt import *
from pipeline.agent_rl_prompt import *
from rl_env import *
from speaking_style import speaking_styles, speaking_styles_zh
import numpy as np
import threading
import torch
import platform
import math
import inspect
from numbers import Integral, Real
from pathlib import Path
from model.utils import extract_info
from env.runtime_paths import RuntimePaths, atomic_write_json
from env.minecraft_client import (
    AgentExecutionCancelledError,
    check_agent_cancellation,
    wait_for_agent_cancellation,
    MinecraftActionLogError,
    MinecraftToolTimeoutError,
    ToolActionBlockedError,
)

class AgentFeedback:
    def __init__(self, task:Task, detail, status):
        self.task = task
        self.detail = detail
        self.status = status

    def to_json(self) -> dict:
        return {
            "task": self.task.to_json(),
            "detail": self.detail,
            "status": self.status,
        }


class ReflectionInterruptedError(InterruptedError):
    """Reflection was cancelled before its result could be committed."""

    def __init__(self, message, *, provider_termination_confirmed, diagnostics=None):
        super().__init__(message)
        self.provider_termination_confirmed = bool(provider_termination_confirmed)
        self.diagnostics = diagnostics or {}


class ReflectionOutcome:
    """A reflection result whose agent-side commit completed atomically."""

    def __init__(self, success: bool):
        self.success = bool(success)
        self.committed = True

    def __bool__(self):
        return self.success



class BaseAgent:
    '''
    ### BaseAgent is the single agent in the system, it can take action and reflect
    
    step: take an action and return the feedback and detail
    reflect: reflect on the task and return the result
    to_json: return the json format of the agent
    '''
    MAX_LOCAL_INTER_ACTION_DELAY = 5.0
    LOCAL_MODEL_CONFIG_KEYS = (
        "local_model_max_attempts",
        "local_model_max_actions",
        "local_model_inter_action_delay",
    )
    def __init__(self, llm:OpenAILanguageModel , env:VillagerBench, data_manager:DataManager, name:str, logger:logging.Logger = None, silent = False, 
    RL_mode = "", rl_env = None, rl_model = None, all_tools = None, local_model_max_attempts = 10,
    local_model_max_actions = 5, local_model_inter_action_delay = 0.0,
    run_id: str | None = None, reflection_output_dir: str | os.PathLike | None = None, **kwargs):
        self.env = env
        self.name = name
        self.data_manager = data_manager
        self.llm = llm
        self.history_action_list = ["No action yet"]
        self.reflect_info = {"prompt": [], "response": []}
        self.RL_mode = RL_mode
        self.logger = logger
        self.all_tools = list(all_tools or ())
        self._virtual_debug = not env.running
        self.run_id = run_id or getattr(env, "task_name", "test")
        runtime_paths = getattr(env, "runtime_paths", RuntimePaths.legacy())
        self.reflection_output_dir = (
            Path(reflection_output_dir)
            if reflection_output_dir is not None
            else runtime_paths.run_result_dir(self.run_id)
        )
        if (
            isinstance(local_model_max_attempts, bool)
            or not isinstance(local_model_max_attempts, Integral)
            or local_model_max_attempts <= 0
        ):
            raise ValueError("local_model_max_attempts must be a positive integer")
        if (
            isinstance(local_model_max_actions, bool)
            or not isinstance(local_model_max_actions, Integral)
            or local_model_max_actions <= 0
        ):
            raise ValueError("local_model_max_actions must be a positive integer")
        if isinstance(local_model_inter_action_delay, bool) or not isinstance(
            local_model_inter_action_delay, Real
        ):
            raise ValueError("local_model_inter_action_delay must be a finite non-negative number")
        try:
            local_model_inter_action_delay = float(local_model_inter_action_delay)
        except (OverflowError, TypeError, ValueError) as error:
            raise ValueError(
                "local_model_inter_action_delay must be a finite non-negative number"
            ) from error
        if not math.isfinite(local_model_inter_action_delay) or local_model_inter_action_delay < 0:
            raise ValueError("local_model_inter_action_delay must be a finite non-negative number")
        self.local_model_max_attempts = local_model_max_attempts
        self.local_model_max_actions = local_model_max_actions
        self.local_model_inter_action_delay = min(
            local_model_inter_action_delay, self.MAX_LOCAL_INTER_ACTION_DELAY
        )
        if self.logger is None:
            self.logger = init_logger("BaseAgent", dump=True, silent=silent)
        
        if self.RL_mode == "PPO":
            self.rl_env = rl_env
            self.rl_model = rl_model

        self.instruction_history = []  # 新增：保存历史指令
        self.state_history = []        # 新增：保存历史状态

        self.IDLE = True  # 控制是否处于 IDLE 状态
        self.stop_event = threading.Event()  # 用于控制线程停止
        # self.start_idle_thread()  # 启动 IDLE 线程

        system_type = platform.system().lower()
        if system_type == "linux":
            self.logger.info("Linux system detected.")
            self.logger.info("EMOJI is supported.")
            self.EMOJI = True
        else:
            self.logger.info("EMOJI is not supported.")
            self.EMOJI = False

    def update_reflect(self, system_prompt, user_prompt, response):
        if type(user_prompt) == str:
            user_prompt = [user_prompt]
        prompt = str(system_prompt) + "\n"
        for i in range(len(user_prompt)):
            prompt += user_prompt[i] + "\n"

        self.reflect_info["prompt"].append(prompt)
        self.reflect_info["response"].append(response)
        atomic_write_json(
            self.reflection_output_dir / f"{self.name}_reflect.json",
            self.reflect_info,
        )

    def supports_cooperative_cancellation(self) -> bool:
        return (
            not self._virtual_debug
            and self.RL_mode == ""
        )

    def step(self, task:Task, cancellation_token=None, phase_callback=None) -> (str, dict):
        '''
        take an action and return the feedback and detail
        cancellation_token is a cooperative boundary signal.
        return: final_answer, {"input": response["input"], "action_list": action_list, "final_answer": final_answer}
        '''
        if self._virtual_debug:
            return self.virtual_step(task)

        if self.RL_mode != "":
            return self.rl_step(task)
        else:
            if isinstance(self.llm, VLLMLanguageModel):
                return self.local_step(task, cancellation_token=cancellation_token)
            else:
                if cancellation_token is None and phase_callback is None:
                    return self.normal_step(task)
                return self.normal_step(task, cancellation_token=cancellation_token,
                                        phase_callback=phase_callback)
        
    def rl_step(self, task:Task) -> (str, dict):
        # 构建基础提示和状态
        instruction = format_string(task_prompt, {
            "task_description": task.description,
            "milestone_description": task.milestones,
        })
        
        basic_state = format_string(state_prompt, {
            "env": self.data_manager.query_env_with_task(task.description, agent_query=True),
            "relevant_data": smart_truncate(task.content, max_length=4096), 
        })

        max_rl_steps = 5
        actions = []
        observations = []
        current_state = basic_state
        task_status = False

        while max_rl_steps > 0:
            # 构建当前状态字符串
            current_context = f"{instruction}\n{current_state}"
            if actions and observations:
                action_history = "\n".join([f"Action: {a}\nObservation: {o}" for a, o in zip(actions, observations)])
                current_context += f"\nHistory:\n{action_history}"

            if self.env.agents_ping()["status"] == False:
                self.logger.info("Some agents are offline!")
                break 
            
            best_act, best_obs = None, None
            best_reward = -np.inf
            print(f"# max_rl_steps: {max_rl_steps}")
            k_step = 30
            while k_step > 0:
                # 获取模型动作
                # try:
                if self.env.agents_ping()["status"] == False:
                    self.logger.info("Some agents are offline!")
                    break 
                rl_action = self.rl_model.take_action(current_context)
                if not isinstance(rl_action, (int, np.integer)) or not 0 <= rl_action < len(self.rl_env.available_actions):
                    raise ValueError(
                        f"RL model returned invalid action index {rl_action!r}; expected an integer from 0 "
                        f"to {len(self.rl_env.available_actions) - 1}"
                    )
                rl_api = self.rl_env._get_available_actions()[rl_action]
                print(f"{rl_action} - {rl_api}")
                
                
                (act, obs), detail = self.env.iter_step(self.name, current_context + "You need try to use the tool, do not use Final Answer.", 
                                                    actions=actions, 
                                                    observations=observations, 
                                                    recommended_actions=[rl_api])
                if act is None:
                    continue
                    
                # 更新状态
                current_state = f"{basic_state}\nLast Action: {act}\nLast Observation: {obs}"
                
                # 计算奖励和任务状态
                reward, task_status = self.rl_one_step_reflect(
                    task.description, 
                    task.milestones,
                    actions=actions,
                    observations=observations,
                    act=act,
                    obs=obs,
                )

                # 构建转换字典
                transition_dict = {
                    "states": current_context,
                    "actions": rl_action,
                    "rewards": reward,
                    "next_states": f"{instruction}\n{current_state}",
                    "dones": task_status
                }

                # 更新模型
                self.rl_model.update(transition_dict)
                
                if self.RL_mode == "PPO":
                    self.rl_model.train_step()

                if reward > best_reward:
                    best_reward = reward
                    best_act, best_obs = act, obs

                k_step -= 1

                # except KeyboardInterrupt:
                #     self.logger.info("KeyboardInterrupt")
                #     raise KeyboardInterrupt
                # except ConnectionError:
                #     self.logger.error("ConnectionError")
                #     raise ConnectionError
                # except ConnectionRefusedError:
                #     self.logger.error("ConnectionRefusedError")
                #     raise ConnectionRefusedError
                # except Exception as e:
                #     self.logger.error(f"Error: {e}")

            actions.append(best_act)
            observations.append(best_obs)
            max_rl_steps -= 1

        status = self.env.agent_status(self.name)
        self.data_manager.update_database(AgentFeedback(task, detail, status).to_json())
        
        if task_status:
            summary = f"successfully done {task.description}."
            task.status = Task.success
        else:
            summary = f"failed to do {task.description}."
            task.status = Task.failure
        return summary, detail

    def start_idle_thread(self):
        '''
        Start the idle_step function in a separate thread.
        '''
        self.idle_thread = threading.Thread(target=self.idle_step, daemon=True)
        self.idle_thread.start()

    def stop_idle_thread(self):
        '''
        Stop the idle_step thread.
        '''
        self.stop_event.set()  # 设置停止信号
        self.idle_thread.join()  # 等待线程结束

    def idle_step(self):
        '''
        idle_step is the step for the agent to wait for the task
        At this time, the agent will help other agents to do the task
        find what the agent can do or talk with other agents or just wait
        '''
        if random() < 0.5:
            speech_style = sample(list(speaking_styles.keys()), 1)[0]
            personality = speaking_styles[speech_style]['personality']
            traits = speaking_styles[speech_style]['traits']
            example = speaking_styles[speech_style]['example']
        else:
            speech_style = sample(list(speaking_styles_zh.keys()), 1)[0]
            personality = speaking_styles_zh[speech_style]['性格']
            traits = speaking_styles_zh[speech_style]['特征']
            example = speaking_styles_zh[speech_style]['示例']


        if platform.system().lower() != "linux":
            idle_prompt = idle_prompt_wo_emoji
        else:
            idle_prompt = idle_prompt_w_emoji

        task_str = format_string(idle_prompt, 
                                 {
                                "agent_name": self.name,
                                "agent_state": self.data_manager.query_history(self.name),
                                "personality": personality,
                                "example": example,
                                "traits": traits,
                                "other_agents": self.other_agents(),
                                "agent_action_list": self.history_action_list,
                                "minecraft_knowledge_card": minecraft_knowledge_card})

        actions = []
        observations = []
        basic_state = ""
        current_state = basic_state

        time.sleep(60) # 等待30秒 等待启动
        while not self.stop_event.is_set():  # 主循环，直到收到退出信号
            if not self.IDLE:
                # 如果不处于 IDLE 状态，进入等待
                time.sleep(1)
                continue

            # 构建当前状态字符串
            current_context = f"{task_str}\n{current_state}"
            if actions and observations:
                action_history = "\n".join([f"Action: {a}\nObservation: {o}" for a, o in zip(actions, observations)])
                current_context += f"\nHistory:\n{action_history}"

            if self.env.agents_ping()["status"] == False:
                self.logger.info("Some agents are offline!")
                break 
            
            try:
                if self.env.agents_ping()["status"] == False:
                    self.logger.info("Some agents are offline!")
                    break 
                
                time.sleep(2)
                (act, obs), detail = self.env.iter_step(self.name, current_context, 
                                                    actions=actions, 
                                                    observations=observations, 
                                                    recommended_actions=[])
                if act is None:
                    continue
                    
                # 更新状态
                current_state = f"{basic_state}\nLast Action: {act}\nLast Observation: {obs}"

            except KeyboardInterrupt:
                self.logger.info("KeyboardInterrupt")
                self.stop_event.set()  # 设置停止信号
                raise KeyboardInterrupt
            except ConnectionError:
                self.logger.error("ConnectionError")
                self.stop_event.set()  # 设置停止信号
                raise ConnectionError
            except ConnectionRefusedError:
                self.logger.error("ConnectionRefusedError")
                self.stop_event.set()  # 设置停止信号
                raise ConnectionRefusedError
            except Exception as e:
                self.logger.error(f"Error: {e}")

            actions.append(act)
            observations.append(obs)

    
    def normal_step(self, task:Task, cancellation_token=None, phase_callback=None) -> (str, dict):
        if random() < 0.5:
            speech_style = sample(list(speaking_styles.keys()), 1)[0]
            personality = speaking_styles[speech_style]['personality']
            traits = speaking_styles[speech_style]['traits']
            example = speaking_styles[speech_style]['example']
        else:
            speech_style = sample(list(speaking_styles_zh.keys()), 1)[0]
            personality = speaking_styles_zh[speech_style]['性格']
            traits = speaking_styles_zh[speech_style]['特征']
            example = speaking_styles_zh[speech_style]['示例']

        if platform.system().lower() != "linux":
            agent_prompt = agent_prompt_wo_emoji
        else:
            agent_prompt = agent_prompt_w_emoji

        if len(task._agent) == 1:
            task_str = format_string(agent_prompt, {"task_description": task.description, "milestone_description": task.milestones, 
                                    "env": self.data_manager.query_env_with_task(task.description, agent_query=True),
                                    "relevant_data": smart_truncate(task.content, max_length=4096), # TODO: change to "relevant_data": task.content
                                    "agent_name": self.name,
                                    "agent_state": self.data_manager.query_history(self.name),
                                    "personality": personality,
                                    "example": example,
                                    "traits": traits,
                                    "other_agents": self.other_agents(),
                                    "agent_action_list": self.history_action_list,
                                    "minecraft_knowledge_card": minecraft_knowledge_card})
        else:
            task_str = format_string(agent_cooper_prompt, {"task_description": task.description, "milestone_description": task.milestones, 
                                    "env": self.data_manager.query_env_with_task(task.description, agent_query=True),
                                    "relevant_data": smart_truncate(task.content, max_length=4096), # TODO: change to "relevant_data": task.content
                                    "agent_name": self.name,
                                    "agent_state": self.data_manager.query_history(self.name),
                                    "other_agents": self.other_agents(),
                                    "agent_action_list": self.history_action_list,
                                    "team_members": ", ".join(task._agent),
                                    "minecraft_knowledge_card": minecraft_knowledge_card})
            
        self.logger.debug("="*20 + " Agent Step " + "="*20)
        self.logger.info(f"{self.name} try task:\n {task.description}")
        self.logger.info(f"{self.history_action_list}")
        self.logger.info(f"other agents: {self.other_agents()}")
        self.logger.info(f"{self.name} status:\n {self.data_manager.query_history(self.name)}")
        max_retry = 3
        
        instruction = format_string(task_prompt, {
            "task_description": task.description,
            "milestone_description": task.milestones,
        })
        state = format_string(state_prompt, {
            "other_agents": self.other_agents(),
            "agent_name": self.name,
            "env": self.data_manager.query_env_with_task(task.description, agent_query=True),
            "relevant_data": smart_truncate(task.content, max_length=4096), 
            "agent_state": self.data_manager.query_history(self.name),
        })
        def check(phase):
            if callable(phase_callback):
                phase_callback(phase)
            check_agent_cancellation(cancellation_token, phase=phase)

        def cancelled_result(error):
            failure = dict(error.failure_detail)
            detail = {"input": task_str, "action_list": [],
                      "final_answer": failure["message"], "failure": failure}
            return ({"message": failure["message"], "status": False,
                     "new_events": [], "error": failure}, detail)

        self.IDLE = False
        last_error = None
        try:
            while max_retry > 0:
                try:
                    check("before_env_step")
                    step_kwargs = {"cancellation_token": cancellation_token,
                                   "phase_callback": phase_callback}
                    try:
                        parameters = inspect.signature(self.env.step).parameters
                        if not any(p.kind == inspect.Parameter.VAR_KEYWORD
                                   for p in parameters.values()):
                            step_kwargs = {k: v for k, v in step_kwargs.items() if k in parameters}
                    except (TypeError, ValueError):
                        pass
                    feedback, detail = self.env.step(self.name, task_str, **step_kwargs)
                    check("after_env_return")
                    break
                except AgentExecutionCancelledError as error:
                    return cancelled_result(error)
                except KeyboardInterrupt:
                    self.logger.info("KeyboardInterrupt")
                    raise KeyboardInterrupt
                except ConnectionError:
                    self.logger.error("ConnectionError")
                    raise ConnectionError
                except ConnectionRefusedError:
                    self.logger.error("ConnectionRefusedError")
                    raise ConnectionRefusedError
                except (ToolActionBlockedError, MinecraftToolTimeoutError,
                        MinecraftActionLogError):
                    raise
                except Exception as e:
                    last_error = e
                    self.logger.error(f"Error: {e}")
                    max_retry -= 1
                    try:
                        wait_for_agent_cancellation(cancellation_token, 3)
                        check("retry_wait")
                    except AgentExecutionCancelledError as error:
                        return cancelled_result(error)
            else:
                raise last_error

            try:
                check("before_agent_status")
                status = self.env.agent_status(self.name)
                check("before_database_update")
                self.data_manager.update_database(AgentFeedback(task, detail, status).to_json())
            except AgentExecutionCancelledError as error:
                return cancelled_result(error)
        finally:
            self.IDLE = True

        # self.data_manager.save()
        return feedback, detail
    
    def local_step(self, task:Task, cancellation_token=None) -> (str, dict):
        self.logger.warning("=" * 20 + " LOCAL Step " + "=" * 20)
        self.logger.warning(f"agent {self.name}")
        self.logger.warning("=" * 20 + " LOCAL Step " + "=" * 20)
        if random() < 0.5:
            speech_style = sample(list(speaking_styles.keys()), 1)[0]
            personality = speaking_styles[speech_style]['personality']
            traits = speaking_styles[speech_style]['traits']
            example = speaking_styles[speech_style]['example']
        else:
            speech_style = sample(list(speaking_styles_zh.keys()), 1)[0]
            personality = speaking_styles_zh[speech_style]['性格']
            traits = speaking_styles_zh[speech_style]['特征']
            example = speaking_styles_zh[speech_style]['示例']

        if platform.system().lower() != "linux":
            agent_prompt = agent_prompt_wo_emoji
        else:
            agent_prompt = agent_prompt_w_emoji

        if len(task._agent) == 1:
            task_str = format_string(agent_prompt, {"task_description": task.description, "milestone_description": task.milestones, 
                                    "env": self.data_manager.query_env_with_task(task.description, agent_query=True),
                                    "relevant_data": smart_truncate(task.content, max_length=4096), # TODO: change to "relevant_data": task.content
                                    "agent_name": self.name,
                                    "agent_state": self.data_manager.query_history(self.name),
                                    "personality": personality,
                                    "example": example,
                                    "traits": traits,
                                    "other_agents": self.other_agents(),
                                    "agent_action_list": self.history_action_list,
                                    "minecraft_knowledge_card": minecraft_knowledge_card})
        else:
            task_str = format_string(agent_cooper_prompt, {"task_description": task.description, "milestone_description": task.milestones, 
                                    "env": self.data_manager.query_env_with_task(task.description, agent_query=True),
                                    "relevant_data": smart_truncate(task.content, max_length=4096), # TODO: change to "relevant_data": task.content
                                    "agent_name": self.name,
                                    "agent_state": self.data_manager.query_history(self.name),
                                    "other_agents": self.other_agents(),
                                    "agent_action_list": self.history_action_list,
                                    "team_members": ", ".join(task._agent),
                                    "minecraft_knowledge_card": minecraft_knowledge_card})
            
        self.logger.info(f"{self.history_action_list}")
        self.logger.info(f"other agents: {self.other_agents()}")
        self.logger.info(f"{self.name} status:\n {self.data_manager.query_history(self.name)}")
        instruction = f"Your name is {self.name}.\n{task_str}"
        system_prompt = "You are Minecraft BaseAgent. You need to complete the task by following the environment feedback."

        prompts = [instruction]    

        action_list = []
        feedback = {"message": "", "status": False, "new_events": []}
        final_answer = ""
        detail = {"input": instruction, "action_list": action_list, "final_answer": final_answer}
        model_attempts = 0
        successful_actions = 0
        last_failure = None

        def is_cancelled():
            if cancellation_token is None:
                return False
            if callable(cancellation_token):
                return bool(cancellation_token())
            is_set = getattr(cancellation_token, "is_set", None)
            if not callable(is_set):
                raise TypeError("cancellation_token must be callable or expose is_set()")
            return bool(is_set())

        def fail(reason, message):
            nonlocal feedback, final_answer, detail
            failure = {
                "reason": reason,
                "message": message,
                "model_attempts": model_attempts,
                "successful_actions": successful_actions,
            }
            if reason == "cancelled":
                failure["cancellation_acknowledged"] = True
            if last_failure is not None:
                failure["last_failure"] = last_failure
            feedback = {
                "message": message,
                "status": False,
                "new_events": [],
                "error": failure,
            }
            final_answer = message
            detail = {
                "input": instruction,
                "action_list": action_list,
                "final_answer": final_answer,
                "failure": failure,
            }

        self.IDLE = False
        try:
            while (
                model_attempts < self.local_model_max_attempts
                and successful_actions < self.local_model_max_actions
            ):
                if is_cancelled():
                    fail("cancelled", "Local agent execution was cancelled.")
                    break

                model_attempts += 1
                try:
                    raw_response = self.llm.few_shot_generate_thoughts(
                        system_prompt,
                        prompts,
                        temperature=0.2,
                        cache_enabled=False,
                        max_tokens=8000,
                        json_check=False,
                    )
                except Exception as error:
                    last_failure = {"reason": "model_exception", "message": str(error)}
                    self.logger.error(f"Local model error: {error}")
                    continue

                if is_cancelled():
                    fail("cancelled", "Local agent execution was cancelled.")
                    break

                from ast import literal_eval
                try:
                    response = literal_eval(raw_response.split("Action: ")[-1].strip())
                    if not isinstance(response, dict):
                        raise ValueError("action must be a dictionary")
                    func_name = response["tool"]
                    tool_input = response["tool_input"]
                    if not isinstance(func_name, str) or not isinstance(tool_input, dict):
                        raise ValueError("tool must be a string and tool_input must be a dictionary")
                except (AttributeError, KeyError, SyntaxError, TypeError, ValueError) as error:
                    last_failure = {"reason": "malformed_model_output", "message": str(error)}
                    self.logger.error(f"Malformed local model output: {raw_response}")
                    continue

                if func_name == "stop":
                    if "final_answer" not in tool_input:
                        last_failure = {
                            "reason": "malformed_model_output",
                            "message": "stop tool_input is missing final_answer",
                        }
                        continue
                    final_answer = str(tool_input["final_answer"])
                    feedback = {"message": final_answer, "status": True, "new_events": []}
                    detail["final_answer"] = final_answer
                    break

                target_tool = next((tool for tool in self.all_tools if tool.name == func_name), None)
                if target_tool is None:
                    last_failure = {
                        "reason": "unknown_tool",
                        "message": f"Unknown tool: {func_name}",
                    }
                    continue

                if is_cancelled():
                    fail("cancelled", "Local agent execution was cancelled.")
                    break

                try:
                    tool_feedback = target_tool(tool_input)
                except ToolActionBlockedError:
                    fail("cancelled", "Tool action was blocked by judged terminal status.")
                    break
                except MinecraftToolTimeoutError as error:
                    last_failure = dict(error.failure_detail)
                    fail(
                        "minecraft_tool_timeout",
                        "Minecraft tool response timed out; the operation outcome is unknown.",
                    )
                    detail["failure"].update({
                        "outcome_certainty": "unknown",
                        "retry_safe": False,
                    })
                    break
                except MinecraftActionLogError as error:
                    last_failure = dict(error.failure_detail)
                    fail(
                        "minecraft_action_log_error",
                        "Minecraft action completed but its evidence log could not be persisted.",
                    )
                    detail["failure"].update({
                        "outcome_certainty": "unknown",
                        "retry_safe": False,
                    })
                    break
                except Exception as error:
                    last_failure = {"reason": "tool_exception", "message": str(error)}
                    fail("tool_exception", f"Tool {func_name} failed: {error}")
                    break

                required_feedback = {"message", "status"}
                if not isinstance(tool_feedback, dict) or not required_feedback.issubset(tool_feedback):
                    missing = (
                        sorted(required_feedback - set(tool_feedback))
                        if isinstance(tool_feedback, dict)
                        else sorted(required_feedback)
                    )
                    last_failure = {
                        "reason": "invalid_tool_feedback",
                        "message": f"Tool {func_name} feedback is missing fields: {', '.join(missing)}",
                    }
                    fail("invalid_tool_feedback", last_failure["message"])
                    break

                feedback = dict(tool_feedback)
                feedback.setdefault("new_events", [])
                successful_actions += 1
                prompts.append(raw_response)
                user = f"Feedback: {feedback['message']}\nStatus: {feedback['status']}\nNew Events: {feedback['new_events']}"
                action_list.append({"action": response, "feedback": feedback["message"]})
                prompts.append(user)
                final_answer = user
                detail["final_answer"] = final_answer

                if is_cancelled():
                    fail("cancelled", "Local agent execution was cancelled.")
                    break

                if (
                    self.local_model_inter_action_delay > 0
                    and model_attempts < self.local_model_max_attempts
                    and successful_actions < self.local_model_max_actions
                ):
                    wait = getattr(cancellation_token, "wait", None)
                    if callable(wait):
                        wait(self.local_model_inter_action_delay)
                    else:
                        time.sleep(self.local_model_inter_action_delay)
            else:
                if model_attempts >= self.local_model_max_attempts:
                    fail("model_attempt_budget_exhausted", "Local model attempt budget exhausted.")
                else:
                    fail("action_budget_exhausted", "Local agent action budget exhausted.")
        finally:
            self.IDLE = True

        if is_cancelled() and detail.get("failure", {}).get("reason") != "cancelled":
            fail("cancelled", "Local agent execution was cancelled.")

        status = self.env.agent_status(self.name)
        self.data_manager.update_database(AgentFeedback(task, detail, status).to_json())

        # self.data_manager.save()
        return feedback, detail
    def other_agents(self) -> [str]:
        '''
        return the feedback of other agent's pretask
        '''
        return self.data_manager.query_other_agent_state(self.name)
    
    def action_format(self, action:dict) -> str:
        action_str = '''{{message}}'''
        feedback = action.get("feedback", {})
        # 如果 feedback 是字符串，转换成 {"message": feedback}
        if isinstance(feedback, str):
            feedback = {"message": feedback}
        # 否则确保 feedback 是字典，并设置默认 message
        elif not isinstance(feedback, dict):
            feedback = {"message": ""}
        
        # 如果 message 不存在，设置默认空字符串
        if "message" not in feedback:
            feedback["message"] = ""

        return format_string(action_str, feedback)
    
    def rl_one_step_reflect(self, task_description, milestone_description, actions, observations, act, obs):
        '''
        One step reflect
        '''
        act_obs = ""
        for act, obs in zip(actions, observations):
            act_obs += f"\n{act['log']}\n{obs}"
        
        if obs["status"] == False:
            return -1, False

        prompt = format_string(one_step_reflect_prompt,
            {
                "task_description": task_description,
                "milestone_description": milestone_description,
                "action_observation": act_obs,
                "act": act,
                "obs": obs,
            })
        
        response = self.llm.few_shot_generate_thoughts(reflect_system_prompt, prompt, cache_enabled=False, max_tokens=256, json_check=True,
                                                    check_tags=["task_status", "reward"])
        print(response)
        result = extract_info(response)[0]
        task_status = result["task_status"]
        reward = result["reward"]
        print(f"rl_action: {act}, reward: {reward}")
        return reward, task_status
    
    def filter_emoji(self, text: str) -> str:
        ret_str = []
        for c in text:
            try:
                c.encode('gbk')
                ret_str.append(c)
            except UnicodeEncodeError:
                continue
        return ''.join(ret_str)

    def reflect(self, task: Task, detail, cancellation_token=None, commit_lock=None) -> bool:
        '''
        Reflect on the task and return the result
        '''
        def is_cancelled():
            if cancellation_token is None:
                return False
            if callable(cancellation_token):
                return bool(cancellation_token())
            is_set = getattr(cancellation_token, "is_set", None)
            if not callable(is_set):
                raise TypeError("cancellation_token must be callable or expose is_set()")
            return bool(is_set())

        def interrupted(diagnostics=None, confirmed=True):
            raise ReflectionInterruptedError(
                "Agent reflection was cancelled",
                provider_termination_confirmed=confirmed,
                diagnostics=diagnostics,
            )

        if is_cancelled():
            interrupted({"phase": "before_start"})

        task_description = task.description
        milestone_description = task.milestones
        action_history = detail["action_list"]
        global reflect_system_prompt, reflect_user_prompt
        # print("before llm")
        if isinstance(self.llm, OpenAILanguageModel):
            prompt = format_string(reflect_user_prompt,
                                   {
                                       "task_description": task_description,
                                       "milestone_description": milestone_description,
                                       "state": self.data_manager.query_history(self.name),
                                       "action_history": action_history
                                   })
            prompt = self.filter_emoji(prompt)
            # print("before response")
            try:
                response = self.llm.few_shot_generate_thoughts(
                    reflect_system_prompt, prompt, cache_enabled=False, max_tokens=256,
                    json_check=True, cancellation_event=cancellation_token,
                    model_admission_lock=commit_lock,
                )
            except ProviderCallCancellationError as error:
                interrupted(error.close_failure_diagnostics, error.provider_termination_confirmed)
            except ProviderCallTerminationError as error:
                interrupted(
                    {
                        "phase": "provider_timeout",
                        "error_type": type(error).__name__,
                    },
                    confirmed=False,
                )
        else:
            prompt = format_string(reflect_user_prompt,
                                   {
                                       "task_description": task_description,
                                       "milestone_description": milestone_description,
                                       "state": self.data_manager.query_history(self.name),
                                       "action_history": action_history
                                   })
            prompt = self.filter_emoji(prompt)
            # print("before response")
            try:
                call_kwargs = {"cache_enabled": False, "max_tokens": 256, "json_check": False}
                if cancellation_token is not None and isinstance(self.llm, OpenAILanguageModel):
                    call_kwargs["cancellation_event"] = cancellation_token
                response = self.llm.few_shot_generate_thoughts(reflect_system_prompt, prompt, **call_kwargs)
            except ProviderCallCancellationError as error:
                interrupted(error.close_failure_diagnostics, error.provider_termination_confirmed)
        if is_cancelled():
            interrupted({"phase": "after_provider"})
        response = self.filter_emoji(response)
        result = extract_info(response)[0]

        def commit_result():
            if is_cancelled():
                interrupted({"phase": "before_commit"})
            self.update_reflect(reflect_system_prompt, prompt, response)
            task.reflect = result
            if "summary" in result.keys():
                task._summary.append(result["summary"])
            else:
                task._summary.append(str(result))
            self.history_action_list = [
                self.action_format(action) for action in action_history
            ]

        if commit_lock is None:
            commit_result()
        else:
            with commit_lock:
                commit_result()
        return ReflectionOutcome(result.get("task_status", False))
        # return result["task_status"]
    
    def to_json(self) -> dict:
        return {
            "name": self.name
        }
    
    def virtual_env(name:str):
        '''
        ### virtual_env is the virtual environment for the agent to test the agent
        return the virtual environment
        '''
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

    def virtual_step(self, task:Task) -> (str, dict):
        '''
        ### virtual_step is the virtual step for the agent to test the agent
        take an action and return the feedback and detail
        return: final_answer, {"input": response["input"], "action_list": action_list, "final_answer": final_answer}
        '''
        # random action
        action = choice(["place", "dig", "find", "open"])
        input = smart_truncate(task.to_json(), max_length=4096)
        random_action_num = randint(1, 10)
        action_list = []
        for i in range(random_action_num):
            action_dict = {
                "tool" : action,
                "tool_input" : {
                    "player_name": self.name,
                    "x": randint(-100, 100),
                    "y": randint(-100, 100),
                    "z": randint(-100, 100),
                },
                "log": "random action"
            }
            feedback = {
                "message": f"execute {action_dict['tool']} at {action_dict['tool_input']['x']} {action_dict['tool_input']['y']} {action_dict['tool_input']['z']}",
                "status": True
            }
            action_list.append({"action": action_dict, "feedback": feedback})
        score = random()
        if score > 0.3:
            final_answer = f"successfully done {task.description}."
            task.status = Task.success
        else:
            final_answer = f"failed to do {task.description}."
            task.status = Task.failure
        detail = {
            "input": input,
            "action_list": action_list,
            "final_answer": final_answer,
        }
        
        self.data_manager.update_database(AgentFeedback(task, detail, VillagerBench.virtual_env(self.name)).to_json())
        # self.data_manager.save()
        return final_answer, {"input": input, "action_list": action_list, "final_answer": final_answer}
