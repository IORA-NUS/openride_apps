"""Information containment is no longer self-certified (final review M5).

Before: the battery's only containment check was ``contract.scope_audit``, which runs
the top's OWN ``audit_scopes`` and SKIPs when there is none, so a per-company badge was
unverified text and the reviewer's leaky solver (``review_final/tests/probe/
test_probe_leak.py``: it restricts each company's orders together with EVERY truck and
defines no audit) passed every check. Now:

- ``contract.scope_audit`` FAILS for a top without ``audit_scopes`` whose badge claims
  a decomposition (anything but ``battery.CENTRAL_BADGES``) or whose solve built
  sub-ticks; it SKIPs only for a central decision (no claim, no sub-tick);
- ``contract.information_scope`` (new, every arm) is FRAMEWORK-MEASURED: the battery
  wraps ``Tick.restrict`` / ``Tick.call`` and reports what each sub-tick showed (carrier
  and owner hauliers) and which inner decided it. SKIP with no sub-tick; FAIL when a
  containment badge is backed by no measured split on a multi-company tick.

Declared valtable delta (``policy-validate`` over the four built-ins): ONLY new rows
``contract.information_scope`` / ``contract.information_scope[<arm>]`` -- SKIP on every
arm of the central leaves (``GreedyNearest`` 2, ``RandomAssignment`` 2), PASS on every
arm of ``Partitioned`` (2) and ``PooledMarket`` (9). No existing row changes (the
leaves' ``contract.scope_audit`` stays SKIP: they are central).

Verified to have teeth (FB, mutate / see it fail / restore):

- reverting ``_scope_audit``'s two new FAIL branches to the old unconditional SKIP made
  ``test_the_reviewers_leaky_probe_is_refused`` and
  ``test_a_containment_badge_without_an_audit_fails`` fail;
- making ``_measured_sub_ticks`` record nothing made
  ``test_information_scope_measures_what_each_sub_tick_showed`` fail and turned
  Partitioned's ``contract.information_scope`` into SKIP;
- ``CENTRAL_BADGES`` without ``"central"`` failed ``test_central_badge_is_the_leaves_badge``
  and the leaves' ``contract.scope_audit`` (they would need an audit they cannot have).
"""

from __future__ import annotations

from dataclasses import dataclass

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.result import Result
from apps.container_logistics.policies.dispatch.tick import Tick
from apps.container_logistics.policies.framework import PolicyParams, policy
from apps.container_logistics.policies.framework.registry import isolated_registry
from apps.container_logistics.policies.testing import battery
from apps.container_logistics.policies.testing.battery import run_battery
from tests.test_policy_battery_catches_broken_policies import _first_feasible


def _by_id(name):
    return {r.id: r for r in run_battery("planner", name)}


def test_the_reviewers_leaky_probe_is_refused():
    """The reviewer's probe, verbatim in behaviour: it passed every check before the fix."""
    with isolated_registry():
        @policy(slot="planner", name="LeakyNoAudit", version="1", description="leaks, no self-audit")
        class LeakyNoAudit(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def solve_tick(self, tick, params):
                hids = sorted({assign_constraints.haulier_of(o) for o in tick.raw_orders} - {None})
                for hid in hids:
                    tick.restrict(trucks=list(tick.raw_trucks),
                                  orders=[o for o in tick.raw_orders if assign_constraints.haulier_of(o) == hid],
                                  scope=f"company:{hid}", sample_rng=tick.rng)
                return Result(pairs=tuple((t, c[0]) for t, c in _first_feasible(tick).items()))

        results = _by_id("LeakyNoAudit")
        failing = {i for i, r in results.items() if not r.ok}
        assert failing == {"contract.scope_audit"}, failing
        assert "built" in results["contract.scope_audit"].detail and "no audit_scopes" in \
            results["contract.scope_audit"].detail
        info = results["contract.information_scope"]
        # The measurement shows the leak: a "company" sub-tick held every company's trucks.
        assert info.status == "PASS" and "held trucks of more than one carrier" in info.detail, info.detail
        assert not info.detail.startswith("measured 0"), info.detail


def test_a_containment_badge_without_an_audit_fails():
    """A ``per company`` badge on a solver that never splits the tick: both checks FAIL."""
    with isolated_registry():
        @policy(slot="planner", name="BadgedCentral", version="1", description="claims per company, is central",
                badge="per company")
        class BadgedCentral(DispatchPolicy):
            @dataclass(frozen=True)
            class Params(PolicyParams):
                pass

            def solve_tick(self, tick, params):
                return Result(pairs=tuple((t, c[0]) for t, c in _first_feasible(tick).items()))

        results = _by_id("BadgedCentral")
        assert results["contract.scope_audit"].status == "FAIL"
        assert "'per company'" in results["contract.scope_audit"].detail
        assert results["contract.information_scope"].status == "FAIL"
        assert "built no sub-tick" in results["contract.information_scope"].detail


def test_central_badge_is_the_leaves_badge():
    from apps.container_logistics.policies.solver_lib.pair_leaf import CENTRAL_BADGE

    assert battery.CENTRAL_BADGES == frozenset({None, CENTRAL_BADGE})


def test_information_scope_measures_what_each_sub_tick_showed():
    parts = _by_id("Partitioned")
    info = parts["contract.information_scope"]
    assert info.status == "PASS", info.detail
    assert "solved by an inner (RandomAssignment)" in info.detail and "badge 'per group'" in info.detail
    assert parts["contract.scope_audit"].status == "PASS"
    leaf = _by_id("GreedyNearest")
    assert leaf["contract.information_scope"].status == "SKIP"
    assert leaf["contract.scope_audit"].status == "SKIP"


def test_the_measurement_restores_tick_methods():
    restrict, call = Tick.restrict, Tick.call
    with battery._measured_sub_ticks([]):
        assert Tick.restrict is not restrict and Tick.call is not call
    assert Tick.restrict is restrict and Tick.call is call
    try:
        with battery._measured_sub_ticks([]):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert Tick.restrict is restrict and Tick.call is call
