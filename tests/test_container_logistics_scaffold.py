from apps.container_logistics_old import (
    ContainerLogisticsActions,
    ContainerLogisticsEvents,
    FacilityInteractionAdapter,
    HaulierInteractionAdapter,
    validate_facility_workflow_payload,
    validate_haulier_workflow_payload,
)


def test_container_logistics_validators():
    assert validate_haulier_workflow_payload(
        {
            "action": ContainerLogisticsActions.HAULIER_WORKFLOW_EVENT,
            "haulier_id": "h1",
            "data": {"event": ContainerLogisticsEvents.ARRIVE_FOR_PICKUP},
        }
    )
    assert validate_facility_workflow_payload(
        {
            "action": ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
            "facility_id": "f1",
            "data": {"event": ContainerLogisticsEvents.FACILITY_ACKNOWLEDGES_PICKUP_CHECKIN},
        }
    )

    assert validate_haulier_workflow_payload({"action": ContainerLogisticsActions.HAULIER_WORKFLOW_EVENT}) is False
    assert validate_facility_workflow_payload({"action": ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT}) is False


def test_haulier_adapter_dispatches_valid_facility_event():
    adapter = HaulierInteractionAdapter()
    seen = []

    def _on_facility_event(payload, data):
        seen.append((payload.get("facility_id"), data.get("event")))

    adapter.register_message(
        ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
        ContainerLogisticsEvents.FACILITY_RELEASES_CONTAINER,
        _on_facility_event,
    )

    payload = {
        "action": ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
        "facility_id": "f1",
        "data": {"event": ContainerLogisticsEvents.FACILITY_RELEASES_CONTAINER},
    }

    assert adapter.handle_message(payload) is True
    assert seen == [("f1", ContainerLogisticsEvents.FACILITY_RELEASES_CONTAINER)]


def test_facility_adapter_ignores_invalid_haulier_event_payload():
    adapter = FacilityInteractionAdapter()
    payload = {
        "action": ContainerLogisticsActions.HAULIER_WORKFLOW_EVENT,
        "haulier_id": "h1",
        "data": {},
    }

    assert adapter.handle_message(payload) is False
