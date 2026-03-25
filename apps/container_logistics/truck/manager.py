from orsim.lifecycle import ORSimManager

from .trip_manager import TruckTripManager

class TruckManager(ORSimManager):
    def __init__(self, run_id, sim_clock, user, persona, messenger=None):
        super().__init__()
        self.trip_manager = TruckTripManager(run_id, sim_clock, user, messenger, persona)

    def create_new_trip(self, sim_clock, current_loc, truck, order, route=None):
        return self.trip_manager.create_new_trip(sim_clock, current_loc, truck, order, route)

    def get_trip(self):
        return self.trip_manager.as_dict()

    def update_trip_state(self, new_state):
        self.trip_manager.update_trip_state(new_state)

    def cancel_trip(self):
        self.trip_manager.cancel_trip()
