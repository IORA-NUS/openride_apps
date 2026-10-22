"""The legacy profile keys that SELECT a planner policy (old inputs, compat only).

A compiled assignment profile selects its planner through ``policies`` (plus the
``policies_format`` marker); an old profile selected it through these keys, which
``dispatch/legacy.py`` translates. They are selection, not world data, so
``dispatch.bind.RunWorld`` hides them from every solver's ``bind`` and ``solve_tick``.

Leaf module: stdlib only.
"""

from __future__ import annotations

__all__ = ["LEGACY_SELECTION_KEYS"]

#: ``strategy`` (the leaf), ``solver_params`` (its params, also the accounting dials),
#: ``planner`` (``topology`` / ``market`` / ``deployment`` / ``rebate_aware``) and
#: ``use_spatial_matching`` (``candidates``).
LEGACY_SELECTION_KEYS: tuple[str, ...] = ("strategy", "solver_params", "planner", "use_spatial_matching")
