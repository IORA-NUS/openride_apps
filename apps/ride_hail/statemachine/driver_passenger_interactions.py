from .events import RideHailEvents, RideHailActions
from .ridehail_driver_trip_sm import RidehailDriverTripStateMachine
from .ridehail_passenger_trip_sm import RidehailPassengerTripStateMachine

# Each tuple: (source_statemachine, source_state, event, target_statemachine, target_state, description)
driver_passenger_interactions = [
    # Passenger accepts trip
    {
        "source_statemachine": RidehailPassengerTripStateMachine.__name__,
        "source_transition": RidehailPassengerTripStateMachine.accept.name,
        "source_new_state": RidehailPassengerTripStateMachine.passenger_accepted_trip.name,
        "action": RideHailActions.PASSENGER_WORKFLOW_EVENT,
        "event": RideHailEvents.PASSENGER_CONFIRMED_TRIP,
        "target_statemachine": RidehailDriverTripStateMachine.__name__,
        "target_new_state": RidehailDriverTripStateMachine.driver_moving_to_pickup.name,
        "description": "Passenger confirms trip, driver starts moving to pickup"
    },
    # Passenger cancels dropoff
    {
        "source_statemachine": RidehailPassengerTripStateMachine.__name__,
        "source_transition": RidehailPassengerTripStateMachine.cancel.name,
        "source_new_state": RidehailPassengerTripStateMachine.passenger_cancelled_trip.name,
        "action": RideHailActions.PASSENGER_WORKFLOW_EVENT,
        "event": RideHailEvents.PASSENGER_CANCEL_TRIP,
        "target_statemachine": RidehailDriverTripStateMachine.__name__,
        "target_new_state": RidehailDriverTripStateMachine.driver_completed_trip.name,
        "description": "Passenger cancels trip, driver ends trip"
    },
    # Passenger rejects trip
    {
        "source_statemachine": RidehailPassengerTripStateMachine.__name__,
        "source_transition": RidehailPassengerTripStateMachine.reject.name,
        "source_new_state": RidehailPassengerTripStateMachine.passenger_requested_trip.name,
        "action": RideHailActions.PASSENGER_WORKFLOW_EVENT,
        "event": RideHailEvents.PASSENGER_REJECTED_TRIP,
        "target_statemachine": RidehailDriverTripStateMachine.__name__,
        "target_new_state": RidehailDriverTripStateMachine.driver_looking_for_job.name,
        "description": "Passenger rejects trip, driver resumes looking for job"
    },
    # Passenger acknowledges pickup
    {
        "source_statemachine": RidehailPassengerTripStateMachine.__name__,
        "source_transition": RidehailPassengerTripStateMachine.driver_arrived_for_pickup.name,
        "source_new_state": RidehailPassengerTripStateMachine.passenger_pickedup.name,
        "action": RideHailActions.PASSENGER_WORKFLOW_EVENT,
        "event": RideHailEvents.PASSENGER_ACKNOWLEDGE_PICKUP,
        "target_statemachine": RidehailDriverTripStateMachine.__name__,
        "target_new_state": RidehailDriverTripStateMachine.driver_moving_to_dropoff.name,
        "description": "Passenger acknowledges pickup, driver moves to dropoff"
    },
    # Passenger acknowledges dropoff
    {
        "source_statemachine": RidehailPassengerTripStateMachine.__name__,
        "source_transition": RidehailPassengerTripStateMachine.driver_waiting_for_dropoff.name,
        "source_new_state": RidehailPassengerTripStateMachine.passenger_droppedoff.name,
        "action": RideHailActions.PASSENGER_WORKFLOW_EVENT,
        "event": RideHailEvents.PASSENGER_ACKNOWLEDGE_DROPOFF,
        "target_statemachine": RidehailDriverTripStateMachine.__name__,
        "target_new_state": RidehailDriverTripStateMachine.driver_droppedoff.name,
        "description": "Passenger acknowledges dropoff, driver ends trip"
    },





    # # Driver confirms trip This is a special case Since the assignment comes from the assignment model. Need to think about this design
    # {
    #     "source_statemachine": RidehailDriverTripStateMachine.__name__,
    #     "source_transition": RidehailDriverTripStateMachine.recieve.name,
    #     "source_new_state": RidehailDriverTripStateMachine.driver_received_trip.name,
    #     "action": RideHailActions.DRIVER_WORKFLOW_EVENT,
    #     "event": RideHailActions.ASSIGNED,
    #     "target_statemachine": RidehailPassengerTripStateMachine.__name__,
    #     "target_new_state": RidehailPassengerTripStateMachine.passenger_received_trip_confirmation.name,
    #     "description": "Driver confirms trip, passenger receives confirmation"
    # },
    # Driver confirms trip
    {
        "source_statemachine": RidehailDriverTripStateMachine.__name__,
        "source_transition": RidehailDriverTripStateMachine.confirm.name,
        "source_new_state": RidehailDriverTripStateMachine.driver_accepted_trip.name,
        "action": RideHailActions.DRIVER_WORKFLOW_EVENT,
        "event": RideHailEvents.DRIVER_CONFIRMED_TRIP,
        "target_statemachine": RidehailPassengerTripStateMachine.__name__,
        "target_new_state": RidehailPassengerTripStateMachine.passenger_received_trip_confirmation.name,
        "description": "Driver confirms trip, passenger receives confirmation"
    },
    # Driver arrives for pickup
    {
        "source_statemachine": RidehailDriverTripStateMachine.__name__,
        "source_transition": RidehailDriverTripStateMachine.wait_to_pickup.name,
        "source_new_state": RidehailDriverTripStateMachine.driver_waiting_to_pickup.name,
        "action": RideHailActions.DRIVER_WORKFLOW_EVENT,
        "event": RideHailEvents.DRIVER_ARRIVED_FOR_PICKUP,
        "target_statemachine": RidehailPassengerTripStateMachine.__name__,
        "target_new_state": RidehailPassengerTripStateMachine.passenger_waiting_for_pickup.name,
        "description": "Driver arrives, passenger waits for pickup"
    },
    # Driver starts dropoff leg
    {
        "source_statemachine": RidehailDriverTripStateMachine.__name__,
        "source_transition": RidehailDriverTripStateMachine.move_to_dropoff.name,
        "source_new_state": RidehailDriverTripStateMachine.driver_moving_to_dropoff.name,
        "action": RideHailActions.DRIVER_WORKFLOW_EVENT,
        "event": RideHailEvents.DRIVER_MOVE_FOR_DROPOFF,
        "target_statemachine": RidehailPassengerTripStateMachine.__name__,
        "target_new_state": RidehailPassengerTripStateMachine.passenger_moving_for_dropoff.name,
        "description": "Driver starts dropoff leg, passenger moves for dropoff"
    },
    # Driver arrives at dropoff
    {
        "source_statemachine": RidehailDriverTripStateMachine.__name__,
        "source_transition": RidehailDriverTripStateMachine.wait_to_dropoff.name,
        "source_new_state": RidehailDriverTripStateMachine.driver_waiting_to_dropoff.name,
        "action": RideHailActions.DRIVER_WORKFLOW_EVENT,
        "event": RideHailEvents.DRIVER_WAITING_FOR_DROPOFF, #RideHailEvents.DRIVER_ARRIVED_FOR_DROPOFF,
        "target_statemachine": RidehailPassengerTripStateMachine.__name__,
        "target_new_state": RidehailPassengerTripStateMachine.passenger_waiting_for_dropoff.name,
        "description": "Driver arrives at dropoff, passenger waits for dropoff"
    },
    # # Driver signals waiting at dropoff
    # {
    #     "source_statemachine": RidehailDriverTripStateMachine.__name__,
    #     "source_transition": RidehailDriverTripStateMachine.passenger_acknowledge_dropoff.name,
    #     "source_new_state": RidehailDriverTripStateMachine.driver_waiting_to_dropoff.name,
    #     "action": RideHailActions.DRIVER_WORKFLOW_EVENT,
    #     "event": RideHailEvents.DRIVER_WAITING_FOR_DROPOFF,
    #     "target_statemachine": RidehailPassengerTripStateMachine.__name__,
    #     "target_new_state": RidehailPassengerTripStateMachine.passenger_droppedoff.name,
    #     "description": "Driver signals waiting at dropoff, passenger dropped off"
    # },
    # Driver cancels trip (can occur from multiple driver states)
    {
        "source_statemachine": RidehailDriverTripStateMachine.__name__,
        "source_transition": RidehailDriverTripStateMachine.cancel.name,
        "source_new_state": RidehailDriverTripStateMachine.driver_cancelled_trip.name,
        "action": RideHailActions.DRIVER_WORKFLOW_EVENT,
        "event": RideHailEvents.DRIVER_CANCELLED_TRIP,
        "target_statemachine": RidehailPassengerTripStateMachine.__name__,
        "target_new_state": RidehailPassengerTripStateMachine.passenger_cancelled_trip.name,
        "description": "Driver cancels trip, passenger cancelled"
    },
]
