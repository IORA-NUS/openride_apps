"""The policy contract battery: ``run_battery(slot, name) -> list[CheckResult]``.

Single responsibility: decide, for one registered dispatch policy, whether it honours
the dispatch contract on WHOLE TICKS (round 2, ``docs/plans/policy_slots_round2_whole_tick.md``
§5 C5; plan ``docs/plans/policy_slots_p0_p3.md`` §4 P3.2). Every decision is one tick
(``fixtures.Fixture.context`` -> ``DispatchRunner.tick``) through
``DispatchRunner.run``, so what passes here is what the sim executes, leaf or
decomposition alike. The ids for the DEFAULT arm are :data:`DISPATCH_CHECKS`, in order:

- ``structure`` -- on every fixture: the tick result passes ``invariants.check_result``
  (on the whole tick its feasibility is ownership-or-declared AND physical, so a
  cross-company pair the top did not declare fails ``only_allowed_pairs``). Each
  verdict is derived from ``check_result``'s own messages
  (:func:`classify_contract_problem`); ``Result.from_pairs``' "truck twice" violation
  is ``no_truck_twice``.
- ``robustness`` -- one per fixture: ``decide`` does not raise anything but a
  contract violation (reported once, under ``structure``).
- ``determinism`` -- ``reproducible``: two fresh instances on the same tick give the
  same plan; ``order_independent``: 20 shuffles of trucks and orders give one plan;
  ``no_module_rng``: ``decide`` leaves the ``random`` module's state untouched.
  ``order_independent`` is SKIPPED (``CheckResult.skipped``, status ``SKIP``, ``ok``
  true) when ANY node of the arm's selection declares ``order_sensitive``: the
  declarations compose. ``no_module_rng`` under a ``module_rng`` declaration is
  scoped to the draws the declaration covers (:func:`_scoped_module_rng`): those are
  SKIP, a draw by a node that declared nothing still FAILS.
- ``contract`` -- per arm, on every fixture:
  ``scope_audit``: the selection's top runs its OWN optional
  ``audit_scopes(scope_log, tick) -> list[str]`` over the sub-ticks it built
  (``tick.restrict``, recorded only while auditing), and every committed pair must lie
  inside one of those sub-ticks (a node that audits its scopes decides each pair inside
  one); SKIP, with the reason, when the top has no ``audit_scopes`` AND makes no
  containment claim (a central leaf). It FAILS (final review M5: containment must not be
  silently self-certified) when the top has no ``audit_scopes`` but its badge claims a
  decomposition (any badge other than :data:`CENTRAL_BADGES`, read from the registration
  and the stamped provenance) or its solve built sub-ticks. This certifies the declared
  decomposition shape; it cannot prove a policy never peeks beyond its sub-ticks.
  ``information_scope``: FRAMEWORK-MEASURED, not the solver's word. The battery wraps
  ``Tick.restrict`` / ``Tick.call`` (from this side; ``dispatch/tick.py`` is not
  touched) and records, for every sub-tick, the carrier hauliers of its trucks and the
  owner hauliers of its orders, and which inner decided it. The detail reports the
  widest view an inner call had against the tick's hauliers. SKIP when no sub-tick was
  built (one central view; nothing to contain); FAIL when the top's badge claims a
  decomposition but no fixture with two or more hauliers made it build a sub-tick
  (the badge is unbacked by any measured split); FAIL when the badge claims a
  decomposition but a sub-tick held EVERY truck of a tick whose carriers the fixture's
  ceiling separates into two or more groups (:func:`_unsplit_fleets`: the fleet was not
  split, so that inner saw every company's trucks -- a single whole-tick ``restrict``,
  or per-company sub-ticks that each carry the whole fleet); PASS otherwise, with the
  measurement. Measured, never keyed on the badge's text.
  ``bind_pure``: binding the selection twice against the fixture's profile gives equal
  declarations and reads, and leaves the ``random`` module state and the profile
  unchanged.
  ``world_reads_at_bind``: every world key read while solving the tick was read by
  some node's ``bind`` (the runner re-binds only when a key a bind read changes, so a
  key read only while solving would be served stale).
  ``declaration_within_ceiling``: the top's ``cross_company`` lies inside the
  fixture's ceiling (``constraints.declared_partnerships``) and every nested
  declaration inside the top's (messages 23 / 24, checked explicitly).
- ``performance`` -- ``scale`` (per arm): best of 3 decide times on the 1000 x 500
  ``scale`` tick under :func:`arm_budget`. A budget is a MEASURED property of an
  arm (``max(3 x worst of 2 x 5 runs, 500)``, numbers at the decorator). An
  unmeasured composite ``inner=X`` arm is held to X's own declared budget plus the
  composite's overhead; any other unmeasured arm inherits the default arm's budget,
  so it fails loudly when slower, which forces a measurement. NOTE: a budget above
  ``STEP_SETTLE_SECONDS`` (2000 ms; ``scenario/runtime_settings.py``
  ``LONG_RUN_STEP_SETTLE_SECONDS``) means
  that arm makes the assignment agent a per-step straggler at 1000 trucks x 500
  orders: every such tick the scheduler waits for it (CLAUDE.md §6.17b).
- ``params`` -- defaults validate (the whole default tree builds); unknown keys,
  out-of-bounds values, bad choices and bad policy references (an unregistered
  target; a decomposition where ``leaf_only``) are rejected; the schema uses only
  ``SUPPORTED_SCHEMA_KEYWORDS``.

**Planning tops** (an arm whose bound top declares ``Declaration.planning``): every
decision is made the way the agent drives it -- a PLANNING tick first
(``Fixture.plan_context``: the fixture's trucks and every fixture order as foresight,
through ``DispatchRunner.run``), then the dispatch tick on the SAME instance, with the
planning tick's ``Result.prefetch`` handed back as ``tick.lookup``. Every check above
judges that dispatch tick (``performance.scale`` times it alone), and
:data:`PLANNING_ARM_CHECKS` run after the arm's ``ARM_CHECKS``:
``contract.planning_declaration`` (the policy is registered ``top_only``: a planning
node can be nothing but the top), ``contract.planning_commits_nothing`` (on every
fixture the planning tick raised nothing and was not discarded: R6 no pairs, R7 a
well-formed prefetch) and ``performance.plan`` (best of 3 planning-tick times on
``scale`` under the declared ``max_hold_seconds``; SKIP when the policy's ``requires``
are missing here). An online arm runs none of them, so its ids are unchanged.

**Arms** (:func:`policy_arms`): the defaults; each non-default ``choices`` value of
each of the policy's OWN ``str`` params; and for each policy-reference param, every
other AVAILABLE target of its kind (``requires`` installed; a leaf where
``leaf_only``; never a ``top_only`` policy) at that target's defaults. Arms are not crossed and not recursive (an
inner's own arms are certified by the inner's battery). Every :data:`ARM_CHECKS` id
runs per arm, with the arm in the id: ``structure.no_order_twice[candidates=sampled]``,
``performance.scale[inner=GreedyNearest]``.

Randomness: the battery injects NOTHING (no ``set_rng``/``set_tiebreak``). The
context is the only randomness input (``ctx.rng = Random(tick_seed)``); the
``random`` module is seeded with the fixture's ``seed`` around each decide and
restored afterwards (``Partitioned`` draws from it by declaration). The runner's
CONTRACT VIOLATION log lines are silenced while the battery runs (it reports them as
FAILs instead).

Testing side: reads the registry and the dispatch framework; registers nothing.
"""

from __future__ import annotations

import copy
import logging
import math
import random
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping, NamedTuple

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.policies.compat.legacy_context import LegacyDecideContext
from apps.container_logistics.policies.dispatch import runner as _runner_module
from apps.container_logistics.policies.dispatch import tick as _tick_module
from apps.container_logistics.policies.dispatch.invariants import check_result
from apps.container_logistics.policies.dispatch.result import Result
from apps.container_logistics.policies.dispatch.runner import DispatchRunner, build_runner_for_policy
from apps.container_logistics.policies.dispatch.tick import Tick
from apps.container_logistics.policies.framework.components import ref_names, ref_target
from apps.container_logistics.policies.framework.errors import PolicyError, SlotNotImplementedError
from apps.container_logistics.policies.framework.params import SUPPORTED_SCHEMA_KEYWORDS, declared_params
from apps.container_logistics.policies.framework.registry import PolicySpec, get_policy_spec, list_policies

from . import fixtures as fx
from .fixtures import seeded_module_random

logger = logging.getLogger(__name__)

__all__ = [
    "CENTRAL_BADGES",
    "CheckResult",
    "Skip",
    "DISPATCH_CHECKS",
    "ARM_CHECKS",
    "PLANNING_ARM_CHECKS",
    "STRUCTURE_CHECKS",
    "arm_budget",
    "arm_label",
    "battery_check_ids",
    "policy_arms",
    "classify_contract_problem",
    "run_battery",
    "decide_seeded",
    "plan_then_decide_seeded",
    "arm_planning",
    "seeded_module_random",
]

#: The badges that make NO information-containment claim: none, and the pair leaves'
#: ``central`` (one planner sees the whole tick; ``solver_lib.pair_leaf.CENTRAL_BADGE``,
#: pinned equal by ``tests/test_policy_battery_information_scope.py``). Any other badge
#: (``per group``, ``per company``, a third party's) claims a decomposition, so the
#: top must let the battery audit it (``audit_scopes``).
CENTRAL_BADGES: frozenset[str | None] = frozenset({None, "central"})

STRUCTURE_CHECKS: tuple[str, ...] = (
    "returns_plan", "no_truck_twice", "no_order_twice", "only_in_scope", "only_allowed_pairs",
)

ARM_CHECKS: tuple[str, ...] = (
    *(f"structure.{c}" for c in STRUCTURE_CHECKS),
    *(f"robustness.{make.__name__}" for make in fx.ROBUSTNESS_FIXTURES),
    "determinism.reproducible",
    "determinism.order_independent",
    "determinism.no_module_rng",
    "contract.scope_audit",
    "contract.information_scope",
    "contract.bind_pure",
    "contract.world_reads_at_bind",
    "contract.declaration_within_ceiling",
    "performance.scale",
)

#: The extra per-arm checks of an arm whose top declares ``Declaration.planning`` (run
#: after :data:`ARM_CHECKS`; an online arm has none of them, so its ids are unchanged).
PLANNING_ARM_CHECKS: tuple[str, ...] = (
    "contract.planning_declaration",
    "contract.planning_commits_nothing",
    "performance.plan",
)

DISPATCH_CHECKS: tuple[str, ...] = (
    *ARM_CHECKS,
    "params.defaults_validate",
    "params.rejects_unknown_key",
    "params.rejects_out_of_bounds",
    "params.rejects_bad_choice",
    "params.rejects_bad_ref",
    "params.schema_keywords_supported",
)

# check_result's message kinds (invariants.py), each keyed on its stable wording, and
# the structure check it fails. tests/test_policy_battery_structure_via_check_plan.py
# asserts this table covers every message check_result can produce.
_PLAN_MESSAGE_KINDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("returns_plan", re.compile(r"^solve_tick\(\) returned .+, expected Result$")),
    ("returns_plan", re.compile(r"^pair .+ is not a \(truck_id, order_id\) pair of strings$")),
    ("no_truck_twice", re.compile(r"^truck .+ appears twice in the result$")),
    ("no_order_twice", re.compile(r"^order .+ appears twice in the result$")),
    ("only_in_scope", re.compile(r"^truck .+ is not in scope ")),
    ("only_in_scope", re.compile(r"^order .+ is not in scope ")),
    ("only_allowed_pairs", re.compile(r"^pair \(.+\) is not allowed$")),
    ("returns_plan", re.compile(r"^solver_notes key .+ is not one of the result's pairs$")),
    ("returns_plan", re.compile(r"^solver_notes for .+ is not a mapping with string keys$")),
    # Only with the framework's ``intact`` (runner / ``Tick.call``), never in the battery's own calls.
    ("returns_plan", re.compile(r"^(truck|order) .+ was modified during the solve \(documents are read-only\)$")),
)
# Raised by Result.from_pairs (or the legacy Plan.from_pairs) inside the solve (a pair
# list cannot repeat a truck legally).
_RAISED_MESSAGE_KINDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("no_truck_twice", re.compile(r"^truck .+ appears twice in pairs$")),
)
# Anything else (a policy-raised violation, or a check_plan message this table does
# not know yet) still FAILS: it is never silently dropped.
_UNCLASSIFIED = "returns_plan"

_SHUFFLES = 20
_PERF_REPEATS = 3
_BAD_REF = "__battery_not_a_policy__"


@dataclass(frozen=True)
class CheckResult:
    """One check's verdict. ``id`` is ``<group>.<check>``; ``check`` carries the arm.

    ``arm`` is ``""`` for the default params, else ``"<param>=<value>"`` (a ref arm
    names the target: ``inner=GreedyNearest``), and then ``check`` ends in ``[<arm>]``.
    ``skipped`` marks a check that did not run (or could not decide) because of a
    declaration (``order_sensitive`` / ``module_rng``); a skip is not a failure, so
    ``ok`` stays true, but it is reported as ``SKIP``, never as ``PASS``
    (:attr:`status`; final review MINOR-1). The ids are the same either way.
    """

    group: str
    check: str
    ok: bool
    detail: str
    ms: float
    arm: str = ""
    skipped: bool = False

    @property
    def id(self) -> str:
        return f"{self.group}.{self.check}"

    @property
    def status(self) -> str:
        """``"SKIP"``, ``"PASS"`` or ``"FAIL"``."""
        if self.skipped:
            return "SKIP"
        return "PASS" if self.ok else "FAIL"


class Skip(NamedTuple):
    """What a check function returns to report a SKIP (``ok`` true): ``(True, detail)``.

    A ``NamedTuple``, so a caller that unpacks ``ok, detail = check(...)`` still works.
    """

    ok: bool
    detail: str


def _skip(detail: str) -> Skip:
    return Skip(True, f"skipped: {detail}")


def decide_seeded(
    runner: DispatchRunner,
    fixture: Any,
    *,
    trucks: list[dict[str, Any]] | None = None,
    orders: list[dict[str, Any]] | None = None,
) -> tuple[Tick, Any]:
    """The top's raw ``solve_tick`` on a fresh whole fixture tick, module RNG seeded.

    The policy's raw output, NOT the runner path (no ``check_result``): this is what
    the policy golden captures. Nothing is injected (the tick is the randomness input);
    the ``random`` module is seeded with ``fixture.seed`` for the call. Exceptions
    propagate.
    """
    with fixture.module_random():
        tick = fixture.context(runner, trucks=trucks, orders=orders)
        return tick, runner.solve_unchecked(tick)


def _run_plan_tick(runner: DispatchRunner, fixture: Any, planning: Mapping[str, Any], *,
                   trucks: list[dict[str, Any]] | None = None, orders: list[dict[str, Any]] | None = None,
                   scope_log: list[dict[str, Any]] | None = None) -> tuple[Any, dict[str, dict[str, Any]]]:
    """One PLANNING tick of ``fixture`` through ``runner.run`` (every fixture order as foresight).

    Returns ``(outcome, lookup)``: the runner's ``TickOutcome`` and the ``{order_id: doc}``
    the next dispatch tick receives as ``tick.lookup`` for the plan's ``Result.prefetch``
    (the fixture's own documents, as the live app reads them back by id). Call inside
    ``fixture.module_random()``.
    """
    plan_tick = fixture.plan_context(runner, foresight=planning.get("foresight", "none"), trucks=trucks,
                                     orders=orders, scope_log=scope_log)
    outcome = runner.run(plan_tick)
    return outcome, _lookup_for(outcome, fixture.orders if orders is None else orders)


def _lookup_for(outcome: Any, orders: Any) -> dict[str, dict[str, Any]]:
    """``{order_id: doc}`` of ``orders`` named by ``outcome``'s (checked) ``prefetch``; ``{}`` when there is none."""
    result = getattr(outcome, "result", None)
    wanted = set(result.prefetch) if result is not None else set()
    if not wanted:
        return {}
    return {str(o.get("_id")): o for o in orders if str(o.get("_id")) in wanted}


def plan_then_decide_seeded(runner: DispatchRunner, fixture: Any) -> tuple[Tick, Any]:
    """:func:`decide_seeded` for a PLANNING top: one planning tick first, on the same instance.

    The planning tick runs through ``runner.run`` (checked; it commits nothing, R6) with
    every fixture order as foresight, then the dispatch tick (with the plan's prefetched
    documents as ``tick.lookup``) is solved raw, exactly like :func:`decide_seeded`. One
    module-RNG seeding covers both. For the policy golden of ``top_only`` planning
    policies (addendum M6); raises ``PolicyError`` when the runner's top declares no
    planning.
    """
    planning = runner.tree.declaration.planning
    if planning is None:
        raise PolicyError(f"plan_then_decide_seeded: {runner.tree.top.name} declares no planning")
    with fixture.module_random():
        _outcome, lookup = _run_plan_tick(runner, fixture, planning)
        tick = fixture.context(runner, lookup=lookup)
        return tick, runner.solve_unchecked(tick)


def arm_planning(spec: PolicySpec, arm: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The normalised ``Declaration.planning`` of ``spec`` at ``arm`` (bound against an empty world), or ``None``.

    ``None`` too when the arm cannot even be built or bound (a ``PolicyError``): its checks
    then FAIL on their own (``params.defaults_validate``, ``robustness.*``), with the ids
    of an online arm.
    """
    try:
        return build_runner_for_policy(spec.name, _arm_params(arm)).tree.declaration.planning
    except PolicyError:
        return None


# --- arms --------------------------------------------------------------------

def _ref_arm_targets(info: Any) -> list[str]:
    """Every other available target of ``info``'s kind (a leaf where ``leaf_only``; never ``top_only``), sorted."""
    kind, default = info.meta.kind, getattr(info.default, "type", None)
    names = []
    if kind == "planner":
        for spec in list_policies("planner"):
            if spec.missing_requirements() or spec.top_only or (info.meta.leaf_only and spec.is_composite):
                continue
            names.append(spec.name)
    else:
        names = list(ref_names(kind))
    return [n for n in sorted(names) if n != default]


def policy_arms(spec: PolicySpec) -> list[dict[str, Any]]:
    """``[{}]`` (the defaults) plus one single-param arm per non-default choice or ref target.

    A str arm is ``{param: choice}``; a ref arm is ``{param: {"type": target}}`` (the
    target at its defaults). Own params only, declared order; not crossed, not recursive.
    """
    arms: list[dict[str, Any]] = [{}]
    for info in declared_params(spec.params_cls):
        if info.is_ref:
            arms += [{info.name: {"type": t}} for t in _ref_arm_targets(info)]
        elif info.type is str and info.meta.choices is not None:
            arms += [{info.name: c} for c in info.meta.choices if c != info.default]
    return arms


def arm_label(arm: Mapping[str, Any]) -> str:
    """``""`` for the defaults, else ``param=value`` (``param=Target`` for a ref arm)."""
    return ",".join(f"{k}={v['type'] if isinstance(v, Mapping) else v}" for k, v in arm.items())


def _arm_suffix(arm: Mapping[str, Any]) -> str:
    return f"[{arm_label(arm)}]" if arm else ""


def arm_budget(spec: PolicySpec, arm: Mapping[str, Any]) -> tuple[int, str]:
    """The ``performance.scale`` budget of ``spec`` on ``arm``, and where it comes from.

    The ONE place an arm's budget is decided (final review MAJOR-3), first rule that applies:

    1. the default arm: ``perf_budget_ms``;
    2. a MEASURED arm: ``arm_budgets_ms[<arm label>]`` (an explicit key always wins);
    3. an unmeasured ``planner`` ref arm (a composite's ``inner=X``): X's own budget at
       the arm it runs at (X at its defaults, so ``arm_budget(X, {})`` =
       ``X.perf_budget_ms``) PLUS the composite's overhead, taken as the composite's
       default-arm budget. That default budget bounds the composite's own work plus one
       solve of its default inner on the same ``scale`` tick, so the sum over-covers the
       overhead by one default-inner solve. On ``scale`` (one haulier, one company, no
       pools) ``Partitioned`` and ``PooledMarket`` call the inner on exactly one scope,
       which is what makes "inner's budget + overhead" the right shape; a composite that
       calls its inner many times on that tick must measure the arm;
    4. any other unmeasured arm (a str choice, a component ref) inherits the default
       arm's budget, so it fails loudly when slower, which forces a measurement.

    Rule 3 is why registering a correct but slow-by-nature leaf (a NetworkFlow solver
    with a large declared budget) does not turn every composite's battery red: the
    composite is held to the leaf's own declared cost, not to a number measured with a
    different inner. A leaf slower than even its own budget still fails, here and in
    its own battery.
    """
    label = arm_label(arm)
    if not label:
        return spec.perf_budget_ms, "perf_budget_ms"
    if label in spec.arm_budgets_ms:
        return spec.arm_budgets_ms[label], f"arm_budgets_ms[{label!r}]"
    inner = _dispatch_ref_target(spec, arm)
    if inner is not None:
        target, target_params = inner
        inner_ms, inner_source = arm_budget(target, target_params)
        total = inner_ms + spec.perf_budget_ms
        return total, (f"arm not measured: derived = {target.name} {inner_source} {inner_ms} ms "
                       f"+ {spec.name} overhead perf_budget_ms {spec.perf_budget_ms} ms")
    return spec.perf_budget_ms, "perf_budget_ms (arm not measured: it inherits the default arm's budget)"


def _dispatch_ref_target(spec: PolicySpec, arm: Mapping[str, Any]) -> tuple[PolicySpec, dict[str, Any]] | None:
    """``(target spec, its params)`` when ``arm`` is one ``planner``-kind ref param set to a target; else ``None``."""
    if len(arm) != 1:
        return None
    (key, value), = arm.items()
    if not isinstance(value, Mapping):
        return None
    info = next((i for i in declared_params(spec.params_cls) if i.name == key), None)
    if info is None or not info.is_ref or info.meta.kind != "planner":
        return None
    return get_policy_spec("planner", value["type"]), dict(value.get("params") or {})


def battery_check_ids(slot: str, name: str) -> list[str]:
    """Every id ``run_battery(slot, name)`` returns, in order.

    ``DISPATCH_CHECKS`` (the default arm), then ``ARM_CHECKS`` once per non-default
    arm with ``[<arm>]`` appended. An arm whose top declares planning also runs
    :data:`PLANNING_ARM_CHECKS`, right after its ``ARM_CHECKS`` (for the default arm:
    before the ``params.*`` checks). Raises like :func:`run_battery`.
    """
    spec = _dispatch_spec(slot, name)
    arms = policy_arms(spec)
    default_extra = list(PLANNING_ARM_CHECKS) if arm_planning(spec, arms[0]) is not None else []
    out = list(ARM_CHECKS) + default_extra + [c for c in DISPATCH_CHECKS if c not in ARM_CHECKS]
    for arm in arms[1:]:
        ids = list(ARM_CHECKS) + (list(PLANNING_ARM_CHECKS) if arm_planning(spec, arm) is not None else [])
        out += [f"{cid}{_arm_suffix(arm)}" for cid in ids]
    return out


def classify_contract_problem(message: str, *, raised: bool = False) -> str:
    """The structure check (an entry of ``STRUCTURE_CHECKS``) a contract message fails.

    ``raised`` marks a ``DispatchContractViolation`` raised inside the solve (e.g.
    ``Result.from_pairs``); otherwise ``message`` is one of ``check_result``'s. An
    unknown message maps to ``returns_plan``, so it still fails.
    """
    for check, pattern in (_RAISED_MESSAGE_KINDS if raised else _PLAN_MESSAGE_KINDS):
        if pattern.search(message):
            return check
    return _UNCLASSIFIED


def run_battery(slot: str, name: str) -> list[CheckResult]:
    """Run every check in :func:`battery_check_ids` on policy ``name`` of ``slot``.

    Raises ``UnknownPolicyError`` for an unregistered name and
    ``SlotNotImplementedError`` for a slot without a battery (only ``planner`` has one).
    """
    spec = _dispatch_spec(slot, name)
    results: list[CheckResult] = []
    with _quiet_runner_violations():
        for arm in policy_arms(spec):
            results += _arm_results(spec, arm)
            if not arm:
                results += _default_only_results(spec)
    return results


def _dispatch_spec(slot: str, name: str) -> PolicySpec:
    spec = get_policy_spec(slot, name)
    # Unreachable while ``planner`` is the only slot (get_policy_spec refuses any other
    # first). Kept on purpose: adding a slot is one SlotSpec entry, and that new slot must
    # be refused here until it has a battery, not run through the planner's checks.
    if slot != "planner":
        raise SlotNotImplementedError(f"no contract battery for slot {slot!r}; only 'planner' has one")
    return spec


def _arm_results(spec: PolicySpec, arm: dict[str, Any]) -> list[CheckResult]:
    outcomes = _Outcomes(spec, arm)
    checks: dict[str, Callable[[], tuple[bool, str]]] = {}
    for check in STRUCTURE_CHECKS:
        checks[f"structure.{check}"] = _bind(_structure_check, outcomes, check)
    for make in fx.ROBUSTNESS_FIXTURES:
        checks[f"robustness.{make.__name__}"] = _bind(_robustness_check, outcomes, make)
    checks["determinism.reproducible"] = _bind(_reproducible, spec, arm)
    checks["determinism.order_independent"] = _bind(_order_independent, spec, arm)
    checks["determinism.no_module_rng"] = _bind(_no_module_rng, outcomes)
    checks["contract.scope_audit"] = _bind(_scope_audit, outcomes)
    checks["contract.information_scope"] = _bind(_information_scope, outcomes)
    checks["contract.bind_pure"] = _bind(_bind_pure, spec, arm)
    checks["contract.world_reads_at_bind"] = _bind(_world_reads_at_bind, outcomes)
    checks["contract.declaration_within_ceiling"] = _bind(_declaration_within_ceiling, spec, arm)
    checks["performance.scale"] = _bind(_performance_scale, spec, arm)
    ids = list(ARM_CHECKS)
    planning = arm_planning(spec, arm)
    if planning is not None:
        checks["contract.planning_declaration"] = _bind(_planning_declaration, spec, planning)
        checks["contract.planning_commits_nothing"] = _bind(_planning_commits_nothing, outcomes)
        checks["performance.plan"] = _bind(_performance_plan, spec, arm, planning)
        ids += PLANNING_ARM_CHECKS
    return [_timed(cid, checks[cid], arm) for cid in ids]


def _default_only_results(spec: PolicySpec) -> list[CheckResult]:
    checks: dict[str, Callable[[], tuple[bool, str]]] = {
        "params.defaults_validate": _bind(_defaults_validate, spec),
        "params.rejects_unknown_key": _bind(_rejects_unknown_key, spec),
        "params.rejects_out_of_bounds": _bind(_rejects_out_of_bounds, spec),
        "params.rejects_bad_choice": _bind(_rejects_bad_choice, spec),
        "params.rejects_bad_ref": _bind(_rejects_bad_ref, spec),
        "params.schema_keywords_supported": _bind(_schema_keywords_supported, spec),
    }
    return [_timed(cid, checks[cid], {}) for cid in DISPATCH_CHECKS if cid not in ARM_CHECKS]


def _bind(fn: Callable[..., tuple[bool, str]], *args: Any) -> Callable[[], tuple[bool, str]]:
    return lambda: fn(*args)


def _timed(check_id: str, fn: Callable[[], tuple[bool, str]], arm: Mapping[str, Any]) -> CheckResult:
    group, _, check = check_id.partition(".")
    start = time.perf_counter()
    skipped = False
    try:
        outcome = fn()
        ok, detail = outcome
        skipped = isinstance(outcome, Skip)
    except Exception as exc:  # noqa: BLE001 - a crashing check is a FAIL, never a crash of the battery
        ok, detail = False, f"raised {type(exc).__name__}: {exc}"
    return CheckResult(group=group, check=f"{check}{_arm_suffix(arm)}", ok=ok, detail=detail,
                       ms=round((time.perf_counter() - start) * 1000.0, 3), arm=arm_label(arm),
                       skipped=skipped)


# --- the runner path ---------------------------------------------------------

class _ViolationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not str(record.msg).startswith("PLANNER POLICY CONTRACT VIOLATION")


@contextmanager
def _quiet_runner_violations() -> Iterator[None]:
    """Drop the CONTRACT VIOLATION ERROR lines for the block (the battery reports them).

    The runner logs a tick violation (message 18); a sub-tick violation (message 19)
    is logged by ``dispatch.tick``.
    """
    flt = _ViolationFilter()
    loggers = (_runner_module.logger, _tick_module.logger)
    for lg in loggers:
        lg.addFilter(flt)
    try:
        yield
    finally:
        for lg in loggers:
            lg.removeFilter(flt)


class _PlanRecorder:
    """Capture hook: the raw TICK result and any violation the solve raised (``DispatchRunner.run``).

    Sub-tick calls (``Tick.call`` reuses the hook) are ignored.
    """

    def __init__(self) -> None:
        self.plan: Any = None
        self.raised: list[str] = []

    def __call__(self, *, tick: Tick, result: Any, problems: list[str]) -> None:
        if tick.depth != 0:
            return
        self.plan = result
        self.raised = list(problems)


@dataclass(frozen=True)
class _Decision:
    """One ``DispatchRunner.run`` on one whole-tick fixture: what came back and what it touched."""

    fixture: Any
    n_trucks: int
    plan: Any                                   # the raw tick result
    pairs: tuple[tuple[str, str], ...]          # committed pairs (truck id, order id)
    problems: tuple[tuple[str, str], ...]       # (structure check, message)
    error: str | None                           # a non-contract exception, as text
    decide_ms: float
    module_rng_touched: bool
    scope_log: tuple[Mapping[str, Any], ...]
    tick: Any = None                            # the whole tick decided (for the scope audit)
    solve_reads: frozenset[str] = frozenset()   # world keys read while solving it
    bind_reads: frozenset[str] = frozenset()    # world keys the bound tree's binds read
    audit: Any = None                           # the top's audit_scopes, or None
    badge: str | None = None                    # the top's claimed badge (registration or provenance)
    sub_ticks: tuple[Mapping[str, Any], ...] = ()   # framework-measured: every restrict() and its call
    tick_hauliers: frozenset[str] = frozenset() # hauliers of the whole tick's trucks and orders
    planned: bool = False                       # a planning tick ran first (the top declares planning)
    plan_pairs: int = 0                         # pairs the planning tick's raw result proposed
    plan_problems: tuple[str, ...] = ()         # the planning tick's discard reasons (R6, R7, ...)
    plan_error: str | None = None               # a non-contract exception of the planning tick
    plan_ms: float = 0.0                        # the planning tick's solve time

    @property
    def fixture_id(self) -> str:
        return self.fixture.id


def _arm_params(arm: Mapping[str, Any]) -> dict[str, Any]:
    return {k: dict(v) if isinstance(v, Mapping) else v for k, v in arm.items()}


def _decide_via_runner(
    spec: PolicySpec,
    arm: Mapping[str, Any],
    fixture: Any,
    *,
    trucks: list[dict[str, Any]] | None = None,
    orders: list[dict[str, Any]] | None = None,
) -> _Decision:
    """``DispatchRunner.run`` on a fresh ``spec`` instance with params ``arm``, on one tick.

    The module RNG is seeded with ``fixture.seed``; nothing is injected. The
    structure problems are ``check_result``'s against the tick (or the violation the
    solve raised), classified per check; a runner verdict that disagrees with them is
    a problem too. The sub-ticks the policy builds are recorded (``scope_log``) for the
    scope audit, and the world keys read while solving (``solve_reads``) against the
    bound tree's (``bind_reads``).
    """
    recorder = _PlanRecorder()
    runner = build_runner_for_policy(spec.name, _arm_params(arm), capture=recorder)
    n_trucks = len(fixture.trucks if trucks is None else trucks)
    error: str | None = None
    scope_log: list[dict[str, Any]] = []
    pairs: list[tuple[str, str]] = []
    sub_ticks: list[dict[str, Any]] = []
    planning = runner.tree.declaration.planning
    plan: dict[str, Any] = {}
    lookup: dict[str, dict[str, Any]] = {}
    plan_reads: frozenset[str] = frozenset()
    with fixture.module_random():
        before = random.getstate()
        if planning is not None:
            # A planning top is decided as the live agent drives it: a planning tick first
            # (every fixture order as foresight), then the dispatch tick on the SAME instance.
            plan, lookup, plan_reads = _planning_first(runner, recorder, fixture, planning, trucks, orders)
        violations_before = runner.stats["violations"]
        ms_before = runner.stats["total_ms"]
        ctx = fixture.context(runner, trucks=trucks, orders=orders, scope_log=scope_log, lookup=lookup)
        try:
            with _measured_sub_ticks(sub_ticks):
                pairs = [(str(t.get("_id")), str(o.get("_id"))) for t, o in runner.run(ctx).pairs]
        except Exception as exc:  # noqa: BLE001 - reported by robustness
            error = f"{type(exc).__name__}: {exc}"
        touched = random.getstate() != before
    solve_reads = ctx.world.reads | plan_reads
    tree = runner.tree  # bound against the fixture's profile by runner.tick
    problems: list[tuple[str, str]] = []
    if error is None:
        if recorder.raised:
            problems = [(classify_contract_problem(m, raised=True), m) for m in recorder.raised]
        else:
            problems = [(classify_contract_problem(m), m) for m in check_result(recorder.plan, ctx)]
        discarded = runner.stats["violations"] > violations_before
        if discarded != bool(problems):
            problems.append((_UNCLASSIFIED, f"the runner {'discarded' if discarded else 'committed'} the result "
                                            f"but check_result found {len(problems)} problem(s)"))
    return _Decision(
        fixture=fixture,
        n_trucks=n_trucks,
        plan=recorder.plan,
        pairs=tuple(pairs),
        problems=tuple(problems),
        error=error,
        decide_ms=runner.stats["total_ms"] - ms_before,
        module_rng_touched=touched,
        scope_log=tuple(scope_log),
        tick=ctx,
        solve_reads=solve_reads,
        bind_reads=tree.world_reads,
        audit=getattr(tree.top.solver, "audit_scopes", None),
        badge=_claimed_badge(spec, tree),
        sub_ticks=tuple(sub_ticks),
        tick_hauliers=_hauliers_of(ctx.raw_trucks, ctx.raw_orders),
        planned=planning is not None,
        plan_pairs=plan.get("pairs", 0),
        plan_problems=tuple(plan.get("problems", ())),
        plan_error=plan.get("error"),
        plan_ms=plan.get("ms", 0.0),
    )


def _planning_first(runner: DispatchRunner, recorder: "_PlanRecorder", fixture: Any, planning: Mapping[str, Any],
                    trucks: Any, orders: Any) -> tuple[dict[str, Any], dict[str, dict[str, Any]], frozenset[str]]:
    """Run the planning tick of a planning arm; ``(record, lookup, world reads)`` for :class:`_Decision`.

    ``record``: ``pairs`` the raw planning result proposed, ``problems`` the runner
    discarded it for (R6 ``a planning tick commits no pairs``, a malformed prefetch R7,
    ...: the raw result is re-checked here), ``error`` a non-contract exception, ``ms``.
    Its sub-ticks are not recorded (the scope audit judges the dispatch tick).
    """
    violations, ms = runner.stats["violations"], runner.stats["total_ms"]
    record: dict[str, Any] = {"pairs": 0, "problems": [], "error": None, "ms": 0.0}
    lookup: dict[str, dict[str, Any]] = {}
    reads: frozenset[str] = frozenset()
    try:
        plan_tick = fixture.plan_context(runner, foresight=planning.get("foresight", "none"),
                                         trucks=trucks, orders=orders)
        reads_view = plan_tick.world
        outcome = runner.run(plan_tick)
        reads = frozenset(reads_view.reads)
        lookup = _lookup_for(outcome, fixture.orders if orders is None else orders)
        raw = recorder.plan
        record["pairs"] = len(raw.pairs) if isinstance(raw, Result) else 0
        if runner.stats["violations"] > violations:
            record["problems"] = list(recorder.raised) or _plan_tick_problems(raw, plan_tick)
    except Exception as exc:  # noqa: BLE001 - reported by contract.planning_commits_nothing
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["ms"] = runner.stats["total_ms"] - ms
    recorder.plan, recorder.raised = None, []
    return record, lookup, reads


def _plan_tick_problems(raw: Any, plan_tick: Any) -> list[str]:
    """Why the runner discarded a planning tick's ``raw`` result (re-derived; the runner only logs it)."""
    from apps.container_logistics.policies.dispatch.invariants import check_wire_notes, normalise_result

    plain, problems = normalise_result(raw, strict=True)
    if isinstance(plain, Result) and plain.pairs:
        problems = [f"a planning tick commits no pairs (got {len(plain.pairs)})", *problems]
    elif isinstance(plain, Result):
        problems = [*problems, *check_wire_notes(plain, ())]
    else:
        problems = [*problems, *check_result(plain, plan_tick)]
    return problems or ["the runner discarded the planning tick's result (see the CONTRACT VIOLATION log)"]


def _claimed_badge(spec: PolicySpec, tree: Any) -> str | None:
    """The top's badge: its registration's, else the one its bound declaration stamps."""
    if spec.badge is not None:
        return spec.badge
    badge = (tree.top.declaration.provenance or {}).get("badge")
    return badge if isinstance(badge, str) else None


def _hauliers_of(trucks: Any, orders: Any) -> frozenset[str]:
    return frozenset(h for h in (*(assign_constraints.haulier_of(t) for t in trucks),
                                 *(assign_constraints.haulier_of(o) for o in orders)) if h is not None)


@contextmanager
def _measured_sub_ticks(record: list[dict[str, Any]]) -> Iterator[None]:
    """Record every ``Tick.restrict`` (what the sub-tick shows) and ``Tick.call`` (who decided it).

    Wraps the two class attributes for the block and restores them; the wrappers call
    the originals unchanged, so the decision is the one the sim makes. Each record:
    ``scope``, ``depth``, ``carriers`` (the trucks' hauliers), ``owners`` (the orders'),
    ``n_trucks``, ``n_orders``, ``callee`` (the inner that solved it, ``None`` if
    never called).
    """
    original_restrict, original_call = Tick.restrict, Tick.call
    by_sub: dict[int, dict[str, Any]] = {}

    def restrict(self: Tick, **kwargs: Any) -> Tick:
        sub = original_restrict(self, **kwargs)
        entry = {
            "scope": sub.scope, "depth": sub.depth,
            "carriers": frozenset(h for h in map(assign_constraints.haulier_of, sub.raw_trucks) if h is not None),
            "owners": frozenset(h for h in map(assign_constraints.haulier_of, sub.raw_orders) if h is not None),
            "n_trucks": len(sub.raw_trucks), "n_orders": len(sub.raw_orders), "callee": None,
        }
        by_sub[id(sub)] = entry
        record.append(entry)
        return sub

    def call(self: Tick, ref: Any, sub: Tick) -> Any:
        entry = by_sub.get(id(sub))
        if entry is not None and entry["callee"] is None:
            try:
                entry["callee"] = self.callee(ref).name
            except Exception:  # noqa: BLE001 - the original call reports a bad ref itself
                entry["callee"] = "?"
        return original_call(self, ref, sub)

    Tick.restrict, Tick.call = restrict, call
    try:
        yield
    finally:
        Tick.restrict, Tick.call = original_restrict, original_call


class _Outcomes:
    """One runner decision per ``ROBUSTNESS_FIXTURES`` fixture for one arm, made on first use."""

    def __init__(self, spec: PolicySpec, arm: dict[str, Any]) -> None:
        self.spec, self.arm = spec, arm
        self._by_fixture: dict[str, _Decision] = {}

    def get(self, make: Callable[[], Any]) -> _Decision:
        key = make.__name__
        if key not in self._by_fixture:
            self._by_fixture[key] = _decide_via_runner(self.spec, self.arm, make())
        return self._by_fixture[key]

    def all(self) -> list[_Decision]:
        return [self.get(make) for make in fx.ROBUSTNESS_FIXTURES]


# --- structure ---------------------------------------------------------------

def _structure_check(outcomes: _Outcomes, check: str) -> tuple[bool, str]:
    problems: list[str] = []
    skipped: list[str] = []
    for d in outcomes.all():
        mine = [f"{d.fixture_id}: {msg}" for kind, msg in d.problems if kind == check]
        if mine:
            problems += mine
        elif d.error is not None or not isinstance(d.plan, Result):
            skipped.append(d.fixture_id)
    if problems:
        return False, "; ".join(problems)
    note = (f" (not checked on {', '.join(skipped)}: no Result to check, see robustness.* "
            "and the other structure.* checks)") if skipped else ""
    return True, f"check_result clean on {len(outcomes.all()) - len(skipped)} whole tick(s){note}"


# --- robustness --------------------------------------------------------------

def _robustness_check(outcomes: _Outcomes, make: Callable[[], Any]) -> tuple[bool, str]:
    d = outcomes.get(make)
    timing = f" in {d.decide_ms:.1f} ms"
    if d.error is not None:
        return False, f"decide raised {d.error}"
    if d.problems:
        return True, f"result discarded by the runner{timing}: contract problems, reported under structure.*"
    return True, f"{len(d.plan.pairs)} pair(s) over {d.n_trucks} trucks{timing}"


# --- determinism -------------------------------------------------------------

def _determinism_fixtures() -> list[Any]:
    return [fx.tied_costs(), fx.three_company_chain(), fx.scale(n_trucks=30, n_orders=20, hauliers=2, seed=9)]


def _plan_items(d: _Decision) -> Any:
    if d.error is not None:
        return ("raised", d.error)
    return list(d.plan.pairs) if isinstance(d.plan, Result) else d.plan


def _plan_mapping(d: _Decision) -> Any:
    """Pairs as a truck -> order dict (commit order ignored), or the non-result outcome itself."""
    return dict(d.plan.pairs) if d.error is None and isinstance(d.plan, Result) else _plan_items(d)


def _reproducible(spec: PolicySpec, arm: dict[str, Any]) -> tuple[bool, str]:
    fixtures = _determinism_fixtures() + [fx.scale(n_trucks=200, n_orders=100, hauliers=2, seed=5)]
    for fixture in fixtures:
        first = _decide_via_runner(spec, arm, fixture)
        second = _decide_via_runner(spec, arm, fixture)
        if _plan_items(first) != _plan_items(second):
            return False, (f"{fixture.id}: two fresh instances on the same tick (tick seed {fixture.tick_seed}) "
                           f"gave different plans: {_plan_items(first)[:4]} vs {_plan_items(second)[:4]}")
    return True, f"same plan twice on {', '.join(f.id for f in fixtures)}"


def _order_independent(spec: PolicySpec, arm: dict[str, Any]) -> tuple[bool, str]:
    """Shuffling trucks and orders (same tick seed) must not change the plan.

    Narrower than it reads: the fixtures keep fewer trucks than the spatial
    per-order cap (``spatial_candidates_per_order``, default 40), because
    ``spatial.iter_candidate_pairs`` truncates candidates in INPUT order. On a dense
    real tick where that cap binds, the framework's candidate set itself depends on
    input order, so no policy is order-independent there.
    """
    scope_note = ("holds only while the spatial per-order candidate cap (spatial_candidates_per_order, "
                  "default 40) does not bind: spatial.iter_candidate_pairs truncates in input order, so on "
                  "dense real ticks the candidate set itself depends on input order")
    declared = _declared_on_selection(spec, arm, "order_sensitive")
    if declared:
        return _skip(f"order_sensitive declared by {declared}{_own_battery_note(spec, arm)}")
    for fixture in _determinism_fixtures():
        want = _plan_mapping(_decide_via_runner(spec, arm, fixture))
        for i in range(_SHUFFLES):
            shuffler = random.Random(1000 + i)
            trucks, orders = list(fixture.trucks), list(fixture.orders)
            shuffler.shuffle(trucks)
            shuffler.shuffle(orders)
            got = _plan_mapping(_decide_via_runner(spec, arm, fixture, trucks=trucks, orders=orders))
            if got != want:
                return False, f"{fixture.id}: shuffle {i} changed the plan: {str(got)[:120]} vs {str(want)[:120]}"
    names = ", ".join(f.id for f in _determinism_fixtures())
    return True, f"one plan over {_SHUFFLES} shuffles on {names}; {scope_note}"


def _declared_on_selection(spec: PolicySpec, arm: Mapping[str, Any], attr: str) -> str:
    """``"<name>: <reason>"`` for every node of the arm's selection tree declaring ``attr``; "" if none.

    The declarations compose (round-2 §5 C5): the default ``PooledMarket`` is
    order-sensitive because its default inner ``RandomAssignment`` is.
    """
    node = build_runner_for_policy(spec.name, _arm_params(arm)).resolved
    reasons = []
    while node is not None:
        reason = getattr(get_policy_spec("planner", node.name), attr)
        if reason is not None:
            reasons.append(f"{node.name}: {reason}")
        node = node.inner
    return "; ".join(reasons)


def _own_battery_note(spec: PolicySpec, arm: Mapping[str, Any]) -> str:
    """For a composite's ``inner=X`` arm: where X's own randomness/order use is certified."""
    target = _dispatch_ref_target(spec, arm)
    if target is None:
        return ""
    name = target[0].name
    return f"; {name} itself is checked in {name}'s own battery (openride policy validate planner {name})"


def _no_module_rng(outcomes: _Outcomes) -> tuple[bool, str]:
    """``DispatchRunner.run`` leaves ``random.getstate()`` alone on every whole tick.

    Nothing is injected: the tick context's ``ctx.rng`` (``Random(tick_seed)``) is the
    only sanctioned randomness, and tick-scope sampling draws from it, so every
    fixture (``scale`` included) is checked.

    When a node of the selection declares ``module_rng`` the declaration is SCOPED
    (final review MINOR-1), not a blanket exemption for the tree: see
    :func:`_scoped_module_rng`. A declared draw is reported as SKIP, a draw by a node
    that declared nothing still FAILS.
    """
    declared = _declared_on_selection(outcomes.spec, outcomes.arm, "module_rng")
    if declared:
        return _scoped_module_rng(outcomes, declared)
    checked, touched = [], []
    for d in outcomes.all():
        if d.error is None:
            checked.append(d.fixture_id)
            if d.module_rng_touched:
                touched.append(d.fixture_id)
    if touched:
        return False, (f"decide drew from the random MODULE on {', '.join(touched)}; draw only from ctx.rng, "
                       "or declare module_rng= at @policy")
    return True, f"random module state unchanged on {len(checked)} whole tick(s)"


# The random module's draw functions: the module-level bound methods of its hidden
# instance (random.random, random.shuffle, random.sample, ..., random.seed).
def _module_draw_names() -> tuple[str, ...]:
    inst = getattr(random, "_inst", None)
    return tuple(sorted(name for name, value in vars(random).items()
                        if not name.startswith("_") and name not in ("getstate", "setstate")
                        and getattr(value, "__self__", None) is inst and callable(value)))


def _selection_instances(runner: DispatchRunner) -> list[tuple[Any, Any]]:
    """``[(built policy, its ResolvedDispatch), ...]`` from the top down the bound tree."""
    return [(bound.instance, bound.node) for bound in runner.tree.nodes]


def _frame_arguments(frame: Any) -> list[Any]:
    """The values of ``frame``'s declared parameters, in order (``self`` first for a method, whatever its name)."""
    code = frame.f_code
    count = code.co_argcount + code.co_kwonlyargcount
    local = frame.f_locals
    return [local[n] for n in code.co_varnames[:count] if n in local]


def _first_argument(frame: Any) -> Any:
    """``frame``'s first parameter (a method's instance), or ``None`` when it takes none."""
    code = frame.f_code
    if not code.co_argcount:
        return None
    return frame.f_locals.get(code.co_varnames[0])


def _attribute_frame(frame: Any, ids: Mapping[int, str]) -> tuple[str, str] | None:
    """``(channel, node name)`` of a module draw whose innermost caller frame is ``frame``; ``None``: no policy frame.

    The owner is the NEAREST frame whose first parameter is one of the selection's
    built policies (``ids``: ``id(instance) -> name``). Matching the first parameter
    by identity, not a local called ``self``, attributes ``def decide(me, ctx)``
    correctly, and never credits a composite's frame that merely holds its inner in a
    local. ``channel`` is ``"sample"`` only for the cap sample of a sub-tick HANDED to
    that node: a frame on the way whose first parameter is a sub-tick (a ``Tick`` at
    ``depth > 0``, or a legacy context over one: ``solver_lib.candidates`` takes the
    tick first), and that sub-tick is a parameter of the node's entry frame (the
    outermost frame with that node as first parameter below its parent's:
    ``solve_tick``). A sub-tick the node built for itself (``restrict(sample_rng=random)``
    inside its own ``solve_tick``) is the node's own ``"direct"`` draw.
    """
    sample_ctx = None
    while frame is not None:
        owner = _first_argument(frame)
        if owner is not None and id(owner) in ids:
            if sample_ctx is not None and not _handed_to(owner, frame, sample_ctx, ids):
                sample_ctx = None
            return ("sample" if sample_ctx is not None else "direct"), ids[id(owner)]
        if sample_ctx is None and isinstance(owner, (Tick, LegacyDecideContext)) and owner.depth != 0:
            sample_ctx = owner
        frame = frame.f_back
    return None


def _handed_to(owner: Any, frame: Any, sample_ctx: Any, ids: Mapping[int, str]) -> bool:
    """Whether ``sample_ctx`` is a parameter of ``owner``'s entry frame (see :func:`_attribute_frame`)."""
    entry = frame
    frame = frame.f_back
    while frame is not None:
        first = _first_argument(frame)
        if first is owner:
            entry = frame
        elif first is not None and id(first) in ids:
            break
        frame = frame.f_back
    return any(arg is sample_ctx for arg in _frame_arguments(entry))


@contextmanager
def _attributed_module_draws(nodes: list[tuple[Any, Any]], record: list[tuple[str, str]]) -> Iterator[None]:
    """Wrap the ``random`` module's draw functions for the block; append ``(channel, node name)`` per call.

    Attribution is :func:`_attribute_frame` over the drawing thread's stack. A thread
    STARTED inside the block (``threading.Thread.start`` is wrapped too, so a
    ``ThreadPoolExecutor`` a node submits to counts) inherits the attribution of the
    frame that started it, so a node's draws in its own thread are its own. A draw
    with no policy frame and no such spawner (a thread that existed before the probe)
    is ``("direct", "")``: provenance cannot be separated.
    """
    import sys
    import threading

    ids = {id(inst): node.name for inst, node in nodes}
    saved = {name: getattr(random, name) for name in _module_draw_names()}
    spawned: dict[int, tuple[str, str]] = {}
    real_start = threading.Thread.start

    def start(thread: threading.Thread) -> None:
        who = _attribute_frame(sys._getframe(1), ids)
        if who is None:
            who = spawned.get(id(threading.current_thread()))
        if who is not None:
            spawned[id(thread)] = who
        real_start(thread)

    def attribute() -> tuple[str, str]:
        who = _attribute_frame(sys._getframe(2), ids)
        if who is None:
            who = spawned.get(id(threading.current_thread()), ("direct", ""))
        return who

    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        def drawn(*args: Any, **kwargs: Any) -> Any:
            record.append(attribute())
            return fn(*args, **kwargs)
        return drawn

    for name, fn in saved.items():
        setattr(random, name, wrap(fn))
    threading.Thread.start = start  # type: ignore[method-assign]
    try:
        yield
    finally:
        threading.Thread.start = real_start  # type: ignore[method-assign]
        for name, fn in saved.items():
            setattr(random, name, fn)


def _probe_module_draws(spec: PolicySpec, arm: Mapping[str, Any], fixture: Any) -> tuple[list[tuple[str, str]], bool]:
    """One runner decision with the sanctioned per-node RNG channel redirected; the module draws left.

    Every built node whose ``rng`` is the random MODULE by default (nothing injected:
    ``Partitioned``'s inner) gets a private ``Random(fixture.seed)`` through
    ``set_rng``, so the inner's sanctioned ``self.rng`` draws leave the module. What
    still reaches the module is either a scope's cap sample (``sample_rng``) or a
    direct draw, each attributed to a node. Returns the draws and whether the module
    state changed; an exception is left to ``robustness``.
    """
    runner = build_runner_for_policy(spec.name, _arm_params(arm))
    nodes = _selection_instances(runner)
    for inst, _node in nodes:
        if getattr(inst, "_rng", object()) is None:
            inst.set_rng(random.Random(fixture.seed))
    record: list[tuple[str, str]] = []
    planning = runner.tree.declaration.planning
    with fixture.module_random():
        before = random.getstate()
        with _attributed_module_draws(nodes, record):
            try:
                lookup = {}
                if planning is not None:  # the planning tick's draws count too
                    _outcome, lookup = _run_plan_tick(runner, fixture, planning)
                runner.run(fixture.context(runner, lookup=lookup))
            except Exception:  # noqa: BLE001 - reported by robustness
                pass
        touched = random.getstate() != before
    return record, touched


def _scoped_module_rng(outcomes: _Outcomes, declared: str) -> tuple[bool, str]:
    """``no_module_rng`` on a selection with a ``module_rng`` declaration: each DRAW is judged by its node.

    The provenance rule (final review MINOR-1, after MAJOR-4):

    - a node's ``module_rng`` covers its OWN code's direct draws, and the cap sample of a
      sub-tick built under it (a ``Tick`` whose ``sample_rng`` the composite set
      to the module: ``Partitioned``'s ``sample_rng=random``) when the node or an
      ancestor of the drawing node declares it;
    - the inner's ``self.rng`` channel is not exempt: it is redirected to a private
      ``Random`` for the probe (:func:`_probe_module_draws`), so a node that declared
      nothing and still reaches the module FAILS, whatever its parent declared
      (``Partitioned[inner=X]`` fails when X draws from the module);
    - provenance that cannot be separated is a SKIP, not a pass: the module changed
      but no wrapped draw was recorded (a function bound at import, ``random._inst``),
      or a draw came from a thread no node of the selection started (no policy frame,
      no spawner: :func:`_attributed_module_draws`). A thread a node starts during the
      decision is that node's.

    Verdicts: any undeclared draw FAILS; else any declared draw (or an unseparable
    change) is SKIP; else (the module untouched with the channels redirected) PASS.
    """
    names = [node.name for _inst, node in _selection_instances(build_runner_for_policy(
        outcomes.spec.name, _arm_params(outcomes.arm)))]
    declaring = {n for n in names if get_policy_spec("planner", n).module_rng is not None}
    rogue: dict[str, list[str]] = {}
    declared_draws = {"direct": 0, "sample": 0}
    unseparable: list[str] = []
    unattributed: list[str] = []
    checked = []
    for make in fx.ROBUSTNESS_FIXTURES:
        if outcomes.get(make).error is not None:
            continue
        fixture = make()
        checked.append(fixture.id)
        record, touched = _probe_module_draws(outcomes.spec, outcomes.arm, fixture)
        for channel, who in record:
            if not who:
                unattributed.append(fixture.id)
                continue
            covered = who in declaring or (
                channel == "sample" and who in names
                and any(n in declaring for n in names[:names.index(who) + 1]))
            if covered:
                declared_draws[channel] += 1
            else:
                rogue.setdefault(who, []).append(fixture.id)
        if touched and not record:
            unseparable.append(fixture.id)
    if rogue:
        shown = "; ".join(f"{who} on {', '.join(sorted(set(fids)))}" for who, fids in sorted(rogue.items()))
        return False, (f"decide drew from the random MODULE outside the module_rng declaration ({declared}): "
                       f"{shown}. A declaration covers the declaring node's own draws and the scope samples "
                       "it routes to the module, not a node that declared nothing; draw only from ctx.rng / "
                       "self.rng, or declare module_rng= on that node")
    note = _own_battery_note(outcomes.spec, outcomes.arm)
    if any(declared_draws.values()) or unseparable or unattributed:
        parts = []
        if declared_draws["direct"]:
            parts.append(f"{declared_draws['direct']} direct draw(s) by the declaring node(s) "
                         f"{', '.join(sorted(declaring))}")
        if declared_draws["sample"]:
            parts.append(f"{declared_draws['sample']} scope cap sample(s) a declaring composite routes to the module")
        if unseparable:
            parts.append(f"the module changed with no attributable draw on {', '.join(unseparable)}: "
                         "provenance cannot be separated")
        if unattributed:
            parts.append(f"{len(unattributed)} draw(s) with no policy frame on the stack and no spawning node "
                         f"(a thread that existed before the decision) on {', '.join(sorted(set(unattributed)))}: "
                         "provenance cannot be separated")
        return _skip(f"module_rng declared by {declared}; {'; '.join(parts)}; no undeclared node drew from it "
                     f"on {len(checked)} whole tick(s), each inner's own RNG redirected{note}")
    return True, (f"random module untouched on {len(checked)} whole tick(s) with each inner's own RNG redirected "
                  f"(module_rng declared by {declared}, but nothing drew on it here){note}")


# --- contract ----------------------------------------------------------------

def _scope_audit(outcomes: _Outcomes) -> tuple[bool, str]:
    """The top's own ``audit_scopes`` over the sub-ticks it built; every committed pair inside one."""
    decisions = [d for d in outcomes.all() if d.error is None]
    audit = next((d.audit for d in decisions if d.audit is not None), None)
    if audit is None:
        badge = next((d.badge for d in decisions if d.badge is not None), outcomes.spec.badge)
        built = sum(len(d.sub_ticks) for d in decisions)
        if badge not in CENTRAL_BADGES:
            return False, (f"{outcomes.spec.name} claims the badge {badge!r} (a decomposition, i.e. information "
                           "containment) but defines no audit_scopes(scope_log, tick): the claim would be "
                           "unverified text. Define audit_scopes, or badge it 'central'")
        if built:
            return False, (f"{outcomes.spec.name} built {built} sub-tick(s) (tick.restrict) but defines no "
                           "audit_scopes(scope_log, tick): a decomposition must let the battery audit its "
                           "scopes (see contract.information_scope for what each one showed)")
        return _skip(f"{outcomes.spec.name} defines no audit_scopes(scope_log, tick): it decides the tick "
                     "without sub-ticks the battery could audit")
    problems: list[str] = []
    scopes = 0
    for d in decisions:
        scopes += len(d.scope_log)
        problems += [f"{d.fixture_id}: {p}" for p in d.audit(list(d.scope_log), d.tick)]
        members = [(set(e["trucks"]), set(e["orders"])) for e in d.scope_log]
        for tid, oid in d.pairs:
            if not any(tid in ts and oid in os_ for ts, os_ in members):
                problems.append(f"{d.fixture_id}: committed pair ({tid}, {oid}) was decided outside every "
                                "sub-tick; a policy that audits its scopes decides each pair inside one")
    if problems:
        shown = "; ".join(problems[:6]) + (f"; ... {len(problems) - 6} more" if len(problems) > 6 else "")
        return False, shown
    return True, f"{scopes} sub-tick(s) audited by {outcomes.spec.name}.audit_scopes, every committed pair inside one"


def _information_scope(outcomes: _Outcomes) -> tuple[bool, str]:
    """FRAMEWORK-MEASURED: what each sub-tick the policy built showed, against the whole tick."""
    decisions = [d for d in outcomes.all() if d.error is None]
    badge = next((d.badge for d in decisions if d.badge is not None), outcomes.spec.badge)
    subs = [(d, e) for d in decisions for e in d.sub_ticks]
    multi = [d for d in decisions if len(d.tick_hauliers) >= 2]
    if not subs:
        if badge not in CENTRAL_BADGES and multi:
            return False, (f"{outcomes.spec.name} claims the badge {badge!r} but built no sub-tick on "
                           f"{len(multi)} multi-company tick(s) ({', '.join(d.fixture_id for d in multi)}): "
                           "every decision saw the whole tick, so the claim is unbacked")
        return _skip(f"no sub-tick built on {len(decisions)} whole tick(s): one central view of every "
                     "haulier, nothing to contain")
    if badge not in CENTRAL_BADGES:
        contradictions = _unsplit_fleets(subs)
        if contradictions:
            shown = "; ".join(contradictions[:4]) + (f"; ... {len(contradictions) - 4} more"
                                                     if len(contradictions) > 4 else "")
            return False, (f"{outcomes.spec.name} claims the badge {badge!r} (a decomposition) but the measured "
                           f"sub-ticks contradict it: {shown}. A decomposition splits the fleet wherever the "
                           "scenario's ceiling separates companies; a sub-tick that holds every truck of such a "
                           "tick shows its inner the whole fleet")
    called = [(d, e) for d, e in subs if e["callee"] is not None]
    widest = max(subs, key=lambda de: (len(de[1]["carriers"] | de[1]["owners"]), de[1]["n_trucks"]))
    d, e = widest
    seen = e["carriers"] | e["owners"]
    multi_carrier = sum(1 for _d, x in subs if len(x["carriers"]) > 1)
    whole = sum(1 for dd, x in subs if len(dd.tick_hauliers) >= 2 and (x["carriers"] | x["owners"]) >= dd.tick_hauliers)
    callees = sorted({x["callee"] for _d, x in called})
    return True, (f"measured {len(subs)} sub-tick(s) on {len({id(dd) for dd, _ in subs})} whole tick(s), "
                  f"{len(called)} solved by an inner ({', '.join(callees) or '-'}); widest: {d.fixture_id} "
                  f"{e['scope']} saw {len(seen)} of the tick's {len(d.tick_hauliers)} haulier(s) "
                  f"({len(e['carriers'])} carrier(s), {len(e['owners'])} owner(s)); {multi_carrier} sub-tick(s) "
                  f"held trucks of more than one carrier; {whole} saw every haulier of a multi-company tick"
                  f" (badge {badge!r})")


def _ceiling_groups(carriers: frozenset[str], profile: Mapping[str, Any]) -> int:
    """How many groups ``carriers`` fall into under the fixture's ceiling (undirected, every arm)."""
    parent = {c: c for c in carriers}

    def root(c: str) -> str:
        while parent[c] != c:
            c = parent[c]
        return c

    for owner, carrier in assign_constraints.declared_partnerships(dict(profile)):
        if owner in parent and carrier in parent:
            parent[root(owner)] = root(carrier)
    return len({root(c) for c in carriers})


def _unsplit_fleets(subs: list[tuple[_Decision, Mapping[str, Any]]]) -> list[str]:
    """The sub-ticks that contradict a decomposition badge (final review round 2, G4).

    MEASURED, badge-text agnostic: on a whole tick whose truck carriers fall into two or
    more groups under the fixture's ceiling (``constraints.declared_partnerships`` over
    every arm, undirected), a sub-tick holding EVERY truck of the tick did not split the
    fleet at all, so the inner it was built for saw every company's trucks. Catches a
    single whole-tick ``restrict`` (the badge liar) and per-company sub-ticks that each
    carry all trucks (the leak). A tick the ceiling joins into one group may be one
    sub-tick (``Partitioned`` on two partners); a split by any other key (a zone) passes,
    because no zone sub-tick holds the whole fleet.
    """
    out: list[str] = []
    groups: dict[int, int] = {}
    for d, e in subs:
        carriers = frozenset(h for h in map(assign_constraints.haulier_of, d.tick.raw_trucks) if h is not None)
        if len(carriers) < 2 or e["n_trucks"] < len(d.tick.raw_trucks):
            continue
        if id(d) not in groups:
            groups[id(d)] = _ceiling_groups(carriers, d.fixture.profile())
        if groups[id(d)] >= 2:
            out.append(f"{d.fixture_id} {e['scope']} held all {e['n_trucks']} trucks of {len(carriers)} carriers "
                       f"in {groups[id(d)]} ceiling groups")
    return out


def _bound_twice(spec: PolicySpec, arm: Mapping[str, Any], profile: Mapping[str, Any]) -> tuple[Any, Any]:
    from apps.container_logistics.policies.dispatch.bind import bind_tree

    tree = build_runner_for_policy(spec.name, _arm_params(arm)).tree
    return (bind_tree(tree.resolved, tree.top.instance, profile),
            bind_tree(tree.resolved, tree.top.instance, profile))


def _declarations(tree: Any) -> list[tuple[str, Any, frozenset[str]]]:
    return [(n.path, n.declaration, n.world_reads) for n in tree.nodes]


def _bind_pure(spec: PolicySpec, arm: Mapping[str, Any]) -> tuple[bool, str]:
    """Binding twice gives equal declarations and reads; no RNG draw, no mutation of the profile."""
    problems: list[str] = []
    fixtures = fx.ROBUSTNESS_FIXTURES
    for make in fixtures:
        fixture = make()
        profile = fixture.profile()
        snapshot = copy.deepcopy(profile)
        with fixture.module_random():
            before = random.getstate()
            first, second = _bound_twice(spec, arm, profile)
            drew = random.getstate() != before
        if _declarations(first) != _declarations(second):
            problems.append(f"{fixture.id}: two binds declared differently")
        if drew:
            problems.append(f"{fixture.id}: bind drew from the random module")
        if profile != snapshot:
            problems.append(f"{fixture.id}: bind mutated the profile")
    if problems:
        return False, "; ".join(problems[:6])
    return True, f"bind pure on {len(fixtures)} fixture(s): equal declarations and reads, no draw, no mutation"


def _world_reads_at_bind(outcomes: _Outcomes) -> tuple[bool, str]:
    """Every world key the solve read was read at bind (the runner's re-bind key)."""
    problems: list[str] = []
    checked = 0
    for d in outcomes.all():
        if d.error is not None:
            continue
        checked += 1
        late = sorted(d.solve_reads - d.bind_reads)
        if late:
            problems.append(f"{d.fixture_id}: read {late} while solving but no bind() read them")
    if problems:
        return False, "; ".join(problems[:6]) + ". A bind() must read every world key its solve_tick reads."
    return True, f"solve reads within bind reads on {checked} whole tick(s)"


def _declaration_within_ceiling(spec: PolicySpec, arm: Mapping[str, Any]) -> tuple[bool, str]:
    """Top ``cross_company`` within the fixture's ceiling; every nested declaration within the top's."""
    problems: list[str] = []
    declared = 0
    for make in fx.ROBUSTNESS_FIXTURES:
        fixture = make()
        profile = fixture.profile()
        try:
            tree, _ = _bound_twice(spec, arm, profile)
        except PolicyError as exc:
            problems.append(f"{fixture.id}: binding refused: {exc}")
            continue
        top = tree.declaration.cross_company
        declared += len(top)
        outside = sorted(top - assign_constraints.declared_partnerships(profile))
        if outside:
            problems.append(f"{fixture.id}: top declared {outside} outside the scenario's ceiling")
        for node in tree.nodes[1:]:
            wider = sorted(node.declaration.cross_company - top)
            if wider:
                problems.append(f"{fixture.id}: {node.path} declared {wider} beyond the top's declaration")
    if problems:
        return False, "; ".join(problems[:6])
    return True, f"{declared} declared (owner, carrier) pair(s) over {len(fx.ROBUSTNESS_FIXTURES)} fixture(s), all within the ceiling"


# --- performance -------------------------------------------------------------

def _performance_scale(spec: PolicySpec, arm: dict[str, Any]) -> tuple[bool, str]:
    fixture = fx.scale()
    budget, source = arm_budget(spec, arm)
    times = []
    for _ in range(_PERF_REPEATS):
        d = _decide_via_runner(spec, arm, fixture)
        if d.error is not None:
            return False, f"decide raised {d.error}"
        times.append(d.decide_ms)
    best = min(times)
    listed = ", ".join(f"{t:.1f}" for t in times)
    verdict = f"best {best:.1f} ms of [{listed}] on {fixture.id}, budget {budget} ms ({source})"
    return best <= budget, verdict


# --- planning ----------------------------------------------------------------

def _planning_declaration(spec: PolicySpec, planning: Mapping[str, Any]) -> tuple[bool, str]:
    """A top that declares planning is registered ``top_only`` (it can be nothing but the top).

    The declaration itself was checked at bind (refusal R2); here: a planning policy that
    is not ``top_only`` would be offered as every composite's ``inner`` arm and the
    ``--solver`` leaf, where message 24 refuses it.
    """
    shown = dict(planning)
    if not spec.top_only:
        return False, (f"{spec.name} declares planning {shown} but is not registered @policy(top_only=True): "
                       "a planning node can only be the top (message 24 refuses it anywhere else), so it "
                       "must not be offered as an inner or as the --solver leaf")
    oracle = " (an ORACLE arm: foresight 'day')" if planning.get("foresight") == "day" else ""
    return True, f"planning {shown}{oracle}; registered top_only"


def _planning_commits_nothing(outcomes: _Outcomes) -> tuple[bool, str]:
    """On every fixture the planning tick (run first, every order as foresight) raised nothing and was not discarded."""
    problems: list[str] = []
    ms: list[float] = []
    for d in outcomes.all():
        if d.plan_error is not None:
            problems.append(f"{d.fixture_id}: the planning tick raised {d.plan_error}")
        elif d.plan_problems:
            problems.append(f"{d.fixture_id}: the planning tick was discarded: {'; '.join(d.plan_problems)}")
        ms.append(d.plan_ms)
    if problems:
        return False, "; ".join(problems[:6])
    return True, (f"{len(ms)} planning tick(s) committed nothing and were not discarded "
                  f"(max {max(ms or [0.0]):.1f} ms)")


def _performance_plan(spec: PolicySpec, arm: dict[str, Any], planning: Mapping[str, Any]) -> tuple[bool, str]:
    """Best of 3 planning-tick solve times on ``scale`` under the declared ``max_hold_seconds``.

    SKIP when the policy's ``requires`` are missing here (the backend that would plan is
    not importable, so the time measured would not be the run's). The dispatch tick after
    it is held to ``performance.scale``'s budget, as for every arm.
    """
    missing = spec.missing_requirements()
    if missing:
        return _skip(f"requires {', '.join(missing)}, not importable here: the planning backend cannot be timed")
    budget_ms = int(planning["max_hold_seconds"]) * 1000
    fixture = fx.scale()
    times = []
    for _ in range(_PERF_REPEATS):
        d = _decide_via_runner(spec, arm, fixture)
        if d.plan_error is not None:
            return False, f"the planning tick raised {d.plan_error}"
        times.append(d.plan_ms)
    best = min(times)
    listed = ", ".join(f"{t:.1f}" for t in times)
    return best <= budget_ms, (f"best {best:.1f} ms of [{listed}] on {fixture.id}, budget {budget_ms} ms "
                               "(the declared max_hold_seconds)")


# --- params ------------------------------------------------------------------

def _defaults_validate(spec: PolicySpec) -> tuple[bool, str]:
    defaults = spec.params_cls.from_mapping(None, where=f"defaults of {spec.name}")
    spec.params_cls.json_schema()
    build_runner_for_policy(spec.name)  # instantiates the whole default tree (a composite's inner too)
    return True, f"defaults {defaults.to_dict()}"


def _rejected(spec: PolicySpec, raw: dict[str, Any]) -> bool:
    try:
        spec.params_cls.from_mapping(raw, where=f"battery for {spec.name}")
    except PolicyError:
        return True
    return False


def _rejects_unknown_key(spec: PolicySpec) -> tuple[bool, str]:
    key = "__battery_unknown_key__"
    return (True, f"{key!r} rejected") if _rejected(spec, {key: 1}) else (False, f"{key!r} was accepted")


def _out_of_bounds_values(info: Any) -> list[tuple[str, Any]]:
    as_int = info.type is int
    out: list[tuple[str, Any]] = []
    m = info.meta
    if m.ge is not None:
        out.append(("ge", math.floor(m.ge) - 1 if as_int else float(m.ge) - 1.0))
    if m.gt is not None:
        out.append(("gt", math.floor(m.gt) if as_int else float(m.gt)))
    if m.le is not None:
        out.append(("le", math.ceil(m.le) + 1 if as_int else float(m.le) + 1.0))
    if m.lt is not None:
        out.append(("lt", math.ceil(m.lt) if as_int else float(m.lt)))
    return out


def _rejects_out_of_bounds(spec: PolicySpec) -> tuple[bool, str]:
    tried, accepted = [], []
    for info in declared_params(spec.params_cls):
        if info.is_ref:
            continue
        for bound, value in _out_of_bounds_values(info):
            tried.append(f"{info.name}={value!r} ({bound})")
            if not _rejected(spec, {info.name: value}):
                accepted.append(f"{info.name}={value!r} violates {bound}= but was accepted")
    if accepted:
        return False, "; ".join(accepted)
    return True, ("rejected " + ", ".join(tried)) if tried else "no bounded params"


def _rejects_bad_choice(spec: PolicySpec) -> tuple[bool, str]:
    tried, accepted = [], []
    for info in declared_params(spec.params_cls):
        if info.is_ref or info.meta.choices is None:
            continue
        value = "__battery_not_a_choice__"
        tried.append(info.name)
        if not _rejected(spec, {info.name: value}):
            accepted.append(f"{info.name}={value!r} was accepted")
    if accepted:
        return False, "; ".join(accepted)
    return True, ("rejected a bad choice for " + ", ".join(tried)) if tried else "no choice params"


def _composite_targets() -> list[str]:
    return [s.name for s in list_policies("planner") if s.is_composite]


def _top_only_targets() -> list[str]:
    return [s.name for s in list_policies("planner") if s.top_only]


def _rejects_bad_ref(spec: PolicySpec) -> tuple[bool, str]:
    """Every ref param refuses an unregistered target, a ``top_only`` policy and, where ``leaf_only``, a decomposition."""
    tried, accepted = [], []
    for info in declared_params(spec.params_cls):
        if not info.is_ref:
            continue
        bad = [{"type": _BAD_REF}]
        if info.meta.kind == "planner" and info.meta.leaf_only:
            bad += [{"type": name} for name in _composite_targets()]
        if info.meta.kind == "planner":  # R5: a top_only policy is never a ref target
            bad += [{"type": name} for name in _top_only_targets() if {"type": name} not in bad]
        for value in bad:
            tried.append(f"{info.name}={value['type']}")
            if not _rejected(spec, {info.name: value}):
                accepted.append(f"{info.name}={value['type']!r} was accepted")
        # Sanity: the declared kind really resolves its own default (no vacuous refusals).
        ref_target(info.meta.kind, info.default.type, where=f"battery for {spec.name}.{info.name}",
                   leaf_only=info.meta.leaf_only)
    if accepted:
        return False, "; ".join(accepted)
    return True, ("rejected " + ", ".join(tried)) if tried else "no policy-reference params"


def _schema_keywords_supported(spec: PolicySpec) -> tuple[bool, str]:
    schema = spec.params_cls.json_schema()
    used = set(schema)
    for prop in schema.get("properties", {}).values():
        used |= set(prop)
    extra = sorted(used - SUPPORTED_SCHEMA_KEYWORDS)
    if extra:
        return False, f"unsupported schema keywords {extra}; supported: {sorted(SUPPORTED_SCHEMA_KEYWORDS)}"
    return True, f"keywords {sorted(used)}"
