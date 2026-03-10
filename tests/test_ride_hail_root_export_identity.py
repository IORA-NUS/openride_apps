import apps.ride_hail as ride_hail
from apps.ride_hail.adapters import (
    RideHailAnalyticsAdapter,
    RideHailAssignmentAdapter,
    RideHailDriverAdapter,
    RideHailPassengerAdapter,
)
from apps.ride_hail.contracts import (
    validate_assigned_payload,
    validate_driver_workflow_payload,
    validate_passenger_workflow_payload,
    validate_requested_trip_payload,
)
from apps.ride_hail.events import RideHailActions, RideHailEvents
from apps.ride_hail.models import (
    AssignedPayload,
    DriverWorkflowPayload,
    PassengerWorkflowPayload,
    RequestedTripPayload,
)


def test_ride_hail_root_all_symbols_resolve():
    for name in ride_hail.__all__:
        assert getattr(ride_hail, name) is not None


def test_ride_hail_root_reexports_identity_for_contract_symbols():
    assert ride_hail.RideHailActions is RideHailActions
    assert ride_hail.RideHailEvents is RideHailEvents

    assert ride_hail.validate_requested_trip_payload is validate_requested_trip_payload
    assert ride_hail.validate_assigned_payload is validate_assigned_payload
    assert ride_hail.validate_passenger_workflow_payload is validate_passenger_workflow_payload
    assert ride_hail.validate_driver_workflow_payload is validate_driver_workflow_payload

    assert ride_hail.RequestedTripPayload is RequestedTripPayload
    assert ride_hail.AssignedPayload is AssignedPayload
    assert ride_hail.PassengerWorkflowPayload is PassengerWorkflowPayload
    assert ride_hail.DriverWorkflowPayload is DriverWorkflowPayload


def test_ride_hail_root_reexports_identity_for_adapter_symbols():
    assert ride_hail.RideHailAssignmentAdapter is RideHailAssignmentAdapter
    assert ride_hail.RideHailAnalyticsAdapter is RideHailAnalyticsAdapter
    assert ride_hail.RideHailDriverAdapter is RideHailDriverAdapter
    assert ride_hail.RideHailPassengerAdapter is RideHailPassengerAdapter
