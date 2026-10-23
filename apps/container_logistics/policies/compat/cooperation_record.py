"""The run's ``COOPERATION`` record: the pre-contract cooperation stamp format, derived from world data.

Single responsibility: build the record ``run_config.meta`` carries as ``COOPERATION``
and the analytics agent reads for its planner-scope breakdown rows and gains
attribution -- ``{structure_id, components, adjacency, pools}``, the first four keys of
the pre-contract ``pooled_planner.effective_cooperation_stamp``, byte for byte. It is
here, in the compatibility quarantine, because it is an OLD format kept so the analytics
and every stored run keep reading identically, and because building it names the pool
decomposition (``assignment.pools``), which framework-side code may only reach through
``compat`` (``tests/test_framework_imports.py`` lists it as a gateway).

Callers: ``scenario.run_declaration`` (the run-declaration check the sim main, the
control-plane dry run and scenario compile share) and ``analytics.manager``, which
re-exports both names. Import contract: stdlib only at module level; the assignment
modules are imported inside the function.
"""

import json
from typing import Any, Dict, Mapping, Optional

__all__ = ["COOPERATION_SETTING", "cooperation_record"]

#: The run setting (``orsim_settings`` key -> ``run_config``) the cooperation record rides in.
COOPERATION_SETTING = "COOPERATION"


def cooperation_record(world: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """The run's ``COOPERATION`` record, derived from WORLD data (final review M3).

    ``world`` is an assignment profile (or a run world over one) AFTER any per-run
    ``--structure`` override; only its ``cooperation`` key is read. Returns ``None``
    when it has no active structure (own-haulier only: no planner rows), else
    ``{structure_id, components, adjacency, pools}`` as plain JSON -- the record
    ``AnalyticsManager.set_cooperation`` reads for the planner-scope breakdown rows
    and gains attribution. The structure's own ``components`` / ``adjacency`` as
    authored (``[]`` / ``{}`` when absent) and the EFFECTIVE pools
    (``assignment.pools.pools_from_structure``).

    FRAMEWORK-owned: the sim main writes it (``_apply_run_declaration``) for every run
    with an arm, whatever the solver declared, so the analytics never depends on a
    solver choosing to stamp it. It equals the built-in solvers' own
    ``solver_lib.cooperation.stamp(resolve_arm(world))`` byte for byte
    (``tests/test_run_entry_ownership.py`` pins it on every structure of the compiled
    smoke bundle and a hand-written block), which keeps the built-ins' run settings identical.

    Moved verbatim from ``analytics/manager.py`` (which re-exports it) so the
    control-plane dry run and scenario compile can compute it without importing the
    analytics agent stack (orsim; round-2 review M-C).
    """
    # Local imports: the assignment package is not on this module's import path.
    from apps.container_logistics.assignment import constraints as assign_constraints
    from apps.container_logistics.assignment import pools as assign_pools

    structure = assign_constraints.active_cooperation_structure(
        {"cooperation": (world or {}).get("cooperation")}
    )
    if structure is None:
        return None
    structure = structure if isinstance(structure, dict) else {}
    return json.loads(json.dumps({
        "structure_id": structure.get("id"),
        "components": structure.get("components") or [],
        "adjacency": structure.get("adjacency") or {},
        "pools": [{"id": p.id, "members": sorted(p.members)}
                  for p in assign_pools.pools_from_structure(structure)],
    }))
