from orsim.lifecycle import ORSimManager

class FacilityManager(ORSimManager):
    def __init__(self, run_id, sim_clock, user, persona):
        super().__init__()
        # Initialize facility resource, statemachine, etc.
