class RideHailPassengerAdapter:
    """Thin compatibility adapter for passenger role wiring."""

    @staticmethod
    def get_app_class():
        from apps.passenger_app.passenger_app import PassengerApp

        return PassengerApp

    @staticmethod
    def get_agent_class():
        from apps.passenger_app.passenger_agent_indie import PassengerAgentIndie

        return PassengerAgentIndie
