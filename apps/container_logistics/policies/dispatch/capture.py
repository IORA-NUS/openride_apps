"""Planner capture: write one whole tick's inputs to disk, and replay them.

Single responsibility: the ``openride.dispatch_context/3`` fixture format (plan
``plan_contract_final.md`` §5.1 row 27): ONE file per captured tick,
``<dir>/step_<n>.json.gz``, holding everything the selected planner decided from:

- ``format``, ``run_id``, ``time_step``, ``sim_clock``, ``horizon_steps``;
- ``tick_seed`` (the tick's ``rng`` is ``Random(tick_seed)``) and ``module_rng_state``
  (the ``random`` module's state just before the solve: ``Partitioned`` draws from the
  module by declaration);
- ``selection``: the bound selection in stamp form (``name``, ``version``, ``label``,
  ``leaf``, ``timing``, nested ``params`` with each ref as ``{type, version, params}``);
- ``world``: the run world (the assignment profile minus the selection keys, the RAW
  mapping: the whole cooperation menu with its active arm, the candidate knobs,
  ``max_travel_time_pickup``, ...); ``accounting_params``: the measurement's dials;
- ``declaration``: the top's ``cross_company`` (sorted ``[owner, carrier]`` lists) and
  each node path's ``candidate_hint``;
- ``trucks``, ``orders`` (the candidates and unassigned orders, in tick order);
- ``live_pairs``: the raw result's pairs (captured BEFORE the invariants judged it), or
  ``None`` when the solve returned no ``Result``;
- ``rebate_aware``: whether the profile opted the solver into rebate pricing. The
  rebate book is NOT captured, so such a tick cannot be replayed (refused).

Format ``/2`` files are read through ``compat.capture_v2`` (converted to ``/3``);
``/1`` is refused (message 22).

Surfaces:

- :class:`CaptureSettings` comes from ``profile["policy_capture"]``; absent (the
  default) means ``None`` and the runner gets no hook at all.
- :class:`CaptureRecorder` is the runner hook. On a capture tick it snapshots the
  module RNG before the solve (``getstate`` consumes nothing) and writes the fixture
  after the solve and before the invariants, honouring ``every_n_ticks`` (every N-th
  whole dispatch TICK this recorder sees, counted from its first, which is always
  captured -- NOT every N-th sim step; final review MINOR-4) and ``max_files``.
  Sub-tick calls (``Tick.call``) are ignored: the whole tick replays them. A write
  failure is logged (``PLANNER CAPTURE FAILED``) and never raised.
- :func:`parse_capture_spec` parses the ``every=N,max=M`` text that
  ``ORSIM_CAPTURE_DISPATCH`` / ``--capture-dispatch`` carry (the one parser).
- :func:`read_fixture` refuses format ``/1`` (message 22) and anything not ``/2``
  or ``/3``; :func:`context_from_fixture` rebuilds the tick for any runner;
  :func:`replay_heads` re-solves the captured selection with the recorded RNG state.

Framework side of the boundary. Not a leaf module (imports ``tick``).
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import random
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping

from apps.container_logistics.policies.framework.errors import PolicyError

from .bind import world_snapshot
from .result import Result
from .tick import DispatchClock, Tick

logger = logging.getLogger(__name__)

__all__ = [
    "FIXTURE_FORMAT",
    "FORMAT_1",
    "FORMAT_2",
    "CAPTURE_SPEC_FORMAT",
    "CaptureSettings",
    "CaptureRecorder",
    "parse_capture_spec",
    "context_to_fixture",
    "context_from_fixture",
    "fixture_path",
    "write_fixture",
    "read_fixture",
    "check_fixture",
    "replay_heads",
    "restored_module_rng",
    "selection_stamp",
]

FIXTURE_FORMAT = "openride.dispatch_context/3"
#: The round-2 whole-tick format: read through ``compat.capture_v2``.
FORMAT_2 = "openride.dispatch_context/2"
#: The round-1 one-scope format: refused on read (message 22).
FORMAT_1 = "openride.dispatch_context/1"
_DEFAULT_EVERY_N_TICKS = 1
_DEFAULT_MAX_FILES = 40
CAPTURE_SPEC_FORMAT = "every=N,max=M"
_SPEC_KEYS = {"every": "every_n_ticks", "max": "max_files"}


@dataclass(frozen=True)
class CaptureSettings:
    """Where and how often to capture: every ``every_n_ticks``-th dispatch tick, at most ``max_files``.

    Ticks, not sim steps: an assignment agent ticks every ``steps_per_action`` steps
    (28 on ``consortium_collab_7d``), so a step modulus would capture every
    ``lcm(N, cadence) / cadence``-th tick (``every=4`` there: every tick).
    """

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
        every = _positive_int(raw.get("every_n_ticks", _DEFAULT_EVERY_N_TICKS), "every_n_ticks")
        cap = _positive_int(raw.get("max_files", _DEFAULT_MAX_FILES), "max_files")
        run_id = raw.get("run_id")
        return cls(dir=directory, every_n_ticks=every, max_files=cap,
                   run_id=None if run_id is None else str(run_id))

    @staticmethod
    def rebate_aware_of(prof: Mapping[str, Any]) -> bool:
        """``profile.planner.rebate_aware`` (``AssignmentApp._rebate_aware``'s rule)."""
        planner = (prof or {}).get("planner") or {}
        return bool(planner.get("rebate_aware", False)) if isinstance(planner, Mapping) else False

    def wants(self, tick_index: int) -> bool:
        """True when the ``tick_index``-th dispatch tick (0-based) is a capture tick."""
        return int(tick_index) % self.every_n_ticks == 0


def _positive_int(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PolicyError(f"policy_capture.{key} must be a positive integer (got {value!r}).")
    return value


def parse_capture_spec(text: str) -> dict[str, int]:
    """Parse ``"every=N,max=M"`` into ``{"every_n_ticks": N, "max_files": M}``.

    Both keys are optional (defaults ``every=1``, ``max=40``) but the text must name
    at least one; values are positive decimal integers. Raises ``PolicyError`` naming
    the expected format for empty text, an unknown or repeated key, or a bad value.
    """
    where = f"capture spec {text!r}"
    hint = f"expected {CAPTURE_SPEC_FORMAT} (e.g. every=10,max=40)"
    parts = [p.strip() for p in str(text or "").split(",")]
    if not any(parts):
        raise PolicyError(f"{where} is empty; {hint}.")
    out: dict[str, int] = {}
    for part in parts:
        key, sep, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in _SPEC_KEYS:
            raise PolicyError(f"{where}: {part!r} is not every=N or max=M; {hint}.")
        field = _SPEC_KEYS[key]
        if field in out:
            raise PolicyError(f"{where} sets {key!r} twice; {hint}.")
        if not (value.isascii() and value.isdigit()) or int(value) <= 0:
            raise PolicyError(f"{where}: {key}={value!r} must be a positive integer; {hint}.")
        out[field] = int(value)
    return {"every_n_ticks": out.get("every_n_ticks", _DEFAULT_EVERY_N_TICKS),
            "max_files": out.get("max_files", _DEFAULT_MAX_FILES)}


def _module_state() -> list[Any]:
    state = random.getstate()
    return [state[0], list(state[1]), state[2]]


def selection_stamp(node: Any) -> dict[str, Any]:
    """The bound ``ResolvedDispatch`` tree in stamp form (``run_config.meta.policies.planner`` shape)."""
    params = node.params
    return {
        "name": node.name,
        "version": node.version,
        "label": node.label,
        "leaf": node.leaf,
        "timing": node.timing,
        "params": None if params is None else params.to_profile_dict(),
    }


def declaration_record(tree: Any) -> dict[str, Any]:
    """The bound tree's declarations in capture form: the top's ``cross_company``, each path's hint."""
    return {
        "cross_company": [list(p) for p in sorted(tree.declaration.cross_company)],
        "hints": {node.path: node.declaration.candidate_hint for node in tree.nodes},
    }


def context_to_fixture(
    tick: Tick,
    result: Any,
    resolved: Any,
    *,
    module_rng_state: list[Any] | None,
    run_id: str | None = None,
    rebate_aware: bool = False,
) -> dict[str, Any]:
    """The ``openride.dispatch_context/3`` dict for one solved WHOLE tick.

    ``resolved`` is the bound selection (a ``ResolvedDispatch`` tree). ``live_pairs``
    is ``result.pairs``, or ``None`` when ``result`` is not a ``Result``. The world is
    the RAW mapping (never iterated through the read-tracking view). Raises
    ``PolicyError`` for a sub-tick.
    """
    if tick.depth != 0:
        raise PolicyError(f"scope {tick.scope}: only a whole tick is captured (format /3)")
    return {
        "format": FIXTURE_FORMAT,
        "run_id": run_id,
        "time_step": tick.clock.time_step,
        "sim_clock": tick.clock.sim_clock,
        "horizon_steps": tick.clock.horizon_steps,
        "tick_seed": tick.seed,
        "module_rng_state": module_rng_state,
        "selection": selection_stamp(resolved),
        "world": world_snapshot(tick.world),
        "accounting_params": dict(tick.measure.dials),
        "declaration": declaration_record(tick._shared.tree),
        "trucks": list(tick.raw_trucks),
        "orders": list(tick.raw_orders),
        "live_pairs": [list(p) for p in result.pairs] if isinstance(result, Result) else None,
        "rebate_aware": bool(rebate_aware),
    }


def fixture_path(directory: str, time_step: int) -> str:
    """``<dir>/step_<n>.json.gz``: one file per captured tick."""
    return os.path.join(directory, f"step_{int(time_step)}.json.gz")


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


def check_fixture(fx: Any, path: str = "<fixture>") -> Mapping[str, Any]:
    """``fx`` as a format ``/3`` fixture (a ``/2`` one converted); raises ``PolicyError`` otherwise (message 22 for ``/1``)."""
    fmt = fx.get("format") if isinstance(fx, Mapping) else None
    if fmt == FORMAT_1:
        raise PolicyError(
            f"dispatch fixture {path} is format {FORMAT_1} (one scope); this version replays whole ticks "
            "(format /3). Re-capture with --capture-dispatch."
        )
    if fmt == FORMAT_2:
        from apps.container_logistics.policies.compat.capture_v2 import read_fixture_v2

        return read_fixture_v2(fx)
    if fmt != FIXTURE_FORMAT:
        raise PolicyError(f"{path}: not a {FIXTURE_FORMAT} fixture (format={fmt!r}).")
    return fx


def read_fixture(path: str) -> dict[str, Any]:
    """Read one fixture file (``/3``, or ``/2`` converted). Raises ``PolicyError`` for ``/1`` (message 22) or any other format."""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        fx = json.load(fh)
    return dict(check_fixture(fx, path))


class CaptureRecorder:
    """The runner's capture hook: ``before_solve(tick)`` then ``__call__(tick=, result=, problems=)``.

    Records whole ticks only (a sub-tick call is ignored). Writes at most
    ``settings.max_files`` files (counting files already in the directory) and only
    on ``settings.wants(tick_index)`` ticks, ``tick_index`` counting the whole ticks
    this recorder has seen (0 = the first, always captured). A rebuilt runner (the
    assignment app rebuilds it when its profile is patched) starts counting afresh.
    ``resolved`` is the bound selection. Never raises.
    """

    def __init__(self, settings: CaptureSettings, *, resolved: Any, rebate_aware: bool = False) -> None:
        self.settings = settings
        self.resolved = resolved
        self.rebate_aware = bool(rebate_aware)
        self.written: int | None = None
        self._pending: tuple[int, list[Any]] | None = None
        self.ticks_seen = 0

    def _room_left(self) -> bool:
        if self.written is None:
            try:
                names = os.listdir(self.settings.dir)
            except FileNotFoundError:
                names = []
            self.written = sum(1 for n in names if n.startswith("step_") and n.endswith(".json.gz"))
        return self.written < self.settings.max_files

    def _wants(self, tick_index: int) -> bool:
        return self.settings.wants(tick_index) and self._room_left()

    def before_solve(self, tick: Tick) -> None:
        """Snapshot the module RNG for ``tick`` when this tick will be captured.

        Planning ticks (``tick.kind == "plan"``) are never captured and never counted:
        ``every=N`` counts dispatch ticks, and a whole-window planning tick is not a
        replayable dispatch decision (format ``/3`` is unchanged).
        """
        if tick.depth != 0 or getattr(tick, "kind", "dispatch") == "plan":
            return
        self._pending = None
        tick_index, self.ticks_seen = self.ticks_seen, self.ticks_seen + 1
        try:
            if self._wants(tick_index):
                self._pending = (id(tick), _module_state())
        except Exception:  # noqa: BLE001 - capture must never break a tick
            logger.exception("PLANNER CAPTURE FAILED: RNG snapshot at step %d (celery journal)",
                             tick.clock.time_step)

    def __call__(self, *, tick: Tick, result: Any, problems: list[str]) -> None:
        if tick.depth != 0 or getattr(tick, "kind", "dispatch") == "plan":
            return
        pending, self._pending = self._pending, None
        if pending is None or pending[0] != id(tick):
            return
        path = fixture_path(self.settings.dir, tick.clock.time_step)
        try:
            fx = context_to_fixture(tick, result, self.resolved, module_rng_state=pending[1],
                                    run_id=self.settings.run_id, rebate_aware=self.rebate_aware)
            write_fixture(path, fx)
            self.written = (self.written or 0) + 1
        except Exception:  # noqa: BLE001 - logged loudly, never blocks the tick
            logger.exception(
                "PLANNER CAPTURE FAILED: could not write %s at step %d; the tick continues "
                "uncaptured (agent-side: see celery's journal, not sim.log).",
                path, tick.clock.time_step,
            )


def context_from_fixture(fx: Mapping[str, Any], runner: Any = None, *, scope_log: list | None = None) -> Tick:
    """Rebuild the captured tick for ``runner``'s selection (``runner.tick``).

    ``runner`` defaults to a fresh runner for the captured selection. The world, tick
    seed, accounting dials, clock and documents are the captured ones. Raises
    ``PolicyError`` for a fixture that is not format ``/3`` (or a convertible ``/2``).
    """
    from .runner import build_runner_for_policy

    fx = check_fixture(fx)
    if runner is None:
        runner = build_runner_for_policy(fx["selection"]["name"], fx["selection"]["params"])
    return runner.tick(
        candidates=list(fx["trucks"]),
        orders=list(fx["orders"]),
        profile=dict(fx["world"]),
        clock=DispatchClock(sim_clock=fx["sim_clock"], time_step=fx["time_step"],
                            horizon_steps=fx.get("horizon_steps")),
        seed=fx["tick_seed"],
        dials=dict(fx.get("accounting_params") or {}),
        scope_log=scope_log,
    )


@contextmanager
def restored_module_rng(fx: Mapping[str, Any]) -> Iterator[None]:
    """The ``random`` module set to the captured ``module_rng_state`` for the block, restored after.

    Raises ``PolicyError`` when the fixture has no recorded state.
    """
    state = fx.get("module_rng_state")
    if not state:
        raise PolicyError("fixture has no recorded module RNG state; it cannot be replayed exactly")
    saved = random.getstate()
    random.setstate((state[0], tuple(state[1]), state[2]))
    try:
        yield
    finally:
        random.setstate(saved)


def replay_heads(fx: Mapping[str, Any]) -> list[list[str]]:
    """Re-solve the captured selection on ``fx`` with its recorded tick seed and module RNG state.

    Returns the result's pairs as ``[[truck_id, order_id], ...]``, comparable with
    ``fx["live_pairs"]``. Raises ``PolicyError`` for a fixture that is not format
    ``/3`` (or a convertible ``/2``), one captured with ``rebate_aware: true`` (the
    rebate book is not captured), one without RNG state, or an unregistered selection.
    """
    from .runner import build_runner_for_policy

    fx = check_fixture(fx)
    if fx.get("rebate_aware"):
        raise PolicyError(
            f"dispatch fixture at step {fx.get('time_step')} was captured with planner.rebate_aware: true; "
            "the rebate book is not captured, so the tick cannot be replayed exactly."
        )
    runner = build_runner_for_policy(fx["selection"]["name"], fx["selection"]["params"])
    with restored_module_rng(fx):
        tick = context_from_fixture(fx, runner)
        result = runner.solve_unchecked(tick)
    return [list(p) for p in result.pairs]
