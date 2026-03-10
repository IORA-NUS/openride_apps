class RideHailAssignmentAdapter:
    """Thin compatibility adapter for assignment role wiring."""

    @staticmethod
    def get_app_class():
        from apps.ride_hail.assignment import AssignmentApp

        return AssignmentApp

    @staticmethod
    def get_agent_class():
        from apps.ride_hail.assignment import AssignmentAgentIndie

        return AssignmentAgentIndie
