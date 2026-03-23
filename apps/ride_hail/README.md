# Ride-Hail Domain Package

This package now holds shared ride-hail domain contracts:

- `events.py`: action and event constants.
- `contracts.py`: payload validators.
- `adapters/`: thin compatibility entrypoints for role modules.

Current adapter entrypoints:

- `RideHailAssignmentAdapter` -> `AssignmentApp`, `AssignmentAgentIndie`
- `RideHailAnalyticsAdapter` -> `AnalyticsApp`, `AnalyticsAgentIndie`
- `RideHailDriverAdapter` -> `DriverApp`, `DriverAgentIndie`
- `RideHailPassengerAdapter` -> `PassengerApp`, `PassengerAgentIndie`

Role package entrypoints (compatibility shims):

- `apps.ride_hail.driver` -> exports `DriverApp`, `DriverAgentIndie`, `DriverManager`, `DriverTripManager`
- `apps.ride_hail.passenger` -> exports `PassengerApp`, `PassengerAgentIndie`, `PassengerManager`, `PassengerTripManager`
- `apps.ride_hail.assignment` -> exports `AssignmentApp`, `AssignmentAgentIndie`
- `apps.ride_hail.analytics` -> exports `AnalyticsApp`, `AnalyticsAgentIndie`

## Should driver/passenger/assignment/analytics be consolidated here?

Short answer: yes, but incrementally.

Recommended phases:

1. Keep existing folder paths; import shared contracts from `apps/ride_hail`.
2. Add adapter layers under `apps/ride_hail/adapters/` for each role.
3. Move implementation files only after import-call sites are stabilized.
4. Leave compatibility re-exports in old folders during transition.

Suggested target structure:

- `apps/ride_hail/driver/`
- `apps/ride_hail/passenger/`
- `apps/ride_hail/assignment/`
- `apps/ride_hail/analytics/`

## Why incremental consolidation

- Reduces import breakage risk.
- Keeps checkpoint commits reviewable.
- Allows rollback at role boundaries.

Shared-vs-domain boundary decision:

- `apps/ADR_agent_core_boundaries.md`

## Current status

- Driver/passenger stack already consumes shared ride-hail contracts.
- Assignment publish action now uses `RideHailActions.REQUESTED_TRIP`.
- Analytics has no direct ride-hail workflow action dispatch and can be migrated later.
