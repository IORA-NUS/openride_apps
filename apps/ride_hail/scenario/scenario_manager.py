
import os, sys, logging, time, json, traceback, pandas as pd
from datetime import datetime, time
from .generate_behavior import GenerateBehavior
from apps.config import simulation_domains
from .scenario_config import driver_settings, passenger_settings, analytics_settings, assignment_settings
from apps.orsim_config import orsim_settings
from apps.common.base_scenario_manager import BaseScenarioManager

def to_sec(tm):
    return (tm.hour*3600) + (tm.minute*60) + tm.second

class ScenarioManager(BaseScenarioManager):
    """
    Ridehail-specific ScenarioManager, inherits from BaseScenarioManager.
    """
    def __init__(self, scenario_name, domain, run_data_dir=None):
        self.driver_collection = None
        self.passenger_collection = None
        self.assignment_collection = None
        self.analytics_collection = None
        self.orsim_settings = None
        self.reference_time = datetime(2020, 1, 1, 8, 0, 0)

        super().__init__(scenario_name, domain, run_data_dir)

        # self.domain = simulation_domains['ridehail']

    def get_run_config_meta(self):
        meta = {
            'num_driver_agents': len(self.get_agent_collection('driver')),
            'num_passenger_agents': len(self.get_agent_collection('passenger')),
            'num_analytics_agents': len(self.get_agent_collection('analytics')),
            'num_assignment_agents': len(self.get_agent_collection('assignment')),
            'simulation_settings': self.orsim_settings,
            'services': {
                'assignment_agents': self.get_agent_collection('assignment'),
                'analytics_agents': self.get_agent_collection('analytics'),
            }
        }
        return meta

    def behaviors_exist_on_disk(self):
        files = [
            'driver_behavior.json', 'passenger_behavior.json',
            'assignment_behavior.json', 'analytics_behavior.json', 'orsim_settings.json']
        return all(os.path.exists(os.path.join(self.behavior_dir, fname)) for fname in files)

    def load_behaviors_from_disk(self):
        with open(f"{self.behavior_dir}/driver_behavior.json", "r") as fp:
            self.driver_collection = json.load(fp)
        with open(f"{self.behavior_dir}/passenger_behavior.json", "r") as fp:
            self.passenger_collection = json.load(fp)
        with open(f"{self.behavior_dir}/assignment_behavior.json", "r") as fp:
            self.assignment_collection = json.load(fp)
        with open(f"{self.behavior_dir}/analytics_behavior.json", "r") as fp:
            self.analytics_collection = json.load(fp)
        with open(f"{self.behavior_dir}/orsim_settings.json", "r") as fp:
            self.orsim_settings = json.load(fp)
        if self.orsim_settings.get('REFERENCE_TIME') is not None:
            self.reference_time = datetime.strptime(self.orsim_settings.get('REFERENCE_TIME'), '%Y-%m-%d %H:%M:%S')
        # Ensure collections dict is always updated
        self.collections['driver'] = self.driver_collection
        self.collections['passenger'] = self.passenger_collection
        self.collections['assignment'] = self.assignment_collection
        self.collections['analytics'] = self.analytics_collection

    def generate_random_behaviors(self):
        self.driver_collection = {}
        for i in range(driver_settings['num_drivers']):
            agent_id = f"d_{i:06d}"
            behavior = GenerateBehavior.ridehail_driver(agent_id)
            self.driver_collection[agent_id] = behavior
        with open(f"{self.behavior_dir}/driver_behavior.json", "w") as fp:
            json.dump(self.driver_collection, fp, indent=4, sort_keys=True)

        self.passenger_collection = {}
        for i in range(passenger_settings['num_passengers']):
            agent_id = f"p_{i:06d}"
            behavior = GenerateBehavior.ridehail_passenger(agent_id)
            self.passenger_collection[agent_id] = behavior
        with open(f"{self.behavior_dir}/passenger_behavior.json", "w") as fp:
            json.dump(self.passenger_collection, fp, indent=4, sort_keys=True)

        self.assignment_collection = {}
        for coverage_area in assignment_settings['coverage_area']:
            agent_id = f"assignment_{coverage_area['name']}"
            behavior = GenerateBehavior.ridehail_assignment(agent_id, coverage_area)
            self.assignment_collection[agent_id] = behavior
        with open(f"{self.behavior_dir}/assignment_behavior.json", "w") as fp:
            json.dump(self.assignment_collection, fp, indent=4, sort_keys=True)

        self.analytics_collection = {}
        for i in range(1):
            agent_id = f"analytics_{i:03d}"
            behavior = GenerateBehavior.ridehail_analytics(agent_id)
            self.analytics_collection[agent_id] = behavior
        with open(f"{self.behavior_dir}/analytics_behavior.json", "w") as fp:
            json.dump(self.analytics_collection, fp, indent=4, sort_keys=True)

        self.orsim_settings = orsim_settings    # NOTE. THERE MUST BE SOME DEFAULT ORSIM_SETTINGS, EITHER LOADED FROM DISK OR DEFINED IN CODE, OTHERWISE SIMULATION WILL BREAK. THIS IS BECAUSE SIMULATION RUNTIME EXPECTS CERTAIN KEYS TO BE PRESENT IN ORSIM_SETTINGS FOR SCHEDULING AND OTHER LOGIC.
        self.orsim_settings['DOMAIN'] = self.domain
        with open(f"{self.behavior_dir}/orsim_settings.json", "w") as fp:
            json.dump(self.orsim_settings, fp, indent=4, sort_keys=True)
        # Ensure collections dict is always updated
        self.collections['driver'] = self.driver_collection
        self.collections['passenger'] = self.passenger_collection
        self.collections['assignment'] = self.assignment_collection
        self.collections['analytics'] = self.analytics_collection

    def load_or_generate_behaviors(self):
        print(f"Checking for existing behaviors on disk for scenario {self.scenario_name} in {self.behavior_dir}...")
        if self.behaviors_exist_on_disk():
            print(f"Found existing behaviors on disk for scenario {self.scenario_name}. Loading...")
            logging.warning(f"Loading scenario behaviors {self.scenario_name=} from disk in {self.behavior_dir}")
            self.load_behaviors_from_disk()
        elif os.path.exists(self.processed_input_dir):
            print(f"No existing behaviors found on disk, but found processed input data for scenario {self.scenario_name} in {self.processed_input_dir}. Generating behaviors from processed input...")
            logging.warning(f"Generating a new scenario  {self.scenario_name=} with behaviors from processed Input in {self.processed_input_dir}")
            self.generate_data_from_processed_inputs(self.processed_input_dir, self.behavior_dir)
        else:
            print(f"No existing behaviors or processed input data found for scenario {self.scenario_name}. Generating random behaviors...")
            logging.warning(f"Generating a scenario with random behaviors for {self.scenario_name=}")
            self.generate_random_behaviors()

    def generate_data_from_processed_inputs(self, processed_input_dir, behavior_dir):
        # ...existing code from previous generate_data_from_processed_inputs...
        reference_start_time = time(4, 0, 0)
        reference_end_time = time(12, 0, 0)
        driver_df = pd.read_csv(f'{processed_input_dir}/driver.csv', parse_dates=['Start_Time', 'End_Time'])
        self.collections['driver'] = {}

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
            self.collections['driver'][agent_id] = behavior
        with open(f"{behavior_dir}/driver_behavior.json", "w") as fp:
                json.dump(self.collections['driver'], fp, indent=4, sort_keys=True)

        passenger_df = pd.read_csv(f'{processed_input_dir}/passenger.csv', parse_dates=['Trip_start_DT', 'Trip_end_DT', 'Start_Time', 'End_Time'])
        self.collections['passenger'] = {}
        for index, row in passenger_df.iterrows():
            agent_id = f"p_{row['No']:06d}"
            record = {
                'trip_request_time': (to_sec(row['Start_Time'].time()) - to_sec(reference_start_time)) // orsim_settings['STEP_INTERVAL'],
                'start_lat': row['Start_Latitude'],
                'start_lon': row['Start_Longitude'],
                'end_lat': row['End_Latitude'],
                'end_lon': row['End_Longitude'],
                'trip_price': row['Fare'],
                'patience': 300,
            }
            behavior = GenerateBehavior.ridehail_passenger(agent_id, record)
            self.collections['passenger'][agent_id] = behavior
        with open(f"{behavior_dir}/passenger_behavior.json", "w") as fp:
                json.dump(self.collections['passenger'], fp, indent=4, sort_keys=True)

        self.assignment_collection = {}
        for coverage_area in assignment_settings['coverage_area']:
            agent_id = f"assignment_{coverage_area['name']}"
            behavior = GenerateBehavior.ridehail_assignment(agent_id, coverage_area)
            self.assignment_collection[agent_id] = behavior
        with open(f"{behavior_dir}/assignment_behavior.json", "w") as fp:
            json.dump(self.assignment_collection, fp, indent=4, sort_keys=True)

        self.analytics_collection = {}
        for i in range(1):
            agent_id = f"analytics_{i:03d}"
            behavior = GenerateBehavior.ridehail_analytics(agent_id)
            self.analytics_collection[agent_id] = behavior
        with open(f"{behavior_dir}/analytics_behavior.json", "w") as fp:
            json.dump(self.analytics_collection, fp, indent=4, sort_keys=True)

        self.orsim_settings = orsim_settings
        self.orsim_settings['DOMAIN'] = self.domain
        with open(f"{behavior_dir}/orsim_settings.json", "w") as fp:
            json.dump(self.orsim_settings, fp, indent=4, sort_keys=True)

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
        self.orsim_settings['DOMAIN'] = self.domain
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
        self.orsim_settings['DOMAIN'] = self.domain
        with open(f"{behavior_dir}/orsim_settings.json", "w") as fp:
            json.dump(self.orsim_settings, fp, indent=4, sort_keys=True)


if __name__ == '__main__':

    gen = ScenarioManager('comfort_delgro_sampled_10p_20d_20211229_svcdist2_8H')
