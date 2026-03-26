from .events import RideHailEvents, RideHailActions
from .ridehail_driver_trip_sm import RidehailDriverTripStateMachine
from .ridehail_passenger_trip_sm import RidehailPassengerTripStateMachine

# Each tuple: (source_statemachine, source_state, event, target_statemachine, target_state, description)
driver_passenger_interactions = [
    # Passenger confirms or rejects trip
    {
        "source_statemachine": RidehailPassengerTripStateMachine.__name__,
        "source_state": RidehailPassengerTripStateMachine.passenger_requested_trip.name,
        "action": RideHailActions.PASSENGER_WORKFLOW_EVENT,
        "event": RideHailEvents.PASSENGER_CONFIRMED_TRIP,
        "target_statemachine": RidehailDriverTripStateMachine.__name__,
        "target_state": RidehailDriverTripStateMachine.driver_moving_to_pickup.name,
        "description": "Passenger confirms trip, driver starts moving to pickup"
    },
    {
        "source_statemachine": RidehailPassengerTripStateMachine.__name__,
        "source_state": RidehailPassengerTripStateMachine.passenger_requested_trip.name,
        "action": RideHailActions.PASSENGER_WORKFLOW_EVENT,
        "event": RideHailEvents.PASSENGER_REJECTED_TRIP,
        "target_statemachine": RidehailDriverTripStateMachine.__name__,
        "target_state": RidehailDriverTripStateMachine.driver_looking_for_job.name,
        "description": "Passenger rejects trip, driver resumes looking for job"
    },
    # Passenger acknowledges pickup
    {
        "source_statemachine": RidehailPassengerTripStateMachine.__name__,
        "source_state": RidehailPassengerTripStateMachine.passenger_waiting_for_pickup.name,
        "action": RideHailActions.PASSENGER_WORKFLOW_EVENT,
        "event": RideHailEvents.PASSENGER_ACKNOWLEDGE_PICKUP,
        "target_statemachine": RidehailDriverTripStateMachine.__name__,
        "target_state": RidehailDriverTripStateMachine.driver_moving_to_dropoff.name,
        "description": "Passenger acknowledges pickup, driver moves to dropoff"
    },
    # Passenger acknowledges dropoff
    {
        "source_statemachine": RidehailPassengerTripStateMachine.__name__,
        "source_state": RidehailPassengerTripStateMachine.passenger_droppedoff.name,
        "action": RideHailActions.PASSENGER_WORKFLOW_EVENT,
        "event": RideHailEvents.PASSENGER_ACKNOWLEDGE_DROPOFF,
        "target_statemachine": RidehailDriverTripStateMachine.__name__,
        "target_state": RidehailDriverTripStateMachine.driver_droppedoff.name,
        "description": "Passenger acknowledges dropoff, driver ends trip"
    },

    # Driver confirms trip
    {
        "source_statemachine": RidehailDriverTripStateMachine.__name__,
        "source_state": RidehailDriverTripStateMachine.driver_received_trip.name,
        "action": RideHailActions.DRIVER_WORKFLOW_EVENT,
        "event": RideHailEvents.DRIVER_CONFIRMED_TRIP,
        "target_statemachine": RidehailPassengerTripStateMachine.__name__,
        "target_state": RidehailPassengerTripStateMachine.passenger_received_trip_confirmation.name,
        "description": "Driver confirms trip, passenger receives confirmation"
    },
    # Driver arrives for pickup
    {
        "source_statemachine": RidehailDriverTripStateMachine.__name__,
        "source_state": RidehailDriverTripStateMachine.driver_moving_to_pickup.name,
        "action": RideHailActions.DRIVER_WORKFLOW_EVENT,
        "event": RideHailEvents.DRIVER_ARRIVED_FOR_PICKUP,
        "target_statemachine": RidehailPassengerTripStateMachine.__name__,
        "target_state": RidehailPassengerTripStateMachine.passenger_waiting_for_pickup.name,
        "description": "Driver arrives, passenger waits for pickup"
    },
    # Driver starts dropoff leg
    {
        "source_statemachine": RidehailDriverTripStateMachine.__name__,
        "source_state": RidehailDriverTripStateMachine.driver_pickedup.name,
        "action": RideHailActions.DRIVER_WORKFLOW_EVENT,
        "event": RideHailEvents.DRIVER_MOVE_FOR_DROPOFF,
        "target_statemachine": RidehailPassengerTripStateMachine.__name__,
        "target_state": RidehailPassengerTripStateMachine.passenger_moving_for_dropoff.name,
        "description": "Driver starts dropoff leg, passenger moves for dropoff"
    },
    # Driver arrives at dropoff
    {
        "source_statemachine": RidehailDriverTripStateMachine.__name__,
        "source_state": RidehailDriverTripStateMachine.driver_moving_to_dropoff.name,
        "action": RideHailActions.DRIVER_WORKFLOW_EVENT,
        "event": RideHailEvents.DRIVER_ARRIVED_FOR_DROPOFF,
        "target_statemachine": RidehailPassengerTripStateMachine.__name__,
        "target_state": RidehailPassengerTripStateMachine.passenger_waiting_for_dropoff.name,
        "description": "Driver arrives at dropoff, passenger waits for dropoff"
    },
    # Driver signals waiting at dropoff
    {
        "source_statemachine": RidehailDriverTripStateMachine.__name__,
        "source_state": RidehailDriverTripStateMachine.driver_moving_to_dropoff.name,
        "action": RideHailActions.DRIVER_WORKFLOW_EVENT,
        "event": RideHailEvents.DRIVER_WAITING_FOR_DROPOFF,
        "target_statemachine": RidehailPassengerTripStateMachine.__name__,
        "target_state": RidehailPassengerTripStateMachine.passenger_droppedoff.name,
        "description": "Driver signals waiting at dropoff, passenger dropped off"
    },
    # Driver cancels trip (can occur from multiple driver states)
    {
        "source_statemachine": RidehailDriverTripStateMachine.__name__,
        "source_state": RidehailDriverTripStateMachine.driver_received_trip.name,
        "action": RideHailActions.DRIVER_WORKFLOW_EVENT,
        "event": RideHailEvents.DRIVER_CANCELLED_TRIP,
        "target_statemachine": RidehailPassengerTripStateMachine.__name__,
        "target_state": RidehailPassengerTripStateMachine.passenger_cancelled_trip.name,
        "description": "Driver cancels trip, passenger cancelled"
    },
    {
        "source_statemachine": RidehailDriverTripStateMachine.__name__,
        "source_state": RidehailDriverTripStateMachine.driver_accepted_trip.name,
        "action": RideHailActions.DRIVER_WORKFLOW_EVENT,
        "event": RideHailEvents.DRIVER_CANCELLED_TRIP,
        "target_statemachine": RidehailPassengerTripStateMachine.__name__,
        "target_state": RidehailPassengerTripStateMachine.passenger_cancelled_trip.name,
        "description": "Driver cancels trip, passenger cancelled"
    },
    {
        "source_statemachine": RidehailDriverTripStateMachine.__name__,
        "source_state": RidehailDriverTripStateMachine.driver_moving_to_pickup.name,
        "action": RideHailActions.DRIVER_WORKFLOW_EVENT,
        "event": RideHailEvents.DRIVER_CANCELLED_TRIP,
        "target_statemachine": RidehailPassengerTripStateMachine.__name__,
        "target_state": RidehailPassengerTripStateMachine.passenger_cancelled_trip.name,
        "description": "Driver cancels trip, passenger cancelled"
    },
    {
        "source_statemachine": RidehailDriverTripStateMachine.__name__,
        "source_state": RidehailDriverTripStateMachine.driver_moving_to_dropoff.name,
        "action": RideHailActions.DRIVER_WORKFLOW_EVENT,
        "event": RideHailEvents.DRIVER_CANCELLED_TRIP,
        "target_statemachine": RidehailPassengerTripStateMachine.__name__,
        "target_state": RidehailPassengerTripStateMachine.passenger_cancelled_trip.name,
        "description": "Driver cancels trip, passenger cancelled"
    },
]
