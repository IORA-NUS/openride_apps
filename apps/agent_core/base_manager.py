
from typing import Any, Dict, Optional
from abc import ABC, abstractmethod
from orsim.utils import WorkflowStateMachine
import logging

class BaseManager(ABC):
    """
    Abstract base class for all manager modules. Designed for library use.
    Subclasses must provide user, run_id, persona, and resource attributes.
    """
    user: Any
    run_id: str
    persona: Any
    resource: Dict[str, Any]

    def __init__(self, *args, **kwargs):
        # Ensure required attributes are set by subclass
        missing = [attr for attr in ('user', 'run_id', 'persona', 'resource') if not hasattr(self, attr)]
        if missing:
            raise NotImplementedError(f"Subclasses of BaseManager must have attributes: {', '.join(missing)}.")
        if not isinstance(self.resource, dict) or '_id' not in self.resource:
            raise NotImplementedError("'resource' must be a dict containing an '_id' key.")
        self.on_init()

    @abstractmethod
    def on_init(self) -> None:
        """Hook for subclasses to run logic after __init__. Must be implemented."""
        pass

    def as_dict(self) -> Dict[str, Any]:
        """Return the current resource as a dict."""
        return self.resource

    def get_id(self) -> Optional[Any]:
        """Return the current resource's id."""
        return self.resource.get('_id')

    def estimate_next_event_time(self, current_time) -> Any:
        """Default: return a distant future date."""
        from dateutil.relativedelta import relativedelta
        return current_time + relativedelta(years=1)

    def init_resource(self, sim_clock, data=None, params=None):
        """Get or create the resource using resource_get and resource_post."""
        if params is None:
            params = {}
        result = self.resource_get(resource_id=None, params=params)
        items = result.get('_items', []) if isinstance(result, dict) else []
        if not items:
            self.create_resource(sim_clock, data=data)
            return self.init_resource(sim_clock, data=data, params=params)
        return items[0]

    def create_resource(self, sim_clock, data=None):
        """Create the resource using resource_post. Expects data to be a dict."""
        if data is None:
            raise NotImplementedError("Subclasses must provide data or override create_resource.")
        return self.resource_post(data=data)

    def update_resource(self, data: Dict[str, Any]) -> Any:
        """Update the resource using resource_patch."""
        return self.resource_patch(resource_id=self.get_id(), data=data, etag=self.resource.get('_etag'))

    def login(self, sim_clock: Any) -> Any:
        """Generic login using transition_resource_to_state. Assumes 'dormant' → 'offline' → 'online'."""
        state = self.resource.get('state')
        if state == 'dormant':
            self.resource = self.transition_resource_to_state(self.resource, 'offline', sim_clock)
            logging.info(f"{self.__class__.__name__}.login: Transitioned from dormant to offline for resource {self.get_id()}")
            return self.login(sim_clock)
        if state == 'offline':
            self.resource = self.transition_resource_to_state(self.resource, 'online', sim_clock)
            logging.info(f"{self.__class__.__name__}.login: Transitioned from offline to online for resource {self.get_id()}")
            return self.login(sim_clock)
        if state == 'online':
            logging.info(f"{self.__class__.__name__}.login: resource {self.get_id()} is now online")
            return self.resource
        raise Exception(f"Unknown Workflow State: {state}")

    def logout(self, sim_clock: Any) -> Any:
        """Generic logout using transition_resource_to_state. Assumes 'logout' is a valid transition from current state."""
        try:
            self.resource = self.transition_resource_to_state(self.resource, 'offline', sim_clock)
            logging.info(f"{self.__class__.__name__}.logout: resource {self.get_id()} has logged out")
        except Exception as e:
            logging.warning(f"{self.__class__.__name__}.logout: unable to logout resource {self.get_id()}: {e}")
        return self.resource

    def refresh(self) -> None:
        """Refresh the local resource state from the backend."""
        result = self.resource_get(resource_id=self.resource['_id'])
        if result:
            self.resource = result
        else:
            raise Exception(f'{self.__class__.__name__}.refresh: Failed getting response for {self.resource["_id"]}')

    def start(self) -> None:
        """Optional lifecycle hook for subclasses. Called to start or initialize the manager if needed."""
        pass

    def stop(self) -> None:
        """Optional lifecycle hook for subclasses. Called to stop or clean up the manager if needed."""
        pass


    def transition_resource_to_state(self, resource: Dict[str, Any], target_state: str, sim_clock: Any) -> Dict[str, Any]:
        """
        State transition logic using WorkflowStateMachine.
        Calls resource_patch, which must be implemented by subclasses or via ResourceClientMixin.
        """
        machine = WorkflowStateMachine(start_value=resource["state"])
        event = next(
            (t.event for t in machine.current_state.transitions if t.target.name == target_state),
            None
        )
        if event is None:
            raise Exception(f"No transition from {resource['state']} to {target_state}")
        self.resource_patch(resource["_id"], {"transition": event, "sim_clock": sim_clock}, etag=resource.get("_etag"))
        return_value = self.resource_get(resource_id=resource["_id"])  # Refresh resource after transition
        return return_value

    # The following are abstract methods that subclasses must implement depending on the backend.


    @abstractmethod
    def resource_get(self, resource_id, params=None, timeout=None):
        """GET a resource or collection from the backend. Uses self.persona. Must be implemented."""
        pass

    @abstractmethod
    def resource_post(self, data, timeout=None):
        """POST a resource to the backend. Uses self.persona. Must be implemented."""
        pass

    @abstractmethod
    def resource_patch(self, resource_id, data, etag=None, timeout=None):
        """PATCH a resource in the backend. Uses self.persona. Must be implemented."""
        pass
