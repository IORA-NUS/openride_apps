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


if __name__ == "__main__":
    unittest.main()
