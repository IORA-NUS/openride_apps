import logging

from apps.common.resource_client_mixin import ResourceClientMixin
from apps.config import simulation_domains
from orsim.lifecycle import ORSimManager
from orsim.utils import WorkflowStateMachine


class TruckManager(ResourceClientMixin, ORSimManager):
    def __init__(self, run_id, sim_clock, user, profile=None, persona=None):
        self.run_id = run_id
        self.user = user
        self.profile = profile or {}
        self.persona = {"role": "truck", **(persona or {})}
        self.simulation_domain = simulation_domains.get("container_logistics", "container-logistics-sim")

        data = {
            "profile": self.profile,
            "persona": self.persona,
            "statemachine": {
                "name": "WorkflowStateMachine",
                "domain": self.simulation_domain,
            },
            "state": WorkflowStateMachine().initial_state.name,
            "sim_clock": sim_clock,
        }
        self.resource = self.init_resource(sim_clock, data=data)

    def on_init(self):
        pass

    def as_dict(self):
        return self.resource

    def get_id(self):
        return self.resource.get("_id")

    def refresh(self):
        self.resource = self.resource_get(resource_id=self.resource.get("_id"))
        return self.resource

    def is_assignable(self, active_trip=None):
        return self.resource.get("state") == WorkflowStateMachine.online.name and active_trip is None

    def set_last_dropoff(self, dropoff_loc=None, dropoff_facility_name=None):
        """Persist the truck's most recent drop-off onto its REST document.

        Read by the assignment service's cost function for dual-cycle scoring
        (prefer the next order whose pickup is near where the truck just dropped
        off). Written once per completed haul (low frequency), so the solver pays
        no extra per-tick query — it reads these straight off the truck doc it
        already fetches. Best-effort: a failed patch must not break the truck.
        """
        if dropoff_loc is None and dropoff_facility_name is None:
            return
        rid = self.get_id()
        etag = self.resource.get("_etag") if self.resource else None
        if not rid or not etag:
            return
        try:
            profile = dict(self.resource.get("profile") or {})
            if dropoff_loc is not None:
                profile["last_dropoff_loc"] = dropoff_loc
                # The freshest anchor for repositioning cost is where the truck
                # actually is between hauls — i.e. its last drop-off.
                profile["current_loc"] = dropoff_loc
            if dropoff_facility_name is not None:
                profile["last_dropoff_facility_name"] = dropoff_facility_name
            patched = self.resource_patch(resource_id=rid, data={"profile": profile}, etag=etag)
            # Eve PATCH responses are meta-only under BANDWIDTH_SAVER (the default): they
            # carry _etag/_updated/_status but NOT state/profile. Assigning that response
            # straight to self.resource wipes `state` to None, which then (a) fails
            # is_assignable (state != online -> truck refuses every future assignment) and
            # (b) crashes perform_workflow_actions ("Truck not available ... None") on every
            # subsequent step. set_last_dropoff fires once per completed haul, so that
            # clobber capped every truck at exactly one haul and stalled the run after day 1.
            # Update the resource in place instead: keep the full doc, apply the profile we
            # just wrote, and refresh only the etag/updated meta from the response.
            if isinstance(patched, dict):
                self.resource["profile"] = profile
                for meta_key in ("_etag", "_updated"):
                    if patched.get(meta_key) is not None:
                        self.resource[meta_key] = patched[meta_key]
        except Exception as exc:
            logging.debug("TruckManager.set_last_dropoff failed: %s", exc)
