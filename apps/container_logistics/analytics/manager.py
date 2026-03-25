from orsim.lifecycle import ORSimManager

class AnalyticsManager(ORSimManager):
    def __init__(self, run_id, sim_clock, user, persona):
        super().__init__()
        # Initialize analytics resource, statemachine, etc.
