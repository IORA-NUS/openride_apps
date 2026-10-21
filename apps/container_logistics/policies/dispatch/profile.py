"""Assignment behavior profile -> dispatch policy: the run-start resolvability check.

Single responsibility: map an assignment agent's behavior ``profile`` onto the
dispatch slot. In P0 that is only :func:`assert_assignment_profiles_resolvable`, the
fail-loud check ``run_container_logistics_simulation.main`` runs before any agent
spawns (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P0.7). P1 extends this module
with ``resolve_dispatch_for_profile`` (the ``policies`` block or the legacy
``strategy``/``solver_params`` keys -> a resolved dispatch) and
``build_dispatch_policy``.

Import contract (plan §2.7): imports only the leaf ``policies.framework`` at module
top, never ``apps.container_logistics.assignment.*``, so ``assignment.app`` can
import it without a cycle.
"""

from __future__ import annotations

from typing import Any, Mapping

from apps.container_logistics.policies.framework import (
    SLOTS,
    PolicyError,
    get_policy_spec,
)

__all__ = ["assert_assignment_profiles_resolvable"]


def assert_assignment_profiles_resolvable(collection: Mapping[str, Any]) -> None:
    """Raise ``PolicyError`` unless every assignment agent's strategy is registered.

    ``collection`` is the assignment behavior collection, ``{agent_id: behavior}``.
    A behavior's ``profile.strategy`` that is missing or empty means the slot
    default, exactly as ``assignment.solver.get_solver`` reads it. The raised error
    is the lookup's own class (e.g. ``UnknownPolicyError``) with the agent id
    prefixed; a behavior that is not a mapping raises plain ``PolicyError``.
    """
    default = SLOTS["dispatch"].default
    for agent_id, behavior in (collection or {}).items():
        if not isinstance(behavior, Mapping):
            raise PolicyError(
                f"assignment agent {agent_id!r}: behavior must be a mapping with a "
                f"'profile' (got {type(behavior).__name__})."
            )
        profile = behavior.get("profile") or {}
        try:
            get_policy_spec("dispatch", profile.get("strategy") or default)
        except PolicyError as exc:
            raise type(exc)(f"assignment agent {agent_id!r}: {exc}") from exc
