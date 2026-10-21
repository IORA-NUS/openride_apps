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
    PolicyRegistrationError,
    SlotNotImplementedError,
    UnknownPolicyError,
    UnknownSlotError,
    default_policy_name,
    get_policy_spec,
    list_policies,
    param,
    policy,
    spec_for_class,
)
from apps.container_logistics.policies.framework import registry
from apps.container_logistics.policies.framework.registry import isolated_registry

REPO_ROOT = Path(__file__).resolve().parents[1]


def _dummy(name: str = "TestDummy", **policy_kwargs):
    """Define and register a fresh dispatch dummy; returns the class."""
    @dataclass(frozen=True)
    class Params(PolicyParams):
        k: float = param(1.0, ge=0, help="A knob.")

    cls = type(name, (), {"Params": Params})
    kwargs = {"slot": "dispatch", "name": name, "version": "1", "description": "A test dummy."}
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
        spec = get_policy_spec("dispatch", "TestDummyA")
        assert spec.cls is cls
        assert spec.params_cls is cls.Params
        assert spec.version == "1" and spec.kind() == "operator"
        assert spec_for_class(cls) is spec
        assert spec in list_policies("dispatch")


def test_same_class_reregistration_is_idempotent():
    """Decorating the same class twice with the same metadata is a no-op.

    Verified to have teeth: removing the ``existing == spec`` early return made the
    second registration raise "registered twice".
    """
    with isolated_registry():
        cls = _dummy("TestDummyB")
        before = len(list_policies())
        policy(slot="dispatch", name="TestDummyB", version="1", description="A test dummy.")(cls)
        assert len(list_policies()) == before


def test_same_class_reregistered_with_different_metadata_raises():
    """Two diverging declarations for one class are a bug, not a silent overwrite.

    Verified to have teeth: returning before the "registered twice" raise failed it.
    """
    with isolated_registry():
        cls = _dummy("TestDummyB2")
        with pytest.raises(PolicyRegistrationError, match="registered twice"):
            policy(slot="dispatch", name="TestDummyB2", version="2", description="A test dummy.")(cls)


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
            policy(slot="dispatch", name="TestDummyD2", version="1", description="x")(cls)


def test_registering_into_an_unknown_slot_raises():
    """An unknown slot fails at registration and lists the known slots.

    Verified to have teeth: replacing the ``slot_spec`` call in
    ``_check_slot_registrable`` with ``SLOTS.get`` + early return let it register.
    """
    with isolated_registry():
        with pytest.raises(PolicyRegistrationError, match=r"Unknown policy slot 'dispatchh'.*Known slots: dispatch"):
            _dummy("TestDummyE", slot="dispatchh")


def test_registering_into_a_planned_slot_raises_with_phase():
    """A planned slot names the phase it lands in.

    Verified to have teeth: removing the ``status == "planned"`` branch let the dummy
    fall through to the legacy-slot message and the match failed.
    """
    with isolated_registry():
        with pytest.raises(PolicyRegistrationError,
                           match=r"slot 'idle' is declared but not implemented yet \(lands in P5\)"):
            _dummy("TestDummyF", slot="idle")


def test_registering_into_a_legacy_slot_raises_naming_its_config_key():
    """Legacy slots stay configured via ``planner.market.*`` this round (plan §1.2 item 10).

    Verified to have teeth: disabling the non-implemented branch let the dummy register.
    """
    with isolated_registry():
        with pytest.raises(PolicyRegistrationError, match=r"legacy slot configured via planner\.market\.offer"):
            _dummy("TestDummyG", slot="market.offer")


def test_params_must_be_a_frozen_dataclass_subclass():
    """Missing, non-dataclass, non-frozen and non-``PolicyParams`` Params all fail.

    Verified to have teeth: deleting the ``frozen`` check in ``declared_params`` let
    the non-frozen case register; deleting the ``issubclass`` check let the plain
    dataclass register.
    """
    def register(cls):
        return policy(slot="dispatch", name=cls.__name__, version="1", description="x")(cls)

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
                register(type(name, (), attrs))
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
            policy(slot="dispatch", name="TestBadDefault", version="1", description="x")(
                type("TestBadDefault", (), {"Params": Params}))


def test_unknown_policy_error_lists_registered_names():
    """Unknown names never fall back; the message lists names and the CLI command.

    Verified to have teeth: dropping the sort in ``get_policy_spec`` produced
    'TestZed, TestAlpha' and the exact-substring assertion failed.
    """
    with isolated_registry():
        _dummy("TestZed")
        _dummy("TestAlpha")
        with pytest.raises(UnknownPolicyError) as exc:
            get_policy_spec("dispatch", "GreedyNeerest")
    message = str(exc.value)
    assert message.startswith("Unknown dispatch policy 'GreedyNeerest'. Registered dispatch policies: ")
    assert "TestAlpha, TestZed" in message, message
    assert message.endswith(". (List them: openride policy list dispatch)")


def test_unknown_policy_error_format_is_exact():
    """The full message for a registry holding exactly two names (the plan's example)."""
    with isolated_registry():
        registry._REGISTRY.clear()
        _dummy("GreedyNearest")
        _dummy("RandomAssignment")
        with pytest.raises(UnknownPolicyError) as exc:
            get_policy_spec("dispatch", "GreedyNeerest")
    assert str(exc.value) == (
        "Unknown dispatch policy 'GreedyNeerest'. Registered dispatch policies: "
        "GreedyNearest, RandomAssignment. (List them: openride policy list dispatch)"
    )


def test_lookups_in_non_implemented_or_unknown_slots_raise():
    """Lookup errors are specific: unknown slot, planned slot, slot without a default.

    Verified to have teeth: dropping ``_require_implemented`` from ``get_policy_spec``
    turned the planned-slot lookup into UnknownPolicyError; disabling the no-default
    branch of ``default_policy_name`` returned None for 'idle'.
    """
    with pytest.raises(UnknownSlotError):
        get_policy_spec("nope", "X")
    with pytest.raises(SlotNotImplementedError, match=r"lands in P5"):
        get_policy_spec("queue", "Fifo")
    with pytest.raises(SlotNotImplementedError, match="configured via planner.market.claim"):
        get_policy_spec("market.claim", "ClaimNone")
    with pytest.raises(SlotNotImplementedError, match=r"slot 'idle' has no default policy"):
        default_policy_name("idle")
    with pytest.raises(UnknownSlotError):
        list_policies("nope")
    assert default_policy_name("market.offer") == "OfferAll"


def test_list_policies_is_sorted_by_slot_then_name():
    """Listings never depend on registration (import) order.

    Verified to have teeth: returning the unsorted ``specs`` list failed the order assertion.
    """
    with isolated_registry():
        for name in ("TestM", "TestB", "TestY"):
            _dummy(name)
        names = [s.name for s in list_policies("dispatch") if s.name in {"TestM", "TestB", "TestY"}]
        assert names == ["TestB", "TestM", "TestY"]


def test_missing_requirements_are_reported():
    """Unfindable modules are listed in declared order; dotted names with a missing parent count.

    Verified to have teeth: making ``_module_findable`` return True always emptied the list.
    """
    with isolated_registry():
        cls = _dummy("TestNeedsDeps",
                     requires=("definitely_not_a_module_xyz", "json", "definitely_not_a_pkg_xyz.sub"))
        spec = get_policy_spec("dispatch", "TestNeedsDeps")
    assert spec.cls is cls
    assert spec.missing_requirements() == ["definitely_not_a_module_xyz", "definitely_not_a_pkg_xyz.sub"]


def test_slot_base_class_table_is_enforced():
    """The P1 hook: a slot listed in ``_SLOT_BASES`` rejects classes outside that base.

    Verified to have teeth: making ``_check_slot_base`` return early let it register.
    """
    class Base:
        pass

    with isolated_registry():
        saved = dict(registry._SLOT_BASES)
        registry._SLOT_BASES["dispatch"] = f"{__name__}:_BaseForSlotTest"
        globals()["_BaseForSlotTest"] = Base
        try:
            with pytest.raises(PolicyRegistrationError, match="must subclass"):
                _dummy("TestNotABase")
        finally:
            registry._SLOT_BASES.clear()
            registry._SLOT_BASES.update(saved)
            globals().pop("_BaseForSlotTest")


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
        get_policy_spec("dispatch", "TestLeak")


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

    specs = {s.name: s for s in list_policies("dispatch")}
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
    assert default_policy_name("dispatch") == "RandomAssignment"
