"""The single policy catalog: every slot, its status, and its selectable policies.

:func:`policy_catalog` is the JSON payload served as ``known-policies.slots`` (plan
``docs/plans/policy_slots_p0_p3.md`` §4 P0.8) and the only source the CLI and the
dashboard read slot/policy lists from -- no hand-copied lists elsewhere.

Leaf module (plan §2.7): the legacy market registries live under
``apps.container_logistics.assignment`` and are imported locally, at call time, in
:func:`_legacy_names` only.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from .registry import PolicySpec, list_policies
from .slots import SLOTS, SlotSpec

logger = logging.getLogger(__name__)

__all__ = ["policy_catalog"]

# Legacy slot -> (module, registry dict) whose keys are the selectable names.
_LEGACY_NAME_SOURCES: dict[str, tuple[str, str]] = {
    "market.offer": ("apps.container_logistics.assignment.policy.offer", "OFFER_REGISTRY"),
    "market.claim": ("apps.container_logistics.assignment.policy.claim", "CLAIM_REGISTRY"),
}


def policy_catalog() -> dict[str, Any]:
    """Return a JSON-serialisable description of every slot, in ``SLOTS`` order.

    Implemented slots list their registered policies (schema, defaults, missing
    requirements); legacy slots list their names read live from the legacy
    registries; planned slots carry only the phase they land in. Raises whatever
    discovery raises (a broken policy module must not silently vanish).
    """
    return {name: _slot_entry(slot) for name, slot in SLOTS.items()}


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
    if slot.status == "legacy":
        return {
            "kind": slot.kind,
            "status": slot.status,
            "default": slot.default,
            "names": _legacy_names(slot.name),
            "configured_via": slot.configured_via,
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
        "order_sensitive": spec.order_sensitive,
        "params_schema": spec.params_cls.json_schema(),
        "defaults": spec.params_cls.from_mapping(None, where=where).to_dict(),
    }


def _legacy_names(slot_name: str) -> list[str]:
    """Return the sorted names of a legacy slot's registry, read at call time.

    Local import on purpose: the import-cycle rule (``apps.container_logistics.policies``
    docstring) forbids a module-top import of ``assignment.*`` here.
    """
    module_name, attribute = _LEGACY_NAME_SOURCES[slot_name]
    registry = getattr(importlib.import_module(module_name), attribute)
    return sorted(registry)
