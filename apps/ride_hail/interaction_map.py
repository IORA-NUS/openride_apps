# --- Helpers for python-statemachine v3+ ---
def extract_states_from_stm_class(stm_cls):
    # Instantiate to access .states (python-statemachine v3+)
    instance = stm_cls()
    return [state.name for state in instance.states]

def extract_transitions_from_stm_class(stm_cls):
    # Instantiate to access states and their transitions (python-statemachine v3+)
    instance = stm_cls()
    transitions = []
    for state in instance.states:
        for transition in state.transitions:
            # transition.source and transition.target are State objects
            src = transition.source.name
            tgt = transition.target.name
            # transition.events is a list of Event objects (may be empty)
            if hasattr(transition, 'events') and transition.events:
                for event in transition.events:
                    event_name = getattr(event, 'name', str(event))
                    transitions.append((src, event_name, tgt))
            else:
                # fallback: no event, just record the transition
                transitions.append((src, '', tgt))
    return transitions
# interaction_map.py
# Explicit declarative mapping of statemachine interactions for visualization and analysis

# Each tuple: (source_statemachine, source_state, event, target_statemachine, target_state, description)
INTERACTION_MAP = [
    # Driver receives trip request from passenger
    ("Passenger", "requested", "PASSENGER_CONFIRMED_TRIP", "Driver", "moving_to_pickup", "Passenger confirms trip, driver starts moving to pickup"),
    ("Passenger", "requested", "PASSENGER_REJECTED_TRIP", "Driver", "looking_for_job", "Passenger rejects trip, driver resumes looking for job"),
    # Driver arrives at pickup
    ("Driver", "moving_to_pickup", "DRIVER_ARRIVED_FOR_PICKUP", "Passenger", "waiting_for_pickup", "Driver arrives, passenger waits for pickup"),
    # Passenger acknowledges pickup
    ("Passenger", "waiting_for_pickup", "PASSENGER_ACKNOWLEDGE_PICKUP", "Driver", "moving_to_dropoff", "Passenger acknowledges pickup, driver moves to dropoff"),
    # Driver arrives at dropoff
    ("Driver", "moving_to_dropoff", "DRIVER_ARRIVED_FOR_DROPOFF", "Passenger", "droppedoff", "Driver arrives at dropoff, passenger dropped off"),
    # Passenger acknowledges dropoff
    ("Passenger", "droppedoff", "PASSENGER_ACKNOWLEDGE_DROPOFF", "Driver", "droppedoff", "Passenger acknowledges dropoff, driver ends trip"),
    # Add more as needed for your flows
]

# Optionally: function to export to Mermaid or Graphviz





def to_mermaid(stm_cls_dict, interactions):
    """
    stm_cls_dict: dict of statemachine name -> python-statemachine class
    interactions: list of (src_sm, src_state, event, tgt_sm, tgt_state, desc)
    """
    lines = ["flowchart TD"]
    # Draw subgraphs for each statemachine
    for sm, cls in stm_cls_dict.items():
        states = extract_states_from_stm_class(cls)
        lines.append(f"    subgraph {sm}")
        for state in states:
            node = f"{sm}_{state}".replace(" ", "_")
            label = f"{state}".replace("_", " ")
            lines.append(f"        {node}[{label}]" )
        lines.append("    end")
    # Draw all statemachine-internal transitions (vertical)
    for sm, cls in stm_cls_dict.items():
        transitions = extract_transitions_from_stm_class(cls)
        for src, event, tgt in transitions:
            src_node = f"{sm}_{src}".replace(" ", "_")
            tgt_node = f"{sm}_{tgt}".replace(" ", "_")
            label = event.replace("_", " ")
            lines.append(f"    {src_node} --|{label}|--> {tgt_node}")
    # Draw cross-statemachine interactions (sideways/horizontal)
    for src_sm, src_state, event, tgt_sm, tgt_state, desc in interactions:
        src = f"{src_sm}_{src_state}".replace(" ", "_")
        tgt = f"{tgt_sm}_{tgt_state}".replace(" ", "_")
        label = f"{event}: {desc}".replace("_", " ")
        # Use -.-> for horizontal/sideways arrows
        lines.append(f"    {src} -. |{label}| .-> {tgt}")
    return "\n".join(lines)



# Example usage

if __name__ == "__main__":
    # Import your statemachine classes here
    from apps.ride_hail.statemachine.ridehail_driver_trip_sm import RidehailDriverTripStateMachine
    from apps.ride_hail.statemachine.ridehail_passenger_trip_sm import RidehailPassengerTripStateMachine

    # --- Cross-statemachine interaction diagram ---

    stm_cls_dict = {
        "RidehailDriverTripStateMachine": RidehailDriverTripStateMachine,
        "RidehailPassengerTripStateMachine": RidehailPassengerTripStateMachine
    }
    print("\n=== Unified statemachine + interaction diagram ===\n")
    mermaid_code = to_mermaid(stm_cls_dict, INTERACTION_MAP)
    # print(mermaid_code)
    # Write to file
    with open("interaction_map.mmd", "w") as f:
        f.write(mermaid_code)
    print("\nMermaid diagram written to interaction_map.mmd\n")


