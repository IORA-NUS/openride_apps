from apps.container_logistics.statemachine.haul_trip_sm import HaulTripStateMachine
from orsim.lifecycle import ORSimManager
import logging

class TruckTripManager(ORSimManager):
    def __init__(self, run_id, sim_clock, user, messenger, persona=None):
        super().__init__()
        self.run_id = run_id
        self.sim_clock = sim_clock
        self.user = user
        self.messenger = messenger
        self.persona = persona
        self.trip = None
        self.simulation_domain = 'container_logistics'

    def as_dict(self):
        return self.trip

    def create_new_trip(self, sim_clock, current_loc, truck, order, route=None):
        data = {
            "truck": f"{truck['_id']}",
            'persona': self.persona,
            "meta": {
                'profile': truck.get('profile', {}),
            },
            "order": f"{order['_id']}",
            "current_loc": current_loc,
            "next_dest_loc": current_loc,
            "statemachine": {
                "name": "HaulTripStateMachine",
                "domain": self.simulation_domain,
            },
            "state": HaulTripStateMachine.initial_state.name,
            "sim_clock": sim_clock,
        }
        # TODO: Implement trip creation logic (e.g., POST to server or DB)
        self.trip = data
        logging.info(f"Created new truck trip: {data}")
        return self.trip

    def refresh(self):
        # TODO: Implement refresh logic (e.g., fetch latest trip state from server)
        pass

    def update_trip_state(self, new_state):
        if self.trip:
            self.trip['state'] = new_state
            logging.info(f"Trip state updated to {new_state}")

    def cancel_trip(self):
        if self.trip:
            self.trip['state'] = HaulTripStateMachine.cancelled.name
            logging.info("Trip cancelled.")
