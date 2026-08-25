# Renamed from run_generalized_simulation.py
import sys, json, os
from datetime import datetime
from apps.ridehail.scenario import ScenarioManager
from openride_apps.apps.simulation.simulation_runtime import SimulationRuntime
# from apps.utils.path_utils import get_run_data_dir
from apps.config import simulation_domains

# def register_state_machines_fn(sim):
#     from apps.ridehail.statemachine.ridehail_driver_trip_sm import RidehailDriverTripStateMachine
#     from apps.ridehail.statemachine.ridehail_passenger_trip_sm import RidehailPassengerTripStateMachine
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

# Honor an externally-injected run id (set by the control layer at launch) so the id is
# known before the process starts producing — eliminates the discovery race where the
# dashboard could attach to the previous, already-completed run. Falls back to a
# timestamp when not provided (e.g. direct CLI invocation).
run_id = os.environ.get("ORSIM_RUN_ID", "").strip() or f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

def main():
    global run_id
    from apps.utils import kafka_utils

    # --- Domain-specific configuration for ridehail ---

    datahub_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'datahub'))
    # Ensure this is an absolute path to the datahub location.
    if not os.path.exists(datahub_dir) or not os.path.isabs(datahub_dir):
        raise ValueError(f"datahub_dir does not exist or is not an absolute path. Got: {datahub_dir}")

    scenario_name = 'stay_or_leave_test'
    domain = simulation_domains['ridehail']
    # run_data_dir = get_run_data_dir(run_id, domain)

    # ScenarioManager for ridehail
    scenario_manager = ScenarioManager(datahub_dir, scenario_name, domain=domain) #, run_data_dir=run_data_dir)

    domain_name = domain  # e.g., 'ridehail-sim'

    # --- Pluggable agent/service collection wiring ---
    agent_config = {
        'driver': {
            'scheduler_key': 'agent',
            'agent_class': 'apps.ridehail.driver.DriverAgentIndie',
            'init_time_step_key': 'shift_start_time',
            'extra_fields': lambda agent_id, behavior, sim: {},
        },
        'passenger': {
            'scheduler_key': 'agent',
            'agent_class': 'apps.ridehail.passenger.PassengerAgentIndie',
            'init_time_step_key': 'trip_request_time',
            'extra_fields': lambda agent_id, behavior, sim: {},
        },
        'assignment': {
            'scheduler_key': 'service',
            'agent_class': 'apps.ridehail.assignment.AssignmentAgentIndie',
            'init_time_step_key': None,
            'extra_fields': lambda agent_id, behavior, sim: {},
        },
        'analytics': {
            'scheduler_key': 'service',
            'agent_class': 'apps.ridehail.analytics.AnalyticsAgentIndie',
            'init_time_step_key': None,
            'extra_fields': lambda agent_id, behavior, sim: {'datahub_dir': sim.datahub_dir},
        },
    }
    

    from apps.ridehail.statemachine.ridehail_driver_trip_sm import RidehailDriverTripStateMachine
    from apps.ridehail.statemachine.ridehail_passenger_trip_sm import RidehailPassengerTripStateMachine
    from orsim.utils import WorkflowStateMachine

    ridehail_statemachines = {
        'RidehailDriverTripStateMachine': RidehailDriverTripStateMachine,
        'RidehailPassengerTripStateMachine': RidehailPassengerTripStateMachine,
        'WorkflowStateMachine': WorkflowStateMachine,
    }

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

    from apps.simulation.container_logistics_wiring import kafka_progress_listener

    sim = SimulationRuntime(
        run_id=run_id,
        scenario_manager=scenario_manager,
        datahub_dir=datahub_dir,
        domain=domain,
        agent_config=agent_config,
        statemachine_collection=ridehail_statemachines,
        scheduler_config=scheduler_config,
        progress_listener=kafka_progress_listener,
    )

    print(f"Initializing Kafka for {run_id}...")
    kafka_utils.initialize_kafka_topics()
    run_status_topic = kafka_utils.resolve_topic("run_status")
    kafka_utils.push_run_status(run_status_topic, run_id, "RUNNING")
    kafka_utils.flush_producer(10)

    print("Running simulation .... ")
    try:
        sim.run_simulation()
        print("Simulation completed!")
        kafka_utils.push_run_status(run_status_topic, run_id, "COMPLETED")
        kafka_utils.flush_producer(10)
    except Exception as e:
        print(f"Simulation Error: {e}")
        # Still try to send a failure event if possible
        kafka_utils.push_run_status(run_status_topic, run_id, "FAILED", msg=str(e))
        kafka_utils.flush_producer(10)


if __name__ == "__main__":
    main()
   