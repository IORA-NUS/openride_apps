"""The registered dispatch names, as the spec-exact messages list them. Tests only.

Responsibility: build the name lists that messages 1, 2 and 11
(``docs/plans/policy_slots_round2_whole_tick.md`` §3.7) embed FROM THE REGISTRY, so a
test pins the wording of a message without pinning which leaves exist. Registering a
new leaf (a NetworkFlow solver, a test injection) then changes the expected text and
the actual text together, instead of breaking every exact-text test (final review
MINOR-7). ``tests/test_policy_registry_names.py`` proves it with an extra leaf.

Each list is read when the function is CALLED, so a test inside
``isolated_registry()`` sees that registry.
"""

from __future__ import annotations

from apps.container_logistics.policies.framework.components import ref_names
from apps.container_logistics.policies.framework.registry import list_policies

__all__ = ["dispatch_names", "leaf_names", "component_names"]


def dispatch_names() -> str:
    """Every registered dispatch policy, sorted, comma-separated (message 1)."""
    return ", ".join(sorted(spec.name for spec in list_policies("dispatch")))


def leaf_names() -> str:
    """Every registered per-scope solver (no ``inner`` of its own), sorted (messages 2 and 11)."""
    return ", ".join(sorted(spec.name for spec in list_policies("dispatch") if not spec.is_composite))


def component_names(kind: str) -> str:
    """Every registered component of ``kind``, sorted (message 3's ``Registered:`` list)."""
    return ", ".join(sorted(ref_names(kind)))
