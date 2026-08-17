"""Temporary scenario_config overrides (e.g. smoke generation)."""

import logging
from contextlib import contextmanager
from copy import deepcopy

from .validation_config import smoke_scenario_config_bundle


def known_solvers():
    """Solver strategy names accepted by ``apply_solver_override`` — sourced from the
    single registry the assignment agent itself uses, so the two never drift."""
    from apps.container_logistics.assignment.solver import SOLVER_REGISTRY

    return tuple(SOLVER_REGISTRY.keys())


def apply_solver_override(assignment_collection, strategy):
    """Set ``profile.strategy`` on every assignment behavior in ``assignment_collection``
    (a ``{agent_id: behavior}`` dict) so a run uses ``strategy`` without regenerating the
    scenario or touching the on-disk behavior files.

    Mutates the behaviors in place (same objects the agent source ships to the workers).
    Returns the applied strategy, or ``None`` if ``strategy`` is unknown / falsy — in which
    case nothing is changed and the scenario's own default solver stands (the assignment
    agent's ``get_solver`` is itself lenient about unknown names).
    """
    if not strategy:
        return None
    valid = known_solvers()
    if strategy not in valid:
        logging.warning(
            "Ignoring unknown solver override %r (known: %s); using scenario default.",
            strategy,
            ", ".join(valid),
        )
        return None
    for behavior in (assignment_collection or {}).values():
        if isinstance(behavior, dict):
            behavior.setdefault("profile", {})["strategy"] = strategy
    return strategy


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
