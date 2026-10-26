"""Finalize path of the order-outcome SIM KPIs (SimulationRuntime + AnalyticsManager).

* ordering in ``on_simulation_complete``: unserved bulk cancel < order outcomes <
  kpi breakdowns < terminal status write;
* the ``meta.order_outcomes`` PATCH adopts the fresh etag, so the terminal write still lands
  at attempt 1; a 412 gets exactly one retry; any failure is logged, never raised;
* the final recount rides the LAST ``final=True`` breakdown flush only;
* ``order_outcomes`` is a reserved ``run_config.meta`` key for solver run stats.
"""
from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from apps.container_logistics.analytics import order_outcomes as oo
from apps.container_logistics.analytics.manager import AnalyticsManager
from apps.simulation import simulation_runtime as srt
from apps.simulation.simulation_runtime import SimulationRuntime, StatusWrite

CL_DOMAIN = "container-logistics-sim"


def test_on_simulation_complete_order():
    body = inspect.getsource(SimulationRuntime.on_simulation_complete)
    order = [body.index(n) for n in (
        "_finalize_unserved_orders()", "_finalize_order_outcomes()",
        "_finalize_kpi_breakdowns()", "_write_terminal_status(")]
    assert order == sorted(order), order


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = json.dumps(self._payload)
        self.url = "u"

    def json(self):
        return self._payload


class _Server:
    """A run_config item with Eve optimistic concurrency (If-Match vs current etag)."""

    def __init__(self, etag="e0", fail_patch=None):
        self.etag = etag
        self.n = 0
        self.patches = []
        self.gets = 0
        self.fail_patch = fail_patch

    def bump(self):
        self.n += 1
        self.etag = f"e{self.n}"

    def patch(self, url, headers=None, data=None, timeout=None):
        self.patches.append((headers.get("If-Match"), json.loads(data)))
        if self.fail_patch is not None:
            return _Resp(self.fail_patch, {"err": 1})
        if headers.get("If-Match") != self.etag:
            return _Resp(412)
        self.bump()
        return _Resp(200, {"_id": "rid", "_etag": self.etag, "_status": "OK"})

    def get(self, url, headers=None, timeout=None, params=None):
        self.gets += 1
        return _Resp(200, {"_id": "rid", "_etag": self.etag})


def _runtime(monkeypatch, server, outcomes=None, raise_compute=None):
    rt = SimulationRuntime.__new__(SimulationRuntime)
    rt.run_id = "run_t"
    rt.domain = CL_DOMAIN
    rt.orsim_settings = {"STEP_INTERVAL": 240, "REFERENCE_TIME": "2020-01-01 08:00:00"}
    rt.steps = 2520
    rt.reference_time = None
    rt.run_record = {"_id": "rid", "_etag": server.etag}
    rt.user = SimpleNamespace(get_headers=lambda etag=None: ({"If-Match": etag} if etag else {}))
    rt._init_status_write_counters()
    monkeypatch.setattr(srt, "get_http_session", lambda: server)
    calls = []

    def fake_compute(run_id, **kw):
        calls.append((run_id, kw))
        if raise_compute:
            raise raise_compute
        return outcomes if outcomes is not None else {
            "final": True, "invariants_ok": True, "invariant_errors": [],
            "totals": {"released": 3, "done_same_day": 1, "failed": {"total": 1}}}

    monkeypatch.setattr(oo, "compute_order_outcomes", fake_compute)
    return rt, calls


def test_meta_patch_adopts_etag_so_terminal_write_is_ok_at_attempt_1(monkeypatch):
    server = _Server()
    rt, calls = _runtime(monkeypatch, server)
    out = rt._finalize_order_outcomes()
    assert out is rt._final_order_outcomes and out["final"] is True
    run_id, kw = calls[0]
    assert run_id == "run_t" and kw["final"] is True
    assert kw["horizon_s"] == 2520 * 240 and kw["step_interval"] == 240.0
    assert kw["reference_time"] == "2020-01-01 08:00:00"
    etag, body = server.patches[0]
    assert list(body) == ["meta.order_outcomes"]  # dotted: no other meta key touched
    assert rt.run_record["_etag"] == server.etag and rt.run_record["_id"] == "rid"
    monkeypatch.setattr(srt.time, "sleep", lambda s: None)
    assert rt._write_terminal_status(1.0) is StatusWrite.OK
    assert rt.status_write_outcomes == {"ok": 1}
    assert server.gets == 0  # no 412 reconciliation needed anywhere


def test_meta_patch_412_retries_exactly_once(monkeypatch):
    server = _Server()
    rt, _ = _runtime(monkeypatch, server)
    server.bump()  # another writer (e.g. the pooled agent's meta.market) moved the etag
    rt._finalize_order_outcomes()
    assert len(server.patches) == 2 and server.gets == 1
    assert server.patches[1][0] == "e1"
    assert rt.run_record["_etag"] == server.etag


def test_meta_patch_412_twice_gives_up_without_raising(monkeypatch, caplog):
    server = _Server()
    rt, _ = _runtime(monkeypatch, server)
    real_patch = server.patch

    def always_stale(url, headers=None, data=None, timeout=None):
        server.bump()
        return real_patch(url, headers=headers, data=data, timeout=timeout)

    server.patch = always_stale
    rt._finalize_order_outcomes()
    assert len(server.patches) == 2  # bounded: one retry, no loop
    assert any("PATCH failed http=412" in r.message for r in caplog.records)
    assert rt._final_order_outcomes is not None  # breakdowns still get the recount


def test_meta_patch_server_error_is_logged_never_raised(monkeypatch, caplog):
    server = _Server(fail_patch=500)
    rt, _ = _runtime(monkeypatch, server)
    rt._finalize_order_outcomes()
    assert len(server.patches) == 1
    assert rt.run_record["_etag"] == "e0"  # untouched on failure
    assert any("PATCH failed http=500" in r.message for r in caplog.records)


def test_session_exception_is_contained(monkeypatch):
    server = _Server()
    rt, _ = _runtime(monkeypatch, server)

    def boom(*a, **k):
        raise ConnectionError("api down")

    server.patch = boom
    assert rt._patch_run_meta("order_outcomes", {"x": 1}) is False


def test_compute_failure_is_logged_and_skips_the_patch(monkeypatch, caplog):
    server = _Server()
    rt, _ = _runtime(monkeypatch, server, raise_compute=RuntimeError("mongo down"))
    assert rt._finalize_order_outcomes() is None
    assert rt._final_order_outcomes is None and server.patches == []
    assert any("order-outcome recount failed" in r.message for r in caplog.records)


def test_invariant_failure_is_loud_but_still_written(monkeypatch, caplog):
    server = _Server()
    bad = {"final": True, "invariants_ok": False, "invariant_errors": ["totals: nope"], "totals": {}}
    rt, _ = _runtime(monkeypatch, server, outcomes=bad)
    rt._finalize_order_outcomes()
    assert any("INVARIANTS FAILED" in r.message for r in caplog.records)
    assert len(server.patches) == 1


def test_non_container_logistics_domain_is_a_noop(monkeypatch):
    server = _Server()
    rt, calls = _runtime(monkeypatch, server)
    rt.domain = "ride-hail"
    assert rt._finalize_order_outcomes() is None and calls == [] and server.patches == []


def test_breakdowns_finalize_receives_the_same_recount(monkeypatch):
    rt = SimulationRuntime.__new__(SimulationRuntime)
    rt.run_id, rt.domain, rt.user = "run_t", CL_DOMAIN, None
    rt.orsim_settings = {}
    rt.current_time = None
    rt._final_order_outcomes = {"final": True, "marker": 1}
    seen = {}
    monkeypatch.setattr(
        AnalyticsManager, "recompute_breakdowns_full",
        lambda self, clock, **kw: seen.update(kw))
    rt._finalize_kpi_breakdowns()
    assert seen["order_outcomes"] is rt._final_order_outcomes


def test_breakdowns_finalize_without_a_recount_passes_none(monkeypatch):
    rt = SimulationRuntime.__new__(SimulationRuntime)
    rt.run_id, rt.domain, rt.user = "run_t", CL_DOMAIN, None
    rt.orsim_settings = {}
    rt.current_time = None
    seen = {}
    monkeypatch.setattr(
        AnalyticsManager, "recompute_breakdowns_full",
        lambda self, clock, **kw: seen.update(kw))
    rt._finalize_kpi_breakdowns()
    assert seen["order_outcomes"] is None


# ------------------------------------------------------------------ final-flush-only


def _trips(hours):
    from apps.utils import time_to_str
    from datetime import datetime, timedelta

    base = datetime(2020, 1, 1, 8, 0, 0)
    return [{"sim_clock": time_to_str(base + timedelta(hours=h)), "truck": "t1"} for h in hours]


def _recording_manager(monkeypatch, trips):
    m = AnalyticsManager("run_t", "c", MagicMock(), None)
    flushes = []
    monkeypatch.setattr(m, "_paged_where", lambda *a, **k: trips)
    monkeypatch.setattr(m, "accumulate_completed_trips", lambda *a, **k: None)
    monkeypatch.setattr(m, "accumulate_forfeited_arrivals", lambda: None)
    monkeypatch.setattr(
        m, "save_breakdowns",
        lambda clock, final=False, order_outcomes=None: flushes.append((final, order_outcomes)))
    return m, flushes


def test_recount_rides_the_final_flush_only(monkeypatch):
    m, flushes = _recording_manager(monkeypatch, _trips([1, 7, 13, 20, 30]))
    recount = {"final": True}
    m.recompute_breakdowns_full("c", order_outcomes=recount)
    assert len(flushes) > 2
    assert [f for f in flushes if f[1] is not None] == [(True, recount)]
    assert flushes[-1] == (True, recount)
    assert all(o is None for final, o in flushes if not final)


def test_recount_rides_the_empty_run_final_flush_too(monkeypatch):
    m, flushes = _recording_manager(monkeypatch, [])
    recount = {"final": True}
    m.recompute_breakdowns_full("c", order_outcomes=recount)
    assert flushes == [(True, recount)]


def test_save_breakdowns_merges_owner_fields_into_haulier_rows_only(monkeypatch):
    from apps.container_logistics.analytics import kpi_breakdown_persist

    m = AnalyticsManager("run_t", "c", MagicMock(), None)
    m._haulier_acc = {"a": m._new_haulier_acc("a", "A"), "z": m._new_haulier_acc("z", "Z")}
    m._truck_acc = {"t1": m._new_truck_acc("t1", "a", "A")}
    monkeypatch.setattr(m, "_seed_haulier_roster", lambda: None)
    docs = []
    monkeypatch.setattr(kpi_breakdown_persist, "persist_kpi_breakdown",
                        lambda user, run_id, **kw: docs.append(kw))
    outcomes = {"final": True, "by_haulier": {"a": {
        "released": 4, "done_same_day": 1, "done_later": 1, "undone_at_day_end": 3,
        "failed": {"total": 2, "overdue_unassigned": 2, "cancelled_in_progress": 0,
                   "unserved_at_horizon": 0, "other": 0}}}}
    m.save_breakdowns("Wed, 01 Jan 2020 08:00:00 GMT", final=True, order_outcomes=outcomes)
    by_scope = {d["scope"]: d for d in docs}
    hrows = {r["id"]: r for r in by_scope["haulier"]["rows"]}
    assert hrows["a"]["orders_released"] == 4 and hrows["a"]["orders_failed_overdue_unassigned"] == 2
    assert hrows["a"]["order_outcomes_final"] is True
    assert hrows["z"]["orders_released"] == 0  # recorded zero, not "not recorded"
    assert by_scope["haulier"]["extra"]["order_outcomes_attribution"] == "owner"
    oo_keys = set(oo.zero_haulier_row_fields(True))
    assert not any(oo_keys & set(r) for r in by_scope["truck"]["rows"])
    assert by_scope["truck"]["extra"] is None

    docs.clear()
    m.save_breakdowns("Wed, 01 Jan 2020 08:00:00 GMT", final=True)
    hrows = {r["id"]: r for r in [d for d in docs if d["scope"] == "haulier"][0]["rows"]}
    assert not any(oo_keys & set(r) for r in hrows.values())
    assert [d for d in docs if d["scope"] == "haulier"][0]["extra"] is None  # default path unchanged


# ------------------------------------------------------------------ reserved meta key


def test_order_outcomes_is_a_reserved_run_config_meta_key():
    from apps.container_logistics.policies.dispatch import run_stats
    from apps.container_logistics.policies.framework.errors import PolicyError

    assert oo.META_KEY == "order_outcomes"
    assert oo.META_KEY in run_stats.RESERVED_META_KEYS
    with pytest.raises(PolicyError, match="reserved"):
        run_stats.check_meta_key(type("S", (), {"meta_key": "order_outcomes"}), "test")
