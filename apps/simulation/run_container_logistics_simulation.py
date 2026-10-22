import logging
import os
import sys
from types import SimpleNamespace

from apps.container_logistics.scenario.scenario_config import default_scenario_name
from apps.container_logistics.scenario.scenario_manager import ScenarioManager
from apps.container_logistics.scenario.scenario_overrides import (
    apply_cooperation_override,
    current_policies_for_profile,
    dispatch_override_label,  # noqa: F401 - re-exported (the run-name label format lives there)
    run_dispatch_overrides,
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


def _resolve_scenario_name() -> str:
    return os.environ.get("ORSIM_SCENARIO", default_scenario_name()).strip()


def _resolve_run_name() -> str | None:
    return sanitize_run_name(os.environ.get("ORSIM_RUN_NAME"))


def _refuse_invalid_policy_config(error: PolicyError) -> None:
    """Print the policy configuration error and exit 2 before any agent spawns."""
    print(f"Policy configuration invalid; refusing to start the run: {error}")
    sys.exit(2)


def _assert_policies_resolvable_or_exit(assignment_collection) -> None:
    """Exit 2 (via :func:`_refuse_invalid_policy_config`) on an unresolvable dispatch policy."""
    try:
        assert_assignment_profiles_resolvable(assignment_collection)
    except PolicyError as exc:
        _refuse_invalid_policy_config(exc)


def _resolve_solver() -> str | None:
    # Per-run per-scope solver override (set by the control layer / frontend). Applied to
    # the loaded assignment behavior in-memory before agents spawn — no scenario regen, no
    # celery restart (the solver registry already lives in the worker). See CLAUDE.md.
    value = os.environ.get("ORSIM_SOLVER", "").strip()
    return value or None


def _resolve_policy_overrides() -> dict:
    """Per-run policy overrides: ``ORSIM_POLICY_OVERRIDES`` (JSON), ``{}`` when unset.

    ``ORSIM_SOLVER`` travels separately (the innermost per-scope solver, round-2 §3.3);
    :func:`_apply_dispatch_overrides_or_exit` combines the two. Raises ``PolicyError``
    for malformed JSON.
    """
    import json

    raw = os.environ.get("ORSIM_POLICY_OVERRIDES", "").strip()
    if not raw:
        return {}
    try:
        overrides = json.loads(raw)
    except ValueError as exc:
        raise PolicyError(f"ORSIM_POLICY_OVERRIDES is not valid JSON ({exc}): {raw!r}") from exc
    if not isinstance(overrides, dict):
        raise PolicyError(f"ORSIM_POLICY_OVERRIDES must be a JSON object of slot -> entry (got {raw!r}).")
    return overrides


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


def _stamp_effective_policies(scenario_manager, overridden: bool) -> None:
    """Stamp ``orsim_settings["POLICIES"]`` (-> ``run_config.meta.policies``) and print it.

    Built from the EFFECTIVE first assignment profile (post-override). Exits 2 on a
    ``PolicyError``.
    """
    from apps.container_logistics.policies.solver_lib.candidates import (  # local: agent-side module
        CandidateSettings,
    )
    import json

    from apps.container_logistics.policies.framework import policies_stamp

    collection = scenario_manager.get_agent_collection("assignment") or {}
    if not collection:
        return
    profile = next(iter(collection.values())).get("profile") or {}
    try:
        resolved, source = current_policies_for_profile(profile)
    except PolicyError as exc:
        _refuse_invalid_policy_config(exc)
    stamp = policies_stamp(
        resolved,
        candidate_settings={
            **CandidateSettings.from_profile(profile).to_dict(),
            # Where the max_trucks_per_scope cap applies: per haulier on a central
            # leaf's tick scope, per cooperation group / per company under the
            # decompositions (so a central arm can see more candidates; C3 deviation 6).
            "sample_scope": sample_scope_for(resolved["planner"].information_model),
        },
        # What AssignmentApp._solver_params will cost with (same ``or {}`` read).
        accounting_params=profile.get("solver_params") or {},
        source="override" if overridden else source,
    )
    scenario_manager.orsim_settings["POLICIES"] = stamp
    d = stamp["planner"]
    print(
        f"Policies effective: planner={d['label']} v{d['version']} timing={d['timing']} "
        f"information_model={d['information_model']} "
        f"params={json.dumps(d['params'], sort_keys=True)} "
        f"accounting_params={json.dumps(d['accounting_params'], sort_keys=True)}"
    )


def sample_scope_for(information_model: str | None) -> str | None:
    """The stamped ``candidate_settings.sample_scope`` of a dispatch information model.

    ``central`` decides the tick scope, whose cap applies per haulier
    (``context.TICK_SAMPLE_SCOPE``); ``per_group`` / ``per_company`` cap each
    decomposition scope as a whole. ``None`` for an undeclared model.
    """
    from apps.container_logistics.policies.dispatch.context import TICK_SAMPLE_SCOPE  # local: agent-side

    return {"central": TICK_SAMPLE_SCOPE, "per_group": "per_group",
            "per_company": "per_company"}.get(information_model)


def _resolve_order_lifecycle_mode() -> str:
    """``service`` (default since 2026-07-29: one lifecycle agent, orders are data) or
    ``agents`` (the legacy per-order-agent topology, kept for A/B and rollback).

    Mirrors ORSIM_SOLVER / ORSIM_HEADLESS: a plain env var on the sim subprocess, carried to
    the celery-hosted agents as *data* (orsim_settings + truck behavior profiles), so
    switching modes never needs a celery restart. Unknown values fail soft to the default.
    """
    value = os.environ.get("ORSIM_ORDER_LIFECYCLE", "").strip().lower()
    if not value:
        return "service"
    if value in ("agents", "service"):
        return value
    logging.warning(
        "Unknown ORSIM_ORDER_LIFECYCLE=%r; falling back to 'service'.", value
    )
    return "service"


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
    scenario_manager.orsim_settings["ORDER_LIFECYCLE"] = "service"
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

    # Per-run cooperation-structure / sharing-algorithm override (collaboration plan
    # §7): same mechanism as the solver override — pure data patched onto the loaded
    # assignment profile before agents spawn; the on-disk bundle is never touched.
    coop_structure = os.environ.get("ORSIM_COOP_STRUCTURE", "").strip() or None
    coop_sharing = os.environ.get("ORSIM_SHARING_POLICY", "").strip() or None
    if coop_structure or coop_sharing:
        applied_structure = apply_cooperation_override(
            scenario_manager.get_agent_collection("assignment"),
            structure=coop_structure,
            sharing=coop_sharing,
        )
        if applied_structure:
            print(f"Cooperation structure override: {applied_structure}")
            run_name = (
                f"{run_name} · {applied_structure}" if run_name else applied_structure
            )
        if coop_sharing:
            print(f"Sharing algorithm override requested: {coop_sharing}")

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
    _assert_policies_resolvable_or_exit(scenario_manager.get_agent_collection("assignment"))
    _stamp_effective_policies(scenario_manager, policies_overridden)

    # Ship the EFFECTIVE cooperation structure (post-override) to the analytics
    # agent via orsim_settings (the STREAM_GEO pattern: validated allow_unknown,
    # carried by scheduler['orsim_settings']) so planner-scope breakdown rows and
    # gains attribution know the active structure without any celery coupling.
    try:
        _assign_coll = scenario_manager.get_agent_collection("assignment") or {}
        _profile = next(iter(_assign_coll.values()), {}).get("profile") or {}
        _coop = _profile.get("cooperation") or {}
        _structures = {s.get("id"): s for s in _coop.get("structures", []) if isinstance(s, dict)}
        _active = _structures.get(_coop.get("active"))
        if _active is not None:
            from apps.container_logistics.assignment.pooled_planner import (
                effective_cooperation_stamp,
            )

            # EFFECTIVE state, not the compiled keys (plan §13.4 FIX-7 / F7): no
            # shipped bundle carries a 'pools' key, yet the runtime derives a full
            # market from 'edges', so the old stamp recorded the OPPOSITE of what
            # ran -- on exactly the workflow §8 promises ("run old bundles pooled
            # without recompiling"). One helper, shared with the runtime, so the
            # stamp cannot drift from what executes.
            _stamp = effective_cooperation_stamp(_profile, _active)
            scenario_manager.orsim_settings["COOPERATION"] = _stamp

            # Name the ARM in the run name, on EVERY run that has one — not just on the
            # override path.
            #
            # Compare compares RUNS: "this scenario with structure A" and "this scenario with
            # structure B" are two runs, and the run name is what tells them apart in the
            # picker and in `lib/runNameMeta.ts::parseRunNameTags`. Only
            # `apply_cooperation_override` appended a `· <structure>` segment, so a launch on
            # the scenario's DEFAULT arm produced an unlabelled run — indistinguishable in the
            # picker from the other arm of the same scenario.
            #
            # Taken from the stamp, so it is the EFFECTIVE structure (post-override) and can
            # never disagree with what actually ran. Idempotent by SEGMENT (not substring, so
            # `chain` never matches `chain2`), which is what keeps the override path — which
            # already appended it above — from producing `… · chain · chain`.
            _sid = _stamp.get("structure_id")
            if _sid:
                _segments = [seg.strip() for seg in (run_name or "").split("·")]
                if _sid not in _segments:
                    run_name = f"{run_name} · {_sid}" if run_name else str(_sid)

            _mkt = _stamp["market"]
            print(
                f"Cooperation effective: topology={_stamp['topology']} "
                f"pools={len(_stamp['pools'])} "
                f"market={_mkt['offer']['type']}/{_mkt['claim']['type']}/"
                f"{_mkt['arbitration']['type']} "
                f"max_rounds_backstop={_mkt['max_rounds']}"
            )
            print(f"Cooperation active structure: {_active.get('id')}")
    except Exception:
        logging.exception("Failed to stamp COOPERATION into orsim_settings (non-fatal)")

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
        scenario_manager.orsim_settings["STREAM_GEO"] = False
        # TruckApp has no orsim_settings, so disable its per-step location emit via the
        # truck behavior profile (ships via spec['behavior'] at spawn — like the solver
        # override patches the assignment collection in place). Done before the agent source
        # is built (in the SimulationRuntime(...) call below) so the patched behaviors ship.
        truck_collection = scenario_manager.get_agent_collection("truck")
        for _behavior in truck_collection.values():
            _behavior.setdefault("profile", {})["stream_geo"] = False
        # The per-step pacing floor exists purely so the live dashboard has a
        # steady stream to render; headless runs have no viewer — run flat out.
        scenario_manager.orsim_settings["MIN_STEP_WALL_TIME_MS"] = 0
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
    scenario_manager.orsim_settings["PERSIST_ROUTE_GEO"] = _persist_route_geo
    print(f"Route geometry persistence: PERSIST_ROUTE_GEO={_persist_route_geo}")

    # Order-lifecycle mode (docs/order_lifecycle_service_plan.md). In ``service`` mode orders
    # stop being agents entirely: their documents are bulk-precreated, published on the demand
    # curve, and transitioned in batch by ONE order_lifecycle service agent. All four carriers
    # below are data-at-spawn (orsim_settings + behavior profiles), matching the HEADLESS /
    # solver-override pattern — so mode switching needs no celery restart. In ``agents`` mode
    # (the default) not one statement in this block runs and everything behaves as before.
    lifecycle_mode = _resolve_order_lifecycle_mode()
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
