from typing import Any, Dict
from orsim.messenger import Messenger
import logging



from abc import ABC, abstractmethod
from typing import Optional, List

class BaseApp(ABC):
    """
    Robust base class for all app modules. Designed for library use.
    Subclasses must implement create_user and create_manager.
    """
    run_id: str
    sim_clock: str
    credentials: Dict[str, Any]
    messenger: Optional[Messenger]
    persona: Optional[Any]
    user: Any
    manager: Any
    topic_params: dict
    message_queue: List[Any]
    exited_market: bool
    latest_sim_clock: Optional[str]
    latest_loc: Optional[Any]

    def __init__(self, run_id: str, sim_clock: str, credentials: Dict[str, Any], messenger: Optional[Messenger]=None, persona: Optional[Any]=None, **kwargs):
        """
        Initialize the base app.
        Args:
            run_id: Unique run identifier
            sim_clock: Simulation clock time
            credentials: Auth credentials dict
            messenger: Messaging interface (optional)
            persona: Persona information (optional)
            kwargs: Additional fields for subclass customization
        """
        self.run_id = run_id
        self.sim_clock = sim_clock
        self.credentials = credentials
        if messenger is not None:
            if Messenger is None or not isinstance(messenger, Messenger):
                raise TypeError("messenger must be an instance of orsim.messenger.Messenger")
        self.messenger = messenger
        self.persona = persona

        for k, v in kwargs.items():
            setattr(self, k, v)

        self.user = self.create_user()
        self.manager = self.create_manager()

        self.topic_params = {}
        if self.manager and hasattr(self.manager, "get_id"):
            try:
                self.topic_params = {
                    f"{self.run_id}/{self.manager.get_id()}": self.message_handler
                }
            except Exception as e:
                logging.warning(f"Failed to set topic_params: {str(e)}")

        self.message_queue: List[Any] = []
        self.exited_market: bool = False
        self.latest_sim_clock: Optional[str] = None
        self.latest_loc: Optional[Any] = None

    @abstractmethod
    def create_user(self) -> Any:
        """Create and return the user object. Must be implemented by subclass."""
        pass

    @abstractmethod
    def create_manager(self) -> Any:
        """Create and return the manager object. Must be implemented by subclass."""
        pass

    def launch(self, sim_clock: str, **kwargs) -> None:
        """Launch the app and login the manager."""
        if self.manager and hasattr(self.manager, "login"):
            try:
                self.manager.login(sim_clock)
            except Exception as e:
                logging.warning(f"Failed to login manager: {str(e)}")

    def close(self, sim_clock: str) -> None:
        """Close the app and logout the manager."""
        self.exited_market = True
        if self.manager and hasattr(self.manager, "logout"):
            try:
                self.manager.logout(sim_clock)
            except Exception as e:
                logging.warning(f"Failed to logout {self.get_manager()}: {str(e)}")

    def update_current(self, sim_clock: str, current_loc: Any) -> None:
        """Update the latest simulation clock and location."""
        self.latest_sim_clock = sim_clock
        self.latest_loc = current_loc

    def message_handler(self, *args, **kwargs) -> None:
        """Default message handler. Subclasses should override."""
        raise NotImplementedError("Subclasses must implement message_handler.")

    def enqueue_message(self, payload: Any) -> None:
        """Add a message to the end of the queue."""
        self.message_queue.append(payload)

    def dequeue_message(self) -> Optional[Any]:
        """Remove and return the first message from the queue, or None if empty."""
        if not self.message_queue:
            logging.debug("Message queue is empty on dequeue.")
            return None
        return self.message_queue.pop(0)

    def enfront_message(self, payload: Any) -> None:
        """Add a message to the front of the queue."""
        self.message_queue.insert(0, payload)

    def get_manager(self) -> Optional[Any]:
        """Return the manager as a dict, if available."""
        if self.manager and hasattr(self.manager, "as_dict"):
            return self.manager.as_dict()
        return None

    @property
    def is_exited(self) -> bool:
        """Check if the app has exited the market."""
        return self.exited_market
