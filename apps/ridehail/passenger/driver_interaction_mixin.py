



from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any
from random import choice, randint, random

from apps.ridehail.statemachine import RidehailPassengerTripStateMachine
from apps.ridehail.statemachine import RideHailActions, RideHailEvents

from orsim.messenger.interaction import message_handler, state_handler
# # See interaction_map.py for explicit statemachine interaction definitions and visualization helpers.

# Example implementation for passenger
class DriverInteractionMixin():
    # ...existing code...

    @message_handler(RideHailActions.DRIVER_WORKFLOW_EVENT, RideHailEvents.DRIVER_CONFIRMED_TRIP)
    def _on_driver_confirmed_trip(self, payload, data):
        # self.trip.driver_confirmed_trip(self.current_time_str, self.current_loc, data.get('estimated_time_to_arrive', 0))

        self.trip.time_confirmed = datetime.strptime(self.current_time_str, "%a, %d %b %Y %H:%M:%S GMT")
        wait_time_driver_confirm = (self.trip.time_confirmed - self.trip.time_requested).total_seconds()
        self.trip.apply_trip_transition_and_notify(
            transition=RidehailPassengerTripStateMachine.driver_confirmed_trip.name,
            data={
                'sim_clock': self.current_time_str,
                'current_loc': self.current_loc,
                'stats.wait_time_driver_confirm': wait_time_driver_confirm,
                'stats.estimated_time_to_arrive': data.get('estimated_time_to_arrive', 0)
            },
            context={}
        )

    @message_handler(RideHailActions.DRIVER_WORKFLOW_EVENT, RideHailEvents.DRIVER_ARRIVED_FOR_PICKUP)
    def _on_driver_arrived_for_pickup(self, payload, data):
        if data.get('location') is None:
            return
        self.current_loc = data.get('location')
        # self.trip.driver_arrived_for_pickup(self.current_time_str, self.current_loc, data.get('driver_trip_id'))

        self.trip.time_pickedup = datetime.strptime(self.current_time_str, "%a, %d %b %Y %H:%M:%S GMT")
        wait_time_pickup = (self.trip.time_pickedup - self.trip.time_assigned).total_seconds()
        wait_time_total = (self.trip.time_pickedup - self.trip.time_requested).total_seconds()
        print(f"PassengerApp [{self.manager.get_id()}]: Driver arrived for pickup. Wait time from assigned: {wait_time_pickup} seconds. Wait time from request: {wait_time_total} seconds.")

        self.trip.apply_trip_transition_and_notify(
            transition=RidehailPassengerTripStateMachine.driver_arrived_for_pickup.name,
            data={
                'sim_clock': self.current_time_str,
                'current_loc': self.current_loc,
                'driver_trip_id': data.get('driver_trip_id'),
                'stats.wait_time_pickup': wait_time_pickup,
                'stats.wait_time_total': wait_time_total
            },
            context={}
        )

    @message_handler(RideHailActions.DRIVER_WORKFLOW_EVENT, RideHailEvents.DRIVER_MOVE_FOR_DROPOFF)
    def _on_driver_move_for_dropoff(self, payload, data):
        if data.get('location') is None:
            return
        self.current_loc = data.get('location')
        # self.trip.driver_move_for_dropoff(self.current_time_str, self.current_loc, route=data['planned_route'])
        self.trip.apply_trip_transition_and_notify(
            transition=RidehailPassengerTripStateMachine.driver_move_for_dropoff.name,
            data={
                'sim_clock': self.current_time_str,
                'current_loc': self.current_loc,
                'planned_route': data.get('planned_route'),
            },
            context={}
            )

    @message_handler(RideHailActions.DRIVER_WORKFLOW_EVENT, RideHailEvents.DRIVER_ARRIVED_FOR_DROPOFF)
    def _on_driver_arrived_for_dropoff(self, payload, data):
        if data.get('location') is None:
            return
        self.current_loc = data.get('location')
        # self.trip.driver_arrived_for_dropoff(self.current_time_str, self.current_loc)

        self.trip.time_droppedoff = datetime.strptime(self.current_time_str, "%a, %d %b %Y %H:%M:%S GMT")
        travel_time_total = (self.trip.time_droppedoff - self.trip.time_pickedup).total_seconds()
        self.trip.apply_trip_transition_and_notify(
            transition=RidehailPassengerTripStateMachine.driver_arrived_for_dropoff.name,
            data={
                'sim_clock': self.current_time_str,
                'current_loc': self.current_loc,
                'stats.travel_time_total': travel_time_total
            },
            context={}
            )

    @message_handler(RideHailActions.DRIVER_WORKFLOW_EVENT, RideHailEvents.DRIVER_WAITING_FOR_DROPOFF)
    def _on_driver_waiting_for_dropoff(self, payload, data):
        if data.get('location') is None:
            return
        self.current_loc = data.get('location')
        # self.trip.driver_waiting_for_dropoff(self.current_time_str, self.current_loc)
        self.trip.apply_trip_transition_and_notify(
            transition=RidehailPassengerTripStateMachine.driver_waiting_for_dropoff.name,
            data={
                'sim_clock': self.current_time_str,
                'current_loc': self.current_loc,
            },
            context={}
        )

    @message_handler(RideHailActions.DRIVER_WORKFLOW_EVENT, RideHailEvents.DRIVER_CANCELLED_TRIP)
    def _on_driver_cancelled_trip(self, payload, data):
        if data.get('location') is None:
            return
        self.current_loc = data.get('location', self.current_loc)
        # self.trip.driver_cancelled_trip(self.current_time_str, self.current_loc)
        self.trip.apply_trip_transition_and_notify(
            transition=RidehailPassengerTripStateMachine.driver_cancelled_trip.name,
            data={
                'sim_clock': self.current_time_str,
                'current_loc': self.current_loc,
            },
            context={}
        )


    @state_handler(RidehailPassengerTripStateMachine.passenger_received_trip_confirmation.name)
    def _on_state_received_trip_confirmation(self):
        print(f"PassengerApp [{self.manager.get_id()}]: Received Trip Confirmation. Current Trip State: {self.get_trip()['state']}")
        if random() <= self.get_transition_probability(('accept', self.get_trip()['state']), 1):
            # self.trip.accept(self.current_time_str, current_loc=self.current_loc)
            self.trip.apply_trip_transition_and_notify(
                transition=RidehailPassengerTripStateMachine.accept.name,
                data={
                    'sim_clock': self.current_time_str,
                    'current_loc': self.current_loc,
                },
                context={}
            )
            print(f"PassengerApp [{self.manager.get_id()}]: Trip Accepted.")
        else:
            # self.trip.reject(self.current_time_str, current_loc=self.current_loc)
            self.trip.apply_trip_transition_and_notify(
                transition=RidehailPassengerTripStateMachine.reject.name,
                data={
                    'sim_clock': self.current_time_str,
                    'current_loc': self.current_loc,
                },
                context={}
            )
            print(f"PassengerApp [{self.manager.get_id()}]: Trip Rejected.")

        print(f"PassengerApp [{self.manager.get_id()}]: Current Trip State after decision: {self.get_trip()['state']}")

    @state_handler(RidehailPassengerTripStateMachine.passenger_accepted_trip.name)
    def _on_state_accepted_trip(self):
        # self.trip.wait_for_pickup(self.current_time_str, current_loc=self.current_loc)
        self.trip.apply_trip_transition_and_notify(
            transition=RidehailPassengerTripStateMachine.wait_for_pickup.name,
            data={
                'sim_clock': self.current_time_str,
                'current_loc': self.current_loc,
            },
            context={}
        )

    @state_handler(RidehailPassengerTripStateMachine.passenger_droppedoff.name)
    def _on_state_droppedoff(self):
        # self.trip.end_trip(self.current_time_str, current_loc=self.current_loc)
        self.trip.apply_trip_transition_and_notify(
            transition=RidehailPassengerTripStateMachine.end_trip.name,
            data={
                'sim_clock': self.current_time_str,
                'current_loc': self.current_loc,
            },
            context={}
        )

