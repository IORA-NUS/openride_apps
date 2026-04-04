# Cross-Domain Shared Core Proof Report

Date: 2026-03-10
Scope: Backlog Item 6.2 (`apps/AGENT_COMMON_LAYER_BACKLOG.md`)

## Objective

Evaluate whether `apps/agent_core` is reusable across ride-hail and container-logistics with acceptable complexity and behavior risk.

## Evidence Collected

Implemented pilot and validation artifacts:

- Shared runtime envelope:
: `apps/agent_core/runtime/agent_runtime_base.py`
- Shared queue/runtime helpers in active use:
: `apps/agent_core/runtime/message_queue.py`, `apps/agent_core/runtime/role_app_base.py`
- Shared interaction router/plugin usage:
: `apps/agent_core/interaction/*`
- Shared transport helpers in active use:
: `apps/agent_core/transport/resource_transition_client.py`, `apps/agent_core/transport/role_trip_manager_base.py`
- Container-logistics runtime-connected pilot adapters:
: `apps/container_logistics/adapters/runtime_agent_adapter_base.py`
: `apps/container_logistics/adapters/haulier_agent_adapter.py`
: `apps/container_logistics/adapters/facility_agent_adapter.py`

Validation runs (latest):

- `45 passed, 1 warning`
- Includes:
: `tests/test_container_logistics_agent_adapters.py`
: `tests/test_container_logistics_scaffold.py`
: `tests/test_interaction_equivalence.py`
: `tests/test_interaction_equivalence_exhaustive.py`
: `tests/test_agent_interactions.py`
: `tests/test_interaction_plugin_adapter.py`
: `tests/test_ride_hail_contracts.py`

## Reuse Results

Directly reused without domain customization:

- `AgentRuntimeBase` step envelope (`init/step`, enter-step-exit, failure accounting).
- Queue buffering pattern (`enqueue/dequeue/enfront`) for agent message processing.
- Callback router interaction pattern for `(action,event)` dispatch.
- Transport request helper patterns (`_post_trip`, `_patch_trip_transition`, `_get_trip`) in ride-hail managers.

Required extension points (domain adapters needed):

- Event-to-transition mapping remains domain-owned.
- State machine transition names and legal transition sequences remain domain-owned.
- Outbound payload message shapes remain domain-owned.
- Domain lifecycle policy decisions (ride-hail patience/overbooking, container handoff semantics) remain domain-owned.

## API Gaps Identified in `agent_core`

1. State access normalization helper
- Issue: state-machine libraries expose state name via different attributes (`id`, `value`, `identifier`).
- Current mitigation: local normalization in `runtime_agent_adapter_base.py`.
- Recommendation: add a small shared helper in `agent_core/runtime`.

2. Generic domain event transition adapter base
- Issue: pilot required custom runtime adapter base in container-logistics.
- Recommendation: promote a generic `DomainInteractionAgentBase` into `agent_core` once a second non-ride-hail domain confirms identical shape.

3. Typed contract parser abstractions for domains
- Ride-hail now has typed payload models (`apps/ridehail/models.py`).
- Recommendation: provide optional core parser protocol/interface, not concrete models, to keep schemas domain-owned.

## Risk Assessment

- Technical risk: Low-Medium
: shared runtime abstractions are stable under current tests.
- Behavioral risk: Low
: parity suites remain green after extraction and pilot integration.
- Architectural risk: Medium
: premature generalization of domain adapter specifics into core could reduce clarity.

## Go/No-Go Decision

Go for broader migration to shared core, with guardrails.

Guardrails:

- Keep event vocabularies and transition mappings strictly domain-owned.
- Promote new core abstractions only after proving reuse in at least two domains.
- Continue mandatory parity suite gating for each extraction increment.
- Preserve compatibility adapters during physical package migration.

## Next Recommended Actions

1. Promote a minimal shared state-name normalization helper into `agent_core/runtime`.
2. Keep container-logistics pilot adapters in domain package for one more iteration before core promotion.
3. Continue consolidation move plan for ride-hail physical package migration (`driver/passenger/assignment/analytics`) with compatibility re-exports.
4. Add a short migration status table to backlog doc after each checkpoint commit.
