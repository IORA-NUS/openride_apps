"""Policy-reference targets: the dispatch registry and the market component kinds.

Single responsibility: answer "what does a ``PolicyRef`` of kind K named N point at?"
for every ref kind, and register those answers into the params hook
(``params._REF_RESOLVERS``) at import. Round 2
``docs/plans/policy_slots_round2_whole_tick.md`` §3.2, §4.

- ``dispatch``: the policy registry (``registry.get_policy_spec``).
- ``market.offer`` / ``market.claim`` / ``market.arbitration``: the pooled market's
  components (``COMPONENT_KINDS``), read from the LIVE legacy dicts ``OFFER_REGISTRY``,
  ``CLAIM_REGISTRY`` and ``ARBITRATION_REGISTRY`` at call time, so a name a test
  injects there resolves too (round-2 S11). A component class's nested ``Params`` is
  its typed params; a class without one declares no params.

:func:`instantiate_component` builds a component from a resolved ref; a ``None``
value (an unset nullable number) is dropped first, so the class keeps its own default.

Leaf module (plan §2.7): never imports ``apps.container_logistics.assignment.*`` at
module top; the component registries are imported locally in :func:`live_registry`.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from . import params as _params
from .errors import PolicyParamsError, UnknownPolicyError
from .params import PolicyParams, PolicyRef
from .registry import get_policy_spec, list_policies

logger = logging.getLogger(__name__)

__all__ = [
    "ComponentKind",
    "COMPONENT_KINDS",
    "NoParams",
    "RefTarget",
    "live_registry",
    "ref_names",
    "ref_target",
    "instantiate_component",
]


@dataclass(frozen=True)
class ComponentKind:
    """One market component kind: where its live registry lives, its default, its meaning."""

    kind: str
    module: str
    registry: str
    default: str
    description: str


_KINDS = (
    ComponentKind(
        kind="market.offer",
        module="apps.container_logistics.assignment.policy.offer",
        registry="OFFER_REGISTRY",
        default="OfferAll",
        description="Which of its own orders a company offers to the shared pool.",
    ),
    ComponentKind(
        kind="market.claim",
        module="apps.container_logistics.assignment.policy.claim",
        registry="CLAIM_REGISTRY",
        default="ClaimAllPlanned",
        description="Which planned pool pairs a company bids for.",
    ),
    ComponentKind(
        kind="market.arbitration",
        module="apps.container_logistics.assignment.arbitration",
        registry="ARBITRATION_REGISTRY",
        default="LowestCost",
        description="Who wins when two companies bid for the same pooled order.",
    ),
)

COMPONENT_KINDS: Mapping[str, ComponentKind] = MappingProxyType({k.kind: k for k in _KINDS})


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

    @property
    def is_composite(self) -> bool:
        return any(kind == "dispatch" for _name, kind, _leaf in self.refs)


def live_registry(kind: str) -> dict[str, type]:
    """The live ``{name: class}`` dict of component ``kind``, imported at call time.

    Local import on purpose: the leaf rule forbids a module-top ``assignment`` import.
    Raises ``KeyError`` for a kind that is not a component kind.
    """
    spec = COMPONENT_KINDS[kind]
    return getattr(importlib.import_module(spec.module), spec.registry)


def ref_names(kind: str) -> list[str]:
    """The sorted names a ref of ``kind`` may name, read live."""
    if kind == "dispatch":
        return [s.name for s in list_policies("dispatch")]
    return sorted(live_registry(kind))


def ref_target(kind: str, name: str, *, where: str, leaf_only: bool = False) -> RefTarget:
    """Resolve ``name`` in ``kind``; ``where`` is the dotted path errors name.

    Raises ``UnknownPolicyError`` for an unknown name (listing the registered ones and
    the CLI command), ``PolicyParamsError`` for a composite target where ``leaf_only``.
    """
    target = _dispatch_target(name, where) if kind == "dispatch" else _component_target(kind, name, where)
    if leaf_only and target.is_composite:
        own = ", ".join(repr(n) for n, k, _leaf in target.refs if k == "dispatch")
        leaves = ", ".join(n for n in ref_names("dispatch") if not _dispatch_target(n, where).is_composite)
        field = where.rpartition(".")[2]
        raise PolicyParamsError(
            f"{where}: {target.name!r} decomposes the tick itself (it has an {own} of its own); "
            f"an {field} must be a per-scope solver: {leaves}."
        )
    return target


def _dispatch_target(name: str, where: str) -> RefTarget:
    names = ref_names("dispatch")
    if name not in names:
        raise UnknownPolicyError(
            f"{where}: unknown dispatch policy {name!r}. Registered dispatch policies: "
            f"{', '.join(names) or '(none)'}. (List them: openride policy list dispatch)"
        )
    spec = get_policy_spec("dispatch", name)
    return RefTarget(kind="dispatch", name=spec.name, version=spec.version,
                     description=spec.description, cls=spec.cls,
                     params_cls=spec.params_cls, refs=spec.refs)


def _component_target(kind: str, name: str, where: str) -> RefTarget:
    registry = live_registry(kind)
    cls = registry.get(name) if isinstance(name, str) else None
    if cls is None:
        raise UnknownPolicyError(
            f"{where}: unknown {kind} component {name!r}. Registered: "
            f"{', '.join(sorted(registry)) or '(none)'}. (List them: openride policy list {kind})"
        )
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
    built with an empty dict. Raises ``UnknownPolicyError`` for an unknown name.
    """
    target = ref_target(kind, value.type, where=f"{kind} component")
    values = value.params.to_dict() if value.params is not None else {}
    return target.cls(params={k: v for k, v in values.items() if v is not None})


def _resolver(kind: str):
    def resolve(name: str, *, where: str, leaf_only: bool = False) -> RefTarget:
        return ref_target(kind, name, where=where, leaf_only=leaf_only)
    return resolve


# The params hook (round-2 §3.2): registered here, at import, so params.py never
# imports the registry (no params <-> registry cycle).
for _kind in ("dispatch", *COMPONENT_KINDS):
    _params._REF_RESOLVERS[_kind] = _resolver(_kind)
