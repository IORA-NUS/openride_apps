import logging

from apps.common.trip_manager_base import TripManagerBase
from apps.config import simulation_domains
from apps.container_logistics.statemachine import (
    ContainerLogisticsActions,
    HaulTripStateMachine,
    haultrip_gate_interactions,
    haultrip_order_interactions,
)
from apps.utils import is_success
from apps.utils.excepions import WriteFailedException


class TruckTripManager(TripManagerBase):
    def __init__(self, run_id, sim_clock, user, messenger, persona=None):
        super().__init__(run_id, user, messenger, persona=persona or {"role": "truck"})
        self.simulation_domain = simulation_domains.get("container_logistics", "container-logistics-sim")

    @property
    def StateMachineCls(self):
        return HaulTripStateMachine

    @property
    def message_channel(self):
        return None

    @property
    def statemachine_interaction_mapping(self):
        return haultrip_order_interactions + haultrip_gate_interactions

    def message_template(self, event):
        if event.startswith("order_"):
            return {
                "action": ContainerLogisticsActions.ORDER_WORKFLOW_EVENT,
                "truck_id": self.trip.get("truck"),
                "data": {"event": event},
            }
        return {
            "action": ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
            "truck_id": self.trip.get("truck"),
            "data": {"event": event},
        }

    def as_dict(self):
        return self.trip

    def refresh(self):
        if self.trip is not None:
            response = self._get_trip()
            if is_success(response.status_code):
                self.trip = response.json()
            else:
                raise WriteFailedException(f"Unable to refresh haul trip: {response.url}, {response.text}")

    def create_new_trip(self, sim_clock, current_loc, truck, order):
        data = {
            "truck": truck.get("_id"),
            "order": order.get("_id"),
            "persona": self.persona,
            "meta": {
                "truck_profile": truck.get("profile", {}),
                "order_profile": order.get("profile", {}),
            },
            "current_loc": current_loc,
            "pickup_loc": order.get("pickup_loc"),
            "dropoff_loc": order.get("dropoff_loc"),
            "stats": {
                "estimated_time_to_pickup": order.get("estimated_time_to_pickup", 0),
                "estimated_time_to_dropoff": order.get("estimated_time_to_dropoff", 0),
                "pickup_service_time": order.get("pickup_service_time", 0),
                "dropoff_service_time": order.get("dropoff_service_time", 0),
            },
            "routes": {
                "planned": {
                    "repositioning_to_pickup": order.get("planned_reposition_route"),
                    "loaded_to_dropoff": order.get("planned_dropoff_route"),
                }
            },
            "statemachine": {
                "name": HaulTripStateMachine.__name__,
                "domain": self.simulation_domain,
            },
            "state": HaulTripStateMachine.initial_state.name,
            "sim_clock": sim_clock,
        }
        response = self._post_trip(data)
        if is_success(response.status_code):
            self.trip = {"_id": response.json()["_id"]}
            self.refresh()
            return self.trip
        raise WriteFailedException(f"Unable to create haul trip: {response.url}, {response.text}")

    def assign(self, sim_clock, current_loc, order=None, assignment_metadata=None):
        return self.apply_trip_transition_and_notify(
            transition=HaulTripStateMachine.assign.name,
            data={
                "sim_clock": sim_clock,
                "current_loc": current_loc,
                "order": None if order is None else order.get("_id"),
                "stats.estimated_time_to_pickup": 0 if order is None else order.get("estimated_time_to_pickup", 0),
                "stats.estimated_time_to_dropoff": 0 if order is None else order.get("estimated_time_to_dropoff", 0),
            },
            context={
                "order_id": None if order is None else order.get("_id"),
                "assignment_metadata": assignment_metadata or {},
            },
        )

    def cancel(self, sim_clock, current_loc):
        return self.apply_trip_transition_and_notify(
            transition=HaulTripStateMachine.cancel.name,
            data={"sim_clock": sim_clock, "current_loc": current_loc},
            context={},
        )

    def estimate_next_event_time(self, current_time):
        return current_time

    def start_empty_reposition(self, sim_clock, current_loc, route=None, estimated_time_to_pickup=0):
        return self.apply_trip_transition_and_notify(
            transition=HaulTripStateMachine.start_empty_reposition.name,
            data={
                "sim_clock": sim_clock,
                "current_loc": current_loc,
                "routes.planned.repositioning_to_pickup": route,
                "stats.estimated_time_to_pickup": estimated_time_to_pickup,
            },
            context={
                "planned_route": route,
                "estimated_time_to_pickup": estimated_time_to_pickup,
            },
        )

    def arrive_pickup_queue(self, sim_clock, current_loc, queue_arrival_time=None):
        return self.apply_trip_transition_and_notify(
            transition=HaulTripStateMachine.arrive_pickup_queue.name,
            data={
                "sim_clock": sim_clock,
                "current_loc": current_loc,
                "stats.pickup_queue_arrival_time": queue_arrival_time or sim_clock,
            },
            context={
                "location": current_loc,
                "queue_arrival_time": queue_arrival_time or sim_clock,
            },
        )

    def enter_pickup_gate(self, sim_clock, current_loc, gate_index=None, service_time=0):
        return self.apply_trip_transition_and_notify(
            transition=HaulTripStateMachine.enter_pickup_gate.name,
            data={
                "sim_clock": sim_clock,
                "current_loc": current_loc,
                "gate_index": gate_index,
                "stats.pickup_service_time": service_time,
            },
            context={
                "gate_index": gate_index,
                "service_time": service_time,
            },
        )

    def finish_pickup_service(self, sim_clock, current_loc, route_to_dropoff=None, estimated_time_to_dropoff=0, service_time=None):
        return self.apply_trip_transition_and_notify(
            transition=HaulTripStateMachine.finish_pickup_service.name,
            data={
                "sim_clock": sim_clock,
                "current_loc": current_loc,
                "routes.planned.loaded_to_dropoff": route_to_dropoff,
                "stats.estimated_time_to_dropoff": estimated_time_to_dropoff,
                "stats.pickup_service_time": service_time,
            },
            context={
                "location": current_loc,
                "planned_route": route_to_dropoff,
                "estimated_time_to_dropoff": estimated_time_to_dropoff,
                "service_time": service_time,
            },
        )

    def arrive_dropoff_queue(self, sim_clock, current_loc, queue_arrival_time=None):
        return self.apply_trip_transition_and_notify(
            transition=HaulTripStateMachine.arrive_dropoff_queue.name,
            data={
                "sim_clock": sim_clock,
                "current_loc": current_loc,
                "stats.dropoff_queue_arrival_time": queue_arrival_time or sim_clock,
            },
            context={
                "location": current_loc,
                "queue_arrival_time": queue_arrival_time or sim_clock,
            },
        )

    def enter_dropoff_gate(self, sim_clock, current_loc, gate_index=None, service_time=0):
        return self.apply_trip_transition_and_notify(
            transition=HaulTripStateMachine.enter_dropoff_gate.name,
            data={
                "sim_clock": sim_clock,
                "current_loc": current_loc,
                "gate_index": gate_index,
                "stats.dropoff_service_time": service_time,
            },
            context={
                "gate_index": gate_index,
                "service_time": service_time,
            },
        )

    def finish_dropoff_service(self, sim_clock, current_loc, service_time=None):
        return self.apply_trip_transition_and_notify(
            transition=HaulTripStateMachine.finish_dropoff_service.name,
            data={
                "sim_clock": sim_clock,
                "current_loc": current_loc,
                "stats.dropoff_service_time": service_time,
            },
            context={
                "location": current_loc,
                "service_time": service_time,
            },
        )
