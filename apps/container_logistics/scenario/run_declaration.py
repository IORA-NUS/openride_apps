"""The run-declaration check: what a TOP solver's ``Declaration`` may add to a run. Pure.

Single responsibility: given the bound planner tree, the keys the run's settings
already hold and the (post-override) assignment profile, decide whether the top's
``stamp_settings`` and ``run_name_segments`` are admissible, and return what the
framework writes for them (:class:`RunDeclarationPlan`). It reads no file, no
environment and no global state, writes nothing and never exits: each entry path maps
:class:`RunDeclarationError` to its own refusal --

- the sim main (``run_container_logistics_simulation._apply_run_declaration``): exit 2;
- the control-plane dry run (``openride_control.policies``, ``validate-run-policies``
  and ``run-preflight``): ``{ok: false, message}``;
- scenario compile (``spec_compile.compile_spec_to_bundle``): the bundle is not written.

So a solver whose declaration every run would refuse is refused at all three, not only
by the spawned sim out of sight (round-2 review M-A).

The rules, in the order they are checked:

1. ``COOPERATION`` is framework-owned: derived from the active arm of the profile's
   cooperation block (``compat.cooperation_record``). A top that also
   declares it must declare exactly that record.
2. No other ``stamp_settings`` key may be one the run's settings already hold, or one
   in :func:`reserved_run_settings` -- every setting the framework WRITES
   (:data:`FRAMEWORK_RUN_SETTINGS`) or READS (:data:`FRAMEWORK_READ_SETTINGS`, the
   orsim settings schema, the runtime knobs ``runtime_settings`` refreshes), whether or
   not this run sets it (round-2 review M-B: ``ORPHAN_TIMEOUT``,
   ``CONCURRENT_SCHEDULERS``). A solver may add settings, never replace or pre-empt one.
3. Every run-name segment is a non-empty string without the separator ``·``, and is
   not one the FRAMEWORK writes or a reader parses as something else (round-2 FE
   finding): a declared cooperation structure id other than the run's ACTIVE arm (it
   would label the run as that arm), an order-lifecycle tag (``lifecycle:...``), a
   legacy dispatch segment, or a dispatch-override label (a registered planner policy
   name, ``Name(k=v)`` or an ``A/B`` chain of those). The active arm's own id stays
   allowed: it is true, and the built-ins declare it.

``FRAMEWORK_READ_SETTINGS`` is a static list, pinned against the code that reads the
settings by ``tests/test_run_declaration.py`` (an AST scan of orsim and ``apps/``), so
a new framework reader with a key missing here fails that test instead of drifting.

A top that declares PLANNING ticks (``Declaration.planning``: ``{at_day_start,
every_steps, foresight, max_hold_seconds}``; the shape itself is the bind contract's
check) is checked here too, after rules 1-3, at every entry path:

4. **R4** -- ``max_hold_seconds`` above :data:`PLANNING_HOLD_CAP_SECONDS` is refused (a
   held step stops every agent's clock; see the constant).
5. **R3** -- ``foresight == "day"`` reads orders before the simulation releases them,
   which only the order-lifecycle ``service`` mode can serve (orders pre-created with
   their request step). Refused when ``lifecycle_mode == "agents"``. Compile passes
   ``lifecycle_mode=None`` (the mode is a per-run input, so compile does not refuse);
   the dry run passes the mode the launch environment selects, the sim main the mode
   it resolved -- BEFORE this check, so nothing is stamped before a refusal.

The admissible declaration comes back as ``RunDeclarationPlan.planning``: the record the
sim main stamps as run setting ``PLANNING`` (the declaration plus ``oracle`` and
``axis: "elapsed"``); :func:`planning_run_name_segment` gives its ``oracle:day``
run-name segment and :func:`planning_orphan_timeout` the raised ``ORPHAN_TIMEOUT``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from apps.container_logistics.policies.compat.cooperation_record import (
    COOPERATION_SETTING,
    cooperation_record,
)
from apps.container_logistics.policies.framework import PolicyError

__all__ = [
    "COOPERATION_SETTING",
    "FRAMEWORK_RUN_SETTINGS",
    "FRAMEWORK_READ_SETTINGS",
    "RUN_NAME_SEPARATOR",
    "RunDeclarationError",
    "RunDeclarationPlan",
    "reserved_run_settings",
    "check_run_declaration",
    "declared_planning",
    "planning_run_name_segment",
    "planning_orphan_timeout",
    "PLANNING_HOLD_CAP_SECONDS",
    "PLANNING_AXIS",
    "PLANNING_CADENCE_PROFILE_KEY",
    "ship_planning_cadence",
    "planning_cadence_mismatches",
    "axis_explicit_record",
    "FRAMEWORK_SEGMENT_PREFIXES",
    "append_run_name_segments",
    "cooperation_record",
]

_COOPERATION_SETTING = COOPERATION_SETTING

#: EVERY run setting the sim main itself writes into ``orsim_settings`` (only through
#: ``run_container_logistics_simulation._set_run_setting``, which refuses a key missing
#: from it; ``tests/test_run_entry_ownership.py`` pins that this is exactly the set it
#: writes). ``COOPERATION`` is framework-owned too (final review M3).
FRAMEWORK_RUN_SETTINGS = frozenset({
    "POLICIES",
    "COOPERATION",
    "STREAM_GEO",
    "MIN_STEP_WALL_TIME_MS",
    "PERSIST_ROUTE_GEO",
    "ORDER_LIFECYCLE",
    # Day-plan framework additions (NetworkFlowDay build, F1/F4/F4b/F5): written only on
    # runs that declare planning / override the cadence / carry working hours, so a
    # default run's settings are unchanged.
    "PLANNING",
    "ORPHAN_TIMEOUT",
    "ASSIGNMENT_CADENCE",
    "MAX_ORDERS_PER_TICK",
    "WORKING_HOURS",
})

#: Every ``orsim_settings`` key framework code (orsim, the scheduler, the runtime, the
#: agents, the scenario loader) reads or writes, read with a default or not. A solver
#: stamping one would change how the FRAMEWORK runs (``ORPHAN_TIMEOUT``,
#: ``CONCURRENT_SCHEDULERS``, a step timeout ...) or be overwritten by it. Pinned by an
#: AST scan of the readers (``tests/test_run_declaration.py``).
FRAMEWORK_READ_SETTINGS = frozenset({
    "AGENT_LAUNCH_TIMEOUT",
    "ALLOW_POST_HORIZON_DRAIN",
    "ASSIGNMENT_CADENCE",
    "BEHAVIOR_REVISION",
    "CONCURRENT_SCHEDULERS",
    "COOPERATION",
    "DOMAIN",
    "FACILITY_RULES",
    "GENERATION_SPEC",
    "HEARTBEAT_INTERVAL",
    "KAFKA_HEARTBEAT_INTERVAL_STEPS",
    "MIN_STEP_WALL_TIME_MS",
    "ORDER_LIFECYCLE",
    "ORDER_SPAWN_MAX_PER_STEP",
    "ORPHAN_TIMEOUT",
    "OSRM_ROUTE_CACHE_MAX_ENTRIES",
    "PERF_DETAIL_INTERVAL_STEPS",
    "PERF_DETAIL_ON_NEW_MAX",
    "PERF_INCLUDE_PROCESS_METRICS",
    "PERF_KAFKA_FLUSH_EVERY_STEPS",
    "PERF_KAFKA_INTERVAL_STEPS",
    "PERF_SLOW_AGENT_TOP_N",
    "PERF_TICK_EVERY_STEP",
    "PERSIST_ROUTE_GEO",
    "PLANNING",
    "POLICIES",
    "POST_HORIZON_DRAIN_MAX_STEPS",
    "POST_HORIZON_GRACE_STEPS",
    "REBATE",
    "REFERENCE_TIME",
    "RUNTIME_PROGRESS_LOG_INTERVAL_STEPS",
    "RUNTIME_STATUS_UPDATE_INTERVAL_STEPS",
    "RUNTIME_STORE_STEP_METRICS",
    "SIMULATION_DAYS",
    "SIMULATION_LENGTH_IN_STEPS",
    "STEP_INTERVAL",
    "STEP_SETTLE_SECONDS",
    "STEP_TIMEOUT",
    "STEP_TIMEOUT_ABORT_CONSECUTIVE",
    "STEP_TIMEOUT_TOLERANCE",
    "STREAM_GEO",
    "UNASSIGNED_ORDER_MAX_WAIT_STEPS",
    "UNASSIGNED_ORDER_SLEEP_STEPS",
    "USE_OSRM_AT_ASSIGNMENT",
    "WORKING_HOURS",
})

#: The run-name segment separator (``" · "`` joins segments; readers split on it).
RUN_NAME_SEPARATOR = "·"

_LIFECYCLE_PREFIX = "lifecycle:"

#: Run-name segment prefixes the FRAMEWORK writes for the day-plan run inputs
#: (``oracle:day`` for a planning declaration with day foresight, ``cadence:N`` for
#: ``--assignment-cadence``, ``orders:N`` for ``--max-orders-per-tick``). A solver
#: segment with one of them would label the run with an input it did not get.
FRAMEWORK_SEGMENT_PREFIXES = ("oracle:", "cadence:", "orders:")

#: The largest ``max_hold_seconds`` a planning declaration may ask for (refusal R4).
#: A held step stops every agent's clock, and a run's whole wall time must stay well
#: inside the Celery/Redis broker's 6 h ``visibility_timeout`` (past it every agent task
#: is redelivered as a duplicate agent): the cap would be ``min(600, (6 h - expected
#: wall) / solves)``; with ~140-170 planning solves per 7-day run that is a few minutes,
#: kept as this constant (plan N1-N3 D2, F1 item 10).
PLANNING_HOLD_CAP_SECONDS = 300

#: The time axis day boundaries and working hours are measured on: seconds ELAPSED since
#: ``REFERENCE_TIME`` (the axis the demand curve is authored on; plan D5).
PLANNING_AXIS = "elapsed"

#: The assignment-profile key the sim main ships the run's EFFECTIVE dispatch cadence
#: under -- the ``steps_per_action`` the assignment agent actually gates its dispatch
#: ticks on, after any ``--assignment-cadence`` / ``ORSIM_ASSIGNMENT_CADENCE`` override
#: (review M1: a plan built for a 6-step cadence on a run that dispatches every 28 steps
#: is a different arm than stamped). Like ``unassigned_order_max_wait_steps``: world data
#: (``world.get(...)``), written ONLY when the bound top declares planning, absent on
#: every other run, so default profiles and bundles are unchanged.
PLANNING_CADENCE_PROFILE_KEY = "assignment_cadence_steps"

#: What the assignment agent gates on when a behavior carries no ``steps_per_action``
#: (``AssignmentAgent.step``: ``behavior.get("steps_per_action", 1)``).
_AGENT_DEFAULT_STEPS_PER_ACTION = 1


def _applied_cadence(behavior: Mapping[str, Any], cadence_override: Any = None) -> int:
    if cadence_override is not None:
        return int(cadence_override)
    return int(behavior.get("steps_per_action", _AGENT_DEFAULT_STEPS_PER_ACTION))


def ship_planning_cadence(assignment_collection: Any, cadence_override: Any = None) -> int:
    """Write :data:`PLANNING_CADENCE_PROFILE_KEY` onto every assignment profile (in place).

    The value is the cadence each behavior WILL run at: ``cadence_override`` (the per-run
    override, which ``apply_assignment_run_overrides`` writes onto every behavior) or the
    behavior's own ``steps_per_action``. Called by the sim main only when the bound top
    declares planning. Returns the number of profiles written.
    """
    n = 0
    for behavior in (assignment_collection or {}).values():
        if isinstance(behavior, dict):
            behavior.setdefault("profile", {})[PLANNING_CADENCE_PROFILE_KEY] = _applied_cadence(
                behavior, cadence_override)
            n += 1
    return n


def planning_cadence_mismatches(assignment_collection: Any) -> list[tuple[str, Any, int]]:
    """``(agent_id, shipped, applied)`` for every profile whose shipped cadence is not the applied one.

    Only profiles that CARRY the key are checked (a default run carries none). Run after the
    per-run overrides are applied: the shipped value must be what the agent gates on.
    """
    out = []
    for agent_id, behavior in (assignment_collection or {}).items():
        if not isinstance(behavior, dict):
            continue
        profile = behavior.get("profile") or {}
        if PLANNING_CADENCE_PROFILE_KEY not in profile:
            continue
        shipped, applied = profile[PLANNING_CADENCE_PROFILE_KEY], _applied_cadence(behavior)
        if shipped != applied:
            out.append((str(agent_id), shipped, applied))
    return out


def axis_explicit_record(record: Mapping[str, Any], reference_time: Any = None) -> dict[str, Any]:
    """A ``PLANNING`` / ``WORKING_HOURS`` stamp with its time axis spelled out (review minor 9).

    ``axis`` is ``"elapsed"`` (seconds since ``REFERENCE_TIME``, NOT the sim wall clock:
    06:00-20:00 elapsed is 14:00-04:00 on the clock face when ``REFERENCE_TIME`` is
    08:00); ``axis_origin`` names that reference time when the run has one. A copy: the
    profile record the agents read is not touched.
    """
    out = {str(k): v for k, v in record.items()}
    out.setdefault("axis", PLANNING_AXIS)
    if reference_time is not None and str(reference_time).strip():
        out.setdefault("axis_origin", str(reference_time))
    return out


#: ``ORPHAN_TIMEOUT`` (s) the agents use when the run sets none (``openride_agent.py``).
DEFAULT_ORPHAN_TIMEOUT = 180

#: The order-lifecycle mode whose orders exist only from their request step (R3).
_AGENTS_LIFECYCLE = "agents"


class RunDeclarationError(PolicyError):
    """A top solver's ``stamp_settings`` / ``run_name_segments`` that no run may take."""


@dataclass(frozen=True)
class RunDeclarationPlan:
    """What the framework writes for an admissible declaration.

    ``cooperation`` is the framework's ``COOPERATION`` record (``None``: the run has no
    active arm, write nothing); ``settings`` the top's other stamp settings, in their
    declared order, to merge into the run's settings; ``run_name_segments`` the
    segments to append (:func:`append_run_name_segments`); ``planning`` the ``PLANNING``
    run-setting record when the top declares planning ticks (``None``: it does not, and
    nothing planning-related is written).
    """

    cooperation: Any = None
    settings: Mapping[str, Any] = field(default_factory=dict)
    run_name_segments: tuple[str, ...] = ()
    planning: Mapping[str, Any] | None = None


def reserved_run_settings() -> frozenset[str]:
    """Every setting key a solver's ``stamp_settings`` may not name (rule 2).

    :data:`FRAMEWORK_RUN_SETTINGS` | :data:`FRAMEWORK_READ_SETTINGS` (which includes
    every key of orsim's settings schema, pinned by a test: importing orsim here would
    cost the dry run ~100 ms) | the keys ``runtime_settings.apply_long_run_orsim_settings``
    writes at run start (read from that function at call time, so a new knob there is
    reserved without an edit here).
    """
    from apps.container_logistics.scenario.runtime_settings import apply_long_run_orsim_settings

    return FRAMEWORK_RUN_SETTINGS | FRAMEWORK_READ_SETTINGS | frozenset(apply_long_run_orsim_settings({}))


def _declared_structure_ids(profile: Mapping[str, Any] | None) -> list[str]:
    """The ids of every structure the profile's cooperation block declares (opaque world data)."""
    block = (profile or {}).get("cooperation") if isinstance(profile, Mapping) else None
    entries = block.get("structures") if isinstance(block, Mapping) else None
    return [str(s["id"]) for s in (entries or []) if isinstance(s, Mapping) and s.get("id")]


def _framework_segments() -> frozenset[str]:
    """The fixed run-name segments the framework's compat layer writes (the legacy dispatch spellings)."""
    from apps.container_logistics.policies.compat import legacy as _legacy

    return frozenset(_legacy.LEGACY_TOPOLOGIES) | frozenset(_legacy.LEGACY_TOPOLOGIES.values())


def _split_label(segment: str) -> list[str] | None:
    """``A(k=v)/B`` -> ``["A", "B"]`` (the node names of a dispatch label); ``None`` if unbalanced."""
    names, depth, start, in_string = [], 0, 0, False
    for i, char in enumerate(segment + "/"):
        if in_string:
            in_string = char != '"' or segment[i - 1] == "\\"
            continue
        if char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return None
        elif char == "/" and depth == 0:
            names.append(segment[start:i].split("(", 1)[0].strip())
            start = i + 1
    return names if depth == 0 else None


def _looks_like_dispatch_label(segment: str, policy_names: frozenset[str]) -> bool:
    names = _split_label(segment)
    return bool(names) and all(name in policy_names for name in names)


def _segment_problem(segment: Any, *, declared: list[str], active: str | None,
                     policy_names: frozenset[str]) -> str | None:
    if not isinstance(segment, str) or type(segment) is not str or not segment.strip():
        return f"{segment!r} is not a non-empty string"
    text = segment.strip()
    if RUN_NAME_SEPARATOR in text or text != segment:
        return (f"{segment!r} contains the run-name separator {RUN_NAME_SEPARATOR!r} or surrounding "
                "whitespace (it would forge or blur a segment boundary)")
    if text in declared and text != active:
        return (f"{segment!r} is a cooperation structure this scenario declares, but the run's active "
                f"arm is {active!r}: the run would be labelled as the wrong arm")
    if text.startswith(_LIFECYCLE_PREFIX):
        return f"{segment!r} is the framework's order-lifecycle tag ({_LIFECYCLE_PREFIX}...)"
    prefix = next((p for p in FRAMEWORK_SEGMENT_PREFIXES if text.startswith(p)), None)
    if prefix is not None:
        return f"{segment!r} is a framework run-input tag ({prefix}...)"
    if text in _framework_segments():
        return f"{segment!r} is a legacy dispatch segment the framework writes"
    if _looks_like_dispatch_label(text, policy_names):
        return (f"{segment!r} reads as a dispatch-override label (registered planner policy names): "
                "the run would be labelled with a selection it did not make")
    return None


def _planner_policy_names() -> frozenset[str]:
    from apps.container_logistics.policies.framework import list_policies

    return frozenset(spec.name for spec in list_policies("planner"))


def declared_planning(tree: Any) -> Mapping[str, Any] | None:
    """The top's ``Declaration.planning`` mapping, or ``None`` (no tree / not declared).

    Read with ``getattr``: a ``Declaration`` without the field (older contract) declares
    no planning.
    """
    declaration = getattr(tree, "declaration", None) if tree is not None else None
    planning = getattr(declaration, "planning", None)
    return planning if isinstance(planning, Mapping) and planning else None


def _planning_record(name: str, planning: Mapping[str, Any], lifecycle_mode: str | None) -> dict[str, Any]:
    """R4, then R3; the ``PLANNING`` run-setting record (declaration + ``oracle`` + ``axis``)."""
    hold = planning.get("max_hold_seconds")
    if isinstance(hold, bool) or not isinstance(hold, int) or hold > PLANNING_HOLD_CAP_SECONDS:
        raise RunDeclarationError(
            f"planner policy {name} declares max_hold_seconds {hold!r}, above the planning hold cap "
            f"{PLANNING_HOLD_CAP_SECONDS} s (a held step stops every agent's clock; runs must stay well "
            "inside the 6 h broker visibility timeout). Lower the solver's time limit."
        )
    foresight = planning.get("foresight")
    if foresight == "day" and lifecycle_mode == _AGENTS_LIFECYCLE:
        raise RunDeclarationError(
            f"planner policy {name} declares planning foresight 'day', which reads orders before the "
            "simulation releases them; that needs ORDER_LIFECYCLE=service (orders pre-created with their "
            "request step). This run uses 'agents' (ORSIM_ORDER_LIFECYCLE=agents): drop that override or "
            "select an online planner."
        )
    record = {str(k): v for k, v in planning.items()}
    record["oracle"] = foresight == "day"
    record["axis"] = PLANNING_AXIS
    return record


def planning_run_name_segment(planning: Mapping[str, Any] | None) -> str | None:
    """``"oracle:day"`` for a ``PLANNING`` record with day foresight, else ``None``."""
    if isinstance(planning, Mapping) and planning.get("oracle"):
        return "oracle:day"
    return None


def planning_orphan_timeout(planning: Mapping[str, Any] | None, current: Any) -> int | None:
    """The raised ``ORPHAN_TIMEOUT`` for a planning run, or ``None`` (no raise needed).

    ``max(current or 180, 2 * max_hold_seconds + 60)``: an agent that hears no scheduler
    message for ``ORPHAN_TIMEOUT`` shuts itself down, and a held step sends none (plan D1c).
    ``None`` when there is no planning record or the current value already suffices.
    """
    if not isinstance(planning, Mapping):
        return None
    hold = planning.get("max_hold_seconds")
    if isinstance(hold, bool) or not isinstance(hold, int):
        return None
    try:
        base = int(current) if current is not None else DEFAULT_ORPHAN_TIMEOUT
    except (TypeError, ValueError):
        base = DEFAULT_ORPHAN_TIMEOUT
    wanted = max(DEFAULT_ORPHAN_TIMEOUT, 2 * hold + 60)
    return wanted if wanted > base else None


def check_run_declaration(tree: Any, *, settings_keys: Iterable[str],
                          profile: Mapping[str, Any] | None,
                          lifecycle_mode: str | None = None) -> RunDeclarationPlan:
    """Check the top's run declaration (module docstring, rules 1-5); return the plan.

    ``tree`` is a bound planner tree (``dispatch.bind.BoundTree``: ``declaration``,
    ``top.name``); ``None`` returns an empty plan. ``settings_keys`` are the keys the
    run's settings hold when the declaration is merged; ``profile`` the assignment
    profile AFTER any per-run override (its ``cooperation`` block is the world's arm).
    ``lifecycle_mode`` is the run's order-lifecycle mode (``"service"`` / ``"agents"``),
    ``None`` at compile (no R3). Raises :class:`RunDeclarationError`. Messages for rules
    1 and 2 are the sim main's former ones, unchanged.
    """
    if tree is None:
        return RunDeclarationPlan()
    declaration = tree.declaration
    name = tree.top.name
    record = cooperation_record(profile)
    declared = dict(declaration.stamp_settings)
    if _COOPERATION_SETTING in declared and declared[_COOPERATION_SETTING] != record:
        raise RunDeclarationError(
            f"planner policy {name} declared run setting {_COOPERATION_SETTING}="
            f"{declared[_COOPERATION_SETTING]!r}, but the run's active cooperation arm is "
            f"{record!r}. {_COOPERATION_SETTING} is framework-owned (derived from the world's "
            "cooperation block); drop it from stamp_settings or declare exactly that record."
        )
    declared.pop(_COOPERATION_SETTING, None)
    held = frozenset(settings_keys) | reserved_run_settings()
    clash = sorted(k for k in declared if k in held)
    if clash:
        raise RunDeclarationError(
            f"planner policy {name} declared run setting(s) {', '.join(clash)} that the run "
            "already sets or the framework owns; a solver's stamp_settings may add settings, never replace one."
        )
    segments = tuple(declaration.run_name_segments)
    if segments:
        active = record.get("structure_id") if isinstance(record, dict) else None
        ids, names = _declared_structure_ids(profile), _planner_policy_names()
        problems = [p for p in (_segment_problem(s, declared=ids, active=active, policy_names=names)
                                for s in segments) if p]
        if problems:
            raise RunDeclarationError(
                f"planner policy {name} declared run-name segment(s) the framework refuses: "
                f"{'; '.join(problems)}. A solver's run_name_segments may add its own tags, never "
                "one the framework writes or a reader parses as a structure, a lifecycle or a selection."
            )
    planning = declared_planning(tree)
    planning_record = None if planning is None else _planning_record(name, planning, lifecycle_mode)
    return RunDeclarationPlan(cooperation=record, settings=declared, run_name_segments=segments,
                              planning=planning_record)


def append_run_name_segments(run_name: str | None, segments: Iterable[str]) -> str | None:
    """Append each segment after ``run_name`` (``" · "``), idempotent by SEGMENT.

    By segment, not substring, so ``chain`` never matches ``chain2``: the
    ``--structure`` path already appended its ``· <structure>`` and must not get
    ``… · chain · chain``. The sim main's former loop, unchanged.
    """
    for segment in segments:
        present = [seg.strip() for seg in (run_name or "").split(RUN_NAME_SEPARATOR)]
        if segment not in present:
            run_name = f"{run_name} · {segment}" if run_name else str(segment)
    return run_name

