

from statemachine import State, StateMachine

class StateMachineSerializer:
    """
    Utility class to serialize and deserialize state machines.
    - deserialize(definition): builds a StateMachine class from definition
    - serialize(sm_cls): serializes a StateMachine class to definition
    """
    @staticmethod
    def deserialize(definition):
        states = {}
        initial = definition['initial']
        # Determine which states are final (targets that are never sources)
        all_sources = set()
        all_targets = set()
        for t in definition['transitions']:
            sources = t['source']
            if not isinstance(sources, list):
                sources = [sources]
            all_sources.update(sources)
            all_targets.add(t['target'])
        final_states = all_targets - all_sources

        # Create State objects (states is a list of strings)
        for name in definition['states']:
            is_initial = (name == initial)
            is_final = name in final_states
            states[name] = State(name, initial=is_initial, final=is_final)

        # Debug print states and transitions
        print("[DEBUG] States:", list(states.keys()))
        print("[DEBUG] Initial:", initial)
        print("[DEBUG] Transitions:")
        for t in definition['transitions']:
            print("  ", t)

        # Build class dict with states as class attributes
        class_dict = {name: state for name, state in states.items()}

        # Attach each transition as a unique method (trigger_source_target)
        for t in definition['transitions']:
            trigger = t['trigger']
            sources = t['source']
            target = t['target']
            if not isinstance(sources, list):
                sources = [sources]
            for src in sources:
                method_name = f"{trigger}__{src}__{target}"
                class_dict[method_name] = getattr(states[src], 'to')(states[target])

        # Compose all transitions for each trigger so main trigger works from all valid sources
        trigger_map = {}
        for t in definition['transitions']:
            trigger = t['trigger']
            sources = t['source']
            target = t['target']
            if not isinstance(sources, list):
                sources = [sources]
            for src in sources:
                method = getattr(states[src], 'to')(states[target])
                if trigger not in trigger_map:
                    trigger_map[trigger] = method
                else:
                    trigger_map[trigger] = trigger_map[trigger] | method
        for trigger, composed in trigger_map.items():
            class_dict[trigger] = composed


        # Recreate user-defined methods from source code if present in definition
        if 'methods' in definition:
            dynamic_method_names = []
            dynamic_method_sources = {}
            for method_name, method_source in definition['methods'].items():
                namespace = {}
                try:
                    exec(method_source, namespace)
                    func = namespace.get(method_name)
                    if func:
                        class_dict[method_name] = func
                        dynamic_method_names.append(method_name)
                        dynamic_method_sources[method_name] = method_source
                    else:
                        def stub(self, *args, **kwargs):
                            raise NotImplementedError(f"Method '{method_name}' could not be reconstructed.")
                        stub.__name__ = method_name
                        class_dict[method_name] = stub
                        dynamic_method_names.append(method_name)
                        dynamic_method_sources[method_name] = method_source
                except Exception:
                    def stub(self, *args, **kwargs):
                        raise NotImplementedError(f"Method '{method_name}' could not be reconstructed.")
                    stub.__name__ = method_name
                    class_dict[method_name] = stub
                    dynamic_method_names.append(method_name)
                    dynamic_method_sources[method_name] = method_source
            class_dict['_dynamic_methods'] = dynamic_method_names
            class_dict['_dynamic_method_sources'] = dynamic_method_sources

        # Dynamically create a StateMachine subclass with states and transitions attached
        DynamicSM = type("DynamicSM", (StateMachine,), class_dict)
        return DynamicSM

    @staticmethod
    def serialize(sm_cls):
        import inspect
        sm = sm_cls()
        states = [state.name for state in sm.states]
        transitions = []
        for state in sm.states:
            for transition in getattr(state, 'transitions', []):
                trigger = getattr(transition, 'trigger', None)
                event = getattr(transition, 'event', None)
                transitions.append({
                    'trigger': trigger or event or '',
                    'source': state.name,
                    'target': transition.target.name,
                })
        initial = None
        for state in sm.states:
            if state.initial:
                initial = state.name
                break

        return {
            'states': states,
            'transitions': transitions,
            'initial': initial,
        }

# Example usage:
if __name__ == '__main__':

    def print_state(sm):
        print(sm.configuration[0].id)

    definition = {
        "states": ["idle", "running", "finished"],
        "transitions": [
            {"trigger": "start", "source": "idle", "target": "running"},
            {"trigger": "finish", "source": "running", "target": "finished"}
        ],
        "initial": "idle",
        "methods": {
            "on_start": "def on_start(self, msg):\n    print('Before:', msg['hello'])\n    msg['hello'] = 'universe'\n    print('After:', msg['hello'])\n",
            "on_finish": "def on_finish(self, msg):\n    print('Before:', msg['hello'])\n    msg['hello'] = 'universe'\n    print('After:', msg['hello'])\n"
        }
    }
    DynamicSM = StateMachineSerializer.deserialize(definition)
    sm = DynamicSM()
    print_state(sm)  # 'idle'
    msg = {'hello': 'world'}
    sm.start(msg)
    print_state(sm)  # 'running'
    msg = {'hello': 'world'}
    sm.finish(msg)
    print_state(sm)  # 'finished'

    # Serialize back
    serialized = StateMachineSerializer.serialize(DynamicSM)
    print("[SERIALIZED]", serialized)

