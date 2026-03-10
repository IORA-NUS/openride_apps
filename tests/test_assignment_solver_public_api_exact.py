import apps.assignment_app.solver as legacy_solver_pkg
import apps.ride_hail.assignment.solver as canonical_solver_pkg


EXPECTED_SOLVER_CLASS_EXPORTS = {
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
}


def _public_class_exports(module_obj):
    return {
        name
        for name, value in module_obj.__dict__.items()
        if not name.startswith("_") and isinstance(value, type)
    }


def test_canonical_solver_public_class_exports_match_expected_exactly():
    assert _public_class_exports(canonical_solver_pkg) == EXPECTED_SOLVER_CLASS_EXPORTS


def test_legacy_solver_public_class_exports_match_expected_exactly():
    assert _public_class_exports(legacy_solver_pkg) == EXPECTED_SOLVER_CLASS_EXPORTS
