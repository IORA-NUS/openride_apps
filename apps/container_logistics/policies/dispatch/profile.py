"""Assignment behavior profile -> the dispatch selection TREE that runs, and its mirrors.

Single responsibility: map an assignment agent's behavior ``profile`` onto the
dispatch slot (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P0.7 and P1.6; round 2
``docs/plans/policy_slots_round2_whole_tick.md`` §3.6, §5 C2):

- :func:`resolve_profile_policies` is THE profile resolver (agent, run-start check,
  run stamp and per-run overrides all use it): the compiled ``policies`` block when
  present, else the legacy keys (``strategy``, ``solver_params``,
  ``use_spatial_matching``, ``planner.topology``/``planner.market``) translated by
  :func:`legacy_dispatch_from_profile` and ``legacy.wrap_legacy_leaf``.
- :func:`resolve_dispatch_for_profile` is its dispatch entry as a typed
  :class:`ResolvedDispatch` tree (:func:`dispatch_node`).
- :func:`build_policy_tree` instantiates a tree; :func:`bind_instance` fits an
  already-built policy object (``AssignmentApp._solver``) into a tree, or refuses;
  :func:`bind_profile_instance` does that for a profile (the runner's entry point).
- :func:`mirror_dispatch_selection` writes the legacy mirrors (``strategy``,
  ``solver_params``, ``planner.deployment``, ``planner.topology``, ``planner.market``)
  from a selection; :func:`check_mirrors` refuses a profile whose mirrors disagree
  with its compiled ``policies`` (message 13).
- :func:`assert_assignment_profiles_resolvable` is the run-start check
  ``run_container_logistics_simulation.main`` runs before any agent spawns.

Everything raises ``PolicyError`` subclasses; nothing falls back silently.

Import contract (plan §2.7): imports only leaf ``policies`` modules at module top,
never ``apps.container_logistics.assignment.*`` (``dispatch/world.py`` is imported
locally), so ``assignment.app`` can import it without a cycle. Framework side.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Iterator, Mapping

from apps.container_logistics.policies.framework import (
    PolicyError,
    ResolvedPolicy,
    get_policy_spec,
    list_policies,
    resolve_policies_block,
    spec_for_class,
)
from apps.container_logistics.policies.framework.resolve import (
    ENTRY_KEYS,
    INNER_PARAM,
    drop_undeclared_legacy_params,
)

from .base import DispatchPolicy
from .composite import CompositePolicy
from .legacy import (
    LEGACY_LEAF_DEFAULT,
    MARKET_ROLES,
    PARTITIONED,
    POOLED_MARKET,
    legacy_market_params,
    legacy_topology,
    market_echo,
    topology_echo,
    wrap_legacy_leaf,
)
from .resolved import ResolvedDispatch, ResolvedSource, candidate_kind_for

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

_DECOMPOSITIONS = (PARTITIONED, POOLED_MARKET)
_LEGACY_SOURCE_NOTE = (
    "(This profile has no 'policies' block: its selection is translated from the legacy keys "
    "strategy, solver_params, use_spatial_matching and planner.topology/planner.market; "
    "the fix goes there, or recompile the scenario.)"
)
_MARKET_KEYS = ("max_rounds", *MARKET_ROLES)


# --- the run-start check ----------------------------------------------------------


def assert_assignment_profiles_resolvable(collection: Mapping[str, Any]) -> None:
    """Raise ``PolicyError`` unless every assignment agent's dispatch resolves and runs.

    ``collection`` is the assignment behavior collection, ``{agent_id: behavior}``.
    Each profile goes through :func:`resolve_dispatch_for_profile` (the shared
    framework resolver also refuses a non-executable timing), then
    :func:`check_mirrors` (message 13) and ``world.validate_cooperation_world``
    (message 14). The raised error is the
    original class (e.g. ``UnknownPolicyError``) with the agent id prefixed; a
    behavior that is not a mapping raises plain ``PolicyError``.
    """
    from .world import validate_cooperation_world  # local: world imports assignment.*

    for agent_id, behavior in (collection or {}).items():
        if not isinstance(behavior, Mapping):
            raise PolicyError(
                f"assignment agent {agent_id!r}: behavior must be a mapping with a "
                f"'profile' (got {type(behavior).__name__})."
            )
        profile = behavior.get("profile") or {}
        try:
            node = resolve_dispatch_for_profile(profile)
            check_mirrors(profile, node)
            validate_cooperation_world(profile)
        except PolicyError as exc:
            raise type(exc)(f"assignment agent {agent_id!r}: {exc}") from exc


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
    spec = get_policy_spec("dispatch", name)
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
    and ``kind`` (and each nested ref's ``version``) are IGNORED: they are a
    compile-time echo, the run stamp re-derives them from the registry, so the record
    is always what ran. A round-1 FLAT block (``{type: GreedyNearest}``) resolves
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
            resolved = resolve_policies_block({"dispatch": entry}, where="profile.policies",
                                              legacy_slots=("dispatch",))
        except PolicyError as exc:
            # The paths above are the translation's; name the keys the author wrote.
            raise type(exc)(f"{exc} {_LEGACY_SOURCE_NOTE}") from exc
        return resolved, "legacy-profile"
    if not isinstance(block, Mapping) or not isinstance(block.get("dispatch"), Mapping):
        raise PolicyError(
            f"assignment profile 'policies' must be an object with a 'dispatch' entry "
            f"(got {block!r}). Recompile the scenario."
        )
    if "use_spatial_matching" in prof and prof["use_spatial_matching"] is not True:
        raise PolicyError(
            "assignment profile sets use_spatial_matching="
            f"{prof['use_spatial_matching']!r} next to a 'policies' block; it is superseded "
            "by policies.dispatch.params.candidates. Remove use_spatial_matching."
        )
    authored = {slot: _authored_from_compiled(slot, entry) for slot, entry in block.items()}
    return resolve_policies_block(authored, where="profile.policies"), "bundle"


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
    return {k: v for k, v in entry.items() if k in ENTRY_KEYS}


# --- the typed tree -----------------------------------------------------------------


def dispatch_node(name: str, params: Any, *, timing: str, source: ResolvedSource) -> ResolvedDispatch:
    """The :class:`ResolvedDispatch` tree of registered dispatch policy ``name``.

    ``params`` is its validated ``Params`` instance (refs resolved). A composite's
    ``inner`` ref becomes the ``inner`` node (same ``timing`` and ``source``: the
    timing belongs to the slot). Raises ``PolicyError`` for a composite that does
    not delegate through a param named ``inner`` (the ``CompositePolicy`` contract).
    """
    spec = get_policy_spec("dispatch", name)
    inner = None
    if spec.is_composite:
        if spec.inner_param is None:
            raise PolicyError(
                f"dispatch policy {spec.name!r} references a dispatch policy but has no "
                f"'{INNER_PARAM}' param; a composite delegates each scope through '{INNER_PARAM}'."
            )
        ref = getattr(params, spec.inner_param)
        inner = dispatch_node(ref.type, ref.params, timing=timing, source=source)
    return ResolvedDispatch(
        name=spec.name,
        version=spec.version,
        timing=timing,
        params=params,
        candidate_kind=candidate_kind_for(params),
        source=source,
        information_model=spec.information_model,
        share_tag_format=spec.share_tag_format,
        inner=inner,
    )


def _node_from_resolved(dispatch: ResolvedPolicy, source: ResolvedSource) -> ResolvedDispatch:
    typed = dispatch.typed
    if typed is None:  # a hand-built ResolvedPolicy: re-validate its plain params
        spec = get_policy_spec("dispatch", dispatch.name)
        typed = spec.params_cls.from_mapping(
            dispatch.params, where=f"profile.policies.dispatch.params for {spec.name}")
    return dispatch_node(dispatch.name, typed, timing=dispatch.timing, source=source)


def resolve_dispatch_for_profile(prof: Mapping[str, Any]) -> ResolvedDispatch:
    """Resolve the dispatch slot of an assignment profile as a typed tree (never coerced).

    :func:`resolve_profile_policies`' dispatch entry as a :class:`ResolvedDispatch`
    (``source`` ``"policies"`` for a compiled block, ``"legacy-profile"`` otherwise).
    A non-executable timing is refused by the framework resolver. Raises
    ``PolicyError``.
    """
    resolved, source = resolve_profile_policies(prof)
    return _node_from_resolved(resolved["dispatch"],
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
    """Instantiate ``node``: each policy with ``params=<validated dict>``, composites around.

    A composite is built as ``cls(params=..., inner=<built inner>, inner_node=<inner
    node>)``. ``reuse`` is used AS the innermost leaf instead of a new instance (the
    caller, :func:`bind_instance`, has checked it fits). Raises ``PolicyError`` for an
    unregistered leaf with no ``reuse`` and for a composite class that is not a
    ``CompositePolicy``.
    """
    if node.inner is None:
        if reuse is not None:
            return reuse
        if node.source == "unregistered" or node.params is None:
            raise PolicyError(
                f"cannot instantiate the unregistered dispatch policy {node.name!r}; "
                "pass the instance as reuse."
            )
        return get_policy_spec("dispatch", node.name).cls(params=node.params.to_dict())
    spec = get_policy_spec("dispatch", node.name)
    if not issubclass(spec.cls, CompositePolicy):
        raise PolicyError(
            f"dispatch policy {spec.name!r} has an '{INNER_PARAM}' but its class "
            f"{spec.cls.__qualname__} does not subclass CompositePolicy (dispatch/composite.py)."
        )
    inner = build_policy_tree(node.inner, reuse)
    return spec.cls(params=node.params.to_dict(), inner=inner, inner_node=node.inner)


def _instance_label(policy: Any) -> str:
    names = []
    while policy is not None:
        spec = spec_for_class(type(policy))
        names.append(spec.name if spec is not None else type(policy).__name__)
        policy = policy.inner if isinstance(policy, CompositePolicy) else None
    return "/".join(names)


def _mismatch(instance: Any, node: ResolvedDispatch) -> PolicyError:
    return PolicyError(
        f"AssignmentApp._solver is a {_instance_label(instance)} but the profile resolves "
        f"dispatch to {node.label}."
    )


def _unregistered_node(policy: Any, *, timing: str, use_spatial_matching: bool) -> ResolvedDispatch:
    """The legacy candidate rule: spatial iff ``use_spatial_matching`` and ``solve_pairs``."""
    spatial = use_spatial_matching and hasattr(policy, "solve_pairs")
    return ResolvedDispatch(
        name=type(policy).__name__,
        version="unregistered",
        timing=timing,
        params=None,
        candidate_kind="spatial" if spatial else "sampled",
        source="unregistered",
    )


def _with_innermost(node: ResolvedDispatch, leaf: ResolvedDispatch) -> ResolvedDispatch:
    """``node`` with its innermost node replaced by ``leaf`` (labels recomputed)."""
    if node.inner is None:
        return leaf
    return dataclasses.replace(node, inner=_with_innermost(node.inner, leaf), label="", leaf="")


def _bound_reused_tree(node: ResolvedDispatch, instance: Any, use_spatial_matching: bool) -> ResolvedDispatch:
    """Check a REUSED top instance's inner chain against ``node``; return the bound node.

    Each inner instance must be the registered class ``node`` names, recursively; an
    unregistered innermost instance takes the legacy candidate rule. Raises on any
    other difference (naming both trees).
    """
    if node.inner is None:
        return node
    inner = getattr(instance, "inner", None)
    spec = spec_for_class(type(inner)) if inner is not None else None
    if spec is not None and spec.name == node.inner.name:
        bound_inner = _bound_reused_tree(node.inner, inner, use_spatial_matching)
        return node if bound_inner is node.inner else dataclasses.replace(
            node, inner=bound_inner, label="", leaf="")
    if inner is not None and spec is None and node.inner.inner is None \
            and not isinstance(inner, CompositePolicy):
        _refuse_unregistered_subclass(type(inner))
        leaf = _unregistered_node(inner, timing=node.timing, use_spatial_matching=use_spatial_matching)
        return dataclasses.replace(node, inner=leaf, label="", leaf="")
    raise _mismatch(instance, node)


def bind_instance(
    node: ResolvedDispatch, instance: DispatchPolicy, *, use_spatial_matching: bool = True
) -> tuple[DispatchPolicy, ResolvedDispatch]:
    """Fit an already-built ``instance`` into the selection ``node``: ``(policy, bound node)``.

    - A registered class named like the TOP is reused as is; its ``inner`` chain must
      match ``node``'s names recursively (an unregistered innermost instance is
      accepted under the legacy candidate rule).
    - A registered class named like the INNERMOST leaf becomes that leaf, and the
      wrappers are built around it (:func:`build_policy_tree` with ``reuse``).
    - An unregistered class (a hand-injected test solver) becomes the innermost leaf
      under the legacy candidate rule (spatial iff ``use_spatial_matching`` and it has
      ``solve_pairs``); the bound node carries it as ``source="unregistered"``.
    - Anything else raises ``PolicyError`` naming both trees ("AssignmentApp._solver is
      a X but the profile resolves dispatch to Partitioned/Y"), as does an unregistered
      subclass of a registered policy or an unregistered composite.
    """
    spec = spec_for_class(type(instance))
    if spec is not None and spec.name == node.name:
        return instance, _bound_reused_tree(node, instance, use_spatial_matching)
    if spec is not None:
        if spec.name == node.innermost.name:
            return build_policy_tree(node, reuse=instance), node
        raise _mismatch(instance, node)
    _refuse_unregistered_subclass(type(instance))
    if isinstance(instance, CompositePolicy):
        raise PolicyError(
            f"{type(instance).__qualname__} is an unregistered CompositePolicy; register it "
            "with @policy(slot='dispatch', ...) to select it."
        )
    leaf = _unregistered_node(instance, timing=node.timing, use_spatial_matching=use_spatial_matching)
    bound = _with_innermost(node, leaf)
    return build_policy_tree(bound, reuse=instance), bound


def _refuse_unregistered_subclass(cls: type) -> None:
    for ancestor in cls.__mro__[1:]:
        spec = spec_for_class(ancestor)
        if spec is not None:
            raise PolicyError(
                f"{cls.__qualname__} subclasses the registered dispatch policy {spec.name} "
                "but is not registered itself; decorate it with @policy(slot='dispatch', ...)."
            )


def bind_profile_instance(prof: Mapping[str, Any], instance: DispatchPolicy) -> tuple[DispatchPolicy, ResolvedDispatch]:
    """:func:`bind_instance` of ``instance`` into ``prof``'s resolved selection (the runner's entry point).

    Returns ``(top-level policy, bound node)``. Raises ``PolicyError`` when the
    profile does not resolve or ``instance`` fits neither its top nor its leaf.
    """
    return bind_instance(resolve_dispatch_for_profile(prof), instance,
                         use_spatial_matching=use_spatial_matching(prof))


# --- the legacy mirrors ---------------------------------------------------------------


def _first_declared(nodes: list[ResolvedDispatch], key: str) -> tuple[bool, Any]:
    """``(True, value)`` of ``key`` in the first node (pre-order) whose params declare it."""
    for node in nodes:
        if node.params is not None and key in type(node.params).__dataclass_fields__:
            return True, getattr(node.params, key)
    return False, None


def mirror_dispatch_selection(profile: dict[str, Any], resolved: ResolvedDispatch | ResolvedPolicy) -> None:
    """Write a selection's legacy mirrors into an assignment ``profile`` (in place).

    The ONE copy of the mirror rule (round-2 §3.6, F1): ``strategy`` becomes the
    innermost leaf; every key ALREADY PRESENT in ``solver_params`` and
    ``planner.deployment.params`` takes the value of the first node of the tree
    (pre-order) that declares it (normally the leaf; a cost-blind leaf leaves the
    accounting dials alone), and an existing ``planner.deployment.type`` becomes the
    leaf; ``planner.topology`` becomes ``legacy.topology_echo`` of the top (the key is
    removed when the echo is ``None``); ``planner.market`` becomes
    ``legacy.market_echo`` of the params when the top is ``PooledMarket`` (otherwise it
    is inert pooled-arm configuration and left as is). No other key is added.
    """
    node = as_dispatch_node(resolved)
    nodes = list(_preorder(node))
    profile["strategy"] = node.leaf
    planner = profile.get("planner")
    deployment = planner.get("deployment") if isinstance(planner, dict) else None
    deployment = deployment if isinstance(deployment, dict) else {}
    for target in (profile.get("solver_params"), deployment.get("params")):
        if isinstance(target, dict):
            for key in list(target):
                found, value = _first_declared(nodes, key)
                if found:
                    target[key] = value
    if "type" in deployment:
        deployment["type"] = node.leaf
    echo = topology_echo(node.name)
    if echo is None:
        if isinstance(planner, dict):
            planner.pop("topology", None)
        return
    planner = profile.setdefault("planner", {}) if not isinstance(planner, dict) else planner
    planner["topology"] = echo
    if node.name == POOLED_MARKET:
        planner["market"] = market_echo(node.params)


def check_mirrors(profile: Mapping[str, Any], resolved: ResolvedDispatch | ResolvedPolicy) -> None:
    """Refuse a profile whose legacy mirrors disagree with its selection (message 13).

    At run start the compiled ``policies`` block is authoritative and the mirrors are
    CHECKED, never read: a present ``strategy`` must be the innermost leaf; every key
    of ``solver_params`` and ``planner.deployment.params`` that a node of the tree
    declares must equal that node's value (the first such node, pre-order: the F1
    one-dial rule ``mirror_dispatch_selection`` writes, and ``solver_params`` is what
    the framework's accounting READS); a present ``planner.topology`` must be a legacy
    topology whose decomposition is the top (``Partitioned`` <-> partitioned,
    ``PooledMarket`` <-> pooled; any other top has none); a present ``planner.market``
    must agree with ``PooledMarket``'s params when that is the top (it is inert
    otherwise). Raises ``PolicyError``.
    """
    node = as_dispatch_node(resolved)
    _check_strategy_mirror(profile.get("strategy"), node)
    _check_accounting_mirrors(profile, node)
    planner = profile.get("planner") if isinstance(profile.get("planner"), Mapping) else {}
    if planner.get("topology") is not None:
        _check_topology_mirror(planner, node)
    if node.name == POOLED_MARKET and planner.get("market") is not None:
        _check_market_mirror(planner, node)


def _check_strategy_mirror(strategy: Any, node: ResolvedDispatch) -> None:
    if strategy is None or strategy == node.leaf:
        return
    # Advise --solver only with a name it accepts (a registered per-scope solver).
    leaves = {spec.name for spec in list_policies("dispatch") if not spec.is_composite}
    hint = (f"change a run's solver with --solver {strategy} (ORSIM_SOLVER is translated into "
            "exactly that). " if strategy in leaves else "")
    raise PolicyError(
        f"profile strategy={strategy!r} disagrees with its compiled policies.dispatch "
        f"({node.label}, whose innermost solver is {node.leaf}). The compiled policies "
        f"block is authoritative; {hint}Fix the bundle or recompile."
    )


def _same_value(a: Any, b: Any) -> bool:
    """Equal, and a bool never equals a number (``True == 1`` in Python; plan §2.6)."""
    return isinstance(a, bool) == isinstance(b, bool) and a == b


def _check_accounting_mirrors(profile: Mapping[str, Any], node: ResolvedDispatch) -> None:
    """Refuse an accounting mirror (``solver_params``, ``planner.deployment.params``) the tree contradicts."""
    nodes = list(_preorder(node))
    planner = profile.get("planner") if isinstance(profile.get("planner"), Mapping) else {}
    deployment = planner.get("deployment") if isinstance(planner.get("deployment"), Mapping) else {}
    for where, mirror in (("solver_params", profile.get("solver_params")),
                          ("planner.deployment.params", deployment.get("params"))):
        if not isinstance(mirror, Mapping):
            continue
        diffs = []
        for key in mirror:
            found, value = _first_declared(nodes, key)
            if found and not _same_value(mirror[key], value):
                diffs.append(f"{key}={mirror[key]!r} vs {value!r}")
        if diffs:
            raise PolicyError(
                f"profile {where} disagrees with its compiled policies.dispatch ({node.label}): "
                f"{', '.join(diffs)}. The compiled policies block is authoritative and the "
                f"framework's share accounting costs with solver_params, so the two must be "
                "equal (one dial). Fix the bundle or recompile."
            )


def _check_topology_mirror(planner: Mapping[str, Any], node: ResolvedDispatch) -> None:
    topology = legacy_topology(planner)
    if topology_echo(node.name) == topology:
        return
    wanted = PARTITIONED if topology == "partitioned" else POOLED_MARKET
    raise PolicyError(
        f"profile planner.topology={planner['topology']!r} disagrees with its compiled "
        f"policies.dispatch ({node.name}). The compiled policies block is authoritative; "
        f"change a run's decomposition with --policy dispatch={wanted} "
        "(ORSIM_PLANNER_TOPOLOGY is translated into exactly that). Fix the bundle or recompile."
    )


def _check_market_mirror(planner: Mapping[str, Any], node: ResolvedDispatch) -> None:
    spec = get_policy_spec("dispatch", node.name)
    compiled = node.params.to_dict()
    authored = spec.params_cls.from_mapping(
        {**legacy_market_params(planner), spec.inner_param: compiled[spec.inner_param]},
        where=f"profile planner.market for {node.name}",
    ).to_dict()
    diffs = [f"{k}={authored[k]!r} vs {compiled[k]!r}" for k in _MARKET_KEYS if authored[k] != compiled[k]]
    if diffs:
        raise PolicyError(
            f"profile planner.market disagrees with its compiled policies.dispatch "
            f"({node.label}): {', '.join(diffs)}. The compiled policies block is "
            "authoritative; change a run's market with --policy-param dispatch.<key>=<value> "
            "(ORSIM_OFFER_POLICY, ORSIM_CLAIM_POLICY, ORSIM_ARBITRATION_RULE and "
            "ORSIM_MARKET_MAX_ROUNDS are translated into exactly that). Fix the bundle or recompile."
        )

