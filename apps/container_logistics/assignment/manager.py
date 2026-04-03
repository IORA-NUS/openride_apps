from orsim.lifecycle import ORSimManager

class AssignmentManager(ORSimManager):
    def __init__(self, run_id, sim_clock, user, persona):
        super().__init__()
        # Initialize assignment resource, statemachine, etc.
