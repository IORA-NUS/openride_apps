"""Read a format ``/2`` planner capture (round 2) as format ``/3`` (compat: old capture files).

A ``/2`` fixture recorded the ACTIVE cooperation structure and the parsed candidate
settings in a ``world`` / ``candidate_settings`` pair, plus ``live_heads``. Format
``/3`` records the run world itself (``dispatch/capture.py``). :func:`read_fixture_v2`
rebuilds that world: ``cooperation = {active: <id>, structures: [<structure>]}``,
``max_travel_time_pickup``, and the four candidate knobs under their profile keys
(their parsed values re-parse to themselves); ``accounting_params`` moves to the top
level and ``live_heads`` becomes ``live_pairs``. The selection's ``information_model``
and ``share_tag_format`` are dropped (the registry re-derives them).

Compat (old inputs). Leaf module: stdlib only.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = ["read_fixture_v2"]

FORMAT_3 = "openride.dispatch_context/3"

#: ``CandidateSettings.to_dict`` key -> the assignment-profile key it is parsed from.
_SETTINGS_TO_PROFILE = {
    "cell_deg": "spatial_cell_deg",
    "per_order": "spatial_candidates_per_order",
    "max_rings": "spatial_max_rings",
    "max_trucks_per_scope": "max_trucks_per_haulier",
}


def read_fixture_v2(fx: Mapping[str, Any]) -> dict[str, Any]:
    """The ``/3`` form of ``/2`` fixture ``fx`` (a new dict; ``fx`` is not modified)."""
    old_world = dict(fx.get("world") or {})
    world: dict[str, Any] = {"max_travel_time_pickup": old_world.get("max_travel_time_pickup")}
    structure = old_world.get("structure")
    if structure is not None:
        world["cooperation"] = {"active": structure.get("id"), "structures": [dict(structure)]}
    for key, value in dict(fx.get("candidate_settings") or {}).items():
        if key in _SETTINGS_TO_PROFILE:
            world[_SETTINGS_TO_PROFILE[key]] = value
    selection = {k: v for k, v in dict(fx.get("selection") or {}).items()
                 if k not in ("information_model", "share_tag_format")}
    out = {k: v for k, v in fx.items() if k not in ("world", "candidate_settings", "live_heads", "selection")}
    out.update({
        "format": FORMAT_3,
        "selection": selection,
        "world": world,
        "accounting_params": dict(old_world.get("accounting_params") or {}),
        "declaration": None,
        "live_pairs": fx.get("live_heads"),
    })
    return out
