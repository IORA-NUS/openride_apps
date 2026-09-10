from apps.common.resource_client_mixin import ResourceClientMixin
from apps.config import simulation_domains


class TruckIdleTripManager(ResourceClientMixin):
    """
    Persisted idle trip record for a truck.

    Important: this is NOT the haul-trip endpoint (role is different), so it will
    not interfere with assignment logic that checks for an active haul trip.
    """

    def __init__(self, run_id, sim_clock, user, current_loc, persona=None):
        self.run_id = run_id
        self.user = user
        self.simulation_domain = simulation_domains.get("container_logistics", "container-logistics-sim")
        # Ensure we never inherit a conflicting `role` from the caller persona.
        # ResourceClientMixin routes purely off persona.role.
        base_persona = dict(persona or {})
        base_persona.pop("role", None)
        self.persona = {**base_persona, "role": "truck_idle_trip"}
        self.resource = None

        # DEFERRED CREATE (perf). The only consumer of this resource is
        # `analytics/manager.py::accumulate_idle_trips`, which queries
        # `state == "ended"` with `projection={start_sim_clock, sim_clock}` to feed
        # `compute_avg_idle_time_seconds`. It never reads an in-progress `idle` doc,
        # and nothing in the frontend reads this collection at all -- truck positions
        # come from the `truck_loc` Kafka stream, not from here.
        #
        # So POSTing at idle START and PATCHing at idle END costs TWO blocking round
        # trips inside agent ticks per idle period, ~31k per 1000-truck run, to
        # persist two timestamps. We now hold the open period in memory and write it
        # ONCE, at `end()`, as a complete `ended` document. Same KPI, half the calls.
        #
        # An agent that dies mid-idle writes nothing, where it used to leave an
        # `idle` doc -- which `accumulate_idle_trips` already ignored, so no
        # measurement changes.
        self._open_start_sim_clock = sim_clock
        self._open_start_loc = current_loc
        self._open_current_loc = current_loc

    def as_dict(self):
        return self.resource

    def create_new(self, sim_clock, current_loc):
        data = {
            "persona": self.persona,
            "kind": "idle",
            "state": "idle",
            "sim_clock": sim_clock,
            "start_sim_clock": sim_clock,
            "current_loc": current_loc,
            "next_dest_loc": current_loc,
            "routes": {"planned": {}, "actual": {}},
        }
        self.resource = self.resource_post(data=data)
        return self.resource

    def refresh(self):
        if self.resource is None:
            return None
        self.resource = self.resource_get(resource_id=self.resource.get("_id"))
        return self.resource

    def ping(self, sim_clock, current_loc):
        """Track the latest stationary position in memory.

        Previously this PATCHed on a location change. While a truck is idle it does
        not move, so that PATCH almost never fired -- and the position it recorded was
        read by nobody (see the deferred-create note in __init__). Keep the latest
        values so `end()` can write an accurate final document.
        """
        if self.resource is not None:
            return self.resource
        self._open_current_loc = current_loc
        return None

    def end(self, sim_clock, current_loc):
        """Write the whole idle period as one `ended` document.

        `start_sim_clock` is the stamp captured when the period opened, so the
        duration `accumulate_idle_trips` derives (`sim_clock - start_sim_clock`) is
        byte-identical to what the old POST-then-PATCH pair produced.
        """
        if self.resource is not None:
            # A document already exists (legacy path, or end() called twice).
            # Close it the old way rather than creating a duplicate period.
            data = {
                "sim_clock": sim_clock,
                "state": "ended",
                "end_sim_clock": sim_clock,
                "end_loc": current_loc,
                "current_loc": current_loc,
                "next_dest_loc": current_loc,
            }
            self.resource = self.resource_patch(
                resource_id=self.resource.get("_id"),
                data=data,
                etag=self.resource.get("_etag"),
            )
            return self.resource
        if self._open_start_sim_clock is None:
            return None
        start_loc = self._open_start_loc
        data = {
            "persona": self.persona,
            "kind": "idle",
            "state": "ended",
            "sim_clock": sim_clock,
            "start_sim_clock": self._open_start_sim_clock,
            "end_sim_clock": sim_clock,
            "start_loc": start_loc,
            "end_loc": current_loc,
            "current_loc": current_loc,
            "next_dest_loc": current_loc,
            "routes": {"planned": {}, "actual": {}},
        }
        self._open_start_sim_clock = None
        self.resource = self.resource_post(data=data)
        return self.resource

