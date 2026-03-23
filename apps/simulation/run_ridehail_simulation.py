# Renamed from run_generalized_simulation.py

import sys, json
from datetime import datetime
from apps.ride_hail.scenario import ScenarioManager
from openride_apps.apps.simulation.simulation_runtime import SimulationRuntime
from apps.utils.path_utils import get_run_data_dir
from apps.config import simulation_domains

# --- Domain-specific configuration for ridehail ---
run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
scenario_name = 'stay_or_leave_test'
domain = simulation_domains['ridehail']
run_data_dir = get_run_data_dir(run_id, domain)

# ScenarioManager for ridehail
scenario_manager = ScenarioManager(scenario_name, domain=domain, run_data_dir=run_data_dir)

domain_name = domain  # e.g., 'ridehail-sim'

# --- Pluggable agent/service collection wiring ---
agent_config = {
    'driver': {
        'scheduler_key': 'agent',
        'agent_class': 'apps.ride_hail.driver.DriverAgentIndie',
        'init_time_step_key': 'shift_start_time',
        'extra_fields': lambda agent_id, behavior, sim: {},
    },
    'passenger': {
        'scheduler_key': 'agent',
        'agent_class': 'apps.ride_hail.passenger.PassengerAgentIndie',
        'init_time_step_key': 'trip_request_time',
        'extra_fields': lambda agent_id, behavior, sim: {},
    },
    'assignment': {
        'scheduler_key': 'service',
        'agent_class': 'apps.ride_hail.assignment.AssignmentAgentIndie',
        'init_time_step_key': None,
        'extra_fields': lambda agent_id, behavior, sim: {},
    },
    'analytics': {
        'scheduler_key': 'service',
        'agent_class': 'apps.ride_hail.analytics.AnalyticsAgentIndie',
        'init_time_step_key': None,
        'extra_fields': lambda agent_id, behavior, sim: {'run_data_dir': sim.run_data_dir},
    },
}

# def register_state_machines_fn(sim):
#     from apps.ride_hail.statemachine.ridehail_driver_trip_sm import RidehailDriverTripStateMachine
#     from apps.ride_hail.statemachine.ridehail_passenger_trip_sm import RidehailPassengerTripStateMachine
#     from orsim.utils import WorkflowStateMachine
#     from apps.common.statemachine_registry import StateMachineRegistry
#     from apps.config import settings
#     statemachines = {
#         'RidehailDriverTripStateMachine': RidehailDriverTripStateMachine,
#         'RidehailPassengerTripStateMachine': RidehailPassengerTripStateMachine,
#         'WorkflowStateMachine': WorkflowStateMachine,
#     }
#     StateMachineRegistry(statemachines=statemachines, domain=sim.domain_name).register_state_machines(
#         server_url=settings['OPENRIDE_SERVER_URL'],
#         headers=sim.user.get_headers()
#     )

from apps.ride_hail.statemachine.ridehail_driver_trip_sm import RidehailDriverTripStateMachine
from apps.ride_hail.statemachine.ridehail_passenger_trip_sm import RidehailPassengerTripStateMachine
from orsim.utils import WorkflowStateMachine

ridehail_statemachines = {
    'RidehailDriverTripStateMachine': RidehailDriverTripStateMachine,
    'RidehailPassengerTripStateMachine': RidehailPassengerTripStateMachine,
    'WorkflowStateMachine': WorkflowStateMachine,
}


# def init_run_config_fn(sim):
#     from apps.config import settings
#     data = {
#         "run_id": sim.run_id,
#         "name": sim.domain_name,
#         "meta": {
#             'num_driver_agents': len(sim.scenario_manager.get_agent_collection('driver')),
#             'num_passenger_agents': len(sim.scenario_manager.get_agent_collection('passenger')),
#             'num_analytics_agents': len(sim.scenario_manager.get_agent_collection('analytics')),
#             'num_assignment_agents': len(sim.scenario_manager.get_agent_collection('assignment')),
#             'simulation_settings': sim.orsim_settings,
#             'services': {
#                 'assignment_agents': sim.scenario_manager.get_agent_collection('assignment'),
#                 'analytics_agents': sim.scenario_manager.get_agent_collection('analytics'),
#             }
#         },
#         'step_metrics': {},
#     }
#     import requests
#     response = requests.post(f"{settings['OPENRIDE_SERVER_URL']}/run-config", headers=sim.user.get_headers(), data=json.dumps(data))
#     if response.status_code in (200, 201):
#         return response.json()
#     else:
#         raise Exception(f"{response.url}, {response.text}")

# run_config_data = {
#     "run_id": run_id,
#     "name": scenario_name,
#     "meta": scenario_manager.get_run_config_meta(),
#     'step_metrics': {},
# }



# Define scheduler_config for config-driven scheduler instantiation (parameters only)
scheduler_config = {
    'agent': {
        'run_id': run_id,
        'scheduler_id': 'agent_scheduler',
        'orsim_settings': scenario_manager.orsim_settings
    },
    'service': {
        'run_id': run_id,
        'scheduler_id': 'service_scheduler',
        'orsim_settings': scenario_manager.orsim_settings,
        'init_failure_handler': 'hard'
    }
}

sim = SimulationRuntime(
    run_id=run_id,
    scenario_manager=scenario_manager,
    run_data_dir=run_data_dir,
    domain=domain,
    agent_config=agent_config,
    statemachine_collection=ridehail_statemachines,
    scheduler_config=scheduler_config
)

if __name__ == "__main__":
    sim.run_simulation()
