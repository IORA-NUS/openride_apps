"""Facility-snapshot coalescing: one snapshot per (run, facility, sim_clock) on the live bus.

`facility/app.py` forces a publish from several call sites per tick, so the same facility
emits ~2.7 snapshots carrying the SAME `sim_clock` — measured 7,327 events for 60 facilities
across 45 ticks in one 32 s capture, 68% of them same-facility-same-tick repeats. The
dashboard's `facilityStreamSlice` upsert is last-writer-wins per `facility_id`, so all but the
last are overwritten on arrival; the bytes are pure waste (the `queue` array each one carries
is ~58% of ALL live SSE traffic).

These drive the real `_FacilitySnapshotCoalescer` — the property that matters is not "fewer
events" but "the surviving event is the one the reducer would have settled on".
"""

from __future__ import annotations

from apps.dataplane.service import _FacilitySnapshotCoalescer


def snap(fid: str, clock: str, **extra) -> dict:
    return {"facility_id": fid, "sim_clock": clock, "queue": [], **extra}


def published(pairs) -> list:
    return [p for _run, p in pairs]


def test_repeats_within_one_tick_collapse_to_the_last():
    """Three snapshots for one facility+tick emit ONE — and it is the newest."""
    c = _FacilitySnapshotCoalescer(max_hold_s=60.0)
    assert published(c.offer("r", snap("f1", "T1", n=1))) == []
    assert published(c.offer("r", snap("f1", "T1", n=2))) == []
    assert published(c.offer("r", snap("f1", "T1", n=3))) == []
    # The tick advances: the LAST snapshot of T1 is what reaches the bus.
    out = published(c.offer("r", snap("f1", "T2", n=4)))
    assert [p["n"] for p in out] == [3]
    assert c.stats()["coalesced"] == 2


def test_distinct_facilities_do_not_coalesce_each_other():
    c = _FacilitySnapshotCoalescer(max_hold_s=60.0)
    for fid in ("f1", "f2", "f3"):
        assert published(c.offer("r", snap(fid, "T1"))) == []
    out = published(c.offer("r", snap("f1", "T2")))
    assert [p["facility_id"] for p in out] == ["f1"]
    assert c.stats()["pending"] == 3


def test_runs_are_isolated():
    """Two concurrent runs share facility ids without colliding."""
    c = _FacilitySnapshotCoalescer(max_hold_s=60.0)
    c.offer("runA", snap("f1", "T1", n="a1"))
    c.offer("runB", snap("f1", "T1", n="b1"))
    out = c.offer("runA", snap("f1", "T2", n="a2"))
    assert [(r, p["n"]) for r, p in out] == [("runA", "a1")]


def test_max_hold_releases_a_facility_that_went_quiet():
    """A held snapshot must not wait forever on a tick that never comes."""
    c = _FacilitySnapshotCoalescer(max_hold_s=2.0)
    assert published(c.offer("r", snap("quiet", "T1"), now=100.0)) == []
    # Another facility keeps traffic flowing; the sweep rides along with it.
    out = published(c.offer("r", snap("busy", "T9"), now=103.0))
    assert [p["facility_id"] for p in out] == ["quiet"]


def test_flush_run_releases_the_final_tick():
    """A terminal run must still deliver the state it was holding."""
    c = _FacilitySnapshotCoalescer(max_hold_s=60.0)
    c.offer("r", snap("f1", "T1", n=1))
    c.offer("r", snap("f2", "T1", n=2))
    c.offer("other", snap("f1", "T1", n=3))
    out = c.flush_run("r")
    assert sorted(p["n"] for _r, p in out) == [1, 2]
    assert c.stats()["pending"] == 1  # the other run is untouched
    assert c.flush_run("r") == []


def test_kill_switch_forwards_verbatim():
    """max_hold_s=0 restores byte-for-byte pass-through — the documented rollback."""
    c = _FacilitySnapshotCoalescer(max_hold_s=0.0)
    assert not c.enabled
    a, b = snap("f1", "T1", n=1), snap("f1", "T1", n=2)
    assert published(c.offer("r", a)) == [a]
    assert published(c.offer("r", b)) == [b]
    assert c.stats()["coalesced"] == 0


def test_payload_missing_the_coalescing_keys_fails_open():
    """No facility_id or no sim_clock => forward, never swallow."""
    c = _FacilitySnapshotCoalescer(max_hold_s=60.0)
    no_id = {"sim_clock": "T1"}
    no_clock = {"facility_id": "f1"}
    assert published(c.offer("r", no_id)) == [no_id]
    assert published(c.offer("r", no_clock)) == [no_clock]


def test_every_tick_survives_a_long_burst():
    """The invariant end to end: one snapshot per (facility, tick), each the tick's last."""
    c = _FacilitySnapshotCoalescer(max_hold_s=60.0)
    seen = []
    for tick in range(1, 6):
        for fid in ("f1", "f2"):
            for rep in range(3):  # the measured ~2.7 repeats per tick
                seen += published(c.offer("r", snap(fid, f"T{tick}", rep=rep)))
    seen += published(c.flush_run("r"))
    assert len(seen) == 10  # 5 ticks x 2 facilities, from 30 offers
    assert {p["rep"] for p in seen} == {2}  # always the last repeat
