"""
Shared-pool cooperation — framework-owned market primitives.

Pure, in-memory data structures for the pooled-planner design
(``docs/shared_pool_cooperation_plan.md`` §3.5, §6.1). This module is
**not yet wired into the runtime path** (``assignment/app.py``) — Phase 2 of
the plan builds it standalone so it can be tested in isolation before the
pooled planner host (Phase 4) starts calling it. It must never be imported
by, or change the behaviour of, the existing ``partitioned`` code path.

``PoolMarket`` is the sole custodian of pool membership, per-tick
contribution (which orders were placed into which pools, by whom) and
allocation state (which orders/trucks are already committed this tick). It
never mutates a truck or order document — callers pass plain ids/haulier ids,
never the mutable dicts themselves, which makes ownership-immutability
(I-P3) true by construction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

DERIVED_POOL_PREFIX = "p:"


@dataclass(frozen=True)
class Pool:
    id: str
    members: FrozenSet[str]


@dataclass(frozen=True)
class Bid:
    haulier_id: str  # bidder
    order_id: str
    truck_id: str
    cost_km: float  # bidder's own cost for this pair
    pool_id: Optional[str]  # None => own private order, never contested
    round: int


@dataclass(frozen=True)
class Award:
    order_id: str
    truck_id: str
    carrier_haulier_id: str
    # Optional because ``arbitration`` fills it from ``owner_of(order_id)``, whose
    # contract returns ``Optional[str]``. The host never produces None (it only ever
    # looks up ids it read from the input orders), but a custom rule could, and the
    # annotation must not lie about it (review F15).
    owner_haulier_id: Optional[str]
    cost_km: float
    pool_id: Optional[str]
    round: int


def _pool_from_entry(entry: object) -> Optional[Pool]:
    """Defensively coerce one ``{"id", "members"}``-shaped entry into a ``Pool``.

    Returns ``None`` (never raises) for anything malformed: not a dict,
    missing/empty id, missing/non-iterable members, or fewer than 2 distinct
    (non-empty, string) members.
    """
    if not isinstance(entry, dict):
        return None
    pid = entry.get("id")
    if not isinstance(pid, str) or not pid:
        return None
    members_raw = entry.get("members")
    if not isinstance(members_raw, (list, tuple, set, frozenset)):
        return None
    members = frozenset(str(m) for m in members_raw if isinstance(m, str) and m)
    if len(members) < 2:
        return None
    return Pool(id=pid, members=members)


def _pools_from_edges(edges: object) -> Tuple[Pool, ...]:
    """One 2-member pool per edge, derived id ``"p:" + "+".join(sorted(members))``."""
    if not isinstance(edges, (list, tuple)):
        return ()
    pools: Dict[str, Pool] = {}
    for edge in edges:
        if not isinstance(edge, (list, tuple)) or len(edge) != 2:
            continue
        a, b = edge
        if not isinstance(a, str) or not isinstance(b, str) or not a or not b or a == b:
            continue
        members = frozenset({a, b})
        if len(members) < 2:
            continue
        pid = DERIVED_POOL_PREFIX + "+".join(sorted(members))
        pools[pid] = Pool(id=pid, members=members)
    return tuple(sorted(pools.values(), key=lambda p: p.id))


def pools_from_structure(structure: Optional[dict]) -> Tuple[Pool, ...]:
    """Compiled ``structure['pools']`` when the key is PRESENT (even if empty),
    else one 2-member pool per ``structure['edges']``. Empty tuple when
    ``structure`` is ``None``, not a dict, or carries neither pools nor edges.

    Result is sorted by pool id and fully deterministic. Never raises —
    malformed pool/edge entries are dropped rather than propagated.
    """
    try:
        if not isinstance(structure, dict):
            return ()
        raw_pools = structure.get("pools")
        # An EXPLICITLY PRESENT ``pools`` key is authoritative even when empty
        # (review S1). Treating ``pools: []`` as "absent" and silently falling back
        # to ``edges`` would hand an operator who wrote it to mean "no pools" a full
        # edge-derived market. Unreachable through the shipped compiler — which
        # derives pools from edges, so both are empty together — but §8 actively
        # invites hand-edited bundles ("run old bundles pooled without recompiling").
        if isinstance(raw_pools, list):
            pools: Dict[str, Pool] = {}
            for entry in raw_pools:
                pool = _pool_from_entry(entry)
                if pool is None:
                    logger.warning("pools_from_structure: dropping malformed pool entry %r", entry)
                    continue
                if pool.id in pools:
                    # Silent last-wins was the tell: a MALFORMED entry warned while a
                    # duplicate id vanished quietly (review F11). Datagen suffixes
                    # duplicates, so this only bites hand-edited/legacy bundles.
                    logger.warning(
                        "pools_from_structure: duplicate pool id %r — keeping the LAST "
                        "definition (%r) and discarding the earlier one (%r)",
                        pool.id, sorted(pool.members), sorted(pools[pool.id].members),
                    )
                pools[pool.id] = pool
            return tuple(sorted(pools.values(), key=lambda p: p.id))
        return _pools_from_edges(structure.get("edges"))
    except Exception:  # pragma: no cover - defensive backstop, must never raise
        logger.warning("pools_from_structure: failed to parse structure %r", structure, exc_info=True)
        return ()


class PoolMarket:
    """Framework custodian of pool membership + one tick's contribution and
    commit state. Never mutates a truck/order document; only ever sees ids.
    """

    def __init__(self, pools: Sequence[Pool]) -> None:
        by_id: Dict[str, Pool] = {}
        for pool in pools:
            by_id[pool.id] = pool
        self._pools: Dict[str, Pool] = by_id
        # order_id -> sorted tuple of pool ids it has been placed into.
        self._contributions: Dict[str, Tuple[str, ...]] = {}
        # order_id -> the haulier_id recorded as owner on first contribution.
        self._contribution_owner: Dict[str, str] = {}
        self._committed_order_ids: Set[str] = set()
        self._committed_truck_ids: Set[str] = set()
        self._awards: List[Award] = []

    @classmethod
    def from_structure(cls, structure: Optional[dict]) -> "PoolMarket":
        return cls(pools_from_structure(structure))

    @property
    def is_empty(self) -> bool:
        return not self._pools

    def pools_for(self, haulier_id: str) -> Tuple[Pool, ...]:
        return tuple(
            sorted(
                (p for p in self._pools.values() if haulier_id in p.members),
                key=lambda p: p.id,
            )
        )

    def member_of(self, haulier_id: str, pool_id: str) -> bool:
        pool = self._pools.get(pool_id)
        return pool is not None and haulier_id in pool.members

    # -- contribution --------------------------------------------------

    def contribute(self, order_id: str, owner_haulier_id: str, pool_ids: Sequence[str]) -> int:
        """Place ``order_id`` (owned by ``owner_haulier_id``) into the named
        pools. Fail-soft (plan §4.1 step 4): every invalid placement is
        dropped with a ``logging.warning`` — this method never raises.

        Validity, per placement:
        - the named pool must exist;
        - ``owner_haulier_id`` must be a member of it.

        Additionally, once an order has been contributed under a given
        owner, a later call naming a *different* ``owner_haulier_id`` for
        the same ``order_id`` is rejected outright (0 accepted, logged) —
        this side map is the only ownership record ``PoolMarket`` keeps, and
        it must never silently flip. Real order ownership is the
        framework's, established before ``contribute`` is ever called;
        this is a consistency backstop, not the source of truth.

        Returns the count of placements accepted. Never mutates any
        order/truck document — it only ever records ids in a side map
        (I-P1, I-P3).
        """
        recorded_owner = self._contribution_owner.get(order_id)
        if recorded_owner is not None and recorded_owner != owner_haulier_id:
            logger.warning(
                "PoolMarket.contribute: %r already contributed by owner %r, "
                "rejecting conflicting owner %r",
                order_id, recorded_owner, owner_haulier_id,
            )
            return 0

        accepted: List[str] = []
        for pid in pool_ids:
            pool = self._pools.get(pid)
            if pool is None:
                logger.warning(
                    "PoolMarket.contribute: unknown pool %r for order %r (owner %r)",
                    pid, order_id, owner_haulier_id,
                )
                continue
            if owner_haulier_id not in pool.members:
                logger.warning(
                    "PoolMarket.contribute: owner %r is not a member of pool %r "
                    "(order %r)",
                    owner_haulier_id, pid, order_id,
                )
                continue
            accepted.append(pid)

        if not accepted:
            return 0

        existing = self._contributions.get(order_id, ())
        self._contributions[order_id] = tuple(sorted(set(existing) | set(accepted)))
        self._contribution_owner[order_id] = owner_haulier_id
        return len(accepted)

    def visible_pooled_order_ids(self, haulier_id: str) -> FrozenSet[str]:
        """Order ids contributed to any pool ``haulier_id`` is a member of.

        Deliberately **excludes** orders ``haulier_id`` itself owns/contributed
        — those are its own orders and the planner host is expected to add
        them to a company's visible set separately (they are not "pooled
        foreign orders"; §4.1 step 5.1 distinguishes ``own uncommitted`` from
        ``visible pooled``). A caller that also wants a company's own
        contributed orders back should read them from its own order list,
        not from this method.
        """
        out: Set[str] = set()
        for order_id, pool_ids in self._contributions.items():
            if self._contribution_owner.get(order_id) == haulier_id:
                continue
            for pid in pool_ids:
                if self.member_of(haulier_id, pid):
                    out.add(order_id)
                    break
        return frozenset(out)

    def pool_of_order(self, order_id: str, viewer_haulier_id: str) -> Optional[str]:
        """A pool id that both contains ``viewer_haulier_id`` and holds
        ``order_id``. Deterministic (lowest qualifying pool id) when several
        qualify; ``None`` if not visible to the viewer at all (including when
        the viewer is the order's own owner and only owner-only pools hold
        it — visibility here is purely membership + contribution, mirroring
        what ``visible_pooled_order_ids``/direct callers need to look up).
        """
        pool_ids = self._contributions.get(order_id, ())
        candidates = sorted(pid for pid in pool_ids if self.member_of(viewer_haulier_id, pid))
        return candidates[0] if candidates else None

    def eligible_carriers(self, order_id: str, owner_haulier_id: str) -> FrozenSet[str]:
        """Every haulier permitted to carry ``order_id``: its owner, plus the members
        of every pool the order has been contributed to.

        Framework-owned (visibility is a world fact, plan §1). Used to build ONE
        candidate set per order over the UNION of eligible fleets, which is what
        makes pooled candidate generation match ``partitioned`` instead of drawing
        ``per_order`` trucks per company (plan §13.4 FIX-2 / F4).
        """
        carriers = {owner_haulier_id}
        for pid in self._contributions.get(order_id, ()):
            pool = self._pools.get(pid)
            if pool is not None:
                carriers |= set(pool.members)
        return frozenset(carriers)

    def is_contributed(self, order_id: str) -> bool:
        return bool(self._contributions.get(order_id))

    # -- allocation state -----------------------------------------------

    def commit(self, award: Award) -> bool:
        """Record ``award``. Returns ``False`` (no state change) if either
        ``award.order_id`` or ``award.truck_id`` is already committed this
        tick (I-P2) — including committing the exact same award twice.
        """
        if award.order_id in self._committed_order_ids or award.truck_id in self._committed_truck_ids:
            return False
        self._committed_order_ids.add(award.order_id)
        self._committed_truck_ids.add(award.truck_id)
        self._awards.append(award)
        return True

    def is_order_free(self, order_id: str) -> bool:
        return order_id not in self._committed_order_ids

    def is_truck_free(self, truck_id: str) -> bool:
        return truck_id not in self._committed_truck_ids

    @property
    def awards(self) -> Tuple[Award, ...]:
        return tuple(self._awards)
