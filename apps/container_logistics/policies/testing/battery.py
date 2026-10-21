"""The policy contract battery: ``run_battery(slot, name) -> list[CheckResult]``.

Single responsibility: decide, for one registered policy, whether it honours the
dispatch contract (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P3.2). Five groups;
the ids for the DEFAULT arm are :data:`DISPATCH_CHECKS`, in that order:

- ``structure`` -- on every fixture: the plan passes ``invariants.check_plan``.
  Each verdict is derived from ``check_plan``'s own messages
  (:func:`classify_contract_problem`), so a new invariant is battery-checked the day
  it lands; ``Plan.from_pairs``' "truck twice" violation is ``no_truck_twice``.
- ``robustness`` -- one per fixture: ``decide`` does not raise anything but a
  contract violation. Contract breaches are reported once, under ``structure``, so
  one bug fails one check.
- ``determinism`` -- ``reproducible``: two fresh instances with the same
  ``Random(seed)`` and tiebreak give the same plan; ``order_independent``: with a
  tiebreak seed, 20 shuffles of trucks and orders give one plan (skipped, ok, when
  the policy declares ``order_sensitive``); ``no_module_rng``: with an injected RNG,
  ``decide`` leaves the ``random`` module's state untouched.
- ``performance`` -- ``scale``: best of 3 decide times under ``spec.perf_budget_ms``.
- ``params`` -- defaults validate; unknown keys, out-of-bounds values and bad
  choices are rejected; the schema uses only ``SUPPORTED_SCHEMA_KEYWORDS``.

**Arms.** Every non-default ``choices`` value of every declared ``str`` param is an
arm (other params at defaults). The :data:`ARM_CHECKS` (structure, robustness,
determinism) run again per arm, with the arm in the id:
``structure.no_order_twice[candidates=sampled]``. Performance gates the default arm
only, because ``perf_budget_ms`` is measured on it; each arm's ``robustness.scale``
detail reports its decide time. :func:`battery_check_ids` lists every id for a policy.

**The runner path.** Every decision goes through ``DispatchRunner.run`` (the capture
hook records the raw plan), so what passes here is what the sim executes: the
runner's own violation verdict is cross-checked against ``check_plan``, and its
decide timer is the performance measurement. The runner's CONTRACT VIOLATION log
line is silenced while the battery runs (the battery reports it as a FAIL instead).

The global ``random`` state is seeded per decide and restored afterwards, because
the partition scope samples from the module (``scopes.partition_context``).

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

from apps.container_logistics.policies.dispatch import runner as _runner_module
from apps.container_logistics.policies.dispatch.context import DispatchContext
from apps.container_logistics.policies.dispatch.invariants import check_plan
from apps.container_logistics.policies.dispatch.plan import Plan
from apps.container_logistics.policies.dispatch.runner import DispatchRunner, build_runner_for_policy
from apps.container_logistics.policies.framework.errors import (
    PolicyParamsError,
    SlotNotImplementedError,
)
from apps.container_logistics.policies.framework.params import SUPPORTED_SCHEMA_KEYWORDS, declared_params
from apps.container_logistics.policies.framework.registry import PolicySpec, get_policy_spec

from . import fixtures as fx

logger = logging.getLogger(__name__)

__all__ = [
    "CheckResult",
    "DISPATCH_CHECKS",
    "ARM_CHECKS",
    "STRUCTURE_CHECKS",
    "battery_check_ids",
    "policy_arms",
    "classify_contract_problem",
    "run_battery",
    "decide_seeded",
    "seeded_module_random",
]

STRUCTURE_CHECKS: tuple[str, ...] = (
    "returns_plan", "no_truck_twice", "no_order_twice", "only_in_scope", "only_allowed_pairs",
)

ARM_CHECKS: tuple[str, ...] = (
    *(f"structure.{c}" for c in STRUCTURE_CHECKS),
    *(f"robustness.{make.__name__}" for make in fx.ROBUSTNESS_FIXTURES),
    "determinism.reproducible",
    "determinism.order_independent",
    "determinism.no_module_rng",
)

DISPATCH_CHECKS: tuple[str, ...] = (
    *ARM_CHECKS,
    "performance.scale",
    "params.defaults_validate",
    "params.rejects_unknown_key",
    "params.rejects_out_of_bounds",
    "params.rejects_bad_choice",
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

_SEED = 7
_TIEBREAK = 4242
_SHUFFLES = 20
_PERF_REPEATS = 3


@dataclass(frozen=True)
class CheckResult:
    """One check's verdict. ``id`` is ``<group>.<check>``; ``check`` carries the arm.

    ``arm`` is ``""`` for the default params, else ``"<param>=<value>"``, and then
    ``check`` ends in ``[<arm>]`` (e.g. ``no_order_twice[candidates=sampled]``).
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


@contextmanager
def seeded_module_random(seed: int) -> Iterator[None]:
    """Seed the global ``random`` state for the block, then put the previous state back."""
    saved = random.getstate()
    random.seed(seed)
    try:
        yield
    finally:
        random.setstate(saved)


def decide_seeded(
    runner: DispatchRunner,
    fixture: Any,
    *,
    seed: int,
    tiebreak: int | None,
    trucks: list[dict[str, Any]] | None = None,
    orders: list[dict[str, Any]] | None = None,
) -> tuple[DispatchContext, Any]:
    """``runner.policy.decide`` on a fresh fixture context with ``Random(seed)`` injected.

    The policy's raw output, NOT the runner path (no ``check_plan``): this is what
    the policy golden captures. The module RNG is seeded with ``seed`` for the call.
    Exceptions propagate.
    """
    runner.policy.set_rng(random.Random(seed))
    runner.policy.set_tiebreak(tiebreak)
    with seeded_module_random(seed):
        ctx = fixture.context(runner, trucks=trucks, orders=orders)
        return ctx, runner.policy.decide(ctx)


def policy_arms(spec: PolicySpec) -> list[dict[str, Any]]:
    """``[{}]`` (the defaults) plus ``{param: choice}`` per non-default choice of each ``str`` param."""
    arms: list[dict[str, Any]] = [{}]
    for info in declared_params(spec.params_cls):
        if info.type is not str or info.meta.choices is None:
            continue
        arms += [{info.name: c} for c in info.meta.choices if c != info.default]
    return arms


def _arm_label(arm: Mapping[str, Any]) -> str:
    return ",".join(f"{k}={v}" for k, v in arm.items())


def _arm_suffix(arm: Mapping[str, Any]) -> str:
    return f"[{_arm_label(arm)}]" if arm else ""


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
    return [_timed(cid, checks[cid], arm) for cid in ARM_CHECKS]


def _default_only_results(spec: PolicySpec) -> list[CheckResult]:
    checks: dict[str, Callable[[], tuple[bool, str]]] = {
        "performance.scale": _bind(_performance_scale, spec),
        "params.defaults_validate": _bind(_defaults_validate, spec),
        "params.rejects_unknown_key": _bind(_rejects_unknown_key, spec),
        "params.rejects_out_of_bounds": _bind(_rejects_out_of_bounds, spec),
        "params.rejects_bad_choice": _bind(_rejects_bad_choice, spec),
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
                       ms=round((time.perf_counter() - start) * 1000.0, 3), arm=_arm_label(arm))


# --- the runner path ---------------------------------------------------------

class _ViolationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not str(record.msg).startswith("DISPATCH POLICY CONTRACT VIOLATION")


@contextmanager
def _quiet_runner_violations() -> Iterator[None]:
    """Drop the runner's CONTRACT VIOLATION ERROR line for the block (the battery reports it)."""
    flt = _ViolationFilter()
    _runner_module.logger.addFilter(flt)
    try:
        yield
    finally:
        _runner_module.logger.removeFilter(flt)


class _PlanRecorder:
    """Capture hook: the raw plan and any violation ``decide`` raised (``DispatchRunner.run``)."""

    def __init__(self) -> None:
        self.plan: Any = None
        self.raised: list[str] = []

    def __call__(self, *, ctx: DispatchContext, plan: Any, problems: list[str]) -> None:
        self.plan = plan
        self.raised = list(problems)


@dataclass(frozen=True)
class _Decision:
    """One ``DispatchRunner.run`` on one fixture: what came back and what it touched."""

    fixture_id: str
    n_trucks: int
    plan: Any
    problems: tuple[tuple[str, str], ...]   # (structure check, message)
    error: str | None                       # a non-contract exception, as text
    decide_ms: float
    module_rng_touched: bool
    cap_binds: bool


def _decide_via_runner(
    spec: PolicySpec,
    arm: Mapping[str, Any],
    fixture: Any,
    *,
    seed: int,
    tiebreak: int | None,
    trucks: list[dict[str, Any]] | None = None,
    orders: list[dict[str, Any]] | None = None,
) -> _Decision:
    """``DispatchRunner.run`` on a fresh ``spec`` instance with params ``arm``.

    ``Random(seed)`` is injected and the module RNG seeded with ``seed``. The
    structure problems are ``check_plan``'s (or the violation ``decide`` raised),
    classified per check; a runner verdict that disagrees with them is a problem too.
    """
    recorder = _PlanRecorder()
    runner = build_runner_for_policy(spec.name, dict(arm), capture=recorder)
    runner.policy.set_rng(random.Random(seed))
    runner.policy.set_tiebreak(tiebreak)
    n_trucks = len(fixture.trucks if trucks is None else trucks)
    error: str | None = None
    with seeded_module_random(seed):
        before = random.getstate()
        ctx = fixture.context(runner, trucks=trucks, orders=orders)
        try:
            runner.run(ctx)
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
        fixture_id=fixture.id,
        n_trucks=n_trucks,
        plan=recorder.plan,
        problems=tuple(problems),
        error=error,
        decide_ms=runner.stats["total_ms"],
        module_rng_touched=touched,
        cap_binds=n_trucks > fixture.settings.max_trucks_per_scope,
    )


class _Outcomes:
    """One runner decision per ``ROBUSTNESS_FIXTURES`` fixture for one arm, made on first use."""

    def __init__(self, spec: PolicySpec, arm: dict[str, Any]) -> None:
        self._spec, self._arm = spec, arm
        self._by_fixture: dict[str, _Decision] = {}

    def get(self, make: Callable[[], Any]) -> _Decision:
        key = make.__name__
        if key not in self._by_fixture:
            fixture = make()
            self._by_fixture[key] = _decide_via_runner(self._spec, self._arm, fixture,
                                                       seed=fixture.seed, tiebreak=None)
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
    return True, f"check_plan clean on {len(outcomes.all()) - len(skipped)} fixture(s){note}"


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
    return [fx.tied_costs(), fx.scale(n_trucks=30, n_orders=20, hauliers=2, seed=9)]


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
        for tiebreak in (None, _TIEBREAK):
            first = _decide_via_runner(spec, arm, fixture, seed=_SEED, tiebreak=tiebreak)
            second = _decide_via_runner(spec, arm, fixture, seed=_SEED, tiebreak=tiebreak)
            if _plan_items(first) != _plan_items(second):
                return False, (f"{fixture.id} (tiebreak={tiebreak}): two fresh instances with Random({_SEED}) "
                               f"gave different plans: {_plan_items(first)[:4]} vs {_plan_items(second)[:4]}")
    return True, f"same plan twice on {', '.join(f.id for f in fixtures)}, with and without a tiebreak"


def _order_independent(spec: PolicySpec, arm: dict[str, Any]) -> tuple[bool, str]:
    """Shuffling trucks and orders (tiebreak set) must not change the plan.

    Narrower than it reads: the fixtures keep fewer trucks than the spatial
    per-order cap (``spatial_candidates_per_order``, default 40), because
    ``spatial.iter_candidate_pairs`` truncates candidates in INPUT order. On a dense
    real tick where that cap binds, the framework's candidate set itself depends on
    input order, so no policy is order-independent there.
    """
    scope_note = ("holds only while the spatial per-order candidate cap (spatial_candidates_per_order, "
                  "default 40) does not bind: spatial.iter_candidate_pairs truncates in input order, so on "
                  "dense real ticks the candidate set itself depends on input order")
    if spec.order_sensitive is not None:
        return True, f"order_sensitive: {spec.order_sensitive}"
    for fixture in _determinism_fixtures():
        want = _plan_mapping(_decide_via_runner(spec, arm, fixture, seed=_SEED, tiebreak=_TIEBREAK))
        for i in range(_SHUFFLES):
            shuffler = random.Random(1000 + i)
            trucks, orders = list(fixture.trucks), list(fixture.orders)
            shuffler.shuffle(trucks)
            shuffler.shuffle(orders)
            got = _plan_mapping(_decide_via_runner(spec, arm, fixture, seed=_SEED, tiebreak=_TIEBREAK,
                                                   trucks=trucks, orders=orders))
            if got != want:
                return False, f"{fixture.id}: shuffle {i} changed the plan: {str(got)[:120]} vs {str(want)[:120]}"
    names = ", ".join(f.id for f in _determinism_fixtures())
    return True, f"one plan over {_SHUFFLES} shuffles on {names}; {scope_note}"


def _no_module_rng(outcomes: _Outcomes) -> tuple[bool, str]:
    """With ``Random(seed)`` injected, ``DispatchRunner.run`` leaves ``random.getstate()`` alone.

    Scoped to fixtures whose trucks fit ``max_trucks_per_scope``: the partition
    scope's ``sampled_trucks`` draws from the random MODULE when that cap binds, by
    framework design (byte identity on the partitioned path), so the ``scale``
    fixture (1000 trucks > 500) is excluded. Any module draw on the others is the
    policy's own.
    """
    checked, excluded, touched = [], [], []
    for d in outcomes.all():
        if d.cap_binds:
            excluded.append(d.fixture_id)
        elif d.error is None:
            checked.append(d.fixture_id)
            if d.module_rng_touched:
                touched.append(d.fixture_id)
    if touched:
        return False, (f"decide drew from the random MODULE on {', '.join(touched)} although Random(seed) was "
                       "injected; draw only from ctx.rng / self.rng")
    note = (f"; not checked on {', '.join(excluded)}: the sample cap binds there and the partition scope "
            "samples from the module by design") if excluded else ""
    return True, f"random module state unchanged on {len(checked)} fixture(s){note}"


# --- performance -------------------------------------------------------------

def _performance_scale(spec: PolicySpec) -> tuple[bool, str]:
    fixture = fx.scale()
    times = []
    for _ in range(_PERF_REPEATS):
        d = _decide_via_runner(spec, {}, fixture, seed=fixture.seed, tiebreak=None)
        if d.error is not None:
            return False, f"decide raised {d.error}"
        times.append(d.decide_ms)
    best = min(times)
    listed = ", ".join(f"{t:.1f}" for t in times)
    verdict = f"best {best:.1f} ms of [{listed}] on {fixture.id}, budget {spec.perf_budget_ms} ms"
    return best <= spec.perf_budget_ms, verdict


# --- params ------------------------------------------------------------------

def _defaults_validate(spec: PolicySpec) -> tuple[bool, str]:
    defaults = spec.params_cls.from_mapping(None, where=f"defaults of {spec.name}")
    spec.params_cls.json_schema()
    spec.cls(params=defaults.to_dict())
    return True, f"defaults {defaults.to_dict()}"


def _rejected(spec: PolicySpec, raw: dict[str, Any]) -> bool:
    try:
        spec.params_cls.from_mapping(raw, where=f"battery for {spec.name}")
    except PolicyParamsError:
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
        if info.meta.choices is None:
            continue
        value = "__battery_not_a_choice__"
        tried.append(info.name)
        if not _rejected(spec, {info.name: value}):
            accepted.append(f"{info.name}={value!r} was accepted")
    if accepted:
        return False, "; ".join(accepted)
    return True, ("rejected a bad choice for " + ", ".join(tried)) if tried else "no choice params"


def _schema_keywords_supported(spec: PolicySpec) -> tuple[bool, str]:
    schema = spec.params_cls.json_schema()
    used = set(schema)
    for prop in schema.get("properties", {}).values():
        used |= set(prop)
    extra = sorted(used - SUPPORTED_SCHEMA_KEYWORDS)
    if extra:
        return False, f"unsupported schema keywords {extra}; supported: {sorted(SUPPORTED_SCHEMA_KEYWORDS)}"
    return True, f"keywords {sorted(used)}"
