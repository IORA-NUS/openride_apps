"""The policy contract battery: ``run_battery(slot, name) -> list[CheckResult]``.

Single responsibility: decide, for one registered policy, whether it honours the
dispatch contract (plan ``docs/plans/policy_slots_p0_p3.md`` §4 P3.2). Five groups,
one :class:`CheckResult` per id in :data:`DISPATCH_CHECKS`, in that order:

- ``structure`` -- on every small non-empty fixture: a ``Plan`` comes back, no truck
  or order twice, only in-scope ids, only allowed heads.
- ``robustness`` -- one per fixture: ``decide`` returns a ``Plan`` without raising.
  Contract breaches are reported once, under ``structure``, so one bug fails one check.
- ``determinism`` -- ``reproducible``: two fresh instances with the same
  ``Random(7)`` and tiebreak give the same plan; ``order_independent``: with a
  tiebreak seed, 20 shuffles of trucks and orders give one plan (skipped, ok, when
  the policy declares ``order_sensitive``). Its fixtures keep fewer trucks than the
  spatial per-order cap: ``spatial.iter_candidate_pairs`` truncates candidates in
  input order, which is framework behaviour, not the policy's.
- ``performance`` -- ``scale``: best of 3 decide times under ``spec.perf_budget_ms``.
- ``params`` -- defaults validate; unknown keys, out-of-bounds values and bad
  choices are rejected; the schema uses only ``SUPPORTED_SCHEMA_KEYWORDS``.

Every check gets a fresh policy instance (``build_runner_for_policy``). The global
``random`` state is seeded per decide and restored afterwards, because the partition
scope samples from the module (``scopes.partition_context``).

Testing side: reads the registry and the dispatch framework; registers nothing.
"""

from __future__ import annotations

import logging
import math
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from apps.container_logistics.policies.dispatch.context import DispatchContext
from apps.container_logistics.policies.dispatch.plan import Plan
from apps.container_logistics.policies.dispatch.runner import DispatchRunner, build_runner_for_policy
from apps.container_logistics.policies.framework.errors import (
    DispatchContractViolation,
    PolicyParamsError,
    SlotNotImplementedError,
)
from apps.container_logistics.policies.framework.params import SUPPORTED_SCHEMA_KEYWORDS, declared_params
from apps.container_logistics.policies.framework.registry import PolicySpec, get_policy_spec

from . import fixtures as fx

logger = logging.getLogger(__name__)

__all__ = ["CheckResult", "DISPATCH_CHECKS", "run_battery", "decide_seeded", "seeded_module_random"]

_STRUCTURE = ("returns_plan", "no_truck_twice", "no_order_twice", "only_in_scope", "only_allowed_pairs")

DISPATCH_CHECKS: tuple[str, ...] = (
    *(f"structure.{c}" for c in _STRUCTURE),
    *(f"robustness.{make.__name__}" for make in fx.ROBUSTNESS_FIXTURES),
    "determinism.reproducible",
    "determinism.order_independent",
    "performance.scale",
    "params.defaults_validate",
    "params.rejects_unknown_key",
    "params.rejects_out_of_bounds",
    "params.rejects_bad_choice",
    "params.schema_keywords_supported",
)

_SEED = 7
_TIEBREAK = 4242
_SHUFFLES = 20
_PERF_REPEATS = 3


@dataclass(frozen=True)
class CheckResult:
    """One check's verdict. ``id`` is ``<group>.<check>``, an entry of ``DISPATCH_CHECKS``."""

    group: str
    check: str
    ok: bool
    detail: str
    ms: float

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

    The module RNG is seeded with ``seed`` for the call. Exceptions propagate.
    """
    runner.policy.set_rng(random.Random(seed))
    runner.policy.set_tiebreak(tiebreak)
    with seeded_module_random(seed):
        ctx = fixture.context(runner, trucks=trucks, orders=orders)
        return ctx, runner.policy.decide(ctx)


def run_battery(slot: str, name: str) -> list[CheckResult]:
    """Run every check in ``DISPATCH_CHECKS`` on policy ``name`` of ``slot``.

    Raises ``UnknownPolicyError`` for an unregistered name and
    ``SlotNotImplementedError`` for a slot without a battery (only ``dispatch`` has one).
    """
    spec = get_policy_spec(slot, name)
    if slot != "dispatch":
        raise SlotNotImplementedError(f"no contract battery for slot {slot!r}; only 'dispatch' has one")
    checks: dict[str, Callable[[], tuple[bool, str]]] = {}
    for check in _STRUCTURE:
        checks[f"structure.{check}"] = _bind(_structure_check, spec, check)
    for make in fx.ROBUSTNESS_FIXTURES:
        checks[f"robustness.{make.__name__}"] = _bind(_robustness_check, spec, make)
    checks["determinism.reproducible"] = _bind(_reproducible, spec)
    checks["determinism.order_independent"] = _bind(_order_independent, spec)
    checks["performance.scale"] = _bind(_performance_scale, spec)
    checks["params.defaults_validate"] = _bind(_defaults_validate, spec)
    checks["params.rejects_unknown_key"] = _bind(_rejects_unknown_key, spec)
    checks["params.rejects_out_of_bounds"] = _bind(_rejects_out_of_bounds, spec)
    checks["params.rejects_bad_choice"] = _bind(_rejects_bad_choice, spec)
    checks["params.schema_keywords_supported"] = _bind(_schema_keywords_supported, spec)
    return [_timed(check_id, checks[check_id]) for check_id in DISPATCH_CHECKS]


def _bind(fn: Callable[..., tuple[bool, str]], *args: Any) -> Callable[[], tuple[bool, str]]:
    return lambda: fn(*args)


def _timed(check_id: str, fn: Callable[[], tuple[bool, str]]) -> CheckResult:
    group, _, check = check_id.partition(".")
    start = time.perf_counter()
    try:
        ok, detail = fn()
    except Exception as exc:  # noqa: BLE001 - a crashing check is a FAIL, never a crash of the battery
        ok, detail = False, f"raised {type(exc).__name__}: {exc}"
    return CheckResult(group=group, check=check, ok=ok, detail=detail,
                       ms=round((time.perf_counter() - start) * 1000.0, 3))


def _fresh(spec: PolicySpec) -> DispatchRunner:
    return build_runner_for_policy(spec.name)


# --- structure ---------------------------------------------------------------

def _docs_by_id(docs: tuple[dict[str, Any], ...]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for d in docs:
        out.setdefault(str(d.get("_id")), d)
    return out


def _structure_problems(check: str, ctx: DispatchContext, plan: Plan) -> list[str]:
    trucks, orders = _docs_by_id(ctx.raw_trucks), _docs_by_id(ctx.raw_orders)
    if check == "returns_plan":
        return [f"chain for {tid!r} is empty or not a tuple: {c!r}"
                for tid, c in plan.chains.items() if not isinstance(c, tuple) or not c]
    chains = [(tid, c) for tid, c in plan.chains.items() if isinstance(c, tuple) and c]
    if check == "no_truck_twice":
        return []  # chains is keyed by truck id; a repeat can only surface as from_pairs' violation
    if check == "no_order_twice":
        seen: set[str] = set()
        repeats = []
        for _tid, c in chains:
            for oid in c:
                if oid in seen:
                    repeats.append(f"order {oid!r} appears twice")
                seen.add(oid)
        return repeats
    if check == "only_in_scope":
        bad = [f"truck {tid!r} not in scope" for tid, _c in chains if tid not in trucks]
        bad += [f"order {oid!r} not in scope" for _tid, c in chains for oid in c if oid not in orders]
        return bad
    if check == "only_allowed_pairs":
        return [f"head ({tid!r}, {c[0]!r}) is not allowed" for tid, c in chains
                if tid in trucks and c[0] in orders and not ctx.allowed_raw(trucks[tid], orders[c[0]])]
    raise AssertionError(check)


def _structure_check(spec: PolicySpec, check: str) -> tuple[bool, str]:
    problems: list[str] = []
    skipped: list[str] = []
    for make in fx.SMALL_FIXTURES:
        fixture = make()
        try:
            ctx, plan = decide_seeded(_fresh(spec), fixture, seed=_SEED, tiebreak=None)
        except DispatchContractViolation as exc:
            twice = "appears twice in pairs" in str(exc)
            if (check == "no_truck_twice" and twice) or (check == "returns_plan" and not twice):
                problems.append(f"{fixture.id}: decide raised DispatchContractViolation: {exc}")
            else:
                skipped.append(fixture.id)
            continue
        except Exception as exc:  # noqa: BLE001
            if check == "returns_plan":
                problems.append(f"{fixture.id}: decide raised {type(exc).__name__}: {exc}")
            else:
                skipped.append(fixture.id)
            continue
        if not isinstance(plan, Plan):
            if check == "returns_plan":
                problems.append(f"{fixture.id}: decide returned {type(plan).__name__}, expected Plan")
            else:
                skipped.append(fixture.id)
            continue
        problems += [f"{fixture.id}: {p}" for p in _structure_problems(check, ctx, plan)]
    if problems:
        return False, "; ".join(problems)
    names = ", ".join(make.__name__ for make in fx.SMALL_FIXTURES)
    note = f" (not checked on {', '.join(skipped)}: see structure.returns_plan)" if skipped else ""
    return True, f"ok on {names}{note}"


# --- robustness --------------------------------------------------------------

def _robustness_check(spec: PolicySpec, make: Callable[[], Any]) -> tuple[bool, str]:
    fixture = make()
    _ctx, plan = decide_seeded(_fresh(spec), fixture, seed=fixture.seed, tiebreak=None)
    if not isinstance(plan, Plan):
        return False, f"decide returned {type(plan).__name__}, expected Plan"
    return True, f"{len(plan.heads())} head(s) over {len(fixture.trucks)} trucks x {len(fixture.orders)} orders"


# --- determinism -------------------------------------------------------------

def _determinism_fixtures() -> list[Any]:
    return [fx.tied_costs(), fx.scale(n_trucks=30, n_orders=20, hauliers=2, seed=9)]


def _plan_items(plan: Any) -> Any:
    return list(plan.chains.items()) if isinstance(plan, Plan) else plan


def _reproducible(spec: PolicySpec) -> tuple[bool, str]:
    fixtures = _determinism_fixtures() + [fx.scale(n_trucks=200, n_orders=100, hauliers=2, seed=5)]
    for fixture in fixtures:
        for tiebreak in (None, _TIEBREAK):
            _c1, first = decide_seeded(_fresh(spec), fixture, seed=_SEED, tiebreak=tiebreak)
            _c2, second = decide_seeded(_fresh(spec), fixture, seed=_SEED, tiebreak=tiebreak)
            if _plan_items(first) != _plan_items(second):
                return False, (f"{fixture.id} (tiebreak={tiebreak}): two fresh instances with Random({_SEED}) "
                               f"gave different plans: {_plan_items(first)[:4]} vs {_plan_items(second)[:4]}")
    return True, f"same plan twice on {', '.join(f.id for f in fixtures)}, with and without a tiebreak"


def _order_independent(spec: PolicySpec) -> tuple[bool, str]:
    if spec.order_sensitive is not None:
        return True, f"order_sensitive: {spec.order_sensitive}"
    for fixture in _determinism_fixtures():
        _c, base = decide_seeded(_fresh(spec), fixture, seed=_SEED, tiebreak=_TIEBREAK)
        want = dict(base.chains) if isinstance(base, Plan) else base
        for i in range(_SHUFFLES):
            shuffler = random.Random(1000 + i)
            trucks, orders = list(fixture.trucks), list(fixture.orders)
            shuffler.shuffle(trucks)
            shuffler.shuffle(orders)
            _c, plan = decide_seeded(_fresh(spec), fixture, seed=_SEED, tiebreak=_TIEBREAK,
                                     trucks=trucks, orders=orders)
            got = dict(plan.chains) if isinstance(plan, Plan) else plan
            if got != want:
                return False, f"{fixture.id}: shuffle {i} changed the plan ({len(got)} vs {len(want)} chains)"
    return True, f"one plan over {_SHUFFLES} shuffles on {', '.join(f.id for f in _determinism_fixtures())}"


# --- performance -------------------------------------------------------------

def _performance_scale(spec: PolicySpec) -> tuple[bool, str]:
    fixture = fx.scale()
    runner = _fresh(spec)
    times = []
    for _ in range(_PERF_REPEATS):
        runner.policy.set_rng(random.Random(fixture.seed))
        runner.policy.set_tiebreak(None)
        with seeded_module_random(fixture.seed):
            ctx = fixture.context(runner)
            start = time.perf_counter()
            runner.policy.decide(ctx)
            times.append((time.perf_counter() - start) * 1000.0)
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
