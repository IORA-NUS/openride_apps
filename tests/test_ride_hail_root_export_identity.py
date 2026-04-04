import apps.ridehail as ridehail
import pytest
from apps.ridehail.analytics import AnalyticsAgentIndie, AnalyticsApp
from apps.ridehail.adapters import (
    RideHailAnalyticsAdapter,
    RideHailAssignmentAdapter,
    RideHailDriverAdapter,
    RideHailPassengerAdapter,
)
from apps.ridehail.assignment import AssignmentAgentIndie, AssignmentApp
from apps.ridehail.contracts import (
    validate_assigned_payload,
    validate_driver_workflow_payload,
    validate_passenger_workflow_payload,
    validate_requested_trip_payload,
)
from apps.ridehail.driver import (
    DriverAgentIndie,
    DriverApp,
    DriverManager,
    DriverTripManager,
)
from openride_apps.apps.ridehail.statemachine.events import RideHailActions, RideHailEvents
from apps.ridehail.models import (
    AssignedPayload,
    DriverWorkflowPayload,
    PassengerWorkflowPayload,
    RequestedTripPayload,
)
from apps.ridehail.passenger import (
    PassengerAgentIndie,
    PassengerApp,
    PassengerManager,
    PassengerTripManager,
)


def test_ride_hail_root_all_symbols_resolve():
    for name in ridehail.__all__:
        assert getattr(ridehail, name) is not None


def test_ride_hail_root_reexports_identity_for_contract_symbols():
    assert ridehail.RideHailActions is RideHailActions
    assert ridehail.RideHailEvents is RideHailEvents

    assert ridehail.validate_requested_trip_payload is validate_requested_trip_payload
    assert ridehail.validate_assigned_payload is validate_assigned_payload
    assert ridehail.validate_passenger_workflow_payload is validate_passenger_workflow_payload
    assert ridehail.validate_driver_workflow_payload is validate_driver_workflow_payload

    assert ridehail.RequestedTripPayload is RequestedTripPayload
    assert ridehail.AssignedPayload is AssignedPayload
    assert ridehail.PassengerWorkflowPayload is PassengerWorkflowPayload
    assert ridehail.DriverWorkflowPayload is DriverWorkflowPayload


def test_ride_hail_root_reexports_identity_for_adapter_symbols():
    assert ridehail.RideHailAssignmentAdapter is RideHailAssignmentAdapter
    assert ridehail.RideHailAnalyticsAdapter is RideHailAnalyticsAdapter
    assert ridehail.RideHailDriverAdapter is RideHailDriverAdapter
    assert ridehail.RideHailPassengerAdapter is RideHailPassengerAdapter


def test_ride_hail_root_reexports_identity_for_role_class_symbols():
    assert ridehail.DriverApp is DriverApp
    assert ridehail.DriverAgentIndie is DriverAgentIndie
    assert ridehail.DriverManager is DriverManager
    assert ridehail.DriverTripManager is DriverTripManager

    assert ridehail.PassengerApp is PassengerApp
    assert ridehail.PassengerAgentIndie is PassengerAgentIndie
    assert ridehail.PassengerManager is PassengerManager
    assert ridehail.PassengerTripManager is PassengerTripManager

    assert ridehail.AssignmentApp is AssignmentApp
    assert ridehail.AssignmentAgentIndie is AssignmentAgentIndie

    assert ridehail.AnalyticsApp is AnalyticsApp
    assert ridehail.AnalyticsAgentIndie is AnalyticsAgentIndie


def test_ride_hail_root_unknown_attribute_raises_attribute_error():
    with pytest.raises(AttributeError):
        getattr(ridehail, "DoesNotExist")
