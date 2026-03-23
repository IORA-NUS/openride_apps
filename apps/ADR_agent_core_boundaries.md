# ADR: Agent Core and Domain Boundaries

Status: Accepted
Date: 2026-03-10

## Context

The codebase is introducing `apps/agent_core` as a shared layer for ride-hail and future container-logistics workflows. We need clear boundaries to avoid leaking domain policy into shared infrastructure.

## Decision

Code belongs in `apps/agent_core` only if it is domain-neutral and reusable across roles/domains.

Keep in `apps/agent_core`:

- Runtime orchestration envelopes and queue plumbing.
- Generic interaction routing/plugin abstractions.
- Transport helpers for resource operations (`GET/PATCH/POST`) and refresh mechanics.
- Lifecycle helpers that are independent of domain event vocabulary.

Keep in domain packages (`apps/ride_hail`, `apps/container_logistics`):

- Event/action vocabularies and payload contracts.
- State machine transitions and domain workflow semantics.
- Domain policy logic (pricing, overbooking/patience, slot allocation, routing policy).
- Domain-specific schema assumptions and publication payload shapes.

## Consequences

- Shared code remains small and stable.
- Domain evolution does not force changes in core utilities.
- Migration can proceed incrementally with compatibility adapters and parity tests.

## Validation

- Interaction parity suites remain mandatory regression gates.
- New shared abstractions require at least one ride-hail and one non-ride-hail use case before promotion.
