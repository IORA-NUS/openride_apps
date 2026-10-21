"""The policy registry: ``@policy`` registration, lookup, and built-in discovery.

A policy is a class decorated with ``@policy(slot=..., name=..., version=...,
description=...)`` that carries a nested frozen ``Params`` dataclass. Registration
validates the declaration at import time and fails loud
(``PolicyRegistrationError``). Lookups (:func:`get_policy_spec`,
:func:`list_policies`, :func:`default_policy_name`, :func:`spec_for_class`) run
:func:`discover_builtin_policies` first, the ONE auto-discovery function (plan
``docs/plans/policy_slots_p0_p3.md`` §2.12, §4 P0.3). No silent fallback: an unknown
name raises ``UnknownPolicyError`` listing what is registered.

Determinism: the registry is a plain dict keyed by ``(slot, name)``; every listing is
sorted, discovery imports modules in sorted order, so nothing depends on
``PYTHONHASHSEED`` or on import order.

Leaf module (plan §2.7): never imports ``apps.container_logistics.assignment.*`` at
module top. Discovery imports the built-in packages lazily, inside the function.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import pkgutil
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, Sequence

from .errors import (
    PolicyError,
    PolicyRegistrationError,
    SlotNotImplementedError,
    UnknownPolicyError,
)
from .params import PolicyParams, declared_params
from .slots import SLOTS, slot_spec

logger = logging.getLogger(__name__)

__all__ = [
    "PolicySpec",
    "policy",
    "get_policy_spec",
    "spec_for_class",
    "list_policies",
    "default_policy_name",
    "discover_builtin_policies",
    "isolated_registry",
    "BUILTIN_POLICY_PACKAGES",
]

BUILTIN_POLICY_PACKAGES = (
    "apps.container_logistics.assignment.solver",
    "apps.container_logistics.policies.dispatch.library",
)

# Per-slot required base class, as "module:QualName", imported lazily at registration
# so this leaf never imports the slot's contract module at top.
_SLOT_BASES: dict[str, str] = {
    "dispatch": "apps.container_logistics.policies.dispatch.base:DispatchPolicy",
}

_REGISTRY: dict[tuple[str, str], "PolicySpec"] = {}
_DISCOVERY = {"done": False, "running": False}
_LOCK = threading.RLock()


@dataclass(frozen=True)
class PolicySpec:
    """A registered policy: identity, metadata, implementing class and its ``Params``.

    ``order_sensitive`` is the stated reason the policy's decision may depend on input
    order, or ``None``. ``perf_budget_ms`` is used by the P3 battery only.
    """

    slot: str
    name: str
    version: str
    description: str
    requires: tuple[str, ...]
    cls: type
    params_cls: type[PolicyParams]
    order_sensitive: str | None
    perf_budget_ms: int

    def missing_requirements(self) -> list[str]:
        """Return the ``requires`` modules that cannot be found, in declared order.

        Uses ``importlib.util.find_spec``, so nothing is imported except the parent
        packages of dotted names; a dotted name whose parent is missing counts as missing.
        """
        return [mod for mod in self.requires if not _module_findable(mod)]

    def kind(self) -> str:
        """Return the slot's kind (``operator`` or ``world``)."""
        return SLOTS[self.slot].kind


def _module_findable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ModuleNotFoundError, ValueError):
        return False


def policy(
    *,
    slot: str,
    name: str,
    version: str,
    description: str,
    requires: Sequence[str] = (),
    order_sensitive: str | None = None,
    perf_budget_ms: int = 2000,
) -> Callable[[type], type]:
    """Class decorator registering a policy into ``slot`` under ``name``.

    Returns the class unchanged. Raises ``PolicyRegistrationError`` (at import) for an
    unknown or non-implemented slot, malformed metadata, a missing/non-frozen/
    non-``PolicyParams`` nested ``Params``, defaults failing their own validation, a
    wrong slot base class, or ``(slot, name)`` already bound to a different class.
    Re-registering the same class with identical metadata is a no-op.
    """
    def decorate(cls: type) -> type:
        spec = _build_spec(cls, slot, name, version, description, requires,
                           order_sensitive, perf_budget_ms)
        with _LOCK:
            _store(spec)
        return cls

    return decorate


def _build_spec(cls, slot, name, version, description, requires,
                order_sensitive, perf_budget_ms) -> PolicySpec:
    where = f"@policy(slot={slot!r}, name={name!r}) on {getattr(cls, '__qualname__', cls)!r}"
    _check_slot_registrable(slot, where)
    _check_metadata(where, name, version, description, requires, order_sensitive, perf_budget_ms)
    _check_slot_base(cls, slot, where)
    params_cls = _checked_params(cls, where)
    return PolicySpec(slot=slot, name=name, version=version, description=description,
                      requires=tuple(requires), cls=cls, params_cls=params_cls,
                      order_sensitive=order_sensitive, perf_budget_ms=perf_budget_ms)


def _check_slot_registrable(slot: str, where: str) -> None:
    try:
        spec = slot_spec(slot)
    except PolicyError as exc:
        raise PolicyRegistrationError(f"{where}: {exc}") from exc
    if spec.status == "planned":
        raise PolicyRegistrationError(
            f"{where}: slot {slot!r} is declared but not implemented yet (lands in {spec.lands_in})"
        )
    if spec.status != "implemented":
        raise PolicyRegistrationError(
            f"{where}: slot {slot!r} is a {spec.status} slot configured via "
            f"{spec.configured_via}; it does not take registered policies yet"
        )


def _check_metadata(where, name, version, description, requires, order_sensitive, perf_budget_ms) -> None:
    for label, value in (("name", name), ("version", version), ("description", description)):
        if not isinstance(value, str) or not value.strip():
            raise PolicyRegistrationError(f"{where}: {label} must be a non-empty string (got {value!r}).")
    if isinstance(requires, str) or not all(isinstance(r, str) and r for r in requires):
        raise PolicyRegistrationError(
            f"{where}: requires must be a sequence of module names, e.g. ('ortools',) (got {requires!r})."
        )
    if order_sensitive is not None and (not isinstance(order_sensitive, str) or not order_sensitive.strip()):
        raise PolicyRegistrationError(
            f"{where}: order_sensitive must be None or the stated reason (got {order_sensitive!r})."
        )
    if isinstance(perf_budget_ms, bool) or not isinstance(perf_budget_ms, int) or perf_budget_ms <= 0:
        raise PolicyRegistrationError(
            f"{where}: perf_budget_ms must be a positive integer (got {perf_budget_ms!r})."
        )


def _check_slot_base(cls: type, slot: str, where: str) -> None:
    target = _SLOT_BASES.get(slot)
    if target is None:
        return
    module_name, _, qualname = target.partition(":")
    base = importlib.import_module(module_name)
    for part in qualname.split("."):
        base = getattr(base, part)
    if not (isinstance(cls, type) and issubclass(cls, base)):
        raise PolicyRegistrationError(f"{where}: a {slot} policy must subclass {target}.")


def _checked_params(cls: type, where: str) -> type[PolicyParams]:
    params_cls = getattr(cls, "Params", None)
    if params_cls is None:
        raise PolicyRegistrationError(
            f"{where}: missing nested Params. Declare `@dataclass(frozen=True) class "
            "Params(PolicyParams): ...` inside the class (empty is fine)."
        )
    try:
        declared_params(params_cls)
        params_cls.from_mapping(None, where=f"{where}: defaults")
    except PolicyRegistrationError as exc:
        raise PolicyRegistrationError(f"{where}: {exc}") from exc
    except PolicyError as exc:
        raise PolicyRegistrationError(f"{where}: defaults fail their own validation: {exc}") from exc
    return params_cls


def _store(spec: PolicySpec) -> None:
    key = (spec.slot, spec.name)
    existing = _REGISTRY.get(key)
    if existing is not None:
        if existing == spec:
            return
        if existing.cls is not spec.cls:
            raise PolicyRegistrationError(
                f"{spec.slot} policy {spec.name!r} is already registered to "
                f"{_qual(existing.cls)}; cannot bind it to {_qual(spec.cls)}. Pick another name."
            )
        raise PolicyRegistrationError(
            f"{spec.slot} policy {spec.name!r} ({_qual(spec.cls)}) was registered twice "
            "with different metadata; keep a single @policy declaration."
        )
    for other in _REGISTRY.values():
        if other.cls is spec.cls:
            raise PolicyRegistrationError(
                f"{_qual(spec.cls)} is already registered as {other.slot} policy "
                f"{other.name!r}; one class backs exactly one policy."
            )
    _REGISTRY[key] = spec


def _qual(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def discover_builtin_policies() -> None:
    """Import every module of each ``BUILTIN_POLICY_PACKAGES`` package, once.

    Modules are imported in sorted name order; names starting with ``_`` are skipped.
    Import errors propagate (a broken policy module must not vanish from the
    catalog). A missing package raises ``PolicyRegistrationError``. Idempotent:
    completes once per process (re-entrant calls during discovery return at once).
    """
    with _LOCK:
        if _DISCOVERY["done"] or _DISCOVERY["running"]:
            return
        _DISCOVERY["running"] = True
        try:
            for package_name in BUILTIN_POLICY_PACKAGES:
                _import_package_modules(package_name)
            _DISCOVERY["done"] = True
        finally:
            _DISCOVERY["running"] = False


def _import_package_modules(package_name: str) -> None:
    try:
        package = importlib.import_module(package_name)
    except ModuleNotFoundError as exc:
        if exc.name and (package_name == exc.name or package_name.startswith(exc.name + ".")):
            raise PolicyRegistrationError(
                f"Built-in policy package {package_name!r} does not exist "
                f"(missing {exc.name!r}); BUILTIN_POLICY_PACKAGES in {__name__} is out of date."
            ) from exc
        raise
    paths = getattr(package, "__path__", None)
    if paths is None:
        raise PolicyRegistrationError(
            f"Built-in policy package {package_name!r} is a module, not a package."
        )
    for name in sorted(info.name for info in pkgutil.iter_modules(paths)):
        if not name.startswith("_"):
            importlib.import_module(f"{package_name}.{name}")


def get_policy_spec(slot: str, name: str) -> PolicySpec:
    """Return the registered policy ``name`` in ``slot``.

    Raises ``UnknownSlotError`` for an unknown slot, ``SlotNotImplementedError`` for a
    legacy/planned slot, and ``UnknownPolicyError`` (listing the registered names and
    the CLI command that lists them) for an unknown name. Never falls back.
    """
    _require_implemented(slot)
    discover_builtin_policies()
    spec = _REGISTRY.get((slot, name)) if isinstance(name, str) else None
    if spec is None:
        registered = ", ".join(sorted(n for s, n in _REGISTRY if s == slot)) or "(none)"
        raise UnknownPolicyError(
            f"Unknown {slot} policy {name!r}. Registered {slot} policies: {registered}. "
            f"(List them: openride policy list {slot})"
        )
    return spec


def _require_implemented(slot: str) -> None:
    spec = slot_spec(slot)
    if spec.status == "planned":
        raise SlotNotImplementedError(
            f"slot {slot!r} is declared but not implemented yet (lands in {spec.lands_in})"
        )
    if spec.status != "implemented":
        raise SlotNotImplementedError(
            f"slot {slot!r} is a {spec.status} slot configured via {spec.configured_via}; "
            "its policies are not in the policy registry"
        )


def spec_for_class(cls: type) -> PolicySpec | None:
    """Return the spec registered for exactly ``cls`` (not a subclass), or ``None``."""
    discover_builtin_policies()
    for spec in _REGISTRY.values():
        if spec.cls is cls:
            return spec
    return None


def list_policies(slot: str | None = None) -> list[PolicySpec]:
    """Return registered policies sorted by ``(slot, name)``, optionally for one slot.

    Raises ``UnknownSlotError`` for an unknown ``slot``.
    """
    if slot is not None:
        slot_spec(slot)
    discover_builtin_policies()
    specs = [s for s in _REGISTRY.values() if slot is None or s.slot == slot]
    return sorted(specs, key=lambda s: (s.slot, s.name))


def default_policy_name(slot: str) -> str:
    """Return ``slot``'s declared default policy name.

    For an implemented slot the default must be registered (else
    ``UnknownPolicyError``). Raises ``SlotNotImplementedError`` for a slot that has
    no default (a planned slot), ``UnknownSlotError`` for an unknown slot.
    """
    spec = slot_spec(slot)
    if spec.default is None:
        raise SlotNotImplementedError(
            f"slot {slot!r} has no default policy: it is declared but not implemented yet "
            f"(lands in {spec.lands_in})"
        )
    if spec.status == "implemented":
        get_policy_spec(slot, spec.default)
    return spec.default


@contextmanager
def isolated_registry() -> Iterator[None]:
    """Snapshot the registry and discovery state, and restore both on exit. Tests only.

    Discovery runs BEFORE the snapshot: built-in modules register on first import
    only, so a discovery that ran inside the block and was then rolled back could
    never re-register them (their modules stay cached in ``sys.modules``).
    """
    discover_builtin_policies()
    with _LOCK:
        saved_registry = dict(_REGISTRY)
        saved_discovery = dict(_DISCOVERY)
    try:
        yield
    finally:
        with _LOCK:
            _REGISTRY.clear()
            _REGISTRY.update(saved_registry)
            _DISCOVERY.clear()
            _DISCOVERY.update(saved_discovery)
