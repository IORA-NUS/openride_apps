
import os, sys
import asyncio
# macOS event loop policy fix
# if sys.platform == "darwin":
#     asyncio.set_event_loop_policy(asyncio.SelectorEventLoopPolicy())

current_path = os.path.abspath('.')
parent_path = os.path.dirname(current_path)
sys.path.append(parent_path)
# print(parent_path)

import logging, time, json, traceback, requests
from pprint import pprint
from datetime import datetime

from apps.ride_hail.analytics import AnalyticsAgentIndie
from apps.ride_hail.assignment import AssignmentAgentIndie
from apps.ride_hail.driver import DriverAgentIndie
from apps.ride_hail.passenger import PassengerAgentIndie

from utils import id_generator, is_success
# from utils.generate_behavior import GenerateBehavior
from apps.ride_hail.scenario import ScenarioManager
# from apps.utils.path_utils import get_run_data_dir

from datetime import datetime
from dateutil.relativedelta import relativedelta
import pytest
from unittest import mock

# from messenger_service import Messenger


# from apps.tasks import start_driver, start_passenger, start_analytics, start_assignment

from orsim import ORSimScheduler, ORSimEnv
from apps.config import settings, messenger_backend, simulation_domains #, driver_settings, passenger_settings, analytics_settings, assignment_settings, orsim_settings
from apps.common.user_registry import UserRegistry
from apps.utils import time_to_str, str_to_time

from apps.common.statemachine_registry import StateMachineRegistry



class DistributedOpenRideSimRandomised:
    def __init__(self, datahub_dir, run_id, scenario_name): #, run_data_dir):
        ORSimEnv.set_backend(messenger_backend)

        self.datahub_dir = datahub_dir
        self.run_id = run_id
        self.scenario_name = scenario_name
        # self.run_data_dir = run_data_dir
        self.scenario = ScenarioManager(self.datahub_dir, self.scenario_name, domain=simulation_domains['ridehail']) #, run_data_dir=run_data_dir)

        self.reference_time = self.scenario.reference_time
        self.current_time = self.reference_time

        self.agent_scheduler = ORSimScheduler(self.run_id, 'agent_scheduler', self.scenario.orsim_settings)
        self.service_scheduler = ORSimScheduler(self.run_id, 'service_scheduler', self.scenario.orsim_settings, init_failure_handler='hard')
        self.agent_registry = {i:[] for i in range(self.scenario.orsim_settings['SIMULATION_LENGTH_IN_STEPS'])}

        self.user = self.setup_user()
        self.run_record = self.init_run_config()

        self.register_state_machines()

        self.execution_start_time = time.time()

        for agent_id, behavior in self.scenario.driver_collection.items():
            spec = {
                'unique_id': agent_id,
                'run_id': self.run_id,
                'reference_time': datetime.strftime(self.reference_time, '%Y%m%d%H%M%S'),
                'init_time_step': behavior['shift_start_time'],
                'behavior': behavior,
            }
            self.agent_registry[behavior['shift_start_time']].append({
                'spec': spec,
                'project_path': f'{parent_path}',
                'agent_class': 'apps.ride_hail.driver.DriverAgentIndie',
            })

        for agent_id, behavior in self.scenario.passenger_collection.items():
            spec = {
                'unique_id': agent_id,
                'run_id': self.run_id,
                'reference_time': datetime.strftime(self.reference_time, '%Y%m%d%H%M%S'),
                'init_time_step': behavior['trip_request_time'],
                'behavior': behavior,
            }
            self.agent_registry[behavior['trip_request_time']].append({
                'spec': spec,
                'project_path': f'{parent_path}',
                'agent_class': 'apps.ride_hail.passenger.PassengerAgentIndie',
            })

        for agent_id, behavior in self.scenario.assignment_collection.items():
            spec = {
                'unique_id': agent_id,
                'run_id': self.run_id,
                'reference_time': datetime.strftime(self.reference_time, '%Y%m%d%H%M%S'),
                'init_time_step': 0,
                'behavior': behavior,
            }
            self.service_scheduler.add_agent(
                spec=spec,
                project_path=f'{parent_path}',
                agent_class='apps.ride_hail.assignment.AssignmentAgentIndie',
            )

        for agent_id, behavior in self.scenario.analytics_collection.items():
            spec = {
                'unique_id': agent_id,
                'run_id': self.run_id,
                'reference_time': datetime.strftime(self.reference_time, '%Y%m%d%H%M%S'),
                'init_time_step': 0,
                'behavior': behavior,
                # 'run_data_dir': run_data_dir,
                'datahub_dir': self.datahub_dir,
            }
            self.service_scheduler.add_agent(
                spec=spec,
                project_path=f'{parent_path}',
                agent_class='apps.ride_hail.analytics.AnalyticsAgentIndie',
            )




    def register_state_machines(self):
        from apps.ride_hail.statemachine import RidehailDriverTripStateMachine, RidehailPassengerTripStateMachine
        from orsim.utils import WorkflowStateMachine
        statemachines = {
            'RidehailDriverTripStateMachine': RidehailDriverTripStateMachine,
            'RidehailPassengerTripStateMachine': RidehailPassengerTripStateMachine,
            'WorkflowStateMachine': WorkflowStateMachine,
        }
        StateMachineRegistry(statemachines=statemachines, domain=simulation_domains['ridehail']).register_state_machines(
            server_url=settings['OPENRIDE_SERVER_URL'],
            headers=self.user.get_headers()
            )

    def step(self, i):
        step_start_time = time.time()
        print(f"Simulation Step: {self.agent_scheduler.time} of {self.scenario.orsim_settings['SIMULATION_LENGTH_IN_STEPS']}")

        try:

            loop = asyncio.get_event_loop()
            # print loop debug
            print(f"Event loop: {loop}")
        except Exception as e:
            # print(f"Error in Simulation step {i}: {str(e)}")
            # traceback.print_exc()
            # step_metric = {
            #     i: {
            #         'error': str(e),
            #     }
            # }
            pass

        # IMPORTANT Make sure agents are added into the scheduler before step
        # add_agent is a blocking process and ensures the agent is ready to listen to step()
        agent_scheduler_start_time = time.time()
        # print(self.agent_registry[0])
        for item in self.agent_registry[i]:
            # print(f"{item}")
            agent_obj = self.agent_scheduler.add_agent(**item)
            # If the agent object has agent_failed, log it
            if hasattr(agent_obj, 'agent_failed') and getattr(agent_obj, 'agent_failed', False):
                import logging
                logging.error(f"Agent {getattr(agent_obj, 'unique_id', 'unknown')} failed to initialize and will not step.")
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

        run_config_url = f"{settings['OPENRIDE_SERVER_URL']}/run-config"

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
        run_config_item_url = f"{settings['OPENRIDE_SERVER_URL']}/run-config/{self.run_record['_id']}"

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
    from apps.config import simulation_domains
    # from apps.utils.path_utils import get_run_data_dir
    domain = simulation_domains['ridehail']
    # run_data_dir = get_run_data_dir(run_id, domain)
    datahub_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'datahub'))
    # Ensure this is an absolute path to the datahub location.
    print(f"[DEBUG] Datahub directory for this run: {datahub_dir}")


    # Option to also display logs to console for debugging
    DISPLAY_LOGS_TO_CONSOLE = False  # Set to False to disable console logging

    # Remove all handlers associated with the root logger object
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    log_file_dir = os.path.join(datahub_dir, domain, 'run_logs', run_id)
    if not os.path.exists(log_file_dir):
        os.makedirs(log_file_dir)
    log_file = os.path.join(log_file_dir, 'app.log')
    print(f"[DEBUG] Log file for this run: {log_file}")
    file_handler = logging.FileHandler(log_file, mode='w')
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s')
    file_handler.setFormatter(formatter)
    logging.root.addHandler(file_handler)

    if DISPLAY_LOGS_TO_CONSOLE:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logging.root.addHandler(console_handler)

    logging.root.setLevel(settings['LOG_LEVEL'])

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
    # scenario_name = 'comfort_delgro_sampled_10p_06d_20211127_svcdist2_compromise_servicebias'
    # scenario_name = 'comfort_delgro_sampled_10p_06d_20211127_svcdist2_compromise_scaled'

    # scenario_name = 'comfort_delgro_sampled_15p_06d_20211202_svcdist2_streethail_pickup_optimal'
    # scenario_name = 'comfort_delgro_sampled_15p_06d_20211202_svcdist2_streethail_revenue_optimal'

    # scenario_name = 'comfort_delgro_sampled_10p_06d_20211203_svcdist2_16H_pickup_optimal'

    # scenario_name = 'comfort_delgro_sampled_10p_20d_20211223_svcdist2_8H_pickup_optimal'
    # scenario_name = 'comfort_delgro_sampled_10p_20d_20211223_svcdist2_8H_revenue_optimal'
    # scenario_name = 'comfort_delgro_sampled_10p_20d_20211223_svcdist2_8H_service_optimal'
    # scenario_name = 'comfort_delgro_sampled_10p_20d_20211223_svcdist2_8H_compromise_servicebias'

    # scenario_name = 'comfort_delgro_sampled_10p_30d_20211228_svcdist2_8H_pickup_optimal'
    # scenario_name = 'comfort_delgro_sampled_10p_30d_20211228_svcdist2_8H_revenue_optimal'
    # scenario_name = 'comfort_delgro_sampled_10p_30d_20211228_svcdist2_8H_service_optimal'
    # scenario_name = 'comfort_delgro_sampled_10p_30d_20211228_svcdist2_8H_compromise_servicebias'

    # scenario_name = 'comfort_delgro_sampled_10p_20d_20211229_svcdist2_8H_pickup_optimal'
    # scenario_name = 'comfort_delgro_sampled_10p_20d_20211229_svcdist2_8H_revenue_optimal'
    # scenario_name = 'comfort_delgro_sampled_10p_20d_20211229_svcdist2_8H_service_optimal'
    # scenario_name = 'comfort_delgro_sampled_10p_20d_20211229_svcdist2_8H_compromise_servicebias'
    # scenario_name = 'comfort_delgro_sampled_10p_20d_20211229_svcdist2_8H_compromise_servicebias_R1.2_P1.2_S1.0'
    # scenario_name = 'comfort_delgro_sampled_15p_06d_20211202_svcdist2_streethail'

    scenario_name = 'stay_or_leave_test'
    # scenario_name = 'stay_or_leave_test_changi'

    try:
        sim = DistributedOpenRideSimRandomised(datahub_dir, run_id, scenario_name) #, run_data_dir=run_data_dir)
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

    # print(f"Generating Visualization output")
    # from utils.viz_data import *

    # target = {
    #     'revenue': 77.5802,
    #     'wait_time_pickup': 2491.5625,
    #     'service_score': 416.38645,
    # }
    # # dump(sim.run_id,
    # #      sim.scenario.orsim_settings['SIMULATION_LENGTH_IN_STEPS'],
    # #      sim.scenario.orsim_settings['STEP_INTERVAL'],
    # #      sim.reference_time,
    # #      True if 'compromise' in scenario_name else False
    # #      )
    # dump_paths(
    #     sim.run_id,
    #     sim.run_id,
    #     sim.scenario.orsim_settings['SIMULATION_LENGTH_IN_STEPS'],
    #     sim.scenario.orsim_settings['STEP_INTERVAL'],
    #     sim.reference_time,
    # )
    # dump_demand_coords(
    #     sim.run_id,
    #     sim.run_id,
    #     sim.scenario.orsim_settings['SIMULATION_LENGTH_IN_STEPS'],
    #     sim.scenario.orsim_settings['STEP_INTERVAL'],
    #     sim.reference_time,
    # )

    # dump_kpi_metrics({sim.run_id: sim.run_id}, target)

    # dump_active_agents(
    #     {sim.run_id: sim.run_id},
    #     sim.scenario.orsim_settings['SIMULATION_LENGTH_IN_STEPS'],
    #     sim.scenario.orsim_settings['STEP_INTERVAL'],
    #     sim.reference_time,
    # )

    # dump_solver_params({sim.run_id: sim.run_id})

    # dump_trip_metrics({sim.run_id: sim.run_id})

    # print(f"Completed {sim.run_id = } with run time {run_time}")

