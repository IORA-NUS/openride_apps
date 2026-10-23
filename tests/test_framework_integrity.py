"""The framework core holds against a DISHONEST solver, not only an honest one (fix loop, group FA).

The final adversarial review (``review_final/findings.md``) showed that ownership held
only while the solver played along. Each test below is one of its attacks, run through
the real path (``bind_tree`` / ``DispatchRunner`` / ``AssignmentApp.assign`` +
``publish``), and asserts the attack is refused while an honest solver is untouched:

- **H1** -- a ``bind`` mutating the world it is handed widened the ceiling and the
  mutation reached the agents' profile. Now: world values are the world's own deep
  copies, the ceiling is computed before any bind, and a profile changed by a bind is
  refused (:class:`~apps.container_logistics.policies.dispatch.bind.BindError`).
- **H2** -- the final check read tick state the solver can rewrite. The five attacks of
  the finding: reassigning ``tick.permitted``; pre-seeding ``tick.__dict__["id_index"]``;
  reassigning ``raw_trucks``; an in-place ``truck["haulier_id"]`` rewrite (which hid a
  cross-company haul from the accounting); an inner overriding ``sub.permitted`` under
  ``Partitioned`` so the verified-pair memo skipped the physical check.
- **M2** -- a run-stats ``meta_key`` that names a framework key of ``run_config.meta``.
- **M13** -- a contract-only solver's stamped provenance carries its ``@policy`` badge.
- **L1** -- a malformed ``cross_company`` is message 23; a non-``PolicyError`` raised in
  ``bind`` is a ``PolicyError`` naming the node.
- **L2** -- ``RunWorld``: no bypass of the read record, no splat, iteration records nothing.
- **L3** -- notes cannot set framework wire fields they did not declare, and are plain JSON.
- **L5** -- an inner's own ``cross_company`` narrows its sub-ticks.
- **L6** -- ``tick.serves(None, None)`` is ``False``.
- **L7** -- a raising run-stats aggregator is contained; the pairs are unchanged.

Each test names its teeth: the mutation of the framework that was made to watch it
fail, then reverted (``git diff`` clean afterwards).
"""

from __future__ import annotations

import ast
import copy
import inspect
import json
import logging
import pickle
import random
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from apps.container_logistics.assignment import constraints as C
from apps.container_logistics.policies.dispatch import run_stats
from apps.container_logistics.policies.dispatch.bind import BindError, Declaration, RunWorld, bind_tree
from apps.container_logistics.policies.dispatch.invariants import check_wire_notes
from apps.container_logistics.policies.dispatch.result import Result
from apps.container_logistics.policies.dispatch.runner import build_runner_for_policy
from apps.container_logistics.policies.dispatch.tick import DispatchClock, Measure, Tick
from apps.container_logistics.policies.framework import PolicyError, PolicyParams, PolicyRef, param, policy, ref
from apps.container_logistics.policies.framework.errors import DispatchContractViolation
from apps.container_logistics.policies.framework.registry import isolated_registry
from tests.test_solver_contract_litmus import SIM_CLOCK, _app
from tests.test_solver_contract_litmus import _profile as _litmus_profile
from tests.test_solver_contract_types import _order, _profile, _top_tick, _tree, _truck

CLOCK = DispatchClock(sim_clock="x", time_step=1)

# acme <-> borax are partners in the litmus world; cargo is partnered with nobody, so
# acme's truck serving cargo's order is an UNDECLARED cross-company pair.
UNDECLARED = ("t_acme", "o_cargo")


def _docs(tick):
    return ({t["_id"]: t for t in tick.raw_trucks}, {o["_id"]: o for o in tick.raw_orders})


def _register_evil():
    """Probe solvers (the reviewer's ``own/probe_commit.py``), one ``mode`` param each."""

    @policy(slot="planner", name="Evil", version="1", description="integrity probe")
    class Evil:
        @dataclass(frozen=True)
        class Params(PolicyParams):
            mode: str = param("plain", help="probe mode")

        def __init__(self, params=None):
            self.params = params

        def bind(self, params, world, *, top):
            if params.mode == "widen_ceiling":
                world["cooperation"]["structures"].append({"id": "evil", "adjacency": {"acme": ["cargo"]}})
                return Declaration(cross_company={("cargo", "acme")})
            if params.mode == "declare_lists":
                return Declaration(cross_company=[["acme", "borax"]])
            if params.mode == "bind_raises":
                raise KeyError("boom")
            if params.mode in ("note_pool", "note_nan", "note_obj", "note_ok"):
                world.get("cooperation")
                return Declaration(cross_company={("acme", "borax"), ("borax", "acme")})
            return Declaration()

        def solve_tick(self, tick, params):
            t, o = _docs(tick)
            m = params.mode
            if m == "reassign_permitted":
                tick.permitted = lambda a, b: True
            elif m == "preseed_id_index":
                tick.__dict__["id_index"] = ({"t_acme": {"_id": "t_acme", "haulier_id": "cargo"}},
                                             {"o_cargo": o["o_cargo"]})
            elif m == "reassign_raw_trucks":
                forged = copy.deepcopy(t["t_acme"])
                forged["haulier_id"] = "cargo"
                tick.raw_trucks = (forged,) + tuple(tick.raw_trucks)
            elif m == "mutate_doc":
                t["t_acme"]["haulier_id"] = "cargo"
                t["t_acme"]["profile"]["haulier_id"] = "cargo"
            elif m == "mutate_order":  # relabel cargo's order as acme's: an "own" pair
                o["o_cargo"]["haulier_id"] = "acme"
                o["o_cargo"]["profile"]["haulier_id"] = "acme"
            elif m == "mutate_unpaired":
                # An honest own pair, but another truck's document was rewritten: the share
                # accounting reads every truck after the commit, so the whole tick is refused.
                t["t_borax"]["haulier_id"] = "cargo"
                return Result(pairs=(("t_cargo", "o_cargo"),))
            elif m == "private_state":  # the reviewer's probe_tamper ``verified`` mode
                tick._shared.owns = lambda a, b: True
                tick._shared.verified.add((id(t["t_acme"]), id(o["o_cargo"])))
            elif m == "measure_dials":  # the accounting prices the committed pairs with it
                tick.measure.dials = {"dual_cycle_bonus_km": 1e9}
                return Result(pairs=(("t_cargo", "o_cargo"),))
            elif m.startswith("note_"):
                note = {"note_pool": {"pool_id": "forged-pool", "market_round": 99},
                        "note_nan": {"x": float("nan")},
                        "note_obj": {"x": object()},
                        "note_ok": {"zone": "west", "n": 1}}[m]
                return Result(pairs=(("t_borax", "o_acme"),), solver_notes={("t_borax", "o_acme"): note})
            return Result(pairs=(UNDECLARED,))

    @policy(slot="planner", name="EvilLeaf", version="1", description="integrity probe leaf")
    class EvilLeaf:
        @dataclass(frozen=True)
        class Params(PolicyParams):
            mode: str = param("plain", help="probe mode")

        def __init__(self, params=None):
            self.params = params

        def bind(self, params, world, *, top):
            return Declaration()

        def solve_tick(self, tick, params):
            if params.mode == "sub_permitted":
                tick.permitted = lambda a, b: True
            pair = [(tr["_id"], o["_id"]) for tr in tick.raw_trucks for o in tick.raw_orders
                    if (tr["_id"], o["_id"]) == ("t_acme", "o_acme")]
            return Result(pairs=tuple(pair))


def _run(top, params=None, *, phys=False, stats=None):
    """Assign + publish one litmus tick with ``top``; returns ``(pairs, published, violations)``."""
    app = _app(_litmus_profile(params or {}, top=top))
    if phys:  # a 20ft truck may not take a 40ft order: own pair, physically infeasible
        for x in app.manager._trucks:
            if x["_id"] == "t_acme":
                x["profile"]["truck_size"] = "20ft"
        for x in app.manager._orders:
            if x["_id"] == "o_acme":
                x["profile"]["order_size"] = "40ft"
    random.seed(1)
    pairs = app.assign(SIM_CLOCK, 1)
    app.publish(pairs)
    published = [json.loads(m) for _, m in app.messenger.published]
    runner = app._dispatch_runner(app._assignment_profile())
    return [(t["_id"], o["_id"]) for t, o in pairs], published, runner.stats["violations"], app


@pytest.fixture
def evil():
    with isolated_registry():
        _register_evil()
        yield


# --- H1 ---------------------------------------------------------------------------------


def test_h1_a_bind_mutating_its_world_reaches_neither_the_ceiling_nor_the_profile(evil):
    """The reviewer's ``worldmut``: the appended structure stays in the bind's own copy.

    Before the fix the pair committed, published as shared, and the profile's structures
    grew ``['pair', 'solo', 'evil', 'evil']``. Teeth: making ``_WorldState.value`` return
    the raw profile value (no deep copy) failed this test (the profile grew).
    """
    prof = _litmus_profile({"mode": "widen_ceiling"}, top="Evil")
    before = copy.deepcopy(prof)
    with pytest.raises(PolicyError, match=r"\('cargo', 'acme'\) \(owner, carrier\) that no structure"):
        _run("Evil", {"mode": "widen_ceiling"})
    for _ in range(2):  # repeated binds never append either
        with pytest.raises(PolicyError, match="that no structure"):
            build_runner_for_policy("Evil", {"mode": "widen_ceiling"}, profile=prof)
    assert prof == before
    assert [s["id"] for s in prof["cooperation"]["structures"]] == ["pair", "solo"]


def test_h1_a_bind_that_changes_the_profile_by_any_other_route_is_refused():
    """A bind holding the profile by reference (not through the world) is caught by the fingerprint.

    Teeth: deleting the ``_check_profile_unchanged`` call from ``bind_tree`` let the bind
    run and leave ``prof["spatial_cell_deg"]`` at 9.
    """
    prof = _profile()

    class Sneaky:
        inner = None

        def bind(self, params, world, *, top):
            prof["spatial_cell_deg"] = 9
            return Declaration()

    node = SimpleNamespace(name="Sneaky", version="1", params=None, inner=None, source="policies")
    with pytest.raises(BindError, match="bind\\(\\) changed the assignment profile"):
        bind_tree(node, Sneaky(), prof)


def test_h1_world_values_are_copies():
    """Teeth: returning ``raw`` from ``_WorldState.value`` failed the last assertion."""
    prof = {"cooperation": {"structures": [1]}, "k": [1, 2]}
    world = RunWorld.from_profile(prof)
    world["k"].append(3)
    world.get("cooperation")["structures"].append(2)
    assert world["k"] == [1, 2, 3]  # the same copy on every read through this world
    assert prof == {"cooperation": {"structures": [1]}, "k": [1, 2]}


# --- H2: the five attacks of the finding, plus the private-state and measure variants -----


@pytest.mark.parametrize("mode", ["reassign_permitted", "preseed_id_index", "reassign_raw_trucks", "mutate_doc", "mutate_order",
                                  "mutate_unpaired", "private_state", "measure_dials"])
def test_h2_tampering_with_the_tick_commits_nothing(evil, mode, caplog):
    """Attacks 1-4 of H2 (and three variants) through ``AssignmentApp.assign`` + ``publish``.

    Before the fix attacks 1, 2 and 4 committed the undeclared ``(t_acme, o_cargo)`` with
    ``violations == 0``; attack 4 published it ``shared=False`` (hidden from the research
    metric).

    Teeth: ``git checkout 44922b4 -- policies/dispatch/{tick,runner}.py assignment/app.py``
    (the code before the fix) failed every case, each by committing a pair; separately, deleting only the
    ``shared.changed()`` scan from ``DispatchRunner.run`` failed ``mutate_unpaired``, and
    only ``intact=shared.intact`` from its ``check_result`` call failed ``mutate_order``
    (a mutated TRUCK is also caught by the scan).
    """
    with caplog.at_level(logging.ERROR):
        pairs, published, violations, _app_ = _run("Evil", {"mode": mode})
    assert UNDECLARED not in pairs and pairs == [] and published == []
    assert violations == 1
    assert "PLANNER POLICY CONTRACT VIOLATION" in caplog.text


def test_h2_attack_5_an_inner_overriding_its_sub_permitted_cannot_skip_the_physical_check(evil, caplog):
    """Attack 5: under ``Partitioned`` an inner rewrote ``sub.permitted``; the memo then skipped physics.

    A 20ft truck took a 40ft order (own company) before the fix. Now the rewrite is a
    contract violation in the sub-tick (message 19) and nothing is committed.

    Teeth: ``git checkout 44922b4 -- policies/dispatch/{tick,runner}.py assignment/app.py``
    committed ``(t_acme, o_acme)``.
    """
    with caplog.at_level(logging.ERROR):
        pairs, _pub, violations, _a = _run("Partitioned", {"inner": {"type": "EvilLeaf", "params": {"mode": "sub_permitted"}}},
                                          phys=True)
    assert ("t_acme", "o_acme") not in pairs
    assert violations >= 1 and "Tick.permitted is read-only" in caplog.text
    # Control: the honest leaf proposes the same infeasible pair and is refused the normal way.
    pairs, _pub, violations, _a = _run("Partitioned", {"inner": {"type": "EvilLeaf", "params": {"mode": "plain"}}},
                                      phys=True)
    assert ("t_acme", "o_acme") not in pairs and violations >= 1


def test_h2_the_tick_has_no_writable_framework_state():
    """Teeth: removing ``Tick.__setattr__`` made the first ``raises`` pass silently."""
    prof = _profile()
    tree, _top, _leaf = _tree(prof)
    tick = _top_tick(tree, [_truck("t1", "h_a")], [_order("o1", "h_a")], prof)
    for name in ("permitted", "raw_trucks", "raw_orders", "id_index", "world", "measure", "scratch", "seed",
                 "tiebreak_seed", "rebates", "_shared", "_permitted", "_index"):
        with pytest.raises(DispatchContractViolation, match="read-only"):
            setattr(tick, name, None)
    with pytest.raises(DispatchContractViolation, match="no instance __dict__"):
        tick.__dict__
    assert not hasattr(tick, "__dict__")
    with pytest.raises(DispatchContractViolation, match="read-only"):
        tick._shared.owns = None
    with pytest.raises(DispatchContractViolation, match="read-only"):
        tick.measure.dials = {}
    tick.scratch["memo"] = 1  # the one mutable member
    assert tick.scratch == {"memo": 1}


# --- M2 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["policies", "simulation_settings", "scenario_slug", "policies.planner",
                                 "$set", "", 3, None])
def test_m2_a_run_stats_meta_key_naming_a_framework_key_is_refused_at_bind(key):
    """Teeth: an early ``return`` in ``run_stats.check_meta_key`` made every case bind."""
    stats_cls = type("S", (), {"meta_key": key})
    prof = _profile()
    with pytest.raises(PolicyError, match="meta_key"):
        _tree(prof, top_decl=Declaration(run_stats=stats_cls))
    ok = type("S", (), {"meta_key": "my_solver"})
    assert _tree(prof, top_decl=Declaration(run_stats=ok))[0].declaration.run_stats is ok


def test_m2_reserved_meta_keys_cover_every_key_the_run_start_meta_writes():
    """The reserved set is pinned against ``scenario_manager.get_run_config_meta``'s source.

    Teeth: removing ``"policies"`` from ``RESERVED_META_KEYS`` failed this test.
    """
    from apps.container_logistics.scenario.scenario_manager import ScenarioManager

    tree = ast.parse(inspect.getsource(ScenarioManager.get_run_config_meta).strip().replace("\n    ", "\n"))
    written = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict) and \
                any(isinstance(t, ast.Name) and t.id == "meta" for t in node.targets):
            written |= {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "meta" \
                and isinstance(node.ctx, ast.Store) and isinstance(node.slice, ast.Constant):
            written.add(node.slice.value)
    assert "policies" in written and len(written) >= 10
    assert written <= run_stats.RESERVED_META_KEYS, sorted(written - run_stats.RESERVED_META_KEYS)


# --- M13 --------------------------------------------------------------------------------


def test_m13_a_contract_only_solver_stamps_its_registered_badge():
    """Teeth: making ``_with_registered_badge`` always return ``declaration`` unchanged failed it."""
    with isolated_registry():
        @policy(slot="planner", name="Badged", version="1", description="contract-only", badge="central")
        class Badged:
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def __init__(self, params=None):
                self.params = params

            def solve_tick(self, tick, params):
                return Result()

        @policy(slot="planner", name="OwnBadge", version="1", description="declares its own", badge="central")
        class OwnBadge(Badged):
            def bind(self, params, world, *, top):
                return Declaration(provenance={"badge": "mine", "x": 1})

        assert build_runner_for_policy("Badged").tree.top.declaration.provenance == {"badge": "central"}
        assert dict(build_runner_for_policy("OwnBadge").tree.top.declaration.provenance) == {"badge": "mine", "x": 1}
        # Under Partitioned the badge of the TOP is Partitioned's own; the leaf gets none.
        tree = build_runner_for_policy("Partitioned", {"inner": {"type": "Badged"}}).tree
        assert tree.top.declaration.provenance["badge"] == "per group"
        assert "badge" not in tree.top.inner.declaration.provenance


# --- L1 ---------------------------------------------------------------------------------


def test_l1_a_malformed_cross_company_is_message_23_and_a_bind_crash_is_a_policy_error(evil):
    """Teeth: ``_pair_set`` reduced to ``return frozenset(value)`` raised ``TypeError``; narrowing
    ``_call_bind``'s ``except Exception`` to ``ZeroDivisionError`` let ``KeyError`` escape."""
    with pytest.raises(PolicyError, match=r"malformed cross-company pair\(s\) \['acme', 'borax'\]"):
        build_runner_for_policy("Evil", {"mode": "declare_lists"}, profile=_litmus_profile())
    with pytest.raises(BindError, match=r"planner policy Evil at planner: bind\(\) raised KeyError: 'boom'") as info:
        build_runner_for_policy("Evil", {"mode": "bind_raises"}, profile=_litmus_profile())
    assert isinstance(info.value.__cause__, KeyError)
    assert Declaration(cross_company=5).cross_company and Declaration(cross_company=None).cross_company == frozenset()


# --- L2 ---------------------------------------------------------------------------------


def test_l2_run_world_reads_cannot_be_bypassed_and_splats_are_refused():
    """Teeth: deleting the ``RunWorld.keys`` override (back to the ``Mapping`` mixin) let ``dict(world)``
    succeed and mark every key read."""
    prof = {"cooperation": {"a": 1}, "k1": 1, "k2": [1, 2], "policies": {"x": 1}, "strategy": "s"}
    world = RunWorld.from_profile(prof)
    assert sorted(world) == ["cooperation", "k1", "k2"] and len(world) == 3
    assert world.reads == frozenset()  # the key list is free
    for splat in (dict, lambda w: (lambda **kw: kw)(**w), lambda w: w.keys(), lambda w: w.items(),
                  lambda w: w.values()):
        with pytest.raises(TypeError, match="read the world keys you use one by one"):
            splat(world)
    with pytest.raises(TypeError):
        pickle.dumps(world)
    assert world.reads == frozenset()
    for name in ("unread", "_data", "_reads", "__dict__"):
        assert not hasattr(world, name), name
    assert world["k1"] == 1 and world.get("absent") is None and "k2" in world
    assert world.reads == {"k1", "absent", "k2"}
    world.reads.__class__  # a fresh frozenset: nothing to clear
    assert copy.copy(world) is world and copy.deepcopy(world) is world


# --- L3 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("mode,problem", [("note_pool", "sets the framework wire field(s) 'market_round', 'pool_id'"),
                                          ("note_nan", "is not plain JSON"), ("note_obj", "is not plain JSON")])
def test_l3_notes_cannot_forge_wire_fields_and_must_be_plain_json(evil, mode, problem, caplog):
    """Before: ``pool_id='forged-pool'`` / ``market_round=99`` reached the wire as market provenance,
    and an ``object()`` was published through ``default=str``.

    Teeth: dropping the ``check_wire_notes`` call from ``DispatchRunner.run`` committed all three.
    """
    with caplog.at_level(logging.ERROR):
        pairs, published, violations, _a = _run("Evil", {"mode": mode})
    assert pairs == [] and published == [] and violations == 1
    assert problem in caplog.text
    pairs, published, violations, _a = _run("Evil", {"mode": "note_ok"})
    assert pairs == [("t_borax", "o_acme")] and violations == 0
    assert published[0]["solver_notes"] == {"zone": "west", "n": 1} and published[0]["pool_id"] is None


def test_l3_a_declared_wire_note_key_is_accepted():
    """``Declaration.wire_note_keys`` (the market's ``pool_id`` / ``market_round``) is the only way in."""
    result = Result(pairs=(("t", "o"),), solver_notes={("t", "o"): {"pool_id": "p", "market_round": 1}})
    reserved = frozenset({"shared", "pool_id", "market_round"})
    assert check_wire_notes(result, reserved - {"pool_id", "market_round"}) == []
    assert check_wire_notes(result, reserved)


# --- L5 / L6 ------------------------------------------------------------------------------


def test_l5_an_inner_declaring_a_subset_narrows_its_sub_ticks_and_l6_serves_needs_both_ids():
    """Teeth: disabling the ``own_cross`` narrowing in ``Tick.call`` failed the sub-tick assertions;
    deleting the missing-id guard of ``serves`` failed the ``None`` assertion."""
    prof = _profile(edges=(("h_a", "h_b"), ("h_a", "h_c")))
    top_decl = Declaration(cross_company={("h_a", "h_b"), ("h_b", "h_a"), ("h_a", "h_c"), ("h_c", "h_a")})
    seen = {}

    def pairs(sub):
        seen["serves"] = (sub.serves("h_b", "h_a"), sub.serves("h_c", "h_a"), sub.serves("h_a", "h_a"))
        seen["permitted"] = (sub.permitted(trucks[1], orders[0]), sub.permitted(trucks[2], orders[0]))
        return ()

    trucks = [_truck("ta", "h_a"), _truck("tb", "h_b"), _truck("tc", "h_c")]
    orders = [_order("oa", "h_a")]
    for inner_decl, want in ((Declaration(cross_company={("h_a", "h_b")}), (True, False)),
                             (Declaration(), (True, True))):
        tree, _top, _leaf = _tree(prof, top_decl=top_decl, inner_decl=inner_decl, inner_pairs=pairs)
        tick = _top_tick(tree, trucks, orders, prof)
        assert tick.serves("h_c", "h_a") and tick.permitted(trucks[2], orders[0])
        tick.call("inner", tick.restrict(trucks=trucks, orders=orders, scope="s", sample_rng=None))
        assert seen["serves"] == (*want, True) and seen["permitted"] == want
    assert tick.serves(None, None) is False and tick.serves("", "") is False and tick.serves("h_a", None) is False
    assert tick.serves("h_a", "h_a") is True


# --- L7 ---------------------------------------------------------------------------------


class _Boom:
    meta_key = "boom"

    def __init__(self):
        self.n = 0

    def fold(self, tick_notes, clock):
        self.n += 1
        raise RuntimeError("fold exploded")

    def push_due(self, *, force=False):
        return True

    def mark_pushed(self):
        pass

    def snapshot(self):
        return {"n": self.n}

    @classmethod
    def empty_snapshot(cls):
        return {}


def test_l7_a_raising_fold_is_contained_and_marked_invalid(caplog):
    """Teeth: narrowing ``record_tick``'s ``except Exception`` to ``ZeroDivisionError`` let
    ``RuntimeError`` escape (this test and the app-level one below failed)."""
    store, pushes = {}, []
    with caplog.at_level(logging.ERROR):
        run_stats.record_tick(store, _Boom, {}, CLOCK, lambda k, v: pushes.append((k, v)) or True)
    assert "RUN STATS DISABLED" in caplog.text and "fold exploded" in caplog.text
    assert pushes == [("boom", {"invalid": True, "error": "fold raised RuntimeError: fold exploded"})]
    run_stats.record_tick(store, _Boom, {}, CLOCK, lambda k, v: pushes.append((k, v)) or True)
    run_stats.flush(store, lambda k, v: pushes.append((k, v)) or True)
    assert len(pushes) == 1  # retired: never folded or pushed again
    assert store["boom"].snapshot()["invalid"] is True


def test_l7_the_assignment_tick_keeps_its_pairs_when_the_aggregator_raises(monkeypatch):
    """Through ``AssignmentApp``: the same pairs as without run stats, and they are published."""
    from tests.test_solver_contract_litmus import StubSession

    monkeypatch.setattr("apps.common.resource_client_mixin.get_http_session", lambda: StubSession())
    with isolated_registry():
        @policy(slot="planner", name="Stats", version="1", description="declares a raising aggregator")
        class Stats:
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def __init__(self, params=None):
                self.params = params

            def bind(self, params, world, *, top):
                return Declaration(run_stats=_Boom) if top else Declaration()

            def solve_tick(self, tick, params):
                return Result(pairs=(("t_cargo", "o_cargo"),), tick_notes={"x": 1})

        pairs, published, violations, app = _run("Stats")
    assert pairs == [("t_cargo", "o_cargo")] and len(published) == 1 and violations == 0
    assert app._run_stats["boom"].snapshot()["invalid"] is True
