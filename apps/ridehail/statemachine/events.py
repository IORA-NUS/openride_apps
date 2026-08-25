class RideHailActions:
    REQUESTED_TRIP = "requested_trip"
    ASSIGNED = "assigned"
    PASSENGER_WORKFLOW_EVENT = "passenger_workflow_event"
    DRIVER_WORKFLOW_EVENT = "driver_workflow_event"


class RideHailEvents:
    PASSENGER_CONFIRMED_TRIP = "passenger_confirmed_trip"
    PASSENGER_REJECTED_TRIP = "passenger_rejected_trip"
    PASSENGER_CANCEL_TRIP = "passenger_cancel_trip"
    PASSENGER_ACKNOWLEDGE_PICKUP = "passenger_acknowledge_pickup"
    PASSENGER_ACKNOWLEDGE_DROPOFF = "passenger_acknowledge_dropoff"

    DRIVER_CONFIRMED_TRIP = "driver_confirmed_trip"
    DRIVER_CANCELLED_TRIP = "driver_cancelled_trip"
    DRIVER_ARRIVED_FOR_PICKUP = "driver_arrived_for_pickup"
    DRIVER_MOVE_FOR_DROPOFF = "driver_move_for_dropoff"
    DRIVER_WAITING_FOR_DROPOFF = "driver_waiting_for_dropoff"
    DRIVER_ARRIVED_FOR_DROPOFF = "driver_arrived_for_dropoff"
