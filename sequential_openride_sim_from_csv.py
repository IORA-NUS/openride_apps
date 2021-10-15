import os, sys, traceback
current_path = os.path.abspath('.')
# parent_path = os.path.dirname(current_path)
sys.path.append(current_path)

from mesa import Model
from mesa.time import RandomActivation, BaseScheduler
from utils.async_base_scheduler import ParallelBaseScheduler
# from utils.celery_base_scheduler import ParallelBaseScheduler
from driver_app import DriverAgent
from passenger_app import PassengerAgent
from assignment_app import AssignmentAgent
from analytics_app import AnalyticsAgent
from config import settings
from utils import id_generator

from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import pytest
from unittest import mock

import pandas as pd
from shapely.geometry import Point, mapping


import asyncio

class SequentialOpenRideSimFromCSV(Model):


    def __init__(self, scenario_name):
        self.run_id = id_generator(12)
        self.start_time = datetime(2020,1,1,0,0,0)
        print(f"{self.run_id = }, {self.start_time = }")
        self.current_time = self.start_time

        # self.num_agents = num_drivers + num_passengers

        self.service_schedule = BaseScheduler(self)
        # self.driver_schedule = RandomActivation(self)
        # self.passenger_schedule = RandomActivation(self)
        self.driver_schedule = ParallelBaseScheduler(self)
        self.passenger_schedule = ParallelBaseScheduler(self)

        self.driver_agents = []
        self.passenger_agents = []

        data_dir = f"{os.path.dirname(os.path.abspath(__file__))}/data/scenario/{scenario_name}"

        driver_df =  pd.read_csv(f"{data_dir}/driver_sample.csv", index_col=0,
                                dtype={
                                    "Driver_ID": 'str',
                                    "Start_Time": 'str',
                                    "Start_Post": 'int',
                                    "Start_Latitude": 'float',
                                    "Start_Longitude": 'float',
                                    "End_Time": 'str',
                                    "End_Post": 'int',
                                    "End_Latitude": 'float',
                                    "End_Longitude": 'float',
                                    "Schedule_Date": 'str',
                                    "Service_Quality": 'float',
                                    "Start_Slot": 'int',
                                    "End_Slot": 'int',
                                    "Start_DistrictCode": 'int'
                                },
                                parse_dates=['Start_Time', 'End_Time', 'Schedule_Date'],
                                # on_bad_lines='skip',
                            )
        passenger_df =  pd.read_csv(f"{data_dir}/order_sample.csv", index_col=0,
                                dtype={
                                    "Booking_ID": 'str',
                                    "Vehicle_ID": 'str',
                                    "Driver_ID": 'str',
                                    "Trip_start_DT": 'str',
                                    "Trip_end_DT": 'str',
                                    "Start_Post": 'int',
                                    "Start_Latitude": 'float',
                                    "Start_Longitude": 'float',
                                    "End_Post": 'int',
                                    "End_Latitude": 'float',
                                    "End_Longitude": 'float',
                                    "Fare": 'float',
                                    "Distance": 'float',
                                    "Order_Type": 'str',
                                    "Start_Time": 'str',
                                    "End_Time": 'str',
                                    "Start_Slot": 'str',
                                    "End_Slot": 'str',
                                    "Patience_Level": 'int',
                                    "Start,_DistrictCode": 'int',
                                    "End_DistrictCode": 'int',
                                },
                                parse_dates=['Trip_start_DT', 'Trip_end_DT', 'Start_Time', 'End_Time'],
                                # on_bad_lines='skip',
                            )

        # Filter by time
        driver_df.dropna(inplace=True)
        # print(driver_df['Start_Time'].dtype)
        driver_df['Start_Time'] = driver_df['Start_Time'].apply(lambda dt: dt.replace(day=1, month=1, year=2020))
        driver_df['End_Time'] = driver_df['End_Time'].apply(lambda dt: dt.replace(day=1, month=1, year=2020))

        driver_df = driver_df[(driver_df['Start_Time'] >= datetime(2020, 1, 1, 0, 0, 0)) & (driver_df['Start_Time'] <= datetime(2020, 1, 1, 0, 1, 0))]
        print(driver_df)

        # create drivers
        for _, row in driver_df.iterrows():
            s_time = row["Start_Time"].to_pydatetime()
            shift_start_time = self.start_time + timedelta(hours=s_time.hour, minutes=s_time.minute, seconds=s_time.second, )
            e_time = row["End_Time"].to_pydatetime()
            shift_end_time = self.start_time + timedelta(hours=e_time.hour, minutes=e_time.minute, seconds=e_time.second, )
            behavior = {
                'email': f'{row["Driver_ID"]}@test.com',
                'password': 'password',

                # 'start_time': 0,
                'shift_start_time': (shift_start_time - self.start_time).seconds // settings['SIM_STEP_SIZE'],
                'shift_end_time': (shift_end_time - self.start_time).seconds // settings['SIM_STEP_SIZE'], # settings['SIM_DURATION'], #shift_start_time + (settings['SIM_DURATION']//2),


                'init_loc': mapping(Point(row["Start_Longitude"], row["Start_Latitude"])), # shapely.geometry.Point
                'empty_dest_loc': mapping(Point(row["End_Longitude"], row["End_Latitude"])), # shapely.geometry.Point

                'settings': {
                    'market': 'RideHail',
                    'patience': 150,
                    'service_score': row["Service_Quality"],
                },

                'transition_prob': {
                    # (confirm + reject) | driver_received_trip == 1
                    ('confirm', 'driver_received_trip'): 1.0,
                    ('reject', 'driver_received_trip'): 0.0,

                    # cancel | driver_accepted_trip = 1  if  exceeded_patience
                    # cancel | driver_accepted_trip ~ 0 otherwise
                    ('cancel', 'driver_accepted_trip', 'exceeded_patience'): 1.0,
                    ('cancel', 'driver_accepted_trip'): 0.0,

                    # cancel | driver_moving_to_pickup ~ 0
                    # wait_to_pickup | driver_moving_to_pickup ~ 1
                    ('cancel', 'driver_moving_to_pickup'): 0.0,
                    ('wait_to_pickup', 'driver_moving_to_pickup'): 1.0,

                    # cancel | driver_waiting_to_pickup = 1 if exceeded_patience
                    # cancel | driver_waiting_to_pickup ~ 0 otherwise
                    ('cancel', 'driver_waiting_to_pickup', 'exceeded_patience'): 1.0,
                    ('cancel', 'driver_waiting_to_pickup'): 0.0,

                },

                # 'constraint_accept': {
                #     'max_distance_moving_to_pickup': 5000,
                #     'wait_time_between_jobs': 60,
                # },

                # 'TrTime_accepted_TO_moving_to_pickup': 0,

                # # 'waiting_time_for_pickup': 0, # NOTE This should be embedded in Passenger behavior (may recieve this via message or requested_trip dict?)
                'TrTime_pickup': 0, # NOTE This should be embedded in Passenger behavior (may recieve this via message or requested_trip dict?)
                # # 'waiting_time_for_dropoff': 0, # NOTE This should be embedded in Passenger behavior (may recieve this via message or requested_trip dict?)

                'TrTime_dropoff': 0,
            }
            print(behavior['shift_start_time'])

            agent = DriverAgent(row['Driver_ID'], self, behavior)
            # self.schedule.add(agent)
            self.driver_agents.append(agent)

        passenger_df.dropna(inplace=True)
        passenger_df['Start_Time'] = passenger_df['Start_Time'].apply(lambda dt: dt.replace(day=1, month=1, year=2020))

        passenger_df = passenger_df[(passenger_df['Start_Time'] >= datetime(2020, 1, 1, 0, 0, 0)) & (passenger_df['Start_Time'] <= datetime(2020, 1, 1, 0, 30, 0))]
        print(passenger_df)

        # Create Pasenger Trips
        for _, row in passenger_df.iterrows():
            s_time = row["Start_Time"].to_pydatetime()
            trip_request_time = self.start_time + timedelta(hours=s_time.hour, minutes=s_time.minute, seconds=s_time.second, )
            behavior = {
                'email': f'{row["Booking_ID"]}@test.com',
                'password': 'password',

                'trip_request_time': (trip_request_time - self.start_time).seconds // settings['SIM_STEP_SIZE'], # in units of Simulation Step Size

                'pickup_loc': mapping(Point(row["Start_Longitude"], row["Start_Latitude"])), # shapely.geometry.Point
                'dropoff_loc': mapping(Point(row["End_Longitude"], row["End_Latitude"])), # shapely.geometry.Point

                'trip_value': row["Fare"],

                'settings':{
                    'market': 'RideHail',
                    'patience': row["Patience_Level"], # in Seconds
                },

                'transition_prob': {
                    # cancel | passenger_requested_trip = 1 if exceeded_patience
                    # cancel | passenger_requested_trip ~ 0
                    ('cancel', 'passenger_requested_trip', 'exceeded_patience'): 1.0,
                    ('cancel', 'passenger_requested_trip'): 0.0,

                    # cancel | passenger_assigned_trip ~ 0
                    ('cancel', 'passenger_assigned_trip'): 0.0,

                    # (accept + reject + cancel) | passenger_received_trip_confirmation == 1
                    ('accept', 'passenger_received_trip_confirmation',): 1.0,
                    ('reject', 'passenger_received_trip_confirmation'): 0.0,
                    ('cancel', 'passenger_received_trip_confirmation'): 0.0,
                    ('cancel', 'passenger_received_trip_confirmation', 'exceeded_patience'): 1.0,

                    # (cancel + move_for_pickup + wait_for_pickup) | passenger_accepted_trip ~ 0
                    ('cancel', 'passenger_accepted_trip'): 0.0,
                    # NOTE move_for_pickup and wait_for_pickup transition dependant on currentLoc and PickupLoc

                    # cancel | passenger_moving_for_pickup ~ 0
                    ('cancel', 'passenger_moving_for_pickup'): 0.0,

                    # cancel | passenger_waiting_for_pickup ~ 0
                    ('cancel', 'passenger_waiting_for_pickup'): 0.0,

                    # end_trip | passenger_droppedoff = 1
                    ('end_trip', 'passenger_droppedoff'): 1.0,

                },
            }

            print(behavior['trip_request_time'])
            agent = PassengerAgent(row['Booking_ID'], self)
            # self.schedule.add(agent)
            self.passenger_agents.append(agent)

        for i in range(1): # Only one Solver for the moment.
            agent = AssignmentAgent(f"assignment_{i:03d}", self)
            self.service_schedule.add(agent)

        for i in range(1): # Only one Solver for the moment.
            agent = AnalyticsAgent(f"analytics_{i:03d}", self)
            self.service_schedule.add(agent)


    def step(self):
        self.current_time = self.current_time + relativedelta(seconds=settings['SIM_STEP_SIZE'])
        print(self.current_time)

        for agent in self.driver_agents:
            if agent.entering_market():
                self.driver_schedule.add(agent)

        for agent in self.passenger_agents:
            if agent.entering_market():
                self.passenger_schedule.add(agent)

        # print('before Step')
        self.driver_schedule.step()
        self.passenger_schedule.step()
        # print('After Step')

        self.service_schedule.step()
        # print('After Assignment')

        for agent in self.driver_agents:
            if agent.exiting_market():
                try:
                    self.driver_schedule.remove(agent)
                except Exception as e:
                    print(traceback.format_exc())


        for agent in self.passenger_agents:
            if agent.exiting_market():
                try:
                    self.passenger_schedule.remove(agent)
                except Exception as e:
                    print(traceback.format_exc())

        print(f"{self.driver_schedule.get_agent_count() = }")
        print(f"{self.passenger_schedule.get_agent_count() = }")


    def get_current_time(self):
        return self.current_time

    def get_current_time_str(self):
        return datetime.strftime(self.current_time, "%a, %d %b %Y %H:%M:%S GMT")


# if __name__ == "__main__":

#     def run_sim():
#         num_drivers =  2 # 2 #50
#         num_passengers =  10 # 10 #100
#         sim = OpenRideSim(num_drivers, num_passengers)
#         # print(f"{sim.run_id = }")
#         for s in range(settings['SIM_DURATION']):
#             sim.step()

#         print(f"{sim.run_id = }")

#     run_sim()

