import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import requests

from apps.container_logistics.analytics.app import AnalyticsApp
from apps.container_logistics.analytics.manager import AnalyticsManager, _sim_clock_http_param
from apps.container_logistics.statemachine import HaulTripStateMachine, OrderStateMachine


class SimClockParamTests(unittest.TestCase):
    def test_rfc1123_format(self):
        dt = datetime(2020, 1, 1, 4, 16, 0)
        self.assertEqual(
            _sim_clock_http_param(dt),
            "Wed, 01 Jan 2020 04:16:00 GMT",
        )


class AnalyticsManagerCountTests(unittest.TestCase):
    def setUp(self):
        self.user = MagicMock()
        self.user.get_headers.return_value = {"Authorization": "Bearer test"}
        self.manager = AnalyticsManager("run_test", "20200101040000", self.user, None)

    def test_count_orders_by_state(self):
        with patch.object(self.manager, "_get", return_value={"_items": [{"num_items": 42}]}) as mock_get:
            count = self.manager.count_orders_by_state("completed")
        self.assertEqual(count, 42)
        self.assertTrue(self.manager._aggregate_counts_enabled)
        url, = mock_get.call_args[0]
        self.assertTrue(url.endswith("/order/count_by_state"))
        aggregate = mock_get.call_args[0][1] if len(mock_get.call_args[0]) > 1 else mock_get.call_args.kwargs.get("params", {}).get("aggregate")
        if aggregate is None:
            aggregate = mock_get.call_args.kwargs["params"]["aggregate"]
        self.assertIn("run_test", aggregate)
        self.assertIn("completed", aggregate)

    def test_count_orders_by_state_falls_back_when_aggregate_missing(self):
        with patch.object(self.manager, "_get", side_effect=requests.HTTPError("404")):
            with patch.object(self.manager, "_legacy_count_orders_by_state", return_value=99) as legacy:
                count = self.manager.count_orders_by_state("completed")
        self.assertEqual(count, 99)
        self.assertFalse(self.manager._aggregate_counts_enabled)
        legacy.assert_called_once_with("completed")

    def test_count_active_orders(self):
        with patch.object(self.manager, "_get", return_value={"_items": [{"num_items": 7}]}) as mock_get:
            count = self.manager.count_active_orders()
        self.assertEqual(count, 7)
        url, = mock_get.call_args[0]
        self.assertTrue(url.endswith("/order/count_active"))

    def test_count_orders_in_window(self):
        start = datetime(2020, 1, 1, 4, 0, 0)
        end = datetime(2020, 1, 1, 5, 0, 0)
        with patch.object(self.manager, "_get", return_value={"_items": [{"num_items": 3}]}) as mock_get:
            count = self.manager.count_orders_in_window("completed", start, end)
        self.assertEqual(count, 3)
        aggregate = mock_get.call_args[1]["params"]["aggregate"]
        self.assertIn("sim_clock_gte", aggregate)
        self.assertIn("sim_clock_lt", aggregate)

    def test_count_haul_trips_by_state(self):
        with patch.object(self.manager, "_get", return_value={"_items": [{"num_items": 11}]}) as mock_get:
            count = self.manager.count_haul_trips_by_state("completed")
        self.assertEqual(count, 11)
        url, = mock_get.call_args[0]
        self.assertTrue(url.endswith("/truck/trip/count_by_state"))

    def test_active_haul_truck_count_from_rows(self):
        rows = [{"truck": "a"}, {"truck": "b"}, {"truck": "a"}]
        self.assertEqual(AnalyticsManager.active_haul_truck_count_from_rows(rows), 2)

    def test_fetch_orders_in_window_builds_sim_clock_filter(self):
        start = datetime(2020, 1, 1, 4, 0, 0)
        end = datetime(2020, 1, 1, 5, 0, 0)
        with patch.object(self.manager, "_get", return_value={"_items": []}) as mock_get:
            self.manager.fetch_orders_in_window(
                start,
                end,
                states=[OrderStateMachine.completed.name],
            )
        params = mock_get.call_args[0][1]
        where = params["where"]
        self.assertIn("sim_clock", where)
        self.assertIn("completed", where)


class AnalyticsAppComputeTests(unittest.TestCase):
    def test_compute_all_metrics_uses_counts_without_haul_fetch(self):
        app = AnalyticsApp.__new__(AnalyticsApp)
        app.kpi_collection = {
            "num_hauls_completed": 0,
            "num_orders_completed": 0,
            "active_haul_trips": 0,
            "active_orders": 0,
        }
        app.manager = MagicMock()
        app.manager.count_haul_trips_in_window.return_value = 3
        app.manager.count_orders_in_window.return_value = 5
        app.manager.count_active_haul_trucks.return_value = 2
        app.manager.count_active_orders.return_value = 5

        start = datetime(2020, 1, 1, 4, 0, 0)
        end = datetime(2020, 1, 1, 5, 4, 0)
        with patch("apps.container_logistics.analytics.app.time_to_str", return_value="clock"):
            app.compute_all_metrics(start, end)

        app.manager.set_metric_window.assert_called_once_with(start, end)
        app.manager.get_active_haul_trips.assert_not_called()
        app.manager.count_haul_trips_in_window.assert_called_once_with(
            HaulTripStateMachine.completed.name,
            start,
            end,
        )
        app.manager.count_orders_in_window.assert_called_once_with(
            OrderStateMachine.completed.name,
            start,
            end,
        )
        app.manager.count_active_haul_trucks.assert_called_once()
        app.manager.count_active_orders.assert_called_once()
        self.assertEqual(app.kpi_collection["num_hauls_completed"], 3)
        self.assertEqual(app.kpi_collection["active_haul_trips"], 2)
        app.manager.save_kpi.assert_called_once()


class AnalyticsAppOrderOutcomeTests(unittest.TestCase):
    NEW_KEYS = (
        "num_orders_released_total",
        "num_orders_failed_total",
        "num_orders_done_same_day_total",
        "num_orders_undone_at_day_end_total",
        "same_day_completion_rate",
    )

    def _app(self):
        app = AnalyticsApp.__new__(AnalyticsApp)
        with patch("orsim.lifecycle.ORSimApp.__init__", lambda self, **kw: None):
            AnalyticsApp.__init__(app, run_id="r", sim_clock="c", behavior={}, messenger=None)
        app.manager = MagicMock()
        m = app.manager
        m.count_haul_trips_in_window.return_value = 3
        m.count_orders_in_window.return_value = 5
        m.count_haul_trips_by_state.return_value = 30
        m.count_orders_by_state.side_effect = lambda s: {"completed": 40, "cancelled": 7}[s]
        m.count_active_haul_trucks.return_value = 2
        m.count_active_orders.return_value = 9
        m.fetch_completed_trips_window.return_value = []
        for name in ("compute_avg_empty_distance_km", "empty_distance_ratio", "compute_orders_per_truck",
                     "orders_per_truck_day", "avg_truck_active_hours", "dual_cycle_rate",
                     "shared_trip_rate", "total_benefit_km", "compute_avg_queue_wait_seconds",
                     "compute_avg_idle_time_seconds"):
            getattr(m, name).return_value = 0.5
        m.compute_peak_queue_length.return_value = 4
        m.order_outcome_scalars.return_value = {
            "num_orders_done_same_day_total": 11,
            "num_orders_undone_at_day_end_total": 13,
            "same_day_completion_rate": 0.4583,
        }
        return app

    def _run(self, app):
        start = datetime(2020, 1, 1, 8, 0, 0)
        end = datetime(2020, 1, 2, 8, 48, 0)
        with patch("apps.container_logistics.analytics.app.time_to_str", return_value="clock"):
            app.compute_all_metrics(start, end)
        return end

    def test_new_keys_are_last_and_computed(self):
        app = self._app()
        self.assertEqual(tuple(app.kpi_collection)[-5:], self.NEW_KEYS)
        end = self._run(app)
        kc = app.kpi_collection
        self.assertEqual(kc["num_orders_failed_total"], 7)
        self.assertEqual(kc["num_orders_released_total"], 40 + 7 + 9)
        self.assertEqual(kc["num_orders_done_same_day_total"], 11)
        self.assertEqual(kc["same_day_completion_rate"], 0.4583)
        app.manager.refresh_order_outcomes_if_day_closed.assert_called_once_with(end)
        app.manager.save_kpi.assert_called_once()
        # the existing calls are unchanged; the only new REST count is cancelled
        self.assertEqual(
            [c.args for c in app.manager.count_orders_by_state.call_args_list],
            [("completed",), ("cancelled",)],
        )
        app.manager.count_orders_in_window.assert_called_once()
        app.manager.count_active_orders.assert_called_once()

    def test_failure_in_new_block_still_saves_existing_kpis(self):
        app = self._app()
        app.manager.count_orders_by_state.side_effect = (
            lambda s: 40 if s == "completed" else (_ for _ in ()).throw(requests.ConnectionError("down"))
        )
        with self.assertLogs(level="ERROR"):
            self._run(app)
        app.manager.save_kpi.assert_called_once()
        saved = app.manager.save_kpi.call_args.args[1]
        self.assertEqual(saved["num_orders_completed_total"], 40)
        self.assertEqual(saved["active_orders"], 9)
        self.assertEqual(saved["num_orders_failed_total"], 0)  # keeps its last value

    def test_refresh_failure_still_saves(self):
        app = self._app()
        app.manager.refresh_order_outcomes_if_day_closed.side_effect = RuntimeError("mongo")
        with self.assertLogs(level="ERROR"):
            self._run(app)
        app.manager.save_kpi.assert_called_once()


if __name__ == "__main__":
    unittest.main()
