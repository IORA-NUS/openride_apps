"""The container-logistics sim main: load the scenario, apply the run's overrides, bind, spawn.

Single responsibility: the run-start ENTRY PATH. ``main()`` reads the run's
``ORSIM_*`` inputs (``scenario_overrides`` holds the readers the control-plane dry run
shares), patches the loaded behaviors in memory, runs the run-start policy check and
binding, writes the framework's run settings (:data:`FRAMEWORK_RUN_SETTINGS`, only
through :func:`_set_run_setting`) and hands everything to ``SimulationRuntime``.

Every policy refusal before an agent spawns is exit 2 with the message printed
(:func:`_refuse_invalid_policy_config`); ``main()`` maps ANY ``PolicyError`` raised by
its policy section to it, so a bind-time error never surfaces as a traceback and
exit 1.
"""

import json
import logging
import os
import sys
from types import SimpleNamespace

from apps.container_logistics.scenario.scenario_config import default_scenario_name
from apps.container_logistics.scenario.scenario_manager import ScenarioManager
from apps.container_logistics.scenario.scenario_overrides import (
    REMOVED_SHARING_ENV as _REMOVED_SHARING_ENV,  # noqa: F401 - kept importable from here
    apply_assignment_run_overrides,
    apply_cooperation_override,
    assignment_cadence_from_env,
    assignment_run_input_records,
    current_policies_for_profile,
    dispatch_override_label,  # noqa: F401 - re-exported (the run-name label format lives there)
    max_orders_per_tick_from_env,
    order_lifecycle_from_env,
    policy_overrides_from_env,
    refuse_removed_sharing_env,
    run_dispatch_overrides,
    ship_planning_world,
    solver_from_env,
    structure_from_env,
)
from apps.simulation.container_logistics_wiring import (
    build_agent_source,
    build_scheduler_config,
    build_termination_condition,
    ensure_order_lifecycle_behavior,
    get_agent_config,
    get_datahub_dir,
    get_domain,
    get_statemachine_collection,
    kafka_progress_listener,
    new_run_id,
)
from apps.simulation.preflight import assert_simulation_dependencies
from apps.simulation.simulation_runtime import SimulationRuntime


from apps.container_logistics.scenario.frontend_scenario_spec import sanitize_run_name
from apps.container_logistics.policies.dispatch.profile import (
    assert_assignment_profiles_resolvable,
)
from apps.container_logistics.policies.compat.legacy_env import read_legacy_dispatch_env
from apps.container_logistics.policies.framework import PolicyError
from apps.container_logistics.scenario.run_declaration import (
    FRAMEWORK_RUN_SETTINGS as _FRAMEWORK_RUN_SETTINGS,
    append_run_name_segments,
    check_run_declaration,
    axis_explicit_record,
    declared_planning,
    PLANNING_CADENCE_PROFILE_KEY,
    planning_cadence_mismatches,
    planning_orphan_timeout,
    planning_run_name_segment,
    ship_planning_cadence,
    working_hours_run_name_segment,
)


def _resolve_scenario_name() -> str:
    return os.environ.get("ORSIM_SCENARIO", default_scenario_name()).strip()


def _resolve_run_name() -> str | None:
    return sanitize_run_name(os.environ.get("ORSIM_RUN_NAME"))


def _refuse_invalid_policy_config(error: PolicyError) -> None:
    """Print the policy configuration error and exit 2 before any agent spawns."""
    print(f"Policy configuration invalid; refusing to start the run: {error}")
    sys.exit(2)


#: EVERY run setting ``main()`` itself writes into ``scenario_manager.orsim_settings``
#: (which ships to the agents and becomes ``run_config.meta``). The ONE list, defined in
#: ``scenario.run_declaration`` (the pure run-declaration check the control-plane dry run
#: and scenario compile share) and re-exported here: every write goes through
#: :func:`_set_run_setting`, which refuses a key missing from it, and
#: ``tests/test_run_entry_ownership.py`` fails on a subscript write that bypasses the
#: helper. A solver ``stamp_settings`` key in this set -- or in the wider
#: ``run_declaration.reserved_run_settings()`` (every setting framework code READS,
#: round-2 review M-B), or already in the settings -- is refused whether or not this run
#: writes it: a key the framework writes later (``STREAM_GEO`` in headless mode,
#: ``PERSIST_ROUTE_GEO``, ``ORDER_LIFECYCLE``) would otherwise silently overwrite the
#: solver's value, or the solver's would stand on a run where the framework does not
#: write it. ``COOPERATION`` is framework-owned too (final review M3): derived from the
#: run's active arm in WORLD data, never from what a solver chose to stamp.
FRAMEWORK_RUN_SETTINGS = _FRAMEWORK_RUN_SETTINGS


def _set_run_setting(scenario_manager, key: str, value) -> None:
    """``scenario_manager.orsim_settings[key] = value`` for a key in :data:`FRAMEWORK_RUN_SETTINGS`.

    Raises ``KeyError`` for any other key: a new framework-written setting is added to
    the list first, so the solver collision guard can never miss it (final review M1).
    """
    if key not in FRAMEWORK_RUN_SETTINGS:
        raise KeyError(
            f"run setting {key!r} is not in FRAMEWORK_RUN_SETTINGS; add it there so the "
            "stamp_settings collision guard refuses it."
        )
    scenario_manager.orsim_settings[key] = value


def _assert_policies_resolvable_or_exit(assignment_collection, patched_world_keys=frozenset()) -> None:
    """Exit 2 (via :func:`_refuse_invalid_policy_config`) on an unresolvable dispatch policy.

    ``patched_world_keys``: the world keys a per-run override patched (message 25 when
    the bound planner tree reads none of them).
    """
    try:
        assert_assignment_profiles_resolvable(
            assignment_collection, patched_world_keys=patched_world_keys
        )
    except PolicyError as exc:
        _refuse_invalid_policy_config(exc)


def _refuse_removed_sharing_env_or_exit() -> None:
    """Exit 2 when the removed ``ORSIM_SHARING_POLICY`` is set (``scenario_overrides.refuse_removed_sharing_env``)."""
    try:
        refuse_removed_sharing_env(os.environ)
    except PolicyError as exc:
        _refuse_invalid_policy_config(exc)


def _apply_cooperation_override_or_exit(assignment_collection) -> tuple[str | None, frozenset]:
    """``ORSIM_COOP_STRUCTURE`` onto the loaded assignment profiles; exit 2 on message 26.

    Returns ``scenario_overrides.apply_cooperation_override``'s ``(applied_id,
    patched_world_keys)``, ``(None, frozenset())`` when the variable is unset.
    """
    structure = structure_from_env(os.environ)
    try:
        return apply_cooperation_override(assignment_collection, structure)
    except PolicyError as exc:
        _refuse_invalid_policy_config(exc)


def _resolve_solver() -> str | None:
    # Per-run per-scope solver override (set by the control layer / frontend). Applied to
    # the loaded assignment behavior in-memory before agents spawn — no scenario regen, no
    # celery restart (the solver registry already lives in the worker). See CLAUDE.md.
    return solver_from_env(os.environ)


def _resolve_policy_overrides() -> dict:
    """Per-run policy overrides: ``ORSIM_POLICY_OVERRIDES`` (JSON), ``{}`` when unset.

    ``ORSIM_SOLVER`` travels separately (the innermost per-scope solver, round-2 §3.3);
    :func:`_apply_dispatch_overrides_or_exit` combines the two. Raises ``PolicyError``
    for malformed JSON (``scenario_overrides.policy_overrides_from_env``).
    """
    return policy_overrides_from_env(os.environ)


def _apply_dispatch_overrides_or_exit(assignment_collection) -> tuple[dict, dict]:
    """Apply every per-run dispatch input in one place; exit 2 on any ``PolicyError``.

    ``ORSIM_POLICY_OVERRIDES``, the legacy dispatch variables (read by
    ``compat.legacy_env.read_legacy_dispatch_env`` and handed through opaquely) and
    ``ORSIM_SOLVER``, in that order, through
    ``scenario_overrides.run_dispatch_overrides`` (the function the control plane's
    dry run calls too). Returns ``(merged_overrides, labels)``: ``labels`` holds the
    ``planner`` run-name segment (or ``None``) and the ``legacy`` segments (a list).
    """
    try:
        return run_dispatch_overrides(
            assignment_collection,
            overrides=_resolve_policy_overrides(),
            solver=_resolve_solver(),
            legacy_env=read_legacy_dispatch_env(os.environ),
        )
    except PolicyError as exc:
        _refuse_invalid_policy_config(exc)


def _apply_capture_env_or_exit(assignment_collection, datahub_dir: str, run_id: str) -> str | None:
    """Map ``ORSIM_CAPTURE_DISPATCH="every=N,max=M"`` onto every assignment profile.

    Sets ``profile["policy_capture"] = {dir, every_n_ticks, max_files, run_id}`` with
    ``dir = <datahub>/fixtures/dispatch/<run_id>`` (read by ``dispatch.capture``).
    Returns that directory, or ``None`` when the variable is unset. Exits 2 on a
    malformed value.
    """
    from apps.container_logistics.policies.dispatch.capture import parse_capture_spec

    raw = os.environ.get("ORSIM_CAPTURE_DISPATCH", "").strip()
    if not raw:
        return None
    try:
        spec = parse_capture_spec(raw)
    except PolicyError as exc:
        _refuse_invalid_policy_config(exc)
    directory = os.path.join(datahub_dir, "fixtures", "dispatch", run_id)
    for behavior in (assignment_collection or {}).values():
        if isinstance(behavior, dict):
            behavior.setdefault("profile", {})["policy_capture"] = {
                "dir": directory, **spec, "run_id": run_id,
            }
    return directory


def _stamp_effective_policies(scenario_manager, overridden: bool):
    """Stamp ``orsim_settings["POLICIES"]`` (-> ``run_config.meta.policies``) and print it.

    Built from the EFFECTIVE first assignment profile (post-override) and its BOUND
    selection (``dispatch.bind.bind_profile``, the binding the agent repeats), so the
    stamp's ``provenance`` / ``declaration`` / ``world_reads`` are what the run's
    solvers declared. Returns that bound tree (``None`` without an assignment
    profile). Exits 2 on a ``PolicyError``.
    """
    import json

    from apps.container_logistics.policies.dispatch.bind import bind_profile
    from apps.container_logistics.policies.framework import policies_stamp

    collection = scenario_manager.get_agent_collection("assignment") or {}
    if not collection:
        return None
    profile = next(iter(collection.values())).get("profile") or {}
    try:
        resolved, source = current_policies_for_profile(profile)
        tree = bind_profile(profile)
        stamp = policies_stamp(
            resolved,
            tree=tree,
            # What AssignmentApp._solver_params will cost with (same ``or {}`` read).
            accounting_params=profile.get("solver_params") or {},
            source="override" if overridden else source,
        )
    except PolicyError as exc:
        _refuse_invalid_policy_config(exc)
    _set_run_setting(scenario_manager, "POLICIES", stamp)
    d = stamp["planner"]
    print(
        f"Policies effective: planner={d['label']} v{d['version']} timing={d['timing']} "
        f"params={json.dumps(d['params'], sort_keys=True)} "
        f"accounting_params={json.dumps(d['accounting_params'], sort_keys=True)}"
    )
    return tree


def _first_assignment_profile(scenario_manager):
    """The first assignment behavior's ``profile`` (post-override), ``{}`` without one."""
    get = getattr(scenario_manager, "get_agent_collection", None)
    collection = (get("assignment") if get is not None else None) or {}
    behavior = next(iter(collection.values()), None)
    return (behavior.get("profile") if isinstance(behavior, dict) else None) or {}


def _apply_run_declaration(scenario_manager, tree, run_name: str | None, profile=None,
                           lifecycle_mode: str | None = None) -> str | None:
    """Write the framework's ``COOPERATION``, merge the top's ``stamp_settings`` and run-name segments.

    The CHECK is ``scenario.run_declaration.check_run_declaration`` (pure; the
    control-plane dry run and scenario compile call the same function, round-2 review
    M-A): ``COOPERATION`` is framework-owned (the active arm of ``profile``'s
    cooperation block, after any ``--structure`` override; default: the first
    assignment profile) and a solver that also declares it must declare exactly that
    record; no other stamp setting may be one the settings already hold or a reserved
    framework setting; and no run-name segment may be one the framework writes or a
    reader parses as another arm, a lifecycle or a selection. A refusal is exit 2
    (:func:`_refuse_invalid_policy_config`).

    Then, here: ``COOPERATION`` is written whenever the run has an arm, whatever the
    solver declared (so a contract-only solver gets planner rows too), at the place the
    solver's value used to be merged, so the built-ins' settings are byte-identical; the
    other ``stamp_settings`` are merged into ``orsim_settings``, which ships them to the
    agents at spawn (the STREAM_GEO pattern); and ``run_name_segments`` are appended
    after the legacy segments, idempotent by SEGMENT
    (``run_declaration.append_run_name_segments``).

    A top that declares planning ticks (refusals R3/R4 in the check; ``lifecycle_mode``
    is the run's resolved order-lifecycle mode, ``None`` skips R3) additionally gets
    :func:`_apply_planning_record`: run setting ``PLANNING``, a raised
    ``ORPHAN_TIMEOUT`` and the ``oracle:day`` segment.
    """
    from apps.container_logistics.analytics.manager import COOPERATION_SETTING

    if tree is None:
        return run_name
    try:
        plan = check_run_declaration(
            tree,
            settings_keys=scenario_manager.orsim_settings,
            profile=_first_assignment_profile(scenario_manager) if profile is None else profile,
            lifecycle_mode=lifecycle_mode,
        )
    except PolicyError as exc:
        _refuse_invalid_policy_config(exc)
    if plan.cooperation is not None:
        _set_run_setting(scenario_manager, COOPERATION_SETTING, plan.cooperation)
    scenario_manager.orsim_settings.update(plan.settings)
    run_name = append_run_name_segments(run_name, plan.run_name_segments)
    run_name = _apply_planning_record(scenario_manager, plan.planning, run_name)
    declaration = tree.declaration
    print(
        f"Run declaration: cross_company={len(declaration.cross_company)} pairs, "
        f"world_reads={','.join(sorted(tree.world_reads)) or '-'}"
    )
    return run_name


def _apply_planning_record(scenario_manager, planning, run_name: str | None) -> str | None:
    """Write a planning run's settings; return the run name with its ``oracle:day`` segment.

    ``planning`` is ``RunDeclarationPlan.planning`` (``None``: the top declares no planning
    and NOTHING is written -- every default run is unchanged). Otherwise: run setting
    ``PLANNING`` (the declaration plus ``oracle`` and ``axis``), ``ORPHAN_TIMEOUT`` raised
    to ``max(180, 2 * max_hold_seconds + 60)`` when the run's value is lower (a held step
    sends no scheduler message, plan D1c), and `` · oracle:day`` when the planner reads
    the day's orders ahead of their release.
    """
    if planning is None:
        return run_name
    _set_run_setting(scenario_manager, "PLANNING", axis_explicit_record(
        planning, scenario_manager.orsim_settings.get("REFERENCE_TIME")))
    raised = planning_orphan_timeout(planning, scenario_manager.orsim_settings.get("ORPHAN_TIMEOUT"))
    if raised is not None:
        _set_run_setting(scenario_manager, "ORPHAN_TIMEOUT", raised)
    segment = planning_run_name_segment(planning)
    if segment:
        run_name = append_run_name_segments(run_name, (segment,))
    print(
        f"Planning declared: {json.dumps(dict(planning), sort_keys=True)}"
        + (f" ORPHAN_TIMEOUT={raised}" if raised is not None else "")
    )
    return run_name


def _ship_planning_world(scenario_manager) -> None:
    """Ship ``UNASSIGNED_ORDER_MAX_WAIT_STEPS`` into the assignment profiles of a PLANNING run.

    Binds the first (post-override) assignment profile once to read the top's
    ``Declaration.planning``; only when it is declared does every assignment profile get
    ``scenario_overrides.PLANNING_MAX_WAIT_PROFILE_KEY`` (world data: the overdue sweep's
    deadline, critique M11). Runs BEFORE the run-start check and the stamp, so the
    stamped tree is bound on exactly the profile the agents receive. A default run is
    never patched. A bind error is a ``PolicyError`` for the caller's section.
    """
    import copy

    from apps.container_logistics.policies.dispatch.bind import bind_profile

    profile = _first_assignment_profile(scenario_manager)
    if not profile:
        return
    if declared_planning(bind_profile(copy.deepcopy(profile))) is None:
        return
    max_wait = scenario_manager.orsim_settings.get("UNASSIGNED_ORDER_MAX_WAIT_STEPS")
    collection = scenario_manager.get_agent_collection("assignment")
    n = ship_planning_world(collection, max_wait)
    print(f"Planning world: unassigned_order_max_wait_steps={max_wait} on {n} assignment profile(s)")
    # Review M1: the cadence the dispatch ticks will REALLY run at, i.e. after the
    # --assignment-cadence override that _apply_assignment_run_inputs_or_exit applies
    # later (it must be on the profile before the stamp binds it); that function's
    # cross-check (_check_planning_cadence) pins the two together. R8 on a bad value.
    cadence = assignment_cadence_from_env(os.environ)
    n = ship_planning_cadence(collection, cadence)
    shipped = sorted({(b.get("profile") or {}).get(PLANNING_CADENCE_PROFILE_KEY)
                      for b in collection.values() if isinstance(b, dict)})
    print(f"Planning world: {PLANNING_CADENCE_PROFILE_KEY}={','.join(map(str, shipped))} "
          f"on {n} assignment profile(s)" + (" (override)" if cadence is not None else ""))


def _check_planning_cadence(scenario_manager) -> None:
    """The shipped planning cadence must equal the applied ``steps_per_action`` (PolicyError if not).

    A no-op on every run that shipped none (no planning declared).
    """
    bad = planning_cadence_mismatches(scenario_manager.get_agent_collection("assignment"))
    if bad:
        raise PolicyError(
            f"internal: the planning cadence shipped to the assignment profile(s) is not the cadence "
            f"applied to their behavior(s) (agent, shipped, applied): {bad}"
        )


def _apply_assignment_run_inputs_or_exit(scenario_manager, run_name: str | None) -> str | None:
    """``ORSIM_ASSIGNMENT_CADENCE`` / ``ORSIM_MAX_ORDERS_PER_TICK`` onto the assignment behaviors.

    Refusal R8 (exit 2) for an out-of-range value. Patches each behavior's
    ``steps_per_action`` / ``profile.max_orders_per_tick`` in memory, stamps
    ``ASSIGNMENT_CADENCE = {steps, source}`` / ``MAX_ORDERS_PER_TICK = {orders, source}``
    only when overridden or authored in the spec (``source: "bundle"``), and appends
    `` · cadence:N`` / `` · orders:N`` only on override, and `` · hours:<form>@<digest>``
    whenever the profile compiled working hours (``run_declaration.working_hours_run_name_segment``). A different cadence moves the
    assignment agent's RNG draws: a new arm by design. Also stamps ``WORKING_HOURS``
    (the compiled per-haulier record) when the profile carries one.
    """
    try:
        cadence = assignment_cadence_from_env(os.environ)
        max_orders = max_orders_per_tick_from_env(os.environ)
    except PolicyError as exc:
        _refuse_invalid_policy_config(exc)
    collection = scenario_manager.get_agent_collection("assignment")
    effective = apply_assignment_run_overrides(collection, cadence=cadence, max_orders=max_orders)
    cadence_record, orders_record, segments = assignment_run_input_records(
        effective, scenario_manager.orsim_settings.get("GENERATION_SPEC"),
        cadence=cadence, max_orders=max_orders,
    )
    if cadence_record is not None:
        _set_run_setting(scenario_manager, "ASSIGNMENT_CADENCE", cadence_record)
        print(f"Assignment cadence: {cadence_record}")
    if orders_record is not None:
        _set_run_setting(scenario_manager, "MAX_ORDERS_PER_TICK", orders_record)
        print(f"Assignment max orders per tick: {orders_record}")
    _check_planning_cadence(scenario_manager)
    working_hours = _first_assignment_profile(scenario_manager).get("working_hours")
    if working_hours:
        segment = working_hours_run_name_segment(working_hours)
        working_hours = axis_explicit_record(working_hours, scenario_manager.orsim_settings.get("REFERENCE_TIME"))
        _set_run_setting(scenario_manager, "WORKING_HOURS", working_hours)
        print(f"Working hours: {json.dumps(working_hours, sort_keys=True)}")
        if segment:  # a comparability boundary: named, like cadence:/orders: (only with hours)
            segments = [*segments, segment]
    return append_run_name_segments(run_name, segments)


def _resolve_order_lifecycle_mode() -> str:
    """``service`` (default since 2026-07-29: one lifecycle agent, orders are data) or
    ``agents`` (the legacy per-order-agent topology, kept for A/B and rollback).

    Mirrors ORSIM_SOLVER / ORSIM_HEADLESS: a plain env var on the sim subprocess, carried to
    the celery-hosted agents as *data* (orsim_settings + truck behavior profiles), so
    switching modes never needs a celery restart. Unknown values fail soft to the default.
    The reader is ``scenario_overrides.order_lifecycle_from_env``, shared with the
    control-plane dry run (refusal R3 is decided on the same mode).
    """
    return order_lifecycle_from_env(os.environ)


def apply_order_lifecycle_service_mode(scenario_manager, run_name: str | None) -> str | None:
    """Switch this run to the order-lifecycle **service** topology. Returns the new run name.

    MUST be called before ``SimulationRuntime`` is constructed: ``ORSimRuntime.__init__``
    dispatches the bootstrap celery agents, so anything that has to be true *before* an agent
    can log in — above all the admin owner user (step 1) — has to happen here. All four
    carriers below are data-at-spawn (orsim_settings + behavior profiles), matching the
    HEADLESS / solver-override pattern, so switching modes needs no celery restart.
    """
    from apps.container_logistics.order_lifecycle import ORDER_LIFECYCLE_TOPIC_SUFFIX
    from apps.container_logistics.order_lifecycle.users import ensure_lifecycle_owner_user
    from apps.utils import time_to_str

    # (1) Behavior for the new role, synthesized when the compiled bundle predates it.
    lifecycle_collection = ensure_order_lifecycle_behavior(scenario_manager)
    # (2) Provision the owner user as ADMIN *now*, while nothing else can be racing us. See
    #     order_lifecycle/users.py for the two silent-corruption branches this closes.
    owner_email = ensure_lifecycle_owner_user(
        lifecycle_collection, time_to_str(scenario_manager.reference_time)
    )
    # (3) Service agents read orsim_settings (ships via scheduler['orsim_settings'], validated
    #     with allow_unknown); SimulationRuntime reads it to decide whether to pre-create
    #     order documents.
    _set_run_setting(scenario_manager, "ORDER_LIFECYCLE", "service")
    # (4) TruckTripManager has no orsim_settings, so re-target its ORDER_* events via the truck
    #     behavior profile (ships via spec['behavior'] at spawn — the stream_geo pattern). Done
    #     before the agent source is built so the patched behaviors ship.
    truck_collection = scenario_manager.get_agent_collection("truck")
    for behavior in truck_collection.values():
        behavior.setdefault("profile", {})["order_events_topic"] = ORDER_LIFECYCLE_TOPIC_SUFFIX
    # (5) Service is the default topology since 2026-07-29, so run names stay untagged here;
    #     main() tags the exceptional legacy runs with `· lifecycle:agents` instead.
    print(
        "Order lifecycle: service mode (orders are data — no order agents; "
        f"owner={owner_email}; "
        f"{len(truck_collection)} trucks re-targeted to '{ORDER_LIFECYCLE_TOPIC_SUFFIX}')"
    )
    return run_name


def _resolve_run_id() -> str:
    # Honor an externally-injected run id (set by the control layer at launch) so the
    # dashboard can attach to this exact run with no discovery race. Falls back to a
    # freshly generated id for direct CLI invocation.
    return os.environ.get("ORSIM_RUN_ID", "").strip() or new_run_id()


def _ensure_transport_lane_topics(run_id: str) -> None:
    """Create the ORSim lane topics for this run, if the Kafka transport is selected.

    Best effort by design: a failure here must not stop a run from launching.
    The bus creates the topics itself if it has to, so the only cost of a
    failure is that the first agent in each worker pays for the creation --
    which is the path this call exists to avoid, not a correctness requirement.
    """
    from apps.config import messenger_backend

    if messenger_backend.get("AGENT_TRANSPORT", "paho") == "paho":
        return
    try:
        from apps.transport.lane_admin import ensure_run_lane_topics

        counts = ensure_run_lane_topics(
            run_id,
            messenger_backend.get("KAFKA_BOOTSTRAP_SERVERS"),
            reply_partitions=int(messenger_backend.get("KAFKA_REPLY_PARTITIONS", 8)),
            app_partitions=int(messenger_backend.get("KAFKA_APP_PARTITIONS", 64)),
        )
        print(f"ORSim lane topics ready for {run_id}: {counts}")
    except Exception as exc:  # noqa: BLE001 - never block a launch on this
        logging.warning("could not pre-create ORSim lane topics for %s: %s", run_id, exc)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    from apps.utils import kafka_utils

    datahub_dir = get_datahub_dir()
    run_id = _resolve_run_id()
    scenario_name = _resolve_scenario_name()
    run_name = _resolve_run_name()
    domain = get_domain()

    print(f"Scenario: {scenario_name}  run_id: {run_id}")
    if run_name:
        print(f"run_name: {run_name}")
    print(f"Kafka broker: {kafka_utils.kafka_config['bootstrap_servers']}")

    run_status_topic = kafka_utils.bootstrap_kafka_for_run(run_id)

    # RabbitMQ -> Kafka migration, phase 2: create this run's three ORSim lane
    # topics next to the standard ones.  NO-OP with OPENRIDE_AGENT_TRANSPORT
    # unset (the default) -- ensure_run_lane_topics is not even called.
    #
    # Doing it HERE, in the launcher, is what keeps the celery workers off the
    # admin API entirely: a worker's bus then finds the topics already present
    # and answers from one list_topics call.  That matters because
    # AdminClient.create_topics() returns concurrent.futures whose completion
    # comes from librdkafka's native thread, which DEADLOCKS a monkey-patched
    # eventlet worker (see apps/transport/lane_admin.py).  The workers have a
    # future-free fallback, but not needing it is better.
    _ensure_transport_lane_topics(run_id)

    # run_id is passed so the Kafka half of the preflight can also confirm this
    # run's lane topics carry the partition counts the buses will compute from
    # (see preflight._kafka_serving). It is ignored on the paho default.
    assert_simulation_dependencies(run_id)

    scenario_manager = ScenarioManager(datahub_dir, scenario_name, domain=domain)

    # Refresh host-runtime performance knobs (timeouts, pacing floor, spawn/sleep
    # cadences, perf publishing) from the CURRENT code. These are properties of
    # this host's runtime, not of the simulated world — but they get baked into
    # scenario.json at compile time, so without this an old bundle pins stale
    # values (e.g. the former MIN_STEP_WALL_TIME_MS=200) forever. World-defining
    # settings (STEP_INTERVAL, SIMULATION_LENGTH_IN_STEPS, agent behaviors) are
    # not in the refreshed set, so results are unaffected.
    from apps.container_logistics.scenario.runtime_settings import (
        apply_long_run_orsim_settings,
    )

    scenario_manager.orsim_settings = apply_long_run_orsim_settings(
        scenario_manager.orsim_settings
    )

    # The order-lifecycle mode is resolved BEFORE the policy section (critique M8): the
    # run-declaration check refuses a day-foresight planner under 'agents' (R3), and that
    # refusal must come before anything is stamped. The mode is APPLIED further down,
    # where it always was.
    lifecycle_mode = _resolve_order_lifecycle_mode()

    # The policy section. Every refusal in it is a ``PolicyError``: the helpers exit 2
    # themselves, and the ``except`` below maps any ``PolicyError`` that still escapes
    # (a bind-time error raised outside them, final review L1) to the same exit 2 with
    # the message printed -- never a traceback and exit 1.
    try:
        # Per-run dispatch selection (ORSIM_POLICY_OVERRIDES, the legacy dispatch
        # variables of compat/legacy_env.py and ORSIM_SOLVER = the innermost per-scope
        # solver): patch the loaded assignment behaviors in place so this run uses the
        # requested policy/params on the identical (unchanged) scenario data — enabling
        # apples-to-apples comparisons on one scenario dir. ONE application point; the
        # legacy segments are appended at their old place below.
        dispatch_overrides, dispatch_labels = _apply_dispatch_overrides_or_exit(
            scenario_manager.get_agent_collection("assignment")
        )
        policies_overridden = bool(dispatch_overrides)
        dispatch_label = dispatch_labels["planner"]
        if dispatch_label:
            print(f"Assignment dispatch override: {dispatch_label}")
            # Surface the chosen policy (and overridden params) in run lists with zero
            # kafka/schema changes; format in dispatch_override_label.
            run_name = f"{run_name} · {dispatch_label}" if run_name else dispatch_label
        capture_dir = _apply_capture_env_or_exit(
            scenario_manager.get_agent_collection("assignment"), datahub_dir, run_id
        )
        if capture_dir:
            print(f"Dispatch context capture: {capture_dir}")

        # Per-run cooperation-structure override (collaboration plan §7): same mechanism as
        # the solver override — pure data patched onto the loaded assignment profile before
        # agents spawn; the on-disk bundle is never touched. Fail-loud: an undeclared id (or
        # a scenario with no cooperation block) exits 2 (message 26), and the patched world
        # keys go to the run-start check below, which refuses a patch the bound planner tree
        # never reads (message 25). It runs BEFORE that check so the check sees the patch.
        _refuse_removed_sharing_env_or_exit()
        applied_structure, patched_world_keys = _apply_cooperation_override_or_exit(
            scenario_manager.get_agent_collection("assignment")
        )
        if applied_structure:
            print(f"Cooperation structure override: {applied_structure}")
            run_name = (
                f"{run_name} · {applied_structure}" if run_name else applied_structure
            )

        # The legacy dispatch variables' run-name segments (compat/legacy_env.py), applied
        # above with the other dispatch inputs. They stay HERE so run names keep their
        # pre-v2 order (solver · structure · legacy segment) and stay byte-identical.
        for legacy_segment in dispatch_labels["legacy"]:
            print(f"Legacy dispatch override: {legacy_segment}")
            # Surface the segment in run lists (same trick as the solver override)
            # so runs are self-describing in the picker.
            run_name = (
                f"{run_name} · {legacy_segment}" if run_name else legacy_segment
            )

        # Fail loud before any agent spawns: an unknown dispatch policy would otherwise
        # only surface agent-side, in the celery journal.
        _assert_policies_resolvable_or_exit(
            scenario_manager.get_agent_collection("assignment"), patched_world_keys
        )
        # A planning top's world data (the overdue-sweep deadline), shipped only when the
        # bound top declares planning; after the run-start check (its messages stay first)
        # and before the stamp, so the stamped tree is bound on the profile the agents receive.
        _ship_planning_world(scenario_manager)
        bound_tree = _stamp_effective_policies(scenario_manager, policies_overridden)

        # The framework's COOPERATION record (the active arm of the world's cooperation
        # block, which the analytics agent builds planner-scope breakdown rows and gains
        # attribution from, carried by scheduler['orsim_settings'] like STREAM_GEO), then
        # the top solver's run declaration: its other stamp settings and its run-name
        # segments. A solver that runs a cooperation arm names it here on
        # EVERY run that has one, not just on the --structure path: Compare compares RUNS,
        # and the run name is what tells two arms of one scenario apart in the picker
        # (`lib/runNameMeta.ts::parseRunNameTags`).
        run_name = _apply_run_declaration(
            scenario_manager, bound_tree, run_name, _first_assignment_profile(scenario_manager),
            lifecycle_mode=lifecycle_mode,
        )
        # Per-run assignment inputs (--assignment-cadence / --max-orders-per-tick) and the
        # compiled WORKING_HOURS record: stamped and segmented only when they apply.
        run_name = _apply_assignment_run_inputs_or_exit(scenario_manager, run_name)
    except PolicyError as exc:
        _refuse_invalid_policy_config(exc)

    # Headless mode (CLI fast-path): suppress the per-tick truck-location Kafka publishing,
    # which exists purely to feed the LIVE frontend map. KPI/breakdown/run_status publishing
    # is untouched, so analysis is identical — just faster. The flag is carried in
    # orsim_settings, which ships to the Celery agents via scheduler['orsim_settings']
    # (validated with allow_unknown), so the truck/analytics agents see it with no celery
    # restart. See CLAUDE.md (headless CLI).
    #
    # NOTE (2026-08-05): this flag used to ALSO mute the analytics agent's trip_geo publish,
    # which is what feeds the always-on openride-trip-geo-sink → container_logistics_trip_geo.
    # That coupling meant headless runs ended up with NO durable route geometry at all and
    # replayed as straight lines forever. Route-geometry persistence now has its own switch,
    # PERSIST_ROUTE_GEO (below), which stays ON in headless. STREAM_GEO now means exactly one
    # thing: the visual-only, per-truck, per-step location stream.
    if os.environ.get("ORSIM_HEADLESS", "").strip().lower() in ("1", "true", "yes", "on"):
        # Run-level marker for "the live visual streams are off". Mirrored into each truck's
        # behavior profile below, which is what the publish hot path actually reads.
        _set_run_setting(scenario_manager, "STREAM_GEO", False)
        # TruckApp has no orsim_settings, so disable its per-step location emit via the
        # truck behavior profile (ships via spec['behavior'] at spawn — like the solver
        # override patches the assignment collection in place). Done before the agent source
        # is built (in the SimulationRuntime(...) call below) so the patched behaviors ship.
        truck_collection = scenario_manager.get_agent_collection("truck")
        for _behavior in truck_collection.values():
            _behavior.setdefault("profile", {})["stream_geo"] = False
        # The per-step pacing floor exists purely so the live dashboard has a
        # steady stream to render; headless runs have no viewer — run flat out.
        _set_run_setting(scenario_manager, "MIN_STEP_WALL_TIME_MS", 0)
        print(
            "Headless mode: truck-location streaming disabled "
            f"(STREAM_GEO=False, {len(truck_collection)} trucks muted, step pacing floor off)"
        )

    # Route-geometry persistence, independent of headless (see the note above). ON by default
    # for EVERY run so `container_logistics_trip_geo` — and therefore post-run replay/compare
    # geometry — is populated regardless of how the run was launched. Opt out per run with
    # ORSIM_PERSIST_ROUTE_GEO=0. Rides in orsim_settings, so it ships to the analytics agent
    # at spawn: no celery restart, per-run switchable.
    _persist_route_geo = os.environ.get("ORSIM_PERSIST_ROUTE_GEO", "").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    _set_run_setting(scenario_manager, "PERSIST_ROUTE_GEO", _persist_route_geo)
    print(f"Route geometry persistence: PERSIST_ROUTE_GEO={_persist_route_geo}")

    # Order-lifecycle mode (docs/order_lifecycle_service_plan.md). In ``service`` mode orders
    # stop being agents entirely: their documents are bulk-precreated, published on the demand
    # curve, and transitioned in batch by ONE order_lifecycle service agent. All four carriers
    # below are data-at-spawn (orsim_settings + behavior profiles), matching the HEADLESS /
    # solver-override pattern — so mode switching needs no celery restart. In ``agents`` mode
    # (the default) not one statement in this block runs and everything behaves as before.
    # ``lifecycle_mode`` was resolved before the policy section (R3, critique M8).
    if lifecycle_mode == "service":
        run_name = apply_order_lifecycle_service_mode(scenario_manager, run_name)
    else:
        # Legacy topology is the exception now — surface it in run lists (the solver-override
        # run-name pattern) so an A/B control is never mistaken for a default run.
        run_name = f"{run_name} · lifecycle:agents" if run_name else "lifecycle:agents"

    steps = scenario_manager.orsim_settings.get("SIMULATION_LENGTH_IN_STEPS", "?")
    print(f"Sim steps: {steps}")
    truck_n = len(scenario_manager.get_agent_collection("truck"))
    order_n = len(scenario_manager.get_agent_collection("order"))
    print(
        f"Scenario agents: trucks={truck_n} orders={order_n} "
        f"facilities={len(scenario_manager.get_agent_collection('facility'))}"
    )
    kafka_utils.push_run_status(
        run_status_topic,
        run_id,
        "RUNNING",
        scenario_slug=scenario_name,
        scenario_display_name=scenario_manager.get_scenario_display_name(),
        num_trucks=truck_n,
        num_orders=order_n,
        run_name=run_name,
    )
    kafka_utils.flush_producer(2)

    agent_config = get_agent_config(lifecycle_mode)
    parent_path = os.path.dirname(os.path.abspath(os.getcwd()))
    runtime_context = SimpleNamespace(datahub_dir=datahub_dir)
    sim = SimulationRuntime(
        run_id=run_id,
        scenario_manager=scenario_manager,
        datahub_dir=datahub_dir,
        domain=domain,
        agent_config=agent_config,
        statemachine_collection=get_statemachine_collection(),
        scheduler_config=build_scheduler_config(run_id, scenario_manager.orsim_settings),
        agent_source=build_agent_source(
            scenario_manager,
            agent_config,
            run_id,
            scenario_manager.reference_time,
            parent_path,
            runtime_context,
        ),
        termination_condition=build_termination_condition(scenario_manager.orsim_settings),
        progress_listener=kafka_progress_listener,
        run_name=run_name,
    )

    try:
        sim.run_simulation()
        print("Simulation completed!")
        kafka_utils.push_run_status(run_status_topic, run_id, "COMPLETED")
    except KeyboardInterrupt:
        print("Simulation interrupted by user (Ctrl+C)")
        raise
    except Exception as e:
        print(f"Simulation Error: {e}")
        kafka_utils.push_run_status(run_status_topic, run_id, "FAILED", msg=str(e))
        raise
    finally:
        kafka_utils.flush_producer(3)


if __name__ == "__main__":
    main()
