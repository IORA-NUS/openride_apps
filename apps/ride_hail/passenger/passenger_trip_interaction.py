



from abc import ABC, abstractmethod
from typing import Any
from orsim.messenger.interaction import message_handler, state_handler
from apps.ride_hail import RideHailEvents

# # See interaction_map.py for explicit statemachine interaction definitions and visualization helpers.

# Example implementation for passenger
class PassengerTripInteraction():
    @message_handler(RideHailEvents.DRIVER_CONFIRMED_TRIP)
    def on_driver_confirmed_trip(self, payload: Any, data: Any):
        self.agent.app.trip.driver_confirmed_trip(
            self.agent.get_current_time_str(),
            self.agent.current_loc,
            data.get('estimated_time_to_arrive', 0),
        )

    @message_handler(RideHailEvents.DRIVER_ARRIVED_FOR_PICKUP)
    def on_driver_arrived_for_pickup(self, payload: Any, data: Any):
        if data.get('location') is None:
            return
        self.agent.current_loc = data.get('location')
        self.agent.app.trip.driver_arrived_for_pickup(
            self.agent.get_current_time_str(),
            self.agent.current_loc,
            data.get('driver_trip_id')
        )

    @message_handler(RideHailEvents.DRIVER_MOVE_FOR_DROPOFF)
    def on_driver_move_for_dropoff(self, payload: Any, data: Any):
        if data.get('location') is None:
            return
        self.agent.current_loc = data.get('location')
        self.agent.app.trip.driver_move_for_dropoff(
            self.agent.get_current_time_str(),
            self.agent.current_loc,
            route=data['planned_route']
        )

    @message_handler(RideHailEvents.DRIVER_ARRIVED_FOR_DROPOFF)
    def on_driver_arrived_for_dropoff(self, payload: Any, data: Any):
        if data.get('location') is None:
            return
        self.agent.current_loc = data.get('location')
        self.agent.app.trip.driver_arrived_for_dropoff(
            self.agent.get_current_time_str(),
            self.agent.current_loc
        )

    @message_handler(RideHailEvents.DRIVER_WAITING_FOR_DROPOFF)
    def on_driver_waiting_for_dropoff(self, payload: Any, data: Any):
        if data.get('location') is None:
            return
        self.agent.current_loc = data.get('location')
        self.agent.app.trip.driver_waiting_for_dropoff(
            self.agent.get_current_time_str(),
            self.agent.current_loc
        )

    @message_handler(RideHailEvents.DRIVER_CANCELLED_TRIP)
    def on_driver_cancelled_trip(self, payload: Any, data: Any):
        if data.get('location') is None:
            return
        self.agent.current_loc = data.get('location', self.agent.current_loc)
        self.agent.app.trip.driver_cancelled_trip(
            self.agent.get_current_time_str(),
            self.agent.current_loc
        )

    @state_handler('passenger_received_trip_confirmation')
    def on_state_received_trip_confirmation(self):
        from random import random
        if random() <= self.agent.get_transition_probability(('accept', self.agent.app.get_trip()['state']), 1):
            self.agent.app.trip.accept(
                self.agent.get_current_time_str(),
                current_loc=self.agent.current_loc
            )
        else:
            self.agent.app.trip.reject(
                self.agent.get_current_time_str(),
                current_loc=self.agent.current_loc
            )

    @state_handler('passenger_accepted_trip')
    def on_state_accepted_trip(self):
        self.agent.app.trip.wait_for_pickup(
            self.agent.get_current_time_str(),
            current_loc=self.agent.current_loc
        )

    @state_handler('passenger_droppedoff')
    def on_state_droppedoff(self):
        self.agent.app.trip.end_trip(
            self.agent.get_current_time_str(),
            current_loc=self.agent.current_loc
        )

# Usage in agent:
# self.interaction = TripManagerInteraction(self)
# Then, use interaction_registry to dispatch events to the correct handler.
