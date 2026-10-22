"""Round-2 C3 fixes asked for by the C1/C2 reviews and the explainer fact-check.

What is locked (each test names its finding):

- The pooled runtime instantiates market components only through the typed path;
  the fail-soft legacy getters are on no runtime path (C1 review #6). The framework
  imports nothing from ``policies.planner`` (C1 review #5).
- The catalog advertises a REGISTERED default and each composite's ``inner_param``,
  the one descent rule ``--solver`` follows (C1 review #4, #9).
- The whole tick's sample cap applies per haulier, drawing from ``tick.rng``
  (explainer gap 1); a top-level crash propagates out of ``assign`` (gap 2); a
  contract violation inside a market company is message 19 and drops only that
  company for that round (gap 3).
- ``check_mirrors`` refuses diverged accounting dials; a changed decomposition keeps
  the current solver on both override paths; a per-run override passes the run-start
  check with the accounting dial equal to the leaf's; message 13 never advises
  ``--solver <composite>``; legacy-profile errors name the legacy keys (C2 review
  findings 1-5).

Why: each of these is a place where the framework could silently run, account or
advertise something other than what was selected.
"""

from __future__ import annotations

import ast
import logging
import random
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

import pytest

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.plan import Plan
from apps.container_logistics.policies.dispatch.profile import (
    assert_assignment_profiles_resolvable,
    check_mirrors,
    resolve_dispatch_for_profile,
)
from apps.container_logistics.policies.dispatch.runner import build_runner_for_policy
from apps.container_logistics.policies.dispatch.tick import DispatchClock
from apps.container_logistics.policies.framework import PolicyError, PolicyParams, UnknownPolicyError, policy
from apps.container_logistics.policies.framework.catalog import policy_catalog
from apps.container_logistics.policies.framework.registry import get_policy_spec, isolated_registry, list_policies
from apps.container_logistics.policies.solver_lib import candidates
from apps.container_logistics.policies.solver_lib.candidates import CandidateSettings
from apps.container_logistics.policies.compat.legacy_env import apply_planner_topology_override
from apps.container_logistics.scenario.scenario_overrides import apply_policy_overrides
from tests.test_assignment_cooperation import _order, _truck
from tests.test_whole_tick_dispatch import _CLOCK, _app, _base_profile, _ids

APPS = Path(__file__).resolve().parents[1] / "apps" / "container_logistics"
_GETTERS = {"get_offer_policy", "get_claim_policy", "get_arbitration_rule"}


def _names_used(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    return names


def test_no_runtime_module_uses_the_fail_soft_market_getters():
    """C1 review #6. Verified to have teeth: re-adding ``from .policy.offer import
    get_offer_policy`` to ``assignment/app.py`` failed this test."""
    runtime = [APPS / "assignment" / "app.py", APPS / "assignment" / "pooled_planner.py",
               *sorted((APPS / "policies").rglob("*.py"))]
    offenders = {str(p.relative_to(APPS)): sorted(_names_used(p) & _GETTERS) for p in runtime}
    assert not {k: v for k, v in offenders.items() if v}, (
        "the fail-soft getters must not be on a live path; instantiate through "
        "framework.components.instantiate_component")


def test_framework_imports_nothing_from_the_dispatch_package():
    """C1 review #5. Verified to have teeth: re-adding the ``from
    apps.container_logistics.policies.dispatch import legacy`` line to ``framework/resolve.py``
    failed this test."""
    leaks = []
    for path in sorted((APPS / "policies" / "framework").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [module or ""]
            if any(n.startswith("apps.container_logistics.policies.dispatch") for n in names):
                leaks.append(f"{path.name}:{node.lineno}")
    assert not leaks, f"framework modules import the dispatch package: {leaks}"


def test_catalog_default_is_one_of_its_own_policies():
    """C1 review #4. Verified to have teeth: setting the dispatch slot default to an
    unregistered name made ``policy_catalog`` raise (``default_policy_name`` is strict)."""
    catalog = policy_catalog()
    for slot, entry in catalog.items():
        if "policies" in entry and entry.get("default") is not None:
            assert entry["default"] in {p["name"] for p in entry["policies"]}, slot
    assert catalog["planner"]["default"] == "Partitioned"


def test_catalog_exposes_inner_param_and_the_solver_override_descends_it():
    """C1 review #9: one descent rule (``PolicySpec.inner_param``) for the catalog and ``--solver``.

    Verified to have teeth: hardcoding ``"inner"`` in ``scenario_overrides.leaf_retargeted``
    is invisible here, but returning ``None`` from ``PolicySpec.inner_param`` failed both halves.
    """
    entries = {p["name"]: p for p in policy_catalog()["planner"]["policies"]}
    for spec in list_policies("planner"):
        assert entries[spec.name]["inner_param"] == spec.inner_param
    assert (entries["Partitioned"]["inner_param"], entries["PooledMarket"]["inner_param"]) == ("inner", "inner")
    assert entries["GreedyNearest"]["inner_param"] is None and entries["RandomAssignment"]["inner_param"] is None
    from apps.container_logistics.scenario.scenario_overrides import (
        current_policies_for_profile,
        run_dispatch_overrides,
    )

    coll = {"a0": {"profile": {"strategy": "RandomAssignment"}}}
    run_dispatch_overrides(coll, solver="GreedyNearest")  # C4: --solver is its own channel
    resolved, _source = current_policies_for_profile(coll["a0"]["profile"])
    assert resolved["planner"].label == "Partitioned/GreedyNearest", "--solver sets the innermost solver"


# --- explainer gap 1: the tick-scope sample cap -----------------------------------------


def test_tick_scope_samples_per_haulier_from_ctx_rng_and_partition_scope_is_unchanged():
    """Verified to have teeth: sampling the whole tick with one ``rng.sample(trucks, cap)``
    (``candidates.sampled_trucks`` in ``tick_sampled_trucks``' depth-0 branch) failed the
    per-haulier counts."""
    trucks = [_truck(f"t_{h}{i}", h, 103.85 + 0.001 * i, 1.30) for h in ("acme", "borax") for i in range(4)]
    orders = [_order("o_acme", "acme", 103.85, 1.30)]
    runner = build_runner_for_policy("RandomAssignment")
    profile = {"max_trucks_per_haulier": 2}
    settings = CandidateSettings.from_profile(profile)
    clock = DispatchClock(sim_clock=_CLOCK, time_step=1)
    tick = runner.tick(candidates=trucks, orders=orders, profile=profile, clock=clock, seed=123, dials={})
    random.seed(1)
    before = random.getstate()
    sample = candidates.tick_sampled_trucks(tick, settings)
    assert random.getstate() == before, "the whole tick must draw from tick.rng, not the module"
    assert [assign_constraints.haulier_of(t) for t in sample] == ["acme", "acme", "borax", "borax"]
    again = candidates.tick_sampled_trucks(
        runner.tick(candidates=trucks, orders=orders, profile=profile, clock=clock, seed=123, dials={}), settings)
    assert [t["_id"] for t in again] == [t["_id"] for t in sample], "reproducible for a tick seed"
    group = tick.restrict(trucks=trucks, orders=orders, scope="partition:component:0", sample_rng=random)
    random.seed(1)
    assert len(candidates.tick_sampled_trucks(group, settings)) == 2, "a group sub-tick still caps its whole slice"
    assert random.getstate() != before, "a group sub-tick still samples from the module (pre-v2)"


# --- explainer gap 2: a top-level crash propagates --------------------------------------


@pytest.fixture
def crashers():
    with isolated_registry():
        @policy(slot="planner", name="TopCrasher", version="1", information_model="central",
                description="raises")
        class TopCrasher(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def decide(self, ctx):
                raise KeyError("solver bug")

        yield


@pytest.mark.parametrize("dispatch", [{"type": "TopCrasher"},
                                      {"type": "Partitioned", "params": {"inner": {"type": "TopCrasher"}}}])
def test_a_crash_in_a_top_level_solver_or_a_group_propagates_out_of_assign(crashers, dispatch):
    """Explainer gap 2: a non-contract exception aborts the tick, as the partitioned path
    always did (only a MARKET company crash is contained, message 20).

    Verified to have teeth: widening ``DispatchRunner._solve``'s ``except`` to
    ``Exception`` swallowed both cases.
    """
    app = _app([_truck("t1", "acme", 103.85, 1.30)], [_order("o1", "acme", 103.85, 1.30)],
               _base_profile(policies={"planner": dispatch}))
    with pytest.raises(KeyError, match="solver bug"):
        app.assign(_CLOCK, 1)
    assert app._share_tags == {}


# --- explainer gap 3: a violation inside a market company ---------------------------------


@pytest.fixture
def borax_double_booker():
    with isolated_registry():
        @policy(slot="planner", name="BoraxDoubleBooker", version="1", information_model="central",
                description="double-books in borax's company scope")
        class BoraxDoubleBooker(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def decide(self, ctx):
                trucks = [str(t["_id"]) for t in ctx.raw_trucks]
                own = [str(o["_id"]) for o in ctx.raw_orders]
                if ctx.scope.key == "company:borax":
                    return Plan(chains={tid: (own[0],) for tid in trucks})
                return Plan(chains={tid: (oid,) for tid, oid in zip(trucks, own)})

        yield


def test_a_market_company_violation_is_message_19_and_drops_only_that_company(borax_double_booker, caplog):
    """Explainer gap 3. Verified to have teeth: letting ``Tick.call`` return the company's
    result without its ``check_result`` committed borax's double booking."""
    trucks = [_truck("t_acme", "acme", 103.85, 1.30), _truck("t_b1", "borax", 103.95, 1.30),
              _truck("t_b2", "borax", 103.951, 1.30)]
    orders = [_order("o_acme", "acme", 103.85, 1.30), _order("o_borax", "borax", 103.95, 1.30)]
    profile = _base_profile(policies={"planner": {"type": "PooledMarket", "params": {
        "inner": {"type": "BoraxDoubleBooker"}}}})
    app = _app(trucks, orders, profile)
    with caplog.at_level(logging.ERROR):
        assert _ids(app.assign(_CLOCK, 4)) == [("t_acme", "o_acme")]
    msgs = [r.getMessage() for r in caplog.records if "CONTRACT VIOLATION" in r.getMessage()]
    assert msgs and all(m.startswith("PLANNER POLICY CONTRACT VIOLATION: inner BoraxDoubleBooker v1 in "
                                     "scope company:borax at step 4") for m in msgs), msgs
    assert app._dispatch_runner(profile).stats["violations"] == len(msgs)


# --- C2 review findings 1-5 ---------------------------------------------------------------


def test_check_mirrors_refuses_diverged_accounting_dials():
    """C2 review finding 1. Verified to have teeth: removing ``_check_accounting_mirrors``
    from ``check_mirrors`` let both tampered profiles pass."""
    profile = {"strategy": "GreedyNearest", "solver_params": {"dual_cycle_bonus_km": 1.0},
               "planner": {"deployment": {"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 1.0}}}}
    node = resolve_dispatch_for_profile(profile)
    check_mirrors(profile, node)
    for where in ("solver_params", "planner.deployment.params"):
        tampered = deepcopy(profile)
        target = tampered["solver_params"] if where == "solver_params" else tampered["planner"]["deployment"]["params"]
        target["dual_cycle_bonus_km"] = 99.0
        with pytest.raises(PolicyError, match=rf"^profile {where} disagrees with its compiled policies.planner "
                                              r"\(Partitioned/GreedyNearest\): dual_cycle_bonus_km=99.0 vs 1.0"):
            check_mirrors(tampered, node)
    # A cost-blind leaf declares no dual-cycle dial: the accounting value is free.
    check_mirrors({"strategy": "RandomAssignment", "solver_params": {"dual_cycle_bonus_km": 7.0}},
                  resolve_dispatch_for_profile({"strategy": "RandomAssignment"}))


def test_changed_decomposition_keeps_the_solver_on_both_override_paths():
    """C2 review finding 2: ``--policy planner=PooledMarket`` and ``ORSIM_PLANNER_TOPOLOGY``
    are the same translation. Verified to have teeth: dropping ``_carried_inner`` made
    the ``--policy`` path run PooledMarket/RandomAssignment."""
    base = {"strategy": "GreedyNearest", "solver_params": {"dual_cycle_bonus_km": 2.0, "dual_cycle_radius_km": 0.5},
            "planner": {"topology": "partitioned"}}
    via_policy = {"a0": {"profile": deepcopy(base)}}
    apply_policy_overrides(via_policy, {"planner": {"type": "PooledMarket"}})
    via_env = {"a0": {"profile": deepcopy(base)}}
    apply_policy_overrides(via_env, {"planner": {"params": {}}})  # compiled block, as a bundle has
    apply_planner_topology_override(via_env, "pooled")
    for coll in (via_policy, via_env):
        prof = coll["a0"]["profile"]
        node = resolve_dispatch_for_profile(prof)
        assert node.label == "PooledMarket/GreedyNearest" and node.inner.params.dual_cycle_bonus_km == 2.0
        assert_assignment_profiles_resolvable(coll)
    assert via_policy["a0"]["profile"]["policies"] == via_env["a0"]["profile"]["policies"]


def test_per_run_override_passes_the_run_start_check_with_the_leafs_accounting_dial():
    """C2 review finding 3, end to end. Verified to have teeth: mirroring with the round-1
    ``strategy = merged.name`` (the top) in ``apply_policy_overrides`` failed the check."""
    coll = {"a0": {"profile": {"strategy": "GreedyNearest", "solver_params": {
        "dual_cycle_bonus_km": 5.0, "dual_cycle_radius_km": 0.5}, "planner": {"topology": "partitioned"}}}}
    apply_policy_overrides(coll, {"planner": {"params": {"inner": {"params": {"dual_cycle_bonus_km": 0.0}}}}})
    assert_assignment_profiles_resolvable(coll)
    prof = coll["a0"]["profile"]
    assert prof["strategy"] == "GreedyNearest"
    assert prof["solver_params"]["dual_cycle_bonus_km"] == resolve_dispatch_for_profile(prof).innermost.params.dual_cycle_bonus_km == 0.0


def test_strategy_mirror_message_never_advises_solver_with_a_composite():
    """C2 review finding 4. Verified to have teeth: always appending the ``--solver``
    hint advised ``--solver Partitioned``."""
    node = resolve_dispatch_for_profile({"strategy": "GreedyNearest"})
    with pytest.raises(PolicyError) as exc:
        check_mirrors({"strategy": "Partitioned"}, node)
    assert "--solver" not in str(exc.value) and "Fix the bundle or recompile." in str(exc.value)
    with pytest.raises(PolicyError, match="--solver RandomAssignment"):
        check_mirrors({"strategy": "RandomAssignment"}, node)


def test_legacy_profile_errors_name_the_legacy_keys():
    """C2 review finding 5. Verified to have teeth: dropping the ``_LEGACY_SOURCE_NOTE``
    re-raise in ``resolve_profile_policies`` failed every case."""
    for profile, error in (({"strategy": "GreedyNeerest"}, UnknownPolicyError),
                           ({"strategy": "Partitioned"}, PolicyError),
                           ({"strategy": "GreedyNearest", "planner": {"topology": "pooled", "market": {
                               "offer": {"type": "OfferSpare", "params": {"keep_below_km": "abc"}}}}}, PolicyError)):
        with pytest.raises(error) as exc:
            resolve_dispatch_for_profile(profile)
        assert "translated from the legacy keys strategy, solver_params" in str(exc.value), str(exc.value)


def test_partitioned_and_pooled_market_params_are_the_shapes_the_frontend_uses():
    """The FE is built on exactly these param shapes (round-2 §3.1, §5 C3)."""
    part = get_policy_spec("planner", "Partitioned").params_cls.json_schema()["properties"]
    pooled = get_policy_spec("planner", "PooledMarket").params_cls.json_schema()["properties"]
    assert list(part) == ["inner"]
    assert list(pooled) == ["max_rounds", "offer", "claim", "arbitration", "inner"]
    assert pooled["max_rounds"]["default"] == 20 and pooled["max_rounds"]["minimum"] == 1
    assert pooled["max_rounds"]["maximum"] == 100
    assert {k: pooled[k]["x-policy-ref"] for k in ("offer", "claim", "arbitration", "inner")} == {
        "offer": "market.offer", "claim": "market.claim", "arbitration": "market.arbitration", "inner": "planner"}
    assert [pooled[k]["x-leaf-only"] for k in ("offer", "claim", "arbitration", "inner")] == [False, False, False, True]
    assert part["inner"]["x-leaf-only"] is True and part["inner"]["default"]["type"] == "RandomAssignment"
    assert [pooled[k]["default"]["type"] for k in ("offer", "claim", "arbitration", "inner")] == [
        "OfferAll", "ClaimAllPlanned", "LowestCost", "RandomAssignment"]
