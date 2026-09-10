import json
import logging
from datetime import timedelta
from typing import Any, Optional

import requests

from apps.common.resource_client_mixin import get_http_session
from apps.common.trip_manager_base import TripManagerBase
from apps.config import settings, simulation_domains
from apps.container_logistics.haul_trip_duration import (
    apply_haul_trip_duration_floors,
    patch_route_duration,
)
from apps.container_logistics.statemachine import (
    ContainerLogisticsActions,
    ContainerLogisticsEvents,
    HaulTripStateMachine,
    haultrip_gate_interactions,
    haultrip_order_interactions,
)
from apps.utils import is_success, str_to_time
from apps.utils.excepions import WriteFailedException


_PLANNED_ROUTE_KEYS = ("planned_reposition_route", "planned_dropoff_route")


def _without_planned_routes(profile):
    """Truck profile minus the transported route payloads (see `create_new_trip`)."""
    if not isinstance(profile, dict):
        return profile
    if not any(k in profile for k in _PLANNED_ROUTE_KEYS):
        return profile
    return {k: v for k, v in profile.items() if k not in _PLANNED_ROUTE_KEYS}


def _route_summary(route):
    """The only parts of an OSRM route any consumer reads, for the MQTT context."""
    if not isinstance(route, dict):
        return route
    summary = {k: route[k] for k in ("geometry", "distance", "duration") if k in route}
    if "geometry_source" in route:
        summary["geometry_source"] = route["geometry_source"]
    return summary or route


class TruckTripManager(TripManagerBase):
    def __init__(self, run_id, sim_clock, user, messenger, persona=None, order_events_topic: Optional[str] = None):
        super().__init__(run_id, user, messenger, persona=persona or {"role": "truck"})
        self.simulation_domain = simulation_domains.get("container_logistics", "container-logistics-sim")
        # ``service`` order-lifecycle mode: address every ORDER_* event to ONE shared topic
        # suffix instead of the per-order ``run_id/<order_id>`` topic. Ships as data on the
        # truck behavior profile (the stream_geo pattern), so switching modes needs no celery
        # restart. ``None`` (default) = today's per-order topics, byte-identically.
        self._order_events_topic = order_events_topic

    @property
    def StateMachineCls(self):
        return HaulTripStateMachine

    @property
    def message_channel(self):
        # Workflow notifications are routed in post_transition_hook (order vs facility topics).
        return None

    @property
    def statemachine_interaction_mapping(self):
        return haultrip_order_interactions + haultrip_gate_interactions

    def post_transition_hook(self, source_transition, source_new_state, context=None):
        """
        Publish MQTT workflow messages after a successful haul-trip REST transition.

        TripManagerBase skips publishing when ``message_channel`` is None; ride-hail uses a
        single passenger topic, but container logistics must notify **order** agents
        (``run_id/<order _id>``) and **facility** agents (``run_id/<facility _id>``). Without
        this, orders stay ``unassigned`` in Eve, facilities never see queue arrivals, and
        hauls eventually cancel on shutdown with ``num_hauls_completed`` stuck at zero.
        """
        event: Optional[str] = None
        for rule in self.statemachine_interaction_mapping:
            if (
                rule.get("source_statemachine") == self.StateMachineCls.__name__
                and rule.get("source_transition") == source_transition
            ):
                event = rule.get("event")
                break
        if not event:
            return
        msg = self.message_template(event)
        if context:
            msg["data"].update(context)
        channel = self._mqtt_topic_for_workflow_event(event)
        if not channel or self.messenger is None:
            return
        try:
            self.messenger.client.publish(channel, json.dumps(msg, default=str))
        except Exception:
            logging.exception("TruckTripManager: failed to publish workflow event %s to %s", event, channel)

    def _mqtt_topic_for_workflow_event(self, event: str) -> Optional[str]:
        if event.startswith("order_"):
            oid = (self.trip or {}).get("order")
            if not oid:
                # No order on the trip => nothing to notify, in BOTH modes.
                return None
            if self._order_events_topic:
                return f"{self.run_id}/{self._order_events_topic}"
            return f"{self.run_id}/{oid}"
        meta = (self.trip or {}).get("meta") or {}
        if event == ContainerLogisticsEvents.TRUCK_ARRIVED_PICKUP_QUEUE:
            fid = meta.get("pickup_facility_resource_id")
            if fid:
                return f"{self.run_id}/{fid}"
            name = meta.get("order_profile", {}).get("pickup_facility_name")
            return self._facility_topic_for_profile_name(name)
        if event == ContainerLogisticsEvents.TRUCK_ARRIVED_DROPOFF_QUEUE:
            fid = meta.get("dropoff_facility_resource_id")
            if fid:
                return f"{self.run_id}/{fid}"
            name = meta.get("order_profile", {}).get("dropoff_facility_name")
            return self._facility_topic_for_profile_name(name)
        return None

    def _facility_resource_id_by_profile_name(self, facility_name: Any) -> Optional[str]:
        if not facility_name:
            return None
        url = f"{settings['OPENRIDE_SERVER_URL']}/{self.simulation_domain}/{self.run_id}/facility"
        params = {
            "where": json.dumps(
                {"$and": [{"run_id": self.run_id}, {"profile.name": str(facility_name)}]}
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
            logging.exception("TruckTripManager: facility lookup failed for %r", facility_name)
            return None
        if not is_success(response.status_code):
            logging.warning(
                "TruckTripManager: facility lookup HTTP %s for %r",
                response.status_code,
                facility_name,
            )
            return None
        items = response.json().get("_items") or []
        if not items:
            logging.warning("TruckTripManager: no facility document for profile.name=%r", facility_name)
            return None
        fid = items[0].get("_id")
        return str(fid) if fid is not None else None

    def _facility_topic_for_profile_name(self, facility_name: Any) -> Optional[str]:
        fid = self._facility_resource_id_by_profile_name(facility_name)
        return f"{self.run_id}/{fid}" if fid else None

    def message_template(self, event):
        if event.startswith("order_"):
            return {
                "action": ContainerLogisticsActions.ORDER_WORKFLOW_EVENT,
                "truck_id": self.trip.get("truck"),
                # F10: carry the trip's own sim time so a batched applier can bucket
                # completions by the true event time rather than its own step clock.
                # (post_transition_hook merges the transition ``context`` into ``data``;
                # no context key is named ``sim_clock``, so this is never clobbered.)
                "data": {
                    "event": event,
                    "order_id": self.trip.get("order"),
                    "sim_clock": self.trip.get("sim_clock"),
                },
            }
        return {
            "action": ContainerLogisticsActions.FACILITY_WORKFLOW_EVENT,
            "truck_id": self.trip.get("truck"),
            "data": {"event": event},
        }

    def as_dict(self):
        return self.trip

    def refresh(self):
        if self.trip is not None:
            response = self._get_trip()
            if is_success(response.status_code):
                self.trip = response.json()
            else:
                raise WriteFailedException(f"Unable to refresh haul trip: {response.url}, {response.text}")

    def create_new_trip(self, sim_clock, current_loc, truck, order):
        truck_prof = (truck or {}).get("profile") or {}
        o = order or {}
        eta_p = truck_prof.get("estimated_time_to_pickup")
        if eta_p is None:
            eta_p = o.get("estimated_time_to_pickup", 0)
        eta_d = truck_prof.get("estimated_time_to_dropoff")
        if eta_d is None:
            eta_d = o.get("estimated_time_to_dropoff", 0)
        eta_p, eta_d = apply_haul_trip_duration_floors(
            eta_p,
            eta_d,
            order=o,
            profile=truck_prof,
        )
        plan_repo = truck_prof.get("planned_reposition_route")
        if plan_repo is None:
            plan_repo = o.get("planned_reposition_route")
        plan_drop = truck_prof.get("planned_dropoff_route")
        if plan_drop is None:
            plan_drop = o.get("planned_dropoff_route")

        # Cross-haulier job sharing (cooperation structures): the tag rides in on
        # the order copy from handle_assignment; lift it onto the trip's meta so
        # analytics can attribute owner/carrier gains. Popped so it never leaks
        # into the order payload PATCHed elsewhere.
        collaboration = o.pop("_collaboration", None) if isinstance(o, dict) else None

        order_prof = o.get("profile") or {}
        pickup_facility_id = o.get("pickup_facility_resource_id") or self._facility_resource_id_by_profile_name(
            order_prof.get("pickup_facility_name")
        )
        dropoff_facility_id = o.get("dropoff_facility_resource_id") or self._facility_resource_id_by_profile_name(
            order_prof.get("dropoff_facility_name")
        )

        data = {
            "truck": truck.get("_id"),
            "order": order.get("_id"),
            "persona": self.persona,
            "meta": {
                # The planned routes are transported on the truck profile (see
                # `handle_assignment`) purely to reach `routes.planned` just below.
                # Keeping them here too stored the SAME polylines a second time in the
                # same document for no reader — pure bloat on every haul doc.
                "truck_profile": _without_planned_routes(truck.get("profile", {})),
                "order_profile": order.get("profile", {}),
                "pickup_facility_resource_id": pickup_facility_id,
                "dropoff_facility_resource_id": dropoff_facility_id,
                # Truck location at assignment = start of the empty repositioning leg. Captured
                # here because current_loc is overwritten as the truck advances, leaving completed
                # trips with current_loc == dropoff (which makes the empty/loaded haversine fallback
                # degenerate to a 0.5 deadhead ratio).
                "reposition_origin_loc": current_loc,
                **({"collaboration": collaboration} if collaboration else {}),
            },
            "current_loc": current_loc,
            "pickup_loc": order.get("pickup_loc"),
            "dropoff_loc": order.get("dropoff_loc"),
            "stats": {
                "estimated_time_to_pickup": eta_p,
                "estimated_time_to_dropoff": eta_d,
                "pickup_service_time": order.get("pickup_service_time", 0),
                "dropoff_service_time": order.get("dropoff_service_time", 0),
            },
            "routes": {
                "planned": {
                    "repositioning_to_pickup": plan_repo,
                    "loaded_to_dropoff": plan_drop,
                }
            },
            "statemachine": {
                "name": HaulTripStateMachine.__name__,
                "domain": self.simulation_domain,
            },
            "state": HaulTripStateMachine.initial_state.name,
            "sim_clock": sim_clock,
        }
        response = self._post_trip(data)
        if is_success(response.status_code):
            # ADOPT THE POST LOCALLY (perf). This used to be
            # `self.trip = {"_id": ...}; self.refresh()` -- a GET that re-read the
            # document we had just written, once per haul trip (~15k per 1000-truck
            # run) inside `truck.consume_1`, the hottest span in the profile. It is
            # the same redundancy the transition PATCH path already removed.
            #
            # A POST is safe to reconstruct where a PATCH is NOT: Eve stores exactly
            # the payload we sent plus its own meta, so `data` IS the persisted
            # document. (A PATCH cannot be reconstructed this way -- dotted-path
            # payloads and server-side transition hooks make the result differ from
            # what was sent, which is why the transition path adopts the server's
            # echoed body instead of guessing.)
            #
            # `feasible_transitions` is `readonly` with `default: []` in the Eve
            # schema, so the stored value for a fresh document is `[]`.
            body = {}
            try:
                body = response.json() or {}
            except Exception:
                body = {}
            # `_etag` is REQUIRED, not optional: the very next call is `assign()`,
            # whose PATCH sends `self.trip['_etag']` as If-Match. Without it we would
            # KeyError, so fall back to the old GET rather than guess.
            if isinstance(body, dict) and body.get("_id") and body.get("_etag"):
                self.trip = {
                    **data,
                    "feasible_transitions": [],
                    **{k: v for k, v in body.items() if k.startswith("_")},
                }
            else:
                self.trip = {"_id": (body or {}).get("_id")}
                self.refresh()
            return self.trip
        raise WriteFailedException(f"Unable to create haul trip: {response.url}, {response.text}")

    def assign(self, sim_clock, current_loc, order=None, truck=None, assignment_metadata=None):
        o = order or {}
        tp = (truck or {}).get("profile") or {}
        eta_p = tp.get("estimated_time_to_pickup")
        if eta_p is None:
            eta_p = o.get("estimated_time_to_pickup", 0)
        eta_d = tp.get("estimated_time_to_dropoff")
        if eta_d is None:
            eta_d = o.get("estimated_time_to_dropoff", 0)
        eta_p, eta_d = apply_haul_trip_duration_floors(
            eta_p,
            eta_d,
            order=o,
            profile=tp,
        )
        return self.apply_trip_transition_and_notify(
            transition=HaulTripStateMachine.assign.name,
            data={
                "sim_clock": sim_clock,
                "current_loc": current_loc,
                "order": None if order is None else order.get("_id"),
                "stats.estimated_time_to_pickup": 0 if order is None else eta_p,
                "stats.estimated_time_to_dropoff": 0 if order is None else eta_d,
            },
            context={
                "order_id": None if order is None else order.get("_id"),
                "assignment_metadata": assignment_metadata or {},
            },
        )

    def cancel(self, sim_clock, current_loc):
        return self.apply_trip_transition_and_notify(
            transition=HaulTripStateMachine.cancel.name,
            data={"sim_clock": sim_clock, "current_loc": current_loc},
            context={},
        )

    @staticmethod
    def _route_duration_seconds(route):
        if isinstance(route, dict):
            d = route.get("duration")
            if d is not None:
                try:
                    return float(d)
                except (TypeError, ValueError):
                    pass
        return None

    @staticmethod
    def _anchor_time(trip, current_time):
        """
        Anchor for phase timing.

        Many OpenRide resources expose `_updated`, but container-logistics haul trips may not.
        We always patch `sim_clock` on transitions, so treat that as the authoritative
        "phase entered at" time when `_updated` is unavailable.
        """
        if not isinstance(trip, dict):
            return current_time

        for key in ("_updated", "sim_clock"):
            raw = trip.get(key)
            if not raw:
                continue
            try:
                return str_to_time(raw)
            except (TypeError, ValueError):
                logging.debug("TruckTripManager: could not parse trip %s %r", key, raw)

        return current_time

    def estimate_next_event_time(self, current_time):
        """When the simulated truck is likely ready for its next autonomous transition."""
        trip = self.trip
        if trip is None:
            return current_time

        state = trip.get("state")
        stats = trip.get("stats") or {}
        planned = (trip.get("routes") or {}).get("planned") or {}

        anchor = self._anchor_time(trip, current_time)

        def seconds_or_route(stat_key, route_key):
            v = stats.get(stat_key)
            if v is not None:
                try:
                    sec = float(v)
                    if sec > 0:
                        return sec
                except (TypeError, ValueError):
                    pass
            rd = self._route_duration_seconds(planned.get(route_key))
            return float(rd) if rd is not None else 0.0

        try:
            if state == HaulTripStateMachine.repositioning_to_pickup.name:
                dur = seconds_or_route("estimated_time_to_pickup", "repositioning_to_pickup")
            elif state == HaulTripStateMachine.loaded_in_transit.name:
                dur = seconds_or_route("estimated_time_to_dropoff", "loaded_to_dropoff")
            elif state == HaulTripStateMachine.at_pickup_gate.name:
                try:
                    dur = float(stats.get("pickup_service_time") or 0)
                except (TypeError, ValueError):
                    dur = 0.0
            elif state == HaulTripStateMachine.at_dropoff_gate.name:
                try:
                    dur = float(stats.get("dropoff_service_time") or 0)
                except (TypeError, ValueError):
                    dur = 0.0
            else:
                # created, assigned, queued (external coordinator), terminal — step as soon as the clock allows.
                return current_time

            return max(anchor + timedelta(seconds=dur), current_time)
        except Exception:
            logging.debug("TruckTripManager.estimate_next_event_time fallback", exc_info=True)
            return current_time

    def start_empty_reposition(self, sim_clock, current_loc, route=None, estimated_time_to_pickup=0):
        # Symmetric with `finish_pickup_service`: never let a None argument blank the route
        # stored at assignment. This path does not currently regress (its caller reads the
        # route back off the refreshed trip, so repositioning coverage is 100%), but the
        # unguarded write is the same shape as the bug that silently reduced loaded-leg
        # coverage to 5.4%, so close the class rather than the instance.
        if route is None:
            route = ((self.trip or {}).get("routes") or {}).get("planned", {}).get(
                "repositioning_to_pickup"
            )
        return self.apply_trip_transition_and_notify(
            transition=HaulTripStateMachine.start_empty_reposition.name,
            data={
                "sim_clock": sim_clock,
                # PER-LEG start time. `sim_clock` is overwritten by every later transition,
                # so it only ever records the LAST one; without a per-leg stamp the replay
                # reader has to give both legs the same trip-level window, and the leg
                # lookup (which takes the first leg containing the target time) then always
                # picks repositioning — the loaded leg would never render and the truck
                # would crawl the empty leg across the whole trip. Written in the
                # transition that already patches this document: no extra write.
                "stats.reposition_started_at": sim_clock,
                "current_loc": current_loc,
                "routes.planned.repositioning_to_pickup": route,
                "stats.estimated_time_to_pickup": estimated_time_to_pickup,
            },
            context={
                # Slimmed: the full OSRM route rides the MQTT wire on every assignment.
                # Only these three fields are ever read downstream.
                "planned_route": _route_summary(route),
                "estimated_time_to_pickup": estimated_time_to_pickup,
            },
        )

    def arrive_pickup_queue(self, sim_clock, current_loc, queue_arrival_time=None):
        return self.apply_trip_transition_and_notify(
            transition=HaulTripStateMachine.arrive_pickup_queue.name,
            data={
                "sim_clock": sim_clock,
                "current_loc": current_loc,
                "stats.pickup_queue_arrival_time": queue_arrival_time or sim_clock,
            },
            context={
                "location": current_loc,
                "queue_arrival_time": queue_arrival_time or sim_clock,
            },
        )

    def enter_pickup_gate(self, sim_clock, current_loc, gate_index=None, service_time=0):
        return self.apply_trip_transition_and_notify(
            transition=HaulTripStateMachine.enter_pickup_gate.name,
            data={
                "sim_clock": sim_clock,
                "current_loc": current_loc,
                "gate_index": gate_index,
                "stats.pickup_service_time": service_time,
            },
            context={
                "gate_index": gate_index,
                "service_time": service_time,
            },
        )

    def finish_pickup_service(self, sim_clock, current_loc, route_to_dropoff=None, estimated_time_to_dropoff=0, service_time=None):
        trip = self.trip or {}
        stats = trip.get("stats") or {}
        meta = trip.get("meta") or {}
        truck_prof = meta.get("truck_profile") or {}
        order_stub = {
            "pickup_service_time": stats.get("pickup_service_time"),
            "dropoff_service_time": service_time if service_time is not None else stats.get("dropoff_service_time"),
        }
        _, estimated_time_to_dropoff = apply_haul_trip_duration_floors(
            stats.get("estimated_time_to_pickup", 0),
            estimated_time_to_dropoff,
            order=order_stub,
            profile=truck_prof,
        )
        # PRESERVE the route planned at assignment. This transition is driven by the
        # facility's gate-service-completed message, which has no idea what the truck's
        # route is — so `route_to_dropoff` arrives as None on essentially every trip, and
        # writing that through blanked `routes.planned.loaded_to_dropoff`, which
        # `create_new_trip` had already filled in at assignment. Measured before this fix:
        # only 193 of 3,601 loaded legs still had geometry (5.4%) while repositioning legs
        # were at 100%. Only overwrite when the caller genuinely supplies a route.
        if route_to_dropoff is None:
            route_to_dropoff = ((trip.get("routes") or {}).get("planned") or {}).get(
                "loaded_to_dropoff"
            )
        route_to_dropoff = patch_route_duration(route_to_dropoff, estimated_time_to_dropoff)
        return self.apply_trip_transition_and_notify(
            transition=HaulTripStateMachine.finish_pickup_service.name,
            data={
                "sim_clock": sim_clock,
                # PER-LEG start time for the loaded leg — see `start_empty_reposition`.
                "stats.loaded_started_at": sim_clock,
                "current_loc": current_loc,
                "routes.planned.loaded_to_dropoff": route_to_dropoff,
                "stats.estimated_time_to_dropoff": estimated_time_to_dropoff,
                "stats.pickup_service_time": service_time,
            },
            context={
                "location": current_loc,
                "planned_route": _route_summary(route_to_dropoff),
                "estimated_time_to_dropoff": estimated_time_to_dropoff,
                "service_time": service_time,
            },
        )

    def arrive_dropoff_queue(self, sim_clock, current_loc, queue_arrival_time=None):
        return self.apply_trip_transition_and_notify(
            transition=HaulTripStateMachine.arrive_dropoff_queue.name,
            data={
                "sim_clock": sim_clock,
                "current_loc": current_loc,
                "stats.dropoff_queue_arrival_time": queue_arrival_time or sim_clock,
            },
            context={
                "location": current_loc,
                "queue_arrival_time": queue_arrival_time or sim_clock,
            },
        )

    def enter_dropoff_gate(self, sim_clock, current_loc, gate_index=None, service_time=0):
        return self.apply_trip_transition_and_notify(
            transition=HaulTripStateMachine.enter_dropoff_gate.name,
            data={
                "sim_clock": sim_clock,
                "current_loc": current_loc,
                "gate_index": gate_index,
                "stats.dropoff_service_time": service_time,
            },
            context={
                "gate_index": gate_index,
                "service_time": service_time,
            },
        )

    def finish_dropoff_service(self, sim_clock, current_loc, service_time=None):
        return self.apply_trip_transition_and_notify(
            transition=HaulTripStateMachine.finish_dropoff_service.name,
            data={
                "sim_clock": sim_clock,
                "current_loc": current_loc,
                "stats.dropoff_service_time": service_time,
            },
            context={
                "location": current_loc,
                "service_time": service_time,
            },
        )
