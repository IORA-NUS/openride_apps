"""``node_provenance``: the per-node run provenance the built-in solvers declare at bind.

Single responsibility: build the plain-JSON ``Declaration.provenance`` a built-in
solver hands the framework (plan ``plan_contract_final.md`` §2.3, §7.9), which stamps
it as ``run_config.meta.policies.planner.provenance[<node path>]``. The framework
never reads the values; the dashboard shows the top node's ``badge``.

- ``badge`` -- the solver's display text for how it decides (``"central"``,
  ``"per group"``, ``"per company"``); the same text its ``@policy(badge=...)``
  declares for the catalog.
- ``sample_scope`` -- where the ``max_trucks_per_scope`` truck-sample cap applies:
  per haulier on a central leaf's whole tick (``candidates.TICK_SAMPLE_SCOPE``),
  per cooperation group / per company under the decompositions (the values the
  pre-contract stamp's ``candidate_settings.sample_scope`` carried).
- ``candidate_settings`` -- the four candidate knobs the node read from the run world
  (``candidates.CandidateSettings.from_world``; reading them records the keys).

Solver helper (``policies/solver_lib``); the framework never imports it.
"""

from __future__ import annotations

from typing import Any, Mapping

from .candidates import CandidateSettings

__all__ = ["node_provenance"]


def node_provenance(world: Mapping[str, Any], *, badge: str | None = None,
                    sample_scope: str | None = None) -> dict[str, Any]:
    """``{badge?, sample_scope?, candidate_settings}`` of one node (unset keys are left out)."""
    out: dict[str, Any] = {}
    if badge is not None:
        out["badge"] = badge
    if sample_scope is not None:
        out["sample_scope"] = sample_scope
    out["candidate_settings"] = CandidateSettings.from_world(world).to_dict()
    return out
