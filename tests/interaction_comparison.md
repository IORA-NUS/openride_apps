# Interaction Implementation Comparison

This report compares the legacy/as-is conditional interaction logic with the callback-based implementation.

## Scope
- Ride-hail passenger message handling (all mapped events + fallback path)
- Ride-hail driver message handling (all mapped events)
- Ride-hail state interaction handling for passenger and driver
- Callback router and plugin adapter behavior

## Side-by-Side Scenarios
| Category | Coverage | Equivalent |
|---|---|---|
| Passenger message interactions | 8 scenarios: `driver_confirmed_trip`, `driver_arrived_for_pickup`, `driver_move_for_dropoff`, `driver_arrived_for_dropoff`, `driver_waiting_for_dropoff`, `driver_cancelled_trip` (with and without location), unknown event fallback | Yes |
| Driver message interactions | 4 scenarios: `passenger_confirmed_trip`, `passenger_rejected_trip`, `passenger_acknowledge_pickup`, `passenger_acknowledge_dropoff` | Yes |
| Passenger state interactions | `passenger_received_trip_confirmation` (accept/reject), `passenger_accepted_trip`, `passenger_droppedoff` | Yes |
| Driver state interactions | `driver_looking_for_job`, `driver_received_trip` (accept/reject), `driver_moving_to_pickup`, `driver_pickedup`, `driver_moving_to_dropoff`, `driver_droppedoff` | Yes |
| Router/plugin behavior | Registered and unregistered message/state dispatch | Yes |

## Test Evidence
Executed:
- `tests/test_interaction_equivalence_exhaustive.py`
- `tests/test_interaction_equivalence.py`
- `tests/test_agent_interactions.py`
- `tests/test_interaction_plugin_adapter.py`

Result:
- `33 passed`

## Notes
- Domain mapping remains in app code.
- Generic dispatch contract is available through plugin adapter abstractions for future ORSim extraction.
