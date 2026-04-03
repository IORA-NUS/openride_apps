from orsim.lifecycle import ORSimApp
from .manager import TruckManager

class TruckApp(ORSimApp):
    def __init__(self, run_id, sim_clock, messenger, persona):
        super().__init__(run_id, sim_clock, messenger=messenger, persona=persona)
        self.manager = TruckManager(run_id, sim_clock, self.user, persona, messenger)

    def create_new_trip(self, sim_clock, current_loc, truck, order, route=None):
        return self.manager.create_new_trip(sim_clock, current_loc, truck, order, route)

    def get_trip(self):
        return self.manager.get_trip()

    def update_trip_state(self, new_state):
        self.manager.update_trip_state(new_state)

    def cancel_trip(self):
        self.manager.cancel_trip()
