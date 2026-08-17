"""One stable tie-break construction, shared by every ordering in the pooled path.

**Why this module exists (plan §13.4 FIX-3 / review F3).** The plan specified the
correct pattern for arbitration — a blake2b hash of the tick seed plus the pair's
identity — and then failed to apply that same pattern one layer down, in the greedy
sweep. Seeding a *shuffle* makes it reproducible for a fixed input order; it does
**not** make it independent of input order, which is what I-P5 asserts. The result
was that identical inputs in a different order produced different awards, and even a
different NUMBER of awards, at the same tick seed.

The tie case is the normal case here, not a corner: ``assignment_cost`` clamps with
``max(0.0, base_km - dual_cycle_bonus_km)``, so with the shipped 5.0 km bonus EVERY
dual-cycle-eligible pair within 5 km scores exactly ``0.000``
(``solver_boundary_audit.md`` P5).

Keeping the construction in one place means the arbitration tie-break and the solver
tie-break can never drift apart.
"""

from __future__ import annotations

from hashlib import blake2b


def stable_tiebreak(seed: int, *parts: object) -> int:
    """A stable 64-bit integer from ``seed`` and the given identity ``parts``.

    Depends on no dict ordering, no list position and no solve order, and rotates
    per tick so nothing gets a systematic edge from a fixed hash. Deterministic
    across processes — unlike ``hash()``, it is not PYTHONHASHSEED-dependent.
    """
    payload = "|".join([str(seed), *(str(p) for p in parts)]).encode()
    return int.from_bytes(blake2b(payload, digest_size=8).digest(), "big")


def pair_tiebreak(seed: int, order_id: object, truck_id: object) -> int:
    """Tie-break for a ``(truck, order)`` candidate pair in the greedy sweep."""
    return stable_tiebreak(seed, order_id, truck_id)


__all__ = ["stable_tiebreak", "pair_tiebreak"]
