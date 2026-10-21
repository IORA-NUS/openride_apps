"""Per-run overrides of a LOADED scenario: the dispatch selection, cooperation, smoke config.

The dispatch selection has ONE application point, :func:`run_dispatch_overrides`
(``ORSIM_POLICY_OVERRIDES``, ``ORSIM_PLANNER_TOPOLOGY``, the ``ORSIM_*`` market
variables and ``ORSIM_SOLVER``), shared by the sim main and the control-plane dry run
(``docs/plans/policy_slots_round2_whole_tick.md`` §3.6, §5 C4); the older entry
points (:func:`apply_solver_override`, :func:`apply_planner_topology_override`,
:func:`apply_market_override`) are loud wrappers of it. Everything here patches the
loaded behaviors in memory; the on-disk bundle is never written.
"""

import copy
import json
import logging
import re
from contextlib import contextmanager
from copy import deepcopy

from .validation_config import smoke_scenario_config_bundle


def known_solvers():
    """The per-scope solvers ``--solver``/``ORSIM_SOLVER`` accept (registered dispatch
    leaves), sorted -- read from the policy registry at call time (the same registry the
    assignment agent resolves against)."""
    from apps.container_logistics.policies.framework import list_policies

    return tuple(spec.name for spec in list_policies("dispatch") if not spec.is_composite)


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
    ``framework.resolve.merge_policy_overrides``), applied LITERALLY: ``{"dispatch":
    {"type": "GreedyNearest"}}`` selects a CENTRAL GreedyNearest (round-2 §3.6); the
    per-scope solver of a decomposition is ``dispatch.params.inner`` (or ``--solver``,
    :func:`run_dispatch_overrides`). Each profile gets ``profile["policies"]``
    (resolved, ``to_profile`` form) and -- exactly as compile does -- the selection's
    legacy mirrors (``strategy`` = the innermost solver, the keys ``solver_params`` and
    ``planner.deployment.params`` already carry, ``planner.topology``,
    ``planner.market``; ``dispatch/profile.py::mirror_dispatch_selection``). So a
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
    ``policies.dispatch {type: X}`` meant the per-scope solver X and would otherwise
    run as a CENTRAL X (final review MAJOR-1). Raises ``PolicyError``.
    """
    from apps.container_logistics.policies.dispatch import legacy
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
    from apps.container_logistics.policies.dispatch import legacy
    from apps.container_logistics.policies.dispatch.profile import mirror_dispatch_selection

    for profile, _merged_sel in planned:
        legacy.check_policies_format(profile)
    for profile, merged in planned:
        profile["policies"] = {slot: r.to_profile() for slot, r in merged.items()}
        profile[legacy.POLICIES_FORMAT_KEY] = legacy.POLICIES_FORMAT
        profile.pop("use_spatial_matching", None)
        mirror_dispatch_selection(profile, merged["dispatch"])


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


def assert_policies_format(assignment_collection):
    """Run-start check: every compiled ``profile.policies`` carries the round-2 format marker.

    ``legacy.check_policies_format`` per behavior, the agent id prefixed (the shape of
    ``assert_assignment_profiles_resolvable``). Raises ``PolicyError``.
    """
    from apps.container_logistics.policies.dispatch import legacy
    from apps.container_logistics.policies.framework import PolicyError

    for agent_id, behavior in (assignment_collection or {}).items():
        profile = behavior.get("profile") if isinstance(behavior, dict) else None
        try:
            legacy.check_policies_format(profile or {})
        except PolicyError as exc:
            raise type(exc)(f"assignment agent {agent_id!r}: {exc}") from exc


#: The ``PooledMarket`` params each legacy market variable sets, in the message order.
MARKET_ENV = (
    ("offer", "ORSIM_OFFER_POLICY"),
    ("claim", "ORSIM_CLAIM_POLICY"),
    ("arbitration", "ORSIM_ARBITRATION_RULE"),
    ("max_rounds", "ORSIM_MARKET_MAX_ROUNDS"),
)


def run_dispatch_overrides(
    assignment_collection,
    *,
    overrides=None,
    solver=None,
    topology=None,
    offer=None,
    claim=None,
    arbitration=None,
    max_rounds=None,
):
    """Apply every per-run dispatch selection input to the loaded assignment behaviors.

    The ONE application point the sim main and the control-plane dry run share
    (round-2 §5 C4). Applied in this order, per behavior, into ONE override entry
    that is merged once (``merge_policy_overrides``) and committed with the mirrors
    (:func:`apply_policy_overrides`' write):

    1. ``overrides`` (``ORSIM_POLICY_OVERRIDES``; ``--policy``/``--policy-param``),
       literally: ``dispatch.type`` is the TOP of the selection;
    2. ``topology`` (``ORSIM_PLANNER_TOPOLOGY``: ``partitioned``, ``pooled`` or
       ``two-stage``): the top becomes ``Partitioned`` / ``PooledMarket`` keeping the
       current per-scope solver; a new ``PooledMarket`` takes its market from the
       profile's ``planner.market`` (strictly). Message 10 when ``overrides`` names
       another top;
    3. the market variables (``offer``/``claim``/``arbitration`` component names,
       ``max_rounds`` an int or an integer string in 1..100): ``PooledMarket`` params;
       a changed component type resets its params. Message 9 when the top is not
       ``PooledMarket`` after step 2;
    4. ``solver`` (``--solver``/``ORSIM_SOLVER``): the INNERMOST per-scope solver (the
       ``inner`` chain; a leaf top is replaced); a changed type resets its params.
       Message 11 for a decomposition name, message 12 when ``overrides`` names a
       different type at that place.

    Every behavior is validated before any is mutated; nothing requested mutates
    nothing. Returns ``(merged_overrides, labels)``: the override object actually
    merged into the first behavior (``{}`` when nothing was requested), and
    ``{"dispatch": <run-name segment or None>, "topology": <canonical topology or
    None>}`` (:func:`dispatch_override_label`; the market variables add no segment,
    as before). Raises ``PolicyError`` (unknown names are ``UnknownPolicyError``).
    """
    from apps.container_logistics.policies.framework import PolicyError

    overrides = dict(overrides or {})
    if not isinstance(overrides.get("dispatch", {}), dict):
        raise PolicyError(f"policy overrides.dispatch must be an object (got {overrides['dispatch']!r}).")
    explicit = overrides.get("dispatch") or {}
    want_top = _topology_top(topology)
    market = _market_request(offer=offer, claim=claim, arbitration=arbitration, max_rounds=max_rounds)
    leaf = _checked_solver(solver)
    if want_top is None and not market and leaf is None and not overrides:
        return {}, {"dispatch": None, "topology": None}

    pairs = _profiles_with_current(assignment_collection)
    if not pairs:
        from apps.container_logistics.policies.framework import resolve_policies_block

        pairs = [(None, resolve_policies_block({}))]
    planned, first = [], None
    for profile, current in pairs:
        entry = _dispatch_entry(current, (profile or {}).get("planner"), explicit,
                                topology=topology, want_top=want_top, market=market, leaf=leaf)
        merged_overrides = dict(overrides)
        if entry:
            merged_overrides["dispatch"] = entry
        else:
            merged_overrides.pop("dispatch", None)
        merged = _merged(current, merged_overrides)
        if first is None:
            first = (merged_overrides, merged)
        if profile is not None:
            planned.append((profile, merged))
    _commit(planned)
    merged_overrides, merged = first
    labels = {
        "dispatch": dispatch_override_label(overrides, merged, solver=leaf),
        "topology": legacy_topology_name(topology),
    }
    return merged_overrides, labels


def legacy_topology_name(topology):
    """The canonical spelling (``partitioned``/``pooled``) of a topology request, ``None`` if unset.

    Raises ``PolicyError`` for an unknown value (the env converter is loud, round-2 §3.6).
    """
    from apps.container_logistics.policies.dispatch import legacy
    from apps.container_logistics.policies.framework import PolicyError

    if topology is None or (isinstance(topology, str) and not topology.strip()):
        return None
    name = topology.strip() if isinstance(topology, str) else topology
    canonical = legacy.LEGACY_TOPOLOGIES.get(name) if isinstance(name, str) else None
    if canonical is None:
        raise PolicyError(
            f"ORSIM_PLANNER_TOPOLOGY={topology!r} is not a legacy topology "
            f"({', '.join(legacy.LEGACY_TOPOLOGIES)}). The decomposition is now the dispatch "
            f"policy: --policy dispatch={legacy.PARTITIONED} or --policy dispatch={legacy.POOLED_MARKET}."
        )
    return canonical


def _topology_top(topology):
    from apps.container_logistics.policies.dispatch import legacy

    canonical = legacy_topology_name(topology)
    if canonical is None:
        return None
    return legacy.POOLED_MARKET if canonical == "pooled" else legacy.PARTITIONED


def _market_request(**values):
    """``{param: value}`` of the market variables that were given; ``max_rounds`` checked as an int in 1..100."""
    from apps.container_logistics.policies.dispatch import legacy
    from apps.container_logistics.policies.framework import PolicyError

    out = {}
    for key, env in MARKET_ENV:
        value = values.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if key != "max_rounds":
            if not isinstance(value, str):
                raise PolicyError(f"{env}={value!r} must name a {key} component.")
            out[key] = value.strip()
            continue
        rounds = value
        if isinstance(value, str) and re.fullmatch(r"\s*[+-]?\d+\s*", value):
            rounds = int(value)
        low, high = legacy.LEGACY_MAX_ROUNDS_RANGE
        if isinstance(rounds, bool) or not isinstance(rounds, int) or not low <= rounds <= high:
            raise PolicyError(
                f"{env}={value!r} must be an integer in {low}..{high}. It is the pooled "
                "auction's safety backstop; the auction ends on convergence."
            )
        out[key] = rounds
    return out


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
        spec = get_policy_spec("dispatch", str(solver).strip())
    except UnknownPolicyError as exc:
        raise UnknownPolicyError(f"--solver {str(solver).strip()!r}: {exc}") from exc
    if spec.is_composite:
        leaves = ", ".join(s.name for s in list_policies("dispatch") if not s.is_composite)
        raise PolicyError(
            f"--solver {spec.name!r} names a decomposition, not a per-scope solver. --solver "
            f"sets the inner solver ({leaves}); select a decomposition with --policy "
            f"dispatch={spec.name}."
        )
    return spec.name


def _dispatch_entry(current, planner, explicit, *, topology, want_top, market, leaf):
    """The ONE dispatch override entry for a profile whose resolved selection is ``current``."""
    from apps.container_logistics.policies.dispatch import legacy
    from apps.container_logistics.policies.framework import PolicyError

    entry = copy.deepcopy(explicit)
    named_top = entry.get("type").strip() if isinstance(entry.get("type"), str) else None
    top = named_top or current["dispatch"].name
    if want_top is not None:
        if named_top is not None and named_top != want_top:
            raise PolicyError(
                f"ORSIM_PLANNER_TOPOLOGY={topology!r} selects {want_top} but ORSIM_POLICY_OVERRIDES "
                f"sets dispatch.type={entry['type']!r}; pass one of them."
            )
        if top != want_top:
            entry["type"] = top = want_top
            if want_top == legacy.POOLED_MARKET:
                params = entry.setdefault("params", {})
                for key, value in legacy.legacy_market_params(planner).items():
                    params[key] = _over(value, params.get(key))
    if market:
        if top != legacy.POOLED_MARKET:
            key, env = next((k, e) for k, e in MARKET_ENV if k in market)
            label = _merged(current, {"dispatch": entry})["dispatch"].label if entry else current["dispatch"].label
            raise PolicyError(
                f"{env}={market[key]!r} configures the PooledMarket dispatch, but this run's dispatch "
                f"is {label}. Add ORSIM_PLANNER_TOPOLOGY=pooled, or select --policy "
                f"dispatch=PooledMarket --policy-param dispatch.{key}={market[key]}."
            )
        params = entry.setdefault("params", {})
        stated = explicit.get("params") if isinstance(explicit.get("params"), dict) else {}
        for key, value in market.items():
            _refuse_market_conflict(key, value, stated.get(key))
            if key == "max_rounds":
                params[key] = value
            else:
                params[key] = _with_type(params.get(key), value)
    if leaf is not None:
        _set_innermost(entry, explicit, current, leaf)
    return entry


def _over(base, stated):
    """A legacy market value under what ``overrides`` stated for the same key (the statement wins)."""
    if stated is None:
        return base
    if isinstance(base, dict) and isinstance(stated, dict) and "type" not in stated:
        return {"type": base["type"], "params": {**base.get("params", {}), **(stated.get("params") or {})}}
    return stated


def _with_type(value, name):
    """A component ref set to ``name``: the same type keeps its params, another type starts from none."""
    if isinstance(value, str):
        value = {"type": value}
    if isinstance(value, dict) and value.get("type") in (None, name):
        return {**value, "type": name}
    return {"type": name}


def _refuse_market_conflict(key, value, stated):
    from apps.container_logistics.policies.framework import PolicyError

    env = dict(MARKET_ENV)[key]
    have = stated.get("type") if isinstance(stated, dict) else stated
    if have is None or (key != "max_rounds" and isinstance(have, str) and have.strip() == value) \
            or (key == "max_rounds" and not isinstance(have, bool) and have == value):
        return
    path = f"dispatch.{key}" if key == "max_rounds" else f"dispatch.{key}.type"
    raise PolicyError(
        f"{env}={value!r} conflicts with {path}={have!r} from ORSIM_POLICY_OVERRIDES "
        "(--policy-param/--policy-overrides); pass one of them."
    )


def _set_innermost(entry, explicit, current, leaf):
    """Point ``entry`` at ``leaf`` as the innermost per-scope solver (in place); message 12 on a conflict."""
    from apps.container_logistics.policies.framework import PolicyError

    trial = _merged(current, {"dispatch": entry})["dispatch"] if entry else current["dispatch"]
    path, innermost = innermost_path(trial)
    if innermost == leaf:
        return
    stated = _type_at(explicit, path)
    if stated is not None and stated != leaf:
        where = ".".join(["dispatch", *path, "type"])
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
        descend = get_policy_spec("dispatch", name).inner_param
        child = params.get(descend) if descend is not None else None
        if not isinstance(child, dict):
            return path, name
        path.append(descend)
        name, params = child["type"], child.get("params") or {}


def _type_at(entry, path):
    """The type ``entry`` states at ``path`` (``dispatch.params.<p>...``), or ``None``."""
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
    name, ``resolved`` the merged ``{slot: ResolvedPolicy}``. The topology and market
    variables never contribute (they keep their own segment / none, as before).
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

    entry = overrides.get("dispatch") if isinstance(overrides, dict) else None
    entry = entry if isinstance(entry, dict) else {}
    node = resolved["dispatch"]
    levels = []  # (name, resolved params, stated entry, inner param)
    name, params, stated = node.name, node.params, entry
    while True:
        descend = get_policy_spec("dispatch", name).inner_param
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
    """
    if not strategy:
        return None
    run_dispatch_overrides(assignment_collection, solver=strategy)
    return _checked_solver(strategy)


def apply_cooperation_override(assignment_collection, structure=None, sharing=None):
    """Per-run collaboration overrides (mirror of ``apply_solver_override``).

    ``structure``: id of a structure declared in the baked ``profile.cooperation`` —
    patches ``cooperation.active`` in place. ``sharing``: sharing-algorithm name —
    patches ``planner.sharing.type``. Both fail-soft: unknown/falsy values are
    ignored with a warning and the scenario's own defaults stand. Mutates the loaded
    behaviors only (never the on-disk bundle). Returns the applied structure id (or
    ``None``).
    """
    applied = None
    for behavior in (assignment_collection or {}).values():
        if not isinstance(behavior, dict):
            continue
        profile = behavior.setdefault("profile", {})
        coop = profile.get("cooperation")
        if structure and isinstance(coop, dict):
            # Compiled structure ids are slugs — slugify the override input too so
            # the authored form ("Port Alliance") matches ("port-alliance") instead
            # of silently keeping the scenario default (F6).
            from apps.container_logistics.datagen.hauliers import structure_id_slug

            wanted = structure_id_slug(structure) or structure
            declared = {s.get("id") for s in coop.get("structures", []) if isinstance(s, dict)}
            if wanted in declared:
                coop["active"] = wanted
                applied = wanted
            else:
                logging.warning(
                    "Ignoring unknown cooperation structure override %r (declared: %s); "
                    "using the scenario's active structure.",
                    structure,
                    ", ".join(sorted(d for d in declared if d)),
                )
        elif structure:
            logging.warning(
                "Cooperation structure override %r requested but this scenario has no "
                "baked cooperation block (recompile it with the new pipeline).",
                structure,
            )
        if sharing:
            planner = profile.get("planner")
            if not isinstance(planner, dict):
                # Aligned with ``apply_planner_topology_override``: CREATE the block
                # on a legacy bundle rather than warn-and-refuse. Both behaviours
                # were defensible; having both in adjacent modules was not (review
                # F17). Creating wins because §8 promises old bundles stay runnable
                # under a per-run override without recompiling.
                logging.info(
                    "Sharing override %r: creating a planner block (bundle predates it).",
                    sharing,
                )
                planner = {}
                profile["planner"] = planner
            planner["sharing"] = {"type": str(sharing), "params": dict(
                (planner.get("sharing") or {}).get("params") or {}
            )}
    return applied


def known_planner_topologies():
    """Planner topologies accepted by ``apply_planner_topology_override`` — sourced
    from the same constant ``Preprocessor._normalize_planner`` validates against, so
    CLI/override validation and the compiler can never drift (plan §6.11/§7)."""
    from apps.container_logistics.datagen.preprocess import PLANNER_TOPOLOGIES

    return tuple(PLANNER_TOPOLOGIES)


def apply_planner_topology_override(assignment_collection, topology):
    """Per-run planner-topology override: ``ORSIM_PLANNER_TOPOLOGY``.

    A LOUD wrapper of :func:`run_dispatch_overrides` (``topology=topology``): the
    dispatch top becomes ``PooledMarket`` (``pooled``; ``two-stage`` is its deprecated
    alias) or ``Partitioned``, keeping the current per-scope solver; the legacy
    ``planner.topology`` / ``planner.market`` keys are rewritten as mirrors. Mutates the
    loaded behaviors only; the on-disk bundle is never touched. Returns the canonical
    topology, or ``None`` for a falsy value (nothing changed). Raises ``PolicyError``
    for an unknown topology (it used to be ignored with a warning).
    """
    _merged_overrides, labels = run_dispatch_overrides(assignment_collection, topology=topology)
    return labels["topology"]


def apply_market_override(
    assignment_collection, *, offer=None, claim=None, arbitration=None, max_rounds=None
):
    """Per-run pooled-market overrides: ``ORSIM_OFFER_POLICY`` / ``_CLAIM_`` / ``_ARBITRATION_`` / ``MAX_ROUNDS``.

    A LOUD wrapper of :func:`run_dispatch_overrides`: each given component name
    becomes that ``PooledMarket`` param (a changed type resets its params) and
    ``max_rounds`` (an int, or an integer string, in 1..100) the round backstop; the
    ``planner.market`` mirror is rewritten in the legacy shape. Returns the applied
    patch ``{role: name, max_rounds: int}``, or ``None`` when nothing was requested.
    Raises ``UnknownPolicyError`` for an unknown component, ``PolicyError`` for a
    ``max_rounds`` outside 1..100 or when the dispatch is not ``PooledMarket``
    (message 9). All used to be ignored or deferred to the runtime's fail-soft
    registries.
    """
    patch = _market_request(offer=offer, claim=claim, arbitration=arbitration, max_rounds=max_rounds)
    if not patch:
        return None
    run_dispatch_overrides(assignment_collection, **patch)
    return patch


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
