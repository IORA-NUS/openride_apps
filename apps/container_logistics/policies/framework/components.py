"""Policy-reference targets: the dispatch registry and the registered component kinds.

Single responsibility: answer "what does a ``PolicyRef`` of kind K named N point at?"
for every ref kind, and register those answers into the params hook
(``params._REF_RESOLVERS``). Round 2 ``docs/plans/policy_slots_round2_whole_tick.md``
§3.2, §4.

- ``planner``: the policy registry (``registry.get_policy_spec``); a target whose
  ``requires`` are missing is refused with the top level's ``PolicyDependencyError``
  wording, prefixed by the ref path.
- Component kinds (``market.offer``, ``market.claim``, ``market.arbitration`` today):
  NOT framework concepts. The solver side that owns a kind declares it by calling
  :func:`register_component_kind` at import (``assignment/policy/offer.py``,
  ``assignment/policy/claim.py``, ``assignment/arbitration.py``), handing over a getter
  of its LIVE ``{name: class}`` registry, so a name a test injects there resolves too
  (round-2 S11). A component class's nested ``Params`` is its typed params; a class
  without one declares no params.
- Built-in kinds are found even when nothing imported their modules yet: the first
  kind lookup (never this module's import) runs
  ``registry.discover_builtin_policies()``, and the policies that use a kind import
  the module that declares it (``assignment/pooled_planner.py`` imports offer, claim
  and arbitration before ``PooledMarket`` registers). The framework names no
  component module. A new solver with its own component kind declares it in a module
  its policy imports, and edits nothing in the framework.
- Listing order is declared by the owner: ``register_component_kind(..., order=N)``
  sorts by ``(order, declaration sequence)``, so it never depends on which module
  happened to be imported first. Built-ins declare 10/20/30; the default is 1000.

:func:`instantiate_component` builds a component from a resolved ref; a ``None``
value (an unset nullable number) is dropped first, so the class keeps its own default.

Leaf module (plan §2.7): never imports ``apps.container_logistics.assignment.*``;
the built-in component modules are imported by policy discovery in
:func:`_load_builtin_kinds`.
"""

from __future__ import annotations

import inspect
import itertools
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping

from . import params as _params
from . import registry as _registry
from .errors import PolicyParamsError, PolicyRegistrationError, UnknownPolicyError
from .params import PolicyParams, PolicyRef, version_echo, warn_version_drift
from .registry import check_policy_name, get_policy_spec, leaf_policies, list_policies, top_only_message

logger = logging.getLogger(__name__)

__all__ = [
    "ComponentKind",
    "DEFAULT_KIND_ORDER",
    "COMPONENT_KINDS",
    "NoParams",
    "RefTarget",
    "register_component_kind",
    "live_registry",
    "ref_names",
    "ref_target",
    "instantiate_component",
]

#: The listing order of a kind that declares none: after every built-in kind.
DEFAULT_KIND_ORDER = 1000


@dataclass(frozen=True)
class ComponentKind:
    """One component kind: its live registry getter, its default name, its meaning.

    ``module`` is the module that declared it (re-declaration check); ``order`` is the
    listing position its owner declared (ties broken by declaration sequence).
    """

    kind: str
    default: str
    description: str
    registry_getter: Callable[[], Mapping[str, type]] = field(compare=False, repr=False)
    module: str = ""
    order: int = DEFAULT_KIND_ORDER


_KINDS: dict[str, ComponentKind] = {}
_KIND_SEQ: dict[str, int] = {}
_SEQ = itertools.count()
_LOCK = threading.RLock()


def register_component_kind(kind: str, registry_getter: Callable[[], Mapping[str, type]],
                            default: str, description: str, *,
                            order: int = DEFAULT_KIND_ORDER) -> ComponentKind:
    """Declare component ``kind``: refs of that kind resolve against ``registry_getter()``.

    Called by the module that owns the kind, at import, after its registry dict exists.
    ``registry_getter`` returns the LIVE ``{name: class}`` dict (read on every lookup);
    ``default`` must be one of its names; ``order`` (an int) is the kind's listing
    position, ties broken by declaration sequence. Re-declaring a kind from the same
    module with the same default, description and order (a reload) replaces the getter;
    anything else raises ``PolicyRegistrationError``. Returns the declared
    :class:`ComponentKind`. The declaring module is read from
    ``registry_getter.__module__`` (a lambda or function defined there), which guards
    re-declaration.
    """
    module = getattr(registry_getter, "__module__", None) or ""
    where = f"register_component_kind({kind!r}) from {module or '?'}"
    if not isinstance(kind, str) or not kind.strip() or kind != kind.strip() or kind == "planner":
        raise PolicyRegistrationError(f"{where}: kind must be a non-blank name other than 'planner'.")
    if not callable(registry_getter):
        raise PolicyRegistrationError(
            f"{where}: registry_getter must be a callable returning the live registry."
        )
    for label, value in (("default", default), ("description", description)):
        if not isinstance(value, str) or not value.strip():
            raise PolicyRegistrationError(f"{where}: {label} must be a non-empty string (got {value!r}).")
    if not isinstance(order, int) or isinstance(order, bool):
        raise PolicyRegistrationError(f"{where}: order must be an int (got {order!r}).")
    registry = registry_getter()
    if not isinstance(registry, Mapping) or default not in registry:
        raise PolicyRegistrationError(
            f"{where}: default {default!r} is not in the registry the getter returns "
            f"({', '.join(sorted(registry)) if isinstance(registry, Mapping) else registry!r})."
        )
    for name in registry:
        # A component name is a node of the selection label and of the run-name
        # segment exactly like a policy name ('PooledMarket(offer=OfferNone)').
        check_policy_name(name, f"{where}: component")
    spec = ComponentKind(kind=kind, default=default, description=description,
                         registry_getter=registry_getter, module=module, order=order)
    with _LOCK:
        existing = _KINDS.get(kind)
        if existing is not None and (existing.module, existing.default, existing.description,
                                     existing.order) != \
                (spec.module, spec.default, spec.description, spec.order):
            raise PolicyRegistrationError(
                f"{where}: component kind {kind!r} is already declared by {existing.module} "
                f"(default {existing.default!r}); one module owns a kind."
            )
        _KINDS[kind] = spec
        _KIND_SEQ.setdefault(kind, next(_SEQ))
        _params._REF_RESOLVERS[kind] = _resolver(kind)
    return spec


def _load_builtin_kinds() -> None:
    """Load the built-in kinds: run policy discovery, which imports their declaring modules.

    Delegates to ``registry.discover_builtin_policies()`` (idempotent; a call during a
    running discovery returns at once, so kinds looked up by a policy module being
    discovered must already be imported by it, as ``pooled_planner`` does). Import
    errors propagate: a broken component module must not vanish.
    """
    _registry.discover_builtin_policies()


def _order(kind: str) -> tuple[int, int]:
    return _KINDS[kind].order, _KIND_SEQ[kind]


class _LiveKinds(Mapping[str, ComponentKind]):
    """Read-only live view of the declared kinds; loads the built-in ones on first use.

    Ordered by each kind's declared ``order``, then declaration sequence, so the
    listing never depends on which module happened to be imported first.
    """

    def __getitem__(self, kind: str) -> ComponentKind:
        _load_builtin_kinds()
        return _KINDS[kind]

    def __iter__(self) -> Iterator[str]:
        _load_builtin_kinds()
        return iter(sorted(_KINDS, key=_order))

    def __len__(self) -> int:
        _load_builtin_kinds()
        return len(_KINDS)

    def __repr__(self) -> str:
        return f"COMPONENT_KINDS({list(self)!r})"


COMPONENT_KINDS: Mapping[str, ComponentKind] = _LiveKinds()


@dataclass(frozen=True)
class NoParams(PolicyParams):
    """The params of a component class that declares no ``Params`` (e.g. a test injection)."""


@dataclass(frozen=True)
class RefTarget:
    """What a ref names: its kind, registered name, version, class and params class.

    ``is_composite`` is true for a dispatch policy with a dispatch ref of its own;
    ``refs`` are its ``(param, kind, leaf_only)`` ref params.
    """

    kind: str
    name: str
    version: str | None
    description: str
    cls: type
    params_cls: type[PolicyParams]
    refs: tuple[tuple[str, str, bool], ...] = ()
    top_only: bool = False

    @property
    def is_composite(self) -> bool:
        return any(kind == "planner" for _name, kind, _leaf in self.refs)


def live_registry(kind: str) -> Mapping[str, type]:
    """The live ``{name: class}`` dict of component ``kind``, read through its getter now.

    Raises ``KeyError`` for a kind that is not a declared component kind.
    """
    return COMPONENT_KINDS[kind].registry_getter()


def ref_names(kind: str) -> list[str]:
    """The sorted names a ref of ``kind`` may name, read live."""
    if kind == "planner":
        return [s.name for s in list_policies("planner")]
    return sorted(live_registry(kind))


def ref_target(kind: str, name: str, *, where: str, leaf_only: bool = False) -> RefTarget:
    """Resolve ``name`` in ``kind``; ``where`` is the dotted path errors name.

    Raises ``UnknownPolicyError`` for an unknown name (listing the registered ones and
    the CLI command), ``PolicyParamsError`` for a composite target where ``leaf_only``,
    and ``PolicyParamsError`` (refusal R5) for a ``top_only`` planner policy, which may
    only be the top of a selection.
    """
    target = _dispatch_target(name, where) if kind == "planner" else _component_target(kind, name, where)
    if target.top_only:
        # R5: a top_only policy is never a ref target, leaf_only or not (it must be the top).
        raise PolicyParamsError(top_only_message(target.name, f"a nested node ({where})"))
    if leaf_only and target.is_composite:
        own = ", ".join(repr(n) for n, k, _leaf in target.refs if k == "planner")
        leaves = ", ".join(s.name for s in leaf_policies("planner"))
        field = where.rpartition(".")[2]
        raise PolicyParamsError(
            f"{where}: {target.name!r} decomposes the tick itself (it has an {own} of its own); "
            f"an {field} must be a per-scope solver: {leaves}."
        )
    return target


def _dispatch_target(name: str, where: str) -> RefTarget:
    names = ref_names("planner")
    if name not in names:
        raise UnknownPolicyError(
            f"{where}: unknown planner policy {name!r}. Registered planner policies: "
            f"{', '.join(names) or '(none)'}. (List them: openride policy list planner)"
        )
    spec = get_policy_spec("planner", name)
    return RefTarget(kind="planner", name=spec.name, version=spec.version,
                     description=spec.description, cls=spec.cls,
                     params_cls=spec.params_cls, refs=spec.refs, top_only=spec.top_only)


def _component_target(kind: str, name: str, where: str) -> RefTarget:
    registry = live_registry(kind)
    cls = registry.get(name) if isinstance(name, str) else None
    if cls is None:
        raise UnknownPolicyError(
            f"{where}: unknown {kind} component {name!r}. Registered: "
            f"{', '.join(sorted(registry)) or '(none)'}. (List them: openride policy list {kind})"
        )
    # The live dict can gain a name after the kind was declared (a test or plugin
    # injection), so the grammar is enforced again where the name is resolved.
    check_policy_name(name, f"{where}: {kind} component")
    return RefTarget(kind=kind, name=name, version=getattr(cls, "version", None),
                     description=_first_paragraph(cls), cls=cls,
                     params_cls=getattr(cls, "Params", None) or NoParams)


def _first_paragraph(cls: type) -> str:
    doc = inspect.getdoc(cls) or ""
    return " ".join(doc.split("\n\n", 1)[0].split())


def instantiate_component(kind: str, value: PolicyRef) -> Any:
    """Build the component a resolved ref names, through its class's ``params=`` dict.

    ``None`` values are dropped, so an unset nullable param (``OfferSpare.keep_below_km``)
    takes the class's own default (``inf``). An unresolved ref (``params is None``) is
    built with an empty dict. Raises ``UnknownPolicyError`` for an unknown name and
    ``PolicyParamsError`` when ``value.params`` is not the named class's ``Params`` (a
    ref whose type was swapped after resolution) or ``value.version`` is malformed; a
    version that differs from the registered one is logged (``POLICY VERSION DRIFT``).
    """
    where = f"{kind} component"
    target = ref_target(kind, value.type, where=where)
    warn_version_drift(where, kind, target.name, version_echo(value.version, where=where),
                       target.version)
    if value.params is not None and type(value.params) is not target.params_cls:
        raise PolicyParamsError(
            f"{where} {target.name!r}: params are a {type(value.params).__qualname__}, not "
            f"{target.name}'s {target.params_cls.__qualname__}; resolve the ref again "
            "(PolicyParams.from_mapping) instead of changing its type."
        )
    values = value.params.to_dict() if value.params is not None else {}
    return target.cls(params={k: v for k, v in values.items() if v is not None})


def _resolver(kind: str):
    def resolve(name: str, *, where: str, leaf_only: bool = False) -> RefTarget:
        target = ref_target(kind, name, where=where, leaf_only=leaf_only)
        if kind == "planner":  # a nested ref fails loud exactly like the top level
            get_policy_spec("planner", target.name).require_available(where=where)
        return target
    return resolve


# The params hook (round-2 §3.2): registered here, so params.py never imports the
# registry (no params <-> registry cycle). Component kinds add theirs in
# register_component_kind; a kind not yet declared runs policy discovery (once).
_params._REF_RESOLVERS["planner"] = _resolver("planner")
_params._REF_KIND_LOADER = _load_builtin_kinds
