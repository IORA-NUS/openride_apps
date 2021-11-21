
import os, sys
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

import logging, time, json, traceback
from pprint import pprint

# from analytics_app.analytics_agent_indie import AnalyticsAgentIndie
# from assignment_app.assignment_agent_indie import AssignmentAgentIndie
# from driver_app import DriverAgentIndie
# from passenger_app import PassengerAgentIndie
# from assignment_app import AssignmentAgentIndie
# from analytics_app import AnalyticsAgentIndie

# from apps.utils import id_generator
from apps.scenario.generate_behavior import GenerateBehavior

# from datetime import datetime
# from dateutil.relativedelta import relativedelta
# import pytest
# from unittest import mock

# from messenger_service import Messenger

import asyncio

# from apps.tasks import start_driver, start_passenger, start_analytics, start_assignment

# from orsim import ORSimScheduler
from apps.config import settings, driver_settings, passenger_settings, analytics_settings, assignment_settings, orsim_settings

class ScenarioManager():

    def __init__(self, dataset):

        self.dataset = dataset
        output_dir = f"{os.path.dirname(os.path.abspath(__file__))}/dataset/{self.dataset}"
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

            # logging.debug('Creating New folder')

            self.driver_collection = {}
            for i in range(driver_settings['NUM_DRIVERS']):
                agent_id = f"d_{i:06d}"
                behavior = GenerateBehavior.ridehail_driver(agent_id)
                self.driver_collection[agent_id] = behavior

            with open(f"{output_dir}/driver_behavior.json", "w") as fp:
                json.dump(self.driver_collection, fp, indent=4, sort_keys=True)


            self.passenger_collection = {}
            for i in range(passenger_settings['NUM_PASSENGERS']):
                agent_id = f"p_{i:06d}"
                behavior = GenerateBehavior.ridehail_passenger(agent_id)
                self.passenger_collection[agent_id] = behavior

            with open(f"{output_dir}/passenger_behavior.json", "w") as fp:
                json.dump(self.passenger_collection, fp, indent=4, sort_keys=True)


            self.assignment_collection = {}
            for coverage_area in assignment_settings['COVERAGE_AREA']: # Support for multiple solvers
                agent_id = f"assignment_{coverage_area['name']}"
                behavior = GenerateBehavior.ridehail_assignment(agent_id, coverage_area)
                self.assignment_collection[agent_id] = behavior

            with open(f"{output_dir}/assignment_behavior.json", "w") as fp:
                json.dump(self.assignment_collection, fp, indent=4, sort_keys=True)


            self.analytics_collection = {}
            for i in range(1): # Only one Analytics agent for the moment.
                agent_id = f"analytics_{i:03d}"
                behavior = GenerateBehavior.ridehail_analytics(agent_id)
                self.analytics_collection[agent_id] = behavior

            with open(f"{output_dir}/analytics_behavior.json", "w") as fp:
                json.dump(self.analytics_collection, fp, indent=4, sort_keys=True)

            self.orsim_settings = orsim_settings
            with open(f"{output_dir}/orsim_settings.json", "w") as fp:
                json.dump(self.orsim_settings, fp, indent=4, sort_keys=True)

        else:
            # logging.debug('Reading from folder')
            with open(f"{output_dir}/driver_behavior.json", "r") as fp:
                self.driver_collection = json.load(fp)

            with open(f"{output_dir}/passenger_behavior.json", "r") as fp:
                self.passenger_collection = json.load(fp)

            with open(f"{output_dir}/assignment_behavior.json", "r") as fp:
                self.assignment_collection = json.load(fp)

            with open(f"{output_dir}/analytics_behavior.json", "r") as fp:
                self.analytics_collection = json.load(fp)

            with open(f"{output_dir}/orsim_settings.json", "r") as fp:
                self.orsim_settings = json.load(fp)

if __name__ == '__main__':

    gen = ScenarioManager('20211117_D50_P100_60mX30s_clementi_compromise')
