"""Control-plane policy commands: validate a run's policy overrides before spawning it.

Single responsibility: turn ``run-simulation``'s ``--solver`` / ``--policy`` /
``--policy-param`` flags into the ``ORSIM_POLICY_OVERRIDES`` object and dry-run it
against the scenario's compiled assignment profile, so a typo or an out-of-range
param is refused synchronously instead of in the background sim's log (plan
``docs/plans/policy_slots_p0_p3.md`` §4 P2.6). The dry run uses the same
``apply_policy_overrides`` the sim main applies, on a deep copy, and never writes.

Runs in the control subprocess (not an agent): no celery restart applies.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from typing import Any, Sequence

logger = logging.getLogger(__name__)

__all__ = ["build_run_policy_overrides", "load_assignment_collection", "validate_run_policy_args"]


def build_run_policy_overrides(
    solver: str | None, policies: Sequence[str] = (), params: Sequence[str] = ()
) -> dict[str, Any]:
    """The overrides object for ``ORSIM_POLICY_OVERRIDES``; ``--solver X`` = ``--policy dispatch=X``.

    Raises ``PolicyError`` for a malformed flag, or when ``--solver`` and
    ``--policy dispatch=`` name different types.
    """
    from apps.container_logistics.policies.framework import PolicyError, parse_policy_cli_args

    overrides = parse_policy_cli_args(list(policies or ()), list(params or ()))
    solver = (solver or "").strip()
    if solver:
        dispatch = overrides.setdefault("dispatch", {})
        if dispatch.get("type") not in (None, solver):
            raise PolicyError(
                f"--solver {solver!r} conflicts with --policy dispatch={dispatch['type']!r}; "
                "--solver is shorthand for --policy dispatch=NAME, pass one of them."
            )
        dispatch["type"] = solver
    return overrides


def load_assignment_collection(scenario: str | None) -> dict[str, Any]:
    """The scenario's compiled assignment behaviors, read-only (``{}`` when not found).

    Reads ``scenario.json`` (or a legacy ``assignment_behavior.json``) from the same
    folder ``ScenarioManager`` loads at run start; never generates anything.
    """
    from apps.container_logistics.scenario.frontend_scenario_spec import (
        container_logistics_scenarios_root,
    )
    from apps.container_logistics.scenario.scenario_bundle import read_bundle
    from apps.container_logistics.scenario.scenario_config import default_scenario_name

    folder = os.path.join(container_logistics_scenarios_root(), scenario or default_scenario_name())
    bundle = read_bundle(folder)
    agents = (bundle or {}).get("agents")
    if isinstance(agents, dict) and isinstance(agents.get("assignment"), dict):
        return agents["assignment"]
    legacy = os.path.join(folder, "assignment_behavior.json")
    if os.path.isfile(legacy):
        with open(legacy, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    return {}


def validate_run_policy_args(
    scenario: str | None,
    solver: str | None,
    policies: Sequence[str] = (),
    params: Sequence[str] = (),
) -> dict[str, Any] | str:
    """Dry-run a run's policy flags against the scenario's assignment profile.

    Returns the overrides object to ship (``{}`` when no flag was given), or the error
    message (``str``) when the flags, the merge, or the patched profile fail to
    resolve. A bundle compiled before policy slots goes through the legacy
    translation, exactly as at run start. A scenario with no readable bundle is
    validated against the slot defaults (its own load fails later, loudly).
    """
    from apps.container_logistics.policies.dispatch.profile import (
        assert_assignment_profiles_resolvable,
    )
    from apps.container_logistics.policies.framework import PolicyError
    from apps.container_logistics.scenario.scenario_overrides import apply_policy_overrides

    try:
        overrides = build_run_policy_overrides(solver, policies, params)
        if not overrides:
            return {}
        collection = copy.deepcopy(load_assignment_collection(scenario))
        apply_policy_overrides(collection, overrides)
        assert_assignment_profiles_resolvable(collection)
    except PolicyError as exc:
        return str(exc)
    return overrides
