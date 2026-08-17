"""Backward-compatible facade over the isolated ``datagen`` package.

The real generation logic now lives in ``apps.container_logistics.datagen``
(OOP, pure, isolated). This thin facade preserves the historical
``GenerateBehavior.container_*`` classmethod API used by the standalone smoke
runners and ``ScenarioManager._expected_behavior_counts``. It resolves a
:class:`GenerationSpec` from the current ``scenario_config`` on each call and
delegates to the corresponding builder.
"""

from __future__ import annotations

from typing import Optional

from apps.container_logistics.datagen import LocationCatalog, default_locations_csv
from apps.container_logistics.datagen.builders import (
    AnalyticsBuilder,
    AssignmentBuilder,
    FacilityBuilder,
    OrderBuilder,
    TruckBuilder,
    resolve_facilities,
)
from apps.orsim_config import orsim_settings

from . import scenario_config

_CATALOG = None


def _catalog() -> LocationCatalog:
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = LocationCatalog(default_locations_csv())
    return _CATALOG


def _spec():
    # Built per call so frontend/smoke overrides of scenario_config are honored.
    from .scenario_datagen import build_generation_spec

    return build_generation_spec(orsim_settings.get("DOMAIN"))


class GenerateBehavior:
    """Behavior factory facade for container logistics actors."""

    @classmethod
    def _get_facilities(cls):
        """Resolved facility site list (used by scenario count expectations)."""
        return resolve_facilities(scenario_config.facility_settings)

    @classmethod
    def container_truck(cls, agent_id, record=None, haulier: Optional[dict] = None):
        return TruckBuilder(_spec(), _catalog()).build(agent_id, haulier=haulier)

    @classmethod
    def container_order(cls, agent_id, record=None, haulier: Optional[dict] = None):
        return OrderBuilder(_spec(), _catalog()).build(agent_id, haulier=haulier)

    @classmethod
    def container_facility(cls, agent_id, facility_index=0, record=None):
        return FacilityBuilder(_spec(), _catalog()).build(agent_id, facility_index=facility_index)

    @classmethod
    def container_assignment(cls, agent_id, record=None):
        return AssignmentBuilder(_spec(), _catalog()).build(agent_id)

    @classmethod
    def container_analytics(cls, agent_id, record=None):
        return AnalyticsBuilder(_spec(), _catalog()).build(agent_id)
