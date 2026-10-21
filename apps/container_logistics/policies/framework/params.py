"""Typed, validated policy parameters declared as frozen dataclasses.

A policy author writes::

    @dataclass(frozen=True)
    class Params(PolicyParams):
        dual_cycle_bonus_km: float = param(5.0, ge=0, help="...")
        keep_below_km: float | None = param(None, ge=0, help="... None = no threshold")
        inner: PolicyRef = param(ref("RandomAssignment"), kind="dispatch", leaf_only=True,
                                 help="...")

and gets strict parsing (:meth:`PolicyParams.from_mapping`), a plain-dict view
(:meth:`PolicyParams.to_dict`, refs nested as ``{"type", "params"}``) and a JSON Schema
subset for the catalog and the editor (:meth:`PolicyParams.json_schema`). Plan
``docs/plans/policy_slots_p0_p3.md`` §4 P0.2 and §2.6, round 2
``docs/plans/policy_slots_round2_whole_tick.md`` §3.2: no silent coercion, ``bool`` is
never a number, non-finite numbers never cross the boundary, unknown keys are rejected.

Supported field types: ``bool``, ``int``, ``float``, ``str``, the nullable numbers
``int | None`` / ``float | None`` (``None`` means unset), and :class:`PolicyRef` (a
reference to another policy or component of a ``kind``, validated recursively).

Where a malformed declaration is caught: :func:`declared_params` validates the class
itself (dataclass, frozen, every field via ``param()``, supported type, ``choices``
only on ``str``, bounds only on numbers, ``kind`` exactly on refs) and raises
``PolicyRegistrationError``. The registry calls it, and :func:`check_defaults`, at
``@policy`` registration, i.e. at import. A ref DEFAULT is checked for shape only at
registration (its target may register later in discovery); it is resolved lazily, on
the first ``from_mapping``/``json_schema``/``to_dict`` that needs it.

Refs are resolved through the hook :data:`_REF_RESOLVERS` (kind -> resolver), filled
by ``framework/components.py`` at import, so this module never imports the registry.

Leaf module: stdlib only (plan §2.7).
"""

from __future__ import annotations

import dataclasses
import logging
import math
import types
import typing
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Self

from .errors import PolicyParamsError, PolicyRegistrationError

logger = logging.getLogger(__name__)

__all__ = [
    "param",
    "ref",
    "PolicyRef",
    "PolicyParams",
    "ParamInfo",
    "declared_params",
    "check_defaults",
    "SUPPORTED_SCHEMA_KEYWORDS",
    "REF_ENTRY_KEYS",
]

SUPPORTED_SCHEMA_KEYWORDS = frozenset({
    "type", "properties", "additionalProperties", "default",
    "minimum", "exclusiveMinimum", "maximum", "exclusiveMaximum",
    "enum", "description", "title", "x-policy-ref", "x-leaf-only",
})

#: The keys a ref VALUE may carry. ``version`` is a compile/stamp echo, ignored on read.
REF_ENTRY_KEYS = ("type", "params", "version")

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
# Path segments dropped when a params path is spelled as a CLI --policy-param path.
_NON_CLI_ROOTS = ("policies", "policy overrides", "profile.policies")

#: kind -> resolver ``(name, *, where, leaf_only) -> target``. The target exposes
#: ``name``, ``version`` and ``params_cls``; the resolver raises a ``PolicyError`` for
#: an unknown or (``leaf_only``) composite target. Filled by ``framework/components.py``.
_REF_RESOLVERS: dict[str, Callable[..., Any]] = {}


@dataclass(frozen=True)
class PolicyRef:
    """A reference to another policy (or market component) of some kind.

    ``params`` is ``None`` until resolved (a declared default made with :func:`ref`);
    :meth:`PolicyParams.from_mapping` always returns refs with validated ``params``
    and the target's registered ``version``.
    """

    type: str
    params: "PolicyParams | None" = None
    version: str | None = None


def ref(type_name: str) -> PolicyRef:
    """An unresolved reference to ``type_name``: the default of a ref param."""
    return PolicyRef(type=type_name)


@dataclass(frozen=True)
class _ParamMeta:
    help: str
    ge: float | None
    gt: float | None
    le: float | None
    lt: float | None
    choices: tuple[str, ...] | None
    kind: str | None = None
    leaf_only: bool = False


@dataclass(frozen=True)
class ParamInfo:
    """One validated field declaration: name, base type, default and constraints.

    ``type`` is the base type (``bool``/``int``/``float``/``str``/:class:`PolicyRef`);
    ``nullable`` is true for ``int | None`` / ``float | None``.
    """

    name: str
    type: type
    default: Any
    meta: _ParamMeta
    nullable: bool = False

    @property
    def is_ref(self) -> bool:
        return self.type is PolicyRef


def param(
    default: Any,
    *,
    help: str,
    ge: float | None = None,
    gt: float | None = None,
    le: float | None = None,
    lt: float | None = None,
    choices: tuple[str, ...] | None = None,
    kind: str | None = None,
    leaf_only: bool = False,
) -> Any:
    """Declare one policy parameter; use as a dataclass field default.

    ``help`` is mandatory (it becomes the schema ``description``). ``ge/gt/le/lt``
    are inclusive/exclusive bounds for numeric fields; ``choices`` is an allowed set
    for ``str`` fields. ``kind`` (required on, and only allowed on, a ``PolicyRef``
    field) names what the ref points at: ``dispatch`` or a component kind such as
    ``market.offer``; ``leaf_only`` refuses a composite target (one with a dispatch
    ref of its own). Returns a ``dataclasses.field``. Consistency against the
    annotation is checked later by :func:`declared_params`.
    """
    meta = _ParamMeta(help=help, ge=ge, gt=gt, le=le, lt=lt,
                      choices=tuple(choices) if choices is not None else None,
                      kind=kind, leaf_only=leaf_only)
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


def _base_type(hint: Any) -> tuple[type | None, bool]:
    """``(base type, nullable)`` for a supported annotation, else ``(None, False)``."""
    if hint in _JSON_TYPES or hint is PolicyRef:
        return hint, False
    if typing.get_origin(hint) in (typing.Union, types.UnionType):
        args = set(typing.get_args(hint))
        if len(args) == 2 and type(None) in args:
            (base,) = args - {type(None)}
            if base in (int, float):
                return base, True
    return None, False


def _field_info(owner: str, f: dataclasses.Field, hint: Any) -> ParamInfo:
    meta = f.metadata.get(_META_KEY)
    if not isinstance(meta, _ParamMeta):
        raise PolicyRegistrationError(
            f"{owner}.{f.name} is not declared with param(): write "
            f"`{f.name}: <type> = param(<default>, help='...')` so it has a default, "
            "its bounds and a help text for the catalog."
        )
    base, nullable = _base_type(hint)
    if base is None:
        raise PolicyRegistrationError(
            f"{owner}.{f.name}: unsupported type {hint!r}. Supported: bool, int, float, str, "
            "int | None, float | None, PolicyRef."
        )
    if not isinstance(meta.help, str) or not meta.help.strip():
        raise PolicyRegistrationError(f"{owner}.{f.name}: param(help=...) must be a non-empty string.")
    _check_constraints(owner, f.name, base, meta)
    if base is PolicyRef:
        _check_ref_declaration(owner, f.name, f.default, meta)
    return ParamInfo(name=f.name, type=base, default=f.default, meta=meta, nullable=nullable)


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
    if hint is not PolicyRef and (meta.kind is not None or meta.leaf_only):
        raise PolicyRegistrationError(
            f"{owner}.{name}: kind=/leaf_only= are only allowed on PolicyRef params, not {hint.__name__}."
        )


def _check_ref_declaration(owner: str, name: str, default: Any, meta: _ParamMeta) -> None:
    """Shape-only check of a ref field: its kind is known, its default is a ``ref(...)``."""
    if meta.kind not in _REF_RESOLVERS:
        known = ", ".join(sorted(_REF_RESOLVERS)) or "(none)"
        raise PolicyRegistrationError(
            f"{owner}.{name}: a PolicyRef param needs kind= one of {known} (got {meta.kind!r})."
        )
    if not isinstance(meta.leaf_only, bool):
        raise PolicyRegistrationError(f"{owner}.{name}: leaf_only must be True or False.")
    if not (isinstance(default, PolicyRef) and isinstance(default.type, str) and default.type.strip()):
        raise PolicyRegistrationError(
            f"{owner}.{name}: the default of a PolicyRef param must be ref('<name>') (got {default!r})."
        )


def check_defaults(cls: type, *, where: str) -> None:
    """Validate ``cls``'s declaration and every non-ref default, without resolving refs.

    The registration-time check: a ref default's target may register later in
    discovery, so refs are checked for shape only (by :func:`declared_params`).
    Raises ``PolicyRegistrationError`` / ``PolicyParamsError``.
    """
    for info in declared_params(cls):
        if not info.is_ref:
            _validated(info, info.default, where, where)


class PolicyParams:
    """Base class for a policy's ``Params``; subclass it with ``@dataclass(frozen=True)``.

    Deliberately not a dataclass itself, so a subclass that forgets ``frozen=True``
    is reported by :func:`declared_params` instead of failing obscurely in Python.
    """

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None, *, where: str, path: str | None = None) -> Self:
        """Parse ``raw`` strictly into an instance; ``None`` means all defaults.

        ``where`` prefixes every message (e.g. ``policies.dispatch.params for
        GreedyNearest``). ``path`` is the dotted location of this params object, used
        to name a nested ref (``<path>.inner``); it defaults to ``where`` up to `` for ``.
        Raises ``PolicyParamsError`` for a non-mapping, an unknown or non-string key, a
        wrong type, a non-finite number, a bound or a choice violation, a malformed
        ref value; ``UnknownPolicyError`` for an unknown ref target. An ``int`` is
        accepted (and converted) for a ``float`` field. Refs are resolved recursively.
        """
        fields = declared_params(cls)
        if path is None:
            path = where.partition(" for ")[0]
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise PolicyParamsError(
                f"{where}: params must be a mapping of name to value (got {type(raw).__name__})."
            )
        _reject_unknown_keys(raw, fields, where)
        values = {
            f.name: _validated(f, raw[f.name] if f.name in raw else f.default, where, path)
            for f in fields
        }
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        """Return the values as a plain dict in declared field order; refs as ``{type, params}``."""
        return {f.name: _plain(type(self), f.name, getattr(self, f.name), versions=False)
                for f in dataclasses.fields(self)}

    def to_profile_dict(self) -> dict[str, Any]:
        """As :meth:`to_dict`, but each ref also carries its ``version`` (compiled/stamp form)."""
        return {f.name: _plain(type(self), f.name, getattr(self, f.name), versions=True)
                for f in dataclasses.fields(self)}

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        """Return the JSON Schema (subset: ``SUPPORTED_SCHEMA_KEYWORDS``) of the params.

        Defaults are the validated defaults, so a float field's int default shows as
        a float and a ref default carries its target's default params. Raises
        ``PolicyRegistrationError`` / ``PolicyParamsError`` for a malformed
        declaration or defaults failing their own constraints.
        """
        defaults = cls.from_mapping(None, where=f"defaults of {cls.__qualname__}").to_dict()
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {f.name: _property_schema(f, defaults[f.name]) for f in declared_params(cls)},
        }


def _plain(cls: type, name: str, value: Any, *, versions: bool) -> Any:
    """``value`` as plain data; a ref becomes ``{type, [version,] params}``, recursively."""
    if not isinstance(value, PolicyRef):
        return value
    if value.params is None:  # an unresolved default (an instance built without from_mapping)
        info = next(i for i in declared_params(cls) if i.name == name)
        value = _resolve_ref(info, value, path=name)
    out: dict[str, Any] = {"type": value.type}
    if versions:
        out["version"] = value.version
    out["params"] = (value.params.to_profile_dict() if versions else value.params.to_dict()) \
        if value.params is not None else {}
    return out


def _property_schema(f: ParamInfo, default: Any) -> dict[str, Any]:
    if f.is_ref:
        return {"type": "object", "x-policy-ref": f.meta.kind, "x-leaf-only": f.meta.leaf_only,
                "default": default, "description": f.meta.help}
    json_type = _JSON_TYPES[f.type]
    prop: dict[str, Any] = {"type": [json_type, "null"] if f.nullable else json_type, "default": default}
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


def _validated(f: ParamInfo, value: Any, where: str, path: str) -> Any:
    if f.is_ref:
        return _resolve_ref(f, value, path=f"{path}.{f.name}")
    if value is None and f.nullable:
        return None
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
        word = _TYPE_WORDS[f.type] + (" or null" if f.nullable else "")
        raise PolicyParamsError(
            f"{where}: {f.name!r} must be {word} "
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


def _cli_path(path: str) -> str:
    """``policies.dispatch.params.inner`` -> ``dispatch.inner`` (the --policy-param spelling)."""
    parts = path.split(".")
    if parts and parts[0] in _NON_CLI_ROOTS:
        parts = parts[1:]
    return ".".join(p for p in parts if p != "params")


def _ref_entry(value: Any, path: str) -> tuple[str, Mapping[str, Any] | None]:
    """``(type, raw params)`` of a ref value: a :class:`PolicyRef` or ``{type, params?}``."""
    if isinstance(value, PolicyRef):
        return value.type, None if value.params is None else value.params.to_dict()
    if not isinstance(value, Mapping):
        example = value if isinstance(value, str) and value.strip() else "<name>"
        raise PolicyParamsError(
            f"{path} must be an object {{type, params?}} (got {value!r}). In spec.json write "
            f'{{"type": "{example}"}}; on the CLI --policy-param {_cli_path(path)}={example} is accepted.'
        )
    unknown = sorted(str(k) for k in value if k not in REF_ENTRY_KEYS)
    if unknown:
        raise PolicyParamsError(
            f"{path}: unknown key(s) {', '.join(repr(k) for k in unknown)} in a policy reference. "
            "Allowed keys: type, params (and version, which is ignored)."
        )
    name = value.get("type")
    if not isinstance(name, str) or not name.strip():
        raise PolicyParamsError(f"{path}.type must name a registered policy (got {name!r}).")
    params = value.get("params")
    if params is not None and not isinstance(params, Mapping):
        raise PolicyParamsError(f"{path}.params must be an object (got {params!r}).")
    return name.strip(), params


def _resolve_ref(f: ParamInfo, value: Any, *, path: str) -> PolicyRef:
    """Resolve one ref value against the live target registry of ``f.meta.kind``."""
    name, raw_params = _ref_entry(value, path)
    resolver = _REF_RESOLVERS.get(f.meta.kind)
    if resolver is None:  # declared_params checked the kind; only a cleared hook gets here
        raise PolicyRegistrationError(f"{path}: no resolver registered for ref kind {f.meta.kind!r}.")
    target = resolver(name, where=path, leaf_only=f.meta.leaf_only)
    params = target.params_cls.from_mapping(raw_params, where=f"{path}.params for {target.name}",
                                            path=f"{path}.params")
    return PolicyRef(type=target.name, params=params, version=target.version)
