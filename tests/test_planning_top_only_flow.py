"""``@policy(top_only=...)`` and the planning battery / golden flow, end to end on test-only toys.

The toys (``tests/planning_toy.py``) are registered only inside ``isolated_registry()``;
nothing here ships a solver. What this proves for a ``top_only`` planning top:

- registration and the catalog carry ``top_only``; it is never a ref target (R5: as an
  ``inner`` of either built-in composite, at resolution, at a CLI-style override merge),
  never listed as a leaf (``leaf_policies``, message 2's list), refused as the ``--solver``
  leaf through ``refuse_top_only`` (the helper ``scenario_overrides`` calls), and never
  one of a composite's battery ``inner`` arms;
- the battery decides it plan-then-dispatch, adds ``contract.planning_*`` and
  ``performance.plan`` (and only for planning arms), and every check PASSes or SKIPs;
  the planning checks FAIL for a planning policy that is not ``top_only`` or whose
  planning tick commits;
- the policy golden records it plan-then-dispatch (addendum M6) with non-empty heads.
"""

from __future__ import annotations

import pytest

from apps.container_logistics.policies.dispatch.bind import bind_profile
from apps.container_logistics.policies.framework import (
    PolicyParamsError,
    get_policy_spec,
    leaf_policies,
    merge_policy_overrides,
    refuse_top_only,
    resolve_policies_block,
)
from apps.container_logistics.policies.framework.catalog import policy_catalog
from apps.container_logistics.policies.framework.errors import PolicyRegistrationError
from apps.container_logistics.policies.framework.registry import (
    SOLVER_LEAF_ROLE,
    isolated_registry,
    policy,
    top_only_message,
)
from apps.container_logistics.policies.testing import battery
from apps.container_logistics.policies.testing import fixtures as fx
from tests.planning_toy import ToyDayPlan, register_toys


@pytest.fixture
def toys():
    with isolated_registry():
        register_toys()
        yield


def _profile(top: str, inner: str | None = None) -> dict:
    entry = {"type": top, "params": {} if inner is None else {"inner": {"type": inner}}}
    return {"policies_format": 2, "policies": {"planner": entry}}


# --- registration, catalog, leaves ----------------------------------------------------

def test_top_only_is_on_the_spec_and_the_catalog(toys):
    assert get_policy_spec("planner", "ToyDayPlan").top_only is True
    assert get_policy_spec("planner", "GreedyNearest").top_only is False
    entries = {p["name"]: p for p in policy_catalog()["planner"]["policies"]}
    assert entries["ToyDayPlan"]["top_only"] is True
    assert all(entries[n]["top_only"] is False for n in ("GreedyNearest", "RandomAssignment",
                                                         "Partitioned", "PooledMarket"))


def test_top_only_must_be_a_bool():
    class X(ToyDayPlan):
        pass

    with isolated_registry():
        with pytest.raises(PolicyRegistrationError, match="top_only must be True or False"):
            policy(slot="planner", name="X", version="1", description="x", top_only=1)(X)


def test_leaf_policies_exclude_top_only_and_composites(toys):
    names = [s.name for s in leaf_policies("planner")]
    assert "ToyDayPlan" not in names and "Partitioned" not in names
    assert {"GreedyNearest", "RandomAssignment", "ToyPlanNotTopOnly"} <= set(names)


# --- R5 -------------------------------------------------------------------------------

@pytest.mark.parametrize("parent", ["Partitioned", "PooledMarket"])
def test_r5_a_top_only_policy_is_never_an_inner(toys, parent):
    """Teeth: removing the ``target.top_only`` raise in ``components.ref_target`` resolved the tree."""
    with pytest.raises(PolicyParamsError) as err:
        bind_profile(_profile(parent, "ToyDayPlan"))
    assert str(err.value) == top_only_message("ToyDayPlan", "a nested node (profile.policies.planner.params.inner)")
    assert str(err.value).startswith("planner policy ToyDayPlan must be the top of the selection (top_only); "
                                     "it cannot be a nested node (")
    assert str(err.value).endswith("Select it with --policy planner=ToyDayPlan.")


def test_r5_through_a_cli_style_override_merge(toys):
    current = resolve_policies_block({"planner": {"type": "Partitioned"}})
    with pytest.raises(PolicyParamsError, match="ToyDayPlan must be the top of the selection"):
        merge_policy_overrides(current, {"planner": {"params": {"inner": "ToyDayPlan"}}})


def test_r5_helper_for_the_solver_leaf(toys):
    """``refuse_top_only`` is what ``scenario_overrides._checked_solver`` (WS-D) calls for ``--solver``."""
    refuse_top_only(get_policy_spec("planner", "GreedyNearest"))  # a leaf: no-op
    with pytest.raises(PolicyParamsError) as err:
        refuse_top_only(get_policy_spec("planner", "ToyDayPlan"))
    assert str(err.value) == (
        "planner policy ToyDayPlan must be the top of the selection (top_only); it cannot be "
        f"{SOLVER_LEAF_ROLE}. Select it with --policy planner=ToyDayPlan.")


def test_a_top_only_policy_binds_as_the_top(toys):
    tree = bind_profile(_profile("ToyDayPlan"))
    assert tree.top.name == "ToyDayPlan" and tree.declaration.planning is not None


def test_message_2_leaf_list_omits_top_only(toys):
    with pytest.raises(PolicyParamsError) as err:
        bind_profile(_profile("Partitioned", "PooledMarket"))
    assert "ToyDayPlan" not in str(err.value) and "GreedyNearest" in str(err.value)


# --- battery arms and checks ----------------------------------------------------------

def test_composite_battery_arms_never_offer_a_top_only_inner(toys):
    """Teeth: dropping ``spec.top_only`` from ``_ref_arm_targets`` added ``inner=ToyDayPlan``."""
    for parent in ("Partitioned", "PooledMarket"):
        labels = [battery.arm_label(a) for a in battery.policy_arms(get_policy_spec("planner", parent))]
        assert "inner=ToyDayPlan" not in labels and "inner=ToyBadPrefetch" not in labels
        assert "inner=GreedyNearest" in labels


def test_battery_check_ids_add_planning_checks_only_for_planning_arms(toys):
    ids = battery.battery_check_ids("planner", "ToyDayPlan")
    n_arm = len(battery.ARM_CHECKS)
    assert ids[:n_arm] == list(battery.ARM_CHECKS)
    assert ids[n_arm:n_arm + 3] == list(battery.PLANNING_ARM_CHECKS)
    assert ids[n_arm + 3:] == [c for c in battery.DISPATCH_CHECKS if c not in battery.ARM_CHECKS]
    assert battery.battery_check_ids("planner", "GreedyNearest")[:len(battery.DISPATCH_CHECKS)] == \
        list(battery.DISPATCH_CHECKS)
    assert not any(c.startswith(("contract.planning", "performance.plan"))
                   for c in battery.battery_check_ids("planner", "GreedyNearest"))


def test_battery_passes_a_top_only_planning_top_end_to_end(toys):
    """The whole battery on the toy: plan-then-dispatch on every fixture, no FAIL."""
    results = battery.run_battery("planner", "ToyDayPlan")
    assert [r.id for r in results] == battery.battery_check_ids("planner", "ToyDayPlan")
    failed = [(r.id, r.detail) for r in results if not r.ok]
    assert failed == []
    by_id = {r.id: r for r in results}
    assert by_id["contract.planning_declaration"].status == "PASS"
    assert "ORACLE" in by_id["contract.planning_declaration"].detail
    assert by_id["contract.planning_commits_nothing"].status == "PASS"
    assert by_id["performance.plan"].status == "PASS"
    # The dispatch tick after the plan really committed the planned heads somewhere.
    assert "0 pair(s)" not in by_id["robustness.one_haulier"].detail


def test_battery_fails_a_planning_policy_that_is_not_top_only(toys):
    by_id = {r.id: r for r in battery.run_battery("planner", "ToyPlanNotTopOnly")}
    assert by_id["contract.planning_declaration"].status == "FAIL"
    assert "not registered @policy(top_only=True)" in by_id["contract.planning_declaration"].detail


def test_battery_fails_a_planning_tick_that_commits(toys):
    by_id = {r.id: r for r in battery.run_battery("planner", "ToyPlanCommits")}
    check = by_id["contract.planning_commits_nothing"]
    assert check.status == "FAIL" and "a planning tick commits no pairs (got 1)" in check.detail


def test_battery_fails_a_malformed_prefetch_on_the_planning_tick(toys):
    by_id = {r.id: r for r in battery.run_battery("planner", "ToyBadPrefetch")}
    check = by_id["contract.planning_commits_nothing"]
    assert check.status == "FAIL" and "prefetch entry 7 is not an order id string" in check.detail


def test_performance_plan_skips_when_requirements_are_missing(toys):
    class Needy(ToyDayPlan):
        pass

    policy(slot="planner", name="NeedyPlanner", version="1", description="x", top_only=True,
           requires=("definitely_not_a_module_xyz",))(Needy)
    spec = get_policy_spec("planner", "NeedyPlanner")
    ok, detail = battery._performance_plan(spec, {}, battery.arm_planning(spec, {}))
    assert ok and detail.startswith("skipped: requires definitely_not_a_module_xyz")


def test_bad_ref_check_refuses_a_top_only_target(toys):
    ok, detail = battery._rejects_bad_ref(get_policy_spec("planner", "Partitioned"))
    assert ok and "inner=ToyDayPlan" in detail


# --- golden ---------------------------------------------------------------------------

def test_policy_golden_records_a_planning_top_plan_then_dispatch(toys):
    """Addendum M6. Teeth: deciding with ``decide_seeded`` alone gave empty heads on every fixture."""
    from tests.golden.capture_policy_golden import golden_cases

    cases = golden_cases("ToyDayPlan")
    assert [c["fixture"] for c in cases] == [m().id for m in fx.ROBUSTNESS_FIXTURES]
    assert any(c["heads"] for c in cases), "a vacuous golden would compare nothing"
    assert golden_cases("ToyDayPlan") == cases, "byte-stable"
    # A non-planning policy is decided exactly as before (one dispatch tick).
    from apps.container_logistics.policies.dispatch.runner import build_runner_for_policy

    greedy = golden_cases("GreedyNearest")
    _tick, result = battery.decide_seeded(build_runner_for_policy("GreedyNearest"), fx.one_haulier())
    assert next(c for c in greedy if c["fixture"] == "one_haulier")["heads"] == [list(p) for p in result.pairs]


def test_plan_then_decide_seeded_refuses_a_top_without_planning():
    from apps.container_logistics.policies.dispatch.runner import build_runner_for_policy
    from apps.container_logistics.policies.framework import PolicyError

    with pytest.raises(PolicyError, match="declares no planning"):
        battery.plan_then_decide_seeded(build_runner_for_policy("GreedyNearest"), fx.one_haulier())
