class RideHailAnalyticsAdapter:
    """Thin compatibility adapter for analytics role wiring."""
    @staticmethod
    def get_app_class():
        from apps.ride_hail.analytics.app import AnalyticsApp

        return AnalyticsApp

    @staticmethod
    def get_agent_class():
        from apps.ride_hail.analytics.agent import AnalyticsAgentIndie

        return AnalyticsAgentIndie
