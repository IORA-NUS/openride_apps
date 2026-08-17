"""Tests for apps.dataplane.store.hot.HotStore.

No conftest.py exists for this package (per shared decisions) -- everything a test
needs is inlined here.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from apps.dataplane.contract.frame import KIND_KEYFRAME, decode, encode, state_code
from apps.dataplane.store.hot import HotStore, RunNotResident


# ---------------------------------------------------------------------------
# Basic slot / update behaviour
# ---------------------------------------------------------------------------


def test_slot_stability_across_many_updates():
    store = HotStore(max_runs=4, capacity=8)
    run_id = "run-a"
    first_slot = store.update_position(run_id, "truck-1", 4.0, 51.0, "idle", None, 0.0)
    for i in range(50):
        slot = store.update_position(
            run_id, "truck-1", 4.0 + i * 0.001, 51.0, "loaded_in_transit", "H1", float(i)
        )
        assert slot == first_slot

    # a second agent gets a distinct, also-stable slot
    second_slot = store.update_position(run_id, "truck-2", 5.0, 52.0, "idle", None, 0.0)
    assert second_slot != first_slot
    for i in range(10):
        slot = store.update_position(run_id, "truck-2", 5.0, 52.0, "idle", None, float(i))
        assert slot == second_slot


def test_growth_past_initial_capacity_preserves_earlier_slots_and_values():
    store = HotStore(max_runs=4, capacity=2)
    run_id = "run-grow"
    slots = {}
    for i in range(10):
        agent_id = f"truck-{i}"
        slot = store.update_position(
            run_id, agent_id, float(i), float(i) + 100.0, "assigned", None, 0.0
        )
        slots[agent_id] = slot

    # slots assigned in order 0..9, never reused/changed
    assert slots == {f"truck-{i}": i for i in range(10)}

    frame = store.snapshot(run_id)
    assert frame.n == 10
    for i in range(10):
        assert frame.lng[i] == pytest.approx(float(i))
        assert frame.lat[i] == pytest.approx(float(i) + 100.0)


def test_snapshot_unknown_run_raises():
    store = HotStore()
    with pytest.raises(RunNotResident):
        store.snapshot("does-not-exist")


def test_snapshot_arrays_are_copies_not_views():
    store = HotStore()
    run_id = "run-copy"
    store.update_position(run_id, "truck-1", 1.0, 2.0, "idle", None, 0.0)
    frame1 = store.snapshot(run_id)
    original_lng = frame1.lng.copy()

    # mutate the returned frame's arrays
    frame1.lng[0] = -999.0
    frame1.lat[0] = -999.0

    # a fresh snapshot must be unaffected by the mutation above
    frame2 = store.snapshot(run_id)
    assert frame2.lng[0] == pytest.approx(1.0)
    assert frame2.lat[0] == pytest.approx(2.0)
    assert original_lng[0] == pytest.approx(1.0)


def test_frame_idx_strictly_increasing_per_run_and_independent_between_runs():
    store = HotStore(max_runs=4)
    store.update_position("run-x", "t1", 0.0, 0.0, "idle", None, 0.0)
    store.update_position("run-y", "t1", 0.0, 0.0, "idle", None, 0.0)

    x_indices = [store.snapshot("run-x").frame_idx for _ in range(5)]
    assert x_indices == sorted(x_indices)
    assert x_indices == list(range(5))

    y_indices = [store.snapshot("run-y").frame_idx for _ in range(3)]
    assert y_indices == list(range(3))


def test_haulier_code_assignment_stable_and_zero_reserved():
    store = HotStore()
    run_id = "run-haul"
    store.update_position(run_id, "t1", 0.0, 0.0, "idle", None, 0.0)  # unknown haulier
    store.update_position(run_id, "t2", 0.0, 0.0, "idle", "haulier-a", 0.0)
    store.update_position(run_id, "t3", 0.0, 0.0, "idle", "haulier-b", 0.0)
    store.update_position(run_id, "t4", 0.0, 0.0, "idle", "haulier-a", 0.0)  # repeat

    codes = store.haulier_codes(run_id)
    assert codes["haulier-a"] == 1
    assert codes["haulier-b"] == 2

    frame = store.snapshot(run_id)
    slot_map = store.slot_map(run_id)
    assert frame.haulier[slot_map["t1"]] == 0
    assert frame.haulier[slot_map["t2"]] == 1
    assert frame.haulier[slot_map["t3"]] == 2
    assert frame.haulier[slot_map["t4"]] == 1


def test_haulier_code_255_overflow():
    store = HotStore(capacity=8)
    run_id = "run-overflow"
    # 300 distinct hauliers: codes 1..254 assigned normally, everything from the
    # 255th onward collapses to 255.
    for i in range(300):
        store.update_position(
            run_id, f"truck-{i}", 0.0, 0.0, "idle", f"haulier-{i}", 0.0
        )
    codes = store.haulier_codes(run_id)
    assert codes["haulier-0"] == 1
    assert codes["haulier-253"] == 254
    assert codes["haulier-254"] == 255
    assert codes["haulier-299"] == 255


def test_state_accepts_string_and_int_producing_same_code():
    store = HotStore()
    run_id = "run-state"
    store.update_position(run_id, "t1", 0.0, 0.0, "loaded_in_transit", None, 0.0)
    expected = state_code("loaded_in_transit")
    store.update_position(run_id, "t2", 0.0, 0.0, expected, None, 0.0)

    frame = store.snapshot(run_id)
    slot_map = store.slot_map(run_id)
    assert frame.state[slot_map["t1"]] == expected
    assert frame.state[slot_map["t2"]] == expected
    assert expected == 7  # per the fixed state table


def test_lru_eviction_of_least_recently_touched_run():
    store = HotStore(max_runs=2, capacity=4)
    store.update_position("run-1", "t1", 0.0, 0.0, "idle", None, 0.0)
    time.sleep(0.01)
    store.update_position("run-2", "t1", 0.0, 0.0, "idle", None, 0.0)
    time.sleep(0.01)
    # touch run-1 again so run-2 becomes the least-recently-touched
    store.update_position("run-1", "t1", 0.0, 0.0, "idle", None, 0.0)
    time.sleep(0.01)
    # adding run-3 must evict run-2 (least recently touched), not run-1
    store.update_position("run-3", "t1", 0.0, 0.0, "idle", None, 0.0)

    resident = store.resident_runs()
    assert set(resident) == {"run-1", "run-3"}
    assert "run-2" not in resident

    with pytest.raises(RunNotResident):
        store.snapshot("run-2")


def test_evict_explicit():
    store = HotStore()
    store.update_position("run-e", "t1", 0.0, 0.0, "idle", None, 0.0)
    assert store.evict("run-e") is True
    assert store.evict("run-e") is False
    with pytest.raises(RunNotResident):
        store.snapshot("run-e")


def test_snapshot_round_trips_through_wire_codec():
    store = HotStore()
    run_id = "run-wire"
    store.update_position(run_id, "t1", 4.35, 51.9, "loaded_in_transit", "haulier-a", 123.5)
    store.update_position(run_id, "t2", 4.40, 51.8, "at_dropoff_gate", "haulier-b", 123.5)
    frame = store.snapshot(run_id, kind=KIND_KEYFRAME)
    assert decode(encode(frame)) == frame


def test_stats_reports_runs_trucks_frames_updates():
    store = HotStore(max_runs=4)
    store.update_position("run-s1", "t1", 0.0, 0.0, "idle", None, 0.0)
    store.update_position("run-s1", "t2", 0.0, 0.0, "idle", None, 0.0)
    store.update_position("run-s1", "t1", 1.0, 1.0, "idle", None, 1.0)
    store.update_position("run-s2", "t1", 0.0, 0.0, "idle", None, 0.0)
    store.snapshot("run-s1")

    stats = store.stats()
    assert stats["runs"] == 2
    assert stats["trucks"] == 3  # 2 in run-s1 + 1 in run-s2
    assert stats["frames"] == 1
    assert stats["updates"] == 4


# ---------------------------------------------------------------------------
# Regressions: frame identity must survive a slab's death (LRU evict / restart)
#
# Before the fix, every one of these produced frame_idx 0 again for a run that
# already had frames persisted downstream, and DuckDB (no PK on frames) fused the
# two eras into one frame on read.
# ---------------------------------------------------------------------------


def test_lru_eviction_then_revive_does_not_restart_frame_idx():
    store = HotStore(max_runs=1, capacity=8)
    store.update_position("R", "truckA", 10.0, 1.0, "idle", "H1", 1000.0)
    seen = [store.snapshot("R").frame_idx for _ in range(3)]
    assert seen == [0, 1, 2]

    # a second run evicts R (max_runs=1)
    store.update_position("OTHER", "t1", 0.0, 0.0, "idle", None, 0.0)
    assert "R" not in store.resident_runs()

    # revive R: frame_idx must continue, slot identity and haulier codes preserved
    store.update_position("R", "truckA", 11.0, 1.5, "idle", "H1", 2000.0)
    frame = store.snapshot("R")
    assert frame.frame_idx == 3, "frame_idx restarted after eviction -> DuckDB row fusion"
    assert store.slot_map("R")["truckA"] == 0
    assert store.haulier_codes("R")["H1"] == 1


def test_explicit_evict_then_revive_preserves_slot_and_haulier_identity():
    store = HotStore(max_runs=4, capacity=8)
    store.update_position("R", "a", 1.0, 1.0, "idle", "H1", 0.0)
    store.update_position("R", "b", 2.0, 2.0, "idle", "H2", 0.0)
    before_slots = store.slot_map("R")
    before_codes = store.haulier_codes("R")
    store.snapshot("R")
    store.snapshot("R")

    assert store.evict("R") is True
    # a different agent reports first after the revive -- it must NOT take slot 0
    store.update_position("R", "b", 5.0, 5.0, "idle", "H2", 10.0)

    assert store.slot_map("R") == before_slots
    assert store.haulier_codes("R") == before_codes
    assert store.snapshot("R").frame_idx == 2


def test_frame_idx_seed_covers_a_process_restart():
    """A fresh process (empty carry-over) seeds from the injected durable source."""
    persisted_max = {"R": 41}  # e.g. DuckStore.frame_range("R")[1]

    def seed(run_id):
        hi = persisted_max.get(run_id)
        return None if hi is None else hi + 1

    store = HotStore(max_runs=4, capacity=8, frame_idx_seed=seed)
    store.update_position("R", "truckB", 99.0, 9.0, "idle", "H2", 2000.0)
    assert store.snapshot("R").frame_idx == 42
    assert store.snapshot("R").frame_idx == 43

    # an unknown run still starts at 0
    store.update_position("NEW", "t", 1.0, 1.0, "idle", None, 0.0)
    assert store.snapshot("NEW").frame_idx == 0


def test_frame_idx_seed_installable_after_construction():
    store = HotStore(max_runs=4, capacity=8)
    store.set_frame_idx_seed(lambda run_id: 100)
    store.update_position("R", "t", 1.0, 1.0, "idle", None, 0.0)
    assert store.next_frame_idx("R") == 100
    assert store.snapshot("R").frame_idx == 100


def test_frame_idx_never_goes_backwards_between_candidates():
    # carry-over ahead of the seed -> carry-over wins
    store = HotStore(max_runs=1, capacity=8, frame_idx_seed=lambda rid: 3)
    store.update_position("R", "t", 1.0, 1.0, "idle", None, 0.0)
    for _ in range(10):
        store.snapshot("R")  # frame_counter now 3 + 10 = 13
    store.update_position("OTHER", "t", 0.0, 0.0, "idle", None, 0.0)  # evicts R
    store.update_position("R", "t", 1.0, 1.0, "idle", None, 0.0)
    assert store.next_frame_idx("R") == 13

    # seed ahead of the carry-over -> seed wins
    store2 = HotStore(max_runs=1, capacity=8, frame_idx_seed=lambda rid: 500)
    store2.update_position("R", "t", 1.0, 1.0, "idle", None, 0.0)
    store2.snapshot("R")
    store2.update_position("OTHER", "t", 0.0, 0.0, "idle", None, 0.0)
    store2.update_position("R", "t", 1.0, 1.0, "idle", None, 0.0)
    assert store2.next_frame_idx("R") == 501

    # explicit ensure_run argument participates in the same max()
    store3 = HotStore(max_runs=4, capacity=8)
    store3.ensure_run("R", start_frame_idx=77, slot_map={"a": 0, "b": 1},
                      haulier_codes={"H9": 4})
    assert store3.next_frame_idx("R") == 77
    assert store3.slot_map("R") == {"a": 0, "b": 1}
    assert store3.update_position("R", "b", 1.0, 1.0, "idle", "H9", 0.0) == 1
    assert store3.haulier_codes("R")["H9"] == 4


def test_broken_frame_idx_seed_is_counted_not_silent(caplog):
    def boom(run_id):
        raise RuntimeError("duckdb down")

    store = HotStore(max_runs=4, capacity=8, frame_idx_seed=boom)
    with caplog.at_level("ERROR"):
        store.update_position("R", "t", 1.0, 1.0, "idle", None, 0.0)
    assert store.stats()["seed_errors"] == 1
    assert any("frame_idx seed failed" in r.message for r in caplog.records)


def test_carryover_table_is_bounded():
    store = HotStore(max_runs=1, capacity=4, carryover_runs=2)
    for i in range(5):
        store.update_position(f"run-{i}", "t", 1.0, 1.0, "idle", None, 0.0)
        store.snapshot(f"run-{i}")
    assert store.stats()["carryover_runs"] == 2
    # the two most recently evicted runs still resume their counters
    store.update_position("run-3", "t", 1.0, 1.0, "idle", None, 0.0)
    assert store.next_frame_idx("run-3") == 1


def test_capture_sweep_does_not_invert_the_lru():
    """resident_runs() + snapshot() in a loop is exactly what the frame-capture
    sweep does; it must never nominate the live run for eviction."""
    store = HotStore(max_runs=4, capacity=8)
    for run_id in ("old1", "old2", "old3", "LIVE"):
        store.update_position(run_id, "t1", 1.0, 2.0, "idle", "H", 0.0)
        time.sleep(0.002)

    for _ in range(3):
        for run_id in store.resident_runs():
            store.snapshot(run_id)
            time.sleep(0.001)

    # LIVE keeps receiving positions; the others are finished
    store.update_position("LIVE", "t1", 1.1, 2.1, "idle", "H", 1.0)
    store.update_position("new_run", "t1", 0.0, 0.0, "idle", None, 0.0)

    resident = store.resident_runs()
    assert "LIVE" in resident, f"the live run was evicted by a read sweep: {resident}"
    assert "old1" not in resident, f"expected the stalest run to be the victim: {resident}"
    assert store.resident_runs()[0] == "new_run" or store.resident_runs()[0] == "LIVE"


def test_allocated_but_unwritten_slots_are_not_emitted_at_null_island():
    store = HotStore(max_runs=4, capacity=8)
    store.update_position("R", "a", 4.0, 51.0, "idle", "H", 0.0)
    store.slot_for("R", "b")  # allocated, never reported a position

    frame = store.snapshot("R")
    assert frame.n == 1
    assert list(frame.slot) == [0]
    assert frame.lng[0] == pytest.approx(4.0)

    store.update_position("R", "b", 5.0, 52.0, "idle", "H", 1.0)
    frame2 = store.snapshot("R")
    assert frame2.n == 2
    assert list(frame2.slot) == [0, 1]
    assert decode(encode(frame2)) == frame2


def test_revived_run_reports_only_trucks_that_have_re_reported():
    store = HotStore(max_runs=1, capacity=8)
    store.update_position("R", "a", 4.0, 51.0, "idle", "H", 0.0)
    store.update_position("R", "b", 5.0, 52.0, "idle", "H", 0.0)
    store.update_position("OTHER", "t", 0.0, 0.0, "idle", None, 0.0)  # evicts R
    store.update_position("R", "b", 6.0, 53.0, "idle", "H", 9.0)

    frame = store.snapshot("R")
    assert frame.n == 1
    assert list(frame.slot) == [1]  # b keeps its original slot
    assert frame.lng[0] == pytest.approx(6.0)
    assert decode(encode(frame)) == frame


# ---------------------------------------------------------------------------
# Concurrency: a genuine race, not an assertion about a lock existing.
# ---------------------------------------------------------------------------


def test_concurrent_writers_and_reader_no_races():
    store = HotStore(max_runs=4, capacity=16)
    run_id = "run-race"
    n_writer_threads = 8
    iterations = 500
    agents_per_thread = 5

    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    stop_flag = threading.Event()

    # Give every writer thread a disjoint set of agent_ids so we can verify
    # exact final state after the join.
    thread_agent_ids = [
        [f"t-{tidx}-{a}" for a in range(agents_per_thread)] for tidx in range(n_writer_threads)
    ]

    expected_final = {}  # agent_id -> (lng, lat)

    def writer(tidx: int):
        try:
            agent_ids = thread_agent_ids[tidx]
            for i in range(iterations):
                for a_idx, agent_id in enumerate(agent_ids):
                    lng = float(tidx * 1000 + a_idx * 10 + (i % 7))
                    lat = float(tidx * 1000 + a_idx * 10 + (i % 5)) * -1.0
                    store.update_position(
                        run_id, agent_id, lng, lat, i % len(range(12)), f"h-{tidx}", float(i)
                    )
        except BaseException as exc:  # noqa: BLE001 - must catch everything for the assertion
            with errors_lock:
                errors.append(exc)

    def reader():
        try:
            while not stop_flag.is_set():
                frame = store.snapshot(run_id)
                lengths = {
                    len(frame.lng),
                    len(frame.lat),
                    len(frame.slot),
                    len(frame.state),
                    len(frame.haulier),
                }
                assert len(lengths) == 1, f"inconsistent snapshot lengths: {lengths}"
        except BaseException as exc:  # noqa: BLE001
            with errors_lock:
                errors.append(exc)

    # seed the run so the reader thread has something to snapshot immediately
    store.ensure_run(run_id)

    writer_threads = [
        threading.Thread(target=writer, args=(tidx,)) for tidx in range(n_writer_threads)
    ]
    reader_thread = threading.Thread(target=reader)

    reader_thread.start()
    for t in writer_threads:
        t.start()
    for t in writer_threads:
        t.join()
    stop_flag.set()
    reader_thread.join(timeout=5.0)

    assert not errors, f"threads raised: {errors}"

    # compute expected last-written coordinates for every agent
    last_i = iterations - 1
    for tidx in range(n_writer_threads):
        for a_idx, agent_id in enumerate(thread_agent_ids[tidx]):
            lng = float(tidx * 1000 + a_idx * 10 + (last_i % 7))
            lat = float(tidx * 1000 + a_idx * 10 + (last_i % 5)) * -1.0
            expected_final[agent_id] = (lng, lat)

    final_frame = store.snapshot(run_id)
    slot_map = store.slot_map(run_id)

    all_agents = [a for agents in thread_agent_ids for a in agents]
    assert set(slot_map.keys()) == set(all_agents)
    # every slot index appears exactly once (stable, unique assignment)
    assert sorted(slot_map.values()) == list(range(len(all_agents)))

    for agent_id, (exp_lng, exp_lat) in expected_final.items():
        slot = slot_map[agent_id]
        assert final_frame.lng[slot] == pytest.approx(exp_lng)
        assert final_frame.lat[slot] == pytest.approx(exp_lat)

    assert final_frame.n == len(all_agents)


# ---------------------------------------------------------------------------
# Identity seeding from a durable record (the restart seam) and frame rollback
# ---------------------------------------------------------------------------


def test_seed_run_identity_creates_the_run_with_the_persisted_identity():
    store = HotStore(max_runs=4, capacity=8)
    assert store.seed_run_identity(
        "R",
        haulier_codes={"ACME": 1, "BOLT": 2},
        slot_map={"t1": 0, "t2": 1},
        start_frame_idx=42,
    ) is True
    assert store.haulier_codes("R") == {"ACME": 1, "BOLT": 2}
    assert store.slot_map("R") == {"t1": 0, "t2": 1}
    assert store.next_frame_idx("R") == 42
    # BOLT reporting first after the restart keeps code 2, and a new haulier gets 3.
    store.update_position("R", "t2", 9.0, 52.0, "idle", "BOLT", 1.0)
    store.update_position("R", "t3", 8.0, 52.0, "idle", "CEDA", 1.0)
    assert store.haulier_codes("R") == {"ACME": 1, "BOLT": 2, "CEDA": 3}
    assert store.slot_map("R")["t2"] == 1
    # A re-seeded truck that has not reported is absent, not sitting at (0, 0).
    frame = store.snapshot("R")
    assert frame.frame_idx == 42
    assert set(frame.slot.tolist()) == {1, 2}


def test_seed_haulier_codes_exists_on_the_real_class_and_delegates():
    store = HotStore(max_runs=4, capacity=8)
    assert callable(getattr(HotStore, "seed_haulier_codes", None))
    assert store.seed_haulier_codes("R", {"ACME": 1}) is True
    assert store.haulier_codes("R") == {"ACME": 1}


def test_seed_run_identity_merges_into_a_resident_run_without_lowering_anything():
    store = HotStore(max_runs=4, capacity=8)
    store.update_position("R", "t1", 4.0, 51.0, "idle", "ACME", 1.0)
    store.snapshot("R")
    store.snapshot("R")  # frame_counter == 2
    store.seed_run_identity(
        "R", haulier_codes={"ACME": 1, "BOLT": 2}, slot_map={"t1": 0, "t9": 5},
        start_frame_idx=1,
    )
    assert store.haulier_codes("R") == {"ACME": 1, "BOLT": 2}
    assert store.slot_map("R") == {"t1": 0, "t9": 5}
    assert store.next_frame_idx("R") == 2  # never lowered to the seed


def test_seed_run_identity_keeps_the_live_meaning_when_a_code_would_clash():
    """A code already handed out in this process encodes frames already written here."""
    store = HotStore(max_runs=4, capacity=8)
    store.update_position("R", "t1", 4.0, 51.0, "idle", "BOLT", 1.0)  # BOLT -> 1
    store.seed_run_identity("R", haulier_codes={"ACME": 1})
    assert store.haulier_codes("R") == {"BOLT": 1}


def test_rollback_frame_idx_returns_the_last_index_exactly_once():
    store = HotStore(max_runs=4, capacity=8)
    store.update_position("R", "t1", 4.0, 51.0, "idle", "ACME", 1.0)
    frame = store.snapshot("R")
    assert frame.frame_idx == 0
    assert store.next_frame_idx("R") == 1
    assert store.rollback_frame_idx("R", frame.frame_idx) is True
    assert store.next_frame_idx("R") == 0
    assert store.stats()["frames"] == 0
    # A second rollback of the same index is refused: only the last one can be given back.
    assert store.rollback_frame_idx("R", frame.frame_idx) is False
    assert store.next_frame_idx("R") == 0
    assert store.rollback_frame_idx("NOPE", 0) is False


def test_repeated_failed_writes_do_not_burn_frame_indices():
    store = HotStore(max_runs=4, capacity=8)
    store.update_position("R", "t1", 4.0, 51.0, "idle", "ACME", 1.0)
    for _ in range(40):  # 40 sweeps whose downstream write failed
        frame = store.snapshot("R")
        assert store.rollback_frame_idx("R", frame.frame_idx) is True
    assert store.next_frame_idx("R") == 0  # no 0..39 hole for the archive to trip over
