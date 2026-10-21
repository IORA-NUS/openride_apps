"""The battery's structure verdicts come from ``invariants.check_plan``, not a copy of it.

What is locked: every message kind ``check_plan`` can produce maps (by its stable
wording) to a named structure check -- never the catch-all -- and the table has
exactly as many kinds as ``check_plan`` has message templates; ``Plan.from_pairs``'
"truck twice" violation maps to ``structure.no_truck_twice``; and a problem that
``check_plan`` reports is what the battery fails on, through the runner path (review
F5).

Why: the battery used to re-implement the invariants, so a new invariant added to
``check_plan`` would silently go unchecked by ``openride policy validate``. Now a new
message template breaks ``test_check_plan_has_exactly_the_classified_message_kinds``
until it is classified, and an unknown message still FAILS ``returns_plan``.

Verified to have teeth (each mutation restored after): adding a seventh
``problems.append(f"...")`` to ``check_plan`` (in a scratch copy of
``invariants.py``) failed ``test_check_plan_has_exactly_the_classified_message_kinds``;
deleting the ``no_order_twice`` row of ``_PLAN_MESSAGE_KINDS`` failed
``test_every_check_plan_message_kind_maps_to_its_structure_check``; replacing
``check_plan(recorder.plan, ctx)`` in ``battery._decide_via_runner`` with ``[]``
failed ``test_a_problem_check_plan_reports_fails_the_battery``.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from apps.container_logistics.policies.dispatch import invariants
from apps.container_logistics.policies.dispatch import runner as runner_module
from apps.container_logistics.policies.dispatch.invariants import check_plan
from apps.container_logistics.policies.dispatch.plan import Plan
from apps.container_logistics.policies.dispatch.runner import build_runner_for_policy
from apps.container_logistics.policies.framework.errors import DispatchContractViolation
from apps.container_logistics.policies.testing import battery
from apps.container_logistics.policies.testing import fixtures as fx
from apps.container_logistics.policies.testing.battery import (
    STRUCTURE_CHECKS,
    battery_check_ids,
    classify_contract_problem,
    run_battery,
)


def _ctx(make):
    return make().context(build_runner_for_policy("GreedyNearest"))


def _cases():
    one, infeasible = _ctx(fx.one_haulier), _ctx(fx.all_infeasible)
    return [
        ("not a Plan", "returns_plan", check_plan("not a plan", one)),
        ("truck not in scope", "only_in_scope", check_plan(Plan(chains={"ghost": ("o000",)}), one)),
        ("empty chain", "returns_plan", check_plan(Plan(chains={"t000": ()}), one)),
        ("order twice", "no_order_twice", check_plan(Plan(chains={"t000": ("o000",), "t001": ("o000",)}), one)),
        ("order not in scope", "only_in_scope", check_plan(Plan(chains={"t000": ("ghost",)}), one)),
        ("head not allowed", "only_allowed_pairs", check_plan(Plan(chains={"t000": ("o000",)}), infeasible)),
    ]


def _matching_row(message):
    rows = [i for i, (_c, pattern) in enumerate(battery._PLAN_MESSAGE_KINDS) if pattern.search(message)]
    assert len(rows) == 1, f"check_plan message {message!r} must match exactly one _PLAN_MESSAGE_KINDS row, got {rows}"
    return rows[0]


def test_every_check_plan_message_kind_maps_to_its_structure_check():
    hit = set()
    for label, want, messages in _cases():
        assert len(messages) == 1, f"{label}: the crafted plan should break exactly one invariant, got {messages}"
        hit.add(_matching_row(messages[0]))
        assert classify_contract_problem(messages[0]) == want, f"{label}: {messages[0]!r} must fail {want}"
    assert hit == set(range(len(battery._PLAN_MESSAGE_KINDS))), "every classified kind is exercised here"
    assert {c for c, _p in battery._PLAN_MESSAGE_KINDS} | {"no_truck_twice"} == set(STRUCTURE_CHECKS)


def test_check_plan_has_exactly_the_classified_message_kinds():
    tree = ast.parse(inspect.getsource(invariants.check_plan))
    templates = [n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)]
    assert len(templates) == len(battery._PLAN_MESSAGE_KINDS), (
        f"check_plan has {len(templates)} message templates but the battery classifies "
        f"{len(battery._PLAN_MESSAGE_KINDS)}: add the new kind to battery._PLAN_MESSAGE_KINDS and a case here"
    )


def test_from_pairs_truck_twice_maps_to_no_truck_twice():
    t, (o1, o2) = fx.one_haulier().trucks[0], fx.one_haulier().orders[:2]
    with pytest.raises(DispatchContractViolation) as exc:
        Plan.from_pairs([(t, o1), (t, o2)])
    assert classify_contract_problem(str(exc.value), raised=True) == "no_truck_twice"


def test_an_unknown_message_still_fails_returns_plan():
    assert classify_contract_problem("a future invariant nobody classified") == "returns_plan"
    assert classify_contract_problem("a policy-raised violation", raised=True) == "returns_plan"


def test_a_problem_check_plan_reports_fails_the_battery(monkeypatch):
    def stricter(plan, ctx):
        return check_plan(plan, ctx) + ["a future invariant: every plan is wrong"]

    # Both references, so the runner discards exactly what the battery reports.
    monkeypatch.setattr(battery, "check_plan", stricter)
    monkeypatch.setattr(runner_module, "check_plan", stricter)
    results = run_battery("dispatch", "RandomAssignment")
    assert [r.id for r in results] == battery_check_ids("dispatch", "RandomAssignment")
    failing = {r.id for r in results if not r.ok}
    assert failing == {"structure.returns_plan", "structure.returns_plan[candidates=spatial]"}, sorted(failing)
    for r in results:
        if not r.ok:
            assert "a future invariant: every plan is wrong" in r.detail, (
                f"{r.id} must report check_plan's own message, got {r.detail[:200]}"
            )
