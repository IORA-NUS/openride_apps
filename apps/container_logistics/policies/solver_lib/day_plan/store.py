"""``PlanStore``: versioned per-truck itineraries for a day-plan solver, with a Redis record.

Single responsibility: hold the CURRENT day plan in memory (one itinerary per truck,
a status per job, per-day KPI counters) and write it through to a key-value record
that survives an agent restart. Pure data in / out (``str`` ids, JSON-able fields);
nothing here knows a tick, a framework class or a solver backend.

Two implementations with IDENTICAL semantics (one shared base, only the transport
differs; ``tests/test_day_plan_store.py`` runs one contract against both):

- :class:`RedisPlanStore` -- the record is Redis, URL ``OPENRIDE_PLAN_REDIS_URL``
  (default ``redis://127.0.0.1:6379/2``). **db 0 is refused**: it is the Celery broker.
- :class:`MemoryPlanStore` -- the record is a process-local :class:`MemoryKV` (shared
  by default, so a new instance in the SAME process can ``load_current``). For the
  battery, fixtures and ``tick.run_id == ""``; a worker restart loses it.

Redis layout (decision D14: one blob per version, keys per truck / job only for the
current version)::

    plan:{run}:current              STRING  "d3v14"                (SET last in every MULTI)
    plan:{run}:d3v14                STRING  zlib(JSON) blob of the whole version (all kept)
    plan:{run}:d3v14:truck:{t}      LIST    [o7, o2, o9]           (current version only)
    plan:{run}:d3v14:job:{o}        HASH    truck, seq, status, reason, <job fields>
                                                                   (current version only;
                                                                    every value JSON-encoded)
    plan:{run}:status               HASH    job -> JSON {status, reason, truck, version}
    plan:{run}:kpi:d3               HASH    field -> JSON number / string
    plan:{run}:versions             HASH    d3 -> 14               (latest version per day)

Every key written gets ``EXPIRE`` 14 days (:data:`TTL_SECONDS`) in the same MULTI that
writes it (strings via ``SET ... EX``), so maxmemory being unset cannot leak plans.
The materialised truck / job keys of a superseded version are DELeted in the MULTI
that writes its successor; the blob stays (it is how ``jobs_moved`` can be audited).

Itinerary rules (design §6a):

- **locked head** -- a job whose status is ``dispatched`` or ``done`` never moves.
  :meth:`PlanStore.write_version` carries it over from the previous version onto its
  truck's itinerary, in its old order, AHEAD of the new open tail. Across a DAY change
  only ``dispatched`` (in flight) jobs carry; yesterday's ``done`` jobs stay in
  yesterday's versions. A locked job the caller put in some tail is dropped from that
  tail and reported in ``locked_in_input`` (the truth is the lock, nothing is lost).
- **open tail** -- everything else; each new version may reorder it, move it to another
  truck, or leave it ``unplanned``.
- **versions** -- ``d{day}v{k}``, ``k`` auto-incremented per day from 1; days never go
  backwards (refused).
- **jobs_moved** -- (same day only; ``0`` for a day's v1) the jobs of the previous
  version's open tail that are still ``planned`` (not locked, not failed since) and
  whose ``(truck, index in the full itinerary)`` changed, including a move to unplanned. Split as ``moved_truck`` /
  ``moved_position`` / ``moved_to_unplanned``.
- **jobs no longer in the plan** -- a previous-version job the new version omits
  entirely, still ``planned`` / ``unplanned``: same day -> ``unplanned`` reason
  ``"dropped"``; new day -> ``undone``. ``failed`` / ``done`` keep their status.

Failure semantics (the wrapper turns these into ``tick_notes``; never silent):

- :class:`PlanStoreUnavailable` -- the record could not be reached / written. The change
  IS applied in memory (the plan keeps dispatching) and the store is marked dirty; the
  next write (or :meth:`PlanStore.flush`) first rewrites the WHOLE in-memory state in
  one MULTI (every unwritten version blob plus the current one, materialised keys, full status / kpi /
  versions hashes, then ``current``). So an outage delays the record, never loses it,
  as long as the agent lives. :attr:`PlanStore.dirty` tells whether the record lags.
  ``load_current`` raising it leaves the store empty.
- :class:`PlanStoreConflict` -- the record's ``current`` names a version this instance
  never wrote or loaded: another writer owns the run. Nothing is applied.
- :class:`PlanStoreBudgetError` -- the write would push the run's record past the key
  or byte budget (:meth:`PlanStore.footprint`). Nothing is applied.
- :class:`PlanStoreInputError` (a ``ValueError``) -- malformed input. Nothing is applied.

Single writer per run (the top's planner instance); concurrency is detected (conflict),
not arbitrated.
"""

from __future__ import annotations

import copy
import json
import os
import re
import zlib
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, urlparse

__all__ = [
    "DEFAULT_REDIS_URL",
    "LOCKED_STATUSES",
    "MemoryKV",
    "MemoryPlanStore",
    "PlanStore",
    "PlanStoreBudgetError",
    "PlanStoreConflict",
    "PlanStoreError",
    "PlanStoreInputError",
    "PlanStoreUnavailable",
    "RedisPlanStore",
    "STATUSES",
    "TTL_SECONDS",
    "plan_store_from_env",
]

DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/2"
TTL_SECONDS = 14 * 86400
STATUSES = ("planned", "dispatched", "done", "failed", "unplanned", "undone")
LOCKED_STATUSES = frozenset({"dispatched", "done"})
_RESERVED_FIELDS = frozenset({"truck", "seq", "status", "reason"})
DEFAULT_MAX_KEYS = 50_000
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9_.:@+\-]{1,200}$")


class PlanStoreError(RuntimeError):
    """Base class of every plan-store failure."""


class PlanStoreInputError(PlanStoreError, ValueError):
    """Malformed input; nothing was applied."""


class PlanStoreConflict(PlanStoreError):
    """The record's ``current`` was written by someone else; nothing was applied."""


class PlanStoreBudgetError(PlanStoreError):
    """The write would exceed the run's key / byte budget; nothing was applied."""


class PlanStoreUnavailable(PlanStoreError):
    """The record could not be written / read.

    For a write: the change IS applied in memory and the store is dirty; the next
    write or :meth:`PlanStore.flush` resynchronises the whole state. ``cause`` holds
    the transport exception.
    """

    def __init__(self, message: str, *, cause: BaseException | None = None,
                 applied_in_memory: bool = False) -> None:
        super().__init__(message)
        self.cause = cause
        self.applied_in_memory = applied_in_memory


# ---------------------------------------------------------------------------
# encoding helpers


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _b(value: str) -> bytes:
    return value.encode("utf-8")


def _s(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, (bytes, bytearray)) else str(value)


def _encode_blob(blob: Mapping[str, Any]) -> bytes:
    return zlib.compress(_b(_dumps(blob)), 6)


def _check_id(kind: str, value: Any) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise PlanStoreInputError(
            f"{kind} id must be a str of 1-200 chars from [A-Za-z0-9_.:@+-], got {value!r}")
    return value


def _check_day(day: Any) -> int:
    if isinstance(day, bool) or not isinstance(day, int) or day < 0:
        raise PlanStoreInputError(f"day must be an int >= 0, got {day!r}")
    return day


# ---------------------------------------------------------------------------
# ops: the only write vocabulary (all applied in ONE transaction)
#   ("set", key, bytes, ttl)        SET key value EX ttl
#   ("del", key)                    DEL key
#   ("rpush", key, [bytes, ...])    RPUSH key v1 v2 ...
#   ("hset", key, {field: bytes})   HSET key f1 v1 ...
#   ("expire", key, ttl)            EXPIRE key ttl


class _Sizes:
    """Approximate payload bytes per key (key + fields + values), mirroring the ops."""

    def __init__(self) -> None:
        self.flat: dict[str, int] = {}
        self.hashes: dict[str, dict[str, int]] = {}

    def copy(self) -> "_Sizes":
        out = _Sizes()
        out.flat = dict(self.flat)
        out.hashes = {k: dict(v) for k, v in self.hashes.items()}
        return out

    def apply(self, ops: Iterable[tuple]) -> None:
        for op in ops:
            kind, key = op[0], op[1]
            if kind == "set":
                self.hashes.pop(key, None)
                self.flat[key] = len(key) + len(op[2])
            elif kind == "del":
                self.flat.pop(key, None)
                self.hashes.pop(key, None)
            elif kind == "rpush":
                self.flat[key] = self.flat.get(key, len(key)) + sum(len(v) for v in op[2])
            elif kind == "hset":
                h = self.hashes.setdefault(key, {})
                for f, v in op[2].items():
                    h[f] = len(f) + len(v)
            # expire: no size change

    def keys(self) -> int:
        return len(self.flat) + len(self.hashes)

    def bytes(self) -> int:
        return sum(self.flat.values()) + sum(len(k) + sum(h.values())
                                             for k, h in self.hashes.items())


class MemoryKV:
    """Process-local stand-in for the Redis record (the subset of commands used).

    TTLs are recorded (``ttls``), not enforced: the process is shorter-lived than 14 d.
    """

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.ttls: dict[str, int] = {}

    def apply(self, ops: Sequence[tuple]) -> None:
        for op in ops:
            kind, key = op[0], op[1]
            if kind == "set":
                self.data[key] = bytes(op[2])
                self.ttls[key] = op[3]
            elif kind == "del":
                self.data.pop(key, None)
                self.ttls.pop(key, None)
            elif kind == "rpush":
                self.data.setdefault(key, []).extend(bytes(v) for v in op[2])
            elif kind == "hset":
                self.data.setdefault(key, {}).update({f: bytes(v) for f, v in op[2].items()})
            elif kind == "expire":
                if key in self.data:
                    self.ttls[key] = op[2]
            else:  # pragma: no cover - programming error
                raise AssertionError(kind)

    def get(self, key: str) -> bytes | None:
        v = self.data.get(key)
        return v if isinstance(v, bytes) else None

    def hgetall(self, key: str) -> dict[str, bytes]:
        v = self.data.get(key)
        return dict(v) if isinstance(v, dict) else {}

    def strlen(self, key: str) -> int:
        v = self.data.get(key)
        return len(v) if isinstance(v, bytes) else 0


_SHARED_MEMORY_KV = MemoryKV()


# ---------------------------------------------------------------------------
# the store


class PlanStore:
    """Shared semantics; subclasses implement only ``_execute`` / ``_get`` /
    ``_hgetall`` / ``_strlen`` (each may raise :class:`PlanStoreUnavailable`)."""

    def __init__(self, run_id: str, *, max_keys: int | None = None,
                 max_bytes: int | None = None) -> None:
        self.run_id = _check_id("run", run_id)
        self.max_keys = int(max_keys if max_keys is not None
                            else os.environ.get("OPENRIDE_PLAN_MAX_KEYS", DEFAULT_MAX_KEYS))
        self.max_bytes = int(max_bytes if max_bytes is not None
                             else os.environ.get("OPENRIDE_PLAN_MAX_BYTES", DEFAULT_MAX_BYTES))
        self._p = f"plan:{self.run_id}"
        self._cur: dict[str, Any] | None = None       # the current version (decoded blob)
        self._status: dict[str, dict[str, Any]] = {}  # job -> status record
        self._kpi: dict[int, dict[str, Any]] = {}     # day -> counters
        self._versions: dict[int, int] = {}           # day -> latest k
        self._dirty = False
        self._pending_blobs: list[tuple[str, bytes]] = []
        self._redis_current: str | None = None        # last `current` known in the record
        self._redis_mat_keys: set[str] = set()        # materialised keys in the record
        self._own_ids: set[str] = set()
        self._sizes = _Sizes()

    # -- transport (subclass) -------------------------------------------------
    def _execute(self, ops: list[tuple]) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def _get(self, key: str) -> bytes | None:  # pragma: no cover - abstract
        raise NotImplementedError

    def _hgetall(self, key: str) -> dict[str, bytes]:  # pragma: no cover - abstract
        raise NotImplementedError

    def _strlen(self, key: str) -> int:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- keys -----------------------------------------------------------------
    def key_current(self) -> str:
        return f"{self._p}:current"

    def key_blob(self, vid: str) -> str:
        return f"{self._p}:{vid}"

    def key_truck(self, vid: str, truck: str) -> str:
        return f"{self._p}:{vid}:truck:{truck}"

    def key_job(self, vid: str, job: str) -> str:
        return f"{self._p}:{vid}:job:{job}"

    def key_status(self) -> str:
        return f"{self._p}:status"

    def key_kpi(self, day: int) -> str:
        return f"{self._p}:kpi:d{day}"

    def key_versions(self) -> str:
        return f"{self._p}:versions"

    # -- read accessors -------------------------------------------------------
    @property
    def dirty(self) -> bool:
        """True while the record lags the in-memory state (after an outage)."""
        return self._dirty

    @property
    def current_id(self) -> str | None:
        return self._cur["id"] if self._cur else None

    @property
    def current_day(self) -> int | None:
        return self._cur["day"] if self._cur else None

    def itinerary(self, truck: str) -> list[str]:
        """The truck's full itinerary in the current version (locked head + open tail)."""
        return list(self._cur["itineraries"].get(truck, ())) if self._cur else []

    def status_of(self, job: str) -> dict[str, Any] | None:
        rec = self._status.get(job)
        return dict(rec) if rec else None

    def job_fields(self, job: str) -> dict[str, Any] | None:
        if not self._cur or job not in self._cur["jobs"]:
            return None
        return copy.deepcopy(self._cur["jobs"][job])

    def kpi(self, day: int) -> dict[str, Any]:
        return dict(self._kpi.get(day, {}))

    def state(self) -> dict[str, Any]:
        """Everything the store holds, JSON-able (restart test: equal before / after)."""
        cur = copy.deepcopy(self._cur) if self._cur else None
        return {
            "run_id": self.run_id,
            "current": cur,
            "status": {j: dict(r) for j, r in sorted(self._status.items())},
            "kpi": {f"d{d}": dict(sorted(v.items())) for d, v in sorted(self._kpi.items())},
            "versions": {f"d{d}": k for d, k in sorted(self._versions.items())},
        }

    def footprint(self) -> dict[str, int]:
        """Keys and approximate payload bytes this run holds in the record.

        Payload = key + field + value bytes; Redis' own per-key overhead comes on top
        (measure with ``MEMORY USAGE``). After ``load_current`` it is re-derived from
        the record (old blobs by ``STRLEN``)."""
        return {"keys": self._sizes.keys(), "bytes": self._sizes.bytes(),
                "max_keys": self.max_keys, "max_bytes": self.max_bytes}

    def check_budget(self, sizes: _Sizes | None = None) -> None:
        s = sizes or self._sizes
        if s.keys() > self.max_keys or s.bytes() > self.max_bytes:
            raise PlanStoreBudgetError(
                f"plan record for run {self.run_id} would hold {s.keys()} keys / "
                f"{s.bytes()} bytes, budget {self.max_keys} keys / {self.max_bytes} bytes "
                "(OPENRIDE_PLAN_MAX_KEYS / OPENRIDE_PLAN_MAX_BYTES)")

    # -- writes ---------------------------------------------------------------
    def write_version(self, day: int, itineraries: Mapping[str, Sequence[str]],
                      jobs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        """Store a new plan version for ``day`` and make it current (one MULTI).

        ``itineraries``: truck -> ordered OPEN-TAIL job ids (locked heads are carried
        over by the store). ``jobs``: job -> JSON-able fields for every job of this
        version (planned and unplanned); a job in no itinerary becomes ``unplanned``.
        Returns a JSON-able summary (``id``, ``jobs_moved``, counts, ``footprint``).
        """
        day = _check_day(day)
        if not isinstance(itineraries, Mapping) or not isinstance(jobs, Mapping):
            raise PlanStoreInputError("itineraries and jobs must be mappings")
        prev = self._cur
        if prev is not None and day < prev["day"]:
            raise PlanStoreInputError(
                f"day {day} is before the current version's day {prev['day']}")
        same_day = prev is not None and prev["day"] == day
        k = self._versions.get(day, 0) + 1
        vid = f"d{day}v{k}"

        # validated, canonicalised job fields
        new_jobs: dict[str, dict[str, Any]] = {}
        for j in sorted(jobs):
            _check_id("job", j)
            fields = jobs[j]
            if not isinstance(fields, Mapping):
                raise PlanStoreInputError(f"job {j}: fields must be a mapping")
            bad = _RESERVED_FIELDS.intersection(fields)
            if bad:
                raise PlanStoreInputError(f"job {j}: reserved field(s) {sorted(bad)}")
            try:
                new_jobs[j] = json.loads(_dumps(dict(fields)))
            except (TypeError, ValueError) as exc:
                raise PlanStoreInputError(f"job {j}: fields are not JSON-able ({exc})") from None

        # locked-head carry-over
        carry = LOCKED_STATUSES if same_day else frozenset({"dispatched"})
        locked: dict[str, list[str]] = {}
        locked_owner: dict[str, str] = {}
        if prev is not None:
            for t in sorted(prev["itineraries"]):
                for j in prev["itineraries"][t]:
                    rec = self._status.get(j)
                    if rec and rec["status"] in carry and j not in locked_owner:
                        owner = rec.get("truck") or t
                        locked.setdefault(owner, []).append(j)
                        locked_owner[j] = owner
        for j in locked_owner:
            new_jobs[j] = copy.deepcopy(prev["jobs"].get(j, new_jobs.get(j, {})))

        # open tails
        placed: dict[str, str] = {}
        locked_in_input: list[str] = []
        new_itin: dict[str, list[str]] = {}
        for t in sorted(itineraries):
            _check_id("truck", t)
            seq = itineraries[t]
            if isinstance(seq, (str, bytes)) or not isinstance(seq, Sequence):
                raise PlanStoreInputError(f"truck {t}: itinerary must be a list of job ids")
            tail: list[str] = []
            for j in seq:
                _check_id("job", j)
                if j in locked_owner:
                    locked_in_input.append(j)
                    continue
                if j in placed:
                    raise PlanStoreInputError(f"job {j} is on trucks {placed[j]} and {t}")
                if j not in new_jobs:
                    raise PlanStoreInputError(f"job {j} (truck {t}) has no entry in jobs")
                placed[j] = t
                tail.append(j)
            full = locked.get(t, []) + tail
            if full:
                new_itin[t] = full
        for t, head in locked.items():
            if t not in new_itin:
                new_itin[t] = list(head)
        new_itin = dict(sorted(new_itin.items()))
        unplanned = sorted(j for j in new_jobs if j not in placed and j not in locked_owner)

        # status changes
        status_new: dict[str, dict[str, Any]] = {}
        for j, t in placed.items():
            status_new[j] = {"status": "planned", "reason": None, "truck": t, "version": vid}
        for j in unplanned:
            status_new[j] = {"status": "unplanned", "reason": None, "truck": None, "version": vid}
        if prev is not None:
            prev_members = set(prev["unplanned"])
            for lst in prev["itineraries"].values():
                prev_members.update(lst)
            for j in sorted(prev_members):
                if j in new_jobs:
                    continue
                rec = self._status.get(j)
                if rec and rec["status"] in ("planned", "unplanned"):
                    status_new[j] = ({"status": "unplanned", "reason": "dropped", "truck": None,
                                      "version": vid} if same_day else
                                     {"status": "undone", "reason": None,
                                      "truck": rec.get("truck"), "version": rec["version"]})

        # jobs_moved vs the previous version of the same day
        moved = {"truck": [], "position": [], "unplanned": []}
        if same_day:
            new_pos = {j: (t, i) for t, lst in new_itin.items() for i, j in enumerate(lst)}
            prev_locked = {j for lst in prev["locked"].values() for j in lst}
            for t in sorted(prev["itineraries"]):
                for i, j in enumerate(prev["itineraries"][t]):
                    if j in prev_locked or j in locked_owner:
                        continue
                    rec = self._status.get(j)
                    if rec is not None and rec["status"] != "planned":
                        continue  # failed since: its re-placement is not plan churn

                    now = new_pos.get(j)
                    if now is None:
                        moved["unplanned"].append(j)
                    elif now[0] != t:
                        moved["truck"].append(j)
                    elif now[1] != i:
                        moved["position"].append(j)
        for v in moved.values():
            v.sort()
        jobs_moved = sum(len(v) for v in moved.values())

        blob = {
            "run": self.run_id, "id": vid, "day": day, "version": k,
            "prev": prev["id"] if prev else None,
            "itineraries": new_itin, "jobs": new_jobs, "unplanned": unplanned,
            "locked": {t: list(v) for t, v in sorted(locked.items())},
            "moved": moved, "jobs_moved": jobs_moved,
        }
        blob_bytes = _encode_blob(blob)

        status_all = dict(self._status)
        status_all.update(status_new)
        kpi_all = {d: dict(v) for d, v in self._kpi.items()}
        dk = kpi_all.setdefault(day, {})
        dk["versions"] = int(dk.get("versions", 0)) + 1
        dk["jobs_moved"] = int(dk.get("jobs_moved", 0)) + jobs_moved
        dk["planned"] = len(placed)
        dk["unplanned"] = len(unplanned)
        dk["locked"] = len(locked_owner)
        versions_all = dict(self._versions)
        versions_all[day] = k

        def incremental() -> list[tuple]:
            ops: list[tuple] = [("set", self.key_blob(vid), blob_bytes, TTL_SECONDS)]
            ops += [("del", key) for key in sorted(self._redis_mat_keys)]
            ops += self._materialise_ops(blob, status_all)
            ops += self._status_ops(status_new)
            kk = self.key_kpi(day)
            ops += [("hset", kk, {f: _b(_dumps(dk[f])) for f in
                                  ("versions", "jobs_moved", "planned", "unplanned", "locked")}),
                    ("expire", kk, TTL_SECONDS),
                    ("hset", self.key_versions(), {f"d{day}": _b(str(k))}),
                    ("expire", self.key_versions(), TTL_SECONDS),
                    ("set", self.key_current(), _b(vid), TTL_SECONDS)]
            return ops

        self._commit(cur=blob, status=status_all, kpi=kpi_all, versions=versions_all,
                     incremental=incremental, new_blob=(self.key_blob(vid), blob_bytes))
        self._own_ids.add(vid)
        return {
            "id": vid, "day": day, "version": k, "prev": blob["prev"],
            "planned": len(placed), "unplanned": len(unplanned), "locked": len(locked_owner),
            "jobs_moved": jobs_moved, "moved_truck": len(moved["truck"]),
            "moved_position": len(moved["position"]),
            "moved_to_unplanned": len(moved["unplanned"]),
            "locked_in_input": sorted(locked_in_input),
            "blob_bytes": len(blob_bytes), "footprint": self.footprint(),
        }

    def set_status(self, job: str, status: str, reason: str | None = None,
                   truck: str | None = None) -> dict[str, Any]:
        """Write-through status change of one job (see :meth:`set_statuses`)."""
        return self.set_statuses([(job, status, reason, truck)])[job]

    def set_statuses(self, changes: Iterable[tuple[str, str, str | None, str | None]]
                     ) -> dict[str, dict[str, Any]]:
        """Write-through status changes, one MULTI for the batch.

        ``status`` is one of :data:`STATUSES`; ``"failed:<reason>"`` is accepted as
        shorthand. ``failed`` needs a reason. The job must be known to the store."""
        if self._cur is None:
            raise PlanStoreInputError("set_status before any plan version exists")
        new: dict[str, dict[str, Any]] = {}
        for item in changes:
            job, status, reason, truck = item
            _check_id("job", job)
            if not isinstance(status, str):
                raise PlanStoreInputError(f"job {job}: status must be a str")
            if status.startswith("failed:"):
                status, reason = "failed", (reason or status.split(":", 1)[1])
            if status not in STATUSES:
                raise PlanStoreInputError(f"job {job}: unknown status {status!r}, use {STATUSES}")
            if status == "failed" and not reason:
                raise PlanStoreInputError(f"job {job}: status failed needs a reason")
            if reason is not None and not isinstance(reason, str):
                raise PlanStoreInputError(f"job {job}: reason must be a str")
            if truck is not None:
                _check_id("truck", truck)
            if job not in self._status and job not in self._cur["jobs"]:
                raise PlanStoreInputError(f"job {job} is not in any stored plan version")
            prior = self._status.get(job, {})
            new[job] = {"status": status, "reason": reason,
                        "truck": truck if truck is not None else prior.get("truck"),
                        "version": self._cur["id"]}
        if not new:
            return {}
        status_all = dict(self._status)
        status_all.update(new)
        vid = self._cur["id"]

        def incremental() -> list[tuple]:
            ops = self._status_ops(new)
            for j in sorted(new):
                if j in self._cur["jobs"]:
                    kj = self.key_job(vid, j)
                    ops += [("hset", kj, {"status": _b(_dumps(new[j]["status"])),
                                          "reason": _b(_dumps(new[j]["reason"]))}),
                            ("expire", kj, TTL_SECONDS)]
            return ops

        self._commit(cur=self._cur, status=status_all, kpi=self._kpi,
                     versions=self._versions, incremental=incremental)
        return {j: dict(r) for j, r in new.items()}

    def incr_kpi(self, day: int, deltas: Mapping[str, int]) -> dict[str, Any]:
        """Add to integer counters of the day's KPI hash (write-through).

        Written as the ABSOLUTE value (``HSET``), not ``HINCRBY``: memory is
        authoritative (single writer), and an absolute write is idempotent under the
        outage resync."""
        day = _check_day(day)
        for f, n in deltas.items():
            if not isinstance(f, str) or not f or isinstance(n, bool) or not isinstance(n, int):
                raise PlanStoreInputError(f"kpi delta {f!r}={n!r}: needs str field, int delta")
        kpi_all = {d: dict(v) for d, v in self._kpi.items()}
        dk = kpi_all.setdefault(day, {})
        for f, n in deltas.items():
            cur = dk.get(f, 0)
            if isinstance(cur, bool) or not isinstance(cur, int):
                raise PlanStoreInputError(f"kpi field {f} holds a non-integer {cur!r}")
            dk[f] = cur + n
        versions_all = dict(self._versions)
        versions_all.setdefault(day, 0)

        def incremental() -> list[tuple]:
            kk = self.key_kpi(day)
            ops: list[tuple] = []
            if deltas:
                ops.append(("hset", kk, {f: _b(_dumps(dk[f])) for f in sorted(deltas)}))
            ops += [("expire", kk, TTL_SECONDS),
                    ("hset", self.key_versions(), {f"d{day}": _b(str(versions_all[day]))}),
                    ("expire", self.key_versions(), TTL_SECONDS)]
            return ops

        self._commit(cur=self._cur, status=self._status, kpi=kpi_all,
                     versions=versions_all, incremental=incremental)
        return dict(dk)

    def set_kpi(self, day: int, fields: Mapping[str, Any]) -> dict[str, Any]:
        """Set JSON-able scalar KPI fields of the day's hash (write-through)."""
        day = _check_day(day)
        enc: dict[str, bytes] = {}
        for f, v in fields.items():
            if not isinstance(f, str) or not f or not (v is None or isinstance(v, (int, float, str))):
                raise PlanStoreInputError(f"kpi field {f!r}={v!r}: needs str field, scalar value")
            enc[f] = _b(_dumps(v))
        kpi_all = {d: dict(v) for d, v in self._kpi.items()}
        dk = kpi_all.setdefault(day, {})
        dk.update({f: json.loads(enc[f]) for f in enc})
        versions_all = dict(self._versions)
        versions_all.setdefault(day, 0)

        def incremental() -> list[tuple]:
            kk = self.key_kpi(day)
            return [("hset", kk, enc), ("expire", kk, TTL_SECONDS),
                    ("hset", self.key_versions(), {f"d{day}": _b(str(versions_all[day]))}),
                    ("expire", self.key_versions(), TTL_SECONDS)]

        self._commit(cur=self._cur, status=self._status, kpi=kpi_all,
                     versions=versions_all, incremental=incremental)
        return dict(dk)

    def flush(self) -> None:
        """Resynchronise the record after an outage (no-op when not dirty)."""
        if not self._dirty:
            return
        self._commit(cur=self._cur, status=self._status, kpi=self._kpi,
                     versions=self._versions, incremental=lambda: [])

    # -- restart --------------------------------------------------------------
    def load_current(self) -> dict[str, Any] | None:
        """Rebuild the in-memory state from the record (agent-restart path).

        Returns :meth:`state` or ``None`` when the record holds no plan for this run.
        Refused on a store that already holds state (it would discard it)."""
        if self._cur is not None or self._dirty or self._status:
            raise PlanStoreError("load_current on a store that already holds state")
        raw = self._get(self.key_current())
        if raw is None:
            return None
        vid = _s(raw)
        blob_raw = self._get(self.key_blob(vid))
        if blob_raw is None:
            raise PlanStoreError(f"record current={vid} but its blob is missing (expired?)")
        blob = json.loads(zlib.decompress(blob_raw))
        status = {_s(f): json.loads(_s(v)) for f, v in self._hgetall(self.key_status()).items()}
        versions = {int(_s(f)[1:]): int(_s(v))
                    for f, v in self._hgetall(self.key_versions()).items()}
        kpi: dict[int, dict[str, Any]] = {}
        for d in sorted(versions):
            h = self._hgetall(self.key_kpi(d))
            if h:
                kpi[d] = {_s(f): json.loads(_s(v)) for f, v in h.items()}
        blob_sizes = {}
        for d, kmax in versions.items():
            for kk in range(1, kmax + 1):
                key = self.key_blob(f"d{d}v{kk}")
                n = self._strlen(key)
                if n:
                    blob_sizes[key] = n
        self._cur, self._status, self._kpi, self._versions = blob, status, kpi, versions
        self._redis_current = vid
        self._own_ids = {vid}
        sizes = _Sizes()
        for key, n in blob_sizes.items():
            sizes.flat[key] = len(key) + n
        sizes.apply(self._state_ops(blob, status, kpi, versions))
        self._sizes = sizes
        self._redis_mat_keys = self._mat_keys(blob)
        return self.state()

    # -- internals ------------------------------------------------------------
    def _mat_keys(self, blob: Mapping[str, Any] | None) -> set[str]:
        if not blob:
            return set()
        vid = blob["id"]
        return ({self.key_truck(vid, t) for t in blob["itineraries"]}
                | {self.key_job(vid, j) for j in blob["jobs"]})

    def _materialise_ops(self, blob: Mapping[str, Any],
                         status: Mapping[str, Mapping[str, Any]]) -> list[tuple]:
        vid = blob["id"]
        ops: list[tuple] = []
        pos: dict[str, tuple[str, int]] = {}
        for t, lst in blob["itineraries"].items():
            kt = self.key_truck(vid, t)
            ops += [("del", kt), ("rpush", kt, [_b(j) for j in lst]), ("expire", kt, TTL_SECONDS)]
            for i, j in enumerate(lst):
                pos[j] = (t, i)
        for j in sorted(blob["jobs"]):
            t, i = pos.get(j, (None, None))
            rec = status.get(j, {})
            h = {f: _b(_dumps(v)) for f, v in blob["jobs"][j].items()}
            h.update({"truck": _b(_dumps(t)), "seq": _b(_dumps(i)),
                      "status": _b(_dumps(rec.get("status"))),
                      "reason": _b(_dumps(rec.get("reason")))})
            kj = self.key_job(vid, j)
            ops += [("del", kj), ("hset", kj, h), ("expire", kj, TTL_SECONDS)]
        return ops

    def _status_ops(self, recs: Mapping[str, Mapping[str, Any]]) -> list[tuple]:
        if not recs:
            return []
        ks = self.key_status()
        return [("hset", ks, {j: _b(_dumps(dict(r))) for j, r in sorted(recs.items())}),
                ("expire", ks, TTL_SECONDS)]

    def _state_ops(self, cur, status, kpi, versions) -> list[tuple]:
        """Ops that rewrite the WHOLE state except old blobs (resync and sizing)."""
        ops: list[tuple] = []
        if cur:
            ops += self._materialise_ops(cur, status)
        ks = self.key_status()
        ops.append(("del", ks))
        ops += self._status_ops(status)
        for d, fields in sorted(kpi.items()):
            kk = self.key_kpi(d)
            ops.append(("del", kk))
            if fields:
                ops += [("hset", kk, {f: _b(_dumps(v)) for f, v in sorted(fields.items())}),
                        ("expire", kk, TTL_SECONDS)]
        kv = self.key_versions()
        ops.append(("del", kv))
        if versions:
            ops += [("hset", kv, {f"d{d}": _b(str(k)) for d, k in sorted(versions.items())}),
                    ("expire", kv, TTL_SECONDS)]
        if cur:
            ops.append(("set", self.key_current(), _b(cur["id"]), TTL_SECONDS))
        return ops

    def _commit(self, *, cur, status, kpi, versions, incremental,
                new_blob: tuple[str, bytes] | None = None) -> None:
        """Budget check -> conflict check -> apply in memory -> one transaction."""
        # 1. who owns the record? (a transport error here = outage path below)
        probe_error: PlanStoreUnavailable | None = None
        force_full = self._dirty
        try:
            raw = self._get(self.key_current())
            remote = _s(raw) if raw is not None else None
            if remote is not None and remote not in self._own_ids:
                raise PlanStoreConflict(
                    f"plan record for run {self.run_id} is at {remote}, which this store "
                    "never wrote or loaded: another writer owns the run")
            if remote != self._redis_current:
                force_full = True  # flushed / expired record, or an ambiguous EXEC
        except PlanStoreUnavailable as exc:
            probe_error = exc

        # 2. the ops, and the budget they imply
        if force_full or probe_error is not None:
            blobs = list(self._pending_blobs) + ([new_blob] if new_blob else [])
            if cur and not any(k == self.key_blob(cur["id"]) for k, _ in blobs):
                blobs.append((self.key_blob(cur["id"]), _encode_blob(cur)))
            ops = [("set", k, v, TTL_SECONDS) for k, v in blobs]
            ops += [("del", key) for key in sorted(self._redis_mat_keys)]
            ops += self._state_ops(cur, status, kpi, versions)
        else:
            blobs = [new_blob] if new_blob else []
            ops = incremental()
        projected = self._sizes.copy()
        projected.apply(ops)
        self.check_budget(projected)

        # 3. apply in memory (the plan keeps working whatever the record does)
        self._cur, self._status, self._kpi, self._versions = cur, status, kpi, versions
        if probe_error is not None:
            if new_blob:
                self._pending_blobs.append(new_blob)
            self._dirty = True
            raise PlanStoreUnavailable(
                f"plan record unreachable; change kept in memory, record dirty ({probe_error})",
                cause=probe_error.cause, applied_in_memory=True)

        # 4. one transaction, `current` last
        try:
            self._execute(ops)
        except PlanStoreUnavailable as exc:
            if new_blob:
                self._pending_blobs.append(new_blob)
            self._dirty = True
            raise PlanStoreUnavailable(
                f"plan record write failed; change kept in memory, record dirty ({exc})",
                cause=exc.cause, applied_in_memory=True) from exc
        self._dirty = False
        self._pending_blobs = []
        self._sizes = projected
        self._redis_mat_keys = self._mat_keys(cur)
        self._redis_current = cur["id"] if cur else self._redis_current


class MemoryPlanStore(PlanStore):
    """Same semantics; the record is a process-local :class:`MemoryKV`."""

    def __init__(self, run_id: str, *, kv: MemoryKV | None = None, **kw: Any) -> None:
        super().__init__(run_id, **kw)
        self.kv = kv if kv is not None else _SHARED_MEMORY_KV

    def _execute(self, ops: list[tuple]) -> None:
        self.kv.apply(ops)

    def _get(self, key: str) -> bytes | None:
        return self.kv.get(key)

    def _hgetall(self, key: str) -> dict[str, bytes]:
        return self.kv.hgetall(key)

    def _strlen(self, key: str) -> int:
        return self.kv.strlen(key)


def _db_of_url(url: str) -> int:
    u = urlparse(url)
    q = parse_qs(u.query)
    if "db" in q:
        return int(q["db"][0])
    path = (u.path or "").strip("/")
    return int(path) if path else 0


class RedisPlanStore(PlanStore):
    """Same semantics; the record is Redis (redis-py, imported lazily).

    ``client`` may be injected (tests: a fake implementing ``get``, ``hgetall``,
    ``strlen`` and ``pipeline(transaction=True)`` with ``set(ex=)``, ``delete``,
    ``rpush``, ``hset(mapping=)``, ``expire``, ``execute``).
    Every redis-py error becomes :class:`PlanStoreUnavailable`.
    """

    def __init__(self, run_id: str, *, url: str | None = None, client: Any = None,
                 socket_timeout: float = 5.0, **kw: Any) -> None:
        super().__init__(run_id, **kw)
        self.url = url or os.environ.get("OPENRIDE_PLAN_REDIS_URL") or DEFAULT_REDIS_URL
        try:
            db = _db_of_url(self.url)
        except ValueError:
            raise PlanStoreInputError(f"unparseable plan Redis URL {self.url!r}") from None
        if client is None and db == 0:
            raise PlanStoreInputError(
                f"plan Redis URL {self.url!r} selects db 0, which is the Celery broker; "
                "use another db (default redis://127.0.0.1:6379/2)")
        self._socket_timeout = socket_timeout
        self._client = client

    def _redis(self) -> Any:
        if self._client is None:
            try:
                import redis  # noqa: PLC0415 - optional dependency, lazily
            except ImportError as exc:
                raise PlanStoreUnavailable(f"redis-py not importable: {exc}", cause=exc) from exc
            self._client = redis.Redis.from_url(
                self.url, socket_timeout=self._socket_timeout,
                socket_connect_timeout=self._socket_timeout)
        return self._client

    def _call(self, fn):
        try:
            return fn(self._redis())
        except PlanStoreUnavailable:
            raise
        except Exception as exc:  # redis.RedisError, OSError, ... -> explicit outage
            if type(exc).__module__.split(".")[0] not in ("redis",) and not isinstance(exc, OSError):
                raise
            raise PlanStoreUnavailable(f"{type(exc).__name__}: {exc}", cause=exc) from exc

    def _execute(self, ops: list[tuple]) -> None:
        def run(client):
            pipe = client.pipeline(transaction=True)
            for op in ops:
                kind, key = op[0], op[1]
                if kind == "set":
                    pipe.set(key, op[2], ex=op[3])
                elif kind == "del":
                    pipe.delete(key)
                elif kind == "rpush":
                    if op[2]:
                        pipe.rpush(key, *op[2])
                elif kind == "hset":
                    if op[2]:
                        pipe.hset(key, mapping=op[2])
                elif kind == "expire":
                    pipe.expire(key, op[2])
            pipe.execute()
        self._call(run)

    def _get(self, key: str) -> bytes | None:
        return self._call(lambda c: c.get(key))

    def _hgetall(self, key: str) -> dict[str, bytes]:
        return self._call(lambda c: c.hgetall(key))

    def _strlen(self, key: str) -> int:
        return int(self._call(lambda c: c.strlen(key)))


def plan_store_from_env(run_id: str, *, mode: str = "auto", **kw: Any) -> PlanStore:
    """``mode="memory"`` -> :class:`MemoryPlanStore`; ``"auto"`` -> Redis when ``run_id``
    is set, memory for an empty run id (battery / fixtures)."""
    if mode not in ("auto", "memory"):
        raise PlanStoreInputError(f"unknown plan store mode {mode!r}")
    if mode == "memory" or not run_id:
        return MemoryPlanStore(run_id or "local", **kw)
    return RedisPlanStore(run_id, **kw)
