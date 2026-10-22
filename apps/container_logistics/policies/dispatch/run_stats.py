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
write. At agent close (:func:`flush`): a forced write of every aggregator. A tick whose
result was discarded (message 18) is never folded.

:func:`patch_run_config_meta` is the run-record writer, moved verbatim from
``AssignmentApp._patch_round_stats_to_run_config`` (``assignment/app.py`` at 1e80917)
with the meta key made generic.

Leaf module: stdlib only at import (the HTTP client and settings are imported inside
:func:`patch_run_config_meta`); never imports ``apps.container_logistics.assignment.*``.
Framework side of the boundary: names no solver and no solver concept.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Mapping, MutableMapping, Protocol

__all__ = ["RunStatsAggregator", "flush", "patch_run_config_meta", "record_tick"]

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


def _push_if_due(agg: Any, push: Push, *, force: bool) -> None:
    if agg.push_due(force=force):
        agg.mark_pushed()
        push(agg.meta_key, agg.snapshot())


def record_tick(store: MutableMapping[str, Any], cls: type, tick_notes: Mapping[str, Any],
                clock: Any, push: Push) -> Any:
    """Fold one committed tick into ``store``'s aggregator for ``cls`` and push when due.

    ``store`` maps ``meta_key`` -> aggregator instance (the app's, so it survives runner
    rebuilds); the instance is created from ``cls`` on first use. Returns it.
    """
    agg = store.get(cls.meta_key)
    if agg is None:
        agg = store[cls.meta_key] = cls()
    if agg.fold(tick_notes, clock):
        _push_if_due(agg, push, force=True)
    _push_if_due(agg, push, force=False)
    return agg


def flush(store: Mapping[str, Any], push: Push) -> None:
    """A forced push of every aggregator in ``store`` (agent close: the final stamp)."""
    for agg in list(store.values()):
        _push_if_due(agg, push, force=True)


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
