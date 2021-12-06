
import os, sys
current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)

import logging, time, json, traceback, requests
from pprint import pprint
from datetime import datetime

from analytics_app.analytics_agent_indie import AnalyticsAgentIndie
from assignment_app.assignment_agent_indie import AssignmentAgentIndie
from driver_app import DriverAgentIndie
from passenger_app import PassengerAgentIndie
from assignment_app import AssignmentAgentIndie
from analytics_app import AnalyticsAgentIndie

from utils import id_generator, is_success
# from utils.generate_behavior import GenerateBehavior
from apps.scenario import ScenarioManager

from datetime import datetime
from dateutil.relativedelta import relativedelta
import pytest
from unittest import mock

from messenger_service import Messenger

import asyncio

from apps.tasks import start_driver, start_passenger, start_analytics, start_assignment

from orsim import ORSimScheduler
from apps.config import settings #, driver_settings, passenger_settings, analytics_settings, assignment_settings, orsim_settings
from apps.utils.user_registry import UserRegistry
from apps.utils import time_to_str, str_to_time

class DistributedOpenRideSimRandomised():


    def __init__(self, run_id, scenario_name):
        self.run_id = run_id
        self.scenario_name = scenario_name
        self.scenario = ScenarioManager(scenario_name)

        self.reference_time = self.scenario.reference_time
        self.current_time = self.reference_time

        self.agent_scheduler = ORSimScheduler(self.run_id, 'agent_scheduler', self.scenario.orsim_settings)
        self.service_scheduler = ORSimScheduler(self.run_id, 'service_scheduler', self.scenario.orsim_settings, init_failure_handler='hard')
        self.agent_registry = {i:[] for i in range(self.scenario.orsim_settings['SIMULATION_LENGTH_IN_STEPS'])}

        self.user = self.setup_user()
        self.run_record = self.init_run_config()

        self.execution_start_time = time.time()

        for agent_id, behavior in self.scenario.driver_collection.items():
            spec = {
                'unique_id': agent_id,
                'run_id': self.run_id,
                'reference_time': datetime.strftime(self.reference_time, '%Y%m%d%H%M%S'),
                'init_time_step': behavior['shift_start_time'],
                'behavior': behavior,
                'orsim_settings': self.scenario.orsim_settings
            }

            self.agent_registry[behavior['shift_start_time']].append({
                                                                'unique_id': agent_id,
                                                                'method': start_driver,
                                                                'spec': spec
                                                            })

        for agent_id, behavior in self.scenario.passenger_collection.items():
            spec = {
                'unique_id': agent_id,
                'run_id': self.run_id,
                'reference_time': datetime.strftime(self.reference_time, '%Y%m%d%H%M%S'),
                'init_time_step': behavior['trip_request_time'],
                'behavior': behavior,
                'orsim_settings': self.scenario.orsim_settings
            }

            self.agent_registry[behavior['trip_request_time']].append({
                                                                'unique_id': agent_id,
                                                                'method': start_passenger,
                                                                'spec': spec
                                                            })


        for agent_id, behavior in self.scenario.assignment_collection.items():
            spec = {
                'unique_id': agent_id,
                'run_id': self.run_id,
                'reference_time': datetime.strftime(self.reference_time, '%Y%m%d%H%M%S'),
                'init_time_step': 0,
                'behavior': behavior,
                'orsim_settings': self.scenario.orsim_settings
            }

            self.service_scheduler.add_agent(agent_id, start_assignment, spec)

        for agent_id, behavior in self.scenario.analytics_collection.items():
            spec = {
                'unique_id': agent_id,
                'run_id': self.run_id,
                'reference_time': datetime.strftime(self.reference_time, '%Y%m%d%H%M%S'),
                'init_time_step': 0,
                'behavior': behavior,
                'orsim_settings': self.scenario.orsim_settings
            }

            self.service_scheduler.add_agent(agent_id, start_analytics, spec)

    def step(self, i):
        step_start_time = time.time()
        print(f"Simulation Step: {self.agent_scheduler.time} of {self.scenario.orsim_settings['SIMULATION_LENGTH_IN_STEPS']}")

        # IMPORTANT Make sure agents are added into the scheduler before step
        # add_agent is a blocking process and ensures the agent is ready to listen to step()
        agent_scheduler_start_time = time.time()
        for item in self.agent_registry[i]:
            self.agent_scheduler.add_agent(**item)
        # step() assumes all agents will be ready to respond to step message
        asyncio.run(self.agent_scheduler.step())
        agent_scheduler_run_time = time.time() - agent_scheduler_start_time

        service_scheduler_start_time = time.time()
        asyncio.run(self.service_scheduler.step())
        service_scheduler_run_time = time.time() - service_scheduler_start_time


        step_metric = {
            i: {
                'agents': {
                    'stat': self.agent_scheduler.agent_stat[i],
                    'run_time': agent_scheduler_run_time,
                },
                'services': {
                    'stat': self.service_scheduler.agent_stat[i],
                    'run_time': service_scheduler_run_time,
                }
            }
        }

        total_run_time = time.time() - self.execution_start_time
        self.run_record = self.update_status('In Progress', total_run_time, step_metric)

    def setup_user(self):
        credentials = {
            'email': 'sim_admin@test.com',
            'password': 'password',
        }
        user = UserRegistry(time_to_str(datetime.now()), credentials, role='admin')
        return user

    def init_run_config(self):

        run_config_url = f"{settings['OPENRIDE_SERVER_URL']}/run_config"

        data = {
            "run_id": self.run_id,
            "name": self.scenario_name,
            "meta": {
                'num_driver_agents': len(self.scenario.driver_collection),
                'num_passenger_agents': len(self.scenario.passenger_collection),
                'num_analytics_agents': len(self.scenario.analytics_collection),
                'num_assignment_agents': len(self.scenario.assignment_collection),

                'simulation_settings': self.scenario.orsim_settings,
                'services': {
                    'assignment_agents': self.scenario.assignment_collection,
                    'analytics_agents': self.scenario.analytics_collection,
                }
            },
            'step_metrics': {},
        }

        response = requests.post(run_config_url, headers=self.user.get_headers(), data=json.dumps(data))

        if is_success(response.status_code):
            return response.json()
        else:
            raise Exception(f"{response.url}, {response.text}")

    def update_status(self, status, execution_time=0, step_metric=None):
        run_config_item_url = f"{settings['OPENRIDE_SERVER_URL']}/run_config/{self.run_record['_id']}"

        data = {
            'status': status,
            'execution_time': execution_time,
        }
        if step_metric is not None:
            for k, v in step_metric.items():
                data[f'step_metrics.{k}'] = v
                break # expecting only one item

        try:
            response = requests.patch(run_config_item_url,
                                    headers=self.user.get_headers(etag=self.run_record['_etag']),
                                    data=json.dumps(data))
            # print('on patch', response.json())

            if is_success(response.status_code):
                return response.json()
            else:
                print(f"{response.url}, {response.text}")
        except Exception as e:
            print(str(e))



if __name__ == '__main__':

    run_id = id_generator(12)

    # log_dir = f"{os.path.dirname(os.path.abspath(__file__))}/log"
    # if not os.path.exists(log_dir):
    #     os.makedirs(log_dir)
    output_dir = f"{os.path.dirname(os.path.abspath(__file__))}/output/{run_id}"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    logging.basicConfig(filename=f'{output_dir}/app.log', level=settings['LOG_LEVEL'], filemode='w')

    # # scenario_name = '20211117_D200_P2000_4Hx30s_U_singapore_random'
    # # scenario_name = '20211117_D200_P2000_4Hx30s_U_singapore_greedy_pickup'
    # # scenario_name = '20211117_D200_P2000_4Hx30s_U_singapore_greedy_revenue'
    # # scenario_name = '20211117_D200_P2000_4Hx30s_U_singapore_greedy_servicescore'

    # # scenario_name = '20211117_D200_P2000_4Hx30s_P_singapore_random'
    # # scenario_name = '20211117_D200_P2000_4Hx30s_P_singapore_greedy_pickup'
    # # scenario_name = '20211117_D200_P2000_4Hx30s_P_singapore_greedy_revenue'
    # # scenario_name = '20211117_D200_P2000_4Hx30s_P_singapore_greedy_servicescore'
    # # scenario_name = '20211117_D200_P2000_4Hx30s_P_singapore_compromise'

    # # scenario_name = '20211117_D200_P4000_8Hx60s_P_singapore_greedy_pickup'
    # scenario_name = '20211117_D200_P4000_8Hx60s_P_singapore_greedy_revenue'

    # scenario_name = '20211117_D300_P4000_8Hx30s_P_singapore_greedy_pickup'
    # scenario_name = '20211117_D300_P4000_8Hx30s_P_singapore_greedy_revenue'

    # scenario_name = '20211117_D300_P5000_8Hx30s_P_singapore_greedy_pickup'

    # scenario_name = '20211117_D10_P20_1Hx60s_P_singapore_compromise'

    # scenario_name = '20211117_D300_P5000_8Hx30s_P_singapore_greedy_pickup'
    # scenario_name = '20211117_D300_P5000_8Hx30s_P_singapore_greedy_revenue'
    # scenario_name = '20211117_D300_P5000_8Hx30s_P_singapore_compromise'

    # scenario_name = 'comfort_delgro_sampled_10pct_20211122_a_greedy_pickup'
    # scenario_name = 'comfort_delgro_sampled_10pct_20211122_a_greedy_revenue'
    # scenario_name = 'comfort_delgro_sampled_10pct_20211122_a_compromise'
    # scenario_name = 'comfort_delgro_sampled_10pct_20211122_a_compromise_2'

    # scenario_name = 'comfort_delgro_sampled_10pct_20211123_b_pickup_optimal'
    # scenario_name = 'comfort_delgro_sampled_10pct_20211123_b_revenue_optimal'
    # scenario_name = 'comfort_delgro_sampled_10pct_20211123_b_service_optimal'
    # scenario_name = 'comfort_delgro_sampled_10pct_20211123_b_compromise'

    # scenario_name = 'comfort_delgro_sampled_10pct_20211123_b_compromise_demand'
    # scenario_name = 'comfort_delgro_sampled_10pct_20211123_b_compromise_demand_prop'

    # scenario_name = 'comfort_delgro_sampled_10p_20d_20211124_a_pickup_greedy'
    # scenario_name = 'comfort_delgro_sampled_10p_20d_20211124_a_revenue_greedy'
    # scenario_name = 'comfort_delgro_sampled_10p_20d_20211124_a_pickup_optimal'
    # scenario_name = 'comfort_delgro_sampled_10p_20d_20211124_a_revenue_optimal'
    # scenario_name = 'comfort_delgro_sampled_10p_20d_20211124_a_compromise'

    # scenario_name = 'comfort_delgro_sampled_10p_08d_20211125_a_pickup_optimal'
    # scenario_name = 'comfort_delgro_sampled_10p_08d_20211125_a_revenue_optimal'
    # scenario_name = 'comfort_delgro_sampled_10p_08d_20211125_a_service_optimal'
    # scenario_name = 'comfort_delgro_sampled_10p_08d_20211125_a_compromise'

    # scenario_name = 'comfort_delgro_sampled_10p_06d_20211126_a_pickup_optimal'
    # scenario_name = 'comfort_delgro_sampled_10p_06d_20211126_a_revenue_optimal'
    # scenario_name = 'comfort_delgro_sampled_10p_06d_20211126_a_service_optimal'
    # scenario_name = 'comfort_delgro_sampled_10p_06d_20211126_a_compromise'

    # scenario_name = 'comfort_delgro_sampled_10p_06d_20211127_svcdist2_pickup_optimal'
    # scenario_name = 'comfort_delgro_sampled_10p_06d_20211127_svcdist2_revenue_optimal'
    # scenario_name = 'comfort_delgro_sampled_10p_06d_20211127_svcdist2_service_optimal'
    # scenario_name = 'comfort_delgro_sampled_10p_06d_20211127_svcdist2_compromise'
    # scenario_name = 'comfort_delgro_sampled_10p_06d_20211127_svcdist2_compromise_R1.5_P1.2_S1.2'
    # scenario_name = 'comfort_delgro_sampled_10p_06d_20211127_svcdist2_compromise_R1.5_P1.0_S1.0'
    # scenario_name = 'comfort_delgro_sampled_10p_06d_20211127_svcdist2_compromise_R1.5_P0.9_S0.9'
    scenario_name = 'comfort_delgro_sampled_10p_06d_20211127_svcdist2_compromise_servicebias'
    # scenario_name = 'comfort_delgro_sampled_10p_06d_20211127_svcdist2_compromise_scaled'

    # scenario_name = 'comfort_delgro_sampled_15p_06d_20211202_svcdist2_streethail_pickup_optimal'
    # scenario_name = 'comfort_delgro_sampled_15p_06d_20211202_svcdist2_streethail_revenue_optimal'

    # scenario_name = 'comfort_delgro_sampled_10p_06d_20211203_svcdist2_16H_pickup_optimal'

    try:
        sim = DistributedOpenRideSimRandomised(run_id, scenario_name)
    except Exception as e:
        print(f"Failed to launch Simulation model... got: {str(e)}")
        raise e

    print(f"Initializing Simulation for scenario {scenario_name = } with {sim.run_id = }")

    execution_start_time = time.time()

    if settings['EXECUTION_STRATEGY'] == 'CELERY':

        for i in range(sim.scenario.orsim_settings['SIMULATION_LENGTH_IN_STEPS']):
            try:
                step_metric = sim.step(i)
            except Exception as e:
                print(e)
                break

    run_time = time.time() - execution_start_time
    sim.update_status('success', run_time)

    print(f"Generating Visualization output")
    from utils.viz_data import *
    # dump(sim.run_id,
    #      sim.scenario.orsim_settings['SIMULATION_LENGTH_IN_STEPS'],
    #      sim.scenario.orsim_settings['STEP_INTERVAL'],
    #      sim.reference_time,
    #      True if 'compromise' in scenario_name else False
    #      )
    dump_paths(
        sim.run_id,
        sim.scenario.orsim_settings['SIMULATION_LENGTH_IN_STEPS'],
        sim.scenario.orsim_settings['STEP_INTERVAL'],
        sim.reference_time,
    )
    dump_kpi_metrics({sim.run_id: sim.run_id})

    dump_active_agents(
        {sim.run_id: sim.run_id},
        sim.scenario.orsim_settings['SIMULATION_LENGTH_IN_STEPS'],
        sim.scenario.orsim_settings['STEP_INTERVAL'],
        sim.reference_time,
    )

    print(f"Completed {sim.run_id = } with run time {run_time}")

