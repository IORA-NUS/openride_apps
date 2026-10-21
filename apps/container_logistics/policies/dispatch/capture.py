"""Dispatch-context capture: write one decision's inputs to disk, and replay them.

Single responsibility: the ``openride.dispatch_context/1`` fixture format (plan
``docs/plans/policy_slots_p0_p3.md`` §4 P3.4).

- :class:`CaptureSettings` comes from ``profile["policy_capture"]``; absent (the
  default) means ``None`` and the runner gets no hook at all.
- :class:`CaptureRecorder` is the runner hook. It snapshots RNG state before
  ``decide`` (``getstate`` consumes nothing) and writes the fixture after ``decide``
  and before the invariants, honouring ``every_n_ticks`` and ``max_files``. A write
  failure is logged (``DISPATCH CAPTURE FAILED``) and never raised.
- :func:`context_to_fixture` / :func:`context_from_fixture` convert both ways;
  :func:`replay_heads` re-runs a fixture with its recorded RNG state and tiebreak.

Why RNG state is recorded: on the partitioned path GreedyNearest breaks equal-cost
ties with a shuffle on the ``random`` MODULE and the sampled kind draws from it too;
on the pooled path both draw from the tick ``Random``. Heads are reproducible only
from the exact state at decide time, so the fixture carries it (an extension of the
plan's field list, under ``"rng"``).

Framework side of the boundary. Not a leaf module (imports ``scopes``/``allowed``).
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import random
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping

from apps.container_logistics.policies.framework.errors import PolicyError

from .allowed import CompanyRule, PartitionRule
from .context import DispatchClock, DispatchContext
from .helpers.candidates import CandidateSettings
from .plan import Plan

logger = logging.getLogger(__name__)

__all__ = [
    "FIXTURE_FORMAT",
    "CaptureSettings",
    "CaptureRecorder",
    "context_to_fixture",
    "context_from_fixture",
    "fixture_path",
    "write_fixture",
    "read_fixture",
    "replay_heads",
    "restored_rngs",
]

FIXTURE_FORMAT = "openride.dispatch_context/1"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class CaptureSettings:
    """Where and how often to capture: every ``every_n_ticks``-th step, at most ``max_files``."""

    dir: str
    every_n_ticks: int
    max_files: int
    run_id: str | None = None

    @classmethod
    def from_profile(cls, prof: Mapping[str, Any]) -> "CaptureSettings | None":
        """``None`` when ``prof`` has no ``policy_capture``; else the validated settings.

        Raises ``PolicyError`` for a non-mapping, a missing/empty ``dir``, or a
        non-positive (or ``bool``) ``every_n_ticks`` / ``max_files``.
        """
        raw = prof.get("policy_capture")
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise PolicyError(
                f"policy_capture must be an object {{dir, every_n_ticks, max_files}} (got {raw!r})."
            )
        directory = raw.get("dir")
        if not isinstance(directory, str) or not directory:
            raise PolicyError(f"policy_capture.dir must be a non-empty path (got {directory!r}).")
        every = _positive_int(raw.get("every_n_ticks", 1), "every_n_ticks")
        cap = _positive_int(raw.get("max_files", 40), "max_files")
        run_id = raw.get("run_id")
        return cls(dir=directory, every_n_ticks=every, max_files=cap,
                   run_id=None if run_id is None else str(run_id))

    def wants(self, time_step: int) -> bool:
        """True when ``time_step`` is a capture tick."""
        return int(time_step) % self.every_n_ticks == 0


def _positive_int(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PolicyError(f"policy_capture.{key} must be a positive integer (got {value!r}).")
    return value


def _rng_entry(rng: Any) -> dict[str, Any]:
    state = rng.getstate()
    return {"module": rng is random, "state": [state[0], list(state[1]), state[2]]}


def _rng_snapshot(ctx: DispatchContext) -> dict[str, Any]:
    # _sample_rng is the scope's sampling RNG, chosen by scopes.py; read here (same
    # package) because capture must record exactly what sampled_trucks draws from.
    sample = getattr(ctx, "_sample_rng", None)
    snap: dict[str, Any] = {"policy": _rng_entry(ctx.rng)}
    snap["sample"] = {"same_as_policy": True} if sample is ctx.rng else _rng_entry(sample)
    return snap


def _ids(pairs: Any) -> list[list[str]]:
    return [[str(t.get("_id")), str(o.get("_id"))] for t, o in pairs]


def context_to_fixture(
    ctx: DispatchContext,
    plan: Any,
    policy_spec: Any,
    *,
    rng_state: Mapping[str, Any] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """The ``openride.dispatch_context/1`` dict for one decided scope.

    ``policy_spec`` is anything with ``name`` and ``version`` (a ``PolicySpec`` or a
    ``ResolvedDispatch``). ``live_heads`` is ``plan.heads()``, or ``None`` when
    ``plan`` is not a ``Plan``. ``spatial_pairs`` is set for company scope (the
    round's prebuilt slice) and ``None`` for partition scope (recomputed on replay).
    Raises ``PolicyError`` when ``ctx.rule`` is missing.
    """
    if ctx.rule is None:
        raise PolicyError(f"scope {ctx.scope.key} has no rule; a context without one cannot be captured")
    spatial = _ids(ctx.candidate_pairs("spatial")) if ctx.scope.kind == "company" else None
    return {
        "format": FIXTURE_FORMAT,
        "run_id": run_id,
        "time_step": ctx.clock.time_step,
        "sim_clock": ctx.clock.sim_clock,
        "horizon_steps": ctx.clock.horizon_steps,
        "scope": {"kind": ctx.scope.kind, "key": ctx.scope.key, "round": ctx.scope.round},
        "policy": {
            "name": policy_spec.name,
            "version": policy_spec.version,
            "params": None if ctx.params is None else ctx.params.to_dict(),
        },
        "candidate_kind": ctx.default_candidate_kind,
        "candidate_settings": ctx.candidate_settings.to_dict(),
        "rule": ctx.rule.to_dict(),
        "tiebreak_seed": ctx.tiebreak_seed,
        "rng": None if rng_state is None else dict(rng_state),
        "trucks": list(ctx.raw_trucks),
        "orders": list(ctx.raw_orders),
        "spatial_pairs": spatial,
        "live_heads": [list(h) for h in plan.heads()] if isinstance(plan, Plan) else None,
    }


def fixture_path(directory: str, time_step: int, scope_key: str, round: int | None = None) -> str:
    """``<dir>/step_<n>_<scope-key>[_r<round>].json.gz`` with the key made filename-safe."""
    stem = f"step_{int(time_step)}_{_UNSAFE.sub('-', scope_key)}"
    if round is not None:
        stem += f"_r{int(round)}"  # pooled rounds share a step and a company
    return os.path.join(directory, stem + ".json.gz")


def write_fixture(path: str, fx: Mapping[str, Any]) -> None:
    """Write ``fx`` as byte-stable gzip JSON (``mtime=0``). Raises ``OSError``/``TypeError``."""
    payload = json.dumps(fx, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(payload)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(buf.getvalue())
    os.replace(tmp, path)


def read_fixture(path: str) -> dict[str, Any]:
    """Read one fixture file. Raises ``PolicyError`` for an unknown ``format``."""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        fx = json.load(fh)
    if not isinstance(fx, dict) or fx.get("format") != FIXTURE_FORMAT:
        raise PolicyError(f"{path}: not a {FIXTURE_FORMAT} fixture (format={fx.get('format')!r}).")
    return fx


class CaptureRecorder:
    """The runner's capture hook: ``before_decide(ctx)`` then ``__call__(ctx=, plan=, problems=)``.

    Writes at most ``settings.max_files`` files (counting files already in the
    directory) and only on ``settings.wants(time_step)`` ticks. Never raises.
    """

    def __init__(self, settings: CaptureSettings, *, resolved: Any) -> None:
        self.settings = settings
        self.resolved = resolved
        self.written: int | None = None
        self._pending: tuple[int, dict[str, Any]] | None = None

    def _room_left(self) -> bool:
        if self.written is None:
            try:
                names = os.listdir(self.settings.dir)
            except FileNotFoundError:
                names = []
            self.written = sum(1 for n in names if n.startswith("step_") and n.endswith(".json.gz"))
        return self.written < self.settings.max_files

    def _wants(self, ctx: DispatchContext) -> bool:
        return self.settings.wants(ctx.clock.time_step) and self._room_left()

    def before_decide(self, ctx: DispatchContext) -> None:
        """Snapshot RNG state for ``ctx`` when this scope will be captured."""
        self._pending = None
        try:
            if self._wants(ctx):
                self._pending = (id(ctx), _rng_snapshot(ctx))
        except Exception:  # noqa: BLE001 - capture must never break a tick
            logger.exception("DISPATCH CAPTURE FAILED: RNG snapshot for scope %s (celery journal)",
                             ctx.scope.key)

    def __call__(self, *, ctx: DispatchContext, plan: Any, problems: list[str]) -> None:
        pending, self._pending = self._pending, None
        if pending is None or pending[0] != id(ctx):
            return
        path = fixture_path(self.settings.dir, ctx.clock.time_step, ctx.scope.key, ctx.scope.round)
        try:
            fx = context_to_fixture(ctx, plan, self.resolved, rng_state=pending[1],
                                    run_id=self.settings.run_id)
            write_fixture(path, fx)
            self.written = (self.written or 0) + 1
        except Exception:  # noqa: BLE001 - logged loudly, never blocks the tick
            logger.exception(
                "DISPATCH CAPTURE FAILED: could not write %s for scope %s at step %d; the tick "
                "continues uncaptured (agent-side: see celery's journal, not sim.log).",
                path, ctx.scope.key, ctx.clock.time_step,
            )


def _rule_from(d: Mapping[str, Any]) -> PartitionRule | CompanyRule:
    if d.get("kind") == "partition":
        return PartitionRule(max_travel_time_pickup=d.get("max_travel_time_pickup"),
                             adjacency={k: set(v) for k, v in (d.get("adjacency") or {}).items()})
    if d.get("kind") == "company":
        return CompanyRule(haulier_id=d["haulier_id"], max_travel_time_pickup=d.get("max_travel_time_pickup"))
    raise PolicyError(f"fixture rule kind {d.get('kind')!r} is not 'partition' or 'company'.")


def _partition_key(scope_key: str) -> tuple[str, Any]:
    _prefix, kind, value = scope_key.split(":", 2)
    return (kind, int(value)) if kind == "component" else (kind, value)


def _pairs_from_ids(fx: Mapping[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    trucks: dict[str, dict[str, Any]] = {}
    for t in fx["trucks"]:
        trucks.setdefault(str(t.get("_id")), t)
    orders: dict[str, dict[str, Any]] = {}
    for o in fx["orders"]:
        orders.setdefault(str(o.get("_id")), o)
    try:
        return [(trucks[tid], orders[oid]) for tid, oid in fx["spatial_pairs"] or ()]
    except KeyError as exc:
        raise PolicyError(f"fixture spatial pair names {exc} which is not in its trucks/orders") from exc


def context_from_fixture(fx: Mapping[str, Any], runner: Any = None, *, rng: Any = None) -> DispatchContext:
    """Rebuild a ``DispatchContext`` from fixture ``fx`` for ``runner``'s policy.

    ``runner`` defaults to a fresh runner for the captured policy and params
    (``build_runner_for_policy``). Built through ``scopes`` like every context.
    ``rng`` is the company scope's sampling RNG (default: the runner policy's
    ``rng``); a partition scope always samples from the ``random`` module.
    """
    from .runner import build_runner_for_policy, framework_accounting_cost

    if fx.get("format") != FIXTURE_FORMAT:
        raise PolicyError(f"not a {FIXTURE_FORMAT} fixture (format={fx.get('format')!r}).")
    if runner is None:
        runner = build_runner_for_policy(fx["policy"]["name"], fx["policy"]["params"])
    scope = fx["scope"]
    common = dict(
        trucks=list(fx["trucks"]),
        orders=list(fx["orders"]),
        rule=_rule_from(fx["rule"]),
        settings=CandidateSettings(**fx["candidate_settings"]),
        accounting_cost=framework_accounting_cost(fx["policy"]["params"]),
        clock=DispatchClock(sim_clock=fx["sim_clock"], time_step=fx["time_step"],
                            horizon_steps=fx.get("horizon_steps")),
    )
    if scope["kind"] == "company":
        return runner.company_context(
            haulier_id=fx["rule"]["haulier_id"], round=scope["round"],
            rng=runner.policy.rng if rng is None else rng,
            prebuilt_pairs=_pairs_from_ids(fx), **common,
        )
    return runner.partition_context(partition_key=_partition_key(scope["key"]), **common)


def _restore(entry: Mapping[str, Any]) -> random.Random:
    r = random.Random()
    r.setstate((entry["state"][0], tuple(entry["state"][1]), entry["state"][2]))
    return r


@contextmanager
def restored_rngs(fx: Mapping[str, Any]) -> Iterator[tuple[Any, Any]]:
    """Yield ``(policy_rng, sample_rng)`` restored from ``fx["rng"]``; ``None`` = the module.

    A module entry seeds the global ``random`` state for the block and puts the
    previous state back afterwards. Raises ``PolicyError`` when the fixture has no
    recorded RNG state.
    """
    snap = fx.get("rng")
    if not snap:
        raise PolicyError("fixture has no recorded RNG state; it cannot be replayed exactly")
    saved = random.getstate()
    try:
        policy_entry = snap["policy"]
        if policy_entry["module"]:
            random.setstate(_restore(policy_entry).getstate())
            policy_rng = None
        else:
            policy_rng = _restore(policy_entry)
        sample_entry = snap["sample"]
        if sample_entry.get("same_as_policy"):
            sample_rng = policy_rng
        elif sample_entry["module"]:
            random.setstate(_restore(sample_entry).getstate())
            sample_rng = None
        else:
            sample_rng = _restore(sample_entry)
        yield policy_rng, sample_rng
    finally:
        random.setstate(saved)


def replay_heads(fx: Mapping[str, Any]) -> list[list[str]]:
    """Re-run the captured policy on ``fx`` with its recorded RNG state and tiebreak.

    Returns the plan's heads as ``[[truck_id, order_id], ...]``, comparable with
    ``fx["live_heads"]``. Raises ``PolicyError`` for a fixture without RNG state or
    an unregistered policy.
    """
    from .runner import build_runner_for_policy

    runner = build_runner_for_policy(fx["policy"]["name"], fx["policy"]["params"])
    with restored_rngs(fx) as (policy_rng, sample_rng):
        runner.policy.set_rng(policy_rng)
        runner.policy.set_tiebreak(fx.get("tiebreak_seed"))
        ctx = context_from_fixture(fx, runner, rng=sample_rng if sample_rng is not None else random)
        plan = runner.policy.decide(ctx)
    return [list(h) for h in plan.heads()]
