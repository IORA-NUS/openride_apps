import os, sys, traceback
current_path = os.path.abspath('.')
# parent_path = os.path.dirname(current_path)
sys.path.append(current_path)

import logging

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

from datetime import datetime
from dateutil.relativedelta import relativedelta
import pytest
from unittest import mock


import asyncio

class SequentialOpenRideSimRandomised(Model):


    def __init__(self, num_drivers, num_passengers):
        self.run_id = id_generator(12)
        self.start_time = datetime(2020,1,1,8,0,0)
        logging.info(f"{self.run_id = }, {self.start_time = }")
        self.current_time = self.start_time

        self.num_agents = num_drivers + num_passengers

        self.service_schedule = BaseScheduler(self)
        # self.driver_schedule = RandomActivation(self)
        # self.passenger_schedule = RandomActivation(self)
        self.driver_schedule = ParallelBaseScheduler(self)
        self.passenger_schedule = ParallelBaseScheduler(self)

        self.driver_agents = []
        self.passenger_agents = []

        self.sim_settings = settings['SIM_SETTINGS']

        for i in range(num_drivers):
            agent = DriverAgent(f"d_{i:06d}", self)
            # self.schedule.add(agent)
            self.driver_agents.append(agent)

        for i in range(num_passengers):
            agent = PassengerAgent(f"p_{i:06d}", self)
            # self.schedule.add(agent)
            self.passenger_agents.append(agent)

        for i in range(1): # Only one Solver for the moment.
            agent = AssignmentAgent(f"assignment_{i:03d}", self)
            self.service_schedule.add(agent)

        for i in range(1): # Only one Solver for the moment.
            agent = AnalyticsAgent(f"analytics_{i:03d}", self)
            self.service_schedule.add(agent)


    def step(self):
        self.current_time = self.current_time + relativedelta(seconds=self.sim_settings['SIM_STEP_SIZE'])
        logging.info(self.current_time)

        for agent in self.driver_agents:
            if agent.entering_market():
                self.driver_schedule.add(agent)

        for agent in self.passenger_agents:
            if agent.entering_market():
                self.passenger_schedule.add(agent)

        self.driver_schedule.step()
        self.passenger_schedule.step()

        self.service_schedule.step()

        for agent in self.driver_agents:
            if agent.exiting_market():
                try:
                    self.driver_schedule.remove(agent)
                except Exception as e:
                    logging.exception(str(e))
                    # print(traceback.format_exc())


        for agent in self.passenger_agents:
            if agent.exiting_market():
                try:
                    self.passenger_schedule.remove(agent)
                except Exception as e:
                    logging.exception(str(e))
                    # print(traceback.format_exc())

        logging.info(f"{self.driver_schedule.get_agent_count() = }")
        logging.info(f"{self.passenger_schedule.get_agent_count() = }")


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

