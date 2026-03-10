from apps.assignment_app.assignment_agent_indie import (
    AssignmentAgentIndie as LegacyAssignmentAgentIndie,
)
from apps.assignment_app.assignment_app import AssignmentApp as LegacyAssignmentApp
from apps.ride_hail.assignment.agent import AssignmentAgentIndie
from apps.ride_hail.assignment.app import AssignmentApp


def test_assignment_app_and_agent_shims_export_canonical_classes():
    assert LegacyAssignmentApp is AssignmentApp
    assert LegacyAssignmentAgentIndie is AssignmentAgentIndie


def test_assignment_app_and_agent_shims_resolve_to_canonical_modules():
    assert AssignmentApp.__module__ == "apps.ride_hail.assignment.app_impl"
    assert AssignmentAgentIndie.__module__ == "apps.ride_hail.assignment.agent_impl"
