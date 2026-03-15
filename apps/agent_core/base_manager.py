from typing import Any
from apps.agent_core.state_machine.workflow_sm import WorkflowStateMachine

class BaseManager:

    def __init__(self, *args, **kwargs):
        # must have user, entity_type and run_id for logging and resource client mixin
        if not hasattr(self, 'user') or not hasattr(self, 'run_id') or not hasattr(self, 'entity_type'):
            raise NotImplementedError("Subclasses of BaseManager must have 'user', 'run_id', and 'entity_type' attributes.")
        # pass

    def as_dict(self):
        """Return the current entity as a dict."""
        return self.entity

    def get_id(self):
        """Return the current entity's id."""
        return self.entity['_id']

    def estimate_next_event_time(self, current_time):
        """Default: return a distant future date."""
        from dateutil.relativedelta import relativedelta
        return current_time + relativedelta(years=1)

    def init_entity(self, sim_clock, data=None, params={}):
        """Get or create the entity using resource_get and resource_post."""

        result = self.resource_get(entity_id=None, params=params)

        items = result.get('_items', []) if isinstance(result, dict) else []
        if not items:
            self.create_entity(sim_clock, data=data)
            return self.init_entity(sim_clock, data=data, params=params)
        return items[0]

    def create_entity(self, sim_clock, data=None):
        """Create the entity using resource_post. Expects data to be a dict."""
        if data is None:
            raise NotImplementedError("Subclasses must provide data or override create_entity.")
        return self.resource_post(data=data)

    def update_entity(self, data):
        """Update the entity using resource_patch."""
        return self.resource_patch(entity_id=self.get_id(), data=data, etag=self.entity.get('_etag'))

    def login(self, sim_clock):
        """Generic login using transition_entity_to_state. Assumes 'dormant' → 'offline' → 'online'."""
        if self.entity['state'] == 'dormant':
            self.entity = self.transition_entity_to_state(self.entity, 'offline', sim_clock)
            print(f"{self.__class__.__name__}.login: Transitioned from dormant to offline for entity {self.get_id()}")
            return self.login(sim_clock)  # Recursive call to handle next transition
        if self.entity['state'] == 'offline':
            self.entity = self.transition_entity_to_state(self.entity, 'online', sim_clock)
            print(f"{self.__class__.__name__}.login: Transitioned from offline to online for entity {self.get_id()}")
            return self.login(sim_clock)  # Recursive call to handle next transition
        if self.entity['state'] == 'online':
            print(f"{self.__class__.__name__}.login: Entity {self.get_id()} is now online")
            return self.entity
        raise Exception("unknown Workflow State")

    def logout(self, sim_clock):
        """Generic logout using transition_entity_to_state. Assumes 'logout' is a valid transition from current state."""
        self.entity = self.transition_entity_to_state(self.entity, 'logout', sim_clock)
        print(f"{self.__class__.__name__}.logout: Entity {self.get_id()} has logged out")
        return self.entity

    def refresh(self):
        """Refresh the local entity state from the backend."""
        result = self.resource_get(entity_id=self.entity['_id'])
        if result:
            self.entity = result
        else:
            raise Exception(f'{self.__class__.__name__}.refresh: Failed getting response for {self.entity["_id"]}')

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

    def transition_entity_to_state(self, entity, target_state, sim_clock):
        """
        State transition logic using WorkflowStateMachine.
        Calls resource_patch, which must be implemented by subclasses or via ResourceClientMixin.
        """
        machine = WorkflowStateMachine(start_value=entity["state"])
        event = next(
            (t.event for t in machine.current_state.transitions if t.target.name == target_state),
            None
        )
        if event is None:
            raise Exception(f"No transition from {entity['state']} to {target_state}")
        # Example usage: self.resource_patch(resource_type, entity_id, data, etag, timeout)
        self.resource_patch(entity["_id"], {"transition": event, "sim_clock": sim_clock}, etag=entity.get("_etag"))
        return_value = self.resource_get(entity_id=entity["_id"])  # Refresh entity after transition
        # print(f"{self.__class__.__name__}.transition_entity_to_state: Refresh result for entity {self.get_id()}: {return_value}")
        return return_value

    # The following are abstract methods that subclasses must implement depending on the backend.

    def resource_get(self, entity_id, params={}, timeout=None):
        """GET an entity or collection from the backend. Uses self.entity_type."""
        raise NotImplementedError("Subclasses must implement resource_get or use ResourceClientMixin.")

    def resource_post(self,  entity_id, data, timeout=None):
        """POST an entity to the backend. Uses self.entity_type."""
        raise NotImplementedError("Subclasses must implement resource_post or use ResourceClientMixin.")

    def resource_patch(self,  entity_id, data, etag=None, timeout=None):
        """PATCH an entity in the backend. Uses self.entity_type."""
        raise NotImplementedError("Subclasses must implement resource_patch or use ResourceClientMixin.")
