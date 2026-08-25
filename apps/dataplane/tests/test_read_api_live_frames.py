"""Tests for the ``positions`` behaviour added to ``apps.dataplane.read_api``.

Covers ``_codebook_delta`` directly (a pure function, cheapest to pin exactly), and
``live_frames`` / ``stream`` end to end against a REAL ``HotStore`` and a REAL
``_LiveBus`` (both already exercised individually in test_hot_store.py /
test_service_integration.py) -- a fake bus/hot pair could silently drift from the
real ``since``/``snapshot`` contract the generator actually depends on.

No conftest.py exists for this package (per shared decisions) -- everything a test
needs is inlined here.

``live_frames`` is a generator: nothing in its body runs until the first ``next()``.
Each explicit ``yield`` is its own pause point, so a single "tick" of the while loop
can span several ``next()`` calls (one per forwarded bus event, then one for the
codebook if any, then one for the frame/keepalive). Tests pull exactly as many
``next()`` calls as the traced control flow implies, rather than sleeping and hoping.
``interval_s`` is kept tiny (0.001s) and ``max_seconds`` short so nothing here waits
on the loop's own deadline.
"""

from __future__ import annotations

import json

import pytest

from apps.dataplane.read_api import _codebook_delta, live_frames, stream
from apps.dataplane.service import _LiveBus
from apps.dataplane.store.hot import HotStore

RUN = "R"


# ── helpers ─────────────────────────────────────────────────────────────────


def _ctx():
    class Ctx:
        pass

    c = Ctx()
    c.hot = HotStore()
    c.live_bus = _LiveBus()
    return c


def _parse(chunk: str) -> dict:
    """Decode one SSE write into {kind, event, payload}.

    kind is "keepalive" for a bare comment line, "data" for a nameless ``data:``
    line (the kpi event), or "event" for a named ``event: ...`` write.
    """
    if chunk.startswith(":"):
        return {"kind": "keepalive", "event": None, "payload": None}
    event = None
    data_raw = None
    for line in chunk.rstrip("\n").split("\n"):
        if line.startswith("event: "):
            event = line[len("event: "):]
        elif line.startswith("data: "):
            data_raw = line[len("data: "):]
    payload = json.loads(data_raw) if data_raw is not None else None
    return {"kind": "event" if event else "data", "event": event, "payload": payload}


def _collect_until_frame(gen, max_iters: int = 500) -> list:
    """Pull chunks until (and including) the first ``event: frame`` write."""
    chunks = []
    for _ in range(max_iters):
        chunk = next(gen)
        chunks.append(chunk)
        if chunk.startswith("event: frame\n"):
            return chunks
    raise AssertionError(f"no frame emitted after {max_iters} pulls; last: {chunks[-10:]}")


# ── _codebook_delta: the pure unit, tested directly ──────────────────────────


class _FakeHot:
    def __init__(self, slots: dict, codes: dict):
        self._slots = slots
        self._codes = codes

    def slot_map(self, run_id):
        return dict(self._slots)

    def haulier_codes(self, run_id):
        return dict(self._codes)


def test_codebook_delta_none_when_connection_is_already_current():
    hot = _FakeHot({"t1": 0}, {"ACME": 1})
    assert _codebook_delta(hot, RUN, known_slots={0: "t1"}, known_codes={"ACME": 1}) is None


def test_codebook_delta_carries_only_new_entries_when_extending():
    hot = _FakeHot({"t1": 0, "t2": 1}, {"ACME": 1, "BOLT": 2})
    delta = _codebook_delta(hot, RUN, known_slots={0: "t1"}, known_codes={"ACME": 1})
    assert delta == {"full": False, "slots": {"1": "t2"}, "haulierCodes": {"BOLT": 2}}


def test_codebook_delta_full_rebuild_on_slot_contradiction():
    hot = _FakeHot({"OTHER": 0}, {})
    delta = _codebook_delta(hot, RUN, known_slots={0: "t1"}, known_codes={})
    assert delta["full"] is True
    assert delta["slots"] == {"0": "OTHER"}


def test_codebook_delta_full_rebuild_on_haulier_code_contradiction():
    hot = _FakeHot({"t1": 0}, {"ACME": 9})
    delta = _codebook_delta(hot, RUN, known_slots={0: "t1"}, known_codes={"ACME": 1})
    assert delta["full"] is True
    assert delta["haulierCodes"] == {"ACME": 9}


def test_codebook_delta_swallows_hot_tier_errors():
    class BrokenHot:
        def slot_map(self, run_id):
            raise RuntimeError("boom")

        def haulier_codes(self, run_id):
            return {}

    assert _codebook_delta(BrokenHot(), RUN, {}, {}) is None


# ── live_frames: positions="events" (default) is exactly the old behaviour ──


def test_default_positions_omitted_forwards_truck_loc_unchanged_and_never_emits_codebook():
    ctx = _ctx()
    ctx.hot.update_position(RUN, "t1", 4.0, 51.0, "idle", "ACME", 0.0)
    gen = live_frames(ctx, RUN, interval_s=0.001, max_seconds=5)  # positions omitted

    first = _parse(next(gen))
    assert first["event"] == "frame"  # no codebook precedes it in default mode

    truck_loc_payload = {"type": "truck_loc", "truck_agent_id": "t1", "lon": 4.0, "lat": 51.0}
    ctx.live_bus.publish(RUN, "trip", truck_loc_payload)
    forwarded = _parse(next(gen))
    assert forwarded["event"] == "trip"
    assert forwarded["payload"] == truck_loc_payload

    seen_events = [forwarded["event"]]
    for _ in range(10):
        seen_events.append(_parse(next(gen))["event"])
    assert "codebook" not in seen_events
    gen.close()


# ── live_frames: positions="frames" ──────────────────────────────────────────


def test_frames_mode_suppresses_truck_loc_but_forwards_everything_else():
    ctx = _ctx()
    ctx.hot.update_position(RUN, "t1", 4.0, 51.0, "idle", "ACME", 0.0)
    gen = live_frames(ctx, RUN, interval_s=0.001, max_seconds=5, positions="frames")
    _collect_until_frame(gen)  # prime past the initial codebook + first frame

    truck_loc_payload = {"type": "truck_loc", "truck_agent_id": "t1", "lon": 4.0, "lat": 51.0}
    route_payload = {"type": "trip_route", "truck_agent_id": "t1", "coords": [[4.0, 51.0]]}
    end_payload = {"type": "trip_end", "truck_agent_id": "t1"}
    unknown_payload = {"type": "something_else", "x": 1}
    non_dict_payload = "not-a-dict"
    facility_payload = {"id": "F1"}
    status_payload = {"ok": True}
    terminal_payload = {"outcome": "completed"}

    ctx.live_bus.publish(RUN, "trip", truck_loc_payload)
    ctx.live_bus.publish(RUN, "trip", route_payload)
    ctx.live_bus.publish(RUN, "trip", end_payload)
    ctx.live_bus.publish(RUN, "trip", unknown_payload)
    ctx.live_bus.publish(RUN, "trip", non_dict_payload)
    ctx.live_bus.publish(RUN, "facility", facility_payload)
    ctx.live_bus.publish(RUN, "status", status_payload)
    ctx.live_bus.publish(RUN, "simulation_terminal", terminal_payload)

    # 8 published, 1 suppressed (truck_loc) -> exactly 7 forwarding yields, in order.
    forwarded = [_parse(next(gen)) for _ in range(7)]
    got = [(f["event"], f["payload"]) for f in forwarded]
    assert got == [
        ("trip", route_payload),
        ("trip", end_payload),
        ("trip", unknown_payload),
        ("trip", non_dict_payload),
        ("facility", facility_payload),
        ("status", status_payload),
        ("simulation_terminal", terminal_payload),
    ]
    gen.close()


def test_codebook_precedes_first_frame_and_resolves_every_slot_in_it():
    ctx = _ctx()
    ctx.hot.update_position(RUN, "t1", 4.0, 51.0, "idle", "ACME", 0.0)
    ctx.hot.update_position(RUN, "t2", 5.0, 52.0, "idle", "BOLT", 0.0)
    slot_map = ctx.hot.slot_map(RUN)
    haulier_codes = ctx.hot.haulier_codes(RUN)

    gen = live_frames(ctx, RUN, interval_s=0.001, max_seconds=5, positions="frames")
    book = _parse(next(gen))
    assert book["event"] == "codebook"
    assert book["payload"]["runId"] == RUN
    assert book["payload"]["full"] is False  # nothing was contradicted, only extended from {}
    assert book["payload"]["connectionEpoch"] == 0
    assert book["payload"]["slots"] == {str(v): k for k, v in slot_map.items()}
    assert book["payload"]["haulierCodes"] == haulier_codes

    frame = _parse(next(gen))
    assert frame["event"] == "frame"
    assert set(frame["payload"]["slot"]) == set(slot_map.values())
    gen.close()


def test_second_frame_with_no_new_slots_emits_no_second_codebook():
    ctx = _ctx()
    ctx.hot.update_position(RUN, "t1", 4.0, 51.0, "idle", "ACME", 0.0)
    gen = live_frames(ctx, RUN, interval_s=0.001, max_seconds=5, positions="frames")
    assert _parse(next(gen))["event"] == "codebook"
    assert _parse(next(gen))["event"] == "frame"

    # no bus traffic, no new agents -> every further tick is a plain frame, never a codebook.
    for _ in range(5):
        chunk = _parse(next(gen))
        assert chunk["event"] != "codebook"
    gen.close()


def test_new_slot_triggers_an_incremental_codebook_with_only_the_new_entry():
    ctx = _ctx()
    ctx.hot.update_position(RUN, "t1", 4.0, 51.0, "idle", "ACME", 0.0)
    gen = live_frames(ctx, RUN, interval_s=0.001, max_seconds=5, positions="frames")
    assert _parse(next(gen))["event"] == "codebook"  # book #1: t1 only
    assert _parse(next(gen))["event"] == "frame"

    ctx.hot.update_position(RUN, "t2", 5.0, 52.0, "idle", "BOLT", 0.0)
    t2_slot = ctx.hot.slot_map(RUN)["t2"]

    book2 = _parse(next(gen))
    assert book2["event"] == "codebook"
    assert book2["payload"]["full"] is False
    assert book2["payload"]["connectionEpoch"] == 0  # extension, not a rebuild
    assert book2["payload"]["slots"] == {str(t2_slot): "t2"}
    assert book2["payload"]["haulierCodes"] == {"BOLT": 2}

    frame2 = _parse(next(gen))
    assert frame2["event"] == "frame"
    assert t2_slot in frame2["payload"]["slot"]
    gen.close()


class _RemappableHotStore(HotStore):
    """A real HotStore whose slot_map()/haulier_codes() can be swapped from under a live
    connection, standing in for a process restart that re-learned slots in a different
    order. HotStore's own seed_run_identity() *refuses* to produce this contradiction for
    an already-resident run (see store/hot.py's docstring) -- this is the only way to
    exercise the rebuild branch of _codebook_delta from inside a real live_frames() run.
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.slot_override = None
        self.haulier_override = None

    def slot_map(self, run_id):
        if self.slot_override is not None:
            return dict(self.slot_override)
        return super().slot_map(run_id)

    def haulier_codes(self, run_id):
        if self.haulier_override is not None:
            return dict(self.haulier_override)
        return super().haulier_codes(run_id)


def test_contradicting_slot_map_forces_a_full_rebuild_codebook_with_bumped_epoch():
    ctx = _ctx()
    ctx.hot = _RemappableHotStore()
    ctx.hot.update_position(RUN, "t1", 4.0, 51.0, "idle", "ACME", 0.0)
    gen = live_frames(ctx, RUN, interval_s=0.001, max_seconds=5, positions="frames")

    book1 = _parse(next(gen))
    assert book1["payload"]["connectionEpoch"] == 0
    assert book1["payload"]["slots"] == {"0": "t1"}
    assert _parse(next(gen))["event"] == "frame"

    # A new agent (t2) must appear in the WRITTEN frame for the connection to re-check the
    # book at all -- the loop only calls _emit_codebook when a frame carries an unknown
    # slot. At that same moment the identity maps it reads are made to contradict slot 0.
    ctx.hot.update_position(RUN, "t2", 5.0, 52.0, "idle", "BOLT", 0.0)
    ctx.hot.slot_override = {"OTHER": 0, "t2": 1}
    ctx.hot.haulier_override = {"ACME": 7, "BOLT": 2}

    rebuild = _parse(next(gen))
    assert rebuild["event"] == "codebook"
    assert rebuild["payload"]["full"] is True
    assert rebuild["payload"]["connectionEpoch"] == 1
    assert rebuild["payload"]["slots"] == {"0": "OTHER", "1": "t2"}
    assert rebuild["payload"]["haulierCodes"] == {"ACME": 7, "BOLT": 2}

    assert _parse(next(gen))["event"] == "frame"
    gen.close()


# ── n == 0 frames: never emitted, in either mode ─────────────────────────────


@pytest.mark.parametrize("positions", ["events", "frames"])
def test_n_zero_frames_are_never_emitted(positions):
    ctx = _ctx()
    ctx.hot.slot_for(RUN, "t1")  # allocated, never written -> frame.n stays 0
    gen = live_frames(ctx, RUN, interval_s=0.001, max_seconds=5, positions=positions)
    for _ in range(5):
        assert next(gen) == ": keepalive\n\n"
    gen.close()


# ── the kpi event stays nameless in both modes ───────────────────────────────


@pytest.mark.parametrize("positions", ["events", "frames"])
def test_kpi_event_is_always_nameless(positions):
    ctx = _ctx()
    ctx.hot.update_position(RUN, "t1", 4.0, 51.0, "idle", "ACME", 0.0)
    gen = live_frames(ctx, RUN, interval_s=0.001, max_seconds=5, positions=positions)
    _collect_until_frame(gen)

    kpi_payload = {"metric": "queue_wait", "value": 42.0, "sim_clock": "2026-06-01T08:00:00Z"}
    ctx.live_bus.publish(RUN, "kpi", kpi_payload)
    chunk = next(gen)
    assert chunk.startswith("data: ") and not chunk.startswith("event:")
    assert json.loads(chunk[len("data: "):].rstrip("\n")) == kpi_payload
    gen.close()


# ── stream(): positions query-param parsing table ────────────────────────────


@pytest.mark.parametrize(
    "positions_values,expect_frames_mode",
    [
        (None, False),
        ([""], False),
        (["events"], False),
        (["frames"], True),
        (["FRAMES"], True),
        ([" frames "], True),
        (["garbage"], False),
    ],
    ids=["absent", "empty", "events", "frames", "FRAMES", "spaced-frames", "garbage"],
)
def test_stream_positions_param_parsing_table(positions_values, expect_frames_mode):
    ctx = _ctx()
    ctx.hot.update_position(RUN, "t1", 4.0, 51.0, "idle", "ACME", 0.0)
    params = {"run_id": [RUN], "interval_ms": ["1"]}
    if positions_values is not None:
        params["positions"] = positions_values

    gen = stream(ctx, "/live", params)
    assert gen is not None
    first = _parse(next(gen))
    if expect_frames_mode:
        assert first["event"] == "codebook", f"{positions_values!r} should be frames mode"
    else:
        assert first["event"] == "frame", f"{positions_values!r} should degrade to events mode"
    gen.close()


# ── bonus: the pre-existing "hot tier unavailable" guard, unaffected by positions ──


def test_no_hot_tier_yields_error_and_stops_regardless_of_positions():
    class Ctx:
        hot = None
        live_bus = None

    gen = live_frames(Ctx(), RUN, interval_s=0.001, max_seconds=1, positions="frames")
    assert next(gen) == 'event: error\ndata: {"error":"hot tier unavailable"}\n\n'
    with pytest.raises(StopIteration):
        next(gen)
