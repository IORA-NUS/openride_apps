from dataclasses import dataclass
from typing import Any, Dict, Optional

from apps.container_logistics.statemachine import ContainerLogisticsActions


@dataclass
class AssignedHaulTripPayload:
    action: str
    order: Dict[str, Any]
    truck_id: Optional[str] = None
    # Collaboration (haulier job sharing): set when the carrying truck's haulier
    # differs from the order's issuing haulier. ``benefit_km`` = SIGNED deadhead
    # saved vs the owner's best own-fleet truck still free after the solve (a full
    # partition scan, not the capped candidate list). Negative = the partner truck
    # was worse than a free own truck (possible under cost-blind solvers or
    # cap-limited greedy). None = the owner had no free feasible truck this tick.
    shared: bool = False
    owner_haulier_id: Optional[str] = None
    carrier_haulier_id: Optional[str] = None
    benefit_km: Optional[float] = None
    # Shared-pool cooperation audit trail (plan §6.13). APPENDED after benefit_km on
    # purpose — dataclass field order matters, ``publish()`` passes keywords and
    # ``parse()`` uses ``payload.get``, so OLD payloads without these keys still
    # parse and old consumers still work. ``None`` everywhere on the legacy
    # ``partitioned`` path.
    pool_id: Optional[str] = None
    awarded_cost_km: Optional[float] = None
    owner_reserve_km: Optional[float] = None
    market_round: Optional[int] = None

    @classmethod
    def parse(cls, payload: Any) -> Optional["AssignedHaulTripPayload"]:
        if not isinstance(payload, dict):
            return None
        if payload.get("action") != ContainerLogisticsActions.ASSIGNED_HAUL_TRIP:
            return None
        order = payload.get("order")
        if not isinstance(order, dict):
            return None
        return cls(
            payload["action"],
            order,
            payload.get("truck_id"),
            shared=bool(payload.get("shared", False)),
            owner_haulier_id=payload.get("owner_haulier_id"),
            carrier_haulier_id=payload.get("carrier_haulier_id"),
            benefit_km=payload.get("benefit_km"),
            pool_id=payload.get("pool_id"),
            awarded_cost_km=payload.get("awarded_cost_km"),
            owner_reserve_km=payload.get("owner_reserve_km"),
            market_round=payload.get("market_round"),
        )


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
