from statemachine import State, StateMachine

class GateStateMachine(StateMachine):
    closed = State('Closed', initial=True)
    available = State('Available')
    occupied = State('Occupied')
    service_in_progress = State('Service in Progress')
    service_complete = State('Service Complete')
    unavailable = State('Unavailable')

    open_gate = closed.to(available)
    close_gate = (available | occupied | service_complete).to(closed)
    assign_truck = available.to(occupied)
    start_service = occupied.to(service_in_progress)
    finish_service = service_in_progress.to(service_complete)
    release_gate = service_complete.to(available)

class Facility:
    def __init__(self, name, num_gates):
        self.name = name
        self.gates = [GateStateMachine() for _ in range(num_gates)]
        self.queue = []

    def open_all_gates(self):
        for gate in self.gates:
            if gate.current_state.id == 'closed':
                gate.open_gate()

    def close_all_gates(self):
        for gate in self.gates:
            if gate.current_state.id != 'closed':
                gate.close_gate()

    def truck_arrives(self, truck):
        self.queue.append(truck)
        self.try_assign_truck_to_gate()

    def try_assign_truck_to_gate(self):
        for gate in self.gates:
            if gate.current_state.id == 'available' and self.queue:
                truck = self.queue.pop(0)
                gate.assign_truck()
                # Assign truck to gate (extend as needed)

    def gate_service_complete(self, gate_id):
        gate = self.gates[gate_id]
        gate.finish_service()
        gate.release_gate()
        self.try_assign_truck_to_gate()
