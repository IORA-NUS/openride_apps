"""Temporary scenario_config overrides (e.g. smoke generation)."""

import logging
from contextlib import contextmanager
from copy import deepcopy

from .validation_config import smoke_scenario_config_bundle


def known_solvers():
    """Registered dispatch policy names, sorted -- read from the policy registry at
    call time (the same registry the assignment agent resolves against)."""
    from apps.container_logistics.policies.framework import list_policies

    return tuple(spec.name for spec in list_policies("dispatch"))


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
    ``framework.resolve.merge_policy_overrides``). Each profile gets
    ``profile["policies"]`` (resolved, ``to_profile`` form) and -- exactly as compile
    does -- the selection's legacy mirrors (``strategy`` = the innermost solver, the
    keys ``solver_params`` and ``planner.deployment.params`` already carry,
    ``planner.topology``, ``planner.market``;
    ``dispatch/profile.py::mirror_dispatch_selection``). So a per-run arm equals the
    same edit compiled into ``spec.json``: the framework's accounting
    (``benefit_km``, owner reserve, claims, arbitration) ranks with what the policy
    ranks with (review F1).

    A changed dispatch TYPE (``--solver X``, ``--policy dispatch=X``) resets the
    policy params to X's defaults plus any override params; the mirror then rewrites
    the shared keys in ``solver_params`` to those values whenever X declares them
    (GreedyNearest: the dual-cycle dials). When X does not declare them
    (RandomAssignment), ``solver_params`` keep the bundle's values, i.e. the
    accounting is unchanged from the bundle. A legacy profile's
    ``use_spatial_matching`` is removed once folded into the block, because the
    runtime resolver refuses it next to ``policies``. Every behavior is validated
    before any is mutated. Returns the first behavior's resolved policies, or
    ``None`` for empty ``overrides``. Raises ``PolicyError``.
    """
    from apps.container_logistics.policies.dispatch.profile import mirror_dispatch_selection
    from apps.container_logistics.policies.framework import merge_policy_overrides

    if not overrides:
        return None
    planned = []
    for behavior in (assignment_collection or {}).values():
        if not isinstance(behavior, dict):
            continue
        profile = behavior.setdefault("profile", {})
        current, _source = current_policies_for_profile(profile)
        planned.append((profile, merge_policy_overrides(current, leaf_retargeted(current, overrides))))
    for profile, merged in planned:
        profile["policies"] = {slot: r.to_profile() for slot, r in merged.items()}
        profile.pop("use_spatial_matching", None)
        mirror_dispatch_selection(profile, merged["dispatch"])
    if planned:
        return planned[0][1]
    from apps.container_logistics.policies.framework import resolve_policies_block

    current = resolve_policies_block({})
    return merge_policy_overrides(current, leaf_retargeted(current, overrides))


def leaf_retargeted(current, overrides):
    """``overrides`` with a dispatch TYPE that names a per-scope solver aimed at the innermost solver.

    The per-run override channels (``--solver``, ``ORSIM_SOLVER``, ``--policy
    dispatch=X``) fold into ``dispatch.type``. When ``X`` is a leaf and the current
    selection is a decomposition (``Partitioned``/``PooledMarket``), ``X`` sets the
    decomposition's innermost solver (its ``inner_param``) and the entry's params are
    that solver's: the pre-v2 meaning of ``--solver`` (round-2 §3.3). Anything else is
    returned unchanged. ``current`` is the profile's ``{slot: ResolvedPolicy}``.
    TRANSITIONAL (round-2 C3): work item C4 separates ``--solver`` from ``--policy``
    (``run_dispatch_overrides``), after which ``--policy dispatch=GreedyNearest``
    selects a central GreedyNearest.
    """
    from apps.container_logistics.policies.framework import get_policy_spec

    entry = overrides.get("dispatch") if isinstance(overrides, dict) else None
    top = current.get("dispatch")
    if not isinstance(entry, dict) or not isinstance(entry.get("type"), str) or top is None:
        return overrides
    named = get_policy_spec("dispatch", entry["type"].strip())
    descend = get_policy_spec("dispatch", top.name).inner_param
    if named.is_composite or descend is None:
        return overrides
    inner = {"type": named.name}
    if entry.get("params") is not None:
        inner["params"] = entry["params"]
    retargeted = {k: v for k, v in entry.items() if k not in ("type", "params")}
    retargeted["params"] = {descend: inner}
    return {**overrides, "dispatch": retargeted}


def apply_solver_override(assignment_collection, strategy):
    """Per-run dispatch policy override by name: ``--solver X`` / ``ORSIM_SOLVER``.

    A thin wrapper: ``apply_policy_overrides(coll, {"dispatch": {"type": strategy}})``,
    so a changed type starts from the new policy's default params; under a
    decomposition it is the innermost solver (:func:`leaf_retargeted`). Returns the
    registered name of the innermost solver, or ``None`` if ``strategy`` is falsy
    (nothing changed). Raises
    ``UnknownPolicyError`` for an unregistered name, before anything is mutated (an
    intended contract change from the old warn-and-ignore; plan
    ``docs/plans/policy_slots_p0_p3.md`` §4 P0.7 and P2.5).
    """
    if not strategy:
        return None
    resolved = apply_policy_overrides(assignment_collection, {"dispatch": {"type": strategy}})
    return resolved["dispatch"].leaf


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
    """Per-run planner-topology override (mirror of ``apply_solver_override``).

    Patches ``profile.planner.topology`` on every loaded assignment behavior so the
    SAME compiled scenario can be run ``partitioned`` or ``pooled`` with zero file
    edits — exactly what the A/B ladder needs (plan §D5). Mutates the loaded
    behaviors only; the on-disk bundle is never touched.

    ``"two-stage"`` is a deprecated alias and resolves to ``"pooled"``, matching the
    compiler. Fail-soft: an unknown/falsy topology is ignored with a warning and the
    scenario's own topology stands. Returns the applied (resolved) topology, or
    ``None``.
    """
    if not topology:
        return None
    valid = known_planner_topologies()
    if topology not in valid:
        logging.warning(
            "Ignoring unknown planner topology override %r (known: %s); using the "
            "scenario's own topology.",
            topology,
            ", ".join(valid),
        )
        return None
    resolved = "pooled" if topology == "two-stage" else topology
    for behavior in (assignment_collection or {}).values():
        if isinstance(behavior, dict):
            profile = behavior.setdefault("profile", {})
            planner = profile.get("planner")
            if not isinstance(planner, dict):
                # Legacy bundles compiled before the planner block existed: create it
                # rather than refuse, so an old scenario is still runnable both ways.
                planner = {}
                profile["planner"] = planner
            planner["topology"] = resolved
            _retarget_compiled_decomposition(profile, resolved)
    return resolved


def _rewrite_compiled_dispatch(profile, override_entry):
    """Merge ``override_entry`` into ``profile``'s compiled dispatch and re-mirror (in place)."""
    from apps.container_logistics.policies.dispatch.profile import mirror_dispatch_selection
    from apps.container_logistics.policies.framework import merge_policy_overrides

    current, _source = current_policies_for_profile(profile)
    merged = merge_policy_overrides(current, {"dispatch": override_entry})
    profile["policies"] = {slot: r.to_profile() for slot, r in merged.items()}
    mirror_dispatch_selection(profile, merged["dispatch"])


def _retarget_compiled_decomposition(profile, topology):
    """Make a COMPILED ``policies.dispatch`` follow a ``planner.topology`` override (in place).

    A profile without ``policies`` is a legacy profile: its translation reads
    ``planner.topology`` itself. With a compiled block (authoritative at run start,
    round-2 §3.6) the top becomes ``PooledMarket`` (market params from
    ``profile.planner.market``, strictly) or ``Partitioned``, keeping the current
    per-scope solver as its inner (a central leaf becomes the inner): exactly what
    ``--policy dispatch=PooledMarket`` does. TRANSITIONAL:
    round-2 C4 replaces this with ``run_dispatch_overrides``.
    """
    from apps.container_logistics.policies.dispatch import legacy

    if not isinstance(profile.get("policies"), dict):
        return
    current, _source = current_policies_for_profile(profile)
    want = legacy.POOLED_MARKET if topology == "pooled" else legacy.PARTITIONED
    if current["dispatch"].name == want:
        return
    # A changed top keeps the current per-scope solver (merge_policy_overrides), the
    # same rule as --policy dispatch=<want>; the market comes from planner.market.
    params = dict(legacy.legacy_market_params(profile.get("planner"))) if want == legacy.POOLED_MARKET else {}
    _rewrite_compiled_dispatch(profile, {"type": want, "params": params})


def apply_market_override(
    assignment_collection, *, offer=None, claim=None, arbitration=None, max_rounds=None
):
    """Per-run pooled-market overrides (offer/claim/arbitration policy + rounds).

    Patches ``profile.planner.market`` in place on the loaded behaviors only. Each
    argument is optional — only the ones supplied are changed, and each policy's
    existing ``params`` are preserved (only ``type`` is replaced), mirroring how
    ``apply_cooperation_override`` patches ``planner.sharing``.

    Algorithm NAMES are deliberately NOT validated here: they are fail-soft at
    runtime (the registries fall back to their default and log), exactly like
    ``deployment.type``. ``max_rounds`` IS a framework constant, so a value outside
    ``1..10`` is rejected with a warning. Returns the applied market patch dict, or
    ``None`` when nothing was requested/applied.
    """
    patch = {}
    for role, value in (("offer", offer), ("claim", claim), ("arbitration", arbitration)):
        if value:
            patch[role] = str(value)
    if max_rounds is not None:
        try:
            rounds = int(max_rounds)
        except (TypeError, ValueError):
            logging.warning("Ignoring non-integer market max_rounds override %r.", max_rounds)
            rounds = None
        else:
            if 1 <= rounds <= 10:
                patch["max_rounds"] = rounds
            else:
                logging.warning(
                    "Ignoring out-of-range market max_rounds override %r (must be 1..10).",
                    max_rounds,
                )
    if not patch:
        return None

    applied = None
    for behavior in (assignment_collection or {}).values():
        if not isinstance(behavior, dict):
            continue
        profile = behavior.setdefault("profile", {})
        planner = profile.get("planner")
        if not isinstance(planner, dict):
            planner = {}
            profile["planner"] = planner
        market = planner.get("market")
        if not isinstance(market, dict):
            market = {}
            planner["market"] = market
        for role in ("offer", "claim", "arbitration"):
            if role in patch:
                existing = market.get(role)
                params = dict(existing.get("params") or {}) if isinstance(existing, dict) else {}
                market[role] = {"type": patch[role], "params": params}
        if "max_rounds" in patch:
            market["max_rounds"] = patch["max_rounds"]
        _patch_compiled_market(profile, patch)
        applied = dict(patch)
    return applied


def _patch_compiled_market(profile, patch):
    """Apply a market patch to a COMPILED ``PooledMarket`` selection too, and re-mirror (in place).

    A compiled ``policies`` block is authoritative at run start, so ``planner.market``
    alone would change nothing there. A changed component type resets that
    component's params to its defaults (the nested-merge rule); the same type keeps
    them. A compiled top that is not ``PooledMarket`` keeps ``planner.market`` as inert
    pooled-arm configuration (read by a later topology override). TRANSITIONAL:
    round-2 C4 replaces this with ``run_dispatch_overrides`` (message 9).
    """
    from apps.container_logistics.policies.dispatch import legacy

    if not isinstance(profile.get("policies"), dict):
        return
    current, _source = current_policies_for_profile(profile)
    if current["dispatch"].name != legacy.POOLED_MARKET:
        return
    params = {role: {"type": patch[role]} for role in legacy.MARKET_ROLES if role in patch}
    if "max_rounds" in patch:
        params["max_rounds"] = patch["max_rounds"]
    _rewrite_compiled_dispatch(profile, {"params": params})


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
