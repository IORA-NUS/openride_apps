"""The frontend read API — one contract over the hot tier, DuckDB and the archive.

Every payload here is the shape the dashboard already expects, so the cutover can be proved
by diffing against the Next.js route it replaces rather than by inspection. The shapes for
``/breakdown`` and ``/breakdown/series`` are carried in from ``apps/kpi_sink/breakdown_api.py``,
which has served them correctly on :8615 since 2026-07-12; only the store call changed, because
the dataplane holds one database with a ``run_id`` column instead of a file per run.

Measured on this machine against a real 500-truck run (2026-08-07): a whole-run scalar read is
~1.8 ms and a breakdown snapshot ~1.3 ms, against a 200 ms budget. Nothing here touches the
ingest path: reads take their own cursor and cannot stall the poll loop.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Points per series handed to the browser. uPlot cannot draw more than the panel has pixels,
#: and shipping a whole run's raw points was the reason the series endpoints were slow.
MAX_SERIES_POINTS = 600

#: Scopes ``/breakdown`` reports. The Mongo route this replaces returns
#: ``{runId, truck, haulier, planner}``, so omitting planner would drop a key the caller
#: already receives — caught by diffing the live payloads before the cutover, 2026-08-07.
BREAKDOWN_PAYLOAD_SCOPES = ("truck", "haulier", "planner")


# ── helpers ─────────────────────────────────────────────────────────────────


def _epoch_ms(value: Any) -> Optional[int]:
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    return None


def _cutoff_dt(sim_time_ms: Optional[int]) -> Optional[datetime]:
    """Replay cutoff: rows at or before the scrubber's position, or None for 'latest'."""
    if sim_time_ms is None:
        return None
    return datetime.utcfromtimestamp(float(sim_time_ms) / 1000.0)


def _downsample(points: List[Dict[str, Any]], max_n: int) -> List[Dict[str, Any]]:
    if len(points) <= max_n:
        return points
    step = len(points) / max_n
    out = [points[int(i * step)] for i in range(max_n - 1)]
    out.append(points[-1])
    return out


def _int_param(params: Dict[str, List[str]], name: str) -> Optional[int]:
    raw = (params.get(name) or [None])[0]
    if raw in (None, ""):
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _sim_time_ms(params: Dict[str, List[str]]) -> Optional[int]:
    """The replay cutoff, under either spelling.

    The dashboard's existing DuckDB client (``analytics/lib/breakdownSource.ts``) sends
    ``simTimeMs``; this API's own convention is ``sim_time_ms``. Honouring only one of them
    silently returns the LATEST snapshot to a scrubbing replay client instead of the one at
    the cursor — measured 2026-08-07: ?simTimeMs=1578400000000 answered 1578489120000.
    """
    for name in ("sim_time_ms", "simTimeMs"):
        value = _int_param(params, name)
        if value is not None:
            return value
    return None


def _str_param(params: Dict[str, List[str]], name: str) -> Optional[str]:
    raw = (params.get(name) or [None])[0]
    return raw or None


# ── endpoints ───────────────────────────────────────────────────────────────


def _ensure_available(ctx, store, run_id: str) -> bool:
    """Fill the cache from the durable record if this run is not resident. Returns True if it did.

    DuckDB is a working set, not the archive: it evicts freely precisely because a cold run
    costs 0.15 s to pull back (measured end to end at 0.86 s including HTTP). That property is
    worthless if only one endpoint uses it, which is what shipped first — ``/replay/frames``
    rehydrated and every other read answered empty, so an evicted run looked like a run that
    never existed. Every run-scoped read goes through here now.

    Cheap on the hot path: a resident run costs one indexed count and no archive call.
    """
    archive = getattr(ctx, "archive", None)
    if archive is None:
        return False
    try:
        if store.kpi_row_count(run_id) or store.breakdown_row_count(run_id):
            return False
        if store.frame_range(run_id) is not None:
            return False
    except Exception:  # noqa: BLE001 - a counting failure must not block the read
        logger.debug("residency check failed for %s", run_id, exc_info=True)
        return False
    try:
        if not archive.has_run(run_id):
            return False
        store.rehydrate_run(run_id, archive)
        logger.info("rehydrated %s from the archive on read", run_id)
        return True
    except Exception:  # noqa: BLE001 - serve what we have rather than 500 on a cold miss
        logger.exception("rehydrate on read failed for %s", run_id)
        return False


def runs(store) -> Dict[str, Any]:
    """Every run the store knows, newest first. Replaces part of /api/runs-list."""
    rows = store.query(
        """
        SELECT run_id, status, first_seen, last_seen, n_trucks, max_frame_idx
        FROM run_meta
        ORDER BY COALESCE(last_seen, first_seen) DESC
        """
    )
    return {
        "runs": [
            {
                "runId": r.get("run_id"),
                "status": r.get("status"),
                "firstSeen": _epoch_ms(r.get("first_seen")),
                "lastSeen": _epoch_ms(r.get("last_seen")),
                "nTrucks": r.get("n_trucks"),
                "maxFrameIdx": r.get("max_frame_idx"),
            }
            for r in rows
        ]
    }


def kpi_scalars(store, run_id: str, sim_time_ms: Optional[int] = None) -> Dict[str, Any]:
    """Scalar KPI rows grouped by metric — the shape of /api/getMetricsByRunId.

    That route returns ``Record<metric, KpiDoc[]>`` straight out of Mongo, so this returns the
    same mapping with the same field names. The Mongo-only ``_id``/``_created``/``_updated``
    keys are not reproduced; nothing in the dashboard reads them.
    """
    cutoff = _cutoff_dt(sim_time_ms)
    rows = store.query(
        """
        SELECT run_id, metric, value, sim_clock
        FROM kpi_events
        WHERE run_id = ? AND (? IS NULL OR sim_clock <= ?)
        ORDER BY metric, sim_clock
        """,
        [run_id, cutoff, cutoff],
    )
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        metric = str(r.get("metric"))
        out.setdefault(metric, []).append(
            {
                "run_id": r.get("run_id"),
                "metric": metric,
                "value": float(r.get("value") or 0.0),
                "sim_clock": _epoch_ms(r.get("sim_clock")),
            }
        )
    return out


def kpi_series(
    store,
    run_id: str,
    metrics: Optional[Sequence[str]] = None,
    sim_time_ms: Optional[int] = None,
    max_points: int = MAX_SERIES_POINTS,
) -> Dict[str, Any]:
    """Per-metric time series, downsampled server-side to a point budget for uPlot."""
    cutoff = _cutoff_dt(sim_time_ms)
    sql = [
        "SELECT metric, value, sim_clock FROM kpi_events",
        "WHERE run_id = ? AND (? IS NULL OR sim_clock <= ?)",
    ]
    params: List[Any] = [run_id, cutoff, cutoff]
    wanted = [m for m in (metrics or []) if m]
    if wanted:
        sql.append("AND metric IN (" + ",".join("?" for _ in wanted) + ")")
        params.extend(wanted)
    sql.append("ORDER BY metric, sim_clock")
    rows = store.query(" ".join(sql), params)

    by_metric: Dict[str, List[Dict[str, Any]]] = {}
    t_min: Optional[int] = None
    t_max: Optional[int] = None
    for r in rows:
        t = _epoch_ms(r.get("sim_clock"))
        if t is None:
            continue
        t_min = t if t_min is None else min(t_min, t)
        t_max = t if t_max is None else max(t_max, t)
        by_metric.setdefault(str(r.get("metric")), []).append(
            {"t": t, "v": float(r.get("value") or 0.0)}
        )
    return {
        "runId": run_id,
        "tMin": t_min,
        "tMax": t_max,
        "series": [
            {"metric": m, "points": _downsample(pts, max_points)}
            for m, pts in sorted(by_metric.items())
        ],
    }


def breakdown_latest(
    store, run_id: str, scope: str, sim_time_ms: Optional[int] = None, prefer_final: bool = False
) -> Dict[str, Any]:
    """Latest breakdown snapshot for one scope (at or before sim_time_ms when replaying).

    Ordered by ``entity_id`` because a table scan has no inherent order: without it the same
    request returned the same 499 trucks in a different sequence each time, which the Mongo
    route it replaces does not do.
    """
    cutoff = _cutoff_dt(sim_time_ms)
    # ``final=1`` asks for the authoritative end-of-run recompute, which the compare view's
    # baseline read depends on. It is a PREFERENCE, not a filter: a run still in flight has no
    # final snapshot yet, and answering nothing there would blank the live comparison strip.
    # The window picks the newest final row when one exists and the newest row otherwise.
    rows = store.query(
        """
        WITH scoped AS (
            SELECT payload, final, sim_clock, entity_id FROM kpi_breakdown_rows
            WHERE run_id = ? AND scope = ? AND (? IS NULL OR sim_clock <= ?)
        ), chosen AS (
            SELECT max(sim_clock) AS mc FROM scoped
            WHERE (? = FALSE) OR final = TRUE
        ), fallback AS (
            SELECT max(sim_clock) AS mc FROM scoped
        )
        SELECT payload, final, sim_clock FROM scoped
        WHERE sim_clock = COALESCE((SELECT mc FROM chosen), (SELECT mc FROM fallback))
        ORDER BY entity_id
        """,
        [run_id, scope, cutoff, cutoff, bool(prefer_final)],
    )
    if not rows:
        return {"entities": [], "final": False, "simTimeMs": None}
    entities: List[Dict[str, Any]] = []
    for r in rows:
        try:
            entities.append(json.loads(r["payload"]))
        except (TypeError, ValueError, KeyError):
            continue
    return {
        "entities": entities,
        "final": bool(rows[0].get("final")),
        "simTimeMs": _epoch_ms(rows[0].get("sim_clock")),
    }


def breakdown_payload(
    store,
    run_id: str,
    sim_time_ms: Optional[int] = None,
    scopes: Optional[Sequence[str]] = None,
    prefer_final: bool = False,
) -> Dict[str, Any]:
    """The /api/breakdown payload: one snapshot per requested scope."""
    out: Dict[str, Any] = {"runId": run_id}
    wanted = [s for s in (scopes or BREAKDOWN_PAYLOAD_SCOPES) if s]
    for scope in BREAKDOWN_PAYLOAD_SCOPES:
        out[scope] = (
            breakdown_latest(store, run_id, scope, sim_time_ms, prefer_final)
            if scope in wanted
            else {"entities": [], "final": False, "simTimeMs": None}
        )
    return out


def breakdown_series(
    store, run_id: str, sim_time_ms: Optional[int] = None, max_points: int = MAX_SERIES_POINTS
) -> Dict[str, Any]:
    """Per-haulier metric series over time — the /api/breakdown/series payload."""
    cutoff = _cutoff_dt(sim_time_ms)
    rows = store.query(
        """
        SELECT sim_clock, entity_id, haulier_name, empty_ratio, orders_per_day,
               active_hours, dual_cycle_rate, empty_km, total_km
        FROM kpi_breakdown_rows
        WHERE run_id = ? AND scope = 'haulier' AND (? IS NULL OR sim_clock <= ?)
        ORDER BY sim_clock
        """,
        [run_id, cutoff, cutoff],
    )
    by_haulier: Dict[str, Dict[str, Any]] = {}
    t_min: Optional[int] = None
    t_max: Optional[int] = None
    for r in rows:
        t = _epoch_ms(r.get("sim_clock"))
        if t is None:
            continue
        t_min = t if t_min is None else min(t_min, t)
        t_max = t if t_max is None else max(t_max, t)
        hid = str(r.get("entity_id"))
        s = by_haulier.get(hid)
        if s is None:
            s = {"id": hid, "name": r.get("haulier_name") or hid, "points": []}
            by_haulier[hid] = s
        s["points"].append(
            {
                "t": t,
                "empty_ratio": float(r.get("empty_ratio") or 0.0),
                "orders_per_day": float(r.get("orders_per_day") or 0.0),
                "active_hours": float(r.get("active_hours") or 0.0),
                "dual_cycle_rate": float(r.get("dual_cycle_rate") or 0.0),
                # Absolute km travel with the ratios: a FLEET empty ratio is
                # Sum(empty_km) / Sum(total_km), which per-haulier ratios cannot
                # reconstruct without their weights. The Structure Lens donut reads these.
                "empty_km": float(r.get("empty_km") or 0.0),
                "total_km": float(r.get("total_km") or 0.0),
            }
        )
    series = [{**s, "points": _downsample(s["points"], max_points)} for s in by_haulier.values()]
    series.sort(key=lambda s: str(s["name"]))
    return {"runId": run_id, "tMin": t_min, "tMax": t_max, "series": series}


def lanes(store, run_id: str, sim_time_ms: Optional[int] = None) -> Dict[str, Any]:
    """Lane aggregates for one run.

    Reads the ``lane`` breakdown scope. Ingest only began storing that scope alongside this
    endpoint; runs consumed before then have no lane rows and answer with an empty list rather
    than an error, because an old run legitimately has none.
    """
    snap = breakdown_latest(store, run_id, "lane", sim_time_ms)
    # Shape is LanesPayload from analytics/types/breakdown.ts — runId, lanes, simTimeMs, final.
    # `final` is not decorative: the compare view's baseline read distinguishes the
    # authoritative end-of-run snapshot from an interim one.
    return {
        "runId": run_id,
        "lanes": snap["entities"],
        "simTimeMs": snap["simTimeMs"],
        "final": snap["final"],
    }



def replay_frames(
    ctx, run_id: str, frame_from: Optional[int], frame_to: Optional[int], limit: int = 200
) -> Dict[str, Any]:
    """Position frames for a run, in the store's own column layout.

    Columnar on purpose: ``read_frames`` returns ``fetchnumpy()`` columns, which are already
    the hot tier's layout and map onto deck.gl's binary attributes without building one object
    per truck. Sending an array of objects here would undo that at the last step.

    A run the working set no longer holds is rehydrated from the archive first — measured at
    0.15 s for a whole run, which is why DuckDB is free to evict.
    """
    store = ctx.duck
    # handle() has already filled the cache if this run was cold; this only reports whether
    # the run has frames at all.
    rng = store.frame_range(run_id)
    rehydrated = False
    if rng is None:
        return {"runId": run_id, "frames": [], "from": None, "to": None, "rehydrated": rehydrated}

    lo, hi = rng
    start = lo if frame_from is None else max(lo, int(frame_from))
    end = hi if frame_to is None else min(hi, int(frame_to))
    if limit > 0:
        end = min(end, start + limit - 1)
    if end < start:
        return {"runId": run_id, "frames": [], "from": start, "to": end, "rehydrated": rehydrated}

    cols = store.read_frames(run_id, start, end)
    idx = cols.get("frame_idx")
    frames: List[Dict[str, Any]] = []
    if idx is not None and len(idx):
        # One pass over the flat columns: the rows arrive ordered by (frame_idx, slot), so a
        # frame is a contiguous slice and no grouping structure is needed.
        boundaries = [0]
        for i in range(1, len(idx)):
            if idx[i] != idx[i - 1]:
                boundaries.append(i)
        boundaries.append(len(idx))
        for b in range(len(boundaries) - 1):
            a, z = boundaries[b], boundaries[b + 1]
            frames.append(
                {
                    "frameIdx": int(idx[a]),
                    "simTimeMs": float(cols["sim_time_ms"][a]),
                    "n": int(z - a),
                    "slot": cols["slot"][a:z].tolist(),
                    "lng": cols["lng"][a:z].tolist(),
                    "lat": cols["lat"][a:z].tolist(),
                    "state": cols["state"][a:z].tolist(),
                    "haulier": cols["haulier"][a:z].tolist(),
                }
            )
    meta = store.get_run_meta(run_id) or {}
    codes = meta.get("haulier_codes")
    return {
        "runId": run_id,
        "from": start,
        "to": end,
        "available": {"first": lo, "last": hi},
        "rehydrated": rehydrated,
        "haulierCodes": json.loads(codes) if isinstance(codes, str) and codes else {},
        "frames": frames,
    }


def compare(ctx, run_ids: Sequence[str]) -> Dict[str, Any]:
    """Final scalar value per metric for several runs, plus the delta against the first.

    One query over all runs rather than N fetches and a diff in the browser — the whole
    reason the compare view is worth moving.
    """
    ids = [r for r in run_ids if r]
    if not ids:
        return {"runIds": [], "metrics": {}}
    placeholders = ",".join("?" for _ in ids)
    rows = ctx.duck.query(
        f"""
        SELECT run_id, metric, value FROM (
            SELECT run_id, metric, value,
                   row_number() OVER (PARTITION BY run_id, metric ORDER BY sim_clock DESC) AS rn
            FROM kpi_events WHERE run_id IN ({placeholders})
        ) WHERE rn = 1
        """,
        list(ids),
    )
    by_metric: Dict[str, Dict[str, float]] = {}
    for r in rows:
        by_metric.setdefault(str(r.get("metric")), {})[str(r.get("run_id"))] = float(
            r.get("value") or 0.0
        )
    base = ids[0]
    out: Dict[str, Any] = {}
    for metric, values in sorted(by_metric.items()):
        entry: Dict[str, Any] = {"values": values}
        if base in values:
            entry["delta"] = {
                rid: round(values[rid] - values[base], 6) for rid in ids if rid in values
            }
        out[metric] = entry
    return {"runIds": list(ids), "baseline": base, "metrics": out}



def _codebook_delta(
    hot,
    run_id: str,
    known_slots: Dict[int, str],
    known_codes: Dict[str, int],
) -> Optional[Dict[str, Any]]:
    """What the client still needs to resolve slots and haulier codes, or None if nothing.

    Returned as a *delta* against what this connection has already been told, with
    ``full=True`` when the mapping it holds has been contradicted rather than merely
    extended. A contradiction means the slab was rebuilt (process restart without a
    persisted slot map, or an LRU revival that re-learned slots in arrival order), so the
    same uint32 now means a different truck and the client must discard what it has.

    Read from the hot tier, never from the live bus: ``live_frames`` starts its bus cursor
    at ``bus.head(run_id)``, so anything published before a (re)connection is invisible to
    it. Per-connection state sidesteps that window entirely -- a reconnecting client is
    handed a full code book as the first write of the new response.
    """
    try:
        slot_map = hot.slot_map(run_id)  # agent_id -> slot
        haulier_codes = hot.haulier_codes(run_id)  # haulier_id -> code
    except Exception:  # noqa: BLE001 - never let a book lookup kill the stream
        return None

    slots: Dict[int, str] = {}
    contradicted = False
    for agent_id, slot in slot_map.items():
        try:
            slot = int(slot)
        except (TypeError, ValueError):
            continue
        slots[slot] = str(agent_id)
        prior = known_slots.get(slot)
        if prior is not None and prior != str(agent_id):
            contradicted = True

    for haulier_id, code in haulier_codes.items():
        prior_code = known_codes.get(str(haulier_id))
        if prior_code is not None and prior_code != int(code):
            contradicted = True

    if contradicted:
        return {
            "full": True,
            "slots": {str(k): v for k, v in sorted(slots.items())},
            "haulierCodes": {str(k): int(v) for k, v in haulier_codes.items()},
        }

    new_slots = {k: v for k, v in slots.items() if k not in known_slots}
    new_codes = {
        str(k): int(v) for k, v in haulier_codes.items() if str(k) not in known_codes
    }
    if not new_slots and not new_codes:
        return None
    return {
        "full": False,
        "slots": {str(k): v for k, v in sorted(new_slots.items())},
        "haulierCodes": new_codes,
    }


def live_frames(
    ctx,
    run_id: str,
    interval_s: float = 0.5,
    max_seconds: float = 3600.0,
    positions: str = "events",
):
    """Server-sent events carrying the live run's positions, in columnar shape.

    ``positions`` selects who owns truck positions on this connection:

    * ``"events"`` (default, unchanged behaviour) -- every ``trip_geo_stream`` payload is
      forwarded verbatim under ``event: trip``, including the ``truck_loc`` ones the
      dashboard's ``parseLiveTruckLocMessage`` reads. Frames are emitted too, and are
      redundant.
    * ``"frames"`` -- ``truck_loc`` payloads are dropped from the forwarded ``trip``
      events and positions travel only in ``event: frame``. This is where the measured
      ~50x message-count reduction on the live-map path actually comes from: without the
      suppression both writers run and the client sees a doubled fleet over two different
      key spaces (slot vs agent id). An ``event: codebook`` is written *before* the first
      frame so the client can resolve slots to agent ids; see :func:`_codebook_delta`.

    Three deliberate choices, each measured rather than assumed:

    * **Columnar, not an array of objects.** The dashboard's cost was never parsing — it was
      ``truckLocSlice`` holding one object per truck plus 300 history entries each, up to
      150,000 immutably-updated objects. Parallel arrays parse in one call and map straight
      onto deck.gl's binary attributes with no per-truck allocation.
    * **SSE, not a binary WebSocket.** The backend has ~6x headroom at the measured live rate
      of 132 msg/s, and a 500-truck frame is ~11 KB either way. EventSource also already has
      working reconnect and backoff in the frontend. Binary stays available if profiling ever
      says the wire is the bottleneck; today it is not.
    * **A fixed emit rate, not one frame per message.** 132 msg/s of state changes become 2
      frames/s of current state. Coalescing here is what client-side buffering cannot do:
      it removes the parse and the garbage as well as the render.

    Reads the hot tier only. Never touches DuckDB or Mongo, so a slow disk cannot stall the
    map, and a disconnected client cannot back-pressure ingest.
    """
    import time as _time

    hot = getattr(ctx, "hot", None)
    if hot is None:
        yield 'event: error\ndata: {"error":"hot tier unavailable"}\n\n'
        return

    frames_own_positions = str(positions).strip().lower() == "frames"

    bus = getattr(ctx, "live_bus", None)
    # Start from the CURRENT head, not from zero: a viewer joining mid-run wants the live
    # state, and replaying a buffered history through the map's event handlers is what the
    # frontend's own late-join logic already avoids.
    cursor = bus.head(run_id) if bus is not None else 0

    deadline = _time.monotonic() + max_seconds
    last_idx: Optional[int] = None
    sent = 0
    # Per-connection code book state. Never shared between connections: two viewers can be
    # at different points in the same run's slot allocation, and a shared book would let one
    # connection's "already sent" suppress the other's first write.
    known_slots: Dict[int, str] = {}
    known_codes: Dict[str, int] = {}
    codebook_epoch = 0

    def _emit_codebook() -> Optional[str]:
        """Nonlocal-updating code-book writer. Returns the SSE frame, or None if nothing new."""
        nonlocal known_slots, known_codes, codebook_epoch
        delta = _codebook_delta(hot, run_id, known_slots, known_codes)
        if delta is None:
            return None
        if delta["full"]:
            known_slots = {}
            known_codes = {}
            codebook_epoch += 1
        for slot_str, agent_id in delta["slots"].items():
            known_slots[int(slot_str)] = agent_id
        for haulier_id, code in delta["haulierCodes"].items():
            known_codes[haulier_id] = int(code)
        payload = {
            "runId": run_id,
            # CONNECTION-scoped: this counter lives on the response, not on the run, and
            # restarts at 0 on every reconnect. A client must not treat it as a global
            # generation number — correctness comes from the contradiction check, not this.
            "connectionEpoch": codebook_epoch,
            "full": bool(delta["full"]),
            "slots": delta["slots"],
            "haulierCodes": delta["haulierCodes"],
        }
        return "event: codebook\ndata: " + json.dumps(payload) + "\n\n"

    while _time.monotonic() < deadline:
        # Bus first: trip / facility / status / simulation_terminal, forwarded verbatim so the
        # dashboard's existing parsers work unchanged.
        if bus is not None:
            events, cursor = bus.since(run_id, cursor)
            for _seq, name, payload in events:
                if name == "kpi":
                    # NAMELESS on purpose: the dashboard consumes KPI through
                    # `EventSource.onmessage`, which only fires for events with no `event:`
                    # name. Naming this one would keep the clock frozen. Matches what the
                    # Next.js Kafka path emitted (`onKpi` -> bare `data:` line).
                    yield "data: " + json.dumps(payload, default=str) + "\n\n"
                    continue
                if (
                    frames_own_positions
                    and name == "trip"
                    and isinstance(payload, dict)
                    and payload.get("type") == "truck_loc"
                ):
                    # The whole point of frames mode. `trip_route` / `trip_end` / anything
                    # else still forwards: only positions change owner.
                    continue
                yield f"event: {name}\ndata: " + json.dumps(payload, default=str) + "\n\n"
        try:
            frame = hot.snapshot(run_id)
        except Exception:
            # Not resident: the run has not started, or has ended and been released. Say so
            # and keep the connection open — a viewer who opens the page early must not have
            # to reload when the run begins.
            yield 'event: waiting\ndata: {"resident":false}\n\n'
            _time.sleep(interval_s)
            continue
        # A frame with no trucks is not information, it is the slab mid-revival:
        # `adopt_slot_map` restores `n_active` but leaves `written` all-False, so the first
        # snapshot after a restart or an LRU revival reports 0 and then refills one truck per
        # message. `capture_run` has always skipped these (service.py, `if frame.n == 0`);
        # this path did not, and on the map an n=0 frame is CLAUDE.md 6.1 verbatim.
        # The client also merges rather than replaces, so this is the second of two guards.
        if int(frame.n) == 0:
            yield ": keepalive\n\n"
            _time.sleep(interval_s)
            continue
        if last_idx is None or frame.frame_idx != last_idx or sent == 0:
            last_idx = frame.frame_idx
            # Slots are allocated lazily (`_RunSlab.slot_for` appends on first sighting), so
            # the book grows through the run and a connect-time snapshot of it goes stale.
            # Checking the FRAME's slots rather than re-reading the map means the steady
            # state costs one set lookup per frame and never takes the hot tier's lock for a
            # 500-entry dict copy it would only throw away.
            slots_now = frame.slot.tolist()
            if frames_own_positions:
                if sent == 0 or any(int(s) not in known_slots for s in slots_now):
                    book = _emit_codebook()
                    if book is not None:
                        yield book
            payload = {
                "runId": run_id,
                "frameIdx": int(frame.frame_idx),
                "simTimeMs": float(frame.sim_time_ms),
                "n": int(frame.n),
                "slot": slots_now,
                "lng": frame.lng.tolist(),
                "lat": frame.lat.tolist(),
                "state": frame.state.tolist(),
                "haulier": frame.haulier.tolist(),
            }
            yield "event: frame\ndata: " + json.dumps(payload) + "\n\n"
            sent += 1
        else:
            yield ": keepalive\n\n"
        _time.sleep(interval_s)


#: Streaming routes are answered separately from the JSON ones: they own the response body.
_STREAM_ROUTES = {"/live"}


def stream(ctx, path: str, params: Dict[str, List[str]]):
    """Return an SSE generator for a streaming path, or None if the path is not one."""
    if path not in _STREAM_ROUTES:
        return None
    run_id = _str_param(params, "run_id") or _str_param(params, "runId")
    if not run_id:
        return None
    interval = _int_param(params, "interval_ms")
    # Unrecognised values fall through to "events", so a typo degrades to today's behaviour
    # rather than to a map with no trucks on it.
    positions = (_str_param(params, "positions") or "events").strip().lower()
    if positions != "frames":
        positions = "events"
    return live_frames(
        ctx, run_id, (interval / 1000.0) if interval else 0.5, positions=positions
    )



#: A non-terminal run with no activity for this long is treated as dead. Generous: a healthy
#: run publishes run_status/kpi continuously and a 7-day 500-truck run completes in ~5 min
#: wall, so a 10-minute silence cannot be normal operation.
LIVE_RUN_STALE_AFTER_S = float(os.environ.get("LIVE_RUN_STALE_AFTER_S", "600"))

#: Below this, an epoch-ms value is not a wall clock — sim clocks sit at ~1.577e12
#: (2020-01-01) while wall clocks are ~1.79e12 (2026). 2025-01-01 is a safe divider.
_WALL_CLOCK_FLOOR_MS = 1_735_689_600_000


#: `run_YYYYMMDD_HHMMSS` — this project's run-id convention, and the ONLY activity evidence
#: available for a row whose `last_seen` is NULL (see `_live_row_is_stale`).
_RUN_ID_TS = re.compile(r"^run_(?:.*_)?(\d{8})_(\d{6})$")


def _run_id_start_ms(run_id: Any) -> Optional[float]:
    """Wall-clock start encoded in a `run_YYYYMMDD_HHMMSS` id, or None if it doesn't match."""
    m = _RUN_ID_TS.match(str(run_id or ""))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").timestamp() * 1000.0
    except ValueError:
        return None


def _live_row_is_stale(
    last_seen: Any,
    now_ms: Optional[float] = None,
    run_id: Any = None,
) -> bool:
    """True only when we can positively establish the run has been silent past the cutoff.

    Two evidence sources, in order:

    1. ``last_seen``, when it is a recognisable wall clock. Sim-valued epochs (~1.577e12) are
       rejected rather than treated as ancient — see ``live_runs``.
    2. The timestamp embedded in the run id, used ONLY when ``last_seen`` is absent. This is
       the case that actually matters: the hard-killed run that stayed listed for an hour had
       ``last_seen`` AND ``first_seen`` both NULL, so a `last_seen`-only rule failed open on
       precisely the row it was written for. A run whose id says it started longer ago than the
       cutoff, and which has since recorded no activity at all, is not in flight. Safe because
       a 7-day 500-truck run completes in ~5 min wall, well inside the 10-minute cutoff.

    Fails OPEN (False) for anything missing, non-numeric, sim-valued, in the future, or with an
    unparseable id. Losing a dead entry is worth less than hiding a live run.
    """
    now = now_ms if now_ms is not None else time.time() * 1000.0
    cutoff_ms = LIVE_RUN_STALE_AFTER_S * 1000.0

    # `run_meta.last_seen` is declared TIMESTAMP (`store/duck.py:192`), so DuckDB returns a
    # `datetime` — `float(datetime)` raises, which the except swallowed, silently making the
    # last-activity branch DEAD for every real row and sending everything to the run-id
    # fallback. That ages a run out on its START time: a run publishing normally but running
    # longer than the cutoff (a 1000-truck run took 452 s; contention or more demand pushes it
    # past 600 s) vanished from /runs/live mid-flight, flipping `isLiveRun` false and rendering
    # an in-flight run as historical. Exactly the inversion this docstring calls the worse
    # failure. Normalise through the same helper `runs()` uses before attempting a cast.
    ts: Optional[float] = None
    epoch = _epoch_ms(last_seen)
    if epoch is not None:
        ts = float(epoch)
    else:
        try:
            ts = float(last_seen)
        except (TypeError, ValueError):
            ts = None

    if ts is not None and ts >= _WALL_CLOCK_FLOOR_MS:
        return (now - ts) > cutoff_ms
    if ts is not None:
        return False  # sim-valued: cannot age it out arithmetically

    started = _run_id_start_ms(run_id)
    if started is None or started < _WALL_CLOCK_FLOOR_MS:
        return False
    return (now - started) > cutoff_ms


def live_runs(store) -> Dict[str, Any]:
    """Runs currently in flight, in the dashboard's ``LiveRunEntry`` shape.

    Replaces ``peekLatestRunStatusPayload`` in the frontend, which minted a Kafka consumer
    group per run per page load. The dataplane already consumes ``run_status`` continuously,
    so this is a single indexed read of what it has been told — no broker round trip, no group.

    "In flight" is a run whose status is not terminal. A run this process never saw does not
    appear, which is why the caller keeps its host/log fallback.

    STALENESS. A non-terminal status is not sufficient: a run killed with SIGKILL, or one whose
    host died, never publishes a terminal ``run_status`` and so stays "in flight" forever. That
    is not cosmetic — the frontend treats a listed run as live (`isLiveRun`), which suppresses
    the historical replay controls, and newest-run discovery latches onto it. A run killed at
    17:01 was still being advertised at 18:01, and it cost two soak attempts before anyone
    noticed. So a run whose last activity is older than ``LIVE_RUN_STALE_AFTER_S`` is dropped.

    The cutoff is applied DEFENSIVELY, in Python rather than SQL, because ``last_seen`` is not
    uniformly a wall clock: some rows carry a *sim* timestamp (2020-01-01 epochs, ~1.577e12)
    rather than a wall one (~1.786e12). Filtering those arithmetically would silently hide a
    genuinely live run. So a row is dropped ONLY when its ``last_seen`` is recognisably a wall
    clock AND older than the cutoff; anything unparseable, missing or sim-valued fails OPEN and
    stays listed. Losing a dead entry is worth less than hiding a live run.
    """
    rows = store.query(
        """
        SELECT run_id, run_name, scenario_slug, scenario_name, status, last_seen
        FROM run_meta
        WHERE status IS NULL
           OR lower(status) NOT IN (
               'completed','failed','cancelled','canceled','stopped','aborted',
               'terminated','interrupted','crashed','timeout','error','inactive'
           )
        ORDER BY run_id
        """
    )
    rows = [
        r for r in rows
        if not _live_row_is_stale(r.get("last_seen"), run_id=r.get("run_id"))
    ]
    return {
        "runs": [
            {
                "runId": r.get("run_id"),
                "runName": r.get("run_name"),
                "scenarioSlug": r.get("scenario_slug"),
                "scenarioName": r.get("scenario_name"),
                "status": r.get("status"),
            }
            for r in rows
        ]
    }


# ── routing ─────────────────────────────────────────────────────────────────

#: path -> (handler, requires_run_id). Kept explicit rather than derived so an endpoint cannot
#: appear by accident, and so the 404 body can list what does exist.
_ROUTES = {
    "/runs": (lambda store, rid, p: runs(store), False),
    "/runs/live": (lambda store, rid, p: live_runs(store), False),
    "/kpi/scalars": (
        lambda store, rid, p: kpi_scalars(store, rid, _sim_time_ms(p)),
        True,
    ),
    "/kpi/series": (
        lambda store, rid, p: kpi_series(
            store,
            rid,
            [m for m in (_str_param(p, "metrics") or "").split(",") if m],
            _sim_time_ms(p),
            _int_param(p, "max_points") or MAX_SERIES_POINTS,
        ),
        True,
    ),
    "/breakdown": (
        lambda store, rid, p: breakdown_payload(
            store,
            rid,
            _sim_time_ms(p),
            [s for s in (_str_param(p, "scope") or "").split(",") if s] or None,
            _str_param(p, "final") in ("1", "true", "True"),
        ),
        True,
    ),
    "/breakdown/series": (
        lambda store, rid, p: breakdown_series(
            store,
            rid,
            _sim_time_ms(p),
            _int_param(p, "max_points") or MAX_SERIES_POINTS,
        ),
        True,
    ),
    "/lanes": (
        lambda store, rid, p: lanes(store, rid, _sim_time_ms(p)),
        True,
    ),
    # ctx-taking routes: replay may need the archive, compare spans several runs.
    "/replay/frames": (
        lambda ctx, rid, p: replay_frames(
            ctx, rid, _int_param(p, "from"), _int_param(p, "to"), _int_param(p, "limit") or 200
        ),
        True,
        True,
    ),
    "/compare": (
        lambda ctx, rid, p: compare(ctx, (_str_param(p, "run_ids") or "").split(",")),
        False,
        True,
    ),
}


def routes() -> List[str]:
    return sorted(_ROUTES)


def handle(ctx, path: str, params: Dict[str, List[str]]) -> Optional[Tuple[int, Any]]:
    """Answer a read request, or None if the path is not ours (so /health still wins).

    ``ctx`` is the service itself, not a store: the store is reached through ``ctx.duck``,
    which is a lazy property, so the HTTP server never holds a reference across requests and
    can start before DuckDB exists. Replay also needs ``ctx.archive`` to rehydrate a run the
    working set has evicted. A handler that raises becomes a 500, never a dead server.
    """
    entry = _ROUTES.get(path)
    if entry is None:
        return None
    handler, needs_run = entry[0], entry[1]
    wants_ctx = len(entry) > 2 and entry[2]
    run_id = _str_param(params, "run_id") or _str_param(params, "runId")
    if needs_run and not run_id:
        return 400, {"error": "run_id is required", "path": path}
    try:
        store = ctx.duck
    except Exception as exc:  # noqa: BLE001
        logger.exception("read api: store unavailable")
        return 503, {"error": f"store unavailable: {exc}"}
    if store is None:
        return 503, {"error": "store unavailable"}
    if needs_run and run_id:
        _ensure_available(ctx, store, run_id)
    try:
        return 200, handler(ctx if wants_ctx else store, run_id, params)
    except Exception as exc:  # noqa: BLE001 - a bad query must not kill the server
        logger.exception("read api: %s failed", path)
        return 500, {"error": str(exc), "path": path}
