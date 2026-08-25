import logging
from random import random

from dateutil.relativedelta import relativedelta

from orsim.lifecycle import ORSimAgent

from .app import AnalyticsApp


class AnalyticsAgentIndie(ORSimAgent):
    """Service-style agent: periodically aggregates haul/order KPIs for a run."""

    def _create_app(self):
        return AnalyticsApp(
            run_id=self.run_id,
            sim_clock=self.get_current_time_str(),
            behavior=self.behavior,
            messenger=self.messenger,
        )

    @property
    def process_payload_on_init(self):
        return False

    def entering_market(self, time_step):
        self.active = True

    def exiting_market(self):
        self.active = False

    def logout(self):
        self._join_trip_geo_publish()
        app = getattr(self, "app", None)
        if app is not None:
            app.close(self.get_current_time_str())

    def estimate_next_event_time(self):
        return self.current_time

    def compute_all_metrics(self):
        start_time = self.current_time - relativedelta(
            seconds=(self.behavior.get("steps_per_action", 1) * self.orsim_settings["STEP_INTERVAL"])
        )
        end_time = self.current_time
        self.app.compute_all_metrics(start_time, end_time)

    def step(self, time_step):
        if not (
            self.current_time_step % self.behavior.get("steps_per_action", 1) == 0
            and random() <= self.behavior.get("response_rate", 1.0)
            and self.next_event_time <= self.current_time
        ):
            return False
        try:
            # Cooperation structure ships via orsim_settings (like STREAM_GEO) —
            # hand it to the manager once so planner-scope breakdown rows exist.
            if not getattr(self, "_coop_forwarded", False):
                self.app.manager.set_cooperation(self.orsim_settings.get("COOPERATION"))
                self._coop_forwarded = True
            self.compute_all_metrics()
            profile = self.behavior.get("profile") or {}
            self._maybe_flush_breakdowns(profile)
            # PERSIST_ROUTE_GEO (default True, and deliberately ON even in headless) gates
            # the trip_geo publish. That stream is not just a live-map feed: the always-on
            # openride-trip-geo-sink consumes it into container_logistics_trip_geo, which is
            # the ONLY durable source of road-following route geometry for replay/compare.
            # This used to be gated on STREAM_GEO, so headless runs produced no geometry at
            # all. STREAM_GEO now means only "visual per-truck per-step truck_loc stream"
            # (see run_container_logistics_simulation.py's headless block).
            if self.orsim_settings.get("PERSIST_ROUTE_GEO", True) and profile.get(
                "publish_trip_geo_kafka", True
            ):
                geo_interval = max(
                    1,
                    int(
                        profile.get(
                            "trip_geo_steps_per_action",
                            self.behavior.get("steps_per_action", 1),
                        )
                    ),
                )
                if self.current_time_step % geo_interval == 0:
                    self._spawn_trip_geo_publish()
        except Exception as e:
            logging.exception(
                "Container logistics analytics failed for %s at step %s: %s",
                self.unique_id,
                time_step,
                e,
            )
        return True

    def _maybe_flush_breakdowns(self, profile):
        """Flush per-truck/haulier breakdown snapshots, throttled by elapsed sim-time (~1 sim hour).

        Throttle on sim-time, not a raw ``current_time_step`` modulo: a step-count gate can be
        co-prime with the analytics agent's ``steps_per_action`` (48) and so never align with an
        actual tick, which silently drops every live snapshot (only the run-finalize one survives).
        """
        try:
            min_gap_s = max(1.0, float(profile.get("breakdown_min_gap_seconds", 3600)))
            now = self.current_time
            last = getattr(self, "_last_breakdown_flush_time", None)
            if last is not None and (now - last).total_seconds() < min_gap_s:
                return
            self._last_breakdown_flush_time = now
            self.app.manager.save_breakdowns(self.get_current_time_str())
        except Exception as e:
            logging.warning("breakdown snapshot flush failed: %s", e)

    def _spawn_trip_geo_publish(self):
        """Run the trip-geo publish off the step barrier.

        The publish (paged haul-trip fetch + OSRM fills + Kafka emits) only reads
        Mongo state and pushes to the map stream — nothing behind the step barrier
        consumes its output, yet run synchronously it made the analytics tick the
        slowest service step of a dashboard run (~1.9s every 48 steps at
        1000-truck scale). The sim clock is captured at spawn so messages carry
        the tick they describe even if the run advances while the publish runs.
        """
        inflight = getattr(self, "_trip_geo_greenlet", None)
        if inflight is not None and not inflight.dead:
            # Previous publish still in flight — skip; the next tick re-reads
            # current haul state, so nothing is lost (dedup keys are per-leg).
            return
        sim_clock = self.get_current_time_str()
        try:
            import eventlet
        except ImportError:
            self._publish_trip_geo_kafka(sim_clock)
            return
        self._trip_geo_greenlet = eventlet.spawn(self._publish_trip_geo_guarded, sim_clock)

    def _publish_trip_geo_guarded(self, sim_clock):
        try:
            self._publish_trip_geo_kafka(sim_clock)
        except Exception:
            logging.exception("trip_geo publish failed for %s", self.unique_id)

    def _join_trip_geo_publish(self, timeout_seconds=5):
        """Bounded wait for an in-flight publish (run teardown only)."""
        inflight = getattr(self, "_trip_geo_greenlet", None)
        if inflight is None or inflight.dead:
            return
        try:
            import eventlet

            with eventlet.Timeout(timeout_seconds):
                inflight.wait()
        except Exception:
            pass

    def _publish_trip_geo_kafka(self, sim_clock=None):
        from apps.container_logistics.analytics.trip_geo_publisher import ContainerTripGeoPublisher

        if not hasattr(self, "_trip_geo_publisher"):
            self._trip_geo_publisher = ContainerTripGeoPublisher(
                self.run_id,
                # Stamped into orsim_settings by ScenarioManager so the run is
                # self-describing; ships to this agent at spawn.
                authoritative_routes=bool(
                    self.orsim_settings.get("USE_OSRM_AT_ASSIGNMENT", False)
                ),
            )
        if sim_clock is None:
            sim_clock = self.get_current_time_str()
        haul_trips = self.app.manager.get_active_haul_trips()
        self._trip_geo_publisher.publish_from_haul_trips(haul_trips, sim_clock)
