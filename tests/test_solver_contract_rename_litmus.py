"""Litmus (c), the RENAME litmus: the framework has no name-keyed branch for the built-ins.

Plan ``plan_contract_final.md`` §7.12 (WS8) and §8 ("No name-keyed framework branch").
Litmus (a) and (b) prove that a NEW solver is served by the generic contract; this test
proves the converse for the OLD ones: if the framework anywhere decided something by
the registered NAME of ``Partitioned`` / ``PooledMarket`` / ``GreedyNearest`` /
``RandomAssignment`` (``if top.name == "PooledMarket": ...``), a run of the very same
classes under different names would come out different.

How. Under ``isolated_registry`` the four built-in classes are registered again as
subclasses under fresh names -- ``RenamedPartitioned(Partitioned)``,
``RenamedPooled(PooledMarket)``, ``RenamedGreedy(GreedyNearestSolver)``,
``RenamedRandom(RandomAssignmentSolver)`` -- with the built-in's own ``@policy``
metadata except ``name`` (copied from its registered ``PolicySpec``, so it cannot drift)
and its inherited ``Params``. Subclasses are needed because one class backs exactly one
name (``registry._store``). Each of the 68 ``dispatch_golden_v1`` cases is then
retargeted:

1. the original (legacy) profile is resolved with ``resolve_profile_policies``;
2. every planner type in the resolved block, nested ``inner`` refs included, is renamed
   (component refs such as the market's offer/claim/arbitration are left alone: they
   are not planner policies);
3. the block is written as a compiled ``policies`` block + ``policies_format: 2``;
4. ``strategy`` is set to the renamed leaf (the harness instantiates ``_solver`` from it);
5. ``use_spatial_matching`` and the legacy ``planner`` selection keys are dropped
   (``topology``, ``market`` and also ``deployment``, the third legacy spelling of the
   solver, so no legacy key carrying an OLD name survives anywhere in the profile).

The case runs through the frozen golden harness's ``run_case`` (imported read-only) and
every output -- ``pairs``, ``round_stats``, ``rng_probe``, ``published``, ``share_tags``
-- must equal the stored golden expectation. The legacy translation, which also names
solvers, is NOT exercised: the profiles are compiled blocks; that is exactly its
documented compat role.

The ONE documented exception is the compat v1 share-tag rule
(``compat/wire.py::V1_SHARE_TAG_BUILTINS``), an explicit name-keyed rule: a run topped
by the registered NAME ``Partitioned`` renders the pre-v2 ``v1`` tag (4 keys), any other
top renders ``audited`` (8 keys). So on a Partitioned-topped case WITH shares (13 of the
68), the renamed run renders ``audited``. For those cases this test asserts, precisely:

- ``pairs``, ``round_stats`` and ``rng_probe`` are equal;
- each share tag is keyed on the same pair, in the same order, and its v1 VALUES
  (``shared``, ``owner_haulier_id``, ``carrier_haulier_id``, ``benefit_km``) equal the
  golden ones; the tag is exactly the audited key set; ``pool_id`` and ``market_round``
  are ``None`` (a partition has no pool or round); and ``benefit_km``
  agrees with ``owner_reserve_km - awarded_cost_km`` to the 3-dp rounding whenever both
  are known;
- the two audited-only values ``awarded_cost_km`` / ``owner_reserve_km`` (``None`` in v1)
  equal values DERIVED FROM THE GOLDEN, never copied from the renamed run (final review
  M10): the award is ``constraints.assignment_cost`` of the pair's STORED documents
  under the case's stored ``solver_params``, the reserve is the owner's cheapest own
  truck left free by the GOLDEN pairs (``pair_allowed`` with the stored
  ``max_travel_time_pickup``), both rounded to 3 dp, and ``reserve - award`` agrees with
  the golden v1 ``benefit_km`` to the 3-dp rounding (``None`` iff it is ``None``);
- every ``published`` payload equals the golden string after the golden payload's two
  audited-only fields are set to those golden-derived values (``tests/golden/compare.py``'s
  rules, applied to that re-dumped golden string; every stored payload re-dumps
  byte-identically). No other key of any payload may differ.

RUN STAMP AND RUN NAME (final review M10): for every case the renamed selection's run
stamp (``framework.policies_stamp`` over ``resolve_profile_policies`` + ``bind_profile``,
the sim main's pair of calls), its stamp settings and its run-name segments equal the
original selection's once each renamed name is mapped back -- except, on a
Partitioned-topped case, ``share_tag_format`` (``v1`` -> ``audited``, the same compat
rule). A name-keyed branch in the stamp, provenance or run declaration fails here.

BLIND SPOT, by construction: a renamed policy is a SUBCLASS of the built-in, so a branch
keyed on the class (``isinstance(x, GreedyNearestSolver)``) or on a param's SHAPE (a
built-in-only param name or value) is taken by both runs and this litmus cannot see it.
Those are covered by the stored expectations instead: the dispatch golden for
decomposition tops, ``tests/golden/central_top_v1.json.gz``
(``tests/test_solver_contract_central_top.py``) for leaf tops, and the registry-built
vocabulary guard for a param name spelled in framework code.

Every other case (Partitioned-topped without shares, and all pooled cases) must be equal
byte for byte, and the exception must actually fire (not vacuous). Any other difference
means the framework branches on a solver name, and the test fails.

The registry lookup the frozen harness makes for ``round_stats`` reads
``case["profile"]["planner"]["topology"]``; the retargeted profile's ``planner`` is
therefore a mapping that is EMPTY of selection keys to every reader (``get``, ``in``,
iteration, ``json.dumps``, which is what the framework uses) and answers the subscript
``["topology"]`` alone, for the harness's own pooled/partitioned report switch.

Verified to have teeth (WS8, mutate / see it fail / restore), in ``DispatchRunner.run``
(``dispatch/runner.py``), one module-RNG draw behind a name-keyed branch on
``tick._shared.tree.top.spec.name``:

- ``not in ("Partitioned", "PooledMarket", "GreedyNearest", "RandomAssignment")`` (a
  branch the golden identity test CANNOT see, since every golden run is topped by a
  built-in name): all 68 cases here failed (on ``pairs``, ``round_stats`` or
  ``rng_probe``) while ``test_dispatch_golden_identity`` stayed green (70 passed) -- the
  hole this litmus exists to close;
- ``== "Partitioned"``: the golden identity test fails as well (34 failures across the
  two files), so this test is not the only guard for that spelling.

``runner.py`` was restored byte for byte afterwards (diffed against a copy).
"""

from __future__ import annotations

import copy
import gzip
import json
from typing import Any

import pytest

from apps.container_logistics.assignment.solver.greedy_nearest import GreedyNearestSolver
from apps.container_logistics.assignment.solver.random_assignment import RandomAssignmentSolver
from apps.container_logistics.policies.dispatch.library.partitioned import Partitioned
from apps.container_logistics.policies.dispatch.library.pooled_market import PooledMarket
from apps.container_logistics.policies.dispatch.profile import (
    resolve_dispatch_for_profile,
    resolve_profile_policies,
)
from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.policies.dispatch.bind import bind_profile
from apps.container_logistics.policies.framework import get_policy_spec, policies_stamp, policy
from apps.container_logistics.policies.framework.registry import isolated_registry
from tests.golden.capture_dispatch_golden import FIXTURE_PATH
from tests.golden.compare import published_mismatch
from tests.golden.harness import TIME_STEP, run_case


class RenamedPartitioned(Partitioned):
    """``Partitioned`` under another name (inherits ``Params`` and every behaviour)."""


class RenamedPooled(PooledMarket):
    """``PooledMarket`` under another name."""


class RenamedGreedy(GreedyNearestSolver):
    """``GreedyNearest`` under another name."""


class RenamedRandom(RandomAssignmentSolver):
    """``RandomAssignment`` under another name."""


#: original registered name -> (renamed class, new name)
RENAMES: dict[str, tuple[type, str]] = {
    "Partitioned": (RenamedPartitioned, "RenamedPartitioned"),
    "PooledMarket": (RenamedPooled, "RenamedPooled"),
    "GreedyNearest": (RenamedGreedy, "RenamedGreedy"),
    "RandomAssignment": (RenamedRandom, "RenamedRandom"),
}
NEW_NAME = {old: new for old, (_cls, new) in RENAMES.items()}

#: The v1 tag's keys, and the audited tag's exact key set (``compat/wire.py``).
V1_KEYS = ("shared", "owner_haulier_id", "carrier_haulier_id", "benefit_km")
AUDITED_KEYS = ("shared", "owner_haulier_id", "carrier_haulier_id", "benefit_km", "pool_id",
                "awarded_cost_km", "owner_reserve_km", "market_round")
#: The payload fields the audited tag fills and the v1 tag leaves ``None``.
AUDITED_ONLY_PAYLOAD_FIELDS = ("awarded_cost_km", "owner_reserve_km")

with gzip.open(FIXTURE_PATH, "rb") as _fh:
    _FIXTURE = json.loads(_fh.read())
_CASES = {c["case_id"]: c for c in _FIXTURE["cases"]}


def _register_renamed() -> None:
    """Register each renamed subclass with its built-in's metadata except ``name``."""
    for old, (cls, new) in RENAMES.items():
        spec = get_policy_spec("planner", old)
        policy(
            slot=spec.slot, name=new, version=spec.version, description=spec.description,
            requires=spec.requires, order_sensitive=spec.order_sensitive,
            perf_budget_ms=spec.perf_budget_ms, module_rng=spec.module_rng,
            arm_budgets_ms=dict(spec.arm_budgets_ms) or None, offload=spec.offload,
            badge=spec.badge, badge_tooltip=spec.badge_tooltip,
        )(cls)


@pytest.fixture(scope="module")
def renamed_registry():
    with isolated_registry():
        _register_renamed()
        yield


class _HarnessOnlyPlanner(dict):
    """A ``planner`` mapping with no keys that answers ``["topology"]`` for the harness only.

    The frozen harness picks its ``round_stats`` report by subscripting
    ``case["profile"]["planner"]["topology"]``. Every framework read (``get``, ``in``,
    iteration, ``json.dumps``) sees the keys this dict really holds, and none of them is a
    selection key.
    """

    def __init__(self, items: dict[str, Any], topology: str):
        super().__init__(items)
        self.harness_topology = topology

    def __getitem__(self, key):
        if key == "topology" and not dict.__contains__(self, key):
            return self.harness_topology
        return dict.__getitem__(self, key)


def _renamed(value: Any) -> Any:
    """``value`` with every planner ``"type"`` renamed (component refs untouched)."""
    if isinstance(value, dict):
        out = {k: _renamed(v) for k, v in value.items()}
        if isinstance(out.get("type"), str) and out["type"] in NEW_NAME:
            out["type"] = NEW_NAME[out["type"]]
        return out
    if isinstance(value, list):
        return [_renamed(v) for v in value]
    return value


def _leaf_name(entry: dict[str, Any]) -> str:
    while isinstance(entry.get("params", {}).get("inner"), dict):
        entry = entry["params"]["inner"]
    return entry["type"]


def _retargeted(case: dict[str, Any]) -> dict[str, Any]:
    """The case with its profile as a compiled block of renamed planner types (steps 1-5)."""
    profile = copy.deepcopy(case["profile"])
    assert "policies" not in profile, "golden v1 profiles are legacy (no policies block)"
    resolved, source = resolve_profile_policies(profile)
    assert source == "legacy-profile"
    block = {slot: _renamed(r.to_profile()) for slot, r in resolved.items()}
    legacy_planner = profile.pop("planner")
    profile.pop("use_spatial_matching", None)
    profile["policies"] = block
    profile["policies_format"] = 2
    profile["strategy"] = _leaf_name(block["planner"])
    rest = {k: v for k, v in legacy_planner.items() if k not in ("topology", "market", "deployment")}
    profile["planner"] = _HarnessOnlyPlanner(rest, legacy_planner["topology"])
    out = dict(case)
    out["profile"] = profile
    out["strategy"] = profile["strategy"]
    return out


def _is_v1_exception(case: dict[str, Any]) -> bool:
    """A Partitioned-topped case with shares: the one documented compat difference."""
    return case["profile"]["planner"]["topology"] == "partitioned" and bool(case["expected"]["share_tags"])


def _golden_audited_values(case: dict[str, Any], tick: dict[str, Any]) -> dict[tuple[str, str], tuple[Any, Any]]:
    """``{(truck_id, order_id): (awarded_cost_km, owner_reserve_km)}`` of each GOLDEN share, from the golden alone.

    Derived from the stored tick documents, the stored golden pairs and the case's stored
    profile, never from the run under test: the award is ``assignment_cost`` of the
    pair under ``solver_params``; the reserve is the owner's cheapest own assignable
    truck that no golden pair commits, that ``pair_allowed`` admits under the stored
    ``max_travel_time_pickup`` (the accounting's own-haulier rule). 3 dp, ``None`` for a
    non-finite award / no free own truck.
    """
    import math

    prof = case["profile"]
    dials = prof.get("solver_params") or {}
    trucks = {str(t["_id"]): t for t in tick["trucks"]}
    orders = {str(o["_id"]): o for o in tick["orders"]}
    busy = {str(x) for x in tick["busy_ids"]} if prof.get("reject_if_active_haul_trip", True) else set()
    assignable = assign_constraints.filter_assignable_trucks(
        list(tick["trucks"]), busy,
        respect_truck_online_state=bool(prof.get("respect_truck_online_state", True)),
        reject_if_active_haul_trip=bool(prof.get("reject_if_active_haul_trip", True)),
        online_state_name="online")
    committed = {tid for tid, _oid in case["expected"]["pairs"]}
    mtt = prof.get("max_travel_time_pickup")
    out: dict[tuple[str, str], tuple[Any, Any]] = {}
    for (tid, oid), _tag in case["expected"]["share_tags"]:
        truck, order = trucks[tid], orders[oid]
        award = float(assign_constraints.assignment_cost(truck, order, dials))
        owner = assign_constraints.haulier_of(order)
        best = None
        for t in assignable:
            if str(t.get("_id")) in committed or assign_constraints.haulier_of(t) != owner:
                continue
            if not assign_constraints.pair_allowed(t, order, max_travel_time_pickup=mtt):
                continue
            c = float(assign_constraints.assignment_cost(t, order, dials))
            if math.isfinite(c) and (best is None or c < best):
                best = c
        out[(tid, oid)] = (round(award, 3) if math.isfinite(award) else None,
                           round(best, 3) if best is not None else None)
    return out


def _expected_published_with_audited_fields(expected_published, golden_values) -> list[list[str]]:
    """Golden payloads with ``awarded_cost_km``/``owner_reserve_km`` set from the GOLDEN-derived values."""
    out = []
    for topic, payload in expected_published:
        assert json.dumps(json.loads(payload)) == payload, "a golden payload does not re-dump verbatim"
        doc = json.loads(payload)
        key = (doc.get("truck_id"), (doc.get("order") or {}).get("_id"))
        if doc.get("shared"):
            for field, value in zip(AUDITED_ONLY_PAYLOAD_FIELDS, golden_values[key]):
                assert doc[field] is None, f"v1 payload already carried {field}"
                doc[field] = value
            payload = json.dumps(doc)
        out.append([topic, payload])
    return out


def _run_declaration(profile: dict[str, Any]) -> dict[str, Any]:
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


def _compiled_original(case: dict[str, Any]) -> dict[str, Any]:
    """The case's profile as a compiled block of the ORIGINAL names (the renamed run's twin)."""
    profile = copy.deepcopy(case["profile"])
    resolved, _source = resolve_profile_policies(profile)
    block = {slot: r.to_profile() for slot, r in resolved.items()}
    legacy_planner = profile.pop("planner")
    profile.pop("use_spatial_matching", None)
    profile["policies"] = block
    profile["policies_format"] = 2
    profile["strategy"] = _leaf_name(block["planner"])
    profile["planner"] = {k: v for k, v in legacy_planner.items() if k not in ("topology", "market", "deployment")}
    return profile


def _unname(value: Any) -> Any:
    """``value`` with each renamed policy name mapped back to its original (inside any string)."""
    text = json.dumps(value, sort_keys=True)
    for old, new in NEW_NAME.items():
        text = text.replace(new, old)
    return json.loads(text)


@pytest.mark.parametrize("case_id", sorted(_CASES))
def test_renamed_builtins_reproduce_the_golden_case(renamed_registry, case_id):
    case = _CASES[case_id]
    retargeted = _retargeted(case)
    # The run really is the renamed tree, top to leaf: no original name is selected.
    node, names = resolve_dispatch_for_profile(retargeted["profile"]), []
    while node is not None:
        names.append(node.name)
        node = node.inner
    assert names and all(n in NEW_NAME.values() for n in names), names

    actual = json.loads(json.dumps(run_case(retargeted, _FIXTURE["tick_states"])))
    expected = case["expected"]
    tick = _FIXTURE["tick_states"][case["tick_state"]]
    for field in ("pairs", "round_stats", "rng_probe"):
        assert actual[field] == expected[field], f"{case_id}: {field!r} differs under renamed solvers"

    if not _is_v1_exception(case):
        assert actual["share_tags"] == expected["share_tags"], f"{case_id}: share_tags differ"
        problem = published_mismatch(actual["published"], expected["published"],
                                     sim_clock=tick["sim_clock"], time_step=TIME_STEP)
        assert problem is None, f"{case_id}: published differs: {problem}"
        return

    # The documented compat v1 rule: same pairs, same v1 values, audited shape.
    assert [k for k, _ in actual["share_tags"]] == [k for k, _ in expected["share_tags"]], case_id
    for (key, got), (_key, want) in zip(actual["share_tags"], expected["share_tags"]):
        assert set(want) == set(V1_KEYS), f"{case_id}: the golden tag is not v1: {want}"
        assert set(got) == set(AUDITED_KEYS), f"{case_id} {key}: renamed tag is not audited: {got}"
        assert {k: got[k] for k in V1_KEYS} == want, f"{case_id} {key}: tag values differ"
        assert got["pool_id"] is None and got["market_round"] is None, f"{case_id} {key}: {got}"
        if got["owner_reserve_km"] is not None and got["awarded_cost_km"] is not None:
            # Both sides are rounded to 3 dp separately, so they agree to one unit there.
            assert abs(got["benefit_km"] - (got["owner_reserve_km"] - got["awarded_cost_km"])) <= 0.0011, got
    golden = _golden_audited_values(case, tick)
    for (key, got), (_key, want) in zip(actual["share_tags"], expected["share_tags"]):
        award, reserve = golden[tuple(key)]
        assert (got["awarded_cost_km"], got["owner_reserve_km"]) == (award, reserve), (
            f"{case_id} {key}: audited values {got['awarded_cost_km']}/{got['owner_reserve_km']} differ from the "
            f"golden-derived {award}/{reserve}")
        if want["benefit_km"] is None:
            assert reserve is None, f"{case_id} {key}: golden benefit None but a reserve was derived"
        elif award is not None:
            assert abs(want["benefit_km"] - (reserve - award)) <= 0.0011, (case_id, key, want, award, reserve)
    want_published = _expected_published_with_audited_fields(expected["published"], golden)
    problem = published_mismatch(actual["published"], want_published,
                                 sim_clock=tick["sim_clock"], time_step=TIME_STEP)
    assert problem is None, f"{case_id}: published differs beyond the audited fields: {problem}"


def test_the_compat_exception_is_exercised_and_bounded():
    """The v1 exception fires on real cases (not vacuous) and only on Partitioned-topped ones."""
    exceptions = [cid for cid, c in _CASES.items() if _is_v1_exception(c)]
    assert len(_CASES) == 68
    assert len(exceptions) == 13, exceptions
    pooled = [cid for cid, c in _CASES.items() if c["profile"]["planner"]["topology"] == "pooled"]
    assert pooled and not set(pooled) & set(exceptions)
    # A shared payload on an exception case really carries audited-only fields to compare.
    assert any(t["shared"] for cid in exceptions for _k, t in _CASES[cid]["expected"]["share_tags"])


@pytest.mark.parametrize("case_id", sorted(_CASES))
def test_renamed_builtins_give_the_same_stamp_and_run_name(renamed_registry, case_id):
    """M10: the run stamp, stamp settings and run-name segments are rename-invariant (modulo the names)."""
    case = _CASES[case_id]
    want = _run_declaration(_compiled_original(case))
    got = _run_declaration(_retargeted(case)["profile"])
    top = want["stamp"]["planner"]["name"]
    assert got["stamp"]["planner"]["name"] == NEW_NAME[top], got["stamp"]["planner"]["name"]
    if top == "Partitioned":
        # The documented compat v1 rule, and nothing else.
        assert (want["stamp"]["planner"]["share_tag_format"], got["stamp"]["planner"]["share_tag_format"]) == \
            ("v1", "audited"), case_id
        got["stamp"]["planner"]["share_tag_format"] = "v1"
    assert _unname(got) == _unname(want), f"{case_id}: the stamp / run declaration differs beyond the name fields"
