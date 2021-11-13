
import os, sys
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

import logging, time, json, traceback

from analytics_app.analytics_agent_indie import AnalyticsAgentIndie
from assignment_app.assignment_agent_indie import AssignmentAgentIndie
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


    def __init__(self):
        self.run_id = id_generator(12)
        self.start_time = datetime(2020,1,1,8,0,0)
        # logging.info(f"{self.run_id = }, {self.start_time = }")
        self.current_time = self.start_time



        self.agent_scheduler = ORSimScheduler(self.run_id, 'agent_scheduler')
        self.service_scheduler = ORSimScheduler(self.run_id, 'service_scheduler')
        self.agent_registry = {i:[] for i in range(orsim_settings['SIMULATION_LENGTH_IN_STEPS'])}

        for i in range(driver_settings['NUM_DRIVERS']):
            agent_id = f"d_{i:06d}"
            behavior = GenerateBehavior.ridehail_driver(agent_id)

            spec = {
                'unique_id': agent_id,
                'run_id': self.run_id,
                'reference_time': datetime.strftime(self.start_time, '%Y%m%d%H%M%S'),
                'behavior': behavior
            }

            self.agent_registry[behavior['shift_start_time']].append({
                                                                'unique_id': agent_id,
                                                                'method': start_driver,
                                                                'spec': spec
                                                            })

        for i in range(passenger_settings['NUM_PASSENGERS']):
            agent_id = f"p_{i:06d}"
            behavior = GenerateBehavior.ridehail_passenger(agent_id)
            spec = {
                'unique_id': agent_id,
                'run_id': self.run_id,
                'reference_time': datetime.strftime(self.start_time, '%Y%m%d%H%M%S'),
                'behavior': behavior
            }

            self.agent_registry[behavior['trip_request_time']].append({
                                                                'unique_id': agent_id,
                                                                'method': start_passenger,
                                                                'spec': spec
                                                            })

        for coverage_area in assignment_settings['COVERAGE_AREA']: # Support for multiple solvers
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
            agent_id = f"analytics_{i:03d}"
            behavior = GenerateBehavior.ridehail_analytics(agent_id)
            spec = {
                'unique_id': agent_id,
                'run_id': self.run_id,
                'reference_time': datetime.strftime(self.start_time, '%Y%m%d%H%M%S'),
                'behavior': behavior
            }
            self.service_scheduler.add_agent(agent_id, start_analytics, spec)

    def step(self, i):
        print(f"Simulation Step: {self.agent_scheduler.time} of {orsim_settings['SIMULATION_LENGTH_IN_STEPS']}")

        # IMPORTANT Make sure agents are added into the scheduler before step
        # add_agent is a blocking process and ensures the agent is ready to listen to step()
        for item in self.agent_registry[i]:
            self.agent_scheduler.add_agent(**item)

        # step() assumes all agents will be ready to respond to step message
        asyncio.run(self.agent_scheduler.step())
        asyncio.run(self.service_scheduler.step())


if __name__ == '__main__':

    log_dir = f"{os.path.dirname(os.path.abspath(__file__))}/log"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    logging.basicConfig(filename=f'{log_dir}/app.log', level=settings['LOG_LEVEL'], filemode='w')

    sim = DistributedOpenRideSimRandomised()

    print(f"Initializing Simulation with {sim.run_id = }")

    # strategy = 'CELERY' # 'MULTIPROCESSING'
    if settings['EXECUTION_STRATEGY'] == 'CELERY':

        for i in range(orsim_settings['SIMULATION_LENGTH_IN_STEPS']):
            try:
                sim.step(i)
            except Exception as e:
                print(e)
                break

    # elif settings['EXECUTION_STRATEGY'] == 'MULTIPROCESSING':
    #     sim.run()

    print(f"{sim.run_id = }")

