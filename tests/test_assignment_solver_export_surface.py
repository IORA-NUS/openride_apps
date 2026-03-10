from apps.assignment_app import solver as legacy_solver
from apps.ride_hail.assignment import solver as canonical_solver


SOLVER_EXPORTS = [
    "RandomAssignment",
    "CompromiseMatching",
    "GreedyMinPickupMatching",
    "GreedyMaxRevenueMatching",
    "GreedyMaxServiceScoreMatching",
    "PickupOptimalMatching",
    "RevenueOptimalMatching",
    "ServiceOptimalMatching",
    "CompromiseServiceBiasMatching",
    "CompromiseScaledMatching",
]


def test_assignment_solver_legacy_and_canonical_export_same_symbol_set():
    legacy_names = {name for name in SOLVER_EXPORTS if hasattr(legacy_solver, name)}
    canonical_names = {name for name in SOLVER_EXPORTS if hasattr(canonical_solver, name)}
    assert legacy_names == canonical_names == set(SOLVER_EXPORTS)


def test_assignment_solver_legacy_exports_match_canonical_class_objects():
    for name in SOLVER_EXPORTS:
        assert getattr(legacy_solver, name) is getattr(canonical_solver, name)
