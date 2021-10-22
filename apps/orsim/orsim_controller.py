from abc import ABC, abstractclassmethod, abstractmethod
import asyncio, json, logging

from datetime import datetime
from apps.messenger_service import Messenger

from random import random


class ORSimController(ABC):

    def __init__(self, sim_settings, run_id):
        self.sim_settings = sim_settings
        self.run_id = run_id
        self.time = 0

        self.agent_collection = {}

        self.agent_credentials = {
            'email': f"{self.run_id}_ORSimController",
            'password': "secret_password",
        }

        self.agent_messenger = Messenger(run_id, self.agent_credentials, f"ORSimController", self.on_receive_message)


    def add_agent(self, unique_id):
        ''' '''
        self.agent_collection[unique_id] = {
            # 'object': agent,
            'step_response': 'waiting'
        }

    def remove_agent(self, unique_id):
        try:
            self.agent_collection.pop(unique_id)
        except Exception as e:
            logging.exception(str(e))
            print(e)

    def on_receive_message(self, client, userdata, message):
        if message.topic == f"{self.run_id}/ORSimController":
            payload = json.loads(message.payload.decode('utf-8'))
            # print(f"Message Recieved: {payload}")
            if payload.get('action') == 'completed':
                self.agent_collection[payload.get('agent_id')]['step_response'] = payload.get('action')
            elif payload.get('action') == 'error':
                logging.warning(f'{self.__class__.__name__} received {message.payload = }')


    async def confirm_responses(self):
        ''' '''
        num_confirmed_responses = 0
        while num_confirmed_responses != len(self.agent_collection):
            num_confirmed_responses = 0
            for agent_id, _ in self.agent_collection.items():
                if self.agent_collection[agent_id]['step_response'] == 'completed':
                    num_confirmed_responses += 1

            # print(self.agent_collection, num_confirmed_responses)
            # # await asyncio.sleep(random()/10)
            await asyncio.sleep(1)

        print(f"{num_confirmed_responses = }")

    def step(self):

        logging.info(f"SimController Time: {self.time}")

        message = {'action': 'step', 'time_step': self.time}

        for agent_id, _ in self.agent_collection.items():
            self.agent_collection[agent_id]['step_response'] = 'waiting'

        # print(f'publish Step to {self.run_id}/ORSimAgent')
        # # [client.publish(topic, json.dumps(message)) for topic in topic_list]
        self.agent_messenger.client.publish(f'{self.run_id}/ORSimAgent', json.dumps(message))

        asyncio.run(self.confirm_responses())

        self.time += 1

    def run_simulation(self):
        while self.time < self.sim_settings['SIM_DURATION']:
            self.step()

