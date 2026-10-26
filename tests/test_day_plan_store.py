"""Day-plan itinerary store (``solver_lib/day_plan/store.py``): one contract, two transports.

The contract tests run against :class:`MemoryPlanStore` AND :class:`RedisPlanStore`
on :class:`FakeRedis` (exactly the redis-py commands the store uses, with MULTI
applied all-or-nothing). Redis-only tests pin the transaction shape, TTLs, the
current-version-only materialisation and the outage semantics. The live test runs
only with ``-m redis_live`` against db 15 of the local Redis (never db 0 / 2).
"""

from __future__ import annotations

import copy
import json
import zlib

import pytest

from apps.container_logistics.policies.solver_lib.day_plan import store as S
from apps.container_logistics.policies.solver_lib.day_plan.store import (
    MemoryKV, MemoryPlanStore, PlanStoreBudgetError, PlanStoreConflict, PlanStoreError,
    PlanStoreInputError, PlanStoreUnavailable, RedisPlanStore, TTL_SECONDS,
)

RUN = "run_test_1"


# ---------------------------------------------------------------------------
# fake Redis: exactly the commands used


class FakeRedis:
    def __init__(self):
        self.data: dict[str, object] = {}
        self.ttl_of: dict[str, int] = {}
        self.down = False          # every call raises ConnectionError
        self.fail_exec = False     # the next EXEC raises (nothing applied)
        self.transactions: list[list[tuple]] = []

    def _check(self):
        if self.down:
            raise ConnectionError("fake redis down")

    def get(self, key):
        self._check()
        v = self.data.get(key)
        return v if isinstance(v, bytes) else None

    def hgetall(self, key):
        self._check()
        v = self.data.get(key)
        return dict(v) if isinstance(v, dict) else {}

    def strlen(self, key):
        self._check()
        v = self.data.get(key)
        return len(v) if isinstance(v, bytes) else 0

    def pipeline(self, transaction=True):
        assert transaction is True, "the store must use MULTI"
        return FakePipeline(self)

    # inspection helpers (not used by the store)
    def keys(self, prefix=""):
        return sorted(k for k in self.data if k.startswith(prefix))


class FakePipeline:
    def __init__(self, r: FakeRedis):
        self.r = r
        self.cmds: list[tuple] = []

    def set(self, key, value, ex=None):
        assert isinstance(value, bytes) and ex is not None
        self.cmds.append(("set", key, value, ex))

    def delete(self, key):
        self.cmds.append(("del", key))

    def rpush(self, key, *values):
        assert values
        self.cmds.append(("rpush", key, list(values)))

    def hset(self, key, mapping=None):
        assert mapping
        self.cmds.append(("hset", key, dict(mapping)))

    def expire(self, key, seconds):
        self.cmds.append(("expire", key, seconds))

    def execute(self):
        self.r._check()
        if self.r.fail_exec:
            self.r.fail_exec = False
            raise ConnectionError("fake EXEC lost")
        data = copy.deepcopy(self.r.data)
        ttl = dict(self.r.ttl_of)
        for c in self.cmds:
            kind, key = c[0], c[1]
            if kind == "set":
                data[key] = c[2]
                ttl[key] = c[3]
            elif kind == "del":
                data.pop(key, None)
                ttl.pop(key, None)
            elif kind == "rpush":
                data.setdefault(key, []).extend(c[2])
            elif kind == "hset":
                data.setdefault(key, {}).update(c[2])
            elif kind == "expire":
                if key in data:
                    ttl[key] = c[2]
        self.r.data, self.r.ttl_of = data, ttl
        self.r.transactions.append(list(self.cmds))
        return [True] * len(self.cmds)


# ---------------------------------------------------------------------------
# factories: (make_store(), make_restarted_store()) sharing one record


def _memory_backend():
    kv = MemoryKV()
    return lambda run=RUN, **kw: MemoryPlanStore(run, kv=kv, **kw)


def _redis_backend():
    fake = FakeRedis()
    mk = lambda run=RUN, **kw: RedisPlanStore(run, client=fake, **kw)  # noqa: E731
    mk.fake = fake
    return mk


@pytest.fixture(params=["memory", "fake_redis"])
def backend(request):
    return _memory_backend() if request.param == "memory" else _redis_backend()


def J(*ids, **extra):
    return {j: {"r": 100 * i, "e": 100 * i + 50, **extra} for i, j in enumerate(ids)}


# ---------------------------------------------------------------------------
# contract (both transports)


def test_versions_autoincrement_per_day_and_days_never_go_back(backend):
    s = backend()
    assert s.load_current() is None and s.current_id is None
    assert s.write_version(1, {"t1": ["a"]}, J("a"))["id"] == "d1v1"
    assert s.write_version(1, {"t1": ["a"]}, J("a"))["id"] == "d1v2"
    out = s.write_version(2, {"t1": ["b"]}, J("b"))
    assert (out["id"], out["prev"]) == ("d2v1", "d1v2")
    with pytest.raises(PlanStoreInputError, match="before"):
        s.write_version(1, {}, {})
    assert s.current_id == "d2v1"


def test_unplanned_jobs_and_status_records(backend):
    s = backend()
    out = s.write_version(1, {"t1": ["a", "b"], "t2": []}, J("a", "b", "c"))
    assert (out["planned"], out["unplanned"]) == (2, 1)
    assert s.itinerary("t1") == ["a", "b"] and s.itinerary("t2") == []
    assert s.status_of("a") == {"status": "planned", "reason": None, "truck": "t1",
                                "version": "d1v1"}
    assert s.status_of("c")["status"] == "unplanned"
    assert s.kpi(1) == {"versions": 1, "jobs_moved": 0, "planned": 2, "unplanned": 1,
                        "locked": 0}


def test_locked_head_carried_over_on_a_replan(backend):
    s = backend()
    s.write_version(1, {"t1": ["a", "b", "c"], "t2": ["x"]}, J("a", "b", "c", "x"))
    s.set_status("a", "done", truck="t1")
    s.set_status("b", "dispatched", truck="t1")
    # the solver returns open tails only, and (wrongly) puts locked `b` on t2
    out = s.write_version(1, {"t1": ["d", "c"], "t2": ["b", "x"]}, J("c", "d", "x", "b"))
    assert s.itinerary("t1") == ["a", "b", "d", "c"]
    assert s.itinerary("t2") == ["x"]
    assert out["locked"] == 2 and out["locked_in_input"] == ["b"]
    assert s.status_of("a")["status"] == "done" and s.status_of("b")["status"] == "dispatched"
    assert s.status_of("d") == {"status": "planned", "reason": None, "truck": "t1",
                                "version": "d1v2"}
    # a truck with a locked head but no new tail keeps the head
    s.set_status("x", "dispatched", truck="t2")
    s.write_version(1, {"t1": ["c", "d"]}, J("c", "d"))
    assert s.itinerary("t2") == ["x"]
    assert s.itinerary("t1") == ["a", "b", "c", "d"]


def test_day_change_carries_only_in_flight_jobs_and_marks_undone(backend):
    s = backend()
    s.write_version(1, {"t1": ["a", "b", "c"], "t2": ["p"]}, J("a", "b", "c", "p", "q"))
    s.set_status("a", "done", truck="t1")
    s.set_status("b", "dispatched", truck="t1")
    s.set_status("p", "failed", reason="cancelled", truck="t2")
    # day 2 re-offers c; q (unplanned) is not offered again
    s.write_version(2, {"t1": ["c"]}, J("c"))
    assert s.itinerary("t1") == ["b", "c"]
    assert s.status_of("a")["status"] == "done"
    assert s.status_of("q")["status"] == "undone"
    assert s.status_of("p") == {"status": "failed", "reason": "cancelled", "truck": "t2",
                                "version": "d1v1"}
    assert s.status_of("c")["version"] == "d2v1"
    assert s.kpi(2)["jobs_moved"] == 0


def test_jobs_moved_matches_the_design_example(backend):
    s = backend()
    # §6a: t2 [o4 o5 o9 o12]; o4 done, o5 driving; re-plan inserts o11, moves o12 to t5
    s.write_version(3, {"t2": ["o4", "o5", "o9", "o12"], "t5": ["o20"]},
                    J("o4", "o5", "o9", "o12", "o20", "o30"))
    s.set_status("o4", "done", truck="t2")
    s.set_status("o5", "dispatched", truck="t2")
    out = s.write_version(3, {"t2": ["o11", "o9"], "t5": ["o20", "o12"]},
                          J("o9", "o11", "o12", "o20", "o30"))
    assert s.itinerary("t2") == ["o4", "o5", "o11", "o9"]
    assert (out["moved_truck"], out["moved_position"], out["moved_to_unplanned"]) == (1, 1, 0)
    assert out["jobs_moved"] == 2
    # o20 stays put; dropping o9 to unplanned counts; a failed job does not
    s.set_status("o12", "failed:cancelled")
    out = s.write_version(3, {"t2": ["o11"], "t5": ["o20"]}, J("o9", "o11", "o20", "o30"))
    assert (out["jobs_moved"], out["moved_to_unplanned"]) == (1, 1)
    assert s.kpi(3)["jobs_moved"] == 3 and s.kpi(3)["versions"] == 3
    assert s.status_of("o12") == {"status": "failed", "reason": "cancelled", "truck": "t5",
                                  "version": "d3v2"}


def test_a_job_omitted_on_the_same_day_becomes_unplanned_dropped(backend):
    s = backend()
    s.write_version(1, {"t1": ["a", "b"]}, J("a", "b"))
    s.write_version(1, {"t1": ["a"]}, J("a"))
    assert s.status_of("b") == {"status": "unplanned", "reason": "dropped", "truck": None,
                                "version": "d1v2"}


@pytest.mark.parametrize("itins, jobs, match", [
    ({"t1": ["a"], "t2": ["a"]}, J("a"), "on trucks"),
    ({"t1": ["a", "zz"]}, J("a"), "no entry"),
    ({"t1": ["a"]}, {"a": {"truck": "t9"}}, "reserved"),
    ({"t1": ["a"]}, {"a": {"x": object()}}, "JSON"),
    ({"t 1": ["a"]}, J("a"), "truck id"),
    ({"t1": "a"}, J("a"), "list of job ids"),
    ({"t1": [7]}, J("a"), "job id"),
])
def test_malformed_input_is_refused_and_nothing_applied(backend, itins, jobs, match):
    s = backend()
    s.write_version(1, {"t1": ["a"]}, J("a"))
    before = s.state()
    with pytest.raises(PlanStoreInputError, match=match):
        s.write_version(1, itins, jobs)
    assert s.state() == before


def test_set_status_validation(backend):
    s = backend()
    with pytest.raises(PlanStoreInputError, match="before any plan"):
        s.set_status("a", "done")
    s.write_version(1, {"t1": ["a"]}, J("a", "b"))
    for args, match in [(("a", "lost"), "unknown status"), (("a", "failed"), "needs a reason"),
                        (("nope", "done"), "not in any"), (("a", "done", None, "t 1"), "truck id")]:
        with pytest.raises(PlanStoreInputError, match=match):
            s.set_status(*args)
    rec = s.set_status("a", "failed:taken", truck="t9")
    assert rec == {"status": "failed", "reason": "taken", "truck": "t9", "version": "d1v1"}
    out = s.set_statuses([("a", "planned", None, None), ("b", "dispatched", None, "t1")])
    assert out["b"]["truck"] == "t1" and out["a"]["truck"] == "t9"


def test_kpi_counters(backend):
    s = backend()
    s.incr_kpi(0, {"done": 2})  # before any version: allowed
    s.write_version(1, {"t1": ["a"]}, J("a"))
    s.incr_kpi(1, {"done": 1, "failed:cancelled": 2})
    s.incr_kpi(1, {"done": 1})
    s.set_kpi(1, {"solve_ms_max": 1234.5, "backend": "greedy"})
    assert s.kpi(1)["done"] == 2 and s.kpi(1)["failed:cancelled"] == 2
    assert s.kpi(1)["solve_ms_max"] == 1234.5 and s.kpi(0) == {"done": 2}
    with pytest.raises(PlanStoreInputError):
        s.incr_kpi(1, {"backend": 1})
    with pytest.raises(PlanStoreInputError):
        s.incr_kpi(1, {"done": 1.5})
    assert s.write_version(1, {"t1": ["a"]}, J("a"))["id"] == "d1v2"


def _busy_history(s):
    s.incr_kpi(0, {"x": 1})
    s.write_version(1, {"t1": ["a", "b", "c"], "t2": ["d"]}, J("a", "b", "c", "d", "e"))
    s.set_status("a", "done", truck="t1")
    s.set_status("b", "dispatched", truck="t1")
    s.write_version(1, {"t1": ["c"], "t2": ["e", "d"]}, J("c", "d", "e"))
    s.set_status("d", "failed:cancelled", truck="t2")
    s.incr_kpi(1, {"done": 1, "failed:cancelled": 1})
    s.set_kpi(1, {"solve_ms_max": 88.0})
    s.write_version(2, {"t1": ["c", "f"]}, J("c", "f"))
    s.set_status("b", "done", truck="t1")


def test_restart_rebuilds_identical_state_and_identical_continuation(backend):
    a = backend()
    _busy_history(a)
    b = backend()                       # a fresh instance: the agent restarted
    assert b.load_current() == a.state()
    assert b.state() == a.state()
    assert b.footprint()["keys"] == a.footprint()["keys"]
    assert b.footprint()["bytes"] == a.footprint()["bytes"]
    # the restarted instance continues exactly as the original would have
    out_b = b.write_version(2, {"t1": ["f"], "t2": ["c"]}, J("c", "f"))
    assert out_b["id"] == "d2v2" and b.itinerary("t1") == ["b", "f"]
    c = backend()
    assert c.load_current() == b.state()


def test_load_current_refuses_to_discard_state(backend):
    s = backend()
    s.write_version(1, {"t1": ["a"]}, J("a"))
    with pytest.raises(PlanStoreError, match="already holds state"):
        s.load_current()


def test_a_second_writer_is_a_conflict_and_nothing_is_applied(backend):
    a = backend()
    a.write_version(1, {"t1": ["a"]}, J("a"))
    b = backend()
    b.load_current()
    b.write_version(1, {"t1": ["a"]}, J("a"))     # b moves the record to d1v2
    before = a.state()
    with pytest.raises(PlanStoreConflict, match="another writer"):
        a.write_version(1, {"t1": ["a"]}, J("a"))
    assert a.state() == before


def test_budget_refuses_before_anything_is_applied(backend):
    s = backend(max_keys=8)
    s.write_version(1, {"t1": ["a"]}, J("a"))  # blob, list, job, status, kpi, versions, current
    before = s.state()
    with pytest.raises(PlanStoreBudgetError, match="budget"):
        s.write_version(1, {"t1": ["a", "b", "c"]}, J("a", "b", "c"))
    assert s.state() == before
    big = backend("run_other", max_bytes=200)
    with pytest.raises(PlanStoreBudgetError):
        big.write_version(1, {"t1": ["a"]}, J("a"))
    assert big.current_id is None


def test_memory_and_redis_records_are_byte_identical():
    m, r = _memory_backend(), _redis_backend()
    sm, sr = m(), r()
    _busy_history(sm)
    _busy_history(sr)
    assert sm.state() == sr.state()
    assert sm.kv.data == r.fake.data
    assert sm.kv.ttls == r.fake.ttl_of


# ---------------------------------------------------------------------------
# Redis transport specifics (fake)


def test_one_multi_per_write_and_current_is_set_last():
    mk = _redis_backend()
    s = mk()
    s.write_version(1, {"t1": ["a", "b"]}, J("a", "b"))
    s.write_version(1, {"t1": ["b", "a"]}, J("a", "b"))
    assert len(mk.fake.transactions) == 2
    for tx in mk.fake.transactions:
        assert tx[-1][:2] == ("set", f"plan:{RUN}:current")
        assert all(c[1] != f"plan:{RUN}:current" for c in tx[:-1])
    s.set_statuses([("a", "dispatched", None, "t1"), ("b", "done", None, "t1")])
    assert len(mk.fake.transactions) == 3


def test_every_key_carries_the_14_day_ttl():
    mk = _redis_backend()
    s = mk()
    _busy_history(s)
    assert mk.fake.data
    assert set(mk.fake.ttl_of) == set(mk.fake.data)
    assert set(mk.fake.ttl_of.values()) == {TTL_SECONDS} and TTL_SECONDS == 14 * 86400


def test_only_the_current_version_is_materialised_and_every_blob_is_kept():
    mk = _redis_backend()
    s = mk()
    s.write_version(1, {"t1": ["a", "b"]}, J("a", "b", "c"))
    s.set_status("a", "dispatched", truck="t1")
    s.write_version(1, {"t2": ["b"]}, J("b", "c"))
    keys = mk.fake.keys(f"plan:{RUN}:")
    assert [k for k in keys if ":d1v1:" in k] == []
    assert sorted(k.split(f"plan:{RUN}:", 1)[1] for k in keys) == sorted([
        "current", "d1v1", "d1v2", "status", "kpi:d1", "versions",
        "d1v2:truck:t1", "d1v2:truck:t2",
        "d1v2:job:a", "d1v2:job:b", "d1v2:job:c"])
    fake = mk.fake
    assert fake.data[f"plan:{RUN}:current"] == b"d1v2"
    assert fake.data[f"plan:{RUN}:d1v2:truck:t1"] == [b"a"]
    job_a = {f: json.loads(v) for f, v in fake.data[f"plan:{RUN}:d1v2:job:a"].items()}
    assert job_a == {"r": 0, "e": 50, "truck": "t1", "seq": 0, "status": "dispatched",
                     "reason": None}
    blob = json.loads(zlib.decompress(fake.data[f"plan:{RUN}:d1v1"]))
    assert blob["itineraries"] == {"t1": ["a", "b"]} and blob["unplanned"] == ["c"]
    # write-through reaches the materialised job hash
    s.set_status("b", "failed:taken", truck="t7")
    job_b = fake.data[f"plan:{RUN}:d1v2:job:b"]
    assert json.loads(job_b["status"]) == "failed" and json.loads(job_b["reason"]) == "taken"


def test_a_lost_exec_is_atomic_kept_in_memory_and_resynchronised():
    mk = _redis_backend()
    s = mk()
    s.write_version(1, {"t1": ["a"]}, J("a", "b"))
    record_before = copy.deepcopy(mk.fake.data)
    mk.fake.fail_exec = True
    with pytest.raises(PlanStoreUnavailable) as ei:
        s.write_version(1, {"t1": ["b", "a"]}, J("a", "b"))
    assert ei.value.applied_in_memory and isinstance(ei.value.cause, ConnectionError)
    assert mk.fake.data == record_before            # all-or-nothing
    assert s.dirty and s.current_id == "d1v2" and s.itinerary("t1") == ["b", "a"]
    s.flush()
    assert not s.dirty
    assert mk().load_current() == s.state()


def test_redis_unreachable_never_loses_state():
    mk = _redis_backend()
    s = mk()
    s.write_version(1, {"t1": ["a", "b"]}, J("a", "b"))
    mk.fake.down = True
    with pytest.raises(PlanStoreUnavailable):
        s.set_status("a", "dispatched", truck="t1")
    with pytest.raises(PlanStoreUnavailable):
        s.write_version(1, {"t1": ["b"]}, J("b"))
    with pytest.raises(PlanStoreUnavailable):
        s.incr_kpi(1, {"done": 1})
    with pytest.raises(PlanStoreUnavailable):
        s.flush()
    assert s.dirty and s.itinerary("t1") == ["a", "b"]
    with pytest.raises(PlanStoreUnavailable):
        mk().load_current()
    mk.fake.down = False
    s.set_status("a", "done", truck="t1")           # next write resynchronises everything
    assert not s.dirty
    assert mk.fake.data[f"plan:{RUN}:d1v2"]          # the version written while down
    assert [k for k in mk.fake.keys() if ":d1v1:" in k] == []
    restarted = mk()
    assert restarted.load_current() == s.state()
    assert restarted.kpi(1)["done"] == 1
    assert set(mk.fake.ttl_of) == set(mk.fake.data)


def test_a_flushed_record_is_rewritten_in_full():
    mk = _redis_backend()
    s = mk()
    s.write_version(1, {"t1": ["a"]}, J("a", "b"))
    s.set_status("a", "dispatched", truck="t1")
    mk.fake.data.clear()
    mk.fake.ttl_of.clear()
    s.set_status("b", "failed:cancelled")
    assert mk().load_current()["status"] == s.state()["status"]


def test_db0_is_refused_and_the_url_comes_from_the_env(monkeypatch):
    for url in ("redis://127.0.0.1:6379/0", "redis://127.0.0.1:6379", "redis://h:1/?db=0"):
        with pytest.raises(PlanStoreInputError, match="db 0"):
            RedisPlanStore(RUN, url=url)
    monkeypatch.delenv("OPENRIDE_PLAN_REDIS_URL", raising=False)
    assert RedisPlanStore(RUN).url == S.DEFAULT_REDIS_URL == "redis://127.0.0.1:6379/2"
    monkeypatch.setenv("OPENRIDE_PLAN_REDIS_URL", "redis://example:6380/5")
    assert RedisPlanStore(RUN).url == "redis://example:6380/5"


def test_plan_store_from_env():
    assert isinstance(S.plan_store_from_env(""), MemoryPlanStore)
    assert isinstance(S.plan_store_from_env("r1", mode="memory"), MemoryPlanStore)
    assert isinstance(S.plan_store_from_env("r1"), RedisPlanStore)
    with pytest.raises(PlanStoreInputError):
        S.plan_store_from_env("r1", mode="disk")


def test_store_module_imports_no_framework_and_no_solver_backend():
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path(S.__file__).read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m and ("ortools" in m or "policies" in m or "framework" in m
                          or "dispatch" in m) for m in mods), mods


# ---------------------------------------------------------------------------
# live (opt-in): pytest -m redis_live tests/test_day_plan_store.py


@pytest.fixture
def live_client(request):
    if "redis_live" not in (request.config.getoption("markexpr") or ""):
        pytest.skip("live Redis test: run with -m redis_live")
    redis = pytest.importorskip("redis")
    client = redis.Redis.from_url("redis://127.0.0.1:6379/15", socket_timeout=2)
    try:
        client.ping()
    except redis.RedisError as exc:
        pytest.skip(f"no local Redis: {exc}")
    assert client.connection_pool.connection_kwargs["db"] == 15
    client.flushdb()
    yield client
    client.flushdb()


@pytest.mark.redis_live
def test_live_redis_roundtrip_ttl_and_restart(live_client):
    run = "run_live_test"
    s = RedisPlanStore(run, url="redis://127.0.0.1:6379/15")
    _busy_history(s)
    keys = sorted(k.decode() for k in live_client.keys(f"plan:{run}:*"))
    assert keys and all(0 < live_client.ttl(k) <= TTL_SECONDS for k in keys)
    assert not [k for k in keys if ":d1v1:" in k or ":d1v2:" in k]
    r = RedisPlanStore(run, url="redis://127.0.0.1:6379/15")
    assert r.load_current() == s.state()
    assert r.footprint()["keys"] == len(keys) == s.footprint()["keys"]
