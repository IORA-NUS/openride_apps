# Ride-Hail Agent Architecture (Driver + Passenger)

This document describes the high-level design of `apps/driver_app` and `apps/passenger_app`, which have now been archived. All core logic is now in `apps/ride_hail`. The archived folders remain for reference and legacy compatibility.


For cross-domain reuse (including container logistics), see `apps/README_agent_common_layer.md`.

## 1. Purpose and Shared Concept

Both folders implement the same layered concept for different actor roles:

- `*_agent_indie.py`: simulation-facing agent loop (`ORSimAgent`) and behavior policy.
- `*_app.py`: role orchestrator that wires managers, messaging, and in-memory queue.
- `*_manager.py`: role profile lifecycle (login/logout/refresh, registration/bootstrap).
- `*_trip_manager.py`: ride-hail trip state transitions, persistence, and counterpart notifications.

In short, each role is a role-specific facade over the same pipeline:

`Agent tick -> refresh -> consume queued messages -> apply workflow rules -> patch trip state -> publish counterpart event`

## 2. Runtime Flow

### Driver side (`apps/driver_app`)

1. `DriverAgentIndie.process_payload()` runs on simulation ticks.
2. `DriverAgentIndie.step()` refreshes app state and consumes incoming messages.
3. Passenger events are routed through interaction callbacks/plugin handlers.
4. Driver state-based actions execute (`look_for_job`, `confirm/reject`, pickup/dropoff progression).
5. `DriverTripManager` patches backend trip resources and emits MQTT events to passenger.

### Passenger side (`apps/passenger_app`)

1. `PassengerAgentIndie.process_payload()` runs on simulation ticks.
2. `PassengerAgentIndie.step()` refreshes app state and consumes incoming messages.
3. Driver events are routed through interaction callbacks/plugin handlers.
4. Passenger state-based actions execute (`accept/reject`, wait for pickup, end trip).
5. `PassengerTripManager` patches backend trip resources and emits MQTT events to driver.

## 3. Module Inventory

### Driver folder

- `apps/driver_app/driver_agent_indie.py`
- Responsibility: simulation tick loop, route movement, message consumption, and interaction dispatch.

- `apps/driver_app/driver_app.py`
- Responsibility: compose `DriverManager` + `DriverTripManager`, buffer messages, and expose app-level operations (`login`, `refresh`, `ping`, `handle_requested_trip`).

- `apps/driver_app/driver_manager.py`
- Responsibility: driver and vehicle bootstrap, lifecycle transitions (dormant/offline/online), and record refresh.

- `apps/driver_app/driver_trip_manager.py`
- Responsibility: trip transition API wrapper and outgoing passenger notifications (assigned/driver_workflow_event).

### Passenger folder

- `apps/passenger_app/passenger_agent_indie.py`
- Responsibility: simulation tick loop, message consumption, and interaction dispatch.

- `apps/passenger_app/passenger_app.py`
- Responsibility: compose `PassengerManager` + `PassengerTripManager`, buffer messages, and expose app-level operations (`login`, `refresh`, `ping`, assignment handling).

- `apps/passenger_app/passenger_manager.py`
- Responsibility: passenger bootstrap and lifecycle transitions (dormant/offline/online), and record refresh.

- `apps/passenger_app/passenger_trip_manager.py`
- Responsibility: trip transition API wrapper and outgoing driver notifications (passenger_workflow_event).

## 4. Similarities and Differences

### Similarities

- Same 4-layer decomposition (`agent -> app -> manager -> trip_manager`).
- Same in-memory message queue pattern (`enqueue`, `dequeue`, `enfront`).
- Same backend integration style (REST patch/get + ETag refresh).
- Same simulation cadence (periodic refresh + event processing + state action).
- Same interaction model (counterpart events routed via plugin/callback registry).

### Differences

- Driver has route projection/movement logic (`update_location_by_route`, route cutting).
- Driver manages vehicle lifecycle; passenger does not.
- Passenger handles assignment overbooking behavior.
- Transition method names and emitted event payloads differ by role semantics.

## 5. Where Streamlining Is Most Promising

These are high-impact opportunities with minimal behavior risk.

1. Shared base app class
- Extract queue handling, `topic_params`, `update_current`, and `refresh` scaffolding from `DriverApp` and `PassengerApp`.
- Candidate: `apps/ride_hail/base_role_app.py`.

2. Shared base manager class
- Extract repeated workflow transition loops (`dormant -> offline -> online`) and refresh logic.
- Keep role-specific create payloads in subclasses.

3. Shared trip API helper
- `DriverTripManager` and `PassengerTripManager` repeat the same `PATCH -> refresh -> optional publish` shape.
- Add a reusable helper for endpoint call, ETag handling, and standardized error/timeout behavior.

4. Shared agent loop mixin
- `process_payload`, failure handling, market enter/exit envelope, and step gating are very similar.
- Extract into a role-agnostic mixin with role hooks (`consume_messages`, `perform_workflow_actions`).

5. Event schema normalization
- Centralize event names and payload keys to avoid drift.
- Candidate: `apps/ride_hail/events.py` constants and builders.

6. Typed interaction contract
- You already moved to plugin-backed dispatch. Next step is typed contexts for known events to reduce runtime key mistakes.

## 6. Suggested Target Architecture

- Keep role-specific behavior in each folder.
- Move shared infrastructure to a common package, for example `apps/ride_hail/common/`.
- Treat `driver_app` and `passenger_app` as thin role adapters over shared primitives.

Proposed common package skeleton:

- `apps/ride_hail/common/base_agent.py`
- `apps/ride_hail/common/base_app.py`
- `apps/ride_hail/common/base_manager.py`
- `apps/ride_hail/common/base_trip_manager.py`
- `apps/ride_hail/common/events.py`
- `apps/ride_hail/common/errors.py`

## 7. Incremental Refactor Plan

1. Extract shared queue and message plumbing from `driver_app.py` and `passenger_app.py`.
2. Extract shared lifecycle transition helper from `driver_manager.py` and `passenger_manager.py`.
3. Introduce shared trip HTTP helper and migrate one transition method at a time.
4. Extract base agent loop while keeping role-specific handlers unchanged.
5. Consolidate event constants and payload builders.
6. Run existing equivalence tests after each step to lock behavior parity.

## 8. Current Architectural Assessment

Your current structure is already conceptually clean and role-oriented.

The main issue is not design direction, but duplicated implementation details across the two role stacks. That duplication increases maintenance cost and risk of behavior drift. A shared common layer is likely to reduce code size and make future changes safer, while preserving role separation.
