class RideHailDriverAdapter:
    """Thin compatibility adapter for driver role wiring."""

    @staticmethod
    def get_app_class():
        from apps.ride_hail.driver import DriverApp

        return DriverApp

    @staticmethod
    def get_agent_class():
        from apps.ride_hail.driver import DriverAgentIndie

        return DriverAgentIndie
