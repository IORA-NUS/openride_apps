
import os, sys
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

import logging, time, json, traceback
from pprint import pprint
import pandas as pd
from datetime import datetime, time

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
from apps.config import settings
# from apps.orsim_config import driver_settings, passenger_settings, analytics_settings, assignment_settings, orsim_settings
from apps.scenario.scenario_config import driver_settings, passenger_settings, analytics_settings, assignment_settings
from apps.orsim_config import orsim_settings

def to_sec(tm):
    return (tm.hour*3600) + (tm.minute*60) + tm.second


class ScenarioManager():

    driver_collection = None
    passenger_collection = None
    assignment_collection = None
    analytics_collection = None
    orsim_settings = None
    reference_time = datetime(2020, 1, 1, 8, 0, 0)


    def __init__(self, dataset):

        self.dataset = dataset
        behavior_dir = f"{os.path.dirname(os.path.abspath(__file__))}/dataset/{self.dataset}"
        processed_input_dir = f"{os.path.dirname(os.path.abspath(__file__))}/processed_input/{self.dataset}"

        if os.path.exists(behavior_dir): # this is a preexisting dataset, so just read the behaviors and retuen
            logging.warning(f"Loading scenario behaviors {dataset=} from disk")
            self.load_behavior_from_disk(behavior_dir)
        else:
            os.makedirs(behavior_dir)

            if os.path.exists(processed_input_dir):
                logging.warning(f"Generating a new scenario  {dataset=} with behaviors form processed Input")
                self.generate_data_from_processed_inputs(processed_input_dir, behavior_dir)
            else:
                logging.warning(f"Generating a scenario with random behaviors for {dataset=}")
                self.generate_random_data_from_orsim_config(behavior_dir)

        if self.orsim_settings.get('REFERENCE_TIME') is not None:
            self.reference_time = datetime.strptime(self.orsim_settings.get('REFERENCE_TIME'), '%Y-%m-%d %H:%M:%S')

    def load_behavior_from_disk(self, behavior_dir):
        with open(f"{behavior_dir}/driver_behavior.json", "r") as fp:
            self.driver_collection = json.load(fp)

        with open(f"{behavior_dir}/passenger_behavior.json", "r") as fp:
            self.passenger_collection = json.load(fp)

        with open(f"{behavior_dir}/assignment_behavior.json", "r") as fp:
            self.assignment_collection = json.load(fp)

        with open(f"{behavior_dir}/analytics_behavior.json", "r") as fp:
            self.analytics_collection = json.load(fp)

        with open(f"{behavior_dir}/orsim_settings.json", "r") as fp:
            self.orsim_settings = json.load(fp)


    def generate_random_data_from_orsim_config(self, behavior_dir):

        self.driver_collection = {}
        for i in range(driver_settings['num_drivers']):
            agent_id = f"d_{i:06d}"
            behavior = GenerateBehavior.ridehail_driver(agent_id)
            self.driver_collection[agent_id] = behavior

        with open(f"{behavior_dir}/driver_behavior.json", "w") as fp:
            json.dump(self.driver_collection, fp, indent=4, sort_keys=True)


        self.passenger_collection = {}
        for i in range(passenger_settings['num_passengers']):
            agent_id = f"p_{i:06d}"
            behavior = GenerateBehavior.ridehail_passenger(agent_id)
            self.passenger_collection[agent_id] = behavior

        with open(f"{behavior_dir}/passenger_behavior.json", "w") as fp:
            json.dump(self.passenger_collection, fp, indent=4, sort_keys=True)


        self.assignment_collection = {}
        for coverage_area in assignment_settings['coverage_area']: # Support for multiple solvers
            agent_id = f"assignment_{coverage_area['name']}"
            behavior = GenerateBehavior.ridehail_assignment(agent_id, coverage_area)
            self.assignment_collection[agent_id] = behavior

        with open(f"{behavior_dir}/assignment_behavior.json", "w") as fp:
            json.dump(self.assignment_collection, fp, indent=4, sort_keys=True)


        self.analytics_collection = {}
        for i in range(1): # Only one Analytics agent for the moment.
            agent_id = f"analytics_{i:03d}"
            behavior = GenerateBehavior.ridehail_analytics(agent_id)
            self.analytics_collection[agent_id] = behavior

        with open(f"{behavior_dir}/analytics_behavior.json", "w") as fp:
            json.dump(self.analytics_collection, fp, indent=4, sort_keys=True)

        self.orsim_settings = orsim_settings
        with open(f"{behavior_dir}/orsim_settings.json", "w") as fp:
            json.dump(self.orsim_settings, fp, indent=4, sort_keys=True)


    def generate_data_from_processed_inputs(self, processed_input_dir, behavior_dir):
        ''' '''
        reference_start_time = time(4, 0, 0)
        reference_end_time = time(12, 0, 0)

        driver_df = pd.read_csv(f'{processed_input_dir}/driver.csv', parse_dates=['Start_Time', 'End_Time'])
        self.driver_collection = {}
        # for i in range(driver_settings['num_drivers']):
        for index, row in driver_df.iterrows():
            agent_id = f"d_{row['No']:06d}"

            record = {
                'start_time_step': (max(to_sec(row['Start_Time'].time()), to_sec(reference_start_time)) - to_sec(reference_start_time)) // orsim_settings['STEP_INTERVAL'],
                'end_time_step': (min(to_sec(row['End_Time'].time()), to_sec(reference_end_time)) - to_sec(reference_start_time)) // orsim_settings['STEP_INTERVAL'],
                'start_lat': row['Start_Latitude'],
                'start_lon': row['Start_Longitude'],
                'end_lat': row['End_Latitude'],
                'end_lon': row['End_Longitude'],
                'service_score': row['Service_Quality'],

                'coverage_area': 'Singapore_SG',
                'patience': 600,
            }

            behavior = GenerateBehavior.ridehail_driver(agent_id, record)
            self.driver_collection[agent_id] = behavior

        with open(f"{behavior_dir}/driver_behavior.json", "w") as fp:
            json.dump(self.driver_collection, fp, indent=4, sort_keys=True)


            # trip_request_time = record['trip_request_time']

            # pickup_loc = mapping(Point(record["Start_Longitude"], record["Start_Latitude"]))
            # dropoff_loc = mapping(Point(record["End_Longitude"], record["End_Latitude"]))

            # trip_price = record["Fare"]

            # patience = record['Patience_Level']

        passenger_df = pd.read_csv(f'{processed_input_dir}/passenger.csv', parse_dates=['Trip_start_DT', 'Trip_end_DT', 'Start_Time', 'End_Time'])
        self.passenger_collection = {}
        # for i in range(passenger_settings['num_passengers']):
        for index, row in passenger_df.iterrows():
            agent_id = f"p_{row['No']:06d}"

            record = {
                # 'trip_request_time': (to_sec(row['Trip_start_DT'].time()) - to_sec(reference_start_time)) // orsim_settings['STEP_INTERVAL'],
                'trip_request_time': (to_sec(row['Start_Time'].time()) - to_sec(reference_start_time)) // orsim_settings['STEP_INTERVAL'],
                'start_lat': row['Start_Latitude'],
                'start_lon': row['Start_Longitude'],
                'end_lat': row['End_Latitude'],
                'end_lon': row['End_Longitude'],
                'trip_price': row['Fare'],
                # 'patience': row['Patience_Level'],
                'patience': 300, # 600,
            }

            behavior = GenerateBehavior.ridehail_passenger(agent_id, record)
            self.passenger_collection[agent_id] = behavior

        with open(f"{behavior_dir}/passenger_behavior.json", "w") as fp:
            json.dump(self.passenger_collection, fp, indent=4, sort_keys=True)



        self.assignment_collection = {}
        for coverage_area in assignment_settings['coverage_area']: # Support for multiple solvers
            agent_id = f"assignment_{coverage_area['name']}"
            behavior = GenerateBehavior.ridehail_assignment(agent_id, coverage_area)
            self.assignment_collection[agent_id] = behavior

        with open(f"{behavior_dir}/assignment_behavior.json", "w") as fp:
            json.dump(self.assignment_collection, fp, indent=4, sort_keys=True)


        self.analytics_collection = {}
        for i in range(1): # Only one Analytics agent for the moment.
            agent_id = f"analytics_{i:03d}"
            behavior = GenerateBehavior.ridehail_analytics(agent_id)
            self.analytics_collection[agent_id] = behavior

        with open(f"{behavior_dir}/analytics_behavior.json", "w") as fp:
            json.dump(self.analytics_collection, fp, indent=4, sort_keys=True)

        self.orsim_settings = orsim_settings
        with open(f"{behavior_dir}/orsim_settings.json", "w") as fp:
            json.dump(self.orsim_settings, fp, indent=4, sort_keys=True)


if __name__ == '__main__':

    gen = ScenarioManager('comfort_delgro_sampled_10p_20d_20211229_svcdist2_8H')
