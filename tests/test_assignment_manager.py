import unittest
from unittest.mock import MagicMock, patch

from apps.container_logistics.assignment.manager import AssignmentManager
from apps.container_logistics.statemachine import OrderStateMachine


class AssignmentManagerAggregateTests(unittest.TestCase):
    def setUp(self):
        self.user = MagicMock()
        self.user.get_headers.return_value = {"Authorization": "Bearer test"}
        self.manager = AssignmentManager(
            "run_test",
            "20200101040000",
            self.user,
            profile={"max_orders_per_tick": 250},
        )

    def test_list_unassigned_orders_uses_batch_aggregate(self):
        items = [{"_id": "o1", "state": "unassigned"}]
        with patch.object(self.manager, "_try_aggregate_items", return_value=items) as mock_agg:
            result = self.manager.list_unassigned_orders()
        self.assertEqual(result, items)
        mock_agg.assert_called_once()
        url, aggregate = mock_agg.call_args[0]
        self.assertTrue(url.endswith("/order/unassigned_batch"))
        self.assertEqual(aggregate["$run_id"], "run_test")
        self.assertEqual(aggregate["$state"], OrderStateMachine.unassigned.name)
        self.assertEqual(aggregate["$limit"], 250)

    def test_list_unassigned_orders_falls_back_to_pagination(self):
        with patch.object(self.manager, "_try_aggregate_items", return_value=None):
            with patch.object(self.manager, "_paged_where", return_value=[{"_id": "o2"}]) as paged:
                result = self.manager.list_unassigned_orders()
        self.assertEqual(result, [{"_id": "o2"}])
        paged.assert_called_once()
        self.assertEqual(paged.call_args.kwargs.get("max_results"), 250)

    def test_order_ids_with_open_haul_from_aggregate(self):
        with patch.object(
            self.manager,
            "_try_aggregate_items",
            return_value=[{"order": "a"}, {"order": "b"}],
        ):
            out = self.manager.order_ids_with_open_haul()
        self.assertEqual(out, {"a", "b"})

    def test_active_haul_truck_ids_from_aggregate(self):
        with patch.object(
            self.manager,
            "_try_aggregate_items",
            return_value=[{"truck": "t1"}, {"truck": "t2"}],
        ):
            out = self.manager.active_haul_truck_ids()
        self.assertEqual(out, ["t1", "t2"])

    def test_aggregate_failure_disables_fast_path(self):
        with patch.object(self.manager, "_get", side_effect=RuntimeError("404")):
            first = self.manager._try_aggregate_items("http://x/open_order_ids", {"$run_id": "run_test"})
        self.assertIsNone(first)
        self.assertFalse(self.manager._aggregates_enabled)
        with patch.object(self.manager, "_get") as mock_get:
            second = self.manager._try_aggregate_items("http://x/open_order_ids", {"$run_id": "run_test"})
        self.assertIsNone(second)
        mock_get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
