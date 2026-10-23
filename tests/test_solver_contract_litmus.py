"""Litmus (a): a solver written ONLY against the contract runs end to end, in process.

Plan ``plan_contract_final.md`` §7.12 (WS8), litmus (a). Two toy policies are defined
here, under ``isolated_registry``, and driven through the REAL ``AssignmentApp.assign`` /
``publish`` (the golden harness's stub manager and messenger are COPIED below, not
imported, so later edits to the harness cannot change what this test feeds the app):

- ``LitmusZones`` (a composite): at ``bind`` it reads its OWN world key
  ``litmus_zones`` (``{haulier: zone}``; the profile carries it) and the scenario's
  ``cooperation`` block, and declares the active structure's ``(owner, carrier)`` pairs
  -- a subset of the scenario's partnerships (the ceiling). Each tick it splits the
  documents by zone and calls its inner through
  ``tick.call(params.inner, tick.restrict(...))``. It returns ``tick_notes`` that its
  OWN run-stats class (``meta_key="litmus"``) folds.
- ``LitmusNearest`` (a leaf): the nearest permitted free truck per order, by
  ``tick.measure.deadhead_km``; every pair carries
  ``solver_notes={"litmus_reason": "nearest"}``.

What is asserted (the six items of §7.12):

1. the published JSON of the SHARED pair carries ``"solver_notes": {"litmus_reason":
   "nearest"}`` and the own-company pair carries no ``solver_notes`` key;
2. ``run_config.meta.litmus`` is PATCHed (through a stub HTTP session);
3. the run stamp (``framework.policies_stamp`` over ``bind.bind_profile``, the sim
   main's pair of calls) carries the toy's provenance and ``world_reads`` ⊇
   ``{"litmus_zones"}``;
4. a declared pair outside the ceiling is message 23;
5. an inner that widens the top's declaration is message 24;
6. message 25 on a SEPARATE tree: ``NonReader``, a leaf that reads no world key,
   selected as a central top on a cooperative profile, refuses the ``--structure``
   patch. ``LitmusZones`` cannot serve here: it must read ``cooperation`` to declare
   its pairs (plan L2).

Nothing in ``policies/`` names either toy: the framework serves a new solver with zero
framework edits. The test's own import set is pinned with ``ast``
(:func:`test_the_litmus_imports_only_the_contract_surface`): ``policies.framework``,
``policies.dispatch.{tick,result,bind,run_stats}`` and ``assignment.app`` -- plus ONE
documented addition, ``scenario.scenario_overrides.apply_cooperation_override``, the
``--structure`` entry point item 6 applies (world plumbing, not the solver contract).

Verified to have teeth (WS8, mutate / see it fail / restore; ``git diff`` clean after):

- dropping the ``solver_notes`` append from ``compat/wire.py::render_audited_tag``
  failed item 1 (the shared pair's payload had no ``solver_notes`` key);
- making ``bind.py::_check_top`` skip the ceiling (``outside = []``) failed item 4
  (no ``PolicyError`` raised);
- making ``bind.py::_check_inner`` skip the subset rule (``wider = []``) failed item 5;
- making ``bind.py::refuse_unread_patches`` return unconditionally failed item 6.
"""

from __future__ import annotations

import ast
import copy
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from apps.container_logistics.assignment.app import AssignmentApp
from apps.container_logistics.policies.dispatch.bind import Declaration, bind_profile, refuse_unread_patches
from apps.container_logistics.policies.dispatch.result import Result
from apps.container_logistics.policies.dispatch.run_stats import RunStatsAggregator
from apps.container_logistics.policies.dispatch.tick import Tick
from apps.container_logistics.policies.framework import (
    PolicyError,
    PolicyParams,
    PolicyRef,
    param,
    policies_stamp,
    policy,
    ref,
    resolve_policies_block,
)
from apps.container_logistics.policies.framework.registry import isolated_registry
from apps.container_logistics.scenario.scenario_overrides import apply_cooperation_override

RUN_ID = "run_litmus"
SIM_CLOCK = "2020-01-01 08:00:00"
NOTE = {"litmus_reason": "nearest"}


# --- the stubs: COPIED from tests/golden/harness.py (StubManager, StubMessenger) -------


class StubManager:
    """In-memory stand-in for ``AssignmentManager`` serving one tick state."""

    def __init__(self, trucks, orders, claimed_order_ids=(), busy_ids=()):
        self._trucks = trucks
        self._orders = orders
        self._claimed = set(claimed_order_ids)
        self._busy = list(busy_ids)

    def list_trucks(self, projection=None, online_only=True):
        return list(self._trucks)

    def list_unassigned_orders(self, projection=None):
        return list(self._orders)

    def order_ids_with_open_haul(self):
        return set(self._claimed)

    def active_haul_truck_ids(self):
        return list(self._busy)

    def online_state_name(self):
        return "online"

    def resolve_facility_resource_id(self, name):
        return None


class StubMessenger:
    """Records every ``client.publish(topic, message)`` as the raw string pair."""

    def __init__(self):
        self.published: list[tuple[str, str]] = []
        self.client = self

    def publish(self, topic, message):
        self.published.append((topic, message))


class StubUser:
    def get_headers(self, etag=None):
        return {"If-Match": etag} if etag else {}


class StubResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class StubSession:
    """The run-record HTTP session ``dispatch.run_stats.patch_run_config_meta`` uses."""

    def __init__(self):
        self.patches: list[tuple[str, dict[str, Any]]] = []

    def get(self, url, headers=None, params=None, timeout=None):
        return StubResponse(200, {"_items": [{"_id": "rc_litmus", "_etag": "etag0"}]})

    def patch(self, url, headers=None, data=None, timeout=None):
        self.patches.append((url, json.loads(data)))
        return StubResponse(200)


# --- the world ---------------------------------------------------------------------------


def _pt(lon, lat):
    return {"type": "Point", "coordinates": [lon, lat]}


def _truck(tid, haulier, lon, lat=1.30):
    return {"_id": tid, "state": "online", "haulier_id": haulier,
            "profile": {"haulier_id": haulier, "current_loc": _pt(lon, lat), "truck_size": "40ft"}}


def _order(oid, haulier, lon, lat=1.30):
    return {"_id": oid, "state": "unassigned", "haulier_id": haulier, "pickup_loc": _pt(lon, lat),
            "dropoff_loc": _pt(lon + 0.05, lat), "profile": {"haulier_id": haulier}}


def _cooperation(active="pair"):
    """Two structures: ``pair`` (acme <-> borax) and ``solo``; the ceiling is the pair's two edges."""
    return {
        "active": active,
        "structures": [
            {"id": "pair", "edges": [["acme", "borax"]], "adjacency": {"acme": ["borax"], "borax": ["acme"]},
             "components": [["acme", "borax"], ["cargo"]],
             "pools": [{"id": "p:acme+borax", "members": ["acme", "borax"]}]},
            {"id": "solo", "edges": [], "adjacency": {}, "components": [["acme"], ["borax"], ["cargo"]],
             "pools": []},
        ],
    }


ZONES = {"acme": "west", "borax": "west", "cargo": "east"}

# acme's own truck is ~11 km from acme's order, borax's ~1 km: the leaf picks borax (a share).
TRUCKS = [_truck("t_acme", "acme", 103.95), _truck("t_borax", "borax", 103.86), _truck("t_cargo", "cargo", 104.20)]
ORDERS = [_order("o_acme", "acme", 103.85), _order("o_cargo", "cargo", 104.21)]


def _profile(top_params=None, *, top="LitmusZones", cooperation=None):
    entry = {"type": top, "params": dict(top_params or {})}
    return {
        "policies": {"planner": entry},
        "policies_format": 2,
        "cooperation": cooperation if cooperation is not None else _cooperation(),
        "litmus_zones": dict(ZONES),
        "respect_truck_online_state": True,
        "reject_if_active_haul_trip": True,
    }


# --- the toy policies ---------------------------------------------------------------------


class LitmusStats:
    """The composite's OWN run stats: ticks and pairs, pushed whenever something new was folded."""

    meta_key = "litmus"

    def __init__(self):
        self.ticks = 0
        self.zones = 0
        self.pairs = 0
        self._pushed = 0

    def fold(self, tick_notes, clock):
        self.ticks += 1
        self.zones += int(tick_notes.get("zones", 0))
        self.pairs += int(tick_notes.get("pairs", 0))
        return False

    def push_due(self, *, force=False):
        return self.ticks > self._pushed

    def mark_pushed(self):
        self._pushed = self.ticks

    def snapshot(self):
        return {"ticks": self.ticks, "zones": self.zones, "pairs": self.pairs}

    @classmethod
    def empty_snapshot(cls):
        return {"ticks": 0, "zones": 0, "pairs": 0}


#: The toy aggregator satisfies the framework's (structural) run-stats protocol.
_STATS_CLASS: "type[RunStatsAggregator]" = LitmusStats


def _active_pairs(world) -> frozenset[tuple[str, str]]:
    """``(owner, carrier)`` for every owner in the active structure's adjacency of a carrier."""
    coop = world.get("cooperation") or {}
    active = next((s for s in coop.get("structures", []) if s.get("id") == coop.get("active")), {})
    return frozenset((owner, carrier) for carrier, owners in (active.get("adjacency") or {}).items()
                     for owner in owners if owner != carrier)


def _register_toys():
    @policy(slot="planner", name="LitmusNearest", version="1",
            description="Litmus leaf: the nearest permitted free truck per order.")
    class LitmusNearest:
        @dataclass(frozen=True)
        class Params(PolicyParams):
            widen: bool = param(False, help="declare a pair the top did not (message 24)")

        def __init__(self, params=None):
            self.params = params

        def bind(self, params, world, *, top):
            return Declaration(cross_company={("cargo", "acme")} if params.widen else frozenset())

        def solve_tick(self, tick: Tick, params) -> Result:
            used: set[int] = set()
            pairs = []
            for o in tick.raw_orders:
                options = [t for t in tick.raw_trucks if id(t) not in used and tick.permitted(t, o)]
                if options:
                    best = min(options, key=lambda t: (tick.measure.deadhead_km(t, o), str(t["_id"])))
                    used.add(id(best))
                    pairs.append((best, o))
            return Result.from_pairs(pairs, solver_notes={(str(t["_id"]), str(o["_id"])): dict(NOTE)
                                                          for t, o in pairs})

    @policy(slot="planner", name="LitmusZones", version="1",
            description="Litmus composite: one inner call per zone of the litmus_zones world key.")
    class LitmusZones:
        @dataclass(frozen=True)
        class Params(PolicyParams):
            inner: PolicyRef = param(ref("LitmusNearest"), kind="planner", leaf_only=True, help="per-zone solver")
            overreach: bool = param(False, help="declare a pair outside the ceiling (message 23)")

        def __init__(self, params=None, inner=None, inner_node=None):
            self.params = params
            self.inner = inner
            self.inner_node = inner_node

        def bind(self, params, world, *, top):
            zones = dict(world.get("litmus_zones") or {})
            if not top:
                return Declaration()
            pairs = set(_active_pairs(world))
            if params.overreach:
                pairs.add(("acme", "cargo"))
            return Declaration(cross_company=pairs, provenance={"litmus": "zones", "zones": len(set(zones.values()))},
                               run_stats=LitmusStats)

        def solve_tick(self, tick: Tick, params) -> Result:
            zone_of = tick.world.get("litmus_zones") or {}
            by_zone: dict[str, tuple[list, list]] = {}
            for t in tick.raw_trucks:
                by_zone.setdefault(zone_of.get(t.get("haulier_id")), ([], []))[0].append(t)
            for o in tick.raw_orders:
                by_zone.setdefault(zone_of.get(o.get("haulier_id")), ([], []))[1].append(o)
            pairs, notes = [], {}
            for zone, (trucks, orders) in by_zone.items():
                if zone is None or not trucks or not orders:
                    continue
                result = tick.call(params.inner, tick.restrict(trucks=trucks, orders=orders,
                                                                scope=f"litmus:{zone}", sample_rng=tick.rng))
                if result is not None:
                    pairs.extend(result.pairs)
                    notes.update(result.solver_notes)
            return Result(pairs=tuple(pairs), solver_notes=notes,
                          tick_notes={"zones": len(by_zone), "pairs": len(pairs)})

    @policy(slot="planner", name="NonReader", version="1", description="Litmus leaf that reads no world key.")
    class NonReader:
        @dataclass(frozen=True)
        class Params(PolicyParams):
            pass

        def __init__(self, params=None):
            self.params = params

        def bind(self, params, world, *, top):
            return Declaration()

        def solve_tick(self, tick, params):
            return Result(pairs=())


@pytest.fixture
def toys():
    with isolated_registry():
        _register_toys()
        yield


def _app(profile):
    """An ``AssignmentApp`` built like the golden harness's ``build_app`` (stubs copied above)."""
    app = AssignmentApp.__new__(AssignmentApp)
    app.behavior = {"profile": copy.deepcopy(profile)}
    app.run_id = RUN_ID
    app.manager = StubManager(copy.deepcopy(TRUCKS), copy.deepcopy(ORDERS))
    app.messenger = StubMessenger()
    app.user = StubUser()
    app._solver = AssignmentApp._build_dispatch_policy(app.behavior["profile"])
    app._solver_params = {}
    app._warned_haulier_block = False
    app._share_tags = {}
    return app


# --- items 1-3: the run ---------------------------------------------------------------------


def test_a_contract_only_solver_runs_end_to_end(toys, monkeypatch):
    session = StubSession()
    monkeypatch.setattr("apps.common.resource_client_mixin.get_http_session", lambda: session)
    app = _app(_profile())
    random.seed(11)
    pairs = app.assign(SIM_CLOCK, 42)
    app.publish(pairs)

    assert [(t["_id"], o["_id"]) for t, o in pairs] == [("t_borax", "o_acme"), ("t_cargo", "o_cargo")]
    published = {json.loads(msg)["truck_id"]: json.loads(msg) for _topic, msg in app.messenger.published}
    # 1. the shared pair carries the leaf's note; the own-company pair carries no key at all.
    shared, own = published["t_borax"], published["t_cargo"]
    assert shared["shared"] is True and shared["owner_haulier_id"] == "acme" and shared["carrier_haulier_id"] == "borax"
    assert shared["solver_notes"] == NOTE
    assert own["shared"] is False and "solver_notes" not in own

    # 2. the composite's own run stats were folded and PATCHed onto run_config.meta.litmus.
    assert set(app._run_stats) == {"litmus"}
    assert [(url.rsplit("/", 2)[-2:], body) for url, body in session.patches] == [
        (["run-config", "rc_litmus"], {"meta.litmus": {"ticks": 1, "zones": 2, "pairs": 2}})]

    # 3. the run stamp (the sim main's calls) carries the toy's provenance and its own world key.
    profile = _profile()
    tree = bind_profile(profile)
    resolved = resolve_policies_block(profile["policies"], where="profile.policies")
    stamp = policies_stamp(resolved, tree=tree, accounting_params={}, source="bundle")["planner"]
    assert stamp["name"] == "LitmusZones" and stamp["label"] == "LitmusZones/LitmusNearest"
    assert stamp["provenance"]["planner"] == {"litmus": "zones", "zones": 2}
    assert {"litmus_zones", "cooperation"} <= set(stamp["world_reads"])
    assert stamp["declaration"] == {"cross_company": [["acme", "borax"], ["borax", "acme"]]}


# --- items 4-6: the refusals -------------------------------------------------------------------


def test_a_pair_outside_the_ceiling_is_message_23(toys):
    with pytest.raises(PolicyError, match=r"declared cross-company pair\(s\) \('acme', 'cargo'\).*no structure of "
                                          r"the scenario declares"):
        bind_profile(_profile({"overreach": True}))
    with pytest.raises(PolicyError, match="no structure of the scenario declares"):
        _app(_profile({"overreach": True})).assign(SIM_CLOCK, 42)


def test_an_inner_widening_the_top_is_message_24(toys):
    widen = {"inner": {"type": "LitmusNearest", "params": {"widen": True}}}
    with pytest.raises(PolicyError, match=r"a nested node's bind\(\) declared cross-company pair\(s\) "
                                          r"\('cargo', 'acme'\) the top did not declare"):
        bind_profile(_profile(widen))


def test_a_structure_patch_on_a_tree_that_reads_no_cooperation_is_message_25(toys):
    profile = _profile(top="NonReader")
    collection = {"assignment_main": {"profile": profile}}
    applied, patched = apply_cooperation_override(collection, "solo")
    assert (applied, patched) == ("solo", frozenset({"cooperation"}))
    tree = bind_profile(profile)
    assert "cooperation" not in tree.world_reads
    with pytest.raises(PolicyError, match=r"patched world key\(s\) 'cooperation'.*NonReader"):
        refuse_unread_patches(tree, patched)
    # The reader composite takes the same patch (it reads cooperation at bind).
    zones = _profile()
    zcoll = {"assignment_main": {"profile": zones}}
    _applied, zpatched = apply_cooperation_override(zcoll, "solo")
    ztree = bind_profile(zones)
    refuse_unread_patches(ztree, zpatched)
    assert ztree.declaration.cross_company == frozenset(), "the solo arm declares no pairs"


# --- the import surface -------------------------------------------------------------------------

_P = "apps.container_logistics.policies"
_ALLOWED_PROJECT = {
    f"{_P}.framework",
    f"{_P}.framework.registry",        # isolated_registry (tests only)
    f"{_P}.dispatch.tick",
    f"{_P}.dispatch.result",
    f"{_P}.dispatch.bind",
    f"{_P}.dispatch.run_stats",
    "apps.container_logistics.assignment.app",
    # The one documented addition: the --structure override entry point item 6 applies.
    "apps.container_logistics.scenario.scenario_overrides",
}


def test_the_litmus_imports_only_the_contract_surface():
    """The project modules this file imports are EXACTLY the contract surface (plan §7.12).

    The string target of ``monkeypatch.setattr`` (the stub HTTP session) is test
    plumbing, not an import. Verified to have teeth: adding
    ``from apps.container_logistics.policies.dispatch.profile import bind_instance``
    failed this test.
    """
    tree = ast.parse(Path(__file__).read_text())
    project = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            mods = [node.module]
        elif isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        else:
            continue
        project.update(m for m in mods if m.split(".")[0] in ("apps", "openride", "openride_control", "tests"))
    assert project == _ALLOWED_PROJECT, (sorted(project - _ALLOWED_PROJECT), sorted(_ALLOWED_PROJECT - project))
