"""Assignment behavior profile -> the dispatch selection TREE that runs, and its mirrors.

Single responsibility: map an assignment agent's behavior ``profile`` onto the
dispatch slot (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P0.7 and P1.6; round 2
``docs/plans/policy_slots_round2_whole_tick.md`` §3.6, §5 C2):

- :func:`resolve_profile_policies` is THE profile resolver (agent, run-start check,
  run stamp and per-run overrides all use it): the compiled ``policies`` block when
  present, else the legacy keys (``strategy``, ``solver_params``,
  ``use_spatial_matching``, ``planner.topology``/``planner.market``) translated by
  :func:`legacy_dispatch_from_profile` and ``compat.legacy.wrap_legacy_leaf``.
- :func:`resolve_dispatch_for_profile` is its dispatch entry as a typed
  :class:`ResolvedDispatch` tree (:func:`dispatch_node`).
- :func:`build_policy_tree` instantiates a tree; :func:`bind_instance` fits an
  already-built policy object (``AssignmentApp._solver``) into a tree, or refuses;
  :func:`bind_profile_instance` does that for a profile. ``dispatch.bind.bind_profile``
  then binds the instances (``bind`` per node, messages 23/24).
- :func:`mirror_dispatch_selection` / :func:`check_mirrors` (the legacy mirrors,
  message 13) live in ``policies/compat/mirrors.py`` and are re-exported here.
- :func:`assert_assignment_profiles_resolvable` is the run-start check
  ``run_container_logistics_simulation.main`` runs before any agent spawns.

Everything raises ``PolicyError`` subclasses; nothing falls back silently.

Import contract (plan §2.7): imports only leaf ``policies`` modules at module top,
never ``apps.container_logistics.assignment.*`` (``dispatch/bind.py`` is imported
locally), so ``assignment.app`` can import it without a cycle. Framework side.
The framework never ``isinstance``-checks a helper base here: a node "has an inner"
when its registration declares an ``inner`` ref, and instances are walked through
their ``inner`` attribute (the construction protocol).
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Iterator, Mapping

from apps.container_logistics.policies.framework import (
    PolicyError,
    ResolvedPolicy,
    get_policy_spec,
    resolve_policies_block,
    spec_for_class,
)
from apps.container_logistics.policies.framework.params import version_echo, warn_version_drift
from apps.container_logistics.policies.framework.resolve import (
    ENTRY_KEYS,
    INNER_PARAM,
    drop_undeclared_legacy_params,
)

from apps.container_logistics.policies.compat.legacy import (
    LEGACY_LEAF_DEFAULT,
    check_policies_format,
    wrap_legacy_leaf,
)
# Re-exported from the compat quarantine until the shims are removed (moved verbatim
# from this module's "legacy mirrors" section, 35b5b92 lines 494-638).
from apps.container_logistics.policies.compat.mirrors import check_mirrors, mirror_dispatch_selection

from .base import DispatchPolicy
from .resolved import ResolvedDispatch, ResolvedSource

logger = logging.getLogger(__name__)

__all__ = [
    "COMPILED_ENTRY_KEYS",
    "as_dispatch_node",
    "assert_assignment_profiles_resolvable",
    "bind_instance",
    "bind_profile_instance",
    "build_policy_tree",
    "check_mirrors",
    "dispatch_node",
    "legacy_dispatch_from_profile",
    "mirror_dispatch_selection",
    "resolve_dispatch_for_profile",
    "resolve_profile_policies",
    "use_spatial_matching",
]

#: The keys of a compiled ``profile.policies.<slot>`` entry (``ResolvedPolicy.to_profile``).
COMPILED_ENTRY_KEYS = ("type", "version", "kind", "timing", "params")

_LEGACY_SOURCE_NOTE = (
    "(This profile has no 'policies' block: its selection is translated from the legacy keys "
    "strategy, solver_params, use_spatial_matching and planner.topology/planner.market; "
    "the fix goes there, or recompile the scenario.)"
)


# --- the run-start check ----------------------------------------------------------


def assert_assignment_profiles_resolvable(
    collection: Mapping[str, Any], *, patched_world_keys: Any = ()
) -> dict[str, Any]:
    """Raise ``PolicyError`` unless every assignment agent's dispatch resolves and runs.

    ``collection`` is the assignment behavior collection, ``{agent_id: behavior}``.
    Each profile's ``policies_format`` marker is checked first
    (``legacy.check_policies_format``: a round-1 flat block is refused), then it goes
    through :func:`resolve_dispatch_for_profile` (the shared
    framework resolver also refuses a non-executable timing), then
    :func:`check_mirrors` (message 13), then the run-start BINDING
    (``dispatch.bind.bind_tree`` over a fresh tree: each node's ``bind``, so a built-in
    top validates its cooperation arm, message 14, and the framework checks messages
    23 / 24), then ``dispatch.bind.refuse_unread_patches`` (message 25: a world key a
    per-run override patched, ``patched_world_keys``, that the bound tree never reads).
    The raised error is the
    original class (e.g. ``UnknownPolicyError``) with the agent id prefixed; a
    behavior that is not a mapping raises plain ``PolicyError``. Returns the bound
    trees, ``{agent_id: BoundTree}`` (the control plane's run preflight reports their
    ``world_reads``).
    """
    from .bind import bind_tree, refuse_unread_patches

    trees: dict[str, Any] = {}
    for agent_id, behavior in (collection or {}).items():
        if not isinstance(behavior, Mapping):
            raise PolicyError(
                f"assignment agent {agent_id!r}: behavior must be a mapping with a "
                f"'profile' (got {type(behavior).__name__})."
            )
        profile = behavior.get("profile") or {}
        try:
            check_policies_format(profile)
            node = resolve_dispatch_for_profile(profile)
            check_mirrors(profile, node)
            tree = bind_tree(node, build_policy_tree(node), profile)
            refuse_unread_patches(tree, patched_world_keys)
        except PolicyError as exc:
            raise type(exc)(f"assignment agent {agent_id!r}: {exc}") from exc
        trees[agent_id] = tree
    return trees


# --- profile -> resolved policies -------------------------------------------------


def _declares(spec: Any, key: str) -> bool:
    return key in spec.params_cls.__dataclass_fields__


def use_spatial_matching(prof: Mapping[str, Any]) -> bool:
    """The legacy ``use_spatial_matching`` flag, strictly boolean (absent means true).

    The pre-v2 code read it with ``bool(...)``; a non-boolean here is a malformed
    profile, so it raises instead of silently picking an arm (plan §2.6).
    """
    value = prof.get("use_spatial_matching", True)
    if not isinstance(value, bool):
        raise PolicyError(
            f"assignment profile use_spatial_matching must be true or false (got {value!r}, "
            f"a {type(value).__name__})."
        )
    return value


def legacy_dispatch_from_profile(prof: Mapping[str, Any]) -> dict[str, Any]:
    """Translate the legacy LEAF keys into a ``{"type", "timing", "params"}`` entry.

    ``type`` is ``strategy`` or the legacy leaf default (``legacy.LEGACY_LEAF_DEFAULT``).
    ``params`` keeps only the ``solver_params`` keys the policy's ``Params`` declares,
    via the framework's one drop rule (``framework.resolve.drop_undeclared_legacy_params``,
    logged at WARNING): legacy keys were never validated, and ``solver_params`` also
    feeds the framework's accounting, so a cost-blind policy legitimately ignores the
    dual-cycle keys. ``use_spatial_matching: false`` becomes ``candidates="sampled"``
    where declared. This is the per-scope solver only; :func:`resolve_profile_policies`
    wraps it in the profile's decomposition. Raises ``UnknownPolicyError`` for an
    unregistered ``strategy``.
    """
    name = prof.get("strategy") or LEGACY_LEAF_DEFAULT
    spec = get_policy_spec("planner", name)
    params = drop_undeclared_legacy_params(
        spec, prof.get("solver_params") or {}, "profile.strategy/solver_params"
    )
    if not use_spatial_matching(prof) and _declares(spec, "candidates"):
        params["candidates"] = "sampled"
    return {"type": name, "timing": "online", "params": params}


def resolve_profile_policies(prof: Mapping[str, Any]) -> tuple[dict[str, ResolvedPolicy], str]:
    """THE resolver of an assignment profile's policies: ``({slot: ResolvedPolicy}, source)``.

    Shared by the agent (:func:`resolve_dispatch_for_profile`), the run-start check
    and the run stamp / per-run override path
    (``scenario_overrides.current_policies_for_profile``), so they cannot disagree
    (review F4). A compiled ``profile.policies`` block is resolved with the same
    strictness as compile: an unknown slot or an entry key outside
    :data:`COMPILED_ENTRY_KEYS` raises (nothing is silently stripped). Its ``version``
    and ``kind`` (and each nested ref's ``version``) never select anything: they are a
    compile-time echo, the run stamp re-derives them from the registry, so the record
    is always what ran. A ``version`` must be a non-blank string or null (else
    ``PolicyParamsError``), and one that differs from the registered version is logged
    as ``POLICY VERSION DRIFT`` (WARNING). A round-1 FLAT block (``{type: GreedyNearest}``) resolves
    literally, as a central ``GreedyNearest`` (round-2 §3.6; the run-start mirror
    check then refuses such a bundle). Next to a block, ``use_spatial_matching``
    other than ``true`` is refused. Without a block the legacy keys are translated:
    the leaf (:func:`legacy_dispatch_from_profile`) wrapped in ``Partitioned`` or
    ``PooledMarket`` per ``planner.topology``/``planner.market``
    (``legacy.wrap_legacy_leaf``; loud on an unknown topology, component or
    ``max_rounds``). ``source`` is ``"bundle"`` or ``"legacy-profile"``. Raises
    ``PolicyError`` subclasses.
    """
    block = prof.get("policies")
    if block is None:
        try:
            entry = wrap_legacy_leaf(legacy_dispatch_from_profile(prof), prof.get("planner"))
            resolved = resolve_policies_block({"planner": entry}, where="profile.policies",
                                              legacy_slots=("planner",))
        except PolicyError as exc:
            # The paths above are the translation's; name the keys the author wrote.
            raise type(exc)(f"{exc} {_LEGACY_SOURCE_NOTE}") from exc
        return resolved, "legacy-profile"
    if not isinstance(block, Mapping) or not isinstance(block.get("planner"), Mapping):
        raise PolicyError(
            f"assignment profile 'policies' must be an object with a 'planner' entry "
            f"(got {block!r}). Recompile the scenario."
        )
    if "use_spatial_matching" in prof and prof["use_spatial_matching"] is not True:
        raise PolicyError(
            "assignment profile sets use_spatial_matching="
            f"{prof['use_spatial_matching']!r} next to a 'policies' block; it is superseded "
            "by policies.planner.params.candidates. Remove use_spatial_matching."
        )
    authored = {slot: _authored_from_compiled(slot, entry) for slot, entry in block.items()}
    resolved = resolve_policies_block(authored, where="profile.policies")
    for slot, entry in block.items():  # the top-level echo; nested refs are checked by params
        warn_version_drift(f"profile.policies.{slot}", slot, resolved[slot].name,
                           entry.get("version"), resolved[slot].version)
    return resolved, "bundle"


def _authored_from_compiled(slot: Any, entry: Any) -> Any:
    """``entry`` minus the ignored ``version``/``kind`` echo; refuses any other unknown key.

    A non-mapping entry is passed through for ``resolve_policies_block`` to refuse
    with its standard message (after its slot check, so an unknown slot names itself).
    """
    if not isinstance(entry, Mapping):
        return entry
    unknown = sorted(str(k) for k in entry if k not in COMPILED_ENTRY_KEYS)
    if unknown:
        raise PolicyError(
            f"profile.policies.{slot}: unknown key(s) {', '.join(repr(k) for k in unknown)}. "
            f"A compiled entry carries exactly {', '.join(COMPILED_ENTRY_KEYS)}. "
            "Fix the bundle or recompile the scenario."
        )
    version_echo(entry.get("version"), where=f"profile.policies.{slot}")
    return {k: v for k, v in entry.items() if k in ENTRY_KEYS}


# --- the typed tree -----------------------------------------------------------------


def dispatch_node(name: str, params: Any, *, timing: str, source: ResolvedSource) -> ResolvedDispatch:
    """The :class:`ResolvedDispatch` tree of registered dispatch policy ``name``.

    ``params`` is its validated ``Params`` instance (refs resolved). A composite's
    ``inner`` ref becomes the ``inner`` node (same ``timing`` and ``source``: the
    timing belongs to the slot). Raises ``PolicyError`` for a composite that does
    not delegate through a param named ``inner`` (the ``CompositePolicy`` contract).
    """
    spec = get_policy_spec("planner", name)
    inner = None
    if spec.is_composite:
        if spec.inner_param is None:
            raise PolicyError(
                f"planner policy {spec.name!r} references a planner policy but has no "
                f"'{INNER_PARAM}' param; a composite delegates each scope through '{INNER_PARAM}'."
            )
        ref = getattr(params, spec.inner_param)
        inner = dispatch_node(ref.type, ref.params, timing=timing, source=source)
    return ResolvedDispatch(
        name=spec.name,
        version=spec.version,
        timing=timing,
        params=params,
        source=source,
        inner=inner,
    )


def _node_from_resolved(dispatch: ResolvedPolicy, source: ResolvedSource) -> ResolvedDispatch:
    typed = dispatch.typed
    if typed is None:  # a hand-built ResolvedPolicy: re-validate its plain params
        spec = get_policy_spec("planner", dispatch.name)
        typed = spec.params_cls.from_mapping(
            dispatch.params, where=f"profile.policies.planner.params for {spec.name}")
    return dispatch_node(dispatch.name, typed, timing=dispatch.timing, source=source)


def resolve_dispatch_for_profile(prof: Mapping[str, Any]) -> ResolvedDispatch:
    """Resolve the dispatch slot of an assignment profile as a typed tree (never coerced).

    :func:`resolve_profile_policies`' dispatch entry as a :class:`ResolvedDispatch`
    (``source`` ``"policies"`` for a compiled block, ``"legacy-profile"`` otherwise).
    A non-executable timing is refused by the framework resolver. Raises
    ``PolicyError``.
    """
    resolved, source = resolve_profile_policies(prof)
    return _node_from_resolved(resolved["planner"],
                               "policies" if source == "bundle" else "legacy-profile")


def as_dispatch_node(resolved: ResolvedDispatch | ResolvedPolicy) -> ResolvedDispatch:
    """The typed :class:`ResolvedDispatch` tree of a resolved dispatch entry (identity for a node)."""
    if isinstance(resolved, ResolvedDispatch):
        return resolved
    return _node_from_resolved(resolved, "policies")


def _preorder(node: ResolvedDispatch) -> Iterator[ResolvedDispatch]:
    while node is not None:
        yield node
        node = node.inner


# --- building and binding policy objects -------------------------------------------


def build_policy_tree(node: ResolvedDispatch, reuse: DispatchPolicy | None = None) -> DispatchPolicy:
    """Instantiate ``node``: each policy with ``params=<validated dict>``, inner-holders around.

    A node with an ``inner`` is built as ``cls(params=..., inner=<built inner>,
    inner_node=<inner node>)`` (the construction protocol; the inner is built ONCE and
    is the object ``bind.bind_tree`` binds as the child). ``reuse`` is used AS the
    innermost leaf instead of a new instance (the caller, :func:`bind_instance`, has
    checked it fits). Raises ``PolicyError`` for an unregistered leaf with no ``reuse``
    and for a class that rejects the construction keywords.
    """
    if node.inner is None:
        if reuse is not None:
            return reuse
        if node.source == "unregistered" or node.params is None:
            raise PolicyError(
                f"cannot instantiate the unregistered planner policy {node.name!r}; "
                "pass the instance as reuse."
            )
        return get_policy_spec("planner", node.name).cls(params=node.params.to_dict())
    spec = get_policy_spec("planner", node.name)
    inner = build_policy_tree(node.inner, reuse)
    try:
        return spec.cls(params=node.params.to_dict(), inner=inner, inner_node=node.inner)
    except TypeError as exc:
        raise PolicyError(
            f"planner policy {spec.name!r} has an '{INNER_PARAM}' but its class "
            f"{spec.cls.__qualname__} does not accept the construction keywords "
            f"cls(params=..., inner=..., inner_node=...) ({exc}); subclass "
            "dispatch/composite.py CompositePolicy or accept them."
        ) from exc


def _inner_of(policy: Any) -> Any:
    """``policy.inner`` when its registration declares an ``inner`` ref, else ``None``."""
    spec = spec_for_class(type(policy))
    if spec is None or spec.inner_param is None:
        return None
    return getattr(policy, "inner", None)


def _innermost_instance(policy: Any) -> Any:
    """The leaf of a built policy tree: follow ``inner`` while the registration declares one."""
    while True:
        inner = _inner_of(policy)
        if inner is None:
            return policy
        policy = inner


def _instance_label(policy: Any) -> str:
    names = []
    while policy is not None:
        spec = spec_for_class(type(policy))
        names.append(spec.name if spec is not None else type(policy).__name__)
        policy = _inner_of(policy)
    return "/".join(names)


def _mismatch(instance: Any, node: ResolvedDispatch) -> PolicyError:
    return PolicyError(
        f"AssignmentApp._solver is a {_instance_label(instance)} but the profile resolves "
        f"dispatch to {node.label}."
    )


def _unregistered_node(policy: Any, *, timing: str) -> ResolvedDispatch:
    """The node of a hand-injected unregistered solver (its candidate rule: ``compat.unregistered``)."""
    return ResolvedDispatch(
        name=type(policy).__name__,
        version="unregistered",
        timing=timing,
        params=None,
        source="unregistered",
    )


def _with_innermost(node: ResolvedDispatch, leaf: ResolvedDispatch) -> ResolvedDispatch:
    """``node`` with its innermost node replaced by ``leaf`` (labels recomputed)."""
    if node.inner is None:
        return leaf
    return dataclasses.replace(node, inner=_with_innermost(node.inner, leaf), label="", leaf="")


def _bound_reused_tree(node: ResolvedDispatch, instance: Any) -> tuple[ResolvedDispatch, bool]:
    """Check a REUSED top instance's inner chain against ``node``: ``(bound node, current)``.

    Each inner instance must be the registered class ``node`` names, recursively; an
    unregistered innermost instance takes the legacy candidate rule. Raises on any
    other difference (naming both trees). ``current`` is False when any reused
    COMPOSITE would run something other than the bound tree: its ``params`` differ
    from the bound node's, or its ``inner_node`` from the bound inner (a composite
    decides from both, e.g. ``PooledMarket``'s ``max_rounds`` and components, the
    groups' params and candidate kind). A leaf is never stale: the runner hands it
    the bound params as ``ctx.params`` (review C3 #4).
    """
    if node.inner is None:
        return node, True
    inner = getattr(instance, "inner", None)
    spec = spec_for_class(type(inner)) if inner is not None else None
    if spec is not None and spec.name == node.inner.name:
        bound_inner, current = _bound_reused_tree(node.inner, inner)
        bound = node if bound_inner is node.inner else dataclasses.replace(
            node, inner=bound_inner, label="", leaf="")
    elif inner is not None and spec is None and node.inner.inner is None \
            and getattr(inner, "inner", None) is None:
        _refuse_unregistered_subclass(type(inner))
        leaf = _unregistered_node(inner, timing=node.timing)
        bound, current = dataclasses.replace(node, inner=leaf, label="", leaf=""), True
    else:
        raise _mismatch(instance, node)
    current = current and instance.params == bound.params.to_dict() \
        and getattr(instance, "inner_node", None) == bound.inner
    return bound, current


def _rebuilt_around_leaf(stale: DispatchPolicy, bound: ResolvedDispatch) -> DispatchPolicy:
    """A fresh tree for ``bound`` around ``stale``'s innermost leaf, keeping its injections.

    The leaf instance is reused (it is what was injected); every composite is rebuilt
    from ``bound``. The old top's injected values (``set_rng``, ``set_tiebreak``,
    ``set_rebate_book``) are re-injected into the new top when they were set, so a
    rebuild changes nothing but the stale params.
    """
    fresh = build_policy_tree(bound, reuse=_innermost_instance(stale))
    if getattr(stale, "_rng", None) is not None:
        fresh.set_rng(stale._rng)
    if stale.tiebreak_seed is not None:
        fresh.set_tiebreak(stale.tiebreak_seed)
    if stale.rebate_book is not None:
        fresh.set_rebate_book(stale.rebate_book)
    return fresh


def bind_instance(node: ResolvedDispatch, instance: DispatchPolicy) -> tuple[DispatchPolicy, ResolvedDispatch]:
    """Fit an already-built ``instance`` into the selection ``node``: ``(policy, bound node)``.

    - A registered class named like the TOP is reused as is; its ``inner`` chain must
      match ``node``'s names recursively (an unregistered innermost instance is
      accepted; its candidate rule is ``compat.unregistered``). When a reused composite's params or
      ``inner_node`` differ from the bound tree (a profile patched after construction),
      the composites are REBUILT from the bound tree around the same innermost leaf
      (injections carried), so what runs is what ``bound`` reports (review C3 #4).
    - A registered class named like the INNERMOST leaf becomes that leaf, and the
      wrappers are built around it (:func:`build_policy_tree` with ``reuse``).
    - An unregistered class (a hand-injected test solver) becomes the innermost leaf;
      the bound node carries it as ``source="unregistered"`` and ``bind.bind_tree``
      wraps it in ``compat.unregistered.UnregisteredPairLeaf`` (spatial iff the
      profile's ``use_spatial_matching`` and it has ``solve_pairs``).
    - Anything else raises ``PolicyError`` naming both trees ("AssignmentApp._solver is
      a X but the profile resolves dispatch to Partitioned/Y"), as does an unregistered
      subclass of a registered policy or an unregistered composite (it holds an
      ``inner``).
    """
    spec = spec_for_class(type(instance))
    if spec is not None and spec.name == node.name:
        bound, current = _bound_reused_tree(node, instance)
        return (instance if current else _rebuilt_around_leaf(instance, bound)), bound
    if spec is not None:
        if spec.name == node.innermost.name:
            return build_policy_tree(node, reuse=instance), node
        raise _mismatch(instance, node)
    _refuse_unregistered_subclass(type(instance))
    if getattr(instance, "inner", None) is not None:
        raise PolicyError(
            f"{type(instance).__qualname__} is an unregistered composite (it holds an 'inner'); "
            "register it with @policy(slot='planner', ...) to select it."
        )
    leaf = _unregistered_node(instance, timing=node.timing)
    bound = _with_innermost(node, leaf)
    return build_policy_tree(bound, reuse=instance), bound


def _refuse_unregistered_subclass(cls: type) -> None:
    for ancestor in cls.__mro__[1:]:
        spec = spec_for_class(ancestor)
        if spec is not None:
            raise PolicyError(
                f"{cls.__qualname__} subclasses the registered planner policy {spec.name} "
                "but is not registered itself; decorate it with @policy(slot='planner', ...)."
            )


def bind_profile_instance(prof: Mapping[str, Any], instance: DispatchPolicy) -> tuple[DispatchPolicy, ResolvedDispatch]:
    """:func:`bind_instance` of ``instance`` into ``prof``'s resolved selection (the runner's entry point).

    Returns ``(top-level policy, bound node)``. Raises ``PolicyError`` when the
    profile does not resolve or ``instance`` fits neither its top nor its leaf.
    """
    return bind_instance(resolve_dispatch_for_profile(prof), instance)
