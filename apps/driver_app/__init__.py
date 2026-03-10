
__all__ = [
	"DriverManager",
	"DriverTripManager",
	"DriverApp",
	"DriverAgentIndie",
]


def __getattr__(name):
	if name == "DriverManager":
		from .driver_manager import DriverManager

		return DriverManager
	if name == "DriverTripManager":
		from .driver_trip_manager import DriverTripManager

		return DriverTripManager
	if name == "DriverApp":
		from .driver_app import DriverApp

		return DriverApp
	if name == "DriverAgentIndie":
		from .driver_agent_indie import DriverAgentIndie

		return DriverAgentIndie
	raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
