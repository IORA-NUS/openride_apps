from apps.container_logistics.events import ContainerLogisticsActions


def validate_haulier_workflow_payload(payload):
    data = payload.get("data") if isinstance(payload, dict) else None
    return (
        isinstance(payload, dict)
        and payload.get("action") == ContainerLogisticsActions.HAULIER_WORKFLOW_EVENT
        and payload.get("haulier_id") is not None
        and isinstance(data, dict)
        and data.get("event") is not None
    )


def validate_facility_workflow_payload(payload):
    data = payload.get("data") if isinstance(payload, dict) else None
    return (
        isinstance(payload, dict)
        and payload.get("action") == ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT
        and payload.get("facility_id") is not None
        and isinstance(data, dict)
        and data.get("event") is not None
    )
