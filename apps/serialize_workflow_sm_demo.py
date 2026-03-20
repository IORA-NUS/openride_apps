# from apps.agent_core.state_machine.sm_serialization_utils import serialize_statemachine
# from apps.agent_core.state_machine import StateMachineSerializer
from orsim.utils import StateMachineSerializer
from apps.agent_core.state_machine.workflow_sm import WorkflowStateMachine
from apps.state_machine.ride_hail.ridehail_driver_trip_sm import RidehailDriverTripStateMachine
from apps.state_machine.ride_hail.ridehail_passenger_trip_sm import RidehailPassengerTripStateMachine

import json

if __name__ == "__main__":
    definition = StateMachineSerializer.serialize(RidehailDriverTripStateMachine)
    print(json.dumps(definition, indent=2))
