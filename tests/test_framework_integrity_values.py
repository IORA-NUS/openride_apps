"""The contract holds against crafted VALUES (re-review round 2: N1-N6, N9, L1).

Round 2 of the adversarial review found that the core read a solver's result more
than once: a ``str`` subclass id hashed as one order during the check and as another
at commit (N1), a note mapping ran solver code after the document scan (N2) and a
lying mapping showed one thing to the checks and another to the wire (N3). The fix
reads the result ONCE, into plain data with exact builtin types
(``invariants.normalise_result``), commits exactly the documents the check judged, and
makes the document scan the last step before the commit. Also here:

- **N4** -- a run-stats aggregator's INSTANCE ``meta_key`` cannot redirect its push;
- **N5** -- the agent's live profile is not reachable through the world's state table,
  and a re-bind can never widen the run-start ceiling;
- **N6** -- the verified-pair memo has no public writer;
- **N9** -- a refused ``RunWorld`` access is a contract violation (message 18), not a
  crash of every tick;
- **L1** -- a non-``PolicyError`` raised by a solver's ``__init__`` is a ``PolicyError``
  naming the solver.

Threat model: crafted values are in scope; interpreter-level tampering (reflection,
monkeypatching framework modules) is not (``dispatch/tick.py`` module docstring).

Each test names its teeth: the mutation of the framework made to watch it fail, then
reverted (``git diff`` clean afterwards).
"""

from __future__ import annotations

import copy
import json
import logging
import random
from dataclasses import dataclass
from typing import Any

import pytest

from apps.container_logistics.policies.dispatch import bind as bind_mod
from apps.container_logistics.policies.dispatch import run_stats
from apps.container_logistics.policies.dispatch.bind import Declaration, SolverInitError, WorldAccessError, bind_profile
from apps.container_logistics.policies.dispatch.invariants import check_result, normalise_result
from apps.container_logistics.policies.dispatch.result import Result
from apps.container_logistics.policies.dispatch.runner import build_runner_for_policy
from apps.container_logistics.policies.dispatch.tick import DispatchClock
from apps.container_logistics.policies.framework import PolicyError, PolicyParams, param, policy
from apps.container_logistics.policies.framework.errors import DispatchContractViolation
from apps.container_logistics.policies.framework.registry import isolated_registry
from tests.test_solver_contract_litmus import SIM_CLOCK, _app, _cooperation
from tests.test_solver_contract_litmus import _profile as _lprofile

DECLARED = {("acme", "borax"), ("borax", "acme")}


@dataclass(frozen=True)
class _P(PolicyParams):
    mode: str = param("plain", help="probe mode")


def _docs(tick):
    return {x["_id"]: x for x in tick.raw_trucks}, {x["_id"]: x for x in tick.raw_orders}


def _toy(solve, *, declare=frozenset(), name="Probe", run_stats_cls=None, init=None):
    """Register a top-level planner whose ``solve_tick`` is ``solve(tick, trucks, orders)``."""

    @policy(slot="planner", name=name, version="1", description="value probe")
    class Probe:
        Params = _P

        def __init__(self, params=None):
            if init is not None:
                init()
            self.params = params

        def bind(self, params, world, *, top):
            world.get("cooperation")
            return Declaration(cross_company=set(declare), run_stats=run_stats_cls)

        def solve_tick(self, tick, params):
            t, o = _docs(tick)
            return solve(tick, t, o)

    return Probe


def _run(profile_top="Probe", ticks=1, profile=None):
    app = _app(profile or _lprofile({}, top=profile_top))
    pairs = []
    for step in range(1, ticks + 1):
        random.seed(1)
        pairs = app.assign(SIM_CLOCK, step)
        app.publish(pairs)
    published = [json.loads(m) for _t, m in app.messenger.published]
    runner = app._dispatch_runner(app._assignment_profile())
    return app, [(t["_id"], o["_id"]) for t, o in pairs], published, runner.stats["violations"]


# --- N1: a str-subclass id -------------------------------------------------------------------


class _Chameleon(str):
    """Hashes / compares as ``o_acme`` until flipped, then as ``o_cargo`` (the review's probe)."""

    flipped = False

    def _now(self):
        return "o_cargo" if _Chameleon.flipped else "o_acme"

    def __hash__(self):
        return hash(self._now())

    def __eq__(self, other):
        return str.__eq__(self._now(), other)

    def __ne__(self, other):
        return not self.__eq__(other)


class _FlipOnIter(dict):
    calls = 0

    def _flip(self):
        _FlipOnIter.calls += 1
        if _FlipOnIter.calls >= 2:
            _Chameleon.flipped = True

    def __iter__(self):
        self._flip()
        return super().__iter__()

    def items(self):
        self._flip()
        return super().items()


def test_n1_a_str_subclass_pair_id_is_refused_never_retargeted():
    """TEETH: ``type(x) is str`` -> ``isinstance(x, str)`` in ``invariants._plain_pair`` and
    ``check_result``: the probe committed ``t_acme -> o_cargo`` (cargo's order) with 0 violations."""
    _Chameleon.flipped, _FlipOnIter.calls = False, 0

    def solve(tick, t, o):
        pair = ("t_acme", _Chameleon("o_acme"))
        return Result(pairs=(pair,), solver_notes={pair: _FlipOnIter(x=1)})

    with isolated_registry():
        _toy(solve)
        _app_, committed, published, violations = _run()
    assert committed == [] and published == [] and violations == 1


def test_n1_a_check_reports_the_str_subclass_by_type():
    with isolated_registry():
        _toy(lambda tick, t, o: Result())
        runner = build_runner_for_policy("Probe")
    from tests.test_solver_contract_litmus import ORDERS, TRUCKS

    tick = runner.tick(candidates=copy.deepcopy(TRUCKS), orders=copy.deepcopy(ORDERS), profile=None,
                       clock=DispatchClock(SIM_CLOCK, 1), seed=1)
    problems = check_result(Result(pairs=(("t_acme", _Chameleon("o_acme")),)), tick)
    assert problems == ["pair ('t_acme', <_Chameleon 'o_acme'>) is not a (truck_id, order_id) pair of strings"]


# --- N2 / N3: solver code inside the notes ----------------------------------------------------


class _Effect(dict):
    """A note whose k-th access (any of ``__iter__`` / ``keys`` / ``items`` / ``values`` /
    ``__getitem__`` / ``get`` / ``__len__``) runs ``effect``: wherever the framework reads it."""

    def __init__(self, base, effect, at):
        super().__init__(base)
        self.n, self.effect, self.at = 0, effect, at

    def _tick(self):
        self.n += 1
        if self.n == self.at:
            self.effect()

    def __iter__(self):
        self._tick()
        return super().__iter__()

    def keys(self):
        self._tick()
        return super().keys()

    def items(self):
        self._tick()
        return super().items()

    def values(self):
        self._tick()
        return super().values()

    def __getitem__(self, k):
        self._tick()
        return super().__getitem__(k)

    def get(self, k, d=None):
        self._tick()
        return super().get(k, d)

    def __len__(self):
        self._tick()
        return super().__len__()


@pytest.mark.parametrize("at", range(1, 8))
def test_n2_a_note_that_retargets_the_commit_is_caught_by_the_last_scan(at):
    """The review's ``toctou_retarget``, swept over WHEN the note's code runs.

    TEETH: moving the ``shared.changed`` scan in ``DispatchRunner.run`` back before
    ``check_wire_notes`` (and ``check_wire_notes`` iterating the RAW notes): at
    ``at=2`` the order's ``_id`` became ``o_cargo`` after the scan and the commit carried
    acme's order under cargo's id.
    """

    def solve(tick, t, o):
        def effect():
            o["o_acme"]["_id"] = "o_cargo"
        return Result(pairs=(("t_acme", "o_acme"),), solver_notes={("t_acme", "o_acme"): _Effect({"x": 1}, effect, at)})

    with isolated_registry():
        _toy(solve)
        app, committed, published, violations = _run()
    # Either the effect ran (the order was modified) and the tick was refused, or it
    # never ran and the legal own pair was committed under its own id.
    if violations:
        assert committed == [] and published == []
    else:
        assert committed == [("t_acme", "o_acme")]
        assert [p["order"]["_id"] for p in published] == ["o_acme"]


@pytest.mark.parametrize("at", range(1, 8))
def test_n2_a_note_that_relabels_the_carrier_cannot_hide_a_shared_haul(at):
    """The review's ``toctou_shared_hide``: a declared cross pair whose truck is relabelled acme after the checks.

    TEETH: as above; at ``at=2`` the wire said ``shared=False`` for borax's truck on acme's order.
    """

    def solve(tick, t, o):
        def effect():
            t["t_borax"]["haulier_id"] = "acme"
            t["t_borax"]["profile"]["haulier_id"] = "acme"
        return Result(pairs=(("t_borax", "o_acme"),),
                      solver_notes={("t_borax", "o_acme"): _Effect({"x": 1}, effect, at)})

    with isolated_registry():
        _toy(solve, declare=DECLARED)
        app, committed, published, violations = _run()
    if violations:
        assert committed == [] and published == []
    else:
        assert committed == [("t_borax", "o_acme")]
        (p,) = published
        assert p["shared"] is True and p["carrier_haulier_id"] == "borax" and p["owner_haulier_id"] == "acme"


class _Lying(dict):
    """Clean for its first ``clean`` reads, then claims market provenance (the review's ``LyingNote``)."""

    def __init__(self, clean):
        super().__init__({"x": 1})
        self.n, self.clean = 0, clean

    def _cur(self):
        self.n += 1
        return {"x": 1} if self.n <= self.clean else {"pool_id": "forged-pool", "market_round": 99}

    def __iter__(self):
        return iter(self._cur())

    def keys(self):
        return self._cur().keys()

    def items(self):
        return self._cur().items()

    def values(self):
        return self._cur().values()

    def __getitem__(self, k):
        return self._cur()[k]

    def get(self, k, d=None):
        return self._cur().get(k, d)

    def __len__(self):
        return len(self._cur())

    def __contains__(self, k):
        return k in self._cur()


@pytest.mark.parametrize("clean", range(0, 6))
def test_n3_a_lying_note_never_forges_market_provenance(clean):
    """TEETH: ``render_share_tags`` handed the RAW ``result.solver_notes`` (``TickOutcome`` built from
    the solver's result, not the normalised copy): ``clean=3`` published ``pool_id='forged-pool'``."""

    def solve(tick, t, o):
        return Result(pairs=(("t_borax", "o_acme"),), solver_notes={("t_borax", "o_acme"): _Lying(clean)})

    with isolated_registry():
        _toy(solve, declare=DECLARED)
        app, committed, published, violations = _run()
    assert all(p.get("pool_id") != "forged-pool" and p.get("market_round") != 99 for p in published)
    if clean == 0:  # the forged keys were what the framework read: refused (undeclared wire fields)
        assert violations == 1 and committed == []
    else:  # read once, clean: the wire carries exactly what was read
        assert violations == 0 and [p.get("solver_notes") for p in published] == [{"x": 1}]


def test_n2_the_normalised_result_holds_only_exact_builtin_types():
    class S(str):
        pass

    class D(dict):
        pass

    class F(float):
        pass

    raw = Result(pairs=(("t", "o"),), solver_notes={("t", "o"): D({S("k"): (F(1.5), D(a=[1, S("x")]))})},
                 tick_notes=D(rounds=1))
    plain, problems = normalise_result(raw, strict=True)
    assert problems == []
    note = plain.solver_notes[("t", "o")]
    assert note == {"k": [1.5, {"a": [1, "x"]}]}

    def kinds(v):
        yield type(v)
        if isinstance(v, dict):
            for k, x in v.items():
                yield type(k)
                yield from kinds(x)
        elif isinstance(v, list):
            for x in v:
                yield from kinds(x)

    assert set(kinds(note)) <= {dict, list, str, int, float, bool, type(None)}
    assert type(plain.tick_notes["rounds"]) is int and plain is not raw


def test_n2_non_json_tick_notes_are_message_18():
    """TEETH: dropping the ``tick_note_problems`` from ``DispatchRunner.run``: the tick committed."""

    def solve(tick, t, o):
        return Result(pairs=(("t_cargo", "o_cargo"),), tick_notes={"rounds": {1, 2}})

    with isolated_registry():
        _toy(solve)
        _a, committed, _p, violations = _run()
    assert committed == [] and violations == 1


# --- N4: the run-stats push key -----------------------------------------------------------------


class _Redirecting:
    """A run-stats aggregator that rewrites its INSTANCE ``meta_key`` to the planner stamp's key."""

    meta_key = "probe_stats"

    def __init__(self):
        self.meta_key = "policies"
        self.n = 0

    def fold(self, tick_notes, clock):
        self.n += 1
        return True

    def push_due(self, *, force=False):
        return True

    def mark_pushed(self):
        pass

    def snapshot(self):
        return {"ticks": self.n}

    @classmethod
    def empty_snapshot(cls):
        return {"ticks": 0}


def test_n4_an_instance_meta_key_cannot_redirect_the_push():
    """TEETH: ``push(agg.meta_key, ...)`` in ``run_stats._push_if_due``: the snapshot went to ``meta.policies``."""
    pushes = []
    store: dict[str, Any] = {}
    run_stats.record_tick(store, _Redirecting, {}, None, lambda k, v: pushes.append(k) or True)
    run_stats.flush(store, lambda k, v: pushes.append(k) or True)
    assert pushes and set(pushes) == {"probe_stats"}


def test_n4_a_class_meta_key_changed_after_bind_writes_nothing(caplog):
    class Later(_Redirecting):
        meta_key = "probe_stats"

    pushes = []
    Later.meta_key = "policies"  # after bind (check_meta_key passed for "probe_stats")
    with caplog.at_level(logging.ERROR):
        assert run_stats.record_tick({}, Later, {}, None, lambda k, v: pushes.append(k) or True) is None
    assert pushes == [] and "RUN STATS REFUSED" in caplog.text


# --- N5: the run world ---------------------------------------------------------------------------


def _ids(value, out):
    out.add(id(value))
    if isinstance(value, dict):
        for v in value.values():
            _ids(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _ids(v, out)
    return out


def test_n5_the_live_profile_is_not_reachable_through_the_world_state():
    """TEETH: ``self.data = dict(data)`` in ``bind._WorldState``: the review's ``probe_widen`` appended a
    structure to the AGENT's profile on tick 1 and committed an undeclared pair on tick 2."""
    seen = {}

    def solve(tick, t, o):
        seen["state"] = bind_mod._WORLD_STATE[tick.world]
        return Result()

    with isolated_registry():
        _toy(solve)
        app = _app(_lprofile({}, top="Probe"))
        app.assign(SIM_CLOCK, 1)
    live = app._assignment_profile()
    state = seen["state"]
    live_ids = {i for k, v in live.items() if not isinstance(v, (str, int, float, bool, type(None)))
                for i in _ids(v, set())}
    reachable = set()
    for v in state.data.values():
        _ids(v, reachable)
    for v in state.copies.values():
        _ids(v, reachable)
    assert not (live_ids & reachable)
    with pytest.raises(TypeError):
        state.data["cooperation"] = {}


def test_n5_a_profile_that_widens_the_ceiling_mid_run_is_refused():
    """TEETH: ``run_ceiling=None`` in ``AssignmentApp._dispatch_runner`` (each rebuilt runner pinning
    its own ceiling): the widened profile bound and committed ``t_acme -> o_cargo``."""

    with isolated_registry():
        _toy(lambda tick, t, o: Result())
        app = _app(_lprofile({}, top="Probe"))
        app.assign(SIM_CLOCK, 1)
        app.behavior["profile"]["cooperation"]["structures"].append(
            {"id": "evil", "adjacency": {"acme": ["cargo"], "cargo": ["acme"]}})
        with pytest.raises(PolicyError, match="did not declare at run start"):
            app.assign(SIM_CLOCK, 2)


def test_n5_a_runner_rebind_cannot_widen_its_run_ceiling():
    with isolated_registry():
        _toy(lambda tick, t, o: Result())
        from tests.test_solver_contract_litmus import ORDERS, TRUCKS

        from apps.container_logistics.policies.dispatch.runner import DispatchRunner

        base = _lprofile({}, top="Probe")
        runner = DispatchRunner(bind_profile(base), profile=base)
        assert runner.run_ceiling == frozenset(DECLARED)
        wide = copy.deepcopy(base)
        wide["cooperation"]["structures"].append({"id": "evil", "adjacency": {"acme": ["cargo"], "cargo": ["acme"]}})
        with pytest.raises(PolicyError, match="did not declare at run start"):
            runner.tick(candidates=copy.deepcopy(TRUCKS), orders=copy.deepcopy(ORDERS), profile=wide,
                        clock=DispatchClock(SIM_CLOCK, 1), seed=1)
        narrow = copy.deepcopy(base)
        narrow["cooperation"] = _cooperation("solo")
        narrow["cooperation"]["structures"] = narrow["cooperation"]["structures"][1:]
        runner.tick(candidates=copy.deepcopy(TRUCKS), orders=copy.deepcopy(ORDERS), profile=narrow,
                    clock=DispatchClock(SIM_CLOCK, 2), seed=2)  # narrower: re-bound, fine


# --- N6: the verified-pair memo -------------------------------------------------------------------


def test_n6_the_memo_has_no_public_writer():
    """TEETH: restoring ``_TickShared.remember``: the review's ``remember_phys`` committed a
    physically infeasible own pair (20ft truck, 40ft order) with 0 violations."""

    def solve(tick, t, o):
        tick._shared.remember(t["t_acme"], o["o_acme"])
        return Result(pairs=(("t_acme", "o_acme"),))

    with isolated_registry():
        _toy(solve)
        app = _app(_lprofile({}, top="Probe"))
        for x in app.manager._trucks:
            if x["_id"] == "t_acme":
                x["profile"]["truck_size"] = "20ft"
        for x in app.manager._orders:
            if x["_id"] == "o_acme":
                x["profile"]["order_size"] = "40ft"
        random.seed(1)
        assert app.assign(SIM_CLOCK, 1) == []
        assert app._dispatch_runner(app._assignment_profile()).stats["violations"] == 1


def test_n6_verify_records_only_a_pair_its_own_check_passed():
    """Calling the memo's one writer directly records nothing the framework would not have."""

    def solve(tick, t, o):
        sub = tick.restrict(trucks=[t["t_acme"]], orders=[o["o_acme"]], scope="probe", sample_rng=None)
        problems, _plain = tick._shared.verify(sub, Result(pairs=(("t_acme", "o_acme"),)))
        assert problems == ["pair ('t_acme', 'o_acme') is not allowed"]
        assert not tick._shared.proved(t["t_acme"], o["o_acme"])
        return Result(pairs=(("t_acme", "o_acme"),))

    with isolated_registry():
        _toy(solve)
        app = _app(_lprofile({}, top="Probe"))
        for x in app.manager._trucks:
            if x["_id"] == "t_acme":
                x["profile"]["truck_size"] = "20ft"
        for x in app.manager._orders:
            if x["_id"] == "o_acme":
                x["profile"]["order_size"] = "40ft"
        random.seed(1)
        assert app.assign(SIM_CLOCK, 1) == []


# --- N9: refused world access --------------------------------------------------------------------


def test_n9_a_world_splat_in_solve_tick_is_message_18_not_a_crash():
    """TEETH: ``RunWorld._no_bulk`` returning a plain ``TypeError``: ``assign`` raised on every tick."""

    def solve(tick, t, o):
        dict(tick.world)
        return Result(pairs=(("t_cargo", "o_cargo"),))

    with isolated_registry():
        _toy(solve)
        _a, committed, _p, violations = _run()
    assert committed == [] and violations == 1
    assert issubclass(WorldAccessError, TypeError) and issubclass(WorldAccessError, DispatchContractViolation)


def test_n9_a_world_splat_in_bind_is_a_policy_error():
    with isolated_registry():
        @policy(slot="planner", name="Splat", version="1", description="probe")
        class Splat:
            Params = _P

            def __init__(self, params=None):
                self.params = params

            def bind(self, params, world, *, top):
                return Declaration(provenance=dict(world))

            def solve_tick(self, tick, params):
                return Result()

        with pytest.raises(WorldAccessError, match="RunWorld.keys"):
            bind_profile(_lprofile({}, top="Splat"))


# --- L1: a solver __init__ ---------------------------------------------------------------------------


def test_l1_a_raising_solver_init_is_a_policy_error_naming_the_solver():
    """TEETH: dropping ``_construct`` in ``profile.build_policy_tree``: a bare ``ZeroDivisionError``."""

    def boom():
        raise ZeroDivisionError("bad dial")

    with isolated_registry():
        _toy(lambda tick, t, o: Result(), name="Boom", init=boom)
        with pytest.raises(SolverInitError, match=r"planner policy Boom v1: constructing .*ZeroDivisionError: bad dial"):
            bind_profile(_lprofile({}, top="Boom"))
        from apps.container_logistics.policies.dispatch.profile import assert_assignment_profiles_resolvable

        with pytest.raises(PolicyError, match="assignment agent 'a1': planner policy Boom"):
            assert_assignment_profiles_resolvable({"a1": {"profile": _lprofile({}, top="Boom")}})
