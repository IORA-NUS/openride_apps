class RideHailDriverAdapter:
    """Thin compatibility adapter for driver role wiring."""

    @staticmethod
    def get_app_class():
        from apps.driver_app.driver_app import DriverApp

        return DriverApp

    @staticmethod
    def get_agent_class():
        from apps.driver_app.driver_agent_indie import DriverAgentIndie

        return DriverAgentIndie
