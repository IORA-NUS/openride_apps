class RideHailPassengerAdapter:
    """Thin compatibility adapter for passenger role wiring."""
    @staticmethod
    def get_app_class():
        from apps.ridehail.passenger.app import PassengerApp

        return PassengerApp

    @staticmethod
    def get_agent_class():
        from apps.ridehail.passenger.agent import PassengerAgentIndie

        return PassengerAgentIndie

    @staticmethod
    def get_manager_class():
        from apps.ridehail.passenger.manager import PassengerManager

        return PassengerManager
