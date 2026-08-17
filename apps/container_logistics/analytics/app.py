import logging

from apps.common.user_registry import UserRegistry
from apps.container_logistics.statemachine import HaulTripStateMachine, OrderStateMachine
from apps.utils import time_to_str
from orsim.lifecycle import ORSimApp

from .manager import AnalyticsManager


class AnalyticsApp(ORSimApp):
    def __init__(self, run_id, sim_clock, behavior, messenger):
        super().__init__(
            run_id=run_id,
            sim_clock=sim_clock,
            behavior=behavior,
            messenger=messenger,
        )
        self.kpi_collection = {
            "num_hauls_completed": 0,
            "num_orders_completed": 0,
            # Cumulative, monotonic counters (idempotent under at-least-once Kafka redelivery)
            # — the TSDB-native shape for VictoriaMetrics. The per-window deltas above stay for
            # the DuckDB/SSE path; these totals are what the VM sink ingests so a sink restart /
            # rebalance can't double-count (a re-imported sample just overwrites the same total).
            "num_hauls_completed_total": 0,
            "num_orders_completed_total": 0,
            "active_haul_trips": 0,
            "active_orders": 0,
            "avg_empty_distance_km": 0.0,
            "empty_distance_ratio": 0.0,
            "orders_per_truck": 0.0,
            "orders_per_truck_day": 0.0,
            "avg_truck_active_hours": 0.0,
            "dual_cycle_rate": 0.0,
            "avg_queue_wait_seconds": 0.0,
            "peak_queue_length": 0,
            "avg_idle_time_seconds": 0.0,
            # Collaboration (haulier job sharing) scalars.
            "shared_trip_rate": 0.0,
            "total_benefit_km": 0.0,
        }
        self._active_haul_trips: list = []

    @property
    def active_haul_trips(self):
        """Active haul-trip rows fetched once per analytics tick (map + KPI gauge)."""
        return self._active_haul_trips

    @property
    def managed_statemachine(self):
        return None

    @property
    def interaction_ground_truth_list(self):
        return []

    @property
    def runtime_behavior_schema(self):
        return {
            "steps_per_action": {"type": "integer", "required": False},
            "response_rate": {"type": "number", "required": False},
            "step_only_on_events": {"type": "boolean", "required": False},
            "profile": {"type": "dict", "required": False, "allow_unknown": True},
        }

    def _create_user(self):
        return UserRegistry(self.sim_clock, self.credentials, role="admin")

    def _create_manager(self):
        return AnalyticsManager(self.run_id, self.sim_clock, self.user, self.behavior.get("persona"))

    def launch(self):
        pass

    def handle_app_topic_messages(self, payload):
        pass

    def prep_metric_computation_queries(self, start_time, end_time):
        """Bind the analytics interval for window-scoped KPI queries."""
        self.manager.set_metric_window(start_time, end_time)

    def compute_all_metrics(self, start_time, end_time):
        try:
            self.prep_metric_computation_queries(start_time, end_time)

            # Per-interval deltas in [start_time, end_time) — frontend sums these into
            # run cumulative totals (see isGaugeMetric / buildCumulativeSeries).
            self.kpi_collection["num_hauls_completed"] = self.manager.count_haul_trips_in_window(
                HaulTripStateMachine.completed.name,
                start_time,
                end_time,
            )
            self.kpi_collection["num_orders_completed"] = self.manager.count_orders_in_window(
                OrderStateMachine.completed.name,
                start_time,
                end_time,
            )

            # Cumulative run totals (reuse the existing fast aggregate-count endpoints). Emitted as
            # monotonic counters so the VM sink is idempotent — see kpi_collection note above.
            self.kpi_collection["num_hauls_completed_total"] = self.manager.count_haul_trips_by_state(
                HaulTripStateMachine.completed.name
            )
            self.kpi_collection["num_orders_completed_total"] = self.manager.count_orders_by_state(
                OrderStateMachine.completed.name
            )

            # Snapshot gauges — count endpoints only (no active-haul document fetch).
            self.kpi_collection["active_haul_trips"] = self.manager.count_active_haul_trucks()
            self.kpi_collection["active_orders"] = self.manager.count_active_orders()

            # One windowed fetch of completed trips feeds both the per-trip average and the
            # cumulative per-entity accumulators (distribution KPIs + breakdown artifact).
            window_trips = self.manager.fetch_completed_trips_window(start_time, end_time)
            self.manager.accumulate_completed_trips(window_trips, end_time)

            self.kpi_collection["avg_empty_distance_km"] = self.manager.compute_avg_empty_distance_km(
                start_time, end_time, trips=window_trips
            )
            self.kpi_collection["empty_distance_ratio"] = self.manager.empty_distance_ratio()
            self.kpi_collection["orders_per_truck"] = self.manager.compute_orders_per_truck()
            self.kpi_collection["orders_per_truck_day"] = self.manager.orders_per_truck_day(end_time)
            self.kpi_collection["avg_truck_active_hours"] = self.manager.avg_truck_active_hours()
            self.kpi_collection["dual_cycle_rate"] = self.manager.dual_cycle_rate()
            self.kpi_collection["shared_trip_rate"] = self.manager.shared_trip_rate()
            self.kpi_collection["total_benefit_km"] = self.manager.total_benefit_km()
            self.kpi_collection["avg_queue_wait_seconds"] = self.manager.compute_avg_queue_wait_seconds()
            self.kpi_collection["peak_queue_length"] = self.manager.compute_peak_queue_length()
            # Fold this window's newly-ended idle trips into the cumulative accumulator, then read
            # the run-wide average. A per-window snapshot collapsed to 0 in any window with no
            # idle-trip ends (e.g. the final window) — see accumulate_idle_trips / compute_*.
            self.manager.accumulate_idle_trips(start_time, end_time)
            self.kpi_collection["avg_idle_time_seconds"] = self.manager.compute_avg_idle_time_seconds()

            for kpi_name, kpi_value in self.kpi_collection.items():
                if kpi_value is None:
                    logging.warning("KPI %s is None at time %s", kpi_name, time_to_str(end_time))

            self.manager.save_kpi(time_to_str(end_time), self.kpi_collection)
        except Exception:
            logging.exception("compute_all_metrics failed")
            raise
