from abc import ABC, abstractmethod
from typing import Any, Dict

class ManagerContract(ABC):
    """
    Abstract contract for agent lifecycle managers (e.g., DriverManager, PassengerManager).
    Ensures a consistent interface for all role managers in agent-based systems.
    """

    @abstractmethod
    def as_dict(self) -> Dict[str, Any]:
        """Return the resource as a dictionary representation."""
        pass

    @abstractmethod
    def get_id(self) -> str:
        """Return the unique identifier for the managed resource."""
        pass

    @abstractmethod
    def estimate_next_event_time(self, current_time: Any) -> Any:
        """Estimate the next event time for the resource (domain-specific)."""
        pass

    @abstractmethod
    def init_resource(self, sim_clock: Any) -> Any:
        """Initialize the managed resource (e.g., driver, passenger) for the given simulation clock."""
        pass

    @abstractmethod
    def create_resource(self, sim_clock: Any) -> Any:
        """Create the managed resource in the backend or database for the given simulation clock."""
        pass

    @abstractmethod
    def login(self, sim_clock: Any) -> None:
        """Log in the managed resource to the system/session for the given simulation clock."""
        pass

    @abstractmethod
    def logout(self, sim_clock: Any) -> None:
        """Log out the managed resource from the system/session for the given simulation clock."""
        pass

    @abstractmethod
    def refresh(self, sim_clock: Any) -> None:
        """Refresh the state of the managed resource from the backend or database for the given simulation clock."""
        pass
