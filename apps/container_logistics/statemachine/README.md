# Container Logistics State Machines

This package defines the first-pass state-machine model for the container logistics ecosystem described in your scenario.

Implementation note: actor machines use `python-statemachine` (`statemachine.StateMachine`).

## Actors

- Assignment model: external decision service that matches orders to trucks using metadata.
- Truck: owns resource availability and shift lifecycle.
- Haul trip: executes one assigned pickup-delivery job, including empty repositioning if needed.
- Order: tracks assignment and movement from pickup to delivery.
- Facility queue controller: manages FIFO queue and gate assignment.
- Gate: models each service gate lifecycle.

## Design Notes

- FIFO rule is handled in `FacilityQueueController.assign_waiting_trucks`.
- Pickup and dropoff service are distinct gate states (`busy_pickup`, `busy_dropoff`).
- Truck availability reuses `orsim.utils.WorkflowStateMachine`, matching the ride-hail resource lifecycle pattern.
- HaulTripStateMachine owns the pickup and dropoff execution flow for a single assigned order.
- A truck can only start a new haul trip when it is workflow-valid for availability and has no active haul trip.
- Assignment is intentionally not modeled as a state machine yet; it is treated as an external solver/process.

## Files

- `truck_sm.py`: truck availability workflow wrapper over `WorkflowStateMachine`.
- `haul_trip_sm.py`: single-job execution workflow for pickup and dropoff.
- `order_sm.py`: order lifecycle.
- `gate_sm.py`: individual service gate lifecycle.
- `facility_queue_sm.py`: facility queue + multi-gate allocator.
- `events.py`: callback action and event constants for cross-actor workflow messages.
- `haultrip_order_interactions.py`: haul-trip to order interaction mapping for callback design.
- `haultrip_gate_interactions.py`: haul-trip to facility/gate interaction mapping for callback design.
- `container_logistics_state_machines.mmd`: overview diagrams.

## Typical Flow

1. Assignment model selects a workflow-available truck for an order.
2. A new haul trip is created for that truck and starts the pickup workflow.
3. Facility queue controller enqueues the truck; first available gate serves first truck in FIFO queue.
4. After pickup service time completes, the haul trip runs loaded transit to destination.
5. Destination facility repeats queue and gate service process for dropoff.
6. Order completes; haul trip ends; truck remains available for the next assignment if still online.

## Callback Design Inputs

Use these files to design app-level callbacks:

- `haultrip_order_interactions.py` for order-facing message callbacks and state implications.
- `haultrip_gate_interactions.py` for facility-facing queue and gate callbacks.
- `README_agent_app_manager_patterns.md` in the parent package for app and manager responsibility boundaries.
