from apps.agent_core_deprecated.runtime import SimulationRuntime
from apps.agent_core_deprecated.agent_factory import AgentFactory
from apps.ride_hail.adapters.driver_adapter import RideHailDriverAdapter
from apps.ride_hail.adapters.passenger_adapter import RideHailPassengerAdapter
from apps.ride_hail.adapters.assignment_adapter import RideHailAssignmentAdapter
from apps.ride_hail.adapters.analytics_adapter import RideHailAnalyticsAdapter
from apps.ride_hail.scenario import ScenarioManager
from orsim import ORSimScheduler, ORSimEnv
from apps.config import simulation_domains, messenger_backend
from apps.utils.path_utils import get_run_data_dir

# Initialize the backend
ORSimEnv.set_backend(messenger_backend)

# Setup adapters for each role
driver_adapter = RideHailDriverAdapter()
passenger_adapter = RideHailPassengerAdapter()
assignment_adapter = RideHailAssignmentAdapter()
analytics_adapter = RideHailAnalyticsAdapter()

adapters = {
    'driver': driver_adapter,
    'passenger': passenger_adapter,
    'assignment': assignment_adapter,
    'analytics': analytics_adapter,
}

# Example run_id and scenario
run_id = 'example_run_001'
scenario_name = 'stay_or_leave_test'
domain = simulation_domains['ridehail']
run_data_dir = get_run_data_dir(run_id, domain)

# ScenarioManager should provide get_agent_specs() and get_service_specs()
scenario_manager = ScenarioManager(scenario_name, domain=domain, run_data_dir=run_data_dir)

# Setup schedulers
agent_scheduler = ORSimScheduler(run_id, 'agent_scheduler', scenario_manager.orsim_settings)
service_scheduler = ORSimScheduler(run_id, 'service_scheduler', scenario_manager.orsim_settings)
schedulers = {'agent': agent_scheduler, 'service': service_scheduler}

# Example run config (could be loaded from file or generated)
run_config = {'run_id': run_id, 'scenario': scenario_name}

# Create factory and runtime
agent_factory = AgentFactory(adapters)
runtime = SimulationRuntime(agent_factory, scenario_manager, schedulers, run_config, run_data_dir)

# Register agents
runtime.register_agents()

# Run simulation loop, closely following distributed_openride_sim_randomised
steps = scenario_manager.orsim_settings['SIMULATION_LENGTH_IN_STEPS']
for i in range(steps):
    try:
        runtime.step(i)
    except Exception as e:
        print(f"Error in simulation step {i}: {e}")
        break
