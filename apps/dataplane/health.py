"""``/health`` for the dataplane process — a dead *or silently failing* task must fail the check.

The payload key set is pinned (shared_decisions §9, plus the ``degraded`` list);
``build_health_payload`` is a pure function so the shape
and the verdict can be tested without binding a socket, and the HTTP layer is the same stdlib
``ThreadingHTTPServer`` pattern ``apps/kpi_sink/breakdown_api.py`` already uses.

Status code is the contract: **200 when ``ok`` is true, 503 when it is not.** An operator
polling this endpoint must not have to parse the body to notice a problem — that is exactly
how the 2026-07-01 sink outage stayed invisible for five weeks.

``ok`` is therefore **not** just "every task thread exists". It is false when any of these
hold, and the reason is listed in ``degraded``:

* a task is unhealthy (dead, stalled, or crash-looping);
* runs are **waiting to be archived** and none has been archived for a sweep interval — a
  durable record that has silently stopped being written is the original failure mode, and
  it does not care *why*: an archive that answers ``ping()`` and fails every ``dump_run``
  leaves exactly the same runs pending as an unreachable one, so the lag is the fault and
  ``available`` only sharpens the reason;
* the consumer holds no partition at all (``ingest_not_consuming``), or holds partitions and
  hears nothing while the **broker still has records waiting** past its commit point, or
  while it holds records it consumed and never wrote (``ingest_stalled``), or is silent with
  a broker that cannot even say whether anything is waiting (``ingest_lag_unknown`` — an
  unknown is never read as a zero), or is consuming steadily and still losing ground against
  the broker (``ingest_behind``), or a partition's commit point has stood still for a stall
  interval while records piled up behind it (``ingest_wedged``);
* records are being **thrown away, or refused by the store, at the ingest boundary**
  (``ingest_discarding``) — an undecodable payload, a missing key, a handler that cannot use
  the record, or a handler whose synchronous DuckDB write raised. The first three have their
  offset committed, so they are gone from Kafka too; the last does not, so it is replayed on
  the next start — but both mean rows are not reaching the store *now*, and both must be
  loud. Measured before this rule existed: 100% of ``kpi_stream`` and
  ``kpi_breakdown_stream`` discarded, 0 rows in DuckDB, the run never finalized, every
  service counter at zero and this endpoint answering ``ok=True, degraded=[]``;
* rows that existed and are now gone (``store_rows_dropped``) — today that is exactly a run
  whose hot slab was evicted with positions the capture sweep had not written yet.

**Every verdict rule here is a function of pending data, of loss, or of silence.** None is a
counter awaiting a matching success. That distinction is the whole repair: an early rule,
``store.write_failures >= 3``, was a per-writer streak that only a later success from the
*same* writer could clear, so a writer whose traffic had stopped left the offset gate shut
forever while this endpoint answered 200.

There is no ``writer`` section any more. The asynchronous write path it described — a bounded
queue, a writer thread, a retry backoff, a quarantine, high/low watermarks and an offset
ledger — has been deleted: a handler writes to DuckDB synchronously and its offset is
committed when the write returns, so "queued but not durable" is not a state this process can
be in, and no verdict here can be built out of one.
"""

from __future__ import annotations

import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

DEFAULT_HTTP_HOST = "127.0.0.1"
# 8615 belonged to the kpi-duckdb-sink, RETIRED 2026-08-12 (stopped + disabled; the unit
# file and all data are kept). The port is free now, but do not reuse it: reviving that
# unit is the documented rollback for the single-writer cutover.
DEFAULT_HTTP_PORT = 8620

# A LIVE run's topics have been silent this long. Silence on its own is not a fault: OpenRide
# is idle for hours between runs, and a process that has seen no message yet is fresh, not broken.
INGEST_STALL_AFTER_S = 300.0
# Subscribed this long while holding NO partition — a failed ``subscribe()``, an unreachable
# broker, a fenced member, an absent topic. Nothing will ever arrive, idle or not: this is the
# 2026-07-01 shape (process up, systemd green, /health 200, nothing ingested). Short, because a
# group join that has not completed in twenty seconds is not a rebalance.
NOT_CONSUMING_AFTER_S = 20.0
# Runs waiting for the archive this long with nothing archived — one reconcile sweep
# (``DEFAULT_RECONCILE_INTERVAL_S``), so a sweep has run and failed to clear them.
ARCHIVE_LAG_AFTER_S = 300.0
# Consecutive broker-lag refreshes in which the backlog failed to fall back to its floor,
# while records were still being consumed. Five refreshes is five seconds of a process that
# is taking records in and still losing ground — the one shape no other rule here can see,
# because a slow process is never silent, never discards and never fails to commit. The
# threshold is on the TREND: judged on the level instead, a healthy live run that measures a
# lag of 11 routinely would answer 503.
BROKER_LAG_RISING_REFRESHES = 5
# A record was discarded at the ingest boundary within this window. A RATE, not a total: the
# verdict clears itself once the bad producer stops, so no counter has to be reset by a later
# success, and the cumulative ``discards`` stays in the body for the postmortem.
DISCARD_WINDOW_S = 300.0

_CONSUMER_KEYS = (
    "topics",
    "messages",
    "handler_errors",
    "decode_errors",
    "last_message_age_s",
    # Added this round: a shut offset gate must be visible in the body, not inferred.
    "uncommitted",          # consumed offsets not yet released by a durable write
    "commit_lag",           # {"topic:partition": records consumed past the commit point}
    "broker_lag",           # {"topic:partition": records the BROKER holds past that point}
    "broker_lag_total",     # sum of the above; {} / 0 when no client could answer
    "broker_lag_unknown",   # assigned partitions the last refresh could not measure at all
    "broker_lag_rising",    # consecutive refreshes the backlog has failed to fall
    "commit_stuck_s",       # how long the worst blocked commit point has not moved
    "assigned_partitions",  # 0 means "not consuming anything", which makes silence expected
    # Consumed records thrown away (undecodable, unkeyed, empty, or unusable by the handler),
    # and how long ago the last one was. One clock over every drop path in the process.
    "discards",
    "last_discard_age_s",
)
_ARCHIVE_KEYS = ("available", "lag_runs", "last_dump_at", "pending_run_ids")
_HOT_KEYS = ("resident_runs", "trucks", "frames")
_STORE_KEYS = ("db_path", "run_ids", "open_runs", "rows_dropped", "frame_write_failures")

_CONSUMER_DEFAULTS: Dict[str, Any] = {
    "topics": [],
    "messages": 0,
    "handler_errors": 0,
    "decode_errors": 0,
    "last_message_age_s": None,
    "uncommitted": 0,
    "commit_lag": {},
    "broker_lag": {},
    "broker_lag_total": 0,
    "broker_lag_unknown": 0,
    "broker_lag_rising": 0,
    "commit_stuck_s": None,
    "assigned_partitions": 0,
    "discards": 0,
    "last_discard_age_s": None,
}
_STORE_DEFAULTS: Dict[str, Any] = {
    "db_path": None,
    "run_ids": 0,
    "open_runs": 0,
    "rows_dropped": 0,
    "frame_write_failures": 0,
}


def _project(src: Optional[Dict[str, Any]], keys: Iterable[str], defaults: Dict[str, Any]) -> Dict[str, Any]:
    """Return exactly ``keys`` from ``src``, filling from ``defaults`` — keeps the payload pinned."""
    src = src or {}
    return {key: src.get(key, defaults[key]) for key in keys}


def _task_entry(status: Any, now: float) -> Dict[str, Any]:
    last_hb = getattr(status, "last_heartbeat", None)
    age = None if last_hb is None else round(max(0.0, now - float(last_hb)), 3)
    return {
        "name": getattr(status, "name", "?"),
        "alive": bool(getattr(status, "alive", False)),
        "healthy": bool(getattr(status, "healthy", False)),
        "restarts": int(getattr(status, "restarts", 0) or 0),
        "last_error": getattr(status, "last_error", None),
        "last_heartbeat_age_s": age,
    }


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _age(value: Any) -> Optional[float]:
    """A duration in seconds, or None when the collaborator reports 'not applicable'."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_health_payload(
    statuses: Iterable[Any],
    *,
    uptime_s: float = 0.0,
    consumer: Optional[Dict[str, Any]] = None,
    archive: Optional[Dict[str, Any]] = None,
    hot: Optional[Dict[str, Any]] = None,
    store: Optional[Dict[str, Any]] = None,
    degraded: Optional[Iterable[str]] = None,
    service: str = "dataplane",
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Build the pinned health document.

    ``ok`` is false when any task is unhealthy, when runs sit unarchived, when rows have been
    dropped, when the consumer is not consuming at all or a live run's topics go silent, when
    records are being discarded or refused by the store, or when the caller passes additional
    ``degraded`` reasons.
    """
    now = time.monotonic() if now is None else now
    tasks: List[Dict[str, Any]] = [_task_entry(s, now) for s in statuses]

    consumer_out = _project(consumer, _CONSUMER_KEYS, _CONSUMER_DEFAULTS)
    archive_out = _project(
        archive,
        _ARCHIVE_KEYS,
        {"available": False, "lag_runs": 0, "last_dump_at": None, "pending_run_ids": []},
    )
    store_out = _project(store, _STORE_KEYS, _STORE_DEFAULTS)
    hot_out = _project(hot, _HOT_KEYS, {"resident_runs": [], "trucks": 0, "frames": 0})

    reasons: List[str] = []
    for task in tasks:
        if not task["healthy"]:
            reasons.append(f"task_unhealthy:{task['name']}")

    # Pending data, not reachability: runs waiting to be archived, and how long nothing has
    # been archived. An archive whose every dump fails is reachable and green under the old
    # ``not available`` rule, while the durable record silently stops being written.
    pending = _int(archive_out.get("lag_runs")) or len(archive_out.get("pending_run_ids") or [])
    last_dump = _age(archive_out.get("last_dump_at"))
    lag_s = float(uptime_s) if last_dump is None else max(0.0, time.time() - last_dump)
    if pending and not archive_out.get("available"):
        reasons.append("archive_unavailable_with_pending_runs")
    elif pending and lag_s > ARCHIVE_LAG_AFTER_S:
        reasons.append("archive_lagging")

    # Rows that existed and are gone. The synchronous write path has no drop path of its own,
    # so the only contributor left is a hot slab evicted with uncaptured positions — which is
    # a real loss (trip_geo offsets are committed on receipt, so nothing can replay them).
    if _int(store_out.get("rows_dropped")):
        reasons.append("store_rows_dropped")

    # Silence is judged against whether ingest is EXPECTED, never against whether partitions
    # happen to be assigned. Holding no partition while subscribed is the loud case: nothing
    # will ever arrive, idle or not. Holding partitions and hearing nothing is a fault only
    # while records this process consumed have still not been written — ``commit_lag``.
    # Having seen no message at all is the shape of a fresh process, not a fault.
    #
    # Two in-process states are IDENTICAL — silence, an open run in the store, zero commit
    # lag: a headless run whose ingest died (must go red) and a run killed mid-flight on an
    # idle box (must stay green). Residency cannot tell them apart (``ORSIM_HEADLESS`` sets
    # ``stream_geo: false``, so a headless run never has a slab), and neither can
    # ``store.open_runs``: nothing in this package can close a run that never sent a terminal
    # ``run_status``, so an abandoned run is open for ever and keyed on it this rule 503'd an
    # idle box at 1 h, 24 h and 30 d — the restart-loop this docstring warns about, bought by
    # making the headless case loud.
    #
    # ``consumer.broker_lag`` is the only signal that separates them, because it is the only
    # one that is not in this process: records waiting AT THE BROKER past our commit point.
    # Waiting records plus silence = they are not being fetched = ingest is dead. No waiting
    # records = there is nothing to consume, however long the silence lasts.
    #
    # ``commit_lag`` stays beside it — records THIS PROCESS consumed and failed to write — as
    # the answer for a client that cannot report watermarks. It cannot carry the rule alone:
    # a cleanly dead fetcher produces none of it, it is exactly 0 while 3 000 records of a
    # live run are never delivered.
    #
    # ``last_message_age_s`` is None on a process that has seen no message AT ALL, and that
    # is "silent since start", not "no verdict": gating the stall rule on ``is not None``
    # made the one shape that matters most — joined, assigned every partition, fetch path
    # dead before the first record — unjudgeable, so 3 001 waiting records answered 200 at
    # +301 s, +1 h, +24 h and +7 days. Silence falls back to uptime, exactly as the
    # ``ingest_not_consuming`` branch beside it already did.
    last_age = _age(consumer_out.get("last_message_age_s"))
    silence = float(uptime_s) if last_age is None else last_age
    if consumer_out.get("topics") and not _int(consumer_out.get("assigned_partitions")):
        if silence > NOT_CONSUMING_AFTER_S:
            reasons.append("ingest_not_consuming")
    elif silence > INGEST_STALL_AFTER_S:
        if (
            _int(consumer_out.get("broker_lag_total"))
            or any(_int(lag) > 0 for lag in (consumer_out.get("commit_lag") or {}).values())
        ):
            reasons.append("ingest_stalled")
        elif _int(consumer_out.get("broker_lag_unknown")):
            # Silent, and the broker cannot even say whether anything is waiting. The one
            # signal that separates a dead fetcher from an idle box is UNKNOWN, and the
            # failure is correlated — the broker that stopped answering fetches is the same
            # one that stopped answering watermarks. Unknown may never be read as zero, so
            # a silence this long that nothing can vouch for is a fault.
            reasons.append("ingest_lag_unknown")

    # Falling behind is not silence, so it gets its own rule rather than a clause in the one
    # above. A merely SLOW process consumes constantly — ``last_message_age_s`` ~0.01 s, zero
    # commit lag because every consumed record was written, zero discards — so every rule in
    # this file was structurally unable to reach it: measured 8 745 records behind at t+10 s,
    # 26 006 at t+30 s and 52 591 (72 s of backlog) at t+60 s, answering ok=True degraded=[]
    # at all three. What is judged is the DERIVATIVE — a backlog that will not come back down
    # while records are still being taken in — because the level alone is noise: a live run
    # measures a lag of 11 routinely and a single poll batch is 200 records. Like every rule
    # here it is a function of pending data, so a drained backlog clears it.
    if _int(consumer_out.get("broker_lag_rising")) >= BROKER_LAG_RISING_REFRESHES:
        reasons.append("ingest_behind")

    # A partition whose commit point has not moved while records keep piling up behind it is
    # wedged: one handler that raised freezes it for the life of the process, every restart
    # replays the same record, and nothing else in this payload says so — ``commit_lag`` is
    # only read above, in conjunction with silence a live run never has. Judged on the age of
    # the standstill, not on its mere existence, so the one-poll-cycle window in which a
    # consumed record is buffered but not yet flushed is not a fault.
    stuck = _age(consumer_out.get("commit_stuck_s"))
    if stuck is not None and stuck > INGEST_STALL_AFTER_S:
        reasons.append("ingest_wedged")

    # Records thrown away at the ingest boundary, judged as a rate over a window. A discard is
    # final — the record's offset is committed, so Kafka cannot replay it either — and the
    # only reason the 2026-07-01 shape stayed invisible is that nothing judged it.
    discard_age = _age(consumer_out.get("last_discard_age_s"))
    if _int(consumer_out.get("discards")) and discard_age is not None and discard_age <= DISCARD_WINDOW_S:
        reasons.append("ingest_discarding")

    for extra in degraded or ():
        if extra and extra not in reasons:
            reasons.append(str(extra))

    return {
        "ok": not reasons,
        "service": service,
        "uptime_s": round(float(uptime_s), 3),
        "degraded": reasons,
        "tasks": tasks,
        "consumer": consumer_out,
        "archive": archive_out,
        "hot": hot_out,
        "store": store_out,
    }


def make_health_server(
    provider: Callable[[], Dict[str, Any]],
    host: str = DEFAULT_HTTP_HOST,
    port: int = DEFAULT_HTTP_PORT,
    reader: Optional[Callable[[str, Dict[str, list]], Optional[tuple]]] = None,
    streamer: Optional[Callable[[str, Dict[str, list]], Any]] = None,
) -> ThreadingHTTPServer:
    """A ThreadingHTTPServer serving GET /health from ``provider()``.

    The caller owns the serving loop (``serve_forever`` / ``shutdown``) so it can be supervised.
    """

    class Handler(BaseHTTPRequestHandler):
        server_version = "dataplane-health/1"

        def log_message(self, *args):  # silence the default stderr access log
            pass

        def _send(self, code: int, body: Dict[str, Any]) -> None:
            data = json.dumps(body, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _stream_sse(self, gen) -> None:
            """Write an SSE body until the client leaves or the generator ends.

            A disconnected browser surfaces as BrokenPipe/ConnectionReset on the next write;
            that ends this request thread and nothing else. The generator reads the hot tier
            only, so a viewer who walks away can never back-pressure ingest.
            """
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")  # nginx must not buffer this
            self.end_headers()
            try:
                for chunk in gen:
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:  # noqa: BLE001 - a broken stream must not kill the server
                logger.exception("sse stream failed")
            finally:
                close = getattr(gen, "close", None)
                if close is not None:
                    close()

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            if path not in ("/health", "/healthz"):
                # The read API shares this server: one port, one process, no second listener.
                # A reader that raises is already a status code, never an exception up here.
                # Streaming is tried first: an SSE response owns its body and cannot go
                # through _send, which sets Content-Length and closes.
                if streamer is not None:
                    gen = streamer(path, parse_qs(parsed.query))
                    if gen is not None:
                        self._stream_sse(gen)
                        return
                if reader is not None:
                    answered = reader(path, parse_qs(parsed.query))
                    if answered is not None:
                        code, body = answered
                        self._send(code, body)
                        return
                self._send(404, {"ok": False, "error": "not found"})
                return
            try:
                payload = provider()
            except Exception as exc:  # noqa: BLE001 — a broken provider must not kill the server
                logger.exception("health provider failed")
                self._send(503, {"ok": False, "service": "dataplane", "error": str(exc)})
                return
            if not isinstance(payload, dict):
                self._send(503, {"ok": False, "service": "dataplane", "error": "bad provider payload"})
                return
            self._send(200 if payload.get("ok") else 503, payload)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
