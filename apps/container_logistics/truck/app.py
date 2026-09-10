import logging

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
from apps.utils.step_profile import span, tick
from apps.container_logistics.haul_trip_duration import (
    apply_haul_trip_duration_floors,
    patch_route_duration,
    warn_if_haul_trip_under_minimum,
)

from .facility_interaction_mixin import FacilityInteractionMixin
from .idle_trip_manager import TruckIdleTripManager
from .manager import TruckManager
from .order_interaction_mixin import OrderInteractionMixin
from .trip_manager import TruckTripManager


class TruckApp(ORSimApp, OrderInteractionMixin, FacilityInteractionMixin):
    exited_market = False
    _osrm_route_cache = {}
    _osrm_route_cache_max = 512

    def _use_osrm_at_assignment(self) -> bool:
        profile = self.behavior.get("profile") or {}
        return bool(profile.get("use_osrm_at_assignment", False))

    @classmethod
    def _osrm_cache_get(cls, cache_key):
        return cls._osrm_route_cache.get(cache_key)

    @classmethod
    def _osrm_cache_put(cls, cache_key, reposition_route, loaded_route):
        max_entries = cls._osrm_route_cache_max
        if max_entries > 0 and len(cls._osrm_route_cache) >= max_entries:
            cls._osrm_route_cache.pop(next(iter(cls._osrm_route_cache)))
        cls._osrm_route_cache[cache_key] = (reposition_route, loaded_route)

    @staticmethod
    def _route_cache_key(start, pickup, dropoff):
        def rounded(point):
            coords = (point or {}).get("coordinates") or [0, 0]
            return (round(float(coords[0]), 5), round(float(coords[1]), 5))

        return (rounded(start), rounded(pickup), rounded(dropoff))

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

    # The route stored at assignment is now AUTHORITATIVE: the truck drives it, the KPI
    # measures it, and replay draws it. So an OSRM failure is no longer a cosmetic
    # degradation — it silently changes simulated movement and the headline deadhead
    # metric. Never swallow it: log at ERROR and stamp the leg so the failure is visible
    # in the trip document (and therefore in the replay provenance badge) instead of being
    # indistinguishable from a real route.
    def _route_or_loud_failure(self, start, end, leg):
        try:
            # steps='false': the turn-by-turn block is 93% of the response (measured
            # 30,908 B vs 2,001 B per route, IDENTICAL geometry) and nothing in
            # container_logistics reads it — but this route is stored on the trip doc,
            # echoed into meta, and put on the MQTT wire, so the bloat multiplies.
            route = OSRMClient.get_route(start, end, steps='false')
        except Exception:
            logging.exception(
                "OSRM route FAILED at assignment (run=%s leg=%s) — this leg has no "
                "authoritative geometry; movement falls back to interpolation and its "
                "distance to haversine.",
                getattr(self, "run_id", "?"),
                leg,
            )
            return {"geometry_source": "unavailable"}
        if isinstance(route, dict) and route.get("geometry"):
            route["geometry_source"] = "osrm"
            return route
        logging.error(
            "OSRM returned no geometry at assignment (run=%s leg=%s) — leg marked "
            "unavailable.",
            getattr(self, "run_id", "?"),
            leg,
        )
        return {"geometry_source": "unavailable"}

    @staticmethod
    def _is_real_route(route):
        return isinstance(route, dict) and bool(route.get("geometry"))

    def _plan_routes_for_assignment(self, current_loc, order):
        """
        Compute real routes exactly once at assignment time.
        Returns (reposition_route, loaded_route, eta_pickup, eta_dropoff).
        """
        behavior_profile = self.behavior.get("profile") or {}
        if not self._use_osrm_at_assignment():
            eta_pickup = behavior_profile.get("estimated_time_to_pickup")
            eta_dropoff = behavior_profile.get("estimated_time_to_dropoff")
            eta_pickup, eta_dropoff = apply_haul_trip_duration_floors(
                eta_pickup,
                eta_dropoff,
                order=order,
                profile=behavior_profile,
            )
            return None, None, eta_pickup, eta_dropoff

        start = self._ensure_point(current_loc)
        pickup = self._ensure_point((order or {}).get("pickup_loc"))
        dropoff = self._ensure_point((order or {}).get("dropoff_loc"))

        if start is None or pickup is None or dropoff is None:
            return None, None, None, None

        cache_key = self._route_cache_key(start, pickup, dropoff)
        cached = self._osrm_cache_get(cache_key)
        if cached is not None:
            reposition_route, loaded_route = cached
        else:
            reposition_route = self._route_or_loud_failure(start, pickup, "repositioning_to_pickup")
            loaded_route = self._route_or_loud_failure(pickup, dropoff, "loaded_to_dropoff")
            # Cache SUCCESSES ONLY. `_osrm_route_cache` is a class attribute on a
            # long-lived Celery worker and is never keyed by run_id nor invalidated, so
            # caching a failure marker would poison that OD triple for the rest of the
            # worker's life — and for every subsequent run. A 2-second OSRM restart would
            # otherwise permanently degrade geometry for everything seen in that window.
            if self._is_real_route(reposition_route) and self._is_real_route(loaded_route):
                self._osrm_cache_put(cache_key, reposition_route, loaded_route)

        eta_pickup = reposition_route.get("duration") if isinstance(reposition_route, dict) else None
        eta_dropoff = loaded_route.get("duration") if isinstance(loaded_route, dict) else None
        if eta_pickup is None:
            eta_pickup = behavior_profile.get("estimated_time_to_pickup")
        if eta_dropoff is None:
            eta_dropoff = behavior_profile.get("estimated_time_to_dropoff")

        eta_pickup, eta_dropoff = apply_haul_trip_duration_floors(
            eta_pickup,
            eta_dropoff,
            order=order,
            profile=behavior_profile,
        )
        reposition_route = patch_route_duration(reposition_route, eta_pickup)
        loaded_route = patch_route_duration(loaded_route, eta_dropoff)
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
        # Cache-invalidation flags for `refresh()`. The truck's own resource
        # and its haul trip are only ever mutated by the truck itself
        # (apply_trip_transition_and_notify refreshes after every PATCH), so a
        # blind GET every step is just connection churn. We only re-fetch
        # when something external bumps these flags.
        self._truck_refresh_pending = True
        self._trip_refresh_pending = True
        cache_max = (self.behavior.get("profile") or {}).get("osrm_route_cache_max_entries")
        if cache_max is not None:
            try:
                TruckApp._osrm_route_cache_max = max(0, int(cache_max))
            except (TypeError, ValueError):
                pass

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
            order_events_topic=(self.behavior.get("profile") or {}).get("order_events_topic"),
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

    def invalidate_truck_cache(self) -> None:
        """Mark the truck resource cache stale so the next refresh fetches it."""
        self._truck_refresh_pending = True

    def invalidate_trip_cache(self) -> None:
        """Mark the haul-trip resource cache stale."""
        self._trip_refresh_pending = True

    def refresh(self):
        # Only hit OpenRide when we know the in-memory copy is stale (init,
        # or an external event flipped a flag). Truck/trip Mongo writes only
        # ever come from this agent's own PATCHes, which already refresh the
        # local cache via apply_trip_transition_and_notify.
        if self._truck_refresh_pending:
            self.manager.refresh()
            self._truck_refresh_pending = False
        if self.trip.as_dict() is not None:
            if self._trip_refresh_pending:
                self.trip.refresh()
                self._trip_refresh_pending = False
            trip = self.trip.as_dict() or {}
            if trip.get("state") == HaulTripStateMachine.completed.name:
                warn_if_haul_trip_under_minimum(
                    trip,
                    completed_at=self.current_time,
                    profile=(self.behavior.get("profile") or {}),
                )
                # Record where this haul ended so the assignment cost function can
                # favour a next order that picks up nearby (dual-cycle).
                self.manager.set_last_dropoff(
                    dropoff_loc=trip.get("dropoff_loc"),
                    dropoff_facility_name=(trip.get("meta") or {})
                    .get("order_profile", {})
                    .get("dropoff_facility_name"),
                )
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

    def handle_assignment(self, sim_clock, current_loc, order, collaboration=None):
        if not self.manager.is_assignable(active_trip=self.trip.as_dict()):
            return None
        # Leaving idle state as soon as we get a job.
        self._end_idle_trip(sim_clock)

        # Compute planned routes once (for smooth viz + correct ETAs) and store on the truck profile
        # (routes / leg ETAs are not carried on the order).
        reposition_route, loaded_route, eta_pickup, eta_dropoff = self._plan_routes_for_assignment(
            current_loc, order or {}
        )
        base_truck = self.get_truck()
        enriched_truck = dict(base_truck)
        truck_prof = dict(enriched_truck.get("profile") or {})
        if reposition_route is not None:
            truck_prof["planned_reposition_route"] = reposition_route
        if loaded_route is not None:
            truck_prof["planned_dropoff_route"] = loaded_route
        truck_prof["estimated_time_to_pickup"] = eta_pickup
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

        if collaboration and collaboration.get("shared"):
            # Cross-haulier job (cooperation structure): ride the order copy into
            # create_new_trip, which lifts it onto the trip doc's meta.
            enriched_order["_collaboration"] = dict(collaboration)

        trip = self.create_new_haul_trip(sim_clock, current_loc, enriched_truck, enriched_order)
        self.trip.assign(sim_clock, current_loc=current_loc, order=enriched_order, truck=enriched_truck)
        # create_new_haul_trip already POST+GETs the new trip and assign()
        # PATCH+GETs it again, so the in-memory copy is authoritative — no
        # need for execute_step_actions to re-fetch it on the very next step.
        self._trip_refresh_pending = False
        return trip

    def handle_app_topic_messages(self, payload):
        if payload.get("action") == ContainerLogisticsActions.ASSIGNED_HAUL_TRIP:
            parsed = AssignedHaulTripPayload.parse(payload)
            if parsed is None:
                return
            if parsed.truck_id is not None and str(parsed.truck_id) != str(self.manager.get_id()):
                return
            collaboration = None
            if parsed.shared:
                collaboration = {
                    "shared": True,
                    "owner_haulier_id": parsed.owner_haulier_id,
                    "carrier_haulier_id": parsed.carrier_haulier_id,
                    "benefit_km": parsed.benefit_km,
                    # Shared-pool audit trail (plan §6.14). Rides into
                    # trip.meta.collaboration through the existing _collaboration
                    # path — trip_manager needs no change. None on the legacy path.
                    "pool_id": parsed.pool_id,
                    "awarded_cost_km": parsed.awarded_cost_km,
                    "owner_reserve_km": parsed.owner_reserve_km,
                    "market_round": parsed.market_round,
                }
            self.handle_assignment(
                self.latest_sim_clock, self.latest_loc, parsed.order,
                collaboration=collaboration,
            )
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

    def _emit_truck_location(self) -> None:
        # Headless runs disable the visual-only truck-location stream. The TruckApp has no
        # orsim_settings (only the agent does), so the flag rides in on the truck behavior's
        # profile (stream_geo, injected per-run when ORSIM_HEADLESS is set — it ships to the
        # Celery agent at spawn, same mechanism as the solver override). This is the per-truck,
        # per-step hot path, so the early return is the main headless speedup.
        if not (self.behavior.get("profile") or {}).get("stream_geo", True):
            return
        if not self.current_time_str:
            return
        loc = self.current_loc
        if not isinstance(loc, dict):
            return
        coords = loc.get("coordinates") or []
        if len(coords) < 2:
            return
        trip = self.get_trip()
        haul_state = (trip or {}).get("state")
        agent_id = self.credentials.get("email", "").split("@")[0]
        if not agent_id:
            return
        truck_prof = self.behavior.get("profile") or {}
        try:
            from apps.container_logistics.analytics.trip_geo_publisher import publish_truck_location
            publish_truck_location(
                self.run_id,
                self.current_time_str,
                agent_id,
                float(coords[0]),
                float(coords[1]),
                haul_state,
                haulier_id=truck_prof.get("haulier_id"),
                haulier_name=truck_prof.get("haulier_name"),
            )
        except Exception:
            pass

    def execute_step_actions(self, current_time, add_step_log_fn=None):
        with span("truck.tick"):
            self.current_time = current_time
            self.current_time_str = current_time.strftime("%a, %d %b %Y %H:%M:%S GMT")
            with span("truck.refresh"):
                self.refresh()
            with span("truck.update_location"):
                self.update_location_by_planned_route()
            with span("truck.emit_location"):
                self._emit_truck_location()
            with span("truck.consume_1"):
                self.consume_messages()
            with span("truck.workflow"):
                self.perform_workflow_actions()
            with span("truck.consume_2"):
                self.consume_messages()
        tick("truck.tick")
