"""Per-run overrides of a LOADED scenario: the dispatch selection, cooperation, smoke config.

The dispatch selection has ONE application point, :func:`run_dispatch_overrides`
(``ORSIM_POLICY_OVERRIDES``, ``ORSIM_SOLVER``, and the legacy dispatch variables,
handed through opaquely as ``legacy_env`` and translated by
``policies/compat/legacy_env.py``), shared by the sim main and the control-plane dry
run (``docs/plans/policy_slots_round2_whole_tick.md`` §3.6, §5 C4); the older entry
points (:func:`apply_policy_overrides`, :func:`apply_solver_override`, and the
compat re-exports below) are compatibility shims with no run-path caller. Everything
here patches the loaded behaviors in memory; the on-disk bundle is never written.
"""

import copy
import json
import logging
from contextlib import contextmanager
from copy import deepcopy

from apps.container_logistics.policies.compat import legacy_env as _legacy_env
# Re-exported from the compat quarantine until the shims are removed (moved verbatim,
# see that module's docstring).
from apps.container_logistics.policies.compat.legacy_env import (  # noqa: F401
    MARKET_ENV,
    apply_market_override,
    apply_planner_topology_override,
    known_planner_topologies,
    legacy_topology_name,
)

from .validation_config import smoke_scenario_config_bundle


def known_solvers():
    """The per-scope solvers ``--solver``/``ORSIM_SOLVER`` accept (registered dispatch
    leaves), sorted -- read from the policy registry at call time (the same registry the
    assignment agent resolves against)."""
    from apps.container_logistics.policies.framework import list_policies

    return tuple(spec.name for spec in list_policies("planner") if not spec.is_composite)


def current_policies_for_profile(profile):
    """The resolved policies of one assignment ``profile``: ``({slot: ResolvedPolicy}, source)``.

    The SAME function the assignment agent resolves with
    (``dispatch/profile.py::resolve_profile_policies``), so the run stamp, the
    per-run override base and the agent can never disagree; nothing is stripped
    silently (an unknown entry key or slot raises). ``source`` is ``"bundle"`` or
    ``"legacy-profile"``. Raises ``PolicyError``.
    """
    from apps.container_logistics.policies.dispatch.profile import resolve_profile_policies

    return resolve_profile_policies(profile)


def apply_policy_overrides(assignment_collection, overrides):
    """Apply per-run policy ``overrides`` to every assignment behavior, in memory.

    ``overrides`` is ``{slot: {type?, timing?, params?}}`` (see
    ``framework.resolve.merge_policy_overrides``), applied LITERALLY: ``{"planner":
    {"type": "GreedyNearest"}}`` selects a CENTRAL GreedyNearest (round-2 §3.6); the
    per-scope solver of a decomposition is ``planner.params.inner`` (or ``--solver``,
    :func:`run_dispatch_overrides`). Each profile gets ``profile["policies"]``
    (resolved, ``to_profile`` form) and -- exactly as compile does -- the selection's
    legacy mirrors (``strategy`` = the innermost solver, the keys ``solver_params`` and
    ``planner.deployment.params`` already carry, ``planner.topology``,
    ``planner.market``; ``compat/mirrors.py::mirror_dispatch_selection``). So a
    per-run arm equals the same edit compiled into ``spec.json``: the framework's
    accounting (``benefit_km``, owner reserve, claims, arbitration) ranks with what
    the policy ranks with (review F1).

    A changed dispatch TYPE resets the policy params to its defaults plus any
    override params, except that a decomposition keeps the current per-scope solver
    as its ``inner`` (``merge_policy_overrides``). A legacy profile's
    ``use_spatial_matching`` is removed once folded into the block, because the
    runtime resolver refuses it next to ``policies``. Every behavior is validated
    before any is mutated. Returns the first behavior's resolved policies, or
    ``None`` for empty ``overrides``. Raises ``PolicyError``.

    Compatibility shim (verification review MINOR-11): no run path calls it; the sim
    main and the control-plane dry run go through :func:`run_dispatch_overrides`.
    Kept for tests and external callers.
    """
    if not overrides:
        return None
    planned = [(profile, _merged(current, overrides))
               for profile, current in _profiles_with_current(assignment_collection)]
    _commit(planned)
    if planned:
        return planned[0][1]
    from apps.container_logistics.policies.framework import resolve_policies_block

    return _merged(resolve_policies_block({}), overrides)


def _profiles_with_current(assignment_collection):
    """``[(profile, {slot: ResolvedPolicy}), ...]`` for every dict behavior (profiles created if absent).

    Each SOURCE profile's format marker is checked first (``legacy.check_policies_format``,
    the agent id prefixed), BEFORE anything is resolved or committed: an override must
    never manufacture the round-2 marker on a round-1 flat bundle, whose
    ``policies.planner {type: X}`` meant the per-scope solver X and would otherwise
    run as a CENTRAL X (final review MAJOR-1). Raises ``PolicyError``.
    """
    from apps.container_logistics.policies.compat import legacy
    from apps.container_logistics.policies.framework import PolicyError

    out = []
    for agent_id, behavior in (assignment_collection or {}).items():
        if not isinstance(behavior, dict):
            continue
        profile = behavior.setdefault("profile", {})
        try:
            legacy.check_policies_format(profile)
        except PolicyError as exc:
            raise type(exc)(f"assignment agent {agent_id!r}: {exc}") from exc
        out.append((profile, current_policies_for_profile(profile)[0]))
    return out


def _merged(current, overrides):
    from apps.container_logistics.policies.framework import merge_policy_overrides

    return merge_policy_overrides(current, overrides)


def _commit(planned):
    """Write each ``(profile, merged)`` selection, its format marker and its mirrors (in place).

    Refuses (before writing anything) to stamp the marker onto a profile whose
    ``policies`` block lacks it: only a legacy profile (no ``policies``, translated) or
    a round-2 block may be committed (final review MAJOR-1; the primary check is
    :func:`_profiles_with_current`, this is the backstop at the write).
    """
    from apps.container_logistics.policies.compat import legacy
    from apps.container_logistics.policies.compat.mirrors import mirror_dispatch_selection

    for profile, _merged_sel in planned:
        legacy.check_policies_format(profile)
    for profile, merged in planned:
        profile["policies"] = {slot: r.to_profile() for slot, r in merged.items()}
        profile[legacy.POLICIES_FORMAT_KEY] = legacy.POLICIES_FORMAT
        profile.pop("use_spatial_matching", None)
        mirror_dispatch_selection(profile, merged["planner"])


def precheck_override_names(overrides, solver=None):
    """Validate the NAMES of a run's dispatch request before any bundle is read.

    ``framework.check_named_overrides(overrides)`` (every named slot, policy and
    component is registered, and the params of every named type validate against it)
    and ``solver`` (a registered per-scope solver; message 11 for a decomposition).
    So ``validate-run-policies --scenario nope --solver Nope`` names ``Nope``, not the
    missing scenario. Raises ``PolicyError``.
    """
    from apps.container_logistics.policies.framework import check_named_overrides

    check_named_overrides(overrides or {})
    _checked_solver(solver)


def run_dispatch_overrides(
    assignment_collection,
    *,
    overrides=None,
    solver=None,
    legacy_env=None,
):
    """Apply every per-run dispatch selection input to the loaded assignment behaviors.

    The ONE application point the sim main and the control-plane dry run share
    (round-2 §5 C4). Applied in this order, per behavior, into ONE override entry
    that is merged once (``merge_policy_overrides``) and committed with the mirrors
    (:func:`apply_policy_overrides`' write):

    1. ``overrides`` (``ORSIM_POLICY_OVERRIDES``; ``--policy``/``--policy-param``),
       literally: ``planner.type`` is the TOP of the selection;
    2-3. ``legacy_env``: the legacy dispatch variables
       (``compat.legacy_env.read_legacy_dispatch_env``), opaque here; checked by
       ``compat.legacy_env.legacy_env_request`` and folded into the entry by
       ``compat.legacy_env.apply_legacy_env`` (messages 9 and 10 are raised there);
    4. ``solver`` (``--solver``/``ORSIM_SOLVER``): the INNERMOST per-scope solver (the
       ``inner`` chain; a leaf top is replaced); a changed type resets its params.
       Message 11 for a decomposition name, message 12 when ``overrides`` names a
       different type at that place.

    Every behavior is validated before any is mutated; nothing requested mutates
    nothing. Returns ``(merged_overrides, labels)``: the override object actually
    merged into the first behavior (``{}`` when nothing was requested), and
    ``{"planner": <run-name segment or None>, "legacy": [<run-name segment>, ...]}``
    (:func:`dispatch_override_label`; ``legacy`` is
    ``compat.legacy_env.LegacyEnvRequest.segments``, appended by the caller where the
    legacy segment always went). Raises ``PolicyError`` (unknown names are
    ``UnknownPolicyError``).
    """
    from apps.container_logistics.policies.framework import PolicyError

    overrides = dict(overrides or {})
    if not isinstance(overrides.get("planner", {}), dict):
        raise PolicyError(f"policy overrides.planner must be an object (got {overrides['planner']!r}).")
    explicit = overrides.get("planner") or {}
    request = _legacy_env.legacy_env_request(legacy_env)
    leaf = _checked_solver(solver)
    if not request and leaf is None and not overrides:
        return {}, {"planner": None, "legacy": []}

    pairs = _profiles_with_current(assignment_collection)
    if not pairs:
        from apps.container_logistics.policies.framework import resolve_policies_block

        pairs = [(None, resolve_policies_block({}))]
    planned, first = [], None
    for profile, current in pairs:
        entry = _dispatch_entry(current, (profile or {}).get("planner"), explicit,
                                request=request, leaf=leaf)
        merged_overrides = dict(overrides)
        if entry:
            merged_overrides["planner"] = entry
        else:
            merged_overrides.pop("planner", None)
        merged = _merged(current, merged_overrides)
        if first is None:
            first = (merged_overrides, merged)
        if profile is not None:
            planned.append((profile, merged))
    _commit(planned)
    merged_overrides, merged = first
    labels = {
        "planner": dispatch_override_label(overrides, merged, solver=leaf),
        "legacy": request.segments(),
    }
    return merged_overrides, labels


def _checked_solver(solver):
    """The registered name of a ``--solver``/``ORSIM_SOLVER`` value (a per-scope solver), or ``None``.

    Raises ``UnknownPolicyError`` for an unknown name and ``PolicyError`` (message 11)
    for a decomposition.
    """
    from apps.container_logistics.policies.framework import PolicyError, get_policy_spec, list_policies

    from apps.container_logistics.policies.framework import UnknownPolicyError

    if solver is None or not str(solver).strip():
        return None
    try:
        spec = get_policy_spec("planner", str(solver).strip())
    except UnknownPolicyError as exc:
        raise UnknownPolicyError(f"--solver {str(solver).strip()!r}: {exc}") from exc
    if spec.is_composite:
        leaves = ", ".join(s.name for s in list_policies("planner") if not s.is_composite)
        raise PolicyError(
            f"--solver {spec.name!r} names a decomposition, not a per-scope solver. --solver "
            f"sets the inner solver ({leaves}); select a decomposition with --policy "
            f"planner={spec.name}."
        )
    return spec.name


def _dispatch_entry(current, planner, explicit, *, request, leaf):
    """The ONE dispatch override entry for a profile whose resolved selection is ``current``.

    The legacy env ``request`` is folded in first (``compat.legacy_env.apply_legacy_env``,
    which reads ``planner``, the profile's legacy block), then ``leaf`` (``--solver``).
    """
    entry = copy.deepcopy(explicit)
    if request:
        _legacy_env.apply_legacy_env(entry, explicit, current, planner, request)
    if leaf is not None:
        _set_innermost(entry, explicit, current, leaf)
    return entry


def _set_innermost(entry, explicit, current, leaf):
    """Point ``entry`` at ``leaf`` as the innermost per-scope solver (in place); message 12 on a conflict."""
    from apps.container_logistics.policies.framework import PolicyError

    trial = _merged(current, {"planner": entry})["planner"] if entry else current["planner"]
    path, innermost = innermost_path(trial)
    if innermost == leaf:
        return
    stated = _type_at(explicit, path)
    if stated is not None and stated != leaf:
        where = ".".join(["planner", *path, "type"])
        raise PolicyError(
            f"--solver {leaf!r} conflicts with {where}={stated!r} from --policy/--policy-param/"
            "--policy-overrides; --solver is shorthand for the innermost solver, pass one of them."
        )
    if not path:
        entry["type"] = leaf
        return
    node = entry
    for name in path:
        params = node.setdefault("params", {})
        child = params.get(name)
        if isinstance(child, str):
            child = {"type": child}
        params[name] = child = dict(child) if isinstance(child, dict) else {}
        node = child
    node["type"] = leaf


def innermost_path(resolved):
    """``([ref param, ...], innermost name)`` of a resolved dispatch selection (the ``inner`` chain)."""
    from apps.container_logistics.policies.framework import get_policy_spec

    path, name, params = [], resolved.name, resolved.params
    while True:
        descend = get_policy_spec("planner", name).inner_param
        child = params.get(descend) if descend is not None else None
        if not isinstance(child, dict):
            return path, name
        path.append(descend)
        name, params = child["type"], child.get("params") or {}


def _type_at(entry, path):
    """The type ``entry`` states at ``path`` (``planner.params.<p>...``), or ``None``."""
    node = entry
    for name in path:
        params = node.get("params") if isinstance(node, dict) else None
        node = params.get(name) if isinstance(params, dict) else None
        if isinstance(node, str):
            node = {"type": node}
    value = node.get("type") if isinstance(node, dict) else None
    return value.strip() if isinstance(value, str) else value


def dispatch_override_label(overrides, resolved, *, solver=None):
    """The ``· <segment>`` a dispatch override appends to the run name, or ``None``.

    ``overrides`` is the raw ``{slot: entry}`` request (``ORSIM_POLICY_OVERRIDES`` /
    ``--policy``/``--policy-param``), ``solver`` the ``--solver``/``ORSIM_SOLVER``
    name, ``resolved`` the merged ``{slot: ResolvedPolicy}``. The legacy env variables
    never contribute (``labels["legacy"]`` carries their own segment, as before).
    A node of the selection's ``inner`` chain is TOUCHED when the request names its
    type (``--solver`` names the innermost one) or sets one of its own params.
    Formats (the frontend's ``lib/runNameMeta.ts`` reads bare names, ``Name(k=v,...)``
    and ``A/B`` labels whose every part is one of those):

    - only the innermost per-scope solver touched: ``<Leaf>`` or ``<Leaf>(k=v,...)``
      (the pre-v2 ``--solver X`` segment, unchanged);
    - any node above it touched: the full chain, each node ``<Name>`` or
      ``<Name>(k=v,...)`` joined by ``/``, e.g. ``PooledMarket/GreedyNearest`` (the
      stamp ``label``) or ``PooledMarket(max_rounds=5)/GreedyNearest``;
    - nothing touched (e.g. a timing-only override): ``None``.

    ``(k=v,...)`` lists ONLY the params the request set on that node, keys sorted,
    each value the validated value as compact JSON (``json.dumps``, separators
    ``,``/``:``, keys sorted); a component ref (``offer``) renders as ``<Type>`` or
    ``<Type>(k=v,...)`` by the same rule.
    """
    from apps.container_logistics.policies.framework import get_policy_spec

    entry = overrides.get("planner") if isinstance(overrides, dict) else None
    entry = entry if isinstance(entry, dict) else {}
    node = resolved["planner"]
    levels = []  # (name, resolved params, stated entry, inner param)
    name, params, stated = node.name, node.params, entry
    while True:
        descend = get_policy_spec("planner", name).inner_param
        levels.append((name, params, stated, descend))
        child = params.get(descend) if descend is not None else None
        if not isinstance(child, dict):
            break
        sub = (stated.get("params") or {}).get(descend) if isinstance(stated.get("params"), dict) else None
        stated = {"type": sub} if isinstance(sub, str) else (sub if isinstance(sub, dict) else {})
        name, params = child["type"], child.get("params") or {}

    def keys(level):
        _name, _params, st, descend = level
        own = st.get("params") if isinstance(st.get("params"), dict) else {}
        return sorted(k for k in own if k != descend)

    touched = [bool(lv[2].get("type")) or bool(keys(lv)) for lv in levels]
    if solver:
        touched[-1] = True
    if not any(touched):
        return None

    def fmt(level):
        name, params, st, _descend = level
        ks = keys(level)
        if not ks:
            return name
        own = st["params"]
        return f"{name}({','.join(f'{k}={_label_value(params.get(k), own[k])}' for k in ks)})"

    if not any(touched[:-1]):
        return fmt(levels[-1])
    return "/".join(fmt(lv) for lv in levels)


def _label_value(value, stated):
    """One overridden param value in a run-name label (a component ref as ``Type`` / ``Type(k=v)``)."""
    if isinstance(value, dict) and "type" in value and isinstance(stated, (dict, str)):
        sub = stated.get("params") if isinstance(stated, dict) else None
        ks = sorted(sub) if isinstance(sub, dict) else []
        inner = value.get("params") or {}
        if not ks:
            return value["type"]
        return f"{value['type']}({','.join(f'{k}={_label_value(inner.get(k), sub[k])}' for k in ks)})"
    # ensure_ascii=True is LOAD-BEARING (final review MINOR-3): it escapes a value's " · "
    # (and any non-ASCII) so a param value can never forge a run-name segment separator.
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)


def apply_solver_override(assignment_collection, strategy):
    """Per-run per-scope solver override: ``--solver X`` / ``ORSIM_SOLVER``.

    A wrapper of :func:`run_dispatch_overrides` (``solver=strategy``): ``X`` becomes
    the innermost per-scope solver, params reset when the type changes. Returns its
    registered name, or ``None`` if ``strategy`` is falsy (nothing changed). Raises
    ``UnknownPolicyError`` for an unregistered name and ``PolicyError`` (message 11)
    for a decomposition, before anything is mutated.

    Compatibility shim (verification review MINOR-11): no run path calls it; the sim
    main and the control-plane dry run go through :func:`run_dispatch_overrides`.
    Kept for tests and external callers.
    """
    if not strategy:
        return None
    run_dispatch_overrides(assignment_collection, solver=strategy)
    return _checked_solver(strategy)


def apply_cooperation_override(assignment_collection, structure=None):
    """Per-run cooperation-structure override: ``--structure X`` / ``ORSIM_COOP_STRUCTURE``.

    Patches ``cooperation.active`` in place on every assignment profile to the
    declared structure ``structure`` names. The match is on the SLUG
    (``datagen.hauliers.structure_id_slug``, F6), so the authored form
    ``"Port Alliance"`` finds the compiled ``"port-alliance"``. Mutates the loaded
    behaviors only (never the on-disk bundle).

    Returns ``(applied_id, patched_world_keys)``: the structure id now active (or
    ``None`` when ``structure`` is falsy, nothing changed) and the world keys the
    patch wrote (``frozenset({"cooperation"})``, or empty). The caller hands the keys
    to the run-start check (``dispatch.profile.assert_assignment_profiles_resolvable``),
    which refuses a patch the bound planner tree never reads (message 25).

    Fail-loud (message 26, ``PolicyError``, raised before anything is mutated): an id
    no structure declares, or any structure on a profile with no cooperation block.
    It used to warn and run the scenario's own arm, so a typo produced a run labelled
    as one experiment that was another.
    """
    from apps.container_logistics.datagen.hauliers import structure_id_slug
    from apps.container_logistics.policies.framework import PolicyError

    if not structure:
        return None, frozenset()
    wanted = structure_id_slug(structure) or structure
    blocks = []
    for agent_id, behavior in (assignment_collection or {}).items():
        if not isinstance(behavior, dict):
            continue
        coop = (behavior.get("profile") or {}).get("cooperation")
        if not isinstance(coop, dict):
            raise PolicyError(
                f"cooperation structure override {structure!r}: assignment agent {agent_id!r} has no "
                "cooperation block (declared structures: none), so there is no arm to select. Drop the "
                "override, or declare structures in the scenario's spec.json cooperation block and recompile."
            )
        declared = [s.get("id") for s in coop.get("structures", []) if isinstance(s, dict) and s.get("id")]
        if wanted not in declared:
            raise PolicyError(
                f"cooperation structure override {structure!r} is not declared by assignment agent "
                f"{agent_id!r} (declared structures: {', '.join(declared) or 'none'}). Pick a declared id "
                "or fix the typo (it is never replaced by the scenario's own arm)."
            )
        blocks.append(coop)
    for coop in blocks:
        coop["active"] = wanted
    return (wanted, frozenset({"cooperation"})) if blocks else (None, frozenset())


@contextmanager
def smoke_scenario_config_override():
    import apps.container_logistics.scenario.scenario_config as scenario_config

    bundle = smoke_scenario_config_bundle()
    backup = {
        "SIMULATION_DAYS": scenario_config.SIMULATION_DAYS,
        "truck_settings": deepcopy(scenario_config.truck_settings),
        "order_settings": deepcopy(scenario_config.order_settings),
    }
    scenario_config.SIMULATION_DAYS = bundle["SIMULATION_DAYS"]
    scenario_config.truck_settings = bundle["truck_settings"]
    scenario_config.order_settings = bundle["order_settings"]
    try:
        yield
    finally:
        scenario_config.SIMULATION_DAYS = backup["SIMULATION_DAYS"]
        scenario_config.truck_settings = backup["truck_settings"]
        scenario_config.order_settings = backup["order_settings"]
