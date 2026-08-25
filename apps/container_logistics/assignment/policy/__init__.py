"""SOLVER-side market policies for the shared-pool cooperation design.

Two registries live here, both mirroring ``solver/__init__.py``:

- :mod:`.offer` — which of my own orders do I contribute, and to which pools;
- :mod:`.claim` — which visible pooled orders do I bid on, and at what cost.

Both are *company* decisions (plan §1). The *world* decisions — pool
membership and conflict resolution — live in ``assignment/pools.py`` and
``assignment/arbitration.py`` respectively and are never reachable from here.

Pure: this package is not yet wired into ``assignment/app.py`` (Phase 4).
"""

from __future__ import annotations

from .claim import (
    CLAIM_REGISTRY,
    DEFAULT_CLAIM_POLICY,
    BaseClaimPolicy,
    ClaimAllPlannedPolicy,
    ClaimIfGainExceedsPolicy,
    ClaimNonePolicy,
    get_claim_policy,
)
from .offer import (
    DEFAULT_OFFER_POLICY,
    OFFER_REGISTRY,
    BaseOfferPolicy,
    OfferAllPolicy,
    OfferNonePolicy,
    OfferSparePolicy,
    get_offer_policy,
)

__all__ = [
    "BaseOfferPolicy",
    "OfferAllPolicy",
    "OfferNonePolicy",
    "OfferSparePolicy",
    "OFFER_REGISTRY",
    "DEFAULT_OFFER_POLICY",
    "get_offer_policy",
    "BaseClaimPolicy",
    "ClaimAllPlannedPolicy",
    "ClaimNonePolicy",
    "ClaimIfGainExceedsPolicy",
    "CLAIM_REGISTRY",
    "DEFAULT_CLAIM_POLICY",
    "get_claim_policy",
]
