# Legacy Archive And Impl Retirement Plan

## Goal
Define a safe, staged cleanup path to:
1. Move legacy shim packages out of active runtime paths.
2. Eventually retire `*_impl.py` module split if desired.

This plan is intentionally non-breaking in the early phases.

## Current Reality (Verified)
- Legacy packages still exist and are used for backward compatibility:
  - `apps/driver_app/`
  - `apps/passenger_app/`
  - `apps/assignment_app/`
  - `apps/analytics_app/`
- Canonical wrapper modules currently re-export symbols from `*_impl.py` modules.
- `*_impl.py` modules still contain the real implementation classes.
- Compatibility tests explicitly depend on legacy import paths and on `__module__` identity pointing to `*_impl` modules.

## Recommendation Summary
- Do not archive legacy packages yet if backward compatibility is still required.
- Do not remove `*_impl.py` yet, because wrappers and tests depend on this shape.
- Perform cleanup in phased releases with explicit compatibility windows.

## Phase 1 (Now, Non-Breaking)
1. Keep legacy packages in-place.
2. Add deprecation messaging in legacy package `__init__.py` files (warning on import).
3. Add release note and migration note: prefer `apps.ride_hail.*` imports.
4. Keep existing compatibility test suite unchanged.

Exit criteria:
- No runtime regression.
- Consumers are notified of preferred canonical imports.

## Phase 2 (Deprecation Window)
1. Track whether internal/external consumers still import legacy paths.
2. Add CI check (or logging) to flag new legacy-path imports.
3. Keep `*_impl.py` unchanged during this phase.

Exit criteria:
- Legacy imports are near-zero or fully controlled.

## Phase 3 (Breaking Cleanup Release)
1. Move legacy packages to archive namespace, for example:
   - `apps/archive/driver_app/`
   - `apps/archive/passenger_app/`
   - `apps/archive/assignment_app/`
   - `apps/archive/analytics_app/`
2. Remove legacy compatibility tests and replace with canonical-only tests.
3. Keep a short-lived redirect module only if required by release policy.

Exit criteria:
- Canonical-only imports are enforced.
- No contract tests depend on legacy modules.

## Optional Phase 4 (Retire `*_impl.py` Split)
This is optional and should happen only after legacy cleanup is complete.

1. Inline implementation classes from `*_impl.py` into canonical wrapper modules:
   - `apps/ride_hail/driver/agent_impl.py` -> `apps/ride_hail/driver/agent.py`
   - Same pattern for `app`, `manager`, `trip_manager`, and other domains.
2. Update adapter and export tests that currently assert `__module__ == ..._impl`.
3. Remove `*_impl.py` files once tests and imports are migrated.

Important:
- This changes module identity and may break reflection/pickling/tooling assumptions.
- Any tests or integrations depending on `hs` exposure through legacy driver shim must be intentionally preserved or retired.

## File Inventory For Future Archive PR

Legacy packages to archive:
- `apps/driver_app/__init__.py`
- `apps/driver_app/driver_app.py`
- `apps/driver_app/driver_agent_indie.py`
- `apps/driver_app/driver_manager.py`
- `apps/driver_app/driver_trip_manager.py`
- `apps/passenger_app/__init__.py`
- `apps/passenger_app/passenger_app.py`
- `apps/passenger_app/passenger_agent_indie.py`
- `apps/passenger_app/passenger_manager.py`
- `apps/passenger_app/passenger_trip_manager.py`
- `apps/assignment_app/__init__.py`
- `apps/assignment_app/assignment_app.py`
- `apps/assignment_app/assignment_agent_indie.py`
- `apps/assignment_app/engine_manager.py`
- `apps/assignment_app/solver/__init__.py` and solver modules
- `apps/analytics_app/__init__.py`
- `apps/analytics_app/analytics_app.py`
- `apps/analytics_app/analytics_agent_indie.py`

Canonical impl modules that remain required for now:
- `apps/ride_hail/driver/*_impl.py`
- `apps/ride_hail/passenger/*_impl.py`
- `apps/ride_hail/assignment/*_impl.py`
- `apps/ride_hail/analytics/*_impl.py`

## Proposed Execution Order
1. Deprecation warnings + docs.
2. One release cycle of observation.
3. Breaking archive PR.
4. Optional impl inlining PR.
