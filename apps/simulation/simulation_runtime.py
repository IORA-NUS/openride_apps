import os
import time
import json
import logging
import requests
import asyncio
from datetime import datetime
import cerberus

from apps.config import settings, simulation_domains
from apps.common.user_registry import UserRegistry
from apps.common.statemachine_registry import StateMachineRegistry


class SimulationRuntime:
    def __init__(
        self,
        run_id,
        domain,
        scenario_manager,
        run_data_dir,
        agent_config,
        statemachine_collection,
        scheduler_config,
    ):
        """
        agent_classes: dict mapping role to agent class string (e.g., {'driver': 'apps.ride_hail.driver.DriverAgentIndie', ...})
        service_classes: dict mapping role to service class string
        scenario_manager: instance of the domain-specific ScenarioManager
        domain: e.g., 'ridehail-sim'
        agent_config: list of dicts, each with:
            - 'collection': attribute name on scenario_manager
            - 'role': agent/service role key
            - 'scheduler': 'agent' or 'service'
            - 'class_type': 'agent' or 'service' (for lookup)
            - 'time_step_key': key in behavior for scheduling (or None for service)
            - 'extra_fields': dict of extra fields to add to spec (optional)
        statemachine_collection: dict of state machine classes
        init_run_config_fn: function(self) to initialize run config, returns run_record
        """
        # Defensive: Validate required arguments

        self.run_id = run_id
        self.domain = domain
        self.scenario_manager = scenario_manager
        # agent_classes and service_classes are now passed via agent_config
        self.run_data_dir = run_data_dir
        self.reference_time = scenario_manager.reference_time
        self.current_time = self.reference_time
        self.statemachine_collection = statemachine_collection
        self.orsim_settings = scenario_manager.orsim_settings
        self.steps = self.orsim_settings['SIMULATION_LENGTH_IN_STEPS']
        self.parent_path = os.path.dirname(os.path.abspath(os.getcwd()))


        # Messaging backend setup (domain-specific)
        from orsim import ORSimEnv
        from apps.config import messenger_backend
        ORSimEnv.set_backend(messenger_backend)

        # Instantiate schedulers from config
        # The order of calls is strict due to the dependency of agent registration on the existence of schedulers.
        self.validate_scheduler_config(scheduler_config)
        self.schedulers = self._instantiate_schedulers(scheduler_config)
        self.agent_registry = {i: [] for i in range(self.steps)}

        self.validate_agent_config(agent_config)
        self._register_agents(agent_config, scenario_manager)

        self.user = self.setup_user()
        self.run_record = self.init_run_config()
        self.register_state_machines()
        self.execution_start_time = time.time()

    def validate_agent_config(self, agent_config):

        # Cerberus schemas for validation
        agent_cfg_schema = {
            'scheduler_key': {
                'type': 'string',
                'required': True,
                #  'A string value that matches the scheduler_config dict keys. This ensures the agent is added to the correct scheduler.'
            },
            'agent_class': {
                'type': 'string',
                'required': True,
                # 'An "agent_class" (import path as string). This is used to dynamically import the agent class for instantiation. It should be a valid import path to an agent class.'
            },
            'init_time_step_key': {
                'nullable': True,
                # 'The "init_time_step_key" Is needed to identify when the agent should be scheduled to start. It should be a key in the behavior dict that indicates the time step for initialization. If the agent is a service that should be initialized at the start of the simulation, this can be set to None.'
            },
            'extra_fields': {
                'nullable': True,
                # 'The "extra_fields" should be a callable or None. If provided, it will be called with (agent_id, behavior, sim) and should return a dict of extra fields to add to the agent spec during registration. This allows for dynamic addition of fields to the agent spec based on the behavior or other factors.'
            },
        }
        agent_config_schema = {
            role: {'type': 'dict', 'schema': agent_cfg_schema, 'required': True}
            for role in agent_config.keys()
        }
        v = cerberus.Validator()
        if not v.validate(agent_config, agent_config_schema):
            raise ValueError(f"Invalid agent_config: {v.errors}")

    def validate_scheduler_config(self, scheduler_config):

        scheduler_cfg_schema = {
            'run_id': {
                'type': 'string',
                'required': True,
                # 'Use the same run_id across all schedulers for this simulation run. This is used for namespacing and logging in the ORSim backend.'
            },
            'scheduler_id': {
                'type': 'string',
                'required': True,
                # 'Each scheduler config must have a unique "scheduler_id" string. This is used for namespacing and logging in the ORSim backend to differentiate between different schedulers.'
            },
            'orsim_settings': {
                'type': 'dict',
                'required': True,
                # 'Each scheduler config must have an "orsim_settings" dict (simulation settings for the scheduler). Even though it is provided by the scenario_manager, It needs to be explicitly specified here to handle the case where different schedulers might need different settings (e.g., different simulation lengths, or other ORSim configuration).'
            },
            'init_failure_handler': {
                'type': 'string',
                'required': False,
                #  'Optional string "init_failure_handler" (e.g., "hard" for strict failure handling).'
            },
        }
        scheduler_config_schema = {
            key: {'type': 'dict', 'schema': scheduler_cfg_schema, 'required': True}
            for key in scheduler_config.keys()
        }
        v = cerberus.Validator()
        if not v.validate(scheduler_config, scheduler_config_schema):
            raise ValueError(f"Invalid scheduler_config: {v.errors}")


    def _register_agents(self, agent_config, scenario_manager):
        """
        Processes agent_config (dict of dicts) and adds agents/services to the appropriate schedulers or agent_registry.
        """
        for role, cfg in agent_config.items():
            collection = scenario_manager.get_agent_collection(role)
            scheduler_key = cfg['scheduler_key']
            scheduler = self.schedulers[scheduler_key]
            agent_class = cfg['agent_class']
            for agent_id, behavior in collection.items():
                spec = {
                    'unique_id': agent_id,
                    'run_id': self.run_id,
                    'reference_time': datetime.strftime(self.reference_time, '%Y%m%d%H%M%S'),
                    'init_time_step': behavior.get(cfg['init_time_step_key'], 0) if cfg['init_time_step_key'] else 0,
                    'behavior': behavior,
                }
                if 'extra_fields' in cfg and cfg['extra_fields']:
                    spec.update(cfg['extra_fields'](agent_id, behavior, self))
                if scheduler_key == 'agent':
                    self.agent_registry[spec['init_time_step']].append({
                        'spec': spec,
                        'project_path': self.parent_path,
                        'agent_class': agent_class,
                    })
                else:
                    scheduler.add_agent(
                        spec=spec,
                        project_path=self.parent_path,
                        agent_class=agent_class,
                    )

    def _instantiate_schedulers(self, scheduler_config):
        """
        scheduler_config: dict mapping scheduler keys to parameter dicts for ORSimScheduler.
        Example:
            {
                'agent': {'run_id': ..., 'scheduler_id': ..., 'orsim_settings': ...},
                'service': {'run_id': ..., 'scheduler_id': ..., 'orsim_settings': ..., 'init_failure_handler': ...},
            }
        """
        from orsim import ORSimScheduler
        schedulers = {}
        for key, params in scheduler_config.items():
            schedulers[key] = ORSimScheduler(**params)
        return schedulers

    def setup_user(self):
        from apps.utils import time_to_str
        credentials = {
            'email': 'sim_admin@test.com',
            'password': 'password',
        }
        user = UserRegistry(time_to_str(datetime.now()), credentials, role='admin')
        return user

    def init_run_config(self):
        run_config_url = f"{settings['OPENRIDE_SERVER_URL']}/run-config"

        run_config_data = {
            "run_id": self.run_id,
            "name": self.scenario_manager.scenario_name,
            "meta": self.scenario_manager.get_run_config_meta(),
            'step_metrics': {},
        }

        response = requests.post(run_config_url, headers=self.user.get_headers(), data=json.dumps(run_config_data))
        if response.status_code in (200, 201):
            return response.json()
        else:
            raise Exception(f"{response.url}, {response.text}")

    def register_state_machines(self):
        StateMachineRegistry(statemachines=self.statemachine_collection, domain=self.domain).register_state_machines(
            server_url=settings['OPENRIDE_SERVER_URL'],
            headers=self.user.get_headers()
        )

    def run_simulation(self):
        # Print agent registry for step 0 for debugging
        print("Agent registry for step 0:")
        print(self.agent_registry.get(0, []))
        for i in range(self.steps):
            print(f"Simulation Step: {self.schedulers['agent'].time} of {self.steps}")
            for item in self.agent_registry[i]:
                agent_obj = self.schedulers['agent'].add_agent(**item)
                if hasattr(agent_obj, 'agent_failed') and getattr(agent_obj, 'agent_failed', False):
                    logging.error(f"Agent {getattr(agent_obj, 'unique_id', 'unknown')} failed to initialize and will not step.")
            # Step all schedulers with defensive fix for agent_stat
            for scheduler in self.schedulers.values():
                # Defensive fix: ensure agent_stat has current time key
                if hasattr(scheduler, 'agent_stat') and hasattr(scheduler, 'time'):
                    if scheduler.time not in scheduler.agent_stat:
                        scheduler.agent_stat[scheduler.time] = []
                asyncio.run(scheduler.step())
            # Capture step metrics for this step
            step_metric = {
                i: {
                    key: {
                        'stat': getattr(self.schedulers[key], 'agent_stat', {}).get(i, None),
                        'run_time': None  # Optionally, you can time each scheduler step if needed
                    } for key in self.schedulers
                }
            }
            self.run_record = self.update_status('In Progress', time.time() - self.execution_start_time, step_metric)
        print("Simulation complete.")
        # Final update at the end of the simulation
        total_run_time = time.time() - self.execution_start_time
        self.run_record = self.update_status('success', total_run_time)

    def update_status(self, status, execution_time=0, step_metric=None):
        """
        Update the run status in the database, including scheduler stats and step metrics.
        Args:
            status (str): Status string (e.g., 'In Progress', 'success', 'failed')
            execution_time (float): Total execution time in seconds
            step_metric (dict): Optional. Step metrics to store (should be a dict with step index as key)
        Returns:
            dict: Updated run record from the server, or None if update failed
        """
        run_config_item_url = f"{settings['OPENRIDE_SERVER_URL']}/run-config/{self.run_record['_id']}"
        data = {
            'status': status,
            'execution_time': execution_time,
        }
        if step_metric is not None:
            for k, v in step_metric.items():
                data[f'step_metrics.{k}'] = v
                break  # Only one step metric per call (for per-step update)
        try:
            response = requests.patch(
                run_config_item_url,
                headers=self.user.get_headers(etag=self.run_record['_etag']),
                data=json.dumps(data)
            )
            if response.status_code in (200, 201):
                return response.json()
            else:
                logging.error(f"Failed to update status: {response.url}, {response.text}")
        except Exception as e:
            logging.error(f"Exception in update_status: {e}")
        return None
