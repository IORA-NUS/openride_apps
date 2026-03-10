from apps.assignment_app.solver import (
    CompromiseMatching as LegacyCompromiseMatching,
    RandomAssignment as LegacyRandomAssignment,
)
from apps.ride_hail.assignment.solver import (
    CompromiseMatching,
    RandomAssignment,
)


def test_assignment_solver_shim_exports_match_canonical_classes():
    assert LegacyRandomAssignment is RandomAssignment
    assert LegacyCompromiseMatching is CompromiseMatching


def test_assignment_solver_shim_resolves_to_canonical_module_objects():
    assert RandomAssignment.__module__ == "apps.ride_hail.assignment.solver.random_assignment"
    assert CompromiseMatching.__module__ == "apps.ride_hail.assignment.solver.compromise_matching"
