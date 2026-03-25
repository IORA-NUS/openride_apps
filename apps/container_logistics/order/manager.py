from orsim.lifecycle import ORSimManager

class OrderManager(ORSimManager):
    def __init__(self, run_id, sim_clock, user, persona):
        super().__init__()
        # Initialize order resource, statemachine, etc.
