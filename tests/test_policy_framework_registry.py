"""Locks the ``@policy`` registry contract (plan §4 P0.3).

Registration must fail loud at import for every malformed declaration, lookups must
never fall back, and the registry must stay a leaf (plan §2.7: importing the
framework never imports ``assignment``). Every test that registers a dummy class runs
under ``isolated_registry()`` so the process-wide registry is left untouched.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from apps.container_logistics.policies.framework import (
    PolicyParams,
    PolicyRef,
    PolicyRegistrationError,
    UnknownPolicyError,
    UnknownSlotError,
    default_policy_name,
    get_policy_spec,
    list_policies,
    param,
    policy,
    ref,
    spec_for_class,
)
from apps.container_logistics.policies.framework import registry
from apps.container_logistics.policies.framework.registry import isolated_registry
from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.plan import Plan


class _TestPolicyBase(DispatchPolicy):
    """Dummy dispatch policies subclass this: the dispatch slot requires DispatchPolicy."""

    def decide(self, ctx):
        return Plan(chains={})


REPO_ROOT = Path(__file__).resolve().parents[1]


def _dummy(name: str = "TestDummy", **policy_kwargs):
    """Define and register a fresh dispatch dummy; returns the class."""
    @dataclass(frozen=True)
    class Params(PolicyParams):
        k: float = param(1.0, ge=0, help="A knob.")

    cls = type(name, (_TestPolicyBase,), {"Params": Params})
    kwargs = {"slot": "planner", "name": name, "version": "1", "description": "A test dummy.",
              "information_model": "central"}
    kwargs.update(policy_kwargs)
    return policy(**kwargs)(cls)


def test_policy_decorator_registers_and_returns_the_class():
    """The decorator returns the class unchanged and makes it resolvable by name.

    Verified to have teeth: making ``decorate`` return ``None`` failed the identity
    assertion; skipping ``_store`` failed the lookup with UnknownPolicyError.
    """
    with isolated_registry():
        cls = _dummy("TestDummyA")
        assert isinstance(cls, type) and cls.__name__ == "TestDummyA"
        spec = get_policy_spec("planner", "TestDummyA")
        assert spec.cls is cls
        assert spec.params_cls is cls.Params
        assert spec.version == "1" and spec.kind() == "operator"
        assert spec_for_class(cls) is spec
        assert spec in list_policies("planner")


def test_same_class_reregistration_is_idempotent():
    """Decorating the same class twice with the same metadata is a no-op.

    Verified to have teeth: removing the ``existing == spec`` early return made the
    second registration raise "registered twice".
    """
    with isolated_registry():
        cls = _dummy("TestDummyB")
        before = len(list_policies())
        policy(slot="planner", information_model="central", name="TestDummyB", version="1", description="A test dummy.")(cls)
        assert len(list_policies()) == before


def test_same_class_reregistered_with_different_metadata_raises():
    """Two diverging declarations for one class are a bug, not a silent overwrite.

    Verified to have teeth: returning before the "registered twice" raise failed it.
    """
    with isolated_registry():
        cls = _dummy("TestDummyB2")
        with pytest.raises(PolicyRegistrationError, match="registered twice"):
            policy(slot="planner", information_model="central", name="TestDummyB2", version="2", description="A test dummy.")(cls)


def test_duplicate_name_with_a_different_class_raises():
    """``(slot, name)`` bound to another class fails loud, naming both classes.

    Verified to have teeth: changing the ``existing.cls is not spec.cls`` branch to
    overwrite the entry made this test fail with DID NOT RAISE.
    """
    with isolated_registry():
        _dummy("TestDummyC")
        with pytest.raises(PolicyRegistrationError, match="already registered to .*TestDummyC"):
            _dummy("TestDummyC")


def test_one_class_cannot_back_two_policies():
    """``spec_for_class`` needs a unique answer, so a class registers under one name.

    Verified to have teeth: disabling the same-class scan in ``_store`` failed it.
    """
    with isolated_registry():
        cls = _dummy("TestDummyD")
        with pytest.raises(PolicyRegistrationError, match="one class backs exactly one policy"):
            policy(slot="planner", information_model="central", name="TestDummyD2", version="1", description="x")(cls)


def test_registering_into_an_unknown_slot_raises():
    """An unknown slot fails at registration and lists the known slots.

    Verified to have teeth: replacing the ``slot_spec`` call in
    ``_check_slot_registrable`` with ``SLOTS.get`` + early return let it register.
    """
    with isolated_registry():
        with pytest.raises(PolicyRegistrationError, match=r"Unknown policy slot 'dispatchh'.*Known slots: planner"):
            _dummy("TestDummyE", slot="dispatchh")


def test_registering_into_a_removed_slot_is_an_unknown_slot():
    """``idle`` (and queue/world.*) were removed as slots: registering into one is the
    ordinary unknown-slot refusal."""
    with isolated_registry():
        with pytest.raises(PolicyRegistrationError,
                           match=r"Unknown policy slot 'idle'\. Known slots: planner\.$"):
            _dummy("TestDummyF", slot="idle")


def test_market_components_are_not_slots():
    """Round 2: ``market.offer``/``market.claim`` are PooledMarket components, not slots.

    Verified to have teeth: re-adding the ``market.offer`` row to ``slots._TABLE`` (as
    implemented) let the dummy register.
    """
    with isolated_registry():
        with pytest.raises(PolicyRegistrationError, match=r"Unknown policy slot 'market\.offer'"):
            _dummy("TestDummyG", slot="market.offer")


def test_params_must_be_a_frozen_dataclass_subclass():
    """Missing, non-dataclass, non-frozen and non-``PolicyParams`` Params all fail.

    Verified to have teeth: deleting the ``frozen`` check in ``declared_params`` let
    the non-frozen case register; deleting the ``issubclass`` check let the plain
    dataclass register.
    """
    def register(cls):
        return policy(slot="planner", information_model="central", name=cls.__name__, version="1", description="x")(cls)

    @dataclass
    class NotFrozen(PolicyParams):
        k: float = param(1.0, help="x")

    @dataclass(frozen=True)
    class NotPolicyParams:
        k: float = param(1.0, help="x")

    class NotDataclass(PolicyParams):
        k: float = 1.0

    cases = {
        "TestNoParams": ({}, "missing nested Params"),
        "TestNotFrozen": ({"Params": NotFrozen}, "must be a frozen dataclass"),
        "TestNotPolicyParams": ({"Params": NotPolicyParams}, "must subclass PolicyParams"),
        "TestNotDataclass": ({"Params": NotDataclass}, r"must be decorated with @dataclass\(frozen=True\)"),
    }
    with isolated_registry():
        for name, (attrs, message) in cases.items():
            with pytest.raises(PolicyRegistrationError, match=message):
                register(type(name, (_TestPolicyBase,), attrs))
            assert all(s.name != name for s in list_policies()), f"{name} must not be registered"


def test_defaults_violating_their_own_bounds_fail_registration():
    """A default outside its own bound is caught at import, not at the first run.

    Verified to have teeth: removing the ``from_mapping(None, ...)`` call from
    ``_checked_params`` let the class register.
    """
    @dataclass(frozen=True)
    class Params(PolicyParams):
        k: float = param(-1.0, ge=0, help="x")

    with isolated_registry():
        with pytest.raises(PolicyRegistrationError, match=r"defaults fail their own validation.*'k' must be >= 0"):
            policy(slot="planner", information_model="central", name="TestBadDefault", version="1", description="x")(
                type("TestBadDefault", (_TestPolicyBase,), {"Params": Params}))


def test_unknown_policy_error_lists_registered_names():
    """Unknown names never fall back; the message lists names and the CLI command.

    Verified to have teeth: dropping the sort in ``get_policy_spec`` produced
    'TestZed, TestAlpha' and the exact-substring assertion failed.
    """
    with isolated_registry():
        _dummy("TestZed")
        _dummy("TestAlpha")
        with pytest.raises(UnknownPolicyError) as exc:
            get_policy_spec("planner", "GreedyNeerest")
    message = str(exc.value)
    assert message.startswith("Unknown planner policy 'GreedyNeerest'. Registered planner policies: ")
    assert "TestAlpha, TestZed" in message, message
    assert message.endswith(". (List them: openride policy list planner)")


def test_unknown_policy_error_format_is_exact():
    """The full message for a registry holding exactly two names (the plan's example)."""
    with isolated_registry():
        registry._REGISTRY.clear()
        _dummy("GreedyNearest")
        _dummy("RandomAssignment")
        with pytest.raises(UnknownPolicyError) as exc:
            get_policy_spec("planner", "GreedyNeerest")
    assert str(exc.value) == (
        "Unknown planner policy 'GreedyNeerest'. Registered planner policies: "
        "GreedyNearest, RandomAssignment. (List them: openride policy list planner)"
    )


def test_lookups_in_unknown_or_removed_slots_raise():
    """Lookups in an unknown slot -- including the removed ``idle``/``queue`` -- raise
    ``UnknownSlotError`` naming the known slots; never a fallback."""
    with pytest.raises(UnknownSlotError):
        get_policy_spec("nope", "X")
    with pytest.raises(UnknownSlotError, match=r"^Unknown policy slot 'queue'\. Known slots: planner\.$"):
        get_policy_spec("queue", "Fifo")
    with pytest.raises(UnknownSlotError):
        get_policy_spec("market.claim", "ClaimNone")
    with pytest.raises(UnknownSlotError, match=r"^Unknown policy slot 'idle'\. Known slots: planner\.$"):
        default_policy_name("idle")
    assert default_policy_name("planner") == "Partitioned"
    with pytest.raises(UnknownSlotError):
        list_policies("nope")


def test_list_policies_is_sorted_by_slot_then_name():
    """Listings never depend on registration (import) order.

    Verified to have teeth: returning the unsorted ``specs`` list failed the order assertion.
    """
    with isolated_registry():
        for name in ("TestM", "TestB", "TestY"):
            _dummy(name)
        names = [s.name for s in list_policies("planner") if s.name in {"TestM", "TestB", "TestY"}]
        assert names == ["TestB", "TestM", "TestY"]


def test_missing_requirements_are_reported():
    """Unfindable modules are listed in declared order; dotted names with a missing parent count.

    Verified to have teeth: making ``_module_findable`` return True always emptied the list.
    """
    with isolated_registry():
        cls = _dummy("TestNeedsDeps",
                     requires=("definitely_not_a_module_xyz", "json", "definitely_not_a_pkg_xyz.sub"))
        spec = get_policy_spec("planner", "TestNeedsDeps")
    assert spec.cls is cls
    assert spec.missing_requirements() == ["definitely_not_a_module_xyz", "definitely_not_a_pkg_xyz.sub"]


def test_slot_entry_point_table_is_enforced():
    """The registration hook: a slot listed in ``_SLOT_ENTRY_POINTS`` rejects a class without that callable.

    (Was ``_SLOT_BASES``: a required base class. WS1 relaxed it to a required entry
    point, G5.) Verified to have teeth: making ``_check_slot_base`` return early let it
    register.
    """
    with isolated_registry():
        saved = dict(registry._SLOT_ENTRY_POINTS)
        registry._SLOT_ENTRY_POINTS["planner"] = "_slot_test_entry_point"
        try:
            with pytest.raises(PolicyRegistrationError, match="must define a callable _slot_test_entry_point"):
                _dummy("TestNoEntryPoint")
        finally:
            registry._SLOT_ENTRY_POINTS.clear()
            registry._SLOT_ENTRY_POINTS.update(saved)


def test_isolated_registry_restores_global_state():
    """Everything registered inside the block, and the discovery flag, is rolled back.

    Verified to have teeth: removing the ``_REGISTRY.clear()/update`` restore left
    'TestLeak' registered after the block and the final lookup did not raise.
    """
    registry.discover_builtin_policies()
    before = list_policies()
    discovery_before = dict(registry._DISCOVERY)
    with isolated_registry():
        _dummy("TestLeak")
        registry._DISCOVERY["done"] = False
    assert list_policies() == before
    assert registry._DISCOVERY == discovery_before
    with pytest.raises(UnknownPolicyError):
        get_policy_spec("planner", "TestLeak")


def _fake_policy_package(tmp_path, monkeypatch) -> str:
    """Create an importable package whose modules record their import order."""
    pkg = tmp_path / "fake_policy_pkg_xyz"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("ORDER = []\n")
    for name in ("zeta", "alpha", "mid"):
        (pkg / f"{name}.py").write_text("from . import ORDER\nORDER.append(__name__.rsplit('.', 1)[1])\n")
    (pkg / "_private.py").write_text("raise RuntimeError('private modules must be skipped')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(registry, "BUILTIN_POLICY_PACKAGES", ("fake_policy_pkg_xyz",))
    return "fake_policy_pkg_xyz"


def test_discovery_is_idempotent_and_imports_in_sorted_order(tmp_path, monkeypatch):
    """Discovery walks each package once, in sorted order, skipping ``_private`` modules.

    Verified to have teeth: reverse-sorting the module names failed the order
    assertion; dropping the ``_`` skip raised the private module's RuntimeError;
    dropping the ``done`` early return made the second call walk the package again.
    """
    walks: list[str] = []
    real_walk = registry._import_package_modules
    with isolated_registry():
        monkeypatch.setattr(registry, "_import_package_modules",
                            lambda name: (walks.append(name), real_walk(name)))
        package = _fake_policy_package(tmp_path, monkeypatch)
        registry._DISCOVERY["done"] = False
        registry.discover_builtin_policies()
        registry.discover_builtin_policies()
        order = list(sys.modules[package].ORDER)
    for name in [m for m in sys.modules if m.startswith(package)]:
        del sys.modules[name]
    assert order == ["alpha", "mid", "zeta"]
    assert walks == [package], f"a second discovery must not walk again: {walks}"


def test_discovery_of_a_missing_package_raises_clearly(monkeypatch):
    """A stale ``BUILTIN_POLICY_PACKAGES`` entry is a loud error, not an empty catalog.

    Verified to have teeth: setting ``done`` in the ``finally`` (i.e. even on failure)
    failed the retry assertion.
    """
    with isolated_registry():
        registry._DISCOVERY["done"] = False
        monkeypatch.setattr(registry, "BUILTIN_POLICY_PACKAGES",
                            ("apps.container_logistics.policies.no_such_pkg",))
        with pytest.raises(PolicyRegistrationError, match="does not exist"):
            registry.discover_builtin_policies()
        assert registry._DISCOVERY["done"] is False, "a failed discovery must retry next time"


def test_framework_import_does_not_pull_in_assignment():
    """Leaf rule (plan §2.7): the framework must import without ``assignment``.

    Runs in a fresh interpreter because this test process has ``assignment`` loaded.
    Verified to have teeth: adding ``import apps.container_logistics.assignment.policy.offer``
    to the top of ``catalog.py`` made the subprocess report the leak.
    """
    code = (
        "import sys\n"
        "import apps.container_logistics.policies\n"
        "import apps.container_logistics.policies.framework\n"
        "import apps.container_logistics.policies.framework.catalog\n"
        "import apps.container_logistics.policies.dispatch\n"
        "leaked = sorted(m for m in sys.modules if m.startswith('apps.container_logistics.assignment'))\n"
        "print(leaked)\n"
        "sys.exit(1 if leaked else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, f"framework import pulled in assignment: {result.stdout}{result.stderr}"


def test_discovery_finds_both_builtins_with_version_1():
    """The two built-in solvers register in place, in the dispatch slot, at version 1.

    They live in ``assignment/solver/`` (plan §1.2 item 5) and are found by discovery
    alone: no test-side import of the solver package precedes the lookup. Random is
    the only order-sensitive one, with its stated reason.
    Verified to have teeth: setting RandomAssignment's ``version="2"`` in
    ``random_assignment.py`` failed the version assertion.
    """
    from apps.container_logistics.assignment.solver import (
        GreedyNearestSolver,
        RandomAssignmentSolver,
    )

    specs = {s.name: s for s in list_policies("planner")}
    assert {"GreedyNearest", "RandomAssignment"} <= set(specs), sorted(specs)
    greedy, rand = specs["GreedyNearest"], specs["RandomAssignment"]
    assert (greedy.version, rand.version) == ("1", "1")
    assert greedy.cls is GreedyNearestSolver and rand.cls is RandomAssignmentSolver
    assert greedy.order_sensitive is None
    assert rand.order_sensitive and "byte-identical" in rand.order_sensitive
    assert greedy.params_cls.from_mapping(None, where="t").to_dict() == {
        "candidates": "spatial", "dual_cycle_bonus_km": 5.0, "dual_cycle_radius_km": 0.5,
    }
    assert rand.params_cls.from_mapping(None, where="t").to_dict() == {"candidates": "sampled"}
    assert (greedy.information_model, rand.information_model) == ("central", "central")
    assert not greedy.is_composite and not rand.is_composite
    assert (greedy.share_tag_format, rand.share_tag_format) == ("audited", "audited")


def test_dispatch_slot_requires_solve_tick_not_a_base_class():
    """The shipped table requires a callable ``solve_tick``; ``DispatchPolicy`` is an optional helper (G5).

    A plain class with ``solve_tick`` registers; one without is refused, and so is one
    that only has the PAIR API ``solve`` (B1: ``solve`` is never the entry point).
    Verified to have teeth: emptying ``_SLOT_ENTRY_POINTS`` let the plain class register.
    """
    @dataclass(frozen=True)
    class Params(PolicyParams):
        pass

    with isolated_registry():
        with pytest.raises(PolicyRegistrationError, match="must define a callable solve_tick"):
            policy(slot="planner", information_model="central", name="TestPlainClass", version="1", description="x")(
                type("TestPlainClass", (), {"Params": Params}))
        with pytest.raises(PolicyRegistrationError, match="must define a callable solve_tick"):
            policy(slot="planner", information_model="central", name="TestPairOnly", version="1", description="x")(
                type("TestPairOnly", (), {"Params": Params, "solve": lambda self, *a, **k: []}))
        plain = type("TestPlainSolver", (), {"Params": Params, "solve_tick": lambda self, tick, params: None})
        policy(slot="planner", information_model="central", name="TestPlainSolver", version="1",
               description="x")(plain)
        assert registry.get_policy_spec("planner", "TestPlainSolver").cls is plain


# --- round 2 (docs/plans/policy_slots_round2_whole_tick.md §5 C1) ---------------------


def _leaf_with_choices(name: str, **policy_kwargs):
    """A Greedy-shaped dummy: one str-choice param, so it has exactly one non-default arm."""
    @dataclass(frozen=True)
    class Params(PolicyParams):
        candidates: str = param("spatial", choices=("spatial", "sampled"), help="Candidates.")

    kwargs = {"slot": "planner", "name": name, "version": "1", "description": "x",
              "information_model": "central"}
    kwargs.update(policy_kwargs)
    return policy(**kwargs)(type(name, (_TestPolicyBase,), {"Params": Params}))


def _composite(name: str, **policy_kwargs):
    """A dummy with an ``inner`` dispatch ref (and a market.offer component ref)."""
    @dataclass(frozen=True)
    class Params(PolicyParams):
        inner: PolicyRef = param(ref("RandomAssignment"), kind="planner", leaf_only=True, help="Inner.")
        offer: PolicyRef = param(ref("OfferAll"), kind="market.offer", help="Offer.")

    kwargs = {"slot": "planner", "name": name, "version": "1", "description": "x",
              "information_model": "per_group"}
    kwargs.update(policy_kwargs)
    return policy(**kwargs)(type(name, (_TestPolicyBase,), {"Params": Params}))


def test_dispatch_policy_must_declare_information_model():
    """A dispatch policy without (or with an unknown) information model never registers.

    Verified to have teeth: making ``_check_information_model`` return early let both
    dummies register (DID NOT RAISE).
    """
    with isolated_registry():
        with pytest.raises(PolicyRegistrationError) as exc:
            _dummy("X", information_model=None)
        assert str(exc.value) == (
            "@policy(slot='planner', name='X'): declare information_model= one of central, "
            "per_group, per_company (whose private data this policy's decision may use)."
        )
        with pytest.raises(PolicyRegistrationError, match=r"one of central, per_group, per_company .*\(got 'global'\)"):
            _dummy("TestBadModel", information_model="global")
        for model in ("central", "per_group", "per_company"):
            assert _dummy(f"TestModel_{model}", information_model=model)
            assert get_policy_spec("planner", f"TestModel_{model}").information_model == model


def test_share_tag_format_v1_reserved_for_partitioned():
    """``v1`` (the pre-v2 wire format) is registration-reserved to ``Partitioned``.

    Verified to have teeth: dropping the ``name != _V1_OWNER`` condition let 'X'
    register with v1; making the check reject every v1 failed the Partitioned case.
    """
    with isolated_registry():
        with pytest.raises(PolicyRegistrationError) as exc:
            _dummy("X", share_tag_format="v1")
        assert str(exc.value) == (
            "@policy(slot='planner', name='X'): share_tag_format='v1' is reserved for the "
            "Partitioned built-in (the pre-v2 wire format, kept only for byte identity); new "
            "policies use the default 'audited'."
        )
        with pytest.raises(PolicyRegistrationError, match="share_tag_format must be one of v1, audited"):
            _dummy("TestBadFormat", share_tag_format="v2")
        assert get_policy_spec("planner", _dummy("TestDefaultFormat").__name__).share_tag_format == "audited"
        registry._REGISTRY.pop(("planner", "Partitioned"), None)  # the real one, from C3 on
        _composite("Partitioned", share_tag_format="v1")
        assert get_policy_spec("planner", "Partitioned").share_tag_format == "v1"


def test_arm_budget_keys_must_name_arms():
    """A str-choice ``arm_budgets_ms`` key must name a non-default arm; ref keys are left
    to the battery test (their targets may register later).

    Verified to have teeth: replacing the ``key not in arms`` raise with ``continue``
    let the misspelt key register; dropping the ``param_name in refs`` skip failed the
    composite registration.
    """
    with isolated_registry():
        registry._REGISTRY.clear()
        with pytest.raises(PolicyRegistrationError) as exc:
            _leaf_with_choices("GreedyNearest", arm_budgets_ms={"candidate=sampled": 5000})
        assert str(exc.value) == (
            "@policy(slot='planner', name='GreedyNearest'): arm_budgets_ms key "
            "'candidate=sampled' is not an arm of this policy. Arms: candidates=sampled."
        )
        with pytest.raises(PolicyRegistrationError, match="'candidates=spatial' is not an arm"):
            _leaf_with_choices("TestDefaultArm", arm_budgets_ms={"candidates=spatial": 900})
        for bad in (0, True, 1.5):
            with pytest.raises(PolicyRegistrationError, match="must be a positive integer"):
                _leaf_with_choices("TestBadMs", arm_budgets_ms={"candidates=sampled": bad})
        _leaf_with_choices("TestGoodArm", arm_budgets_ms={"candidates=sampled": 5000})
        assert dict(get_policy_spec("planner", "TestGoodArm").arm_budgets_ms) == {"candidates=sampled": 5000}
        _composite("TestRefArm", arm_budgets_ms={"inner=GreedyNearest": 800})
        assert dict(get_policy_spec("planner", "TestRefArm").arm_budgets_ms) == {"inner=GreedyNearest": 800}


def test_composite_flag_from_a_dispatch_ref():
    """``refs`` lists every ref param; only a DISPATCH ref makes a policy composite, and
    registration checks a ref default's shape without resolving it.

    Verified to have teeth: computing ``is_composite`` as ``bool(self.refs)`` made the
    offer-only dummy composite; resolving ref defaults in ``check_defaults`` made the
    not-yet-registered default fail registration.
    """
    @dataclass(frozen=True)
    class OfferOnly(PolicyParams):
        offer: PolicyRef = param(ref("OfferAll"), kind="market.offer", help="Offer.")

    @dataclass(frozen=True)
    class LaterInner(PolicyParams):
        inner: PolicyRef = param(ref("TestRegisteredLater"), kind="planner", help="Inner.")

    with isolated_registry():
        spec = get_policy_spec("planner", _composite("TestComposite").__name__)
        assert spec.refs == (("inner", "planner", True), ("offer", "market.offer", False))
        assert spec.is_composite
        policy(slot="planner", name="TestOfferOnly", version="1", description="x",
               information_model="central")(type("TestOfferOnly", (_TestPolicyBase,), {"Params": OfferOnly}))
        offer_only = get_policy_spec("planner", "TestOfferOnly")
        assert offer_only.refs == (("offer", "market.offer", False),) and not offer_only.is_composite
        policy(slot="planner", name="TestLaterInner", version="1", description="x",
               information_model="per_group")(type("TestLaterInner", (_TestPolicyBase,), {"Params": LaterInner}))
        with pytest.raises(UnknownPolicyError, match="unknown planner policy 'TestRegisteredLater'"):
            LaterInner.from_mapping(None, where="t")
        _dummy("TestRegisteredLater")
        assert LaterInner.from_mapping(None, where="t").inner.type == "TestRegisteredLater"


def test_every_policy_default_tree_resolves():
    """Every registered policy's defaults resolve in full, refs included (registration
    only checks a ref default's shape), and render in both dict forms.

    Verified to have teeth: adding ``inner: PolicyRef = param(ref("NoSuchPolicy"),
    kind="planner", ...)`` to ``GreedyNearestSolver.Params`` still registered (shape
    only) and failed here with UnknownPolicyError.
    """
    for spec in list_policies():
        defaults = spec.params_cls.from_mapping(None, where=f"defaults of {spec.name}")
        assert spec.params_cls.from_mapping(defaults.to_profile_dict(), where="t") == defaults, spec.name
        for name, _kind, _leaf in spec.refs:
            assert getattr(defaults, name).version is not None, (spec.name, name)
