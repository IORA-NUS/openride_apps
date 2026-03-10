# Common Agent Layer Feasibility (Ride-Hail + Container Logistics)

This note evaluates whether the shared layer identified in ride-hail can also support upcoming `container_logistics` agent apps.

## Short Answer

Yes, with one important boundary:

- Share the runtime and infrastructure mechanics.
- Keep domain workflows, event vocabularies, and state transitions in domain packages.

A generic core is realistic because both models are event-driven, state-machine-backed agent workflows with similar tick/message/update loops.

## Why It Fits Container Logistics

From `apps/state_machine/container_logistics/*`:

- Haulier and facility agents are interaction-event driven.
- Flows are explicit state transitions (request -> confirm -> arrival/checkin -> slot -> handoff -> close).
- Cancellation and reset semantics mirror ride-hail style orchestration.

This maps directly onto the same orchestration shape already used in ride-hail:

`tick -> refresh -> process events -> apply state actions -> persist -> publish counterpart event`

## What Should Be Shared

## 1. Agent Runtime Shell

Reusable responsibilities:

- tick/process envelope (`process_payload`, step gating)
- failure threshold and shutdown behavior
- common logging hooks
- message dequeue/requeue pattern (`dequeue`/`enfront`)

Proposed core component:

- `AgentRuntimeBase` with abstract hooks:
- `entering_market`
- `consume_messages`
- `perform_workflow_actions`
- `exiting_market`

## 2. Interaction Dispatch

Reusable responsibilities:

- event + state callback registration
- dispatch through plugin interface
- optional backward-compatible callback router access

Proposed core component:

- existing `apps/utils/interaction_plugin.py` + router adapter
- promote to domain-agnostic location (`apps/agent_core/interaction/`)

## 3. Message Queue and Topic Binding

Reusable responsibilities:

- queue buffering between simulation ticks
- topic registration and central message handler wiring

Proposed core component:

- `RoleAppBase` with `topic_params`, `enqueue_message`, `dequeue_message`, `enfront_message`, `update_current`

## 4. State Transition API Client Patterns

Reusable responsibilities:

- REST `PATCH/GET/POST` calls
- ETag handling and refresh
- standardized timeout and error wrapping
- optional post-transition event publication

Proposed core component:

- `ResourceTransitionClient` and `TripTransitionBase`

## 5. Workflow Lifecycle Helper

Reusable responsibilities:

- repeated lifecycle transition loops (`dormant -> offline -> online`)
- refresh and state checks

Proposed core component:

- `LifecycleManagerBase`

## What Should Stay Domain-Specific

Keep these in `ride_hail` and future `container_logistics` packages:

- state machine classes and transitions
- event names and payload contracts
- domain decisions (pricing, slot allocation policy, overbooking policy)
- route/location behavior and geospatial rules
- role-specific manager data schemas (driver+vehicle vs haulier+facility)

## Domain Adapter Model

Use a ports-and-adapters style split:

- Core layer defines interfaces and runtime orchestration.
- Domain layer implements policy and event/state mapping.

Example shape:

- `apps/agent_core/` (shared)
- `apps/ride_hail/` (domain adapter)
- `apps/container_logistics/` (domain adapter)

## Proposed Package Layout

```text
apps/
  agent_core/
    runtime/
      agent_runtime_base.py
      role_app_base.py
    interaction/
      interaction_plugin.py
      interaction_router.py
    transport/
      resource_transition_client.py
    lifecycle/
      lifecycle_manager_base.py
    contracts/
      events.py
      context.py

  ride_hail/
    adapters/
      driver_agent.py
      passenger_agent.py
      driver_trip_adapter.py
      passenger_trip_adapter.py
    events.py

  container_logistics/
    adapters/
      haulier_agent.py
      facility_agent.py
      haul_trip_adapter.py
      facility_interaction_adapter.py
    events.py
```

## Reuse Readiness Matrix

- Tick orchestration shell: High reuse
- Message queue plumbing: High reuse
- Interaction dispatch plugin: High reuse
- HTTP transition helpers: High reuse
- Lifecycle state helper: Medium-high reuse
- Trip/domain method names: Low reuse (adapter layer required)
- Event vocabulary: Low reuse (domain-owned)
- Geospatial movement logic: Medium reuse (utility-level only)

## Risks and Guardrails

Risks:

- Over-generalizing too early can make domain code harder to read.
- Forcing identical event schemas across domains may create brittle coupling.

Guardrails:

- Keep domain event constants separate per domain.
- Keep core APIs small and behavior-neutral.
- Add equivalence tests per domain before and after each extraction.
- Favor composition over inheritance for domain adapters.

## Recommended Path Before Full Refactor

1. Extract only the shared interaction/runtime pieces first.
2. Pilot with ride-hail driver + passenger adapters.
3. Build first container-logistics app on the same core APIs.
4. Promote only patterns proven useful in both domains.

If step 3 succeeds with minimal domain workarounds, the common layer is validated for broader use.

Concrete implementation backlog with effort and risk:

- `apps/AGENT_COMMON_LAYER_BACKLOG.md`

Architecture decision reference:

- `apps/ADR_agent_core_boundaries.md`

## Current Pilot Status

Implemented lightweight container-logistics scaffold to validate shared-core reuse:

- `apps/container_logistics/events.py`
- `apps/container_logistics/contracts.py`
- `apps/container_logistics/adapters/base_interaction_adapter.py`
- `apps/container_logistics/adapters/haulier_adapter.py`
- `apps/container_logistics/adapters/facility_adapter.py`
- `apps/container_logistics/README.md`

Validation coverage:

- `tests/test_container_logistics_scaffold.py`

This scaffold is intentionally minimal and can be rebuilt later without large migration cost.
