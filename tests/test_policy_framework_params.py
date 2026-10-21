"""Locks the strict parsing contract of ``PolicyParams`` (plan §4 P0.2, §2.6).

Params are the one boundary where scenario JSON becomes policy behaviour, so they must
never coerce: no truthy strings, no ``bool`` as a number, no ``nan``, no unknown keys
silently ignored. The exact message formats are part of the contract because the
compile step surfaces them verbatim to the scenario editor.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

import pytest

from apps.container_logistics.policies.framework import (
    SUPPORTED_SCHEMA_KEYWORDS,
    PolicyParams,
    PolicyParamsError,
    PolicyRegistrationError,
    param,
)

WHERE = "policies.dispatch.params for GreedyNearest"


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
