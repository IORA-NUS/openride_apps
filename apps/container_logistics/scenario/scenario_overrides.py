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
    """The resolved policies of one assignment ``profile``: ``{slot: ResolvedPolicy}``.

    Reads the compiled ``profile.policies`` block (its ``version``/``kind`` are
    re-derived from the registry), or, for a bundle compiled before policy slots, the
    legacy translation of ``strategy``/``solver_params``/``use_spatial_matching``
    (``dispatch/profile.py::legacy_dispatch_from_profile``). Raises ``PolicyError``.
    Returns ``(resolved, source)`` with ``source`` ``"bundle"`` or ``"legacy-profile"``.
    """
    from apps.container_logistics.policies.dispatch.profile import legacy_dispatch_from_profile
    from apps.container_logistics.policies.framework import PolicyError, resolve_policies_block

    block = profile.get("policies")
    if block is None:
        entry = legacy_dispatch_from_profile(profile)
        return resolve_policies_block({"dispatch": entry}, where="profile.policies",
                                      legacy_slots=("dispatch",)), "legacy-profile"
    if not isinstance(block, dict):
        raise PolicyError(f"assignment profile 'policies' must be an object (got {block!r}). Recompile the scenario.")
    if profile.get("use_spatial_matching", True) is not True:
        raise PolicyError(
            "assignment profile sets use_spatial_matching="
            f"{profile['use_spatial_matching']!r} next to a 'policies' block; it is superseded "
            "by policies.dispatch.params.candidates. Remove use_spatial_matching."
        )
    authored = {
        slot: {k: v for k, v in entry.items() if k in ("type", "timing", "params")}
        if isinstance(entry, dict) else entry
        for slot, entry in block.items()
    }
    return resolve_policies_block(authored, where="profile.policies"), "bundle"


def apply_policy_overrides(assignment_collection, overrides):
    """Apply per-run policy ``overrides`` to every assignment behavior, in memory.

    ``overrides`` is ``{slot: {type?, timing?, params?}}`` (see
    ``framework.resolve.merge_policy_overrides``: a changed type resets params to the
    new policy's defaults). Each profile gets ``profile["policies"]`` (resolved,
    ``to_profile`` form) and the ``profile["strategy"]`` mirror; ``solver_params`` is
    NOT touched (it is the framework's accounting dial). A legacy profile's
    ``use_spatial_matching`` is removed once folded into the block, because the
    runtime resolver refuses it next to ``policies``. Every behavior is validated
    before any is mutated. Returns the first behavior's resolved policies, or ``None``
    for empty ``overrides``. Raises ``PolicyError``.
    """
    from apps.container_logistics.policies.framework import merge_policy_overrides

    if not overrides:
        return None
    planned = []
    for behavior in (assignment_collection or {}).values():
        if not isinstance(behavior, dict):
            continue
        profile = behavior.setdefault("profile", {})
        current, _source = current_policies_for_profile(profile)
        planned.append((profile, merge_policy_overrides(current, overrides)))
    for profile, merged in planned:
        profile["policies"] = {slot: r.to_profile() for slot, r in merged.items()}
        profile["strategy"] = merged["dispatch"].name
        profile.pop("use_spatial_matching", None)
    if planned:
        return planned[0][1]
    from apps.container_logistics.policies.framework import resolve_policies_block

    return merge_policy_overrides(resolve_policies_block({}), overrides)


def apply_solver_override(assignment_collection, strategy):
    """Per-run dispatch policy override by name: ``--solver X`` / ``ORSIM_SOLVER``.

    A thin wrapper: ``apply_policy_overrides(coll, {"dispatch": {"type": strategy}})``,
    so a changed type starts from the new policy's default params. Returns the
    registered name, or ``None`` if ``strategy`` is falsy (nothing changed). Raises
    ``UnknownPolicyError`` for an unregistered name, before anything is mutated (an
    intended contract change from the old warn-and-ignore; plan
    ``docs/plans/policy_slots_p0_p3.md`` §4 P0.7 and P2.5).
    """
    if not strategy:
        return None
    resolved = apply_policy_overrides(assignment_collection, {"dispatch": {"type": strategy}})
    return resolved["dispatch"].name


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
    return resolved


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
        applied = dict(patch)
    return applied


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
