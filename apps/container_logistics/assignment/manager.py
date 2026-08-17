from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Set

from apps.common.resource_client_mixin import ResourceClientMixin, get_http_session
from apps.config import settings, simulation_domains
from apps.container_logistics.statemachine import HaulTripStateMachine, OrderStateMachine
from apps.utils import is_success
from orsim.lifecycle import ORSimManager
from orsim.utils import WorkflowStateMachine

logger = logging.getLogger(__name__)

_DEFAULT_PAGE_SIZE = 500
_DEFAULT_MAX_ORDERS_PER_TICK = 500


class AssignmentManager(ResourceClientMixin, ORSimManager):
    """Read-only OpenRide queries for trucks, orders, and active haul trips."""

    _final_haul_states = (HaulTripStateMachine.completed.name, HaulTripStateMachine.cancelled.name)

    def __init__(
        self,
        run_id: str,
        sim_clock: str,
        user,
        profile: Optional[Dict[str, Any]] = None,
        persona: Optional[Dict[str, Any]] = None,
    ):
        self.run_id = run_id
        self.sim_clock = sim_clock
        self.user = user
        self.profile = profile or {}
        self.persona = persona or {}
        self.simulation_domain = simulation_domains.get("container_logistics", "container-logistics-sim")
        self._facility_resource_id_cache: Dict[str, Optional[str]] = {}
        self._aggregates_enabled: Optional[bool] = None
        self._page_size = max(
            1,
            int(self.profile.get("assignment_page_size", _DEFAULT_PAGE_SIZE)),
        )
        self._max_orders_per_tick = max(
            1,
            int(self.profile.get("max_orders_per_tick", _DEFAULT_MAX_ORDERS_PER_TICK)),
        )
        # ORSimManager requires resource with _id; assignment does not persist an engine row via login().
        self.resource = {"_id": "assignment_service", "state": "online"}
        super().__init__()

    def on_init(self):
        pass

    def login(self, sim_clock: Any) -> Any:
        """No engine document to transition; assignment only performs REST reads."""
        return self.resource

    def logout(self, sim_clock: Any) -> Any:
        return self.resource

    def as_dict(self) -> Dict[str, Any]:
        return {"profile": self.profile, "run_id": self.run_id}

    def _truck_url(self) -> str:
        return f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/truck"

    def _order_url(self) -> str:
        return f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/order"

    def _haul_trip_url(self) -> str:
        return f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/truck/trip"

    def _facility_url(self) -> str:
        return f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/facility"

    def resolve_facility_resource_id(self, facility_name: Any) -> Optional[str]:
        if not facility_name:
            return None
        key = str(facility_name)
        if key in self._facility_resource_id_cache:
            return self._facility_resource_id_cache[key]
        url = self._facility_url()
        params = {
            "where": json.dumps(
                {"$and": [{"run_id": self.run_id}, {"profile.name": key}]}
            ),
            "page": 1,
            "max_results": 1,
        }
        try:
            response = get_http_session().get(
                url,
                headers=self.user.get_headers(),
                params=params,
                timeout=settings.get("NETWORK_REQUEST_TIMEOUT", 10),
            )
        except Exception:
            logging.exception("AssignmentManager.resolve_facility_resource_id failed for %r", key)
            return None
        if not is_success(response.status_code):
            logging.warning(
                "AssignmentManager: facility lookup HTTP %s for %r",
                response.status_code,
                key,
            )
            return None
        items = response.json().get("_items") or []
        if not items:
            logging.warning("AssignmentManager: no facility for profile.name=%r", key)
            return None
        fid = items[0].get("_id")
        out = str(fid) if fid is not None else None
        self._facility_resource_id_cache[key] = out
        return out

    def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        response = get_http_session().get(
            url,
            headers=self.user.get_headers(),
            params=params or {},
            timeout=settings.get("NETWORK_REQUEST_TIMEOUT", 10),
        )
        if not is_success(response.status_code):
            raise RuntimeError(f"{response.url} -> {response.status_code}: {response.text}")
        return response.json()

    def _try_aggregate_items(
        self,
        url: str,
        aggregate: Dict[str, Any],
    ) -> Optional[List[Dict[str, Any]]]:
        """Single-request Eve aggregation, or None if unavailable."""
        if self._aggregates_enabled is False:
            return None
        try:
            result = self._get(url, params={"aggregate": json.dumps(aggregate)})
        except Exception as exc:
            if self._aggregates_enabled is not False:
                logger.warning(
                    "Assignment aggregate unavailable url=%s (%s) — using paginated fallback "
                    "(restart OpenRide API after deploy to enable fast assignment reads)",
                    url,
                    exc,
                )
                self._aggregates_enabled = False
            return None
        self._aggregates_enabled = True
        return list(result.get("_items") or [])

    def _paged_where(
        self,
        base_url: str,
        where_clause: Dict[str, Any],
        projection: Optional[Dict[str, Any]] = None,
        *,
        max_results: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        page = 1
        page_size = max_results or self._page_size
        while True:
            params: Dict[str, Any] = {
                "where": json.dumps(where_clause),
                "page": page,
                "max_results": page_size,
            }
            if projection is not None:
                params["projection"] = json.dumps(projection)
            result = self._get(base_url, params)
            batch = result.get("_items") or []
            if not batch:
                break
            items.extend(batch)
            if max_results is not None and len(items) >= max_results:
                return items[:max_results]
            page += 1
        return items

    def list_trucks(
        self,
        projection: Optional[Dict[str, Any]] = None,
        *,
        online_only: bool = False,
    ) -> List[Dict[str, Any]]:
        # Fast path: one aggregation request for the whole online fleet instead of
        # paging (10+ round-trips at fleet scale). Falls back to pagination if the
        # endpoint isn't deployed yet (older API container). Only the online-fleet
        # read has a bulk endpoint; the rare full-fleet read still pages.
        if online_only:
            batch = self._try_aggregate_items(
                f"{self._truck_url()}/online_batch",
                {"$run_id": self.run_id, "$state": self.online_state_name()},
            )
            if batch is not None:
                return batch
        where: Dict[str, Any] = {"$and": [{"run_id": self.run_id}]}
        if online_only:
            where["$and"].append({"state": self.online_state_name()})
        return self._paged_where(self._truck_url(), where, projection=projection)

    def list_unassigned_orders(self, projection: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        batch = self._try_aggregate_items(
            f"{self._order_url()}/unassigned_batch",
            {
                "$run_id": self.run_id,
                "$state": OrderStateMachine.unassigned.name,
                "$limit": self._max_orders_per_tick,
            },
        )
        if batch is not None:
            return batch
        return self._paged_where(
            self._order_url(),
            {
                "$and": [
                    {"run_id": self.run_id},
                    {"state": OrderStateMachine.unassigned.name},
                ]
            },
            projection=projection,
            max_results=self._max_orders_per_tick,
        )

    def active_haul_truck_ids(self) -> List[str]:
        rows = self._try_aggregate_items(
            f"{self._haul_trip_url()}/active_truck_ids",
            {"$run_id": self.run_id},
        )
        if rows is not None:
            out: List[str] = []
            for row in rows:
                tid = row.get("truck")
                if tid:
                    out.append(str(tid))
            return out
        try:
            rows = self._paged_where(
                self._haul_trip_url(),
                {
                    "$and": [
                        {"run_id": self.run_id},
                        {"state": {"$nin": list(self._final_haul_states)}},
                    ]
                },
                projection={"truck": 1},
            )
        except Exception:
            logging.exception("AssignmentManager.active_haul_truck_ids failed")
            return []
        out = []
        for row in rows:
            tid = row.get("truck")
            if tid:
                out.append(str(tid))
        return out

    def order_ids_with_open_haul(self) -> Set[str]:
        """Order ids already tied to a non-terminal haul trip (Eve order may still be ``unassigned``)."""
        rows = self._try_aggregate_items(
            f"{self._haul_trip_url()}/open_order_ids",
            {"$run_id": self.run_id},
        )
        if rows is not None:
            out: Set[str] = set()
            for row in rows:
                oid = row.get("order")
                if oid:
                    out.add(str(oid))
            return out
        try:
            rows = self._paged_where(
                self._haul_trip_url(),
                {
                    "$and": [
                        {"run_id": self.run_id},
                        {"state": {"$nin": list(self._final_haul_states)}},
                        {"order": {"$exists": True, "$ne": None}},
                    ]
                },
                projection={"order": 1},
            )
        except Exception:
            logging.exception("AssignmentManager.order_ids_with_open_haul failed")
            return set()
        out = set()
        for row in rows:
            oid = row.get("order")
            if oid:
                out.add(str(oid))
        return out

    @staticmethod
    def online_state_name() -> str:
        return WorkflowStateMachine.online.name
