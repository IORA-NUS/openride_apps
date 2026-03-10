__all__ = [
	"PassengerApp",
	"PassengerManager",
	"PassengerTripManager",
	"PassengerAgentIndie",
]


def __getattr__(name):
	if name == "PassengerApp":
		from .passenger_app import PassengerApp

		return PassengerApp
	if name == "PassengerManager":
		from .passenger_manager import PassengerManager

		return PassengerManager
	if name == "PassengerTripManager":
		from .passenger_trip_manager import PassengerTripManager

		return PassengerTripManager
	if name == "PassengerAgentIndie":
		from .passenger_agent_indie import PassengerAgentIndie

		return PassengerAgentIndie
	raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
