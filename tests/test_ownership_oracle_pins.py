"""Ownership oracles: who may serve whom, pinned BEFORE the solver-contract refactor moves it.

Plan ``plan_contract_final.md`` §7.3 (WS0). The refactor deletes ``DispatchWorld`` /
``PartitionRule`` and re-derives ownership from the haulier field of the truck and order
documents (decision 1). These pins state the rule as an ORACLE written here, from the
documents and the scenario's cooperation data alone, and compare the code under test
against it. Nothing is imported from the code under test except through the three
``_sut_*`` adapters below; WS1 re-points only those adapters (``_sut_groups`` at
``library.partitioned``).

What is locked:

1. ``permitted(t, o) == physical(t, o) and (own(t, o) or (haulier(o), haulier(t)) in
   edges(active))`` over every golden case world x tick state A/B/C/T (sampled 1/4 trucks
   x 1/3 orders, as ``test_policy_round2_c3_review_fixes.py``), every
   ``fixtures.synthetic_fixtures()`` world, and a haulier-less stray truck and order.
   ``edges(s) = {(owner, carrier): owner in s.adjacency[carrier]}``; ``own`` = both
   haulier ids present and equal. ``physical`` is
   ``constraints.pair_allowed_ignoring_haulier`` with the profile's pickup limit (the
   physical rule is not what moves; who-may-serve-whom is).
2. ``serves(carrier, owner) == carrier == owner or (owner, carrier) in edges(active)``
   over all haulier pairs of each world.
3. ``cooperation_groups``: the group keys of each golden case world x tick, in order,
   equal "first-seen order of the orders' groups, groups with no trucks dropped"
   (``compose.py:63-99``), and each group carries exactly its trucks/orders in input order.
4. Ceiling: ``edges(active)`` is a subset of the union of ``edges(arm)`` over every arm,
   for every golden case and every compiled bundle.
5. The message-14 premise: every arm of every golden case and bundle has its pools
   (authored, else one per edge) inside its adjacency, both directions. This is what
   makes ``PooledMarket``'s ``narrow=None`` byte-identical in WS1.

Bundles are the compiled ``scenarios/*/scenario.json`` of this checkout, or of
``$ORSIM_SCENARIOS_DIR`` when set (they are gitignored, derived artifacts; CLAUDE.md §8).
Without any, the bundle pins skip honestly; the golden worlds always run.

Verified to have teeth (WS0, mutate / see it fail / restore):

- dropping the ``share_eligible`` branch from ``PartitionRule.allowed`` (``allowed.py:58``)
  failed pin 1 on golden ``chain`` ticks A/B/T and the ``tied_costs`` /
  ``three_company_chain`` synthetic worlds (5 cases). Mutating
  ``DispatchWorld.structure_allows`` instead would NOT: ``DispatchWorld.allowed``
  delegates to ``self.rule.allowed`` (``world.py:105-107``).
- returning ``list(reversed(groups))`` from ``compose.cooperation_groups`` failed pin 3
  (11 world x tick cases).

Re-verified after WS1 re-pointed the adapters (``Tick.permitted`` / ``Tick.serves`` of
the profile's bound selection, ``library.partitioned.cooperation_groups``):

- dropping the declared-pair test from ``tick._top_permitted`` (every cross pair passes
  ownership) failed pin 1 (12 cases);
- ``list(reversed(groups))`` in ``partitioned.cooperation_groups`` failed pin 3 (11 cases);
- swapping ``(owner, carrier)`` in ``Tick.serves`` does NOT fail pin 2: every golden and
  synthetic adjacency is symmetric, so the direction is not observable here (the
  directional rule is pinned in ``test_solver_contract_types.py::test_serves_is_directional_like_the_declaration``).
"""

from __future__ import annotations

import copy
import functools
import glob
import gzip
import itertools
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping

import pytest

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.policies.testing import fixtures as synthetic

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GOLDEN = _REPO_ROOT / "tests" / "golden" / "dispatch_golden_v1.json.gz"

Doc = dict[str, Any]
Edge = tuple[str, str]  # (owner, carrier)


# --- the code under test: the ONLY place it is reached (WS1 re-points these) -----------


def _sut_tick(profile: Mapping[str, Any]) -> Any:
    """A whole tick of ``profile``'s own selection, bound against ``profile`` (WS1: ``dispatch.bind``)."""
    from apps.container_logistics.policies.dispatch.bind import bind_profile
    from apps.container_logistics.policies.dispatch.runner import DispatchRunner
    from apps.container_logistics.policies.dispatch.tick import DispatchClock

    prof = dict(profile)
    runner = DispatchRunner(bind_profile(prof), profile=prof)
    return runner.tick(candidates=[], orders=[], profile=prof,
                       clock=DispatchClock(sim_clock="Wed, 01 Jan 2020 08:00:00 GMT", time_step=0), seed=0)


def _sut_permitted(profile: Mapping[str, Any]) -> Callable[[Doc, Doc], bool]:
    """The framework's pair feasibility for ``profile`` (WS1: the whole tick's ``Tick.permitted``)."""
    return _sut_tick(profile).permitted


def _sut_serves(profile: Mapping[str, Any]) -> Callable[[str, str], bool]:
    """``serves(carrier, owner)`` for ``profile`` (WS1: ``Tick.serves``)."""
    return _sut_tick(profile).serves


def _sut_groups(profile: Mapping[str, Any], trucks: list[Doc], orders: list[Doc]) -> list[tuple[Any, list, list]]:
    """``(key, truck ids, order ids)`` per cooperation group (WS1: ``library.partitioned.cooperation_groups``).

    It reads exactly ``tick.raw_trucks`` / ``tick.raw_orders`` plus the active arm's
    ``component_of``, so a namespace with the two document tuples is the whole tick input.
    """
    from apps.container_logistics.policies.dispatch.bind import RunWorld
    from apps.container_logistics.policies.dispatch.library import partitioned
    from apps.container_logistics.policies.solver_lib import cooperation

    arm = cooperation.resolve_arm(RunWorld.from_profile(dict(profile)))
    tick = SimpleNamespace(raw_trucks=tuple(trucks), raw_orders=tuple(orders))
    return [(g.key, [t["_id"] for t in g.trucks], [o["_id"] for o in g.orders])
            for g in partitioned.cooperation_groups(tick, arm.component_of if arm is not None else {})]


# --- the oracle: documents and cooperation data only -----------------------------------


def _haulier(doc: Mapping[str, Any]) -> str | None:
    """The document's haulier: top-level ``haulier_id``, else ``profile.haulier_id``; blank is none."""
    hid = doc.get("haulier_id")
    if hid is None:
        hid = (doc.get("profile") or {}).get("haulier_id")
    return None if hid in (None, "") else str(hid)


def _arms(profile: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    coop = profile.get("cooperation")
    if not isinstance(coop, dict) or not isinstance(coop.get("structures"), list):
        return []
    return [s for s in coop["structures"] if isinstance(s, dict)]


def _active(profile: Mapping[str, Any]) -> Mapping[str, Any] | None:
    coop = profile.get("cooperation")
    if not isinstance(coop, dict):
        return None
    return next((s for s in _arms(profile) if s.get("id") == coop.get("active")), None)


def _edges(structure: Mapping[str, Any] | None) -> set[Edge]:
    """``{(owner, carrier): owner in adjacency[carrier]}`` -- a carrier serves the owner's orders."""
    adjacency = (structure or {}).get("adjacency")
    if not isinstance(adjacency, dict):
        return set()
    return {(str(owner), str(carrier)) for carrier, owners in adjacency.items()
            if isinstance(owners, (list, tuple)) for owner in owners}


def _physical(profile: Mapping[str, Any]) -> Callable[[Doc, Doc], bool]:
    mtt = profile.get("max_travel_time_pickup")
    return lambda t, o: assign_constraints.pair_allowed_ignoring_haulier(t, o, max_travel_time_pickup=mtt)


def _oracle_permitted(profile: Mapping[str, Any]) -> Callable[[Doc, Doc], bool]:
    edges, physical = _edges(_active(profile)), _physical(profile)

    def permitted(t: Doc, o: Doc) -> bool:
        ht, ho = _haulier(t), _haulier(o)
        own = ht is not None and ht == ho
        return physical(t, o) and (own or (ho, ht) in edges)

    return permitted


def _oracle_groups(profile: Mapping[str, Any], trucks: list[Doc], orders: list[Doc]) -> list[tuple[Any, list, list]]:
    """First-seen order of the orders' groups; groups with no trucks (and haulier-less docs) dropped.

    A group is a multi-company component of the active arm (keyed by its index in the
    arm's ``components``, which must be the connected components of its adjacency),
    else the haulier on its own.
    """
    active = _active(profile)
    component_of: dict[str, int] = {}
    if active and active.get("adjacency"):
        for idx, members in enumerate(active.get("components") or []):
            if len(members) > 1:
                component_of.update({str(h): idx for h in members})

    def key(doc: Doc) -> Any:
        h = _haulier(doc)
        if h is None:
            return None
        return ("component", component_of[h]) if h in component_of else ("haulier", h)

    order_keys = list(dict.fromkeys(k for k in map(key, orders) if k is not None))
    truck_keys = {key(t) for t in trucks}
    return [(k, [t["_id"] for t in trucks if key(t) == k], [o["_id"] for o in orders if key(o) == k])
            for k in order_keys if k in truck_keys]


def _connected_components(structure: Mapping[str, Any]) -> set[frozenset[str]]:
    """Connected components (size > 1) of the arm's adjacency, undirected."""
    nbrs: dict[str, set[str]] = {}
    for a, b in _edges(structure):
        nbrs.setdefault(a, set()).add(b)
        nbrs.setdefault(b, set()).add(a)
    seen: set[str] = set()
    out: set[frozenset[str]] = set()
    for start in nbrs:
        if start in seen:
            continue
        comp, stack = set(), [start]
        while stack:
            h = stack.pop()
            if h not in comp:
                comp.add(h)
                stack.extend(nbrs[h] - comp)
        seen |= comp
        out.add(frozenset(comp))
    return out


def _pools(structure: Mapping[str, Any]) -> list[tuple[str, list[str]]]:
    """Authored pools when the key is present (even empty), else one pool per edge."""
    if isinstance(structure.get("pools"), list):
        return [(str(p.get("id")), [str(m) for m in p.get("members") or []])
                for p in structure["pools"] if isinstance(p, dict)]
    return [(f"edge:{a}+{b}", [str(a), str(b)]) for a, b in structure.get("edges") or []]


# --- the worlds ------------------------------------------------------------------------


def _load_golden() -> dict[str, Any]:
    with gzip.open(_GOLDEN, "rb") as fh:
        return json.load(fh)


_FIXTURE = _load_golden()
_TICKS = _FIXTURE["tick_states"]
_TICK_IDS = sorted(_TICKS)  # A B C T


def _world_key(profile: Mapping[str, Any]) -> str:
    """Everything the ownership rule reads from a profile: cooperation + pickup limit."""
    return json.dumps({"cooperation": profile.get("cooperation"),
                       "max_travel_time_pickup": profile.get("max_travel_time_pickup")}, sort_keys=True)


def _golden_worlds() -> dict[str, tuple[dict[str, Any], list[str]]]:
    """Distinct ownership worlds of the 68 golden cases: ``{label: (profile, case ids)}``.

    Cases sharing cooperation + pickup limit share a world, so each is checked once;
    ``test_the_golden_worlds_cover_every_case`` pins that no case is dropped.
    """
    by_key: dict[str, tuple[dict[str, Any], list[str]]] = {}
    for case in _FIXTURE["cases"]:
        prof = case["profile"]
        by_key.setdefault(_world_key(prof), (prof, []))[1].append(case["case_id"])
    out = {}
    for prof, ids in by_key.values():
        label = f"{prof['cooperation'].get('active')}-mtt{prof.get('max_travel_time_pickup')}-n{len(ids)}"
        out[label] = (prof, ids)
    return out


_GOLDEN_WORLDS = _golden_worlds()


def _sampled(tick: str) -> tuple[list[Doc], list[Doc]]:
    state = _TICKS[tick]
    return copy.deepcopy(state["trucks"][::4]), copy.deepcopy(state["orders"][::3])


def _strays(trucks: list[Doc], orders: list[Doc]) -> tuple[list[Doc], list[Doc]]:
    """A haulier-less truck and order (copies of real ones, so they are physically plausible)."""
    out_t, out_o = [], []
    if trucks:
        t = copy.deepcopy(trucks[0])
        t["_id"], t["haulier_id"] = f"{t['_id']}-stray", None
        t.setdefault("profile", {})["haulier_id"] = None
        out_t.append(t)
    if orders:
        o = copy.deepcopy(orders[0])
        o["_id"], o["haulier_id"] = f"{o['_id']}-stray", None
        o.setdefault("profile", {})["haulier_id"] = None
        out_o.append(o)
    return out_t, out_o


def _bundle_paths() -> list[str]:
    roots = [os.environ.get("ORSIM_SCENARIOS_DIR"), str(_REPO_ROOT / "scenarios")]
    found: list[str] = []
    for root in filter(None, roots):
        found += sorted(glob.glob(os.path.join(root, "*", "scenario.json")))
    return list(dict.fromkeys(found))


@functools.lru_cache(maxsize=None)
def _bundle_profile(path: str) -> dict[str, Any]:
    with open(path) as fh:
        bundle = json.load(fh)
    return next(iter(bundle["agents"]["assignment"].values()))["profile"]


def _ceiling_and_pool_worlds() -> list[Any]:
    worlds = [pytest.param(prof, id=f"golden:{label}") for label, (prof, _) in _GOLDEN_WORLDS.items()]
    bundles = _bundle_paths()
    worlds += [pytest.param(path, id=f"bundle:{Path(path).parent.name}") for path in bundles]
    if not bundles:
        worlds.append(pytest.param(None, id="bundle:none",
                                   marks=pytest.mark.skip(reason="no compiled scenarios/*/scenario.json "
                                                                 "(run `openride scenario compile`, or set "
                                                                 "ORSIM_SCENARIOS_DIR)")))
    return worlds


def _profile_of(world: Any) -> dict[str, Any]:
    return _bundle_profile(world) if isinstance(world, str) else world


def _haulier_ids(profile: Mapping[str, Any], docs: Iterable[Doc] = ()) -> list[str]:
    ids = {h for d in docs if (h := _haulier(d)) is not None}
    for arm in _arms(profile):
        ids |= {str(h) for comp in arm.get("components") or [] for h in comp}
        ids |= {h for e in _edges(arm) for h in e}
    return sorted(ids)


# --- pins ------------------------------------------------------------------------------


def test_the_golden_worlds_cover_every_case():
    covered = sorted(cid for _, ids in _GOLDEN_WORLDS.values() for cid in ids)
    assert covered == sorted(c["case_id"] for c in _FIXTURE["cases"])
    assert len(covered) == _FIXTURE["header"]["case_count"] == 68
    assert {prof["cooperation"]["active"] for prof, _ in _GOLDEN_WORLDS.values()} == {"chain", "no-coop"}


def _assert_permitted_matches(profile: Mapping[str, Any], trucks: list[Doc], orders: list[Doc]) -> int:
    """Pin 1 over ``trucks x orders`` plus strays; returns the number of allowed cross pairs."""
    stray_t, stray_o = _strays(trucks, orders)
    sut, oracle = _sut_permitted(profile), _oracle_permitted(profile)
    cross_allowed, bad = 0, []
    for t, o in itertools.product(trucks + stray_t, orders + stray_o):
        want = oracle(t, o)
        if sut(t, o) != want:
            bad.append((t["_id"], o["_id"], want))
        if want and _haulier(t) != _haulier(o):
            cross_allowed += 1
    assert not bad, f"{len(bad)} pairs disagree with the oracle, first: {bad[:5]}"
    return cross_allowed


@pytest.mark.parametrize("tick", _TICK_IDS)
@pytest.mark.parametrize("label", sorted(_GOLDEN_WORLDS))
def test_permitted_is_physical_and_own_or_an_active_edge_on_golden_worlds(label, tick):
    profile, _ = _GOLDEN_WORLDS[label]
    trucks, orders = _sampled(tick)
    _assert_permitted_matches(profile, trucks, orders)


def test_the_golden_worlds_include_an_allowed_cross_pair():
    """Not vacuous: some golden ``chain`` tick allows a pair along a cooperation edge."""
    per_tick = {}
    for label, (profile, _) in _GOLDEN_WORLDS.items():
        if not _edges(_active(profile)):
            continue
        for tick in _TICK_IDS:
            trucks, orders = _sampled(tick)
            oracle = _oracle_permitted(profile)
            per_tick[(label, tick)] = sum(1 for t, o in itertools.product(trucks, orders)
                                          if _haulier(t) != _haulier(o) and oracle(t, o))
    assert sum(per_tick.values()) > 0, per_tick


@pytest.mark.parametrize("make", synthetic.ROBUSTNESS_FIXTURES, ids=lambda m: m.__name__)
def test_permitted_is_physical_and_own_or_an_active_edge_on_synthetic_worlds(make):
    fx = make()
    assert any(f.id == fx.id for f in synthetic.synthetic_fixtures())
    profile = synthetic.world_profile(fx.structure, fx.max_travel_time_pickup)
    # ``scale`` is 1000 x 500; a 1/4 x 1/3 sample keeps it fast and covers every haulier.
    trucks, orders = list(fx.trucks), list(fx.orders)
    if len(trucks) * len(orders) > 50_000:
        trucks, orders = trucks[::4], orders[::3]
    _assert_permitted_matches(profile, copy.deepcopy(trucks), copy.deepcopy(orders))


def test_the_synthetic_worlds_include_an_allowed_cross_pair():
    """Not vacuous: at least one synthetic world allows a pair along a cooperation edge."""
    total = 0
    for fx in synthetic.synthetic_fixtures():
        if fx.structure is None:
            continue
        profile = synthetic.world_profile(fx.structure, fx.max_travel_time_pickup)
        oracle = _oracle_permitted(profile)
        total += sum(1 for t, o in itertools.product(fx.trucks, fx.orders)
                     if _haulier(t) != _haulier(o) and oracle(t, o))
    assert total > 0


def _serves_worlds() -> list[Any]:
    worlds = [pytest.param(prof, id=f"golden:{label}") for label, (prof, _) in _GOLDEN_WORLDS.items()]
    worlds += [pytest.param(synthetic.world_profile(fx.structure, fx.max_travel_time_pickup),
                            id=f"synthetic:{fx.id}") for fx in synthetic.synthetic_fixtures()]
    return worlds


@pytest.mark.parametrize("profile", _serves_worlds())
def test_serves_is_own_or_an_active_edge(profile):
    edges, serves = _edges(_active(profile)), _sut_serves(profile)
    ids = _haulier_ids(profile, _TICKS["A"]["trucks"] + _TICKS["A"]["orders"]) + ["h_unknown"]
    bad = [(c, o) for c, o in itertools.product(ids, ids) if serves(c, o) != (c == o or (o, c) in edges)]
    assert not bad, bad[:5]


@pytest.mark.parametrize("tick", _TICK_IDS)
@pytest.mark.parametrize("label", sorted(_GOLDEN_WORLDS))
def test_cooperation_groups_follow_first_seen_order_of_the_orders(label, tick):
    profile, _ = _GOLDEN_WORLDS[label]
    for trucks, orders in (_sampled(tick), (copy.deepcopy(_TICKS[tick]["trucks"]),
                                             copy.deepcopy(_TICKS[tick]["orders"]))):
        stray_t, stray_o = _strays(trucks, orders)
        trucks, orders = trucks + stray_t, stray_o + orders  # a haulier-less order seen FIRST
        assert _sut_groups(profile, trucks, orders) == _oracle_groups(profile, trucks, orders)


def test_the_golden_ticks_have_several_groups():
    """Not vacuous: the order of one group cannot be wrong, so most ticks must have several."""
    sizes = {(label, tick): len(_oracle_groups(profile, _TICKS[tick]["trucks"], _TICKS[tick]["orders"]))
             for label, (profile, _) in _GOLDEN_WORLDS.items() for tick in _TICK_IDS}
    assert sum(n > 1 for n in sizes.values()) >= 6, sizes


def test_the_arm_components_are_the_connected_components_of_its_adjacency():
    """The oracle keys groups by the arm's ``components`` index; that list must be the adjacency's."""
    for profile, _ in _GOLDEN_WORLDS.values():
        for arm in _arms(profile):
            multi = {frozenset(map(str, c)) for c in arm.get("components") or [] if len(c) > 1}
            assert multi == _connected_components(arm), arm.get("id")


@pytest.mark.parametrize("world", _ceiling_and_pool_worlds())
def test_active_edges_lie_inside_the_ceiling_of_all_arms(world):
    profile = _profile_of(world)
    ceiling = set().union(*(_edges(arm) for arm in _arms(profile)))
    assert _edges(_active(profile)) <= ceiling


@pytest.mark.parametrize("world", _ceiling_and_pool_worlds())
def test_every_pool_lies_inside_its_arms_adjacency_both_ways(world):
    """Message 14's premise, over every arm (not only the active one)."""
    profile = _profile_of(world)
    for arm in _arms(profile):
        edges = _edges(arm)
        for pool_id, members in _pools(arm):
            for a, b in itertools.combinations(sorted(set(members)), 2):
                assert (a, b) in edges and (b, a) in edges, (arm.get("id"), pool_id, a, b)
