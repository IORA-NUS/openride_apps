"""Locks the strict parsing contract of ``PolicyParams`` (plan §4 P0.2, §2.6).

Params are the one boundary where scenario JSON becomes policy behaviour, so they must
never coerce: no truthy strings, no ``bool`` as a number, no ``nan``, no unknown keys
silently ignored. The exact message formats are part of the contract because the
compile step surfaces them verbatim to the scenario editor. Round 2 adds policy
references (``PolicyRef``, resolved recursively against the live registries) and
nullable numbers; their exact §3.7 messages are locked here too.
"""

from __future__ import annotations

import json
import math
from contextlib import contextmanager
from dataclasses import dataclass

import pytest

from apps.container_logistics.policies.dispatch.base import DispatchPolicy
from apps.container_logistics.policies.compat.legacy_context import Plan
from apps.container_logistics.policies.framework import (
    SUPPORTED_SCHEMA_KEYWORDS,
    PolicyParams,
    PolicyParamsError,
    PolicyRef,
    PolicyRegistrationError,
    UnknownPolicyError,
    param,
    policy,
    ref,
)
from apps.container_logistics.policies.framework import registry
from apps.container_logistics.policies.framework.params import check_defaults
from apps.container_logistics.policies.framework.registry import isolated_registry
from tests.policy_registry_names import component_names, dispatch_names, leaf_names

WHERE = "policies.planner.params for GreedyNearest"


@dataclass(frozen=True)
class _GreedyLike(PolicyParams):
    candidates: str = param("spatial", choices=("spatial", "sampled"), help="Candidate generation.")
    dual_cycle_bonus_km: float = param(5.0, ge=0, help="Bonus for a dual cycle.")
    dual_cycle_radius_km: float = param(0.5, ge=0, help="Dual-cycle radius.")


@dataclass(frozen=True)
class _Mixed(PolicyParams):
    enabled: bool = param(True, help="On/off.")
    rounds: int = param(3, ge=1, le=10, help="How many rounds.")
    share: float = param(0.5, gt=0, lt=1, help="A fraction.")


def _parse(cls, raw):
    return cls.from_mapping(raw, where=WHERE)


def test_defaults_fill_missing_keys():
    """Missing keys and ``None`` both take the declared defaults.

    Verified to have teeth: making ``from_mapping`` use ``raw.get(name)`` (no
    default) for absent keys made both calls raise PolicyParamsError.
    """
    assert _parse(_GreedyLike, None).to_dict() == {
        "candidates": "spatial", "dual_cycle_bonus_km": 5.0, "dual_cycle_radius_km": 0.5,
    }
    partial = _parse(_GreedyLike, {"dual_cycle_bonus_km": 2.0})
    assert partial.dual_cycle_bonus_km == 2.0
    assert partial.candidates == "spatial", "an absent key must take its default"


def test_unknown_key_rejected_with_declared_list():
    """An unknown key fails loud, naming it and every declared param.

    Verified to have teeth: removing the ``_reject_unknown_keys`` call made the typo
    silently ignored and this test failed with DID NOT RAISE.
    """
    with pytest.raises(PolicyParamsError) as exc:
        _parse(_GreedyLike, {"bonus_km": 1.0})
    assert str(exc.value) == (
        f"{WHERE}: unknown key 'bonus_km'. "
        "Declared params: candidates, dual_cycle_bonus_km, dual_cycle_radius_km."
    )


def test_non_mapping_params_rejected_naming_the_type():
    """A list (or any non-mapping) is an error that names its type.

    Verified to have teeth: disabling the ``Mapping`` check failed the message match.
    """
    with pytest.raises(PolicyParamsError, match=r"must be a mapping .*\(got list\)"):
        _parse(_GreedyLike, [("candidates", "spatial")])


@pytest.mark.parametrize("field, value", [
    ("dual_cycle_bonus_km", True),
    ("dual_cycle_bonus_km", False),
])
def test_bool_rejected_for_numbers(field, value):
    """``bool`` is an ``int`` subclass in Python but never a number here (float field).

    Verified to have teeth: dropping ``not isinstance(value, bool)`` from the float
    branch of ``_typed`` accepted ``True`` as 1.0 and this test failed.
    """
    with pytest.raises(PolicyParamsError, match="must be a number"):
        _parse(_GreedyLike, {field: value})


def test_bool_rejected_for_int_fields():
    """Same rule for ``int`` fields.

    Verified to have teeth: dropping the bool exclusion from the int branch made
    ``rounds=True`` parse as 1.
    """
    with pytest.raises(PolicyParamsError, match="'rounds' must be an integer"):
        _parse(_Mixed, {"rounds": True})


def test_strings_are_not_coerced():
    """A numeric-looking or truthy string never becomes a number or bool.

    Verified to have teeth: coercing float-field strings with ``float(value)`` before
    the type check made the string cases stop raising PolicyParamsError.
    """
    with pytest.raises(PolicyParamsError) as exc:
        _parse(_GreedyLike, {"dual_cycle_bonus_km": "five"})
    assert str(exc.value) == (
        f"{WHERE}: 'dual_cycle_bonus_km' must be a number (got 'five', a str). "
        "Values are not coerced from strings."
    )
    with pytest.raises(PolicyParamsError, match="not coerced from strings"):
        _parse(_GreedyLike, {"dual_cycle_bonus_km": "5"})
    with pytest.raises(PolicyParamsError, match="must be a boolean"):
        _parse(_Mixed, {"enabled": "true"})
    with pytest.raises(PolicyParamsError, match="must be a string"):
        _parse(_GreedyLike, {"candidates": 1})


def test_int_accepted_for_float():
    """An ``int`` is accepted for a ``float`` field and stored as a float.

    Verified to have teeth: removing the ``float(value)`` conversion left ``2`` as an
    int and the type assertion failed.
    """
    parsed = _parse(_GreedyLike, {"dual_cycle_bonus_km": 2})
    assert parsed.dual_cycle_bonus_km == 2.0
    assert type(parsed.dual_cycle_bonus_km) is float, "int must be converted to float"
    with pytest.raises(PolicyParamsError, match="must be an integer"):
        _parse(_Mixed, {"rounds": 3.0})


@pytest.mark.parametrize("raw, message", [
    ({"dual_cycle_bonus_km": -1}, f"{WHERE}: 'dual_cycle_bonus_km' must be >= 0 (got -1.0)."),
    ({"rounds": 0}, f"{WHERE}: 'rounds' must be >= 1 (got 0)."),
    ({"rounds": 11}, f"{WHERE}: 'rounds' must be <= 10 (got 11)."),
    ({"share": 0}, f"{WHERE}: 'share' must be > 0 (got 0.0)."),
    ({"share": 1.0}, f"{WHERE}: 'share' must be < 1 (got 1.0)."),
])
def test_ge_gt_le_lt_bounds(raw, message):
    """Each bound kind rejects its first out-of-range value, with the exact message.

    Verified to have teeth: disabling the bound check made the cases fail with
    DID NOT RAISE.
    """
    cls = _GreedyLike if "dual_cycle_bonus_km" in raw else _Mixed
    with pytest.raises(PolicyParamsError) as exc:
        _parse(cls, raw)
    assert str(exc.value) == message


def test_bounds_are_inclusive_or_exclusive_as_declared():
    """The edges themselves: ``ge``/``le`` include the bound.

    Verified to have teeth: swapping ``>=`` for ``>`` in the ``ge`` comparator failed it.
    """
    assert _parse(_GreedyLike, {"dual_cycle_bonus_km": 0}).dual_cycle_bonus_km == 0.0
    assert _parse(_Mixed, {"rounds": 1}).rounds == 1
    assert _parse(_Mixed, {"rounds": 10}).rounds == 10


def test_choices_enforced():
    """A value outside ``choices`` is rejected, listing the sorted allowed set.

    Verified to have teeth: deleting the choices check let 'near' through.
    """
    with pytest.raises(PolicyParamsError) as exc:
        _parse(_GreedyLike, {"candidates": "near"})
    assert str(exc.value) == f"{WHERE}: 'candidates' must be one of ['sampled', 'spatial'] (got 'near')."
    assert _parse(_GreedyLike, {"candidates": "sampled"}).candidates == "sampled"


def test_choices_on_a_non_string_field_is_a_declaration_error():
    """``choices`` only makes sense on ``str``; the class is rejected when first used.

    Verified to have teeth: disabling the ``hint is not str`` check let it through.
    """
    @dataclass(frozen=True)
    class Bad(PolicyParams):
        k: int = param(1, choices=("1", "2"), help="x")

    with pytest.raises(PolicyRegistrationError, match="choices= is only allowed on str"):
        Bad.json_schema()


def test_field_without_param_is_a_declaration_error():
    """Every field must go through ``param()``: help text and bounds are mandatory metadata.

    Verified to have teeth: substituting a dummy meta for a missing one let it through.
    """
    @dataclass(frozen=True)
    class Bad(PolicyParams):
        k: float = 1.0

    with pytest.raises(PolicyRegistrationError, match=r"Bad\.k is not declared with param\(\)"):
        Bad.from_mapping(None, where=WHERE)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, 10 ** 400])
def test_non_finite_rejected(value):
    """``nan``/``inf`` (and ints too large for a float) never cross the boundary.

    Verified to have teeth: disabling the ``math.isfinite`` check failed this test
    (``nan`` slipped through the bound check, since every comparison with nan is False).
    """
    with pytest.raises(PolicyParamsError, match="must be a finite number"):
        _parse(_GreedyLike, {"dual_cycle_radius_km": value})


def _schema_keys(node, found):
    if isinstance(node, dict):
        for key, child in node.items():
            found.add(key)
            if key == "properties":
                for prop in child.values():
                    _schema_keys(prop, found)
    return found


def test_json_schema_shape_and_only_supported_keywords():
    """The schema is the documented subset, with JSON types and validated defaults.

    Verified to have teeth: emitting ``"ge"`` instead of ``"minimum"`` failed the
    supported-keyword assertion; emitting the raw declared default (bypassing
    ``from_mapping``) left the int 5 on a float field and failed the type assertion.
    """
    schema = _Mixed.json_schema()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert list(schema["properties"]) == ["enabled", "rounds", "share"], "declared order"
    assert schema["properties"]["enabled"] == {"type": "boolean", "default": True, "description": "On/off."}
    assert schema["properties"]["rounds"] == {
        "type": "integer", "default": 3, "minimum": 1, "maximum": 10, "description": "How many rounds.",
    }
    assert schema["properties"]["share"] == {
        "type": "number", "default": 0.5, "exclusiveMinimum": 0.0, "exclusiveMaximum": 1.0,
        "description": "A fraction.",
    }
    @dataclass(frozen=True)
    class IntDefault(PolicyParams):
        x: float = param(5, ge=0, help="An int default on a float field.")

    x_prop = IntDefault.json_schema()["properties"]["x"]
    assert x_prop["default"] == 5.0 and type(x_prop["default"]) is float, (
        "the schema default must be the validated (converted) default"
    )
    assert type(x_prop["minimum"]) is float
    greedy = _GreedyLike.json_schema()["properties"]
    assert greedy["candidates"]["enum"] == ["spatial", "sampled"]
    assert greedy["candidates"]["type"] == "string"
    used = _schema_keys(_GreedyLike.json_schema(), set()) | _schema_keys(schema, set())
    assert used <= SUPPORTED_SCHEMA_KEYWORDS, f"unsupported keywords: {used - SUPPORTED_SCHEMA_KEYWORDS}"
    json.dumps(schema)


def test_to_dict_roundtrips_through_from_mapping():
    """``to_dict`` -> ``from_mapping`` is the identity, and the dict is plain and ordered.

    Verified to have teeth: making ``to_dict`` omit fields equal to their default
    failed the key-order assertion (``rounds`` keeps its default here).
    """
    original = _parse(_Mixed, {"enabled": False, "share": 0.25})
    as_dict = original.to_dict()
    assert type(as_dict) is dict
    assert list(as_dict) == ["enabled", "rounds", "share"], "every field, in declared order"
    assert _Mixed.from_mapping(as_dict, where=WHERE) == original


# --- round 2: refs and nullable numbers (docs/plans/policy_slots_round2_whole_tick.md §3.2) ---

COMPOSITE_WHERE = "policies.planner.params for Partitioned"


@dataclass(frozen=True)
class _CompositeLike(PolicyParams):
    inner: PolicyRef = param(ref("RandomAssignment"), kind="planner", leaf_only=True, help="Inner solver.")
    offer: PolicyRef = param(ref("OfferAll"), kind="market.offer", help="Offer component.")


@dataclass(frozen=True)
class _Nullable(PolicyParams):
    keep_below_km: float | None = param(None, ge=0, help="None = no threshold.")
    rounds: int | None = param(3, ge=1, help="None = unbounded.")


def _composite_parse(raw):
    return _CompositeLike.from_mapping(raw, where=COMPOSITE_WHERE)


@contextmanager
def _named_dispatch_registry():
    """Exactly the four round-2 built-in NAMES, as dummies (two composites, two leaves)."""
    @dataclass(frozen=True)
    class LeafParams(PolicyParams):
        candidates: str = param("spatial", choices=("spatial", "sampled"), help="Candidates.")

    class _Base(DispatchPolicy):
        def decide(self, ctx):
            return Plan(chains={})

    with isolated_registry():
        registry._REGISTRY.clear()
        for name, params_cls, model in (("GreedyNearest", LeafParams, "central"),
                                        ("RandomAssignment", LeafParams, "central"),
                                        ("Partitioned", _CompositeLike, "per_group"),
                                        ("PooledMarket", _CompositeLike, "per_company")):
            policy(slot="planner", name=name, version="1", description="x", badge=model)(
                type(name, (_Base,), {"Params": params_cls}))
        yield


def test_ref_param_accepts_registered_target_and_fills_its_defaults():
    """A ref resolves against the live registry, validates its params and fills defaults.

    Verified to have teeth: keeping the raw ``params`` mapping in ``_resolve_ref``
    (no ``from_mapping``) failed it; dropping ``version=`` from the returned ref
    failed the version assertion.
    """
    from apps.container_logistics.assignment.policy.offer import OfferAllPolicy
    from apps.container_logistics.assignment.solver import GreedyNearestSolver

    parsed = _composite_parse({"inner": {"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": 1}}})
    assert parsed.inner.type == "GreedyNearest" and parsed.inner.version == "1"
    assert isinstance(parsed.inner.params, GreedyNearestSolver.Params)
    assert parsed.inner.params.to_dict() == {
        "candidates": "spatial", "dual_cycle_bonus_km": 1.0, "dual_cycle_radius_km": 0.5,
    }, "the target's undeclared-by-the-caller params take the target's defaults"
    assert parsed.offer == PolicyRef(type="OfferAll", params=OfferAllPolicy.Params(), version="1")
    default = _composite_parse(None)
    assert default.inner.type == "RandomAssignment"
    assert default.inner.params.to_dict() == {"candidates": "sampled"}, "lazy default resolution"


def test_ref_param_rejects_unknown_target_listing_registered():
    """Messages 1 and 3 (round-2 §3.7), exactly.

    Verified to have teeth: disabling the ``name not in names`` check in
    ``components._dispatch_target`` turned message 1 into the registry's own message
    (no path); dropping the ``{where}: `` prefix from the component message failed
    message 3.
    """
    with _named_dispatch_registry():
        with pytest.raises(UnknownPolicyError) as exc:
            _composite_parse({"inner": {"type": "GreedyNeerest"}})
        names = dispatch_names()  # read inside: the registry the message was built from (MINOR-7)
    assert str(exc.value) == (
        "policies.planner.params.inner: unknown planner policy 'GreedyNeerest'. Registered "
        f"planner policies: {names}. "
        "(List them: openride policy list planner)"
    )
    with pytest.raises(UnknownPolicyError) as exc:
        _composite_parse({"offer": {"type": "OfferAl"}})
    assert str(exc.value) == (
        "policies.planner.params.offer: unknown market.offer component 'OfferAl'. Registered: "
        f"{component_names('market.offer')}. (List them: openride policy list market.offer)"
    )


def test_ref_param_rejects_composite_where_leaf_only():
    """Message 2: a ``leaf_only`` ref refuses a target that has an ``inner`` of its own.

    Verified to have teeth: removing the ``leaf_only and target.is_composite`` raise in
    ``components.ref_target`` let PooledMarket in as an inner.
    """
    with _named_dispatch_registry():
        with pytest.raises(PolicyParamsError) as exc:
            _composite_parse({"inner": {"type": "PooledMarket"}})
        assert str(exc.value) == (
            "policies.planner.params.inner: 'PooledMarket' decomposes the tick itself (it has an "
            f"'inner' of its own); an inner must be a per-scope solver: {leaf_names()}."
        )
        assert _composite_parse({"inner": {"type": "GreedyNearest"}}).inner.type == "GreedyNearest"


def test_ref_value_must_be_object_with_type_and_params_only():
    """Message 4 for a bare name; unknown keys, a missing type or non-object params fail.

    Verified to have teeth: accepting a bare string as ``{"type": value}`` in
    ``_ref_entry`` failed the message-4 case; dropping the unknown-key check let
    ``parms`` through silently.
    """
    with pytest.raises(PolicyParamsError) as exc:
        _composite_parse({"inner": "GreedyNearest"})
    assert str(exc.value) == (
        "policies.planner.params.inner must be an object {type, params?} (got 'GreedyNearest'). "
        'In spec.json write {"type": "GreedyNearest"}; on the CLI --policy-param '
        "planner.inner=GreedyNearest is accepted."
    )
    with pytest.raises(PolicyParamsError, match=r"unknown key\(s\) 'parms' in a policy reference"):
        _composite_parse({"inner": {"type": "GreedyNearest", "parms": {}}})
    with pytest.raises(PolicyParamsError, match=r"policies\.planner\.params\.inner\.type must name"):
        _composite_parse({"inner": {"params": {}}})
    with pytest.raises(PolicyParamsError, match=r"policies\.planner\.params\.inner\.params must be an object"):
        _composite_parse({"inner": {"type": "GreedyNearest", "params": [1]}})
    echoed = _composite_parse({"inner": {"type": "GreedyNearest", "version": "9", "params": {}}})
    assert echoed.inner.version == "1", "a stamped version is ignored on read"


def test_nested_param_error_names_full_path():
    """A nested params error names the full dotted path and the target.

    Verified to have teeth: passing the bare ref path as the target's ``where``
    (instead of ``<path>.params for <name>``) failed the first message.
    """
    with pytest.raises(PolicyParamsError) as exc:
        _composite_parse({"inner": {"type": "GreedyNearest", "params": {"dual_cycle_bonus_km": -1}}})
    assert str(exc.value) == (
        "policies.planner.params.inner.params for GreedyNearest: 'dual_cycle_bonus_km' "
        "must be >= 0 (got -1.0)."
    )
    with pytest.raises(PolicyParamsError) as exc:
        _composite_parse({"offer": {"type": "OfferSpare", "params": {"keep_below": 3}}})
    assert str(exc.value) == (
        "policies.planner.params.offer.params for OfferSpare: unknown key 'keep_below'. "
        "Declared params: keep_below_km, only_when_short."
    )


def test_nullable_number_accepts_none_and_bounds_apply_to_numbers():
    """``float | None`` / ``int | None``: ``None`` is unset; numbers keep every rule.

    Verified to have teeth: dropping the ``value is None and f.nullable`` early return
    in ``_validated`` made the all-defaults parse raise PolicyParamsError.
    """
    assert _Nullable.from_mapping(None, where=WHERE).to_dict() == {"keep_below_km": None, "rounds": 3}
    parsed = _Nullable.from_mapping({"keep_below_km": 2, "rounds": None}, where=WHERE)
    assert parsed.keep_below_km == 2.0 and type(parsed.keep_below_km) is float and parsed.rounds is None
    for raw, message in (({"keep_below_km": -1}, "'keep_below_km' must be >= 0"),
                         ({"rounds": 0}, "'rounds' must be >= 1"),
                         ({"keep_below_km": True}, "must be a number or null"),
                         ({"keep_below_km": "3"}, "not coerced from strings"),
                         ({"keep_below_km": math.inf}, "must be a finite number"),
                         ({"rounds": 2.0}, "must be an integer or null")):
        with pytest.raises(PolicyParamsError, match=message):
            _Nullable.from_mapping(raw, where=WHERE)

    @dataclass(frozen=True)
    class NullableStr(PolicyParams):
        name: str | None = param(None, help="x")

    with pytest.raises(PolicyRegistrationError, match="unsupported type"):
        NullableStr.json_schema()


def test_json_schema_for_ref_and_nullable_uses_only_supported_keywords():
    """Refs and nullable numbers render with the round-2 keywords, all supported.

    Verified to have teeth: removing ``x-policy-ref`` from ``SUPPORTED_SCHEMA_KEYWORDS``
    failed the subset assertion; rendering a nullable type as plain ``"number"``
    failed the nullable equality.
    """
    schema = _CompositeLike.json_schema()
    assert schema["properties"]["inner"] == {
        "type": "object", "x-policy-ref": "planner", "x-leaf-only": True,
        "default": {"type": "RandomAssignment", "params": {"candidates": "sampled"}},
        "description": "Inner solver.",
    }
    assert schema["properties"]["offer"]["x-leaf-only"] is False
    nullable = _Nullable.json_schema()["properties"]
    assert nullable["keep_below_km"] == {
        "type": ["number", "null"], "default": None, "minimum": 0.0, "description": "None = no threshold.",
    }
    assert nullable["rounds"]["type"] == ["integer", "null"]
    used = _schema_keys(schema, set()) | _schema_keys(_Nullable.json_schema(), set())
    assert {"x-policy-ref", "x-leaf-only"} <= used
    assert used <= SUPPORTED_SCHEMA_KEYWORDS, f"unsupported keywords: {used - SUPPORTED_SCHEMA_KEYWORDS}"
    json.dumps(schema)


def test_to_dict_roundtrips_nested_refs():
    """``to_dict`` nests refs as ``{type, params}``; ``to_profile_dict`` adds ``version``;
    both parse back to an equal instance. An instance built without ``from_mapping``
    renders its default ref resolved.

    Verified to have teeth: rendering a ref as ``value.type`` (flat) in ``_plain``
    failed the nested equality.
    """
    parsed = _composite_parse({"inner": {"type": "GreedyNearest", "params": {"candidates": "sampled"}},
                               "offer": {"type": "OfferSpare", "params": {"keep_below_km": 3}}})
    as_dict = parsed.to_dict()
    assert as_dict == {
        "inner": {"type": "GreedyNearest", "params": {
            "candidates": "sampled", "dual_cycle_bonus_km": 5.0, "dual_cycle_radius_km": 0.5}},
        "offer": {"type": "OfferSpare", "params": {"keep_below_km": 3.0, "only_when_short": True}},
    }
    assert _composite_parse(as_dict) == parsed
    profile = parsed.to_profile_dict()
    assert profile["inner"]["version"] == "1" and list(profile["inner"]) == ["type", "version", "params"]
    assert _composite_parse(profile) == parsed
    assert json.loads(json.dumps(profile)) == profile
    assert _CompositeLike().to_dict()["inner"] == {"type": "RandomAssignment", "params": {"candidates": "sampled"}}


def test_ref_declaration_errors_are_registration_errors():
    """A ref needs a known ``kind`` and a ``ref(...)`` default; ``kind`` only on refs.

    Verified to have teeth: not calling ``_check_ref_declaration`` from ``_field_info``
    let the kind-less ref through (DID NOT RAISE).
    """
    cases = []

    @dataclass(frozen=True)
    class NoKind(PolicyParams):
        inner: PolicyRef = param(ref("GreedyNearest"), help="x")
    cases.append((NoKind, "needs kind= one of market.arbitration, market.claim, market.offer, planner"))

    @dataclass(frozen=True)
    class BareDefault(PolicyParams):
        inner: PolicyRef = param("GreedyNearest", kind="planner", help="x")
    cases.append((BareDefault, r"must be ref\('<name>'\)"))

    @dataclass(frozen=True)
    class KindOnFloat(PolicyParams):
        k: float = param(1.0, kind="planner", help="x")
    cases.append((KindOnFloat, "only allowed on PolicyRef params"))

    for cls, message in cases:
        with pytest.raises(PolicyRegistrationError, match=message):
            check_defaults(cls, where="t")
