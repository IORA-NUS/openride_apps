from apps.assignment_app import AssignmentManager as LegacyAssignmentManagerFromPackage
from apps.assignment_app import EngineManager as LegacyEngineManagerFromPackage
from apps.assignment_app.engine_manager import (
    AssignmentManager as LegacyAssignmentManager,
)
from apps.assignment_app.engine_manager import EngineManager as LegacyEngineManager
from apps.ride_hail.assignment.manager import AssignmentManager


def test_assignment_manager_shim_exports_match_canonical_class():
    assert LegacyAssignmentManager is AssignmentManager
    assert LegacyEngineManager is AssignmentManager


def test_assignment_manager_package_lazy_exports_match_canonical_class():
    assert LegacyAssignmentManagerFromPackage is AssignmentManager
    assert LegacyEngineManagerFromPackage is AssignmentManager


def test_assignment_manager_shim_resolves_to_canonical_module_object():
    assert AssignmentManager.__module__ == "apps.ride_hail.assignment.manager_impl"
