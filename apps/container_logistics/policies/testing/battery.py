"""The policy contract battery: ``run_battery(slot, name) -> list[CheckResult]``.

Single responsibility: decide, for one registered dispatch policy, whether it honours
the dispatch contract on WHOLE TICKS (round 2, ``docs/plans/policy_slots_round2_whole_tick.md``
§5 C5; plan ``docs/plans/policy_slots_p0_p3.md`` §4 P3.2). Every decision is one tick
(``fixtures.Fixture.context`` -> ``DispatchRunner.tick_context``) through
``DispatchRunner.run``, so what passes here is what the sim executes, leaf or
decomposition alike. The ids for the DEFAULT arm are :data:`DISPATCH_CHECKS`, in order:

- ``structure`` -- on every fixture: the tick plan passes ``invariants.check_plan``
  (on the tick scope its feasibility is the WORLD rule, so a cross-company head the
  structure does not permit fails ``only_allowed_pairs``). Each verdict is derived
  from ``check_plan``'s own messages (:func:`classify_contract_problem`);
  ``Plan.from_pairs``' "truck twice" violation is ``no_truck_twice``.
- ``robustness`` -- one per fixture: ``decide`` does not raise anything but a
  contract violation (reported once, under ``structure``).
- ``determinism`` -- ``reproducible``: two fresh instances on the same tick give the
  same plan; ``order_independent``: 20 shuffles of trucks and orders give one plan;
  ``no_module_rng``: ``decide`` leaves the ``random`` module's state untouched. The
  last two are skipped (ok, with the reason) when ANY node of the arm's selection
  declares ``order_sensitive`` / ``module_rng``: the declarations compose.
- ``information`` -- ``scopes_respect_model`` (per arm): the sub-scopes the policy
  built (``ctx.scope_log``, recorded only while auditing) respect its declared
  model: ``per_company`` -- every sub-scope's trucks belong to one company and every
  order is that company's or a world partner's; ``per_group`` -- every sub-scope lies
  inside one cooperation group; and for both, every committed head lies inside one
  sub-scope (a non-central policy that commits a head it decided outside any scope
  decided centrally). ``central`` has no sub-scope restriction. This certifies the
  ``compose``/``scopes`` helpers and the declared shape; it cannot prove a policy
  never peeks at ``ctx`` beyond them. ``model_declared`` (default arm only): the
  registration's model is one of the three and is what the resolved selection carries.
- ``performance`` -- ``scale`` (per arm): best of 3 decide times on the 1000 x 500
  ``scale`` tick under ``spec.arm_budgets_ms.get(arm, spec.perf_budget_ms)``. A
  budget is a MEASURED property of an arm (``max(3 x worst of 2 x 5 runs, 500)``,
  numbers at the decorator). An unmeasured arm inherits the default arm's budget,
  so it fails loudly when slower, which forces a measurement. NOTE: a budget above
  ``STEP_SETTLE_SECONDS`` (2000 ms; ``scenario/runtime_settings.py``
  ``LONG_RUN_STEP_SETTLE_SECONDS``) means
  that arm makes the assignment agent a per-step straggler at 1000 trucks x 500
  orders: every such tick the scheduler waits for it (CLAUDE.md §6.17b).
- ``params`` -- defaults validate (the whole default tree builds); unknown keys,
  out-of-bounds values, bad choices and bad policy references (an unregistered
  target; a decomposition where ``leaf_only``) are rejected; the schema uses only
  ``SUPPORTED_SCHEMA_KEYWORDS``.

**Arms** (:func:`policy_arms`): the defaults; each non-default ``choices`` value of
each of the policy's OWN ``str`` params; and for each policy-reference param, every
other AVAILABLE target of its kind (``requires`` installed; a leaf where
``leaf_only``) at that target's defaults. Arms are not crossed and not recursive (an
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

import logging
import math
import random
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping

from apps.container_logistics.assignment import constraints as assign_constraints
from apps.container_logistics.policies.dispatch import compose as _compose_module
from apps.container_logistics.policies.dispatch import runner as _runner_module
from apps.container_logistics.policies.dispatch.context import DispatchContext
from apps.container_logistics.policies.dispatch.invariants import check_plan
from apps.container_logistics.policies.dispatch.plan import Plan
from apps.container_logistics.policies.dispatch.runner import DispatchRunner, build_runner_for_policy
from apps.container_logistics.policies.framework.components import ref_names, ref_target
from apps.container_logistics.policies.framework.errors import PolicyError, SlotNotImplementedError
from apps.container_logistics.policies.framework.params import SUPPORTED_SCHEMA_KEYWORDS, declared_params
from apps.container_logistics.policies.framework.registry import PolicySpec, get_policy_spec, list_policies

from . import fixtures as fx
from .fixtures import seeded_module_random

logger = logging.getLogger(__name__)

__all__ = [
    "CheckResult",
    "DISPATCH_CHECKS",
    "ARM_CHECKS",
    "STRUCTURE_CHECKS",
    "INFORMATION_MODELS",
    "arm_label",
    "battery_check_ids",
    "policy_arms",
    "classify_contract_problem",
    "run_battery",
    "decide_seeded",
    "seeded_module_random",
]

INFORMATION_MODELS: tuple[str, ...] = ("central", "per_group", "per_company")

STRUCTURE_CHECKS: tuple[str, ...] = (
    "returns_plan", "no_truck_twice", "no_order_twice", "only_in_scope", "only_allowed_pairs",
)

ARM_CHECKS: tuple[str, ...] = (
    *(f"structure.{c}" for c in STRUCTURE_CHECKS),
    *(f"robustness.{make.__name__}" for make in fx.ROBUSTNESS_FIXTURES),
    "determinism.reproducible",
    "determinism.order_independent",
    "determinism.no_module_rng",
    "information.scopes_respect_model",
    "performance.scale",
)

DISPATCH_CHECKS: tuple[str, ...] = (
    *ARM_CHECKS,
    "information.model_declared",
    "params.defaults_validate",
    "params.rejects_unknown_key",
    "params.rejects_out_of_bounds",
    "params.rejects_bad_choice",
    "params.rejects_bad_ref",
    "params.schema_keywords_supported",
)

# check_plan's message kinds (invariants.py), each keyed on its stable wording, and
# the structure check it fails. tests/test_policy_battery_structure_via_check_plan.py
# asserts this table covers every message check_plan can produce.
_PLAN_MESSAGE_KINDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("returns_plan", re.compile(r"^decide\(\) returned .+, expected Plan$")),
    ("only_in_scope", re.compile(r"^truck .+ is not in scope ")),
    ("returns_plan", re.compile(r"^chain for truck .+ is empty or not a tuple: ")),
    ("no_order_twice", re.compile(r"^order .+ appears twice in the plan$")),
    ("only_in_scope", re.compile(r"^order .+ is not in scope ")),
    ("only_allowed_pairs", re.compile(r"^head pair \(.+\) is not allowed$")),
)
# Raised by Plan.from_pairs inside decide (a pair list cannot repeat a truck legally).
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
    """

    group: str
    check: str
    ok: bool
    detail: str
    ms: float
    arm: str = ""

    @property
    def id(self) -> str:
        return f"{self.group}.{self.check}"


def decide_seeded(
    runner: DispatchRunner,
    fixture: Any,
    *,
    trucks: list[dict[str, Any]] | None = None,
    orders: list[dict[str, Any]] | None = None,
) -> tuple[DispatchContext, Any]:
    """``runner.policy.decide`` on a fresh whole-tick fixture context, module RNG seeded.

    The policy's raw output, NOT the runner path (no ``check_plan``): this is what the
    policy golden captures. Nothing is injected (the context is the randomness input);
    the ``random`` module is seeded with ``fixture.seed`` for the call. Exceptions
    propagate.
    """
    with fixture.module_random():
        ctx = fixture.context(runner, trucks=trucks, orders=orders)
        return ctx, runner.policy.decide(ctx)


# --- arms --------------------------------------------------------------------

def _ref_arm_targets(info: Any) -> list[str]:
    """Every other available target of ``info``'s kind (a leaf where ``leaf_only``), sorted."""
    kind, default = info.meta.kind, getattr(info.default, "type", None)
    names = []
    if kind == "dispatch":
        for spec in list_policies("dispatch"):
            if spec.missing_requirements() or (info.meta.leaf_only and spec.is_composite):
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


def _arm_budget(spec: PolicySpec, arm: Mapping[str, Any]) -> tuple[int, str]:
    """The arm's budget and where it comes from (measured key, or the default arm's)."""
    label = arm_label(arm)
    if not label:
        return spec.perf_budget_ms, "perf_budget_ms"
    if label in spec.arm_budgets_ms:
        return spec.arm_budgets_ms[label], f"arm_budgets_ms[{label!r}]"
    return spec.perf_budget_ms, "perf_budget_ms (arm not measured: it inherits the default arm's budget)"


def battery_check_ids(slot: str, name: str) -> list[str]:
    """Every id ``run_battery(slot, name)`` returns, in order.

    ``DISPATCH_CHECKS`` (the default arm), then ``ARM_CHECKS`` once per non-default
    arm with ``[<arm>]`` appended. Raises like :func:`run_battery`.
    """
    spec = _dispatch_spec(slot, name)
    return list(DISPATCH_CHECKS) + [
        f"{cid}{_arm_suffix(arm)}" for arm in policy_arms(spec)[1:] for cid in ARM_CHECKS
    ]


def classify_contract_problem(message: str, *, raised: bool = False) -> str:
    """The structure check (an entry of ``STRUCTURE_CHECKS``) a contract message fails.

    ``raised`` marks a ``DispatchContractViolation`` raised inside ``decide`` (e.g.
    ``Plan.from_pairs``); otherwise ``message`` is one of ``check_plan``'s. An
    unknown message maps to ``returns_plan``, so it still fails.
    """
    for check, pattern in (_RAISED_MESSAGE_KINDS if raised else _PLAN_MESSAGE_KINDS):
        if pattern.search(message):
            return check
    return _UNCLASSIFIED


def run_battery(slot: str, name: str) -> list[CheckResult]:
    """Run every check in :func:`battery_check_ids` on policy ``name`` of ``slot``.

    Raises ``UnknownPolicyError`` for an unregistered name and
    ``SlotNotImplementedError`` for a slot without a battery (only ``dispatch`` has one).
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
    if slot != "dispatch":
        raise SlotNotImplementedError(f"no contract battery for slot {slot!r}; only 'dispatch' has one")
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
    checks["information.scopes_respect_model"] = _bind(_scopes_respect_model, outcomes)
    checks["performance.scale"] = _bind(_performance_scale, spec, arm)
    return [_timed(cid, checks[cid], arm) for cid in ARM_CHECKS]


def _default_only_results(spec: PolicySpec) -> list[CheckResult]:
    checks: dict[str, Callable[[], tuple[bool, str]]] = {
        "information.model_declared": _bind(_model_declared, spec),
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
    try:
        ok, detail = fn()
    except Exception as exc:  # noqa: BLE001 - a crashing check is a FAIL, never a crash of the battery
        ok, detail = False, f"raised {type(exc).__name__}: {exc}"
    return CheckResult(group=group, check=f"{check}{_arm_suffix(arm)}", ok=ok, detail=detail,
                       ms=round((time.perf_counter() - start) * 1000.0, 3), arm=arm_label(arm))


# --- the runner path ---------------------------------------------------------

class _ViolationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not str(record.msg).startswith("DISPATCH POLICY CONTRACT VIOLATION")


@contextmanager
def _quiet_runner_violations() -> Iterator[None]:
    """Drop the CONTRACT VIOLATION ERROR lines for the block (the battery reports them).

    The runner logs a tick violation (message 18); a sub-scope violation (message 19)
    is logged by ``compose``.
    """
    flt = _ViolationFilter()
    loggers = (_runner_module.logger, _compose_module.logger)
    for lg in loggers:
        lg.addFilter(flt)
    try:
        yield
    finally:
        for lg in loggers:
            lg.removeFilter(flt)


class _PlanRecorder:
    """Capture hook: the raw TICK plan and any violation ``decide`` raised (``DispatchRunner.run``).

    Sub-scope calls (``compose.decide_in_scope`` reuses the hook) are ignored.
    """

    def __init__(self) -> None:
        self.plan: Any = None
        self.raised: list[str] = []

    def __call__(self, *, ctx: DispatchContext, plan: Any, problems: list[str]) -> None:
        if ctx.scope.kind != "tick":
            return
        self.plan = plan
        self.raised = list(problems)


@dataclass(frozen=True)
class _Decision:
    """One ``DispatchRunner.run`` on one whole-tick fixture: what came back and what it touched."""

    fixture: Any
    n_trucks: int
    plan: Any
    pairs: tuple[tuple[str, str], ...]          # committed heads (truck id, order id)
    problems: tuple[tuple[str, str], ...]       # (structure check, message)
    error: str | None                           # a non-contract exception, as text
    decide_ms: float
    module_rng_touched: bool
    scope_log: tuple[Mapping[str, Any], ...]

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
    structure problems are ``check_plan``'s against the tick context (or the violation
    ``decide`` raised), classified per check; a runner verdict that disagrees with
    them is a problem too. The sub-scopes the policy builds are recorded
    (``scope_log``) for the information audit.
    """
    recorder = _PlanRecorder()
    runner = build_runner_for_policy(spec.name, _arm_params(arm), capture=recorder)
    n_trucks = len(fixture.trucks if trucks is None else trucks)
    error: str | None = None
    scope_log: list[dict[str, Any]] = []
    pairs: list[tuple[str, str]] = []
    with fixture.module_random():
        before = random.getstate()
        ctx = fixture.context(runner, trucks=trucks, orders=orders, scope_log=scope_log)
        try:
            pairs = [(str(t.get("_id")), str(o.get("_id"))) for t, o in runner.run(ctx).pairs]
        except Exception as exc:  # noqa: BLE001 - reported by robustness
            error = f"{type(exc).__name__}: {exc}"
        touched = random.getstate() != before
    problems: list[tuple[str, str]] = []
    if error is None:
        if recorder.raised:
            problems = [(classify_contract_problem(m, raised=True), m) for m in recorder.raised]
        else:
            problems = [(classify_contract_problem(m), m) for m in check_plan(recorder.plan, ctx)]
        discarded = runner.stats["violations"] > 0
        if discarded != bool(problems):
            problems.append((_UNCLASSIFIED, f"the runner {'discarded' if discarded else 'committed'} the plan "
                                            f"but check_plan found {len(problems)} problem(s)"))
    return _Decision(
        fixture=fixture,
        n_trucks=n_trucks,
        plan=recorder.plan,
        pairs=tuple(pairs),
        problems=tuple(problems),
        error=error,
        decide_ms=runner.stats["total_ms"],
        module_rng_touched=touched,
        scope_log=tuple(scope_log),
    )


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
        elif d.error is not None or not isinstance(d.plan, Plan):
            skipped.append(d.fixture_id)
    if problems:
        return False, "; ".join(problems)
    note = (f" (not checked on {', '.join(skipped)}: no Plan to check, see robustness.* "
            "and the other structure.* checks)") if skipped else ""
    return True, f"check_plan clean on {len(outcomes.all()) - len(skipped)} whole tick(s){note}"


# --- robustness --------------------------------------------------------------

def _robustness_check(outcomes: _Outcomes, make: Callable[[], Any]) -> tuple[bool, str]:
    d = outcomes.get(make)
    timing = f" in {d.decide_ms:.1f} ms"
    if d.error is not None:
        return False, f"decide raised {d.error}"
    if d.problems:
        return True, f"plan discarded by the runner{timing}: contract problems, reported under structure.*"
    return True, f"{len(d.plan.heads())} head(s) over {d.n_trucks} trucks{timing}"


# --- determinism -------------------------------------------------------------

def _determinism_fixtures() -> list[Any]:
    return [fx.tied_costs(), fx.three_company_chain(), fx.scale(n_trucks=30, n_orders=20, hauliers=2, seed=9)]


def _plan_items(d: _Decision) -> Any:
    if d.error is not None:
        return ("raised", d.error)
    return list(d.plan.chains.items()) if isinstance(d.plan, Plan) else d.plan


def _plan_mapping(d: _Decision) -> Any:
    """Chains as a dict (commit order ignored), or the non-plan outcome itself."""
    return dict(d.plan.chains) if d.error is None and isinstance(d.plan, Plan) else _plan_items(d)


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
        return True, f"skipped: order_sensitive declared by {declared}"
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
        reason = getattr(get_policy_spec("dispatch", node.name), attr)
        if reason is not None:
            reasons.append(f"{node.name}: {reason}")
        node = node.inner
    return "; ".join(reasons)


def _no_module_rng(outcomes: _Outcomes) -> tuple[bool, str]:
    """``DispatchRunner.run`` leaves ``random.getstate()`` alone on every whole tick.

    Nothing is injected: the tick context's ``ctx.rng`` (``Random(tick_seed)``) is the
    only sanctioned randomness, and tick-scope sampling draws from it, so every
    fixture (``scale`` included) is checked. Skipped when a node of the selection
    declares ``module_rng`` (``Partitioned``: the pre-v2 groups draw from the module).
    """
    declared = _declared_on_selection(outcomes.spec, outcomes.arm, "module_rng")
    if declared:
        return True, f"skipped: module_rng declared by {declared}"
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


# --- information -------------------------------------------------------------

def _model_declared(spec: PolicySpec) -> tuple[bool, str]:
    model = spec.information_model
    if model not in INFORMATION_MODELS:
        return False, f"information_model {model!r} is not one of {list(INFORMATION_MODELS)}"
    resolved = build_runner_for_policy(spec.name).resolved.information_model
    if resolved != model:
        return False, f"the resolved selection carries information_model {resolved!r}, the registration {model!r}"
    return True, f"information_model {model!r} (stamped as meta.policies.dispatch.information_model)"


def _group_key(world: Any, hid: Any) -> Any:
    if hid is None:
        return None
    return ("component", world.component_of[hid]) if hid in world.component_of else ("haulier", hid)


def _scope_problems(model: str, entry: Mapping[str, Any], world: Any,
                    truck_h: Mapping[str, Any], order_h: Mapping[str, Any]) -> list[str]:
    where = f"scope {entry['key']}"
    t_h = [truck_h.get(t) for t in entry["trucks"]]
    o_h = [order_h.get(o) for o in entry["orders"]]
    if model == "per_company":
        hid = entry["key"].partition(":")[2] if entry["kind"] == "company" else (t_h[0] if t_h else None)
        out = [f"{where} (company {hid}) holds truck {t} of {h}" for t, h in zip(entry["trucks"], t_h) if h != hid]
        visible = {hid} | set(world.partners(hid)) if hid is not None else set()
        out += [f"{where} (company {hid}) sees order {o} of {h}, neither its own nor a partner's"
                for o, h in zip(entry["orders"], o_h) if h not in visible]
        return out
    keys = {_group_key(world, h) for h in t_h + o_h}
    if len(keys) > 1 or None in keys:
        return [f"{where} spans cooperation groups {sorted(map(str, keys))}"]
    return []


def _scopes_respect_model(outcomes: _Outcomes) -> tuple[bool, str]:
    """Audit the sub-scopes the policy built against its declared information model."""
    model = build_runner_for_policy(outcomes.spec.name, _arm_params(outcomes.arm)).resolved.information_model
    if model == "central":
        return True, "central: decides the whole tick; no sub-scope restriction to audit"
    if model not in INFORMATION_MODELS:
        return False, f"information_model {model!r} is not one of {list(INFORMATION_MODELS)}"
    problems: list[str] = []
    scopes = 0
    for d in outcomes.all():
        if d.error is not None:
            continue
        world = d.fixture.world()
        truck_h = {str(t.get("_id")): assign_constraints.haulier_of(t) for t in d.fixture.trucks}
        order_h = {str(o.get("_id")): assign_constraints.haulier_of(o) for o in d.fixture.orders}
        members: list[tuple[set[str], set[str]]] = []
        for entry in d.scope_log:
            scopes += 1
            problems += [f"{d.fixture_id}: {p}" for p in _scope_problems(model, entry, world, truck_h, order_h)]
            members.append((set(entry["trucks"]), set(entry["orders"])))
        for tid, oid in d.pairs:
            if not any(tid in ts and oid in os_ for ts, os_ in members):
                problems.append(f"{d.fixture_id}: committed head ({tid}, {oid}) was decided outside every "
                                f"sub-scope; a {model} policy decides each head inside one scope")
    if problems:
        shown = "; ".join(problems[:6]) + (f"; ... {len(problems) - 6} more" if len(problems) > 6 else "")
        return False, f"{model}: {shown}"
    return True, f"{model}: {scopes} sub-scope(s) audited (scope_log), every committed head inside one"


# --- performance -------------------------------------------------------------

def _performance_scale(spec: PolicySpec, arm: dict[str, Any]) -> tuple[bool, str]:
    fixture = fx.scale()
    budget, source = _arm_budget(spec, arm)
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
    return [s.name for s in list_policies("dispatch") if s.is_composite]


def _rejects_bad_ref(spec: PolicySpec) -> tuple[bool, str]:
    """Every ref param refuses an unregistered target and, where ``leaf_only``, a decomposition."""
    tried, accepted = [], []
    for info in declared_params(spec.params_cls):
        if not info.is_ref:
            continue
        bad = [{"type": _BAD_REF}]
        if info.meta.kind == "dispatch" and info.meta.leaf_only:
            bad += [{"type": name} for name in _composite_targets()]
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
