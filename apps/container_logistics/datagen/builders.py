"""The "generate" half: turn a :class:`GenerationSpec` + sampled locations into
agent behavior dicts.

Each builder is a small, single-responsibility class with no I/O and no global
state. The emitted dicts are field-for-field compatible with the previous
``GenerateBehavior`` output (verified against the live behavior schema) so the
sim/agents are unaffected — only *where* things are placed changes (real
addresses, and code/type/coordinate coherence for orders).
"""

from __future__ import annotations

import random
from typing import Any, Optional

# Pure, side-effect-free leaf utilities (no scenario/global coupling, no back-edge
# to this package) — reused rather than duplicated to stay single-source-of-truth.
from apps.container_logistics.duration_constants import (
    MAX_LEG_DROPOFF_SECONDS,
    MAX_LEG_PICKUP_SECONDS,
    MIN_LEG_DROPOFF_SECONDS,
    MIN_LEG_PICKUP_SECONDS,
)
from apps.container_logistics.haul_trip_duration import apply_haul_trip_duration_floors

from .catalog import LocationCatalog
from .sampling import sample_request_time_step
from .spec import GenerationSpec
from .trip_matrix import sample_pickup_delivery_codes

# Every facility handed to datagen must carry these data-generated fields. There
# are NO defaults and NO fallbacks — facilities come from
# ``LocationCatalog.facility_sites(...)`` and are handed in via the spec.
_REQUIRED_FACILITY_FIELDS = (
    "name", "lat", "lon", "code", "facility_type", "gate_count", "service_time",
)

# Truck-profile keys consumed only during generation — stripped from the emitted
# base profile (matches the previous generator exactly).
_TRUCK_PROFILE_GENERATION_KEYS = {
    "min_haul_trip_seconds",
    "min_estimated_time_to_pickup",
    "max_estimated_time_to_pickup",
    "min_estimated_time_to_dropoff",
    "max_estimated_time_to_dropoff",
    "default_shift_start_seconds",
    "default_shift_end_seconds",
    "use_osrm_at_assignment",
}


def geojson_point(lon: float, lat: float) -> dict:
    """A GeoJSON Point, JSON-identical to shapely ``mapping(Point(lon, lat))``."""
    return {"type": "Point", "coordinates": [lon, lat]}


#: Site keys an ORDER's embedded facility snapshot must NOT carry.
_ORDER_FACILITY_EXCLUDED = ("rebate",)


def order_facility_view(site: dict) -> dict:
    """The facility snapshot embedded in an order behavior — **provenance only.**

    **This snapshot has no runtime reader and never reaches Mongo.** The order
    document is built from ``behavior["profile"]`` alone (``precreate.py`` in service
    mode, ``order/manager.py`` fed by ``order/app.py`` in agents mode); this key sits
    one level up, on the behavior, and is never copied down. Every key in it is dead
    *here*, ``gate_count`` and ``service_time`` included.

    The exclusion rule is therefore **bulk, not readership**: a 24-point rebate
    schedule copied into every one of 5,000 orders is real bundle bloat, while two
    integers are not. It also keeps a rule that only PRICES a facility from rewriting
    the whole ORDER collection, which a facility-collection equivalence proof cannot
    see.

    **Corrected (F5/F12).** This docstring previously justified keeping
    ``gate_count``/``service_time`` as *"world physics, so a rule that changes them is
    supposed to be visible here"* — which asserts a consumer that does not exist, and
    is exactly the P11 shape the feature's own allow-list was written to avoid. It
    also made the cut look backwards, because it implied the kept keys were live.

    The live ``service_time`` is a **different field one level up**:
    ``profile.pickup_service_time`` / ``profile.dropoff_service_time``, flattened from
    the same site a few lines below. Those DO reach Mongo and have three hot readers —
    the assignment payload, the haul-duration floor, and the trip-stats seed. See
    ``test_service_time_rule_rewrites_only_matched_orders_profile_service_time``.
    """
    return {k: v for k, v in site.items() if k not in _ORDER_FACILITY_EXCLUDED}


def resolve_facilities(facility_settings: dict) -> list[dict]:
    """Validate and return the facility site list.

    Facilities MUST be generated from the real address data (via
    ``LocationCatalog.facility_sites``) and handed in through
    ``facility_settings['profile']['facilities']``. There are no default facility
    centers and no fallbacks: a missing/empty list — or any facility lacking a
    required data-generated field — raises ``ValueError`` and stops generation.
    """
    profile_cfg = (facility_settings or {}).get("profile") or {}
    facilities = profile_cfg.get("facilities")
    if not facilities:
        raise ValueError(
            "datagen requires facilities generated from the address data; none were "
            "provided in facility_settings['profile']['facilities']. Build them with "
            "LocationCatalog.facility_sites(n, registry, ...) and hand them to the spec."
        )
    resolved = []
    for idx, item in enumerate(facilities):
        if not isinstance(item, dict):
            raise ValueError(f"facility[{idx}] must be a dict, got {item!r}")
        missing = [f for f in _REQUIRED_FACILITY_FIELDS if item.get(f) is None]
        if missing:
            raise ValueError(
                f"facility[{idx}] (name={item.get('name')!r}) is missing required "
                f"data-generated field(s) {missing}; facilities must come from "
                f"LocationCatalog.facility_sites(...). Got keys: {sorted(item)}"
            )
        entry = {f: item[f] for f in _REQUIRED_FACILITY_FIELDS}
        # Real footprint polygon ("mask"), when the generator matched one.
        if item.get("footprint") is not None:
            entry["footprint"] = item["footprint"]
        # This facility's resolved rebate schedule, stamped onto the site by the
        # Preprocessor's facilityRules step. An OPT-IN passthrough, exactly like
        # ``footprint`` above: this projection is a whitelist, so a site key that is
        # not named here is silently dropped — which is how a resolved schedule would
        # vanish between the Preprocessor and the builders with nothing to show for it.
        if item.get("rebate") is not None:
            entry["rebate"] = item["rebate"]
        resolved.append(entry)
    return resolved


class _BaseBuilder:
    def __init__(self, spec: GenerationSpec, catalog: LocationCatalog, rng=None):
        self.spec = spec
        self.catalog = catalog
        self.rng = rng or random
        self.facilities = resolve_facilities(spec.facility_settings)

    @property
    def domain(self):
        return self.spec.domain

    def _default_haulier(self):
        hauliers = self.spec.truck_hauliers or self.spec.order_hauliers
        if hauliers:
            return hauliers[0]
        return {"id": "haulier", "name": "Haulier"}


class TruckBuilder(_BaseBuilder):
    def _shift_bounds_steps(self, profile_cfg):
        simulation_end = self.spec.simulation_end_step
        interval = self.spec.step_interval_seconds
        default_end_seconds = self.spec.simulation_days * 24 * 3600
        end_seconds = profile_cfg.get("default_shift_end_seconds")
        if end_seconds is None:
            end_seconds = default_end_seconds
        start_seconds = profile_cfg.get("default_shift_start_seconds", 0)
        shift_start = min(int(start_seconds // interval), simulation_end)
        shift_end = min(int(end_seconds // interval), simulation_end)
        return shift_start, shift_end

    def _init_location(self):
        """Real on-land start point, sampled from the configured truck-origin codes
        (default: any code with real addresses) — no hardcoded origin type."""
        lon, lat = self.catalog.sample_origin(self.spec.truck_origin_codes, rng=self.rng)
        return geojson_point(lon, lat)

    def build(self, agent_id: str, haulier: Optional[dict] = None) -> dict:
        truck_cfg = self.spec.truck_settings
        profile_cfg = truck_cfg.get("profile", {})

        home_facility = self.rng.choice(self.facilities)
        shift_start_time, shift_end_time = self._shift_bounds_steps(profile_cfg)
        init_loc = self._init_location()

        eta_pickup = self.rng.randint(
            profile_cfg.get("min_estimated_time_to_pickup", MIN_LEG_PICKUP_SECONDS),
            profile_cfg.get("max_estimated_time_to_pickup", MAX_LEG_PICKUP_SECONDS),
        )
        eta_dropoff = self.rng.randint(
            profile_cfg.get("min_estimated_time_to_dropoff", MIN_LEG_DROPOFF_SECONDS),
            profile_cfg.get("max_estimated_time_to_dropoff", MAX_LEG_DROPOFF_SECONDS),
        )

        base_profile = {
            k: v for k, v in profile_cfg.items() if k not in _TRUCK_PROFILE_GENERATION_KEYS
        }
        eta_pickup, eta_dropoff = apply_haul_trip_duration_floors(
            eta_pickup, eta_dropoff, profile=profile_cfg
        )

        haulier = haulier or self._default_haulier()

        return {
            "email": f"{agent_id}@test.com",
            "password": "password",
            "persona": {"role": "truck", "domain": self.domain},
            "steps_per_action": truck_cfg.get("steps_per_action", 1),
            "response_rate": truck_cfg.get("response_rate", 1.0),
            "step_only_on_events": truck_cfg.get("step_only_on_events", True),
            "shift_start_time": shift_start_time,
            "shift_end_time": shift_end_time,
            "init_loc": init_loc,
            "profile": {
                **base_profile,
                "home_facility_name": home_facility.get("name"),
                "truck_size": profile_cfg.get("truck_size", "20ft"),
                "haulier_id": haulier.get("id"),
                "haulier_name": haulier.get("name"),
                "restricted_areas": profile_cfg.get("restricted_areas", ["West Coast", "MBS"]),
                "planned_reposition_route": None,
                "planned_dropoff_route": None,
                "estimated_time_to_pickup": eta_pickup,
                "estimated_time_to_dropoff": eta_dropoff,
                "use_osrm_at_assignment": profile_cfg.get("use_osrm_at_assignment", False),
            },
        }


class OrderBuilder(_BaseBuilder):
    def __init__(self, spec, catalog, rng=None):
        super().__init__(spec, catalog, rng)
        # Group facilities by location code for coherent leg snapping.
        self._by_code: dict[str, list[dict]] = {}
        for fac in self.facilities:
            self._by_code.setdefault(fac.get("code"), []).append(fac)

    def _request_time_step(self) -> int:
        order_cfg = self.spec.order_settings
        simulation_end = self.spec.simulation_end_step
        bh_start = int(order_cfg.get("business_hour_start", self.spec.business_hour_start))
        bh_end = int(order_cfg.get("business_hour_end", self.spec.business_hour_end))
        weights = order_cfg.get("order_demand_weights") or self.spec.hourly_weights
        if weights:
            return sample_request_time_step(
                weights,
                simulation_end=simulation_end,
                simulation_days=self.spec.simulation_days,
                step_interval_seconds=self.spec.step_interval_seconds,
                business_hour_start=bh_start,
                business_hour_end=bh_end,
                rng=self.rng if isinstance(self.rng, random.Random) else None,
            )
        # Uniform fallback over business hours.
        interval = self.spec.step_interval_seconds
        steps_per_day = (24 * 3600) // interval
        bh_end = max(bh_start + 1, min(24, bh_end))
        day = self.rng.randint(0, max(0, self.spec.simulation_days - 1))
        hour = self.rng.randint(bh_start, bh_end - 1)
        minute = self.rng.randint(0, 59)
        step = day * steps_per_day + (hour * 3600 + minute * 60) // interval
        return min(max(0, step), simulation_end)

    def _pick_facility(self, code: str, *, exclude: Optional[dict] = None) -> dict:
        pool = self._by_code.get(code) or self.facilities
        if exclude is not None and len(pool) > 1:
            pool = [f for f in pool if f is not exclude] or pool
        return self.rng.choice(pool)

    def build(self, agent_id: str, haulier: Optional[dict] = None) -> dict:
        order_cfg = self.spec.order_settings
        profile_cfg = order_cfg.get("profile", {})

        request_time = self._request_time_step()
        pickup_code, dropoff_code = sample_pickup_delivery_codes(
            rng=self.rng, matrix=self.spec.trip_matrix or order_cfg.get("trip_matrix")
        )
        # Coherence: snap each leg to a facility of the sampled type. The realized
        # code/type then *follow the chosen facility*, so code <-> type <-> facility
        # <-> coordinate always agree — even when a sampled code has no facility (a
        # tiny-scenario fallback) the order honestly reflects where it actually goes.
        pickup_facility = self._pick_facility(pickup_code)
        dropoff_facility = self._pick_facility(dropoff_code, exclude=pickup_facility)

        pickup_code = pickup_facility.get("code") or pickup_code
        dropoff_code = dropoff_facility.get("code") or dropoff_code
        pickup_location_type = pickup_facility["facility_type"]
        dropoff_location_type = dropoff_facility["facility_type"]
        pickup_loc = geojson_point(pickup_facility["lon"], pickup_facility["lat"])
        dropoff_loc = geojson_point(dropoff_facility["lon"], dropoff_facility["lat"])

        pickup_service_time = pickup_facility["service_time"]
        dropoff_service_time = dropoff_facility["service_time"]

        haulier = haulier or self._default_haulier()

        return {
            "email": f"{agent_id}@test.com",
            "password": "password",
            "persona": {"role": "order", "domain": self.domain},
            "steps_per_action": order_cfg.get("steps_per_action", 1),
            "dormant_steps_per_action": order_cfg.get("dormant_steps_per_action", 48),
            "response_rate": order_cfg.get("response_rate", 1.0),
            "step_only_on_events": order_cfg.get("step_only_on_events", True),
            "request_time_step": request_time,
            "pickup_code": pickup_code,
            "delivery_code": dropoff_code,
            "pickup_loc": pickup_loc,
            "dropoff_loc": dropoff_loc,
            "pickup_facility": order_facility_view(pickup_facility),
            "dropoff_facility": order_facility_view(dropoff_facility),
            "pickup_service_time": pickup_service_time,
            "dropoff_service_time": dropoff_service_time,
            "order_size": profile_cfg.get("order_size", "1x20"),
            "haulier_id": haulier.get("id"),
            "haulier_name": haulier.get("name"),
            "order_type": profile_cfg.get("order_type", "import"),
            "vessel_number": profile_cfg.get("vessel_number", "Vessel123"),
            "shipping_line": profile_cfg.get("shipping_line", "Shipping Line"),
            "order_owner": profile_cfg.get("order_owner", ""),
            "pickup_location_type": pickup_location_type,
            "dropoff_location_type": dropoff_location_type,
            "container_status": profile_cfg.get("container_status", "Empty"),
            "profile": {
                **profile_cfg,
                "haulier_id": haulier.get("id"),
                "haulier_name": haulier.get("name"),
                "pickup_facility_name": pickup_facility.get("name"),
                "dropoff_facility_name": dropoff_facility.get("name"),
                "pickup_loc": pickup_loc,
                "dropoff_loc": dropoff_loc,
                "pickup_service_time": pickup_service_time,
                "dropoff_service_time": dropoff_service_time,
            },
        }


class FacilityBuilder(_BaseBuilder):
    def build(self, agent_id: str, facility_index: int = 0) -> dict:
        profile_cfg = self.spec.facility_settings.get("profile", {})
        facility = self.facilities[facility_index % len(self.facilities)]

        gate_count = facility["gate_count"]
        service_time = facility["service_time"]
        facility_type = facility["facility_type"]

        # Don't re-embed the whole site list in every facility's stored profile —
        # it's config bloat (nothing reads profile.facilities at runtime) and with
        # per-facility footprints it would duplicate every mask N×N.
        # ``rebate`` is stripped here and re-read from the SITE below: the
        # scenario-wide ``overrides.facility.rebate`` is rank 0 and has already been
        # folded into the site's resolved value, so letting it ride the blanket merge
        # would let it shadow a rule that deliberately overrode it (including a rule
        # that set it to null).
        profile_cfg_clean = {
            k: v for k, v in profile_cfg.items() if k not in ("facilities", "rebate")
        }

        profile = {
            **profile_cfg_clean,
            "name": facility.get("name"),
            "facility_type": facility_type,
            "location": geojson_point(facility["lon"], facility["lat"]),
            "gate_count": gate_count,
            "max_queue_size": profile_cfg.get("max_queue_size", None),
            "status": profile_cfg.get("status", "Open"),
            "operating_hours": profile_cfg.get("operating_hours", "24/7"),
            "operating_days": profile_cfg.get("operating_days", "7 days a week"),
            "service_time": service_time,
        }
        # This facility's own real footprint polygon ("mask"), when matched.
        if facility.get("footprint") is not None:
            profile["footprint"] = facility.get("footprint")
        # THIS facility's resolved schedule, read off the SITE (facility rules plan
        # §8.1). Both builders read the same site rather than sharing a resolver
        # function, which removes the divergence class instead of mitigating it: a
        # patch to one builder can no longer leave the other emitting rebate-less
        # facilities — the hardest class of this bug to diagnose.
        resolved_rebate = facility.get("rebate")
        if resolved_rebate is not None:
            profile["rebate"] = resolved_rebate
        else:
            profile.pop("rebate", None)

        return {
            "email": f"{agent_id}@test.com",
            "password": "password",
            "persona": {"role": "facility", "domain": self.domain},
            "steps_per_action": self.spec.facility_settings.get("steps_per_action", 1),
            "response_rate": self.spec.facility_settings.get("response_rate", 1.0),
            "step_only_on_events": self.spec.facility_settings.get("step_only_on_events", True),
            "gate_count": gate_count,
            "service_time": service_time,
            "profile": profile,
        }


class AssignmentBuilder(_BaseBuilder):
    def build(self, agent_id: str) -> dict:
        cfg = self.spec.assignment_settings
        return {
            "email": f"{agent_id}@test.com",
            "password": "password",
            "persona": {"role": "engine", "domain": self.domain},
            "steps_per_action": cfg.get("steps_per_action", 1),
            "response_rate": cfg.get("response_rate", 1.0),
            "step_only_on_events": cfg.get("step_only_on_events", False),
            "profile": cfg.get("profile", {}),
        }


class AnalyticsBuilder(_BaseBuilder):
    def build(self, agent_id: str) -> dict:
        cfg = self.spec.analytics_settings
        return {
            "email": f"{agent_id}@test.com",
            "password": "password",
            "persona": {"role": "analytics", "domain": self.domain},
            "steps_per_action": cfg.get("steps_per_action", 1),
            "response_rate": cfg.get("response_rate", 1.0),
            "step_only_on_events": cfg.get("step_only_on_events", False),
            "profile": cfg.get("profile", {}),
        }
