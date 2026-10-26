"""The accounting dials of the framework's measurement cost (compat: "first node wins").

Verbatim move of ``dispatch/runner.py`` ``_ACCOUNTING_KEYS`` and ``accounting_params_for``
(plan ``plan_contract_final.md`` §5.1 row 18), plus :func:`offline_measurement_dials`,
the framework's one entry point into it (``dispatch/runner.py``). The two zero-caller
helpers that also lived here (``accounting_dials``, ``framework_accounting_cost``) were
deleted in the final-review fix loop (M8): dead code in the quarantine is a hole, not
a convenience.

Why compat: the dials are named after one solver family's params
(``dual_cycle_bonus_km`` / ``dual_cycle_radius_km``) and are found by walking the
selection for the first node that declares them (the F1 one-dial mirror rule). Moving
them to a world measurement setting changes bundle bytes and override semantics, a
documented later phase (plan §11). Until then this rule is kept, quarantined.

Leaf module: imports ``policies.dispatch.resolved`` locally (inside
:func:`accounting_params_for`) and nothing else.
"""

from __future__ import annotations

from typing import Any

__all__ = ["accounting_params_for", "offline_measurement_dials"]

_ACCOUNTING_KEYS = ("dual_cycle_bonus_km", "dual_cycle_radius_km")


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
