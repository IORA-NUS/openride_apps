"""
Bulk-cancel any non-terminal orders for a run, in a single Mongo operation.

Past the simulation horizon all trucks go offline, so orders still left in a non-terminal
state (overwhelmingly ``unassigned`` — unserved demand) can never be served. Cancelling
them one-by-one via the Eve API does not scale: thousands of simultaneous PATCHes overwhelm
the backend and cannot drain within the post-horizon window (see CLAUDE.md §6.4). Instead,
order agents leave the market *locally* past the horizon (OrderAgent.exiting_market) and this
single ``update_many`` terminalizes their records at simulation completion — keeping the run's
final order data clean (0 non-terminal) and turning unserved demand into a visible
``cancelled`` count.

``cancel`` is a valid OrderStateMachine transition from every non-terminal state, so the bulk
update never violates the state machine.
"""
from __future__ import annotations

import logging
from typing import Optional

from pymongo import MongoClient

from apps.config import kpi_sink_settings
from apps.container_logistics.statemachine import OrderStateMachine

logger = logging.getLogger(__name__)

# Eve resource "order" in the container-logistics domain → this Mongo collection.
_ORDER_COLLECTION = "container_logistics_order"
_TERMINAL_STATES = (OrderStateMachine.completed.name, OrderStateMachine.cancelled.name)


def _mongo_client() -> MongoClient:
    uri = kpi_sink_settings.get("mongo_uri")
    if uri:
        return MongoClient(uri)
    return MongoClient(kpi_sink_settings["mongo_host"], int(kpi_sink_settings["mongo_port"]))


def bulk_cancel_nonterminal_orders(run_id: str, sim_clock: Optional[str] = None) -> int:
    """Cancel every non-terminal order for ``run_id`` in one update_many.

    Returns the number of orders transitioned to ``cancelled``.
    """
    client = _mongo_client()
    try:
        collection = client[kpi_sink_settings["mongo_db"]][_ORDER_COLLECTION]
        update = {"state": OrderStateMachine.cancelled.name}
        if sim_clock:
            update["_updated"] = sim_clock
        result = collection.update_many(
            {"run_id": run_id, "state": {"$nin": list(_TERMINAL_STATES)}},
            {"$set": update},
        )
        logger.info(
            "bulk_cancel_nonterminal_orders: cancelled %d unserved/stranded orders for run_id=%s",
            result.modified_count,
            run_id,
        )
        return result.modified_count
    finally:
        client.close()
