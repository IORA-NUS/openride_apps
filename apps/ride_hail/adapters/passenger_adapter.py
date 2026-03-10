class RideHailPassengerAdapter:
    """Thin compatibility adapter for passenger role wiring."""

    @staticmethod
    def get_app_class():
        from apps.ride_hail.passenger.app import PassengerApp

        return PassengerApp

    @staticmethod
    def get_agent_class():
        from apps.ride_hail.passenger.agent import PassengerAgentIndie

        return PassengerAgentIndie
