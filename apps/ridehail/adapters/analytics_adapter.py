class RideHailAnalyticsAdapter:
    """Thin compatibility adapter for analytics role wiring."""
    @staticmethod
    def get_app_class():
        from apps.ridehail.analytics.app import AnalyticsApp

        return AnalyticsApp

    @staticmethod
    def get_agent_class():
        from apps.ridehail.analytics.agent import AnalyticsAgentIndie

        return AnalyticsAgentIndie

    @staticmethod
    def get_manager_class():
        from apps.ridehail.analytics.manager import AnalyticsManager

        return AnalyticsManager
