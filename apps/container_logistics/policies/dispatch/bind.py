"""Run-start binding of a planner selection: ``RunWorld``, ``Declaration``, ``BoundTree``.

Single responsibility: turn a resolved selection tree plus its built instances into
the BOUND tree the framework runs every tick (plan ``plan_contract_final.md``
§2.2-§2.4, §2.7). Each node's wrapper is asked ONCE, pre-order,
``bind(params, world, top=<is root>) -> Declaration``:

- :class:`RunWorld` -- a read-only, READ-TRACKING view of the assignment profile minus
  the selection keys. Whatever a solver needs from the scenario (cooperation menu,
  candidate knobs, ``max_travel_time_pickup``) it reads from here; the keys a node
  reads at bind are frozen on its :class:`BoundNode` (``world_reads``), and the
  tree's union keys the runner caches.
- :class:`Declaration` -- what a node declares at bind: the cross-company
  ``(owner_haulier, carrier_haulier)`` pairs (TOP only, inside the scenario's
  declared ceiling), a ``candidate_hint`` a caller may read, plus provenance / stamp
  / run-name / run-stats fields consumed by later workstreams.
- :class:`BoundNode` / :class:`BoundTree` -- the bound selection: node, spec,
  instance (the object injections reach), the wrapper the framework calls, the
  declaration and the reads.

Framework rules checked here: message 23 (a top declaration outside the ceiling
``assignment.constraints.declared_partnerships``, or malformed) and message 24 (a
non-top node declaring more than the top, or a top-only field). Both are
``PolicyError`` and fail the run before any agent spawns.

The framework names no solver here: ownership is ``haulier_of`` on the two
documents, and who may serve whom across companies is exactly what the top node
declared.

Leaf module: stdlib, ``policies.framework`` and ``policies.compat.selection_keys`` at
module top; ``assignment.constraints``, ``dispatch.profile`` and
``compat.unregistered`` are imported locally.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterator, Mapping

from apps.container_logistics.policies.compat.selection_keys import LEGACY_SELECTION_KEYS
from apps.container_logistics.policies.framework.errors import PolicyError

logger = logging.getLogger(__name__)

__all__ = [
    "BoundNode",
    "BoundTree",
    "Declaration",
    "FRAMEWORK_SELECTION_KEYS",
    "RunWorld",
    "bind_profile",
    "bind_tree",
    "world_digest",
]

#: Profile keys the FRAMEWORK uses to select and capture a planner: never world data.
FRAMEWORK_SELECTION_KEYS: tuple[str, ...] = ("policies", "policies_format", "policy_capture")
_EXCLUDED = frozenset(FRAMEWORK_SELECTION_KEYS) | frozenset(LEGACY_SELECTION_KEYS)
_TOP_PATH = "planner"


class RunWorld(Mapping):
    """Read-only, read-tracking view of an assignment profile minus the selection keys.

    Excluded: :data:`FRAMEWORK_SELECTION_KEYS` and
    ``compat.selection_keys.LEGACY_SELECTION_KEYS``. Values are the profile's own
    objects (read-only by contract). Every key accessed through ``[]``, ``get`` or
    ``in`` is recorded in :attr:`reads`; iterating records every key. Framework code
    that must not count as a solver read (capture, the stamp) uses :meth:`unread`.
    """

    __slots__ = ("_data", "_reads")

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = MappingProxyType(dict(data))
        self._reads: set[str] = set()

    @classmethod
    def from_profile(cls, profile: Mapping[str, Any] | None) -> "RunWorld":
        """The world of assignment profile ``profile`` (``None`` -> empty)."""
        return cls({k: v for k, v in (profile or {}).items() if k not in _EXCLUDED})

    def __getitem__(self, key: str) -> Any:
        self._reads.add(key)
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        self._reads.add(key)
        return self._data.get(key, default)

    def __contains__(self, key: object) -> bool:
        self._reads.add(key)  # type: ignore[arg-type]
        return key in self._data

    def __iter__(self) -> Iterator[str]:
        self._reads.update(self._data)
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"RunWorld({sorted(self._data)})"

    @property
    def reads(self) -> frozenset[str]:
        """Every key read so far."""
        return frozenset(self._reads)

    def unread(self) -> Mapping[str, Any]:
        """The underlying read-only mapping, WITHOUT recording reads (framework use only)."""
        return self._data


@dataclass(frozen=True)
class Declaration:
    """What one node declares at bind (plan §2.3). Every field defaults to "nothing".

    - ``cross_company`` -- ``(owner_haulier, carrier_haulier)`` pairs whose carrier's
      trucks may serve the owner's orders. TOP only; inside the ceiling (message 23).
    - ``candidate_hint`` -- read by a CALLER through ``tick.callee(ref)`` (e.g. a
      market prebuilds spatial pairs once per round for an inner that hints
      ``"spatial"``). Not validated.
    - ``provenance`` -- JSON, stamped per node path (consumed by WS4b).
    - ``stamp_settings`` -- TOP only; merged into the run's settings (WS4b).
    - ``run_name_segments`` -- TOP only; appended to the run name (WS4b).
    - ``run_stats`` -- TOP only; a run-stats aggregator CLASS (WS4a).
    """

    cross_company: frozenset[tuple[str, str]] = frozenset()
    candidate_hint: str | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)
    stamp_settings: Mapping[str, Any] = field(default_factory=dict)
    run_name_segments: tuple[str, ...] = ()
    run_stats: type | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "cross_company", frozenset(self.cross_company or ()))
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance or {})))
        object.__setattr__(self, "stamp_settings", MappingProxyType(dict(self.stamp_settings or {})))
        object.__setattr__(self, "run_name_segments", tuple(self.run_name_segments or ()))


@dataclass(frozen=True, eq=False)
class BoundNode:
    """One bound node of the selection.

    ``node`` is its ``ResolvedDispatch``; ``spec`` its registration (``None`` for an
    unregistered instance); ``instance`` the built object (what injections such as
    ``set_rng`` reach, and what the parent holds as ``inner``); ``solver`` the object
    whose ``solve_tick(tick, params)`` the framework calls (``instance`` itself for a
    registered policy, a ``compat.unregistered`` adapter otherwise); ``declaration``
    what its ``bind`` returned; ``world_reads`` the keys that ``bind`` read;
    ``path`` ``planner`` for the top, ``planner.inner`` below it.
    """

    node: Any
    spec: Any
    instance: Any
    solver: Any
    declaration: Declaration
    world_reads: frozenset[str]
    inner: "BoundNode | None"
    path: str

    @property
    def name(self) -> str:
        return self.node.name

    @property
    def version(self) -> str:
        return self.node.version

    @property
    def params(self) -> Any:
        """The node's validated params (``None`` for an unregistered instance)."""
        return self.node.params

    @property
    def offload(self) -> str:
        """``"self"``: the framework offloads this node's ``solve_tick``; ``"children"``: it offloads its own calls."""
        return self.spec.offload if self.spec is not None else "self"


@dataclass(frozen=True, eq=False)
class BoundTree:
    """The bound selection: :attr:`top` and its ``inner`` chain (single ``inner``, G8)."""

    top: BoundNode
    world_reads: frozenset[str]

    @property
    def resolved(self) -> Any:
        """The top's ``ResolvedDispatch`` tree."""
        return self.top.node

    @property
    def declaration(self) -> Declaration:
        return self.top.declaration

    @property
    def nodes(self) -> tuple[BoundNode, ...]:
        """Pre-order (top first)."""
        out, node = [], self.top
        while node is not None:
            out.append(node)
            node = node.inner
        return tuple(out)

    @property
    def leaf(self) -> BoundNode:
        """The innermost node."""
        return self.nodes[-1]

    def child_for(self, node: BoundNode, ref: Any) -> BoundNode:
        """The bound child ``node`` delegates to through ``ref`` (its ``inner`` value or the param name).

        Raises ``PolicyError`` when ``node`` has no such child.
        """
        child = node.inner
        if child is not None:
            if isinstance(ref, str) and ref == "inner":
                return child
            value = getattr(node.params, "inner", None) if node.params is not None else None
            if value is not None and (ref is value or ref == value):
                return child
        raise PolicyError(
            f"planner policy {node.name} at {node.path} has no bound child for {ref!r}; a node "
            "calls only its own 'inner' (single-inner nesting)."
        )

    def digest(self, profile: Mapping[str, Any] | None) -> str:
        """:func:`world_digest` of ``profile`` over this tree's world reads."""
        return world_digest(profile, self.world_reads)


def world_digest(profile: Mapping[str, Any] | None, keys: frozenset[str]) -> str:
    """A stable text of ``profile``'s values at ``keys`` (an absent key is distinct from any value)."""
    prof = profile or {}
    return json.dumps({k: ([1, prof[k]] if k in prof else [0]) for k in sorted(keys)},
                      sort_keys=True, default=str)


def _call_bind(solver: Any, params: Any, world: RunWorld, *, top: bool, where: str) -> Declaration:
    bind = getattr(solver, "bind", None)
    if bind is None:
        return Declaration()
    declaration = bind(params, world, top=top)
    if not isinstance(declaration, Declaration):
        raise PolicyError(f"{where}: bind() returned {type(declaration).__name__}, expected a Declaration.")
    return declaration


def _check_top(declaration: Declaration, profile: Mapping[str, Any], where: str) -> None:
    """Message 23: the top's ``cross_company`` is well-formed and inside the declared ceiling."""
    from apps.container_logistics.assignment import constraints as assign_constraints  # local: leaf module

    bad = sorted(repr(p) for p in declaration.cross_company
                 if not (isinstance(p, tuple) and len(p) == 2 and all(isinstance(x, str) and x for x in p)
                         and p[0] != p[1]))
    if bad:
        raise PolicyError(
            f"{where}: bind() declared malformed cross-company pair(s) {', '.join(bad)}; each must be an "
            "(owner_haulier, carrier_haulier) pair of two different non-empty strings."
        )
    outside = sorted(declaration.cross_company - assign_constraints.declared_partnerships(dict(profile or {})))
    if outside:
        shown = ", ".join(f"({o!r}, {c!r})" for o, c in outside[:8]) + (" ..." if len(outside) > 8 else "")
        raise PolicyError(
            f"{where}: bind() declared cross-company pair(s) {shown} (owner, carrier) that no structure of "
            "the scenario declares. A solver may let a carrier serve another company's orders only where the "
            "scenario's structure menu pairs them (the union of every structure's adjacency); fix the "
            "solver's bind() or the scenario's structures."
        )


def _check_inner(declaration: Declaration, top: Declaration, where: str) -> None:
    """Message 24: a non-top node declares nothing the top did not, and no top-only field."""
    wider = sorted(declaration.cross_company - top.cross_company)
    extras = [name for name, value in (("stamp_settings", declaration.stamp_settings),
                                       ("run_name_segments", declaration.run_name_segments),
                                       ("run_stats", declaration.run_stats)) if value]
    if wider or extras:
        what = []
        if wider:
            what.append(f"cross-company pair(s) {', '.join(repr(p) for p in wider[:8])} the top did not declare")
        if extras:
            what.append(f"the top-only field(s) {', '.join(extras)}")
        raise PolicyError(
            f"{where}: a nested node's bind() declared {' and '.join(what)}. Only the top node of the selection "
            "declares cross-company pairs (an inner may declare a subset), stamp settings, run-name segments "
            "and run stats."
        )


def _solver_for(node: Any, instance: Any, profile: Mapping[str, Any]) -> Any:
    if node.source != "unregistered":
        return instance
    from apps.container_logistics.policies.compat.unregistered import UnregisteredPairLeaf, legacy_spatial_rule

    return UnregisteredPairLeaf(instance, spatial=legacy_spatial_rule(instance, profile))


def bind_tree(node: Any, top_instance: Any, profile: Mapping[str, Any] | None) -> BoundTree:
    """Bind the resolved selection ``node`` with its built top instance against ``profile``.

    Walks ``node``'s ``inner`` chain and the instances' ``inner`` attributes together
    (the construction protocol of ``dispatch.profile.build_policy_tree``, so each
    child instance IS the object at ``parent.inner``), calls each wrapper's ``bind``
    pre-order with a fresh :class:`RunWorld`, and checks messages 23 / 24. Pure in
    ``(params, world)``. Raises ``PolicyError``.
    """
    from apps.container_logistics.policies.framework.registry import spec_for_class

    prof = dict(profile or {})
    chain: list[tuple[Any, Any, str]] = []
    n, inst, path = node, top_instance, _TOP_PATH
    while n is not None:
        chain.append((n, inst, path))
        if n.inner is not None:
            child = getattr(inst, "inner", None)
            if child is None:
                raise PolicyError(
                    f"planner policy {n.name} at {path} resolves an inner {n.inner.name} but its instance "
                    f"{type(inst).__qualname__} holds no 'inner' (build it with inner=, inner_node=)."
                )
            inst = child
        n, path = n.inner, f"{path}.inner"
    bound: list[tuple[Any, Any, Any, Any, Declaration, frozenset[str], str]] = []
    top_declaration: Declaration | None = None
    for i, (n, inst, path) in enumerate(chain):
        where = f"planner policy {n.name} at {path}"
        solver = _solver_for(n, inst, prof)
        world = RunWorld.from_profile(prof)
        declaration = _call_bind(solver, n.params, world, top=(i == 0), where=where)
        if i == 0:
            _check_top(declaration, prof, where)
            top_declaration = declaration
        else:
            _check_inner(declaration, top_declaration, where)
        spec = spec_for_class(type(inst)) if n.source != "unregistered" else None
        bound.append((n, spec, inst, solver, declaration, world.reads, path))
    inner: BoundNode | None = None
    for n, spec, inst, solver, declaration, reads, path in reversed(bound):
        inner = BoundNode(node=n, spec=spec, instance=inst, solver=solver, declaration=declaration,
                          world_reads=reads, inner=inner, path=path)
    reads_union = frozenset().union(*(b[5] for b in bound))
    return BoundTree(top=inner, world_reads=reads_union)


def bind_profile(profile: Mapping[str, Any], instance: Any = None) -> BoundTree:
    """THE run-start binding of assignment profile ``profile`` (sim main, run-start check, agent).

    ``instance`` is an already-built policy object to fit into the profile's selection
    (``AssignmentApp._solver``; ``dispatch.profile.bind_instance``), else the tree is
    built fresh. Raises ``PolicyError``.
    """
    from .profile import bind_instance, build_policy_tree, resolve_dispatch_for_profile

    node = resolve_dispatch_for_profile(profile)
    if instance is None:
        return bind_tree(node, build_policy_tree(node), profile)
    top, bound = bind_instance(node, instance)
    return bind_tree(bound, top, profile)
