import json
import logging
import requests
import time

from orsim.runtime import ORSimRuntime


class SimulationRuntime(ORSimRuntime):
    """
    Ridehail-specific simulation runtime.

    Thin subclass of ORSimRuntime that implements the three domain lifecycle
    hooks for the openride backend: user auth, run-config registration, and
    state-machine registration.  All orchestration logic lives in ORSimRuntime.

    Args:
        run_id:                 Unique simulation run identifier.
        domain:                 Domain string (e.g. 'ridehail-sim').
        scheduler_config:       Passed through to ORSimRuntime.
        agent_source:           Passed through to ORSimRuntime.
        termination_condition:  Passed through to ORSimRuntime.
        scenario_manager:       Used for run-config metadata.
        statemachine_collection: Dict of state machine classes to register.
        datahub_dir:            Optional path forwarded to analytics agents.
    """

    def __init__(
        self,
        run_id,
        domain,
        scheduler_config,
        agent_source,
        termination_condition,
        scenario_manager,
        statemachine_collection,
        datahub_dir=None,
    ):
        from apps.config import messenger_backend

        self.domain = domain
        self.scenario_manager = scenario_manager
        self.statemachine_collection = statemachine_collection
        self.datahub_dir = datahub_dir

        # Set by on_before_run; declared here so type-checkers are satisfied.
        self.user = None
        self.run_record = None

        super().__init__(
            run_id=run_id,
            scheduler_config=scheduler_config,
            agent_source=agent_source,
            termination_condition=termination_condition,
            messenger_backend=messenger_backend,
        )

    # ------------------------------------------------------------------ hooks

    def on_before_run(self):
        self.user = self._setup_user()
        self.run_record = self._init_run_config()
        self._register_state_machines()

    def on_step_complete(self, step: int, elapsed: float):
        step_metric = {
            step: {
                key: {
                    'stat': getattr(self.schedulers[key], 'agent_stat', {}).get(step),
                    'run_time': None,
                }
                for key in self.schedulers
            }
        }
        self.run_record = self._update_status('In Progress', elapsed, step_metric)

    def on_simulation_complete(self, elapsed: float):
        self._update_status('success', elapsed)

    # -------------------------------------------------------- private helpers

    def _setup_user(self):
        from apps.utils import time_to_str
        from apps.common.user_registry import UserRegistry
        from datetime import datetime
        credentials = {'email': 'sim_admin@test.com', 'password': 'password'}
        return UserRegistry(time_to_str(datetime.now()), credentials, role='admin')

    def _init_run_config(self):
        from apps.config import settings
        data = {
            'run_id': self.run_id,
            'name': self.scenario_manager.scenario_name,
            'meta': self.scenario_manager.get_run_config_meta(),
            'step_metrics': {},
        }
        response = requests.post(
            f"{settings['OPENRIDE_SERVER_URL']}/run-config",
            headers=self.user.get_headers(),
            data=json.dumps(data),
        )
        if response.status_code in (200, 201):
            return response.json()
        raise Exception(f"{response.url}, {response.text}")

    def _register_state_machines(self):
        from apps.config import settings
        from apps.common.statemachine_registry import StateMachineRegistry
        StateMachineRegistry(
            statemachines=self.statemachine_collection,
            domain=self.domain,
        ).register_state_machines(
            server_url=settings['OPENRIDE_SERVER_URL'],
            headers=self.user.get_headers(),
        )

    def _update_status(self, status, execution_time=0, step_metric=None):
        from apps.config import settings
        data = {'status': status, 'execution_time': execution_time}
        if step_metric is not None:
            for k, v in step_metric.items():
                data[f'step_metrics.{k}'] = v
                break
        try:
            response = requests.patch(
                f"{settings['OPENRIDE_SERVER_URL']}/run-config/{self.run_record['_id']}",
                headers=self.user.get_headers(etag=self.run_record['_etag']),
                data=json.dumps(data),
            )
            if response.status_code in (200, 201):
                return response.json()
            logging.error(f"Failed to update status: {response.url}, {response.text}")
        except Exception as e:
            logging.error(f"Exception in _update_status: {e}")
        return None
