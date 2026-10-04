"""Lifecycle of the three per-run lane topics: create, confirm, reap.

WHY EXISTENCE IS ESTABLISHED ONCE PER RUN PER PROCESS, NOT PER ``subscribe()``
------------------------------------------------------------------------------
LIFECYCLE CONTRACT point 4 says a subscription must be IN EFFECT when
``subscribe()`` returns -- and ``subscribe()`` is also re-entered from inside
the transport's own ``on_message``.  Anything that blocks there is a deadlock
waiting to happen.  So all the blocking work is done once, at bus construction:
the three lane topics are created, their partition counts are read back from
broker metadata, and only then is any agent allowed to subscribe.  By the time
``subscribe()`` runs, the partition it needs demonstrably exists and
``incremental_assign`` is immediate and local.

If the topics cannot be confirmed, :func:`ensure_run_lane_topics` RAISES.  That
propagates out of ``KafkaTransport.__init__`` and out of ``Messenger.__init__``,
which is exactly LIFECYCLE point 2's "fail loudly at construction" and matches
today's ``ConnectionRefusedError`` behaviour on the paho path.

WHY THE PARTITION COUNTS ARE READ BACK RATHER THAN ASSUMED
----------------------------------------------------------
``partition_for`` is evaluated independently by the publisher and by the
subscriber in different OS processes.  If they disagree on ``n_partitions`` the
record is written to a partition nobody is assigned to and vanishes with NO
error -- the barrier then stalls with a clean log, which is the worst failure
shape this project has.  The topic is created once per run with the configured
count; from that moment the BROKER'S METADATA is the single source of truth and
a stale env var on one worker cannot split the mapping.  Do not add a
config-read fallback for the case where metadata is unavailable: raising is the
correct answer there.

THE REAP FILTER IS THE SAFETY-CRITICAL PART OF THIS FILE
--------------------------------------------------------
A loose prefix match in a delete function is one keystroke from removing
``run_status`` or ``trip_geo_stream``, which the running dashboard consumes.
Every deletion path in this module goes through :func:`is_reapable_lane_topic`,
which requires the full ``^orsim_(bcast|reply|app)_run_`` shape.  None of the
eleven topics this broker actually carries can match it, and
``tests/test_kafka_lane_topics.py`` asserts that against their real names.

``concurrent.futures`` FROM A MONKEY-PATCHED PROCESS IS A DEADLOCK
------------------------------------------------------------------
Measured here, not theorised.  ``AdminClient.create_topics()`` and
``delete_topics()`` return ``concurrent.futures.Future`` objects, and
``Future.result()`` waits on a ``threading.Condition``.  Under
``eventlet.monkey_patch()`` that Condition is GREEN, while the thread that
completes the future and calls ``notify_all()`` is librdkafka's own native
background thread.  A green condition signalled from a non-hub OS thread never
wakes the hub, so the greenlet waits forever: a ``KafkaTransport`` constructed
inside a monkey-patched celery worker hung on its very first
``ensure_run_lane_topics``, silently, before the agent had done anything.

**Running the admin call on an unpatched OS thread does NOT fix it.**  That was
the first attempt and it hung identically, because inside such a thread
``threading`` is still the PATCHED module -- the same reason ``kafka_bus``'s poll
thread has to reach for ``eventlet.patcher.original('time').sleep``.  The future
is green wherever it is created.

So this module never WAITS on an admin future:

* Existence and partition counts are established with ``list_topics()``, a plain
  blocking C call with no Python future involved.  That is safe on the hub (it
  parks it for a few milliseconds at construction time) and it is the fast path
  a worker takes, because the launching process has already created the topics.
* Creation and deletion, when they are needed from a patched process, are issued
  FIRE-AND-FORGET and confirmed through ``list_topics``.  ``Future.set_result``
  from librdkafka's thread onto a green Condition with no waiter is a
  no-op-shaped uncontended acquire/release, which is safe; it is *waiting* that
  deadlocks.
* Unpatched -- the run launcher, the asyncio scheduler process, pytest -- the
  project's existing ``create_topic_if_not_exists`` is used unchanged, futures
  and all.

Age for the ops sweep is taken from the RUN ID EMBEDDED IN THE NAME
(``run_YYYYMMDD_HHMMSS``), not from broker metadata -- Kafka does not expose a
topic creation time.  A lane topic whose run id does not parse is SKIPPED, never
deleted: "I cannot date this" must not mean "delete it".
"""

import logging
import re
import time
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from apps.transport.lane_topics import (
    LANE_APP,
    LANE_BCAST,
    LANE_REPLY,
    LANES,
    sanitize_run_id,
)

logger = logging.getLogger(__name__)

#: The ONLY names any function in this module is permitted to delete.  Anchored
#: at both ends of the prefix: the lane word must be one of the three, and the
#: run id must begin with ``run_`` (which ``sanitize_run_id`` preserves for every
#: real run id this project generates).
_REAPABLE = re.compile(r'^orsim_(bcast|reply|app)_run_[A-Za-z0-9._-]*$')

#: ``run_20260911_054140`` -- the shape ``run_container_logistics_simulation``
#: mints.  Used only to DATE a topic for the ops sweep.
_RUN_ID_STAMP = re.compile(r'^orsim_(?:bcast|reply|app)_(run_(\d{8}_\d{6}))')

#: Default partition counts.  These are the values a lane topic is CREATED with;
#: after creation the broker is authoritative (see the module docstring).
DEFAULT_BCAST_PARTITIONS = 1
DEFAULT_REPLY_PARTITIONS = 8
DEFAULT_APP_PARTITIONS = 64

#: Lane topics are pure run-scoped scratch.  Short retention is the fourth line
#: of defence behind FORWARD-ONLY delivery (see ``kafka_bus``): even if
#: everything else failed, a run's traffic is not sitting around to be replayed.
DEFAULT_RETENTION_HOURS = 6
DEFAULT_RETENTION_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB per lane topic

#: How long ``ensure_run_lane_topics`` will keep asking the broker for metadata
#: before giving up and raising.  Bounded on purpose: an agent boot that hangs
#: here is worse than an agent boot that fails.
DEFAULT_CONFIRM_TIMEOUT_S = 10.0


def lane_topic_names(run_id: str) -> Dict[str, str]:
    """``{lane: kafka topic name}`` for ``run_id``.  Pure; no broker contact."""
    safe = sanitize_run_id(run_id)
    return {lane: f'orsim_{lane}_{safe}' for lane in LANES}


def is_reapable_lane_topic(name: str) -> bool:
    """True only for a per-run ORSim lane topic.

    The single gate every delete in this module passes through.  Deliberately
    strict: a name that merely *starts with* ``orsim_`` does not qualify.
    """
    return bool(_REAPABLE.match(name))


def _monkey_patched() -> bool:
    """True when this process's stdlib has been eventlet-patched.

    The one thing that changes admin behaviour in this module -- see the module
    docstring's ``concurrent.futures`` section.
    """
    try:
        import eventlet.patcher
        return bool(eventlet.patcher.is_monkey_patched('socket'))
    except Exception:  # pragma: no cover - eventlet absent or too old
        return False


#: One ``AdminClient`` per bootstrap address, CACHED -- and the cache is
#: load-bearing, not an optimisation.  ``create_topics()`` is issued
#: fire-and-forget on the monkey-patched path (see the module docstring), so if
#: the client were a local that went out of scope, CPython would collect it and
#: librdkafka would tear the handle down with the CreateTopics request still in
#: flight.  Measured: exactly that, two of three lane topics never created and a
#: 10 s confirm timeout with `last error: None`.
_ADMIN_CLIENTS: Dict[str, object] = {}


def _admin(bootstrap: Optional[str]):
    """The cached ``AdminClient`` for ``bootstrap``.

    ``confluent_kafka`` is imported here, never at module scope:
    ``apps.transport.__init__`` promises that importing the package costs
    nothing on the paho path, and that promise is only worth anything if every
    ``confluent_kafka`` import in the package is function-local.
    """
    from confluent_kafka.admin import AdminClient

    from apps.config import kafka_config

    address = bootstrap or kafka_config['bootstrap_servers']
    client = _ADMIN_CLIENTS.get(address)
    if client is None:
        client = AdminClient({'bootstrap.servers': address})
        _ADMIN_CLIENTS[address] = client
    return client


def ensure_run_lane_topics(
    run_id: str,
    bootstrap: Optional[str] = None,
    *,
    bcast_partitions: int = DEFAULT_BCAST_PARTITIONS,
    reply_partitions: int = DEFAULT_REPLY_PARTITIONS,
    app_partitions: int = DEFAULT_APP_PARTITIONS,
    retention_hours: int = DEFAULT_RETENTION_HOURS,
    retention_bytes: int = DEFAULT_RETENTION_BYTES,
    confirm_timeout_s: float = DEFAULT_CONFIRM_TIMEOUT_S,
) -> Dict[str, int]:
    """Create the run's three lane topics if absent; return their REAL partition counts.

    The returned counts come from broker metadata, not from the arguments: if a
    topic already existed with a different count, the broker's number is what
    both publisher and subscriber must use.  That asymmetry is the point --
    passing ``app_partitions=64`` to a run whose topic already has 16 gives back
    ``{'app': 16, ...}`` and the mapping stays consistent.

    Creation goes through the project's existing
    ``apps.utils.kafka_utils.create_topic_if_not_exists`` (which already treats
    "already exists" as success) rather than a second, subtly different
    implementation of the same idea.

    Raises:
        RuntimeError: if the topics cannot be confirmed within
            ``confirm_timeout_s``.  Raising is correct: see the module
            docstring.  The caller is an agent construction, and an agent that
            cannot see its lane topics must fail loudly rather than boot deaf.
    """
    names = lane_topic_names(run_id)
    wanted = {
        LANE_BCAST: max(1, int(bcast_partitions)),
        LANE_REPLY: max(1, int(reply_partitions)),
        LANE_APP: max(1, int(app_partitions)),
    }

    # BEFORE the fast path, because a topic that no sweep can ever collect must
    # be visible at LAUNCH rather than never.
    _warn_about_uncollectable_names(names, run_id)

    # FAST PATH, and the one a celery worker almost always takes: the launching
    # process created these at run bootstrap, so a single list_topics answers
    # everything.  No future is created, so nothing here can deadlock a
    # monkey-patched worker.
    counts = _read_partition_counts(names, bootstrap)
    if counts is not None:
        return _reject_shrunken_lanes(counts, wanted, names, run_id)

    topic_config = {
        'retention.ms': str(int(retention_hours) * 3600 * 1000),
        'retention.bytes': str(int(retention_bytes)),
    }
    _create_lane_topics(names, wanted, topic_config, bootstrap)
    counts = _confirm_partition_counts(names, bootstrap, confirm_timeout_s)
    return _reject_shrunken_lanes(counts, wanted, names, run_id)


#: Names already complained about in this process, so a per-agent-boot call does
#: not print 1060 copies of the same warning.
_WARNED_UNCOLLECTABLE = set()


def _warn_about_uncollectable_names(names: Dict[str, str], run_id: str) -> None:
    """Say so, loudly and once, when a lane topic can never be collected.

    TWO filters have to pass, and only the first one used to be checked -- which
    is exactly the gap that leaks:

    * :func:`is_reapable_lane_topic` decides what any delete is ALLOWED to
      touch.
    * :data:`_RUN_ID_STAMP` decides what the aged sweep can DATE, and
      :func:`reap_lane_topics_older_than` skips an undatable topic forever, by
      design ("undatable is not the same as stale").

    The date stamp is strictly stricter than the reap filter, so a run id that
    is reapable-but-undatable -- ``ORSIM_RUN_ID=run_mytest``, which
    ``run_container_logistics_simulation._resolve_run_id`` passes through
    verbatim -- created three topics that NO code path could ever collect, and
    nothing warned.  Finalize deliberately does not delete its own topics and
    ``retention.ms``/``retention.bytes`` expire DATA rather than topics, so the
    aged sweep is the only collector there is.  The broker was found carrying
    exactly this debris (``orsim_*_run_probe6_2823712`` and friends), left by
    the phase-2 suite's own undated fixtures.
    """
    for name in names.values():
        if name in _WARNED_UNCOLLECTABLE:
            continue
        reapable = is_reapable_lane_topic(name)
        datable = _run_started_at(name) is not None
        if reapable and datable:
            continue
        _WARNED_UNCOLLECTABLE.add(name)
        if not reapable:
            logger.warning(
                'lane topic %r (run_id=%r) does not match the reap filter %s; '
                'it will not be cleaned up automatically',
                name, run_id, _REAPABLE.pattern,
            )
        else:
            logger.warning(
                'lane topic %r (run_id=%r) carries no parseable %s timestamp, so '
                'the aged ops sweep will SKIP it forever and it will leak (3 topics '
                'per such run). Use a run id whose stamp matches %s -- e.g. '
                'run_YYYYMMDD_HHMMSS with any suffix after it.',
                name, run_id, 'run_YYYYMMDD_HHMMSS', _RUN_ID_STAMP.pattern,
            )


def _reject_shrunken_lanes(counts, wanted, names, run_id):
    """Refuse a lane topic that carries FEWER partitions than this run asked for.

    "The broker is authoritative" is the right rule and it stays -- but it is
    only safe if every bus reads the same answer, and there is one way for that
    to break badly.  This broker runs ``auto.create.topics.enable=true`` with
    ``num.partitions=1``, so a lane topic deleted while the run is still alive
    is RESURRECTED with one partition by the next request that names it.  Buses
    built before the deletion then hold ``app=64``, buses built after hold
    ``app=1``, publisher and subscriber compute different partitions from the
    same key, and records land where nobody is assigned and vanish with no error
    on either side -- the failure this module's docstring calls the worst shape
    this project has.  It was reproduced end to end: 20 publishes, zero
    deliveries, ``delivery_failures 0``, ``dropped_publishes 0``,
    ``consume_errors 0``.

    So: MORE partitions than requested is the documented, legitimate asymmetry
    (a run whose topic already had 16 while the caller passed 64 gets 16 and
    everyone agrees on 16) and is merely logged.  FEWER is the resurrection
    signature and is fatal, here, loudly, at agent construction -- which is
    exactly LIFECYCLE point 2's "fail loudly at construction" and is infinitely
    better than booting a bus that will silently disagree with its peers.
    """
    shrunk = {lane: (counts[lane], wanted[lane])
              for lane in counts if counts[lane] < wanted[lane]}
    grown = {lane: (counts[lane], wanted[lane])
             for lane in counts if counts[lane] > wanted[lane]}
    if grown:
        logger.warning(
            'lane topics for %s already exist with MORE partitions than requested '
            '%s; adopting the broker numbers, which is the documented rule',
            run_id, grown)
    if shrunk:
        raise RuntimeError(
            f'ORSim lane topic partition counts for {run_id} are SMALLER than this '
            f'run requires {shrunk} (topics {names}). A lane topic with fewer '
            'partitions than the run was configured with is the signature of a '
            'topic that was deleted mid-run and auto-recreated by the broker at '
            'num.partitions=1. Continuing would make publishers and subscribers '
            'compute different partitions from the same key, and the records '
            'would vanish with no error anywhere. Delete the lane topics while '
            'NO run is using them and let the launcher recreate them.'
        )
    return counts


def _create_lane_topics(names, wanted, topic_config, bootstrap) -> None:
    """Create the three lane topics, by whichever road this process can survive.

    Unpatched -- the run launcher, the scheduler's asyncio process, pytest --
    this is the project's existing ``create_topic_if_not_exists``, unchanged,
    including its "already exists is success" handling.

    Monkey-patched, the same helper would hang forever on ``future.result()``
    (see the module docstring), so the request is issued fire-and-forget and
    :func:`_confirm_partition_counts` establishes the outcome from broker
    metadata instead.  That is strictly more information than the future
    carries anyway: the future says "the create call succeeded", metadata says
    "the partitions exist and there are N of them", and N is what the mapping
    actually depends on.
    """
    if not _monkey_patched():
        from apps.utils.kafka_utils import create_topic_if_not_exists

        for lane, name in names.items():
            create_topic_if_not_exists(
                topic_name=name,
                num_partitions=wanted[lane],
                replication_factor=1,
                config=topic_config,
            )
        return

    from confluent_kafka.admin import NewTopic

    new_topics = [
        NewTopic(name, num_partitions=wanted[lane], replication_factor=1,
                 config=dict(topic_config))
        for lane, name in names.items()
    ]
    try:
        # The returned futures are DELIBERATELY not awaited.  Waiting is what
        # deadlocks; letting librdkafka resolve them onto a green Condition that
        # has no waiter is an uncontended acquire/release and is safe.
        _admin(bootstrap).create_topics(new_topics)
    except Exception as exc:  # noqa: BLE001 - confirmed by metadata, not by this call
        logger.warning('lane-topic create request failed (%s); '
                       'falling through to the metadata confirm', exc)


def _read_partition_counts(names: Dict[str, str], bootstrap: Optional[str]):
    """``{lane: n_partitions}`` if EVERY lane topic is READY, else ``None``.

    One ``list_topics`` call, no futures, no retry.  Kept separate from
    :func:`_confirm_partition_counts` so the fast path cannot accidentally
    inherit its 10 s retry budget.
    """
    try:
        metadata = _admin(bootstrap).list_topics(timeout=5)
    except Exception as exc:  # noqa: BLE001
        logger.debug('lane-topic metadata read failed: %s', exc)
        return None
    counts, _missing = _counts_from_metadata(metadata, names)
    return counts


def _counts_from_metadata(metadata, names: Dict[str, str]):
    """``({lane: n}, [])`` when every lane topic is ready, else ``(None, missing)``.

    "Ready" means present, error-free, non-empty AND **every partition has an
    elected leader**.  The leader check is not pedantry: immediately after
    creation a partition exists in metadata with ``leader == -1``, and the very
    next thing the bus does is ask that partition for its high watermark, which
    then fails with ``NOT_LEADER_FOR_PARTITION``.  The bus falls back to
    ``OFFSET_END`` in that case, and ``OFFSET_END`` carries exactly the
    under-delivery race that resolving the watermark ourselves exists to remove
    -- so a run could silently drop the first records published to a
    freshly-created lane.  Observed as an intermittent
    ``check_subscribe_in_effect_on_return`` failure; waiting for the leader here
    is the root fix.
    """
    counts, missing = {}, []
    for lane, name in names.items():
        topic_meta = metadata.topics.get(name)
        if topic_meta is None or getattr(topic_meta, 'error', None) is not None \
                or not topic_meta.partitions:
            missing.append(name)
            continue
        if any(getattr(part, 'leader', -1) < 0 or getattr(part, 'error', None) is not None
               for part in topic_meta.partitions.values()):
            missing.append(name)
            continue
        counts[lane] = len(topic_meta.partitions)
    if missing:
        return None, missing
    return counts, []


def _confirm_partition_counts(
    names: Dict[str, str], bootstrap: Optional[str], timeout_s: float
) -> Dict[str, int]:
    """Poll broker metadata until every lane topic reports its partitions."""
    admin = _admin(bootstrap)
    deadline = time.monotonic() + timeout_s
    last_error = None
    missing: List[str] = list(names.values())
    while True:
        try:
            metadata = admin.list_topics(timeout=min(5.0, max(0.5, deadline - time.monotonic())))
            counts, missing = _counts_from_metadata(metadata, names)
            if counts is not None:
                logger.info('ORSim lane topics confirmed: %s', counts)
                return counts
        except Exception as exc:  # noqa: BLE001 - retried until the deadline, then re-raised
            last_error = exc
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f'ORSim lane topics could not be confirmed on the broker within '
                f'{timeout_s}s: {missing or list(names.values())} '
                f'(last error: {last_error!r}). The transport refuses to start '
                'rather than publish to partitions nobody is assigned to.'
            )
        time.sleep(0.25)


def reap_run_lane_topics(run_id: str, bootstrap: Optional[str] = None) -> List[str]:
    """Delete this run's three lane topics.  Best effort; never raises.

    **NOT SAFE TO CALL WHILE THE RUN IS STILL IN FLIGHT, AND THAT IS WHY IT IS
    NO LONGER CALLED AT RUN FINALIZE.**  Deleting a lane topic that anything is
    still publishing to opens a gap in which the broker
    (``auto.create.topics.enable=true``, ``num.partitions=1``) recreates it with
    ONE partition; from then on buses built before and after the gap disagree on
    the partition count and records vanish silently on both sides.  Reproduced
    end to end: a live subscriber holding ``app=64`` received 0 of 20 records a
    freshly-built ``app=1`` publisher reported as delivered successfully.

    At finalize the run is not actually quiet: CLAUDE.md 6.22's pruned
    stragglers are still replying and every bus lingers for
    ``BUS_LINGER_S`` (30 s) with its consumer assigned.  So finalize now runs
    the AGED sweep instead (:func:`reap_lane_topics_older_than`), and
    ``retention.ms`` -- 6 h, set at creation -- bounds the leak in the meantime.

    Keep using this one for TESTS, which own their run ids and know they are
    finished, and for a deliberate operator cleanup of a run that is definitely
    dead.
    """
    return _delete_topics(list(lane_topic_names(run_id).values()), bootstrap)


#: The youngest a run may be before the ops sweep is allowed to delete its lane
#: topics.  A sweep is an UNTARGETED delete -- it hits every run on the broker
#: that matches the filter -- and it has no way to ask whether a run is alive.
#: Runs on this box finish in 6-16 minutes (CLAUDE.md 6.24), so an hour is many
#: multiples of the longest one ever measured while still collecting same-day
#: debris.  ``hours`` below is CLAMPED to this, loudly, rather than obeyed: an
#: ops sweep that silently deletes a live run's topics is the same defect as the
#: finalize reap it replaced.
MIN_SWEEP_AGE_HOURS = 1.0


def reap_lane_topics_older_than(
    hours: float, bootstrap: Optional[str] = None, dry_run: bool = False
) -> List[str]:
    """The ops sweep for lane topics left behind by killed runs.

    Ages a topic by the ``run_YYYYMMDD_HHMMSS`` stamp inside its own name (Kafka
    exposes no creation time).  A lane topic whose run id does not carry that
    stamp is SKIPPED -- undatable is not the same as stale.

    ``hours`` is clamped to :data:`MIN_SWEEP_AGE_HOURS` under ``dry_run=False``.
    A sweep cannot tell a live run from a dead one, and deleting a live run's
    lane topics does not merely inconvenience it -- it re-partitions it (see
    :func:`reap_run_lane_topics`).  A dry run is free to look at anything.

    Args:
        hours: delete lane topics whose run started more than this many hours ago.
        dry_run: return what WOULD be deleted without deleting it.

    Returns:
        The topic names deleted (or, under ``dry_run``, selected).
    """
    if not dry_run and float(hours) < MIN_SWEEP_AGE_HOURS:
        logger.error(
            'lane-topic sweep asked for age %.3fh, which is below the %.1fh floor; '
            'clamping. A sweep cannot tell a live run from a dead one, and '
            'deleting a live run\'s lane topics silently re-partitions it.',
            float(hours), MIN_SWEEP_AGE_HOURS)
        hours = MIN_SWEEP_AGE_HOURS

    admin = _admin(bootstrap)
    try:
        metadata = admin.list_topics(timeout=10)
    except Exception as exc:  # noqa: BLE001
        logger.warning('lane-topic sweep could not read broker metadata: %s', exc)
        return []

    cutoff = time.time() - float(hours) * 3600.0
    doomed = []
    for name in metadata.topics:
        if not is_reapable_lane_topic(name):
            continue
        started = _run_started_at(name)
        if started is None:
            logger.info('lane-topic sweep skipping %r: run id carries no parseable timestamp', name)
            continue
        if started < cutoff:
            doomed.append(name)

    if dry_run or not doomed:
        return doomed
    return _delete_topics(doomed, bootstrap)


def _run_started_at(topic_name: str) -> Optional[float]:
    """Epoch seconds parsed from the ``run_YYYYMMDD_HHMMSS`` inside ``topic_name``.

    A stamp in the FUTURE is refused (``None``), because ``started < cutoff``
    could never be true for it and the sweep would quietly carry it forever
    while reporting nothing: the broker was found holding
    ``orsim_*_run_20990101_00000{1,2,3}`` in exactly that state.  ``None`` keeps
    the module's rule intact -- undatable is not the same as stale, and a sweep
    may not guess -- and the creation-time warning in
    :func:`_warn_about_uncollectable_names` is what makes it visible instead of
    silent.
    """
    match = _RUN_ID_STAMP.match(topic_name)
    if not match:
        return None
    try:
        started = datetime.strptime(match.group(2), '%Y%m%d_%H%M%S').timestamp()
    except ValueError:
        return None
    # One day of slack for a clock skew between this process and whatever minted
    # the run id; beyond that the stamp is wrong, not merely fresh.
    if started > time.time() + 86400.0:
        logger.warning(
            'lane topic %r carries a FUTURE run stamp (%s); treating it as '
            'undatable rather than as eternally young. It must be reaped by name.',
            topic_name, match.group(2))
        return None
    return started


def _delete_topics(names: Iterable[str], bootstrap: Optional[str]) -> List[str]:
    """Delete ``names``, refusing anything the reap filter does not accept.

    The filter is applied HERE as well as at every call site.  That is
    deliberate belt-and-braces: this is the only function in the package that
    can destroy data the dashboard reads, so it does not trust its callers.
    """
    safe, refused = [], []
    for name in names:
        (safe if is_reapable_lane_topic(name) else refused).append(name)
    if refused:
        logger.error(
            'refusing to delete non-lane topic(s) %s: only names matching %s may be reaped',
            refused, _REAPABLE.pattern,
        )
    if not safe:
        return []

    try:
        admin = _admin(bootstrap)
        futures = admin.delete_topics(safe, operation_timeout=10)
    except Exception as exc:  # noqa: BLE001 - best effort by contract
        logger.warning('lane-topic reap could not be issued for %s: %s', safe, exc)
        return []

    if _monkey_patched():
        # Same rule as creation: never WAIT on an admin future from the hub.
        # The reap is best-effort by contract, so "issued" is a good enough
        # answer here -- the ops sweep is the backstop.
        logger.info('lane-topic reap issued (fire-and-forget, monkey-patched): %s', safe)
        return list(safe)

    deleted = []
    for name, future in futures.items():
        try:
            future.result()
            deleted.append(name)
        except Exception as exc:  # noqa: BLE001
            # "unknown topic" is the normal answer when two workers reap the
            # same run, or when the run never published anything.
            logger.info('lane-topic reap of %r did not complete: %s', name, exc)
    if deleted:
        logger.info('reaped ORSim lane topics: %s', deleted)
    return deleted
