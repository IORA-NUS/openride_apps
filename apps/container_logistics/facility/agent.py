from orsim.lifecycle import ORSimAgent
from .app import FacilityApp

class FacilityAgent(ORSimAgent):
    def __init__(self, unique_id, run_id, reference_time, init_time_step, scheduler, behavior):
        super().__init__(unique_id, run_id, reference_time, init_time_step, scheduler, behavior)
        self.app = FacilityApp(run_id, self.get_current_time_str(), self.messenger, self.behavior.get('persona', {}))
