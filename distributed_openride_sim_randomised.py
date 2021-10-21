import os, sys, traceback

from analytics_app.analytics_agent_indie import AnalyticsAgentIndie
from assignment_app.assignment_agent_indie import AssignmentAgentIndie
current_path = os.path.abspath('.')
# parent_path = os.path.dirname(current_path)
sys.path.append(current_path)

import logging, time
from multiprocessing import Pool, Process

# from mesa import Model
# from mesa.time import RandomActivation, BaseScheduler
# # from utils.async_base_scheduler import ParallelBaseScheduler
# from utils.celery_base_scheduler import CeleryBaseScheduler
from driver_app import DriverAgentIndie
from passenger_app import PassengerAgentIndie
from assignment_app import AssignmentAgentIndie
from analytics_app import AnalyticsAgentIndie
from config import settings
from utils import id_generator

from datetime import datetime
from dateutil.relativedelta import relativedelta
import pytest
from unittest import mock

from messenger_service import Messenger

import asyncio

from orsim import ORSimController

class DistributedOpenRideSimRandomised():


    def __init__(self, num_drivers, num_passengers):
        self.run_id = id_generator(12)
        self.start_time = datetime(2020,1,1,8,0,0)
        logging.info(f"{self.run_id = }, {self.start_time = }")
        self.current_time = self.start_time

        # message_channel = f"sim_admin_{self.run_id}"

        # self.messenger = Messenger(self.run_id,
        #                             credentials={'email': f'{message_channel}@test.io', 'password': 'password'},
        #                             channel_id=message_channel,
        #                             on_message=self.receive_message)

        # self.num_agents = num_drivers + num_passengers

        # self.service_schedule = BaseScheduler(self)
        # # self.driver_schedule = RandomActivation(self)
        # # self.passenger_schedule = RandomActivation(self)
        # self.driver_schedule = CeleryBaseScheduler(self)
        # self.passenger_schedule = CeleryBaseScheduler(self)

        self.driver_agent_spec = []
        self.passenger_agent_spec = []
        self.assignment_agent_spec = []
        self.analytics_agent_spec = []

        self.sim_settings = settings['SIM_SETTINGS']


        self.sim_controller = ORSimController(self.sim_settings, self.run_id)
        self.agent_list = []

        for i in range(num_drivers):
            agent_id = f"d_{i:06d}"
            self.driver_agent_spec.append((agent_id, self.run_id, datetime.strftime(self.start_time, '%Y%m%d%H%M%S')))
            self.sim_controller.add_agent(agent_id)

        for i in range(num_passengers):
            agent_id = f"p_{i:06d}"
            self.passenger_agent_spec.append((agent_id, self.run_id, datetime.strftime(self.start_time, '%Y%m%d%H%M%S')))

            self.sim_controller.add_agent(agent_id)

        for i in range(1): # Only one Solver for the moment.
            # agent = AssignmentAgent(f"assignment_{i:03d}", self)
            agent_id = f"assignment_{i:03d}"
            self.assignment_agent_spec.append((agent_id, self.run_id, datetime.strftime(self.start_time, '%Y%m%d%H%M%S')))
            self.sim_controller.add_agent(agent_id)

        for i in range(1): # Only one Solver for the moment.
            # agent = AnalyticsAgent(f"analytics_{i:03d}", self)
            agent_id = f"analytics_{i:03d}"
            self.analytics_agent_spec.append((agent_id, self.run_id, datetime.strftime(self.start_time, '%Y%m%d%H%M%S')))
            self.sim_controller.add_agent(agent_id)

    def run(self):

        print('starting passenger')

        # with Pool(len(self.passenger_agent_spec)) as p:
        #     p.map(PassengerAgentIndie.run, self.passenger_agent_spec)

        # with Pool(len(self.driver_agent_spec)) as p:
        #     p.map(DriverAgentIndie.run, self.driver_agent_spec)


        # with Pool(len(self.analytics_agent_spec)) as p:
        #     p.map(AnalyticsAgentIndie.run, self.analytics_agent_spec)

        # with Pool(len(self.assignment_agent_spec)) as p:
        #     p.map(AssignmentAgentIndie.run, self.assignment_agent_spec)

        num_agents = len(self.passenger_agent_spec) + \
                    len(self.driver_agent_spec) + \
                    len(self.analytics_agent_spec) + \
                    len(self.assignment_agent_spec)

        pool = Pool(processes=num_agents+1)
        pool.map_async(PassengerAgentIndie.run, self.passenger_agent_spec)
        pool.map_async(DriverAgentIndie.run, self.driver_agent_spec)
        pool.map_async(AnalyticsAgentIndie.run, self.analytics_agent_spec)
        pool.map_async(AssignmentAgentIndie.run, self.assignment_agent_spec)

        time.sleep(10)
        self.sim_controller.run_simulation()

        # for spec in self.driver_agent_spec:
        #     p = Process(target=DriverAgentIndie.run, args=(spec,))
        #     p.start()
        #     p.join()

        # for spec in self.passenger_agent_spec:
        #     p = Process(target=PassengerAgentIndie.run, args=(spec,))
        #     p.start()
        #     p.join()

        # for spec in self.analytics_agent_spec:
        #     p = Process(target=AnalyticsAgentIndie.run, args=(spec,))
        #     p.start()
        #     p.join()

        # for spec in self.assignment_agent_spec:
        #     p = Process(target=AssignmentAgentIndie.run, args=(spec,))
        #     p.start()
        #     p.join()


if __name__ == '__main__':

    logging.basicConfig(filename='app.log', level=settings['LOG_LEVEL'], filemode='w')

    num_drivers =  settings['SIM_SETTINGS']['NUM_DRIVERS'] # 2 # 2 #50
    num_passengers =  settings['SIM_SETTINGS']['NUM_PASSENGERS'] # 10 # 10 #100
    sim = DistributedOpenRideSimRandomised(num_drivers, num_passengers)

    # strategy = 'CELERY' # 'MULTIPROCESSING'
    # # sim.run()
    if settings['EXECUTION_STRATEGY'] == 'CELERY':
        from apps.tasks import execute_step
        for spec in sim.driver_agent_spec:
            execute_step.delay('DriverAgentIndie', spec)
            time.sleep(0.1)

        for spec in sim.passenger_agent_spec:
            execute_step.delay('PassengerAgentIndie', spec)
            time.sleep(0.1)

        for spec in sim.analytics_agent_spec:
            execute_step.delay('AnalyticsAgentIndie', spec)
            time.sleep(0.1)

        for spec in sim.assignment_agent_spec:
            execute_step.delay('AssignmentAgentIndie', spec)
            time.sleep(0.1)

        time.sleep(5)

        sim.sim_controller.run_simulation()

    if settings['EXECUTION_STRATEGY'] == 'MULTIPROCESSING':
        sim.run()

    logging.info(f"{sim.run_id = }")

