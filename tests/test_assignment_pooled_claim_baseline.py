"""`own_pairs` is the company's own work, not just its uncontested work (FIX-4 / F5).

The host used ONE test — ``owner == hid and not is_contributed(oid)`` — to answer two
different questions: *"is this pair uncontested, so it can be committed directly?"*
and *"is this pair my own work, so it is the opportunity-cost baseline?"*. Under the
DEFAULT ``OfferAll`` every own order is contributed, so the second answer was always
"no" and ``own_pairs`` was **always empty**. ``ClaimIfGainExceeds`` therefore had no
baseline and ``min_gain_km`` — the knob driving experiment E6 — silently did nothing.

The reviewer measured it: ``min_gain_km`` of 0.0, 5.0, 1000.0 and 1e9 all produced an
identical allocation with zero shares. Its unit test passed only because it called
the policy directly with a hand-constructed non-empty ``own_pairs`` — a shape the
host could not produce. That is CLAUDE.md §6.15 verbatim, which is why every test
here drives the REAL ``AssignmentApp.assign``.
"""

import pytest

from apps.container_logistics.assignment.policy import claim as claim_mod

from tests.test_assignment_cooperation import _coop, _order, _truck
from tests.test_assignment_pooled import _app, _ids


def _fixture():
    """borax has two trucks, so its planner can hold BOTH a foreign pair and an own
    pair in the same round — which is what makes an opportunity-cost baseline exist.

    Geometry: o_acme at 103.85, o_borax ~11 km east at 103.95.
      t_b1  103.851 -> ~0.1 km to o_acme, ~11 km to o_borax
      t_b2  103.949 -> nearest to o_borax
      t_acme 103.90 -> ~5.6 km to either
    """
    trucks = [
        _truck("t_acme", "acme", 103.90, 1.30),
        _truck("t_b1", "borax", 103.851, 1.30),
        _truck("t_b2", "borax", 103.949, 1.30),
    ]
    orders = [
        _order("o_acme", "acme", 103.85, 1.30),
        _order("o_borax", "borax", 103.95, 1.30),
    ]
    return trucks, orders, _coop([["acme", "borax"]])


def _market(min_gain, **extra):
    return {"claim": {"type": "ClaimIfGainExceeds", "params": {"min_gain_km": min_gain}}, **extra}


def test_min_gain_km_changes_outcome_under_default_offer_all():
    """End-to-end, DEFAULT ``OfferAll``, default rounds: the knob must matter.

    Before FIX-4 every value of ``min_gain_km`` produced the identical allocation.
    Verified to have teeth: reverting ``own_pairs`` to ``own_private_pairs`` makes
    the two allocations below identical again and this fails.
    """
    trucks, orders, coop = _fixture()
    low = _ids(_app(trucks, orders, cooperation=coop, market=_market(0.0)).assign("2020-01-01 08:00:00"))
    high = _ids(_app(trucks, orders, cooperation=coop, market=_market(1e9)).assign("2020-01-01 08:00:00"))
    assert low != high, (
        f"min_gain_km had no effect under the default offer policy: {low} == {high} "
        f"— ClaimIfGainExceeds is inert again"
    )


def test_a_high_min_gain_suppresses_a_companys_cross_claim():
    """Scoped to one round so the suppression is unambiguous: with an impossible
    gain threshold borax claims nothing at all, so acme's order is not carried by
    borax's truck.

    ``max_rounds=1`` is deliberate here and the backstop WILL bind — that is the
    point of the fixture, not an accident. It isolates round 1 from the
    empty-``own_pairs`` fallback documented below.
    """
    trucks, orders, coop = _fixture()

    low = _ids(_app(trucks, orders, cooperation=coop,
                    market=_market(0.0, max_rounds=1)).assign("2020-01-01 08:00:00"))
    high = _ids(_app(trucks, orders, cooperation=coop,
                     market=_market(1e9, max_rounds=1)).assign("2020-01-01 08:00:00"))

    assert ("t_b1", "o_acme") in low, "borax should take the 10.9 km gain when the threshold is 0"
    assert ("t_b1", "o_acme") not in high, "an impossible threshold must suppress the claim"


def test_the_empty_own_pairs_fallback_is_real_and_deliberate():
    """HONESTY GUARD on the above. ``ClaimIfGainExceeds`` treats "I planned no own
    order for this truck" as an UNBOUNDED gain and bids anyway (documented in the
    policy). So a company with no own alternative still claims even at
    ``min_gain_km=1e9``, and across several convergence rounds a company whose own
    work is already committed reaches exactly that state.

    This is why the test above pins ``max_rounds=1``, and why "a huge min_gain_km
    means zero shares" is NOT true in general — the plan's FIX-4 wording implies it
    is. Asserting the real behaviour instead of the convenient one.
    """
    trucks, orders, coop = _fixture()
    app = _app(trucks, orders, cooperation=coop, market=_market(1e9))
    app.assign("2020-01-01 08:00:00")
    assert app._share_tags, (
        "expected the empty-own_pairs fallback to still produce a share across rounds; "
        "if this ever becomes 0 the fallback changed and the docs above are stale"
    )


def test_claim_policy_receives_non_empty_own_pairs_under_offer_all():
    """The direct mechanism guard: the host must actually hand the policy a
    baseline under the DEFAULT offer policy. This is the assertion whose absence
    let F5 ship."""
    seen = []

    class _SpyClaim(claim_mod.ClaimAllPlannedPolicy):
        def bids(self, *, haulier_id, pooled_pairs, own_pairs, **kw):
            seen.append((haulier_id, len(own_pairs), len(pooled_pairs)))
            return super().bids(
                haulier_id=haulier_id, pooled_pairs=pooled_pairs, own_pairs=own_pairs, **kw
            )

    claim_mod.CLAIM_REGISTRY["_SpyClaim"] = _SpyClaim
    try:
        trucks, orders, coop = _fixture()
        app = _app(trucks, orders, cooperation=coop,
                   market={"claim": {"type": "_SpyClaim", "params": {}}})
        app.assign("2020-01-01 08:00:00")
    finally:
        claim_mod.CLAIM_REGISTRY.pop("_SpyClaim", None)

    assert seen, "the claim policy was never invoked"
    assert any(own > 0 for _h, own, _p in seen), (
        f"own_pairs was empty on EVERY call under OfferAll — the F5 conflation is "
        f"back. Calls (haulier, own, pooled): {seen}"
    )


def test_default_claim_policy_allocation_is_pinned():
    """Regression pin for the DEFAULT path.

    FIX-4 re-labels the claim policy's baseline input only; award ROUTING is
    untouched (own-contributed pairs are still bid, not direct-committed), and the
    default ``ClaimAllPlanned`` never reads ``own_pairs`` at all — so the default
    allocation cannot move. The stronger evidence that it did not is that all 84
    pre-existing pooled/market tests passed unchanged across the split; this pin
    just makes a future accidental change to the routing loud.
    """
    trucks, orders, coop = _fixture()
    default = _ids(_app(trucks, orders, cooperation=coop).assign("2020-01-01 08:00:00"))
    assert default == [("t_b1", "o_acme"), ("t_b2", "o_borax")]
    # borax carries acme's order -> exactly one cross-haulier share tag.
    app = _app(trucks, orders, cooperation=coop)
    app.assign("2020-01-01 08:00:00")
    assert set(app._share_tags) == {("t_b1", "o_acme")}
