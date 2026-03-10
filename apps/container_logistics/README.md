# Container Logistics Adapter Scaffold

This is a lightweight scaffold to validate shared `agent_core` reuse for container-logistics interactions.

Included pieces:

- `events.py`: action and event constants for haulier/facility interactions.
- `contracts.py`: payload validators for haulier and facility workflow messages.
- `adapters/base_interaction_adapter.py`: thin wrapper around shared plugin dispatch.
- `adapters/haulier_adapter.py`: minimal facility-message ingress for haulier side.
- `adapters/facility_adapter.py`: minimal haulier-message ingress for facility side.
- `adapters/haulier_agent_adapter.py`: runtime-connected haulier adapter on `agent_core` runtime.
- `adapters/facility_agent_adapter.py`: runtime-connected facility adapter on `agent_core` runtime.

Pilot flow support:

- request -> confirm -> arrival -> checkin -> slot -> handoff -> close
- plus dropoff completion events for haulier-side workflow closure.

Intended use:

- Start simple and evolve later into full ORSim agents/apps.
- Keep domain policies and state transitions in domain modules.
- Reuse shared dispatch and validation patterns from `agent_core`.
