from apps.assignment_app import EngineManager as LegacyEngineManagerFromPackage
from apps.assignment_app.engine_manager import EngineManager as LegacyEngineManager
from apps.ride_hail.assignment.manager_impl import (
    AssignmentManager,
    EngineManager as CanonicalEngineManager,
)


def test_canonical_engine_manager_alias_points_to_assignment_manager():
    assert CanonicalEngineManager is AssignmentManager


def test_legacy_engine_manager_aliases_match_canonical_alias():
    assert LegacyEngineManager is CanonicalEngineManager
    assert LegacyEngineManagerFromPackage is CanonicalEngineManager
