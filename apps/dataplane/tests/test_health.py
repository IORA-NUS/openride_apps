"""/health tests: a dead task must flip the HTTP status, not just a field in the body."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from apps.dataplane.health import (
    BROKER_LAG_RISING_REFRESHES,
    INGEST_STALL_AFTER_S,
    NOT_CONSUMING_AFTER_S,
    build_health_payload,
    make_health_server,
)
from apps.dataplane.supervisor import Supervisor, TaskStatus

# `degraded` (top level) and `rows_dropped` (store) extend the set pinned in
# shared_decisions §9: the verdict has to be explainable, and lost rows have to be able to
# fail it. Every originally-pinned key is still present and unchanged. There is no `writer`
# section: the asynchronous write path it described has been deleted.
TOP_KEYS = {
    "ok", "service", "uptime_s", "degraded", "tasks", "consumer", "archive", "hot", "store",
}
TASK_KEYS = {"name", "alive", "healthy", "restarts", "last_error", "last_heartbeat_age_s"}
CONSUMER_KEYS = {
    "topics", "messages", "handler_errors", "decode_errors", "last_message_age_s",
    "uncommitted", "commit_lag", "commit_stuck_s", "assigned_partitions",
    # Records the BROKER still holds past our commit point — the only measurement of
    # consumer lag in the process, and the only signal that is not inside it.
    "broker_lag", "broker_lag_total",
    # Assigned partitions the last refresh could not measure. Unknown is not zero.
    "broker_lag_unknown",
    # Consecutive refreshes the backlog failed to fall — the derivative, not the level.
    "broker_lag_rising",
    # Records thrown away at the ingest boundary, and how long ago the last one was.
    "discards", "last_discard_age_s",
}
ARCHIVE_KEYS = {"available", "lag_runs", "last_dump_at", "pending_run_ids"}
HOT_KEYS = {"resident_runs", "trucks", "frames"}
# `write_failures` is gone: it was a per-writer streak only a later success from the SAME
# writer could clear, and _store_stats projected the MAX over three of them. `rows_dropped`
# now carries the one loss the process can still suffer — a hot slab evicted with positions
# the sweep never captured.
STORE_KEYS = {"db_path", "run_ids", "open_runs", "rows_dropped", "frame_write_failures"}


def _wait_until(pred, timeout=3.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def _get(port, path="/health"):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class _Server:
    def __init__(self, provider):
        self.server = make_health_server(provider, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@pytest.fixture
def serve():
    made = []

    def _make(provider):
        s = _Server(provider)
        made.append(s)
        return s

    yield _make
    for s in made:
        s.close()


class TestPayloadShape:
    def test_pinned_key_set(self):
        payload = build_health_payload(
            [TaskStatus("consumer", True, True, 1.0, 2.0, 0, None)], uptime_s=12.3, now=2.4
        )
        assert set(payload) == TOP_KEYS
        assert payload["ok"] is True
        assert payload["service"] == "dataplane"
        assert payload["uptime_s"] == pytest.approx(12.3)
        assert set(payload["tasks"][0]) == TASK_KEYS
        assert payload["tasks"][0]["last_heartbeat_age_s"] == pytest.approx(0.4)
        assert set(payload["consumer"]) == CONSUMER_KEYS
        assert set(payload["archive"]) == ARCHIVE_KEYS
        assert set(payload["hot"]) == HOT_KEYS
        assert set(payload["store"]) == STORE_KEYS

    def test_extra_keys_from_a_collaborator_are_projected_away(self):
        payload = build_health_payload(
            [],
            consumer={"messages": 7, "by_topic": {"kpi": 7}, "group_id": "dataplane"},
            hot={"trucks": 3, "junk": 1},
            store={"run_ids": 4, "junk": 1},
        )
        assert set(payload["consumer"]) == CONSUMER_KEYS
        assert payload["consumer"]["messages"] == 7
        assert payload["hot"]["trucks"] == 3
        assert set(payload["store"]) == STORE_KEYS
        assert payload["store"]["run_ids"] == 4

    def test_the_offset_gate_is_visible_in_the_body(self):
        """A shut gate used to be invisible: uncommitted/commit_lag were projected OUT.

        An operator saw 200 while the offsets had silently stopped advancing forever.
        """
        payload = build_health_payload(
            [],
            consumer={
                "uncommitted": 500,
                "commit_lag": {"kpi_stream:0": 500},
                "assigned_partitions": 6,
                "messages": 500,
                "last_message_age_s": 0.1,
            },
        )
        assert payload["consumer"]["uncommitted"] == 500
        assert payload["consumer"]["commit_lag"] == {"kpi_stream:0": 500}
        assert payload["consumer"]["assigned_partitions"] == 6

    def test_falling_behind_is_judged_without_any_silence_at_all(self):
        """The backlog verdict must be ungated by silence, or it can never fire.

        Every other ingest rule here needs a gap in traffic, and a process that is merely
        slow never has one: it is receiving records at 0.01 s intervals while the broker
        runs away from it. Measured before this rule existed: broker_lag_total 8 873 /
        25 835 / 53 920 sitting in this very body, ok=True degraded=[] at all three.
        """
        busy = {
            "topics": ["kpi_stream"],
            "assigned_partitions": 6,
            "messages": 500_000,
            "last_message_age_s": 0.01,   # never silent, not once
            "commit_lag": {"kpi_stream:0": 0},   # every consumed record was written
            "broker_lag": {"kpi_stream:0": 53_920},
            "broker_lag_total": 53_920,
        }
        # A big backlog on its own is NOT a fault: a burst that is draining is normal.
        payload = build_health_payload([], consumer=dict(busy, broker_lag_rising=0))
        assert payload["ok"] is True, payload["degraded"]

        # A backlog that refuses to fall while records keep coming in IS one.
        payload = build_health_payload(
            [], consumer=dict(busy, broker_lag_rising=BROKER_LAG_RISING_REFRESHES)
        )
        assert payload["ok"] is False
        assert "ingest_behind" in payload["degraded"]
        assert "ingest_stalled" not in payload["degraded"]

    def test_ok_is_false_when_any_task_is_unhealthy(self):
        payload = build_health_payload(
            [
                TaskStatus("a", True, True, 0.0, 0.0, 0, None),
                TaskStatus("b", False, False, 0.0, 0.0, 3, "boom"),
            ]
        )
        assert payload["ok"] is False
        assert payload["degraded"] == ["task_unhealthy:b"]


class TestVerdictBeyondTaskLiveness:
    """The archive can stop being written while every thread is alive. That must not be 200."""

    def test_archive_unavailable_with_pending_runs_is_not_ok(self):
        payload = build_health_payload(
            [TaskStatus("consumer", True, True, 0.0, 0.0, 0, None)],
            archive={"available": False, "lag_runs": 2, "pending_run_ids": ["r1", "r2"]},
        )
        assert payload["ok"] is False
        assert "archive_unavailable_with_pending_runs" in payload["degraded"]

    def test_archive_unavailable_with_nothing_pending_is_still_ok(self):
        payload = build_health_payload(
            [TaskStatus("consumer", True, True, 0.0, 0.0, 0, None)],
            archive={"available": False, "lag_runs": 0, "pending_run_ids": []},
        )
        assert payload["ok"] is True

    def test_dropped_rows_fail_the_verdict(self):
        """Rows that existed and are gone must be loud — the write path has no drop path, so
        the only contributor is a hot slab evicted with uncaptured positions."""
        payload = build_health_payload(
            [TaskStatus("frames", True, True, 0.0, 0.0, 0, None)],
            store={"db_path": "/x.duckdb", "run_ids": 1, "rows_dropped": 500},
        )
        assert payload["ok"] is False
        assert "store_rows_dropped" in payload["degraded"]

    def test_the_failed_subscribe_shape_is_a_fault(self):
        """The failed-subscribe shape: process up, systemd green, /health 200, 0 ingested.

        This test's original form asserted that partitions-and-no-messages was the fault.
        That rule 503'd a merely idle dataplane, which is the normal state here — OpenRide
        runs for 5-6 minutes and is then silent. The shape it was really aiming at is
        *subscribed but holding no partition*, which is now its own reason.
        """
        stalled = build_health_payload(
            [TaskStatus("consumer", True, True, 0.0, 0.0, 1, None)],
            uptime_s=NOT_CONSUMING_AFTER_S + 1,
            consumer={
                "topics": ["kpi_stream"],
                "assigned_partitions": 0,
                "messages": 0,
                "last_message_age_s": None,
            },
        )
        assert stalled["ok"] is False
        assert "ingest_not_consuming" in stalled["degraded"]

    def test_a_consumer_that_went_quiet_for_too_long_is_a_stall(self):
        """Silence is a fault only while this process is still owed a write."""
        quiet = build_health_payload(
            [],
            consumer={
                "assigned_partitions": 6,
                "messages": 12_000,
                "last_message_age_s": INGEST_STALL_AFTER_S + 1,
                "commit_lag": {"kpi_stream:0": 7},
            },
        )
        assert quiet["ok"] is False
        assert "ingest_stalled" in quiet["degraded"]

    def test_a_resident_run_alone_is_not_a_stall(self):
        """Residency is not liveness: a sim killed mid-run leaves a slab nobody will close,
        and the old rule then answered 503 for the life of the process with nothing able to
        clear it — restart-looping any healthcheck wired to the endpoint."""
        quiet = build_health_payload(
            [],
            consumer={
                "assigned_partitions": 6,
                "messages": 12_000,
                "last_message_age_s": INGEST_STALL_AFTER_S + 1,
                "commit_lag": {"kpi_stream:0": 0},
            },
            hot={"resident_runs": ["R1"], "trucks": 500, "frames": 12},
        )
        assert quiet["ok"] is True, quiet["degraded"]

    def test_an_idle_consumer_with_no_partitions_is_not_a_stall(self):
        """Zero assigned partitions means nothing is expected — a standby is not broken."""
        idle = build_health_payload(
            [], consumer={"assigned_partitions": 0, "messages": 0, "last_message_age_s": None}
        )
        assert idle["ok"] is True
        recent = build_health_payload(
            [],
            consumer={"assigned_partitions": 6, "messages": 5, "last_message_age_s": 1.0},
        )
        assert recent["ok"] is True

    def test_caller_supplied_reasons_are_honoured(self):
        payload = build_health_payload([], degraded=["task_stalled:consumer"])
        assert payload["ok"] is False
        assert payload["degraded"] == ["task_stalled:consumer"]


class TestHttp:
    def test_200_then_503_when_a_task_dies(self, serve):
        gate = threading.Event()

        def looper(task):
            while not task.stopping:
                task.heartbeat()
                task.wait(0.01)

        def wedged(task):
            task.heartbeat()
            gate.wait(10.0)  # stops heartbeating -> unhealthy

        sup = Supervisor()
        sup.add("good", looper, heartbeat_timeout=5.0)
        sup.add("bad", wedged, heartbeat_timeout=0.1, backoff_initial=0.01)
        sup.start_all()
        srv = serve(lambda: build_health_payload(sup.statuses(), uptime_s=1.0))
        try:
            assert _wait_until(lambda: _get(srv.port)[0] == 200)
            code, body = _get(srv.port)
            assert code == 200 and body["ok"] is True
            assert {t["name"] for t in body["tasks"]} == {"good", "bad"}

            assert _wait_until(lambda: _get(srv.port)[0] == 503, timeout=3.0)
            code, body = _get(srv.port)
            assert code == 503
            assert body["ok"] is False
            bad = [t for t in body["tasks"] if t["name"] == "bad"][0]
            assert bad["healthy"] is False
        finally:
            gate.set()
            sup.stop_all(timeout=2.0)

    def test_crash_looping_task_flips_the_http_status_to_503(self, serve):
        """A restart storm used to answer 200 forever: the thread existed, so it "was alive"."""

        def boom(task):
            raise RuntimeError("duckdb TransactionException")

        sup = Supervisor()
        sup.add(
            "flush",
            boom,
            backoff_initial=0.01,
            backoff_max=0.02,
            heartbeat_timeout=30.0,
            reset_after=5.0,
        )
        sup.start_all()
        srv = serve(lambda: build_health_payload(sup.statuses(), uptime_s=1.0))
        try:
            assert _wait_until(lambda: _get(srv.port)[0] == 503, timeout=4.0)
            code, body = _get(srv.port)
            assert code == 503
            assert body["ok"] is False
            assert body["degraded"] == ["task_unhealthy:flush"]
            assert body["tasks"][0]["restarts"] >= 3
        finally:
            sup.stop_all(timeout=2.0)

    def test_archive_that_stopped_being_written_flips_the_http_status(self, serve):
        state = {"available": True, "pending": []}

        srv = serve(
            lambda: build_health_payload(
                [],
                archive={
                    "available": state["available"],
                    "lag_runs": len(state["pending"]),
                    "pending_run_ids": state["pending"],
                },
            )
        )
        assert _get(srv.port)[0] == 200
        state["available"] = False
        state["pending"] = ["run_20260805_150813"]
        code, body = _get(srv.port)
        assert code == 503
        assert body["archive"]["available"] is False
        assert "archive_unavailable_with_pending_runs" in body["degraded"]

    def test_provider_exception_yields_503_and_the_server_survives(self, serve):
        state = {"boom": True}

        def provider():
            if state["boom"]:
                raise RuntimeError("provider exploded")
            return build_health_payload([])

        srv = serve(provider)
        code, body = _get(srv.port)
        assert code == 503
        assert body["ok"] is False
        assert "provider exploded" in body["error"]

        state["boom"] = False
        code, body = _get(srv.port)
        assert code == 200
        assert body["ok"] is True

    def test_unknown_path_404s(self, serve):
        srv = serve(lambda: build_health_payload([]))
        code, body = _get(srv.port, "/nope")
        assert code == 404
        assert body["ok"] is False

    def test_concurrent_requests_are_all_served(self, serve):
        srv = serve(lambda: build_health_payload([]))
        results = []
        lock = threading.Lock()

        def hit():
            code, _ = _get(srv.port)
            with lock:
                results.append(code)

        threads = [threading.Thread(target=hit) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert results == [200] * 12
