"""The generic solver contract's value types: ``Result``, ``RunWorld``, ``Declaration``, ``Tick``.

What is locked (plan ``plan_contract_final.md`` §2.3-§2.7, WS1 commit 1.2):

- ``Result.from_pairs`` refuses a repeated truck; ``invariants.check_result`` reports
  every malformed result;
- ``RunWorld`` hides the selection keys and records every read, ``unread()`` none;
- ``bind_tree`` refuses a top declaration outside the ceiling (message 23) and a nested
  node declaring more than the top or a top-only field (message 24);
- ``Tick.restrict`` refuses documents that are not the tick's (by identity), narrows
  ``permitted`` and never widens it; ``Tick.call`` refuses a foreign or reused
  sub-tick, binds the CALLEE instance's injected RNG as the sub-tick's decision RNG
  while ``sample_rng`` stays the caller's, and remembers passing pairs so the top check
  re-checks only their ownership half;
- ``constraints.declared_partnerships`` is the union over every structure's adjacency.

The tree here is built from duck-typed nodes (``SimpleNamespace``) so these types are
tested without the selection resolver. Each test names its teeth (the mutation that
was made to see it fail, then restored).
"""

from __future__ import annotations

import copy
import random
from types import SimpleNamespace

import pytest

from apps.container_logistics.assignment import constraints as C
from apps.container_logistics.policies.dispatch.bind import Declaration, RunWorld, bind_tree
from apps.container_logistics.policies.dispatch.invariants import check_result
from apps.container_logistics.policies.dispatch.result import Result
from apps.container_logistics.policies.dispatch.tick import DispatchClock, Measure, Tick
from apps.container_logistics.policies.framework.errors import DispatchContractViolation, PolicyError

CLOCK = DispatchClock(sim_clock="Wed, 01 Jan 2020 08:00:00 GMT", time_step=3)


def _pt(lon, lat):
    return {"type": "Point", "coordinates": [lon, lat]}


def _truck(tid, h, lon=103.85):
    return {"_id": tid, "haulier_id": h, "profile": {"haulier_id": h, "truck_size": "40ft", "current_loc": _pt(lon, 1.3)}}


def _order(oid, h, lon=103.85):
    return {"_id": oid, "haulier_id": h, "order_size": "1x20",
            "profile": {"haulier_id": h, "order_size": "1x20"}, "pickup_loc": _pt(lon, 1.301)}


def _profile(edges=(("h_a", "h_b"),), extra_structures=()):
    adjacency = {}
    for a, b in edges:
        adjacency.setdefault(a, []).append(b)
        adjacency.setdefault(b, []).append(a)
    structures = [{"id": "s1", "adjacency": adjacency}, *extra_structures]
    return {"max_travel_time_pickup": None, "cooperation": {"active": "s1", "structures": structures},
            "strategy": "X", "solver_params": {"k": 1}, "policies": {}, "spatial_cell_deg": 0.05}


class _Leaf:
    """A leaf wrapper: declares what it is told, returns the pairs it is told."""

    def __init__(self, declaration=None, pairs=None, record=None):
        self.declaration = declaration or Declaration()
        self.pairs = pairs
        self.record = record if record is not None else []
        self._rng = None
        self.tiebreak_seed = 77
        self.rebate_book = "book"

    @property
    def rng(self):
        return self._rng if self._rng is not None else random

    def bind(self, params, world, *, top):
        world.get("spatial_cell_deg")
        return self.declaration

    def solve_tick(self, tick, params):
        self.record.append(tick)
        pairs = self.pairs(tick) if callable(self.pairs) else (self.pairs or ())
        return Result(pairs=tuple(pairs))


class _Composite:
    """A top wrapper holding ``inner``; ``solve_tick`` is driven by the test."""

    def __init__(self, inner, declaration=None):
        self.inner = inner
        self.declaration = declaration or Declaration()
        self.rebate_book = None

    def bind(self, params, world, *, top):
        world.get("cooperation")
        return self.declaration


def _node(name, inner=None):
    return SimpleNamespace(name=name, version="1", params=SimpleNamespace(inner="INNER") if inner else None,
                           inner=inner, source="policies")


def _tree(profile, *, top_decl=None, inner_decl=None, inner_pairs=None):
    leaf = _Leaf(inner_decl, inner_pairs)
    top = _Composite(leaf, top_decl)
    node = _node("Top", inner=_node("Leaf"))
    return bind_tree(node, top, profile), top, leaf


def _top_tick(tree, trucks, orders, profile):
    return Tick.top(tree=tree, trucks=trucks, orders=orders, world=RunWorld.from_profile(profile),
                    max_travel_time_pickup=profile.get("max_travel_time_pickup"), clock=CLOCK, seed=42,
                    measure=Measure({}))


# --- Result / check_result ---------------------------------------------------------

def test_result_from_pairs_refuses_a_repeated_truck():
    """Teeth: dropping the ``seen`` check in ``Result.from_pairs`` made this pass silently."""
    t, o1, o2 = _truck("t1", "h_a"), _order("o1", "h_a"), _order("o2", "h_a")
    assert Result.from_pairs([(t, o1)]).pairs == (("t1", "o1"),)
    with pytest.raises(DispatchContractViolation, match="truck 't1' appears twice in pairs"):
        Result.from_pairs([(t, o1), (t, o2)])


def test_check_result_reports_every_malformed_result():
    """Teeth: removing the order-twice branch of ``check_result`` failed the second assertion."""
    prof = _profile()
    tree, _top, _leaf = _tree(prof, top_decl=Declaration(cross_company={("h_a", "h_b")}))
    trucks = [_truck("t1", "h_a"), _truck("t2", "h_c")]
    orders = [_order("o1", "h_a"), _order("o2", "h_c"), _order("o3", "h_b")]
    tick = _top_tick(tree, trucks, orders, prof)
    assert check_result("nope", tick) == ["solve_tick() returned str, expected Result"]
    got = check_result(Result(pairs=(("t1", "o1"), ("t2", "o1"), ("t1", "o2"), ("tx", "o9"), ("t2", "o3"))), tick)
    assert "order 'o1' appears twice in the result" in got
    assert "truck 't1' appears twice in the result" in got
    assert "truck 'tx' is not in scope tick" in got and "order 'o9' is not in scope tick" in got
    # h_c serving h_b is not declared; h_a's truck serving h_c's order neither.
    assert "pair ('t2', 'o3') is not allowed" in got and "pair ('t1', 'o2') is not allowed" in got
    assert check_result(Result(pairs=(("t1", 1),)), tick) == ["pair ('t1', 1) is not a (truck_id, order_id) pair of strings"]
    notes = Result(pairs=(("t1", "o1"),), solver_notes={("t2", "o2"): {}, ("t1", "o1"): {1: "x"}})
    assert check_result(notes, tick) == [
        "solver_notes key ('t2', 'o2') is not one of the result's pairs",
        "solver_notes for ('t1', 'o1') is not a mapping with string keys",
    ]
    assert check_result(Result(pairs=(("t1", "o1"),), solver_notes={("t1", "o1"): {"pool_id": "p"}}), tick) == []


# --- RunWorld ------------------------------------------------------------------------

def test_run_world_hides_selection_keys_and_records_reads():
    """Teeth: making ``get`` skip ``_reads.add`` failed the ``reads`` assertion."""
    world = RunWorld.from_profile({**_profile(), "policy_capture": {"dir": "x"}, "policies_format": 2,
                                   "planner": {}, "use_spatial_matching": True})
    assert sorted(world.unread()) == ["cooperation", "max_travel_time_pickup", "spatial_cell_deg"]
    assert world.reads == frozenset()
    assert world.get("strategy") is None and "solver_params" not in world
    assert world["spatial_cell_deg"] == 0.05
    assert world.reads == {"strategy", "solver_params", "spatial_cell_deg"}
    list(world)
    assert world.reads >= {"cooperation", "max_travel_time_pickup"}
    with pytest.raises(KeyError):
        world["policies"]


# --- bind: messages 23 / 24 ---------------------------------------------------------

def test_message_23_refuses_a_top_declaration_outside_the_ceiling():
    """Teeth: returning early from ``_check_top`` made the first ``raises`` pass silently."""
    prof = _profile(edges=(("h_a", "h_b"),))
    with pytest.raises(PolicyError, match=r"\(.h_c., .h_a.\) \(owner, carrier\) that no structure of the scenario"):
        _tree(prof, top_decl=Declaration(cross_company={("h_c", "h_a")}))
    with pytest.raises(PolicyError, match="malformed cross-company pair"):
        _tree(prof, top_decl=Declaration(cross_company={("h_a", "h_a")}))
    # Inside the ceiling: an INACTIVE structure of the menu counts (the ceiling is the union over arms).
    other = {"id": "s2", "adjacency": {"h_c": ["h_a"], "h_a": ["h_c"]}}
    tree, _t, _l = _tree(_profile(extra_structures=(other,)), top_decl=Declaration(cross_company={("h_c", "h_a")}))
    assert tree.declaration.cross_company == {("h_c", "h_a")}


def test_message_24_refuses_a_nested_node_declaring_more_than_the_top():
    """Teeth: dropping the ``wider`` term of ``_check_inner`` made the first ``raises`` pass silently."""
    prof = _profile()
    with pytest.raises(PolicyError, match="the top did not declare"):
        _tree(prof, inner_decl=Declaration(cross_company={("h_a", "h_b")}))
    with pytest.raises(PolicyError, match="top-only field"):
        _tree(prof, inner_decl=Declaration(stamp_settings={"COOPERATION": {}}))
    with pytest.raises(PolicyError, match="top-only field"):
        _tree(prof, inner_decl=Declaration(run_name_segments=("x",)))
    tree, _t, _l = _tree(prof, top_decl=Declaration(cross_company={("h_a", "h_b")}),
                         inner_decl=Declaration(cross_company={("h_a", "h_b")}, candidate_hint="spatial"))
    assert tree.top.inner.declaration.candidate_hint == "spatial"
    assert tree.world_reads == {"cooperation", "spatial_cell_deg"}
    assert tree.top.world_reads == {"cooperation"} and tree.top.inner.world_reads == {"spatial_cell_deg"}


def test_bind_is_pure():
    """Binding twice gives equal declarations and touches neither the profile nor the random module.

    Teeth: a ``random.random()`` call added to ``_Composite.bind`` failed the state assertion.
    """
    prof = _profile()
    before_prof, before_rng = copy.deepcopy(prof), random.getstate()
    a, _t, _l = _tree(prof, top_decl=Declaration(cross_company={("h_a", "h_b")}))
    b, _t, _l = _tree(prof, top_decl=Declaration(cross_company={("h_a", "h_b")}))
    assert a.declaration == b.declaration and a.world_reads == b.world_reads
    assert prof == before_prof and random.getstate() == before_rng


# --- Tick: restrict / call / verified pairs ------------------------------------------

def test_restrict_refuses_foreign_documents_and_narrows_without_widening():
    """Teeth: removing the ``id(t) not in truck_ids`` check made the first ``raises`` pass silently."""
    prof = _profile()
    tree, _top, _leaf = _tree(prof, top_decl=Declaration(cross_company={("h_a", "h_b")}))
    trucks, orders = [_truck("t1", "h_a"), _truck("t2", "h_b")], [_order("o1", "h_a"), _order("o2", "h_b")]
    tick = _top_tick(tree, trucks, orders, prof)
    with pytest.raises(DispatchContractViolation, match="truck 't1' is not a document of scope tick"):
        tick.restrict(trucks=[dict(trucks[0])], orders=orders, scope="s", sample_rng=None)
    with pytest.raises(DispatchContractViolation, match="order 'o1' is not a document"):
        tick.restrict(trucks=trucks, orders=[copy.deepcopy(orders[0])], scope="s", sample_rng=None)
    assert tick.permitted(trucks[1], orders[0])  # h_b serves h_a: declared (owner h_a, carrier h_b)
    sub = tick.restrict(trucks=trucks, orders=orders, scope="part:x", sample_rng=None,
                        narrow=lambda t, o: C.haulier_of(t) == C.haulier_of(o))
    assert sub.depth == 1 and sub.scope == "part:x"
    assert not sub.permitted(trucks[1], orders[0]) and sub.permitted(trucks[0], orders[0])
    always = tick.restrict(trucks=trucks, orders=orders, scope="all", sample_rng=None, narrow=lambda t, o: True)
    # Narrow=True never widens: h_a's truck may not serve h_b's order (not declared that way round).
    assert tick.permitted(trucks[0], orders[1]) is False and always.permitted(trucks[0], orders[1]) is False


def test_call_binds_the_callee_rng_and_keeps_the_callers_sample_rng():
    """Teeth: binding ``sub._rng`` from the PARENT tick's rng failed the ``rng is inner_rng`` assertion."""
    prof = _profile()
    tree, top, leaf = _tree(prof)
    inner_rng, sampler = random.Random(5), random.Random(9)
    leaf._rng = inner_rng
    trucks, orders = [_truck("t1", "h_a")], [_order("o1", "h_a")]
    tick = _top_tick(tree, trucks, orders, prof)
    assert tick.callee("inner") is tree.top.inner
    tick.call("INNER", tick.restrict(trucks=trucks, orders=orders, scope="a", sample_rng=sampler))
    tick.call("inner", tick.restrict(trucks=trucks, orders=orders, scope="b", sample_rng=None))
    first, second = leaf.record
    assert first.rng is inner_rng and first.sample_rng is sampler
    assert second.rng is inner_rng and second.sample_rng is inner_rng
    assert first.tiebreak_seed == 77 and first.rebates == "book" and first.params is None
    assert tick._rng is None, "the top tick's Random must not be built by a call"
    assert tick.rng is tick.rng and tick.rng is not inner_rng


def test_call_refuses_a_foreign_or_reused_sub_tick():
    """Teeth: removing the ``sub._used`` check made the second ``raises`` pass silently."""
    prof = _profile()
    tree, _top, _leaf = _tree(prof)
    trucks, orders = [_truck("t1", "h_a")], [_order("o1", "h_a")]
    tick = _top_tick(tree, trucks, orders, prof)
    other = _top_tick(tree, trucks, orders, prof)
    with pytest.raises(DispatchContractViolation, match="not made by this tick"):
        tick.call("inner", other.restrict(trucks=trucks, orders=orders, scope="x", sample_rng=None))
    sub = tick.restrict(trucks=trucks, orders=orders, scope="x", sample_rng=None)
    tick.call("inner", sub)
    with pytest.raises(DispatchContractViolation, match="already used by a call"):
        tick.call("inner", sub)
    with pytest.raises(PolicyError, match="no bound child"):
        tick.call("other", tick.restrict(trucks=trucks, orders=orders, scope="y", sample_rng=None))


def test_a_discarded_sub_result_is_message_19_and_counted(caplog):
    """Teeth: skipping ``check_result`` in ``Tick.call`` returned the bad result instead of ``None``."""
    prof = _profile()
    tree, _top, _leaf = _tree(prof, inner_pairs=(("t1", "o2"),))
    trucks, orders = [_truck("t1", "h_a")], [_order("o1", "h_a"), _order("o2", "h_a")]
    tick = _top_tick(tree, trucks, orders, prof)
    sub = tick.restrict(trucks=trucks, orders=orders[:1], scope="partition:haulier:h_a", sample_rng=None)
    with caplog.at_level("ERROR"):
        assert tick.call("inner", sub) is None
    assert tick._shared.stats["violations"] == 1
    assert "inner Leaf v1 in scope partition:haulier:h_a at step 3 returned an invalid result: " \
           "order 'o2' is not in scope partition:haulier:h_a" in caplog.text


def test_verified_pairs_recheck_only_the_ownership_half(monkeypatch):
    """A pair a sub-tick check passed re-checks ownership only at the top; others get the full predicate.

    Teeth: making ``Tick._top_check`` ignore ``verified`` raised the physical-check count to 2.
    """
    prof = _profile()
    tree, _top, _leaf = _tree(prof, top_decl=Declaration(cross_company={("h_a", "h_b")}),
                              inner_pairs=(("t1", "o1"),))
    trucks, orders = [_truck("t1", "h_a"), _truck("t2", "h_a")], [_order("o1", "h_a"), _order("o2", "h_a")]
    tick = _top_tick(tree, trucks, orders, prof)
    calls = []
    real = C.pair_allowed_ignoring_haulier
    monkeypatch.setattr(C, "pair_allowed_ignoring_haulier", lambda *a, **k: calls.append(a) or real(*a, **k))
    assert tick.call("inner", tick.restrict(trucks=trucks[:1], orders=orders[:1], scope="s", sample_rng=None))
    assert len(calls) == 1  # the sub-tick check
    check = tick._top_check()
    assert check(trucks[0], orders[0]) and len(calls) == 1  # verified: ownership only
    assert check(trucks[1], orders[1]) and len(calls) == 2  # not verified: the full predicate
    foreign = _truck("t9", "h_c")
    assert not check(foreign, orders[0]) and len(calls) == 2  # ownership refuses before physics


def test_scope_log_entries_keep_the_audited_shape():
    """Teeth: logging ``kind=scope`` (the whole key) failed the first assertion."""
    prof = _profile()
    tree, _top, _leaf = _tree(prof)
    log: list = []
    trucks, orders = [_truck("t1", "h_a")], [_order("o1", "h_a")]
    tick = Tick.top(tree=tree, trucks=trucks, orders=orders, world=RunWorld.from_profile(prof),
                    max_travel_time_pickup=None, clock=CLOCK, seed=1, measure=Measure({}), scope_log=log)
    tick.restrict(trucks=trucks, orders=orders, scope="company:h_a", sample_rng=None)
    assert log == [{"kind": "company", "key": "company:h_a", "round": None, "trucks": ["t1"], "orders": ["o1"]}]


def test_declared_partnerships_is_the_union_over_every_structure():
    """Teeth: reading only the ACTIVE structure lost the s2 pairs."""
    other = {"id": "s2", "adjacency": {"h_c": ["h_a", "h_c"]}}
    got = C.declared_partnerships(_profile(extra_structures=(other,)))
    assert got == {("h_a", "h_b"), ("h_b", "h_a"), ("h_a", "h_c")}
    assert C.declared_partnerships({}) == frozenset() and C.declared_partnerships({"cooperation": []}) == frozenset()


def test_measure_is_dual_cycle_equals_the_constraint_predicate():
    """``Measure.is_dual_cycle`` (a copy over the geometry readers) equals ``constraints._is_dual_cycle_pair``.

    Over every truck x order pair of the dense synthetic tick (30% of trucks just
    dropped off at an order pickup) at three radii, plus a radius-boundary pair whose
    facility names differ. Teeth: changing ``<=`` to ``<`` in ``Measure.is_dual_cycle``
    failed the boundary assertion; dropping its facility-name branch failed the far
    same-facility assertion.
    """
    from apps.container_logistics.policies.testing import fixtures as fx

    truck = _truck("tb", "h_a")
    truck["profile"].update(last_dropoff_loc=_pt(103.85, 1.30), last_dropoff_facility_name="f1")
    order = _order("ob", "h_a")
    order["profile"]["pickup_facility_name"] = "f2"
    edge = C._haversine_km(C._truck_last_dropoff_loc(truck), C.order_pickup_loc(order))
    assert edge > 0 and C._is_dual_cycle_pair(truck, order, C.order_pickup_loc(order), edge)
    assert Measure.is_dual_cycle(truck, order, radius_km=edge) is True
    assert Measure.is_dual_cycle(truck, order, radius_km=edge / 2) is False
    far = _truck("tf", "h_a")
    far["profile"].update(last_dropoff_loc=_pt(104.5, 1.0), last_dropoff_facility_name="f2")
    assert C._is_dual_cycle_pair(far, order, C.order_pickup_loc(order), 0.5)
    assert Measure.is_dual_cycle(far, order, radius_km=0.5) is True
    tick = fx.scale(n_trucks=60, n_orders=40, hauliers=2, seed=4)
    hits = 0
    for radius in (0.0, 0.5, 3.0):
        for t in tick.trucks:
            for o in tick.orders:
                want = C._is_dual_cycle_pair(t, o, C.order_pickup_loc(o), radius)
                assert Measure.is_dual_cycle(t, o, radius_km=radius) is want
                hits += want
    assert hits > 0


def test_serves_is_directional_like_the_declaration():
    """``serves(carrier, owner)`` holds for own or a declared ``(owner, carrier)``, never the reverse.

    Teeth: swapping the tuple to ``(carrier_id, owner_id)`` in ``Tick.serves`` failed it.
    """
    prof = _profile()
    tree, _top, _leaf = _tree(prof, top_decl=Declaration(cross_company={("h_a", "h_b")}))
    tick = _top_tick(tree, [], [], prof)
    assert tick.serves("h_b", "h_a") and tick.serves("h_a", "h_a")
    assert not tick.serves("h_a", "h_b") and not tick.serves("h_c", "h_a")
