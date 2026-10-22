"""Re-export shim: the legacy dispatch translation moved to ``policies/compat/legacy.py``.

Kept so existing imports of ``policies.dispatch.legacy`` keep working; it is removed
together with the other compat re-export shims. New code imports
``apps.container_logistics.policies.compat.legacy``.
"""

from __future__ import annotations

from apps.container_logistics.policies.compat.legacy import *  # noqa: F401,F403

__all__ = [
    "LEGACY_LEAF_DEFAULT",
    "PARTITIONED",
    "POOLED_MARKET",
    "LEGACY_TOPOLOGIES",
    "LEGACY_MAX_ROUNDS_DEFAULT",
    "LEGACY_MAX_ROUNDS_RANGE",
    "MARKET_ROLES",
    "legacy_topology",
    "legacy_market_params",
    "wrap_legacy_leaf",
    "topology_echo",
    "market_echo",
    "check_authored_planner",
    "POLICIES_FORMAT",
    "POLICIES_FORMAT_KEY",
    "check_policies_format",
]
