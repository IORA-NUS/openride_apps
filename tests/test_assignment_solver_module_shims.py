import importlib

import pytest


SOLVER_MODULE_CLASS_MAPPINGS = [
    ("random_assignment", "RandomAssignment"),
    ("compromise_matching", "CompromiseMatching"),
    ("greedy_min_pickup_matching", "GreedyMinPickupMatching"),
    ("greedy_max_revenue_matching", "GreedyMaxRevenueMatching"),
    ("greedy_max_service_score_matching", "GreedyMaxServiceScoreMatching"),
    ("pickup_optimal_matching", "PickupOptimalMatching"),
    ("revenue_optimal_matching", "RevenueOptimalMatching"),
    ("service_optimal_matching", "ServiceOptimalMatching"),
    ("compromise_servicebias_matching", "CompromiseServiceBiasMatching"),
    ("compromise_scaled_matching", "CompromiseScaledMatching"),
    ("abstract_solver", "AbstractSolver"),
]


@pytest.mark.parametrize("module_name,class_name", SOLVER_MODULE_CLASS_MAPPINGS)
def test_assignment_solver_legacy_submodule_exports_match_canonical(
    module_name, class_name
):
    legacy_module = importlib.import_module(
        f"apps.assignment_app.solver.{module_name}"
    )
    canonical_module = importlib.import_module(
        f"apps.ride_hail.assignment.solver.{module_name}"
    )
    assert getattr(legacy_module, class_name) is getattr(canonical_module, class_name)
