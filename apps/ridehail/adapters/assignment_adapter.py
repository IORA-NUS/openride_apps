class RideHailAssignmentAdapter:
    """Thin compatibility adapter for assignment role wiring."""
    @staticmethod
    def get_app_class():
        from apps.ridehail.assignment.app import AssignmentApp

        return AssignmentApp

    @staticmethod
    def get_agent_class():
        from apps.ridehail.assignment.agent import AssignmentAgentIndie

        return AssignmentAgentIndie

    @staticmethod
    def get_manager_class():
        from apps.ridehail.assignment.manager import AssignmentManager

        return AssignmentManager
