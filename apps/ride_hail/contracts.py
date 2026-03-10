from apps.ride_hail.events import RideHailActions


def validate_requested_trip_payload(payload):
    return (
        isinstance(payload, dict)
        and payload.get("action") == RideHailActions.REQUESTED_TRIP
        and payload.get("passenger_id") is not None
        and payload.get("requested_trip") is not None
    )


def validate_assigned_payload(payload):
    return (
        isinstance(payload, dict)
        and payload.get("action") == RideHailActions.ASSIGNED
        and payload.get("driver_id") is not None
    )


def validate_passenger_workflow_payload(payload):
    data = payload.get("data") if isinstance(payload, dict) else None
    return (
        isinstance(payload, dict)
        and payload.get("action") == RideHailActions.PASSENGER_WORKFLOW_EVENT
        and payload.get("passenger_id") is not None
        and isinstance(data, dict)
        and data.get("event") is not None
    )


def validate_driver_workflow_payload(payload):
    data = payload.get("data") if isinstance(payload, dict) else None
    return (
        isinstance(payload, dict)
        and payload.get("action") == RideHailActions.DRIVER_WORKFLOW_EVENT
        and payload.get("driver_id") is not None
        and isinstance(data, dict)
        and data.get("event") is not None
    )
