# Agent Common Layer Refactor Backlog

This backlog turns the cross-domain architecture plan into executable work items for:

- `apps/driver_app`
- `apps/passenger_app`
- future `container_logistics` apps

Effort scale:

- S: <= 1 day
- M: 2-4 days
- L: 5+ days

Risk scale:

- Low: local changes, low behavior impact
- Medium: cross-module changes, manageable behavior risk
- High: architectural changes touching critical control flow

## Phase 0: Baseline and Safety Rails

## Item 0.1: Freeze regression baseline

- Scope: record baseline behavior before extraction.
- Tasks:
- keep current interaction suites as mandatory gate.
- add one smoke test command doc for contributors.
- Effort: S
- Risk: Low
- Dependencies: none
- Acceptance criteria:
- existing interaction suites pass consistently:
- `tests/test_interaction_equivalence_exhaustive.py`
- `tests/test_interaction_equivalence.py`
- `tests/test_agent_interactions.py`
- `tests/test_interaction_plugin_adapter.py`

## Item 0.2: Define extraction rules

- Scope: guardrails for shared-vs-domain code.
- Tasks:
- codify what goes into `agent_core` vs domain packages.
- add lightweight ADR note in docs.
- Effort: S
- Risk: Low
- Dependencies: none
- Acceptance criteria:
- decision note exists and is referenced from both README docs.

## Phase 1: Shared Runtime Skeleton (Low-Risk Foundation)

## Item 1.1: Create `agent_core` package scaffold

- Scope: add empty shared package and interfaces.
- Tasks:
- create `apps/agent_core/` with subpackages:
- `runtime/`, `interaction/`, `transport/`, `lifecycle/`, `contracts/`.
- add `__init__.py` and minimal exports.
- Effort: S
- Risk: Low
- Dependencies: 0.2
- Acceptance criteria:
- imports work without changing runtime behavior.

## Item 1.2: Move interaction plugin primitives to core

- Scope: centralize plugin abstractions.
- Tasks:
- move or mirror `InteractionContext`, `InteractionPlugin`, router adapter.
- keep compatibility import path from `apps/utils/interaction_plugin.py`.
- Effort: S
- Risk: Low
- Dependencies: 1.1
- Acceptance criteria:
- existing tests still pass.
- no caller breakage in driver/passenger agents.

## Item 1.3: Extract message queue mixin

- Scope: remove duplicated queue plumbing in app classes.
- Tasks:
- create `RoleMessageQueueMixin` with:
- `enqueue_message`, `dequeue_message`, `enfront_message`.
- use it in `DriverApp` and `PassengerApp`.
- Effort: S
- Risk: Low
- Dependencies: 1.1
- Acceptance criteria:
- no behavior change in message consumption tests.

## Phase 2: Shared App and Lifecycle Layer

## Item 2.1: Introduce `RoleAppBase`

- Scope: unify common app-level orchestration.
- Tasks:
- add base fields and methods:
- `run_id`, `messenger`, `topic_params`, `update_current`, `refresh` hook.
- migrate `DriverApp` and `PassengerApp` to inherit base.
- Effort: M
- Risk: Medium
- Dependencies: 1.3
- Acceptance criteria:
- same topic subscriptions and queue behavior as before.

## Item 2.2: Introduce `LifecycleManagerBase`

- Scope: unify repeated dormant/offline/online transition loops.
- Tasks:
- add reusable transition helper for manager classes.
- migrate `DriverManager.login` and `PassengerManager.login` internals.
- keep role-specific create payload logic in subclasses.
- Effort: M
- Risk: Medium
- Dependencies: 1.1
- Acceptance criteria:
- login/logout/refresh behavior unchanged by tests or smoke checks.

## Phase 3: Shared Transport/Transition Helpers

## Item 3.1: Add `ResourceTransitionClient`

- Scope: standardize REST operations and ETag updates.
- Tasks:
- implement helper for `POST/GET/PATCH` with timeout + error wrapping.
- implement standard refresh utility with `_id` + `_etag` handling.
- Effort: M
- Risk: Medium
- Dependencies: 1.1
- Acceptance criteria:
- at least one driver and one passenger transition method use helper with parity.

## Item 3.2: Incrementally migrate trip managers

- Scope: reduce duplicate patch/refresh boilerplate.
- Tasks:
- migrate transition methods in small batches:
- batch A: read-only or simple updates (`ping`, `refresh`, `reject`).
- batch B: methods with publications (`confirm`, `wait_to_pickup`, `accept`).
- batch C: more complex transitions (`move_to_dropoff`, `end_trip`, `force_quit`).
- Effort: L
- Risk: High
- Dependencies: 3.1
- Acceptance criteria:
- no regression in interaction equivalence tests after each batch.

## Phase 4: Shared Agent Runtime Envelope

## Item 4.1: Extract `AgentRuntimeBase`

- Scope: unify agent tick envelope logic.
- Tasks:
- move common logic from agent classes:
- `process_payload` envelope, failure counters, enter/step/exit sequencing.
- expose hooks for role-specific behavior methods.
- Effort: M
- Risk: High
- Dependencies: 1.2, 2.1
- Acceptance criteria:
- driver/passenger agents keep same external behavior and logs.

## Item 4.2: Keep domain hooks role-specific

- Scope: preserve domain logic where needed.
- Tasks:
- keep driver route projection/movement methods in driver adapter.
- keep passenger-specific patience/overbooking flow in passenger adapter.
- Effort: S
- Risk: Low
- Dependencies: 4.1
- Acceptance criteria:
- no accidental abstraction of domain policy into core.

## Phase 5: Event and Contract Hardening

## Item 5.1: Create domain event constants

- Scope: prevent string drift in events/actions.
- Tasks:
- add `events.py` in `ride_hail` and future `container_logistics` domain package.
- replace inline event/action string literals incrementally.
- Effort: M
- Risk: Medium
- Dependencies: 1.2
- Acceptance criteria:
- all plugin registrations and dispatches use constants.

## Item 5.2: Add typed payload/context models

- Scope: make interaction contracts explicit.
- Tasks:
- define lightweight typed structures for common payloads.
- validate required keys before dispatch.
- Effort: M
- Risk: Medium
- Dependencies: 5.1
- Acceptance criteria:
- unknown/malformed payload handling is deterministic and tested.

## Phase 6: Container Logistics Pilot on Shared Core

## Item 6.1: Build first container-logistics agent adapters

- Scope: validate shared core portability.
- Tasks:
- implement `HaulierAgentAdapter` and `FacilityAgentAdapter` on `agent_core` runtime.
- wire dispatch using container-logistics event constants and state machines.
- Effort: L
- Risk: Medium
- Dependencies: 1.2, 2.1, 4.1
- Acceptance criteria:
- pilot agents run with expected request-confirm-arrival-checkin-slot-handoff-close flow.

## Item 6.2: Cross-domain proof report

- Scope: evaluate whether common layer is truly reusable.
- Tasks:
- record what was reused directly, what required extension points.
- identify API changes needed in `agent_core`.
- Effort: S
- Risk: Low
- Dependencies: 6.1
- Acceptance criteria:
- documented go/no-go for broader migration to shared core.

## Prioritization Recommendation

Execute in this order for best risk-adjusted progress:

1. 0.1, 0.2
2. 1.1, 1.2, 1.3
3. 2.1, 2.2
4. 3.1, 3.2 (in batches)
5. 4.1, 4.2
6. 5.1, 5.2
7. 6.1, 6.2

## Suggested Milestones

- Milestone A (1-2 weeks): phases 0-2 complete.
- Milestone B (2-4 weeks): phase 3 complete with parity.
- Milestone C (1-2 weeks): phases 4-5 complete.
- Milestone D (pilot): phase 6 complete and assessed.

## Definition of Done (Overall)

- Ride-hail behavior parity maintained by regression/equivalence tests.
- Shared core used by both ride-hail agents.
- At least one container-logistics agent pair runs on shared core.
- Domain-specific behavior remains outside `agent_core`.
- Documentation reflects new architecture and extension points.

## Progress Snapshot

- Phase 6.1 pilot: completed (`HaulierAgentAdapter` and `FacilityAgentAdapter` on shared runtime)
- Phase 6.2 proof report: completed (`apps/REPORT_cross_domain_shared_core.md`)
- Ride-hail physical consolidation slice 1: completed (`apps/ride_hail/{driver,passenger,assignment,analytics}` compatibility packages)
- Ride-hail physical consolidation slice 2: completed (test suite imports switched to `apps.ride_hail.*` role packages)
- Ride-hail physical consolidation slice 3: completed (adapter lazy imports switched to `apps.ride_hail.*` role packages)
- Ride-hail physical consolidation slice 4: completed (runtime module import cleanup in `passenger_agent_indie.py`)
- Ride-hail physical consolidation slice 5: completed (canonical wrapper modules for assignment/analytics and adapter alignment)
- Ride-hail physical consolidation slice 6: completed (canonical wrapper modules for driver/passenger and adapter alignment)
- Ride-hail physical consolidation slice 7: completed (moved analytics app implementation to `apps/ride_hail/analytics/app_impl.py` with old-path shim)
- Ride-hail physical consolidation slice 8: completed (moved analytics agent implementation to `apps/ride_hail/analytics/agent_impl.py` with old-path shim)
- Ride-hail physical consolidation slice 9: completed (moved assignment app implementation to `apps/ride_hail/assignment/app_impl.py` with old-path shim)
- Ride-hail physical consolidation slice 10: completed (moved assignment agent implementation to `apps/ride_hail/assignment/agent_impl.py` with old-path shim and lazy legacy export)
- Ride-hail physical consolidation slice 11: completed (moved driver app implementation to `apps/ride_hail/driver/app_impl.py` with old-path shim and lazy legacy exports)
- Ride-hail physical consolidation slice 12: completed (moved driver agent implementation to `apps/ride_hail/driver/agent_impl.py` with old-path shim and compatibility re-exports)
- Ride-hail physical consolidation slice 13: completed (moved passenger app implementation to `apps/ride_hail/passenger/app_impl.py` with old-path shim and lazy legacy exports)
- Ride-hail physical consolidation slice 14: completed (moved passenger agent implementation to `apps/ride_hail/passenger/agent_impl.py` with old-path shim)
- Ride-hail physical consolidation slice 15: completed (moved driver manager implementation to `apps/ride_hail/driver/manager_impl.py` with old-path shim)
- Ride-hail physical consolidation slice 16: completed (moved driver trip-manager implementation to `apps/ride_hail/driver/trip_manager_impl.py` with old-path shim)
- Ride-hail physical consolidation slice 17: completed (moved passenger manager implementation to `apps/ride_hail/passenger/manager_impl.py` with old-path shim)
- Ride-hail physical consolidation slice 18: completed (moved passenger trip-manager implementation to `apps/ride_hail/passenger/trip_manager_impl.py` with old-path shim)
- Ride-hail physical consolidation slice 19: completed (aligned runtime orchestration imports and agent class paths to `apps.ride_hail.*` wrappers)
- Ride-hail physical consolidation slice 20: completed (repointed ride-hail app implementations to canonical local manager/trip-manager wrappers)
- Ride-hail physical consolidation slice 21: completed (normalized scenario comment examples to canonical `apps.ride_hail.*` import paths)
- Ride-hail physical consolidation slice 22: completed (added canonical `ride_hail_*` behavior aliases and switched scenario call sites)
- Ride-hail physical consolidation slice 23: completed (ported assignment `EngineManager` to canonical `AssignmentManager` with legacy compatibility shim)
- Ride-hail physical consolidation slice 24: completed (added canonical assignment solver bridge and switched assignment app implementation to `apps.ride_hail.assignment.solver`)
- Ride-hail physical consolidation slice 25: completed (expanded legacy `apps.assignment_app` lazy exports to include assignment manager compatibility symbols)
- Ride-hail physical consolidation slice 26: completed (physically moved assignment solver implementations to `apps.ride_hail.assignment.solver` with legacy shims in `apps.assignment_app.solver`)
- Ride-hail physical consolidation slice 27: completed (added explicit test coverage for assignment solver legacy-shim to canonical-class parity)
- Ride-hail physical consolidation slice 28: completed (added explicit test coverage for assignment manager legacy-shim and lazy-package export parity)
