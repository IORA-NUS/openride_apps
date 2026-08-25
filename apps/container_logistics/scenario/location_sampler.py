"""Backward-compatible shim.

Location sampling and the (data-driven) pickup/delivery trip matrix now live in
the isolated ``apps.container_logistics.datagen`` package. This module re-exports
the small public surface other code still imports (notably ``parse_trip_matrix``
and ``restrict_trip_matrix`` for the frontend scenario spec) and delegates point
sampling to ``LocationCatalog``.

Kept light at import time (no shapely import) — the catalog is imported lazily.
"""

from __future__ import annotations

from apps.container_logistics.datagen.trip_matrix import (  # noqa: F401
    parse_trip_matrix,
    restrict_trip_matrix,
    sample_pickup_delivery_codes,
)
from apps.container_logistics.datagen.trip_matrix import (
    location_type_for_code as _location_type_for_code,
)

_CATALOG = None


def _catalog():
    global _CATALOG
    if _CATALOG is None:
        from apps.container_logistics.datagen.catalog import (
            LocationCatalog,
            default_locations_csv,
            default_sg_mask_path,
        )

        from . import scenario_config

        _CATALOG = LocationCatalog(
            default_locations_csv(),
            mask_path=default_sg_mask_path(),
            excluded=getattr(scenario_config, "EXCLUDED_CODES", ()),
        )
    return _CATALOG


def location_type_for_code(code, metadata=None):
    """Location-type label for a code, using the config-layer metadata by default."""
    if metadata is None:
        from . import scenario_config

        metadata = getattr(scenario_config, "LOCATION_TYPE_METADATA", None)
    return _location_type_for_code(code, metadata)


def active_codes():
    """The location codes that have real addresses (excluding EXCLUDED_CODES)."""
    return _catalog().codes()


def sample_port_point(rng=None):
    """Return a real (lon, lat) for a port (CT) address."""
    return _catalog().sample("CT", rng=rng)


def sample_depot_point(rng=None):
    """Return a real (lon, lat) for a depot (MT) address."""
    return _catalog().sample("MT", rng=rng)


def sample_customer_point(rng=None):
    """Return a real (lon, lat) for a customer (CU) address."""
    return _catalog().sample("CU", rng=rng)
