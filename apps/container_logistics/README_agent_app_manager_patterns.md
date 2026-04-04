# Container Logistics Agent/App/Manager Patterns

This document defines the recommended runtime layering for the `container_logistics` domain, following the same separation used in `ridehail`.

## Core Rule

Keep resource lifecycle separate from job execution lifecycle.

- Truck owns availability and shift lifecycle.
- HaulTrip owns execution of one assigned pickup-delivery job.
- Order owns demand lifecycle.
- Facility owns queue and gate service operations.
- Assignment is an external decision service, not a state machine actor.

## Shared Layering Pattern

Use the same decomposition for each first-class runtime role:

`Agent -> App -> Manager -> TripManager or WorkflowManager`

Responsibilities:

- `agent.py`: simulation-facing loop, entry/exit conditions, step cadence.
- `app.py`: orchestration shell, queue handling, callback routing, step sequencing.
- `manager.py`: resource lifecycle, bootstrap, refresh, login/logout, profile metadata.
- `trip_manager.py`: state machine transitions, persistence, event publication, counterpart notifications.

## Recommended Domain Roles

### 1. Truck Role

Files:

- `truck/agent.py`
- `truck/app.py`
- `truck/manager.py`
- `truck/trip_manager.py`

Responsibilities:

- `TruckAgent`: enters market on shift start, exits on shift end, triggers app step.
- `TruckApp`: refreshes active haul trip, consumes queue messages, performs state-driven actions.
- `TruckManager`: owns truck resource using `WorkflowStateMachine` and truck metadata.
- `TruckTripManager`: owns the active `HaulTripStateMachine` document and publishes order/facility workflow events.

Important invariant:

- A truck is assignable if truck workflow state is `online` and there is no active incomplete haul trip.

### 2. Order Role

Files:

- `order/agent.py` optional
- `order/app.py` optional
- `order/manager.py`

Recommendation:

- Keep `Order` lightweight unless it needs independent simulation behavior.
- If orders are passive demand records, an agent/app pair may be unnecessary.
- `OrderManager` can own resource bootstrap, refresh, and transition methods for `OrderStateMachine`.

### 3. Facility Role

Files:

- `facility/agent.py`
- `facility/app.py`
- `facility/manager.py`
- `facility/gate_manager.py` optional

Responsibilities:

- `FacilityAgent`: ticks queue allocation and service completion checks.
- `FacilityApp`: consumes haul-trip messages, updates queue state, dispatches gate callbacks.
- `FacilityManager`: owns facility resource metadata including service times, gate count, and current queue data.
- `GateManager` optional: use only if each gate becomes a separately persisted resource.

Recommendation:

- Keep gates in-memory under facility first. Promote to separate managers only if gates become independently observable/persisted resources.

### 4. Assignment Solver

Recommendation:

- Do not model assignment solver as `agent/app/manager/trip_manager` yet.
- Treat it as an external service or periodic process that consumes truck/order metadata and emits assignments.
- It can live in `assignment/` later if solver run lifecycle becomes operationally important.

## App Step Pattern

For `TruckApp` and `FacilityApp`, use this sequence:

1. refresh in-memory state from backend
2. consume queued messages in order
3. validate resource lifecycle preconditions
4. perform state-driven actions for the active workflow
5. publish counterpart events when a transition requires them

Equivalent shape:

`tick -> refresh -> consume messages -> apply workflow actions -> patch transition -> publish events`

## Suggested Truck App Surface

- `launch(sim_clock)`
- `close(sim_clock)`
- `refresh()`
- `get_truck()`
- `get_trip()`
- `create_new_haul_trip(sim_clock, current_loc, truck, order)`
- `handle_assignment(sim_clock, current_loc, order)`
- `consume_messages()`
- `perform_workflow_actions()`

## Suggested Facility App Surface

- `launch(sim_clock)`
- `refresh()`
- `enqueue_arrival(truck_id, is_pickup_leg)`
- `assign_waiting_trucks()`
- `complete_gate_service(gate_index)`
- `consume_messages()`
- `perform_workflow_actions()`

## Suggested Order Manager Surface

- `create_order(...)`
- `assign_to_truck(...)`
- `mark_pickup_started(...)`
- `mark_pickup_done(...)`
- `mark_dropoff_started(...)`
- `mark_delivered(...)`
- `cancel(...)`

## Callback Placement Rule

Use the ride-hail split:

- message callbacks react to external events
- state callbacks react to local state progression conditions

Examples:

- haul-trip receives `gate_slot_assigned` message -> message callback
- haul-trip sees route completed and reaches facility boundary -> state callback
- facility sees gate service timer elapsed -> state callback or app workflow action

## Interaction Pairs To Implement

Two interaction groups are enough for the first pass:

1. `haultrip_order`
2. `haultrip_gate`

These are specified in the state machine package alongside event constants.
