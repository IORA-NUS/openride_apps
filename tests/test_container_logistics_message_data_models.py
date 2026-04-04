from apps.container_logistics.message_data_models import (
    AssignedHaulTripPayload,
    FacilityWorkflowPayload,
    OrderWorkflowPayload,
)
from apps.container_logistics.statemachine import ContainerLogisticsActions


def test_assigned_haul_trip_payload_parse_valid_and_invalid():
    valid = {
        "action": ContainerLogisticsActions.ASSIGNED_HAUL_TRIP,
        "truck_id": "truck-1",
        "order": {"_id": "order-1"},
    }
    parsed = AssignedHaulTripPayload.parse(valid)
    assert parsed is not None
    assert parsed.truck_id == "truck-1"
    assert parsed.order["_id"] == "order-1"

    assert AssignedHaulTripPayload.parse({"action": ContainerLogisticsActions.ASSIGNED_HAUL_TRIP}) is None
    assert AssignedHaulTripPayload.parse({"action": "x", "order": {}}) is None


def test_order_workflow_payload_parse_valid_and_invalid():
    valid = {
        "action": ContainerLogisticsActions.ORDER_WORKFLOW_EVENT,
        "truck_id": "truck-1",
        "data": {"event": "order_cancelled"},
    }
    parsed = OrderWorkflowPayload.parse(valid)
    assert parsed is not None
    assert parsed.truck_id == "truck-1"
    assert parsed.data["event"] == "order_cancelled"

    assert OrderWorkflowPayload.parse({"action": ContainerLogisticsActions.ORDER_WORKFLOW_EVENT, "truck_id": "t"}) is None
    assert OrderWorkflowPayload.parse({"action": "x", "truck_id": "t", "data": {"event": "e"}}) is None


def test_facility_workflow_payload_parse_valid_and_invalid():
    valid = {
        "action": ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
        "truck_id": "truck-2",
        "data": {"event": "gate_slot_assigned_for_pickup", "gate_index": 0},
    }
    parsed = FacilityWorkflowPayload.parse(valid)
    assert parsed is not None
    assert parsed.truck_id == "truck-2"
    assert parsed.data["gate_index"] == 0

    assert FacilityWorkflowPayload.parse({"action": ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT, "data": {"event": "e"}}) is None
    assert FacilityWorkflowPayload.parse({"action": "x", "truck_id": "t", "data": {"event": "e"}}) is None
