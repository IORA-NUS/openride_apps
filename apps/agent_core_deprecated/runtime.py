
import time
import logging
import asyncio
import json
import requests
from apps.config import settings, simulation_domains
from apps.common.user_registry import UserRegistry
from apps.common.statemachine_registry import StateMachineRegistry

class SimulationRuntime:

    def __init__(self, agent_factory, scenario_manager, schedulers, run_config, run_data_dir):
        self.agent_factory = agent_factory
        self.scenario_manager = scenario_manager
        self.schedulers = schedulers  # Dict: {'agent': ORSimScheduler, 'service': ORSimScheduler}
        self.run_config = run_config
        self.run_data_dir = run_data_dir
        self.execution_start_time = None
        self.agent_registry = None  # Will be populated in register_agents

        # --- DistributedOpenRideSimRandomised wiring ---
        self.user = self.setup_user()
        self.run_record = self.init_run_config()
        self.register_state_machines()

    def setup_user(self):
        from datetime import datetime
        from apps.utils import time_to_str
        credentials = {
            'email': 'sim_admin@test.com',
            'password': 'password',
        }
        user = UserRegistry(time_to_str(datetime.now()), credentials, role='admin')
        return user

    def init_run_config(self):
        run_config_url = f"{settings['OPENRIDE_SERVER_URL']}/run-config"
        data = {
            "run_id": self.run_config['run_id'],
            "name": self.run_config['scenario'],
            "meta": {
                'num_driver_agents': len(self.scenario_manager.driver_collection),
                'num_passenger_agents': len(self.scenario_manager.passenger_collection),
                'num_analytics_agents': len(self.scenario_manager.analytics_collection),
                'num_assignment_agents': len(self.scenario_manager.assignment_collection),
                'simulation_settings': self.scenario_manager.orsim_settings,
                'services': {
                    'assignment_agents': self.scenario_manager.assignment_collection,
                    'analytics_agents': self.scenario_manager.analytics_collection,
                }
            },
            'step_metrics': {},
        }
        response = requests.post(run_config_url, headers=self.user.get_headers(), data=json.dumps(data))
        if response.status_code in (200, 201):
            return response.json()
        else:
            raise Exception(f"{response.url}, {response.text}")

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
        """
        Run a single simulation step, dynamically adding agents scheduled for this step.
        """
        agent_scheduler = self.schedulers['agent']
        # Add agents scheduled for this step
        for item in self.agent_registry.get(i, []):
            agent_obj = agent_scheduler.add_agent(**item)
            if hasattr(agent_obj, 'agent_failed') and getattr(agent_obj, 'agent_failed', False):
                logging.error(f"Agent {getattr(agent_obj, 'unique_id', 'unknown')} failed to initialize and will not step.")
        try:
            asyncio.run(agent_scheduler.step())
            asyncio.run(self.schedulers['service'].step())
        except Exception as e:
            logging.error(f"Error at step {i}: {e}")
            raise
        return None

    def register_agents(self):
        # Inject scheduler as a dict for serialization, matching DistributedOpenRideSimRandomised pattern
        agent_scheduler = self.schedulers['agent']
        service_scheduler = self.schedulers['service']
        agent_scheduler_dict = {
            'id': getattr(agent_scheduler, 'scheduler_id', 'agent_scheduler'),
            'orsim_settings': getattr(agent_scheduler, 'orsim_settings', {})
        }
        service_scheduler_dict = {
            'id': getattr(service_scheduler, 'scheduler_id', 'service_scheduler'),
            'orsim_settings': getattr(service_scheduler, 'orsim_settings', {})
        }

        import os
        parent_path = os.path.dirname(os.path.abspath(os.getcwd()))
        project_path = parent_path

        # Build agent_registry: {step: [ {spec, project_path, agent_class}, ... ] }
        steps = self.scenario_manager.orsim_settings['SIMULATION_LENGTH_IN_STEPS']
        self.agent_registry = {i: [] for i in range(steps)}

        # Register drivers and passengers in agent_registry by their init_time_step
        for agent_spec in self.scenario_manager.get_agent_specs(scheduler=agent_scheduler_dict):
            role = agent_spec['role']
            adapter = self.agent_factory.adapters[role]
            agent_class = adapter.get_agent_class()
            agent_class_str = f"{agent_class.__module__}.{agent_class.__name__}"
            init_time_step = agent_spec['init_args'].get('init_time_step', 0)
            self.agent_registry[init_time_step].append({
                'spec': agent_spec['init_args'],
                'project_path': project_path,
                'agent_class': agent_class_str
            })

        # Register services (assignment, analytics) up front (not per-step)
        for service_spec in self.scenario_manager.get_service_specs(scheduler=service_scheduler_dict):
            role = service_spec['role']
            adapter = self.agent_factory.adapters[role]
            service_class = adapter.get_agent_class()
            service_class_str = f"{service_class.__module__}.{service_class.__name__}"
            service_scheduler.add_agent(
                spec=service_spec['init_args'],
                project_path=project_path,
                agent_class=service_class_str
            )

    def run(self, steps):
        self.execution_start_time = time.time()
        for i in range(steps):
            try:
                asyncio.run(self.schedulers['agent'].step())
                asyncio.run(self.schedulers['service'].step())
                # Optionally: collect and log metrics
            except Exception as e:
                logging.error(f"Error at step {i}: {e}")
                break
        total_time = time.time() - self.execution_start_time
        return total_time
