"""Run-level statistics a top solver declares: the aggregator protocol and its run-record writer.

Single responsibility: the FRAMEWORK half of per-run solver provenance (plan
``plan_contract_final.md`` §2.8, WS4a). A top node's ``bind`` may declare
``Declaration.run_stats = <aggregator class>``; the framework then holds one instance
per :attr:`RunStatsAggregator.meta_key` (created on first use, surviving runner
rebuilds), folds each committed tick's ``Result.tick_notes`` into it, and writes its
:meth:`~RunStatsAggregator.snapshot` onto ``run_config.meta.<meta_key>`` when the
aggregator says a push is due. What the notes mean, the cadence and any warning text
belong to the aggregator (the solver side); the framework only drives it.

Per tick (:func:`record_tick`): ``forced = agg.fold(tick_notes, clock)``; if ``forced``
and ``agg.push_due(force=True)``: mark + write; then if ``agg.push_due()``: mark +
write. At agent close (:func:`flush`): a forced write of every aggregator, then the
aggregator's OPTIONAL ``run_end_warnings() -> list[str]`` (when it defines one), each line
logged at WARNING by the framework (``RUN STATS WARNING (run_config.meta.<key>): ...``) so a
degraded run says so in the run's log, not only in its record. A tick whose result was
discarded (message 18) is never folded.

Containment (review L7): the aggregator is solver code running inside the assignment
tick, AFTER the pairs were committed. Anything it raises (construction, ``fold``,
``push_due``, ``mark_pushed``, ``snapshot``) is logged at ERROR with its traceback, the
aggregator is retired for the rest of the run, and ``run_config.meta.<meta_key>`` is
overwritten with :func:`invalid_snapshot` (``{"invalid": true, "error": ...}``) so the
run record says its stats cannot be trusted. The tick, its pairs and the wire are
untouched.

Validation (review M2): :func:`check_meta_key`, called from ``bind`` on the top's
declaration, refuses a ``meta_key`` that is not a plain identifier or that names a key
the framework itself writes under ``run_config.meta`` (:data:`RESERVED_META_KEYS`).
Every write goes to the CLASS key, re-checked on each tick with the same rule, never to
an instance's ``meta_key`` (review N4: ``self.meta_key = "policies"`` in an aggregator
would otherwise overwrite the planner stamp); a class whose key no longer passes is
never constructed, folded or pushed.

:func:`patch_run_config_meta` is the run-record writer, moved verbatim from
``AssignmentApp._patch_round_stats_to_run_config`` (``assignment/app.py`` at 1e80917)
with the meta key made generic.

Leaf module: stdlib and ``policies.framework.errors`` at import (the HTTP client and
settings are imported inside :func:`patch_run_config_meta`); never imports
``apps.container_logistics.assignment.*``.
Framework side of the boundary: names no solver and no solver concept.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Mapping, MutableMapping, Protocol

from apps.container_logistics.policies.framework.errors import PolicyError

__all__ = [
    "RESERVED_META_KEYS",
    "RunStatsAggregator",
    "check_meta_key",
    "flush",
    "invalid_snapshot",
    "patch_run_config_meta",
    "record_tick",
]

logger = logging.getLogger(__name__)

#: Every key the framework itself writes under ``run_config.meta``: the run-start meta of
#: ``container_logistics.scenario.scenario_manager.get_run_config_meta`` (``policies`` is
#: the planner stamp). A run-stats ``meta_key`` naming one would PATCH over it.
#: ``tests/test_framework_integrity.py`` pins this set against that function's source.
RESERVED_META_KEYS = frozenset({
    "facility_rules",
    "num_analytics_agents",
    "num_assignment_agents",
    "num_facility_agents",
    "num_order_agents",
    "num_truck_agents",
    "policies",
    "rebate",
    "scenario_display_name",
    "scenario_slug",
    "services",
    "simulation_settings",
})

# A plain identifier: no "." (the PATCH is dotted, so "policies.planner" would write
# INSIDE the stamp) and no leading "$" (a Mongo operator).
_META_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: ``push(meta_key, snapshot) -> bool``: writes one snapshot onto the run record.
Push = Callable[[str, dict[str, Any]], bool]


class RunStatsAggregator(Protocol):
    """Folds a top solver's ``tick_notes`` into run-level stats and owns their push cadence."""

    meta_key: str  # the snapshot is written to run_config.meta.<meta_key>

    def fold(self, tick_notes: Mapping[str, Any], clock: Any) -> bool:
        """Fold one committed tick's notes. ``True`` asks for a push NOW (forced)."""

    def push_due(self, *, force: bool = False) -> bool:
        """Whether a push is due (``force``: unless there is nothing to push)."""

    def mark_pushed(self) -> None:
        """Record that the current state is being pushed."""

    def snapshot(self) -> dict[str, Any]:
        """The JSON value written to ``run_config.meta.<meta_key>``."""

    @classmethod
    def empty_snapshot(cls) -> dict[str, Any]:
        """The snapshot before any tick was folded."""

    # Optional: ``run_end_warnings(self) -> list[str]`` -- WARNING lines the framework logs
    # once, at the final flush (agent close). Absent: nothing is logged.


def _meta_key_problem(key: Any) -> str | None:
    """Why ``key`` is not a free ``run_config.meta`` key (``None``: it is)."""
    if type(key) is not str or not _META_KEY.fullmatch(key):
        return "not a plain identifier"
    if key in RESERVED_META_KEYS:
        return "reserved"
    return None


def check_meta_key(cls: Any, where: str) -> None:
    """Refuse run-stats aggregator ``cls`` unless its ``meta_key`` is a free ``run_config.meta`` key (M2).

    Raises ``PolicyError`` (at bind, before any agent spawns) for a missing or
    non-string key, one that is not a plain identifier, and one in
    :data:`RESERVED_META_KEYS`.
    """
    key = getattr(cls, "meta_key", None)
    name = getattr(cls, "__qualname__", repr(cls))
    problem = _meta_key_problem(key)
    if problem == "not a plain identifier":
        raise PolicyError(
            f"{where}: bind() declared run stats {name} whose meta_key {key!r} is not a plain identifier "
            "(letters, digits and '_', not starting with a digit). The snapshot is written to "
            "run_config.meta.<meta_key>; pick a name of your own, e.g. the solver's name in snake_case."
        )
    if problem == "reserved":
        raise PolicyError(
            f"{where}: bind() declared run stats {name} whose meta_key {key!r} is a key the framework writes "
            f"under run_config.meta (reserved: {', '.join(sorted(RESERVED_META_KEYS))}). Its snapshot would "
            "overwrite that record; pick a name of your own."
        )


class _Retired:
    """Stands in ``store`` for an aggregator that raised: never folded or pushed again."""

    __slots__ = ("meta_key", "error")

    def __init__(self, meta_key: str, error: str) -> None:
        self.meta_key = meta_key
        self.error = error

    def snapshot(self) -> dict[str, Any]:
        """What the run record now holds for this key (``invalid_snapshot``)."""
        return invalid_snapshot(self.error)


def invalid_snapshot(error: str) -> dict[str, Any]:
    """The value written to ``run_config.meta.<meta_key>`` once its aggregator raised."""
    return {"invalid": True, "error": error}


def _retire(store: MutableMapping[str, Any], key: str, exc: BaseException, push: Push, what: str) -> _Retired:
    error = f"{what} raised {type(exc).__name__}: {exc}"
    logger.error(
        "RUN STATS DISABLED: the run-stats aggregator for run_config.meta.%s %s. Its stats are marked "
        "invalid in the run record and it is not called again this run; assignment continues unchanged. "
        "This is a solver bug: run `openride policy validate planner <name>`.",
        key, error, exc_info=(type(exc), exc, exc.__traceback__),
    )
    retired = store[key] = _Retired(key, error)
    try:
        push(key, invalid_snapshot(error))
    except Exception:  # noqa: BLE001 - provenance never breaks a tick
        logger.debug("invalid-stats marker for meta.%s not written", key, exc_info=True)
    return retired


def _push_if_due(agg: Any, key: str, push: Push, *, force: bool) -> None:
    # Under the validated CLASS key, never the instance's ``agg.meta_key`` (review N4).
    if agg.push_due(force=force):
        agg.mark_pushed()
        push(key, agg.snapshot())


def record_tick(store: MutableMapping[str, Any], cls: type, tick_notes: Mapping[str, Any],
                clock: Any, push: Push) -> Any:
    """Fold one committed tick into ``store``'s aggregator for ``cls`` and push when due.

    ``store`` maps ``meta_key`` -> aggregator instance (the app's, so it survives runner
    rebuilds); the instance is created from ``cls`` on first use. Returns it (``None``
    when ``cls.meta_key`` no longer passes :func:`check_meta_key`'s rule: logged, nothing
    written). Every snapshot goes to ``run_config.meta.<cls.meta_key>`` (review N4). Never
    raises because of the aggregator: see the module's containment rule (L7).
    """
    key = getattr(cls, "meta_key", None)
    if _meta_key_problem(key) is not None:
        logger.error(
            "RUN STATS REFUSED: run-stats aggregator %s now has meta_key %r, which is not a free "
            "run_config.meta key (it was checked at bind); nothing is written. This is a solver bug.",
            getattr(cls, "__qualname__", cls), key)
        return None
    agg = store.get(key)
    if isinstance(agg, _Retired):
        return agg
    what = "construction"
    try:
        if agg is None:
            agg = store[key] = cls()
        what = "fold"
        forced = agg.fold(tick_notes, clock)
        what = "push"
        if forced:
            _push_if_due(agg, key, push, force=True)
        _push_if_due(agg, key, push, force=False)
    except Exception as exc:  # noqa: BLE001 - contained (L7)
        return _retire(store, key, exc, push, what)
    return agg


def flush(store: MutableMapping[str, Any], push: Push) -> None:
    """A forced push of every aggregator in ``store`` (agent close: the final stamp); contained like ticks."""
    for key, agg in list(store.items()):
        if isinstance(agg, _Retired):
            continue
        try:
            _push_if_due(agg, key, push, force=True)
        except Exception as exc:  # noqa: BLE001 - contained (L7)
            _retire(store, key, exc, push, "final push")
            continue
        warnings = getattr(agg, "run_end_warnings", None)
        if not callable(warnings):
            continue
        try:
            lines = list(warnings() or ())
        except Exception:  # noqa: BLE001 - a warning hook never breaks the close
            logger.error("run-stats aggregator for run_config.meta.%s: run_end_warnings raised", key, exc_info=True)
            continue
        for line in lines:
            logger.warning("RUN STATS WARNING (run_config.meta.%s): %s", key, line)


def patch_run_config_meta(app: Any, key: str, value: Any) -> bool:
    """Write ``value`` onto the run record's ``run_config.meta.<key>`` (moved verbatim, generic key).

    **Why it is done from the AGENT and not at finalize.** A solver's run-level stats
    accumulate inside the long-lived celery workers (CLAUDE.md §8) and the run record is
    written by a different process, so there is no in-process handoff. The wire cannot
    carry it either: per-trip provenance only rides along on some assignments, so a tick
    that reported a condition while awarding nothing would leave no trace at all.

    So the agent PATCHes the run record itself, using the admin REST client it already
    holds (``app.user``). ``run_config.meta`` is an unschema'd dict in the Eve model, so
    this needs **no new resource and no api container rebuild** (CLAUDE.md §8).

    Best-effort by construction: any failure is logged at debug and swallowed --
    provenance must never be able to break an assignment tick.
    """
    try:
        from apps.common.resource_client_mixin import get_http_session
        from apps.config import settings

        base = settings["OPENRIDE_SERVER_URL"]
        timeout = settings.get("NETWORK_REQUEST_TIMEOUT", 10)
        session = get_http_session()
        found = session.get(
            f"{base}/run-config",
            headers=app.user.get_headers(),
            params={"where": json.dumps({"run_id": app.run_id})},
            timeout=timeout,
        )
        items = (found.json() or {}).get("_items") or []
        if not items:
            return False
        doc = items[0]
        # Dotted patch: leaves every other meta key untouched, and mirrors how
        # SimulationRuntime.update_status patches `step_metrics.<k>`.
        response = session.patch(
            f"{base}/run-config/{doc['_id']}",
            headers=app.user.get_headers(etag=doc["_etag"]),
            data=json.dumps({f"meta.{key}": value}),
            timeout=timeout,
        )
        return response.status_code in (200, 201)
    except Exception:
        logging.debug("Run-stats provenance patch of meta.%s failed (non-fatal).", key, exc_info=True)
        return False
