from .events import RideHailEvents, RideHailActions

# Each tuple: (source_statemachine, source_state, event, target_statemachine, target_state, description)
driver_passenger_interactions = [
    # Passenger confirms or rejects trip
    {
        "source_statemachine": "RidehailPassengerTripStateMachine",
        "source_state": "passenger_requested_trip",
        "event": RideHailEvents.PASSENGER_CONFIRMED_TRIP,
        "target_statemachine": "RidehailDriverTripStateMachine",
        "target_state": "driver_moving_to_pickup",
        "description": "Passenger confirms trip, driver starts moving to pickup"
    },
    {
        "source_statemachine": "RidehailPassengerTripStateMachine",
        "source_state": "passenger_requested_trip",
        "event": RideHailEvents.PASSENGER_REJECTED_TRIP,
        "target_statemachine": "RidehailDriverTripStateMachine",
        "target_state": "driver_looking_for_job",
        "description": "Passenger rejects trip, driver resumes looking for job"
    },
    # Passenger acknowledges pickup
    {
        "source_statemachine": "RidehailPassengerTripStateMachine",
        "source_state": "passenger_waiting_for_pickup",
        "event": RideHailEvents.PASSENGER_ACKNOWLEDGE_PICKUP,
        "target_statemachine": "RidehailDriverTripStateMachine",
        "target_state": "driver_moving_to_dropoff",
        "description": "Passenger acknowledges pickup, driver moves to dropoff"
    },
    # Passenger acknowledges dropoff
    {
        "source_statemachine": "RidehailPassengerTripStateMachine",
        "source_state": "passenger_droppedoff",
        "event": RideHailEvents.PASSENGER_ACKNOWLEDGE_DROPOFF,
        "target_statemachine": "RidehailDriverTripStateMachine",
        "target_state": "driver_droppedoff",
        "description": "Passenger acknowledges dropoff, driver ends trip"
    },

    # Driver confirms trip
    {
        "source_statemachine": "RidehailDriverTripStateMachine",
        "source_state": "driver_received_trip",
        "event": RideHailEvents.DRIVER_CONFIRMED_TRIP,
        "target_statemachine": "RidehailPassengerTripStateMachine",
        "target_state": "passenger_received_trip_confirmation",
        "description": "Driver confirms trip, passenger receives confirmation"
    },
    # Driver arrives for pickup
    {
        "source_statemachine": "RidehailDriverTripStateMachine",
        "source_state": "driver_moving_to_pickup",
        "event": RideHailEvents.DRIVER_ARRIVED_FOR_PICKUP,
        "target_statemachine": "RidehailPassengerTripStateMachine",
        "target_state": "passenger_waiting_for_pickup",
        "description": "Driver arrives, passenger waits for pickup"
    },
    # Driver starts dropoff leg
    {
        "source_statemachine": "RidehailDriverTripStateMachine",
        "source_state": "driver_pickedup",
        "event": RideHailEvents.DRIVER_MOVE_FOR_DROPOFF,
        "target_statemachine": "RidehailPassengerTripStateMachine",
        "target_state": "passenger_moving_for_dropoff",
        "description": "Driver starts dropoff leg, passenger moves for dropoff"
    },
    # Driver arrives at dropoff
    {
        "source_statemachine": "RidehailDriverTripStateMachine",
        "source_state": "driver_moving_to_dropoff",
        "event": RideHailEvents.DRIVER_ARRIVED_FOR_DROPOFF,
        "target_statemachine": "RidehailPassengerTripStateMachine",
        "target_state": "passenger_waiting_for_dropoff",
        "description": "Driver arrives at dropoff, passenger waits for dropoff"
    },
    # Driver signals waiting at dropoff
    {
        "source_statemachine": "RidehailDriverTripStateMachine",
        "source_state": "driver_moving_to_dropoff",
        "event": RideHailEvents.DRIVER_WAITING_FOR_DROPOFF,
        "target_statemachine": "RidehailPassengerTripStateMachine",
        "target_state": "passenger_droppedoff",
        "description": "Driver signals waiting at dropoff, passenger dropped off"
    },
    # Driver cancels trip (can occur from multiple driver states)
    {
        "source_statemachine": "RidehailDriverTripStateMachine",
        "source_state": "driver_received_trip",
        "event": RideHailEvents.DRIVER_CANCELLED_TRIP,
        "target_statemachine": "RidehailPassengerTripStateMachine",
        "target_state": "passenger_cancelled_trip",
        "description": "Driver cancels trip, passenger cancelled"
    },
    {
        "source_statemachine": "RidehailDriverTripStateMachine",
        "source_state": "driver_accepted_trip",
        "event": RideHailEvents.DRIVER_CANCELLED_TRIP,
        "target_statemachine": "RidehailPassengerTripStateMachine",
        "target_state": "passenger_cancelled_trip",
        "description": "Driver cancels trip, passenger cancelled"
    },
    {
        "source_statemachine": "RidehailDriverTripStateMachine",
        "source_state": "driver_moving_to_pickup",
        "event": RideHailEvents.DRIVER_CANCELLED_TRIP,
        "target_statemachine": "RidehailPassengerTripStateMachine",
        "target_state": "passenger_cancelled_trip",
        "description": "Driver cancels trip, passenger cancelled"
    },
    {
        "source_statemachine": "RidehailDriverTripStateMachine",
        "source_state": "driver_moving_to_dropoff",
        "event": RideHailEvents.DRIVER_CANCELLED_TRIP,
        "target_statemachine": "RidehailPassengerTripStateMachine",
        "target_state": "passenger_cancelled_trip",
        "description": "Driver cancels trip, passenger cancelled"
    },
]
