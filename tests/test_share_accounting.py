"""Framework share accounting: one computation, two wire formats (plan round 2 §2.5, §5 C2).

What is locked:
- ``v1`` rendering of ``account_shares`` over a tick's committed pairs reproduces EVERY
  stored partitioned share tag of the dispatch golden fixture, and ``audited``
  rendering reproduces every stored pooled tag (only ``pool_id``/``market_round``,
  solver provenance, are fed from the stored tags). The candidates are rebuilt from
  the stored tick state with ``filter_assignable_trucks``, and cost/feasibility come
  from ``DispatchWorld.from_profile`` -- exactly the inputs work item C3 wires;
- the non-finite rules: ``v1`` keeps the pre-v2 ``-inf`` benefit (byte identity),
  ``audited`` never lets a non-finite number through;
- the owner reserve is the minimum over the owner's FREE (uncommitted by any pair of
  the tick), feasible (``own_allowed``) candidate trucks;
- only whitelisted ``pair_notes`` keys reach a tag; self-serves and haulier-less
  pairs are never shares.

Why: C3 moves the share accounting out of the two topology code paths into this one
framework function. The golden replay is the proof that the move is value-identical
before any runtime switches to it (risk 1 of the plan: the owner-truck set and the
committed set must be equivalent to the old per-bucket scan).
"""

from __future__ import annotations

import gzip
import json
import math

import pytest

from apps.container_logistics.assignment import accounting
from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.policies.dispatch.world import DispatchWorld
from tests.golden.capture_dispatch_golden import FIXTURE_PATH
from tests.test_assignment_cooperation import _order, _truck

with gzip.open(FIXTURE_PATH, "rb") as _fh:
    _FIXTURE = json.loads(_fh.read())


def _cases(topology):
    return [c for c in _FIXTURE["cases"] if c["profile"]["planner"]["topology"] == topology]


def _tick_inputs(case):
    """``(candidates, pairs, world)`` exactly as ``AssignmentApp.assign`` sees them."""
    state = _FIXTURE["tick_states"][case["tick_state"]]
    prof = case["profile"]
    respect = bool(prof.get("respect_truck_online_state", True))
    reject = bool(prof.get("reject_if_active_haul_trip", True))
    busy = {str(x) for x in state["busy_ids"]} if reject else set()
    candidates = assign_constraints.filter_assignable_trucks(
        state["trucks"], busy, respect_truck_online_state=respect,
        reject_if_active_haul_trip=reject, online_state_name="online",
    )
    trucks, orders = {}, {}
    for t in candidates:
        trucks.setdefault(str(t["_id"]), t)
    for o in state["orders"]:
        orders.setdefault(str(o["_id"]), o)
    pairs = [(trucks[tid], orders[oid]) for tid, oid in case["expected"]["pairs"]]
    world = DispatchWorld.from_profile(prof, accounting_params=prof.get("solver_params") or {})
    return candidates, pairs, world


def _rendered(case, fmt, pair_notes=None):
    candidates, pairs, world = _tick_inputs(case)
    accounts = accounting.account_shares(pairs, candidates=candidates, cost=world.accounting_cost,
                                         own_allowed=world.own_allowed)
    tags = accounting.render_share_tags(accounts, fmt=fmt, pair_notes=pair_notes)
    rows = sorted(([list(k), t] for k, t in tags.items()), key=lambda r: (r[0][0], r[0][1]))
    return json.loads(json.dumps(rows))


def test_v1_render_reproduces_every_stored_partitioned_golden_share_tag():
    """Verified to have teeth: dropping the committed-set skip in ``owner_reserve_km``,
    dropping the ``carrier != owner`` test, and rendering ``benefit_km`` as
    ``round(awarded - reserve, 3)`` each failed this test."""
    with_shares = 0
    for case in _cases("partitioned"):
        expected = case["expected"]["share_tags"]
        assert _rendered(case, "v1") == expected, f"{case['case_id']}: v1 tags differ"
        with_shares += bool(expected)
    assert with_shares == 13, "the fixture's partitioned share-tag coverage changed"


def test_audited_render_reproduces_stored_pooled_tags_except_market_notes():
    """Verified to have teeth: rounding the awarded cost before computing the benefit
    (instead of after), and dropping the committed-set skip in ``owner_reserve_km``,
    each failed this test."""
    with_shares = 0
    for case in _cases("pooled"):
        expected = case["expected"]["share_tags"]
        notes = {(k[0], k[1]): {"pool_id": t["pool_id"], "market_round": t["market_round"]}
                 for k, t in expected}
        assert _rendered(case, "audited", notes) == expected, f"{case['case_id']}: audited tags differ"
        with_shares += bool(expected)
    assert with_shares == 13, "the fixture's pooled share-tag coverage changed"


def _world(mtt=None):
    return DispatchWorld.from_profile({"max_travel_time_pickup": mtt}, accounting_params={})


def _account(pairs, candidates, world=None):
    world = world or _world()
    return accounting.account_shares(pairs, candidates=candidates, cost=world.accounting_cost,
                                     own_allowed=world.own_allowed)


def test_v1_non_finite_awarded_cost_renders_minus_inf():
    """The pre-v2 partitioned arithmetic, kept for byte identity (FIX-5 defect).

    Verified to have teeth: passing the benefit through ``finite_or_none`` in ``_v1_tag``
    failed this test.
    """
    carrier = {"_id": "tb", "state": "online", "haulier_id": "b", "profile": {"haulier_id": "b"}}  # no location
    own = _truck("ta", "a", 103.85, 1.30)
    order = _order("oa", "a", 103.86, 1.30)
    (acc,) = _account([(carrier, order)], [carrier, own])
    assert acc.awarded_cost_km == math.inf and acc.owner_reserve_km is not None
    tag = accounting.render_share_tags([acc], fmt="v1")[("tb", "oa")]
    assert tag == {"shared": True, "owner_haulier_id": "a", "carrier_haulier_id": "b",
                   "benefit_km": -math.inf}
    assert list(tag) == ["shared", "owner_haulier_id", "carrier_haulier_id", "benefit_km"]


def test_audited_non_finite_renders_none():
    """Verified to have teeth: rendering ``awarded_cost_km`` with ``round(acc.awarded_cost_km,
    3)`` (no ``finite_or_none``) failed this test with ``inf``."""
    carrier = {"_id": "tb", "state": "online", "haulier_id": "b", "profile": {"haulier_id": "b"}}
    own = _truck("ta", "a", 103.85, 1.30)
    order = _order("oa", "a", 103.86, 1.30)
    (acc,) = _account([(carrier, order)], [carrier, own])
    tag = accounting.render_share_tags([acc], fmt="audited")[("tb", "oa")]
    assert tag["awarded_cost_km"] is None and tag["benefit_km"] is None
    assert tag["owner_reserve_km"] == round(acc.owner_reserve_km, 3)
    assert list(tag) == ["shared", "owner_haulier_id", "carrier_haulier_id", "benefit_km", "pool_id",
                         "awarded_cost_km", "owner_reserve_km", "market_round"]
    json.dumps(tag, allow_nan=False)  # no non-finite number crosses the boundary


def test_raising_cost_is_accounted_as_infinite():
    """Same as ``claim._cost_km``: a cost function that raises is an infinite cost.

    Verified to have teeth: letting the exception propagate failed this test.
    """
    carrier, order = _truck("tb", "b", 103.85, 1.30), _order("oa", "a", 103.85, 1.30)

    def cost(t, o):
        raise RuntimeError("boom")

    (acc,) = accounting.account_shares([(carrier, order)], candidates=[carrier], cost=cost,
                                       own_allowed=lambda t, o: True)
    assert acc.awarded_cost_km == math.inf and acc.owner_reserve_km is None


def test_reserve_is_min_over_owner_free_feasible_trucks():
    """Verified to have teeth: dropping the committed-set skip in ``owner_reserve_km``
    (it picked the committed ``a_near``), and dropping the ``own_allowed`` filter (it
    picked the infeasible 20ft ``a_small``), each failed this test."""
    order = {**_order("oa", "a", 103.850, 1.30), "order_size": "40ft"}
    other = _order("oa2", "a", 103.900, 1.30)
    carrier = _truck("tb", "b", 103.851, 1.30)       # the partner truck serving oa
    a_near = _truck("a_near", "a", 103.850, 1.30)    # cheapest, but committed to oa2
    a_small = _truck("a_small", "a", 103.8502, 1.30)
    a_small["profile"] = {**a_small["profile"], "truck_size": "20ft"}  # cannot carry 40ft
    a_mid = _truck("a_mid", "a", 103.860, 1.30)      # the owner's best free feasible truck
    a_far = _truck("a_far", "a", 103.900, 1.30)
    b_other = _truck("b_other", "b", 103.8501, 1.30)  # cheap, but not the owner's
    candidates = [carrier, a_near, a_small, a_mid, a_far, b_other]
    world = _world()
    assert not world.own_allowed(a_small, order), "probe: a_small must be infeasible for the order"
    assert world.accounting_cost(a_small, order) < world.accounting_cost(a_mid, order)

    (acc,) = _account([(carrier, order), (a_near, other)], candidates)  # (a_near, oa2): self-serve
    assert acc.owner_reserve_km == pytest.approx(world.accounting_cost(a_mid, order))
    assert (acc.owner_haulier_id, acc.carrier_haulier_id) == ("a", "b")
    assert acc.awarded_cost_km == pytest.approx(world.accounting_cost(carrier, order))

    # No free feasible own truck at all -> None (v1 and audited both render None).
    (none_acc,) = _account([(carrier, order)], [carrier, b_other])
    assert none_acc.owner_reserve_km is None
    assert accounting.render_share_tags([none_acc], fmt="v1")[("tb", "oa")]["benefit_km"] is None


def test_only_whitelisted_pair_notes_reach_the_tag():
    """Verified to have teeth: merging the raw note over the computed tag
    (``{**tag, **raw}``) let the note's ``benefit_km``/``shared`` through and failed this test."""
    carrier, own = _truck("tb", "b", 103.85, 1.30), _truck("ta", "a", 103.86, 1.30)
    order = _order("oa", "a", 103.85, 1.30)
    (acc,) = _account([(carrier, order)], [carrier, own])
    notes = {("tb", "oa"): {"pool_id": "p:a+b", "market_round": 2, "benefit_km": 999.0,
                            "shared": False, "owner_reserve_km": -1}}
    tag = accounting.render_share_tags([acc], fmt="audited", pair_notes=notes)[("tb", "oa")]
    assert (tag["pool_id"], tag["market_round"]) == ("p:a+b", 2)
    assert tag["shared"] is True and tag["benefit_km"] != 999.0 and tag["owner_reserve_km"] != -1
    assert tag["benefit_km"] == round(acc.owner_reserve_km - acc.awarded_cost_km, 3)
    bare = accounting.render_share_tags([acc], fmt="audited")[("tb", "oa")]
    assert (bare["pool_id"], bare["market_round"]) == (None, None)
    v1 = accounting.render_share_tags([acc], fmt="v1", pair_notes=notes)[("tb", "oa")]
    assert "pool_id" not in v1, "v1 never carries market notes"
    with pytest.raises(ValueError, match="'v1' or 'audited'"):
        accounting.render_share_tags([acc], fmt="v2")


def test_self_serves_and_haulierless_pairs_are_not_shares_and_cost_nothing():
    """Verified to have teeth: dropping the ``carrier != owner`` test made the self-serve
    a share."""
    calls = []

    def own_allowed(t, o):
        calls.append((t["_id"], o["_id"]))
        return True

    ta, oa = _truck("ta", "a", 103.85, 1.30), _order("oa", "a", 103.85, 1.30)
    nobody = {"_id": "tx", "state": "online", "profile": {}}
    accounts = accounting.account_shares([(ta, oa), (nobody, _order("ob", "b", 103.85, 1.30))],
                                         candidates=[ta, nobody], cost=lambda t, o: 1.0,
                                         own_allowed=own_allowed)
    assert accounts == [] and calls == [], "no cross pair: no owner index, no feasibility call"
