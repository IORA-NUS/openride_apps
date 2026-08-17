"""TruckAgent and its policy tree (Random / Historical parents).

For trucks the sampled fields are the **origin** (`init_loc`) and the **home facility**.
`HistoricalTruckPolicy` = today's default (origin from real addresses via the catalog,
home = uniform over facilities) → byte-identical to the old `TruckBuilder`.
`RandomTruckPolicy` widens the origin to any real address uniformly (Q2).
"""

from __future__ import annotations

from apps.container_logistics.duration_constants import (
    MAX_LEG_DROPOFF_SECONDS,
    MAX_LEG_PICKUP_SECONDS,
    MIN_LEG_DROPOFF_SECONDS,
    MIN_LEG_PICKUP_SECONDS,
)
from apps.container_logistics.haul_trip_duration import apply_haul_trip_duration_floors as _floors

from ..builders import geojson_point
from ..distributions.base import Distribution
from .base import Agent, Policy

_TRUCK_PROFILE_GENERATION_KEYS = {
    "min_haul_trip_seconds", "min_estimated_time_to_pickup", "max_estimated_time_to_pickup",
    "min_estimated_time_to_dropoff", "max_estimated_time_to_dropoff",
    "default_shift_start_seconds", "default_shift_end_seconds", "use_osrm_at_assignment",
}


# ---- distributions the truck policies build ----

class _CatalogOrigin(Distribution):
    """A real start point sampled from the catalog by the given codes (None = any)."""

    def __init__(self, catalog, codes):
        self.catalog = catalog
        self.codes = codes

    def sample(self, rng):
        lon, lat = self.catalog.sample_origin(self.codes, rng=rng)
        return geojson_point(lon, lat)


class _ChoiceHome(Distribution):
    """Uniform choice over the facility list (== rng.choice(facilities))."""

    def __init__(self, facilities):
        self.facilities = facilities

    def sample(self, rng):
        return rng.choice(self.facilities)


# ---- the truck policies ----

class TruckPolicy(Policy):
    role = "truck"

    def home_dist(self) -> Distribution:
        return _ChoiceHome(self.ctx.facilities)

    def origin_dist(self) -> Distribution:
        raise NotImplementedError


class HistoricalTruckPolicy(TruckPolicy):
    """Today's default: origin from the configured truck-origin codes."""

    name = "default"

    def origin_dist(self) -> Distribution:
        return _CatalogOrigin(self.ctx.catalog, self.ctx.spec.truck_origin_codes)


class RandomTruckPolicy(TruckPolicy):
    """Random data: origin uniform over ALL real addresses (Q2)."""

    name = "random"

    def origin_dist(self) -> Distribution:
        return _CatalogOrigin(self.ctx.catalog, None)


# ---- the truck agent ----

class TruckAgent(Agent):
    role = "truck"

    def _shift_bounds(self, profile_cfg):
        spec = self.spec
        simulation_end = spec.simulation_end_step
        interval = spec.step_interval_seconds
        default_end_seconds = spec.simulation_days * 24 * 3600
        end_seconds = profile_cfg.get("default_shift_end_seconds")
        if end_seconds is None:
            end_seconds = default_end_seconds
        start_seconds = profile_cfg.get("default_shift_start_seconds", 0)
        return (min(int(start_seconds // interval), simulation_end),
                min(int(end_seconds // interval), simulation_end))

    def generate(self, n: int) -> dict:
        spec = self.spec
        truck_cfg = spec.truck_settings
        profile_cfg = truck_cfg.get("profile", {})
        home_dist = self.policy.home_dist()
        origin_dist = self.policy.origin_dist()

        out = {}
        for i in range(max(1, int(n))):
            agent_id = f"truck_{i:06d}"
            # SAME draw order as the old TruckBuilder: home, origin, eta_pickup, eta_dropoff.
            home_facility = home_dist.sample(self.rng)
            shift_start_time, shift_end_time = self._shift_bounds(profile_cfg)
            init_loc = origin_dist.sample(self.rng)
            eta_pickup = self.rng.randint(
                profile_cfg.get("min_estimated_time_to_pickup", MIN_LEG_PICKUP_SECONDS),
                profile_cfg.get("max_estimated_time_to_pickup", MAX_LEG_PICKUP_SECONDS),
            )
            eta_dropoff = self.rng.randint(
                profile_cfg.get("min_estimated_time_to_dropoff", MIN_LEG_DROPOFF_SECONDS),
                profile_cfg.get("max_estimated_time_to_dropoff", MAX_LEG_DROPOFF_SECONDS),
            )
            base_profile = {k: v for k, v in profile_cfg.items() if k not in _TRUCK_PROFILE_GENERATION_KEYS}
            eta_pickup, eta_dropoff = _floors(eta_pickup, eta_dropoff, profile=profile_cfg)
            haulier = self.haulier_for(i) or self._default_haulier()

            out[agent_id] = {
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
        return out
