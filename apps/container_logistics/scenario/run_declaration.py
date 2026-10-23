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
   cooperation block (``analytics.manager.cooperation_record``). A top that also
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
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional

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
    "append_run_name_segments",
    "cooperation_record",
]

#: The run setting (``orsim_settings`` key -> ``run_config``) the cooperation record rides in.
COOPERATION_SETTING = "COOPERATION"
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
})

#: Every ``orsim_settings`` key framework code (orsim, the scheduler, the runtime, the
#: agents, the scenario loader) reads or writes, read with a default or not. A solver
#: stamping one would change how the FRAMEWORK runs (``ORPHAN_TIMEOUT``,
#: ``CONCURRENT_SCHEDULERS``, a step timeout ...) or be overwritten by it. Pinned by an
#: AST scan of the readers (``tests/test_run_declaration.py``).
FRAMEWORK_READ_SETTINGS = frozenset({
    "AGENT_LAUNCH_TIMEOUT",
    "ALLOW_POST_HORIZON_DRAIN",
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
})

#: The run-name segment separator (``" · "`` joins segments; readers split on it).
RUN_NAME_SEPARATOR = "·"

_LIFECYCLE_PREFIX = "lifecycle:"


class RunDeclarationError(PolicyError):
    """A top solver's ``stamp_settings`` / ``run_name_segments`` that no run may take."""


@dataclass(frozen=True)
class RunDeclarationPlan:
    """What the framework writes for an admissible declaration.

    ``cooperation`` is the framework's ``COOPERATION`` record (``None``: the run has no
    active arm, write nothing); ``settings`` the top's other stamp settings, in their
    declared order, to merge into the run's settings; ``run_name_segments`` the
    segments to append (:func:`append_run_name_segments`).
    """

    cooperation: Any = None
    settings: Mapping[str, Any] = field(default_factory=dict)
    run_name_segments: tuple[str, ...] = ()


def cooperation_record(world: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """The run's ``COOPERATION`` record, derived from WORLD data (final review M3).

    ``world`` is an assignment profile (or a run world over one) AFTER any per-run
    ``--structure`` override; only its ``cooperation`` key is read. Returns ``None``
    when it has no active structure (own-haulier only: no planner rows), else
    ``{structure_id, components, adjacency, pools}`` as plain JSON -- the record
    ``AnalyticsManager.set_cooperation`` reads for the planner-scope breakdown rows
    and gains attribution. The structure's own ``components`` / ``adjacency`` as
    authored (``[]`` / ``{}`` when absent) and the EFFECTIVE pools
    (``assignment.pools.pools_from_structure``).

    FRAMEWORK-owned: the sim main writes it (``_apply_run_declaration``) for every run
    with an arm, whatever the solver declared, so the analytics never depends on a
    solver choosing to stamp it. It equals the built-in solvers' own
    ``solver_lib.cooperation.stamp(resolve_arm(world))`` byte for byte
    (``tests/test_run_entry_ownership.py`` pins it on every structure of the compiled
    smoke bundle and a hand-written block), which keeps the built-ins' run settings identical.

    Moved verbatim from ``analytics/manager.py`` (which re-exports it), so the
    control-plane dry run can compute it without importing the analytics agent stack.
    """
    # Local imports: the assignment package is not on this module's import path.
    from apps.container_logistics.assignment import constraints as assign_constraints
    from apps.container_logistics.assignment import pools as assign_pools

    structure = assign_constraints.active_cooperation_structure(
        {"cooperation": (world or {}).get("cooperation")}
    )
    if structure is None:
        return None
    structure = structure if isinstance(structure, dict) else {}
    return json.loads(json.dumps({
        "structure_id": structure.get("id"),
        "components": structure.get("components") or [],
        "adjacency": structure.get("adjacency") or {},
        "pools": [{"id": p.id, "members": sorted(p.members)}
                  for p in assign_pools.pools_from_structure(structure)],
    }))


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
    if text in _framework_segments():
        return f"{segment!r} is a legacy dispatch segment the framework writes"
    if _looks_like_dispatch_label(text, policy_names):
        return (f"{segment!r} reads as a dispatch-override label (registered planner policy names): "
                "the run would be labelled with a selection it did not make")
    return None


def _planner_policy_names() -> frozenset[str]:
    from apps.container_logistics.policies.framework import list_policies

    return frozenset(spec.name for spec in list_policies("planner"))


def check_run_declaration(tree: Any, *, settings_keys: Iterable[str],
                          profile: Mapping[str, Any] | None) -> RunDeclarationPlan:
    """Check the top's run declaration (module docstring, rules 1-3); return the plan.

    ``tree`` is a bound planner tree (``dispatch.bind.BoundTree``: ``declaration``,
    ``top.name``); ``None`` returns an empty plan. ``settings_keys`` are the keys the
    run's settings hold when the declaration is merged; ``profile`` the assignment
    profile AFTER any per-run override (its ``cooperation`` block is the world's arm).
    Raises :class:`RunDeclarationError`. Messages for rules 1 and 2 are the sim main's
    former ones, unchanged.
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
    return RunDeclarationPlan(cooperation=record, settings=declared, run_name_segments=segments)


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

