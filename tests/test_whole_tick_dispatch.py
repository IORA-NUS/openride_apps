"""The whole-tick dispatch runtime (round 2 C3): one context per tick, decompositions as solvers.

What is locked (plan ``docs/plans/policy_slots_round2_whole_tick.md`` §2, §5 C3):

- ``Partitioned`` splits the tick into exactly the groups the pre-v2 inline bucketing
  of ``AssignmentApp.assign`` built (a frozen copy of it is the oracle), and never
  constructs the tick ``Random``; ``PooledMarket`` constructs it exactly once.
- The FRAMEWORK re-checks every committed head against the world rule: a central
  policy that pairs companies the structure does not connect commits nothing that
  tick (message 18), and a sub-scope violation drops only that scope (message 19).
- Selecting ``PooledMarket`` through ``policies`` equals the legacy
  ``planner.topology: pooled`` path byte for byte (pairs, tags, published strings,
  round stats).
- A central leaf serves only world-allowed cross pairs, is accounted in the
  ``audited`` format, and draws nothing from the ``random`` module.
- A crashing market company logs message 20, is counted and skipped; the rebate book
  reaches the leaf through a composite.

Why: C3 moves the decomposition out of the framework into ordinary solvers. The
golden fixture proves the defaults byte-identical; these tests lock the new
framework guarantees (feasibility, accounting, RNG) that the golden cannot see.
"""

from __future__ import annotations

import copy
import json
import logging
import random
from dataclasses import dataclass

import pytest

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.assignment.app import AssignmentApp
from apps.container_logistics.assignment.solver import get_solver
from apps.container_logistics.policies.dispatch import compose
from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.composite import innermost_policy
from apps.container_logistics.policies.dispatch.plan import Plan
from apps.container_logistics.policies.dispatch.profile import build_policy_tree, resolve_dispatch_for_profile
from apps.container_logistics.policies.framework import PolicyParams, policy
from apps.container_logistics.policies.framework.registry import isolated_registry
from tests import test_dispatch_golden_identity as _golden
from tests.golden.harness import build_app, run_case
from tests.test_assignment_cooperation import _StubManager, _StubMessenger, _coop, _order, _truck

_FIXTURE = _golden._FIXTURE
_CASES = {c["case_id"]: c for c in _FIXTURE["cases"]}
_TICKS = _FIXTURE["tick_states"]
_CLOCK = "Wed, 01 Jan 2020 08:00:00 GMT"


def _app(trucks, orders, profile, solver=None):
    """A real ``AssignmentApp`` (``__new__`` harness) on ``profile``; ``solver`` defaults to its tree."""
    app = AssignmentApp.__new__(AssignmentApp)
    app.behavior = {"profile": profile}
    app.run_id = "run_whole_tick"
    app.manager = _StubManager(trucks, orders)
    app.messenger = _StubMessenger()
    app._solver = solver if solver is not None else build_policy_tree(resolve_dispatch_for_profile(profile))
    app._solver_params = profile.get("solver_params") or {}
    app._warned_haulier_block = False
    app._share_tags = {}
    return app


def _base_profile(**extra):
    return {"respect_truck_online_state": True, "reject_if_active_haul_trip": True, **extra}


def _ids(pairs):
    return [(str(t["_id"]), str(o["_id"])) for t, o in pairs]


# --- 1. the bucketing ----------------------------------------------------------------


def _frozen_inline_bucketing(prof, candidates, orders):
    """FROZEN copy of ``AssignmentApp.assign`` lines 615-700 at ``c30fd25`` (the partitioned
    bucketing). Returns ``[(partition_key, [truck ids], [order ids])]`` in solve order."""
    structure = assign_constraints.active_cooperation_structure(prof)
    adjacency = assign_constraints.share_adjacency(structure)
    component_of = {}
    if adjacency and isinstance(structure, dict):
        for idx, members in enumerate(structure.get("components") or []):
            if isinstance(members, (list, tuple)) and len(members) > 1:
                for hid in members:
                    component_of[str(hid)] = idx

    def _partition_key(haulier_id):
        if haulier_id is None:
            return None
        hid = str(haulier_id)
        if hid in component_of:
            return ("component", component_of[hid])
        return ("haulier", hid)

    truck_buckets = {}
    for t in candidates:
        truck_buckets.setdefault(_partition_key(assign_constraints.haulier_of(t)), []).append(t)
    order_buckets = {}
    for o in orders:
        order_buckets.setdefault(_partition_key(assign_constraints.haulier_of(o)), []).append(o)
    out = []
    for partition_key, bucket_orders in order_buckets.items():
        if partition_key is None:
            continue
        bucket_trucks = truck_buckets.get(partition_key)
        if not bucket_trucks:
            continue
        out.append((partition_key, [t["_id"] for t in bucket_trucks], [o["_id"] for o in bucket_orders]))
    return out


@pytest.mark.parametrize("case_id", ["A-greedy-partitioned-chain-spatial-cap500",
                                     "A-greedy-partitioned-no-coop-spatial-cap500"])
def test_partitioned_groups_match_the_frozen_inline_bucketing(case_id, monkeypatch):
    """Verified to have teeth: keying a multi-company component by haulier in
    ``compose.cooperation_groups`` (dropping the ``component_of`` branch) failed the
    chain case. (The skips are locked by the synthetic test below.)"""
    case = _CASES[case_id]
    seen = []
    real = compose.cooperation_groups

    def _spy(ctx):
        groups = real(ctx)
        seen.append((ctx, groups))
        return groups

    monkeypatch.setattr(compose, "cooperation_groups", _spy)
    app = build_app(case["profile"], case["strategy"], _TICKS[case["tick_state"]])
    app.assign(_TICKS[case["tick_state"]]["sim_clock"], 42)
    (ctx, groups), = seen
    got = [(g.key, [t["_id"] for t in g.trucks], [o["_id"] for o in g.orders]) for g in groups]
    want = _frozen_inline_bucketing(case["profile"], ctx.raw_trucks, ctx.raw_orders)
    assert got == want, "the group split differs from the pre-v2 inline bucketing"
    assert len(got) > 1, "vacuous: the tick must have several groups"
    if "chain" in case_id:
        assert any(key[0] == "component" for key, _t, _o in got), "the chain must form a component group"


def test_partitioned_groups_skip_truckless_and_haulierless_orders_like_the_inline_bucketing(monkeypatch):
    """A synthetic tick with an orders-only haulier, a haulier-less order and a solo
    haulier next to a component (the golden ticks have neither skip).

    Verified to have teeth: dropping the empty-truck-bucket skip in
    ``compose.cooperation_groups`` failed this test; so did dropping the ``None``-key skip.
    """
    stray = _truck("t_none", "acme", 103.851, 1.30)
    stray["haulier_id"] = None
    stray["profile"]["haulier_id"] = None
    trucks = [_truck("t_acme", "acme", 103.85, 1.30), _truck("t_borax", "borax", 103.86, 1.30),
              _truck("t_delta", "delta", 103.87, 1.30), stray]
    nobody = _order("o_none", "acme", 103.85, 1.30)
    nobody["haulier_id"] = None
    nobody["profile"]["haulier_id"] = None
    orders = [_order("o_cargo", "cargo", 103.90, 1.30), nobody, _order("o_delta", "delta", 103.87, 1.30),
              _order("o_acme", "acme", 103.85, 1.30), _order("o_echo", "echo", 103.88, 1.30)]
    profile = _base_profile(strategy="GreedyNearest", cooperation=_coop([("acme", "borax")], active="s1"))
    seen = []
    real = compose.cooperation_groups
    monkeypatch.setattr(compose, "cooperation_groups", lambda ctx: seen.append(ctx) or real(ctx))
    app = _app(trucks, orders, profile)
    app.assign(_CLOCK, 2)
    (ctx,) = seen
    got = [(g.key, [t["_id"] for t in g.trucks], [o["_id"] for o in g.orders]) for g in real(ctx)]
    assert got == _frozen_inline_bucketing(profile, ctx.raw_trucks, ctx.raw_orders)
    assert [key for key, _t, _o in got] == [("haulier", "delta"), ("component", 0)], got


# --- 2. RNG construction ---------------------------------------------------------------


@pytest.mark.parametrize("case_id,constructions", [
    ("A-greedy-partitioned-chain-sampled-cap40", 0),
    ("A-random-partitioned-chain-spatial-cap40", 0),
    ("A-greedy-pooled-chain-spatial-cap500", 1),
])
def test_partitioned_never_constructs_the_tick_rng(case_id, constructions, monkeypatch):
    """``ctx.rng`` is lazy: Partitioned never reads it, PooledMarket builds it once.

    Verified to have teeth: building ``random.Random(tick_seed)`` eagerly in
    ``scopes.tick_context`` failed both partitioned cases.
    """
    built = []

    class _Counting(random.Random):
        def __init__(self, *args, **kw):
            built.append(args)
            super().__init__(*args, **kw)

    case = _CASES[case_id]
    app = build_app(case["profile"], case["strategy"], _TICKS[case["tick_state"]])
    monkeypatch.setattr(random, "Random", _Counting)
    app.assign(_TICKS[case["tick_state"]]["sim_clock"], 42)
    assert len(built) == constructions, f"{case_id}: random.Random constructed {len(built)} time(s)"


# --- 3/4. the framework's feasibility and scope checks ----------------------------------


def _chain_abc():
    return _coop([("acme", "borax"), ("borax", "cargo")], active="s1")


@pytest.fixture
def cross_pairer():
    """A central policy that hands the cargo truck the acme order (cargo-acme is not an edge)."""
    with isolated_registry():
        @policy(slot="planner", name="CrossPairer", version="1", information_model="central",
                description="pairs cargo with acme")
        class CrossPairer(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def decide(self, ctx):
                return Plan(chains={"t_cargo": ("o_acme",)})

        yield CrossPairer


def test_disallowed_cross_company_head_is_discarded_by_the_framework(cross_pairer, caplog):
    """Verified to have teeth: building the tick context with ``allowed_raw = lambda t, o:
    True`` (instead of ``world.allowed``) in ``scopes.tick_context`` committed the pair."""
    trucks = [_truck("t_cargo", "cargo", 103.85, 1.30), _truck("t_acme", "acme", 103.95, 1.30)]
    orders = [_order("o_acme", "acme", 103.85, 1.30)]
    profile = _base_profile(policies={"planner": {"type": "CrossPairer"}}, cooperation=_chain_abc())
    app = _app(trucks, orders, profile)
    with caplog.at_level(logging.ERROR):
        assert app.assign(_CLOCK, 7) == []
    assert app._share_tags == {}
    assert app._dispatch_runner(profile).stats["violations"] == 1
    msg = next(r.getMessage() for r in caplog.records if "CONTRACT VIOLATION" in r.getMessage())
    assert msg.startswith(
        "PLANNER POLICY CONTRACT VIOLATION: CrossPairer v1 at step 7 returned an invalid tick plan: "
        "head pair ('t_cargo', 'o_acme') is not allowed. The plan was DISCARDED (nothing committed "
        "this tick). This is a policy bug: run `openride policy validate planner CrossPairer`."), msg


@pytest.fixture
def acme_breaker():
    """A leaf that double-books an order in acme's scope only, and is honest elsewhere."""
    with isolated_registry():
        @policy(slot="planner", name="AcmeBreaker", version="1", information_model="central",
                description="breaks acme's scope")
        class AcmeBreaker(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def decide(self, ctx):
                trucks = [str(t["_id"]) for t in ctx.raw_trucks]
                orders = [str(o["_id"]) for o in ctx.raw_orders]
                if ctx.scope.key == "partition:haulier:acme":
                    return Plan(chains={tid: (orders[0],) for tid in trucks})
                return Plan(chains={tid: (oid,) for tid, oid in zip(trucks, orders)})

        yield AcmeBreaker


def test_scope_violation_discards_only_that_group(acme_breaker, caplog):
    """Verified to have teeth: returning ``Plan(chains={})`` from ``Partitioned.decide``
    when any group is dropped (instead of skipping just that group) failed the borax
    assertion."""
    trucks = [_truck("t_a1", "acme", 103.85, 1.30), _truck("t_a2", "acme", 103.86, 1.30),
              _truck("t_b1", "borax", 103.95, 1.30)]
    orders = [_order("o_a1", "acme", 103.85, 1.30), _order("o_b1", "borax", 103.95, 1.30)]
    profile = _base_profile(policies={"planner": {"type": "Partitioned",
                                                   "params": {"inner": {"type": "AcmeBreaker"}}}})
    app = _app(trucks, orders, profile)
    with caplog.at_level(logging.ERROR):
        assert _ids(app.assign(_CLOCK, 3)) == [("t_b1", "o_b1")]
    assert app._dispatch_runner(profile).stats["violations"] == 1
    msg = next(r.getMessage() for r in caplog.records if "CONTRACT VIOLATION" in r.getMessage())
    assert msg.startswith("PLANNER POLICY CONTRACT VIOLATION: inner AcmeBreaker v1 in scope "
                          "partition:haulier:acme at step 3 returned an invalid plan: order 'o_a1' "
                          "appears twice in the plan. The scope's plan was DISCARDED"), msg


# --- 5. PooledMarket via policies == the legacy topology path ----------------------------


@pytest.mark.parametrize("case_id", ["A-greedy-pooled-chain-spatial-cap500",
                                     "B-random-pooled-chain-spatial-cap40",
                                     "A-greedy-pooled-chain-sampled-cap40"])
def test_pooled_market_selection_equals_legacy_topology_path(case_id):
    """Verified to have teeth: calling ``self.inner.set_tiebreak(None)`` instead of the
    tick seed in ``PooledMarket.decide`` failed the greedy cases (on the legacy path
    too, so the comparison here was against the golden-locked legacy run)."""
    case = copy.deepcopy(_CASES[case_id])
    legacy = run_case(case, _TICKS)
    selected = copy.deepcopy(case)
    prof = selected["profile"]
    node = resolve_dispatch_for_profile(prof)
    assert node.label.startswith("PooledMarket/")
    prof["policies"] = {"planner": {"type": "PooledMarket", "params": node.params.to_dict()}}
    prof.pop("use_spatial_matching", None)
    if not case["profile"].get("use_spatial_matching", True):
        prof["policies"]["planner"]["params"]["inner"]["params"]["candidates"] = "sampled"
    via_policies = run_case(selected, _TICKS)
    assert legacy["pairs"], "vacuous: the tick awarded nothing"
    for field in ("pairs", "share_tags", "published", "round_stats", "rng_probe"):
        assert via_policies[field] == legacy[field], f"{case_id}: {field} differs"
    assert legacy == _golden_expected(case_id), "the legacy path itself must still match the golden"


def _golden_expected(case_id):
    return json.loads(json.dumps(_CASES[case_id]["expected"]))


# --- 6/7. central leaves ------------------------------------------------------------------


def _central_world():
    trucks = [_truck("t_acme", "acme", 103.95, 1.30), _truck("t_borax", "borax", 103.851, 1.30),
              _truck("t_cargo", "cargo", 103.8505, 1.30), _truck("t_acme2", "acme", 103.70, 1.30)]
    orders = [_order("o_acme", "acme", 103.85, 1.30), _order("o_cargo", "cargo", 103.70, 1.30)]
    return trucks, orders


def test_central_greedy_serves_only_world_allowed_cross_pairs_and_renders_audited_tags():
    """Cargo's truck is nearer to acme's order than borax's, but only borax is acme's
    partner: the central leaf's candidates and the framework's check both follow the
    world rule. Verified to have teeth: rendering the central leaf's tags with
    ``fmt="v1"`` failed the key assertion."""
    trucks, orders = _central_world()
    profile = _base_profile(policies={"planner": {"type": "GreedyNearest"}},
                            cooperation=_coop([("acme", "borax")], active="s1"))
    app = _app(trucks, orders, profile)
    pairs = _ids(app.assign(_CLOCK, 1))
    world = app._dispatch_world(profile)
    by_id = {t["_id"]: t for t in trucks} | {o["_id"]: o for o in orders}
    assert ("t_borax", "o_acme") in pairs and ("t_cargo", "o_acme") not in pairs, pairs
    assert all(world.allowed(by_id[t], by_id[o]) for t, o in pairs)
    tag = app._share_tags[("t_borax", "o_acme")]
    assert list(tag) == ["shared", "owner_haulier_id", "carrier_haulier_id", "benefit_km", "pool_id",
                         "awarded_cost_km", "owner_reserve_km", "market_round"]
    assert tag["pool_id"] is None and tag["market_round"] is None
    assert tag["benefit_km"] == pytest.approx(tag["owner_reserve_km"] - tag["awarded_cost_km"], abs=2e-3)


@pytest.mark.parametrize("name,params", [("GreedyNearest", {"candidates": "spatial"}),
                                         ("GreedyNearest", {"candidates": "sampled"}),
                                         ("RandomAssignment", {})],
                         ids=["GreedyNearest-spatial", "GreedyNearest-sampled", "RandomAssignment"])
def test_central_leaves_draw_no_module_rng(name, params):
    """With the sample cap binding (1 per haulier), the tick scope samples from ``ctx.rng``.

    Verified to have teeth: drawing the tick-scope sample from the ``random`` module
    (``rng=random`` in ``DispatchContext.sampled_trucks``' tick branch) failed the
    GreedyNearest-sampled and RandomAssignment cases.
    """
    trucks = [_truck(f"t_{h}{i}", h, 103.85 + 0.01 * i, 1.30) for h in ("acme", "borax") for i in range(3)]
    orders = [_order(f"o_{h}{i}", h, 103.85 + 0.01 * i, 1.30) for h in ("acme", "borax") for i in range(2)]
    profile = _base_profile(policies={"planner": {"type": name, "params": params}}, max_trucks_per_haulier=1)
    app = _app(trucks, orders, profile)
    random.seed(5)
    before = random.getstate()
    assert app.assign(_CLOCK, 1)
    assert random.getstate() == before, f"{name} {params} drew from the random module"


# --- 8. the market crash path --------------------------------------------------------------


@pytest.fixture
def borax_crasher():
    with isolated_registry():
        @policy(slot="planner", name="BoraxCrasher", version="1", information_model="central",
                description="raises for borax")
        class BoraxCrasher(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def decide(self, ctx):
                if ctx.scope.key == "company:borax":
                    raise RuntimeError("borax planner exploded")
                trucks = [str(t["_id"]) for t in ctx.raw_trucks]
                orders = [str(o["_id"]) for o in ctx.raw_orders if assign_constraints.haulier_of(o) == "acme"]
                return Plan(chains={tid: (oid,) for tid, oid in zip(trucks, orders)})

        yield BoraxCrasher


def test_market_crash_logs_crashed_prefix_counts_and_skips_company(borax_crasher, caplog):
    """Verified to have teeth: re-raising in ``run_pooled_market``'s company ``except``
    propagated the crash out of ``assign``; dropping the ``crashes`` count failed the
    stats assertion."""
    trucks = [_truck("t_acme", "acme", 103.85, 1.30), _truck("t_borax", "borax", 103.95, 1.30)]
    orders = [_order("o_acme", "acme", 103.85, 1.30), _order("o_borax", "borax", 103.95, 1.30)]
    profile = _base_profile(policies={"planner": {"type": "PooledMarket", "params": {
        "inner": {"type": "BoraxCrasher"}}}})
    app = _app(trucks, orders, profile)
    with caplog.at_level(logging.ERROR):
        assert _ids(app.assign(_CLOCK, 9)) == [("t_acme", "o_acme")]
    stats = app._dispatch_runner(profile).stats
    assert stats["crashes"] >= 1 and stats["violations"] == 0
    rec = next(r for r in caplog.records if "PLANNER POLICY CRASHED" in r.getMessage())
    assert rec.getMessage().startswith(
        "PLANNER POLICY CRASHED: inner BoraxCrasher raised for company 'borax' in round 1 at step 9; "
        "that company commits nothing this round (pooled-market behaviour, unchanged). Traceback "
        "follows. (celery journal, not sim.log)")
    assert rec.exc_info is not None, "the traceback must follow"


# --- 9. the rebate book through a composite ----------------------------------------------


@pytest.fixture
def rebate_reader():
    with isolated_registry():
        @policy(slot="planner", name="RebateReader", version="1", information_model="central",
                description="records ctx.rebates")
        class RebateReader(DispatchPolicy):
            seen: list = []

            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def decide(self, ctx):
                RebateReader.seen.append(ctx.rebates)
                return Plan(chains={})

        RebateReader.seen = []
        yield RebateReader


class _RebateManager(_StubManager):
    run_id = "run_whole_tick"

    def _facility_url(self):
        return "facility"

    def _paged_where(self, url, where, projection=None):
        return [{"_id": "f1", "profile": {"rebate": {}}}]


@pytest.mark.parametrize("top", ["Partitioned", "PooledMarket"])
def test_rebate_book_reaches_the_leaf_through_a_composite(rebate_reader, top):
    """Verified to have teeth: not forwarding ``set_rebate_book`` in
    ``CompositePolicy`` left the leaf's book ``None`` (both tops)."""
    trucks = [_truck("t_acme", "acme", 103.85, 1.30)]
    orders = [_order("o_acme", "acme", 103.85, 1.30)]
    profile = _base_profile(planner={"rebate_aware": True},
                            policies={"planner": {"type": top, "params": {"inner": {"type": "RebateReader"}}}})
    app = _app(trucks, orders, profile)
    app.manager = _RebateManager(trucks, orders)
    app.assign(_CLOCK, 1)
    leaf = innermost_policy(app._solver)
    assert leaf.rebate_book is not None, "the book must reach the leaf"
    assert rebate_reader.seen and all(book is leaf.rebate_book for book in rebate_reader.seen), (
        "every scope the leaf decides must carry the leaf's book")


def test_get_solver_harness_still_builds_the_default_tree_around_its_leaf():
    """The pre-v2 ``__new__`` harness injects a LEAF; the runner wraps it (``bind_instance``)."""
    app = _app([], [], _base_profile(strategy="GreedyNearest"), solver=get_solver("GreedyNearest", None))
    runner = app._dispatch_runner(app.behavior["profile"])
    assert runner.resolved.label == "Partitioned/GreedyNearest" and innermost_policy(runner.policy) is app._solver
