"""Round-2 framework fixes from the early review (findings 1, 2, 5, 7, 11, 15).

What is locked, and why each matters:

- A nested ref to a policy whose ``requires`` are missing fails loud with the top
  level's ``PolicyDependencyError`` wording, prefixed by the ref path (#1): otherwise
  ``Partitioned(inner=NetworkFlow)`` without ortools compiles and dies mid-run.
- Ref DEFAULTS that form a cycle are refused at the registration that closes the
  cycle, with a ``PolicyRegistrationError`` naming the path; a cycle that bypassed
  registration (component kinds) is a named ``PolicyParamsError``, never a
  ``RecursionError``; and one bad entry never blanks the catalog (#2).
- Component kinds are declared by the solver side (``register_component_kind``) and
  still found when nothing imported the market modules; the ``v1`` share-tag
  reservation is one allowlist; the framework never imports ``dispatch.legacy`` (#5).
- A ref ``version`` must be a non-blank string or null, and drift from the registered
  version is logged as ``POLICY VERSION DRIFT``, nested and at the top level (#7).
- The ``--policy-param`` hint is right for ``profile.policies...`` paths and absent
  for paths with no CLI spelling (#11).
- ``instantiate_component`` refuses params of another class and honours #7 (#15).
"""

from __future__ import annotations

import ast
import json
import logging
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from apps.container_logistics.assignment.policy.offer import OFFER_REGISTRY, OfferAllPolicy, OfferSparePolicy
from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.dispatch.plan import Plan
from apps.container_logistics.policies.dispatch.profile import resolve_profile_policies
from apps.container_logistics.policies.framework import (
    PolicyDependencyError,
    PolicyParams,
    PolicyParamsError,
    PolicyRef,
    PolicyRegistrationError,
    param,
    policy,
    ref,
    register_component_kind,
    resolve_policies_block,
)
from apps.container_logistics.policies.framework import components, params as params_mod, registry
from apps.container_logistics.policies.framework.catalog import component_catalog, policy_catalog
from apps.container_logistics.policies.framework.components import COMPONENT_KINDS, instantiate_component
from apps.container_logistics.policies.framework.params import VERSION_DRIFT_PREFIX, _cli_path
from apps.container_logistics.policies.framework.registry import get_policy_spec, isolated_registry

REPO = Path(__file__).resolve().parents[1]
FRAMEWORK = REPO / "apps" / "container_logistics" / "policies" / "framework"
MISSING = "definitely_not_a_module_xyz"


class _Base(DispatchPolicy):
    def decide(self, ctx):  # pragma: no cover - resolution only
        return Plan(chains={})


def _register(name: str, params_cls: type, *, model: str = "central", **kwargs) -> type:
    return policy(slot="planner", name=name, version="1", description="x",
                  information_model=model, **kwargs)(type(name, (_Base,), {"Params": params_cls}))


def _wrapper_params(default: str, *, leaf_only: bool = True) -> type:
    @dataclass(frozen=True)
    class Params(PolicyParams):
        inner: PolicyRef = param(ref(default), kind="planner", leaf_only=leaf_only, help="Inner.")
    return Params


@dataclass(frozen=True)
class _Empty(PolicyParams):
    pass


@dataclass(frozen=True)
class _MarketParams(PolicyParams):
    offer: PolicyRef = param(ref("OfferAll"), kind="market.offer", help="Offer.")


# --- #1 -------------------------------------------------------------------------------


def test_nested_ref_to_a_policy_with_missing_requirements_fails_loud_like_the_top_level():
    """The reviewer's repro: ``Wrap(inner=NeedsMissing)`` used to resolve.

    Verified to have teeth: removing the ``require_available`` call from
    ``components._resolver`` let both nested resolutions through (DID NOT RAISE).
    """
    with isolated_registry():
        _register("NeedsMissing", _Empty, requires=(MISSING,))
        wrap = _register("Wrap", _wrapper_params("GreedyNearest"), model="per_group")
        with pytest.raises(PolicyDependencyError) as top:
            resolve_policies_block({"planner": {"type": "NeedsMissing"}})
        with pytest.raises(PolicyDependencyError) as nested:
            wrap.Params.from_mapping({"inner": {"type": "NeedsMissing"}},
                                     where="policies.planner.params for Wrap")
        assert str(nested.value) == f"policies.planner.params.inner: {top.value}"
        assert str(top.value) == (
            f"Planner policy 'NeedsMissing' requires module(s) not importable in this "
            f"environment: {MISSING}. Install them or pick another policy.")
        with pytest.raises(PolicyDependencyError, match=r"^policies\.planner\.params\.inner: Planner"):
            resolve_policies_block({"planner": {"type": "Partitioned",
                                                 "params": {"inner": {"type": "NeedsMissing"}}}})


# --- #2 -------------------------------------------------------------------------------


def test_a_self_referencing_ref_default_is_refused_at_registration():
    """Verified to have teeth: making ``_check_ref_default_cycles`` return at once let
    ``SelfR`` register (DID NOT RAISE)."""
    with isolated_registry():
        with pytest.raises(PolicyRegistrationError) as exc:
            _register("SelfR", _wrapper_params("SelfR", leaf_only=False), model="per_group")
        assert str(exc.value).startswith(
            "@policy(slot='planner', name='SelfR'): ref param defaults form a cycle "
            "(SelfR.inner -> SelfR)"), exc.value
        assert "SelfR" not in [s.name for s in registry.list_policies("planner")]


def test_a_two_policy_default_cycle_is_refused_by_the_registration_that_closes_it():
    """``A -> B`` registers (B unknown yet); ``B -> A`` closes the cycle and is refused.

    Verified to have teeth: skipping the recursive ``walk`` into registered targets
    let ``CycB`` register (DID NOT RAISE).
    """
    with isolated_registry():
        _register("CycA", _wrapper_params("CycB", leaf_only=False), model="per_group")
        with pytest.raises(PolicyRegistrationError, match=r"\(CycB\.inner -> CycA\.inner -> CycB\)"):
            _register("CycB", _wrapper_params("CycA", leaf_only=False), model="per_group")


def test_explicit_finite_self_nesting_still_resolves():
    """Nesting a composite inside itself explicitly is finite and stays legal."""
    with isolated_registry():
        nest = _register("Nest", _wrapper_params("GreedyNearest", leaf_only=False), model="per_group")
        parsed = nest.Params.from_mapping(
            {"inner": {"type": "Nest", "params": {"inner": {"type": "Nest"}}}}, where="p for Nest")
        assert parsed.inner.params.inner.type == "Nest"
        assert parsed.inner.params.inner.params.inner.type == "GreedyNearest"


def test_a_cycle_that_bypassed_registration_is_a_named_params_error_and_the_catalog_survives():
    """Backstop: a component self-cycle (components are not ``@policy``-registered) and
    a dispatch spec whose params were swapped behind the registry's back.

    Verified to have teeth: removing the ``node in stack`` check in
    ``params._resolve_ref`` turned both into ``RecursionError``; removing the
    ``try/except`` in ``catalog._described`` made ``component_catalog()`` raise.
    """
    @dataclass(frozen=True)
    class CyclicParams(PolicyParams):
        back: PolicyRef = param(ref("_CyclicOffer"), kind="market.offer", help="Back.")

    cyclic = type("_CyclicOffer", (OfferAllPolicy,), {"Params": CyclicParams})
    OFFER_REGISTRY["_CyclicOffer"] = cyclic
    try:
        with pytest.raises(PolicyParamsError) as exc:
            _MarketParams.from_mapping({"offer": {"type": "_CyclicOffer"}}, where="m for M")
        assert str(exc.value).startswith(
            "m.offer.params.back: the policy-reference defaults form a cycle "
            "(_CyclicOffer -> _CyclicOffer)"), exc.value
        entry = next(p for p in component_catalog()["market.offer"]["policies"]
                     if p["name"] == "_CyclicOffer")
        assert entry["available"] is False and "form a cycle" in entry["error"]
        assert entry["params_schema"] == {"type": "object", "additionalProperties": False,
                                          "properties": {}} and entry["defaults"] == {}
    finally:
        OFFER_REGISTRY.pop("_CyclicOffer", None)

    with isolated_registry():
        _register("SneakyR", _wrapper_params("GreedyNearest", leaf_only=False), model="per_group")
        key = ("planner", "SneakyR")
        registry._REGISTRY[key] = replace(registry._REGISTRY[key],
                                          params_cls=_wrapper_params("SneakyR", leaf_only=False))
        with pytest.raises(PolicyParamsError, match=r"cycle \(SneakyR -> SneakyR\)"):
            resolve_policies_block({"planner": {"type": "SneakyR"}})


def test_one_bad_policy_entry_is_reported_and_never_blanks_the_catalog():
    """A default naming an unregistered policy registers (shape-only check) but cannot
    be described: the catalog lists it disabled with the resolver's message.

    Verified to have teeth: removing the ``try/except`` in ``catalog._described`` made
    ``policy_catalog()`` raise ``UnknownPolicyError`` for every policy.
    """
    with isolated_registry():
        _register("BadDefault", _wrapper_params("NoSuchLeaf"), model="per_group")
        entries = {p["name"]: p for p in policy_catalog()["planner"]["policies"]}
        bad = entries["BadDefault"]
        assert bad["available"] is False and "unknown planner policy 'NoSuchLeaf'" in bad["error"]
        assert "error" not in entries["GreedyNearest"] and entries["GreedyNearest"]["available"]
        assert json.loads(json.dumps(entries)) == entries


# --- #5 -------------------------------------------------------------------------------


def test_a_solver_side_module_declares_its_own_component_kind():
    """``register_component_kind`` is all a new solver needs; malformed declarations fail.

    Verified to have teeth: dropping the ``_REF_RESOLVERS[kind]`` line from
    ``register_component_kind`` made the ``test.widget`` composite fail to register
    ("needs kind= one of ..."); dropping the default-in-registry check let
    ``default='Nope'`` through.
    """
    class Widget:
        """A widget component."""
        version = "1"

        def __init__(self, params=None):
            self.params = params

    widgets = {"Plain": Widget}
    try:
        declared = register_component_kind("test.widget", lambda: widgets, "Plain", "Widgets.")
        assert COMPONENT_KINDS["test.widget"] is declared and list(COMPONENT_KINDS)[-1] == "test.widget"
        assert list(COMPONENT_KINDS)[:3] == ["market.offer", "market.claim", "market.arbitration"]

        @dataclass(frozen=True)
        class UsesWidget(PolicyParams):
            widget: PolicyRef = param(ref("Plain"), kind="test.widget", help="Widget.")

        with isolated_registry():
            _register("UsesWidget", UsesWidget)
            spec = get_policy_spec("planner", "UsesWidget")
            assert spec.params_cls.from_mapping(None, where="w").widget.version == "1"
        assert [p["name"] for p in component_catalog()["test.widget"]["policies"]] == ["Plain"]
        assert isinstance(instantiate_component("test.widget", PolicyRef(type="Plain")), Widget)

        with pytest.raises(PolicyRegistrationError, match="default 'Nope' is not in the registry"):
            register_component_kind("test.other", lambda: widgets, "Nope", "x")
        with pytest.raises(PolicyRegistrationError, match="other than 'planner'"):
            register_component_kind("planner", lambda: widgets, "Plain", "x")
        foreign = eval("lambda: widgets", {"__name__": "some.other.module", "widgets": widgets})
        with pytest.raises(PolicyRegistrationError, match=r"already declared by \S*test_policy_framework_review_fixes"):
            register_component_kind("test.widget", foreign, "Plain", "Widgets.")
    finally:
        components._KINDS.pop("test.widget", None)
        params_mod._REF_RESOLVERS.pop("test.widget", None)


_PROBE = """
import json, sys
first = sys.argv[1]
if first:
    __import__(first)
from apps.container_logistics.policies.framework import components
offer = "apps.container_logistics.assignment.policy.offer"
before = offer in sys.modules
legacy = any(m in sys.modules for m in ("apps.container_logistics.policies.dispatch.legacy",
                                         "apps.container_logistics.policies.compat.legacy"))
kinds = list(components.COMPONENT_KINDS)
print(json.dumps({"before": before, "kinds": kinds, "after": offer in sys.modules, "legacy": legacy}))
"""


@pytest.mark.parametrize("first", ["", "apps.container_logistics.assignment.arbitration"])
def test_builtin_kinds_are_found_lazily_in_a_fixed_order_whatever_was_imported_first(first):
    """Importing the framework imports no market module; the first kind lookup loads
    them, and the order is the bootstrap list's, not the import order.

    Verified to have teeth: emptying ``BUILTIN_COMPONENT_MODULES`` made the cold
    probe list no kinds; ordering by declaration only put ``market.arbitration``
    first when arbitration was imported first.
    """
    out = subprocess.run([sys.executable, "-c", _PROBE, first], cwd=REPO, capture_output=True,
                         text=True, check=True, env={"PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"})
    probe = json.loads(out.stdout.strip().splitlines()[-1])
    assert probe["kinds"] == ["market.offer", "market.claim", "market.arbitration"], probe
    assert probe["after"], probe
    if not first:
        assert not probe["before"], "importing framework.components must not import the market"
        assert not probe["legacy"], "the framework must not pull in the legacy translation (compat.legacy)"


def test_the_v1_reservation_is_the_one_allowlist():
    """Message 15 is unchanged and keyed on ``V1_SHARE_TAG_BUILTINS`` only.

    Verified to have teeth: reverting ``_check_share_tag_format`` to a bare
    ``name != 'Partitioned'`` check failed the allowlist-extension assertion (X was
    still refused); a second ``'Partitioned'`` literal in registry.py fails the count.
    """
    with isolated_registry():
        with pytest.raises(PolicyRegistrationError) as exc:
            _register("X", _Empty, share_tag_format="v1")
        assert str(exc.value) == (
            "@policy(slot='planner', name='X'): share_tag_format='v1' is reserved for the "
            "Partitioned built-in (the pre-v2 wire format, kept only for byte identity); new "
            "policies use the default 'audited'.")
    original = registry.V1_SHARE_TAG_BUILTINS
    registry.V1_SHARE_TAG_BUILTINS = original | {("planner", "X")}
    try:
        with isolated_registry():
            assert _register("X", _Empty, share_tag_format="v1")
    finally:
        registry.V1_SHARE_TAG_BUILTINS = original
    literals = [n for n in ast.walk(ast.parse((FRAMEWORK / "registry.py").read_text()))
                if isinstance(n, ast.Constant) and n.value == "Partitioned"]
    assert len(literals) == 1, "the v1 owner must be named in exactly one place"


def test_no_framework_module_mentions_the_dispatch_legacy_module():
    """C3 removed ``framework/resolve.py``'s ``dispatch.legacy`` import (C1 review #5);
    no framework module may import or name it again (AST imports and strings), at its
    old path or at its compat home (``policies/compat/legacy.py``).

    Verified to have teeth: adding ``from apps.container_logistics.policies.dispatch
    import legacy`` inside a function of ``catalog.py`` failed it; so did ``from
    apps.container_logistics.policies.compat import legacy`` there (WS2).
    """
    offenders = []
    for path in sorted(FRAMEWORK.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or "", *(f"{node.module}.{a.name}" for a in node.names)]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names = [node.value]
            if any("policies.dispatch.legacy" in n or "policies.compat.legacy" in n for n in names):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, offenders


# --- #7 -------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [7, 1.0, True, {"x": 1}, ["1"], "", "   "])
def test_a_ref_version_must_be_a_non_blank_string_or_null(bad):
    """Verified to have teeth: making ``version_echo`` return its value unchecked let
    every case through (DID NOT RAISE)."""
    with pytest.raises(PolicyParamsError, match=r"^m\.offer\.version must be a non-empty string or null"):
        _MarketParams.from_mapping({"offer": {"type": "OfferAll", "version": bad}}, where="m for M")
    for ok in (None, "1"):
        assert _MarketParams.from_mapping({"offer": {"type": "OfferAll", "version": ok}},
                                          where="m for M").offer.version == "1"


def test_version_drift_is_logged_nested_and_at_the_top_level(caplog):
    """A compiled version that differs from the registered one still runs the
    registered version, and says so with the greppable prefix.

    Verified to have teeth: removing the ``warn_version_drift`` call from
    ``params._resolve_ref`` failed the nested assertion; removing the loop in
    ``profile.resolve_profile_policies`` failed the top-level one.
    """
    caplog.set_level(logging.WARNING)
    parsed = _MarketParams.from_mapping({"offer": {"type": "OfferAll", "version": "999"}}, where="m for M")
    assert parsed.offer.version == "1"
    drift = [r.getMessage() for r in caplog.records if r.getMessage().startswith(VERSION_DRIFT_PREFIX)]
    assert drift and "m.offer names market.offer 'OfferAll' at version '999'" in drift[0], drift

    caplog.clear()
    compiled = resolve_policies_block({"planner": {"type": "Partitioned"}})["planner"].to_profile()
    resolve_profile_policies({"policies": {"planner": compiled}})
    assert not [r for r in caplog.records if VERSION_DRIFT_PREFIX in r.getMessage()], "no drift, no log"
    resolve_profile_policies({"policies": {"planner": {**compiled, "version": "7"}}})
    drift = [r.getMessage() for r in caplog.records if r.getMessage().startswith(VERSION_DRIFT_PREFIX)]
    assert len(drift) == 1 and "profile.policies.planner names planner 'Partitioned' at version '7'" \
        in drift[0], drift
    with pytest.raises(PolicyParamsError, match=r"^profile\.policies\.planner\.version must be"):
        resolve_profile_policies({"policies": {"planner": {**compiled, "version": 7}}})


# --- #11 ------------------------------------------------------------------------------


def test_the_cli_hint_is_right_for_every_root_and_absent_without_a_cli_spelling():
    """The reviewer's repro: ``profile.policies.planner.params.inner`` gave
    ``--policy-param profile.policies.planner.inner``.

    Verified to have teeth: restoring the first-segment match failed the
    ``profile.policies`` cases; always emitting a hint failed the no-hint case.
    """
    assert _cli_path("profile.policies.planner.params.inner") == "planner.inner"
    assert _cli_path("policies.planner.params.inner.params.offer") == "planner.inner.offer"
    assert _cli_path("policy overrides.planner.params.inner") == "planner.inner"
    assert _cli_path("planner.market.offer") is None and _cli_path("defaults of X.inner") is None
    wrap = _wrapper_params("GreedyNearest")
    with pytest.raises(PolicyParamsError) as exc:
        wrap.from_mapping({"inner": "GreedyNearest"}, where="profile.policies.planner.params for W")
    assert str(exc.value).endswith(
        '{"type": "GreedyNearest"}; on the CLI --policy-param planner.inner=GreedyNearest is accepted.')
    assert "--policy-param profile" not in str(exc.value)
    with pytest.raises(PolicyParamsError) as exc:
        wrap.from_mapping({"inner": "GreedyNearest"}, where="defaults of W")
    assert str(exc.value) == (
        "defaults of W.inner must be an object {type, params?} (got 'GreedyNearest'). "
        'In spec.json write {"type": "GreedyNearest"}.')


# --- #15 ------------------------------------------------------------------------------


def test_instantiate_component_checks_its_params_class_and_version(caplog):
    """Verified to have teeth: removing the ``type(value.params) is not
    target.params_cls`` check built an ``OfferSpare`` from ``OfferAll``'s params;
    dropping ``version_echo`` there let ``version=3`` through."""
    swapped = PolicyRef(type="OfferSpare", params=OfferAllPolicy.Params(), version="1")
    with pytest.raises(PolicyParamsError, match="params are a .*OfferAllPolicy.Params, not OfferSpare"):
        instantiate_component("market.offer", swapped)
    with pytest.raises(PolicyParamsError, match=r"version must be a non-empty string or null"):
        instantiate_component("market.offer", PolicyRef(type="OfferAll", version=3))
    caplog.set_level(logging.WARNING)
    built = instantiate_component("market.offer", PolicyRef(
        type="OfferSpare", params=OfferSparePolicy.Params(), version="9"))
    assert isinstance(built, OfferSparePolicy)
    assert any(r.getMessage().startswith(VERSION_DRIFT_PREFIX) for r in caplog.records)
