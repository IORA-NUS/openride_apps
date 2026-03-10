class RideHailAnalyticsAdapter:
    """Thin compatibility adapter for analytics role wiring."""

    @staticmethod
    def get_app_class():
        from apps.analytics_app.analytics_app import AnalyticsApp

        return AnalyticsApp

    @staticmethod
    def get_agent_class():
        from apps.analytics_app.analytics_agent_indie import AnalyticsAgentIndie

        return AnalyticsAgentIndie
