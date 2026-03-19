# Statemachine registration and validation
from apps.agent_core.state_machine.sm_serialization_utils import register_and_validate_statemachine
from apps.agent_core.state_machine.workflow_sm import WorkflowStateMachine

from .ridehail_driver_trip_sm import RidehailDriverTripStateMachine
from .ridehail_passenger_trip_sm import RidehailPassengerTripStateMachine


class StateMachineRegistry:
    """Registry for state machines used in the ride hail simulation."""
    state_machines = {
        'workflow': WorkflowStateMachine,
        'ridehail_driver_trip': RidehailDriverTripStateMachine,
        'ridehail_passenger_trip': RidehailPassengerTripStateMachine,
    }

    SIM_TYPE = 'ride_hail'

    def register_state_machines(self, SERVER_URL, headers):
        for sm_name, sm_class in self.state_machines.items():
            try:
                print(f"Registering state machine: {sm_name}")
                result = register_and_validate_statemachine(SERVER_URL, headers, self.SIM_TYPE, sm_name, sm_class)
                print(f"Statemachine registration result: {result}")
            except ValueError as e:
                print(f"ERROR: {e}")
                raise e
            except Exception as e:
                print(f"Unexpected error: {e}")
                raise e

