"""The single policy catalog: every slot and its policies, and every component kind.

:func:`policy_catalog` is the JSON payload served as ``known-policies.slots`` and
:func:`component_catalog` the one served as ``known-policies.components`` (plan
``docs/plans/policy_slots_p0_p3.md`` §4 P0.8; round 2
``docs/plans/policy_slots_round2_whole_tick.md`` §5 C1). They are the only source the
CLI and the dashboard read slot, policy and component lists from -- no hand-copied
lists elsewhere.

Leaf module (plan §2.7): the component registries live under
``apps.container_logistics.assignment`` and are reached only through
``framework.components``, which imports them locally, at call time.
"""

from __future__ import annotations

import logging
from typing import Any

from .components import COMPONENT_KINDS, ComponentKind, ref_names, ref_target
from .registry import PolicySpec, list_policies
from .slots import SLOTS, SlotSpec

logger = logging.getLogger(__name__)

__all__ = ["policy_catalog", "component_catalog"]


def policy_catalog() -> dict[str, Any]:
    """Return a JSON-serialisable description of every slot, in ``SLOTS`` order.

    Implemented slots list their registered policies (schema, defaults, missing
    requirements, and the dispatch declarations); planned slots carry only the phase
    they land in. Raises whatever discovery raises (a broken policy module must not
    silently vanish).
    """
    return {name: _slot_entry(slot) for name, slot in SLOTS.items()}


def component_catalog() -> dict[str, Any]:
    """Return ``{kind: {default, description, policies: [...]}}`` for every component kind.

    Names are read live from the component registries, sorted; each policy entry
    carries ``name``, ``version``, ``description``, ``params_schema`` and ``defaults``.
    """
    return {kind: _component_entry(spec) for kind, spec in COMPONENT_KINDS.items()}


def _slot_entry(slot: SlotSpec) -> dict[str, Any]:
    if slot.status == "implemented":
        return {
            "kind": slot.kind,
            "status": slot.status,
            "default": slot.default,
            "timings": list(slot.timings),
            "executable_timings": list(slot.executable_timings),
            "description": slot.description,
            "policies": [_policy_entry(p) for p in list_policies(slot.name)],
        }
    return {"kind": slot.kind, "status": slot.status, "lands_in": slot.lands_in}


def _policy_entry(spec: PolicySpec) -> dict[str, Any]:
    missing = spec.missing_requirements()
    where = f"defaults of {spec.slot} policy {spec.name}"
    return {
        "name": spec.name,
        "version": spec.version,
        "description": spec.description,
        "requires": list(spec.requires),
        "missing": missing,
        "available": not missing,
        "information_model": spec.information_model,
        "composite": spec.is_composite,
        # The dispatch ref --solver descends (None for a leaf): read from the spec, the one source.
        "inner_param": spec.inner_param,
        "share_tag_format": spec.share_tag_format,
        "module_rng": spec.module_rng,
        "order_sensitive": spec.order_sensitive,
        "arm_budgets_ms": dict(spec.arm_budgets_ms),
        "params_schema": spec.params_cls.json_schema(),
        "defaults": spec.params_cls.from_mapping(None, where=where).to_dict(),
    }


def _component_entry(spec: ComponentKind) -> dict[str, Any]:
    return {
        "default": spec.default,
        "description": spec.description,
        "policies": [_component_policy(spec.kind, name) for name in ref_names(spec.kind)],
    }


def _component_policy(kind: str, name: str) -> dict[str, Any]:
    target = ref_target(kind, name, where=f"{kind} component catalog")
    where = f"defaults of {kind} component {name}"
    return {
        "name": target.name,
        "version": target.version,
        "description": target.description,
        "params_schema": target.params_cls.json_schema(),
        "defaults": target.params_cls.from_mapping(None, where=where).to_dict(),
    }
