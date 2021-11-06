from abc import ABC, abstractclassmethod, abstractmethod
import asyncio, json, logging, time

from datetime import datetime
from apps.messenger_service import Messenger

from random import random
from apps.config import settings

class ORSimScheduler(ABC):

    def __init__(self, run_id, scheduler_id):
        # self.sim_settings = sim_settings
        self.run_id = run_id
        self.scheduler_id = scheduler_id
        self.time = 0

        self.sim_settings = settings['SIM_SETTINGS']

        self.agent_collection = {}

        self.agent_credentials = {
            'email': f"{self.run_id}_{self.scheduler_id}_ORSimScheduler",
            'password': "secret_password",
        }

        self.agent_messenger = Messenger(self.agent_credentials, f"{self.run_id}/{self.scheduler_id}/ORSimScheduler", self.on_receive_message)


    def add_agent(self, unique_id, method, spec):
        ''' '''
        self.agent_collection[unique_id] = {
            # 'object': agent,
            'method': method,
            'spec': spec,
            'step_response': 'waiting'
        }

        kwargs = spec.copy()
        kwargs['scheduler_id'] = self.scheduler_id
        method.delay(**kwargs)

        # # NOTE A beter approach is to implement a handshake protocol
        # time.sleep(0.1)
        while True:
            if self.agent_collection[unique_id]['step_response'] == 'ready':
                print(f'agent {unique_id} is ready')
                break
            else:
                time.sleep(0.001)


    def remove_agent(self, unique_id):
        try:
            self.agent_collection.pop(unique_id)
            logging.info(self.agent_collection)
        except Exception as e:
            logging.exception(str(e))
            print(e)

    # def initialize(self):
    #     # for agent_id, value in self.agent_collection.items():
    #     #     method = value['method']
    #     #     kwargs = value['spec']
    #     #     kwargs['scheduler_id'] = self.scheduler_id
    #     #     method.delay(**kwargs)

    #     #     self.agent_collection[agent_id]['step_response'] = 'waiting'

    #     #     time.sleep(0.1)


    #     # # message = {'action': 'init'}
    #     # # self.agent_messenger.client.publish(f'{self.run_id}/{self.scheduler_id}/ORSimAgent', json.dumps(message))

    #     # # asyncio.run(self.confirm_responses())


    #     self.time = 0


    def on_receive_message(self, client, userdata, message):
        if message.topic == f"{self.run_id}/{self.scheduler_id}/ORSimScheduler":
            payload = json.loads(message.payload.decode('utf-8'))
            # # print(f"Message Recieved: {payload}")
            # if (payload.get('action') == 'completed') or (payload.get('action') == 'ready'):
            #     self.agent_collection[payload.get('agent_id')]['step_response'] = payload.get('action')
            # elif payload.get('action') == 'error':
            #     logging.warning(f'{self.__class__.__name__} received {message.payload = }')
            self.agent_collection[payload.get('agent_id')]['step_response'] = payload.get('action')
            if payload.get('action') == 'error':
                logging.warning(f'{self.__class__.__name__} received {message.payload = }')
            # elif payload.get('action') == 'shutdown':
            #     # agents_shutdown.append(agent_id)
            #     self.remove_agent(payload.get('agent_id'))

    async def confirm_responses(self):
        ''' '''
        start_time = time.time()
        base = 0
        num_confirmed_responses = 0
        while num_confirmed_responses != len(self.agent_collection):
            num_confirmed_responses = 0
            for agent_id, _ in self.agent_collection.items():
                if (self.agent_collection[agent_id]['step_response'] == 'completed') or \
                        (self.agent_collection[agent_id]['step_response'] == 'error') or \
                        (self.agent_collection[agent_id]['step_response'] == 'shutdown'):
                    num_confirmed_responses += 1
            #     if (self.agent_collection[agent_id]['step_response'] == 'shutdown'):
            #         agents_shutdown.append(agent_id)

            # for agent_id in agents_shutdown:
            #     self.remove_agent(agent_id)

            current_time = time.time()
            if current_time - start_time >= 5:
                logging.info(f"Waiting for Agent Response... {num_confirmed_responses}, {len(self.agent_collection)}: {base + (current_time - start_time):0.0f} sec")
                base = base + (current_time - start_time)
                start_time = current_time

            await asyncio.sleep(0.1)

        # # Handle shutdown agents once successfully exiting the loop
        # agents_shutdown = []
        # for agent_id, _ in self.agent_collection.items():
        #     if self.agent_collection[agent_id]['step_response'] == 'shutdown':
        #         agents_shutdown.append(agent_id)

        # logging.info(agents_shutdown)
        # for agent_id in agents_shutdown:
        #     logging.info(f'removing agent {agent_id}')
        #     self.remove_agent(agent_id)

        # print(f"{self.scheduler_id} has {num_confirmed_responses = }")

    async def step(self):

        logging.info(f"{self.scheduler_id} Step: {self.time}")

        if self.time < self.sim_settings['SIM_DURATION']-1:
            message = {'action': 'step', 'time_step': self.time}
        else:
            message = {'action': 'shutdown', 'time_step': self.time}

        for agent_id, _ in self.agent_collection.items():
            self.agent_collection[agent_id]['step_response'] = 'ready'

        # print(f'publish Step to {self.run_id}/ORSimAgent')
        # # [client.publish(topic, json.dumps(message)) for topic in topic_list]
        self.agent_messenger.client.publish(f'{self.run_id}/{self.scheduler_id}/ORSimAgent', json.dumps(message))

        # asyncio.run(self.confirm_responses())
        try:
            start_time = time.time()
            await asyncio.wait_for(self.confirm_responses(), timeout=self.sim_settings['STEP_TIMEOUT'])
            end_time = time.time()
            logging.info(f'{self.scheduler_id} Runtime: {end_time-start_time} sec')

        except asyncio.TimeoutError as e:
            logging.exception(f'Scheduler {self.scheduler_id} timeout beyond {self.sim_settings["STEP_TIMEOUT"] = } while waiting for confirm_responses.\n{self.agent_collection = }')
            raise e

        # Handle shutdown agents once successfully exiting the loop
        agents_shutdown = []
        for agent_id, _ in self.agent_collection.items():
            if self.agent_collection[agent_id]['step_response'] == 'shutdown':
                agents_shutdown.append(agent_id)

        # logging.info(agents_shutdown)
        for agent_id in agents_shutdown:
            # logging.info(f'removing agent {agent_id}')
            self.remove_agent(agent_id)


        self.time += 1

        sim_stat = {
            'status': 'success',
            'end_sim': False,
        }

        if self.time == self.sim_settings['SIM_DURATION']-1:
            sim_stat['end_sim'] = True

        return sim_stat
    # def run_simulation(self):
    #     while self.time < self.sim_settings['SIM_DURATION']:
    #         self.step()

