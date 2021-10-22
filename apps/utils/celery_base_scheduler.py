from mesa.time import BaseScheduler
import asyncio
from random import random
from test_celery.tasks_tmp import execute_step

class CeleryBaseScheduler(BaseScheduler):

    def step(self) -> None:
        """Execute the step of all the agents, in parallel."""
        async def step_agent(agent):
            # # if agent.unique_id == 'p_000001':
            # #     print(f"{agent.unique_id = } will sleep for 5 seconds")
            # #     await asyncio.sleep(5)
            # await asyncio.sleep(random())
            await agent.step(self.time)
            # task = asyncio.create_task(agent.step())
            # await task

        async def execute_step(agent_list):
            # print('inside ParallelBaseScheduler.execute_step')

            # await asyncio.gather(*[step_agent(agent) for agent in agent_buffer(shuffled=False)])

            task_list = []
            for agent in agent_list:
                # async def step_agent(agent):
                #     await agent.step()
                # # agent.step()
                task = asyncio.create_task(step_agent(agent))
                task_list.append(task)

            for task in task_list:
                # print(task)
                await task

            # await asyncio.gather(*task_list)

        asyncio.run(execute_step(self.agent_buffer(shuffled=False)))

        self.steps += 1
        self.time += 1


    # def step(self) -> None:
    #     """Execute the step of all the agents, in parallel."""
    #     for agent in self.agent_buffer(shuffled=False):
    #         execute_step.delay(agent)

    #     self.steps += 1
    #     self.time += 1



