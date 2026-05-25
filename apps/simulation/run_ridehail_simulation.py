import os
from datetime import datetime

from apps.ridehail.scenario import ScenarioManager
from apps.config import simulation_domains
from orsim import PrecomputedAgentSource, FixedStepTermination
from openride_apps.apps.simulation.simulation_runtime import SimulationRuntime


# --- Paths and identifiers ---

datahub_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'datahub'))
if not os.path.exists(datahub_dir) or not os.path.isabs(datahub_dir):
    raise ValueError(f"datahub_dir does not exist or is not an absolute path. Got: {datahub_dir}")

run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
scenario_name = 'stay_or_leave_test'
domain = simulation_domains['ridehail']

# --- Scenario ---

scenario_manager = ScenarioManager(datahub_dir, scenario_name, domain=domain)

# --- Agent configuration (scheduler_key, class, timing, extras per role) ---
# extra_fields closes over locals; the third argument (context) is unused here.

agent_config = {
    'driver': {
        'scheduler_key': 'agent',
        'agent_class': 'apps.ridehail.driver.DriverAgentIndie',
        'init_time_step_key': 'shift_start_time',
        'extra_fields': lambda agent_id, behavior, ctx: {},
    },
    'passenger': {
        'scheduler_key': 'agent',
        'agent_class': 'apps.ridehail.passenger.PassengerAgentIndie',
        'init_time_step_key': 'trip_request_time',
        'extra_fields': lambda agent_id, behavior, ctx: {},
    },
    'assignment': {
        'scheduler_key': 'service',
        'agent_class': 'apps.ridehail.assignment.AssignmentAgentIndie',
        'init_time_step_key': None,
        'extra_fields': lambda agent_id, behavior, ctx: {},
    },
    'analytics': {
        'scheduler_key': 'service',
        'agent_class': 'apps.ridehail.analytics.AnalyticsAgentIndie',
        'init_time_step_key': None,
        'extra_fields': lambda agent_id, behavior, ctx: {'datahub_dir': datahub_dir},
    },
}

# --- Scheduler configuration ---

scheduler_config = {
    'agent': {
        'run_id': run_id,
        'scheduler_id': 'agent_scheduler',
        'orsim_settings': scenario_manager.orsim_settings,
    },
    'service': {
        'run_id': run_id,
        'scheduler_id': 'service_scheduler',
        'orsim_settings': scenario_manager.orsim_settings,
        'init_failure_handler': 'hard',
    },
}

# --- State machines ---

from apps.ridehail.statemachine.ridehail_driver_trip_sm import RidehailDriverTripStateMachine
from apps.ridehail.statemachine.ridehail_passenger_trip_sm import RidehailPassengerTripStateMachine
from orsim.utils import WorkflowStateMachine

ridehail_statemachines = {
    'RidehailDriverTripStateMachine': RidehailDriverTripStateMachine,
    'RidehailPassengerTripStateMachine': RidehailPassengerTripStateMachine,
    'WorkflowStateMachine': WorkflowStateMachine,
}

# --- Agent source and termination condition ---

parent_path = os.path.dirname(os.path.abspath(os.getcwd()))

agent_source = PrecomputedAgentSource(
    scenario_manager=scenario_manager,
    agent_config=agent_config,
    run_id=run_id,
    reference_time=scenario_manager.reference_time,
    project_path=parent_path,
)

termination = FixedStepTermination(scenario_manager.orsim_settings['SIMULATION_LENGTH_IN_STEPS'])

# --- Runtime ---

sim = SimulationRuntime(
    run_id=run_id,
    domain=domain,
    scheduler_config=scheduler_config,
    agent_source=agent_source,
    termination_condition=termination,
    scenario_manager=scenario_manager,
    statemachine_collection=ridehail_statemachines,
    datahub_dir=datahub_dir,
)

if __name__ == "__main__":
    sim.run_simulation()
