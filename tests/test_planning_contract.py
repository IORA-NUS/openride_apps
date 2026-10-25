"""F1 planning ticks and F3 prefetch, contract side (NetworkFlowDay build, workstream A).

Covers, for the framework (no solver names; the solvers are test-only toys from
``tests/planning_toy.py``, registered inside ``isolated_registry()``):

- ``Declaration.planning``: validated at bind (refusal R2), stored normalised and
  read-only; an inner declaring it is message 24 with ``planning`` in the field list and the
  static tail unchanged;
- ``Tick`` planning members (``kind`` / ``planning`` / ``engaged`` / ``lookup`` /
  ``run_id``): defaults leave an ordinary tick exactly as before, ``restrict`` inherits them,
  engaged / lookup documents are fingerprinted (a solver that rewrites one has its tick
  refused), malformed planning members are refused;
- the runner: a planning tick commits no pairs (R6), a ``"plan"`` tick for a top that
  declared no planning is refused;
- ``Result.prefetch`` + ``normalise_result`` (R7, ``PREFETCH_MAX``);
- ``policies_stamp`` gains ``planning`` (+ ``oracle``, ``axis``) only when declared;
- capture skips planning ticks (and does not count them).

Teeth (mutate / see it fail / restore), recorded per test below.
"""

from __future__ import annotations

import copy
import logging

import pytest

from apps.container_logistics.policies.dispatch import capture as capture_mod
from apps.container_logistics.policies.dispatch.bind import (
    PLANNING_KEYS,
    Declaration,
    bind_profile,
    normalise_planning,
)
from apps.container_logistics.policies.dispatch.invariants import normalise_result
from apps.container_logistics.policies.dispatch.profile import resolve_profile_policies
from apps.container_logistics.policies.dispatch.result import PREFETCH_MAX, Result
from apps.container_logistics.policies.dispatch.runner import build_runner_for_policy
from apps.container_logistics.policies.dispatch.tick import DispatchClock, PlanningWindow
from apps.container_logistics.policies.framework import PolicyError, policies_stamp
from apps.container_logistics.policies.framework.registry import isolated_registry
from apps.container_logistics.policies.testing import fixtures as fx
from tests.planning_toy import PLANNING, register_toys

_R2_TAIL = ("planning is {at_day_start: bool, every_steps: int >= 1 or null, foresight: 'none' | 'day', "
            "max_hold_seconds: int 1..600}.")


@pytest.fixture
def toys():
    with isolated_registry():
        register_toys()
        yield


def _window(**kw):
    base = dict(reason="day_start", day=0, start_step=0, end_step=360, now_step=0, step_seconds=240)
    base.update(kw)
    return PlanningWindow(**base)


def _profile(top: str, inner: str | None = None) -> dict:
    entry = {"type": top, "params": {} if inner is None else {"inner": {"type": inner}}}
    return {"policies_format": 2, "policies": {"planner": entry}}


# --- Declaration.planning (R2, message 24) ---------------------------------------------

def test_planning_declaration_is_normalised_and_read_only(toys):
    """Teeth: dropping the ``dataclasses.replace(... normalise_planning ...)`` in ``bind_tree``
    left the declared (partial) dict and failed the key-set assertion."""
    from apps.container_logistics.policies.framework import policy
    from tests.planning_toy import ToyDayPlan

    class Partial(ToyDayPlan):
        planning = {"every_steps": 18}

    policy(slot="planner", name="PartialPlanner", version="1", description="x", top_only=True)(Partial)
    tree = bind_profile(_profile("PartialPlanner"))
    planning = tree.declaration.planning
    assert tuple(planning) == PLANNING_KEYS
    assert dict(planning) == {"at_day_start": False, "every_steps": 18, "foresight": "none",
                              "max_hold_seconds": 30}
    assert dict(bind_profile(_profile("ToyDayPlan")).declaration.planning) == PLANNING
    with pytest.raises(TypeError):
        planning["foresight"] = "none"  # type: ignore[index]
    partial = normalise_planning({"every_steps": 12}, "w")
    assert dict(partial) == {"at_day_start": False, "every_steps": 12, "foresight": "none", "max_hold_seconds": 30}
    assert normalise_planning(None, "w") is None


def test_declaration_planning_defaults_to_none_and_copies_the_mapping():
    src = {"at_day_start": True}
    decl = Declaration(planning=src)
    src["at_day_start"] = False
    assert decl.planning["at_day_start"] is True, "the declaration holds its own copy"
    assert Declaration().planning is None
    assert Declaration(planning={"at_day_start": True}) == Declaration(planning={"at_day_start": True})


@pytest.mark.parametrize("value, problem", [
    ([1, 2], "it is not a mapping"),
    ({"at_day_start": True, "nope": 1}, "unknown key(s) 'nope'"),
    ({"at_day_start": 1}, "at_day_start must be true or false (got 1)"),
    ({"every_steps": 0}, "every_steps must be an integer >= 1 or null (got 0)"),
    ({"every_steps": True}, "every_steps must be an integer >= 1 or null (got True)"),
    ({"every_steps": 1.5}, "every_steps must be an integer >= 1 or null (got 1.5)"),
    ({"at_day_start": True, "foresight": "week"}, "foresight must be 'none' or 'day' (got 'week')"),
    ({"at_day_start": True, "max_hold_seconds": 601}, "max_hold_seconds must be an integer in 1..600 (got 601)"),
    ({"at_day_start": True, "max_hold_seconds": 0}, "max_hold_seconds must be an integer in 1..600 (got 0)"),
    ({}, "it never plans (at_day_start is false and every_steps is null)"),
    ({"at_day_start": False, "every_steps": None}, "it never plans (at_day_start is false and every_steps is null)"),
])
def test_r2_refuses_a_malformed_planning_declaration_with_the_pinned_text(value, problem):
    """R2, exact text. Teeth: making ``_planning_problem`` answer ``None`` bound every case."""
    from dataclasses import dataclass

    from apps.container_logistics.policies.framework import PolicyParams, policy

    class BadPlanner:
        @dataclass(frozen=True)
        class Params(PolicyParams):
            pass

        def __init__(self, params=None):
            pass

        def bind(self, params, world, *, top):
            return Declaration(planning=value)

        def solve_tick(self, tick, params):
            return Result()

    with isolated_registry():
        policy(slot="planner", name="BadPlanner", version="1", description="x", top_only=True)(BadPlanner)
        shown = repr(dict(value)) if isinstance(value, dict) else repr(value)
        want = f"planner policy BadPlanner at planner: bind() declared planning {shown}: {problem}. {_R2_TAIL}"
        with pytest.raises(PolicyError) as err:
            bind_profile(_profile("BadPlanner"))
        assert str(err.value) == want


def test_message_24_lists_planning_for_an_inner_and_keeps_the_static_tail(toys):
    """Teeth: removing the ``extras.append("planning")`` line bound the tree without refusal."""
    with pytest.raises(PolicyError) as err:
        bind_profile(_profile("Partitioned", "ToyPlanNotTopOnly"))
    assert str(err.value) == (
        "planner policy ToyPlanNotTopOnly at planner.inner: a nested node's bind() declared the top-only "
        "field(s) planning. Only the top node of the selection declares cross-company pairs (an inner may "
        "declare a subset), stamp settings, run-name segments and run stats."
    )


# --- Tick members ----------------------------------------------------------------------

def test_an_ordinary_tick_has_the_default_planning_members():
    runner = build_runner_for_policy("GreedyNearest")
    tick = fx.one_haulier().context(runner)
    assert (tick.kind, tick.planning, dict(tick.engaged), dict(tick.lookup), tick.run_id) == \
        ("dispatch", None, {}, {}, "")
    sub = tick.restrict(trucks=tick.raw_trucks[:1], orders=tick.raw_orders[:1], scope="s", sample_rng=None)
    assert (sub.kind, sub.planning, sub.run_id) == ("dispatch", None, "")


def test_plan_tick_members_are_read_only_and_inherited_by_restrict(toys):
    """The members live on the decision's shared framework state, so every sub-tick reads the top's."""
    runner = build_runner_for_policy("ToyDayPlan")
    f = fx.one_haulier()
    engaged = {"t_busy": {"_id": "o_busy", "state": "in_transit"}}
    lookup = {"o000": dict(f.orders[0])}
    tick = runner.tick(candidates=list(f.trucks), orders=list(f.orders), profile=f.profile(),
                       clock=DispatchClock(sim_clock=fx.SIM_CLOCK, time_step=0), seed=f.tick_seed,
                       kind="plan", planning=_window(), engaged=engaged, lookup=lookup, run_id="run_x")
    assert tick.kind == "plan" and tick.planning == _window() and tick.run_id == "run_x"
    assert dict(tick.engaged) == engaged and dict(tick.lookup) == lookup
    with pytest.raises(TypeError):
        tick.lookup["o9"] = {}  # type: ignore[index]
    with pytest.raises(Exception):
        tick.kind = "dispatch"  # type: ignore[misc]
    sub = tick.restrict(trucks=tick.raw_trucks, orders=tick.raw_orders, scope="all", sample_rng=None)
    assert (sub.kind, sub.planning, sub.run_id) == ("plan", _window(), "run_x")
    assert sub.engaged is tick.engaged and sub.lookup is tick.lookup


@pytest.mark.parametrize("kw", [
    {"kind": "plan"},                                   # a plan tick without a window
    {"planning": "WINDOW"},                             # a window on a dispatch tick
    {"kind": "nope"},
    {"kind": "plan", "planning": {"day": 0}},           # not a PlanningWindow
    {"run_id": 5},
    {"lookup": [1, 2]},
    {"engaged": {"t1": "not a document"}},
])
def test_malformed_planning_members_are_refused(toys, kw):
    runner = build_runner_for_policy("ToyDayPlan")
    f = fx.one_haulier()
    if kw.get("planning") == "WINDOW":
        kw = {"planning": _window()}
    with pytest.raises(ValueError):
        runner.tick(candidates=list(f.trucks), orders=list(f.orders), profile=f.profile(),
                    clock=DispatchClock(sim_clock=fx.SIM_CLOCK, time_step=0), seed=1, **kw)


def test_planning_window_validates_its_fields():
    assert _window(reason="interval").reason == "interval"
    for bad in ({"reason": "noon"}, {"day": 1.0}, {"step_seconds": 0}, {"start_step": 10, "end_step": 5}):
        with pytest.raises(ValueError):
            _window(**bad)


def test_a_plan_tick_for_a_top_without_planning_is_refused():
    runner = build_runner_for_policy("GreedyNearest")
    with pytest.raises(PolicyError, match="declared no planning"):
        fx.one_haulier().plan_context(runner)


def test_a_rewritten_lookup_document_discards_the_tick(toys, caplog):
    """Critique minor 6: lookup / engaged documents are fingerprinted like the tick's own.

    Teeth: dropping ``self._aux`` from ``_TickShared.changed`` committed the pairs.
    """
    from apps.container_logistics.policies.dispatch.runner import DispatchRunner

    runner = build_runner_for_policy("ToyDayPlan")
    f = fx.one_haulier()
    with f.module_random():
        runner.run(f.plan_context(runner, foresight="day"))
    lookup = {str(o["_id"]): copy.deepcopy(o) for o in f.orders}
    tick = f.context(runner, lookup=lookup)
    assert runner.run(tick).pairs, "the untouched lookup commits"

    original = runner.tree.top.solver.solve_tick

    def tampering(tick, params):
        tick.lookup["o000"]["state"] = "cancelled"
        return original(tick, params)

    runner.tree.top.solver.solve_tick = tampering
    try:
        with caplog.at_level(logging.ERROR):
            out = runner.run(f.context(runner, lookup={str(o["_id"]): copy.deepcopy(o) for o in f.orders}))
    finally:
        del runner.tree.top.solver.solve_tick
    assert out.pairs == [] and out.result is None
    assert any("'o000' were modified during the solve" in r.getMessage() for r in caplog.records)
    assert isinstance(runner, DispatchRunner)


# --- R6 -------------------------------------------------------------------------------

def test_r6_a_planning_tick_that_returns_pairs_is_discarded(toys, caplog):
    """R6 (message 18). Teeth: removing the ``shared.kind == "plan"`` branch committed the pair."""
    runner = build_runner_for_policy("ToyPlanCommits")
    f = fx.one_haulier()
    with caplog.at_level(logging.ERROR):
        out = runner.run(f.plan_context(runner, foresight="day"))
    assert out.pairs == [] and out.result is None and runner.stats["violations"] == 1
    assert any("a planning tick commits no pairs (got 1)" in r.getMessage() for r in caplog.records)


def test_a_planning_tick_commits_nothing_and_the_dispatch_tick_follows_the_plan(toys):
    """The whole F1/F3 contract path on one instance: plan (prefetch) -> lookup -> dispatch."""
    runner = build_runner_for_policy("ToyDayPlan")
    f = fx.one_haulier()
    with f.module_random():
        plan = runner.run(f.plan_context(runner, foresight="day"))
    assert plan.pairs == [] and plan.result is not None
    heads = plan.result.prefetch
    assert heads and all(type(h) is str for h in heads)
    assert plan.result.tick_notes == {"planned_trucks": len(heads)}
    lookup = {str(o["_id"]): o for o in f.orders if str(o["_id"]) in heads}
    out = runner.run(f.context(runner, lookup=lookup))
    assert sorted(o for _t, o in ((str(t["_id"]), str(o["_id"])) for t, o in out.pairs)) == sorted(heads)
    # A head another truck took meanwhile is skipped (read through tick.lookup).
    taken = {**lookup, heads[0]: {**lookup[heads[0]], "state": "assigned"}}
    again = runner.run(f.context(runner, lookup=taken))
    assert heads[0] not in {str(o["_id"]) for _t, o in again.pairs}


# --- F3 prefetch (R7) -----------------------------------------------------------------

def test_result_prefetch_defaults_and_forms():
    assert Result().prefetch == ()
    assert Result(prefetch=["a", "b"]).prefetch == ("a", "b")
    assert Result(prefetch={"b", "a"}).prefetch == ("a", "b"), "a set is ordered, never hash-ordered"
    assert Result(prefetch="ab").prefetch == "ab", "a bare string is kept for normalise to refuse"
    assert Result.from_pairs([], prefetch=iter(["x"])).prefetch == ("x",)
    assert Result(prefetch=None).prefetch == ()


def test_normalise_copies_prefetch_dedupes_and_reports_r7_only_when_strict():
    """R7 texts. Teeth: dropping ``prefetch=prefetch`` from normalise's Result lost the ids."""
    plain, problems = normalise_result(Result(prefetch=("o1", "o2", "o1")), strict=True)
    assert plain.prefetch == ("o1", "o2") and problems == []

    class Sub(str):
        pass

    plain, problems = normalise_result(Result(prefetch=("o1", 7, Sub("o2"), "")), strict=True)
    assert plain.prefetch == ("o1",)
    assert problems == ["prefetch entry 7 is not an order id string",
                        "prefetch entry 'o2' is not an order id string",
                        "prefetch entry '' is not an order id string"]
    plain, problems = normalise_result(Result(prefetch="o1"), strict=True)
    assert plain.prefetch == () and problems == ["prefetch 'o1' is not a sequence of order id strings"]
    many = tuple(f"o{i}" for i in range(PREFETCH_MAX + 1))
    _plain, problems = normalise_result(Result(prefetch=many), strict=True)
    assert problems == [f"prefetch names {PREFETCH_MAX + 1} ids, more than the cap {PREFETCH_MAX}"]
    assert normalise_result(Result(prefetch=many[:PREFETCH_MAX]), strict=True)[1] == []
    # Not strict (a sub-tick): the clean subset is kept, nothing is reported.
    plain, problems = normalise_result(Result(prefetch=("o1", 7)))
    assert plain.prefetch == ("o1",) and problems == []
    assert PREFETCH_MAX == 4000


def test_r7_a_malformed_prefetch_discards_the_tick(toys, caplog):
    runner = build_runner_for_policy("ToyBadPrefetch")
    f = fx.one_haulier()
    with caplog.at_level(logging.ERROR):
        out = runner.run(f.plan_context(runner))
    assert out.result is None and runner.stats["violations"] == 1
    assert any("prefetch entry 7 is not an order id string" in r.getMessage() for r in caplog.records)


def test_a_default_tick_result_carries_an_empty_prefetch():
    runner = build_runner_for_policy("GreedyNearest")
    out = runner.run(fx.one_haulier().context(runner))
    assert out.pairs and out.result.prefetch == ()


# --- stamp ----------------------------------------------------------------------------

def test_policies_stamp_has_planning_only_when_declared(toys):
    """Teeth: stamping ``planning`` unconditionally failed the online stamps (no declaration to stamp)."""
    def stamp(profile):
        resolved, source = resolve_profile_policies(profile)
        return policies_stamp(resolved, tree=bind_profile(profile), accounting_params={}, source=source)["planner"]

    assert "planning" not in stamp(_profile("GreedyNearest"))
    assert "planning" not in stamp(_profile("Partitioned", "GreedyNearest"))
    got = stamp(_profile("ToyDayPlan"))["planning"]
    assert got == {**PLANNING, "oracle": True, "axis": "elapsed"}


# --- capture --------------------------------------------------------------------------

def test_capture_skips_planning_ticks_and_counts_only_dispatch_ticks(toys, tmp_path):
    """Teeth: removing the ``kind == "plan"`` guard from ``before_solve`` wrote step_0 from the plan tick."""
    settings = capture_mod.CaptureSettings(dir=str(tmp_path), every_n_ticks=1, max_files=10, run_id="r")
    runner = build_runner_for_policy("ToyDayPlan")
    recorder = capture_mod.CaptureRecorder(settings, resolved=runner.tree.resolved)
    runner._capture = recorder
    f = fx.one_haulier()
    with f.module_random():
        runner.run(f.plan_context(runner, foresight="day"))
    assert recorder.ticks_seen == 0 and not list(tmp_path.iterdir())
    runner.run(f.context(runner))
    assert recorder.ticks_seen == 1 and [p.name for p in tmp_path.iterdir()] == ["step_0.json.gz"]
