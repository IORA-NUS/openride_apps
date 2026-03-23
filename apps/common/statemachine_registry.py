# Statemachine registration and validation
from .statemachine_manager import StateMachineManager
# from apps.agent_core.state_machine.workflow_sm import WorkflowStateMachine
from orsim.utils import WorkflowStateMachine

# from .ridehail_driver_trip_sm import RidehailDriverTripStateMachine
# from .ridehail_passenger_trip_sm import RidehailPassengerTripStateMachine


class StateMachineRegistry:
    """Registry for state machines used in the ride hail simulation."""
    # state_machines = {
    #     'WorkflowStateMachine': WorkflowStateMachine,
    #     'RidehailDriverTripStateMachine': RidehailDriverTripStateMachine,
    #     'RidehailPassengerTripStateMachine': RidehailPassengerTripStateMachine,
    # }
    def __init__(self, statemachines, domain):
        self.statemachines = statemachines

        self.domain = domain

    def register_state_machines(self, server_url, headers):
        for statemachine_name, statemachine_class in self.statemachines.items():
            try:
                print(f"Registering state machine: {statemachine_name}")
                result = StateMachineManager.register_and_validate(
                    server_url=server_url,
                    headers=headers,
                    domain=self.domain,
                    statemachine_name=statemachine_name,
                    statemachine_cls=statemachine_class)
                print(f"Statemachine registration result: {result}")
            except ValueError as e:
                print(f"ERROR: {e}")
                raise e
            except Exception as e:
                print(f"Unexpected error: {e}")
                raise e

