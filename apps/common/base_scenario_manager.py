
import os
from datetime import datetime
from abc import ABC, abstractmethod

class BaseScenarioManager(ABC):
    """
    Abstract base class for all ScenarioManagers.
    Only essential interfaces are defined. Subclasses must implement required methods.
    """
    def __init__(self, scenario_name, domain, run_data_dir=None):
        self.scenario_name = scenario_name
        self.domain = domain
        self.run_data_dir = run_data_dir
        self.reference_time = datetime(2020, 1, 1, 8, 0, 0)
        # Global simulation configuration for the scenario (must be set by subclass)
        self._orsim_settings = None
        self.collections = {}  # e.g., {'driver': {...}, 'passenger': {...}}
        self.setup_data_dirs()

        self.load_or_generate_behaviors()

    @property
    def orsim_settings(self):
        """
        Global simulation configuration for the scenario. Must be set by subclass during setup.
        """
        return self._orsim_settings

    @orsim_settings.setter
    def orsim_settings(self, value):
        self._orsim_settings = value

    def setup_data_dirs(self):
        if self.run_data_dir is not None:
            self.base_datahub = os.path.abspath(os.path.join(self.run_data_dir, '..', '..'))
        else:
            # fallback for legacy usage
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
            self.base_datahub = os.path.join(project_root, 'datahub', self.domain)
        self.behavior_dir = os.path.join(self.base_datahub, 'dataset', self.scenario_name)
        self.processed_input_dir = os.path.join(self.base_datahub, 'processed_input', self.scenario_name)
        os.makedirs(self.behavior_dir, exist_ok=True)

    @abstractmethod
    def load_or_generate_behaviors(self):
        """
        Subclasses must implement this method to load or generate agent/service behaviors.
        Should populate self.collections and set self.orsim_settings as needed.
        """
        pass

    def get_agent_collection(self, key):
        """
        Return the agent/service collection for the given key (domain-specific keys).
        """
        return self.collections.get(key, {})

    @abstractmethod
    def get_run_config_meta(self):
        """
        Return a dict of metadata for run config. Subclasses must implement this.
        """
        pass
