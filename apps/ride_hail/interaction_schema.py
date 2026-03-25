



from abc import ABC, abstractmethod
from typing import Any
from orsim.messenger.interaction import message_handler, state_handler
from apps.ride_hail import RideHailEvents

# See interaction_map.py for explicit statemachine interaction definitions and visualization helpers.

# === COMMON BASE INTERACTION ===
class BaseInteraction(ABC):
    def __init__(self, agent):
        self.agent = agent
    # Add shared utilities/logging here if needed




# Example implementation for driver
class DriverTripInteraction(BaseInteraction):
    @message_handler(RideHailEvents.PASSENGER_CONFIRMED_TRIP)
    def on_passenger_confirmed_trip(self, payload: Any, data: Any):
        self.agent.set_route(self.agent.current_loc, self.agent.app.get_trip()['pickup_loc'])
        self.agent.app.trip.passenger_confirmed_trip(
            self.agent.get_current_time_str(),
            current_loc=self.agent.current_loc,
            route=self.agent.active_route
        )

    @message_handler(RideHailEvents.PASSENGER_REJECTED_TRIP)
    def on_passenger_rejected_trip(self, payload: Any, data: Any):
        self.agent.app.trip.force_quit(self.agent.get_current_time_str(), current_loc=self.agent.current_loc)
        if self.agent.action_when_free == 'random_walk':
            self.agent.set_route(self.agent.current_loc, self.agent.behavior['empty_dest_loc'])
        elif self.agent.action_when_free == 'stay':
            self.agent.set_route(self.agent.current_loc, None)
        self.agent.app.create_new_unoccupied_trip(
            self.agent.get_current_time_str(),
            current_loc=self.agent.current_loc,
            route=self.agent.active_route
        )

    @message_handler(RideHailEvents.PASSENGER_ACKNOWLEDGE_PICKUP)
    def on_passenger_acknowledge_pickup(self, payload: Any, data: Any):
        self.agent.set_route(self.agent.current_loc, self.agent.app.get_trip()['dropoff_loc'])
        self.agent.app.trip.passenger_acknowledge_pickup(
            self.agent.get_current_time_str(),
            current_loc=self.agent.current_loc,
            route=self.agent.active_route
        )

    @message_handler(RideHailEvents.PASSENGER_ACKNOWLEDGE_DROPOFF)
    def on_passenger_acknowledge_dropoff(self, payload: Any, data: Any):
        self.agent.app.trip.passenger_acknowledge_dropoff(
            self.agent.get_current_time_str(),
            current_loc=self.agent.current_loc
        )

    @state_handler('driver_looking_for_job')
    def on_state_looking_for_job(self, time_since_last_event):
        from shapely.geometry import Point
        if type(self.agent.projected_path) == Point:
            self.agent.app.trip.end_trip(self.agent.get_current_time_str(), current_loc=self.agent.current_loc)
            self.agent.set_route(self.agent.current_loc, self.agent.get_random_location())
            self.agent.app.create_new_unoccupied_trip(
                self.agent.get_current_time_str(),
                current_loc=self.agent.current_loc,
                route=self.agent.active_route
            )

    @state_handler('driver_received_trip')
    def on_state_received_trip(self, time_since_last_event):
        from random import random
        if random() <= self.agent.get_transition_probability(('accept', self.agent.app.get_trip()['state']), 1):
            estimated_time_to_arrive = self.agent.get_tentative_travel_time(
                self.agent.current_loc, self.agent.app.get_trip()['pickup_loc'])
            self.agent.app.trip.confirm(
                self.agent.get_current_time_str(),
                current_loc=self.agent.current_loc,
                estimated_time_to_arrive=estimated_time_to_arrive
            )
        else:
            self.agent.app.trip.reject(
                self.agent.get_current_time_str(),
                current_loc=self.agent.current_loc
            )
            self.agent.app.create_new_unoccupied_trip(
                self.agent.get_current_time_str(),
                current_loc=self.agent.current_loc,
                route=self.agent.active_route
            )

    @state_handler('driver_moving_to_pickup')
    def on_state_moving_to_pickup(self, time_since_last_event):
        import haversine as hs
        distance = hs.haversine(
            reversed(self.agent.current_loc['coordinates'][:2]),
            reversed(self.agent.app.get_trip()['pickup_loc']['coordinates'][:2]),
            unit=hs.Unit.METERS,
        )
        if distance < 100:
            self.agent.app.trip.wait_to_pickup(
                self.agent.get_current_time_str(),
                current_loc=self.agent.current_loc
            )

    @state_handler('driver_pickedup')
    def on_state_pickedup(self, time_since_last_event):
        if time_since_last_event >= self.agent.behavior['transition_time_pickup']:
            self.agent.app.trip.move_to_dropoff(
                self.agent.get_current_time_str(),
                current_loc=self.agent.current_loc
            )

    @state_handler('driver_moving_to_dropoff')
    def on_state_moving_to_dropoff(self, time_since_last_event):
        import haversine as hs
        distance = hs.haversine(
            reversed(self.agent.current_loc['coordinates'][:2]),
            reversed(self.agent.app.get_trip()['dropoff_loc']['coordinates'][:2]),
            unit=hs.Unit.METERS,
        )
        if distance < 100:
            self.agent.app.trip.wait_to_dropoff(
                self.agent.get_current_time_str(),
                current_loc=self.agent.current_loc
            )

    @state_handler('driver_droppedoff')
    def on_state_droppedoff(self, time_since_last_event):
        if time_since_last_event >= self.agent.behavior['transition_time_dropoff']:
            self.agent.app.trip.end_trip(
                self.agent.get_current_time_str(),
                current_loc=self.agent.current_loc
            )



# Example implementation for passenger
class PassengerTripInteraction(BaseInteraction):
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
