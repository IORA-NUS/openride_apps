from abc import ABC, abstractclassmethod, abstractmethod
import asyncio, json, logging, time, os, pprint

from datetime import datetime
from apps import orsim
from apps.messenger_service import Messenger

from random import random
from apps.config import orsim_settings

class ORSimScheduler(ABC):

    def __init__(self, run_id, scheduler_id):
        self.run_id = run_id
        self.scheduler_id = scheduler_id
        self.time = 0

        self.agent_collection = {}
        self.agent_stat = {}

        self.agent_credentials = {
            'email': f"{self.run_id}_{self.scheduler_id}_ORSimScheduler",
            'password': "secret_password",
        }

        self.agent_messenger = Messenger(self.agent_credentials, f"{self.run_id}/{self.scheduler_id}/ORSimScheduler", self.on_receive_message)


    def add_agent(self, unique_id, method, spec):
        ''' '''
        self.agent_collection[unique_id] = {
            'method': method,
            'spec': spec,
            # 'step_response': 'waiting'
            'step_response': {
                self.time: 'waiting'
            }
        }

        kwargs = spec.copy()
        kwargs['scheduler_id'] = self.scheduler_id
        method.delay(**kwargs) # NOTE This starts the Celery Task in a new worker thread

        while True:
            # if self.agent_collection[unique_id]['step_response'] == 'ready':
            if self.agent_collection[unique_id]['step_response'][self.time] == 'ready':
                print(f'agent {unique_id} is ready')
                break
            else:
                time.sleep(0.001)


    def remove_agent(self, unique_id):
        try:
            self.agent_collection.pop(unique_id)
        except Exception as e:
            logging.exception(str(e))
            print(e)

    def on_receive_message(self, client, userdata, message):
        if message.topic == f"{self.run_id}/{self.scheduler_id}/ORSimScheduler":
            payload = json.loads(message.payload.decode('utf-8'))
            # print(payload.get('time_step'))

            # self.agent_collection[payload.get('agent_id')]['step_response'] = payload.get('action')
            if payload.get('action') == 'ready':
                self.agent_collection[payload.get('agent_id')]['step_response'][self.time] = payload.get('action')
            else:
                self.agent_collection[payload.get('agent_id')]['step_response'][payload.get('time_step')] = payload.get('action')

            if payload.get('action') == 'error':
                logging.warning(f'{self.__class__.__name__} received {message.payload = }')

    async def confirm_responses(self):
        ''' '''
        start_time = time.time()
        base = 0
        completed = 0
        shutdown = 0
        error = 0
        waiting = len(self.agent_collection)
        num_confirmed_responses = 0
        # while num_confirmed_responses != len(self.agent_collection):
        #     num_confirmed_responses = 0
        #     for agent_id, _ in self.agent_collection.items():
        #         if (self.agent_collection[agent_id]['step_response'] == 'completed') or \
        #                 (self.agent_collection[agent_id]['step_response'] == 'error') or \
        #                 (self.agent_collection[agent_id]['step_response'] == 'shutdown'):
        #             num_confirmed_responses += 1

        while waiting > 0:
            completed = 0
            shutdown = 0
            error = 0
            waiting = 0
            for agent_id, _ in self.agent_collection.items():
                response = self.agent_collection[agent_id]['step_response'][self.time]
                if (response == 'completed'):
                    completed += 1
                elif (response == 'error'):
                    error += 1
                elif (response == 'shutdown'):
                    shutdown += 1
                elif (response == 'waiting'):
                    waiting += 1

            self.agent_stat[self.time] = {
                'completed': completed,
                'error': error,
                'shutdown': shutdown,
                'waiting': waiting,
            }
            current_time = time.time()
            if current_time - start_time >= 5:
                # logging.info(f"Waiting for Agent Response... {num_confirmed_responses}, {len(self.agent_collection)}: {base + (current_time - start_time):0.0f} sec")
                logging.info(f"Waiting for Agent Response... {completed=}, {error=}, {shutdown=}, {waiting=} of {len(self.agent_collection)}: {base + (current_time - start_time):0.0f} sec")
                base = base + (current_time - start_time)
                start_time = current_time

            await asyncio.sleep(0.1)

    async def step(self):

        logging.info(f"{self.scheduler_id} Step: {self.time}")

        if self.time < orsim_settings['SIM_DURATION']-1:
            message = {'action': 'step', 'time_step': self.time}
        else:
            message = {'action': 'shutdown', 'time_step': self.time}

        for agent_id, _ in self.agent_collection.items():
            # self.agent_collection[agent_id]['step_response'] = 'waiting'
            self.agent_collection[agent_id]['step_response'][self.time] = 'waiting'

        self.agent_messenger.client.publish(f'{self.run_id}/{self.scheduler_id}/ORSimAgent', json.dumps(message))

        try:
            start_time = time.time()
            await asyncio.wait_for(self.confirm_responses(), timeout=orsim_settings['STEP_TIMEOUT'])
            end_time = time.time()
            logging.info(f'{self.scheduler_id} Runtime: {(end_time-start_time):0.2f} sec')

        except asyncio.TimeoutError as e:
            logging.exception(f'Scheduler {self.scheduler_id} timeout beyond {orsim_settings["STEP_TIMEOUT"] = } while waiting for confirm_responses.')
            pp = pprint.PrettyPrinter(indent=2)
            logging.exception(f'{pp.pformat(self.agent_collection)}')
            raise e

        # Handle shutdown agents once successfully exiting the loop
        agents_shutdown = []
        for agent_id, _ in self.agent_collection.items():
            # if self.agent_collection[agent_id]['step_response'] == 'shutdown':
            if self.agent_collection[agent_id]['step_response'][self.time] == 'shutdown':
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

        if self.time == orsim_settings['SIM_DURATION']-1:
            sim_stat['end_sim'] = True

        return sim_stat

