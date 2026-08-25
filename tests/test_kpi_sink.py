import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from apps.kpi_sink.duckdb_store import DuckDbKpiStore
from apps.kpi_sink.kpi_parse import coerce_metric_value, parse_sim_clock
from apps.kpi_sink.mongo_export import rows_to_mongo_docs
from apps.kpi_sink.run_status import is_simulation_lifecycle_message, parse_run_terminal_outcome
from apps.utils.kpi_save import save_kpi_batch


class KpiParseTests(unittest.TestCase):
    def test_parse_sim_clock_rfc2822(self):
        parsed = parse_sim_clock("Mon, 01 Jan 2020 00:16:00 GMT")
        self.assertEqual(parsed.year, 2020)
        self.assertEqual(parsed.month, 1)
        self.assertEqual(parsed.day, 1)

    def test_coerce_metric_value(self):
        self.assertEqual(coerce_metric_value(None), 0.0)
        self.assertEqual(coerce_metric_value(3), 3.0)


class RunStatusTests(unittest.TestCase):
    def test_parse_terminal_outcome(self):
        self.assertEqual(parse_run_terminal_outcome({"status": "COMPLETED"}), "completed")
        self.assertEqual(parse_run_terminal_outcome({"status": "FAILED"}), "failed")
        self.assertEqual(parse_run_terminal_outcome({"status": "CANCELLED"}), "cancelled")
        self.assertIsNone(parse_run_terminal_outcome({"status": "RUNNING"}))

    def test_should_export_on_stop_statuses(self):
        from apps.kpi_sink.run_status import should_export_run_status

        self.assertEqual(should_export_run_status({"status": "COMPLETED"}), "completed")
        self.assertEqual(should_export_run_status({"status": "STOPPED"}), "stopped")
        self.assertEqual(should_export_run_status({"status": "STOP"}), "stopped")
        self.assertEqual(should_export_run_status({"status": "ABORTED"}), "aborted")
        self.assertEqual(
            should_export_run_status({"status": "KILLED", "simulation_active": False}),
            "terminated",
        )
        self.assertIsNone(should_export_run_status({"status": "RUNNING", "simulation_active": True}))

    def test_lifecycle_scope_filter(self):
        self.assertTrue(is_simulation_lifecycle_message({"lifecycle_scope": "simulation"}))
        self.assertFalse(is_simulation_lifecycle_message({"lifecycle_scope": "trip"}))


class DuckDbKpiStoreTests(unittest.TestCase):
    def test_insert_and_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DuckDbKpiStore(tmp, batch_max_rows=100)
            sim_clock = datetime(2020, 1, 1, 0, 16, 0)
            store.enqueue("run-a", "num_served", 1.0, sim_clock)
            store.enqueue("run-a", "num_served", 2.0, sim_clock)
            store.flush("run-a")
            rows = store.fetch_all("run-a")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["value"], 2.0)
            store.close()

    def test_idempotent_upsert(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DuckDbKpiStore(tmp, batch_max_rows=1)
            sim_clock = datetime(2020, 1, 1, 0, 16, 0)
            store.enqueue("run-b", "active_orders", 5.0, sim_clock)
            store.enqueue("run-b", "active_orders", 7.0, sim_clock)
            self.assertEqual(store.row_count("run-b"), 1)
            store.close()


class MongoDocTests(unittest.TestCase):
    def test_rows_to_mongo_docs(self):
        sim_clock = datetime(2020, 1, 1, 0, 16, 0)
        docs = rows_to_mongo_docs(
            [{"run_id": "r1", "metric": "num_served", "value": 3.0, "sim_clock": sim_clock}]
        )
        self.assertEqual(docs[0]["run_id"], "r1")
        self.assertEqual(docs[0]["_created"], sim_clock)
        self.assertEqual(docs[0]["_updated"], sim_clock)


class SaveKpiBatchTests(unittest.TestCase):
    @patch("apps.utils.kpi_save.flush_producer")
    @patch("apps.utils.kpi_save.push_kpi_to_topic")
    def test_skips_mongo_when_flag_disabled(self, mock_push, mock_flush):
        catalog = MagicMock()
        catalog.is_allowed.return_value = True
        mongo_post = MagicMock()

        with patch.dict("apps.config.settings", {"KPI_WRITE_MONGO_DURING_RUN": False}, clear=False):
            save_kpi_batch(
                "run-x",
                "Mon, 01 Jan 2020 00:16:00 GMT",
                {"num_served": 1},
                catalog,
                ecosystem_label="ridehail",
                mongo_post=mongo_post,
            )

        mock_push.assert_called_once()
        mongo_post.assert_not_called()

    @patch("apps.utils.kpi_save.flush_producer")
    @patch("apps.utils.kpi_save.push_kpi_to_topic")
    def test_posts_mongo_when_flag_enabled(self, mock_push, mock_flush):
        catalog = MagicMock()
        catalog.is_allowed.return_value = True
        mongo_post = MagicMock()

        with patch.dict("apps.config.settings", {"KPI_WRITE_MONGO_DURING_RUN": True}, clear=False):
            save_kpi_batch(
                "run-y",
                "Mon, 01 Jan 2020 00:16:00 GMT",
                {"num_served": 2},
                catalog,
                ecosystem_label="ridehail",
                mongo_post=mongo_post,
            )

        mongo_post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
