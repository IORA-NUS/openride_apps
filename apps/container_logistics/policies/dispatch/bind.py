"""Run-start binding of a planner selection: ``RunWorld``, ``Declaration``, ``BoundTree``.

Single responsibility: turn a resolved selection tree plus its built instances into
the BOUND tree the framework runs every tick (plan ``plan_contract_final.md``
§2.2-§2.4, §2.7). Each node's wrapper is asked ONCE, pre-order,
``bind(params, world, top=<is root>) -> Declaration``:

- :class:`RunWorld` -- a read-only, READ-TRACKING view of the assignment profile minus
  the selection keys. Whatever a solver needs from the scenario (cooperation menu,
  candidate knobs, ``max_travel_time_pickup``) it reads from here; the keys a node
  reads at bind are frozen on its :class:`BoundNode` (``world_reads``), and the
  tree's union keys the runner caches. Every value it hands out is the reading
  world's own DEEP COPY, and the world itself holds only a FROZEN copy of the profile
  (taken when it is built: each value pickled, immutable bytes), so no solver can reach
  the profile the sim main ships to the agents through it -- not through a value, and
  not through the framework-private state table either (review H1, N5). Its bulk-access
  refusals are contract violations (:class:`WorldAccessError`, also a ``TypeError``;
  review N9).
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
``PolicyError`` and fail the run before any agent spawns. Also refused here, against a
DISHONEST solver (review H1, L1, M2): a ``bind`` that changed the profile (the
ceiling is computed BEFORE any bind runs, and the profile's fingerprint is compared
after every bind), a ``bind`` that raised anything but a ``PolicyError`` (re-raised as
:class:`BindError` naming the node), and a run-stats aggregator whose ``meta_key``
would overwrite a framework key of ``run_config.meta``
(``run_stats.check_meta_key``). Message 25
(:func:`refuse_unread_patches`) is the override layer's check against a bound tree: a
per-run patch of a world key no node read at bind changes nothing the planner sees.

The framework names no solver here: ownership is ``haulier_of`` on the two
documents, and who may serve whom across companies is exactly what the top node
declared.

Leaf module: stdlib, ``policies.framework`` and ``policies.compat.selection_keys`` at
module top; ``assignment.constraints``, ``dispatch.profile`` and
``compat.unregistered`` are imported locally.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import pickle
import weakref
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterator, Mapping

from apps.container_logistics.policies.compat.selection_keys import LEGACY_SELECTION_KEYS
from apps.container_logistics.policies.framework.errors import DispatchContractViolation, PolicyError

from .run_stats import check_meta_key

logger = logging.getLogger(__name__)

__all__ = [
    "BindError",
    "BoundNode",
    "BoundTree",
    "Declaration",
    "FRAMEWORK_SELECTION_KEYS",
    "PLANNING_DEFAULT_MAX_HOLD_SECONDS",
    "PLANNING_FORESIGHT",
    "PLANNING_KEYS",
    "PLANNING_MAX_HOLD_LIMIT",
    "RunWorld",
    "SolverInitError",
    "WorldAccessError",
    "bind_profile",
    "bind_tree",
    "normalise_planning",
    "refuse_unread_patches",
    "value_fingerprint",
    "world_digest",
    "world_snapshot",
]


class BindError(PolicyError):
    """A ``bind`` that broke the bind contract: raised a non-``PolicyError``, or changed the profile."""


class SolverInitError(PolicyError):
    """A solver class whose construction (``__init__``) raised something other than a ``PolicyError`` (L1).

    Raised by ``dispatch.profile.build_policy_tree`` naming the solver, so every run-start
    path (sim main: exit 2; ``validate-run-policies``: a JSON refusal) reports it like a
    refused ``bind``.
    """


class WorldAccessError(DispatchContractViolation, TypeError):
    """A refused :class:`RunWorld` access (bulk read, pickling): a contract violation that is still a ``TypeError``.

    Inside ``solve_tick`` it is message 18/19 (the tick or scope is discarded) instead of an
    exception that aborts every tick; inside ``bind`` a ``PolicyError`` that fails the run
    with this message (review N9). ``except TypeError`` code keeps working.
    """

#: Profile keys the FRAMEWORK uses to select and capture a planner: never world data.
FRAMEWORK_SELECTION_KEYS: tuple[str, ...] = ("policies", "policies_format", "policy_capture")
_EXCLUDED = frozenset(FRAMEWORK_SELECTION_KEYS) | frozenset(LEGACY_SELECTION_KEYS)
_TOP_PATH = "planner"


_SCALARS = (str, int, float, bool, type(None))


def value_fingerprint(value: Any) -> bytes:
    """A byte string equal for two values iff they are structurally identical (framework use).

    ``pickle`` protocol 5: the bytes depend on the value's content, types, dict order and
    which sub-objects are SHARED (its memo is identity-based within one dump) -- all of
    which only a mutation can change -- and never on reference counts or string
    interning (``marshal`` from format 1 on depends on one or the other). ~1.8 us per tick
    document. A value ``pickle`` refuses falls back to ``repr``.
    """
    try:
        return pickle.dumps(value, 5)
    except Exception:  # noqa: BLE001 - a fingerprint must never raise
        return b"r" + repr(value).encode("utf-8", "replace")


class _Pickled:
    """A frozen world value: its ``pickle`` bytes (immutable); every read unpickles a fresh deep copy."""

    __slots__ = ("blob",)

    def __init__(self, blob: bytes) -> None:
        self.blob = blob


class _Copied:
    """A frozen world value ``pickle`` refused: a private deep copy, deep-copied again on every read."""

    __slots__ = ("value", "error")

    def __init__(self, value: Any, error: BaseException | None) -> None:
        self.value = value
        self.error = error


def _freeze(value: Any) -> Any:
    """``value`` frozen at world construction: scalars as they are, anything else :class:`_Pickled`."""
    if type(value) in _SCALARS:
        return value
    try:
        return _Pickled(pickle.dumps(value, 5))
    except Exception:  # noqa: BLE001 - not picklable: a private deep copy
        pass
    try:
        return _Copied(copy.deepcopy(value), None)
    except Exception as exc:  # noqa: BLE001 - not copyable either: raised on read, as before
        return _Copied(None, exc)


def _thaw(frozen: Any) -> Any:
    """A fresh deep copy of a :func:`_freeze` d value (``pickle.loads`` equals ``copy.deepcopy`` on profile data)."""
    kind = type(frozen)
    if kind is _Pickled:
        return pickle.loads(frozen.blob)
    if kind is _Copied:
        if frozen.error is not None:
            raise frozen.error
        return copy.deepcopy(frozen.value)
    return frozen


class _WorldState:
    """What one :class:`RunWorld` holds: the FROZEN values, the keys read, the copies handed out.

    ``data`` is a read-only mapping of frozen values (:func:`_freeze`), taken when the
    world is built, so nothing reachable from here is the caller's profile (review N5):
    mutating a thawed copy, or the state table itself, changes no other tick and no agent.
    """

    __slots__ = ("data", "reads", "copies")

    def __init__(self, data: Mapping[str, Any]) -> None:
        self.data = MappingProxyType({k: _freeze(v) for k, v in data.items()})
        self.reads: set[Any] = set()
        self.copies: dict[str, Any] = {}

    def value(self, key: str) -> Any:
        """``data[key]`` as this world's own deep copy (made once per key; scalars as they are)."""
        try:
            return self.copies[key]
        except KeyError:
            pass
        value = _thaw(self.data[key])
        self.copies[key] = value
        return value


#: Each world's state, held OFF the instance: a ``RunWorld`` has no attribute that reads
#: a value without recording it or that resets the record (review L2).
_WORLD_STATE: "weakref.WeakKeyDictionary[RunWorld, _WorldState]" = weakref.WeakKeyDictionary()


class RunWorld(Mapping):
    """Read-only, read-tracking view of an assignment profile minus the selection keys.

    Excluded: :data:`FRAMEWORK_SELECTION_KEYS` and
    ``compat.selection_keys.LEGACY_SELECTION_KEYS``.

    Semantics, pinned by ``tests/test_framework_integrity.py`` (review H1, L2):

    - **Values are copies.** ``world[key]`` / ``world.get(key)`` return this world's own
      deep copy of the profile value (made on the first read of that key, then the same
      object on every later read through this world). Mutating it changes nothing the
      framework, another tick or the agents see.
    - **Reads are recorded per key.** ``world[key]``, ``world.get(key)`` and
      ``key in world`` record ``key`` in :attr:`reads` (an absent key too).
    - **The key list is free.** ``iter(world)``, ``list(world)``, ``sorted(world)`` and
      ``len(world)`` reveal which keys exist and record nothing.
    - **No bulk value access.** ``keys()``, ``items()``, ``values()``, ``dict(world)`` and
      ``**world`` raise :class:`WorldAccessError` (a ``TypeError`` and a contract
      violation: message 18/19 in a tick, a refused bind at run start): a splat would either record every key (and so
      silently disable message 25 for every key) or record none (and hide the reads).
      Read the keys you use, one by one.
    - **Nothing on the instance bypasses the record.** The state lives in a
      framework-private table, not in attributes; framework code that must not count
      as a solver read (capture, bench) uses :func:`world_snapshot`. ``copy.copy`` /
      ``copy.deepcopy`` return the world itself (its values are already copies, and
      reads through it keep being recorded); pickling is refused (:class:`WorldAccessError`).
    - **The profile is not reachable.** The world holds a frozen copy taken when it was
      built; the caller's profile is never referenced (review N5).
    """

    __slots__ = ("__weakref__",)

    def __init__(self, data: Mapping[str, Any]) -> None:
        _WORLD_STATE[self] = _WorldState(data)

    @classmethod
    def from_profile(cls, profile: Mapping[str, Any] | None) -> "RunWorld":
        """The world of assignment profile ``profile`` (``None`` -> empty)."""
        return cls({k: v for k, v in (profile or {}).items() if k not in _EXCLUDED})

    def __getitem__(self, key: str) -> Any:
        state = _WORLD_STATE[self]
        state.reads.add(key)
        return state.value(key)

    def get(self, key: str, default: Any = None) -> Any:
        state = _WORLD_STATE[self]
        state.reads.add(key)
        return state.value(key) if key in state.data else default

    def __contains__(self, key: object) -> bool:
        state = _WORLD_STATE[self]
        state.reads.add(key)
        return key in state.data

    def __iter__(self) -> Iterator[str]:
        return iter(tuple(_WORLD_STATE[self].data))

    def __len__(self) -> int:
        return len(_WORLD_STATE[self].data)

    def __repr__(self) -> str:
        return f"RunWorld({sorted(_WORLD_STATE[self].data)})"

    def _no_bulk(self, what: str) -> WorldAccessError:
        return WorldAccessError(
            f"RunWorld.{what} is not supported: read the world keys you use one by one "
            "(world[key] / world.get(key)) so the framework records exactly those reads; "
            "iterate the world for its key list."
        )

    def keys(self) -> Any:  # dict(world) and **world call keys(): refused with them
        raise self._no_bulk("keys() (and dict(world) / **world)")

    def items(self) -> Any:
        raise self._no_bulk("items()")

    def values(self) -> Any:
        raise self._no_bulk("values()")

    def __eq__(self, other: object) -> bool:
        return self is other

    __hash__ = object.__hash__

    def __copy__(self) -> "RunWorld":
        return self

    def __deepcopy__(self, memo: Any) -> "RunWorld":
        return self

    def __reduce_ex__(self, protocol: Any) -> Any:
        raise WorldAccessError("a RunWorld cannot be pickled; read the world keys you use one by one")

    @property
    def reads(self) -> frozenset[str]:
        """Every key read so far (a fresh frozenset: changing it changes nothing)."""
        return frozenset(_WORLD_STATE[self].reads)


def world_snapshot(world: RunWorld) -> dict[str, Any]:
    """A deep copy of ``world``'s values, WITHOUT recording reads (framework use only: capture, bench)."""
    return {k: _thaw(v) for k, v in _WORLD_STATE[world].data.items()}


@dataclass(frozen=True)
class _Malformed:
    """A ``cross_company`` entry that could not even be held in a set (e.g. a list): kept as its repr.

    Never well-formed, so :func:`_check_top` reports it in message 23's malformed form
    instead of the declaration raising a bare ``TypeError`` inside the solver (review L1).
    """

    text: str

    def __repr__(self) -> str:
        return self.text


def _pair_set(value: Any) -> frozenset:
    """``frozenset(value)``; an unhashable entry, or a value that is not iterable, becomes a :class:`_Malformed`."""
    if not value:
        return frozenset()
    try:
        return frozenset(value)
    except TypeError:
        pass
    try:
        items = list(value)
    except TypeError:
        return frozenset({_Malformed(repr(value))})
    out = set()
    for item in items:
        try:
            hash(item)
        except TypeError:
            item = _Malformed(repr(item))
        out.add(item)
    return frozenset(out)


#: The keys of ``Declaration.planning``, in stamp order.
PLANNING_KEYS: tuple[str, ...] = ("at_day_start", "every_steps", "foresight", "max_hold_seconds")
#: ``planning["foresight"]``: ``"none"`` (released orders only) or ``"day"`` (every order of
#: the planning window, released or not: an ORACLE run).
PLANNING_FORESIGHT: tuple[str, ...] = ("none", "day")
#: The largest ``max_hold_seconds`` a declaration may state (refusal R2). The run start's
#: planning hold cap (refusal R4, the sim main) is lower.
PLANNING_MAX_HOLD_LIMIT = 600
#: ``max_hold_seconds`` when a declaration leaves it out.
PLANNING_DEFAULT_MAX_HOLD_SECONDS = 30
_PLANNING_DEFAULTS: Mapping[str, Any] = MappingProxyType({
    "at_day_start": False, "every_steps": None, "foresight": "none",
    "max_hold_seconds": PLANNING_DEFAULT_MAX_HOLD_SECONDS,
})
_PLANNING_SHAPE = ("planning is {at_day_start: bool, every_steps: int >= 1 or null, foresight: 'none' | 'day', "
                   f"max_hold_seconds: int 1..{PLANNING_MAX_HOLD_LIMIT}}}.")


def _frozen_planning(value: Any) -> Any:
    """``Declaration.planning`` as stored: ``None``, a read-only copy of a mapping, else a malformed marker."""
    if value is None:
        return None
    if isinstance(value, Mapping):
        try:
            return MappingProxyType(copy.deepcopy(dict(value)))
        except Exception:  # noqa: BLE001 - an uncopyable value is still refused by _check_top
            return MappingProxyType(dict(value))
    try:
        return _Malformed(repr(value))
    except Exception:  # noqa: BLE001
        return _Malformed(f"<{type(value).__name__}>")


def _int_like(value: Any) -> bool:
    return type(value) is int


def _planning_problem(value: Any) -> str | None:
    """Why ``value`` is not a valid planning declaration, or ``None`` (R2)."""
    if not isinstance(value, Mapping):
        return "it is not a mapping"
    unknown = sorted(repr(k) for k in value if k not in PLANNING_KEYS)
    if unknown:
        return f"unknown key(s) {', '.join(unknown)}"
    at_day_start = value.get("at_day_start", False)
    if type(at_day_start) is not bool:
        return f"at_day_start must be true or false (got {at_day_start!r})"
    every = value.get("every_steps")
    if every is not None and not (_int_like(every) and every >= 1):
        return f"every_steps must be an integer >= 1 or null (got {every!r})"
    foresight = value.get("foresight", "none")
    if type(foresight) is not str or foresight not in PLANNING_FORESIGHT:
        return f"foresight must be 'none' or 'day' (got {foresight!r})"
    hold = value.get("max_hold_seconds", PLANNING_DEFAULT_MAX_HOLD_SECONDS)
    if not (_int_like(hold) and 1 <= hold <= PLANNING_MAX_HOLD_LIMIT):
        return f"max_hold_seconds must be an integer in 1..{PLANNING_MAX_HOLD_LIMIT} (got {hold!r})"
    if not at_day_start and every is None:
        return "it never plans (at_day_start is false and every_steps is null)"
    return None


def normalise_planning(value: Any, where: str) -> Mapping[str, Any] | None:
    """A planning declaration checked (refusal R2) and completed: every :data:`PLANNING_KEYS` key, read-only.

    ``None`` stays ``None`` (no planning). Raises ``PolicyError`` naming ``where``.
    """
    if value is None:
        return None
    problem = _planning_problem(value)
    if problem is not None:
        shown = repr(dict(value)) if isinstance(value, Mapping) else repr(value)
        raise PolicyError(f"{where}: bind() declared planning {shown}: {problem}. {_PLANNING_SHAPE}")
    return MappingProxyType({key: value.get(key, _PLANNING_DEFAULTS[key]) for key in PLANNING_KEYS})


def _pair_order(pair: Any) -> tuple:
    """Sort key: well-formed pairs in their natural order first, anything else by repr."""
    if isinstance(pair, tuple) and all(isinstance(x, str) for x in pair):
        return (0, pair, "")
    return (1, (), repr(pair))


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
    - ``wire_note_keys`` -- TOP only; the ``solver_notes`` keys this solver writes into
      fields the framework publishes on the wire itself (a market's ``pool_id`` /
      ``market_round``). Any other note key naming such a field is refused at the tick
      (message 18; ``invariants.check_wire_notes``, review L3), so a solver can never
      claim, say, market provenance it did not declare at bind.
    - ``planning`` -- TOP only (message 24); ``None`` (the default: an online solver) or
      ``{at_day_start, every_steps, foresight, max_hold_seconds}`` (:data:`PLANNING_KEYS`):
      the agent also runs PLANNING ticks (``Tick.kind == "plan"``, which commit no pairs)
      at each day start and/or every ``every_steps`` steps, with orders up to the end of
      the planning window when ``foresight == "day"`` (an ORACLE run, stamped). Checked by
      :func:`_check_top` (refusal R2) and stored normalised (every key present) and
      read-only (:func:`normalise_planning`).
    """

    cross_company: frozenset[tuple[str, str]] = frozenset()
    candidate_hint: str | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)
    stamp_settings: Mapping[str, Any] = field(default_factory=dict)
    run_name_segments: tuple[str, ...] = ()
    run_stats: type | None = None
    wire_note_keys: frozenset[str] = frozenset()
    planning: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "cross_company", _pair_set(self.cross_company))
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance or {})))
        object.__setattr__(self, "stamp_settings", MappingProxyType(dict(self.stamp_settings or {})))
        object.__setattr__(self, "run_name_segments", tuple(self.run_name_segments or ()))
        object.__setattr__(self, "wire_note_keys", _pair_set(self.wire_note_keys))
        object.__setattr__(self, "planning", _frozen_planning(self.planning))


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
    """The bound selection: :attr:`top` and its ``inner`` chain (single ``inner``, G8).

    ``ceiling`` is the cross-company ceiling the top's declaration was checked against
    (``constraints.declared_partnerships`` of the profile, taken before any bind ran); the
    runner keeps the run-start one and refuses a re-bind that widens it (review N5).
    """

    top: BoundNode
    world_reads: frozenset[str]
    ceiling: frozenset = frozenset()

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
    """``solver.bind(params, world, top=top)``; anything but a ``PolicyError`` becomes a :class:`BindError` (L1)."""
    bind = getattr(solver, "bind", None)
    if bind is None:
        return Declaration()
    try:
        declaration = bind(params, world, top=top)
    except PolicyError:
        raise
    except Exception as exc:  # noqa: BLE001 - re-raised as a PolicyError naming the node
        raise BindError(
            f"{where}: bind() raised {type(exc).__name__}: {exc}. A bind() must return a Declaration or "
            "raise a PolicyError; fix the solver's bind()."
        ) from exc
    if not isinstance(declaration, Declaration):
        raise PolicyError(f"{where}: bind() returned {type(declaration).__name__}, expected a Declaration.")
    return declaration


def _profile_fingerprint(profile: Mapping[str, Any]) -> bytes:
    return value_fingerprint(dict(profile))


def _check_profile_unchanged(before: bytes, profile: Mapping[str, Any], where: str) -> None:
    """Refuse a ``bind`` after which the profile differs (review H1): it reaches the agents."""
    if _profile_fingerprint(profile) != before:
        raise BindError(
            f"{where}: bind() changed the assignment profile. A bind() is pure in (params, world): the "
            "profile is world data the sim main ships to every agent, and the cross-company ceiling is "
            "computed from it. Read the world, never write it."
        )


def _check_top(declaration: Declaration, ceiling: frozenset, where: str) -> None:
    """Message 23: the top's ``cross_company`` is well-formed and inside the declared ``ceiling``.

    ``ceiling`` is ``constraints.declared_partnerships`` of the profile, computed by
    :func:`bind_tree` BEFORE any node's ``bind`` ran (review H1). Also refuses a run-stats
    aggregator whose ``meta_key`` is not a free ``run_config.meta`` key (review M2).
    """
    bad = sorted((repr(p) for p in declaration.cross_company
                  if not (isinstance(p, tuple) and len(p) == 2 and all(isinstance(x, str) and x for x in p)
                          and p[0] != p[1])))
    if bad:
        raise PolicyError(
            f"{where}: bind() declared malformed cross-company pair(s) {', '.join(bad)}; each must be an "
            "(owner_haulier, carrier_haulier) pair of two different non-empty strings."
        )
    outside = sorted(declaration.cross_company - ceiling)
    if outside:
        shown = ", ".join(f"({o!r}, {c!r})" for o, c in outside[:8]) + (" ..." if len(outside) > 8 else "")
        raise PolicyError(
            f"{where}: bind() declared cross-company pair(s) {shown} (owner, carrier) that no structure of "
            "the scenario declares. A solver may let a carrier serve another company's orders only where the "
            "scenario's structure menu pairs them (the union of every structure's adjacency); fix the "
            "solver's bind() or the scenario's structures."
        )
    if declaration.run_stats is not None:
        check_meta_key(declaration.run_stats, where)
    keys = sorted((repr(k) for k in declaration.wire_note_keys if not (isinstance(k, str) and k)), key=str)
    if keys:
        raise PolicyError(f"{where}: bind() declared wire_note_keys {', '.join(keys)}; each must be a non-empty string.")
    normalise_planning(declaration.planning, where)  # refusal R2


def _check_inner(declaration: Declaration, top: Declaration, where: str) -> None:
    """Message 24: a non-top node declares nothing the top did not, and no top-only field."""
    wider = sorted(declaration.cross_company - top.cross_company, key=_pair_order)
    extras = [name for name, value in (("stamp_settings", declaration.stamp_settings),
                                       ("run_name_segments", declaration.run_name_segments),
                                       ("run_stats", declaration.run_stats),
                                       ("wire_note_keys", declaration.wire_note_keys)) if value]
    if declaration.planning is not None:
        extras.append("planning")
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


def _with_registered_badge(declaration: Declaration, spec: Any) -> Declaration:
    """The top's declaration with its ``@policy(badge=...)`` in the provenance, when the solver left it out (M13).

    A contract-only solver's ``bind`` (``DispatchPolicy``'s default) stamps no
    provenance, so its run would carry no badge although it registered one. Only the
    badge is filled in: ``sample_scope`` / ``candidate_settings`` describe how a solver
    used ``solver_lib.candidates``, which the framework cannot know, so it never
    claims them. A declaration that already carries a ``badge`` (every built-in top) is
    returned unchanged, so no built-in stamp moves a byte.
    """
    badge = getattr(spec, "badge", None) if spec is not None else None
    if badge is None or "badge" in declaration.provenance:
        return declaration
    return dataclasses.replace(declaration, provenance={"badge": badge, **declaration.provenance})


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
    ``(params, world)``, and held to it against a dishonest solver: the ceiling is
    computed before the first ``bind``, every world value a ``bind`` reads is its own
    copy, and the profile's fingerprint must be unchanged after each ``bind``. Raises
    ``PolicyError``.
    """
    from apps.container_logistics.assignment import constraints as assign_constraints  # local: leaf module
    from apps.container_logistics.policies.framework.registry import spec_for_class

    source = profile or {}
    before = _profile_fingerprint(source)
    # The ceiling, from a deep copy of the profile taken before ANY bind ran, so no bind
    # can widen it (review H1).
    ceiling = assign_constraints.declared_partnerships(copy.deepcopy(dict(source)))
    prof = dict(source)
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
        _check_profile_unchanged(before, source, where)
        spec = spec_for_class(type(inst)) if n.source != "unregistered" else None
        if i == 0:
            _check_top(declaration, ceiling, where)
            if declaration.planning is not None:
                declaration = dataclasses.replace(declaration,
                                                  planning=normalise_planning(declaration.planning, where))
            declaration = _with_registered_badge(declaration, spec)
            top_declaration = declaration
        else:
            _check_inner(declaration, top_declaration, where)
        bound.append((n, spec, inst, solver, declaration, world.reads, path))
    inner: BoundNode | None = None
    for n, spec, inst, solver, declaration, reads, path in reversed(bound):
        inner = BoundNode(node=n, spec=spec, instance=inst, solver=solver, declaration=declaration,
                          world_reads=reads, inner=inner, path=path)
    reads_union = frozenset().union(*(b[5] for b in bound))
    return BoundTree(top=inner, world_reads=reads_union, ceiling=frozenset(ceiling))


def refuse_unread_patches(tree: BoundTree, patched_world_keys: Any) -> None:
    """Message 25: refuse a per-run patch of world keys the bound ``tree`` never reads.

    ``patched_world_keys`` are the keys a per-run override wrote into the profile
    (``scenario_overrides.apply_cooperation_override`` returns them). A key outside
    ``tree.world_reads`` reached no node's ``bind``, and by the read rule (plan §2.4,
    M4: a ``bind`` reads every key its ``solve_tick`` will) no tick either, so every
    value of it would run the same experiment under a different label. Raises
    ``PolicyError``; a no-op for an empty ``patched_world_keys``.
    """
    unread = sorted(set(patched_world_keys or ()) - tree.world_reads)
    if not unread:
        return
    top = tree.top
    reads = ", ".join(sorted(tree.world_reads)) or "none"
    raise PolicyError(
        f"a per-run override patched world key(s) {', '.join(repr(k) for k in unread)}, but the planner "
        f"selection {top.name} (bound at {top.path}) reads no such key (it reads: {reads}). Every value of "
        "an unread key runs the same experiment, so the run is refused: drop the override, or select a "
        "planner whose bind() reads that key."
    )


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
