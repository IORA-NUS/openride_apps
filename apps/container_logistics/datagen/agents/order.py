"""OrderAgent and its policy tree.

**Two parents, by data source** (the matrix/curve are *distributions* the policy samples,
NOT policies of their own):
  * ``RandomOrderPolicy``     — random data (uniform OD + uniform time)
  * ``HistoricalOrderPolicy`` — given/real data → samples a ``ProbabilityMatrix`` (+ ``Curve``).
        The matrix/curve come from an **authored** trip matrix (the default) **or are learned
        from a records file** when ``source`` is given. This single given-data policy reproduces
        today's default order generation byte-for-byte for the authored case (its distributions
        wrap the exact sampling functions the old ``OrderBuilder`` used).
"""

from __future__ import annotations

from ..builders import geojson_point, order_facility_view
from ..demand import hourly_weights_from_spec
from ..distributions import Curve, ProbabilityMatrix
from ..distributions.base import Distribution
from .base import Agent, Policy


# ---- a uniform arrival-time distribution (the old builder's uniform fallback) ----

class UniformTime(Distribution):
    def __init__(self, spec, business_hour_start=0, business_hour_end=24):
        self.spec = spec
        self.bh_start = int(business_hour_start)
        self.bh_end = int(business_hour_end)

    def sample(self, rng):
        return self.sample_step(rng)

    def sample_step(self, rng, **_ignore):
        interval = self.spec.step_interval_seconds
        steps_per_day = (24 * 3600) // interval
        bh_start = max(0, min(23, self.bh_start))
        bh_end = max(bh_start + 1, min(24, self.bh_end))
        day = rng.randint(0, max(0, self.spec.simulation_days - 1))
        hour = rng.randint(bh_start, bh_end - 1)
        minute = rng.randint(0, 59)
        step = day * steps_per_day + (hour * 3600 + minute * 60) // interval
        return min(max(0, step), self.spec.simulation_end_step)


# ---- the order policies (owned by OrderAgent) ----

class OrderPolicy(Policy):
    role = "order"

    def _bh(self):
        o = self.ctx.spec.order_settings
        return (int(o.get("business_hour_start", self.ctx.spec.business_hour_start)),
                int(o.get("business_hour_end", self.ctx.spec.business_hour_end)))

    # A grid (marginals source) for demand-proportional facility allocation.
    def location_grid(self) -> dict:
        raise NotImplementedError

    def location_dist(self) -> Distribution:
        return ProbabilityMatrix(self.location_grid())

    def time_dist(self) -> Distribution:
        raise NotImplementedError


class RandomOrderPolicy(OrderPolicy):
    name = "random"

    def location_grid(self) -> dict:
        codes = self.ctx.codes()
        grid = {p: {d: (0.0 if p == d else 1.0) for d in codes} for p in codes}
        total = sum(v for row in grid.values() for v in row.values()) or 1.0
        return {p: {d: v / total for d, v in row.items()} for p, row in grid.items()}

    def time_dist(self) -> Distribution:
        bh_start, bh_end = self._bh()
        return UniformTime(self.ctx.spec, bh_start, bh_end)


class HistoricalOrderPolicy(OrderPolicy):
    """Given/real data → samples a ``ProbabilityMatrix`` (+ ``Curve``).

    The distributions come from an **authored** trip matrix / demand curve (the default,
    byte-identical to the old ``OrderBuilder``), or are **learned from a records file** when
    ``source`` is supplied (matrix via ``from_records``; arrival curve via ``from_records``
    when the records carry timestamps, else the authored/default curve).
    """

    name = "historical"

    def _records(self) -> list:
        return self.params.get("records") or []

    def location_grid(self) -> dict:
        records = self._records()
        if records:
            return ProbabilityMatrix.from_records(records).restrict(self.ctx.codes()).as_grid()
        # Authored/default: the Preprocessor already resolved the grid onto the spec
        # (identical to the grid used for demand-proportional facility allocation).
        return self.ctx.spec.trip_matrix

    def time_dist(self) -> Distribution:
        bh_start, bh_end = self._bh()
        records = self._records()
        if Curve.records_have_hours(records):
            return Curve.from_records(records, business_hour_start=bh_start, business_hour_end=bh_end)
        weights = (self.ctx.spec.order_settings.get("order_demand_weights")
                   or self.ctx.spec.hourly_weights
                   or hourly_weights_from_spec(self.params.get("curve")))
        return Curve.from_hourly_weights(weights, business_hour_start=bh_start, business_hour_end=bh_end)


# ---- the order agent ----

class OrderAgent(Agent):
    role = "order"

    def generate(self, n: int) -> dict:
        spec = self.spec
        order_cfg = spec.order_settings
        profile_cfg = order_cfg.get("profile", {})
        loc = self.policy.location_dist()
        time = self.policy.time_dist()
        by_code: dict[str, list[dict]] = {}
        for fac in self.facilities:
            by_code.setdefault(fac.get("code"), []).append(fac)

        def pick(code, exclude=None):
            pool = by_code.get(code) or self.facilities
            if exclude is not None and len(pool) > 1:
                pool = [f for f in pool if f is not exclude] or pool
            return self.rng.choice(pool)

        out = {}
        for i in range(max(1, int(n))):
            agent_id = f"order_{i:06d}"
            # SAME draw order as the old OrderBuilder: time, OD, snap pickup, snap dropoff.
            request_time = time.sample_step(
                self.rng,
                simulation_end=spec.simulation_end_step,
                simulation_days=spec.simulation_days,
                step_interval_seconds=spec.step_interval_seconds,
            )
            pickup_code, dropoff_code = loc.sample(self.rng)
            pickup_facility = pick(pickup_code)
            dropoff_facility = pick(dropoff_code, exclude=pickup_facility)

            pickup_code = pickup_facility.get("code") or pickup_code
            dropoff_code = dropoff_facility.get("code") or dropoff_code
            pickup_location_type = pickup_facility["facility_type"]
            dropoff_location_type = dropoff_facility["facility_type"]
            pickup_loc = geojson_point(pickup_facility["lon"], pickup_facility["lat"])
            dropoff_loc = geojson_point(dropoff_facility["lon"], dropoff_facility["lat"])
            pickup_service_time = pickup_facility["service_time"]
            dropoff_service_time = dropoff_facility["service_time"]
            haulier = self.haulier_for(i) or self._default_haulier()

            out[agent_id] = {
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
        return out
