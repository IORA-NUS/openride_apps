import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.state_machine.container_logistics.facility_agent_sm import FacilityAgentInteractionStateMachine
from apps.state_machine.container_logistics.haulier_workflow_sm import HaulierContainerWorkflowStateMachine
from apps.state_machine.ride_hail.ridehail_driver_trip_sm import RidehailDriverTripStateMachine
from apps.state_machine.ride_hail.ridehail_passenger_trip_sm import RidehailPassengerTripStateMachine
from statemachine.contrib.diagram import DotGraphMachine


def _generate_for_class(machine_cls: type, output_path: Path) -> None:
    graph = DotGraphMachine(machine_cls).get_graph()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_format = output_path.suffix.lstrip(".")
    graph.write(str(output_path), format=output_format)


def main() -> None:
    _generate_for_class(
        HaulierContainerWorkflowStateMachine,
        ROOT / "container_logistics" / "diagrams" / "haulier_workflow_sm.png",
    )
    _generate_for_class(
        FacilityAgentInteractionStateMachine,
        ROOT / "container_logistics" / "diagrams" / "facility_agent_sm.png",
    )
    _generate_for_class(
        RidehailDriverTripStateMachine,
        ROOT / "ride_hail" / "diagrams" / "ridehail_driver_trip_sm.png",
    )
    _generate_for_class(
        RidehailPassengerTripStateMachine,
        ROOT / "ride_hail" / "diagrams" / "ridehail_passenger_trip_sm.png",
    )


if __name__ == "__main__":
    main()
