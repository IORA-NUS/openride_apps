



from abc import ABC, abstractmethod
from typing import Any
from orsim.messenger.interaction import message_handler, state_handler
from apps.ride_hail import RideHailEvents
from random import choice, randint, random

from apps.ride_hail.statemachine import RidehailPassengerTripStateMachine

from apps.ride_hail import RideHailActions, validate_assigned_payload
from apps.ride_hail import RideHailActions, RideHailEvents, validate_driver_workflow_payload

# # See interaction_map.py for explicit statemachine interaction definitions and visualization helpers.

# Example implementation for passenger
class DriverInteractionMixin():
    # ...existing code...

    @message_handler(RideHailActions.DRIVER_WORKFLOW_EVENT, RideHailEvents.DRIVER_CONFIRMED_TRIP)
    def _on_driver_confirmed_trip(self, payload, data):
        self.trip.driver_confirmed_trip(
            self.current_time_str,
            self.current_loc,
            data.get('estimated_time_to_arrive', 0),
        )

    @message_handler(RideHailActions.DRIVER_WORKFLOW_EVENT, RideHailEvents.DRIVER_ARRIVED_FOR_PICKUP)
    def _on_driver_arrived_for_pickup(self, payload, data):
        if data.get('location') is None:
            return
        self.current_loc = data.get('location')
        self.trip.driver_arrived_for_pickup(self.current_time_str, self.current_loc, data.get('driver_trip_id'))

    @message_handler(RideHailActions.DRIVER_WORKFLOW_EVENT, RideHailEvents.DRIVER_MOVE_FOR_DROPOFF)
    def _on_driver_move_for_dropoff(self, payload, data):
        if data.get('location') is None:
            return
        self.current_loc = data.get('location')
        self.trip.driver_move_for_dropoff(self.current_time_str, self.current_loc, route=data['planned_route'])

    @message_handler(RideHailActions.DRIVER_WORKFLOW_EVENT, RideHailEvents.DRIVER_ARRIVED_FOR_DROPOFF)
    def _on_driver_arrived_for_dropoff(self, payload, data):
        if data.get('location') is None:
            return
        self.current_loc = data.get('location')
        self.trip.driver_arrived_for_dropoff(self.current_time_str, self.current_loc)

    @message_handler(RideHailActions.DRIVER_WORKFLOW_EVENT, RideHailEvents.DRIVER_WAITING_FOR_DROPOFF)
    def _on_driver_waiting_for_dropoff(self, payload, data):
        if data.get('location') is None:
            return
        self.current_loc = data.get('location')
        self.trip.driver_waiting_for_dropoff(self.current_time_str, self.current_loc)

    @message_handler(RideHailActions.DRIVER_WORKFLOW_EVENT, RideHailEvents.DRIVER_CANCELLED_TRIP)
    def _on_driver_cancelled_trip(self, payload, data):
        if data.get('location') is None:
            return
        self.current_loc = data.get('location', self.current_loc)
        self.trip.driver_cancelled_trip(self.current_time_str, self.current_loc)

    @state_handler(RidehailPassengerTripStateMachine.passenger_received_trip_confirmation.name)
    def _on_state_received_trip_confirmation(self):
        print(f"PassengerApp [{self.manager.get_id()}]: Received Trip Confirmation. Current Trip State: {self.get_trip()['state']}")
        # if random() <= self.get_transition_probability(('accept', self.get_trip()['state']), 1):
        if random() <= self.agent_helper.get_transition_probability(('accept', self.get_trip()['state']), 1):
            self.trip.accept(self.current_time_str, current_loc=self.current_loc)
            print(f"PassengerApp [{self.manager.get_id()}]: Trip Accepted.")
        else:
            self.trip.reject(self.current_time_str, current_loc=self.current_loc)
            print(f"PassengerApp [{self.manager.get_id()}]: Trip Rejected.")

        print(f"PassengerApp [{self.manager.get_id()}]: Current Trip State after decision: {self.get_trip()['state']}")

    @state_handler(RidehailPassengerTripStateMachine.passenger_accepted_trip.name)
    def _on_state_accepted_trip(self):
        self.trip.wait_for_pickup(self.current_time_str, current_loc=self.current_loc)

    @state_handler(RidehailPassengerTripStateMachine.passenger_droppedoff.name)
    def _on_state_droppedoff(self):
        self.trip.end_trip(self.current_time_str, current_loc=self.current_loc)

