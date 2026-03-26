from abc import ABC, abstractmethod
from typing import Any
from orsim.messenger.interaction import message_handler, state_handler
from apps.ride_hail import RideHailEvents

from apps.ride_hail.statemachine import RidehailDriverTripStateMachine
from apps.loc_service import TaxiStop, BusStop, cut, cut_route, create_route, get_tentative_travel_time
from apps.ride_hail import RideHailActions, RideHailEvents, validate_passenger_workflow_payload
from shapely.geometry import Point, mapping
from shapely.geometry.linestring import LineString

import haversine as hs
from random import choice, randint, random

# Example implementation for driver
class PassengerInteractionMixin():

    @message_handler(RideHailActions.PASSENGER_WORKFLOW_EVENT, RideHailEvents.PASSENGER_CONFIRMED_TRIP)
    def _on_passenger_confirmed_trip(self, payload, data):
        self.active_route, self.projected_path, self.traversed_path = create_route(self.current_loc, self.get_trip()['pickup_loc'])
        self.trip.passenger_confirmed_trip(self.current_time_str, current_loc=self.current_loc, route=self.active_route)

    @message_handler(RideHailActions.PASSENGER_WORKFLOW_EVENT, RideHailEvents.PASSENGER_REJECTED_TRIP)
    def _on_passenger_rejected_trip(self, payload, data):
        self.trip.force_quit(self.current_time_str, current_loc=self.current_loc)

        if self.behavior.get('action_when_free') == 'random_walk':
            self.active_route, self.projected_path, self.traversed_path = create_route(self.current_loc, self.behavior.get('empty_dest_loc')) # self.behavior['empty_dest_loc'])
        elif self.behavior.get('action_when_free') == 'stay':
            self.active_route, self.projected_path, self.traversed_path = create_route(self.current_loc, None)

        self.create_new_unoccupied_trip(self.current_time_str, current_loc=self.current_loc, route=self.active_route)

    @message_handler(RideHailActions.PASSENGER_WORKFLOW_EVENT, RideHailEvents.PASSENGER_ACKNOWLEDGE_PICKUP)
    def _on_passenger_acknowledge_pickup(self, payload, data):
        self.active_route, self.projected_path, self.traversed_path = create_route(self.current_loc, self.get_trip()['dropoff_loc'])
        self.trip.passenger_acknowledge_pickup(self.current_time_str, current_loc=self.current_loc, route=self.active_route)

    @message_handler(RideHailActions.PASSENGER_WORKFLOW_EVENT, RideHailEvents.PASSENGER_ACKNOWLEDGE_DROPOFF)
    def _on_passenger_acknowledge_dropoff(self, payload, data):
        self.trip.passenger_acknowledge_dropoff(self.current_time_str, current_loc=self.current_loc)

    @state_handler(RidehailDriverTripStateMachine.driver_looking_for_job.name)
    def _on_state_looking_for_job(self, time_since_last_event):
        if type(self.projected_path) == Point:
            self.trip.end_trip(self.current_time_str, current_loc=self.current_loc)
            self.active_route, self.projected_path, self.traversed_path = create_route(self.current_loc, self.get_random_location())
            self.create_new_unoccupied_trip(self.current_time_str, current_loc=self.current_loc, route=self.active_route)

    @state_handler(RidehailDriverTripStateMachine.driver_received_trip.name)
    def _on_state_received_trip(self, time_since_last_event):
        print(f"DriverApp [{self.manager.get_id()}]: Received Trip Request.")
        if random() <= self.agent_helper.get_transition_probability(('accept', self.get_trip()['state']), 1):
            estimated_time_to_arrive = get_tentative_travel_time(self.current_loc, self.get_trip()['pickup_loc'])
            self.trip.confirm(self.current_time_str, current_loc=self.current_loc, estimated_time_to_arrive=estimated_time_to_arrive)
            print(f"DriverApp [{self.manager.get_id()}]: Trip Confirmed.")
        else:
            self.trip.reject(self.current_time_str, current_loc=self.current_loc)
            self.create_new_unoccupied_trip(self.current_time_str, current_loc=self.current_loc, route=self.active_route)
            print(f"DriverApp [{self.manager.get_id()}]: Trip Rejected.")

        print(f"DriverApp [{self.manager.get_id()}]: after _on_state_received_trip Current Trip State: {self.get_trip()['state']}")

    @state_handler(RidehailDriverTripStateMachine.driver_moving_to_pickup.name)
    def _on_state_moving_to_pickup(self, time_since_last_event):
        distance = hs.haversine(
            reversed(self.current_loc['coordinates'][:2]),
            reversed(self.get_trip()['pickup_loc']['coordinates'][:2]),
            unit=hs.Unit.METERS,
        )
        if distance < 100:
            self.trip.wait_to_pickup(self.current_time_str, current_loc=self.current_loc)

    @state_handler(RidehailDriverTripStateMachine.driver_pickedup.name)
    def _on_state_pickedup(self, time_since_last_event):
        if time_since_last_event >= self.behavior.get('transition_time_pickup', 0): #self.behavior['transition_time_pickup']:
            self.trip.move_to_dropoff(self.current_time_str, current_loc=self.current_loc)

    @state_handler(RidehailDriverTripStateMachine.driver_moving_to_dropoff.name)
    def _on_state_moving_to_dropoff(self, time_since_last_event):
        distance = hs.haversine(
            reversed(self.current_loc['coordinates'][:2]),
            reversed(self.get_trip()['dropoff_loc']['coordinates'][:2]),
            unit=hs.Unit.METERS,
        )
        if distance < 100:
            self.trip.wait_to_dropoff(self.current_time_str, current_loc=self.current_loc)

    @state_handler(RidehailDriverTripStateMachine.driver_droppedoff.name)
    def _on_state_droppedoff(self, time_since_last_event):
        if time_since_last_event >= self.behavior.get('transition_time_dropoff', 0): #self.behavior['transition_time_dropoff']:
            self.trip.end_trip(self.current_time_str, current_loc=self.current_loc)

            if self.behavior.get('action_when_free') == 'random_walk':
                self.active_route, self.projected_path, self.traversed_path = create_route(self.current_loc, self.behavior.get('empty_dest_loc'))
            elif self.behavior.get('action_when_free') == 'stay':
                self.active_route, self.projected_path, self.traversed_path = create_route(self.current_loc, None)

            self.create_new_unoccupied_trip(self.current_time_str, current_loc=self.current_loc, route=self.active_route)
