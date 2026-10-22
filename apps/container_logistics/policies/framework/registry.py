"""The policy registry: ``@policy`` registration, lookup, and built-in discovery.

A policy is a class decorated with ``@policy(slot=..., name=..., version=...,
description=...)`` that carries a nested frozen ``Params`` dataclass. It may declare a
display-only ``badge`` / ``badge_tooltip`` (the catalog shows them; nothing reads them at
run time; what a run actually did is stamped from its bound declaration, plan
``plan_contract_final.md`` §2.1). Registration
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
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Iterator, Mapping, Sequence

from .errors import (
    PolicyDependencyError,
    PolicyError,
    PolicyRegistrationError,
    UnknownPolicyError,
)
from .params import PolicyParams, check_defaults, declared_params
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
    "INNER_PARAM",
    "OFFLOAD_MODES",
]

BUILTIN_POLICY_PACKAGES = (
    "apps.container_logistics.assignment.solver",
    "apps.container_logistics.policies.dispatch.library",
)

# Per-slot entry point a registered class must define (callable): the framework calls it
# every tick. ``solve_tick``, NOT ``solve`` (the pair API of PairSolver, plan B1). The
# helper base ``dispatch.base.DispatchPolicy`` provides one for a legacy ``decide(ctx)``.
_SLOT_ENTRY_POINTS: dict[str, str] = {
    "planner": "solve_tick",
}
#: Where the framework offloads a node's work (``solve_in_thread``): its own
#: ``solve_tick`` (``self``), or only the calls it makes to its children (``children``).
OFFLOAD_MODES = ("self", "children")

#: The dispatch-ref param a composite delegates each scope to (``dispatch/composite.py``).
#: ``PolicySpec.inner_param`` is the one place the descent rule is read from: the
#: ``label``/``leaf`` chain, ``--solver`` (the innermost leaf) and the catalog follow it.
INNER_PARAM = "inner"

_REGISTRY: dict[tuple[str, str], "PolicySpec"] = {}
_DISCOVERY = {"done": False, "running": False}
_LOCK = threading.RLock()


@dataclass(frozen=True)
class PolicySpec:
    """A registered policy: identity, metadata, implementing class and its ``Params``.

    ``order_sensitive`` is the stated reason the policy's decision may depend on input
    order, or ``None``; ``module_rng`` the stated reason it draws from the ``random``
    module, or ``None``. ``perf_budget_ms`` (the default arm) and ``arm_budgets_ms``
    (``"param=value"`` -> ms for other arms) are used by the battery only.
    ``badge`` / ``badge_tooltip`` are display text for the catalog (``None``: no
    badge); never read at run time.
    ``refs`` lists the ``PolicyRef`` params as ``(param, kind, leaf_only)``.
    ``offload`` (:data:`OFFLOAD_MODES`) says whether the framework runs the node's
    ``solve_tick`` through ``tick.execute`` (``self``) or the node offloads its own
    child calls (``children``; the default for a node with a planner ref).
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
    module_rng: str | None
    arm_budgets_ms: Mapping[str, int]
    refs: tuple[tuple[str, str, bool], ...]
    offload: str = "self"
    badge: str | None = None
    badge_tooltip: str | None = None

    @property
    def is_composite(self) -> bool:
        """True when the policy references a dispatch policy of its own (an ``inner``)."""
        return any(kind == "planner" for _name, kind, _leaf in self.refs)

    @property
    def inner_param(self) -> str | None:
        """The dispatch-ref param the selection descends through (``inner``), or ``None`` for a leaf.

        ``None`` too for a composite that does not delegate through :data:`INNER_PARAM`
        (``dispatch/profile.dispatch_node`` refuses such a composite).
        """
        return next((name for name, kind, _leaf in self.refs
                     if kind == "planner" and name == INNER_PARAM), None)

    def missing_requirements(self) -> list[str]:
        """Return the ``requires`` modules that cannot be found, in declared order.

        Uses ``importlib.util.find_spec``, so nothing is imported except the parent
        packages of dotted names; a dotted name whose parent is missing counts as missing.
        """
        return [mod for mod in self.requires if not _module_findable(mod)]

    def require_available(self, *, where: str | None = None) -> None:
        """Raise ``PolicyDependencyError`` when :meth:`missing_requirements` is non-empty.

        The one wording for the top-level selection and for a nested ref (``where`` is
        the ref path, prefixed as ``<where>: ``).
        """
        missing = self.missing_requirements()
        if missing:
            prefix = f"{where}: " if where else ""
            raise PolicyDependencyError(
                f"{prefix}{self.slot.capitalize()} policy {self.name!r} requires module(s) not "
                f"importable in this environment: {', '.join(missing)}. Install them or pick "
                "another policy."
            )

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
    module_rng: str | None = None,
    arm_budgets_ms: Mapping[str, int] | None = None,
    offload: str | None = None,
    badge: str | None = None,
    badge_tooltip: str | None = None,
) -> Callable[[type], type]:
    """Class decorator registering a policy into ``slot`` under ``name``.

    Returns the class unchanged. Raises ``PolicyRegistrationError`` (at import) for an
    unknown or non-implemented slot, malformed metadata, a missing/non-frozen/
    non-``PolicyParams`` nested ``Params``, defaults failing their own validation, a
    class without a callable ``solve_tick``, an ``offload`` outside
    :data:`OFFLOAD_MODES`, a ``badge`` / ``badge_tooltip`` that is not ``None`` or
    non-empty text (or a tooltip without a badge), an ``arm_budgets_ms``
    key naming no str-choice arm, ref defaults that form a cycle through registered
    dispatch policies (including a self-reference), or ``(slot, name)`` already bound
    to a different class.
    Re-registering the same class with identical metadata is a no-op.
    """
    def decorate(cls: type) -> type:
        spec = _build_spec(cls, slot, name, version, description, requires, order_sensitive,
                           perf_budget_ms, module_rng, arm_budgets_ms, offload, badge, badge_tooltip)
        with _LOCK:
            _store(spec)
        return cls

    return decorate


def _build_spec(cls, slot, name, version, description, requires, order_sensitive,
                perf_budget_ms, module_rng, arm_budgets_ms, offload=None, badge=None,
                badge_tooltip=None) -> PolicySpec:
    where = f"@policy(slot={slot!r}, name={name!r}) on {getattr(cls, '__qualname__', cls)!r}"
    short = f"@policy(slot={slot!r}, name={name!r})"
    _check_slot_registrable(slot, where)
    _check_metadata(where, name, version, description, requires, order_sensitive, perf_budget_ms)
    _check_reason(where, "module_rng", module_rng)
    _check_badge(short, badge, badge_tooltip)
    _check_slot_base(cls, slot, where)
    params_cls = _checked_params(cls, where)
    infos = declared_params(params_cls)
    budgets = _checked_arm_budgets(short, infos, arm_budgets_ms)
    _check_ref_default_cycles(short, slot, name, infos)
    refs = tuple((i.name, i.meta.kind, i.meta.leaf_only) for i in infos if i.is_ref)
    offload = _checked_offload(short, offload, refs)
    return PolicySpec(slot=slot, name=name, version=version, description=description,
                      requires=tuple(requires), cls=cls, params_cls=params_cls,
                      order_sensitive=order_sensitive, perf_budget_ms=perf_budget_ms,
                      module_rng=module_rng, arm_budgets_ms=budgets,
                      refs=refs, offload=offload, badge=badge, badge_tooltip=badge_tooltip)


def _check_slot_registrable(slot: str, where: str) -> None:
    try:
        slot_spec(slot)
    except PolicyError as exc:
        raise PolicyRegistrationError(f"{where}: {exc}") from exc


#: What a policy NAME may be (final review MINOR-3): the frontend's ``NODE_NAME``
#: grammar (``lib/runNameMeta.ts``). A name is a run-name segment and a node of a
#: ``A/B`` label, so the separators ``" · "``, ``/``, ``(``, ``)``, ``=`` and ``,``
#: (and any whitespace) would make the segment unparseable; ``:`` is kept free for a
#: prefix such as ``lifecycle:``.
POLICY_NAME_PATTERN = r"[A-Za-z_][A-Za-z0-9_.-]*"
_POLICY_NAME_RE = re.compile(POLICY_NAME_PATTERN)


def check_policy_name(name: object, where: str) -> None:
    """Raise ``PolicyRegistrationError`` unless ``name`` matches :data:`POLICY_NAME_PATTERN`."""
    if not isinstance(name, str) or not _POLICY_NAME_RE.fullmatch(name):
        raise PolicyRegistrationError(
            f"{where}: name {name!r} must match {POLICY_NAME_PATTERN} (letters, digits, '_', "
            "'.', '-'; no spaces): a policy name is a run-name segment and a node of the "
            "'A/B' selection label, so ' · ', '/', '(', ')', '=' and ',' would break both."
        )


def _check_metadata(where, name, version, description, requires, order_sensitive, perf_budget_ms) -> None:
    for label, value in (("name", name), ("version", version), ("description", description)):
        if not isinstance(value, str) or not value.strip():
            raise PolicyRegistrationError(f"{where}: {label} must be a non-empty string (got {value!r}).")
    check_policy_name(name, where)
    if isinstance(requires, str) or not all(isinstance(r, str) and r for r in requires):
        raise PolicyRegistrationError(
            f"{where}: requires must be a sequence of module names, e.g. ('ortools',) (got {requires!r})."
        )
    _check_reason(where, "order_sensitive", order_sensitive)
    if isinstance(perf_budget_ms, bool) or not isinstance(perf_budget_ms, int) or perf_budget_ms <= 0:
        raise PolicyRegistrationError(
            f"{where}: perf_budget_ms must be a positive integer (got {perf_budget_ms!r})."
        )


def _check_reason(where: str, label: str, value) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise PolicyRegistrationError(f"{where}: {label} must be None or the stated reason (got {value!r}).")


def _check_badge(short: str, badge, tooltip) -> None:
    """``badge`` / ``badge_tooltip``: ``None`` or non-empty text; a tooltip needs a badge."""
    for label, value in (("badge", badge), ("badge_tooltip", tooltip)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise PolicyRegistrationError(
                f"{short}: {label} must be None or non-empty display text (got {value!r})."
            )
    if tooltip is not None and badge is None:
        raise PolicyRegistrationError(f"{short}: badge_tooltip={tooltip!r} needs a badge to describe.")


def _check_ref_default_cycles(short: str, slot: str, name: str, infos) -> None:
    """Refuse dispatch-ref DEFAULTS that lead back to the policy being registered.

    Walks the default ``kind="planner"`` refs through the policies registered so far
    (plus this one, so a self-reference is caught). Every cycle is closed by the
    registration of its last member, whose targets are then all known, so each cycle
    is refused exactly there. Unregistered targets end the walk (resolution checks
    them; ``params._DEFAULT_STACK`` is the backstop for cycles this cannot see, e.g.
    through component kinds).
    """
    if slot != "planner":
        return

    def default_targets(policy_infos):
        return [(i.name, i.default.type) for i in policy_infos
                if i.is_ref and i.meta.kind == "planner" and isinstance(i.default.type, str)]

    def infos_of(target: str):
        if target == name:
            return infos
        other = _REGISTRY.get(("planner", target))
        return declared_params(other.params_cls) if other is not None else None

    def walk(current: str, policy_infos, trail: list[str], seen: set[str]) -> None:
        for param_name, target in default_targets(policy_infos):
            step = [*trail, f"{current}.{param_name}"]
            if target == name:
                raise PolicyRegistrationError(
                    f"{short}: ref param defaults form a cycle ({' -> '.join([*step, target])}), so "
                    f"resolving {name} at its defaults would never terminate. Make one default in "
                    "the cycle a leaf (a policy with no dispatch ref of its own)."
                )
            if target in seen:
                continue
            seen.add(target)
            target_infos = infos_of(target)
            if target_infos is not None:
                walk(target, target_infos, step, seen)

    walk(name, infos, [], set())


def _str_arms(infos) -> list[str]:
    """The battery's non-default str-choice arms, ``"param=value"`` in declared order."""
    return [f"{i.name}={c}" for i in infos if i.type is str and i.meta.choices is not None
            for c in i.meta.choices if c != i.default]


def _checked_arm_budgets(short: str, infos, budgets) -> Mapping[str, int]:
    """Validate ``arm_budgets_ms``; keys of a ref param are left to the battery test."""
    if budgets is None:
        return MappingProxyType({})
    if not isinstance(budgets, Mapping):
        raise PolicyRegistrationError(f"{short}: arm_budgets_ms must be a mapping 'param=value' -> ms.")
    refs = {i.name for i in infos if i.is_ref}
    arms = _str_arms(infos)
    for key, ms in budgets.items():
        if isinstance(ms, bool) or not isinstance(ms, int) or ms <= 0:
            raise PolicyRegistrationError(
                f"{short}: arm_budgets_ms[{key!r}] must be a positive integer (got {ms!r})."
            )
        param_name = key.partition("=")[0] if isinstance(key, str) else None
        if param_name in refs:
            continue
        if key not in arms:
            raise PolicyRegistrationError(
                f"{short}: arm_budgets_ms key {key!r} is not an arm of this policy. "
                f"Arms: {', '.join(arms) or '(none)'}."
            )
    return MappingProxyType(dict(budgets))


def _check_slot_base(cls: type, slot: str, where: str) -> None:
    entry = _SLOT_ENTRY_POINTS.get(slot)
    if entry is None:
        return
    if not (isinstance(cls, type) and callable(getattr(cls, entry, None))):
        raise PolicyRegistrationError(
            f"{where}: a {slot} policy class must define a callable {entry}(tick, params) "
            "(or subclass apps.container_logistics.policies.dispatch.base:DispatchPolicy, whose "
            f"{entry} runs a legacy decide(ctx))."
        )


def _checked_offload(short: str, offload: str | None, refs: tuple[tuple[str, str, bool], ...]) -> str:
    """``offload`` or its default: ``children`` for a node with a planner ref, else ``self``."""
    if offload is None:
        return "children" if any(kind == "planner" for _n, kind, _l in refs) else "self"
    if offload not in OFFLOAD_MODES:
        raise PolicyRegistrationError(f"{short}: offload {offload!r} is not one of {list(OFFLOAD_MODES)}.")
    return offload


def _checked_params(cls: type, where: str) -> type[PolicyParams]:
    params_cls = getattr(cls, "Params", None)
    if params_cls is None:
        raise PolicyRegistrationError(
            f"{where}: missing nested Params. Declare `@dataclass(frozen=True) class "
            "Params(PolicyParams): ...` inside the class (empty is fine)."
        )
    try:
        check_defaults(params_cls, where=f"{where}: defaults")
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

    Raises ``UnknownSlotError`` for an unknown slot and ``UnknownPolicyError`` (listing the registered names and
    the CLI command that lists them) for an unknown name. Never falls back.
    """
    slot_spec(slot)
    discover_builtin_policies()
    spec = _REGISTRY.get((slot, name)) if isinstance(name, str) else None
    if spec is None:
        registered = ", ".join(sorted(n for s, n in _REGISTRY if s == slot)) or "(none)"
        raise UnknownPolicyError(
            f"Unknown {slot} policy {name!r}. Registered {slot} policies: {registered}. "
            f"(List them: openride policy list {slot})"
        )
    return spec


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

    The default must be registered (else ``UnknownPolicyError``); ``UnknownSlotError``
    for an unknown slot.
    """
    spec = slot_spec(slot)
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
