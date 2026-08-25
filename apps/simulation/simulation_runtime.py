import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime
from enum import Enum
from typing import Callable, Optional

import cerberus
from orsim.runtime import FixedStepTermination, ORSimRuntime

from apps.common.resource_client_mixin import get_http_session
from apps.common.statemachine_registry import StateMachineRegistry
from apps.common.user_registry import UserRegistry
from apps.config import messenger_backend, settings, simulation_domains
from apps.simulation.batched_agent_source import BatchedPrecomputedAgentSource
from apps.simulation.openride_scheduler import OpenRideScheduler
from apps.simulation.run_status_hooks import install_terminal_status_publisher

logger = logging.getLogger(__name__)


class StatusWrite(Enum):
    """Outcome of a ``run_config`` status PATCH (R3-4).

    THE GOVERNING PRINCIPLE, and the reason this enum exists at all:

        **Fail-soft is correct for an *input*** — an unknown policy name degrades to a
        default, the run continues, and nothing persisted is wrong.
        **Fail-soft is WRONG for a *state write*** — swallowing it leaves stored state
        contradicting reality.

    The previous implementation collapsed 412-after-retry, 500, 401, timeout and exception
    into a single ``None``, and both call sites then wrote ``... or self.run_record``. That
    traded a loud crash for silent wrong state: a run that completed successfully could
    record ``status: "In Progress"`` forever while the runtime proceeded as if the write had
    landed. That is CLAUDE.md §6.12's stale-run family reintroduced at the writer.

    ``OK``/``RETRIED_OK`` are successes; ``RETRIED_OK`` specifically is NOT a failure — the
    run record legitimately has more than one writer (the pooled assignment agent stamps
    ``meta.market`` from inside celery), so Eve's optimistic concurrency rejecting a cached
    etag is expected traffic, not an error.
    """

    OK = "ok"
    RETRIED_OK = "retried_ok"
    STALE_ETAG_EXHAUSTED = "stale_etag_exhausted"
    SERVER_ERROR = "server_error"
    EXCEPTION = "exception"

    @property
    def succeeded(self) -> bool:
        return self in (StatusWrite.OK, StatusWrite.RETRIED_OK)


# R3-9: bounded 412-only retry. Kept short — this sits on the sim's per-step status path.
STATUS_PATCH_MAX_ATTEMPTS = 3
STATUS_PATCH_BACKOFF_BASE_S = 0.05
STATUS_PATCH_BACKOFF_CAP_S = 0.5

# The terminal ("success") write decides whether the run is discoverable as finished, so it
# is retried as a whole — including the outcomes update_status itself refuses to retry.
TERMINAL_STATUS_PATCH_MAX_ATTEMPTS = 3
TERMINAL_STATUS_BACKOFF_BASE_S = 0.25
TERMINAL_STATUS_BACKOFF_CAP_S = 2.0

# R3-12: eve 1.1.5 runs with BANDWIDTH_SAVER defaulting True and openride_server sets no
# override, so a POST/PATCH response body carries ONLY these keys.
RUN_RECORD_METADATA_KEYS = frozenset(
    {"_id", "_etag", "_updated", "_created", "_status", "_links", "_version", "_latest_version"}
)


def _backoff_delay_s(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with full jitter, bounded by ``cap``.

    Jitter matters because the competing writer is a periodic celery tick: a fixed backoff
    re-collides with it at the same phase on every retry.
    """
    ceiling = min(cap, base * (2 ** max(0, attempt - 1)))
    return random.uniform(0.0, ceiling)


class SimulationRuntime(ORSimRuntime):
    def __init__(
        self,
        run_id,
        domain,
        scenario_manager,
        datahub_dir,
        agent_config,
        statemachine_collection,
        scheduler_config,
        progress_listener: Optional[Callable[["SimulationRuntime", int, int], None]] = None,
        agent_source=None,
        termination_condition=None,
        run_name: str | None = None,
    ):
        self.domain = domain
        self.scenario_manager = scenario_manager
        self.run_name = (run_name or "").strip() or None
        self.datahub_dir = datahub_dir
        self.reference_time = scenario_manager.reference_time
        self.current_time = self.reference_time
        self.statemachine_collection = statemachine_collection
        self.orsim_settings = scenario_manager.orsim_settings
        self.steps = self.orsim_settings["SIMULATION_LENGTH_IN_STEPS"]
        self.parent_path = os.path.dirname(os.path.abspath(os.getcwd()))
        self.progress_listener = progress_listener
        self.status_update_interval = max(
            1, int(self.orsim_settings.get("RUNTIME_STATUS_UPDATE_INTERVAL_STEPS", 1))
        )
        self.progress_log_interval = max(
            1, int(self.orsim_settings.get("RUNTIME_PROGRESS_LOG_INTERVAL_STEPS", 1))
        )
        self.store_step_metrics = bool(
            self.orsim_settings.get("RUNTIME_STORE_STEP_METRICS", True)
        )
        self.kafka_heartbeat_interval = int(
            self.orsim_settings.get("KAFKA_HEARTBEAT_INTERVAL_STEPS", 0)
        )
        default_perf_interval = self.orsim_settings.get(
            "RUNTIME_STATUS_UPDATE_INTERVAL_STEPS", 10
        )
        self.perf_detail_interval = max(
            1,
            int(
                self.orsim_settings.get(
                    "PERF_DETAIL_INTERVAL_STEPS",
                    self.orsim_settings.get("PERF_KAFKA_INTERVAL_STEPS", default_perf_interval),
                )
            ),
        )
        self.perf_detail_on_new_max = bool(
            self.orsim_settings.get("PERF_DETAIL_ON_NEW_MAX", True)
        )
        self.perf_slow_agent_top_n = max(
            1, int(self.orsim_settings.get("PERF_SLOW_AGENT_TOP_N", 10))
        )
        self.perf_include_process = bool(
            self.orsim_settings.get("PERF_INCLUDE_PROCESS_METRICS", False)
        )
        self.perf_flush_every = max(
            1, int(self.orsim_settings.get("PERF_KAFKA_FLUSH_EVERY_STEPS", 10))
        )
        self.perf_tick_every_step = bool(
            self.orsim_settings.get("PERF_TICK_EVERY_STEP", True)
        )
        from apps.utils.perf_rollup import PerfRollup

        self._perf_rollup = PerfRollup()
        self._step_detail_sent_steps: set = set()
        self._step_wall_times_ms: list = []
        self._last_step_scheduler_ms: dict = {}
        self.min_step_wall_time_ms = max(
            0, int(self.orsim_settings.get("MIN_STEP_WALL_TIME_MS", 0))
        )
        self.order_spawn_max_per_step = max(
            1, int(self.orsim_settings.get("ORDER_SPAWN_MAX_PER_STEP", 40))
        )

        try:
            from apps.utils.perf_metrics import process_metrics

            process_metrics()
        except Exception:
            pass

        self.validate_agent_config(agent_config)

        if agent_source is None:
            agent_source = BatchedPrecomputedAgentSource(
                scenario_manager=scenario_manager,
                agent_config=agent_config,
                run_id=run_id,
                reference_time=self.reference_time,
                project_path=self.parent_path,
                context=self,
                order_spawn_max_per_step=self.order_spawn_max_per_step,
            )
        if termination_condition is None:
            termination_condition = FixedStepTermination(self.steps)

        super().__init__(
            run_id=run_id,
            scheduler_config=scheduler_config,
            agent_source=agent_source,
            termination_condition=termination_condition,
            messenger_backend=messenger_backend,
        )

        self.user = None
        # R3-12: a METADATA STUB after ``init_run_config``, never the run's own document.
        self.run_record = None
        self.execution_start_time = 0.0
        self._terminal_status_publisher = None
        self._init_status_write_counters()

    def _init_status_write_counters(self) -> None:
        """R3-4 ledger of status-write outcomes, surfaced in the perf summary."""
        self.status_write_failures = 0
        self.status_write_outcomes: dict = {}
        self.consecutive_status_write_failures = 0
        self.terminal_status_write: Optional[StatusWrite] = None

    def _record_status_write(self, outcome: StatusWrite) -> None:
        self.status_write_outcomes[outcome.value] = (
            self.status_write_outcomes.get(outcome.value, 0) + 1
        )
        if outcome.succeeded:
            self.consecutive_status_write_failures = 0
        else:
            self.status_write_failures += 1
            self.consecutive_status_write_failures += 1

    def _apply_status_write(self, result: tuple) -> StatusWrite:
        """Keep the returned record ONLY on ``OK``/``RETRIED_OK``; count anything else.

        On a failure the cached etag is knowingly stale, so the next PATCH is guaranteed to
        412 and pay a GET — that is the reconciliation path, and it is bounded (R3-9).
        Keeping the stale record here is deliberate (``_id`` must survive so a later attempt
        can address the document at all); what must NOT survive is the pretence that the
        write landed.
        """
        record, outcome = result
        self._record_status_write(outcome)
        if outcome.succeeded:
            self.run_record = record
        return outcome

    def _instantiate_schedulers(self, scheduler_config: dict) -> dict:
        # Use the app-side OpenRideScheduler (incremental O(1) step barrier +
        # pending_boot / consecutive-timeout robustness) instead of stock
        # ORSimScheduler, keeping the orsim library untouched (CLAUDE.md §7).
        return {
            key: OpenRideScheduler(**params) for key, params in scheduler_config.items()
        }

    def validate_agent_config(self, agent_config):
        agent_cfg_schema = {
            "scheduler_key": {"type": "string", "required": True},
            "agent_class": {"type": "string", "required": True},
            "init_time_step_key": {"nullable": True},
            "extra_fields": {"nullable": True},
        }
        agent_config_schema = {
            role: {"type": "dict", "schema": agent_cfg_schema, "required": True}
            for role in agent_config.keys()
        }
        v = cerberus.Validator()
        if not v.validate(agent_config, agent_config_schema):
            raise ValueError(f"Invalid agent_config: {v.errors}")

    def on_before_run(self):
        self.execution_start_time = time.time()
        self.user = self.setup_user()
        self.run_record = self.init_run_config()
        self.register_state_machines()
        self._precreate_agent_docs()

    def _precreate_agent_docs(self):
        """Bulk pre-create truck + facility REST docs so each agent adopts its document
        instead of POSTing at boot — collapsing the ~5000-truck create stampede that drops
        connections and loses facilities + trucks (container_logistics only).

        Runs after register_state_machines() (so ``statemachine.id`` can be bound) and before
        the first agent spawns (no GET/insert race). Best-effort: a failure here must not abort
        an otherwise-runnable run — agents would fall back to creating their own docs.
        """
        if self.domain != simulation_domains.get("container_logistics", "container-logistics-sim"):
            return
        try:
            from apps.container_logistics.precreate import precreate_agent_docs

            # Order-lifecycle ``service`` mode: orders have no agent to create their own
            # document, so the whole population is pre-created here (the only place holding
            # the full order behavior collection in memory). ``agents`` mode passes nothing
            # new and the call is byte-identical to before.
            extra = {}
            if self.orsim_settings.get("ORDER_LIFECYCLE", "agents") == "service":
                lifecycle_behaviors = self.scenario_manager.get_agent_collection("order_lifecycle") or {}
                owner = next(iter(lifecycle_behaviors.values()), {}).get("email")
                extra = {
                    "order_behaviors": self.scenario_manager.get_agent_collection("order"),
                    "order_owner_email": owner or "order_lifecycle_main@test.com",
                }

            stats = precreate_agent_docs(
                run_id=self.run_id,
                truck_behaviors=self.scenario_manager.get_agent_collection("truck"),
                facility_behaviors=self.scenario_manager.get_agent_collection("facility"),
                sim_clock=self.reference_time,
                **extra,
            )
            logger.info("Run %s: pre-created agent docs %s", self.run_id, stats)
        except Exception:
            logger.exception("Run %s: agent doc pre-create failed (continuing).", self.run_id)

    def on_simulation_complete(self, elapsed: float):
        self._finalize_unserved_orders()
        self._finalize_kpi_breakdowns()
        # R3-4: the terminal status write happens BEFORE the perf summary, so the summary
        # can carry its outcome. That ordering is what makes a failed terminal write
        # detectable from the perf stream instead of only by reading Mongo.
        self._write_terminal_status(elapsed)
        self._publish_perf_summary(elapsed)

    def _write_terminal_status(self, elapsed: float) -> StatusWrite:
        """Write the terminal ``success`` status, retrying with backoff, and be LOUD if it fails.

        This is the write that decides whether the run is discoverable as finished, so unlike
        the per-step writes it retries every failing outcome, not just stale etags. If it
        still fails, a terminal ``run_status`` is published so the run does not sit as
        "In Progress" with no other trace — plus a ``logging.error`` and a counter carried in
        the perf summary, because a failure that is only inferable from a missing field is
        not detectable (the gate reads run status from Mongo and would see the lie, not the
        failure).
        """
        outcome = StatusWrite.EXCEPTION
        for attempt in range(1, TERMINAL_STATUS_PATCH_MAX_ATTEMPTS + 1):
            outcome = self._apply_status_write(self.update_status("success", elapsed))
            if outcome.succeeded:
                self.terminal_status_write = outcome
                return outcome
            if attempt < TERMINAL_STATUS_PATCH_MAX_ATTEMPTS:
                time.sleep(
                    _backoff_delay_s(
                        attempt, TERMINAL_STATUS_BACKOFF_BASE_S, TERMINAL_STATUS_BACKOFF_CAP_S
                    )
                )
        self.terminal_status_write = outcome
        logging.error(
            "TERMINAL STATUS WRITE FAILED run_id=%s outcome=%s attempts=%s: the run COMPLETED "
            "but run_config.status stays 'In Progress'. Publishing a terminal run_status so the "
            "run is still discoverable as finished.",
            self.run_id,
            outcome.value,
            TERMINAL_STATUS_PATCH_MAX_ATTEMPTS,
        )
        self._publish_terminal_run_status(
            f"terminal run_config PATCH failed ({outcome.value}) after "
            f"{TERMINAL_STATUS_PATCH_MAX_ATTEMPTS} attempts; run_config.status is stale"
        )
        return outcome

    def _publish_terminal_run_status(self, reason: str, status: str = "COMPLETED") -> None:
        publisher = self._terminal_status_publisher
        try:
            if publisher is not None:
                publisher(reason, status=status)
                return
            from apps.utils import kafka_utils

            kafka_utils.push_run_status(
                kafka_utils.resolve_topic("run_status"), self.run_id, status, msg=reason
            )
            kafka_utils.flush_producer(3)
        except Exception:
            logger.exception(
                "run_id=%s: terminal run_status publish ALSO failed after a failed terminal "
                "status PATCH — the run has no terminal record anywhere.",
                self.run_id,
            )

    def _finalize_kpi_breakdowns(self):
        """Authoritative end-of-run per-truck/haulier breakdown recompute (container_logistics).

        A full scan of completed trips written with ``final=True``, independent of the analytics
        agent's in-memory accumulators, so the definitive distribution + company numbers survive
        an analytics-agent restart. Best-effort — must not fail an otherwise-successful run.
        """
        if self.domain != simulation_domains.get("container_logistics", "container-logistics-sim"):
            return
        try:
            from apps.container_logistics.analytics.manager import AnalyticsManager
            from apps.utils import time_to_str

            sim_clock = getattr(self, "current_time", None)
            sim_clock_str = time_to_str(sim_clock) if sim_clock else None
            manager = AnalyticsManager(self.run_id, sim_clock_str, self.user, persona=None)
            # Fresh manager: hand it the active cooperation structure (stamped into
            # orsim_settings at boot) so the final=True planner-scope rows exist.
            manager.set_cooperation(self.orsim_settings.get("COOPERATION"))
            manager.recompute_breakdowns_full(sim_clock_str)
            logger.info("Run %s: finalized KPI breakdowns (truck + haulier).", self.run_id)
        except Exception:
            logger.exception("Run %s: KPI breakdown finalize failed (continuing).", self.run_id)

    def _finalize_unserved_orders(self):
        """Terminalize any orders left non-terminal at run end (container_logistics only).

        Order agents leave the market locally past the horizon for a fast drain; this single
        bulk update_many cancels their now-orphaned records so the run's final data is clean
        (0 non-terminal) and unserved demand shows as ``cancelled``. Best-effort: a failure here
        must not turn an otherwise-successful run into a failure.
        """
        if self.domain != simulation_domains.get("container_logistics", "container-logistics-sim"):
            return
        try:
            from apps.container_logistics.order.bulk_cancel import bulk_cancel_nonterminal_orders

            sim_clock = getattr(self, "current_time", None)
            sim_clock_str = sim_clock.strftime("%a, %d %b %Y %H:%M:%S GMT") if sim_clock else None
            cancelled = bulk_cancel_nonterminal_orders(self.run_id, sim_clock=sim_clock_str)
            logger.info(
                "Run %s: bulk-cancelled %d unserved/stranded orders at completion.",
                self.run_id,
                cancelled,
            )
        except Exception:
            logger.exception("Run %s: bulk cancel of unserved orders failed (continuing).", self.run_id)

    def setup_user(self):
        from apps.utils import time_to_str

        credentials = {
            "email": "sim_admin@test.com",
            "password": "password",
        }
        return UserRegistry(time_to_str(datetime.now()), credentials, role="admin")

    def init_run_config(self):
        run_config_url = f"{settings['OPENRIDE_SERVER_URL']}/run-config"
        display_name = self.run_name or self.scenario_manager.get_scenario_display_name()
        run_config_data = {
            "run_id": self.run_id,
            "name": display_name,
            "status": "In Progress",
            "meta": self.scenario_manager.get_run_config_meta(),
            "step_metrics": {},
        }
        response = get_http_session().post(
            run_config_url,
            headers=self.user.get_headers(),
            data=json.dumps(run_config_data),
            timeout=settings.get("NETWORK_REQUEST_TIMEOUT", 10),
        )
        if response.status_code in (200, 201):
            return self._assert_run_record_shape(response.json())
        raise Exception(f"{response.url}, {response.text}")

    def _assert_run_record_shape(self, record):
        """Pin what ``self.run_record`` actually is: a METADATA STUB, not the run record (R3-12).

        eve 1.1.5 with ``BANDWIDTH_SAVER`` defaulting True (openride_server sets no override)
        returns only ``_id/_etag/_updated/_created/_status/_links`` from a POST or PATCH. So
        ``self.run_record`` has never carried the run's own fields after ``init_run_config``:
        ``self.run_record["meta"]`` raises KeyError in production, on a line that looks
        obviously correct. Only ``_id`` and ``_etag`` are ever read, and both are required —
        without them no later PATCH can even address the document, so a missing one is fatal
        and raises here rather than at some later dereference.
        """
        if not isinstance(record, dict):
            raise TypeError(
                f"run-config response is {type(record).__name__}, expected a dict of eve metadata"
            )
        missing = [key for key in ("_id", "_etag") if key not in record]
        if missing:
            raise ValueError(
                f"run-config response is missing required eve metadata {missing}; "
                f"got keys {sorted(record)}"
            )
        payload_keys = sorted(set(record) - RUN_RECORD_METADATA_KEYS)
        if payload_keys:
            logger.warning(
                "run-config response carries non-metadata keys %s — BANDWIDTH_SAVER appears to "
                "be off. run_record is still treated as a metadata stub; do not start reading "
                "run fields off it without re-GETting the document.",
                payload_keys,
            )
        return record

    def register_state_machines(self):
        StateMachineRegistry(
            statemachines=self.statemachine_collection, domain=self.domain
        ).register_state_machines(
            server_url=settings["OPENRIDE_SERVER_URL"],
            headers=self.user.get_headers(),
        )

    def _should_update_status(self, step_index: int) -> bool:
        if step_index <= 0:
            return True
        if step_index >= self.steps - 1:
            return True
        return step_index % self.status_update_interval == 0

    def _log_progress(self, step_index: int) -> None:
        if step_index % self.progress_log_interval != 0 and step_index not in (
            0,
            self.steps - 1,
        ):
            return
        step_interval = self.orsim_settings.get("STEP_INTERVAL", 30)
        sim_seconds = step_index * step_interval
        sim_days = sim_seconds / 86400.0
        wall_seconds = time.time() - self.execution_start_time
        logger.info(
            "Simulation progress run_id=%s step=%s/%s sim_time=%.1fd wall=%.0fs",
            self.run_id,
            step_index,
            self.steps,
            sim_days,
            wall_seconds,
        )

    def _notify_progress(self, step_index: int) -> None:
        if step_index % self.progress_log_interval == 0 or step_index in (
            0,
            self.steps - 1,
        ):
            self._log_progress(step_index)
        if (
            self.progress_listener is not None
            and self.kafka_heartbeat_interval > 0
            and step_index % self.kafka_heartbeat_interval == 0
        ):
            self.progress_listener(self, step_index, self.steps)

    def _build_step_metric(self, step_index: int):
        if not self.store_step_metrics:
            return None
        return {
            step_index: {
                key: {
                    "stat": getattr(self.schedulers[key], "agent_stat", {}).get(
                        step_index, None
                    ),
                    "run_time": self._last_step_scheduler_ms.get(key),
                }
                for key in self.schedulers
            }
        }

    def _collect_scheduler_metrics(self, step_index: int) -> dict:
        sched_metrics = {}
        for key, scheduler in self.schedulers.items():
            stat_step = getattr(scheduler, "time", step_index + 1) - 1
            stat = getattr(scheduler, "agent_stat", {}).get(stat_step, {})
            if isinstance(stat, list):
                stat = {}
            sched_metrics[key] = {
                "run_ms": self._last_step_scheduler_ms.get(key),
                "stat": stat,
            }
        return sched_metrics

    def _should_publish_step_tick(self, step_index: int) -> bool:
        if self.perf_tick_every_step:
            return True
        return step_index <= 0 or step_index >= self.steps - 1

    def _should_publish_step_detail(self, step_index: int, is_new_max: bool) -> bool:
        if is_new_max and self.perf_detail_on_new_max:
            return True
        if step_index <= 0 or step_index >= self.steps - 1:
            return True
        return step_index % self.perf_detail_interval == 0

    def _publish_step_tick(self, step_index: int, step_wall_ms: float) -> None:
        if not self._should_publish_step_tick(step_index):
            return
        try:
            from apps.utils.perf_metrics import maybe_flush_perf_producer, publish_perf

            publish_perf(
                self.run_id,
                "step_tick",
                {
                    "step_wall_ms": round(step_wall_ms, 2),
                    "rollup": self._perf_rollup.to_dict(),
                    "progress_pct": round(
                        100.0 * step_index / max(1, self.steps - 1), 2
                    ),
                },
                sim_step=step_index,
            )
            maybe_flush_perf_producer(step_index, interval=self.perf_flush_every)
        except Exception as exc:
            logger.warning(
                "perf_stream step_tick publish skipped run_id=%s: %s", self.run_id, exc
            )

    def _publish_step_detail(
        self,
        step_index: int,
        step_wall_ms: float,
        *,
        trigger: str,
        api_patch_ms: float = 0.0,
        spawn_ms: float = 0.0,
    ) -> None:
        try:
            from apps.utils.perf_metrics import (
                build_bottlenecks,
                build_step_spans,
                collect_slow_agents,
                publish_perf,
            )

            sched_metrics = self._collect_scheduler_metrics(step_index)
            bottlenecks = build_bottlenecks(
                step_wall_ms, sched_metrics, api_ms=api_patch_ms
            )
            slow_agents = collect_slow_agents(
                self.schedulers,
                step_index,
                top_n=self.perf_slow_agent_top_n,
            )
            spans = build_step_spans(
                self._last_step_scheduler_ms,
                api_ms=api_patch_ms,
                spawn_ms=spawn_ms,
            )
            publish_perf(
                self.run_id,
                "step_detail",
                {
                    "step_wall_ms": round(step_wall_ms, 2),
                    "trigger": trigger,
                    "bottlenecks": bottlenecks,
                    "slow_agents": slow_agents,
                    "spans": spans,
                    "rollup": self._perf_rollup.to_dict(),
                },
                sim_step=step_index,
            )
            self._step_detail_sent_steps.add(step_index)
        except Exception as exc:
            logger.warning(
                "perf_stream step_detail publish skipped run_id=%s: %s", self.run_id, exc
            )

    def _publish_perf_after_step(
        self,
        step_index: int,
        step_wall_ms: float,
        *,
        api_patch_ms: float = 0.0,
        spawn_ms: float = 0.0,
    ) -> None:
        is_new_max = self._perf_rollup.observe(step_index, step_wall_ms)
        self._publish_step_tick(step_index, step_wall_ms)
        if not self._should_publish_step_detail(step_index, is_new_max):
            return
        trigger = "new_max" if is_new_max else "interval"
        self._publish_step_detail(
            step_index,
            step_wall_ms,
            trigger=trigger,
            api_patch_ms=api_patch_ms,
            spawn_ms=spawn_ms,
        )

    def _publish_perf_summary(self, total_run_time: float) -> None:
        try:
            from apps.utils.perf_metrics import publish_perf

            wall_times = self._step_wall_times_ms
            summary = {
                "total_wall_s": round(total_run_time, 2),
                "steps": self.steps,
                # R3-4: the status-write ledger rides the perf summary so a failed PATCH is
                # detectable from the perf stream. A run whose terminal write failed records
                # ``status: "In Progress"`` in Mongo — reading Mongo alone cannot distinguish
                # that from a run still in flight, so the counter is the signal.
                "status_write_failures": self.status_write_failures,
                "status_write_outcomes": dict(self.status_write_outcomes),
                "terminal_status_write": (
                    self.terminal_status_write.value if self.terminal_status_write else None
                ),
            }
            if wall_times:
                sorted_w = sorted(wall_times)
                summary["p50_step_ms"] = sorted_w[len(sorted_w) // 2]
                summary["p95_step_ms"] = sorted_w[int(len(sorted_w) * 0.95)]
                summary["max_step_ms"] = self._perf_rollup.max_step_ms
                summary["max_step_index"] = self._perf_rollup.max_step_index
                summary["avg_step_ms"] = round(
                    self._perf_rollup.sum_step_ms / max(self._perf_rollup.step_count, 1),
                    2,
                )
            publish_perf(
                self.run_id,
                "summary",
                summary,
                include_process=self.perf_include_process,
                flush=True,
            )
            max_idx = self._perf_rollup.max_step_index
            if (
                max_idx >= 0
                and max_idx not in self._step_detail_sent_steps
                and max_idx < len(self._step_wall_times_ms)
            ):
                self._publish_step_detail(
                    max_idx,
                    self._step_wall_times_ms[max_idx],
                    trigger="summary",
                )
        except Exception as exc:
            logger.debug("perf_stream summary publish skipped: %s", exc)

    async def _run_schedulers_for_step(self, step: int) -> None:
        is_final = self.termination_condition.is_final_step(
            step, self.schedulers, self.agent_source
        )
        self._last_step_scheduler_ms = {}

        async def _step_one(key: str, scheduler) -> None:
            if hasattr(scheduler, "agent_stat") and hasattr(scheduler, "time"):
                # Match the dict shape the scheduler fills in via _update_agent_stat;
                # a list here was a latent TypeError for any consumer of agent_stat.
                if scheduler.time not in scheduler.agent_stat:
                    scheduler.agent_stat[scheduler.time] = {}
            sched_start = time.perf_counter()
            await scheduler.step(is_final=is_final)
            self._last_step_scheduler_ms[key] = round(
                (time.perf_counter() - sched_start) * 1000, 2
            )

        # The service agents (assignment/analytics) act on Mongo state that the
        # domain agents write asynchronously over HTTP, so there is no strict
        # intra-step ordering between the two barriers to preserve — running
        # them sequentially just added the service barrier's wall time (incl.
        # the periodic multi-second assignment/analytics ticks) on top of every
        # step. Overlap them by default; CONCURRENT_SCHEDULERS=False restores
        # the strict sequential order.
        if bool(self.orsim_settings.get("CONCURRENT_SCHEDULERS", True)) and len(self.schedulers) > 1:
            await asyncio.gather(
                *(_step_one(key, sched) for key, sched in self.schedulers.items())
            )
        else:
            for key, scheduler in self.schedulers.items():
                await _step_one(key, scheduler)

    def run_simulation(self):
        logger.info(
            "Starting simulation run_id=%s steps=%s status_patch_every=%s perf_detail_every=%s step_metrics=%s",
            self.run_id,
            self.steps,
            self.status_update_interval,
            self.perf_detail_interval,
            self.store_step_metrics,
        )
        publish_cancelled = install_terminal_status_publisher(self.run_id)
        # Reused by _write_terminal_status when the terminal PATCH fails, so a completed run
        # still gets a terminal run_status even though run_config.status is stale.
        self._terminal_status_publisher = publish_cancelled
        self.on_before_run()

        async def _simulation_loop() -> None:
            step = 0
            while not self.termination_condition.should_terminate(
                step, self.schedulers, self.agent_source
            ):
                step_start = time.perf_counter()
                spawn_start = time.perf_counter()

                for item in self.agent_source.agents_for_step(step):
                    self.schedulers[item["scheduler_key"]].add_agent(
                        spec=item["spec"],
                        project_path=item["project_path"],
                        agent_class=item["agent_class"],
                    )
                spawn_ms = (time.perf_counter() - spawn_start) * 1000

                await self._run_schedulers_for_step(step)

                step_wall_ms = (time.perf_counter() - step_start) * 1000
                self._step_wall_times_ms.append(round(step_wall_ms, 2))
                self._notify_progress(step)

                api_patch_ms = 0.0
                if self._should_update_status(step):
                    patch_start = time.perf_counter()
                    step_metric = self._build_step_metric(step)
                    # R3-4: keep the record only on OK/RETRIED_OK; every other outcome is
                    # counted and surfaced (perf summary) rather than silently swallowed.
                    self._apply_status_write(
                        self.update_status(
                            "In Progress",
                            time.time() - self.execution_start_time,
                            step_metric,
                        )
                    )
                    api_patch_ms = (time.perf_counter() - patch_start) * 1000

                self._publish_perf_after_step(
                    step, step_wall_ms, api_patch_ms=api_patch_ms, spawn_ms=spawn_ms
                )

                if self.min_step_wall_time_ms > 0:
                    elapsed_ms = (time.perf_counter() - step_start) * 1000
                    remaining_ms = self.min_step_wall_time_ms - elapsed_ms
                    if remaining_ms > 0:
                        await asyncio.sleep(remaining_ms / 1000.0)

                step += 1

        # One event loop for the whole run: the feature-branch scheduler binds
        # asyncio.Event to the loop on first step(); asyncio.run() per step
        # creates a new loop and triggers "bound to a different event loop".
        try:
            asyncio.run(_simulation_loop())
        except KeyboardInterrupt:
            publish_cancelled("KeyboardInterrupt")
            raise
        except Exception as exc:
            # A mid-run crash (e.g. a sustained step-timeout abort) must still leave a
            # terminal run_status, so dashboards/KPI sinks don't sit on RUNNING forever
            # and the stop is attributable. publish_cancelled is idempotent, so an outer
            # wrapper or signal handler publishing FAILED first is harmless.
            logger.exception("Simulation aborted run_id=%s", self.run_id)
            publish_cancelled(f"{type(exc).__name__}: {exc}", status="FAILED")
            raise

        total = time.time() - self.execution_start_time
        logger.info("Simulation complete run_id=%s", self.run_id)
        self.on_simulation_complete(total)

    def update_status(self, status, execution_time=0, step_metric=None) -> tuple:
        """PATCH ``run_config.status``; returns ``(record | None, StatusWrite)`` (R3-4/R3-9).

        The record is non-None exactly when the outcome succeeded. Callers must branch on the
        outcome — there is deliberately no ``None``-collapsing return left to swallow, because
        a swallowed *state write* leaves stored state contradicting reality (see
        ``StatusWrite``). Only a 412 (stale etag) is retried, bounded and jittered; every
        other failure returns immediately, loudly and classified.
        """
        run_config_item_url = (
            f"{settings['OPENRIDE_SERVER_URL']}/run-config/{self.run_record['_id']}"
        )
        data = {
            "status": status,
            "execution_time": execution_time,
        }
        if step_metric is not None:
            for k, v in step_metric.items():
                data[f"step_metrics.{k}"] = v
                break
        timeout = settings.get("NETWORK_REQUEST_TIMEOUT", 10)

        def _patch(etag):
            return get_http_session().patch(
                run_config_item_url,
                headers=self.user.get_headers(etag=etag),
                data=json.dumps(data),
                timeout=timeout,
            )

        try:
            etag = self.run_record["_etag"]
            for attempt in range(1, STATUS_PATCH_MAX_ATTEMPTS + 1):
                response = _patch(etag)
                if response.status_code in (200, 201):
                    return (
                        response.json(),
                        StatusWrite.OK if attempt == 1 else StatusWrite.RETRIED_OK,
                    )
                if response.status_code != 412:
                    # NOT a concurrency problem — a 500/401/404/… is a genuine failure of
                    # the write. Never retried (retrying a 401 or a 500 just repeats it) and
                    # always loud: the caller counts it and surfaces it.
                    logging.error(
                        "Failed to update status run_id=%s status=%s http=%s attempt=%s/%s: %s, %s",
                        self.run_id,
                        status,
                        response.status_code,
                        attempt,
                        STATUS_PATCH_MAX_ATTEMPTS,
                        response.url,
                        response.text,
                    )
                    return None, StatusWrite.SERVER_ERROR

                # 412 == STALE ETAG, not a real failure. The run record has other legitimate
                # writers — the pooled assignment agent stamps its market provenance onto
                # ``meta.market`` from inside celery — and Eve's optimistic concurrency
                # rejects our cached etag the moment anyone else writes. Before any retry
                # existed, ONE concurrent write poisoned every subsequent update_status, and
                # the final call returning None set ``self.run_record = None``, so the next
                # ``self.run_record["_id"]`` raised TypeError and ABORTED A HEALTHY RUN.
                if attempt == STATUS_PATCH_MAX_ATTEMPTS:
                    break
                # Backoff BEFORE the re-GET: the GET→PATCH window is a full round trip, so a
                # retry that fires immediately tends to lose to the same competing writer.
                time.sleep(
                    _backoff_delay_s(
                        attempt, STATUS_PATCH_BACKOFF_BASE_S, STATUS_PATCH_BACKOFF_CAP_S
                    )
                )
                refreshed = get_http_session().get(
                    run_config_item_url,
                    headers=self.user.get_headers(),
                    timeout=timeout,
                )
                if refreshed.status_code != 200:
                    logging.error(
                        "Failed to refresh etag for status update run_id=%s status=%s http=%s: %s",
                        self.run_id,
                        status,
                        refreshed.status_code,
                        refreshed.text,
                    )
                    return None, StatusWrite.SERVER_ERROR
                etag = refreshed.json()["_etag"]

            logging.error(
                "Status update lost the etag race run_id=%s status=%s after %s attempts; "
                "run_config was NOT updated.",
                self.run_id,
                status,
                STATUS_PATCH_MAX_ATTEMPTS,
            )
            return None, StatusWrite.STALE_ETAG_EXHAUSTED
        except Exception as e:
            logging.error(
                "Exception in update_status run_id=%s status=%s: %s", self.run_id, status, e
            )
            return None, StatusWrite.EXCEPTION
