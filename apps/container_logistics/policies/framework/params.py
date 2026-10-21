"""Typed, validated policy parameters declared as frozen dataclasses.

A policy author writes::

    @dataclass(frozen=True)
    class Params(PolicyParams):
        dual_cycle_bonus_km: float = param(5.0, ge=0, help="...")

and gets strict parsing (:meth:`PolicyParams.from_mapping`), a plain-dict view
(:meth:`PolicyParams.to_dict`) and a JSON Schema subset for the catalog and the
editor (:meth:`PolicyParams.json_schema`). Plan ``docs/plans/policy_slots_p0_p3.md``
§4 P0.2 and §2.6: no silent coercion, ``bool`` is never a number, non-finite numbers
never cross the boundary, unknown keys are rejected.

Where a malformed declaration is caught: :func:`declared_params` validates the class
itself (dataclass, frozen, every field via ``param()``, supported type, ``choices``
only on ``str``, bounds only on numbers) and raises ``PolicyRegistrationError``. The
registry calls it at ``@policy`` registration, i.e. at import; an unregistered class
is checked on its first ``from_mapping``/``json_schema`` call.

Leaf module: stdlib only (plan §2.7).
"""

from __future__ import annotations

import dataclasses
import logging
import math
import typing
from dataclasses import dataclass
from typing import Any, Mapping, Self

from .errors import PolicyParamsError, PolicyRegistrationError

logger = logging.getLogger(__name__)

__all__ = [
    "param",
    "PolicyParams",
    "ParamInfo",
    "declared_params",
    "SUPPORTED_SCHEMA_KEYWORDS",
]

SUPPORTED_SCHEMA_KEYWORDS = frozenset({
    "type", "properties", "additionalProperties", "default",
    "minimum", "exclusiveMinimum", "maximum", "exclusiveMaximum",
    "enum", "description", "title",
})

_META_KEY = "openride_policy_param"
_JSON_TYPES = {bool: "boolean", int: "integer", float: "number", str: "string"}
_TYPE_WORDS = {bool: "a boolean", int: "an integer", float: "a number", str: "a string"}
# (param() keyword, comparison that must hold, symbol, JSON Schema keyword)
_BOUNDS = (
    ("ge", lambda v, b: v >= b, ">=", "minimum"),
    ("gt", lambda v, b: v > b, ">", "exclusiveMinimum"),
    ("le", lambda v, b: v <= b, "<=", "maximum"),
    ("lt", lambda v, b: v < b, "<", "exclusiveMaximum"),
)


@dataclass(frozen=True)
class _ParamMeta:
    help: str
    ge: float | None
    gt: float | None
    le: float | None
    lt: float | None
    choices: tuple[str, ...] | None


@dataclass(frozen=True)
class ParamInfo:
    """One validated field declaration: name, resolved type, default and constraints."""

    name: str
    type: type
    default: Any
    meta: _ParamMeta


def param(
    default: Any,
    *,
    help: str,
    ge: float | None = None,
    gt: float | None = None,
    le: float | None = None,
    lt: float | None = None,
    choices: tuple[str, ...] | None = None,
) -> Any:
    """Declare one policy parameter; use as a dataclass field default.

    ``help`` is mandatory (it becomes the schema ``description``). ``ge/gt/le/lt``
    are inclusive/exclusive bounds for numeric fields; ``choices`` is an allowed set
    for ``str`` fields. Returns a ``dataclasses.field``. Consistency against the
    annotation is checked later by :func:`declared_params`.
    """
    meta = _ParamMeta(help=help, ge=ge, gt=gt, le=le, lt=lt,
                      choices=tuple(choices) if choices is not None else None)
    return dataclasses.field(default=default, metadata={_META_KEY: meta})


def declared_params(cls: type) -> tuple[ParamInfo, ...]:
    """Return ``cls``'s parameter declarations in field order.

    Raises ``PolicyRegistrationError`` when ``cls`` is not a frozen dataclass subclass
    of :class:`PolicyParams`, or any field is not a well-formed ``param()``.
    """
    owner = cls.__qualname__ if isinstance(cls, type) else repr(cls)
    if not (isinstance(cls, type) and issubclass(cls, PolicyParams)):
        raise PolicyRegistrationError(f"{owner} must subclass PolicyParams.")
    if not dataclasses.is_dataclass(cls) or "__dataclass_fields__" not in vars(cls):
        raise PolicyRegistrationError(
            f"{owner} must be decorated with @dataclass(frozen=True) itself."
        )
    if not cls.__dataclass_params__.frozen:
        raise PolicyRegistrationError(
            f"{owner} must be a frozen dataclass: use @dataclass(frozen=True), "
            "so resolved params cannot change mid-run."
        )
    try:
        hints = typing.get_type_hints(cls)
    except Exception as exc:  # NameError/TypeError from an unresolvable annotation
        raise PolicyRegistrationError(f"{owner}: cannot resolve field annotations ({exc}).") from exc
    return tuple(_field_info(owner, f, hints.get(f.name)) for f in dataclasses.fields(cls))


def _field_info(owner: str, f: dataclasses.Field, hint: Any) -> ParamInfo:
    meta = f.metadata.get(_META_KEY)
    if not isinstance(meta, _ParamMeta):
        raise PolicyRegistrationError(
            f"{owner}.{f.name} is not declared with param(): write "
            f"`{f.name}: <type> = param(<default>, help='...')` so it has a default, "
            "its bounds and a help text for the catalog."
        )
    if hint not in _JSON_TYPES:
        raise PolicyRegistrationError(
            f"{owner}.{f.name}: unsupported type {hint!r}. Supported: bool, int, float, str."
        )
    if not isinstance(meta.help, str) or not meta.help.strip():
        raise PolicyRegistrationError(f"{owner}.{f.name}: param(help=...) must be a non-empty string.")
    _check_constraints(owner, f.name, hint, meta)
    return ParamInfo(name=f.name, type=hint, default=f.default, meta=meta)


def _check_constraints(owner: str, name: str, hint: type, meta: _ParamMeta) -> None:
    numeric = hint in (int, float)
    for key, _ok, _sym, _schema in _BOUNDS:
        bound = getattr(meta, key)
        if bound is None:
            continue
        if not numeric:
            raise PolicyRegistrationError(
                f"{owner}.{name}: bound {key}= is only allowed on int/float params, not {hint.__name__}."
            )
        if isinstance(bound, bool) or not isinstance(bound, (int, float)) or not math.isfinite(bound):
            raise PolicyRegistrationError(f"{owner}.{name}: bound {key}={bound!r} must be a finite number.")
    if meta.choices is not None:
        if hint is not str:
            raise PolicyRegistrationError(
                f"{owner}.{name}: choices= is only allowed on str params, not {hint.__name__}."
            )
        if not meta.choices or not all(isinstance(c, str) for c in meta.choices):
            raise PolicyRegistrationError(f"{owner}.{name}: choices must be a non-empty tuple of strings.")
        if len(set(meta.choices)) != len(meta.choices):
            raise PolicyRegistrationError(f"{owner}.{name}: choices {list(meta.choices)} repeat a value.")


class PolicyParams:
    """Base class for a policy's ``Params``; subclass it with ``@dataclass(frozen=True)``.

    Deliberately not a dataclass itself, so a subclass that forgets ``frozen=True``
    is reported by :func:`declared_params` instead of failing obscurely in Python.
    """

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None, *, where: str) -> Self:
        """Parse ``raw`` strictly into an instance; ``None`` means all defaults.

        ``where`` prefixes every message (e.g. ``policies.dispatch.params for
        GreedyNearest``). Raises ``PolicyParamsError`` for a non-mapping, an unknown
        or non-string key, a wrong type, a non-finite number, a bound or a choice
        violation. An ``int`` is accepted (and converted) for a ``float`` field.
        """
        fields = declared_params(cls)
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise PolicyParamsError(
                f"{where}: params must be a mapping of name to value (got {type(raw).__name__})."
            )
        _reject_unknown_keys(raw, fields, where)
        values = {
            f.name: _validated(f, raw[f.name] if f.name in raw else f.default, where)
            for f in fields
        }
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        """Return the values as a plain dict in declared field order."""
        return {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        """Return the JSON Schema (subset: ``SUPPORTED_SCHEMA_KEYWORDS``) of the params.

        Defaults are the validated defaults, so a float field's int default shows as
        a float. Raises ``PolicyRegistrationError`` / ``PolicyParamsError`` for a
        malformed declaration or defaults failing their own constraints.
        """
        defaults = cls.from_mapping(None, where=f"defaults of {cls.__qualname__}").to_dict()
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {f.name: _property_schema(f, defaults[f.name]) for f in declared_params(cls)},
        }


def _property_schema(f: ParamInfo, default: Any) -> dict[str, Any]:
    prop: dict[str, Any] = {"type": _JSON_TYPES[f.type], "default": default}
    for key, _ok, _sym, schema_key in _BOUNDS:
        bound = getattr(f.meta, key)
        if bound is not None:
            prop[schema_key] = float(bound) if f.type is float else bound
    if f.meta.choices is not None:
        prop["enum"] = list(f.meta.choices)
    prop["description"] = f.meta.help
    return prop


def _reject_unknown_keys(raw: Mapping[Any, Any], fields: tuple[ParamInfo, ...], where: str) -> None:
    declared = {f.name for f in fields}
    for key in raw:
        if not isinstance(key, str):
            raise PolicyParamsError(
                f"{where}: param names must be strings (got {key!r}, a {type(key).__name__})."
            )
    unknown = sorted(k for k in raw if k not in declared)
    if not unknown:
        return
    listed = ", ".join(sorted(declared)) or "(none)"
    if len(unknown) == 1:
        raise PolicyParamsError(f"{where}: unknown key {unknown[0]!r}. Declared params: {listed}.")
    names = ", ".join(repr(k) for k in unknown)
    raise PolicyParamsError(f"{where}: unknown keys {names}. Declared params: {listed}.")


def _validated(f: ParamInfo, value: Any, where: str) -> Any:
    value = _typed(f, value, where)
    for key, ok, symbol, _schema in _BOUNDS:
        bound = getattr(f.meta, key)
        if bound is not None and not ok(value, bound):
            raise PolicyParamsError(f"{where}: {f.name!r} must be {symbol} {bound} (got {value!r}).")
    if f.meta.choices is not None and value not in f.meta.choices:
        raise PolicyParamsError(
            f"{where}: {f.name!r} must be one of {sorted(f.meta.choices)} (got {value!r})."
        )
    return value


def _typed(f: ParamInfo, value: Any, where: str) -> Any:
    """Check ``value`` against ``f.type`` with no coercion except int -> float."""
    if f.type is bool:
        ok = isinstance(value, bool)
    elif f.type is str:
        ok = isinstance(value, str)
    elif f.type is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    else:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    if not ok:
        hint = " Values are not coerced from strings." if isinstance(value, str) else ""
        raise PolicyParamsError(
            f"{where}: {f.name!r} must be {_TYPE_WORDS[f.type]} "
            f"(got {value!r}, a {type(value).__name__}).{hint}"
        )
    if f.type is float:
        try:
            converted = float(value)
        except OverflowError:  # an int too large for a float
            converted = math.inf
        if not math.isfinite(converted):
            raise PolicyParamsError(f"{where}: {f.name!r} must be a finite number (got {value!r}).")
        value = converted
    return value
