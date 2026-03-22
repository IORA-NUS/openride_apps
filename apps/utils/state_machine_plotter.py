
from graphviz import Digraph
from statemachine.contrib.diagram import DotGraphMachine
import sys
from pathlib import Path


class StateMachinePlotter:
    """Utility class for plotting state machines using Graphviz."""

    @staticmethod
    def _resolve_output_path(output_path: Path) -> Path:
        """
        Ensures the output path is absolute (relative to current working directory if not absolute)
        and that its parent directory exists.
        """
        resolved = output_path if output_path.is_absolute() else Path.cwd() / output_path
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    @staticmethod
    def plot_legacy(statemachine_cls, output_path: Path) -> None:
        """
        Generates and renders a state machine diagram using Graphviz.

        Args:
            statemachine_cls: The state machine class. It should have a `__name__` attribute,
                a `states` iterable, where each state has a `transitions` iterable. Each transition should have
                `source`, `targets` (list), and `event` attributes.
            output_path: Path to the output SVG file.

        Side Effects:
            Creates and saves an SVG image of the state machine diagram at the specified path.

        Example:
            StateMachinePlotter.plot_legacy(my_state_machine, Path("output/diagram.svg"))
        """
        output_path = StateMachinePlotter._resolve_output_path(output_path)
        dg = Digraph(comment=statemachine_cls.__name__, engine='dot')
        for s in statemachine_cls.states:
            for t in s.transitions:
                for target in t.targets:
                    dg.edge(t.source.name, target.name, label=t.event)
        dg.render(str(output_path.with_suffix("")), format='svg')

    @staticmethod
    def plot_modern(statemachine_cls, output_path: Path) -> None:
        """
        Generates and renders a state machine diagram using DotGraphMachine (modern statemachine API).

        Args:
            statemachine_cls: The state machine class or instance.
            output_path: Path to the output SVG file.

        Side Effects:
            Creates and saves an SVG image of the state machine diagram at the specified path.

        Example:
            StateMachinePlotter.plot_modern(MyStateMachine, Path("output/diagram.svg"))
        """
        output_path = StateMachinePlotter._resolve_output_path(output_path)
        output_format = "svg"
        graph = DotGraphMachine(statemachine_cls).get_graph()
        graph.write(str(output_path), format=output_format)

