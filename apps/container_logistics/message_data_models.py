from dataclasses import dataclass
from typing import Any, Dict, Optional

from apps.container_logistics.statemachine import ContainerLogisticsActions


@dataclass
class AssignedHaulTripPayload:
    action: str
    order: Dict[str, Any]
    truck_id: Optional[str] = None

    @classmethod
    def parse(cls, payload: Any) -> Optional["AssignedHaulTripPayload"]:
        if not isinstance(payload, dict):
            return None
        if payload.get("action") != ContainerLogisticsActions.ASSIGNED_HAUL_TRIP:
            return None
        order = payload.get("order")
        if not isinstance(order, dict):
            return None
        return cls(payload["action"], order, payload.get("truck_id"))


@dataclass
class OrderWorkflowPayload:
    action: str
    truck_id: str
    data: Dict[str, Any]

    @classmethod
    def parse(cls, payload: Any) -> Optional["OrderWorkflowPayload"]:
        if not isinstance(payload, dict):
            return None
        if payload.get("action") != ContainerLogisticsActions.ORDER_WORKFLOW_EVENT:
            return None
        truck_id = payload.get("truck_id")
        data = payload.get("data")
        if truck_id is None or not isinstance(data, dict) or data.get("event") is None:
            return None
        return cls(payload["action"], truck_id, data)


@dataclass
class FacilityWorkflowPayload:
    action: str
    truck_id: str
    data: Dict[str, Any]

    @classmethod
    def parse(cls, payload: Any) -> Optional["FacilityWorkflowPayload"]:
        if not isinstance(payload, dict):
            return None
        if payload.get("action") != ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT:
            return None
        truck_id = payload.get("truck_id")
        data = payload.get("data")
        if truck_id is None or not isinstance(data, dict) or data.get("event") is None:
            return None
        return cls(payload["action"], truck_id, data)
