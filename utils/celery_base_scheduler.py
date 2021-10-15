from __future__ import absolute_import

from mesa.time import BaseScheduler
import asyncio
from random import random
# from test_celery.tasks import execute_step

from test_celery.celery import app
import time


@app.task
def execute_step(agent_id):
    try:
        agent.step()
    except: pass

    # print ('long time task begins')
    # # sleep 5 seconds
    # time.sleep(5)
    # print ('long time task finished')
    return True


class ParallelBaseScheduler(BaseScheduler):

    def step(self) -> None:
        """Execute the step of all the agents, in parallel."""
        for agent in self.agent_buffer(shuffled=False):
            # execute_step.delay(agent)
            print(agent.unique_id)

        self.steps += 1
        self.time += 1



