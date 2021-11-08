from mesa.time import BaseScheduler
import asyncio
from random import random
from test_celery.tasks_tmp import execute_step

class CeleryBaseScheduler(BaseScheduler):

    def step(self) -> None:
        """Execute the step of all the agents, in parallel."""
        async def step_agent(agent):
            await agent.step(self.time)

        async def execute_step(agent_list):

            task_list = []
            for agent in agent_list:
                task = asyncio.create_task(step_agent(agent))
                task_list.append(task)

            for task in task_list:
                await task


        asyncio.run(execute_step(self.agent_buffer(shuffled=False)))

        self.steps += 1
        self.time += 1
