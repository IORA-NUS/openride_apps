from typing import Any
# from apps.agent_core.state_machine.workflow_sm import WorkflowStateMachine
from orsim.utils import WorkflowStateMachine

class BaseManager:

    def __init__(self, *args, **kwargs):
        # must have user, run_id, and persona for logging and resource client mixin
        if not hasattr(self, 'user') or \
            not hasattr(self, 'run_id') or \
            not hasattr(self, 'persona'):
            raise NotImplementedError("Subclasses of BaseManager must have 'user', 'run_id', 'persona', and 'resource' attributes.")

        # resource must be a dict and shoule have id
        if not isinstance(self.resource, dict) or '_id' not in self.resource:
            raise NotImplementedError("Subclasses of BaseManager must have an 'resource' attribute that is a dict containing an '_id' key.")
        # pass

    def as_dict(self):
        """Return the current resource as a dict."""
        return self.resource

    def get_id(self):
        """Return the current resource's id."""
        return self.resource.get('_id')

    def estimate_next_event_time(self, current_time):
        """Default: return a distant future date."""
        from dateutil.relativedelta import relativedelta
        return current_time + relativedelta(years=1)

    def init_resource(self, sim_clock, data=None, params={}):
        """Get or create the resource using resource_get and resource_post."""

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

    def update_resource(self, data):
        """Update the resource using resource_patch."""
        return self.resource_patch(resource_id=self.get_id(), data=data, etag=self.resource.get('_etag'))

    def login(self, sim_clock):
        """Generic login using transition_resource_to_state. Assumes 'dormant' → 'offline' → 'online'."""
        if self.resource['state'] == 'dormant':
            self.resource = self.transition_resource_to_state(self.resource, 'offline', sim_clock)
            print(f"{self.__class__.__name__}.login: Transitioned from dormant to offline for resource {self.get_id()}")
            return self.login(sim_clock)  # Recursive call to handle next transition
        if self.resource['state'] == 'offline':
            self.resource = self.transition_resource_to_state(self.resource, 'online', sim_clock)
            print(f"{self.__class__.__name__}.login: Transitioned from offline to online for resource {self.get_id()}")
            return self.login(sim_clock)  # Recursive call to handle next transition
        if self.resource['state'] == 'online':
            print(f"{self.__class__.__name__}.login: resource {self.get_id()} is now online")
            return self.resource
        raise Exception("unknown Workflow State")

    def logout(self, sim_clock):
        """Generic logout using transition_resource_to_state. Assumes 'logout' is a valid transition from current state."""
        # self.resource = self.transition_resource_to_state(self.resource, 'logout', sim_clock)
        try:
            self.resource = self.transition_resource_to_state(self.resource, 'offline', sim_clock)
            print(f"{self.__class__.__name__}.logout: resource {self.get_id()} has logged out")
        except Exception as e:
            print(f"{self.__class__.__name__}.logout: unable to logout resource {self.get_id()}: {e}")
        # May be consider moving te state to dormant if the agent will not need to participate in market again...
        return self.resource

    def refresh(self):
        """Refresh the local resource state from the backend."""
        result = self.resource_get(resource_id=self.resource['_id'])
        if result:
            self.resource = result
        else:
            raise Exception(f'{self.__class__.__name__}.refresh: Failed getting response for {self.resource["_id"]}')

    # def start(self):
    #     """
    #     Optional lifecycle hook for subclasses. Called to start or initialize the manager if needed.
    #     Override in subclasses if your manager needs explicit startup logic.
    #     """
    #     pass

    # def stop(self):
    #     """
    #     Optional lifecycle hook for subclasses. Called to stop or clean up the manager if needed.
    #     Override in subclasses if your manager needs explicit shutdown logic.
    #     """
    #     pass

    def transition_resource_to_state(self, resource, target_state, sim_clock,):
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
        # Example usage: self.resource_patch(resource_id, data, etag, timeout)
        self.resource_patch(resource["_id"], {"transition": event, "sim_clock": sim_clock}, etag=resource.get("_etag"))
        return_value = self.resource_get(resource_id=resource["_id"])  # Refresh resource after transition
        # print(f"{self.__class__.__name__}.transition_resource_to_state: Refresh result for resource {self.get_id()}: {return_value}")
        return return_value

    # The following are abstract methods that subclasses must implement depending on the backend.

    def resource_get(self, resource_id, params={}, timeout=None):
        """GET an resource or collection from the backend. Uses self.persona."""
        raise NotImplementedError("Subclasses must implement resource_get or use ResourceClientMixin.")

    def resource_post(self,  resource_id, data, timeout=None):
        """POST an resource to the backend. Uses self.persona."""
        raise NotImplementedError("Subclasses must implement resource_post or use ResourceClientMixin.")

    def resource_patch(self,  resource_id, data, etag=None, timeout=None):
        """PATCH an resource in the backend. Uses self.persona."""
        raise NotImplementedError("Subclasses must implement resource_patch or use ResourceClientMixin.")
