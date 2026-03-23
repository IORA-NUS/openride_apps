from apps.ride_hail.models import (
    AssignedPayload,
    DriverWorkflowPayload,
    PassengerWorkflowPayload,
    RequestedTripPayload,
)


def validate_requested_trip_payload(payload):
    return RequestedTripPayload.parse(payload) is not None


def validate_assigned_payload(payload):
    return AssignedPayload.parse(payload) is not None


def validate_passenger_workflow_payload(payload):
    return PassengerWorkflowPayload.parse(payload) is not None


def validate_driver_workflow_payload(payload):
    return DriverWorkflowPayload.parse(payload) is not None
