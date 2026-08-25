"""Agent + policy registries.

- `AGENT_REGISTRY`  : role -> Agent class (which generator builds a role's agents).
- `POLICY_REGISTRY` : (role, type) -> Policy class — an **explicit dict** of the built-in
  policies (you see them all at a glance). External policies register via the `register`
  decorator (the extension seam — `@register("order", "poisson")`).

`known_policies()` reads the registry and is the single source of truth the CLI / TUI /
dashboard bind to (via the `known-policies` control command), so a new policy is instantly
selectable everywhere.
"""

from __future__ import annotations

from typing import Callable

from .base import Policy, PolicyContext
from .order import HistoricalOrderPolicy, OrderAgent, RandomOrderPolicy
from .truck import HistoricalTruckPolicy, RandomTruckPolicy, TruckAgent
from .facility import AllocateFacilityPolicy, FacilityAgent, RandomFacilityPolicy
from .engine import (
    AnalyticsAgent,
    AnalyticsPolicy,
    AssignmentAgent,
    AssignmentPolicy,
    OrderLifecycleAgent,
    OrderLifecyclePolicy,
)

# role -> Agent class
AGENT_REGISTRY: dict[str, type] = {
    "order": OrderAgent,
    "truck": TruckAgent,
    "facility": FacilityAgent,
    "assignment": AssignmentAgent,
    "analytics": AnalyticsAgent,
    "order_lifecycle": OrderLifecycleAgent,
}

# (role, type) -> Policy class. Built-ins listed explicitly; **two parents per agent by
# data source** (random / given-data). The matrix/curve are *distributions* the given-data
# policy samples — NOT policies of their own (see policy_strategy_refactor_plan.md §Design).
POLICY_REGISTRY: dict[tuple[str, str], type] = {
    ("order", "random"): RandomOrderPolicy,                # random data (uniform OD + time)
    ("order", "historical"): HistoricalOrderPolicy,        # default — authored matrix OR learned from records
    ("truck", "default"): HistoricalTruckPolicy,
    ("truck", "random"): RandomTruckPolicy,
    ("truck", "historical"): HistoricalTruckPolicy,
    ("facility", "allocate"): AllocateFacilityPolicy,      # demand-proportional sites
    ("facility", "random"): RandomFacilityPolicy,          # uniform code mix
    ("assignment", "default"): AssignmentPolicy,
    ("analytics", "default"): AnalyticsPolicy,
    ("order_lifecycle", "default"): OrderLifecyclePolicy,
}

# Deprecated policy-type aliases (old spec.json still compiles; normalized on recompile).
# "matrix" was never a real policy — a matrix is a Distribution the historical policy samples.
POLICY_ALIASES: dict[tuple[str, str], str] = {
    ("order", "matrix"): "historical",
}

# Role default policy type (the one that reproduces today's behavior).
_ROLE_DEFAULT = {
    "truck": "default",
    "order": "historical",
    "facility": "allocate",
    "assignment": "default",
    "analytics": "default",
    "order_lifecycle": "default",
}


def canonical_policy_type(role: str, ptype: str) -> str:
    """Map a deprecated alias to its canonical type (identity if not an alias)."""
    return POLICY_ALIASES.get((role, ptype), ptype)


def register(role: str, name: str) -> Callable[[type], type]:
    """Extension seam: add a new policy so it's selectable in spec.json by ``name``.

        @register("order", "poisson")
        class PoissonOrderPolicy(OrderPolicy): ...
    """

    def _wrap(cls: type) -> type:
        cls.role, cls.name = role, name
        POLICY_REGISTRY[(role, name)] = cls
        return cls

    return _wrap


def default_policy_name(role: str) -> str:
    return _ROLE_DEFAULT.get(role, "default")


def known_policies(role: str | None = None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for (r, name) in POLICY_REGISTRY:
        if role is not None and r != role:
            continue
        out.setdefault(r, []).append(name)
    return {r: sorted(v) for r, v in out.items()}


def resolve_policy(role: str, policy_cfg: dict | None, ctx: PolicyContext) -> Policy:
    cfg = policy_cfg if isinstance(policy_cfg, dict) else {}
    ptype = canonical_policy_type(role, str(cfg.get("type") or default_policy_name(role)))
    cls = POLICY_REGISTRY.get((role, ptype))
    if cls is None:
        avail = sorted(n for (r, n) in POLICY_REGISTRY if r == role)
        raise ValueError(f"Unknown {role} policy type {ptype!r}. Available: {avail}")
    return cls(cfg, ctx)


def agent_class(role: str) -> type:
    return AGENT_REGISTRY[role]
