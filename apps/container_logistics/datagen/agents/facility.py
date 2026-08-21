"""FacilityAgent and its policy tree.

Facility *sites* are resolved once by the Preprocessor from real seeded addresses and
stored on the spec; the FacilityAgent builds each facility's behavior dict from them —
byte-identical to the old FacilityBuilder.

The facility policy selects the **site allocation strategy**, which the Preprocessor
reads by policy *type* when weighting the code mix:
  * ``AllocateFacilityPolicy`` (default) — demand-proportional to the order matrix
  * ``RandomFacilityPolicy``            — uniform code mix (ignores demand)
"""

from __future__ import annotations

from ..builders import geojson_point
from .base import Agent, Policy


class FacilityPolicy(Policy):
    role = "facility"


class AllocateFacilityPolicy(FacilityPolicy):
    name = "allocate"     # demand-proportional to the order matrix


class RandomFacilityPolicy(FacilityPolicy):
    name = "random"       # uniform code mix


class FacilityAgent(Agent):
    role = "facility"

    def generate(self, n: int) -> dict:
        spec = self.spec
        profile_cfg = spec.facility_settings.get("profile", {})
        count = len(self.facilities)
        # ``rebate`` is stripped here and re-read from the SITE below: the
        # scenario-wide ``overrides.facility.rebate`` is rank 0 and has already been
        # folded into the site's resolved value, so letting it ride the blanket merge
        # would let it shadow a rule that deliberately overrode it (including a rule
        # that set it to null).
        profile_clean = {
            k: v for k, v in profile_cfg.items() if k not in ("facilities", "rebate")
        }

        out = {}
        for idx in range(max(1, count)):
            agent_id = f"facility_{idx:03d}"
            facility = self.facilities[idx % len(self.facilities)]
            gate_count = facility["gate_count"]
            service_time = facility["service_time"]
            facility_type = facility["facility_type"]
            profile = {
                **profile_clean,
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
            if facility.get("footprint") is not None:
                profile["footprint"] = facility.get("footprint")
            # THIS facility's resolved schedule, read off the SITE (facility rules
            # plan §8.1). Both builders read the same site rather than sharing a
            # resolver function, which removes the divergence class instead of
            # mitigating it. Zero RNG here — a dict lookup, nothing else.
            resolved_rebate = facility.get("rebate")
            if resolved_rebate is not None:
                profile["rebate"] = resolved_rebate
            else:
                profile.pop("rebate", None)
            out[agent_id] = {
                "email": f"{agent_id}@test.com",
                "password": "password",
                "persona": {"role": "facility", "domain": self.domain},
                "steps_per_action": spec.facility_settings.get("steps_per_action", 1),
                "response_rate": spec.facility_settings.get("response_rate", 1.0),
                "step_only_on_events": spec.facility_settings.get("step_only_on_events", True),
                "gate_count": gate_count,
                "service_time": service_time,
                "profile": profile,
            }
        return out
