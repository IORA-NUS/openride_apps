"""Final review fixes (battery): composite arm budgets, SKIP status, scoped module_rng, names.

What is locked (``docs/plans/policy_slots_round2_whole_tick.md``, "Final review fixes
(battery)"):

- MAJOR-3: an UNMEASURED composite ``inner=X`` arm is held to X's own declared budget
  plus the composite's overhead (its default-arm budget), in ``battery.arm_budget``;
  a measured ``arm_budgets_ms`` key still wins. So a correct, slow-by-nature leaf with
  a large declared budget (the review's NetworkFlow sketch) keeps the ``Partitioned``
  and ``PooledMarket`` batteries green, and a leaf slower than even its own budget
  still fails them (and its own).
- MINOR-1: a check exempted by a declaration is reported ``SKIP`` (``CheckResult.status``,
  ``skipped``; ``ok`` stays true, the ids are unchanged), in the control payload and the
  CLI table, and the CLI exits 0. A composite's ``inner=X`` arm says where X itself is
  checked. ``determinism.no_module_rng`` under a ``module_rng`` declaration is SCOPED:
  the declaring node's own draws and the scope samples it routes to the module are
  exempt; a node that declared nothing still FAILS (``Partitioned[inner=X]`` with an X
  that draws from the module).
- MINOR-7: the spec-exact messages list names built from the registry
  (``tests/policy_registry_names.py``), and the tests that pin them, and the contract
  battery's expected arms, stay green with an extra leaf registered.
- Selection follow-up: component names obey the policy-name grammar, both when a kind
  is declared and when a name is resolved.

Each test was mutation-checked; see the plan's subsection for the list.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

import pytest

from apps.container_logistics.policies.dispatch import scopes
from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.composite import CompositePolicy
from apps.container_logistics.policies.dispatch import compose
from apps.container_logistics.policies.dispatch.plan import Plan
from apps.container_logistics.policies.framework import PolicyParams, PolicyRef, param, policy, ref
from apps.container_logistics.policies.framework import components
from apps.container_logistics.policies.framework import params as params_mod
from apps.container_logistics.policies.framework.errors import PolicyRegistrationError
from apps.container_logistics.policies.framework.registry import get_policy_spec, isolated_registry
from apps.container_logistics.policies.testing import battery
from apps.container_logistics.policies.testing.battery import CheckResult, arm_budget, run_battery
from openride import cli, control
from openride_control import policies as control_policies
from tests.policy_registry_names import component_names, dispatch_names, leaf_names

_SLEEP_S = 0.75  # a decide on the 1000-truck scale tick takes at least this long


def _register_slow_leaf(name: str, budget_ms: int):
    """A correct central leaf that is slow ONLY on the scale tick, declaring ``budget_ms``."""

    @policy(slot="dispatch", name=name, version="1", description="slow by nature (a NetworkFlow stand-in)",
            information_model="central", perf_budget_ms=budget_ms)
    class SlowLeaf(DispatchPolicy):
        @dataclass(frozen=True)
        class Params(PolicyParams):
            pass

        def decide(self, ctx):
            if len(ctx.raw_trucks) >= 1000:
                time.sleep(_SLEEP_S)
            used_t, used_o, chains = set(), set(), {}
            pairs = sorted(ctx.candidate_pairs(), key=lambda p: (str(p[0]["_id"]), str(p[1]["_id"])))
            for t, o in pairs:
                tid, oid = str(t["_id"]), str(o["_id"])
                if tid not in used_t and oid not in used_o:
                    used_t.add(tid)
                    used_o.add(oid)
                    chains[tid] = (oid,)
            return Plan(chains=chains)

    return SlowLeaf


def _by_id(results):
    return {r.id: r for r in results}


# --- MAJOR-3: composite inner=X budgets -----------------------------------------------

def test_an_unmeasured_inner_arm_is_held_to_the_inner_budget_plus_the_composite_overhead():
    """Verified to have teeth: no derivation (the pre-fix inheritance) failed the
    derived-value asserts; ignoring the measured key made ``inner=GreedyNearest`` derived
    (and failed ``test_policy_arm_budgets``' gating test)."""
    with isolated_registry():
        _register_slow_leaf("SlowFlow", 4000)
        for top in ("Partitioned", "PooledMarket"):
            spec = get_policy_spec("dispatch", top)
            budget, source = arm_budget(spec, {"inner": {"type": "SlowFlow"}})
            assert budget == 4000 + spec.perf_budget_ms, (top, budget, source)
            assert "arm not measured" in source and "derived" in source and "SlowFlow" in source, source
            # A measured key always wins over the derivation.
            assert arm_budget(spec, {"inner": {"type": "GreedyNearest"}}) == (
                spec.arm_budgets_ms["inner=GreedyNearest"], "arm_budgets_ms['inner=GreedyNearest']")
        # Component and str arms still inherit the default arm's budget.
        pooled = get_policy_spec("dispatch", "PooledMarket")
        assert arm_budget(pooled, {"max_rounds": 2})[0] == pooled.perf_budget_ms


def test_a_slow_leaf_with_a_declared_budget_keeps_both_composites_green():
    """The review's MAJOR-3 repro, with a sleep stand-in for the NetworkFlow sketch.

    Before the fix both ``performance.scale[inner=SlowFlow]`` failed under the
    composite's 660 / 500 ms. Verified to have teeth: the pre-fix inheritance in
    ``arm_budget`` (no derivation) failed it on ``performance.scale[inner=SlowFlow]``.
    """
    with isolated_registry():
        _register_slow_leaf("SlowFlow", 4000)
        own = run_battery("dispatch", "SlowFlow")
        assert [r.id for r in own if not r.ok] == []
        for top in ("Partitioned", "PooledMarket"):
            results = run_battery("dispatch", top)
            failed = [r.id for r in results if not r.ok]
            assert failed == [], (top, failed, _by_id(results).get("performance.scale[inner=SlowFlow]"))
            perf = _by_id(results)["performance.scale[inner=SlowFlow]"]
            assert perf.status == "PASS" and "derived" in perf.detail, perf.detail


def test_a_leaf_slower_than_its_own_budget_still_fails_the_composites():
    """20 ms declared, >= 750 ms taken: fails its own budget AND both composite arms (680 / 520 ms).

    Verified to have teeth: an ``arm_budget`` that returned the inner's budget times 10
    let both composite arms pass.
    """
    with isolated_registry():
        _register_slow_leaf("TooSlowFlow", 20)
        ok, detail = battery._performance_scale(get_policy_spec("dispatch", "TooSlowFlow"), {})
        assert not ok and "budget 20 ms" in detail, detail
        for top in ("Partitioned", "PooledMarket"):
            spec = get_policy_spec("dispatch", top)
            ok, detail = battery._performance_scale(spec, {"inner": {"type": "TooSlowFlow"}})
            assert not ok, (top, detail)
            assert f"budget {20 + spec.perf_budget_ms} ms" in detail, detail


# --- MINOR-1: SKIP status and the scoped module_rng ------------------------------------

def test_check_result_status_names_skip_pass_and_fail():
    assert CheckResult("g", "c", True, "d", 1.0).status == "PASS"
    assert CheckResult("g", "c", False, "d", 1.0).status == "FAIL"
    skipped = CheckResult("g", "c", True, "skipped: x", 1.0, skipped=True)
    assert skipped.status == "SKIP" and skipped.ok and skipped.id == "g.c"


def _module_random_leaf(name: str = "ModuleRandom"):
    @policy(slot="dispatch", name=name, version="1", description="draws from the random module",
            information_model="central")
    class ModuleRandom(DispatchPolicy):
        @dataclass(frozen=True)
        class Params(PolicyParams):
            pass

        def decide(self, ctx):
            random.random()
            return Plan(chains={})

    return ModuleRandom


def test_partitioned_reports_skip_and_its_inner_arms_are_really_checked():
    """``Partitioned``'s own sample draw is SKIP; an inner that draws from the module FAILS.

    Before the fix every ``no_module_rng`` under Partitioned was "PASS (skipped)",
    so ``Partitioned[inner=ModuleRandom]`` passed. Verified to have teeth, each failed
    this test: dropping the ``skipped`` flag in ``_timed`` (the default arm read PASS);
    treating every draw as covered by the declaration (the ModuleRandom arm SKIP, not
    FAIL); not redirecting the inner's own RNG in ``_probe_module_draws`` (the default
    arm FAILED on RandomAssignment's sanctioned ``self.rng`` draws); returning the
    ``order_sensitive`` skip as a plain tuple (PASS).
    """
    with isolated_registry():
        _module_random_leaf()
        results = _by_id(run_battery("dispatch", "Partitioned"))
    default = results["determinism.no_module_rng"]
    assert default.status == "SKIP" and default.ok, default.detail
    assert "module_rng declared by Partitioned" in default.detail and "scope cap sample" in default.detail
    greedy = results["determinism.no_module_rng[inner=GreedyNearest]"]
    assert greedy.status == "PASS", greedy.detail
    assert "GreedyNearest itself is checked in GreedyNearest's own battery" in greedy.detail
    rogue = results["determinism.no_module_rng[inner=ModuleRandom]"]
    assert rogue.status == "FAIL" and "ModuleRandom on" in rogue.detail, rogue.detail
    order = results["determinism.order_independent[inner=GreedyNearest]"]
    assert order.status == "SKIP" and "checked in GreedyNearest's own battery" in order.detail, order.detail
    # Every other check of the built-in's own arms still passes.
    assert [cid for cid, r in results.items() if not r.ok] == ["determinism.no_module_rng[inner=ModuleRandom]"]


def _groups_composite(name: str, *, declare: bool, own_draw: bool):
    """A per-group composite that injects ``ctx.rng`` into its inner and samples from it.

    ``own_draw``: its own code draws from the random module once per tick (declared
    when ``declare``).
    """
    extra = {"module_rng": "draws one module number per tick on purpose"} if declare else {}

    @policy(slot="dispatch", name=name, version="1", description="groups via the shipped helpers",
            information_model="per_group", **extra)
    class Groups(CompositePolicy):
        @dataclass(frozen=True)
        class Params(PolicyParams):
            inner: PolicyRef = param(ref("GreedyNearest"), kind="dispatch", leaf_only=True, help="Inner.")

        def decide(self, ctx):
            if own_draw:
                random.random()
            self.inner.set_rng(ctx.rng)
            chains = {}
            for group in compose.cooperation_groups(ctx):
                sub = scopes.partition_context(
                    policy=self.inner, params=self.inner_node.params,
                    candidate_kind=self.inner_node.candidate_kind, trucks=list(group.trucks),
                    orders=list(group.orders), partition_key=group.key, rule=ctx.world.rule,
                    settings=ctx.candidate_settings, accounting_cost=ctx.accounting_cost,
                    clock=ctx.clock, sample_rng=ctx.rng, parent=ctx)
                plan = ctx.execute(compose.decide_in_scope, self.inner, self.inner_node, sub)
                if plan is not None:
                    chains.update(plan.chains)
            return Plan(chains=chains)

    return Groups


def test_a_declaration_covers_the_declaring_nodes_own_draws_only():
    """A composite declaring ``module_rng`` for its own draw is SKIP with a clean inner and
    FAILS with an inner that declared nothing and draws from the module.

    Verified to have teeth: treating any declaration on the selection as covering every
    draw (the pre-fix blanket skip) made the ModuleRandom arm SKIP.
    """
    with isolated_registry():
        _module_random_leaf()
        _groups_composite("OwnDraw", declare=True, own_draw=True)
        spec = get_policy_spec("dispatch", "OwnDraw")
        clean = battery._no_module_rng(battery._Outcomes(spec, {}))
        assert isinstance(clean, battery.Skip) and "direct draw(s) by the declaring node(s) OwnDraw" in clean.detail
        ok, detail = battery._no_module_rng(battery._Outcomes(spec, {"inner": {"type": "ModuleRandom"}}))
        assert not ok and "ModuleRandom on" in detail, detail


# --- MINOR-1: the control payload and the CLI ------------------------------------------

def test_control_payload_carries_status_and_counts_skips(monkeypatch):
    """Verified to have teeth: dropping ``status`` from ``_check_dict`` failed it."""
    from apps.container_logistics.policies.testing import battery as battery_mod

    rows = [CheckResult("determinism", "no_module_rng", True, "skipped: declared", 0.1, skipped=True),
            CheckResult("structure", "returns_plan", True, "fine", 0.1)]
    monkeypatch.setattr(battery_mod, "run_battery", lambda slot, name: rows)
    payload = control_policies.cmd_policy_validate("dispatch", "GreedyNearest")
    assert payload["ok"] is True and payload["skipped"] == 1
    assert [r["status"] for r in payload["results"]] == ["SKIP", "PASS"]
    assert payload["results"][0]["ok"] is True and payload["results"][0]["skipped"] is True
    assert payload["message"] == "all 2 checks passed (1 skipped)"


def test_cli_renders_skip_and_exits_0(monkeypatch, capsys):
    """The arm suffix of an id survives the table (Rich read ``[inner=X]`` as markup).

    Verified to have teeth: rendering by ``ok`` alone printed PASS for the skipped row;
    an unescaped id lost its ``[inner=GreedyNearest]``.
    """
    results = [{"id": "determinism.no_module_rng[inner=GreedyNearest]", "ok": True, "status": "SKIP", "skipped": True,
                "detail": "skipped: module_rng declared", "ms": 0.1},
               {"id": "structure.returns_plan", "ok": True, "status": "PASS", "detail": "fine", "ms": 1.0}]
    monkeypatch.setattr(control, "policy_validate", lambda slot, name: {
        "ok": True, "results": results, "skipped": 1, "message": "all 2 checks passed (1 skipped)"})
    assert cli.main(["policy", "validate", "dispatch", "Alpha"]) == 0
    text = capsys.readouterr().out
    row = next(line for line in text.splitlines() if "determinism.no_module_rng[inner=GreedyNearest]" in line)
    assert "SKIP" in row and "PASS" not in row, row


# --- MINOR-7: name lists from the registry ----------------------------------------------

def _extra_leaf(name: str = "ExtraLeafZ"):
    @policy(slot="dispatch", name=name, version="1", description="an extra registered leaf",
            information_model="central")
    class Extra(DispatchPolicy):
        @dataclass(frozen=True)
        class Params(PolicyParams):
            pass

        def decide(self, ctx):
            return Plan(chains={})

    return Extra


def test_the_exact_message_tests_survive_an_extra_leaf():
    """Run the exact-text tests and the arm-list test with one more leaf registered.

    Before MINOR-7 each of these pinned ``GreedyNearest, RandomAssignment`` (or the
    four names), so this extra leaf broke all of them. Verified to have teeth: putting
    the literal leaf list back into ``test_policy_tree_resolution`` (message 2), and
    the literal ``inner=GreedyNearest`` back into ``_WANT_ARMS``, each failed it.
    """
    from tests import test_policy_contract_battery as contract
    from tests import test_policy_framework_params as params_tests
    from tests import test_policy_run_overrides as overrides_tests
    from tests import test_policy_tree_resolution as tree_tests

    with isolated_registry():
        _extra_leaf()
        assert "ExtraLeafZ" in leaf_names().split(", ") and "ExtraLeafZ" in dispatch_names().split(", ")
        assert "Partitioned" not in leaf_names().split(", ")
        tree_tests.test_unknown_inner_is_message_1()
        tree_tests.test_composite_inner_is_message_2()
        overrides_tests.test_solver_naming_a_composite_is_rejected()
        params_tests.test_ref_param_rejects_unknown_target_listing_registered()
        params_tests.test_ref_param_rejects_composite_where_leaf_only()
        for name in ("Partitioned", "PooledMarket"):
            assert "inner=ExtraLeafZ" in contract.expected_arms(name)
            contract.test_builtins_have_their_arms_in_the_battery(name)
    assert "ExtraLeafZ" not in leaf_names()


def test_component_names_come_from_the_live_registry():
    from apps.container_logistics.assignment.policy.offer import OFFER_REGISTRY

    OFFER_REGISTRY["TestExtraOffer"] = OFFER_REGISTRY["OfferAll"]
    try:
        assert "TestExtraOffer" in component_names("market.offer").split(", ")
    finally:
        del OFFER_REGISTRY["TestExtraOffer"]


# --- component names obey the policy-name grammar ---------------------------------------

@pytest.mark.parametrize("bad", ["Bad · Name", "A/B", "X(y)", "k=v", "a,b", "has space", "1st"])
def test_component_names_with_run_name_separators_are_refused(bad):
    """At kind declaration and at resolution (a name injected into the live dict later).

    Verified to have teeth: without the ``check_policy_name`` loop in
    ``register_component_kind`` the declaration was accepted; without the call in
    ``_component_target`` the injected name resolved.
    """
    class Widget:
        """A widget."""
        version = "1"

    with pytest.raises(PolicyRegistrationError, match="must match"):
        components.register_component_kind("test.badname", lambda: {"Plain": Widget, bad: Widget},
                                           "Plain", "Widgets.")
    assert "test.badname" not in components._KINDS

    from apps.container_logistics.assignment.policy.offer import OFFER_REGISTRY

    OFFER_REGISTRY[bad] = OFFER_REGISTRY["OfferAll"]
    try:
        with pytest.raises(PolicyRegistrationError, match="must match"):
            components.ref_target("market.offer", bad, where="policies.dispatch.params.offer")
    finally:
        del OFFER_REGISTRY[bad]
    params_mod._REF_RESOLVERS.pop("test.badname", None)


def test_a_well_formed_component_name_still_resolves():
    assert components.ref_target("market.offer", "OfferNone", where="w").name == "OfferNone"
