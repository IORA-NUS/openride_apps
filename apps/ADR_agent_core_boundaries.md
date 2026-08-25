# ADR: Agent Core and Domain Boundaries

Status: **Superseded** (2026-08-21) — was: Accepted
Date: 2026-03-10

> ## Superseded — the layer this ADR governs was never committed
>
> This decision is preserved as a record of intent. **It does not describe this
> codebase.** `apps/agent_core` contained four empty directories and zero files,
> with nothing tracked in git; it was removed on 2026-08-21. The only references
> to it anywhere were commented-out imports in `apps/ridehail`.
>
> The companion documents that described the layer as built —
> `REPORT_cross_domain_shared_core.md` (which cited
> `apps/agent_core/runtime/agent_runtime_base.py` and
> `apps/agent_core/transport/...` by path), `README_agent_common_layer.md`,
> `AGENT_COMMON_LAYER_BACKLOG.md`, `README_ride_hail_agents.md` and
> `CLEANUP_LEGACY_ARCHIVE_PLAN.md` — were deleted in the same commit. They are
> recoverable from git history.
>
> **Two rules below are therefore not in force and must not be cited as
> constraints:**
>
> - *"New shared abstractions require at least one ride-hail and one
>   non-ride-hail use case before promotion."* There is no shared layer to
>   promote into. This rule was cited during the 2026-08 domain-boundary review
>   as a reason to retain `apps/ridehail`; that reasoning does not hold.
> - *"Interaction parity suites remain mandatory regression gates."* Those suites
>   have been failing — 30 of the repository's 36 known failures — for reasons
>   unrelated to any change under review.
>
> The underlying principle — domain-neutral code stays shared, domain policy
> stays in the domain package — remains sound and is worth re-adopting. It needs
> a new ADR describing a layer that exists, not this one.

## Context

The codebase is introducing `apps/agent_core` as a shared layer for ride-hail and future container-logistics workflows. We need clear boundaries to avoid leaking domain policy into shared infrastructure.

## Decision

Code belongs in `apps/agent_core` only if it is domain-neutral and reusable across roles/domains.

Keep in `apps/agent_core`:

- Runtime orchestration envelopes and queue plumbing.
- Generic interaction routing/plugin abstractions.
- Transport helpers for resource operations (`GET/PATCH/POST`) and refresh mechanics.
- Lifecycle helpers that are independent of domain event vocabulary.

Keep in domain packages (`apps/ridehail`, `apps/container_logistics`):

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
