from dataclasses import dataclass
from typing import Any, Dict, Optional

from apps.ride_hail.statemachine.events import RideHailActions


@dataclass
class RequestedTripPayload:
    action: str
    passenger_id: str
    requested_trip: Dict[str, Any]

    @classmethod
    def parse(cls, payload: Any) -> Optional["RequestedTripPayload"]:
        if not isinstance(payload, dict):
            return None
        if payload.get("action") != RideHailActions.REQUESTED_TRIP:
            return None
        passenger_id = payload.get("passenger_id")
        requested_trip = payload.get("requested_trip")
        if passenger_id is None or requested_trip is None:
            return None
        return cls(payload["action"], passenger_id, requested_trip)


@dataclass
class AssignedPayload:
    action: str
    driver_id: str

    @classmethod
    def parse(cls, payload: Any) -> Optional["AssignedPayload"]:
        if not isinstance(payload, dict):
            return None
        if payload.get("action") != RideHailActions.ASSIGNED:
            return None
        driver_id = payload.get("driver_id")
        if driver_id is None:
            return None
        return cls(payload["action"], driver_id)


@dataclass
class PassengerWorkflowPayload:
    action: str
    passenger_id: str
    data: Dict[str, Any]

    @classmethod
    def parse(cls, payload: Any) -> Optional["PassengerWorkflowPayload"]:
        if not isinstance(payload, dict):
            return None
        if payload.get("action") != RideHailActions.PASSENGER_WORKFLOW_EVENT:
            return None
        passenger_id = payload.get("passenger_id")
        data = payload.get("data")
        if passenger_id is None or not isinstance(data, dict) or data.get("event") is None:
            return None
        return cls(payload["action"], passenger_id, data)


@dataclass
class DriverWorkflowPayload:
    action: str
    driver_id: str
    data: Dict[str, Any]

    @classmethod
    def parse(cls, payload: Any) -> Optional["DriverWorkflowPayload"]:
        if not isinstance(payload, dict):
            return None
        if payload.get("action") != RideHailActions.DRIVER_WORKFLOW_EVENT:
            return None
        driver_id = payload.get("driver_id")
        data = payload.get("data")
        if driver_id is None or not isinstance(data, dict) or data.get("event") is None:
            return None
        return cls(payload["action"], driver_id, data)
