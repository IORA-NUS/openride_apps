from orsim.lifecycle import ORSimApp
from orsim.messenger.interaction import CallbackRouterPlugin, InteractionContext
from orsim.utils import WorkflowStateMachine

from apps.common.user_registry import UserRegistry
from apps.utils import str_to_time
from apps.container_logistics.message_data_models import (
    AssignedHaulTripPayload,
    FacilityWorkflowPayload,
    OrderWorkflowPayload,
)
from apps.container_logistics.statemachine import (
    ContainerLogisticsActions,
    HaulTripStateMachine,
    haultrip_gate_interactions,
    haultrip_order_interactions,
)

from shapely.geometry import LineString, Point, mapping

from apps.loc_service.osrm_client import OSRMClient

from .facility_interaction_mixin import FacilityInteractionMixin
from .idle_trip_manager import TruckIdleTripManager
from .manager import TruckManager
from .order_interaction_mixin import OrderInteractionMixin
from .trip_manager import TruckTripManager


class TruckApp(ORSimApp, OrderInteractionMixin, FacilityInteractionMixin):
    exited_market = False

    @staticmethod
    def _ensure_point(loc):
        """Normalize a geojson Point-like dict to OSRMClient input."""
        if not isinstance(loc, dict):
            return None
        if loc.get("type") == "Point" and isinstance(loc.get("coordinates"), (list, tuple)) and len(loc["coordinates"]) >= 2:
            return {"type": "Point", "coordinates": [loc["coordinates"][0], loc["coordinates"][1]]}
        # Also accept shapely mapping output (same shape) or any dict with coordinates.
        if isinstance(loc.get("coordinates"), (list, tuple)) and len(loc["coordinates"]) >= 2:
            return {"type": "Point", "coordinates": [loc["coordinates"][0], loc["coordinates"][1]]}
        return None

    def _plan_routes_for_assignment(self, current_loc, order):
        """
        Compute real routes exactly once at assignment time.
        Returns (reposition_route, loaded_route, eta_pickup, eta_dropoff).
        """
        start = self._ensure_point(current_loc)
        pickup = self._ensure_point((order or {}).get("pickup_loc"))
        dropoff = self._ensure_point((order or {}).get("dropoff_loc"))

        if start is None or pickup is None or dropoff is None:
            return None, None, None, None

        try:
            reposition_route = OSRMClient.get_route(start, pickup)
        except Exception:
            reposition_route = None

        try:
            loaded_route = OSRMClient.get_route(pickup, dropoff)
        except Exception:
            loaded_route = None

        eta_pickup = reposition_route.get("duration") if isinstance(reposition_route, dict) else None
        eta_dropoff = loaded_route.get("duration") if isinstance(loaded_route, dict) else None
        return reposition_route, loaded_route, eta_pickup, eta_dropoff

    @property
    def managed_statemachine(self):
        return HaulTripStateMachine

    @property
    def interaction_ground_truth_list(self):
        return [haultrip_order_interactions, haultrip_gate_interactions]

    @property
    def runtime_behavior_schema(self):
        return {
            "init_loc": {"type": "dict", "required": True},
            "shift_start_time": {"type": "integer", "required": True},
            "shift_end_time": {"type": "integer", "required": True},
            "steps_per_action": {"type": "integer", "required": False},
            "response_rate": {"type": "number", "required": False},
        }

    def __init__(self, run_id, sim_clock, behavior, messenger, agent_helper=None):
        super().__init__(
            run_id=run_id,
            sim_clock=sim_clock,
            behavior=behavior,
            messenger=messenger,
            agent_helper=agent_helper,
        )
        self.trip = self.create_trip_manager()
        self.idle = None
        self.current_time = None
        self.current_time_str = None
        self.current_loc = self.behavior.get("init_loc")
        self.latest_loc = self.current_loc
        self.latest_sim_clock = sim_clock
        self._interaction_plugin = CallbackRouterPlugin(handler_obj=self)

    def _create_user(self):
        return UserRegistry(self.sim_clock, self.credentials)

    def _create_manager(self):
        return TruckManager(
            run_id=self.run_id,
            sim_clock=self.sim_clock,
            user=self.user,
            profile=self.behavior.get("profile", {}),
            persona=self.behavior.get("persona", {}),
        )

    def create_trip_manager(self):
        return TruckTripManager(
            run_id=self.run_id,
            sim_clock=self.sim_clock,
            user=self.user,
            messenger=self.messenger,
            persona=self.behavior.get("persona", {}),
        )

    def launch(self, sim_clock):
        super().launch(sim_clock)
        # Start an idle trip immediately; it does not block assignment because it is not a haul trip endpoint.
        self._ensure_idle_trip(sim_clock)

    def close(self, sim_clock):
        if self.trip.as_dict() is not None:
            self.trip.cancel(sim_clock, current_loc=self.current_loc)
        self._end_idle_trip(sim_clock)
        super().close(sim_clock)

    def _ensure_idle_trip(self, sim_clock):
        """Create/maintain a persisted idle-trip resource."""
        if self.idle is None:
            self.idle = TruckIdleTripManager(
                run_id=self.run_id,
                sim_clock=sim_clock,
                user=self.user,
                current_loc=self.current_loc,
                persona=self.behavior.get("persona", {}),
            )
        # Persist latest stationary position each tick while idle.
        self.idle.ping(sim_clock=sim_clock, current_loc=self.current_loc)

    def _end_idle_trip(self, sim_clock):
        if self.idle is None:
            return
        try:
            self.idle.end(sim_clock=sim_clock, current_loc=self.current_loc)
        finally:
            self.idle = None

    def refresh(self):
        self.manager.refresh()
        if self.trip.as_dict() is not None:
            self.trip.refresh()
            trip = self.trip.as_dict() or {}
            if trip.get("state") in (HaulTripStateMachine.completed.name, HaulTripStateMachine.cancelled.name):
                # Clear active haul trip and fall back to idle.
                self.trip.trip = None
                self._ensure_idle_trip(self.latest_sim_clock)
        else:
            # No haul trip -> remain on idle trip until assigned.
            self._ensure_idle_trip(self.latest_sim_clock)

    def get_truck(self):
        return self.manager.as_dict()

    def get_manager(self):
        return self.manager.as_dict()

    def get_trip(self):
        return self.trip.as_dict()

    def create_new_haul_trip(self, sim_clock, current_loc, truck, order):
        return self.trip.create_new_trip(sim_clock, current_loc, truck, order)

    def handle_assignment(self, sim_clock, current_loc, order):
        if not self.manager.is_assignable(active_trip=self.trip.as_dict()):
            return None
        # Leaving idle state as soon as we get a job.
        self._end_idle_trip(sim_clock)

        # Compute planned routes once (for smooth viz + correct ETAs) and store on the truck profile
        # (routes / leg ETAs are not carried on the order).
        reposition_route, loaded_route, eta_pickup, eta_dropoff = self._plan_routes_for_assignment(current_loc, order or {})
        base_truck = self.get_truck()
        enriched_truck = dict(base_truck)
        truck_prof = dict(enriched_truck.get("profile") or {})
        if reposition_route is not None:
            truck_prof["planned_reposition_route"] = reposition_route
        if loaded_route is not None:
            truck_prof["planned_dropoff_route"] = loaded_route
        if eta_pickup is not None:
            truck_prof["estimated_time_to_pickup"] = eta_pickup
        if eta_dropoff is not None:
            truck_prof["estimated_time_to_dropoff"] = eta_dropoff
        enriched_truck["profile"] = truck_prof

        enriched_order = dict(order or {})
        for _k in (
            "planned_reposition_route",
            "planned_dropoff_route",
            "estimated_time_to_pickup",
            "estimated_time_to_dropoff",
        ):
            enriched_order.pop(_k, None)

        trip = self.create_new_haul_trip(sim_clock, current_loc, enriched_truck, enriched_order)
        self.trip.assign(sim_clock, current_loc=current_loc, order=enriched_order, truck=enriched_truck)
        return trip

    def handle_app_topic_messages(self, payload):
        if payload.get("action") == ContainerLogisticsActions.ASSIGNED_HAUL_TRIP:
            parsed = AssignedHaulTripPayload.parse(payload)
            if parsed is None:
                return
            if parsed.truck_id is not None and parsed.truck_id != self.manager.get_id():
                return
            self.handle_assignment(self.latest_sim_clock, self.latest_loc, parsed.order)
            return
        self.enqueue_message(payload)

    def consume_messages(self):
        payload = self.dequeue_message()
        while payload is not None:
            parsed_data = payload.get("data")
            if payload.get("action") == ContainerLogisticsActions.ORDER_WORKFLOW_EVENT:
                parsed = OrderWorkflowPayload.parse(payload)
                if parsed is None:
                    payload = self.dequeue_message()
                    continue
                parsed_data = parsed.data
            elif payload.get("action") == ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT:
                parsed = FacilityWorkflowPayload.parse(payload)
                if parsed is None:
                    payload = self.dequeue_message()
                    continue
                parsed_data = parsed.data
            self._interaction_plugin.on_message(
                InteractionContext(
                    action=payload.get("action"),
                    event=(parsed_data or {}).get("event", payload.get("event")),
                    payload=payload,
                    data=parsed_data,
                )
            )
            payload = self.dequeue_message()

    def perform_workflow_actions(self):
        if self.get_truck().get("state") != WorkflowStateMachine.online.name:
            raise Exception(f"Truck not available for workflow actions: {self.get_truck().get('state')}")
        trip = self.get_trip()
        if trip is None:
            return
        self._interaction_plugin.on_state(
            InteractionContext(
                state=trip.get("state"),
                extra={"time_since_last_event": 0},
            )
        )

    @staticmethod
    def _extract_route_coords(route):
        """
        Extract a coordinate sequence (lon, lat) from a planned route object, without
        making any routing calls.
        """
        if not isinstance(route, dict):
            return None
        if route.get("geometry"):
            try:
                return OSRMClient.get_coords_from_route(route)
            except Exception:
                return None
        coords = route.get("coordinates") or route.get("coords")
        if isinstance(coords, list) and coords:
            # Accept either [(lon,lat), ...] or [[lon,lat], ...]
            return [tuple(c[:2]) for c in coords if isinstance(c, (list, tuple)) and len(c) >= 2]
        return None

    def update_location_by_planned_route(self):
        """
        Option C: move smoothly along an existing planned route geometry (if present).
        Falls back to no-op when routes don't include geometry/coords.
        """
        trip = self.get_trip()
        if trip is None:
            return

        state = trip.get("state")
        stats = trip.get("stats") or {}
        planned = (trip.get("routes") or {}).get("planned") or {}

        # Snap locations for terminal states that imply arrival.
        if state in (HaulTripStateMachine.queued_for_pickup.name, HaulTripStateMachine.at_pickup_gate.name):
            pickup = trip.get("pickup_loc")
            if pickup:
                self.current_loc = pickup
            return
        if state in (HaulTripStateMachine.queued_for_dropoff.name, HaulTripStateMachine.at_dropoff_gate.name):
            dropoff = trip.get("dropoff_loc")
            if dropoff:
                self.current_loc = dropoff
            return

        if state == HaulTripStateMachine.repositioning_to_pickup.name:
            route = planned.get("repositioning_to_pickup")
            try:
                duration = float(stats.get("estimated_time_to_pickup") or 0)
            except (TypeError, ValueError):
                duration = 0.0
            if duration <= 0 and isinstance(route, dict):
                try:
                    duration = float(route.get("duration") or 0)
                except (TypeError, ValueError):
                    duration = 0.0
        elif state == HaulTripStateMachine.loaded_in_transit.name:
            route = planned.get("loaded_to_dropoff")
            try:
                duration = float(stats.get("estimated_time_to_dropoff") or 0)
            except (TypeError, ValueError):
                duration = 0.0
            if duration <= 0 and isinstance(route, dict):
                try:
                    duration = float(route.get("duration") or 0)
                except (TypeError, ValueError):
                    duration = 0.0
        else:
            return

        coords = self._extract_route_coords(route)
        if not coords or len(coords) < 2 or duration <= 0:
            return

        # Use the trip's last transition time (`sim_clock`) as the phase anchor.
        try:
            anchor = str_to_time(trip.get("sim_clock"))
        except Exception:
            anchor = self.current_time
        elapsed = max(0.0, (self.current_time - anchor).total_seconds())
        progress = min(1.0, elapsed / duration) if duration > 0 else 1.0

        try:
            line = LineString(coords)
            if line.length <= 0:
                return
            point = line.interpolate(progress * line.length)
            if isinstance(point, Point):
                self.current_loc = mapping(Point(point.x, point.y))
        except Exception:
            return

    def execute_step_actions(self, current_time, add_step_log_fn=None):
        self.current_time = current_time
        self.current_time_str = current_time.strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.refresh()
        self.update_location_by_planned_route()
        self.consume_messages()
        self.perform_workflow_actions()
