"""Phase 3: runtime tuning for multi-day container logistics simulations."""

# STEP_INTERVAL / SIMULATION_DAYS live in scenario_config (single source of truth).

# Server run-config PATCH frequency (sim steps). 48 @ 240s ≈ 3.2 sim hours.
STATUS_UPDATE_INTERVAL_STEPS = 48

# Progress logging frequency (sim steps). 288 @ 240s = one sim day.
PROGRESS_LOG_INTERVAL_STEPS = 288

# Kafka RUNNING heartbeat while the sim executes (sim steps). 0 disables.
# This drives the dashboard's "step X/total" progress counter, so it must update
# often enough to look live. At MIN_STEP_WALL_TIME_MS=200 a healthy run does ~5
# steps/sec, so 12 steps ≈ one counter update every ~2.4s. (Was 288 = one update
# per sim-day, which left the counter visibly frozen for minutes at a time.)
KAFKA_HEARTBEAT_INTERVAL_STEPS = 12

# perf_stream — step_tick every step; step_detail on interval / new max
from apps.utils.perf_settings import (
    PERF_DETAIL_INTERVAL_STEPS,
    PERF_DETAIL_ON_NEW_MAX,
    PERF_INCLUDE_PROCESS_METRICS,
    PERF_KAFKA_FLUSH_EVERY_STEPS,
    PERF_KAFKA_INTERVAL_STEPS,
    PERF_SLOW_AGENT_TOP_N,
    PERF_TICK_EVERY_STEP,
    apply_perf_settings,
)

# Persist per-step scheduler stats to run-config (expensive for 20k+ steps).
STORE_STEP_METRICS = False

# OSRM routing at assignment.
#
# ON as of 2026-08-05 — this is the "one authoritative route per leg" architecture:
#   1. the route is planned ONCE at assignment and written to `routes.planned[leg]`
#      (truck/trip_manager.py `start_empty_reposition` / the loaded-leg transition),
#   2. the truck then MOVES along that stored route (`update_location_by_planned_route`,
#      previously dead code — its reactivation is intended, not a side effect),
#   3. the KPI measures that same route (`analytics/manager.py` prefers
#      `routes.planned[leg]["distance"]` over the haversine fallback), and
#   4. replay reads that same route back out of Mongo.
# One route, planned once, used by movement, metrics and replay — so the replayed path is
# by construction the path the truck drove, with no straight lines and no dependence on
# Kafka publish-tick sampling (which only ever captured ~72% of legs).
#
# DELIBERATE METRIC CHANGE: empty/deadhead distance becomes ROAD distance instead of
# straight-line haversine, so it is NOT comparable with runs generated before this date.
#
# `scenario_manager` stamps this onto every truck profile at scenario load, overriding
# whatever is baked into a scenario's behavior JSON, and it can still be overridden per run
# via `orsim_settings["USE_OSRM_AT_ASSIGNMENT"]`. It ships in `spec['behavior']`
# (data-at-spawn), so changing it needs no celery restart.
USE_OSRM_AT_ASSIGNMENT = True

# In-memory OSRM response cache size when routing is enabled.
OSRM_ROUTE_CACHE_MAX_ENTRIES = 512

# Per-step wait for outstanding agent responses. This bounds ONLY how long the
# scheduler waits on agents that booted but went silent — booting agents are excluded
# from the wait (pending_boot), so step 0's mass boot does NOT need a large value here.
# Steady-state steps finish in ~2–3s with a healthy backend, so a silent agent at tens
# of seconds is dead, not slow. The previous 180s meant a single straggler stalled an
# entire step for 3 minutes; 30s prunes it ~6x faster while still leaving generous
# margin for a heavy step. (Raise via STEP_TIMEOUT if a real backend is genuinely slow.)
LONG_RUN_STEP_TIMEOUT = 30

# Grace period (seconds) before the scheduler stops waiting on a within-tolerance
# set of stragglers and prunes them. Healthy steps finish in well under a second, so
# 2s comfortably separates a transiently-busy agent from a dead one — and caps the
# per-step cost of a lone straggler at ~2s instead of the full LONG_RUN_STEP_TIMEOUT.
LONG_RUN_STEP_SETTLE_SECONDS = 2

# Consecutive over-tolerance step timeouts before the run is aborted. A single step
# where >STEP_TIMEOUT_TOLERANCE of agents fail to respond is no longer fatal: those
# agents are pruned and the run continues. The run only gives up after this many such
# steps in a row, so a multi-day run survives transient stalls / individual agent
# deaths but still bails on a genuine cascade.
LONG_RUN_STEP_TIMEOUT_ABORT_CONSECUTIVE = 3

# Post-horizon drain bounds (see orsim_config.py for semantics). At 240s/step these are
# 360 steps/day, so 60 ≈ 4 sim-hours of grace for in-flight trips to finish and 90 ≈ 6
# sim-hours as the hard termination backstop. Orders that can never be served self-cancel
# within the grace window; the cap guarantees the run ends even if an agent gets stuck.
LONG_RUN_POST_HORIZON_GRACE_STEPS = 60
LONG_RUN_POST_HORIZON_DRAIN_MAX_STEPS = 90

# Analytics aggregation cadence (sim steps). 48 @ 240s ≈ every 3.2 sim hours.
# Drives the KPI refresh rate the dashboard sees (trip geo can use its own cadence).
ANALYTICS_STEPS_PER_ACTION = 48

# Assignment cadence (sim steps). 28 @ 240s ≈ every 1.9 sim hours.
ASSIGNMENT_STEPS_PER_ACTION = 28

# Cap unassigned orders considered per assignment tick (RandomAssignment drains backlog over time).
ASSIGNMENT_MAX_ORDERS_PER_TICK = 500

# Max order agents to Celery-boot per scheduler step. Spreads the ~600 early
# orders (and any step with many simultaneous request_time_steps) across
# several ticks instead of one 9 s spike at step 0.
ORDER_SPAWN_MAX_PER_STEP = 40

# Optional floor on wall-clock time between scheduler steps (milliseconds).
# 0 = disabled. When steps get very fast, a small floor (~50 ms) prevents
# Mongo/RabbitMQ write bursts from outrunning backend capacity.
#
# It also paces the run so the live dashboard has something to watch. But it is a
# hard tax on every step: at 2520 steps, 200 ms alone floored a 7-day run at
# 8.4 min — over the ≤10-min budget for the 1000-truck consortium runs before any
# real work. Measured on the 7d consortium dashboard runs (2026-07-31): light
# steps do 50-100 ms of real work, so a 100 ms floor added ~70 s of pure sleep
# while a 50 ms floor adds ~1 s. 50 ms still paces the dashboard (a KPI window
# every 48 steps ≥ ~2.4 s in the lightest phases; heavy phases run above the
# floor anyway).
MIN_STEP_WALL_TIME_MS = 50

# Bounded wait for unassigned orders (steps; 0 disables). An order still
# unassigned this long after its request step self-cancels (same terminal state
# the horizon cleanup would give it — cancelled ≠ error) instead of parking
# until the end of the run. With demand beyond fleet throughput (e.g. the 30k
# order / 1000 truck consortium runs at ~2.5x capacity) the parked backlog
# otherwise grows to tens of thousands of live agents, and each live agent
# holds an MQTT connection — past ~20k the host exhausts ephemeral ports
# (EADDRINUSE) and later order agents silently never boot, truncating demand.
# Bounding the wait keeps the live population turning over, every order agent
# boots, and the backlog an order actually competes in stays honest.
# 360 @ 240s = 24 sim-hours — still far beyond any wait a serveable order sees
# (matches get made within hours; a monotonic backlog never shrinks), and at
# 2.5x-oversubscribed demand it keeps peak live agents (~parked(24h) + fleet +
# in-flight ≈ 6-7k) comfortably under the ~20k connection ceiling. 48h (720)
# was measured to still brush EADDRINUSE at 30k-order scale.
UNASSIGNED_ORDER_MAX_WAIT_STEPS = 360

# Nap policy for unassigned order agents (see OpenRideAgent sleep protocol).
# -1 (default) = indefinite event-driven nap: the parked order also drops out of
# the step-broadcast fanout entirely (at 30k cumulative orders that fanout was a
# per-step RabbitMQ cost growing all run); it wakes on any app-topic event and
# is direct-notified at shutdown. A positive value opts back into timed
# check-in naps (agent stays subscribed, wakes every N steps).
UNASSIGNED_ORDER_SLEEP_STEPS = -1


def apply_long_run_orsim_settings(settings: dict) -> dict:
    """Merge Phase 3 knobs into an orsim_settings dict."""
    settings = dict(settings)
    settings["STEP_TIMEOUT"] = LONG_RUN_STEP_TIMEOUT
    settings["STEP_SETTLE_SECONDS"] = LONG_RUN_STEP_SETTLE_SECONDS
    settings["STEP_TIMEOUT_ABORT_CONSECUTIVE"] = LONG_RUN_STEP_TIMEOUT_ABORT_CONSECUTIVE
    settings["RUNTIME_STATUS_UPDATE_INTERVAL_STEPS"] = STATUS_UPDATE_INTERVAL_STEPS
    settings["RUNTIME_PROGRESS_LOG_INTERVAL_STEPS"] = PROGRESS_LOG_INTERVAL_STEPS
    settings["RUNTIME_STORE_STEP_METRICS"] = STORE_STEP_METRICS
    settings["KAFKA_HEARTBEAT_INTERVAL_STEPS"] = KAFKA_HEARTBEAT_INTERVAL_STEPS
    settings["PERF_KAFKA_INTERVAL_STEPS"] = PERF_KAFKA_INTERVAL_STEPS
    settings["PERF_DETAIL_INTERVAL_STEPS"] = PERF_DETAIL_INTERVAL_STEPS
    settings["PERF_DETAIL_ON_NEW_MAX"] = PERF_DETAIL_ON_NEW_MAX
    settings["PERF_SLOW_AGENT_TOP_N"] = PERF_SLOW_AGENT_TOP_N
    settings["PERF_INCLUDE_PROCESS_METRICS"] = PERF_INCLUDE_PROCESS_METRICS
    settings["PERF_KAFKA_FLUSH_EVERY_STEPS"] = PERF_KAFKA_FLUSH_EVERY_STEPS
    settings["USE_OSRM_AT_ASSIGNMENT"] = USE_OSRM_AT_ASSIGNMENT
    settings["OSRM_ROUTE_CACHE_MAX_ENTRIES"] = OSRM_ROUTE_CACHE_MAX_ENTRIES
    settings["ORDER_SPAWN_MAX_PER_STEP"] = ORDER_SPAWN_MAX_PER_STEP
    settings["MIN_STEP_WALL_TIME_MS"] = MIN_STEP_WALL_TIME_MS
    settings["UNASSIGNED_ORDER_SLEEP_STEPS"] = UNASSIGNED_ORDER_SLEEP_STEPS
    settings["UNASSIGNED_ORDER_MAX_WAIT_STEPS"] = UNASSIGNED_ORDER_MAX_WAIT_STEPS
    settings["ALLOW_POST_HORIZON_DRAIN"] = True
    settings["POST_HORIZON_GRACE_STEPS"] = LONG_RUN_POST_HORIZON_GRACE_STEPS
    settings["POST_HORIZON_DRAIN_MAX_STEPS"] = LONG_RUN_POST_HORIZON_DRAIN_MAX_STEPS
    return apply_perf_settings(settings)


def steps_per_simulated_hour():
    from .scenario_config import STEP_INTERVAL_SECONDS

    return 3600 // STEP_INTERVAL_SECONDS


def steps_per_simulated_day():
    from .scenario_config import SIMULATION_DAYS, simulation_length_in_steps

    return simulation_length_in_steps() // max(1, SIMULATION_DAYS)
