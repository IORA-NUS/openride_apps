from orsim.lifecycle import ORSimApp
from .manager import AssignmentManager

class AssignmentApp(ORSimApp):
    def __init__(self, run_id, sim_clock, messenger, persona):
        super().__init__(run_id, sim_clock, messenger=messenger, persona=persona)
        self.manager = AssignmentManager(run_id, sim_clock, self.user, persona)
