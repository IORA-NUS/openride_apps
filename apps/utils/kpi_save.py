"""Publish KPI metrics to kpi_stream (optional legacy Mongo POST during run)."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from apps.config import settings
from apps.utils.kafka_utils import flush_producer, push_kpi_to_topic

logger = logging.getLogger(__name__)


def publish_kpi_batch(
    run_id: str,
    sim_clock: Any,
    kpi_collection: Dict[str, Any],
    kpi_catalog: Any,
    *,
    ecosystem_label: str,
) -> List[Dict[str, Any]]:
    """Validate catalog, publish each metric to Kafka. Returns rows that were emitted."""
    rows: List[Dict[str, Any]] = []
    for metric, value in kpi_collection.items():
        if settings.get("ENFORCE_KPI_DEFINITIONS", True) and not kpi_catalog.is_allowed(metric):
            logger.error(
                "Skipping KPI '%s' (not in catalog) for ecosystem=%s. "
                "Create a KPI definition first.",
                metric,
                ecosystem_label,
            )
            continue
        kafka_message = {
            "metric": metric,
            "value": value,
            "sim_clock": sim_clock,
        }
        push_kpi_to_topic(run_id, kafka_message)
        rows.append(
            {
                "run_id": run_id,
                "metric": metric,
                "value": float(value) if value is not None else 0.0,
                "sim_clock": sim_clock,
            }
        )
    flush_producer()
    return rows


def save_kpi_batch(
    run_id: str,
    sim_clock: Any,
    kpi_collection: Dict[str, Any],
    kpi_catalog: Any,
    *,
    ecosystem_label: str,
    mongo_post: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
) -> None:
    """Publish KPI rows to Kafka; optionally POST to Mongo when legacy flag is enabled."""
    rows = publish_kpi_batch(
        run_id,
        sim_clock,
        kpi_collection,
        kpi_catalog,
        ecosystem_label=ecosystem_label,
    )
    if not rows:
        return
    if settings.get("KPI_WRITE_MONGO_DURING_RUN", False) and mongo_post is not None:
        mongo_post(rows)
