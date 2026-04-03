from orsim.lifecycle import ORSimAgent
from .app import TruckApp


from orsim.messenger.interaction import message_handler, state_handler, InteractionContext
from apps.container_logistics.statemachine.haul_trip_sm import HaulTripStateMachine
import logging
from apps.loc_service import OSRMClient


class TruckAgent(ORSimAgent):
    def __init__(self, unique_id, run_id, reference_time, init_time_step, scheduler, behavior):
        super().__init__(unique_id, run_id, reference_time, init_time_step, scheduler, behavior)
        self.app = TruckApp(run_id, self.get_current_time_str(), self.messenger, self.behavior.get('persona', {}))
        self.active = False

    def step(self, time_step):
        """
        Main simulation step: process messages and update trip state.
        """
        self.app.update_current(self.get_current_time_str(), getattr(self, 'current_loc', None))
        self.consume_messages()
        self.perform_workflow_actions()
        return True

    def consume_messages(self):
        payload = self.app.dequeue_message() if hasattr(self.app, 'dequeue_message') else None
        while payload is not None:
            try:
                # Dispatch via interaction plugin or handle as needed
                self._interaction_plugin.on_message(
                    InteractionContext(
                        action=payload.get('action'),
                        event=payload.get('event'),
                        payload=payload,
                    )
                )
                payload = self.app.dequeue_message()
            except Exception as e:
                logging.exception(f"TruckAgent[{self.unique_id}]: Error processing message: {e}")
                break

    def set_route(self, from_loc, to_loc):
        ''' find a Feasible route using some routeing engine'''
        if to_loc is not None:
            self.active_route = OSRMClient.get_route(from_loc, to_loc)
            self.projected_path = OSRMClient.get_coords_from_route(self.active_route)
            self.traversed_path = []
            print(f"DriverAgentIndie[{self.unique_id}]: Setting route from {from_loc} to {to_loc}")
            print(f"DriverAgentIndie[{self.unique_id}]: Active route set with duration {self.active_route['duration']} seconds and distance {self.active_route['distance']} meters")
        else:
            self.active_route = None
            self.projected_path = []
            self.traversed_path = []
            print(f"DriverAgentIndie[{self.unique_id}]: No route set as to_loc is None")


    def perform_workflow_actions(self):
        """
        Execute workflow actions in sequence using state handlers, allowing for nuanced pre-state-update logic.
        """
        trip = self.app.get_trip() if hasattr(self.app, 'get_trip') else None
        if not trip:
            return
        state = trip['state']
        # Nuanced pre-state-update logic can be inserted here, e.g.:
        # if state == HaulTripStateMachine.en_route_to_pickup.name and self._arrived_at_pickup():
        #     ...custom logic...
        #     self.app.update_trip_state(HaulTripStateMachine.waiting_at_pickup.name)
        # else:
        #     self._interaction_plugin.on_state(...)

        # Default: let the interaction plugin handle the state
        self._interaction_plugin.on_state(
            InteractionContext(
                state=state,
                extra={},
            )
        )
        # After state handler, check for state change and log
        new_state = self.app.get_trip()['state']
        if new_state != state:
            print(f"TruckAgent [{self.unique_id}]: State changed from {state} to {new_state}")

    # Facility workflow event: gate available for pickup
    @message_handler('facility_workflow_event', 'gate_available')
    def _on_facility_gate_available(self, payload, data=None):
        trip = self.app.get_trip()
        # Insert nuanced logic before state update if needed
        if trip['state'] == HaulTripStateMachine.waiting_at_pickup.name:
            # e.g., check if truck is ready, log event, etc.
            self.app.update_trip_state(HaulTripStateMachine.at_pickup_gate.name)
        elif trip['state'] == HaulTripStateMachine.waiting_at_dropoff.name:
            self.app.update_trip_state(HaulTripStateMachine.at_dropoff_gate.name)

    # Facility workflow event: start loading
    @message_handler('facility_workflow_event', 'start_loading')
    def _on_facility_start_loading(self, payload, data=None):
        trip = self.app.get_trip()
        if trip['state'] == HaulTripStateMachine.at_pickup_gate.name:
            # Insert nuanced logic before state update if needed
            self.app.update_trip_state(HaulTripStateMachine.loading.name)

    # Facility workflow event: finish loading
    @message_handler('facility_workflow_event', 'finish_loading')
    def _on_facility_finish_loading(self, payload, data=None):
        trip = self.app.get_trip()
        if trip['state'] == HaulTripStateMachine.loading.name:
            # Insert nuanced logic before state update if needed
            self.app.update_trip_state(HaulTripStateMachine.loaded.name)

    # Facility workflow event: start unloading
    @message_handler('facility_workflow_event', 'start_unloading')
    def _on_facility_start_unloading(self, payload, data=None):
        trip = self.app.get_trip()
        if trip['state'] == HaulTripStateMachine.at_dropoff_gate.name:
            # Insert nuanced logic before state update if needed
            self.app.update_trip_state(HaulTripStateMachine.unloading.name)

    # Facility workflow event: finish unloading
    @message_handler('facility_workflow_event', 'finish_unloading')
    def _on_facility_finish_unloading(self, payload, data=None):
        trip = self.app.get_trip()
        if trip['state'] == HaulTripStateMachine.unloading.name:
            # Insert nuanced logic before state update if needed
            self.app.update_trip_state(HaulTripStateMachine.completed.name)

    # Order workflow event: assigned
    @message_handler('order_workflow_event', 'order_assigned')
    def _on_order_assigned(self, payload, data=None):
        trip = self.app.get_trip()
        if trip['state'] == HaulTripStateMachine.created.name:
            # Insert nuanced logic before state update if needed
            self.app.update_trip_state(HaulTripStateMachine.assigned.name)

    # Order workflow event: cancel
    @message_handler('order_workflow_event', 'order_cancelled')
    def _on_order_cancelled(self, payload, data=None):
        trip = self.app.get_trip()
        if trip['state'] not in [HaulTripStateMachine.completed.name, HaulTripStateMachine.cancelled.name]:
            # Insert nuanced logic before state update if needed
            self.app.cancel_trip()

    # State handler: assigned (start trip to pickup)
    @state_handler(HaulTripStateMachine.assigned.name)
    def _on_state_assigned(self, extra):
        trip = self.app.get_trip()
        # Insert nuanced logic before state update if needed
        # self.set_route(self.current_loc, trip['pickup_loc']) # implement as needed
        self.app.update_trip_state(HaulTripStateMachine.en_route_to_pickup.name)

    # State handler: en_route_to_pickup (arrive at pickup)
    @state_handler(HaulTripStateMachine.en_route_to_pickup.name)
    def _on_state_en_route_to_pickup(self, extra):
        trip = self.app.get_trip()
        # Insert nuanced logic before state update if needed
        # If arrived at pickup (implement location check as needed)
        self.app.update_trip_state(HaulTripStateMachine.waiting_at_pickup.name)

    # State handler: at_pickup_gate (start loading)
    @state_handler(HaulTripStateMachine.at_pickup_gate.name)
    def _on_state_at_pickup_gate(self, extra):
        trip = self.app.get_trip()
        # Insert nuanced logic before state update if needed
        self.app.update_trip_state(HaulTripStateMachine.loading.name)

    # State handler: loading (finish loading)
    @state_handler(HaulTripStateMachine.loading.name)
    def _on_state_loading(self, extra):
        trip = self.app.get_trip()
        # Insert nuanced logic before state update if needed
        self.app.update_trip_state(HaulTripStateMachine.loaded.name)

    # State handler: loaded (start trip to dropoff)
    @state_handler(HaulTripStateMachine.loaded.name)
    def _on_state_loaded(self, extra):
        trip = self.app.get_trip()
        # Insert nuanced logic before state update if needed
        # self.set_route(self.current_loc, trip['dropoff_loc']) # implement as needed
        self.app.update_trip_state(HaulTripStateMachine.en_route_to_dropoff.name)

    # State handler: en_route_to_dropoff (arrive at dropoff)
    @state_handler(HaulTripStateMachine.en_route_to_dropoff.name)
    def _on_state_en_route_to_dropoff(self, extra):
        trip = self.app.get_trip()
        # Insert nuanced logic before state update if needed
        # If arrived at dropoff (implement location check as needed)
        self.app.update_trip_state(HaulTripStateMachine.waiting_at_dropoff.name)

    # State handler: at_dropoff_gate (start unloading)
    @state_handler(HaulTripStateMachine.at_dropoff_gate.name)
    def _on_state_at_dropoff_gate(self, extra):
        trip = self.app.get_trip()
        # Insert nuanced logic before state update if needed
        self.app.update_trip_state(HaulTripStateMachine.unloading.name)

    # State handler: unloading (finish unloading)
    @state_handler(HaulTripStateMachine.unloading.name)
    def _on_state_unloading(self, extra):
        trip = self.app.get_trip()
        # Insert nuanced logic before state update if needed
        self.app.update_trip_state(HaulTripStateMachine.completed.name)
