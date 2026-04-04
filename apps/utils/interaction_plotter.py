# Centralized node name generator
def get_node_name(sm, state):
    return f"{sm}_{state}".replace(" ", "_")
# --- DOT (Graphviz) Export ---
def to_dot(stm_cls_dict, interactions):
    lines = ["digraph StateMachines {"]
    # Subgraphs for each statemachine
    for sm, cls in stm_cls_dict.items():
        lines.append(f"  subgraph cluster_{sm} {{")
        lines.append(f"    label=\"{sm}\"")
        states = extract_states_from_stm_class(cls)
        for state in states:
            node = get_node_name(sm, state)
            label = f"{state}".replace("_", " ")
            lines.append(f"    {node} [label=\"{label}\"];")
        lines.append("  }")
    # Internal transitions
    for sm, cls in stm_cls_dict.items():
        transitions = extract_transitions_from_stm_class(cls)
        for src, event, tgt in transitions:
            src_node = get_node_name(sm, src)
            tgt_node = get_node_name(sm, tgt)
            label = event.replace("_", " ")
            lines.append(f"  {src_node} -> {tgt_node} [label=\"{label}\"];")
    # Cross-statemachine interactions
    for int_item in interactions:
        src_sm = int_item["source_statemachine"]
        src_transition = int_item["source_transition"]
        src_new_state = int_item["source_new_state"]
        tgt_sm = int_item["target_statemachine"]
        tgt_new_state = int_item["target_new_state"]
        desc = int_item["description"]

        src = get_node_name(src_sm, src_new_state)
        tgt = get_node_name(tgt_sm, tgt_new_state)
        label = f"{src_transition}: {desc}".replace("_", " ")
        lines.append(f"  {src} -> {tgt} [label=\"{label}\", style=dashed, color=blue];")
    lines.append("}")
    return "\n".join(lines)

# --- PlantUML Export ---
def to_plantuml(stm_cls_dict, interactions):
    lines = ["@startuml", "skinparam state {", "  BackgroundColor<<Driver>> LightBlue", "  BackgroundColor<<Passenger>> LightGreen", "}"]
    # States for each statemachine
    for sm, cls in stm_cls_dict.items():
        states = extract_states_from_stm_class(cls)
        for state in states:
            node = get_node_name(sm, state)
            label = f"{state}".replace("_", " ")
            lines.append(f"state \"{label}\" as {node} <<{sm}>>")
    # Internal transitions
    for sm, cls in stm_cls_dict.items():
        transitions = extract_transitions_from_stm_class(cls)
        for src, event, tgt in transitions:
            src_node = get_node_name(sm, src)
            tgt_node = get_node_name(sm, tgt)
            label = event.replace("_", " ")
            lines.append(f"{src_node} --> {tgt_node} : {label}")
    # Cross-statemachine interactions
    for int_item in interactions:
        src_sm = int_item["source_statemachine"]
        src_transition = int_item["source_transition"]
        src_new_state = int_item["source_new_state"]
        tgt_sm = int_item["target_statemachine"]
        tgt_new_state = int_item["target_new_state"]
        desc = int_item["description"]

        src = get_node_name(src_sm, src_new_state)
        tgt = get_node_name(tgt_sm, tgt_new_state)
        label = f"{src_transition}: {desc}".replace("_", " ")
        lines.append(f"{src} -[#blue,dashed]-> {tgt} : {label}")
    lines.append("@enduml")
    return "\n".join(lines)
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
            node = get_node_name(sm, state)
            label = f"{state}".replace("_", " ")
            lines.append(f"        {node}[{label}]" )
        lines.append("    end")
    # Draw all statemachine-internal transitions (vertical)
    for sm, cls in stm_cls_dict.items():
        transitions = extract_transitions_from_stm_class(cls)
        for src, event, tgt in transitions:
            src_node = get_node_name(sm, src)
            tgt_node = get_node_name(sm, tgt)
            label = event.replace("_", " ")
            lines.append(f"    {src_node} --|{label}|--> {tgt_node}")
    # Draw cross-statemachine interactions (sideways/horizontal)
    for int_item in interactions:
        src_sm = int_item["source_statemachine"]
        src_transition = int_item["source_transition"]
        src_new_state = int_item["source_new_state"]
        tgt_sm = int_item["target_statemachine"]
        tgt_new_state = int_item["target_new_state"]
        desc = int_item["description"]

        src = get_node_name(src_sm, src_new_state)
        tgt = get_node_name(tgt_sm, tgt_new_state)
        label = f"{src_transition}: {desc}".replace("_", " ")
        # Use -.-> for horizontal/sideways arrows
        lines.append(f"    {src} -. |{label}| .-> {tgt}")
    return "\n".join(lines)



# Example usage

if __name__ == "__main__":
    # Import your statemachine classes here
    from apps.ridehail.statemachine import RidehailDriverTripStateMachine
    from apps.ridehail.statemachine import RidehailPassengerTripStateMachine
    from apps.ridehail.statemachine import driver_passenger_interactions

    # --- Cross-statemachine interaction diagram ---

    stm_cls_dict = {
        "RidehailDriverTripStateMachine": RidehailDriverTripStateMachine,
        "RidehailPassengerTripStateMachine": RidehailPassengerTripStateMachine
    }
    print("\n=== Unified statemachine + interaction diagram ===\n")
    mermaid_code = to_mermaid(stm_cls_dict, driver_passenger_interactions)
    # dot_code = to_dot(stm_cls_dict, INTERACTION_MAP)
    # plantuml_code = to_plantuml(stm_cls_dict, INTERACTION_MAP)

    # print(mermaid_code)
    # Write Mermaid
    with open("interaction_map.mmd", "w") as f:
        f.write(mermaid_code)
    print("Mermaid diagram written to interaction_map.mmd")


    # # Write PlantUML
    # with open("interaction_map.puml", "w") as f:
    #     f.write(plantuml_code)
    # print("PlantUML diagram written to interaction_map.puml")

    # # Write DOT
    # with open("interaction_map.dot", "w") as f:
    #     f.write(dot_code)
    # print("DOT diagram written to interaction_map.dot")

    # # Try to export SVG from DOT if graphviz is available
    # try:
    #     import subprocess
    #     subprocess.run(["dot", "-Tsvg", "interaction_map.dot", "-o", "interaction_map.svg"], check=True)
    #     print("SVG diagram written to interaction_map.svg")
    # except Exception as e:
    #     print(f"Could not generate SVG from DOT: {e}")


