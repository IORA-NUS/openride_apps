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

        # Create an initial idle-trip resource immediately.
        self.create_new(sim_clock=sim_clock, current_loc=current_loc)

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
        if self.resource is None:
            return self.create_new(sim_clock=sim_clock, current_loc=current_loc)
        data = {
            "sim_clock": sim_clock,
            "current_loc": current_loc,
            "next_dest_loc": current_loc,
        }
        self.resource_patch(resource_id=self.resource.get("_id"), data=data, etag=self.resource.get("_etag"))
        self.refresh()
        return self.resource

    def end(self, sim_clock, current_loc):
        if self.resource is None:
            return None
        data = {
            "sim_clock": sim_clock,
            "state": "ended",
            "end_sim_clock": sim_clock,
            "end_loc": current_loc,
            "current_loc": current_loc,
            "next_dest_loc": current_loc,
        }
        self.resource_patch(resource_id=self.resource.get("_id"), data=data, etag=self.resource.get("_etag"))
        self.refresh()
        return self.resource

