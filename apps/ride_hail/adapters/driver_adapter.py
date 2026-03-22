class RideHailDriverAdapter:
    """Thin compatibility adapter for driver role wiring."""
    @staticmethod
    def get_app_class():
        from apps.ride_hail.driver.app import DriverApp

        return DriverApp

    @staticmethod
    def get_agent_class():
        from apps.ride_hail.driver.agent import DriverAgentIndie

        return DriverAgentIndie

    @staticmethod
    def get_manager_class():
        from apps.ride_hail.driver.manager import DriverManager

        return DriverManager
