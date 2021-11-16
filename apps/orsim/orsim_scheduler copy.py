from abc import ABC, abstractclassmethod, abstractmethod
import asyncio, json, logging, time, os, pprint

from datetime import datetime
from apps import orsim
from apps.messenger_service import Messenger

from random import randint, random
from apps.config import orsim_settings

class ORSimScheduler(ABC):

    def __init__(self, run_id, scheduler_id, num_groups=1):
        self.run_id = run_id
        self.scheduler_id = scheduler_id
        self.time = 0
        self.num_groups = num_groups if num_groups > 0 else 1
        self.active_gid = 0

        # self.agent_collection = {}
        # self.agent_stat = {}
        self.agent_collection = {gid:{} for gid in range(self.num_groups)}
        self.agent_stat = {gid:{} for gid in range(self.num_groups)}

        self.agent_credentials = {
            'email': f"{self.run_id}_{self.scheduler_id}_ORSimScheduler",
            'password': "secret_password",
        }

        # self.agent_messenger = Messenger(self.agent_credentials, f"{self.run_id}/{self.scheduler_id}/ORSimScheduler", self.on_receive_message)

        channel_list = [f"{self.run_id}/{self.scheduler_id}/{gid}/ORSimScheduler" for gid in range(self.num_groups)]
        self.agent_messenger = Messenger(self.agent_credentials, channel_list, self.on_receive_message)

        # self.agent_messenger = {}
        # for gid in range(self.num_groups):
        #     self.agent_credentials = {
        #         'email': f"{self.run_id}_{self.scheduler_id}_{gid}_ORSimScheduler",
        #         'password': "secret_password",
        #     }

        #     self.agent_messenger.append(Messenger(self.agent_credentials, f"{self.run_id}/{self.scheduler_id}/{gid}/ORSimScheduler", self.on_receive_message))

        self.pp = pprint.PrettyPrinter(indent=2)


    def add_agent(self, unique_id, method, spec):
        ''' '''
        self.active_gid = randint(0, self.num_groups-1)
        self.agent_collection[self.active_gid][unique_id] = {
            'method': method,
            'spec': spec,
            # 'step_response': 'waiting'
            'step_response': {
                self.time: 'waiting'
            }
        }

        kwargs = spec.copy()
        kwargs['scheduler_id'] = f"{self.scheduler_id}/{self.active_gid}"
        method.delay(**kwargs) # NOTE This starts the Celery Task in a new worker thread

        while True:
            # if self.agent_collection[unique_id]['step_response'] == 'ready':
            if self.agent_collection[self.active_gid][unique_id]['step_response'][self.time] == 'ready':
                logging.debug(f'agent {unique_id} is ready')
                print(f'agent {unique_id} is ready')
                break
            else:
                print(f'Waiting for agent {unique_id}')
                time.sleep(0.001)


    def remove_agent(self, unique_id, gid):
        try:
            # for gid in range(self.num_groups):
            #     popped = self.agent_collection[gid].pop(unique_id, None)
            #     if popped is not None:
            #         break

            # self.agent_collection.pop(unique_id)
            self.agent_collection[gid].pop(unique_id)
            logging.debug(f"agent {unique_id} has left")
            # print(f"agent {unique_id} has left")
        except Exception as e:
            logging.exception(str(e))
            print(e)

    def on_receive_message(self, client, userdata, message):
        logging.warning(message.topic)
        if message.topic == f"{self.run_id}/{self.scheduler_id}/{self.active_gid}/ORSimScheduler":
            payload = json.loads(message.payload.decode('utf-8'))

            response_time_step = payload.get('time_step') if payload.get('time_step') != -1 else self.time

            try:
                # self.agent_collection[payload.get('agent_id')]['step_response'][response_time_step] = payload.get('action')
                self.agent_collection[self.active_gid][payload.get('agent_id')]['step_response'][response_time_step] = payload.get('action')
            except Exception as e:
                logging.exception(str(e))

            if (payload.get('action') == 'error') or (response_time_step is None):
                logging.warning(f'{self.__class__.__name__} received {message.payload = }')

    async def confirm_responses(self):
        ''' '''
        start_time = time.time()
        base = 0
        completed = 0
        shutdown = 0
        error = 0
        # waiting = len(self.agent_collection)
        waiting = len(self.agent_collection[self.active_gid])

        while waiting > 0:
            completed = 0
            shutdown = 0
            error = 0
            waiting = 0
            # for agent_id, _ in self.agent_collection.items():
            #     response = self.agent_collection[agent_id]['step_response'][self.time]
            for agent_id, _ in self.agent_collection[self.active_gid].items():
                response = self.agent_collection[self.active_gid][agent_id]['step_response'][self.time]
                if (response == 'completed'):
                    completed += 1
                elif (response == 'error'):
                    error += 1
                elif (response == 'shutdown'):
                    shutdown += 1
                elif (response == 'waiting'):
                    waiting += 1

            # self.agent_stat[self.time] = {
            self.agent_stat[self.active_gid][self.time] = {
                'completed': completed,
                'error': error,
                'shutdown': shutdown,
                'waiting': waiting,
            }
            current_time = time.time()
            if current_time - start_time >= 5:
                logging.info(f"Waiting for Agent Response... {completed=}, {error=}, {shutdown=}, {waiting=} of {len(self.agent_collection)}: {base + (current_time - start_time):0.0f} sec")
                base = base + (current_time - start_time)
                start_time = current_time

            await asyncio.sleep(0.1)

    async def step(self):

        logging.info(f"{self.scheduler_id} Step: {self.time}")
        self.active_gid = (self.active_gid + 1) % self.num_groups

        if self.time == orsim_settings['SIMULATION_LENGTH_IN_STEPS']-1:
            message = {'action': 'shutdown', 'time_step': self.time}
        else:
            message = {'action': 'step', 'time_step': self.time}

        # for agent_id, _ in self.agent_collection.items():
        #     self.agent_collection[agent_id]['step_response'][self.time] = 'waiting'

        # self.agent_messenger.client.publish(f'{self.run_id}/{self.scheduler_id}/ORSimAgent', json.dumps(message))
        for agent_id, _ in self.agent_collection[self.active_gid].items():
            self.agent_collection[self.active_gid][agent_id]['step_response'][self.time] = 'waiting'

        self.agent_messenger[self.active_gid].client.publish(f'{self.run_id}/{self.scheduler_id}/{self.active_gid}/ORSimAgent', json.dumps(message))

        try:
            start_time = time.time()
            await asyncio.wait_for(self.confirm_responses(), timeout=orsim_settings['STEP_TIMEOUT'])
            end_time = time.time()
            logging.info(f'{self.scheduler_id} Runtime: {(end_time-start_time):0.2f} sec')

        except asyncio.TimeoutError as e:
            logging.exception(f'Scheduler {self.scheduler_id} timeout beyond {orsim_settings["STEP_TIMEOUT"] = } while waiting for confirm_responses.')
            self.step_timeout_handler(e)

        # Handle shutdown agents once successfully exiting the loop
        agents_shutdown = []
        # for agent_id, _ in self.agent_collection.items():
        #     if self.agent_collection[agent_id]['step_response'][self.time] in ['shutdown', 'waiting']:
        #         agents_shutdown.append(agent_id)
        for agent_id, _ in self.agent_collection.items():
            if self.agent_collection[self.active_gid][agent_id]['step_response'][self.time] in ['shutdown', 'waiting']:
                agents_shutdown.append(agent_id)

        for agent_id in agents_shutdown:
            # self.remove_agent(agent_id)
            self.remove_agent(agent_id, self.active_gid)


        self.time += 1

        sim_stat = {
            'status': 'success',
            'end_sim': False,
        }

        if self.time == orsim_settings['SIMULATION_LENGTH_IN_STEPS']-1:
            sim_stat['end_sim'] = True

        return sim_stat


    def step_timeout_handler(self, e):
        ''' '''
        tolerance = orsim_settings['STEP_TIMEOUT_TOLERANCE'] # Max % or agents having network issues


        # if (self.agent_stat[self.time]['waiting'] / len(self.agent_collection)) <= tolerance:
        #     logging.debug(f"agent_stat = {self.pp.pformat(self.agent_stat)}")
        #     logging.warning(f"Unable to receive response from {self.agent_stat[self.time]['waiting']} Agents at {self.time=}. % Error ({(self.agent_stat[self.time]['waiting'] / len(self.agent_collection)):0.3f}) is within {tolerance=}. Continue processing...")
        #     # logging.warning(f'{self.pp.pformat(self.agent_collection)}')
        # else:
        #     logging.error(f"Too many missing messages. % Error ({self.agent_stat[self.time]['waiting'] *100 / len(self.agent_collection)}) exceeded {tolerance=}. Abort...")
        #     logging.error(f'{self.pp.pformat(self.agent_collection)}')
        #     raise e
        if (self.agent_stat[self.active_gid][self.time]['waiting'] / len(self.agent_collection)) <= tolerance:
            logging.debug(f"agent_stat = {self.pp.pformat(self.agent_stat[self.active_gid])}")
            logging.warning(f"Unable to receive response from {self.agent_stat[self.active_gid][self.time]['waiting']} Agents at {self.time=}. % Error ({(self.agent_stat[self.active_gid][self.time]['waiting'] / len(self.agent_collection)):0.3f}) is within {tolerance=}. Continue processing...")
            # logging.warning(f'{self.pp.pformat(self.agent_collection)}')
        else:
            logging.error(f"Too many missing messages. % Error ({self.agent_stat[self.active_gid][self.time]['waiting'] *100 / len(self.agent_collection)}) exceeded {tolerance=}. Abort...")
            logging.error(f'{self.pp.pformat(self.agent_collection)}')
            raise e
