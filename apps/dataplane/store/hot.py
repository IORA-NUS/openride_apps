"""Packed-numpy hot tier: one preallocated slab of truck positions per resident run.

Live reads come from here and nowhere else. This module never touches DuckDB, Mongo
or Kafka -- it is pure numpy plus a single threading.RLock guarding every mutation
and every read.

Frame identity across slab lifetimes
------------------------------------
``frame_idx`` is the key under which a frame is persisted downstream (DuckDB
``frames`` rows and the Mongo ``_id`` ``f"{run_id}:{frame_idx}"``).  A slab that
restarts its counter at 0 for a run that already has persisted frames therefore
*fuses* two unrelated frames into one on read.  Three things prevent that here:

1. **Carry-over.**  Evicting a slab (LRU or explicit) parks its identity -- frame
   counter, agent->slot map, haulier codes -- in a small bounded table.  Reviving
   the run restores it, so eviction is no longer destructive to frame identity.
2. **Seeding.**  ``HotStore(frame_idx_seed=...)`` / ``set_frame_idx_seed()`` /
   ``ensure_run(...)`` / ``seed_run_identity(start_frame_idx=..., slot_map=...,
   haulier_codes=...)`` (and a failed write's ``rollback_frame_idx()``) let the
   wiring layer hand a brand-new process the next safe frame index (e.g. from
   ``DuckStore.frame_range(run_id)[1] + 1``) and the persisted slot map.  The hot
   tier never queries a store itself -- the callable is injected.
3. **Never going backwards.**  When several candidates are available (explicit
   argument, carry-over, seed callable) the *largest* wins.

Slots that have been allocated but never written are excluded from ``snapshot()``
rather than being emitted at (0, 0) -- so a revived run reports a truck only once
it has actually reported a position.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Callable, Dict, List, Optional

import numpy as np

from apps.dataplane.contract.frame import Frame, KIND_KEYFRAME, state_code

logger = logging.getLogger(__name__)

_INITIAL_CAPACITY_DEFAULT = 1024
_MAX_HAULIER_CODE = 255
_CARRYOVER_RUNS_DEFAULT = 64


class HotStoreError(Exception):
    """Base error for the hot tier."""


class RunNotResident(HotStoreError):
    """Raised when an operation targets a run that is not currently resident."""


class _RunIdentity:
    """The part of a run that must survive eviction: identity, not payload.

    Positions are deliberately *not* carried over -- they are the big part, and a
    revived run re-learns them from the next message per truck.
    """

    __slots__ = (
        "frame_counter",
        "frames_emitted",
        "agent_to_slot",
        "slot_to_agent",
        "haulier_to_code",
        "next_haulier_code",
        "last_sim_time_ms",
        "update_count",
    )

    def __init__(
        self,
        *,
        frame_counter: int,
        frames_emitted: int,
        agent_to_slot: Dict[str, int],
        slot_to_agent: List[str],
        haulier_to_code: Dict[str, int],
        next_haulier_code: int,
        last_sim_time_ms: float,
        update_count: int,
    ) -> None:
        self.frame_counter = frame_counter
        self.frames_emitted = frames_emitted
        self.agent_to_slot = agent_to_slot
        self.slot_to_agent = slot_to_agent
        self.haulier_to_code = haulier_to_code
        self.next_haulier_code = next_haulier_code
        self.last_sim_time_ms = last_sim_time_ms
        self.update_count = update_count


class _RunSlab:
    """Preallocated, in-place-updated position arrays for a single run."""

    __slots__ = (
        "capacity",
        "lng",
        "lat",
        "state",
        "haulier",
        "written",
        "agent_to_slot",
        "slot_to_agent",
        "n_active",
        "frame_counter",
        "frames_emitted",
        "last_sim_time_ms",
        "haulier_to_code",
        "next_haulier_code",
        "last_touched",
        "last_update",
        "update_count",
    )

    def __init__(self, capacity: int) -> None:
        capacity = max(int(capacity), 1)
        self.capacity = capacity
        self.lng = np.zeros(capacity, dtype=np.float64)
        self.lat = np.zeros(capacity, dtype=np.float64)
        self.state = np.zeros(capacity, dtype=np.uint8)
        self.haulier = np.zeros(capacity, dtype=np.uint8)
        # A slot may be allocated (slot_for) before any position arrives; those
        # slots must not be emitted as trucks sitting at (0, 0).
        self.written = np.zeros(capacity, dtype=bool)
        self.agent_to_slot: Dict[str, int] = {}
        self.slot_to_agent: List[str] = []
        self.n_active = 0
        self.frame_counter = 0
        self.frames_emitted = 0
        self.last_sim_time_ms = 0.0
        self.haulier_to_code: Dict[str, int] = {}
        self.next_haulier_code = 1
        now = time.monotonic()
        self.last_touched = now
        # last_update is bumped by writes ONLY. Eviction is keyed on it, so a
        # read sweep over resident_runs() can never make the live run the victim.
        self.last_update = now
        self.update_count = 0

    # -- capacity ---------------------------------------------------------

    def _grow(self, min_capacity: int) -> None:
        new_capacity = max(self.capacity * 2, min_capacity, 1)
        for name, dtype in (
            ("lng", np.float64),
            ("lat", np.float64),
            ("state", np.uint8),
            ("haulier", np.uint8),
            ("written", bool),
        ):
            old = getattr(self, name)
            new = np.zeros(new_capacity, dtype=dtype)
            new[: old.shape[0]] = old
            setattr(self, name, new)
        self.capacity = new_capacity

    def _ensure_capacity(self, slot: int) -> None:
        if slot >= self.capacity:
            self._grow(slot + 1)

    # -- identity ---------------------------------------------------------

    def slot_for(self, agent_id: str) -> int:
        slot = self.agent_to_slot.get(agent_id)
        if slot is not None:
            return slot
        slot = self.n_active
        self._ensure_capacity(slot)
        self.agent_to_slot[agent_id] = slot
        self.slot_to_agent.append(agent_id)
        self.n_active += 1
        return slot

    def code_for_haulier(self, haulier_id: Optional[str]) -> int:
        if not haulier_id:
            return 0
        code = self.haulier_to_code.get(haulier_id)
        if code is not None:
            return code
        if self.next_haulier_code > _MAX_HAULIER_CODE:
            code = _MAX_HAULIER_CODE
        else:
            code = self.next_haulier_code
            self.next_haulier_code += 1
        self.haulier_to_code[haulier_id] = code
        return code

    def adopt_slot_map(self, slot_map: Dict[str, int]) -> None:
        """Restore a previously assigned agent->slot mapping (holes allowed)."""
        if not slot_map:
            return
        highest = max(int(v) for v in slot_map.values())
        self._ensure_capacity(highest)
        slot_to_agent: List[str] = [""] * (highest + 1)
        for agent_id, slot in slot_map.items():
            slot = int(slot)
            slot_to_agent[slot] = agent_id
            self.agent_to_slot[agent_id] = slot
        self.slot_to_agent = slot_to_agent
        self.n_active = max(self.n_active, highest + 1)

    def adopt_haulier_codes(self, codes: Dict[str, int]) -> None:
        if not codes:
            return
        for haulier_id, code in codes.items():
            self.haulier_to_code[haulier_id] = int(code)
        assigned = [c for c in self.haulier_to_code.values() if c < _MAX_HAULIER_CODE]
        self.next_haulier_code = max(self.next_haulier_code, (max(assigned) + 1) if assigned else 1)

    def identity(self) -> _RunIdentity:
        return _RunIdentity(
            frame_counter=self.frame_counter,
            frames_emitted=self.frames_emitted,
            agent_to_slot=dict(self.agent_to_slot),
            slot_to_agent=list(self.slot_to_agent),
            haulier_to_code=dict(self.haulier_to_code),
            next_haulier_code=self.next_haulier_code,
            last_sim_time_ms=self.last_sim_time_ms,
            update_count=self.update_count,
        )


class HotStore:
    """Bounded, thread-safe, packed hot tier over the most recently active runs."""

    def __init__(
        self,
        *,
        max_runs: int = 4,
        capacity: int = _INITIAL_CAPACITY_DEFAULT,
        frame_idx_seed: Optional[Callable[[str], Optional[int]]] = None,
        carryover_runs: int = _CARRYOVER_RUNS_DEFAULT,
    ) -> None:
        self._max_runs = max(int(max_runs), 1)
        self._default_capacity = capacity
        self._lock = threading.RLock()
        self._runs: Dict[str, _RunSlab] = {}
        # run_id -> identity of an evicted slab, most-recently-parked LAST.
        self._carryover: "OrderedDict[str, _RunIdentity]" = OrderedDict()
        self._carryover_max = max(int(carryover_runs), 0)
        self._frame_idx_seed = frame_idx_seed
        self._seed_errors = 0
        self._evictions = 0

    # -- seeding ------------------------------------------------------------

    def set_frame_idx_seed(self, fn: Optional[Callable[[str], Optional[int]]]) -> None:
        """Install the callable that yields the next safe frame_idx for a run.

        The callable is invoked while the store lock is held and must not call
        back into this HotStore. Returning ``None`` means "no information".
        """
        with self._lock:
            self._frame_idx_seed = fn

    def _seeded_frame_idx(self, run_id: str) -> int:
        """Ask the injected seed for the next frame_idx. Never raises."""
        fn = self._frame_idx_seed
        if fn is None:
            return 0
        try:
            value = fn(run_id)
        except Exception:  # noqa: BLE001 - a broken seed must not stop ingest
            self._seed_errors += 1
            logger.error(
                "frame_idx seed failed for run_id=%s; frames may collide with "
                "already-persisted frames for this run",
                run_id,
                exc_info=True,
            )
            return 0
        if value is None:
            return 0
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            self._seed_errors += 1
            logger.error("frame_idx seed returned non-integer %r for run_id=%s", value, run_id)
            return 0

    # -- internal -----------------------------------------------------------

    def _get(self, run_id: str) -> _RunSlab:
        """Lock held. Return the slab or raise; does NOT affect eviction order."""
        slab = self._runs.get(run_id)
        if slab is None:
            raise RunNotResident(run_id)
        return slab

    def _touch_read(self, run_id: str) -> _RunSlab:
        """Lock held. A read: records access time but not write activity."""
        slab = self._get(run_id)
        slab.last_touched = time.monotonic()
        return slab

    def _touch_write(self, run_id: str) -> _RunSlab:
        """Lock held. A write: this is what LRU eviction is keyed on."""
        slab = self._get(run_id)
        now = time.monotonic()
        slab.last_touched = now
        slab.last_update = now
        return slab

    def _park_locked(self, run_id: str, slab: _RunSlab) -> None:
        if self._carryover_max <= 0:
            return
        self._carryover.pop(run_id, None)
        self._carryover[run_id] = slab.identity()
        while len(self._carryover) > self._carryover_max:
            self._carryover.popitem(last=False)

    def _evict_lru_locked(self) -> None:
        while len(self._runs) > self._max_runs:
            # Keyed on last_update (writes), never on last_touched (reads): a
            # capture sweep that snapshots every resident run must not be able to
            # nominate the live run as the eviction victim.
            victim = min(self._runs, key=lambda rid: self._runs[rid].last_update)
            slab = self._runs.pop(victim)
            self._park_locked(victim, slab)
            self._evictions += 1
            logger.info(
                "hot tier evicted run_id=%s (frame_counter=%d, trucks=%d) — identity parked",
                victim,
                slab.frame_counter,
                slab.n_active,
            )

    def _create_slab_locked(
        self,
        run_id: str,
        *,
        capacity: Optional[int],
        start_frame_idx: Optional[int],
        slot_map: Optional[Dict[str, int]],
        haulier_codes: Optional[Dict[str, int]],
    ) -> _RunSlab:
        slab = _RunSlab(capacity or self._default_capacity)
        parked = self._carryover.pop(run_id, None)

        # Identity: explicit argument wins, else carry-over.
        if slot_map:
            slab.adopt_slot_map({k: int(v) for k, v in slot_map.items()})
        elif parked is not None:
            slab.agent_to_slot = dict(parked.agent_to_slot)
            slab.slot_to_agent = list(parked.slot_to_agent)
            slab.n_active = len(slab.slot_to_agent)
            slab._ensure_capacity(max(slab.n_active - 1, 0))

        if haulier_codes:
            slab.adopt_haulier_codes({k: int(v) for k, v in haulier_codes.items()})
        elif parked is not None:
            slab.haulier_to_code = dict(parked.haulier_to_code)
            slab.next_haulier_code = parked.next_haulier_code

        # frame_idx: never go backwards -- take the largest candidate available.
        candidates = [self._seeded_frame_idx(run_id)]
        if parked is not None:
            candidates.append(parked.frame_counter)
            slab.frames_emitted = parked.frames_emitted
            slab.update_count = parked.update_count
            slab.last_sim_time_ms = parked.last_sim_time_ms
        if start_frame_idx is not None:
            candidates.append(max(int(start_frame_idx), 0))
        slab.frame_counter = max(candidates)

        self._runs[run_id] = slab
        self._evict_lru_locked()
        return slab

    # -- public API ---------------------------------------------------------

    def ensure_run(
        self,
        run_id: str,
        *,
        capacity: Optional[int] = None,
        start_frame_idx: Optional[int] = None,
        slot_map: Optional[Dict[str, int]] = None,
        haulier_codes: Optional[Dict[str, int]] = None,
    ) -> None:
        """Make ``run_id`` resident, restoring/seeding its frame identity if new."""
        with self._lock:
            slab = self._runs.get(run_id)
            if slab is not None:
                now = time.monotonic()
                slab.last_touched = now
                slab.last_update = now
                return
            self._create_slab_locked(
                run_id,
                capacity=capacity,
                start_frame_idx=start_frame_idx,
                slot_map=slot_map,
                haulier_codes=haulier_codes,
            )

    def slot_for(self, run_id: str, agent_id: str) -> int:
        with self._lock:
            self.ensure_run(run_id)
            slab = self._touch_write(run_id)
            return slab.slot_for(agent_id)

    def update_position(
        self,
        run_id: str,
        agent_id: str,
        lng: float,
        lat: float,
        state: str | int | None,
        haulier_id: Optional[str],
        sim_time_ms: float,
    ) -> int:
        with self._lock:
            self.ensure_run(run_id)
            slab = self._touch_write(run_id)
            slot = slab.slot_for(agent_id)
            slab.lng[slot] = lng
            slab.lat[slot] = lat
            slab.state[slot] = state_code(state)
            slab.haulier[slot] = slab.code_for_haulier(haulier_id)
            slab.written[slot] = True
            slab.last_sim_time_ms = sim_time_ms
            slab.update_count += 1
            return slot

    def snapshot(self, run_id: str, *, kind: int = KIND_KEYFRAME) -> Frame:
        """Copy the run's written positions into a Frame. Does NOT count as a write.

        Reads deliberately do not refresh the eviction clock: the frame-capture
        sweep iterates every resident run, so if reads counted the sweep would
        invert the LRU and evict whichever run it visited first (the live one).
        """
        with self._lock:
            slab = self._touch_read(run_id)
            n_alloc = slab.n_active
            written = slab.written[:n_alloc]
            if n_alloc and not bool(written.all()):
                idx = np.flatnonzero(written)
                slot = idx.astype(np.uint32, copy=False)
                lng = slab.lng[idx]
                lat = slab.lat[idx]
                state = slab.state[idx]
                haulier = slab.haulier[idx]
            else:
                slot = np.arange(n_alloc, dtype=np.uint32)
                lng = np.array(slab.lng[:n_alloc], copy=True)
                lat = np.array(slab.lat[:n_alloc], copy=True)
                state = np.array(slab.state[:n_alloc], copy=True)
                haulier = np.array(slab.haulier[:n_alloc], copy=True)
            frame = Frame(
                frame_idx=slab.frame_counter,
                sim_time_ms=slab.last_sim_time_ms,
                lng=lng,
                lat=lat,
                slot=slot,
                state=state,
                haulier=haulier,
                kind=kind,
            )
            slab.frame_counter += 1
            slab.frames_emitted += 1
            return frame

    def resident_runs(self) -> list[str]:
        """Resident run ids, most recently *active* first (writes, then reads)."""
        with self._lock:
            return sorted(
                self._runs.keys(),
                key=lambda rid: (self._runs[rid].last_update, self._runs[rid].last_touched),
                reverse=True,
            )

    def evict(self, run_id: str) -> bool:
        with self._lock:
            slab = self._runs.pop(run_id, None)
            if slab is None:
                return False
            self._park_locked(run_id, slab)
            self._evictions += 1
            return True

    def haulier_codes(self, run_id: str) -> dict[str, int]:
        with self._lock:
            slab = self._touch_read(run_id)
            return dict(slab.haulier_to_code)

    def slot_map(self, run_id: str) -> dict[str, int]:
        with self._lock:
            slab = self._touch_read(run_id)
            return dict(slab.agent_to_slot)

    def next_frame_idx(self, run_id: str) -> int:
        """The frame_idx the next ``snapshot()`` of ``run_id`` will carry."""
        with self._lock:
            return self._get(run_id).frame_counter

    # -- identity seeding from a durable record ------------------------------

    def seed_run_identity(
        self,
        run_id: str,
        *,
        haulier_codes: Optional[Dict[str, int]] = None,
        slot_map: Optional[Dict[str, int]] = None,
        start_frame_idx: Optional[int] = None,
    ) -> bool:
        """Put a persisted identity (code book, slot map, frame counter) into the tier.

        This is the restart seam: the caller reads ``run_meta`` and hands the numbers
        back so the same uint8 keeps meaning the same haulier and the same uint32 the
        same truck across a process boundary.  Returns True when the run is resident
        afterwards (it always is, unless an argument was unusable).

        Nothing is ever *lowered*: the frame counter only moves up, and for a run that
        is already resident only entries that do not contradict what the live tier has
        already handed out are merged (a contradiction means frames written in this
        process already carry the other meaning; overwriting the map would mislabel
        them).  Contradictions are logged, not silently applied.
        """
        with self._lock:
            slab = self._runs.get(run_id)
            if slab is None:
                self._create_slab_locked(
                    run_id,
                    capacity=None,
                    start_frame_idx=start_frame_idx,
                    slot_map=slot_map,
                    haulier_codes=haulier_codes,
                )
                return True

            if haulier_codes:
                mergeable, clashes = self._non_clashing(
                    slab.haulier_to_code, {k: int(v) for k, v in haulier_codes.items()}
                )
                if clashes:
                    logger.warning(
                        "run_id=%s: %d persisted haulier code(s) contradict codes already "
                        "assigned in this process (%s) — keeping the live assignment",
                        run_id, len(clashes), sorted(clashes),
                    )
                slab.adopt_haulier_codes(mergeable)
            if slot_map:
                mergeable, clashes = self._non_clashing(
                    slab.agent_to_slot, {k: int(v) for k, v in slot_map.items()}
                )
                if clashes:
                    logger.warning(
                        "run_id=%s: %d persisted slot(s) contradict slots already assigned "
                        "in this process (%s) — keeping the live assignment",
                        run_id, len(clashes), sorted(clashes),
                    )
                if mergeable:
                    merged = dict(slab.agent_to_slot)
                    merged.update(mergeable)
                    slab.adopt_slot_map(merged)
            if start_frame_idx is not None:
                try:
                    slab.frame_counter = max(slab.frame_counter, int(start_frame_idx))
                except (TypeError, ValueError):
                    logger.error(
                        "non-integer start_frame_idx %r for run_id=%s", start_frame_idx, run_id
                    )
            return True

    @staticmethod
    def _non_clashing(current: Dict[str, int], incoming: Dict[str, int]):
        """Entries of ``incoming`` that neither rename a known key nor steal a used value."""
        used = {v: k for k, v in current.items()}
        keep: Dict[str, int] = {}
        clashes: List[str] = []
        for key, value in incoming.items():
            live = current.get(key)
            if live is not None:
                if live != value:
                    clashes.append(key)
                continue
            owner = used.get(value)
            if owner is not None and owner != key:
                clashes.append(key)
                continue
            keep[key] = value
        return keep, clashes

    def seed_haulier_codes(self, run_id: str, codes: Dict[str, int]) -> bool:
        """Alias kept because the wiring layer asks for it by name."""
        return self.seed_run_identity(run_id, haulier_codes=codes)

    def rollback_frame_idx(self, run_id: str, frame_idx: int) -> bool:
        """Give back a frame index whose downstream write failed.

        ``snapshot()`` bumps the counter before the caller can know whether the store
        accepted the frame.  A frame index burned per failure leaves a hole (0..39 with
        the first good frame at 40), which permanently defeats the archive's dense-prefix
        check.  Only the *last* index handed out can be returned, and only once.
        """
        with self._lock:
            slab = self._runs.get(run_id)
            if slab is None:
                return False
            try:
                idx = int(frame_idx)
            except (TypeError, ValueError):
                return False
            if slab.frame_counter != idx + 1:
                return False
            slab.frame_counter = idx
            slab.frames_emitted = max(slab.frames_emitted - 1, 0)
            return True

    def stats(self) -> dict:
        with self._lock:
            return {
                "runs": len(self._runs),
                "trucks": sum(slab.n_active for slab in self._runs.values()),
                "frames": sum(slab.frames_emitted for slab in self._runs.values()),
                "updates": sum(slab.update_count for slab in self._runs.values()),
                "evictions": self._evictions,
                "carryover_runs": len(self._carryover),
                "seed_errors": self._seed_errors,
            }
