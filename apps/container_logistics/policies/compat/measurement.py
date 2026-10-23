"""The accounting dials of the framework's measurement cost (compat: "first node wins").

Verbatim move of ``dispatch/runner.py`` ``_ACCOUNTING_KEYS``, ``accounting_params_for``
and ``framework_accounting_cost`` (plan ``plan_contract_final.md`` §5.1 row 18), plus
:func:`accounting_dials`, the live app's read (``AssignmentApp._solver_params``).

Why compat: the dials are named after one solver family's params
(``dual_cycle_bonus_km`` / ``dual_cycle_radius_km``) and are found by walking the
selection for the first node that declares them (the F1 one-dial mirror rule). Moving
them to a world measurement setting changes bundle bytes and override semantics, a
documented later phase (plan §11). Until then this rule is kept, quarantined.

Not a leaf module: ``framework_accounting_cost`` imports ``assignment.constraints``
locally.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

__all__ = ["accounting_dials", "accounting_params_for", "framework_accounting_cost", "offline_measurement_dials"]

_ACCOUNTING_KEYS = ("dual_cycle_bonus_km", "dual_cycle_radius_km")


def accounting_dials(profile: Mapping[str, Any]) -> Mapping[str, Any]:
    """The live dials: ``profile["solver_params"] or {}`` (``AssignmentApp.__init__``'s read)."""
    return (profile or {}).get("solver_params") or {}


def accounting_params_for(selection: Any) -> dict[str, Any]:
    """The accounting dials a selection implies: the F1 one-dial mirror rule, offline.

    ``selection`` is a ``ResolvedDispatch`` tree (each key from the first node,
    pre-order, that declares it; normally the leaf), a ``PolicyParams`` or a plain
    mapping. Keys nobody declares are left out (the constraint defaults apply).
    """
    from apps.container_logistics.policies.dispatch.resolved import ResolvedDispatch

    if isinstance(selection, ResolvedDispatch):
        out: dict[str, Any] = {}
        node: ResolvedDispatch | None = selection
        while node is not None:
            values = node.params.to_dict() if node.params is not None else {}
            for key in _ACCOUNTING_KEYS:
                if key in values and key not in out:
                    out[key] = values[key]
            node = node.inner
        return out
    values = selection.to_dict() if hasattr(selection, "to_dict") else dict(selection or {})
    return {k: values[k] for k in _ACCOUNTING_KEYS if k in values}


def offline_measurement_dials(selection: Any) -> dict[str, Any]:
    """The measurement dials of an OFFLINE tick (battery, fixtures, bench, capture replay).

    The framework's entry point (``dispatch/runner.py``) into :func:`accounting_params_for`,
    under a name that states the framework's need rather than this module's rule: the
    framework only asks "which dials does ``Measure`` get when the live app supplies
    none"; that the answer is "the first node that declares the dual-cycle keys" is the
    compat rule, and it stays here (plan ``plan_contract_final.md`` §2.5 ``measure``,
    M5). Same value as :func:`accounting_params_for`, which it calls.
    """
    return accounting_params_for(selection)


def framework_accounting_cost(selection: Any) -> Callable[[dict[str, Any], dict[str, Any]], float]:
    """The accounting cost an offline tick uses: ``constraints.assignment_cost``.

    The live sim passes ``AssignmentApp._solver_params``, which the one-dial mirror
    keeps equal to the selection's dual-cycle params; offline they come from
    :func:`accounting_params_for` and the constraint defaults for the rest.
    """
    from apps.container_logistics.assignment import constraints as assign_constraints

    accounting = accounting_params_for(selection)

    def cost(t: dict[str, Any], o: dict[str, Any]) -> float:
        return assign_constraints.assignment_cost(t, o, accounting)

    return cost
