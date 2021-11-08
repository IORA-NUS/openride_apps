import os, sys, traceback

from analytics_app.analytics_agent_indie import AnalyticsAgentIndie
from assignment_app.assignment_agent_indie import AssignmentAgentIndie
current_path = os.path.abspath('.')
# parent_path = os.path.dirname(current_path)
sys.path.append(current_path)

import logging, time, json
# from multiprocessing import Pool, Process

# from mesa import Model
# from mesa.time import RandomActivation, BaseScheduler
# # from utils.async_base_scheduler import ParallelBaseScheduler
# from utils.celery_base_scheduler import CeleryBaseScheduler
from driver_app import DriverAgentIndie
from passenger_app import PassengerAgentIndie
from assignment_app import AssignmentAgentIndie
from analytics_app import AnalyticsAgentIndie

from utils import id_generator
from utils.generate_behavior import GenerateBehavior

from datetime import datetime
from dateutil.relativedelta import relativedelta
import pytest
from unittest import mock

from messenger_service import Messenger

import asyncio

from apps.tasks import start_driver, start_passenger, start_analytics, start_assignment

from orsim import ORSimScheduler
from apps.config import settings, driver_settings, passenger_settings, analytics_settings, assignment_settings, orsim_settings

class DistributedOpenRideSimRandomised():


    # def __init__(self, num_drivers, num_passengers):
    def __init__(self):
        self.run_id = id_generator(12)
        self.start_time = datetime(2020,1,1,8,0,0)
        # logging.info(f"{self.run_id = }, {self.start_time = }")
        self.current_time = self.start_time

        # self.sim_settings = settings['SIM_SETTINGS']


        self.agent_scheduler = ORSimScheduler(self.run_id, 'agent_scheduler')
        self.service_scheduler = ORSimScheduler(self.run_id, 'service_scheduler')
        # self.agent_list = []
        # self.agent_registry = {i:[] for i in range(self.sim_settings['SIM_DURATION'])}
        self.agent_registry = {i:[] for i in range(orsim_settings['SIM_DURATION'])}

        # for i in range(num_drivers):
        for i in range(driver_settings['NUM_DRIVERS']):
            agent_id = f"d_{i:06d}"
            # spec = (agent_id, self.run_id, datetime.strftime(self.start_time, '%Y%m%d%H%M%S'))
            # self.driver_agent_spec.append(spec)
            behavior = GenerateBehavior.ridehail_driver(agent_id)
            # print(behavior)
            spec = {
                'unique_id': agent_id,
                'run_id': self.run_id,
                'reference_time': datetime.strftime(self.start_time, '%Y%m%d%H%M%S'),
                'behavior': behavior
            }
            # self.agent_scheduler.add_agent(agent_id, start_driver, spec)

            self.agent_registry[behavior['shift_start_time']].append({
                                                                'unique_id': agent_id,
                                                                'method': start_driver,
                                                                'spec': spec
                                                            })

        # for i in range(num_passengers):
        for i in range(passenger_settings['NUM_PASSENGERS']):
            agent_id = f"p_{i:06d}"
            # spec = (agent_id, self.run_id, datetime.strftime(self.start_time, '%Y%m%d%H%M%S'))
            # self.passenger_agent_spec.append(spec)
            behavior = GenerateBehavior.ridehail_passenger(agent_id)
            spec = {
                'unique_id': agent_id,
                'run_id': self.run_id,
                'reference_time': datetime.strftime(self.start_time, '%Y%m%d%H%M%S'),
                'behavior': behavior
            }
            # self.agent_scheduler.add_agent(agent_id, start_passenger, spec)

            self.agent_registry[behavior['trip_request_time']].append({
                                                                'unique_id': agent_id,
                                                                'method': start_passenger,
                                                                'spec': spec
                                                            })

        # for i in range(1): # Only one Solver for the moment.
        # for coverage_area in self.sim_settings['COVERAGE_AREA']: # Support for multiple solvers
        for coverage_area in assignment_settings['COVERAGE_AREA']: # Support for multiple solvers
            # agent_id = f"assignment_{i:03d}"
            agent_id = f"assignment_{coverage_area['name']}"
            behavior = GenerateBehavior.ridehail_assignment(agent_id, coverage_area)
            spec = {
                'unique_id': agent_id,
                'run_id': self.run_id,
                'reference_time': datetime.strftime(self.start_time, '%Y%m%d%H%M%S'),
                'behavior': behavior
            }
            self.service_scheduler.add_agent(agent_id, start_assignment, spec)

        for i in range(1): # Only one Analytics agent for the moment.
            # agent = AnalyticsAgent(f"analytics_{i:03d}", self)
            agent_id = f"analytics_{i:03d}"
            # spec = (agent_id, self.run_id, datetime.strftime(self.start_time, '%Y%m%d%H%M%S'))
            # self.analytics_agent_spec.append(spec)
            behavior = GenerateBehavior.ridehail_analytics(agent_id)
            spec = {
                'unique_id': agent_id,
                'run_id': self.run_id,
                'reference_time': datetime.strftime(self.start_time, '%Y%m%d%H%M%S'),
                'behavior': behavior
            }
            self.service_scheduler.add_agent(agent_id, start_analytics, spec)

    # def start_schedulers(self):
    #     self.agent_scheduler.initialize()
    #     self.service_scheduler.initialize()

    #     # # NOTE This should bre replaced by a more reliable handshake function within the Initialize method
    #     # time.sleep(1)

    def step(self, i):
        print(f"Simulation Step: {self.agent_scheduler.time} of {orsim_settings['SIM_DURATION']}")

        for item in self.agent_registry[i]:
            self.agent_scheduler.add_agent(**item)

        asyncio.run(self.agent_scheduler.step())
        asyncio.run(self.service_scheduler.step())


    # def run(self):

    #     print('starting passenger')

    #     # with Pool(len(self.passenger_agent_spec)) as p:
    #     #     p.map(PassengerAgentIndie.run, self.passenger_agent_spec)

    #     # with Pool(len(self.driver_agent_spec)) as p:
    #     #     p.map(DriverAgentIndie.run, self.driver_agent_spec)


    #     # with Pool(len(self.analytics_agent_spec)) as p:
    #     #     p.map(AnalyticsAgentIndie.run, self.analytics_agent_spec)

    #     # with Pool(len(self.assignment_agent_spec)) as p:
    #     #     p.map(AssignmentAgentIndie.run, self.assignment_agent_spec)

    #     num_agents = len(self.passenger_agent_spec) + \
    #                 len(self.driver_agent_spec) + \
    #                 len(self.analytics_agent_spec) + \
    #                 len(self.assignment_agent_spec)

    #     pool = Pool(processes=num_agents+1)
    #     pool.map_async(PassengerAgentIndie.run, self.passenger_agent_spec)
    #     pool.map_async(DriverAgentIndie.run, self.driver_agent_spec)
    #     pool.map_async(AnalyticsAgentIndie.run, self.analytics_agent_spec)
    #     pool.map_async(AssignmentAgentIndie.run, self.assignment_agent_spec)

    #     time.sleep(10)
    #     self.sim_scheduler.run_simulation()

    #     # for spec in self.driver_agent_spec:
    #     #     p = Process(target=DriverAgentIndie.run, args=(spec,))
    #     #     p.start()
    #     #     p.join()

    #     # for spec in self.passenger_agent_spec:
    #     #     p = Process(target=PassengerAgentIndie.run, args=(spec,))
    #     #     p.start()
    #     #     p.join()

    #     # for spec in self.analytics_agent_spec:
    #     #     p = Process(target=AnalyticsAgentIndie.run, args=(spec,))
    #     #     p.start()
    #     #     p.join()

    #     # for spec in self.assignment_agent_spec:
    #     #     p = Process(target=AssignmentAgentIndie.run, args=(spec,))
    #     #     p.start()
    #     #     p.join()


if __name__ == '__main__':

    log_dir = f"{os.path.dirname(os.path.abspath(__file__))}/log"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    logging.basicConfig(filename=f'{log_dir}/app.log', level=settings['LOG_LEVEL'], filemode='w')

    # num_drivers =  settings['SIM_SETTINGS']['NUM_DRIVERS'] # 2 # 2 #50
    # num_passengers =  settings['SIM_SETTINGS']['NUM_PASSENGERS'] # 10 # 10 #100
    # sim = DistributedOpenRideSimRandomised(num_drivers, num_passengers)
    sim = DistributedOpenRideSimRandomised()

    print(f"Initializing Simulation with {sim.run_id = }")

    # strategy = 'CELERY' # 'MULTIPROCESSING'
    # # sim.run()
    if settings['EXECUTION_STRATEGY'] == 'CELERY':

        # sim.start_schedulers()

        # for i in range(settings['SIM_SETTINGS']['SIM_DURATION']):
        for i in range(orsim_settings['SIM_DURATION']):
            try:
                sim.step(i)
            except Exception as e:
                print(e)
                break

    # elif settings['EXECUTION_STRATEGY'] == 'MULTIPROCESSING':
    #     sim.run()

    print(f"{sim.run_id = }")

