"""Isolated, data-driven container-logistics data generation.

Pure boundary: a :class:`GenerationSpec` in, agent behavior collections (and,
optionally, the persisted JSON files) out. Imports nothing from the scenario /
runtime layers, reads no module globals, and holds **no** hardcoded location
codes — the active codes are discovered from the address book and the trip matrix.

Sample, then generate:
    catalog (real on-land addresses, codes discovered from data)
      + CodeRegistry (labels / prefixes / demand-proportional facility shares)
      -> builders (truck/order/facility/assignment/analytics behavior dicts)
        -> ScenarioGenerator (collections / 6 JSON files)
"""

from .catalog import LocationCatalog, SingaporeMask, default_locations_csv, default_sg_mask_path
from .codes import CodeRegistry, LocationType, derive_label, trip_matrix_marginals
from .generator import DEFAULT_BUILDERS, GenerationResult, ScenarioGenerator
from .overrides import load_scenario_overrides, override_sha256
from .spec import GenerationSpec
from .trip_matrix import (
    location_type_for_code,
    parse_trip_matrix,
    restrict_trip_matrix,
    sample_pickup_delivery_codes,
)

__all__ = [
    "CodeRegistry",
    "DEFAULT_BUILDERS",
    "GenerationResult",
    "GenerationSpec",
    "LocationCatalog",
    "LocationType",
    "ScenarioGenerator",
    "SingaporeMask",
    "load_scenario_overrides",
    "override_sha256",
    "default_locations_csv",
    "default_sg_mask_path",
    "derive_label",
    "location_type_for_code",
    "parse_trip_matrix",
    "restrict_trip_matrix",
    "sample_pickup_delivery_codes",
    "trip_matrix_marginals",
]
