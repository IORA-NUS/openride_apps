"""Central tops: every leaf selected at TOP level, through the real ``assign`` / ``publish``.

Final review H3: all 68 golden and rename-litmus cases are topped by ``Partitioned`` or
``PooledMarket``, so a framework branch that fires only under a LEAF top passed them
all -- the reviewer's mutation (``publish()`` drops one assignment when the top is
``RandomAssignment``) left the guards, litmus (a)/(b)/(c) and the goldens green. Three
tests close it, each through ``AssignmentApp.assign`` + ``publish`` (the frozen golden
harness's ``build_app``, imported read-only):

1. :func:`test_central_top_reproduces_its_golden` -- the 34 central-top cases of
   ``tests/golden/central_top_v1.json.gz`` (every partitioned v1 case retargeted to its
   own leaf, captured at ``44922b4`` before any fix-loop edit; see
   ``tests/golden/capture_central_top_golden.py``): pairs, share tags, the module-RNG
   probe and the published payloads (count + sha256 of the canonical list) must equal
   the stored ones. A STORED expectation, so unlike a rename it also sees a branch keyed
   on the class (``isinstance``) or on a param's shape, not only on a name.
2. :func:`test_every_committed_pair_is_published_once_in_order` -- for EVERY registered
   leaf (read from the registry, so a new built-in leaf is covered without an edit) and
   a contract-only toy leaf, on a small and a dense tick: the published topics are
   exactly the committed pairs' trucks, in order, one payload per pair; each payload
   names its pair's truck and order; a payload is ``shared`` iff its pair has a share
   tag, and its share fields are that tag's. Needs no stored expectation.
3. :func:`test_a_renamed_leaf_top_is_byte_identical` -- the rename litmus with a
   central top: each built-in leaf re-registered under a fresh name (a subclass, its
   ``@policy`` metadata copied from its ``PolicySpec``) gives byte-identical pairs,
   share tags, published payloads and RNG probe, and the same run stamp, stamp
   settings and run-name segments modulo the name fields.

Verified to have teeth (FB, mutate / see it fail / restore; ``git diff`` clean after),
in ``assignment/app.py::publish``, ``if runner.tree.top.name == "RandomAssignment":
assignment = assignment[1:]`` (the reviewer's mutation, via the cached runner):

- (1) failed on all 12 RandomAssignment central cases (``published_count``);
- (2) failed on the RandomAssignment rows (one pair unpublished);
- (3) failed on the RandomAssignment rows (the renamed run published one more payload);
- keyed on ``isinstance(self._solver, RandomAssignmentSolver)`` instead (a branch the
  renamed subclass shares, so (3) is blind to it), (1) and (2) still failed.
See the hand-back of fix group FB for the run.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

import pytest

from apps.container_logistics.policies.dispatch.bind import bind_profile
from apps.container_logistics.policies.dispatch.profile import resolve_profile_policies
from apps.container_logistics.policies.dispatch.result import Result
from apps.container_logistics.policies.framework import PolicyParams, get_policy_spec, policies_stamp, policy
from apps.container_logistics.policies.framework.registry import (
    discover_builtin_policies,
    isolated_registry,
    list_policies,
)
from tests.golden.capture_central_top_golden import (
    canonical_published,
    load_central,
    load_v1,
    run_central,
    summarise,
)
from tests.golden.harness import RUN_ID

_V1 = load_v1()
_CENTRAL = load_central()
_CASES = {c["case_id"]: c for c in _CENTRAL["cases"]}


def _state(case: dict[str, Any]) -> dict[str, Any]:
    return _V1["tick_states"][case["tick_state"]]


# --- 1. the stored central-top golden --------------------------------------------------

def test_the_central_fixture_is_what_it_claims():
    assert _CENTRAL["format"] == 1 and len(_CASES) == 34
    tops = [c["profile"]["policies"]["planner"]["type"] for c in _CASES.values()]
    assert sorted(set(tops)) == ["GreedyNearest", "RandomAssignment"]
    assert tops.count("GreedyNearest") == 22 and tops.count("RandomAssignment") == 12
    for case in _CASES.values():
        planner = case["profile"]["planner"]
        assert not {"topology", "market", "deployment"} & set(planner), case["case_id"]
        assert "inner" not in (case["profile"]["policies"]["planner"].get("params") or {})
    # Not vacuous: shares are exercised under both leaves.
    assert {c["profile"]["strategy"] for c in _CASES.values() if c["expected"]["share_tags"]} == {
        "GreedyNearest", "RandomAssignment"}


@pytest.mark.parametrize("case_id", sorted(_CASES))
def test_central_top_reproduces_its_golden(case_id):
    case = _CASES[case_id]
    state = _state(case)
    got = summarise(run_central(copy.deepcopy(case["profile"]), state), sim_clock=state["sim_clock"])
    want = case["expected"]
    for field in ("pairs", "rng_probe", "share_tags", "published_count", "published_sha256"):
        assert got[field] == want[field], f"{case_id}: {field!r} differs from the central-top golden"


# --- 2. the publish invariant, every registered leaf ----------------------------------

class _CentralToyLeaf:
    """Nearest permitted free truck per order (orders by id), no RNG, contract surface only."""

    @dataclass(frozen=True)
    class Params(PolicyParams):
        pass

    def __init__(self, params=None):
        self.params = params

    def bind(self, params, world, *, top):
        from apps.container_logistics.policies.dispatch.bind import Declaration

        return Declaration()

    def solve_tick(self, tick, params):
        used: set[int] = set()
        pairs = []
        for o in sorted(tick.raw_orders, key=lambda d: str(d.get("_id"))):
            options = [t for t in tick.raw_trucks if id(t) not in used and tick.permitted(t, o)]
            if options:
                best = min(options, key=lambda t: (tick.measure.deadhead_km(t, o), str(t.get("_id"))))
                used.add(id(best))
                pairs.append((best, o))
        return Result.from_pairs(pairs)


_SHARE_FIELDS = ("owner_haulier_id", "carrier_haulier_id", "benefit_km", "pool_id", "awarded_cost_km",
                 "owner_reserve_km", "market_round")


def _assert_published_is_the_pairs(label: str, out: dict[str, Any]) -> None:
    pairs = [tuple(p) for p in out["pairs"]]
    tags = {tuple(k): t for k, t in out["share_tags"]}
    published = out["published"]
    assert [topic for topic, _ in published] == [f"{RUN_ID}/{tid}" for tid, _ in pairs], (
        f"{label}: published topics are not the committed pairs' trucks, in order "
        f"({len(published)} payloads for {len(pairs)} pairs)")
    for (tid, oid), (_topic, payload) in zip(pairs, published):
        doc = json.loads(payload)
        assert (doc["truck_id"], doc["order"]["_id"]) == (tid, oid), f"{label}: payload of {tid}/{oid}"
        tag = tags.get((tid, oid))
        assert doc["shared"] is (tag is not None), f"{label}: {tid}/{oid} shared={doc['shared']}, tag={tag}"
        if tag is not None:
            assert {k: doc[k] for k in _SHARE_FIELDS} == {k: tag.get(k) for k in _SHARE_FIELDS}, (
                f"{label}: {tid}/{oid} payload share fields differ from its tag")
    assert set(tags) <= set(pairs), f"{label}: a share tag for an uncommitted pair"


def _leaf_names() -> list[str]:
    discover_builtin_policies()
    return sorted(s.name for s in list_policies("planner") if not s.is_composite)


#: One case per tick state with shares (chain arm) and the small one: T (4 x 4), B (500 x 120).
_INVARIANT_BASES = ("central:T-greedy-partitioned-chain-spatial-cap500",
                    "central:B-greedy-partitioned-chain-spatial-cap500")


def _retarget_top(profile: dict[str, Any], name: str) -> dict[str, Any]:
    out = copy.deepcopy(profile)
    spec = get_policy_spec("planner", name)
    from apps.container_logistics.policies.framework.params import declared_params

    declared = {i.name for i in declared_params(spec.params_cls)}
    old = out["policies"]["planner"].get("params") or {}
    out["policies"]["planner"] = {"type": name, "params": {k: v for k, v in old.items() if k in declared}}
    out["strategy"] = name
    return out


@pytest.mark.parametrize("base", _INVARIANT_BASES)
def test_every_committed_pair_is_published_once_in_order(base):
    names = _leaf_names()
    assert {"GreedyNearest", "RandomAssignment"} <= set(names), names
    case = _CASES[base]
    state = _state(case)
    with isolated_registry():
        # Registered only inside the isolated registry (never in the live one).
        policy(slot="planner", name="CentralToyLeaf", version="1",
               description="Contract-only leaf for the central-top publish invariant.")(_CentralToyLeaf)
        for name in [*names, "CentralToyLeaf"]:
            if name == "CentralToyLeaf":
                profile = copy.deepcopy(case["profile"])
                profile["policies"]["planner"] = {"type": name, "params": {}}
                profile["strategy"] = "GreedyNearest"   # the harness's get_solver; bind_instance wraps it
                out = _run_with_registered_top(profile, state)
            else:
                out = run_central(_retarget_top(case["profile"], name), state)
            assert out["pairs"], f"{base} / {name}: nothing committed, the invariant would be vacuous"
            _assert_published_is_the_pairs(f"{base} / {name}", out)


def _run_with_registered_top(profile: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Like ``run_central``, but the app's ``_solver`` is built from the compiled block (a contract-only leaf).

    The frozen harness instantiates ``_solver`` through the legacy ``get_solver`` (a
    built-in name only); for a registered non-built-in top the app is given the tree
    ``AssignmentApp._build_dispatch_policy`` builds, exactly as ``__init__`` does.
    """
    import random

    from apps.container_logistics.assignment.app import AssignmentApp
    from tests.golden.harness import MODULE_SEED, TIME_STEP, build_app

    app = build_app(profile, profile["strategy"], state)
    app._solver = AssignmentApp._build_dispatch_policy(app.behavior["profile"])
    random.seed(MODULE_SEED)
    result = app.assign(state["sim_clock"], TIME_STEP)
    app.publish(result)
    share_tags = sorted(([list(k), t] for k, t in (app._share_tags or {}).items()),
                        key=lambda row: (row[0][0], row[0][1]))
    return json.loads(json.dumps({
        "pairs": [[str(t.get("_id")), str(o.get("_id"))] for t, o in result],
        "share_tags": share_tags,
        "published": [list(p) for p in app.messenger.published],
    }))


# --- 3. the rename litmus with a central top ----------------------------------------------

def _renamed_classes() -> dict[str, tuple[type, str]]:
    from apps.container_logistics.assignment.solver.greedy_nearest import GreedyNearestSolver
    from apps.container_logistics.assignment.solver.random_assignment import RandomAssignmentSolver

    return {
        "GreedyNearest": (type("CentralRenamedGreedy", (GreedyNearestSolver,), {}), "CentralRenamedGreedy"),
        "RandomAssignment": (type("CentralRenamedRandom", (RandomAssignmentSolver,), {}),
                             "CentralRenamedRandom"),
    }


@pytest.fixture(scope="module")
def renamed_leaves():
    renames = _renamed_classes()
    with isolated_registry():
        for old, (cls, new) in renames.items():
            spec = get_policy_spec("planner", old)
            policy(slot=spec.slot, name=new, version=spec.version, description=spec.description,
                   requires=spec.requires, order_sensitive=spec.order_sensitive,
                   perf_budget_ms=spec.perf_budget_ms, module_rng=spec.module_rng,
                   arm_budgets_ms=dict(spec.arm_budgets_ms) or None, offload=spec.offload,
                   badge=spec.badge, badge_tooltip=spec.badge_tooltip)(cls)
        yield {old: new for old, (_cls, new) in renames.items()}


def _stamp_and_declaration(profile: dict[str, Any]) -> dict[str, Any]:
    """The sim main's stamp pair (``resolve_profile_policies`` + ``bind_profile``) and the run declaration."""
    resolved, source = resolve_profile_policies(profile)
    tree = bind_profile(profile)
    stamp = policies_stamp(resolved, tree=tree, accounting_params=profile.get("solver_params") or {},
                           source=source)
    decl = tree.declaration
    return json.loads(json.dumps({
        "stamp": stamp,
        "stamp_settings": dict(decl.stamp_settings),
        "run_name_segments": list(decl.run_name_segments),
        "cross_company": sorted(map(list, decl.cross_company)),
        "world_reads": sorted(tree.world_reads),
    }, default=str))


def _unname(value: Any, names: dict[str, str]) -> Any:
    """``value`` with every renamed policy name mapped back to the original (whole strings and inside text)."""
    text = json.dumps(value, sort_keys=True)
    for old, new in names.items():
        text = text.replace(new, old)
    return json.loads(text)


@pytest.mark.parametrize("case_id", sorted(_CASES))
def test_a_renamed_leaf_top_is_byte_identical(renamed_leaves, case_id):
    case = _CASES[case_id]
    state = _state(case)
    old = case["profile"]["policies"]["planner"]["type"]
    new = renamed_leaves[old]
    renamed = copy.deepcopy(case["profile"])
    renamed["policies"]["planner"]["type"] = new
    renamed["strategy"] = new

    want = run_central(copy.deepcopy(case["profile"]), state)
    got = run_central(renamed, state)
    for field in ("pairs", "share_tags", "rng_probe"):
        assert got[field] == want[field], f"{case_id}: {field!r} differs under the renamed central top"
    assert canonical_published(got["published"], sim_clock=state["sim_clock"]) == \
        canonical_published(want["published"], sim_clock=state["sim_clock"]), \
        f"{case_id}: published payloads differ under the renamed central top"

    want_decl = _stamp_and_declaration(copy.deepcopy(case["profile"]))
    got_decl = _stamp_and_declaration(renamed)
    assert want_decl["stamp"]["planner"]["name"] == old and got_decl["stamp"]["planner"]["name"] == new
    assert _unname(got_decl, renamed_leaves) == _unname(want_decl, renamed_leaves), (
        f"{case_id}: the stamp / run declaration differs beyond the name fields")
