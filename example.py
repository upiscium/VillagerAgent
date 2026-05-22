from env.env import VillagerBench, env_type, Agent
from pipeline.controller import GlobalController
from pipeline.data_manager import DataManager
from pipeline.task_manager import TaskManager
from model.ollama_config import make_ollama_llm_config, configure_ollama_agent


if __name__ == "__main__":
    env = VillagerBench(
        env_type.construction,
        task_id=0,
        _virtual_debug=False,
        dig_needed=False,
        host="10.12.1.200",
        port=40000,
    )

    llm_config = make_ollama_llm_config("gemma4:e4b")
    configure_ollama_agent(Agent, "gemma4:e4b")

    agent_tool = [
        Agent.fetchContainerContents,
        Agent.MineBlock,
        Agent.scanNearbyEntities,
        Agent.equipItem,
        Agent.SmeltingCooking,
        Agent.navigateTo,
        Agent.withdrawItem,
        Agent.craftBlock,
        Agent.attackTarget,
        Agent.useItemOnEntity,
        Agent.handoverBlock,
    ]

    env.agent_register(agent_tool=agent_tool, agent_number=3, name_list=["Agent1", "Agent2", "Agent3"])

    with env.run(fast_api=True):
        dm = DataManager(silent=False)
        dm.update_database_init(env.get_init_state())

        tm = TaskManager(silent=False)
        ctrl = GlobalController(llm_config, tm, dm, env)

        tm.init_task("Build a small construction task with three agents.", {})
        ctrl.run()
