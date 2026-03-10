class RideHailAssignmentAdapter:
    """Thin compatibility adapter for assignment role wiring."""

    @staticmethod
    def get_app_class():
        from apps.ride_hail.assignment.app import AssignmentApp

        return AssignmentApp

    @staticmethod
    def get_agent_class():
        from apps.ride_hail.assignment.agent import AssignmentAgentIndie

        return AssignmentAgentIndie
