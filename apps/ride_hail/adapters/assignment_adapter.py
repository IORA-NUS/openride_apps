class RideHailAssignmentAdapter:
    """Thin compatibility adapter for assignment role wiring."""

    @staticmethod
    def get_app_class():
        from apps.assignment_app.assignment_app import AssignmentApp

        return AssignmentApp

    @staticmethod
    def get_agent_class():
        from apps.assignment_app.assignment_agent_indie import AssignmentAgentIndie

        return AssignmentAgentIndie
